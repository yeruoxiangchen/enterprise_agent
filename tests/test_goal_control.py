"""Actual native continuations and canonical ITSM actions across user turns."""
import json
from pathlib import Path

import yaml
import pytest

from kb.context import ContextBudget
from kb.harness.engine import DeepHarness
from kb.harness.ports import CRMPort
from test_deep_harness import ScriptModel,Port,call,finish


def setup(tmp_path,replies):
    from harness.tools.environment import create_environment
    import harness.tools.domains
    root=Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
    port=CRMPort(create_environment('itsm'),yaml.safe_load((root/'src/agents/itsm.yaml').read_text()),
                 (root/'src/tasks/policies/itsm_policy.md').read_text())
    model=ScriptModel(replies=replies)
    return DeepHarness(model=model,port=port,workspace=tmp_path,namespace='conversation',max_calls=40),model


def boundary(user,relation='continue'):
    return call('RequestBoundary',{'relation':relation,'facts':[
        {'parameter':'user_id','value':user,'quote':user}]if user else[]})


def requirement(tool,arguments=None,description='Requested business operation.'):
    return call('GoalRequirements',{'kind':'business_operations','actions':[
        {'tool':tool,'arguments':arguments or{},'description':description,'after':[]}]})


def review_stop(messages):
    payload=json.loads(messages[-1].content)
    evidence=next(r['evidence_id']for r in payload['ledger']['recent_observations']
        if r['tool']=='check_permission'and r['value'].get('allowed')is False)
    return call('ProgressDecision',{'is_request_satisfied':True,'is_progress_being_made':False,
        'is_in_loop':False,'next_action':'stop','instruction':'The requested action is denied by current policy.',
        'final_result':{'status':'refused','answer':'Your current account cannot delete this ticket.',
                        'evidence_ids':[evidence],'remaining_work':[]}})


def refusal(*,compile_goal=True):
    return [call('check_permission',{'user_id':'agent.jsmith','action':'delete','ticket_id':'TCK-1001'}),
        finish('Your account cannot delete this ticket.')[0],call('CompletionMetadata',{
            'status':'partial','evidence_ids':[],'remaining_work':[{
                'kind':'agent_action','description':'Look for an alternative user role.'}]}),
        *([requirement('delete_ticket',{'ticket_id':'TCK-1001'})]if compile_goal else[])]


def deletion(ticket,user='admin.rlee',*,delegate=True):
    return [*([call('task',{'subagent_type':'record_safety__deletion','description':f'Delete {ticket} for {user} after safety checks.'})]if delegate else[]),
        call('check_dependencies',{'ticket_id':ticket}),
        call('check_permission',{'user_id':user,'action':'delete','ticket_id':ticket}),
        call('delete_ticket',{'ticket_id':ticket}),*finish('The ticket was deleted.'),*finish('The ticket was deleted.')]


def test_identity_followup_continues_the_original_goal_after_an_unexecuted_promise(tmp_path):
    def replan(messages):
        payload=json.loads(messages[-1].content)
        assert 'Delete TCK-1003'in payload['user_request']and 'admin.rlee'in payload['user_request']
        return call('ProgressDecision',{'is_request_satisfied':False,'is_progress_being_made':True,
            'is_in_loop':False,'next_action':'replan','next_agent':'record_safety__deletion',
            'instruction':'Delete TCK-1003 for admin.rlee after the required checks.'})
    engine,model=setup(tmp_path,[*finish('Please provide your user ID.','needs_user_input'),
        boundary('admin.rlee'),requirement('delete_ticket',{'ticket_id':'TCK-1003'}),
        *finish('I will delete the ticket.','partial'),replan,*deletion('TCK-1003',delegate=False)])
    try:
        assert engine.invoke('Delete TCK-1003 after the required checks.','first').status=='needs_user_input'
        result=engine.invoke('My identity is admin.rlee. Please continue.','second')
        assert result.status=='completed'and 'TCK-1003'not in engine.port.environment.get_state()['tickets']
        assert sum(a['name']=='delete_ticket'and a['backend_executed']for a in engine.port.audit)==1
        assert engine.active_goal.id=='first'and engine.active_goal.status=='completed'
        assert engine.control_passes==1 and not model.replies
        messages=engine.agent.get_state({'configurable':{'thread_id':'conversation'}}).values['messages']
        assert any(m.additional_kwargs.get('lc_source')=='model_progress_routing'for m in messages)
    finally:engine.close()


