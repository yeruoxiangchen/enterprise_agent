"""V6: ordered tasks, bounded handoffs, verified receipts and partial delivery.

V5 remains available as a comparator. Recovery reuses the original plan, task
IDs, completed results and budgets; a write timeout is reconciled against the
business database before an operation can be repeated.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from .business_multi_v2 import Workflow, ROLES, render_receipts
from .business_multi_v4 import response_schema, render_result
from .business_multi_v5 import BusinessQA as V5
from .business_v5_context import classify_span, ticket_fields, ticket_title, ticket_description
from .business_v6_control import (DOMAINS, ReassignmentRequested,
    choose_reassignment, merge_results, order_tasks, prior_results, safe_failure,
    stable_task_id, task_control)
from ..tickets import draft_ticket


class BusinessQA(V5):
    def blocked_answer(self):
        return '前序任务尚未完成，本步骤尚未执行；可完成前序任务后继续。'

    def reassignment_answer(self):
        return '专业领域仍不明确，请补充具体对象及希望执行的操作。'

    def failure_answer(self, exc, write_unknown):
        return safe_failure(exc, write_unknown)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        version = hashlib.sha256(Path(__file__).read_bytes()+
            Path(__file__).with_name('business_v6_control.py').read_bytes()).hexdigest()
        if self.state.get('v6_version', version) != version:
            raise ValueError('业务v6版本变化，请创建新请求')
        self.state['v6_version'] = version
        self.state.setdefault('task_failures', {})

    def route(self, state):
        result = super().route(state)
        tasks = result['tasks']
        # The Chinese finite router groups by domain. Restore source order;
        # explicit sequential requests additionally carry completion barriers.
        tasks.sort(key=lambda t: min((int(s[1:]) for s in t['span_ids']), default=0))
        sequential = bool(re.search(r'先[\s\S]+(?:然后|再|之后)', self.run['question']))
        for i, task in enumerate(tasks):
            task['id'] = stable_task_id(task)
            task['depends_on'] = [tasks[i-1]['id']] if sequential and i else []
        result['tasks'] = order_tasks(tasks)
        self.state['plan'] = copy.deepcopy(result['tasks'])
        self.save()
        self.emit('business_plan_v6', {'tasks': result['tasks']})
        return result

    def complete_task(self, task, state, status, answer):
        result = super().complete_task(task, state, status, answer)
        result['results'][task['id']]['receipts'] = copy.deepcopy([
            r for r in self.state['receipts'] if r.get('task_id') == task['id']])
        return result

    def worker(self, domain, state):
        task = state['tasks'][state['cursor']]
        self.cancelled()
        if domain != task['domain']:
            raise ValueError('领域路由与原始任务不一致')
        fingerprint = hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cached = self.state.setdefault('completed_tasks', {}).get(task['id'])
        if cached:
            if cached['task_sha256'] != fingerprint:
                raise ValueError('已完成任务与恢复计划不一致')
            self.emit('business_completed_reused', {'point': task['id'], 'domain': cached['result']['domain']})
            return {'results': {**state['results'], task['id']: cached['result']}, 'cursor': state['cursor']+1}
        if any(state['results'].get(dep, {}).get('status') != 'answered' for dep in task.get('depends_on', [])):
            return self.complete_task(task, state, 'blocked', self.blocked_answer())
        control = task_control(self.state, task)
        while True:
            current = control['current_domain']
            if current not in control['attempted_domains']:
                control['attempted_domains'].append(current)
            self.active_task = task
            self.save()  # attempts and the current domain survive interruption
            local = {**state, 'tasks': copy.deepcopy(state['tasks'])}
            local['tasks'][state['cursor']]['domain'] = current
            try:
                result = self.execute_task(current, local)
            except ReassignmentRequested as exc:
                self.cancelled()
                target = choose_reassignment(control, exc.suggested_domain)
                self.emit('business_reassignment', {'point': task['id'], 'from': current, 'to': target,
                    'reason': exc.reason, 'count': control['reassignments']})
                self.save()
                if target is None:
                    return self.complete_task(task, state, 'clarify', self.reassignment_answer())
                continue
            except Exception as exc:
                self.cancelled()  # cancellation is never converted to a partial success
                failure = {'kind': type(exc).__name__, 'reason': str(exc)[:300],
                           'write_unknown': control.get('write_unknown', False)}
                self.state.setdefault('task_failures', {})[task['id']] = failure
                self.emit('business_task_failed', {'point': task['id'], **failure})
                self.save()
                return self.complete_task(task, state, 'error', self.failure_answer(exc, failure['write_unknown']))
            completed = result['results'][task['id']]
            self.state['completed_tasks'][task['id']] = {'task_sha256': fingerprint, 'result': completed}
            self.state.setdefault('task_failures', {}).pop(task['id'], None)
            self.save()
            return result

    def execute_task(self, domain, state):
        task = state['tasks'][state['cursor']]
        if domain == 'service':
            return self.service_worker(task, state)
        point, evidence, warnings = task['id'], {}, []
        for i, query in enumerate(task.get('queries') or [task['question']]):
            if i >= 2:
                break
            found = self.search(domain, query[:500], point)
            warnings.extend(found['warnings'])
            evidence.update({r['source']: r for r in found['evidence']})
        self.state['task_sources'][point] = list(evidence)
        self.save()
        allowed = set(evidence)
        schema = response_schema(allowed)
        searches = model_calls = validation_errors = 0
        reassignment = None
        control = task_control(self.state, task)

        @tool
        def search_documents(query: str) -> dict:
            """仅为本任务缺少的资料补查一次；已取得的结果不必重复查询。"""
            nonlocal searches
            if searches >= 1:
                control['stalled'] += 1
                self.save()
                return {'evidence': [], 'warnings': ['补查已用完，请提交已有结论或说明缺失信息。']}
            searches += 1
            found = self.search(domain, query[:500], point)
            allowed.update(r['source'] for r in found['evidence'])
            return found

        @tool
        def request_reassignment(reason: str, suggested_domain: str = '') -> dict:
            """首次执行决定中发现领域不符时请求转派；缺证据、权限拒绝或工具错误应直接说明。"""
            nonlocal reassignment
            self.use_tool()
            if model_calls != 1 or searches or control['write_started']:
                raise ValueError('转派只允许在首次执行决定且业务操作开始前提出')
            reassignment = ReassignmentRequested(reason, suggested_domain)
            return {'decision': 'reject_domain', 'reason': reason, 'suggested_domain': suggested_domain}

        def feedback(exc):
            nonlocal validation_errors
            validation_errors += 1
            if validation_errors > 1:
                raise exc
            return '请按当前已读来源修正结构化结果：'+str(exc)[:1000]

        @wrap_model_call
        def meter(request, handler):
            nonlocal model_calls
            if reassignment is not None:
                raise reassignment
            finalizing = self.state['calls'] >= 10 or model_calls >= 3 or control['stalled'] >= 2
            if finalizing:
                request = request.override(tools=[], tool_choice='required')
            self.reserve(([request.system_message] if request.system_message else [])+list(request.messages),
                         list(request.tools)+[schema])
            model_calls += 1
            response = handler(request)
            for message in response.result:
                calls = getattr(message, 'tool_calls', [])
                if any(c['name'] == 'request_reassignment' for c in calls) and len(calls) != 1:
                    raise ValueError('转派决定必须单独返回，不能同时执行业务工具')
            self.cancelled()
            return response

        payload = {'task': task['question'], 'user_context': task.get('context', ''),
                   'evidence': list(evidence.values()), 'warnings': warnings,
                   'completed_tasks': prior_results(task, state['results'])}
        prompt = (f'你是{ROLES[domain]}专业Agent。只回答本轮task，事实必须由本轮evidence支持。'
            '使用KnowledgeResult给出事实、来源和未解决部分；来源不足时保留已有事实。'
            '历史与completed_tasks仅用于理解指代和前序进度，不能当作新的知识来源或业务写入授权。'
            '证据足够即提交结果，最多补查一次，不重复查询。'
            '确属其他专业领域时，可在首次执行决定中单独请求request_reassignment；'
            '资料不足、用户条件不足或工具失败分别说明，不以转派规避。'
            '不得声称已执行任何业务操作。\n'+self.skills[domain])
        self.emit('business_input', {'point': point, 'domain': domain, 'payload': payload})
        agent = create_agent(self.model, tools=[search_documents, request_reassignment], name=domain,
            system_prompt=prompt, middleware=[meter], response_format=ToolStrategy(schema, handle_errors=feedback))
        output = agent.invoke({'messages': list(self.history)+[
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]}, config={'recursion_limit': 12})
        result = output.get('structured_response')
        if not isinstance(result, schema):
            raise ValueError('专业Agent未返回有效结构化结果')
        status, answer = render_result(result)
        self.state.setdefault('specialist_results', {})[point] = result.model_dump()
        self.save()
        return self.complete_task(task, state, status, answer)

    def draft_receipt(self, task, ticket, fields):
        return {'action': 'draft', 'ticket_id': ticket['id'], 'title': ticket['title'],
                'status': ticket['status'], 'source_run': self.run['id'], 'task_id': task['id'],
                'description_sha256': hashlib.sha256(ticket['description'].encode()).hexdigest(),
                'fields': fields, 'verified': True}

    def reconcile_draft(self, task, fields):
        ticket = self.db.one('SELECT * FROM service_tickets WHERE source_run=? AND user_id=?',
                             (self.run['id'], self.user['id']))
        if ticket is None:
            return None
        if (ticket['status'] != 'draft' or ticket['title'] != ticket_title(fields)
                or ticket['description'] != ticket_description(fields)):
            raise ValueError('已有工单与本次原始请求不一致，需要核对')
        return self.draft_receipt(task, ticket, fields)

    def service_worker(self, task, state):
        actions = task.get('actions', [])
        if not actions:
            hint = classify_span(task['question'])
            raise ReassignmentRequested('当前任务没有授权的服务办理动作', hint if hint in DOMAINS else None)
        if set(actions)-{'ticket_create', 'ticket_list'}:
            raise ValueError('未知服务动作')
        fields = ticket_fields(self.run['question'], self.history)
        if 'ticket_create' in actions and fields is None:
            self.state['pending_service'] = 'ticket_create'
            self.save()
            return self.complete_task(task, state, 'clarify', '请先描述报障问题和已知环境，补充后可生成待确认草稿。')
        if 'ticket_list' in actions and re.search(r'其他|所有人|别人|他人', task['question']):
            return self.complete_task(task, state, 'insufficient', '仅支持查询当前账号本人的工单。')
        control = task_control(self.state, task)
        for action in actions:
            self.use_tool()
            if action == 'ticket_create':
                # Authorization still derives exclusively from original user
                # spans/fields. A handoff or another specialist's answer cannot
                # introduce ticket_create into task.actions.
                receipt = self.reconcile_draft(task, fields)
                if receipt is None:
                    control['write_started'] = True
                    self.save()
                    try:
                        draft_ticket(self.db, self.user, self.run['id'], ticket_title(fields), ticket_description(fields))
                    except Exception:
                        self.cancelled()
                        try:
                            receipt = self.reconcile_draft(task, fields)
                        except Exception:
                            control['write_unknown'] = True
                            self.save()
                            raise
                        if receipt is None:
                            raise
                    else:
                        receipt = self.reconcile_draft(task, fields)
                if receipt is None:
                    raise ValueError('服务任务缺少可核实的工单记录')
                control['write_unknown'] = False
                self.state['receipts'] = [r for r in self.state['receipts']
                    if not (r['action'] == 'draft' and r['ticket_id'] == receipt['ticket_id'])]+[receipt]
                self.emit('ticket_draft', receipt)
            else:
                tickets = self.db.all('SELECT id,title,status,note FROM service_tickets WHERE user_id=? ORDER BY updated_at DESC LIMIT 10',
                                      (self.user['id'],))
                self.state['receipts'] = [r for r in self.state['receipts'] if r['action'] != 'list']+[
                    {'action': 'list', 'tickets': tickets, 'task_id': task['id'], 'verified': True}]
            self.save()
        return self.complete_task(task, state, 'answered', render_receipts([
            r for r in self.state['receipts'] if r.get('task_id') == task['id']]))

    def merge(self, state):
        return merge_results(state)

    def answer(self):
        graph = StateGraph(Workflow)
        graph.add_node('route', self.route)
        for domain in DOMAINS:
            graph.add_node(domain, lambda state, d=domain: self.worker(d, state))
            graph.add_conditional_edges(domain, self.next_node)
        graph.add_node('merge', self.merge)
        graph.add_edge(START, 'route')
        graph.add_conditional_edges('route', self.next_node)
        graph.add_edge('merge', END)
        with SqliteSaver.from_conn_string(str(self.cfg.root/'business_checkpoints.sqlite3')) as saver:
            app = graph.compile(checkpointer=saver)
            config = {'configurable': {'thread_id': self.run['id']}, 'recursion_limit': 24}
            saved = app.get_state(config)
            if self.state.get('resume_requested') and saved.values:
                app.update_state(config, {'cursor': 0, 'results': {}, 'answer': '', 'status': ''}, as_node='route')
                self.state['resume_requested'] = False
                self.save()
                saved = app.get_state(config)
            result = saved.values if saved.values and not saved.next else app.invoke(
                None if saved.values else {}, config, durability='sync')
        self.cancelled()
        if not result.get('answer') or result.get('status') not in ('answered', 'clarify', 'insufficient', 'partial_success', 'error'):
            raise ValueError('检查点未形成有效的分项结果')
        for row in self.state['sources'].values():
            current = self.retriever.chunk(self.user, row['id'], self.allowed)
            if current['text'] != row['text']:
                raise ValueError('回答期间证据已变化，请重新提问')
        return result
