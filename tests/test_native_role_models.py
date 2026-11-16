"""Selective RL keeps root and child models separate in real DeepAgents graphs."""
import pytest

from kb.harness.native import NativeHarness, NativeBudgetExceeded
from test_deep_harness import ScriptModel, Port, call
from test_native_harness import answer


@pytest.mark.parametrize('forked', [False, True])
def test_specialist_model_is_separate_and_keeps_shared_accounting(tmp_path, forked):
    supervisor = ScriptModel(replies=[
        call('task', {'subagent_type': 'service', 'description': 'Read the status.'}, 'delegate'),
        answer('The status is Open.')])
    specialist = ScriptModel(replies=[call('lookup', {}, 'lookup'), answer('Open.')])
    engine = NativeHarness(model=supervisor, role_models={'*': specialist},
        port=Port(), workspace=tmp_path, namespace='one', fork_subagents=forked)
    try:
        assert engine.invoke('Ask the specialist to read the status.', 'request') == 'The status is Open.'
        assert len(supervisor.seen) == len(specialist.seen) == 2
        assert engine.calls == 4 and engine.prompt_tokens == 44
        assert len(engine.journal.records()) == 1
        assert 'task' in supervisor.bindings[0]
        assert 'lookup' in specialist.bindings[0]
    finally:
        engine.close()


def test_exact_specialist_override_takes_precedence_over_default(tmp_path):
    supervisor = ScriptModel(replies=[
        call('task', {'subagent_type': 'service', 'description': 'Read status.'}, 'delegate'), answer('Open.')])
    fallback = ScriptModel(replies=[])
    service = ScriptModel(replies=[call('lookup', {}, 'lookup'), answer('Open.')])
    engine = NativeHarness(model=fallback,
        role_models={'supervisor': supervisor, '*': fallback, 'service': service},
        port=Port(), workspace=tmp_path, namespace='one', fork_subagents=True)
    try:
        assert engine.invoke('Read status.', 'request') == 'Open.'
        assert len(supervisor.seen) == len(service.seen) == 2
        assert fallback.seen == []
    finally:
        engine.close()


def test_separate_models_share_one_budget(tmp_path):
    supervisor = ScriptModel(replies=[
        call('task', {'subagent_type': 'service', 'description': 'Read status.'}, 'delegate'), answer('Open.')])
    specialist = ScriptModel(replies=[call('lookup', {}, 'lookup'), answer('Open.')])
    engine = NativeHarness(model=supervisor, role_models={'*': specialist},
        port=Port(), workspace=tmp_path, namespace='one', fork_subagents=True, max_calls=2)
    try:
        with pytest.raises(NativeBudgetExceeded):
            engine.invoke('Read status.', 'request')
        assert engine.calls == 2
        assert len(supervisor.seen) == len(specialist.seen) == 1
        assert len(engine.journal.records()) == 1
    finally:
        engine.close()


def test_unknown_role_fails_before_creating_journal(tmp_path):
    with pytest.raises(ValueError,match='Unknown model roles'):
        NativeHarness(model=ScriptModel(replies=[]),role_models={'misspelled':ScriptModel(replies=[])},
            port=Port(),workspace=tmp_path,namespace='one')
    assert not (tmp_path/'effects.sqlite3').exists()
