"""Regressions for observed ITSM routing, side-effect, and answer failures."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from kb.context import ContextBudget
from test_crm_benchmark import reply
from test_crm_v6 import final, setup_engine


@pytest.fixture(autouse=True)
def local_tokens(monkeypatch):
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))


def batch(*calls):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': ident}
                                           for name, args, ident in calls],
                     usage_metadata={'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15})


def test_advisory_context_errors_do_not_drop_the_current_request(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['U0'],
                                      'context_ids': ['TCK-1001', 'U0']}]}, 'route'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'read'), final('TCK-1001 is Open.'),
    ], 'Look up TCK-1001.', 'multi_v6')
    assert engine.answer()['status'] == 'answered'
    assert owner.model_calls == 3 and owner.tool_calls[0]['backend_executed']
    events = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    plan = next(e['data'] for e in events if e['kind'] == 'external_route')
    assert plan['dropped_context_ids'] == ['TCK-1001', 'U0']
    assert plan['tasks'][0]['context_ids'] == []


def test_invalid_primary_ids_use_bounded_generalist_fallback(tmp_path, monkeypatch):
    invalid = {'tasks': [{'domain': 'service', 'span_ids': ['H0_0']}]}
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('SpanRoute', invalid, 'bad1'), reply('SpanRoute', invalid, 'bad2'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'read'), final('TCK-1001 is Open.'),
    ], 'Look up TCK-1001.', 'multi_v6')
    assert engine.answer()['status'] == 'answered'
    assert owner.model_calls == 4 and len(owner.tool_calls) == 1
    assert 'external_route_fallback' in (tmp_path/'events.jsonl').read_text()


def test_plan_tool_coverage_repairs_wrong_specialist(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['U0']}]}, 'route'),
        reply('get_business_hours', {}, 'hours'), final('The desk is open.'),
    ], 'What are the service desk hours?', 'multi_v6')
    result = engine.answer()
    assert result['status'] == 'answered' and 'America/Los_Angeles' in result['answer']
    assert engine.state['plan'][0]['domain'] == 'technical'
    assert owner.tool_calls[0]['backend_executed']


def test_ambiguous_target_cannot_be_authorized_by_listing_tickets(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('list_tickets', {}, 'list'),
        reply('close_ticket', {'ticket_id': 'TCK-1001'}, 'guess'),
        final('Which ticket?', 'needs_user_input'),
    ], 'Can you close out my ticket?')
    before = owner.environment.get_state()
    result = engine.answer()
    assert result['status'] == 'clarify'
    assert owner.environment.get_state() == before
    assert not owner.tool_calls[-1]['backend_executed']
    assert json.loads(owner.tool_calls[-1]['result'])['error'] == 'ticket_identity_required'


def test_unique_description_is_resolved_from_actual_listing(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('list_tickets', {}, 'list'),
        reply('check_dependencies', {'ticket_id': 'TCK-1001'}, 'deps'),
        reply('close_ticket', {'ticket_id': 'TCK-1001'}, 'close'),
        final('Closed.', actions=['close']),
    ], 'Close the VPN ticket.')
    result = engine.answer()
    assert result['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['status'] == 'Closed'


def test_duplicate_writes_return_existing_receipt_without_another_append(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Keep once'}, 'write'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Keep once'}, 'again'),
        final('Done.', actions=['add_ticket_comment']),
    ], 'Add the comment "Keep once" to TCK-1001.')
    result = engine.answer()
    assert result['status'] == 'answered' and 'Keep once' in result['answer']
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Keep once']
    assert [c['backend_executed'] for c in owner.tool_calls] == [True, False]
    assert owner.tool_calls[1]['replay_of'] == 'write'
    assert [c['id'] for c in next(iter(engine.state['completed_tasks'].values()))['result']['receipts']] == ['write']


def test_distinct_user_steps_can_intentionally_repeat_a_comment(tmp_path, monkeypatch):
    question = 'First, add "Repeat" to TCK-1001; second, add "Repeat" to TCK-1001.'
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['U0']},
                                     {'domain': 'service', 'span_ids': ['U1'], 'depends_on': ['U0']}]}, 'plan'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Repeat'}, 'one'), final(actions=['one']),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Repeat'}, 'two'), final(actions=['two']),
    ], question, 'multi_v6')
    assert engine.answer()['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Repeat', 'Repeat']


def test_business_rejection_is_not_a_committed_receipt(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('check_dependencies', {'ticket_id': 'TCK-1003'}, 'deps'),
        reply('close_ticket', {'ticket_id': 'TCK-1003'}, 'close'),
        final('Closed successfully.', actions=['close']),
    ], 'Close TCK-1003.')
    result = engine.answer()
    assert result['status'] == 'insufficient' and 'already Closed' in result['answer']
    write = owner.tool_calls[-1]
    assert write['ok'] and write['business_outcome'] == 'rejected'
    assert not next(iter(engine.state['completed_tasks'].values()))['result']['receipts']


def test_mixed_final_and_business_batch_is_repaired_before_execution(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        batch(('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'One'}, 'premature'),
              ('ExternalResult', {'answer': 'Done', 'resolution': 'complete'}, 'early-final')),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'One'}, 'write'),
        final(actions=['write']),
    ], 'Add the comment "One" to TCK-1001.')
    assert engine.answer()['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['One']
    assert owner.model_calls == 3 and engine.state['tools'] == 2
    assert [c.get('backend_executed', False) for c in owner.tool_calls] == [False, True]


def test_permission_alias_is_normalized_and_rendered_from_actual_result(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('check_permission', {'user_id': 'agent.jsmith', 'action': 'escalate_ticket'}, 'permission'),
        final('This user has no permission.'),
    ], 'Check only whether agent.jsmith is permitted to escalate tickets.')
    result = engine.answer()
    assert result['status'] == 'answered' and 'is permitted to escalate' in result['answer']
    assert owner.tool_calls[0]['arguments']['action'] == 'escalate'
    assert owner.tool_calls[0]['requested_arguments']['action'] == 'escalate_ticket'


def test_permission_question_does_not_authorize_a_write(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('escalate_ticket', {'ticket_id': 'TCK-1001'}, 'write'),
        final('No change was requested.', 'needs_user_input'),
    ], 'Is agent.jsmith allowed to escalate TCK-1001? Check permission only.')
    before = owner.environment.get_state()
    engine.answer()
    assert owner.environment.get_state() == before
    assert not owner.tool_calls[0]['backend_executed']


def test_confirmed_closure_can_proceed_with_open_dependencies(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('check_dependencies', {'ticket_id': 'TCK-1002'}, 'deps'),
        reply('close_ticket', {'ticket_id': 'TCK-1002'}, 'close'), final('I refuse to close it.'),
    ], "Close TCK-1002. I'm aware of the dependencies, please close it anyway.")
    result = engine.answer()
    assert result['status'] == 'answered' and 'open dependencies' in result['answer']
    assert owner.environment.get_state()['tickets']['TCK-1002']['status'] == 'Closed'


def test_closure_intent_uses_canonical_close_operation(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('check_dependencies', {'ticket_id': 'TCK-1004'}, 'deps'),
        reply('update_ticket_status', {'ticket_id': 'TCK-1004', 'status': 'Closed'}, 'update'), final(),
    ], 'Close TCK-1004.')
    assert engine.answer()['status'] == 'answered'
    assert owner.tool_calls[-1]['name'] == 'close_ticket'
    assert owner.tool_calls[-1]['requested_name'] == 'update_ticket_status'


def test_explicit_status_edit_keeps_its_requested_operation(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('update_ticket_status', {'ticket_id': 'TCK-1004', 'status': 'Closed'}, 'update'), final(),
    ], 'Set the status of TCK-1004 to Closed.')
    assert engine.answer()['status'] == 'answered'
    assert owner.tool_calls[-1]['name'] == 'update_ticket_status'


def test_successful_deletion_overrides_a_hallucinated_refusal(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('check_dependencies', {'ticket_id': 'TCK-1004'}, 'deps'),
        reply('check_permission', {'user_id': 'admin.rlee', 'action': 'delete', 'ticket_id': 'TCK-1004'}, 'permission'),
        reply('delete_ticket', {'ticket_id': 'TCK-1004'}, 'delete'),
        final('A dependency prevented deletion.', 'insufficient', actions=['delete_ticket']),
    ], 'Delete TCK-1004. My user ID is admin.rlee.')
    result = engine.answer()
    assert result['status'] == 'answered' and result['answer'] == 'Ticket TCK-1004 was deleted.\nadmin.rlee is permitted to delete tickets.'
    assert 'TCK-1004' not in owner.environment.get_state()['tickets']


def test_out_of_scope_reply_offers_supported_business_work_without_model(tmp_path, monkeypatch):
    owner, engine, model = setup_engine(tmp_path, monkeypatch, [], 'What is the capital of Japan?', 'multi_v6')
    result = engine.answer()
    assert result['status'] == 'answered' and 'IT service tickets' in result['answer']
    assert not model.seen and not owner.tool_calls


def test_user_question_is_not_echoed_as_an_assistant_claim(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'read'), final('Ticket TCK-1001 is Open.'),
    ], 'Please look up ticket TCK-1001.')
    result = engine.answer()
    assert 'TCK-1001' in result['answer'] and 'Open' in result['answer']
    assert 'Please look up' not in result['answer']


def test_identical_retry_in_next_turn_reuses_verified_write_receipt(tmp_path, monkeypatch):
    owner, _, _ = setup_engine(tmp_path, monkeypatch, [
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'write'), final(),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'retry'), final(),
    ], 'Add the comment "Once" to TCK-1001.')
    async def run():
        first = await owner.run('Add the comment "Once" to TCK-1001.', [])
        second = await owner.run('Add the comment "Once" to TCK-1001.', [])
        return first, second
    first, second = asyncio.run(run())
    assert first.content == second.content
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']
    assert not owner.tool_calls[-1]['backend_executed']


def test_explicit_again_in_new_turn_performs_another_requested_write(tmp_path, monkeypatch):
    question = 'Add the comment "Repeat" to TCK-1001 again.'
    owner, _, _ = setup_engine(tmp_path, monkeypatch, [
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Repeat'}, 'one'), final(),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Repeat'}, 'two'), final(),
    ], question)
    async def run():
        await owner.run(question, [])
        await owner.run(question, [])
    asyncio.run(run())
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Repeat', 'Repeat']


def test_single_agent_can_perform_two_explicit_identical_steps(tmp_path, monkeypatch):
    question = 'First, add "Repeat" to TCK-1001, then add "Repeat" to TCK-1001.'
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Repeat'}, 'one'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Repeat'}, 'two'), final(),
    ], question)
    assert engine.answer()['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Repeat', 'Repeat']


def test_partial_write_does_not_claim_all_explicit_targets_completed(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('update_ticket_status', {'ticket_id': 'TCK-1001', 'status': 'Resolved'}, 'one'), final(),
    ], 'Set the status of TCK-1001 and TCK-1004 to Resolved.')
    result = engine.answer()
    assert result['status'] == 'insufficient'
    assert 'remaining requested changes' in result['answer']
    assert owner.environment.get_state()['tickets']['TCK-1004']['status'] == 'Open'


def test_clarification_carries_original_operation_to_unique_description(tmp_path, monkeypatch):
    from harness.models import ChatMessage, Role
    question = 'Can you close my ticket?'
    owner, _, _ = setup_engine(tmp_path, monkeypatch, [
        final('Which ticket?', 'needs_user_input'),
        reply('list_tickets', {}, 'list'),
        reply('check_dependencies', {'ticket_id': 'TCK-1001'}, 'deps'),
        reply('close_ticket', {'ticket_id': 'TCK-1001'}, 'close'), final(),
    ], question)
    async def run():
        initial = await owner.run(question, [])
        history = [ChatMessage(role=Role.USER, content=question),
                   ChatMessage(role=Role.ASSISTANT, content=initial.content)]
        return await owner.run('The VPN one.', history)
    result = asyncio.run(run())
    assert 'TCK-1001 is now Closed' in result.content
    assert owner.environment.get_state()['tickets']['TCK-1001']['status'] == 'Closed'


def test_dependent_step_uses_the_unique_ticket_from_its_predecessor_receipt(tmp_path, monkeypatch):
    question = 'First, add "Ready" to TCK-1001; second, set its status to Resolved.'
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['U0']},
                                     {'domain': 'service', 'span_ids': ['U1'], 'depends_on': ['U0']}]}, 'plan'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Ready'}, 'note'), final(),
        reply('update_ticket_status', {'ticket_id': 'TCK-1001', 'status': 'Resolved'}, 'status'), final(),
    ], question, 'multi_v6')
    result = engine.answer()
    assert result['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['status'] == 'Resolved'


def test_unknown_write_result_is_not_blindly_reexecuted(tmp_path, monkeypatch):
    from types import SimpleNamespace
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'write'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'retry'), final(),
    ], 'Add the comment "Once" to TCK-1001.')
    actual = owner.registry.execute_with_status
    async def lost_acknowledgement(name, arguments):
        await actual(name, arguments)
        return SimpleNamespace(result='{"error":"write result lost"}', ok=False)
    monkeypatch.setattr(owner.registry, 'execute_with_status', lost_acknowledgement)
    result = engine.answer()
    assert result['status'] == 'insufficient'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']
    assert not owner.tool_calls[-1]['backend_executed']
    assert not next(iter(engine.state['completed_tasks'].values()))['result']['receipts']


def test_repeated_final_format_errors_finish_from_the_already_committed_write(tmp_path, monkeypatch):
    duplicate = lambda ident: batch(
        ('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, ident),
        ('ExternalResult', {'answer': 'Done', 'resolution': 'complete'}, 'final-'+ident))
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'write'),
        duplicate('mixed1'), duplicate('mixed2'),
    ], 'Add the comment "Once" to TCK-1001.')
    assert engine.answer()['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']


def test_unrelated_yes_reply_does_not_reactivate_pending_write(tmp_path, monkeypatch):
    from harness.models import ChatMessage, Role
    question = 'Can you close my ticket?'
    owner, _, model = setup_engine(tmp_path, monkeypatch, [final('Which ticket?', 'needs_user_input')], question)
    async def run():
        first = await owner.run(question, [])
        history = [ChatMessage(role=Role.USER, content=question),
                   ChatMessage(role=Role.ASSISTANT, content=first.content)]
        return await owner.run('Yes, tell me about the weather.', history)
    result = asyncio.run(run())
    assert 'IT service tickets' in result.content and len(model.seen) == 1
    assert not owner.tool_calls


def test_actual_chat_transport_exposes_the_receipt_in_tool_message_content(tmp_path, monkeypatch):
    from langchain_openai import ChatOpenAI
    from langchain_openai._compat import httpx
    from kb.benchmark.crm_adapter_v6 import ExternalBusinessQA
    from test_crm_benchmark import adapter
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            name, args = 'add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}
        else:
            name, args = 'ExternalResult', {'answer': 'Done', 'resolution': 'complete',
                                            'completed_action_ids': ['provider-1']}
        return httpx.Response(200, json={'id': 'chat-test', 'object': 'chat.completion',
            'created': 1, 'model': 'test', 'choices': [{'index': 0, 'finish_reason': 'tool_calls',
                'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'provider-'+str(len(captured)), 'type': 'function',
                     'function': {'name': name, 'arguments': json.dumps(args)}}]}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr('kb.benchmark.crm_adapter.ChatOpenAI', lambda **kwargs:
                        ChatOpenAI(**kwargs, http_client=client, http_async_client=async_client))
    owner = adapter(tmp_path, 'single_v6')
    try:
        result = ExternalBusinessQA(owner, 'Add the comment "Once" to TCK-1001.', [], 0).answer()
        assert result['status'] == 'answered'
        tool_message = next(m for m in captured[1]['messages'] if m['role'] == 'tool')
        observation = json.loads(tool_message['content'])
        assert observation['receipt_id'] == 'provider-1'
        assert observation['business_outcome'] == 'committed'
        assert owner.model_calls == 2 and owner.usage_missing_calls == 0
    finally:
        client.close()
        asyncio.run(async_client.aclose())


def test_quoted_note_contents_are_not_routed_as_another_request(tmp_path, monkeypatch):
    comment = 'Permission reviewed; then check business hours.'
    question = f'Add the comment "{comment}" to TCK-1001.'
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('SpanRoute', {'tasks': [{'domain': 'service', 'span_ids': ['U0']}]}, 'route'),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': comment}, 'note'), final(),
    ], question, 'multi_v6')
    assert engine.answer()['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == [comment]
    assert [c['name'] for c in owner.tool_calls] == ['add_ticket_comment']


def test_finished_write_keeps_an_unfinished_requested_lookup_visible(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Ready'}, 'note'), final(),
    ], 'Add the comment "Ready" to TCK-1001, then tell me the business hours.')
    result = engine.answer()
    assert result['status'] == 'insufficient'
    assert 'remaining requested' in result['answer']


def test_background_dependency_id_does_not_become_a_write_target(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('close_ticket', {'ticket_id': 'TCK-1002'}, 'wrong'),
        reply('check_dependencies', {'ticket_id': 'TCK-1001'}, 'deps'),
        reply('close_ticket', {'ticket_id': 'TCK-1001'}, 'right'), final(),
    ], 'Close TCK-1001, but leave dependency ticket TCK-1002 unchanged.')
    before = owner.environment.get_state()['tickets']['TCK-1002']
    result = engine.answer()
    assert result['status'] == 'answered'
    assert not owner.tool_calls[0]['backend_executed']
    assert owner.environment.get_state()['tickets']['TCK-1002'] == before


def test_negative_instruction_does_not_authorize_its_ticket(tmp_path, monkeypatch):
    owner, engine, _ = setup_engine(tmp_path, monkeypatch, [
        reply('close_ticket', {'ticket_id': 'TCK-1001'}, 'wrong'),
        reply('check_dependencies', {'ticket_id': 'TCK-1004'}, 'deps'),
        reply('close_ticket', {'ticket_id': 'TCK-1004'}, 'right'), final(),
    ], "Don't close TCK-1001, close TCK-1004.")
    assert engine.answer()['status'] == 'answered'
    assert owner.environment.get_state()['tickets']['TCK-1001']['status'] == 'Open'


def test_unknown_write_receipt_survives_an_identical_next_turn_retry(tmp_path, monkeypatch):
    from types import SimpleNamespace
    question = 'Add the comment "Once" to TCK-1001.'
    owner, _, _ = setup_engine(tmp_path, monkeypatch, [
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'write'), final(),
        reply('add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}, 'retry'), final(),
    ], question)
    actual = owner.registry.execute_with_status
    async def lost_acknowledgement(name, arguments):
        await actual(name, arguments)
        return SimpleNamespace(result='{"error":"write acknowledgement lost"}', ok=False)
    monkeypatch.setattr(owner.registry, 'execute_with_status', lost_acknowledgement)
    async def run():
        await owner.run(question, [])
        return await owner.run(question, [])
    response = asyncio.run(run())
    assert 'acknowledgement lost' in response.content
    assert owner.environment.get_state()['tickets']['TCK-1001']['comments'] == ['Once']
