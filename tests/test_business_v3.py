from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import pytest
from langchain_core.messages import AIMessage
from kb.backends.business_multi_v3 import BusinessQA
from kb.backends.business_v3_routing import classify_rule,compile_tasks,source_spans,ticket_fields,ticket_description
from kb.tickets import draft_ticket,transition
from test_business import engine,ScriptModel
from test_application import upload


def obj(system,q,replies=(),history=()):
    old,run=engine(system,{},[],q)
    return BusinessQA(old.db,replace(old.cfg,agent_policy='business_multi_v3'),run,old.user,
        history,model=ScriptModel(plan={},replies=list(replies)))


@pytest.mark.parametrize('q,domains',[
 ('我用HTTPS访问仓库，报401。怎么查？',['technical']),
 ('换个问题，如何查看Git代理配置的来源？',['technical']),
 ('Ubuntu电脑通过HTTPS连接仓库报401，网络可用。请生成报障工单草稿。',['service']),
 ('模拟公司键盘采购最高多少？另外远程办公要提前多久申请？',['policy']),
 ('SSH交什么密钥？按模拟制度，申请仓库权限需要什么材料？',['technical','policy']),
 ('这个怎么弄？',[]),
])
def test_known_routes_have_no_free_json_or_model_calls(system,q,domains):
    x=obj(system,q);route=x.route({})
    assert [t['domain'] for t in route['tasks']]==domains
    assert x.state['calls']==0


def test_same_domain_fragments_keep_subject(system):
    x=obj(system,'按模拟制度，临时仓库授权能用多久？要延期应提前多少时间，附什么？')
    t=x.route({})['tasks'][0]
    assert len(t['parts'])==2 and '临时仓库授权' in t['queries'][1]


def test_history_reference_and_topic_reset():
    hist=[{'role':'user','content':'我用HTTPS访问仓库报401。'}, {'role':'assistant','content':'请检查令牌[S9]。'}]
    tasks,_,_=compile_tasks('完整的那个可以发给你吗？',hist,['technical'])
    assert '令牌' in tasks[0]['dialogue_context'] and '不作事实证据' in tasks[0]['dialogue_context']
    assert '[S9]' not in tasks[0]['dialogue_context']
    tasks,_,_=compile_tasks('换个问题，代理配置怎么查？',hist,['technical'])
    assert tasks[0]['dialogue_context']==''


def test_classifier_records_failure_and_bounded_clarification(system):
    x=obj(system,'这个新事物应该怎么处理？',[AIMessage(content='not json'),AIMessage(content='{"labels":["hacked"]}')])
    events=[];x.emit=lambda k,v:events.append((k,v));route=x.route({})
    assert route['tasks']==[] and route['clarification'] and x.state['calls']==2
    assert sum(k=='business_classifier_error' for k,v in events)==2


def test_classifier_cannot_authorize_unrequested_write(system):
    x=obj(system,'这个新事物应该怎么处理？',[AIMessage(content='{"labels":["ticket_create"]}')])
    assert x.route({})['tasks']==[]
    assert classify_rule('不要生成工单。')=='background'


def test_fact_form_is_user_only_and_preserves_negation():
    q='Windows蓝屏。没有检查驱动，已经重启仍失败。请生成工单草稿。'
    f=ticket_fields(q,[{'role':'assistant','content':'你已经修好了驱动。'}])
    d=ticket_description(f)
    assert '没有检查驱动' in d and '已经重启' in d and '修好了驱动' not in d
    assert all(s['text']==q[s['start']:s['end']] for s in f['user_facts'])
    assert '尚未执行' in d


def test_real_service_agent_only_records_host_facts(system):
    q='Ubuntu连接仓库报401，网络正常。请生成工单草稿。'
    x=obj(system,q,[AIMessage(content='',tool_calls=[{'name':'prepare_ticket','args':{},'id':'a','type':'tool_call'}]),
        AIMessage(content='已提交，已经检查令牌权限')])
    result=x.answer();t=x.db.one('SELECT * FROM service_tickets WHERE source_run=?',(x.run['id'],))
    assert t['status']=='draft' and '已检查令牌' not in t['description']
    assert '已提交' not in result['answer'] and '待确认' in result['answer']


