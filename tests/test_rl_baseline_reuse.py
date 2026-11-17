"""Correct a grader without inventing behavior or rerunning the same baseline."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
import yaml

from kb.rl.tasks import BENCH_ROOT,make_task,FAMILIES,environment_for,pack_task
from kb.rl.baseline import policy_input_hash,rescore,inherit_baseline
from kb.rl.reward import score_task
from kb.harness.ports import CRMPort


@pytest.fixture
def fixture(tmp_path):
    sample=make_task('dev',FAMILIES.index('assess_only'))
    old=copy.deepcopy(sample)
    old['id']=old['task']['id']=sample['id'].replace('business-rl-v4','business-rl-v3')
    old['task']['dataset_version']='business-rl-v3'
    old['task']['expected_order']=['get_ticket','check_permission','check_dependencies']
    env=environment_for(sample)
    port=CRMPort(env,yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
                 (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text())
    port.begin_request(sample['messages'][0],'0')
    for name in ('get_ticket','check_dependencies','check_permission'):
        port.execute(name,sample['task']['expected_args'][name],name)
    adapter=SimpleNamespace(environment=env,tool_calls=port.audit,
        engine=SimpleNamespace(specs={spec.name:spec for spec in port.tools()},
                               journal=SimpleNamespace(records=lambda:[])))
    original={**score_task(old,adapter),'task_id':old['id'],'split':'dev',
        'audit':copy.deepcopy(port.audit),'effects':[],'environment':copy.deepcopy(env.get_state()),
        'task_sha256':pack_task(old)['task_sha256'],
        'evaluation_phase':{'kind':'baseline','global_step':0,'completed_updates':0}}
    assert original['reward']==0 and original['checks']['order'] is False
    return sample,old,original


def test_read_only_assessment_accepts_both_check_orders_without_rewriting_behavior(fixture):
    sample,old,original=fixture
    before=copy.deepcopy(original)
    graded=rescore(sample,original)
    assert graded['reward']==1
    assert original==before and policy_input_hash(sample)==policy_input_hash(old)
    wrong=copy.deepcopy(original)
    wrong['audit'][2]['arguments']['action']='view'
    assert rescore(sample,wrong)['reward']==0


@pytest.mark.parametrize('problem',[None,'changed_input','changed_runtime','wrong_task','updated_policy','changed_policy'])
def test_reuse_checks_immutable_source_input_runtime_and_policy(tmp_path,fixture,monkeypatch,problem):
    import kb.rl.baseline as baseline
    sample,old,original=fixture
    # The fixture stays on the project's /data test directory.
    monkeypatch.setattr(baseline,'DATA_ROOT',tmp_path)
    source=tmp_path/'outputs/old'
    output=tmp_path/'outputs/new'
    dataset=tmp_path/'datasets/new'
    old_dataset=tmp_path/'datasets/old'
    for folder in (source,output,dataset,old_dataset):folder.mkdir(parents=True)
    def write(path,value):path.write_text(json.dumps(value))
    old_raw=(json.dumps(old)+'\n').encode()
    (old_dataset/'dev.jsonl').write_bytes(old_raw)
    write(source/'dataset-manifest.json',{'splits':{'dev':{'sha256':hashlib.sha256(old_raw).hexdigest()}}})
    write(source/'status.json',{'resources_released':True,'train_role':'specialists','trace_export_exit':0,'dataset':'old'})
    write(source/'source-manifest.json',{'kb/rl/runtime.py':'same'})
    write(output/'source-manifest.json',{'kb/rl/runtime.py':'different' if problem=='changed_runtime' else 'same'})
    if problem=='changed_input':sample['messages'][0]+=' Make another change.'
    if problem=='wrong_task':original['task_sha256']='wrong'
    if problem=='updated_policy':original['evaluation_phase']['completed_updates']=1
    (dataset/'dev.jsonl').write_text(json.dumps(sample)+'\n')
    path=source/'rollouts/ro-one/at-one/result.json';path.parent.mkdir(parents=True)
    write(path,original)
    if problem:
        with pytest.raises(ValueError):inherit_baseline(source,output,dataset,limit=1,train_role='specialists',
            policy_model='different_sft_base' if problem=='changed_policy' else None)
    else:
        report=inherit_baseline(source,output,dataset,limit=1,train_role='specialists')
        assert report['count']==1 and len(report['grade_corrections'])==1
        inherited=json.loads(next((output/'inherited-baseline').glob('*/*/result.json')).read_text())
        assert inherited['reward']==1 and inherited['baseline_lineage']['source_reward']==0
