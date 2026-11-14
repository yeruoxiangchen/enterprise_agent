import asyncio
import json
from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult, ChatGeneration

from harness.tools.environment import create_environment
import harness.tools.domains
from kb.benchmark.crm_adapter import CRMAdapter, ExternalBusinessQA
from kb.benchmark.crm_metrics import summarize
from kb.config import Settings
from kb.context import ContextBudget

UPSTREAM = Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')


@pytest.mark.parametrize('free_mib,expected', [(20572, True), (19500, False), (18126, False)])
def test_shared_gpu_admission_preserves_service_budget_and_spare_memory(free_mib, expected):
    from crm_bench_services import gpu_has_room
    assert gpu_has_room({'free_mib': free_mib, 'total_mib': 24576}) is expected


class RecordedModel(BaseChatModel):
    replies: list
    seen: list = []

    @property
    def _llm_type(self):
        return 'scripted-crm-integration'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])


def reply(name, args, ident):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': ident}],
                     usage_metadata={'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15})


def adapter(tmp_path, arm='single'):
    return CRMAdapter(arm=arm, environment=create_environment('itsm'),
                      settings=Settings(root=tmp_path, llm_url='http://local.invalid/v1',
                                        llm_model='test', context_tokens=8192, output_tokens=1400),
                      config=yaml.safe_load((UPSTREAM/'src/agents/itsm.yaml').read_text()),
                      policy=(UPSTREAM/'src/tasks/policies/itsm_policy.md').read_text(),
                      seed=42, output=tmp_path)


def test_real_v5_graph_routes_tools_and_records_all_usage(tmp_path, monkeypatch):
    model = RecordedModel(replies=[
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['U0']}],
                            'clarification': '', 'unused_ids': []}, 'r'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 't'),
        reply('ExternalResult', {'answer': 'TCK-1001 is Open, priority High.', 'resolution': 'complete'}, 'f'),
    ])
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda self, messages, tools: (100, 'test'))
    owner = adapter(tmp_path, 'multi_v5')
    engine = ExternalBusinessQA(owner, 'Look up TCK-1001.', [], 0)
    result = engine.answer()
    assert 'Open' in result['answer']
    assert owner.model_calls == 3 and owner.prompt_tokens == 33 and owner.completion_tokens == 12
    assert owner.usage_missing_calls == 0
    assert len(owner.tool_calls) == 1 and owner.tool_calls[0]['ok']
    assert engine.state['calls'] == 3 and engine.state['tools'] == 1
    assert 'P1' in engine.state['completed_tasks']
    assert engine.answer() == result and owner.model_calls == 3
    assert (tmp_path/'business_checkpoints.sqlite3').exists()
    # The only benchmark material supplied to the actor is public capability
    # policy/schema and visible user dialogue, not tasks or gold assertions.
    assert all('expected_actions' not in str(messages) and 'expected_response_semantic' not in str(messages)
               for messages in model.seen)


def test_single_uses_actual_tool_state_and_no_router(tmp_path, monkeypatch):
    model = RecordedModel(replies=[
        reply('assign_ticket', {'ticket_id': 'TCK-1004', 'assignee': 'agent.jsmith'}, 't'),
        reply('ExternalResult', {'answer': 'Assigned TCK-1004 to agent.jsmith.', 'resolution': 'complete'}, 'f'),
    ])
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    owner = adapter(tmp_path)
    result = asyncio.run(owner.run('Assign TCK-1004 to agent.jsmith.', []))
    assert result.tool_calls_made[0]['name'] == 'assign_ticket'
    assert owner.environment.get_state()['tickets']['TCK-1004']['assigned_to'] == 'agent.jsmith'
    assert owner.model_calls == 2 and len(owner.turn_times) == 1


