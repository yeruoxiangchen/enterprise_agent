"""Remove masked prompt columns shared by a training batch, preserving all observations."""


def trim_shared_prompt_padding(data):
    batch=data.batch
    width=batch['prompts'].shape[-1]
    active=batch['attention_mask'][:,:width].any(dim=0).nonzero(as_tuple=True)[0]
    if active.numel()==0:
        raise ValueError('Training batch contains no prompt tokens')
    removed=int(active[0].item())
    if removed:
        batch['prompts']=batch['prompts'][...,removed:].contiguous()
        for key in ('input_ids','attention_mask','position_ids'):
            batch[key]=batch[key][...,removed:].contiguous()
    return removed
