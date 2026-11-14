import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('roles', [('actor',), ('actor', 'auxiliary')])
def test_model_readiness_tracks_the_number_of_owned_services(tmp_path, monkeypatch, roles):
    import crm_bench_services as services
    seen = []
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url):
            seen.append(url)
            return SimpleNamespace(status_code=200)
    monkeypatch.setattr(services.httpx, 'AsyncClient', lambda **kwargs: Client())
    monkeypatch.setattr(services, 'identity', lambda pid: str(pid))
    model = services.ModelServices(tmp_path, lambda *args, **kwargs: None)
    model.owned = {role: {'pid': i, 'start_ticks': str(i), 'url': f'http://{role}',
        'gpu': str(i), 'log': 'unused'} for i, role in enumerate(roles)}
    asyncio.run(asyncio.wait_for(model.ready(), timeout=.1))
    assert len(seen) == len(roles)


@pytest.mark.parametrize('requested,expected,fraction', [(None, 16384, .78), (8192, 8192, .75)])
def test_short_context_is_bound_to_the_server_command_and_lease(tmp_path, monkeypatch, requested, expected, fraction):
    import crm_bench_services as services
    model_path = tmp_path/'models/Qwen3-8B'
    model_path.mkdir(parents=True)
    (model_path/'config.json').write_text('{}')
    monkeypatch.setattr(services, 'RUNTIME', tmp_path)
    monkeypatch.setattr(services, 'gpu_snapshot', lambda: {'3': {'free_mib': 24000, 'used_mib': 1, 'total_mib': 24576}})
    monkeypatch.setattr(services, 'identity', lambda pid: 'fake-process-start')
    commands = []
    def launch(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(pid=999999)
    monkeypatch.setattr(services.subprocess, 'Popen', launch)
    model = services.ModelServices(tmp_path, lambda *args, **kwargs: None)
    model.start('actor', '3', max_model_len=requested, memory_fraction=fraction)
    command = commands[0]
    assert command[command.index('--max-model-len')+1] == str(expected)
    assert model.owned['actor']['max_model_len'] == expected
    assert command[command.index('--gpu-memory-utilization')+1] == str(fraction)
    assert model.owned['actor']['memory_fraction'] == fraction


def test_actor_budget_matches_the_started_service_context(tmp_path, monkeypatch):
    import crm_small_check as checks
    from kb.benchmark.crm_adapter import CRMAdapter
    from kb.config import Settings
    from harness.models import ChatMessage
    class Captured(Exception):
        pass
    settings = []
    def capture(**kwargs):
        settings.append(kwargs['settings'])
        raise Captured()
    monkeypatch.setattr('kb.benchmark.crm_adapter.CRMAdapter', capture)
    lease = {'url': 'http://127.0.0.1:1', 'model': 'test', 'max_model_len': 8192}
    with pytest.raises(Captured):
        asyncio.run(checks.business_case(tmp_path, lease, 'single_v6', 'query', ['Look up TCK-1001.']))
    assert settings[0].context_tokens == 8192


def test_failed_model_case_is_recorded_with_its_error_and_usage(tmp_path, monkeypatch):
    import crm_small_check as checks
    import json
    class FailedAgent:
        model_calls = 2
        prompt_tokens = 120
        completion_tokens = 40
        usage_missing_calls = 0
        tool_calls = []
        execution_outcomes = []
        def __init__(self, **kwargs):
            self.environment = kwargs['environment']
        async def run(self, *args):
            raise RuntimeError('intake failed')
        def write_environment(self):
            return None
    monkeypatch.setattr('kb.benchmark.crm_adapter.CRMAdapter', FailedAgent)
    lease = {'url': 'http://127.0.0.1:1', 'model': 'test', 'max_model_len': 8192}
    result = asyncio.run(checks.business_case(tmp_path, lease, 'single_v6', 'query', ['Look up TCK-1001.']))
    stored = json.loads((tmp_path/'cases/query__single_v6/result.json').read_text())
    assert stored == result and not result['passed']
    assert result['error'] == 'RuntimeError: intake failed'
    assert result['model_calls'] == 2 and result['prompt_tokens'] == 120


def test_memory_budget_can_fit_auxiliary_service_with_headroom():
    from crm_bench_services import gpu_has_room
    snapshot = {'free_mib': 19662, 'total_mib': 24576}
    assert gpu_has_room(snapshot, .75)
    assert not gpu_has_room(snapshot, .78)
