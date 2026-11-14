"""User-quoted service facts and finite routing; preserves the v3/v4 comparator."""
import re

from .business_v3_routing import (CREATE, LIST, NEGATIVE_CREATE, QUESTION, TECH, POLICY,
                                  classify_rule, ticket_user_sources)

ENVIRONMENT = re.compile(r'(?<![A-Za-z])(?:Ubuntu|Windows|Linux|macOS|Debian|CentOS)(?![A-Za-z])', re.I)
CORRECTION = re.compile(r'更正|改为|改成|实际(?:使用|是)|说错|写错')
FAULT = re.compile(r'故障|报错|蓝屏|死机|崩溃|坏了|损坏|无法|失败|超时|报\s*\d{3}|返回\s*\d{3}|提示|Permission denied', re.I)
ASSET = re.compile(r'(?:资产(?:编号|号)|设备编号)(?:是|为|[:：])?\s*([^\s，,。；;？?]+)')
CONTACT = re.compile(r'(?:可联系时间|联系时间)(?:是|为|[:：])?\s*([^，,。；;？?]+)')
OPERATION = re.compile(r'已(?:经)?|检查过|尝试过|做过|确认过|重启过|尚未|没有|没做|未做|还没')
NOT_DONE = re.compile(r'尚未|未(?:检查|做|清理|尝试)|没有|没做|还没|不确定')


def classify_span(text):
    if NEGATIVE_CREATE.search(text):
        # Negative write intent cannot erase a separate read-only question.
        remaining = NEGATIVE_CREATE.sub('', text)
        label = classify_rule(remaining)
        return label if label in ('technical', 'policy', 'ticket_list') else 'background'
    if CREATE.search(text):
        return 'ticket_create'
    if LIST.search(text):
        return classify_rule(text)
    if not QUESTION.search(text) and (CORRECTION.search(text) or ASSET.search(text) or CONTACT.search(text)):
        return 'background'
    return classify_rule(text)


def clauses(source):
    """Split at commas without losing original offsets or manufacturing user text."""
    text = source['text']
    boundaries = [0]+[m.end() for m in re.finditer(r'[，,；;]', text)]+[len(text)]
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        while start < end and text[start].isspace():
            start += 1
        while end > start and (text[end-1].isspace() or text[end-1] in '，,；;。'):
            end -= 1
        if start < end:
            yield {**source, 'id': f'{source["id"]}.{index}', 'start': source['start']+start,
                   'end': source['start']+end, 'text': text[start:end]}


def ticket_fields(question, history):
    sources = [part for s in ticket_user_sources(question, history) for part in clauses(s)]
    facts = []
    for source in sources:
        text = source['text']
        detail = bool(FAULT.search(text) or ASSET.search(text) or CONTACT.search(text) or
                      ENVIRONMENT.search(text) or CORRECTION.search(text) or OPERATION.search(text))
        if not detail and (CREATE.search(text) or LIST.search(text) or QUESTION.search(text)):
            continue
        if detail:
            facts.append(source)
    # A bare creation request or asset identifier does not specify a fault.
    if not any(FAULT.search(s['text']) for s in facts):
        return None
    current = {}
    for source in facts:
        for name, pattern in [('environment', ENVIRONMENT), ('asset_id', ASSET), ('contact_time', CONTACT)]:
            matches = list(pattern.finditer(source['text']))
            if not matches:
                continue
            match = matches[-1]
            value = match.group(0) if name == 'environment' else match.group(1).strip()
            previous = current.get(name)
            # An explicit correction outranks a later historical reference such
            # as “刚才的Windows电脑”; later explicit corrections may replace it.
            explicit = name != 'environment' or bool(CORRECTION.search(source['text']))
            if previous and previous.get('explicit_correction') and not explicit:
                continue
            current[name] = {'value': value, 'source': source, 'explicit_correction': explicit}
    attempts = [{**s, 'operation_status': 'not_done_or_uncertain' if NOT_DONE.search(s['text']) else 'user_reports_done'}
                for s in facts if OPERATION.search(s['text'])]
    return {'user_facts': facts, 'user_attempts': attempts, 'current_fields': current,
            'suggested_next_steps': ['待支持人员核对故障并给出下一步建议；此栏不表示用户已执行。']}


def ticket_title(fields):
    fault = next(s['text'] for s in reversed(fields['user_facts']) if FAULT.search(s['text']))
    environment = fields['current_fields'].get('environment', {}).get('value')
    if environment:
        fault = ENVIRONMENT.sub('', fault)
        return (environment+' · '+fault)[:100]
    return fault[:100]


def ticket_description(fields):
    lines = ['当前用户声明（未经核实；更正后的值优先）：']
    names = {'environment': '当前环境', 'asset_id': '资产编号', 'contact_time': '可联系时间'}
    for key, entry in fields['current_fields'].items():
        lines.append(f'{names[key]}：{entry["value"]} [{entry["source"]["id"]}]')
    lines += ['用户原始描述（按时间保留原话与更正）：']
    lines += [f'[{s["id"]}] {s["text"]}' for s in fields['user_facts']]
    lines += ['用户提及的操作状态（原话保留否定及不确定性）：']
    lines += [f'[{s["id"]}] {s["text"]}' for s in fields['user_attempts']] or ['未提供。']
    lines += ['建议下一步（尚未执行）：', *fields['suggested_next_steps']]
    result = '\n'.join(lines)
    if len(result) > 3000:
        raise ValueError('工单原文超过3000字，请缩小描述范围，未静默截断')
    return result
