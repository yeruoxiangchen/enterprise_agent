import json
import re
import time
from .agent import Cancelled


def execute(h, run_id):
    db = h.db
    if not db.execute("UPDATE runs SET status='running',updated_at=? WHERE id=? AND status='queued'", (time.time(), run_id)):
        return
    run = db.one('SELECT * FROM runs WHERE id=?', (run_id,))
    try:
        policy = h.settings.agent_policy
        if policy == 'business_multi_v1':
            from .backends.business_multi import BusinessQA
        elif policy == 'business_multi_v2':
            from .backends.business_multi_v2 import BusinessQA
        elif policy == 'business_multi_v3':
            from .backends.business_multi_v3 import BusinessQA
        elif policy == 'business_multi_v4':
            from .backends.business_multi_v4 import BusinessQA
        elif policy == 'business_single_v2':
            from .backends.business_multi_v2 import SingleBusinessQA as BusinessQA
        else: raise ValueError('未知业务策略')
        user = db.one('SELECT id,role,username FROM users WHERE id=?', (run['user_id'],))
        history = db.all('SELECT role,content FROM messages WHERE conversation_id=? AND created_at<? ORDER BY created_at DESC LIMIT 8',
                         (run['conversation_id'], run['created_at']))[::-1]
        previous = json.loads(run['state'])
        if previous and previous.get('policy') != policy:
            raise ValueError('检查点策略不匹配')
        engine = BusinessQA(db, h.settings, run, user, history, previous.get('business'),
            checkpoint=lambda s: h.checkpoint(run_id, {'policy':policy, 'business':s}),
            event=lambda k,v: db.event(run_id,k,v), cancelled=lambda: h.check_cancel(run_id))
        result = engine.answer()
        labels = list(dict.fromkeys(re.findall(r'\[(S\d+)\]', result['answer'])))
        state = {'policy':policy, 'business': engine.state, 'sources': engine.state['sources'],
                 'user': user, 'base_ids': engine.allowed}
        h.finish(run, state, result['answer'], labels, result['status'])
    except Exception as exc:
        from .backends.agentpack_rag import exception_diagnostic
        row = db.one('SELECT cancel_requested FROM runs WHERE id=?', (run_id,))
        status = 'cancelled' if isinstance(exc, Cancelled) or row['cancel_requested'] else 'error'
        db.execute('UPDATE runs SET status=?,error=?,updated_at=? WHERE id=?', (status, type(exc).__name__ if status=='error' else None, time.time(),run_id))
        db.event(run_id,status,{'message':type(exc).__name__, 'reason':str(exc)[:300] if isinstance(exc,ValueError) else '', 'chain':exception_diagnostic(exc)})
