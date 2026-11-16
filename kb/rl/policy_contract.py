"""Require the updated adapter when a policy update has completed."""


def select_policy_adapter(adapter_ids, completed_updates):
    ids=sorted(adapter_ids)
    if len(ids)>1:
        raise RuntimeError('Expected one current policy adapter, found multiple loaded adapters')
    if ids:return ids[0]
    if completed_updates:
        raise RuntimeError('Policy was updated but the rollout engine has no adapter; stop before sampling the base model')
    # Initial PEFT B matrices are zero. Upstream's first base-weight sync is
    # therefore the exact initial policy; later updates require a real adapter.
    return None
