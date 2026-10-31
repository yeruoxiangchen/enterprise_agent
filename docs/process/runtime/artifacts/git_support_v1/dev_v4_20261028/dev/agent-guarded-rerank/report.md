# Knowledge Desk 评测

- 分支：agent-guarded-rerank；划分：dev
- 完成：100/100；模型：kb-qwen3-8b
- 问题为代理编写、原文证据自动核对的候选集，尚未独立人工复核。每题族 4 个变体；置信区间按题族重采样。
- 事实关键词命中、引用与标准证据重合只作诊断，不能宣称为真实回答准确率或语义忠实度。

| 指标 | 均值 | 95% 题族 bootstrap 区间 |
|---|---:|---|
| action_correct | 0.8200 | 0.7300–0.9100 |
| all_required_facts_diagnostic | 0.5595 | 0.4048–0.7143 |
| citation_format_valid | 0.8800 | 0.8100–0.9500 |
| citation_gold_evidence_precision | 0.7480 | 0.6581–0.8406 |
| citation_gold_evidence_recall | 0.7440 | 0.6190–0.8690 |
| correct_abstention | 0.9167 | 0.7500–1.0000 |
| correct_clarification | 1.0000 | 1.0000–1.0000 |
| failed | 0.1200 | 0.0600–0.1900 |
| false_refusal | 0.0714 | 0.0119–0.1429 |
| forbidden_output | 0.0000 | 0.0000–0.0000 |
| required_fact_match_diagnostic | 0.7000 | 0.5690–0.8294 |
| unauthorized_citation | 0.0000 | 0.0000–0.0000 |
| unsupported_answer | 0.0000 | 0.0000–0.0000 |

调用成本：模型 202 次，工具 740 次，输入 1000395 / 输出 38702 tokens。

详见 report.json 的分类统计，以及 predictions.jsonl 的逐题结果与完整工具轨迹。

评测方法参考 [BEIR](https://github.com/beir-cellar/beir) 的检索分层评测，以及 [ALCE](https://github.com/princeton-nlp/ALCE) 的答案/引用分开评估。此报告不是这两个官方基准的成绩。
