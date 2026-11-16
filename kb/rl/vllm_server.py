"""HTTP compatibility for selective LoRA rollouts on pinned veRL/vLLM.

veRL 0.6 updates TensorLoRARequest objects on the GPU worker. Its HTTP frontend
does not select these dynamic adapters. Keep native OpenAI/tool/token handling,
but bind each request to the actually loaded adapter after a policy update.
"""
import json
import os
from pathlib import Path
from concurrent.futures import Future

import ray
from verl.workers.rollout.vllm_rollout.vllm_async_server import (
    ExternalZeroMQDistributedExecutor, vLLMHttpServer,
)
from .policy_contract import select_policy_adapter


BaseServer=getattr(vLLMHttpServer,'__ray_actor_class__',None)
if BaseServer is None:
    BaseServer=vLLMHttpServer.__ray_metadata__.modified_class


class BusinessZeroMQExecutor(ExternalZeroMQDistributedExecutor):
    def collective_rpc(self,method,timeout=None,args=(),kwargs=None,non_block=False):
        # vLLM 0.11 added this keyword to the executor contract. Keep veRL's
        # serialized ZMQ transport; a non-blocking caller receives completed
        # futures with exactly the same worker outputs.
        outputs=super().collective_rpc(method,timeout=timeout,args=args,kwargs=kwargs)
        if not non_block:return outputs
        futures=[]
        for output in outputs:
            future=Future();future.set_result(output);futures.append(future)
        return futures


@ray.remote(num_cpus=1)
class BusinessVLLMServer(BaseServer):
    async def run_server(self,args):
        import vllm.entrypoints.openai.api_server as api
        from vllm.entrypoints.openai.serving_chat import OpenAIServingChat
        from vllm.lora.request import LoRARequest
        args.enable_prefix_caching=False
        args.distributed_executor_backend=BusinessZeroMQExecutor
        class PolicyChat(OpenAIServingChat):
            async def create_chat_completion(self,request,raw_request=None):
                state=Path(os.environ['KB_RL_RUN_DIR'])/'policy-updates.json'
                updates=json.loads(state.read_text())['completed_updates'] if state.exists() else 0
                adapter=select_policy_adapter(await self.engine_client.list_loras(),updates)
                if adapter is not None:
                    alias='enterprise-current-policy'
                    self.models.lora_requests.clear()
                    self.models.lora_requests[alias]=LoRARequest(
                        lora_name=str(adapter),lora_int_id=adapter,lora_path='simon_lora_path')
                    request=request.model_copy(update={'model':alias})
                audit={'completed_updates':updates,'adapter_id':adapter,
                       'initial_zero_adapter_policy':adapter is None}
                path=Path(os.environ['KB_RL_RUN_DIR'])/'served-policy.jsonl'
                with path.open('a')as stream:stream.write(json.dumps(audit)+'\n')
                return await super().create_chat_completion(request,raw_request)
        # This class is installed only in this job's private HTTP Ray actor.
        # vLLM retains ownership of parsing, tool calls, scheduling and sampling.
        api.OpenAIServingChat=PolicyChat
        return await super().run_server(args)
