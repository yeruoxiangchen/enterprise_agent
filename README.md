# 知页 · 企业本地知识库与问答系统

[简历固定版本](https://github.com/yeruoxiangchen/enterprise_agent/tree/resume-20261116) · [最新代码](https://github.com/yeruoxiangchen/enterprise_agent/tree/main) · [版本与评测记录](docs/versions.md) · [操作演示](docs/demos/README.md)

2026-11-16，工单当前状态视图已接入原生网页执行核心：历史回执保持原样，进度查询按账号权限读取最新状态、版本与备注，并核对生成期间的版本变化。最终定向检查17/17，真实模型三个请求的业务链路通过；应用已重载。实现、首次复测问题及最终结果见[状态新鲜度修复](STATE_FRESHNESS_REPAIR_20261116.md)，小型验证摘要见[本次发布记录](docs/releases/20261116_fresh_state/README.md)。

2026-11-16，在线 `business_native_v1` 已接入 Go 模型网关，继续使用现有 Qwen3-8B。
真实 HTTP 验证完成带引用检索、专业 Agent 创建草稿、成员确认、管理员处理及同会话状态查询；
应用记录、网关请求和账户扣额三者核对一致，临时占用为零。
接入配置与报告见 [网关接入说明](docs/model-gateway.md)。

2026-11-15，原生执行核心已接入网页路径 `business_native_v1`，真实浏览器 4/4、日志/截图 HTTP 验收 2/2、相关工程检查去重 68/68 通过。覆盖带引用排查、跨轮草稿、用户确认、管理员处理、进度查询、取消和跨账号隔离；复用官方 DeepAgents fork/检查点，补齐统一业务网关、原文绑定及网页输出契约。固定 ITSM16 仍保留原 15/16，转派条件遵循问题单独记录。[应用接入、测试、运行及剩余问题](HARNESS_WEB_INTEGRATION_20261115.md)。

2026-11-15，原生 fork 固定对话扩展检查完成：旧 8 例验源复用 8/8、新增 8 例 7/8，合计 15/16，执行异常 0；新增消耗 66,539 Token。唯一失败是主管将“未分配则保持不变”的用户条件在委派时反转，专业 Agent 尝试转派，被业务层拦截，未发生写入。新增答复与真实回执逐项审阅一致；该轮 GPU 服务已释放。后续网页接入见上方新记录。[完整结果与原因](HARNESS_FIXED16_20261115.md)。

2026-11-15，原生 fork 执行核心完成已知故障短测：8 类固定开发对话全部通过，执行异常 0；实际关闭、删除、拒绝、评估与取消的最终答复均已核对。主管保持 Agent，专业 Agent 继承原始上下文；权限账号和操作类型绑定，确认与前置检查走真实回执。参考 5/8、75,744 Token；新版 8/8、123,012 Token；相关行为检查 30/30 通过，本轮 GPU 服务已释放。[实现与完整记录](HARNESS_FORK_HANDOFF_20261115.md)。

2026-11-15，原生 DeepAgents 执行复核与确认续办已接入：直接复用官方 RubricMiddleware/HITL，真实回执验收、最多一次纠正、确认后恢复原调用。此前 16 条固定对话完成；原生动作/状态检查 5/8、复核 6/8，Token 为 75,753/184,249，复核保留为可选配置。[前序结果](HARNESS_NATIVE_REVIEW_20261115.md)。

2026-11-15，完整对照r1在35/132触发混合异常停止，已修正Agent超时与评测故障的分类并接入兼容性续跑，24项检查通过。r2随后启动续跑，后续 V17 和原生诊断结果见 [当天记录](HARNESS_V17_20261115.md)。

2026-11-15，完整66题新版Harness/官方GraphAgent对照入口已准备：共132次，16项入口检查及全目录标准路径预检通过，等待用户启动。共享已校准客户核验、开场与资源预算，输出在/data，6小时总时限，退出释放模型服务。[启动、监视与停止命令](HARNESS_RUNTIME_20261114.md)。

2026-11-14，执行闭环修复已接入共享核心：持久化原始目标和真实回执、模型选择后的原生专业委派、拒绝终态复用、未来用户选择与Agent待办分离。v16进一步接入规范删除顺序与真实检查结果交付；工程检查去重98项通过，核验器12/12正反例通过；最终四项公开开发任务原始评分4/4，执行错误/评测异常/未判定/超时均为0，GPU5已释放。前序r8固定多轮2/2、公开3/4；r9定位了两条错误拒绝结束和一条实际顺序错误，原始结果保留。[修改与复测记录](HARNESS_CLOSURE_REPAIR_20261114.md)、[前序12次逐项审阅](HARNESS_FAILURE_RETEST_20261114.md)。

2026-11-14，新候选`business_deep_v1`已实现并测试：直接接入DeepAgents/LangGraph统一在线协作核心及LangMem结构化记忆，主管保持Agent。实际轨迹自动归纳两条流程，经配对验证后发布；最终三任务开发对照为关闭记忆2/3、加载记忆3/3，Token增加11.01%。46项工程检查、截图/日志到确认建单2/2通过。前序公开短测2通过、1失败、1超时，身份续办及复杂收尾仍需继续诊断。原8768配置保留，新策略通过独立入口选择。[实现与全部结果](HARNESS_IMPLEMENTATION_20261114.md)、[运行与学习命令](HARNESS_RUNTIME_20261114.md)。

最新完整复测已结束：`experience_v2_official_itsm66_20261114_r1`完成132/132，experience v2通过50/66、官方47/66，双方未判定各1；耗时约2小时1分41秒，GPU5已释放。候选较v1净增2题，但Token增加126.36%、任务p95接近翻倍；已定位请求误判、拒绝终态循环、读取义务遗漏和一条空回复异常，下一步保留经验策略做定向修复。[完整结果与逐题审阅](CRM_EXPERIENCE_V2_20261114.md)。

本轮完整测试前：按用户选择保留经验策略，完成experience v2修复：阶段化经验检索、明确查询的原生工具选择、读取顺序账本、参数纠正和待办终态。106项兼容性回归、37项定向检查通过；四条真实模型退步任务复测4/4通过，实际工具执行审阅4/4，最终待办全部为空。[改动、复测与记录](CRM_EXPERIENCE_V2_20261114.md)。

历史strategy1冻结结果：132/132；方案和官方均48/66，候选未判定0、官方1。相对官方Agent Token少36.59%，任务Agent耗时p95从117.47降至84.35秒；旧r2为53/66。当前v2完整结果见上方新记录。[完整结果与逐题审阅](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

截图CPU区域识别修复已应用原8768服务：同8张公开开发样例关键文字4/8→8/8，企业8例的26个字段、知识文档匹配和工单闭环全部保持通过，17项定向检查通过。公开截图API p95为6.71秒；CPU进程加载增加处理时间。[实现与证据](OCR_REPAIR_20261114.md)。

2026-11-14，strategy1扩展开发记录：执行进度驱动的有限恢复、按领域检索的审核经验，以及故障截图／日志→字段核对→知识库排查→确认建单。去重228项检查通过；相同开场的四题真实模型开发对照为baseline2/4、experience4/4，Token少19.09%、模型调用少23.08%。12个合成材料样例及浏览器工单闭环通过，原8768应用已更新，GPU已释放。完整66题结果见上方新记录。[实现、数据与启用方式](CRM_STRATEGY1_20261114.md)。

2026-11-14，主管结构保留的延迟优化perf1已完成：SQLite检查点改为与后续步骤重叠提交，保持FULL和关键业务回执。112次模型调用的两轮受控回放，总墙钟198.66→163.63秒（降低17.63%），额外流程墙钟49.41→14.35秒（降低70.96%）。148项代码回归通过，含强制退出恢复；真实模型四题两组8/8记录齐全，候选4/4、官方参考2/4，双方运行异常及未判定0，本轮GPU1服务已释放。[实现、性能与验证](CRM_LATENCY_PERF1_20261114.md)。

2026-11-13，r2完整ITSM复测已结束：132/132，候选53/66（80.30%）、未判定0，官方参考48/66（72.73%）、未判定1。运行约1小时51分钟，本轮GPU3服务已释放；另各有一题模型调用或上下文预算终止，待逐题深入分析。运行标签`execution_r2_itsm66_20261113_r1`。[结果、异常及输出路径](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

2026-11-13执行层修复r2：142项去重代码回归通过，四题两组真实模型复测完成8/8，r2多Agent4/4、官方参考2/4，双方异常及未判定均为0。备注原文、终态收尾、内部指令拒绝和严格删除顺序均通过；本轮GPU3服务已释放。旧132条结果保持原样，下一步完整66题复测。[修复清单与证据](CRM_EXECUTION_REPAIR_R2_20261113.md)、[逐题结果与效率](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

2026-11-13完整ITSM对照已完成：132/132记录齐全，原PID 534755已退出并释放本轮GPU 3资源。官方原始通过46/66、未判定2；v4通过48/66、未判定1。双方均有效的63题中各通过46、各独赢6，整体有效配对持平。v4平均Token高2.37%、模型调用多58.81%、Agent累计耗时高87.22%；安全类更强，工单和服务时间类回退。已确认裁判上下文/语义误判及执行层具体缺陷，下一步校准后统一重评，再修执行问题。[完整审阅与证据](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

2026-11-13官方基线四题对照已完成：冻结上游原生`GraphAgent`与v4多Agent各4/4通过、未判定0，开场4/4相同；原队列已退出并释放资源。平均Agent Token为8864.25/8469.75，平均Agent累计处理时间6.71/11.73秒。v4总Token少4.45%，Agent处理时间高74.81%，节省主要来自寒暄直接收尾。后续已按用户要求直接扩至完整66项。[结果、证据与下一步](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

2026-11-13校准验收：v4修复后的`calibrated_acceptance_20261113_customer_r3`已结束并释放资源，用户生成4/4、固定业务4/4、公开任务4/4通过，未判定0。额外审核6/8，误拒2、截断0，继续只作诊断。公开四任务平均Agent Token为8471，共27次模型调用。按用户后续要求采用官方参考/v4两组对照；产品默认仍为固定RAG。[当前进度与待办](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)、[协议及原始结果](CALIBRATED_ACCEPTANCE_20261113.md)。

以下按阶段保留历史改动和运行入口，实时进度以上方文档为准。

2026-11-13当前状态：公开任务r3为1通过、3模拟用户验证异常。新增6项反例复现了业务适配中的完成状态、漏办目标和未知结果重试缺陷，已修复；相关CPU回归68项通过。模拟器反馈传递也已纠正，真实模型仍需单独校准。本轮没有启动GPU重跑。[当前问题与下一步](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

2026-11-13后续修复：公开任务单卡r2为2通过、2模拟审核异常。v4现复用上游原生`VerifyUserSimulator`验证，修复实际身份表述的跨轮续办；63项定向测试通过。队列`v4_supervisor_official4_20261113_native_r3`（PID 3938160、GPU 2、24分钟）只重跑同样4任务，记录为共享权重的开发诊断。[原因、证据及监视命令](CRM_NATIVE_VERIFIER_REVIEW_20261113.md)。

2026-11-13：新增 `business_v6_external_v4_supervisor`，采用LangChain官方主管/专业Agent工具模式，统一会话待办与操作回执，直接运行模型/工具循环。多Agent真实模型开发小测4/4通过；恢复审计修复后27项定向代码检查通过。首轮公开任务双卡启动因共享卡显存波动失败，当前改为单卡四任务开发诊断，PID 3872104、GPU 2、24分钟预算；角色共用Qwen3-8B权重，非独立评测。新增运行配置21项验证通过。实现、监控及结果入口见 [CRM_SUPERVISOR_V4_20261113.md](CRM_SUPERVISOR_V4_20261113.md)。

2026-11-13：新增 `business_v6_external_v3`，以结构化请求契约、逐步工具证据及模拟用户忠实性核验处理本轮失败。61项相关短回归通过；真实模型先运行单卡九项开发验收，15分钟预算。实现、测试口径及监视命令见 [CRM_EXECUTION_V3_20261113.md](CRM_EXECUTION_V3_20261113.md)。

2026-11-11 更新 CRM v6 执行适配为 `business_v6_external_v2`。继续使用现有 LangChain `ToolStrategy`、模型与工具中间件，以及 LangGraph/SQLite 执行图。

- 路由使用实际片段编号和工具目录，清理无效的可选背景引用，检查专业节点的工具覆盖。计划修正耗尽后通过同一预算内的通用执行节点继续。
- 写入前核对当前请求及唯一工单。相同逻辑步骤的重复提交复用真实回执，用户明确要求的独立重复步骤分别执行。结果未知的写入保留待核实状态。
- 回执区分已提交、业务拒绝和结果未知。模型可在工具消息正文读取回执编号，最终写入结果由实际回执生成。格式修正复用现有观察结果。
- 两臂复用同一份已验证的模拟用户开场。重复请求且没有新工具证据或工单状态变化时明确记录无进展终止。报告增加正常结束、最大轮数、写入和错误分项。

CPU 验证入口如下，报告写入 `/data/zjr/enterprise_kb_agent/artifacts/harness_v6_repair_20261111/`。

```bash
bash scripts/check_harness_v6.sh
```

以下为 v2 历史对照入口。v3短测已结束，完成4项、3通过1失败；当前按上方v4文档进行小测。

```bash
bash scripts/crm_bench_run.sh launch --harness-version v6 --execution-adapter v2 --tag itsm_v6_repair_20261111 --gpus auto --trials 2 --max-hours 10
bash scripts/crm_bench_run.sh watch --tag itsm_v6_repair_20261111
bash scripts/crm_bench_run.sh stop --tag itsm_v6_repair_20261111
```

2026-11-10 新增 `business_multi_v6` 候选：按任务依赖调度、交接前序回执、限制重复读取、按需有限转派，并实现此前记录的多诉求部分完成反馈。v5保留作为对照。

- `partial_success` 保留已完成回答及引用，分项说明未完成步骤；网页支持“继续未完成步骤”。恢复沿用原任务编号、完成结果及剩余预算，同一请求的回答原位更新。
- 建单异常后先按原 `run_id/source_run` 核对真实数据库；已创建且字段一致的草稿复用，结果未查清时保留待核实状态。部分完成请求中的工单确认须有已核实的完成回执。
- 专业转派最多两次、最多三个不同领域，沿用同一预算；已开始业务写入后通过原记录继续。正常任务沿用首次执行决定，无额外接收模型调用。
- CRM另有 `single_v6/multi_v6`，共享进度与安全控制。记录缓存命中、调用尝试、实际执行及分项结果；12次模型调用/15次工具尝试预算保持原值。

小规模CPU回归（脚本模型、真实LangChain/LangGraph、SQLite与官方ITSM工具，无GPU）：

```bash
bash scripts/check_harness_v6.sh
```

报告位于 `/data/zjr/enterprise_kb_agent/artifacts/harness_v6_cpu_20261110/`。应用启用时设置 `KB_AGENT_POLICY=business_multi_v6`，使用既有业务模型配置与依赖环境。CRM长评测入口为 `bash scripts/crm_bench_run.sh launch --harness-version v6 --tag itsm66_v6_candidate --gpus auto`；候选必须使用新tag。CPU回归与公开任务的模型通过率分别记录，详细实现及验收见 [CRM实验记录](CRM_ITSM_20261109.md)。

2026-11-10 过夜评测入口：增加 `--trials 2 --max-hours 10`，66任务×两次×两臂共264次；时间限制包含GPU等待与模型加载，到点保留完成记录并释放本次模型服务，提前完成则提前释放。两次全部成功按两次重复统计。初次队列在模拟用户停止字段矛盾时退出，旧结果保留；修复后的当前队列 `itsm_v6_overnight_20261110_r2` 使用GPU1、7，按原截止时间将剩余上限设为9.158小时：

```bash
bash scripts/crm_bench_run.sh watch --tag itsm_v6_overnight_20261110_r2
bash scripts/crm_bench_run.sh stop --tag itsm_v6_overnight_20261110_r2
```

时间到但覆盖尚未完成时状态为 `time_budget_complete_review_required`，完整结束为 `complete_review_required`；两种状态均应核对 `resources_released`。当前结果和日志位于 `/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/itsm_v6_overnight_20261110_r2/`。

2026-11-09 新增 [CRM ITSM 公开任务评测](CRM_ITSM_20261109.md)：66 个任务、单 Agent/多 Agent 各 3 次，记录完整任务成功、三次全部成功、响应 p50/p95、Token 和调用次数。复用实际 v5 图与预算机制，以外部工具适配接入官方 ITSM 环境；结果写到 `/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/`。监视命令：`bash scripts/crm_bench_run.sh watch`。

v5完整交付入口：复用原12场景24轮问题、单Agent及v4原始记录，运行v5并统一检查业务契约；随后在独立网页中执行员工咨询/确认、管理员处理、员工查询，输出run_id、截图、视频和轨迹。完成后导出全栈简历v5及指标口径材料，原简历保留。

```bash
bash scripts/run_business.sh delivery launch --gpu auto
bash scripts/run_business.sh delivery status
tail -f /data/zjr/enterprise_kb_agent/artifacts/business_delivery_v5_20261103/final_v1/queue.log
bash scripts/run_business.sh delivery stop
```

产物位于 `/data/zjr/enterprise_kb_agent/artifacts/business_delivery_v5_20261103/final_v1/`；简历物理文件位于 `/data/zjr/shixi/ai_fullstack_v5_20261103/`，入口位于 `shixi/简历新/简历_全栈_v5/`。模型等待/释放策略沿用下述定向回归；故障8项引用同版本已有记录。本次验收以实际状态、字段和规定知识点为准，逐题要求及原始回答分别存档。

`final_v1` 已完成完整24轮和员工→管理员→员工网页验收，GPU 7上的本次模型及临时网页均已释放。最终采用 `final_v1/wording_review_v2/report.md`：对所有保存回答统一接受同义否定表述和“中性标题+描述明确当前环境”，原评分协议与报告保留。单Agent为8/12场景、20/24轮，v4为9/12场景、20/24轮，v5为12/12场景、24/24轮；模型调用分别为28、32、31。这是固定开发场景的业务契约验收。复核没有重新推理。

网页截图、视频、Playwright轨迹位于 `final_v1/demo/browser/`，实际run_id及数据库事件位于 `final_v1/demo/trace_records.json`。新简历的HTML、Word、PDF入口在 `shixi/简历新/简历_全栈_v5/`，物理产物与面试讲述、主张证据账本位于 `/data/zjr/shixi/ai_fullstack_v5_20261103/`。

2026-11-03 新增 `business_multi_v5` 修复候选，保留v4对照：独立处理否定创建与同句咨询，按用户原话保留创建请求中的字段及最新环境更正，同步提交图检查点并复用已完成任务。定向回归复用原6个场景（11轮）和8项故障实验；按实际回答与数据库字段检查，不调用语义评分模型。运行产物位于 `/data/zjr/enterprise_kb_agent/artifacts/business_repair_v5_20261103/`。

```bash
bash scripts/run_business.sh repair launch --gpu auto
bash scripts/run_business.sh repair status
tail -f /data/zjr/enterprise_kb_agent/artifacts/business_repair_v5_20261103/regression_v1/queue.log
bash scripts/run_business.sh repair stop
```

定向回归默认最多等待6小时，从GPU 0/1/6/7中选择满足78%显存预算及额外2048MiB余量的卡；等待阶段不加载模型，失败任务不会自动重启。

`regression_v1` 已结束：同一组定向检查中，旧v4通过3/6场景、7/11轮，新v5通过6/6场景、11/11轮；模型调用16→17。故障检查由7/8到8/8，两个真实模型进程中断均恢复成功，已完成领域未重复执行、已写入草稿仅保留一张；16次并发建单和16次重复确认分别保留一张草稿和一次状态变更。结果为固定开发场景的修复回归，详细记录位于上述目录 `regression_v1/report.md`、`summary.json`，原v4结果保留。本轮模型服务已释放。

2026-11-03 新增业务、多Agent上下文与恢复评测入口。固定12个开发场景、每个版本24轮请求，对照单Agent与业务多Agent v4；按完整会话统计任务完成，另列上下文保持、真实工单状态、8项故障实验、真实问答队列和调用/Token代价。模型评分保留未知；故障记录区分真实模型进程中断和受控模型输出。原23轮、67轮结果保留。

```bash
bash scripts/run_business.sh assessment launch --tag dev_v2_20261103 --manage-gpu 7
bash scripts/run_business.sh assessment status --tag dev_v2_20261103
tail -f /data/zjr/enterprise_kb_agent/artifacts/business_assessment_20261103/dev_v2_20261103/queue.log
bash scripts/run_business.sh assessment stop --tag dev_v2_20261103
```

`--manage-gpu` 在指定卡上顺序启动回答和评分模型，结束时仅释放本次启动的服务；已有两个模型服务均可用时可省略该参数。所有数据库、源码快照、日志和报告位于上述 `/data/zjr` 目录。当前执行进度与结果以该目录 `status.json`、`summary.json` 为准。

2026-10-30 新增业务多 Agent v3：有限意图协调、同域/多轮上下文、用户原文工单与真实回执、122轮分层开发对照及隔离网页验收。实现、评测边界和监视命令见 [BUSINESS_MULTI_V3_20261030.md](BUSINESS_MULTI_V3_20261030.md)。v2代码与结果保留，在线配置未切换。

**当前开发主线：业务多 Agent 企业服务助手（2026-10-30）。**技术支持、制度流程、服务办理三个 Agent 已通过 LangGraph 接入，独立演示网页在8772运行，支持工单草稿确认与管理员处理，详见 [实现与交付](BUSINESS_MULTI_20261030.md)。38项整组回归及修复后12项业务回归通过，真实模型和浏览器工单闭环已打通；首轮17题开发诊断14题正常返回；接口修复后17/17正常返回、领域匹配17/17，但仍有跨域串答与事实错误，详见交付文档。原8768固定RAG继续作为可用基线，记录见 [固定RAG发布](FIXED_RAG_RELEASE_20261030.md)。

2026-10-30 历史实验：固定优先、一次补查候选已完成开发对照，未确认收益，不予上线。实现、证据和结果见 [FIXED_FIRST_20261030.md](FIXED_FIRST_20261030.md)。

2026-10-30 官方 Advanced RAG Agent 候选接入：直接复用 Agent Pack 完整工具循环与收尾机制，见 [AGENTPACK_20261030.md](AGENTPACK_20261030.md)。新增独立环境与开发对照入口，默认在线流程未切换。

新对话交接与最新实验状态见 [HANDOFF_20261029.md](HANDOFF_20261029.md)：多 Agent 四组开发诊断已完成，待复核与修复，尚未发布为在线默认流程。

2026-10-30 逐题结果、协议错误定位和下一步见 [MULTI_AGENT_ANALYSIS_20261030.md](MULTI_AGENT_ANALYSIS_20261030.md)。

2026-10-29 多 Agent / 按需 Skills 扩展：实现、预算、隔离预览和后台对照命令见 [MULTI_AGENT_20261029.md](MULTI_AGENT_20261029.md)。这是一条新增候选流程，效果以独立运行结果为准。

从独立代码仓库组织的可运行应用：管理员上传文档，成员提问、查看原文依据，管理员处理反馈。包含网页、身份与权限、持久化入库队列、检索、模型工具调用 harness、运行记录、测试与部署入口。

应用流程参考 QAnything 的文档问答，以及 ChatterMate 的业务系统组织方式；没有 fork 或复制它们的应用代码。默认问答复用 Haystack 固定检索与生成组件；有界工具循环、官方 Agent Pack 等保留为显式实验候选。

## 快速使用

```bash
cd /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent
bash scripts/run.sh setup
bash scripts/run.sh init
bash scripts/run.sh demo
bash scripts/run.sh start
bash scripts/run.sh status
```

浏览器打开 `http://127.0.0.1:8768`。管理员账号密码保存在 `/data/zjr/enterprise_kb_agent/credentials.txt`，权限为 600；`init` 不覆盖已有账号。查看凭据：

```bash
cat /data/zjr/enterprise_kb_agent/credentials.txt
```

在自己的电脑访问远程服务器时，可以使用 VS Code 端口转发，或者：

```bash
ssh -N -L 8768:127.0.0.1:8768 zjr@你的服务器地址
```

然后在本机浏览器打开相同地址。默认只监听回环地址。

前台运行、停止和查看日志：

```bash
bash scripts/run.sh serve
bash scripts/run.sh stop
tail -f /data/zjr/enterprise_kb_agent/logs/server.log
```

`start` 以独立进程组后台启动；`stop` 校验 PID、启动时间和命令行后，仅终止本项目服务。已完成任务和会话保留。自动重启服务建议采用容器或自行配置 systemd。

## 两种模式

| 模式 | 行为 | 需要的资源 |
|---|---|---|
| `extractive`，默认 | 检索并摘录原文，附带来源；页面明确显示“原文检索” | CPU，无模型、无 API Key |
| `agent` | 模型问答兼容模式；当前默认固定检索、重排、一次生成，输出带引用回答 | chat-completions 兼容模型服务；默认流程不要求自主工具调用 |

原文模式用于立即体验入库、检索、引用与系统流程，不是模型问答效果，也不证明 Agent 的任务成功率。模型模式调用失败会显示失败，不会悄悄替换为原文模式。

### 配置模型

把 `private.env.example` 复制到 `/data/zjr/enterprise_kb_agent/private.env` 并设置权限 600。在文件中填写：

```bash
KB_MODE=agent
KB_LLM_BASE_URL=http://127.0.0.1:8000/v1
KB_LLM_MODEL=你实际部署的模型名称
KB_LLM_API_KEY=如果服务需要则填写
```

默认固定流程需要 chat-completions 接口，正式验证需准确的 Token 计数接口 `KB_TOKENIZE_URL`。旧 Agent 候选另需工具调用支持。当前服务器在线设置由 `/data/zjr/enterprise_kb_agent/app.env` 指定（仅应用命令读取），模型地址和密钥仍沿用原配置；详细配置边界见发布记录。配置后重启：

```bash
bash scripts/run.sh stop
bash scripts/run.sh start
bash scripts/run.sh doctor --live
bash scripts/run.sh evaluate --agent
```

最后一个命令会实际调用模型，在独立目录执行 12 道演示问题；可能耗时，输出逐题进度。只填接口并不能证明该模型的工具调用质量，需要完成此验证及真实资料测试。日常启动应用不会自动申请 GPU；需要时可显式执行 `bash scripts/run.sh model start --gpu 1`。模型服务与网页、评测队列分别管理，不使用其他项目密钥。

2026-10-28 已完成真实 Qwen3-8B 工具调用连通验证，并构建 40 份公开技术资料、100 开发题/300 测试题的冻结候选评测集。详细数据来源、独立人工复核边界、指标、固定 RAG/Agent 对照和后台监视命令见 [EVALUATION.md](EVALUATION.md)。演示题通过不等于真实问答质量达标。

### 可选语义向量检索

默认检索采用 SQLite FTS5/BM25，对中文做双字切分，对英文做词切分。它是关键词检索，不冒称语义检索。

配置 `/embeddings` 兼容服务后启用稠密检索与 BM25 的 RRF 融合：

```bash
KB_EMBED_BASE_URL=http://127.0.0.1:8001/v1
KB_EMBED_MODEL=你实际部署的向量模型
KB_EMBED_API_KEY=如果服务需要则填写
```

修改地址或模型后，在文档页重新索引。索引按地址和模型隔离；失败时保留 BM25，并展示降级提示。当前向量检索采用小规模精确扫描，适用于原型和小知识库；没有宣称支持大规模企业向量库。相似度过滤值和 BM25 覆盖率是初始工程配置，尚未经过业务集调参。

## 已实现功能

- 员工上传截图或日志并发送问题后，FastAPI 将任务入队并唤醒 Worker。Worker 空闲时读取材料、检索知识库，网页通过 SSE 展示进度、识别到的文字和排查建议。影响判断的错误码、故障现象等信息不清时，只追问对应字段。需要建单时，员工可核对并编辑草稿，再确认提交。
- 用户登录、退出、管理员/成员角色、Cookie 会话；管理员创建成员。
- 多知识库；全员可见或仅管理员可见。检索、文档读取、下载和模型工具均执行权限过滤；会话与任务归属单个用户。
- PDF、DOCX、TXT、Markdown、CSV 上传；同库内容去重，持久化队列，解析状态、错误信息、重新索引和删除。
- 文档解析在限时、限内存子进程运行；PDF 保留页码，DOCX 保留段落与表格文本；文本分块含重叠。
- 问答与历史会话、引用原文预览/下载、任务停止、执行记录及回答反馈。
- 模型 harness：工具白名单、参数验证、检索后才能读取片段、最多 6 轮模型请求/12 次工具调用、来源编号校验、连接超时与有限重试。
- 默认 `bounded_v2` 策略按当前进度开放工具：搜索后先读证据，最多两个不同查询，重复查询和已读片段不重复执行；预留最后两轮用于提交答案和修正格式。
- 已读完整原文在上下文中各保留一份，加入上下文前准确计算 Token 预算，不能容纳的新证据明确拒绝加入；当前问题和已读证据不被截断。完整模型和工具响应仍保存在事件轨迹中。
- 持久化证据状态与来源；服务重启后重新排队未完成任务，从完整轮次检查点恢复。已完成答案和状态原子写入，防止重复落库。不同策略的旧检查点明确拒绝混用。
- REST API 与执行事件 SSE；网页当前使用轮询更新状态，答案生成完成后显示，不模拟逐 token 流式输出。
- 运行脚本、Dockerfile/Compose、接口和 harness 测试、浏览器 E2E、独立演示评估命令。

## 文件与运行数据

```text
enterprise_kb_agent/
  kb/app.py          API、身份与权限边界
  kb/agent.py        自编写 Agent harness、工具和引用校验
  kb/bounded.py      v2 工具阶段限制、证据去重和上下文准入
  kb/retrieval.py    BM25、可选向量/RRF 检索
  kb/ingestion.py    上传与索引任务
  kb/extract.py      受资源约束的文档解析
  kb/worker.py       入库与问答后台任务
  kb/db.py           SQLite 状态与事件存储
  kb/web/            中文响应式网页
  kb/cli.py          初始化、启动、监视与评估
  kb/context.py      总 Token 预算与完整工具消息保护
  kb/benchmark/      证据标注、冻结数据、批量推理、评分与配对报告
  tests/            自动化验证
  scripts/          运行与浏览器验证
```

数据库、上传文件、运行环境、缓存、测试和截图全部位于 `/data/zjr/enterprise_kb_agent/`。`run.sh` 设置 TMPDIR、TORCH_HOME、HF_HOME、HF_HUB_CACHE、TORCH_EXTENSIONS_DIR、MPLCONFIGDIR 等；Tracker 仅存源码和小文档。

## 验证命令

```bash
bash scripts/run.sh test
bash scripts/run.sh evaluate
bash scripts/run.sh browser-setup
bash scripts/run.sh browser-test
```

接口与 harness 测试使用临时隔离数据库，包括越权、来源伪造、预算终止、取消、恢复和模型错误。模型行为在单元测试中由脚本化响应驱动，这是协议/工程验证，不是实际模型成绩。

浏览器 E2E 自动创建独立测试服务，验证登录、建库、上传、入库、提问、引用、反馈、会话恢复及窄屏显示，报告和截图写入 `/data/.../artifacts/browser-*/`。`evaluate` 使用代码内自编写的虚构示例资料，不能当作真实企业准确率。

## Docker

先准备已有运行目录，并导出与目录所有者一致的 UID/GID：

```bash
export KB_UID=$(id -u)
export KB_GID=$(id -g)
docker compose build
docker compose run --rm knowledge-desk python -m kb.cli init
docker compose run --rm knowledge-desk python -m kb.cli demo
docker compose up -d
```

不要同时启动本地服务和指向同一数据库的容器服务。容器内的模型地址需在容器网络中可访问；不能直接把宿主机的回环地址当作容器外服务地址。

## 当前边界

- 当前是单组织、小规模、单应用进程版本；没有 SSO、复杂部门权限、多实例任务队列或生产高可用承诺。
- 扫描 PDF、图片 OCR、旧版 `.doc`、复杂版面表格还未接入；不支持的文档明确报错。
- 来源校验验证“编号存在、片段读取过、权限有效、行内引用一致”；它不能自动证明回答的每个结论都受原文支持。
- 已接通单卡 Qwen3-8B 并验证工具协议；实际业务问答质量和任务完成率仍需完整评测与独立复核。语义向量服务尚未部署。
- 文档只作为数据，不开放模型执行 Python、Shell 或任意写入工具；本版工具只读知识。
- 反馈用于管理员审阅，尚未用于 SFT/RL。无模型训练任务。

后续依据开发集配对结果和独立复核处理检索漏召回、回答缺乏依据与跨文档问题，再接入向量服务形成检索策略对照。冻结测试集不用于调参。

## 参考

- 产品流程参考：[QAnything](https://github.com/netease-youdao/QAnything/tree/qanything-v2)、[ChatterMate](https://github.com/chattermate/chattermate.chat)。
- 实际依赖：FastAPI、Uvicorn、HTTPX、SQLite FTS5、pypdf、python-docx；测试使用 pytest、Playwright。
- 接口与解析依据：[FastAPI 文件上传](https://fastapi.tiangolo.com/tutorial/request-files/)、[SQLite FTS5](https://www.sqlite.org/fts5.html)、[pypdf 文本提取](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。
