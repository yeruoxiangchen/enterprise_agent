import asyncio
from types import SimpleNamespace

import pytest

import crm_benchmark as runner


VALUES = dict(HARNESS_VERSION='v6', EXECUTION_ADAPTER='v4',
              ARMS=('official_graph','multi_v6'), RUN_TRIALS=1, MAX_RUN_HOURS=6.,
              REQUESTED_TASK_IDS=(), SHARED_EVAL_MODEL=True, USER_STRATEGY='contextual-react',
              FULL_ITSM=True)


def configure(monkeypatch):
    for key, value in VALUES.items():
        monkeypatch.setattr(runner, key, value)


def command():
    return ['crm_benchmark.py','launch','--tag','full-itsm-test','--harness-version','v6',
            '--execution-adapter','v4','--arms','official_graph','multi_v6','--trials','1',
            '--shared-eval-model','--full-itsm','--user-strategy','contextual-react','--max-hours','6']


def test_full_catalogue_freezes_66_tasks_132_trials_and_matched_long_dialogue_budget(tmp_path, monkeypatch):
    configure(monkeypatch)
    protocol = asyncio.run(runner.preflight(tmp_path, '3'))
    assert len(protocol['task_ids']) == len(set(protocol['task_ids'])) == 66
    assert protocol['expected_trials'] == 132 and protocol['trials'] == 1
    assert protocol['arms'] == ['official_graph','multi_v6']
    assert protocol['diagnostic_subset'] is False and protocol['full_itsm'] is True
    assert protocol['max_turns'] == 30 and protocol['task_timeout_seconds'] == 600
    assert protocol['model_calls_per_turn'] == protocol['reference']['model_calls_per_turn'] == 24
    assert protocol['tool_calls_per_turn'] == protocol['reference']['tool_calls_per_turn'] == 30
    assert protocol['skip_duplicate_integration_trials'] is True
    assert protocol['user_strategy'] == 'contextual-react'
    assert protocol['independent_actor_and_judge_weights'] is False
    assert (tmp_path/'source_manifest.json').exists()


def test_full_catalogue_flag_and_two_arms_reach_the_background_process(tmp_path, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(runner, 'OUTPUT', tmp_path)
    monkeypatch.setattr(runner.sys, 'argv', command())
    launched = []
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda args, **kwargs:
                        launched.append(args) or SimpleNamespace(pid=99999999))
    runner.main()
    args = launched[0]
    assert '--full-itsm' in args and '--shared-eval-model' in args
    assert '--task-ids' not in args
    assert args[args.index('--arms')+1:args.index('--arms')+3] == ['official_graph','multi_v6']
    assert args[args.index('--trials')+1] == '1'
    assert args[args.index('--max-hours')+1] == '6.0'


@pytest.mark.parametrize('extra', [
    ['--task-ids','itsm_ops_001_lookup_ticket_by_id'],
    ['--trials','2'],
    ['--arms','official_graph'],
    ['--arms','official_graph','single_v6'],
])
def test_full_catalogue_requires_the_declared_scope_and_two_requested_arms(monkeypatch, extra):
    configure(monkeypatch)
    monkeypatch.setattr(runner.sys, 'argv', command()+extra)
    with pytest.raises(SystemExit) as failure:
        runner.main()
    assert failure.value.code == 2