def test_proven_refusal_repeated_without_new_authority_stops_without_more_business_calls(tmp_path):
    engine,model=setup(tmp_path,[*refusal(),boundary('agent.jsmith')])
    try:
        first=engine.invoke('I am agent.jsmith. Delete TCK-1001.','first')
        assert first.status=='refused'
        second=engine.invoke('I am still agent.jsmith. Please proceed anyway.','second')
        assert second==first and len(engine.port.audit)==1 and not model.replies
        assert engine.active_goal.denial_evidence_ids
        assert 'TCK-1001'in engine.port.environment.get_state()['tickets']
    finally:engine.close()


def test_new_identity_invalidates_refusal_reuse_and_is_checked_again(tmp_path):
    engine,model=setup(tmp_path,[*refusal(),boundary('admin.rlee'),requirement('delete_ticket',{'ticket_id':'TCK-1001'}),*deletion('TCK-1001')])
    try:
        engine.invoke('I am agent.jsmith. Delete TCK-1001.','first')
        result=engine.invoke('I am admin.rlee. Please continue the original request.','second')
        assert result.status=='completed'and 'TCK-1001'not in engine.port.environment.get_state()['tickets']
        assert [a['arguments']['user_id']for a in engine.port.audit if a['name']=='check_permission']==['agent.jsmith','admin.rlee']
        assert not model.replies
    finally:engine.close()


def test_new_goal_after_refusal_does_not_continue_the_old_write(tmp_path):
    engine,model=setup(tmp_path,[*refusal(),boundary(None,'replace'),
        call('get_ticket',{'ticket_id':'TCK-1004'}),*finish('TCK-1004 is Open.')])
    try:
        engine.invoke('I am agent.jsmith. Delete TCK-1001.','first')
        result=engine.invoke('What is the status of TCK-1004 instead?','second')
        assert result.status=='completed'and engine.active_goal.id=='second'
        assert not any(a['name']=='delete_ticket'for a in engine.port.audit)
        assert not model.replies
    finally:engine.close()


def test_invented_followup_fact_is_repaired_by_native_schema_validation(tmp_path):
    engine,model=setup(tmp_path,[*refusal(),boundary('admin.rlee'),boundary('agent.jsmith')])
    try:
        engine.invoke('I am agent.jsmith. Delete TCK-1001.','first')
        assert engine.invoke('I am still agent.jsmith. Proceed anyway.','second').status=='refused'
        assert len(engine.port.audit)==1
        assert any('Facts must use catalogue'in str(messages)for messages in model.seen)
        assert not model.replies
    finally:engine.close()


def test_repeated_prior_user_facts_are_not_misclassified_as_new_authorization(tmp_path):
    engine,model=setup(tmp_path,[*refusal(),call('RequestBoundary',{'relation':'continue','facts':[
        {'parameter':'action','value':'delete','quote':'Delete TCK-1001.'},
        {'parameter':'ticket_id','value':'TCK-1001','quote':'Delete TCK-1001.'},
        {'parameter':'user_id','value':'agent.jsmith','quote':'I am still agent.jsmith.'}]})])
    try:
        first=engine.invoke('I am agent.jsmith. Delete TCK-1001.','first')
        assert engine.invoke('I am still agent.jsmith. Please proceed anyway.','second')==first
        assert len(engine.port.audit)==1 and not model.replies
        events=[json.loads(line)for line in (tmp_path/'events.jsonl').read_text().splitlines()]
        assert any(e['kind']=='remembered_user_facts_ignored'for e in events)
        assert engine.active_goal.status=='refused'
    finally:engine.close()


