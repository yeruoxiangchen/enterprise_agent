import copy
import json
import pytest
from kb.benchmark.fixed_v42 import canonical_citations
from kb.benchmark.semantic import validate_answer_quotes
from kb.benchmark.calibration import cases
from kb.guarded_v42 import assess
from test_v3 import start
from test_harness import call
from test_application import upload


def assessment(scope='document_fact',state='covered',query=''):
    return {'intent':'住宿报销标准','requirements':[{'need':'住宿标准详细规定','question_span':'住宿',
        'scope':scope,'state':state,'evidence':['S1:1'] if state=='covered' else []}],
        'clarifying_questions':[],'query':query}


def memory():return {'question':'住宿报销标准','history':[],
    'sources':{'S1':{'id':1,'text':'住宿标准600元','filename':'标准'}}}


def test_citation_metadata_can_be_reconstructed_without_changing_answer():
    sources={'S1':{},'S5':{}}
    assert canonical_citations('正文 [S1]',['S1','S5'],sources)==['S1']
    assert canonical_citations('资料不足 [S1]',[],sources)==['S1']
    assert canonical_citations('无依据正文',['S1'],sources)==[]  # Harness still rejects answered without references.
    for answer,refs in [('正文 [S99]',[]),('正文 [S1]',['S99'])]:
        with pytest.raises(ValueError,match='伪造'):canonical_citations(answer,refs,sources)


def test_fixed_v42_still_finishes_through_read_and_acl_checks(system):
    from kb.benchmark.fixed_v42 import FixedRAGV42
    cfg,db,client,base,*_=system;cfg.context_tokens=24000;upload(system)
    row=client.post('/api/chat',json={'question':'住宿报销标准','base_ids':[base]}).json()
    class Model:
        def complete(self,messages,tools):
            return call('finish',{'answer':'住宿标准600元 [S1]','citations':[],'status':'answered'})
    FixedRAGV42(db,cfg,Model()).execute_fixed(row['run_id'],[])
    r=client.get('/api/runs/'+row['run_id']).json()
    assert r['status']=='answered' and len(r['citations'])==1
    assert any(e['kind']=='citation_normalization' for e in r['events'])


def test_question_requirement_is_anchored_to_user_not_tool_text():
    state=memory();args=assessment()
    assert assess(args,state)[0]=='answer'
    args['requirements'][0]['question_span']='确认现场系统已经安装'
    state['sources']['S1']['text']+='确认现场系统已经安装'
    with pytest.raises(ValueError,match='逐字'):assess(args,state)


def test_missing_document_query_is_not_silently_abandoned():
    assert assess(assessment(state='missing'),memory())==('search','住宿标准详细规定')
    assert assess(assessment(scope='organization_fact',state='missing',query='通用教程'),memory())[0]=='insufficient'
    with pytest.raises(ValueError,match='用户选择'):assess(assessment(scope='document_fact',state='needs_user'),memory())


def test_user_intent_can_use_history_but_still_requires_clarification():
    state=memory();state['history']=[{'role':'user','content':'目标环境尚未确定'}]
    args=assessment(scope='user_choice',state='needs_user')
    args['requirements'][0]['question_span']='目标环境尚未确定';args['clarifying_questions']=['目标环境是测试还是生产？']
    assert assess(args,state)[0]=='clarify'


def test_real_harness_runs_one_fallback_search_and_preserves_bound(system):
    class Gap:
        def complete(self,messages,tools):return call('assess_evidence',assessment(state='missing'))
    result=start(system,Gap(),'guarded_v42')
    assert result['status']=='insufficient'
    events=result['events']
    assert sum(e['kind']=='model' for e in events)==2
    assert sum(e['kind']=='tool' and e['payload']['name']=='search' for e in events)==2
    assert any(e['kind']=='policy' and e['payload']['action']=='gap_query_fallback' for e in events)


def test_real_harness_submits_claims_with_host_rendered_evidence(system):
    class Answer:
        def complete(self,messages,tools):
            if tools[0]['function']['name']=='assess_evidence':return call('assess_evidence',assessment())
            return call('submit_grounded',{'claims':[{'text':'住宿标准600元','evidence':['S1:1']}]})
    result=start(system,Answer(),'guarded_v42')
    assert result['status']=='answered' and result['citations'][0]['label']=='S1'


