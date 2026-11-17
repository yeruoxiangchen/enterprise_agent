"""Collect response-only SFT targets from actual vLLM token IDs and CRM outcomes.

This is transport/curation code: sampling is vLLM, execution is the native
DeepAgents graph, business verification is the existing executable scorer,
and fitting is TRL. No conversation is reconstructed from a final answer.
"""
from __future__ import annotations

from collections import Counter
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


async def collect_one(sample,supervisor,specialist,root,index,*,seed,max_length=4096):
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
        write_json(Path(root)/'traces'/f'{identifier}.json',record)
        admitted,reason=verified_rows(record,result,max_length=max_length)
        return result,admitted,reason
    finally:client.close()


def coverage(rows):
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
    return {'successful_clean_episodes_by_family':dict(counts),'conditional_branches':dict(branches),
        'missing_families':missing,'passed':not missing and all(branches[b]>=1 for b in ('assigned','unassigned'))}


def check_adapter(path):
    from safetensors.torch import load_file
    tensors=load_file(str(Path(path)/'adapter_model.safetensors'))
    nonzero={k:int(t.count_nonzero())for k,t in tensors.items()if'lora_B'in k}
    if not nonzero or not any(nonzero.values()):raise ValueError('SFT adapter has no nonzero LoRA B weights')
    return {'nonzero_lora_B_tensors':sum(bool(n)for n in nonzero.values()),
            'nonzero_lora_B_values':sum(nonzero.values())}
