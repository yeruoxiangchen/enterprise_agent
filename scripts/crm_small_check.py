"""Single-GPU, time-bounded development checks; eight cases plus user fidelity.

Fixed user messages exercise the real model, graph and canonical ITSM tools.
The same model also runs the isolated simulator check. This is a development
acceptance run, separate from the two-model public-benchmark protocol.
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
import traceback
from types import SimpleNamespace

import yaml

from crm_bench_control import RUNTIME, identity, write
from crm_bench_services import ModelServices, LocalEvalClient, gpu_snapshot, gpu_has_room

PROJECT = Path(__file__).resolve().parents[1]
UPSTREAM = RUNTIME/'benchmarks/crm-agent-bench'
ROOT = RUNTIME/'artifacts/crm_short_checks_20261113'
ARMS = ('single_v6', 'multi_v6')
CONTEXT_TOKENS = 8192
CASES = (
    ('query', ["What's the current status of ticket TCK-1001?"]),
    ('semantic_note', ['TCK-1001 needs the note "Short-check note".']),
    ('ordered_write', ['Add "Ready for review" to TCK-1001, then set its status to Resolved.']),
    ('identity', ['Delete TCK-1004.', 'My user ID is admin.rlee.']),
)


def check_case(name, owner, replies, before):
    state = owner.environment.get_state()
    writes = [c for c in owner.tool_calls if c.get('business_outcome') == 'committed']
    if name == 'query':
        return {'correct_status': 'Open' in replies[-1] and 'TCK-1001' in replies[-1],
                'read_only': state == before, 'metered': owner.usage_missing_calls == 0}
    if name == 'semantic_note':
        return {'stored_once': state['tickets']['TCK-1001']['comments'] == ['Short-check note'],
                'confirmed_from_receipt': 'Short-check note' in replies[-1], 'one_write': len(writes) == 1}
    if name == 'ordered_write':
        return {'correct_order': [c['name'] for c in writes] == ['add_ticket_comment', 'update_ticket_status'],
                'stored_once': state['tickets']['TCK-1001']['comments'] == ['Ready for review'],
                'correct_status': state['tickets']['TCK-1001']['status'] == 'Resolved'}
    return {'asked_for_identity': any(word in replies[0].lower() for word in ['user', 'identity', 'account']),
            'deleted_after_identity': 'TCK-1004' not in state['tickets'],
            'one_deletion': [c['name'] for c in writes] == ['delete_ticket'],
            'no_unsupported_identity_executed': all(c['arguments'].get('user_id') == 'admin.rlee'
                for c in owner.tool_calls if c['name'] == 'check_permission' and c.get('backend_executed'))}


async def business_case(output, lease, arm, name, questions, execution_adapter='v3'):
    from harness.models import ChatMessage, Role
    from harness.tools.environment import create_environment
    import harness.tools.domains
    from kb.benchmark.crm_adapter import CRMAdapter
    from kb.config import Settings
    folder = output/'cases'/f'{name}__{arm}'
    folder.mkdir(parents=True)
    owner = CRMAdapter(arm=arm, environment=create_environment('itsm'),
        settings=Settings(root=folder, mode='agent', llm_url=lease['url']+'/v1', llm_key='local',
            llm_model=lease['model'], tokenize_url=lease['url']+'/tokenize',
            context_tokens=lease['max_model_len'], output_tokens=1400, timeout=60),
        config=yaml.safe_load((UPSTREAM/'src/agents/itsm.yaml').read_text()),
        policy=(UPSTREAM/'src/tasks/policies/itsm_policy.md').read_text(), seed=20261113,
        output=folder, execution_adapter=execution_adapter)
    before = owner.environment.get_state()
    history, replies = [], []
    started = time.monotonic()
    error = None
    detail = None
    try:
        for index, question in enumerate(questions):
            response = await asyncio.wait_for(owner.run(question, history), timeout=180)
            replies.append(response.content)
            history += [ChatMessage(role=Role.USER, content=question), ChatMessage(role=Role.ASSISTANT, content=response.content)]
            if name == 'identity' and index == 0 and 'TCK-1004' not in owner.environment.get_state()['tickets']:
                raise RuntimeError('Deletion occurred before a user identity was supplied')
        checks = check_case(name, owner, replies, before)
    except Exception as exc:
        error, detail = f'{type(exc).__name__}: {exc}', traceback.format_exc()
        checks = {'execution_completed': False}
    checks['metered'] = owner.usage_missing_calls == 0
    result = {'case': name, 'arm': arm, 'passed': all(checks.values()), 'checks': checks,
              'questions': questions, 'replies': replies, 'seconds': time.monotonic()-started,
              'model_calls': owner.model_calls, 'prompt_tokens': owner.prompt_tokens,
              'completion_tokens': owner.completion_tokens, 'usage_missing_calls': owner.usage_missing_calls,
              'tool_calls': owner.tool_calls, 'execution_outcomes': owner.execution_outcomes}
    if name == 'identity':
        unsupported = [c for c in owner.tool_calls if c['name'] == 'check_permission'
                       and c['arguments'].get('user_id') != 'admin.rlee']
        result['identity_metrics'] = {'unsupported_attempts': len(unsupported),
            'unsupported_backend_executions': sum(bool(c.get('backend_executed')) for c in unsupported)}
    if error:
        result.update(error=error, traceback=detail)
    owner.write_environment()
    write(folder/'result.json', result)
    return result


async def simulator_case(output, lease):
    from harness.user_simulator import STOP_SIGNAL
    from harness.tools.environment import create_environment
    import harness.tools.domains
    from kb.benchmark.crm_dialogue import PairedUserSimulator
    folder = output/'cases/simulator_stop'
    folder.mkdir(parents=True)
    def emit(kind, data):
        with (folder/'events.jsonl').open('a') as stream:
            stream.write(json.dumps({'kind': kind, 'data': data}, ensure_ascii=False)+'\n')
    client = LocalEvalClient(lease['url'], lease['model'], folder/'user-model.jsonl', 20261113)
    owner = SimpleNamespace(tool_calls=[], environment=create_environment('itsm'), emit=emit)
    sim = PairedUserSimulator(client, temperature=0, max_tokens=800,
        opening_path=folder/'opening.json', identity={'seed': 20261113, 'mode': 'single-model-development'},
        adapter=owner, arm='development', verify_fidelity=True)
    sim.reset('You want to know the current status of ticket TCK-1001. Ask for its status, then end as soon as the agent tells you the status. Do not add any other requests.')
    try:
        opening = await sim.step('')
        ending = await sim.step('Ticket TCK-1001 is Open, priority High, assigned to agent.jsmith.')
        result = {'case': 'simulator_stop', 'passed': ending == STOP_SIGNAL,
                  'opening': opening, 'ending': ending, 'usage': client.usage}
        write(folder/'result.json', result)
        return result
    finally:
        await client.aclose()


async def run(output, requested_gpu, max_minutes, execution_adapter='v3', arms=ARMS, skip_simulator=False):
    state = {'pid': os.getpid(), 'start_ticks': identity(os.getpid()), 'started_at': time.time(),
             'status': 'running', 'completed': 0, 'total': len(arms)*len(CASES)+int(not skip_simulator), 'max_minutes': max_minutes,
             'context_tokens': CONTEXT_TOKENS,
             'scope': 'single-model development checks; fixed users; separate simulator fidelity check'}
    def progress(stage, **extra):
        if stage in ('business_case', 'simulator_fidelity'):
            state.pop('passed', None)
        state.update(stage=stage, updated_at=time.time(), **extra)
        write(output/'status.json', state)
        print(json.dumps({'stage': stage, **extra}, ensure_ascii=False), flush=True)
    services = ModelServices(output, progress)
    results = []
    timer = asyncio.timeout(max_minutes*60)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    loop.add_signal_handler(signal.SIGINT, task.cancel)
    try:
        async with timer:
            candidates = list(map(str, range(8))) if requested_gpu == 'auto' else requested_gpu.split(',')
            deadline = time.monotonic()+120
            while True:
                snapshot = gpu_snapshot()
                available = [i for i in candidates if i in snapshot and gpu_has_room(snapshot[i])]
                if available:
                    gpu = min(available, key=lambda i: snapshot[i]['used_mib'])
                    break
                progress('waiting_for_gpu', candidates=candidates, gpu_snapshot=snapshot)
                if time.monotonic() > deadline:
                    raise TimeoutError('No suitable GPU within the two-minute development-check wait')
                await asyncio.sleep(5)
            progress('starting_model', gpu=gpu)
            services.start('actor', gpu, max_model_len=CONTEXT_TOKENS)
            await services.ready()
            lease = services.owned['actor']
            write(output/'protocol.json', {'scope': state['scope'], 'execution_adapter': execution_adapter,
                'shared_actor_and_simulator_weights': True, 'model': lease['model'], 'gpu': gpu,
                'context_tokens': lease['max_model_len'],
                'cases': CASES, 'arms': arms, 'official_benchmark_score': False,
                'source_sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                    [Path(__file__), PROJECT/'kb/benchmark/crm_execution.py', PROJECT/'kb/benchmark/crm_supervisor.py',
                     PROJECT/'kb/benchmark/crm_adapter.py', PROJECT/'kb/benchmark/crm_dialogue.py']}})
            for arm in arms:
                for name, questions in CASES:
                    progress('business_case', arm=arm, case=name)
                    result = await business_case(output, lease, arm, name, questions, execution_adapter)
                    results.append(result)
                    write(output/'summary.json', {'complete': False, 'results': results})
                    progress('case_complete', completed=len(results), passed=result['passed'])
                    if not result['passed']:
                        raise RuntimeError(f'Development check failed: {name}/{arm}; inspect its result.json')
            if not skip_simulator:
                progress('simulator_fidelity')
                result = await asyncio.wait_for(simulator_case(output, lease), timeout=180)
                results.append(result)
            write(output/'summary.json', {'complete': True, 'passed': all(r['passed'] for r in results), 'results': results})
            if not all(r['passed'] for r in results):
                raise RuntimeError('The simulated user did not stop after a correct status answer')
            progress('complete', status='complete_review_required', completed=len(results))
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
    path = output/'status.json'
    state = json.loads(path.read_text()) if path.exists() else {'status': 'not_started'}
    state['process_alive'] = identity(state['pid']) == state.get('start_ticks') if state.get('pid') else False
    print(json.dumps({**state, 'output': str(output)}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['launch', 'run', 'status', 'stop'])
    parser.add_argument('--tag', default='v3_short_20261113')
    parser.add_argument('--gpu', default='auto')
    parser.add_argument('--max-minutes', type=float, default=15)
    parser.add_argument('--execution-adapter', choices=('v3', 'v4'), default='v3')
    parser.add_argument('--arms', nargs='+', choices=ARMS, default=list(ARMS))
    parser.add_argument('--skip-simulator', action='store_true')
    args = parser.parse_args()
    if Path(args.tag).name != args.tag or args.tag in ['.', '..'] or not 0 < args.max_minutes <= 30:
        parser.error('Use a single tag name and a development budget of at most 30 minutes')
    output = ROOT/args.tag
    if args.action == 'status':
        status(output)
        return
    output.mkdir(parents=True, exist_ok=True)
    if args.action == 'stop':
        state = json.loads((output/'status.json').read_text())
        if identity(state['pid']) == state['start_ticks']:
            command = Path(f'/proc/{state["pid"]}/cmdline').read_bytes()
            if b'crm_small_check.py' not in command:
                raise RuntimeError('Development queue identity changed')
            os.kill(state['pid'], signal.SIGTERM)
        status(output)
        return
    inherited = os.environ.pop('CRM_SMALL_LOCK_FD', None)
    lock_file = os.fdopen(int(inherited), 'a') if inherited else (output/'queue.lock').open('a')
    with lock_file as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'launch':
            command = [sys.executable, '-B', '-u', __file__, 'run', '--tag', args.tag,
                       '--gpu', args.gpu, '--max-minutes', str(args.max_minutes),
                       '--execution-adapter', args.execution_adapter, '--arms', *args.arms]
            if args.skip_simulator:
                command.append('--skip-simulator')
            with (output/'queue.log').open('a') as stream:
                child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=True, pass_fds=(lock.fileno(),),
                    env=dict(os.environ, CRM_SMALL_LOCK_FD=str(lock.fileno())))
            write(output/'launch.json', {'pid': child.pid, 'start_ticks': identity(child.pid)})
            print(json.dumps({'pid': child.pid, 'output': str(output), 'log': str(output/'queue.log')}, ensure_ascii=False))
            return
        if (output/'status.json').exists():
            raise RuntimeError('A previous development run exists; use a new tag, preserving its results')
        asyncio.run(run(output, args.gpu, args.max_minutes, args.execution_adapter, tuple(args.arms), args.skip_simulator))


if __name__ == '__main__':
    main()
