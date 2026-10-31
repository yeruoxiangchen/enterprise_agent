import copy
import pytest
from kb.backends.fixed_first_rag import FixedFirstQA, followup_reason
from kb.backends.haystack_rag import SYSTEM
from test_agentpack import build,request
from test_external_rag import engine_data


def make(data,responses,**kwargs):
    parent,gen=build(data,responses)
    return FixedFirstQA(parent.db,parent.settings,parent.mapping,generator=gen,counter=lambda m,t:100,**kwargs),gen


def test_normal_answer_identical_fixed_prompt_and_one_call(engine_data):
    engine,gen=make(engine_data,['<answered>凭证十五分钟，仅存内存 [S1]。'])
    out=engine.answer(request())
    assert out['model_calls']==1 and out['followup_trigger'] is None and not out['warnings']
    assert gen.calls[0][0].text==SYSTEM and not gen.tool_names[0]
    assert out['citations'][0]['document_id']=='d1'
    assert 'SECRET_CANARY' not in str([[m.to_dict() for m in c] for c in gen.calls])


def test_clarification_is_not_forced_to_search(engine_data):
    engine,gen=make(engine_data,['<clarify>请提供标签名以及本地还是远端。'])
    out=engine.answer(request('请同时删除标签，名字和位置没给'))
    assert out['model_calls']==1 and out['status']=='clarify'


def test_insufficient_can_use_one_official_tool_and_reanswer(engine_data):
    engine,gen=make(engine_data,['<insufficient>没有足够信息。',('search_documents',{'query':'凭证缓存'}),
                                '<answered>凭证在内存中缓存十五分钟 [S1]。'])
    out=engine.answer(request())
    assert out['extra_searches']==1 and out['model_calls']==3 and out['followup_accepted']
    assert gen.tool_names[1]==['search_documents'] and gen.options[1]['tool_choice']['function']['name']=='search_documents'
    assert '没有足够信息。' not in gen.calls[1][-1].text
    assert all(t['kind']!='retrieval' or not t.get('filters') for t in out['trace'])


def test_invalid_replacement_retains_valid_initial_answer(engine_data):
    draft='<answered>缓存十五分钟且不写盘 [S1]。'
    engine,gen=make(engine_data,[draft,'<answered>错误引用 [S99]。'])
    out=engine.answer(request('凭证缓存多久？另外会写盘吗？'))
    assert out['raw_answer']==draft and not out['followup_accepted'] and not out['warnings']
    assert out['model_calls']==2 and engine.state['candidate']['warnings']


def test_refusal_cannot_overwrite_valid_answer(engine_data):
    draft='<answered>缓存十五分钟且不写盘 [S1]。'
    engine,_=make(engine_data,[draft,'<insufficient>没有资料。'])
    out=engine.answer(request('缓存多久以及是否写盘'))
    assert out['raw_answer']==draft and not out['followup_accepted']


def test_repeat_tool_cannot_exceed_one_search_or_four_calls(engine_data):
    engine,gen=make(engine_data,['<insufficient>不清楚。',('search_documents',{'query':'凭证缓存'}),
                                ('search_documents',{'query':'凭证缓存'}),'<answered>缓存十五分钟 [S1]。'])
    out=engine.answer(request())
    assert out['extra_searches']==1 and out['model_calls']==4 and out['backup_calls']==1
    assert len([t for t in out['trace'] if t['kind']=='followup_search'])==1
    assert not gen.tool_names[-1] and out['followup_accepted']


def test_unauthorized_scope_rejected_without_generation(engine_data):
    engine,gen=make(engine_data,[])
    with pytest.raises(PermissionError):engine.answer(request(bases=('private',)))
    assert not gen.calls


def test_source_change_prevents_fallback_commit(engine_data):
    def changed(messages):
        engine.db.execute('UPDATE chunks SET text=? WHERE id=1',('changed',))
        return '<answered>缓存十五分钟 [S1]。'
    engine,_=make(engine_data,['<answered>缓存十五分钟 [S1]。',changed])
    with pytest.raises(RuntimeError,match='source changed'):engine.answer(request('缓存多久以及是否写盘'))


def test_resume_does_not_regenerate_saved_draft(engine_data):
    engine,_=make(engine_data,['<answered>缓存十五分钟 [S1]。'])
    engine.answer(request());saved=copy.deepcopy(engine.state)
    resumed,gen=make(engine_data,[])
    out=resumed.answer(request(),saved)
    assert not gen.calls and out['model_calls']==1


def test_partial_content_is_not_erased_by_missing_grounding(monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    from audit_agentpack_20261030 import independent_axes
    row={'status':'answered','grade':{'correctness':None,'faithfulness':None,'citation_support':None,
        'partial_grades':{'content':{'correctness':1,'rationale':'提供了答案','answer_checks':[
            {'requirement':'回答值','satisfied':1,'answer_quote':'十五分钟','reason':'满足'}]},'grounding':None}}}
    result=independent_axes(row,{'answer':'十五分钟','expected_action':'answered','prediction_status':'answered'})
    assert result['values']=={'correctness':1,'faithfulness':None,'citation_support':None}
    assert result['origins']['correctness']=='validated_saved_content'


@pytest.mark.parametrize('policy',['fixed_first_v1','fixed_rag_v1'])
def test_fixed_first_app_commits_once(system,monkeypatch,policy):
    from dataclasses import replace
    from test_application import upload,login
    from test_agentpack import Scripted
    from kb.agent import Harness
    import kb.backends.fixed_first_rag as module
    cfg,db,client,base,*_=system
    upload(system);login(client)
    gen=Scripted(['<answered>住宿标准600元。 [S1]'])
    cls=module.FixedFirstQA
    monkeypatch.setattr(module,'FixedFirstQA',lambda *a,**k:cls(*a,**k,generator=gen,counter=lambda m,t:100))
    rid=client.post('/api/chat',json={'question':'住宿标准','base_ids':[base]}).json()['run_id']
    db.execute("UPDATE runs SET mode='agent' WHERE id=?",(rid,))
    harness=Harness(db,replace(cfg,agent_policy=policy))
    harness.execute(rid);harness.execute(rid)
    assert db.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='answered'
    assert db.one("SELECT count(*) AS n FROM messages WHERE run_id=? AND role='assistant'",(rid,))['n']==1
    assert len(gen.calls)==1


@pytest.mark.parametrize('question,response',[
    ('缓存多久以及是否写盘','<answered>十五分钟，仅存内存 [S1]。'),
    ('本公司的真实网址','<insufficient>资料没有公司实际网址。'),
    ('给我一个未明确的操作','<clarify>请提供操作对象和目标。')])
def test_fixed_rag_never_enters_followup(engine_data,question,response):
    engine,gen=make(engine_data,[response],enable_followup=False)
    out=engine.answer(request(question))
    assert out['model_calls']==1 and out['tool_calls']==0 and out['extra_searches']==0
    assert out['followup_trigger'] is None and not out['followup_accepted']


def test_fixed_rag_reserved_call_cannot_silently_replay(engine_data):
    engine,_=make(engine_data,[RuntimeError('interrupted transport')],enable_followup=False)
    with pytest.raises(Exception):engine.answer(request())
    state=copy.deepcopy(engine.state)
    assert state['calls']==1 and 'draft' not in state
    resumed,gen=make(engine_data,['<answered>十五分钟 [S1]。'],enable_followup=False)
    with pytest.raises(Exception,match='budget exhausted'):resumed.answer(request(),state)
    assert not gen.calls and resumed.state['calls']==1
