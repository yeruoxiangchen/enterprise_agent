"""Dev-only official Agent Pack vs fixed RAG; serial existing services, no training."""
import argparse
import fcntl
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
from kb.backends.agentpack_rag import AgentPackQA, dependency_receipt, exception_diagnostic
from kb.backends.fixed_first_rag import FixedFirstQA
from audit_agentpack_20261030 import independent_axes
from kb.benchmark.review import jobs_for
from kb.backends.haystack_rag import HaystackRAG, QARequest
from kb.cli import process_identity
from kb.benchmark.dataset import verify_freeze
from kb.benchmark.runner import index, resume_rows, export_grading
from kb.benchmark.scoring import answer_metrics, retrieval_metrics, summary
from kb.benchmark.sources import digest, read_jsonl, write_json, write_jsonl

PROJECT = Path(__file__).resolve().parents[1]
ARMS = ['fixed-rerank', 'official-agentpack']
SMOKE = ['first-time-setup__1-1', 'unknown-1-1', 'clarify-1-2', 'acl-1-1', 'hooks__2-2', 'injection-dev-4']
PROBE = ['first-time-setup__1-1', 'unknown-1-1', 'clarify-1-2']


def integration_receipt(rows):
    # This permits OFFLINE DIAGNOSIS only. Output/action failures must remain
    # measurable outcomes, not be removed by selecting only successful cases.
    checks={r['id']:bool(r['execution_status']=='complete'
        and not r['metrics'].get('unauthorized_citation')
        and not r['metrics'].get('forbidden_output')) for r in rows}
    exercised=any((r.get('tool_calls',0)>0 or r.get('initial_retrieval_complete')) and r.get('hits') for r in rows)
    quality={r['id']:bool(r['metrics']['output_contract_valid'] and r['metrics']['action_correct']) for r in rows}
    return {'passed':bool(rows) and all(checks.values()) and exercised,
        'cases':checks,'real_retrieval_exercised':bool(exercised),'quality_checks':quality,
        'quality_passed':bool(rows) and all(quality.values()),
        'meaning':'offline diagnostic readiness only; output/action failures retained; no deployment approval'}


def fingerprint():
    paths = sorted((PROJECT/'kb').rglob('*.py')) + [Path(__file__),
        PROJECT/'scripts/run_agentpack_20261030.sh', PROJECT/'scripts/eval_haystack.py',
        PROJECT/'scripts/reranker_worker.py', PROJECT/'scripts/semantic_v4.py',
        PROJECT/'scripts/audit_agentpack_20261030.py',
        PROJECT/'requirements.haystack.lock', PROJECT/'requirements.agentpack.txt']
    return digest((''.join(str(p.relative_to(PROJECT))+digest(p.read_bytes()) for p in paths)
                   +json.dumps(dependency_receipt(), sort_keys=True)).encode())


