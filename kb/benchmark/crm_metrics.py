"""Fixed-denominator reporting for repeated, paired CRM trials."""
from __future__ import annotations

import math
import statistics
from itertools import combinations


def local_verdict(official, error):
    """Keep upstream grading intact; count explicit harness limits as failures."""
    if official is not None:
        return official
    known_limits = ('达到12次模型调用上限', '达到工具调用上限', '上下文预算不足',
                    'Specialist did not return a valid ExternalResult', 'V6 execution contract failed:',
                    'Supervisor model-call budget exhausted', 'Supervisor tool-call budget exhausted',
                    'Supervisor context budget exhausted', 'Reference model-call budget exhausted',
                    'Reference tool-call budget exhausted', 'Reference context budget exhausted',
                    'Reference malformed model response:')
    if error and (any(message in error for message in known_limits)
                  or error.startswith(('GraphRecursionError:', 'RoutingPlanError:', 'ConversationStalled:'))):
        return False
    return None


def verdict(row):
    return row.get('local_verdict', row['official_verdict'])


def percentile(values, quantile):
    if not values:
        return None
    items = sorted(values)
    index = (len(items)-1)*quantile
    lo, hi = math.floor(index), math.ceil(index)
    return items[lo] + (items[hi]-items[lo])*(index-lo)


def error_category(trace):
    error = trace.get('error') or ''
    for category, marker in (
        ('simulator_fidelity', 'SimulatorFidelityError:'),
        ('conversation_stalled', 'ConversationStalled:'),
        ('router_validation', 'RoutingPlanError:'),
        ('receipt_reference', 'successful tool receipts'),
        ('mixed_final_tools', 'simultaneous business tools'),
        ('model_call_budget', '达到12次模型调用上限'),
        ('tool_call_budget', '达到工具调用上限'),
        ('context_budget', '上下文预算不足'),
        ('model_call_budget', 'Supervisor model-call budget exhausted'),
        ('tool_call_budget', 'Supervisor tool-call budget exhausted'),
        ('context_budget', 'Supervisor context budget exhausted'),
    ):
        if marker in error:
            return category
    return trace.get('error_kind') or 'unclassified'


def opening_message(row):
    return next((turn['content'] for turn in row['trace'].get('turns', []) if turn['role'] == 'user'), None)


