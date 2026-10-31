import copy
import json

import pytest
pytest.importorskip('haystack')
from haystack import component
from haystack.dataclasses import ChatMessage, ToolCall
from kb.backends.haystack_rag import QARequest
from kb.backends.multi_agent import MultiAgentQA
from kb.skills import SkillRegistry
from kb.retrieval import tokens
from test_external_rag import engine_data


@component
class Script:
    def __init__(self, calls):
        self.script, self.calls, self.seen = list(calls), 0, []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools=None, generation_kwargs=None):
        self.calls += 1
        self.seen.append([m.to_dict() for m in messages])
        value = self.script.pop(0)
        if isinstance(value, BaseException):
            raise value
        name, args = value
        return {'replies': [ChatMessage.from_assistant(tool_calls=[ToolCall(
            tool_name=name, arguments=args, id='call_'+str(self.calls))],
            meta={'usage': {'prompt_tokens': 20, 'completion_tokens': 10}, 'finish_reason': 'tool_calls'})]}


def req(bases=('public',), question='Git 凭证缓存多久？'):
    return QARequest(question, (), {'role': 'member'}, bases)


def result(label='S1'):
    return ('submit_result', {'status': 'answered',
        'claims': [{'text': '缓存十五分钟', 'evidence': [label]}], 'message': ''})


def engine(data, calls, **kwargs):
    cfg, db, mapping = data
    cfg.reranker_path = ''
    for row in db.all('SELECT c.id,c.text,d.filename FROM chunks c JOIN documents d ON d.id=c.document_id'):
        if not db.one('SELECT rowid FROM chunks_fts WHERE rowid=?',(row['id'],)):
            db.execute('INSERT INTO chunks_fts(rowid,tokens) VALUES(?,?)',
                       (row['id'],' '.join(tokens(row['filename']+' '+row['text']))))
    model = Script(calls)
    return MultiAgentQA(db, cfg, mapping, generator=model, counter=lambda m,t: 100, **kwargs), model


def test_real_haystack_agenttool_and_skill_loading(engine_data):
    h, m = engine(engine_data, [('submit_plan', {'tasks': [
        {'question': 'Git 凭证缓存多久', 'skill': 'lookup'}]}), result()])
    out = h.answer(req())
    assert out['answer'] == '缓存十五分钟 [S1]'
    assert out['model_calls'] == m.calls == 2
    assert out['citations'][0]['document_id'] == 'd1'
    assert any(t['kind']=='skill_loaded' for t in out['trace'])
    assert 'SECRET_CANARY' not in json.dumps(m.seen)


def test_multitask_shared_evidence_and_synthesis(engine_data):
    h, m = engine(engine_data, [('submit_plan', {'tasks': [
        {'question': 'Git 缓存时间', 'skill': 'lookup'},
        {'question': 'Git 缓存位置', 'skill': 'procedure'}]}), result(), result(), result()])
    out = h.answer(req(question='缓存时间和位置？'))
    assert len(out['plan'])==2 and len(out['hits'])==1 and m.calls==4
    assert len(h.state['results'])==2 and out['tool_calls']<=24


def test_bad_citation_receives_feedback_then_repairs(engine_data):
    h, m = engine(engine_data, [result('S99'),result()], decompose=False)
    out = h.answer(req())
    assert out['model_calls']==2 and out['citations'][0]['label']=='S1'
    assert any(t['kind']=='agent_validation' for t in out['trace'])


def test_permissions_rejected_before_any_model_call(engine_data):
    h, m = engine(engine_data, [], decompose=False)
    with pytest.raises(PermissionError):h.answer(req(('private',)))
    assert m.calls==0


def test_global_budget_counts_failed_request_and_resume(engine_data):
    saved=[]
    plan=('submit_plan',{'tasks':[{'question':'凭证缓存多久','skill':'lookup'}]})
    h, m=engine(engine_data,[plan,SystemExit('interrupt')],checkpoint=lambda s:saved.append(s))
    with pytest.raises(SystemExit):h.answer(req())
    assert saved[-1]['calls']==2 and saved[-1]['plan']
    h2,m2=engine(engine_data,[result()])
    out=h2.answer(req(),saved[-1])
    assert m2.calls==1 and out['model_calls']==3
    with pytest.raises(RuntimeError,match='different request'):h2.answer(req(question='另一问题'),saved[-1])


