# 业务多Agent v3：项目分层评测

自建开发场景；旧17题、同批23轮四组对照、新增场景分别统计。没有消费冻结300题测试集。
执行/权限/动作/原文完整性为程序检查。模型语义评审独立于答案生成且盲化方案名，但不是人工金标；未知评分保留。
固定RAG无工单工具。拒绝越权可视为正确动作。状态和关键词仅辅助诊断。

|阶段|正常返回|真实服务完成|草稿原文检查|权限检查|调用|p50秒|p95秒|
|---|---|---|---|---|---|---|---|
|smoke|7/8|2/2|2/2|8/8|12|7.56|17.96|

## 语义评分（模型评审，非人工准确率）

- smoke：{"correctness": {"pass": 0, "fail": 0, "unknown": 8}, "citation_support": {"pass": 0, "fail": 0, "unknown": 8}, "task_completion": {"pass": 0, "fail": 0, "unknown": 8}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 8}}

## 检索重排回放
旧开发集BM25与BGE的Recall@5、Hit@5、MRR@5见retrieval_replay.json；分母、数据版本独立于本轮业务题。
这些是既有检索配置的回放指标，不能归因为本轮多Agent修改。

## 意图路由与多轮完成
领域集合匹配只衡量领域分配，不能替代细粒度意图识别准确率或语义正确率。
- smoke: 领域集合匹配 8/8；对话全部正常返回 5/6；语义完成见模型评审并等待人工抽检。

## 失败与待审计项
- smoke/followup-https/1: ['execution']; error=ValueError

网页、HTTP并发和两路真实模型请求验收见engineering/report.json；共享服务耗时不外推生产吞吐。
