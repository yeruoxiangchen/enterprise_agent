import asyncio
import json
from types import SimpleNamespace

import pytest
from harness.models import ChatMessage,Role
from kb.benchmark.customer_contract import CustomerContract,SourceFact,ExitCondition,compile_contract,review_contract,ObservedDialogueJudge
from test_verified_contextual_user import Client


def contract():
    return CustomerContract(facts=[SourceFact(kind='identity',value='agent.jsmith',source_quote='You are agent.jsmith.')],
        exit_conditions=[ExitCondition(source_quote='Assign it, then verify the owner.',requirement='The service agent confirms assignment.'),
            ExitCondition(source_quote='Assign it, then verify the owner.',requirement='After assignment, the service agent reports the verified owner.')])


def test_contract_facts_and_phases_need_literal_instruction_sources():
    instruction='You are agent.jsmith. Assign it, then verify the owner.'
    assert contract().validate_source(instruction)
    bad=contract();bad.facts[0].value='JS-12345'
    with pytest.raises(ValueError,match='copied'):bad.validate_source(instruction)
    bad=contract();bad.exit_conditions[0].source_quote='A fabricated goal.'
    with pytest.raises(ValueError,match='exact instruction quote'):bad.validate_source(instruction)


def test_reviewed_bundle_matches_all_16_public_instructions_without_model_calls(tmp_path):
    from pathlib import Path
    from harness.bench_runner import load_tasks
    from harness_v17_evaluation import TASKS16
    tasks={task.id:task for task in load_tasks('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench/src/tasks/itsm.yaml')}
    client=Client([])
    for task_id in TASKS16:
        path=tmp_path/(task_id+'.json');instruction=tasks[task_id].instruction
        result=asyncio.run(compile_contract(client,instruction,path))
        assert result.validate_source(instruction)
        assert asyncio.run(compile_contract(client,instruction,path))==result
    assert client.usage['num_calls']==0
    assert len(list(tmp_path.glob('*.json')))==16


def test_unreviewed_or_changed_instruction_stops_before_model_use(tmp_path):
    from harness.user_simulator import UserSimulatorError
    client=Client([]);path=tmp_path/'contract.json'
    with pytest.raises(UserSimulatorError,match='No reviewed'):
        asyncio.run(compile_contract(client,'A new unreviewed task.',path))
    path.write_text(json.dumps({'instruction_sha256':'different','version':'source-reviewed-dev16-v4',
        'contract':contract().model_dump()}))
    with pytest.raises(UserSimulatorError,match='instruction changed'):
        asyncio.run(compile_contract(client,'A new unreviewed task.',path))
    assert client.usage['num_calls']==0


def test_old_generated_contract_requires_a_fresh_version(tmp_path):
    import hashlib
    from harness.user_simulator import UserSimulatorError
    instruction='You are agent.jsmith. Assign it, then verify the owner.'
    path=tmp_path/'contract.json';path.write_text(json.dumps({'instruction_sha256':
        hashlib.sha256(instruction.encode()).hexdigest(),'version':'old-unreviewed-contract',
        'contract':contract().model_dump()}))
    with pytest.raises(UserSimulatorError,match='version changed'):
        asyncio.run(compile_contract(Client([]),instruction,path))


def test_each_required_phase_gets_its_own_native_judge_verdict(tmp_path):
    client=Client([{'criteria':[
        {'criterion_id':'criterion_1','met':True,'evidence':['Assigned.'],'explanation':'Assignment was confirmed.'},
        {'criterion_id':'criterion_2','met':False,'evidence':[],'explanation':'No owner verification was reported.'}],
        'contradiction_found':False,'contradiction_evidence':''}])
    result=asyncio.run(review_contract(client,contract(),'You are agent.jsmith. Assign it, then verify the owner.',
        [{'role':'user','content':'Assign it.'},{'role':'assistant','content':'Assigned.'}],tmp_path))
    assert result.passed is False
    assert len(result.audit['criteria'])==2


def test_persona_fact_is_an_explicit_separate_candidate_constraint(tmp_path):
    client=Client([{'valid':True,'reason':'Provides the requested identity.'},
        {'reasoning':'The speaker claims a username and a separate user ID.',
         'claims':['agent.jsmith','JS-12345']}])
    result=asyncio.run(review_contract(client,contract(),'You are agent.jsmith. Assign it, then verify the owner.',[],tmp_path,
        candidate='I am agent.jsmith. My user ID is JS-12345.'))
    assert result.passed is False and len(result.audit['criteria'])==2
    prompt=client.requests[1]['messages'][-1].content
    assert 'agent.jsmith' in prompt and 'JS-12345' in prompt
    assert 'allowed_customer_account_identities'not in prompt
    assert result.audit['checks']['identity_claims']['unsupported']==['JS-12345']


