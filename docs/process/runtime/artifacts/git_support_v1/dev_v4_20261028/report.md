# 知识库 v4 开发评测与语义复核

语义指标由 Qwen3-VL-8B 评审；回答为 Qwen3-8B。权重不同但同属 Qwen 系列，不是独立人工真值。
全部失败保留；Judge失败是缺失评分，不按回答错误计算。带缺失评分的均值不参与准入。

| 版本/策略 | 完成记录 | 语义正确/总题数 | 评分缺失 | 忠实性通过 | 调用次数 | p50秒 |
|---|---:|---:|---:|---:|---:|---:|
| v3_fixed-bm25 | 100 | 84/100 | 0 | 83/100 | 100 | 5.27 |
| v3_fixed-rerank | 100 | 88/100 | 0 | 86/100 | 100 | 16.40 |
| v3_agent-bm25 | 100 | 81/100 | 1 | 80/100 | 488 | 14.88 |
| v3_agent-native-bm25 | 100 | 81/100 | 0 | 81/100 | 419 | 12.89 |
| v3_agent-evidence-bm25 | 100 | 83/100 | 0 | 82/100 | 101 | 5.72 |
| v3_agent-evidence-rerank | 100 | 88/100 | 0 | 85/100 | 100 | 17.01 |
| v4_fixed-bm25 | 100 | 84/100 | 0 | 84/100 | 100 | 5.05 |
| v4_agent-guarded-bm25 | 100 | 84/100 | 0 | 91/100 | 199 | 11.84 |
| v4_agent-guarded-rerank | 100 | 77/100 | 0 | 81/100 | 202 | 24.46 |

配对比较按题族 bootstrap，见 comparisons.json；25个题族的100种问法不是100个独立任务。
旧 v3 与新 v4 的差异包含策略和提示词共同变化；新基线与候选同一代码快照。

## 补查诊断

{
  "natural": {
    "without": 8,
    "with": 8,
    "total": 8,
    "regressions": 0,
    "judge_complete": true,
    "actual_withholding_cases": 0,
    "followup_requests": 0
  },
  "withheld_first": {
    "without": 0,
    "with": 8,
    "total": 8,
    "regressions": 0,
    "judge_complete": true,
    "actual_withholding_cases": 8,
    "followup_requests": 8
  }
}

人工移除第一轮中的目标证据仅用于诊断，不是业务准确率；隐藏ID不传给模型。

## 预注册准入结论

{
  "agent-guarded-bm25": {
    "eligible": false,
    "reasons": [
      "semantic_gain_under_3_questions",
      "completion_under_99",
      "critical_semantic_regression"
    ],
    "semantic_correct": 84,
    "baseline_correct": 84
  },
  "agent-guarded-rerank": {
    "eligible": false,
    "reasons": [
      "semantic_gain_under_3_questions",
      "completion_under_99",
      "critical_semantic_regression",
      "faithfulness_regression",
      "semantic_citation_support_regression"
    ],
    "semantic_correct": 77,
    "baseline_correct": 84
  }
}

选中候选：None
无合格候选时本轮到开发报告结束，冻结测试集不执行。合格才冻结参数并执行300题基线/候选对照。
隔离浏览器报告：browser/report.json。未切换现有线上Web；独立人工复核仍待完成。
完整逐题复核包：human_review.jsonl；不将模型评审冒充人工复核。
