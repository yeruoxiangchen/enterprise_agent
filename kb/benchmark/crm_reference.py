"""Thin metering/budget bridge to the pinned official graph agent.

Routing, node prompts, reasoning loops and backend tools come from upstream.
Audit wrappers observe calls without applying our production business guards.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

from harness.graph_agent import GraphAgent
from harness.graph_cli import _build_nodes_from_config
from harness.llm.base import LLMClient
from harness.llm.openai_client import _messages_to_dicts
from harness.tools.registry import ToolRegistry

from kb.context import ContextBudget

EXECUTION_VERSION = 'crm-agent-bench-official-graph-plain-v1'


class AuditedRegistry(ToolRegistry):
    def __init__(self, source, owner):
        super().__init__()
        self._tools = source._tools.copy()
        self.owner = owner

    async def execute_with_status(self, name, arguments):
        owner = self.owner
        call = next((c for c in owner.tool_calls if c['turn'] == owner.turn and c['name'] == name
                     and c['arguments'] == arguments and not c.get('executed')), None)
        if call is None:
            raise RuntimeError('Reference tool execution has no model call audit')
        started = time.monotonic()
        call.update(executed=True, backend_executed=name in self._tools)
        try:
            result = await super().execute_with_status(name, arguments)
            call.update(ok=result.ok, result=result.result)
            from .crm_contract import WRITE_TOOLS
            if name in WRITE_TOOLS and result.ok:
                try:
                    payload = json.loads(result.result)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    call['business_outcome'] = 'rejected' if payload.get('error') else 'committed'
            return result
        finally:
            call['seconds'] = time.monotonic()-started
            owner.emit('reference_tool', copy.deepcopy(call))
            owner.write_environment()


class OfficialGraphAdapter(LLMClient):
    """The official GraphAgent, using the shared model and task budgets."""

    def __init__(self, *, environment, settings, config, policy, seed, output,
                 model_call_limit=24, tool_call_limit=30):
        from crm_bench_services import LocalEvalClient
        self.environment, self.settings, self.seed = environment, settings, seed
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.arm, self.turn = 'official_graph', 0
        self.registry = environment.create_registry()
        self.tool_calls, self.turn_times, self.execution_outcomes = [], [], []
        self.model_calls = self.prompt_tokens = self.completion_tokens = self.usage_missing_calls = 0
        self.llm_seconds = 0.0
        self.call_limit, self.tool_limit = model_call_limit, tool_call_limit
        self.turn_calls = self.turn_tool_attempts = 0
        self.budget = ContextBudget(settings)
        self.http = LocalEvalClient(settings.llm_url.removesuffix('/v1'), settings.llm_model,
                                    self.output/'actor-model.jsonl', seed, timeout=settings.timeout)
        nodes = _build_nodes_from_config(config, environment=environment)
        for node in nodes.values():
            node.tools = AuditedRegistry(node.tools, self)
        self.route_names = set(nodes) | {'_unsafe', '_out_of_scope'}
        # Both candidate and reference receive the same public policy. Task
        # instructions, expected actions and grading assertions are never passed.
        prompt = config['system_prompt'] + '\n\nPublic ITSM policy:\n' + policy
        self.graph = GraphAgent(llm=self, system_prompt=prompt, nodes=nodes,
                                entry_node=config.get('entry_node'), temperature=0,
                                max_tokens=settings.output_tokens)

    def emit(self, kind, payload):
        with (self.output/'events.jsonl').open('a') as stream:
            stream.write(json.dumps({'time': time.time(), 'turn': self.turn, 'kind': kind,
                                     'data': payload}, ensure_ascii=False)+'\n')

    def write_environment(self):
        temporary = self.output/'environment.tmp'
        temporary.write_text(json.dumps(self.environment.get_state(), ensure_ascii=False))
        temporary.replace(self.output/'environment.json')

    async def chat(self, messages, tools=None, temperature=0, max_tokens=1400, response_format=None):
        from crm_bench_services import InvalidActorToolCall
        if self.turn_calls >= self.call_limit:
            raise RuntimeError('Reference model-call budget exhausted')
        definitions = [t.model_dump() for t in tools or []]
        count, _ = await asyncio.to_thread(self.budget.count, _messages_to_dicts(messages), definitions)
        if count > self.settings.context_tokens-self.settings.output_tokens-256:
            raise RuntimeError('Reference context budget exhausted')
        self.turn_calls += 1
        # Our LangChain actor uses a constant per-trial seed on every call.
        self.http.seed = self.seed-self.http.usage['num_calls']
        try:
            response = await self.http.chat(messages, tools=tools, temperature=temperature,
                max_tokens=min(max_tokens, self.settings.output_tokens), response_format=response_format)
        except InvalidActorToolCall as exc:
            raise RuntimeError('Reference malformed model response: '+str(exc)) from exc
        finally:
            self.model_calls = self.http.usage['num_calls']
            self.prompt_tokens = self.http.usage['prompt_tokens']
            self.completion_tokens = self.http.usage['completion_tokens']
            self.usage_missing_calls = self.model_calls-self.http.metered_calls
            self.llm_seconds = self.http.seconds
        routing = bool(tools) and all(t.function.name in self.route_names for t in tools)
        calls = response.tool_calls or []
        self.turn_tool_attempts += len(calls)
        if routing:
            self.emit('reference_route', {'choices': [c.model_dump() for c in calls], 'content': response.content})
        else:
            for call in calls:
                self.tool_calls.append({'turn': self.turn, 'id': call.id, 'name': call.name,
                    'arguments': copy.deepcopy(call.arguments), 'ok': False, 'executed': False,
                    'backend_executed': False, 'result': '{"error":"tool call not executed"}',
                    'seconds': 0.0, 'execution_index': len(self.tool_calls)})
        if self.turn_tool_attempts > self.tool_limit:
            raise RuntimeError('Reference tool-call budget exhausted')
        return response

    async def run(self, user_message, history):
        started = time.monotonic()
        before = len(self.tool_calls)
        before_models = self.model_calls
        self.turn_calls = self.turn_tool_attempts = 0
        try:
            response = await self.graph.run(user_message, history=history)
            return response.model_copy(update={'tool_calls_made': copy.deepcopy(self.tool_calls[before:])})
        finally:
            self.turn_times.append({'turn': self.turn, 'seconds': time.monotonic()-started,
                'model_calls': self.model_calls-before_models, 'tool_calls': len(self.tool_calls)-before})
            self.write_environment()
            self.turn += 1

    def stream_chat(self, *args, **kwargs):
        raise NotImplementedError('Reference benchmark uses non-streaming actor calls')

    async def aclose(self):
        await self.http.aclose()
