"""Live-model HTTP acceptance for screenshot/log -> retrieval -> confirmed draft.

Uses the actual application, OCR, ACLs, database, selected execution core and local LLM.
The two fixtures are development cases; all generated files belong in /data.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',required=True,help='Local OpenAI base URL, including /v1')
    parser.add_argument('--model',default='crm-qwen3-8b')
    parser.add_argument('--output',required=True)
    parser.add_argument('--policy',default='business_native_v1',choices=['business_native_v1','business_deep_v1'])
    parser.add_argument('--goal-review',action=argparse.BooleanOptionalAction,default=False,
        help='Enable the native model-based completion critic (on/off development comparison)')
    args=parser.parse_args()
    if args.policy=='business_native_v1' and args.goal_review:
        parser.error('Native fork application uses the reviewed configuration without an extra critic')
    folder=Path(args.output).resolve()
    if not folder.is_relative_to('/data/zjr/enterprise_kb_agent'):
        parser.error('Outputs belong under /data/zjr/enterprise_kb_agent')
    folder.mkdir(parents=True,exist_ok=True)
    if (folder/'started.json').exists():parser.error('Use a fresh output folder')
    (folder/'started.json').write_text(json.dumps({'time':time.time(),'pid':os.getpid()}))
    os.environ['KB_DATA_DIR']=str(folder/'application')
    from fastapi.testclient import TestClient
    from PIL import Image,ImageDraw,ImageFont
    from kb.agent import Harness
    from kb.app import create_app
    from kb.auth import create_user
    from kb.config import Settings
    from kb.db import Database
    from kb.ingestion import ingest
    cfg=Settings(root=folder/'application',mode='agent',agent_policy=args.policy,
        supervisor_strategy='baseline',llm_url=args.url,llm_model=args.model,llm_key='local',
        tokenize_url=args.url.removesuffix('/v1')+'/tokenize',context_tokens=16384,
        output_tokens=1400,timeout=90,embedding_url='',embedding_model='',reranker_path='',
        harness_goal_review=args.goal_review,
        incident_ocr_backend='tesseract',incident_ocr_languages='eng')
    cfg.prepare();db=Database(cfg.db_path);db.initialize()
    password=secrets.token_urlsafe(20)
    create_user(db,'acceptance_admin',password,'admin')
    member=create_user(db,'acceptance_member',password,'member')
    rows=[]
    with TestClient(create_app(cfg,start_workers=False),headers={'X-KB-Client':'web'})as client:
        client.post('/api/login',json={'username':'acceptance_admin','password':password}).raise_for_status()
        base=client.post('/api/bases',json={'name':'VPN处理知识库'}).json()['id']
        content='VPN HTTP 401 Authentication failed on Windows 11. Check the account expiry, renew authentication and retry. Never disclose access tokens.'
        response=client.post('/api/documents',data={'base_id':base},files={'file':('VPN.md',content.encode())})
        response.raise_for_status();ingest(db,cfg,response.json()['id'])
        client.post('/api/login',json={'username':'acceptance_member','password':password}).raise_for_status()
        text='Windows 11\nVPN HTTP 401\nAuthentication failed\naccess_token=do-not-persist-this-secret'
        image=Image.new('RGB',(1200,340),'#f5f5f5')
        draw=ImageDraw.Draw(image);font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',25)
        draw.multiline_text((30,30),text,fill='black',font=font,spacing=20)
        image.save(folder/'fixture.png')
        buffer=io.BytesIO();image.save(buffer,format='PNG')
        for kind,payload in [('log',text.encode()),('screenshot',buffer.getvalue())]:
            started=time.monotonic()
            response=client.post('/api/incident-evidence',files={'file':('VPN.'+('log'if kind=='log'else'png'),payload)})
            response.raise_for_status();incident=response.json()
            question=(f"请先读取故障材料 {incident['id']}，再检索VPN故障处理文档，给出带引用的建议。"
                      '然后根据材料创建标题为“VPN登录故障”的工单草稿，包含操作系统、错误码和故障现象，等待我确认提交。')
            response=client.post('/api/chat',json={'question':question,'base_ids':[base],'incident_ids':[incident['id']],
                'knowledge_grounded':True})
            response.raise_for_status();ident=response.json()['run_id']
            Harness(db,cfg).execute(ident)
            run=client.get('/api/runs/'+ident).json()
            tickets=db.all('SELECT * FROM service_tickets WHERE source_run=?',(ident,))
            effects=[]
            conversation_root=cfg.root/('harness/native_application'if args.policy=='business_native_v1'else'harness/conversations')/run['conversation_id']
            for p in conversation_root.glob('*/effects.sqlite3'):
                con=sqlite3.connect(f'file:{p}?mode=ro',uri=True);con.row_factory=sqlite3.Row
                effects.extend(dict(r)for r in con.execute('SELECT * FROM effects ORDER BY created'))
                con.close()
            names=[e['name']for e in effects]
            assertions={'answered':run['status']=='answered',
                'owned_incident_read':'read_incident'in names,
                'knowledge_retrieved':'search_documents'in names,
                'valid_document_citation':bool(run.get('citations')),
                'one_owned_draft':len(tickets)==1 and tickets[0]['user_id']==member and tickets[0]['status']=='draft',
                'grounded_description':len(tickets)==1 and all(s in tickets[0]['description']for s in ('VPN','401','Windows')),
                'secret_redacted':'do-not-persist-this-secret'not in json.dumps(incident),
                'not_auto_submitted':not any(t['status']=='submitted'for t in tickets)}
            if len(tickets)==1:
                t=tickets[0]
                submitted=client.post('/api/tickets/'+t['id']+'/action',json={'action':'submit','version':t['version']})
                assertions['explicit_confirmation_submits']=submitted.status_code==200 and submitted.json()['status']=='submitted'
            row={'case':kind,'passed':all(assertions.values()),'assertions':assertions,
                 'seconds':time.monotonic()-started,'run':run,'effects':effects,'incident':incident,'tickets':tickets}
            rows.append(row)
            (folder/(kind+'.json')).write_text(json.dumps(row,ensure_ascii=False,indent=2))
            print(json.dumps({'case':kind,'passed':row['passed'],'assertions':assertions},ensure_ascii=False),flush=True)
    report={'scope':'two local development cases; live local model; actual HTTP/OCR/DB; no quality benchmark',
            'agent_policy':args.policy,
            'goal_review':args.goal_review,
            'passed':sum(r['passed']for r in rows),'total':len(rows),'rows':rows}
    (folder/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if not all(r['passed']for r in rows):raise SystemExit(1)


if __name__=='__main__':main()