def summarize(rows, task_ids, trials=3, arms=('single', 'multi_v5')):
    expected_keys = {(arm, task, trial) for arm in arms for task in task_ids for trial in range(trials)}
    indexed = {}
    for row in rows:
        key = (row['arm'], row['task_id'], row['trial'])
        if key not in expected_keys or key in indexed:
            raise ValueError(f'Unexpected or duplicate trial: {key}')
        indexed[key] = row
    result = {'expected_trials': len(expected_keys), 'completed_trials': len(indexed),
              'complete': len(indexed) == len(expected_keys), 'trials': trials, 'arms': {},
              'execution_versions': sorted({r['execution_version'] for r in indexed.values() if r.get('execution_version')})}
    for arm in arms:
        selected = [r for key, r in indexed.items() if key[0] == arm]
        passed = sum(verdict(r) is True for r in selected)
        unknown = sum(verdict(r) is None for r in selected)
        official_unknown = sum(r['official_verdict'] is None for r in selected)
        official_scored = len(selected)-official_unknown
        scored = len(selected)-unknown
        reliable = []
        reliability_complete = 0
        for task in task_ids:
            group = [indexed.get((arm, task, t)) for t in range(trials)]
            if all(row is not None for row in group):
                reliability_complete += 1
                reliable.append(all(verdict(r) is True for r in group))
        tokens = sum(r['agent_usage']['total_tokens'] for r in selected)
        missing_usage = sum(r['agent_usage']['usage_missing_calls'] for r in selected)
        latencies = [t['seconds'] for r in selected for t in r['agent_turn_times']]
        totals = [sum(t['seconds'] for t in r['agent_turn_times']) for r in selected]
        errors = {}
        for r in selected:
            if r['trace'].get('error'):
                k = error_category(r['trace'])
                errors[k] = errors.get(k, 0)+1
        result['arms'][arm] = {
            'expected': len(task_ids)*trials, 'completed': len(selected), 'passed': passed,
            'scored': scored, 'unknown': unknown,
            'official_scored': official_scored, 'official_unknown': official_unknown,
            'known_harness_failures': sum(verdict(r) is False and r['official_verdict'] is None for r in selected),
            'official_success_over_scored': passed/official_scored if official_scored else None,
            # A conservative system-level result: no unknown/infrastructure
            # result is silently counted as a success or dropped.
            'strict_success_lower_bound_over_completed': passed/len(selected) if selected else None,
            'all_trials_success': sum(reliable), 'all_three_success': sum(reliable) if trials == 3 else None,
            'reliability_tasks_completed': reliability_complete,
            'reliability_tasks_expected': len(task_ids),
            'all_trials_success_rate': sum(reliable)/len(reliable) if reliable else None,
            'prompt_tokens': sum(r['agent_usage']['prompt_tokens'] for r in selected),
            'completion_tokens': sum(r['agent_usage']['completion_tokens'] for r in selected),
            'total_tokens': tokens, 'usage_missing_calls': missing_usage,
            'tokens_per_task': tokens/len(selected) if selected and not missing_usage else None,
            'tokens_per_success': tokens/passed if passed and not missing_usage else None,
            'model_calls': sum(r['agent_usage']['num_calls'] for r in selected),
            'tool_calls': sum(len(r['trace']['tool_calls_detail']) for r in selected),
            'turn_seconds_p50': percentile(latencies, .5), 'turn_seconds_p95': percentile(latencies, .95),
            'task_active_seconds_p50': percentile(totals, .5),
            'task_active_seconds_p95': percentile(totals, .95),
            'agent_cost_usd': None, 'pricing': 'self-hosted; no metered monetary charge recorded',
            'normal_stops': sum(r['trace'].get('stopped') is True for r in selected),
            'max_turns_reached': sum(r['trace'].get('stopped') is False and not r['trace'].get('timed_out')
                                     and not r['trace'].get('error') for r in selected),
            'conversation_stalls': sum(error_category(r['trace']) == 'conversation_stalled' for r in selected),
            'simulator_failures': sum(error_category(r['trace']) == 'simulator_fidelity' for r in selected),
            'committed_write_calls': sum(c.get('business_outcome') == 'committed' for r in selected for c in r['trace']['tool_calls_detail']),
            'reused_write_attempts': sum(c.get('business_outcome') == 'reused' for r in selected for c in r['trace']['tool_calls_detail']),
            'business_write_rejections': sum(c.get('business_outcome') == 'rejected' for r in selected for c in r['trace']['tool_calls_detail']),
            'errors': errors,
        }
    paired = {'multi_only': 0, 'single_only': 0, 'both_pass': 0, 'both_fail': 0, 'unknown': 0}
    openings = {'same': 0, 'different': 0, 'missing': 0}
    for task in task_ids if len(arms) == 2 else []:
        for trial in range(trials):
            a, b = (indexed.get((arm, task, trial)) for arm in arms)
            if a is None or b is None:
                continue
            first_a, first_b = opening_message(a), opening_message(b)
            if first_a is None or first_b is None:
                openings['missing'] += 1
            else:
                openings['same' if first_a == first_b else 'different'] += 1
            x, y = verdict(a), verdict(b)
            if x is None or y is None:
                paired['unknown'] += 1
            else:
                paired['both_pass' if x and y else 'both_fail' if not x and not y
                       else 'single_only' if x else 'multi_only'] += 1
    result['paired'] = paired
    result['paired_available'] = len(arms) == 2
    result['paired_openings'] = openings
    result['pairwise'] = []
    for left, right in combinations(arms, 2):
        pair = {'left': left, 'right': right, 'completed': 0, 'left_only': 0, 'right_only': 0,
                'both_pass': 0, 'both_fail': 0, 'unknown': 0,
                'openings': {'same': 0, 'different': 0, 'missing': 0}}
        for task in task_ids:
            for trial in range(trials):
                a, b = (indexed.get((arm, task, trial)) for arm in (left, right))
                if a is None or b is None:
                    continue
                pair['completed'] += 1
                first_a, first_b = opening_message(a), opening_message(b)
                pair['openings']['missing' if first_a is None or first_b is None else
                                 'same' if first_a == first_b else 'different'] += 1
                x, y = verdict(a), verdict(b)
                pair['unknown' if x is None or y is None else 'both_pass' if x and y else
                     'both_fail' if not x and not y else 'left_only' if x else 'right_only'] += 1
        result['pairwise'].append(pair)
    if len(arms) == 2 and 'official_graph' in arms:
        pair = result['pairwise'][0]
        result['paired'] = {f'{pair["left"]}_only': pair['left_only'],
                            f'{pair["right"]}_only': pair['right_only'],
                            **{k: pair[k] for k in ('both_pass','both_fail','unknown')}}
    return result


