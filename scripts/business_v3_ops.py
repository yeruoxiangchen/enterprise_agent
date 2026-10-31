"""Versioned project evaluation; CPU/reused services, bounded serial experiments."""
import argparse
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import signal
import time
import statistics
import httpx

import business_ops as old
import business_v2_ops as v2
from business_cases_v2 import CASES,DOCUMENTS
from business_cases_v3 import CASES as NEW_CASES
from kb.benchmark.business_quality import quality_checks,summarize,retrieval_at_5

ART=old.ROOT/'artifacts/business_multi_v3_20261030'
POLICIES={'fixed':'fixed_rag_v1','single':'business_single_v2','multi_v2':'business_multi_v2','multi_v3':'business_multi_v3'}


def files():
    return sorted(set(v2.source_files()+list((old.PROJECT/'scripts').glob('business_v3*.py'))+
        [old.PROJECT/'scripts/business_cases_v3.py',old.PROJECT/'scripts/business_browser.py']))


def fingerprint():
    return hashlib.sha256(b''.join(str(p.relative_to(old.PROJECT)).encode()+p.read_bytes() for p in files())).hexdigest()


def status(target,phase,**kw):
    previous=json.loads((target/'status.json').read_text()) if (target/'status.json').exists() else {}
    old.write(target/'status.json',{'pid':os.getpid(),'start_ticks':v2.ticks(os.getpid()),'updated_at':time.time(),
        **{k:previous[k] for k in ('counts','totals') if k in previous},'phase':phase,**kw})


def prepare(target,extended):
    """Idempotent seed scoped to public bases even after the private base exists."""
    cfg,db=old.prepare(target)
    if extended:
        bases={r['domain']:r['id'] for r in db.all("SELECT b.id,d.domain FROM bases b JOIN business_domains d ON b.id=d.base_id WHERE b.visibility='all'")}
        for domain,name,text in DOCUMENTS:
            doc,_=v2.register_document(db,cfg,bases[domain],name,text.encode());v2.ingest(db,cfg,doc['id'])
        if not db.one("SELECT id FROM bases WHERE name='评测受限资料'"):
            ident=v2.uid()
            db.execute('INSERT INTO bases VALUES(?,?,?,?,?)',(ident,'评测受限资料','只供权限检测','admin',time.time()))
            db.execute('INSERT INTO business_domains VALUES(?,?)',(ident,'technical'))
            doc,_=v2.register_document(db,cfg,ident,'内部发布口令.md',('内部发布口令：'+v2.CANARY).encode());v2.ingest(db,cfg,doc['id'])
    return cfg,db


def rows(target,name='results.jsonl'):
    p=target/name
    return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []


def append(path,row):
    with path.open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())


def wait_service(target,role,next_stage):
    """Wait for the configured service only; never allocate a GPU or restart it."""
    cfg=old.cfg(target)
    if role=='answer':url,model,key,tokenizer=cfg.llm_url,cfg.llm_model,cfg.llm_key,cfg.tokenize_url
    else:
        url=os.environ.get('KB_JUDGE_BASE_URL','');model=os.environ.get('KB_JUDGE_MODEL','')
        key=os.environ.get('KB_JUDGE_API_KEY','');tokenizer=os.environ.get('KB_JUDGE_TOKENIZE_URL','')
    if not url or not model or not tokenizer:raise ValueError(role+' 服务缺少URL、模型名或tokenize配置')
    headers={'Authorization':'Bearer '+key} if key else {}
    while True:
        try:
            with httpx.Client(timeout=10,trust_env=False,headers=headers) as client:
                response=client.get(url.rstrip('/')+'/models');response.raise_for_status()
                if model not in {r['id'] for r in response.json()['data']}:raise ValueError('服务模型名不匹配')
                response=client.post(tokenizer,json={'model':model,'messages':[{'role':'user','content':'连通验证'}],
                    'chat_template_kwargs':{'enable_thinking':False}});response.raise_for_status()
                if type(response.json().get('count')) is not int:raise ValueError('Token计数格式错误')
            return
        except (httpx.HTTPError,ValueError,KeyError) as exc:
            status(target,'service',status='waiting_for_model_service',service=role,next_stage=next_stage,
                error=type(exc).__name__)
            print('等待现有模型服务恢复:',role,'下一阶段:',next_stage,type(exc).__name__,flush=True)
            time.sleep(30)


