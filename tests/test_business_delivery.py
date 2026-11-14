import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from business_assessment_cases import CASES
from business_delivery_contracts import CONTRACTS, checks, summarize, fair_contracts


def row(question='问题', answer='回答', key='single/cancel-pending/1'):
    return {'key': key, 'question': question, 'answer': answer, 'run_id': 'one', 'calls': 1, 'elapsed': 1,
            'observation': {'usage': {'complete': True, 'prompt_tokens': 5, 'completion_tokens': 3}},
            'quality': {'execution': True, 'permission_checks': True, 'no_auto_submission': True},
            'business_state_ok': True, 'status': 'answered', 'actual_tickets': []}


def test_all_frozen_requests_have_contracts_and_missing_are_unknown():
    assert len(CONTRACTS) == 24
    out = summarize(CASES, ['single', 'multi_v5'], [])
    assert out['arms']['multi_v5']['turn_contracts']['unknown'] == 24
    assert out['arms']['multi_v5']['conversation_contracts']['unknown'] == 12
    assert out['assessment_complete'] is False


def test_echoing_question_does_not_satisfy_answer_requirement():
    r = row(question='借用14天吗？', answer='综合助手 · 借用14天吗？\n请澄清具体需求。')
    c = checks(r, CONTRACTS['cancel-pending/1'])
    assert c['answer:' + r'(?:14|十四)\s*天'] is False
    r['answer'] += '\n设备最多借用14天。'
    assert all(checks(r, CONTRACTS['cancel-pending/1']).values())


def test_missing_actual_draft_fields_cannot_be_replaced_by_answer_text():
    r = row(answer='Windows SSH Permission denied 检查网络 DEMO-172 今天下午')
    r['actual_tickets'] = [{'title': 'Windows SSH故障', 'description': 'Windows SSH Permission denied 检查网络'}]
    c = checks(r, CONTRACTS['consult-then-draft/1'])
    assert c['draft:DEMO-172'] is False and c['draft:今天下午'] is False


def test_duplicate_records_rejected():
    r = row()
    with pytest.raises(ValueError, match='Duplicate'):
        summarize(CASES, ['single'], [r, r])


def test_fair_wording_review_accepts_equivalent_negation_and_neutral_title():
    fair = fair_contracts()
    r = row()
    r['actual_tickets'] = [{'title': 'HTTPS故障', 'description': 'Ubuntu HTTPS 401 尚未检查令牌权限，未清理缓存。'}]
    assert all(checks(r, fair['negative-attempt/0']).values())
    r['actual_tickets'] = [{'title': 'HTTPS 401故障', 'description': 'HTTPS 401；实际使用的是Ubuntu系统。'}]
    assert all(checks(r, fair['corrected-environment/2']).values())
    r['actual_tickets'][0]['title'] = 'Windows HTTPS 401故障'
    assert not checks(r, fair['corrected-environment/2'])['title:absent:Windows']
    assert CONTRACTS['corrected-environment/2']['title_literals'] == ['Ubuntu']
