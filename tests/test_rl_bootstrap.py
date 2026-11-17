"""Original-token capture, clean curation and owned-job failure behavior."""
import asyncio
import copy
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from kb.rl.bootstrap import paired_check,Job
from kb.rl.bootstrap_data import token_record,coverage,collect_one
from kb.rl.sft import verified_rows
from kb.rl.tasks import make_task
from test_rl_sft import real_success


def wire(role='supervisor'):
    return {'metadata':{'rl_role':role},'status_code':200,'index':0,'request_sha256':'original',
        'sdk_matches_provider':True,'response':{'prompt_token_ids':[1,2,3],
            'choices':[{'token_ids':[4,5],'finish_reason':'stop'}],
            'usage':{'prompt_tokens':3,'completion_tokens':2}}}


def test_original_tokens_and_role_isolation_survive_export():
    _,result=real_success();result['train_role']='supervisor'
    record=token_record(make_task('train',6),result,[wire('frozen_reviewer'),wire(),wire('frozen_specialist')],'one')
    rows,reason=verified_rows(record,result)
    assert reason=='accepted'and len(rows)==1
    assert rows[0]['input_ids']==[1,2,3,4,5]
    assert rows[0]['completion_mask']==[0,0,0,1,1]
    assert rows[0]['provenance']['token_source']=='vllm-return-token-ids-v1'


@pytest.mark.parametrize('problem',['ids','usage','sdk','counter'])
def test_incomplete_or_changed_provider_token_evidence_stops_curation(problem):
    _,result=real_success();sample=make_task('train',6);row=wire()
    if problem=='ids':row['response']['prompt_token_ids']=None
    if problem=='usage':row['response']['usage']['completion_tokens']=1
    if problem=='sdk':row['sdk_matches_provider']=False
    if problem=='counter':result['policy_model_calls_completed']=2
    with pytest.raises(ValueError):token_record(sample,result,[row],'one')


@pytest.mark.parametrize('problem',['routing','review','length'])
def test_successful_outcome_with_bad_calls_or_truncated_text_is_not_sft_target(problem):
    record,result=real_success()
    if problem=='routing':result['routing_errors']=[{'tool':'unknown'}]
    if problem=='review':result['review_evaluations']=[{'result':'needs_revision'}]
    if problem=='length':record['triplets'][0]['response']['finish_reason']='length'
    rows,reason=verified_rows(record,result)
    assert not rows and reason in ('repaired_episode','truncated_response')


def test_coverage_needs_distinct_tasks_and_both_conditional_branches():
    samples=[make_task('train',i)for i in range(16)]
    assert not coverage([(s,{},[1])for s in samples[:8]]*4)['passed']
    complete=coverage([(s,{},[1])for s in samples])
    assert complete['passed']and complete['conditional_branches']=={'assigned':1,'unassigned':1}


def scored(task='one',reward=1,*,forbidden=True):
    return {'task_id':task,'task_sha256':task,'harness_profile':{'kind':'same'},
        'family':'close','train_role':'supervisor','supervisor':{'temperature':0},
        'specialist':{'temperature':0},'reward':reward,'error':None,'seconds':1.,
        'tokens':10,'model_calls':2,'usage_missing_calls':0,
        'checks':{'forbidden_attempts':forbidden,'no_extra_committed_writes':True}}


def test_regressed_or_less_safe_sft_never_passes_rl_gate():
    assert paired_check([scored()],[scored()])['passed']
    assert not paired_check([scored()],[scored(reward=0)])['passed']
    assert not paired_check([scored()],[scored(forbidden=False)])['passed']
    changed=scored();changed['harness_profile']={'kind':'different'}
    with pytest.raises(ValueError):paired_check([scored()],[changed])


