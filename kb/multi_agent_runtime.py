"""Application adapter for the optional Haystack multi-agent environment."""
import json
import time

from .agent import Cancelled


def execute(h, run_id):
    from .backends.haystack_rag import QARequest
    from .backends.multi_agent import MultiAgentQA
    db = h.db
    if not db.execute("UPDATE runs SET status='running',updated_at=? WHERE id=? AND status='queued'", (time.time(), run_id)):
        return
    run = db.one('SELECT * FROM runs WHERE id=?', (run_id,))
    try:
        user = db.one('SELECT id,role,username FROM users WHERE id=?', (run['user_id'],))
        history = db.all('SELECT role,content FROM messages WHERE conversation_id=? AND created_at<? ORDER BY created_at DESC LIMIT 8',
                         (run['conversation_id'], run['created_at']))[::-1]
        old = json.loads(run['state'])
        if old and old.get('policy') != 'multi_agent_v1':
            raise RuntimeError('Cannot restore another policy checkpoint')
        engine = MultiAgentQA(db, h.settings,
            checkpoint=lambda state: h.checkpoint(run_id, {'policy': 'multi_agent_v1', 'multi': state}),
            cancelled=lambda: h.check_cancel(run_id), event=lambda kind, data: db.event(run_id, kind, data))
        result = engine.answer(QARequest(run['question'], tuple(history), user,
                                        tuple(json.loads(run['base_ids']))), old.get('multi'))
        h.finish(run, {'sources': engine.state['sources'], 'user': user,
                      'base_ids': json.loads(run['base_ids']), 'policy': 'multi_agent_v1', 'multi': engine.state},
                 result['answer'], [c['label'] for c in result['citations']], result['status'])
    except Cancelled:
        db.execute("UPDATE runs SET status='cancelled',updated_at=? WHERE id=?", (time.time(), run_id))
        db.event(run_id, 'cancelled', {})
    except Exception as exc:
        if db.one('SELECT cancel_requested FROM runs WHERE id=?',(run_id,))['cancel_requested']:
            db.execute("UPDATE runs SET status='cancelled',updated_at=? WHERE id=?", (time.time(), run_id))
            db.event(run_id, 'cancelled', {})
            return
        error = type(exc).__name__
        db.execute("UPDATE runs SET status='error',error=?,updated_at=? WHERE id=?", (error, time.time(), run_id))
        db.event(run_id, 'error', {'message': error})
