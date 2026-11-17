"""Reuse zero-update model behavior after a documented grading correction."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from .tasks import DATA_ROOT,BENCH_ROOT,pack_task,environment_for
from .reward import score_task
from .runtime import write_json


def policy_input_hash(sample):
    payload={'messages':sample['messages'],'initial_state':sample['task']['initial_state']}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def rescore(sample,original):
    """Read the actual calls/effects/state; execute no model or business writes."""
    import yaml
    from kb.harness.ports import CRMPort
    port=CRMPort(environment_for(sample),
        yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
        (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text())
    adapter=SimpleNamespace(tool_calls=original['audit'],
        environment=SimpleNamespace(get_state=lambda:original['environment']),
        engine=SimpleNamespace(specs={spec.name:spec for spec in port.tools()},
            journal=SimpleNamespace(records=lambda:original['effects'])))
    return score_task(sample,adapter,error=original['error'])


def inherit_baseline(source,output,dataset,*,limit,train_role,policy_model=None,
                     delegation_protocol='explicit',completion_review='none'):
    source,output,dataset=map(lambda p:Path(p).resolve(),(source,output,dataset))
    if not all(path.is_relative_to(DATA_ROOT) for path in (source,output,dataset)):
        raise ValueError('Baseline lineage must stay on project data storage')
    old_state=json.loads((source/'status.json').read_text())
    original_model='/data/zjr/enterprise_kb_agent/models/Qwen3-8B'
    if old_state.get('policy_model',original_model)!=(policy_model or original_model):
        raise ValueError('Policy initialization changed; run a new live baseline')
    if (old_state.get('delegation_protocol','explicit')!=delegation_protocol or
            old_state.get('completion_review','none')!=completion_review):
        raise ValueError('Harness profile changed; run a new live baseline')
    if not old_state['resources_released'] or old_state['train_role']!=train_role:
        raise ValueError('Release the source job and preserve its trainable role')
    if old_state.get('trace_export_exit')!=0:
        raise ValueError('Source baseline needs exported actual token traces')
    before=json.loads((source/'source-manifest.json').read_text())
    after=json.loads((output/'source-manifest.json').read_text())
    core=[name for name in before if name.startswith('kb/harness/') or name in (
        'kb/benchmark/crm_native_adapter.py','kb/config.py','kb/context.py',
        'kb/rl/runtime.py','kb/rl/agent.py')]
    if not core or any(before[name]!=after.get(name) for name in core):
        raise ValueError('Model execution code changed; run a new live baseline')
    old_dataset=DATA_ROOT/'datasets'/old_state['dataset']
    old_manifest=json.loads((source/'dataset-manifest.json').read_text())
    raw=(old_dataset/'dev.jsonl').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=old_manifest['splits']['dev']['sha256']:
        raise ValueError('Source development fixtures changed')
    old_samples={row['id']:row for row in map(json.loads,raw.splitlines())}
    samples=list(map(json.loads,(dataset/'dev.jsonl').read_bytes().splitlines()))
    if limit:samples=samples[:limit]
    expected={policy_input_hash(row):row for row in samples}
    if len(expected)!=len(samples):raise ValueError('Ambiguous baseline task identity')
    inherited=[]
    seen=set()
    for path in sorted((source/'rollouts').glob('*/*/result.json')):
        original=json.loads(path.read_text())
        phase=original.get('evaluation_phase') or {}
        if original['split']!='dev' or phase.get('kind')!='baseline':continue
        if phase['global_step']!=0 or phase['completed_updates']!=0:
            raise ValueError('Inherited baseline had already updated the policy')
        old=old_samples[original['task_id']]
        if original['task_sha256']!=pack_task(old)['task_sha256']:
            raise ValueError('Source result is bound to a different task payload')
        key=policy_input_hash(old)
        if key in seen:raise ValueError('Duplicate source baseline case')
        if key not in expected:continue
        seen.add(key)
        sample=expected.pop(key)
        corrected=copy.deepcopy(original)
        corrected.update(rescore(sample,original),task_id=sample['id'],
                         task_sha256=pack_task(sample)['task_sha256'])
        lineage={'source_result':str(path),'source_result_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                 'source_reward':original['reward'],'source_task_id':original['task_id'],
                 'policy_input_sha256':key,'model_execution_code_matches':True}
        corrected['baseline_lineage']=lineage
        target=output/'inherited-baseline'/path.parent.parent.name/path.parent.name/'result.json'
        write_json(target,corrected)
        inherited.append({'task_id':sample['id'],**lineage,'corrected_reward':corrected['reward']})
    if expected:raise ValueError(f'Incomplete inherited baseline: {len(inherited)}/{len(samples)}')
    report={'source':str(source),'count':len(inherited),
        'model_execution_code_matches':True,'model_inputs_match':True,
        'grade_corrections':[row for row in inherited if row['source_reward']!=row['corrected_reward']],
        'rows':inherited}
    write_json(output/'baseline-reuse.json',report)
    return report
