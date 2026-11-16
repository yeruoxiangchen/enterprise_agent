"""ITSM tool names, request preconditions, and factual write outcomes.

These rules consume visible user text and recorded tool responses. They do
not depend on benchmark task IDs, expected actions, or expected answers.
"""
from __future__ import annotations

import json
import re

READ_TOOLS = {'get_ticket', 'list_tickets', 'get_business_hours', 'check_dependencies', 'check_permission'}
WRITE_TOOLS = {'assign_ticket', 'reassign_ticket', 'close_ticket', 'escalate_ticket',
               'update_ticket_status', 'add_ticket_comment', 'delete_ticket'}
PERMISSION_ACTIONS = ('view', 'edit', 'assign', 'delete', 'escalate')
PERMISSION_ALIASES = {
    'get_ticket': 'view', 'list_tickets': 'view', 'update_ticket_status': 'edit',
    'add_ticket_comment': 'edit', 'close_ticket': 'edit', 'assign_ticket': 'assign',
    'reassign_ticket': 'assign', 'delete_ticket': 'delete', 'escalate_ticket': 'escalate',
}
TICKET_ID = re.compile(r'(?<![\w-])TCK-[A-Za-z0-9]+(?![\w-])', re.I)
SCOPE_REPLY = ('I can help look up and manage IT service tickets, check service desk hours, '
               'and check ticket dependencies or account permissions. Which would you like help with?')
SCOPE_REPLY_ZH = '我可以查询和办理 IT 工单、查询服务台营业时间，以及核对工单依赖和账户权限。你想办理哪一项？'

_PERMISSION_QUERY = re.compile(
    r'\b(?:allowed|permitted|permission|authorized)\b|\b(?:can|may|should) (?:i|we|he|she|they)\b'
    r'|\bhow (?:do|can|should) (?:i|we)\b|权限|是否允许', re.I)
_HOURS_QUERY = re.compile(
    r'\b(?:business hours|opening hours|service hours|desk hours|office hours|time zone|timezone)\b'
    r'|\b(?:desk|office)\b[^.?!;]{0,45}\b(?:open|closed)\b|\bwhen\b[^.?!;]{0,55}\b(?:open|respond|response|hear back)\b'
    r'|营业时间|工作时间|时区', re.I)
_STATUS_UPDATE = re.compile(r'\b(?:set|update|change|mark|move)\b[^.?!;]{0,100}\bstatus\b|(?:设置|更新|修改)状态', re.I)
_WRITE_PATTERNS = {
    'close_ticket': r'\bclose\b|\b(?:want|need)\s+(?:ticket\s+)?TCK-[A-Za-z0-9]+[^.?!;]{0,65}\bclosed\b|关闭',
    'delete_ticket': r'\b(?:delete|purge|remove|deletion)\b|\b(?:want|need)\b[^.?!;]{0,90}\b(?:deleted|removed)\b|删除',
    'assign_ticket': r'\bassign\b|\b(?:get|have|want|need)\b[^.?!;]{0,100}\bassigned\b|分配',
    'reassign_ticket': r'\breassign\b|\b(?:get|have|want|need)\b[^.?!;]{0,100}\breassigned\b|\bmove\b[^.?!;]{0,65}\bfrom\b[^.?!;]{0,55}\bto\b|转派|重新分配',
    'escalate_ticket': r'\bescalate\b|\braise\b[^.?!;]{0,55}\bpriority\b|升级',
    'update_ticket_status': _STATUS_UPDATE.pattern,
    'add_ticket_comment': r'\b(?:add|append|leave|record|log|post)\b[^.?!;]{0,100}\b(?:note|comment|remark)\b'
                          r'|\b(?:needs?|requires?)\s+(?:the\s+|a\s+)?(?:note|comment)\b'
                          r'|\b(?:add|append|leave|log|post)\s+["“]|留言|添加备注',
}
_WRITE_PATTERNS = {name: re.compile(pattern, re.I) for name, pattern in _WRITE_PATTERNS.items()}
_NEGATION = re.compile(r"\b(?:do not|don't|never|without)\s+(?:(?:ever|please)\s+)?$|(?:不要|不得|无需)$", re.I)
_EXPLICIT_COMMAND = re.compile(r'\b(?:please|then)\s+(?:close|delete|remove|assign|reassign|escalate|update|add)\b', re.I)
_SUBJECT_STOPWORDS = set(('the and for from with this that ticket tickets issue request open closed resolved status '
                         'please need want one my your can could would you close out delete update assign').split())
