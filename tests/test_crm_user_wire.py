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