def test_native_schema_repair_limit_stops_repeated_invented_authority_before_business_calls(tmp_path):
    from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
    engine,model=setup(tmp_path,[*finish('Please provide your identity.','needs_user_input'),
        boundary('admin.rlee'),boundary('admin.rlee'),boundary('admin.rlee')])
    try:
        engine.invoke('Delete TCK-1003 after the required checks.','first')
        with pytest.raises(ModelCallLimitExceededError):
            engine.invoke('My identity is agent.jsmith.','second')
        assert engine.turn_calls==3 and not model.replies
        assert not engine.port.audit and 'TCK-1003'in engine.port.environment.get_state()['tickets']
    finally:engine.close()


def test_continuation_limit_survives_resume_of_the_same_request(tmp_path):
    replan=call('ProgressDecision',{'is_request_satisfied':False,'is_progress_being_made':True,
        'is_in_loop':False,'next_action':'replan','instruction':'Continue the remaining authorized work.'})
    model=ScriptModel(replies=[*finish('Still unfinished.','partial'),requirement('lookup'),replan,
        *finish('Still unfinished.','partial'),replan,*finish('Still unfinished.','partial'),
        *finish('Still unfinished.','partial')])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one')
    try:
        assert engine.invoke('Read the status.','same').status=='partial'
        assert engine.control_passes==2
        assert engine.invoke('Read the status.','same').status=='partial'
        assert engine.control_passes==2 and not model.replies
    finally:engine.close()


def test_changed_backend_version_requires_a_fresh_policy_check(tmp_path,monkeypatch):
    engine,model=setup(tmp_path,[*refusal(),boundary('agent.jsmith'),*refusal(compile_goal=False)])
    try:
        engine.invoke('I am agent.jsmith. Delete TCK-1001.','first')
        original=engine.port.version
        monkeypatch.setattr(engine.port,'version',lambda:original()+'-external-change')
        assert engine.invoke('I am still agent.jsmith. Proceed anyway.','second').status=='refused'
        assert len(engine.port.audit)==2 and not model.replies
    finally:engine.close()


def test_goal_bound_writes_reuse_receipts_after_clarification_but_allow_new_requests(tmp_path):
    def change():return [call('task',{'subagent_type':'service','description':'Change the status to Closed.'}),
        call('change',{'value':'Closed'}),*finish('Status changed to Closed.')]
    model=ScriptModel(replies=[*change(),*finish('Which language should I use?','needs_user_input'),
        boundary(None),requirement('change',{'value':'Closed'}),*change(),*finish('The status is Closed.'),
        *change(),*finish('The new operation completed.')])
    port=Port();engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one')
    try:
        engine.invoke('Change status to Closed and then explain.','first')
        assert port.writes==1
        assert engine.invoke('Explain in English.','second').status=='completed'
        assert port.writes==1 and engine.active_goal.id=='first'
        assert engine.invoke('Perform a new change to Closed again.','third').status=='completed'
        assert port.writes==2 and engine.active_goal.id=='third'and not model.replies
    finally:engine.close()


def test_refusal_and_goal_binding_survive_engine_restart(tmp_path):
    first,_=setup(tmp_path,refusal())
    first.invoke('I am agent.jsmith. Delete TCK-1001.','first')
    port=first.port;first.close()
    model=ScriptModel(replies=[boundary('agent.jsmith')])
    second=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='conversation',max_calls=40)
    try:
        assert second.invoke('I am still agent.jsmith. Please proceed anyway.','second').status=='refused'
        assert second.active_goal.id=='first'and len(port.audit)==1 and not model.replies
    finally:second.close()


def test_permission_assessment_is_completed_even_when_the_answer_is_no(tmp_path):
    engine,model=setup(tmp_path,[call('check_permission',{'user_id':'agent.jsmith','action':'delete','ticket_id':'TCK-1001'}),
        *finish('You do not have delete permission.'),requirement('check_permission',{'user_id':'agent.jsmith','action':'delete','ticket_id':'TCK-1001'}),call('ProgressDecision',{
            'is_request_satisfied':True,'is_progress_being_made':True,'is_in_loop':False,'next_action':'stop',
            'instruction':'The permission assessment is complete; no deletion was requested.',
            'final_result':{'status':'completed','answer':'You do not have delete permission.','evidence_ids':[],'remaining_work':[]}})])
    try:
        assert engine.invoke('I am agent.jsmith. Check if I may delete TCK-1001; do not delete it.','first').status=='completed'
        assert not engine.active_goal.denial_evidence_ids and not model.replies
        assert 'TCK-1001'in engine.port.environment.get_state()['tickets']
    finally:engine.close()


