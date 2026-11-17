"""Recovery data trains verified actions, preserving original masked context."""
import copy
import hashlib
import json

import pytest

from kb.rl.sft import verified_rows
from kb.rl.bootstrap_data import coverage
from kb.rl.tasks import make_task
from test_rl_sft import real_success


@pytest.fixture
def evidence(tmp_path,monkeypatch):
    import kb.rl.sft_selection as selection
    monkeypatch.setattr(selection,'DATA_ROOT',tmp_path)
    record,result=real_success();result['train_role']='supervisor';result['policy_model_calls_completed']=4
    result['routing_errors']=[{'call_id':'bad','tool':'unknown'}]
    record.update(source_format='vllm-return-token-ids-v1',wire_root=str(tmp_path))
    calls=[('get_ticket',result['audit'][0]['arguments'],'get_ticket'),
           ('unknown',{},'bad'),('check_dependencies',result['audit'][1]['arguments'],'check_dependencies'),
           None]
    wires=[];triplets=[]
    for i,call in enumerate(calls):
        tools=[{'type':'function','function':{'name':name,'parameters':{'type':'object','properties':{
            'ticket_id':{'type':'string'}},'required':['ticket_id']}}}for name in ('get_ticket','check_dependencies')]
        payload={'messages':[{'role':'user','content':'Observe the ticket.'}], 'tools':tools}
        raw=json.dumps(payload);digest=hashlib.sha256(raw.encode()).hexdigest()
        message={'content':'Recovered outcome.'if call is None else None}
        if call:
            name,args,ident=call
            message['tool_calls']=[{'id':ident,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]
        wire={'index':i,'metadata':{'rl_role':'supervisor'},'request_text':raw,'request_sha256':digest,
              'payload':payload,'response':{'prompt_token_ids':[10+i,20+i],
              'choices':[{'message':message,'token_ids':[30+i],'finish_reason':'stop'}],
              'usage':{'prompt_tokens':2,'completion_tokens':1}}}
        wires.append(wire)
        (tmp_path/f'parsed-{i:03d}.json').write_text(json.dumps({'sdk_matches_provider':True,'sdk_message':message}))
        triplets.append({'prompt':{'token_ids':[10+i,20+i]},'response':{'token_ids':[30+i],'finish_reason':'stop'},
                         'source':{'wire_index':i,'request_sha256':digest}})
    (tmp_path/'wire.jsonl').write_text(''.join(json.dumps(r)+'\n'for r in wires))
    record['triplets']=triplets
    return record,result,wires,tmp_path


def test_correct_actions_survive_recovery_while_errors_and_answers_stay_masked(evidence):
    record,result,_,_=evidence
    assert verified_rows(record,result)[1]=='repaired_episode'
    rows,reason=verified_rows(record,result,selection_policy='verified_calls')
    assert reason=='accepted'and len(rows)==2
    assert [r['input_ids']for r in rows]==[[10,20,30],[12,22,32]]
    assert all(r['completion_mask']==[0,0,1]for r in rows)
    assert [r['provenance']['response_verification']['turn']for r in rows]==[0,2]


@pytest.mark.parametrize('part',['request','tokens','sdk'])
def test_tampered_wire_evidence_cannot_create_sft_targets(evidence,part):
    record,result,wires,root=evidence
    if part=='request':wires[0]['request_text']+=' '
    if part=='tokens':wires[0]['response']['prompt_token_ids']=[999]
    if part=='sdk':
        (root/'parsed-000.json').write_text(json.dumps({'sdk_matches_provider':False,'sdk_message':{}}))
    (root/'wire.jsonl').write_text(''.join(json.dumps(r)+'\n'for r in wires))
    with pytest.raises(ValueError):verified_rows(record,result,selection_policy='verified_calls')


def test_actual_business_failure_is_still_rejected_before_response_selection(evidence):
    record,result,_,_=evidence
    result['environment']['users'].clear()
    rows,reason=verified_rows(record,result,selection_policy='verified_calls')
    assert not rows and reason=='business_verification_failed'


def test_warm_start_reports_unsolved_branch_while_strict_coverage_still_fails():
    samples=[make_task('train',i)for i in range(16)if i!=9]+[make_task('train',17)]
    rows=[(s,{},[1])for s in samples]
    assert not coverage(rows)['passed']
    warm=coverage(rows,require_branches=False)
    assert warm['passed']and warm['rl_focus_gaps']==['conditional_reassign:unassigned']
