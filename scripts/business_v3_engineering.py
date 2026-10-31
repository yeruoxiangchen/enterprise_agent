"""Isolated live HTTP/browser acceptance and small concurrency measurements.

Own subprocess only, bounded 2 real questions. No restart of 8768/8772/model APIs.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
import httpx
import business_ops as old


def check(target,version='v3',port=8773):
    target.mkdir(parents=True,exist_ok=True)
    url=f'http://127.0.0.1:{port}';proc=None;result={'status':'running','url':url,'load':[]}
    log=(target/'server.log').open('a')
    try:
        with httpx.Client(timeout=2,trust_env=False) as probe:
            try:
                probe.get(url+'/api/health')
                raise RuntimeError(f'{port}已被占用，不停止未知服务')
            except httpx.ConnectError:pass
        proc=subprocess.Popen(['bash',str(old.PROJECT/'scripts/run_business.sh'),version,'serve','--data-dir',str(target/'app')],stdout=log,stderr=log)
        for _ in range(90):
            if proc.poll() is not None:raise RuntimeError('验收网页启动失败')
            try:
                if httpx.get(url+'/api/health',timeout=2,trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(1)
        else:raise RuntimeError('验收网页未就绪')
        credentials=dict(line.split('：',1) for line in (target/'app/credentials.txt').read_text().splitlines())
        with httpx.Client(base_url=url,timeout=10,trust_env=False,headers={'X-KB-Client':'web'}) as client:
            r=client.post('/api/login',json={'username':credentials['账号'],'password':credentials['密码']});r.raise_for_status()
            cookies=dict(client.cookies)
        def query_endpoint(i):
            start=time.monotonic()
            with httpx.Client(base_url=url,timeout=10,trust_env=False,cookies=cookies,headers={'X-KB-Client':'web'}) as c:
                response=c.get(('/api/me','/api/bases','/api/tickets')[i%3]);response.raise_for_status()
            return time.monotonic()-start
        for concurrency in (1,4,8):
            start=time.monotonic()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:times=list(pool.map(query_endpoint,range(24)))
            times.sort();elapsed=time.monotonic()-start
            result['load'].append({'concurrency':concurrency,'requests':24,'success':len(times),
                'p50_seconds':statistics.median(times),'p95_seconds':times[22],'observed_rps':24/elapsed,
                'scope':'authenticated read APIs, client creation included; not model generation throughput'})
        # Real questions use separate conversations; end-to-end latency includes the app queue.
        def model_question(question):
            start=time.monotonic()
            with httpx.Client(base_url=url,timeout=15,trust_env=False,cookies=cookies,headers={'X-KB-Client':'web'}) as client:
                response=client.post('/api/chat',json={'question':question});response.raise_for_status();rid=response.json()['run_id']
                for _ in range(240):
                    response=client.get('/api/runs/'+rid);response.raise_for_status();row=response.json()
                    if row['status'] not in ('queued','running'):
                        return {'run_id':rid,'status':row['status'],'seconds':time.monotonic()-start,'has_citations':bool(row['citations'])}
                    time.sleep(1)
            raise RuntimeError('真实问答验收超时')
        with ThreadPoolExecutor(max_workers=2) as pool:
            result['real_questions']=list(pool.map(model_question,['SSH认证应交公钥还是私钥？','按演示制度，住宿每晚上限是多少？']))
        if not all(r['status']=='answered' and r['has_citations'] for r in result['real_questions']):raise RuntimeError('真实并发问答未全部正常返回')
        env={**os.environ,'KB_BROWSER_URL':url,'KB_BROWSER_CREDENTIALS':str(target/'app/credentials.txt'),
            'KB_BROWSER_OUTPUT':str(target/'browser')};env.pop('PYTHONPATH',None)
        with (target/'browser.log').open('a') as browser_log:
            completed=subprocess.run([str(old.ROOT/'venv/bin/python'),'-B',str(old.PROJECT/'scripts/business_browser.py')],
                env=env,stdout=browser_log,stderr=browser_log,timeout=360)
        if completed.returncode:raise RuntimeError('浏览器验收失败，见browser.log')
        result['browser']=json.loads((target/'browser/report.json').read_text())
        result['status']='passed'
    except Exception as exc:
        result.update(status='failed',error=str(exc))
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=20)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
        log.close()
        (target/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result
