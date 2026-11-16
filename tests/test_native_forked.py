"""Native fork handoff and operation-bound permission checks against real CRM."""
import asyncio
import json
from pathlib import Path

import pytest
import yaml
from langchain_core.messages import HumanMessage
from harness.tools.environment import create_environment
import harness.tools.domains

from kb.benchmark.crm_native_adapter import NativeCRMAdapter
from kb.config import Settings
from kb.context import ContextBudget
from kb.harness.native import NativeHarness
from test_deep_harness import ScriptModel, Port, call
from test_native_harness import answer


def test_native_fork_receives_the_actual_request_and_identity_without_supervisor_rephrasing(tmp_path):
    def child(messages):
        literal = [message.content for message in messages if isinstance(message, HumanMessage)]
        assert 'Read the status as my original request.' in literal
        assert 'I am admin.rlee; continue that request.' in literal
        return call('lookup', {}, 'read')
    model = ScriptModel(replies=[answer('Please identify yourself.'),
        call('task', {'subagent_type': 'service', 'description': 'Continue the original request.'}, 'delegate'),
        child, answer('Open.'), answer('Open.')])
    engine = NativeHarness(model=model, port=Port(), workspace=tmp_path, namespace='one', fork_subagents=True)
    try:
        engine.invoke('Read the status as my original request.', 'first')
        assert engine.invoke('I am admin.rlee; continue that request.', 'second') == 'Open.'
        assert engine.calls == 5 and len(engine.journal.records()) == 1
        assert 'quoted_customer_messages' not in str(model.seen[-2])
    finally:
        engine.close()


def adapter(tmp_path, monkeypatch, replies, *, prepare_operations=False, specialist_owned_checks=False):
    root = Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    return NativeCRMAdapter(create_environment('itsm'), Settings(root=tmp_path),
        yaml.safe_load((root/'src/agents/itsm.yaml').read_text()),
        (root/'src/tasks/policies/itsm_policy.md').read_text(), tmp_path,
        model=ScriptModel(replies=replies), fork_subagents=True, operation_permissions=True,
        require_dependency_confirmation=True,prepare_operations=prepare_operations,
        specialist_owned_checks=specialist_owned_checks)


@pytest.mark.parametrize('operation,label,allowed', [('close_ticket', 'edit', True), ('delete_ticket', 'delete', False)])
def test_operation_permission_uses_backend_label_and_preserves_canonical_receipt(tmp_path, monkeypatch, operation, label, allowed):
    instance = adapter(tmp_path, monkeypatch, [call('check_operation_permission',
        {'user_id': 'agent.jsmith', 'operation': operation, 'ticket_id': 'TCK-1002'}, 'permission'),
        answer('The permission assessment is complete.')])
    async def run():
        try:
            await instance.run(f'I am agent.jsmith. Assess my permission for {operation} on TCK-1002.', [])
            receipt = instance.engine.journal.records()[0]
            assert receipt['name'] == 'check_permission'
            assert receipt['arguments']['action'] == label and receipt['result']['allowed'] is allowed
            exported = instance.tool_calls[0]
            assert exported['name'] == 'check_permission' and exported['arguments']['action'] == label
            assert exported['presented_name'] == 'check_operation_permission'
            assert not any(r['is_write'] for r in instance.engine.journal.records())
            from langchain_core.messages import ToolMessage
            response=next(message for message in instance.engine.model.seen[-1] if isinstance(message,ToolMessage))
            body=json.loads(response.content)
            if allowed:
                assert 'business_message'not in body
            else:
                assert 'agent.jsmith'in body['business_message']and operation in body['business_message']
                assert 'is not permitted'in body['business_message']
        finally:
            await instance.aclose()
    asyncio.run(run())


def test_operation_route_preserves_the_actual_user_identity_guard(tmp_path, monkeypatch):
    instance = adapter(tmp_path, monkeypatch, [call('check_operation_permission',
        {'user_id': 'admin.rlee', 'operation': 'delete_ticket', 'ticket_id': 'TCK-1004'}, 'invented'),
        answer('Please provide your identity.')])
    async def run():
        try:
            await instance.run('Please delete TCK-1004.', [])
            receipt = instance.engine.journal.records()[0]
            assert receipt['status'] == 'refused' and receipt['result']['error'] == 'precondition'
            assert not instance.tool_calls[0]['backend_executed']
        finally:
            await instance.aclose()
    asyncio.run(run())


