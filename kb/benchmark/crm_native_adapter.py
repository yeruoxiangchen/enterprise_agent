"""Benchmark bridge for the official DeepAgents assembly and shared CRM gateway."""
import asyncio
import json
import threading
import time
from pathlib import Path

from langchain_openai import ChatOpenAI
from kb.context import ContextBudget
from kb.harness.native import NativeHarness, NativeApprovalRequired
from kb.harness.ports import CRMPort


class NativeCRMAdapter:
    def __init__(self, environment, settings, config, policy, output, seed=42, model=None, shared_context=False,
                 completion_review=False, require_dependency_confirmation=False,
                 fork_subagents=False, operation_permissions=False, prepare_operations=False,
                 specialist_owned_checks=False):
        self.environment, self.settings = environment, settings
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.turn = 0
        self.stop = threading.Event()
        self.turn_times, self.tool_calls, self.execution_outcomes = [], [], []
        self.port = CRMPort(environment, config, policy)
        routes = ()
        if operation_permissions:
            from kb.harness.tool_routes import operation_permission_route
            routes = (operation_permission_route(self.port),)
        self.pending_review = None
        interrupt_on = None
        if require_dependency_confirmation or prepare_operations:
            def needs_confirmation(request):
                if self.engine.has_reusable_user_approval(request.tool_call['name'],request.tool_call['args'],
                                                         request.tool_call['id']):
                    return False
                ticket=request.tool_call['args'].get('ticket_id')
                proof=self.port._proof('check_dependencies',{'ticket_id':ticket})or{}
                return bool(proof.get('open_dependencies'))
            interrupt_on={'close_ticket':{'allowed_decisions':['approve','reject'],'when':needs_confirmation}}
        model = model or ChatOpenAI(model=settings.llm_model, base_url=settings.llm_url,
            api_key=settings.llm_key or 'local', temperature=0, max_tokens=settings.output_tokens,
            timeout=settings.timeout, max_retries=0,
            extra_body={'chat_template_kwargs': {'enable_thinking': False}, 'seed': seed})
        self.engine = NativeHarness(model=model, port=self.port, workspace=self.output, namespace='conversation',
            context_budget=ContextBudget(settings), max_calls=settings.harness_model_calls,
            max_tools=settings.harness_tool_calls, cancelled=self.check_cancel, shared_context=shared_context,
            completion_review=completion_review,interrupt_on=interrupt_on,
            fork_subagents=fork_subagents,tool_routes=routes,
            supervisor_read_tools={'get_ticket','list_tickets','get_business_hours'}if specialist_owned_checks else None)
        if prepare_operations:
            from kb.harness.crm_preparation import prepare
            self.engine.prepare_tools = lambda call, available: prepare(self, call, available)

    def check_cancel(self):
        if self.stop.is_set():
            raise RuntimeError('Evaluation cancelled')

    def __getattr__(self, name):
        attributes = {'model_calls': 'calls', 'prompt_tokens': 'prompt_tokens',
            'completion_tokens': 'completion_tokens', 'usage_missing_calls': 'missing_usage', 'llm_seconds': 'llm_seconds'}
        if name in attributes:
            return getattr(self.engine, attributes[name])
        raise AttributeError(name)

    def emit(self, kind, value):
        self.engine.emit(kind, value)

    def write_environment(self):
        (self.output / 'environment.json').write_text(json.dumps(self.environment.get_state(), ensure_ascii=False))

    def pending_message(self):
        requests=[action for payload in self.pending_review['payloads']
                  for action in payload.get('action_requests',[])]
        ticket=requests[0]['args'].get('ticket_id')if requests else None
        proof=self.port._proof('check_dependencies',{'ticket_id':ticket})or{}
        dependencies=', '.join(item['ticket_id']for item in proof.get('open_dependencies',[]))
        return (f'Ticket {ticket} has open dependencies: {dependencies}. '
                'Please confirm whether to close it despite those dependencies.')

    def _invoke(self,user_message,approval):
        def resume():
            pending=self.pending_review
            requests=[request for payload in pending['payloads']for request in payload['action_requests']]
            if (len(requests)!=1 or requests[0]['name']!=approval['name']or
                    requests[0]['args']!=approval['args']or approval['decision']not in ('approve','reject')):
                raise ValueError('User review does not match the pending operation')
            try:
                answer=self.engine.invoke(pending['question'],pending['request_id'],
                    decisions=[{'type':approval['decision']}],review_message=user_message)
            except NativeApprovalRequired as next_pending:
                self.pending_review={**pending,'payloads':next_pending.payloads}
                self.emit('pending_tool_review',self.pending_review)
                return self.pending_message()
            self.pending_review=None
            return answer
        if self.pending_review:
            if approval is None:
                return 'The requested operation is paused. Please approve or reject that operation to continue.'
            return resume()
        question,request_id=user_message,str(self.turn)
        try:
            return self.engine.invoke(question,request_id)
        except NativeApprovalRequired as pending:
            self.pending_review={'question':question,'request_id':request_id,'payloads':pending.payloads}
            self.emit('pending_tool_review',self.pending_review)
            if approval is not None:
                return resume()
            return self.pending_message()

    async def run(self, user_message, history, *, approval=None):
        from harness.models import AgentResponse
        started = time.monotonic()
        before, calls = len(self.engine.attempts), self.model_calls
        previous={a['id']:dict(a)for a in self.engine.attempts}
        job = asyncio.create_task(asyncio.to_thread(self._invoke, user_message, approval))
        try:
            answer = await asyncio.shield(job)
            self.execution_outcomes.append({'turn': self.turn,
                'status': 'needs_user_review'if self.pending_review else'graph_returned',
                'review_status': self.engine.last_review_status})
            changed=[a for a in self.engine.attempts if previous.get(a['id'])!=a]
            return AgentResponse(content=answer, tool_calls_made=changed, iterations=self.model_calls-calls)
        except asyncio.CancelledError:
            self.stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(job), self.settings.timeout+10)
            except (Exception, asyncio.CancelledError):
                pass
            raise
        finally:
            for attempt in self.engine.attempts:
                existing=next((row for row in self.tool_calls if row['id']==attempt['id']),None)
                if existing is None:
                    self.tool_calls.append({**attempt, 'turn': self.turn,
                        'executed': attempt['backend_executed'],
                        'execution_index': attempt.get('execution_index',len(self.tool_calls))})
                elif existing.get('effect_id')!=attempt.get('effect_id')or existing['ok']!=attempt['ok']:
                    existing.update(attempt,turn=self.turn,executed=attempt['backend_executed'])
            self.write_environment()
            self.turn_times.append(time.monotonic()-started)
            self.turn += 1

    async def aclose(self):
        self.engine.close()
