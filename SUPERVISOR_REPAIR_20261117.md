# 2026-11-17：原生委派、观察/执行角色与主管收尾

## 要修复的问题

上一轮从原始 Qwen3-8B 做 1 次专家 RL，同 8 题为 5/8 → 5/8；旧原生接口相同题为 7/8。
主管在评论和改派请求中直接调用未向它开放的工具，收到工具错误后只承诺委派就结束。
主管被冻结，未委派的任务也没有专家梯度。

## 实际机制

1. `delegation_protocol=native` 使用 DeepAgents 原生
   `task(description, subagent_type)`，不向主管要求额外的写工具列表。完整用户原文、
   具体子任务和后台权限仍进入持久化执行合同；历史 explicit 协议保留用于复现。
2. `kb/harness/capabilities.py` 从后台 ToolSpec 目录生成 SubAgent 声明。混合读写的
   能力拆成只读角色及单操作执行角色，只读角色实际没有写工具；每个执行角色只有
   一个写工具及所属领域的读取能力。单写工具目录保留 `__execute` 名称，多写工具目录
   使用 `__<tool_name>`。角色选择仍由主管模型完成，没有客户文本分类器或评分条件路由。
3. 直接复用官方 `RubricMiddleware` 的判据解析、反馈、迭代状态和两次评判上限。
   根节点过滤让它可与原生 fork 并用，专家不会继承完整请求的终局评判。
   `completion_review=errors` 只在真实接口/范围错误后触发，合法拒绝和正常查询不触发。
4. 错误账本按 request_id 存入 SQLite，进程恢复后仍可供主管复核；复核器只读取用户
   原文、实际回执和真实能力目录。它看不到私有奖励标签、期望动作或目标状态。
5. RL 入口显式记录协议/复核配置，拒绝复用其他配置的基线。复核器强制走冻结端点，
   不加入主管或专家的策略梯度。TRL SFT 支持选择 `supervisor` 或 `specialists`，
   数据中的训练角色必须一致。

这部分是官方 Agent 组件的集成、权限建模和恢复机制；没有增加新 RL 损失或宣称新算法。

## 上游依据

- [DeepAgents Subagents](https://docs.langchain.com/oss/python/deepagents/subagents)：
  原生 SubAgent 声明、工具隔离、fork、任务调用和图检查点；本地版本 0.7.38。
- [Grading rubrics](https://docs.langchain.com/oss/python/deepagents/rubric)：
  实际调用安装包的 `deepagents.middleware.rubric.RubricMiddleware`。
- LangChain 的 `wrap_model_call` / `wrap_tool_call` 与 `hook_config`，继续负责实际
  工具可见性、执行拦截和根节点恢复入口。

## 检查与试验

- 原生二参数接口、只读角色、fork 下根节点复核、无错误时跳过、合法拒绝时跳过、
  错误后正确委派、断连恢复、不同请求之间不串用错误状态已检查。
- 使用实际 CRM 工具的两种训练角色检查：复核器均指向冻结端点，其响应不计作策略动作。
- 新配置与新 decoding 的基线不能混用；同题比较校验 harness_profile。
- 原网页路径的文档引用、附件、草稿、确认、工单查询与恢复检查保持通过。

### 原生 API 原型：`supervisor_native_repair16_20261117_r1`

40 次实际执行已经结束。第一组 8 题中，显式三参数接口 5/8，原生接口 7/8；另一组
8 题中参考 5/8、原生原型 6/8。该原型恢复了评论和正向改派，但安全检查专家仍持有
删除工具，在关单检查中错误尝试删除。因而未把该原型作为最终配置。
这也表明只恢复调用 schema 还不够，需要实际拆分观察权限和执行权限。
该轮专家温度 0.7，仅用于接口诊断，无模型训练。

### 分离权限后的对照：`supervisor_native_scoped16_20261117_r2`

使用冻结源代码，预先固定两种配置：explicit 参考和 native + error review。
主管和专家温度都为 0，匹配实际验证模式；请求 seed 为 20261117。
样本为 dev 前 8 题及 dev 第 25–32 题，每种配置 16 题，共 32 次真实执行。
后 8 题未参与这次接口原型的选择。heldout 32 题保留，评分标准没有修改。

状态、请求/响应、回执与最终成绩均在
`/data/zjr/enterprise_kb_agent/rl/outputs/supervisor_native_scoped16_20261117_r2`。
源代码快照在同一项目 `rl/source/supervisor_native_scoped16_20261117_r2`。
仅实际通过后的配置才进入后续主管 SFT / RL。

实测 explicit 9/16、native_scoped 13/16，执行异常均为 0；均值耗时分别为
9.44s、11.58s，均值 token 分别为 10,346.94、14,358.19。新路径恢复了评论、
正向改派和两道关单，但被禁止操作的尝试次数由 1 增至 3，因此这轮仍需修复。
三道失败都发生在已知状态不允许操作时：无负责人仍尝试改派，以及有开放依赖仍尝试
删除。自然语言最终回答正确，原始工具调用仍使它们失败，评分保留。
该轮 116 个执行源文件已逐文件对应 Git `d122da3`。

### 当前状态约束修复

通过 LangChain `ModelRequest.override(tools=...)`，把后台已有的操作前置条件用于
模型的工具可见性：只使用当前版本下实际取得的 get_ticket / check_dependencies
回执。单个目标操作不可用时移除该工具；多个目标时保留工具，对 ticket_id 声明
具体受限实体。未知状态不自动读取，外部状态变化令旧回执失效。
删除入口也校验已知开放依赖；模型若无视工具声明仍发出调用，原始尝试照常记入
审计并按失败计分。这是业务 API 的状态约束工程，没有引入新的学习算法。

### 状态约束轮次：`supervisor_native_affordance16_20261117_r3`

32 次执行结束，源文件冻结于 Git `b1b617b`。explicit 9/16，新路径 15/16，
接口异常均为 0。两道开放依赖删除已不再尝试；剩余一题中，主管将“已分配才改派”
改述为“未分配则改派”，广权限专家在改派工具不可用后转而执行初次分配，实际改变
了工单，按原评分为失败。该结果保留，不能把最终一句自然语言或后台返回成功当作
业务完成。此轮没有训练模型。

### 单操作执行权限

继续使用上游原生 SubAgent 工具声明，每个执行 Agent 只公开一个写操作；从实际
ToolSpec 自动生成，不根据题目文本手写路由。这样改派 Agent 不持有初次分配、删除
或关单权限；需要多个写操作时由主管分别委派。模型强行生成其他写调用仍记录为失败，
执行网关拒绝，原始用户条件继续随执行合同传递。

RL 启动入口默认采用 `--delegation-protocol native --completion-review errors`；
历史试验仍可显式选择 `--delegation-protocol explicit --completion-review none`。
配置变化和执行代码变化均使旧训练基线复用失效。网页部署没有在这轮重新启动。
