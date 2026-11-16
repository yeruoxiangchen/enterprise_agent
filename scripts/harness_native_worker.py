"""One isolated native/custom execution with source-frozen scripted users."""
import argparse
import asyncio
from dataclasses import asdict
import faulthandler
import json
from pathlib import Path
import signal
import time

import yaml
from harness.bench_runner import ConversationTrace, Task, load_tasks
from harness.tools.environment import create_environment
import harness.tools.domains

from kb.benchmark.crm_deep_adapter import DeepCRMAdapter
from kb.benchmark.crm_native_adapter import NativeCRMAdapter
from kb.config import Settings
from crm_benchmark import finish_deterministic_checks
from crm_bench_control import write
from harness_native_cases import CASES, GUARD_CASES, EXTRA_CASES, gate_ready, expanded_gate_ready, mechanism_checks

ROOT = Path('/data/zjr/enterprise_kb_agent')
UPSTREAM = ROOT/'benchmarks/crm-agent-bench'


def task_for_case(case):
    if case['kind']=='cancellation':
        return Task(id=case['task_id'],instruction='Scripted cancellation development check.')
    return next(t for t in load_tasks(str(UPSTREAM/'src/tasks/itsm.yaml'))if t.id==case['task_id'])


def deterministic_checks(task, trace):
    from harness.grading import compute_action_pass
    checks = {'required_actions': compute_action_pass(task.expected_actions, trace.actions_matched)}
    for name in ('state_passed', 'args_passed', 'order_passed', 'collateral_passed', 'forbidden_passed'):
        if getattr(trace, name) is not None:
            checks[name] = getattr(trace, name)
    return checks


async def run(args):
    folder = Path(args.output).resolve()
    if not folder.is_relative_to(ROOT):
        raise ValueError('Use project data storage')
    folder.mkdir(parents=True, exist_ok=True)
    case = next(c for c in [*CASES,*GUARD_CASES] if c['task_id'] == args.task)
    task = task_for_case(case)
    service = json.loads(Path(args.service_manifest).read_text())['actor']
    settings = Settings(root=folder, mode='agent', llm_url=service['url']+'/v1', llm_model=service['model'],
        llm_key='local', tokenize_url=service['url']+'/tokenize', context_tokens=8192, output_tokens=1400,
        timeout=90, harness_model_calls=40, harness_tool_calls=48,
        harness_goal_review=False, harness_compact_handoffs=True)
    adapter_type = NativeCRMAdapter if args.arm in ('native_deepagents', 'native_context','native_rubric','native_forked') else DeepCRMAdapter
    extra = ({'fork_subagents':True,'operation_permissions':True,'prepare_operations':True,
              'specialist_owned_checks':True}if args.arm=='native_forked'else
        {'shared_context': True} if args.arm == 'native_context' else {'completion_review':True}if args.arm=='native_rubric'else{})
    if args.confirmation_guard:
        extra['require_dependency_confirmation']=True
    adapter = adapter_type(create_environment('itsm', task.initial_state), settings,
        yaml.safe_load((UPSTREAM/'src/agents/itsm.yaml').read_text()),
        (UPSTREAM/'src/tasks/policies/itsm_policy.md').read_text(), folder, seed=20261114, **extra)
    trace = ConversationTrace(task_id=task.id, expected_actions=list(task.expected_actions))
    started = time.monotonic()
    error = None
    gate_blocked = None
    fixture_checks = {}
    try:
        for turn, item in enumerate(case['turns']):
            if item.get('when') and not gate_ready(item['when'], adapter):
                gate_blocked = item['when']
                break
            trace.turns.append({'role': 'user', 'content': item['message']})
            write(folder/'progress.json', {'stage': 'actor_turn', 'turn': turn,
                'updated_at': time.time(), 'model_calls': adapter.model_calls})
            kwargs={'approval':item['approval']}if item.get('approval')and args.confirmation_guard else{}
            response = await asyncio.wait_for(adapter.run(item['message'], trace.turns[:-1],**kwargs), timeout=180)
            trace.turns.append({'role': 'assistant', 'content': response.content})
            finish_deterministic_checks(task, trace, adapter)
            fixture_checks = mechanism_checks(case, adapter, trace.turns)
            checks = {**deterministic_checks(task, trace), **fixture_checks}
            if all(checks.values()):
                break
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        trace.error = error
        trace.error_kind = 'actor_execution_failure'
    finally:
        finish_deterministic_checks(task, trace, adapter)
        fixture_checks = mechanism_checks(case, adapter, trace.turns)
        checks = {**deterministic_checks(task, trace), **fixture_checks}
        row = {'task_id': task.id, 'case_kind': case['kind'], 'arm': args.arm,
            'scope': 'scripted-user development mechanism check; final response semantics not graded',
            'passed': error is None and gate_blocked is None and all(checks.values()),
            'checks': checks, 'error': error, 'gate_blocked': gate_blocked,
            'tokens': adapter.prompt_tokens+adapter.completion_tokens, 'model_calls': adapter.model_calls,
            'usage_missing_calls': adapter.usage_missing_calls, 'actor_turn_times': adapter.turn_times,
            'completion_review':getattr(adapter.engine,'review_evaluations',[]),
            'completion_review_status':getattr(adapter.engine,'last_review_status',None),
            'seconds': time.monotonic()-started, 'trace': asdict(trace),
            'effects': adapter.engine.journal.records(), 'audit': adapter.port.audit,
            'environment': adapter.environment.get_state()}
        write(folder/'result.json', row)
        await adapter.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--service-manifest', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--arm', choices=['native_deepagents', 'native_context', 'native_rubric', 'native_forked', 'v17_custom'], required=True)
    parser.add_argument('--confirmation-guard',action='store_true')
    parser.add_argument('--suite',choices=('dev8','expanded16'),default='dev8')
    args = parser.parse_args()
    if args.suite=='expanded16':
        CASES=[*CASES,*EXTRA_CASES]
        gate_ready=expanded_gate_ready
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'stacks.log').open('a') as stream:
        faulthandler.enable(file=stream, all_threads=True)
        faulthandler.register(signal.SIGUSR1, file=stream, all_threads=True)
        faulthandler.dump_traceback_later(165, file=stream, repeat=True)
        try:
            asyncio.run(run(args))
        finally:
            faulthandler.cancel_dump_traceback_later()
