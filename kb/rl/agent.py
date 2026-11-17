"""Route the selected agent role through the upstream training proxy."""
from __future__ import annotations

import os
from pathlib import Path

import agentlightning as agl


class BusinessAgent(agl.LitAgent[dict]):
    async def _episode(self, task, resources, rollout, temperature):
        from .runtime import run_episode
        from .tasks import unpack_task
        import json
        from .runtime import write_json
        task_sha256=task['task_sha256']
        task=unpack_task(task)
        llm = resources['main_llm']
        url = llm.get_base_url(rollout.rollout_id, rollout.attempt.attempt_id)
        folder = Path(os.environ['KB_RL_RUN_DIR'])/'rollouts'/rollout.rollout_id/rollout.attempt.attempt_id
        phase_path=Path(os.environ['KB_RL_RUN_DIR'])/'evaluation-phase.json'
        phase=json.loads(phase_path.read_text()) if task['split']=='dev' and phase_path.exists() else None
        result = await run_episode(task,
            {'url':url,'model':llm.model,'key':llm.api_key or os.environ['KB_RL_PROXY_KEY']},
            {'url':os.environ['KB_RL_SPECIALIST_URL'],
             'model':os.environ['KB_RL_SPECIALIST_MODEL']},folder,
            temperature=temperature,
            train_role=os.environ.get('KB_RL_TRAIN_ROLE','supervisor'),
            delegation_protocol=os.environ.get('KB_RL_DELEGATION_PROTOCOL','native'),
            completion_review={'none':False,'errors':'errors','always':True}[
                os.environ.get('KB_RL_COMPLETION_REVIEW','errors')],
            context_tokens=int(os.environ.get('KB_RL_CONTEXT','8192')),
            output_tokens=int(os.environ.get('KB_RL_OUTPUT_TOKENS','512')))
        result.update(task_sha256=task_sha256,evaluation_phase=phase)
        write_json(folder/'result.json',result)
        # The upstream runner records this scalar as a reward span. It never
        # receives our private oracle fields in any LLM request.
        return result['reward']

    async def training_rollout_async(self, task, resources, rollout):
        return await self._episode(task,resources,rollout,0.7)

    async def validation_rollout_async(self, task, resources, rollout):
        return await self._episode(task,resources,rollout,0)
