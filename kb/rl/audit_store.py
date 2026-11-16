"""Persist upstream traces before the local in-memory store is released."""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import json
from pathlib import Path

from .runtime import write_json


async def export(store_url,output):
    import agentlightning as agl
    store=agl.LightningStoreClient(store_url)
    try:
        return await _export(store,Path(output))
    finally:
        await store.close()


async def _export(store,output):
    import agentlightning as agl
    rollouts=await store.query_rollouts()
    groups=defaultdict(list)
    rows=[]
    for rollout in rollouts:
        spans=await store.query_spans(rollout.rollout_id)
        triplets=agl.LlmProxyTraceToTriplet().adapt(spans)
        record={'rollout':rollout.model_dump(mode='json'),
                'spans':[s.model_dump(mode='json')for s in spans],
                'triplets':[t.model_dump(mode='json')for t in triplets]}
        write_json(output/'traces'/f'{rollout.rollout_id}.json',record)
        rewards=[t.reward for t in triplets if t.reward is not None]
        reward=rewards[-1]if rewards else None
        group=rollout.input.get('id','unknown')
        groups[group].append(reward)
        rows.append({'rollout_id':rollout.rollout_id,'task_id':group,'status':rollout.status,
                     'triplets':len(triplets),'reward':reward,
                     'token_ids_complete':bool(triplets) and all(
                         t.prompt.get('token_ids') and t.response.get('token_ids') for t in triplets)})
    mixed=sum(len(set(v for v in values if v is not None))>1 for values in groups.values())
    summary={'rollouts':len(rows),'training_proxy_roles':['supervisor'],
             'groups':len(groups),'groups_with_mixed_outcomes':mixed,'rows':rows,
             'scope':'training plumbing and executable outcome feedback; final-answer semantic accuracy not scored'}
    write_json(output/'trajectory-summary.json',summary)
    return summary


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--store',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(asyncio.run(export(args.store,args.output)),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
