"""Actual HTTP/DB application surface on the same new runtime as the CRM bridge."""
import hashlib
import json

import pytest

from kb.agent import Harness
from kb.context import ContextBudget
from kb.harness.ports import ApplicationPort
from test_application import upload,login
from test_deep_harness import ScriptModel,call,finish


def configure(cfg,monkeypatch,model):
    cfg.agent_policy='business_deep_v1';cfg.llm_model='test';cfg.llm_url='http://local.invalid/v1'
    cfg.harness_goal_review=False # Protocol tests isolate transport/checkpoint behavior; native review is tested separately.
    cfg.harness_compact_handoffs=False # Historical scripted replies include the old specialist metadata call.
    monkeypatch.setattr('kb.harness.application.ChatOpenAI',lambda **kwargs:model)
    monkeypatch.setattr(ContextBudget,'count',lambda *args:(100,'test'))


def test_http_to_shared_harness_preserves_citations_and_acl(system,monkeypatch):
    cfg,db,client,base,private,admin,member=system
    upload(system,'VPN连接故障先检查网络与客户端配置。','VPN.md')
    chunk=db.one('SELECT * FROM chunks LIMIT 1')
    label='S'+str(int(hashlib.sha256(str(chunk['id']).encode()).hexdigest()[:8],16))
    model=ScriptModel(replies=[call('task',{'description':'检索VPN连接故障处理方法并保留引用。','subagent_type':'evidence'}),
        call('search_documents',{'query':'VPN连接故障'}),*finish(f'先检查网络与客户端配置 [{label}]。'),
        *finish('先检查网络与客户端配置。')])
    configure(cfg,monkeypatch,model);login(client)
    response=client.post('/api/chat',json={'question':'VPN连接故障怎么处理？','base_ids':[base]})
    assert response.status_code==202
    ident=response.json()['run_id'];Harness(db,cfg).execute(ident)
    result=client.get('/api/runs/'+ident).json()
    assert result['status']=='answered',result
    assert result['citations'][0]['label']==label
    assert any(e['kind']=='answer_rendered'for e in result['events'])
    assert '资料原文'in result['answer'] and 'VPN连接故障先检查网络'in result['answer']
    with pytest.raises(PermissionError):ApplicationPort(db,cfg,member,[private],ident)


def test_delegated_draft_uses_real_database_and_requires_ui_confirmation(system,monkeypatch):
    cfg,db,client,base,_,_,member=system
    model=ScriptModel(replies=[call('task',{'subagent_type':'service','description':
        'Prepare a VPN draft: title VPN连接故障; description Windows VPN无法连接，已检查网络。'},'delegate'),
        call('prepare_ticket_draft',{'title':'VPN连接故障','description':'Windows VPN无法连接，已检查网络。'},'draft'),
        *finish('已生成草稿，等待用户确认提交。'),finish('已生成草稿，请在服务工单页面确认提交。')[0],
        call('CompletionMetadata',{'status':'partial','evidence_ids':[],'remaining_work':[
            {'kind':'deferred_user_action','description':'用户在服务工单页面确认提交。'}]})])
    configure(cfg,monkeypatch,model);login(client)
    response=client.post('/api/chat',json={'question':'帮我准备工单草稿：Windows VPN无法连接，已检查网络。','base_ids':[base]})
    ident=response.json()['run_id'];Harness(db,cfg).execute(ident)
    assert client.get('/api/runs/'+ident).json()['status']=='answered'
    state=json.loads(db.one('SELECT state FROM runs WHERE id=?',(ident,))['state'])
    assert state['business']['deferred_user_actions']==['用户在服务工单页面确认提交。']
    assert state['business']['remaining_work']==[]
    rows=db.all('SELECT * FROM service_tickets WHERE source_run=?',(ident,))
    assert len(rows)==1 and rows[0]['status']=='draft' and rows[0]['user_id']==member['id']
    # Existing authenticated confirmation performs the submission; the model did not.
    from kb.tickets import transition
    submitted=transition(db,member,rows[0]['id'],'submit',rows[0]['version'])
    assert submitted['status']=='submitted'


def test_budget_recovery_keeps_retrieved_evidence_in_the_delivered_http_answer(system,monkeypatch):
    cfg,db,client,base,*_=system
    upload(system,'VPN连接故障先检查网络与客户端配置。','VPN.md')
    model=ScriptModel(replies=[call('task',{'description':'检索VPN处理方法。','subagent_type':'evidence'}),
        call('search_documents',{'query':'VPN连接故障'}),finish('已检索处理方法。')[0],
        call('ProgressDecision',{'is_request_satisfied':True,'is_progress_being_made':True,
            'is_in_loop':False,'next_action':'stop','instruction':'The document was found.',
            'final_result':{'status':'completed','answer':'先检查网络与客户端配置。',
                            'evidence_ids':[],'remaining_work':[]}})])
    configure(cfg,monkeypatch,model);cfg.harness_model_calls=9;login(client)
    ident=client.post('/api/chat',json={'question':'VPN连接故障怎么处理？','base_ids':[base]}).json()['run_id']
    Harness(db,cfg).execute(ident)
    result=client.get('/api/runs/'+ident).json()
    assert result['status']=='answered',result
    assert result['citations']and '资料原文'in result['answer']
    assert any(e['kind']=='progress_review'for e in result['events'])
    assert 'VPN连接故障先检查网络'in result['answer']


