"""Durable tool effects, separate from the model's compressible context."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .contracts import ToolOutcome


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class Journal:
    def __init__(self, path: Path, namespace: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS effects ('
            'id TEXT PRIMARY KEY, namespace TEXT, request_id TEXT, name TEXT, arguments TEXT,'
            'is_write INTEGER, status TEXT, result TEXT, version TEXT, created REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS effect_goals(effect_id TEXT PRIMARY KEY,goal_id TEXT)')
        self.db.commit()
        self.namespace = namespace
        self.lock = threading.RLock()

    def records(self):
        with self.lock:
            rows = self.db.execute('SELECT * FROM effects WHERE namespace=? ORDER BY created,id',
                                   (self.namespace,)).fetchall()
        return [{**dict(r), 'arguments': json.loads(r['arguments']), 'result': json.loads(r['result'])}
                for r in rows]

    def close(self):
        self.db.close()

    def invoke(self, spec, arguments, request_id, port, emit,*,goal_id=None):
        with self.lock:
            matching = [r for r in self.records() if r['name'] == spec.name and r['arguments'] == arguments]
            if spec.write:
                unknown = next((r for r in reversed(matching) if r['status'] == 'unknown'), None)
                if unknown:
                    return {'evidence_id': unknown['id'], 'status': 'unknown',
                            'value': unknown['result'], 'reused': True,
                            'instruction': 'Use reconcile_operation before any retry of this uncertain write.'}
                committed = next((r for r in reversed(matching)if r['status']=='committed'and (
                    r['request_id']==request_id or goal_id and self.db.execute(
                        'SELECT 1 FROM effect_goals WHERE effect_id=? AND goal_id=?',(r['id'],goal_id)).fetchone())),None)
                if committed:
                    emit('effect_reused', committed)
                    return {'evidence_id': committed['id'], 'status': 'committed',
                            'value': committed['result'], 'reused': True}
            version = port.version()
            key = 'effect-' + uuid.uuid4().hex
            initial = 'unknown' if spec.write else 'observed'
            self.db.execute('INSERT INTO effects VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key,self.namespace,request_id,spec.name,canonical(arguments),int(spec.write),initial,
                 canonical({'pending': True}),version,time.time()))
            if goal_id:self.db.execute('INSERT INTO effect_goals VALUES(?,?)',(key,goal_id))
            self.db.commit()  # persist before entering a side-effecting backend
            try:
                outcome = port.execute(spec.name, arguments, key)
            except (TimeoutError, ConnectionError) as exc:
                outcome = ToolOutcome({'error': type(exc).__name__, 'needs_reconciliation': spec.write},
                                      'unknown' if spec.write else 'refused')
            except (ValueError, PermissionError, LookupError) as exc:
                outcome = ToolOutcome({'error': type(exc).__name__, 'message': str(exc)}, 'refused')
            except BaseException:
                # The durable pending write remains unknown if execution stops.
                raise
            self.db.execute('UPDATE effects SET status=?, result=?, version=? WHERE id=?',
                            (outcome.status,canonical(outcome.value),port.version(),key))
            self.db.commit()
            record = {'evidence_id': key, 'status': outcome.status, 'value': outcome.value}
            emit('tool_effect', {'name':spec.name,'arguments':arguments,'request_id':request_id,**record})
            return record

    def reconcile(self, ident, port, emit):
        with self.lock:
            record = next((r for r in self.records() if r['id'] == ident), None)
            if not record:
                raise ValueError('Operation does not belong to this conversation')
            if record['status'] != 'unknown':
                return {'evidence_id': ident, 'status':record['status'],'value':record['result']}
            result = port.reconcile(record['name'], record['arguments'], ident)
            self.db.execute('UPDATE effects SET status=?,result=?,version=? WHERE id=?',
                            (result.status,canonical(result.value),port.version(),ident))
            self.db.commit()
            emit('operation_reconciled', {'evidence_id':ident,'status':result.status,'value':result.value})
            return {'evidence_id':ident,'status':result.status,'value':result.value}

    def snapshot(self,request_id=None,*,request_ids=None,current_version=None):
        records = self.records()
        scope=request_ids if request_ids is not None else {request_id}if request_id is not None else None
        if scope is not None:
            records=[r for r in records if r['request_id']in scope or r['status']=='unknown']
        observations=[]
        for r in records[-8:]:
            observation={'evidence_id':r['id'],'tool':r['name'],
                         'status':r['status'],'value':r['result']}
            if current_version is not None:
                # The receipt proves what this tool returned at execution time;
                # it never changes into a claim about the entity's current state.
                observation.update(evidence_kind='historical_tool_receipt',recorded_at=r['created'],
                    observed_state_version=r['version'],
                    state_changed_since_observation=str(r['version'])!=str(current_version))
            observations.append(observation)
        return {'committed': [r['id'] for r in records if r['status'] == 'committed'],
                'uncertain': [r['id'] for r in records if r['status'] == 'unknown'],
                'recent_observations': observations}
