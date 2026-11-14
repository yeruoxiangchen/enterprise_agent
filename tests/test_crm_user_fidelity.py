import asyncio
import json
from types import SimpleNamespace

import pytest

from harness.user_simulator import STOP_SIGNAL
from kb.benchmark.crm_dialogue import PairedUserSimulator, SimulatorFidelityError
from kb.benchmark.crm_metrics import local_verdict, error_category
from test_crm_benchmark import adapter


def decision(message='', stop=False):
    return {'stop': stop, 'message': message, 'exit_conditions_met': stop}


def review(valid=True, complete=False, continuation='needs_agent_action', reason='Faithful to the user goal.'):
    return {'valid': valid, 'goals_complete': complete, 'continuation': continuation, 'reason': reason}


class Client:
    def __init__(self, values):
        self.values = list(values)
        self.usage, self.seed, self.requests = {'num_calls': 0}, 42, []
    async def chat(self, **kwargs):
        self.requests.append(kwargs)
        self.usage['num_calls'] += 1
        value = self.values.pop(0)
        return SimpleNamespace(content=value if isinstance(value, str) else json.dumps(value), finish_reason='stop')


def simulator(tmp_path, client, **kwargs):
    sim = PairedUserSimulator(client, opening_path=tmp_path/'opening.json', identity={'seed': 42},
        adapter=adapter(tmp_path/'agent'), arm='single_v6', verify_fidelity=True, **kwargs)
    sim.reset('Ask the status of TCK-1001 and stop once the agent states it.')
    return sim


def test_agent_answer_echo_is_repaired_before_it_enters_user_history(tmp_path):
    answer = 'Ticket TCK-1001 is Open, priority High.'
    client = Client([decision('What is the status of TCK-1001?'), review(),
        decision(answer), review(False, True, 'complete', 'The customer goal is complete; this is an echo.'),
        decision(stop=True), review(True, True, 'complete')])
    sim = simulator(tmp_path, client)
    async def run():
        await sim.step('')
        assert await sim.step(answer) == STOP_SIGNAL
    asyncio.run(run())
    assert sum(m.content == answer for m in sim._messages) == 1
    assert sim._messages[-1].content == STOP_SIGNAL
    assert client.usage['num_calls'] == 6
    assert 'ROLE MAPPING' in client.requests[0]['messages'][0].content
    assert 'User:' in client.requests[3]['messages'][-1].content


def test_unrepairable_user_proxy_is_an_evaluation_error_not_an_agent_failure(tmp_path):
    client = Client([decision('What is the status of TCK-1001?'), review(False, False, reason='Invented a new demand.'),
                     decision('A different invented demand.'), review(False, False, reason='Still unfaithful.')])
    sim = simulator(tmp_path, client)
    with pytest.raises(SimulatorFidelityError):
        asyncio.run(sim.step(''))
    error = 'SimulatorFidelityError: User proxy drift'
    assert local_verdict(None, error) is None
    assert error_category({'error': error}) == 'simulator_fidelity'
    assert not (tmp_path/'opening.json').exists()


def test_required_scripted_pushback_is_not_misclassified_as_agent_stall(tmp_path):
    client = Client([v for _ in range(4) for v in [decision('Please reconsider my request.'),
        review(True, False, 'scripted_pushback')]])
    sim = simulator(tmp_path, client)
    sim.reset('Keep requesting reconsideration for at least four exchanges before ending.')
    async def run():
        for _ in range(4):
            assert await sim.step('The decision is unchanged.') == 'Please reconsider my request.'
    asyncio.run(run())
    assert sim.repeated == 0


def test_native_verifier_retains_opening_and_unfinished_second_phase(tmp_path):
    first = 'Please add "Asset tag received from procurement." to TCK-1002.'
    next_step = 'Now set the status of TCK-1002 to Resolved.'
    client = Client([decision(first), 'PASS: The first phase is requested.',
                     decision(next_step), 'PASS: The status update is still required.'])
    sim = simulator(tmp_path, client, verification_mode='upstream')
    sim.reset('First ask to log "Asset tag received from procurement." on TCK-1002. '
              'After confirmation, ask to set its status to Resolved. Stop only after both are done.')
    async def run():
        assert await sim.step('') == first
        assert await sim.step('The note was added. Status remains In Progress.') == next_step
    asyncio.run(run())
    verifier = client.requests[3]
    prompt = verifier['messages'][0].content
    assert 'Extract ALL goals and exit conditions' in prompt
    assert 'Status remains In Progress' in prompt and next_step in prompt
    assert verifier.get('response_format') is None
    assert 'candidate_stop' in sim.last_review.model_dump()
    assert 'goals_complete' not in sim.last_review.model_dump()


def test_native_verifier_rejects_premature_stop_without_rewriting_agent_score(tmp_path):
    client = Client([decision(stop=True), 'FAIL: No agent has provided the ticket status.',
                     decision('What is the status of TCK-1001?'), 'PASS: The user is asking for the status.'])
    sim = simulator(tmp_path, client, verification_mode='upstream')
    assert asyncio.run(sim.step('')) == 'What is the status of TCK-1001?'
    assert client.usage['num_calls'] == 4


def test_native_verifier_keeps_deterministic_role_echo_guard(tmp_path):
    answer = 'Ticket TCK-1001 is Open, priority High.'
    client = Client([decision('What is its status?'), 'PASS',
                     decision(answer), 'PASS', decision(stop=True), 'PASS'])
    sim = simulator(tmp_path, client, verification_mode='upstream')
    async def run():
        await sim.step('')
        assert await sim.step(answer) == STOP_SIGNAL
    asyncio.run(run())
    assert sum(m.content == answer for m in sim._messages) == 1


def test_semantic_retry_keeps_actual_agent_message_intact_and_includes_rejected_candidate(tmp_path):
    agent_reply = 'Please provide the user ID for the deletion permission check.'
    rejected = 'The ticket has already been deleted.'
    reason = 'Provide the known identity as the customer; no deletion was confirmed.'
    client = Client([decision('Please delete TCK-1004.'), review(),
                     decision(rejected), review(False, False, reason=reason),
                     decision('My user ID is admin.rlee.'), review()])
    sim = simulator(tmp_path, client)
    sim.reset('Request deletion of TCK-1004. Your user ID is admin.rlee; provide it if asked.')
    async def run():
        await sim.step('')
        assert await sim.step(agent_reply) == 'My user ID is admin.rlee.'
    asyncio.run(run())
    retry_messages = client.requests[4]['messages']
    assert retry_messages[-1].content == agent_reply
    assert rejected in retry_messages[0].content and reason in retry_messages[0].content
    assert 'not an Agent utterance' in retry_messages[0].content
    assert all(m.content != rejected for m in sim._messages)
