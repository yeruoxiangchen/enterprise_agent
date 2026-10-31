"""Frozen dev-only comparison; shared existing model service, serial arms."""
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
import statistics
import subprocess
import sys
import time

from eval_haystack import settings, health, cases_for, read
from kb.cli import process_identity
from kb.benchmark.dataset import verify_freeze
from kb.benchmark.runner import index, resume_rows, export_grading
from kb.benchmark.scoring import answer_metrics, retrieval_metrics, summary, cluster_interval
from kb.benchmark.sources import digest, read_jsonl, write_json, write_jsonl

PROJECT = Path(__file__).resolve().parents[1]
ARMS = ['fixed-rerank', 'single-agent', 'multi-no-skills', 'multi-skills']
SMOKE = ['first-time-setup__1-1', 'multi-dev-ssh-store-1']


def fingerprint():
    files = sorted((PROJECT/'kb').rglob('*.py')) + sorted((PROJECT/'skills').rglob('*.json')) + sorted((PROJECT/'skills').rglob('*.md'))
    files += [Path(__file__), PROJECT/'scripts/run_multi_agent_20261029.sh', PROJECT/'requirements.haystack.lock',
              PROJECT/'scripts/eval_haystack.py', PROJECT/'scripts/reranker_worker.py']
    return digest(''.join(str(p.relative_to(PROJECT))+digest(p.read_bytes()) for p in files).encode())


