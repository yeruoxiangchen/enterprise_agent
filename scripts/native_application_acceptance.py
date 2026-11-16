"""Bounded isolated Web acceptance, using an existing owned local model service."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx
from PIL import Image,ImageDraw,ImageFont

from kb.auth import create_user
from kb.config import Settings
from kb.db import Database
from kb.ingestion import ingest,register_document


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--model-url',required=True)
    p.add_argument('--model',default='crm-qwen3-8b')
    args=p.parse_args();root=args.output.resolve()
    if not root.is_relative_to('/data/zjr/enterprise_kb_agent'):
        p.error('Runtime data belongs under /data/zjr/enterprise_kb_agent')
    root.mkdir(parents=True,exist_ok=False)
    started=time.time();protocol={'started':started,'policy':'business_native_v1',
        'model':args.model,'context_tokens':16384,'output_tokens':1400,
        'scope':'isolated full Web flow with live local model; authored business development cases'}
    (root/'protocol.json').write_text(json.dumps(protocol,indent=2))
    cfg=Settings(root=root/'application',mode='agent',agent_policy='business_native_v1',
        llm_url=args.model_url,llm_model=args.model,
        embedding_url='',embedding_model='',reranker_path='')
    cfg.prepare();db=Database(cfg.db_path);db.initialize()
    password=secrets.token_urlsafe(20)
    for name,role in [('member','member'),('second','member'),('admin','admin')]:
        create_user(db,name,password,role)
    (root/'credentials.json').write_text(json.dumps({'password':password}))
    (root/'credentials.json').chmod(0o600)
    # Seed knowledge through the same registration/ingestion components used by HTTP upload.
    import uuid
    base=str(uuid.uuid4());now=time.time()
    with db.connect()as con:
        con.execute('INSERT INTO bases(id,name,description,visibility,created_at) VALUES(?,?,?,?,?)',
            (base,'VPN故障排查手册','本地演示资料','all',now))
    doc,_=register_document(db,cfg,base,'VPN认证故障.md',
        ('Windows 11 VPN出现HTTP 401或Authentication failed时，先核对账户有效期和访问权限，'
         '再更新认证凭据并重试。工单需保留系统版本、错误码和故障现象。不要上传密码或访问令牌。').encode())
    ingest(db,cfg,doc['id'])
    inputs=root/'inputs';inputs.mkdir()
    image=Image.new('RGB',(1200,340),'#f5f5f5')
    draw=ImageDraw.Draw(image);font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',28)
    draw.multiline_text((30,30),'Windows 11\nVPN HTTP 401\nAuthentication failed',font=font,fill='black',spacing=20)
    image.save(inputs/'vpn.png')
    with socket.socket()as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    url=f'http://127.0.0.1:{port}'
    env=dict(os.environ,KB_DATA_DIR=str(cfg.root),KB_MODE='agent',KB_AGENT_POLICY='business_native_v1',
        KB_LLM_BASE_URL=args.model_url,KB_LLM_MODEL=args.model,KB_LLM_API_KEY='local',
        KB_TOKENIZE_URL=args.model_url.removesuffix('/v1')+'/tokenize',KB_CONTEXT_TOKENS='16384',
        KB_OUTPUT_TOKENS='1400',KB_LLM_TIMEOUT_SECONDS='90',KB_HARNESS_GOAL_REVIEW='0',
        KB_EMBED_BASE_URL='',KB_EMBED_MODEL='',KB_RERANKER_PATH='',
        KB_INCIDENT_OCR_BACKEND='tesseract',KB_INCIDENT_OCR_LANGUAGES='eng')
    app=None;report={'status':'failed','started':started,'url':url}
    try:
        with (root/'server.log').open('a')as log:
            app=subprocess.Popen([sys.executable,'-B','-m','uvicorn','kb.app:app','--host','127.0.0.1',
                '--port',str(port),'--workers','1'],env=env,stdout=log,stderr=subprocess.STDOUT)
        (root/'server.json').write_text(json.dumps({'pid':app.pid,'url':url}))
        with httpx.Client(trust_env=False,timeout=2)as client:
            for _ in range(120):
                if app.poll()is not None:raise RuntimeError('Application startup failed; inspect server.log')
                try:
                    if client.get(url+'/api/health').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.5)
            else:raise TimeoutError('Application did not become ready')
        browser=Path(__file__).with_name('native_application_browser.py')
        command=['/data/zjr/enterprise_kb_agent/venv/bin/python','-B',str(browser),'--output',str(root),'--url',url]
        with (root/'browser.log').open('a')as log:
            subprocess.run(command,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=1800,check=True)
        browser_report=json.loads((root/'browser/report.json').read_text())
        rows=db.all('SELECT id,status,state FROM runs ORDER BY created_at')
        usages=[json.loads(r['state'])['business']['usage']for r in rows]
        report={'status':'passed','started':started,'finished':time.time(),'policy':'business_native_v1',
            'requests_passed':sum(r['status']=='answered'for r in rows),'requests_total':len(rows),
            'browser_checks':browser_report['checks'],'prompt_tokens':sum(u['prompt_tokens']for u in usages),
            'completion_tokens':sum(u['completion_tokens']for u in usages),
            'missing_usage_calls':sum(u['missing_usage_calls']for u in usages),
            'run_ids':[r['id']for r in rows],'database_ticket_events':db.all('SELECT payload FROM ticket_events'),
            'scope':protocol['scope']}
    except BaseException as exc:
        report.update(error=type(exc).__name__+': '+str(exc),finished=time.time())
        raise
    finally:
        if app is not None and app.poll()is None:
            app.terminate()
            try:app.wait(20)
            except subprocess.TimeoutExpired:app.kill();app.wait()
        report['application_released']=app is None or app.poll()is not None
        (root/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
