"""Development16: source-grounded customer stages and reusable official traces."""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
import yaml

from crm_bench_control import write
from crm_bench_services import LocalEvalClient
from kb.benchmark.customer_contract import compile_contract,review_contract,ObservedDialogueJudge,CommunicationClient
from kb.benchmark.crm_dialogue import VerifiedContextualUserSimulator
from kb.config import Settings
from harness.bench_runner import ConversationTrace,run_task,_trace_task_success
from harness.semantic_grader import SemanticJudge,SemanticJudgeConfig,SemanticJudgeError
from harness.tools.environment import create_environment
import harness.tools.domains


TASKS16=(
 'itsm_ops_001_lookup_ticket_by_id','itsm_ops_004_assign_unassigned_ticket',
 'itsm_ops_005_add_comment_to_ticket','itsm_ops_008_close_resolved_ticket',
 'itsm_ops_010_reassign_unassigned_ticket_fails','itsm_ops_011_close_already_closed_ticket',
 'itsm_ops_012_comment_then_status_update_order','itsm_ops_013_collateral_close_does_not_affect_dependency',
 'itsm_ops_015_assign_then_confirm_via_lookup','itsm_ops_017_discovery_assign_unassigned_by_description',
 'itsm_ops_018_dependency_aware_close_warning','itsm_ops_021_adversarial_offtopic_redirect',
 'itsm_safety_007_safe_delete_admin_ticket1004','itsm_safety_009_delete_no_permission_agent_403',
 'itsm_safety_010_delete_ticket_with_dependencies_409','itsm_safety_013_assessment_only_no_delete')


def client(service,path,seed=20261114):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    return LocalEvalClient(service['url'],service['model'],path,seed,context_tokens=8192,reason_first_grading=True)


def judge(llm,folder):
    return ObservedDialogueJudge(SemanticJudge(CommunicationClient(llm),SemanticJudgeConfig(provider='openai',model=llm.model,
        max_tokens=2000,max_attempts=3,endpoint_fingerprint='observed-dialogue-trusted-interpretation-v5',
        cache_dir=folder/'communication-cache')))


async def trace_contract_valid(llm,contract,instruction,turns,folder):
    """Accept old inference only if every customer turn and exit remains valid."""
    audits=[]
    for index,turn in enumerate(turns):
        if turn['role']!='user':continue
        result=await review_contract(llm,contract,instruction,turns[:index],folder/'contract-cache',candidate=turn['content'])
        audits.append({'index':index,'kind':'customer_turn','passed':result.passed,'audit':result.audit})
        if not result.passed:return False,audits
    result=await review_contract(llm,contract,instruction,turns,folder/'contract-cache')
    audits.append({'kind':'conversation_exit','passed':result.passed,'audit':result.audit})
    return result.passed,audits


async def cached_reference(task,path,folder,service,protocol,contract):
    import harness_comparison as comparison
    tag=protocol.get('reference_from')
    if not tag:return None
    parent=folder.parent/tag;source=parent/'rows'/(task.id+'__official_graph.json')
    if not source.exists():return None
    previous=json.loads((parent/'comparison-protocol.json').read_text())
    keys=['upstream_commit','context_tokens','output_tokens','seed','model_calls_per_turn',
          'tool_calls_per_turn','tool_budget_scope','max_turns','agent_step_timeout_seconds','task_timeout_seconds','raw_task_sha256']
    if any(previous.get(key)!=protocol.get(key)for key in keys):
        write(path/'cache-disposition.json',{'reuse':False,'reason':'Reference execution protocol differs'});return None
    manifest=json.loads((parent/'source-manifest.json').read_text());source_root=Path(__file__).resolve().parents[1]
    for name in ['kb/benchmark/crm_reference.py','scripts/crm_bench_services.py','kb/context.py']:
        if hashlib.sha256((source_root/name).read_bytes()).hexdigest()!=manifest.get(name):
            write(path/'cache-disposition.json',{'reuse':False,'reason':'Reference execution source differs: '+name});return None
    original=json.loads(source.read_text());error=original['trace'].get('error_kind')
    if error not in (None,'user_simulator_divergence','user_simulator_timeout','grader_infrastructure'):return None
    auditor=client(service,path/'cache-audit-model.jsonl');grader=client(service,path/'cache-grade-model.jsonl',20261113)
    try:
        try:valid,audits=await trace_contract_valid(auditor,contract,task.instruction,original['trace']['turns'],folder)
        except SemanticJudgeError as exc:
            write(path/'cache-disposition.json',{'reuse':False,'source':str(source),'error':str(exc)});return None
        write(path/'cache-disposition.json',{'reuse':valid,'source':str(source),'audits':audits})
        if not valid:return None
        row=copy.deepcopy(original);trace=ConversationTrace(**row['trace'])
        trace.error=None;trace.error_kind=None;trace.timed_out=False;trace.stopped=True
        if task.expected_response_semantic:
            try:
                outcome=await judge(grader,folder).grade(task_id=task.id,task_instruction=task.instruction,
                    criteria=list(task.expected_response_semantic),turns=trace.turns)
                trace.response_passed=outcome.passed;trace.response_failures=outcome.failures;trace.response_judge=outcome.audit
            except SemanticJudgeError as exc:
                trace.response_passed=None;trace.response_judge=None;trace.error=str(exc);trace.error_kind='grader_infrastructure'
        row.update(trace=asdict(trace),official_verdict=_trace_task_success(trace),
            reference_mode='cached_regraded',source_user_usage=row.get('user_usage'),
            semantic_review_required=protocol.get('semantic_review_required',False),
            user_usage=dict(auditor.usage),judge_usage=dict(grader.usage),finished_at=time.time(),
            reference_cache={'source':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                'original_verdict':original['official_verdict'],'original_error':original['trace'].get('error'),
                'original_actor_output':original['trial_output'],'validation_output':str(path)})
        row=comparison.classify_row(row)
        write(path/'result.json',row);write(folder/'rows'/(task.id+'__official_graph.json'),row)
        return row
    finally:await auditor.aclose();await grader.aclose()


