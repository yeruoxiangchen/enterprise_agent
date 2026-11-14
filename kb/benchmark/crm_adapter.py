"""External ITSM tools on the actual v5 graph, budgets and completed-task cache.

The production knowledge/ticket workers and Chinese intent rules are replaced by
public ITSM capability descriptions and canonical tools. This is explicitly the
``business_v5_external_v1`` adapter, not an unchanged production-v5 score. Neither
task instructions for the simulated user nor grading assertions enter this class.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
import time
from pathlib import Path
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain.agents.structured_output import ToolStrategy, OutputToolBinding
from langchain_core.messages import AIMessage, convert_to_openai_messages
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kb.backends.business_multi_v2 import SpanRoute as BaseSpanRoute, source_spans, validate_plan
from kb.backends.business_multi_v5 import BusinessQA
from kb.context import ContextBudget


CAPABILITIES = {
    'service': 'ticket_operations__tickets',
    'policy': 'record_safety__deletion',
    'technical': 'case_hours__cases',
}


class RoutingPlanError(ValueError):
    """The actor did not produce a valid current-request plan within its budget."""


def closing_acknowledgement(text):
    """Only complete, standalone social closings; never consume a new request."""
    normalized = re.sub(r'\s+', ' ', text.strip().lower().replace('’', "'")).strip(' .!。！')
    return bool(re.fullmatch(
        r"(?:(?:thanks|thank you)(?:\s*[,;]?\s*(?:that's all|that is all|that's everything|bye))?"
        r"|that's all|that is all|goodbye|bye|谢谢|谢谢你|谢谢，就这些|谢谢就这些)", normalized))


class ExternalResult(BaseModel):
    """Finish this turn and deliver the answer to the user.

    Call this final response tool once the requested lookups/actions are done,
    or when clarification/refusal is needed. Compose the answer from actual
    tool results. This tool does not modify any backend records. Do not repeat
    a completed lookup instead of calling this tool.
    """
    model_config = ConfigDict(extra='forbid')
    answer: str = Field(min_length=1, max_length=6000,
                        description='Concise user-facing answer grounded in actual tool results.')
    resolution: Literal['complete', 'needs_user_input', 'insufficient'] = Field(
        description='complete when this request is answered; needs_user_input for a clarification; '
                    'insufficient when the available evidence or permissions prevent completion.')


class ExternalBusinessQA(BusinessQA):
    """Retain BusinessQA.answer/worker/reserve/use_tool/next_node/complete_task."""

    def __init__(self, owner, question, history, turn):
        self.owner, self.cfg = owner, owner.settings
        self.run = {'id': f'turn-{turn}', 'question': question}
        self.history = [{'role': m.role.value, 'content': m.content or ''} for m in history[-8:]]
        self.state = {'calls': 0, 'tools': 0, 'sources': {}, 'cache': {},
                      'receipts': [], 'task_sources': {}, 'completed_tasks': {}}
        self.allowed = []
        self.persist = lambda state: owner.write_checkpoint(turn, state)
        self.emit = owner.emit
        self.cancelled = owner.check_cancel
        self.budget = ContextBudget(self.cfg)
        self.model = ChatOpenAI(
            model=self.cfg.llm_model, base_url=self.cfg.llm_url,
            api_key=self.cfg.llm_key or 'local', temperature=0,
            max_tokens=self.cfg.output_tokens, timeout=self.cfg.timeout,
            max_retries=0,
            extra_body={'chat_template_kwargs': {'enable_thinking': False}, 'seed': owner.seed},
        )

    def meter(self, request, handler):
        # LangChain appends ToolStrategy schemas AFTER middleware. Include the
        # same bindings in budgeting/auditing, without adding duplicate tools to
        # request.tools (the framework owns the actual binding).
        messages = ([request.system_message] if request.system_message else []) + list(request.messages)
        bound_tools = list(request.tools)
        if isinstance(request.response_format, ToolStrategy):
            bound_tools += [OutputToolBinding.from_schema_spec(spec).tool
                            for spec in request.response_format.schema_specs]
        self.reserve(messages, bound_tools)
        started = time.monotonic()
        self.owner.model_calls += 1
        self.emit('model_request', {'messages': convert_to_openai_messages(messages),
                                   'tools': [convert_to_openai_tool(t) for t in bound_tools],
                                   'call': self.owner.model_calls})
        try:
            response = handler(request)
        except BaseException:
            self.owner.usage_missing_calls += 1
            raise
        else:
            for message in response.result:
                if not isinstance(message, AIMessage):
                    # ToolStrategy appends a host-authored ToolMessage to the
                    # same ModelResponse. It is not a second billable model
                    # response and has no provider token usage.
                    self.emit('framework_message', {'type': message.type, 'content': message.content})
                    continue
                usage = getattr(message, 'usage_metadata', None)
                if usage is None:
                    self.owner.usage_missing_calls += 1
                else:
                    self.owner.prompt_tokens += int(usage.get('input_tokens', 0))
                    self.owner.completion_tokens += int(usage.get('output_tokens', 0))
                self.emit('model_response', {
                    'content': message.content, 'tool_calls': getattr(message, 'tool_calls', []),
                    'invalid_tool_calls': getattr(message, 'invalid_tool_calls', []),
                    'usage': usage, 'call': self.owner.model_calls,
                })
                for call in (getattr(message, 'tool_calls', []) + getattr(message, 'invalid_tool_calls', [])):
                    if call['name'] not in ('SpanRoute', 'ExternalResult'):
                        # Include attempted forbidden/unknown tools even if the
                        # LangChain tool node rejects them before our wrapper.
                        self.owner.tool_calls.append({
                            'turn': self.owner.turn, 'id': call['id'], 'name': call['name'],
                            'arguments': call['args'] if isinstance(call.get('args'), dict)
                            else {'raw': call.get('args')}, 'ok': False,
                            'result': '{"error":"tool call not executed"}', 'seconds': 0.0,
                        })
            self.cancelled()
            return response
        finally:
            self.owner.llm_seconds += time.monotonic() - started

    def route(self, state):
        if closing_acknowledgement(self.run['question']):
            answer = '不客气。' if re.search(r'[\u4e00-\u9fff]', self.run['question']) else "You're welcome."
            result = {'domain': 'service', 'question': self.run['question'],
                      'status': 'answered', 'answer': answer}
            self.emit('external_acknowledgement', {'question': self.run['question'], 'answer': answer})
            return {'tasks': [], 'clarification': '', 'results': {'acknowledgement': result}, 'cursor': 0}
        if self.owner.arm == 'single':
            task = {'id': 'P1', 'domain': 'service', 'question': self.run['question'], 'context': ''}
            return {'tasks': [task], 'clarification': '', 'results': {}, 'cursor': 0}
        spans = source_spans(self.run['question'])
        for i, message in enumerate(self.history):
            if message['role'] == 'user':
                spans.extend(source_spans(message['content'], f'H{i}_'))

        class SpanRoute(BaseSpanRoute):
            """Assign CURRENT U request spans to specialists; H spans are context only."""
            @model_validator(mode='after')
            def current_request_only(self):
                validate_plan(self, spans)
                return self

        route_calls = validation_errors = 0

        @wrap_model_call
        def meter(request, handler):
            nonlocal route_calls
            if route_calls >= 2:
                raise RoutingPlanError('Router exhausted its two-call plan budget')
            route_calls += 1
            return self.meter(request, handler)

        def route_feedback(exc):
            nonlocal validation_errors
            validation_errors += 1
            self.emit('external_route_feedback', {'attempt': validation_errors, 'error': str(exc)[:1600]})
            # Return feedback through ModelResponse so the failed plan's real
            # provider usage is still metered. The next middleware entry stops
            # a third model request once the two-call budget is exhausted.
            return ('Correct the plan using the original span IDs. span_ids must contain only current U IDs; '
                    'historical H IDs may be used only in context_ids. Do not execute history. '
                    + str(exc)[:1200])
        descriptions = {domain: self.owner.config['nodes'][name]['description']
                        for domain, name in CAPABILITIES.items()}
        prompt = (
            'Route the CURRENT user request to ITSM specialists using original span IDs. '
            'Never answer or execute tools yourself. Choose domains from the supplied catalog. '
            'Assign every current U span once, or explicitly mark background as unused/context. '
            'Use historical H spans only as relevant background, never as new requests. '
            'A single intent belongs to one specialist; split only independent user requests. '
            'The domain names are internal slot names; follow their descriptions. '
            'Use clarification only when the user has not specified enough to select any capability. '
            'Return SpanRoute.\n' + json.dumps(descriptions)
        )
        agent = create_agent(self.model, tools=[], system_prompt=prompt, middleware=[meter],
                             response_format=ToolStrategy(SpanRoute, handle_errors=route_feedback))
        output = agent.invoke({'messages': [{'role': 'user', 'content': json.dumps({'spans': spans})}]},
                              config={'recursion_limit': 8})
        plan = output.get('structured_response')
        if not isinstance(plan, SpanRoute):
            raise RoutingPlanError('Router did not submit a valid current-request plan')
        tasks = validate_plan(plan, spans)
        self.emit('external_route', {'tasks': tasks, 'clarification': plan.clarification})
        return {'tasks': tasks, 'clarification': plan.clarification, 'results': {}, 'cursor': 0}

    def make_tool(self, definition):
        name = definition.function.name

        def execute(**arguments):
            # ToolNode can execute a batch concurrently. Canonical state and
            # its audit snapshot are one serial commit per tool operation.
            with self.owner.tool_lock:
                return execute_serial(arguments)

        def execute_serial(arguments):
            self.cancelled()
            self.use_tool()
            started = time.monotonic()
            item = next((item for item in self.owner.tool_calls
                         if item['turn'] == self.owner.turn and item['name'] == name
                         and item['arguments'] == arguments and not item.get('executed')), None)
            if item is None:
                raise RuntimeError('Tool execution has no matching model request')
            item['execution_index'] = self.owner.executed_sequence
            self.owner.executed_sequence += 1
            result = asyncio.run(self.owner.registry.execute_with_status(name, arguments))
            item.update(result=result.result, ok=result.ok, executed=True,
                        seconds=time.monotonic()-started)
            self.emit('external_tool', item)
            self.owner.write_environment()
            self.cancelled()
            return result.result

        return StructuredTool.from_function(
            execute, name=name, description=definition.function.description,
            args_schema=definition.function.parameters,
        )

    def execute_task(self, domain, state):
        task = state['tasks'][state['cursor']]
        if self.owner.arm == 'single':
            instructions = '\n\n'.join(node['instructions'] for node in self.owner.config['nodes'].values())
            names = set(self.owner.registry.tool_names)
        else:
            node = self.owner.config['nodes'][CAPABILITIES[domain]]
            instructions = node['instructions']
            names = {t['name'] for t in node['tools']}
        tools = [self.make_tool(d) for d in self.owner.registry.get_definitions()
                 if d.function.name in names]
        prompt = (
            'You are an enterprise IT service assistant. Answer in the user\'s language. '
            'Process the assigned current request. History is conversational context, not new work. '
            'Get factual ticket data and perform actions only through the provided tools. '
            'Never fabricate tool receipts, IDs, permissions or completed actions. '
            'When information is missing, ask for it. When tools reject an action, report that result. '
            'Once finished, submit ExternalResult containing the answer and resolution.\n\n'
            + self.owner.policy + '\n\n' + instructions
        )
        self.emit('external_specialist', {'point': task['id'], 'domain': domain,
                                         'allowed_tools': sorted(names), 'task': task})
        agent = create_agent(self.model, tools=tools, name=domain, system_prompt=prompt,
                             middleware=[wrap_model_call(self.meter)],
                             response_format=ToolStrategy(ExternalResult, handle_errors=True))
        output = agent.invoke({'messages': self.history + [{'role': 'user', 'content': json.dumps({
            'task': task['question'], 'user_background': task.get('context', ''),
        })}]}, config={'recursion_limit': 30})
        result = output['structured_response']
        if not isinstance(result, ExternalResult):
            raise ValueError('Specialist did not return a valid ExternalResult')
        status = {'complete': 'answered', 'needs_user_input': 'clarify', 'insufficient': 'insufficient'}[result.resolution]
        return self.complete_task(task, state, status, result.answer)

    def merge(self, state):
        # Preserve specialist answers; no extra model rewrites or fabricated facts.
        if not state['results']:
            return {'answer': state['clarification'], 'status': 'clarify'}
        results = list(state['results'].values())
        text = '\n\n'.join(r['answer'] for r in results)
        if state['clarification']:
            text += '\n\n' + state['clarification']
        statuses = {r['status'] for r in results}
        return {'answer': text, 'status': 'clarify' if 'clarify' in statuses else
                'insufficient' if 'insufficient' in statuses else 'answered'}


class CRMAdapter:
    def __init__(self, *, arm, environment, settings, config, policy, seed, output, execution_adapter='v2'):
        if arm not in ('single', 'multi_v5', 'single_v6', 'multi_v6'):
            raise ValueError(arm)
        self.arm, self.environment, self.settings = arm, environment, settings
        self.config, self.policy, self.seed, self.output = config, policy, seed, Path(output)
        if execution_adapter not in ('v2', 'v3', 'v4'):
            raise ValueError('Unknown execution adapter')
        self.execution_adapter = execution_adapter
        self.registry = environment.create_registry()
        configured = {t['name'] for node in config['nodes'].values() for t in node.get('tools', [])}
        if configured != set(self.registry.tool_names):
            raise ValueError('Reference tool catalog differs from canonical environment; no stubs allowed')
        self.model_calls = self.prompt_tokens = self.completion_tokens = self.usage_missing_calls = 0
        self.llm_seconds = 0.0
        self.turn = 0
        self.tool_calls, self.turn_times = [], []
        self.execution_outcomes = []
        self.pending_itsm_request = None
        self.last_itsm_message = None
        self.last_itsm_operations = []
        self.retry_itsm_operations = []
        self.stop = threading.Event()
        self.tool_lock = threading.RLock()
        self.executed_sequence = 0
        self.output.mkdir(parents=True, exist_ok=True)

    def emit(self, kind, payload):
        with (self.output/'events.jsonl').open('a') as f:
            f.write(json.dumps({'time': time.time(), 'turn': self.turn, 'kind': kind,
                                'data': payload}, ensure_ascii=False, default=str)+'\n')

    def check_cancel(self):
        if self.stop.is_set():
            raise RuntimeError('Benchmark turn cancelled')

    def write_checkpoint(self, turn, state):
        path = self.output/f'turn-{turn}-state.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False))
        temporary.replace(path)

    def write_environment(self):
        path = self.output/'environment.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.environment.get_state(), ensure_ascii=False))
        temporary.replace(path)

    async def run(self, user_message, history):
        from harness.models import AgentResponse
        current = self.turn
        before_calls, before_tools = self.model_calls, len(self.tool_calls)
        self.settings.root = self.output
        engine_type = ExternalBusinessQA
        if self.arm in ('single_v6', 'multi_v6'):
            if self.execution_adapter == 'v4':
                from .crm_supervisor import ExternalBusinessQA as engine_type
            elif self.execution_adapter == 'v3':
                from .crm_execution import ExternalBusinessQA as engine_type
            else:
                from .crm_adapter_v6 import ExternalBusinessQA as engine_type
            from .crm_contract import explicit_repeat
            key = ' '.join(user_message.split())
            self.retry_itsm_operations = ([c for c in self.last_itsm_operations
                if c.get('business_outcome') == 'unknown' or not explicit_repeat(user_message)]
                if key == self.last_itsm_message else [])
        engine = engine_type(self, user_message, history, current)
        started = time.monotonic()
        job = asyncio.create_task(asyncio.to_thread(engine.answer))
        try:
            result = await asyncio.shield(job)
            if self.arm in ('single_v6', 'multi_v6'):
                failures = copy.deepcopy(engine.state.get('task_failures', {}))
                outcome = {'turn': current, 'status': result['status'], 'failures': failures}
                self.execution_outcomes.append(outcome)
                self.emit('external_turn_outcome', outcome)
                if result['status'] == 'error':
                    reasons = '; '.join(f['reason'] for f in failures.values())
                    contract = bool(failures) and all(f['kind'] in ('ValueError', 'PermissionError', 'FinalizationError')
                        or any(s in f['reason'] for s in ('达到12次模型调用上限', '达到工具调用上限', '上下文预算不足'))
                        for f in failures.values())
                    prefix = 'V6 execution contract failed: ' if contract else 'V6 execution infrastructure failed: '
                    raise RuntimeError(prefix+reasons)
            return AgentResponse(content=result['answer'],
                                 tool_calls_made=copy.deepcopy(self.tool_calls[before_tools:]),
                                 iterations=self.model_calls-before_calls)
        except asyncio.CancelledError:
            self.stop.set()
            # A cancelled thread may finish its current HTTP request, but cannot
            # start another model/tool operation after the cancellation fence.
            try:
                await asyncio.wait_for(asyncio.shield(job), self.settings.timeout+10)
            except (Exception, asyncio.CancelledError):
                pass
            raise
        finally:
            if self.arm in ('single_v6', 'multi_v6'):
                recorded = [c for c in self.tool_calls[before_tools:]
                            if c.get('business_outcome') in ('committed', 'unknown')]
                self.last_itsm_message = ' '.join(user_message.split())
                self.last_itsm_operations = copy.deepcopy(recorded or [
                    c for c in self.retry_itsm_operations if any(
                        call.get('replay_of') == c['id'] for call in self.tool_calls[before_tools:])])
            self.turn_times.append({'turn': current, 'seconds': time.monotonic()-started,
                                    'model_calls': self.model_calls-before_calls,
                                    'tool_calls': len(self.tool_calls)-before_tools})
            self.turn += 1
