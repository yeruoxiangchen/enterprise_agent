"""Full paired ITSM comparison using the existing native benchmark components."""
from __future__ import annotations

from dataclasses import asdict
import ast
import copy
from pathlib import Path
import hashlib
import importlib.metadata
import json
import subprocess
import time

import yaml

from crm_bench_control import write
from crm_bench_services import LocalEvalClient
from kb.benchmark.crm_deep_adapter import DeepCRMAdapter
from kb.benchmark.crm_dialogue import VerifiedContextualUserSimulator
from kb.benchmark.crm_reference import OfficialGraphAdapter
from kb.benchmark.crm_metrics import local_verdict,summarize,report_markdown
from kb.config import Settings

ROOT=Path('/data/zjr/enterprise_kb_agent')
UPSTREAM=ROOT/'benchmarks/crm-agent-bench'
ARMS=('official_graph','deep_harness')
PIN='fc9575cf9bda085aa1f66d920fea944588005964'
EVALUATION_INFRASTRUCTURE=frozenset({'grader_infrastructure','user_simulator_divergence','user_simulator_timeout'})
RESUME_RUNNER_FILES=frozenset({'scripts/harness_assessment.py','scripts/harness_comparison.py'})
FIDELITY_REPAIR_FILES=frozenset({'kb/benchmark/customer_contract.py','kb/benchmark/crm_dialogue.py'})


def execution_signature(text):
    """Check actor/evaluator construction even when runner bookkeeping changes."""
    module=ast.parse(text)
    functions={node.name:node for node in module.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
    factory=functions['create_adapter'];execute=functions['execute_case']
    before_execution=[]
    for node in execute.body:
        if isinstance(node,ast.Try):break
        before_execution.append(node)
    names={'run_task','finish_deterministic_checks','_trace_task_success'}
    calls=[node for node in ast.walk(execute)if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Name)and node.func.id in names]
    if {node.func.id for node in calls}!=names:raise RuntimeError('Comparison execution contract is incomplete')
    imports=[node for node in module.body if isinstance(node,ast.ImportFrom)
        and any(item.name in {'DeepCRMAdapter','OfficialGraphAdapter','VerifiedContextualUserSimulator',
                             'LocalEvalClient','Settings'}for item in node.names)]
    return [ast.dump(node,include_attributes=False)for node in [*imports,factory,*before_execution,*calls]]


def classify_row(row):
    """Retain native grading; account for the configured agent deadline locally."""
    result=copy.deepcopy(row)
    trace=result['trace'];raw=result['official_verdict']
    local=local_verdict(raw,trace.get('error'))
    if raw is None and (trace.get('error_kind')=='agent_timeout'or
            (trace.get('error')or'').startswith(('BudgetExceeded:','ExecutionStalled:'))):
        local=False
    result['local_verdict']=local
    result['failure_accounting_version']='agent-deadline-local-failure-v2'
    result['evaluation_infrastructure_error']=(local is None and trace.get('error_kind')in EVALUATION_INFRASTRUCTURE)
    return result


def fidelity_repair_signature(source):
    """Protect frozen task annotations, response grading and user generation."""
    signatures=[]
    for name,selected in [
        ('kb/benchmark/customer_contract.py',{'SourceFact','ExitCondition','CustomerContract','compile_contract',
            'CommunicationClient','ObservedDialogueJudge'}),
        ('kb/benchmark/crm_dialogue.py',{'ContextualReactUserSimulator'})]:
        module=ast.parse((source/name).read_text())
        found={node.name:node for node in module.body if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef))}
        for selected_name in sorted(selected):signatures.append(ast.dump(found[selected_name],include_attributes=False))
        if name.endswith('crm_dialogue.py'):
            paired=found['PairedUserSimulator']
            for node in paired.body:
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))and node.name!='_review_decision':
                    signatures.append(ast.dump(node,include_attributes=False))
            for node in module.body:
                if isinstance(node,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='CUSTOMER_RULES'for t in node.targets):
                    signatures.append(ast.dump(node,include_attributes=False))
    return signatures


