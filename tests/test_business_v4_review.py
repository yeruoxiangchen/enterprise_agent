import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import business_v4_ops as ops
from business_v4_review import AxisJudge,combine,payload,calibration_jobs,make_job


def parts(correct=1,cite=1,fidelity=1):
    return {k:{'score':v,'review_status':'ok'} for k,v in zip(
        ('correctness','citation_support','factual_fidelity'),(correct,cite,fidelity))}


def test_wrong_answer_cannot_pass_task_completion_and_failed_grades_stay_unknown():
    assert combine(parts(correct=0))['task_completion']==0
    assert combine(parts(),False)['task_completion']==0
    assert combine(parts())['task_completion']==1  # consultation needs no ticket
    p=parts();p['correctness']={'review_status':'error','correctness':None}
    result=combine(p)
    assert result['correctness'] is None and result['task_completion'] is None
    assert result['review_status']=='error'


def test_reference_sources_cannot_leak_into_citation_review():
    job={'answer':'a','candidate_citations':[{'text':'candidate'}],
         'reference_documents':[{'text':'gold'}],'rubric':'gold rubric','expected':1,'arm':'v4'}
    assert payload(job,'citation_support')=={'answer':'a','candidate_citations':[{'text':'candidate'}]}
    content=payload(job,'correctness')
    assert 'candidate_citations' not in content and 'expected' not in content and 'arm' not in content
    assert 'rubric' not in payload(job,'factual_fidelity')


def test_axis_rejects_nonbinary_values_and_non_candidate_quotes():
    judge=object.__new__(AxisJudge)
    for score,quote in ((True,'abc'),(2,'abc'),(1,'xyz')):
        with pytest.raises(ValueError):judge.validate_grade({'score':score,'reason':'why','answer_quote':quote},{'answer':'abc'})
    judge.validate_grade({'score':0,'reason':'why','answer_quote':'abc'},{'answer':'abc'})


def test_status_keeps_baseline_errors_separate_from_candidate(tmp_path):
    original={'status':'complete_with_errors'}
    (tmp_path/'status.json').write_text(json.dumps(original))
    rows=[{'arm':'multi_v4','key':'new','quality':{'execution':True},'error':None},
          {'arm':'multi_v3','key':'old','quality':{'execution':False},'error':'old contract error'}]
    (tmp_path/'results.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    (tmp_path/'judge_status.json').write_text(json.dumps({'status':'calibration_failed'}))
    details=ops.status_details(tmp_path)
    assert details['candidate']['passed']==1 and details['candidate']['errors']==[]
    assert details['comparators']['errors'][0]['key']=='old'
    assert details['original_judge']['status']=='calibration_failed'
    assert json.loads((tmp_path/'status.json').read_text())==original


def test_regrade_uses_frozen_documents_and_service_receipt():
    row={'question':'q','user_history':[],'rubric':'r','answer':'a','expected_action':'draft',
         'citations':[],'ticket':None,'tool_actions':[],'quality':{'service_complete':False}}
    job=make_job(row,{'documents':[['d','f','frozen gold']]})
    assert job['reference_documents']==[{'filename':'f','text':'frozen gold'}]
    assert job['service_verified'] is False


def test_existing_calibration_labels_are_preserved():
    original=[{'job':{'task_kind':'knowledge','answer':'a','reference_documents':[]},
               'expected':{'correctness':i%2}} for i in range(5)]
    copy=json.loads(json.dumps(original));jobs=calibration_jobs(original)
    assert original==copy
    assert [j['expected'] for j in jobs[:5]]==[j['expected'] for j in original]
    assert len(jobs)==8
