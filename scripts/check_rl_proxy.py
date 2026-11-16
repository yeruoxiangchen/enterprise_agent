"""Exercise the real AGL HTTP proxy, authentication and raw-token telemetry on CPU."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time

import httpx
from fastapi import FastAPI, Request
import uvicorn

from kb.rl.proxy import make_training_proxy


def port():
    with socket.socket()as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


async def check(folder):
    import agentlightning as agl
    folder.mkdir(parents=True,exist_ok=False)
    os.environ['NO_PROXY']='*'
    os.environ['no_proxy']='*'
    os.environ['KB_RL_PROXY_KEY']='sk-'+secrets.token_hex(32)
    app=FastAPI()
    observed=[]
    @app.get('/health')
    def health():return {'status':'ok'}
    @app.post('/v1/chat/completions')
    async def completion(request:Request):
        payload=await request.json();observed.append(payload)
        return {'id':'chatcmpl-proxy-check','object':'chat.completion','created':int(time.time()),
            'model':'wire-check','prompt_token_ids':[11,12],
            'choices':[{'index':0,'message':{'role':'assistant','content':'pong'},
                        'finish_reason':'stop','token_ids':[13,14],
                        'logprobs':{'content':[
                            {'token':'po','logprob':-.1,'bytes':[112,111],'top_logprobs':[]},
                            {'token':'ng','logprob':-.2,'bytes':[110,103],'top_logprobs':[]}]}}],
            'usage':{'prompt_tokens':2,'completion_tokens':2,'total_tokens':4}}
    backend_port,store_port=port(),port()
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=backend_port,log_level='warning'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    with (folder/'store.log').open('w')as log:
        proc=subprocess.Popen(['/data/zjr/enterprise_kb_agent/rl/envs/runner/bin/agl',
            'store','--host','127.0.0.1','--port',str(store_port)],stdout=log,stderr=subprocess.STDOUT)
    proxy=None
    store=None
    try:
        async with httpx.AsyncClient(timeout=30,trust_env=False)as client:
            for _ in range(60):
                try:
                    if (await client.get(f'http://127.0.0.1:{store_port}/v1/agl/health')).status_code==200:break
                except httpx.HTTPError:pass
                await asyncio.sleep(1)
            else:raise RuntimeError('Store did not start')
            store=agl.LightningStoreClient(f'http://127.0.0.1:{store_port}')
            rollout=await store.start_rollout(input={'id':'wire-check'},mode='train')
            proxy=make_training_proxy(store,model_list=[{'model_name':'wire-check','litellm_params':{
                'model':'hosted_vllm/wire-check','api_base':f'http://127.0.0.1:{backend_port}/v1'}}])
            await proxy.start()
            endpoint=proxy.as_resource(rollout.rollout_id,rollout.attempt.attempt_id).endpoint
            response=await client.post(endpoint+'/chat/completions',
                headers={'Authorization':'Bearer '+os.environ['KB_RL_PROXY_KEY']},
                json={'model':'wire-check','messages':[{'role':'user','content':'ping'}]})
            response.raise_for_status()
            assert response.json()['choices'][0]['message']['content']=='pong',response.text
            assert observed[-1].get('return_token_ids')is True,observed
            assert observed[-1].get('logprobs')in (True,1),observed
            unauthorized=await client.post(endpoint+'/chat/completions',
                json={'model':'wire-check','messages':[{'role':'user','content':'ping'}]})
            assert unauthorized.status_code in (401,403),unauthorized.text
            triplets=[]
            for _ in range(30):
                spans=await store.query_spans(rollout.rollout_id)
                triplets=agl.LlmProxyTraceToTriplet().adapt(spans)
                if triplets:break
                await asyncio.sleep(1)
            assert len(triplets)==1,len(triplets)
            assert triplets[0].prompt['token_ids']==[11,12]
            assert triplets[0].response['token_ids']==[13,14]
            from kb.rl.audit_store import export
            archive=await export(f'http://127.0.0.1:{store_port}',folder/'archive')
            assert archive['rollouts']==1 and archive['rows'][0]['token_ids_complete'],archive
            result={'passed':True,'authenticated_request':True,'unauthenticated_rejected':True,
                'trace_archive_complete':True,
                'original_prompt_token_ids':[11,12],'original_response_token_ids':[13,14],
                'scope':'real upstream proxy and telemetry with a controlled HTTP backend; no model quality claim'}
            (folder/'result.json').write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps(result))
    finally:
        if proxy:await proxy.stop()
        if store:await store.close()
        proc.terminate()
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        server.should_exit=True;thread.join(timeout=5)


if __name__=='__main__':
    import sys
    asyncio.run(check(Path(sys.argv[1])))
