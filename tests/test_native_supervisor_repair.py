"""Native task compatibility and upstream, root-only execution review."""
import pytest

from kb.harness.native import NativeHarness, NativeApprovalRequired
from kb.harness.contracts import ToolOutcome, ToolSpec
from test_deep_harness import ScriptModel, Port, call
from test_native_harness import answer
from test_native_completion_review import grade


def test_native_protocol_uses_the_original_task_api_and_capability_graph(tmp_path,monkeypatch):
    def custom_schema(*args):
        raise AssertionError('Native task ABI must not be extended')
    monkeypatch.setattr('kb.harness.native.task_schema',custom_schema)
    root=ScriptModel(replies=[call('task',{'subagent_type':'service__execute',
        'description':'Change status to Closed.'},'task'),answer('Closed.')])
    child=ScriptModel(replies=[call('change',{'value':'Closed'},'change'),answer('Closed.')])
    port=Port()
    engine=NativeHarness(model=root,role_models={'*':child},port=port,workspace=tmp_path,
        namespace='native',fork_subagents=True,bind_user_request=True,delegation_protocol='native')
    try:
        assert engine.invoke('Change status to Closed.','r')=='Closed.'
        assert port.writes==1
        assert 'allowed_write_tools' not in str(root.seen[0])
        assert 'Change status to Closed.' in str(child.seen[0])
    finally:engine.close()


def test_native_observer_has_no_write_capability_even_when_the_overall_goal_has_a_write(tmp_path):
    root=ScriptModel(replies=[call('task',{'subagent_type':'service',
        'description':'Read status before I decide the next step.'},'task'),answer('Observed.')])
    child=ScriptModel(replies=[call('change',{'value':'Closed'},'bad'),answer('Only observation is permitted.')])
    port=Port()
    engine=NativeHarness(model=root,role_models={'*':child},port=port,workspace=tmp_path,
        namespace='observe',fork_subagents=True,bind_user_request=True,delegation_protocol='native')
    try:
        engine.invoke('Check status, then change it if permitted.','r')
        assert port.writes==0 and 'change' not in child.bindings[0]
        assert engine.attempts[0]['backend_executed'] is False
        assert engine.attempts[0]['business_outcome']=='rejected'
        assert engine.routing_errors()
    finally:engine.close()


def test_execution_graph_cannot_substitute_another_domain_mutation(tmp_path):
    class TwoWritePort(Port):
        def tools(self):
            return [*super().tools(),ToolSpec('erase','Erase status.',
                {'type':'object','properties':{}},'service',True)]
    root=ScriptModel(replies=[call('task',{'subagent_type':'service__change',
        'description':'Change the status.'},'task'),answer('Closed.')])
    child=ScriptModel(replies=[call('erase',{},'substitute'),
        call('change',{'value':'Closed'},'write'),answer('Closed.')])
    port=TwoWritePort()
    engine=NativeHarness(model=root,role_models={'*':child},port=port,workspace=tmp_path,
        namespace='scope',fork_subagents=True,bind_user_request=True,delegation_protocol='native')
    try:
        assert engine.invoke('Change status to Closed.','r')=='Closed.'
        assert port.writes==1 and 'erase'not in child.bindings[0]
        assert engine.capabilities['service__change']['tools']==['lookup','change']
        assert 'service__execute'not in engine.capabilities
        assert engine.attempts[0]['business_outcome']=='rejected'
        assert engine.attempts[0]['backend_executed']is False
        assert [r['name']for r in engine.journal.records()]==['change']
    finally:engine.close()


def test_forked_completion_review_grades_only_the_root_and_keeps_upstream_iteration_state(tmp_path):
    root=ScriptModel(replies=[answer('I will change it.'),
        call('task',{'subagent_type':'service__execute','description':'Close the status.'},'task'),answer('Closed.')])
    child=ScriptModel(replies=[call('change',{'value':'Closed'},'write'),answer('Closed.')])
    reviewer=ScriptModel(replies=[grade(False),grade(True)])
    port=Port()
    engine=NativeHarness(model=root,role_models={'*':child,'reviewer':reviewer},port=port,
        workspace=tmp_path,namespace='review',fork_subagents=True,bind_user_request=True,
        delegation_protocol='native',completion_review=True)
    try:
        assert engine.invoke('Change status to Closed.','r')=='Closed.'
        assert port.writes==1 and len(engine.review_evaluations)==2
        assert len(reviewer.seen)==2 and len(child.seen)==2 and engine.calls==7
        assert engine.last_review_status=='satisfied'
    finally:engine.close()


