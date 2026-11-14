from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from business_assessment_cases import CASES
from business_assessment_metrics import counts, conjunction, summarize, turn_success
from business_assessment_observe import observation
from business_assessment_faults import state_cases


def sample(arm='single', turn=0, calls=2, passed=True):
    return {'key': f'{arm}/case/{turn}', 'calls': calls, 'elapsed': 1.,
            'quality': {'execution': passed, 'permission_checks': True, 'no_auto_submission': True},
            'business_state_ok': True,
            'observation': {'usage': {'complete': True, 'prompt_tokens': 10, 'completion_tokens': 2}}}


def test_unknowns_and_missing_turns_cannot_become_success():
    cases = [{'id': 'case', 'category': 'multi_turn', 'turns': [{'context_rubric': 'retain'}, {}]}]
    row = sample()
    result = summarize(cases, ['single'], [row], [{'key': row['key'], 'correctness': 1, 'context_retention': 1}])
    arm = result['arms']['single']
    assert arm['execution'] == {'passed': 1, 'failed': 0, 'unknown': 1, 'expected': 2, 'pass_fraction_of_expected': .5}
    assert arm['conversation_task_success']['passed'] == 0
    assert arm['conversation_task_success']['unknown'] == 1
    assert arm['prompt_tokens'] is None
    assert not result['semantic_assessment_complete']


def test_known_failure_overrides_unknown_judgment_and_retains_cost():
    cases = [{'id': 'case', 'category': 'cross_domain', 'turns': [{}, {}]}]
    rows = [sample(turn=0, calls=3, passed=False), sample(turn=1, calls=2)]
    result = summarize(cases, ['single'], rows, [])['arms']['single']
    assert result['conversation_task_success']['failed'] == 1
    assert result['model_calls_all_attempts'] == 5
    assert result['calls_per_successful_conversation'] is None
    assert turn_success(rows[0], {}) == 0


def test_full_conversation_pairing_and_cost_include_failed_attempts():
    cases = [{'id': 'case', 'category': 'cross_domain', 'turns': [{}]}]
    rows = [sample('single'), sample('multi_v4', calls=3)]
    grades = [{'key': rows[0]['key'], 'correctness': 0}, {'key': rows[1]['key'], 'correctness': 1}]
    result = summarize(cases, ['single', 'multi_v4'], rows, grades)
    assert result['paired_conversation_outcomes']['win']['count'] == 1
    assert result['arms']['multi_v4']['calls_per_successful_conversation'] == 3
    assert result['arms']['single']['conversation_task_success']['failed'] == 1
    with pytest.raises(ValueError):
        summarize(cases, ['single'], [rows[0], rows[0]], grades)


def test_business_write_failure_blocks_semantically_correct_answer():
    row = sample()
    row['business_state_ok'] = False
    assert turn_success(row, {'correctness': 1}) == 0
    assert conjunction([]) is None
    assert counts([True, None, False])['expected'] == 3


def test_authored_cases_have_unique_ids_and_review_rubrics():
    assert len(CASES) == len({c['id'] for c in CASES}) == 12
    assert all(t['rubric'] and t['question'] for c in CASES for t in c['turns'])
    assert any(len(c['turns']) >= 3 for c in CASES)


def test_usage_and_queue_are_taken_from_actual_callback_events():
    import json
    db = Mock()
    db.one.return_value = {'created_at': 10}
    events = [('assessment_execution_start', {'unix': 13}),
              ('assessment_model_input', {'call_id': 'a', 'messages': []}),
              ('assessment_model_end', {'call_id': 'a', 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}),
              ('assessment_execution_end', {'unix': 18})]
    db.all.return_value = [{'kind': k, 'payload': json.dumps(v), 'created_at': 0} for k, v in events]
    result = observation(db, 'run')
    assert result['queue_seconds'] == 3 and result['execution_seconds'] == 5
    assert result['usage']['complete'] and result['usage']['prompt_tokens'] == 10
    db.all.return_value.pop(2)
    assert not observation(db, 'run')['usage']['complete']


