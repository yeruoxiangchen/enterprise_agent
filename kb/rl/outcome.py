"""Keep verified task outcomes for episodes with no selected-role action."""
import json
from pathlib import Path


def reward_from_spans(spans):
    from agentlightning.emitter.reward import get_reward_value
    for span in sorted(spans,key=lambda s:s.sequence_id,reverse=True):
        reward=get_reward_value(span)
        if reward is not None:return reward
    return None


def verify_unacted_outcome(record,task_id,emitted_reward):
    if emitted_reward is None:return None
    if record.get('task_id')!=task_id:
        raise RuntimeError('Business outcome belongs to another task')
    if record.get('policy_model_calls_completed')!=0:
        raise RuntimeError('Completed policy responses are missing from the training trace')
    if record.get('reward')!=emitted_reward:
        raise RuntimeError('Emitted reward does not match the verified business result')
    return emitted_reward


async def recover_unacted_outcome(store,rollout,run_dir,spans=None):
    if spans is None:spans=await store.query_spans(rollout.rollout_id,attempt_id='latest')
    reward=reward_from_spans(spans)
    if reward is None:return None
    attempts=await store.query_attempts(rollout.rollout_id,sort_order='desc',limit=1)
    if not attempts:raise RuntimeError('Missing attempt for verified business outcome')
    path=Path(run_dir)/'rollouts'/rollout.rollout_id/attempts[0].attempt_id/'result.json'
    record=json.loads(path.read_text())
    return verify_unacted_outcome(record,rollout.input['id'],reward)
