import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from crm_bench_services import LocalEvalClient
from harness.models import ChatMessage, Role
from harness.user_simulator import STOP_SIGNAL
from kb.benchmark.crm_dialogue import ContextualReactUserSimulator, SimulatorFidelityError
from test_crm_user_fidelity import Client, decision, simulator


def test_customer_transcript_preserves_speakers_and_supplied_identity():
    client = Client([decision('Please remove request REQ-52.'), decision('My user ID is user.alex.')])
    sim = ContextualReactUserSimulator(client, temperature=0)
    instruction = 'You are user.alex. Ask to remove request REQ-52; supply your identity if asked.'
    sim.reset(instruction)

    async def run():
        assert await sim.step('') == 'Please remove request REQ-52.'
        assert await sim.step('Which user ID should I check?') == 'My user ID is user.alex.'
    asyncio.run(run())
    prompt = json.loads(client.requests[1]['messages'][-1].content)
    assert prompt['original_customer_instruction'] == instruction
    assert prompt['visible_dialogue'] == [
        {'speaker': 'customer', 'message': 'Please remove request REQ-52.'},
        {'speaker': 'service_agent', 'message': 'Which user ID should I check?'},
    ]
    assert prompt['next_speaker'] == 'customer'
    assert sim._messages[-2].role == Role.USER
    assert sim._messages[-2].content == 'Which user ID should I check?'
    assert sim._messages[-1].content == 'My user ID is user.alex.'
    assert all('original_customer_instruction' not in m.content for m in sim._messages)


def test_customer_keeps_react_validation_and_excludes_failed_candidates_from_history():
    client = Client([{'stop': False, 'message': 'invalid decision', 'exit_conditions_met': True},
                     decision('Please record "Original note" on request REQ-52.')])
    sim = ContextualReactUserSimulator(client, temperature=0)
    sim.reset('Request that the exact note "Original note" be recorded on REQ-52.')
    result = asyncio.run(sim.step(''))
    assert result == 'Please record "Original note" on request REQ-52.'
    assert client.usage['num_calls'] == 2
    assert all('invalid decision' not in (m.content or '') for m in sim._messages)
    assert all('protocol' not in (m.content or '').lower() for m in sim._messages[1:])


@pytest.mark.parametrize('valid', [True, False])
def test_compact_review_keeps_reference_rubric_and_structured_verdict(tmp_path, valid):
    client = Client([{'valid': valid, 'reason': 'Matches the stated customer goal.' if valid else 'Invents a new goal.'}])
    sim = simulator(tmp_path, client, verification_mode='compact')
    candidate = 'What is the status of TCK-1001?' if valid else 'Delete a different request.'
    verdict = asyncio.run(sim._review_decision(candidate))
    assert verdict.valid is valid
    request = client.requests[0]
    assert 'Extract ALL goals and exit conditions' in request['messages'][-1].content
    assert candidate in request['messages'][-1].content
    schema = request['response_format']['json_schema']['schema']
    assert schema['required'] == ['valid', 'reason']
    assert schema['properties']['reason']['maxLength'] == 240
    assert request['max_tokens'] == 256


@pytest.mark.parametrize('mode,content', [
    ('upstream', 'PASS: correct but cut off'),
    ('compact', '{"valid":true,"reason":"Correct."}'),
])
def test_truncated_review_cannot_be_accepted_even_with_a_positive_prefix(tmp_path, mode, content):
    class TruncatedClient(Client):
        async def chat(self, **kwargs):
            response = await super().chat(**kwargs)
            response.finish_reason = 'length'
            return response
    sim = simulator(tmp_path, TruncatedClient([content]), verification_mode=mode)
    with pytest.raises(SimulatorFidelityError, match='Incomplete customer review'):
        asyncio.run(sim._review_decision('What is the status of TCK-1001?'))
    assert not (tmp_path/'opening.json').exists()


@pytest.mark.parametrize('content', [
    'PASS: not JSON',
    '{"valid":true}',
    json.dumps({'valid': True, 'reason': 'x' * 241}),
])
def test_compact_review_rejects_malformed_or_overlong_verdicts(tmp_path, content):
    sim = simulator(tmp_path, Client([content]), verification_mode='compact')
    with pytest.raises(SimulatorFidelityError, match='Invalid compact customer review'):
        asyncio.run(sim._review_decision('What is the status of TCK-1001?'))


@pytest.mark.parametrize('finish_reason', ['stop', 'length'])
def test_customer_http_client_accounts_for_truncated_output_without_delivering_it(tmp_path, monkeypatch, finish_reason):
    client = LocalEvalClient('http://local.invalid', 'model', tmp_path/'model.jsonl', 42, require_complete=True)
    async def post(url, **kwargs):
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'choices': [{'message': {'content': json.dumps(decision('Please check my request.'))},
                         'finish_reason': finish_reason}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30},
        })
    monkeypatch.setattr(client.client, 'post', post)
    async def run():
        try:
            return await client.chat([ChatMessage(role=Role.USER, content='Generate a customer turn.')])
        finally:
            await client.aclose()
    if finish_reason == 'length':
        with pytest.raises(SimulatorFidelityError, match='Incomplete customer output'):
            asyncio.run(run())
    else:
        assert asyncio.run(run()).finish_reason == 'stop'
    assert client.usage['num_calls'] == 1 and client.usage['total_tokens'] == 30
    assert client.usage['failed_calls'] == int(finish_reason == 'length')
    audit = json.loads((tmp_path/'model.jsonl').read_text())
    assert audit['response']['choices'][0]['finish_reason'] == finish_reason
    assert ('error' in audit) == (finish_reason == 'length')
