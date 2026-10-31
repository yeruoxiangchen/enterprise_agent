# 知识库 v3 开发集对照

只用于开发诊断；不是独立人工语义准确率，也未运行冻结测试集。

| 分支 | 记录 | 失败 | 全事实关键词诊断（原标签） | 动作匹配 | 模型调用 | 输入 tokens | p50 秒 |
|---|---:|---:|---:|---:|---:|---:|---:|
| retrieval-bm25 | 100/100 | — | — | — | 0 | 0 | 0.02 |
| fixed-bm25 | 100/100 | 0.00% | 77.38% | 98.00% | 100 | 315288 | 5.27 |
| agent-bm25 | 100/100 | 0.00% | 71.43% | 97.00% | 488 | 1144533 | 14.88 |
| agent-native-bm25 | 100/100 | 1.00% | 66.67% | 95.00% | 419 | 966870 | 12.89 |
| agent-evidence-bm25 | 100/100 | 0.00% | 73.81% | 97.00% | 101 | 315166 | 5.72 |
| retrieval-rerank | 100/100 | — | — | — | 0 | 0 | 11.43 |
| fixed-rerank | 100/100 | 1.00% | 75.00% | 98.00% | 100 | 317709 | 16.40 |
| agent-evidence-rerank | 100/100 | 0.00% | 73.81% | 100.00% | 100 | 314484 | 17.01 |

配对报告（含题族 bootstrap 区间）：
- [retrieval](comparison/retrieval/comparison.md)
- [rerank](comparison/rerank/comparison.md)
- [messages](comparison/messages/comparison.md)
- [workflow](comparison/workflow/comparison.md)
- [workflow_rerank](comparison/workflow_rerank/comparison.md)
- [evidence_rerank](comparison/evidence_rerank/comparison.md)

- messages：相同 bounded 控制策略，规范化状态消息 vs 最近真实 assistant/tool 批次＋唯一证据状态；消息角色和上一轮参数呈现共同变化，不宣称只改一个 role 字段。
- rerank：固定检索与生成不变，只在 BM25 前20候选上重排取前5；未添加按开发集调出的分数阈值。
- workflow：对比先取证、最多一次缺口补查；策略和提示词共同变化，按整个工作流评估。
- 修订标签诊断见 label_review；原报告仍保留原冻结标签分数。
- 语义评分状态：pending_independent_judge; reviewed grading.jsonl exported
- CPU 重排首题含模型加载开销；共享 GPU 时延不是专用吞吐。
