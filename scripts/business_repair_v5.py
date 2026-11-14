"""Frozen targeted regression of the v5 repairs; no new training or model judge."""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import business_ops as old
import business_assessment as assessment
from business_assessment_metrics import counts, conjunction
from business_assessment_faults import run_faults
from business_assessment_services import Services

ART = old.ROOT/'artifacts/business_repair_v5_20261103'
BASE = old.ROOT/'artifacts/business_assessment_20261103/dev_v2_20261103'
CASE_IDS = ('three-domain', 'negative-attempt', 'corrected-environment',
            'consult-then-draft', 'cancel-pending', 'suggestion-not-fact')
POLICY = 'business_multi_v5'
CONTRACTS = {
    'three-domain/0': {'answer_patterns': ['令牌.*过期', '读(?:取)?权限', r'(?:2|两|二)\s*个?工作日', '项目负责人'],
                       'draft_literals': ['Windows', 'HTTPS', '401', '检查网络']},
    'negative-attempt/0': {'draft_literals': ['Ubuntu', 'HTTPS', '401', '尚未检查令牌权限', '没有清理缓存']},
    'corrected-environment/1': {'answer_patterns': ['(?:禁止|不能|不要).*令牌']},
    'corrected-environment/2': {'draft_literals': ['Ubuntu', 'HTTPS', '401'],
                               'title_literals': ['Ubuntu'], 'title_absent': ['Windows']},
    'consult-then-draft/0': {'answer_patterns': ['资产(?:编号|号|ID)|设备编号', '故障(?:现象|描述)', '联系.{0,4}时间']},
    'consult-then-draft/1': {'draft_literals': ['Windows', 'SSH', 'Permission denied', '检查网络', 'DEMO-172', '今天下午']},
    'cancel-pending/0': {'status': 'clarify'},
    'cancel-pending/1': {'answer_patterns': [r'(?:14|十四)\s*天']},
    'suggestion-not-fact/0': {'answer_patterns': ['令牌.*过期', '读(?:取)?权限']},
    'suggestion-not-fact/1': {'draft_literals': ['Windows', 'HTTPS', '401', '检查过网络'],
                              'draft_absent': ['已检查令牌', '已经检查令牌']},
}


def folder(tag):
    if not tag or not tag.replace('_', '').isalnum():
        raise ValueError('Invalid regression tag')
    return ART/tag


def freeze(target):
    original = assessment.read(BASE/'protocol.json')
    cases = [c for c in original['cases'] if c['id'] in CASE_IDS]
    if {c['id'] for c in cases} != set(CASE_IDS):
        raise ValueError('Frozen baseline lacks the requested regression cases')
    protocol = assessment.freeze(target, cases=cases, arms={'multi_v5': POLICY},
        scope='targeted repair regression; deterministic state and field contracts; no semantic model judge')
    declaration = {'baseline': str(BASE), 'baseline_protocol_sha256': hashlib.sha256((BASE/'protocol.json').read_bytes()).hexdigest(),
                   'case_ids': list(CASE_IDS), 'contracts': CONTRACTS, 'judge_used': False,
                   'scope': 'same saved questions and original rubric requirements; checks do not enter agent prompts'}
    declaration = json.loads(json.dumps(declaration, ensure_ascii=False))
    path = target/'contracts.json'
    if path.exists() and assessment.read(path) != declaration:
        raise RuntimeError('Regression contracts changed; use a new tag')
    old.write(path, declaration)
    return protocol


def checks(row, contract):
    if row is None:
        return {'recorded': None}
    result = {'execution': row['quality']['execution'], 'permissions': row['quality']['permission_checks'],
              'no_auto_submit': row['quality']['no_auto_submission'], 'real_business_state': row['business_state_ok']}
    body = '\n'.join(t['description'] for t in row['actual_tickets'])
    title = '\n'.join(t['title'] for t in row['actual_tickets'])
    for pattern in contract.get('answer_patterns', []):
        result['answer:'+pattern] = bool(re.search(pattern, row['answer'] or '', re.I | re.S))
    for field, text in [('draft', body), ('title', title)]:
        for literal in contract.get(field+'_literals', []):
            result[field+':'+literal] = bool(row['actual_tickets']) and literal in text
        for literal in contract.get(field+'_absent', []):
            result[field+':absent:'+literal] = literal not in text
    if 'status' in contract:
        result['expected_status'] = row['status'] == contract['status']
    return result


