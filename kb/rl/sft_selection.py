"""Select verified original responses while masking errors in recovered episodes.

Whole-episode business assertions still apply. In recovered episodes only
audited successful calls and successful native task handoffs become targets;
all natural-language replies and invalid calls remain masked context.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator,ValidationError

from .tasks import DATA_ROOT


def select_responses(record,result):
    from native_rl_diagnostic import message_signature
    root=Path(record['wire_root']).resolve()
    if not root.is_relative_to(DATA_ROOT.resolve()):raise ValueError('Wire evidence must stay on project data storage')
    records=[json.loads(line)for line in(root/'wire.jsonl').read_text().splitlines()]
    wires={r['index']:r for r in records}
    if len(wires)!=len(records):raise ValueError('Ambiguous original wire indices')
    errors={e['call_id']for e in result.get('routing_errors',[])}
    audit={a['id']:a for a in result['audit']}
    recovered=bool(errors or any(e.get('result')!='satisfied'for e in result.get('review_evaluations',[])))
    receipts={m['tool_call_id']for r in records for m in r['payload']['messages']
        if m.get('role')=='tool'and m.get('tool_call_id')}
    selected=[];decisions=[]
    for index,triplet in enumerate(record['triplets']):
        source=triplet['source'];wire=wires[source['wire_index']]
        if wire['metadata'].get('rl_role')!='supervisor':raise ValueError('Only supervisor responses enter supervisor SFT')
        digest=hashlib.sha256(wire['request_text'].encode()).hexdigest()
        if digest!=source['request_sha256']or digest!=wire['request_sha256']:
            raise ValueError('Original request bytes changed')
        if json.loads(wire['request_text'])!=wire['payload']:raise ValueError('Original request payload changed')
        response=wire['response'];choice=response['choices'][0]
        if (response['prompt_token_ids']!=triplet['prompt']['token_ids']or
                choice['token_ids']!=triplet['response']['token_ids']):
            raise ValueError('Original token IDs changed')
        if (len(response['prompt_token_ids'])!=response['usage']['prompt_tokens']or
                len(choice['token_ids'])!=response['usage']['completion_tokens']):
            raise ValueError('Original token counts differ from provider usage')
        parsed=json.loads((root/f"parsed-{source['wire_index']:03d}.json").read_text())
        message=choice['message']
        if not parsed['sdk_matches_provider']or message_signature(message)!=message_signature(parsed['sdk_message']):
            raise ValueError('Provider/SDK action mismatch')
        schemas={t['function']['name']:t['function']['parameters']for t in wire['payload'].get('tools',[])if t.get('type')=='function'}
        calls=message.get('tool_calls',[])
        reason='verified_call'if calls else'clean_answer'
        if choice.get('finish_reason')=='length':reason='truncated_response'
        elif not calls and recovered:reason='recovery_answer_masked'
        else:
            for call in calls:
                name=call['function']['name'];ident=call['id']
                if ident in errors or name not in schemas:
                    reason='invalid_tool_call';break
                try:
                    args=json.loads(call['function']['arguments'])
                    Draft202012Validator(schemas[name]).validate(args)
                except (ValueError,ValidationError):
                    reason='invalid_tool_arguments';break
                if name=='task':
                    if ident not in receipts:reason='unverified_handoff';break
                else:
                    actual=audit.get(ident)
                    if not actual or not actual['ok']or not actual['backend_executed']:
                        reason='unverified_tool_outcome';break
        keep=reason in ('verified_call','clean_answer')
        if keep:selected.append(index)
        decisions.append({'turn':index,'wire_index':source['wire_index'],'selected':keep,
            'reason':reason,'tools':[c['function']['name']for c in calls]})
    return selected,decisions
