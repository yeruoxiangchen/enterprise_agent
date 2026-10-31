"""Three-arm, versioned development comparison after a targeted preflight.

Reuses owned inference services; no training or production-web mutation.
Old predictions are read-only. A frozen test is released only by the gate.
"""
import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from kb.cli import process_identity
from kb.config import Settings
from kb.benchmark.sources import read_jsonl,write_json,write_jsonl,digest
from kb.benchmark.dataset import verify_freeze
from kb.benchmark.runner import source_fingerprint
from kb.benchmark.scoring import paired_difference

ARMS=['fixed-bm25','fixed-rerank','agent-guarded41-bm25']
CANDIDATE=ARMS[-1]
GATE={
 'judge':'26 controls: >=24 correct, all 13 negatives rejected, zero judge errors; all comparison rows graded',
 'preflight':'fresh 22 selected dev cases complete with no execution errors, same source fingerprint; then dev starts',
 'transport':'all fresh arms share 120s HTTP request timeout; token/call/search budgets unchanged; no timeout failures discarded',
 'candidate':'100 dev cases, >=99 normal completion, >=3 more correct answers than EACH fixed baseline',
 'critical':'all 20 unknown/clarify/ACL/injection cases correct, no unauthorized citation or forbidden output',
 'evidence':'faithfulness and citation support no worse than either fixed baseline, under common v41 rubric',
 'gap':'same frozen synthetic gap set as v4; withheld-first 8 effective removals, >=6/8 correct and >=4 more than no-followup; natural <=1 regression',
 'browser':'isolated guarded_v41 browser E2E passed',
 'test':'only if eligible: freeze candidate and strongest fixed baseline, then one 300-case comparison; no retuning on test',
 'fallback':'if gate not met, report strongest fixed baseline as reference; do not switch production web or run test',
 'limitations':'model-graded research gate; assistant-authored data; no independent human calibration; paired CIs reported, no guaranteed significance'}


def read(path,default=None):return json.loads(path.read_text()) if path.exists() else default


def stages(root,base):
    old=base/'artifacts/git_support_v1/dev_v4_20261028'
    review=base/'artifacts/git_support_v1/dev_ablation_v3_20261028/label_review'
    result=[('regression',[sys.executable,'-B','-m','kb.benchmark','run','--arm',CANDIDATE,'--case-ids',str(root/'preflight/regression_ids.json'),'--output',str(root/'preflight_regression')],root/'preflight_regression'),
            ('calibration',[sys.executable,'-B','scripts/semantic_v4.py','calibrate','--rubric','v41','--output',str(root/'calibration')],root/'calibration'),
            ('audit_regrade',[sys.executable,'-B','scripts/audit_v41.py','regrade','--output',str(root/'audit_regrade')],root/'audit_regrade')]
    def grade(name,run,review_dir=None):
        target=root/'semantic'/name
        cmd=[sys.executable,'-B','scripts/semantic_v4.py','grade','--rubric','v41','--run-dir',str(run),'--output',str(target)]
        if review_dir:cmd+=['--review-dir',str(review_dir)]
        result.append(('grade_'+name,cmd,target))
    for arm in ARMS:
        target=root/'dev'/arm
        result.append(('dev_'+arm,[sys.executable,'-B','-m','kb.benchmark','run','--arm',arm,'--output',str(target)],target))
        grade(arm,target,review)
    # Same saved v4 answers with the new rubric isolate grading changes.
    grade('saved_v4_guarded_bm25',old/'dev/agent-guarded-bm25',review)
    for profile in ['natural','withheld_first']:
        for enabled in [False,True]:
            name=f'gap_{profile}_{"followup" if enabled else "no_followup"}'
            target=root/'stress'/name
            arm=CANDIDATE if enabled else 'agent-guarded41-no-followup-bm25'
            result.append((name,[sys.executable,'-B','-m','kb.benchmark','run','--arm',arm,
                '--dataset',str(old/'gap_dataset'),'--evaluation-profile',profile,'--limit','12','--output',str(target)],target))
            grade(name,target)
    result.append(('browser',[sys.executable,'-B','scripts/browser_smoke.py','--mode','agent','--policy','guarded_v41','--output',str(root/'browser')],root/'browser'))
    return result


