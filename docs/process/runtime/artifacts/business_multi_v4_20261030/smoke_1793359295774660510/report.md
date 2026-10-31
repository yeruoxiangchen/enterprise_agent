# 多Agent v4：官方结构化结果组件修复与同批对照

实际复用 LangChain create_agent/ToolStrategy 的解析、ToolMessage校验反馈和structured_response；LangGraph保留图状态/检查点。
正文与来源字段分开，主机按已校验来源渲染引用；纯证据不足答复不伪造引用。来源合法不等于事实被支持。
自建开发数据，同模型同知识资料；调用次数可不同。没有使用正式测试集，不覆盖原v3结果。

| 阶段 | 正常返回 | 真实服务动作 | 草稿原文 | 权限检查 | 模型调用 | p50秒 |
|---|---|---|---|---|---|---|
|repair_business|7/7|1/1|1/1|7/7|10|24.76|
|repair_regression|3/3|0/0|0/0|3/3|3|15.33|
|repair_versions|2/2|0/0|0/0|2/2|2|9.11|

## 执行失败与业务失败

## 新版语义评审
普通咨询按回答完成度评价，不要求创建工单；明确办理请求才校验真实回执。新评分与v3旧评分分开。
- repair_business: {"correctness": {"pass": 0, "fail": 0, "unknown": 7}, "citation_support": {"pass": 0, "fail": 0, "unknown": 7}, "task_completion": {"pass": 0, "fail": 0, "unknown": 7}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 7}}
- repair_regression: {"correctness": {"pass": 0, "fail": 0, "unknown": 3}, "citation_support": {"pass": 0, "fail": 0, "unknown": 3}, "task_completion": {"pass": 0, "fail": 0, "unknown": 3}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 3}}
- repair_versions: {"correctness": {"pass": 0, "fail": 0, "unknown": 2}, "citation_support": {"pass": 0, "fail": 0, "unknown": 2}, "task_completion": {"pass": 0, "fail": 0, "unknown": 2}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 2}}

## 官方参考与复用
- https://docs.langchain.com/oss/python/langchain/multi-agent/router
- https://docs.langchain.com/oss/python/langchain/multi-agent/subagents
- https://docs.langchain.com/oss/python/langchain/structured-output
库源代码读取位置：business-packages-v2/langchain/agents/structured_output.py 与 factory.py。具体版本见 requirements.business.lock。
没有引入额外自由规划Supervisor，不增加未验证的协作步骤；保留领域路由、工单原文、权限和真实回执。
