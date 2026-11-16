"""Exercise actual veRL algorithms on the observed homogeneous-group condition."""
import pytest
torch=pytest.importorskip('torch')


def test_global_reinforce_retains_signal_when_each_prompt_group_is_homogeneous():
    import numpy as np
    from types import SimpleNamespace
    from verl.trainer.ppo.core_algos import (
        compute_grpo_outcome_advantage,compute_reinforce_plus_plus_outcome_advantage,
    )
    mask=torch.ones(4,3)
    rewards=torch.tensor([[0.,0.,1.],[0.,0.,1.],[0.,0.,0.],[0.,0.,0.]])
    groups=np.array(['easy','easy','hard','hard'])
    grpo,_=compute_grpo_outcome_advantage(rewards,mask,groups)
    rpp,_=compute_reinforce_plus_plus_outcome_advantage(rewards,mask,config=SimpleNamespace(gamma=1.))
    assert grpo.count_nonzero()==0
    assert torch.isfinite(rpp).all() and (rpp[:2]>0).all() and (rpp[2:]<0).all()