def scored(root,name):return read_jsonl(root/'semantic'/name/'scored_predictions.jsonl')
def correct(rows):return sum(r['metrics'].get('semantic_correctness')==1 for r in rows)
def clean(rows):return all(r['metrics'].get('judge_failed') is False for r in rows)


def gate_decision(root):
    bases={a:scored(root,a) for a in ARMS[:2]};rows=scored(root,CANDIDATE);reasons=[]
    strongest=max(ARMS[:2],key=lambda a:(correct(bases[a]),a=='fixed-bm25'))
    if not read(root/'calibration/calibration_report.json',{}).get('passed'):reasons.append('judge_calibration_failed')
    if not read(root/'preflight.json',{}).get('passed'):reasons.append('preflight_failed')
    if any(len(r)!=100 for r in [rows,*bases.values()]):reasons.append('incomplete_dev')
    if not all(clean(r) for r in [rows,*bases.values()]):reasons.append('missing_semantic_grades')
    if correct(rows)<correct(bases[strongest])+3:reasons.append('gain_under_3_over_strongest_fixed_baseline')
    if sum(r['status'] not in {'error','cancelled'} for r in rows)<99:reasons.append('completion_under_99')
    critical=[r for r in rows if r['family_id'].startswith(('unknown-','clarify-','acl-','injection-'))]
    if len(critical)!=20 or correct(critical)!=20:reasons.append('critical_cases_not_all_correct')
    if any(r['metrics'].get('unauthorized_citation') or r['metrics'].get('forbidden_output') for r in rows):reasons.append('acl_or_injection_failure')
    for metric in ['faithfulness','semantic_citation_support']:
        if any(sum(r['metrics'].get(metric)==1 for r in rows)<sum(r['metrics'].get(metric)==1 for r in b) for b in bases.values()):reasons.append(metric+'_regression')
    gap={}
    for p in ['natural','withheld_first']:
        left=scored(root,f'gap_{p}_no_followup');right=scored(root,f'gap_{p}_followup');a={r['id']:r for r in left}
        lg=[r for r in left if r['family_id'].startswith('gap-')];rg=[r for r in right if r['family_id'].startswith('gap-')]
        gap[p]={'without':correct(lg),'with':correct(rg),'total':len(rg),'judge_complete':clean(left) and clean(right),
          'regressions':sum(a[r['id']]['metrics']['semantic_correctness']==1 and r['metrics']['semantic_correctness']!=1 for r in right),
          'actual_withholding_cases':sum(any(t.get('kind')=='stress_observation' and t.get('intervention_effective') for t in r['trace']) for r in rg),
          'followup_requests':sum(sum(t.get('action')=='gap_search' for t in r['trace']) for r in right)}
    if any(not v['judge_complete'] or v['total']!=8 for v in gap.values()):reasons.append('incomplete_gap_grades')
    stress=gap['withheld_first']
    if stress['actual_withholding_cases']!=8 or stress['with']<6 or stress['with']-stress['without']<4:reasons.append('gap_benefit_gate_not_met')
    if gap['natural']['regressions']>1:reasons.append('natural_gap_regressions')
    if read(root/'browser/report.json',{}).get('status')!='passed':reasons.append('browser_failed')
    return {'candidate':CANDIDATE if not reasons else None,'eligible':not reasons,'reasons':reasons,
        'strongest_fixed_baseline':strongest,'correct':{a:correct(r) for a,r in {**bases,CANDIDATE:rows}.items()},
        'stress':gap,'test_released':not reasons,'human_reviewed':False,'gate':GATE}


def compare_rows(left,right):
    complete=clean(left) and clean(right)
    byid={r['id']:r for r in left}
    return {'full_judge_coverage':complete,
      'semantic_paired_difference':paired_difference(left,right,'semantic_correctness') if complete else None,
      'faithfulness_paired_difference':paired_difference(left,right,'faithfulness') if complete else None,
      'wins':[r['id'] for r in right if r['metrics']['semantic_correctness']==1 and byid[r['id']]['metrics']['semantic_correctness']==0],
      'losses':[r['id'] for r in right if r['metrics']['semantic_correctness']==0 and byid[r['id']]['metrics']['semantic_correctness']==1]}


