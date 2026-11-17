# 2026-11-17：专家交接范围、轨迹级 RL 与成功轨迹 SFT

## 起点与目标

此前 `specialists_rpp_paired32_20261117_r2` 的实际更新已被核验，但开发集通过数为
25/32 → 24/32，关闭工单两边均为 0/4。需要先修交接范围、训练统计和正样本缺口。
这次继续训练共享专家 LoRA；主管仍是冻结的 Qwen3-8B Agent。

## 1. 保留任务范围，并在执行时收窄工具

`kb/harness/delegation.py` 为 DeepAgents 原生 `task` 增加
`allowed_write_tools`。主管提供具体子任务与允许的写工具；纯观察用空列表。
原始用户请求、否定、条件与取消信息一并送给专家，优先级高于主管的转述。

- 原生 task/subagent、fork、checkpoint、恢复均继续由 DeepAgents 0.7.38 执行。
- LangChain 模型中间件过滤实际可见工具；工具中间件再次校验执行范围。
- 每个 task call 的范围在 SQLite 持久化，通过原生
  `lc_subagent_tool_call_id` 元数据定位，避免任务之间串用权限。
- 模型生成的错误参数返回工具反馈供主管纠正，不将其混同为运行时代码异常。
- 嵌套任务只能继续收窄范围。观察任务的模型即使生成写调用，也不会执行后台写入。
- 用户条件与具体业务授权仍由既有业务工具和回执核验，不从描述文本猜测工具权限。

**真实短测暴露并修正的一处接口问题：** `specialists_rollout_contract_short_20261117_r1`
在基线阶段发现，内部合同回写 AI 历史后，主管会照抄整段合同、漏填新 schema 的
`allowed_write_tools`。已主动停止该轮，保留全部记录。最终实现保留模型原始 typed
task 参数及调用历史，仅在工具执行入口给专家注入合同。合同不再替换主管的调用参数。
这属于交接接口修复，不计作训练效果。

## 2. 复用 Agent Lightning 的轨迹级训练模块

实际复用 Microsoft Agent Lightning 提交
`2070bad5113e52f1aa2e5bdba822d4b24b197c05` 的
`rollout_level_advantage.py` 与 `per_rollout_loss.py`，保留 MIT 许可及来源。
主训练环境仍为 veRL 0.6.0 / Agent Lightning 0.3.1，避免整体升级已跑通的 vLLM 栈。
兼容层和版权说明见 [kb/rl/vendor/UPSTREAM.md](kb/rl/vendor/UPSTREAM.md)。

- **优势统计：** 一个完整业务 rollout 只贡献一次终局结果；RPP 先在单标量 episode
  batch 上调用 veRL 原生实现，再广播回该轨迹的所有实际回复 token。这样调用数量、
  代表回复的长度不会改变 whitening 基线。
- **损失：** 每条轨迹内部对全部回复 token 求平均，再对 eligible rollout 求平均；
  PG 和 reference KL 使用相同轨迹权重。沿用上游 clipped PPO 损失、KL、FSDP 和优化器。
- **更新边界：** 一次采样批次的全部有效轨迹在一个优化器更新中累积，微批为 1，
  不按调用行随机分成多个优化器更新，也不裁掉行数余数。
- **完整性：** 某个调用过长则整条轨迹退出该次更新，并记录数量。缺失奖励、原始 token
  或角色归因仍会终止训练；主管独立完成而未调用专家的任务不制造专家梯度。

兼容 actor 当前限定一个训练 rank、一个冻结模型 rank、单 epoch、microbatch=1，
与两张 3090 的现有拓扑相同。未实现多训练 rank 的 rollout 分发。

## 3. 原始成功轨迹 → TRL SFT → RL 初始策略

`kb/rl/sft.py` 提供完整数据与权重入口：

1. 只取 train rollout，重新用原业务状态与实际工具调用校验通过结果。
2. 校验 task payload 哈希、专家实际完成的调用数与原始 token trace 对应。
3. 保留原始 prompt/response token ID，使用 completion_mask；用户、工具结果和历史
   assistant 上下文不参与本次回复的监督。不根据最终答案伪造工具调用对话。
4. 使用 TRL 0.21.0 的原生 SFTTrainer 与 PEFT LoRA，保存真实 adapter。
5. 使用 PEFT 的 `merge_and_unload(safe_merge=True)` 得到单独的 SFT 基座；
   `agent_rl.sh --policy-model` 可从该基座启动新 RL LoRA。冻结主管继续使用原模型。
6. 策略初始化模型路径改变时，旧基线复用被拒绝，重新采集真实未更新基线。

已从旧 r2 训练记录导出 **14 条成功专家轨迹、38 次实际回复**：评估 6、条件改派 2、
依赖阻止删除 2、评论 2、无删除权限 2。目录为：
`/data/zjr/enterprise_kb_agent/rl/datasets/sft_verified_20261117_v1`。
关闭工单成功轨迹为 **0**。这批数据可用于检查 SFT 链路；完整 bootstrap 应先补齐
真实执行成功的关闭工单训练样本。dev、heldout 均不进入 SFT 数据。

## 已完成的行为检查

