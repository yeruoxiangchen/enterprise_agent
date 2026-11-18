"""Local model traffic bypasses desktop proxies while remote proxy settings survive."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest


ENV_SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/rl_env.sh'
PROXY_KEYS = ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy',
              'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy')


def sourced(tmp_path, code, **overrides):
    env = {k: v for k, v in os.environ.items() if k not in PROXY_KEYS}
    env.update(KB_RL_ROOT=str(tmp_path / 'runtime'), **overrides)
    return subprocess.run(
        ['bash', '-c', 'source "$1"; source "$1"; exec "$2" -B -c "$3"',
         'rl-env-test', str(ENV_SCRIPT), sys.executable, code],
        env=env, capture_output=True, text=True, timeout=30)


@contextmanager
def endpoint(*, proxy=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path, body))
            payload = ({'error': {'message': 'Local request wrongly sent to proxy',
                                  'type': 'proxy_error', 'code': 'proxy_error'}} if proxy else
                       {'id': 'local-contract', 'object': 'chat.completion', 'created': 1,
                        'model': 'local-contract', 'choices': [{'index': 0,
                            'message': {'role': 'assistant', 'content': 'local-reached'},
                            'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
            encoded = json.dumps(payload).encode()
            self.send_response(502 if proxy else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost'])
def test_sync_and_async_chat_clients_reach_loopback_even_with_a_failing_proxy(tmp_path, host):
    with endpoint() as (port, direct), endpoint(proxy=True) as (proxy_port, proxied):
        proxy = f'http://127.0.0.1:{proxy_port}'
        code = f'''
import asyncio
from langchain_openai import ChatOpenAI
model = ChatOpenAI(model='local-contract', base_url='http://{host}:{port}/v1',
                   api_key='local-test', timeout=5, max_retries=0)
assert model.invoke('Check sync routing').content == 'local-reached'
assert asyncio.run(model.ainvoke('Check async routing')).content == 'local-reached'
'''
        result = sourced(tmp_path, code, HTTP_PROXY=proxy, http_proxy=proxy,
                         HTTPS_PROXY=proxy, https_proxy=proxy, ALL_PROXY=proxy, all_proxy=proxy)
        assert result.returncode == 0, result.stderr
        assert len(direct) == 2 and not proxied
        assert all(path == '/v1/chat/completions' for path, _ in direct)


def test_bypass_keeps_existing_domains_and_remote_proxy_configuration(tmp_path):
    result = sourced(tmp_path, '''
import json, os
print(json.dumps({key: os.environ[key] for key in
                 ('NO_PROXY', 'no_proxy', 'HTTP_PROXY', 'HTTPS_PROXY')}))
''', NO_PROXY='upper.internal', no_proxy='lower.internal',
        HTTP_PROXY='http://proxy.example:8888', HTTPS_PROXY='http://proxy.example:8888')
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    bypass = values['NO_PROXY'].split(',')
    assert set(bypass) == {'upper.internal', 'lower.internal', '127.0.0.1', 'localhost', '::1'}
    assert len(bypass) == len(set(bypass))
    assert values['no_proxy'] == values['NO_PROXY']
    assert values['HTTP_PROXY'] == values['HTTPS_PROXY'] == 'http://proxy.example:8888'
