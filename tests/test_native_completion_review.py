import pytest

from kb.harness.native import NativeHarness, NativeApprovalRequired
from kb.harness.completion_review import RUBRIC
from test_deep_harness import ScriptModel, Port, call
from test_native_harness import answer


def grade(passed, gap='Execute the requested change through the service specialist; use the actual receipt.'):
    names=[line[2:] for line in RUBRIC.splitlines() if line.startswith('- ')]
    criteria=[{'name':name,'passed':passed,**({}if passed else{'gap':gap})}for name in names]
    return call('GraderResponse',{'result':'satisfied'if passed else'needs_revision',
        'explanation':'Actual receipts prove completion.'if passed else'Only a promise exists; no committed change.',
        'criteria':criteria},'review')


def test_official_rubric_retries_a_promise_and_observes_actual_write(tmp_path):
    model=ScriptModel(replies=[answer('I will change the status.'),grade(False),
        call('task',{'subagent_type':'service','description':'Change status to Closed.'},'delegate'),
        call('change',{'value':'Closed'},'write'),answer('Closed.'),answer('The status was changed to Closed.'),grade(True)])
    port=Port();engine=NativeHarness(model=model,port=port,workspace=tmp_path,namespace='one',completion_review=True)
    try:
        assert 'Closed'in engine.invoke('Change status to Closed.','r')
        assert port.writes==1 and engine.calls==7
        assert engine.last_review_status=='satisfied'and len(engine.review_evaluations)==2
        assert 'inspect_execution_evidence'in str(model.bindings[1])
        assert 'change'not in model.bindings[1]
        assert 'RequestBoundary'not in str(model.bindings)
    finally:engine.close()


def test_official_iteration_cap_stops_after_one_unsuccessful_retry(tmp_path):
    engine=NativeHarness(model=ScriptModel(replies=[answer('I will change it.'),grade(False),
        answer('I will change it shortly.'),grade(False)]),port=Port(),workspace=tmp_path,namespace='one',completion_review=True)
    try:
        engine.invoke('Change status to Closed.','r')
        assert engine.calls==4 and engine.last_review_status=='max_iterations_reached'
        assert engine.port.writes==0 and len(engine.review_evaluations)==2
    finally:engine.close()


def test_missing_input_and_cancellation_can_stop_without_a_write(tmp_path):
    engine=NativeHarness(model=ScriptModel(replies=[answer('Please provide your identity.'),grade(True),
        answer('The pending request has been cancelled.'),grade(True)]),port=Port(),workspace=tmp_path,
        namespace='one',completion_review=True)
    try:
        engine.invoke('Change the status after verifying my identity.','first')
        engine.invoke('Cancel the previous request; change nothing.','second')
        assert engine.port.writes==0 and engine.calls==4
        assert engine.last_review_status=='satisfied'
    finally:engine.close()


def test_reviewer_context_budget_counts_the_native_verdict_schema(tmp_path):
    from types import SimpleNamespace
    schemas=[]
    class Budget:
        settings=SimpleNamespace(context_tokens=8192,output_tokens=1400)
        def count(self,messages,tools):
            schemas.append([t['function']['name']for t in tools])
            return 100,'test'
    engine=NativeHarness(model=ScriptModel(replies=[answer('Please provide your identity.'),grade(True)]),
        port=Port(),workspace=tmp_path,namespace='one',completion_review=True,context_budget=Budget())
    try:
        engine.invoke('Change it after checking my identity.','r')
        assert any('GraderResponse'in names for names in schemas)
    finally:engine.close()


@pytest.mark.parametrize('decision,writes',[('approve',1),('reject',0)])
def test_native_approval_resumes_the_existing_tool_call_once(tmp_path,decision,writes):
    model=ScriptModel(replies=[call('task',{'subagent_type':'service','description':'Change to Closed.'},'delegate'),
        call('change',{'value':'Closed'},'write'),answer('The reviewed operation is handled.'),answer('Handled.')])
    port=Port();engine=NativeHarness(model=model,port=port,workspace=tmp_path,namespace='one',
        interrupt_on={'change':{'allowed_decisions':['approve','reject']}})
    try:
        with pytest.raises(NativeApprovalRequired)as pending:
            engine.invoke('Change status to Closed.','r')
        assert pending.value.payloads and port.writes==0
        assert engine.invoke('Change status to Closed.','r',decisions=[{'type':decision}])=='Handled.'
        assert port.writes==writes and engine.calls==4
        assert engine.invoke('Change status to Closed.','r')=='Handled.'
        assert port.writes==writes and engine.calls==4
    finally:engine.close()


def test_user_approval_can_resume_after_process_restart_without_replanning(tmp_path):
    model=ScriptModel(replies=[call('task',{'subagent_type':'service','description':'Change to Closed.'},'delegate'),
        call('change',{'value':'Closed'},'write'),answer('Closed.'),answer('Closed.')])
    port=Port();options={'model':model,'port':port,'workspace':tmp_path,'namespace':'one',
        'interrupt_on':{'change':{'allowed_decisions':['approve','reject']}}}
    engine=NativeHarness(**options)
    with pytest.raises(NativeApprovalRequired):engine.invoke('Change status to Closed.','r')
    engine.close()
    resumed=NativeHarness(**options)
    try:
        with pytest.raises(NativeApprovalRequired):
            resumed.invoke('Change status to Closed.','r')
        pending=resumed.journal.db.execute('SELECT actions FROM native_pending_reviews').fetchone()
        import json
        assert json.loads(pending['actions'])[0]['tool_call_id']=='write'
        assert resumed.invoke('Change status to Closed.','r',decisions=[{'type':'approve'}])=='Closed.'
        assert port.writes==1 and resumed.calls==2
    finally:resumed.close()


def test_reviewer_correction_cannot_bypass_native_user_confirmation(tmp_path):
    model = ScriptModel(replies=[
        answer('Please confirm the status change.'), grade(False),
        call('task', {'subagent_type': 'service', 'description': 'Change to Closed.'}, 'delegate'),
        call('change', {'value': 'Closed'}, 'write'),
        answer('Closed.'), answer('The status is Closed.'), grade(True),
    ])
    port = Port()
    engine = NativeHarness(model=model, port=port, workspace=tmp_path, namespace='one',
        completion_review=True, interrupt_on={'change': {'allowed_decisions': ['approve', 'reject']}})
    try:
        with pytest.raises(NativeApprovalRequired):
            engine.invoke('Change status only after my confirmation.', 'r')
        assert port.writes == 0 and len(engine.review_evaluations) == 1
        assert 'Closed' in engine.invoke('Change status only after my confirmation.', 'r',
            decisions=[{'type': 'approve'}], review_message='I approve the requested status change.')
        assert port.writes == 1 and engine.last_review_status == 'satisfied'
        assert engine.calls == 7
        assert engine.invoke('Change status only after my confirmation.', 'r')
        assert port.writes == 1 and engine.calls == 7
    finally:
        engine.close()
