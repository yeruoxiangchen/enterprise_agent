"""Opt-in v6 ITSM execution; only public policies, tools and live dialogue.

The single/multi variants share identical progress guards and budgets. Tool
batches commit in the model's declared order, including rejected attempts.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import threading
import time
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call, wrap_tool_call
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import Field, model_validator

from .crm_adapter import (CAPABILITIES, ExternalBusinessQA as ExternalV5,
                         ExternalResult as ResultV5, RoutingPlanError, closing_acknowledgement)
from kb.backends.business_multi_v2 import source_spans
from kb.backends.business_multi_v6 import BusinessQA as V6
from kb.backends.business_v6_control import (OrderedPlan, PlannedTask, ReadProgress,
    ReassignmentRequested, compile_plan, merge_results, order_tasks, prior_results,
    stable_task_id, task_control)
from .crm_contract import (PERMISSION_ACTIONS, READ_TOOLS, SCOPE_REPLY, SCOPE_REPLY_ZH,
    WRITE_TOOLS, canonical_call, is_continuation, is_service_request, mask_quoted, payload, read_fact,
    receipt_answer, receipt_still_applies,
    requested_write_targets, requested_writes, selected_ticket, split_explicit_steps,
    tool_hints, unique_context_ticket, write_allowance, write_observation, write_outcome)

EXECUTION_VERSION = 'business_v6_external_v2'


class FinalizationError(ValueError):
    """The model exhausted a bounded formatting repair without another write."""


class ExternalResult(ResultV5):
    """Finish this turn from actual observations, or request a first-decision domain transfer.

    Submit the answer once lookups/actions are complete. Repeating a successful
    lookup is unnecessary. This final-response tool does not modify records.
    """
    resolution: Literal['complete', 'needs_user_input', 'insufficient', 'out_of_scope', 'reject_domain'] = Field(
        description='Finish from observations. Use out_of_scope for non-ITSM requests. '
                    'Use reject_domain only for a first-decision domain mismatch.')
    suggested_domain: Literal['technical', 'policy', 'service'] | None = None
    completed_action_ids: list[str] = Field(default_factory=list, max_length=15,
        description='Optional receipt_id values returned by committed write observations. Omit when unsure.')


def single_result_schema():
    # Keep the same final tool name/metering, with no unusable handoff choice in
    # the single-agent prompt or schema.
    class ExternalResult(ResultV5):
        """Finish the turn with an answer grounded in the actual tool observations.

        Call once the requested lookups/actions are done or clarification/refusal
        is needed. Do not repeat a completed lookup. This tool changes no records.
        """
        resolution: Literal['complete', 'needs_user_input', 'insufficient', 'out_of_scope'] = Field(
            description='Finish from observations. Use out_of_scope for non-ITSM requests.')
        completed_action_ids: list[str] = Field(default_factory=list, max_length=15,
            description='Optional receipt_id values returned by committed write observations.')
    return ExternalResult


class ExternalBusinessQA(V6, ExternalV5):
    def blocked_answer(self):
        return 'The prerequisite task is incomplete; this step has not been executed.'

    def reassignment_answer(self):
        return 'Please clarify the target and intended operation so I can select the right capability.'

    def failure_answer(self, exc, write_unknown):
        return ('The write result requires reconciliation against the original record before retrying.' if write_unknown
                else 'This step did not complete. Previous results and execution records have been retained.')

    def merge(self, state):
        pending = [self.request_text(r) for r in state['results'].values()
                   if r['status'] == 'clarify' and requested_writes(self.request_text(r))]
        self.owner.pending_itsm_request = pending[0] if len(pending) == 1 else None
        return merge_results(state, english=not bool(re.search(r'[\u4e00-\u9fff]', self.run['question'])),
                             include_questions=False)

    def __init__(self, owner, question, history, turn):
        ExternalV5.__init__(self, owner, question, history, turn)
        self.state['execution_version'] = EXECUTION_VERSION
        self.state['write_journal'] = {}
        self.state['write_counts'] = {}
        self.state['reused_receipts'] = {}
        self.progress = ReadProgress(self.state)
        self.condition = threading.Condition(owner.tool_lock)
        self.batch_order, self.batch_done = [], set()
        self.current_call_id = None
        self.dependency_receipts = []
        pending = getattr(owner, 'pending_itsm_request', None)
        self.continued_request = pending if pending and is_continuation(question) else None

    @property
    def single(self):
        return self.owner.arm == 'single_v6'

    def request_text(self, task):
        return '\n'.join(part for part in (self.continued_request, task['question']) if part)

    def write_actions(self, task):
        return requested_writes(self.request_text(task))

    def write_targets(self, task, text):
        return requested_write_targets(text)

    def write_limit(self, name, arguments, task):
        return write_allowance(name, arguments, self.request_text(task))

    def scope_reply(self):
        return SCOPE_REPLY_ZH if re.search(r'[\u4e00-\u9fff]', self.run['question']) else SCOPE_REPLY

    def direct_plan(self, *, fallback=False):
        task = {'domain': 'service', 'span_ids': ['U0'], 'question': self.run['question'],
                'context': '', 'depends_on': [], 'use_all_tools': True}
        task['id'] = stable_task_id(task)
        if fallback:
            self.emit('external_route_fallback', {'task': task, 'reason': 'no valid specialist plan'})
        return {'tasks': [task], 'clarification': '', 'results': {}, 'cursor': 0}

    def scope_plan(self):
        return {'tasks': [], 'clarification': '', 'cursor': 0, 'results': {
            'scope': {'domain': 'service', 'question': self.run['question'], 'status': 'answered',
                      'answer': self.scope_reply()}}}

    def route(self, state):
        if closing_acknowledgement(self.run['question']):
            return ExternalV5.route(self, state)
        if not is_service_request(self.request_text({'question': self.run['question']})):
            self.emit('external_scope_reply', {'reason': 'no current ITSM operation or request'})
            return self.scope_plan()
        if self.single:
            return self.direct_plan()
        def spans_from(text, prefix='U'):
            pieces = []
            for source in source_spans(mask_quoted(text, 'x'), prefix):
                original = text[source['start']:source['end']]
                for start, end in split_explicit_steps(original):
                    pieces.append({'id': prefix+str(len(pieces)), 'start': source['start']+start,
                                   'end': source['start']+end, 'text': original[start:end]})
            return pieces

        spans = spans_from(self.run['question'])
        for i, message in enumerate(self.history):
            if message['role'] == 'user':
                spans.extend(spans_from(message['content'], f'H{i}_'))

        current_ids = [s['id'] for s in spans if s['id'].startswith('U')]
        known_ids = {s['id'] for s in spans}
        catalog = {domain: self.owner.config['nodes'][name] for domain, name in CAPABILITIES.items()}
        tools_by_domain = {domain: {t['name'] for t in node['tools']} for domain, node in catalog.items()}
        available_tools = sorted(self.owner.registry.tool_names)

        class RoutedTask(PlannedTask):
            span_ids: list[str] = Field(min_length=1, max_length=12,
                description='Copy current U IDs from the provided spans.',
                json_schema_extra={'items': {'type': 'string', 'enum': current_ids}})
            context_ids: list[str] = Field(default_factory=list, max_length=12,
                description='Optional background span IDs. Use [] when the task is self-contained.',
                json_schema_extra={'items': {'type': 'string', 'enum': sorted(known_ids)}})
            required_tools: list[str] = Field(default_factory=list, max_length=12,
                description='Canonical tool names needed for this task, without arguments.',
                json_schema_extra={'items': {'type': 'string', 'enum': available_tools}})

        def compile_external(plan):
            if plan.out_of_scope:
                if plan.tasks:
                    raise ValueError('An out_of_scope reply cannot also dispatch tasks')
                return [], []
            assigned = {ident for task in plan.tasks for ident in task.span_ids}
            dropped = []
            clean_tasks = []
            for task in plan.tasks:
                if not set(task.required_tools) <= set(available_tools):
                    raise ValueError('required_tools must use names in the tool catalog')
                context = list(dict.fromkeys(i for i in task.context_ids if i in known_ids - assigned))
                dropped.extend(i for i in task.context_ids if i not in context)
                clean_tasks.append(task.model_copy(update={'context_ids': context}))
            tasks = compile_plan(plan.model_copy(update={'tasks': clean_tasks}), spans)
            by_sources = {tuple(t.span_ids): t for t in clean_tasks}
            source_order = sorted(tasks, key=lambda t: min(current_ids.index(i) for i in t['span_ids']))
            for index, task in enumerate(source_order):
                planned = by_sources[tuple(task['span_ids'])]
                needed = set(planned.required_tools) | tool_hints(self.request_text(task))
                task['required_tools'] = sorted(needed)
                if not needed <= tools_by_domain[task['domain']]:
                    matches = [domain for domain, tools in tools_by_domain.items() if needed <= tools]
                    if matches:
                        task['domain'] = matches[0]
                    else:
                        task['use_all_tools'] = True
                if index and re.match(r'\s*(?:then\b|second\b|after that\b|然后|之后)', task['question'], re.I):
                    predecessor = source_order[index - 1]['id']
                    task['depends_on'] = list(dict.fromkeys([*task['depends_on'], predecessor]))
            return order_tasks(tasks), dropped

        class SpanRoute(OrderedPlan):
            """Route current U spans to capable specialists. H spans are optional background only."""
            tasks: list[RoutedTask] = Field(default_factory=list, max_length=6)
            out_of_scope: bool = False

            @model_validator(mode='after')
            def validate_sources_and_dependencies(self):
                compile_external(self)
                return self

        attempts = 0

        @wrap_model_call
        def meter(request, handler):
            nonlocal attempts
            if attempts >= 2:
                raise RoutingPlanError('Router exhausted its two-call plan budget')
            attempts += 1
            return ExternalV5.meter(self, request, handler)

        def route_feedback(exc):
            return (f'Use current task IDs {current_ids}. Copy an existing background ID or leave context_ids empty. '
                    f'Depends_on may refer only to another current task. {str(exc)[:700]}')

        prompt = ('Route the current ITSM request by copying IDs from spans. Choose specialists by their tool lists. '
                  'A self-contained task has empty context_ids and depends_on. Copy history IDs only for needed background. '
                  'Assign every current span once, or mark it unused if irrelevant. Preserve explicit order using depends_on. '
                  'Use out_of_scope for unrelated requests. If a request mixes unrelated text with ITSM work, retain the ITSM work. '
                  'Use clarification for missing information. Return SpanRoute.\n'
                  + json.dumps({d: {'description': node['description'], 'tools': sorted(tools_by_domain[d])}
                                for d, node in catalog.items()}))
        agent = create_agent(self.model, tools=[], system_prompt=prompt, middleware=[meter],
                             response_format=ToolStrategy(SpanRoute, handle_errors=route_feedback))
        try:
            output = agent.invoke({'messages': [{'role': 'user', 'content': json.dumps({'spans': spans})}]},
                                  config={'recursion_limit': 8})
        except RoutingPlanError:
            return self.direct_plan(fallback=True)
        plan = output.get('structured_response')
        if not isinstance(plan, SpanRoute):
            return self.direct_plan(fallback=True)
        tasks, dropped = compile_external(plan)
        if not tasks and tool_hints(self.request_text({'question': self.run['question']})):
            return self.direct_plan(fallback=True)
        if plan.out_of_scope or not tasks:
            answer = self.scope_reply() if plan.out_of_scope else ' '.join(filter(None, [plan.clarification, self.scope_reply()]))
            self.emit('external_route', {'tasks': [], 'clarification': plan.clarification, 'out_of_scope': plan.out_of_scope})
            return {'tasks': [], 'clarification': '', 'cursor': 0, 'results': {
                'scope': {'domain': 'service', 'question': self.run['question'], 'status': 'answered', 'answer': answer}}}
        self.state['plan'] = copy.deepcopy(tasks)
        self.save()
        self.emit('external_route', {'tasks': tasks, 'clarification': plan.clarification,
                                    'dropped_context_ids': dropped, 'execution_version': self.state['execution_version']})
        return {'tasks': tasks, 'clarification': plan.clarification, 'results': {}, 'cursor': 0}

    def environment_version(self):
        return hashlib.sha256(json.dumps(self.owner.environment.get_state(), sort_keys=True).encode()).hexdigest()

    def cached_read_ok(self):
        return True

    def block_stalled_read(self, name, arguments, control):
        return control['stalled'] >= 2 and name in READ_TOOLS

    def latest_ticket_list(self):
        for call in reversed(self.owner.tool_calls):
            if (call['name'] == 'list_tickets' and call.get('backend_executed') and call['ok']
                    and not call['arguments'].get('status')):
                value = payload(call['result'])
                if isinstance(value, dict) and isinstance(value.get('tickets'), list):
                    return value['tickets']
        return []

    def guard_arguments(self, name, arguments):
        scoped_text = self.request_text(self.active_task)
        user_text = '\n'.join([scoped_text]+[m['content'] for m in self.history if m['role'] == 'user'])
        ticket_id = arguments.get('ticket_id')
        session = self.owner.environment.get_state().get('session', {})
        if name in WRITE_TOOLS:
            if name not in self.write_actions(self.active_task):
                return {'error': 'write_intent_required', 'message': 'The current request does not authorize this change.'}
            control = task_control(self.state, self.active_task)
            if control.get('write_unknown'):
                return {'error': 'write_needs_reconciliation', 'message': 'Verify the prior write result before making another change.'}
            current = self.active_task['question']
            specified = {ticket for action, ticket in self.write_targets(self.active_task, current) if action == name}
            if not specified:
                specified = {ticket for action, ticket in self.write_targets(self.active_task, scoped_text) if action == name}
            if not specified and (context_ticket := unique_context_ticket(scoped_text, name)):
                specified = {context_ticket}
            target_matches = ticket_id and (ticket_id in specified if specified else
                                            selected_ticket(scoped_text, self.latest_ticket_list()) == ticket_id)
            if not specified and not target_matches and re.search(r'\b(?:it|its|that ticket|same ticket)\b|该工单|这张', current, re.I):
                previous_targets = {c['arguments']['ticket_id'] for c in self.dependency_receipts}
                target_matches = previous_targets == {ticket_id}
            if not target_matches:
                return {'error': 'ticket_identity_required', 'message': 'Ask which ticket to change, or identify it uniquely from the user description and a complete ticket lookup.'}
            if name in ('assign_ticket', 'reassign_ticket'):
                assignee = arguments.get('assignee', '')
                if not assignee or not re.search(r'(?<![\w.@-])'+re.escape(assignee)+r'(?![\w@-]|\.[\w])', scoped_text):
                    return {'error': 'assignee_required', 'message': 'Confirm the assignee from the current request.'}
            if name == 'reassign_ticket':
                reads = [payload(c['result']) for c in self.owner.tool_calls
                         if c['name'] == 'get_ticket' and c.get('backend_executed') and c['ok']
                         and c['arguments'].get('ticket_id') == ticket_id]
                if not reads:
                    return {'error': 'lookup_required', 'message': 'Look up the current owner before reassigning.'}
                if not reads[-1] or not reads[-1].get('assigned_to'):
                    return {'error': 'unassigned_ticket', 'message': 'This ticket has no current assignee to reassign from.'}
            if name == 'close_ticket':
                check = session.get('dependency_checks', {}).get(ticket_id)
                if check is None:
                    return {'error': 'dependency_check_required', 'message': 'Check the ticket dependencies before closing.'}
                confirmed = re.search(r'\b(?:anyway|despite|i confirm|i am aware|i\'m aware)\b|仍然|确认关闭', scoped_text, re.I)
                if check.get('open_dependencies') and not confirmed:
                    return {'error': 'close_confirmation_required', 'message': 'The ticket has open dependencies. Ask the user to confirm closing it anyway.'}
        if name == 'check_permission':
            identity = arguments.get('user_id', '')
            if arguments.get('action') not in PERMISSION_ACTIONS:
                return {'error': 'unknown_permission_action', 'message': 'Choose a canonical permission action: '+', '.join(PERMISSION_ACTIONS)}
            session_identity = session.get('user_id')
            if not identity or (identity != session_identity and not re.search(
                    r'(?<![\w.@-])'+re.escape(identity)+r'(?![\w@-]|\.[\w])', user_text)):
                return {'error': 'identity_required', 'message': 'Ask the user which identity to check; never invent a user ID.'}
        if name == 'delete_ticket':
            ticket = arguments.get('ticket_id')
            check = session.get('dependency_checks', {}).get(ticket)
            if check is None or check.get('open_dependencies'):
                return {'error': 'dependency_precondition', 'message': 'A current clean dependency check for this ticket is required.'}
            if ticket not in session.get('delete_authorizations', {}):
                return {'error': 'permission_precondition', 'message': 'Ticket-specific delete permission is required.'}
            permissions = [c for c in self.owner.tool_calls if c['name'] == 'check_permission' and c.get('executed')
                           and c['arguments'].get('ticket_id') == ticket and c['arguments'].get('action') == 'delete']
            if not permissions:
                return {'error': 'permission_precondition', 'message': 'Obtain an actual permission receipt for this ticket.'}
            latest = json.loads(permissions[-1]['result'])
            if (latest.get('allowed') is not True or latest.get('user_id') !=
                    session['delete_authorizations'][ticket]):
                return {'error': 'permission_precondition', 'message': 'The latest checked identity has no current delete permission for this ticket.'}
        return None

    def make_tool(self, definition):
        requested_name = definition.function.name

        def execute(**arguments):
            self.cancelled()
            item = next((c for c in self.owner.tool_calls if c['id'] == self.current_call_id
                         and c['turn'] == self.owner.turn), None)
            if item is None:
                raise RuntimeError('Tool execution has no matching audited request')
            task = self.active_task
            control = task_control(self.state, task)
            name, arguments = canonical_call(requested_name, arguments, self.write_actions(task))
            if name != requested_name or arguments != item['arguments']:
                item.update(requested_name=requested_name, requested_arguments=item['arguments'],
                            name=name, arguments=arguments)
            write = name in WRITE_TOOLS
            key = json.dumps([task['id'], name, arguments], sort_keys=True) if write else None
            journal = self.state['write_journal']
            previous = journal.get(key) if write else None
            if previous and previous['business_outcome'] == 'committed':
                allowance = self.write_limit(name, arguments, task)
                if (self.state['write_counts'].get(key, 0) < allowance
                        or not receipt_still_applies(previous, self.owner.environment.get_state())):
                    previous = None
            if write and previous is None:
                previous = next((c for c in getattr(self.owner, 'retry_itsm_operations', [])
                                 if c['name'] == name and c['arguments'] == arguments
                                 and (c.get('business_outcome') == 'unknown'
                                      or receipt_still_applies(c, self.owner.environment.get_state()))), None)
            blocked = ({'error': 'tool_not_available', 'message': 'This operation needs another capability.'}
                       if name not in self.available_tools else self.guard_arguments(name, arguments))
            cached = self.progress.get(task['id'], name, arguments, self.environment_version()) if name in READ_TOOLS else None
            started = time.monotonic()
            item.update(execution_index=self.owner.executed_sequence, executed=True,
                        backend_executed=False, cache_hit=False, task_id=task['id'])
            self.owner.executed_sequence += 1
            observation = None
            if previous is not None:
                # Same logical operation in this task and turn. Only a new
                # task may authorize a distinct operation with identical args.
                value, ok = previous['result'], False
                item.update(business_outcome='reused', replay_of=previous['id'])
                journal[key] = copy.deepcopy(previous)
                if previous['business_outcome'] == 'committed':
                    self.state['reused_receipts'][previous['id']] = copy.deepcopy(previous)
                    self.state['write_counts'][key] = max(self.state['write_counts'].get(key, 0),
                                                         self.write_limit(name, arguments, task))
                observation = write_observation(previous)
                control['stalled'] += 1
            elif blocked:
                value, ok = json.dumps(blocked), False
                if write:
                    item['business_outcome'] = 'blocked'
                control['stalled'] += 1
            elif cached:
                value, ok = cached['result'], self.cached_read_ok()
                item['cache_hit'] = True
                if cached.get('source_call_id'):
                    item.update(read_outcome='reused', replay_of=cached['source_call_id'])
                control['stalled'] += 1
            elif self.block_stalled_read(name, arguments, control):
                value, ok = json.dumps({'error': 'no_progress', 'message': 'Use existing observations and finish or ask for missing information.'}), False
            else:
                if write:
                    control['write_started'] = True
                    control['write_unknown'] = True
                    self.progress.invalidate()
                    journal[key] = {**item, 'business_outcome': 'unknown',
                                    'result': json.dumps({'error': 'Write result needs reconciliation'})}
                    self.save()
                result = asyncio.run(self.owner.registry.execute_with_status(name, arguments))
                value, ok = result.result, result.ok
                item['backend_executed'] = True
                if write:
                    outcome = write_outcome(name, arguments, value, ok)
                    item.update(business_outcome=outcome, result=value, ok=ok)
                    if outcome == 'committed':
                        self.state['write_counts'][key] = self.state['write_counts'].get(key, 0) + 1
                    control['write_unknown'] = outcome == 'unknown'
                    journal[key] = copy.deepcopy(item)
                    observation = write_observation(item)
                    control['stalled'] = 0 if outcome == 'committed' else control['stalled'] + 1
                else:
                    control['stalled'] = 0 if ok else control['stalled']+1
                if name in READ_TOOLS and ok:
                    # Preflight checks can update canonical session state: key
                    # their observations by the post-execution version.
                    self.progress.put(task['id'], name, arguments, self.environment_version(), value)
            item.update(result=value, ok=ok, seconds=time.monotonic()-started)
            self.owner.write_environment()
            self.save()
            self.emit('external_tool', item)
            return observation if observation is not None else value

        parameters = copy.deepcopy(definition.function.parameters)
        if requested_name == 'check_permission':
            parameters['properties']['action'].update(enum=list(PERMISSION_ACTIONS),
                description='Permission name, such as escalate or delete.')
        return StructuredTool.from_function(execute, name=requested_name, description=definition.function.description,
                                            args_schema=parameters)

    def execute_task(self, domain, state):
        task = state['tasks'][state['cursor']]
        self.active_task = task
        self.dependency_receipts = [c for dep in task.get('depends_on', [])
            for c in state['results'].get(dep, {}).get('receipts', [])
            if c.get('business_outcome') == 'committed']
        control = task_control(self.state, task)
        general = self.single or task.get('use_all_tools', False)
        if general:
            hints = tool_hints(self.request_text(task))
            relevant = {'service'}
            if 'delete_ticket' in hints:
                relevant = {'policy'}
            if 'get_business_hours' in hints:
                relevant.add('technical')
            instructions = '\n\n'.join(self.owner.config['nodes'][CAPABILITIES[d]]['instructions']
                                       for d in sorted(relevant))
            names = set(self.owner.registry.tool_names)
        else:
            node = self.owner.config['nodes'][CAPABILITIES[domain]]
            instructions = node['instructions']
            names = {t['name'] for t in node['tools']}
        if 'check_permission' in tool_hints(self.request_text(task)) and not requested_writes(self.request_text(task)):
            instructions = (self.owner.config['nodes']['general']['instructions'] + '\n'
                'Check only the requested account permission. The action name is a permission such as view, edit, '
                'assign, delete or escalate. Use an optional ticket ID only if the user provided one. '
                'Report the allowed value. A permission lookup does not request a ticket change.')
        self.available_tools = names
        tools = [self.make_tool(d) for d in self.owner.registry.get_definitions() if d.function.name in names]
        schema = single_result_schema() if general else ExternalResult
        model_steps = 0

        @wrap_model_call
        def meter(request, handler):
            nonlocal model_steps
            finalizing = self.state['calls'] >= 10 or control['stalled'] >= 2
            settings = {**request.model_settings, 'parallel_tool_calls': False}
            request = request.override(model_settings=settings)
            if finalizing:
                request = request.override(tools=[], tool_choice='required')
            for attempt in range(2):
                response = ExternalV5.meter(self, request, handler)
                model_steps += 1
                message = next((m for m in response.result if isinstance(m, AIMessage)), None)
                calls = message.tool_calls if message else []
                actions = [c for c in calls if c['name'] != 'ExternalResult']
                mixed = actions and len(actions) != len(calls)
                if not mixed and not (finalizing and actions):
                    self.batch_order = [c['id'] for c in actions]
                    self.batch_done = set()
                    return response
                for _ in actions:
                    self.use_tool()
                self.emit('external_finish_repair', {'point': task['id'], 'call': self.owner.model_calls,
                                                     'mixed': bool(mixed), 'finish_only': finalizing})
                if attempt == 0:
                    feedback = ('Submit only ExternalResult using the existing observations.' if finalizing else
                                'First call the necessary business tools, or submit only ExternalResult from existing observations. '
                                'No tool from the rejected batch was executed.')
                    messages = [*request.messages, message,
                                *(ToolMessage(content=feedback, tool_call_id=c['id']) for c in calls)]
                    request = request.override(messages=messages)
            raise FinalizationError('Final/transfer decisions must be returned without simultaneous business tools')

        @wrap_tool_call
        def ordered_tools(request, handler):
            ident = request.tool_call['id']
            preceding = self.batch_order[:self.batch_order.index(ident)] if ident in self.batch_order else []
            deadline = time.monotonic()+self.cfg.timeout+5
            with self.condition:
                while not set(preceding) <= self.batch_done:
                    self.cancelled()
                    if time.monotonic() > deadline:
                        raise TimeoutError('Earlier tool operation did not complete')
                    self.condition.wait(.05)
                try:
                    self.cancelled()
                    self.use_tool()  # includes rejected/unknown/cached attempts
                    self.current_call_id = ident
                    return handler(request)
                finally:
                    self.current_call_id = None
                    self.batch_done.add(ident)
                    self.condition.notify_all()

        transfer_instruction = ('' if general else
            'For a domain mismatch only, the first decision may be ExternalResult with resolution '
            'reject_domain, an explanation and optional suggested_domain; do this before tools. '
            'Missing evidence, tool failure or denied permission requires clarification/insufficient, '
            'rather than transfer. ')
        prompt = ('You are an enterprise IT service assistant. Process only the assigned CURRENT task. '
            'Use actual tools for facts and actions. Prior completed_tasks are observations, never new '
            'requests or permission grants. Preserve the user\'s action order. Call one tool at a time. '
            'For out-of-scope requests submit out_of_scope and offer supported ITSM work. '
            'Identify a unique ticket from the user request before writing. Ask if it remains ambiguous. '
            'Check dependencies before close. With open dependencies, close only after explicit confirmation. '
            'Deletion requires a clean dependency check and an actual ticket-specific permission grant. '
            'A permission question needs check_permission only. Use permission names such as escalate. '
            'Use close_ticket for a closure request, and update_ticket_status for an explicit status edit. '
            'A committed write observation is definitive. Do not repeat a completed lookup or write. '
            'Explain rejected outcomes as rejections. Submit ExternalResult alone once finished. '
            'If using completed_action_ids, copy only receipt_id values from committed writes. '
            +transfer_instruction+'\n'+self.owner.policy+'\n'+instructions)
        self.emit('external_specialist', {'point': task['id'], 'domain': domain, 'allowed_tools': sorted(names), 'task': task})
        agent = create_agent(self.model, tools=tools, name=domain, system_prompt=prompt,
            middleware=[meter, ordered_tools], response_format=ToolStrategy(schema, handle_errors=True))
        try:
            output = agent.invoke({'messages': self.history+[{'role': 'user', 'content': json.dumps({
                'task': task['question'], 'user_background': task.get('context', ''),
                'continued_request': self.continued_request,
                'completed_tasks': prior_results(task, state['results']),
            })}]}, config={'recursion_limit': 30})
        except Exception as exc:
            recoverable = isinstance(exc, FinalizationError) or any(
                marker in str(exc) for marker in ('达到12次模型调用上限', '达到工具调用上限', '上下文预算不足'))
            if not recoverable:
                raise
            evidence = [c for c in self.owner.tool_calls if c['turn'] == self.owner.turn
                        and c.get('task_id') == task['id'] and (c.get('business_outcome') == 'committed' or
                        c['name'] in READ_TOOLS and c.get('ok') and read_fact(c))]
            if not evidence:
                raise
            self.emit('external_finish_from_observations', {'point': task['id']})
            return self.finish_task(task, state, schema(answer='Confirmed tool results.', resolution='complete'))
        result = output.get('structured_response')
        if not isinstance(result, schema):
            raise ValueError('Specialist did not return a valid ExternalResult')
        if result.resolution == 'reject_domain':
            if not general and model_steps == 1 and not control['write_started']:
                raise ReassignmentRequested(result.answer, result.suggested_domain)
            self.emit('external_transfer_blocked', {'point': task['id'], 'reason': 'execution already started'})
        return self.finish_task(task, state, result)

    def finish_task(self, task, state, result):
        calls = [c for c in self.owner.tool_calls if c['turn'] == self.owner.turn and c.get('task_id') == task['id']]
        receipts = [c for c in calls if c.get('business_outcome') == 'committed']
        receipts.extend(self.state['reused_receipts'][c['replay_of']] for c in calls
                        if c.get('replay_of') in self.state['reused_receipts']
                        and self.state['reused_receipts'][c['replay_of']]['business_outcome'] == 'committed')
        receipts = list({c['id']: c for c in receipts}.values())
        if not set(result.completed_action_ids) <= {c['id'] for c in receipts}:
            self.emit('external_receipt_claim_corrected', {'point': task['id'],
                'claimed': result.completed_action_ids, 'committed': [c['id'] for c in receipts]})
        actions = requested_writes(self.request_text(task))
        remaining = actions - {c['name'] for c in receipts}
        missing_targets = requested_write_targets(self.request_text(task)) - {
            (c['name'], c['arguments']['ticket_id']) for c in receipts}
        reads = [c for c in calls if c['name'] in READ_TOOLS and c.get('ok')]
        proof_scopes = {(c['turn'], c['task_id']) for c in receipts if c['turn'] != self.owner.turn}
        reads.extend(c for c in self.owner.tool_calls if (c['turn'], c.get('task_id')) in proof_scopes
                     and c['name'] in READ_TOOLS and c.get('ok'))
        missing_reads = (tool_hints(self.request_text(task)) & READ_TOOLS) - {c['name'] for c in reads}
        last_write = max((c['execution_index'] for c in receipts), default=-1)
        facts = list(dict.fromkeys(fact for c in reads
                                  if (not receipts or c['name'] != 'get_ticket' or c['execution_index'] > last_write)
                                  and (fact := read_fact(c))))
        status = {'complete': 'answered', 'needs_user_input': 'clarify', 'insufficient': 'insufficient',
                  'out_of_scope': 'answered', 'reject_domain': 'insufficient'}[result.resolution]
        answer = result.answer
        if receipts:
            answer = '\n'.join([*(receipt_answer(c, reads) for c in receipts), *facts])
            if remaining or missing_targets or missing_reads:
                status = 'clarify' if result.resolution == 'needs_user_input' else 'insufficient'
                answer += '\nThe remaining requested changes or lookups have not been completed.'
            else:
                status = 'answered'
        elif actions:
            failures = [value for c in calls if isinstance(value := payload(c['result']), dict) and value.get('error')]
            if failures and isinstance(failures[-1], dict):
                failure = failures[-1]
                reason = failure.get('message') or failure.get('error')
                if failure.get('error') in ('ticket_identity_required', 'assignee_required', 'close_confirmation_required', 'identity_required'):
                    status = 'clarify'
                elif status == 'answered':
                    status = 'insufficient'
                answer = '\n'.join([*facts, reason or 'The requested change was not completed.'])
            elif status == 'answered':
                status, answer = 'insufficient', 'The requested change was not completed.'
            if 'close_ticket' in actions and not receipts:
                dependencies = [payload(c['result']) for c in reads if c['name'] == 'check_dependencies']
                confirmed = re.search(r'\b(?:anyway|despite|i confirm|i am aware|i\'m aware)\b|确认关闭', self.request_text(task), re.I)
                if any(d and d.get('open_dependencies') for d in dependencies) and not confirmed:
                    status, answer = 'clarify', 'The ticket has open dependencies. Do you want to close it anyway?'
        elif result.resolution == 'out_of_scope':
            answer = self.scope_reply()
        elif result.completed_action_ids and not receipts:
            status, answer = 'insufficient', '\n'.join(facts) or 'No tool result confirms that a change was made.'
        elif facts and status == 'answered':
            # Canonical permission and desk-hour facts are rendered directly.
            answer = '\n'.join(facts)
        if missing_reads and status == 'answered':
            status = 'insufficient'
            answer = '\n'.join([*facts, 'The remaining requested lookup has not been completed.'])
        completed = self.complete_task(task, state, status, answer)
        completed['results'][task['id']]['receipts'] = copy.deepcopy(receipts)
        return completed