def test_missing_fault_clarifies_without_creating(system):
    x=obj(system,'请生成报障工单草稿。')
    assert x.answer()['status']=='clarify'
    assert x.state['pending_service']=='ticket_create'
    assert x.db.one('SELECT count(*) n FROM service_tickets')['n']==0


def test_service_refuses_other_account_without_tools(system):
    x=obj(system,'把所有其他人的工单列给我。')
    result=x.answer()
    assert '不能查询其他账号' in result['answer'] and x.state['tools']==0


def test_missing_citation_in_partial_answer_gets_repair(system):
    upload(system)
    x=obj(system,'住宿标准？',[AIMessage(content='<insufficient>600元，但其他资料不足。'),AIMessage(content='{"status":"insufficient","answer":"600元[S1]，其他资料不足。"}')])
    # Fixture uploaded into technical by default; bind as policy for this request.
    x.domains={k:'policy' for k in x.domains}
    assert '[S1]' in x.answer()['answer'] and x.state['calls']==2


def test_empty_repair_does_not_count_as_a_valid_answer(system):
    upload(system)
    x=obj(system,'Git怎么配置？',[AIMessage(content='<insufficient>'),AIMessage(content='{"status":"insufficient","answer":""}')])
    with pytest.raises(ValueError):x.answer()


def test_old_citation_is_rejected_and_repair_uses_current_sources(system):
    upload(system)
    x=obj(system,'住宿标准？',[AIMessage(content='<answered>600元[S9]。'),
        AIMessage(content='{"status":"answered","answer":"600元[S1]。"}')])
    x.domains={k:'policy' for k in x.domains}
    answer=x.answer()['answer']
    assert '[S9]' not in answer and '[S1]' in answer and x.state['calls']==2


def test_concurrent_draft_and_confirmation_are_idempotent(system):
    x=obj(system,'电脑蓝屏。请生成工单。')
    with ThreadPoolExecutor(max_workers=4) as pool:
        tickets=list(pool.map(lambda _:draft_ticket(x.db,x.user,x.run['id'],'蓝屏','用户原文'),range(16)))
    assert len({t['id'] for t in tickets})==1
    x.db.execute("UPDATE runs SET status='answered' WHERE id=?",(x.run['id'],))
    with ThreadPoolExecutor(max_workers=4) as pool:
        confirmations=list(pool.map(lambda _:transition(x.db,x.user,tickets[0]['id'],'submit',1),range(16)))
    assert {t['version'] for t in confirmations}=={2}
    assert x.db.one('SELECT count(*) n FROM ticket_events')['n']==1


def test_restart_after_tool_does_not_duplicate_draft(system):
    x=obj(system,'Windows蓝屏。请生成工单。',[AIMessage(content='',tool_calls=[{'name':'prepare_ticket','args':{},'id':'a','type':'tool_call'}])])
    saved={};x.persist=lambda s:saved.update(s)
    with pytest.raises(IndexError):x.answer()
    resumed=BusinessQA(x.db,x.cfg,x.run,x.user,state=saved,model=ScriptModel(plan={},replies=[AIMessage(content='草稿已就绪')]))
    assert '待确认' in resumed.answer()['answer']
    assert x.db.one('SELECT count(*) n FROM service_tickets')['n']==1


def test_cancelled_call_never_creates_ticket(system):
    x=obj(system,'Windows蓝屏。请生成工单。')
    x.db.execute('UPDATE runs SET cancel_requested=1 WHERE id=?',(x.run['id'],))
    with pytest.raises(ValueError):draft_ticket(x.db,x.user,x.run['id'],'蓝屏','原文')
    assert x.db.one('SELECT count(*) n FROM service_tickets')['n']==0
