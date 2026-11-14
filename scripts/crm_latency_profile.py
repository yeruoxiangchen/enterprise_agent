"""Opt-in wall-clock observers; graph, prompt and tool behavior stay unchanged."""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack, contextmanager
import functools
import json
from pathlib import Path
import sqlite3
import threading
import time
from unittest.mock import patch


class LatencyProfile:
    def __init__(self, output):
        self.output = Path(output)
        self.lock = threading.Lock()
        self.values = defaultdict(lambda: {'calls': 0, 'seconds': 0.0, 'max_seconds': 0.0})

    def add(self, name, seconds):
        with self.lock:
            r = self.values[name]
            r['calls'] += 1
            r['seconds'] += seconds
            r['max_seconds'] = max(r['max_seconds'], seconds)

    def wrap(self, fn, name):
        @functools.wraps(fn)
        def timed(*a, **kw):
            start = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                self.add(name, time.perf_counter()-start)
        return timed

    def save(self):
        with self.lock:
            body = json.dumps({'inclusive_times_may_overlap': True, 'timings': dict(self.values)}, indent=2)
        temporary = self.output.with_suffix('.tmp')
        temporary.write_text(body+'\n')
        temporary.replace(self.output)

    @contextmanager
    def observe(self):
        from langgraph.checkpoint.sqlite import SqliteSaver
        from kb.benchmark import crm_adapter, crm_supervisor
        from kb.context import ContextBudget
        profile = self

        class Cursor(sqlite3.Cursor):
            def execute(self, *a, **kw):
                return profile.wrap(super().execute, 'sqlite.execute')(*a, **kw)

            def executemany(self, *a, **kw):
                return profile.wrap(super().executemany, 'sqlite.executemany')(*a, **kw)

        class Connection(sqlite3.Connection):
            def cursor(self, *a, **kw):
                kw.setdefault('factory', Cursor)
                return super().cursor(*a, **kw)

            def commit(self):
                return profile.wrap(super().commit, 'sqlite.commit')()

        original_connect = sqlite3.connect

        def connect(*a, **kw):
            kw.setdefault('factory', Connection)
            return original_connect(*a, **kw)

        original_run = crm_adapter.CRMAdapter.run
        original_meter = crm_supervisor.ExternalBusinessQA.meter

        @functools.wraps(original_meter)
        def meter(engine, request, handler):
            role = ('supervisor' if any(t.name.startswith('delegate_') for t in request.tools) else 'specialist')
            before_api, start = engine.owner.llm_seconds, time.perf_counter()
            try:
                return original_meter(engine, request, handler)
            finally:
                profile.add('model_step.'+role, time.perf_counter()-start)
                profile.add('api.'+role, engine.owner.llm_seconds-before_api)

        @functools.wraps(original_run)
        async def run(owner, *a, **kw):
            try:
                return await original_run(owner, *a, **kw)
            finally:
                profile.save()

        with ExitStack() as stack:
            stack.enter_context(patch.object(sqlite3, 'connect', connect))
            stack.enter_context(patch.object(crm_adapter.CRMAdapter, 'run', run))
            stack.enter_context(patch.object(crm_supervisor.ExternalBusinessQA, '__init__',
                self.wrap(crm_supervisor.ExternalBusinessQA.__init__, 'engine_init')))
            stack.enter_context(patch.object(crm_supervisor.ExternalBusinessQA, 'meter', meter))
            stack.enter_context(patch.object(ContextBudget, 'count', self.wrap(ContextBudget.count, 'token_count')))
            stack.enter_context(patch.object(crm_supervisor, 'create_agent', self.wrap(crm_supervisor.create_agent, 'graph_build')))
            stack.enter_context(patch.object(crm_supervisor.ExternalBusinessQA, 'save', self.wrap(crm_supervisor.ExternalBusinessQA.save, 'business_save')))
            for method in ['put', 'put_writes', 'get_tuple', 'setup']:
                stack.enter_context(patch.object(SqliteSaver, method, self.wrap(getattr(SqliteSaver, method), 'checkpoint.'+method)))
            for method in ['emit', 'write_checkpoint', 'write_environment']:
                stack.enter_context(patch.object(crm_adapter.CRMAdapter, method,
                                                self.wrap(getattr(crm_adapter.CRMAdapter, method), method)))
            try:
                yield self
            finally:
                self.save()
