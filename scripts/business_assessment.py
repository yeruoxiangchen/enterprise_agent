"""Isolated business/context/recovery assessment. Reuses existing local model APIs."""
import argparse
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import httpx

import business_ops as old
import business_v2_ops as v2
import business_v3_ops as ops
from business_cases_v2 import DOCUMENTS
from business_assessment_cases import CASES, ARMS
from business_assessment_metrics import summarize, conjunction, counts
from business_assessment_observe import instrument, observation
from business_v4_review import AxisJudge, COMMON, PROMPTS, payload
from business_v3_judge import BusinessJudge as Transport

ART = old.ROOT/'artifacts/business_assessment_20261103'
ops.POLICIES.update(ARMS)
CONTEXT_PROMPT = '''只判断context_rubric中的上下文要求是否被保留并正确使用，不评其他知识质量。
question/user_history是用户原话；model_inputs是Agent实际收到的消息；actual_ticket是数据库记录。
区分历史说法与最新更正、否定与肯定、用户已做操作与助手建议。格式合法或包含关键词不等于满足要求。
若用户更正Windows为Ubuntu，历史原话可保留，但当前环境必须清楚；不得把建议写成已做。
answer包含本轮实际回答及实际草稿描述，可从其中摘录。所有要求满足才判1，遗漏或矛盾判0。'''


def read(path):
    return json.loads(path.read_text())


def directory(tag):
    if not tag or not tag.replace('_', '').isalnum():
        raise ValueError('Tag must contain only letters, digits and underscores')
    return ART/tag


def source_hashes():
    paths = sorted((old.PROJECT/'kb').rglob('*.py'))
    paths += sorted((old.PROJECT/'kb/business_skills').glob('*.md'))
    paths += [p for p in (old.PROJECT/'scripts').glob('business*.py')]
    paths += [old.PROJECT/'scripts/run_business.sh', old.PROJECT/'requirements.business.lock']
    return {str(p.relative_to(old.PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))}


def freeze(target, cases=None, arms=None, scope=None):
    target.mkdir(parents=True, exist_ok=True)
    cfg = old.cfg(target/'configuration_only')
    from kb.benchmark.semantic import model_identity
    identity = model_identity(old.ROOT, os.getenv('KB_JUDGE_BASE_URL', ''), os.getenv('KB_JUDGE_MODEL', ''))
    protocol = {'schema': 1, 'cases': CASES if cases is None else cases, 'arms': ARMS if arms is None else arms,
                'documents': old.DOCS+DOCUMENTS,
                'model_identity': identity,
                'source_hashes': source_hashes(), 'answer_model': cfg.llm_model,
                'answer_url': cfg.llm_url, 'judge_model': os.getenv('KB_JUDGE_MODEL'),
                'judge_url': os.getenv('KB_JUDGE_BASE_URL'),
                'budget': {'model_calls_per_request': 12, 'tool_calls_per_request': 15,
                           'context_tokens': cfg.context_tokens, 'output_tokens': cfg.output_tokens},
                'correctness_prompt': COMMON+PROMPTS['correctness'],
                'context_prompt': COMMON+CONTEXT_PROMPT,
                'scope': scope or 'authored development; semantic and context labels are model review',
                'comparison': 'same model/docs/tools/max budgets; actual compute reported separately'}
    # Tuples in authored document fixtures become JSON lists on disk.
    protocol = json.loads(json.dumps(protocol, ensure_ascii=False))
    receipt = target/'protocol.json'
    if receipt.exists():
        if read(receipt) != protocol:
            raise RuntimeError('Code, cases or model configuration changed; use a new tag')
    else:
        old.write(receipt, protocol)
        for name in protocol['source_hashes']:
            dest = target/'source'/name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old.PROJECT/name, dest)
    for name in ('attempts', 'rows', 'grades'):
        (target/name).mkdir(exist_ok=True)
    return protocol


def healthy(url, model, key=''):
    if not url or not model:
        raise ValueError('Local model endpoint and model name are required')
    headers = {'Authorization': 'Bearer '+key} if key else {}
    with httpx.Client(timeout=15, trust_env=False, headers=headers) as client:
        response = client.get(url.rstrip('/')+'/models')
        response.raise_for_status()
        if model not in {x['id'] for x in response.json()['data']}:
            raise ValueError('Configured model is absent from the local service')


def check():
    cfg = old.cfg(ART/'configuration_only')
    healthy(cfg.llm_url, cfg.llm_model, cfg.llm_key)
    healthy(os.getenv('KB_JUDGE_BASE_URL'), os.getenv('KB_JUDGE_MODEL'), os.getenv('KB_JUDGE_API_KEY', ''))
    judge = AxisJudge(old.ROOT)
    print(json.dumps({'answer_model': cfg.llm_model, 'judge_model': judge.backend.model,
                      'different_checkpoints': judge.identity['different_weight_files'],
                      'cases': len(CASES), 'turns_per_arm': sum(len(c['turns']) for c in CASES),
                      'new_gpu_models': 0}, ensure_ascii=False), flush=True)


