import copy
import json

import pytest

from crm_bench_services import constrained_response_format
from harness.user_simulator import ReactUserSimulator, UserSimulatorError


def original_format():
    return {'type': 'json_schema', 'json_schema': {'name': 'benchmark_user_turn', 'strict': True,
        'schema': {'type': 'object', 'additionalProperties': False, 'properties': {
            'stop': {'type': 'boolean'}, 'message': {'type': 'string'},
            'exit_conditions_met': {'type': 'boolean'}},
            'required': ['stop', 'message', 'exit_conditions_met']}}}


def test_simulator_wire_encodes_only_consistent_decisions_and_keeps_input_unchanged():
    source = original_format()
    snapshot = copy.deepcopy(source)
    actual = constrained_response_format(source)
    assert source == snapshot
    branches = actual['json_schema']['schema']['anyOf']
    assert len(branches) == 2
    for branch, stopped in zip(branches, [False, True]):
        fields = branch['properties']
        assert fields['stop']['const'] is fields['exit_conditions_met']['const'] is stopped
        assert branch['additionalProperties'] is False
        if stopped:
            assert fields['message']['const'] == ''
        else:
            assert fields['message']['minLength'] == 1
            assert 'pattern' not in fields['message']  # whitespace is still checked by upstream .strip()
    assert ReactUserSimulator._parse_response(json.dumps(
        {'stop': False, 'message': "thanks, that's all", 'exit_conditions_met': False})) == "thanks, that's all"
    ReactUserSimulator._parse_response(json.dumps({'stop': True, 'message': '', 'exit_conditions_met': True}))


@pytest.mark.parametrize('decision', [
    {'stop': False, 'message': "thanks, that's all", 'exit_conditions_met': True},
    {'stop': True, 'message': 'thanks', 'exit_conditions_met': True},
    {'stop': False, 'message': ' ', 'exit_conditions_met': False},
])
def test_upstream_invalid_decisions_are_still_rejected(decision):
    with pytest.raises(UserSimulatorError):
        ReactUserSimulator._parse_response(json.dumps(decision))


def test_judge_schema_is_passed_through_unchanged():
    schema = original_format()
    schema['json_schema']['name'] = 'semantic_communication_grade'
    assert constrained_response_format(schema) is schema


def test_evidence_first_schema_preserves_native_constraints_and_original_input():
    from crm_bench_services import evidence_first_grading_format
    from harness.semantic_grader import _response_format,SemanticJudgeWireResult
    source=_response_format();snapshot=copy.deepcopy(source)
    actual=evidence_first_grading_format(source)
    assert source==snapshot
    original=source['json_schema']['schema']['$defs']['SemanticCriterionVerdict']
    reordered=actual['json_schema']['schema']['$defs']['SemanticCriterionVerdict']
    assert list(reordered['properties'])==['criterion_id','explanation','evidence','met']
    assert reordered['properties']==original['properties']
    assert set(reordered['required'])==set(original['required'])
    assert reordered['additionalProperties']is False
    record={'criteria':[{'criterion_id':'criterion_1','explanation':'Closure is still pending.',
        'evidence':['I will close it.'],'met':False}],
        'contradiction_found':False,'contradiction_evidence':''}
    assert SemanticJudgeWireResult.model_validate(record).criteria[0].met is False
    record['criteria'][0]['met']='false'
    with pytest.raises(ValueError):SemanticJudgeWireResult.model_validate(record)


@pytest.mark.parametrize('enabled',[False,True])
def test_client_only_reorders_grading_when_explicitly_enabled(tmp_path,monkeypatch,enabled):
    import asyncio
    import httpx
    from crm_bench_services import LocalEvalClient
    from harness.models import ChatMessage,Role
    from harness.semantic_grader import _response_format
    client=LocalEvalClient('http://local.invalid','test',tmp_path/'model.jsonl',42,
        reason_first_grading=enabled)
    source=_response_format();original=copy.deepcopy(source);sent=[]
    async def post(url,*,json):
        sent.append(json)
        return httpx.Response(200,request=httpx.Request('POST',url),json={
            'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':100,'completion_tokens':2,'total_tokens':102}})
    monkeypatch.setattr(client.client,'post',post)
    async def run():
        try:await client.chat([ChatMessage(role=Role.USER,content='Original task and exact dialogue.')],response_format=source)
        finally:await client.aclose()
    asyncio.run(run())
    assert source==original
    fields=sent[0]['response_format']['json_schema']['schema']['$defs']['SemanticCriterionVerdict']['properties']
    assert list(fields)==(['criterion_id','explanation','evidence','met']if enabled else list(original['json_schema']['schema']['$defs']['SemanticCriterionVerdict']['properties']))
    assert sent[0]['messages'][-1]['content']=='Original task and exact dialogue.'
