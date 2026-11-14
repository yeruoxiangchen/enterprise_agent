"""Conversation evidence and business terminal states, without task annotations."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter

from kb.backends.business_v6_control import ReadProgress
from .crm_contract import (QUOTED_TEXT, TICKET_ID, is_continuation,
    mask_quoted, payload, requested_writes, requested_write_targets, split_explicit_steps,
    write_ticket_ids)


def data_version(state):
    # Read preconditions update session bookkeeping, not the business records.
    # Cache hits additionally verify that required session proofs still exist.
    business = {key: value for key, value in state.items() if key != 'session'}
    return hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()


class ConversationReads(ReadProgress):
    def __init__(self, engine):
        super().__init__(engine.state, ttl=float('inf'))
        self.engine = engine

    def get(self, task_id, name, arguments, version):
        record = super().get(task_id, name, arguments, version)
        if not record:
            return None
        value = payload(record['result'])
        if not isinstance(value, dict) or value.get('error'):
            return None
        session = self.engine.owner.environment.get_state().get('session', {})
        ticket = arguments.get('ticket_id')
        if name == 'check_dependencies':
            expected = [item['ticket_id'] for item in value.get('open_dependencies', [])]
            if session.get('dependency_checks', {}).get(ticket) != {'open_dependencies': expected}:
                return None
        if name == 'check_permission' and arguments.get('action') == 'delete' and ticket and value.get('allowed'):
            if session.get('delete_authorizations', {}).get(ticket) != arguments.get('user_id'):
                return None
        return record

    def put(self, task_id, name, arguments, version, result):
        super().put(task_id, name, arguments, version, result)
        self.state['read_observations'][self.key(task_id, name, arguments, version)].update(
            task_id=task_id, name=name, arguments=copy.deepcopy(arguments), version=version,
            source_call_id=self.engine.current_call_id)

    def observations(self):
        task_id = self.engine.active_task['id']
        version = self.engine.environment_version()
        result = []
        for record in list(self.state['read_observations'].values()):
            if record.get('task_id') != task_id or record.get('version') != version:
                continue
            current = self.get(task_id, record['name'], record['arguments'], version)
            if current:
                result.append({'name': record['name'], 'arguments': record['arguments'],
                               'result': record['result'], 'id': record.get('source_call_id'), 'ok': True})
        return result


def pending_context(pending, question, last_reply=''):
    if not pending:
        return None, False
    clean = mask_quoted(question)
    if requested_writes(question) and re.search(r'\b(?:another|one more)\b|另外一条', clean, re.I):
        return None, False
    if re.fullmatch(r'\s*(?:thanks|thank you|thank you very much|谢谢|好的谢谢)[\s,.!。！]*', clean, re.I):
        return None, False
    if re.search(r"\b(?:cancel|never mind|nevermind|forget it|don't do it|do not change|read.only)\b|取消|不用了|只查询", clean, re.I):
        return None, False
    before, current = requested_writes(pending), requested_writes(question)
    if current and not current <= before:
        return None, False
    old_quotes = {m.group()[1:-1] for m in QUOTED_TEXT.finditer(pending)}
    new_quotes = {m.group()[1:-1] for m in QUOTED_TEXT.finditer(question)}
    if 'add_ticket_comment' in current and new_quotes and not new_quotes <= old_quotes:
        return None, False
    old_ids = write_ticket_ids(pending, 'close_ticket')
    new_ids = write_ticket_ids(question, 'close_ticket')
    if old_ids and new_ids and not new_ids <= old_ids:
        correction = re.search(r'\b(?:actually|correction|i meant|instead)\b|更正|改为', clean, re.I)
        if correction and not current and len(old_ids) == len(new_ids) == 1:
            old, new = next(iter(old_ids)), next(iter(new_ids))
            masked = mask_quoted(pending)
            return TICKET_ID.sub(lambda m: new if m.group().upper() == old and
                masked[m.start():m.end()].strip() else m.group(), pending), True
        return None, False
    bare = re.fullmatch(r'[\w.@-]+[.!]?', question.strip())
    requested_field = re.search(r'\b(?:user.?id|identity|username|assignee|who|which ticket)\b|身份|工单号|指派给谁', last_reply, re.I)
    followup = re.search(r'\b(?:confirm|confirmation|complete|completed|finish|finished|proceed|continue|again|retry|still|done|reassigned|assigned|resolved|updated|authorization)\b|继续|确认|完成|重试', clean, re.I)
    field_reply = requested_field and (new_ids or re.search(
        r'\b(?:my (?:user\s*id|username|id)|(?:the )?(?:assignee|user\s*id|username) (?:is|:))\b', clean, re.I))
    if is_continuation(question) or (bare and (requested_field or re.search(r'[.@]', question))) or field_reply or current or followup:
        return pending, False
    return None, False


def action_counts(text, fallback):
    result = Counter()
    for start, end in split_explicit_steps(text):
        result.update(requested_writes(text[start:end]))
    for name in fallback:
        result[name] = max(result[name], 1)
    return result


def user_identities(text, session):
    clean = mask_quoted(text)
    values = {value.rstrip('.') for value in re.findall(r'(?<![\w.@-])[\w-]+(?:[.@][\w.@-]+)+', clean)}
    values.update(value.rstrip('.') for value in re.findall(
        r'\b(?:user\s*id|username|my\s+id)\s*(?:is|:|=)?\s*([\w.@-]+)', clean, re.I))
    if session.get('user_id'):
        values.add(session['user_id'])
    return values


def disclosure_request(text):
    clean = mask_quoted(text)
    return bool(re.search(
        r'\b(?:system|developer|internal|hidden)\s+(?:prompt|instructions?|rules?|configuration|messages?)\b'
        r'|\b(?:available\s+tools?|tool\s+(?:names|schemas|definitions)|function\s+(?:names|definitions))\b'
        r'|(?:系统|内部|隐藏)(?:提示词|指令|规则)|列出.*工具', clean, re.I))


def terminal_results(engine, targets):
    """Resolve a requested mutation from current real read evidence, never gold."""
    observations = engine.progress.observations()
    tickets = {r['arguments']['ticket_id']: payload(r['result']) for r in observations if r['name'] == 'get_ticket'}
    dependencies = {r['arguments']['ticket_id']: payload(r['result']) for r in observations if r['name'] == 'check_dependencies'}
    permissions = [payload(r['result']) for r in observations if r['name'] == 'check_permission']
    resolved = {}
    for name, ticket in targets:
        value = tickets.get(ticket) or {}
        if value.get('found') is False:
            resolved[(name,ticket)] = {'status': 'refused', 'answer': f'Ticket {ticket} was not found. No change was made.'}
        elif name == 'close_ticket' and value.get('status') == 'Closed':
            resolved[(name,ticket)] = {'status': 'already_satisfied', 'answer': f'Ticket {ticket} is already Closed. No additional closure was performed.'}
        elif name == 'reassign_ticket' and value.get('ticket_id') == ticket and not value.get('assigned_to'):
            resolved[(name,ticket)] = {'status': 'refused', 'answer': f'Ticket {ticket} is currently unassigned, so it cannot be reassigned. No reassignment was made.'}
        elif name == 'delete_ticket':
            deps = dependencies.get(ticket) or {}
            if deps.get('open_dependencies'):
                identifiers = ', '.join(d['ticket_id'] for d in deps['open_dependencies'])
                resolved[(name,ticket)] = {'status': 'refused', 'answer': f'Ticket {ticket} cannot be deleted because it has open dependencies: {identifiers}. No deletion was performed.'}
            else:
                denied = [v for v in permissions if v.get('ticket_id') == ticket and v.get('action') == 'delete' and v.get('allowed') is False]
                if denied:
                    who = denied[-1]['user_id']
                    resolved[(name,ticket)] = {'status': 'refused', 'answer': f'{who} does not have permission to delete ticket {ticket}. No deletion was performed.'}
    return resolved
