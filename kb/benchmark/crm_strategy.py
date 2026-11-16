"""Observed progress and bounded recovery; no benchmark annotations are read."""
from __future__ import annotations

import json
import re
from collections import Counter

from .crm_contract import mask_quoted, payload, requested_write_targets, requested_writes, split_explicit_steps, ticket_ids, tool_hints
from .crm_supervisor_state import action_counts, terminal_results


def permission_action(text):
    clean = mask_quoted(text)
    actions = {name for name, pattern in {
        'delete': r'\b(?:delete|deletion|remove)\b|删除',
        'escalate': r'\b(?:escalate|escalation)\b|升级',
        'assign': r'\b(?:assign|reassign|assignment)\b|分配|转派',
        'edit': r'\b(?:edit|modify)\b|编辑', 'view': r'\b(?:view|read)\b|查看',
    }.items() if re.search(pattern, clean, re.I)}
    return next(iter(actions)) if len(actions) == 1 else None


def requested_reads(engine):
    """Explicit lookups from visible dialogue, including post-write checks."""
    text = engine.request_text(engine.active_task)
    requirements = []
    previous_write = False
    for start, end in split_explicit_steps(text):
        clause = text[start:end]
        clean = mask_quoted(clause)
        hints, writes = tool_hints(clause), requested_writes(clause)
        if 'get_ticket' in hints or (previous_write and re.search(
                r'\b(?:confirm|verify|look\s+(?:it\s+)?up|read\s*back)\b|复查|核实', clean, re.I)):
            identifiers = ticket_ids(clause)
            if not identifiers and len(ticket_ids(text)) == 1:
                identifiers = ticket_ids(text)
            for ticket in sorted(identifiers) if identifiers else [None]:
                requirements.append({'name': 'get_ticket',
                    'arguments': {'ticket_id': ticket} if ticket else {}, 'after_write': previous_write or bool(writes)})
        if 'get_business_hours' in hints:
            requirements.append({'name': 'get_business_hours', 'arguments': {}, 'after_write': False})
        # Mutation preconditions remain the executor's responsibility; an
        # explicit permission question is itself a read goal.
        if 'check_permission' in hints and not writes:
            action = permission_action(clause)
            arguments = {'action': action} if action else {}
            from .crm_supervisor_state import user_identities
            identities = user_identities(clause, {}) or user_identities(
                clause, engine.owner.environment.get_state().get('session', {}))
            if len(identities) == 1:
                arguments['user_id'] = next(iter(identities))
            if len(ticket_ids(clause)) == 1:
                arguments['ticket_id'] = next(iter(ticket_ids(clause)))
            requirements.append({'name': 'check_permission', 'arguments': arguments, 'after_write': False})
        if not writes and re.search(r'\b(?:check|show|list|what)\b[^.?!]{0,50}\bdependencies\b|查询依赖', clean, re.I):
            for ticket in sorted(ticket_ids(clause)):
                requirements.append({'name': 'check_dependencies', 'arguments': {'ticket_id': ticket}, 'after_write': False})
        previous_write = previous_write or bool(writes)
    return list({json.dumps(check, sort_keys=True): check for check in requirements}.values())


def remaining_reads(engine, committed):
    observations = engine.progress.observations()
    calls = {(c['turn'], c['id']): c for c in engine.owner.tool_calls}
    last_write = max((r.get('execution_index', -1) for r in committed), default=-1)
    terminal = terminal_results(engine, engine.write_targets(engine.active_task, engine.request_text(engine.active_task)))
    result = []
    for check in requested_reads(engine):
        found = False
        evidence = list(observations)
        if not check['after_write']:
            # A completed pre-write lookup remains a completed step after a
            # mutation invalidates the cache. It is not a current-state fact.
            evidence.extend(call for call in engine.owner.tool_calls
                if call.get('task_id') == engine.active_task['id']
                and call.get('backend_executed') and call.get('ok')
                and (check['name'] not in engine.fresh_read_names
                     or call.get('turn') == engine.owner.turn))
        for observation in evidence:
            if observation['name'] != check['name'] or not all(
                    observation['arguments'].get(k) == v for k, v in check['arguments'].items()):
                continue
            # Observations already exclude stale business versions. Explicit
            # verification additionally requires a real read after the write.
            source = calls.get((observation.get('turn'), observation.get('id')), {})
            already_terminal = any(ticket == check['arguments'].get('ticket_id') for _, ticket in terminal)
            if check['after_write'] and not already_terminal and (not committed or source.get('execution_index', -1) <= last_write):
                continue
            found = True; break
        if not found:
            result.append(check)
    return result


def progress_snapshot(engine):
    operation = engine.active_task['id']
    records = [r for key, r in engine.state['write_journal'].items() if json.loads(key)[0] == operation]
    committed = [r for r in records if r.get('business_outcome') == 'committed']
    completed = {(r['name'], r['arguments']['ticket_id']) for r in committed}
    targets = engine.write_targets(engine.active_task, engine.request_text(engine.active_task))
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
            'remaining_reads': remaining_reads(engine, committed),
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
            and bool(snapshot['remaining_actions'] or snapshot['remaining_targets'] or snapshot.get('remaining_reads')))
