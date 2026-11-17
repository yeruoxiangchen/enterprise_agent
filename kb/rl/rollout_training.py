"""Rollout-balanced training on pinned veRL 0.6 / Agent Lightning 0.3.

The advantage broadcast and clipped loss are real Agent Lightning v1 modules
(see vendor/agent_lightning_v1). This compatibility layer scalarizes terminal
RPP feedback and keeps every eligible episode's calls in one optimizer update.
It uses the installed veRL actor's forward, KL, FSDP and optimizer routines.
"""
from __future__ import annotations

import torch
from verl import DataProto
from verl.trainer.ppo.ray_trainer import compute_advantage as upstream_advantage
from verl.trainer.ppo.core_algos import kl_penalty
from verl.utils.device import get_device_id
from verl.workers.actor.dp_actor import DataParallelPPOActor

from .vendor.agent_lightning_v1.rollout_level_advantage import compute_rollout_level_advantage
from .vendor.agent_lightning_v1.per_rollout_loss import (
    compute_policy_loss_per_rollout_mean, normalize_advantages_by_rollout,
)


def scalar_outcome_advantage(batch, **kwargs):
    """One outcome = one observation in the upstream RPP whitening baseline.

    Using a representative response with its original length would still weight
    long episodes more during RPP's masked whitening. A one-token scalar batch
    avoids that, then the upstream v1 wrapper broadcasts back to real tokens.
    """
    scalar = DataProto.from_dict(tensors={
        'response_mask': torch.ones((len(batch), 1), device=batch.batch.device),
        'token_level_rewards': batch.batch['token_level_rewards'].sum(-1, keepdim=True)},
        non_tensors=batch.non_tensor_batch)
    return upstream_advantage(scalar, **kwargs)


def rollout_advantage(batch, **kwargs):
    if kwargs['adv_estimator'] not in ('grpo', 'reinforce_plus_plus'):
        raise ValueError('Rollout adapter supports outcome GRPO / REINFORCE++ only')
    if kwargs.get('gamma', 1.) != 1.:
        raise ValueError('Terminal business outcome adapter requires gamma=1')
    batch, metrics = compute_rollout_level_advantage(batch,
        compute_advantage_fn=scalar_outcome_advantage, **kwargs)
    batch.meta_info['rollout_advantage_metrics'] = metrics
    return batch


def trajectory_weights(mask, rollout_ids):
    """Each eligible rollout has total weight 1 / n_rollouts, across its calls."""
    count = len(set(rollout_ids))
    if not count or not (mask.sum(-1) > 0).all():
        raise ValueError('Every trainable row must contain real response tokens')
    # Keep the upstream precision-safe normalization, then convert its trained
    # row denominator to the number of eligible rollouts for this update.
    return normalize_advantages_by_rollout(torch.ones_like(mask, dtype=torch.float32),
        mask, rollout_ids, num_trained_rows=len(mask)) * (len(mask) / count)


class LossConfig:
    """Only the v1 global_batch_info field is new to the veRL 0.6 actor API."""
    global_batch_info = {'dp_size': 1}

    def __init__(self, config):
        self.config = config

    def __getattr__(self, name):
        return getattr(self.config, name)

    def get(self, name, default=None):
        return getattr(self, name, default)


class RolloutPPOActor(DataParallelPPOActor):
    def update_policy(self, data):
        """Accumulate microbatches, then one upstream clipped optimizer step.

        Adapted from veRL 0.6 DataParallelPPOActor.update_policy. The differences
        are rollout weights for PG/KL and a single full-batch optimizer boundary.
        This job uses one training GPU and a separate frozen-model GPU.
        """
        if torch.distributed.is_initialized() and torch.distributed.get_world_size() != 1:
            raise ValueError('Rollout compatibility actor requires one training rank')
        if (self.config.ppo_micro_batch_size_per_gpu != 1 or self.config.use_dynamic_bsz
                or self.config.ppo_epochs != 1 or self.config.entropy_coeff != 0):
            raise ValueError('Use one epoch, microbatch=1, static batching and entropy_coeff=0')
        rollout_ids = data.non_tensor_batch['rollout_id_list']
        weights = trajectory_weights(data.batch['response_mask'], rollout_ids)
        if not torch.isfinite(data.batch['advantages']).all():
            raise ValueError('Non-finite rollout advantage')
        self.actor_module.train()
        self.actor_optimizer.zero_grad()
        totals = dict(pg_loss=0., kl_loss=0., pg_clipfrac=0., ppo_kl=0., pg_clipfrac_lower=0.)
        cfg = LossConfig(self.config)
        for index, micro in enumerate(data.split(1)):
            micro = micro.to(get_device_id())
            inputs = {**micro.batch, **micro.non_tensor_batch}
            mask = inputs['response_mask']
            weight = weights[index:index+1].to(mask.device)
            _, log_prob = self._forward_micro_batch(inputs, temperature=data.meta_info['temperature'],
                                                    calculate_entropy=False)
            pg, diagnostics = compute_policy_loss_per_rollout_mean(
                old_log_prob=inputs['old_log_probs'], log_prob=log_prob,
                advantages=inputs['advantages'] * weight,
                response_mask=mask, config=cfg,
                rollout_is_weights=inputs.get('rollout_is_weights'))
            loss = pg
            totals['pg_loss'] += pg.detach().item()
            for key in ('pg_clipfrac', 'ppo_kl', 'pg_clipfrac_lower'):
                totals[key] += diagnostics['actor/'+key] / len(data)
            if self.config.use_kl_loss:
                kl = kl_penalty(logprob=log_prob, ref_logprob=inputs['ref_log_prob'],
                                kl_penalty=self.config.kl_loss_type)
                kl_loss = (kl * mask * weight).sum()
                loss = loss + self.config.kl_loss_coef * kl_loss
                totals['kl_loss'] += kl_loss.detach().item()
            # PG and KL already include the full rollout denominator. Adding
            # the old row/microbatch scaling would normalize twice.
            loss.backward()
        grad = self._optimizer_step()
        self.actor_optimizer.zero_grad()
        return {**{'actor/'+key: [value] for key, value in totals.items()},
            'actor/grad_norm': [grad.detach().item()],
            'actor/kl_coef': [self.config.kl_loss_coef],
            'training/optimizer_updates': [1], 'training/trained_rows': [len(data)],
            'training/trained_rollouts': [len(set(rollout_ids))]}
