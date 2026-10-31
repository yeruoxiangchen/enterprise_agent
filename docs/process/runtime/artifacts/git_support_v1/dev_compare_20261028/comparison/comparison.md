# 开发集配对结果

- 固定 RAG：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_compare_20261028/fixed-bm25
- Agent：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_compare_20261028/agent-bm25
- 两组各 100 条；差值为 Agent 减固定 RAG。所有失败记录保留。
- 原始预测和分数未改写。本报告单独记录推理与汇总代码指纹。
- 指标为自动诊断，不等于独立人工确认的回答准确率。

| 指标 | 固定 RAG | Agent | 配对差值 | 95% 题族 bootstrap 区间 |
|---|---:|---:|---:|---|
| action_correct | 0.9800 | 0.6100 | -0.3700 | -0.4900–-0.2500 |
| all_required_facts_diagnostic | 0.7381 | 0.5119 | -0.2262 | -0.3690–-0.0952 |
| citation_format_valid | 1.0000 | 0.6200 | -0.3800 | -0.5100–-0.2500 |
| citation_gold_evidence_recall | 0.7143 | 0.4702 | -0.2440 | -0.3631–-0.1310 |
| correct_abstention | 1.0000 | 0.2500 | -0.7500 | -0.7500–-0.7500 |
| correct_clarification | 0.5000 | 0.0000 | -0.5000 | -0.5000–-0.5000 |
| failed | 0.0000 | 0.3800 | +0.3800 | +0.2500–+0.5100 |
| false_refusal | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| forbidden_output | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| required_fact_match_diagnostic | 0.8841 | 0.6087 | -0.2754 | -0.4004–-0.1603 |
| unauthorized_citation | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| unsupported_answer | 0.1250 | 0.0625 | -0.0625 | -0.3750–+0.1875 |

## 无法进行配对差值的条件指标

- citation_gold_evidence_precision：固定 RAG 有定义 84/100，Agent 有定义 58/100；两组统计对象不同，不计算配对差值。无引用时精度未定义，不补成 0，也不据此剔除其他指标的失败样本。

## 调用成本

| 分支 | 模型调用 | 工具调用 | 输入 tokens | 输出 tokens |
|---|---:|---:|---:|---:|
| 固定 RAG | 100 | 695 | 315520 | 10015 |
| Agent | 405 | 574 | 911465 | 25268 |