def test_permission_denial_does_not_terminate_a_different_requested_mutation(tmp_path):
    from kb.harness.goals import RequestedAction
    engine,_=setup(tmp_path,[])
    try:
        engine.request_id='first'
        engine.active_goal=engine.goals.fresh('Delete TCK-1003.','first')
        engine.port.begin_request('I am agent.jsmith.','first')
        engine.journal.invoke(engine.specs['check_permission'],{'user_id':'agent.jsmith',
            'action':'delete','ticket_id':'TCK-1001'},'first',engine.port,lambda *x:None)
        action=RequestedAction(tool='delete_ticket',arguments={'ticket_id':'TCK-1003'})
        assert engine.port.refusal_for_actions([action],engine.journal.records())is None
        assessment=RequestedAction(tool='check_permission',arguments={'ticket_id':'TCK-1001'})
        assert engine.port.refusal_for_actions([assessment],engine.journal.records())is None
    finally:engine.close()


def test_permission_catalog_supports_closure_for_an_agent_without_delete_permission(tmp_path):
    from kb.harness.goals import RequestedAction
    engine,_=setup(tmp_path,[]);port=engine.port
    try:
        port.begin_request('I am agent.jsmith. Close TCK-1004.','r')
        schema=engine.specs['check_permission'].parameters['properties']['action']
        assert 'close_ticket=edit'in schema['description']and 'delete_ticket=delete'in schema['description']
        def execute(name,arguments):
            return engine.journal.invoke(engine.specs[name],arguments,'r',port,lambda *a:None)
        assert execute('check_permission',{'user_id':'agent.jsmith','action':'edit','ticket_id':'TCK-1004'})['value']['allowed']
        execute('get_ticket',{'ticket_id':'TCK-1004'});execute('check_dependencies',{'ticket_id':'TCK-1004'})
        assert execute('close_ticket',{'ticket_id':'TCK-1004'})['status']=='committed'
        assert execute('check_permission',{'user_id':'agent.jsmith','action':'delete','ticket_id':'TCK-1004'})['value']['allowed']is False
        close=RequestedAction(tool='close_ticket',arguments={'ticket_id':'TCK-1004'})
        assert port.refusal_for_actions([close],engine.journal.records())is None
        assert port.environment.get_state()['tickets']['TCK-1004']['status']=='Closed'
    finally:engine.close()


def test_wrong_permission_denial_is_repaired_before_native_closure_delegation(tmp_path):
    def replan(messages):
        assert 'No matching backend refusal'in str(messages)
        # The original catalogue is in the first user message; schema repair
        # feedback is appended without changing the available capabilities.
        payload=json.loads(next(m.content for m in messages if m.type=='human'))
        agent=next(name for name,names in payload['available_specialists'].items()if 'close_ticket'in names)
        return call('ProgressDecision',{'is_request_satisfied':False,'is_progress_being_made':True,
            'is_in_loop':False,'next_action':'replan','next_agent':agent,
            'instruction':'Close TCK-1004 for agent.jsmith after looking it up and checking dependencies.'})
    engine,model=setup(tmp_path,[call('check_permission',{'user_id':'agent.jsmith','action':'delete','ticket_id':'TCK-1004'}),
        *finish('The account cannot close the ticket because deletion is denied.','partial'),
        requirement('close_ticket',{'ticket_id':'TCK-1004'}),call('ProgressDecision',{
            'is_request_satisfied':True,'is_progress_being_made':False,'is_in_loop':False,
            'next_action':'stop','instruction':'Deletion is denied.',
            'final_result':{'status':'refused','answer':'You cannot close the ticket.'}}),replan,
        call('get_ticket',{'ticket_id':'TCK-1004'}),call('check_dependencies',{'ticket_id':'TCK-1004'}),
        call('close_ticket',{'ticket_id':'TCK-1004'}),*finish('TCK-1004 is closed.'),*finish('TCK-1004 is closed.')])
    try:
        result=engine.invoke('I am agent.jsmith. Close TCK-1004.','first')
        assert result.status=='completed'and engine.port.environment.get_state()['tickets']['TCK-1004']['status']=='Closed'
        assert not model.replies and engine.control_passes==1
    finally:engine.close()


