# 业务多Agent v3：项目分层评测

自建开发场景；旧17题、同批23轮四组对照、新增场景分别统计。没有消费冻结300题测试集。
执行/权限/动作/原文完整性为程序检查。模型语义评审独立于答案生成且盲化方案名，但不是人工金标；未知评分保留。
固定RAG无工单工具。拒绝越权可视为正确动作。状态和关键词仅辅助诊断。

|阶段|正常返回|真实服务完成|草稿原文检查|权限检查|调用|p50秒|p95秒|
|---|---|---|---|---|---|---|---|
|smoke|7/8|2/2|2/2|8/8|12|13.35|151.47|

## 语义评分（模型评审，非人工准确率）

- smoke：{"correctness": {"pass": 0, "fail": 0, "unknown": 8}, "citation_support": {"pass": 0, "fail": 0, "unknown": 8}, "task_completion": {"pass": 0, "fail": 0, "unknown": 8}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 8}}

## 检索重排回放
旧开发集BM25与BGE的Recall@5、Hit@5、MRR@5见retrieval_replay.json；分母、数据版本独立于本轮业务题。

## 失败与待审计项
- smoke/followup-https/1: ['execution']; error=ValueError

网页、HTTP并发和两路真实模型请求验收见engineering/report.json；共享服务耗时不外推生产吞吐。
