import asyncio
import copy
import json
from itertools import count
from types import SimpleNamespace

import httpx
import pytest
import yaml

import crm_benchmark as benchmark
from crm_bench_services import LocalEvalClient, InvalidActorToolCall, parse_tool_calls
from harness.models import ChatResponse, ToolCall
from harness.tools.environment import create_environment
import harness.tools.domains
from kb.benchmark.crm_metrics import local_verdict, summarize, report_markdown
from kb.benchmark.crm_reference import OfficialGraphAdapter, EXECUTION_VERSION
from kb.config import Settings
from kb.context import ContextBudget
from test_crm_benchmark import UPSTREAM, row


call_ids = count()


def call(name, **arguments):
    return ToolCall(id=f'call-{next(call_ids)}', name=name, arguments=arguments)


class Client:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.usage = dict(num_calls=0, prompt_tokens=0, completion_tokens=0)
        self.metered_calls, self.seconds, self.seed = 0, 0.0, 42
    async def chat(self, messages, **kwargs):
        self.requests.append((copy.deepcopy(messages), kwargs))
        self.usage['num_calls'] += 1
        self.usage['prompt_tokens'] += 10
        self.usage['completion_tokens'] += 5
        self.metered_calls += 1
        return self.responses.pop(0)
    async def aclose(self): pass


def reference(tmp_path, monkeypatch, responses, **limits):
    config = yaml.safe_load((UPSTREAM/'src/agents/itsm.yaml').read_text())
    policy = (UPSTREAM/'src/tasks/policies/itsm_policy.md').read_text()
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    owner = OfficialGraphAdapter(environment=create_environment('itsm'),
        settings=Settings(root=tmp_path, llm_url='http://local.invalid/v1', llm_model='test',
                          context_tokens=8192, output_tokens=1400),
        config=config, policy=policy, seed=42, output=tmp_path, **limits)
    original = owner.http
    asyncio.run(original.aclose())
    owner.http = Client(responses)
    return owner


def response(*calls, content=''):
    return ChatResponse(content=content, tool_calls=list(calls), finish_reason='tool_calls' if calls else 'stop')


def test_official_graph_routes_to_scoped_tools_and_meters_router(tmp_path, monkeypatch):
    owner = reference(tmp_path, monkeypatch, [response(call('ticket_operations__tickets')),
        response(call('get_ticket', ticket_id='TCK-1001')), response(content='TCK-1001 is Open.')])
    result = asyncio.run(owner.run('Look up TCK-1001.', []))
    assert type(owner.graph).__name__ == 'GraphAgent'
    assert result.topic == 'ticket_operations__tickets'
    assert result.content == 'TCK-1001 is Open.'
    assert owner.model_calls == 3 and owner.prompt_tokens == 30 and owner.usage_missing_calls == 0
    assert [c['name'] for c in owner.tool_calls] == ['get_ticket']
    assert owner.tool_calls[0]['ok'] and owner.tool_calls[0]['backend_executed']
    assert owner.turn_times[0]['model_calls'] == 3 and owner.turn_times[0]['tool_calls'] == 1
    tools = {t.function.name for t in owner.http.requests[1][1]['tools']}
    assert 'get_ticket' in tools and 'delete_ticket' not in tools
    assert all(kw['max_tokens'] == 1400 for _, kw in owner.http.requests)
    assert all('SpanRoute' not in str(messages) and 'expected_actions' not in str(messages)
               for messages, _ in owner.http.requests)


def test_audit_wrapper_leaves_official_write_behavior_and_duplicate_calls_unchanged(tmp_path, monkeypatch):
    owner = reference(tmp_path, monkeypatch, [response(call('ticket_operations__tickets')),
        response(call('add_ticket_comment', ticket_id='TCK-1001', comment='Model-chosen note'),
                 call('add_ticket_comment', ticket_id='TCK-1001', comment='Model-chosen note')),
        response(content='Added the notes.')])
    asyncio.run(owner.run('Add "Original note" to TCK-1001.', []))
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Model-chosen note'] * 2
    assert [c['business_outcome'] for c in owner.tool_calls] == ['committed'] * 2
    assert len(owner.tool_calls) == 2 and owner.turn_tool_attempts == 3


