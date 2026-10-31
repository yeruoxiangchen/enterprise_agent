"""Optional app policy; upstream workflow runs against an isolated authorized store."""
import json
import time
from .agent import Cancelled


def execute(h, run_id):
    from .backends.haystack_rag import QARequest
    from .backends.agentpack_rag import AgentPackQA, exception_diagnostic
    from .backends.fixed_first_rag import FixedFirstQA
    policy=h.settings.agent_policy
    db = h.db
    if not db.execute("UPDATE runs SET status='running',updated_at=? WHERE id=? AND status='queued'", (time.time(), run_id)):
        return
    run = db.one('SELECT * FROM runs WHERE id=?', (run_id,))
    try:
        user = db.one('SELECT id,role,username FROM users WHERE id=?', (run['user_id'],))
        history = db.all('SELECT role,content FROM messages WHERE conversation_id=? AND created_at<? ORDER BY created_at DESC LIMIT 8',
                         (run['conversation_id'], run['created_at']))[::-1]
        old = json.loads(run['state'])
        if old and old.get('policy') != policy:
            raise RuntimeError('KB: cannot restore a different policy')
        backend=FixedFirstQA if policy in {'fixed_first_v1','fixed_rag_v1'} else AgentPackQA
        options={'enable_followup':False} if policy=='fixed_rag_v1' else {}
        engine = backend(db, h.settings,
            **options,
            checkpoint=lambda s: h.checkpoint(run_id, {'policy': policy, 'pack': s}),
            cancelled=lambda: h.check_cancel(run_id), event=lambda k,v: db.event(run_id,k,v))
        result = engine.answer(QARequest(run['question'], tuple(history), user,
                                        tuple(json.loads(run['base_ids']))), old.get('pack'))
        if result['warnings']:
            raise RuntimeError('KB: final output contract failed: '+','.join(result['warnings']))
        h.finish(run, {'sources': engine.sources, 'user': user, 'base_ids': json.loads(run['base_ids']),
                      'policy': policy, 'pack': engine.state},
                 result['answer'], [c['label'] for c in result['citations']], result['status'])
    except Exception as exc:
        row = db.one('SELECT cancel_requested FROM runs WHERE id=?', (run_id,))
        if isinstance(exc, Cancelled) or (row and row['cancel_requested']):
            db.execute("UPDATE runs SET status='cancelled',updated_at=? WHERE id=?", (time.time(), run_id))
            db.event(run_id, 'cancelled', {})
        else:
            db.execute("UPDATE runs SET status='error',error=?,updated_at=? WHERE id=?", (type(exc).__name__, time.time(), run_id))
            db.event(run_id, 'error', {'message': type(exc).__name__, 'chain': exception_diagnostic(exc)})
