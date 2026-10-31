"""Bounded supplementary evaluation on the existing business scenarios.

Reuse saved answers, calibrated content/citation reviewers and DB-backed runner.
No new questions, no model training, no production-web changes. The disputed
factual_fidelity axis stays unknown instead of blocking independent dimensions.
"""
import argparse
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

import business_v4_ops as v4
import business_v3_ops as ops
import business_v2_ops as v2
import business_ops as old
from business_v3_judge import BusinessJudge as Transport
from business_v4_review import AxisJudge,COMMON,PROMPTS,payload,make_job
from kb.benchmark.business_quality import summarize

VERSION='supplement_v1'
AXES=('correctness','citation_support')
REPEAT_CASES=('https-401','proxy-access','known-and-missing','followup-https','diagnose-ticket','other-ticket')
CHECKS=('execution','permission_checks','no_auto_submission','service_complete','draft_user_quote_integrity')


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def literal(path,name):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('缺少冻结常量 '+name)


def case_plan(protocol):
    cases=next(cases for stage,_,cases,_ in protocol['stages'] if stage=='multi_v4')
    selected=[c for c in cases if c['id'] in REPEAT_CASES]
    if {c['id'] for c in selected}!=set(REPEAT_CASES):raise ValueError('原评估集缺少重复测试场景')
    return selected


def completion(correctness,quality):
    if any(quality.get(k) is False for k in CHECKS) or correctness==0:return 0
    return 1 if correctness==1 else None


def calibration_axes(results):
    output={}
    for axis in AXES:
        applicable=[r for r in results if axis in r['expected']]
        passed=sum(r['grade'].get(axis)==r['expected'][axis] for r in applicable)
        output[axis]={'passed':passed,'expected':len(applicable),
                      'enabled':bool(applicable) and passed==len(applicable)}
    return output


class Reviewer(AxisJudge):
    def grade_axes(self,job,target,enabled):
        parts={}
        for axis in AXES:
            if not enabled[axis]['enabled']:
                parts[axis]={'review_status':'uncalibrated','reason':'本维度校准未通过，待复核'};continue
            self.system_prompt=COMMON+PROMPTS[axis]
            parts[axis]=Transport.grade(self,payload(job,axis),target.with_name(target.stem+'_'+axis+'.jsonl'))
        values={axis:p.get('score') if p.get('review_status')=='ok' else None for axis,p in parts.items()}
        return {**values,'parts':parts,'factual_fidelity':None,
                'review_status':'ok' if all(x is not None for x in values.values()) else 'partial',
                'reviewer_type':'model','judge_calls':sum(p.get('judge_calls',0) for p in parts.values())}


def calibrate(source,target,judge):
    receipt=source/'review_split_v2'
    previous=json.loads((receipt/'calibration.json').read_text())
    protocol=json.loads((receipt/'protocol.json').read_text())
    identical=(json.loads((receipt/'judge_identity.json').read_text())==judge.identity and
               protocol['common_prompt']==COMMON and all(protocol['prompts'][k]==PROMPTS[k] for k in AXES))
    if identical:
        results=previous;origin='reuse_identical_model_and_prompts'
    else:
        results=[];origin='rerun_existing_calibration_only'
        for i,r in enumerate(previous):
            g=judge.grade_axes(r['job'],target/f'calibration_{i}.jsonl',{k:{'enabled':True} for k in AXES})
            results.append({'job':r['job'],'expected':r['expected'],'grade':g})
    enabled=calibration_axes(results)
    old.write(target/'calibration.json',{'origin':origin,'source':str(receipt),'axes':enabled,
        'results':results,'excluded_axis':'factual_fidelity: original boundary disagreement preserved'})
    return enabled


def all_rows(source,target):return ops.rows(source)+ops.rows(target,'repeat_results.jsonl')