def test_resume_skips_finished_subtask(engine_data):
    saved=[]
    plan=('submit_plan',{'tasks':[{'question':'凭证缓存时间','skill':'lookup'},
                                  {'question':'凭证缓存位置','skill':'lookup'}]})
    h,_=engine(engine_data,[plan,result(),SystemExit('interrupt')],checkpoint=lambda s:saved.append(s))
    with pytest.raises(SystemExit):h.answer(req())
    assert 'T1' in saved[-1]['results']
    h2,m=engine(engine_data,[result(),result()])
    out=h2.answer(req(),saved[-1])
    assert m.calls==2 and out['model_calls']==5


def test_budget_and_cancellation_do_not_produce_answers(engine_data):
    h,m=engine(engine_data,[result('S99'),result()],decompose=False,max_calls=1)
    with pytest.raises(Exception):h.answer(req())
    assert m.calls==1 and not h.state.get('final')
    h,m=engine(engine_data,[],decompose=False,cancelled=lambda: (_ for _ in ()).throw(RuntimeError('cancelled')))
    with pytest.raises(RuntimeError,match='cancelled'):h.answer(req())
    assert m.calls==0


def test_skills_off_and_unknown_skill(engine_data):
    h,m=engine(engine_data,[result()],decompose=False,skills=False)
    out=h.answer(req())
    assert not any(t['kind']=='skill_loaded' for t in out['trace'])
    with pytest.raises(ValueError):SkillRegistry().load('../private.env')


def test_changed_evidence_invalidates_checkpoint(engine_data):
    saved=[]
    h,_=engine(engine_data,[SystemExit('interrupt')],decompose=False,checkpoint=lambda s:saved.append(s))
    with pytest.raises(SystemExit):h.answer(req())
    engine_data[1].execute("UPDATE chunks SET text='changed' WHERE id=1")
    h2,m=engine(engine_data,[result()],decompose=False)
    with pytest.raises(RuntimeError,match='Evidence changed'):h2.answer(req(),saved[-1])
    assert m.calls==0


def test_followup_retrieves_once_and_retains_source_ids(engine_data):
    h,m=engine(engine_data,[('search_more',{'query':'凭证 内存'}),result()],decompose=False)
    out=h.answer(req())
    assert m.calls==2 and h.state['followups']==['T1']
    assert len(h.state['sources'])==1 and out['citations'][0]['label']=='S1'


def test_context_budget_stops_before_generation(engine_data):
    h,m=engine(engine_data,[result()],decompose=False)
    h.budget.counter=lambda m,t:99999
    with pytest.raises(Exception):h.answer(req())
    assert m.calls==0 and not h.state.get('final')


def test_application_adapter_finishes_once_with_sources(system,monkeypatch):
    from kb.agent import Harness
    from test_application import upload
    import kb.multi_agent_runtime
    import kb.backends.multi_agent as module
    upload(system)
    cfg,db,client,base,*_=system
    plan=('submit_plan',{'tasks':[{'question':'住宿报销标准','skill':'lookup'}]})
    actual=module.MultiAgentQA
    model=Script([plan,('submit_result',{'status':'answered','claims':[
        {'text':'住宿标准600元','evidence':['S1']}],'message':''})])
    def factory(*args,**kwargs):
        return actual(*args,**kwargs,generator=model,counter=lambda m,t:100)
    monkeypatch.setattr(module,'MultiAgentQA',factory)
    cfg.agent_policy='multi_agent_v1'
    response=client.post('/api/chat',json={'question':'住宿报销标准','base_ids':[base]}).json()
    rid=response['run_id'];db.execute("UPDATE runs SET mode='agent' WHERE id=?",(rid,))
    Harness(db,cfg).execute(rid)
    row=client.get('/api/runs/'+rid).json()
    assert row['status']=='answered' and row['citations'][0]['label']=='S1'
    assert any(e['kind']=='task_plan' for e in row['events'])
    Harness(db,cfg).execute(rid)
    assert model.calls==2
