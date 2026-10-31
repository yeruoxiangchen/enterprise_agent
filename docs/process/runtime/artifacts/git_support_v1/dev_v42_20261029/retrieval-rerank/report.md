# Knowledge Desk 评测

- 分支：retrieval-rerank；划分：dev
- 完成：34/34；模型：无模型，CPU 检索
- 问题为代理编写、原文证据自动核对的候选集，尚未独立人工复核。每题族 4 个变体；置信区间按题族重采样。
- 事实关键词命中、引用与标准证据重合只作诊断，不能宣称为真实回答准确率或语义忠实度。

| 指标 | 均值 | 95% 题族 bootstrap 区间 |
|---|---:|---|
| all_evidence_at_1 | 0.3750 | 0.1250–0.6250 |
| all_evidence_at_10 | 0.8333 | 0.6667–1.0000 |
| all_evidence_at_3 | 0.5729 | 0.3542–0.7812 |
| all_evidence_at_5 | 0.7083 | 0.4583–0.9167 |
| any_evidence_at_1 | 0.3750 | 0.1250–0.6250 |
| any_evidence_at_10 | 0.8333 | 0.6667–1.0000 |
| any_evidence_at_3 | 0.6979 | 0.5104–0.8750 |
| any_evidence_at_5 | 0.8333 | 0.6667–1.0000 |
| evidence_ndcg_at_10_diagnostic | 0.6210 | 0.4828–0.7678 |
| evidence_recall_at_1 | 0.3750 | 0.1250–0.6250 |
| evidence_recall_at_10 | 0.8333 | 0.6667–1.0000 |
| evidence_recall_at_3 | 0.6354 | 0.4688–0.8125 |
| evidence_recall_at_5 | 0.7708 | 0.6042–0.9375 |
| mrr_at_10 | 0.5604 | 0.4167–0.7208 |
| returned | 9.4375 | 8.7083–10.0000 |

调用成本：模型 0 次，工具 0 次，输入 0 / 输出 0 tokens。

详见 report.json 的分类统计，以及 predictions.jsonl 的逐题结果与完整工具轨迹。

评测方法参考 [BEIR](https://github.com/beir-cellar/beir) 的检索分层评测，以及 [ALCE](https://github.com/princeton-nlp/ALCE) 的答案/引用分开评估。此报告不是这两个官方基准的成绩。
