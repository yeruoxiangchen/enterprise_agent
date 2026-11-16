# 原生 DeepAgents：执行复核与确认续办

日期：2026-11-15。代码接入和短测完成；本轮 GPU 服务已释放。

## 改动及采用方式

主管继续作为 Agent 理解请求、查询和委派；专业 Agent 使用原有 CRMPort/Journal 执行业务。原生配置直接使用 `deepagents.create_deep_agent`。本轮增加以下两个可独立启用的能力：

- **执行复核**：直接复用 DeepAgents 的 `RubricMiddleware`。主管准备结束时，只读复核 Agent 检查用户原话、实际操作回执和当前答复；发现缺口后，由官方中间件回到主管，最多纠正一次。复核 Agent 不能写业务数据，不能产生用户批准。
- **确认续办**：直接复用官方 HITL 与 LangGraph `Command(resume=...)`。已观测到开放依赖的关闭操作暂停在真正写入之前，外部调用方提交与工具名和参数一致的批准/拒绝后恢复原调用。检查点、回执和剩余预算持续保存。

版本固定为 deepagents 0.7.38、langchain 1.5.0、langgraph 1.2.22。上游模块来自 [DeepAgents](https://github.com/langchain-ai/deepagents) 和 [LangGraph](https://github.com/langchain-ai/langgraph)。Rubric 判定、迭代、反馈消息和跳转均由官方模块实现；业务证据适配、确认事件绑定和用量归集由本项目实现。

这是执行复核与业务恢复机制；轨迹归纳和工作流学习仍沿前序单独的记忆模块记录。

|代码|职责|
|---|---|
|`kb/harness/native.py`|原生主管/子 Agent、SQLite 检查点、共享预算、请求缓存、暂停/恢复入口、复核状态持久化|
|`kb/harness/completion_review.py`|只读用户/回执证据工具，官方 Rubric 适配，复核上下文预算和事件记录|
|`kb/benchmark/crm_native_adapter.py`|CRM 接入，按实际依赖结果选择 HITL，批准与待执行工具绑定，暂停后回执记账|
|`scripts/harness_native_cases.py`|固定用户对话及动作/状态检查，含等待确认、身份补充、拒绝、评估和取消|
|`scripts/harness_native_worker.py`|独立环境执行、显式用户批准传入、状态断言、实际成本和失败记录|
|`scripts/harness_native_check.py`|冻结源码/协议，两配置交替运行，资源预算和退出释放|

## 数据、权限和预算

复核输入来自 `native_requests` 的原始用户消息、`native_user_reviews` 的明确批准记录、Journal 的真实回执和主管实际对话。公开任务的预期动作、答案和最终状态不进入 Actor 或复核 Agent；它们只用于离线判定。

每个请求共用 40 次模型调用、48 次业务工具调用预算；主管、专业 Agent、摘要及复核调用全部计入成本。用户确认恢复沿用原请求的剩余额度。Rubric 最多两次评估，对应最多一次主管纠正；每次嵌套 grader 调用上限 3 次模型请求。官方模块对不完整的判定可再调用 grader 一次，因此单次评估的总调用仍受共享 40 次额度控制。

上下文按实际 8192 token、输出预留 1400 token 计算；复核额外计算结构化判定工具 Schema，覆盖官方 `AutoStrategy` 转 `ToolStrategy` 的路径。复核状态从检查点中的 `_rubric_status` 读取，并连同答复缓存保存。

批准仅由调用方的明确用户事件传入，匹配一个待执行工具及其完整参数。暂停期间不写入，拒绝后不执行该调用；原调用恢复后回执只记录一次。框架的核心暂停可跨进程恢复，确认消息和时间单独留痕。当前 CRM 适配器提供结构化批准接口；网页确认交互仍使用其既有入口。

## 工程行为验证

完整基础检查 21/21 通过，覆盖原生委派、身份边界、真实工具作用域、预算、未知写入对账、官方 Rubric 纠正、纠正上限、确认批准/拒绝、跨进程恢复等。随后补充实际官方 Rubric 与 HITL 的组合检查：复核催促继续执行时仍须暂停等待批准；批准后写入一次，请求重读也不重放。最新复核/批准相关检查 10/10 通过，包含这一新增检查。

验证文件位于 `/data/zjr/enterprise_kb_agent/artifacts/harness_native_20261115/`：

- `rubric-approval-unit.xml`：21 项基础行为检查。
- `rubric-hitl-composition-unit.xml`：最新 10 项复核/批准检查。

这些检查使用脚本模型驱动真实官方执行图；实际模型行为由下面的 GPU 对照测量。

## 本地 Qwen3-8B 对照

最终标签：`native_rubric_dev8_20261115_r3`。GPU 5、RTX 3090、模型显存比例 0.78；16 条执行记录全部完成，队列及持有模型服务已退出。

两组共用确认约束、业务工具、身份规则和预算。分别在重置环境上执行同样的 8 个固定对话（7 个公开开发任务改编对话和 1 个取消案例），同一组配置每例一次；原官方 GraphAgent 和旧 V17 不重跑。

|配置|动作/状态断言通过|执行异常|总 Token（含复核）|模型调用|每案例耗时 p50|
|---|---:|---:|---:|---:|---:|
|原生 DeepAgents|5/8|0|75,753|38|8.09 秒|
|原生 + Rubric|6/8|0|184,249|80|36.32 秒|

额外通过的是无删除权限案例：Rubric 配置实际检查了正确的用户及删除权限，并保持未删除。两组实际提交的写入均为两个分配操作，各执行一次。评估型和取消案例均未发生业务写入。

**答复审阅发现两处动作断言未覆盖的问题**：Rubric 配置在无权限案例中仍反复索要已提供的身份，在取消案例中仍复述原删除计划。这里的 6/8 是动作/状态检查结果，完整服务质量还需要结合这些答复问题判断。当前结果属于固定脚本机制诊断，和前序完整 ITSM66 的分数分别记录。

Rubric 成本为原生配置的 2.43 倍，耗时 p50 为 4.49 倍。4 个案例最终触发纠正上限；23 次运行时评估无异常，但多次把合法等待身份/确认判为未完成，带来无效纠正。

两组共同失败：

1. **依赖确认后关闭**：把 `close_ticket` 对应的 `edit` 权限混成 `delete`，或把已提供的确认丢在委派交接中；实际没有关闭调用。
2. **管理员身份补充后删除**：隔离子任务没有可靠使用主管掌握的身份；随后主管口头表示继续执行，实际没有删除回执。Rubric 发现未完成后，模型仍可能只复述反馈。

因此执行复核保留为可选能力，原生运行时作为参考配置。后续优先处理身份/确认的结构化任务交接与操作权限契约，以及合法暂停的复核误判；扩大整套评测会同时放大当前的调用成本。

原始数据、逐条审阅、冻结协议及源码均位于：

`/data/zjr/enterprise_kb_agent/artifacts/harness_native_20261115/native_rubric_dev8_20261115_r3/`

汇总审阅：同级 `completion_review_analysis.md`、`completion_review_analysis.json`。

## 前序记录保留

- `native_rubric_dev8_20261115_r1`：加载阶段主动停止，0 条 Actor 执行；原因是发现复核 Schema 预算遗漏，修正后使用新标签。
- `native_rubric_dev8_20261115_r2`：保留全部 16 条记录，其中两个取消夹具在运行模型前缺少 Task.instruction 而失败；另发现复核可能催促提前关闭，随后在两组共同接入官方 HITL。原始提前关闭失败仍保留。
- R3 首轮条件请求提前提供身份，并将后续用户批准作为明确的结构化事件送入两组。夹具改变后，R2/R3 的分数不直接作提升比较。
- R3 冻结协议里的 `grader_call_limit_per_evaluation` 应按“每次嵌套 grader 调用上限”解读；上游还有一次覆盖重试。源码的新协议字段已更正，原冻结文件保持原样，审阅文件补充这一解释。

## 使用入口

Python 接入：`NativeCRMAdapter(..., completion_review=True, require_dependency_confirmation=True)`。关闭复核即可使用原生执行基线；明确用户确认通过 `adapter.run(..., approval={"name": ..., "args": ..., "decision": "approve"})` 提交。

查看已完成短测：

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh native status --tag native_rubric_dev8_20261115_r3
```

后续重新评估需使用新标签并冻结当轮代码和协议。本轮运行已经结束。