def update(target, **values):
    previous = read(target/'status.json') if (target/'status.json').exists() else {}
    old.write(target/'status.json', {**previous, 'pid': os.getpid(), 'start_ticks': v2.ticks(os.getpid()),
                                  'updated_unix': time.time(), **values})


def all_records(target, subdir):
    return [read(p) for p in sorted((target/subdir).glob('*.json'))]


def save_record(target, subdir, row):
    dest = target/subdir/(hashlib.sha256(row['key'].encode()).hexdigest()[:24]+'.json')
    if dest.exists() and read(dest) != row:
        raise RuntimeError('Refusing to overwrite an archived record')
    old.write(dest, row)


def enrich(cfg, db, row, spec):
    row['observation'] = observation(db, row['run_id'])
    run = db.one('SELECT user_id FROM runs WHERE id=?', (row['run_id'],))
    tickets = db.all('SELECT * FROM service_tickets WHERE source_run=?', (row['run_id'],))
    row['actual_tickets'] = tickets
    expected = spec.get('action')
    row['business_state_ok'] = (len(tickets) == 1 and tickets[0]['user_id'] == run['user_id']
                               and tickets[0]['status'] == 'draft') if expected == 'draft' else len(tickets) == 0
    if expected in ('list', 'refuse'):
        row['business_state_ok'] &= row['quality']['service_complete'] is True
    row['context_rubric'] = spec.get('context_rubric')
    return row


def generate(target, protocol, on_record=None):
    existing = all_records(target, 'rows')
    done = {r['key']: r for r in existing}
    # Interleave arms by scenario; each arm has an independent DB and accounts.
    cases, arms = protocol['cases'], protocol['arms']
    ops.POLICIES.update(arms)
    databases = {arm: ops.prepare(target/'applications'/arm, True) for arm in arms}
    with instrument():
        for case in cases:
            for arm in arms:
                cfg, db = databases[arm]
                for i, spec in enumerate(case['turns']):
                    key = f'{arm}/{case["id"]}/{i}'
                    if key in done:
                        continue
                    prior = done.get(f'{arm}/{case["id"]}/{i-1}') if i else None
                    cid = prior['conversation_id'] if prior else None
                    if i and prior is None:
                        raise RuntimeError('Missing conversation prefix')
                    user = v2.seed_case_user(db, case['id'])
                    archived = {r['run_id'] for r in done.values() if r['arm'] == arm}
                    unarchived = db.all("SELECT id FROM runs WHERE user_id=? AND question!='预置工单'", (user['id'],))
                    if any(r['id'] not in archived for r in unarchived):
                        raise RuntimeError('An interrupted run is not archived; inspect this isolated DB before resuming')
                    update(target, status='running', stage='business_comparison', current=key,
                           recorded_turns=len(done), expected_turns=sum(len(c['turns']) for c in cases)*len(arms))
                    row = ops.evaluate_one(cfg, db, spec, case, arm, arm, i, cid)
                    row['expected_action'] = spec.get('action')
                    row = enrich(cfg, db, row, spec)
                    save_record(target, 'rows', row)
                    done[key] = row
                    print(f'{key}: {row["status"]}; calls={row["calls"]}; state_ok={row["business_state_ok"]}', flush=True)
                    (on_record or report)(target)


def context_job(row):
    draft = '\n'.join(t['description'] for t in row.get('actual_tickets', []))
    return {'question': row['question'], 'user_history': row['user_history'],
            'context_rubric': row['context_rubric'],
            'answer': (row['answer'] or '')+('\n实际工单描述：\n'+draft if draft else ''),
            'model_inputs': row['observation']['model_inputs'], 'actual_ticket': row['actual_tickets']}


