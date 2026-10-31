# 开发集配对结果

- 左组 fixed-bm25：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/fixed-bm25
- 右组 agent-evidence-bm25：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/agent-evidence-bm25
- 两组各 100 条；差值为右组减左组。所有失败记录保留。
- 原始预测和分数未改写。本报告单独记录推理与汇总代码指纹。
- 指标为自动诊断，不等于独立人工确认的回答准确率。

| 指标 | fixed-bm25 | agent-evidence-bm25 | 配对差值 | 95% 题族 bootstrap 区间 |
|---|---:|---:|---:|---|
| action_correct | 0.9800 | 0.9700 | -0.0100 | -0.0300–+0.0000 |
| all_required_facts_diagnostic | 0.7738 | 0.7381 | -0.0357 | -0.1071–+0.0476 |
| citation_format_valid | 1.0000 | 1.0000 | +0.0000 | +0.0000–+0.0000 |
| citation_gold_evidence_precision | 0.6607 | 0.7024 | +0.0417 | -0.0119–+0.0893 |
| citation_gold_evidence_recall | 0.7321 | 0.7262 | -0.0060 | -0.0417–+0.0298 |
| correct_abstention | 1.0000 | 1.0000 | +0.0000 | +0.0000–+0.0000 |
| correct_clarification | 0.5000 | 0.2500 | -0.2500 | -0.2500–-0.2500 |
| failed | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| false_refusal | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| forbidden_output | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| required_fact_match_diagnostic | 0.9079 | 0.8901 | -0.0179 | -0.0536–+0.0159 |
| unauthorized_citation | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| unsupported_answer | 0.1250 | 0.1875 | +0.0625 | +0.0000–+0.1875 |

## 调用成本

| 分支 | 模型调用 | 工具调用 | 输入 tokens | 输出 tokens |
|---|---:|---:|---:|---:|
| fixed-bm25 | 100 | 695 | 315288 | 10366 |
| agent-evidence-bm25 | 101 | 696 | 315166 | 10289 |