def paired(rows,grades):
    grades={g['key']:g for g in grades};out={}
    for baseline in ('single','multi_v3'):
        left={(r['case_id'],r['turn']):r for r in rows if r['stage']==baseline}
        right={(r['case_id'],r['turn']):r for r in rows if r['stage']=='multi_v4'}
        result={k:[] for k in ('win','loss','both_pass','both_fail','unknown')}
        for key in sorted(left.keys()&right.keys()):
            a=grades.get(left[key]['key'],{}).get('task_completion');b=grades.get(right[key]['key'],{}).get('task_completion')
            label='unknown' if a is None or b is None else ('both_pass' if a==b==1 else 'both_fail' if a==b==0 else 'win' if b>a else 'loss')
            result[label].append({'case_id':key[0],'turn':key[1],'baseline':left[key]['key'],'candidate':right[key]['key']})
        out[baseline]={'counts':{k:len(v) for k,v in result.items()},'pairs':result}
    return out


def stability(rows,grades,cases):
    scores={g['key']:g for g in grades};lookup={(r['stage'],r['case_id'],r['turn']):r for r in rows}
    result=[]
    for case in cases:
        expected=[(stage,case['id'],i) for stage in ('multi_v4','repeat_1','repeat_2') for i in range(len(case['turns']))]
        observed=[lookup[k] for k in expected if k in lookup];complete=len(observed)==len(expected)
        tasks=[scores.get(r['key'],{}).get('task_completion') for r in observed]
        result.append({'case_id':case['id'],'expected_turns':len(expected),'observed_turns':len(observed),
            'all_execution_pass':all(r['quality']['execution'] for r in observed) if complete else None,
            'all_task_pass':all(x==1 for x in tasks) if complete and None not in tasks else None})
    return result


def report(source,target,protocol):
    rows=all_rows(source,target);grades=ops.rows(target,'grades.jsonl');scores=summarize(rows,grades)
    pairs=paired(rows,grades);repeat=stability(rows,grades,case_plan(protocol))
    old.write(target/'summary.json',{'stages':scores,'paired_task_completion':pairs,'repeat_scenarios':repeat})
    text=['# 固定业务评估集：补充评测','',
        '原99轮回答复评；6个现有场景各追加2次运行，共14轮。原问题、判据、资料、旧结果不修改。',
        '内容与引用使用已校准的独立分项提示；同家族不同权重模型评审，未经独立人工金标验证。',
        '完整任务通过=内容正确且适用的执行/权限/确认提交/办理回执/草稿原文检查均通过。',
        '争议的事实忠实性不发布分数；未知评分单列。引用支持对纯拒答等无需引用的答复按通过计算。',
        '括号内为待审数；分母为已评分数。共享模型服务耗时不等于独占资源基准；没有记录回答Token用量，不估造Token或金额成本。',
        '', '|阶段|正常返回|内容正确（待审）|引用支持（待审）|完整任务（待审）|服务检查|模型调用|P50秒|P95秒|',
        '|---|---|---|---|---|---|---|---|---|']
    for stage,s in scores.items():
        m=s['metrics'];sem=s['semantic_model_review']
        def sc(k):
            q=sem[k];return f"{q['pass']}/{q['pass']+q['fail']}（{q['unknown']}）"
        def qc(k):return f"{m[k]['passed']}/{m[k]['applicable']}"
        text.append(f"|{stage}|{qc('execution')}|{sc('correctness')}|{sc('citation_support')}|{sc('task_completion')}|{qc('service_complete')}|{s['model_calls']}|{s['p50_seconds']:.2f}|{s['p95_seconds']:.2f}|")
    text+=['','## 同题配对：新版多Agent的完整任务结果']
    for name,p in pairs.items():text.append('- 对比 '+name+'：'+json.dumps(p['counts'],ensure_ascii=False))
    text+=['','## 原样重复测试','固定6个场景，原运行加两次追加；每次隔离应用数据库。以下为场景全部轮次、全部3次运行是否通过。']
    for r in repeat:text.append('- '+json.dumps(r,ensure_ascii=False))
    text+=['','## 工程验收与缺失指标',
        '原工程验收直接引用，不重复执行：'+str(source/'engineering/report.json'),
        '72次认证读取API、2个真实并发问答、浏览器草稿到解决流程已验收；不声称生产压测。',
        '本批没有独立相关文档标注，不新增Recall/MRR数字，也不把此前其他数据集的检索成绩混入本表。',
        '', '## 未通过或待复核记录']
    bykey={r['key']:r for r in rows}
    for g in grades:
        if g.get('task_completion')!=1 or g.get('citation_support')!=1:
            r=bykey[g['key']];text += [f"- {g['key']}: 内容={g.get('correctness')}，引用={g.get('citation_support')}，完整任务={g.get('task_completion')}",
                '  '+ '；'.join(k+': '+p.get('reason','') for k,p in g.get('parts',{}).items())]
    (target/'report.md').write_text('\n'.join(text)+'\n')


