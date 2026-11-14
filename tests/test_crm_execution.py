"""Small integrations through real LangChain/LangGraph and public ITSM state."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from kb.benchmark.crm_execution import ExternalBusinessQA, request_sources
from kb.context import ContextBudget
from test_crm_benchmark import RecordedModel, adapter, reply
from test_crm_v6 import final


def contract(question, *steps, domain='service', disposition='execute', explanation=''):
    return reply('SpanRoute', {'tasks': [{'source_ids': [s['id'] for s in request_sources(question)], 'domain': domain,
        'steps': [{'tool': name, 'arguments': args} for name, args in steps],
        'disposition': disposition, 'explanation': explanation}]}, 'intake')


def setup(tmp_path, monkeypatch, question, replies, arm='single_v6'):
    model = RecordedModel(replies=list(replies))
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    owner = adapter(tmp_path, arm)
    owner.execution_adapter = 'v3'
    return owner, ExternalBusinessQA(owner, question, [], 0), model


@pytest.mark.parametrize('arm', ['single_v6', 'multi_v6'])
def test_query_finishes_from_real_evidence_and_accounts_for_shared_intake(tmp_path, monkeypatch, arm):
    q = "What's the status of TCK-1001?"
    owner, engine, model = setup(tmp_path, monkeypatch, q, [
        contract(q, ('get_ticket', {'ticket_id': 'TCK-1001'})),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'read'), final('It is Closed.'),
    ], arm)
    result = engine.answer()
    assert result['status'] == 'answered' and 'status Open' in result['answer']
    assert owner.model_calls == 3 and owner.prompt_tokens == 33 and owner.usage_missing_calls == 0
    assert all('expected_actions' not in str(messages) for messages in model.seen)
    assert engine.answer() == result and owner.model_calls == 3


@pytest.mark.parametrize('arm', ['single_v6', 'multi_v6'])
def test_semantic_note_intent_is_not_lost_by_keyword_matching(tmp_path, monkeypatch, arm):
    q = 'TCK-1001 needs the note "Ready".'
    args = {'ticket_id': 'TCK-1001', 'comment': 'Ready'}
    owner, engine, _ = setup(tmp_path, monkeypatch, q, [
        contract(q, ('add_ticket_comment', args)), reply('add_ticket_comment', args, 'note'), final(),
    ], arm)
    assert engine.answer()['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Ready']
    assert engine.state['plan'][0]['question'] == q
    events = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    request = next(e['data'] for e in events if e['kind'] == 'model_request')
    schema = request['tools'][0]['function']['parameters']
    def source_enums(value):
        if isinstance(value, dict):
            properties = value.get('properties', {})
            if 'source_ids' in properties:
                yield properties['source_ids']['items']['enum']
            for child in value.values():
                yield from source_enums(child)
        elif isinstance(value, list):
            for child in value:
                yield from source_enums(child)
    assert list(source_enums(schema)) == [['U0']]
    assert 'source_quote' not in json.dumps(schema)


def test_unexecuted_and_partial_requests_cannot_claim_completion(tmp_path, monkeypatch):
    q = 'Add "Ready" to TCK-1001, then set its status to Resolved.'
    note = {'ticket_id': 'TCK-1001', 'comment': 'Ready'}
    status = {'ticket_id': 'TCK-1001', 'status': 'Resolved'}
    owner, engine, _ = setup(tmp_path, monkeypatch, q, [
        contract(q, ('add_ticket_comment', note), ('update_ticket_status', status)),
        reply('add_ticket_comment', note, 'note'), final('Everything is done.'),
    ])
    result = engine.answer()
    assert result['status'] == 'insufficient' and 'remaining requested steps' in result['answer']
    assert owner.environment.get_state()['tickets']['TCK-1001']['status'] == 'Open'
    assert [s['status'] for s in next(iter(engine.state['execution_contracts'].values()))] == ['completed', 'pending']


def test_wrong_order_is_rejected_before_state_change_and_can_be_repaired(tmp_path, monkeypatch):
    q = 'Add "Ready" to TCK-1001, then set its status to Resolved.'
    note = {'ticket_id': 'TCK-1001', 'comment': 'Ready'}
    status = {'ticket_id': 'TCK-1001', 'status': 'Resolved'}
    owner, engine, _ = setup(tmp_path, monkeypatch, q, [
        contract(q, ('add_ticket_comment', note), ('update_ticket_status', status)),
        reply('update_ticket_status', status, 'too-soon'), reply('add_ticket_comment', note, 'note'),
        reply('update_ticket_status', status, 'status'), final(),
    ])
    assert engine.answer()['status'] == 'answered'
    assert not owner.tool_calls[0]['backend_executed']
    assert [c['name'] for c in owner.tool_calls if c['backend_executed']] == ['add_ticket_comment', 'update_ticket_status']


def test_explicit_duplicate_operations_have_distinct_stable_step_ids(tmp_path, monkeypatch):
    q = 'TCK-1001 needs "Ready" twice, as two separate notes.'
    args = {'ticket_id': 'TCK-1001', 'comment': 'Ready'}
    owner, engine, _ = setup(tmp_path, monkeypatch, q, [
        contract(q, ('add_ticket_comment', args), ('add_ticket_comment', args)),
        reply('add_ticket_comment', args, 'one'), reply('add_ticket_comment', args, 'two'),
        reply('add_ticket_comment', args, 'retry'), final(),
    ])
    assert engine.answer()['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Ready', 'Ready']
    assert [c['backend_executed'] for c in owner.tool_calls] == [True, True, False]
    steps = next(iter(engine.state['execution_contracts'].values()))
    assert len({s['id'] for s in steps}) == 2


def test_missing_identity_asks_a_user_facing_question_and_continues(tmp_path, monkeypatch):
    from harness.models import ChatMessage, Role
    q = 'Delete TCK-1004.'
    deps = {'ticket_id': 'TCK-1004'}
    permission = {'action': 'delete', 'ticket_id': 'TCK-1004'}
    followup = 'My user ID is admin.rlee.'
    combined = q+'\n'+followup
    owner, _, _ = setup(tmp_path, monkeypatch, q, [
        contract(q, ('check_dependencies', deps), ('check_permission', permission), ('delete_ticket', deps), domain='policy'),
        reply('check_dependencies', deps, 'deps1'),
        reply('check_permission', {**permission, 'user_id': 'invented.user'}, 'bad-identity'),
        final('Ask the user which identity to check.', 'needs_user_input'),
        contract(combined, ('check_dependencies', deps), ('check_permission', {**permission, 'user_id': 'admin.rlee'}),
                 ('delete_ticket', deps), domain='policy'),
        reply('check_dependencies', deps, 'deps2'),
        reply('check_permission', {**permission, 'user_id': 'admin.rlee'}, 'permission'),
        reply('delete_ticket', deps, 'delete'), final(),
    ])
    async def run():
        first = await owner.run(q, [])
        assert 'Which user ID' in first.content and 'Ask the user' not in first.content
        assert 'TCK-1004' in owner.environment.get_state()['tickets']
        return await owner.run(followup, [ChatMessage(role=Role.USER, content=q),
            ChatMessage(role=Role.ASSISTANT, content=first.content)])
    result = asyncio.run(run())
    assert 'was deleted' in result.content and 'TCK-1004' not in owner.environment.get_state()['tickets']


def test_unknown_write_is_not_blindly_reexecuted_or_reported_as_success(tmp_path, monkeypatch):
    q = 'TCK-1001 needs the note "Once".'
    args = {'ticket_id': 'TCK-1001', 'comment': 'Once'}
    owner, engine, _ = setup(tmp_path, monkeypatch, q, [contract(q, ('add_ticket_comment', args)),
        reply('add_ticket_comment', args, 'write'), reply('add_ticket_comment', args, 'retry'), final()])
    actual = owner.registry.execute_with_status
    async def lost_ack(name, arguments):
        await actual(name, arguments)
        return SimpleNamespace(result='{"error":"acknowledgement lost"}', ok=False)
    monkeypatch.setattr(owner.registry, 'execute_with_status', lost_ack)
    assert engine.answer()['status'] == 'insufficient'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']
    assert [c['backend_executed'] for c in owner.tool_calls] == [True, False]


def test_restart_recovers_committed_steps_without_a_second_write(tmp_path, monkeypatch):
    class SimulatedExit(BaseException):
        pass
    q = 'TCK-1001 needs the note "Once".'
    args = {'ticket_id': 'TCK-1001', 'comment': 'Once'}
    owner, engine, _ = setup(tmp_path, monkeypatch, q, [contract(q, ('add_ticket_comment', args)),
        reply('add_ticket_comment', args, 'write'), final()])
    def crash(*args):
        raise SimulatedExit()
    monkeypatch.setattr(engine, 'finish_task', crash)
    with pytest.raises(SimulatedExit):
        engine.answer()
    fresh_owner, restored, model = setup(tmp_path, monkeypatch, q, [])
    fresh_owner.environment, fresh_owner.registry = owner.environment, owner.registry
    result = restored.answer()
    assert result['status'] == 'answered' and 'Once' in result['answer']
    assert not model.seen and fresh_owner.model_calls == owner.model_calls
    assert fresh_owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']


def test_no_tool_evidence_never_turns_a_fluent_answer_into_success(tmp_path, monkeypatch):
    q = 'What is the status of TCK-1001?'
    _, engine, _ = setup(tmp_path, monkeypatch, q, [contract(q, ('get_ticket', {'ticket_id': 'TCK-1001'})),
        final('It is Open, everything is done.')])
    result = engine.answer()
    assert result['status'] == 'insufficient' and 'remaining requested steps' in result['answer']


def test_real_business_rejection_remains_visible_and_is_not_a_write_receipt(tmp_path, monkeypatch):
    q = 'Close TCK-1003.'
    args = {'ticket_id': 'TCK-1003'}
    owner, engine, _ = setup(tmp_path, monkeypatch, q, [
        contract(q, ('check_dependencies', args), ('close_ticket', args)),
        reply('check_dependencies', args, 'deps'), reply('close_ticket', args, 'close'), final('Closed successfully.'),
    ])
    result = engine.answer()
    assert result['status'] == 'answered' and 'already Closed' in result['answer']
    assert 'Closed successfully' not in result['answer']
    assert not next(iter(engine.state['completed_tasks'].values()))['result']['receipts']


def test_intake_cannot_drop_negation_to_authorize_a_write(tmp_path, monkeypatch):
    q = "Don't close TCK-1001."
    owner, engine, _ = setup(tmp_path, monkeypatch, q, [
        contract('close TCK-1001.', ('close_ticket', {'ticket_id': 'TCK-1001'})),
        contract(q, disposition='refuse', explanation='No ticket has been changed.'),
    ])
    assert engine.answer()['status'] == 'answered'
    assert not owner.tool_calls and owner.environment.get_state()['tickets']['TCK-1001']['status'] == 'Open'


def test_source_id_transport_does_not_accept_changed_note_contents(tmp_path, monkeypatch):
    q = 'TCK-1001 needs the note "Ready".'
    wrong = {'ticket_id': 'TCK-1001', 'comment': '``Ready``'}
    right = {'ticket_id': 'TCK-1001', 'comment': 'Ready'}
    owner, engine, _ = setup(tmp_path, monkeypatch, q, [
        contract(q, ('add_ticket_comment', wrong)), contract(q, ('add_ticket_comment', right)),
        reply('add_ticket_comment', right, 'note'), final(),
    ])
    assert engine.answer()['status'] == 'answered'
    assert owner.model_calls == 4
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Ready']