@pytest.mark.parametrize('kind',['lookup','policy_denial'])
def test_error_trigger_skips_clean_results_and_actual_policy_denials(tmp_path,kind):
    class DenyPort(Port):
        def execute(self,name,args,operation_id):
            if name=='change':return ToolOutcome({'error':'permission_denied'},'refused')
            return super().execute(name,args,operation_id)
    reviewer=ScriptModel(replies=[])
    if kind=='lookup':
        root=ScriptModel(replies=[call('lookup',{},'get'),answer('Open.')]);child=ScriptModel(replies=[])
    else:
        root=ScriptModel(replies=[call('task',{'subagent_type':'service__execute',
            'description':'Change status to Closed.'},'task'),answer('Permission denied.')])
        child=ScriptModel(replies=[call('change',{'value':'Closed'},'change'),answer('Permission denied.')])
    port=DenyPort()
    engine=NativeHarness(model=root,role_models={'*':child,'reviewer':reviewer},port=port,
        workspace=tmp_path,namespace='review',fork_subagents=True,bind_user_request=True,
        delegation_protocol='native',completion_review='errors')
    try:
        engine.invoke('Read status.' if kind=='lookup' else 'Close the status.','r')
        assert not reviewer.seen and not engine.review_evaluations
        assert not engine.routing_errors() and port.writes==0
    finally:engine.close()


def test_official_error_review_recovers_an_invalid_root_write_without_direct_execution(tmp_path):
    root=ScriptModel(replies=[call('change',{'value':'Closed'},'bad'),answer('I will delegate it.'),
        call('task',{'subagent_type':'service__execute','description':'Change status to Closed.'},'task'),answer('Closed.')])
    child=ScriptModel(replies=[call('change',{'value':'Closed'},'write'),answer('Closed.')])
    reviewer=ScriptModel(replies=[grade(False),grade(True)])
    port=Port()
    engine=NativeHarness(model=root,role_models={'*':child,'reviewer':reviewer},port=port,
        workspace=tmp_path,namespace='review',fork_subagents=True,bind_user_request=True,
        delegation_protocol='native',completion_review='errors')
    try:
        assert engine.invoke('Change status to Closed.','r')=='Closed.'
        assert len(engine.routing_errors())==1 and port.writes==1 and engine.calls==8
        assert engine.attempts[0]['backend_executed'] is False
        assert len(engine.review_evaluations)==2 and engine.last_review_status=='satisfied'
        assert any(getattr(m,'name',None)=='rubric_grader'for m in root.seen[-1])
        assert engine.invoke('Change status to Closed.','r')=='Closed.' and port.writes==1
    finally:engine.close()


def test_routing_failure_survives_restart_and_does_not_leak_to_the_next_request(tmp_path):
    def disconnect(messages):raise ConnectionError('controlled disconnect')
    port=Port()
    engine=NativeHarness(model=ScriptModel(replies=[call('change',{'value':'Closed'},'bad'),disconnect]),
        port=port,workspace=tmp_path,namespace='resume',fork_subagents=True,bind_user_request=True,
        delegation_protocol='native',completion_review='errors')
    try:
        with pytest.raises(ConnectionError):engine.invoke('Close the status.','r')
        assert len(engine.routing_errors())==1 and port.writes==0
    finally:engine.close()
    root=ScriptModel(replies=[answer('I will delegate.'),call('task',{'subagent_type':'service__execute',
        'description':'Change status to Closed.'},'task'),answer('Closed.'),answer('It is Closed.')])
    child=ScriptModel(replies=[call('change',{'value':'Closed'},'write'),answer('Closed.')])
    reviewer=ScriptModel(replies=[grade(False),grade(True)])
    engine=NativeHarness(model=root,role_models={'*':child,'reviewer':reviewer},port=port,
        workspace=tmp_path,namespace='resume',fork_subagents=True,bind_user_request=True,
        delegation_protocol='native',completion_review='errors')
    try:
        assert engine.invoke('Close the status.','r')=='Closed.' and port.writes==1
        assert engine.invoke('Describe current status.','next')=='It is Closed.'
        assert not engine.routing_errors() and len(reviewer.seen)==2
    finally:engine.close()


@pytest.mark.parametrize('decision,writes',[('approve',1),('reject',0)])
def test_root_error_recovery_preserves_forked_human_confirmation(tmp_path,decision,writes):
    root=ScriptModel(replies=[call('task',{'subagent_type':'missing',
        'description':'Change status after confirmation.'},'invalid'),answer('I will delegate.'),
        call('task',{'subagent_type':'service__execute','description':'Change status after confirmation.'},'task'),
        answer('The reviewed operation is handled.')])
    child=ScriptModel(replies=[call('change',{'value':'Closed'},'write'),answer('The review is handled.')])
    reviewer=ScriptModel(replies=[grade(False),grade(True)])
    port=Port()
    engine=NativeHarness(model=root,role_models={'*':child,'reviewer':reviewer},port=port,
        workspace=tmp_path,namespace='confirm',fork_subagents=True,bind_user_request=True,
        delegation_protocol='native',completion_review='errors',
        interrupt_on={'change':{'allowed_decisions':['approve','reject']}})
    try:
        with pytest.raises(NativeApprovalRequired):
            engine.invoke('Change the status only after my confirmation.','r')
        assert port.writes==0 and len(engine.review_evaluations)==1
        assert engine.invoke('Change the status only after my confirmation.','r',
            decisions=[{'type':decision}])=='The reviewed operation is handled.'
        assert port.writes==writes and len(engine.review_evaluations)==2
        calls=engine.calls
        assert engine.invoke('Change the status only after my confirmation.','r')=='The reviewed operation is handled.'
        assert port.writes==writes and engine.calls==calls
    finally:engine.close()
