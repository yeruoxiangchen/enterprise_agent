"""Response probabilities and gradients agree after removing shared masked columns."""
import copy
import pytest

torch=pytest.importorskip('torch')


def test_shared_padding_preserves_qwen_response_logits_gradients_and_labels():
    from transformers import Qwen3Config,Qwen3ForCausalLM
    from verl import DataProto
    from kb.rl.padding import trim_shared_prompt_padding
    torch.manual_seed(21)
    config=Qwen3Config(vocab_size=64,hidden_size=32,intermediate_size=64,
        num_hidden_layers=2,num_attention_heads=4,num_key_value_heads=2,head_dim=8,
        max_position_embeddings=128,pad_token_id=0,attention_dropout=0.)
    config._attn_implementation='sdpa'
    original=Qwen3ForCausalLM(config).eval()
    trimmed=copy.deepcopy(original)
    ids=torch.tensor([[0,0,0,0,4,5,6,7,8,9,0,0],[0,0,3,4,5,6,7,8,9,10,11,0]])
    mask=ids.ne(0).long();positions=(mask.cumsum(-1)-1).clamp(min=0)
    data=DataProto.from_dict({'prompts':ids[:,:8].clone(),'responses':ids[:,8:].clone(),
        'input_ids':ids.clone(),'attention_mask':mask.clone(),'position_ids':positions.clone(),
        'token_level_scores':torch.ones(2,4),'is_drop_mask':torch.zeros(2,dtype=torch.bool)})
    assert trim_shared_prompt_padding(data)==2
    assert torch.equal(data.batch['responses'],ids[:,8:])
    assert torch.equal(data.batch['token_level_scores'],torch.ones(2,4))
    left=original(input_ids=ids,attention_mask=mask,position_ids=positions,use_cache=False).logits[:,-5:-1]
    right=trimmed(input_ids=data.batch['input_ids'],attention_mask=data.batch['attention_mask'],
        position_ids=data.batch['position_ids'],use_cache=False).logits[:,-5:-1]
    torch.testing.assert_close(left,right,rtol=1e-5,atol=1e-6)
    targets=ids[:,8:].reshape(-1)
    loss_left=torch.nn.functional.cross_entropy(left.reshape(-1,64),targets,ignore_index=0)
    loss_right=torch.nn.functional.cross_entropy(right.reshape(-1,64),targets,ignore_index=0)
    loss_left.backward();loss_right.backward()
    for a,b in zip(original.parameters(),trimmed.parameters(),strict=True):
        if a.grad is not None:torch.testing.assert_close(a.grad,b.grad,rtol=1e-4,atol=1e-6)
    assert trim_shared_prompt_padding(data)==0