def test_business_receipt_rendering_uses_actual_pre_write_checks_and_is_idempotent(tmp_path):
    engine,_=setup(tmp_path,[]);port=engine.port
    try:
        port.begin_request('I am admin.rlee. Delete TCK-1003.','r')
        for name,args in [('check_dependencies',{'ticket_id':'TCK-1003'}),
                ('check_permission',{'user_id':'admin.rlee','action':'delete','ticket_id':'TCK-1003'}),
                ('delete_ticket',{'ticket_id':'TCK-1003'})]:
            engine.journal.invoke(engine.specs[name],args,'r',port,lambda *a:None)
        records=engine.journal.records();answer=port.render_answer('TCK-1003 was deleted.',records)
        assert 'Safety checks completed before'in answer and 'admin.rlee had delete permission'in answer
        assert port.render_answer(answer,records)==answer
        records[0]['created']=records[-1]['created']+1
        assert port.render_answer('TCK-1003 was deleted.',records)=='TCK-1003 was deleted.'
    finally:engine.close()


def test_read_only_request_does_not_receive_a_previous_request_write_receipt(tmp_path):
    engine,_=setup(tmp_path,[]);port=engine.port
    try:
        port.begin_request('What is the status?','new')
        records=[{'request_id':'old','status':'committed','name':'delete_ticket','arguments':{'ticket_id':'TCK-1003'},'created':3}]
        assert port.render_answer('The status is Open.',records)=='The status is Open.'
        assert not engine.port.audit
    finally:engine.close()


def test_deletion_requires_permission_after_dependencies_and_recovers_without_duplicate_mutation(tmp_path):
    engine,model=setup(tmp_path,[call('check_permission',{'user_id':'admin.rlee','action':'delete','ticket_id':'TCK-1003'}),
        call('check_dependencies',{'ticket_id':'TCK-1003'}),
        call('task',{'subagent_type':'record_safety__deletion','description':'Delete TCK-1003 for admin.rlee.'}),
        call('delete_ticket',{'ticket_id':'TCK-1003'}),
        call('check_permission',{'user_id':'admin.rlee','action':'delete','ticket_id':'TCK-1003'}),
        call('delete_ticket',{'ticket_id':'TCK-1003'}),*finish('Deleted.'),*finish('Deleted.')])
    try:
        assert engine.invoke('I am admin.rlee. Delete TCK-1003.','first').status=='completed'
        attempts=[a for a in engine.port.audit if a['name']=='delete_ticket']
        assert [a['backend_executed']for a in attempts]==[False,True]
        assert not model.replies and 'TCK-1003'not in engine.port.environment.get_state()['tickets']
    finally:engine.close()


def test_cancellation_closes_the_goal_and_cached_result_restores_its_identity(tmp_path):
    engine,model=setup(tmp_path,[*refusal(),call('RequestBoundary',{
        'relation':'cancel','facts':[],'acknowledgement':'The pending request is cancelled.'})])
    try:
        engine.invoke('I am agent.jsmith. Delete TCK-1001.','first')
        cancelled=engine.invoke('Cancel the request.','second')
        assert cancelled.status=='completed'and engine.active_goal.status=='cancelled'
        assert engine.invoke('Cancel the request.','second')==cancelled
        assert engine.active_goal is not None and len(engine.port.audit)==1 and not model.replies
    finally:engine.close()


def test_request_result_and_goal_terminal_commit_roll_back_together(tmp_path,monkeypatch):
    model=ScriptModel(replies=[call('task',{'subagent_type':'service','description':'Set Closed.'}),
        call('change',{'value':'Closed'}),*finish('Changed.'),*finish('Changed.'),*finish('Already changed.')])
    port=Port();engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one')
    original=engine.goals.save
    def unavailable(episode,**kwargs):
        if episode.result is not None:raise OSError('Injected goal commit failure')
        return original(episode,**kwargs)
    monkeypatch.setattr(engine.goals,'save',unavailable)
    try:
        with pytest.raises(OSError):engine.invoke('Set the status to Closed.','same')
        assert port.writes==1
        assert engine.journal.db.execute('SELECT result FROM requests WHERE id=?',('same',)).fetchone()[0]is None
        assert engine.goals.for_request('same').status=='active'
        monkeypatch.setattr(engine.goals,'save',original)
        assert engine.invoke('Set the status to Closed.','same').status=='completed'
        assert port.writes==1 and not model.replies
    finally:engine.close()


