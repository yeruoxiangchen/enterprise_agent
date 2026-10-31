# 开发集配对结果

- 左组 agent-evidence-bm25：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/agent-evidence-bm25
- 右组 agent-evidence-rerank：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/agent-evidence-rerank
- 两组各 100 条；差值为右组减左组。所有失败记录保留。
- 原始预测和分数未改写。本报告单独记录推理与汇总代码指纹。
- 指标为自动诊断，不等于独立人工确认的回答准确率。

| 指标 | agent-evidence-bm25 | agent-evidence-rerank | 配对差值 | 95% 题族 bootstrap 区间 |
|---|---:|---:|---:|---|
| action_correct | 0.9700 | 1.0000 | +0.0300 | +0.0000–+0.0900 |
| all_required_facts_diagnostic | 0.7381 | 0.7381 | +0.0000 | -0.0952–+0.0952 |
| citation_format_valid | 1.0000 | 1.0000 | +0.0000 | +0.0000–+0.0000 |
| citation_gold_evidence_precision | 0.7024 | 0.7520 | +0.0496 | -0.0496–+0.1587 |
| citation_gold_evidence_recall | 0.7262 | 0.8333 | +0.1071 | +0.0060–+0.2262 |
| correct_abstention | 1.0000 | 1.0000 | +0.0000 | +0.0000–+0.0000 |
| correct_clarification | 0.2500 | 1.0000 | +0.7500 | +0.7500–+0.7500 |
| failed | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| false_refusal | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| forbidden_output | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| required_fact_match_diagnostic | 0.8901 | 0.9002 | +0.0101 | -0.0421–+0.0776 |
| unauthorized_citation | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| unsupported_answer | 0.1875 | 0.0000 | -0.1875 | -0.5625–+0.0000 |

## 调用成本

| 分支 | 模型调用 | 工具调用 | 输入 tokens | 输出 tokens |
|---|---:|---:|---:|---:|
| agent-evidence-bm25 | 101 | 696 | 315166 | 10289 |
| agent-evidence-rerank | 100 | 695 | 314484 | 10789 |
