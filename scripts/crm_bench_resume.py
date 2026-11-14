"""Continue frozen actor trials while applying one common judge format revision."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time

from crm_bench_control import write


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def continuation_plan(parent, protocol, mutable_scripts):
    parent = Path(parent).resolve()
    previous = json.loads((parent/'protocol.json').read_text())
    state = json.loads((parent/'status.json').read_text())
    if not state.get('resources_released'):
        raise RuntimeError('Parent queue must have exited and released its services')
    allowed_fields = {'source_sha256', 'gpus_requested', 'evaluation_wire_format', 'continuation'}
    changed = [key for key in set(previous)|set(protocol)
               if key not in allowed_fields and previous.get(key) != protocol.get(key)]
    if changed:
        raise RuntimeError('Continuation would change actor/user/task settings: '+str(changed))
    mutable_scripts = {str(Path(p).resolve()) for p in mutable_scripts}
    changed = [p for p, sha in previous['source_sha256'].items()
               if str(Path(p).resolve()) not in mutable_scripts and file_sha(p) != sha]
    if changed:
        raise RuntimeError('Frozen actor or official benchmark source changed: '+str(changed))
    expected = {(arm, task, trial) for arm in protocol['arms'] for task in protocol['task_ids']
                for trial in range(protocol['trials'])}
    seen, entries = set(), []
    for path in sorted((parent/'rows').glob('*.json')):
        row = json.loads(path.read_text())
        key = (row['arm'], row['task_id'], row['trial'])
        if key not in expected or key in seen or row.get('smoke'):
            raise RuntimeError(f'Unexpected or duplicate parent trial: {key}')
        stem = f'{row["task_id"]}__{row["trial"]}__{row["arm"]}'
        if path.stem != stem or row['trace']['task_id'] != row['task_id']:
            raise RuntimeError(f'Parent row identity mismatch: {path}')
        seen.add(key)
        entries.append({'path': str(path), 'sha256': file_sha(path), 'filename': path.name,
                        'task_id': row['task_id'], 'trial': row['trial'], 'arm': row['arm'],
                        'grader_error': row['trace'].get('error_kind') == 'grader_infrastructure'})
    incomplete = [str(p.parent) for p in (parent/'trials').glob('*/started.json')
                  if not (p.parent/'result.json').exists()]
    if incomplete:
        raise RuntimeError('Parent has incomplete actor trials; inspect before continuing: '+str(incomplete))
    # Check previously invalid judge outputs first; every eligible saved trace,
    # including previous passes and previous failures, still gets the same judge.
    entries.sort(key=lambda item: (not item['grader_error'], item['filename']))
    return {'parent': str(parent), 'parent_protocol_sha256': file_sha(parent/'protocol.json'),
            'inherited_trials': len(entries), 'pending_actor_trials': len(expected-seen),
            'policy': 'retain every completed actor trial; regrade all judge-eligible traces uniformly',
            'entries': entries}


async def import_saved_row(target, entry, protocol, services, task):
    from harness.bench_runner import ConversationTrace, _trace_task_success
    from harness.semantic_grader import SemanticJudge, SemanticJudgeConfig
    from kb.benchmark.crm_metrics import local_verdict
    from crm_bench_services import LocalEvalClient

    if file_sha(entry['path']) != entry['sha256']:
        raise RuntimeError('Frozen parent row changed: '+entry['path'])
    row = json.loads(Path(entry['path']).read_text())
    trace = ConversationTrace(**row['trace'])
    folder = Path(target)/'regrades'/Path(entry['filename']).stem
    folder.mkdir(parents=True, exist_ok=True)
    if (folder/'started.json').exists():
        raise RuntimeError(f'Incomplete regrade retained at {folder}; inspect before repeating')
    write(folder/'started.json', {'source': entry, 'time': time.time()})
    row['source_row'] = {'path': entry['path'], 'sha256': entry['sha256']}
    row['source_trial_output'] = row['trial_output']
    row['source_finished_at'] = row['finished_at']
    row['previous_grade'] = {key: row.get(key) for key in
                             ('official_verdict', 'local_verdict', 'judge_usage', 'judge_llm_seconds')}
    row['previous_grade']['response_judge'] = trace.response_judge
    row['previous_grade']['error'] = trace.error
    row['regraded'] = False
    # Actor failures and interrupted user simulations remain failures/unknowns;
    # grading their partial responses cannot repair a failed actor conversation.
    if task.expected_response_semantic and (not trace.error or trace.error_kind == 'grader_infrastructure'):
        auxiliary = services.owned['auxiliary']
        client = LocalEvalClient(auxiliary['url'], auxiliary['model'], folder/'judge-model.jsonl',
                                 protocol['judge_seed'])
        judge = SemanticJudge(client, SemanticJudgeConfig(
            provider='openai', model=auxiliary['model'], max_tokens=protocol['judge_max_tokens'],
            max_attempts=protocol['judge_max_attempts'],
            endpoint_fingerprint=hashlib.sha256((auxiliary['model_path']+'|'+protocol['evaluation_wire_format']).encode()).hexdigest()[:16],
            cache_dir=Path(target)/'judge_cache'))
        trace.error, trace.error_kind = None, None
        trace.response_passed, trace.response_judge, trace.response_failures = None, None, []
        try:
            try:
                outcome = await judge.grade(task_id=task.id, task_instruction=task.instruction,
                                            criteria=task.expected_response_semantic, turns=trace.turns)
            except Exception as exc:
                trace.error, trace.error_kind = f'{type(exc).__name__}: {exc}', 'grader_infrastructure'
            else:
                trace.response_passed = outcome.passed
                trace.response_judge = outcome.audit
                trace.response_failures = outcome.failures
            row['regraded'] = True
            row['regrade_usage'] = dict(client.usage)
            # Keep the cost of prior invalid/valid judge attempts in the ledger.
            row['judge_usage'] = {key: row.get('judge_usage', {}).get(key, 0)+client.usage.get(key, 0)
                                  for key in set(row.get('judge_usage', {}))|set(client.usage)}
            row['judge_llm_seconds'] = row.get('judge_llm_seconds', 0)+client.seconds
        finally:
            await client.aclose()
    row['trace'] = asdict(trace)
    row['official_verdict'] = _trace_task_success(trace)
    row['local_verdict'] = local_verdict(row['official_verdict'], trace.error)
    row['trial_output'] = str(folder)
    row['finished_at'] = time.time()
    write(folder/'result.json', row)
    write(Path(target)/'rows'/entry['filename'], row)
    return row