@pytest.mark.parametrize('invalid', ['empty_reconciliation','read_receipt_as_deletion'])
def test_native_progress_schema_repairs_invalid_decisions_before_authorized_deletion(tmp_path,invalid):
    def faulty_decision(messages):
        payload=json.loads(messages[-1].content)
        assert not payload['ledger']['uncertain']
        if invalid=='empty_reconciliation':
            return call('ProgressDecision',{'is_request_satisfied':False,'is_progress_being_made':False,
                'is_in_loop':False,'next_action':'reconcile','instruction':'Reconcile before deleting.'})
        evidence=payload['ledger']['recent_observations'][0]['evidence_id']
        return call('ProgressDecision',{'is_request_satisfied':True,'is_progress_being_made':True,
            'is_in_loop':False,'next_action':'stop','instruction':'Deletion is complete.',
            'final_result':{'status':'completed','answer':'Deleted.','evidence_ids':[evidence]}})
    replan=call('ProgressDecision',{'is_request_satisfied':False,'is_progress_being_made':True,
        'is_in_loop':False,'next_action':'replan','next_agent':'record_safety__deletion',
        'instruction':'Delete TCK-1003 for admin.rlee after the required checks.'})
    engine,model=setup(tmp_path,[*finish('Please provide your identity.','needs_user_input'),
        boundary('admin.rlee'),requirement('delete_ticket',{'ticket_id':'TCK-1003'}),
        call('check_permission',{'user_id':'admin.rlee','action':'delete','ticket_id':'TCK-1003'}),
        *finish('I will perform the deletion.','partial'),faulty_decision,replan,*deletion('TCK-1003',delegate=False)])
    try:
        engine.invoke('Delete TCK-1003 after the required checks.','first')
        result=engine.invoke('My identity is admin.rlee. Please proceed.','second')
        assert result.status=='completed'and 'TCK-1003'not in engine.port.environment.get_state()['tickets']
        assert sum(a['name']=='delete_ticket'and a['backend_executed']for a in engine.port.audit)==1
        message='Input should be'if invalid=='empty_reconciliation'else'no matching actual receipt'
        assert any(message in str(messages)for messages in model.seen)
        assert engine.control_passes==1 and not model.replies
    finally:engine.close()


def test_receipt_matching_requires_requested_arguments_and_post_write_verification(tmp_path):
    from kb.harness.goals import GoalEpisode,GoalRequirements
    from kb.harness.journal import Journal
    port=Port();specs={s.name:s for s in port.tools()}
    goal=GoalEpisode(id='g',original_request='Set Closed and verify it afterwards.',
        updates=[{'request_id':'r','text':'Set Closed and verify it afterwards.'}],
        requirements=GoalRequirements(kind='business_operations',actions=[
            {'tool':'change','arguments':{'value':'Closed'}},
            {'tool':'lookup','after':[0]}]))
    journal=Journal(tmp_path/'proofs.sqlite3','one')
    try:
        journal.invoke(specs['lookup'],{},'r',port,lambda *x:None)
        journal.invoke(specs['change'],{'value':'Pending'},'r',port,lambda *x:None)
        assert [a.tool for a in goal.unproven_actions(journal.records(),specs)]==['change','lookup']
        journal.invoke(specs['change'],{'value':'Closed'},'r',port,lambda *x:None)
        assert [a.tool for a in goal.unproven_actions(journal.records(),specs)]==['lookup']
        journal.invoke(specs['lookup'],{},'r',port,lambda *x:None)
        assert not goal.unproven_actions(journal.records(),specs)
    finally:journal.close()


