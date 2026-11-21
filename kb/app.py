from contextlib import asynccontextmanager
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import create_session, create_user, session_user, token_hash, verify_password
from .config import Settings
from .db import Database, uid, encode
from .ingestion import register_document
from .retrieval import Retriever
from .worker import Workers


class Login(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class NewUser(Login):
    role: str = "member"


class NewBase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    visibility: str = "all"
    domain: str = 'technical'


class TicketAction(BaseModel):
    action: str
    version: int = Field(ge=1)
    note: str = Field(default='', max_length=2000)


class DomainBinding(BaseModel):
    domain: str


class IncidentAnswer(BaseModel):
    incident_id: str
    field: Literal['error_code', 'symptom', 'environment', 'software']
    value: str = Field(min_length=1, max_length=240)


class Question(BaseModel):
    question: str = Field(default="", max_length=3000)
    client_message_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    conversation_id: str | None = None
    base_ids: list[str] = Field(default_factory=list, max_length=50)
    incident_ids: list[str] = Field(default_factory=list,max_length=8)
    knowledge_grounded: bool = False
    read_only_incident: bool = False


class Feedback(BaseModel):
    rating: int
    comment: str = Field(default="", max_length=2000)


class IncidentDraft(BaseModel):
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2700)
    confirmed: bool = False


