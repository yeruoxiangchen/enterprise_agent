# 2026-11-17：主管成功轨迹 SFT 与轨迹级 RL

本轮训练主管的条件理解、原生委派和完整收尾；专家、Rubric 复核器使用原始
Qwen3-8B。网页模型继续由原服务配置管理。

当前方案采用 `verified_calls`：从业务执行通过的轨迹中选择可验证回复；
纠错轨迹只监督正确工具调用，错误调用和自然语言结尾保持为零损失上下文。
SFT覆盖8类业务，仍未解出的未分配条件分支列为RL训练重点。r1的原始方案和
停止原因保留在后文，业务执行评分继续统计全部禁用尝试。

## 方法与实现

- 成功轨迹筛选：优先复用已结束采集的原始记录；新采集从 `business-v4/train` 的前32个独立任务采样，每任务最多4次。
  调用真实 DeepAgents 图及 CRM 工具，原执行断言给出结果。覆盖8类业务、至少
  每类2个不同任务后进入SFT；未解决的条件分支单独记录并保留在RL训练和验证中。
- 原始 token：直接使用 vLLM `return_token_ids` 的 prompt/response ID，核对 SDK
  解析、token usage 和主管调用计数。数据标记为 `vllm-return-token-ids-v1`；
  Agent Lightning 轨迹导出器继续支持其原生代理格式。两者共用实际业务结果验证。
- 数据核验：业务失败轨迹退出SFT；业务成功但发生纠错的轨迹逐回复核对原请求、
  JSON Schema、SDK解析、原始token、实际回执和错误账本，只保留正确调用。
  截断、超4096 tokens和错误回复不作为目标，原上下文完整保留。
  模型输入不含期望动作或目标状态。只对主管当前合格回复监督，
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
bash scripts/agent_rl.sh bootstrap launch --tag supervisor_sft_rl_20261117_r2 \
  --reuse-collection supervisor_sft_rl_20261117_r1 --selection-policy verified_calls --gpus 5,6
bash scripts/agent_rl.sh bootstrap watch --tag supervisor_sft_rl_20261117_r2
bash scripts/agent_rl.sh bootstrap stop --tag supervisor_sft_rl_20261117_r2
```

恢复运行输出：`/data/zjr/enterprise_kb_agent/rl/outputs/supervisor_sft_rl_20261117_r2`。
关键记录依次为 `collection-summary.json`、`data-gate.json`、`sft/progress.json`、
`sft/result.json`、`sft-comparison.json`。RL 使用独立的同名 `_rl` 输出目录，
完成后自动调用原有同题审阅，核对实际更新、策略版本和原始 token 归因。

## 启动前检查

已检查真实 ChatOpenAI 序列化与实际 CRM 读取、主管 token 选择、错误数据剔除、
覆盖条件及重复任务处理、比较退步门禁、失败子进程清理和跨服务权重路径。
实际缩小 Qwen3 完成 TRL 更新、adapter保存及合并权重比较。
真实8B的采样、优化器更新与最终效果写入本轮运行记录。

## 启动记录

2026-11-17 07:04:38 UTC，代码 `dfecece`，队列 PID `2838522`，GPU5、6。
运行已进入真实主管轨迹采集；首条合格评论任务得到3次主管原始回复，
prompt长度1902/2035/2162、response长度30/65/51，实际执行 get_ticket 和
add_ticket_comment，业务评分1、路由错误0。源快照117个文件与提交逐一对应。
21项去重行为检查通过，包含实际缩小Qwen3的TRL更新及adapter合并。
SFT与RL效果由后续运行结果记录。

启动摘要：[startup.json](docs/releases/20261117_supervisor_sft_rl/startup.json)。

## r1结束审阅与恢复

实际运行26分14秒，在采集门槛停止，SFT与RL优化器更新均为0，拥有的GPU进程已释放。
真实结果文件53条，状态字段最后停在52；新代码已修正逐条写回计数。
53条中42条通过原业务评分、运行异常0；旧筛选保留12条，因纠错丢弃30条，业务失败11条。
缺口是两个删除评估类别的无错轨迹，以及未分配分支的成功示范；后者8次均有禁用改派尝试。

新筛选复用42条实际业务成功轨迹，核对原始wire与回执后保留94条主管回复，
屏蔽60条错误工具调用、50条纠错结尾、3条截断输出。去重后为72条SFT目标，
覆盖8类业务；原始token未重构。未分配分支继续作为RL难例，原业务分数仍为0。

SFT的冷启动覆盖与RL的改进目标分别记录：SFT学习已有可执行正例，RL继续从完整
训练划分采样、接收未解决任务的实际反馈。现有REINFORCE++使用全局批次优势归一化，
具体实现和许可见vendor记录；方法依据[REINFORCE++论文](https://arxiv.org/abs/2501.03262)。
回复监督沿用[TRL的prompt-completion训练](https://huggingface.co/docs/trl/v0.21.0/sft_trainer)。

恢复命令使用新标签，r1及其数据、状态和失败记录保持原样：

```bash
bash scripts/agent_rl.sh bootstrap launch --tag supervisor_sft_rl_20261117_r2 \
  --reuse-collection supervisor_sft_rl_20261117_r1 --selection-policy verified_calls --gpus 5,6
