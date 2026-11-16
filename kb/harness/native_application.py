"""Authenticated Web adapter for the upstream NativeHarness execution core.

The application owns login, ACLs, draft submission and ticket state transitions.
DeepAgents owns planning, forked specialists, context management and checkpoints.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from langchain_openai import ChatOpenAI

from kb.context import ContextBudget
from .native import NativeHarness, NativeBudgetExceeded
from .ports import ApplicationPort

POLICY = 'business_native_v1'


def execute(h, run_id):
    db = h.db
    if not db.execute("UPDATE runs SET status='running',updated_at=? WHERE id=? AND status='queued'",
                      (time.time(), run_id)):
        return
    run = db.one('SELECT * FROM runs WHERE id=?', (run_id,))
    user = db.one('SELECT id,role,username FROM users WHERE id=?', (run['user_id'],))
    previous = json.loads(run['state']).get('business', {})
    engine = None
    started = time.monotonic()
    try:
        h.check_cancel(run_id)
        port = ApplicationPort(db, h.settings, user, json.loads(run['base_ids']), run_id)
        scope = hashlib.sha256(json.dumps([user, port.base_ids], sort_keys=True).encode()).hexdigest()[:20]
        workspace = h.settings.root/'harness/native_application'/run['conversation_id']/scope
        model = ChatOpenAI(model=h.settings.llm_model, base_url=h.settings.llm_url,
            api_key=h.settings.llm_key or 'local', temperature=0, max_tokens=h.settings.output_tokens,
            timeout=h.settings.timeout, max_retries=0,
            extra_body={'chat_template_kwargs': {'enable_thinking': False}})
        engine = NativeHarness(model=model, port=port, workspace=workspace,
            namespace=run['conversation_id'], max_calls=h.settings.harness_model_calls,
            max_tools=h.settings.harness_tool_calls, context_budget=ContextBudget(h.settings),
            cancelled=lambda: h.check_cancel(run_id), shared_context=True, fork_subagents=True,
            supervisor_read_tools={'list_my_tickets', 'get_my_ticket'}, bind_user_request=True,
            completion_validator=port.validate_completion,
            event=lambda kind, value: db.event(run_id, kind, value))
        db.event(run_id, 'native_application_started', {'runtime': 'deepagents.create_deep_agent',
            'fork_subagents': True, 'request_bound_handoffs': True})
        answer = engine.invoke(run['question'], request_id=run_id)
        records = engine.journal.records()
        # Rehydrate every source through the current user's actual ACLs, even
        # when invoke reused a completed answer after a transport failure.
        for effect in records:
            for evidence in effect['result'].get('evidence', []) if isinstance(effect['result'], dict) else []:
                current = port.retriever.chunk(user, evidence['id'], port.base_ids)
                if current['text'] != evidence['text']:
                    raise ValueError('Source changed; request fresh evidence')
                port.sources[evidence['source']] = current
        state = _state(h, run, user, port, engine, previous, started)
        citations = list(dict.fromkeys(re.findall(r'\[(S\d+)\]', answer)))
        current = [r for r in records if r['request_id'] == run_id]
        unresolved = [r for r in records if r['status'] == 'unknown']
        completed = (any(r['status'] in ('observed', 'committed') for r in current)
                     or bool(port.current_ticket_states or port.current_unavailable_ticket_ids))
        status = ('partial_success' if completed else 'insufficient') if unresolved else 'answered' if citations or current or completed else 'clarify'
        state['business']['resumable'] = False
        h.finish(run, state, answer, citations, status)
    except BaseException as exc:
        from kb.agent import Cancelled
        status = 'cancelled' if isinstance(exc, Cancelled) else 'error'
        if engine is not None:
            state = _state(h, run, user, port, engine, previous, started)
            state['business']['resumable'] = status == 'error' and not isinstance(exc, (NativeBudgetExceeded, ValueError, PermissionError))
        else:
            state = {'policy': POLICY, 'business': {'resumable': False}}
        h.checkpoint(run_id, state)
        db.execute('UPDATE runs SET status=?,error=?,updated_at=? WHERE id=?',
                   (status, type(exc).__name__, time.time(), run_id))
        db.event(run_id, 'native_application_error', {'kind': type(exc).__name__, 'message': str(exc)[:500]})
        if not isinstance(exc, Exception):
            raise
    finally:
        if engine is not None:
            engine.close()


def _state(h, run, user, port, engine, previous, started):
    records = engine.journal.records()
    current = [r for r in records if r['request_id'] == run['id']]
    receipts = list(port.receipts)
    observed_tasks={}
    if port.current_ticket_states or port.current_unavailable_ticket_ids:
        # This is an actual read-only application DB observation, not a tool
        # call invented for the transcript. Keep its provenance in the finish
        # contract even when the model answers directly from supplied context.
        observation_id='current-state-'+run['id']
        receipts.append({'action':'current_ticket_state','task_id':observation_id,
            'source':'authenticated_ticket_database','observed_at':port.current_state_observed_at,
            'tickets':port.current_ticket_states,'unavailable_ticket_ids':port.current_unavailable_ticket_ids,
            'verified':True})
        observed_tasks[observation_id]={'kind':'database_observation','result':{'status':'answered'}}
    # The persisted journal survives a worker/process restart; in-memory port
    # receipts alone cannot establish that a draft has already been created.
    for effect in current:
        value = effect['result']
        if effect['name'] == 'prepare_ticket_draft' and effect['status'] == 'committed':
            row = h.db.one('SELECT * FROM service_tickets WHERE id=? AND user_id=?',
                          (value['id'], user['id']))
            if row:
                receipts.append({'action': 'draft', 'ticket_id': row['id'], 'title': row['title'],
                    'source_run': run['id'], 'task_id': effect['id'], 'verified': True,
                    'description_sha256': hashlib.sha256(row['description'].encode()).hexdigest()})
        elif effect['status'] in ('observed', 'committed'):
            receipts.append({'action': effect['name'], 'evidence_id': effect['id']})
    usage = previous.get('usage', {})
    unmetered = max(0, engine.turn_calls-previous.get('calls', 0)-engine.calls)
    missing_usage = usage.get('missing_usage_calls', 0)+engine.missing_usage+unmetered
    return {'policy': POLICY, 'sources': port.sources, 'user': user, 'base_ids': port.base_ids,
        'input_contract': json.loads(run['state']).get('input_contract', {}),
        'business': {'runtime': 'deepagents.create_deep_agent', 'workspace': str(engine.root),
            'calls': engine.turn_calls, 'tools': engine.turn_tools,
            'max_calls': engine.max_calls, 'max_tools': engine.max_tools,
            'receipts': receipts, 'resumable': False,
            'current_ticket_states':port.current_ticket_states,
            'unavailable_ticket_ids':port.current_unavailable_ticket_ids,
            'current_state_observed_at':port.current_state_observed_at,
            'deferred_user_actions': [f"在服务工单页面确认提交工单 {item['ticket_id']}。"
                                     for item in port.user_handoffs(current)],
            'remaining_work': [r['id'] for r in records if r['status'] == 'unknown'],
            'completed_tasks': {**{r['id']: {'result': {'status': 'answered'}} for r in current
                if r['status'] in ('observed', 'committed')},**observed_tasks},
            'usage': {'prompt_tokens': usage.get('prompt_tokens', 0)+engine.prompt_tokens,
                'completion_tokens': usage.get('completion_tokens', 0)+engine.completion_tokens,
                'missing_usage_calls': missing_usage, 'usage_complete': missing_usage == 0,
                'seconds': usage.get('seconds', 0)+time.monotonic()-started}}}
