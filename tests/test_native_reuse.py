import asyncio
import copy
import json
from pathlib import Path

import pytest
from harness.tools.environment import create_environment
import harness.tools.domains

from harness_native_reuse import restore,replay_business


def test_changed_model_budget_rejects_reuse_before_copying_results(tmp_path):
    source=tmp_path/'old';source.mkdir()
    (source/'protocol.json').write_text(json.dumps({'model':'local','context_tokens':8192}))
    with pytest.raises(ValueError,match='context_tokens'):
        asyncio.run(restore(source,tmp_path/'new',{'model':'local','context_tokens':4096}, {},tmp_path/'upstream'))


def test_business_replay_checks_actual_observations_and_final_state():
    async def check():
        env=create_environment('itsm');registry=env.create_registry()
        result=await registry.execute_with_status('get_ticket',{'ticket_id':'TCK-1001'})
        row={'task_id':'itsm_ops_001_lookup_ticket_by_id','case_kind':'ordinary_read',
            'audit':[{'backend_executed':True,'name':'get_ticket','arguments':{'ticket_id':'TCK-1001'},
                      'ok':result.ok,'result':result.result}], 'environment':env.get_state()}
        upstream=Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
        assert await replay_business(row,upstream)==1
        changed=copy.deepcopy(row)
        value=json.loads(changed['audit'][0]['result']);value['status']='Closed'
        changed['audit'][0]['result']=json.dumps(value)
        with pytest.raises(ValueError,match='Business observation changed'):
            await replay_business(changed,upstream)
        changed=copy.deepcopy(row);changed['environment']['tickets']['TCK-1001']['status']='Closed'
        with pytest.raises(ValueError,match='Initial environment/tool contract changed'):
            await replay_business(changed,upstream)
    asyncio.run(check())
