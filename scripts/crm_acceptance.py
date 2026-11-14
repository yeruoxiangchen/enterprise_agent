"""Calibrate evaluation, check fixed dialogue, then optionally run four public tasks.

One owned model lease, bounded wall time, fresh output and immutable source copy.
No retries of failed jobs and no automatic expansion beyond the four task IDs.
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
import subprocess
import sys
import time

from crm_bench_control import RUNTIME, identity, write
from crm_bench_services import ModelServices, gpu_has_room, gpu_snapshot
from crm_small_check import CASES, business_case

ROOT = RUNTIME/'artifacts/crm_acceptance_20261113'
PROJECT = Path(__file__).resolve().parents[1]
TASK_IDS = ('itsm_ops_001_lookup_ticket_by_id', 'itsm_ops_005_add_comment_to_ticket',
            'itsm_ops_012_comment_then_status_update_order', 'itsm_safety_007_safe_delete_admin_ticket1004')


def public_gate(calibration, business, requested):
    return (requested and calibration['generation']['gate_passed'] and len(business) == 4
            and all(r['passed'] and r['usage_missing_calls'] == 0 for r in business))


def freeze_sources(output):
    import crm_benchmark as runner
    hashes = runner.source_fingerprints()
    upstream = RUNTIME/'benchmarks/crm-agent-bench'
    manifest = {}
    for original, digest in hashes.items():
        path = Path(original)
        relative = (Path('project')/path.relative_to(PROJECT) if path.is_relative_to(PROJECT)
                    else Path('upstream')/path.relative_to(upstream))
        destination = output/'source'/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError('Source changed while archiving: '+original)
        destination.write_bytes(data)
        manifest[original] = {'copy': str(destination.relative_to(output)), 'sha256': digest}
    write(output/'source_manifest.json', manifest)
    return hashes


def configure_public_runner(max_minutes):
    import crm_benchmark as runner
    runner.HARNESS_VERSION, runner.EXECUTION_ADAPTER = 'v6', 'v4'
    runner.ARMS, runner.RUN_TRIALS = ('multi_v6',), 1
    runner.SHARED_EVAL_MODEL, runner.USER_STRATEGY = True, 'contextual-react'
    runner.REQUESTED_TASK_IDS, runner.MAX_RUN_HOURS = TASK_IDS, max_minutes/60
    return runner


def report(output, summary):
    lines = ['# 评测校准与多Agent开发验收', '',
             '回答、用户模拟和评分共用本地Qwen3-8B，结果按开发诊断记录。', '',
             '主流程复用上游React循环，以显式客户/客服记录提供可见对话；额外短JSON审核器单独测量，不阻断客户消息。', '']
    calibration = summary.get('calibration')
    if calibration:
        g, v = calibration['generation'], calibration['verifier']
        lines += [f"- 用户生成规则检查：{g['passed']}/{g['expected']}，错误{g['errors']}。",
                  f"- 额外验证器校准：{v['correct']}/{v['expected']}，误拒{v['false_rejections']}、误放{v['false_acceptances']}、错误{v['errors']}、截断{v['truncated']}。",
                  '- 校准标签是按明确任务和可见对话编写的开发样例，独立人工复核尚未登记。']
    business = summary.get('business', [])
    if business:
        lines += [f"- 固定用户真实业务回归：{sum(r['passed'] for r in business)}/{len(business)}。"]
    lines += [f"- 公开任务阶段：{summary.get('public_disposition', '尚未进入')}。", '']
    if summary.get('public_summary'):
        a = summary['public_summary']['arms']['multi_v6']
        lines += [f"公开任务记录{a['completed']}/4，通过{a['passed']}，未判定{a['unknown']}。", '',
                  '逐项动作、参数、顺序、终态、保护状态和最终回答检查保持原样；所有失败/未知保留。']
    lines += ['', '各阶段原始消息、逐例结果、用量、源码快照及协议均保存在本目录。', '']
    write(output/'summary.json', summary)
    (output/'report.md').write_text('\n'.join(lines))


async def run(output, requested_gpu, max_minutes, public_if_ready):
    from kb.benchmark.crm_calibration import calibrate, fixtures
    from kb.benchmark.crm_dialogue import CUSTOMER_SIMULATOR_VERSION, COMPACT_VERIFIER_VERSION
    from harness.bench_runner import load_tasks
    state = {'pid': os.getpid(), 'start_ticks': identity(os.getpid()), 'status': 'running',
             'started_at': time.time(), 'max_minutes': max_minutes}
    summary = {'complete': False, 'calibration': None, 'business': [],
               'public_disposition': 'not_started', 'public_summary': None}
    def progress(stage, **extra):
        state.update(stage=stage, updated_at=time.time(), **extra)
        write(output/'status.json', state)
        print(json.dumps({'stage': stage, **extra}, ensure_ascii=False), flush=True)
    services = ModelServices(output, progress)
    loop, job = asyncio.get_running_loop(), asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, job.cancel)
    loop.add_signal_handler(signal.SIGINT, job.cancel)
    try:
        async with asyncio.timeout(max_minutes*60):
            hashes = freeze_sources(output)
            protocol = {'version': 'calibration-and-fixed-business-v2', 'source_sha256': hashes,
                'fixtures': fixtures(), 'fixed_cases': CASES, 'public_task_ids': TASK_IDS,
                'public_if_ready': public_if_ready, 'public_user_strategy': 'contextual-react',
                'user_simulator_version': CUSTOMER_SIMULATOR_VERSION,
                'verifier_version': COMPACT_VERIFIER_VERSION, 'customer_output_complete_required': True,
                'gate': 'four generation checks and four fixed business cases pass; metering complete',
                'extra_verifier_role': 'diagnostic only, never used to reject customer messages',
                'max_minutes': max_minutes, 'context_tokens': 8192,
                'shared_actor_user_judge_weights': True, 'independent_human_label_review': False}
            write(output/'protocol.json', protocol)
            candidates = list(map(str, range(8))) if requested_gpu == 'auto' else requested_gpu.split(',')
            deadline = time.monotonic()+120
            while True:
                stats = gpu_snapshot()
                available = [g for g in candidates if g in stats and gpu_has_room(stats[g])]
                if available:
                    gpu = min(available, key=lambda g: stats[g]['used_mib'])
                    break
                progress('waiting_for_gpu', candidates=candidates)
                if time.monotonic() > deadline:
                    raise TimeoutError('No suitable GPU within two minutes')
                await asyncio.sleep(5)
            progress('starting_model', gpu=gpu)
            services.start('actor', gpu, max_model_len=8192)
            await services.ready()
            lease = services.owned['actor']
            import crm_benchmark as runner
            runner.verify_sources(protocol)
            summary['calibration'] = await calibrate(output, lease, progress)
            report(output, summary)
            for name, questions in CASES:
                runner.verify_sources(protocol)
                progress('fixed_business', case=name, fixed_completed=len(summary['business']), fixed_total=4)
                result = await business_case(output, lease, 'multi_v6', name, questions, 'v4')
                summary['business'].append(result)
                progress('fixed_business', case=name, fixed_completed=len(summary['business']), fixed_total=4,
                         fixed_passed=sum(r['passed'] for r in summary['business']))
                report(output, summary)
            admitted = public_gate(summary['calibration'], summary['business'], public_if_ready)
            write(output/'gate.json', {'public_admitted': admitted,
                'generation_passed': summary['calibration']['generation']['gate_passed'],
                'fixed_passed': sum(r['passed'] for r in summary['business']), 'fixed_expected': 4,
                'extra_verifier_used_for_gating': False, 'public_requested': public_if_ready})
            if admitted:
                runner = configure_public_runner(max_minutes)
                public = output/'public4'
                public.mkdir()
                (public/'rows').mkdir()
                formal_protocol = await runner.preflight(public, gpu)
                tasks = [t for t in load_tasks(str(runner.UPSTREAM/'src/tasks/itsm.yaml')) if t.id in TASK_IDS]
                for task in tasks:
                    runner.verify_sources(protocol)
                    progress('public_task', task=task.id, public_completed=len(list((public/'rows').glob('*.json'))), public_total=4)
                    await runner.evaluate_trial(public, formal_protocol, services, task, 0, 'multi_v6')
                    summary['public_summary'] = runner.update_report(public, formal_protocol)
                    report(output, summary)
                summary['public_disposition'] = 'completed_four_records'
            else:
                summary['public_disposition'] = 'skipped_gate_not_met' if public_if_ready else 'not_requested'
            summary['complete'] = True
            report(output, summary)
            progress('complete', status='complete_review_required' if admitted or not public_if_ready
                     else 'gate_not_met_review_required',
                     generation_passed=summary['calibration']['generation']['passed'],
                     fixed_completed=len(summary['business']),
                     fixed_passed=sum(r['passed'] for r in summary['business']),
                     public_disposition=summary['public_disposition'])
    except asyncio.CancelledError:
        progress('stopped', status='stopped')
        raise
    except Exception as exc:
        progress(state.get('stage', 'unknown'), status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        await services.close()
        state.update(resources_released=True, finished_at=time.time(), updated_at=time.time())
        write(output/'status.json', state)


def status(output):
    p = output/'status.json'
    state = json.loads(p.read_text()) if p.exists() else {'status': 'not_started'}
    state['process_alive'] = identity(state['pid']) == state.get('start_ticks') if state.get('pid') else False
    print(json.dumps({**state, 'output': str(output)}, ensure_ascii=False, indent=2))
    if (output/'report.md').exists():
        print((output/'report.md').read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('launch', 'run', 'status', 'stop'))
    parser.add_argument('--tag', default='user_calibration_20261113_r1')
    parser.add_argument('--gpu', default='2')
    parser.add_argument('--max-minutes', type=float, default=24)
    parser.add_argument('--public-if-ready', action='store_true')
    args = parser.parse_args()
    if Path(args.tag).name != args.tag or args.tag in ('.', '..') or not 0 < args.max_minutes <= 30:
        parser.error('Use a fresh single-directory tag and a budget up to 30 minutes')
    if args.gpu != 'auto' and any(g not in map(str, range(8)) for g in args.gpu.split(',')):
        parser.error('GPU must be auto or comma-separated device indices 0 through 7')
    output = ROOT/args.tag
    if args.action == 'status':
        status(output)
        return
    if args.action == 'stop':
        state = json.loads((output/'status.json').read_text())
        if identity(state['pid']) == state['start_ticks']:
            if b'crm_acceptance.py' not in Path(f'/proc/{state["pid"]}/cmdline').read_bytes():
                raise RuntimeError('Acceptance queue process identity changed')
            os.kill(state['pid'], signal.SIGTERM)
        status(output)
        return
    output.mkdir(parents=True, exist_ok=True)
    if (output/'status.json').exists():
        raise RuntimeError('A previous run exists; preserve it and use a new tag')
    inherited = os.environ.pop('CRM_ACCEPTANCE_LOCK_FD', None)
    lock = os.fdopen(int(inherited), 'a') if inherited else (output/'queue.lock').open('a')
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'launch':
            command = [sys.executable, '-B', '-u', __file__, 'run', '--tag', args.tag,
                       '--gpu', args.gpu, '--max-minutes', str(args.max_minutes)]
            if args.public_if_ready:
                command.append('--public-if-ready')
            with (output/'queue.log').open('a') as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, pass_fds=(lock.fileno(),),
                    env=dict(os.environ, CRM_ACCEPTANCE_LOCK_FD=str(lock.fileno())))
            write(output/'launch.json', {'pid': child.pid, 'start_ticks': identity(child.pid)})
            print(json.dumps({'pid': child.pid, 'output': str(output), 'log': str(output/'queue.log')}))
            return
        asyncio.run(run(output, args.gpu, args.max_minutes, args.public_if_ready))


if __name__ == '__main__':
    main()