@pytest.mark.parametrize('arm', ['single', 'multi_v5'])
def test_saved_thanks_turn_does_not_redispatch_history_or_invoke_model(tmp_path, monkeypatch, arm):
    from harness.models import ChatMessage, Role
    model = RecordedModel(replies=[])
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    owner = adapter(tmp_path, arm)
    before = owner.environment.get_state()
    history = [ChatMessage(role=Role.USER, content='Could you look up the current status and priority of TCK-1001?'),
               ChatMessage(role=Role.ASSISTANT, content="TCK-1001 is Open, priority High.")]
    answer = asyncio.run(owner.run("thanks, that's all", history))
    assert answer.content == "You're welcome."
    assert owner.environment.get_state() == before and not owner.tool_calls
    assert owner.model_calls == 0 and owner.usage_missing_calls == 0
    assert not model.seen


def test_polite_new_business_requests_are_not_consumed_as_closings():
    from kb.benchmark.crm_adapter import closing_acknowledgement
    for text in ['Thanks, please close TCK-1004.', 'Thank you, now delete TCK-1001.',
                 '谢谢，请再查一下工单。', 'OK', 'Yes, confirm deletion.']:
        assert not closing_acknowledgement(text)
    assert closing_acknowledgement("Thanks, that’s all!")


def test_historical_task_id_is_rejected_then_repaired_before_any_tool_execution(tmp_path, monkeypatch):
    from harness.models import ChatMessage, Role
    model = RecordedModel(replies=[
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['H0_0']}],
                            'clarification': '', 'unused_ids': []}, 'bad'),
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['U0']}],
                            'clarification': '', 'unused_ids': []}, 'fixed'),
        reply('get_ticket', {'ticket_id': 'TCK-1004'}, 't'),
        reply('ExternalResult', {'answer': 'TCK-1004 is Open.', 'resolution': 'complete'}, 'f'),
    ])
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    owner = adapter(tmp_path, 'multi_v5')
    answer = asyncio.run(owner.run('Look up TCK-1004.', [ChatMessage(role=Role.USER, content='Look up TCK-1001.')]))
    assert 'TCK-1004' in answer.content and owner.model_calls == 4
    assert len(owner.tool_calls) == 1 and owner.tool_calls[0]['arguments'] == {'ticket_id': 'TCK-1004'}
    assert owner.usage_missing_calls == 0
    events = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert sum(e['kind'] == 'external_route_feedback' for e in events) == 1


def test_repeated_invalid_plans_are_bounded_known_failures_with_complete_usage(tmp_path, monkeypatch):
    from kb.benchmark.crm_adapter import RoutingPlanError
    from kb.benchmark.crm_metrics import local_verdict
    model = RecordedModel(replies=[
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['U404']}],
                            'clarification': '', 'unused_ids': []}, f'bad-{i}') for i in range(2)])
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    owner = adapter(tmp_path, 'multi_v5')
    with pytest.raises(RoutingPlanError) as caught:
        asyncio.run(owner.run('Look up TCK-1001.', []))
    assert owner.model_calls == 2 and not owner.tool_calls and owner.usage_missing_calls == 0
    assert owner.prompt_tokens == 22 and owner.completion_tokens == 8
    assert local_verdict(None, 'RoutingPlanError: '+str(caught.value)) is False


def test_cancel_fence_prevents_mutation(tmp_path, monkeypatch):
    owner = adapter(tmp_path)
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: RecordedModel(replies=[]))
    engine = ExternalBusinessQA(owner, 'Delete TCK-1004.', [], 0)
    delete = next(d for d in owner.registry.get_definitions() if d.function.name == 'delete_ticket')
    tool = engine.make_tool(delete)
    owner.stop.set()
    with pytest.raises(RuntimeError, match='cancelled'):
        tool.invoke({'ticket_id': 'TCK-1004'})
    assert 'TCK-1004' in owner.environment.get_state()['tickets']


