import importlib.util
from pathlib import Path
import sys
from kb.benchmark.sources import write_json

scripts=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(scripts))
spec=importlib.util.spec_from_file_location('eval_v42',scripts/'eval_v42.py')
queue=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue)
sys.path.pop(0)


def fixture(root,monkeypatch,reduced=True,critical=True):
    (root/'calibration').mkdir();(root/'browser').mkdir()
    write_json(root/'calibration/calibration_report.json',{'passed':True})
    write_json(root/'browser/report.json',{'status':'passed'})
    def rows(root,phase,arm):
        n={'fixed42-rerank':32,'agent-guarded41-rerank':31,'agent-guarded42-rerank':33}[arm]
        refuse=3 if arm=='agent-guarded41-rerank' or not reduced else 1
        r=[{'id':str(i),'family_id':'unknown-'+str(i) if i<20 else str(i),'status':'answered',
            'metrics':{'semantic_correctness':int(i<n),'judge_failed':False,'false_refusal':i>=34-refuse}}
           for i in range(34)]
        if not critical and arm==queue.ARMS[-1]:r[1]['metrics']['semantic_correctness']=0
        return r
    monkeypatch.setattr(queue,'rows_for',rows)


def test_pilot_gate_requires_actual_reduction_and_critical_coverage(tmp_path,monkeypatch):
    fixture(tmp_path,monkeypatch)
    assert queue.pilot_gate(tmp_path)['eligible']
    other=tmp_path/'other';other.mkdir();fixture(other,monkeypatch,reduced=False)
    assert 'false_refusals_not_reduced' in queue.pilot_gate(other)['reasons']
    third=tmp_path/'third';third.mkdir();fixture(third,monkeypatch,critical=False)
    assert 'critical_cases_not_all_correct' in queue.pilot_gate(third)['reasons']


def test_plans_share_reranker_and_never_consume_test(tmp_path):
    assert all(a.endswith('rerank') for a in queue.ARMS)
    for phase in ['pilot','full']:
        for name,cmd,target in queue.plans(tmp_path,phase):
            assert '--ack-frozen-test' not in cmd and '--split' not in cmd
            assert target.is_relative_to(tmp_path)
            if name.startswith(phase+'_'):
                assert ('--case-ids' in cmd)==(phase=='pilot')


def test_inference_identity_excludes_only_offline_grader(tmp_path):
    (tmp_path/'benchmark').mkdir();(tmp_path/'agent.py').write_text('original')
    judge=tmp_path/'benchmark/semantic.py';judge.write_text('v1')
    a=queue.inference_fingerprint(tmp_path);judge.write_text('v2')
    assert queue.inference_fingerprint(tmp_path)==a
    (tmp_path/'agent.py').write_text('modified')
    assert queue.inference_fingerprint(tmp_path)!=a


def test_coverage_needs_complete_matched_unique_cases():
    def row(cid,failed=False):return {'id':cid,'metrics':{'judge_failed':failed}}
    assert queue.complete_grading({'a':[row('1'),row('2')],'b':[row('1'),row('2')]},2)
    assert not queue.complete_grading({'a':[row('1'),row('2')],'b':[row('1'),row('2',True)]},2)
    assert not queue.complete_grading({'a':[row('1'),row('2')],'b':[row('1'),row('3')]},2)
    assert not queue.complete_grading({'a':[row('1'),row('1')]},2)
