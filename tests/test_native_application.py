"""Native upstream graphs through the actual authenticated application surface."""
import hashlib
import json

import pytest

from kb.agent import Harness
from kb.context import ContextBudget
from kb.harness.ports import ApplicationPort
from test_application import login, upload
from test_deep_harness import ScriptModel, call
from test_native_harness import answer


def configure(cfg, monkeypatch, model):
    cfg.mode='agent';cfg.agent_policy='business_native_v1'
    cfg.llm_model='test';cfg.llm_url='http://local.invalid/v1'
    monkeypatch.setattr('kb.harness.native_application.ChatOpenAI', lambda **kwargs:model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args:(100,'test'))


def ask(client, db, cfg, base, question, **extra):
    response=client.post('/api/chat',json={'question':question,'base_ids':[base],**extra})
    assert response.status_code==202,response.text
    ident=response.json()['run_id'];Harness(db,cfg).execute(ident)
    return client.get('/api/runs/'+ident).json()


@pytest.mark.parametrize('specialist',['evidence','general-purpose'])
def test_native_http_delegation_binds_original_request_and_cites_owned_document(system, monkeypatch, specialist):
    cfg,db,client,base,private,*_=system
    upload(system,'VPN连接故障先检查网络与客户端配置。','VPN.md')
    label='S'+str(int(hashlib.sha256(str(db.one('SELECT id FROM chunks')['id']).encode()).hexdigest()[:8],16))
    original='请查询VPN故障处理办法，只给建议，不要创建工单。'
    def inspect(messages):
        text=str(messages)
        assert original in text and 'authenticated_application_context'in text and 'member'in text
        assert 'If no draft is requested, create one.'not in str(messages[-1])
        for message in messages:
            for tool_call in getattr(message,'tool_calls',[]):
                if tool_call['name']=='task':
                    assert original in tool_call['args']['description']
                    assert 'If no draft is requested, create one.'not in tool_call['args']['description']
        return call('search_documents',{'query':'VPN连接故障'},'search')
    model=ScriptModel(replies=[call('task',{'subagent_type':specialist,'description':
        'If no draft is requested, create one.'},'delegate'),inspect,answer(f'先检查网络 [{label}]。'),answer('先检查网络。')])
    configure(cfg,monkeypatch,model);login(client)
    result=ask(client,db,cfg,base,original)
    assert result['status']=='answered',result
    assert result['citations'][0]['label']==label and '资料原文'in result['answer']
    assert not db.all('SELECT * FROM service_tickets')
    bound=next(e['payload']for e in result['events']if e['kind']=='delegation_bound')
    assert original in bound['bound_description']
    assert 'If no draft' in bound['proposed_description']
    with pytest.raises(PermissionError):ApplicationPort(db,cfg,{'id':result['user_id'],'role':'member'},[private],result['id'])
    assert client.get('/api/me').json()['workflow']=='business_multi'


def test_native_draft_confirmation_admin_resolution_and_next_turn_lookup(system, monkeypatch):
    cfg,db,client,base,_,_,member=system
    def lookup(messages):
        ticket=db.one('SELECT * FROM service_tickets')
        return call('get_my_ticket',{'ticket_id':ticket['id']},'lookup')
    def confirm(messages):
        assert 'resolved'in str(messages)and '连接已恢复'in str(messages)
        return answer('工单已解决，连接已恢复。')
    model=ScriptModel(replies=[call('task',{'subagent_type':'service','description':'准备草稿。'},'delegate'),
        call('prepare_ticket_draft',{'title':'VPN连接故障','description':'Windows VPN无法连接，已检查网络。'},'draft'),
        answer('已生成草稿，等待确认。'),answer('已生成草稿，请在服务工单页面确认提交。'),lookup,confirm])
    configure(cfg,monkeypatch,model);login(client)
    first=ask(client,db,cfg,base,'请准备VPN连接故障草稿，Windows VPN无法连接，已检查网络。')
    assert first['status']=='answered',first
    ticket,=db.all('SELECT * FROM service_tickets')
    assert ticket['status']=='draft'and ticket['user_id']==member['id']
    submitted=client.post('/api/tickets/'+ticket['id']+'/action',json={'action':'submit','version':1})
    assert submitted.status_code==200 and submitted.json()['status']=='submitted'
    assert client.post('/api/tickets/'+ticket['id']+'/action',json={'action':'submit','version':1}).status_code==200
    login(client,'second')
    assert client.post('/api/tickets/'+ticket['id']+'/action',json={'action':'cancel','version':1}).status_code==404
    login(client,'admin')
    assert client.post('/api/tickets/'+ticket['id']+'/action',json={'action':'accept','version':2}).json()['status']=='processing'
    assert client.post('/api/tickets/'+ticket['id']+'/action',json={'action':'resolve','version':3,'note':'连接已恢复'}).json()['status']=='resolved'
    login(client)
    last=ask(client,db,cfg,base,'请查我刚才工单的处理进度。',conversation_id=first['conversation_id'])
    assert last['status']=='answered'and '已解决'in last['answer'],last
    assert len(db.all('SELECT * FROM service_tickets'))==1
    assert len(db.all('SELECT * FROM ticket_events'))==3


