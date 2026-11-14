import asyncio
from types import SimpleNamespace

import pytest

import crm_benchmark as runner
from kb.benchmark.crm_metrics import report_markdown, summarize
from test_crm_benchmark import row


def test_time_limit_keeps_cleanup_and_marks_partial_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'RUN_TRIALS', 2)
    runner.write(tmp_path/'protocol.json', {'task_ids': ['case'], 'trials': 2,
        'arms': ['single', 'multi_v5'], 'expected_trials': 4})
    saved = row('single', 0, True)
    runner.write(tmp_path/'rows'/'saved.json', saved)
    async def operation():
        try:
            await asyncio.Event().wait()
        finally:
            runner.write(tmp_path/'status.json', {'status': 'cancelled', 'resources_released': True})
    asyncio.run(runner.run_with_budget(tmp_path, operation, .00001))
    state = runner.read(tmp_path/'status.json')
    assert state['status'] == 'time_budget_complete_review_required'
    assert state['resources_released'] and state['completed'] == 1 and state['total'] == 4
    assert runner.read(tmp_path/'rows'/'saved.json') == saved
    assert not runner.read(tmp_path/'summary.json')['complete']
    assert runner.read(tmp_path/'budget.json')['elapsed_seconds'] < 5


def test_success_finishes_early_and_real_timeout_is_not_hidden(tmp_path):
    async def done():
        runner.write(tmp_path/'status.json', {'status': 'complete_review_required', 'resources_released': True})
        return 'done'
    assert asyncio.run(runner.run_with_budget(tmp_path, done, 10)) == 'done'
    assert runner.read(tmp_path/'status.json')['status'] == 'complete_review_required'
    async def failure():
        raise TimeoutError('model readiness failed')
    with pytest.raises(TimeoutError, match='model readiness failed'):
        asyncio.run(runner.run_with_budget(tmp_path, failure, 10))


def test_manual_cancel_stays_cancelled(tmp_path):
    async def check():
        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                runner.write(tmp_path/'status.json', {'status': 'cancelled', 'resources_released': True})
        task = asyncio.create_task(runner.run_with_budget(tmp_path, operation, 10))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(check())
    assert runner.read(tmp_path/'status.json')['status'] == 'cancelled'


def test_two_repeat_report_is_not_labelled_three_repeats():
    rows = [row(arm, repeat, True) for arm in ('single', 'multi_v5') for repeat in range(2)]
    summary = summarize(rows, ['case'], trials=2)
    assert summary['complete'] and summary['expected_trials'] == 4
    assert summary['arms']['multi_v5']['all_trials_success'] == 1
    assert summary['arms']['multi_v5']['all_three_success'] is None
    report = report_markdown(summary)
    assert '2次全部成功' in report and '三次全部成功' not in report


def test_launch_preserves_version_repeat_and_wall_limit(tmp_path, monkeypatch):
    for name in ('ARMS', 'HARNESS_VERSION', 'RUN_TRIALS', 'MAX_RUN_HOURS'):
        monkeypatch.setattr(runner, name, getattr(runner, name))
    monkeypatch.setattr(runner, 'OUTPUT', tmp_path)
    monkeypatch.setattr(runner.sys, 'argv', ['crm_benchmark.py', 'launch', '--tag', 'overnight-test',
        '--harness-version', 'v6', '--gpus', '1,7', '--trials', '2', '--max-hours', '10'])
    commands = []
    def launch(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(pid=99999999)
    monkeypatch.setattr(runner.subprocess, 'Popen', launch)
    runner.main()
    command = commands[0]
    for flag, value in [('--harness-version', 'v6'), ('--trials', '2'), ('--max-hours', '10.0'), ('--gpus', '1,7')]:
        assert command[command.index(flag)+1] == value


def test_supervisor_launch_keeps_small_task_set_and_multi_only_arm(tmp_path, monkeypatch):
    for name in ('ARMS', 'HARNESS_VERSION', 'RUN_TRIALS', 'MAX_RUN_HOURS', 'EXECUTION_ADAPTER', 'REQUESTED_TASK_IDS'):
        monkeypatch.setattr(runner, name, getattr(runner, name))
    monkeypatch.setattr(runner, 'OUTPUT', tmp_path)
    tasks = 'itsm_ops_001_lookup_ticket_by_id,itsm_ops_005_add_comment_to_ticket'
    monkeypatch.setattr(runner.sys, 'argv', ['crm_benchmark.py', 'launch', '--tag', 'supervisor-small',
        '--harness-version', 'v6', '--execution-adapter', 'v4', '--arms', 'multi_v6',
        '--task-ids', tasks, '--trials', '1', '--max-hours', '.3'])
    commands = []
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda command, **kwargs:
        commands.append(command) or SimpleNamespace(pid=99999999))
    runner.main()
    command = commands[0]
    for flag, value in [('--execution-adapter', 'v4'), ('--arms', 'multi_v6'), ('--task-ids', tasks), ('--trials', '1')]:
        assert command[command.index(flag)+1] == value


