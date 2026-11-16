"""Exercise the installed DeepAgents graphs, shared effect gateway and budgets."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from kb.harness.native import NativeHarness, NativeBudgetExceeded
from test_deep_harness import ScriptModel, Port, call


def answer(text):
    return AIMessage(content=text, usage_metadata={'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15})


def test_native_read_has_no_completion_classifier_and_cached_request_survives_restart(tmp_path):
    port = Port()
    engine = NativeHarness(model=ScriptModel(replies=[call('lookup', {}, 'read'), answer('Open.')]),
                           port=port, workspace=tmp_path, namespace='one')
    try:
        assert engine.invoke('Read the status.', 'read-request') == 'Open.'
        assert engine.calls == 2 and engine.prompt_tokens == 22
        assert len(engine.journal.records()) == 1
    finally:
        engine.close()
    resumed = NativeHarness(model=ScriptModel(replies=[]), port=port, workspace=tmp_path, namespace='one')
    try:
        assert resumed.invoke('Read the status.', 'read-request') == 'Open.'
        assert resumed.calls == 0
        with pytest.raises(ValueError, match='identity changed'):
            resumed.invoke('Change it.', 'read-request')
    finally:
        resumed.close()


def test_native_delegation_uses_upstream_message_handoff_with_four_model_calls(tmp_path):
    model = ScriptModel(replies=[call('task', {'subagent_type': 'service', 'description': 'Read the status.'}, 'delegate'),
                                call('lookup', {}, 'lookup'), answer('Open.'), answer('The status is Open.')])
    engine = NativeHarness(model=model, port=Port(), workspace=tmp_path, namespace='one')
    try:
        assert engine.invoke('Have the service specialist read the status.', 'request') == 'The status is Open.'
        assert engine.calls == 4 and len(engine.journal.records()) == 1
        assert 'CompletionMetadata' not in str(model.bindings)
        assert 'RequestBoundary' not in str(model.bindings)
        assert 'PatchToolCallsMiddleware' in str(engine.agent.nodes)
        assert 'write_file' not in str(model.bindings)
    finally:
        engine.close()


def test_identity_followup_enters_native_conversation_without_boundary_schema_calls(tmp_path):
    model = ScriptModel(replies=[answer('What is your identity?'), call('lookup', {}, 'read'), answer('Open.')])
    engine = NativeHarness(model=model, port=Port(), workspace=tmp_path, namespace='one')
    try:
        assert engine.invoke('Please read the status.', 'first') == 'What is your identity?'
        assert engine.invoke('I am admin.rlee. Please continue.', 'second') == 'Open.'
        assert engine.calls == 3
        assert 'I am admin.rlee.' in str(model.seen[-1])
        assert not any(x['name'] in ('RequestBoundary', 'GoalRequirements') for x in engine.attempts)
    finally:
        engine.close()


def test_typed_customer_context_reaches_isolated_native_subagent(tmp_path):
    def verify(messages):
        text = str(messages)
        assert 'admin.rlee' in text and 'original request' in text
        assert 'quoted_customer_messages' in text
        return call('lookup', {}, 'read')
    model = ScriptModel(replies=[answer('Please provide your identity.'),
        call('task', {'subagent_type': 'service', 'description': 'Continue the original request.'}, 'task'),
        verify, answer('Open.'), answer('Open.')])
    engine = NativeHarness(model=model, port=Port(), workspace=tmp_path, namespace='one', shared_context=True)
    try:
        engine.invoke('Please look up the status as my original request.', 'first')
        assert engine.invoke('I am admin.rlee.', 'second') == 'Open.'
        assert engine.calls == 5
    finally:
        engine.close()


def test_supervisor_cannot_execute_an_unadvertised_write(tmp_path):
    port = Port()
    engine = NativeHarness(model=ScriptModel(replies=[call('change', {'value': 'Closed'}, 'forbidden'),
                           answer('This tool is not available to me.')]),
                           port=port, workspace=tmp_path, namespace='one')
    try:
        engine.invoke('Read only.', 'request')
        assert port.writes == 0 and engine.journal.records() == []
        assert engine.attempts[0]['backend_executed'] is False
        assert 'unadvertised_tool' in engine.attempts[0]['result']
    finally:
        engine.close()


def test_uncertain_write_is_not_repeated_by_a_native_subagent(tmp_path):
    port = Port()
    port.lose_ack = True
    model = ScriptModel(replies=[call('task', {'subagent_type': 'service', 'description': 'Change status to Closed.'}, 'task'),
        call('change', {'value': 'Closed'}, 'first-write'), call('change', {'value': 'Closed'}, 'retry-write'),
        answer('The write needs reconciliation.'), answer('The write needs reconciliation.')])
    engine = NativeHarness(model=model, port=port, workspace=tmp_path, namespace='one')
    try:
        engine.invoke('Change status to Closed.', 'request')
        assert port.writes == 1
        assert len(engine.journal.records()) == 1
        assert engine.journal.records()[0]['status'] == 'unknown'
        assert json.loads(engine.attempts[1]['result'])['needs_reconciliation']
    finally:
        engine.close()


def test_shared_model_budget_counts_supervisor_and_subagent_calls(tmp_path):
    engine = NativeHarness(model=ScriptModel(replies=[call('task', {'subagent_type': 'service', 'description': 'Read status.'}, 'task'),
        call('lookup', {}, 'lookup'), answer('Open.'), answer('Open.')]),
        port=Port(), workspace=tmp_path, namespace='one', max_calls=2)
    try:
        with pytest.raises(NativeBudgetExceeded):
            engine.invoke('Read the status.', 'request')
        assert engine.calls == 2
        assert len(engine.journal.records()) == 1
    finally:
        engine.close()


def test_crm_gateway_retains_source_identity_and_destructive_action_checks(tmp_path, monkeypatch):
    import yaml
    from pathlib import Path
    from harness.tools.environment import create_environment
    import harness.tools.domains
    from kb.benchmark.crm_native_adapter import NativeCRMAdapter
    from kb.config import Settings
    from kb.context import ContextBudget
    root = Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    model = ScriptModel(replies=[call('check_permission', {'ticket_id': 'TCK-1004', 'user_id': 'admin.rlee', 'action': 'delete'}, 'invent'),
        answer('Please provide your identity.')])
    adapter = NativeCRMAdapter(create_environment('itsm'), Settings(root=tmp_path),
        yaml.safe_load((root/'src/agents/itsm.yaml').read_text()),
        (root/'src/tasks/policies/itsm_policy.md').read_text(), tmp_path, model=model)
    async def run():
        try:
            await adapter.run('Please delete TCK-1004.', [])
            assert adapter.model_calls == 2
            assert adapter.tool_calls[0]['backend_executed'] is False
            assert 'do not invent' in adapter.tool_calls[0]['result']
            assert 'TCK-1004' in adapter.environment.get_state()['tickets']
        finally:
            await adapter.aclose()
    asyncio.run(run())
