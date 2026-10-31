# 开发集配对结果

- 左组 agent-bm25：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/agent-bm25
- 右组 agent-native-bm25：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/agent-native-bm25
- 两组各 100 条；差值为右组减左组。所有失败记录保留。
- 原始预测和分数未改写。本报告单独记录推理与汇总代码指纹。
- 指标为自动诊断，不等于独立人工确认的回答准确率。

| 指标 | agent-bm25 | agent-native-bm25 | 配对差值 | 95% 题族 bootstrap 区间 |
|---|---:|---:|---:|---|
| action_correct | 0.9700 | 0.9500 | -0.0200 | -0.0500–+0.0000 |
| all_required_facts_diagnostic | 0.7143 | 0.6667 | -0.0476 | -0.1786–+0.0476 |
| citation_format_valid | 1.0000 | 0.9900 | -0.0100 | -0.0300–+0.0000 |
| citation_gold_evidence_precision | 0.6131 | 0.5952 | -0.0179 | -0.0734–+0.0417 |
| citation_gold_evidence_recall | 0.6369 | 0.6548 | +0.0179 | -0.0238–+0.0595 |
| correct_abstention | 0.9167 | 0.8333 | -0.0833 | -0.2500–+0.0000 |
| correct_clarification | 0.5000 | 0.2500 | -0.2500 | -0.2500–-0.2500 |
| failed | 0.0000 | 0.0100 | +0.0100 | +0.0000–+0.0300 |
| false_refusal | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| forbidden_output | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| required_fact_match_diagnostic | 0.8583 | 0.8504 | -0.0079 | -0.0575–+0.0337 |
| unauthorized_citation | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| unsupported_answer | 0.1875 | 0.2500 | +0.0625 | +0.0000–+0.1875 |

## 调用成本

| 分支 | 模型调用 | 工具调用 | 输入 tokens | 输出 tokens |
|---|---:|---:|---:|---:|
| agent-bm25 | 488 | 759 | 1144533 | 32318 |
| agent-native-bm25 | 419 | 737 | 966870 | 28962 |
