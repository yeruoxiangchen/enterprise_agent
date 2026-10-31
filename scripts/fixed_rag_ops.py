"""Controlled local application switch; experiments and model services are untouched."""
import argparse
import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import shlex
import sqlite3
import subprocess
import time
import httpx
from kb.config import Settings
from kb.cli import server_state, write_private

PROJECT=Path(__file__).resolve().parents[1]
ROOT=Settings().root
TARGET=ROOT/'artifacts/fixed_rag_release_20261030'


def revision():
    paths=[*sorted((PROJECT/'kb').rglob('*.py')),PROJECT/'kb/web/app.js',Path(__file__),PROJECT/'scripts/run.sh']
    return hashlib.sha256(b''.join(str(p.relative_to(PROJECT)).encode()+p.read_bytes() for p in paths)).hexdigest()


def active():
    with sqlite3.connect(f'file:{ROOT}/knowledge.sqlite3?mode=ro',uri=True) as c:
        return {'runs':c.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0],
                'documents':c.execute("SELECT count(*) FROM documents WHERE status IN ('queued','processing')").fetchone()[0]}


def app_command(action):
    return subprocess.run(['bash',str(PROJECT/'scripts/run.sh'),action],check=True,capture_output=True,text=True)


def config_text(values):return ''.join(f'export {k}={shlex.quote(str(v))}\n' for k,v in values.items())


def online_check(expected='fixed_rag'):
    cfg=Settings();s,alive=server_state(cfg)
    if not alive:raise RuntimeError('Application not alive')
    with httpx.Client(base_url=s['url'],trust_env=False,timeout=15,headers={'X-KB-Client':'web'}) as client:
        h=client.get('/api/health');h.raise_for_status()
        if h.json().get('pid')!=s['pid']:raise RuntimeError('Unexpected server PID')
        values=dict(line.split('：',1) for line in (ROOT/'credentials.txt').read_text().splitlines() if '：' in line)
        response=client.post('/api/login',json={'username':values['账号'],'password':values['密码']})
        if response.status_code!=200:raise RuntimeError('Existing login verification failed; credentials unchanged')
        try:
            me=client.get('/api/me');me.raise_for_status();profile=me.json()
            if expected and (profile.get('workflow')!=expected or not profile.get('reranking')):
                raise RuntimeError('Workflow or reranker configuration not active')
            bases=client.get('/api/bases');bases.raise_for_status()
            docs=client.get('/api/documents');docs.raise_for_status()
            page=client.get('/');page.raise_for_status()
            return {'pid':s['pid'],'url':s['url'],'workflow':profile.get('workflow'),
                'reranking':profile.get('reranking'),'model':profile.get('model'),
                'bases':len(bases.json()),'documents':len(docs.json()),'login':'passed','page_http':page.status_code}
        finally:client.post('/api/logout')


def smoke():
    tested_revision=revision()
    from fastapi.testclient import TestClient
    from kb.app import create_app
    from kb.auth import create_user
    from kb.db import Database
    from kb.ingestion import ingest
    from kb.agent import Harness
    target=TARGET/('smoke-'+str(time.time_ns()))
    cfg=replace(Settings(),root=target,mode='agent',agent_policy='fixed_rag_v1',
        reranker_path=str(ROOT/'models/bge-reranker-base'),timeout=120,
        embedding_url='',embedding_model='',embedding_key='')
    cfg.prepare();db=Database(cfg.db_path);db.initialize();password=secrets.token_urlsafe(24)
    create_user(db,'release-admin',password,'admin');create_user(db,'release-member',password,'member')
    results=[]
    with TestClient(create_app(cfg,start_workers=False),headers={'X-KB-Client':'web'}) as client:
        assert client.post('/api/login',json={'username':'release-admin','password':password}).status_code==200
        public=client.post('/api/bases',json={'name':'发布验收公开资料'}).json()['id']
        private=client.post('/api/bases',json={'name':'管理员资料','visibility':'admin'}).json()['id']
        for base,name,text in [(public,'虚构测试政策.md','本资料为虚构验收政策。员工出差住宿报销上限为每晚600元。报销申请在出差结束后15天内提交。'),
                               (private,'内部验收码.md','内部验收识别码 PRIVATE_RELEASE_CANARY_4971。')]:
            response=client.post('/api/documents',data={'base_id':base},files={'file':(name,text.encode())});assert response.status_code==202
            ingest(db,cfg,response.json()['id'])
        assert client.post('/api/login',json={'username':'release-member','password':password}).status_code==200
        assert client.get('/api/me').json()['workflow']=='fixed_rag'
        assert client.post('/api/chat',json={'question':'内部验收识别码','base_ids':[private]}).status_code==403
        for q,expected in [('出差住宿每晚上限是多少？','answered'),('本公司生产 Git 服务器的真实网址是什么？','insufficient')]:
            response=client.post('/api/chat',json={'question':q,'base_ids':[public]});assert response.status_code==202
            ident=response.json()['run_id'];Harness(db,cfg).execute(ident)
            run=client.get('/api/runs/'+ident).json();saved=db.one('SELECT state FROM runs WHERE id=?',(ident,))
            state=json.loads(saved['state']);pack=state.get('pack',{})
            result={'question':q,'status':run['status'],'answer':run.get('answer'),'citations':len(run.get('citations',[])),
                'model_calls':pack.get('calls'),'tool_calls':pack.get('tool_calls'),'extra_searches':pack.get('extra_searches',0)}
            results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
            assert run['status']==expected and pack.get('calls')==1 and pack.get('tool_calls')==0
            assert not pack.get('extra_searches') and 'PRIVATE_RELEASE_CANARY' not in (run.get('answer') or '')
            if expected=='answered':
                assert run['citations'] and '600' in run['answer']
                assert client.post('/api/runs/'+ident+'/feedback',json={'rating':1,'comment':'固定流程发布验收'}).status_code==200
        if revision()!=tested_revision:raise RuntimeError('Sources changed during smoke; rerun validation')
        receipt={'passed':True,'revision':tested_revision,'scope':'isolated_real_model_api_smoke_not_business_accuracy','cases':results,'directory':str(target)}
        write_private(target/'receipt.json',json.dumps(receipt,ensure_ascii=False,indent=2))
        TARGET.mkdir(parents=True,exist_ok=True)
        write_private(TARGET/'smoke_receipt.json',json.dumps(receipt,ensure_ascii=False,indent=2))
        print('Isolated API smoke passed; live data unchanged',flush=True)


