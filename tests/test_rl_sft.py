"""Successful executable training episodes produce response-only SFT samples."""
import copy
import json
from types import SimpleNamespace

import pytest
import yaml

from kb.harness.ports import CRMPort
from kb.rl.reward import score_task
from kb.rl.sft import verified_rows
from kb.rl.tasks import BENCH_ROOT, FAMILIES, environment_for, make_task, pack_task


def real_success(split='train'):
    sample = make_task(split, FAMILIES.index('assess_only'))
    env = environment_for(sample)
    port = CRMPort(env, yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
                   (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text())
    port.begin_request(sample['messages'][0], '0')
    for name in ('get_ticket', 'check_dependencies', 'check_permission'):
        port.execute(name, sample['task']['expected_args'][name], name)
    adapter = SimpleNamespace(environment=env, tool_calls=port.audit,
        engine=SimpleNamespace(specs={s.name:s for s in port.tools()},
                               journal=SimpleNamespace(records=lambda: [])))
    result = {**score_task(sample, adapter), 'task_id': sample['id'], 'split': split,
        'audit': copy.deepcopy(port.audit), 'effects': [], 'environment': copy.deepcopy(env.get_state()),
        'task_sha256': pack_task(sample)['task_sha256'], 'policy_model_calls_completed': 1,
        'train_role': 'specialists'}
    record = {'rollout': {'rollout_id': 'one', 'input': pack_task(sample), 'mode': 'train'},
        'triplets': [{'prompt': {'token_ids': [2,3,4]}, 'response': {'token_ids': [5,6]}}]}
    return record, result


def test_verified_original_ids_mask_user_and_tool_context_and_preserve_exact_response():
    record, result = real_success()
    rows, reason = verified_rows(record, result)
    assert reason == 'accepted' and len(rows) == 1
    assert rows[0]['input_ids'] == [2,3,4,5,6]
    assert rows[0]['completion_mask'] == [0,0,0,1,1]
    assert rows[0]['provenance']['split'] == 'train'


@pytest.mark.parametrize('problem', ['wrong_state','forged_reward','missing_trace','dev','long'])
def test_sft_rejects_failed_nontraining_incomplete_and_truncated_episodes(problem):
    record, result = real_success('dev' if problem == 'dev' else 'train')
    if problem == 'wrong_state':
        result['environment']['users'].clear()
    if problem == 'forged_reward':
        result['reward'] = 0.
    if problem == 'missing_trace':
        record['triplets'] = []
    if problem in ('dev','missing_trace'):
        with pytest.raises(ValueError):
            verified_rows(record, result)
    else:
        rows, reason = verified_rows(record, result, max_length=4 if problem == 'long' else 8192)
        assert not rows and reason != 'accepted'


def test_actual_trl_lora_step_uses_response_mask_and_saves_adapter(tmp_path, monkeypatch):
    torch = pytest.importorskip('torch')
    pytest.importorskip('trl')
    from transformers import AutoTokenizer, Qwen3Config, Qwen3ForCausalLM
    from kb.rl import sft
    from safetensors.torch import load_file
    torch.set_num_threads(2)
    torch.manual_seed(17)
    tokenizer = AutoTokenizer.from_pretrained('/data/zjr/enterprise_kb_agent/models/Qwen3-8B',
                                               local_files_only=True)
    config = Qwen3Config(vocab_size=len(tokenizer), hidden_size=32, intermediate_size=64,
        num_hidden_layers=1, num_attention_heads=4, num_key_value_heads=2, head_dim=8,
        max_position_embeddings=128, pad_token_id=tokenizer.pad_token_id, attention_dropout=0.)
    model = Qwen3ForCausalLM(config)
    model_path = tmp_path/'tiny_qwen'
    model.save_pretrained(model_path)
    tokenizer.save_pretrained(model_path)
    ids = tokenizer.apply_chat_template([{'role':'user','content':'Read status.'},
        {'role':'assistant','content':'The status is Open.'}], tokenize=True, enable_thinking=False)
    prompt = tokenizer.apply_chat_template([{'role':'user','content':'Read status.'}], tokenize=True,
                                           add_generation_prompt=True, enable_thinking=False)
    assert ids[:len(prompt)] == prompt
    row = {'input_ids':ids,'completion_mask':[0]*len(prompt)+[1]*(len(ids)-len(prompt))}
    raw = (json.dumps(row)+'\n').encode()
    dataset = tmp_path/'dataset';dataset.mkdir()
    (dataset/'train.jsonl').write_bytes(raw)
    (dataset/'manifest.json').write_text(json.dumps({'dataset_sha256':sft.digest(raw),'rows':1,
        'train_role_rows':{'specialists':1},'max_length':128,
        'successful_episodes_by_family':{'unit':1}}))
    from trl.trainer.sft_trainer import DataCollatorForLanguageModeling
    batch = DataCollatorForLanguageModeling(tokenizer.pad_token_id)([row])
    assert (batch['labels'][0,:len(prompt)] == -100).all()
    assert torch.equal(batch['labels'][0,len(prompt):], torch.tensor(ids[len(prompt):]))
    output = tmp_path/'sft'
    monkeypatch.setattr(sft,'DATA_ROOT',tmp_path)
    metrics = sft.train(dataset,output,model_path=model_path,steps=1)
    assert metrics['train_loss'] > 0
    assert json.loads((output/'result.json').read_text())['global_step'] == 1
    tensors = load_file(str(output/'adapter/adapter_model.safetensors'))
    assert any(value.count_nonzero() for key,value in tensors.items() if 'lora_B' in key)
    from peft import PeftModel
    from transformers import AutoModelForCausalLM
    unmerged = PeftModel.from_pretrained(AutoModelForCausalLM.from_pretrained(model_path), output/'adapter').eval()
    inputs = torch.tensor(ids)[None]
    with torch.no_grad():
        expected = unmerged(inputs).logits
    merged_path = tmp_path/'merged_policy'
    sft.merge_adapter(output/'adapter',merged_path,base_model=model_path)
    loaded = AutoModelForCausalLM.from_pretrained(merged_path).eval()
    with torch.no_grad():
        actual = loaded(inputs).logits
    torch.testing.assert_close(actual,expected,rtol=1e-4,atol=1e-5)
