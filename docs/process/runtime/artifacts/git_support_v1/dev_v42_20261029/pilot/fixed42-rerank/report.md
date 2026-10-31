# Knowledge Desk 评测

- 分支：fixed42-rerank；划分：dev
- 完成：34/34；模型：kb-qwen3-8b
- 问题为代理编写、原文证据自动核对的候选集，尚未独立人工复核。每题族 4 个变体；置信区间按题族重采样。
- 事实关键词命中、引用与标准证据重合只作诊断，不能宣称为真实回答准确率或语义忠实度。

| 指标 | 均值 | 95% 题族 bootstrap 区间 |
|---|---:|---|
| action_correct | 0.9583 | 0.8750–1.0000 |
| all_required_facts_diagnostic | 0.6250 | 0.3750–0.8750 |
| citation_format_valid | 1.0000 | 1.0000–1.0000 |
| citation_gold_evidence_precision | 0.6302 | 0.5312–0.7656 |
| citation_gold_evidence_recall | 0.7083 | 0.5417–0.8750 |
| correct_abstention | 1.0000 | 1.0000–1.0000 |
| correct_clarification | 0.5000 | 0.5000–0.5000 |
| failed | 0.0000 | 0.0000–0.0000 |
| false_refusal | 0.0000 | 0.0000–0.0000 |
| forbidden_output | 0.0000 | 0.0000–0.0000 |
| required_fact_match_diagnostic | 0.8125 | 0.6667–0.9375 |
| unauthorized_citation | 0.0000 | 0.0000–0.0000 |
| unsupported_answer | 0.1250 | 0.0000–0.3750 |

调用成本：模型 34 次，工具 233 次，输入 106513 / 输出 3054 tokens。

详见 report.json 的分类统计，以及 predictions.jsonl 的逐题结果与完整工具轨迹。

评测方法参考 [BEIR](https://github.com/beir-cellar/beir) 的检索分层评测，以及 [ALCE](https://github.com/princeton-nlp/ALCE) 的答案/引用分开评估。此报告不是这两个官方基准的成绩。
