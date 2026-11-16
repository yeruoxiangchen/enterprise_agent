"""Dedicated local model leases; never change shared model.env/service files."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

import httpx

from harness.llm.base import LLMClient
from harness.llm.openai_client import _messages_to_dicts
from harness.models import ChatResponse, ToolCall

from crm_bench_control import RUNTIME, write, identity

WIRE_FORMAT_VERSION = 'visible-compact-exact-evidence-v4-budgeted'
GPU_MEMORY_FRACTION = .78
GPU_HEADROOM_MIB = 1024
PREFILL_TOKENS = 512


class InvalidActorToolCall(ValueError):
    """A provider returned malformed function-call arguments."""


def parse_tool_calls(message):
    calls = []
    try:
        for raw in message.get('tool_calls') or []:
            arguments = json.loads(raw['function']['arguments'])
            if not isinstance(arguments, dict):
                raise ValueError('Tool arguments must be an object')
            calls.append(ToolCall(id=raw['id'], name=raw['function']['name'], arguments=arguments))
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidActorToolCall('Malformed actor function call') from exc
    return calls


def constrained_response_format(response_format):
    """Encode the existing simulator contract in the decoding schema.

    The model still decides whether task goals are met. No output is repaired
    after generation, and the upstream simulator validator remains unchanged.
    """
    if (not response_format or response_format.get('type') != 'json_schema'
            or response_format.get('json_schema', {}).get('name') != 'benchmark_user_turn'):
        return response_format
    result = copy.deepcopy(response_format)
    original = result['json_schema']['schema']
    if set(original.get('properties', {})) != {'stop', 'message', 'exit_conditions_met'}:
        raise ValueError('Unexpected simulator schema; inspect the upstream contract')
    branches = []
    for stopped in (False, True):
        properties = copy.deepcopy(original['properties'])
        properties['stop']['const'] = stopped
        properties['exit_conditions_met']['const'] = stopped
        if stopped:
            properties['message']['const'] = ''
        else:
            properties['message']['minLength'] = 1
        branches.append({'type': 'object', 'additionalProperties': False,
                         'properties': properties, 'required': ['stop', 'message', 'exit_conditions_met']})
    result['json_schema']['schema'] = {'anyOf': branches}
    return result


def evidence_first_grading_format(response_format):
    """Preserve the native verdict schema while emitting reasons before votes."""
    if (not response_format or response_format.get('type')!='json_schema'
            or response_format.get('json_schema',{}).get('name')!='semantic_communication_grade'):
        return response_format
    result=copy.deepcopy(response_format)
    def reorder(node):
        if isinstance(node,dict):
            fields=node.get('properties',{})
            if set(fields)=={'criterion_id','met','evidence','explanation'}:
                order=['criterion_id','explanation','evidence','met']
                node['properties']={key:fields[key]for key in order}
                node['required']=[key for key in order if key in node.get('required',[])]
            for value in node.values():reorder(value)
        elif isinstance(node,list):
            for value in node:reorder(value)
    reorder(result['json_schema']['schema'])
    return result


def normalize_usage(raw):
    """Translate the provider's nested usage into the benchmark's integer map."""
    if not isinstance(raw, dict):
        raise ValueError('Provider response has no measurable token usage')

    def count(value, name):
        if type(value) is not int or value < 0:
            raise ValueError(f'Invalid provider token count: {name}')
        return value

    prompt = count(raw.get('prompt_tokens'), 'prompt_tokens')
    completion = count(raw.get('completion_tokens'), 'completion_tokens')
    prompt_details = raw.get('prompt_tokens_details') or {}
    completion_details = raw.get('completion_tokens_details') or {}
    if not isinstance(prompt_details, dict) or not isinstance(completion_details, dict):
        raise ValueError('Invalid provider token usage details')
    return {
        'prompt_tokens': prompt, 'completion_tokens': completion,
        'total_tokens': count(raw.get('total_tokens') if raw.get('total_tokens') is not None
                              else prompt+completion, 'total_tokens'),
        'cache_read_tokens': count(prompt_details.get('cached_tokens') or 0, 'cached_tokens'),
        'cache_write_tokens': count(raw.get('cache_creation_input_tokens') or 0, 'cache_creation_input_tokens'),
        'reasoning_tokens': count(completion_details.get('reasoning_tokens') or 0, 'reasoning_tokens'),
    }


def gpu_snapshot():
    text = subprocess.check_output([
        'nvidia-smi', '--query-gpu=index,memory.used,memory.free,memory.total,utilization.gpu',
        '--format=csv,noheader,nounits'], text=True)
    return {str(i): {'used_mib': used, 'free_mib': free, 'total_mib': total, 'utilization': util}
            for i, used, free, total, util in [list(map(int, line.split(','))) for line in text.splitlines()]}


def gpu_has_room(snapshot, memory_fraction=GPU_MEMORY_FRACTION):
    """Admit a shared card only with the full service budget plus spare memory."""
    return snapshot['free_mib'] >= snapshot['total_mib']*memory_fraction+GPU_HEADROOM_MIB


class ModelServices:
    def __init__(self, output, progress):
        self.output, self.progress, self.owned = Path(output), progress, {}

    def save(self):
        write(self.output/'services.json', self.owned)

    def start(self, role, gpu, *, max_model_len=None, memory_fraction=GPU_MEMORY_FRACTION,
              tensor_parallel_size=1):
        default_context = 16384 if role == 'actor' else 12288
        context = default_context if max_model_len is None else max_model_len
        if type(context) is not int or not 0 < context <= default_context:
            raise ValueError(f'Invalid {role} context length: {context}')
        if not .5 <= memory_fraction <= .8:
            raise ValueError('Service memory fraction must be between .5 and .8')
        group = gpu.split(',')
        if (tensor_parallel_size not in (1,2) or len(group)!=tensor_parallel_size
                or len(set(group))!=len(group)):
            raise ValueError('Use one distinct physical GPU per tensor-parallel rank (1 or 2)')
        snapshots = gpu_snapshot()
        for index in group:
            if index not in snapshots or not gpu_has_room(snapshots[index], memory_fraction):
                raise RuntimeError(f'GPU {index} has insufficient headroom before {role} startup: {snapshots.get(index)}')
        snapshot = snapshots[gpu] if tensor_parallel_size==1 else {index:snapshots[index]for index in group}
        model = RUNTIME/'models/Qwen3-8B' if role == 'actor' else Path('/data/zjr/models/Qwen3-VL-8B-Instruct')
        if not (model/'config.json').is_file():
            raise FileNotFoundError(model)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        alias = 'crm-qwen3-8b' if role == 'actor' else 'crm-qwen3-vl-8b'
        command = ['/data/zjr/deepsearch_rl/venv/bin/python', '-B', '-u', '-m',
                   'vllm.entrypoints.openai.api_server', '--model', str(model),
                   '--served-model-name', alias, '--host', '127.0.0.1', '--port', str(port),
                   '--dtype', 'bfloat16', '--gpu-memory-utilization', str(memory_fraction),
                   '--safetensors-load-strategy', 'eager',
                   '--max-model-len', str(context), '--max-num-seqs', '1',
                   '--max-num-batched-tokens', str(PREFILL_TOKENS), '--enforce-eager', '--disable-log-requests']
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=gpu)
        if tensor_parallel_size>1:
            command += ['--tensor-parallel-size',str(tensor_parallel_size),'--disable-custom-all-reduce']
        env.pop('PYTHONPATH', None)
        if role == 'actor':
            command += ['--enable-auto-tool-choice', '--tool-call-parser', 'hermes']
        else:
            overlay = RUNTIME/'judge_packages'
            if not (overlay/'receipt.json').exists():
                raise RuntimeError('Project judge compatibility packages are missing')
            env['PYTHONPATH'] = str(overlay)
            command += ['--limit-mm-per-prompt', '{"image":0,"video":0}']
        log_path = self.output/f'{role}-model.log'
        with log_path.open('a') as log:
            proc = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        lease = {'pid': proc.pid, 'start_ticks': identity(proc.pid), 'gpu': gpu,
                 'model': alias, 'model_path': str(model.resolve()), 'command': command,
                 'url': f'http://127.0.0.1:{port}', 'memory_fraction': memory_fraction,
                 'max_model_len': context,
                 'tensor_parallel_size':tensor_parallel_size,'physical_gpus':group,
                 'snapshot_before_start': snapshot, 'headroom_mib': GPU_HEADROOM_MIB,
                 'log': str(log_path), 'started_at': time.time(), 'released': False}
        self.owned[role] = lease
        self.save()

    async def ready(self):
        deadline = time.monotonic()+1200
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            while True:
                ready = []
                for role, lease in self.owned.items():
                    if identity(lease['pid']) != lease['start_ticks']:
                        raise RuntimeError(f'{role} model exited; see {lease["log"]}')
                    try:
                        if (await client.get(lease['url']+'/health')).status_code == 200:
                            ready.append(role)
                    except httpx.HTTPError:
                        pass
                self.progress('loading_models', ready=ready,
                              gpus={r: l['gpu'] for r, l in self.owned.items()})
                if self.owned and len(ready) == len(self.owned):
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError('Dedicated models did not become ready in 1200 seconds')
                await asyncio.sleep(5)

    async def close(self):
        for lease in self.owned.values():
            if not lease['released'] and identity(lease['pid']) == lease['start_ticks']:
                command = Path(f'/proc/{lease["pid"]}/cmdline').read_bytes()
                if b'vllm.entrypoints.openai.api_server' not in command or lease['model'].encode() not in command:
                    raise RuntimeError('Owned model identity no longer matches')
                os.killpg(lease['pid'], signal.SIGTERM)
        deadline = time.monotonic()+30
        while time.monotonic() < deadline and any(identity(l['pid']) == l['start_ticks']
                                                  for l in self.owned.values()):
            await asyncio.sleep(.5)
        for lease in self.owned.values():
            if identity(lease['pid']) == lease['start_ticks']:
                os.killpg(lease['pid'], signal.SIGKILL)
            lease['released'] = True
            lease['released_at'] = time.time()
        self.save()


class LocalEvalClient(LLMClient):
    """Small tracked bridge for the official simulator and semantic grader."""
    def __init__(self, url, model, output, seed, timeout=90, *, require_complete=False, context_tokens=None,
                 reason_first_grading=False):
        self.url, self.model, self.output, self.seed = url, model, Path(output), seed
        self.require_complete = require_complete
        self.context_tokens = context_tokens
        self.reason_first_grading=reason_first_grading
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        self.usage = {'num_calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
                      'total_tokens': 0, 'failed_calls': 0}
        self.metered_calls = 0
        self.seconds = 0.0

    async def chat(self, messages, tools=None, temperature=0, max_tokens=4096, response_format=None):
        payload = {'model': self.model, 'messages': _messages_to_dicts(messages),
                   'temperature': temperature, 'max_tokens': max_tokens,
                   'seed': self.seed+self.usage['num_calls'],
                   'chat_template_kwargs': {'enable_thinking': False}}
        if response_format:
            response_format = constrained_response_format(response_format)
            if self.reason_first_grading:
                response_format=evidence_first_grading_format(response_format)
            payload['response_format'] = response_format
            if response_format.get('type') == 'json_schema':
                # vLLM constrains decoding but does not insert the schema into
                # the model's textual prompt. Make required keys visible so a
                # small local model does not stall in optional JSON whitespace.
                schema = response_format['json_schema']['schema']
                instruction = (
                    '\n\nOUTPUT FORMAT: Return one compact JSON object with no optional whitespace, '
                    'indentation, blank lines or Markdown. Include EVERY required field, including '
                    'each criterion explanation when present. The output must match this JSON Schema:\n'
                    + json.dumps(schema, separators=(',', ':')))
                if response_format['json_schema'].get('name') == 'semantic_communication_grade':
                    if self.reason_first_grading:
                        instruction += ('\nFor each criterion, first explain whether every required part is satisfied, '
                            'then cite exact evidence, then output met consistent with that explanation. '
                            'Use the schema field order: criterion_id, explanation, evidence, met.')
                    instruction += (
                        '\nEVIDENCE FIELD FORMAT: When contradiction_found is false, '
                        'contradiction_evidence MUST be the empty string "". Never put '
                        '"none", "N/A", an explanation, or a summary in that field. '
                        'When contradiction_found is true, contradiction_evidence MUST '
                        'contain ONLY a contiguous exact excerpt copied from an '
                        'assistant_messages entry. Put your reasoning in a criterion '
                        'explanation, not inside a quote. Do not add ellipses, labels, '
                        'quotation wrappers, or connecting words to any evidence. '
                        'Each criteria[].evidence entry follows the same exact-excerpt '
                        'rule; use [] when no excerpt exists. Preserve every criterion '
                        'and its independent true/false judgement under the original rubric.')
                if payload['messages'] and payload['messages'][0]['role'] == 'system':
                    payload['messages'][0]['content'] += instruction
                else:
                    payload['messages'].insert(0, {'role': 'system', 'content': instruction.strip()})
        if tools:
            payload['tools'] = [tool.model_dump() for tool in tools]
        start = time.monotonic()
        body = None
        budget = None
        try:
            if self.context_tokens is not None:
                # Count the exact final prompt, including the structured-output
                # instructions above. Preserve every rubric and transcript item;
                # fit the output allowance to the server's remaining capacity.
                token_request = {key: payload[key] for key in ('model', 'messages', 'chat_template_kwargs')}
                if tools:
                    token_request['tools'] = payload['tools']
                tokenized = await self.client.post(self.url+'/tokenize', json=token_request)
                tokenized.raise_for_status()
                counted = tokenized.json()
                count = int(counted['count'])
                limit = min(self.context_tokens, int(counted.get('max_model_len', self.context_tokens)))
                remaining = limit - count - 64
                budget = {'input_tokens': count, 'context_tokens': limit,
                          'requested_output_tokens': max_tokens, 'headroom_tokens': 64}
                if count < 0 or remaining < 128:
                    raise RuntimeError(f'Evaluation context exhausted: input={count}, limit={limit}, remaining={remaining}; evidence retained')
                payload['max_tokens'] = min(max_tokens, remaining)
                budget['output_tokens'] = payload['max_tokens']
            self.usage['num_calls'] += 1
            response = await self.client.post(self.url+'/v1/chat/completions', json=payload)
            response.raise_for_status()
            body = response.json()
            usage = normalize_usage(body.get('usage'))
            self.metered_calls += 1
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                self.usage[key] += usage[key]
            message = body['choices'][0]['message']
            result = ChatResponse(content=message.get('content') or '', tool_calls=parse_tool_calls(message),
                                  finish_reason=body['choices'][0].get('finish_reason'), usage=usage)
            if self.require_complete and result.finish_reason != 'stop':
                from kb.benchmark.crm_dialogue import SimulatorFidelityError
                raise SimulatorFidelityError(f'Incomplete customer output: finish_reason={result.finish_reason}')
        except BaseException as exc:
            self.usage['failed_calls'] += 1
            self.log({'request': payload, 'response': body, 'error': type(exc).__name__, 'context_budget': budget})
            raise
        else:
            self.log({'request': payload, 'response': body, 'context_budget': budget})
            return result
        finally:
            self.seconds += time.monotonic()-start

    def log(self, value):
        with self.output.open('a') as f:
            f.write(json.dumps(value, ensure_ascii=False, default=str)+'\n')

    def stream_chat(self, *args, **kwargs):
        raise NotImplementedError('Benchmark uses non-streaming evaluation calls')

    async def aclose(self):
        await self.client.aclose()
