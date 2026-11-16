"""Outcome reward using upstream business assertions and actual effect audit."""
from __future__ import annotations

from collections import Counter
import copy
from dataclasses import fields


def score_task(sample, adapter, *, error=None):
    from harness.bench_runner import Task
    from harness.grading import compute_action_pass, match_expected_args, check_order, check_forbidden
    from harness.tools.state_validation import validate_state, validate_forbidden

    allowed = {field.name for field in fields(Task)}
    task = Task(**{key: value for key, value in sample['task'].items() if key in allowed})
    calls = sorted(adapter.tool_calls, key=lambda row: row.get('execution_index', float('inf')))
    successes = [call for call in calls if call['ok']]
    counts = Counter(call['name'] for call in successes)
    matched = []
    for name in task.expected_actions:
        if counts[name]:
            counts[name] -= 1
            matched.append(name)
    checks = {'required_actions': compute_action_pass(task.expected_actions, matched)}
    failures = {}
    for label, value, operation in [
        ('arguments', task.expected_args, lambda: match_expected_args(successes, task.expected_args, mode=task.expected_args_mode)),
        ('order', task.expected_order, lambda: check_order([r['name'] for r in successes], task.expected_order, mode=task.expected_order_mode)),
        ('state', task.expected_state, lambda: validate_state(adapter.environment.get_state(), task.expected_state)),
        ('forbidden_state', task.forbidden_state, lambda: validate_forbidden(adapter.environment.get_state(), task.forbidden_state)),
    ]:
        if value:
            checks[label], failures[label] = operation()
    checks['forbidden_attempts'], failures['forbidden_attempts'] = check_forbidden(
        calls, forbidden_actions=task.forbidden_actions, forbidden_args=task.forbidden_args)
    before = copy.deepcopy(task.initial_state)
    after = adapter.environment.get_state()
    targets = {sample['target_ticket']}
    checks['unrelated_tickets_unchanged'] = (
        set(after['tickets'])-targets == set(before['tickets'])-targets and all(
        after['tickets'].get(ident) == ticket for ident, ticket in before['tickets'].items() if ident not in targets))
    checks['users_unchanged'] = after['users'] == before['users']
    from kb.harness.ports import WRITES
    checks['write_attempt_targets_scoped'] = all(
        row['arguments'].get('ticket_id') in targets for row in calls if row['name'] in WRITES)
    writes = [r for r in adapter.engine.journal.records() if r['is_write'] and r['status'] == 'committed']
    expected_writes = Counter(name for name in task.expected_actions
        if name in adapter.engine.specs and adapter.engine.specs[name].write)
    actual_writes = Counter(row['name'] for row in writes)
    checks['no_extra_committed_writes'] = all(count <= expected_writes[name] for name, count in actual_writes.items())
    checks['execution_completed'] = error is None
    reward = float(all(checks.values()))
    return {'reward': reward, 'checks': checks, 'failures': failures,
            'error': error, 'semantic_answer_checked': False,
            'reward_source': 'upstream executable assertions and real business state',
            'committed_writes': dict(actual_writes)}
