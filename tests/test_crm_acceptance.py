import asyncio
import copy
import hashlib
import json

import pytest

import crm_acceptance as acceptance
import crm_benchmark as benchmark
from kb.benchmark.crm_calibration import (NOTE, fixtures, generation_checks, load_history,
                                         summarize_calibration)
from harness.user_simulator import ReactUserSimulator, STOP_SIGNAL
from harness.models import Role
from test_crm_user_fidelity import Client, decision


def calibration(passed=True):
    generated = [{'passed': passed} for _ in range(4)]
    reviewed = [{'expected_valid': i < 4, 'predicted_valid': i < 4,
                 'correct': True, 'model_finish_reason': 'stop'} for i in range(8)]
    return summarize_calibration(generated, reviewed)


def test_fixture_contract_has_four_valid_and_four_invalid_cases():
    cases = fixtures()
    assert len(cases) == len({c['id'] for c in cases}) == 8
    assert sum(c['expected_valid'] for c in cases) == 4
    assert sum(bool(c.get('generation_rule')) for c in cases) == 4
    assert 'You want to log the\nexact note' in cases[0]['instruction']
    assert 'Only after that comment is logged' in cases[1]['instruction']


@pytest.mark.parametrize('rule,message,passed', [
    ('request_note', f'Please add "{NOTE}" to TCK-1001.', True),
    ('request_note', f'Please confirm "{NOTE}" has been added to TCK-1001.', False),
    ('next_phase', 'Now set its status to Resolved.', True),
    ('next_phase', STOP_SIGNAL, False),
    ('provide_identity', 'I am the admin.rlee.', True),
    ('provide_identity', 'What is my user ID?', False),
    ('stop_when_complete', STOP_SIGNAL, True),
    ('stop_when_complete', 'Both changes have been completed.', False),
])
def test_generation_checks_detect_observed_failure_modes(rule, message, passed):
    assert all(generation_checks(rule, message).values()) is passed


def test_generation_uses_reference_react_without_a_second_verifier():
    sample = next(c for c in fixtures() if c['id'] == 'provide_identity')
    client = Client([decision('My user ID is admin.rlee.')])
    sim = ReactUserSimulator(client, temperature=0)
    load_history(sim, sample)
    assert sim._messages[-1].role == Role.USER
    assert asyncio.run(sim.step('')) == 'My user ID is admin.rlee.'
    assert client.usage['num_calls'] == 1
    assert client.requests[0]['response_format']['json_schema']['name'] == 'benchmark_user_turn'


def test_calibration_reports_false_acceptances_and_rejections_separately():
    rows = [{'expected_valid': True, 'predicted_valid': False, 'correct': False},
            {'expected_valid': False, 'predicted_valid': True, 'correct': False}]
    result = summarize_calibration([], rows)
    assert result['verifier']['false_rejections'] == 1
    assert result['verifier']['false_acceptances'] == 1
    assert not result['generation']['gate_passed']
    assert not result['verifier']['eligible_for_optional_gating']


def test_public_gate_requires_generation_business_metering_and_explicit_request():
    good = [{'passed': True, 'usage_missing_calls': 0} for _ in range(4)]
    assert acceptance.public_gate(calibration(), good, True)
    assert not acceptance.public_gate(calibration(False), good, True)
    assert not acceptance.public_gate(calibration(), good[:3], True)
    assert not acceptance.public_gate(calibration(), good, False)
    broken = copy.deepcopy(good)
    broken[0]['usage_missing_calls'] = 1
    assert not acceptance.public_gate(calibration(), broken, True)
    broken[0].update(usage_missing_calls=0, passed=False)
    assert not acceptance.public_gate(calibration(), broken, True)


def test_reference_verifier_never_controls_default_react_customer_turns():
    result = calibration()
    result['verifier'].update(correct=0, false_rejections=4, false_acceptances=4)
    assert result['verifier']['used_to_gate_customer_turns'] is False
    assert acceptance.public_gate(result, [{'passed': True, 'usage_missing_calls': 0} for _ in range(4)], True)


