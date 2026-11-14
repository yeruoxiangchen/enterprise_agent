"""Observed progress and bounded recovery; no benchmark annotations are read."""
from __future__ import annotations

import json
from collections import Counter

from .crm_contract import payload, requested_write_targets
from .crm_supervisor_state import action_counts, terminal_results


def progress_snapshot(engine):
    operation = engine.active_task['id']
    records = [r for key, r in engine.state['write_journal'].items() if json.loads(key)[0] == operation]
    committed = [r for r in records if r.get('business_outcome') == 'committed']
    completed = {(r['name'], r['arguments']['ticket_id']) for r in committed}
    targets = requested_write_targets(engine.request_text(engine.active_task))
    terminal = terminal_results(engine, targets - completed)
    counts = Counter()
    for key, record in engine.state['write_journal'].items():
        if json.loads(key)[0] == operation and record.get('business_outcome') == 'committed':
            counts[record['name']] += engine.state['write_counts'].get(key, 1)
    for name, _ in terminal:
        counts[name] += 1
    required = action_counts(engine.intent_text(), engine.write_actions(engine.active_task))
    reconcile_blocked = any(call.get('turn') == engine.owner.turn and
        isinstance(value := payload(call.get('result', '')), dict) and value.get('error') == 'write_needs_reconciliation'
        for call in engine.owner.tool_calls)
    return {'operation_id': operation,
            'target_description': engine.memory.get('target_description'),
            'remaining_actions': sorted(name for name, n in required.items() if counts[name] < n),
            'remaining_targets': sorted(targets - completed - terminal.keys()),
            'committed_receipt_ids': [r['id'] for r in committed],
            'unknown_write_result': reconcile_blocked or any(r.get('business_outcome') == 'unknown' for r in records),
            'terminal_decisions': [{'action': name, 'target': target, 'status': value['status']}
                                   for (name, target), value in sorted(terminal.items())]}


def repeated_observation(calls, turn):
    """Signal a repeated observation; never block a required lookup or mutation."""
    seen = Counter()
    for call in calls:
        if call.get('turn') != turn or not call.get('result'):
            continue
        value = payload(call['result'])
        if isinstance(value, dict) and value.get('error') == 'tool call not executed':
            continue
        key = json.dumps([call['name'], call.get('arguments', {}), value], sort_keys=True)
        seen[key] += 1
        if seen[key] >= 2:
            return {'tool': call['name'], 'occurrences': seen[key],
                    'error': value.get('error') if isinstance(value, dict) else None}
    return None


def can_recover(result, snapshot):
    return (result['status'] == 'insufficient' and not snapshot['unknown_write_result']
            and bool(snapshot['remaining_actions'] or snapshot['remaining_targets']))
