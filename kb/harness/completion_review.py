"""Read-only execution evidence for the official DeepAgents rubric loop.

This module supplies business context; rubric parsing, verdicts, feedback,
iteration state and control flow belong to upstream RubricMiddleware.
"""
import json
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_model_call
from langchain.agents.middleware.types import hook_config
from langchain.agents.structured_output import AutoStrategy, ToolStrategy, OutputToolBinding
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import ToolMessage, convert_to_openai_messages
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from deepagents.middleware.rubric import RubricMiddleware
from langgraph.config import get_config

from .journal import canonical


RUBRIC = (
    'Evaluate the current request using the actual user messages and execution evidence. '
    'The latest user correction or cancellation takes precedence over earlier intent.\n'
    '- Fulfil the current request: carry out authorized available operations; a future promise '
    'does not complete an operation. Correctly asking for genuinely missing required user input, '
    'waiting for an explicitly required confirmation, or reporting an actual policy denial is a valid pause.\n'
    '- Respect execution evidence and write scope: report successful changes only when a matching '
    'committed receipt exists; do not repeat committed writes, bypass refusals, or mutate records '
    'for assessment-only or cancelled requests. Permission approval alone is not a completed change.'
)


def evidence(owner, offset=0, limit=8):
    records = owner.journal.records()
    return {'customer_messages': [dict(row) for row in owner.journal.db.execute(
                'SELECT id AS request_id,question AS text FROM native_requests ORDER BY rowid')],
            'explicit_user_tool_reviews':[{'request_id':row['request_id'],'message':row['message'],
                'decisions':json.loads(row['decisions']),'actions':json.loads(row['actions']),
                'created':row['created']}for row in
                owner.journal.db.execute('SELECT * FROM native_user_reviews ORDER BY created')],
            'current_request_id': owner.request_id,
            'routing_errors': owner.routing_errors(),
            'specialist_capabilities': {role:{'description':cap.get('description',role),
                'tools':[owner.route_targets[n].public_spec.name if n in owner.route_targets else n
                         for n in cap['tools']]}for role,cap in owner.capabilities.items()},
            'receipt_count': len(records),
            'receipts': [{key: row[key] for key in ('id', 'request_id', 'name', 'arguments', 'status', 'result')}
                         for row in records[max(0, offset):max(0, offset)+max(1, min(limit, 8))]]}


def middleware(owner):
    class RootRubricMiddleware(RubricMiddleware):
        """Scope the upstream grader to the supervisor, including forked graphs.

        The upstream middleware still owns criteria, feedback, bookkeeping and
        the iteration cap. The error-triggered mode observes actual tool-node
        failures, without reading task labels or classifying customer intent.
        """
        def is_root(self):
            return get_config().get('metadata',{}).get('lc_agent_name')=='enterprise_native_supervisor'

        def before_agent(self,state,runtime):
            return super().before_agent(state,runtime) if self.is_root() else None

        async def abefore_agent(self,state,runtime):
            return await super().abefore_agent(state,runtime) if self.is_root() else None

        def enabled(self):
            return self.is_root() and (owner.completion_review!='errors' or bool(owner.routing_errors()))

        @hook_config(can_jump_to=['model'])
        def after_agent(self,state,runtime):
            return super().after_agent(state,runtime) if self.enabled() else None

        @hook_config(can_jump_to=['model'])
        async def aafter_agent(self,state,runtime):
            return await super().aafter_agent(state,runtime) if self.enabled() else None

    @tool
    def inspect_execution_evidence(offset: int = 0, limit: int = 8) -> dict:
        """Read literal user requests and actual business receipts; paginate without executing business actions."""
        owner.cancelled()
        return evidence(owner, offset, limit)

    @wrap_model_call
    def grader_context_budget(request, handler):
        owner.cancelled()
        request = request.override(model_settings={**request.model_settings, 'parallel_tool_calls': False})
        if owner.context_budget:
            messages = [*([request.system_message] if request.system_message else []), *request.messages]
            tools = list(request.tools)
            response_format = request.response_format
            # create_agent keeps raw schemas as AutoStrategy at the middleware
            # boundary, then binds the ToolStrategy schema further downstream.
            if isinstance(response_format, AutoStrategy):
                response_format = ToolStrategy(response_format.schema)
            if isinstance(response_format, ToolStrategy):
                tools.extend(OutputToolBinding.from_schema_spec(s).tool for s in response_format.schema_specs)
            count, _ = owner.context_budget.count(convert_to_openai_messages(messages),
                [convert_to_openai_tool(t) for t in tools])
            cfg = owner.context_budget.settings
            reserve=max(cfg.output_tokens,request.model_settings.get('max_tokens') or
                        getattr(request.model,'max_tokens',None) or 1200)
            if count > cfg.context_tokens-reserve-256:
                raise ContextOverflowError('Completion reviewer exceeds the shared context budget')
        return handler(request)

    def prepare(messages):
        records = owner.journal.records()
        snapshot = evidence(owner, max(0, len(records)-4), 4)
        return [*messages, ToolMessage(name='application_execution_snapshot',
            content=canonical(snapshot), tool_call_id='application-execution-snapshot')]

    def evaluated(value):
        record = {'request_id': owner.request_id, **dict(value)}
        owner.review_evaluations.append(record)
        owner.emit('completion_review', record)

    reviewer=owner.role_models.get('reviewer',owner.model)
    return RootRubricMiddleware(model=reviewer.model_copy(update={'max_tokens': 1200}),
        tools=[inspect_execution_evidence], max_iterations=2,
        grader_middleware=[ModelCallLimitMiddleware(run_limit=3, exit_behavior='error'), grader_context_budget],
        prepare_messages_for_grader=prepare, on_evaluation=evaluated)