def calibrate(judge, target):
    receipt = target/'calibration.json'
    if receipt.exists():
        result = read(receipt)
        if result['judge_identity'] != judge.identity:
            raise RuntimeError('Judge checkpoint changed')
        return result['enabled']
    # Five narrow checks; no disputed citation/fidelity axis in this assessment.
    content = {'question': 'HTTPS报401应该先查什么？', 'user_history': [],
               'rubric': '检查令牌过期及仓库读权限。',
               'reference_documents': [{'text': '401先查令牌是否过期及仓库读权限。'}]}
    contexts = {'question': '按更正后的情况生成草稿。',
                'user_history': [{'role': 'user', 'content': '先前说Windows，更正为Ubuntu。令牌检查还没做。'}],
                'context_rubric': '当前系统是Ubuntu，令牌检查未做，不能记为Windows或已经检查。'}
    jobs = [('correctness', dict(content, answer='检查令牌是否过期及仓库读权限。'), 1),
            ('correctness', dict(content, answer='重新安装电脑即可。'), 0),
            ('context_retention', dict(contexts, answer='Ubuntu电脑，尚未检查令牌。'), 1),
            ('context_retention', dict(contexts, answer='Windows电脑，尚未检查令牌。'), 0),
            ('context_retention', dict(contexts, answer='Ubuntu电脑，已经检查令牌。'), 0)]
    results = []
    for i, (axis, job, expected) in enumerate(jobs):
        judge.system_prompt = COMMON+(PROMPTS['correctness'] if axis == 'correctness' else CONTEXT_PROMPT)
        grade = Transport.grade(judge, job, target/'attempts'/f'calibration_{i}.jsonl')
        results.append({'axis': axis, 'expected': expected, 'job': job, 'grade': grade,
                        'passed': grade.get('review_status') == 'ok' and grade.get('score') == expected})
    enabled = {axis: all(r['passed'] for r in results if r['axis'] == axis)
               for axis in ('correctness', 'context_retention')}
    old.write(receipt, {'judge_identity': judge.identity, 'results': results, 'enabled': enabled})
    print('Judge calibration: '+json.dumps(enabled), flush=True)
    return enabled


def grade(target, protocol):
    healthy(os.getenv('KB_JUDGE_BASE_URL'), os.getenv('KB_JUDGE_MODEL'), os.getenv('KB_JUDGE_API_KEY', ''))
    judge = AxisJudge(old.ROOT)
    enabled = calibrate(judge, target)
    done = {r['key'] for r in all_records(target, 'grades')}
    for row in all_records(target, 'rows'):
        if row['key'] in done:
            continue
        update(target, status='running', stage='semantic_review', current=row['key'])
        scores = {'key': row['key'], 'correctness': None, 'context_retention': None, 'parts': {}}
        if not row['quality']['execution']:
            scores.update(correctness=0, context_retention=0 if row['context_rubric'] else None)
        else:
            content = {'question': row['question'], 'user_history': row['user_history'],
                       'rubric': row['rubric'], 'answer': row['answer'],
                       'reference_documents': [{'filename': n, 'text': t} for _, n, t in protocol['documents']],
                       'actual_ticket': row['ticket'], 'tool_actions': row['tool_actions'],
                       'task_kind': row.get('expected_action') or 'knowledge'}
            for axis, job in [('correctness', payload(content, 'correctness')),
                              ('context_retention', context_job(row) if row['context_rubric'] else None)]:
                if job is None or not enabled[axis]:
                    continue
                judge.system_prompt = COMMON+(PROMPTS['correctness'] if axis == 'correctness' else CONTEXT_PROMPT)
                name = hashlib.sha256((row['key']+axis).encode()).hexdigest()[:24]
                part = Transport.grade(judge, job, target/'attempts'/(name+'.jsonl'))
                scores['parts'][axis] = part
                if part['review_status'] == 'ok':
                    scores[axis] = part['score']
        save_record(target, 'grades', scores)
        print('review '+row['key']+': '+json.dumps({k: scores[k] for k in ('correctness', 'context_retention')}), flush=True)
        report(target)


def report(target):
    protocol = read(target/'protocol.json')
    result = summarize(protocol['cases'], protocol['arms'], all_records(target, 'rows'), all_records(target, 'grades'))
    for name in ('faults', 'load'):
        p = target/name/'summary.json'
        result[name] = read(p) if p.exists() else {'status': 'pending'}
    result['execution_complete'] = all(a['recorded_turns'] == a['expected_turns'] for a in result['arms'].values())
    result['scope'] = protocol['scope']
    old.write(target/'summary.json', result)
    lines = ['# 企业服务Agent：业务、上下文与恢复评测', '',
             '固定自建开发场景；主指标按完整会话统计。内容/上下文由独立权重模型评审，未知保留。',
             '使用已有本地服务；延迟受共享服务负载影响。Token缺失时保持未知，不估算金额。', '',
             '|版本|请求归档|业务会话通过/失败/未知|上下文通过/失败/未知|模型调用|每成功会话调用|',
             '|---|---|---|---|---|---|']
    for arm, s in result['arms'].items():
        def c(key):
            q = s[key]
            return f'{q["passed"]}/{q["failed"]}/{q["unknown"]}'
        lines.append(f'|{arm}|{s["recorded_turns"]}/{s["expected_turns"]}|{c("conversation_task_success")}|'
                     f'{c("context_retention_model_review")}|{s["model_calls_all_attempts"]}|{s["calls_per_successful_conversation"]}|')
    lines += ['', '## 同会话配对', json.dumps(result['paired_conversation_outcomes'], ensure_ascii=False, indent=2),
              '', '## 故障与真实问答队列', json.dumps({k: result[k] for k in ('faults', 'load')}, ensure_ascii=False, indent=2)]
    (target/'report.md').write_text('\n'.join(lines)+'\n')
    return result


