import json
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.runnables import RunnableLambda
from kb.backends.business_multi import BusinessQA, Route, canonical_reply
from kb.tickets import draft_ticket
from kb.agent import Harness
from test_application import upload, login


class ScriptModel(BaseChatModel):
    plan: dict
    replies: list
    @property
    def _llm_type(self):return 'scripted-test'
    def bind_tools(self, tools, **kwargs):return self
    def with_structured_output(self, schema, **kwargs):
        values={name:False for name in ('technical','policy','service')}
        values['clarification']=self.plan.get('clarification','')
        for task in self.plan.get('tasks',[]):values[task['domain']]=True
        return RunnableLambda(lambda messages:schema(**values))
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])


def engine(system, plan, replies, question='住宿标准？'):
    cfg,db,client,base,_,_,member=system
    login(client)
    created=client.post('/api/chat',json={'question':question}).json()
    run=db.one('SELECT * FROM runs WHERE id=?',(created['run_id'],))
    model=ScriptModel(plan=plan,replies=replies)
    return BusinessQA(db,cfg,run,member,model=model),run


def test_domain_isolation_no_empty_scope_fallback(system):
    upload(system)
    obj,_=engine(system,{},[])
    assert not obj.search('policy','住宿')['evidence']
    assert obj.search('technical','住宿')['evidence']


def test_real_graph_and_agent_citation_preservation(system):
    upload(system)
    obj,run=engine(system,{'tasks':[{'domain':'technical','question':'住宿标准？'}]},
        [AIMessage(content='<answered>\n每晚600元 [S1]。')])
    result=obj.answer()
    assert result['status']=='answered' and '600' in result['answer']
    calls=obj.state['calls']
    assert obj.answer()==result and obj.state['calls']==calls  # persisted graph reused


def test_clarification_without_workers(system):
    obj,_=engine(system,{'tasks':[],'clarification':'请说明具体问题'},[])
    assert obj.answer()['status']=='clarify'
    assert obj.state['calls']==1


def test_wrong_domain_citation_rejected(system):
    upload(system)
    obj,_=engine(system,{'tasks':[{'domain':'policy','question':'住宿标准？'}]},
        [AIMessage(content='<answered>\n600 [S1]'), AIMessage(content='<answered>\n600 [S1]')])
    with pytest.raises(ValueError,match='引用'):obj.answer()


def test_missing_citation_repaired_by_model_once(system):
    upload(system)
    obj,_=engine(system,{'tasks':[{'domain':'technical','question':'住宿标准？'}]},
        [AIMessage(content='<answered>\n600元。'),AIMessage(content='<answered>\n600元 [S1]。')])
    assert '[S1]' in obj.answer()['answer']
    assert obj.state['calls']==3


def test_explicit_parenthesis_citations_only():
    assert canonical_reply('<answered>\n15天（S1）。(S2)')[1]=='15天[S1]。[S2]'
    assert canonical_reply('<answered>\n15天。')[1]=='15天。'


def test_service_tools_create_only_draft_and_idempotent(system):
    obj,run=engine(system,{'tasks':[{'domain':'service','question':'电脑坏了，请生成工单草稿'}]},[
        AIMessage(content='',tool_calls=[{'name':'prepare_ticket','args':{'title':'电脑故障','description':'Windows开机蓝屏'},'id':'call1','type':'tool_call'}]),
        AIMessage(content='<answered>\n已经提交，编号错误')])
    result=obj.answer()
    assert '待确认' in result['answer'] and '已经提交' not in result['answer']
    ticket=obj.db.one('SELECT * FROM service_tickets')
    assert ticket['status']=='draft'
    assert draft_ticket(obj.db,obj.user,run['id'],'新标题','新内容')['id']==ticket['id']
    assert obj.db.one('SELECT count(*) AS n FROM service_tickets')['n']==1


def test_ticket_confirmation_acl_and_lifecycle(system):
    cfg,db,client,_,_,_,member=system
    obj,run=engine(system,{},[])
    ticket=draft_ticket(db,member,run['id'],'连接故障','SSH连接提示Permission denied')
    path='/api/tickets/'+ticket['id']+'/action'
    assert client.post(path,json={'action':'submit','version':1}).status_code==409
    db.execute("UPDATE runs SET status='answered' WHERE id=?",(run['id'],))
    assert client.post(path,json={'action':'submit','version':1}).json()['status']=='submitted'
    assert client.post(path,json={'action':'submit','version':1}).json()['version']==2
    login(client,'second')
    assert client.get('/api/tickets').json()==[]
    assert client.post(path,json={'action':'cancel','version':2}).status_code==404
    login(client)
    assert client.post(path,json={'action':'resolve','version':2}).status_code==403
    login(client,'admin')
    assert client.post(path,json={'action':'accept','version':2}).json()['status']=='processing'
    assert client.post(path,json={'action':'resolve','version':3,'note':'已处理'}).json()['status']=='resolved'


def test_budget_and_cancel(system):
    obj,_=engine(system,{},[])
    obj.state['calls']=12
    with pytest.raises(RuntimeError,match='上限'):obj.reserve([{'role':'user','content':'x'}])
    def cancel():raise RuntimeError('cancelled')
    obj.cancelled=cancel
    with pytest.raises(RuntimeError,match='cancelled'):obj.search('technical','住宿')


def test_resume_keeps_completed_domain_and_budget(system):
    upload(system)
    saved={}
    obj,run=engine(system,{'tasks':[{'domain':'technical','question':'住宿'}, {'domain':'policy','question':'报销'}]},
        [AIMessage(content='<answered>\n600 [S1]')])
    obj.persist=lambda s:saved.update(s)
    original=obj.worker
    def fail_policy(domain,state):
        if domain=='policy':raise RuntimeError('simulated interruption')
        return original(domain,state)
    obj.worker=fail_policy
    with pytest.raises(RuntimeError,match='interruption'):obj.answer()
    cfg,db,*_=system
    resumed=BusinessQA(db,cfg,run,obj.user,state=saved,model=ScriptModel(plan={},replies=[AIMessage(content='<insufficient>\n未配置制度资料。')]))
    result=resumed.answer()
    assert result['status']=='insufficient' and '600' in result['answer']
    assert resumed.state['calls']==3  # one router, one technical, one resumed policy


def test_cancelled_draft_cannot_be_submitted(system):
    cfg,db,client,*_=system
    obj,run=engine(system,{},[])
    ticket=draft_ticket(db,obj.user,run['id'],'问题','描述')
    db.execute("UPDATE runs SET status='cancelled',cancel_requested=1 WHERE id=?",(run['id'],))
    response=client.post('/api/tickets/'+ticket['id']+'/action',json={'action':'submit','version':1})
    assert response.status_code==409


def test_domain_binding_is_admin_only(system):
    cfg,db,client,base,*_=system
    login(client)
    assert client.put('/api/bases/'+base+'/domain',json={'domain':'policy'}).status_code==403
    login(client,'admin')
    assert client.put('/api/bases/'+base+'/domain',json={'domain':'policy'}).status_code==200
    obj,_=engine(system,{},[])
    assert obj.domains[base]=='policy'
