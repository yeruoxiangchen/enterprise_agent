import asyncio
import json

import pytest

from kb.benchmark.crm_strategy import requested_reads
from kb.experience import ExperienceMemory
from test_crm_benchmark import reply
from test_crm_supervisor import delegate, setup, text, turn


def test_readback_after_assignment_is_completed_without_repeating_write(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [
        delegate('service', ident='assign-delegation'),
        reply('assign_ticket', {'ticket_id': 'TCK-1004', 'assignee': 'admin.rlee'}, 'assign'),
        text('Assigned.'), text('Done.'),
        delegate('service', ident='verify-delegation'),
        reply('get_ticket', {'ticket_id': 'TCK-1004'}, 'verify'),
        text('Confirmed.'), text('Confirmed.')])
    owner.settings.supervisor_strategy = 'experience'
    result = asyncio.run(owner.run('Assign TCK-1004 to admin.rlee, then look it up to confirm the new owner.', []))
    calls = [call for call in owner.tool_calls if call['backend_executed']]
    assert [call['name'] for call in calls] == ['assign_ticket', 'get_ticket']
    assert owner.execution_outcomes[-1]['status'] == 'answered'
    assert 'admin.rlee' in result.content


@pytest.mark.parametrize('second_id', ['second-read', 'first-read'])
def test_owner_followup_uses_a_real_fresh_lookup(tmp_path, monkeypatch, second_id):
    owner, _ = setup(tmp_path, monkeypatch, [
        delegate('service', ident='first'), reply('get_ticket', {'ticket_id': 'TCK-1004'}, 'first-read'),
        text('Unassigned.'), text('Unassigned.'),
        delegate('service', ident='second'), reply('get_ticket', {'ticket_id': 'TCK-1004'}, second_id),
        text('Unassigned.'), text('Unassigned.')])
    owner.settings.supervisor_strategy = 'experience'
    async def run():
        history = []
        await turn(owner, 'Who is the current owner of TCK-1004?', history)
        return await turn(owner, 'Please confirm the current owner of TCK-1004.', history)
    result = asyncio.run(run())
    assert len([call for call in owner.tool_calls if call['backend_executed']]) == 2
    assert 'no current owner' in result.content


def test_permission_claim_requires_actual_read_and_uses_only_supplied_identity(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [text('The account is allowed.'),
        delegate('policy'), reply('check_permission', {}, 'permission'),
        text('Allowed.'), text('Allowed.')])
    owner.settings.supervisor_strategy = 'experience'
    result = asyncio.run(owner.run('Can you confirm whether agent.jsmith is permitted to escalate tickets?', []))
    call = next(call for call in owner.tool_calls if call['backend_executed'])
    assert call['name'] == 'check_permission'
    assert call['arguments'] == {'user_id': 'agent.jsmith', 'action': 'escalate'}
    assert call['requested_arguments'] == {}
    assert 'agent.jsmith is permitted' in result.content
    assert not any(call['name'] == 'escalate_ticket' for call in owner.tool_calls)


def test_explicit_permission_subject_takes_precedence_over_session_identity(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('policy'), reply('check_permission', {}, 'permission'),
        text('Allowed.'), text('Allowed.')])
    owner.settings.supervisor_strategy = 'experience'
    state = owner.environment.get_state(); state['session']['user_id'] = 'admin.rlee'
    owner.environment.data.clear(); owner.environment.data.update(state)
    asyncio.run(owner.run('Is agent.jsmith permitted to escalate tickets?', []))
    assert next(call for call in owner.tool_calls if call['backend_executed'])['arguments']['user_id'] == 'agent.jsmith'


def test_reassignment_alias_is_canonicalized_with_original_arguments_audited(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'read'),
        reply('reassign_ticket', {'ticket_id': 'TCK-1001', 'new_owner': 'admin.rlee'}, 'reassign'),
        text('Reassigned.'), text('Reassigned.')])
    owner.settings.supervisor_strategy = 'experience'
    asyncio.run(owner.run('Reassign TCK-1001 from agent.jsmith to admin.rlee.', []))
    call = next(call for call in owner.tool_calls if call['name'] == 'reassign_ticket')
    assert call['backend_executed'] and call['arguments']['assignee'] == 'admin.rlee'
    assert call['requested_arguments']['new_owner'] == 'admin.rlee'


def test_missing_model_parameter_is_feedback_not_an_extra_user_clarification(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('assign_ticket', {'ticket_id': 'TCK-1004'}, 'missing-assignee'),
        reply('assign_ticket', {'ticket_id': 'TCK-1004', 'assignee': 'admin.rlee'}, 'corrected'),
        text('Assigned.'), text('Assigned.')])
    owner.settings.supervisor_strategy = 'experience'
    asyncio.run(owner.run('Assign TCK-1004 to admin.rlee.', []))
    first, second = owner.tool_calls
    assert not first['backend_executed']
    assert json.loads(first['result'])['error'] == 'invalid_tool_arguments'
    assert second['backend_executed']
    assert owner.environment.get_state()['tickets']['TCK-1004']['assigned_to'] == 'admin.rlee'


