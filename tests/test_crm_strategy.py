import asyncio
import json

import pytest

from kb.benchmark.crm_supervisor_state import pending_context
from test_crm_benchmark import reply
from test_crm_supervisor import delegate, setup, text, turn


@pytest.mark.parametrize('question', ['Wait, which ticket are you closing?', 'The VPN one.', '哪个工单？'])
def test_clarification_preserves_original_operation(question):
    pending = 'Close the ticket about VPN.'
    assert pending_context(pending, question, 'Which ticket ID?', True) == (pending, False)


@pytest.mark.parametrize('question', ['Cancel that request.', 'Instead, delete TCK-1004.'])
def test_new_goal_or_cancellation_is_not_forced_into_old_operation(question):
    assert pending_context('Close TCK-1001.', question, '', True)[0] is None


def test_partial_work_recovers_through_supervisor_without_repeating_comment(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [
        delegate('service', ident='first'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Checked'}, 'comment'),
        text('Added the comment.'), text('Done.'),
        delegate('service', 'Continue the remaining status update.', ident='recovery'),
        reply('update_ticket_status', {'ticket_id': 'TCK-1001', 'status': 'Resolved'}, 'status'),
        text('Updated the status.'), text('Added Checked and updated the status to Resolved.')])
    owner.settings.supervisor_strategy = 'experience'
    result = asyncio.run(owner.run('Add "Checked" to TCK-1001, then update its status to Resolved.', []))
    ticket = owner.environment.get_state()['tickets']['TCK-1001']
    assert ticket['comments'] == ['Checked'] and ticket['status'] == 'Resolved'
    assert owner.execution_outcomes[-1]['status'] == 'answered'
    assert owner.model_calls == 8
    calls = [c['name'] for c in owner.tool_calls if c['backend_executed']]
    assert calls == ['add_ticket_comment', 'update_ticket_status']
    events = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert sum(e['kind'] == 'strategy_recovery' for e in events) == 1
    assert any(e['kind'] == 'strategy_experience' for e in events)
    assert 'Resolved' in result.content


def test_recovery_is_bounded_to_one_attempt(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'), text('Done.'), text('Done.'), text('Done.')])
    owner.settings.supervisor_strategy = 'recovery'
    asyncio.run(owner.run('Add "Checked" to TCK-1001.', []))
    assert owner.execution_outcomes[-1]['status'] == 'insufficient'
    assert owner.model_calls == 4 and not owner.tool_calls
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == []


def test_recovery_never_repeats_unknown_write(tmp_path, monkeypatch):
    from types import SimpleNamespace
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'note'),
        text('The result is uncertain.'), text('The result is uncertain.')])
    owner.settings.supervisor_strategy = 'experience'
    execute = owner.registry.execute_with_status
    async def lost_ack(name, arguments):
        await execute(name, arguments)
        return SimpleNamespace(ok=False, result='{"error":"acknowledgement lost"}')
    monkeypatch.setattr(owner.registry, 'execute_with_status', lost_ack)
    asyncio.run(owner.run('Add "Once" to TCK-1001.', []))
    assert owner.model_calls == 4
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']
    assert owner.execution_outcomes[-1]['status'] == 'insufficient'


def test_target_clarification_then_bare_id_keeps_task_across_three_turns(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [
        delegate('service', ident='first'), text('Which ticket ID?'), text('Which ticket ID?'),
        delegate('service', ident='second'), text('Which ticket ID do you mean?'), text('Which ticket ID do you mean?'),
        delegate('service', ident='third'),
        reply('assign_ticket', {'ticket_id': 'TCK-1004', 'assignee': 'agent.jsmith'}, 'assign'),
        text('Assigned the ticket.'), text('Assigned the ticket to agent.jsmith.')])
    owner.settings.supervisor_strategy = 'experience'
    async def run():
        history = []
        await turn(owner, 'Please assign the ticket to agent.jsmith.', history)
        await turn(owner, 'Wait, which ticket are you assigning?', history)
        assert owner.pending_itsm_request
        await turn(owner, 'TCK-1004', history)
    asyncio.run(run())
    assert owner.environment.get_state()['tickets']['TCK-1004']['assigned_to'] == 'agent.jsmith'
    assert len({c['task_id'] for c in owner.tool_calls}) == 1


def test_successful_simple_lookup_has_no_extra_model_calls(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'lookup'), text('Open.'), text('Open.')])
    owner.settings.supervisor_strategy = 'experience'
    asyncio.run(owner.run('What is the status of TCK-1001?', []))
    assert owner.model_calls == 4


def test_issue_description_is_resolved_from_actual_listing_after_clarification(tmp_path, monkeypatch):
    owner, model = setup(tmp_path, monkeypatch, [delegate('service', ident='first'),
        text('Please provide the ticket ID.'), text('Please provide the ticket ID.'),
        delegate('service', ident='second'), reply('list_tickets', {}, 'listing'),
        reply('check_dependencies', {'ticket_id': 'TCK-1001'}, 'dependencies'),
        reply('close_ticket', {'ticket_id': 'TCK-1001'}, 'close'),
        text('Closed TCK-1001.'), text('Closed the VPN ticket, TCK-1001.')])
    owner.settings.supervisor_strategy = 'experience'
    async def run():
        history = []
        first = await turn(owner, 'Can you close out my ticket?', history)
        assert 'Which ticket' in first.content and 'describe the issue' in first.content
        await turn(owner, 'the VPN one', history)
    asyncio.run(run())
    assert owner.environment.get_state()['tickets']['TCK-1001']['status'] == 'Closed'
    assert [c['name'] for c in owner.tool_calls if c['backend_executed']] == ['list_tickets', 'check_dependencies', 'close_ticket']
    assert any('target_description' in str(messages) and 'the VPN one' in str(messages) for messages in model.seen)


def test_repeated_read_produces_one_hint_but_does_not_block_needed_tools(tmp_path, monkeypatch):
    owner, model = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'first'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'second'), text('Open.'), text('Open.')])
    owner.settings.supervisor_strategy = 'recovery'
    asyncio.run(owner.run('What is the status of TCK-1001? Please check it twice.', []))
    assert len(owner.tool_calls) == 2
    assert any('observed_repetition' in str(messages) for messages in model.seen)
