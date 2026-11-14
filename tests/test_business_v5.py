from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest
from langchain_core.messages import AIMessage

from kb.backends.business_multi_v5 import BusinessQA
from kb.backends.business_v5_context import classify_span, ticket_fields, ticket_title, ticket_description
from test_business import engine, ScriptModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))


def obj(system, question, replies=(), history=()):
    parent, run = engine(system, {}, [], question)
    return BusinessQA(parent.db, replace(parent.cfg, agent_policy='business_multi_v5'), run, parent.user,
                      history, model=ScriptModel(plan={}, replies=list(replies)))


@pytest.mark.parametrize('question,domain', [
    ('先别生成工单，换个问题，演示制度里设备借用最多多久？', 'policy'),
    ('先告诉我演示制度要求报修提供哪些信息，不要生成工单。', 'policy'),
    ('不要生成工单，帮我排查SSH连接故障。', 'technical'),
    ('不要创建工单。', 'background'),
    ('更正一下，实际使用Ubuntu，刚才把系统说错了。', 'background'),
    ('请生成工单草稿，资产编号是DEMO-172，可联系时间是今天下午。', 'ticket_create'),
])
def test_negative_write_does_not_erase_consultation(question, domain):
    assert classify_span(question) == domain


def test_route_keeps_consultation_without_write_authority(system):
    question = '我的Windows电脑SSH连接提示Permission denied，已经检查网络。先告诉我演示制度要求报修提供哪些信息，不要生成工单。'
    x = obj(system, question)
    route = x.route({})
    assert [t['domain'] for t in route['tasks']] == ['policy']
    assert not route['clarification'] and x.state['calls'] == 0
    assert all('ticket_create' not in t['actions'] for t in route['tasks'])


def test_inline_creation_fields_keep_exact_user_offsets():
    question = '现在请把刚才的Windows电脑SSH故障生成工单草稿，资产编号是DEMO-172，可联系时间是今天下午。'
    history = [{'role': 'user', 'content': '我的Windows电脑SSH连接提示Permission denied，已经检查网络。'},
               {'role': 'assistant', 'content': '已经检查令牌，资产编号是FAKE-999。'}]
    fields = ticket_fields(question, history)
    assert fields['current_fields']['asset_id']['value'] == 'DEMO-172'
    assert fields['current_fields']['contact_time']['value'] == '今天下午'
    body = ticket_description(fields)
    assert 'DEMO-172' in body and '今天下午' in body and 'Permission denied' in body
    assert 'FAKE-999' not in body and '已经检查令牌' not in body
    originals = [question, history[0]['content']]
    assert all(any(text[s['start']:s['end']] == s['text'] for text in originals) for s in fields['user_facts'])


def test_latest_environment_is_explicit_without_rewriting_old_quotes():
    history = [{'role': 'user', 'content': '我的Windows电脑HTTPS拉取仓库返回401，应该先检查什么？'},
               {'role': 'assistant', 'content': '建议检查令牌。'},
               {'role': 'user', 'content': '更正一下，实际使用Ubuntu，刚才把系统说错了。'}]
    fields = ticket_fields('继续处理这个HTTPS报401的问题，按我刚才更正的信息生成工单草稿。', history)
    assert fields['current_fields']['environment']['value'] == 'Ubuntu'
    assert 'Ubuntu' in ticket_title(fields) and 'Windows' not in ticket_title(fields)
    body = ticket_description(fields)
    assert '当前环境：Ubuntu' in body and '我的Windows电脑' in body
    assert '建议检查令牌' not in body


def test_negative_operations_are_kept_as_user_quotes():
    fields = ticket_fields('我的Ubuntu电脑HTTPS拉取仓库报401，已经检查网络，但尚未检查令牌权限，也没有清理缓存。请生成工单草稿。', [])
    not_done = [s['text'] for s in fields['user_attempts'] if s['operation_status'] == 'not_done_or_uncertain']
    assert any('尚未检查令牌权限' in s for s in not_done)
    assert any('没有清理缓存' in s for s in not_done)
    assert ticket_fields('请生成工单草稿，资产编号是DEMO-172。', []) is None


def test_real_service_persists_inline_fields_and_reuses_completed_result(system):
    q = '我的Windows电脑SSH连接提示Permission denied，已经检查网络。请生成工单草稿，资产编号是DEMO-172，可联系时间是今天下午。'
    x = obj(system, q, [AIMessage(content='', tool_calls=[{'name': 'prepare_ticket', 'args': {}, 'id': 'one', 'type': 'tool_call'}]),
                        AIMessage(content='已经提交并解决。')])
    answer = x.answer()
    ticket = x.db.one('SELECT * FROM service_tickets WHERE source_run=?', (x.run['id'],))
    assert ticket['status'] == 'draft' and 'DEMO-172' in ticket['description'] and '今天下午' in ticket['description']
    assert '已经提交' not in answer['answer'] and '待确认' in answer['answer']
    calls = x.state['calls']
    assert x.answer() == answer and x.state['calls'] == calls


@pytest.mark.parametrize('fault', ['after_domain_checkpoint', 'after_draft_commit'])
def test_abrupt_process_exit_recovers_without_duplicate_work(tmp_path, monkeypatch, fault):
    import business_ops as old
    from business_assessment_faults import crash_case
    original = old.cfg
    monkeypatch.setattr(old, 'cfg', lambda root: replace(original(root), mode='extractive', reranker_path=''))
    result = crash_case(tmp_path/fault, fault, policy='business_multi_v5', scripted_success=True)
    assert result['first_exit'] == 86
    assert result['passed'], result
    assert result['assistant_messages'] == 1 and result['budget_retained']
    assert result['no_duplicate_completed_domain_or_draft']


def test_repair_contract_reads_actual_ticket_not_request_or_answer():
    from business_repair_v5 import checks, CONTRACTS
    row = {'quality': {'execution': True, 'permission_checks': True, 'no_auto_submission': True},
           'business_state_ok': True, 'status': 'answered',
           'answer': '资产编号DEMO-172，可联系时间今天下午，草稿已生成。',
           'actual_tickets': [{'title': 'SSH故障', 'description': 'Windows SSH Permission denied，检查网络。'}]}
    result = checks(row, CONTRACTS['consult-then-draft/1'])
    assert result['draft:DEMO-172'] is False and result['draft:今天下午'] is False
    row['actual_tickets'][0]['description'] += '资产编号DEMO-172，可联系时间今天下午。'
    assert all(checks(row, CONTRACTS['consult-then-draft/1']).values())


def test_gpu_queue_keeps_declared_memory_budget_and_headroom():
    from business_repair_v5 import capacity_candidate
    assert capacity_candidate([{'gpu': 0, 'total_mib': 24576, 'free_mib': 18571},
                               {'gpu': 6, 'total_mib': 24576, 'free_mib': 20091}]) is None
    assert capacity_candidate([{'gpu': 0, 'total_mib': 24576, 'free_mib': 21218},
                               {'gpu': 6, 'total_mib': 24576, 'free_mib': 23000}]) == 6