def test_multiple_tools_in_one_model_call_commit_serially(tmp_path, monkeypatch):
    import time
    model = RecordedModel(replies=[
        AIMessage(content='', tool_calls=[
            {'name': 'get_ticket', 'args': {'ticket_id': 'TCK-1001'}, 'id': 'one'},
            {'name': 'list_tickets', 'args': {'status': 'Open'}, 'id': 'two'},
        ], usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}),
        reply('ExternalResult', {'answer': 'TCK-1001 is Open.', 'resolution': 'complete'}, 'f'),
    ])
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    owner = adapter(tmp_path)
    original = owner.write_environment
    active = {'now': 0, 'peak': 0}
    def snapshot():
        active['now'] += 1
        active['peak'] = max(active['now'], active['peak'])
        time.sleep(.03)
        original()
        active['now'] -= 1
    owner.write_environment = snapshot
    result = asyncio.run(owner.run('Look up TCK-1001 and list open tickets.', []))
    assert len(result.tool_calls_made) == 2 and all(t['ok'] for t in result.tool_calls_made)
    assert active['peak'] == 1
    assert sorted(t['execution_index'] for t in owner.tool_calls) == [0, 1]


def test_invalid_tool_arguments_remain_visible_to_forbidden_checks(tmp_path, monkeypatch):
    model = RecordedModel(replies=[
        AIMessage(content='', invalid_tool_calls=[{'name': 'delete_ticket', 'args': '{', 'id': 'bad',
                                                    'error': 'invalid JSON'}],
                  usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}),
        reply('ExternalResult', {'answer': 'I cannot delete that ticket.', 'resolution': 'complete'}, 'f'),
    ])
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    owner = adapter(tmp_path)
    with pytest.raises(ValueError, match='valid ExternalResult'):
        asyncio.run(owner.run('Do not delete TCK-1004.', []))
    from harness.grading import check_forbidden
    assert not check_forbidden(owner.tool_calls, forbidden_actions=['delete_ticket'])[0]
    assert 'TCK-1004' in owner.environment.get_state()['tickets']


def row(arm, trial, verdict, tokens=100):
    return {'arm': arm, 'task_id': 'case', 'trial': trial, 'official_verdict': verdict,
            'agent_usage': {'total_tokens': tokens, 'prompt_tokens': tokens-10,
                            'completion_tokens': 10, 'usage_missing_calls': 0, 'num_calls': 2},
            'agent_turn_times': [{'seconds': 2}], 'trace': {'tool_calls_detail': [{}]}}


def test_failure_spend_unknowns_and_all_three_reliability():
    rows = [row('single', 0, True), row('single', 1, False), row('single', 2, None)]
    rows += [row('multi_v5', trial, True, 120) for trial in range(3)]
    summary = summarize(rows, ['case'])
    a, b = summary['arms']['single'], summary['arms']['multi_v5']
    assert summary['complete']
    assert a['passed'] == 1 and a['unknown'] == 1
    assert a['official_success_over_scored'] == .5
    assert a['strict_success_lower_bound_over_completed'] == 1/3
    assert a['tokens_per_success'] == 300  # charges failed/unknown spend
    assert a['all_three_success'] == 0 and b['all_three_success'] == 1
    assert b['tokens_per_success'] == 120
    assert a['agent_cost_usd'] is None


def test_missing_usage_is_not_reported_as_free_or_complete():
    item = row('single', 0, True)
    item['agent_usage']['usage_missing_calls'] = 1
    result = summarize([item], ['case'])
    assert not result['complete']
    assert result['arms']['single']['tokens_per_success'] is None
    assert result['arms']['single']['reliability_tasks_completed'] == 0
    with pytest.raises(ValueError, match='duplicate'):
        summarize([item, item], ['case'])


def test_model_budget_failure_is_counted_without_hiding_official_unknown():
    from kb.benchmark.crm_metrics import local_verdict
    assert local_verdict(None, 'RuntimeError: 达到12次模型调用上限') is False
    assert local_verdict(None, 'OpenAIConnectionError: Connection error.') is None
    item = row('single', 0, None)
    item['local_verdict'] = False
    result = summarize([item], ['case'])['arms']['single']
    assert result['unknown'] == 0 and result['known_harness_failures'] == 1
    assert result['official_unknown'] == 1 and result['official_success_over_scored'] is None