def test_backend_permission_failure_is_observed_without_custom_identity_guard(tmp_path, monkeypatch):
    owner = reference(tmp_path, monkeypatch, [response(call('record_safety__deletion')),
        response(call('check_permission', user_id='unknown.user', action='delete', ticket_id='TCK-1004')),
        response(content='Permission was not granted.')])
    asyncio.run(owner.run('Can I delete TCK-1004?', []))
    assert owner.tool_calls[0]['backend_executed'] is True
    assert owner.tool_calls[0]['arguments']['user_id'] == 'unknown.user'
    assert 'TCK-1004' in owner.environment.get_state()['tickets']


def test_interrupted_official_turn_keeps_prior_write_and_failed_tool_attempt(tmp_path, monkeypatch):
    owner = reference(tmp_path, monkeypatch, [response(call('ticket_operations__tickets')),
        response(call('add_ticket_comment', ticket_id='TCK-1001', comment='Once'), call('unavailable_tool'))])
    with pytest.raises(Exception, match='not found in available tools'):
        asyncio.run(owner.run('Add "Once" to TCK-1001.', []))
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']
    assert owner.tool_calls[0]['ok'] and owner.tool_calls[0]['backend_executed']
    assert not owner.tool_calls[1]['ok'] and not owner.tool_calls[1]['backend_executed']
    assert owner.model_calls == 2 and owner.turn == 1 and owner.turn_times


def test_reference_model_budget_preserves_real_observations(tmp_path, monkeypatch):
    owner = reference(tmp_path, monkeypatch, [response(call('ticket_operations__tickets')),
        response(call('get_ticket', ticket_id='TCK-1001'))], model_call_limit=2)
    with pytest.raises(RuntimeError, match='Reference model-call budget exhausted'):
        asyncio.run(owner.run('Look up TCK-1001.', []))
    assert owner.model_calls == 2 and owner.tool_calls[0]['ok']
    assert local_verdict(None, 'RuntimeError: Reference model-call budget exhausted') is False


def test_reference_tool_budget_includes_route_and_preserves_unexecuted_attempts(tmp_path, monkeypatch):
    owner = reference(tmp_path, monkeypatch, [response(call('ticket_operations__tickets')),
        response(call('get_ticket', ticket_id='TCK-1001'), call('get_ticket', ticket_id='TCK-1002'))], tool_call_limit=2)
    with pytest.raises(RuntimeError, match='Reference tool-call budget exhausted'):
        asyncio.run(owner.run('Look up two tickets.', []))
    assert len(owner.tool_calls) == 2 and all(not c['backend_executed'] for c in owner.tool_calls)


@pytest.mark.parametrize('raw', ['[1]', '{bad JSON'])
def test_malformed_actor_arguments_are_identified(raw):
    with pytest.raises(InvalidActorToolCall):
        parse_tool_calls({'tool_calls': [{'id': 'x', 'function': {'name': 'get_ticket', 'arguments': raw}}]})


def test_actor_http_bridge_preserves_function_calls_and_nested_usage(tmp_path, monkeypatch):
    client = LocalEvalClient('http://local.invalid', 'test', tmp_path/'model.jsonl', 42)
    async def post(url, **kwargs):
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'choices': [{'message': {'content': None, 'tool_calls': [{'id': 'tool-1', 'type': 'function',
                'function': {'name': 'get_ticket', 'arguments': '{"ticket_id":"TCK-1001"}'}}]}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30, 'prompt_tokens_details': None}})
    monkeypatch.setattr(client.client, 'post', post)
    async def run():
        try: return await client.chat([])
        finally: await client.aclose()
    actual = asyncio.run(run())
    assert actual.tool_calls[0].arguments == {'ticket_id': 'TCK-1001'}
    assert actual.usage['total_tokens'] == 30 and client.metered_calls == 1