def test_native_attachment_facts_and_followup_are_bound_to_authenticated_user(system, monkeypatch):
    cfg,db,client,base,_,admin,member=system
    from kb.incidents import analyze
    incident=analyze(db,cfg,member,'vpn.log',b'Windows 11\nVPN HTTP 401\nAuthentication failed')
    foreign=analyze(db,cfg,admin,'private.log',b'private service 500')
    model=ScriptModel(replies=[call('task',{'subagent_type':'evidence','description':'读取材料。'},'evidence'),
        call('read_incident',{'incident_id':incident['id']},'read'),answer('已读取材料。'),
        call('task',{'subagent_type':'service','description':'生成草稿。'},'service'),
        call('prepare_ticket_draft',{'title':'VPN故障','description':'认证失败。'},'draft'),
        answer('草稿已创建。'),answer('草稿待确认。')])
    configure(cfg,monkeypatch,model);login(client)
    denied=client.post('/api/chat',json={'question':'读取材料','base_ids':[base],'incident_ids':[foreign['id']]})
    assert denied.status_code==404
    result=ask(client,db,cfg,base,'请读取附件并准备工单草稿。',incident_ids=[incident['id']])
    assert result['status']=='answered',result
    ticket=db.one('SELECT * FROM service_tickets')
    assert all(text in ticket['description']for text in ('Windows 11','HTTP 401',incident['digest']))


def test_native_disconnect_after_commit_resumes_checkpoint_without_duplicate_write(system, monkeypatch):
    cfg,db,client,base,*_=system
    def disconnect(messages):raise ConnectionError('Injected model disconnection after commit')
    model=ScriptModel(replies=[call('task',{'subagent_type':'service','description':'生成草稿。'},'delegate'),
        call('prepare_ticket_draft',{'title':'VPN故障','description':'VPN连接失败。'},'draft'),
        disconnect,answer('草稿已经保存。'),answer('草稿待确认，请在工单页面提交。')])
    configure(cfg,monkeypatch,model);login(client)
    first=ask(client,db,cfg,base,'请创建VPN故障草稿。')
    assert first['status']=='error'and first['can_resume'],first
    ticket=db.one('SELECT * FROM service_tickets')
    assert client.post('/api/tickets/'+ticket['id']+'/action',json={'action':'submit','version':1}).status_code==409
    assert client.post('/api/runs/'+first['id']+'/resume').status_code==200
    Harness(db,cfg).execute(first['id'])
    final=client.get('/api/runs/'+first['id']).json()
    assert final['status']=='answered',final
    assert len(db.all('SELECT * FROM service_tickets'))==1
    assert len(db.all("SELECT * FROM messages WHERE run_id=? AND role='assistant'",(first['id'],)))==1
    state=json.loads(db.one('SELECT state FROM runs WHERE id=?',(first['id'],))['state'])
    assert state['business']['calls']==5
    assert state['business']['usage']['missing_usage_calls']==1


