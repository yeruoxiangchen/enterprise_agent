"""The benchmark business trace contains real API attempts, not schema tools."""
import asyncio
from pathlib import Path

import yaml

from kb.benchmark.crm_deep_adapter import DeepCRMAdapter
from kb.config import Settings
from kb.context import ContextBudget
from test_deep_harness import ScriptModel,call,finish


def adapter(tmp_path,monkeypatch,replies):
    from harness.tools.environment import create_environment
    import harness.tools.domains
    root=Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
    settings=Settings(root=tmp_path,mode='agent',llm_url='http://local.invalid/v1',
                      llm_model='script',harness_goal_review=False)
    monkeypatch.setattr(ContextBudget,'count',lambda *a:(100,'test'))
    return DeepCRMAdapter(create_environment('itsm'),settings,
        yaml.safe_load((root/'src/agents/itsm.yaml').read_text()),
        (root/'src/tasks/policies/itsm_policy.md').read_text(),tmp_path,
        model=ScriptModel(replies=replies))


def test_schema_finishing_is_excluded_from_native_business_tool_trace(tmp_path,monkeypatch):
    subject=adapter(tmp_path,monkeypatch,[call('get_ticket',{'ticket_id':'TCK-1002'}),
        *finish('The status is In Progress.')])
    async def run():
        try:
            response=await subject.run('What is the status of TCK-1002?',[])
            assert [c['name']for c in response.tool_calls_made]==['get_ticket']
            assert all(c['backend_executed']and c['executed']for c in response.tool_calls_made)
            assert subject.model_calls==3
            assert any(a['name']=='CompletionMetadata'for a in subject.engine.attempts)
        finally:await subject.aclose()
    asyncio.run(run())


def test_rejected_business_attempt_is_retained_without_claiming_execution(tmp_path,monkeypatch):
    subject=adapter(tmp_path,monkeypatch,[call('delete_ticket',{'ticket_id':'TCK-1003'}),
        *finish('The requested change remains unfinished.','partial'),call('GoalRequirements',{
            'kind':'business_operations','actions':[{'tool':'delete_ticket','arguments':{'ticket_id':'TCK-1003'},'after':[]}]}),call('ProgressDecision',{
            'is_request_satisfied':False,'is_progress_being_made':False,'is_in_loop':True,
            'next_action':'stop','instruction':'The requested change remains unfinished.'})])
    async def run():
        try:
            response=await subject.run('I am admin.rlee. Delete TCK-1003.',[])
            assert [c['name']for c in response.tool_calls_made]==['delete_ticket']
            record=response.tool_calls_made[0]
            assert not record['backend_executed']and not record['executed']
            assert 'TCK-1003'in subject.environment.get_state()['tickets']
        finally:await subject.aclose()
    asyncio.run(run())
