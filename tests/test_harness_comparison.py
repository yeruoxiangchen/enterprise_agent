import asyncio
import copy
import json
import hashlib
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest
import yaml

import harness_comparison as comparison
import harness_assessment as assessment
from harness.bench_runner import ConversationTrace,load_tasks
from harness.models import ChatResponse,ToolCall
from harness.tools.environment import create_environment
import harness.tools.domains
from kb.benchmark.crm_reference import OfficialGraphAdapter
from kb.config import Settings
from kb.context import ContextBudget


def args(**overrides):
    return SimpleNamespace(**dict(dict(task_ids=None,agent_step_timeout=180,task_timeout=600,
        max_hours=6,gpus='auto',resume_from=None),**overrides))


def test_full_protocol_validates_all_gold_paths_and_allocates_132_trials(tmp_path):
    protocol=asyncio.run(comparison.prepare(args(),tmp_path))
    assert len(protocol['task_ids'])==66 and protocol['expected_trials']==132
    assert protocol['arms']==['official_graph','deep_harness']
    assert protocol['goal_review']is False and protocol['workflow_memory']is False
    assert protocol['model_calls_per_turn']==Settings().harness_model_calls
    assert protocol['tool_budget_scope']=='canonical_business_attempts'
    assert protocol['task_timeout_seconds']==600 and protocol['agent_step_timeout_seconds']==180
    assert (tmp_path/'source/upstream/src/tasks/itsm.yaml').is_file()


@pytest.mark.parametrize('task_ids',['missing','itsm_ops_001_lookup_ticket_by_id,itsm_ops_001_lookup_ticket_by_id'])
def test_invalid_diagnostic_filters_stop_before_loading_models(tmp_path,task_ids):
    with pytest.raises(ValueError):asyncio.run(comparison.prepare(args(task_ids=task_ids),tmp_path))


def test_adapter_factory_uses_new_harness_and_matched_reference_budgets(tmp_path,monkeypatch):
    captured={}
    def official(**kwargs):captured['official']=kwargs;return 'reference'
    def deep(*args,**kwargs):captured['deep']=(args,kwargs);return 'candidate'
    monkeypatch.setattr(comparison,'OfficialGraphAdapter',official)
    monkeypatch.setattr(comparison,'DeepCRMAdapter',deep)
    settings=Settings(root=tmp_path,harness_model_calls=40,harness_tool_calls=48,harness_goal_review=False)
    protocol={'model_calls_per_turn':40,'tool_calls_per_turn':48,'tool_budget_scope':'canonical_business_attempts'}
    assert comparison.create_adapter('official_graph',object(),settings,{},'',42,tmp_path,protocol)=='reference'
    assert comparison.create_adapter('deep_harness',object(),settings,{},'',42,tmp_path,protocol)=='candidate'
    assert captured['official']['model_call_limit']==40 and captured['official']['tool_call_limit']==48
    assert captured['official']['tool_budget_scope']=='canonical_business_attempts'
    assert captured['deep'][0][1]is settings
    assert captured['deep'][1]['seed']==42


def row(task,arm,passed):
    trace=ConversationTrace(task_id=task.id,stopped=True)
    trace.turns=[{'role':'user','content':'Please look up this ticket.'}]
    return {'arm':arm,'task_id':task.id,'trial':0,'official_verdict':passed,'local_verdict':passed,
        'trace':asdict(trace),'agent_usage':{'total_tokens':15,'prompt_tokens':10,'completion_tokens':5,
            'num_calls':2,'usage_missing_calls':0},'agent_turn_times':[{'seconds':2.0}],
        'execution_version':'test'}


def test_paired_loop_alternates_order_and_preserves_failure_unknown_and_cost(tmp_path):
    tasks=load_tasks(str(comparison.UPSTREAM/'src/tasks/itsm.yaml'))[:2]
    protocol={'task_ids':[task.id for task in tasks],'expected_trials':4,'scope':'test','version':'test'}
    calls=[];events=[]
    async def execute(task,arm,*unused):
        calls.append((task.id,arm))
        value=(arm=='deep_harness')if task.id==tasks[0].id else(True if arm=='official_graph'else None)
        return row(task,arm,value)
    asyncio.run(comparison.run_comparison(args(),tmp_path,{},lambda stage,**v:events.append((stage,v)),protocol,execute))
    assert calls==[(tasks[0].id,'official_graph'),(tasks[0].id,'deep_harness'),
                  (tasks[1].id,'deep_harness'),(tasks[1].id,'official_graph')]
    result=json.loads((tmp_path/'summary.json').read_text())
    assert result['complete']and result['completed_trials']==4
    assert result['arms']['official_graph']['passed']==result['arms']['deep_harness']['passed']==1
    assert result['arms']['deep_harness']['unknown']==1
    assert result['arms']['deep_harness']['total_tokens']==30
    assert result['arms']['deep_harness']['tokens_per_success']==30
    assert result['paired_openings']=={'same':2,'different':0,'missing':0}
    assert events[-1][1]['status']=='complete_review_required'
    assert 'Qwen3-8B'in (tmp_path/'report.md').read_text()


