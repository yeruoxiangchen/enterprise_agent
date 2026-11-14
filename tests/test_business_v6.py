import json

import pytest
from langchain_core.messages import AIMessage

from kb.agent import Harness
from kb.context import ContextBudget
from kb.backends import business_multi_v6 as v6
from test_application import upload, login
from test_business import ScriptModel

QUESTION = '我的SSH连接提示Permission denied，应该先检查什么？请生成工单草稿。'


def configure(system, monkeypatch, replies=(), question=QUESTION):
    cfg, db, client, *_ = system
    upload(system, 'SSH连接故障应先检查网络和公钥。', name='SSH.md')
    cfg.agent_policy = 'business_multi_v6'
    cfg.mode, cfg.llm_url, cfg.llm_model = 'agent', 'http://local.invalid/v1', 'scripted'
    model = ScriptModel(plan={}, replies=list(replies))
    monkeypatch.setattr('kb.backends.business_multi_v2.ChatOpenAI', lambda **kwargs: model)
    monkeypatch.setattr(ContextBudget, 'count', lambda *args: (100, 'test'))
    login(client)
    result = client.post('/api/chat', json={'question': question})
    assert result.status_code == 202, result.text
    return cfg, db, client, model, result.json()['run_id']


def knowledge():
    return AIMessage(content='', tool_calls=[{'name': 'KnowledgeResult', 'args': {
        'facts': [{'text': '应先检查网络和公钥。', 'sources': ['S1']}],
        'resolution': 'complete', 'unresolved_question': ''}, 'id': 'knowledge'}])


def test_partial_answer_resume_reuses_knowledge_budget_and_one_assistant_message(system, monkeypatch):
    cfg, db, client, model, run_id = configure(system, monkeypatch, [knowledge()])
    original = v6.draft_ticket
    def unavailable(*args, **kwargs):
        raise TimeoutError('controlled tool timeout before commit')
    monkeypatch.setattr(v6, 'draft_ticket', unavailable)
    Harness(db, cfg).execute(run_id)
    run = client.get('/api/runs/'+run_id).json()
    assert run['status'] == 'partial_success', run
    assert '网络和公钥' in run['answer'] and '[S1]' in run['answer'] and '未完成' in run['answer']
    assert run['can_resume'] and len(run['citations']) == 1
    before = json.loads(db.one('SELECT state FROM runs WHERE id=?', (run_id,))['state'])
    assert before['business']['calls'] == 1
    assert len(model.replies) == 0
    assert client.post('/api/runs/'+run_id+'/feedback', json={'rating': 1}).status_code == 200
    login(client, 'second')
    assert client.post('/api/runs/'+run_id+'/resume').status_code == 404
    login(client)
    exhausted = json.loads(json.dumps(before))
    exhausted['business']['calls'] = 12
    db.execute('UPDATE runs SET state=? WHERE id=?', (json.dumps(exhausted), run_id))
    assert client.post('/api/runs/'+run_id+'/resume').status_code == 409
    db.execute('UPDATE runs SET state=? WHERE id=?', (json.dumps(before), run_id))
    monkeypatch.setattr(v6, 'draft_ticket', original)
    assert client.post('/api/runs/'+run_id+'/resume').status_code == 200
    Harness(db, cfg).execute(run_id)
    finished = client.get('/api/runs/'+run_id).json()
    assert finished['status'] == 'answered', finished
    after = json.loads(db.one('SELECT state FROM runs WHERE id=?', (run_id,))['state'])
    assert after['business']['calls'] == before['business']['calls']
    assert after['business']['tools'] > before['business']['tools']
    assert db.one('SELECT count(*) AS n FROM service_tickets WHERE source_run=?', (run_id,))['n'] == 1
    assert db.one("SELECT count(*) AS n FROM messages WHERE run_id=? AND role='assistant'", (run_id,))['n'] == 1
    assert not finished['can_resume']
    assert client.post('/api/runs/'+run_id+'/resume').status_code == 409


def test_timeout_after_commit_is_reconciled_without_a_second_draft(system, monkeypatch):
    cfg, db, client, _, run_id = configure(system, monkeypatch,
        question='我的Ubuntu电脑SSH连接提示Permission denied。请生成工单草稿。')
    original = v6.draft_ticket
    calls = []
    def commit_then_timeout(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise TimeoutError('controlled response lost after commit')
    monkeypatch.setattr(v6, 'draft_ticket', commit_then_timeout)
    Harness(db, cfg).execute(run_id)
    run = client.get('/api/runs/'+run_id).json()
    assert run['status'] == 'answered', run
    assert '待确认' in run['answer'] and len(calls) == 1
    assert db.one('SELECT count(*) AS n FROM service_tickets')['n'] == 1
    state = json.loads(db.one('SELECT state FROM runs WHERE id=?', (run_id,))['state'])
    assert state['business']['receipts'][0]['verified'] is True


def test_partial_draft_confirmation_requires_verified_completed_receipt(system, monkeypatch):
    cfg, db, client, _, run_id = configure(system, monkeypatch)
    original = v6.BusinessQA.execute_task
    def technical_timeout(self, domain, state):
        if domain == 'technical':
            raise TimeoutError('controlled retrieval service timeout')
        return original(self, domain, state)
    monkeypatch.setattr(v6.BusinessQA, 'execute_task', technical_timeout)
    Harness(db, cfg).execute(run_id)
    run = client.get('/api/runs/'+run_id).json()
    assert run['status'] == 'partial_success', run
    ticket = db.one('SELECT * FROM service_tickets WHERE source_run=?', (run_id,))
    assert ticket['status'] == 'draft'
    path = '/api/tickets/'+ticket['id']+'/action'
    original_state = db.one('SELECT state FROM runs WHERE id=?', (run_id,))['state']
    unverified = json.loads(original_state)
    unverified['business']['receipts'][0]['verified'] = False
    db.execute('UPDATE runs SET state=? WHERE id=?', (json.dumps(unverified), run_id))
    assert client.post(path, json={'action': 'submit', 'version': 1}).status_code == 409
    db.execute('UPDATE runs SET state=? WHERE id=?', (original_state, run_id))
    response = client.post(path, json={'action': 'submit', 'version': 1})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'submitted'


def test_reassignment_never_grants_ticket_creation_authority(system, monkeypatch):
    cfg, db, client, _, run_id = configure(system, monkeypatch, [
        AIMessage(content='', tool_calls=[{'name': 'request_reassignment', 'args': {
            'reason': 'controlled domain mismatch', 'suggested_domain': 'service'}, 'id': 'reject'}]),
        AIMessage(content='', tool_calls=[{'name': 'KnowledgeResult', 'args': {
            'facts': [], 'resolution': 'missing_evidence', 'unresolved_question': '当前缺少对应资料'}, 'id': 'finish'}]),
    ], question='SSH连接失败应该先检查什么？')
    Harness(db, cfg).execute(run_id)
    run = client.get('/api/runs/'+run_id).json()
    assert run['status'] == 'insufficient', run
    assert db.one('SELECT count(*) AS n FROM service_tickets')['n'] == 0
    state = json.loads(db.one('SELECT state FROM runs WHERE id=?', (run_id,))['state'])
    control = next(iter(state['business']['task_control'].values()))
    assert control['attempted_domains'] == ['technical', 'service', 'policy']
    assert control['reassignments'] == 2
