"""Host-owned plans, progress and bounded handoffs shared by v6 executors.

This module knows neither benchmark task IDs nor expected answers. Dependencies
refer to user source spans; tool observations and side effects stay auditable.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .business_multi_v2 import validate_plan

DOMAINS = ('technical', 'policy', 'service')


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra='forbid')
    domain: Literal['technical', 'policy', 'service']
    span_ids: list[str] = Field(min_length=1, max_length=12)
    context_ids: list[str] = Field(default_factory=list, max_length=12)
    depends_on: list[str] = Field(default_factory=list, max_length=12,
        description='Current U span IDs of tasks that must finish first; never historical IDs.')


class OrderedPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tasks: list[PlannedTask] = Field(default_factory=list, max_length=6)
    clarification: str = Field(default='', max_length=500)
    unused_ids: list[str] = Field(default_factory=list, max_length=24)


def stable_task_id(task):
    # Domain is deliberately absent: a handoff retains the same task identity.
    value = [task.get('span_ids', []), task['question']]
    return 'T'+hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()[:12]


def compile_plan(plan, spans):
    """Reuse v5 provenance validation, discard its domain-based scheduling."""
    checked = validate_plan(plan, spans)
    indexed = {(t['domain'], tuple(t['span_ids'])): t for t in checked}
    tasks = [copy.deepcopy(indexed[(item.domain, tuple(item.span_ids))]) for item in plan.tasks]
    for task in tasks:
        task['id'] = stable_task_id(task)
    owners = {span: task['id'] for task in tasks for span in task['span_ids']}
    for task, item in zip(tasks, plan.tasks):
        if len(set(item.depends_on)) != len(item.depends_on) or not set(item.depends_on) <= owners.keys():
            raise ValueError('Dependencies must reference unique current task source spans')
        task['depends_on'] = list(dict.fromkeys(owners[s] for s in item.depends_on))
        if task['id'] in task['depends_on']:
            raise ValueError('A task cannot depend on itself')
    return order_tasks(tasks)


def order_tasks(tasks):
    """Stable topological order: independent tasks retain planner/source order."""
    pending, ordered, done = copy.deepcopy(tasks), [], set()
    ids = {t['id'] for t in pending}
    if len(ids) != len(pending):
        raise ValueError('Duplicate task identity')
    if any(not set(t.get('depends_on', [])) <= ids for t in pending):
        raise ValueError('Unknown task dependency')
    while pending:
        ready = next((t for t in pending if set(t.get('depends_on', [])) <= done), None)
        if ready is None:
            raise ValueError('Task dependencies contain a cycle')
        ordered.append(ready)
        done.add(ready['id'])
        pending.remove(ready)
    return ordered


def prior_results(task, results):
    """Only completed observations; these never authorize new writes."""
    return [{'task_id': ident, 'domain': result['domain'], 'status': result['status'],
             'answer': result['answer'], 'receipts': copy.deepcopy(result.get('receipts', []))}
            for ident, result in results.items() if ident != task['id']]


class ReassignmentRequested(ValueError):
    def __init__(self, reason, suggested_domain=None):
        super().__init__(reason)
        self.reason = str(reason)[:500]
        self.suggested_domain = suggested_domain


def task_control(state, task):
    return state.setdefault('task_control', {}).setdefault(task['id'], {
        'current_domain': task['domain'], 'attempted_domains': [], 'rejected_domains': [],
        'reassignments': 0, 'write_started': False, 'stalled': 0,
    })


def choose_reassignment(control, suggested=None):
    if control['write_started']:
        raise ValueError('A task with a started write must reconcile its original receipt')
    current = control['current_domain']
    if current not in control['rejected_domains']:
        control['rejected_domains'].append(current)
    candidates = [d for d in DOMAINS if d not in control['attempted_domains']]
    if control['reassignments'] >= 2 or not candidates:
        return None
    target = suggested if suggested in candidates else candidates[0]
    control['reassignments'] += 1
    control['current_domain'] = target
    control['stalled'] = 0
    return target


def merge_results(state, english=False, include_questions=True):
    results = list(state.get('results', {}).values())
    if not results:
        return {'answer': state.get('clarification') or '请补充具体问题或希望办理的事项。', 'status': 'clarify'}
    statuses = {r['status'] for r in results}
    completed = 'answered' in statuses
    unresolved = bool(statuses-{'answered'}) or bool(state.get('clarification'))
    if completed and unresolved:
        status = 'partial_success'
    elif 'error' in statuses:
        status = 'error'
    elif 'clarify' in statuses or state.get('clarification'):
        status = 'clarify'
    elif 'insufficient' in statuses:
        status = 'insufficient'
    elif 'blocked' in statuses:
        status = 'clarify'
    else:
        status = 'answered'
    labels = {'answered': '已完成', 'error': '未完成', 'blocked': '等待前序任务',
              'clarify': '待补充信息', 'insufficient': '资料或权限不足'}
    if english:
        labels = {'answered': 'Completed', 'error': 'Incomplete', 'blocked': 'Waiting for prerequisite',
                  'clarify': 'Needs clarification', 'insufficient': 'Insufficient evidence or permission'}
    if include_questions:
        answer = '\n\n'.join(labels.get(r['status'], r['status'])+' · '+r['question']+'\n'+r['answer'] for r in results)
    elif len(results) == 1:
        answer = results[0]['answer']
    else:
        answer = '\n\n'.join(labels.get(r['status'], r['status'])+'. '+r['answer'] for r in results)
    if state.get('clarification'):
        answer += '\n\n'+state['clarification']
    return {'answer': answer, 'status': status}


class ReadProgress:
    """Short-lived read reuse, scoped to a task and observable state version.

    Every attempted call is still counted/logged by the caller. Writes are never
    replayed from this cache. A write invalidates prior reads, including those
    whose external data version cannot be obtained reliably.
    """
    def __init__(self, state, ttl=60):
        self.state = state
        self.ttl = ttl
        self.state.setdefault('read_observations', {})
        self.state.setdefault('state_generation', 0)

    def key(self, task_id, name, arguments, version):
        data = [task_id, name, arguments, version, self.state['state_generation']]
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def get(self, task_id, name, arguments, version):
        entry = self.state['read_observations'].get(self.key(task_id, name, arguments, version))
        if entry and time.time()-entry['time'] < self.ttl:
            return copy.deepcopy(entry)
        return None

    def put(self, task_id, name, arguments, version, result):
        self.state['read_observations'][self.key(task_id, name, arguments, version)] = {
            'result': copy.deepcopy(result), 'time': time.time()}

    def invalidate(self):
        self.state['state_generation'] += 1
        self.state['read_observations'].clear()


def safe_failure(exc, write_unknown=False):
    if write_unknown:
        return '操作结果待核实；请核对原工单记录后继续，避免重复办理。'
    if isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in ('ReadTimeout', 'ConnectTimeout', 'ConnectError'):
        return '本步骤的服务暂时未响应，可恢复后继续；已完成步骤已保留。'
    if isinstance(exc, PermissionError):
        return '当前账号没有完成本步骤所需的权限。'
    if '上限' in str(exc) or 'budget' in str(exc).lower():
        return '本次执行预算已用尽，已完成步骤已保留。'
    return '本步骤执行失败，已保留完成结果与执行记录，可检查后继续。'
