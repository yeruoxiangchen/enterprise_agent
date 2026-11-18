"""Paired business outcomes, inference cost and actual learning evidence."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics

from .runtime import write_json
from .tasks import DATA_ROOT, FAMILIES, pack_task


def percentile(values, fraction):
    values=sorted(values)
    position=(len(values)-1)*fraction
    low=math.floor(position)
    high=math.ceil(position)
    return values[low]+(values[high]-values[low])*(position-low)


def outcome_stats(rows):
    return {'passed':sum(row['reward']==1 for row in rows),'total':len(rows),
            'success_rate':statistics.mean(row['reward'] for row in rows),
            'execution_errors':sum(row['error'] is not None for row in rows),
            'mean_seconds':statistics.mean(row['seconds'] for row in rows),
            'p95_seconds':percentile([row['seconds'] for row in rows],.95),
            'mean_tokens':statistics.mean(row['tokens'] for row in rows),
            'mean_model_calls':statistics.mean(row['model_calls'] for row in rows),
            'forbidden_attempt_episodes':sum(not row['checks'].get('forbidden_attempts',True) for row in rows),
            'order_only_failures':sum([key for key,value in row['checks'].items() if not value]==['order'] for row in rows),
            'usage_missing_calls':sum(row['usage_missing_calls'] for row in rows)}


def compare_results(records, expected, *, steps):
    """Require one baseline and one trained outcome for every frozen task."""
    arms={'baseline':{},'trained':{}}
    for row in records:
        if row['split']!='dev':continue
        phase=row.get('evaluation_phase') or {}
        kind=phase.get('kind')
        if kind not in arms:
            raise ValueError('Development outcome has no evaluation phase')
        task_id=row['task_id']
        if task_id not in expected or row.get('task_sha256')!=expected[task_id]:
            raise ValueError('Development task bytes differ from the frozen protocol')
        if task_id in arms[kind]:
            raise ValueError('Duplicate task in one evaluation arm')
        if kind=='baseline' and (phase['global_step']!=0 or phase['completed_updates']!=0):
            raise ValueError('Baseline was evaluated after a policy update')
        if kind=='trained' and (phase['global_step']!=steps or phase['completed_updates']<=0):
            raise ValueError('Trained arm was not evaluated at the final training step')
        if row['reward'] not in (0.,1.):
            raise ValueError('Expected the frozen binary outcome reward')
        arms[kind][task_id]=row
    for kind,rows in arms.items():
        if rows.keys()!=expected.keys():
            raise ValueError(f'Incomplete {kind} evaluation: {len(rows)}/{len(expected)}')
    paired=[]
    families=defaultdict(lambda:{'baseline':[],'trained':[]})
    for task_id in expected:
        before,after=arms['baseline'][task_id],arms['trained'][task_id]
        if any(before[key]!=after[key] for key in ('family','train_role','supervisor','specialist')):
            raise ValueError('Role, decoding or family changed between evaluation arms')
        if before.get('harness_profile')!=after.get('harness_profile'):
            raise ValueError('Harness profile changed between evaluation arms')
        for kind,row in (('baseline',before),('trained',after)):
            families[row['family']][kind].append(row)
        paired.append({'task_id':task_id,'family':before['family'],
            'baseline':before['reward'],'trained':after['reward'],
            'baseline_failed_checks':[key for key,value in before['checks'].items() if not value],
            'trained_failed_checks':[key for key,value in after['checks'].items() if not value]})
    wins=sum(row['trained']>row['baseline'] for row in paired)
    losses=sum(row['trained']<row['baseline'] for row in paired)
    discordant=wins+losses
    p_value=min(1.,2*sum(math.comb(discordant,k) for k in range(min(wins,losses)+1))/2**discordant) if discordant else 1.
    stats={kind:outcome_stats(list(rows.values())) for kind,rows in arms.items()}
    return {'arms':stats,'families':{name:{kind:outcome_stats(rows) for kind,rows in group.items()}
        for name,group in families.items()},'paired':{'wins':wins,'regressions':losses,
        'unchanged':len(paired)-discordant,'exact_mcnemar_p':p_value},
        'success_delta_percentage_points':100*(stats['trained']['success_rate']-stats['baseline']['success_rate']),
        'cases':paired}


def review(folder):
    folder=Path(folder).resolve()
    if not folder.is_relative_to(DATA_ROOT):raise ValueError('Review project data storage only')
    state=json.loads((folder/'status.json').read_text())
    if state['status']!='complete_review_required' or not state['resources_released']:
        raise ValueError('Wait for successful completion and owned-resource release')
    manifest=json.loads((folder/'dataset-manifest.json').read_text())
    dataset=DATA_ROOT/'datasets'/state['dataset']
    import hashlib
    raw=(dataset/'dev.jsonl').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['splits']['dev']['sha256']:
        raise ValueError('Frozen development file changed')
    samples=[json.loads(line) for line in raw.splitlines()]
    if state['val_limit']:samples=samples[:state['val_limit']]
    expected={sample['id']:pack_task(sample)['task_sha256'] for sample in samples}
    records=[]
    paths=[]
    for path in sorted((folder/'rollouts').glob('*/*/result.json')):
        row=json.loads(path.read_text())
        records.append(row)
        paths.append(path)
    inherited=list(sorted((folder/'inherited-baseline').glob('*/*/result.json')))
    for path in inherited:
        row=json.loads(path.read_text())
        lineage=row['baseline_lineage']
        source=Path(lineage['source_result'])
        if hashlib.sha256(source.read_bytes()).hexdigest()!=lineage['source_result_sha256']:
            raise ValueError('Inherited raw baseline changed')
        from .baseline import rescore
        sample=next(sample for sample in samples if sample['id']==row['task_id'])
        corrected=rescore(sample,json.loads(source.read_text()))
        if any(corrected[key]!=row[key] for key in ('reward','checks','failures')):
            raise ValueError('Inherited grading differs from actual source behavior')
        records.append(row)
        paths.append(path)
    compared=compare_results(records,expected,steps=state['steps'])
    training=[row for row in records if row['split']=='train']
    train_raw=(dataset/'train.jsonl').read_bytes()
    if hashlib.sha256(train_raw).hexdigest()!=manifest['splits']['train']['sha256']:
        raise ValueError('Frozen training file changed')
    train_expected={sample['id']:pack_task(sample)['task_sha256']
        for sample in (json.loads(line) for line in train_raw.splitlines())}
    if len(training)!=state['steps']*state['batch_size']*state['group_size']:
        raise ValueError('Training trajectory count differs from the frozen budget')
    if any(row['task_id'] not in train_expected or
           row.get('task_sha256')!=train_expected[row['task_id']] for row in training):
        raise ValueError('Training trajectory was not from the frozen training split')
    summary=json.loads((folder/'trajectory-summary.json').read_text())
    traces={row['rollout_id']:row for row in summary['rows']}
    source_traces={}
    token_checks=[]
    for path,row in zip(paths,records,strict=True):
        lineage=row.get('baseline_lineage')
        trace_rows=traces
        expected_reward=row['reward']
        if lineage:
            source_run=Path(lineage['source_result']).parents[3]
            if source_run not in source_traces:
                original_summary=json.loads((source_run/'trajectory-summary.json').read_text())
                source_traces[source_run]={item['rollout_id']:item for item in original_summary['rows']}
            trace_rows=source_traces[source_run]
            expected_reward=lineage['source_reward']
        trace=trace_rows.get(path.parent.parent.name)
        completed=row['policy_model_calls_completed']
        token_checks.append(bool(trace and trace['reward']==expected_reward
            and trace['triplets']==completed
            and (trace['token_ids_complete'] if completed else trace['token_ids_complete'] is None)))
    if not all(token_checks):raise ValueError('Role counts, actual token traces or reward attribution differ')
    history=[json.loads(line) for line in (folder/'policy-update-history.jsonl').read_text().splitlines()]
    if not 0<len(history)<=state['steps'] or len(history)!=state['completed_updates']:
        raise ValueError('Update history differs from the actual completed-update counter')
    serving=[json.loads(line) for line in (folder/'served-policy.jsonl').read_text().splitlines()]
    versions=sorted({row['completed_updates'] for row in serving})
    if versions!=list(range(len(history)+1)):
        raise ValueError('A policy version was never sampled through the actual adapter')
    if any(row['adapter_id'] is None for row in serving if row['completed_updates']):
        raise ValueError('An updated-policy request used the base model')
    def numbers(value):
        if isinstance(value,(list,tuple)):
            for item in value:yield from numbers(item)
        else:yield float(value)
    gradients=[number for row in history
        for number in numbers(row['metrics'].get('actor/grad_norm',[]))]
    if not gradients or not all(math.isfinite(number) for number in gradients):
        raise ValueError('Missing or non-finite actual training gradients')
    from safetensors import safe_open
    import torch
    adapter=folder/'checkpoints'/f'global_step_{state["steps"]}'/'actor/lora_adapter/adapter_model.safetensors'
    count=nonzero=0
    squared_norm=0.
    with safe_open(adapter,framework='pt',device='cpu') as weights:
        for key in weights.keys():
            if 'lora_B' not in key:continue
            tensor=weights.get_tensor(key).float()
            count+=1
            nonzero+=int(torch.count_nonzero(tensor))
            squared_norm+=float(torch.sum(tensor.double().square()))
    report={'status':'reviewed_paired_pilot','dataset':state['dataset'],
        'inherited_baseline_cases':len(inherited),
        'baseline_grade_corrections':len(json.loads((folder/'baseline-reuse.json').read_text())['grade_corrections']) if inherited else 0,
        'training_code_commit':json.loads((folder/'code-provenance.json').read_text()).get('commit') if (folder/'code-provenance.json').exists() else None,
        'training_source_manifest_sha256':hashlib.sha256((folder/'source-manifest.json').read_bytes()).hexdigest(),
        'reviewer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'train_role':state['train_role'],'algorithm':state['adv_estimator'],
        'steps':state['steps'],'physical_gpus':state['gpus'],
        'train':outcome_stats(training),**compared,
        'training_family_coverage':{family:{
            'trajectories':sum(row['family']==family for row in training),
            'passed':sum(row['family']==family and row['reward']==1 for row in training),
            'expert_participated':sum(row['family']==family and row['policy_model_calls_completed']>0 for row in training),
            'successful_expert_episodes':sum(row['family']==family and row['policy_model_calls_completed']>0 and row['reward']==1 for row in training)
            } for family in FAMILIES},
        'trace_integrity':{'checked':len(records),'matched':sum(token_checks)},
        'training_data_integrity':{'checked':len(training),'matched':len(training),
                                   'development_tasks_in_optimizer':0},
        'learning':{'update_rpcs':len(history),
            'logical_steps_without_update':state['steps']-len(history),
            'rpcs_with_outcome_advantage':sum(row['learning_signal']['nonzero_tokens']>0 for row in history),
            'rpcs_with_zero_outcome_advantage':sum(row['learning_signal']['nonzero_tokens']==0 for row in history),
            'optimizer_minibatch_updates':len(gradients),
            'gradient_norm_min':min(gradients),'gradient_norm_max':max(gradients),
            'positive_advantage_tokens':sum(row['learning_signal']['positive_tokens'] for row in history),
            'negative_advantage_tokens':sum(row['learning_signal']['negative_tokens'] for row in history),
            'lora_B_tensors':count,'lora_B_nonzero_elements':nonzero,
            'lora_B_l2':math.sqrt(squared_norm),'sampled_policy_versions':versions},
        'elapsed_minutes':(state['finished_at']-state['started_at'])/60,
        'resources_released':state['resources_released'],
        'scope':'paired authored business-development tasks; executable outcomes, no final-answer semantic score'}
    write_json(folder/'review.json',report)
    failures=[]
    for path,row in zip(paths,records,strict=True):
        if row['split']!='dev' or row['reward']:continue
        event_folder=Path(row['baseline_lineage']['source_result']).parent if row.get('baseline_lineage') else path.parent
        events=[json.loads(line) for line in (event_folder/'events.jsonl').read_text().splitlines()]
        failures.append({'task_id':row['task_id'],'family':row['family'],
            'phase':row['evaluation_phase'],'failed_checks':{
                key:value for key,value in row['checks'].items() if not value},
            'delegations':[event['data'] for event in events if event['kind']=='delegation_bound'],
            'model_tool_schemas':[event['data']['tools'] for event in events if event['kind']=='model_request'],
            'actual_tool_calls':row['audit'],'conversation':row['turns'],'error':row['error']})
    write_json(folder/'failure-details.json',failures)
    role_description={'supervisor':'主管 LoRA；固定专家与复核器',
                      'specialists':'专家共享 LoRA；固定主管与复核器'}[state['train_role']]
    lines=['# 业务 Agent RL 同题对照复测','',
        f'数据：{state["dataset"]}；{role_description}；上游 {state["adv_estimator"]}。',
        f'训练 {state["steps"]} 轮，{len(training)} 条真实轨迹；完整开发集 {len(expected)} 题，训练前后各一次。','',
        '|任务类别|训练前|训练后|','|---|---:|---:|']
    for name,group in report['families'].items():
        lines.append(f'|{name}|{group["baseline"]["passed"]}/{group["baseline"]["total"]}|{group["trained"]["passed"]}/{group["trained"]["total"]}|')
    lines+=['','|指标|训练前|训练后|','|---|---:|---:|']
    for label,key in [('成功率','success_rate'),('平均处理秒数','mean_seconds'),
                      ('处理秒数 p95','p95_seconds'),('平均 token','mean_tokens'),('平均模型调用','mean_model_calls')]:
        lines.append(f'|{label}|{report["arms"]["baseline"][key]:.4f}|{report["arms"]["trained"][key]:.4f}|')
    lines+=['',f'逐题改善 {wins_text(report)}。',
        f'轨迹、奖励与实际策略调用核对：{sum(token_checks)}/{len(records)}；LoRA B L2={math.sqrt(squared_norm):.8f}。',
        '评分保持二值业务结果、必要检查、正确参数和顺序、禁止尝试及无关状态不变；自然语言答复语义另行评估。',
        '本轮任务来自参数化业务环境，结果不折算为公开 ITSM66 成绩。','']
    if inherited:
        lines+= [f'训练前基线复用 {len(inherited)} 条零更新时的原始行为，已核对执行代码、请求及初始状态；'
                 f'评分校准 {report["baseline_grade_corrections"]} 题，单独记录在 baseline-reuse.json。',
                 '复用结果保留原始来源哈希、原始奖励和修正后评分，未进入优化器。','']
    (folder/'comparison.md').write_text('\n'.join(lines))
    return report


def wins_text(report):
    paired=report['paired']
    return f'{paired["wins"]} 题，退步 {paired["regressions"]} 题，持平 {paired["unchanged"]} 题'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',type=Path,required=True)
    args=parser.parse_args()
    report=review(args.run_dir)
    print(json.dumps({key:report[key] for key in ('status','arms','paired','learning','elapsed_minutes')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
