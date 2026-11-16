# 统一多 Agent Harness 与工作流记忆

日期：2026-11-14。在线策略名：`business_deep_v1`。

最新共享核心为 `ordered-receipt-grounded-delivery-v16`，原始目标、续办、写入回执与拒绝终态持久化，实际回执决定完成，删除按规范安全顺序执行。最终98项工程检查和12例核验器校准通过，四题公开开发原始评分4/4；详细开销、附加裁判分歧及历史失败见[执行闭环修复记录](HARNESS_CLOSURE_REPAIR_20261114.md)。下方初版验收记录按历史保留。

## 实现

主管 Agent 负责理解目标、规划、委派、澄清及收尾。网页业务和公开 ITSM 适配器调用同一个 `kb/harness/engine.py::DeepHarness`，业务端口分别提供当前账户可访问的知识/工单工具、上游 ITSM 的真实工具。

| 能力 | 实际实现与来源 |
|---|---|
| 子 Agent 与交接 | 直接使用 DeepAgents 0.7.38 `SubAgentMiddleware`；原生隔离上下文和结构化 `TaskResult` |
| 任务计划、摘要与材料 | LangChain 1.5.0 `TodoListMiddleware`、`SummarizationMiddleware`；DeepAgents `FilesystemMiddleware` 只提供给材料角色 |
| 多诉求交付复核 | 直接使用 DeepAgents `RubricMiddleware`，根据原始用户目标检查完成情况并有限修订；真实缺失信息与业务拒绝结束当前轮 |
| 持久化 | LangGraph 1.2.22 / SQLite 检查点；写入回执单独持久化，先记录结果未知再进入业务后端 |
| 停滞处理 | 借鉴 Magentic-One 的任务/进度分离：同一角色重复提出相同动作且业务状态未变时，由模型阅读真实账本作进度审阅，交回主管重规划或收尾 |
| 轨迹归纳 | 直接调用 LangMem 0.0.30 `create_memory_manager`；采用 AWM 的执行轨迹→参数化流程归纳机制 |
| 经验维护与发布 | 本项目实现候选/版本/配对验证/发布流程，借鉴 ACE 的反馈修订和有害条目淘汰机制 |

上游代码与论文对应关系见 [调研与设计](HARNESS_REDESIGN_20261114.md)。AWM、ACE、Magentic-One 是机制适配；直接调用的运行时是 DeepAgents、LangChain、LangGraph 与 LangMem。

## 执行与恢复

- 保留上游角色的**完整工具集合及重叠工具归属**。查询和评论等工具可以同时属于多个专业角色。
- 业务执行使用原生自动工具选择；执行结束后，在 LangChain `after_agent` 中单独调用 Schema 输出生成状态与引用，保留原回答。避免本地模型在“业务工具/最终结果工具”联合强制选择时循环。
- 子任务只接收当前任务所需的事实和原文；工具 Schema、鉴权和真实业务结果由端口及共享工具网关处理。
- 本轮工具事实通过系统上下文共享给隔离子 Agent，用户问题保持为用户消息。大材料保留原文文件及可追溯事实投影。
- 相同请求的相同已提交写入复用回执。响应丢失的写入先对账；未知结果保留在持久化账本中。
- 检查点恢复保留请求编号、已完成动作和已消耗的模型/工具预算。模型错误后的网页恢复继续更新同一条回答。
- 权限标签来自后端能力目录，结果引用使用实际回执 ID 枚举约束。网页的引用校验失败会通过 SDK 返回模型修订，实际来源仍由 ACL 复查。
- 材料通过现有 OCR/日志处理、ACL 和检索接口接入。工单草稿仍由用户在已有 UI 确认提交。
- 上传材料通过 `/api/chat.incident_ids` 与请求绑定，保存类型、所有权与内容摘要；读材料后才能创建依赖它的草稿。网页“用于知识库排查”传递同一结构化附件，不再仅靠问题中的 UUID 区分材料和工单。
- 回答中的资料原文摘录由真实检索回执和 ACL 复核生成，正文建议仍由 Agent 撰写。草稿保存模型叙述及独立的材料核验字段，支持中断后的同内容对账。
- 停止评测时设置取消标记并等待执行线程退出，再释放本轮拥有的模型服务。

## 工作流记忆生命周期

`可见对话、实际工具结果及标量反馈 → LangMem 归纳/更新 → 参数化 WorkflowCard → 工具与来源检查 → 新任务上的有/无记忆配对执行 → 启用或淘汰 → 不可变版本发布`。

记录包含适用条件、参数槽位、步骤、依赖、失败分支、停止条件和来源轨迹。公开任务的预期答案、`expected_actions` 等评分字段不进入归纳输入。真实工具 Schema 用于检查流程参数。

