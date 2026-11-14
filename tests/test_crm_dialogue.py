import asyncio
import json
from types import SimpleNamespace

import pytest

from kb.benchmark.crm_dialogue import ConversationStalled, PairedUserSimulator
from kb.benchmark.crm_metrics import error_category, local_verdict, report_markdown, summarize
from test_crm_benchmark import adapter, row


class UserClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.seed = 42
        self.usage = {'num_calls': 0}
        self.seeds_seen = []

    async def chat(self, **kwargs):
        self.seeds_seen.append(self.seed+self.usage['num_calls'])
        self.usage['num_calls'] += 1
        text = self.messages.pop(0)
        return SimpleNamespace(content=json.dumps({'stop': False, 'message': text,
                                                   'exit_conditions_met': False}), finish_reason='stop')


def simulator(tmp_path, client, owner, *, identity=None, arm='single_v6'):
    result = PairedUserSimulator(client, opening_path=tmp_path/'opening.json',
        identity=identity or {'seed': 42, 'model': 'local-user'}, adapter=owner,
        arm=arm, repeated_turns=3)
    result.reset('Ask for the current ticket status, then stop once it is answered.')
    return result


def test_paired_arms_share_validated_opening_and_subsequent_seed_ordinal(tmp_path):
    first_client = UserClient(['Look up TCK-1001.', 'What is its priority?'])
    second_client = UserClient(['What is its priority?'])
    first = simulator(tmp_path, first_client, adapter(tmp_path/'first'))
    second = simulator(tmp_path, second_client, adapter(tmp_path/'second'), arm='multi_v6')
    async def run():
        assert await first.step('') == await second.step('') == 'Look up TCK-1001.'
        assert second_client.usage['num_calls'] == 0
        assert await first.step('It is Open.') == await second.step('It is Open.')
    asyncio.run(run())
    assert first_client.seeds_seen == [42, 43] and second_client.seeds_seen == [43]
    assert first.opening_metadata['message_sha256'] == second.opening_metadata['message_sha256']
    assert second.opening_metadata['reused']


def test_opening_cannot_be_reused_under_a_different_protocol(tmp_path):
    async def run():
        first = simulator(tmp_path, UserClient(['Look up TCK-1001.']), adapter(tmp_path/'first'))
        await first.step('')
        changed = simulator(tmp_path, UserClient([]), adapter(tmp_path/'second'), identity={'seed': 99})
        with pytest.raises(RuntimeError, match='identity'):
            await changed.step('')
    asyncio.run(run())


def test_unchanged_repeated_user_request_is_a_recorded_failure(tmp_path):
    owner = adapter(tmp_path/'agent')
    sim = simulator(tmp_path, UserClient(['Look up TCK-1001.']*3), owner)
    async def run():
        await sim.step('')
        await sim.step('Please clarify.')
        with pytest.raises(ConversationStalled):
            await sim.step('Please clarify.')
    asyncio.run(run())
    error = 'ConversationStalled: repeated without progress'
    assert local_verdict(None, error) is False
    assert error_category({'error': error, 'error_kind': 'user_simulator_divergence'}) == 'conversation_stalled'
    assert 'conversation_stalled' in (tmp_path/'agent/events.jsonl').read_text()


def test_new_tool_evidence_resets_the_repetition_counter(tmp_path):
    owner = adapter(tmp_path/'agent')
    sim = simulator(tmp_path, UserClient(['Look up TCK-1001.']*4), owner)
    async def run():
        await sim.step('')
        await sim.step('Please clarify.')
        owner.tool_calls.append({'name': 'get_ticket', 'arguments': {'ticket_id': 'TCK-1001'},
            'result': '{"status":"Open"}', 'backend_executed': True, 'ok': True})
        await sim.step('It is Open.')
        await sim.step('It is Open.')
    asyncio.run(run())


def test_report_keeps_business_success_and_conversation_end_separate():
    records = [row('single', 0, True), row('multi_v5', 0, True)]
    for item in records:
        item['trace'].update(stopped=False, timed_out=False, turns=[{'role': 'user', 'content': 'Same opening'}])
    records[0]['trace']['tool_calls_detail'] = [
        {'name': 'close_ticket', 'business_outcome': 'committed'},
        {'name': 'close_ticket', 'business_outcome': 'reused'}]
    summary = summarize(records, ['case'], trials=1)
    single = summary['arms']['single']
    assert single['passed'] == 1 and single['normal_stops'] == 0 and single['max_turns_reached'] == 1
    assert single['committed_write_calls'] == 1 and single['reused_write_attempts'] == 1
    assert summary['paired_openings'] == {'same': 1, 'different': 0, 'missing': 0}
    assert '达到最大轮数' in report_markdown(summary)


def test_standard_preflight_freezes_the_new_contract_and_all_public_gold_paths(tmp_path, monkeypatch):
    import crm_benchmark as runner
    monkeypatch.setattr(runner, 'HARNESS_VERSION', 'v6')
    monkeypatch.setattr(runner, 'ARMS', ('single_v6', 'multi_v6'))
    monkeypatch.setattr(runner, 'RUN_TRIALS', 2)
    protocol = asyncio.run(runner.preflight(tmp_path, 'auto'))
    assert protocol['adapter'] == 'business_v6_external_v3'
    assert protocol['expected_trials'] == 264
    assert protocol['paired_user_openings'] and protocol['conversation_stall_repetitions'] == 3
    assert protocol['user_fidelity_validation'] and protocol['user_temperature'] == 0
    assert any(path.endswith('crm_contract.py') for path in protocol['source_sha256'])