def test_generated_read_check_order_does_not_reject_a_completed_write(tmp_path):
    from kb.harness.goals import GoalEpisode,GoalRequirements
    from kb.harness.contracts import ToolSpec
    names=('permission','dependencies','details','delete')
    specs={name:ToolSpec(name,name,{},'service',name=='delete')for name in names}
    goal=GoalEpisode(id='g',original_request='Delete after the required checks.',updates=[],
        requirements=GoalRequirements(kind='business_operations',actions=[
            {'tool':'permission'},{'tool':'dependencies','after':[0]},
            {'tool':'details','after':[1]},{'tool':'delete','after':[2]}]))
    # Details were read first; the permission and dependency observations
    # both precede the successful write, although their plan order differs.
    records=[{'id':name,'name':name,'status':'committed'if name=='delete'else'observed',
        'arguments':{},'created':created}for name,created in
        [('details',1),('dependencies',2),('permission',3),('delete',4)]]
    assert not goal.unproven_actions(records,specs)
    records[-1]['status']='unknown'
    assert [a.tool for a in goal.unproven_actions(records,specs)]==['delete']


def test_committed_backend_receipt_overrides_model_added_prerequisite_reads(tmp_path):
    from kb.harness.goals import GoalEpisode,GoalRequirements
    port=Port();specs={s.name:s for s in port.tools()}
    goal=GoalEpisode(id='g',original_request='Set Closed.',updates=[],
        requirements=GoalRequirements(kind='business_operations',actions=[
            {'tool':'lookup'},{'tool':'change','arguments':{'value':'Closed'},'after':[0]}]))
    records=[{'id':'commit','name':'change','arguments':{'value':'Closed'},'status':'committed','created':1}]
    assert not goal.unproven_actions(records,specs)
    records[0]['status']='unknown'
    assert [a.tool for a in goal.unproven_actions(records,specs)]==['change']


def test_compiled_goal_blocks_an_unrequested_write_and_retains_a_rejected_attempt(tmp_path):
    model=ScriptModel(replies=[*finish('Which status?','needs_user_input'),
        call('RequestBoundary',{'relation':'continue','facts':[
            {'parameter':'value','value':'Closed','quote':'Closed'}]}),requirement('change',{'value':'Closed'}),
        call('task',{'subagent_type':'service','description':'Change status to Closed.'}),
        call('change',{'value':'Open'},'wrong'),call('change',{'value':'Closed'},'right'),
        *finish('Changed to Closed.'),*finish('The status is Closed.')])
    port=Port();engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one')
    try:
        engine.invoke('Change the status.','first')
        assert engine.invoke('Set it to Closed.','second').status=='completed'
        assert port.value=='Closed'and port.writes==1
        rejected=next(a for a in engine.attempts if a['id']=='wrong')
        assert json.loads(rejected['result'])['error']=='outside_requested_goal'
        assert rejected['backend_executed']is False and rejected['ok']is False
        assert not model.replies
    finally:engine.close()


def test_exhausted_continuations_report_unfinished_work_instead_of_a_false_completed_write(tmp_path):
    replan=call('ProgressDecision',{'is_request_satisfied':False,'is_progress_being_made':True,
        'is_in_loop':False,'next_action':'replan','next_agent':'service','instruction':'Perform the requested status change.'})
    model=ScriptModel(replies=[*finish('Which status?','needs_user_input'),
        call('RequestBoundary',{'relation':'continue','facts':[
            {'parameter':'value','value':'Closed','quote':'Closed'}]}),requirement('change',{'value':'Closed'}),
        *finish('Status changed to Closed.'),replan,*finish('Still unfinished.','partial'),
        *finish('Status changed to Closed.'),replan,*finish('Still unfinished.','partial'),*finish('Status changed to Closed.')])
    port=Port();engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one')
    try:
        engine.invoke('Change the status.','first')
        result=engine.invoke('Set it to Closed.','second')
        assert result.status=='partial'and result.remaining_work
        assert 'incomplete'in result.answer and 'Status changed'not in result.answer
        assert port.writes==0 and port.value=='Open'and engine.control_passes==2
        assert not model.replies
    finally:engine.close()


