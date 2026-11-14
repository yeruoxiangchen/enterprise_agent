"""Explicit acceptance contracts for the original 12 development conversations.

The checks below are observable acceptance requirements, not general semantic
accuracy. They are applied identically to all archived and new arms.
"""
from copy import deepcopy
import re

from business_repair_v5 import CONTRACTS as REPAIR_CONTRACTS, checks as repair_checks
from business_assessment_metrics import counts, conjunction, quantiles

CONTRACTS = deepcopy(REPAIR_CONTRACTS)
CONTRACTS.update({
    'two-domain-consult/0': {'answer_patterns': [
        '公钥', r'(?:不要|不能|不应|禁止|勿).{0,16}私钥|私钥.{0,12}(?:不要|不能|不应|禁止)',
        r'(?:14|十四)\s*天', '资产(?:编号|号|ID)|设备编号', '用途', '归还', '资产管理员']},
    'corrected-environment/0': {'answer_patterns': ['令牌.*过期', '读(?:取)?权限']},
    'switch-topic/0': {'answer_patterns': [r'(?:14|十四)\s*天']},
    'switch-topic/1': {'answer_patterns': ['600|六百']},
    'switch-topic/2': {'answer_patterns': [r'(?:15|十五)\s*天', '发票', '主管']},
    'renewal-followup/0': {'answer_patterns': [r'(?:7|七)\s*天']},
    'renewal-followup/1': {'answer_patterns': [r'(?:2|两|二)\s*个?工作日', '项目负责人']},
    'renewal-followup/2': {'answer_patterns': [
        r'(?:不|并非|未).{0,12}(?:批准|获批|授权)|(?:批准|获批).{0,8}(?:另行|需要)']},
    'partial-evidence/0': {'answer_patterns': [
        '键盘', '300|三百', r'显示器.{0,35}(?:未|没有|不足|不明确|不适用)']},
    'partial-evidence/1': {'answer_patterns': [
        r'(?:不能|不适用).{0,24}显示器|显示器.{0,24}(?:不适用|未规定|未明确)']},
    'account-isolation/0': {},  # Actual own-user ACL and service receipts below.
    'account-isolation/1': {},
    'unknown-real-setting/0': {'answer_patterns': ['代理', '配置', '来源']},
    'unknown-real-setting/1': {'answer_patterns': [
        r'(?:未|没有|无法|不能|不足).{0,32}(?:真实|生产|公司|地址)|(?:真实|生产|公司).{0,24}(?:未提供|未规定|未知)']},
})


def checks(row, contract):
    if row is None:
        return {'recorded': None}
    # Single-Agent reports repeat the user's question as their heading. Matching
    # that heading would reward merely echoing a requested number or entity.
    row = dict(row)
    lines = (row.get('answer') or '').splitlines()
    if lines and row.get('question') and row['question'] in lines[0]:
        lines.pop(0)
    row['answer'] = '\n'.join(lines)
    result = repair_checks(row, contract)
    body = '\n'.join(t['description'] for t in row['actual_tickets'])
    for pattern in contract.get('draft_patterns', []):
        result['draft_pattern:'+pattern] = bool(row['actual_tickets']) and bool(re.search(pattern, body, re.I | re.S))
    return result


def fair_contracts():
    """Second-pass wording review; original frozen contracts remain preserved."""
    contracts = deepcopy(CONTRACTS)
    negative = contracts['negative-attempt/0']
    negative['draft_literals'].remove('没有清理缓存')
    negative['draft_patterns'] = [r'(?:没有|未|尚未|未曾).{0,3}清理.{0,3}缓存']
    correction = contracts['corrected-environment/2']
    del correction['title_literals']
    # A neutral fault title plus an explicit current environment in the body is
    # sufficient. A title claiming Windows conflicts with the latest Ubuntu.
    correction['draft_patterns'] = [r'(?:当前|实际(?:使用)?|更正|最新).{0,18}Ubuntu']
    return contracts


def summarize(cases, arms, records, contracts=None):
    contracts = CONTRACTS if contracts is None else contracts
    lookup = {r['key']: r for r in records}
    if len(lookup) != len(records):
        raise ValueError('Duplicate archived request keys')
    result = {'scope': '12 authored development conversations; deterministic business/state/field/fact contracts',
              'judge_used': False, 'arms': {}, 'details': {}, 'paired_conversations': {}}
    outcomes = {}
    for arm in arms:
        turns, conversations, details, observed = [], [], [], []
        for case in cases:
            values = []
            for i in range(len(case['turns'])):
                key = f'{arm}/{case["id"]}/{i}'
                contract = contracts[f'{case["id"]}/{i}']
                row = lookup.get(key)
                checked = checks(row, contract)
                score = conjunction(checked.values())
                values.append(score)
                turns.append(score)
                if row is not None:
                    observed.append(row)
                details.append({'key': key, 'run_id': row['run_id'] if row else None,
                                'passed': score, 'checks': checked})
            conversations.append(conjunction(values))
        outcomes[arm] = dict(zip((c['id'] for c in cases), conversations))
        usage = [r['observation']['usage'] for r in observed]
        tokens_complete = len(observed) == len(turns) and all(u.get('complete') for u in usage)
        result['arms'][arm] = {
            'conversation_contracts': counts(conversations), 'turn_contracts': counts(turns),
            'execution': counts(lookup[k]['quality']['execution'] if k in lookup else None
                               for k in [f'{arm}/{c["id"]}/{i}' for c in cases for i in range(len(c['turns']))]),
            'recorded_turns': len(observed), 'model_calls': sum(r['calls'] for r in observed),
            'tokens_complete': tokens_complete,
            'prompt_tokens': sum(u.get('prompt_tokens', 0) for u in usage) if tokens_complete else None,
            'completion_tokens': sum(u.get('completion_tokens', 0) for u in usage) if tokens_complete else None,
            'observed_latency': quantiles(r['elapsed'] for r in observed),
        }
        result['details'][arm] = details
    for reference in ('single', 'multi_v4'):
        if reference not in arms or 'multi_v5' not in arms:
            continue
        pairs = {k: [] for k in ('win', 'loss', 'both_pass', 'both_fail', 'unknown')}
        for case in cases:
            a, b = outcomes[reference][case['id']], outcomes['multi_v5'][case['id']]
            bucket = 'unknown' if a is None or b is None else (
                'both_pass' if a == b == 1 else 'both_fail' if a == b == 0 else 'win' if b > a else 'loss')
            pairs[bucket].append(case['id'])
        result['paired_conversations'][reference] = {k: {'count': len(v), 'cases': v} for k, v in pairs.items()}
    result['assessment_complete'] = all(s['turn_contracts']['unknown'] == 0 for s in result['arms'].values())
    return result