def test_forked_specialist_resumes_confirmed_close_once(tmp_path, monkeypatch):
    root = Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
    config = yaml.safe_load((root/'src/agents/itsm.yaml').read_text())
    domain = next(k for k, value in config['nodes'].items()
                  if any(t['name'] == 'close_ticket' for t in value.get('tools', [])))
    instance = adapter(tmp_path, monkeypatch, [
        call('get_ticket', {'ticket_id': 'TCK-1002'}, 'read'),
        call('check_dependencies', {'ticket_id': 'TCK-1002'}, 'dependencies'),
        call('task', {'subagent_type': domain, 'description': 'Complete the requested close.'}, 'delegate'),
        call('close_ticket', {'ticket_id': 'TCK-1002'}, 'close'), answer('Closed.'), answer('Closed.')])
    async def run():
        try:
            await instance.run('I am agent.jsmith. Close TCK-1002 after I confirm.', [])
            assert instance.pending_review
            assert instance.environment.get_state()['tickets']['TCK-1002']['status'] != 'Closed'
            await instance.run('Yes, close the ticket despite its open dependency.', [],
                approval={'name': 'close_ticket', 'args': {'ticket_id': 'TCK-1002'}, 'decision': 'approve'})
            assert instance.environment.get_state()['tickets']['TCK-1002']['status'] == 'Closed'
            writes = [r for r in instance.engine.journal.records() if r['is_write']]
            assert len(writes) == 1 and writes[0]['status'] == 'committed'
            assert len([c for c in instance.tool_calls if c['name'] == 'close_ticket']) == 1
        finally:
            await instance.aclose()
    asyncio.run(run())


def test_same_operation_approval_survives_precondition_retry_without_a_second_prompt(tmp_path, monkeypatch):
    domain = 'ticket_operations__tickets'
    instance = adapter(tmp_path, monkeypatch, [
        call('check_dependencies', {'ticket_id': 'TCK-1002'}, 'dependencies'),
        call('task', {'subagent_type': domain, 'description': 'Complete the requested close.'}, 'delegate'),
        call('close_ticket', {'ticket_id': 'TCK-1002'}, 'close-before-read'),
        call('get_ticket', {'ticket_id': 'TCK-1002'}, 'required-read'),
        call('close_ticket', {'ticket_id': 'TCK-1002'}, 'close-retry'), answer('Closed.'), answer('Closed.')])
    async def run():
        try:
            await instance.run('I am agent.jsmith. Close TCK-1002 after I confirm.', [])
            assert instance.pending_review
            await instance.run('Yes, proceed despite the dependency.', [],
                approval={'name': 'close_ticket', 'args': {'ticket_id': 'TCK-1002'}, 'decision': 'approve'})
            assert not instance.pending_review
            assert instance.environment.get_state()['tickets']['TCK-1002']['status'] == 'Closed'
            writes = [r for r in instance.engine.journal.records() if r['is_write']]
            assert [r['status'] for r in writes] == ['refused', 'committed']
            assert instance.engine.journal.db.execute('SELECT COUNT(*) FROM native_user_reviews').fetchone()[0] == 1
            assert not instance.engine.has_reusable_user_approval('close_ticket', {'ticket_id': 'TCK-1003'}, 'other')
        finally:
            await instance.aclose()
    asyncio.run(run())


def test_permission_schema_uses_literal_supplied_account_not_a_shortened_guess(tmp_path, monkeypatch):
    instance = adapter(tmp_path, monkeypatch, [
        answer('Please identify yourself.'),
        call('check_operation_permission', {'user_id':'jsmith','operation':'delete_ticket','ticket_id':'TCK-1001'}, 'shortened'),
        call('check_operation_permission', {'user_id':'agent.jsmith','operation':'delete_ticket','ticket_id':'TCK-1001'}, 'literal'),
        answer('Your account does not have permission to delete this ticket.')])
    async def run():
        try:
            await instance.run('Please delete TCK-1001.', [])
            await instance.run('I am agent.jsmith. Continue my deletion request.', [])
            attempts = instance.tool_calls
            assert not attempts[0]['backend_executed']
            assert json.loads(attempts[0]['result'])['error'] == 'invalid_tool_arguments'
            assert attempts[1]['backend_executed']
            assert json.loads(attempts[1]['result'])['allowed'] is False
            schema = instance.engine.routes['check_operation_permission'].parameters_for_request()
            assert schema['properties']['user_id']['enum'] == ['agent.jsmith']
        finally:
            await instance.aclose()
    asyncio.run(run())


