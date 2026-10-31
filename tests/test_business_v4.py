from dataclasses import replace
import pytest
from langchain_core.messages import AIMessage
from kb.backends.business_multi_v4 import BusinessQA,response_schema,render_result
from test_business import engine,ScriptModel
from test_application import upload


def result(facts=(),resolution='complete',unresolved=''):
    return {'facts':list(facts),'resolution':resolution,'unresolved_question':unresolved}


def fact(text='每晚600元',source='S1'):
    return {'text':text,'sources':[source]}


def reply(data,ident='r'):
    return AIMessage(content='',tool_calls=[{'name':'KnowledgeResult','args':data,'id':ident,'type':'tool_call'}])


def obj(system,replies):
    upload(system)
    old,run=engine(system,{},[],'住宿标准？')
    x=BusinessQA(old.db,replace(old.cfg,agent_policy='business_multi_v4'),run,old.user,
        model=ScriptModel(plan={},replies=list(replies)))
    x.domains={k:'policy' for k in x.domains}
    return x


def test_render_citations_from_explicit_source_fields():
    schema=response_schema({'S1'})
    assert render_result(schema(**result([fact()])))==('answered','每晚600元[S1]')
    with pytest.raises(ValueError):schema(**result([fact(source='S9')]))
    with pytest.raises(ValueError):schema(**result([fact(text='每晚600元[S9]')]))


def test_missing_evidence_is_valid_without_fabricated_citation():
    schema=response_schema({'S1'})
    status,answer=render_result(schema(**result([], 'missing_evidence','生产服务器的真实地址')))
    assert status=='insufficient' and '[S1]' not in answer
    with pytest.raises(ValueError):schema(**result([], 'complete'))
    with pytest.raises(ValueError):schema(**result([], 'missing_evidence',''))


def test_partial_answer_retains_evidence_and_unresolved_question():
    schema=response_schema({'S1'})
    status,answer=render_result(schema(**result([fact('键盘上限300元')],'missing_evidence','显示器预算')))
    assert status=='insufficient' and '300元[S1]' in answer and '显示器预算' in answer


def test_real_framework_structured_result_and_checkpoint(system):
    x=obj(system,[reply(result([fact()]))])
    answer=x.answer()
    assert answer['status']=='answered' and '600元[S1]' in answer['answer']
    assert x.state['calls']==1 and x.state['specialist_results']['P1']['facts'][0]['sources']==['S1']
    assert x.answer()==answer and x.state['calls']==1


def test_framework_feedback_repairs_unauthorized_source_once(system):
    x=obj(system,[reply(result([fact(source='S9')]),'bad'),reply(result([fact()]),'fixed')])
    events=[];x.emit=lambda k,v:events.append((k,v))
    assert '600元[S1]' in x.answer()['answer']
    assert x.state['calls']==2
    assert len([k for k,_ in events if k=='business_schema_feedback'])==1


def test_repeated_invalid_source_fails_instead_of_auto_relabel(system):
    x=obj(system,[reply(result([fact(source='S9')]),'a'),reply(result([fact(source='S8')]),'b')])
    with pytest.raises(Exception,match='引用不在'):x.answer()
    assert x.state['calls']==2 and not x.state.get('specialist_results')


def test_actual_tool_search_precedes_structured_completion(system):
    x=obj(system,[AIMessage(content='',tool_calls=[{'name':'search_documents','args':{'query':'住宿'},'id':'s','type':'tool_call'}]),reply(result([fact()]))])
    assert '600元[S1]' in x.answer()['answer'] and x.state['calls']==2


def test_empty_evidence_reply_goes_through_framework(system):
    x=obj(system,[reply(result([], 'missing_evidence','餐饮补贴标准'))])
    output=x.answer()
    assert output['status']=='insufficient' and '[S' not in output['answer']


def test_clarification_is_not_marked_as_completed(system):
    x=obj(system,[reply(result([], 'needs_user_input','具体出差城市'))])
    assert x.answer()['status']=='clarify'


def test_existing_service_still_uses_real_db_receipt(system):
    old,run=engine(system,{},[],'Windows蓝屏。请生成工单草稿。')
    x=BusinessQA(old.db,replace(old.cfg,agent_policy='business_multi_v4'),run,old.user,
        model=ScriptModel(plan={},replies=[AIMessage(content='',tool_calls=[{'name':'prepare_ticket','args':{},'id':'t','type':'tool_call'}]),AIMessage(content='已经提交并解决')]))
    answer=x.answer()['answer']
    assert '待确认' in answer and '已经提交并解决' not in answer
    assert x.db.one('SELECT status FROM service_tickets WHERE source_run=?',(run['id'],))['status']=='draft'


def test_typed_classifier_routes_unrecognized_business_and_cannot_authorize_write(system):
    old,run=engine(system,{},[],'模拟公司借测试设备最多多久？')
    def label(value):return AIMessage(content='',tool_calls=[{'name':'RouteLabels','args':{'labels':[value]},'id':'c','type':'tool_call'}])
    x=BusinessQA(old.db,replace(old.cfg,agent_policy='business_multi_v4'),run,old.user,
        model=ScriptModel(plan={},replies=[label('policy')]))
    assert x.route({})['tasks'][0]['domain']=='policy'
    x=BusinessQA(old.db,replace(old.cfg,agent_policy='business_multi_v4'),run,old.user,
        model=ScriptModel(plan={},replies=[label('ticket_create')]))
    assert x.route({})['tasks']==[]


class RecordingModel(ScriptModel):
    seen: list=[]
    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        self.seen.append(messages)
        return super()._generate(messages,stop,run_manager,**kwargs)


def test_history_roles_are_preserved_and_current_task_is_last(system):
    upload(system)
    old,run=engine(system,{},[],'那上限是多少？')
    history=[{'role':'user','content':'演示制度的住宿报销怎么算？'},
        {'role':'assistant','content':'每晚600元[S9]。'}]
    model=RecordingModel(plan={},replies=[reply(result([fact()]))])
    x=BusinessQA(old.db,replace(old.cfg,agent_policy='business_multi_v4'),run,old.user,history,model=model)
    x.domains={k:'policy' for k in x.domains}
    assert '600元[S1]' in x.answer()['answer']
    messages=model.seen[0]
    assert [m.type for m in messages]==['system','human','ai','human']
    assert '[S9]' not in messages[2].content
    import json
    assert json.loads(messages[-1].content)['task']=='那上限是多少？'
