"""Contract/access tests, using real Haystack retrieval and pipeline execution."""
from dataclasses import replace
import json
import os
from types import SimpleNamespace

os.environ['HAYSTACK_TELEMETRY_ENABLED']='false'
import httpx
import pytest
haystack = pytest.importorskip('haystack')
from haystack import component, Document
from haystack.dataclasses import ChatMessage
from haystack.components.builders import AnswerBuilder
from kb.backends.haystack_rag import HaystackRAG, QARequest, ExactBudget, parse_reply
from kb.backends.qanything import QAnythingClient
from kb.config import Settings
from kb.db import Database


@component
class FakeGenerator:
    def __init__(self, text='<answered>默认缓存十五分钟 [S1]。'):
        self.text=text
        self.calls=0
        self.seen=[]

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage]):
        self.calls+=1
        self.seen=messages
        return {'replies':[ChatMessage.from_assistant(self.text,meta={
            'usage':{'prompt_tokens':123,'completion_tokens':12},'finish_reason':'stop'})]}


@pytest.fixture
def engine_data(tmp_path,monkeypatch):
    cfg=Settings(root=tmp_path,llm_model='fake',llm_url='http://127.0.0.1:9999/v1',
                 tokenize_url='',reranker_path='test-only')
    cfg.prepare()
    db=Database(cfg.db_path);db.initialize()
    mapping={}
    for n,base,vis,text in [(1,'public','all','Git 凭证缓存默认十五分钟，仅存内存。'),
                             (2,'private','admin','内部机密凭证缓存 SECRET_CANARY')]:
        db.execute('INSERT INTO bases VALUES(?,?,?,?,?)',(base,base,'',vis,0))
        db.execute("INSERT INTO documents(id,base_id,filename,digest,size,status,created_at,chunk_count) VALUES(?,?,?,?,?,'ready',0,1)",
            (f'd{n}',base,base+'.txt',str(n),len(text)))
        db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)',(n,f'd{n}',0,None,text))
        mapping[n]={'document_id':f'd{n}','start':0,'end':len(text),'text':text,'visibility':vis,'fixture':False}
    import kb.backends.haystack_rag as module
    monkeypatch.setattr(module,'rerank',lambda cfg,q,rows:(rows,{'device':'fake_cpu_for_unit_test'}))
    return cfg,db,mapping


def test_real_pipeline_preserves_sources_and_uses_one_call(engine_data):
    cfg,db,mapping=engine_data
    model=FakeGenerator()
    engine=HaystackRAG(db,cfg,mapping,generator=model,counter=lambda messages,tools:100)
    out=engine.answer(QARequest('凭证缓存多久？',(),{'role':'member'},('public',)))
    assert model.calls==1 and out['execution_status']=='complete'
    assert out['status']=='answered' and out['citations'][0]['document_id']=='d1'
    assert out['citations'][0]['label']=='S1'
    assert out['hits'][0]['text']==mapping[1]['text']
    assert 'SECRET_CANARY' not in ''.join(m.text for m in model.seen)
    assert out['prompt_tokens']==123 and out['completion_tokens']==12


def test_unauthorized_scope_rejected_before_generation(engine_data):
    cfg,db,mapping=engine_data
    model=FakeGenerator()
    engine=HaystackRAG(db,cfg,mapping,generator=model,counter=lambda m,t:100)
    with pytest.raises(PermissionError):
        engine.answer(QARequest('凭证',(),{'role':'member'},('public','private')))
    assert model.calls==0


def test_admin_can_explicitly_retrieve_private(engine_data):
    cfg,db,mapping=engine_data
    engine=HaystackRAG(db,cfg,mapping,generator=FakeGenerator(),counter=lambda m,t:100)
    docs,_=engine.retrieve(QARequest('内部机密',(),{'role':'admin'},('private',)))
    assert docs and all(d.meta['base_id']=='private' for d in docs)


def test_bad_citation_keeps_answer_with_warning(engine_data):
    cfg,db,mapping=engine_data
    model=FakeGenerator('<answered>错误编号 [S99]。')
    engine=HaystackRAG(db,cfg,mapping,generator=model,counter=lambda m,t:100)
    out=engine.answer(QARequest('凭证缓存',(),{'role':'member'},('public',)))
    assert out['execution_status']=='complete' and out['citations']==[]
    assert 'unknown_citation_labels' in out['warnings'] and model.calls==1


