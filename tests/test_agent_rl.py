"""Business reward controls use the real registry and upstream grading code."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from kb.harness.journal import Journal
from kb.harness.ports import CRMPort
from kb.rl.tasks import BENCH_ROOT, make_task, FAMILIES, environment_for
from kb.rl.reward import score_task
from kb.rl.training import training_config


def execute(sample, tmp_path, *, forbidden=False, collateral=False, wrong_permission=False):
    from harness.tools.environment import create_environment
    import harness.tools.domains
    environment=environment_for(sample)
    port=CRMPort(environment,yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
                 (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text())
    port.begin_request(sample['messages'][0],'0')
    journal=Journal(tmp_path/'effects.sqlite3','one')
    specs={s.name:s for s in port.tools()}
    target=sample['target_ticket']
    user='admin.rlee' if sample['family']=='delete_dependency' else 'agent.jsmith'
    plan=[]
    gets=sample['task']['expected_args']['get_ticket']
    for arguments in gets if isinstance(gets,list) else [gets]:
        plan.append(('get_ticket',arguments))
    checks=[name for name in sample['task']['expected_order'] if name in ('check_dependencies','check_permission')]
    for name in checks:
        arguments={'ticket_id':target}
        if name=='check_permission':
            operation='delete' if sample['family'].startswith('delete') else 'edit'
            arguments.update(user_id=user,action='view' if wrong_permission else operation)
        plan.append((name,arguments))
    for name in ('reassign_ticket','close_ticket','add_ticket_comment'):
        if name in sample['task']['expected_actions']:
            plan.append((name,sample['task']['expected_args'][name]))
    if forbidden:
        plan.append(('delete_ticket',{'ticket_id':target}))
    try:
        for name,arguments in plan:
            journal.invoke(specs[name],arguments,'0',port,lambda *args:None)
        if collateral:
            other=next(k for k in environment.get_state()['tickets'] if k!=target)
            port.execute('add_ticket_comment',{'ticket_id':other,'comment':'Unrequested change'},'collateral')
        adapter=SimpleNamespace(environment=environment,tool_calls=port.audit,
                                engine=SimpleNamespace(journal=journal,specs=specs))
        return score_task(sample,adapter)
    finally:
        journal.close()


@pytest.mark.parametrize('index',list(range(16)))
def test_valid_outcomes_and_both_conditional_branches_receive_reward(index,tmp_path):
    sample=make_task('train',index)
    result=execute(sample,tmp_path)
    assert result['reward']==1,result


def test_backend_blocked_forbidden_attempt_is_still_a_failure(tmp_path):
    sample=make_task('train',FAMILIES.index('delete_dependency'))
    result=execute(sample,tmp_path,forbidden=True)
    assert result['reward']==0 and result['checks']['forbidden_attempts'] is False


def test_collateral_change_fails_despite_target_success(tmp_path):
    result=execute(make_task('train',FAMILIES.index('lookup')),tmp_path,collateral=True)
    assert result['checks']['required_actions'] is True
    assert result['reward']==0 and result['checks']['unrelated_tickets_unchanged'] is False


def test_checking_view_permission_does_not_satisfy_close_assessment(tmp_path):
    sample=make_task('train',FAMILIES.index('assess_only'))
    result=execute(sample,tmp_path,wrong_permission=True)
    assert result['checks']['required_actions'] is True
    assert result['checks']['arguments'] is False and result['reward']==0


def test_prerequisites_are_visible_and_bind_the_business_subject():
    for family in ('close','delete_no_permission','delete_dependency','assess_only'):
        sample=make_task('train',FAMILIES.index(family))
        instruction=sample['messages'][0].lower()
        assert 'read' in instruction
        assert 'permission' in instruction or 'allowed to delete' in instruction
        expected=sample['task']['expected_args']['check_permission']
        assert expected['ticket_id']==sample['target_ticket']
        assert expected['user_id'] in sample['messages'][0]
        assert expected['action']==('delete' if family.startswith('delete') else 'edit')
        assert sample['task']['expected_order'][0]=='get_ticket'


def test_heldout_is_new_composition_with_independent_fixtures(tmp_path):
    train=make_task('train',0)
    heldout=make_task('heldout',0)
    assert train['fixture_sha256']!=heldout['fixture_sha256']
    assert train['task']['expected_actions']==['get_ticket']
    assert heldout['task']['expected_actions']==['get_ticket','get_ticket']
    assert execute(heldout,tmp_path)['reward']==1


def test_exact_snapshot_reset_and_sibling_rollout_isolation():
    sample=make_task('train',0)
    first,second=environment_for(sample),environment_for(sample)
    assert first.get_state()==sample['task']['initial_state']
    first.data['tickets'][sample['target_ticket']]['status']='Closed'
    assert second.get_state()['tickets'][sample['target_ticket']]['status']=='Open'
    assert sample['task']['initial_state']['tickets'][sample['target_ticket']]['status']=='Open'


def test_frozen_dataset_hash_is_checked_before_training(tmp_path):
    from kb.rl.tasks import prepare,verify_dataset
    folder=tmp_path/'frozen'
    manifest=prepare(folder,{'train':8,'dev':1,'heldout':1})
    assert verify_dataset(folder)==manifest
    with (folder/'train.jsonl').open('ab') as stream:stream.write(b' ')
    with pytest.raises(ValueError,match='Frozen train dataset changed'):
        verify_dataset(folder)


def test_config_uses_upstream_grpo_lora_and_data_storage():
    cfg=training_config('/data/zjr/enterprise_kb_agent/rl/outputs/check','cached-model')
    assert cfg['algorithm']['adv_estimator']=='grpo'
    assert cfg['actor_rollout_ref']['model']['lora_rank']==16
    assert cfg['actor_rollout_ref']['model']['target_modules']=='all-linear'
    assert cfg['actor_rollout_ref']['model']['override_config']['_attn_implementation']=='sdpa'
    assert cfg['actor_rollout_ref']['actor']['fsdp_config']['model_dtype']=='bfloat16'
    assert cfg['trainer']['n_gpus_per_node']==1
    assert cfg['actor_rollout_ref']['actor']['strategy']=='fsdp2'
    assert cfg['actor_rollout_ref']['rollout']['enable_prefix_caching'] is False
    assert cfg['actor_rollout_ref']['rollout']['engine_kwargs']['vllm']['enable_lora'] is True
    with pytest.raises(ValueError,match='data volume'):
        training_config('/tmp/wrong','cached-model')


def test_paired_validation_uses_upstream_pretraining_evaluation_and_periodic_checkpoints():
    cfg=training_config('/data/zjr/enterprise_kb_agent/rl/outputs/check','cached-model',
        steps=8,val_before_train=True,checkpoint_interval=4)
    assert cfg['trainer']['val_before_train'] is True
    assert cfg['trainer']['test_freq']==8 and cfg['trainer']['save_freq']==4
    assert cfg['trainer']['max_actor_ckpt_to_keep']==2


def test_rollout_requires_updated_adapter_and_rejects_ambiguous_versions():
    from kb.rl.policy_contract import select_policy_adapter
    assert select_policy_adapter(set(),0) is None
    assert select_policy_adapter({73},1)==73
    with pytest.raises(RuntimeError,match='no adapter'):
        select_policy_adapter(set(),1)
    with pytest.raises(RuntimeError,match='multiple'):
        select_policy_adapter({73,74},2)


@pytest.mark.parametrize('train_role',['supervisor','specialists'])
def test_native_episode_uses_real_tools_and_keeps_oracle_out_of_model(tmp_path,monkeypatch,train_role):
    import asyncio
    from kb.rl.runtime import run_episode
    from kb.context import ContextBudget
    from test_deep_harness import ScriptModel,call
    from test_native_harness import answer
    sample=make_task('train',0)
    supervisor=ScriptModel(replies=[call('task',{'subagent_type':'general-purpose',
        'description':'Read the requested ticket.'},'delegate'),answer('The requested ticket is open.')])
    expert=ScriptModel(replies=[call('get_ticket',{'ticket_id':sample['target_ticket']},'get'),
                              answer('The requested ticket is open.')])
    monkeypatch.setattr(ContextBudget,'count',lambda *args:(100,'test'))
    resources=[]
    def models(**kwargs):
        resources.append((kwargs['metadata']['rl_role'],kwargs['base_url']))
        return supervisor if kwargs['metadata']['rl_role'] in ('supervisor','frozen_supervisor') else expert
    result=asyncio.run(run_episode(sample,{'url':'http://train.local/v1','model':'policy'},
        {'url':'http://frozen.local/v1','model':'base'},tmp_path/'episode',model_factory=models,
        train_role=train_role))
    assert result['reward']==1,result
    assert len(supervisor.seen)==len(expert.seen)==2
    assert result['model_calls']==4
    assert result['policy_model_calls_completed']==2
    assert result['supervisor']['trainable']==(train_role=='supervisor')
    assert result['specialist']['trainable']==(train_role=='specialists')
    assert resources==([('supervisor','http://train.local/v1'),('frozen_specialist','http://frozen.local/v1')]
        if train_role=='supervisor' else
        [('frozen_supervisor','http://frozen.local/v1'),('specialist_policy','http://train.local/v1')])
    inputs=str(supervisor.seen)+str(expert.seen)
    assert 'fixture_sha256'not in inputs and 'reward_contract'not in inputs
    assert 'expected_actions'not in inputs and 'expected_state'not in inputs


def test_frozen_supervisor_may_finish_lookup_without_a_trainable_call(tmp_path,monkeypatch):
    import asyncio
    from kb.rl.runtime import run_episode
    from kb.context import ContextBudget
    from test_deep_harness import ScriptModel,call
    from test_native_harness import answer
    sample=make_task('train',0)
    root=ScriptModel(replies=[call('get_ticket',{'ticket_id':sample['target_ticket']},'read'),
                              answer('The ticket is open.')])
    child=ScriptModel(replies=[])
    monkeypatch.setattr(ContextBudget,'count',lambda *args:(100,'test'))
    def models(**kwargs):
        return root if kwargs['metadata']['rl_role']=='frozen_supervisor' else child
    result=asyncio.run(run_episode(sample,{'url':'http://policy.local/v1','model':'policy'},
        {'url':'http://fixed.local/v1','model':'base'},tmp_path/'episode',
        model_factory=models,train_role='specialists'))
    assert result['reward']==1 and result['model_calls']==2
    assert result['policy_model_calls_completed']==0
    assert result['role_model_calls']['frozen_supervisor']['completed']==2


@pytest.mark.parametrize('budget_failure',[False,True])
def test_programming_failure_stops_training_while_policy_budget_gets_zero(tmp_path,monkeypatch,budget_failure):
    import asyncio
    from kb.rl.runtime import run_episode
    from kb.context import ContextBudget
    from kb.harness.native import NativeBudgetExceeded
    from test_deep_harness import ScriptModel
    failure=NativeBudgetExceeded('shared budget') if budget_failure else RuntimeError('broken integration')
    def fail(messages):raise failure
    model=ScriptModel(replies=[fail])
    monkeypatch.setattr(ContextBudget,'count',lambda *args:(100,'test'))
    episode=run_episode(make_task('train',0),{'url':'http://train.local/v1','model':'policy'},
        {'url':'http://frozen.local/v1','model':'base'},tmp_path/'episode',
        model_factory=lambda **kwargs:model)
    if budget_failure:
        result=asyncio.run(episode)
        assert result['reward']==0 and result['error'].startswith('NativeBudgetExceeded:')
    else:
        with pytest.raises(RuntimeError,match='broken integration'):
            asyncio.run(episode)
