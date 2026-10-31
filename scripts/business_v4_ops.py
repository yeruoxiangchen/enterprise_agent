"""Versioned v4 repair acceptance and same-data single/v3/v4 comparisons.

Reuse existing data preparation, DB-backed execution and truthful action checks.
Old snapshots/results are immutable inputs; all new outputs have a v4 directory.
"""
import argparse
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import business_v3_ops as v3
import business_v2_ops as v2
import business_ops as old
from business_cases_v2 import CASES,DOCUMENTS
from business_cases_v3 import CASES as NEW_CASES
from kb.benchmark.business_quality import summarize

ART=old.ROOT/'artifacts/business_multi_v4_20261030'
POLICIES={'single':'business_single_v2','multi_v3':'business_multi_v3','multi_v4':'business_multi_v4'}
v3.POLICIES.update(POLICIES)
REFS=[
 'https://docs.langchain.com/oss/python/langchain/multi-agent/router',
 'https://docs.langchain.com/oss/python/langchain/multi-agent/subagents',
 'https://docs.langchain.com/oss/python/langchain/structured-output',
]


def source_files():
    return sorted(set(v3.files()+list((old.PROJECT/'scripts').glob('business_v4*.py'))))


def fingerprint():
    return hashlib.sha256(b''.join(str(p.relative_to(old.PROJECT)).encode()+p.read_bytes() for p in source_files())).hexdigest()


def regression_cases():
    return [{'id':i,'turns':[{'question':q,'domains':ds,'probes':[probe] if probe else [],
        'statuses':list(v2.SUCCESS),'rubric':'按授权资料回答用户问题；未提供的事实如实说明。',
        'action':'draft' if i=='draft' else None}]} for i,q,ds,probe in old.CASES]


def stages(smoke=False):
    if smoke:
        return [('repair_regression','multi_v4',[c for c in regression_cases() if c['id'] in ('travel','missing','meal')],False),
            ('repair_business','multi_v4',[c for c in CASES if c['id'] in ('proxy-access','auth-loan','actual-proxy','known-and-missing','ticket-draft','followup-https')],True),
            ('repair_versions','multi_v4',[c for c in NEW_CASES if c['id']=='new-switch'],True)]
    return [('regression','multi_v4',regression_cases(),False)]+[(a,a,CASES,True) for a in POLICIES]+[('new_acceptance','multi_v4',NEW_CASES,True)]


def report(target):
    rows=v3.rows(target);scores=summarize(rows,v3.rows(target,'grades.jsonl')) if rows else {}
    old.write(target/'summary.json',scores)
    text=['# 多Agent v4：官方结构化结果组件修复与同批对照','',
        '实际复用 LangChain create_agent/ToolStrategy 的解析、ToolMessage校验反馈和structured_response；LangGraph保留图状态/检查点。',
        '正文与来源字段分开，主机按已校验来源渲染引用；纯证据不足答复不伪造引用。来源合法不等于事实被支持。',
        '自建开发数据，同模型同知识资料；调用次数可不同。没有使用正式测试集，不覆盖原v3结果。',
        '', '| 阶段 | 正常返回 | 真实服务动作 | 草稿原文 | 权限检查 | 模型调用 | p50秒 |',
        '|---|---|---|---|---|---|---|']
    for name,s in scores.items():
        m=s['metrics']
        def count(k):return f"{m[k]['passed']}/{m[k]['applicable']}"
        text.append(f"|{name}|{count('execution')}|{count('service_complete')}|{count('draft_user_quote_integrity')}|{count('permission_checks')}|{s['model_calls']}|{s['p50_seconds']:.2f}|")
    text+=['','## 执行失败与业务失败']
    for r in rows:
        failed=[k for k in ('execution','service_complete','draft_user_quote_integrity','permission_checks','no_auto_submission') if r['quality'][k] is False]
        if failed:text.append(f"- {r['key']}: {failed}; error={r['error']}")
    text+=['','## 新版语义评审','普通咨询按回答完成度评价，不要求创建工单；明确办理请求才校验真实回执。新评分与v3旧评分分开。']
    for name,s in scores.items():text.append('- '+name+': '+json.dumps(s['semantic_model_review'],ensure_ascii=False))
    for name,path in [('工程',target/'engineering/report.json'),('评分校准/状态',target/'judge_status.json')]:
        if path.exists():text.append(name+': '+path.read_text())
    text+=['','## 官方参考与复用','\n'.join('- '+url for url in REFS),
        '库源代码读取位置：business-packages-v2/langchain/agents/structured_output.py 与 factory.py。具体版本见 requirements.business.lock。',
        '没有引入额外自由规划Supervisor，不增加未验证的协作步骤；保留领域路由、工单原文、权限和真实回执。']
    (target/'report.md').write_text('\n'.join(text)+'\n')
    review=['# 逐题复核：尚未独立人工评分','']
    for r in rows:review += [f"## {r['key']}",r['question'],'标准：'+r['rubric'],r['answer'] or '(执行失败)',
        '工单：'+json.dumps(r['ticket'],ensure_ascii=False),'引用：'+json.dumps(r['citations'],ensure_ascii=False),'']
    (target/'review.md').write_text('\n'.join(review))


