"""Reject evaluation mixing and incomplete comparisons before reporting gains."""
import copy

import pytest

from kb.rl.review import compare_results


def record(task,kind,reward,*,family='close'):
    return {'task_id':task,'task_sha256':'hash-'+task,'split':'dev','family':family,
        'train_role':'specialists','supervisor':{'model':'fixed','temperature':0},
        'specialist':{'model':'policy','temperature':0},
        'evaluation_phase':{'kind':kind,'global_step':0 if kind=='baseline' else 8,
                            'completed_updates':0 if kind=='baseline' else 8},
        'reward':reward,'checks':{'outcome':bool(reward)},'error':None,
        'seconds':10,'tokens':100,'model_calls':3,'usage_missing_calls':0}


def test_paired_report_keeps_regressions_and_measures_inference_cost():
    rows=[record('one','baseline',0),record('one','trained',1),
          record('two','baseline',1),record('two','trained',0)]
    result=compare_results(rows,{'one':'hash-one','two':'hash-two'},steps=8)
    assert result['success_delta_percentage_points']==0
    assert result['paired']=={'wins':1,'regressions':1,'unchanged':0,'exact_mcnemar_p':1.}
    assert result['arms']['trained']['mean_tokens']==100
    assert result['arms']['baseline']['p95_seconds']==10


@pytest.mark.parametrize('problem',['missing','duplicate','changed_task','updated_baseline','different_decoding'])
def test_bad_pairing_is_rejected(problem):
    rows=[record('one','baseline',0),record('one','trained',1)]
    if problem=='missing':rows.pop()
    elif problem=='duplicate':rows.append(copy.deepcopy(rows[1]))
    elif problem=='changed_task':rows[1]['task_sha256']='other'
    elif problem=='updated_baseline':rows[0]['evaluation_phase']['completed_updates']=1
    else:rows[1]['specialist']['temperature']=.7
    with pytest.raises(ValueError):compare_results(rows,{'one':'hash-one'},steps=8)