def deploy():
    receipt=json.loads((TARGET/'smoke_receipt.json').read_text())
    if not receipt['passed']:raise RuntimeError('Real-model smoke required')
    if receipt.get('revision')!=revision():raise RuntimeError('Sources changed since smoke; revalidate before switching')
    busy=active()
    if any(busy.values()):raise RuntimeError('Active application jobs; defer switch: '+str(busy))
    state,alive=server_state(Settings())
    if not alive:raise RuntimeError('Expected existing owned application for controlled switch')
    raw=Path(f"/proc/{state['pid']}/environ").read_bytes()
    oldenv=dict(x.decode().split('=',1) for x in raw.split(b'\0') if b'=' in x)
    command=Path(f"/proc/{state['pid']}/cmdline").read_bytes().split(b'\0')
    if b'kb.app:app' not in command:raise RuntimeError('Owned service command mismatch')
    target=TARGET/('deploy-'+str(time.time_ns()));target.mkdir(parents=True)
    rollback={k:oldenv.get(k,v) for k,v in {'KB_MODE':'extractive','KB_AGENT_POLICY':'bounded_v2',
        'KB_RERANKER_PATH':'','KB_LLM_TIMEOUT_SECONDS':'45','KB_HOST':state['host'],'KB_PORT':state['port'],
        'KB_EMBED_BASE_URL':'','KB_EMBED_MODEL':''}.items()}
    rollback['KB_APP_PYTHON']=command[0].decode()
    write_private(target/'rollback.env',config_text(rollback))
    if (ROOT/'app.env').exists():write_private(target/'previous_app.env',(ROOT/'app.env').read_text())
    # Readiness checks before interrupting the owned web process.
    if not (ROOT/'models/bge-reranker-base/receipt.json').exists():raise RuntimeError('Reranker missing')
    candidate={**rollback,'KB_MODE':'agent','KB_AGENT_POLICY':'fixed_rag_v1',
        'KB_RERANKER_PATH':str(ROOT/'models/bge-reranker-base'),'KB_LLM_TIMEOUT_SECONDS':'120',
        'KB_APP_PYTHON':str(ROOT/'venv-agentpack/bin/python'),'KB_EMBED_BASE_URL':'','KB_EMBED_MODEL':'',
        'HAYSTACK_TELEMETRY_ENABLED':'false','OMP_NUM_THREADS':'4','OPENBLAS_NUM_THREADS':'1','TOKENIZERS_PARALLELISM':'false'}
    app_command('stop')
    if server_state(Settings())[1]:raise RuntimeError('Application still stopping; config not modified')
    try:
        if any(active().values()):raise RuntimeError('Jobs arrived during drain; retaining previous configuration')
        with sqlite3.connect(ROOT/'knowledge.sqlite3') as src,sqlite3.connect(target/'knowledge.sqlite3') as dst:src.backup(dst)
        write_private(ROOT/'app.env',config_text(candidate))
        app_command('start');verification=online_check()
        result={'status':'deployed','policy':'fixed_rag_v1','backup':str(target),'verification':verification,'at':time.time()}
    except BaseException:
        app_command('stop');write_private(ROOT/'app.env',(target/'rollback.env').read_text());app_command('start')
        write_private(target/'result.json',json.dumps({'status':'rolled_back','at':time.time()}))
        raise
    write_private(target/'result.json',json.dumps(result,ensure_ascii=False,indent=2))
    write_private(TARGET/'latest.json',json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['inspect','smoke','deploy','verify']);a=p.parse_args()
    if a.action=='inspect':
        s,alive=server_state(Settings());print(json.dumps({'server':s,'alive':alive,'active':active(),'app_config_exists':(ROOT/'app.env').exists()},ensure_ascii=False))
    elif a.action=='smoke':smoke()
    elif a.action=='deploy':deploy()
    else:print(json.dumps(online_check(),ensure_ascii=False,indent=2))
