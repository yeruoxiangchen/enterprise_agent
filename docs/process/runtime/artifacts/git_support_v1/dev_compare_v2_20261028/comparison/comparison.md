# 开发集配对结果

- 固定 RAG：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_compare_v2_20261028/fixed-bm25
- Agent：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_compare_v2_20261028/agent-bm25
- 两组各 100 条；差值为 Agent 减固定 RAG。所有失败记录保留。
- 原始预测和分数未改写。本报告单独记录推理与汇总代码指纹。
- 指标为自动诊断，不等于独立人工确认的回答准确率。

| 指标 | 固定 RAG | Agent | 配对差值 | 95% 题族 bootstrap 区间 |
|---|---:|---:|---:|---|
| action_correct | 0.9800 | 0.9700 | -0.0100 | -0.0300–+0.0000 |
| all_required_facts_diagnostic | 0.7619 | 0.7143 | -0.0476 | -0.1429–+0.0476 |
| citation_format_valid | 1.0000 | 1.0000 | +0.0000 | +0.0000–+0.0000 |
| citation_gold_evidence_precision | 0.6687 | 0.6131 | -0.0556 | -0.1389–+0.0218 |
| citation_gold_evidence_recall | 0.7262 | 0.6369 | -0.0893 | -0.2024–+0.0060 |
| correct_abstention | 1.0000 | 0.9167 | -0.0833 | -0.2500–+0.0000 |
| correct_clarification | 0.5000 | 0.5000 | +0.0000 | +0.0000–+0.0000 |
| failed | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| false_refusal | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| forbidden_output | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| required_fact_match_diagnostic | 0.8940 | 0.8583 | -0.0357 | -0.0873–+0.0099 |
| unauthorized_citation | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| unsupported_answer | 0.1250 | 0.1875 | +0.0625 | +0.0000–+0.1875 |

## 调用成本

| 分支 | 模型调用 | 工具调用 | 输入 tokens | 输出 tokens |
|---|---:|---:|---:|---:|
| 固定 RAG | 100 | 695 | 315216 | 10223 |
| Agent | 488 | 759 | 1144533 | 32318 |
