"""Auditable corrections to saved development records; no generation or relabelled originals."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import business_ops as old
from business_assessment_metrics import summarize
from business_assessment_observe import observation


def apply_confirmed_failures(rows, grades):
    by_key = {r['key']: r for r in rows}
    adjusted = {g['key']: deepcopy(g) for g in grades}
    changes = []

    def fail(key, axes, reason, established, evidence):
        if not established:
            raise ValueError('Saved records no longer match the reviewed failure: '+key)
        for axis in axes:
            previous = adjusted[key].get(axis)
            changes.append({'key': key, 'axis': axis, 'original_score': previous, 'reviewed_score': 0,
                            'reason': reason, 'evidence': evidence,
                            'reviewer': 'assistant inspection plus explicit saved-artifact checks'})
            adjusted[key][axis] = 0

    for key, requirement in [
            ('multi_v4/cancel-pending/1', '回答设备借用最多14天'),
            ('multi_v4/consult-then-draft/0', '回答资产编号、故障现象和可联系时间')]:
        row = by_key[key]
        fail(key, ('correctness',), '只返回通用澄清，未回答原rubric必答点：'+requirement,
             row['answer'] == '请补充具体问题、对象或希望办理的事项。',
             {'answer': row['answer'], 'original_rubric': row['rubric']})

    row = by_key['multi_v4/consult-then-draft/1']
    descriptions = '\n'.join(t['description'] for t in row['actual_tickets'])
    fail(row['key'], ('correctness', 'context_retention'),
         '实际草稿未包含用户本轮明确提供的资产编号DEMO-172与联系时间今天下午，原评分补出了不存在的内容。',
         bool(row['actual_tickets']) and 'DEMO-172' not in descriptions and '今天下午' not in descriptions,
         {'actual_draft_descriptions': descriptions, 'question': row['question'], 'original_rubric': row['rubric']})

    for key in ('single/three-domain/0', 'single/consult-then-draft/1', 'single/account-isolation/1'):
        row = by_key[key]
        fail(key, ('correctness',), '明确要求实际办理，但原数据库/工具动作检查失败；文字描述或要求再次确认不构成办理成功。',
             row['business_state_ok'] is False,
             {'answer': row['answer'], 'actual_tickets': row['actual_tickets'], 'tool_actions': row['tool_actions']})
    row = by_key['single/consult-then-draft/1']
    fail(row['key'], ('context_retention',), '未生成要求的草稿，回答仅复述本轮请求并再次索要已有信息；没有保留故障背景的实际目标工单。',
         not row['actual_tickets'] and 'Permission denied' not in row['answer'] and '已检查网络' not in row['answer'],
         {'answer': row['answer'], 'context_rubric': row['context_rubric'], 'actual_tickets': row['actual_tickets']})
    return list(adjusted.values()), changes


def finalized_load(source, db):
    result = deepcopy(source)
    from business_assessment_metrics import quantiles
    for batch in result['batches']:
        for row in batch['requests']:
            final = observation(db, row['run_id'])
            row['queue_seconds'] = final['queue_seconds']
            row['execution_seconds'] = final['execution_seconds']
        batch['queue'] = quantiles(r['queue_seconds'] for r in batch['requests'] if r['queue_seconds'] is not None)
        batch['execution'] = quantiles(r['execution_seconds'] for r in batch['requests'] if r['execution_seconds'] is not None)
    result['timing_note'] = 'Execution timestamps read after all requests finished; wrapper return includes post-answer event logging.'
    return result


def audit(source):
    from kb.db import Database
    target = source/'audit_v1'
    target.mkdir(exist_ok=True)
    paths = [source/'protocol.json', source/'summary.json', source/'faults/summary.json', source/'load/summary.json']
    paths += sorted((source/'rows').glob('*.json'))+sorted((source/'grades').glob('*.json'))
    hashes = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    protocol = json.loads((source/'protocol.json').read_text())
    rows = [json.loads(p.read_text()) for p in sorted((source/'rows').glob('*.json'))]
    grades = [json.loads(p.read_text()) for p in sorted((source/'grades').glob('*.json'))]
    revised, changes = apply_confirmed_failures(rows, grades)
    result = summarize(protocol['cases'], protocol['arms'], rows, revised)
    result.update(source=str(source), original_artifact_sha256=hashes,
                  reviewer='assistant visual/text inspection with DB-backed action checks; not independent human labels',
                  original_model_summary=json.loads((source/'summary.json').read_text())['arms'],
                  confirmed_corrections=changes,
                  faults=json.loads((source/'faults/summary.json').read_text()),
                  load=finalized_load(json.loads((source/'load/summary.json').read_text()), Database(source/'load/app/knowledge.sqlite3')))
    old.write(target/'comparison.json', result)
    lines = ['# 已完成评测的复查', '',
             '原回答、原模型评分、原summary/report均保留。本报告核对实际回答与数据库记录，对已确认的误判单独修正。',
             '复查人为本次助手；其他条目保留模型评分，不属于独立人工准确率或新一轮推理结果。', '',
             '|版本|完整会话通过|上下文通过|总模型调用|每成功会话调用|P50秒|P95秒|',
             '|---|---|---|---|---|---|---|']
    for arm, s in result['arms'].items():
        task, ctx = s['conversation_task_success'], s['context_retention_model_review']
        lines.append(f'|{arm}|{task["passed"]}/{task["expected"]}|{ctx["passed"]}/{ctx["expected"]}|'
                     f'{s["model_calls_all_attempts"]}|{s["calls_per_successful_conversation"]:.3f}|'
                     f'{s["response_latency"]["p50_seconds"]:.2f}|{s["response_latency"]["p95_seconds"]:.2f}|')
    lines += ['', '## 已确认的评分误判']
    for c in changes:
        lines.append(f'- {c["key"]} / {c["axis"]}: {c["original_score"]} → {c["reviewed_score"]}；{c["reason"]}')
    lines += ['', '## 运行级结果',
              '- 故障实验7/8通过。领域间中断恢复触发KeyError，未完成；草稿写入后中断恢复成功且仅有一张草稿。',
              '- 16次并发建单产生1张草稿，16次重复确认产生1条状态事件。',
              '- 7个真实HTTP问答均正常返回；排队和执行时长从完整事件回填，无须重新生成。',
              '', '## 待修复的产品行为',
              '- 否定创建意图时，仍需处理同句中的明确制度咨询；当前部分请求被整体当成澄清。',
              '- 创建请求中的资产编号与联系时间未写入草稿。',
              '- 用户更正环境后，草稿正文保留了Ubuntu更正，但标题及回执仍突出旧Windows环境。',
              '- 领域任务中断后恢复到了缺少最终answer的图状态，执行器抛出KeyError。']
    (target/'report.md').write_text('\n'.join(lines)+'\n')
    print('Saved audit: '+str(target/'report.md'), flush=True)
    return result
