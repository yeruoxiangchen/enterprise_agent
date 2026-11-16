# 新版 Harness 固定 16 例小测

2026-11-15 13:12 UTC 启动；按用户要求交接后台监视。仅运行新版 `native_forked`，沿用本地 Qwen3-8B 权重；本轮没有训练和完整 ITSM66 评测。

## 完成结果与逐项审阅

2026-11-15 13:17 UTC 计算结束，随后完成本地轨迹审阅。16/16 条记录齐全：复用旧案例 8/8 通过，新增案例 7/8 通过，合计 **15/16（93.75%）**；执行异常、超时和缺失用量均为 0。本轮队列及模型 API 进程已退出，状态记录 `resources_released=true`。

| 范围 | 通过/完成 | Actor Token | 模型调用 |
|---|---:|---:|---:|
| 验源复用旧 8 例 | 8/8 | 123,012 | 56 |
| 本轮新增 8 例 | 7/8 | 66,539 | 31 |
| 合计记录 | 15/16 | 189,551 | 87 |

合计任务耗时 p50 为 12.45 秒、最大 25.95 秒，包含历史执行观测；本轮推理开销仅为新增 8 例。原生 DeepAgents 旧参考仍只覆盖旧 8 例的 5/8，不能把它与本轮 15/16 直接相减作为提升。

唯一失败为 `itsm_ops_010_reassign_unassigned_ticket_fails`。用户要求：若工单未分配，则说明情况并保持不变。主管先查询到了 `assigned_to=""`，随后生成的子任务却把条件反转为“未分配则转派，已分配则不改”。专业 Agent 调用 `reassign_ticket`，业务适配层以 `business_state_conflict` 拒绝，`backend_executed=false`，工单未改变。最终答复正确说明了工单未分配且保持不变，但公开任务的禁止调用断言仍失败；保留原始失败，不以最终状态安全代替指令遵循通过。

新增 8 例的最终答复均与真实回执一致；其余 7 例的转派、已关闭不重关、原文备注后改状态、升级/备注/状态顺序、依赖阻塞删除、不存在工单、状态与营业时间组合查询均通过实际动作/状态检查。答复一致性来自本次人工轨迹审阅，自动汇总仍只统计原定动作/状态断言。

下一步优先处理委派过程中用户条件被反转的问题，沿用原失败例验证，再检查已有委派及安全案例。最新原生执行核心仍处于 CRM 适配与开发验收阶段；产品网页的 `kb/harness/application.py` 仍调用旧 `DeepHarness`，后续需要迁移并验收截图/知识检索到工单办理的完整流程。本轮仅审阅、记录，没有修改 Actor 或启动追加评测。

原始结果：输出目录的 `summary.json`、`rows.json`；逐项审阅见 `manual_review.json`。失败的委派原文及调用顺序保存在对应 `events.jsonl`。

## 样例与复用

目标为 16 个不同开发案例：R6 已通过的 8 例验源后复用，新增 8 例执行。新增覆盖已分配转派、未分配转派拒绝、已关闭工单、备注后更新状态、升级/备注/状态三步操作、依赖阻塞删除、不存在工单、状态与营业时间组合查询。每个新增案例独立重置环境，沿公开任务的动作/参数/顺序/状态/禁止行为断言验收。

这是一套固定用户对话扩展检查，与旧的生成式用户 Development16 分开记录。备注后状态更新的第二条用户消息，只有观察到实际提交了原文备注后才发送。

复用已检查通过：85 个 Actor 源文件哈希一致，模型、种子、上下文、输出、预算、框架版本及策略开关一致；原 8 例用户脚本、执行/评分函数未改变；对 20 次实际后端调用在隔离环境重放，工具观测和最终状态与历史结果一致。原记录、SHA256 和来源保存在本轮 `reuse_plan.json` 与各结果的 `reuse` 字段。

原生 DeepAgents 旧参考仅在已有 8 例覆盖内复用，成绩 5/8，单独保留；新增 8 例先报告新版结果。复用的历史 Token 与本轮新 Token 分别记账，状态中的 `new_tokens` 仅统计新增推理。

新增评测/复用入口检查 9/9 通过，记录：

`/data/zjr/enterprise_kb_agent/artifacts/harness_native_20261115/expanded16-reuse-unit.xml`

Actor 与 R6 相同，本轮仅扩展评测案例、复用校验和记账。

## 启动与交接记录（计算已结束）

- 标签：`native_forked_fixed16_20261115_r1`
- 队列 PID：2179501；模型 API PID：2179552。
- GPU：1、5，双卡 TP；每卡显存比例 0.5，约 12GiB。
- 启动时交接状态：8/16 为已验源的历史记录，新增 0/8，模型权重正常加载中；完成结果见本文上方。
- 总时限：30 分钟；异常停止后续阶段并释放本轮模型服务；没有自动重启。
- 数据、缓存、临时文件与运行日志均在 `/data/zjr/enterprise_kb_agent/`。

输出目录：

`/data/zjr/enterprise_kb_agent/artifacts/harness_native_20261115/native_forked_fixed16_20261115_r1/`

日志：上述目录的 `queue.log`、`actor-model.log`，以及 `trials/<任务>/native_forked/worker.log`。

监视：

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh native watch --tag native_forked_fixed16_20261115_r1
```

单次状态：

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh native status --tag native_forked_fixed16_20261115_r1
```

停止队列并清理本轮持有服务：

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh native stop --tag native_forked_fixed16_20261115_r1
```

结束后审阅 `rows.json`、`summary.json`、`report.md`；分别报告旧 8 例、新增 8 例和合计 16 例，新增失败保留原始结果。
