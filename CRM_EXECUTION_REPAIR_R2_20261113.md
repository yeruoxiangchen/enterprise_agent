# ITSM 完整运行后的执行层修复 r2

日期：2026-11-13。候选版本：`business_v6_external_v4_supervisor_r2`。

原始运行：`/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/official_vs_v4_itsm66_20261113_r1/`。132条原始结果、源码快照及评分均保留；本次代码修复不修改旧通过数。

## 已处理的问题

| 观察到的问题 | 修复及验收行为 |
| --- | --- |
| 单引号备注在 `user's` 处提前闭合，正确评论被判为改写 | 支持词内撇号、独立引号片段和转义字符；备注原样传给真实工具，备注内部的动作词不作为操作指令 |
| 普通确认、身份或工单补充丢失待办，已完成步骤被重新执行 | 会话持久化原始意图和待办上下文；确认保留原操作，明确新增、取消或更正分别处理；独立记录动作次数，重复确认不增加写入额度 |
| 跨轮重复依赖检查破坏严格删除顺序 | 保存操作内的真实只读证据；业务数据不变且会话前置证明仍有效时复用，重放记录链接到原调用；业务写入、外部数据变化、证明丢失都会使证据失效 |
| 复用查询仍被计作一次新的成功执行 | 保留全部工具尝试、原始结果和重放来源，`backend_executed=false`、`cache_hit=true`；成功执行序列只统计实际执行，原始尝试仍用于审计和禁用动作检查 |
| 两次错误后，不同的必要查询也被 `no_progress` 拦截 | 新查询照常执行，相同查询复用证据；保留每轮模型及工具次数上限 |
| 已关闭、查无记录、无当前负责人、删除受依赖或权限阻止被当作永远未完成 | 从当前真实查询证据生成业务终态；明确拒绝或已满足后清除待办，多目标部分成功同时报告已提交回执与拒绝原因 |
| 模型先发起没有前置条件的改派、权限或删除调用 | 按当前观测和用户提供的身份动态提供工具；执行时保留独立护栏，隐藏工具的幻觉调用也保留审计 |
| 被动句“get it assigned”没有被识别为分配请求；`Unassigned`被误作状态筛选；“nobody has picked up”未参与唯一目标判断 | 补充被动请求及未分配描述识别；权限询问和完成确认不授权写入；明确拒绝负责人/状态混用的查询参数，指导无筛选列举后检查 `assigned_to` |
| 模型编造工单 ID，或转发内部提示词 | 查询目标须来自可见用户请求或真实列表/依赖证据；纯内部提示词请求与题外请求直接处理，混合业务请求按实际查询及回执收尾，备注引用内容保持原样 |
| 8K 裁判服务固定预留4096输出，长对话触发HTTP400 | 在最终结构化提示构造后调用服务端 Tokenizer；保留完整输入，输出上限取剩余容量并留64 Token余量；空间不足或计数失败明确报错并记录，未发送的请求不计为模型调用 |

## 代码与验证入口

- 执行和会话证据：`kb/benchmark/crm_supervisor.py`、`crm_supervisor_state.py`。
- 通用请求解析与兼容钩子：`crm_contract.py`、`crm_adapter_v6.py`。旧v2/v3执行路径保留原有缓存/停滞策略，通用请求解析获得同样的备注修正。
- 评测预算：`scripts/crm_bench_services.py`、`crm_benchmark.py`；线格式登记为`visible-compact-exact-evidence-v4-budgeted`。
- 新回归：`tests/test_crm_supervisor_regressions.py`、`test_crm_eval_budget.py`；通过真实LangChain/LangGraph循环、冻结工具和后台状态，固定模型决策复现执行缺陷；Token预算检查覆盖原4228/4566输入长度及计数失败。
- 原有未知写入结果、崩溃恢复、权限隔离、顺序和多目标回归继续参与验证。

测试报告目录：`/data/zjr/enterprise_kb_agent/artifacts/crm_execution_r2_20261113/`。测试完成数和真实模型复测状态以此目录及当前进度入口为准。

联合检查139/139通过，原始报告为`verified-tests.xml`。分号及换行分隔规则的补充兼容检查49/49通过，登记于`parser-compatibility-tests.xml`；其中46项与联合检查重叠，去重后142项全部通过，统计见`verification.json`。原运行18条候选未通过记录逐项登记于`failure-review.json`；源代码差异另存`changes.patch`。

真实模型短复测`execution_r2_4tasks_20261113_r1`已完成8/8：候选4/4、官方参考2/4，双方未判定和运行异常0，四题开场相同。整轮447.65秒，本轮PID1181003及GPU3模型服务已退出。输出为`/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/execution_r2_4tasks_20261113_r1/`。[逐题审阅及效率](/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/execution_r2_4tasks_20261113_r1/review_20261113/review.md)。

已确认修复在真实轨迹生效：含user's备注原样提交，已关闭工单只查询收尾，内部提示词请求得到拒绝，删除指定动作无重复检查。仍有一次模型缺少action且猜测身份的权限查询尝试，被护栏拒绝后正常完成，原始尝试保留。候选Token33939、官方62402；四题Agent累计耗时42.85/45.91秒，普通备注流程仍有多Agent调度开销。后续完整66题复测已按用户要求启动，见下方运行信息。

## 剩余评测问题

旧运行的三条典型裁判误判为：`09:00/17:00`被要求必须写成`9:00 AM/5:00 PM`，明确的关闭确认被判为没有关闭确认，以及已确认新负责人的回答被判为没有确认负责人。`itsm_ops_018_dependency_aware_close_warning`的模拟用户开场还增加了关闭依赖工单的要求。这些需要在新的统一评测协议下分析，代码修复不直接改写其旧判定。

真实模型短复测保持`official_graph`与`multi_v6`两组，原始工具、任务和业务评分条件使用冻结上游文件。检查备注后更新状态、已关闭工单、严格顺序删除和混合提示词请求四类代表案例，再根据新结果决定完整目录复测。

## 完整目录复测已启动

2026-11-13 14:04 UTC，已执行下方命令。运行标签`execution_r2_itsm66_20261113_r1`，队列PID1290659，模型PID1290992，自动选中GPU3。预检查已确认66题、两组132次，当前加载模型。保持原完整目录30轮/600秒及两组一致预算；显存预算78%，选卡保留1GiB余量，整体6小时上限。

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/crm_bench_run.sh launch \
  --tag execution_r2_itsm66_20261113_r1 \
  --harness-version v6 --execution-adapter v4 \
  --arms official_graph multi_v6 --full-itsm --trials 1 \
  --shared-eval-model --user-strategy contextual-react \
  --max-hours 6 --gpus 0,1,2,3,4,5,6,7
```

启动后监视：

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/crm_bench_run.sh watch --tag execution_r2_itsm66_20261113_r1
```

停止时将`watch`换成`stop`。输出目录为`/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/execution_r2_itsm66_20261113_r1/`。
