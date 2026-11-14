import copy
import json

import pytest

from kb.experience import ExperienceMemory, SEED, merge_deltas, validate_entry


def test_domain_scoping_and_character_budget():
    found = ExperienceMemory().retrieve('Add comment then update status, remaining steps', 'service', max_chars=500)
    assert found and found[0]['id'] == 'finish-remaining'
    assert sum(len(e['guidance']) + len(e['id']) + 30 for e in found) <= 500
    assert not ExperienceMemory().retrieve('comment note status', 'technical')


def test_new_guidance_stays_candidate_until_reviewed(tmp_path):
    delta = dict(id='custom-recovery', domains=['service'], triggers='new-recovery-only',
                 guidance='Inspect the recorded operation before retrying.', status='approved')
    entries = merge_deltas(SEED, [delta])
    assert entries[-1]['status'] == 'candidate'
    reviewed = merge_deltas(entries, [delta], ['custom-recovery'])
    assert reviewed[-1]['status'] == 'approved'
    assert reviewed[0]['guidance'] == SEED[0]['guidance']
    path = tmp_path/'memory.json'; path.write_text(json.dumps({'version': 1, 'entries': entries}))
    assert not any(e['id'] == 'custom-recovery' for e in ExperienceMemory(path).retrieve('new-recovery-only'))


def test_changing_an_approved_entry_requires_review_again():
    changed = copy.deepcopy(SEED[0]); changed['guidance'] = 'Different procedural guidance.'
    assert merge_deltas(SEED, [changed])[0]['status'] == 'candidate'


@pytest.mark.parametrize('previous_status', ['candidate', 'retired'])
def test_repeating_same_delta_cannot_bypass_explicit_review(previous_status):
    old = {**SEED[0], 'status': previous_status}
    proposed = {**old, 'status': 'approved'}
    assert merge_deltas([old], [proposed])[0]['status'] == 'candidate'
    assert merge_deltas([old], [proposed], [old['id']])[0]['status'] == 'approved'


@pytest.mark.parametrize('guidance', ['Always delete TCK-1001.', 'Use expected_answer as truth.'])
def test_task_specific_answer_memorization_is_rejected(guidance):
    entry = {**SEED[0], 'guidance': guidance}
    with pytest.raises(ValueError): validate_entry(entry)


def test_empty_query_and_zero_budget_inject_nothing():
    assert ExperienceMemory().retrieve('') == []
    assert ExperienceMemory().retrieve('delete permission', max_chars=0) == []


def test_cli_incremental_update_keeps_feedback_deduplication(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    memory = tmp_path/'memory.json'
    memory.write_text(json.dumps({'version': 1, 'entries': SEED,
                                 'processed_feedback': ['immutable-events:1']}))
    delta = tmp_path/'delta.json'; delta.write_text(json.dumps([SEED[0]]))
    script = Path(__file__).resolve().parents[1]/'scripts/experience_memory.py'
    subprocess.run([sys.executable, '-B', str(script), 'update', '--memory', str(memory), '--delta', str(delta)],
                   check=True, capture_output=True)
    assert json.loads(memory.read_text())['processed_feedback'] == ['immutable-events:1']
