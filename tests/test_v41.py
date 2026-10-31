import json
from dataclasses import replace
import httpx
import pytest
from kb.agent import ModelClient, Harness
from kb.config import Settings
from kb.model_errors import ModelServiceError
from kb.guarded_v41 import ASSESS, resolve_evidence, clarification
from kb.benchmark.calibration import cases
from test_v4 import memory, Grounded
from test_v3 import start


def test_request_timeout_can_be_frozen_per_experiment(monkeypatch):
    monkeypatch.setenv('KB_LLM_TIMEOUT_SECONDS','120')
    assert Settings().timeout==120


def test_evidence_bound_is_visible_and_errors_actionable():
    prop=ASSESS['function']['parameters']['properties']['requirements']['items']['properties']['evidence']
    assert prop['maxItems']==8
    state=memory()
    with pytest.raises(ValueError,match='数量过多'):resolve_evidence(['S1:1']*9,state)
    with pytest.raises(ValueError,match='编号不存在'):resolve_evidence(['S99:2'],state)
    with pytest.raises(ValueError,match='重复'):resolve_evidence(['S1:1','S1:1'],state)
    assert resolve_evidence(['S1:2'],state)[0]['text']=='报销期限15天'


def test_clarification_allows_git_topic_but_not_unconfirmed_commands():
    assert 'Git 服务器' in clarification(['您指的是哪个 Git 服务器？'])
    for s in ['运行 git tag -d a 后再确认可以吗？','请先尝试删除本地标签可以吗？','先执行 curl /delete 可以吗？']:
        with pytest.raises(ValueError):clarification([s])


@pytest.mark.parametrize('policy',['guarded_v4','guarded_v41'])
def test_named_tool_choice_is_versioned(tmp_path,monkeypatch,policy):
    requests=[]
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[]},'finish_reason':'length'}]})
    real=httpx.Client
    monkeypatch.setattr('kb.agent.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(handler)))
    cfg=Settings(root=tmp_path,llm_url='http://mock/v1',agent_policy=policy)
    _,usage=ModelClient(cfg).complete([],tools=[ASSESS])
    assert usage['finish_reason']=='length'
    expected={'type':'function','function':{'name':'assess_evidence'}} if policy=='guarded_v41' else 'required'
    assert requests[0]['tool_choice']==expected


def test_http_400_logs_diagnostic_not_key_and_is_not_retried(tmp_path,monkeypatch):
    key='private-fixture-key';requests=[]
    def handler(req):
        requests.append(req)
        return httpx.Response(400,json={'error':{'type':'BadRequestError','message':'Invalid JSON: EOF while parsing '+key+' Bearer other-secret'}})
    real=httpx.Client
    monkeypatch.setattr('kb.agent.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(handler)))
    cfg=Settings(root=tmp_path,llm_url='http://mock/v1',llm_key=key)
    with pytest.raises(ModelServiceError,match='tool_output_invalid_json'):ModelClient(cfg).complete([])
    raw=(tmp_path/'logs/model_errors.jsonl').read_text()
    assert key not in raw and 'other-secret' not in raw and len(requests)==1
    assert json.loads(raw)['request_headers_recorded'] is False


def test_truncated_call_is_not_executed_and_can_repair(system):
    class Truncated(Grounded):
        def complete(self,messages,tools):
            if self.calls==0:
                self.calls+=1
                return {'role':'assistant','content':'incomplete'}, {'finish_reason':'length'}
            assert '长度上限' in json.dumps(messages,ensure_ascii=False) or self.calls>1
            return super().complete(messages,tools)
    model=Truncated();result=start(system,model,'guarded_v41')
    assert result['status']=='answered' and model.calls==3
    assert sum(e['kind']=='assessment' for e in result['events'])==1


def test_v41_rejects_v4_checkpoint(system):
    class Crash(Grounded):
        def complete(self,*args,**kwargs):raise SystemExit('crash')
    with pytest.raises(SystemExit):start(system,Crash(),'guarded_v4')
    cfg,db,client,*_=system;row=db.one("SELECT * FROM runs WHERE status='running'")
    db.execute("UPDATE runs SET status='queued' WHERE id=?",(row['id'],))
    cfg.agent_policy='guarded_v41';Harness(db,cfg,Grounded()).execute(row['id'])
    result=client.get('/api/runs/'+row['id']).json()
    assert result['status']=='error' and '跨策略' in result['error']


def test_calibration_keeps_original_controls_and_adds_positive_negative_pairs():
    old,new=cases(),cases('v41')
    assert new[:20]==old and len(new)==26
    assert sum(c['expected_correctness']==0 for c in new)==13
