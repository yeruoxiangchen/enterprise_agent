"""Frozen final comparison, role-separated browser demo and résumé handoff."""
import argparse
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import httpx

import business_ops as old
import business_assessment as assessment
from business_assessment_observe import instrument, observation
from business_assessment_services import Services
from business_delivery_contracts import CONTRACTS, summarize
from business_repair_v5 import BASE, POLICY, wait_capacity

ART = old.ROOT/'artifacts/business_delivery_v5_20261103'
REPAIR = old.ROOT/'artifacts/business_repair_v5_20261103/regression_v1'
RESUME = Path('/home/zjr/Tracker/internship_project_lab/shixi/简历新/简历_全栈_v5/制作源')
RESUME_ROOT = Path('/data/zjr/shixi/ai_fullstack_v5_20261103')


def folder(tag):
    if not tag or not tag.replace('_', '').isalnum():
        raise ValueError('Invalid delivery tag')
    return ART/tag


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(target):
    original = assessment.read(BASE/'protocol.json')
    protocol = assessment.freeze(target, cases=original['cases'], arms={'multi_v5': POLICY},
        scope='frozen 12 development conversations / 24 requests; uniform program acceptance contracts')
    for key in ('documents', 'answer_model', 'budget'):
        if protocol[key] != original[key]:
            raise RuntimeError(f'Original comparison setting changed: {key}')
    expected = {f'{c["id"]}/{i}' for c in protocol['cases'] for i in range(len(c['turns']))}
    if set(CONTRACTS) != expected:
        raise ValueError('Every original request needs an explicit contract')
    baseline = assessment.all_records(BASE, 'rows')
    expected_baseline = {f'{a}/{key}' for a in ('single', 'multi_v4') for key in expected}
    if {r['key'] for r in baseline} != expected_baseline or len(baseline) != 48:
        raise ValueError('Baseline is incomplete or has duplicate requests')
    source = {'baseline_protocol': {'path': str(BASE/'protocol.json'), 'sha256': digest(BASE/'protocol.json')},
              'baseline_records': {str(p): digest(p) for p in sorted((BASE/'rows').glob('*.json'))},
              'contracts': CONTRACTS, 'repair_faults': {'path': str(REPAIR/'faults/summary.json'),
                                                      'sha256': digest(REPAIR/'faults/summary.json')},
              'faults_are_reused': True, 'model_judge_used': False}
    receipt = target/'contracts.json'
    if receipt.exists() and assessment.read(receipt) != source:
        raise RuntimeError('Frozen comparison sources changed; choose a new tag')
    old.write(receipt, source)
    return protocol


def report(target):
    protocol = assessment.read(target/'protocol.json')
    manifest = assessment.read(target/'contracts.json')
    for path, sha in manifest['baseline_records'].items():
        if digest(Path(path)) != sha:
            raise RuntimeError('Archived baseline changed')
    fault_path = Path(manifest['repair_faults']['path'])
    if digest(fault_path) != manifest['repair_faults']['sha256']:
        raise RuntimeError('Referenced fault evidence changed')
    records = assessment.all_records(BASE, 'rows')+assessment.all_records(target, 'rows')
    result = summarize(protocol['cases'], ('single', 'multi_v4', 'multi_v5'), records, contracts=manifest['contracts'])
    result['reused_fault_checks'] = {**manifest['repair_faults'], 'summary': assessment.read(fault_path)}
    browser = target/'demo/browser/report.json'
    result['browser_demo'] = assessment.read(browser) if browser.exists() else {'status': 'pending'}
    old.write(target/'comparison.json', result)
    lines = ['# 知页 v5 完整交付对照', '',
             '同一套12个开发场景、24轮请求；单Agent和v4复用原始回答，v5重新执行。',
             '统一检查真实工单、权限、状态、必要字段与规定知识点；场景全部请求通过才记场景通过。',
             '这些数字是程序业务契约验收；完整问答语义质量仍可从逐题记录复核。', '',
             '|方案|场景契约|请求契约|正常返回|模型调用|输入/输出token|',
             '|---|---|---|---|---|---|']
    for arm, value in result['arms'].items():
        c, t, e = (value[k] for k in ('conversation_contracts', 'turn_contracts', 'execution'))
        lines.append(f'|{arm}|{c["passed"]}/{c["expected"]}（待执行{c["unknown"]}）|{t["passed"]}/{t["expected"]}|'
                     f'{e["passed"]}/{e["expected"]}|{value["model_calls"]}|'
                     f'{value["prompt_tokens"]}/{value["completion_tokens"]}|')
    lines += ['', '## 配对场景', json.dumps(result['paired_conversations'], ensure_ascii=False, indent=2),
              '', '## 故障与网页',
              '复用同版本已经完成的8/8项故障检查（其中2项为真实模型请求中的进程强退与恢复）；完整来源与SHA-256见contracts.json。',
              '网页演示：'+json.dumps(result['browser_demo'], ensure_ascii=False),
              '', '## 未通过/待执行的逐项要求']
    for arm, details in result['details'].items():
        for row in details:
            if row['passed'] != 1:
                lines.append('- '+row['key']+' '+json.dumps({k: v for k, v in row['checks'].items() if v is not True}, ensure_ascii=False))
    lines += ['', '## 口径', '固定分母，未执行记未知；拒绝他人工单与正确澄清可以是成功动作。',
              '三组采用相同模型/资料/工具预算，记录实际调用和token。延时来自各次共享GPU环境，不作受控加速结论。',
              '完整原始回答与实际模型输入在baseline_records和rows中，未沿用旧模型评审分数。']
    (target/'report.md').write_text('\n'.join(lines)+'\n')
    review = ['# 逐题验收记录', '', '原始rubric用于人工阅读；程序检查仅覆盖contracts.json列出的要求。', '']
    lookup = {r['key']: r for r in records}
    for arm, details in result['details'].items():
        for checked in details:
            row = lookup.get(checked['key'])
            if row is None:
                continue
            review += ['## '+row['key'], 'run_id: '+row['run_id'], row['question'], '原判据：'+row['rubric'],
                       row['answer'] or '(无回答)', '实际草稿：'+json.dumps(row['actual_tickets'], ensure_ascii=False),
                       '检查：'+json.dumps(checked['checks'], ensure_ascii=False), '']
    (target/'review.md').write_text('\n'.join(review))
    return result