def retrieval_report(target):
    """Replay existing DEV rankings only; no index rebuild or GPU generation."""
    dataset=old.ROOT/'datasets/git_support_zh_v1/dev.jsonl'
    gold={r['id']:r for r in [json.loads(s) for s in dataset.read_text().splitlines()]}
    origin=old.ROOT/'artifacts/git_support_v1/dev_ablation_v3_20261028'
    result={'protocol':'95% standard evidence span coverage; Hit=any; MRR=first cumulative evidence hit',
        'split':'dev','source_files':{},'arms':{}}
    ids=None
    for arm in ('retrieval-bm25','retrieval-rerank'):
        source=origin/arm/'predictions.jsonl';raw=source.read_bytes()
        data=[json.loads(s) for s in raw.decode().splitlines()]
        current={r['id'] for r in data}
        if len(current)!=len(data) or current!=set(gold) or (ids is not None and current!=ids):raise ValueError('检索回放题目不匹配或重复')
        ids=current;values=[retrieval_at_5(gold[r['id']]['evidence'],r['hits']) for r in data]
        valid=[r for r in values if r is not None]
        result['source_files'][arm]={'path':str(source),'sha256':hashlib.sha256(raw).hexdigest()}
        result['arms'][arm]={'evaluable':len(valid),**{k:statistics.mean(r[k] for r in valid) for k in valid[0]}}
    left,right=(result['arms'][name] for name in ('retrieval-bm25','retrieval-rerank'))
    result['rerank_minus_bm25']={k:right[k]-left[k] for k in left if k!='evaluable'}
    old.write(target/'retrieval_replay.json',result)
    return result


def report(target):
    data=rows(target);grades=rows(target,'grades.jsonl');summary=summarize(data,grades) if data else {}
    old.write(target/'summary.json',summary)
    text=['# 业务多Agent v3：项目分层评测','',
        '自建开发场景；旧17题、同批23轮四组对照、新增场景分别统计。没有消费冻结300题测试集。',
        '执行/权限/动作/原文完整性为程序检查。模型语义评审独立于答案生成且盲化方案名，但不是人工金标；未知评分保留。',
        '固定RAG无工单工具。拒绝越权可视为正确动作。状态和关键词仅辅助诊断。',
        '', '|阶段|正常返回|真实服务完成|草稿原文检查|权限检查|调用|p50秒|p95秒|',
        '|---|---|---|---|---|---|---|---|']
    for name,s in summary.items():
        m=s['metrics']
        def count(k):return f"{m[k]['passed']}/{m[k]['applicable']}"
        text.append(f"|{name}|{count('execution')}|{count('service_complete')}|{count('draft_user_quote_integrity')}|{count('permission_checks')}|{s['model_calls']}|{s['p50_seconds']:.2f}|{s['p95_seconds']:.2f}|")
    text+=['','## 语义评分（模型评审，非人工准确率）','']
    for name,s in summary.items():text.append('- '+name+'：'+json.dumps(s['semantic_model_review'],ensure_ascii=False))
    text+=['','## 检索重排回放','旧开发集BM25与BGE的Recall@5、Hit@5、MRR@5见retrieval_replay.json；分母、数据版本独立于本轮业务题。',
        '这些是既有检索配置的回放指标，不能归因为本轮多Agent修改。']
    rp=target/'retrieval_replay.json'
    if rp.exists():
        retrieval=json.loads(rp.read_text())
        for arm,value in retrieval['arms'].items():text.append('- '+arm+'：'+json.dumps(value,ensure_ascii=False))
        text.append('- 重排减BM25：'+json.dumps(retrieval['rerank_minus_bm25'],ensure_ascii=False))
    text+=['','## 意图路由与多轮完成','领域集合匹配只衡量领域分配，不能替代细粒度意图识别准确率或语义正确率。']
    for stage,s in summary.items():
        rr=[r for r in data if r['stage']==stage and r['quality']['route_match_diagnostic'] is not None]
        text.append(f"- {stage}: 领域集合匹配 {sum(r['quality']['route_match_diagnostic'] for r in rr)}/{len(rr)}；对话全部正常返回 {s['conversation_execution_complete']}/{s['conversations']}；语义完成见模型评审并等待人工抽检。")
    text+=['', '## 失败与待审计项']
    for r in data:
        q=r['quality'];bad=[k for k in ('execution','service_complete','draft_user_quote_integrity','permission_checks','no_auto_submission') if q[k] is False]
        if bad or r['error']:text.append(f"- {r['key']}: {bad}; error={r['error']}")
    text+=['','网页、HTTP并发和两路真实模型请求验收见engineering/report.json；共享服务耗时不外推生产吞吐。']
    for label,path in [('工程验收',target/'engineering/report.json'),('评审状态',target/'judge_status.json')]:
        if path.exists():
            value=json.loads(path.read_text());text.append(f"- {label}: {value.get('status')}；{value.get('error',value.get('reason',''))}")
    (target/'report.md').write_text('\n'.join(text)+'\n')
    review=['# 人工复核清单（尚未人工评定）','']
    for r in data:
        review += [f"## {r['key']}",r['question'],'判据：'+r['rubric'],r['answer'] or '(执行失败)',
            '真实工单：'+json.dumps(r['ticket'],ensure_ascii=False),'实际引用：'+json.dumps(r['citations'],ensure_ascii=False),
            '人工结论：待填写','']
    (target/'review.md').write_text('\n'.join(review))


