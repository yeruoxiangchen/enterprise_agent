"""Upstream critic-free RL and separate CPU-runner/GPU-trainer roles."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .tasks import DATA_ROOT, pack_task

AGL_COMMIT = '6db3c73dc0af2e50fe80e56873a57c1703f1b2a7'


def training_config(run_dir, model_path, *, steps=20, batch_size=4, group_size=4,
                    adv_estimator='grpo', val_before_train=False, checkpoint_interval=0):
    """Adapt the upstream Calc-X LoRA recipe; no custom policy-gradient loss."""
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_relative_to(DATA_ROOT):
        raise ValueError('RL outputs belong on the project data volume')
    if adv_estimator not in ('grpo','reinforce_plus_plus'):
        raise ValueError('Use a supported upstream advantage estimator')
    return {
        'ray_kwargs': {'ray_init':{'num_cpus':8}},
        'algorithm': {'adv_estimator':adv_estimator,'gamma':1.0,'use_kl_in_reward':False},
        'data': {'train_batch_size':batch_size,'max_prompt_length':8192,
                 'max_response_length':512,'filter_overlong_prompts':False},
        'actor_rollout_ref': {
            'model': {'path':str(model_path),'lora_rank':16,'lora_alpha':32,
                      'target_modules':'all-linear',
                      'override_config':{'_attn_implementation':'sdpa'},
                      'use_remove_padding':False,'enable_gradient_checkpointing':True},
            # The legacy trainer rounds rows to this size. One keeps all rows;
            # RolloutPPOActor owns the full-batch optimizer boundary.
            'actor': {'strategy':'fsdp2','ppo_mini_batch_size':1,'ppo_micro_batch_size_per_gpu':1,
                      'ppo_epochs':1,'policy_loss':{'loss_mode':'per_rollout_mean'},
                      'use_dynamic_bsz':False,'optim':{'lr':1e-6},
                      'use_kl_loss':True,'kl_loss_coef':0.001,'entropy_coeff':0,
                      'fsdp_config':{'model_dtype':'bfloat16','param_offload':True,'optimizer_offload':True,
                                     'use_torch_compile':False},
                      'checkpoint':{'save_contents':['model','optimizer','extra']}},
            'ref': {'log_prob_micro_batch_size_per_gpu':1,
                    'fsdp_config':{'param_offload':True}},
            'rollout': {'name':'vllm','mode':'async','n':group_size,
                        'enable_prefix_caching':False,
                        'tensor_model_parallel_size':1,'gpu_memory_utilization':0.78,
                        'log_prob_micro_batch_size_per_gpu':1,'max_num_seqs':1,
                        'max_num_batched_tokens':1024,'enforce_eager':True,
                        'multi_turn':{'format':'hermes'},
                        'engine_kwargs':{'vllm':{'enable_auto_tool_choice':True,
                                                 'tool_call_parser':'hermes',
                                                 'enable_lora':True,'max_loras':1,
                                                 'max_cpu_loras':1,'max_lora_rank':16}},
                        'agent':{'num_workers':1},
                        }},
        'trainer': {'n_gpus_per_node':1,'nnodes':1,'critic_warmup':0,
                    'logger':['console'],'project_name':'enterprise-agent-rl',
                    'experiment_name':run_dir.name,'val_before_train':val_before_train,
                    'total_training_steps':steps,'total_epochs':100,
                    'save_freq':checkpoint_interval or steps,'test_freq':steps,
                    'max_actor_ckpt_to_keep':2,
                    'default_local_dir':str(run_dir/'checkpoints')},
        'critic': {'strategy':'fsdp2'},
        'agentlightning': {'trace_aggregator':{'level':'transition',
            'mismatch_log_dir':str(run_dir/'trace_mismatches')}}}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--role',choices=('algorithm','runner'),required=True)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--store',required=True)
    parser.add_argument('--model',default='/data/zjr/enterprise_kb_agent/models/Qwen3-8B')
    parser.add_argument('--steps',type=int,default=20)
    parser.add_argument('--batch-size',type=int,default=4)
    parser.add_argument('--group-size',type=int,default=4)
    parser.add_argument('--adv-estimator',choices=('grpo','reinforce_plus_plus'),default='grpo')
    parser.add_argument('--val-limit',type=int,default=0)
    parser.add_argument('--val-before-train',action='store_true')
    parser.add_argument('--checkpoint-interval',type=int,default=0)
    args=parser.parse_args()
    os.environ['AGL_MANAGED_STORE']='0'
    os.environ['AGL_CURRENT_ROLE']=args.role
    import agentlightning as agl
    from .agent import BusinessAgent
    config=training_config(args.run_dir,args.model,steps=args.steps,
                           batch_size=args.batch_size,group_size=args.group_size,
                           adv_estimator=args.adv_estimator,
                           val_before_train=args.val_before_train,
                           checkpoint_interval=args.checkpoint_interval)
    if args.role=='algorithm':
        from .verl_contract import StrictBusinessDaemon,BusinessTrainer
        algorithm=agl.VERL(config,daemon_cls=StrictBusinessDaemon,trainer_cls=BusinessTrainer)
    else:
        algorithm=None
    trainer=agl.Trainer(algorithm=algorithm,n_runners=1,
        strategy=agl.ClientServerExecutionStrategy(role=args.role),
        store=agl.LightningStoreClient(args.store),tracer=agl.OtelTracer(),
        adapter=agl.LlmProxyTraceToTriplet())
    train=[pack_task(json.loads(line)) for line in (args.dataset/'train.jsonl').read_text().splitlines()]
    val=[pack_task(json.loads(line)) for line in (args.dataset/'dev.jsonl').read_text().splitlines()]
    if args.val_limit:
        val=val[:args.val_limit]
    try:
        trainer.fit(BusinessAgent(),train_dataset=train,val_dataset=val)
    finally:
        if args.role=='algorithm':
            import ray
            ray.shutdown()


if __name__=='__main__':
    main()
