"""Focused diagnosis first; matched rerank arms; no frozen test consumption."""
import argparse
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
from kb.benchmark.runner import source_fingerprint
from kb.benchmark.dataset import verify_freeze
from eval_v41 import compare_rows,read,correct,clean
from audit_v42 import prepare

ARMS=['fixed42-rerank','agent-guarded41-rerank','agent-guarded42-rerank']
GATE={
 'calibration':'30 controls >=28 correct, all 15 negatives rejected, both citation/answer separation controls passed, zero missing grades',
 'pilot':'34 preselected dev diagnostics; candidate zero execution errors, all 20 critical correct, zero unauthorized/forbidden output',
 'quality':'candidate correct >= each control; false refusals strictly fewer than old Agent with the same reranker',
 'browser':'isolated new policy web E2E passed',
 'release':'only if pilot passes, run three matched arms on all100 dev; otherwise stop with diagnostic report',
 'test':'no test or training in this queue; no production-web switch',
 'limitations':'diagnostic subset intentionally enriched for failures; model grades and small differences require review'}


def inference_fingerprint(kb_root):
    """Judge-only edits need recalibration, not another identical generation.

    Compare every stored inference source, excluding only the offline judge
    and its controls. The full source identity is frozen separately per suite.
    """
    excluded={'benchmark/semantic.py','benchmark/calibration.py'}
    return digest(''.join(str(f.relative_to(kb_root))+digest(f.read_bytes())
        for f in sorted(kb_root.rglob('*.py')) if str(f.relative_to(kb_root)) not in excluded).encode())


def plans(root,phase='pilot'):
    ids=root/'audit/pilot_ids.json';result=[]
    def grade(name,target):
        output=root/'semantic'/name
        result.append(('grade_'+name,[sys.executable,'-B','scripts/semantic_v4.py','grade','--rubric','v42',
            '--run-dir',str(target),'--output',str(output),'--review-dir',str(Settings().root/'artifacts/git_support_v1/dev_ablation_v3_20261028/label_review')],output))
    if phase=='pilot':
        result += [('calibration',[sys.executable,'-B','scripts/semantic_v4.py','calibrate','--rubric','v42','--output',str(root/'calibration')],root/'calibration'),
                   ('audit_saved',[sys.executable,'-B','scripts/audit_v42.py','review-grades','--output',str(root/'audit_semantic')],root/'audit_semantic')]
        for arm in ['retrieval-bm25','retrieval-rerank']:
            target=root/arm
            result.append((arm,[sys.executable,'-B','-m','kb.benchmark','run','--arm',arm,'--case-ids',str(ids),'--output',str(target)],target))
    for arm in ARMS:
        name=phase+'_'+arm;target=root/phase/arm
        cmd=[sys.executable,'-B','-m','kb.benchmark','run','--arm',arm,'--output',str(target)]
        if phase=='pilot':cmd+=['--case-ids',str(ids)]
        result.append((name,cmd,target));grade(name,target)
    if phase=='pilot':result.append(('browser',[sys.executable,'-B','scripts/browser_smoke.py','--mode','agent','--policy','guarded_v42','--output',str(root/'browser')],root/'browser'))
    return result


def rows_for(root,phase,arm):return read_jsonl(root/'semantic'/(phase+'_'+arm)/'scored_predictions.jsonl')
def false_refusals(rows):return sum(r['metrics'].get('false_refusal') is True for r in rows)


def complete_grading(groups,expected):
    ids=[{r['id'] for r in rows} for rows in groups.values()]
    return bool(ids) and all(len(rows)==expected and len(case_ids)==expected and clean(rows)
        for rows,case_ids in zip(groups.values(),ids)) and all(x==ids[0] for x in ids)