def test_real_chat_client_collects_original_ids_with_actual_crm_reads(tmp_path,monkeypatch):
    from kb.context import ContextBudget
    import kb.rl.runtime as runtime
    sample=make_task('train',0);target=sample['target_ticket']
    messages=iter([{'role':'assistant','content':None,'tool_calls':[{'id':'read','type':'function',
        'function':{'name':'get_ticket','arguments':json.dumps({'ticket_id':target})}}]},
        {'role':'assistant','content':'The ticket is Open, Medium priority, and unassigned.'}])
    def provider(request):
        payload=json.loads(request.content)
        assert payload['return_token_ids']is True
        message=next(messages)
        return httpx.Response(200,json={'id':'unit','object':'chat.completion','created':1,'model':'unit',
            'prompt_token_ids':[1,2,3],'choices':[{'index':0,'message':message,'token_ids':[4,5],
                'finish_reason':'tool_calls'if message.get('tool_calls')else'stop'}],
            'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}})
    real_client=httpx.Client
    class ControlledClient(real_client):
        def __init__(self,**kw):
            kw.setdefault('transport',httpx.MockTransport(provider))
            super().__init__(**kw)
    monkeypatch.setattr(httpx,'Client',ControlledClient)
    monkeypatch.setattr(ContextBudget,'count',lambda *a:(100,'test'))
    monkeypatch.setattr(runtime,'DATA_ROOT',tmp_path)
    result,rows,reason=asyncio.run(collect_one(sample,{'url':'http://policy.local/v1','model':'unit'},
        {'url':'http://frozen.local/v1','model':'unit'},tmp_path,0,seed=17))
    assert result['reward']==1 and reason=='accepted'and len(rows)==2
    assert result['policy_model_calls_completed']==2
    assert [r['name']for r in result['audit']]==['get_ticket']
    assert all(r['input_ids']==[1,2,3,4,5]for r in rows)


def test_failed_owned_stage_releases_its_child_and_preserves_unrelated_process(tmp_path,monkeypatch):
    import kb.rl.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap,'DATA_ROOT',tmp_path)
    args=SimpleNamespace(tag='controlled',max_minutes=1,gpus='5,6',sft_steps=1,rl_steps=1,
        collect_tasks=16,collect_attempts=2)
    (tmp_path/'outputs/controlled').mkdir(parents=True)
    job=Job(args)
    async def fail():job.command('failure',[sys.executable,'-c','raise SystemExit(3)'])
    job.work=fail
    unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    try:
        job.run()
        assert job.state['status']=='failed'and job.state['resources_released']
        assert job.state['active_process']is None and unrelated.poll()is None
        assert 'exited 3'in job.state['error']
    finally:unrelated.terminate();unrelated.wait(timeout=5)


def test_cleanup_continues_after_one_service_rejects_ownership(tmp_path,monkeypatch):
    import kb.rl.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap,'DATA_ROOT',tmp_path)
    args=SimpleNamespace(tag='cleanup',max_minutes=1,gpus='5,6',sft_steps=1,rl_steps=1,
        collect_tasks=16,collect_attempts=2)
    (tmp_path/'outputs/cleanup').mkdir(parents=True)
    job=Job(args);closed=[]
    class Service:
        def __init__(self,name):self.name=name
        async def close(self):
            closed.append(self.name)
            if self.name=='changed':raise RuntimeError('Ownership changed')
    job.services=[Service('changed'),Service('owned')]
    async def complete():job.save(status='complete_review_required')
    job.work=complete;job.run()
    assert closed==['changed','owned']
    assert job.state['status']=='failed'and job.state['resources_released']is False
    assert job.state['cleanup_errors']==['Ownership changed']


def test_evaluation_server_uses_the_requested_sft_weights(tmp_path,monkeypatch):
    import crm_bench_services as services
    merged=tmp_path/'merged';merged.mkdir();(merged/'config.json').write_text('{}')
    recorded=[]
    monkeypatch.setattr(services,'gpu_snapshot',lambda:{'5':{'free_mib':24126}})
    monkeypatch.setattr(services,'gpu_has_room',lambda *a:True)
    monkeypatch.setattr(services,'identity',lambda pid:'owned')
    def spawn(command,**kwargs):
        recorded.append(command);return SimpleNamespace(pid=12345)
    monkeypatch.setattr(services.subprocess,'Popen',spawn)
    model=services.ModelServices(tmp_path,lambda *a,**kw:None)
    model.start('actor','5',model_path=merged,max_model_len=8192)
    command=recorded[0]
    assert command[command.index('--model')+1]==str(merged)
    assert '--enable-auto-tool-choice'in command
    assert model.owned['actor']['model_path']==str(merged)