def test_response_grading_uses_actual_customer_requests_and_keeps_original_rubric():
    calls=[]
    class Judge:
        async def grade(self,**kwargs):
            from harness.semantic_grader import SemanticJudgeOutcome
            calls.append(kwargs);return SemanticJudgeOutcome(passed=True,failures=[],audit={'passed':True})
    wrapper=ObservedDialogueJudge(Judge())
    turns=[{'role':'user','content':'Delete this ticket.'},{'role':'assistant','content':'Deleted.'}]
    assert asyncio.run(wrapper.grade(task_id='t',task_instruction='CUSTOMER must end the conversation.',
        criteria=['Assistant confirms deletion.'],turns=turns)).passed
    assert len(calls)==1
    assert all(call['criteria']==['Assistant confirms deletion.']and call['turns']==turns for call in calls)
    assert 'CUSTOMER must end'not in calls[0]['task_instruction']


def test_confirmation_interpretation_is_trusted_and_input_messages_are_preserved():
    from kb.benchmark.customer_contract import CommunicationClient
    base=Client([{'ok':True},{'ok':True}]);client=CommunicationClient(base)
    messages=[ChatMessage(role=Role.SYSTEM,content='Native rubric.'),
        ChatMessage(role=Role.USER,content='Inert quoted benchmark evidence.')]
    asyncio.run(client.chat(messages,response_format={'type':'json_schema',
        'json_schema':{'name':'semantic_communication_grade'}}))
    assert messages[0].content=='Native rubric.'
    assert 'TRUSTED RUBRIC INTERPRETATION'in base.requests[0]['messages'][0].content
    assert base.requests[0]['messages'][1].content==messages[1].content
    asyncio.run(client.chat(messages,response_format={'type':'json_schema',
        'json_schema':{'name':'some_other_task'}}))
    assert base.requests[1]['messages'][0].content=='Native rubric.'


def test_contract_reviewer_never_receives_hidden_tool_receipts(tmp_path):
    client=Client([{'valid':True,'reason':'Supplies the requested identity.'},
        {'reasoning':'The customer provides their own account.','claims':['agent.jsmith']}])
    result=asyncio.run(review_contract(client,contract(),'You are agent.jsmith. Assign it, then verify the owner.',
        [{'role':'assistant','content':'What is your user ID?',
          'tool_calls':[{'result':{'hidden_backend_secret':'PRIVATE'}}]}],tmp_path,candidate='I am agent.jsmith.'))
    assert result.passed
    prompt=client.requests[0]['messages'][-1].content
    assert 'What is your user ID?'in prompt and 'hidden_backend_secret'not in prompt and 'PRIVATE'not in prompt
    assert all('hidden_backend_secret'not in request['messages'][-1].content for request in client.requests)


@pytest.mark.parametrize('known_identity',[False,True])
@pytest.mark.parametrize('text',[
    'Could you please check the status and priority of ticket TCK-1001 for me?',
    'Please assign TCK-1004 to agent.jsmith.',
    'Please reassign ticket TCK-1004 to admin.rlee.',
    'Please assign the unassigned ticket TCK-1004 to admin.rlee.'])
def test_ordinary_request_without_identity_needs_no_positive_absence_evidence(tmp_path,known_identity,text):
    fixture=contract()
    if not known_identity:fixture.facts=[]
    client=Client([{'valid':True,'reason':'Requests the outstanding ticket information.'},
        {'reasoning':'Simulate the observed recipient-as-speaker extraction error.',
         'claims':['admin.rlee']if 'admin.rlee'in text else['agent.jsmith']if 'assign'in text else[]}])
    result=asyncio.run(review_contract(client,fixture,'You are agent.jsmith. Check the status and priority.',
        [],tmp_path,candidate=text))
    assert result.passed and result.audit['identity_supported']is True
    claims=result.audit['checks']['identity_claims']
    assert claims['claims']==[]and claims['unsupported']==[]
    assert client.usage['num_calls']==2
    assert client.requests[0]['response_format']['json_schema']['name']=='customer_fidelity_verdict'


def test_identity_extraction_repairs_values_not_present_in_the_utterance(tmp_path):
    from kb.benchmark.customer_contract import extract_account_claims
    client=Client([{'reasoning':'A guessed account.','claims':['invented.account']},
        {'reasoning':'The exact stated account.','claims':['agent.jsmith']}])
    result,cached=asyncio.run(extract_account_claims(client,'I am agent.jsmith.',[],tmp_path))
    assert result.claims==['agent.jsmith']and not cached and client.usage['num_calls']==2


