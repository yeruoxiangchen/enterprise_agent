# Knowledge Desk 评测

- 分支：agent-bm25；划分：dev
- 完成：100/100；模型：kb-qwen3-8b
- 问题为代理编写、原文证据自动核对的候选集，尚未独立人工复核。每题族 4 个变体；置信区间按题族重采样。
- 事实关键词命中、引用与标准证据重合只作诊断，不能宣称为真实回答准确率或语义忠实度。

| 指标 | 均值 | 95% 题族 bootstrap 区间 |
|---|---:|---|
| action_correct | 0.9700 | 0.9200–1.0000 |
| all_required_facts_diagnostic | 0.7143 | 0.5714–0.8452 |
| citation_format_valid | 1.0000 | 1.0000–1.0000 |
| citation_gold_evidence_precision | 0.6131 | 0.4762–0.7560 |
| citation_gold_evidence_recall | 0.6369 | 0.5060–0.7619 |
| correct_abstention | 0.9167 | 0.7500–1.0000 |
| correct_clarification | 0.5000 | 0.5000–0.5000 |
| failed | 0.0000 | 0.0000–0.0000 |
| false_refusal | 0.0000 | 0.0000–0.0000 |
| forbidden_output | 0.0000 | 0.0000–0.0000 |
| required_fact_match_diagnostic | 0.8583 | 0.7778–0.9286 |
| unauthorized_citation | 0.0000 | 0.0000–0.0000 |
| unsupported_answer | 0.1875 | 0.0000–0.3750 |

调用成本：模型 488 次，工具 759 次，输入 1144533 / 输出 32318 tokens。

详见 report.json 的分类统计，以及 predictions.jsonl 的逐题结果与完整工具轨迹。

评测方法参考 [BEIR](https://github.com/beir-cellar/beir) 的检索分层评测，以及 [ALCE](https://github.com/princeton-nlp/ALCE) 的答案/引用分开评估。此报告不是这两个官方基准的成绩。