- 真实 DeepAgents 图：隔离式与 fork 查询范围、范围拒绝、后续执行任务互不污染、
  断连后 checkpoint 恢复；实际 ChatOpenAI 序列化与真实 CRM 工具读取。
- 网页业务链路：知识引用、附件归属、草稿提交、管理员处理、下一轮查询、断连恢复、
  取消与输出合同；使用受控模型回答、真实 HTTP/数据库/原生图执行。
- 实际上游 RPP：轨迹拆分前后的正负优势与 rollout 总权重一致；错误的同轨迹奖励被拒绝。
- 实际缩小 Qwen3 + veRL forward：全批 PG/KL 与逐条微批累积的权重更新数值一致，
  每批恰好一次 optimizer step，没有行数余数裁剪。
- 实际 TRL + PEFT：response mask 排除 prompt，完成一次 LoRA 更新并保存；重新加载
  adapter 与合并成基座后的 logits 一致。这项使用缩小随机 Qwen3 检查训练实现。
- 冻结基线、无专家调用、task 数据运输、禁用动作、错误参数、无关对象与策略初始化
  改变的检查通过。测试日志/小模型产物均在 `/data/zjr/enterprise_kb_agent/rl`。

## 真实模型短测协议

每轮使用冻结源代码快照，业务 fixture 版本 v4：8 条 dev（覆盖 8 类业务），
4 个 train task × 2 次采样，1 次优化器更新，再重测相同 8 条 dev。
GPU 5 运行 trainable specialist / veRL，GPU 6 运行 frozen supervisor；
预算与所有日志、进程身份及退出清理由现有 controller 管理。

由于交接模型输入改变，使用新的真实基线，不继承旧轮次的成绩。
该短测回答端到端路径能否正确训练、checkpoint 是否在真实生成中生效；
8 条开发任务适合定位接口问题，后续效果应使用固定更大开发集对照。

### 启动记录

- `specialists_rollout_contract_short_20261117_r1`：实际 Qwen3-8B 基线阶段暴露上述
  历史消息回写问题，主动停止，0 次更新；轨迹已导出，拥有的 GPU 进程已释放。
- `specialists_rollout_contract_short_20261117_r2`：代码提交 `e0a7328`；GPU 5、6 被
  `edit_reward_probe_20261030` 任务占用，资源预检终止，未加载本轮模型。
- 后续使用新标签 `specialists_rollout_contract_short_20261117_r3` 排队：保持原空余显存
  门槛（训练卡 23500 MiB、冻结模型卡 22000 MiB），最长等卡 120 分钟；取得卡后开始
  独立的 55 分钟运行预算。CPU 等卡期间不加载 CUDA，不重启前两轮。

查看：`bash scripts/agent_rl.sh watch --tag specialists_rollout_contract_short_20261117_r3`。
停止：`bash scripts/agent_rl.sh stop --tag specialists_rollout_contract_short_20261117_r3`。
所有状态、进程身份、源代码快照及日志在
`/data/zjr/enterprise_kb_agent/rl/outputs/specialists_rollout_contract_short_20261117_r3`。

## 常用命令

从 Tracker 执行，所有新增数据、模型、缓存及临时目录均在 `/data/zjr`：

```bash
cd /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent
source scripts/rl_env.sh
export PYTHONPATH="$PWD:$PWD/scripts:/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench/src"

# 重新导出指定训练轮次的成功记录；输出目录必须为新目录。
/data/zjr/enterprise_kb_agent/rl/envs/trainer/bin/python -B -m kb.rl.sft export \
  --run /data/zjr/enterprise_kb_agent/rl/outputs/<completed_run> \
  --output /data/zjr/enterprise_kb_agent/rl/datasets/<new_sft_dataset>

# 补齐关键业务成功样本、检查 family 覆盖后再训练。
CUDA_VISIBLE_DEVICES=5 /data/zjr/enterprise_kb_agent/rl/envs/trainer/bin/python -B -m kb.rl.sft train \
  --dataset /data/zjr/enterprise_kb_agent/rl/datasets/<new_sft_dataset> \
  --output /data/zjr/enterprise_kb_agent/rl/outputs/<sft_run> --steps 32

# 合并约需一个完整 8B 模型的 /data 存储，不写到 home。
/data/zjr/enterprise_kb_agent/rl/envs/trainer/bin/python -B -m kb.rl.sft merge \
  --adapter /data/zjr/enterprise_kb_agent/rl/outputs/<sft_run>/adapter \
  --output /data/zjr/enterprise_kb_agent/rl/models/<sft_policy>

bash scripts/agent_rl.sh launch --tag <sft_then_rl_run> --gpus 5,6 \
  --gpu-wait-minutes 120 \
  --train-role specialists --adv-estimator reinforce_plus_plus \
  --policy-model /data/zjr/enterprise_kb_agent/rl/models/<sft_policy> \
  --steps 4 --batch-size 4 --group-size 2 --val-limit 32 --val-before-train
```

ToolRL 的细粒度工具奖励、AgentFlow 的 planner RL 留作后续方向。本次未修改终局
业务奖励，也未把查询范围控制称为经验学习或新增论文算法。
