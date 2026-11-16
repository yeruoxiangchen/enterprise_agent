"""Local, authenticated upstream training proxy configuration."""
import os
import json


def _attribution_middleware():
    from agentlightning.llm_proxy import RolloutAttemptMiddleware
    class AttributedRolloutMiddleware(RolloutAttemptMiddleware):
        async def dispatch(self, request, call_next):
            async def attributed_next(stamped):
                # LiteLLM 1.78 no longer copies arbitrary HTTP headers into its
                # standard logging metadata. Preserve only AGL's three routing
                # identifiers through its supported metadata field.
                headers = dict(stamped.scope['headers'])
                keys = ('x-rollout-id', 'x-attempt-id', 'x-sequence-id')
                attribution = {k: headers[k.encode()].decode() for k in keys
                               if k.encode() in headers}
                if len(attribution) == len(keys) and stamped.method == 'POST':
                    payload = json.loads(await stamped.body())
                    metadata = dict(payload.get('metadata') or {})
                    metadata['requester_custom_headers'] = attribution
                    payload['metadata'] = metadata
                    body = json.dumps(payload).encode()
                    stamped._body = body
                    stamped.scope['headers'] = [(k, v) for k, v in stamped.scope['headers']
                                                if k.lower() != b'content-length']
                    stamped.scope['headers'].append((b'content-length', str(len(body)).encode()))
                    stamped.__dict__.pop('_headers', None)
                return await call_next(stamped)
            return await super().dispatch(request, attributed_next)
    return AttributedRolloutMiddleware


def make_training_proxy(store, *, model_list=None):
    from agentlightning import LLMProxy
    from agentlightning.utils.server_launcher import PythonServerLauncherArgs
    key=os.environ['KB_RL_PROXY_KEY']
    # Recent OTEL exporters cache their HTTP endpoint in a client object. AGL
    # 0.3 updates only the old _endpoint attribute, so supply the store endpoint
    # before the callback/exporter is constructed in this private job process.
    os.environ['OTEL_EXPORTER_OTLP_TRACES_ENDPOINT']=store.otlp_traces_endpoint()
    return LLMProxy(store=store,model_list=model_list,
        launcher_args=PythonServerLauncherArgs(host='127.0.0.1',access_host='127.0.0.1',
            launch_mode='mp',startup_timeout=180,healthcheck_url='/health/liveliness'),
        callbacks=['return_token_ids','opentelemetry','logprobs'],
        middlewares=[_attribution_middleware(), 'stream_conversion'],
        litellm_config={'general_settings':{'master_key':key}})