def report(target):
    protocol = assessment.read(target/'protocol.json')
    declaration = assessment.read(target/'contracts.json')
    baseline = assessment.all_records(BASE, 'rows')
    current = assessment.all_records(target, 'rows')
    result = {'scope': protocol['scope'], 'arms': {}, 'case_details': {}, 'judge_used': False}
    for arm, records in [('multi_v4', baseline), ('multi_v5', current)]:
        lookup = {(r['case_id'], r['turn']): r for r in records if r['arm'] == arm}
        case_scores, turn_scores, details = [], [], []
        for case in protocol['cases']:
            values = []
            for index in range(len(case['turns'])):
                row = lookup.get((case['id'], index))
                c = checks(row, declaration['contracts'].get(f'{case["id"]}/{index}', {}))
                value = conjunction(c.values())
                values.append(value)
                turn_scores.append(value)
                details.append({'case_id': case['id'], 'turn': index, 'run_id': row['run_id'] if row else None,
                                'passed': value, 'checks': c})
            case_scores.append(conjunction(values))
        result['arms'][arm] = {'conversation_contracts': counts(case_scores), 'turn_contracts': counts(turn_scores),
                               'model_calls': sum(r['calls'] for key, r in lookup.items() if key[0] in CASE_IDS)}
        result['case_details'][arm] = details
    p = target/'faults/summary.json'
    result['faults'] = assessment.read(p) if p.exists() else {'status': 'pending'}
    candidate = result['arms']['multi_v5']['conversation_contracts']
    faults = result['faults'].get('outcomes', {})
    result['all_targeted_checks_passed'] = candidate['passed'] == candidate['expected'] and faults.get('passed') == 8
    old.write(target/'summary.json', result)
    lines = ['# v5 定向修复回归', '',
             '复用原12场景中的6个场景（11轮）；原始v4回答只读重放同一组检查。',
             '检查真实业务状态、规定字段、否定指令和回执标题；通过只代表本组回归契约，不代表全面语义准确率。',
             '', '|版本|场景契约通过|请求契约通过|模型调用|', '|---|---|---|---|']
    for arm, s in result['arms'].items():
        c, t = s['conversation_contracts'], s['turn_contracts']
        lines.append(f'|{arm}|{c["passed"]}/{c["expected"]}（未知{c["unknown"]}）|{t["passed"]}/{t["expected"]}|{s["model_calls"]}|')
    lines += ['', '## 故障回归', json.dumps(result['faults'], ensure_ascii=False, indent=2), '', '## 未通过或待执行的检查']
    for arm, rows in result['case_details'].items():
        for row in rows:
            if row['passed'] != 1:
                lines.append(f'- {arm}/{row["case_id"]}/{row["turn"]}: '+json.dumps(row['checks'], ensure_ascii=False))
    (target/'report.md').write_text('\n'.join(lines)+'\n')
    return result


def capacity_candidate(rows):
    eligible = [r for r in rows if r['free_mib'] >= math.ceil(r['total_mib']*.78)+2048]
    return max(eligible, key=lambda r: r['free_mib'])['gpu'] if eligible else None


def wait_capacity(target, requested, hours):
    candidates = (0, 1, 6, 7) if requested == 'auto' else (int(requested),)
    deadline, previous, stable = time.monotonic()+hours*3600, None, 0
    while True:
        output = subprocess.check_output(['nvidia-smi', '-i', ','.join(map(str, candidates)),
            '--query-gpu=index,memory.total,memory.free', '--format=csv,noheader,nounits'], text=True, timeout=90)
        rows = []
        for line in output.strip().splitlines():
            gpu, total, free = map(int, line.split(','))
            rows.append({'gpu': gpu, 'total_mib': total, 'free_mib': free,
                         'required_free_mib': math.ceil(total*.78)+2048})
        selected = capacity_candidate(rows)
        stable = stable+1 if selected is not None and selected == previous else 1 if selected is not None else 0
        previous = selected
        assessment.update(target, status='waiting_for_gpu', stage='gpu_admission',
                          requested_gpu=requested, gpu=selected, capacity=rows, stable_samples=stable)
        print(json.dumps({'event': 'waiting_for_gpu_capacity', 'capacity': rows,
                          'selected': selected, 'stable_samples': stable}, ensure_ascii=False), flush=True)
        if selected is not None and stable >= 2:
            return selected
        if time.monotonic() >= deadline:
            raise TimeoutError('No GPU met the declared memory budget before the queue deadline')
        time.sleep(min(15, max(0, deadline-time.monotonic())))


