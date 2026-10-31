"""Real official Agent Pack loop/tools/hook, with deterministic model responses."""
from dataclasses import replace
import hashlib
import json
import pytest
from haystack import component
from haystack.dataclasses import ChatMessage, ToolCall
from kb.backends.agentpack_rag import AgentPackQA
from kb.backends.haystack_rag import QARequest
from kb.retrieval import tokens
from test_external_rag import engine_data


@component
class Scripted:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.options = []
        self.tool_names = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools=None, generation_kwargs=None):
        self.calls.append(messages)
        self.options.append(generation_kwargs or {})
        self.tool_names.append([t.name for t in (tools or [])])
        value = self.responses[len(self.calls)-1]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(messages)
        if isinstance(value, tuple):
            reply = ChatMessage.from_assistant(tool_calls=[ToolCall(tool_name=value[0], arguments=value[1], id=str(len(self.calls)))])
        else:
            reply = ChatMessage.from_assistant(value)
        reply.meta.update(usage={'prompt_tokens': 10, 'completion_tokens': 5}, finish_reason='stop')
        return {'replies': [reply]}


def build(data, responses, **kwargs):
    cfg, db, mapping = data
    for r in db.all('SELECT c.*,d.filename FROM chunks c JOIN documents d ON c.document_id=d.id'):
        db.execute('INSERT OR REPLACE INTO chunks_fts(rowid,tokens) VALUES(?,?)',
                   (r['id'], ' '.join(tokens(r['filename']+' '+r['text']))))
    gen = Scripted(responses)
    return AgentPackQA(db, replace(cfg, reranker_path=''), mapping, generator=gen,
                       counter=kwargs.pop('counter', lambda m,t: 100), **kwargs), gen


def request(question='Git 凭证缓存多久？', bases=('public',), role='member'):
    return QARequest(question, (), {'role': role}, bases)


REF = hashlib.sha256(b'public:d1:1').hexdigest()[:8]
SEARCH = ('search_documents', {'query': 'Git 凭证缓存'})


def test_official_tools_and_single_answer_with_host_citations(engine_data):
    engine, gen = build(engine_data, [('list_metadata_fields', {}), SEARCH,
                                    f'<answered>\n凭证只在内存缓存十五分钟。 [doc {REF}]'])
    out = engine.answer(request())
    assert out['answer']=='凭证只在内存缓存十五分钟。 [S1]'
    assert out['citations'][0]['document_id']=='d1' and not out['warnings']
    assert out['model_calls']==3 and out['backup_calls']==0
    assert out['prompt_tokens']==30
    assert engine.agent.hooks['after_run'][0].__class__.__name__=='BackupAnswerHook'
    assert gen.options[0]['tool_choice']['function']['name']=='list_metadata_fields'
    assert gen.options[1]['tool_choice']['function']['name']=='search_documents'
    assert gen.options[2]['tool_choice']=='auto'
    assert 'search_documents' in gen.tool_names[0]
    assert 'SECRET_CANARY' not in str([[m.to_dict() for m in call] for call in gen.calls])


@pytest.mark.parametrize('answer,status', [('<insufficient>\n资料没有公司真实网址。','insufficient'),
    ('<clarify>\n请说明标签名称，以及删除本地还是远端标签？','clarify')])
def test_refusal_and_clarification_need_no_claims_tool(engine_data, answer, status):
    engine, _ = build(engine_data, [answer])
    out = engine.answer(request())
    assert out['status']==status and not out['citations'] and not out['warnings']
    assert out['model_calls']==1


def test_upstream_backup_is_metered_and_uses_same_model(engine_data):
    engine, gen = build(engine_data, [SEARCH, f'<answered>十五分钟。 [doc {REF}]'], max_steps=1)
    out = engine.answer(request())
    assert out['model_calls']==2 and out['backup_calls']==1 and len(gen.calls)==2
    assert 'interrupted document-search' in gen.calls[-1][0].text
    assert not gen.tool_names[-1] and 'tool_choice' not in gen.options[-1]
    assert out['answer']=='十五分钟。 [S1]'


def test_unauthorized_scope_fails_before_any_model_request(engine_data):
    engine, gen = build(engine_data, [])
    with pytest.raises(PermissionError): engine.answer(request(bases=('public','private')))
    assert not gen.calls