def test_incident_tool_rejects_another_users_evidence(system):
    cfg,db,client,base,_,admin,member=system
    from kb.incidents import analyze
    incident=analyze(db,cfg,admin,'vpn.log',b'Windows VPN error 401: permission denied')
    port=ApplicationPort(db,cfg,member,[base],'unused')
    with pytest.raises(LookupError):port.execute('read_incident',{'incident_id':incident['id']},'read')


def test_canonical_crm_tools_and_opaque_assessment_do_not_enter_actor_input(tmp_path):
    from harness.tools.environment import create_environment
    import harness.tools.domains
    from kb.harness.ports import CRMPort
    import yaml
    from pathlib import Path
    root=Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
    port=CRMPort(create_environment('itsm'),yaml.safe_load((root/'src/agents/itsm.yaml').read_text()),
                 (root/'src/tasks/policies/itsm_policy.md').read_text())
    assert len(port.tools())==12
    from kb.harness.engine import DeepHarness
    engine=DeepHarness(model=ScriptModel(replies=[]),port=port,workspace=tmp_path,namespace='catalogue')
    for domain,capability in port.specialists.items():
        assert {t.name for t in engine._tools(domain)}==set(capability['tools'])
    assert {'get_ticket','add_ticket_comment','update_ticket_status'} <= {
        t.name for t in engine._tools('ticket_operations__tickets')}
    engine.close()
    denied=port.execute('check_permission',{'user_id':'admin.rlee','action':'delete','ticket_id':'TCK-1001'},'one')
    assert denied.status=='refused' and not port.audit[-1]['backend_executed']
    port.begin_request('I am agent.jsmith. Please check my permission to delete TCK-1001.','two')
    observed=port.execute('check_permission',{'user_id':'agent.jsmith','action':'delete','ticket_id':'TCK-1001'},'two')
    assert observed.status=='observed' and observed.value['allowed']is False


def test_http_resume_uses_native_checkpoint_and_preserves_spent_budget(system,monkeypatch):
    cfg,db,client,base,*_=system
    def fail_once(messages):raise ConnectionError('Injected transient model failure')
    model=ScriptModel(replies=[fail_once,call('list_my_tickets',{}),*finish('当前没有工单。')])
    configure(cfg,monkeypatch,model);login(client)
    ident=client.post('/api/chat',json={'question':'查一下我当前的工单。','base_ids':[base]}).json()['run_id']
    Harness(db,cfg).execute(ident)
    first=client.get('/api/runs/'+ident).json()
    assert first['status']=='error'
    assert first['can_resume']
    assert client.post('/api/runs/'+ident+'/resume').status_code==200
    Harness(db,cfg).execute(ident)
    after=client.get('/api/runs/'+ident).json()
    assert after['status']=='answered',after
    state=json.loads(db.one('SELECT state FROM runs WHERE id=?',(ident,))['state'])
    assert state['business']['calls']==4
    assert len(db.all("SELECT * FROM messages WHERE run_id=? AND role='assistant'",(ident,)))==1


def test_model_failure_after_database_commit_resumes_without_duplicate_draft(system,monkeypatch):
    cfg,db,client,base,*_=system
    def disconnect(messages):raise ConnectionError('Injected model failure after draft commit')
    model=ScriptModel(replies=[call('task',{'subagent_type':'service','description':'准备VPN连接故障工单草稿。'}),
        call('prepare_ticket_draft',{'title':'VPN连接故障','description':'Windows VPN无法连接。'}),
        disconnect,*finish('草稿已生成，等待确认。'),*finish('草稿已生成，请确认后提交。')])
    configure(cfg,monkeypatch,model);login(client)
    ident=client.post('/api/chat',json={'question':'请准备VPN连接故障工单草稿，Windows VPN无法连接。',
                                      'base_ids':[base]}).json()['run_id']
    Harness(db,cfg).execute(ident)
    assert client.get('/api/runs/'+ident).json()['status']=='error'
    first=db.all('SELECT * FROM service_tickets WHERE source_run=?',(ident,))
    assert len(first)==1 and first[0]['status']=='draft'
    assert client.post('/api/runs/'+ident+'/resume').status_code==200
    Harness(db,cfg).execute(ident)
    assert client.get('/api/runs/'+ident).json()['status']=='answered'
    after=db.all('SELECT * FROM service_tickets WHERE source_run=?',(ident,))
    assert len(after)==1 and after[0]['id']==first[0]['id']
    state=json.loads(db.one('SELECT state FROM runs WHERE id=?',(ident,))['state'])
    assert state['business']['calls']==7


