import asyncio
from dataclasses import asdict
import hashlib,json
from pathlib import Path
from types import SimpleNamespace

import pytest
import harness_v17_evaluation as evaluation
from harness.bench_runner import ConversationTrace
from kb.benchmark.customer_contract import CustomerContract,ExitCondition


@pytest.fixture
def cached(tmp_path,monkeypatch):
    folder=tmp_path/'runs/new';path=folder/'trials/task/official_graph';path.mkdir(parents=True)
    parent=folder.parent/'old'
    source_root=Path(evaluation.__file__).resolve().parents[1]
    manifest={name:hashlib.sha256((source_root/name).read_bytes()).hexdigest()
        for name in ['kb/benchmark/crm_reference.py','scripts/crm_bench_services.py','kb/context.py']}
    evaluation.write(parent/'source-manifest.json',manifest)
    evaluation.write(parent/'comparison-protocol.json',{})
    trace=ConversationTrace(task_id='task',stopped=True,response_passed=False)
    trace.turns=[{'role':'user','content':'Delete it.'},{'role':'assistant','content':'Deleted.'}]
    row={'arm':'official_graph','task_id':'task','trial':0,'official_verdict':False,'local_verdict':False,
        'trace':asdict(trace),'agent_usage':{'total_tokens':100,'prompt_tokens':90,'completion_tokens':10,
        'num_calls':3,'usage_missing_calls':0},'agent_turn_times':[{'seconds':3}],
        'trial_output':str(parent/'trials/task/official_graph'),'user_usage':{'num_calls':2}}
    original=parent/'rows/task__official_graph.json';evaluation.write(original,row)
    async def close():pass
    monkeypatch.setattr(evaluation,'client',lambda *a,**k:SimpleNamespace(usage={'num_calls':0},aclose=close))
    class Judge:
        async def grade(self,**kwargs):return SimpleNamespace(passed=True,failures=[],audit={'rubric_unchanged':True})
    monkeypatch.setattr(evaluation,'judge',lambda *a:Judge())
    contract=CustomerContract(exit_conditions=[ExitCondition(source_quote='Delete it.',requirement='Agent confirms deletion.')])
    task=SimpleNamespace(id='task',instruction='Delete it.',expected_response_semantic=['Assistant confirms deletion.'])
    return SimpleNamespace(folder=folder,path=path,source=original,task=task,contract=contract,row=row)


def test_valid_cache_regrades_without_reexecuting_actor_or_mutating_old_result(cached,monkeypatch):
    c=cached;old=c.source.read_bytes()
    async def valid(*args):return True,[{'passed':True}]
    monkeypatch.setattr(evaluation,'trace_contract_valid',valid)
    result=asyncio.run(evaluation.cached_reference(c.task,c.path,c.folder,{}, {'reference_from':'old','semantic_review_required':True},c.contract))
    assert result['reference_mode']=='cached_regraded'and result['official_verdict']is True
    assert result['semantic_review_required']is True
    assert result['reference_cache']['original_verdict']is False
    assert result['agent_usage']==c.row['agent_usage']and c.source.read_bytes()==old
    assert result['reference_cache']['sha256']==hashlib.sha256(old).hexdigest()


def test_incomplete_or_fabricated_customer_trajectory_requires_fresh_reference(cached,monkeypatch):
    c=cached
    async def invalid(*args):return False,[{'passed':False,'reason':'The second requested phase is missing.'}]
    monkeypatch.setattr(evaluation,'trace_contract_valid',invalid)
    result=asyncio.run(evaluation.cached_reference(c.task,c.path,c.folder,{}, {'reference_from':'old'},c.contract))
    assert result is None and not(c.path/'result.json').exists()
    assert json.loads((c.path/'cache-disposition.json').read_text())['reuse']is False