def run(target, manage_gpu=None):
    with (target/'queue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def stop(signum, frame):
            raise SystemExit(128+signum)
        for s in (signal.SIGTERM, signal.SIGINT):
            signal.signal(s, stop)
        services = None
        try:
            protocol = freeze(target)
            if manage_gpu is not None:
                from business_assessment_services import Services
                services = Services(target, manage_gpu)
                update(target, status='running', stage='answer_model_startup', gpu=manage_gpu)
                services.start('answer')
            else:
                check()
            from business_assessment_faults import run_faults
            update(target, status='running', stage='faults')
            run_faults(target/'faults')
            generate(target, protocol)
            from business_assessment_load import run_load
            update(target, status='running', stage='real_question_queue')
            run_load(target/'load')
            if services:
                update(target, stage='judge_model_startup')
                services.stop('answer')
                services.start('judge')
            grade(target, protocol)
            result = report(target)
            update(target, status='complete' if result['execution_complete'] and result['semantic_assessment_complete'] else 'complete_pending_review',
                   stage=None, current=None, recorded_turns=sum(a['recorded_turns'] for a in result['arms'].values()),
                   finished_unix=time.time(), report=str(target/'report.md'))
            print('Assessment invocation complete: '+str(target/'report.md'), flush=True)
        except BaseException as exc:
            update(target, status='stopped' if isinstance(exc, (SystemExit, KeyboardInterrupt)) else 'failed',
                   error=str(exc), finished_unix=time.time())
            raise
        finally:
            if services:
                services.close()


def alive(state):
    pid = state.get('pid')
    if not pid or v2.ticks(pid) != state.get('start_ticks'):
        return False
    try:
        command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        return str(Path(__file__).resolve()).encode() in command or b'scripts/business_assessment.py' in command
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['check', 'prepare', 'launch', 'run', 'status', 'report', 'audit', 'stop', 'fault-worker', 'serve'])
    parser.add_argument('--tag', default='dev_v2_20261103')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--fault')
    parser.add_argument('--phase', default='first')
    parser.add_argument('--run-id')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--policy', choices=['business_multi_v4', 'business_multi_v5'], default='business_multi_v4')
    parser.add_argument('--scripted-success', action='store_true')
    parser.add_argument('--manage-gpu', type=int, choices=(0, 1, 6, 7))
    args = parser.parse_args()
    target = directory(args.tag)
    if args.action == 'check':
        check()
    elif args.action == 'fault-worker':
        from business_assessment_faults import fault_worker
        fault_worker(args)
    elif args.action == 'serve':
        from business_assessment_load import serve
        serve(args)
    elif args.action in ('prepare', 'launch', 'run'):
        target.mkdir(parents=True, exist_ok=True)
        if args.action == 'run':
            run(target, args.manage_gpu)
        else:
            with (target/'launch.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = read(target/'status.json') if (target/'status.json').exists() else {}
                if alive(state):
                    raise RuntimeError(f'Assessment already active: {state["pid"]}')
                freeze(target)
                if args.action == 'launch':
                    if args.manage_gpu is None:
                        check()
                    else:
                        from business_assessment_services import Services
                        Services(target, args.manage_gpu)  # Ensure both prior services have stopped.
                    log = target/'queue.log'
                    command = [sys.executable, '-B', str(Path(__file__).resolve()), 'run', '--tag', args.tag]
                    if args.manage_gpu is not None:
                        command += ['--manage-gpu', str(args.manage_gpu)]
                    with log.open('a') as handle:
                        child = subprocess.Popen(command,
                            stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
                    old.write(target/'status.json', {'status': 'starting', 'pid': child.pid,
                        'start_ticks': v2.ticks(child.pid), 'process_group': child.pid, 'log': str(log)})
                    print(json.dumps({'pid': child.pid, 'process_group': child.pid, 'log': str(log),
                        'output': str(target), 'managed_gpu': args.manage_gpu,
                        'simultaneous_new_gpu_models': 1 if args.manage_gpu is not None else 0,
                        'automatic_restart': False}, ensure_ascii=False, indent=2))
                else:
                    print(target/'protocol.json')
    elif args.action == 'report':
        print(json.dumps(report(target), ensure_ascii=False, indent=2))
    elif args.action == 'audit':
        from business_assessment_audit import audit
        audit(target)
    else:
        state = read(target/'status.json') if (target/'status.json').exists() else {}
        if args.action == 'stop' and alive(state):
            os.killpg(state['pid'], signal.SIGTERM)
            print(f'Stopped owned assessment group {state["pid"]}')
        else:
            print(json.dumps({**state, 'alive': alive(state), 'output': str(target)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
