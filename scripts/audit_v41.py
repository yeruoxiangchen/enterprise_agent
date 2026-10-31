"""Read-only v4 audit and bounded replay; original runs are never edited."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from kb.config import Settings
from kb.context import ContextBudget
from kb.agent import ModelClient
from kb.model_errors import ModelServiceError
from kb.benchmark.sources import read_jsonl,write_json,write_jsonl,digest


def prepare(base,output):
    old=base/'artifacts/git_support_v1/dev_v4_20261028'
    baseline={r['id']:r for r in read_jsonl(old/'semantic/v4_fixed-bm25/scored_predictions.jsonl')}
    packets=[];ids=set();failures=[];inputs={}
    for arm in ['agent-guarded-bm25','agent-guarded-rerank']:
        p=old/'semantic'/('v4_'+arm)/'scored_predictions.jsonl'
        inputs[str(p)]=digest(p.read_bytes())
        for row in read_jsonl(p):
            b=baseline[row['id']]
            if row['status']=='error':
                failures.append({'arm':arm,'id':row['id'],'error':row['error'],
                  'validation_errors':[e['result'] for e in row['trace'] if e.get('name')=='validation']})
                ids.add(row['id'])
            if b['metrics']['semantic_correctness']!=row['metrics']['semantic_correctness'] or row['status']=='error':
                packets.append({'id':row['id'],'arm':arm,'question':row['question'],
                    'answer':row['answer'],'citations':row['citations'],'grade':row['grade'],
                    'baseline_answer':b['answer'],'baseline_grade':b['grade'],'status':row['status']})
    # Add unchanged easy positives and critical rejection/clarification controls.
    ids.update(['aliases__1-1','first-time-setup__2-1','clarify-1-1','clarify-1-3','acl-1-1','injection-dev-1'])
    grade_jobs={j['id']:j for j in read_jsonl(old/'dev/agent-guarded-rerank/grading.jsonl')}
    for row in packets:
        j=grade_jobs[row['id']];row['reference_answer']=j['reference_answer'];row['gold_evidence']=j['gold_evidence']
        row['assistant_review']=None;row['independent_human_review']=False
    missing=[g for g in read_jsonl(old/'semantic/v3_agent-bm25/grades.jsonl') if g['judgment_status']=='error']
    write_json(output/'regression_ids.json',sorted(ids))
    write_jsonl(output/'disagreements.jsonl',packets)
    write_json(output/'failure_audit.json',{'failures':failures,'failed_rows':len(failures),
        'unique_failed_ids':len({r['id'] for r in failures}),'grading_errors':missing,'originals_sha256':inputs})
    return {'regression_cases':len(ids),'review_rows':len(packets),'failed_rows':len(failures)}


def replay(base,output):
    from kb.guarded import prepare as old_prepare,ASSESS,SUBMIT
    old=base/'artifacts/git_support_v1/dev_v4_20261028/dev/agent-guarded-bm25'
    row=next(r for r in read_jsonl(old/'predictions.jsonl') if r['id']=='credentials__2-3')
    conn=sqlite3.connect('file:'+str(old/'workspace/knowledge.sqlite3')+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    run=dict(conn.execute('SELECT * FROM runs WHERE question=? ORDER BY created_at DESC LIMIT 1',(row['question'],)).fetchone())
    conn.close();state=json.loads(run['state'])
    cfg=replace(Settings(),root=output/'replay_workspace',agent_policy='guarded_v4',timeout=120)
    tools=[ASSESS] if state['phase']=='assess' else [SUBMIT]
    messages,budget=old_prepare(SimpleNamespace(budget=ContextBudget(cfg),settings=cfg),run,state,tools)
    result={'original_case_id':row['id'],'reconstructed_from_checkpoint':True,'gold_used':False,'budget':budget,
            'request_sha256':digest(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False,sort_keys=True).encode()),
            'tool_choice':'required','output_tokens':cfg.output_tokens}
    try:
        message,usage=ModelClient(cfg).complete(messages,tools)
        result.update(status='returned',response=message,usage=usage)
    except ModelServiceError as exc:result.update(status='http_error',diagnostic=exc.diagnostic)
    except RuntimeError as exc:result.update(status='runtime_error',error=str(exc))
    write_json(output/'http_replay.json',result)
    return {k:v for k,v in result.items() if k not in {'response'}}


def review(base,output):
    """Record assistant review separately; never relabel saved model grades."""
    notes={
      'aliases__1-2':('keep_incorrect','只说明可以配置别名，没有给出所需配置命令；评分关于缺少明确声明别名方向的理由过强，但缺少操作本身成立。'),
      'tagging__2-2':('keep_incorrect','有可访问标准证据却拒答，正确性为0；纯拒答的忠实性/引用指标口径不一致，需要统一说明。'),
      'credentials__2-4':('keep_incorrect','仅把是否加密、是否到期两个问题重复了一遍，没有回答明文/永不过期。'),
      'searching__2-4':('disputed_rationale_not_auto_overturned','日志搜索命令正确，不应因未重复推荐二字判错；但仅凭列出的两个相关提交，不能确认指定ef49a7a就是引入提交。保留额外事实未经充分支持的争议，不直接翻为全对。'),
      'hooks__1-4':('disputed_rationale_not_auto_overturned','已经提到可执行权限，不必强制chmod命令；但首句即可运行过强，示例.sample条件表达不明确。须整体阅读，不自动改分。'),
      'multi-dev-config-alias-1':('keep_incorrect','可回答跨文档题误拒答；不能因为没编造而给正确性分。'),
      'multi-dev-config-alias-2':('keep_incorrect','同上；忠实性不等于任务完成。'),
    }
    rows=[]
    for row in read_jsonl(output/'disagreements.jsonl'):
        if row['arm']=='agent-guarded-rerank' and row['id'] in notes:
            verdict,note=notes[row['id']]
            rows.append({**row,'assistant_review':{'verdict':verdict,'note':note,
               'model_grade_overwritten':False,'evidence_used':'candidate answer, candidate citations, frozen gold evidence'},
               'independent_human_review':False})
    write_jsonl(output/'assistant_review.jsonl',rows)
    return {'reviewed':len(rows),'original_grades_changed':False,'independent_human_review':False}


def regrade(base,output):
    from kb.benchmark.review import jobs_for
    from kb.benchmark.semantic import evaluate_jobs
    old=base/'artifacts/git_support_v1/dev_v4_20261028'
    prior=base/'artifacts/git_support_v1/dev_ablation_v3_20261028'
    selected={'aliases__1-2','tagging__2-2','credentials__2-4','searching__2-4','hooks__1-4',
              'multi-dev-config-alias-1','multi-dev-config-alias-2'}
    jobs=[]
    for run,ids,prefix in [(old/'dev/agent-guarded-rerank',selected,'v4_'),
        (prior/'agent-bm25',{g['id'] for g in read_jsonl(old/'semantic/v3_agent-bm25/grades.jsonl') if g['judgment_status']=='error'},'v3_')]:
        rows={r['id']:r for r in read_jsonl(run/'predictions.jsonl')}
        for j in jobs_for(run,prior/'label_review'):
            if j['id'] in ids:
                j['prediction_status']=rows[j['id']]['status'];j['original_id']=j['id'];j['id']=prefix+j['id'];jobs.append(j)
    write_jsonl(output/'jobs.jsonl',jobs)
    grades=evaluate_jobs(jobs,output,base,{'purpose':'saved disputed answers and missing v3 judgment; not fresh inference'},'v41')
    return {'graded':len(grades),'judge_errors':sum(g['judgment_status']=='error' for g in grades),'old_results_changed':False}


def retry_slow(base,output):
    from kb.benchmark.runner import run
    r=run(base/'datasets/git_support_zh_v1',replace(Settings(),timeout=120),output,
          'agent-guarded41-bm25',case_ids=['multi-dev-ssh-store-3'])
    return {'records':r['records'],'complete':r['complete'],'output':str(output)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','replay-http','review','regrade','retry-slow'])
    p.add_argument('--output',type=Path,default=Settings().root/'artifacts/git_support_v1/v41_preflight_20261028')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    print(json.dumps({'prepare':prepare,'replay-http':replay,'review':review,'regrade':regrade,'retry-slow':retry_slow}[a.action](Settings().root,a.output),ensure_ascii=False,indent=2))
