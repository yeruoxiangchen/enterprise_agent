"""Fail closed on missing outcome feedback; policy optimization stays upstream."""
from agentlightning.verl.daemon import AgentModeDaemon
from agentlightning.verl.trainer import AgentLightningTrainer


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

    def get_train_data_batch(self,*args,**kwargs):
        from .padding import trim_shared_prompt_padding
        data,metrics=super().get_train_data_batch(*args,**kwargs)
        metrics['training/common_prompt_padding_removed']=trim_shared_prompt_padding(data)
        return data,metrics


class BusinessTrainer(AgentLightningTrainer):
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
            result=original(batch)
            completed+=1
            write_json(Path(os.environ['KB_RL_RUN_DIR'])/'policy-updates.json',
                {'completed_updates':completed,'metrics':result.meta_info.get('metrics',{})})
            return result
        self.actor_rollout_wg.update_actor=update
        try:return super().fit()
        finally:self.actor_rollout_wg.update_actor=original
