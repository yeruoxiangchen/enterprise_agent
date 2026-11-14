"""Small real-inference queue measurements on an isolated, owned HTTP server."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

import business_ops as old
import business_v3_ops as ops
from business_assessment_metrics import counts, quantiles
from business_assessment_observe import instrument, observation
from kb.config import Settings
from kb.db import Database

QUESTIONS = ['SSH认证应该交公钥还是私钥？', '按演示差旅制度，住宿每晚上限是多少？',
             'HTTPS拉取返回401，根据演示资料应该先检查什么？', '按演示制度，测试设备最多能借多久？']


def serve(args):
    import uvicorn
    from kb.app import create_app
    cfg = replace(old.cfg(args.data_dir), agent_policy='business_multi_v4')
    with instrument():
        uvicorn.run(create_app(cfg), host='127.0.0.1', port=args.port, log_level='warning')


def run_load(target):
    target.mkdir(parents=True, exist_ok=True)
    receipt = target/'summary.json'
    if receipt.exists() and json.loads(receipt.read_text()).get('status') == 'complete':
        return json.loads(receipt.read_text())
    if (target/'started.json').exists():
        raise RuntimeError('Interrupted load batch exists; inspect saved runs instead of duplicating requests')
    cfg, db = ops.prepare(target/'app', True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    command = [sys.executable, '-B', str(Path(__file__).with_name('business_assessment.py')), 'serve',
               '--data-dir', str(target/'app'), '--port', str(port)]
    result = {'status': 'running', 'url': url, 'batches': [], 'qa_workers': 1,
              'scope': '7 real questions; end-to-end includes single QA worker queue; descriptive small-sample latency'}
    old.write(target/'started.json', {'pid': os.getpid(), 'url': url, 'requests': 7})
    proc = None
    with (target/'server.log').open('a') as log:
        try:
            proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            for _ in range(90):
                if proc.poll() is not None:
                    raise RuntimeError('Owned queue-assessment server exited during startup')
                try:
                    if httpx.get(url+'/api/health', timeout=2, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise TimeoutError('Owned queue-assessment server did not start in 90 seconds')
            credentials = dict(s.split('：', 1) for s in (target/'app/credentials.txt').read_text().splitlines())
            with httpx.Client(base_url=url, timeout=15, trust_env=False, headers={'X-KB-Client': 'web'}) as client:
                response = client.post('/api/login', json={'username': 'demo-member', 'password': credentials['密码']})
                response.raise_for_status()
                cookies = dict(client.cookies)

            def request(index):
                start = time.monotonic()
                with httpx.Client(base_url=url, timeout=15, trust_env=False, cookies=cookies,
                                  headers={'X-KB-Client': 'web'}) as client:
                    response = client.post('/api/chat', json={'question': QUESTIONS[index % len(QUESTIONS)]})
                    response.raise_for_status()
                    rid = response.json()['run_id']
                    accepted = time.monotonic()-start
                    deadline = time.monotonic()+600
                    while time.monotonic() < deadline:
                        response = client.get('/api/runs/'+rid)
                        response.raise_for_status()
                        row = response.json()
                        if row['status'] not in ('queued', 'running'):
                            obs = observation(db, rid)
                            return {'run_id': rid, 'status': row['status'], 'http_accept_seconds': accepted,
                                    'client_end_to_end_seconds': time.monotonic()-start,
                                    'queue_seconds': obs['queue_seconds'], 'execution_seconds': obs['execution_seconds'],
                                    'usage': obs['usage'], 'has_citations': bool(row['citations'])}
                        time.sleep(.3)
                    return {'run_id': rid, 'status': 'assessment_timeout',
                            'client_end_to_end_seconds': time.monotonic()-start}

            for concurrency in (1, 2, 4):
                print(f'real question queue: concurrency={concurrency}; requests={concurrency}', flush=True)
                started = time.monotonic()
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    rows = list(pool.map(request, range(concurrency)))
                wall = time.monotonic()-started
                batch = {'concurrent_submissions': concurrency, 'requests': rows,
                         'execution_outcomes': counts(r['status'] in ('answered', 'clarify', 'insufficient') for r in rows),
                         'end_to_end': quantiles(r['client_end_to_end_seconds'] for r in rows),
                         'queue': quantiles(r['queue_seconds'] for r in rows if r.get('queue_seconds') is not None),
                         'execution': quantiles(r['execution_seconds'] for r in rows if r.get('execution_seconds') is not None),
                         'wall_seconds': wall, 'observed_completed_requests_per_second': len(rows)/wall}
                result['batches'].append(batch)
                old.write(receipt, result)
            result['status'] = 'complete'
            # Terminal run status becomes visible before the execute() wrapper
            # has emitted its final timing event. Read these after the batch.
            from business_assessment_audit import finalized_load
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                refreshed = finalized_load(result, db)
                if all(r.get('execution_seconds') is not None for b in refreshed['batches'] for r in b['requests']):
                    result = refreshed
                    break
                time.sleep(.2)
            old.write(receipt, result)
            return result
        except BaseException as exc:
            result.update(status='stopped' if isinstance(exc, (SystemExit, KeyboardInterrupt)) else 'failed', error=str(exc))
            old.write(receipt, result)
            raise
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
