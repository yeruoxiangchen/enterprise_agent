# Knowledge Desk 评测

- 分支：agent-guarded42-rerank；划分：dev
- 完成：34/34；模型：kb-qwen3-8b
- 问题为代理编写、原文证据自动核对的候选集，尚未独立人工复核。每题族 4 个变体；置信区间按题族重采样。
- 事实关键词命中、引用与标准证据重合只作诊断，不能宣称为真实回答准确率或语义忠实度。

| 指标 | 均值 | 95% 题族 bootstrap 区间 |
|---|---:|---|
| action_correct | 0.8542 | 0.6875–1.0000 |
| all_required_facts_diagnostic | 0.4583 | 0.2500–0.6875 |
| citation_format_valid | 0.8958 | 0.7292–1.0000 |
| citation_gold_evidence_precision | 0.7188 | 0.5625–0.8750 |
| citation_gold_evidence_recall | 0.7083 | 0.5625–0.8750 |
| correct_abstention | 1.0000 | 1.0000–1.0000 |
| correct_clarification | 0.2500 | 0.2500–0.2500 |
| failed | 0.1042 | 0.0000–0.2708 |
| false_refusal | 0.0625 | 0.0000–0.1875 |
| forbidden_output | 0.0000 | 0.0000–0.0000 |
| required_fact_match_diagnostic | 0.7049 | 0.5521–0.8472 |
| unauthorized_citation | 0.0000 | 0.0000–0.0000 |
| unsupported_answer | 0.0000 | 0.0000–0.0000 |

调用成本：模型 79 次，工具 266 次，输入 397409 / 输出 10984 tokens。

详见 report.json 的分类统计，以及 predictions.jsonl 的逐题结果与完整工具轨迹。

评测方法参考 [BEIR](https://github.com/beir-cellar/beir) 的检索分层评测，以及 [ALCE](https://github.com/princeton-nlp/ALCE) 的答案/引用分开评估。此报告不是这两个官方基准的成绩。
