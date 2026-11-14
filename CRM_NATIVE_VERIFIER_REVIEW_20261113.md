# 四任务诊断：两个未知的原因与修复

日期：2026-11-13。原运行 `v4_supervisor_official4_20261113_single_r2`，原始结果保留。修复后运行 `v4_supervisor_official4_20261113_native_r3`，仅重跑同样四项、一次、多Agent、单卡；业务预算和任务检查条件保持原值。

后续结果：r3已结束并释放资源，1通过、3因模拟用户验证失败而未判定，换用原生验证器尚未解决实际语义错误。进一步定位、业务状态修复及下一步校准门槛见[当前状态](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)；下文保留当时的r2分析和r3启动记录。

## 原运行的实际结果

4个任务记录全部落盘，2通过、2未知。成功率下界为2/4；两个未知的异常类型均为 `SimulatorFidelityError`。这里的4/4完成指记录处理完毕，不能解释为四段业务均执行完毕。

| 任务 | 记录中的结果 | 实际发生的行为 |
| --- | --- | --- |
| 查单 | 通过 | 真实读取TCK-1001，正确回答Open/High。 |
| 添加备注 | 未知 | 用户开场生成后被本地审核器拦截；Agent调用数为0，后台没有写入备注。 |
| 先备注后更新状态 | 未知 | 备注真实写入；用户生成下一步“更新为Resolved”的请求被本地审核器拦截。状态仍是In Progress。 |
| 安全删除 | 通过 | 真实检查依赖及admin.rlee权限后删除TCK-1004，目标状态、保护状态及顺序检查通过；中间有重复身份/目标追问。 |

证据：[原运行报告](/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/v4_supervisor_official4_20261113_single_r2/report.md)、[逐任务原始结果](/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/v4_supervisor_official4_20261113_single_r2/rows/)。

## 本地审核器的两个误判

备注任务的可见对话仍是`(conversation start)`，候选用户正在请求确认备注写入。本地LLM审核却返回`goals_complete=true`，理由称用户已经确认写入，随后强迫模拟用户停止。第二次修正依旧误判，任务以评测异常结束。

顺序任务中，可见Agent回复明确保留状态`In Progress`，候选用户提出`Now, please move the ticket's status to 'Resolved' for ticket TCK-1002.`。本地审核器仍声称备注与状态更新均已完成，拦截了正确的下一阶段请求。

两个错误都把候选用户请求当成已发生的Agent操作。它们发生在业务执行或后续请求进入Agent之前，既有记录无法说明被拦截阶段的Agent表现。

## 复用上游原生验证

v4改用冻结上游的 `VerifyUserSimulator._verify`，原生提示词逐项提取全部目标和退出条件，依据已出现的对话核验，再单独审查拟发送的用户消息。直接调用参考实现，不再要求本地LLM输出`goals_complete`。

- [上游源码](/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench/src/harness/user_simulator.py)
- [本地薄适配](kb/benchmark/crm_dialogue.py)：保留开场缓存、角色映射、原生验证失败后的有限修正及确定性的角色回声拦截；原生模式沿用任务总时限和最大轮数，不再叠加本地重复请求裁决。
- 原生审计只记录验证结果、候选是否停止及理由，不把候选停止意图冒充已确认的业务完成状态。
- 任务动作、参数、顺序、终态、受保护状态和最终回复检查条件保持原样；原来的两个未知保留。

旧本地验证模式保留用于历史适配；新v4协议写入 `simulator_verifier=upstream`，版本为 `paired-verified-user-v2-native-verifier`，开场指纹同时记录验证模式。

## 身份续办修复

安全删除轨迹还暴露一个独立缺陷：用户补充`I am the admin.rlee and have the necessary permissions to delete the ticket.`时，续办识别未接受`the`，导致创建新操作、遗漏待办中的工单号，最终靠第三轮再次提供TCK-1004才完成。

[crm_contract.py](kb/benchmark/crm_contract.py)现接受身份称谓前的可选`the`。两种身份表述的集成回归均确认：沿用操作编号、复用已执行依赖检查、实际校验权限、仅删除一次；用户自述“有权限”仍需后端授权结果。

## 验证与新运行

主管、原生用户验证、权限/执行契约和小批运行协议63项定向测试通过，用时76.07秒。覆盖首轮请求、合法第二阶段、提前停止拒绝、角色回声拒绝和实际出现的身份表述；受控模型输出与真实框架/工具用于代码行为验证。

新运行 `v4_supervisor_official4_20261113_native_r3`，队列PID `3938160`，GPU 2，显存预算78%（约18.72GiB），上下文8192，总时限24分钟；仅同样4任务。回答、用户模拟和评分共用Qwen3-8B权重，按单卡开发诊断记录，独立模型标记为false。实际效果以新运行结果为准。

启动核验：服务PID `3938312`，CUDA初始化通过，5个权重分片已加载完成（132.10秒、模型权重15.2683GiB），核验时正在完成服务初始化。协议确认仅原4任务、`simulator_verifier=upstream`，退出释放本次模型服务。启动核验后按工作区约定交由用户监控。

```bash
bash scripts/crm_bench_run.sh watch --tag v4_supervisor_official4_20261113_native_r3
tail -n 80 /data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/v4_supervisor_official4_20261113_native_r3/queue.log
bash scripts/crm_bench_run.sh stop --tag v4_supervisor_official4_20261113_native_r3
```

新产物目录：[原生验证器四任务诊断](/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/v4_supervisor_official4_20261113_native_r3/)。本轮不自动扩大任务范围。