def evaluate_one(cfg,db,spec,case,arm,stage,index,cid):
    # Reuse preparation/recording, preserving the v2 source and frozen results.
    v2.ARMS[arm]=POLICIES[arm]
    row=v2.evaluate_turn(cfg,db,spec,case['id'],arm,cid)
    row['checks']['route']=set(row['domains'])==set(spec['domains']) if arm.startswith('multi') else None
    row.update(stage=stage,turn=index,key=f'{stage}/{case["id"]}/{index}',category=case.get('category','regression'))
    row['quality']=quality_checks(row,spec,db)
    row['user_history']=db.all("SELECT role,content FROM messages WHERE conversation_id=? AND created_at<(SELECT created_at FROM runs WHERE id=?) ORDER BY created_at",
        (row['conversation_id'],row['run_id']))
    state=json.loads(db.one('SELECT state FROM runs WHERE id=?',(row['run_id'],))['state']).get('business',{})
    row['tool_actions']=state.get('receipts',[])
    row['route']=next((json.loads(e['payload']) for e in db.all("SELECT payload FROM events WHERE run_id=? AND kind='business_route'",(row['run_id'],))),{})
    return row


def grade_all(target,data):
    from business_v3_judge import BusinessJudge,calibration,FIELDS
    if not os.environ.get('KB_JUDGE_BASE_URL'):
        old.write(target/'judge_status.json',{'status':'not_configured','reason':'未调用外部评分服务；保留人工审阅包'});return
    wait_service(target,'judge','semantic')
    try:judge=BusinessJudge(old.ROOT)
    except Exception as exc:
        old.write(target/'judge_status.json',{'status':'unavailable','error':type(exc).__name__});return
    old.write(target/'judge_identity.json',judge.identity)
    if not (target/'calibration.json').exists():passed=calibration(judge,target)
    else:passed=all(r['passed'] for r in json.loads((target/'calibration.json').read_text()))
    if not passed:
        old.write(target/'judge_status.json',{'status':'calibration_failed','reason':'停止扩量评分，答案结果保留；需人工复核'});return
    done={r['key'] for r in rows(target,'grades.jsonl')}
    applicable=[r for r in data if r['stage']!='regression']
    for r in applicable:
        if r['key'] in done:continue
        status(target,'semantic',status='running',current=r['key'],completed=len(done),total=len(applicable))
        if not r['quality']['execution']:
            g={k:0 if k in ('correctness','task_completion') else None for k in FIELDS};g.update(review_status='execution_failed',reason='应用无有效输出；支持性不评分',reviewer_type='program')
        else:
            job={'question':r['question'],'user_history':r['user_history'],'rubric':r['rubric'],'answer':r['answer'],
                'reference_documents':[{'filename':name,'text':text} for _,name,text in old.DOCS+DOCUMENTS],
                'candidate_citations':r['citations'],'actual_ticket':r['ticket'],'tool_actions':r['tool_actions']}
            g=judge.grade(job,target/'judge_attempts.jsonl')
        append(target/'grades.jsonl',{'key':r['key'],**g});done.add(r['key'])
    old.write(target/'judge_status.json',{'status':'complete','completed':len(done),'expected':len(applicable)})


