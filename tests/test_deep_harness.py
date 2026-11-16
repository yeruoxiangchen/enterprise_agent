"""Actual upstream graphs/middlewares, durable effects and memory lifecycle."""
import json
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from kb.harness.contracts import ToolSpec, ToolOutcome, WorkflowCard
from kb.harness.engine import DeepHarness
from kb.harness.journal import Journal
from kb.harness.memory import WorkflowStore, export_episode


class ScriptModel(BaseChatModel):
    replies: list
    seen: list = Field(default_factory=list)
    bindings: list = Field(default_factory=list)
    @property
    def _llm_type(self):return 'deep-harness-integration'
    def bind_tools(self,tools,**kwargs):
        self.bindings.append([getattr(t,'name',str(t))for t in tools]);return self
    def _generate(self,messages,**kwargs):
        self.seen.append(messages)
        item=self.replies.pop(0)
        msg=item(messages) if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


def call(name,args,ident='call'):
    return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':ident}],
                     usage_metadata={'input_tokens':11,'output_tokens':4,'total_tokens':15})


def done(answer='Done.',status='completed'):
    return call('CompletionMetadata',{'status':status,'evidence_ids':[],'remaining_work':[]},'final')


def finish(answer='Done.',status='completed'):
    return [AIMessage(content=answer,usage_metadata={'input_tokens':11,'output_tokens':4,'total_tokens':15}),
            done(answer,status)]


class Port:
    policy='Use real tools. Change only when the user requests a change.'
    def __init__(self):self.value='Open';self.writes=0;self.lose_ack=False
    def tools(self):
        return [ToolSpec('lookup','Read current status.',{'type':'object','properties':{}},'service'),
                ToolSpec('change','Change status.',{'type':'object','properties':{'value':{'type':'string'}},
                         'required':['value']},'service',True)]
    def version(self):return self.value
    def execute(self,name,args,operation_id):
        if name=='lookup':return ToolOutcome({'status':self.value})
        self.value=args['value'];self.writes+=1
        if self.lose_ack:raise TimeoutError('acknowledgement lost after commit')
        return ToolOutcome({'status':self.value},'committed')
    def reconcile(self,name,args,operation_id):
        return ToolOutcome({'status':self.value},'committed' if self.value==args['value'] else 'unknown')


def test_native_direct_read_and_idempotent_request_resume(tmp_path):
    model=ScriptModel(replies=[call('lookup',{}),*finish('The status is Open.')]);port=Port()
    engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one')
    result=engine.invoke('Is there any update on the status?',request_id='request')
    assert result.status=='completed' and port.writes==0 and engine.calls==3
    engine.close()
    resumed=DeepHarness(model=ScriptModel(replies=[]),port=port,workspace=tmp_path,namespace='one')
    assert resumed.invoke('Is there any update on the status?',request_id='request')==result
    assert resumed.calls==0
    with pytest.raises(ValueError,match='identity changed'):resumed.invoke('Different request','request')
    resumed.close()


@pytest.mark.parametrize('kind,expected',[
    ('agent_action','partial'),('missing_user_input','needs_user_input'),('deferred_user_action','completed')])
def test_completion_distinguishes_agent_work_missing_input_and_future_user_decisions(tmp_path,kind,expected):
    model=ScriptModel(replies=[finish('The observed result is available.')[0],
        call('CompletionMetadata',{'status':'partial','evidence_ids':[],
            'remaining_work':[{'kind':kind,'description':'A follow-up step.'}]})])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one',execution_control=False)
    result=engine.invoke('Explain the available result and who owns any follow-up.')
    assert result.status==expected
    assert result.deferred_user_actions==(['A follow-up step.']if kind=='deferred_user_action'else[])
    assert result.remaining_work==([]if kind=='deferred_user_action'else['A follow-up step.'])
    engine.close()


