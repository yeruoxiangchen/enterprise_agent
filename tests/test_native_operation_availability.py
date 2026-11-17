"""Observed backend state constrains tool choices without changing the scorer."""
import asyncio

import pytest
import yaml
from pydantic import Field
from langchain_core.utils.function_calling import convert_to_openai_tool

from kb.benchmark.crm_contract import ticket_ids
from kb.benchmark.crm_native_adapter import NativeCRMAdapter
from kb.config import Settings
from kb.context import ContextBudget
from kb.harness.ports import CRMPort
from kb.rl.tasks import BENCH_ROOT,make_task,FAMILIES,environment_for
from kb.rl.reward import score_task
from test_deep_harness import ScriptModel,call
from test_native_harness import answer


def make_port(sample):
    env=environment_for(sample)
    port=CRMPort(env,yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
        (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text())
    port.begin_request(sample['messages'][0],'0')
    return port


def test_compound_ticket_ids_remain_exact_entities():
    assert ticket_ids('Inspect TCK-123-3 and TCK-1234. Do not confuse XXTCK-123-3.')=={'TCK-123-3','TCK-1234'}


def test_availability_uses_only_fresh_receipts_and_executes_no_implicit_reads():
    sample=make_task('dev',25)
    port=make_port(sample);target=sample['target_ticket']
    assert not port.operation_availability()['blocked_operations']
    port.execute('get_ticket',{'ticket_id':target},'get')
    before=list(port.audit)
    assert target in port.operation_availability()['blocked_operations']['reassign_ticket']
    assert port.audit==before
    # Another actor changing the backend invalidates the cached observation.
    port.environment.data['tickets'][target]['assigned_to']='agent.jsmith'
    assert not port.operation_availability()['blocked_operations']
    port.execute('get_ticket',{'ticket_id':target},'fresh')
    available=port.operation_availability()['blocked_operations']
    assert target not in available.get('reassign_ticket',{})
    assert target in available['assign_ticket']


def test_known_open_dependency_is_rejected_before_entering_the_mutating_backend():
    sample=make_task('dev',FAMILIES.index('delete_dependency'))
    port=make_port(sample);target=sample['target_ticket']
    for name in ('get_ticket','check_dependencies','check_permission'):
        port.execute(name,sample['task']['expected_args'][name],name)
    assert target in port.operation_availability()['blocked_operations']['delete_ticket']
    before=port.environment.get_state()
    denied=port.execute('delete_ticket',{'ticket_id':target},'blocked')
    assert denied.status=='refused' and port.audit[-1]['backend_executed'] is False
    assert port.environment.get_state()==before


class SchemaModel(ScriptModel):
    schemas:list=Field(default_factory=list)
    def bind_tools(self,tools,**kwargs):
        self.schemas.append({getattr(t,'name',''):convert_to_openai_tool(t)['function']['parameters']for t in tools})
        return super().bind_tools(tools,**kwargs)


@pytest.mark.parametrize('multiple',[False,True])
def test_unassigned_target_masks_reassignment_without_masking_other_entities(tmp_path,monkeypatch,multiple):
    sample=make_task('dev',25);target=sample['target_ticket']
    environment=environment_for(sample)
    original=sample['messages'][0]
    if multiple:
        other=next(k for k in environment.get_state()['tickets']if k!=target)
        original+=' Also inspect '+other+'.'
    root=SchemaModel(replies=[call('get_ticket',{'ticket_id':target},'get'),
        call('task',{'subagent_type':'ticket_operations__tickets__reassign_ticket',
                     'description':'Follow the original conditional request.'},'task'),answer('Unchanged.')])
    child=SchemaModel(replies=[answer('The ticket is unassigned and remains unchanged.')])
    monkeypatch.setattr(ContextBudget,'count',lambda *args:(100,'test'))
    adapter=NativeCRMAdapter(environment,Settings(root=tmp_path),
        yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
        (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text(),tmp_path,
        model=root,role_models={'*':child},fork_subagents=True,operation_permissions=True,
        prepare_operations=True,specialist_owned_checks=True,bind_user_request=True,
        delegation_protocol='native',execution_granularity='operation')
    try:
        asyncio.run(adapter.run(original,[]))
        schema=child.schemas[0]
        if multiple:
            assert schema['reassign_ticket']['properties']['ticket_id']['not']=={'enum':[target]}
        else:
            assert 'reassign_ticket'not in schema
        assert all(row['name']not in ('assign_ticket','reassign_ticket')for row in adapter.tool_calls)
    finally:asyncio.run(adapter.aclose())


@pytest.mark.parametrize('attempt',['reassign_ticket','assign_ticket'])
def test_model_ignoring_a_state_mask_still_fails_forbidden_attempt_scoring(tmp_path,monkeypatch,attempt):
    sample=make_task('dev',25);target=sample['target_ticket']
    root=ScriptModel(replies=[call('get_ticket',{'ticket_id':target},'get'),
        call('task',{'subagent_type':'ticket_operations__tickets__reassign_ticket',
                     'description':'Follow the original request.'},'task'),answer('Unchanged.')])
    child=ScriptModel(replies=[call(attempt,{'ticket_id':target,'assignee':'admin.rlee'},'bad'),
                              answer('Unchanged.')])
    monkeypatch.setattr(ContextBudget,'count',lambda *args:(100,'test'))
    adapter=NativeCRMAdapter(environment_for(sample),Settings(root=tmp_path),
        yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
        (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text(),tmp_path,
        model=root,role_models={'*':child},fork_subagents=True,operation_permissions=True,
        specialist_owned_checks=True,bind_user_request=True,delegation_protocol='native',execution_granularity='operation')
    try:
        asyncio.run(adapter.run(sample['messages'][0],[]))
        result=score_task(sample,adapter)
        assert result['reward']==0 and result['checks']['forbidden_attempts'] is False
        assert adapter.tool_calls[-1]['backend_executed'] is False
        assert adapter.environment.get_state()['tickets'][target]['assigned_to']==''
    finally:asyncio.run(adapter.aclose())


def test_single_operation_executor_can_complete_cross_domain_permission_checks(tmp_path,monkeypatch):
    sample=make_task('dev',FAMILIES.index('close'));target=sample['target_ticket']
    root=ScriptModel(replies=[call('get_ticket',{'ticket_id':target},'get'),
        call('task',{'subagent_type':'ticket_operations__tickets__close_ticket',
            'description':'Check permissions and dependencies, then close as requested.'},'task'),answer('Closed.')])
    child=ScriptModel(replies=[call('check_operation_permission',
        {'user_id':'agent.jsmith','operation':'close_ticket','ticket_id':target},'permission'),
        call('check_dependencies',{'ticket_id':target},'dependencies'),
        call('close_ticket',{'ticket_id':target},'write'),answer('Closed after the checks.')])
    monkeypatch.setattr(ContextBudget,'count',lambda *args:(100,'test'))
    adapter=NativeCRMAdapter(environment_for(sample),Settings(root=tmp_path),
        yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
        (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text(),tmp_path,
        model=root,role_models={'*':child},fork_subagents=True,operation_permissions=True,
        prepare_operations=True,specialist_owned_checks=True,bind_user_request=True,
        delegation_protocol='native',execution_granularity='operation')
    try:
        asyncio.run(adapter.run(sample['messages'][0],[]))
        result=score_task(sample,adapter)
        assert result['reward']==1,result
        assert 'check_operation_permission'in child.bindings[0]
        assert 'assign_ticket'not in child.bindings[0]and 'delete_ticket'not in child.bindings[0]
        assert [a['name']for a in adapter.tool_calls]==[
            'get_ticket','check_permission','check_dependencies','close_ticket']
    finally:asyncio.run(adapter.aclose())