def test_source_archive_keeps_bytes_after_original_changes(tmp_path, monkeypatch):
    project = tmp_path/'project'
    source = project/'kb/example.py'
    source.parent.mkdir(parents=True)
    source.write_text('value = 1\n')
    monkeypatch.setattr(acceptance, 'PROJECT', project)
    monkeypatch.setattr(benchmark, 'source_fingerprints', lambda: {
        str(source): hashlib.sha256(source.read_bytes()).hexdigest()})
    output = tmp_path/'output'
    acceptance.freeze_sources(output)
    source.write_text('value = 2\n')
    assert (output/'source/project/kb/example.py').read_text() == 'value = 1\n'
    assert str(source) in json.loads((output/'source_manifest.json').read_text())


def test_public_protocol_freezes_contextual_customer_strategy_and_exactly_four_tasks(tmp_path, monkeypatch):
    for key in ('HARNESS_VERSION', 'EXECUTION_ADAPTER', 'ARMS', 'RUN_TRIALS', 'SHARED_EVAL_MODEL',
                'USER_STRATEGY', 'REQUESTED_TASK_IDS', 'MAX_RUN_HOURS'):
        monkeypatch.setattr(benchmark, key, getattr(benchmark, key))
    runner = acceptance.configure_public_runner(24)
    protocol = asyncio.run(runner.preflight(tmp_path, '2'))
    assert protocol['user_strategy'] == 'contextual-react'
    assert protocol['user_output_complete'] is True
    assert protocol['simulator_version'] == 'contextual-react-customer-v1'
    assert protocol['paired_user_openings'] is False
    assert protocol['user_fidelity_validation'] is False
    assert protocol['simulator_verifier'] is None
    assert protocol['expected_trials'] == 4
    assert protocol['task_ids'] == list(acceptance.TASK_IDS)
    assert protocol['independent_actor_and_judge_weights'] is False


def test_failed_generation_gate_skips_public_tasks_and_releases_service(tmp_path, monkeypatch):
    released, fixed = [], []
    class Services:
        def __init__(self, *args): self.owned = {}
        def start(self, *args, **kwargs): self.owned['actor'] = {'url': 'unused', 'model': 'test'}
        async def ready(self): pass
        async def close(self): released.append(True)
    async def calibrate(*args): return calibration(False)
    async def business(*args):
        fixed.append(args[3])
        return {'passed': True, 'usage_missing_calls': 0}
    async def forbidden(*args): raise AssertionError('Public task must not start when calibration fails')
    monkeypatch.setattr(acceptance, 'ModelServices', Services)
    monkeypatch.setattr(acceptance, 'freeze_sources', lambda output: {})
    monkeypatch.setattr(acceptance, 'gpu_snapshot', lambda: {'2': {
        'free_mib': 24000, 'total_mib': 24576, 'used_mib': 1}})
    monkeypatch.setattr('kb.benchmark.crm_calibration.calibrate', calibrate)
    monkeypatch.setattr(acceptance, 'business_case', business)
    monkeypatch.setattr(benchmark, 'evaluate_trial', forbidden)
    asyncio.run(acceptance.run(tmp_path, '2', 1, True))
    summary = json.loads((tmp_path/'summary.json').read_text())
    state = json.loads((tmp_path/'status.json').read_text())
    assert len(fixed) == 4 and released == [True]
    assert summary['public_disposition'] == 'skipped_gate_not_met'
    assert state['resources_released'] is True
    assert state['fixed_completed'] == state['fixed_total'] == state['fixed_passed'] == 4
    assert not (tmp_path/'public4').exists()


def test_truncated_positive_reviews_remain_ineligible_for_customer_gating():
    result = calibration()
    reviews = result['verifier_rows']
    reviews[0]['model_finish_reason'] = 'length'
    actual = summarize_calibration(result['generation_rows'], reviews)
    assert actual['verifier']['correct'] == 8
    assert actual['verifier']['truncated'] == 1
    assert actual['verifier']['eligible_for_optional_gating'] is False