def test_three_arm_summary_has_twelve_records_and_pairwise_outcomes():
    arms = ('official_graph','single_v6','multi_v6')
    rows = [row(arm, 0, arm != 'official_graph') for arm in arms]
    summary = summarize(rows, ['case'], trials=1, arms=arms)
    assert summary['complete'] and summary['expected_trials'] == 3
    assert len(summary['pairwise']) == 3
    pair = summary['pairwise'][0]
    assert pair['left'] == 'official_graph' and pair['right_only'] == 1
    assert 'official_graph' in report_markdown(summary)
    assert '仅运行一个方案' not in report_markdown(summary)


def test_reference_protocol_and_source_archive_freeze_both_arms(tmp_path, monkeypatch):
    values = dict(HARNESS_VERSION='v6', EXECUTION_ADAPTER='v4', ARMS=('official_graph','multi_v6'),
                  REQUESTED_TASK_IDS=tuple(__import__('crm_acceptance').TASK_IDS), RUN_TRIALS=1,
                  SHARED_EVAL_MODEL=True, USER_STRATEGY='contextual-react', MAX_RUN_HOURS=.5)
    for key, value in values.items(): monkeypatch.setattr(benchmark, key, value)
    protocol = asyncio.run(benchmark.preflight(tmp_path, '2'))
    assert protocol['expected_trials'] == 8
    assert protocol['adapter_by_arm']['official_graph'] == EXECUTION_VERSION
    assert protocol['reference']['custom_business_guards'] is False
    assert protocol['reference']['model_calls_per_turn'] == protocol['model_calls_per_turn'] == 24
    assert protocol['user_strategy'] == 'contextual-react'
    manifest = json.loads((tmp_path/'source_manifest.json').read_text())
    source = next(k for k in manifest if k.endswith('/crm_reference.py'))
    assert (tmp_path/manifest[source]['copy']).read_bytes() == __import__('pathlib').Path(source).read_bytes()


def test_cli_preserves_reference_arm_and_small_shared_model_protocol(tmp_path, monkeypatch):
    for key in ('HARNESS_VERSION','EXECUTION_ADAPTER','ARMS','REQUESTED_TASK_IDS','RUN_TRIALS',
                'SHARED_EVAL_MODEL','USER_STRATEGY','MAX_RUN_HOURS'):
        monkeypatch.setattr(benchmark, key, getattr(benchmark, key))
    monkeypatch.setattr(benchmark, 'OUTPUT', tmp_path)
    tasks = ','.join(__import__('crm_acceptance').TASK_IDS)
    monkeypatch.setattr(benchmark.sys, 'argv', ['crm_benchmark.py','launch','--tag','reference-small',
        '--harness-version','v6','--execution-adapter','v4','--arms','official_graph','multi_v6',
        '--task-ids',tasks,'--trials','1','--shared-eval-model','--user-strategy','contextual-react','--max-hours','.5'])
    commands = []
    monkeypatch.setattr(benchmark.subprocess, 'Popen', lambda command, **kwargs: commands.append(command) or SimpleNamespace(pid=999999))
    benchmark.main()
    command = commands[0]
    assert command[command.index('--arms')+1:command.index('--arms')+3] == ['official_graph','multi_v6']
    assert command[command.index('--user-strategy')+1] == 'contextual-react'


def test_two_arm_reference_comparison_names_the_actual_baseline():
    summary = summarize([row('official_graph',0,False), row('multi_v6',0,True)], ['case'],
                        trials=1, arms=('official_graph','multi_v6'))
    assert summary['paired'] == {'official_graph_only': 0, 'multi_v6_only': 1,
                                 'both_pass': 0, 'both_fail': 0, 'unknown': 0}
    assert '仅左成功' in report_markdown(summary)
    assert 'single_only' not in report_markdown(summary)
