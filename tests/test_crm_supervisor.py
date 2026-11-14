"""Supervisor integrations: real framework graphs and canonical business state."""
import asyncio
import json

import pytest
from harness.models import ChatMessage, Role
from langchain_core.messages import AIMessage

from kb.benchmark.crm_supervisor import DELEGATIONS
from kb.context import ContextBudget
from test_crm_benchmark import RecordedModel, adapter, reply


def text(content):
    return AIMessage(content=content, usage_metadata={
        'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15})


def delegate(domain, request='Handle the current request.', ident='delegate'):
    name = next(name for name, selected in DELEGATIONS.items() if selected == domain)
    return reply(name, {'request': request}, ident)


def setup(tmp_path, monkeypatch, replies, arm='multi_v6'):
    model = RecordedModel(replies=list(replies))
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    owner = adapter(tmp_path, arm)
    owner.execution_adapter = 'v4'
    return owner, model


async def turn(owner, question, history):
    response = await owner.run(question, history)
    history.extend([ChatMessage(role=Role.USER, content=question),
                    ChatMessage(role=Role.ASSISTANT, content=response.content)])
    return response


def test_native_supervisor_delegates_and_accounts_for_actual_backend_only(tmp_path, monkeypatch):
    owner, model = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'lookup'),
        text('TCK-1001 is Open.'), text('TCK-1001 is Open.')])
    response = asyncio.run(owner.run('What is the status of TCK-1001?', []))
    assert response.content == 'TCK-1001 is Open.'
    assert owner.model_calls == 4 and owner.prompt_tokens == 44 and owner.completion_tokens == 16
    assert owner.usage_missing_calls == 0
    assert [c['name'] for c in owner.tool_calls] == ['get_ticket']
    assert owner.tool_calls[0]['backend_executed']
    assert all('expected_actions' not in str(messages) for messages in model.seen)
    events = [json.loads(s) for s in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert len([e for e in events if e['kind'] == 'external_delegation']) == 1


@pytest.mark.parametrize('identity_reply', ['My user ID is admin.rlee.',
    'I am the admin.rlee and have the necessary permissions to delete the ticket.'])
def test_identity_followup_continues_without_recompiling_completed_steps(tmp_path, monkeypatch, identity_reply):
    target = {'ticket_id': 'TCK-1004'}
    owner, _ = setup(tmp_path, monkeypatch, [
        delegate('policy', ident='d0'), reply('check_dependencies', target, 'deps'),
        reply('check_permission', {**target, 'user_id': 'made.up', 'action': 'delete'}, 'invented'),
        text('Which user ID should I check?'), text('Which user ID should I check?'),
        delegate('policy', ident='d1'),
        reply('check_permission', {**target, 'user_id': 'admin.rlee', 'action': 'delete'}, 'permission'),
        reply('delete_ticket', target, 'delete'), text('TCK-1004 was deleted.'), text('TCK-1004 was deleted.'),
    ])
    async def run():
        history = []
        await turn(owner, 'Delete TCK-1004.', history)
        assert 'TCK-1004' in owner.environment.get_state()['tickets']
        return await turn(owner, identity_reply, history)
    assert 'deleted' in asyncio.run(run()).content
    assert 'TCK-1004' not in owner.environment.get_state()['tickets']
    assert len({c['task_id'] for c in owner.tool_calls}) == 1
    assert [c['arguments']['user_id'] for c in owner.tool_calls
            if c['name'] == 'check_permission' and c['backend_executed']] == ['admin.rlee']
    assert owner.tool_calls[1]['backend_executed'] is False


def test_ordered_batch_commits_in_declared_order(tmp_path, monkeypatch):
    batch = AIMessage(content='', tool_calls=[
        {'name': 'add_ticket_comment', 'args': {'ticket_id': 'TCK-1001', 'comment': 'Ready'}, 'id': 'note'},
        {'name': 'update_ticket_status', 'args': {'ticket_id': 'TCK-1001', 'status': 'Resolved'}, 'id': 'status'},
    ], usage_metadata={'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15})
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'), batch,
        text('Added Ready and set TCK-1001 to Resolved.'), text('Added Ready and set TCK-1001 to Resolved.')])
    asyncio.run(owner.run('Add "Ready" to TCK-1001, then set its status to Resolved.', []))
    assert [c['name'] for c in owner.tool_calls if c['backend_executed']] == ['add_ticket_comment', 'update_ticket_status']
    ticket = owner.environment.get_state()['tickets']['TCK-1001']
    assert ticket['comments'] == ['Ready'] and ticket['status'] == 'Resolved'


def test_quoted_comment_is_guarded_against_specialist_rewrite(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Rewritten'}, 'bad'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Exact note'}, 'good'),
        text('Added Exact note.'), text('Added Exact note.')])
    asyncio.run(owner.run('TCK-1001 needs the note "Exact note".', []))
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Exact note']
    assert [c['backend_executed'] for c in owner.tool_calls] == [False, True]


