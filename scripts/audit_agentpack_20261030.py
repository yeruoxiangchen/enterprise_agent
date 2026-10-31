"""Offline evidence audit; no test data, model calls, or edits to frozen runs."""
import collections
import json
from pathlib import Path
from kb.benchmark.sources import read_jsonl, write_json, write_jsonl, digest
from kb.benchmark.semantic import validate, validate_answer_quotes
from kb.benchmark.review import jobs_for

ARMS = ('fixed-rerank', 'official-agentpack')


def independent_axes(row, job):
    """Keep validated partial judgments; never promote missing grounding to pass."""
    g=row['grade'];parts=g.get('partial_grades',{})
    values={k:g.get(k) for k in ('correctness','faithfulness','citation_support')}
    origins={k:'original' if v is not None else 'missing' for k,v in values.items()}
    c=parts.get('content')
    if values['correctness'] is None and c is not None:
        check={**c,'faithfulness':1,'citation_support':1,'claims':[]}
        validate(check,job);validate_answer_quotes(check,job)
        values['correctness']=c['correctness'];origins['correctness']='validated_saved_content'
    gnd=parts.get('grounding')
    if gnd is not None:
        validate({**gnd,'correctness':0},job)
        for k in ('faithfulness','citation_support'):
            if values[k] is None:values[k]=gnd[k];origins[k]='validated_saved_grounding'
    if values['correctness'] is None and job['expected_action']=='answered' and row['status'] in {'clarify','insufficient'}:
        values['correctness']=0;origins['correctness']='existing_rubric_false_refusal_rule'
    return {'values':values,'origins':origins}


def audit(root, output, review):
    output.mkdir(parents=True,exist_ok=True)
    arms={};missing=[];cases=[];receipts={}
    for arm in ARMS:
        run=root/arm
        jobs={j['id']:j for j in jobs_for(run,review)}
        rows=read_jsonl(root/'semantic'/arm/'scored_predictions.jsonl')
        if len(rows)!=len(jobs) or {r['id'] for r in rows}!=set(jobs):raise ValueError('Mismatched audit IDs')
        receipts[arm]=digest((root/'semantic'/arm/'scored_predictions.jsonl').read_bytes())
        arms[arm]={}
        for r in rows:
            job={**jobs[r['id']],'prediction_status':r['status']}
            axes=independent_axes(r,job);arms[arm][r['id']]={**r,'axes':axes}
            if r['grade'].get('judgment_status')=='error':
                missing.append({'arm':arm,'id':r['id'],'question':r['question'],'answer':r['answer'],
                    'reference':job['reference_answer'],'rubric':job.get('semantic_rubric'),
                    'axes':axes,'original_grade':r['grade'],'independent_human_review':False})
    for ident,a in arms['official-agentpack'].items():
        b=arms['fixed-rerank'][ident];searches=[t for t in a['trace'] if t['kind']=='retrieval']
        filtered=[s for s in searches if s.get('filters')]
        empty=[s for s in filtered if not s.get('documents')]
        tags=list(a['warnings'])
        if not a['hits'] and empty:tags.append('no_evidence_after_filtered_search')
        if a.get('backup_calls'):tags.append('step_limit_backup')
        if '<tool_call>' in (a['raw_answer'] or ''):tags.append('tool_call_leaked_as_text')
        if a['hits'] and a['axes']['values']['correctness']==0:tags.append('wrong_despite_retrieved_text')
        av=a['axes']['values']['correctness'];bv=b['axes']['values']['correctness']
        pair='unknown' if av is None or bv is None else ('improved' if av>bv else 'regressed' if av<bv else 'same')
        cases.append({'id':ident,'question':a['question'],'tags':tags,'pair':pair,
            'fixed_correctness':bv,'agent_correctness':av,'fixed_answer':b['answer'],'agent_answer':a['answer'],
            'filtered_searches':len(filtered),'empty_filtered_searches':len(empty),
            'queries':[{'query':s['query'],'filters':s.get('filters'),'returned':len(s['documents'])} for s in searches],
            'agent_retrieval':a['retrieval_metrics'],'fixed_retrieval':b['retrieval_metrics']})
    stats={arm:{k:{'correct':sum(r['axes']['values'][k]==1 for r in rows.values()),
                       'incorrect':sum(r['axes']['values'][k]==0 for r in rows.values()),
                       'unknown':sum(r['axes']['values'][k] is None for r in rows.values())}
                for k in ('correctness','faithfulness','citation_support')} for arm,rows in arms.items()}
    result={'protocol':'per_axis_audit_v1','source':str(root),'source_hashes':receipts,'arms':stats,
        'pairs':dict(collections.Counter(c['pair'] for c in cases)),
        'failures':dict(collections.Counter(t for c in cases for t in c['tags'])),
        'filtered_searches':sum(c['filtered_searches'] for c in cases),
        'empty_filtered_searches':sum(c['empty_filtered_searches'] for c in cases),
        'independent_human_review':False,'test_used':False,
        'review_flags':['fixed-rerank/hooks__2-2: saved content rationale treats a non-required point as failure; requires independent adjudication.']}
    write_json(output/'summary.json',result);write_jsonl(output/'cases.jsonl',cases);write_jsonl(output/'missing_review.jsonl',missing)
    lines=['# Agent Pack 逐题归因与独立评分轴复核','',
        '原始结果只读；按已通过验证的内容/引用评分分别恢复。这里是助手审计，不是独立人工标注；不把缺失引用评分算正确。',
        '固定RAG hooks__2-2 的自动评分理由自相矛盾，另列复核标记；以下仍保留原模型值，不能当人工最终结论。','',
        '|组别|内容对/错/未知|忠实性对/错/未知|','|---|---|---|']
    for arm,s in stats.items():
        c=s['correctness'];g=s['faithfulness'];lines.append(f"|{arm}|{c['correct']}/{c['incorrect']}/{c['unknown']}|{g['correct']}/{g['incorrect']}/{g['unknown']}|")
    lines+=['',f"配对结果：{result['pairs']}。",f"过滤检索 {result['filtered_searches']} 次，其中空结果 {result['empty_filtered_searches']} 次。",f"失败标签：{result['failures']}。",'',
        '|题目|配对变化|失败标签|','|---|---|---|']
    for c in cases:lines.append(f"|{c['id']}|{c['pair']}|{', '.join(c['tags']) or '—'}|")
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    return result


if __name__=='__main__':
    import argparse
    from kb.config import Settings
    p=argparse.ArgumentParser();base=Settings().root/'artifacts/git_support_v1'
    p.add_argument('--source',type=Path,default=base/'dev_agentpack_diagnostic_20261030')
    p.add_argument('--output',type=Path,default=base/'agentpack_audit_20261030')
    a=p.parse_args()
    if not a.output.resolve().is_relative_to(Settings().root.resolve()/'artifacts'):raise ValueError('Use /data artifacts')
    print(json.dumps(audit(a.source,a.output,base/'dev_ablation_v3_20261028/label_review'),ensure_ascii=False,indent=2))
