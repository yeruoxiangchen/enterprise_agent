"""CPU-only acceptance against the xgrammar installed in the inference venv."""
import ast
import copy
import hashlib
import json
from pathlib import Path

import xgrammar as xgr
from xgrammar.testing import _is_grammar_accept_string


def main():
    source = Path(__file__).with_name('crm_bench_services.py')
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'constrained_response_format')
    namespace = {'copy': copy}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    original = {'type': 'json_schema', 'json_schema': {'name': 'benchmark_user_turn', 'strict': True,
        'schema': {'type': 'object', 'additionalProperties': False, 'properties': {
            'stop': {'type': 'boolean'}, 'message': {'type': 'string'},
            'exit_conditions_met': {'type': 'boolean'}},
            'required': ['stop', 'message', 'exit_conditions_met']}}}
    schema = namespace['constrained_response_format'](original)['json_schema']['schema']
    grammar = xgr.Grammar.from_json_schema(schema)
    cases = [(False, "thanks, that's all", False, True), (False, '谢谢，够了。', False, True),
             (True, '', True, True), (False, 'thanks', True, False),
             (True, 'thanks', True, False), (False, '', False, False)]
    results = []
    for stop, message, met, expected in cases:
        value = {'stop': stop, 'message': message, 'exit_conditions_met': met}
        accepted = _is_grammar_accept_string(grammar, json.dumps(value, ensure_ascii=False))
        results.append({'response': value, 'accepted': accepted, 'expected': expected})
        if accepted != expected:
            raise AssertionError(results[-1])
    report = {'passed': True, 'cases': results, 'model_loaded': False,
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    output = Path('/data/zjr/enterprise_kb_agent/artifacts/harness_v6_cpu_20261110/user-wire-xgrammar.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