新条目首先进入候选库。验证任务与来源轨迹分离、每次重置业务环境；成功数提高或成功数相同且 Token 降低，同时无逐题退步/政策违规时，才成为 active。修改已发布条目生成新 revision 并重新验证。有害条目 retired。发布命令只接受存在 active 条目的库，并拒绝覆盖旧 release。

启用还要求验证过程中实际将该条目送入模型。运行前按工具兼容性和任务检索少量流程，主管及匹配的专业 Agent 接收参数化内容和执行范围；事件区分检索命中和实际模型输入。库中存在条目、模型却未接收它时，统计变化不算经验收益。

下一轮归纳会读取相关条目的实际配对反馈，包含成功数、退步数、政策违规和 Token 开销，再交给 LangMem 维护。每轮最多装入3条相关记录、6000字符，控制增量整理的上下文成本。

槽位、依赖顺序等跨字段约束放在 `WorkflowCard` 的 Pydantic 验证中，错误会进入 LangMem/Trustcall 原生 JSONPatch 修复循环；接入目录的工具参数仍独立执行 Schema 检查。

## 运行

代码目录为当前项目；运行环境、模型和所有产物位于 `/data/zjr/enterprise_kb_agent/`。

```bash
# 独立依赖环境；不覆盖旧业务环境
bash scripts/harness.sh setup

# 原生 SDK、应用 HTTP/DB、持久化和记忆生命周期检查
bash scripts/harness.sh test tests/test_deep_harness.py tests/test_deep_application.py

# 本地真实模型：固定业务契约、公开开发子集、轨迹归纳及配对验证
# 每次使用新 tag；失败不会自动重启
bash scripts/harness.sh launch --tag deep_harness_new_run --gpus 5 --public --simulator-review audited
bash scripts/harness.sh status --tag deep_harness_new_run
bash scripts/harness.sh watch --tag deep_harness_new_run
bash scripts/harness.sh stop --tag deep_harness_new_run

# 当前执行修复的开发复测：两项固定多轮+四项公开任务，关闭记忆
bash scripts/harness.sh launch --tag closure_retest_new_tag --gpus 5,1 \
  --retest --trials 1 --no-goal-review --simulator-review audited \
  --agent-step-timeout 180 --task-timeout 600
```

### 完整66题官方对照（2026-11-15）

新增 `--full-itsm`，直接使用本项目固定的官方 `GraphAgent` 适配器和当前 `DeepCRMAdapter`。
66个公开任务、两组各一次，共132次；同一Qwen3-8B服务、8192上下文、1400输出、
每轮40次模型调用和48次规范业务工具尝试上限。官方路由调用计入模型调用，
与候选一致，业务工具预算不计路由；旧参考适配器的默认计数方式保留。
两组共享已校准的客户核验与缓存开场，按任务交替先运行的方案；30轮、单步180秒、每题600秒。
工作流记忆及可选目标复核关闭，原始评分与本地预算终止分类分别保存。

入口16项检查通过，含真实官方GraphAgent的预算与工具执行、开场配对及统计、CLI参数传递、
超时分类和到期清理；全部66题的冻结校验及标准工具执行路径预检通过。
这些是入口检查，当前尚未启动132次正式评测。

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh launch \
  --full-itsm --tag harness_vs_official_itsm66_20261115_r1 --gpus auto --max-hours 6

bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh watch \
  --tag harness_vs_official_itsm66_20261115_r1

bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh stop \
  --tag harness_vs_official_itsm66_20261115_r1
```

源代码快照、协议、逐题结果、`summary.json`、`report.md`、`queue.log`和模型日志均在
`/data/zjr/enterprise_kb_agent/artifacts/harness_runs/harness_vs_official_itsm66_20261115_r1/`。
总时限包含GPU等待和模型加载，正常完成或到期均保留结果并释放本轮拥有的模型；失败不自动重启。
固定一张卡可将 `--gpus auto` 替换为卡号。已启动的tag不覆盖；后续新运行需换tag。
测试报告：`/data/zjr/enterprise_kb_agent/artifacts/harness_full_comparison_20261115/entry-tests-final.xml`。

#### 2026-11-15：r1中断与r2续跑

r1在35/132时主动停止，原保护逻辑将“任何未判定”都计为评测基础设施异常。
最后三条实际为一条模拟用户校验异常和两条Agent单步180秒超时，触发了混合类型的三连停止。
Agent超时在系统指标中计为失败，原始官方未判定及轨迹保留；保护停止仅累计连续的模拟用户/裁判基础设施异常。

新增 `--resume-from`，只允许从已停止并释放资源的同协议运行继承完整结果。
校验模型、用户模拟和评分的源文件、构建代码及执行参数一致；复制原始开场，保留结果来源与哈希，
旧结果保持原样，已完成的任务不重跑。参数不兼容或执行/评测代码变化时拒绝继承。
24项入口/续跑检查通过；真实r1续跑预检确认继承35条、剩余97条。

当前r2队列PID4033131，GPU5，模型显存预算78%（约19GiB）；沿用原实验截止时间，
启动时剩余5.1812小时，模型加载及等待继续计入。命令入口：

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh watch \
  --tag harness_vs_official_itsm66_20261115_r2

bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/harness.sh stop \
  --tag harness_vs_official_itsm66_20261115_r2
```

