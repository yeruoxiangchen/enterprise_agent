# 用户校准、固定业务回归与有门槛的公开小测

日期：2026-11-13。入口 `scripts/crm_acceptance.py`，复用现有模型租约、上游React循环和审核规则、实际多Agent与规范ITSM工具。协议v2新增显式客户对话适配与完整JSON审核；本轮60项定向代码检查通过。此前37项入口检查及68项业务状态修复回归保留为历史记录，测试组有重叠，不相加计数。

运行 `calibrated_acceptance_20261113_customer_r3` 已结束，原PID `110088` 已退出，本轮GPU 2模型资源已释放。含加载的总用时656.12秒，低于20分钟上限。产物根目录 `/data/zjr/enterprise_kb_agent/artifacts/crm_acceptance_20261113/calibrated_acceptance_20261113_customer_r3/`。

最新结果：用户生成4/4、固定业务4/4、公开任务4/4通过，未判定0；额外审核6/8，误拒2、误放0、截断0。公开四任务平均Agent Token为8471、模型调用共27次，用量缺失0。删除任务中的1次猜测身份尝试被拦截，补充真实身份后规范检查和删除均完成。额外审核继续只作诊断。

## 本轮协议

一次启动Qwen3-8B模型服务，8192上下文、78%显存预算；本轮整个流程含等待和加载最多20分钟。所有客户端分别记录请求、回答、Token及错误。角色共用权重，成绩按本地开发诊断记录，独立模型标记为false。

1. **用户生成校准4例**：通过 `ContextualReactUserSimulator` 复用冻结上游的React循环，临时将原始任务和可见历史转成带客户/客服标签的上下文，在原文备注、第二阶段状态请求、补充身份、目标完成后停止四种上下文生成客户消息。按原开发契约检查原文值、目标及请求/停止行为，要求模型输出完整。
2. **额外验证器校准8例**：四个合法候选与四个非法候选，沿用上游审核规则，以短JSON返回判断和最多240字符的理由；单独测量误拒、误放、调用错误和截断。截断输出计异常。该验证器始终作为诊断对照，不拒绝主流程的客户消息。
3. **固定用户业务回归4例**：实际运行multi_v6/v4，检查查单、备注、备注后改状态、缺身份后补充并删除。每项恢复全新业务环境，直接检查后台状态、权限、写入次数、顺序及计量。
4. **公开任务最多4项**：只有用户生成校准4/4、固定业务4/4以及用量完整，才在同一模型服务上运行原4个公开任务、每项一次。保持任务动作、参数、顺序、终态、保护状态和最终回复检查条件。门槛未过时保存原因并跳过此阶段。

额外验证器在8例校准中全部正确且回答完整时，才具备后续考虑接入的资格；本轮没有根据其成绩动态选择审核策略。主流程和门槛在任何模型调用前写入协议，明确登记 `contextual-react-customer-v1` 与 `reference-rubric-compact-json-v1`。原始上游React模式仍保留为独立选择。

校准标签由明确任务和可见对话编写，独立人工复核尚未登记。规则检查仅适用于这4种明确开发场景，原始生成消息保留供逐例检查；该小批结果不作为独立泛化准确率。

## 8例校准内容

| 例子 | 预期 | 依据 |
| --- | --- | --- |
| 首次请求添加原文备注 | 合法 | 用户提出动作，尚未声称执行完成。 |
| 备注确认后请求Resolved状态 | 合法 | 第一阶段已确认，第二阶段仍待办理。 |
| 被问身份后提供admin.rlee | 合法 | 候选正在提供已知身份，权限仍由业务后端检查。 |
| 两项都确认后停止 | 合法 | 已有对话满足所有退出条件。 |
| 开场直接停止 | 非法 | 用户尚未提出请求。 |
| 仅备注完成便停止 | 非法 | 状态仍为In Progress。 |
| 复制Agent的身份追问 | 非法 | 客户应提供已知身份。 |
| 增加工单删除和提权要求 | 非法 | 超出原有备注任务。 |

生成校准和验证器校准分别统计，不把业务错误、模拟错误、裁判错误混成一个数字。

## 运行与产物

首轮 `calibrated_acceptance_20261113_r1`（原PID `4171565`）在模型启动阶段退出，尚未运行校准或业务用例。本次GPU 3在初始化前被新启动的三维任务占用，CUDA实际可用15.72GiB，低于所需18.38GiB；旧现场保留，资源已释放。

第二轮 `calibrated_acceptance_20261113_r2` 已结束，原队列PID `4191089` 已退出，GPU 2上的本轮模型资源已释放。运行使用8192上下文、78%显存预算，源码快照118份与同步时的当前文件一致。

实际结果：固定用户业务4/4通过；React用户生成2/4通过；额外验证器8/8标签判断一致，其中3条输出达到长度上限。生成门槛未通过，原4项公开任务被跳过，最终状态为 `gate_not_met_review_required`。

两个生成失败分别是将“添加原文备注”改成“确认备注是否已添加”，以及被追问身份时没有提供任务中已有的 `admin.rlee`。最新汇总和待办见[当前进度](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

```bash
bash scripts/crm_bench_run.sh acceptance watch --tag calibrated_acceptance_20261113_customer_r3
```

独立查看和停止：

```bash
bash scripts/crm_bench_run.sh acceptance status --tag calibrated_acceptance_20261113_customer_r3
bash scripts/crm_bench_run.sh acceptance stop --tag calibrated_acceptance_20261113_customer_r3
```

本轮产物：[验收目录](/data/zjr/enterprise_kb_agent/artifacts/crm_acceptance_20261113/calibrated_acceptance_20261113_customer_r3/)。

- `status.json`、`queue.log`：阶段、进度和异常。
- `protocol.json`：预先固定的校准样例、任务、门槛和模型范围。
- `source/`、`source_manifest.json`：实际使用源码的物理副本与摘要；不扫描或复制模型权重。
- `calibration/`：4例生成与8例验证的逐例消息、标签、用量和结果。
- `cases/`：固定业务4例的实际模型、工具、后台状态与回执。
- `gate.json`：公开阶段的准入结果。
- `public4/`：仅在门槛通过后创建，保存公开任务协议、原始轨迹及单独成绩。
- `report.md`、`summary.json`：按阶段汇总，所有失败和未判定保留。

代码入口：[校准样例和评测](kb/benchmark/crm_calibration.py)、[流程与资源管理](scripts/crm_acceptance.py)、[门槛与恢复测试](tests/test_crm_acceptance.py)。前一阶段的问题和修复见[当前状态](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。
