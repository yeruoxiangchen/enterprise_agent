"""Owned-process crash recovery and explicitly scripted fault injection."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration

import business_ops as old
import business_v2_ops as v2
import business_v3_ops as ops
from business_assessment_metrics import counts
from business_assessment_observe import instrument
from kb.agent import Harness
from kb.context import ContextBudget
from kb.db import uid
from kb.tickets import draft_ticket, transition

CRASH_EXIT = 86
FAULT_IDS = ('after_domain_checkpoint', 'after_draft_commit', 'model_timeout', 'invalid_schema',
             'cancelled_request_cannot_write', 'duplicate_draft_and_confirmation', 'stale_version', 'member_admin_action')


def create_run(db, question, name):
    user = v2.seed_case_user(db, 'fault-'+name)
    cid, rid, now = uid(), uid(), time.time()
    db.execute('INSERT INTO conversations VALUES(?,?,?,?)', (cid, user['id'], name, now))
    db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)', (uid(), cid, 'user', question, rid, now))
    db.execute("INSERT INTO runs(id,conversation_id,user_id,question,base_ids,mode,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?)",
               (rid, cid, user['id'], question, '[]', 'agent', now, now))
    return rid, user


class InjectedModel(BaseChatModel):
    kind: str

    @property
    def _llm_type(self):
        return 'assessment-scripted-fault'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.kind == 'model_timeout':
            raise TimeoutError('assessment: injected model timeout')
        message = AIMessage(content='', tool_calls=[{
            'name': 'KnowledgeResult', 'id': uid(), 'type': 'tool_call',
            'args': {'facts': [{'text': '住宿每晚600元', 'sources': ['S999']}],
                     'resolution': 'complete', 'unresolved_question': ''}}])
        return ChatResult(generations=[ChatGeneration(message=message)])


class SuccessModel(BaseChatModel):
    """Deterministic model for CPU process-crash regression tests only."""
    @property
    def _llm_type(self):
        return 'assessment-scripted-success'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if messages[-1].type == 'tool':
            message = AIMessage(content='草稿已就绪，待本人确认。')
        else:
            data = json.loads(messages[-1].content)
            if data.get('actions'):
                message = AIMessage(content='', tool_calls=[{'name': 'prepare_ticket', 'args': {}, 'id': uid(), 'type': 'tool_call'}])
            else:
                policy = '住宿' in data['task']
                evidence = next(e for e in data['evidence'] if ('600' if policy else '公钥') in e['text'])
                message = AIMessage(content='', tool_calls=[{'name': 'KnowledgeResult', 'id': uid(), 'type': 'tool_call',
                    'args': {'facts': [{'text': '住宿每晚600元' if policy else '向管理员提供公钥，不发送私钥', 'sources': [evidence['source']]}],
                             'resolution': 'complete', 'unresolved_question': ''}}])
        return ChatResult(generations=[ChatGeneration(message=message)])


def fault_worker(args):
    if not getattr(args, 'scripted_success', False):
        return _fault_worker(args)
    from kb.backends.business_multi_v2 import BusinessQA as Base
    original = Base.__init__

    def init(self, *values, **kwargs):
        kwargs['model'] = SuccessModel()
        original(self, *values, **kwargs)
        self.budget = ContextBudget(self.cfg, counter=lambda messages, tools: 100)

    with patch.object(Base, '__init__', init):
        return _fault_worker(args)


def _fault_worker(args):
    from kb.backends.business_multi_v2 import BusinessQA as Base
    policy = getattr(args, 'policy', 'business_multi_v4')
    if policy == 'business_multi_v5':
        from kb.backends.business_multi_v5 import BusinessQA as Candidate
        from kb.backends import business_multi_v5 as service
    else:
        from kb.backends.business_multi_v4 import BusinessQA as Candidate
        from kb.backends import business_multi_v3 as service
    from kb.worker import Workers
    cfg, db = ops.prepare(args.data_dir, True)
    cfg = replace(cfg, agent_policy=policy, reranker_path='' if getattr(args, 'scripted_success', False) else cfg.reranker_path)
    marker = args.data_dir/'injection.json'
    if args.phase == 'resume':
        # Exercise the actual startup recovery in Workers.start(), including its
        # running -> queued transition. This DB belongs only to this fault case.
        workers = Workers(db, cfg)
        try:
            with instrument():
                workers.start()
                deadline = time.monotonic()+300
                while db.one('SELECT status FROM runs WHERE id=?', (args.run_id,))['status'] in ('queued', 'running'):
                    if time.monotonic() > deadline:
                        raise TimeoutError('Recovery exceeded 300 seconds')
                    time.sleep(.2)
        finally:
            workers.stop()
        return
    if args.fault == 'after_domain_checkpoint':
        original = Candidate.worker

        def interrupt(self, domain, state):
            if domain == 'policy' and any(r['domain'] == 'technical' for r in state['results'].values()):
                old.write(marker, {'point': 'second_domain_entry', 'calls_before': self.state['calls'],
                                   'completed': list(state['results']), 'pid': os.getpid()})
                os._exit(CRASH_EXIT)
            return original(self, domain, state)

        with instrument(), patch.object(Candidate, 'worker', interrupt):
            Harness(db, cfg).execute(args.run_id)
    elif args.fault == 'after_draft_commit':
        original = service.draft_ticket

        def interrupt(*values, **kwargs):
            ticket = original(*values, **kwargs)
            state = json.loads(db.one('SELECT state FROM runs WHERE id=?', (args.run_id,))['state'])
            old.write(marker, {'point': 'draft_committed_before_receipt_checkpoint', 'ticket_id': ticket['id'],
                               'calls_before': state['business']['calls'], 'pid': os.getpid()})
            os._exit(CRASH_EXIT)

        with instrument(), patch.object(service, 'draft_ticket', interrupt):
            Harness(db, cfg).execute(args.run_id)
    else:
        original = Base.__init__

        def init(self, *values, **kwargs):
            kwargs['model'] = InjectedModel(kind=args.fault)
            original(self, *values, **kwargs)
            self.budget = ContextBudget(self.cfg, counter=lambda messages, tools: 100)

        with patch.object(Base, '__init__', init):
            Harness(db, cfg).execute(args.run_id)


def child(target, fault, run_id, phase='first', policy='business_multi_v4', scripted_success=False):
    command = [sys.executable, '-B', str(Path(__file__).with_name('business_assessment.py')),
               'fault-worker', '--data-dir', str(target), '--fault', fault, '--run-id', run_id, '--phase', phase, '--policy', policy]
    if scripted_success:
        command.append('--scripted-success')
    with (target/(phase+'.log')).open('a') as handle:
        proc = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
        try:
            return proc.wait(timeout=360)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def crash_case(target, fault, policy='business_multi_v4', scripted_success=False):
    cfg, db = ops.prepare(target, True)
    question = ('SSH认证应该交公钥还是私钥？另外按演示差旅制度，住宿每晚上限是多少？'
                if fault == 'after_domain_checkpoint' else '我的Windows电脑SSH连接提示Permission denied，已经检查网络。请生成工单草稿。')
    rid, user = create_run(db, question, fault)
    code = child(target, fault, rid, policy=policy, scripted_success=scripted_success)
    marker = target/'injection.json'
    if code != CRASH_EXIT or not marker.exists():
        return {'passed': False, 'first_exit': code, 'reason': 'Declared interruption point was not reached', 'run_id': rid}
    before = json.loads(marker.read_text())
    started = time.monotonic()
    resumed = child(target, fault, rid, 'resume', policy=policy, scripted_success=scripted_success)
    row = db.one('SELECT status,state FROM runs WHERE id=?', (rid,))
    state = json.loads(row['state']).get('business', {})
    messages = db.one("SELECT count(*) n FROM messages WHERE run_id=? AND role='assistant'", (rid,))['n']
    tickets = db.all('SELECT id,status FROM service_tickets WHERE source_run=?', (rid,))
    workers = [json.loads(e['payload']) for e in db.all("SELECT payload FROM events WHERE run_id=? AND kind='business_worker'", (rid,))]
    retained = state.get('calls', 0) >= before['calls_before']
    effect_ok = (sum(e['domain'] == 'technical' for e in workers) == 1 if fault == 'after_domain_checkpoint'
                 else len(tickets) == 1 and tickets[0]['id'] == before['ticket_id'] and tickets[0]['status'] == 'draft')
    return {'passed': resumed == 0 and row['status'] in v2.SUCCESS and messages == 1 and effect_ok and retained,
            'kind': 'scripted_model_real_process_exit' if scripted_success else 'real_model_and_process_exit',
            'run_id': rid, 'first_exit': code, 'resume_exit': resumed,
            'final_status': row['status'], 'assistant_messages': messages, 'tickets_for_run': tickets,
            'calls_before': before['calls_before'], 'calls_after': state.get('calls'),
            'no_duplicate_completed_domain_or_draft': effect_ok, 'budget_retained': retained,
            'recovery_seconds': time.monotonic()-started, 'injection': before}


def scripted_case(target, fault, policy='business_multi_v4'):
    cfg, db = ops.prepare(target, True)
    rid, _ = create_run(db, '按演示差旅制度，住宿报销上限是多少？', fault)
    code = child(target, fault, rid, policy=policy)
    row = db.one('SELECT status,state FROM runs WHERE id=?', (rid,))
    calls = json.loads(row['state']).get('business', {}).get('calls', 0)
    tickets = db.one('SELECT count(*) n FROM service_tickets WHERE source_run=?', (rid,))['n']
    return {'passed': code == 0 and row['status'] == 'error' and tickets == 0 and
            (calls == 1 if fault == 'model_timeout' else calls == 2),
            'kind': 'scripted_model_fault_real_harness', 'run_id': rid, 'exit': code,
            'final_status': row['status'], 'calls': calls, 'tickets_for_run': tickets}


def state_cases(target):
    cfg, db = ops.prepare(target, True)
    rid, user = create_run(db, '电脑故障，请生成草稿。', 'state')
    db.execute("UPDATE runs SET status='answered' WHERE id=?", (rid,))
    with ThreadPoolExecutor(max_workers=4) as pool:
        tickets = list(pool.map(lambda _: draft_ticket(db, user, rid, '连接故障', '用户原文'), range(16)))
    ident = tickets[0]['id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        confirmations = list(pool.map(lambda _: transition(db, user, ident, 'submit', 1), range(16)))
    events = db.one('SELECT count(*) n FROM ticket_events WHERE ticket_id=?', (ident,))['n']
    admin = db.one("SELECT * FROM users WHERE username='demo-admin'")
    result = {'duplicate_draft_and_confirmation': {
        'passed': len({t['id'] for t in tickets}) == 1 and {t['version'] for t in confirmations} == {2} and events == 1,
        'kind': 'concurrent_real_db_operations', 'requests_each': 16,
        'unique_drafts': len({t['id'] for t in tickets}), 'confirmation_events': events}}
    for name, actor, action, version, error in [
            ('stale_version', admin, 'accept', 1, ValueError),
            ('member_admin_action', user, 'resolve', 2, PermissionError)]:
        refused = False
        try:
            transition(db, actor, ident, action, version)
        except error:
            refused = True
        unchanged = db.one('SELECT status,version FROM service_tickets WHERE id=?', (ident,))
        result[name] = {'passed': refused and unchanged['status'] == 'submitted' and unchanged['version'] == 2,
                        'kind': 'real_db_state_guard', 'refused': refused, 'state': unchanged}
    cancelled, cancel_user = create_run(db, '电脑故障，请生成草稿。', 'cancel')
    db.execute('UPDATE runs SET cancel_requested=1 WHERE id=?', (cancelled,))
    refused = False
    try:
        draft_ticket(db, cancel_user, cancelled, '取消故障', '用户原文')
    except ValueError:
        refused = True
    count = db.one('SELECT count(*) n FROM service_tickets WHERE source_run=?', (cancelled,))['n']
    result['cancelled_request_cannot_write'] = {'passed': refused and count == 0,
        'kind': 'real_db_state_guard', 'write_refused': refused, 'tickets_for_run': count}
    return result


def run_faults(target, policy='business_multi_v4'):
    target.mkdir(parents=True, exist_ok=True)
    receipt = target/'summary.json'
    previous = json.loads(receipt.read_text()) if receipt.exists() else {}
    results = previous.get('cases', {})
    if previous.get('status') == 'complete':
        return previous

    def save():
        result = {'status': 'complete' if all(k in results for k in FAULT_IDS) else 'running',
                  'cases': results, 'outcomes': counts(results[k]['passed'] if k in results else None for k in FAULT_IDS),
                  'policy': policy,
                  'scope': 'two real-model process crashes; scripted model failures and real DB guards separately labeled'}
        old.write(receipt, result)
        return result

    for fault in FAULT_IDS[:4]:
        if fault in results:
            continue
        app = target/fault
        if app.exists() and (app/'knowledge.sqlite3').exists():
            raise RuntimeError('Unarchived fault experiment exists; inspect it before resuming')
        print('fault start: '+fault, flush=True)
        results[fault] = crash_case(app, fault, policy=policy) if fault.startswith('after_') else scripted_case(app, fault, policy=policy)
        save()
        print('fault '+fault+': '+json.dumps(results[fault], ensure_ascii=False), flush=True)
    if not all(k in results for k in FAULT_IDS[4:]):
        direct = target/'state_guards'
        if direct.exists():
            raise RuntimeError('Unarchived state guard experiment exists')
        results.update(state_cases(direct))
    return save()