@pytest.mark.parametrize('progress_exit',[False,True])
def test_proven_completion_discards_a_phantom_confirmation_without_repeating_the_write(tmp_path,progress_exit):
    phantom=call('CompletionMetadata',{'status':'completed','evidence_ids':[],
        'remaining_work':[{'kind':'deferred_user_action','description':'Confirm the status change.'}]})
    replies=[*finish('Which status?','needs_user_input'),
        call('RequestBoundary',{'relation':'continue','facts':[
            {'parameter':'value','value':'Closed','quote':'Closed'}]}),requirement('change',{'value':'Closed'}),
        call('task',{'subagent_type':'service','description':'Change status to Closed.'}),
        call('change',{'value':'Closed'}),finish('The status is Closed.')[0],phantom,
        finish('The status is Closed.')[0]]
    if progress_exit:
        replies.extend([call('CompletionMetadata',{'status':'partial','evidence_ids':[],
            'remaining_work':[{'kind':'agent_action','description':'Confirm the status change.'}]}),
            call('ProgressDecision',{'is_request_satisfied':True,'is_progress_being_made':True,
                'is_in_loop':False,'next_action':'stop','instruction':'The change is proven by its committed receipt.',
                'final_result':{'status':'completed','answer':'The status is Closed.',
                    'deferred_user_actions':['Confirm the status change.']}})])
    else:replies.append(phantom)
    model=ScriptModel(replies=replies);port=Port()
    engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one')
    try:
        engine.invoke('Change the status.','first')
        result=engine.invoke('Set it to Closed.','second')
        assert result.status=='completed'and not result.deferred_user_actions
        assert port.value=='Closed'and port.writes==1 and not model.replies
        events=[json.loads(line)for line in (tmp_path/'events.jsonl').read_text().splitlines()]
        assert any(e['kind']=='unverified_handoff_discarded'for e in events)
    finally:engine.close()


@pytest.mark.parametrize('misindexed',[False,True])
def test_goal_extraction_can_use_actual_api_receipts_without_the_proposed_completion_answer(tmp_path,misindexed):
    def extract(messages):
        payload=json.loads(messages[-1].content)
        assert list(payload['observed_operations'].values())==[{'tool':'lookup','arguments':{},'receipt_status':'observed'}]
        assert next(iter(payload['observed_operations'])).startswith('effect-')
        assert 'agent_answer'not in payload and 'proposed_result'not in payload
        return call('GoalRequirements',{'kind':'business_operations','actions':[
            {'tool':'lookup','after':[0]if misindexed else[]}]})
    model=ScriptModel(replies=[call('lookup',{}),finish('The status is Open.')[0],
        call('CompletionMetadata',{'status':'partial','evidence_ids':[],
            'remaining_work':[{'kind':'agent_action','description':'Confirm the status.'}]}),extract,
            *([requirement('lookup')]if misindexed else[]),call('ProgressDecision',{
                'is_request_satisfied':True,'is_progress_being_made':True,'is_in_loop':False,
                'next_action':'stop','instruction':'The current status was observed.',
                'final_result':{'status':'completed','answer':'The status is Open.'}})])
    port=Port();engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one')
    try:
        result=engine.invoke('What is the current status?')
        assert result.status=='completed'and not result.deferred_user_actions
        assert port.writes==0 and len(engine.journal.records())==1 and not model.replies
        if misindexed:assert any('The first action must have after=[]'in str(messages)for messages in model.seen)
    finally:engine.close()


@pytest.mark.parametrize('read_first',[False,True])
def test_future_user_choices_do_not_launch_new_agent_work_or_a_goal_compiler(tmp_path,read_first):
    replies=[*([call('lookup',{})]if read_first else[]),finish('The requested result is ready.')[0],
        call('CompletionMetadata',{'status':'completed','evidence_ids':[],
            'remaining_work':[{'kind':'deferred_user_action','description':'Decide whether to authorize a later change.'}]})]
    model=ScriptModel(replies=replies);port=Port()
    engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one')
    try:
        result=engine.invoke('Tell me the current status.'if read_first else'Do not change anything unless I authorize it.')
        assert result.status=='completed'and port.writes==0 and not model.replies
        assert engine.active_goal.requirements is None and engine.control_passes==0
        assert engine.calls==(3 if read_first else 2)
    finally:engine.close()