def test_retry_reuses_receipt_but_explicit_another_note_is_new_work(tmp_path, monkeypatch):
    args = {'ticket_id': 'TCK-1001', 'comment': 'Once'}
    replies = []
    for i in range(3):
        replies += [delegate('service', ident=f'd{i}'), reply('add_ticket_comment', args, f'n{i}'),
                    text('Added Once.'), text('Added Once.')]
    owner, _ = setup(tmp_path, monkeypatch, replies)
    async def run():
        history = []
        for q in ['Add note "Once" to TCK-1001.', 'Add note "Once" to TCK-1001.',
                  'Add another note "Once" to TCK-1001.']:
            await turn(owner, q, history)
    asyncio.run(run())
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once', 'Once']
    assert [c['backend_executed'] for c in owner.tool_calls] == [True, False, True]


def test_cancellation_drops_pending_deletion(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('policy'),
        text('Which user ID should I check?'), text('Which user ID should I check?'),
        text('Cancelled. No ticket was deleted.')])
    async def run():
        history = []
        await turn(owner, 'Delete TCK-1004.', history)
        assert owner.pending_itsm_request
        await turn(owner, 'Cancel that request.', history)
    asyncio.run(run())
    assert owner.pending_itsm_request is None
    assert 'TCK-1004' in owner.environment.get_state()['tickets']
    assert not owner.tool_calls


def test_supervisor_handles_two_domains_in_user_order(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service', 'Add the requested comment.', 'd0'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Ready'}, 'note'),
        text('Added Ready to TCK-1001.'), delegate('technical', 'Check service desk hours.', 'd1'),
        reply('get_business_hours', {}, 'hours'), text('The service desk is open from 09:00 to 18:00 UTC.'),
        text('Added Ready to TCK-1001. The service desk is open from 09:00 to 18:00 UTC.')])
    asyncio.run(owner.run('Add note "Ready" to TCK-1001, then tell me the business hours.', []))
    assert [c['name'] for c in owner.tool_calls if c['backend_executed']] == ['add_ticket_comment', 'get_business_hours']
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Ready']


def test_restart_during_delegation_reuses_committed_receipt(tmp_path, monkeypatch):
    class Interrupted(BaseException):
        pass
    args = {'ticket_id': 'TCK-1001', 'comment': 'Once'}
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'), reply('add_ticket_comment', args, 'first')])
    original = owner.emit
    def crash(kind, data):
        original(kind, data)
        if kind == 'external_tool' and data.get('business_outcome') == 'committed':
            raise Interrupted()
    monkeypatch.setattr(owner, 'emit', crash)
    with pytest.raises(Interrupted):
        asyncio.run(owner.run('Add note "Once" to TCK-1001.', []))
    resumed, _ = setup(tmp_path, monkeypatch, [reply('add_ticket_comment', args, 'retry'),
        text('Added Once.'), text('Added Once.')])
    resumed.environment, resumed.registry = owner.environment, owner.registry
    result = asyncio.run(resumed.run('Add note "Once" to TCK-1001.', []))
    assert 'Once' in result.content
    assert resumed.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']
    assert sum(c['backend_executed'] for c in resumed.tool_calls) == 1
    assert resumed.tool_calls[0]['business_outcome'] == 'committed'
    replays = [c for c in resumed.tool_calls if c.get('business_outcome') == 'reused']
    assert replays and all(not c['backend_executed'] for c in replays)