def grade_all(target,data):
    from business_v4_judge import BusinessJudge,calibration,FIELDS
    if not os.environ.get('KB_JUDGE_BASE_URL'):
        old.write(target/'judge_status.json',{'status':'not_configured'});return
    v3.wait_service(target,'judge','semantic');judge=BusinessJudge(old.ROOT)
    old.write(target/'judge_identity.json',judge.identity)
    cal=target/'calibration.json'
    passed=all(r['passed'] for r in json.loads(cal.read_text())) if cal.exists() else calibration(judge,target)
    if not passed:
        old.write(target/'judge_status.json',{'status':'calibration_failed','reason':'停止扩量评审，保留逐题复核材料'});return
    done={r['key'] for r in v3.rows(target,'grades.jsonl')}
    applicable=[r for r in data if r['stage']!='regression']
    for row in applicable:
        if row['key'] in done:continue
        v3.status(target,'semantic',status='running',current=row['key'],completed=len(done),total=len(applicable))
        if not row['quality']['execution']:
            grade={k:0 if k in ('correctness','task_completion') else None for k in FIELDS}
            grade.update(review_status='execution_failed',reason='应用无有效输出，引用支持不评分',reviewer_type='program')
        else:
            job={'question':row['question'],'user_history':row['user_history'],'rubric':row['rubric'],'answer':row['answer'],
                'task_kind':row['expected_action'] or 'knowledge',
                'reference_documents':[{'filename':name,'text':text} for _,name,text in old.DOCS+DOCUMENTS],
                'candidate_citations':row['citations'],'actual_ticket':row['ticket'],'tool_actions':row['tool_actions']}
            grade=judge.grade(job,target/'judge_attempts.jsonl')
        v3.append(target/'grades.jsonl',{'key':row['key'],**grade});done.add(row['key'])
    old.write(target/'judge_status.json',{'status':'complete','completed':len(done),'expected':len(applicable)})