def test_launch_command_forwards_full_scope_and_validated_profile(tmp_path,monkeypatch,capsys):
    sent=[]
    monkeypatch.setattr(assessment,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['harness_assessment.py','launch','--full-itsm','--tag','command-check','--gpus','auto'])
    monkeypatch.setattr(assessment.subprocess,'Popen',lambda command,**kwargs:sent.append(command)or SimpleNamespace(pid=12345))
    assessment.main()
    command=sent[0]
    for flag,value in [('--simulator-review','audited'),('--agent-step-timeout','180'),
                       ('--task-timeout','600'),('--gpus','auto')]:
        assert command[command.index(flag)+1]==value
    assert float(command[command.index('--max-hours')+1])==6
    assert '--full-itsm'in command and '--no-goal-review'in command
    assert json.loads(capsys.readouterr().out)['pid']==12345


@pytest.mark.parametrize('arguments',[
    ['--full-itsm','--trials','2'],['--full-itsm','--simulator-review','compact'],
    ['--full-itsm','--goal-review'],['--full-itsm','--agent-step-timeout','0'],
    ['--full-itsm','--task-timeout','10'],['--full-itsm','--retest'],
])
def test_invalid_profiles_are_rejected_before_queue_start(monkeypatch,arguments):
    monkeypatch.setattr(sys,'argv',['harness_assessment.py','launch',*arguments])
    with pytest.raises(SystemExit)as exc:assessment.main()
    assert exc.value.code==2


@pytest.mark.parametrize('scope',['canonical_business_attempts','all_model_tools'])
def test_reference_business_budget_excludes_router_but_meters_all_actor_calls(tmp_path,monkeypatch,scope):
    config=yaml.safe_load((comparison.UPSTREAM/'src/agents/itsm.yaml').read_text())
    monkeypatch.setattr(ContextBudget,'count',lambda *a:(100,'test'))
    options={'tool_budget_scope':scope}if scope=='canonical_business_attempts'else{}
    owner=OfficialGraphAdapter(environment=create_environment('itsm'),
        settings=Settings(root=tmp_path,context_tokens=8192,output_tokens=1400),
        config=config,policy='',seed=42,output=tmp_path,tool_call_limit=1,**options)
    original=owner.http
    class Client:
        seed=42;seconds=0.0;metered_calls=0
        usage={'num_calls':0,'prompt_tokens':0,'completion_tokens':0}
        replies=[ChatResponse(content='',tool_calls=[ToolCall(id='r',name='ticket_operations__tickets',arguments={})]),
            ChatResponse(content='',tool_calls=[ToolCall(id='b',name='get_ticket',arguments={'ticket_id':'TCK-1001'})]),
            ChatResponse(content='TCK-1001 is Open.',tool_calls=[])]
        async def chat(self,*a,**kw):
            self.usage['num_calls']+=1;self.metered_calls+=1
            self.usage['prompt_tokens']+=10;self.usage['completion_tokens']+=5
            return self.replies.pop(0)
        async def aclose(self):pass
    async def run():
        await original.aclose();owner.http=Client()
        try:
            if scope=='all_model_tools':
                with pytest.raises(RuntimeError,match='Reference tool-call budget exhausted'):
                    await owner.run('Look up TCK-1001.',[])
                return None
            return await owner.run('Look up TCK-1001.',[])
        finally:await owner.aclose()
    result=asyncio.run(run())
    if scope=='all_model_tools':
        assert result is None and owner.model_calls==2
        assert owner.tool_calls[0]['executed']is False
    else:
        assert result.content=='TCK-1001 is Open.'
        assert owner.turn_tool_attempts==1 and owner.model_calls==3
        assert owner.tool_calls[0]['executed']and owner.tool_calls[0]['ok']