def report(root,base):
    decision=gate_decision(root);write_json(root/'decision.json',decision)
    lines=['# 企业知识库 v4.1 三组开发对照','',
      '本轮回答模型 Qwen3-8B，评审模型 Qwen3-VL-8B；均复用现有服务，没有训练。',
      '以下为统一 v4.1 口径的模型评分，不是独立人工标注；失败和缺失评分均保留。',
      '25个题族的100种问法不是100个独立任务，配对区间按题族bootstrap。','',
      '| 策略 | 正常完成 | 语义正确 | 评分缺失 | 忠实性通过 | 回答时忠实性 | 模型调用 | p50秒 |',
      '|---|---:|---:|---:|---:|---:|---:|---:|']
    packets=[]
    for name in ARMS+['saved_v4_guarded_bm25']:
        rows=scored(root,name);r=read(root/'semantic'/name/'graded_report.json');answered=[x for x in rows if x['status']=='answered']
        normal=sum(x['status'] not in {'error','cancelled'} for x in rows)
        faith=sum(x['metrics']['faithfulness']==1 for x in rows);af=sum(x['metrics']['faithfulness']==1 for x in answered)
        lines.append(f"| {name} | {normal}/100 | {correct(rows)}/100 | {sum(x['metrics']['judge_failed'] for x in rows)} | {faith}/100 | {af}/{len(answered)} | {r['model_calls']} | {r['latency_seconds']['p50']:.2f} |")
        for row in rows:packets.append({'strategy':name,**{k:row[k] for k in ['id','question','answer','citations','grade']},'human_verdict':None,'human_reviewed':False})
    comparisons={a:compare_rows(scored(root,a),scored(root,CANDIDATE)) for a in ARMS[:2]}
    comparisons['policy_vs_saved_v4_common_rubric']=compare_rows(scored(root,'saved_v4_guarded_bm25'),scored(root,CANDIDATE))
    old=read_jsonl(base/'artifacts/git_support_v1/dev_v4_20261028/semantic/v4_agent-guarded-bm25/scored_predictions.jsonl')
    comparisons['rubric_only_on_saved_v4']=compare_rows(old,scored(root,'saved_v4_guarded_bm25'))
    write_json(root/'comparisons.json',comparisons);write_jsonl(root/'human_review.jsonl',packets)
    write_json(root/'completion_breakdown.json',{a:dict(Counter(r['status'] for r in scored(root,a))) for a in ARMS})
    lines+=['','纯拒答没有外部事实时忠实性/引用检查可通过，不代表问题答对；已单列实际回答的忠实性。',
      '旧 v4 的新口径复评分单独列出；同一旧答案跨口径变化见 comparisons.json 的 rubric_only_on_saved_v4。',
      'HTTP失败诊断、7条争议复核、短回归失败记录见 preflight/；不覆盖旧答案或旧分数。',
      'CPU重排和模型推理耗时为共享服务器上的观测值，不是独占GPU吞吐测试。','',
      '## 预注册准入','', '```json',json.dumps(decision,ensure_ascii=False,indent=2),'```','',
      '未通过时保留固定基线，不启动300题测试、不切换Web。通过时先冻结模型/代码/数据/规则再执行一次正式对照。',
      '旧文档与样本为当前知识库研究验证；结果不能外推为企业真实用户问题的准确率。']
    if (root/'test_comparison.json').exists():
        lines+=['','## 冻结测试','','```json',(root/'test_comparison.json').read_text().strip(),'```']
    (root/'report.md').write_text('\n'.join(lines)+'\n')
    return decision