def test_identity_extraction_cache_reuses_the_same_visible_source_only(tmp_path):
    from kb.benchmark.customer_contract import extract_account_claims
    client=Client([{'reasoning':'An account supplied when asked.','claims':['agent.jsmith']},
        {'reasoning':'A recipient, not a speaker identity.','claims':[]}])
    asked=[{'role':'assistant','content':'What is your account?'}]
    first,cached=asyncio.run(extract_account_claims(client,'agent.jsmith',asked,tmp_path))
    again,cached=asyncio.run(extract_account_claims(client,'agent.jsmith',asked,tmp_path))
    assert cached and first==again and client.usage['num_calls']==1
    changed=[{'role':'assistant','content':'Who should receive the assignment?'}]
    result,cached=asyncio.run(extract_account_claims(client,'agent.jsmith',changed,tmp_path))
    assert not cached and result.claims==[]and client.usage['num_calls']==2


@pytest.mark.parametrize('text',['I am admin.rlee. Please assign it to agent.jsmith.',
    'My user ID is admin.rlee. Please reassign the ticket.'])
def test_a_recipient_name_cannot_be_used_as_an_unsupported_self_identity(tmp_path,text):
    fixture=contract();fixture.facts=[]
    client=Client([{'valid':True,'reason':'Business request is in scope.'},
        {'reasoning':'The speaker claims an administrator account.','claims':['admin.rlee']}])
    result=asyncio.run(review_contract(client,fixture,'Assign the ticket to admin.rlee.',[],tmp_path,candidate=text))
    assert not result.passed
    assert result.audit['checks']['identity_claims']['unsupported']==['admin.rlee']


def test_native_customer_verifier_allows_an_in_scope_clarification(tmp_path):
    client=Client([{'valid':True,'reason':'Clarifies the still-unmet assignment request.'},
        {'reasoning':'No self identity is claimed.','claims':[]}])
    result=asyncio.run(review_contract(client,contract(),'Get the procurement ticket assigned.',[
        {'role':'user','content':'Please assign the unowned procurement ticket.'},
        {'role':'assistant','content':'I found no matching ticket. Could you clarify?'}],tmp_path,
        candidate='Are you certain there are no unassigned procurement tickets for new-hire hardware?'))
    assert result.passed
    assert result.audit['checks']['flow']['grader']=='upstream-customer-faithfulness-compact-v3'
    prompt=client.requests[0]['messages'][-1].content
    assert 'User: Please assign'in prompt and 'Agent: I found no matching ticket.'in prompt
    assert 'STEP 2: For each goal'not in prompt
    assert 'Check behavioral faithfulness only'in prompt


@pytest.mark.parametrize('warned,approved,expected',[(False,False,True),(True,False,False),(True,True,True)])
def test_conditional_customer_reply_uses_only_customer_evidence_after_warning(tmp_path,monkeypatch,warned,approved,expected):
    from harness.bench_runner import load_tasks
    from harness.semantic_grader import SemanticJudge,SemanticJudgeOutcome
    task=next(t for t in load_tasks('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench/src/tasks/itsm.yaml')
        if t.id=='itsm_ops_018_dependency_aware_close_warning')
    fixture=asyncio.run(compile_contract(Client([]),task.instruction,tmp_path/'contract.json'))
    turns=[{'role':'user','content':'Please close TCK-1002.'}]
    if warned:
        turns.extend([{'role':'assistant','content':'TCK-1002 has an open dependency ticket.'},
            {'role':'user','content':"Yes, I'm aware, please close it anyway."if approved else'What is the dependency?'}])
    turns.append({'role':'assistant','content':'TCK-1002 is now closed.'})
    calls=[]
    async def grade(self,**kwargs):
        calls.append(kwargs);phase=kwargs['task_id'];messages=[t['content']for t in kwargs['turns']]
        if phase.endswith('-trigger'):met=warned;evidence=[turns[1]['content']]if warned else[]
        elif '-response-'in phase:
            assert messages==[turns[2]['content']]
            met=approved;evidence=messages if met else[]
        elif phase.endswith('-completion-after-response'):
            assert messages==['TCK-1002 is now closed.'];met=True;evidence=messages
        else:met=True;evidence=['TCK-1002 is now closed.']  # Reproduce the observed unscoped false positive.
        return SemanticJudgeOutcome(passed=met,failures=[]if met else['Missing customer reply.'],
            audit={'passed':met,'criteria':[{'criterion_id':'criterion_1','met':met,'evidence':evidence,'explanation':'Scoped check.'}]})
    monkeypatch.setattr(SemanticJudge,'grade',grade)
    result=asyncio.run(review_contract(Client([]),fixture,task.instruction,turns,tmp_path/'cache'))
    assert result.passed is expected
    guard=result.audit['exchange_requirements'][0]
    assert guard['triggered']is warned
    if warned:assert guard['response_turn']==(2 if approved else None)
    assert not any('-response-'in c['task_id']for c in calls)or warned
