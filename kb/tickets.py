"""Local service desk. Models can prepare drafts; only the authenticated UI confirms."""
import json
import time
from .db import uid


def get_ticket(db, user, ticket_id):
    row = db.one('SELECT * FROM service_tickets WHERE id=? AND user_id=?', (ticket_id, user['id']))
    if not row:
        raise LookupError('工单不存在或不属于当前用户')
    return row


def draft_ticket(db, user, run_id, title, description):
    if not title.strip() or len(title) > 120 or not description.strip() or len(description) > 3000:
        raise ValueError('标题需1–120字，问题描述需1–3000字')
    run = db.one('SELECT * FROM runs WHERE id=? AND user_id=?', (run_id, user['id']))
    if not run or run['cancel_requested']:
        raise ValueError('问答不存在或已取消')
    with db.connect() as c:
        # One immutable draft per request: replay cannot duplicate a side effect.
        c.execute('INSERT OR IGNORE INTO service_tickets VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (uid(), user['id'], run_id, title.strip(), description.strip(), 'draft', '', 1, time.time(), time.time()))
    return db.one('SELECT * FROM service_tickets WHERE source_run=?', (run_id,))


def transition(db, user, ticket_id, action, version, note=''):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM service_tickets WHERE id=?', (ticket_id,)).fetchone()
        if not row or (row['user_id'] != user['id'] and user['role'] != 'admin'):
            raise LookupError('工单不存在')
        allowed = {'submit': ('draft', 'submitted'), 'cancel': ('draft', 'cancelled')}
        if user['role'] == 'admin':
            allowed.update({'accept': ('submitted', 'processing'), 'resolve': ('processing', 'resolved')})
        if action not in allowed:
            raise PermissionError('无权进行该操作')
        before, after = allowed[action]
        if row['status'] == after and row['version'] == version + 1:
            return dict(row)  # Idempotent retry of the same confirmation.
        if row['status'] != before or row['version'] != version:
            raise ValueError('工单已更新，请刷新后操作')
        if action == 'submit':
            run = c.execute('SELECT status,cancel_requested FROM runs WHERE id=?', (row['source_run'],)).fetchone()
            if not run or run['cancel_requested'] or run['status'] not in ('answered', 'clarify', 'insufficient'):
                raise ValueError('原问答尚未正常完成，不能提交该草稿')
        c.execute('UPDATE service_tickets SET status=?,note=?,version=version+1,updated_at=? WHERE id=?',
                  (after, note[:2000], time.time(), ticket_id))
        c.execute('INSERT INTO ticket_events VALUES(?,?,?,?,?)', (uid(), ticket_id, user['id'],
                  json.dumps({'action': action, 'from': before, 'to': after}, ensure_ascii=False), time.time()))
        return dict(c.execute('SELECT * FROM service_tickets WHERE id=?', (ticket_id,)).fetchone())
