# 业务多Agent v3：项目分层评测

自建开发场景；旧17题、同批23轮四组对照、新增场景分别统计。没有消费冻结300题测试集。
执行/权限/动作/原文完整性为程序检查。模型语义评审独立于答案生成且盲化方案名，但不是人工金标；未知评分保留。
固定RAG无工单工具。拒绝越权可视为正确动作。状态和关键词仅辅助诊断。

|阶段|正常返回|真实服务完成|草稿原文检查|权限检查|调用|p50秒|p95秒|
|---|---|---|---|---|---|---|---|
|fixed|21/23|0/0|0/0|23/23|23|6.10|10.68|
|multi_v2|20/23|3/4|0/0|23/23|56|7.01|15.66|
|multi_v3|20/23|4/4|2/2|23/23|36|6.24|11.06|
|new_acceptance|12/13|4/4|2/2|13/13|24|6.29|22.08|
|regression|14/17|1/1|1/1|17/17|33|6.17|20.60|
|single|23/23|3/4|0/0|23/23|26|7.04|10.93|

## 语义评分（模型评审，非人工准确率）

- fixed：{"correctness": {"pass": 20, "fail": 2, "unknown": 1}, "citation_support": {"pass": 20, "fail": 0, "unknown": 3}, "task_completion": {"pass": 3, "fail": 19, "unknown": 1}, "factual_fidelity": {"pass": 20, "fail": 0, "unknown": 3}}
- multi_v2：{"correctness": {"pass": 17, "fail": 3, "unknown": 3}, "citation_support": {"pass": 17, "fail": 0, "unknown": 6}, "task_completion": {"pass": 5, "fail": 15, "unknown": 3}, "factual_fidelity": {"pass": 17, "fail": 0, "unknown": 6}}
- multi_v3：{"correctness": {"pass": 20, "fail": 3, "unknown": 0}, "citation_support": {"pass": 20, "fail": 0, "unknown": 3}, "task_completion": {"pass": 5, "fail": 18, "unknown": 0}, "factual_fidelity": {"pass": 20, "fail": 0, "unknown": 3}}
- new_acceptance：{"correctness": {"pass": 12, "fail": 1, "unknown": 0}, "citation_support": {"pass": 12, "fail": 0, "unknown": 1}, "task_completion": {"pass": 5, "fail": 8, "unknown": 0}, "factual_fidelity": {"pass": 12, "fail": 0, "unknown": 1}}
- regression：{"correctness": {"pass": 0, "fail": 0, "unknown": 17}, "citation_support": {"pass": 0, "fail": 0, "unknown": 17}, "task_completion": {"pass": 0, "fail": 0, "unknown": 17}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 17}}
- single：{"correctness": {"pass": 23, "fail": 0, "unknown": 0}, "citation_support": {"pass": 23, "fail": 0, "unknown": 0}, "task_completion": {"pass": 5, "fail": 18, "unknown": 0}, "factual_fidelity": {"pass": 23, "fail": 0, "unknown": 0}}

## 检索重排回放
旧开发集BM25与BGE的Recall@5、Hit@5、MRR@5见retrieval_replay.json；分母、数据版本独立于本轮业务题。
这些是既有检索配置的回放指标，不能归因为本轮多Agent修改。
- retrieval-bm25：{"evaluable": 84, "evidence_recall_at_5": 0.8333333333333334, "hit_at_5": 0.8690476190476191, "mrr_at_5": 0.6208333333333333, "all_evidence_at_5": 0.7976190476190477}
- retrieval-rerank：{"evaluable": 84, "evidence_recall_at_5": 0.9166666666666666, "hit_at_5": 0.9642857142857143, "mrr_at_5": 0.7496031746031746, "all_evidence_at_5": 0.8690476190476191}
- 重排减BM25：{"evidence_recall_at_5": 0.08333333333333326, "hit_at_5": 0.09523809523809523, "mrr_at_5": 0.12876984126984126, "all_evidence_at_5": 0.0714285714285714}

## 意图路由与多轮完成
领域集合匹配只衡量领域分配，不能替代细粒度意图识别准确率或语义正确率。
- fixed: 领域集合匹配 0/0；对话全部正常返回 18/20；语义完成见模型评审并等待人工抽检。
- multi_v2: 领域集合匹配 19/23；对话全部正常返回 17/20；语义完成见模型评审并等待人工抽检。
- multi_v3: 领域集合匹配 22/23；对话全部正常返回 17/20；语义完成见模型评审并等待人工抽检。
- new_acceptance: 领域集合匹配 13/13；对话全部正常返回 9/10；语义完成见模型评审并等待人工抽检。
- regression: 领域集合匹配 17/17；对话全部正常返回 14/17；语义完成见模型评审并等待人工抽检。
- single: 领域集合匹配 0/0；对话全部正常返回 20/20；语义完成见模型评审并等待人工抽检。

## 失败与待审计项
- regression/travel/0: ['execution']; error=ValueError
- regression/missing/0: ['execution']; error=ValueError
- regression/meal/0: ['execution']; error=ValueError
- fixed/actual-proxy/0: ['execution']; error=ValueError
- fixed/other-ticket/0: ['execution']; error=RuntimeError
- single/diagnose-ticket/0: ['service_complete']; error=None
- multi_v2/followup-https/0: ['execution']; error=OutputParserException
- multi_v2/topic-switch/1: ['execution']; error=ValueError
- multi_v2/ticket-draft/0: ['execution', 'service_complete']; error=LengthFinishReasonError
- multi_v3/proxy-access/0: ['execution']; error=ValueError
- multi_v3/auth-loan/0: ['execution']; error=ValueError
- multi_v3/actual-proxy/0: ['execution']; error=ValueError
- new_acceptance/new-switch/0: ['execution']; error=ValueError

网页、HTTP并发和两路真实模型请求验收见engineering/report.json；共享服务耗时不外推生产吞吐。
- 工程验收: failed；真实并发问答未全部正常返回
- 评审状态: complete；
