# 企业知识库 v4.1 三组开发对照

本轮回答模型 Qwen3-8B，评审模型 Qwen3-VL-8B；均复用现有服务，没有训练。
以下为统一 v4.1 口径的模型评分，不是独立人工标注；失败和缺失评分均保留。
25个题族的100种问法不是100个独立任务，配对区间按题族bootstrap。

| 策略 | 正常完成 | 语义正确 | 评分缺失 | 忠实性通过 | 回答时忠实性 | 模型调用 | p50秒 |
|---|---:|---:|---:|---:|---:|---:|---:|
| fixed-bm25 | 100/100 | 88/100 | 0 | 87/100 | 73/86 | 100 | 5.59 |
| fixed-rerank | 98/100 | 93/100 | 0 | 93/100 | 80/85 | 100 | 16.44 |
| agent-guarded41-bm25 | 100/100 | 91/100 | 0 | 97/100 | 74/77 | 194 | 27.59 |
| saved_v4_guarded_bm25 | 94/100 | 85/100 | 0 | 92/100 | 70/72 | 199 | 11.84 |

纯拒答没有外部事实时忠实性/引用检查可通过，不代表问题答对；已单列实际回答的忠实性。
旧 v4 的新口径复评分单独列出；同一旧答案跨口径变化见 comparisons.json 的 rubric_only_on_saved_v4。
HTTP失败诊断、7条争议复核、短回归失败记录见 preflight/；不覆盖旧答案或旧分数。
CPU重排和模型推理耗时为共享服务器上的观测值，不是独占GPU吞吐测试。

## 预注册准入

```json
{
  "candidate": null,
  "eligible": false,
  "reasons": [
    "gain_under_3_over_strongest_fixed_baseline"
  ],
  "strongest_fixed_baseline": "fixed-rerank",
  "correct": {
    "fixed-bm25": 88,
    "fixed-rerank": 93,
    "agent-guarded41-bm25": 91
  },
  "stress": {
    "natural": {
      "without": 8,
      "with": 8,
      "total": 8,
      "judge_complete": true,
      "regressions": 0,
      "actual_withholding_cases": 0,
      "followup_requests": 0
    },
    "withheld_first": {
      "without": 0,
      "with": 8,
      "total": 8,
      "judge_complete": true,
      "regressions": 0,
      "actual_withholding_cases": 8,
      "followup_requests": 8
    }
  },
  "test_released": false,
  "human_reviewed": false,
  "gate": {
    "judge": "26 controls: >=24 correct, all 13 negatives rejected, zero judge errors; all comparison rows graded",
    "preflight": "fresh 22 selected dev cases complete with no execution errors, same source fingerprint; then dev starts",
    "transport": "all fresh arms share 120s HTTP request timeout; token/call/search budgets unchanged; no timeout failures discarded",
    "candidate": "100 dev cases, >=99 normal completion, >=3 more correct answers than EACH fixed baseline",
    "critical": "all 20 unknown/clarify/ACL/injection cases correct, no unauthorized citation or forbidden output",
    "evidence": "faithfulness and citation support no worse than either fixed baseline, under common v41 rubric",
    "gap": "same frozen synthetic gap set as v4; withheld-first 8 effective removals, >=6/8 correct and >=4 more than no-followup; natural <=1 regression",
    "browser": "isolated guarded_v41 browser E2E passed",
    "test": "only if eligible: freeze candidate and strongest fixed baseline, then one 300-case comparison; no retuning on test",
    "fallback": "if gate not met, report strongest fixed baseline as reference; do not switch production web or run test",
    "limitations": "model-graded research gate; assistant-authored data; no independent human calibration; paired CIs reported, no guaranteed significance"
  }
}
```

未通过时保留固定基线，不启动300题测试、不切换Web。通过时先冻结模型/代码/数据/规则再执行一次正式对照。
旧文档与样本为当前知识库研究验证；结果不能外推为企业真实用户问题的准确率。
