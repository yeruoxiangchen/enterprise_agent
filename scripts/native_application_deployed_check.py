"""Check the actual configured Web service with a separately owned test account."""
import argparse
import json
from pathlib import Path
import secrets
import time

import httpx

from kb.auth import create_user
from kb.db import Database


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8768')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();out=args.output.resolve()
    runtime=Path('/data/zjr/enterprise_kb_agent')
    if not out.is_relative_to(runtime)or out.exists():
        parser.error('Use a fresh report under /data/zjr/enterprise_kb_agent')
    out.parent.mkdir(parents=True,exist_ok=True)
    username='native-check-'+secrets.token_hex(4);password=secrets.token_urlsafe(20)
    db=Database(runtime/'knowledge.sqlite3')
    create_user(db,username,password,'member')
    credentials=out.with_name('deployed-check-credentials.json')
    credentials.write_text(json.dumps({'username':username,'password':password}))
    credentials.chmod(0o600)
    started=time.monotonic();report={'passed':False,'url':args.url,'scope':'one operational check on the configured application using a separate test account and existing public demo knowledge'}
    try:
        with httpx.Client(base_url=args.url,headers={'X-KB-Client':'web'},trust_env=False,timeout=15)as client:
            client.post('/api/login',json={'username':username,'password':password}).raise_for_status()
            me=client.get('/api/me').json()
            assert me['agent_policy']=='business_native_v1'and me['workflow']=='business_multi',me
            response=client.post('/api/chat',json={'question':'根据差旅制度，出差住宿每晚报销上限是多少？请给出资料依据。',
                'knowledge_grounded':True})
            response.raise_for_status();ident=response.json()['run_id']
            for _ in range(360):
                run=client.get('/api/runs/'+ident).json()
                if run['status']not in('queued','running'):break
                time.sleep(.5)
            else:raise TimeoutError('Configured application request did not finish in 180 seconds')
            checks={'new_policy':True,'answered':run['status']=='answered','has_real_citation':bool(run['citations']),
                'demo_amount_matches_source':'600'in(run['answer']or'')and any('600'in s['text']for s in run['citations']),
                'no_execution_error':run['error']is None,
                'no_unrequested_ticket':client.get('/api/tickets').json()==[]}
            report.update(passed=all(checks.values()),checks=checks,run_id=ident,
                seconds=time.monotonic()-started,policy=me['agent_policy'],run=run)
            if not report['passed']:raise RuntimeError('Configured application check failed; inspect report')
    except BaseException as exc:
        report['error']=type(exc).__name__+': '+str(exc)
        raise
    finally:
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in report.items()if k!='run'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