def run(target,smoke=False):
    target.mkdir(parents=True,exist_ok=True)
    with (target/'queue.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        digest=fingerprint();protocol=target/'protocol.json'
        if protocol.exists() and json.loads(protocol.read_text())['fingerprint']!=digest:raise RuntimeError('代码变化，请使用新tag')
        plan=stages(smoke)
        if not protocol.exists():
            for path in source_files():
                dest=target/'source'/path.relative_to(old.PROJECT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
            old.write(protocol,{'fingerprint':digest,'stages':plan,'documents':old.DOCS+DOCUMENTS,
                'policies':POLICIES,'references':REFS,'model':old.cfg(target).llm_model,
                'evaluation':'authored development, not independent test','same_compute_budget':False,
                'judge_version':'v4_task_applicability'})
        data=v3.rows(target);done={r['key'] for r in data}
        totals={s:sum(len(c['turns']) for c in cases) for s,_,cases,_ in plan}
        try:
            for stage,arm,cases,extended in plan:
                v3.wait_service(target,'answer',stage);cfg,db=v3.prepare(target/stage/'app',extended)
                for case in cases:
                    prior=[r for r in data if r['stage']==stage and r['case_id']==case['id']]
                    cid=prior[-1]['conversation_id'] if prior else None
                    for index,spec in enumerate(case['turns']):
                        key=f'{stage}/{case["id"]}/{index}'
                        if key in done:continue
                        recorded={r['run_id'] for r in data if r['stage']==stage}
                        found=db.all("SELECT r.id FROM runs r JOIN users u ON r.user_id=u.id WHERE u.username=? AND r.question!='预置工单'",('case-'+case['id'],))
                        if any(r['id'] not in recorded for r in found):raise RuntimeError('存在未归档运行，停止自动重试以免重复办理')
                        if fingerprint()!=digest:raise RuntimeError('运行中代码改变，停止混跑')
                        v3.wait_service(target,'answer',key)
                        v3.status(target,stage,status='running',current=key,totals=totals,
                            counts={s:sum(r['stage']==s for r in data) for s in totals})
                        row=v3.evaluate_one(cfg,db,spec,case,arm,stage,index,cid);cid=row['conversation_id']
                        row['expected_action']=spec.get('action')
                        v3.append(target/'results.jsonl',row);data.append(row);done.add(key)
                        print(key,row['status'],row['quality'],flush=True)
                report(target)
            if not smoke:
                v3.status(target,'engineering',status='running')
                from business_v3_engineering import check
                check(target/'engineering',version='v4',port=8774)
                grade_all(target,data)
            report(target)
            errors=sum(not r['quality']['execution'] for r in data)
            failed_actions=sum(r['quality']['service_complete'] is False for r in data)
            passed=not errors and not failed_actions and all(r['quality']['permission_checks'] and r['quality']['no_auto_submission'] and r['quality']['draft_user_quote_integrity'] is not False for r in data)
            # Smoke covers predeclared concrete facts; missing a requested fact
            # blocks expansion, but keyword hits are never semantic accuracy.
            if smoke:
                passed=passed and all(all(r['probes'].values()) for r in data)
            if smoke:old.write(ART/'smoke_latest.json',{'passed':passed,'fingerprint':digest,'path':str(target)})
            v3.status(target,None,status='complete_pending_review' if passed else 'complete_with_errors',errors=errors,
                totals=totals,counts={s:sum(r['stage']==s for r in data) for s in totals},report=str(target/'report.md'))
            if smoke and not passed:raise RuntimeError('v4修复联调未通过，见逐题记录')
        except BaseException as exc:
            v3.status(target,locals().get('stage'),status='failed',error=str(exc),report=str(target/'report.md'));raise


def status_details(target):
    data=v3.rows(target)
    candidate=[r for r in data if r['arm']=='multi_v4']
    baseline=[r for r in data if r['arm']!='multi_v4']
    def execution(group):
        return {'completed':len(group),'passed':sum(r['quality']['execution'] for r in group),
                'errors':[{'key':r['key'],'error':r['error']} for r in group if not r['quality']['execution']]}
    def read(path):return json.loads(path.read_text()) if path.exists() else {'status':'pending'}
    from business_v4_review import VERSION
    return {'candidate':execution(candidate),'comparators':execution(baseline),
            'engineering':read(target/'engineering/report.json'),'original_judge':read(target/'judge_status.json'),
            'review':read(target/VERSION/'status.json'),'review_dir':target/VERSION}


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['smoke','run','launch','status','stop','report','serve','engineering','review-run','review-launch'])
    p.add_argument('--tag',default='dev_v1');p.add_argument('--data-dir');args=p.parse_args()
    if not args.tag.replace('_','').isalnum():raise ValueError('非法tag')
    ART.mkdir(parents=True,exist_ok=True);target=ART/args.tag
    if args.action=='smoke':run(ART/('smoke_'+str(time.time_ns())),True)
    elif args.action=='run':run(target)
    elif args.action=='report':report(target)
    elif args.action=='serve':
        import uvicorn
        from kb.app import create_app
        root=Path(args.data_dir).resolve()
        if not root.is_relative_to(ART):raise ValueError('验收数据须位于v4目录')
        cfg,_=v3.prepare(root,True)
        uvicorn.run(create_app(replace(cfg,agent_policy='business_multi_v4')),host='127.0.0.1',port=8774)
    elif args.action=='engineering':
        from business_v3_engineering import check
        v3.wait_service(target,'answer','engineering');print(check(target/'engineering',version='v4',port=8774))
    elif args.action in ('review-run','review-launch'):
        from business_v4_review import run as review_run,VERSION
        state=json.loads((target/'status.json').read_text())
        if not state['status'].startswith('complete') or state.get('counts')!=state.get('totals'):
            raise RuntimeError('只可复评已完成且归档完整的问答')
        if args.action=='review-run':review_run(target)
        else:
            review=target/VERSION;review.mkdir(parents=True,exist_ok=True)
            with (review/'queue.lock').open('w') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with (review/'queue.log').open('a') as log:
                proc=subprocess.Popen(['bash',str(old.PROJECT/'scripts/run_business.sh'),'v4','review-run','--tag',args.tag],
                    stdout=log,stderr=log,start_new_session=True)
            print('仅复评保存的回答；PID:',proc.pid,'目录:',review)
    elif args.action=='launch':
        receipt=json.loads((ART/'smoke_latest.json').read_text())
        if not receipt['passed'] or receipt['fingerprint']!=fingerprint():raise RuntimeError('当前版本尚未通过真实模型联调')
        target.mkdir(parents=True,exist_ok=True)
        with (target/'queue.lock').open('w') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (target/'queue.log').open('a') as log:
            proc=subprocess.Popen(['bash',str(old.PROJECT/'scripts/run_business.sh'),'v4','run','--tag',args.tag],
                stdout=log,stderr=log,start_new_session=True)
        print('队列PID:',proc.pid,'目录:',target)
    elif args.action in ('status','stop'):
        if not (target/'status.json').exists():print('尚无运行状态:',target);return
        status=json.loads((target/'status.json').read_text());pid=status['pid']
        alive=bool(status.get('start_ticks') and v2.ticks(pid)==status['start_ticks'])
        if args.action=='stop':
            if not alive:print('队列已停止');return
            if os.getpgid(pid)!=pid:raise RuntimeError('不是独立后台进程组')
            os.killpg(pid,signal.SIGTERM);print('已停止本评测队列');return
        print(f"问答队列状态（含旧对照）: {status['status']} | 阶段: {status.get('phase')} | PID: {pid} | 存活: {alive}")
        for stage,total in status.get('totals',{}).items():print(f"{stage:22} {status.get('counts',{}).get(stage,0):3}/{total}")
        print('当前:',status.get('current'),'错误:',status.get('error'))
        details=status_details(target)
        for name,key in [('新版v4执行','candidate'),('保留对照执行','comparators')]:
            d=details[key];print(f"{name}: {d['passed']}/{d['completed']} 正常，执行报错 {len(d['errors'])}")
            for error in d['errors']:print('  ',error['key'],error['error'])
        print('工程验收:',details['engineering'].get('status'),'原评分校准:',details['original_judge'].get('status'))
        review=details['review'];review_alive=bool(review.get('start_ticks') and v2.ticks(review.get('pid'))==review['start_ticks'])
        print('保存回答复评:',review['status'],'阶段:',review.get('phase'),'进度:',str(review.get('completed',0))+'/'+str(review.get('total','-')),
              '进程存活:',review_alive,'当前:',review.get('current'),'错误:',review.get('error'))
        if review['status']=='calibration_failed_pending_manual_review':
            print('评分校准未通过:',review.get('failed'),'例；未启动完整语义评分，请查看复评报告。问答执行结果仍已完整保存。')
        print('复评报告:',details['review_dir']/'report.md')
        print('报告:',target/'report.md');print('日志:',target/'queue.log')
        print('同批single/v3/v4；复用现有模型，CPU重排；无训练，工程验收用临时8774。')


if __name__=='__main__':main()