新结果目录 `/data/zjr/enterprise_kb_agent/artifacts/harness_runs/harness_vs_official_itsm66_20261115_r2/`。
`resume-plan.json`记录来源及兼容性，`continuation-launch.json`保存实际启动参数，`queue.log`保存日志。
原r1三条Agent超时、两条模拟用户异常，以及提前结束多阶段任务的评测质量问题，
均在 `/data/zjr/enterprise_kb_agent/artifacts/harness_full_comparison_20261115/r1-interruption-review.json` 中单列；本轮续跑保持原执行与评测配置。

### 网页候选入口

候选网页服务可在独立端口运行，显式提供本地模型地址和模型名：

```bash
KB_LLM_BASE_URL=http://127.0.0.1:PORT/v1 \
KB_LLM_MODEL=crm-qwen3-8b \
KB_TOKENIZE_URL=http://127.0.0.1:PORT/tokenize \
KB_HARNESS_GOAL_REVIEW=0 \
bash scripts/harness.sh serve
```

默认端口 18768。服务使用现有页面与业务接口，新策略通过 `KB_AGENT_POLICY=business_deep_v1` 选择。工作流版本通过 `KB_SUPERVISOR_STRATEGY=experience` 与 `KB_HARNESS_WORKFLOW_STORE=/data/zjr/enterprise_kb_agent/.../release-vN.sqlite3` 选择。

独立 `serve` 入口默认关闭模型目标复核，本地 8B 开发运行曾把已完成读取误判为遗漏并触发重复处理；可显式设置 `KB_HARNESS_GOAL_REVIEW=1` 对照。离线验收器的原配置保留复核开关，结果记录其使用范围。环境由 `requirements.harness.lock` 固定。开发迭代可通过 `--service-manifest /data/zjr/enterprise_kb_agent/.../services.json` 复用已有本地模型，借用运行不会关闭其服务；生命周期由原拥有者管理。

```bash
# 导出共享 Harness 的实际执行记录
bash scripts/harness.sh memory export --format harness \
  --input /data/zjr/enterprise_kb_agent/PATH_TO_CONVERSATION_WORKSPACE \
  --output /data/zjr/enterprise_kb_agent/artifacts/development-episodes.jsonl

# 归纳应用工具流程；CRM 使用 --catalog crm
bash scripts/harness.sh memory induce --catalog application \
  --input /data/zjr/enterprise_kb_agent/artifacts/development-episodes.jsonl \
  --store /data/zjr/enterprise_kb_agent/artifacts/workflows.sqlite3 \
  --url http://127.0.0.1:PORT/v1

bash scripts/harness.sh memory status \
  --store /data/zjr/enterprise_kb_agent/artifacts/workflows.sqlite3
```

归纳命令只产生候选。实际配对执行通过 `WorkflowStore.validate(..., run_case)` 接入业务验收器；`scripts/harness_assessment.py` 提供 ITSM 的完整使用示例。验证通过后可发布：

```bash
bash scripts/harness.sh memory publish \
  --store /data/zjr/enterprise_kb_agent/artifacts/workflows.sqlite3 \
  --output /data/zjr/enterprise_kb_agent/artifacts/releases/workflows-v1.sqlite3
```

发布后可验证多条流程共同使用时的真实在线检索：

```bash
bash scripts/harness.sh run scripts/harness_release_check.py \
  --service-manifest /data/zjr/enterprise_kb_agent/PATH_TO_EXISTING_RUN/services.json \
  --store /data/zjr/enterprise_kb_agent/artifacts/releases/workflows-v1.sqlite3 \
  --output /data/zjr/enterprise_kb_agent/artifacts/release-check-v1
```

该命令借用已有本地模型，每题重新创建业务环境，执行相同核心的有/无记忆对照。输入和输出路径保留在 `/data`，输出目录必须全新；它不负责关闭借用的服务。

## 验收记录

实现与测试已收尾：46 项工程检查通过；实际截图/日志到确认建单 2/2 通过；发布工作流的开发对照由 2/3 提至 3/3，Token 增加 11.01%。前序固定业务契约为 6/8，公开短测为 2 通过、1 失败、1 超时。完整过程、失败记录和口径见[本轮实现与测试](HARNESS_IMPLEMENTATION_20261114.md)。

所有执行记录保留在 `/data/zjr/enterprise_kb_agent/artifacts/harness_runs/`；依赖锁定与应用验收在 `/data/zjr/enterprise_kb_agent/artifacts/harness_impl_20261114/`。
