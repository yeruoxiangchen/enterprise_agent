import asyncio
import functools
import json
from pathlib import Path
import subprocess
import sys
import sqlite3
from contextlib import contextmanager

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
import pytest

from test_crm_benchmark import reply
from test_crm_supervisor import delegate, setup, text


def test_async_checkpoints_keep_supervisor_and_specialist_execution(tmp_path, monkeypatch):
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'get'), text('TCK-1001 is Open.'), text('TCK-1001 is Open.')])
    modes = []
    sync_levels = []
    invoke = CompiledStateGraph.invoke
    connection = SqliteSaver.from_conn_string

    @contextmanager
    def observe_connection(*a, **kw):
        with connection(*a, **kw) as saver:
            yield saver
            sync_levels.append(saver.conn.execute('PRAGMA synchronous').fetchone()[0])

    @functools.wraps(invoke)
    def observe(graph, *a, **kw):
        if graph.name == 'supervisor':
            modes.append(kw.get('durability'))
        return invoke(graph, *a, **kw)
    monkeypatch.setattr(CompiledStateGraph, 'invoke', observe)
    monkeypatch.setattr(SqliteSaver, 'from_conn_string', observe_connection)
    result = asyncio.run(owner.run('What is the status of TCK-1001?', []))
    assert result.content == 'TCK-1001 is Open.' and owner.model_calls == 4
    assert modes == ['async']
    assert sync_levels == [2]  # SQLite FULL is retained; only scheduling changes.
    assert (tmp_path/'supervisor-checkpoints.sqlite3').exists()


@pytest.mark.parametrize('phase,exit_code,status', [('committed', 77, 'answered'), ('unknown', 78, 'insufficient')])
@pytest.mark.parametrize('delay_checkpoints', [False, True])
def test_hard_process_exit_retains_receipts_without_duplicate_backend_write(tmp_path, phase, exit_code, status, delay_checkpoints):
    worker = Path(__file__).with_name('crm_checkpoint_process.py')
    args = [sys.executable, '-B', str(worker), str(tmp_path), phase]
    crash_args = [*args, 'crash'] + (['--delay-checkpoints'] if delay_checkpoints else [])
    crashed = subprocess.run(crash_args, capture_output=True, text=True, timeout=90)
    assert crashed.returncode == exit_code, crashed.stdout+crashed.stderr
    assert not (tmp_path/'resumed.json').exists()
    if delay_checkpoints:
        with sqlite3.connect(tmp_path/'supervisor-checkpoints.sqlite3') as connection:
            assert connection.execute('SELECT count(*) FROM checkpoints').fetchone()[0] == 0
            assert connection.execute('SELECT count(*) FROM writes').fetchone()[0] == 0
    resumed = subprocess.run([*args, 'resume'], capture_output=True, text=True, timeout=90)
    assert resumed.returncode == 0, resumed.stdout+resumed.stderr
    result = json.loads((tmp_path/'resumed.json').read_text())
    assert result['comments'] == ['Once'] and result['backend_writes'] == 1
    assert result['status'] == status
    if phase == 'unknown':
        assert 'uncertain' in result['answer'] or 'reconciliation' in result['answer']


def test_latency_observer_preserves_business_calls_and_counts(tmp_path, monkeypatch):
    from crm_latency_profile import LatencyProfile
    owner, _ = setup(tmp_path, monkeypatch, [delegate('service'),
        reply('get_ticket', {'ticket_id': 'TCK-1001'}, 'get'), text('TCK-1001 is Open.'), text('TCK-1001 is Open.')])
    output = tmp_path/'latency.json'
    with LatencyProfile(output).observe():
        response = asyncio.run(owner.run('What is the status of TCK-1001?', []))
    profile = json.loads(output.read_text())['timings']
    assert response.content == 'TCK-1001 is Open.' and owner.model_calls == 4
    assert [c['name'] for c in owner.tool_calls if c.get('backend_executed')] == ['get_ticket']
    assert profile['token_count']['calls'] == 4 and profile['sqlite.commit']['calls'] > 0
