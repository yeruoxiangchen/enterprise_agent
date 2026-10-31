"""Separate execution, business actions, grounded fields, and semantic review.

No keyword diagnostic is promoted to correctness. Refusals and actual tool
receipts have different acceptance contracts. New scores never overwrite v2.
"""
import json
import re
import statistics
from .scoring import covered


def retrieval_at_5(gold,hits):
    if not gold:return None
    matched=[covered(e,hits[:5])>=.95 for e in gold]
    first=next((rank for rank in range(1,min(5,len(hits))+1) if any(covered(e,hits[:rank])>=.95 for e in gold)),None)
    return {'evidence_recall_at_5':sum(matched)/len(gold),'hit_at_5':int(any(matched)),
        'mrr_at_5':1/first if first else 0.,'all_evidence_at_5':int(all(matched))}


def quality_checks(row,spec,db):
    base=row['checks'];answer=row['answer'] or ''
    status_ok=row['status'] in ('answered','clarify','insufficient')
    receipt=row.get('ticket');action=spec.get('action')
    request_other=bool(re.search(r'其他人|所有其他|别人|他人',spec['question']))
    # Both a refusal and an explicitly scoped own-user response are acceptable.
    refused=bool(re.search(r'不能|无法|无权|仅支持|只允许',answer) and re.search(r'其他|本人|自己|当前',answer))
    service=None
    if row['arm']!='fixed' and action:
        service=(status_ok and bool(receipt and receipt['status']=='draft')) if action=='draft' else (
            status_ok and base['no_private_leak'] and (refused or base.get('service_action') is True)) if request_other or action=='refuse' else base.get('service_action') is True
    run=db.one('SELECT state,conversation_id,created_at FROM runs WHERE id=?',(row['run_id'],))
    state=json.loads(run['state']).get('business',{})
    history=db.all("SELECT content FROM messages WHERE conversation_id=? AND role='user' AND created_at<=? ORDER BY created_at",(run['conversation_id'],run['created_at']))
    facts_check=None
    for r in state.get('receipts',[]):
        if r['action']!='draft' or 'fields' not in r:continue
        sources=r['fields']['user_facts']+r['fields']['user_attempts']
        facts_check=all(s.get('origin')=='user' and any(m['content'][s['start']:s['end']]==s['text'] for m in history) for s in sources)
        if not sources:facts_check=False
    has_factual_unreferenced_numbers=bool(re.search(r'\d+\s*(元|天|秒|个工作日)',answer) and not row['citations'] and not receipt)
    return {'execution':status_ok,'route_match_diagnostic':base['route'],
        'status_match_diagnostic':base['status'],'service_complete':service,
        'draft_user_quote_integrity':facts_check,'permission_checks':base['no_private_leak'] and base['source_acl'],
        'no_auto_submission':base['no_auto_submission'],
        'unreferenced_number_diagnostic':has_factual_unreferenced_numbers,
        'semantic_correctness':None,'citation_support':None,'draft_semantic_fidelity':None}


def summarize(rows,grades=()):
    grades={g['key']:g for g in grades}
    result={}
    for stage in sorted({r['stage'] for r in rows}):
        group=[r for r in rows if r['stage']==stage]
        calls=sum(r['calls'] for r in group);times=sorted(r['elapsed'] for r in group)
        keys=('execution','service_complete','draft_user_quote_integrity','permission_checks','no_auto_submission')
        metrics={k:{'passed':sum(r['quality'][k] is True for r in group),'applicable':sum(r['quality'][k] is not None for r in group)} for k in keys}
        review=[grades.get(r['key'],{}) for r in group]
        sem={k:{'pass':sum(g.get(k)==1 for g in review),'fail':sum(g.get(k)==0 for g in review),
            'unknown':sum(g.get(k) is None for g in review)} for k in ('correctness','citation_support','task_completion','factual_fidelity')}
        conv={}
        for r in group:conv.setdefault(r['case_id'],[]).append(r)
        result[stage]={'records':len(group),'metrics':metrics,'model_calls':calls,
            'elapsed_seconds':sum(times),'p50_seconds':statistics.median(times),
            'p95_seconds':times[min(len(times)-1,__import__('math').ceil(.95*len(times))-1)],
            'conversation_execution_complete':sum(all(r['quality']['execution'] for r in rr) for rr in conv.values()),
            'conversations':len(conv),'semantic_model_review':sem,'semantic_note':'model review, not independent human accuracy'}
    return result