def test_budget_blocks_call_and_does_not_truncate_evidence(engine_data):
    cfg,db,mapping=engine_data
    model=FakeGenerator()
    engine=HaystackRAG(db,cfg,mapping,generator=model,counter=lambda m,t:999999)
    with pytest.raises(Exception,match='exceeds'):
        engine.answer(QARequest('凭证缓存',(),{'role':'member'},('public',)))
    assert model.calls==0


def test_action_marker_is_not_invented():
    assert parse_reply('自然语言但没有标记')==('unclassified','自然语言但没有标记')
    assert parse_reply('<clarify>\n请提供标签名称。')==('clarify','请提供标签名称。')


def test_qanything_uses_only_authorized_scopes_and_hides_keys():
    calls=[]
    def handler(request):
        body=json.loads(request.content)
        calls.append(body)
        return httpx.Response(200,json={'code':200,'response':'answer',
            'source_documents':[{'kb_id':'public','file_id':'abc','content':'text'}],
            'retrieval_documents':[],'api_key':'MUST_NOT_RETURN'})
    cfg=Settings(llm_url='http://127.0.0.1:18768/v1',llm_model='qwen',llm_key='SECRET')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter=QAnythingClient('http://127.0.0.1:18777','test_user',cfg,['public'],client)
        with pytest.raises(PermissionError):adapter.chat('question',[],['private'])
        assert calls==[]
        result=adapter.chat('question',[{'role':'user','content':'old'},{'role':'assistant','content':'reply'}],['public'])
        assert calls[0]['history']==[['old','reply']]
        assert not calls[0]['networking'] and not calls[0]['streaming']
        assert 'api_key' not in result and 'SECRET' not in json.dumps(result)


def test_qanything_fails_closed_on_returned_foreign_source():
    def handler(request):
        return httpx.Response(200,json={'code':200,'response':'bad','source_documents':[{'kb_id':'secret'}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter=QAnythingClient('http://127.0.0.1:18777','test',Settings(),['public'],client)
        with pytest.raises(PermissionError):adapter.chat('q',[],['public'])


def test_qanything_rejects_error_envelope_without_echoing_it():
    def handler(request):
        return httpx.Response(200,json={'code':500,'msg':'api_key=SECRET'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter=QAnythingClient('http://127.0.0.1:18777','test',Settings(),[],client)
        with pytest.raises(RuntimeError) as exc:adapter.health()
        assert 'SECRET' not in str(exc.value)


def test_comparison_reports_missing_grades_without_claiming_accuracy(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    from kb.benchmark.sources import write_json,write_jsonl
    from kb.benchmark.scoring import answer_metrics
    path=Path(__file__).parents[1]/'scripts/eval_haystack.py'
    spec=importlib.util.spec_from_file_location('haystack_suite_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'Settings',lambda:SimpleNamespace(root=tmp_path))
    dataset=tmp_path/'datasets/git_support_zh_v1';dataset.mkdir(parents=True)
    case={'id':'case-1','family_id':'case','category':'lookup','question':'q','history':[],
          'reference_answer':'a','expected_action':'insufficient','evidence':[],
          'required_facts':[],'forbidden_strings':[]}
    write_jsonl(dataset/'dev.jsonl',[case])
    row={k:case[k] for k in ('id','family_id','category','question','history')}
    row.update(status='insufficient',answer='资料不足',citations=[],latency_seconds=1,model_calls=1)
    row['metrics']=answer_metrics(case,row)
    config={'dataset_receipt':{'f':'sha'},'split':'dev','llm_model':'same','context_tokens':8192,
            'output_tokens':1400,'temperature':0,'enable_thinking':False}
    root=tmp_path/'experiment';baseline=tmp_path/'baseline'
    for arm in module.ARMS+module.BASELINE_ARMS:
        target=(root/arm) if arm in module.ARMS else (baseline/'pilot'/arm)
        target.mkdir(parents=True)
        write_json(target/'config.json',config)
        write_jsonl(target/'predictions.jsonl',[{**row,'arm':arm}])
    module.comparison(root,baseline,['case-1'])
    result=json.loads((root/'comparison.json').read_text())
    assert result['semantic_diagnostic'][module.ARMS[0]]['status']=='pending'
    assert result['production_released'] is False
    assert len((root/'human_review.jsonl').read_text().splitlines())==5
    write_json(baseline/'pilot'/module.BASELINE_ARMS[0]/'config.json',{**config,'llm_model':'different'})
    with pytest.raises(ValueError,match='Baseline mismatch'):
        module.comparison(root,baseline,['case-1'])
