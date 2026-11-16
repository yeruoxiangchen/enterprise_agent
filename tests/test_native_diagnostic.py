import json
from types import SimpleNamespace

from harness.bench_runner import ConversationTrace
from harness_native_check import report
from harness_native_cases import gate_ready
from harness_native_worker import deterministic_checks,task_for_case


def test_fixture_cannot_send_confirmation_before_dependency_observation():
    rows=[]
    adapter=SimpleNamespace(environment=SimpleNamespace(get_state=lambda:{'tickets':{'TCK-1002':{'status':'Open'}}}),
        engine=SimpleNamespace(journal=SimpleNamespace(records=lambda:rows)))
    assert not gate_ready('open_dependency_observed',adapter)
    rows.append({'name':'check_dependencies','arguments':{'ticket_id':'TCK-1002'},
                 'result':{'open_dependencies':['TCK-1004']}})
    assert gate_ready('open_dependency_observed',adapter)


def test_no_business_execution_does_not_pass_a_required_action():
    task=SimpleNamespace(expected_actions=['get_ticket'])
    checks=deterministic_checks(task,ConversationTrace(task_id='read'))
    assert checks=={'required_actions':False}


def test_hard_worker_failure_retains_unknown_cost_instead_of_zero(tmp_path):
    row={'arm':'native_deepagents','task_id':'read','passed':False,
         'error':'Worker killed after timeout','tokens':None,'model_calls':None,'seconds':360}
    result=report(tmp_path,[row])
    assert result['arms']['native_deepagents']['tokens']is None
    assert not result['arms']['native_deepagents']['usage_complete']
    assert result['arms']['native_deepagents']['errors']==1
    assert json.loads((tmp_path/'summary.json').read_text())['completed']==1


def test_every_public_and_cancellation_fixture_constructs_a_valid_task():
    from harness_native_cases import CASES,GUARD_CASES,EXTRA_CASES
    cases=[*CASES,*GUARD_CASES,*EXTRA_CASES]
    assert len(cases)==16 and len({case['task_id']for case in cases})==16
    for case in cases:
        task=task_for_case(case)
        assert task.id==case['task_id']and task.instruction


def test_status_followup_waits_for_the_actual_committed_comment():
    from harness_native_cases import expanded_gate_ready
    rows=[]
    instance=SimpleNamespace(engine=SimpleNamespace(journal=SimpleNamespace(records=lambda:rows)))
    assert not expanded_gate_ready('asset_tag_comment_logged',instance)
    row={'name':'add_ticket_comment','status':'refused','arguments':{
        'ticket_id':'TCK-1002','comment':'Asset tag received from procurement.'}}
    rows.append(row)
    assert not expanded_gate_ready('asset_tag_comment_logged',instance)
    row['status']='committed'
    assert expanded_gate_ready('asset_tag_comment_logged',instance)


def test_reused_tokens_are_separate_from_new_inference_cost(tmp_path,monkeypatch):
    import harness_native_check as control
    monkeypatch.setattr(control,'ARMS',('native_forked',))
    rows=[{'task_id':'old','arm':'native_forked','passed':True,'tokens':100,'model_calls':2,'seconds':5,'reused':True},
          {'task_id':'new','arm':'native_forked','passed':False,'tokens':40,'model_calls':1,'seconds':3}]
    item=control.report(tmp_path,rows)['arms']['native_forked']
    assert item['completed']==2 and item['passed']==1
    assert item['tokens']==140 and item['new_tokens']==40
    assert item['reused']==1 and item['new_executions']==1
