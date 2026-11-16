#!/usr/bin/env python
"""Tiny public screenshot text-coverage check through the real intake API.

Uses eight OCRBench-v2 website screenshots and their published answer strings.
The task is answer-text recovery from OCR, not visual question answering or an
official OCRBench score. Images, original annotations and results stay on /data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import statistics
import time
import unicodedata
import urllib.parse
import urllib.request

from fastapi.testclient import TestClient

from kb.app import create_app
from kb.auth import create_user
from kb.config import Settings
from kb.db import Database


ROOT = Path('/data/zjr/enterprise_kb_agent')
DATASET = 'ling99/OCRBench_v2'
# Frozen before inference: first eight direct visible-text website questions in
# the inspected 3000--3299 metadata window. Interpretive questions 3052/3062
# are excluded; no selection depends on OCR predictions.
IDS = (3011, 3043, 3064, 3074, 3144, 3186, 3197, 3214)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def normalize(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    # Currency symbols carry information: "$5" must not match a stray "5"
    # in an unrelated URL. Underscores are layout punctuation, not letters.
    return ''.join(character for character in text if character.isalnum() or character in '$€£¥')


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    manifest = root/'manifest.json'
    if manifest.exists():
        return manifest
    records = []
    for ident in IDS:
        metadata_path = root/f'{ident}.json'
        if metadata_path.exists():
            response = json.loads(metadata_path.read_text())
        else:
            query = urllib.parse.urlencode(dict(dataset=DATASET, config='default', split='test',
                                               offset=ident, length=1))
            with urllib.request.urlopen('https://datasets-server.huggingface.co/rows?'+query, timeout=45) as stream:
                response = json.load(stream)
            write(metadata_path, response)
        row = response['rows'][0]['row']
        assert row['id'] == ident and row['dataset_name'] == 'website_screenshot'
        image = root/f'{ident}.jpg'
        if not image.exists():
            with urllib.request.urlopen(row['image']['src'], timeout=45) as stream:
                content = stream.read(5*1024*1024+1)
            if len(content) > 5*1024*1024:
                raise ValueError(f'Public image {ident} exceeds the intake size limit; retain this failure')
            image.write_bytes(content)
        records.append(dict(id=ident, file=image.name, sha256=sha(image.read_bytes()),
                            annotation_file=metadata_path.name, annotation_sha256=sha(metadata_path.read_bytes()),
                            question=row['question'], answers=row['answers'], original_type=row['type']))
        print(json.dumps({'prepared_public_id': ident, 'bytes': image.stat().st_size}), flush=True)
    write(manifest, dict(version=1, dataset=DATASET, split='test', source='website_screenshot',
        upstream='https://github.com/qywh2023/OCRBench/tree/main/OCRBench_v2',
        dataset_url='https://huggingface.co/datasets/'+DATASET,
        scope='Eight manually selected public screenshots; answer-text recovery from OCR only. '
              'Not an official VQA score, dense OCR accuracy, or enterprise workflow benchmark.',
        selection='Fixed IDs selected from direct visible-text questions before inference; all selected IDs retained.',
        metric='NFKC/casefold/alphanumeric-and-currency-v2 normalization; any complete published answer must be a contiguous '
               'substring of the OCR transcript. Per-answer token coverage is secondary.',
        images=records))
    return manifest


def evaluate(manifest, output):
    dataset = json.loads(manifest.read_text())
    if output.exists():
        raise ValueError('Use a fresh result directory; preserve prior attempts')
    output.mkdir(parents=True)
    write(output/'manifest.json', dataset)
    cfg = Settings(root=output/'app', mode='extractive', llm_url='', llm_model='',
                   embedding_url='', embedding_model='')
    cfg.prepare(); db = Database(cfg.db_path); db.initialize()
    password = secrets.token_urlsafe(18)
    create_user(db, 'public-reader', password, 'member')
    rows = []
    with TestClient(create_app(cfg, start_workers=False), headers={'X-KB-Client':'web'}) as client:
        assert client.post('/api/login', json={'username':'public-reader','password':password}).status_code == 200
        for record in dataset['images']:
            image = manifest.parent/record['file']; content = image.read_bytes()
            assert sha(content) == record['sha256'], 'Public image changed'
            assert sha((manifest.parent/record['annotation_file']).read_bytes()) == record['annotation_sha256']
            start = time.monotonic()
            response = client.post('/api/incident-evidence', files={'file':(image.name,content,'image/jpeg')})
            result = dict(id=record['id'], answers=record['answers'], status_code=response.status_code,
                          seconds=time.monotonic()-start)
            if response.status_code == 201:
                evidence = response.json()
                text = '\n'.join(line['text'] for line in evidence['lines'])
                recovered = any(normalize(a) in normalize(text) for a in record['answers'])
                words = set(re.findall(r'\w+', unicodedata.normalize('NFKC',text).casefold()))
                coverage = max(sum(w in words for w in target)/len(target)
                    for a in record['answers'] if (target:=re.findall(r'\w+',unicodedata.normalize('NFKC',a).casefold())))
                result.update(answer_text_recovered=recovered, answer_token_coverage=coverage,
                    analysis_seconds=evidence['analysis_seconds'], ocr_text=text, evidence=evidence)
            else:
                result.update(answer_text_recovered=False, answer_token_coverage=0, error=response.text)
            rows.append(result)
            print(json.dumps({k:v for k,v in result.items() if k not in {'ocr_text','evidence'}},ensure_ascii=False),flush=True)
    seconds = [r['seconds'] for r in rows]
    report = dict(scope=dataset['scope'], scoring_version='nfkc-alphanumeric-currency-v2',
        official_benchmark_score=False, llm_calls=0, gpu_required=False,
        cases=len(rows), api_success=sum(r['status_code']==201 for r in rows),
        answer_text_recovered=sum(r['answer_text_recovered'] for r in rows),
        answer_text_recovery_rate=sum(r['answer_text_recovered'] for r in rows)/len(rows),
        mean_answer_token_coverage=statistics.mean(r['answer_token_coverage'] for r in rows),
        no_automatic_ticket_created=db.one('SELECT count(*) n FROM service_tickets')['n']==0,
        api_seconds_p50=statistics.median(seconds),
        api_seconds_p95=statistics.quantiles(seconds,n=100,method='inclusive')[94],records=rows)
    write(output/'report.json', report)
    print(json.dumps({k:v for k,v in report.items() if k!='records'},ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-root',type=Path,default=ROOT/'datasets/incident_public_ocrbench8_20261114/selected8')
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    for path in [args.dataset_root,args.output]:
        if path and not path.resolve().is_relative_to(ROOT):
            parser.error('Runtime data must be under /data/zjr/enterprise_kb_agent')
    manifest=prepare(args.dataset_root) if args.prepare else args.dataset_root/'manifest.json'
    if args.output:
        evaluate(manifest,args.output)
    elif not args.prepare:
        parser.error('Choose --prepare and/or --output')


if __name__=='__main__':
    main()
