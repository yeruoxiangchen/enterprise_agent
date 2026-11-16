"""Bounded, source-frozen official-DeepAgents vs custom-controller diagnostics."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import time

from crm_bench_control import identity, write
from crm_bench_services import ModelServices, gpu_snapshot, gpu_has_room
from harness_native_cases import CASES, GUARD_CASES, EXTRA_CASES

ROOT = Path('/data/zjr/enterprise_kb_agent')
SOURCE = Path(__file__).resolve().parents[1]
OUTPUT = ROOT/'artifacts/harness_native_20261115'
ARMS = ('native_deepagents', 'v17_custom')


def freeze(folder):
    files = [*sorted((SOURCE/'kb').rglob('*.py')), *sorted((SOURCE/'scripts').glob('*.py')),
             SOURCE/'scripts/harness.sh', SOURCE/'requirements.harness.lock']
    manifest = {}
    for path in files:
        relative = path.relative_to(SOURCE)
        data = path.read_bytes()
        target = folder/'source'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest[str(relative)] = hashlib.sha256(data).hexdigest()
    write(folder/'source-manifest.json', manifest)
    original = ROOT/'artifacts/harness_runs/harness_v17_dev16_20261115_r3/source-manifest.json'
    old = json.loads(original.read_text())
    actor_names = [name for name in old if name.startswith('kb/harness/') or name in
                   ('kb/benchmark/crm_deep_adapter.py', 'kb/config.py', 'kb/context.py')]
    differences = [name for name in actor_names if manifest.get(name) != old[name]]
    if differences:
        raise RuntimeError('Existing custom Actor changed: '+str(differences))
    write(folder/'custom-source-check.json', {'matches_frozen_v17': True, 'checked': actor_names,
        'source_manifest': str(original), 'source_manifest_sha256': hashlib.sha256(original.read_bytes()).hexdigest()})
    return manifest


def report(folder, rows):
    arms = {}
    for arm in ARMS:
        selected = [row for row in rows if row['arm'] == arm]
        known = all(row.get('tokens') is not None and row.get('usage_missing_calls', 0) == 0 for row in selected)
        durations = sorted(row['seconds'] for row in selected)
        arms[arm] = {'completed': len(selected), 'passed': sum(row['passed'] for row in selected),
            'errors': sum(bool(row.get('error')) for row in selected),
            'tokens': sum(row['tokens'] for row in selected) if known else None,
            'model_calls': sum(row.get('model_calls') or 0 for row in selected),
            'seconds_p50': statistics.median(durations) if durations else None,
            'seconds_max': max(durations) if durations else None,
            'usage_complete': known,
            'reused':sum(bool(row.get('reused'))for row in selected),
            'new_executions':sum(not row.get('reused')for row in selected),
            'new_tokens':sum(row['tokens']for row in selected if not row.get('reused'))
                if all(row.get('tokens')is not None and not row.get('usage_missing_calls')
                       for row in selected if not row.get('reused'))else None,
            'review_max_iterations':sum(row.get('completion_review_status')=='max_iterations_reached' for row in selected),
            'review_errors':sum(row.get('completion_review_status')in ('grader_error','failed')for row in selected),
            'review_evaluations':sum(len(row.get('completion_review',[]))for row in selected)}
    summary = {'scope': 'fixed-user development mechanism checks; response semantics ungraded; not an ITSM leaderboard score',
               'total': len(CASES)*len(ARMS), 'completed': len(rows), 'arms': arms,
               'failures': [{key: row.get(key) for key in ('task_id', 'arm', 'error', 'checks', 'gate_blocked', 'output')}
                            for row in rows if not row['passed']]}
    write(folder/'summary.json', summary)
    text = ['# DeepAgents 执行机制对照', '', summary['scope'], '',
            '|配置|通过/已完成|执行异常|Actor Token|模型调用|任务耗时p50秒|', '|---|---:|---:|---:|---:|---:|']
    for arm, item in arms.items():
        text.append(f"|{arm}|{item['passed']}/{item['completed']}|{item['errors']}|{item['tokens']}|{item['model_calls']}|{item['seconds_p50']}|")
    text.extend(['', '用户使用固定脚本，条件式后续消息由实际业务观测放行。各配置共用 CRMPort、Journal、公开任务状态/动作/顺序/禁止行为检查。',
                 '原生配置使用官方 create_deep_agent；v17_custom 为旧控制器，native_context 为原生运行时加实际用户/回执上下文。工作流记忆关闭。',
                 'reused 为经源代码、协议、原始轨迹与隔离业务重放校验的历史结果。总 Token 包含历史用量；new_tokens 只统计本轮新增推理。',
                 '最终离线判定采用实际动作/状态断言，答复含义另看对话；native_rubric 在运行时启用只读验收 Agent，所有用量计入 Actor。超时和预算耗尽计执行失败。',
                 '硬停止时保留已有结果、日志及 stacks.log；任何缺失用量单列，不作为零成本。', '', '## 执行明细', ''])
    for row in rows:
        text.append(f"- {row['task_id']} / {row['arm']}: passed={row['passed']}; error={row.get('error')}; tokens={row.get('tokens')}; seconds={row['seconds']:.2f}")
    (folder/'report.md').write_text('\n'.join(text)+'\n')
    return summary


async def run(args, folder):
    if (folder/'started.json').exists():
        raise RuntimeError('Tag already started; retain it and use a fresh tag')
    manifest=freeze(folder)
    status = {'pid': os.getpid(), 'start_ticks': identity(os.getpid()), 'status': 'running',
              'started_at': time.time(), 'completed': 0, 'total': len(CASES)*len(ARMS),
              'deadline_at': time.time()+args.max_hours*3600}
    write(folder/'started.json', status)
    def progress(stage, **values):
        status.update(stage=stage, updated_at=time.time(), **values)
        write(folder/'status.json', status)
        print(json.dumps(status), flush=True)
    protocol = {'scope': 'source-frozen fixed-user mechanism comparison', 'cases': CASES, 'arms': ARMS,
        'model': 'Qwen3-8B', 'context_tokens': 8192, 'output_tokens': 1400,
        'model_calls_per_turn': 40, 'business_calls_per_turn': 48, 'agent_turn_timeout_seconds': 180,
        'worker_hard_timeout_seconds': 360, 'max_hours': args.max_hours, 'seed': 20261114,
        'workflow_memory': False, 'user_simulator': False, 'semantic_judge': False,
        'official_graph_rerun': False, 'checks': 'canonical expected actions, arguments, order, state and forbidden actions; fixture checks',
        'native_factory': 'deepagents.create_deep_agent', 'native_extras': 'shared business gateway, accounting, context guard, read-only evidence filesystem',
        'native_control_layers': [], 'custom_source': 'frozen R3 Actor',
        'typed_context': 'native_context forwards literal customer messages via native application state and exposes actual receipts',
        'runtime_completion_review':{'enabled':args.rubric_comparison,'implementation':'official RubricMiddleware',
            'max_iterations':2,'max_actor_corrections':1,'grader_tools':['inspect_execution_evidence'],
            'grader_call_limit_per_invocation':3,'grader_coverage_retries':1,
            'shared_model_call_limit_per_request':40,'groundtruth_in_actor':False},
        'confirmation_guard':args.confirmation_guard,
        'approval_source':'explicit user review scoped to one tool and its arguments; never generated by the Actor or grader',
        'forked_subagents':args.fork_comparison,
        'operation_permission_route':args.fork_comparison,
        'candidate_only':args.candidate_only,
        'suite':args.suite,'reuse_from':args.reuse_from,
        'specialist_owned_checks':args.fork_comparison,
        'operation_preparations':{'close_ticket':['get_ticket','check_dependencies'],
                                  'reassign_ticket':['get_ticket']}if args.fork_comparison else{},
        'tensor_parallel_size':args.tensor_parallel_size,'gpu_memory_fraction':args.memory_fraction,
        'dependencies': {name: importlib.metadata.version(name) for name in ('deepagents', 'langchain', 'langgraph')}}
    write(folder/'protocol.json', protocol)
    services = ModelServices(folder, progress)
    process = None
    rows = []
    try:
        if args.reuse_from:
            from harness_native_reuse import restore
            rows,plan=await restore(OUTPUT/args.reuse_from,folder,protocol,manifest,ROOT/'benchmarks/crm-agent-bench')
            write(folder/'reuse_plan.json',plan)
            write(folder/'rows.json',rows)
            summary=report(folder,rows)
            progress('reuse_validated',completed=len(rows),inherited_trials=len(rows),
                     new_completed=0,new_total=len(CASES)*len(ARMS)-len(rows),arms=summary['arms'])
        inherited={(row['task_id'],row['arm'])for row in rows}
        if len(inherited)==len(CASES)*len(ARMS):
            progress('complete',status='complete_review_required',task=None,arm=None,worker_pid=None)
            return
        candidates = args.gpus.split(',')
        while True:
            stats = gpu_snapshot()
            group = next((group for group in itertools.combinations(candidates,args.tensor_parallel_size)
                          if all(g in stats and gpu_has_room(stats[g],args.memory_fraction)for g in group)),None)
            if group is not None:
                break
            progress('waiting_for_gpu', candidates=candidates)
            await asyncio.sleep(10)
        services.start('actor', ','.join(group), max_model_len=8192, memory_fraction=args.memory_fraction,
                       tensor_parallel_size=args.tensor_parallel_size)
        await services.ready()
        for index, case in enumerate(CASES):
            # Alternate execution order while every run has a fresh environment.
            for arm in ARMS if index % 2 == 0 else ARMS[::-1]:
                if (case['task_id'],arm)in inherited:
                    continue
                service = services.owned['actor']
                if identity(service['pid']) != service['start_ticks']:
                    raise RuntimeError('Owned model exited; inspect actor-model.log')
                target = folder/'trials'/case['task_id']/arm
                target.mkdir(parents=True)
                progress('execution', task=case['task_id'], arm=arm, completed=len(rows))
                command = [sys.executable, '-B', '-u', str(folder/'source/scripts/harness_native_worker.py'),
                    '--output', str(target), '--service-manifest', str(folder/'services.json'),
                    '--task', case['task_id'], '--arm', arm,'--suite',args.suite]
                if args.confirmation_guard:
                    command.append('--confirmation-guard')
                env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(folder/'source'), str(folder/'source/scripts'),
                    str(ROOT/'benchmarks/crm-agent-bench/src'))))
                started = time.monotonic()
                with (target/'worker.log').open('a') as log:
                    process = await asyncio.create_subprocess_exec(*command, cwd=folder/'source', env=env,
                        stdout=log, stderr=asyncio.subprocess.STDOUT)
                    progress('execution', worker_pid=process.pid)
                    hard_timeout = False
                    try:
                        code = await asyncio.wait_for(process.wait(), 360)
                    except TimeoutError:
                        hard_timeout = True
                        process.send_signal(signal.SIGUSR1)
                        await asyncio.sleep(1)
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 10)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
                        code = process.returncode
                if (target/'result.json').exists():
                    row = json.loads((target/'result.json').read_text())
                else:
                    row = {'task_id': case['task_id'], 'arm': arm, 'passed': False,
                        'error': f'Worker exited {code}; hard_timeout={hard_timeout}', 'tokens': None,
                        'model_calls': None, 'checks': {}, 'seconds': time.monotonic()-started}
                if code and not row.get('error'):
                    row.update(passed=False, error=f'Worker exited {code}; inspect worker.log and stacks.log')
                row.update(output=str(target), worker_returncode=code, hard_timeout=hard_timeout)
                rows.append(row)
                write(folder/'rows.json', rows)
                summary = report(folder, rows)
                progress('case_complete', completed=len(rows),new_completed=sum(not row.get('reused')for row in rows),
                         arms=summary['arms'])
                process = None
        progress('complete', status='complete_review_required', task=None, arm=None, worker_pid=None)
    except BaseException as exc:
        progress('failed', status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if process and process.returncode is None:
            process.send_signal(signal.SIGUSR1)
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
        await services.close()
        status.update(resources_released=True, finished_at=time.time())
        write(folder/'status.json', status)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('launch', 'run', 'status', 'watch', 'stop'))
    parser.add_argument('--tag', default='native_vs_v17_dev6_20261115_r1')
    parser.add_argument('--gpus', default='5,1')
    parser.add_argument('--tensor-parallel-size',type=int,choices=(1,2),default=1)
    parser.add_argument('--memory-fraction',type=float,default=.78)
    parser.add_argument('--max-hours', type=float, default=1)
    parser.add_argument('--context-comparison', action='store_true', help='Compare raw native assembly with typed business-state handoff')
    parser.add_argument('--rubric-comparison', action='store_true', help='Compare raw native assembly with official read-only completion review; includes assessment/cancellation guards')
    parser.add_argument('--fork-comparison', action='store_true', help='Compare isolated subagents with official forked context and typed operation permission lookup')
    parser.add_argument('--candidate-only',action='store_true',help='Evaluate only the selected native candidate; preserve prior reference records')
    parser.add_argument('--suite',choices=('dev8','expanded16'),default='dev8')
    parser.add_argument('--reuse-from',help='Reuse unchanged Actor/case results after source/protocol/business replay validation')
    parser.add_argument('--confirmation-guard',action='store_true',help='Both arms require native HITL approval for closure with observed open dependencies')
    args = parser.parse_args()
    global ARMS, CASES
    if sum((args.context_comparison,args.rubric_comparison,args.fork_comparison))>1:
        parser.error('Choose one diagnostic comparison')
    if args.context_comparison:
        ARMS = ('native_deepagents', 'native_context')
    if args.rubric_comparison:
        ARMS = ('native_deepagents', 'native_rubric')
        CASES = [*CASES,*GUARD_CASES]
    if args.fork_comparison:
        ARMS = ('native_deepagents','native_forked')
        CASES = [*CASES,*GUARD_CASES]
    if args.suite=='expanded16':
        if not(args.fork_comparison and args.candidate_only):
            parser.error('expanded16 evaluates only the selected fork candidate')
        CASES=[*CASES,*EXTRA_CASES]
    if args.candidate_only:
        if not any((args.context_comparison,args.rubric_comparison,args.fork_comparison)):
            parser.error('--candidate-only requires an explicit native candidate')
        ARMS = (ARMS[-1],)
    if args.confirmation_guard and 'v17_custom'in ARMS:
        parser.error('--confirmation-guard requires a native-only comparison')
    if Path(args.tag).name != args.tag or args.tag in ('.', '..'):
        parser.error('Use a single run tag')
    if args.reuse_from and (not args.candidate_only or Path(args.reuse_from).name!=args.reuse_from
                           or args.reuse_from in ('.','..',args.tag)):
        parser.error('Reuse requires a distinct single source tag and --candidate-only')
    if not 0 < args.max_hours <= 2 or any(g not in set(map(str, range(8))) for g in args.gpus.split(',')):
        parser.error('Use valid GPU indices and a budget up to two hours')
    if (not .5<=args.memory_fraction<=.8 or
            len(set(args.gpus.split(',')))<args.tensor_parallel_size):
        parser.error('Use enough distinct GPUs and a memory fraction between .5 and .8')
    folder = OUTPUT/args.tag
    folder.mkdir(parents=True, exist_ok=True)
    if args.action in ('status', 'watch', 'stop'):
        path = folder/'status.json'
        if not path.exists():
            print('Not started')
            return
        status = json.loads(path.read_text())
        if args.action == 'watch':
            os.execvp('watch', ['watch', '-n', '5', 'bash', str(SOURCE/'scripts/harness.sh'), 'native', 'status', '--tag', args.tag])
        if args.action == 'stop':
            if identity(status['pid']) == status['start_ticks']:
                os.kill(status['pid'], signal.SIGTERM)
            return
        status['process_alive'] = identity(status['pid']) == status['start_ticks']
        status['output'] = str(folder)
        print(json.dumps(status, indent=2))
        return
    if args.action == 'launch':
        if (folder/'started.json').exists():
            parser.error('Tag already started')
        with (folder/'queue.log').open('a') as log:
            command = [sys.executable, '-B', '-u', __file__, 'run', '--tag', args.tag,
                       '--gpus', args.gpus, '--max-hours', str(args.max_hours),
                       '--tensor-parallel-size',str(args.tensor_parallel_size),
                       '--memory-fraction',str(args.memory_fraction),'--suite',args.suite]
            if args.context_comparison:
                command.append('--context-comparison')
            if args.rubric_comparison:
                command.append('--rubric-comparison')
            if args.fork_comparison:
                command.append('--fork-comparison')
            if args.candidate_only:
                command.append('--candidate-only')
            if args.reuse_from:
                command+=['--reuse-from',args.reuse_from]
            if args.confirmation_guard:
                command.append('--confirmation-guard')
            process = subprocess.Popen(command,
                cwd=SOURCE, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        print(json.dumps({'pid': process.pid, 'output': str(folder)}))
        return
    async def execute():
        task = asyncio.current_task()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        async with asyncio.timeout(args.max_hours*3600):
            await run(args, folder)
    asyncio.run(execute())


if __name__ == '__main__':
    main()
