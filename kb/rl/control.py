"""Bounded, process-owned RL jobs; never stop a shared Ray/model service."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import secrets
import socket
import subprocess
import sys
import time

from .tasks import DATA_ROOT, DATASET_NAME, prepare, verify_dataset
from .runtime import write_json

SOURCE=Path(__file__).resolve().parents[2]


def identity(pid):
    try:return Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()[19]
    except (OSError,IndexError):return None


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def acquire_gpu_leases(devices, root, *, wait_minutes, report, snapshot_fn=None):
    """Wait for the existing free-memory thresholds without loading CUDA.

    A new, named run may opt in to waiting. Failed/stopped runs are retained;
    this helper does not restart them or stop another project's processes.
    """
    from crm_bench_services import gpu_snapshot
    snapshot_fn = snapshot_fn or gpu_snapshot
    deadline = time.monotonic() + wait_minutes*60
    while True:
        snapshot, leases, blocked = snapshot_fn(), [], {}
        for gpu in devices:
            required = 23500 if gpu == devices[0] else 22000
            if snapshot[gpu]['free_mib'] < required:
                blocked[gpu] = {'required_free_mib': required, 'available_free_mib': snapshot[gpu]['free_mib']}
        with ExitStack() as pending:
            if not blocked:
                for gpu in devices:
                    lease = pending.enter_context((root/f'gpu-{gpu}.lock').open('a'))
                    leases.append(lease)
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        blocked[gpu] = {'reason': 'leased_by_another_rl_job'}
                        break
            if not blocked:
                pending.pop_all()  # ownership transfers to run_job's finally
                return leases, snapshot
        if wait_minutes <= 0:
            raise RuntimeError('GPU preflight failed: '+json.dumps(blocked))
        if time.monotonic() >= deadline:
            raise TimeoutError('GPU wait budget reached: '+json.dumps(blocked))
        report(stage='waiting_for_gpus', gpu_snapshot=snapshot, blocked_gpus=blocked,
               gpu_wait_minutes=wait_minutes)
        time.sleep(min(5, max(.01, deadline-time.monotonic())))


def run_job(args):
    import httpx
    from crm_bench_services import ModelServices
    folder=DATA_ROOT/'outputs'/args.tag
    folder.mkdir(parents=True,exist_ok=True)
    status={'pid':os.getpid(),'start_ticks':identity(os.getpid()),'status':'running',
            'stage':'prepare','started_at':time.time(),'gpus':args.gpus.split(','),
            'max_minutes':args.max_minutes,'steps':args.steps,'resources_released':False}
    status['train_role']=args.train_role
    status['adv_estimator']=args.adv_estimator
    status.update(val_before_train=args.val_before_train,val_limit=args.val_limit,
                  batch_size=args.batch_size,group_size=args.group_size,
                  dataset=DATASET_NAME,checkpoint_interval=args.checkpoint_interval,
                  baseline_from=args.baseline_from)
    status['policy_model']=args.policy_model
    deadline=time.monotonic()+args.max_minutes*60
    children=[]
    leases=[]
    stopped=False

    def save(**updates):
        status.update(updates,updated_at=time.time())
        write_json(folder/'status.json',status)

    def interrupt(signum,frame):
        raise KeyboardInterrupt(f'Requested stop ({signum})')
    signal.signal(signal.SIGTERM,interrupt)

    def start(name,cmd,env):
        log=(folder/f'{name}.log').open('a')
        try:proc=subprocess.Popen(cmd,cwd=SOURCE,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        finally:log.close()
        children.append((name,proc,identity(proc.pid)))
        write_json(folder/'processes.json',[{'name':n,'pid':p.pid,'start_ticks':i}for n,p,i in children])
        return proc

    services=ModelServices(folder,lambda stage,**data:save(stage='start_frozen_specialist',model_loading=data))
    try:
        write_json(folder/'dataset-manifest.json',verify_dataset(DATA_ROOT/'datasets'/DATASET_NAME))
        if args.baseline_from:
            from .baseline import inherit_baseline
            inherited=inherit_baseline(DATA_ROOT/'outputs'/args.baseline_from,folder,
                DATA_ROOT/'datasets'/DATASET_NAME,limit=args.val_limit,train_role=args.train_role,
                policy_model=args.policy_model)
            save(inherited_baseline_cases=inherited['count'])
        devices=args.gpus.split(',')
        if len(devices)!=2 or len(set(devices))!=2 or not all(d.isdigit() for d in devices):
            raise ValueError('Specify two distinct physical GPU indices')
        leases,snapshot=acquire_gpu_leases(devices,DATA_ROOT,
            wait_minutes=args.gpu_wait_minutes,report=save)
        deadline=time.monotonic()+args.max_minutes*60
        save(stage='start_frozen_specialist',gpu_snapshot=snapshot,
             compute_budget_started_at=time.time(),blocked_gpus={})
        services.start('actor',devices[1],memory_fraction=.78,max_model_len=8192)
        async def ready():
            await asyncio.wait_for(services.ready(),timeout=min(720,max(1,deadline-time.monotonic())))
        asyncio.run(ready())
        expert=services.owned['actor']
        with httpx.Client(timeout=20,trust_env=False) as client:
            counted=client.post(expert['url']+'/tokenize',json={
                'model':expert['model'],'messages':[{'role':'user','content':'budget check'}],
                'tools':[],'chat_template_kwargs':{'enable_thinking':False}})
            counted.raise_for_status()
            if counted.json().get('count',0)<=0:
                raise RuntimeError('Frozen specialist tokenizer returned no tokens')
        save(tokenizer_verified={'model':expert['model'],'count':counted.json()['count']})
        store_port=free_port()
        env=os.environ.copy()
        short_root=Path('/data/zjr/ekbr')
        if not short_root.exists():short_root.symlink_to(DATA_ROOT,target_is_directory=True)
        if short_root.resolve()!=DATA_ROOT.resolve():
            raise RuntimeError('Short Ray path is already used by another directory')
        ray_dir=short_root/'r'/hashlib.sha256(args.tag.encode()).hexdigest()[:6]
        ray_dir.mkdir(parents=True,exist_ok=True)
        env.update(KB_RL_RUN_DIR=str(folder),KB_RL_SPECIALIST_URL=expert['url']+'/v1',
                   KB_RL_TRAIN_ROLE=args.train_role,
                   KB_RL_SPECIALIST_MODEL=expert['model'],AGL_MANAGED_STORE='0',
                   AGENTOPS_API_KEY='',RAY_TMPDIR=str(ray_dir),WANDB_MODE='offline',
                   PYTHONUNBUFFERED='1')
        # All job endpoints are local. Keep requests away from the terminal's
        # outbound HTTP proxy; this only affects this job's child processes.
        env['NO_PROXY']='*'
        env['no_proxy']='*'
        proxy_key='sk-'+secrets.token_hex(32)
        key_file=folder/'proxy.key'
        key_file.write_text(proxy_key+'\n')
        key_file.chmod(0o600)
        env['KB_RL_PROXY_KEY']=proxy_key
        env.pop('RAY_ADDRESS',None)
        store_url=f'http://127.0.0.1:{store_port}'
        runner_python=str(DATA_ROOT/'envs/runner/bin/python')
        trainer_python=str(DATA_ROOT/'envs/trainer/bin/python')
        start('store',[str(DATA_ROOT/'envs/runner/bin/agl'),'store',
                       '--host','127.0.0.1','--port',str(store_port)],env)
        save(stage='start_store',store_url=store_url)
        with httpx.Client(timeout=2,trust_env=False) as client:
            while True:
                if time.monotonic()>deadline:raise TimeoutError('RL job time budget reached')
                try:
                    response=client.get(store_url+'/v1/agl/health')
                    if response.status_code==200:break
                except httpx.HTTPError:pass
                if children[-1][1].poll()is not None:raise RuntimeError('Store exited; inspect store.log')
                time.sleep(1)
        common=['-B','-m','kb.rl.training','--run-dir',str(folder),
                '--dataset',str(DATA_ROOT/'datasets'/DATASET_NAME),'--store',store_url,
                '--steps',str(args.steps),'--batch-size',str(args.batch_size),
                '--group-size',str(args.group_size),'--val-limit',str(args.val_limit)]
        common+=['--model',args.policy_model]
        common+=['--adv-estimator',args.adv_estimator]
        if args.val_before_train:common+=['--val-before-train']
        common+=['--checkpoint-interval',str(args.checkpoint_interval)]
        runner_env={**env,'CUDA_VISIBLE_DEVICES':''}
        start('runner',[runner_python,*common,'--role','runner'],runner_env)
        trainer_env={**env,'CUDA_VISIBLE_DEVICES':devices[0]}
        algorithm=start('trainer',[trainer_python,*common,'--role','algorithm'],trainer_env)
        save(stage='training',roles={'trainable_policy':devices[0],'frozen_model':devices[1],
                                    'train_role':args.train_role})
        first_episode_deadline=min(deadline,time.monotonic()+720)
        while algorithm.poll()is None:
            if time.monotonic()>deadline:raise TimeoutError('RL job time budget reached; progress retained')
            if time.monotonic()>first_episode_deadline and not any((folder/'rollouts').glob('*/*/result.json')):
                raise TimeoutError('No completed business episode in 12 minutes; inspect trainer.log for initialization failure')
            for name,proc,_ in children:
                if name!='trainer'and proc.poll()is not None:
                    raise RuntimeError(f'{name} exited {proc.returncode}; inspect {name}.log')
            if identity(expert['pid'])!=expert['start_ticks']:
                raise RuntimeError('Frozen specialist exited')
            update_path=folder/'policy-updates.json'
            updates=json.loads(update_path.read_text())['completed_updates'] if update_path.exists() else 0
            save(completed_updates=updates)
            time.sleep(2)
        if algorithm.returncode:
            raise RuntimeError(f'Trainer exited {algorithm.returncode}; inspect trainer.log')
        save(status='complete_review_required',stage='complete')
    except BaseException as exc:
        save(status='stopped'if isinstance(exc,KeyboardInterrupt)else'failed',stage='failed',
             error=f'{type(exc).__name__}: {exc}')
        if not isinstance(exc,(Exception,KeyboardInterrupt)):raise
    finally:
        if status.get('store_url'):
            try:
                with (folder/'trace-export.log').open('a')as stream:
                    exported=subprocess.run([str(DATA_ROOT/'envs/runner/bin/python'),'-B',
                        '-m','kb.rl.audit_store','--store',status['store_url'],'--output',str(folder)],
                        cwd=SOURCE,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=120)
                save(trace_export_exit=exported.returncode)
            except (OSError,subprocess.TimeoutExpired)as exc:
                save(trace_export_error=str(exc))
        for name,proc,ticks in reversed(children):
            if identity(proc.pid)==ticks and proc.poll()is None:
                os.killpg(proc.pid,signal.SIGINT)
        end=time.monotonic()+20
        for _,proc,ticks in reversed(children):
            try:proc.wait(timeout=max(.1,end-time.monotonic()))
            except subprocess.TimeoutExpired:
                if identity(proc.pid)==ticks:os.killpg(proc.pid,signal.SIGKILL)
                proc.wait(timeout=5)
        asyncio.run(services.close())
        for lock in leases:lock.close()
        save(resources_released=True,finished_at=time.time())


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=('prepare','launch','_run','status','watch','stop'))
    parser.add_argument('--tag',default='supervisor_grpo_short_20261116')
    parser.add_argument('--gpus',default='5,6')
    parser.add_argument('--gpu-wait-minutes',type=float,default=0,
                        help='Opt-in GPU queue budget before the separate compute time budget')
    parser.add_argument('--train-role',choices=('supervisor','specialists'),default='supervisor')
    parser.add_argument('--policy-model',default='/data/zjr/enterprise_kb_agent/models/Qwen3-8B')
    parser.add_argument('--adv-estimator',choices=('grpo','reinforce_plus_plus'),default='grpo')
    parser.add_argument('--steps',type=int,default=2)
    parser.add_argument('--batch-size',type=int,default=2)
    parser.add_argument('--group-size',type=int,default=4)
    parser.add_argument('--val-limit',type=int,default=4)
    parser.add_argument('--val-before-train',action='store_true')
    parser.add_argument('--checkpoint-interval',type=int,default=0)
    parser.add_argument('--baseline-from',default='')
    parser.add_argument('--max-minutes',type=float,default=90)
    args=parser.parse_args()
    if (args.steps<1 or args.batch_size<1 or args.group_size<2 or args.max_minutes<=0
            or args.val_limit<0 or args.checkpoint_interval<0 or args.gpu_wait_minutes<0):
        parser.error('Use positive steps/batch/time and at least two trajectories per task')
    if Path(args.tag).name!=args.tag or args.tag in ('.','..'):
        parser.error('Use a plain run tag')
    if args.baseline_from and (Path(args.baseline_from).name!=args.baseline_from or args.baseline_from in ('.','..')):
        parser.error('Use a plain baseline tag')
    if args.baseline_from and args.val_before_train:
        parser.error('Select inherited baseline or live pretraining validation')
    folder=DATA_ROOT/'outputs'/args.tag
    if args.command=='prepare':
        print(json.dumps(prepare(DATA_ROOT/'datasets'/DATASET_NAME),ensure_ascii=False,indent=2))
    elif args.command=='_run':run_job(args)
    elif args.command=='launch':
        folder.mkdir(parents=True,exist_ok=False)
        frozen=folder/'source'
        shutil.copytree(SOURCE/'kb',frozen/'kb',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (frozen/'scripts').mkdir()
        for name in ('crm_bench_services.py','crm_bench_control.py'):
            shutil.copy2(SOURCE/'scripts'/name,frozen/'scripts'/name)
        manifest={str(p.relative_to(frozen)):hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in frozen.rglob('*.py')}
        write_json(folder/'source-manifest.json',manifest)
        env=os.environ.copy()
        env['PYTHONPATH']=str(frozen)+':'+str(frozen/'scripts')+':/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench/src'
        command=[sys.executable,'-B','-m','kb.rl.control','_run',*sys.argv[2:]]
        with (folder/'queue.log').open('a')as stream:
            proc=subprocess.Popen(command,cwd=frozen,env=env,stdout=stream,
                                  stderr=subprocess.STDOUT,start_new_session=True)
        write_json(folder/'launcher.json',{'pid':proc.pid,'start_ticks':identity(proc.pid)})
        print(json.dumps({'pid':proc.pid,'output':str(folder)},ensure_ascii=False))
    elif args.command in ('status','watch'):
        while True:
            path=folder/'status.json'
            state=json.loads(path.read_text())if path.exists()else{'status':'starting'}
            print(json.dumps(state,ensure_ascii=False,indent=2),flush=True)
            if args.command=='status' or (state.get('status') in
                ('failed','stopped','complete_review_required') and state.get('resources_released')):break
            time.sleep(5)
    else:
        info=json.loads((folder/'launcher.json').read_text())
        if identity(info['pid'])==info['start_ticks']:
            os.kill(info['pid'],signal.SIGTERM)
            print('Stop requested; owned processes will be released.')
        else:print('Queue has ended.')


if __name__=='__main__':main()
