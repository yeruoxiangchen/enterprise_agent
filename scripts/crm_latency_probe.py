"""Replay frozen actor decisions through the real graphs and canonical tools.

This measures host execution, not model generation or a new task success rate.
Original dialogue, outputs, tool effects and model-call counts must match.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import copy
import functools
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from unittest.mock import patch


class Timings:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = defaultdict(lambda: {'calls': 0, 'seconds': 0.0, 'max_seconds': 0.0})

    def add(self, name, seconds):
        with self.lock:
            v = self.values[name]
            v['calls'] += 1
            v['seconds'] += seconds
            v['max_seconds'] = max(v['max_seconds'], seconds)

    def wrap(self, original, label):
        @functools.wraps(original)
        def measured(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                name = label(*args, **kwargs) if callable(label) else label
                self.add(name, time.perf_counter() - started)
        return measured


def load_recording(run, task, arm):
    row = json.loads((run/'rows'/f'{task}__0__{arm}.json').read_text())
    folder = Path(row['trial_output'])
    if arm == 'multi_v6':
        events = [json.loads(line) for line in (folder/'events.jsonl').read_text().splitlines()]
        responses = []
        request_started = None
        for e in events:
            if e['kind'] == 'model_request':
                request_started = e['time']
            elif e['kind'] == 'model_response':
                if request_started is None:
                    raise RuntimeError('Response has no recorded request')
                responses.append(e['data'] | {'recorded_wait_seconds': max(0, e['time']-request_started)})
                request_started = None
        counts = [e['data']['input_tokens'] for e in events if e['kind'] == 'business_model']
    else:
        responses = [json.loads(line)['response'] for line in (folder/'actor-model.jsonl').read_text().splitlines()]
        counts = [r['usage']['prompt_tokens'] for r in responses]
    if len(responses) != len(counts):
        raise RuntimeError(f'Incomplete model recording: {task}/{arm}')
    return row, responses, counts


def normalized_calls(calls):
    result = []
    for c in calls:
        value = c['result']
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            pass
        result.append({k: c.get(k) for k in ['name', 'arguments', 'ok', 'backend_executed']}
                      | {'result': value})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--input-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tasks', nargs='+', required=True)
    parser.add_argument('--arms', nargs='+', default=['multi_v6'], choices=['multi_v6', 'official_graph'])
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--replay-model-wait', action='store_true',
        help='Also replay the original measured API wait; isolates checkpoint overlap with identical actor decisions')
    parser.add_argument('--tokenize-url', help='Optional owned tokenizer endpoint; otherwise replay original token counts')
    parser.add_argument('--model', default='crm-qwen3-8b')
    args = parser.parse_args()
    if args.replay_model_wait and args.arms != ['multi_v6']:
        parser.error('Recorded per-request waits are available for multi_v6 only')
    if args.output.exists():
        parser.error('Use a fresh output directory')
    if not args.output.resolve().is_relative_to(Path('/data/zjr/enterprise_kb_agent')):
        parser.error('Runtime outputs must be under /data/zjr/enterprise_kb_agent')
    args.output.mkdir(parents=True)
    root = args.source_root.resolve()
    sys.path[:0] = [str(root), str(root/'scripts')]

    import langchain_core.language_models.chat_models as chat_models
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.checkpoint.sqlite import SqliteSaver
    from harness.bench_runner import load_tasks
    from harness.models import ChatMessage, ChatResponse, Role, ToolCall
    from harness.tools.environment import create_environment
    import harness.tools.domains
    from kb.benchmark import crm_adapter, crm_supervisor
    from kb.benchmark.crm_reference import OfficialGraphAdapter
    from kb.config import Settings
    from kb.context import ContextBudget

    class ReplayModel(chat_models.BaseChatModel):
        replies: list
        index: int = 0

        @property
        def _llm_type(self):
            return 'frozen-actor-response-replay'

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, **kwargs):
            r = self.replies[self.index]
            self.index += 1
            if args.replay_model_wait:
                time.sleep(r['recorded_wait_seconds'])
            msg = AIMessage(content=r['content'], tool_calls=copy.deepcopy(r['tool_calls']),
                invalid_tool_calls=copy.deepcopy(r.get('invalid_tool_calls', [])), usage_metadata=r.get('usage'))
            return ChatResult(generations=[ChatGeneration(message=msg)])

    class ReplayHTTP:
        def __init__(self, replies):
            self.replies, self.index, self.seed = replies, 0, 0
            self.usage = dict(num_calls=0, prompt_tokens=0, completion_tokens=0, total_tokens=0)
            self.metered_calls, self.seconds = 0, 0.0

        async def chat(self, messages, **kwargs):
            r = self.replies[self.index]
            self.index += 1
            self.usage['num_calls'] += 1
            self.metered_calls += 1
            for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                self.usage[k] += r['usage'][k]
            raw = r['choices'][0]
            calls = [ToolCall(id=t['id'], name=t['function']['name'],
                             arguments=json.loads(t['function']['arguments']))
                     for t in raw['message'].get('tool_calls') or []]
            return ChatResponse(content=raw['message'].get('content') or '', tool_calls=calls,
                                finish_reason=raw.get('finish_reason'))

        async def aclose(self):
            pass

    timings = Timings()

    class MeasuredCursor(sqlite3.Cursor):
        def execute(self, *a, **kw):
            return timings.wrap(super().execute, 'sqlite.execute')(*a, **kw)

        def executemany(self, *a, **kw):
            return timings.wrap(super().executemany, 'sqlite.executemany')(*a, **kw)

    class MeasuredConnection(sqlite3.Connection):
        def cursor(self, *a, **kw):
            kw.setdefault('factory', MeasuredCursor)
            return super().cursor(*a, **kw)

        def commit(self):
            return timings.wrap(super().commit, 'sqlite.commit')()

    original_connect = sqlite3.connect

    def connect(*a, **kw):
        kw.setdefault('factory', MeasuredConnection)
        return original_connect(*a, **kw)

    original_count = ContextBudget.count
    active = {}

    def count(self, messages, tools):
        started = time.perf_counter()
        try:
            i = active['count_index']
            active['count_index'] += 1
            if args.tokenize_url:
                return original_count(self, messages, tools)
            return active['counts'][i], 'recorded_server_count'
        finally:
            timings.add('token_count', time.perf_counter() - started)

    protocol = json.loads((args.input_run/'protocol.json').read_text())
    tasks_path = args.input_run/'source/upstream/src/tasks/itsm.yaml'
    tasks = {t.id: t for t in load_tasks(str(tasks_path))}
    rows = []
    patches = [patch.object(ContextBudget, 'count', count), patch.object(sqlite3, 'connect', connect),
        patch.object(crm_supervisor, 'create_agent', timings.wrap(crm_supervisor.create_agent, 'graph_build')),
        patch.object(crm_supervisor.ExternalBusinessQA, 'save', timings.wrap(crm_supervisor.ExternalBusinessQA.save, 'business_save')),
        patch.object(CompiledStateGraph, 'invoke', timings.wrap(CompiledStateGraph.invoke,
            lambda self, *a, **kw: 'graph_invoke.' + str(self.name))),
    ]
    for name in ('put', 'put_writes', 'get_tuple', 'setup'):
        patches.append(patch.object(SqliteSaver, name, timings.wrap(getattr(SqliteSaver, name), 'checkpoint.'+name)))
    for cls in [crm_adapter.CRMAdapter, OfficialGraphAdapter]:
        for name in ['emit', 'write_environment', 'write_checkpoint']:
            if hasattr(cls, name):
                patches.append(patch.object(cls, name, timings.wrap(getattr(cls, name), name)))
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        for repeat in range(args.repeats):
            for task_id in args.tasks:
                for arm in args.arms:
                    recording, responses, counts = load_recording(args.input_run, task_id, arm)
                    active.update(counts=counts, count_index=0)
                    model = ReplayModel(replies=responses) if arm == 'multi_v6' else ReplayHTTP(responses)
                    folder = args.output/f'{repeat}_{task_id}_{arm}'
                    folder.mkdir()
                    cfg = Settings(root=folder, llm_url='http://replay.invalid/v1', llm_model=args.model,
                        tokenize_url=args.tokenize_url, context_tokens=protocol['context_tokens'],
                        output_tokens=protocol['output_tokens'])
                    common = dict(environment=create_environment('itsm', tasks[task_id].initial_state), settings=cfg,
                        config=protocol['public_agent_config'], policy=protocol['public_policy'], seed=recording['seed'], output=folder)
                    owner = (crm_adapter.CRMAdapter(arm='multi_v6', execution_adapter='v4', **common)
                             if arm == 'multi_v6' else OfficialGraphAdapter(**common))
                    if arm == 'official_graph':
                        asyncio.run(owner.http.aclose())
                        owner.http = model
                    expected = recording['trace']['turns']
                    dialogue = []
                    error = None
                    started = time.perf_counter()

                    async def replay():
                        nonlocal error
                        history = []
                        for i, utterance in enumerate(expected):
                            if utterance['role'] != 'user':
                                continue
                            original = expected[i+1] if i+1 < len(expected) else None
                            # run_task retains the customer's final, unprocessed
                            # message when its dialogue-turn limit is reached.
                            if (original is None or original['role'] != 'assistant') and recording['trace'].get('error') is None:
                                break
                            try:
                                response = await owner.run(utterance['content'], history)
                            except Exception as exc:
                                error = f'{type(exc).__name__}: {exc}'
                                break
                            if original is None or original['role'] != 'assistant' or response.content != original['content']:
                                raise RuntimeError(f'Reply replay mismatch at {task_id}/{arm}/turn {owner.turn-1}')
                            dialogue.append(response.content)
                            history.extend([ChatMessage(role=Role.USER, content=utterance['content']),
                                            ChatMessage(role=Role.ASSISTANT, content=response.content)])

                    with patch.object(crm_adapter, 'ChatOpenAI', lambda **kw: model):
                        asyncio.run(replay())
                    elapsed = time.perf_counter()-started
                    if error != recording['trace'].get('error'):
                        raise RuntimeError(f'Error replay mismatch: {error} / {recording["trace"].get("error")}')
                    if model.index != len(responses) or active['count_index'] != len(counts):
                        raise RuntimeError('Model/count recording was not consumed exactly')
                    if normalized_calls(owner.tool_calls) != normalized_calls(recording['trace']['tool_calls_detail']):
                        raise RuntimeError(f'Tool replay mismatch: {task_id}/{arm}')
                    original_env = json.loads((Path(recording['trial_output'])/'environment.json').read_text())
                    if owner.environment.get_state() != original_env:
                        raise RuntimeError(f'Environment replay mismatch: {task_id}/{arm}')
                    result = dict(task=task_id, arm=arm, repeat=repeat, wall_seconds=elapsed,
                        agent_seconds=sum(t['seconds'] for t in owner.turn_times),
                        model_request_seconds=owner.llm_seconds, model_calls=owner.model_calls,
                        calls_and_effects_match=True, responses_match=True, error=error)
                    rows.append(result)
                    report = {'scope': 'frozen model response replay; not new inference or accuracy',
                        'source_root': str(root), 'token_count_mode': 'live' if args.tokenize_url else 'recorded',
                        'replayed_recorded_model_wait': args.replay_model_wait,
                        'component_times_are_inclusive_and_can_overlap': True,
                        'rows': rows, 'timings': dict(timings.values)}
                    (args.output/'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
                    print(json.dumps(result), flush=True)
    print(json.dumps({'timings': dict(timings.values)}), flush=True)


if __name__ == '__main__':
    main()