_STEP_BOUNDARY = re.compile(
    r'(?=\b(?:then|after that)\b)|[,;\n]+\s*(?=\b(?:first|second|then|after that)\b)'
    r'|(?<!\bfirst)(?<!\bsecond)(?<!\bthen)(?<!\bafter that)[,;\n]+\s*(?=\b(?:add|append|close|delete|remove|assign|reassign|escalate|update|set|check|tell|leave|keep)\b)'
    r'|\band\s+(?=\b(?:add|append|close|delete|remove|assign|reassign|escalate|update|set|check|confirm|verify|look|show|tell|leave|keep|do not|don\'t)\b)'
    r'|[\n]+|[.!?]\s+(?=(?:please\s+)?(?:confirm|verify|check|look|show)\b)'
    r'|[,;]\s*(?=(?:please\s+)?(?:confirm|verify|look|show)\b)'
    r'|(?=\b(?:but|while|without|except|leaving)\b)'
    r'|(?=\bwith\s+(?:(?:an?|open)\s+)*dependenc)', re.I)
QUOTED_TEXT = re.compile(
    r'"(?:\\.|[^"\\\n])*"|“[^”\n]*”|'
    r"(?<!\w)'(?:\\.|[^'\\\n]|(?<=\w)'(?=\w))*'(?!\w)")


def mask_quoted(text, fill=' '):
    return QUOTED_TEXT.sub(lambda match: fill * len(match.group()), text)


def split_explicit_steps(text):
    """Return source offsets for explicit coordinators outside quoted data."""
    masked = mask_quoted(text)
    edges = [0, *(m.start() for m in _STEP_BOUNDARY.finditer(masked)), len(text)]
    result = []
    for start, end in zip(edges, edges[1:]):
        while start < end and (text[start].isspace() or text[start] in ',;.!?'):
            start += 1
        while end > start and (text[end-1].isspace() or text[end-1] in ',;'):
            end -= 1
        if start < end:
            result.append((start, end))
    return result


def ticket_ids(text):
    return {match.group().upper() for match in TICKET_ID.finditer(text)}


def write_ticket_ids(text, operation):
    def keep_identifier(match):
        value = match.group()[1:-1].strip()
        return match.group() if operation != 'add_ticket_comment' and TICKET_ID.fullmatch(value) else ''
    return ticket_ids(QUOTED_TEXT.sub(keep_identifier, text))


def unique_context_ticket(text, operation):
    """Allow one user-provided lookup target to carry into a following action."""
    candidates = set()
    for start, end in split_explicit_steps(text):
        clause = text[start:end]
        if re.match(r"\s*(?:(?:but|and)\s+)?(?:leave|keep|do not|don't|never|without|except|while|with\b.*dependenc)", clause, re.I):
            continue
        candidates.update(write_ticket_ids(clause, operation))
    return next(iter(candidates)) if len(candidates) == 1 else None


def requested_writes(text):
    """Recognize direct requests for a canonical operation, including ordered clauses."""
    actions = set()
    for start, end in split_explicit_steps(text):
        clause = text[start:end]
        # Quoted comments are data. Preserve the opening quote for the add pattern.
        clean = QUOTED_TEXT.sub('""', clause)
        if re.match(r'\s*(?:then\s+)?(?:please\s+)?(?:confirm|verify|check)\b', clean, re.I) and not _EXPLICIT_COMMAND.search(clean):
            # Asking whether a change completed does not authorize another one.
            continue
        if _PERMISSION_QUERY.search(clean) and not _EXPLICIT_COMMAND.search(clean):
            continue
        for name, pattern in _WRITE_PATTERNS.items():
            if name == 'close_ticket' and _HOURS_QUERY.search(clause) and not re.search(r'ticket|工单', clause, re.I):
                continue
            if any(not _NEGATION.search(clean[:m.start()]) for m in pattern.finditer(clean)):
                actions.add(name)
    if 'update_ticket_status' in actions and _STATUS_UPDATE.search(text):
        actions.discard('close_ticket')
    if 'reassign_ticket' in actions and not re.search(r'\bassign\b', text, re.I):
        actions.discard('assign_ticket')
    return actions


def requested_write_targets(text):
    targets = set()
    for start, end in split_explicit_steps(text):
        clause = text[start:end]
        targets.update((action, ticket) for action in requested_writes(clause)
                       for ticket in write_ticket_ids(clause, action))
    return targets


