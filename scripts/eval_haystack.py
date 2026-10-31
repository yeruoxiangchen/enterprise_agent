"""Isolated Haystack pilot; frozen dev questions, saved controls, no live Web changes."""
import argparse
from dataclasses import replace
import fcntl
from importlib.metadata import version
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time

import httpx
from kb.cli import process_identity
from kb.config import Settings
from kb.benchmark.dataset import verify_freeze
from kb.benchmark.runner import index, resume_rows, export_grading
from kb.benchmark.scoring import answer_metrics, retrieval_metrics, summary, cluster_interval
from kb.benchmark.sources import digest, read_jsonl, write_json, write_jsonl

PROJECT = Path(__file__).resolve().parents[1]
ARMS = ['haystack-existing-rerank', 'haystack-bm25-rerank']
BASELINE_ARMS = ['fixed42-rerank', 'agent-guarded41-rerank', 'agent-guarded42-rerank']
SMOKE_IDS = ['multi-dev-ssh-store-1', 'clarify-1-2', 'acl-1-1', 'injection-dev-1']


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def fingerprint():
    files = [*sorted((PROJECT/'kb').rglob('*.py')), Path(__file__),
             PROJECT/'scripts/reranker_worker.py', PROJECT/'scripts/run_haystack_20261029.sh']
    return digest(''.join(str(p.relative_to(PROJECT))+digest(p.read_bytes()) for p in files).encode())


def cases_for(dataset, ids):
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('Expected nonempty unique dev ids')
    cases = read_jsonl(dataset/'dev.jsonl')
    result = [c for c in cases if c['id'] in ids]
    if len(result) != len(ids):
        raise ValueError('Unknown dev ids; no test cases permitted')
    return result


def settings():
    cfg = Settings()
    return replace(cfg, reranker_path=str(cfg.root/'models/bge-reranker-base'),
                   embedding_url='', embedding_model='', embedding_key='', timeout=120)


def health(cfg):
    if not cfg.llm_url or not cfg.llm_model or not cfg.tokenize_url:
        raise RuntimeError('Model endpoint, model and exact tokenizer must be configured')
    headers = {'Authorization': 'Bearer '+cfg.llm_key} if cfg.llm_key else {}
    with httpx.Client(timeout=10, trust_env=False) as client:
        r = client.get(cfg.llm_url.rstrip('/')+'/models', headers=headers)
        r.raise_for_status()
        if cfg.llm_model not in {m['id'] for m in r.json()['data']}:
            raise RuntimeError('Configured answer model not served')
    if not (Path(cfg.reranker_path)/'receipt.json').exists():
        raise RuntimeError('CPU reranker is not ready')