def test_cancellation_does_not_call_a_model_or_reopen_the_goal(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'), text('Which ticket?'), text('Which ticket?')])
    owner.settings.supervisor_strategy = 'experience'
    async def run():
        history = []
        await turn(owner, 'Can you close my ticket?', history)
        before = owner.model_calls
        answer = await turn(owner, "Never mind, I'll come back with the number.", history)
        assert owner.model_calls == before and owner.pending_itsm_request is None
        return answer
    assert 'cancelled' in asyncio.run(run()).content


def test_phase_filter_avoids_mutation_guidance_for_read_only_queries():
    memory = ExperienceMemory()
    found = memory.retrieve('permission identity delete missing', 'policy', phase='read')
    assert all(entry['id'] != 'identity-is-required' for entry in found)
    assert all(entry['id'] != 'finish-remaining' for entry in found)
    assert memory.retrieve('confirm verify owner lookup', 'service', phase='verify')


def test_lookup_before_write_keeps_its_order_requirement(tmp_path, monkeypatch):
    from kb.benchmark.crm_supervisor import ExternalBusinessQA
    owner, _ = setup(tmp_path, monkeypatch, [])
    owner.settings.supervisor_strategy = 'experience'
    engine = ExternalBusinessQA(owner, 'Look up TCK-1004, then assign TCK-1004 to admin.rlee.', [], 0)
    reads = requested_reads(engine)
    assert reads == [{'name': 'get_ticket', 'arguments': {'ticket_id': 'TCK-1004'}, 'after_write': False}]


def test_completed_prewrite_lookup_does_not_trigger_an_extra_read(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('get_ticket', {'ticket_id': 'TCK-1004'}, 'before-write'),
        reply('assign_ticket', {'ticket_id': 'TCK-1004', 'assignee': 'admin.rlee'}, 'assign'),
        text('Assigned.'), text('Assigned.')])
    owner.settings.supervisor_strategy = 'experience'
    asyncio.run(owner.run('Look up TCK-1004, then assign TCK-1004 to admin.rlee.', []))
    assert [call['name'] for call in owner.tool_calls if call['backend_executed']] == [
        'get_ticket', 'assign_ticket']
    assert owner.execution_outcomes[-1]['status'] == 'answered'
    assert owner.model_calls == 5


def test_distinct_read_target_does_not_spread_to_other_ticket_ids(tmp_path, monkeypatch):
    from kb.benchmark.crm_supervisor import ExternalBusinessQA
    owner, _ = setup(tmp_path, monkeypatch, [])
    owner.settings.supervisor_strategy = 'experience'
    engine = ExternalBusinessQA(owner, 'Add note "Ready" to TCK-1001, then look up TCK-1004.', [], 0)
    assert requested_reads(engine) == [
        {'name': 'get_ticket', 'arguments': {'ticket_id': 'TCK-1004'}, 'after_write': True}]


def test_explicit_lookup_binds_native_delegation_and_tool_then_releases_choice(tmp_path, monkeypatch):
    owner, model = setup(tmp_path, monkeypatch, [delegate('policy'),
        reply('check_permission', {'user_id': 'agent.jsmith', 'action': 'escalate'}, 'permission'),
        text('Allowed.'), text('Allowed.')])
    bindings = []
    def bind(model, tools, **kwargs):
        bindings.append(kwargs.get('tool_choice'))
        return model
    monkeypatch.setattr(type(model), 'bind_tools', bind)
    owner.settings.supervisor_strategy = 'experience'
    asyncio.run(owner.run('Is agent.jsmith permitted to escalate tickets?', []))
    choices = [choice['function']['name'] for choice in bindings if isinstance(choice, dict)]
    assert choices == [delegate('policy').tool_calls[0]['name'], 'check_permission']
    assert bindings[-2:] == [None, None]
    assert owner.execution_outcomes[-1]['status'] == 'answered'


def test_missing_user_identity_keeps_clarification_available(tmp_path, monkeypatch):
    owner, model = setup(tmp_path, monkeypatch, [delegate('policy'),
        text('Which user ID should I check?'), text('Which user ID should I check?')])
    bindings = []
    def bind(model, tools, **kwargs):
        bindings.append(kwargs.get('tool_choice'))
        return model
    monkeypatch.setattr(type(model), 'bind_tools', bind)
    owner.settings.supervisor_strategy = 'experience'
    result = asyncio.run(owner.run('Can you check whether my account is permitted to escalate tickets?', []))
    assert bindings and all(choice is None for choice in bindings)
    assert owner.execution_outcomes[-1]['status'] == 'clarify'
    assert 'user ID' in result.content
    assert not owner.tool_calls
