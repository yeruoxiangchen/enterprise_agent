"""Scoped native tasks preserve objectives and cannot expand write capability."""
import json

import pytest

from kb.harness.native import NativeHarness
from test_deep_harness import Port, ScriptModel, call
from test_native_harness import answer


@pytest.mark.parametrize('forked', [False, True])
def test_read_only_subtask_hides_and_blocks_write_even_when_user_has_a_larger_goal(tmp_path, forked):
    objective = 'Check status only; report to the supervisor.'
    supervisor = ScriptModel(replies=[call('task', {'subagent_type': 'service',
        'description': objective, 'allowed_write_tools': []}, 'inspect'), answer('Checked.')])
    child = ScriptModel(replies=[call('change', {'value': 'Closed'}, 'unauthorized-write'),
                                answer('The delegated task permits observation only.')])
    port = Port()
    engine = NativeHarness(model=supervisor, role_models={'*': child}, port=port,
        workspace=tmp_path, namespace='scope', fork_subagents=forked, bind_user_request=True)
    try:
        assert engine.invoke('Check the ticket, then close it if allowed.', 'request') == 'Checked.'
        assert port.writes == 0 and port.value == 'Open'
        assert 'change' not in child.bindings[0]
        assert objective in str(child.seen[0])
        assert 'Check the ticket, then close it if allowed.' in str(child.seen[0])
        assert any('delegation_scope_violation' in str(m) for m in child.seen[1])
        assert engine.attempts[0]['business_outcome'] == 'rejected'
        assert not engine.attempts[0]['backend_executed']
        saved_call = next(c for m in supervisor.seen[1] for c in getattr(m, 'tool_calls', []))
        assert saved_call['args'] == {'subagent_type': 'service', 'description': objective,
                                      'allowed_write_tools': []}
    finally:
        engine.close()


def test_new_task_changes_scope_without_leaking_the_previous_read_only_scope(tmp_path):
    supervisor = ScriptModel(replies=[
        call('task', {'subagent_type': 'service', 'description': 'Read status.',
                      'allowed_write_tools': []}, 'inspect'),
        call('task', {'subagent_type': 'service', 'description': 'Close the ticket.',
                      'allowed_write_tools': ['change']}, 'execute'), answer('Closed.')])
    child = ScriptModel(replies=[call('lookup', {}, 'read'), answer('Open.'),
                                call('change', {'value': 'Closed'}, 'write'), answer('Closed.')])
    port = Port()
    engine = NativeHarness(model=supervisor, role_models={'*': child}, port=port,
        workspace=tmp_path, namespace='scope', fork_subagents=True, bind_user_request=True)
    try:
        assert engine.invoke('Check and close the ticket.', 'request') == 'Closed.'
        assert port.writes == 1 and port.value == 'Closed'
        assert 'change' not in child.bindings[0] and 'change' in child.bindings[2]
        contracts = [json.loads(row['contract']) for row in engine.journal.db.execute(
            'SELECT contract FROM native_delegations ORDER BY rowid')]
        assert [c['allowed_write_tools'] for c in contracts] == [[], ['change']]
    finally:
        engine.close()


def test_delegation_capability_is_persisted_across_interruption_and_resume(tmp_path):
    def disconnect(messages):
        raise ConnectionError('controlled disconnect after delegation')
    port = Port()
    root = ScriptModel(replies=[call('task', {'subagent_type': 'service',
        'description': 'Check status only.', 'allowed_write_tools': []}, 'inspect')])
    engine = NativeHarness(model=root, role_models={'*': ScriptModel(replies=[disconnect])},
        port=port, workspace=tmp_path, namespace='scope', fork_subagents=True, bind_user_request=True)
    try:
        with pytest.raises(ConnectionError):
            engine.invoke('Check status only.', 'request')
    finally:
        engine.close()
    child = ScriptModel(replies=[call('change', {'value': 'Closed'}, 'bad-write'), answer('Unchanged.')])
    engine = NativeHarness(model=ScriptModel(replies=[answer('Unchanged.')]), role_models={'*': child},
        port=port, workspace=tmp_path, namespace='scope', fork_subagents=True, bind_user_request=True)
    try:
        assert engine.invoke('Check status only.', 'request') == 'Unchanged.'
        assert port.writes == 0 and 'change' not in child.bindings[0]
    finally:
        engine.close()