def test_metadata_and_fetch_cannot_escape_request_scope(engine_data):
    engine, gen = build(engine_data, [('get_metadata_field_values', {'field': 'filename'}),
        ('fetch_documents_by_filter', {'filters': {'field':'meta.knowledge_base','operator':'==','value':'private'}}),
        '<insufficient>没有该资料。'])
    out=engine.answer(request())
    assert not out['hits']
    assert 'SECRET_CANARY' not in str([[m.to_dict() for m in c] for c in gen.calls])
    assert 'private.txt' not in str([[m.to_dict() for m in c] for c in gen.calls])


def test_source_changes_detected_before_return(engine_data):
    cfg,db,_=engine_data
    def change(messages):
        db.execute('UPDATE chunks SET text=? WHERE id=1', ('changed',))
        return f'<answered>十五分钟 [doc {REF}]'
    engine,_=build(engine_data,[SEARCH,change])
    with pytest.raises(RuntimeError,match='source changed'): engine.answer(request())


def test_context_budget_blocks_http_before_reservation(engine_data):
    engine,gen=build(engine_data,[],counter=lambda m,t:999999)
    with pytest.raises(Exception,match='context budget'): engine.answer(request())
    assert not gen.calls and engine.state['calls']==0


def test_failed_call_reserved_and_recorded_without_sensitive_error(engine_data):
    engine,gen=build(engine_data,[RuntimeError('api_key=SECRET')])
    with pytest.raises(Exception): engine.answer(request())
    assert engine.state['calls']==1
    assert 'SECRET' not in json.dumps(engine.state.get('error_chain'))


def test_foreign_reference_cannot_become_application_citation(engine_data):
    engine,_=build(engine_data,[SEARCH,'<answered>答案 [doc deadbeef]'])
    out=engine.answer(request())
    assert not out['citations'] and 'unknown_citation_labels' in out['warnings']


def test_native_refusal_prefix_and_missing_marker_are_not_guessed(engine_data):
    engine,_=build(engine_data,['No matching information was found.'])
    assert engine.answer(request())['status']=='insufficient'
    engine,_=build(engine_data,['请补充信息。'])
    assert engine.answer(request())['status']=='unclassified'


def test_cancel_before_call(engine_data):
    def cancelled(): raise RuntimeError('cancelled')
    engine,gen=build(engine_data,[],cancelled=cancelled)
    with pytest.raises(RuntimeError,match='cancelled'): engine.answer(request())
    assert not gen.calls


def test_app_adapter_finishes_once(system, monkeypatch):
    from test_application import upload,login
    from kb.agent import Harness
    import kb.backends.agentpack_rag as module
    cfg,db,client,base,*_=system
    doc=upload(system)
    chunk=db.one('SELECT * FROM chunks WHERE document_id=?',(doc['id'],))
    ref=hashlib.sha256(f"{base}:{doc['id']}:{chunk['id']}".encode()).hexdigest()[:8]
    gen=Scripted([('search_documents',{'query':'住宿标准'}),f'<answered>住宿标准600元。 [doc {ref}]'])
    original=module.AgentPackQA
    monkeypatch.setattr(module,'AgentPackQA',lambda *a,**k:original(*a,**k,generator=gen,counter=lambda m,t:100))
    login(client)
    rid=client.post('/api/chat',json={'question':'住宿标准','base_ids':[base]}).json()['run_id']
    db.execute("UPDATE runs SET mode='agent' WHERE id=?",(rid,))
    cfg=replace(cfg,agent_policy='agentpack_v1')
    Harness(db,cfg).execute(rid)
    assert db.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='answered'
    Harness(db,cfg).execute(rid)
    assert db.one("SELECT count(*) AS n FROM messages WHERE run_id=? AND role='assistant'",(rid,))['n']==1


def test_diagnostic_gate_retains_quality_failures_but_blocks_leaks_and_no_retrieval(monkeypatch):
    from pathlib import Path
    import copy
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    from eval_agentpack import integration_receipt
    row={'id':'case','execution_status':'complete','tool_calls':2,'hits':[{'id':1}],
         'metrics':{'output_contract_valid':False,'action_correct':False,
                    'forbidden_output':False,'unauthorized_citation':False}}
    receipt=integration_receipt([row])
    assert receipt['passed'] and not receipt['quality_passed']
    assert receipt['quality_checks']=={'case':False}
    bad=copy.deepcopy(row);bad['metrics']['unauthorized_citation']=True
    assert not integration_receipt([bad])['passed']
    bad=copy.deepcopy(row);bad['hits']=[]
    assert not integration_receipt([bad])['passed']
    bad=copy.deepcopy(row);bad['execution_status']='error'
    assert not integration_receipt([bad])['passed']