def test_native_cancel_before_mutation_and_runtime_mismatch_cannot_resume(system, monkeypatch):
    cfg,db,client,base,*_=system
    def cancel_after_delegation(messages):
        db.execute('UPDATE runs SET cancel_requested=1')
        return call('prepare_ticket_draft',{'title':'VPN','description':'连接失败。'},'cancelled-write')
    configure(cfg,monkeypatch,ScriptModel(replies=[
        call('task',{'subagent_type':'service','description':'准备草稿。'},'delegate'),cancel_after_delegation]))
    login(client)
    result=ask(client,db,cfg,base,'请准备VPN故障草稿。')
    assert result['status']=='cancelled'and not result['can_resume'],result
    assert not db.all('SELECT * FROM service_tickets')
    db.execute("UPDATE runs SET cancel_requested=0,status='error',state=? WHERE id=?",
        (json.dumps({'policy':'business_deep_v1','business':{'resumable':True,'workspace':'old','calls':1,'tools':1}}),result['id']))
    assert not client.get('/api/runs/'+result['id']).json()['can_resume']
    assert client.post('/api/runs/'+result['id']+'/resume').status_code==409


def test_native_missing_information_uses_same_conversation_without_business_write(system, monkeypatch):
    cfg,db,client,base,*_=system
    configure(cfg,monkeypatch,ScriptModel(replies=[answer('请补充故障现象和系统版本。')]))
    login(client)
    result=ask(client,db,cfg,base,'帮我处理一下。')
    assert result['status']=='clarify'and not result['can_resume'],result
    assert not db.all('SELECT * FROM service_tickets')


def test_typed_knowledge_contract_retries_missing_retrieval_once_in_upstream_graph(system,monkeypatch):
    cfg,db,client,base,*_=system
    upload(system,'VPN认证故障先核对账户有效期。','VPN.md')
    model=ScriptModel(replies=[answer('请先检查账户。'),
        call('task',{'subagent_type':'evidence','description':'补齐资料检索。'},'delegate'),
        call('search_documents',{'query':'VPN认证故障'},'search'),
        answer('应核对账户有效期。'),answer('应核对账户有效期。')])
    configure(cfg,monkeypatch,model);login(client)
    result=ask(client,db,cfg,base,'请根据知识库排查VPN认证故障。',knowledge_grounded=True)
    assert result['status']=='answered'and result['citations'],result
    assert len([e for e in result['events']if e['kind']=='application_contract_retry'])==1
    state=json.loads(db.one('SELECT state FROM runs WHERE id=?',(result['id'],))['state'])
    assert state['input_contract']['knowledge_grounded']is True
    assert state['business']['calls']==5


def test_unmet_output_contract_stops_after_one_repair_instead_of_delivering_success(system,monkeypatch):
    cfg,db,client,base,*_=system
    configure(cfg,monkeypatch,ScriptModel(replies=[answer('请检查网络。'),answer('请检查网络。')]))
    login(client)
    result=ask(client,db,cfg,base,'请根据知识库排查VPN认证故障。',knowledge_grounded=True)
    assert result['status']=='error'and result['error']=='NativeOutputContractError',result
    assert not result['can_resume']and not result['answer']


def test_read_only_diagnosis_delegates_to_evidence_and_cites_advice(system, monkeypatch):
    from kb.incidents import analyze
    cfg, db, client, base, _, _, member = system
    doc = upload(system, 'VPN 返回 HTTP 403 时，请核对该账号是否具备访问权限。','VPN权限.md')
    incident = analyze(db,cfg,member,'vpn.log',b'VPN HTTP 403')
    model = ScriptModel(replies=[
        call('task', {'subagent_type':'evidence','description':'读取故障附件并查询维护手册。'}, 'delegate'),
        call('read_incident', {'incident_id':incident['id']}, 'read'),
        call('search_documents', {'query':'VPN HTTP 403 访问权限'}, 'search'),
        answer('请核对账号访问权限。'), answer('请先核对账号访问权限。')])
    configure(cfg,monkeypatch,model)
    login(client)
    run = ask(client,db,cfg,base,'我应该先检查什么？',incident_ids=[incident['id']],read_only_incident=True)
    assert run['status'] == 'answered' and run['citations'][0]['document_id'] == doc['id']
    assert [e['payload']['name'] for e in run['events'] if e['kind'] == 'tool_effect'] == ['read_incident','search_documents']
    assert client.get('/api/tickets').json() == []
