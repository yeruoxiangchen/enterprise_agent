"""Read-only audit of the frozen multi-agent v1 run; no models or new grading."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics


ARMS = ['fixed-rerank', 'single-agent', 'multi-no-skills', 'multi-skills']
DEFAULT = Path('/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_multi_agent_20261029')
CONTRACT_ERROR = 'Clarification/insufficiency needs message and no factual claims'


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def score_counts(rows):
    values = [row['metrics'].get('semantic_correctness') for row in rows]
    return dict(correct=values.count(1), incorrect=values.count(0), unknown=values.count(None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=DEFAULT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.input.resolve()
    output = (args.output or root / 'analysis_20261030').resolve()
    if output == root or root in output.parents and output.name in ARMS:
        raise ValueError('Use a separate analysis directory')
    summary = {'input': str(root), 'created_at': datetime.now(timezone.utc).isoformat(),
               'method': 'Saved predictions/checkpoints/grades only; no new inference or regrading',
               'human_reviewed': False, 'test_used': False, 'arms': {}}
    details = []
    expected_ids = None
    for arm in ARMS:
        predictions = read_rows(root / arm / 'predictions.jsonl')
        grades = read_rows(root / 'semantic' / arm / 'scored_predictions.jsonl')
        ids = [r['id'] for r in predictions]
        if len(ids) != 34 or len(set(ids)) != len(ids):
            raise ValueError(f'Unexpected or duplicate IDs: {arm}')
        if expected_ids is None:
            expected_ids = set(ids)
        if set(ids) != expected_ids or len(grades) != len(ids) or {g['id'] for g in grades} != set(ids):
            raise ValueError(f'Unpaired predictions/grades: {arm}')
        grade_by_id = {g['id']: g for g in grades}
        plans, single_plans = Counter(), Counter()
        validation_events = Counter()
        exceptions = Counter()
        success_rows = [r for r in grades if r['execution_status'] == 'complete']
        categories = {}
        for cat in sorted({r['category'] for r in grades}):
            rows = [r for r in grades if r['category'] == cat]
            categories[cat] = {'count': len(rows), 'completed': sum(r['execution_status'] == 'complete' for r in rows),
                               'semantic': score_counts(rows)}
        for row in predictions:
            path = root / arm / 'checkpoints' / (row['id'] + '.json')
            state = json.loads(path.read_text()) if path.exists() else {}
            trace = row.get('trace', [])
            errors = [t['error'] for t in trace if t['kind'] == 'agent_validation']
            validation_events.update(errors)
            steps = [t for t in trace if t['kind'] == 'agent_step']
            last_role = steps[-1]['role'] if steps else None
            role_steps = sum(t['role'] == last_role for t in steps)
            final = state.get('final', {})
            if state:
                plans[len(state.get('plan', []))] += 1
                if row['category'] == 'single_evidence':
                    single_plans[len(state.get('plan', []))] += 1
            failure = row['execution_status'] != 'complete'
            if failure:
                exceptions[row.get('error')] += 1
            step_limit = 2 if last_role == 'synthesizer' else 3
            exhausted = bool(failure and row.get('error') == 'RuntimeError'
                             and CONTRACT_ERROR in errors and role_steps == step_limit
                             and not (state.get('final') if last_role == 'synthesizer'
                                      else last_role in state.get('results', {})))
            grade = grade_by_id[row['id']]
            details.append({'arm': arm, 'id': row['id'], 'category': row['category'],
                'status': row['status'], 'execution_status': row['execution_status'],
                'error': row.get('error'), 'calls': row['model_calls'],
                'semantic_correctness': grade['metrics'].get('semantic_correctness'),
                'action_correct': row['metrics']['action_correct'],
                'validation_errors': dict(Counter(errors)),
                'failure_after_contract_rejections_and_step_limit': exhausted,
                'last_model_role': last_role, 'last_role_steps': role_steps,
                'last_event': trace[-1]['kind'] if trace else None,
                'plan': state.get('plan'),
                'answer_has_question_mark': row['status'] == 'answered' and any(x in (row['answer'] or '') for x in ['?', '？']),
                'final_claims': final.get('claims'), 'final_message': final.get('message'),
                'checkpoint': str(path) if path.exists() else None})
        attempts = read_rows(root / 'semantic' / arm / 'attempts.jsonl')
        error_attempts = [a for a in attempts if a.get('error')]
        arm_details = [r for r in details if r['arm'] == arm]
        summary['arms'][arm] = {'processed': len(predictions), 'completed': len(success_rows),
            'action_correct': sum(r['metrics']['action_correct'] for r in predictions),
            'model_calls': sum(r['model_calls'] for r in predictions),
            'prompt_tokens': sum(r['prompt_tokens'] for r in predictions),
            'completion_tokens': sum(r['completion_tokens'] for r in predictions),
            'median_seconds': statistics.median(r['latency_seconds'] for r in predictions),
            'mean_evidence': statistics.mean(len(r['hits']) for r in predictions),
            'semantic': score_counts(grades), 'semantic_on_completed': score_counts(success_rows),
            'exceptions': dict(exceptions), 'validation_events': dict(validation_events),
            'failure_after_contract_rejections_and_step_limit': sum(r['failure_after_contract_rejections_and_step_limit'] for r in arm_details),
            'failed_cases_with_contract_rejection': sum(r['execution_status'] != 'complete' and CONTRACT_ERROR in r['validation_errors'] for r in arm_details),
            'plan_sizes': dict(plans), 'single_evidence_plan_sizes': dict(single_plans),
            'answered_with_question_mark_ids': [r['id'] for r in arm_details if r['answer_has_question_mark']],
            'by_category': categories,
            'grading_attempt_errors': dict(Counter(a['error'] for a in error_attempts)),
            'missing_grade_ids': [r['id'] for r in grades if r['metrics'].get('semantic_correctness') is None]}
    output.mkdir(parents=True, exist_ok=True)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    (output / 'cases.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in details))
    lines = ['# 多 Agent v1 离线结果审计', '', f'输入：`{root}`', '',
             '仅分析冻结记录；没有重新推理、重评分或修改旧结果。完成率和自动语义分数分开报告。', '',
             '|方案|处理|正常完成|动作匹配|模型调用|p50秒|判对/错/未知|',
             '|---|---:|---:|---:|---:|---:|---|']
    for arm, s in summary['arms'].items():
        counts = '/'.join(str(s['semantic'][k]) for k in ['correct', 'incorrect', 'unknown'])
        lines.append(f"|{arm}|{s['processed']}|{s['completed']}|{s['action_correct']}|{s['model_calls']}|{s['median_seconds']:.2f}|{counts}|")
    lines.extend(['', '## 执行错误', '', '|方案|异常类型|发生过拒答字段冲突的失败记录|冲突后阶段步数耗尽|', '|---|---|---:|---:|'])
    for arm, s in summary['arms'].items():
        lines.append(f"|{arm}|{json.dumps(s['exceptions'])}|{s['failed_cases_with_contract_rejection']}|{s['failure_after_contract_rejections_and_step_limit']}|")
    lines.extend(['', '其余 ToolInvocationError 的最后事件均需结合 cases.jsonl 与原 checkpoint 排查；日志未保存完整异常原因链，不能仅凭类型断定根因。',
                  '问号检测仅用于筛查问题复述，不是语义评分器。细节在 summary.json 和 cases.jsonl。',
                  '本轮是34题开发诊断，不是独立测试；不同组的生成次数和证据数上限不同，不是等算力对照。'])
    (output / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'output': str(output), 'cases': len(details),
                      'completed': {a: s['completed'] for a, s in summary['arms'].items()},
                      'step_limit_failures': sum(s['failure_after_contract_rejections_and_step_limit'] for s in summary['arms'].values())}, ensure_ascii=False))


if __name__ == '__main__':
    main()