def serve(target, port):
    import uvicorn
    from kb.app import create_app
    cfg, _ = assessment.ops.prepare(target/'demo/app', True)
    with instrument():
        uvicorn.run(create_app(replace(cfg, agent_policy=POLICY)), host='127.0.0.1', port=port, workers=1)


def browser_demo(target):
    # Select a dedicated loopback port without changing an existing app.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    output = target/'demo'
    output.mkdir(exist_ok=True)
    with (output/'web.log').open('a') as log:
        app = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'serve',
                                '--tag', target.name, '--port', str(port)], stdout=log, stderr=subprocess.STDOUT)
    old.write(output/'service.json', {'pid': app.pid, 'start_ticks': assessment.v2.ticks(app.pid),
                                     'url': url, 'policy': POLICY, 'app_root': str(output/'app')})
    try:
        deadline = time.monotonic()+90
        while True:
            if app.poll() is not None:
                raise RuntimeError('Dedicated v5 app exited; see demo/web.log')
            try:
                if httpx.get(url+'/api/health', timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError('Dedicated v5 app startup timed out')
            time.sleep(1)
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        with (output/'browser.log').open('a') as log:
            subprocess.run([str(old.ROOT/'venv/bin/python'), '-B',
                            str(old.PROJECT/'scripts/business_delivery_browser.py'), str(output), url],
                           stdout=log, stderr=subprocess.STDOUT, env=env, check=True, timeout=1000)
        from kb.db import Database
        db = Database(output/'app/knowledge.sqlite3')
        b = assessment.read(output/'browser/report.json')
        old.write(output/'trace_records.json', {
            'runs': [db.one('SELECT * FROM runs WHERE id=?', (rid,)) for rid in b['run_ids']],
            'observations': {rid: observation(db, rid) for rid in b['run_ids']},
            'ticket': db.one('SELECT * FROM service_tickets WHERE id=?', (b['ticket_id'],)),
            'ticket_events': db.all('SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY created_at', (b['ticket_id'],)),
        })
    finally:
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=20)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait(timeout=5)
        state = assessment.read(output/'service.json')
        old.write(output/'service.json', {**state, 'released': True})


