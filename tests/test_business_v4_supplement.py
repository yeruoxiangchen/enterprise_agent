import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import business_v4_supplement as sup
import business_v4_ops as v4


def test_repeat_plan_uses_exact_existing_questions():
    cases=sup.case_plan({'stages':v4.stages()})
    assert len(cases)==6 and sum(len(c['turns']) for c in cases)==7
    assert all(c in v4.CASES for c in cases)


def test_disputed_axis_does_not_override_calibrated_axes():
    calibration=[{'expected':{'correctness':1,'citation_support':0,'factual_fidelity':1},
                  'grade':{'correctness':1,'citation_support':0,'factual_fidelity':0}}]
    assert all(v['enabled'] for v in sup.calibration_axes(calibration).values())
    calibration[0]['grade']['citation_support']=None
    assert not sup.calibration_axes(calibration)['citation_support']['enabled']
    assert sup.calibration_axes(calibration)['correctness']['enabled']


def test_task_completion_requires_real_action_and_keeps_unknown():
    quality={k:True for k in sup.CHECKS}
    assert sup.completion(1,quality)==1
    quality['service_complete']=False
    assert sup.completion(1,quality)==0
    quality['service_complete']=None
    assert sup.completion(None,quality) is None
    assert sup.completion(0,quality)==0


def test_pairing_reports_regressions_and_unknown_not_only_wins():
    rows=[];grades=[]
    for i,(a,b) in enumerate(((0,1),(1,0),(None,1),(1,1),(0,0))):
        for stage,value in (('single',a),('multi_v4',b)):
            key=f'{stage}/{i}/0';rows.append({'key':key,'case_id':str(i),'turn':0,'stage':stage})
            grades.append({'key':key,'task_completion':value})
    assert sup.paired(rows,grades)['single']['counts']==dict(win=1,loss=1,both_pass=1,both_fail=1,unknown=1)


def test_partial_repetitions_cannot_look_like_all_pass():
    cases=[{'id':'x','turns':[{}]}]
    rows=[{'key':'multi_v4/x/0','case_id':'x','turn':0,'stage':'multi_v4','quality':{'execution':True}}]
    grades=[{'key':'multi_v4/x/0','task_completion':1}]
    assert sup.stability(rows,grades,cases)[0]['all_execution_pass'] is None
    for stage in ('repeat_1','repeat_2'):
        rows.append({**rows[0],'key':stage+'/x/0','stage':stage})
        grades.append({'key':stage+'/x/0','task_completion':0 if stage=='repeat_2' else 1})
    result=sup.stability(rows,grades,cases)[0]
    assert result['all_execution_pass'] is True and result['all_task_pass'] is False
