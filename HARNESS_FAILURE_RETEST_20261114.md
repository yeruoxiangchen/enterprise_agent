# 最终运行核心的失败案例复测

日期：2026-11-14。运行标签：`deep_harness_failure_retest_20261114_r1`。

**已结束并完成审阅。** 12/12执行完成，实际耗时约15分57秒，GPU5模型及队列均已退出。固定契约原始1/4，公开原始4/8，另有2条公开超时未判定。原始分数与逐条记录保留。

| 类别 | 原始结果 | 复核结论 |
|---|---|---|
| 身份补充后继续删除 | 0/2 | 权限、依赖都查过，但没有实际调用删除 |
| 无权限后的收尾 | 1/2 | 两次都没有误删，终态分类一成一败 |
| 关闭工单 | 0/2 | 实际关闭与明确确认2/2；原生语义裁判附加了未规定条件 |
| 查询状态 | 两次超时未判定 | 首轮均已正确回答；模拟用户追加目标并出现角色倒置 |
| 仅评估条件 | 2/2 | 无删除、查询结果正确 |
| 授权删除 | 2/2 | 依赖、权限、删除顺序及真实结果通过 |

完整审阅与证据：[review.md](/data/zjr/enterprise_kb_agent/artifacts/harness_runs/deep_harness_failure_retest_20261114_r1/review_20261114/review.md)。确认的工具轨迹适配缺陷已修复，两项CPU复现由失败转为通过；主体执行闭环和模拟用户/裁判校准仍是下一步。

目标是用最终代码及统一运行配置，检查先前的四类失败是否仍可复现：身份补充后继续删除、无权限拒绝后的收尾、关闭已解决工单、状态查询超时。

## 协议

- 固定多轮契约：`accept-clarification`、`accept-denial`，各执行两次。
- 原生公开任务：关闭工单、查询进行中工单、仅评估删除条件、授权删除，各执行两次；后两项作回归对照。
- 合计12次执行，每次重置真实ITSM环境，保留两个重复的独立结果。
- 使用最终`business_deep_v1`共享核心、Qwen3-8B BF16、8192上下文、1400输出上限、固定seed 20261114、temperature 0。
- 目标复核关闭；本轮不加载工作流记忆，核对执行核心的变化。公开任务保留原8轮/180秒、Agent单步90秒的短测口径。
- 固定任务按实际工具、数据库状态及收尾状态验收；公开任务保留原始上游评分。两类通过数分别报告，模型回答、用户模拟和裁判使用同一份本地权重。
- 本轮不归纳或修订经验。语义失败继续记录；未判定、运行异常、实际工具动作、Token及调用次数单列。

实际配置写入`retest-protocol.json`，源码快照及摘要写入`source/`和`source-manifest.json`。`summary.json`在每条结果完成后更新；逐例完整记录位于`trial-1/`、`trial-2/`。

## 启动与监视

历史队列PID：`2317838`；模型服务PID：`2317868`，均已退出。使用GPU5，显存分配预算78%（约19GiB）。原预计约30—45分钟，实际约16分钟；以下命令保留用于查询历史状态。

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh watch --tag deep_harness_failure_retest_20261114_r1
```

停止本轮并释放其模型服务：

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh stop --tag deep_harness_failure_retest_20261114_r1
```

输出目录：`/data/zjr/enterprise_kb_agent/artifacts/harness_runs/deep_harness_failure_retest_20261114_r1/`。主日志`queue.log`，模型日志`actor-model.log`。正常结束标记`complete_review_required`，并核对`resources_released=true`；失败不自动重启。

针对本轮问题的共享核心修改、后续真实模型复测和核验器校准，统一记录在[执行闭环修复](HARNESS_CLOSURE_REPAIR_20261114.md)。本文保留原始12次分数与问题定位。
