"""V5: independent consultation intent, current user facts and durable task commits."""
import hashlib
import json
from pathlib import Path
import re

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from .business_multi_v4 import BusinessQA as V4
from .business_multi_v2 import Workflow, render_receipts
from .business_v3_routing import (source_spans, compile_tasks, relevant_history, CREATE, NEGATIVE_CREATE, SWITCH, TECH, POLICY)
from .business_v5_context import classify_span, ticket_fields, ticket_title, ticket_description
from ..tickets import draft_ticket

DOMAINS = ('technical', 'policy', 'service')


class BusinessQA(V4):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        version = hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('business_v5_context.py').read_bytes()).hexdigest()
        if self.state.get('v5_version', version) != version:
            raise ValueError('业务v5版本变化，请创建新请求')
        self.state['v5_version'] = version
        self.state.setdefault('completed_tasks', {})

    def route(self, state):
        spans = source_spans(self.run['question'])
        if len(spans) > 12:
            raise ValueError('单次请求超过12个片段，请拆分问题')
        labels = [classify_span(s['text']) for s in spans]
        background = '\n'.join(s['text'] for s, label in zip(spans, labels) if label == 'background')
        domain = 'policy' if POLICY.search(background) else 'technical' if TECH.search(background) else None
        if domain:
            labels = [domain if label is None and re.search(r'怎么|如何|排查|检查', spans[i]['text']) else label
                      for i, label in enumerate(labels)]
        pending = self.pending_creation and not SWITCH.search(self.run['question']) and not re.search(r'取消|不用|不要|先别', self.run['question'])
        if pending and all(label in ('background', None, 'clarify') for label in labels):
            labels[-1] = 'ticket_create'
        history = relevant_history(self.history, self.run['question'])
        prior = next((classify_span(m['content']) for m in reversed(history)
                      if m['role'] == 'user' and classify_span(m['content']) in ('technical', 'policy')), None)
        if prior:
            labels = [prior if label in (None, 'clarify') else label for label in labels]
        missing = [i for i, label in enumerate(labels) if label is None]
        if missing:
            for i, label in zip(missing, self.classify_missing(spans, missing)):
                labels[i] = label
        for i, label in enumerate(labels):
            if label == 'ticket_create' and ((not CREATE.search(self.run['question']) and not pending) or NEGATIVE_CREATE.search(self.run['question'])):
                labels[i] = 'clarify'
        tasks, unknown, spans = compile_tasks(self.run['question'], self.history, labels)
        clarification = '请补充具体问题、对象或希望办理的事项。' if unknown or not tasks else ''
        self.emit('business_route', {'tasks': tasks, 'spans': spans, 'labels': labels, 'clarification': clarification,
                                    'routing': 'v5_independent_read_and_write_intents', 'model_classified_spans': missing})
        return {'tasks': tasks, 'clarification': clarification, 'results': {}, 'cursor': 0}

    def worker(self, domain, state):
        task = state['tasks'][state['cursor']]
        fingerprint = hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cached = self.state['completed_tasks'].get(task['id'])
        self.cancelled()
        if cached:
            if cached['task_sha256'] != fingerprint:
                raise ValueError('已完成任务与恢复计划不一致')
            self.emit('business_completed_reused', {'point': task['id'], 'domain': domain})
            return {'results': {**state['results'], task['id']: cached['result']}, 'cursor': state['cursor']+1}
        result = self.execute_task(domain, state)
        self.state['completed_tasks'][task['id']] = {'task_sha256': fingerprint, 'result': result['results'][task['id']]}
        self.save()
        return result

    def execute_task(self, domain, state):
        """Domain execution hook; the v5 cache/checkpoint wrapper stays shared."""
        return super().worker(domain, state)

    def service_worker(self, task, state):
        actions = task['actions']
        fields = ticket_fields(self.run['question'], self.history)
        if 'ticket_create' in actions and fields is None:
            self.state['pending_service'] = 'ticket_create'
            self.save()
            return self.complete_task(task, state, 'clarify', '请先描述要报障的问题和已知环境；补充后可生成待确认工单草稿。')
        if 'ticket_list' in actions and re.search(r'其他|所有人|别人|他人', task['question']):
            self.emit('service_refusal', {'reason': 'other_account', 'scope': 'current_user_only'})
            return self.complete_task(task, state, 'insufficient', '不能查询其他账号的工单；仅支持当前账号本人记录。')

        @tool
        def prepare_ticket() -> dict:
            """从用户原话生成待确认草稿，不提交，不接受模型编写的故障信息。"""
            self.use_tool()
            if 'ticket_create' not in actions or fields is None:
                raise ValueError('当前请求未授权创建草稿')
            ticket = draft_ticket(self.db, self.user, self.run['id'], ticket_title(fields), ticket_description(fields))
            receipt = {'action': 'draft', 'ticket_id': ticket['id'], 'title': ticket['title'], 'status': ticket['status'], 'fields': fields}
            if receipt not in self.state['receipts']:
                self.state['receipts'].append(receipt)
            self.save()
            self.emit('ticket_draft', receipt)
            return receipt

        @tool
        def list_my_tickets() -> list[dict]:
            """查询当前账号本人工单，不支持指定其他用户。"""
            self.use_tool()
            if 'ticket_list' not in actions:
                raise ValueError('当前请求未要求查询工单')
            tickets = self.db.all('SELECT id,title,status,note FROM service_tickets WHERE user_id=? ORDER BY updated_at DESC LIMIT 10', (self.user['id'],))
            self.state['receipts'] = [r for r in self.state['receipts'] if r['action'] != 'list']+[{'action': 'list', 'tickets': tickets}]
            self.save()
            self.emit('ticket_list', {'scope': 'current_user', 'count': len(tickets)})
            return tickets

        def satisfied():
            return all(any(r['action'] == ('draft' if a == 'ticket_create' else 'list') for r in self.state['receipts']) for a in actions)

        @wrap_model_call
        def meter(request, handler):
            self.reserve(request.messages, request.tools)
            if not satisfied():
                request = request.override(tool_choice='required')
            response = handler(request)
            self.cancelled()
            return response

        tools = ([prepare_ticket] if 'ticket_create' in actions else [])+([list_my_tickets] if 'ticket_list' in actions else [])
        agent = create_agent(self.model, tools=tools, middleware=[meter], name='service',
                             system_prompt='按当前用户明确意图调用限定工具。取得实际回执后停止调用，草稿需用户本人确认。'+self.skills['service'])
        self.emit('business_input', {'domain': 'service', 'point': task['id'], 'payload': {'task': task['question'], 'user_fields': fields}})
        agent.invoke({'messages': [{'role': 'user', 'content': json.dumps({'task': task['question'], 'actions': actions}, ensure_ascii=False)}]},
                     config={'recursion_limit': 8})
        if not satisfied():
            raise ValueError('服务任务缺少真实工具回执')
        for receipt in self.state['receipts']:
            if receipt['action'] == 'draft':
                actual = self.db.one('SELECT * FROM service_tickets WHERE id=? AND user_id=? AND source_run=?',
                                     (receipt['ticket_id'], self.user['id'], self.run['id']))
                if not actual or actual['status'] != 'draft' or actual['description'] != ticket_description(fields) or actual['title'] != ticket_title(fields):
                    raise ValueError('工单回执与真实记录不一致')
        return self.complete_task(task, state, 'answered', render_receipts(self.state['receipts']))

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
            complete = (saved.values and not saved.next and isinstance(saved.values.get('answer'), str)
                        and saved.values.get('status') in ('answered', 'clarify', 'insufficient'))
            # No pending node alone does not establish a final answer: pending
            # writes can have updated a partial snapshot after an abrupt exit.
            result = saved.values if complete else app.invoke(None if saved.values else {}, config, durability='sync')
        if not isinstance(result.get('answer'), str) or result.get('status') not in ('answered', 'clarify', 'insufficient'):
            raise ValueError('检查点未形成完整业务结果，保留状态供检查')
        self.cancelled()
        for row in self.state['sources'].values():
            current = self.retriever.chunk(self.user, row['id'], self.allowed)
            if current['text'] != row['text']:
                raise ValueError('回答期间证据已变化，请重新提问')
        return result
