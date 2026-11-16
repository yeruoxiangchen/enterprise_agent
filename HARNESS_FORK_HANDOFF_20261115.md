# 身份与确认交接修复：原生 fork 子 Agent

后续进度：已按用户要求启动[固定 16 例小测](HARNESS_FIXED16_20261115.md)，只运行新版；原 8 例验源复用，新增 8 例后台执行。监视交给用户，完整 ITSM66 暂缓。

日期：2026-11-15。当前阶段：R6 完成并审阅，已知执行故障通过固定开发对话验收。

## 最终结果

最终配置在本地 Qwen3-8B 上完成 8/8 类固定对话，动作、参数、执行顺序、最终状态与禁止行为检查全部通过，执行异常为 0。逐条审阅确认：管理员补充身份后实际删除一次；关闭在用户明确批准后提交一次；权限拒绝后的坚持请求继续被拒绝；仅评估和取消均未写入，最终答复与回执一致。普通查询、分配及按描述发现目标的答复也与实际业务状态一致。

|配置|动作/状态检查|执行异常|总 Token|模型调用|每案例耗时 p50|
|---|---:|---:|---:|---:|---:|
|原生 DeepAgents 参考 R4（历史复用）|5/8|0|75,744|38|9.19 秒|
|新版执行核心 R6（本轮）|8/8|0|123,012|56|15.00 秒|

Token 增加 62.4%，模型调用增加 47.4%。把失败开销一并计入，每成功案例 Token 分别为 15,148.8、15,376.5。两组使用同一模型、样例、预算与双卡配置；参考和候选在不同时间运行，墙钟数据按各自运行观测记录。这些数字来自 8 类固定开发对话，后续沿完整 ITSM66 协议复测。

R6 只执行候选 8 次，原生参考结果复用 R4。通过加载 R4 冻结实现与当前默认实现，检查相同初始提示、完整工具 Schema、实际业务观测及调用计数一致；证据在 R6 的 `reference_reuse.json`。旧默认路径保留原工具集合，新职责过滤仅由候选启用。

最新相关执行图与业务边界检查 30/30 通过，记录为 `forked-specialist-scope-unit.xml`。最终队列和模型服务均已退出，本轮持有的 GPU 1、5 显存预算已释放。

分析与全部记录：

- `/data/zjr/enterprise_kb_agent/artifacts/harness_native_20261115/fork_handoff_analysis.json`
- `/data/zjr/enterprise_kb_agent/artifacts/harness_native_20261115/native_forked_dev8_20261115_r6/`

## 实际失败与修改

前一轮 Rubric 短测仍出现管理员补充身份后未删除、已确认后未关闭、关闭权限被误作删除权限。复核增加了一次主管重试，但没有解决隔离子 Agent 收不到原始用户事实的问题。

1. 直接使用 DeepAgents 0.7.38 的 `SubAgent(mode="fork")`，继承主管当前有效对话和状态。专业 Agent 继续使用原有领域工具范围；身份补充、确认和取消的原始消息通过官方运行时传入，减少对主管委派文字转述的依赖。原有摘要、工具消息修复、检查点和总预算继续生效。
2. 引入通用 `ToolRoute` 接口，将模型可见的 `check_operation_permission(user_id, operation, ticket_id)` 解析为原后端的 `check_permission(user_id, action, ticket_id)`。操作与权限映射直接来自现有 `PERMISSION_ALIASES`：例如 `close_ticket→edit`、`delete_ticket→delete`。模型只选择实际操作，不再生成另一套权限标签。
3. 实际 Journal 与导出的业务调用仍记录规范后端工具和参数；同时保存 `presented_name` / `presented_arguments`，可以核对模型原始调用与后端实际调用。原有身份来源检查仍会拒绝模型猜测的账号。
4. 新配置使用原生执行与 HITL 确认，关闭额外 Rubric。用户批准仍由明确的结构化事件传入，并匹配待执行工具及参数；恢复沿用原调用和剩余预算。
5. R2 真机发现首次关闭在批准后因缺少查询步骤被拒绝，补查后的再次调用触发第二个暂停并逃出恢复适配器。修复后将明确批准连同原始工具调用 ID 持久化：原暂停调用仍由官方中间件消费批准；同一请求、同一操作和参数的后续前置条件重试复用已有批准。改变对象、操作或请求无法复用；新暂停正常返回待审阅状态。
6. 权限工具的账号 Schema 在每次模型请求时绑定到已有身份来源规则允许的准确账号（用户原文中已经提供，或当前会话身份），避免把 `agent.jsmith` 缩成不存在的 `jsmith`。后端身份规则继续执行，模型猜测的其他身份仍被拒绝。
7. R3 中专业 Agent 在缺少查询时口头承诺继续，仍没有完成关闭。新增只读操作准备：模型已提出且当前专业 Agent 确有权限使用的关闭调用，先通过同一 Journal 执行缺失的 `get_ticket`、`check_dependencies`，再进入官方 HITL 和实际写入。转派补齐 `get_ticket`。准备规则来自现有业务后端的前置条件，不解析测试目标，不增加主管写入权限，不调用额外语言模型。
8. 每次准备读取占用原来的业务工具预算，保存 `origin=operation_preparation` 与原始模型调用 ID；业务调用顺序在真正执行时编号。保持模型提议日志和实际后端执行记录分别可查，避免把模型提议写入的顺序误当成执行顺序。
9. 权限工具在保留后端原始回执的同时，对实际拒绝返回明确的账号、操作和拒绝事实说明。正向权限结果沿用原格式，避免额外的“评估完成”文字干扰后续操作。
10. 最终收紧主管的直接业务工具集合为简单查询（工单查询、列表、营业时间），权限和依赖检查由专业 Agent 持有。主管保持模型规划、澄清和原生 task 委派；专业工作流连贯执行检查与后续操作。R5 轨迹证明主管独自做完安全检查后会直接输出执行承诺，职责过滤解决了这处缺少委派的路径。