def run_arm(root, dataset, ids, arm):
    from kb.backends.haystack_rag import HaystackRAG, QARequest
    cfg = settings()
    health(cfg)
    receipt = verify_freeze(dataset)
    cases = cases_for(dataset, ids)
    target = root/arm
    target.mkdir(parents=True, exist_ok=True)
    lock = (target/'run.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = {'arm': arm, 'split': 'dev', 'case_ids': [c['id'] for c in cases],
        'dataset': str(dataset), 'dataset_receipt': receipt['files'], 'source_fingerprint': fingerprint(),
        'llm_model': cfg.llm_model, 'llm_url': cfg.llm_url, 'context_tokens': cfg.context_tokens,
        'output_tokens': cfg.output_tokens, 'temperature': 0, 'enable_thinking': cfg.enable_thinking,
        'timeout_seconds': cfg.timeout, 'max_generation_calls': 1, 'hidden_retries': 0,
        'haystack_version': version('haystack-ai'), 'openai_version': version('openai'),
        'reranker_receipt': read(Path(cfg.reranker_path)/'receipt.json'),
        'retrieval_top_k': 5, 'candidates': 20, 'chunk_size': 900, 'chunk_overlap': 120,
        'protocol': 'plaintext_action_marker_inline_citations_no_finish_tool',
        'citation_validation': 'format_and_access_not_semantic_entailment',
        'latency_scope': 'shared_server_observed_latency_not_dedicated_throughput'}
    previous = read(target/'config.json')
    if previous and previous != config:
        raise ValueError('Config/code/dataset changed; choose new output directory')
    write_json(target/'config.json', config)
    rows = resume_rows(target/'predictions.jsonl')
    done = {r['id'] for r in rows}
    if len(done) != len(rows) or not done <= set(ids):
        raise ValueError('Duplicate/unexpected predictions')
    def progress(status, current=None, error=None):
        write_json(target/'status.json', {'status': status, 'completed': len(done), 'total': len(cases),
            'current': current, 'error': error, 'updated_at': time.time()})
    try:
        if len(done) < len(cases):
            progress('indexing')
            db, scoped, mapping = index(dataset, cfg, target)
            engine = HaystackRAG(db, scoped, mapping, retrieval='existing' if arm==ARMS[0] else 'haystack')
            for case in cases:
                if case['id'] in done:
                    continue
                progress('running', case['id'])
                started = time.monotonic()
                user = db.one('SELECT id,role,username FROM users WHERE username=?', (case['user_role'],))
                bases = ['public']
                fixture = case.get('fixture')
                if fixture and (fixture['visibility']=='all' or user['role']=='admin'):
                    bases.append(fixture['id'])
                row = {k: case[k] for k in ('id','family_id','category','variant','question','history')}
                row['arm'] = arm
                try:
                    # Explicit boundary: no expected_action/reference/evidence/fixture text passed.
                    row.update(engine.answer(QARequest(case['question'], tuple(case['history']), user, tuple(bases))))
                except Exception as exc:
                    # Exceptions from an SDK can contain headers/body. Only record the class here.
                    row.update(status='error', execution_status='error', answer=None, raw_answer=None,
                        citations=[], hits=[], error=type(exc).__name__, model_calls=0, model_calls_unknown=True,
                        cost_accounting='unknown_on_error', trace=[])
                    print(json.dumps({'id':case['id'],'exception_type':type(exc).__name__}), flush=True)
                    # Tracebacks have no local variables/request headers; useful during smoke.
                    if os.getenv('KB_HAYSTACK_DEBUG') == '1':
                        import traceback
                        traceback.print_exc()
                row['latency_seconds'] = time.monotonic()-started
                row['metrics'] = answer_metrics(case, row)
                row['retrieval_metrics'] = retrieval_metrics(case, row['hits'])
                row['metrics']['execution_failed'] = row['execution_status'] != 'complete'
                row['metrics']['action_unclassified'] = row['status'] == 'unclassified'
                with (target/'predictions.jsonl').open('a') as f:
                    f.write(json.dumps(row, ensure_ascii=False)+'\n')
                    f.flush()
                rows.append(row)
                done.add(case['id'])
                print(json.dumps({'arm':arm,'id':case['id'],'status':row['status'],
                                  'completed':len(done),'total':len(cases)},ensure_ascii=False),flush=True)
        # summary expects numeric counts; unknown calls remain explicitly tracked separately.
        totals = summary([{**r, 'model_calls': r.get('model_calls') or 0} for r in rows], len(cases))
        totals['calls_unknown_cases'] = sum(r.get('model_calls_unknown',False) for r in rows)
        write_json(target/'report.json', totals)
        export_grading(dataset, target, cases, rows)
        progress('complete')
        return rows
    except BaseException as exc:
        progress('interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed', error=type(exc).__name__)
        raise
    finally:
        lock.close()