def test_rejected_concurrent_request_cannot_change_the_inflight_request_identity(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered,release=threading.Event(),threading.Event()
    def wait(messages):
        entered.set()
        assert release.wait(10)
        return call('lookup',{})
    engine=DeepHarness(model=ScriptModel(replies=[wait,*finish('The status is Open.')]),
        port=Port(),workspace=tmp_path,namespace='one')
    try:
        with ThreadPoolExecutor(max_workers=1)as pool:
            first=pool.submit(engine.invoke,'Read the status.','first')
            assert entered.wait(10)
            try:
                with pytest.raises(BlockingIOError):engine.invoke('Another question.','second')
                assert engine.request_id=='first'
            finally:release.set()
            assert first.result(timeout=20).status=='completed'
        assert {e['request_id']for e in engine.journal.records()}=={'first'}
    finally:release.set();engine.close()


def test_real_subagent_middleware_isolates_history_and_returns_structured_result(tmp_path):
    model=ScriptModel(replies=[call('task',{'description':'Read current status with lookup.','subagent_type':'service'},'delegate'),
        call('lookup',{},'read'),*finish('Open.'),*finish('The status is Open.')])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one')
    result=engine.invoke('Please have the service specialist check status.',history=[
        {'role':'user','content':'UNRELATED-HISTORY-ONLY'}])
    assert result.status=='completed' and engine.calls==6
    assert 'UNRELATED-HISTORY-ONLY' in str(model.seen[0])
    assert 'UNRELATED-HISTORY-ONLY' not in str(model.seen[1])
    assert engine.prompt_tokens==66 and engine.completion_tokens==24
    assert len(engine.journal.records())==1
    engine.close()


def test_ack_loss_reconciles_across_restart_without_second_mutation(tmp_path):
    port=Port();port.lose_ack=True
    journal=Journal(tmp_path/'effects.sqlite3','conversation');spec=port.tools()[1]
    first=journal.invoke(spec,{'value':'Closed'},'request',port,lambda *x:None)
    assert first['status']=='unknown' and port.writes==1
    journal.close();journal=Journal(tmp_path/'effects.sqlite3','conversation')
    repeated=journal.invoke(spec,{'value':'Closed'},'request',port,lambda *x:None)
    assert repeated['status']=='unknown' and port.writes==1
    assert journal.reconcile(first['evidence_id'],port,lambda *x:None)['status']=='committed'
    assert journal.invoke(spec,{'value':'Closed'},'request',port,lambda *x:None)['reused']
    assert port.writes==1
    journal.close()


def card(source='train-a'):
    return WorkflowCard(title='Change status with verification',applicability='When the user requests a status change and verification.',
        slots=['status'],steps=[{'description':'Read status','tool':'lookup','arguments':{}},
          {'description':'Change the requested status','tool':'change','arguments':{'value':'$status'},'depends_on':[0]}],
        exception_branches=['If acknowledgement is lost, reconcile before retrying.'],
        stop_conditions=['Stop after the requested state is observed.'],source_trace_ids=[source])


def test_candidate_requires_independent_paired_validation_and_revision_retires_old_release(tmp_path):
    store=WorkflowStore(tmp_path/'memory.sqlite3')
    ident=store.add(card(),['lookup','change'],['train-a'])
    assert not store.search('change status',{'lookup','change'})
    with pytest.raises(ValueError,match='independent'):store.validate(ident,[{'id':'train-a'}],lambda *x:None)
    report=store.validate(ident,[{'id':'validation-b'}],lambda case,memory:{
        'passed':bool(memory),'tokens':10 if memory else 20,'policy_violations':0,
        'workflow_ids':[ident]if memory else[]})
    assert report['promotion_passed']
    assert store.search('change status',{'lookup','change'})[0]['id']==ident
    revised=card().model_copy(update={'stop_conditions':['Stop only after reading back the requested state.']})
    store.add(revised,['lookup','change'],['train-a'],ident)
    assert not store.search('change status',{'lookup','change'})
    assert len(store.db.execute('SELECT * FROM versions').fetchall())==2
    store.close()


def test_harmful_candidate_does_not_activate(tmp_path):
    store=WorkflowStore(tmp_path/'memory.sqlite3');ident=store.add(card(),['lookup','change'],['train-a'])
    report=store.validate(ident,[{'id':'validation-b'}],lambda case,memory:{
        'passed':True,'tokens':1 if memory else 20,'policy_violations':int(bool(memory))})
    assert not report['promotion_passed'] and not store.entries('active')
    store.close()


def test_actual_langmem_extraction_produces_a_candidate_from_exported_episode(tmp_path):
    store=WorkflowStore(tmp_path/'memory.sqlite3')
    model=ScriptModel(replies=[call('WorkflowCard',card().model_dump(),'induced'),call('Done',{},'consolidated')])
    episode=export_episode('train-a',[{'role':'user','content':'Change the current status.'}],[],
                           {'completed':True,'expected_actions':['DO NOT EXPORT']})
    assert 'expected_actions' not in json.dumps(episode)
    ids=store.induce(model,[episode],['lookup','change'])
    assert len(ids)==1 and store.entries()[0]['status']=='candidate'
    assert model.seen and 'Change the current status' in str(model.seen)
    store.close()


def test_langmem_repairs_invalid_slot_with_upstream_jsonpatch(tmp_path):
    from langchain_core.messages import ToolMessage
    bad=card().model_dump();bad['steps'][1]['arguments']['value']='$undeclared_status'
    def repair(messages):
        failed=next(m for m in reversed(messages)if isinstance(m,ToolMessage))
        return call('PatchFunctionErrors',{'json_doc_id':failed.tool_call_id,
            'planned_edits':'Replace the undeclared parameter with the declared status slot.',
            'patches':[{'op':'replace','path':'/steps/1/arguments/value','value':'$status'}]},'patch')
    model=ScriptModel(replies=[call('WorkflowCard',bad,'induced'),repair,call('Done',{},'consolidated')])
    store=WorkflowStore(tmp_path/'memory.sqlite3')
    episode=export_episode('train-a',[{'role':'user','content':'Change status and verify it.'}],[],{'completed':True})
    ids=store.induce(model,[episode],['lookup','change'])
    assert len(ids)==1 and store.entries()[0]['card'].steps[1].arguments['value']=='$status'
    assert any('undeclared parameter slot'in str(messages)for messages in model.seen)
    store.close()


def test_workflow_rejects_benchmark_identity_and_unknown_sources(tmp_path):
    store=WorkflowStore(tmp_path/'memory.sqlite3')
    with pytest.raises(ValueError,match='source'):store.add(card('test-a'),['lookup','change'],['train-a'])
    bad=card().model_copy(update={'title':'Always modify TCK-1004'})
    with pytest.raises(ValueError,match='identity'):store.add(bad,['lookup','change'],['train-a'])
    store.close()


def test_exhausted_execution_returns_actual_progress_decision_not_spurious_clarification(tmp_path):
    model=ScriptModel(replies=[call('lookup',{}),call('ProgressDecision',{
        'is_request_satisfied':False,'is_progress_being_made':False,'is_in_loop':True,
        'next_action':'stop','instruction':'The lookup succeeded; the requested change remains unfinished.'})])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one',max_calls=7)
    result=engine.invoke('Read and change the status.')
    assert result.status=='partial' and engine.calls==2
    assert result.remaining_work and engine.journal.records()[0]['name']=='lookup'
    engine.close()


def test_progress_review_can_deliver_evidence_grounded_completed_result(tmp_path):
    def conclude(messages):
        ledger=json.loads(messages[-1].content)['ledger']
        ident=ledger['recent_observations'][0]['evidence_id']
        return call('ProgressDecision',{'is_request_satisfied':True,'is_progress_being_made':True,
            'is_in_loop':False,'next_action':'stop','instruction':'The status was observed.',
            'final_result':{'status':'completed','answer':'The current status is Open.',
                            'evidence_ids':[ident],'remaining_work':[]}})
    model=ScriptModel(replies=[call('lookup',{}),conclude])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one',max_calls=7)
    result=engine.invoke('What is the current status?')
    assert result.status=='completed' and result.evidence_ids
    engine.close()


def test_budget_recovery_executes_the_model_selected_native_specialist_without_an_extra_plan_call(tmp_path):
    model=ScriptModel(replies=[call('lookup',{}),call('ProgressDecision',{
        'is_request_satisfied':False,'is_progress_being_made':True,'is_in_loop':False,
        'next_action':'replan','next_agent':'service',
        'instruction':'Set the requested status to Closed using change.'}),
        call('change',{'value':'Closed'}),*finish('Changed to Closed.'),*finish('The status is Closed.')])
    port=Port();engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one',max_calls=7)
    try:
        result=engine.invoke('Read the status, then set it to Closed.')
        assert result.status=='completed'and port.value=='Closed'and port.writes==1
        assert engine.calls==7 and not model.replies
        messages=engine.agent.get_state({'configurable':{'thread_id':'one'}}).values['messages']
        assert any(m.additional_kwargs.get('lc_source')=='model_progress_routing'for m in messages)
    finally:engine.close()


def test_replanning_does_not_replace_the_original_completion_goal(tmp_path):
    question='Read the current status and explain it to me.'
    def metadata(messages):
        payload=json.loads(messages[-1].content)
        assert payload['task_request']==question
        assert payload['business_policy']==Port.policy
        return done()
    model=ScriptModel(replies=[call('lookup',{}),finish('Status Open.')[0],
        call('ProgressDecision',{'is_request_satisfied':False,'is_progress_being_made':True,
            'is_in_loop':False,'next_action':'replan','instruction':'Finish from the existing observation.'}),
        finish('The observed status is Open.')[0],metadata])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one',max_calls=8)
    result=engine.invoke(question)
    assert result.status=='completed'and len(engine.journal.records())==1
    engine.close()


def test_stalled_child_returns_model_progress_review_to_actual_supervisor(tmp_path):
    model=ScriptModel(replies=[call('task',{'description':'Look up the current status.','subagent_type':'service'}),
        call('lookup',{},'read-1'),call('lookup',{},'read-2'),call('lookup',{},'read-3'),
        call('ProgressDecision',{'is_request_satisfied':True,'is_progress_being_made':False,
            'is_in_loop':True,'next_action':'stop','instruction':'Status Open was already observed.',
            'final_result':{'status':'completed','answer':'Open.','evidence_ids':[],'remaining_work':[]}}),
        *finish('The status is Open.')])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one')
    result=engine.invoke('Look up the current status.')
    assert result.status=='completed' and engine.calls==7
    assert len(engine.journal.records())==2
    assert '"status":"completed"'in str(model.seen[-2])
    engine.close()


def test_workflow_tool_templates_must_match_real_tool_schema(tmp_path):
    store=WorkflowStore(tmp_path/'memory.sqlite3')
    catalogue={s.name:s.parameters for s in Port().tools()}
    bad=card().model_dump()
    bad['steps'][1]['arguments']={'wrong_parameter':'$status'}
    with pytest.raises(ValueError,match='arguments do not match'):
        store.add(bad,catalogue,['train-a'])
    assert store.add(card(),catalogue,['train-a'])
    store.close()


def test_unused_memory_cannot_claim_a_paired_gain(tmp_path):
    store=WorkflowStore(tmp_path/'memory.sqlite3');ident=store.add(card(),['lookup','change'],['train-a'])
    report=store.validate(ident,[{'id':'validation-b'}],lambda case,memory:{
        'passed':True,'tokens':10 if memory else 20,'policy_violations':0,'workflow_ids':[]})
    assert not report['promotion_passed'] and report['workflow_used_cases']==0
    store.close()


def test_supervisor_cannot_execute_an_unadvertised_write_through_gateway(tmp_path):
    model=ScriptModel(replies=[call('change',{'value':'Closed'},'unscoped'),*finish('The change needs a service specialist.','partial'),
        call('GoalRequirements',{'kind':'business_operations','actions':[{'tool':'change','arguments':{'value':'Closed'},'after':[]}]}),
        call('ProgressDecision',{'is_request_satisfied':False,'is_progress_being_made':False,
            'is_in_loop':True,'next_action':'stop','instruction':'The change remains incomplete.'})])
    port=Port();engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one')
    result=engine.invoke('Please change the status to Closed.')
    assert port.writes==0 and result.status=='partial'
    assert not engine.journal.records()
    assert 'not a valid tool'in str(model.seen[1])
    engine.close()


def test_shared_tool_evidence_reaches_isolated_child_without_replacing_user_request(tmp_path):
    def child_answer(messages):
        assert '"tool":"lookup"'in messages[0].content and '"status":"Open"'in messages[0].content
        assert 'UNRELATED-HISTORY-ONLY'not in str(messages)
        return finish('The observed status is Open.')[0]
    question='Read the current status and ask the service specialist to explain it.'
    model=ScriptModel(replies=[call('lookup',{}),call('task',{
        'description':'Explain the result already observed.','subagent_type':'service'}),child_answer,
        done('The observed status is Open.'),*finish('The observed status is Open.')])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one')
    assert engine.invoke(question,history=[{'role':'user','content':'UNRELATED-HISTORY-ONLY'}]).status=='completed'
    assert model.seen[0][-1].content==question
    assert len(engine.journal.records())==1
    engine.close()


def test_native_rubric_revises_missing_deliverable_before_finishing(tmp_path):
    criterion='Explain status and identify its observation source'
    model=ScriptModel(replies=[call('lookup',{}),*finish('Open.'),call('GraderResponse',{
        'result':'needs_revision','explanation':'The source was omitted.',
        'criteria':[{'name':criterion,'passed':False,'gap':'Explain that lookup observed the status.'}]}),
        *finish('The observed status is Open, according to lookup.'),call('GraderResponse',{
        'result':'satisfied','explanation':'The requested explanation and source are supplied.',
        'criteria':[{'name':criterion,'passed':True}]})])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one',goal_review=True)
    result=engine.invoke('Explain the current status and mention its observation source.')
    assert result.status=='completed' and 'according to lookup'in result.answer and engine.calls==7
    assert engine.agent.get_state({'configurable':{'thread_id':'one'}}).values['_rubric_status']=='satisfied'
    assert len(engine.journal.records())==1
    engine.close()


def test_schema_finishing_calls_do_not_count_as_repeated_business_actions(tmp_path):
    replies=[]
    for i in range(3):
        replies.extend([call('task',{'description':f'Explain item {i}.','subagent_type':'service'},f'task-{i}'),
                        *finish(f'Explained item {i}.')])
    replies.extend(finish('All three explanations are complete.'))
    engine=DeepHarness(model=ScriptModel(replies=replies),port=Port(),workspace=tmp_path,namespace='one')
    assert engine.invoke('Explain three static items.').status=='completed'
    assert engine.calls==11
    engine.close()


def test_schema_finishing_rejects_unobserved_identifier_and_asks_model_to_correct(tmp_path):
    model=ScriptModel(replies=[call('lookup',{}),finish('The status is Open.')[0],
        call('CompletionMetadata',{'status':'completed','evidence_ids':['TCK-1001'],'remaining_work':[]}),done()])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one')
    assert engine.invoke('What is the current status?').status=='completed'
    assert engine.calls==4 and len(engine.journal.records())==1
    engine.close()


def test_native_rubric_pauses_when_user_information_is_needed(tmp_path):
    engine=DeepHarness(model=ScriptModel(replies=finish('Which status should I use?','needs_user_input')),
        port=Port(),workspace=tmp_path,namespace='one',goal_review=True)
    assert engine.invoke('Change the status.').status=='needs_user_input'
    assert engine.calls==2
    engine.close()


def test_selected_procedure_reaches_model_without_an_optional_lookup_call(tmp_path):
    class Memory:
        def __init__(self):
            self.card={'id':'learned-procedure','revision':1,**card().model_dump()};self.queries=[]
        def search(self,query,tools):self.queries.append(query);return[self.card]
    memory=Memory();port=Port()
    model=ScriptModel(replies=[call('lookup',{}),*finish('The status is Open.')])
    engine=DeepHarness(model=model,port=port,workspace=tmp_path,namespace='one',memory=memory)
    assert engine.invoke('Read the current status.').status=='completed'
    assert memory.queries==['Read the current status.'] and port.writes==0
    content=model.seen[0][0].content
    content=content if isinstance(content,str)else ''.join(b.get('text','')for b in content)
    assert 'Change status with verification'in content
    assert '"execution_scopes":["service"]'in content
    assert 'train-a'not in content
    events=[json.loads(line)for line in(tmp_path/'events.jsonl').read_text().splitlines()]
    assert any(e['kind']=='workflow_context'and e['data']['ids']==['learned-procedure']for e in events)
    engine.close()
