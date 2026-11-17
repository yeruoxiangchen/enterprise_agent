"""Episode fragmentation must not change the baseline, PG/KL or optimizer result."""
import copy

import numpy as np
import pytest

torch = pytest.importorskip('torch')
from omegaconf import OmegaConf
from verl import DataProto

from kb.rl.rollout_training import RolloutPPOActor, rollout_advantage, trajectory_weights


def outcomes(fragmented=False):
    mask = torch.tensor([[1., 1., 1., 1.], [1., 1., 0., 0.]])
    rewards = torch.tensor([[0., 0., 0., 1.], [0., 0., 0., 0.]])
    ids = ['success', 'failure']
    if fragmented:
        mask = torch.tensor([[1., 1., 0., 0.], [1., 1., 0., 0.], [1., 1., 0., 0.]])
        rewards = torch.tensor([[0., 1., 0., 0.], [0., 1., 0., 0.], [0., 0., 0., 0.]])
        ids = ['success', 'success', 'failure']
    return DataProto.from_dict({'response_mask': mask, 'token_level_rewards': rewards},
        non_tensors={'rollout_id_list': np.array(ids),
                     'uid': np.array(['easy' if i == 'success' else 'hard' for i in ids])})


def test_episode_advantages_and_normalized_loss_are_fragmentation_invariant():
    results = []
    for split in (False, True):
        data = rollout_advantage(outcomes(split), adv_estimator='reinforce_plus_plus',
            gamma=1., lam=1., num_repeat=2, config=OmegaConf.create({'gamma': 1.}))
        mask = data.batch['response_mask']
        ids = data.non_tensor_batch['rollout_id_list']
        adv = data.batch['advantages']
        weights = trajectory_weights(mask, ids)
        assert (adv[0][mask[0].bool()] > 0).all()
        assert (adv[-1][mask[-1].bool()] < 0).all()
        for rid in set(ids):
            selected = torch.tensor(ids == rid)
            torch.testing.assert_close((weights[selected] * mask[selected]).sum(), torch.tensor(.5))
        coeff = torch.tensor(0., requires_grad=True)
        # Different outcome-specific errors prevent the two signs cancelling.
        values = torch.tensor([2. if rid == 'success' else 1. for rid in ids])[:, None]
        loss = (-adv * (coeff * values).exp() * weights * mask).sum()
        loss.backward()
        results.append((adv[0, 0], adv[-1, 0], loss.detach(), coeff.grad))
    for left, right in zip(*results):
        torch.testing.assert_close(left, right)


def test_mismatched_rewards_inside_one_rollout_are_rejected():
    data = outcomes(True)
    data.batch['token_level_rewards'][1] = 0
    with pytest.raises(RuntimeError, match='same scalar'):
        rollout_advantage(data, adv_estimator='reinforce_plus_plus', gamma=1., lam=1.,
                          num_repeat=2, config=OmegaConf.create({'gamma': 1.}))


def test_real_qwen_ppo_microbatches_match_one_full_rollout_update(monkeypatch):
    from transformers import Qwen3Config, Qwen3ForCausalLM
    from kb.rl.rollout_training import LossConfig, compute_policy_loss_per_rollout_mean, kl_penalty
    torch.manual_seed(28)
    torch.set_num_threads(2)
    config = Qwen3Config(vocab_size=64, hidden_size=32, intermediate_size=64,
        num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2, head_dim=8,
        max_position_embeddings=64, pad_token_id=0, attention_dropout=0.)
    config._attn_implementation = 'sdpa'
    original = Qwen3ForCausalLM(config)
    micro_model, full_model = copy.deepcopy(original), copy.deepcopy(original)
    cfg = OmegaConf.create(dict(ppo_micro_batch_size_per_gpu=1, use_dynamic_bsz=False,
        ppo_epochs=1, entropy_coeff=0., use_kl_loss=True, kl_loss_type='low_var_kl',
        kl_loss_coef=.02, grad_clip=100., clip_ratio=.2, clip_ratio_low=None,
        clip_ratio_high=None, use_remove_padding=False, use_fused_kernels=False,
        entropy_checkpointing=False))
    actor = object.__new__(RolloutPPOActor)
    actor.actor_module, actor.config = micro_model, cfg
    actor.actor_optimizer = torch.optim.SGD(micro_model.parameters(), lr=.01)
    actor.use_remove_padding = actor.use_fused_kernels = False
    actor.device_name = 'cpu'
    ids = torch.tensor([[2,3,4,5,6,7], [3,4,5,6,7,8], [4,5,6,7,8,9]])
    data = DataProto.from_dict({'input_ids': ids, 'attention_mask': torch.ones_like(ids),
        'position_ids': torch.arange(6)[None].expand(3,-1), 'responses': ids[:,-3:],
        'response_mask': torch.tensor([[1.,1.,1.], [1.,1.,0.], [1.,1.,1.]]),
        'advantages': torch.tensor([[.7,.7,.7], [.7,.7,0.], [-.7,-.7,-.7]])},
        non_tensors={'rollout_id_list': np.array(['one','one','two'])},
        meta_info={'temperature': 1.})
    monkeypatch.setattr('kb.rl.rollout_training.get_device_id', lambda: 'cpu')
    with torch.no_grad():
        _, old = actor._forward_micro_batch(dict(data.batch), 1.)
    data.batch['old_log_probs'] = old.clone()
    data.batch['ref_log_prob'] = old + .05
    expected = object.__new__(RolloutPPOActor)
    expected.__dict__.update(actor.__dict__, actor_module=full_model)
    optimizer = torch.optim.SGD(full_model.parameters(), lr=.01)
    _, logp = expected._forward_micro_batch(dict(data.batch), 1.)
    weights = trajectory_weights(data.batch['response_mask'], data.non_tensor_batch['rollout_id_list'])
    pg, _ = compute_policy_loss_per_rollout_mean(old, logp,
        data.batch['advantages']*weights, data.batch['response_mask'], config=LossConfig(cfg))
    kl = kl_penalty(logprob=logp, ref_logprob=data.batch['ref_log_prob'], kl_penalty=cfg.kl_loss_type)
    expected_loss = pg + cfg.kl_loss_coef*(kl*weights*data.batch['response_mask']).sum()
    expected_loss.backward()
    torch.nn.utils.clip_grad_norm_(full_model.parameters(), cfg.grad_clip)
    optimizer.step()
    metrics = actor.update_policy(data)
    assert metrics['training/optimizer_updates'] == [1]
    assert metrics['training/trained_rows'] == [3]
    assert metrics['training/trained_rollouts'] == [2]
    for actual, desired in zip(micro_model.parameters(), full_model.parameters(), strict=True):
        torch.testing.assert_close(actual, desired, rtol=1e-4, atol=2e-5)
