#!/usr/bin/env python
"""Reviewable incremental procedural memory; no model or GPU is required."""
import argparse
import json
from pathlib import Path

from kb.experience import ExperienceMemory, SEED, merge_deltas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['init', 'query', 'update', 'feedback'])
    parser.add_argument('--memory', type=Path, required=True)
    parser.add_argument('--query', default='')
    parser.add_argument('--delta', type=Path)
    parser.add_argument('--events', type=Path)
    parser.add_argument('--reviewed-ids', nargs='*', default=[])
    args = parser.parse_args()
    path = args.memory.resolve()
    if not path.is_relative_to('/data/zjr/enterprise_kb_agent'):
        parser.error('Memory artifacts must be stored under /data/zjr/enterprise_kb_agent')
    if args.action == 'init':
        if path.exists(): parser.error('Existing memory is preserved; use update')
        entries = SEED
    else:
        entries = ExperienceMemory(path).entries
    if args.action == 'query':
        print(json.dumps(ExperienceMemory(path).retrieve(args.query), ensure_ascii=False, indent=2))
        return
    if args.action == 'update':
        if not args.delta: parser.error('update requires --delta')
        deltas = json.loads(args.delta.read_text())
        entries = merge_deltas(entries, deltas, args.reviewed_ids)
    if args.action == 'feedback':
        if not args.events: parser.error('feedback requires --events')
        value = json.loads(path.read_text())
        processed = set(value.get('processed_feedback', []))
        events = args.events.read_text().splitlines()
        by_id = {entry['id']: entry for entry in entries}
        for number, line in enumerate(events):
            key = str(args.events.resolve()) + ':' + str(number)
            event = json.loads(line)
            if key in processed or event.get('kind') != 'strategy_outcome': continue
            data = event.get('data', event.get('payload', {})); progress = data.get('progress', {})
            positive = (data.get('status') == 'answered' and not progress.get('remaining_actions')
                        and not progress.get('remaining_targets') and not progress.get('unknown_write_result'))
            negative = data.get('status') == 'insufficient'
            for ident in data.get('experience_ids', []):
                if ident in by_id and (positive or negative):
                    field = 'helpful' if positive else 'harmful'
                    by_id[ident][field] = by_id[ident].get(field, 0) + 1
            processed.add(key)
        value.update(entries=entries, processed_feedback=sorted(processed))
    else:
        value = json.loads(path.read_text()) if path.exists() else {'version': 1}
        value['entries'] = entries
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp'); temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)
    print(json.dumps({'memory': str(path), 'entries': len(entries),
        'approved': sum(e['status'] == 'approved' for e in entries)}, ensure_ascii=False))


if __name__ == '__main__': main()
