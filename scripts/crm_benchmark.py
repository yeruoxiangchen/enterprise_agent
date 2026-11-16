"""Pinned public ITSM tasks, our v5 adapter, and a paired single-agent baseline."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, replace
import fcntl
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time

import yaml

from crm_bench_control import RUNTIME, identity, write

PROJECT = Path(__file__).resolve().parents[1]
UPSTREAM = RUNTIME/'benchmarks/crm-agent-bench'
OUTPUT = RUNTIME/'artifacts/crm_itsm_20261109'
ARMS = ('single', 'multi_v5')
HARNESS_VERSION = 'v5'
RUN_TRIALS = 3
MAX_RUN_HOURS = None
EXECUTION_ADAPTER = 'v3'
REQUESTED_TASK_IDS = ()
SHARED_EVAL_MODEL = False
USER_STRATEGY = 'verified'
FULL_ITSM = False
SUPERVISOR_STRATEGY = 'baseline'


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_fingerprints():
    files = list((PROJECT/'kb').rglob('*.py'))
    files += [PROJECT/'scripts'/name for name in ('crm_benchmark.py', 'crm_bench_services.py',
                                                'crm_bench_control.py', 'crm_bench_run.sh', 'crm_bench_resume.py',
                                                'crm_acceptance.py', 'crm_small_check.py',
                                                'crm_latency_probe.py', 'crm_latency_profile.py',
                                                'experience_memory.py', 'incident_acceptance.py', 'public_screenshot_check.py',
                                                'setup_incident_ocr.sh')]
    files += [p for p in (UPSTREAM/'src/harness').rglob('*.py')]
    files += [UPSTREAM/p for p in ('src/tasks/itsm.yaml', 'src/agents/itsm.yaml',
                                 'src/tasks/policies/itsm_policy.md', 'benchmark/benchmark.yaml',
                                 'benchmark/benchmark.lock.json')]
    return {str(p): digest(p) for p in sorted(files)}


def archive_sources(target, protocol):
    manifest = {}
    for original, expected in protocol['source_sha256'].items():
        path = Path(original)
        relative = (Path('project')/path.relative_to(PROJECT) if path.is_relative_to(PROJECT)
                    else Path('upstream')/path.relative_to(UPSTREAM))
        destination = target/'source'/relative
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError('Source changed while archiving: '+original)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        manifest[original] = {'copy': str(destination.relative_to(target)), 'sha256': expected}
    write(target/'source_manifest.json', manifest)


async def preflight(target, gpus, resume_from=None):
    from harness.bench_runner import load_tasks
    from harness.benchmark_spec import validate_lock, validate_gold_task
    from harness.tools.environment import create_environment
    import harness.tools.domains
    from crm_bench_services import WIRE_FORMAT_VERSION, GPU_MEMORY_FRACTION, GPU_HEADROOM_MIB, PREFILL_TOKENS
    if FULL_ITSM and (HARNESS_VERSION != 'v6' or EXECUTION_ADAPTER != 'v4' or REQUESTED_TASK_IDS
            or RUN_TRIALS != 1 or set(ARMS) != {'official_graph','multi_v6'} or len(ARMS) != 2):
        raise ValueError('Full ITSM needs all 66 tasks, official_graph/multi_v6, v6/v4 and one trial')
    errors = validate_lock()
    if errors:
        raise RuntimeError('Upstream freeze validation failed: '+str(errors))
    gold_errors = []
    for task in yaml.safe_load((UPSTREAM/'src/tasks/itsm.yaml').read_text())['tasks']:
        gold_errors.extend(await validate_gold_task('itsm', task))
    if gold_errors:
        raise RuntimeError('Upstream gold paths failed: '+str(gold_errors))
    tasks = load_tasks(str(UPSTREAM/'src/tasks/itsm.yaml'))
    if len(tasks) != 66 or len({t.id for t in tasks}) != 66:
        raise RuntimeError('Expected exactly 66 distinct frozen ITSM tasks')
    if REQUESTED_TASK_IDS:
        unknown = set(REQUESTED_TASK_IDS)-{t.id for t in tasks}
        if unknown or len(set(REQUESTED_TASK_IDS)) != len(REQUESTED_TASK_IDS):
            raise ValueError('Diagnostic task IDs must be distinct public ITSM tasks: '+str(sorted(unknown)))
        tasks = [t for t in tasks if t.id in REQUESTED_TASK_IDS]
    commit = subprocess.check_output(['git', '-C', str(UPSTREAM), 'rev-parse', 'HEAD'], text=True).strip()
    config = yaml.safe_load((UPSTREAM/'src/agents/itsm.yaml').read_text())
    policy = (UPSTREAM/'src/tasks/policies/itsm_policy.md').read_text()
    for task in tasks:
        env = create_environment('itsm', task.initial_state)
        names = set(env.create_registry().tool_names)
        configured = {tool['name'] for node in config['nodes'].values() for tool in node['tools']}
        if names != configured:
            raise RuntimeError('Canonical tools and adapter capability schemas differ')
    adapter_version = 'business_v5_external_v1'
    if HARNESS_VERSION == 'v6':
        if EXECUTION_ADAPTER == 'v4':
            from kb.benchmark.crm_supervisor import EXECUTION_VERSION
        elif EXECUTION_ADAPTER == 'v3':
            from kb.benchmark.crm_execution import EXECUTION_VERSION
        else:
            from kb.benchmark.crm_adapter_v6 import EXECUTION_VERSION
        adapter_version = EXECUTION_VERSION
    protocol = {
        'version': 1, 'upstream_commit': commit, 'adapter': adapter_version,
        'execution_adapter': EXECUTION_ADAPTER if HARNESS_VERSION == 'v6' else 'v5',
        'diagnostic_subset': bool(REQUESTED_TASK_IDS), 'available_public_tasks': 66,
        'task_ids': [t.id for t in tasks], 'trials': RUN_TRIALS, 'arms': list(ARMS),
        'expected_trials': len(tasks)*RUN_TRIALS*len(ARMS),
        'max_run_hours': MAX_RUN_HOURS,
        'wall_time_scope': 'queue startup through evaluation; includes GPU wait and model loading',
        'base_seed': 20261109, 'context_tokens': 16384, 'auxiliary_context_tokens': 12288, 'output_tokens': 1400,
        'model_calls_per_turn': 12, 'tool_calls_per_turn': 15, 'max_turns': 30,
        'router_calls_per_turn': 2, 'standalone_social_closing_reply': 'host_same_for_both_arms',
        'task_timeout_seconds': 1200, 'agent_step_timeout_seconds': 240,
        'user_step_timeout_seconds': 90, 'http_timeout_seconds': 60,
        'actor': 'Qwen3-8B', 'actor_temperature': 0, 'user_model': 'Qwen3-VL-8B-Instruct',
        'user_temperature': .5, 'user_max_tokens': 1086,
        'judge_model': 'Qwen3-VL-8B-Instruct', 'judge_temperature': 0, 'judge_max_tokens': 4096,
        'judge_seed': 20261109, 'judge_max_attempts': 3,
        'evaluation_output_budget': 'server-tokenizer-remaining-context-v1',
        'latency_profile_enabled': os.environ.get('CRM_LATENCY_PROFILE') == '1',
        'evaluation_wire_format': WIRE_FORMAT_VERSION,
        'independent_actor_and_judge_weights': True,
        'gpus_requested': gpus, 'gpu_memory_fraction_each': GPU_MEMORY_FRACTION,
        'gpu_spare_mib': GPU_HEADROOM_MIB, 'shared_gpus_allowed': True,
        'max_num_batched_tokens': PREFILL_TOKENS,
        'latency_scope': 'wall time on recorded local GPUs; shared GPU load may vary',
        'safetensors_load_strategy': 'eager',
        'public_policy': policy, 'public_agent_config': config,
        'source_sha256': source_fingerprints(),
        'dependencies': {name: importlib.metadata.version(name) for name in
                         ('openai', 'httpx', 'pydantic', 'PyYAML', 'langchain',
                          'langchain-openai', 'langgraph', 'langgraph-checkpoint-sqlite')},
        'resumption': 'skip completed trial records; do not automatically restart failed services',
        'score_scope': f'public ITSM subset, local simulator/judge protocol, {HARNESS_VERSION} external-tool adapter',
    }
    if HARNESS_VERSION == 'v6':
        from kb.benchmark.crm_dialogue import SIMULATOR_VERSION
        verified_user = EXECUTION_ADAPTER in ('v3', 'v4')
        protocol.update(paired_user_openings=True, conversation_stall_repetitions=3,
                        user_fidelity_validation=verified_user,
                        simulator_version=SIMULATOR_VERSION if verified_user else 'paired-react-v2')
        if verified_user:
            protocol['user_temperature'] = 0
        if EXECUTION_ADAPTER == 'v4':
            from kb.benchmark.crm_supervisor import MODEL_CALL_LIMIT, TOOL_CALL_LIMIT, CHECKPOINT_DURABILITY
            protocol.pop('gpu_memory_fraction_each', None)
            protocol.update(context_tokens=8192, model_calls_per_turn=MODEL_CALL_LIMIT,
                            tool_calls_per_turn=TOOL_CALL_LIMIT, router_calls_per_turn=None,
                            auxiliary_context_tokens=8192,
                            gpu_memory_fractions={'actor': GPU_MEMORY_FRACTION, 'auxiliary': .75},
                            orchestration='LangChain supervisor and specialists as tools',
                            checkpoint_durability=CHECKPOINT_DURABILITY,
                            simulator_verifier='upstream',
                            task_timeout_seconds=180, agent_step_timeout_seconds=120, max_turns=8)
    if FULL_ITSM:
        protocol.update(evaluation_profile='full-itsm66-v1', full_itsm=True,
                        max_turns=30, task_timeout_seconds=600,
                        skip_duplicate_integration_trials=True,
                        score_scope='complete public ITSM 66-task catalogue; local evaluation protocol')
    protocol['shared_evaluation_model'] = SHARED_EVAL_MODEL
    protocol['supervisor_strategy'] = SUPERVISOR_STRATEGY
    if SHARED_EVAL_MODEL:
        protocol.update(independent_actor_and_judge_weights=False,
                        user_model=protocol['actor'], judge_model=protocol['actor'],
                        auxiliary_context_tokens=protocol['context_tokens'],
                        gpu_memory_fractions={'actor': GPU_MEMORY_FRACTION},
                        score_scope='public ITSM development subset; shared actor/user/judge weights')
        if FULL_ITSM:
            protocol['score_scope'] = 'complete public ITSM 66-task catalogue; shared actor/user/judge weights'
    protocol['user_strategy'] = 'verified' if protocol.get('user_fidelity_validation') else 'react'
    if HARNESS_VERSION == 'v6' and USER_STRATEGY in ('react', 'contextual-react'):
        from kb.benchmark.crm_dialogue import CUSTOMER_SIMULATOR_VERSION
        contextual = USER_STRATEGY == 'contextual-react'
        protocol.update(user_strategy=USER_STRATEGY, paired_user_openings=False, user_fidelity_validation=False,
                        simulator_verifier=None, user_temperature=0, user_output_complete=contextual,
                        simulator_version=CUSTOMER_SIMULATOR_VERSION if contextual else 'upstream-react-default')
    protocol['adapter_by_arm'] = {arm: adapter_version for arm in ARMS}
    if 'official_graph' in ARMS:
        if HARNESS_VERSION != 'v6' or EXECUTION_ADAPTER != 'v4':
            raise ValueError('Official comparison requires the v6/v4 matched budget')
        from kb.benchmark.crm_reference import EXECUTION_VERSION as REFERENCE_VERSION
        protocol['adapter_by_arm']['official_graph'] = REFERENCE_VERSION
        protocol['reference'] = {'engine': 'harness.graph_agent.GraphAgent', 'runtime': 'plain_python',
            'nodes': 'harness.graph_cli._build_nodes_from_config', 'max_iterations_per_node': 10,
            'public_policy': 'same policy appended to reference system prompt',
            'custom_business_guards': False, 'custom_receipt_or_pending_state': False,
            'tool_registry': 'canonical tools with observation-only audit wrapper',
            'model_calls_per_turn': protocol['model_calls_per_turn'],
            'tool_calls_per_turn': protocol['tool_calls_per_turn'],
            'output_tokens': protocol['output_tokens'], 'seed': 'constant per trial, same as candidates'}
        protocol['standalone_social_closing_reply'] = {
            'official_graph': 'upstream router and model', 'multi_v6': 'host standalone acknowledgement'}
        protocol['arm_execution_order'] = 'rotate_by_task'
    if resume_from:
        from crm_bench_resume import continuation_plan
        scripts = [PROJECT/'scripts'/name for name in ('crm_benchmark.py', 'crm_bench_services.py',
                                                       'crm_bench_control.py', 'crm_bench_run.sh', 'crm_bench_resume.py')]
        plan = continuation_plan(OUTPUT/resume_from, protocol, scripts)
        plan_path = target/'resume_plan.json'
        if plan_path.exists() and read(plan_path) != plan:
            raise RuntimeError('Frozen continuation inputs changed; inspect the parent run')
        write(plan_path, plan)
        protocol['continuation'] = {k: v for k, v in plan.items() if k != 'entries'}
        protocol['continuation']['plan_sha256'] = digest(plan_path)
    previous = target/'protocol.json'
    if previous.exists() and read(previous) != protocol:
        raise RuntimeError('Frozen protocol or source changed; use a new --tag')
    write(previous, protocol)
    if 'official_graph' in ARMS and not (target/'source_manifest.json').exists():
        archive_sources(target, protocol)
    write(target/'preflight.json', {'passed': True, 'gold_paths': 66, 'unique_tasks': 66,
                                    'upstream_commit': commit, 'time': time.time()})
    return protocol


def verify_sources(protocol):
    changed = [p for p, sha in protocol['source_sha256'].items() if digest(p) != sha]
    if changed:
        raise RuntimeError('Frozen source changed: '+str(changed))


def update_report(target, protocol):
    from kb.benchmark.crm_metrics import summarize, report_markdown
    rows = [read(p) for p in sorted((target/'rows').glob('*.json'))]
    summary = summarize(rows, protocol['task_ids'], protocol['trials'], arms=tuple(protocol['arms']))
    summary['evaluation'] = {key: protocol.get(key) for key in
        ['score_scope', 'shared_evaluation_model', 'independent_actor_and_judge_weights',
         'actor', 'user_model', 'judge_model', 'simulator_verifier', 'user_strategy']}
    write(target/'summary.json', summary)
    (target/'report.md').write_text(report_markdown(summary))
    return summary


def finish_deterministic_checks(task, trace, adapter):
    from harness.grading import match_expected_args, check_order, check_forbidden
    from harness.tools.state_validation import validate_state, validate_forbidden
    # Preserve rejected attempts as well as successfully executed calls,
    # including calls made before an error interrupted an agent turn.
    actual_calls = sorted(adapter.tool_calls, key=lambda d: d.get('execution_index', float('inf')))
    trace.tool_calls_detail = actual_calls
    trace.tools_called = [d['name'] for d in actual_calls]
    counts = Counter(d['name'] for d in actual_calls if d['ok'])
    trace.actions_matched = []
    for name in task.expected_actions:
        if counts[name] > 0:
            counts[name] -= 1
            trace.actions_matched.append(name)
    ok_calls = [d for d in actual_calls if d['ok']]
    if task.expected_args:
        trace.args_passed, trace.args_failures = match_expected_args(ok_calls, task.expected_args,
                                                                  mode=task.expected_args_mode)
    if task.expected_order:
        trace.order_passed, trace.order_failures = check_order([d['name'] for d in ok_calls],
                                                              task.expected_order, mode=task.expected_order_mode)
    if task.forbidden_actions or task.forbidden_args:
        trace.forbidden_passed, trace.forbidden_failures = check_forbidden(
            actual_calls, forbidden_actions=task.forbidden_actions, forbidden_args=task.forbidden_args)
    if task.expected_state:
        trace.state_passed, trace.state_failures = validate_state(adapter.environment.get_state(), task.expected_state)
    if task.forbidden_state:
        trace.collateral_passed, trace.collateral_failures = validate_forbidden(adapter.environment.get_state(),
                                                                            task.forbidden_state)


def create_trial_adapter(arm, environment, settings, protocol, seed, folder):
    if arm == 'official_graph':
        from kb.benchmark.crm_reference import OfficialGraphAdapter
        return OfficialGraphAdapter(environment=environment, settings=settings,
            config=protocol['public_agent_config'], policy=protocol['public_policy'], seed=seed, output=folder,
            model_call_limit=protocol['model_calls_per_turn'], tool_call_limit=protocol['tool_calls_per_turn'])
    from kb.benchmark.crm_adapter import CRMAdapter
    return CRMAdapter(arm=arm, environment=environment, settings=settings,
        config=protocol['public_agent_config'], policy=protocol['public_policy'], seed=seed, output=folder,
        execution_adapter=protocol.get('execution_adapter', 'v2') if arm in ('single_v6', 'multi_v6') else 'v2')


async def evaluate_trial(target, protocol, services, task, trial, arm, *, smoke=False):
    from harness.bench_runner import run_task, _trace_task_success
    from harness.benchmark_spec import derive_trial_seed
    from harness.semantic_grader import SemanticJudge, SemanticJudgeConfig
    from harness.tools.environment import create_environment
    from harness.user_simulator import ReactUserSimulator
    from kb.config import Settings
    from kb.benchmark.crm_metrics import local_verdict
    from crm_bench_services import LocalEvalClient
    folder = target/('smoke' if smoke else 'trials')/f'{task.id}__{trial}__{arm}'
    folder.mkdir(parents=True, exist_ok=True)
    if (folder/'started.json').exists():
        raise RuntimeError(f'Incomplete trial exists at {folder}; retained for inspection, not silently restarted')
    seed = derive_trial_seed(protocol['base_seed'], task.id, trial)
    random.seed(seed)
    write(folder/'started.json', {'time': time.time(), 'task': task.id, 'trial': trial, 'arm': arm, 'seed': seed})
    actor = services.owned['actor']
    aux = evaluation_service(protocol, services)
    settings = Settings(root=folder, mode='agent', llm_url=actor['url']+'/v1',
                        llm_model=actor['model'], llm_key='local', tokenize_url=actor['url']+'/tokenize',
                        context_tokens=protocol['context_tokens'], output_tokens=protocol['output_tokens'],
                        timeout=protocol['http_timeout_seconds'],
                        supervisor_strategy=protocol.get('supervisor_strategy', 'baseline'))
    adapter = create_trial_adapter(arm, create_environment('itsm', task.initial_state), settings, protocol, seed, folder)
    # These clients and task assertions remain exclusively on the evaluation side.
    user_client = LocalEvalClient(aux['url'], aux['model'], folder/'user-model.jsonl', seed,
                                 require_complete=protocol.get('user_output_complete', False),
                                 context_tokens=aux['max_model_len'])
    judge_client = LocalEvalClient(aux['url'], aux['model'], folder/'judge-model.jsonl', protocol['judge_seed'],
                                  context_tokens=aux['max_model_len'])
    from kb.benchmark.crm_dialogue import ContextualReactUserSimulator
    user_class = ContextualReactUserSimulator if protocol.get('user_strategy') == 'contextual-react' else ReactUserSimulator
    user = user_class(user_client, temperature=protocol['user_temperature'],
                      max_tokens=protocol['user_max_tokens'], max_message_chars=4000)
    if protocol.get('paired_user_openings'):
        from kb.benchmark.crm_dialogue import PairedUserSimulator
        opening_path = target/('smoke_openings' if smoke else 'paired_openings')/f'{task.id}__{trial}.json'
        user = PairedUserSimulator(user_client, temperature=protocol['user_temperature'],
            max_tokens=protocol['user_max_tokens'], max_message_chars=4000,
            opening_path=opening_path, identity={'seed': seed, 'model': aux['model'],
                'wire': protocol['evaluation_wire_format'], 'temperature': protocol['user_temperature'],
                'max_tokens': protocol['user_max_tokens'],
                'verifier': protocol.get('simulator_verifier', 'local')}, adapter=adapter, arm=arm,
            repeated_turns=protocol['conversation_stall_repetitions'],
            verify_fidelity=protocol.get('user_fidelity_validation', False),
            verification_mode=protocol.get('simulator_verifier', 'local'))
    judge = SemanticJudge(judge_client, SemanticJudgeConfig(
        provider='openai', model=aux['model'], max_tokens=protocol['judge_max_tokens'],
        max_attempts=protocol['judge_max_attempts'],
        endpoint_fingerprint=hashlib.sha256((aux['model_path']+'|'+protocol['evaluation_wire_format']).encode()).hexdigest()[:16],
        cache_dir=target/'judge_cache'))
    try:
        trace = await run_task(task, adapter, user, semantic_judge=judge,
                               max_turns=protocol['max_turns'], timeout_seconds=protocol['task_timeout_seconds'],
                               agent_step_timeout_seconds=protocol['agent_step_timeout_seconds'],
                               user_step_timeout_seconds=protocol['user_step_timeout_seconds'])
        trace.trial, trace.seed = trial, seed
        finish_deterministic_checks(task, trace, adapter)
        trace.agent_usage = {'prompt_tokens': adapter.prompt_tokens, 'completion_tokens': adapter.completion_tokens,
                             'total_tokens': adapter.prompt_tokens+adapter.completion_tokens,
                             'num_calls': adapter.model_calls}
        trace.agent_model, trace.user_sim_model = actor['model'], aux['model']
        trace.agent_llm_seconds = adapter.llm_seconds
        trace.user_sim_usage = user_client.usage
        adapter.write_environment()
        row = {'arm': arm, 'task_id': task.id, 'trial': trial, 'seed': seed, 'smoke': smoke,
               'evaluation_scope': protocol['score_scope'],
               'independent_actor_and_judge_weights': protocol['independent_actor_and_judge_weights'],
               'official_verdict': _trace_task_success(trace), 'trace': asdict(trace),
               'agent_usage': {**trace.agent_usage, 'usage_missing_calls': adapter.usage_missing_calls},
               'agent_turn_times': adapter.turn_times, 'user_usage': user_client.usage,
               'judge_usage': judge_client.usage, 'user_llm_seconds': user_client.seconds,
               'judge_llm_seconds': judge_client.seconds,
               'finished_at': time.time(), 'trial_output': str(folder)}
        row['local_verdict'] = local_verdict(row['official_verdict'], trace.error)
        if arm in ('single_v6', 'multi_v6', 'official_graph'):
            row['execution_outcomes'] = adapter.execution_outcomes
            row['execution_version'] = protocol.get('adapter_by_arm', {}).get(arm, protocol['adapter'])
            row['paired_opening'] = getattr(user, 'opening_metadata', None)
        write(folder/'result.json', row)
        if not smoke:
            write(target/'rows'/f'{task.id}__{trial}__{arm}.json', row)
        return row
    finally:
        await user_client.aclose()
        await judge_client.aclose()
        if hasattr(adapter, 'aclose'):
            await adapter.aclose()


def evaluation_service(protocol, services):
    """Separate clients/contexts may share a lease only in declared development mode."""
    return services.owned['actor' if protocol.get('shared_evaluation_model') else 'auxiliary']


def select_gpus(stats, candidates, fractions, shared_evaluation=False):
    from crm_bench_services import gpu_has_room
    available = sorted(candidates, key=lambda g: (stats[g]['used_mib'], stats[g]['utilization']))
    for actor in available:
        if not gpu_has_room(stats[actor], fractions.get('actor', .78)):
            continue
        if shared_evaluation:
            return [actor]
        for auxiliary in available:
            if actor != auxiliary and gpu_has_room(stats[auxiliary], fractions.get('auxiliary', .78)):
                return [actor, auxiliary]
    return []


async def run(target, gpus, live_acceptance=False, resume_from=None):
    from harness.bench_runner import load_tasks
    from crm_bench_services import ModelServices, gpu_snapshot, gpu_has_room
    state = {'pid': os.getpid(), 'start_ticks': identity(os.getpid()), 'started_at': time.time()}
    if (target/'budget.json').exists():
        state.update(read(target/'budget.json'))
    def progress(stage, **kwargs):
        state.update(stage=stage, updated_at=time.time(), **kwargs)
        write(target/'status.json', state)
        print(json.dumps({'stage': stage, **kwargs}, ensure_ascii=False), flush=True)
    services = ModelServices(target, progress)
    try:
        progress('preflight', status='running')
        protocol = None if live_acceptance else await preflight(target, gpus, resume_from)
        expected_trials = protocol['expected_trials'] if protocol else 66*RUN_TRIALS*len(ARMS)
        progress('preflight_passed', total=expected_trials, completed=0,
                 score_scope=protocol['score_scope'] if protocol else 'live acceptance')
        tasks = load_tasks(str(UPSTREAM/'src/tasks/itsm.yaml'))
        if protocol:
            tasks = [t for t in tasks if t.id in protocol['task_ids']]
        if protocol and protocol.get('continuation'):
            progress('continuation_verified', resume_from=resume_from,
                     inherited_trials=protocol['continuation']['inherited_trials'],
                     pending_actor_trials=protocol['continuation']['pending_actor_trials'])
        candidates = list(map(str, range(8))) if gpus == 'auto' else gpus.split(',')
        deadline = time.monotonic()+6*3600
        while True:
            stats = gpu_snapshot()
            fractions = protocol.get('gpu_memory_fractions', {}) if protocol else {}
            shared = bool(protocol and protocol.get('shared_evaluation_model'))
            selected = select_gpus(stats, candidates, fractions, shared)
            if selected:
                break
            progress('waiting_for_gpus', status='waiting_for_gpus', candidates=candidates, gpu_snapshot=stats)
            if time.monotonic() > deadline:
                raise TimeoutError('No suitable GPU allocation within 6 hours')
            await asyncio.sleep(5)
        progress('starting_models', status='running', gpus=selected)
        services.start('actor', selected[0], max_model_len=protocol['context_tokens'] if protocol else None,
                       memory_fraction=fractions.get('actor', .78))
        if not shared:
            services.start('auxiliary', selected[1], max_model_len=protocol['auxiliary_context_tokens'] if protocol else None,
                           memory_fraction=fractions.get('auxiliary', .78))
        await services.ready()
        if protocol and protocol.get('continuation'):
            from crm_bench_resume import import_saved_row
            plan_path = target/'resume_plan.json'
            if digest(plan_path) != protocol['continuation']['plan_sha256']:
                raise RuntimeError('Frozen continuation manifest changed')
            plan = read(plan_path)
            tasks_by_id = {task.id: task for task in tasks}
            consecutive_infra = 0
            for index, entry in enumerate(plan['entries']):
                if (target/'rows'/entry['filename']).exists():
                    continue
                verify_sources(protocol)
                progress('regrading_saved_dialogues', task=entry['task_id'], arm=entry['arm'],
                         inherited_processed=index, inherited_total=len(plan['entries']),
                         completed=len(list((target/'rows').glob('*.json'))), total=expected_trials)
                row = await import_saved_row(target, entry, protocol, services, tasks_by_id[entry['task_id']])
                summary = update_report(target, protocol)
                state.update(completed=summary['completed_trials'], inherited_processed=index+1,
                             updated_at=time.time())
                write(target/'status.json', state)
                consecutive_infra = consecutive_infra+1 if row['local_verdict'] is None else 0
                if consecutive_infra >= 3:
                    raise RuntimeError('Three consecutive ungradeable saved trials; inspect before continuing')
            parent_receipt = Path(plan['parent'])/'smoke_complete.json'
            if not parent_receipt.is_file() or not read(parent_receipt).get('passed'):
                raise RuntimeError('Parent actor/tool integration receipt is missing')
            write(target/'smoke_complete.json', {**read(parent_receipt), 'inherited': True,
                  'source': str(parent_receipt), 'source_sha256': digest(parent_receipt),
                  'scope': 'original actor/tool integration; current judge applied uniformly to saved traces'})
        if live_acceptance:
            # Keep the expensive services warm while separate, fresh Python
            # processes validate the real bridge. Freeze formal source only
            # AFTER this acceptance, so fixes never rewrite a frozen protocol.
            deadline = time.monotonic()+1800
            while not (target/'live_acceptance.json').exists():
                progress('waiting_for_live_acceptance', status='running')
                if time.monotonic() > deadline:
                    raise TimeoutError('Live bridge acceptance not completed in 1800 seconds')
                for lease in services.owned.values():
                    if identity(lease['pid']) != lease['start_ticks']:
                        raise RuntimeError('Owned model exited during live acceptance')
                await asyncio.sleep(5)
            acceptance = read(target/'live_acceptance.json')
            verify_sources(acceptance['protocol'])
            protocol = await preflight(target, gpus)
            write(target/'smoke_complete.json', acceptance)
        # Fixed first task from each capability: execute the pipeline without
        # using task success as a tuning or admission criterion.
        smoke_tasks = []
        seen = set()
        for task in tasks:
            capability = task.capabilities[0]
            if capability not in seen:
                seen.add(capability)
                smoke_tasks.append(task)
        smoke_receipt = target/'smoke_complete.json'
        if protocol.get('skip_duplicate_integration_trials') and not smoke_receipt.exists():
            write(smoke_receipt, {'passed': None, 'skipped': True, 'checks': [],
                'accuracy_is_not_admission_gate': True,
                'scope': 'Full catalogue evaluates each task once per arm directly; no duplicate preliminary trials.'})
        if protocol.get('diagnostic_subset') and not smoke_receipt.exists():
            write(smoke_receipt, {'passed': True, 'checks': [], 'accuracy_is_not_admission_gate': True,
                                 'scope': 'Small diagnostic subset runs directly; no duplicate integration cases.'})
        if not smoke_receipt.exists():
            checks = []
            for task in smoke_tasks:
                for arm in ARMS:
                    progress('smoke', task=task.id, arm=arm)
                    row = await evaluate_trial(target, protocol, services, task, 0, arm, smoke=True)
                    # Model task mistakes are benchmark outcomes. Infrastructure,
                    # missing metering and broken runtime prevent a valid run.
                    if (row['trace']['error'] and row['local_verdict'] is None) or row['agent_usage']['usage_missing_calls']:
                        raise RuntimeError(f'Integration check failed: {row["trial_output"]}/result.json')
                    checks.append({'task': task.id, 'arm': arm, 'verdict': row['official_verdict']})
            write(smoke_receipt, {'passed': True, 'checks': checks, 'accuracy_is_not_admission_gate': True})
        consecutive_infra = 0
        for trial in range(protocol['trials']):
            order = list(tasks)
            random.Random(protocol['base_seed']+trial).shuffle(order)
            for task_index, task in enumerate(order):
                arms = ARMS if trial % 2 == 0 else ARMS[::-1]
                if protocol.get('arm_execution_order') == 'rotate_by_task':
                    shift = task_index % len(arms)
                    arms = arms[shift:]+arms[:shift]
                for arm in arms:
                    path = target/'rows'/f'{task.id}__{trial}__{arm}.json'
                    if path.exists():
                        continue
                    verify_sources(protocol)
                    for lease in services.owned.values():
                        if identity(lease['pid']) != lease['start_ticks']:
                            raise RuntimeError('Owned model exited; stop without automatic restart')
                    progress('evaluation', task=task.id, trial=trial+1, arm=arm,
                             completed=len(list((target/'rows').glob('*.json'))), total=expected_trials)
                    row = await evaluate_trial(target, protocol, services, task, trial, arm)
                    summary = update_report(target, protocol)
                    state.update(completed=summary['completed_trials'], updated_at=time.time())
                    write(target/'status.json', state)
                    consecutive_infra = consecutive_infra+1 if row['local_verdict'] is None else 0
                    print(json.dumps({'completed': summary['completed_trials'], 'arm': arm,
                                      'success': row['official_verdict'], 'error': row['trace']['error']}, ensure_ascii=False), flush=True)
                    if consecutive_infra >= 3:
                        raise RuntimeError('Three consecutive ungradeable trials; inspect results before continuing')
        summary = update_report(target, protocol)
        if not summary['complete']:
            raise RuntimeError(f'Expected {expected_trials} completed paired trials')
        progress('complete', status='complete_review_required', completed=expected_trials, total=expected_trials)
    except asyncio.CancelledError:
        progress('cancelled', status='cancelled')
        raise
    except Exception as exc:
        progress(state.get('stage', 'unknown'), status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        await services.close()
        state['resources_released'] = True
        write(target/'status.json', state)


async def run_with_budget(target, operation, max_hours=None):
    """Cancel at the wall-time limit; the existing run finally releases leases.

    A real infrastructure TimeoutError remains an error. Only expiration of this
    timer is reported as a budget-limited completion, with partial coverage.
    """
    if max_hours is None:
        return await operation()
    started = time.time()
    budget = {'max_run_hours': max_hours, 'budget_started_at': started,
              'deadline_at': started+max_hours*3600}
    write(target/'budget.json', budget)
    timer = asyncio.timeout(max_hours*3600)
    try:
        async with timer:
            return await operation()
    except TimeoutError:
        if not timer.expired():
            raise
        state = read(target/'status.json') if (target/'status.json').exists() else {}
        summary = update_report(target, read(target/'protocol.json')) if (target/'protocol.json').exists() else None
        state.update(status='time_budget_complete_review_required', stage='complete',
                     stop_reason='wall_time_budget', updated_at=time.time(),
                     completed=summary['completed_trials'] if summary else 0,
                     total=summary['expected_trials'] if summary else 66*RUN_TRIALS*len(ARMS))
        state.pop('error', None)
        write(target/'status.json', state)
        print(json.dumps({'status': state['status'], 'completed': state['completed'],
                          'total': state['total'], 'resources_released': state.get('resources_released', False)}), flush=True)
    finally:
        budget.update(finished_at=time.time(), elapsed_seconds=time.time()-started)
        write(target/'budget.json', budget)


def status(target):
    state = read(target/'status.json') if (target/'status.json').exists() else {'status': 'not_started'}
    live = bool(state.get('pid')) and identity(state['pid']) == state.get('start_ticks')
    print(json.dumps({**state, 'process_alive': live, 'output': str(target)}, ensure_ascii=False, indent=2))
    if (target/'summary.json').exists():
        summary = read(target/'summary.json')
        print(f"任务运行记录：{summary['completed_trials']}/{summary['expected_trials']}")
        for arm, a in summary['arms'].items():
            repeats = summary.get('trials', 3)
            all_pass = a.get('all_trials_success', a.get('all_three_success'))
            print(f"{arm}: 通过 {a['passed']}/{a['completed']}；评测异常/未判定 {a['unknown']}；"
                  f"{repeats}次全成功 {all_pass}/{a['reliability_tasks_completed']}")


async def live_probe(target, gpus, accept=False):
    from harness.bench_runner import load_tasks
    from types import SimpleNamespace
    leases = read(target/'services.json')
    if len(leases) != 2 or any(lease.get('released') or identity(lease['pid']) != lease['start_ticks']
                               for lease in leases.values()):
        raise RuntimeError('Live probe requires both owned model services')
    folder = target/'live_probes'/str(time.time_ns())
    folder.mkdir(parents=True)
    protocol = await preflight(folder, gpus)
    tasks, seen = [], set()
    for task in load_tasks(str(UPSTREAM/'src/tasks/itsm.yaml')):
        if task.capabilities[0] not in seen:
            seen.add(task.capabilities[0])
            tasks.append(task)
    checks = []
    for task in tasks:
        for arm in ARMS:
            print(json.dumps({'live_probe': task.id, 'arm': arm, 'output': str(folder)}), flush=True)
            row = await evaluate_trial(folder, protocol, SimpleNamespace(owned=leases), task, 0, arm, smoke=True)
            checked = {'task': task.id, 'arm': arm, 'verdict': row['official_verdict'],
                       'error': row['trace']['error'], 'usage_missing_calls': row['agent_usage']['usage_missing_calls']}
            checks.append(checked)
            write(folder/'checks.json', checks)
            if (checked['error'] and row['local_verdict'] is None) or checked['usage_missing_calls']:
                raise RuntimeError(f'Live bridge check failed; retained at {row["trial_output"]}/result.json')
    verify_sources(protocol)
    acceptance = {'passed': True, 'checks': checks, 'protocol': protocol, 'probe_output': str(folder),
                  'accuracy_is_not_admission_gate': True, 'accepted_at': time.time()}
    write(target/'live_probe_complete.json', acceptance)
    if accept:
        write(target/'live_acceptance.json', acceptance)
    print('Six real-model bridge checks completed; receipt saved.', flush=True)


def main():
    global ARMS, HARNESS_VERSION, RUN_TRIALS, MAX_RUN_HOURS, EXECUTION_ADAPTER, REQUESTED_TASK_IDS, SHARED_EVAL_MODEL, USER_STRATEGY, FULL_ITSM, SUPERVISOR_STRATEGY
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('launch', 'run', 'status', 'stop', 'preflight', 'report', 'probe'))
    parser.add_argument('--tag', default='itsm66_v10')
    parser.add_argument('--gpus', default='auto')
    parser.add_argument('--live-acceptance', action='store_true')
    parser.add_argument('--accept-live', action='store_true')
    parser.add_argument('--resume-from', help='Import frozen completed actor trials and regrade them uniformly')
    parser.add_argument('--harness-version', choices=('v5', 'v6'), default='v5')
    parser.add_argument('--trials', type=int, choices=(1, 2, 3), default=3)
    parser.add_argument('--execution-adapter', choices=('v2', 'v3', 'v4'), default='v3')
    parser.add_argument('--supervisor-strategy', choices=('baseline', 'recovery', 'experience'), default='baseline')
    parser.add_argument('--arms', nargs='+', choices=('single', 'multi_v5', 'single_v6', 'multi_v6', 'official_graph'),
                        help='Explicit arms for a small run; defaults to the paired protocol')
    parser.add_argument('--task-ids', help='Comma-separated public task IDs for a small diagnostic run')
    parser.add_argument('--full-itsm', action='store_true',
                        help='Explicit full 66-task official_graph vs multi_v6 comparison, one trial per arm')
    parser.add_argument('--shared-eval-model', action='store_true',
                        help='Single-GPU local evaluation; actor, user and judge share model weights')
    parser.add_argument('--user-strategy', choices=('verified', 'react', 'contextual-react'), default='verified',
                        help='User simulator; contextual-react uses explicit speaker-labelled visible dialogue')
    parser.add_argument('--max-hours', type=float, help='Wall-time limit including model loading; retain completed rows and release GPUs')
    args = parser.parse_args()
    if args.max_hours is not None and not 0 < args.max_hours <= 72:
        parser.error('--max-hours must be positive and at most 72')
    RUN_TRIALS, MAX_RUN_HOURS = args.trials, args.max_hours
    EXECUTION_ADAPTER = args.execution_adapter
    SUPERVISOR_STRATEGY = args.supervisor_strategy
    REQUESTED_TASK_IDS = tuple(item.strip() for item in (args.task_ids or '').split(',') if item.strip())
    HARNESS_VERSION = args.harness_version
    SHARED_EVAL_MODEL = args.shared_eval_model
    USER_STRATEGY = args.user_strategy
    FULL_ITSM = args.full_itsm
    if SUPERVISOR_STRATEGY != 'baseline' and (HARNESS_VERSION != 'v6' or EXECUTION_ADAPTER != 'v4'):
        parser.error('--supervisor-strategy requires v6/v4')
    shared_scope_ok = ((0 < len(REQUESTED_TASK_IDS) <= 4 and not FULL_ITSM)
                       or (FULL_ITSM and not REQUESTED_TASK_IDS))
    if SHARED_EVAL_MODEL and (HARNESS_VERSION != 'v6' or EXECUTION_ADAPTER != 'v4'
            or not shared_scope_ok or RUN_TRIALS != 1
            or MAX_RUN_HOURS is None or args.resume_from or args.live_acceptance or args.action == 'probe'):
        parser.error('--shared-eval-model needs v6/v4, explicit small tasks or --full-itsm, one trial, --max-hours and a fresh run')
    if HARNESS_VERSION == 'v6':
        ARMS = ('single_v6', 'multi_v6')
        if args.tag == 'itsm66_v10':
            parser.error('v6 needs a new explicit --tag; the frozen v10 output is reserved')
    if args.arms:
        available_arms = set(ARMS) | ({'official_graph'} if HARNESS_VERSION == 'v6' and EXECUTION_ADAPTER == 'v4' else set())
        if len(set(args.arms)) != len(args.arms) or not set(args.arms) <= available_arms:
            parser.error('--arms must be distinct arms matching the selected harness version')
        ARMS = tuple(args.arms)
    if FULL_ITSM and (HARNESS_VERSION != 'v6' or EXECUTION_ADAPTER != 'v4' or REQUESTED_TASK_IDS
            or RUN_TRIALS != 1 or set(ARMS) != {'official_graph','multi_v6'} or len(ARMS) != 2
            or MAX_RUN_HOURS is None or args.resume_from or args.live_acceptance or args.action == 'probe'):
        parser.error('--full-itsm requires v6/v4, official_graph and multi_v6, no task filter, one trial and --max-hours')
    if Path(args.tag).name != args.tag or args.tag in ('.', '..'):
        parser.error('tag must be one directory name')
    if args.resume_from and (Path(args.resume_from).name != args.resume_from
                            or args.resume_from in ('.', '..', args.tag) or args.live_acceptance):
        parser.error('--resume-from needs a distinct parent tag and the standard execution mode')
    target = OUTPUT/args.tag
    if args.action == 'status':
        status(target)
        return
    target.mkdir(parents=True, exist_ok=True)
    (target/'rows').mkdir(exist_ok=True)
    if args.action == 'stop':
        state = read(target/'status.json')
        if identity(state['pid']) == state['start_ticks']:
            command = Path(f'/proc/{state["pid"]}/cmdline').read_bytes()
            if b'crm_benchmark.py' not in command:
                raise RuntimeError('Queue identity changed')
            os.kill(state['pid'], signal.SIGTERM)
        status(target)
        return
    if args.action == 'report':
        update_report(target, read(target/'protocol.json'))
        status(target)
        return
    if args.action == 'probe':
        with (target/'probe.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(live_probe(target, args.gpus, args.accept_live))
        return
    inherited_fd = os.environ.pop('CRM_BENCH_LOCK_FD', None)
    lock_file = os.fdopen(int(inherited_fd), 'a') if inherited_fd else (target/'queue.lock').open('a')
    with lock_file as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'preflight':
            asyncio.run(preflight(target, args.gpus, args.resume_from))
            print('66 public tasks, canonical freeze and gold paths passed')
            return
        if args.action == 'launch':
            command = [sys.executable, '-B', '-u', __file__, 'run', '--tag', args.tag, '--gpus', args.gpus,
                       '--harness-version', HARNESS_VERSION, '--trials', str(RUN_TRIALS),
                       '--execution-adapter', EXECUTION_ADAPTER,
                       '--supervisor-strategy', SUPERVISOR_STRATEGY]
            if REQUESTED_TASK_IDS:
                command += ['--task-ids', ','.join(REQUESTED_TASK_IDS)]
            if args.arms:
                command += ['--arms', *args.arms]
            if SHARED_EVAL_MODEL:
                command.append('--shared-eval-model')
            if FULL_ITSM:
                command.append('--full-itsm')
            command += ['--user-strategy', USER_STRATEGY]
            if MAX_RUN_HOURS is not None:
                command += ['--max-hours', str(MAX_RUN_HOURS)]
            if args.live_acceptance:
                command.append('--live-acceptance')
            if args.resume_from:
                command += ['--resume-from', args.resume_from]
            with (target/'queue.log').open('a') as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True, pass_fds=(lock.fileno(),),
                                         env=dict(os.environ, CRM_BENCH_LOCK_FD=str(lock.fileno())))
            # Inherit the held lock to close the launch/start race. The child
            # reuses this descriptor rather than taking an unrelated lock.
            write(target/'launch.json', {'pid': child.pid, 'start_ticks': identity(child.pid),
                                         'lock_fd': lock.fileno(), 'time': time.time()})
            print(json.dumps({'pid': child.pid, 'output': str(target), 'log': str(target/'queue.log')}))
            return
        async def guarded():
            loop = asyncio.get_running_loop()
            job = asyncio.current_task()
            loop.add_signal_handler(signal.SIGTERM, job.cancel)
            loop.add_signal_handler(signal.SIGINT, job.cancel)
            await run_with_budget(target, lambda: run(target, args.gpus, args.live_acceptance, args.resume_from), MAX_RUN_HOURS)
        if os.environ.get('CRM_LATENCY_PROFILE') == '1':
            from crm_latency_profile import LatencyProfile
            with LatencyProfile(target/'latency-profile.json').observe():
                asyncio.run(guarded())
        else:
            asyncio.run(guarded())


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    main()