@pytest.mark.parametrize('details', [None, {}, {'cached_tokens': 7}])
def test_actual_http_response_usage_is_translated_before_benchmark_validation(tmp_path, details):
    import httpx
    from crm_bench_services import LocalEvalClient
    from harness.models import ChatMessage, Role
    raw = {'choices': [{'message': {'content': '{"stop":false,"message":"Look up TCK-1001.","exit_conditions_met":false}'},
                         'finish_reason': 'stop'}],
           'usage': {'prompt_tokens': 101, 'completion_tokens': 19, 'total_tokens': 120,
                     'prompt_tokens_details': details, 'completion_tokens_details': None}}
    async def check():
        client = LocalEvalClient('http://test.invalid', 'test', tmp_path/'client.jsonl', 42)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)))
        try:
            reply = await client.chat([ChatMessage(role=Role.USER, content='Begin.')], response_format={
                'type': 'json_schema', 'json_schema': {'name': 'test', 'schema': {
                    'type': 'object', 'properties': {'message': {'type': 'string'}}, 'required': ['message']}}})
            assert reply.content == raw['choices'][0]['message']['content']
            assert reply.usage['prompt_tokens'] == 101 and reply.usage['total_tokens'] == 120
            assert reply.usage['cache_read_tokens'] == (7 if details else 0)
            assert all(type(v) is int for v in reply.usage.values())
            assert client.usage == {'num_calls': 1, 'prompt_tokens': 101, 'completion_tokens': 19,
                                    'total_tokens': 120, 'failed_calls': 0}
            recorded = json.loads((tmp_path/'client.jsonl').read_text())
            assert recorded['response']['usage'] == raw['usage']
            assert recorded['request']['messages'][0]['role'] == 'system'
            assert 'compact JSON' in recorded['request']['messages'][0]['content']
            assert '"required":["message"]' in recorded['request']['messages'][0]['content']
        finally:
            await client.aclose()
    asyncio.run(check())


def test_invalid_usage_retains_raw_response_without_inventing_token_counts(tmp_path):
    import httpx
    from crm_bench_services import LocalEvalClient
    from harness.models import ChatMessage, Role
    raw = {'choices': [{'message': {'content': 'answer'}, 'finish_reason': 'stop'}],
           'usage': {'prompt_tokens': None, 'completion_tokens': 19, 'prompt_tokens_details': None}}
    async def check():
        client = LocalEvalClient('http://test.invalid', 'test', tmp_path/'client.jsonl', 42)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)))
        try:
            with pytest.raises(ValueError, match='prompt_tokens'):
                await client.chat([ChatMessage(role=Role.USER, content='Begin.')])
            assert client.usage['failed_calls'] == 1 and client.usage['total_tokens'] == 0
            assert json.loads((tmp_path/'client.jsonl').read_text())['response'] == raw
        finally:
            await client.aclose()
    asyncio.run(check())