async def paired_reference_opening(task,folder,service,protocol,contract):
    """Keep the validated existing opening identical for the two arms."""
    tag=protocol.get('reference_from')
    if not tag:return
    destination=folder/'paired_openings'/(task.id+'.json')
    if destination.exists():return
    source=folder.parent/tag/'rows'/(task.id+'__official_graph.json')
    if not source.exists():return
    original=json.loads(source.read_text())
    opening=next((turn['content']for turn in original['trace']['turns']if turn['role']=='user'),None)
    if not opening:return
    auditor=client(service,folder/'contracts'/(task.id+'-opening-audit.jsonl'))
    try:result=await review_contract(auditor,contract,task.instruction,[],folder/'contract-cache',candidate=opening)
    except SemanticJudgeError:return
    finally:await auditor.aclose()
    if not result.passed:return
    identity={'task':task.id,'seed':protocol['seed'],'version':'customer-contract-v17'}
    fingerprint=hashlib.sha256(json.dumps([identity,task.instruction],sort_keys=True).encode()).hexdigest()
    destination.parent.mkdir(parents=True,exist_ok=True)
    write(destination,{'fingerprint':fingerprint,'message':opening,'provider_calls':2,
        'source_arm':'official_reference_cache'})


async def execute(task,arm,folder,service,protocol):
    import harness_comparison as comparison
    from crm_benchmark import finish_deterministic_checks
    path=folder/'trials'/task.id/arm;path.mkdir(parents=True,exist_ok=True)
    compiler=client(service,folder/'contracts'/(task.id+'-compiler.jsonl'),202611150)
    try:contract=await compile_contract(compiler,task.instruction,folder/'contracts'/(task.id+'.json'))
    finally:await compiler.aclose()
    await paired_reference_opening(task,folder,service,protocol,contract)
    if arm=='official_graph':
        cached=await cached_reference(task,path,folder,service,protocol,contract)
        if cached:return cached
    config=yaml.safe_load((comparison.UPSTREAM/'src/agents/itsm.yaml').read_text())
    policy=(comparison.UPSTREAM/'src/tasks/policies/itsm_policy.md').read_text()
    settings=Settings(root=path,mode='agent',llm_url=service['url']+'/v1',llm_model=service['model'],
        llm_key='local',tokenize_url=service['url']+'/tokenize',context_tokens=8192,output_tokens=1400,timeout=90,
        harness_model_calls=protocol['model_calls_per_turn'],harness_tool_calls=protocol['tool_calls_per_turn'],
        harness_goal_review=False,harness_compact_handoffs=True,supervisor_strategy='baseline')
    adapter=comparison.create_adapter(arm,create_environment('itsm',task.initial_state),settings,config,policy,
        protocol['seed'],path,protocol)
    customer=client(service,path/'user-model.jsonl');grader=client(service,path/'judge-model.jsonl',20261113)
    user=VerifiedContextualUserSimulator(customer,temperature=0,max_tokens=500,max_message_chars=4000,
        opening_path=folder/'paired_openings'/(task.id+'.json'),
        identity={'task':task.id,'seed':protocol['seed'],'version':'customer-contract-v17'},adapter=adapter,
        arm=arm,verify_fidelity=True,fidelity_attempts=3,verification_mode='audited',task_contract=contract)
    try:
        trace=await run_task(task,adapter,user,semantic_judge=judge(grader,folder),max_turns=protocol['max_turns'],
            timeout_seconds=protocol['task_timeout_seconds'],agent_step_timeout_seconds=protocol['agent_step_timeout_seconds'],
            user_step_timeout_seconds=90)
        trace.trial=0;trace.seed=protocol['seed'];finish_deterministic_checks(task,trace,adapter)
        trace.agent_usage={'prompt_tokens':adapter.prompt_tokens,'completion_tokens':adapter.completion_tokens,
            'total_tokens':adapter.prompt_tokens+adapter.completion_tokens,'num_calls':adapter.model_calls}
        trace.user_sim_usage=dict(customer.usage);trace.agent_model=trace.user_sim_model=service['model']
        trace.agent_llm_seconds=adapter.llm_seconds;adapter.write_environment()
        row=comparison.classify_row({'arm':arm,'task_id':task.id,'trial':0,'seed':protocol['seed'],
            'official_verdict':_trace_task_success(trace),'trace':asdict(trace),
            'agent_usage':{**trace.agent_usage,'usage_missing_calls':adapter.usage_missing_calls},
            'agent_turn_times':comparison.turn_timings(adapter),'user_usage':dict(customer.usage),
            'judge_usage':dict(grader.usage),'execution_outcomes':adapter.execution_outcomes,
            'paired_opening':user.opening_metadata,
            'semantic_review_required':protocol.get('semantic_review_required',False),
            'execution_version':'receipt-specialist-handoff-v17'if arm=='deep_harness'else'official-graph-business-budget-v1',
            'reference_mode':'fresh'if arm=='official_graph'else None,
            'finished_at':time.time(),'trial_output':str(path)})
        write(path/'result.json',row);write(folder/'rows'/(task.id+'__'+arm+'.json'),row)
        return row
    finally:await adapter.aclose();await customer.aclose();await grader.aclose()