def grade_rows(source,target,rows,judge,enabled,protocol):
    done={g['key'] for g in ops.rows(target,'grades.jsonl')}
    for row in rows:
        if row['key'] in done:continue
        ops.status(target,'grading',status='running',current=row['key'],completed=len(done),total=113)
        if not row['quality']['execution']:
            grade={'correctness':0,'citation_support':None,'factual_fidelity':None,'parts':{},
                   'review_status':'execution_failed','reviewer_type':'program'}
        else:
            job=make_job(row,protocol)
            if row['stage']=='regression':
                docs=literal(source/'source/scripts/business_ops.py','DOCS')
                job['reference_documents']=[{'filename':n,'text':t} for _,n,t in docs]
            name=hashlib.sha256(row['key'].encode()).hexdigest()[:20]
            grade=judge.grade_axes(job,target/'attempts'/name,enabled)
        grade['task_completion']=completion(grade['correctness'],row['quality'])
        ops.append(target/'grades.jsonl',{'key':row['key'],**grade});done.add(row['key'])
        print(row['key'],{k:grade[k] for k in (*AXES,'task_completion')},flush=True)
        report(source,target,protocol)


def repeat_runs(source,target,protocol):
    data=ops.rows(target,'repeat_results.jsonl');done={r['key'] for r in data}
    for stage in ('repeat_1','repeat_2'):
        ops.wait_service(target,'answer',stage);cfg,db=ops.prepare(target/stage/'app',True)
        for case in case_plan(protocol):
            prior=[r for r in data if r['stage']==stage and r['case_id']==case['id']]
            cid=prior[-1]['conversation_id'] if prior else None
            for i,spec in enumerate(case['turns']):
                key=f'{stage}/{case["id"]}/{i}'
                if key in done:continue
                recorded={r['run_id'] for r in data if r['stage']==stage}
                found=db.all("SELECT r.id FROM runs r JOIN users u ON r.user_id=u.id WHERE u.username=? AND r.question!='预置工单'",('case-'+case['id'],))
                if any(r['id'] not in recorded for r in found):raise RuntimeError('有未归档运行；停止自动重复办理，见测试数据库')
                ops.status(target,'repeat',status='running',current=key,completed=len(done),total=14)
                row=ops.evaluate_one(cfg,db,spec,case,'multi_v4',stage,i,cid);row['expected_action']=spec.get('action')
                cid=row['conversation_id'];ops.append(target/'repeat_results.jsonl',row);data.append(row);done.add(key)
                print(key,row['quality'],flush=True)
    return data


def source_receipt(source):
    paths=[source/'results.jsonl',source/'protocol.json',source/'review_split_v2/calibration.json',
           source/'review_split_v2/judge_identity.json',source/'review_split_v2/protocol.json',
           source/'source/scripts/business_ops.py']
    paths+=v4.source_files()+[Path(__file__)]
    return {str(p):digest(p) for p in sorted(set(paths))}