def create_app(settings=None, start_workers=True):
    cfg = settings or Settings()
    db = Database(cfg.db_path)

    @asynccontextmanager
    async def lifespan(app):
        cfg.prepare()
        lock = (cfg.root / "worker.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError("同一数据目录只能运行一个应用进程；请使用 --workers 1") from None
        db.initialize()
        workers = Workers(db, cfg)
        app.state.workers = workers
        app.state.db, app.state.settings = db, cfg
        if start_workers:
            workers.start()
        try:
            yield
        finally:
            if start_workers:
                workers.stop()
            lock.close()

    app = FastAPI(title="Knowledge Desk", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if request.headers.get("x-kb-client") != "web" or (origin and urlsplit(origin).netloc != request.headers.get("host")):
                return JSONResponse({"detail": "请求来源校验失败"}, status_code=403)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "非法 Content-Length"}, status_code=400)
            if length > cfg.max_upload_bytes + 1024 * 1024:
                return JSONResponse({"detail": "请求内容过大"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def current_user(request: Request):
        user = session_user(db, request.cookies.get("kb_session"))
        if not user:
            raise HTTPException(401, "请先登录")
        return user

    def admin(user=Depends(current_user)):
        if user["role"] != "admin":
            raise HTTPException(403, "需要管理员权限")
        return user

    def base_access(user, ids):
        try:
            return db.check_bases(user, ids)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None

    def own_run(user, run_id):
        row = db.one("SELECT * FROM runs WHERE id=? AND user_id=?", (run_id, user["id"]))
        if not row:
            raise HTTPException(404, "任务不存在")
        return row

    def document_access(user, document_id):
        row = db.one("SELECT * FROM documents WHERE id=?", (document_id,))
        if not row:
            raise HTTPException(404, "文档不存在")
        base_access(user, [row["base_id"]])
        return row

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "enterprise-kb-agent", "version": "0.1.0", "pid": os.getpid()}

    @app.post("/api/login")
    def login(payload: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "local"
        now = time.time()
        db.execute("DELETE FROM login_attempts WHERE created_at<?", (now - 600,))
        if db.one("SELECT count(*) AS n FROM login_attempts WHERE ip=?", (ip,))["n"] >= 20:
            raise HTTPException(429, "登录尝试过多，请 10 分钟后重试")
        user = db.one("SELECT * FROM users WHERE username=?", (payload.username,))
        if not user or not verify_password(payload.password, user["password"]):
            db.execute("INSERT INTO login_attempts VALUES(?,?)", (ip, now))
            raise HTTPException(401, "账号或密码错误")
        token = create_session(db, user["id"])
        response.set_cookie("kb_session", token, httponly=True, secure=cfg.secure_cookie, samesite="strict", max_age=8*3600)
        return {k: user[k] for k in ("id", "username", "role")}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, user=Depends(current_user)):
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(request.cookies["kb_session"]),))
        response.delete_cookie("kb_session")
        return {"ok": True}

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return {**user, "mode": cfg.mode, "retrieval": "hybrid" if cfg.embedding_url else "keyword",
                "workflow": "business_multi" if cfg.mode=='agent' and cfg.agent_policy in {'business_multi_v1','business_multi_v2','business_multi_v3','business_multi_v4','business_multi_v5','business_multi_v6','business_single_v2','business_deep_v1','business_native_v1'} else "fixed_rag" if cfg.mode=='agent' and cfg.agent_policy=='fixed_rag_v1' else 'agent' if cfg.mode=='agent' else 'extractive',
                "agent_policy": cfg.agent_policy,
                "reranking": bool(cfg.reranker_path),
                "model": cfg.llm_model if cfg.mode == "agent" else None}

    @app.post("/api/users")
    def add_user(payload: NewUser, user=Depends(admin)):
        try:
            return {"id": create_user(db, payload.username, payload.password, payload.role)}
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise HTTPException(400, "账号已存在或输入不符合要求" if isinstance(exc, sqlite3.IntegrityError) else str(exc)) from None

    @app.get("/api/bases")
    def bases(user=Depends(current_user)):
        rows = db.bases_for(user)
        for row in rows:
            binding = db.one('SELECT domain FROM business_domains WHERE base_id=?', (row['id'],))
            row['domain'] = binding['domain'] if binding else 'technical'
            row["document_count"] = db.one("SELECT count(*) AS n FROM documents WHERE base_id=?", (row["id"],))["n"]
        return rows

    @app.post("/api/bases", status_code=201)
    def add_base(payload: NewBase, user=Depends(admin)):
        if payload.domain not in {'technical', 'policy'}:
            raise HTTPException(400, '非法业务领域')
        if payload.visibility not in {"all", "admin"} or not payload.name.strip():
            raise HTTPException(400, "请填写有效名称和可见范围")
        base_id = uid()
        db.execute("INSERT INTO bases VALUES(?,?,?,?,?)", (base_id, payload.name.strip(), payload.description, payload.visibility, time.time()))
        db.execute('INSERT INTO business_domains VALUES(?,?)', (base_id, payload.domain))
        return {"id": base_id}

    @app.put('/api/bases/{base_id}/domain')
    def bind_domain(base_id: str, payload: DomainBinding, user=Depends(admin)):
        base_access(user, [base_id])
        if payload.domain not in {'technical', 'policy'}:
            raise HTTPException(400, '非法业务领域')
        db.execute('INSERT OR REPLACE INTO business_domains VALUES(?,?)', (base_id, payload.domain))
        return {'ok': True}

    @app.get('/api/tickets')
    def tickets(user=Depends(current_user)):
        return db.all('SELECT * FROM service_tickets WHERE user_id=? OR ?=\'admin\' ORDER BY updated_at DESC LIMIT 100',
                      (user['id'], user['role']))

    @app.post('/api/tickets/{ticket_id}/action')
    def ticket_action(ticket_id: str, payload: TicketAction, user=Depends(current_user)):
        from .tickets import transition
        try:
            return transition(db, user, ticket_id, payload.action, payload.version, payload.note)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/incident-evidence', status_code=201)
    async def incident_upload(file: UploadFile = File(...), defer_ocr: bool = False, user=Depends(current_user)):
        from .incidents import analyze, MAX_BYTES
        try:
            data = await file.read(MAX_BYTES + 1)
            return await asyncio.to_thread(analyze, db, cfg, user, file.filename or '', data, defer_ocr)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            await file.close()

    @app.get('/api/incident-evidence/{evidence_id}')
    def incident_detail(evidence_id: str, user=Depends(current_user)):
        from .incidents import owned
        try:
            return json.loads(owned(db, user, evidence_id)['payload'])
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post('/api/incident-evidence/{evidence_id}/recognize')
    def incident_recognize(evidence_id: str, user=Depends(current_user)):
        from .incidents import recognize
        try:
            return recognize(db,cfg,user,evidence_id)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None

    @app.get('/api/incident-evidence/{evidence_id}/image')
    def incident_image(evidence_id: str, user=Depends(current_user)):
        from .incidents import owned
        try:
            row = owned(db, user, evidence_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        if json.loads(row['payload'])['kind'] != 'screenshot':
            raise HTTPException(404, '此材料不是截图')
        return FileResponse(cfg.root/'uploads/incidents'/(row['id']+'.png'), media_type='image/png',
                            headers={'Cache-Control': 'no-store'})

    @app.post('/api/incident-evidence/{evidence_id}/draft', status_code=201)
    def incident_draft(evidence_id: str, payload: IncidentDraft, user=Depends(current_user)):
        from .incidents import confirm_draft
        try:
            return confirm_draft(db, user, evidence_id, **payload.model_dump())
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/documents")
    def documents(base_id: str | None = None, user=Depends(current_user)):
        ids = base_access(user, [base_id] if base_id else [])
        if not ids:
            return []
        return db.all("SELECT * FROM documents WHERE base_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY created_at DESC", ids)

    @app.post("/api/documents", status_code=202)
    async def upload(base_id: str = Form(...), file: UploadFile = File(...), user=Depends(admin)):
        base_access(user, [base_id])
        try:
            data = await file.read(cfg.max_upload_bytes + 1)
            doc, duplicate = register_document(db, cfg, base_id, file.filename or "", data)
            return {**doc, "duplicate": duplicate}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            await file.close()

    @app.get("/api/documents/{document_id}")
    def detail(document_id: str, user=Depends(current_user)):
        doc = document_access(user, document_id)
        doc["chunks"] = db.all("SELECT id,ordinal,page,text FROM chunks WHERE document_id=? ORDER BY ordinal", (document_id,))
        return doc

    @app.get("/api/documents/{document_id}/download")
    def download(document_id: str, user=Depends(current_user)):
        doc = document_access(user, document_id)
        return FileResponse(cfg.root / "uploads" / document_id, filename=doc["filename"], media_type="application/octet-stream")

    @app.post("/api/documents/{document_id}/reindex")
    def reindex(document_id: str, user=Depends(admin)):
        doc = document_access(user, document_id)
        if doc["status"] == "processing":
            raise HTTPException(409, "文档正在处理中")
        db.execute("UPDATE documents SET status='queued',error=NULL WHERE id=?", (document_id,))
        return {"status": "queued"}

    @app.delete("/api/documents/{document_id}")
    def delete_document(document_id: str, user=Depends(admin)):
        document_access(user, document_id)
        db.execute("DELETE FROM documents WHERE id=?", (document_id,))
        (cfg.root / "uploads" / document_id).unlink(missing_ok=True)
        return {"ok": True}

    @app.get("/api/search")
    def search(q: str, base_id: str | None = None, user=Depends(current_user)):
        if not 1 <= len(q.strip()) <= 500:
            raise HTTPException(400, "检索词长度需为 1–500 字符")
        ids = base_access(user, [base_id] if base_id else [])
        rows, warnings = Retriever(db, cfg).search(user, q, ids)
        return {"results": rows, "warnings": warnings}

    @app.get("/api/conversations")
    def conversations(user=Depends(current_user)):
        return db.all("SELECT * FROM conversations WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],))

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: str, user=Depends(current_user)):
        from .incidents import message_attachments
        row = db.one("SELECT * FROM conversations WHERE id=? AND user_id=?", (conversation_id, user["id"]))
        if not row:
            raise HTTPException(404, "会话不存在")
        row["messages"] = db.all("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (conversation_id,))
        row["runs"] = db.all("SELECT id,status,answer,citations,error,question,created_at,state,cancel_requested FROM runs WHERE conversation_id=? ORDER BY created_at", (conversation_id,))
        questions, attachments = {}, {}
        for message in row['messages']:
            if message['role'] == 'user':
                questions.setdefault(message['run_id'], message['content'])
        for item in message_attachments(db, user, conversation_id=conversation_id):
            attachments.setdefault(item.pop('run_id'), []).append(item)
        observations = {e['run_id']: json.loads(e['payload']) for e in db.all(
            "SELECT e.run_id,e.payload FROM events e JOIN runs r ON r.id=e.run_id "
            "WHERE r.conversation_id=? AND e.kind='incident_diagnosis' ORDER BY e.id", (conversation_id,))}
        from .business_status import can_resume_for
        for run in row["runs"]:
            if run['id'] in observations:
                run['diagnosis'] = observations[run['id']]
            run['question'] = questions.get(run['id'], run['question'])
            run['incident_inputs'] = attachments.get(run['id'], [])
            run['can_resume'] = can_resume_for(run, cfg.agent_policy)
            run.pop('state')
            run["citations"] = json.loads(run["citations"])
        return row

    @app.post("/api/chat", status_code=202)
    def chat(payload: Question, user=Depends(current_user)):
        question = payload.question.strip()
        if not question and not payload.incident_ids:
            raise HTTPException(400, "请输入消息或添加附件")
        ids = base_access(user, payload.base_ids)
        from .incidents import owned, requests_support_advice
        try:
            incidents=[json.loads(owned(db,user,ident)['payload'])for ident in dict.fromkeys(payload.incident_ids)]
        except LookupError:
            raise HTTPException(404,'故障材料不存在或不属于当前用户')from None
        if not ids and not incidents and cfg.agent_policy not in ('business_native_v1', 'business_deep_v1'):
            raise HTTPException(400, "请先创建可访问的知识库并上传文档")
        conversation_id = payload.conversation_id or uid()
        if payload.conversation_id and not db.one("SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user["id"])):
            raise HTTPException(404, "会话不存在")
        agent_question = question or '请阅读随消息发送的附件，说明其中能辨认的内容。'
        if payload.read_only_incident and not incidents:
            raise HTTPException(422, '截图或日志排查需要随问题附上故障材料')
        now, run_id = time.time(), uid()
        queued_event = {}
        if payload.client_message_id:
            run_id = uuid5(NAMESPACE_URL, f"kb-chat/{user['id']}/{payload.client_message_id}").hex
            queued_event['request_digest'] = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        try:
            with db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                if payload.client_message_id:
                    previous = c.execute("SELECT r.conversation_id,r.status,e.payload FROM runs r JOIN events e ON e.run_id=r.id "
                                         "WHERE r.user_id=? AND r.id=? AND e.kind='queued' ORDER BY e.id LIMIT 1",
                                         (user['id'], run_id)).fetchone()
                    if previous:
                        if json.loads(previous['payload']) != queued_event:
                            raise HTTPException(409, '该消息已发送，内容已变更')
                        return {'run_id':run_id, 'conversation_id':previous['conversation_id'], 'status':previous['status']}
                if c.execute("SELECT id FROM runs WHERE conversation_id=? AND status IN ('queued','running')", (conversation_id,)).fetchone():
                    raise HTTPException(409, "此会话还有任务正在执行，请等待或取消")
                if not payload.conversation_id:
                    title = question or incidents[0]['filename']
                    c.execute("INSERT INTO conversations VALUES(?,?,?,?)", (conversation_id, user["id"], title[:45], now))
                c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (uid(), conversation_id, "user", question, run_id, now))
                initial_state = {}
                if cfg.agent_policy == 'business_native_v1':
                    grounded = payload.knowledge_grounded or payload.read_only_incident or bool(
                        incidents and ids and requests_support_advice(question))
                    initial_state = {'input_contract': {'knowledge_grounded': grounded}}
                c.execute("INSERT INTO runs(id,conversation_id,user_id,question,base_ids,mode,status,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?,?)",
                          (run_id, conversation_id, user["id"], agent_question, encode(ids), cfg.mode, encode(initial_state), now, now))
                c.executemany('INSERT INTO run_incidents VALUES(?,?,?)',
                    [(run_id,item['id'],item['digest'])for item in incidents])
                if payload.read_only_incident or (incidents and cfg.agent_policy not in ('business_native_v1', 'business_deep_v1')):
                    event = 'incident_request' if payload.read_only_incident else 'attachment_request'
                    intake = {'question': agent_question, 'answers': {item['id']: {} for item in incidents}}
                    c.execute('INSERT INTO events(run_id,kind,payload,created_at) VALUES(?,?,?,?)',
                              (run_id,event,encode(intake),now))
                c.execute('INSERT INTO events(run_id,kind,payload,created_at) VALUES(?,?,?,?)',
                          (run_id,'queued',encode(queued_event),now))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "会话状态冲突，请刷新后重试") from None
        app.state.workers.notify_run()
        return {"run_id": run_id, "conversation_id": conversation_id, "status":"queued"}

    @app.post('/api/runs/{run_id}/clarification', status_code=202)
    def answer_clarification(run_id: str, reply: IncidentAnswer, user=Depends(current_user)):
        from .incidents import FIELD_LABELS, UNKNOWN_REPLY
        run = own_run(user, run_id)
        value = reply.value.strip()
        if not value:
            raise HTTPException(422, '请填写所询问的字段')
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            latest = c.execute("SELECT e.payload FROM events e JOIN runs r ON r.id=e.run_id "
                "WHERE r.id=? AND r.status='clarify' AND e.kind='incident_diagnosis' ORDER BY e.id DESC LIMIT 1",
                (run_id,)).fetchone()
            diagnosis = json.loads(latest['payload']) if latest else {}
            prompt = diagnosis.get('clarification', {})
            if (prompt.get('incident_id'),prompt.get('field')) != (reply.incident_id,reply.field):
                raise HTTPException(409, '这项追问已经更新，请查看当前任务')
            if c.execute("SELECT 1 FROM runs WHERE conversation_id=? AND id<>? AND status IN ('queued','running')",
                         (run['conversation_id'],run_id)).fetchone():
                raise HTTPException(409, '此会话还有任务正在执行，请稍后补充')
            answers = {item['id']:item['answers'] for item in diagnosis['observations']}
            if not UNKNOWN_REPLY.fullmatch(value):
                answers[reply.incident_id][reply.field] = value
            now = time.time()
            c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                      (uid(),run['conversation_id'],'user',f'{FIELD_LABELS[reply.field]}：{value}',run_id,now))
            c.execute("UPDATE runs SET status='queued',answer=NULL,updated_at=? WHERE id=?", (now,run_id))
            c.execute('INSERT INTO events(run_id,kind,payload,created_at) VALUES(?,?,?,?)',
                      (run_id,'incident_request',encode({'question':diagnosis['question'],'answers':answers}),now))
            cursor = c.execute('INSERT INTO events(run_id,kind,payload,created_at) VALUES(?,?,?,?)',
                               (run_id,'queued','{}',now)).lastrowid
        app.state.workers.notify_run()
        return {'run_id':run_id, 'conversation_id':run['conversation_id'], 'status':'queued', 'after':cursor}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str, user=Depends(current_user)):
        from .incidents import message_attachments
        run = own_run(user, run_id)
        from .business_status import can_resume_for
        run['can_resume'] = can_resume_for(run, cfg.agent_policy)
        run['incident_inputs'] = message_attachments(db, user, run_id=run_id)
        original = db.one("SELECT content FROM messages WHERE run_id=? AND role='user' ORDER BY created_at LIMIT 1", (run_id,))
        if original:
            run['question'] = original['content']
        run.pop("state")
        run["citations"] = json.loads(run["citations"])
        run["events"] = db.all("SELECT id,kind,payload,created_at FROM events WHERE run_id=? ORDER BY id", (run_id,))
        for event in run["events"]:
            event["payload"] = json.loads(event["payload"])
            if event['kind'] == 'incident_diagnosis':
                run['diagnosis'] = event['payload']
        return run

    @app.post('/api/runs/{run_id}/resume')
    def resume(run_id: str, user=Depends(current_user)):
        own_run(user, run_id)
        from .business_status import can_resume_for
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            run = dict(c.execute('SELECT * FROM runs WHERE id=? AND user_id=?', (run_id, user['id'])).fetchone())
            if not can_resume_for(run, cfg.agent_policy):
                raise HTTPException(409, '当前请求没有可恢复的步骤，或本次执行预算已用尽')
            if c.execute("SELECT 1 FROM runs WHERE conversation_id=? AND id<>? AND status IN ('queued','running')",
                         (run['conversation_id'], run_id)).fetchone():
                raise HTTPException(409, '此会话已有任务在执行，请等待完成后继续')
            state = json.loads(run['state'])
            state['business']['resume_requested'] = True
            c.execute("UPDATE runs SET status='queued',error=NULL,state=?,updated_at=? WHERE id=?",
                      (encode(state), time.time(), run_id))
        db.event(run_id, 'business_resumed', {'reuse_completed_tasks': True})
        app.state.workers.notify_run()
        return {'run_id': run_id, 'conversation_id': run['conversation_id'], 'status': 'queued'}

    @app.get("/api/runs/{run_id}/events")
    def stream_events(run_id: str, request: Request, after: int = 0, user=Depends(current_user)):
        own_run(user, run_id)
        try:
            cursor = max(0, after, int(request.headers.get('last-event-id', '0')))
            if cursor > 2**63 - 1:
                raise ValueError
        except ValueError:
            raise HTTPException(400, '无效的事件编号') from None
        async def events():
            last = cursor
            for _ in range(600):
                if await request.is_disconnected():
                    break
                rows = db.all("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, last))
                for row in rows:
                    last = row["id"]
                    yield f"id: {last}\ndata: {encode({'kind':row['kind'], 'payload':json.loads(row['payload'])})}\n\n"
                state = db.one("SELECT status FROM runs WHERE id=?", (run_id,))
                if state["status"] not in {"queued", "running"}:
                    final = run_detail(run_id, user)
                    if final["status"] not in {"queued", "running"}:
                        yield f"event: done\ndata: {encode(final)}\n\n"
                        break
                yield ": heartbeat\n\n"
                await asyncio.sleep(.5)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, user=Depends(current_user)):
        own_run(user, run_id)
        db.execute("UPDATE runs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=? AND status IN ('queued','running')", (run_id,))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/feedback")
    def feedback(run_id: str, payload: Feedback, user=Depends(current_user)):
        run = own_run(user, run_id)
        if payload.rating not in {-1, 1} or run["status"] not in {"answered", "insufficient", "clarify", "partial_success"}:
            raise HTTPException(400, "请对已完成的回答提交有效评价")
        db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?,?,?)", (run_id, user["id"], payload.rating, payload.comment, time.time()))
        return {"ok": True}

    @app.get("/api/feedback")
    def feedback_list(user=Depends(admin)):
        return db.all("""SELECT f.*,r.question,r.answer,u.username FROM feedback f JOIN runs r ON r.id=f.run_id
          JOIN users u ON u.id=f.user_id ORDER BY f.created_at DESC LIMIT 200""")

    web = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web, check_dir=False), name="static")

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    return app


app = create_app()
