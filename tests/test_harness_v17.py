import json

from langchain_core.messages import AIMessage,ToolMessage

from kb.harness.contracts import TaskResult
from kb.harness.goals import GoalRequirements,RequestedAction
from kb.harness.engine import DeepHarness
from test_deep_harness import ScriptModel,Port,call,done
from test_goal_control import setup


def test_native_specialist_report_saves_one_model_call_and_retains_actual_receipts(tmp_path):
    model=ScriptModel(replies=[call('task',{'description':'Read the current status.','subagent_type':'service'}),
        call('lookup',{}),AIMessage(content='The status is Open.'),
        AIMessage(content='The status is Open.'),done()])
    engine=DeepHarness(model=model,port=Port(),workspace=tmp_path,namespace='one',compact_handoffs=True)
    try:
        result=engine.invoke('Have the specialist look up the status.')
        assert result.status=='completed'and engine.calls==5
        messages=[m for m in model.seen[-2]if isinstance(m,ToolMessage)]
        report=next(json.loads(m.content)for m in messages if 'specialist_report'in m.content)
        assert report['observations'][0]['name']=='lookup'
        assert report['observations'][0]['status']=='observed'
        assert report['observations'][0]['id']==engine.journal.records()[0]['id']
    finally:engine.close()


def terminal_engine(tmp_path,*,extra=False):
    engine,_=setup(tmp_path,[]);engine.request_id='r'
    engine.active_goal=engine.goals.fresh('I am admin.rlee. Delete TCK-1003.','r')
    actions=[RequestedAction(tool='delete_ticket',arguments={'ticket_id':'TCK-1003'})]
    if extra:actions.append(RequestedAction(tool='get_ticket',arguments={'ticket_id':'TCK-1004'},after=[0]))
    engine.active_goal.requirements=GoalRequirements(kind='business_operations',actions=actions,communication_requirements=[])
    port=engine.port;port.begin_request('I am admin.rlee. Delete TCK-1003.','r')
    for name,args in [('check_dependencies',{'ticket_id':'TCK-1003'}),
            ('check_permission',{'user_id':'admin.rlee','action':'delete','ticket_id':'TCK-1003'}),
            ('delete_ticket',{'ticket_id':'TCK-1003'})]:
        engine.journal.invoke(engine.specs[name],args,'r',port,lambda *a:None)
    return engine


def test_committed_goal_closes_even_when_metadata_invents_a_further_user_question(tmp_path):
    engine=terminal_engine(tmp_path)
    try:
        result,_=engine._control_result(TaskResult(status='needs_user_input',answer='Deleted. Need anything else?',
            remaining_work=['Please confirm if further assistance is needed.']))
        assert result.status=='completed'and not result.remaining_work
        assert engine.calls==0
        assert 'TCK-1003'not in engine.port.environment.get_state()['tickets']
    finally:engine.close()


def test_real_unfinished_post_write_read_remains_pending(tmp_path):
    engine=terminal_engine(tmp_path,extra=True)
    try:
        result,_=engine._control_result(TaskResult(status='needs_user_input',answer='Which additional detail?',
            remaining_work=['Need a required detail.']))
        assert result.status=='needs_user_input'and result.remaining_work
    finally:engine.close()


def test_a_separate_explanation_is_not_closed_by_a_write_receipt(tmp_path):
    engine=terminal_engine(tmp_path)
    try:
        engine.active_goal.requirements.communication_requirements=['Explain the change in the requested language.']
        result,_=engine._control_result(TaskResult(status='needs_user_input',answer='Which language should I use?',
            remaining_work=['The requested explanation needs a language choice.']))
        assert result.status=='needs_user_input'and result.remaining_work
    finally:engine.close()


def test_http_application_uses_compact_handoff_with_real_document_citations(system,monkeypatch):
    import hashlib
    from kb.agent import Harness
    from test_application import upload,login
    from test_deep_application import configure
    cfg,db,web,base,*_=system
    upload(system,'VPN连接故障先检查网络与客户端配置。','VPN.md')
    chunk=db.one('SELECT * FROM chunks LIMIT 1')
    label='S'+str(int(hashlib.sha256(str(chunk['id']).encode()).hexdigest()[:8],16))
    model=ScriptModel(replies=[call('task',{'description':'检索VPN连接故障处理方法。','subagent_type':'evidence'}),
        call('search_documents',{'query':'VPN连接故障'}),AIMessage(content=f'先检查网络与客户端配置 [{label}]。'),
        AIMessage(content=f'先检查网络与客户端配置 [{label}]。'),done()])
    configure(cfg,monkeypatch,model);cfg.harness_compact_handoffs=True;login(web)
    run=web.post('/api/chat',json={'question':'VPN连接故障怎么处理？','base_ids':[base]}).json()['run_id']
    Harness(db,cfg).execute(run)
    result=web.get('/api/runs/'+run).json()
    assert result['status']=='answered',result
    assert result['citations'][0]['label']==label
    assert any(event['kind']=='specialist_report'for event in result['events'])
    assert len(model.seen)==5


def test_terminal_business_state_rejection_closes_without_replanning(tmp_path):
    engine,_=setup(tmp_path,[]);engine.request_id='r'
    try:
        args={key:('TCK-1004'if key=='ticket_id'else'admin.rlee')
            for key in engine.specs['reassign_ticket'].parameters['properties']}
        engine.active_goal=engine.goals.fresh('Reassign TCK-1004 to admin.rlee.','r')
        engine.active_goal.requirements=GoalRequirements(kind='business_operations',
            actions=[RequestedAction(tool='reassign_ticket',arguments=args)],communication_requirements=[])
        engine.port.begin_request('Reassign TCK-1004 to admin.rlee.','r')
        engine.journal.invoke(engine.specs['get_ticket'],{'ticket_id':'TCK-1004'},'r',engine.port,lambda *a:None)
        effect=engine.journal.invoke(engine.specs['reassign_ticket'],args,'r',engine.port,lambda *a:None)
        assert effect['value']['retryable']is False
        result,_=engine._control_result(TaskResult(status='partial',answer='The ticket is unassigned.',
            remaining_work=['Retry reassignment.']))
        assert result.status=='refused'and 'unassigned'in result.answer
        assert engine.calls==0 and engine.control_passes==0
        assert engine.port.environment.get_state()['tickets']['TCK-1004']['assigned_to']==''
    finally:engine.close()
