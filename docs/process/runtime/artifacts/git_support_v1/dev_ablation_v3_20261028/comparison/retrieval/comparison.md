# 开发集配对结果

- 左组 retrieval-bm25：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/retrieval-bm25
- 右组 retrieval-rerank：/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/retrieval-rerank
- 两组各 100 条；差值为右组减左组。所有失败记录保留。
- 原始预测和分数未改写。本报告单独记录推理与汇总代码指纹。
- 指标为自动诊断，不等于独立人工确认的回答准确率。

| 指标 | retrieval-bm25 | retrieval-rerank | 配对差值 | 95% 题族 bootstrap 区间 |
|---|---:|---:|---:|---|
| all_evidence_at_1 | 0.4048 | 0.5952 | +0.1905 | +0.0714–+0.3095 |
| all_evidence_at_10 | 0.9048 | 0.9167 | +0.0119 | -0.0476–+0.0714 |
| all_evidence_at_3 | 0.7024 | 0.7619 | +0.0595 | -0.0714–+0.2024 |
| all_evidence_at_5 | 0.7976 | 0.8690 | +0.0714 | -0.0238–+0.1786 |
| any_evidence_at_1 | 0.4643 | 0.6190 | +0.1548 | +0.0119–+0.2976 |
| any_evidence_at_10 | 0.9405 | 0.9643 | +0.0238 | -0.0238–+0.0833 |
| any_evidence_at_3 | 0.7619 | 0.8452 | +0.0833 | -0.0357–+0.2262 |
| any_evidence_at_5 | 0.8690 | 0.9643 | +0.0952 | +0.0119–+0.1905 |
| evidence_ndcg_at_10_diagnostic | 0.6991 | 0.7903 | +0.0912 | +0.0145–+0.1820 |
| evidence_recall_at_1 | 0.4345 | 0.6071 | +0.1726 | +0.0417–+0.2976 |
| evidence_recall_at_10 | 0.9226 | 0.9405 | +0.0179 | -0.0357–+0.0833 |
| evidence_recall_at_3 | 0.7321 | 0.8036 | +0.0714 | -0.0476–+0.2143 |
| evidence_recall_at_5 | 0.8333 | 0.9167 | +0.0833 | +0.0000–+0.1905 |
| mrr_at_10 | 0.6307 | 0.7496 | +0.1189 | +0.0205–+0.2276 |
| returned | 9.6900 | 9.6900 | +0.0000 | +0.0000–+0.0000 |

## 调用成本

| 分支 | 模型调用 | 工具调用 | 输入 tokens | 输出 tokens |
|---|---:|---:|---:|---:|
| retrieval-bm25 | 0 | 0 | 0 | 0 |
| retrieval-rerank | 0 | 0 | 0 | 0 |