def test_multi_only_report_has_correct_denominator_and_no_paired_claim():
    summary = summarize([row('multi_v6', 0, True)], ['case'], trials=1, arms=('multi_v6',))
    assert summary['complete'] and summary['expected_trials'] == 1
    assert summary['arms']['multi_v6']['passed'] == 1
    assert summary['paired_available'] is False
    assert '无配对对照' in report_markdown(summary)


@pytest.mark.parametrize('message', ['Supervisor model-call budget exhausted',
    'Supervisor tool-call budget exhausted', 'Supervisor context budget exhausted'])
def test_supervisor_budget_failures_stay_in_failure_denominator(message):
    from kb.benchmark.crm_metrics import local_verdict
    assert local_verdict(None, 'RuntimeError: '+message) is False


def test_single_gpu_development_selects_one_card_and_reuses_only_the_lease():
    stats = {'2': {'used_mib': 1, 'free_mib': 24126, 'total_mib': 24576, 'utilization': 0},
             '3': {'used_mib': 14672, 'free_mib': 9456, 'total_mib': 24576, 'utilization': 20}}
    assert runner.select_gpus(stats, ['2', '3'], {'actor': .78}, True) == ['2']
    assert runner.select_gpus(stats, ['2', '3'], {'actor': .78, 'auxiliary': .75}) == []
    leases = SimpleNamespace(owned={'actor': {'url': 'http://actor'}, 'auxiliary': {'url': 'http://judge'}})
    assert runner.evaluation_service({'shared_evaluation_model': True}, leases) is leases.owned['actor']
    assert runner.evaluation_service({}, leases) is leases.owned['auxiliary']


def test_shared_model_protocol_discloses_dependencies_and_four_task_limit(tmp_path, monkeypatch):
    tasks = ('itsm_ops_001_lookup_ticket_by_id', 'itsm_ops_005_add_comment_to_ticket',
             'itsm_ops_012_comment_then_status_update_order', 'itsm_safety_007_safe_delete_admin_ticket1004')
    for name, value in {'SHARED_EVAL_MODEL': True, 'HARNESS_VERSION': 'v6', 'EXECUTION_ADAPTER': 'v4',
                        'REQUESTED_TASK_IDS': tasks, 'ARMS': ('multi_v6',), 'RUN_TRIALS': 1,
                        'MAX_RUN_HOURS': .4}.items():
        monkeypatch.setattr(runner, name, value)
    protocol = asyncio.run(runner.preflight(tmp_path, '2'))
    assert protocol['expected_trials'] == 4
    assert protocol['task_ids'] == list(tasks)
    assert protocol['shared_evaluation_model'] is True
    assert protocol['independent_actor_and_judge_weights'] is False
    assert protocol['actor'] == protocol['user_model'] == protocol['judge_model']
    assert protocol['gpu_memory_fractions'] == {'actor': .78}
    assert protocol['simulator_verifier'] == 'upstream'
    summary = runner.update_report(tmp_path, protocol)
    assert summary['evaluation']['shared_evaluation_model'] is True
    report = (tmp_path/'report.md').read_text()
    assert '非独立评测' in report and 'Qwen3-VL-8B' not in report


def test_shared_model_launch_preserves_flag_and_does_not_expand_tasks(tmp_path, monkeypatch):
    for name in ('ARMS', 'HARNESS_VERSION', 'RUN_TRIALS', 'MAX_RUN_HOURS', 'EXECUTION_ADAPTER',
                 'REQUESTED_TASK_IDS', 'SHARED_EVAL_MODEL'):
        monkeypatch.setattr(runner, name, getattr(runner, name))
    monkeypatch.setattr(runner, 'OUTPUT', tmp_path)
    monkeypatch.setattr(runner.sys, 'argv', ['crm_benchmark.py', 'launch', '--tag', 'single-gpu-check',
        '--harness-version', 'v6', '--execution-adapter', 'v4', '--arms', 'multi_v6',
        '--shared-eval-model', '--task-ids', 'itsm_ops_001_lookup_ticket_by_id', '--trials', '1', '--max-hours', '.4'])
    commands = []
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda command, **kwargs:
        commands.append(command) or SimpleNamespace(pid=99999999))
    runner.main()
    assert '--shared-eval-model' in commands[0]
    assert commands[0][commands[0].index('--task-ids')+1] == 'itsm_ops_001_lookup_ticket_by_id'


def test_shared_model_rejects_unbounded_default_task_set(tmp_path, monkeypatch):
    for name in ('ARMS', 'HARNESS_VERSION', 'RUN_TRIALS', 'MAX_RUN_HOURS', 'EXECUTION_ADAPTER',
                 'REQUESTED_TASK_IDS', 'SHARED_EVAL_MODEL'):
        monkeypatch.setattr(runner, name, getattr(runner, name))
    monkeypatch.setattr(runner.sys, 'argv', ['crm_benchmark.py', 'launch', '--tag', 'invalid-shared',
        '--harness-version', 'v6', '--execution-adapter', 'v4', '--shared-eval-model',
        '--trials', '1', '--max-hours', '.4'])
    with pytest.raises(SystemExit) as failure:
        runner.main()
    assert failure.value.code == 2