@pytest.mark.parametrize('arm', ['single_v6', 'multi_v6'])
def test_unknown_write_is_never_repeated(tmp_path, monkeypatch, arm):
    calls = [reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'one'),
             reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'retry'),
             text('The result is uncertain; it needs reconciliation.')]
    if arm == 'multi_v6':
        calls = [delegate('service'), *calls, text('The result needs reconciliation.')]
    owner, _ = setup(tmp_path, monkeypatch, calls, arm)
    actual = owner.registry.execute_with_status
    async def lost_ack(name, arguments):
        from types import SimpleNamespace
        await actual(name, arguments)
        return SimpleNamespace(result='{"error":"acknowledgement lost"}', ok=False)
    monkeypatch.setattr(owner.registry, 'execute_with_status', lost_ack)
    asyncio.run(owner.run('Add note "Once" to TCK-1001.', []))
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']
    assert [c['backend_executed'] for c in owner.tool_calls] == [True, False]
    assert owner.execution_outcomes[-1]['status'] == 'insufficient'


def test_clarification_without_question_mark_keeps_pending_operation(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service', ident='first'),
        text('Please provide the assignee username.'), text('Please provide the assignee username.'),
        delegate('service', ident='second'),
        reply('assign_ticket', {'ticket_id': 'TCK-1004', 'assignee': 'agent.jsmith'}, 'assign'),
        text('Assigned TCK-1004 to agent.jsmith.'), text('Assigned TCK-1004 to agent.jsmith.')])
    async def run():
        history = []
        await turn(owner, 'Please assign TCK-1004.', history)
        assert owner.pending_itsm_request == 'Please assign TCK-1004.'
        await turn(owner, 'agent.jsmith', history)
    asyncio.run(run())
    assert owner.environment.get_state()['tickets']['TCK-1004']['assigned_to'] == 'agent.jsmith'
    assert owner.execution_outcomes[-1]['status'] == 'answered'


def test_unexecuted_write_cannot_be_presented_as_completed(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        text('Added Once.'), text('Added Once.')])
    response = asyncio.run(owner.run('Add note "Once" to TCK-1001.', []))
    assert owner.execution_outcomes[-1]['status'] == 'insufficient'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == []
    assert response.content != 'Added Once.'


def test_same_action_on_two_targets_remains_pending_after_only_one_write(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Ready'}, 'first'),
        text('Added the requested comments.'), text('Added the requested comments.')])
    response = asyncio.run(owner.run('Add note "Ready" to TCK-1001 and add note "Ready" to TCK-1004.', []))
    assert owner.execution_outcomes[-1]['status'] == 'insufficient'
    assert owner.pending_itsm_request
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Ready']
    assert owner.environment.get_state()['tickets']['TCK-1004']['comments'] == []
    assert response.content != 'Added the requested comments.'


def test_paraphrased_retry_of_unknown_write_is_blocked_across_operations(tmp_path, monkeypatch):
    args = {'ticket_id': 'TCK-1001', 'comment': 'Once'}
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service', ident='first'),
        reply('add_ticket_comment', args, 'one'), text('The result is uncertain.'), text('The result is uncertain.'),
        delegate('service', ident='second'), reply('add_ticket_comment', args, 'two'),
        text('The result needs reconciliation.'), text('The result needs reconciliation.')])
    actual = owner.registry.execute_with_status
    async def lost_ack(name, arguments):
        from types import SimpleNamespace
        await actual(name, arguments)
        return SimpleNamespace(result='{"error":"acknowledgement lost"}', ok=False)
    monkeypatch.setattr(owner.registry, 'execute_with_status', lost_ack)
    async def run():
        history = []
        await turn(owner, 'Add note "Once" to TCK-1001.', history)
        await turn(owner, 'Please add note "Once" to TCK-1001.', history)
    asyncio.run(run())
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']
    assert sum(c.get('backend_executed', False) for c in owner.tool_calls) == 1
    assert all(o['status'] == 'insufficient' for o in owner.execution_outcomes)
