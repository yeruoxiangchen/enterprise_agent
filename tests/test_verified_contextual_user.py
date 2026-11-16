import asyncio
import json
from types import SimpleNamespace

import pytest

from harness.user_simulator import STOP_SIGNAL
from kb.benchmark.crm_dialogue import VerifiedContextualUserSimulator,SimulatorFidelityError
from kb.benchmark.crm_communication import review_observed_dialogue


class Client:
    def __init__(self,values):self.values=list(values);self.usage={'num_calls':0};self.seed=42;self.requests=[]
    async def chat(self,messages=None,**kwargs):
        if messages is not None:kwargs['messages']=messages
        self.requests.append(kwargs);self.usage['num_calls']+=1
        value=self.values.pop(0)
        return SimpleNamespace(content=json.dumps(value),finish_reason='stop',usage=None)


def decision(message='',stop=False):return {'stop':stop,'message':message,'exit_conditions_met':stop}
def verdict(valid=True):return {'valid':valid,'reason':'Faithful.'if valid else'The status is already answered; stop rather than introducing a new task.'}


def simulator(tmp_path,values):
    client=Client(values);events=[]
    owner=SimpleNamespace(emit=lambda k,v:events.append((k,v)),tool_calls=[],turn=0,
                          environment=SimpleNamespace(get_state=lambda:{'tickets':{}}))
    sim=VerifiedContextualUserSimulator(client,opening_path=tmp_path/'opening.json',identity={'seed':42},
        adapter=owner,arm='deep_core',verify_fidelity=True,fidelity_attempts=3,verification_mode='compact')
    sim.reset('Ask the current status of TCK-1002, then end as soon as you are told.')
    return sim,client,events


def test_goal_drift_is_rejected_and_corrected_before_becoming_a_customer_turn(tmp_path):
    sim,client,events=simulator(tmp_path,[decision('What is the status of TCK-1002?'),verdict(),
        decision('When will procurement deliver the asset tag?'),verdict(False),decision(stop=True),verdict()])
    async def run():
        assert await sim.step('')=='What is the status of TCK-1002?'
        assert await sim.step('TCK-1002 is In Progress.')==STOP_SIGNAL
    asyncio.run(run())
    assert not any(m.content=='When will procurement deliver the asset tag?'for m in sim._messages)
    generation=json.loads(client.requests[0]['messages'][-1].content)
    assert generation['next_speaker']=='customer'
    assert 'User:'in client.requests[3]['messages'][-1].content
    assert 'EVALUATOR FEEDBACK'in client.requests[4]['messages'][0].content
    assert client.usage['num_calls']==6 and not client.values
    assert any(k=='simulator_fidelity_review'and not v['valid']for k,v in events)


def test_role_echo_does_not_enter_accepted_user_history(tmp_path):
    answer='Ticket TCK-1002 is In Progress and waiting on an asset tag.'
    sim,client,_=simulator(tmp_path,[decision('What is its status?'),verdict(),
        decision(answer),verdict(),decision(stop=True),verdict()])
    async def run():
        await sim.step('')
        assert await sim.step(answer)==STOP_SIGNAL
    asyncio.run(run())
    assert sum(m.content==answer for m in sim._messages)==1 and client.usage['num_calls']==6


def test_unfaithful_proxy_failure_is_explicit_and_does_not_create_an_opening(tmp_path):
    sim,_,_=simulator(tmp_path,[v for _ in range(3)for v in [decision('Delete an unrelated ticket.'),verdict(False)]])
    with pytest.raises(SimulatorFidelityError):asyncio.run(sim.step(''))
    assert not(tmp_path/'opening.json').exists()


def test_additional_communication_review_keeps_criteria_and_native_trace_intact():
    calls=[]
    class Judge:
        async def grade(self,**kwargs):
            calls.append(kwargs);return SimpleNamespace(passed=True,failures=[],audit={'exact_quotes':True})
    criterion='The assistant must report that the ticket is closed.'
    task=SimpleNamespace(id='close',instruction='As the customer, ask the agent to close the ticket.',
                         expected_response_semantic=[criterion])
    turns=[{'role':'user','content':'Close TCK-1004.'},
           {'role':'assistant','content':'TCK-1004 is Closed.'}]
    trace=SimpleNamespace(error=None,timed_out=False,turns=turns,response_passed=False)
    review=asyncio.run(review_observed_dialogue(Judge(),task,trace))
    assert review['passed']and trace.response_passed is False
    assert calls[0]['criteria']==[criterion]and calls[0]['turns']==turns
    context=json.loads(calls[0]['task_instruction'])
    assert context['customer_requests']==['Close TCK-1004.']
    assert task.instruction not in calls[0]['task_instruction']


def grade(met,evidence):
    return {'criteria':[{'criterion_id':'criterion_1','met':met,'evidence':evidence,
        'explanation':'Visible completion evidence.'if met else'Completion has not been confirmed.'}],
        'contradiction_found':False,'contradiction_evidence':''}


def test_audited_stop_reuses_native_quote_validation_on_actual_service_messages(tmp_path):
    from harness.models import ChatMessage,Role
    answer='Ticket TCK-1002 is In Progress.'
    sim,client,events=simulator(tmp_path,[grade(True,[answer])]);sim.verification_mode='audited'
    sim._messages.extend([ChatMessage(role=Role.ASSISTANT,content='What is the status?'),
        ChatMessage(role=Role.USER,content=answer)])
    review=asyncio.run(sim._review_decision(STOP_SIGNAL))
    assert review.valid and client.usage['num_calls']==1
    assert any(k=='simulator_evidence_audit'and v['passed']for k,v in events)
    assert answer in client.requests[0]['messages'][-1].content


