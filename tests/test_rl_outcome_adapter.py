"""Real upstream span parsing and legacy batch behavior for unacted roles."""
import asyncio
import json
from types import SimpleNamespace
import pytest

pytest.importorskip('torch')


@pytest.mark.parametrize('completed_calls',[0,1])
def test_annotation_only_episode_is_verified_or_rejected_as_trace_loss(tmp_path,monkeypatch,completed_calls):
    import agentlightning as agl
    from agentlightning.types import Span
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext
    from kb.rl.verl_contract import StrictBusinessDaemon,NoPolicyActions
    span=Span.from_opentelemetry(ReadableSpan(name='agentlightning.annotation',
        context=SpanContext(trace_id=1,span_id=2,is_remote=False),start_time=1_000_000_000,
        end_time=2_000_000_000,attributes={'agentlightning.reward.0.name':'primary',
                                        'agentlightning.reward.0.value':1.}),
        rollout_id='ro-test',attempt_id='at-test',sequence_id=1)
    class Store:
        async def query_spans(self,*args,**kwargs):return [span]
        async def query_attempts(self,*args,**kwargs):return [SimpleNamespace(attempt_id='at-test')]
    path=tmp_path/'rollouts/ro-test/at-test/result.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'task_id':'task','policy_model_calls_completed':completed_calls,'reward':1.}))
    monkeypatch.setenv('KB_RL_RUN_DIR',str(tmp_path))
    daemon=object.__new__(StrictBusinessDaemon)
    daemon.store=Store();daemon.adapter=agl.LlmProxyTraceToTriplet()
    rollout=SimpleNamespace(rollout_id='ro-test',input={'id':'task'},mode='train',
                            resources_id='resource',metadata={})
    if completed_calls:
        with pytest.raises(RuntimeError,match='missing from the training trace'):
            asyncio.run(daemon._validate_data_v1(rollout))
    else:
        result=asyncio.run(daemon._validate_data_v1(rollout))
        assert result.final_reward==1. and result.triplets==[]
        daemon.is_train=True;daemon._completed_rollouts_v0={'ro-test':result};daemon._total_tasks_queued=1
        with pytest.raises(NoPolicyActions)as caught:
            daemon.get_train_data_batch()
        assert caught.value.metrics['training/reward']==1.
