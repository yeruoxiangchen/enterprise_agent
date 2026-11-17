# 2026-11-17：主管成功轨迹 SFT 与轨迹级 RL

本轮训练主管的条件理解、原生委派和完整收尾；专家、Rubric 复核器使用原始
Qwen3-8B。网页模型继续由原服务配置管理。

## 方法与实现

- 成功轨迹筛选：从 `business-v4/train` 的前32个独立任务采样，每任务最多4次。
  调用真实 DeepAgents 图及 CRM 工具，原执行断言给出结果。覆盖8类业务、至少
  每类2个不同任务，以及改派条件的已分配／未分配分支后提前停止。
- 原始 token：直接使用 vLLM `return_token_ids` 的 prompt/response ID，核对 SDK
  解析、token usage 和主管调用计数。数据标记为 `vllm-return-token-ids-v1`；
  Agent Lightning 轨迹导出器继续支持其原生代理格式。两者共用实际业务结果验证。
- 干净数据：失败、路由错误、复核纠错、截断及任一完整调用超过4096 tokens的轨迹
  保留原记录并退出 SFT；模型输入不含期望动作或目标状态。只对主管当前回复监督，
  历史、用户消息、工具结果、专家和复核器响应均不作为目标。
- SFT：复用 [TRL 0.21 SFTTrainer](https://huggingface.co/docs/trl/v0.21.0/sft_trainer)
  的 `completion_mask` 和 PEFT LoRA，rank16、alpha32、all-linear、lr1e-5、
  microbatch1、累积8，16次更新；梯度检查点、BF16、最大序列4096。
  保存 adapter、检查非零 LoRA B 权重，再由 PEFT safe merge 生成独立基座。
- 同题验证：原始主管与 SFT 主管都在 `dev[0:16]` 上温度0执行，专家和复核器冻结。
  任务哈希、执行配置、解码配置必须一致；通过数不下降、禁用尝试不增加、没有额外
  提交写入才进入 RL。否则结束为 `sft_complete_review_required` 并释放资源。
- RL：复用已有 Agent Lightning / veRL 的轨迹级 REINFORCE++ 与 clipped PPO、
  KL 和 LoRA。以 SFT 基座为初始策略和 reference，冻结专家；2次更新，每批
  4个任务×4次采样，再对相同16题验证。终局奖励继续由原始执行断言计算。

主要代码为 `kb/rl/bootstrap.py`、`bootstrap_data.py`、`sft.py`。
原始 token API 依据 [vLLM 协议](https://docs.vllm.ai/en/stable/api/vllm/entrypoints/openai/chat_completion/protocol/)，
本机版本还已核对安装源码。RL 上游模块与许可沿用 `kb/rl/vendor/UPSTREAM.md`。
这是标准成功轨迹筛选、SFT 与已有 RL 的工程串联。

## 存储、运行及失败处理

默认 GPU5 为主管采样／SFT／RL，GPU6 为固定专家和复核器。推理服务使用每卡
0.78 显存比例、8192上下文；SFT 使用单卡 BF16 LoRA，实际显存峰值写入结果。
所有数据、原始请求、权重、临时目录和缓存都在 `/data/zjr/enterprise_kb_agent/rl`。
源代码快照、任务清单及哈希与运行一起保存。

计算预算120分钟；初始等卡预算另为120分钟，采集阶段最多40分钟。
数据不达标、服务退出、SFT异常或比较退步会停止后续阶段；阶段失败保留现场，
不重启失败标签。停止操作只处理本轮登记的进程及 GPU 租约。

```bash
bash scripts/agent_rl.sh bootstrap launch --tag supervisor_sft_rl_20261117_r1 --gpus 5,6
bash scripts/agent_rl.sh bootstrap watch --tag supervisor_sft_rl_20261117_r1
bash scripts/agent_rl.sh bootstrap stop --tag supervisor_sft_rl_20261117_r1
```

运行输出：`/data/zjr/enterprise_kb_agent/rl/outputs/supervisor_sft_rl_20261117_r1`。
关键记录依次为 `collection-summary.json`、`data-gate.json`、`sft/progress.json`、
`sft/result.json`、`sft-comparison.json`。RL 使用独立的同名 `_rl` 输出目录，
完成后自动调用原有同题审阅，核对实际更新、策略版本和原始 token 归因。

## 启动前检查

已检查真实 ChatOpenAI 序列化与实际 CRM 读取、主管 token 选择、错误数据剔除、
覆盖条件及重复任务处理、比较退步门禁、失败子进程清理和跨服务权重路径。
实际缩小 Qwen3 完成 TRL 更新、adapter保存及合并权重比较。
真实8B的采样、优化器更新与最终效果写入本轮运行记录。