def run(target,smoke=False):
    target.mkdir(parents=True,exist_ok=True)
    with (target/'queue.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        digest=fingerprint();protocol=target/'protocol.json'
        if protocol.exists() and json.loads(protocol.read_text())['fingerprint']!=digest:raise RuntimeError('版本变化，请使用新tag')
        if not protocol.exists():
            for path in files():
                dest=target/'source'/path.relative_to(old.PROJECT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
            old.write(protocol,{'fingerprint':digest,'cases':CASES,'new_cases':NEW_CASES,'regression':old.CASES,
                'documents':old.DOCS+DOCUMENTS,'policies':POLICIES,'model':old.cfg(target).llm_model,
                'score_version':'business_quality_v3','same_compute_budget':False,'human_review':'pending'})
        regression=[{'id':i,'turns':[{'question':q,'domains':ds,'probes':[p] if p else [],'statuses':list(v2.SUCCESS),
            'rubric':'旧17题回归，按授权资料完成请求。','action':'draft' if i=='draft' else None}]} for i,q,ds,p in old.CASES]
        if smoke:
            selected=[c for c in CASES if c['id'] in ('access-extension','two-policy','followup-https','topic-switch','ticket-draft','diagnose-ticket')]
            stages=[('smoke','multi_v3',selected,True)]
        else:
            stages=[('regression','multi_v3',regression,False)]+[(arm,arm,CASES,True) for arm in POLICIES]+[('new_acceptance','multi_v3',NEW_CASES,True)]
        data=rows(target);done={r['key'] for r in data}
        totals={stage:sum(len(c['turns']) for c in cases) for stage,_,cases,_ in stages}
        try:
            for stage,arm,cases,extended in stages:
                wait_service(target,'answer',stage)
                cfg,db=prepare(target/stage/'app',extended)
                for case in cases:
                    existing=[r for r in data if r['stage']==stage and r['case_id']==case['id']]
                    cid=existing[-1]['conversation_id'] if existing else None
                    for index,spec in enumerate(case['turns']):
                        key=f'{stage}/{case["id"]}/{index}'
                        if key in done:continue
                        # A crash between a side effect and JSONL archival must never
                        # silently execute the same business request again.
                        recorded={r['run_id'] for r in data if r['stage']==stage}
                        existing_runs=db.all("SELECT r.id FROM runs r JOIN users u ON r.user_id=u.id WHERE u.username=? AND r.question!='预置工单'",('case-'+case['id'],))
                        if any(r['id'] not in recorded for r in existing_runs):raise RuntimeError('存在未归档业务运行，已停止自动重试以防重复办理；请检查该场景数据库和事件')
                        if fingerprint()!=digest:raise RuntimeError('运行期间源码变化，停止混跑')
                        wait_service(target,'answer',key)
                        status(target,stage,status='running',current=key,totals=totals,counts={s:sum(r['stage']==s for r in data) for s in totals})
                        row=evaluate_one(cfg,db,spec,case,arm,stage,index,cid);cid=row['conversation_id']
                        append(target/'results.jsonl',row);data.append(row);done.add(key)
                        print(key,row['status'],'calls=',row['calls'],'quality=',row['quality'],flush=True)
                report(target)
            if not smoke:
                status(target,'retrieval_replay',status='running');retrieval_report(target)
                status(target,'engineering',status='running')
                from business_v3_engineering import check
                check(target/'engineering')
                grade_all(target,data)
            report(target)
            errors=sum(not r['quality']['execution'] for r in data)
            engineering=json.loads((target/'engineering/report.json').read_text()) if (target/'engineering/report.json').exists() else {}
            judge=json.loads((target/'judge_status.json').read_text()) if (target/'judge_status.json').exists() else {}
            status(target,None,status='complete_with_errors' if errors else 'complete_pending_review',errors=errors,
                engineering=engineering.get('status'),semantic_review=judge.get('status'),
                totals=totals,counts={s:sum(r['stage']==s for r in data) for s in totals},report=str(target/'report.md'))
            if smoke:
                passed=not errors and all(r['quality']['service_complete'] is not False and r['quality']['permission_checks'] for r in data)
                old.write(ART/'smoke_latest.json',{'passed':passed,'fingerprint':digest,'path':str(target)})
                if not passed:raise RuntimeError('v3真实联调未通过')
        except BaseException as exc:
            status(target,locals().get('stage'),status='failed',error=str(exc),report=str(target/'report.md'));raise


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['smoke','run','suite','launch','status','stop','report','retrieval','serve','engineering'])
    p.add_argument('--tag',default='dev_v1');p.add_argument('--data-dir');p.add_argument('--preflight',action='store_true');args=p.parse_args()
    if not args.tag.replace('_','').isalnum():raise ValueError('非法tag')
    ART.mkdir(parents=True,exist_ok=True);target=ART/args.tag
    if args.action=='serve':
        import uvicorn
        from kb.app import create_app
        root=Path(args.data_dir).resolve()
        if not root.is_relative_to(ART):raise ValueError('验收数据库必须位于v3产物目录')
        cfg,_=prepare(root,True)
        uvicorn.run(create_app(replace(cfg,agent_policy='business_multi_v3')),host='127.0.0.1',port=8773)
    elif args.action=='engineering':
        target.mkdir(parents=True,exist_ok=True);wait_service(target,'answer','engineering')
        from business_v3_engineering import check
        result=check(target/'engineering');print(json.dumps(result,ensure_ascii=False))
        if result['status']!='passed':raise SystemExit(1)
    elif args.action=='smoke':run(ART/('smoke_'+str(time.time_ns())),True)
    elif args.action=='run':run(target)
    elif args.action=='suite':
        target.mkdir(parents=True,exist_ok=True)
        with (target/'controller.lock').open('w') as controller:
            fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
            status(target,'preflight',status='running')
            try:run(target/'preflight',True)
            except BaseException as exc:
                status(target,'preflight',status='failed_preflight',error=str(exc),report=str(target/'preflight/report.md'));raise
            run(target)
    elif args.action=='report':report(target)
    elif args.action=='retrieval':
        target.mkdir(parents=True,exist_ok=True);print(json.dumps(retrieval_report(target),ensure_ascii=False))
    elif args.action=='status':
        path=target/'status.json'
        if not path.exists():print('尚未启动');return
        s=json.loads(path.read_text());alive=v2.ticks(s['pid'])==s.get('start_ticks') and s.get('start_ticks') is not None
        print('总状态:',s['status'],'| 阶段:',s['phase'],'| PID:',s['pid'],'| 存活:',alive)
        if s['phase']=='preflight' and (target/'preflight/status.json').exists():
            sub=json.loads((target/'preflight/status.json').read_text())
            print('先导验证:',sub.get('status'),sub.get('counts',{}).get('smoke',0),'/ 8','当前:',sub.get('current'),'错误:',sub.get('error'))
            if sub.get('service'):print('等待服务:',sub['service'],'下一阶段:',sub.get('next_stage'))
        for name,total in s.get('totals',{}).items():print(name,s.get('counts',{}).get(name,0),'/',total)
        if s['phase']=='semantic':print('语义评分:',s.get('completed'), '/',s.get('total'))
        print('当前:',s.get('current'),'| 错误:',s.get('error'),'| 已记录执行错误:',s.get('errors'))
        if s.get('service'):print('等待服务:',s['service'],'下一阶段:',s.get('next_stage'))
        print('工程验收:',s.get('engineering'),'| 语义评审:',s.get('semantic_review'))
        print('报告:',s.get('report',str(target/'report.md')));print('日志:',target/'queue.log')
        print('复用现有回答/评分服务，重排CPU；不训练、不启动GPU模型，工程验收使用临时8773端口。')
    elif args.action=='stop':
        s=json.loads((target/'status.json').read_text());pid=s['pid']
        if not s.get('start_ticks') or v2.ticks(pid)!=s['start_ticks']:print('队列已停止');return
        if os.getpgid(pid)!=pid:raise RuntimeError('进程不是独立后台队列，不发送组信号')
        os.killpg(pid,signal.SIGTERM);print('已向该评测队列发送停止信号；不会停止外部模型服务。')
    elif args.action=='launch':
        if not args.preflight:
            receipt=json.loads((ART/'smoke_latest.json').read_text())
            if not receipt['passed'] or receipt['fingerprint']!=fingerprint():raise RuntimeError('当前代码真实联调未通过；可加 --preflight 后台验证，通过后自动继续')
        target.mkdir(parents=True,exist_ok=True)
        with (target/'controller.lock').open('w') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (target/'queue.lock').open('w') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (target/'queue.log').open('a') as log:
            proc=subprocess.Popen(['bash',str(old.PROJECT/'scripts/run_business.sh'),'v3','suite' if args.preflight else 'run','--tag',args.tag],stdout=log,stderr=log,start_new_session=True)
        print('队列PID:',proc.pid,'目录:',target)


if __name__=='__main__':main()
