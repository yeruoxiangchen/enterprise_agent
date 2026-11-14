import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harness.bench_runner import ConversationTrace
from harness.models import ChatMessage, Role
from harness.semantic_grader import SemanticJudge, SemanticJudgeConfig, SemanticJudgeError
from crm_bench_resume import continuation_plan, file_sha, import_saved_row
from crm_bench_services import LocalEvalClient, WIRE_FORMAT_VERSION


def provider_response(content):
    return httpx.Response(200, json={
        'choices': [{'message': {'content': json.dumps(content)}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 11, 'completion_tokens': 4, 'total_tokens': 15}})


def judgement(answer, contradiction=False):
    return {'criteria': [{'criterion_id': 'criterion_1', 'met': not contradiction,
                          'evidence': [] if contradiction else [answer],
                          'explanation': 'The stated fact is checked.'}],
            'contradiction_found': contradiction,
            'contradiction_evidence': answer if contradiction else ''}


@pytest.mark.parametrize('contradiction,fabricated', [(False, False), (True, False), (True, True)])
def test_evidence_format_guidance_preserves_official_quote_validation(tmp_path, contradiction, fabricated):
    answer = 'Ticket TCK-9999 was not found.'
    body = judgement('An invented assistant quote.' if fabricated else answer, contradiction)
    requests = []
    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        system = payload['messages'][0]['content']
        assert 'contradiction_evidence MUST be the empty string ""' in system
        assert 'contiguous exact excerpt' in system
        return provider_response(body)
    async def check():
        client = LocalEvalClient('http://test.invalid', 'test', tmp_path/'judge.jsonl', 42)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        judge = SemanticJudge(client, SemanticJudgeConfig(provider='openai', model='test',
                                                         cache_enabled=False, cache_dir=tmp_path/'cache'))
        try:
            kwargs = {'task_id': 'case', 'task_instruction': 'Look up the ticket.',
                      'criteria': ['Explain that the ticket could not be found.'],
                      'turns': [{'role': 'assistant', 'content': answer}]}
            if fabricated:
                with pytest.raises(SemanticJudgeError):
                    await judge.grade(**kwargs)
                assert len(requests) == 3
            else:
                outcome = await judge.grade(**kwargs)
                assert outcome.passed is (not contradiction)
                assert len(requests) == 1
            # The bridge instructs the model; it never edits its returned verdict/quotes.
            recorded = [json.loads(line) for line in (tmp_path/'judge.jsonl').read_text().splitlines()]
            assert all(json.loads(item['response']['choices'][0]['message']['content']) == body for item in recorded)
        finally:
            await client.aclose()
    asyncio.run(check())


def test_evidence_guidance_is_not_added_to_simulated_user_requests(tmp_path):
    async def check():
        client = LocalEvalClient('http://test.invalid', 'test', tmp_path/'user.jsonl', 42)
        await client.client.aclose()
        def transport(request):
            payload = json.loads(request.content)
            assert 'EVIDENCE FIELD FORMAT' not in payload['messages'][0]['content']
            return provider_response({'stop': False, 'message': 'Hello', 'exit_conditions_met': False})
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        try:
            await client.chat([ChatMessage(role=Role.USER, content='Start.')], response_format={
                'type': 'json_schema', 'json_schema': {'name': 'benchmark_user_turn',
                    'schema': {'type': 'object', 'additionalProperties': False,
                        'properties': {'stop': {'type': 'boolean'}, 'message': {'type': 'string'},
                                       'exit_conditions_met': {'type': 'boolean'}},
                        'required': ['stop', 'message', 'exit_conditions_met']}}})
        finally:
            await client.aclose()
    asyncio.run(check())


def test_continuation_rejects_actor_changes_and_changed_parent_rows(tmp_path):
    actor = tmp_path/'actor.py'
    actor.write_text('frozen actor')
    parent = tmp_path/'parent'
    (parent/'rows').mkdir(parents=True)
    protocol = {'source_sha256': {str(actor): file_sha(actor)}, 'task_ids': ['case', 'missing'],
                'trials': 1, 'arms': ['single', 'multi_v5'], 'context_tokens': 16384,
                'evaluation_wire_format': 'v1'}
    (parent/'protocol.json').write_text(json.dumps(protocol))
    (parent/'status.json').write_text(json.dumps({'resources_released': True}))
    for arm in protocol['arms']:
        row = {'arm': arm, 'task_id': 'case', 'trial': 0, 'smoke': False,
               'trace': {'task_id': 'case', 'error_kind': None}}
        (parent/'rows'/f'case__0__{arm}.json').write_text(json.dumps(row))
    current = {**protocol, 'evaluation_wire_format': WIRE_FORMAT_VERSION}
    plan = continuation_plan(parent, current, [])
    assert plan['inherited_trials'] == 2 and plan['pending_actor_trials'] == 2
    assert all(item['sha256'] == file_sha(item['path']) for item in plan['entries'])
    with pytest.raises(RuntimeError, match='actor/user/task settings'):
        continuation_plan(parent, {**current, 'context_tokens': 8192}, [])
    actor.write_text('changed actor')
    with pytest.raises(RuntimeError, match='Frozen actor'):
        continuation_plan(parent, current, [])


@pytest.mark.parametrize('error_kind,error,regraded,verdict', [
    (None, None, True, True),
    ('grader_infrastructure', 'SemanticJudgeError: invalid quote', True, True),
    (None, 'RuntimeError: 达到12次模型调用上限', False, False),
    ('user_simulator_divergence', 'UserSimulatorError: invalid stop', False, None),
])
def test_saved_trace_regrade_keeps_actor_work_and_all_previous_judge_costs(
        tmp_path, monkeypatch, error_kind, error, regraded, verdict):
    answer = 'Ticket TCK-9999 was not found.'
    trace = ConversationTrace(task_id='case', turns=[{'role': 'assistant', 'content': answer}],
                              error=error, error_kind=error_kind, stopped=True)
    original = {'task_id': 'case', 'trial': 0, 'arm': 'single', 'smoke': False, 'trace': asdict(trace),
                'official_verdict': None, 'local_verdict': None,
                'agent_usage': {'prompt_tokens': 31, 'completion_tokens': 7,
                                'total_tokens': 38, 'num_calls': 2, 'usage_missing_calls': 0},
                'agent_turn_times': [{'seconds': 3.25}],
                'judge_usage': {'num_calls': 3, 'prompt_tokens': 30, 'completion_tokens': 9,
                                'total_tokens': 39, 'failed_calls': 0}, 'judge_llm_seconds': 2,
                'finished_at': 1, 'trial_output': '/frozen/source'}
    source = tmp_path/'case__0__single.json'
    source.write_text(json.dumps(original))
    sha = file_sha(source)
    entry = {'path': str(source), 'sha256': sha, 'filename': source.name}
    calls = []
    def factory(*args, **kwargs):
        client = LocalEvalClient(*args, **kwargs)
        asyncio.get_running_loop().create_task(client.client.aclose())
        def transport(request):
            calls.append(request)
            return provider_response(judgement(answer))
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        return client
    monkeypatch.setattr('crm_bench_services.LocalEvalClient', factory)
    services = SimpleNamespace(owned={'auxiliary': {'url': 'http://test.invalid', 'model': 'test',
                                                   'model_path': '/frozen/model'}})
    protocol = {'judge_seed': 42, 'judge_max_tokens': 4096, 'judge_max_attempts': 3,
                'evaluation_wire_format': WIRE_FORMAT_VERSION}
    task = SimpleNamespace(id='case', instruction='Look up the ticket.',
                           expected_response_semantic=['Explain that the ticket could not be found.'])
    output = tmp_path/'continued'
    row = asyncio.run(import_saved_row(output, entry, protocol, services, task))
    assert row['regraded'] is regraded and row['local_verdict'] is verdict
    assert row['agent_usage'] == original['agent_usage'] and row['agent_turn_times'] == original['agent_turn_times']
    assert row['trace']['turns'] == original['trace']['turns']
    assert file_sha(source) == sha and row['source_row']['sha256'] == sha
    assert len(calls) == (1 if regraded else 0)
    assert row['judge_usage']['total_tokens'] == (54 if regraded else 39)
    assert row['previous_grade']['judge_usage']['total_tokens'] == 39
    assert (output/'rows'/source.name).exists()
    source.write_text('changed parent row')
    with pytest.raises(RuntimeError, match='Frozen parent row changed'):
        asyncio.run(import_saved_row(output, entry, protocol, services, task))