def test_internal_timeout_is_preserved_as_failure_not_a_global_budget_expiry(tmp_path,monkeypatch):
    monkeypatch.setattr(assessment,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['harness_assessment.py','run','--full-itsm','--tag','timeout-check'])
    async def fail(*args):raise TimeoutError('model startup timed out')
    monkeypatch.setattr(assessment,'run',fail)
    with pytest.raises(TimeoutError,match='model startup timed out'):assessment.main()


def test_global_budget_expiry_preserves_partial_status_and_cleanup(tmp_path,monkeypatch):
    monkeypatch.setattr(assessment,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['harness_assessment.py','run','--full-itsm','--tag','budget-check','--max-hours','0.000001'])
    async def wait(args,folder):
        state={'completed':3,'total':132}
        try:await asyncio.sleep(1)
        finally:
            state['resources_released']=True
            assessment.write(folder/'status.json',state)
    monkeypatch.setattr(assessment,'run',wait)
    assessment.main()
    state=json.loads((tmp_path/'artifacts/harness_runs/budget-check/status.json').read_text())
    assert state['status']=='time_budget_complete_review_required'
    assert state['completed']==3 and state['resources_released']is True


def timeout_row(task,arm,kind):
    result=row(task,arm,None)
    result['trace'].update(error='AgentTimeout: step exceeded 180s'if kind=='agent_timeout'else'SimulatorFidelityError: invalid customer turn',
        error_kind=kind,timed_out=kind=='agent_timeout')
    return result


def test_agent_deadline_is_local_failure_and_native_trace_remains_unchanged():
    task=load_tasks(str(comparison.UPSTREAM/'src/tasks/itsm.yaml'))[0]
    original=timeout_row(task,'deep_harness','agent_timeout');snapshot=copy.deepcopy(original)
    result=comparison.classify_row(original)
    assert result['local_verdict']is False and result['official_verdict']is None
    assert result['evaluation_infrastructure_error']is False
    assert result['trace']==snapshot['trace']and original==snapshot


def test_mixed_simulator_error_and_two_agent_timeouts_do_not_abort_catalogue(tmp_path):
    tasks=load_tasks(str(comparison.UPSTREAM/'src/tasks/itsm.yaml'))[:2]
    protocol={'task_ids':[task.id for task in tasks],'expected_trials':4,'scope':'test','version':'test'}
    async def execute(task,arm,*unused):
        if task.id==tasks[0].id and arm=='official_graph':return timeout_row(task,arm,'user_simulator_divergence')
        if arm=='deep_harness':return timeout_row(task,arm,'agent_timeout')
        return row(task,arm,True)
    asyncio.run(comparison.run_comparison(args(),tmp_path,{},lambda *a,**k:None,protocol,execute))
    result=json.loads((tmp_path/'summary.json').read_text())
    assert result['complete']and result['completed_trials']==4
    assert result['arms']['deep_harness']['unknown']==0
    assert result['arms']['deep_harness']['official_unknown']==2
    assert result['arms']['deep_harness']['known_harness_failures']==2


def test_three_actual_evaluation_failures_still_stop_and_preserve_rows(tmp_path):
    tasks=load_tasks(str(comparison.UPSTREAM/'src/tasks/itsm.yaml'))[:2]
    protocol={'task_ids':[task.id for task in tasks],'expected_trials':4,'scope':'test','version':'test'}
    async def execute(task,arm,*unused):return timeout_row(task,arm,'user_simulator_divergence')
    with pytest.raises(RuntimeError,match='Three consecutive evaluation-infrastructure'):
        asyncio.run(comparison.run_comparison(args(),tmp_path,{},lambda *a,**k:None,protocol,execute))
    assert json.loads((tmp_path/'summary.json').read_text())['completed_trials']==3


