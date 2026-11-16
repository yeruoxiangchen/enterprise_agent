"""Current ticket facts stay separate from durable historical tool receipts."""
import json
from pathlib import Path

import pytest
from langchain_core.messages import SystemMessage, ToolMessage

from kb.harness.journal import Journal
from kb.harness.ports import ApplicationPort
from test_application import login
from test_deep_harness import ScriptModel, call
from test_native_application import ask, configure
from test_native_harness import answer


def draft(system, monkeypatch, username='member', title='VPN连接故障'):
    cfg, db, client, base, *_ = system
    model = ScriptModel(replies=[
        call('task', {'subagent_type': 'service', 'description': '准备草稿。'}, 'delegate'),
        call('prepare_ticket_draft', {'title': title, 'description': 'Windows VPN无法连接。'}, 'draft'),
        answer('草稿已经保存。'), answer('请在工单页面确认提交。')])
    configure(cfg, monkeypatch, model)
    login(client, username)
    result = ask(client, db, cfg, base, '请准备VPN故障工单草稿，等待我确认。')
    assert result['status'] == 'answered', result
    ticket = db.one('SELECT * FROM service_tickets WHERE source_run=?', (result['id'],))
    state = json.loads(db.one('SELECT state FROM runs WHERE id=?', (result['id'],))['state'])
    return result, ticket, Path(state['business']['workspace'])


def resolve(system, ticket):
    _, _, client, *_ = system
    submitted = client.post('/api/tickets/' + ticket['id'] + '/action',
        json={'action': 'submit', 'version': 1})
    assert submitted.status_code == 200, submitted.text
    login(client, 'admin')
    assert client.post('/api/tickets/' + ticket['id'] + '/action',
        json={'action': 'accept', 'version': 2}).status_code == 200
    assert client.post('/api/tickets/' + ticket['id'] + '/action',
        json={'action': 'resolve', 'version': 3, 'note': '企业账号授权已恢复。'}).status_code == 200
    login(client)


def view(messages):
    system = next(m.content for m in messages if isinstance(m, SystemMessage))
    start = system.index('{"actual_business_receipts":')
    return json.JSONDecoder().raw_decode(system[start:])[0]['actual_business_receipts']


def test_no_ticket_conversation_keeps_the_existing_knowledge_context_shape(system, monkeypatch):
    cfg, db, client, base, *_ = system

    def inspect(messages):
        assert set(view(messages)) == {'committed', 'uncertain', 'recent_observations'}
        return answer('请补充您需要查询的资料主题。')

    configure(cfg, monkeypatch, ScriptModel(replies=[inspect]))
    login(client)
    result = ask(client, db, cfg, base, '想查一些资料。')
    assert result['status'] == 'clarify'
    assert not db.all('SELECT * FROM service_tickets')


@pytest.mark.parametrize('inspect_tool', [False, True])
def test_resolved_followup_receives_current_state_and_preserves_draft_history(
        system, monkeypatch, inspect_tool):
    cfg, db, client, base, *_ = system
    first, ticket, workspace = draft(system, monkeypatch)
    resolve(system, ticket)

    def check(messages):
        progress = view(messages)
        current, = progress['current_ticket_states']
        assert current['id'] == ticket['id'] and current['status'] == 'resolved'
        assert current['version'] == 4 and current['note'] == '企业账号授权已恢复。'
        historical = next(r for r in progress['recent_observations'] if r['tool'] == 'prepare_ticket_draft')
        assert historical['value']['status'] == 'draft'
        assert historical['state_changed_since_observation'] is True
        assert historical['recorded_at'] <= progress['current_state_observed_at']
        if inspect_tool:
            tool = next(m for m in reversed(messages) if isinstance(m, ToolMessage) and m.name == 'inspect_progress')
            assert json.loads(tool.content)['current_ticket_states'][0]['status'] == 'resolved'
        return answer('工单已解决，企业账号授权已恢复。')

    replies = [call('inspect_progress', {}, 'progress'), check] if inspect_tool else [check]
    configure(cfg, monkeypatch, ScriptModel(replies=replies))
    result = ask(client, db, cfg, base, '请查刚才工单的实际处理进度。',
                 conversation_id=first['conversation_id'])
    assert result['status'] == 'answered' and '已解决' in result['answer'], result
    state = json.loads(db.one('SELECT state FROM runs WHERE id=?', (result['id'],))['state'])
    assert state['business']['current_ticket_states'][0]['version'] == 4
    assert state['business']['deferred_user_actions'] == []
    journal = Journal(workspace / 'effects.sqlite3', first['conversation_id'])
    try:
        original, = [r for r in journal.records() if r['name'] == 'prepare_ticket_draft']
        assert original['result']['status'] == 'draft' and original['result']['version'] == 1
    finally:
        journal.close()
    assert db.one('SELECT COUNT(*) AS n FROM service_tickets')['n'] == 1
    assert db.one('SELECT COUNT(*) AS n FROM ticket_events')['n'] == 3


