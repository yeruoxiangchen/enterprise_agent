"""Typed delegation boundaries carried by the native DeepAgents task tool.

Planning stays with the supervisor. A proposed objective is preserved together
with authoritative user messages, while an explicit write allowlist narrows a
specialist's capability. Contracts are persisted before task checkpointing and
resolved via DeepAgents' lc_subagent_tool_call_id, including forked children.
"""
from __future__ import annotations

import json

from .journal import canonical

PREFIX = 'Delegated business contract v1:\n'


def task_schema(original, writes):
    """Extend the advertised schema; the underlying upstream task API is intact."""
    schema = original.model_json_schema() if isinstance(original, type) else dict(original)
    names = sorted(writes)
    return {**schema, 'properties': {**schema['properties'],
        'allowed_write_tools': {'type': 'array',
            'items': {'type': 'string', **({'enum': names} if names else {})},
            **({} if names else {'maxItems': 0}),
            'description': 'Exact business write tool names authorized for this subtask. '
                'Use [] for checks, lookups and assessment. For an execution task list '
                'only its intended writes. Original user conditions still apply.',
            'default': []}},
        'required': [*schema.get('required', []), 'allowed_write_tools']}


class DelegationBindings:
    def __init__(self, journal, specs, groups, emit, *, protocol='explicit'):
        if protocol not in ('explicit','native'):
            raise ValueError('Unknown delegation protocol')
        self.protocol = protocol
        self.journal, self.specs, self.emit = journal, specs, emit
        self.groups = {name: set(cap['tools']) for name, cap in groups.items()}
        self.groups.setdefault('general-purpose', {s.name for s in specs.values() if not s.write})
        journal.db.execute('CREATE TABLE IF NOT EXISTS native_delegations ('
            'call_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, contract TEXT NOT NULL, arguments TEXT)')
        if 'arguments' not in {row[1] for row in journal.db.execute('PRAGMA table_info(native_delegations)')}:
            journal.db.execute('ALTER TABLE native_delegations ADD COLUMN arguments TEXT')
        journal.db.commit()

    def current(self, config):
        call_id = (config.get('metadata') or {}).get('lc_subagent_tool_call_id')
        if not call_id:
            return None
        row = self.journal.db.execute('SELECT contract FROM native_delegations WHERE call_id=?',
                                      (call_id,)).fetchone()
        # Legacy delegated checkpoints predate typed boundaries and get a
        # read-only scope until the supervisor creates a new explicit task.
        return json.loads(row['contract']) if row else {
            'allowed_write_tools': [], 'legacy_checkpoint': True, 'errors': []}

    def bind(self, call, request_id, config):
        args = dict(call['args'])
        with self.journal.lock:
            saved = self.journal.db.execute('SELECT request_id,contract,arguments FROM native_delegations WHERE call_id=?',
                                            (call['id'],)).fetchone()
            if saved:
                contract = json.loads(saved['contract'])
                bound_args = {'subagent_type': contract['subagent_type'],
                              'description': PREFIX + canonical(contract)}
                original_args = json.loads(saved['arguments']) if saved['arguments'] else None
                if saved['request_id'] != request_id or args not in (original_args, bound_args):
                    raise ValueError('Persisted delegation identity changed')
                return {**call, 'args': bound_args}
            original_args = canonical(args)
            originals = [{'request_id': row['id'], 'text': row['question']} for row in
                self.journal.db.execute('SELECT id,question FROM native_requests ORDER BY rowid')]
            role = args.get('subagent_type')
            errors = []
            if not isinstance(role, str):
                errors.append('subagent_type must be a string')
            elif role not in self.groups:
                errors.append('Unknown subagent_type; select an advertised specialist')
            if not isinstance(args.get('description'), str):
                errors.append('description must be a string')
            capability = self.groups.get(role, ()) if isinstance(role, str) else ()
            writes = {n for n in capability if self.specs[n].write}
            # Native task(description, subagent_type) selects an actual
            # capability-scoped graph. Explicit historical scopes may narrow
            # it further, but are not required from the supervisor.
            allowed = args.pop('allowed_write_tools', sorted(writes) if self.protocol=='native' else [])
            if not isinstance(allowed, list) or any(not isinstance(n, str) for n in allowed):
                errors.append('allowed_write_tools must be a list of tool names')
                allowed = []
            if set(allowed) - writes:
                errors.append('write allowlist exceeds the selected specialist capability')
            parent = self.current(config)
            if parent is not None and set(allowed) - set(parent['allowed_write_tools']):
                errors.append('a nested task cannot widen its parent write allowlist')
            contract = {'version': 1, 'protocol':self.protocol, 'subagent_type': role,
                'objective': args.get('description', ''),
                'authoritative_customer_requests': originals,
                'allowed_write_tools': sorted(set(allowed)) if not errors else [],
                'errors': errors,
                'instructions': 'Complete only this objective within the allowed tools. '
                    'Original user requests take priority over a supervisor paraphrase: '
                    'preserve conditions, negations and cancellations. The write allowlist '
                    'narrows capability and grants no extra customer authority. '
                    'Report actual receipts to the supervisor.'}
            with self.journal.db:
                self.journal.db.execute('INSERT INTO native_delegations VALUES(?,?,?,?)',
                                       (call['id'], request_id, canonical(contract), original_args))
        description = PREFIX + canonical(contract)
        self.emit('delegation_bound', {'call_id': call['id'], 'subagent_type': role,
            'request_ids': [row['request_id'] for row in originals],
            'proposed_description': contract['objective'], 'bound_description': description,
            'allowed_write_tools': contract['allowed_write_tools'], 'errors': errors})
        # Only the two supported native task arguments enter its executor.
        return {**call, 'args': {'subagent_type': role, 'description': description}}

    def allows(self, name, contract):
        return (contract is None or not self.specs[name].write or
                name in contract['allowed_write_tools'])
