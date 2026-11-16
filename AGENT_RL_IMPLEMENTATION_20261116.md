# 企业业务 Agent RL 接入记录 · 2026-11-16

基线为已发布的 `adcbd65802abc17f3d082bad061e988a1aed5cb7`。本轮将主管的策略模型接入真实 DeepAgents 运行链，专家使用独立、固定的 Qwen3-8B 服务；当前网页服务沿用原有模型配置。

## 方法与实现来源

- **AgentFlow**：[论文](https://arxiv.org/abs/2510.05592)、[官方实现](https://github.com/lupantech/AgentFlow)。借鉴其在完整系统运行过程中选择性训练 Planner 的组织方式；本项目继续使用自己的 DeepAgents 业务图。
- **Agent Lightning 0.3.1**：固定官方兼容分支提交 [`6db3c73`](https://github.com/microsoft/agent-lightning/tree/6db3c73dc0af2e50fe80e56873a57c1703f1b2a7)。直接复用 LitAgent、Trainer、OtelTracer、LLMProxy、LlmProxyTraceToTriplet、VERL 和逐调用训练接口。
- **veRL 0.6.0**：直接使用官方 GRPO、LoRA/FSDP2、参考策略和 vLLM 权重同步。奖励为整个任务的可执行结果，沿用 Agent Lightning 的相同结果分配。当前采用官方逐调用训练路径，不将此实现称为 AgentFlow 的完整复现或 Agent Lightning 1.0。
- 本项目新增：角色模型路由、业务采样适配、参数化任务、业务判分、缺失奖励终止、独立环境与进程管理。没有新增自定义策略梯度算法。

固定旧版兼容组合是因为已有 GPU 环境为 Python 3.11、Torch 2.8、vLLM 0.11；DeepAgents 运行环境为 Python 3.12。训练器和执行器通过官方 Store/Proxy 跨进程连接，分别安装在 `/data` 的独立叠加环境，共享依赖目录只读使用。

代理固定 LiteLLM 1.78.7、OpenAI SDK 2.45.0。兼容层沿用上游鉴权、HTTP代理、OTEL和原始token记录：只补充rollout/attempt/sequence归属字段与Store端点。代理绑定loopback，每次任务生成独立密钥。

veRL 0.6 的HTTP采样前端通过本轮私有Ray进程内的服务扩展注册。扩展从引擎读取已加载的实际LoRA ID，将请求交给原生vLLM OpenAI服务；参数更新后缺少适配器会终止，避免继续采样底座。变权期间关闭prefix cache，输出`served-policy.jsonl`与`policy-updates.json`核对采样和更新。原生attention、工具解析、采样及GRPO损失保留在上游实现中。

另外适配vLLM 0.11新增的executor `non_block`参数：保留veRL原有ZMQ执行与结果，异步接口返回已完成Future。对应上游已记录的[接口兼容问题](https://github.com/verl-project/verl/issues/4308)，本轮保持同步执行，不宣称异步吞吐优化。

## 当前训练对象与任务

主管使用 rank16/alpha32 的 all-linear LoRA，专家使用另一模型端点和冻结底座，temperature=0。只有主管请求进入训练代理，工具结果和专家输出作为观测。共享的调用预算、鉴权、业务回执、fork上下文及确认机制继续由实际运行链管理。

首批任务使用公开 crm-agent-bench 的真实 ITSM 工具和原生判分函数，另行生成业务实例。它们属于自建训练/开发环境，并非公开 ITSM66 成绩。

- 训练128项，开发32项，组合留出32项。
- 覆盖查询、条件转派、关闭、权限拒绝、依赖阻塞、精确备注、仅评估及取消。
- 训练/开发使用单目标任务；留出使用额外查询加业务请求的双目标组合，数据命名空间独立。
- 任务生成器保留 oracle 供评分，模型只获得用户原文与真实工具返回。
- 每条轨迹从完整初始快照重置。显式处理上游默认“深度合并初始状态”的行为，避免混入默认工单。

当前冻结数据为`datasets/business-v2/`。任务通过带哈希的完整JSON字符串穿过上游LoadedDataset、Arrow与collate边界，再由执行器还原，避免不同工单ID和可选断言被Arrow合并补null。全部8类任务经过真实上游数据管线后逐字段一致，原始初始快照完全保留；损坏载荷和身份错绑会拒绝。

`business-v1`及此前运行全部保留。r10的真实轨迹暴露了嵌套字段合并：4张工单变为511个键，其中507个null；该轮已在0次参数更新时停止，奖励结果作废。v2同时把备注任务的文字改为明确的“先读取再备注”，与原先已要求的动作断言一致；评分约束没有放宽。

奖励检查实际动作、参数、最终状态、禁止尝试、无关对象与用户数据是否被修改、是否发生多余的提交写入。正确保持不变可以成功；被后端拦截的禁止尝试仍然失败。

策略超预算和输出契约失败记为0分；传输、程序与框架故障向上抛出，缺失核验奖励时停止参数更新，避免将接入故障当作负样本。

该奖励目前衡量可执行业务结果。自然语言答复的语义准确性另行审阅，尚未作为此轮自动奖励指标；检索问答能力不在首批任务范围内。

## 运行入口

```bash
# 首次安装，新增环境及缓存均位于 /data
bash scripts/setup_agent_rl.sh

# 生成一次冻结数据；已存在时保留，不静默覆盖
bash scripts/agent_rl.sh prepare

# 短闭环：一张卡训练/采样，一张卡运行冻结专家
bash scripts/agent_rl.sh launch --tag supervisor_grpo_short_NEW \
  --gpus 5,6 --steps 2 --batch-size 2 --group-size 4 \
  --val-limit 4 --max-minutes 90
bash scripts/agent_rl.sh watch --tag supervisor_grpo_short_NEW
bash scripts/agent_rl.sh stop --tag supervisor_grpo_short_NEW
```

每个任务生成独立源码快照、源码指纹、日志、真实业务轨迹、上游训练代理记录和检查点。启动时检查所选卡的空余显存；退出仅释放本轮拥有的进程。失败保留原记录，修复后使用新tag。

大文件、临时文件、模型缓存和检查点物理位置：`/data/zjr/enterprise_kb_agent/rl/`。Ray使用 `/data/zjr/ekbr` 兼容链接缩短Unix socket路径，实际数据仍在上述目录。

## 本轮验证进度

- 原有执行链兼容检查：32项通过。
- 前序应用、角色路由与业务奖励检查：36项通过。
- 当前角色路由、奖励与采样配置检查：28项通过；另增2项，验证策略预算失败计0、程序异常终止训练。检查使用真实工具、真实DeepAgents图，覆盖跨角色共享预算、环境隔离及oracle不进入模型输入。
- 计数端点配置检查：5项通过，覆盖独立模型名、鉴权凭据及原配置回退。新增`KB_TOKENIZE_MODEL`可将计数服务的别名与生成服务分开。
- vLLM实际Executor接口检查：2项通过，同步和Future返回均保留原执行结果；在隔离训练环境运行，不分配GPU。
- 数据传输检查：2项通过，直接使用上游LoadedDataset和collate覆盖全部8类任务，核对完整任务、实际环境快照及载荷完整性。
- 真实上游HTTP代理检查通过：合法鉴权、未鉴权拒绝、原始prompt/response token恢复、Store轨迹归属及归档。报告：`outputs/proxy-wire-final/result.json`。该检查使用受控HTTP后端。
- GPU短闭环正在联调，最终状态、采样数量和参数更新情况以对应输出目录为准；本记录不预填模型收益。

联调发现并修正的接入问题：Ray socket路径长度、跨Ray传递已启动代理的不可序列化资源、训练注意力后端和BF16配置、上游HFModelConfig要求LoRA目标为字符串、单卡FSDP1的显存驻留、代理鉴权及遥测版本差异、veRL HTTP前端的动态LoRA选择。各轮失败日志保留在 `outputs/supervisor_grpo_short_20261116_r*/`。