def test_prepared_close_observes_prerequisites_before_confirmation_and_commits_once(tmp_path,monkeypatch):
    instance=adapter(tmp_path,monkeypatch,[
        call('task',{'subagent_type':'ticket_operations__tickets','description':'Close the requested ticket.'},'delegate'),
        call('close_ticket',{'ticket_id':'TCK-1002'},'close'),answer('Closed.'),answer('Closed.')],prepare_operations=True)
    async def run():
        try:
            paused=await instance.run('I am agent.jsmith. Close TCK-1002 after confirmation.',[])
            assert 'TCK-1004'in paused.content and instance.pending_review
            before=instance.engine.journal.records()
            assert [r['name']for r in before]==['get_ticket','check_dependencies']
            assert instance.environment.get_state()['tickets']['TCK-1002']['status']!='Closed'
            await instance.run('I approve closing despite the open dependency.',[],
                approval={'name':'close_ticket','args':{'ticket_id':'TCK-1002'},'decision':'approve'})
            receipts=instance.engine.journal.records()
            assert [r['name']for r in receipts]==['get_ticket','check_dependencies','close_ticket']
            assert receipts[-1]['status']=='committed'and instance.engine.tool_calls==3
            ordered=sorted(instance.tool_calls,key=lambda row:row['execution_index'])
            assert [r['name']for r in ordered]==['get_ticket','check_dependencies','close_ticket']
            assert all(r['origin']=='operation_preparation'for r in ordered[:2])
        finally:await instance.aclose()
    asyncio.run(run())


def test_unadvertised_supervisor_write_cannot_start_preparation(tmp_path,monkeypatch):
    instance=adapter(tmp_path,monkeypatch,[call('close_ticket',{'ticket_id':'TCK-1002'},'unadvertised'),
        answer('The ticket specialist must handle this request.')],prepare_operations=True)
    async def run():
        try:
            await instance.run('Please close TCK-1002.',[])
            assert not instance.engine.journal.records()
            assert not instance.pending_review
            assert instance.environment.get_state()['tickets']['TCK-1002']['status']!='Closed'
        finally:await instance.aclose()
    asyncio.run(run())


def test_preparation_reads_share_the_business_call_budget(tmp_path,monkeypatch):
    from kb.harness.native import NativeBudgetExceeded
    instance=adapter(tmp_path,monkeypatch,[
        call('task',{'subagent_type':'ticket_operations__tickets','description':'Close the requested ticket.'},'delegate'),
        call('close_ticket',{'ticket_id':'TCK-1002'},'close')],prepare_operations=True)
    instance.engine.max_tools=1
    async def run():
        try:
            with pytest.raises(NativeBudgetExceeded):await instance.run('Please close TCK-1002.',[])
            receipts=instance.engine.journal.records()
            assert len(receipts)==1 and not receipts[0]['is_write']
            assert instance.environment.get_state()['tickets']['TCK-1002']['status']!='Closed'
        finally:await instance.aclose()
    asyncio.run(run())


def test_specialist_owns_permission_and_dependency_checks_through_actual_delete(tmp_path,monkeypatch):
    def route(messages):
        return call('task',{'subagent_type':'record_safety__deletion',
            'description':'Complete the requested deletion using the supplied identity.'},'delegate')
    instance=adapter(tmp_path,monkeypatch,[route,
        call('check_dependencies',{'ticket_id':'TCK-1004'},'dependencies'),
        call('check_operation_permission',{'user_id':'admin.rlee','operation':'delete_ticket','ticket_id':'TCK-1004'},'permission'),
        call('delete_ticket',{'ticket_id':'TCK-1004'},'delete'),answer('Deleted.'),answer('Deleted.')],
        prepare_operations=True,specialist_owned_checks=True)
    async def run():
        try:
            await instance.run('I am admin.rlee. Permanently delete TCK-1004 after checks.',[])
            assert 'TCK-1004'not in instance.environment.get_state()['tickets']
            bindings=instance.engine.model.bindings
            assert 'task'in bindings[0]and'get_ticket'in bindings[0]
            assert 'check_operation_permission'not in bindings[0]and'check_dependencies'not in bindings[0]
            assert 'check_operation_permission'in bindings[1]and'check_dependencies'in bindings[1]
            assert [r['name']for r in instance.engine.journal.records()]==[
                'check_dependencies','check_permission','delete_ticket']
        finally:await instance.aclose()
    asyncio.run(run())


def test_specialist_owned_assessment_remains_read_only(tmp_path,monkeypatch):
    instance=adapter(tmp_path,monkeypatch,[
        call('task',{'subagent_type':'record_safety__deletion','description':'Assess only; do not change the ticket.'},'delegate'),
        call('check_dependencies',{'ticket_id':'TCK-1001'},'dependencies'),
        call('check_operation_permission',{'user_id':'admin.rlee','operation':'delete_ticket','ticket_id':'TCK-1001'},'permission'),
        answer('No open dependencies; permission allowed. No change made.'),answer('Assessment complete; unchanged.')],
        specialist_owned_checks=True)
    async def run():
        try:
            await instance.run('I am admin.rlee. Assess dependencies and delete permission for TCK-1001; do not modify it.',[])
            assert all(not r['is_write']for r in instance.engine.journal.records())
            assert 'TCK-1001'in instance.environment.get_state()['tickets']
        finally:await instance.aclose()
    asyncio.run(run())
