"""Fail closed on missing outcome feedback; policy optimization stays upstream."""
from agentlightning.verl.daemon import AgentModeDaemon
from agentlightning.verl.trainer import AgentLightningTrainer


class NoPolicyActions(RuntimeError):
    def __init__(self,metrics):
        super().__init__('Verified batch contains no selected-role action')
        self.metrics=metrics


class StrictBusinessDaemon(AgentModeDaemon):
    def __init__(self,*args,**kwargs):
        if kwargs.get('mode','v1')=='v1' and kwargs.get('llm_proxy') is None:
            from .proxy import make_training_proxy
            kwargs['llm_proxy']=make_training_proxy(kwargs['store'])
        super().__init__(*args,**kwargs)
        # Construct proxy state inside the upstream Ray task, where its HTTP
        # client and event-loop locks belong. Passing a live proxy through Ray
        # would attempt to pickle those process-local resources.
        from agentlightning.llm_proxy import AddLogprobs
        if AddLogprobs not in self.llm_proxy.callbacks:
            self.llm_proxy.callbacks.append(AddLogprobs)

    def _fillna_reward(self, rollout):
        if rollout.final_reward is None:
            raise RuntimeError(f'Business rollout {rollout.rollout_id} has no verified reward; stop the update')
        return super()._fillna_reward(rollout)

    async def _validate_data_v1(self,rollout):
        # Preserve the pinned upstream conversion, including original token
        # traces. Only zero-selected-role-action episodes need reward recovery.
        import os
        from agentlightning import RolloutLegacy
        from agentlightning.types import Task
        from .outcome import recover_unacted_outcome
        spans=await self.store.query_spans(rollout.rollout_id,attempt_id='latest')
        triplets=self.adapter.adapt(spans) if spans else []
        reward=next((t.reward for t in reversed(triplets) if t.reward is not None),None)
        if not triplets:
            reward=await recover_unacted_outcome(self.store,rollout,os.environ['KB_RL_RUN_DIR'],spans)
        result=RolloutLegacy(rollout_id=rollout.rollout_id,
            task=Task(rollout_id=rollout.rollout_id,input=rollout.input,mode=rollout.mode,
                      resources_id=rollout.resources_id,metadata=rollout.metadata or {}),
            final_reward=reward,triplets=triplets,metadata=rollout.metadata or {})
        self._fillna_reward(result)
        if triplets:self._validate_data(result)
        return result

    def get_train_data_batch(self,*args,**kwargs):
        from .padding import trim_shared_prompt_padding
        assert self.is_train
        assert len(self._completed_rollouts_v0)==self._total_tasks_queued
        if self._completed_rollouts_v0 and not any(r.triplets for r in self._completed_rollouts_v0.values()):
            rewards=[self._fillna_reward(r)for r in self._completed_rollouts_v0.values()]
            raise NoPolicyActions({'training/skipped_no_policy_actions':1,
                'training/n_rollouts':len(rewards),'training/reward':sum(rewards)/len(rewards)})
        data,metrics=super().get_train_data_batch(*args,**kwargs)
        metrics['training/common_prompt_padding_removed']=trim_shared_prompt_padding(data)
        return data,metrics


class BusinessTrainer(AgentLightningTrainer):
    def _validate(self):
        # Use the upstream evaluator with an immutable phase label. Both arms
        # use the same native graph, task bytes, frozen role and decoding.
        import os,json,time
        from pathlib import Path
        from .runtime import write_json
        folder=Path(os.environ['KB_RL_RUN_DIR'])
        state=folder/'policy-updates.json'
        updates=json.loads(state.read_text())['completed_updates'] if state.exists() else 0
        phase={'kind':'baseline' if self.global_steps==0 else 'trained',
               'global_step':self.global_steps,'completed_updates':updates,
               'started_at':time.time()}
        write_json(folder/'evaluation-phase.json',phase)
        metrics=super()._validate()
        with (folder/'evaluation-history.jsonl').open('a') as stream:
            stream.write(json.dumps({**phase,'finished_at':time.time(),'metrics':metrics},default=str)+'\n')
        return metrics

    def _train_step(self,batch_dict):
        try:return super()._train_step(batch_dict)
        except NoPolicyActions as exc:
            self.agent_mode_daemon.clear_data_and_server()
            self.async_rollout_manager.sleep()
            return exc.metrics

    def init_workers(self):
        # veRL 0.6's replica creates this actor through the module symbol; the
        # older AGL custom_async_server option is ignored by that release.
        # Replace it only within this job's private trainer process.
        from verl.workers.rollout.vllm_rollout import vllm_async_server
        from .vllm_server import BusinessVLLMServer
        original=vllm_async_server.vLLMHttpServer
        vllm_async_server.vLLMHttpServer=BusinessVLLMServer
        try:return super().init_workers()
        finally:vllm_async_server.vLLMHttpServer=original

    def fit(self):
        from pathlib import Path
        import os
        from .runtime import write_json
        original=self.actor_rollout_wg.update_actor
        completed=0
        def update(batch):
            nonlocal completed
            import torch,json
            advantages=batch.batch['advantages']
            mask=batch.batch['response_mask'].bool()
            active=advantages[mask]
            if not torch.isfinite(active).all():raise RuntimeError('Non-finite policy advantages')
            signal={'positive_tokens':int((active>0).sum()),'negative_tokens':int((active<0).sum()),
                    'nonzero_tokens':int(active.count_nonzero())}
            result=original(batch)
            completed+=1
            record={'completed_updates':completed,'learning_signal':signal,
                    'metrics':result.meta_info.get('metrics',{})}
            write_json(Path(os.environ['KB_RL_RUN_DIR'])/'policy-updates.json',record)
            with (Path(os.environ['KB_RL_RUN_DIR'])/'policy-update-history.jsonl').open('a')as stream:
                stream.write(json.dumps(record,default=str)+'\n')
            return result
        self.actor_rollout_wg.update_actor=update
        try:return super().fit()
        finally:self.actor_rollout_wg.update_actor=original