def write_allowance(name, arguments, text):
    """Distinct explicitly requested occurrences may have identical arguments."""
    count = 0
    for start, end in split_explicit_steps(text):
        clause = text[start:end]
        if name not in requested_writes(clause):
            continue
        identifiers = write_ticket_ids(clause, name)
        if identifiers and arguments.get('ticket_id') not in identifiers:
            continue
        if name == 'add_ticket_comment':
            values = [m.group()[1:-1] for m in QUOTED_TEXT.finditer(clause)]
            if values and arguments.get('comment') not in values:
                continue
        count += 1
    return max(count, 1)


def tool_hints(text):
    hints = requested_writes(text)
    clean = mask_quoted(text)
    if _HOURS_QUERY.search(clean):
        hints.add('get_business_hours')
    if _PERMISSION_QUERY.search(clean) or 'delete_ticket' in hints:
        hints.add('check_permission')
    if hints.intersection({'close_ticket', 'delete_ticket'}) or re.search(r'\bcheck\b[^.?!;]{0,60}\bdependencies\b', clean, re.I):
        hints.add('check_dependencies')
    if not hints.intersection(WRITE_TOOLS) and ticket_ids(text) and re.search(
            r'\b(?:status|priority|details|look up|lookup|assigned|assignee|owner|ownership|subject|confirm|verify)\b|查询|负责人|复查', clean, re.I):
        hints.add('get_ticket')
    if hints.intersection(WRITE_TOOLS) and re.search(
            r'\b(?:confirm|verify|look\s+(?:it\s+)?up|lookup|read\s*back|check\s+(?:the\s+)?(?:new|updated|current))\b|复查|核实', clean, re.I):
        hints.add('get_ticket')
    if re.search(r'\blook\b[^.?!;]{0,35}\bback up\b|\bconfirm who\b|重新查询', clean, re.I):
        hints.add('get_ticket')
    return hints


def is_service_request(text):
    return bool(tool_hints(text) or re.search(
        r'\b(?:ticket|tickets|ITSM|service desk|support|status|priority|assignee|owner|ownership|dependency|dependencies|permission|permitted|authorized)\b'
        r'|工单|服务台|依赖|权限|负责人', text, re.I))


def is_continuation(text):
    if re.fullmatch(r'\s*(?:yes|i confirm|go ahead|proceed|确认|是的)[\s,.!。！]*', text, re.I):
        return True
    if requested_writes(text) and re.match(r'\s*(?:yes\b|i confirm\b|proceed\b|确认)', text, re.I):
        return True
    return bool(re.match(
        r"\s*(?:(?:i am|i'm)\s+(?:the\s+)?[\w-]+[.@][\w.@-]+\b|my (?:user )?id\b|"
        r"the .{1,80} one\s*[.!]?\s*$|TCK-[A-Za-z0-9]+\s*[.!]?\s*$|工单号)", text, re.I))


def selected_ticket(text, records):
    """A description must identify one ticket in an observed complete listing."""
    text = mask_quoted(text)
    terms = set(re.findall(r'[a-z0-9]{3,}', text.lower())) - _SUBJECT_STOPWORDS
    unassigned = bool(re.search(
        r'\bunassigned\b|\bno (?:current )?(?:owner|assignee)\b'
        r'|\b(?:nobody|no one)\b[^.?!;]{0,45}\b(?:picked up|owns|handling|assigned)\b'
        r'|\b(?:not|has not been|hasn\'t been)\s+(?:currently\s+)?assigned\b', text, re.I))
    candidates = set()
    for record in records:
        if unassigned and record.get('assigned_to'):
            continue
        subject = set(re.findall(r'[a-z0-9]{3,}', record.get('subject', '').lower())) - _SUBJECT_STOPWORDS
        if terms.intersection(subject) and record.get('ticket_id'):
            candidates.add(record['ticket_id'])
    return next(iter(candidates)) if len(candidates) == 1 else None


def canonical_call(name, arguments, actions):
    arguments = dict(arguments)
    if name in {'assign_ticket', 'reassign_ticket'}:
        aliases = ('new_owner', 'new_assignee', 'assigned_to', 'to_user')
        supplied = {arguments[key] for key in aliases if isinstance(arguments.get(key), str)}
        if len(supplied) == 1 and ('assignee' not in arguments or arguments['assignee'] in supplied):
            arguments.setdefault('assignee', next(iter(supplied)))
            for key in aliases:
                arguments.pop(key, None)
    ticket = arguments.get('ticket_id')
    if isinstance(ticket, str) and TICKET_ID.fullmatch(ticket):
        arguments['ticket_id'] = ticket.upper()
    if name == 'check_permission' and isinstance(arguments.get('action'), str):
        action = arguments['action'].strip().lower()
        arguments['action'] = PERMISSION_ALIASES.get(action, action)
    if (name == 'update_ticket_status' and str(arguments.get('status', '')).lower() == 'closed'
            and 'close_ticket' in actions and 'update_ticket_status' not in actions):
        return 'close_ticket', {'ticket_id': arguments.get('ticket_id')}
    return name, arguments


