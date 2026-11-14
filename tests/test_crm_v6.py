"""Small CPU integrations with real LangChain/LangGraph and canonical ITSM tools."""
import json

import pytest
from langchain_core.messages import AIMessage

from kb.benchmark.crm_adapter_v6 import ExternalBusinessQA
from kb.backends.business_v6_control import OrderedPlan, compile_plan
from kb.backends.business_multi_v2 import source_spans
from kb.context import ContextBudget
from test_crm_benchmark import RecordedModel, adapter, reply


@pytest.fixture(autouse=True)
def local_tokens(monkeypatch):
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))


def setup_engine(tmp_path, monkeypatch, replies, question, arm='single_v6'):
    model = RecordedModel(replies=replies)
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs: model)
    owner = adapter(tmp_path, arm)
    return owner, ExternalBusinessQA(owner, question, [], 0), model


def final(answer='Done.', resolution='complete', actions=()):
    return reply('ExternalResult', {'answer': answer, 'resolution': resolution,
                                   'completed_action_ids': list(actions)}, 'final-'+str(len(answer)))


def test_service_first_order_and_receipt_reach_next_specialist(tmp_path, monkeypatch):
    question = 'First, add the note "Please prioritize" to TCK-1001; second, tell me whether the desk is open.'
    owner, engine, model = setup_engine(tmp_path, monkeypatch, [
        reply('SpanRoute', {'tasks': [
            {'domain': 'service', 'span_ids': ['U0']},
            {'domain': 'technical', 'span_ids': ['U1'], 'depends_on': ['U0']}]}, 'plan'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Please prioritize'}, 'note'),
        final('Added your note.', actions=['note']),
        reply('get_business_hours', {}, 'hours'), final('The desk is open.'),
    ], question, 'multi_v6')
    result = engine.answer()
    assert result['status'] == 'answered'
    assert [c['name'] for c in owner.tool_calls] == ['add_ticket_comment', 'get_business_hours']
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Please prioritize']
    hours_input = next(messages for messages in model.seen if any(
        'completed_tasks' in str(m.content) and '"id": "note"' in str(m.content) for m in messages))
    assert '"id": "note"' in str(hours_input[-1].content)
    assert all('expected_actions' not in str(m) and 'expected_order' not in str(m) for m in model.seen)
    assert engine.answer() == result and owner.model_calls == 5


def test_dependency_cycle_rejected_and_independent_order_preserved():
    spans = source_spans('First task; second task.')
    plan = OrderedPlan(tasks=[{'domain': 'service', 'span_ids': ['U0']},
                              {'domain': 'technical', 'span_ids': ['U1']}])
    tasks = compile_plan(plan, spans)
    assert [t['domain'] for t in tasks] == ['service', 'technical']
    plan.tasks[0].depends_on = ['U1']
    plan.tasks[1].depends_on = ['U0']
    with pytest.raises(ValueError, match='cycle'):
        compile_plan(plan, spans)


def test_duplicate_read_batch_is_audited_then_only_finish_is_offered(tmp_path, monkeypatch):
    batch = AIMessage(content='', tool_calls=[
        {'name': 'get_business_hours', 'args': {}, 'id': f'h{i}'} for i in range(3)],
        usage_metadata={'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15})
    owner, engine, _ = setup_engine(tmp_path, monkeypatch,
        [batch, final('The desk is open.')], 'Is the desk open?')
    assert engine.answer()['status'] == 'answered'
    assert len(owner.tool_calls) == 3 and engine.state['tools'] == 3
    assert sum(c['backend_executed'] for c in owner.tool_calls) == 1
    assert sum(c['cache_hit'] for c in owner.tool_calls) == 2
    events = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    last_request = [e for e in events if e['kind'] == 'model_request'][-1]['data']
    assert [tool['function']['name'] for tool in last_request['tools']] == ['ExternalResult']
    assert 'reject_domain' not in str(last_request['tools'][0]['function']['parameters'])
    assert owner.usage_missing_calls == 0 and owner.model_calls == 2


def test_write_invalidates_read_and_batch_order_is_deterministic(tmp_path, monkeypatch):
    batch = AIMessage(content='', tool_calls=[
        {'name': 'get_ticket', 'args': {'ticket_id': 'TCK-1001'}, 'id': 'before'},
        {'name': 'add_ticket_comment', 'args': {'ticket_id': 'TCK-1001', 'comment': 'New evidence'}, 'id': 'write'},
        {'name': 'get_ticket', 'args': {'ticket_id': 'TCK-1001'}, 'id': 'after'},
    ], usage_metadata={'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15})
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [batch, final('Note added.', actions=['write'])],
        'Look up TCK-1001, add "New evidence", then read the updated ticket.')
    assert engine.answer()['status'] == 'answered'
    assert [c['execution_index'] for c in owner.tool_calls] == [0, 1, 2]
    assert all(c['backend_executed'] for c in owner.tool_calls)
    assert json.loads(owner.tool_calls[0]['result'])['comments'] == []
    assert json.loads(owner.tool_calls[2]['result'])['comments'] == ['New evidence']


def test_invented_permission_identity_is_rejected_before_backend(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('check_permission', {'user_id': 'user123', 'action': 'delete', 'ticket_id': 'TCK-1001'}, 'bad'),
        final('Which user identity should I check?', 'needs_user_input'),
    ], 'Can you delete TCK-1001?')
    assert engine.answer()['status'] == 'clarify'
    assert len(owner.tool_calls) == 1 and not owner.tool_calls[0]['backend_executed']
    assert not owner.tool_calls[0]['ok']
    assert owner.environment.get_state()['session']['delete_authorizations'] == {}


def test_guarded_delete_uses_exact_ticket_preconditions_in_batch_order(tmp_path, monkeypatch):
    calls = [
        {'name': 'check_dependencies', 'args': {'ticket_id': 'TCK-1001'}, 'id': 'dep'},
        {'name': 'check_permission', 'args': {'user_id': 'admin.rlee', 'action': 'delete', 'ticket_id': 'TCK-1001'}, 'id': 'perm'},
        {'name': 'delete_ticket', 'args': {'ticket_id': 'TCK-1001'}, 'id': 'delete'},
    ]
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        AIMessage(content='', tool_calls=calls, usage_metadata={'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15}),
        final('TCK-1001 deleted.', actions=['delete']),
    ], 'Check dependencies and permission, then delete TCK-1001. My user ID is admin.rlee.')
    assert engine.answer()['status'] == 'answered'
    assert 'TCK-1001' not in owner.environment.get_state()['tickets']
    assert all(c['ok'] for c in owner.tool_calls)


def test_later_permission_denial_cannot_reuse_another_identitys_grant(tmp_path, monkeypatch):
    calls = [
        {'name': 'check_dependencies', 'args': {'ticket_id': 'TCK-1001'}, 'id': 'dep'},
        {'name': 'check_permission', 'args': {'user_id': 'admin.rlee', 'action': 'delete', 'ticket_id': 'TCK-1001'}, 'id': 'admin'},
        {'name': 'check_permission', 'args': {'user_id': 'agent.jsmith', 'action': 'delete', 'ticket_id': 'TCK-1001'}, 'id': 'member'},
        {'name': 'delete_ticket', 'args': {'ticket_id': 'TCK-1001'}, 'id': 'delete'},
    ]
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        AIMessage(content='', tool_calls=calls, usage_metadata={'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15}),
        final('agent.jsmith has no delete permission.', 'insufficient'),
    ], 'Check admin.rlee, then agent.jsmith, and attempt deletion of TCK-1001 using agent.jsmith.')
    assert engine.answer()['status'] == 'insufficient'
    assert 'TCK-1001' in owner.environment.get_state()['tickets']
    assert not owner.tool_calls[-1]['backend_executed'] and not owner.tool_calls[-1]['ok']


@pytest.mark.parametrize('exhaust', [False, True])
def test_handoffs_use_distinct_domains_with_one_shared_budget(tmp_path, monkeypatch, exhaust):
    replies = [reply('SpanRoute', {'tasks': [{'domain': 'technical', 'span_ids': ['U0']}]}, 'plan')]
    replies += [reply('ExternalResult', {'answer': 'Domain mismatch', 'resolution': 'reject_domain',
                                        'suggested_domain': suggested}, f'reject{i}')
                for i, suggested in enumerate(['policy', 'technical'])]
    replies += ([reply('ExternalResult', {'answer': 'Domain mismatch', 'resolution': 'reject_domain'}, 'reject2')]
                if exhaust else [reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'read'), final('TCK-1001 is Open.')])
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, replies, 'Look up TCK-1001.', 'multi_v6')
    result = engine.answer()
    control = next(iter(engine.state['task_control'].values()))
    assert control['attempted_domains'] == ['technical', 'policy', 'service']
    assert control['reassignments'] == 2
    assert engine.state['calls'] == owner.model_calls == (4 if exhaust else 5)
    assert result['status'] == ('clarify' if exhaust else 'answered')
    assert len(owner.tool_calls) == (0 if exhaust else 1)


def test_handoff_after_write_is_blocked_and_no_write_replayed(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['U0']}]}, 'plan'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once only'}, 'write'),
        reply('ExternalResult', {'answer': 'Try another domain', 'resolution': 'reject_domain',
                                 'suggested_domain': 'technical'}, 'bad-transfer'),
    ], 'Add "Once only" to TCK-1001.', 'multi_v6')
    result = engine.answer()
    assert result['status'] == 'answered' and 'Once only' in result['answer']
    control = next(iter(engine.state['task_control'].values()))
    assert control['reassignments'] == 0 and control['write_started']
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once only']


def test_unfinished_dependency_blocks_only_its_dependents(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('SpanRoute', {'tasks': [
            {'domain': 'service', 'span_ids': ['U0']},
            {'domain': 'technical', 'span_ids': ['U1'], 'depends_on': ['U0']}]}, 'plan'),
        final('Please specify the comment.', 'needs_user_input'),
    ], 'Add a comment to TCK-1001; after that, tell me whether the desk is open.', 'multi_v6')
    result = engine.answer()
    assert result['status'] == 'clarify'
    assert not owner.tool_calls and owner.model_calls == 2
