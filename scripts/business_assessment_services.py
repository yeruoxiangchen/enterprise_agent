"""Sequential use of one GPU; only stop services started by this assessment."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx

import business_ops as old
import business_v2_ops as v2


class Services:
    def __init__(self, target, gpu):
        self.target, self.gpu, self.owned = target, gpu, {}
        self.snapshot = {}
        for role, name in [('answer', 'model_service.json'), ('judge', 'judge_service.json')]:
            state = json.loads((old.ROOT/name).read_text())
            if v2.ticks(state['pid']) == state['start_ticks']:
                raise RuntimeError(f'{role} service is already owned by another invocation; use existing-service mode')
            self.snapshot[role] = state

    def save(self):
        old.write(self.target/'service_leases.json', {'gpu': self.gpu, 'memory_fraction': .78,
                  'owned': self.owned, 'policy': 'sequential answer/judge; cleanup only matching owned PID/start ticks'})

    def start(self, role):
        previous = self.snapshot[role]
        filename = 'model_service.json' if role == 'answer' else 'judge_service.json'
        current = json.loads((old.ROOT/filename).read_text())
        if v2.ticks(current['pid']) == current['start_ticks']:
            raise RuntimeError('A project model service started concurrently; assessment will not replace it')
        env = {**os.environ, 'PYTHONPATH': str(old.PROJECT), 'KB_DATA_DIR': str(old.ROOT)}
        command = [sys.executable, '-B', str(old.PROJECT/'scripts/model_service.py'), 'start',
                   '--role', role, '--gpu', str(self.gpu), '--memory-fraction', '.78',
                   '--port', str(int(previous['url'].rsplit(':', 1)[1])), '--model', previous['model_physical_path']]
        print(f'Starting owned {role} service on GPU {self.gpu}, memory fraction=0.78', flush=True)
        with (self.target/f'{role}_service_start.log').open('a') as log:
            proc = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = proc.wait(timeout=45)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
                state = json.loads((old.ROOT/filename).read_text())
                if state.get('pid') != current.get('pid') and state.get('model_physical_path') == previous['model_physical_path']:
                    self.owned[role] = {k: state[k] for k in ('pid', 'start_ticks', 'model', 'url', 'log')}
                    self.save()
        if code:
            raise RuntimeError(f'{role} startup command failed ({code}); see {role}_service_start.log')
        lease = self.owned[role]
        deadline = time.monotonic()+600
        last = 0
        while time.monotonic() < deadline:
            if v2.ticks(lease['pid']) != lease['start_ticks']:
                raise RuntimeError(f'{role} service exited while loading; see {lease["log"]}')
            try:
                if httpx.get(lease['url']+'/health', timeout=3, trust_env=False).status_code == 200:
                    print(f'Owned {role} model ready; PID={lease["pid"]}', flush=True)
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic()-last > 30:
                print(f'Waiting for owned {role} model startup; log={lease["log"]}', flush=True)
                last = time.monotonic()
            time.sleep(2)
        raise TimeoutError(f'{role} model did not become ready in 600 seconds')

    def stop(self, role):
        lease = self.owned.get(role)
        if not lease or lease.get('released'):
            return
        if v2.ticks(lease['pid']) == lease['start_ticks']:
            command = Path(f'/proc/{lease["pid"]}/cmdline').read_bytes()
            if lease['model'].encode() not in command or b'vllm.entrypoints.openai.api_server' not in command:
                raise RuntimeError('Owned model process command no longer matches')
            os.killpg(lease['pid'], signal.SIGTERM)
            deadline = time.monotonic()+30
            while v2.ticks(lease['pid']) == lease['start_ticks']:
                stat = Path(f'/proc/{lease["pid"]}/stat')
                if not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                    break
                if time.monotonic() > deadline:
                    os.killpg(lease['pid'], signal.SIGKILL)
                    break
                time.sleep(.5)
        lease['released'] = True
        self.save()
        # The allocator can be gone slightly after the API process exits.
        time.sleep(2)

    def close(self):
        for role in list(self.owned):
            self.stop(role)