def test_runtime_initialization_failure_releases_queued_run_with_actionable_error(system,monkeypatch):
    cfg,db,client,base,*_=system
    configure(cfg,monkeypatch,ScriptModel(replies=[]));login(client)
    def changed(**kwargs):raise ValueError('Runtime or workflow library changed; start a new workspace')
    monkeypatch.setattr('kb.harness.application.DeepHarness',changed)
    ident=client.post('/api/chat',json={'question':'查看工单','base_ids':[base]}).json()['run_id']
    Harness(db,cfg).execute(ident)
    result=client.get('/api/runs/'+ident).json()
    assert result['status']=='error' and not result['can_resume']
    assert any(e['kind']=='harness_initialization_error'for e in result['events'])


def test_schema_finishing_failure_resumes_after_agent_without_replaying_tools(system,monkeypatch):
    cfg,db,client,base,*_=system
    answer,metadata=finish('当前没有工单。')
    def broken_schema_reply(messages):raise ConnectionError('Injected finishing-call failure')
    model=ScriptModel(replies=[call('list_my_tickets',{}),answer,broken_schema_reply,metadata])
    configure(cfg,monkeypatch,model);login(client)
    ident=client.post('/api/chat',json={'question':'查看我的工单','base_ids':[base]}).json()['run_id']
    Harness(db,cfg).execute(ident)
    assert client.get('/api/runs/'+ident).json()['status']=='error'
    assert client.post('/api/runs/'+ident+'/resume').status_code==200
    Harness(db,cfg).execute(ident)
    assert client.get('/api/runs/'+ident).json()['status']=='answered'
    state=json.loads(db.one('SELECT state FROM runs WHERE id=?',(ident,))['state'])
    assert state['business']['calls']==4
    import sqlite3
    with sqlite3.connect(state['business']['workspace']+'/effects.sqlite3')as connection:
        assert connection.execute('SELECT COUNT(*) FROM effects').fetchone()[0]==1


def test_draft_preserves_verified_incident_fields_and_reconciles_after_restart(system,tmp_path):
    cfg,db,client,base,_,_,member=system
    from kb.incidents import analyze
    from kb.harness.journal import Journal
    incident=analyze(db,cfg,member,'vpn.log',b'Windows 11\nVPN HTTP 401\nAuthentication failed')
    login(client)
    ident=client.post('/api/chat',json={'question':'根据故障材料准备草稿','base_ids':[base]}).json()['run_id']
    port=ApplicationPort(db,cfg,member,[base],ident)
    journal=Journal(tmp_path/'effects.sqlite3','evidence');port.bind_journal(journal)
    specs={s.name:s for s in port.tools()}
    journal.invoke(specs['read_incident'],{'incident_id':incident['id']},ident,port,lambda *a:None)
    args={'title':'VPN故障','description':'请检查连接问题。'}
    outcome=journal.invoke(specs['prepare_ticket_draft'],args,ident,port,lambda *a:None)
    description=outcome['value']['description']
    assert all(t in description for t in ('Windows 11','HTTP 401','Authentication failed',incident['digest']))
    port=ApplicationPort(db,cfg,member,[base],ident);port.bind_journal(journal)
    assert port.reconcile('prepare_ticket_draft',args,outcome['evidence_id']).status=='committed'
    assert len(db.all('SELECT * FROM service_tickets WHERE source_run=?',(ident,)))==1
    journal.close()


def test_typed_attachment_is_owned_and_must_be_read_before_deriving_draft(system,tmp_path):
    cfg,db,client,base,_,admin,member=system
    from kb.incidents import analyze
    from kb.harness.journal import Journal
    foreign=analyze(db,cfg,admin,'private.log',b'Windows 11 VPN HTTP 401')
    incident=analyze(db,cfg,member,'vpn.log',b'Windows 11 VPN HTTP 401')
    login(client)
    assert client.post('/api/chat',json={'question':'读取材料','base_ids':[base],
        'incident_ids':[foreign['id']]}).status_code==404
    response=client.post('/api/chat',json={'question':'根据材料准备草稿','base_ids':[base],
        'incident_ids':[incident['id']]})
    assert response.status_code==202
    ident=response.json()['run_id'];port=ApplicationPort(db,cfg,member,[base],ident)
    assert port.input_context()['incident_inputs'][0]['id']==incident['id']
    with pytest.raises(ValueError,match='not a service ticket'):
        port.execute('get_my_ticket',{'ticket_id':incident['id']},'wrong-resource')
    with pytest.raises(ValueError,match='Read attached'):
        port.execute('prepare_ticket_draft',{'title':'VPN','description':'问题描述'},'too-early')
    assert not db.all('SELECT * FROM service_tickets WHERE source_run=?',(ident,))
    journal=Journal(tmp_path/'effects.sqlite3','attached');port.bind_journal(journal)
    spec=next(s for s in port.tools()if s.name=='read_incident')
    journal.invoke(spec,{'incident_id':incident['id']},ident,port,lambda *a:None)
    outcome=port.execute('prepare_ticket_draft',{'title':'VPN','description':'问题描述'},'after-reading')
    assert outcome.status=='committed' and 'Windows 11'in outcome.value['description']
    journal.close()
