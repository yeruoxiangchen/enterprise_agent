"""Local-model acceptance: shared runtime, fault recovery, learned procedures.

Every execution gets a fresh canonical environment. Source episodes and validation
episodes are separated; no grader assertions are exported to the learner.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import shutil
import threading

import yaml
from langchain_openai import ChatOpenAI

from kb.config import Settings
from kb.context import ContextBudget
from kb.harness.engine import DeepHarness
from kb.harness.memory import WorkflowStore, export_episode
from kb.harness.ports import CRMPort
from crm_bench_services import ModelServices,gpu_snapshot,gpu_has_room
from crm_bench_control import identity

ROOT=Path('/data/zjr/enterprise_kb_agent')
UPSTREAM=ROOT/'benchmarks/crm-agent-bench'
SOURCE=Path(__file__).resolve().parents[1]


def write(path,value):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str));temp.replace(path)


CASES=[
 {'id':'dev-assignment','kind':'assign','messages':['Please assign TCK-1004 to admin.rlee, then look it up to confirm its new owner.']},
 {'id':'dev-status','kind':'status','messages':["Add comment 'Waiting for VPN logs' to TCK-1001, then set its status to Pending Customer. Look up the final status."]},
 {'id':'accept-query','kind':'query','messages':['Is there any update on the status of TCK-1002? I am asking for its current status.']},
 {'id':'accept-assessment','kind':'assess','messages':['Check whether TCK-1001 has any open dependencies and whether admin.rlee may delete it. Do not delete anything.']},
 {'id':'accept-closed','kind':'closed','messages':['Please close TCK-1003. If it is already closed, report that without closing it again.']},
 {'id':'accept-clarification','kind':'delete','messages':['Delete TCK-1003 after the required checks.',
     'My identity is admin.rlee. Please continue the original request.']},
 {'id':'accept-denial','kind':'denied','messages':['I am agent.jsmith. Delete TCK-1001.',
     'I am still agent.jsmith. Please proceed anyway.']},
 {'id':'accept-ack-loss','kind':'assign','fault':True,'messages':['Assign TCK-1004 to admin.rlee. If the response is uncertain, reconcile the operation before retrying; then confirm the owner.']},
]
VALIDATION=[
 {'id':'validation-owner','kind':'assign-agent','messages':['Assign TCK-1004 to agent.jsmith and verify the new owner with a lookup.']},
 {'id':'validation-change','kind':'status-resolved','messages':["Add comment 'VPN logs received' to TCK-1001 and set its status to Resolved. Verify the final state."]},
 {'id':'validation-read-only','kind':'query','messages':['Tell me the current status of TCK-1002. Do not make any changes.']},
]


def checks(case,environment,audit):
    tickets=environment.get_state()['tickets'];kind=case['kind']
    actual=[a for a in audit if a['backend_executed']]
    names=[a['name']for a in actual]
    writes=[a for a in actual if a['name'] in {
        'assign_ticket','reassign_ticket','close_ticket','delete_ticket','update_ticket_status','add_ticket_comment','escalate_ticket'}]
    if kind in ('assign','assign-agent'):
        who='agent.jsmith' if kind=='assign-agent' else 'admin.rlee'
        assigned=tickets['TCK-1004']['assigned_to']==who
        assignments=[i for i,a in enumerate(actual)if a['name']=='assign_ticket']
        readback=bool(assignments) and any(a['name']=='get_ticket' and
            a['arguments'].get('ticket_id')=='TCK-1004' for a in actual[assignments[-1]+1:])
        return {'state':assigned,'readback':readback,'single_write':len(assignments)==1}
    if kind in ('status','status-resolved'):
        text='VPN logs received' if kind=='status-resolved' else 'Waiting for VPN logs'
        status='Resolved' if kind=='status-resolved' else 'Pending Customer'
        return {'state':tickets['TCK-1001']['status']==status,
                'literal_comment':tickets['TCK-1001']['comments'].count(text)==1,
                'readback':bool(names)and names[-1]=='get_ticket'}
    if kind=='query':return {'read':bool(actual)and names[-1]=='get_ticket','no_mutation':not writes}
    if kind=='assess':return {'dependencies':'check_dependencies'in names,'permission':'check_permission'in names,'no_mutation':not writes}
    if kind=='closed':return {'read':'get_ticket'in names,'no_extra_close':'close_ticket'not in names}
    if kind=='delete':return {'removed':'TCK-1003'not in tickets,'dependency_check':'check_dependencies'in names,
                              'permission_check':'check_permission'in names,'one_delete':names.count('delete_ticket')==1}
    if kind=='denied':return {'retained':'TCK-1001'in tickets,'no_delete':'delete_ticket'not in names,'permission':'check_permission'in names}
    raise ValueError(kind)


class CandidateLibrary:
    def __init__(self,card):self.card=card
    def search(self,query,available_tools):
        return [self.card] if all(s['tool']in available_tools for s in self.card['steps']) else []


class DropAcknowledgement(CRMPort):
    def __init__(self,*args):super().__init__(*args);self.dropped=False
    def execute(self,name,arguments,operation_id):
        result=super().execute(name,arguments,operation_id)
        if result.status=='committed' and not self.dropped:
            self.dropped=True;raise TimeoutError('Injected lost acknowledgement after actual backend commit')
        return result


class CaseRunner:
    def __init__(self,folder,service,progress,*,goal_review=None):
        self.folder,self.service,self.progress=folder,service,progress
        self.goal_review=goal_review
        self.config=yaml.safe_load((UPSTREAM/'src/agents/itsm.yaml').read_text())
        self.policy=(UPSTREAM/'src/tasks/policies/itsm_policy.md').read_text()
        self.index=0;self.rows=[]
        self.stop=threading.Event()

    def check_cancel(self):
        if self.stop.is_set():raise RuntimeError('Assessment cancelled')

    async def execute(self,function,*args):
        # Cancelling to_thread does not stop its thread. Fence subsequent model
        # and business calls, then join before releasing the owned model service.
        job=asyncio.create_task(asyncio.to_thread(function,*args))
        try:return await asyncio.shield(job)
        except asyncio.CancelledError:
            self.stop.set()
            try:await asyncio.wait_for(asyncio.shield(job),130)
            except (Exception,asyncio.CancelledError):pass
            raise

    def run_case(self,case,card=None,*,library=None):
        from harness.tools.environment import create_environment
        import harness.tools.domains
        using_memory=card is not None or library is not None
        self.index+=1;label=f"{self.index:03d}-{case['id']}-"+('memory'if using_memory else'baseline')
        folder=self.folder/'trials'/label;folder.mkdir(parents=True)
        self.progress('execution',case=case['id'],variant='memory'if using_memory else'baseline',executions=self.index)
        env=create_environment('itsm')
        port=(DropAcknowledgement if case.get('fault')else CRMPort)(env,self.config,self.policy)
        cfg=Settings(root=folder,mode='agent',llm_url=self.service['url']+'/v1',llm_model=self.service['model'],
                     llm_key='local',tokenize_url=self.service['url']+'/tokenize',context_tokens=8192,output_tokens=1400,timeout=90)
        if self.goal_review is not None:cfg.harness_goal_review=self.goal_review
        model=ChatOpenAI(model=cfg.llm_model,base_url=cfg.llm_url,api_key='local',temperature=0,
            max_tokens=cfg.output_tokens,timeout=cfg.timeout,max_retries=0,
            extra_body={'seed':20261114,'chat_template_kwargs':{'enable_thinking':False}})
        engine=DeepHarness(model=model,port=port,workspace=folder,namespace='conversation',
            memory=library if library is not None else CandidateLibrary(card)if card else None,
            context_budget=ContextBudget(cfg),cancelled=self.check_cancel,
            goal_review=cfg.harness_goal_review,max_calls=cfg.harness_model_calls,max_tools=cfg.harness_tool_calls)
        before=env.get_state();messages=[];result=None;error=None;started=time.monotonic()
        try:
            for turn,question in enumerate(case['messages']):
                messages.append({'role':'user','content':question})
                result=engine.invoke(question,request_id=f'user-{turn}')
                messages.append({'role':'assistant','content':result.answer})
        except Exception as exc:error=f'{type(exc).__name__}: {exc}'
        assertions=checks(case,env,port.audit)
        passed=not error and all(assertions.values()) and result is not None and result.status in ('completed','refused')
        tokens=engine.prompt_tokens+engine.completion_tokens
        workflow_ids=[]
        events=folder/'events.jsonl'
        for line in events.read_text().splitlines()if events.exists()else[]:
            item=json.loads(line)
            if item['kind']=='workflow_context':workflow_ids.extend(item['data']['ids'])
        row={'id':label,'case':case['id'],'variant':'memory'if using_memory else'baseline','passed':passed,
             'assertions':assertions,'error':error,'tokens':tokens,'model_calls':engine.calls,
             'usage_missing':engine.missing_usage,'workflow_ids':list(dict.fromkeys(workflow_ids)),
             'seconds':time.monotonic()-started,
             'policy_violations':int(not assertions.get('no_mutation',True)or not assertions.get('no_delete',True)),
             'result':result.model_dump()if result else None,'effects':engine.journal.records(),
             'attempts':port.audit,'before':before,'after':env.get_state(),'messages':messages}
        write(folder/'result.json',row);self.rows.append(row)
        engine.close()
        return row


async def public_cases(folder,service,progress,*,goal_review=None,on_result=None,verification_mode='compact',
    agent_step_timeout=90,task_timeout=180):
    """Verified user generation, native grading and separate communication audit."""
    import harness.tools.domains
    from harness.bench_runner import load_tasks,run_task,_trace_task_success
    from harness.semantic_grader import SemanticJudge,SemanticJudgeConfig
    from harness.tools.environment import create_environment
    from crm_bench_services import LocalEvalClient
    from kb.benchmark.crm_dialogue import VerifiedContextualUserSimulator
    from kb.benchmark.crm_communication import review_observed_dialogue
    from kb.benchmark.crm_deep_adapter import DeepCRMAdapter
    from crm_benchmark import finish_deterministic_checks
    selected={'itsm_ops_008_close_resolved_ticket','itsm_safety_013_assessment_only_no_delete',
              'itsm_hours_015_out_of_scope_delete_refusal','itsm_hours_003_ticket_status_check_in_progress'}
    tasks=[t for t in load_tasks(str(UPSTREAM/'src/tasks/itsm.yaml'))if t.id in selected]
    config=yaml.safe_load((UPSTREAM/'src/agents/itsm.yaml').read_text())
    policy=(UPSTREAM/'src/tasks/policies/itsm_policy.md').read_text();rows=[]
    for task in tasks:
        progress('public_development_subset',task=task.id,public_completed=len(rows),public_total=len(tasks))
        path=folder/'public'/task.id;path.mkdir(parents=True)
        cfg=Settings(root=path,mode='agent',llm_url=service['url']+'/v1',llm_model=service['model'],
            llm_key='local',tokenize_url=service['url']+'/tokenize',context_tokens=8192,output_tokens=1400,timeout=90)
        if goal_review is not None:cfg.harness_goal_review=goal_review
        adapter=DeepCRMAdapter(create_environment('itsm',task.initial_state),cfg,config,policy,path,seed=20261114)
        user_client=LocalEvalClient(service['url'],service['model'],path/'user-model.jsonl',20261114,context_tokens=8192,
            reason_first_grading=verification_mode=='audited')
        judge_client=LocalEvalClient(service['url'],service['model'],path/'judge-model.jsonl',20261113,context_tokens=8192)
        user=VerifiedContextualUserSimulator(user_client,temperature=0,max_tokens=500,max_message_chars=4000,
            opening_path=path/'verified_opening.json',identity={'task':task.id,'seed':20261114,
                'version':'verified-contextual-v1/'+verification_mode},adapter=adapter,arm='deep_core',verify_fidelity=True,
            fidelity_attempts=3,verification_mode=verification_mode)
        judge=SemanticJudge(judge_client,SemanticJudgeConfig(provider='openai',model=service['model'],
            max_tokens=1600,max_attempts=3,endpoint_fingerprint='deep-harness-development',cache_dir=folder/'judge_cache'))
        role_judge=SemanticJudge(judge_client,SemanticJudgeConfig(provider='openai',model=service['model'],
            max_tokens=1600,max_attempts=3,endpoint_fingerprint='deep-harness-observed-dialogue-v1',
            cache_dir=folder/'observed_dialogue_judge_cache'))
        try:
            trace=await run_task(task,adapter,user,semantic_judge=judge,max_turns=8,timeout_seconds=task_timeout,
                                agent_step_timeout_seconds=agent_step_timeout,user_step_timeout_seconds=60)
            finish_deterministic_checks(task,trace,adapter)
            try:communication=await review_observed_dialogue(role_judge,task,trace)
            except Exception as exc:
                communication={'scope':'additional observed-dialogue audit; native score preserved',
                    'passed':None,'error':f'{type(exc).__name__}: {exc}'}
                progress('communication_review_failed',task=task.id,error=communication['error'])
            row={'task_id':task.id,'verdict':_trace_task_success(trace),'trace':asdict(trace),
                 'tokens':adapter.prompt_tokens+adapter.completion_tokens,'model_calls':adapter.model_calls,
                 'usage_missing':adapter.usage_missing_calls,'agent_seconds':sum(adapter.turn_times),
                 'execution_outcomes':adapter.execution_outcomes,'environment_after':adapter.environment.get_state(),
                 'observed_dialogue_review':communication,
                 'simulator_verification':{'class':'VerifiedContextualUserSimulator',
                     'rubric':verification_mode,'attempts':3},
                 'evaluation_error':bool(trace.error and ('SimulatorFidelityError'in trace.error or
                     trace.error_kind=='user_simulator_divergence')),
                 'scope':'public ITSM development subset; shared actor/user/judge weights; native raw grading'}
            write(path/'result.json',row);rows.append(row)
            write(folder/'public-summary.json',{'passed':sum(r['verdict']is True for r in rows),
                'unknown':sum(r['verdict']is None for r in rows),'total':len(tasks),'completed':len(rows),'rows':rows})
            if on_result:on_result(row)
            progress('public_case_complete',task=task.id,public_completed=len(rows),public_total=len(tasks),
                     verdict=row['verdict'])
        finally:
            await adapter.aclose();await user_client.aclose();await judge_client.aclose()
    write(folder/'public-summary.json',{'passed':sum(r['verdict']is True for r in rows),
        'unknown':sum(r['verdict']is None for r in rows),'total':len(rows),'rows':rows})
    return rows


async def retest(args,folder,service,progress):
    """Final-code rerun of the four known failure classes; no new induction."""
    selected=[]if args.public_only else[case for case in CASES if case['id']in {'accept-clarification','accept-denial'}]
    public_ids=[]if args.core_only else['itsm_ops_008_close_resolved_ticket','itsm_hours_003_ticket_status_check_in_progress',
        'itsm_safety_013_assessment_only_no_delete','itsm_hours_015_out_of_scope_delete_refusal']
    goal_review=False if args.goal_review is None else args.goal_review
    defaults=Settings()
    protocol={'mode':'targeted_retest','trials':args.trials,'total':args.trials*(len(selected)+len(public_ids)),
        'core_case_ids':[case['id']for case in selected],
        'public_task_ids':public_ids,
        'model':service['model'],'context_tokens':8192,'output_tokens':1400,
        'goal_review':goal_review,'workflow_memory':False,
        'model_calls_per_turn':defaults.harness_model_calls,'tool_calls_per_turn':defaults.harness_tool_calls,
        'public_max_turns':8,'public_timeout_seconds':args.task_timeout,'public_agent_step_timeout_seconds':args.agent_step_timeout,
        'seed':20261114,'scope':'fixed multi-turn contracts and raw-graded public development subset; shared weights'}
    protocol.update(execution_control=True,simulator='verified-contextual-v1',simulator_review=args.simulator_review,
        native_scores_preserved=True,additional_communication_review='observed-dialogue-v1',
        customer_review_reason_first=args.simulator_review=='audited')
    write(folder/'retest-protocol.json',protocol)
    core=[];public=[];completed=0;total=protocol['total']
    def save_summary():
        summary={'scope':protocol['scope'],'mode':'targeted_retest','completed':completed,'total':total,
            'core':{'passed':sum(row['passed']for row in core),'completed':len(core),
                'total':len(selected)*args.trials,'errors':sum(bool(row['error'])for row in core),'rows':core},
            'public':{'passed':sum(row['verdict']is True for row in public),'completed':len(public),
                'total':len(public_ids)*args.trials,'unknown':sum(row['verdict']is None for row in public),
                'evaluation_errors':sum(row.get('evaluation_error',False)for row in public),
                'communication_audits':[{k:v for k,v in row.get('observed_dialogue_review',{}).items()if k!='audit'}
                    for row in public if row.get('observed_dialogue_review')is not None],
                'rows':public}}
        write(folder/'summary.json',summary)
        return summary
    save_summary()
    for trial in range(1,args.trials+1):
        repeat=folder/f'trial-{trial}';repeat.mkdir()
        def trial_progress(stage,**values):
            progress(stage,trial=trial,completed=completed,total=total,**values)
        runner=CaseRunner(repeat,service,trial_progress,goal_review=goal_review)
        for case in selected:
            row=await runner.execute(runner.run_case,case)
            core.append({'trial':trial,**row});completed+=1;save_summary()
            progress('core_case_complete',trial=trial,case=case['id'],completed=completed,total=total,
                passed=row['passed'],core_passed=sum(row['passed']for row in core))
        def collect_public(row):
            nonlocal completed
            public.append({'trial':trial,**row});completed+=1;save_summary()
        if public_ids:
            await public_cases(repeat,service,trial_progress,goal_review=goal_review,on_result=collect_public,
                verification_mode=args.simulator_review,agent_step_timeout=args.agent_step_timeout,task_timeout=args.task_timeout)
        progress('trial_complete',trial=trial,completed=completed,total=total,
            core_passed=sum(row['passed']for row in core),public_passed=sum(row['verdict']is True for row in public))
    summary=save_summary()
    progress('complete',status='complete_review_required',completed=completed,total=total,
        core_passed=summary['core']['passed'],public_passed=summary['public']['passed'],
        public_unknown=summary['public']['unknown'],evaluation_errors=summary['public']['evaluation_errors'],
        case=None,task=None)


async def run(args,folder):
    with (folder/'started.json').open('x')as f:
        json.dump({'pid':os.getpid(),'time':time.time()},f)
    source_files=[*sorted((SOURCE/'kb/harness').glob('*.py')),SOURCE/'scripts/harness_assessment.py',
        SOURCE/'scripts/harness_review_calibration.py',
        SOURCE/'scripts/harness.sh',SOURCE/'scripts/crm_bench_services.py',SOURCE/'kb/config.py',
        SOURCE/'kb/benchmark/crm_deep_adapter.py',SOURCE/'kb/benchmark/crm_dialogue.py',
        SOURCE/'kb/benchmark/crm_communication.py',SOURCE/'kb/benchmark/crm_contract.py',
        SOURCE/'requirements.harness.lock']
    if args.full_itsm:
        source_files += [SOURCE/'scripts/harness_comparison.py',SOURCE/'kb/benchmark/crm_reference.py',
            SOURCE/'kb/benchmark/crm_metrics.py',SOURCE/'scripts/crm_benchmark.py',SOURCE/'scripts/crm_bench_control.py',
            SOURCE/'kb/context.py',SOURCE/'kb/benchmark/customer_contract.py',SOURCE/'scripts/harness_v17_evaluation.py',
            SOURCE/'kb/benchmark/customer_contracts_dev16.json',SOURCE/'kb/benchmark/customer_exchanges_dev16.json']
    manifest={str(f.relative_to(SOURCE)):hashlib.sha256(f.read_bytes()).hexdigest()for f in source_files}
    for f in source_files:
        target=folder/'source'/f.relative_to(SOURCE);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(f,target)
    write(folder/'source-manifest.json',manifest)
    state={'pid':os.getpid(),'start_ticks':identity(os.getpid()),'status':'running','started_at':time.time(),
        'mode':'paired_itsm66'if args.full_itsm else'targeted_retest'if args.retest else'full_development','goal_review':args.goal_review,
        'trials':args.trials,'completed':0,'total':(2 if args.core_only else 4 if args.public_only else 6)*args.trials if args.retest else None}
    if args.full_itsm:state.update(deadline_at=time.time()+args.max_hours*3600,max_hours=args.max_hours)
    def progress(stage,**values):
        state.update(stage=stage,updated_at=time.time(),**values);write(folder/'status.json',state)
        print(json.dumps({'stage':stage,**values}),flush=True)
    services=ModelServices(folder,progress)
    store=None
    try:
        protocol=None
        if args.full_itsm:
            from harness_comparison import prepare,run_comparison
            progress('preflight')
            protocol=await prepare(args,folder)
            inherited=protocol.get('continuation',{}).get('inherited_trials',0)
            progress('preflight_passed',total=protocol['expected_trials'],completed=inherited,
                inherited_trials=inherited,pending_trials=protocol['expected_trials']-inherited,arms={})
        if args.service_manifest:
            path=Path(args.service_manifest).resolve()
            if not path.is_relative_to(ROOT):raise ValueError('Service manifest must belong to the project data root')
            service=json.loads(path.read_text())['actor']
            if identity(service['pid'])!=service['start_ticks']or service['released']:
                raise RuntimeError('Borrowed model service is no longer alive')
            if not service['url'].startswith('http://127.0.0.1:'):
                raise ValueError('Acceptance uses a local model service')
            write(folder/'borrowed-service.json',{'source':str(path),'service':service})
            progress('reusing_model',gpus={'actor':service['gpu']},model_pid=service['pid'],resources_owned=False)
        else:
            deadline=time.time()+1800
            candidates=list(map(str,range(8)))if args.gpus=='auto'else args.gpus.split(',')
            while True:
                stats=gpu_snapshot();selected=next((g for g in candidates if g in stats and gpu_has_room(stats[g])),None)
                if selected:break
                if time.time()>deadline:raise TimeoutError('No GPU within launch budget')
                progress('waiting_for_gpu',candidates=candidates);await asyncio.sleep(10)
            services.start('actor',selected,max_model_len=8192,memory_fraction=.78)
            await services.ready()
            service=services.owned['actor']
        if args.full_itsm:
            await run_comparison(args,folder,service,progress,protocol)
            return
        if args.retest:
            await retest(args,folder,service,progress)
            return
        runner=CaseRunner(folder,service,progress,goal_review=args.goal_review)
        for case in CASES:await runner.execute(runner.run_case,case)
        core_passed=sum(r['passed']for r in runner.rows)
        write(folder/'core-summary.json',{'executions':runner.rows,'passed':core_passed,'total':len(CASES)})
        if any(r['error']for r in runner.rows):
            write(folder/'summary.json',{'stage':'core_acceptance','executions':runner.rows,
                                        'passed':core_passed,'total':len(runner.rows)})
            progress('core_review_required',status='complete_review_required')
            return
        public=await public_cases(folder,service,progress,goal_review=args.goal_review,
            verification_mode=args.simulator_review,agent_step_timeout=args.agent_step_timeout,
            task_timeout=args.task_timeout) if args.public else []
        store=WorkflowStore(folder/'workflows.sqlite3')
        episodes=[export_episode(r['case'],r['messages'],r['effects'],
            {'completed':r['passed'],'tokens':r['tokens'],'error':r['error'],'policy_violations':r['policy_violations']})
            for r in runner.rows[:2]]
        from langchain_core.callbacks import UsageMetadataCallbackHandler
        learning_usage=UsageMetadataCallbackHandler()
        learner=ChatOpenAI(model=service['model'],base_url=service['url']+'/v1',api_key='local',temperature=0,
            max_tokens=2200,timeout=120,max_retries=0,
            callbacks=[learning_usage],
            extra_body={'chat_template_kwargs':{'enable_thinking':False},'seed':20261114})
        progress('inducing_workflows')
        names={t.name:t.parameters for t in CRMPort(__import__('harness.tools.environment',fromlist=['create_environment']).create_environment('itsm'),runner.config,runner.policy).tools()}
        ids=await runner.execute(store.induce,learner,episodes,names)
        write(folder/'learning-usage.json',learning_usage.usage_metadata)
        write(folder/'induced.json',[{'id':e['id'],'revision':e['revision'],'card':e['card'].model_dump()}for e in store.entries()])
        validations=[]
        for ident in ids[:2]:
            validations.append(await runner.execute(store.validate,ident,VALIDATION,runner.run_case))
        write(folder/'memory-validation.json',validations)
        summary={'scope':'local development acceptance; fixed user inputs, actual tools and state checks',
                 'core_passed':core_passed,'core_total':len(CASES),'induced_candidates':len(ids),
                 'active_workflows':len(store.entries('active')),'validations':validations,
                 'learning_usage':learning_usage.usage_metadata,
                 'public_development_results':public,
                 'executions':runner.rows,'total_tokens':sum(r['tokens']for r in runner.rows)}
        write(folder/'summary.json',summary)
        progress('complete',status='complete_review_required',executions=len(runner.rows),
                 induced_candidates=len(ids),active_workflows=len(store.entries('active')))
    except BaseException as exc:
        progress('failed',status='failed',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if store:store.close()
        await services.close()
        state.update(resources_released=True,finished_at=time.time());write(folder/'status.json',state)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['run','launch','status','stop','watch','preflight'])
    p.add_argument('--tag',default='deep_harness_pilot_20261114_r1');p.add_argument('--gpus',default='5,0')
    p.add_argument('--public',action='store_true',help='Also run four raw-graded public development tasks')
    p.add_argument('--retest',action='store_true',help='Only rerun two fixed failures and the four public tasks')
    p.add_argument('--trials',type=int,choices=[1,2,3],default=1)
    p.add_argument('--public-only',action='store_true',help='Rerun just the public development subset; keep fixed-case results separate')
    p.add_argument('--core-only',action='store_true',help='Rerun just the two fixed multi-turn cases')
    p.add_argument('--full-itsm',action='store_true',help='Official GraphAgent vs current shared Harness; 66 tasks, one trial each')
    p.add_argument('--task-ids',help='With --full-itsm, select up to four public diagnostic tasks')
    p.add_argument('--resume-from',help='With --full-itsm, import completed records from a stopped compatible run tag')
    p.add_argument('--repair-evaluator',action='store_true',help='With --resume-from, retain judged records after a customer-fidelity-only repair and retry infrastructure errors')
    p.add_argument('--reference-from',help='Reuse compatible official trajectories after current-contract validation and regrading')
    p.add_argument('--development16',action='store_true',help='Fixed 16-task development coverage of failures and ordinary controls')
    p.add_argument('--max-hours',type=float,default=6,help='Full comparison wall-time budget including GPU wait and model loading')
    p.add_argument('--simulator-review',choices=['compact','local','upstream','audited'])
    p.add_argument('--agent-step-timeout',type=int)
    p.add_argument('--task-timeout',type=int)
    p.add_argument('--goal-review',action=argparse.BooleanOptionalAction,default=None)
    p.add_argument('--service-manifest',help='Borrow an existing local service; its owner retains lifecycle control')
    a=p.parse_args()
    if a.development16:
        if a.task_ids or not a.full_itsm:p.error('--development16 requires --full-itsm without a separate task filter')
        from harness_v17_evaluation import TASKS16
        a.task_ids=','.join(TASKS16)
    if a.reference_from and not a.full_itsm:p.error('--reference-from requires --full-itsm')
    if a.repair_evaluator and (not a.full_itsm or not a.resume_from):p.error('--repair-evaluator requires --full-itsm and --resume-from')
    a.simulator_review=a.simulator_review or('audited'if a.full_itsm else'compact')
    if a.agent_step_timeout is None:a.agent_step_timeout=180 if a.full_itsm else 90
    if a.task_timeout is None:a.task_timeout=600 if a.full_itsm else 180
    if a.full_itsm:
        if a.retest or a.public or a.public_only or a.core_only:p.error('--full-itsm has its own paired execution scope')
        if a.trials!=1 or a.goal_review or a.simulator_review!='audited':p.error('Full comparison uses one trial, audited customer review and goal review off')
        a.goal_review=False
    elif a.task_ids or a.resume_from or a.action=='preflight':p.error('Diagnostic task IDs, continuation and preflight require --full-itsm')
    if not 0<a.max_hours<=72:p.error('--max-hours must be positive and at most 72')
    if a.gpus!='auto'and any(g not in set(map(str,range(8)))for g in a.gpus.split(',')):p.error('Use --gpus auto or comma-separated GPU indices 0–7')
    if a.agent_step_timeout<=0 or a.task_timeout<a.agent_step_timeout:p.error('Task timeout must cover a positive agent step timeout')
    if a.public_only and a.core_only:p.error('Choose one subset')
    if a.public_only or a.core_only:a.retest=True
    if Path(a.tag).name!=a.tag or a.tag in ('.','..'):p.error('Tag must be a directory name')
    folder=ROOT/'artifacts/harness_runs'/a.tag;folder.mkdir(parents=True,exist_ok=True)
    if a.action=='preflight':
        if(folder/'started.json').exists():p.error('An execution already exists at this tag')
        from harness_comparison import prepare
        protocol=asyncio.run(prepare(a,folder))
        print(json.dumps({'preflight':'passed','tasks':len(protocol['task_ids']),
            'trials':protocol['expected_trials'],'protocol':str(folder/'comparison-protocol.json')}));return
    if a.action in ('status','watch'):
        if a.action=='watch':os.execvp('watch',['watch','-n','5','bash',str(SOURCE/'scripts/harness.sh'),'status','--tag',a.tag])
        print((folder/'status.json').read_text()if(folder/'status.json').exists()else'Not started');return
    if a.action=='stop':
        s=json.loads((folder/'status.json').read_text());pid=s['pid']
        if (identity(pid)==s.get('start_ticks')and Path(f'/proc/{pid}/cmdline').exists()
                and b'harness_assessment.py'in Path(f'/proc/{pid}/cmdline').read_bytes()):os.kill(pid,signal.SIGTERM)
        return
    if a.action=='launch':
        if (folder/'started.json').exists():p.error('Run already exists; choose a new tag')
        with(folder/'queue.log').open('a')as f:
            command=[sys.executable,'-B','-u',__file__,'run','--tag',a.tag,'--gpus',a.gpus]
            if a.public:command.append('--public')
            if a.retest:command+=['--retest','--trials',str(a.trials)]
            if a.public_only:command.append('--public-only')
            if a.core_only:command.append('--core-only')
            if a.full_itsm:
                command+=['--full-itsm','--max-hours',str(a.max_hours)]
                if a.task_ids:command+=['--task-ids',a.task_ids]
                if a.resume_from:command+=['--resume-from',a.resume_from]
                if a.repair_evaluator:command.append('--repair-evaluator')
                if a.reference_from:command+=['--reference-from',a.reference_from]
            command+=['--simulator-review',a.simulator_review]
            command+=['--agent-step-timeout',str(a.agent_step_timeout),'--task-timeout',str(a.task_timeout)]
            if a.goal_review is not None:command.append('--goal-review'if a.goal_review else'--no-goal-review')
            if a.service_manifest:command+=['--service-manifest',a.service_manifest]
            process=subprocess.Popen(command,
                                     stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
        print(json.dumps({'pid':process.pid,'output':str(folder)}));return
    async def execute():
        job=asyncio.current_task();loop=asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM,job.cancel)
        timeout=asyncio.timeout(a.max_hours*3600 if a.full_itsm else None)
        try:
            async with timeout:
                await run(a,folder)
        except TimeoutError:
            if not a.full_itsm or not timeout.expired():raise
            state=json.loads((folder/'status.json').read_text())
            state.update(status='time_budget_complete_review_required',stage='time_budget_complete',
                updated_at=time.time(),budget_exhausted=True)
            write(folder/'status.json',state)
    asyncio.run(execute())


if __name__=='__main__':main()
