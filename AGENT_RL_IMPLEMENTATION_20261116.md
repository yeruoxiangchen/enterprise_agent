# 企业业务 Agent RL 接入记录 · 2026-11-16

基线为已发布的 `adcbd65802abc17f3d082bad061e988a1aed5cb7`。选择性训练接入真实DeepAgents运行链，可选择主管或专业Agent作为策略模型，其他角色直连固定的Qwen3-8B服务。当前网页服务沿用原有模型配置。

## 方法与实现来源

- **AgentFlow**：[论文](https://arxiv.org/abs/2510.05592)、[官方实现](https://github.com/lupantech/AgentFlow)。借鉴其在完整系统运行过程中选择性训练 Planner 的组织方式；本项目继续使用自己的 DeepAgents 业务图。
- **Agent Lightning 0.3.1**：固定官方兼容分支提交 [`6db3c73`](https://github.com/microsoft/agent-lightning/tree/6db3c73dc0af2e50fe80e56873a57c1703f1b2a7)。直接复用 LitAgent、Trainer、OtelTracer、LLMProxy、LlmProxyTraceToTriplet、VERL 和逐调用训练接口。
- **veRL 0.6.0**：直接使用官方GRPO/REINFORCE++、LoRA/FSDP2、参考策略和vLLM权重同步。奖励为整个任务的可执行结果，沿用Agent Lightning的相同结果分配。当前采用官方逐调用训练路径，不将此实现称为AgentFlow的完整复现或Agent Lightning 1.0。
- **REINFORCE++**：[论文](https://arxiv.org/abs/2501.03262)、[veRL实现](https://verl.readthedocs.io/en/v0.5.x/_modules/verl/trainer/ppo/core_algos.html)。本轮使用`reinforce_plus_plus`，在整个有效动作批次上归一化优势；保留`grpo`配置作对照，不修改上游策略梯度损失。
- 本项目新增：角色模型路由、业务采样适配、参数化任务、业务判分、缺失奖励终止、独立环境与进程管理。没有新增自定义策略梯度算法。

固定旧版兼容组合是因为已有 GPU 环境为 Python 3.11、Torch 2.8、vLLM 0.11；DeepAgents 运行环境为 Python 3.12。训练器和执行器通过官方 Store/Proxy 跨进程连接，分别安装在 `/data` 的独立叠加环境，共享依赖目录只读使用。

代理固定 LiteLLM 1.78.7、OpenAI SDK 2.45.0。兼容层沿用上游鉴权、HTTP代理、OTEL和原始token记录：只补充rollout/attempt/sequence归属字段与Store端点。代理绑定loopback，每次任务生成独立密钥。

veRL 0.6 的HTTP采样前端通过本轮私有Ray进程内的服务扩展注册。扩展从引擎读取已加载的实际LoRA ID，将请求交给原生vLLM OpenAI服务；参数更新后缺少适配器会终止，避免继续采样底座。变权期间关闭prefix cache，输出`served-policy.jsonl`与`policy-updates.json`核对采样和更新。原生attention、工具解析、采样及GRPO损失保留在上游实现中。

另外适配vLLM 0.11新增的executor `non_block`参数：保留veRL原有ZMQ执行与结果，异步接口返回已完成Future。对应上游已记录的[接口兼容问题](https://github.com/verl-project/verl/issues/4308)，本轮保持同步执行，不宣称异步吞吐优化。

在进入原生训练器前，删除该批次所有样本共同的、attention mask为0的左侧prompt padding。全部真实token、position id、response及奖励保留，上限仍为8K；避免把两三千token的主管调用强行补到8K进行全序列计算。小型Qwen3上已核对响应logits和反向梯度一致。r11已完成8/8有效业务轨迹，但在未去除这些padding的反向传播中OOM，参数更新为0；新轮次独立记录。

## 当前训练对象与任务

训练策略使用rank16/alpha32的all-linear LoRA，采样temperature=0.7；固定角色使用另一模型端点和冻结底座，temperature=0。只有选定角色的请求进入训练代理。共享的调用预算、鉴权、业务回执、fork上下文及确认机制继续由实际运行链管理。

`--train-role supervisor`训练主管、冻结专业Agent；`--train-role specialists`冻结主管，训练专业Agent共享的LoRA。r12主管试验中，首两组各4次全成功，后两组各4次全失败，组内优势和梯度均为0。失败轨迹显示固定专家在未分配工单上尝试转派、在依赖未解除时尝试删除。因此下一轮选择训练专业Agent，数据与可执行评分保持v2原样，以检查是否获得实际策略梯度。

专家GRPO r1的首批仍然是同题全对/全错，优势和梯度均为0；下一批在主管直接处理查询、取消时遇到零策略调用，旧代理把有效任务奖励误报为缺失。该轮失败记录保留。现在由实际LangChain回调记录各角色已完成调用数；零策略调用时同时核对上游reward span、任务身份、当前attempt和本地业务结果，保留得分并跳过其梯度。已完成策略调用缺少token轨迹仍然终止。整批没有可训练动作时记录跳过，不生成伪样本。

数据与二值业务奖励保持不变。当前试验选择REINFORCE++利用批次中成功/失败的差异；每次更新记录正/负优势token数、梯度及历史，最后核对LoRA B权重。主管GRPO r12的252个LoRA B张量已核对为全零，未将该轮的训练完成计为学习收益。

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
  --gpus 5,6 --train-role specialists --adv-estimator reinforce_plus_plus \
  --steps 2 --batch-size 4 --group-size 2 \
  --val-limit 4 --max-minutes 90
bash scripts/agent_rl.sh watch --tag supervisor_grpo_short_NEW
bash scripts/agent_rl.sh stop --tag supervisor_grpo_short_NEW
```

每个任务生成独立源码快照、源码指纹、日志、真实业务轨迹、上游训练代理记录和检查点。启动时检查所选卡的空余显存；退出仅释放本轮拥有的进程。失败保留原记录，修复后使用新tag。

大文件、临时文件、模型缓存和检查点物理位置：`/data/zjr/enterprise_kb_agent/rl/`。Ray使用 `/data/zjr/ekbr` 兼容链接缩短Unix socket路径，实际数据仍在上述目录。

## 本轮验证进度

- 原有执行链兼容检查：32项通过。
- 前序应用、角色路由与业务奖励检查：36项通过。
- 应用/角色/奖励/计数配置原36项通过；选择性角色验证现扩展为主管和专家两种实际图路由，共增加1项。加上vLLM接口2项、真实上游数据传输2项、Qwen3 padding概率及梯度等价1项，去重共42项通过。新增`KB_TOKENIZE_MODEL`可将计数服务的别名与生成服务分开。
- 真实上游HTTP代理检查通过：合法鉴权、未鉴权拒绝、原始prompt/response token恢复、Store轨迹归属及归档。报告：`outputs/proxy-wire-final/result.json`。该检查使用受控HTTP后端。
- 新增角色回调、零策略动作的真实图路径、奖励恢复/轨迹丢失拒绝及上游GRPO/REINFORCE++数值对照检查通过；原始报告为`role-outcomes.xml`和`adapter-and-estimator.xml`。前者24项中含原有16项奖励重验，后者3项。
- 最终代码侧检查：运行链、角色、奖励、计数及无动作处理43项，上游适配器/优势数值对照3项，数据传输2项，vLLM接口2项，Qwen3 padding梯度等价1项，共51项通过。

## 已审阅的双卡短训

运行：`outputs/specialists_rpp_short_20261116_r1/`，训练源码与Git提交`0f3ab13`一致。GPU5训练/采样业务专家共享LoRA，GPU6运行固定主管；总耗时22.81分钟，退出后两卡均释放。

|检查|结果|
|---|---|
|训练迭代/更新RPC|2/2|
|优势信号|正492 token、负398 token|
|首轮梯度范数|0.2493、0.3283、0.4242|
|LoRA B权重|252个张量，22,413,312个非零元素；L2=0.0103078|
|更新后采样|策略版本0、1、2均记录；两个更新后适配器ID均被实际选用|
|调用/token/奖励核对|20/20一致；其中5条无专家调用的轨迹保留真实得分|
|执行异常|0|
|训练业务结果|12/16|
|开发业务结果|2/4；未训练基线同为2/4|

这轮已验证有效梯度、权重变化、更新后采样、检查点、开发评估与资源释放的完整闭环。4题开发检查没有出现收益，后续应固定预算开展更长训练与基线对照。完整结构化结果在`review.json`，原始奖励、轨迹与权重核对分别在`rollouts/`、`traces/`和`adapter-weight-review.json`。所有产物物理位置均在`/data/zjr/enterprise_kb_agent/rl/`。

联调发现并修正的接入问题：Ray socket路径长度、跨Ray传递已启动代理的不可序列化资源、训练注意力后端和BF16配置、上游HFModelConfig要求LoRA目标为字符串、单卡FSDP1的显存驻留、代理鉴权及遥测版本差异、veRL HTTP前端的动态LoRA选择。各轮失败日志保留在 `outputs/supervisor_grpo_short_20261116_r*/`。
