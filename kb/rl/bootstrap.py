"""Owned two-GPU job: verified sampling -> TRL SFT -> paired check -> veRL RL."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time

from .control import acquire_gpu_leases,identity
from .runtime import run_episode,write_json
from .tasks import DATA_ROOT,DATASET_NAME,pack_task,verify_dataset

SOURCE=Path(__file__).resolve().parents[2]
BASE=Path('/data/zjr/enterprise_kb_agent/models/Qwen3-8B')
RUNNER=DATA_ROOT/'envs/runner/bin/python'
TRAINER=DATA_ROOT/'envs/trainer/bin/python'


def paired_check(before,after):
    from .review import outcome_stats
    left={r['task_id']:r for r in before};right={r['task_id']:r for r in after}
    if not left or left.keys()!=right.keys()or len(left)!=len(before)or len(right)!=len(after):
        raise ValueError('Incomplete or duplicate SFT comparison cases')
    for key,a in left.items():
        b=right[key]
        if any(a[k]!=b[k]for k in ('task_sha256','harness_profile','family','train_role','supervisor','specialist')):
            raise ValueError('SFT comparison changed task bytes or harness profile')
    a,b=outcome_stats(before),outcome_stats(after)
    safe=not b['execution_errors']and b['forbidden_attempt_episodes']<=a['forbidden_attempt_episodes']
    safe=safe and all(r['checks']['no_extra_committed_writes']for r in after)
    return {'baseline':a,'sft':b,'passed':safe and b['passed']>=a['passed'],
        'wins':sum(right[k]['reward']>left[k]['reward']for k in left),
        'regressions':sum(right[k]['reward']<left[k]['reward']for k in left)}


class Job:
    def __init__(self,args):
        self.args=args;self.folder=DATA_ROOT/'outputs'/args.tag
        self.deadline=time.monotonic()+args.max_minutes*60
        self.services=[];self.leases=[];self.active=None;self.rl_tag=None
        self.state={'pid':os.getpid(),'start_ticks':identity(os.getpid()),'status':'running',
            'stage':'prepare','started_at':time.time(),'max_minutes':args.max_minutes,
            'gpus':args.gpus.split(','),'resources_released':False,
            'train_role':'supervisor','frozen_roles':['specialists','reviewer'],
            'profile':{'delegation_protocol':'native','execution_granularity':'domain','completion_review':'errors'},
            'sft_steps':args.sft_steps,'rl_steps':args.rl_steps,'collect_tasks':args.collect_tasks,
            'collect_attempts':args.collect_attempts,'validation_tasks':16,
            'reuse_collection':getattr(args,'reuse_collection',''),
            'selection_policy':getattr(args,'selection_policy','verified_calls'),
            'base_model':str(BASE),'heldout_used':False}
    def save(self,stage=None,**data):
        self.state.update(data,updated_at=time.time())
        if stage is not None:self.state['stage']=stage
        write_json(self.folder/'status.json',self.state)
    def budget(self):
        if time.monotonic()>=self.deadline:raise TimeoutError('Bootstrap job time budget reached')
    def command(self,name,args,*,gpu=''):
        self.budget();self.save(name)
        env={**os.environ,'CUDA_VISIBLE_DEVICES':gpu,'OMP_NUM_THREADS':'4'}
        with (self.folder/f'{name}.log').open('a')as stream:
            self.active=subprocess.Popen([str(a)for a in args],cwd=SOURCE,env=env,
                stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        self.save(active_process={'pid':self.active.pid,'start_ticks':identity(self.active.pid),'name':name})
        while self.active.poll()is None:
            self.budget()
            progress=self.folder/'sft/progress.json'
            self.save(**({'sft_progress':json.loads(progress.read_text())}if name=='sft'and progress.exists()else{}))
            time.sleep(5)
        result=self.active.returncode;self.active=None;self.save(active_process=None)
        if result:raise RuntimeError(f'{name} exited {result}; inspect {self.folder/name}.log')
    async def server(self,name,gpu,model):
        from crm_bench_services import ModelServices
        output=self.folder/name;output.mkdir(parents=True,exist_ok=False)
        service=ModelServices(output,lambda stage,**data:self.save(name,model_loading=data))
        self.services.append(service)
        service.start('actor',gpu,memory_fraction=.78,max_model_len=8192,model_path=model)
        await asyncio.wait_for(service.ready(),timeout=min(600,self.deadline-time.monotonic()))
        lease=service.owned['actor']
        return service,{'url':lease['url']+'/v1','model':lease['model'],'key':'local'}
    async def evaluate(self,name,supervisor,specialist,samples):
        rows=[]
        for i,sample in enumerate(samples):
            self.budget();self.save(name,completed=i,total=len(samples),task=sample['id'])
            result=await run_episode(sample,supervisor,specialist,self.folder/name/f'{i:02d}',
                temperature=0,train_role='supervisor',delegation_protocol='native',
                execution_granularity='domain',completion_review='errors')
            result['task_sha256']=pack_task(sample)['task_sha256']
            write_json(self.folder/name/f'{i:02d}'/'result.json',result)
            rows.append(result);write_json(self.folder/f'{name}-rows.json',rows)
        self.save(name,completed=len(samples));return rows
    async def work(self):
        from .bootstrap_data import collect_one,coverage,reuse_collection
        from .sft import export_successes
        dataset=DATA_ROOT/'datasets'/DATASET_NAME
        write_json(self.folder/'dataset-manifest.json',verify_dataset(dataset))
        policy=self.args.selection_policy
        def curation(kept):return coverage(kept,require_branches=policy=='clean_episode')
        train_all=list(map(json.loads,(dataset/'train.jsonl').read_text().splitlines()))
        kept=None
        sft_data=DATA_ROOT/'datasets'/f'{self.args.tag}_sft'
        manifest=None
        if self.args.reuse_collection:
            self.save('reuse_collection')
            kept=reuse_collection(DATA_ROOT/'outputs'/self.args.reuse_collection,self.folder,
                train_all,selection_policy=policy)
            gate=curation(kept);write_json(self.folder/'data-gate.json',gate)
            self.save('reuse_data_checked',completed=len(kept),curation=gate)
            if not gate['passed']:raise RuntimeError('Reused corpus lacks warm-start skill coverage')
            manifest=export_successes(self.folder,sft_data,max_length=4096,selection_policy=policy)
            self.save('data_ready',sft_dataset=str(sft_data),sft_rows=manifest['rows'],curation=gate)
        devices=self.args.gpus.split(',')
        if len(devices)!=2 or len(set(devices))!=2:raise ValueError('Use two distinct physical GPUs')
        self.leases,snapshot=acquire_gpu_leases(devices,DATA_ROOT,wait_minutes=self.args.gpu_wait_minutes,
            report=lambda **d:self.save(**d))
        # GPU wait is separate from this compute budget.
        self.deadline=time.monotonic()+self.args.max_minutes*60
        self.save('loading_models',gpu_snapshot=snapshot,compute_started_at=time.time())
        supervisor_service,supervisor=await self.server('supervisor_base',devices[0],BASE)
        _,specialist=await self.server('frozen_specialists',devices[1],BASE)
        val=list(map(json.loads,(dataset/'dev.jsonl').read_text().splitlines()))
        val=val[:16]
        train=train_all[:self.args.collect_tasks]
        if len(train)!=self.args.collect_tasks:raise ValueError('Not enough distinct training fixtures')
        plan=[(s,k)for k in range(self.args.collect_attempts)for s in train]
        # Randomize within each pass while retaining fixture coverage before resampling.
        randomizer=random.Random(20261118)
        for start in range(0,len(plan),len(train)):
            block=plan[start:start+len(train)];randomizer.shuffle(block);plan[start:start+len(train)]=block
        reused=kept is not None
        if kept is None:kept=[]
        counts={};collect_deadline=min(self.deadline,time.monotonic()+self.args.collect_minutes*60)
        for i,(sample,attempt)in enumerate(plan):
            if reused:break
            self.budget()
            if time.monotonic()>=collect_deadline:break
            current=curation(kept)
            needs_branch=False
            if sample['family']=='conditional_reassign':
                assigned=bool(sample['task']['initial_state']['tickets'][sample['target_ticket']]['assigned_to'])
                needs_branch=current['conditional_branches'].get('assigned'if assigned else'unassigned',0)<1
            if sample['family']not in current['missing_families']and not needs_branch:continue
            self.save('collect_supervisor',completed=len(kept),total=len(plan),task=sample['id'],
                curation=current,rejections=counts,plan_position=i)
            result,admitted,reason=await collect_one(sample,supervisor,specialist,self.folder,i,
                seed=20261118+i,max_length=4096,selection_policy=policy)
            kept.append((sample,result,admitted));counts[reason]=counts.get(reason,0)+1
            write_json(self.folder/'collection-summary.json',{'completed':len(kept),'planned':len(plan),
                'curation':curation(kept),'outcomes':counts})
            self.save(completed=len(kept),curation=curation(kept),rejections=counts)
            if curation(kept)['passed']:break
        gate=curation(kept);write_json(self.folder/'data-gate.json',gate)
        if not gate['passed']:raise RuntimeError('Clean supervisor corpus lacks required families/conditional branches; inspect data-gate.json')
        if manifest is None:manifest=export_successes(self.folder,sft_data,max_length=4096,selection_policy=policy)
        self.save('data_ready',sft_dataset=str(sft_data),sft_rows=manifest['rows'],curation=gate)
        before=await self.evaluate('baseline',supervisor,specialist,val)
        await supervisor_service.close()
        sft_output=self.folder/'sft'
        self.command('sft',[TRAINER,'-B','-m','kb.rl.sft','train','--dataset',sft_data,
            '--output',sft_output,'--steps',str(self.args.sft_steps),'--train-role','supervisor'],gpu=devices[0])
        self.command('verify_adapter',[TRAINER,'-B','-c',
            "import json,sys; from kb.rl.bootstrap_data import check_adapter; print(json.dumps(check_adapter(sys.argv[1])))",
            sft_output/'adapter'])
        merged=DATA_ROOT/'models'/f'{self.args.tag}_sft'
        self.command('merge_sft',[TRAINER,'-B','-m','kb.rl.sft','merge','--adapter',sft_output/'adapter','--output',merged])
        _,supervisor=await self.server('supervisor_sft',devices[0],merged)
        after=await self.evaluate('sft_evaluation',supervisor,specialist,val)
        comparison=paired_check(before,after);write_json(self.folder/'sft-comparison.json',comparison)
        self.save('sft_review',sft_comparison=comparison)
        if not comparison['passed']:
            self.save(status='sft_complete_review_required');return
        for service in self.services:await service.close()
        self.services.clear()
        for lease in self.leases:lease.close()
        self.leases.clear()
        self.rl_tag=self.args.tag+'_rl'
        self.command('launch_rl',[RUNNER,'-B','-m','kb.rl.control','launch','--tag',self.rl_tag,
            '--gpus',self.args.gpus,'--gpu-wait-minutes',str(self.args.gpu_wait_minutes),
            '--policy-model',merged,'--train-role','supervisor','--adv-estimator','reinforce_plus_plus',
            '--steps',str(self.args.rl_steps),'--batch-size','4','--group-size','4',
            '--val-limit','16','--val-before-train','--max-minutes',str(max(1,(self.deadline-time.monotonic())/60))])
        self.save('reinforcement_learning',rl_tag=self.rl_tag,rl_output=str(DATA_ROOT/'outputs'/self.rl_tag))
        path=DATA_ROOT/'outputs'/self.rl_tag/'status.json'
        while True:
            self.budget()
            if path.exists():
                state=json.loads(path.read_text());self.save(rl_status=state)
                if state.get('resources_released'):
                    if state['status']!='complete_review_required':raise RuntimeError('RL job failed; inspect child status and logs')
                    break
            time.sleep(5)
        self.command('review_rl',[RUNNER,'-B','-m','kb.rl.review','--run-dir',DATA_ROOT/'outputs'/self.rl_tag])
        self.save('complete',status='complete_review_required')
    def stop_child(self):
        if self.active and self.active.poll()is None:
            os.killpg(self.active.pid,signal.SIGTERM)
            try:self.active.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.active.pid,signal.SIGKILL);self.active.wait(timeout=5)
        if self.rl_tag:
            path=DATA_ROOT/'outputs'/self.rl_tag
            if path.exists():
                # A stop can arrive between the child spawn and launcher.json.
                until=time.monotonic()+15
                while not(path/'launcher.json').exists()and not(path/'status.json').exists()and time.monotonic()<until:
                    time.sleep(.2)
                metadata=path/('launcher.json'if(path/'launcher.json').exists()else'status.json')
                if not metadata.exists():return False
                owned=json.loads(metadata.read_text())
                if identity(owned['pid'])==owned['start_ticks']:os.kill(owned['pid'],signal.SIGTERM)
                deadline=time.monotonic()+150
                while time.monotonic()<deadline:
                    if(path/'status.json').exists()and json.loads((path/'status.json').read_text()).get('resources_released'):return True
                    time.sleep(2)
                return False
        return True
    def run(self):
        def interrupt(signum,frame):raise KeyboardInterrupt('Requested stop')
        previous=signal.signal(signal.SIGTERM,interrupt)
        child_released=True
        try:asyncio.run(self.work())
        except BaseException as exc:
            self.save(status='stopped'if isinstance(exc,KeyboardInterrupt)else'failed',
                error=f'{type(exc).__name__}: {exc}')
        finally:
            signal.signal(signal.SIGTERM,signal.SIG_IGN)
            errors=[]
            try:child_released=self.stop_child()
            except Exception as exc:
                child_released=False;errors.append(str(exc))
            for service in self.services:
                try:asyncio.run(service.close())
                except Exception as exc:errors.append(str(exc))
            for lease in self.leases:lease.close()
            if errors:self.save(status='failed',cleanup_errors=errors)
            self.save(resources_released=child_released and not errors,active_process=None,finished_at=time.time())
            signal.signal(signal.SIGTERM,previous)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=('launch','_run','status','watch','stop'))
    parser.add_argument('--tag',default='supervisor_sft_rl_20261117_r1')
    parser.add_argument('--gpus',default='5,6')
    parser.add_argument('--collect-tasks',type=int,default=32)
    parser.add_argument('--collect-attempts',type=int,default=4)
    parser.add_argument('--collect-minutes',type=float,default=40)
    parser.add_argument('--sft-steps',type=int,default=16)
    parser.add_argument('--rl-steps',type=int,default=2)
    parser.add_argument('--max-minutes',type=float,default=120)
    parser.add_argument('--gpu-wait-minutes',type=float,default=120)
    parser.add_argument('--reuse-collection',default='')
    parser.add_argument('--selection-policy',choices=('clean_episode','verified_calls'),default='verified_calls')
    args=parser.parse_args()
    if Path(args.tag).name!=args.tag or args.tag in ('.','..'):parser.error('Use a plain tag')
    if args.reuse_collection and (Path(args.reuse_collection).name!=args.reuse_collection or args.reuse_collection in ('.','..')):
        parser.error('Use a plain source collection tag')
    if min(args.collect_tasks,args.collect_attempts,args.sft_steps,args.rl_steps,args.max_minutes,args.collect_minutes)<=0:
        parser.error('Use positive data/training/time budgets')
    folder=DATA_ROOT/'outputs'/args.tag
    if args.command=='_run':Job(args).run()
    elif args.command=='launch':
        folder.mkdir(parents=True,exist_ok=False)
        source=folder/'source'
        shutil.copytree(SOURCE/'kb',source/'kb',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (source/'scripts').mkdir()
        for name in ('crm_bench_services.py','crm_bench_control.py','native_rl_diagnostic.py'):
            shutil.copy2(SOURCE/'scripts'/name,source/'scripts'/name)
        manifest={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()for p in source.rglob('*.py')}
        write_json(folder/'source-manifest.json',manifest)
        env={**os.environ,'PYTHONPATH':f'{source}:{source}/scripts:/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench/src'}
        with(folder/'queue.log').open('a')as stream:
            proc=subprocess.Popen([str(RUNNER),'-B','-m','kb.rl.bootstrap','_run',*sys.argv[2:]],
                cwd=source,env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        write_json(folder/'launcher.json',{'pid':proc.pid,'start_ticks':identity(proc.pid)})
        print(json.dumps({'pid':proc.pid,'output':str(folder)}))
    elif args.command=='stop':
        state=json.loads((folder/'launcher.json').read_text())
        if identity(state['pid'])==state['start_ticks']:os.kill(state['pid'],signal.SIGTERM)
        print('Stop requested for this owned job.')
    else:
        while True:
            p=folder/'status.json';s=json.loads(p.read_text())if p.exists()else{'status':'starting'}
            print(json.dumps(s,ensure_ascii=False,indent=2),flush=True)
            if args.command=='status'or s.get('resources_released'):break
            time.sleep(5)


if __name__=='__main__':main()
