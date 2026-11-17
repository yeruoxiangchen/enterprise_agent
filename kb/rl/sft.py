"""Verified successful business trajectories -> native TRL SFTTrainer.

Train on the original assistant token IDs from the training proxy, preserving
the exact tools, tool results and template in their original prompt. User and
tool tokens stay masked. No conversation is reconstructed from final answers.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from .runtime import write_json
from .tasks import DATA_ROOT, unpack_task


def digest(data):
    return hashlib.sha256(data).hexdigest()


def data_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to(DATA_ROOT):
        raise ValueError('SFT data and outputs belong under /data/zjr/enterprise_kb_agent/rl')
    return path


def verified_rows(record, result, *, max_length=8192):
    """Rescore real actions/state before admitting a whole training episode."""
    from .baseline import rescore
    rollout = record['rollout']
    if rollout['mode'] != 'train':
        return [], 'non_training_rollout'
    sample = unpack_task(rollout['input'])
    if sample['split'] != 'train' or result['split'] != 'train':
        raise ValueError('Development / heldout examples cannot enter SFT')
    if sample['id'] != result['task_id'] or result['task_sha256'] != rollout['input']['task_sha256']:
        raise ValueError('SFT result is bound to a different task payload')
    if rescore(sample, result)['reward'] != 1. or result['reward'] != 1.:
        return [], 'business_verification_failed'
    triplets = record['triplets']
    expected = result['policy_model_calls_completed']
    if len(triplets) != expected:
        raise ValueError('Selected-role calls are missing from original token traces')
    if not triplets:
        return [], 'no_selected_role_actions'
    rows = []
    for turn, triplet in enumerate(triplets):
        prompt, response = triplet['prompt'].get('token_ids'), triplet['response'].get('token_ids')
        if not prompt or not response or any(type(i) is not int or i < 0 for i in prompt + response):
            raise ValueError('SFT requires complete original integer token IDs')
        if len(prompt) + len(response) > max_length:
            return [], 'whole_rollout_too_long'
        row = {'input_ids': prompt + response, 'completion_mask': [0]*len(prompt) + [1]*len(response),
            'provenance': {'task_id': sample['id'], 'family': sample['family'],
                'split': 'train', 'task_sha256': result['task_sha256'],
                'rollout_id': rollout['rollout_id'], 'turn': turn,
                'train_role': result['train_role'], 'business_reward': 1.}}
        rows.append(row)
    return rows, 'accepted'


def export_successes(run, output, *, max_length=8192):
    run, output = data_path(run), data_path(output)
    if output.exists():
        raise FileExistsError('Use a new immutable SFT dataset directory')
    output.mkdir(parents=True)
    counts, families, skipped, rows, seen = Counter(), Counter(), Counter(), [], set()
    for path in sorted((run/'traces').glob('*.json')):
        raw = path.read_bytes()
        record = json.loads(raw)
        if record['rollout']['mode'] != 'train':
            skipped['non_training_rollout'] += 1
            continue
        rollout_id = record['rollout']['rollout_id']
        results = list((run/'rollouts'/rollout_id).glob('*/result.json'))
        if len(results) != 1:
            raise ValueError('Each exported rollout needs one unambiguous final attempt')
        result_raw = results[0].read_bytes()
        result = json.loads(result_raw)
        admitted, reason = verified_rows(record, result, max_length=max_length)
        if not admitted:
            skipped[reason] += 1
            continue
        for row in admitted:
            signature = digest(json.dumps([row['input_ids'], row['completion_mask']]).encode())
            if signature in seen:
                skipped['duplicate_call'] += 1
                continue
            seen.add(signature)
            row['provenance'].update(source_trace=str(path), source_trace_sha256=digest(raw),
                source_result=str(results[0]), source_result_sha256=digest(result_raw))
            rows.append(row)
            counts[result['train_role']] += 1
        families[result['family']] += 1
    data = ''.join(json.dumps(row, separators=(',', ':'))+'\n' for row in rows).encode()
    (output/'train.jsonl').write_bytes(data)
    report = {'source_run': str(run), 'source_manifest_sha256': digest((run/'source-manifest.json').read_bytes()),
        'rows': len(rows), 'train_role_rows': dict(counts), 'successful_episodes_by_family': dict(families),
        'skipped': dict(skipped), 'max_length': max_length, 'dataset_sha256': digest(data),
        'format': 'original token IDs, response-only completion_mask; TRL 0.21.0',
        'heldout_used': False}
    write_json(output/'manifest.json', report)
    return report


def train(dataset, output, *, model_path, steps=32, learning_rate=1e-5):
    """Standard response-only LoRA SFT; defaults fit one otherwise-free 3090."""
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTConfig, SFTTrainer
    import torch
    dataset, output = data_path(dataset), data_path(output)
    manifest = json.loads((dataset/'manifest.json').read_text())
    raw = (dataset/'train.jsonl').read_bytes()
    if digest(raw) != manifest['dataset_sha256'] or not manifest['rows']:
        raise ValueError('SFT dataset is empty or its frozen hash changed')
    if set(manifest['train_role_rows']) != {'specialists'}:
        raise ValueError('This SFT job trains the shared specialist policy only')
    if output.exists():
        raise FileExistsError('Use a new SFT output directory')
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    cuda = torch.cuda.is_available()
    model = AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True,
        torch_dtype=torch.bfloat16 if cuda else torch.float32, attn_implementation='sdpa')
    model.config.use_cache = False
    rows = [json.loads(line) for line in raw.splitlines()]
    args = SFTConfig(output_dir=str(output), max_steps=steps, learning_rate=learning_rate,
        per_device_train_batch_size=1, gradient_accumulation_steps=8,
        bf16=cuda, use_cpu=not cuda, gradient_checkpointing=True,
        gradient_checkpointing_kwargs={'use_reentrant': False},
        max_length=manifest['max_length'], packing=False, completion_only_loss=True,
        dataset_kwargs={'skip_prepare_dataset': True}, report_to='none',
        logging_steps=1, save_strategy='no', seed=20261117)
    trainer = SFTTrainer(model=model, processing_class=tokenizer, args=args,
        train_dataset=Dataset.from_list([{k: row[k] for k in ('input_ids','completion_mask')} for row in rows]),
        peft_config=LoraConfig(r=16, lora_alpha=32, lora_dropout=0.,
                              target_modules='all-linear', task_type='CAUSAL_LM'))
    write_json(output/'training-input.json', {'model': str(model_path), 'dataset': str(dataset),
        'dataset_sha256': manifest['dataset_sha256'], 'steps': steps, 'library': 'trl==0.21.0'})
    try:
        result = trainer.train()
        trainer.save_model(str(output/'adapter'))
        tokenizer.save_pretrained(str(output/'adapter'))
        write_json(output/'result.json', {'status': 'complete', 'global_step': trainer.state.global_step,
            'metrics': result.metrics, 'adapter': str(output/'adapter'),
            'successful_episodes_by_family': manifest['successful_episodes_by_family']})
        return result.metrics
    finally:
        del trainer, model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def merge_adapter(adapter, output, *, base_model):
    """PEFT's checked merge creates a standalone policy base for native veRL.

    The frozen supervisor continues to use its original model. The RL reference
    and policy start from this SFT base, with a new trainable RL LoRA adapter.
    """
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    adapter, output = data_path(adapter), data_path(output)
    if output.exists():
        raise FileExistsError('Use a new merged policy directory')
    base = AutoModelForCausalLM.from_pretrained(base_model, local_files_only=True,
                                               torch_dtype='auto', attn_implementation='sdpa')
    model = PeftModel.from_pretrained(base, adapter, local_files_only=True)
    merged = model.merge_and_unload(safe_merge=True)
    merged.save_pretrained(output, max_shard_size='2GB', safe_serialization=True)
    AutoTokenizer.from_pretrained(base_model, local_files_only=True).save_pretrained(output)
    report = {'adapter': str(adapter), 'base_model': str(base_model), 'output': str(output),
              'merge': 'peft.merge_and_unload(safe_merge=True)'}
    write_json(output/'sft-lineage.json', report)
    return report


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('--run', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--max-length', type=int, default=8192)
    fit = sub.add_parser('train')
    fit.add_argument('--dataset', type=Path, required=True)
    fit.add_argument('--output', type=Path, required=True)
    fit.add_argument('--model', default='/data/zjr/enterprise_kb_agent/models/Qwen3-8B')
    fit.add_argument('--steps', type=int, default=32)
    merge = sub.add_parser('merge')
    merge.add_argument('--adapter', type=Path, required=True)
    merge.add_argument('--output', type=Path, required=True)
    merge.add_argument('--base-model', default='/data/zjr/enterprise_kb_agent/models/Qwen3-8B')
    args = parser.parse_args()
    if args.command == 'export':
        report = export_successes(args.run, args.output, max_length=args.max_length)
    elif args.command == 'train':
        report = train(args.dataset, args.output, model_path=args.model, steps=args.steps)
    else:
        report = merge_adapter(args.adapter, args.output, base_model=args.base_model)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