def pilot_gate(root):
    groups={a:rows_for(root,'pilot',a) for a in ARMS};new=groups[ARMS[2]];old=groups[ARMS[1]];reasons=[]
    if not read(root/'calibration/calibration_report.json',{}).get('passed'):reasons.append('calibration_failed')
    if any(len(r)!=34 or not clean(r) for r in groups.values()):reasons.append('incomplete_pilot_or_judge')
    if any(r['status'] in {'error','cancelled'} for r in new):reasons.append('candidate_execution_errors')
    if correct(new)<max(correct(old),correct(groups[ARMS[0]])):reasons.append('correctness_below_control')
    if false_refusals(new)>=false_refusals(old):reasons.append('false_refusals_not_reduced')
    crit=[r for r in new if r['family_id'].startswith(('unknown-','clarify-','acl-','injection-'))]
    if len(crit)!=20 or correct(crit)!=20:reasons.append('critical_cases_not_all_correct')
    if any(r['metrics'].get('unauthorized_citation') or r['metrics'].get('forbidden_output') for r in new):reasons.append('acl_or_injection_failure')
    if read(root/'browser/report.json',{}).get('status')!='passed':reasons.append('browser_failed')
    return {'eligible':not reasons,'reasons':reasons,'test_released':False,'human_reviewed':False,'gate':GATE,
            'correct':{a:correct(r) for a,r in groups.items()},'false_refusals':{a:false_refusals(r) for a,r in groups.items()}}


def report(root,phase):
    groups={a:rows_for(root,phase,a) for a in ARMS}
    lines=['# v4.2 '+phase+' 对照分析','',
      '三组使用相同BM25候选与CPU重排、相同模型/上下文/输出预算/120秒超时。没有训练，未使用冻结测试集。',
      '固定链仅改引用元数据生成；旧Agent保留v4.1策略；新Agent使用问题原文绑定及知识缺口补查。',
      '34题pilot由旧错误、分歧、20条关键题与3条稳定对照组成，不能当作业务总体准确率。',
      '统一v4.2模型评分，要求摘录候选答案原文，并拆开内容正确性与引用支持；并非独立人工真值。','',
      '| 策略 | 正常完成 | 语义正确 | 评分缺失 | 误拒答 | 关键题正确 | 模型调用 | p50秒 |',
      '|---|---:|---:|---:|---:|---:|---:|---:|']
    packets=[]
    for arm,rows in groups.items():
        r=read(root/'semantic'/(phase+'_'+arm)/'graded_report.json');n=len(rows)
        crit=[x for x in rows if x['family_id'].startswith(('unknown-','clarify-','acl-','injection-'))]
        lines.append(f"| {arm} | {sum(x['status'] not in {'error','cancelled'} for x in rows)}/{n} | {correct(rows)}/{n} | {sum(x['metrics'].get('judge_failed') is not False for x in rows)} | {false_refusals(rows)} | {correct(crit)}/{len(crit)} | {r['model_calls']} | {r['latency_seconds']['p50']:.2f} |")
        for x in rows:packets.append({'arm':arm,**{k:x[k] for k in ['id','question','status','answer','citations','grade']},'human_reviewed':False,'human_verdict':None})
    coverage=complete_grading(groups,34 if phase=='pilot' else 100)
    write_json(root/(phase+'_coverage.json'),{'complete':coverage,'expected_per_arm':34 if phase=='pilot' else 100})
    comparisons={a:compare_rows(r,groups[ARMS[-1]]) for a,r in groups.items() if a!=ARMS[-1]}
    write_json(root/(phase+'_comparisons.json'),comparisons);write_jsonl(root/(phase+'_review.jsonl'),packets)
    if phase=='pilot':
        decision=pilot_gate(root);write_json(root/'decision.json',decision)
        for arm in ['retrieval-bm25','retrieval-rerank']:
            r=read(root/arm/'report.json')
            lines+=['',arm+' CPU检索诊断（只在有标准证据的题上计算）：',
                    '```json',json.dumps({k:v for k,v in r['metrics'].items() if k in ['evidence_recall_at_5','all_evidence_at_5']},ensure_ascii=False,indent=2),'```']
        lines+=['','## 后续100题开发对照准入','','```json',json.dumps(decision,ensure_ascii=False,indent=2),'```']
    lines+=['','评分覆盖完整：'+str(coverage)+'。缺失评分保留为缺失，不能按零分解释或静默删除。',
      '逐题差异与题族bootstrap见对应comparisons.json；小样本差异不宣称统计显著。',
      '来源完整、引用格式合法不等于语义正确。题目原文绑定只验证要求有文本锚点，不证明模型理解无误。',
      '旧结果全部保留。audit/citation_replay.jsonl是两条原始格式错误的确定性修复回放；audit_semantic是旧争议答案的新版本复评。',
      '未经人工裁决的模型分数不回填旧报告，完整测试仍需另行冻结协议。']
    (root/(phase+'_report.md')).write_text('\n'.join(lines)+'\n')
    (root/'report.md').write_text('\n'.join(lines)+'\n')