def run_arm(root, dataset, ids, arm):
    cfg=settings();health(cfg)
    cases=cases_for(dataset,ids)
    target=root/arm;target.mkdir(parents=True,exist_ok=True)
    with (target/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        config={'arm':arm,'split':'dev','case_ids':ids,'dataset_receipt':verify_freeze(dataset)['files'],
            'source_fingerprint':fingerprint(),'upstream':dependency_receipt(),
            'llm_model':cfg.llm_model,'llm_url':cfg.llm_url,'context_tokens':cfg.context_tokens,
            'output_tokens':cfg.output_tokens,'enable_thinking':cfg.enable_thinking,'temperature':0,
            'max_model_calls':1 if arm=='fixed-rerank' else 4 if arm=='fixed-first' else 7,
            'agent_steps':0 if arm=='fixed-rerank' else 2 if arm=='fixed-first' else 6,'backup_included':arm!='fixed-rerank',
            'retrieval_top_k':5,'reranker_receipt':read(Path(cfg.reranker_path)/'receipt.json'),
            'candidate_top_k':cfg.reranker_candidates,'sdk_retries':0,'fetch_top_k':0 if arm=='fixed-first' else 5,
            'filter_policy':'immutable_auth_scope_only' if arm in {'fixed-first','fixed-rerank'} else 'immutable_auth_scope_then_optional_metadata_postfilter',
            'output_protocol':'single_text_action_marker_canonical_citations',
            'first_step_policy':'fixed_draft_then_optional_one_search' if arm=='fixed-first' else 'fixed_retrieval_then_generation' if arm=='fixed-rerank' else 'require_metadata_then_content_search_then_auto',
            'extra_search_limit':1 if arm=='fixed-first' else None,
            'required_search_triggers':['insufficient_evidence','multi_part_question'] if arm=='fixed-first' else [],
            'extra_unique_documents':3 if arm=='fixed-first' else None,
            'resume':'completed_cases_skipped; unfinished_case_restarts_with_consumed_call_budget'}
        if read(target/'config.json',config)!=config:
            raise RuntimeError('Code/config changed: choose a new output directory')
        write_json(target/'config.json',config)
        rows=resume_rows(target/'predictions.jsonl');done={r['id'] for r in rows}
        if len(done)!=len(rows) or not done<=set(ids):raise RuntimeError('Invalid saved IDs')
        def progress(status,current=None,error=None):
            write_json(target/'status.json',{'status':status,'completed':len(done),'total':len(cases),
                'current':current,'error':error,'updated_at':time.time()})
        try:
            if len(done)<len(cases):
                progress('indexing')
                db,scoped,mapping=index(dataset,cfg,target)
                fixed=HaystackRAG(db,scoped,mapping,retrieval='existing') if arm=='fixed-rerank' else None
                for case in cases:
                    if case['id'] in done:continue
                    progress('running',case['id'])
                    checkpoint=target/'checkpoints'/(case['id']+'.json');checkpoint.parent.mkdir(exist_ok=True)
                    user=db.one('SELECT id,role,username FROM users WHERE username=?',(case['user_role'],))
                    bases=['public'];fixture=case.get('fixture')
                    if fixture and (fixture['visibility']=='all' or user['role']=='admin'):bases.append(fixture['id'])
                    row={k:case[k] for k in ('id','family_id','category','variant','question','history')};row['arm']=arm
                    start=time.monotonic();engine=None
                    try:
                        request=QARequest(case['question'],tuple(case['history']),user,tuple(bases))
                        if fixed:row.update(fixed.answer(request))
                        else:
                            backend=FixedFirstQA if arm=='fixed-first' else AgentPackQA
                            engine=backend(db,scoped,mapping,checkpoint=lambda s:write_json(checkpoint,s))
                            row.update(engine.answer(request,read(checkpoint)))
                    except Exception as exc:
                        state=getattr(engine,'state',{})
                        row.update(status='error',execution_status='error',answer=None,raw_answer=None,
                            hits=[],citations=[],warnings=[],error=type(exc).__name__,
                            error_chain=exception_diagnostic(exc),model_calls=state.get('calls',0),
                            model_calls_unknown=bool(fixed),tool_calls=state.get('tool_calls',0),
                            backup_calls=state.get('backup_calls',0),prompt_tokens=state.get('prompt_tokens',0),
                            completion_tokens=state.get('completion_tokens',0),trace=state.get('trace',[]))
                    row['latency_seconds']=time.monotonic()-start
                    row['metrics']=answer_metrics(case,row)
                    # The old scorer only understands S labels; retain the adapter warning as a separate metric.
                    row['metrics']['output_contract_valid']=not row.get('warnings') and row['metrics']['citation_format_valid']
                    row['retrieval_metrics']=retrieval_metrics(case,row['hits'])
                    with (target/'predictions.jsonl').open('a') as f:
                        f.write(json.dumps(row,ensure_ascii=False)+'\n');f.flush()
                    rows.append(row);done.add(case['id'])
                    print(json.dumps({'id':case['id'],'arm':arm,'status':row['status'],'completed':len(done),
                                      'total':len(cases),'calls':row['model_calls'],'error':row.get('error')},ensure_ascii=False),flush=True)
            write_json(target/'report.json',summary(rows,len(cases)))
            export_grading(dataset,target,cases,rows);progress('complete')
            return rows
        except BaseException as exc:
            progress('interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed',error=type(exc).__name__)
            raise


def report(root,dataset,ids):
    configs={a:read(root/a/'config.json') for a in ARMS}
    for a in ARMS:
        if not configs[a]:raise RuntimeError('Missing config')
        for key in ('case_ids','dataset_receipt','source_fingerprint','upstream','llm_model','context_tokens',
                    'output_tokens','temperature','enable_thinking','reranker_receipt'):
            if configs[a][key]!=configs[ARMS[0]][key]:raise RuntimeError('Incompatible comparison: '+key)
    result={};groups={};smoke=read(root/'smoke/receipt.json',{});effective={}
    fixed_first='fixed-first' in ARMS
    lines=['# 固定 RAG 与候选流程开发对照','',
        f'相同{len(ids)}道开发诊断题、授权语料、Qwen和CPU重排。固定RAG一次生成。',
        ('fixed-first：相同固定检索和草稿，输出契约失败/资料不足/显式复合问题才启动官方Agent；最多补查1次、总生成4次。'
         if fixed_first else '官方Agent最多6步加1次收尾；强制先元数据再正文检索。'),
        '复用Haystack Agent/Agent Pack及BackupAnswerHook；本地适配权限、调用计量和输出规则。',
        f"联调回答契约与动作全部通过：{smoke.get('quality_passed', 'unknown')}；离线诊断就绪不代表部署门槛通过。",
        '这是流程方案对照，不是等计算量消融。固定优先候选无模型自定义过滤器，只允许正文查询；规则不使用标准答案。' if fixed_first else '原生元数据工具与按条件取文档也可用，不是等预算消融。',
        '内容正确性独立恢复已通过校验的partial content评分；缺失引用评分仍未知。此处自动评分不等于人工真值。',
        '执行完成、动作匹配和语义正确分开统计；评分缺失保留未知。未训练、未用测试集、未切换在线网页。','',
        '|组别|正常完成|动作匹配|输出契约通过|生成调用|收尾调用|p50秒|自动判对/错/未知|',
        '|---|---:|---:|---:|---:|---:|---:|---|']
    for arm in ARMS:
        rows=read_jsonl(root/arm/'predictions.jsonl')
        if len(rows)!=len(ids) or {r['id'] for r in rows}!=set(ids):raise RuntimeError('Incomplete predictions')
        groups[arm]={r['id']:r for r in rows}
        path=root/'semantic'/arm/'scored_predictions.jsonl'
        grades=read_jsonl(path) if path.exists() else []
        if grades and (len(grades)!=len(ids) or {r['id'] for r in grades}!=set(ids)):raise RuntimeError('Incomplete grades')
        jobs={j['id']:j for j in jobs_for(root/arm,Path(dataset).parents[1]/'artifacts/git_support_v1/dev_ablation_v3_20261028/label_review')} if grades else {}
        effective[arm]={g['id']:independent_axes(g,{**jobs[g['id']],'prediction_status':g['status']})['values'] for g in grades}
        correct=sum(v['correctness']==1 for v in effective[arm].values())
        incorrect=sum(v['correctness']==0 for v in effective[arm].values())
        s={'completed':sum(r['execution_status']=='complete' for r in rows),
           'action':sum(r['metrics']['action_correct'] for r in rows),
           'contract':sum(r['metrics']['output_contract_valid'] for r in rows),
           'calls':sum(r['model_calls'] for r in rows),'unknown_call_cases':sum(r.get('model_calls_unknown',False) for r in rows),
           'backup_calls':sum(r.get('backup_calls',0) for r in rows),
           'median_seconds':statistics.median(r['latency_seconds'] for r in rows),
           'correct':correct,'incorrect':incorrect,'unknown':len(ids)-correct-incorrect,
           'followup_triggered':sum(bool(r.get('followup_trigger')) for r in rows),
           'followup_accepted':sum(bool(r.get('followup_accepted')) for r in rows),
           'extra_searches':sum(r.get('extra_searches',0) for r in rows),
           'grounding_unknown':sum(v['faithfulness'] is None for v in effective[arm].values()) if grades else len(ids)}
        result[arm]=s
        lines.append(f"|{arm}|{s['completed']}/{len(ids)}|{s['action']}/{len(ids)}|{s['contract']}/{len(ids)}|{s['calls']}|{s['backup_calls']}|{s['median_seconds']:.2f}|{correct}/{incorrect}/{s['unknown']}|")
    packets=[];key={};rng=random.Random(20261030)
    for case in cases_for(dataset,ids):
        order=list(ARMS);rng.shuffle(order)
        for i,arm in enumerate(order):
            row=groups[arm][case['id']];blind=f"{case['id']}/{i+1}";key[blind]=arm
            packets.append({'blind_id':blind,'question':case['question'],'history':case['history'],
                'answer':row['answer'],'evidence':row['hits'],'citations':row['citations'],
                'reference_answer':case['reference_answer'],'expected_action':case['expected_action'],
                'correctness':None,'faithfulness':None,'reviewer':'','notes':''})
    pairs={'improved':[],'regressed':[],'same':[],'unknown':[]}
    for ident in ids:
        x=effective[ARMS[0]].get(ident,{}).get('correctness');y=effective[ARMS[1]].get(ident,{}).get('correctness')
        key='unknown' if x is None or y is None else 'improved' if y>x else 'regressed' if y<x else 'same'
        pairs[key].append(ident)
    lines+=['',f'配对变化：{ {k:len(v) for k,v in pairs.items()} }。',
            f'候选补查路由统计：触发 {result[ARMS[1]]["followup_triggered"]}，采用 {result[ARMS[1]]["followup_accepted"]}，实际补检索 {result[ARMS[1]]["extra_searches"]}。']
    write_json(root/'comparison.json',{'arms':result,'paired':pairs,'preflight':smoke,'test_used':False,'human_reviewed':False,'production_released':False})
    write_jsonl(root/'human_review.jsonl',packets);write_json(root/'human_review_key.json',key)
    (root/'report.md').write_text('\n'.join(lines)+'\n')


def main():
    global ARMS
    cfg=settings();p=argparse.ArgumentParser()
    p.add_argument('action',choices=['launch','work','status','stop','smoke','probe','arm','report'])
    p.add_argument('--variant',choices=['original','fixed-first'],default='original')
    p.add_argument('--output',type=Path)
    p.add_argument('--ids',type=Path,default=cfg.root/'artifacts/git_support_v1/v42_preflight_20261029/pilot_ids.json')
    p.add_argument('--arm',choices=[*ARMS,'fixed-first']);p.add_argument('--no-grade',action='store_true')
    a=p.parse_args()
    if a.variant=='fixed-first':ARMS=['fixed-rerank','fixed-first']
    a.output=a.output or cfg.root/'artifacts/git_support_v1'/('dev_fixed_first_20261030' if a.variant=='fixed-first' else 'dev_agentpack_diagnostic_20261030')
    root=a.output.resolve();dataset=cfg.root/'datasets/git_support_zh_v1'
    if not root.is_relative_to(cfg.root.resolve()/'artifacts'):p.error('Use project /data artifacts')
    root.mkdir(parents=True,exist_ok=True);ids=read(a.ids);cases_for(dataset,ids)
    owner=read(root/'queue.json',{});alive=bool(owner) and process_identity(owner['pid'])==owner['start_ticks']
    if a.action=='status':
        s=read(root/'queue_state.json',{});status=s.get('status','not_started')
        if status=='running' and not alive:status='interrupted'
        print(f"总状态: {status} | 阶段: {s.get('stage')} | PID: {owner.get('pid')} | 存活: {alive}")
        smoke=read(root/'smoke'/ARMS[1]/'status.json',{})
        print('联调:',smoke.get('status','pending'),str(smoke.get('completed',0))+'/'+str(smoke.get('total',len(SMOKE))))
        for arm in ARMS:
            st=read(root/arm/'status.json',{});grade=read(root/'semantic'/arm/'status.json',{})
            print(f"{arm:22} {st.get('status','pending'):12} {st.get('completed',0)}/{st.get('total',len(ids))} 当前={st.get('current')}")
            if grade:print('  评分:',grade.get('completed'),'/',grade.get('total'),'缺失:',grade.get('judge_errors',0))
        print('错误:',s.get('error'));print('报告:',root/'report.md');print('日志:',root/'queue.log')
        print('复用回答/评分服务，CPU重排；串行推理；无训练、无新GPU模型。');return
    if a.action=='stop':
        if alive:
            cmd=Path(f"/proc/{owner['pid']}/cmdline").read_bytes()
            if b'eval_agentpack.py' not in cmd or b'work' not in cmd:raise RuntimeError('Queue identity mismatch')
            os.killpg(owner['pid'],signal.SIGTERM)
        print('仅停止本评测队列，保留已完成记录。');return
    if a.action=='report':
        if alive:raise RuntimeError('Queue currently owns report')
        report(root,dataset,ids);return
    if a.action=='arm':
        if not a.arm:p.error('--arm required')
        run_arm(root,dataset,ids,a.arm);return
    if a.action in {'probe','smoke'}:
        selected=PROBE if a.action=='probe' else SMOKE
        if a.variant=='fixed-first' and a.action=='probe':selected=['first-time-setup__2-2','multi-dev-ssh-store-1','unknown-1-1']
        if a.variant=='fixed-first' and a.action=='smoke':
            audit_cases=read_jsonl(cfg.root/'artifacts/git_support_v1/agentpack_audit_20261030/cases.jsonl')
            selected=list(dict.fromkeys([c['id'] for c in audit_cases if c['pair']=='regressed' or
                set(c['tags']) & {'answer_without_citation','action_marker_missing'}]+SMOKE))
        rows=run_arm(root/a.action,dataset,selected,ARMS[1])
        receipt=integration_receipt(rows);receipt['fingerprint']=fingerprint()
        write_json(root/a.action/'receipt.json',receipt)
        if not receipt['passed']:raise RuntimeError('Transport/access/retrieval gate failed; inspect per-case records')
        print(f"{len(rows)} cases completed diagnostic preflight; output/action passed={receipt['quality_passed']}");return
    if a.action=='launch':
        health(cfg)
        with (root/'launch.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            owner=read(root/'queue.json',{})
            if owner and process_identity(owner['pid'])==owner['start_ticks']:raise RuntimeError('Queue already running')
            cmd=[sys.executable,'-B',__file__,'work','--output',str(root),'--ids',str(a.ids),'--variant',a.variant]
            if a.no_grade:cmd.append('--no-grade')
            with (root/'queue.log').open('a') as log:
                child=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            write_json(root/'queue.json',{'pid':child.pid,'start_ticks':process_identity(child.pid),'started_at':time.time()})
            print(json.dumps({'pid':child.pid,'output':str(root)},ensure_ascii=False));return
    with (root/'suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stage=None;signature=fingerprint()
        protocol={'fingerprint':signature,'dataset':verify_freeze(dataset),'ids':ids,'arms':ARMS,
                  'grade':not a.no_grade,'test_used':False,'llm_model':cfg.llm_model,'upstream':dependency_receipt()}
        if read(root/'protocol.json',protocol)!=protocol:raise RuntimeError('Frozen suite changed')
        write_json(root/'protocol.json',protocol)
        if not (root/'source_snapshot').exists():
            for name in ('kb','scripts','tests'):
                shutil.copytree(PROJECT/name,root/'source_snapshot'/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            for name in ('requirements.agentpack.txt','requirements.haystack.lock'):
                shutil.copy2(PROJECT/name,root/'source_snapshot'/name)
            import haystack_integrations.agent_pack.advanced_rag as package
            shutil.copytree(Path(package.__file__).parent,root/'source_snapshot/upstream_advanced_rag',ignore=shutil.ignore_patterns('__pycache__'))
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
            execute('smoke',[sys.executable,'-B',__file__,'smoke','--output',str(root),'--variant',a.variant])
            for arm in ARMS:
                execute(arm,[sys.executable,'-B',__file__,'arm','--arm',arm,'--output',str(root),'--ids',str(a.ids),'--variant',a.variant])
            report(root,dataset,ids)
            if not a.no_grade:
                for arm in ARMS:
                    execute('grade_'+arm,[str(cfg.root/'venv/bin/python'),'-B','scripts/semantic_v4.py','grade',
                        '--rubric','v42','--run-dir',str(root/arm),'--output',str(root/'semantic'/arm),
                        '--review-dir',str(cfg.root/'artifacts/git_support_v1/dev_ablation_v3_20261028/label_review')])
            stage='report';progress('running');report(root,dataset,ids)
            stage=None;progress('complete_pending_review')
        except BaseException as exc:
            progress('interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed',str(exc)[:200])
            raise


if __name__=='__main__':main()
