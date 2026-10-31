"""Offline cause receipts and dev-only case selection. No gold in inference."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
from kb.config import Settings
from kb.benchmark.sources import read_jsonl,write_json,write_jsonl,digest
from kb.benchmark.fixed_v42 import canonical_citations


def prepare(output):
    base=Settings().root;old=base/'artifacts/git_support_v1/dev_v41_20261028'
    agent=read_jsonl(old/'semantic/agent-guarded41-bm25/scored_predictions.jsonl')
    fixed=read_jsonl(old/'dev/fixed-rerank/predictions.jsonl')
    pairs=json.loads((old/'comparisons.json').read_text())['fixed-rerank']
    ids=set(pairs['wins']+pairs['losses'])
    ids.update(r['id'] for r in agent if r['metrics']['semantic_correctness']!=1 or r['family_id'].startswith(('unknown-','clarify-','acl-','injection-')))
    ids.update(['aliases__1-1','tagging__2-1','first-time-setup__2-1'])
    receipts=[]
    for r in fixed:
        if r['status']!='error':continue
        events=[e for e in r['trace'] if e.get('kind')=='model']
        args=json.loads(events[-1]['response']['tool_calls'][0]['function']['arguments'])
        sources={e['result']['citation']:e['result'] for e in r['trace'] if e.get('name')=='read_chunk' and 'citation' in e.get('result',{})}
        refs=canonical_citations(args['answer'],args['citations'],sources)
        receipts.append({'id':r['id'],'original_error':r['error'],'answer':args['answer'],
            'original_citations':args['citations'],'canonical_citations':refs,'answer_changed':False,
            'membership_checked':all(x in sources for x in refs),'semantic_correctness_not_inferred':True})
        ids.add(r['id'])
    output.mkdir(parents=True,exist_ok=True)
    write_json(output/'pilot_ids.json',sorted(ids))
    write_json(output/'smoke_ids.json',['tagging__2-2','multi-dev-ssh-store-1','unknown-1-1','clarify-1-1'])
    write_jsonl(output/'citation_replay.jsonl',receipts)
    write_json(output/'receipt.json',{'pilot_count':len(ids),'citation_cases':len(receipts),
      'frozen_test_used':False,'origin':str(old),'agent_predictions_sha256':digest((old/'dev/agent-guarded41-bm25/predictions.jsonl').read_bytes()),
      'selection':'dev failures, paired disagreements, all20 critical cases,3 unchanged positives; diagnostic not representative accuracy'})
    return {'pilot_count':len(ids),'citation_replay':receipts}


def review_grades(output):
    from kb.benchmark.review import jobs_for
    from kb.benchmark.semantic import evaluate_jobs
    base=Settings().root;old=base/'artifacts/git_support_v1/dev_v41_20261028'
    cases=[('fixed-rerank','hooks__2-2'),('fixed-bm25','first-time-setup__2-3'),('agent-guarded41-bm25','multi-dev-ssh-store-3')]
    jobs=[]
    for arm,cid in cases:
        run=old/'dev'/arm;rows={r['id']:r for r in read_jsonl(run/'predictions.jsonl')}
        j=next(j for j in jobs_for(run,base/'artifacts/git_support_v1/dev_ablation_v3_20261028/label_review') if j['id']==cid)
        j['prediction_status']=rows[cid]['status'];j['id']=arm+'__'+cid;jobs.append(j)
    output.mkdir(parents=True,exist_ok=True);write_jsonl(output/'jobs.jsonl',jobs)
    g=evaluate_jobs(jobs,output,base,{'purpose':'versioned regrade of saved disputed outputs; original grades unchanged'},'v42')
    return {'graded':len(g),'missing':sum(x['judgment_status']=='error' for x in g)}


def smoke(output):
    from kb.benchmark.runner import run
    base=Settings().root
    return run(base/'datasets/git_support_zh_v1',replace(Settings(),timeout=120),output/'smoke',
        'agent-guarded42-rerank',case_ids=json.loads((output/'smoke_ids.json').read_text()))


def judge_smoke(output):
    from kb.benchmark.calibration import cases
    from kb.benchmark.semantic import evaluate_jobs
    ids={'quote_separation-positive','quote_separation-negative','wrong_cause-negative'}
    jobs=[j for j in cases('v42') if j['id'] in ids]
    output.mkdir(parents=True,exist_ok=True)
    write_jsonl(output/'cases.jsonl',jobs)
    grades=evaluate_jobs(jobs,output,Settings().root,{'purpose':'three mandatory judge regression controls; two isolated requests per answer'},'v42')
    by_id={g['id']:g for g in grades}
    passed=all(by_id[j['id']]['judgment_status']=='ok' and by_id[j['id']]['correctness']==j['expected_correctness'] and
               ('expected_faithfulness' not in j or by_id[j['id']]['faithfulness']==j['expected_faithfulness']) for j in jobs)
    report={'passed':passed,'total':len(jobs),'independent_human_calibration':False}
    write_json(output/'report.json',report)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','smoke','review-grades','judge-smoke'])
    p.add_argument('--output',type=Path,default=Settings().root/'artifacts/git_support_v1/v42_preflight_20261029')
    a=p.parse_args();r={'prepare':prepare,'smoke':smoke,'review-grades':review_grades,'judge-smoke':judge_smoke}[a.action](a.output)
    print(json.dumps(r if a.action!='smoke' else {'records':r['records'],'complete':r['complete']},ensure_ascii=False,indent=2))
