# v4.2 pilot 对照分析

三组使用相同BM25候选与CPU重排、相同模型/上下文/输出预算/120秒超时。没有训练，未使用冻结测试集。
固定链仅改引用元数据生成；旧Agent保留v4.1策略；新Agent使用问题原文绑定及知识缺口补查。
34题pilot由旧错误、分歧、20条关键题与3条稳定对照组成，不能当作业务总体准确率。
统一v4.2模型评分，要求摘录候选答案原文，并拆开内容正确性与引用支持；并非独立人工真值。

| 策略 | 正常完成 | 语义正确 | 评分缺失 | 误拒答 | 关键题正确 | 模型调用 | p50秒 |
|---|---:|---:|---:|---:|---:|---:|---:|
| fixed42-rerank | 34/34 | 24/34 | 7 | 0 | 15/20 | 34 | 16.13 |
| agent-guarded41-rerank | 34/34 | 23/34 | 2 | 4 | 17/20 | 58 | 34.77 |
| agent-guarded42-rerank | 29/34 | 20/34 | 3 | 1 | 13/20 | 79 | 47.99 |

retrieval-bm25 CPU检索诊断（只在有标准证据的题上计算）：
```json
{
  "all_evidence_at_5": {
    "mean": 0.4791666666666667,
    "ci95_family_bootstrap": [
      0.1875,
      0.7916666666666666
    ],
    "families": 8
  },
  "evidence_recall_at_5": {
    "mean": 0.5416666666666666,
    "ci95_family_bootstrap": [
      0.25,
      0.8125
    ],
    "families": 8
  }
}
```

retrieval-rerank CPU检索诊断（只在有标准证据的题上计算）：
```json
{
  "all_evidence_at_5": {
    "mean": 0.7083333333333334,
    "ci95_family_bootstrap": [
      0.4583333333333333,
      0.9166666666666666
    ],
    "families": 8
  },
  "evidence_recall_at_5": {
    "mean": 0.7708333333333334,
    "ci95_family_bootstrap": [
      0.6041666666666666,
      0.9375
    ],
    "families": 8
  }
}
```

## 后续100题开发对照准入

```json
{
  "eligible": false,
  "reasons": [
    "incomplete_pilot_or_judge",
    "candidate_execution_errors",
    "correctness_below_control",
    "critical_cases_not_all_correct"
  ],
  "test_released": false,
  "human_reviewed": false,
  "gate": {
    "calibration": "30 controls >=28 correct, all 15 negatives rejected, both citation/answer separation controls passed, zero missing grades",
    "pilot": "34 preselected dev diagnostics; candidate zero execution errors, all 20 critical correct, zero unauthorized/forbidden output",
    "quality": "candidate correct >= each control; false refusals strictly fewer than old Agent with the same reranker",
    "browser": "isolated new policy web E2E passed",
    "release": "only if pilot passes, run three matched arms on all100 dev; otherwise stop with diagnostic report",
    "test": "no test or training in this queue; no production-web switch",
    "limitations": "diagnostic subset intentionally enriched for failures; model grades and small differences require review"
  },
  "correct": {
    "fixed42-rerank": 24,
    "agent-guarded41-rerank": 23,
    "agent-guarded42-rerank": 20
  },
  "false_refusals": {
    "fixed42-rerank": 0,
    "agent-guarded41-rerank": 4,
    "agent-guarded42-rerank": 1
  }
}
```

评分覆盖完整：False。缺失评分保留为缺失，不能按零分解释或静默删除。
逐题差异与题族bootstrap见对应comparisons.json；小样本差异不宣称统计显著。
来源完整、引用格式合法不等于语义正确。题目原文绑定只验证要求有文本锚点，不证明模型理解无误。
旧结果全部保留。audit/citation_replay.jsonl是两条原始格式错误的确定性修复回放；audit_semantic是旧争议答案的新版本复评。
未经人工裁决的模型分数不回填旧报告，完整测试仍需另行冻结协议。
