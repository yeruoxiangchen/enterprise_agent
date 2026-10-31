# 开发集配对结果

- 左组 fixed-bm25：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/fixed-bm25
- 右组 fixed-rerank：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/fixed-rerank
- 两组各 100 条；差值为右组减左组。所有失败记录保留。
- 原始预测和分数未改写。本报告单独记录推理与汇总代码指纹。
- 指标为自动诊断，不等于独立人工确认的回答准确率。

| 指标 | fixed-bm25 | fixed-rerank | 配对差值 | 95% 题族 bootstrap 区间 |
|---|---:|---:|---:|---|
| action_correct | 0.9800 | 0.9800 | +0.0000 | -0.0300–+0.0300 |
| all_required_facts_diagnostic | 0.7738 | 0.7500 | -0.0238 | -0.1071–+0.0476 |
| citation_format_valid | 1.0000 | 0.9900 | -0.0100 | -0.0300–+0.0000 |
| citation_gold_evidence_precision | 0.6607 | 0.7183 | +0.0575 | -0.0417–+0.1627 |
| citation_gold_evidence_recall | 0.7321 | 0.8095 | +0.0774 | -0.0179–+0.1845 |
| correct_abstention | 1.0000 | 0.9167 | -0.0833 | -0.2500–+0.0000 |
| correct_clarification | 0.5000 | 0.7500 | +0.2500 | +0.2500–+0.2500 |
| failed | 0.0000 | 0.0100 | +0.0100 | +0.0000–+0.0300 |
| false_refusal | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| forbidden_output | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| required_fact_match_diagnostic | 0.9079 | 0.9040 | -0.0040 | -0.0377–+0.0337 |
| unauthorized_citation | 0.0000 | 0.0000 | +0.0000 | +0.0000–+0.0000 |
| unsupported_answer | 0.1250 | 0.0625 | -0.0625 | -0.1875–+0.0000 |

## 调用成本

| 分支 | 模型调用 | 工具调用 | 输入 tokens | 输出 tokens |
|---|---:|---:|---:|---:|
| fixed-bm25 | 100 | 695 | 315288 | 10366 |
| fixed-rerank | 100 | 694 | 317709 | 10416 |