def test_actual_db_replay_races_and_state_guards(tmp_path, monkeypatch):
    from dataclasses import replace
    import business_assessment_faults as faults
    import business_ops as old
    original = old.cfg
    # No GPU/reranker is needed to verify transaction and state invariants.
    monkeypatch.setattr(old, 'cfg', lambda root: replace(original(root), mode='extractive', reranker_path=''))
    result = state_cases(tmp_path/'owned-state')
    assert all(r['passed'] for r in result.values())
    assert result['duplicate_draft_and_confirmation']['unique_drafts'] == 1
    assert result['duplicate_draft_and_confirmation']['confirmation_events'] == 1


@pytest.mark.parametrize('kind,expected_calls', [('model_timeout', 1), ('invalid_schema', 2)])
def test_injected_model_failures_finish_through_real_harness(tmp_path, monkeypatch, kind, expected_calls):
    from dataclasses import replace
    from types import SimpleNamespace
    import json
    import business_ops as old
    import business_v3_ops as ops
    from business_assessment_faults import create_run, fault_worker
    original = old.cfg
    monkeypatch.setattr(old, 'cfg', lambda root: replace(original(root), mode='extractive', reranker_path=''))
    target = tmp_path/kind
    cfg, db = ops.prepare(target, True)
    run_id, _ = create_run(db, '按演示差旅制度，住宿报销上限是多少？', kind)
    fault_worker(SimpleNamespace(data_dir=target, run_id=run_id, phase='first', fault=kind))
    row = db.one('SELECT status,state FROM runs WHERE id=?', (run_id,))
    assert row['status'] == 'error'
    assert json.loads(row['state'])['business']['calls'] == expected_calls


def test_service_cleanup_cannot_signal_an_unrelated_command(monkeypatch):
    from business_assessment_services import Services
    import business_assessment_services as services
    x = object.__new__(Services)
    x.owned = {'answer': {'pid': 123, 'start_ticks': 'tick', 'model': 'expected-model'}}
    monkeypatch.setattr(services.v2, 'ticks', lambda pid: 'tick')
    monkeypatch.setattr(Path, 'read_bytes', lambda path: b'unrelated-process')
    kill = Mock()
    monkeypatch.setattr(services.os, 'killpg', kill)
    with pytest.raises(RuntimeError, match='no longer matches'):
        x.stop('answer')
    kill.assert_not_called()


def test_saved_protocol_matches_after_json_roundtrip(tmp_path, monkeypatch):
    import business_assessment as assessment
    from kb.benchmark import semantic
    monkeypatch.setattr(assessment, 'source_hashes', lambda: {})
    monkeypatch.setattr(semantic, 'model_identity', lambda *args: {'identity': 'fixed'})
    first = assessment.freeze(tmp_path/'protocol')
    assert assessment.freeze(tmp_path/'protocol') == first


def test_final_load_report_reads_timing_after_terminal_status(system):
    from business_assessment_audit import finalized_load
    from business_assessment_observe import observation
    from kb.db import uid
    cfg, db, _, _, _, _, member = system
    cid, rid = uid(), uid()
    db.execute('INSERT INTO conversations VALUES(?,?,?,?)', (cid, member['id'], 'timing', 5.))
    db.execute("INSERT INTO runs(id,conversation_id,user_id,question,base_ids,mode,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'answered',?,?)",
               (rid, cid, member['id'], 'q', '[]', 'agent', 5., 15.))
    db.event(rid, 'assessment_execution_start', {'unix': 10.})
    assert observation(db, rid)['execution_seconds'] is None
    saved = {'batches': [{'requests': [{'run_id': rid, 'execution_seconds': None}]}]}
    db.event(rid, 'assessment_execution_end', {'unix': 15.})
    refreshed = finalized_load(saved, db)
    assert refreshed['batches'][0]['requests'][0]['execution_seconds'] == 5.
    assert refreshed['batches'][0]['queue']['p50_seconds'] == 5.
    assert saved['batches'][0]['requests'][0]['execution_seconds'] is None