def report_markdown(summary):
    def number(value, digits=2):
        return '—' if value is None else f'{value:.{digits}f}'
    version = ', '.join(summary.get('execution_versions', [])) or (
        'business_v6_external_v1' if 'multi_v6' in summary['arms'] else 'business_v5_external_v1')
    evaluation = summary.get('evaluation', {})
    model_scope = ('官方ITSM任务与检查器保持原样；单卡开发诊断中回答、用户模拟和评分共用'
                   'Qwen3-8B权重，上下文及用量独立记录，结果按非独立评测呈现。'
                   if evaluation.get('shared_evaluation_model') else
                   '官方ITSM任务与检查器保持原样；用户模拟器和裁判使用本地Qwen3-VL-8B，这是公开任务上的本地协议对照。')
    lines = ['# CRM ITSM Harness 对照', '',
             f"已收集 {summary['completed_trials']}/{summary['expected_trials']} 条任务运行记录；记录齐全：{summary['complete']}。", '',
             f"|方案|通过/运行记录|评测异常/未判定|{summary.get('trials', 3)}次全部成功/已完成任务组|单轮p50/p95秒|Token/任务|Token/成功任务|模型/工具调用|",
             '|---|---|---|---|---|---|---|---|']
    for arm, a in summary['arms'].items():
        lines.append(f"|{arm}|{a['passed']}/{a['completed']}|{a['unknown']}|"
                     f"{a.get('all_trials_success', a.get('all_three_success'))}/{a['reliability_tasks_completed']}|"
                     f"{number(a['turn_seconds_p50'])}/{number(a['turn_seconds_p95'])}|"
                     f"{number(a['tokens_per_task'])}|{number(a['tokens_per_success'])}|"
                     f"{a['model_calls']}/{a['tool_calls']}|")
    lines += ['', '|方案|正常结束|达到最大轮数|无进展终止|模拟用户异常|已提交写入|复用写入回执|业务拒绝|',
              '|---|---:|---:|---:|---:|---:|---:|---:|']
    for arm, a in summary['arms'].items():
        lines.append(f"|{arm}|{a['normal_stops']}|{a['max_turns_reached']}|{a['conversation_stalls']}|"
                     f"{a.get('simulator_failures', 0)}|{a['committed_write_calls']}|{a['reused_write_attempts']}|{a['business_write_rejections']}|")
    lines += ['', '## 口径',
              '- '+model_scope,
              '- 业务适配版本 '+version+'。调用预算按运行协议记录，领域规则与工具来自公开ITSM。',
              '- 官方成功率排除评分未知；另报固定完成分母的成功率下界及错误分类，失败和未知不会作为成功。未完成项单列覆盖率。',
              '- 明确耗尽模型/工具/上下文预算或未形成有效结束结果，在本地系统指标中记任务失败；原始官方判定另存，不将这些失败作为基础设施故障停止整批评测。',
              f"- {summary.get('trials', 3)}次全部成功要求该任务各次独立运行全部通过；各次从全新环境开始。",
              '- 耗时仅计 Agent 每轮实际墙钟时间，包含路由、Token 预算、工具和检查点，不含模型加载、用户模拟和裁判。',
              '- Token 累加所有协调/专业 Agent/格式修复调用及失败任务；若存在 usage 缺失，派生 Token 效率留空。',
              '- 本地模型未产生可计量 API 账单，金额留空；模型、模拟器、裁判用量分别存档。',
              '- 续跑仅跳过完整落盘的任务；不会自动重启失败的模型服务或队列。',
              '', '## 配对任务', str(summary['paired']) if summary.get('paired_available', True) else
              '多方案逐对结果如下。' if summary.get('pairwise') else '本次仅运行一个方案，无配对对照。', '']
    if len(summary['arms']) > 2 or 'official_graph' in summary['arms']:
        lines += ['|左方案|右方案|配对数|仅左成功|仅右成功|共同成功|共同失败|未知|',
                  '|---|---|---:|---:|---:|---:|---:|---:|']
        for pair in summary.get('pairwise', []):
            lines.append('|'+ '|'.join(str(pair[k]) for k in ('left','right','completed','left_only',
                         'right_only','both_pass','both_fail','unknown'))+'|')
    lines += ['配对开场核对 '+str(summary.get('paired_openings', {})), '',
              '写入分项取自新适配器的业务结果标记。历史记录缺少此标记时计数表示已记录数量。',
              '无进展终止保留原始判定，在本地任务通过率中计失败。正常结束与业务正确分别统计。', '']
    lines += ['模拟用户忠实性校验失败单列为评测异常，保留全部完成分母，不计成功；正常用户交互下的执行停滞仍计 Agent 失败。', '']
    return '\n'.join(lines)
