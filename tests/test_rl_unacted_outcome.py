import pytest
from kb.rl.outcome import verify_unacted_outcome


@pytest.mark.parametrize('reward',[0.,1.])
def test_verified_outcome_survives_without_a_policy_action(reward):
    record={'task_id':'task','policy_model_calls_completed':0,'reward':reward}
    assert verify_unacted_outcome(record,'task',reward)==reward


def test_missing_policy_trace_remains_a_failure():
    with pytest.raises(RuntimeError,match='missing from the training trace'):
        verify_unacted_outcome({'task_id':'task','policy_model_calls_completed':1,'reward':1.},'task',1.)


def test_missing_emitted_reward_is_not_synthesized():
    assert verify_unacted_outcome({'task_id':'task','policy_model_calls_completed':0,'reward':1.},'task',None)is None


def test_conflicting_reward_or_identity_is_rejected():
    with pytest.raises(RuntimeError,match='another task'):
        verify_unacted_outcome({'task_id':'other','policy_model_calls_completed':0,'reward':1.},'task',1.)
    with pytest.raises(RuntimeError,match='does not match'):
        verify_unacted_outcome({'task_id':'task','policy_model_calls_completed':0,'reward':0.},'task',1.)
