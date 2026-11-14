# CRM 执行层重写与小规模验证

日期：2026-11-13。新适配版本为 `business_v6_external_v3`；继续复用 LangChain `create_agent`、`ToolStrategy`、工具中间件及 LangGraph/SQLite。旧的 `business_v6_external_v2` 保留为对照，历史结果目录不作改写。

## 实现

- `kb/benchmark/crm_execution.py`：单 Agent 和多 Agent 共享一次结构化接入。宿主提供带枚举约束的当前片段 ID，模型选择 `source_ids`，由程序保留原始用户文字；计划明确工具步骤、已知参数和任务依赖。单 Agent 使用全部工具，专业 Agent 使用对应工具集。计划与完成状态不读取 benchmark 的预期动作、预期状态或评分答案。
- 工具执行前核对下一步骤及已知参数；继续复用原有目标、身份、权限和依赖保护。每一步具有稳定操作编号、状态和真实工具证据；重复提交复用原回执，明确要求的两个相同操作分别执行。
- 完成判断逐步检查执行证据。未执行或部分执行的请求不会被模型的完成声明放行；业务拒绝单独保留，最终回答呈现实际工具结果。内部错误指令转换成明确的用户问题。
- 写入结果未知时保留待核实状态，禁止盲目重复写入。LangGraph 恢复时，已提交步骤和回执从持久化状态恢复；完成提交前中断的用例验证恢复后没有第二次写入。
- `kb/benchmark/crm_dialogue.py`：复用上游模拟器的结构化决策、反馈修正及 User/Agent 对话映射。启用模式下逐轮检查用户角色、目标完成与停止决定，候选通过后才进入对话历史。合法的指定持续施压不被提前判为无进展。
- 无法修复的模拟用户失真记为 `SimulatorFidelityError`，在报告中单列。正常用户交互下的 Agent 停滞仍计失败，所有完成任务仍保留在固定分母中。

`--harness-version v6` 的新运行默认选择 v3，也可显式使用 `--execution-adapter v2` 运行旧适配。`--task-ids` 可指定公开 ITSM 任务的小子集，任务及原始评分条件保持原样；小子集直接执行，避免额外重复六项接入检查。

## 已完成的短回归

1. 首轮定向回归 14/14，通过用时 39.05 秒。
2. 补充业务拒绝和否定指令后，联合执行新契约、模拟用户、原有权限/回执和运行预算回归：59/59，通过用时 191.13 秒。
3. 单服务/双服务就绪判断：2/2，通过用时 0.53 秒。

后两组共 61 项。使用脚本模型输出及实际 LangChain/LangGraph、SQLite、公开 ITSM 工具执行，直接检查后台状态、顺序、用量记录和中断恢复；该数字表示代码行为检查。

```bash
bash scripts/crm_bench_run.sh test \
  tests/test_crm_execution.py tests/test_crm_user_fidelity.py \
  tests/test_crm_dialogue.py tests/test_crm_v6_contract.py tests/test_crm_overnight.py \
  --basetemp /data/zjr/enterprise_kb_agent/tmp/crm-v3-regression-20261113

bash scripts/crm_bench_run.sh test tests/test_crm_small_setup.py \
  --basetemp /data/zjr/enterprise_kb_agent/tmp/crm-v3-small-setup-20261113
```

## 真实模型小样例

入口 `scripts/crm_small_check.py`。本轮只执行四类业务样例（查单、语义改写的备注请求、按序备注与状态更新、补充身份后删除），单/多两臂共八项，再单独验证一次模拟用户正确收尾，共九项。

开发短测共用单个 Qwen3-8B 服务，固定脚本用户驱动业务用例；模拟用户忠实性另作真实调用。该配置用于开发验收，正式公开基准仍使用独立回答模型和模拟/裁判模型。

首轮 `v3_short_20261113` 在模型启动阶段失败，0/9：16K 上下文需要 2.25 GiB KV 缓存，实际可用 1.94 GiB。五个权重分片加载用时 307.12 秒，尚未执行业务用例；本轮进程和模型资源均已释放，现场保留。

修复将短测上下文设为 8192，服务命令、服务租约、Agent 输入预算和运行协议使用同一个值；标准服务的默认值仍为 16384。新增启动配置检查连同单/双服务就绪检查 5/5 通过，用时 10.25 秒。

第二轮 `v3_short_20261113_8k_r2` 成功启动模型，查单用例通过（3次模型调用、后台未修改、用量完整）；在第二项语义改写备注请求的计划阶段停止。模型将自由文本 `source_quote` 的双引号改为两个反引号，两次反馈修正仍相同，原文校验拒绝，尚未调用备注工具。该轮资源已释放。

本次将自由文本引用改为宿主提供的 `source_ids` 枚举，复用原有 U 片段分割并保护引号内数据。计划、原文及参数校验继续保留。20项定向回归通过，用时24.04秒，覆盖实际框架请求中的ID枚举、原文保留、备注内容改写拒绝、权限、执行顺序、回执恢复和失败用例完整落盘。

第三轮 `v3_short_20261113_sources_r3` 已因用例失败结束：计划9项，实际执行4项，其中查单、语义改写备注、按序备注与状态更新通过，补充身份后删除失败；其余5项未执行。原PID `3216969` 已退出，状态记录模型资源已释放。该轮使用GPU 3、整卡78%的服务显存预算及8192上下文，总预算15分钟、等卡上限两分钟。

失败轨迹中，首轮依赖检查已经执行；补充 `admin.rlee` 后生成新的任务ID和pending步骤，模型沿用历史依赖检查结果先查权限，被新计划的步骤检查拦截，最终未删除。后端权限保护生效，跨轮待办进度与历史观察需要统一。完整诊断见[进度与评测审计](EVALUATION_AUDIT_20261113.md)。

数据与日志根目录：`/data/zjr/enterprise_kb_agent/artifacts/crm_short_checks_20261113/v3_short_20261113_sources_r3/`。

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/crm_bench_run.sh small status --tag v3_short_20261113_sources_r3

tail -n 80 /data/zjr/enterprise_kb_agent/artifacts/crm_short_checks_20261113/v3_short_20261113_sources_r3/queue.log
```

下一步先处理身份补充的跨轮执行进度并接入原生参考基线。公开任务只选查单、备注、顺序更新、安全删除4项，原生基线和候选各1次，共8次；达到核心合同、权限、幂等和计量门槛后再安排10任务小批扩展。具体任务ID与门槛见[评测审计](EVALUATION_AUDIT_20261113.md)。本次审计没有启动新模型或新评测。

## 复用依据

- [LangChain 结构化输出与校验反馈](https://docs.langchain.com/oss/python/langchain/structured-output)
- [LangChain 工具中间件](https://docs.langchain.com/oss/python/langchain/middleware/custom)
- [LangGraph 任务恢复与幂等](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/functional-api.mdx)
- 上游 CRM-Agent-Bench `VerifyUserSimulator` 的对话格式及 React 决策修正；上游任务、工具、评分器文件保持冻结内容。