def career_materials(target, result):
    data_path = RESUME/'resume_data.json'
    data = assessment.read(data_path)
    a, b = (result['arms'][arm] for arm in ('single', 'multi_v5'))
    if not result['assessment_complete']:
        raise ValueError('Complete all requests before exporting final comparison wording')
    if result['browser_demo']['status'] != 'passed':
        raise ValueError('Complete the actual browser flow before recording its result')
    data['projects'][0]['bullets'][-1] = (
        '**业务对照与验收：**同批12场景、24轮开发请求中，v5与单Agent按统一业务契约验收，'
        f'场景通过{b["conversation_contracts"]["passed"]}/12 对 {a["conversation_contracts"]["passed"]}/12；'
        '8/8项故障检查通过，浏览器完成员工确认、管理员处理及本人状态查询。')
    comparison_path = result.get('comparison_path', str(target/'comparison.json'))
    old_comparisons = {str(target/'comparison.json'), comparison_path}
    data['evidence'] = [p for p in data['evidence'] if p not in old_comparisons]+[comparison_path]
    old.write(data_path, data)
    RESUME_ROOT.mkdir(parents=True, exist_ok=True)
    sources = [comparison_path, str(REPAIR/'faults/summary.json'), str(target/'demo/browser/report.json')]
    claims = []
    for ident, fact, wording, details in [
        ('business-contracts', '固定12场景24轮；v5与保存的单Agent回答采用同一程序检查',
         data['projects'][0]['bullets'][-1], ['为何用同基座单Agent作为主对照', '场景通过要求所有请求通过', '字段从实际工单读取，问题复述不计回答']),
        ('idempotence', '16路重复建草稿得到1份草稿；16路重复确认得到1条状态事件',
         '以source_run唯一约束、事务及版本号校验约束重复副作用', ['写入提交后回执丢失怎样恢复', '状态机合法前后状态', 'SQLite单组织部署边界']),
        ('recovery', '8项故障检查通过，其中2项为真实模型请求中强退进程再恢复',
         '同步检查点与已完成任务结果复用；进程恢复保留预算且避免重复已完成工作', ['同步持久化与异步写入竞态', '恢复时校验权限、策略及调用预算', '超时与非法schema采用脚本故障注入']),
        ('browser', '普通员工咨询与确认、管理员接单解决、员工状态查询全流程通过',
         'FastAPI与网页前端打通知识问答和工单闭环', ['普通员工创建草稿但管理员推进处理', '每一步对应run_id/数据库票据/审计事件', '生成答案和真实业务动作分别验收']),
    ]:
        claims.append({'id': ident, 'source_fact': fact, 'candidate_wording': wording,
                       'sources': [{'type': 'local_report', 'location': p, 'public': False} for p in sources],
                       'responsibility_level': '负责模块', 'verification_status': '已确认',
                       'allowed_uses': ['AI应用与全栈研发简历', '项目面试'],
                       'interview_details': {'decisions': details, 'difficulties': [details[0]],
                                             'verification': sources, 'result': fact},
                       'boundary': '个人项目，与AI协作实现；使用模拟企业资料与开发场景，本地单组织部署。',
                       'risk_notes': ['业务契约通过率仅覆盖冻结开发场景；不写线上SLA或全面语义准确率。'],
                       'last_verified': '2026-11-03'})
    old.write(RESUME_ROOT/'claim_evidence_ledger.json', {'schema_version': 1,
              'profile': {'candidate_id': 'local-candidate', 'target_roles': ['AI应用与全栈研发'],
                          'updated_at': '2026-11-03'}, 'claims': claims})
    text = ['# 知页面试讲述材料', '',
            '项目目标是把员工咨询推进到可确认、可追踪的业务办理。技术难点集中在上下文交接、权限和可恢复副作用。',
            '', '## 90秒讲述',
            '我将员工需求分成技术支持、制度咨询和服务办理，以LangGraph编排领域任务。知识检索先按权限收缩资料范围，再用BM25召回和CPU BGE重排；领域结果用Pydantic结构交接。',
            '实际调试发现，模型可能正常回答却漏掉资产编号，也可能把Windows的历史信息继续当成当前环境。v5把用户事实保留原文位置，显式处理否定和最新更正，并把草稿验收绑定数据库字段。',
            '写动作先创建草稿，员工确认后才能提交。source_run唯一约束防重复建单，事务和版本号约束状态流转；同步检查点及已完成任务缓存支持进程中断恢复。',
            data['projects'][0]['bullets'][-1].replace('**', ''), '',
            '## 关键追问',
            '- 为什么多Agent：按领域隔离知识和工具权限，保存领域结果便于恢复；调用成本单独记录。',
            '- 为什么单Agent是主对照：使用相同模型、资料、工具及最大预算，保留原始逐题回答；v4用于解释修复贡献。',
            '- 23轮是什么：此前20个开发场景中的23次用户请求；本轮固定12场景24轮是另一组评测，分别说明。',
            '- 通过率检查什么：实际状态、权限、草稿字段和规定知识点；程序检查规则与逐题记录可查。',
            '- 8项故障是什么：2次真实模型请求强退与恢复、2项脚本化模型故障、4项真实数据库并发/权限/状态保护。',
            '- 为什么SQLite：单组织单应用进程部署，事务和唯一约束清楚；当前并发验收不能外推大规模生产吞吐。',
            '- AI如何分工：AI协助编写、调试与分析；个人需要能解释需求取舍、追踪链路和每个指标的分母。',
            '', '## 证据入口', *('- '+p for p in sources),
            '- '+str(target/'review.md'), '- '+str(RESUME_ROOT/'claim_evidence_ledger.json')]
    (RESUME_ROOT/'面试讲述与指标口径.md').write_text('\n'.join(text)+'\n')
    with (target/'resume_build.log').open('a') as log:
        subprocess.run(['bash', '/home/zjr/Tracker/ASu-skills/.local/run.sh', 'env', '-u', 'PYTHONPATH',
                        '/home/zjr/anaconda3/bin/python', '-B', str(RESUME/'build.py')], stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=240)


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
            report(target)
            selected = wait_capacity(target, gpu, wait_hours)
            if assessment.source_hashes() != protocol['source_hashes']:
                raise RuntimeError('Source changed while queued; use a new delivery tag')
            services = Services(target, selected)
            assessment.update(target, status='running', stage='answer_model_startup', gpu=selected)
            services.start('answer')
            assessment.generate(target, protocol, on_record=report)
            assessment.update(target, stage='browser_demo', current=None, recorded_turns=24)
            browser_demo(target)
            result = report(target)
            services.close()
            assessment.update(target, stage='resume_export')
            career_materials(target, result)
            assessment.update(target, status='complete', stage=None, current=None, finished_unix=time.time(),
                              report=str(target/'report.md'), resume=str(RESUME.parent))
            print('V5 final delivery complete: '+str(target/'report.md'), flush=True)
        except BaseException as exc:
            assessment.update(target, status='stopped' if isinstance(exc, (SystemExit, KeyboardInterrupt)) else 'failed',
                              error=repr(exc), finished_unix=time.time())
            raise
        finally:
            if services:
                services.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('launch', 'run', 'status', 'stop', 'report', 'serve', 'export'))
    parser.add_argument('--tag', default='final_v1')
    parser.add_argument('--gpu', choices=('auto', '0', '1', '6', '7'), default='auto')
    parser.add_argument('--wait-hours', type=float, default=6)
    parser.add_argument('--port', type=int, default=8775)
    args = parser.parse_args()
    if args.wait_hours < 0:
        raise ValueError('Wait hours must be nonnegative')
    target = folder(args.tag)
    target.mkdir(parents=True, exist_ok=True)
    if args.action == 'run':
        run(target, args.gpu, args.wait_hours)
    elif args.action == 'serve':
        serve(target, args.port)
    elif args.action == 'report':
        print(json.dumps(report(target), ensure_ascii=False, indent=2))
    elif args.action == 'export':
        career_materials(target, report(target))
    elif args.action == 'launch':
        with (target/'launch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = assessment.read(target/'status.json') if (target/'status.json').exists() else {}
            if state.get('pid') and assessment.v2.ticks(state['pid']) == state.get('start_ticks'):
                raise RuntimeError('Delivery queue already running')
            if state.get('status') == 'complete':
                raise RuntimeError('Delivery already complete; inspect the report')
            freeze(target)
            Services(target, args.gpu)
            with (target/'queue.log').open('a') as log:
                child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'run', '--tag', args.tag,
                    '--gpu', args.gpu, '--wait-hours', str(args.wait_hours)],
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            old.write(target/'status.json', {'pid': child.pid, 'start_ticks': assessment.v2.ticks(child.pid),
                'status': 'starting', 'process_group': child.pid, 'gpu': args.gpu, 'log': str(target/'queue.log')})
            print(json.dumps({'pid': child.pid, 'gpu': args.gpu, 'memory_fraction': .78,
                              'log': str(target/'queue.log'), 'output': str(target), 'automatic_restart': False},
                             ensure_ascii=False, indent=2))
    else:
        state = assessment.read(target/'status.json') if (target/'status.json').exists() else {}
        pid = state.get('pid')
        live = bool(pid) and assessment.v2.ticks(pid) == state.get('start_ticks')
        if live:
            live = str(Path(__file__).resolve()).encode() in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if args.action == 'stop' and live:
            os.killpg(pid, signal.SIGTERM)
            print('Stop requested for owned delivery group '+str(pid))
        else:
            print(json.dumps({**state, 'alive': live, 'output': str(target)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
