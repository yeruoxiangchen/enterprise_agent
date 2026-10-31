import importlib.util
from pathlib import Path
import pytest
from kb.benchmark.sources import write_json,write_jsonl

spec=importlib.util.spec_from_file_location('eval_v41',Path(__file__).resolve().parents[1]/'scripts/eval_v41.py')
queue=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue)


def fixture(root,monkeypatch,candidate_correct=92,missing=False):
    (root/'calibration').mkdir();(root/'browser').mkdir()
    write_json(root/'calibration/calibration_report.json',{'passed':True})
    write_json(root/'preflight.json',{'passed':True})
    write_json(root/'browser/report.json',{'status':'passed'})
    def row(i,ok,family=None):
        return {'id':str(i),'family_id':family or ('unknown-'+str(i) if i<20 else str(i)),
                'status':'answered','trace':[],
                'metrics':{'semantic_correctness':int(ok),'judge_failed':False,'faithfulness':1,'semantic_citation_support':1}}
    def scored(root,name):
        if name.startswith('gap_'):
            enabled=name.endswith('_followup') and not name.endswith('_no_followup')
            rows=[row(i,i>=8 or enabled or 'withheld' not in name,'gap-'+str(i) if i<8 else 'control-'+str(i)) for i in range(12)]
            if 'withheld' in name:
                for r in rows[:8]:r['trace']=[{'kind':'stress_observation','intervention_effective':True}]
            return rows
        n=84 if name=='fixed-bm25' else 88 if name=='fixed-rerank' else candidate_correct
        rows=[row(i,i<n) for i in range(100)]
        if missing and name==queue.CANDIDATE:rows[99]['metrics'].update(judge_failed=True,semantic_correctness=None)
        return rows
    monkeypatch.setattr(queue,'scored',scored)


def test_gate_requires_improvement_over_stronger_baseline(tmp_path,monkeypatch):
    fixture(tmp_path,monkeypatch,candidate_correct=89)
    d=queue.gate_decision(tmp_path)
    assert d['strongest_fixed_baseline']=='fixed-rerank' and d['candidate'] is None
    assert 'gain_under_3_over_strongest_fixed_baseline' in d['reasons']


def test_gate_can_release_but_does_not_itself_run_test(tmp_path,monkeypatch):
    fixture(tmp_path,monkeypatch)
    assert queue.gate_decision(tmp_path)['candidate']==queue.CANDIDATE
    assert not (tmp_path/'test').exists()


def test_missing_judge_never_counts_as_complete_gate(tmp_path,monkeypatch):
    fixture(tmp_path,monkeypatch,missing=True)
    assert 'missing_semantic_grades' in queue.gate_decision(tmp_path)['reasons']


def test_plan_has_three_fresh_arms_and_old_runs_read_only(tmp_path):
    plan=queue.stages(tmp_path/'output',tmp_path/'runtime')
    assert sum(n.startswith('dev_') for n,_,_ in plan)==3
    assert plan[0][0]=='regression'
    for name,cmd,target in plan:
        assert target.is_relative_to(tmp_path/'output') and '--ack-frozen-test' not in cmd
        if name=='grade_saved_v4_guarded_bm25':assert 'grade' in cmd and '--review-dir' in cmd
        if name.startswith('gap_'):assert 'dev_v4_20261028/gap_dataset' in cmd[cmd.index('--dataset')+1]


def test_preflight_rejects_stale_source_and_partial_run(tmp_path,monkeypatch):
    run=tmp_path/'regression_bm25_r2'
    run.mkdir();(tmp_path/'calibration').mkdir()
    write_json(run/'config.json',{'source_fingerprint':'old'})
    write_jsonl(run/'predictions.jsonl',[{'id':'a','status':'answered'}])
    write_json(tmp_path/'calibration/calibration_report.json',{'passed':True})
    monkeypatch.setattr(queue,'source_fingerprint',lambda:'new')
    with pytest.raises(RuntimeError,match='Preflight not ready'):queue.check_preflight(tmp_path)
