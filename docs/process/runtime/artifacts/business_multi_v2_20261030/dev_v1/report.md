# 多 Agent v2 开发对照

本轮是自建模拟业务开发集；不属于独立正式测试。模型相同、知识资料相同、检索器均为 BM25+BGE CPU。
固定 RAG 只有问答，没有工单工具；工单完成率只比较单 Agent 和多 Agent。模型调用预算和实际成本另列，不声称等算力。
状态/路由/关键词探针不代表语义正确率；逐点依据、遗漏和引用支持关系仍需人工复核 review.md。

|阶段|执行完成|状态匹配|路由|服务动作|关键词全命中|安全失败|模型调用|耗时秒|
|---|---|---|---|---|---|---|---|---|
|fixed|21/23|18/23|0/0|0/0|18/20|0|23|131.97|
|multi|20/23|16/23|18/23|3/4|16/20|0|54|184.97|
|regression|17/17|17/17|16/17|0/0|11/11|0|43|147.45|
|single|23/23|21/23|0/0|2/4|19/20|0|27|127.43|

## 失败与待复核项
- regression / unknown / 0: 检查失败=['route'], 未命中探针=[], error=None
- fixed / known-and-missing / 0: 检查失败=['status'], 未命中探针=[], error=None
- fixed / actual-proxy / 0: 检查失败=['execution', 'status'], 未命中探针=[], error=ValueError
- fixed / followup-https / 1: 检查失败=['status'], 未命中探针=[], error=None
- fixed / own-ticket / 0: 检查失败=['status'], 未命中探针=['当前账号'], error=None
- fixed / other-ticket / 0: 检查失败=['execution', 'status'], 未命中探针=['当前账号'], error=RuntimeError
- single / followup-https / 1: 检查失败=['status'], 未命中探针=[], error=None
- single / diagnose-ticket / 0: 检查失败=['service_action'], 未命中探针=[], error=None
- single / other-ticket / 0: 检查失败=['status', 'service_action'], 未命中探针=['当前账号'], error=None
- multi / https-401 / 0: 检查失败=['status'], 未命中探针=[], error=None
- multi / access-extension / 0: 检查失败=['status'], 未命中探针=[], error=None
- multi / two-policy / 0: 检查失败=['status', 'route'], 未命中探针=['300', '1个工作日'], error=None
- multi / followup-https / 0: 检查失败=['execution', 'status', 'route'], 未命中探针=['令牌'], error=OutputParserException
- multi / followup-https / 1: 检查失败=['status'], 未命中探针=[], error=None
- multi / topic-switch / 1: 检查失败=['execution', 'status', 'route'], 未命中探针=['--show-origin'], error=ValueError
- multi / ticket-draft / 0: 检查失败=['execution', 'status', 'route', 'service_action'], 未命中探针=['草稿'], error=LengthFinishReasonError
- multi / private-doc / 0: 检查失败=['route'], 未命中探针=[], error=None