def inherit_results(folder,protocol,parent,*,evaluator_repair=False):
    """Import completed records only when actor and evaluator code are unchanged."""
    source=Path(__file__).resolve().parents[1]
    parent=Path(parent).resolve()
    if parent==folder.resolve():raise ValueError('Use a new tag for continuation')
    from crm_bench_control import identity
    status=json.loads((parent/'status.json').read_text())
    if identity(status.get('pid',-1))==status.get('start_ticks')and status.get('start_ticks'):
        raise RuntimeError('Parent comparison is still running')
    if status.get('resources_released')is not True:raise RuntimeError('Parent resources must be released before continuation')
    old=json.loads((parent/'comparison-protocol.json').read_text())
    ignored={'gpus_requested','max_hours','continuation','failure_accounting_version'}
    if evaluator_repair:ignored.add('evaluator_calibration')
    for key,value in old.items():
        if key not in ignored and protocol.get(key)!=value:
            raise RuntimeError('Continuation protocol mismatch: '+key)
    manifest=json.loads((parent/'source-manifest.json').read_text())
    changed=[]
    for name,expected in manifest.items():
        if hashlib.sha256((parent/'source'/name).read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Parent archived source changed: '+name)
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=expected:
            allowed=RESUME_RUNNER_FILES|(FIDELITY_REPAIR_FILES if evaluator_repair else frozenset())
            if name not in allowed:raise RuntimeError('Actor or evaluator changed; use a fresh comparison: '+name)
            changed.append(name)
    comparison_name='scripts/harness_comparison.py'
    if execution_signature((parent/'source'/comparison_name).read_text())!=execution_signature((source/comparison_name).read_text()):
        raise RuntimeError('Actor or evaluator setup changed inside comparison runner; use a fresh comparison')
    if evaluator_repair and fidelity_repair_signature(parent/'source')!=fidelity_repair_signature(source):
        raise RuntimeError('User generation, task contract or final response scoring changed; cannot inherit under fidelity repair')
    def exchange_rules(root):
        path=root/'kb/benchmark/customer_exchanges_dev16.json'
        return {rule['task_id']:rule for rule in json.loads(path.read_text())['tasks']}if path.exists()else{}
    old_exchanges=exchange_rules(parent/'source');new_exchanges=exchange_rules(source)
    entries=[];skipped=[];keys=set();task_ids=set(protocol['task_ids'])
    for path in sorted((parent/'rows').glob('*.json')):
        original=json.loads(path.read_text());key=(original['task_id'],original['arm'],original['trial'])
        if key[0]not in task_ids or key[1]not in ARMS or key[2]!=0 or key in keys:
            raise RuntimeError('Invalid or duplicate parent trial: '+str(key))
        keys.add(key)
        row=classify_row(original)
        if evaluator_repair and row['evaluation_infrastructure_error']:
            skipped.append({'filename':path.name,'reason':row['trace'].get('error_kind'),
                'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'agent_tokens':row['agent_usage'].get('total_tokens',0),
                'agent_calls':row['agent_usage'].get('num_calls',0)})
            continue
        if evaluator_repair and old_exchanges.get(key[0])!=new_exchanges.get(key[0]):
            raise RuntimeError('Customer exchange rules changed for an inherited task: '+key[0])
        row['inherited_from']={'run':str(parent),'row':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'original_local_verdict':original['local_verdict']}
        destination=folder/'rows'/path.name
        if destination.exists()and json.loads(destination.read_text())!=row:
            raise RuntimeError('Continuation destination already has a different trial')
        write(destination,row)
        entries.append({'filename':path.name,'task_id':key[0],'arm':key[1],
            'source_sha256':row['inherited_from']['sha256']})
    if not entries:raise RuntimeError('Parent comparison has no completed records')
    for task_id in {key[0]for key in keys}:
        opening=parent/'paired_openings'/(task_id+'.json')
        if not opening.exists():continue  # A failed opening remains an unknown trial.
        destination=folder/'paired_openings'/opening.name;destination.parent.mkdir(parents=True,exist_ok=True)
        data=opening.read_bytes()
        if destination.exists()and destination.read_bytes()!=data:raise RuntimeError('Continuation customer opening differs')
        destination.write_bytes(data)
    plan={'parent':str(parent),'inherited_trials':len(entries),'pending_trials':protocol['expected_trials']-len(entries),
        'runner_changes':changed,'actor_and_evaluator_unchanged':not evaluator_repair,
        'actor_and_response_scoring_unchanged':True,'evaluator_fidelity_repair':evaluator_repair,
        'skipped_evaluation_errors':skipped,
        'skipped_evaluation_actor_tokens':sum(row['agent_tokens']for row in skipped),
        'skipped_evaluation_actor_calls':sum(row['agent_calls']for row in skipped),
        'scope':'same frozen actor, task and final grader; customer fidelity repaired; genuine failures retained'
            if evaluator_repair else'same frozen actor and evaluator; runner accounting/continuation fix only',
        'entries':entries}
    write(folder/'resume-plan.json',plan)
    return {key:value for key,value in plan.items()if key!='entries'}


async def prepare(args,folder):
    from harness.bench_runner import load_tasks
    from harness.benchmark_spec import validate_lock,validate_gold_task
    import harness.tools.domains
    commit=subprocess.check_output(['git','-C',str(UPSTREAM),'rev-parse','HEAD'],text=True).strip()
    if commit!=PIN:raise RuntimeError('Pinned benchmark commit changed')
    errors=validate_lock()
    if errors:raise RuntimeError('Benchmark freeze validation failed: '+str(errors))
    path=UPSTREAM/'src/tasks/itsm.yaml'
    tasks=load_tasks(str(path));ids=[task.id for task in tasks]
    if len(ids)!=66 or len(set(ids))!=66:raise RuntimeError('Expected 66 distinct public ITSM tasks')
    requested=tuple(item.strip()for item in (args.task_ids or '').split(',')if item.strip())
    if requested:
        if len(requested)>16 or len(set(requested))!=len(requested)or set(requested)-set(ids):
            raise ValueError('Diagnostic filter must select 1–16 distinct public ITSM task IDs')
        tasks=[task for task in tasks if task.id in requested]
    raw={row['id']:row for row in yaml.safe_load(path.read_text())['tasks']}
    for task in tasks:
        errors=await validate_gold_task('itsm',raw[task.id])
        if errors:raise RuntimeError('Canonical gold path failed: '+str(errors))
    cfg=Settings()
    protocol={'version':'deep-harness-official-itsm-v1','upstream_commit':commit,
        'scope':'public ITSM development catalogue; shared actor/user/judge weights',
        'full_catalogue':not bool(requested),'task_ids':[task.id for task in tasks],
        'arms':list(ARMS),'trials':1,'expected_trials':len(tasks)*2,
        'context_tokens':8192,'output_tokens':1400,'seed':20261114,
        'model_calls_per_turn':cfg.harness_model_calls,'tool_calls_per_turn':cfg.harness_tool_calls,
        'tool_budget_scope':'canonical_business_attempts','model_budget_scope':'all actor calls including routing and structured control',
        'max_turns':30,'agent_step_timeout_seconds':args.agent_step_timeout,
        'task_timeout_seconds':args.task_timeout,'user_step_timeout_seconds':60,
        'http_timeout_seconds':90,'max_hours':args.max_hours,'workflow_memory':False,'goal_review':False,
        'simulator':'VerifiedContextualUserSimulator','simulator_review':'audited',
        'customer_review_reason_first':True,'paired_openings':True,
        'order':'alternate first arm by catalogue index','native_scores_preserved':True,
        'pricing':'self-hosted; monetary charge not measured',
        'gpus_requested':args.gpus,'gpu_memory_fraction':.78,'gpu_headroom_mib':1024,
        'raw_task_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'capability_by_task':{task.id:task.capabilities[0]for task in tasks},
        'dependencies':{name:importlib.metadata.version(name)for name in
            ['deepagents','langchain','langgraph','langmem','pydantic','openai']}}
    protocol['failure_accounting_version']='agent-deadline-local-failure-v2'
    reference=getattr(args,'reference_from',None)
    if reference and (Path(reference).name!=reference or reference in ('.','..')):raise ValueError('Reference must be a run tag')
    if reference or getattr(args,'development16',False):
        if not requested:raise ValueError('Reference reuse currently requires the reviewed development task filter')
        from harness_v17_evaluation import TASKS16
        if set(requested)-set(TASKS16):raise ValueError('This filter includes tasks without reviewed customer contracts')
        protocol.update(version='receipt-handoff-contract-v17',customer_contract=True,
            user_step_timeout_seconds=90,grading_profile='observed-dialogue-trusted-interpretation-v5',reference_from=reference,
            reference_reuse='cached trajectories must pass the current customer contract and be regraded; otherwise run fresh',
            compact_handoffs=True,customer_contract_version='source-reviewed-dev16-v5',native_scores_preserved=False,
            customer_turn_verifier='upstream-verify-compact-with-account-attribution-v2',
            response_criteria_preserved=True,deterministic_assertions_preserved=True,
            reference_timing='cached actor timing is inherited; current validation and inference usage are recorded separately')
        calibration_path=folder/'evaluator-calibration.json'
        if calibration_path.exists():
            calibration=json.loads(calibration_path.read_text())
            protocol['evaluator_calibration']=calibration
            protocol['semantic_review_required']=bool(calibration.get('semantic_review_required',True))
    resume=getattr(args,'resume_from',None)
    if resume:
        if Path(resume).name!=resume or resume in ('.','..'):raise ValueError('Parent must be a run tag')
        protocol['continuation']=inherit_results(folder,protocol,folder.parent/resume,
            evaluator_repair=bool(getattr(args,'repair_evaluator',False)))
    write(folder/'comparison-protocol.json',protocol)
    # Archive the pinned upstream code and task definitions on /data. Actors
    # receive only dialogue and the public policy, never gold task assertions.
    for source in [*sorted((UPSTREAM/'src/harness').rglob('*.py')),path,
                   UPSTREAM/'src/agents/itsm.yaml',UPSTREAM/'src/tasks/policies/itsm_policy.md']:
        target=folder/'source/upstream'/source.relative_to(UPSTREAM)
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(source.read_bytes())
    return protocol


def create_adapter(arm,environment,settings,config,policy,seed,path,protocol):
    if arm=='official_graph':
        return OfficialGraphAdapter(environment=environment,settings=settings,config=config,policy=policy,
            seed=seed,output=path,model_call_limit=protocol['model_calls_per_turn'],
            tool_call_limit=protocol['tool_calls_per_turn'],tool_budget_scope=protocol['tool_budget_scope'])
    if arm=='deep_harness':
        return DeepCRMAdapter(environment,settings,config,policy,path,seed=seed)
    raise ValueError('Unknown comparison arm')


def turn_timings(adapter):
    return [value if isinstance(value,dict)else{'turn':i,'seconds':value}
        for i,value in enumerate(adapter.turn_times)]


async def execute_case(task,arm,folder,service,protocol):
    if protocol.get('customer_contract'):
        from harness_v17_evaluation import execute
        return await execute(task,arm,folder,service,protocol)
    from harness.bench_runner import run_task,_trace_task_success
    from harness.semantic_grader import SemanticJudge,SemanticJudgeConfig
    from harness.tools.environment import create_environment
    import harness.tools.domains
    from crm_benchmark import finish_deterministic_checks
    path=folder/'trials'/task.id/arm;path.mkdir(parents=True,exist_ok=False)
    config=yaml.safe_load((UPSTREAM/'src/agents/itsm.yaml').read_text())
    policy=(UPSTREAM/'src/tasks/policies/itsm_policy.md').read_text()
    seed=protocol['seed']
    settings=Settings(root=path,mode='agent',llm_url=service['url']+'/v1',llm_model=service['model'],
        llm_key='local',tokenize_url=service['url']+'/tokenize',context_tokens=protocol['context_tokens'],
        output_tokens=protocol['output_tokens'],timeout=protocol['http_timeout_seconds'],
        harness_model_calls=protocol['model_calls_per_turn'],harness_tool_calls=protocol['tool_calls_per_turn'],
        harness_goal_review=False,supervisor_strategy='baseline')
    adapter=create_adapter(arm,create_environment('itsm',task.initial_state),settings,config,policy,seed,path,protocol)
    user_client=LocalEvalClient(service['url'],service['model'],path/'user-model.jsonl',seed,
        context_tokens=8192,reason_first_grading=True)
    judge_client=LocalEvalClient(service['url'],service['model'],path/'judge-model.jsonl',20261113,context_tokens=8192)
    user=VerifiedContextualUserSimulator(user_client,temperature=0,max_tokens=500,max_message_chars=4000,
        opening_path=folder/'paired_openings'/(task.id+'.json'),
        identity={'task':task.id,'seed':seed,'version':'verified-contextual-v1/audited/reason-first'},
        adapter=adapter,arm=arm,verify_fidelity=True,fidelity_attempts=3,verification_mode='audited')
    judge=SemanticJudge(judge_client,SemanticJudgeConfig(provider='openai',model=service['model'],
        max_tokens=1600,max_attempts=3,endpoint_fingerprint='deep-harness-development',cache_dir=folder/'judge_cache'))
    try:
        trace=await run_task(task,adapter,user,semantic_judge=judge,max_turns=protocol['max_turns'],
            timeout_seconds=protocol['task_timeout_seconds'],
            agent_step_timeout_seconds=protocol['agent_step_timeout_seconds'],
            user_step_timeout_seconds=protocol['user_step_timeout_seconds'])
        trace.trial=0;trace.seed=seed
        finish_deterministic_checks(task,trace,adapter)
        trace.agent_usage={'prompt_tokens':adapter.prompt_tokens,'completion_tokens':adapter.completion_tokens,
            'total_tokens':adapter.prompt_tokens+adapter.completion_tokens,'num_calls':adapter.model_calls}
        trace.user_sim_usage=dict(user_client.usage)
        trace.agent_model=trace.user_sim_model=service['model'];trace.agent_llm_seconds=adapter.llm_seconds
        adapter.write_environment()
        raw=_trace_task_success(trace)
        row={'arm':arm,'task_id':task.id,'trial':0,'seed':seed,'official_verdict':raw,
            'trace':asdict(trace),'agent_usage':{**trace.agent_usage,'usage_missing_calls':adapter.usage_missing_calls},
            'agent_turn_times':turn_timings(adapter),'user_usage':dict(user_client.usage),
            'judge_usage':dict(judge_client.usage),'user_llm_seconds':user_client.seconds,
            'judge_llm_seconds':judge_client.seconds,'paired_opening':user.opening_metadata,
            'execution_outcomes':adapter.execution_outcomes,'execution_version':
                'official-graph-business-budget-v1'if arm=='official_graph'else'ordered-receipt-grounded-delivery-v16',
            'finished_at':time.time(),'trial_output':str(path)}
        row=classify_row(row)
        write(path/'result.json',row)
        write(folder/'rows'/(task.id+'__'+arm+'.json'),row)
        return row
    finally:
        await adapter.aclose();await user_client.aclose();await judge_client.aclose()


def update_report(folder,protocol,rows):
    summary=summarize(rows,protocol['task_ids'],trials=1,arms=ARMS)
    summary.update(scope=protocol['scope'],protocol=protocol['version'],
        evaluation={'shared_evaluation_model':True,'simulator_review':'audited',
                    'customer_review_reason_first':True})
    summary['continuation']=protocol.get('continuation')
    summary['reference_reused']=sum(row.get('reference_mode')=='cached_regraded'for row in rows)
    summary['reference_fresh']=sum(row.get('reference_mode')=='fresh'for row in rows)
    summary['new_actor_tokens']=sum(row['agent_usage']['total_tokens']for row in rows
        if not row.get('inherited_from')and row.get('reference_mode')!='cached_regraded')
    summary['inherited_actor_tokens']=sum(row['agent_usage']['total_tokens']for row in rows if row.get('inherited_from'))
    summary['cached_reference_actor_tokens']=sum(row['agent_usage']['total_tokens']for row in rows
        if not row.get('inherited_from')and row.get('reference_mode')=='cached_regraded')
    summary['semantic_review_required']=protocol.get('semantic_review_required',False)
    summary['evaluator_calibration']=protocol.get('evaluator_calibration')
    write(folder/'summary.json',summary)
    note=('本轮原生语义判分包含已知的明确完成句误判，分数保留，须结合实际对话复核。'
        '工具调用、顺序、状态与原始模型输出均保留。见 evaluator-calibration.json。\n\n'
        if summary['semantic_review_required']else'')
    (folder/'report.md').write_text(note+report_markdown(summary))
    return summary


async def run_comparison(args,folder,service,progress,protocol,execute=execute_case):
    from harness.bench_runner import load_tasks
    tasks=[task for task in load_tasks(str(UPSTREAM/'src/tasks/itsm.yaml'))if task.id in protocol['task_ids']]
    rows=[];infra_streak=0
    if protocol.get('continuation'):
        plan=json.loads((folder/'resume-plan.json').read_text())
        rows=[json.loads((folder/'rows'/entry['filename']).read_text())for entry in plan['entries']]
    indexed={(row['task_id'],row['arm'])for row in rows}
    if len(indexed)!=len(rows):raise RuntimeError('Duplicate continuation trial records')
    summary=update_report(folder,protocol,rows)
    progress('continuation_ready'if rows else'comparison_ready',completed=len(rows),total=protocol['expected_trials'],
        inherited_trials=len(rows),pending_trials=protocol['expected_trials']-len(rows),
        semantic_review_required=summary['semantic_review_required'])
    for index,task in enumerate(tasks):
        for arm in ARMS if index%2==0 else tuple(reversed(ARMS)):
            if(task.id,arm)in indexed:continue
            manifest=folder/'source-manifest.json'
            if manifest.exists():
                source=Path(__file__).resolve().parents[1]
                for name,expected in json.loads(manifest.read_text()).items():
                    if hashlib.sha256((source/name).read_bytes()).hexdigest()!=expected:
                        raise RuntimeError('Runtime source changed after protocol freeze: '+name)
            progress('evaluation',status='running',task=task.id,arm=arm,completed=len(rows),total=protocol['expected_trials'])
            row=await execute(task,arm,folder,service,protocol)
            row=classify_row(row)
            rows.append(row);summary=update_report(folder,protocol,rows)
            if summary['paired_openings']['different']:
                raise RuntimeError('Paired customer openings differ; inspect saved rows')
            per_arm={name:{key:value for key,value in data.items()if key in
                ['passed','completed','expected','unknown','total_tokens','model_calls','task_active_seconds_p95']}
                for name,data in summary['arms'].items()}
            progress('trial_complete',completed=len(rows),total=protocol['expected_trials'],arms=per_arm,
                reference_reused=summary['reference_reused'],reference_fresh=summary['reference_fresh'],
                semantic_review_required=summary['semantic_review_required'])
            infra_streak=infra_streak+1 if row['evaluation_infrastructure_error']else 0
            if infra_streak>=3:raise RuntimeError('Three consecutive evaluation-infrastructure failures; stop and inspect logs')
    if not summary['complete']:raise RuntimeError('Comparison ended with missing trial records')
    progress('complete',status='complete_review_required',completed=len(rows),total=protocol['expected_trials'],task=None,arm=None)
