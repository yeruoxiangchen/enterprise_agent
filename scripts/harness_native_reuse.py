"""Reuse unchanged Actor/fixture results after checking their real business trace.

No model inference is replayed here. Canonical tool executions run only in a
fresh in-memory benchmark environment and must reproduce stored observations.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import time

from crm_bench_control import write

ACTOR_KEYS = (
    'model','context_tokens','output_tokens','model_calls_per_turn','business_calls_per_turn',
    'agent_turn_timeout_seconds','worker_hard_timeout_seconds','seed','workflow_memory',
    'user_simulator','semantic_judge','confirmation_guard','dependencies','forked_subagents',
    'operation_permission_route','specialist_owned_checks','operation_preparations',
    'tensor_parallel_size','gpu_memory_fraction',
)


def function_ast(path, names):
    tree=ast.parse(path.read_text())
    return {node.name:ast.dump(node,include_attributes=False)for node in tree.body
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))and node.name in names}


async def replay_business(row, upstream):
    from harness.bench_runner import load_tasks
    from harness.tools.environment import create_environment
    import harness.tools.domains
    if row['case_kind']=='cancellation':
        initial=None
    else:
        task=next(task for task in load_tasks(str(upstream/'src/tasks/itsm.yaml'))if task.id==row['task_id'])
        initial=task.initial_state
    environment=create_environment('itsm',initial)
    registry=environment.create_registry()
    count=0
    for call in row['audit']:
        if not call['backend_executed']:
            continue
        actual=await registry.execute_with_status(call['name'],call['arguments'])
        if actual.ok!=call['ok']or json.loads(actual.result)!=json.loads(call['result']):
            raise ValueError(f'Business observation changed: {row["task_id"]}/{call["name"]}')
        count+=1
    if environment.get_state()!=row['environment']:
        raise ValueError('Initial environment/tool contract changed: '+row['task_id'])
    return count


async def restore(source, target, protocol, manifest, upstream):
    previous=json.loads((source/'protocol.json').read_text())
    for key in ACTOR_KEYS:
        if previous.get(key)!=protocol.get(key):
            raise ValueError('Reuse Actor protocol differs: '+key)
    old_manifest=json.loads((source/'source-manifest.json').read_text())
    actor_files={name:value for name,value in old_manifest.items()if name.startswith('kb/')}
    if actor_files!={name:value for name,value in manifest.items()if name.startswith('kb/')}:
        raise ValueError('Reuse requires unchanged Actor source')
    for name in ('scripts/crm_benchmark.py','scripts/crm_bench_services.py'):
        if old_manifest.get(name)!=manifest.get(name):
            raise ValueError('Reuse execution/grading source differs: '+name)
    for name,functions in [('scripts/harness_native_worker.py',{'run','task_for_case','deterministic_checks'}),
                           ('scripts/harness_native_cases.py',{'gate_ready','mechanism_checks'})]:
        if function_ast(source/'source'/name,functions)!=function_ast(target/'source'/name,functions):
            raise ValueError('Reuse worker/check functions differ: '+name)
    started=json.loads((source/'started.json').read_text())['started_at']
    shared_inputs=[upstream/'src/agents/itsm.yaml',upstream/'src/tasks/policies/itsm_policy.md',
                   upstream/'src/tasks/itsm.yaml',upstream/'src/harness/tools/domains/itsm.py']
    fingerprints={}
    for path in shared_inputs:
        if path.stat().st_mtime>started:
            raise ValueError('Shared benchmark input changed after source run: '+str(path))
        fingerprints[str(path)]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'mtime':path.stat().st_mtime}
    cases={case['task_id']:case for case in protocol['cases']}
    old_cases={case['task_id']:case for case in previous['cases']}
    rows=[];ledger=[]
    for original in json.loads((source/'rows.json').read_text()):
        key=original['task_id']
        if key not in cases or original['arm']not in protocol['arms']:
            continue
        if old_cases[key]!=cases[key]:
            raise ValueError('Reuse customer fixture changed: '+key)
        raw_path=source/'trials'/key/original['arm']/'result.json'
        raw=json.loads(raw_path.read_text())
        for field in ('task_id','arm','passed','checks','error','tokens','trace','audit','effects','environment'):
            if raw.get(field)!=original.get(field):
                raise ValueError('Source result/row mismatch: '+key+'/'+field)
        replayed=await replay_business(original,upstream)
        folder=target/'trials'/key/original['arm'];folder.mkdir(parents=True,exist_ok=False)
        provenance={'source_run':str(source),'source_result':str(raw_path),
            'source_result_sha256':hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            'backend_calls_replayed':replayed,'new_inference_tokens':0}
        row={**copy.deepcopy(original),'output':str(folder),'reused':True,'reuse':provenance}
        write(folder/'result.json',row);rows.append(row);ledger.append({'task_id':key,**provenance})
    return rows,{'validated_at':time.time(),'source_run':str(source),'inherited_trials':len(rows),
        'new_inference_tokens':0,'actor_files_checked':len(actor_files),'protocol_fields_checked':list(ACTOR_KEYS),
        'shared_input_fingerprints':fingerprints,'entries':ledger,
        'evaluation_scope':'same fixed-user fixture; source/check compatibility and isolated canonical business replay'}