这是原生上下文交接与业务接口适配，归入执行可靠性工程。经验学习继续沿独立的轨迹归纳/验证模块记录。

上游采用说明：[DeepAgents forked subagents](https://docs.langchain.com/oss/python/deepagents/subagents)、[LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。本机版本固定为 deepagents 0.7.38 / langgraph 1.2.22；fork API 按此固定版本接入。

## 接入位置

- `kb/harness/native.py`：原生 fork 装配、公开工具路由、真实调用记账。
- `kb/harness/tool_routes.py`：类型化公开工具与后端工具的参数适配。
- `kb/harness/crm_preparation.py`：关闭/转派的只读服务前置条件准备。
- `kb/benchmark/crm_native_adapter.py`：候选开关 `fork_subagents=True, operation_permissions=True`。
- `scripts/harness_native_worker.py`：新增 `native_forked` 配置。
- `scripts/harness_native_check.py`：配对入口 `--fork-comparison`，保存冻结协议与源码。
- `scripts/crm_bench_services.py`：支持同一 BF16 模型两卡 TP，逐卡检查预算与余量，退出释放整个持有服务。

当前新执行核心接入 CRM 适配器。推荐配置：

```python
NativeCRMAdapter(
    ...,
    fork_subagents=True,
    operation_permissions=True,
    prepare_operations=True,
    specialist_owned_checks=True,
    require_dependency_confirmation=True,
)
```

`--fork-comparison` 会启用这一组合；`--candidate-only` 只运行该候选。网页原入口的迁移另行按业务端到端结果决定。

## 已检查的行为

真实官方执行图配合脚本模型，检查原始请求和后来补充的身份同时到达子 Agent、关闭/删除映射到不同后端权限、虚构身份仍被拒绝、fork 子 Agent 在用户批准后只关闭一次。

相关旧原生与确认检查一起通过 15/15；新接口与双卡资源检查通过 8/8；后台参数转发修复后的模型启动检查通过 4/4。这些记录含重复用例，分别保留，不合并求和。

记录：`/data/zjr/enterprise_kb_agent/artifacts/harness_native_20261115/forked-contracts-unit.xml`、`forked-tp-unit.xml`、`forked-launch-unit.xml`。

追加确认重试修复后，fork、权限、HITL、Rubric 相关检查 17/17 通过，见 `forked-approval-retry-unit.xml`。另增加暂停检查点在重启后被重复读取时保留原始工具调用 ID 的检查。

操作准备加入后，相关原生执行、fork、确认和复核行为检查 28/28 通过，见 `forked-preparation-unit.xml`。新增检查覆盖：即使模型直接提出关闭，也先取得真实前置观测并等待确认；主管未获工具能力时不触发准备；准备读取占用共享额度；执行排序与真实回执一致。权限事实说明另有 2/2 检查，见 `forked-permission-message-unit.xml`。

## 真实模型复测

R2 标签：`native_forked_dev8_20261115_r2`。原生动作/状态检查 5/8、fork 6/8；fork 有 1 次确认恢复异常。管理员身份补充后的删除和取消答复已正确；重复确认异常、权限查询账号截断导致的两条失败保留在原始结果及 `review.json`。

R3 标签：`native_forked_dev8_20261115_r3`。原生动作/状态检查 5/8、fork 7/8，两组执行异常均为 0。剩余关闭失败由模型停止子任务、随后主管调用未公开的写入工具并口头承诺造成；无权限案例虽通过动作/状态检查，最终答复仍混淆无依赖与删除权限、重复索要身份，记录在 R3 `review.json`。

R4 标签：`native_forked_dev8_20261115_r4`。原生 5/8、候选 7/8；关闭、拒绝、评估和取消已正确，但删除停在多余确认。R5 恢复正向权限原格式后，候选仍为 7/8；进一步核对模型请求与工具能力，确认失败发生在主管自身：完成安全读取后没有调用 task，直接说正在执行删除。两轮原始失败与假设更新均保存在各自 `review.json`。

最终复测标签：`native_forked_dev8_20261115_r6`。只执行候选，复用原生参考。8 类固定对话的环境分别重置，均使用明确的用户确认约束。只读、分配、发现目标、确认关闭、身份删除、权限拒绝、仅评估、取消全部保留；没有删除失败案例或放宽断言。

GPU 1、5，两卡 TP，模型保持本地 Qwen3-8B BF16，每卡显存比例 0.5（约 12GiB），实际 8192 上下文/1400 输出，共享 40 次模型调用、48 次业务工具调用。两组在同一服务中交替运行；不把两卡部署下的耗时直接与此前单卡测量混算。

R6 队列 PID 2127511、模型 API PID 2127592，均已退出。日志和全部模型/运行数据在：

`/data/zjr/enterprise_kb_agent/artifacts/harness_native_20261115/native_forked_dev8_20261115_r6/`

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh native status --tag native_forked_dev8_20261115_r6
```

首个标签 `native_forked_dev8_20261115_r1` 的后台启动器遗漏了新增参数，子进程在 CLI 预检退出，Actor 执行 0 次、模型未启动。原日志和 `launch_failure.json` 保留；参数转发已修复，并增加启动到子进程的契约检查。
