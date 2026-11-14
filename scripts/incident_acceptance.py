#!/usr/bin/env python
"""Executable intake acceptance using synthetic logs and rendered screenshots."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import statistics
import time

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from kb.app import create_app
from kb.auth import create_user
from kb.config import Settings
from kb.db import Database
from kb.ingestion import register_document, ingest
from kb.agent import Harness


CASES = [
    ('ssh', 'Ubuntu 24.04\nSSH connection failed\nPermission denied',
     [('environment', 'Ubuntu 24.04'), ('software', 'SSH'), ('symptom', 'Permission denied')]),
    ('vpn', 'Windows 11\nVPN\nAuthentication failed\nHTTP 403',
     [('environment', 'Windows 11'), ('software', 'VPN'), ('symptom', 'Authentication failed'), ('error_code', 'HTTP 403')]),
    ('docker', 'Ubuntu 22.04\nDocker daemon\nConnection refused',
     [('environment', 'Ubuntu 22.04'), ('software', 'Docker'), ('symptom', 'Connection refused')]),
    ('python', 'Linux\nPython\nMemoryError\nOut of memory',
     [('environment', 'Linux'), ('software', 'Python'), ('error_code', 'MemoryError'), ('symptom', 'Out of memory')]),
    ('nginx', 'Nginx\nHTTP 502\nConnection timed out',
     [('software', 'Nginx'), ('error_code', 'HTTP 502'), ('symptom', 'Connection timed out')]),
    ('database', 'Windows 10\nMySQL\nAccess denied',
     [('environment', 'Windows 10'), ('software', 'MySQL'), ('symptom', 'Access denied')]),
]


def normalized(pair):
    return pair[0], ''.join(pair[1].casefold().split())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); output = args.output.resolve()
    if not output.is_relative_to('/data/zjr/enterprise_kb_agent'):
        parser.error('Outputs must be under /data/zjr/enterprise_kb_agent')
    if (output/'report.json').exists(): parser.error('Use a fresh output; preserve prior results')
    output.mkdir(parents=True, exist_ok=True)
    cfg = Settings(root=output/'app', mode='extractive', llm_url='', llm_model='', embedding_url='', embedding_model='')
    cfg.prepare(); db = Database(cfg.db_path); db.initialize()
    password = secrets.token_urlsafe(18)
    for user, role in [('admin', 'admin'), ('member', 'member'), ('second', 'member')]:
        create_user(db, user, password, role)
    credentials = output/'credentials.json'; credentials.write_text(json.dumps({'password': password})); credentials.chmod(0o600)
    sources = output/'inputs'; sources.mkdir(exist_ok=True)
    records = []
    with TestClient(create_app(cfg, start_workers=False), headers={'X-KB-Client': 'web'}) as client:
        def login(user):
            response = client.post('/api/login', json={'username': user, 'password': password})
            assert response.status_code == 200
        login('admin')
        base = client.post('/api/bases', json={'name': '故障排查手册'}).json()['id']
        manual = ('SSH Permission denied：先核对用户名、公钥和服务器authorized_keys。\n'
                  'VPN Authentication failed、HTTP 403：核对账户状态及访问授权。\n'
                  'Docker Connection refused：核对服务是否启动。\n'
                  'Python MemoryError、Out of memory：检查进程内存和输入规模。\n'
                  'Nginx HTTP 502、Connection timed out：检查上游服务和连接超时。\n'
                  'MySQL Access denied：核对数据库账户和权限。')
        doc, _ = register_document(db, cfg, base, '故障排查.md', manual.encode()); ingest(db, cfg, doc['id'])
        for index, (name, text, expected) in enumerate(CASES):
            for kind in ['log', 'screenshot']:
                login('member')
                if kind == 'log': data, filename = text.encode(), name+'.log'
                else:
                    dark = index % 2 == 1
                    image = Image.new('RGB', (1150, 360), '#20252b' if dark else '#f9fafb')
                    draw = ImageDraw.Draw(image)
                    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 30 + 2*(index % 3))
                    draw.multiline_text((32, 30), text, font=font, fill='white' if dark else '#202020', spacing=18)
                    buffer = io.BytesIO(); image.save(buffer, format='PNG'); data, filename = buffer.getvalue(), name+'.png'
                (sources/filename).write_bytes(data)
                start = time.monotonic()
                response = client.post('/api/incident-evidence', files={'file': (filename, data)})
                assert response.status_code == 201, response.text
                evidence = response.json()
                predicted = {normalized((f['kind'], f['value'])) for f in evidence['fields']}
                gold = {normalized(pair) for pair in expected}
                lines = {line['number']: line['text'] for line in evidence['lines']}
                grounded = all(lines[f['source_line']][f['span'][0]:f['span'][1]] == f['value'] for f in evidence['fields'])
                query = '请根据知识库帮助我排查：' + '；'.join(f['value'] for f in evidence['fields'])
                request = client.post('/api/chat', json={'question': query, 'base_ids': [base]})
                assert request.status_code == 202, request.text
                run_id = request.json()['run_id']; Harness(db, cfg).execute(run_id)
                answer = client.get('/api/runs/'+run_id).json()
                body = {'digest': evidence['digest'], 'title': evidence['suggested_title'],
                        'description': evidence['suggested_description'], 'confirmed': True}
                draft = client.post('/api/incident-evidence/'+evidence['id']+'/draft', json=body)
                assert draft.status_code == 201, draft.text
                ticket = draft.json()
                duplicate = client.post('/api/incident-evidence/'+evidence['id']+'/draft', json=body)
                assert duplicate.json()['id'] == ticket['id']
                submitted = client.post('/api/tickets/'+ticket['id']+'/action', json={'action': 'submit', 'version': 1})
                assert submitted.json()['status'] == 'submitted'
                login('second')
                assert client.get('/api/incident-evidence/'+evidence['id']).status_code == 404
                assert client.get('/api/incident-evidence/'+evidence['id']+'/image').status_code == 404
                login('admin')
                assert client.post('/api/tickets/'+ticket['id']+'/action', json={'action': 'accept', 'version': 2}).json()['status'] == 'processing'
                assert client.post('/api/tickets/'+ticket['id']+'/action', json={'action': 'resolve', 'version': 3, 'note': '验收完成'}).json()['status'] == 'resolved'
                record = dict(case=name, kind=kind, expected_fields=len(gold), correct_fields=len(gold & predicted),
                    predicted_fields=len(predicted), fields_complete=gold <= predicted, grounded=grounded,
                    lookup_status=answer['status'], citations=len(answer['citations']), ticket_id=ticket['id'],
                    lifecycle_passed=True, seconds=time.monotonic()-start, evidence=evidence)
                records.append(record)
                print(json.dumps({k:v for k,v in record.items() if k != 'evidence'}, ensure_ascii=False), flush=True)
    durations=sorted(r['seconds'] for r in records)
    report = {'scope': '12 synthetic log/rendered-screenshot fixtures; actual CPU OCR, extractive retrieval and real authenticated ticket state transitions',
        'natural_screenshot_benchmark': False, 'llm_calls': 0, 'gpu_required': False,
        'cases': len(records), 'field_recall': sum(r['correct_fields'] for r in records)/sum(r['expected_fields'] for r in records),
        'field_precision': sum(r['correct_fields'] for r in records)/sum(r['predicted_fields'] for r in records),
        'all_fields_correct_cases': sum(r['fields_complete'] for r in records),
        'grounded_cases': sum(r['grounded'] for r in records),
        'ticket_lifecycles_passed': sum(r['lifecycle_passed'] for r in records),
        'lookup_with_citations': sum(r['citations'] > 0 for r in records),
        'case_seconds_p50': statistics.median(durations),
        'case_seconds_p95': statistics.quantiles(durations,n=100,method='inclusive')[94],
        'records': records}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k != 'records'}, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
