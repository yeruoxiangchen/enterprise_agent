# Knowledge Desk 评测

- 分支：retrieval-rerank；划分：dev
- 完成：100/100；模型：无模型，CPU 检索
- 问题为代理编写、原文证据自动核对的候选集，尚未独立人工复核。每题族 4 个变体；置信区间按题族重采样。
- 事实关键词命中、引用与标准证据重合只作诊断，不能宣称为真实回答准确率或语义忠实度。

| 指标 | 均值 | 95% 题族 bootstrap 区间 |
|---|---:|---|
| all_evidence_at_1 | 0.5952 | 0.4524–0.7500 |
| all_evidence_at_10 | 0.9167 | 0.8452–0.9762 |
| all_evidence_at_3 | 0.7619 | 0.6071–0.8810 |
| all_evidence_at_5 | 0.8690 | 0.7381–0.9643 |
| any_evidence_at_1 | 0.6190 | 0.4643–0.7738 |
| any_evidence_at_10 | 0.9643 | 0.9286–1.0000 |
| any_evidence_at_3 | 0.8452 | 0.7500–0.9286 |
| any_evidence_at_5 | 0.9643 | 0.9286–1.0000 |
| evidence_ndcg_at_10_diagnostic | 0.7903 | 0.7072–0.8721 |
| evidence_recall_at_1 | 0.6071 | 0.4643–0.7619 |
| evidence_recall_at_10 | 0.9405 | 0.8929–0.9762 |
| evidence_recall_at_3 | 0.8036 | 0.6964–0.8929 |
| evidence_recall_at_5 | 0.9167 | 0.8452–0.9762 |
| mrr_at_10 | 0.7496 | 0.6492–0.8480 |
| returned | 9.6900 | 9.3100–9.9600 |

调用成本：模型 0 次，工具 0 次，输入 0 / 输出 0 tokens。

详见 report.json 的分类统计，以及 predictions.jsonl 的逐题结果与完整工具轨迹。

评测方法参考 [BEIR](https://github.com/beir-cellar/beir) 的检索分层评测，以及 [ALCE](https://github.com/princeton-nlp/ALCE) 的答案/引用分开评估。此报告不是这两个官方基准的成绩。