```

[逐类结果与离线恢复摘要](docs/releases/20261117_supervisor_recovery/review.json)。

### r2恢复队列

恢复代码 `9fa7ee7`，PID `3398154`，复用 r1 原始记录完成72条去重监督样本，
8类业务覆盖检查通过。与离线恢复的数据逐条核对input_ids和completion_mask完全一致。
当前等待GPU5、6；等卡期间不加载CUDA模型，保留原显存门槛。
监视／停止使用上方 r2 命令，计算预算120分钟，初始等卡另计。

### 2026-11-18：释放空闲服务，切换GPU0、7

r2已因GPU5、6等卡预算耗尽退出，SFT和RL均未启动，旧运行及其失败记录保留。
检查CamCat模型服务无活动连接、日志最近约10小时无新增后，按用户要求停止
planning-model、visual-model、local-models三个systemd服务，释放约79 GiB显存。
网页、数据库及文件保留。停止回执和恢复命令记录在
`/data/zjr/camcat_studio/checks/gpu_pause_20261118.json`。

新运行 `supervisor_sft_rl_20261118_r3`，队列PID `740683`，GPU0、7。
两卡资源检查通过，复用r1采集记录得到72条去重SFT目标，已启动主管基础模型加载。
计划沿用16次SFT更新、16题开发验证和2次RL更新，计算预算120分钟。
输出：`/data/zjr/enterprise_kb_agent/rl/outputs/supervisor_sft_rl_20261118_r3`。

```bash
bash scripts/agent_rl.sh bootstrap watch --tag supervisor_sft_rl_20261118_r3
bash scripts/agent_rl.sh bootstrap stop --tag supervisor_sft_rl_20261118_r3
```

### 2026-11-18：r3本机请求代理错误与r4恢复

r3在两台vLLM服务就绪后的首个baseline请求返回HTTP 502，已完成模型调用0、
业务工具调用0、SFT/RL更新0，GPU进程均已释放。终端设置了HTTP(S)代理，
未设置NO_PROXY；baseline默认ChatOpenAI客户端把127.0.0.1请求发给了代理。
采集客户端显式使用trust_env=False，因此r1采集未受此问题影响。

修复`scripts/rl_env.sh`，合并保留已有NO_PROXY/no_proxy并补齐127.0.0.1、
localhost和::1，远程代理设置保留。实际本地HTTP契约测试先复现同类502，
修复后3项通过，覆盖同步/异步ChatOpenAI、localhost/127.0.0.1、已有配置保留
及重复加载。该修改只调整进程网络环境，原执行代码和72条原始token监督保持不变。

新运行`supervisor_sft_rl_20261118_r4`，队列PID`773018`，GPU0、7，
计算上限120分钟，复用r1数据。环境回执及脚本快照已存入本轮输出：
`/data/zjr/enterprise_kb_agent/rl/outputs/supervisor_sft_rl_20261118_r4/network-fix.json`。

```bash
bash scripts/agent_rl.sh bootstrap watch --tag supervisor_sft_rl_20261118_r4
bash scripts/agent_rl.sh bootstrap stop --tag supervisor_sft_rl_20261118_r4
```

### 2026-11-18：r4结束与结果审阅

SFT完成16次更新，训练耗时287.9秒；RL完成2次实际优化器更新，训练前后各评测16题。
两项训练及评测均已结束，本轮GPU0、7进程和租约已释放。
自动汇总曾因runner环境没有safetensors/torch报错；已改为使用现有trainer环境，
并在CPU上补完原始结果核对。原失败状态保存在`status-before-review-recovery.json`，
恢复依据与报告哈希在`review-recovery.json`；训练和推理均未重跑。
当前父队列状态为`complete_review_required`，完成表示计算和审阅文件齐备。

|阶段|开发集通过|成功率|
|---|---:|---:|
|原始Qwen3-8B主管|15/16|93.75%|
|SFT后主管|15/16|93.75%|
|SFT＋2步RL后主管|14/16|87.50%|

RL同题对照为改善0题、退步1题、持平15题。新增失败为存在开放依赖时尝试删除，
原有未分配工单上的条件反转、禁止改派尝试仍存在。禁止操作受后端拦截，
评分同时检查尝试行为。RL同一服务链路下，平均耗时24.30→36.37秒，
p95为51.81→94.96秒，平均token为16545.88→16874.81。

32条RL训练轨迹中27条通过业务评分，覆盖6/8类；close和delete_no_permission未进入
这两步采样。第1次更新有3469个非零优势token，梯度范数0.152914；
第2次更新的16条轨迹全部成功，优势token为0，策略梯度损失0，仍有KL梯度。
实际采样策略版本0、1、2均已核对，64/64轨迹奖励与token归因一致，开发题进入优化器0条。

结论：本轮未取得质量提升，RL权重不替换现有应用版本。下一步先检查困难分支的
训练覆盖与有效奖励批次，再开展小规模更新验证。

完整对照与逐题失败详情：
`/data/zjr/enterprise_kb_agent/rl/outputs/supervisor_sft_rl_20261118_r4_rl/comparison.md`、
`review.json`、`failure-details.json`。
