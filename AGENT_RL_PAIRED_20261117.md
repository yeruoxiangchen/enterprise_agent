# 业务专家 RL 同题对照 · 2026-11-17

## 失败归因与数据修订

前一轮 `specialists_rpp_short_20261116_r1` 的四题开发检查为 2/4。逐条审阅后，两道失败的实际行为为：

- `close`：读取、检查依赖并成功关闭，但未检查关闭权限。
- `delete_no_permission`：检查权限并正确拒绝删除，但没有读取工单。

两例均完成执行且没有程序异常。删除题的 v2 评分要求 `get_ticket`，用户请求却没有明确要求读取。关闭题的权限检查也只由通用业务政策约束。先修正训练环境的指令—评分对应关系，再观察模型学习，保持原始 v2 数据及其运行结果。

新增冻结数据 `business-v3`：128 训练、32 开发、32 组合留出。读取、权限及依赖检查均写入业务请求；原有必要动作、禁止尝试、终态和无关状态约束保持，同时增加检查对象/用户/操作参数和请求明确给出的先后顺序约束。评分仍为严格二值结果，没有根据失败样例调低通过标准。

该数据复用公开 crm-agent-bench 工具、环境与判分模块，属于自建业务任务。开发题不送入优化器；本轮不调用组合留出集。v2/v3 的分数不作直接增益对比。

## 冻结实验协议

- 固定主管 Qwen3-8B；训练业务专家共享 Qwen3-8B LoRA，rank16、alpha32、all-linear。
- 直接复用 Agent Lightning 0.3.1 与 veRL 0.6.0 的 REINFORCE++，学习率 1e-6、KL 0.001。
- 8 轮训练，每轮 4 项任务、每项 2 条采样，共 64 条训练轨迹；训练温度 0.7。
- 完整 32 题开发集在零次更新时和第 8 轮后各评估一次；温度均为 0，使用同一运行图、固定主管、题目字节和预算。
- 训练前后逐题配对，报告总体及八类任务成功数、改善/退步数、平均及 p95 耗时、token 和模型调用量。
- 按实际 LangChain 回调、上游原始 token 轨迹、奖励与采样适配器版本核对训练归属；核对最终 LoRA B 权重。
- 每 4 轮保存检查点，最多保留 2 个；本轮有独立的时间上限及退出清理。

所有产物：`/data/zjr/enterprise_kb_agent/rl/outputs/specialists_rpp_paired32_20261117_r1/`。源代码快照与数据 manifest 保存在运行目录。当前网页继续使用已发布的稳定配置。

```bash
bash scripts/agent_rl.sh launch --tag specialists_rpp_paired32_20261117_r1 \
  --gpus 5,6 --train-role specialists --adv-estimator reinforce_plus_plus \
  --steps 8 --batch-size 4 --group-size 2 --val-limit 32 \
  --val-before-train --checkpoint-interval 4 --max-minutes 90
bash scripts/agent_rl.sh status --tag specialists_rpp_paired32_20261117_r1
bash scripts/agent_rl.sh stop --tag specialists_rpp_paired32_20261117_r1
```

本轮结果补充在本文末尾；结构化原始报告为运行目录的 `review.json` 和 `comparison.md`。