@pytest.fixture
def continuation(tmp_path,monkeypatch):
    current_source=__import__('pathlib').Path(comparison.__file__).read_text()
    source=tmp_path/'project';parent=tmp_path/'runs/parent';child=tmp_path/'runs/child'
    child.mkdir(parents=True)
    files={'kb/harness/engine.py':'actor unchanged',
        'scripts/harness_comparison.py':current_source,
        'scripts/harness_assessment.py':'runner bookkeeping'}
    manifest={}
    for name,text in files.items():
        actual=source/name;actual.parent.mkdir(parents=True,exist_ok=True);actual.write_text(text)
        frozen=parent/'source'/name;frozen.parent.mkdir(parents=True,exist_ok=True);frozen.write_text(text)
        manifest[name]=hashlib.sha256(frozen.read_bytes()).hexdigest()
    (source/'scripts/harness_assessment.py').write_text('new runner bookkeeping')
    monkeypatch.setattr(comparison,'__file__',str(source/'scripts/harness_comparison.py'))
    comparison.write(parent/'source-manifest.json',manifest)
    comparison.write(parent/'status.json',{'pid':-1,'start_ticks':'not-alive','resources_released':True})
    tasks=load_tasks(str(comparison.UPSTREAM/'src/tasks/itsm.yaml'))[:2]
    protocol={'task_ids':[task.id for task in tasks],'expected_trials':4,'scope':'test','version':'test',
        'arms':list(comparison.ARMS),'context_tokens':8192}
    comparison.write(parent/'comparison-protocol.json',protocol)
    records=[row(tasks[0],'official_graph',True),row(tasks[0],'deep_harness',False),
        timeout_row(tasks[1],'deep_harness','agent_timeout')]
    for record in records:
        comparison.write(parent/'rows'/(record['task_id']+'__'+record['arm']+'.json'),record)
        comparison.write(parent/'paired_openings'/(record['task_id']+'.json'),
            {'message':'Please look up this ticket.','provider_calls':2,'fingerprint':'fixture','source_arm':'official_graph'})
    return SimpleNamespace(parent=parent,child=child,source=source,tasks=tasks,protocol=protocol,records=records)


def test_resume_imports_partial_pair_and_executes_only_the_missing_arm(continuation):
    c=continuation
    c.protocol['continuation']=comparison.inherit_results(c.child,c.protocol,c.parent)
    calls=[]
    async def execute(task,arm,*unused):calls.append((task.id,arm));return row(task,arm,True)
    asyncio.run(comparison.run_comparison(args(),c.child,{},lambda *a,**k:None,c.protocol,execute))
    assert calls==[(c.tasks[1].id,'official_graph')]
    result=json.loads((c.child/'summary.json').read_text())
    assert result['complete']and result['completed_trials']==4
    assert result['continuation']['inherited_trials']==3
    assert result['continuation']['pending_trials']==1
    assert result['arms']['deep_harness']['known_harness_failures']==1
    inherited=json.loads((c.child/'rows'/(c.tasks[1].id+'__deep_harness.json')).read_text())
    assert inherited['official_verdict']is None and inherited['local_verdict']is False
    assert inherited['trace']==c.records[-1]['trace']
    assert json.loads((c.parent/'rows'/(c.tasks[1].id+'__deep_harness.json')).read_text())['local_verdict']is None
    assert (c.child/'paired_openings'/(c.tasks[1].id+'.json')).read_bytes()==(c.parent/'paired_openings'/(c.tasks[1].id+'.json')).read_bytes()


@pytest.mark.parametrize('change',['actor','protocol','evaluator_inside_runner'])
def test_resume_rejects_changed_actor_evaluator_or_budget(continuation,change):
    c=continuation
    if change=='actor':(c.source/'kb/harness/engine.py').write_text('different actor')
    elif change=='protocol':c.protocol['context_tokens']=16384
    else:
        p=c.source/'scripts/harness_comparison.py';p.write_text(p.read_text().replace('temperature=0,max_tokens=500','temperature=0,max_tokens=600'))
    with pytest.raises(RuntimeError,match='changed|mismatch'):
        comparison.inherit_results(c.child,c.protocol,c.parent)