def test_normalized_http_usage_through_official_user_agent_and_semantic_grading(tmp_path, monkeypatch):
    import httpx
    from crm_bench_services import LocalEvalClient
    from crm_benchmark import finish_deterministic_checks
    from harness.bench_runner import load_tasks, run_task, _trace_task_success
    from harness.semantic_grader import SemanticJudge, SemanticJudgeConfig
    from harness.user_simulator import ReactUserSimulator
    answer = 'TCK-1001 is Open, priority High.'
    model = RecordedModel(replies=[
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 't'),
        reply('ExternalResult', {'answer': answer, 'resolution': 'complete'}, 'f'),
    ])
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    task = load_tasks(str(UPSTREAM/'src/tasks/itsm.yaml'))[0]
    decisions = iter([{'stop': False, 'message': 'Look up the status and priority of TCK-1001.',
                       'exit_conditions_met': False},
                      {'stop': True, 'message': '', 'exit_conditions_met': True}])

    def transport(request):
        data = json.loads(request.content)
        schema = data['response_format']['json_schema']['name']
        if schema == 'benchmark_user_turn':
            content = next(decisions)
        else:
            content = {'criteria': [
                {'criterion_id': f'criterion_{i}', 'met': True, 'evidence': [answer], 'explanation': 'Exact fact stated.'}
                for i in range(1, len(task.expected_response_semantic)+1)],
                'contradiction_found': False, 'contradiction_evidence': ''}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(content)}, 'finish_reason': 'stop'}],
                                        'usage': {'prompt_tokens': 101, 'completion_tokens': 19, 'total_tokens': 120,
                                                  'prompt_tokens_details': None, 'completion_tokens_details': None}})

    async def check():
        user_client = LocalEvalClient('http://test.invalid', 'test', tmp_path/'user.jsonl', 42)
        judge_client = LocalEvalClient('http://test.invalid', 'test', tmp_path/'judge.jsonl', 42)
        for client in (user_client, judge_client):
            await client.client.aclose()
            client.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        owner = adapter(tmp_path)
        judge = SemanticJudge(judge_client, SemanticJudgeConfig(provider='openai', model='test',
                                                               cache_enabled=False, cache_dir=tmp_path/'cache'))
        try:
            trace = await run_task(task, owner, ReactUserSimulator(user_client), semantic_judge=judge,
                                   max_turns=3, timeout_seconds=30, agent_step_timeout_seconds=10,
                                   user_step_timeout_seconds=10)
            finish_deterministic_checks(task, trace, owner)
            assert trace.error is None and _trace_task_success(trace) is True
            assert trace.stopped and len(trace.tool_calls_detail) == 1
            assert user_client.usage['total_tokens'] == 240 and judge_client.usage['total_tokens'] == 120
            assert owner.prompt_tokens+owner.completion_tokens == 30  # excludes simulator and judge
        finally:
            await user_client.aclose()
            await judge_client.aclose()
    asyncio.run(check())


def test_real_chatopenai_transport_binds_final_response_and_budgets_identical_schemas(tmp_path, monkeypatch):
    from langchain_openai import ChatOpenAI as ActualChatOpenAI
    from langchain_openai._compat import httpx as sdk_httpx
    from langchain_core.utils.function_calling import convert_to_openai_tool
    captured, budgeted = [], []
    def transport(request):
        payload = json.loads(request.content)
        captured.append(payload)
        name, arguments = (('get_ticket', {'ticket_id': 'TCK-1001'}) if len(captured) == 1 else
                           ('ExternalResult', {'answer': 'TCK-1001 is Open, priority High.', 'resolution': 'complete'}))
        return sdk_httpx.Response(200, json={
            'id': f'chatcmpl-test-{len(captured)}', 'object': 'chat.completion', 'created': 1, 'model': 'test',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'tool-{len(captured)}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 101, 'completion_tokens': 19, 'total_tokens': 120,
                      'prompt_tokens_details': None, 'completion_tokens_details': None}})
    client = sdk_httpx.Client(transport=sdk_httpx.MockTransport(transport))
    async_client = sdk_httpx.AsyncClient(transport=sdk_httpx.MockTransport(transport))
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI',
                        lambda **kwargs: ActualChatOpenAI(**kwargs, http_client=client, http_async_client=async_client))
    def count(self, messages, tools):
        budgeted.append([convert_to_openai_tool(t) for t in tools])
        return 100, 'test'
    monkeypatch.setattr(ContextBudget, 'count', count)
    try:
        owner = adapter(tmp_path)
        result = asyncio.run(owner.run('Look up TCK-1001.', []))
        assert 'Open' in result.content and owner.model_calls == 2
        assert len(captured) == 2
        for request, counted in zip(captured, budgeted):
            assert request['tools'] == counted
            final = next(t for t in request['tools'] if t['function']['name'] == 'ExternalResult')
            assert 'Finish this turn' in final['function']['description']
            assert request['tool_choice'] == 'required'
        assert captured[1]['messages'][-1]['role'] == 'tool'
        assert 'TCK-1001' in captured[1]['messages'][-1]['content']
        assert owner.prompt_tokens+owner.completion_tokens == 240
        assert owner.usage_missing_calls == 0
    finally:
        client.close()
        asyncio.run(async_client.aclose())
