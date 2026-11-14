"""Fixed-denominator business outcomes; unknown judgments remain explicit."""
import math
import statistics


def conjunction(values):
    values = list(values)
    if any(v is False or type(v) is int and v == 0 for v in values):
        return 0
    if not values or any(v is None for v in values):
        return None
    return 1


def counts(values):
    values = list(values)
    passed = sum(v is True or type(v) is int and v == 1 for v in values)
    failed = sum(v is False or type(v) is int and v == 0 for v in values)
    return {'passed': passed, 'failed': failed, 'unknown': len(values)-passed-failed,
            'expected': len(values), 'pass_fraction_of_expected': passed/len(values) if values else None}


def quantiles(values):
    values = sorted(values)
    return {'observations': len(values), 'p50_seconds': statistics.median(values) if values else None,
            'p95_seconds': values[math.ceil(.95*len(values))-1] if values else None}


def turn_success(row, grade):
    if row is None:
        return None
    return conjunction([row['quality']['execution'], row['quality']['permission_checks'],
                        row['quality']['no_auto_submission'], row['business_state_ok'],
                        grade.get('correctness')])


def summarize(cases, arms, rows, grades):
    records = {r['key']: r for r in rows}
    if len(records) != len(rows):
        raise ValueError('Duplicate evaluation records')
    scores = {r['key']: r for r in grades}
    result = {'arms': {}, 'paired_conversation_outcomes': {}}
    outcomes = {}
    for arm in arms:
        expected = [f'{arm}/{c["id"]}/{i}' for c in cases for i in range(len(c['turns']))]
        observed = [records[k] for k in expected if k in records]
        success = {c['id']: conjunction(turn_success(records.get(f'{arm}/{c["id"]}/{i}'),
                       scores.get(f'{arm}/{c["id"]}/{i}', {})) for i in range(len(c['turns']))) for c in cases}
        outcomes[arm] = success
        context = [scores.get(f'{arm}/{c["id"]}/{i}', {}).get('context_retention')
                   for c in cases for i, t in enumerate(c['turns']) if t.get('context_rubric')]
        calls = sum(r['calls'] for r in observed)
        completed = sum(v == 1 for v in success.values())
        usage = [r.get('observation', {}).get('usage', {}) for r in observed]
        usage_complete = len(observed) == len(expected) and all(u.get('complete') for u in usage)
        result['arms'][arm] = {
            'expected_turns': len(expected), 'recorded_turns': len(observed),
            'execution': counts(records[k]['quality']['execution'] if k in records else None for k in expected),
            'conversation_task_success': counts(success.values()),
            'context_retention_model_review': counts(context),
            'business_state': counts(records[k]['business_state_ok'] if k in records else None for k in expected),
            'categories': {cat: counts(success[c['id']] for c in cases if c['category'] == cat)
                           for cat in sorted({c['category'] for c in cases})},
            'model_calls_all_attempts': calls,
            'calls_per_successful_conversation': calls/completed if completed else None,
            'response_latency': quantiles(r['elapsed'] for r in observed),
            'token_usage_complete': usage_complete,
            'prompt_tokens': sum(u.get('prompt_tokens', 0) for u in usage) if usage_complete else None,
            'completion_tokens': sum(u.get('completion_tokens', 0) for u in usage) if usage_complete else None,
        }
    if 'single' in outcomes and 'multi_v4' in outcomes:
        pairs = {'win': [], 'loss': [], 'both_pass': [], 'both_fail': [], 'unknown': []}
        for c in cases:
            a, b = outcomes['single'][c['id']], outcomes['multi_v4'][c['id']]
            key = 'unknown' if a is None or b is None else (
                'both_pass' if a == b == 1 else 'both_fail' if a == b == 0 else 'win' if b > a else 'loss')
            pairs[key].append(c['id'])
        result['paired_conversation_outcomes'] = {k: {'count': len(v), 'cases': v} for k, v in pairs.items()}
    result['semantic_assessment_complete'] = all(
        f'{arm}/{c["id"]}/{i}' in records and
        scores.get(f'{arm}/{c["id"]}/{i}', {}).get('correctness') in (0, 1) and
        (not t.get('context_rubric') or scores.get(f'{arm}/{c["id"]}/{i}', {}).get('context_retention') in (0, 1))
        for arm in arms for c in cases for i, t in enumerate(c['turns']))
    return result