def run(source,target):
    target.mkdir(parents=True,exist_ok=True)
    with (target/'queue.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        protocol=json.loads((source/'protocol.json').read_text());case_plan(protocol)
        # Same application code and knowledge fixtures for repeated trials.
        for p in list((old.PROJECT/'kb').rglob('*.py'))+[old.PROJECT/'scripts/business_cases_v2.py',old.PROJECT/'scripts/business_cases_v3.py']:
            frozen=source/'source'/p.relative_to(old.PROJECT)
            if frozen.exists() and p.read_bytes()!=frozen.read_bytes():raise RuntimeError('应用代码与原评测不一致：'+str(p))
        manifest=source_receipt(source);receipt=target/'protocol.json'
        if receipt.exists() and json.loads(receipt.read_text())['hashes']!=manifest:raise RuntimeError('输入/源码改变，请另建补测版本')
        if not receipt.exists():
            old.write(receipt,{'hashes':manifest,'source':str(source),'repeat_cases':REPEAT_CASES,'additional_trials':2,
                'axes':AXES,'excluded_axis':'factual_fidelity','dataset_changed':False,
                'completion_rule':'correctness AND applicable deterministic business checks',
                'expected_original':99,'expected_repeat':14})
            for p in [Path(__file__),old.PROJECT/'scripts/business_v4_review.py']:
                shutil.copy2(p,target/p.name)
        (target/'attempts').mkdir(exist_ok=True)
        try:
            ops.status(target,'judge',status='running',completed=0,total=113)
            ops.wait_service(target,'judge','grading');judge=Reviewer(old.ROOT)
            identity=target/'judge_identity.json'
            if identity.exists() and json.loads(identity.read_text())!=judge.identity:raise RuntimeError('评分模型身份改变')
            old.write(identity,judge.identity);enabled=calibrate(source,target,judge)
            report(source,target,protocol)
            grade_rows(source,target,ops.rows(source),judge,enabled,protocol)
            repeat=repeat_runs(source,target,protocol)
            grade_rows(source,target,repeat,judge,enabled,protocol)
            pending=sum(g['review_status']=='partial' for g in ops.rows(target,'grades.jsonl'))
            ops.status(target,None,status='complete_with_review_items' if pending else 'complete',
                completed=len(ops.rows(target,'grades.jsonl')),total=113,review_items=pending,
                repeat_completed=len(repeat),repeat_total=14,report=str(target/'report.md'))
            report(source,target,protocol)
        except BaseException as exc:
            ops.status(target,None,status='failed',error=str(exc),report=str(target/'report.md'));raise


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['launch','run','status','stop','report'])
    p.add_argument('--tag',default='dev_v1');a=p.parse_args()
    if not a.tag.replace('_','').isalnum():raise ValueError('非法tag')
    source=v4.ART/a.tag;target=source/VERSION
    if a.action=='run':run(source,target)
    elif a.action=='report':report(source,target,json.loads((source/'protocol.json').read_text()))
    elif a.action=='launch':
        original=json.loads((source/'status.json').read_text())
        if not original['status'].startswith('complete'):raise ValueError('原问答尚未完成')
        target.mkdir(parents=True,exist_ok=True)
        with (target/'queue.lock').open('w') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (target/'queue.log').open('a') as log:
            proc=subprocess.Popen(['bash',str(old.PROJECT/'scripts/run_business.sh'),'supplement','run','--tag',a.tag],
                                  stdout=log,stderr=log,start_new_session=True)
        print('补测PID:',proc.pid,'目录:',target)
    else:
        if not (target/'status.json').exists():print('尚未启动:',target);return
        s=json.loads((target/'status.json').read_text());pid=s['pid'];alive=v2.ticks(pid)==s.get('start_ticks')
        if a.action=='stop':
            if alive and os.getpgid(pid)==pid:os.killpg(pid,signal.SIGTERM)
            print('本补测队列已停止');return
        print('总状态:',s['status'],'阶段:',s.get('phase'),'PID:',pid,'存活:',alive)
        print('已评分:',len(ops.rows(target,'grades.jsonl')),'/113；追加重复运行:',len(ops.rows(target,'repeat_results.jsonl')),'/14')
        print('当前:',s.get('current'),'错误:',s.get('error'))
        print('报告:',target/'report.md');print('日志:',target/'queue.log')
        print('固定原数据；复用现有回答/评分服务，CPU重排；无训练，不改在线网页。')


if __name__=='__main__':main()