def test_judge_cannot_quote_reference_as_candidate_answer():
    grade={'correctness':1,'claims':[],
       'answer_checks':[{'requirement':'回答期限','satisfied':1,'answer_quote':'7天','reason':'提到期限'}]}
    validate_answer_quotes(grade,{'answer':'7天内完成'})
    with pytest.raises(ValueError,match='实际answer'):validate_answer_quotes(grade,{'answer':'不知道','reference_answer':'7天内完成'})
    grade['answer_checks'][0].update(satisfied=0,answer_quote='')
    with pytest.raises(ValueError,match='未满足'):validate_answer_quotes(grade,{'answer':'不知道'})


def test_v42_controls_separate_correct_answer_from_bad_citation():
    controls=cases('v42')
    assert len(controls)==30 and sum(c['expected_correctness']==0 for c in controls)==15
    positive=next(c for c in controls if c['id']=='quote_separation-positive')
    assert positive['expected_correctness']==1 and positive['expected_faithfulness']==0
    assert positive['gold_evidence'][0]['quote']!=positive['citations'][0]['text']


def test_calibration_rejects_mixing_content_with_bad_citations(tmp_path,monkeypatch):
    from kb.benchmark import calibration
    def grades(jobs,*args):
        return [{'id':j['id'],'correctness':0 if j['id']=='quote_separation-positive' else j['expected_correctness'],
                 'faithfulness':j.get('expected_faithfulness',1),'judgment_status':'ok'} for j in jobs]
    monkeypatch.setattr(calibration,'evaluate_jobs',grades)
    result=calibration.run(tmp_path,tmp_path/'cal','v42')
    assert result['matched']==29 and result['negative_recall']==1 and result['faithfulness_controls']['matched']==2
    assert not result['passed'] and result['content_citation_separation']['matched']==1


def test_v42_judge_uses_structured_output_and_excludes_expected_labels(tmp_path,monkeypatch):
    import httpx
    from kb.benchmark import semantic
    requests=[]
    grade={'correctness':1,'faithfulness':0,'citation_support':0,'rationale':'答案正确但引用不支持',
      'answer_checks':[{'requirement':'归档期限','satisfied':1,'answer_quote':'7天','reason':'明确给出期限'}],
      'claims':[{'claim':'7天归档','answer_quote':'7天','supported':0,'evidence_ids':[],'reason':'引用是食堂位置'}]}
    def handler(request):
        body=json.loads(request.content)
        if request.url.path=='/tokenize':return httpx.Response(200,json={'count':100,'max_model_len':12288})
        requests.append(body)
        fields=body['response_format']['json_schema']['schema']['required']
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({k:grade[k] for k in fields})}}]})
    real=httpx.Client
    monkeypatch.setattr(semantic.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(handler)))
    judge=object.__new__(semantic.Judge)
    judge.url='http://mock/v1';judge.tokenizer='http://mock/tokenize';judge.headers={}
    judge.model='judge';judge.output_tokens=2200;judge.context=12288;judge.rubric_version='v42';judge.system=semantic.SYSTEM_V42
    job=next(j for j in cases('v42') if j['id']=='quote_separation-positive')
    result=judge.grade(job,tmp_path)
    assert result['judgment_status']=='ok' and result['correctness']==1 and result['faithfulness']==0
    assert len(requests)==2 and result['model_calls']==2
    fmt=requests[1]['response_format'];assert fmt['type']=='json_schema'
    schema=fmt['json_schema']['schema']
    assert 'answer_quote' in schema['properties']['claims']['items']['required']
    payload=json.loads(requests[0]['messages'][1]['content'])
    assert 'expected_correctness' not in payload and 'expected_faithfulness' not in payload
    assert 'citations' not in payload and 'reference_answer' in payload
    evidence_payload=json.loads(requests[1]['messages'][1]['content'])
    assert 'citations' in evidence_payload and not {'reference_answer','gold_evidence','semantic_rubric'}&evidence_payload.keys()
