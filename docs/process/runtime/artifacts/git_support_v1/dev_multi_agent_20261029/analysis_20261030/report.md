# 多 Agent v1 离线结果审计

输入：`/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_multi_agent_20261029`

仅分析冻结记录；没有重新推理、重评分或修改旧结果。完成率和自动语义分数分开报告。

|方案|处理|正常完成|动作匹配|模型调用|p50秒|判对/错/未知|
|---|---:|---:|---:|---:|---:|---|
|fixed-rerank|34|34|33|34|13.59|21/6/7|
|single-agent|34|22|21|58|45.79|11/23/0|
|multi-no-skills|34|20|17|142|102.49|8/24/2|
|multi-skills|34|22|19|136|90.85|8/22/4|

## 执行错误

|方案|异常类型|发生过拒答字段冲突的失败记录|冲突后阶段步数耗尽|
|---|---|---:|---:|
|fixed-rerank|{}|0|0|
|single-agent|{"RuntimeError": 9, "ToolInvocationError": 3}|12|9|
|multi-no-skills|{"RuntimeError": 12, "ToolInvocationError": 2}|14|12|
|multi-skills|{"RuntimeError": 10, "ToolInvocationError": 2}|12|10|

其余 ToolInvocationError 的最后事件均需结合 cases.jsonl 与原 checkpoint 排查；日志未保存完整异常原因链，不能仅凭类型断定根因。
问号检测仅用于筛查问题复述，不是语义评分器。细节在 summary.json 和 cases.jsonl。
本轮是34题开发诊断，不是独立测试；不同组的生成次数和证据数上限不同，不是等算力对照。
