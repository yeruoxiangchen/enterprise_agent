"""Collect response-only SFT targets from actual vLLM token IDs and CRM outcomes.

This is transport/curation code: sampling is vLLM, execution is the native
DeepAgents graph, business verification is the existing executable scorer,
and fitting is TRL. No conversation is reconstructed from a final answer.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

from native_rl_diagnostic import WireCapture
from .runtime import run_episode,write_json
from .sft import verified_rows
from .tasks import pack_task


class TokenCapture(WireCapture):
    def callback(self):
        original=super().callback()
        capture=self
        class RoleCapture(type(original)):
            def on_chat_model_start(self,serialized,messages,**kwargs):
                super().on_chat_model_start(serialized,messages,**kwargs)
                capture.local.metadata['rl_role']=(kwargs.get('metadata')or{}).get('rl_role')
        return RoleCapture()


def token_record(sample,result,calls,rollout_id):
    selected=[r for r in calls if r['metadata'].get('rl_role')=='supervisor']
    if len(selected)!=result['policy_model_calls_completed']:
        raise ValueError('Original supervisor responses do not match the role counter')
    triplets=[]
    for row in selected:
        response=row.get('response',{})
        choices=response.get('choices',[])
        if row.get('status_code')!=200 or len(choices)!=1 or not row.get('sdk_matches_provider'):
            raise ValueError('Provider/SDK response mismatch while collecting SFT')
        prompt=response.get('prompt_token_ids')
        tokens=choices[0].get('token_ids')
        if not prompt or not tokens or any(type(t)is not int or t<0 for t in prompt+tokens):
            raise ValueError('vLLM must return the complete original prompt and response token IDs')
        usage=response.get('usage',{})
        if len(prompt)!=usage.get('prompt_tokens') or len(tokens)!=usage.get('completion_tokens'):
            raise ValueError('Original token lengths differ from provider usage')
        triplets.append({'prompt':{'token_ids':prompt},'response':{'token_ids':tokens,
            'finish_reason':choices[0].get('finish_reason')},
            'source':{'request_sha256':row['request_sha256'],'wire_index':row['index']}})
    return {'source_format':'vllm-return-token-ids-v1',
        'rollout':{'rollout_id':rollout_id,'mode':'train','input':pack_task(sample)},
        'triplets':triplets}


async def collect_one(sample,supervisor,specialist,root,index,*,seed,max_length=4096,selection_policy='clean_episode'):
    import httpx
    from langchain_openai import ChatOpenAI
    if sample['split']!='train':raise ValueError('SFT collection accepts the training split only')
    identifier=f'collect-{index:04d}'
    wire=TokenCapture(Path(root)/'wire'/identifier)
    wire.user_messages=sample['messages']
    client=httpx.Client(timeout=65,trust_env=False,
        event_hooks={'request':[wire.request],'response':[wire.response]})
    def factory(**kwargs):
        kwargs['extra_body']={**kwargs['extra_body'],'seed':seed,
            'return_token_ids':kwargs['metadata']['rl_role']=='supervisor'}
        return ChatOpenAI(**kwargs,http_client=client,callbacks=[wire.callback()])
    try:
        folder=Path(root)/'rollouts'/identifier/'0'
        result=await run_episode(sample,supervisor,specialist,folder,temperature=.7,
            train_role='supervisor',delegation_protocol='native',execution_granularity='domain',
            completion_review='errors',model_factory=factory)
        result.update(task_sha256=pack_task(sample)['task_sha256'],sampling_seed=seed)
        write_json(folder/'result.json',result)
        if result['reward']!=1.:
            return result,[],'business_verification_failed'
        record=token_record(sample,result,wire.rows,identifier)
        record['wire_root']=str((Path(root)/'wire'/identifier).resolve())
        write_json(Path(root)/'traces'/f'{identifier}.json',record)
        admitted,reason=verified_rows(record,result,max_length=max_length,selection_policy=selection_policy)
        return result,admitted,reason
    finally:client.close()


def coverage(rows,*,require_branches=True):
    """Require clean examples from every family and both conditional branches."""
    from .tasks import FAMILIES
    counts=Counter();branches=Counter();seen=set()
    for sample,result,admitted in rows:
        if not admitted:continue
        if sample['id']in seen:continue
        seen.add(sample['id'])
        counts[sample['family']]+=1
        if sample['family']=='conditional_reassign':
            assigned=bool(sample['task']['initial_state']['tickets'][sample['target_ticket']]['assigned_to'])
            branches['assigned'if assigned else'unassigned']+=1
    missing=[f for f in FAMILIES if counts[f]<2]
    gaps=[f'conditional_reassign:{b}'for b in ('assigned','unassigned')if branches[b]<1]
    return {'successful_clean_episodes_by_family':dict(counts),'conditional_branches':dict(branches),
        'missing_families':missing,'rl_focus_gaps':gaps,
        'passed':not missing and (not require_branches or not gaps)}


def reuse_collection(source,target,train,*,selection_policy='verified_calls',max_length=4096):
    """Derive targets without changing or resampling an ended collection."""
    from .tasks import DATA_ROOT,unpack_task
    source,target=Path(source).resolve(),Path(target).resolve()
    if not all(p.is_relative_to(DATA_ROOT.resolve())for p in (source,target)):
        raise ValueError('Collection lineage must stay on project data storage')
    state=json.loads((source/'status.json').read_text())
    if not state.get('resources_released'):raise ValueError('Wait for source collection resource release')
    if state.get('train_role')!='supervisor'or state.get('base_model')!=str(DATA_ROOT.parent/'models/Qwen3-8B'):
        raise ValueError('Source collection role or base model differs')
    expected={'delegation_protocol':'native','execution_granularity':'domain','completion_review':'errors'}
    if state.get('profile')!=expected:raise ValueError('Source harness profile differs')
    before=json.loads((source/'source-manifest.json').read_text())
    after=json.loads((target/'source-manifest.json').read_text())
    core=[n for n in before if n.startswith('kb/harness/')or n in(
        'kb/benchmark/crm_native_adapter.py','kb/rl/runtime.py','kb/config.py','kb/context.py',
        'scripts/crm_bench_services.py')]
    if not core or any(before[n]!=after.get(n)for n in core):raise ValueError('Source execution code differs')
    expected_tasks={s['id']:pack_task(s)['task_sha256']for s in train}
    retained=[];lineage=[]
    (target/'traces').mkdir(exist_ok=False);(target/'rollouts').mkdir(exist_ok=False)
    for path in sorted((source/'traces').glob('*.json')):
        raw=path.read_bytes();record=json.loads(raw);sample=unpack_task(record['rollout']['input'])
        if sample['split']!='train'or expected_tasks.get(sample['id'])!=record['rollout']['input']['task_sha256']:
            raise ValueError('Collection example does not match the frozen training split')
        ident=record['rollout']['rollout_id']
        results=list((source/'rollouts'/ident).glob('*/result.json'))
        if len(results)!=1:raise ValueError('Ambiguous original collection attempt')
        result=json.loads(results[0].read_text())
        record['wire_root']=str(source/'wire'/ident)
        record['derived_from']={'trace':str(path),'sha256':hashlib.sha256(raw).hexdigest(),
            'result':str(results[0]),'result_sha256':hashlib.sha256(results[0].read_bytes()).hexdigest()}
        admitted,reason=verified_rows(record,result,max_length=max_length,selection_policy=selection_policy)
        write_json(target/'traces'/path.name,record)
        (target/'rollouts'/ident).symlink_to(source/'rollouts'/ident,target_is_directory=True)
        retained.append((sample,result,admitted))
        lineage.append({'task_id':sample['id'],'rows':len(admitted),'reason':reason,**record['derived_from']})
    report={'source':str(source),'selection_policy':selection_policy,'original_episodes':len(retained),
        'selected_responses':sum(len(r[2])for r in retained),'lineage':lineage}
    write_json(target/'reused-collection.json',report)
    return retained


def check_adapter(path):
    from safetensors.torch import load_file
    tensors=load_file(str(Path(path)/'adapter_model.safetensors'))
    nonzero={k:int(t.count_nonzero())for k,t in tensors.items()if'lora_B'in k}
    if not nonzero or not any(nonzero.values()):raise ValueError('SFT adapter has no nonzero LoRA B weights')
    return {'nonzero_lora_B_tensors':sum(bool(n)for n in nonzero.values()),
            'nonzero_lora_B_values':sum(nonzero.values())}