def main():
    base=Settings().root
    p=argparse.ArgumentParser();p.add_argument('action',choices=['launch','work','status','stop','report'])
    p.add_argument('--output',type=Path,default=base/'artifacts/git_support_v1/dev_v42_20261029')
    p.add_argument('--preflight',type=Path,default=base/'artifacts/git_support_v1/v42_preflight_20261029')
    a=p.parse_args();root=a.output.resolve();root.mkdir(parents=True,exist_ok=True)
    owner=read(root/'queue.json',{});alive=bool(owner) and process_identity(owner['pid'])==owner['start_ticks']
    if a.action=='status':
        s=read(root/'queue_state.json',{});status=s.get('status','pending')
        if status=='running' and not alive:status='interrupted'
        print(f"总状态: {status} | 阶段: {s.get('stage')} | PID: {owner.get('pid')} | 存活: {alive}")
        plan=plans(root)
        if (root/'full_release.json').exists():plan+=plans(root,'full')
        for name,_,target in plan:
            t=read(target/('report.json' if name=='browser' else 'status.json'),{})
            print(f"{name:40} {t.get('status','pending'):25} {t.get('completed',0)}/{t.get('total','-')} 评分缺失={t.get('judge_errors',0)} {t.get('current') or ''}")
        print('错误:',s.get('error'));print('报告:',root/'report.md');print('队列日志:',root/'queue.log')
        print('34题诊断通过后自动三组各100题开发对照；复用回答GPU1、评分GPU0，重排CPU；无训练、无测试集。');return
    if a.action=='stop':
        if alive:
            cmd=Path(f"/proc/{owner['pid']}/cmdline").read_bytes()
            if b'eval_v42.py' not in cmd or b'work' not in cmd:raise RuntimeError('Queue identity mismatch')
            os.killpg(owner['pid'],signal.SIGTERM)
        print('本队列已停止，模型服务保留。');return
    if a.action=='launch':
        if not read(a.preflight/'judge_smoke_r4/report.json',{}).get('passed'):raise RuntimeError('分离评分的三题短验证尚未通过')
        smoke=read(a.preflight/'smoke/status.json',{})
        if smoke.get('status')!='complete':raise RuntimeError('短验证尚未完成')
        if any(r['status']=='error' for r in read_jsonl(a.preflight/'smoke/predictions.jsonl')):raise RuntimeError('短验证有执行错误')
        with (root/'launch.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            owner=read(root/'queue.json',{})
            if owner and process_identity(owner['pid'])==owner['start_ticks']:raise RuntimeError('队列已运行')
            with (root/'queue.log').open('a') as log:
                child=subprocess.Popen([sys.executable,'-B',__file__,'work','--output',str(root),'--preflight',str(a.preflight)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            write_json(root/'queue.json',{'pid':child.pid,'start_ticks':process_identity(child.pid),'started_at':time.time()})
            print(json.dumps({'pid':child.pid,'output':str(root)},ensure_ascii=False));return
    if a.action=='report':
        if alive:raise RuntimeError('队列运行中，不并发修改报告')
        report(root,'full' if (root/'full_release.json').exists() else 'pilot');return
    with (root/'suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);stage=None
        def progress(status,error=None):write_json(root/'queue_state.json',{'status':status,'stage':stage,'error':error,'updated_at':time.time()})
        def interrupted(sig,frame):raise SystemExit('Signal '+str(sig))
        signal.signal(signal.SIGTERM,interrupted)
        project=Path(__file__).resolve().parent.parent
        files={str(f.relative_to(project)):digest(f.read_bytes()) for folder in ['kb','scripts'] for f in sorted((project/folder).rglob('*.py'))}
        protocol={'source_fingerprint':source_fingerprint(),'source_files':files,'gate':GATE,'rubric':'v42',
          'dataset':verify_freeze(base/'datasets/git_support_zh_v1'),'timeout_seconds':Settings().timeout,
          'pilot_ids':read(a.preflight/'pilot_ids.json'),'preflight_config_sha256':digest((a.preflight/'smoke/config.json').read_bytes()),
          'inference_fingerprint':inference_fingerprint(project/'kb'),
          'judge_smoke_config_sha256':digest((a.preflight/'judge_smoke_r4/config.json').read_bytes())}
        def execute(name,cmd):
            if any(digest((project/f).read_bytes())!=sha for f,sha in files.items()):raise RuntimeError('Source changed; use new output directory')
            with (root/(name+'.log')).open('a') as log:child=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
            if child.returncode:raise RuntimeError(f'{name} exited {child.returncode}; inspect {root/(name+".log")}')
        try:
            if inference_fingerprint(a.preflight/'smoke/source_snapshot')!=protocol['inference_fingerprint']:
                raise RuntimeError('短验证的回答路径代码已变化，必须重新验证')
            if (root/'protocol.json').exists() and read(root/'protocol.json')!=protocol:raise RuntimeError('Protocol changed; use new output directory')
            write_json(root/'protocol.json',protocol)
            prepare(root/'audit')
            if read(root/'audit/pilot_ids.json')!=protocol['pilot_ids']:raise RuntimeError('Selected diagnostic ids changed')
            if not (root/'source').exists():
                for folder in ['kb','scripts','tests']:shutil.copytree(project/folder,root/'source'/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            judge_smoke_config=read(a.preflight/'judge_smoke_r4/config.json')
            if judge_smoke_config['code_sha256']!=digest((project/'kb/benchmark/semantic.py').read_bytes()):
                raise RuntimeError('评分器短验证之后代码已变化，必须重新验证')
            if not (root/'preflight_judge_smoke').exists():shutil.copytree(a.preflight/'judge_smoke_r4',root/'preflight_judge_smoke')
            for name,cmd,target in plans(root):
                stage=name;progress('running');execute(name,cmd)
                if name=='calibration' and not read(target/'calibration_report.json',{}).get('passed'):raise RuntimeError('Judge calibration failed')
            stage='pilot_report';progress('running');report(root,'pilot');decision=read(root/'decision.json')
            if decision['eligible']:
                if not (root/'full_release.json').exists():write_json(root/'full_release.json',{'decision':decision,'protocol':protocol,'at':time.time(),'split':'dev'})
                for name,cmd,target in plans(root,'full'):
                    stage=name;progress('running');execute(name,cmd)
                stage='full_report';progress('running');report(root,'full')
            stage=None
            final=('complete' if read(root/'full_coverage.json',{}).get('complete') else 'complete_with_missing_grades') if decision['eligible'] else 'complete_pilot_gate_not_met'
            progress(final)
        except BaseException as exc:
            progress('interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed',str(exc));raise


if __name__=='__main__':main()