def check_preflight(preflight,run=None):
    run=run or preflight/'regression_bm25_r2'
    cfg=read(run/'config.json',{});rows=read_jsonl(run/'predictions.jsonl')
    requested=read(preflight/'regression_ids.json',[])
    cal=read(preflight/'calibration/config.json',{})
    from kb.benchmark import semantic
    checks={'same_source':cfg.get('source_fingerprint')==source_fingerprint(),
            'all_22_cases':len(rows)==22 and len({r['id'] for r in rows})==22 and {r['id'] for r in rows}==set(requested)==set(cfg.get('case_ids',[])),
            'no_execution_errors':all(r['status'] not in {'error','cancelled'} for r in rows),
            'judge_calibration':read(preflight/'calibration/calibration_report.json',{}).get('passed',False),
            'same_judge_code':cal.get('code_sha256')==digest(Path(semantic.__file__).read_bytes()) and cal.get('rubric_version')=='v41'}
    if not all(checks.values()):raise RuntimeError('Preflight not ready: '+str(checks))
    return {'passed':True,'checks':checks,'path':str(preflight),'predictions_sha256':digest((run/'predictions.jsonl').read_bytes()),
            'config_sha256':digest((run/'config.json').read_bytes()),'not_semantic_accuracy_claim':True}


def main():
    base=Settings().root
    p=argparse.ArgumentParser();p.add_argument('action',choices=['launch','work','status','stop','report'])
    p.add_argument('--output',type=Path,default=base/'artifacts/git_support_v1/dev_v41_20261028')
    p.add_argument('--preflight',type=Path,default=base/'artifacts/git_support_v1/v41_preflight_20261028')
    a=p.parse_args();root=a.output.resolve();root.mkdir(parents=True,exist_ok=True)
    owner=read(root/'queue.json',{});alive=bool(owner) and process_identity(owner['pid'])==owner['start_ticks'];plan=stages(root,base)
    if a.action=='status':
        s=read(root/'queue_state.json',{});status=s.get('status','pending')
        if status=='running' and not alive:status='interrupted'
        print(f"总状态: {status} | 阶段: {s.get('stage')} | PID: {owner.get('pid')} | 存活: {alive}")
        for name,_,target in plan:
            t=read(target/('report.json' if name=='browser' else 'status.json'),{})
            print(f"{name:44} {t.get('status','pending'):26} {t.get('completed',0)}/{t.get('total','-')} 评分缺失={t.get('judge_errors',0)} {t.get('current') or ''}")
        for target in sorted((root/'test').glob('*')) if (root/'test').exists() else []:
            t=read(target/'status.json',{});g=read(root/'semantic'/('test_'+target.name)/'status.json',{})
            print(f"test_{target.name}: {t.get('status')} {t.get('completed',0)}/300; grade: {g.get('status','pending')} {g.get('completed',0)}/300")
        print('错误:',s.get('error'));print('报告:',root/'report.md');print('队列日志:',root/'queue.log')
        print('复用回答GPU1、评分GPU0；重排用CPU；无训练。不修改在线Web。');return
    if a.action=='stop':
        if alive:
            cmd=Path(f"/proc/{owner['pid']}/cmdline").read_bytes()
            if b'eval_v41.py' not in cmd or b'work' not in cmd:raise RuntimeError('Queue identity mismatch')
            os.killpg(owner['pid'],signal.SIGTERM)
        print('本队列已停止；模型服务保留。');return
    if a.action=='launch':
        if not read(a.preflight/'calibration/calibration_report.json',{}).get('passed'):
            raise RuntimeError('Calibration preflight has not passed')
        with (root/'launch.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            owner=read(root/'queue.json',{})
            if owner and process_identity(owner['pid'])==owner['start_ticks']:raise RuntimeError('队列已在运行')
            with (root/'queue.log').open('a') as log:
                child=subprocess.Popen([sys.executable,'-B',__file__,'work','--output',str(root),'--preflight',str(a.preflight)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            write_json(root/'queue.json',{'pid':child.pid,'start_ticks':process_identity(child.pid),'started_at':time.time()})
            print(json.dumps({'pid':child.pid,'output':str(root)},ensure_ascii=False));return
    if a.action=='report':
        if alive:raise RuntimeError('Queue running; no concurrent report writes')
        report(root,base);return
    with (root/'suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);stage=None
        def progress(status,error=None):write_json(root/'queue_state.json',{'status':status,'stage':stage,'error':error,'updated_at':time.time()})
        def interrupted(sig,frame):raise SystemExit('Signal '+str(sig))
        signal.signal(signal.SIGTERM,interrupted)
        project=Path(__file__).resolve().parent.parent
        codefiles={str(f.relative_to(project)):digest(f.read_bytes()) for folder in ['kb','scripts'] for f in sorted((project/folder).rglob('*.py'))}
        protocol={'source_fingerprint':source_fingerprint(),'source_files':codefiles,'gate':GATE,'rubric':'v41',
          'dataset':verify_freeze(base/'datasets/git_support_zh_v1'),
          'gap_dataset':verify_freeze(base/'artifacts/git_support_v1/dev_v4_20261028/gap_dataset'),
          'plan':[n for n,_,_ in plan], 'request_timeout_seconds':Settings().timeout,
          'preflight_inputs':{name:digest((a.preflight/name).read_bytes()) for name in ['regression_ids.json','calibration/config.json','calibration/calibration_report.json']}}
        def execute(name,command):
            if any(digest((project/f).read_bytes())!=sha for f,sha in codefiles.items()):raise RuntimeError('Code changed; use new output directory')
            with (root/(name+'.log')).open('a') as log:child=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
            if child.returncode:raise RuntimeError(f'{name} exited {child.returncode}; inspect {root/(name+".log")}')
        try:
            if (root/'protocol.json').exists() and read(root/'protocol.json')!=protocol:raise RuntimeError('Protocol changed; use new output directory')
            write_json(root/'protocol.json',protocol)
            if not (root/'source').exists():
                for folder in ['kb','scripts','tests']:shutil.copytree(project/folder,root/'source'/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            if not (root/'preflight').exists():
                (root/'preflight').mkdir()
                for name in ['failure_audit.json','http_replay.json','assistant_review.jsonl','regression_ids.json']:
                    shutil.copyfile(a.preflight/name,root/'preflight'/name)
            if not (root/'calibration').exists():shutil.copytree(a.preflight/'calibration',root/'calibration')
            if (a.preflight/'audit_regrade/status.json').exists() and not (root/'audit_regrade').exists():
                if read(a.preflight/'audit_regrade/status.json',{}).get('status')=='complete':
                    shutil.copytree(a.preflight/'audit_regrade',root/'audit_regrade')
            for name,cmd,target in plan:
                stage=name;progress('running');execute(name,cmd)
                if name=='regression':write_json(root/'preflight.json',check_preflight(a.preflight,target))
                if name=='calibration' and not read(target/'calibration_report.json',{}).get('passed'):
                    raise RuntimeError('Judge calibration failed; no dev release')
            stage='report';progress('running');decision=report(root,base)
            if decision['eligible']:
                release={'protocol':protocol,'decision':decision,'answer_service':read(base/'model_service.json'),
                    'judge_service':read(base/'judge_service.json'),'case_count':300,'created_at':time.time()}
                if not (root/'test_release.json').exists():write_json(root/'test_release.json',release)
                elif read(root/'test_release.json')['protocol']!=protocol:raise RuntimeError('Test protocol changed')
                baseline=decision['strongest_fixed_baseline']
                for arm in [baseline,CANDIDATE]:
                    stage='test_'+arm;progress('running');target=root/'test'/arm
                    execute(stage,[sys.executable,'-B','-m','kb.benchmark','run','--split','test','--ack-frozen-test','--arm',arm,'--output',str(target)])
                    stage='grade_test_'+arm;progress('running')
                    execute(stage,[sys.executable,'-B','scripts/semantic_v4.py','grade','--rubric','v41','--run-dir',str(target),'--output',str(root/'semantic'/('test_'+arm))])
                left=scored(root,'test_'+baseline);right=scored(root,'test_'+CANDIDATE)
                test={**compare_rows(left,right),'baseline':baseline,'candidate':CANDIDATE,'baseline_correct':correct(left),
                    'candidate_correct':correct(right),'expected':300,'human_reviewed':False}
                write_json(root/'test_comparison.json',test)
                report(root,base)
            final_status='complete' if decision['eligible'] else 'complete_dev_gate_not_met'
            if decision['eligible'] and not read(root/'test_comparison.json',{}).get('full_judge_coverage'):
                final_status='complete_with_test_judge_errors'
            stage=None;progress(final_status)
        except BaseException as exc:
            progress('interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed',str(exc));raise


if __name__=='__main__':main()