def test_audited_review_rejects_invented_completion_quotes_before_acceptance(tmp_path):
    from harness.models import ChatMessage,Role
    sim,client,_=simulator(tmp_path,[grade(True,['The ticket is closed.']),grade(False,[]),
        grade(False,[])])
    sim.verification_mode='audited'
    sim._messages.extend([ChatMessage(role=Role.ASSISTANT,content='Close the ticket.'),
        ChatMessage(role=Role.USER,content='I will close the ticket.')])
    assert not asyncio.run(sim._review_decision(STOP_SIGNAL)).valid
    assert client.usage['num_calls']==3


def test_audited_terminal_review_stops_before_generating_an_extra_customer_demand(tmp_path):
    opening='What is the status of TCK-1002?';answer='Ticket TCK-1002 is In Progress.'
    sim,client,events=simulator(tmp_path,[decision(opening),grade(True,[opening]),grade(True,[answer])])
    sim.verification_mode='audited'
    async def run():
        assert await sim.step('')==opening
        assert await sim.step(answer)==STOP_SIGNAL
    asyncio.run(run())
    assert client.usage['num_calls']==3 and not client.values
    assert any(k=='simulator_terminal_review'and v['valid']for k,v in events)


def test_audited_terminal_review_keeps_the_customer_active_after_a_promise(tmp_path):
    opening='Please close TCK-1004.';followup='Please complete the closure and confirm it.'
    sim,client,events=simulator(tmp_path,[decision(opening),grade(True,[opening]),grade(False,[]),
        grade(False,[]),
        decision(followup),grade(True,[followup])]);sim.verification_mode='audited'
    sim.reset('Ask to close TCK-1004. Stop only when closure is confirmed.')
    async def run():
        await sim.step('')
        assert await sim.step('I will close TCK-1004.')==followup
    asyncio.run(run())
    assert client.usage['num_calls']==6 and not client.values
    assert any(k=='simulator_terminal_review'and not v['valid']for k,v in events)


def test_unsupported_negative_stop_is_adjudicated_without_overwriting_its_audit(tmp_path):
    from harness.models import ChatMessage,Role
    sim,client,events=simulator(tmp_path,[grade(False,[]),grade(True,['TCK-1002 is In Progress.'])])
    sim.verification_mode='audited'
    sim._messages.extend([ChatMessage(role=Role.ASSISTANT,content='What is the status?'),
        ChatMessage(role=Role.USER,content='TCK-1002 is In Progress.')])
    assert asyncio.run(sim._review_decision(STOP_SIGNAL)).valid
    assert client.usage['num_calls']==2
    assert any(k=='simulator_evidence_audit'and not v['passed']for k,v in events)
    assert any(k=='simulator_rejection_adjudication'and not v['rejection_supported']for k,v in events)
    assert 'Completion has not been confirmed.'not in client.requests[-1]['messages'][-1].content


def test_rejection_review_uses_original_exit_condition_as_context_and_actual_promise_as_evidence(tmp_path):
    from harness.models import ChatMessage,Role
    instruction='Ask to close TCK-1004. Stop only when closure is confirmed.'
    promise='I will close TCK-1004.'
    sim,client,_=simulator(tmp_path,[grade(False,[]),grade(False,[promise])])
    sim.verification_mode='audited';sim.reset(instruction)
    sim._messages.extend([ChatMessage(role=Role.ASSISTANT,content='Please close TCK-1004.'),
        ChatMessage(role=Role.USER,content=promise)])
    assert not asyncio.run(sim._review_decision(STOP_SIGNAL)).valid
    assert client.usage['num_calls']==2
    raw=client.requests[-1]['messages'][-1].content
    payload=json.loads(raw.split('<UNTRUSTED_BENCHMARK_DATA>\n',1)[1].rsplit('\n</UNTRUSTED_BENCHMARK_DATA>',1)[0])
    evidence=payload['assistant_messages'][0]
    assert instruction not in evidence
    assert json.loads(payload['task_instruction'])['customer_instruction']==instruction
    assert 'SERVICE AGENT:\n'+promise in evidence
    assert 'CUSTOMER:\nPlease close TCK-1004.'in evidence


def test_rejection_review_still_rejects_a_fabricated_evidence_quote(tmp_path):
    from harness.models import ChatMessage,Role
    sim,client,events=simulator(tmp_path,[grade(False,[]),
        grade(True,['The ticket was deleted.']),grade(True,['The ticket was deleted.'])])
    sim.verification_mode='audited'
    sim._messages.extend([ChatMessage(role=Role.ASSISTANT,content='What is its status?'),
        ChatMessage(role=Role.USER,content='TCK-1002 is In Progress.')])
    with pytest.raises(SimulatorFidelityError):asyncio.run(sim._review_decision(STOP_SIGNAL))
    assert client.usage['num_calls']==3
    assert any(k=='simulator_review_error'and v['stage']=='customer-rejection'for k,v in events)


def test_primary_judge_failure_becomes_a_recordable_simulator_error(tmp_path):
    from harness.models import ChatMessage,Role
    sim,client,events=simulator(tmp_path,[grade(True,['Invented completed action.'])]*2)
    sim.verification_mode='audited'
    sim._messages.extend([ChatMessage(role=Role.ASSISTANT,content='What is its status?'),
        ChatMessage(role=Role.USER,content='TCK-1002 is In Progress.')])
    with pytest.raises(SimulatorFidelityError):asyncio.run(sim._review_decision(STOP_SIGNAL))
    assert client.usage['num_calls']==2
    assert any(k=='simulator_review_error'and v['stage']=='customer-fidelity'for k,v in events)