def test_ticket_change_during_generation_rechecks_before_delivering(system, monkeypatch):
    cfg, db, client, base, *_ = system
    first, ticket, _ = draft(system, monkeypatch)

    def change_after_observation(messages):
        assert view(messages)['current_ticket_states'][0]['status'] == 'draft'
        response = client.post('/api/tickets/' + ticket['id'] + '/action',
            json={'action': 'cancel', 'version': 1})
        assert response.status_code == 200
        return answer('工单仍为草稿。')

    def refreshed(messages):
        assert view(messages)['current_ticket_states'][0]['status'] == 'cancelled'
        return answer('工单已经取消。')

    configure(cfg, monkeypatch, ScriptModel(replies=[change_after_observation, refreshed]))
    result = ask(client, db, cfg, base, '请查询刚才工单当前状态。', conversation_id=first['conversation_id'])
    assert result['status'] == 'answered' and '已经取消' in result['answer'], result
    retries = [e for e in result['events'] if e['kind'] == 'application_contract_retry']
    assert len(retries) == 1
    assert retries[0]['payload']['errors'][0]['code'] == 'ticket_state_changed_during_generation'


def test_repeated_external_changes_stop_without_delivering_stale_answer(system, monkeypatch):
    cfg, db, client, base, *_ = system
    first, ticket, _ = draft(system, monkeypatch)

    def submit_after_read(messages):
        assert view(messages)['current_ticket_states'][0]['status'] == 'draft'
        assert client.post('/api/tickets/' + ticket['id'] + '/action',
            json={'action': 'submit', 'version': 1}).status_code == 200
        return answer('仍然是草稿。')

    def accept_after_read(messages):
        assert view(messages)['current_ticket_states'][0]['status'] == 'submitted'
        login(client, 'admin')
        assert client.post('/api/tickets/' + ticket['id'] + '/action',
            json={'action': 'accept', 'version': 2}).status_code == 200
        login(client)
        return answer('已提交，尚未接单。')

    configure(cfg, monkeypatch, ScriptModel(replies=[submit_after_read, accept_after_read]))
    result = ask(client, db, cfg, base, '查询当前工单状态。', conversation_id=first['conversation_id'])
    assert result['status'] == 'error' and result['error'] == 'NativeOutputContractError', result
    assert not result['answer'] and not result['can_resume']
    assert len([e for e in result['events'] if e['kind'] == 'application_contract_retry']) == 1
    assert db.one('SELECT status FROM service_tickets WHERE id=?', (ticket['id'],))['status'] == 'processing'


def test_current_view_is_user_and_conversation_scoped(system, monkeypatch):
    cfg, db, _, _, _, _, member = system
    first, ticket, workspace = draft(system, monkeypatch)
    _, foreign, _ = draft(system, monkeypatch, username='second', title='另一个用户的私密工单')
    journal = Journal(workspace / 'effects.sqlite3', first['conversation_id'])
    try:
        port = ApplicationPort(db, cfg, member,
            json.loads(db.one('SELECT base_ids FROM runs WHERE id=?', (first['id'],))['base_ids']), first['id'])
        port.bind_journal(journal)
        current = port.progress_snapshot(journal.snapshot(current_version=port.version()))
        assert [r['id'] for r in current['current_ticket_states']] == [ticket['id']]
        assert foreign['id'] not in json.dumps(current) and foreign['title'] not in json.dumps(current, ensure_ascii=False)
    finally:
        journal.close()


def test_no_longer_available_ticket_is_not_presented_as_a_current_draft(system, monkeypatch):
    cfg, db, _, _, _, _, member = system
    first, ticket, workspace = draft(system, monkeypatch)
    db.execute('DELETE FROM service_tickets WHERE id=?', (ticket['id'],))
    journal = Journal(workspace / 'effects.sqlite3', first['conversation_id'])
    try:
        port = ApplicationPort(db, cfg, member,
            json.loads(db.one('SELECT base_ids FROM runs WHERE id=?', (first['id'],))['base_ids']), first['id'])
        port.bind_journal(journal)
        snapshot = port.progress_snapshot(journal.snapshot(current_version=port.version()))
        assert snapshot['current_ticket_states'] == []
        assert snapshot['unavailable_ticket_ids'] == [ticket['id']]
        assert port.user_handoffs(journal.records()) == []
    finally:
        journal.close()


def test_freshness_metadata_does_not_rewrite_the_durable_receipt(tmp_path):
    from test_deep_harness import Port
    journal = Journal(tmp_path / 'effects.sqlite3', 'one')
    port = Port()
    try:
        spec = next(s for s in port.tools() if not s.write)
        journal.invoke(spec, {}, 'r', port, lambda *args: None)
        before = journal.records()
        changed = journal.snapshot(current_version='a-later-version')
        assert changed['recent_observations'][0]['state_changed_since_observation'] is True
        assert journal.records() == before
    finally:
        journal.close()
