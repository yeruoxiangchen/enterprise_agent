import asyncio
from pathlib import Path

import pytest
import yaml
from harness.tools.environment import create_environment
import harness.tools.domains

from kb.benchmark.crm_native_adapter import NativeCRMAdapter
from kb.config import Settings
from kb.context import ContextBudget
from test_deep_harness import ScriptModel,call
from test_native_harness import answer


def make_adapter(tmp_path,monkeypatch):
    root=Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
    model=ScriptModel(replies=[])
    monkeypatch.setattr(ContextBudget,'count',lambda *args:(100,'test'))
    adapter=NativeCRMAdapter(create_environment('itsm'),Settings(root=tmp_path),
        yaml.safe_load((root/'src/agents/itsm.yaml').read_text()),
        (root/'src/tasks/policies/itsm_policy.md').read_text(),tmp_path,model=model,
        require_dependency_confirmation=True)
    domain=next(k for k,v in adapter.port.specialists.items()if'close_ticket'in v['tools'])
    model.replies.extend([call('get_ticket',{'ticket_id':'TCK-1002'},'read'),
        call('check_dependencies',{'ticket_id':'TCK-1002'},'deps'),
        call('task',{'subagent_type':domain,'description':'Close TCK-1002 after the user confirms.'},'task'),
        call('close_ticket',{'ticket_id':'TCK-1002'},'close'),answer('Closed.'),answer('The ticket is closed.')])
    return adapter


def test_dependency_closure_pauses_then_records_one_actual_close_after_approval(tmp_path,monkeypatch):
    adapter=make_adapter(tmp_path,monkeypatch)
    async def run():
        try:
            await adapter.run('I am agent.jsmith. Close TCK-1002 after confirmation.',[])
            assert adapter.pending_review
            assert adapter.environment.get_state()['tickets']['TCK-1002']['status']!='Closed'
            response=await adapter.run("Yes, I'm aware, please close it anyway.",[],
                approval={'name':'close_ticket','args':{'ticket_id':'TCK-1002'},'decision':'approve'})
            assert adapter.environment.get_state()['tickets']['TCK-1002']['status']=='Closed'
            closes=[r for r in adapter.tool_calls if r['name']=='close_ticket']
            assert len(closes)==1 and closes[0]['ok']and closes[0]['turn']==1
            assert any(c['name']=='close_ticket'and c['ok']for c in response.tool_calls_made)
            receipt=next(r for r in adapter.engine.journal.records()if r['name']=='close_ticket')
            approved=adapter.engine.journal.db.execute('SELECT created FROM native_user_reviews').fetchone()
            assert receipt['created']>=approved['created']
        finally:await adapter.aclose()
    asyncio.run(run())


def test_approval_for_a_different_ticket_cannot_resume_pending_write(tmp_path,monkeypatch):
    adapter=make_adapter(tmp_path,monkeypatch)
    async def run():
        try:
            await adapter.run('Close TCK-1002 after confirmation.',[])
            with pytest.raises(ValueError,match='does not match'):
                await adapter.run('Approve another ticket.',[],
                    approval={'name':'close_ticket','args':{'ticket_id':'TCK-1003'},'decision':'approve'})
            assert adapter.environment.get_state()['tickets']['TCK-1002']['status']!='Closed'
        finally:await adapter.aclose()
    asyncio.run(run())