def run_arm(root, dataset, ids, arm):
    from kb.backends.haystack_rag import HaystackRAG, QARequest
    from kb.backends.multi_agent import MultiAgentQA
    cfg=settings();health(cfg)
    cases=cases_for(dataset,ids)
    target=root/arm;target.mkdir(parents=True,exist_ok=True)
    with (target/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        config={'arm':arm,'split':'dev','case_ids':ids,'dataset':str(dataset),
            'dataset_receipt':verify_freeze(dataset)['files'],'source_fingerprint':fingerprint(),
            'llm_model':cfg.llm_model,'llm_url':cfg.llm_url,'context_tokens':cfg.context_tokens,
            'output_tokens':cfg.output_tokens,'temperature':0,'enable_thinking':cfg.enable_thinking,
            'timeout':cfg.timeout,'max_model_calls':1 if arm=='fixed-rerank' else 12,
            'max_tools':24,'max_subtasks':3,'max_sources':5 if arm=='fixed-rerank' else 8,
            'candidate_top_k':20,'reranker_receipt':read(Path(cfg.reranker_path)/'receipt.json'),
            'skills':arm=='multi-skills','decompose':arm.startswith('multi-'),
            'haystack':version('haystack-ai'),'hidden_model_retries':0}
        if read(target/'config.json',config)!=config:
            raise RuntimeError('Code/config changed: choose a new output directory')
        write_json(target/'config.json',config)
        rows=resume_rows(target/'predictions.jsonl');done={r['id'] for r in rows}
        if len(rows)!=len(done) or not done<=set(ids):raise RuntimeError('Invalid saved prediction IDs')
        def progress(status,current=None,error=None):
            write_json(target/'status.json',{'status':status,'completed':len(done),'total':len(cases),
                'current':current,'error':error,'updated_at':time.time()})
        try:
            db,scoped,mapping=index(dataset,cfg,target)
            fixed=HaystackRAG(db,scoped,mapping,retrieval='existing') if arm=='fixed-rerank' else None
            for case in cases:
                if case['id'] in done:continue
                progress('running',case['id'])
                checkpoint=target/'checkpoints'/(case['id']+'.json')
                checkpoint.parent.mkdir(exist_ok=True)
                user=db.one('SELECT id,role,username FROM users WHERE username=?',(case['user_role'],))
                bases=['public'];fixture=case.get('fixture')
                if fixture and (fixture['visibility']=='all' or user['role']=='admin'):bases.append(fixture['id'])
                row={k:case[k] for k in ('id','family_id','category','variant','question','history')};row['arm']=arm
                start=time.monotonic();engine=None
                try:
                    request=QARequest(case['question'],tuple(case['history']),user,tuple(bases))
                    if fixed:
                        row.update(fixed.answer(request))
                    else:
                        engine=MultiAgentQA(db,scoped,mapping,skills=arm=='multi-skills',
                            decompose=arm.startswith('multi-'),checkpoint=lambda s:write_json(checkpoint,s))
                        row.update(engine.answer(request,read(checkpoint)))
                except Exception as exc:
                    state=getattr(engine,'state',{})
                    row.update(status='error',execution_status='error',answer=None,citations=[],hits=[],
                        error=type(exc).__name__,model_calls=state.get('calls',1 if fixed else 0),
                        model_calls_unknown=bool(fixed),tool_calls=state.get('tool_calls',0),
                        prompt_tokens=state.get('prompt_tokens',0),completion_tokens=state.get('completion_tokens',0),
                        trace=state.get('trace',[]))
                    print(json.dumps({'id':case['id'],'error':type(exc).__name__}),flush=True)
                    if os.getenv('KB_MULTI_DEBUG')=='1':
                        import traceback
                        traceback.print_exc()
                row['latency_seconds']=time.monotonic()-start
                row['metrics']=answer_metrics(case,row)
                row['retrieval_metrics']=retrieval_metrics(case,row['hits'])
                with (target/'predictions.jsonl').open('a') as f:
                    f.write(json.dumps(row,ensure_ascii=False)+'\n');f.flush()
                done.add(case['id']);rows.append(row)
                print(json.dumps({'arm':arm,'id':case['id'],'status':row['status'],
                    'completed':len(done),'total':len(cases),'calls':row['model_calls']},ensure_ascii=False),flush=True)
            write_json(target/'report.json',summary(rows,len(cases)))
            export_grading(dataset,target,cases,rows)
            progress('complete')
            return rows
        except BaseException as exc:
            progress('interrupted' if isinstance(exc,(SystemExit,KeyboardInterrupt)) else 'failed',error=type(exc).__name__)
            raise


def report(root,dataset,ids):
    cases={c['id']:c for c in cases_for(dataset,ids)}
    configs={arm:read(root/arm/'config.json') for arm in ARMS}
    for arm in ARMS:
        if not configs[arm]:raise RuntimeError('Missing arm config '+arm)
        for field in ('split','case_ids','dataset_receipt','source_fingerprint','llm_model','llm_url',
                      'context_tokens','output_tokens','temperature','enable_thinking','reranker_receipt'):
            if configs[arm][field]!=configs[ARMS[0]][field]:
                raise RuntimeError('Incompatible comparison '+arm+': '+field)
    groups={};scores={};packets=[];key={};rng=random.Random(20261029)
    lines=['# 多 Agent 与动态 Skills 开发诊断','',
        '固定RAG、单Agent、多Agent关闭/开启Skills。相同模型、语料、CPU重排；串行共享服务。',
        '仅开发诊断。Agent总调用上限12、工具上限24；固定RAG一次生成；来源上限分别8/5。',
        '这不是等计算预算消融；单/多Agent同时改变问题拆分、证据组织和调用流程。',
        '模型评分有缺失或争议时保留未知；关键词与动作匹配不是回答准确率。','',
        '|方案|完成|动作匹配|调用|p50秒|平均证据数|语义判对/错/未知|',
        '|---|---:|---:|---:|---:|---:|---|']
    for arm in ARMS:
        rows=read_jsonl(root/arm/'predictions.jsonl')
        if len(rows)!=len(ids) or {r['id'] for r in rows}!=set(ids):raise RuntimeError('Incomplete arm '+arm)
        groups[arm]={r['id']:r for r in rows}
        graded_path=root/'semantic'/arm/'scored_predictions.jsonl'
        graded=read_jsonl(graded_path) if graded_path.exists() else []
        if graded and (len(graded)!=len(ids) or {r['id'] for r in graded}!=set(ids)):
            raise RuntimeError('Incomplete grades '+arm)
        counts=[sum(r['metrics'].get('semantic_correctness')==x for r in graded) for x in (1,0)]
        counts.append(len(ids)-sum(counts))
        scores[arm]={'correct':counts[0],'incorrect':counts[1],'unknown':counts[2],
            'status':'graded_pending_review' if graded else 'pending'}
        lines.append(f"|{arm}|{sum(r['execution_status']=='complete' for r in rows)}/{len(ids)}|"
            f"{sum(r['metrics']['action_correct'] for r in rows)}/{len(ids)}|{sum(r['model_calls'] for r in rows)}|"
            f"{statistics.median(r['latency_seconds'] for r in rows):.2f}|"
            f"{statistics.mean(len(r['hits']) for r in rows):.2f}|{'/'.join(map(str,counts))}|")
    for cid,case in cases.items():
        order=list(ARMS);rng.shuffle(order)
        for n,arm in enumerate(order):
            p=groups[arm][cid];blind=f'{cid}/{n+1}';key[blind]=arm
            packets.append({'blind_id':blind,'question':case['question'],'history':case['history'],
                'answer':p['answer'],'citations':p['citations'],'evidence':p['hits'],
                'reference_answer':case['reference_answer'],'expected_action':case['expected_action'],
                'correctness':None,'faithfulness':None,'reviewer':'','notes':''})
    deltas={}
    for arm in ARMS[1:]:
        paired=[{'family_id':r['family_id'],'metrics':{'action_delta':
            float(r['metrics']['action_correct'])-float(groups[ARMS[0]][cid]['metrics']['action_correct'])}}
            for cid,r in groups[arm].items()]
        deltas[arm]=cluster_interval(paired,'action_delta')
    write_json(root/'comparison.json',{'semantic':scores,'paired_action_deltas':deltas,
        'test_used':False,'human_reviewed':False,'production_released':False})
    write_jsonl(root/'human_review.jsonl',packets);write_json(root/'human_review_key.json',key)
    (root/'report.md').write_text('\n'.join(lines)+'\n')


def main():
    cfg=settings();p=argparse.ArgumentParser()
    p.add_argument('action',choices=['launch','work','status','stop','smoke','arm','report'])
    p.add_argument('--output',type=Path,default=cfg.root/'artifacts/git_support_v1/dev_multi_agent_20261029')
    p.add_argument('--ids',type=Path,default=cfg.root/'artifacts/git_support_v1/v42_preflight_20261029/pilot_ids.json')
    p.add_argument('--arm',choices=ARMS)
    p.add_argument('--no-grade',action='store_true')
    a=p.parse_args();root=a.output.resolve();dataset=cfg.root/'datasets/git_support_zh_v1'
    if not root.is_relative_to(cfg.root.resolve()/'artifacts'):p.error('Output must be under /data project artifacts')
    root.mkdir(parents=True,exist_ok=True);ids=read(a.ids);cases_for(dataset,ids)
    owner=read(root/'queue.json',{});alive=bool(owner) and process_identity(owner['pid'])==owner['start_ticks']
    if a.action=='status':
        s=read(root/'queue_state.json',{})
        status=s.get('status','not_started')
        if status=='running' and not alive:status='interrupted'
        print(f"总状态: {status} | 阶段: {s.get('stage')} | PID: {owner.get('pid')} | 存活: {alive}")
        for arm in ARMS:
            st=read(root/arm/'status.json',{})
            print(f"{arm:22} {st.get('status','pending'):12} {st.get('completed',0)}/{st.get('total',len(ids))} 当前={st.get('current')}")
            grade=read(root/'semantic'/arm/'status.json',{})
            if grade:print('  评分:',grade.get('completed'), '/',grade.get('total'),'缺失:',grade.get('judge_errors',0))
        print('错误:',s.get('error'));print('日志:',root/'queue.log');print('报告:',root/'report.md')
        print('共用现有模型服务；CPU重排；子Agent与评测组串行，无新GPU模型。');return
    if a.action=='stop':
        if alive:
            cmd=Path(f"/proc/{owner['pid']}/cmdline").read_bytes()
            if b'eval_multi_agent.py' not in cmd or b'work' not in cmd:raise RuntimeError('Queue identity mismatch')
            os.killpg(owner['pid'],signal.SIGTERM)
        print('本队列已停止，已完成题目与子任务可恢复。');return
    if a.action=='report':
        if alive:raise RuntimeError('Queue currently owns report')
        report(root,dataset,ids);return
    if a.action=='arm':
        if not a.arm:p.error('--arm required')
        run_arm(root,dataset,ids,a.arm);return
    if a.action=='smoke':
        rows=run_arm(root/'smoke',dataset,SMOKE,'multi-skills')
        passed=all(r['execution_status']=='complete' and r['metrics']['citation_format_valid']
            and not r['metrics'].get('unauthorized_citation') and not r['metrics'].get('forbidden_output') for r in rows)
        write_json(root/'smoke/receipt.json',{'passed':passed,'fingerprint':fingerprint(),
            'meaning':'integration smoke only; not semantic acceptance'})
        if not passed:raise RuntimeError('Integration smoke failed; inspect saved outputs')
        print('Two-case real-service integration smoke passed');return
    if a.action=='launch':
        health(cfg)
        with (root/'launch.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            owner=read(root/'queue.json',{})
            if owner and process_identity(owner['pid'])==owner['start_ticks']:raise RuntimeError('Queue already running')
            cmd=[sys.executable,'-B',__file__,'work','--output',str(root),'--ids',str(a.ids)]
            if a.no_grade:cmd.append('--no-grade')
            with (root/'queue.log').open('a') as log:
                child=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            write_json(root/'queue.json',{'pid':child.pid,'start_ticks':process_identity(child.pid),'started_at':time.time()})
            print(json.dumps({'pid':child.pid,'output':str(root)},ensure_ascii=False));return
    with (root/'suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stage=None;signature=fingerprint()
        protocol={'fingerprint':signature,'dataset':verify_freeze(dataset),'ids':ids,'arms':ARMS,
            'grade':not a.no_grade,'test_used':False,'llm_model':cfg.llm_model}
        if read(root/'protocol.json',protocol)!=protocol:raise RuntimeError('Frozen suite changed')
        write_json(root/'protocol.json',protocol)
        if not (root/'source_snapshot').exists():
            for name in ('kb','scripts','skills','tests'):
                shutil.copytree(PROJECT/name,root/'source_snapshot'/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        def progress(status,error=None):
            write_json(root/'queue_state.json',{'status':status,'stage':stage,'error':error,'updated_at':time.time()})
        def interrupted(sig,frame):raise SystemExit('signal '+str(sig))
        signal.signal(signal.SIGTERM,interrupted)
        def execute(name,cmd):
            nonlocal stage
            if fingerprint()!=signature:raise RuntimeError('Sources changed during evaluation')
            stage=name;progress('running')
            with (root/(name+'.log')).open('a') as log:
                proc=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
            if proc.returncode:raise RuntimeError(name+' failed; inspect stage log')
        try:
            execute('smoke',[sys.executable,'-B',__file__,'smoke','--output',str(root),'--ids',str(a.ids)])
            for arm in ARMS:
                execute(arm,[sys.executable,'-B',__file__,'arm','--arm',arm,'--output',str(root),'--ids',str(a.ids)])
            report(root,dataset,ids)
            if not a.no_grade:
                for arm in ARMS:
                    execute('grade_'+arm,[str(cfg.root/'venv/bin/python'),'-B','scripts/semantic_v4.py','grade',
                        '--rubric','v42','--run-dir',str(root/arm),'--output',str(root/'semantic'/arm),
                        '--review-dir',str(cfg.root/'artifacts/git_support_v1/dev_ablation_v3_20261028/label_review')])
            stage='report';progress('running');report(root,dataset,ids)
            stage=None;progress('complete_pending_review')
        except BaseException as exc:
            progress('interrupted' if isinstance(exc,(SystemExit,KeyboardInterrupt)) else 'failed',str(exc)[:200])
            raise


if __name__=='__main__':main()