def payload(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def write_outcome(name, arguments, raw, ok):
    value = payload(raw)
    if not ok or not isinstance(value, dict):
        return 'unknown'
    if value.get('error') or value.get('success') is False:
        return 'rejected'
    if value.get('ticket_id') != arguments.get('ticket_id'):
        return 'unknown'
    if name == 'delete_ticket' and value.get('status') != 'deleted':
        return 'unknown'
    field = {'close_ticket': 'status', 'update_ticket_status': 'status', 'assign_ticket': 'assigned_to',
             'reassign_ticket': 'assigned_to', 'escalate_ticket': 'priority'}.get(name)
    if field and not isinstance(value.get(field), str):
        return 'unknown'
    return 'committed'


def write_observation(record):
    value = payload(record['result'])
    outcome = record['business_outcome']
    return json.dumps({'tool': record['name'], 'business_outcome': outcome,
                       'receipt_id': record['id'] if outcome == 'committed' else None,
                       'result': value if value is not None else record['result']}, ensure_ascii=False)


def receipt_still_applies(record, state):
    """A retry may reuse a receipt only while its recorded effect still holds."""
    name, args = record['name'], record['arguments']
    ticket = state.get('tickets', {}).get(args['ticket_id'])
    if name == 'delete_ticket':
        return ticket is None
    if not ticket:
        return False
    previous = payload(record['result']) or {}
    field = {'close_ticket': 'status', 'update_ticket_status': 'status', 'assign_ticket': 'assigned_to',
             'reassign_ticket': 'assigned_to', 'escalate_ticket': 'priority'}.get(name)
    if field:
        return ticket.get(field) == previous.get(field)
    comment = args.get('comment')
    return (name == 'add_ticket_comment' and comment is not None
            and ticket.get('comments', []).count(comment) >= previous.get('comments', []).count(comment) > 0)


def explicit_repeat(text):
    clean = mask_quoted(text)
    return bool(re.search(r'\b(?:again|another|one more|repeat)\b|再一次|另外一条', clean, re.I))


def receipt_answer(receipt, observations):
    name, args = receipt['name'], receipt['arguments']
    result = payload(receipt['result'])
    ticket = args['ticket_id']
    if name == 'delete_ticket':
        return f'Ticket {ticket} was deleted.'
    if name in ('close_ticket', 'update_ticket_status'):
        warning = ''
        if name == 'close_ticket':
            for item in observations:
                if item['name'] == 'check_dependencies' and item['arguments'].get('ticket_id') == ticket:
                    value = payload(item['result']) or {}
                    if value.get('open_dependencies'):
                        warning = 'The ticket still has open dependencies. '
        return warning + f'Ticket {ticket} is now {result["status"]}.'
    if name in ('assign_ticket', 'reassign_ticket'):
        return f'Ticket {ticket} is now assigned to {result["assigned_to"]}.'
    if name == 'escalate_ticket':
        return f'Ticket {ticket} was escalated to {result["priority"]} priority.'
    return f'Added your comment to ticket {ticket}: {args["comment"]}'


def read_fact(record):
    value = payload(record['result'])
    if not isinstance(value, dict) or value.get('error'):
        return None
    if record['name'] == 'get_business_hours' and {'open', 'close', 'timezone', 'currently_open'} <= value.keys():
        state = 'open' if value['currently_open'] else 'closed'
        return (f'The service desk is currently {state}. Business hours are '
                f'{value["open"]}–{value["close"]} ({value["timezone"]}).')
    if record['name'] == 'check_permission' and isinstance(value.get('allowed'), bool):
        decision = 'permitted' if value['allowed'] else 'not permitted'
        return f'{value["user_id"]} is {decision} to {value["action"]} tickets.'
    if record['name'] == 'get_ticket' and value.get('ticket_id'):
        if value.get('found') is False:
            return f'Ticket {value["ticket_id"]} was not found.'
        fields = [f'{key.replace("_", " ")} {value[key]}' for key in ('subject', 'status', 'priority', 'assigned_to')
                  if key in value and value[key]]
        if 'assigned_to' in value and not value['assigned_to']:
            fields.append('currently unassigned (no current owner)')
        return f'Ticket {value["ticket_id"]}: '+', '.join(fields)+'.' if fields else None
    return None
