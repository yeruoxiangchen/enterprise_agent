# Knowledge Desk 评测

- 分支：agent-bm25；划分：dev
- 完成：100/100；模型：kb-qwen3-8b
- 问题为代理编写、原文证据自动核对的候选集，尚未独立人工复核。每题族 4 个变体；置信区间按题族重采样。
- 事实关键词命中、引用与标准证据重合只作诊断，不能宣称为真实回答准确率或语义忠实度。

| 指标 | 均值 | 95% 题族 bootstrap 区间 |
|---|---:|---|
| action_correct | 0.6100 | 0.4800–0.7400 |
| all_required_facts_diagnostic | 0.5119 | 0.3810–0.6429 |
| citation_format_valid | 0.6200 | 0.4900–0.7500 |
| citation_gold_evidence_precision | 0.6104 | 0.4437–0.7646 |
| citation_gold_evidence_recall | 0.4702 | 0.3214–0.6190 |
| correct_abstention | 0.2500 | 0.2500–0.2500 |
| correct_clarification | 0.0000 | 0.0000–0.0000 |
| failed | 0.3800 | 0.2500–0.5100 |
| false_refusal | 0.0000 | 0.0000–0.0000 |
| forbidden_output | 0.0000 | 0.0000–0.0000 |
| required_fact_match_diagnostic | 0.6087 | 0.4821–0.7258 |
| unauthorized_citation | 0.0000 | 0.0000–0.0000 |
| unsupported_answer | 0.0625 | 0.0000–0.1875 |

调用成本：模型 405 次，工具 574 次，输入 911465 / 输出 25268 tokens。

详见 report.json 的分类统计，以及 predictions.jsonl 的逐题结果与完整工具轨迹。

评测方法参考 [BEIR](https://github.com/beir-cellar/beir) 的检索分层评测，以及 [ALCE](https://github.com/princeton-nlp/ALCE) 的答案/引用分开评估。此报告不是这两个官方基准的成绩。