def run(target, gpu, wait_hours):
    with (target/'queue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        services = None
        def stop(signum, frame):
            raise SystemExit(128+signum)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        try:
            protocol = freeze(target)
            selected = wait_capacity(target, gpu, wait_hours)
            if assessment.source_hashes() != protocol['source_hashes']:
                raise RuntimeError('Code changed while queued; no model started, use a new regression tag')
            services = Services(target, selected)
            assessment.update(target, status='running', stage='answer_model_startup', gpu=selected)
            services.start('answer')
            assessment.update(target, stage='v5_fault_regression')
            run_faults(target/'faults', policy=POLICY)
            assessment.generate(target, protocol, on_record=report)
            result = report(target)
            assessment.update(target, status='complete' if result['all_targeted_checks_passed'] else 'complete_with_failures',
                              current=None, stage=None,
                              recorded_turns=len(assessment.all_records(target, 'rows')),
                              expected_turns=sum(len(c['turns']) for c in protocol['cases']),
                              finished_unix=time.time(), report=str(target/'report.md'))
            print('V5 regression complete: '+str(target/'report.md'), flush=True)
        except BaseException as exc:
            assessment.update(target, status='stopped' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed',
                              error=str(exc), finished_unix=time.time())
            raise
        finally:
            if services:
                services.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['launch', 'run', 'status', 'stop', 'report'])
    parser.add_argument('--tag', default='regression_v1')
    parser.add_argument('--gpu', choices=('auto', '0', '1', '6', '7'), default='auto')
    parser.add_argument('--wait-hours', type=float, default=6)
    args = parser.parse_args()
    if args.wait_hours < 0:
        raise ValueError('Wait hours must be nonnegative')
    target = folder(args.tag)
    target.mkdir(parents=True, exist_ok=True)
    if args.action == 'run':
        run(target, args.gpu, args.wait_hours)
    elif args.action == 'launch':
        with (target/'launch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = assessment.read(target/'status.json') if (target/'status.json').exists() else {}
            pid = state.get('pid')
            if pid and assessment.v2.ticks(pid) == state.get('start_ticks'):
                raise RuntimeError('Regression already running')
            freeze(target)
            Services(target, args.gpu)
            with (target/'queue.log').open('a') as log:
                child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'run', '--tag', args.tag,
                    '--gpu', args.gpu, '--wait-hours', str(args.wait_hours)],
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            old.write(target/'status.json', {'pid': child.pid, 'start_ticks': assessment.v2.ticks(child.pid),
                'status': 'starting', 'process_group': child.pid, 'gpu': args.gpu, 'log': str(target/'queue.log')})
            print(json.dumps({'pid': child.pid, 'gpu': args.gpu, 'memory_fraction': .78,
                'log': str(target/'queue.log'), 'output': str(target), 'automatic_restart': False}, ensure_ascii=False, indent=2))
    elif args.action == 'report':
        print(json.dumps(report(target), ensure_ascii=False, indent=2))
    else:
        state = assessment.read(target/'status.json') if (target/'status.json').exists() else {}
        if state.get('status') in ('complete', 'complete_with_failures'):
            # Earlier progress snapshots were written before the last request.
            state['recorded_turns'] = len(assessment.all_records(target, 'rows'))
            if (target/'protocol.json').exists():
                state['expected_turns'] = sum(len(c['turns']) for c in assessment.read(target/'protocol.json')['cases'])
        pid = state.get('pid')
        live = bool(pid) and assessment.v2.ticks(pid) == state.get('start_ticks')
        if live:
            command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            live = str(Path(__file__).resolve()).encode() in command
        if args.action == 'stop' and live:
            os.killpg(pid, signal.SIGTERM)
            print('Stopped owned v5 regression group '+str(pid))
        else:
            print(json.dumps({**state, 'alive': live, 'output': str(target)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