def test_resume_launch_forwards_parent_tag(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(assessment,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['harness_assessment.py','launch','--full-itsm','--tag','child',
        '--resume-from','parent','--gpus','5,1'])
    sent=[]
    monkeypatch.setattr(assessment.subprocess,'Popen',lambda command,**kw:sent.append(command)or SimpleNamespace(pid=1))
    assessment.main()
    assert sent[0][sent[0].index('--resume-from')+1]=='parent'


def repair_fixture_sources(c):
    from pathlib import Path
    real=Path(__file__).resolve().parents[1]
    manifest=json.loads((c.parent/'source-manifest.json').read_text())
    for name in comparison.FIDELITY_REPAIR_FILES:
        text=(real/name).read_text()
        for root in (c.source,c.parent/'source'):
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        manifest[name]=hashlib.sha256(text.encode()).hexdigest()
    comparison.write(c.parent/'source-manifest.json',manifest)
    p=c.source/'kb/benchmark/customer_contract.py'
    p.write_text(p.read_text().replace('upstream-customer-faithfulness-compact-v2',
        'upstream-customer-faithfulness-compact-repaired'))


def test_fidelity_repair_retains_success_failure_and_timeout_but_retries_evaluator_errors(continuation):
    c=continuation;repair_fixture_sources(c)
    failed=timeout_row(c.tasks[1],'official_graph','user_simulator_divergence')
    comparison.write(c.parent/'rows'/(c.tasks[1].id+'__official_graph.json'),failed)
    before={p.name:p.read_bytes()for p in (c.parent/'rows').glob('*.json')}
    plan=comparison.inherit_results(c.child,c.protocol,c.parent,evaluator_repair=True)
    assert plan['inherited_trials']==3 and plan['pending_trials']==1
    assert len(plan['skipped_evaluation_errors'])==1
    inherited=[json.loads(p.read_text())for p in (c.child/'rows').glob('*.json')]
    assert sorted(row['local_verdict']for row in inherited)==[False,False,True]
    assert any(row['trace'].get('error_kind')=='agent_timeout'for row in inherited)
    assert plan['actor_and_response_scoring_unchanged']is True
    assert {p.name:p.read_bytes()for p in (c.parent/'rows').glob('*.json')}==before


@pytest.mark.parametrize('change',['actor','scoring','budget'])
def test_fidelity_repair_still_rejects_actor_grader_or_budget_changes(continuation,change):
    c=continuation;repair_fixture_sources(c)
    if change=='actor':(c.source/'kb/harness/engine.py').write_text('different actor')
    elif change=='scoring':
        p=c.source/'kb/benchmark/customer_contract.py'
        p.write_text(p.read_text().replace('TRUSTED RUBRIC INTERPRETATION','Different scoring instructions'))
    else:c.protocol['context_tokens']=16384
    with pytest.raises(RuntimeError,match='changed|mismatch|scoring'):
        comparison.inherit_results(c.child,c.protocol,c.parent,evaluator_repair=True)


def test_fidelity_repair_launch_requires_parent_and_forwards_explicit_flag(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(assessment,'ROOT',tmp_path)
    sent=[]
    monkeypatch.setattr(assessment.subprocess,'Popen',lambda command,**kw:sent.append(command)or SimpleNamespace(pid=1))
    monkeypatch.setattr(sys,'argv',['harness_assessment.py','launch','--full-itsm','--tag','repaired',
        '--resume-from','parent','--repair-evaluator'])
    assessment.main();assert '--repair-evaluator'in sent[0]
    monkeypatch.setattr(sys,'argv',['harness_assessment.py','launch','--full-itsm','--tag','invalid','--repair-evaluator'])
    with pytest.raises(SystemExit):assessment.main()


@pytest.mark.parametrize('inherited_task',[False,True])
def test_new_exchange_guards_only_apply_to_previously_unrun_tasks(continuation,inherited_task):
    c=continuation;repair_fixture_sources(c)
    task_id=c.tasks[0].id if inherited_task else'unrun-conditional-task'
    comparison.write(c.source/'kb/benchmark/customer_exchanges_dev16.json',
        {'tasks':[{'task_id':task_id,'requirement':'Explicit customer reply after service warning.'}]})
    if inherited_task:
        with pytest.raises(RuntimeError,match='exchange rules changed for an inherited task'):
            comparison.inherit_results(c.child,c.protocol,c.parent,evaluator_repair=True)
    else:
        plan=comparison.inherit_results(c.child,c.protocol,c.parent,evaluator_repair=True)
        assert plan['inherited_trials']==3


def test_report_separates_new_inference_from_inherited_and_cached_actor_cost(tmp_path):
    tasks=load_tasks(str(comparison.UPSTREAM/'src/tasks/itsm.yaml'))[:2]
    fresh=row(tasks[0],'official_graph',True);fresh['reference_mode']='fresh'
    inherited=row(tasks[0],'deep_harness',True);inherited['inherited_from']={'run':'previous'}
    cached=row(tasks[1],'official_graph',True);cached['reference_mode']='cached_regraded'
    result=comparison.update_report(tmp_path,{'task_ids':[task.id for task in tasks],
        'scope':'test','version':'test'},[fresh,inherited,cached])
    assert result['new_actor_tokens']==15
    assert result['inherited_actor_tokens']==15
    assert result['cached_reference_actor_tokens']==15