def comparison(root, baseline, ids):
    groups = {}
    for arm in BASELINE_ARMS:
        groups[arm] = read_jsonl(baseline/'pilot'/arm/'predictions.jsonl')
    for arm in ARMS:
        groups[arm] = read_jsonl(root/arm/'predictions.jsonl')
    configs = {a: read((baseline/'pilot'/a if a in BASELINE_ARMS else root/a)/'config.json') for a in groups}
    reference = configs[ARMS[0]]
    for arm, rows in groups.items():
        if len(rows) != len(ids) or {r['id'] for r in rows} != set(ids):
            raise ValueError('Incomplete/unpaired arm: '+arm)
        for key in ('dataset_receipt','split','llm_model','context_tokens','output_tokens','temperature','enable_thinking'):
            if configs[arm][key] != reference[key]:
                raise ValueError('Baseline mismatch: '+key)
    lines = ['# Haystack 34 题开发诊断对照','',
        '同一批开发集诊断题：包含旧错误、分歧与关键控制，不能代表业务总体准确率。',
        '旧三组直接引用 v4.2 保存结果；新两组复用相同 Qwen、BGE 重排、原始文档与切块。',
        'Haystack-existing 仅替换回答链；Haystack-bm25 同时替换 BM25 引擎。未用测试集、未训练、未切换网页。',
        'AnswerBuilder 只解析引用编号。来源存在不证明答案正确；自然语言输出减少工具协议负担，不保证语义提升。','',
        '| 方案 | 推理成功 | 动作未分类 | 误拒答 | 引用格式通过 | 禁止内容/越权 | 调用已知数 | p50秒 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    result = {}
    semantic = {}
    packets = []
    case_map = {c['id']:c for c in cases_for(Settings().root/'datasets/git_support_zh_v1',ids)}
    rng = random.Random(20261029)
    key = {}
    for case_id in ids:
        candidates = [(a, next(r for r in rows if r['id']==case_id)) for a,rows in groups.items()]
        rng.shuffle(candidates)
        for slot, (arm, row) in enumerate(candidates, 1):
            blind_id = f'{case_id}/candidate-{slot}'
            key[blind_id] = arm
            packets.append({'blind_id':blind_id,'question':row['question'],'history':row['history'],
                'answer':row.get('answer'),'citations':row.get('citations',[]),'retrieved':row.get('hits',[]),
                'reference_answer':case_map[case_id]['reference_answer'],
                'gold_evidence':case_map[case_id]['evidence'], 'expected_action':case_map[case_id]['expected_action'],
                'content_correct':None,'grounded':None,'action_correct':None,'reviewer':'','notes':''})
    for arm, rows in groups.items():
        count = len(rows)
        s = summary([{**r,'model_calls':r.get('model_calls') or 0} for r in rows],count)
        vals = {'execution_success':sum(r.get('execution_status','error' if r['status'] in {'error','cancelled'} else 'complete')=='complete' for r in rows),
            'unclassified':sum(r['status']=='unclassified' for r in rows),
            'false_refusal':sum(r['metrics'].get('false_refusal') is True for r in rows),
            'citation_format':sum(r['metrics'].get('citation_format_valid') is True for r in rows),
            'unsafe':sum(bool(r['metrics'].get('forbidden_output') or r['metrics'].get('unauthorized_citation')) for r in rows),
            'calls_known':s['model_calls'],'calls_unknown_cases':sum(r.get('model_calls_unknown',False) for r in rows),
            'p50':s['latency_seconds']['p50']}
        result[arm] = {**vals,'diagnostic_summary':s}
        lines.append(f"| {arm} | {vals['execution_success']}/{count} | {vals['unclassified']} | {vals['false_refusal']} | {vals['citation_format']}/{count} | {vals['unsafe']} | {vals['calls_known']} | {vals['p50']:.2f} |")
        grading = (root/'semantic'/arm if arm in ARMS else baseline/'semantic'/('pilot_'+arm))/'scored_predictions.jsonl'
        if grading.exists():
            graded = read_jsonl(grading)
            if len(graded)!=count or {r['id'] for r in graded}!=set(ids):
                raise ValueError('Incomplete grading artifact')
            semantic[arm] = {'correct':sum(r['metrics'].get('semantic_correctness')==1 for r in graded),
                'incorrect':sum(r['metrics'].get('semantic_correctness')==0 for r in graded),
                'unknown':sum(r['metrics'].get('semantic_correctness') is None for r in graded),
                'judge_failed':sum(r['metrics'].get('judge_failed') is True for r in graded), 'n':count}
        else:
            semantic[arm] = {'status':'pending','n':count}
    lines += ['', '语义评分采用原 v4.2 评分器，保留原始错误与缺失；以下分数只作待人工核验的诊断：', '',
        '| 方案 | 正确 | 错误 | 未知 |', '|---|---:|---:|---:|']
    for arm, scores in semantic.items():
        lines.append(f"| {arm} | {scores.get('correct','待评分')} | {scores.get('incorrect','-')} | {scores.get('unknown','-')} |")
    # Stable denominators and family-level paired uncertainty for selected diagnostics.
    deltas = {}
    for arm in ARMS:
        new = {r['id']:r for r in groups[arm]}
        deltas[arm] = {}
        for metric in ('action_correct','citation_format_valid','all_required_facts_diagnostic'):
            paired = []
            for old in groups[BASELINE_ARMS[0]]:
                a,b = old['metrics'].get(metric),new[old['id']]['metrics'].get(metric)
                if (a is None)!=(b is None):
                    raise ValueError('Metric denominator mismatch')
                if a is not None:
                    paired.append({'family_id':old['family_id'],'metrics':{metric:float(b)-float(a)}})
            deltas[arm][metric] = cluster_interval(paired,metric)
    lines += ['', '关键词命中与格式合法不是回答准确率；逐题人工表见 human_review.jsonl，随机编号映射单独保存。',
        '自动评分可能不稳定，本轮不会自动放行 100/300 题或切换在线后端。',
        '时间为共享服务观测值，旧组与新组采集时间不同，不能解释为严格吞吐基准。']
    write_json(root/'comparison.json', {'arms':result,'semantic_diagnostic':semantic,'paired_diagnostic_deltas':deltas,
        'baseline':str(baseline),'independent_human_review':False,'production_released':False})
    write_jsonl(root/'human_review.jsonl',packets)
    write_json(root/'human_review_key.json',key)
    (root/'report.md').write_text('\n'.join(lines)+'\n')


def main():
    cfg = settings()
    p = argparse.ArgumentParser()
    p.add_argument('action',choices=['smoke','launch','work','status','stop','report','arm','qanything-health'])
    p.add_argument('--output',type=Path,default=cfg.root/'artifacts/git_support_v1/dev_haystack_20261029')
    p.add_argument('--baseline',type=Path,default=cfg.root/'artifacts/git_support_v1/dev_v42_20261029')
    p.add_argument('--ids',type=Path,default=cfg.root/'artifacts/git_support_v1/v42_preflight_20261029/pilot_ids.json')
    p.add_argument('--arm',choices=ARMS)
    a = p.parse_args()
    root = a.output.resolve()
    if not root.is_relative_to(cfg.root.resolve()/'artifacts'):
        p.error('Outputs must stay under project /data artifacts')
    root.mkdir(parents=True,exist_ok=True)
    dataset = cfg.root/'datasets/git_support_zh_v1'
    ids = read(a.ids)
    cases_for(dataset,ids)
    owner = read(root/'queue.json',{})
    alive = bool(owner) and process_identity(owner['pid'])==owner['start_ticks']
    if a.action=='status':
        s=read(root/'queue_state.json',{})
        state=s.get('status','not_started')
        if state=='running' and not alive:state='interrupted'
        print(f"总状态: {state} | 阶段: {s.get('stage')} | PID: {owner.get('pid')} | 存活: {alive}")
        for arm in ARMS:
            for name,target in [(arm,root/arm),('grade_'+arm,root/'semantic'/arm)]:
                status=read(target/'status.json',{})
                print(f"{name:35} {status.get('status','pending'):28} {status.get('completed',0)}/{status.get('total',len(ids))} 当前={status.get('current')} 评分缺失={status.get('judge_errors',0)}")
        print('错误:',s.get('error'));print('报告:',root/'report.md');print('日志:',root/'queue.log')
        print('仅34题开发诊断；复用回答/评分服务，CPU重排；不训练、不切换网页。');return
    if a.action=='qanything-health':
        from kb.backends.qanything import QAnythingClient
        client=QAnythingClient(os.getenv('KB_QANYTHING_URL','http://127.0.0.1:18777'),'kb_haystack_comparison',cfg)
        try:client.health();print('QAnything HTTP service ready')
        finally:client.close()
        return
    if a.action=='stop':
        if alive:
            cmd=Path(f"/proc/{owner['pid']}/cmdline").read_bytes()
            if b'eval_haystack.py' not in cmd or b'work' not in cmd:raise RuntimeError('Queue identity mismatch')
            os.killpg(owner['pid'],signal.SIGTERM)
        print('已停止本实验队列，模型服务保留。');return
    if a.action=='arm':
        if not a.arm:p.error('--arm is required')
        run_arm(root,dataset,ids,a.arm);return
    if a.action=='smoke':
        rows=[]
        for arm in ARMS:rows.extend(run_arm(root/'smoke',dataset,SMOKE_IDS,arm))
        passed=all(r.get('execution_status')=='complete' and r['status']!='unclassified' and
                   not r['metrics'].get('forbidden_output') and not r['metrics'].get('unauthorized_citation') for r in rows)
        write_json(root/'smoke/receipt.json',{'passed':passed,'source_fingerprint':fingerprint(),'cases':SMOKE_IDS,
            'records':len(rows),'meaning':'integration/access/output smoke, not semantic quality approval'})
        if not passed:raise RuntimeError('Smoke failed; inspect smoke predictions')
        print('8/8 integration smoke passed; semantic quality still requires pilot');return
    if a.action=='launch':
        smoke=read(root/'smoke/receipt.json',{})
        if not smoke.get('passed') or smoke.get('source_fingerprint')!=fingerprint():
            raise RuntimeError('Run smoke with current code first')
        health(cfg)
        with (root/'launch.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            owner=read(root/'queue.json',{})
            if owner and process_identity(owner['pid'])==owner['start_ticks']:raise RuntimeError('Already running')
            with (root/'queue.log').open('a') as log:
                child=subprocess.Popen([sys.executable,'-B',__file__,'work','--output',str(root),
                    '--baseline',str(a.baseline),'--ids',str(a.ids)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            write_json(root/'queue.json',{'pid':child.pid,'start_ticks':process_identity(child.pid),'started_at':time.time()})
            print(json.dumps({'pid':child.pid,'output':str(root)},ensure_ascii=False));return
    if a.action=='report':
        if alive:raise RuntimeError('Queue owns the report while running')
        comparison(root,a.baseline,ids);return
    with (root/'suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stage=None
        def progress(state,error=None):write_json(root/'queue_state.json',{'status':state,'stage':stage,'error':error,'updated_at':time.time()})
        def interrupted(sig,frame):raise SystemExit('signal '+str(sig))
        signal.signal(signal.SIGTERM,interrupted)
        signature=fingerprint()
        protocol={'source_fingerprint':signature,'ids':ids,'dataset':verify_freeze(dataset),
            'baseline':str(a.baseline.resolve()),'models':{'answer':cfg.llm_model},'test_used':False,
            'decision':'diagnostic_only_no_auto_release', 'haystack_version':version('haystack-ai')}
        previous=read(root/'protocol.json')
        if previous and previous!=protocol:raise RuntimeError('Frozen suite changed')
        write_json(root/'protocol.json',protocol)
        if not (root/'source_snapshot').exists():
            for name in ('kb','scripts','tests'):
                shutil.copytree(PROJECT/name,root/'source_snapshot'/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            shutil.copyfile(PROJECT/'requirements.haystack.in',root/'source_snapshot/requirements.haystack.in')
            shutil.copyfile(cfg.root/'artifacts/haystack-installed.txt',root/'source_snapshot/installed.txt')
        try:
            for arm in ARMS:
                if fingerprint()!=signature:raise RuntimeError('Sources changed during suite')
                stage=arm;progress('running')
                with (root/(arm+'.log')).open('a') as log:
                    cmd=[sys.executable,'-B',__file__,'arm','--arm',arm,'--output',str(root),'--ids',str(a.ids)]
                    completed=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
                if completed.returncode:raise RuntimeError(arm+' failed; inspect its log')
            stage='deterministic_report';progress('running');comparison(root,a.baseline,ids)
            for arm in ARMS:
                if fingerprint()!=signature:raise RuntimeError('Sources changed during suite')
                stage='grade_'+arm;progress('running')
                # Frozen existing rubric; no opportunistic judge repair in this experiment.
                with (root/(stage+'.log')).open('a') as log:
                    cmd=[str(cfg.root/'venv/bin/python'),'-B','scripts/semantic_v4.py','grade','--rubric','v42',
                         '--run-dir',str(root/arm),'--output',str(root/'semantic'/arm),
                         '--review-dir',str(cfg.root/'artifacts/git_support_v1/dev_ablation_v3_20261028/label_review')]
                    completed=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
                if completed.returncode:raise RuntimeError(stage+' failed; deterministic report retained')
            stage='report';progress('running');comparison(root,a.baseline,ids)
            stage=None;progress('complete_pending_human_review')
        except BaseException as exc:
            progress('interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed',str(exc)[:250])
            raise


if __name__=='__main__':
    main()
