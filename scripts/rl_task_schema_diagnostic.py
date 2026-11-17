"""One-factor, no-training task-schema comparison on frozen business fixtures.

Reuse native DeepAgents, actual CRM execution/scoring, the local model lifecycle
and raw-wire capture. The optional arm removes only the write-name enum from
the advertised task schema; persisted scopes and execution guards stay enabled.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
from threading import Lock
import time

import httpx

from crm_bench_services import ModelServices, gpu_snapshot, identity
from native_rl_diagnostic import WireCapture
from kb.rl.runtime import write_json
from kb.rl.tasks import DATA_ROOT, BENCH_ROOT, environment_for, pack_task
from kb.rl.reward import score_task
from kb.rl.review import outcome_stats
from kb.rl.role_usage import RoleUsage


async def case(sample, arm, folder, service, deadline):
    import yaml
    from langchain_openai import ChatOpenAI
    from kb.benchmark.crm_native_adapter import NativeCRMAdapter
    from kb.config import Settings
    folder.mkdir(parents=True, exist_ok=False)
    wire = WireCapture(folder/'wire')
    wire.user_messages = sample['messages']
    counts, lock = {}, Lock()
    client = httpx.Client(timeout=65, trust_env=False,
        event_hooks={'request':[wire.request], 'response':[wire.response]})

    def model(role, temperature):
        return ChatOpenAI(model=service['model'], base_url=service['url']+'/v1', api_key='local',
            temperature=temperature, max_tokens=512, timeout=60, max_retries=0, http_client=client,
            metadata={'rl_role':role,'rl_task_id':sample['id']},
            callbacks=[wire.callback(),RoleUsage(role,counts,lock)],
            extra_body={'chat_template_kwargs':{'enable_thinking':False},'seed':20261117})

    cfg = Settings(root=folder, mode='agent', llm_url=service['url']+'/v1',
        llm_model=service['model'],llm_key='local',tokenize_url=service['url']+'/tokenize',
        tokenize_model=service['model'],enable_thinking=False, context_tokens=8192,
        output_tokens=512,timeout=60,harness_model_calls=24,harness_tool_calls=32)
    environment = environment_for(sample)
    adapter = NativeCRMAdapter(environment, cfg,
        yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
        (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text(),folder,
        model=model('frozen_supervisor',0),role_models={'*':model('specialist_base',.7)},
        fork_subagents=True,operation_permissions=True,prepare_operations=True,
        specialist_owned_checks=True,require_dependency_confirmation=True,bind_user_request=True)
    started, turns, error = time.monotonic(), [], None
    try:
        for text in sample['messages']:
            reply = await asyncio.wait_for(adapter.run(text,turns),
                timeout=max(.01,min(180,deadline-time.monotonic())))
            turns.extend([{'role':'user','content':text},{'role':'assistant','content':reply.content}])
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        result = {**score_task(sample,adapter,error=error),'task_id':sample['id'],
            'task_sha256':pack_task(sample)['task_sha256'],'family':sample['family'],'split':sample['split'],
            'arm':arm,'error':error,'seconds':time.monotonic()-started,'turns':turns,
            'model_calls':adapter.model_calls,'tokens':adapter.prompt_tokens+adapter.completion_tokens,
            'usage_missing_calls':adapter.usage_missing_calls,'role_model_calls':counts,
            'policy_model_calls_completed':counts['specialist_base']['completed'],
            'audit':adapter.tool_calls,'effects':adapter.engine.journal.records(),
            'environment':environment.get_state(),'wire_sdk_matches':all(
                row.get('sdk_matches_provider') is True for row in wire.rows)}
        write_json(folder/'result.json',result)
        await adapter.aclose()
        client.close()
    return result


async def run(args):
    import kb.harness.native as native
    root = DATA_ROOT/'outputs'/args.tag
    root.mkdir(parents=True,exist_ok=False)
    state = {'status':'running','stage':'preflight','pid':os.getpid(),
        'start_ticks':identity(os.getpid()),'started_at':time.time(),'completed':0,'total':16,
        'gpu':args.gpu,'max_minutes':args.max_minutes,'resources_released':False}
    deadline = time.monotonic()+args.max_minutes*60

    def progress(stage,**extra):
        state.update(stage=stage,updated_at=time.time(),**extra)
        write_json(root/'status.json',state)

    def interrupt(signum,frame):
        raise KeyboardInterrupt('Requested stop')
    signal.signal(signal.SIGTERM,interrupt)
    services = ModelServices(root,progress)
    lease = None
    rows = []
    original = native.task_schema

    def no_enum(original_schema,writes):
        schema = original(original_schema,writes)
        schema['properties']['allowed_write_tools']['items'].pop('enum',None)
        return schema

    samples = [json.loads(line)for line in (DATA_ROOT/'datasets/business-v4/dev.jsonl').read_text().splitlines()][:8]
    write_json(root/'protocol.json',{'purpose':'Does the write-name enum cause supervisor routing regressions?',
        'arms':['current_enum','no_enum'],'model':'Qwen3-8B base, no adapter or training',
        'supervisor_temperature':0,'specialist_temperature':.7,'request_seed':20261117,
        'context_tokens':8192,'output_tokens':512,
        'model_source':str(Path(native.__file__).resolve()),
        'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'tasks':[{'id':s['id'],'sha256':pack_task(s)['task_sha256']}for s in samples]})
    try:
        snapshot = gpu_snapshot()
        if snapshot[args.gpu]['free_mib']<23500:
            raise RuntimeError('Diagnostic needs a free GPU; other jobs are retained')
        lease=(DATA_ROOT/f'gpu-{args.gpu}.lock').open('a')
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        services.start('actor',args.gpu,memory_fraction=.78,max_model_len=8192)
        await asyncio.wait_for(services.ready(),min(600,deadline-time.monotonic()))
        for arm, schema in [('current_enum',original),('no_enum',no_enum)]:
            native.task_schema = schema
            for index,sample in enumerate(samples):
                if time.monotonic()>=deadline:
                    raise TimeoutError('Diagnostic time budget reached')
                progress('case',arm=arm,family=sample['family'],completed=len(rows))
                result=await case(sample,arm,root/'cases'/arm/str(index),services.owned['actor'],deadline)
                rows.append(result)
                write_json(root/'rows.json',rows)
        report = {'status':'reviewed','training_started':False,
            'arms':{arm:outcome_stats([r for r in rows if r['arm']==arm])for arm in ('current_enum','no_enum')},
            'cases':[{'task_id':s['id'],'family':s['family'],**{
                arm:next(r['reward']for r in rows if r['arm']==arm and r['task_id']==s['id'])
                for arm in ('current_enum','no_enum')}}for s in samples],
            'wire_sdk_all_match':all(r['wire_sdk_matches']for r in rows)}
        write_json(root/'summary.json',report)
        progress('complete',status='complete_review_required',completed=len(rows))
    except BaseException as exc:
        progress('failed',status='failed',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        native.task_schema = original
        await services.close()
        if lease:lease.close()
        progress(state['stage'],resources_released=True,finished_at=time.time())


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--tag',default='task_schema_ab8_20261117_r1')
    parser.add_argument('--gpu',default='5')
    parser.add_argument('--max-minutes',type=float,default=22)
    args=parser.parse_args()
    if Path(args.tag).name!=args.tag or args.tag in ('.','..') or args.max_minutes<=0:
        parser.error('Use a plain tag and a positive time budget')
    asyncio.run(run(args))
