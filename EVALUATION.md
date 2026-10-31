# 评测协议与运行说明

本轮先评估“研发团队 Git 技术支持知识库”这一明确场景。数据与产物均在 `/data/zjr/enterprise_kb_agent/`，应用线上数据库不参与评测。没有训练任务。

## 数据与标注

- 数据目录：`/data/zjr/enterprise_kb_agent/datasets/git_support_zh_v1/`。
- 40 份 [Pro Git 中文文档](https://github.com/progit/progit2-zh)，固定提交 `e5c1da8a64a718f5e53faa4d863f0f5383604ed8`。其中 36 份用于命题，4 份作干扰资料。原文、下载来源、许可证、变换说明、文本摘要全部保留。
- 原作者为 Scott Chacon、Ben Straub 及中文翻译贡献者；原文使用 CC BY-NC-SA 3.0。当前用于研究评测，不能把这份公开代理语料直接描述为真实企业私有资料。
- 文本转换只移除图像/include/锚点/indexterm 指令行，规范化空白；不改写原文事实。图表、图片和外部 include 不在本次解析范围内。
- 100 个题族，400 个变体：开发 25 题族/100 题，测试 75 题族/300 题。
- 每题族含两种编写的问法，以及场景包装、附带历史上下文的变体。它们是相关样本，不是 400 个独立真实用户工单；历史变体也不是完整多轮用户模拟。
- 72 个单证据题族、8 个多证据题族、8 个资料不足题族、4 个澄清题族、4 个权限题族、4 个文档注入题族。数值、单位、版本适用说明体现在部分事实题中。
- 权限和文档注入是明确标记的合成工程控制组。虚构识别码不是真实密钥。控制文档与原始公开资料分开管理。
- 问题由代理依据文档编写，原文引用和字符位置经过程序校验；**尚无独立人工复核，当前是冻结候选集**，不是官方基准或人工金标集。

主要文件：

| 文件 | 用途 |
|---|---|
| `documents.jsonl`、`corpus/`、`raw/` | 唯一允许进入公共检索索引的原文资料、来源和授权信息 |
| `catalog.jsonl`、`authoring_spec.json` | 题族、参考答案、证据及构造过程；不进入公共索引 |
| `dev.jsonl`、`test.jsonl` | 推理问题、历史、身份、标准动作、参考要点、证据、禁止输出项 |
| `validation.json` | 格式、证据、摘要与划分校验结果 |
| `freeze.json` | 固定语料和标签文件摘要，防止实验间悄悄改题 |
| `annotation_review.csv` | 带原文证据的逐题复核表；reviewer/verdict/correction 留空，未虚构人工审核 |

证据由 `document_id + document_sha256 + start + end + quote` 标识，独立于当前 chunk 划分。同一源文档用于命题时只属于一个问题划分，同一题族所有变体保持同一划分；多证据题不能跨开发/测试源文档组。测试文档本身存在于公共知识库，这是检索评测的必要条件；测试问题、答案和评分要求不提供给普通推理分支。不同章节仍有主题相关性，也可能出现在基座预训练中，不能据此宣称测试了未知企业事实的泛化能力。

冻结后的标签纠正需要新数据版本。调试 prompt、工具反馈、检索参数只能使用演示资料或开发集。执行测试集需显式 `--ack-frozen-test`。

## 对照与指标

| 分支 | 行为 | 当前验证情况 |
|---|---|---|
| `retrieval-bm25` | 当前真实 BM25，返回前 10 个片段，不调用模型 | 100 道开发题 CPU 基线已完成 |
| `fixed-bm25` | 一次 BM25 检索，最多读取前 5 个片段，一次模型生成，仅可调用 finish | 已进行真实模型小批验证；完整开发集由队列运行 |
| `agent-bm25` | 相同 BM25，模型决定检索、读证据和重新检索 | 模型协议通过演示测试；完整开发集由队列运行 |
| `fixed-hybrid` / `agent-hybrid` | 接入真实向量服务后以 RRF 融合，对照固定流程和 Agent | 代码可运行，尚未部署向量模型、没有这两项成绩 |
| `oracle` | 将标准证据对应片段提供给模型，诊断生成瓶颈 | 仅诊断入口；普通流程不能使用，不称作严格理论上界 |

固定 RAG 与 Agent 使用同一模型、精度、语料、问题和单次上下文/输出预算；Agent 最多 6 轮模型请求、12 次工具调用、8 条已读证据。固定 RAG 一次生成，没有隐藏的格式纠错重试。两者**实际计算量不同**，需要连同调用成本报告，不能直接声称同 GPU 时间或 FLOPs。

当前 BM25 参数保留初版工程配置：中文双字切分、英文词切分、候选最多 40、词覆盖率过滤 0.12。未根据测试成绩调参。混合分支要求真实向量服务和索引，正式评测禁止静默降级为 BM25；线上应用仍可以带提示地降级。

指标分开记录：

1. **检索**：每题标准证据的平均 Recall@1/3/5/10、完整证据集合命中率。一个证据单元至少 95% 的字符区间被前 k 个片段的并集覆盖才计命中，重叠不能重复计数。`mrr_at_10` 是首次累计获取任一完整证据单元的 reciprocal rank；`evidence_ndcg_at_10_diagnostic` 是按新增证据单元计收益的诊断量，均不是直接复现 BEIR 的段落 qrels 分数。
2. **程序诊断**：是否按要求回答/拒答/澄清、错误率、来源编号与行内标记一致性、标准事实字符串命中、引用原文与标准证据的覆盖、权限识别码或注入目标泄露。
3. **独立语义评分**：答案正确与完整、引用原文是否真正支持结论、引用是否支持相邻事实。这些不能用字符串命中或“存在 S1”替代。未评分时明确记为 null。
4. **成本**：每题模型/工具调用数、输入输出 tokens、实际耗时和完整模型/工具轨迹。失败、超时、预算耗尽均留在分母中，不删除难题。

拒答率仅在有答案题中统计；不应回答却回答的比例仅在资料不足/应澄清题中统计。配对比较要求两个完整运行拥有完全相同的问题 ID、数据版本和代码版本。95% bootstrap 区间按题族重采样；同一书的不同题族仍可能相关，小样本区间不能解释为对所有企业业务的保证。

当前是共享 GPU 和共享服务器，因此耗时是观测值，不是专用卡吞吐基准。最终业务评估仍需真实目标领域文档、独立审核和更广的数据。

评测层次参考 [BEIR](https://github.com/beir-cellar/beir) 的检索评估和 [ALCE](https://github.com/princeton-nlp/ALCE) 将答案与引用质量分开评估的方法；没有声称复现官方数据集分数。

## 单卡模型与预算

```bash
cd /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent
bash scripts/run.sh model status
# 需要启动且资源满足时，显式执行；不会自动抢卡
bash scripts/run.sh model start --gpu 1
```

当前使用本地已有 Qwen3-8B、BF16、vLLM 0.11.0，单并发，8192 上下文、eager 模式、0.78 显存比例。复用 `/data/zjr/deepsearch_rl/venv` 只读依赖，不修改其他项目环境。既有权重的物理旧路径记录在 `/data/zjr/enterprise_kb_agent/model_service.json`，不重复下载、不搬迁共享模型。新缓存、日志、评测产物均在本项目 `/data` 下。

服务：`http://127.0.0.1:18768/v1`；模型名 `kb-qwen3-8b`。自动写出的 `model.env` 提供接口，不自动切换或重启原有网页服务。`private.env` 的明确设置有优先权。

每次模型请求前通过服务 `/tokenize` 计算包含 chat template 和工具 schema 的真实输入 token 数，预留 1400 输出 tokens 和 256 安全余量。超预算只移除本轮问题之前的旧对话组；不截断当前问题、证据或工具调用/结果对。仍放不下就显式失败。没有 `/tokenize` 的普通应用接口可用 UTF-8 字节保守估计，但正式模型评测要求准确计数接口。

模型使用 `tool_choice=required`；Qwen3 请求关闭 thinking。最终仍执行原有引用/权限校验，没有把模型的普通文本当成有效工具调用，也没有自动替它补造引用。

## 运行、监视与恢复

所有命令均从项目源码目录执行：

```bash
# 数据校验，不重新生成或覆盖冻结文件
bash scripts/run.sh benchmark validate

# CPU 检索基线：新代码或参数应使用新的输出目录
bash scripts/run.sh benchmark run --arm retrieval-bm25 --split dev \
  --output /data/zjr/enterprise_kb_agent/artifacts/git_support_v1/bm25_dev_new

# 串行运行 100 道 fixed-bm25，再跑相同 100 道 agent-bm25，最后输出配对报告
bash scripts/run.sh eval-queue launch
bash scripts/run.sh eval-queue status
watch -n 10 bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/run.sh eval-queue status
```

队列默认目录：`/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_compare_20261028/`。每题落盘，保存配置、源码快照、语料摘要、模型小型元数据摘要；不重复扫描或哈希大权重。停止后同命令可恢复；代码/模型/预算/数据改变会拒绝混入已有运行，应通过 `--output` 新建实验目录。已完成但出错的题不会被悄悄重试筛成最好结果。

```bash
# 仅停止评测队列，不影响模型服务或其他项目
bash scripts/run.sh eval-queue stop
# 单独释放本项目的模型服务显存
bash scripts/run.sh model stop
```

单分支运行与查看：

```bash
bash scripts/run.sh benchmark run --arm agent-bm25 --split dev \
  --output /data/zjr/enterprise_kb_agent/artifacts/git_support_v1/agent_dev_new
bash scripts/run.sh benchmark status \
  --output /data/zjr/enterprise_kb_agent/artifacts/git_support_v1/agent_dev_new
```

`report.md/report.json` 是汇总；`predictions.jsonl` 保留逐题结果与完整轨迹；`grading.jsonl` 仅在推理结束后加入标准答案和证据，供独立评分，不暴露给回答模型。

## 独立评分

可以直接复核 `grading.jsonl`，填写 `id/correctness/faithfulness/citation_support/rationale/reviewer/reviewer_type`。三项分数取 0 或 1，`reviewer_type` 必须如实填写 human 或 model。不得把代理自动标注写成人工结果。

```bash
bash scripts/run.sh benchmark score-grades --run-dir <某个完整评测目录> \
  --grades <填完的grades.jsonl> --output <新的评分报告目录>
```

导入要求逐题覆盖，包括失败题；缺失、重复、失败题获得通过分数均拒绝。也提供独立模型评审入口，需显式配置 `KB_JUDGE_BASE_URL/KB_JUDGE_MODEL/KB_JUDGE_API_KEY`；未配置时不会调用外部服务：

```bash
bash scripts/run.sh benchmark judge --run-dir <某个完整评测目录> --output <独立评审目录>
```

Judge 不接收分支标签或回答模型名，保留评分理由；仍需要人工抽查与校准。同名回答模型不能直接自评。当前未部署或调用 Judge，不能把自动诊断报告当成最终语义准确率。
# v3 开发对照（2026-10-28）

入口：`bash scripts/run_eval_v3_20261028.sh launch`，进度用同一脚本 `status`；`stop` 只停止本队列，保留独立的模型服务。默认输出 `/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/`。`report` 只重做 CPU 汇总（配置了 Judge 时还会恢复语义评分），不会重跑答案生成。恢复要求原代码、模型与数据配置不变；变更实现须用新目录。

报告代码修复后可单独执行 `report`：核验每组原推理指纹、划分、题目、数据收据和完整记录，保留原 `protocol.json` 与全部推理文件；当前汇总代码及输入摘要另记在 `reporting_manifest.json`，生成后再次校验输入未改。此例外仅针对已有结果的汇总，`launch/work` 的推理恢复仍要求原代码指纹完全一致。

全部使用冻结的 100 题开发集，每题族四个变体。正常推理只接收问题、会话和授权文档，不接收参考答案、标准证据或复核标签。冻结的 300 题测试集继续保留。

| 对照 | 左组 | 右组 | 变化 |
|---|---|---|---|
| 检索 | retrieval-bm25 | retrieval-rerank | BM25 前20候选经 CPU BGE 重排，报告前1/3/5/10覆盖 |
| 重排 | fixed-bm25 | fixed-rerank | 固定检索、读取前5和一次生成，仅增加重排 |
| 工具消息 | agent-bm25 | agent-native-bm25 | 相同 bounded 去重、阶段控制和预算，替换下一轮消息组织 |
| 取证流程 | fixed-bm25 | agent-evidence-bm25 | 初次主机自动检索/读证据；模型最多申请一次明确缺口的补查 |
| 重排后流程 | fixed-rerank | agent-evidence-rerank | 同上，检索都启用重排 |
| 新流程重排 | agent-evidence-bm25 | agent-evidence-rerank | 新流程下比较重排收益 |

消息对照保留最近一轮真实 `assistant.tool_calls` 与所有对应 `role=tool/tool_call_id`，此前唯一完整证据合并到最后一个工具响应；不伪造工具调用、不让模型摘要替换原文。它同时改变了角色和上一轮调用参数的呈现，不能解读成纯粹只改一个 role 字段的实验。原 v2 的规范化 user 状态消息作为对照保留。

`evidence_first_v3` 最多3次模型调用：首轮可直接 finish 或申请 `need_search(gap, query, checked_sources)`；补查后只能 finish，最后一轮用于格式修正。初次读取最多5段、合计最多8段，所有数据获取给 finish 留工具预算；按真实 tokenizer 准入完整片段。重复查询被拒绝，不能将无答案的错误任务自动记成成功。来源/行内引用/权限验证沿用原有实现。缺少企业事实或用户必要条件时由模型拒答或澄清；这些提示并不保证其语义判断必然正确。

重排采用 [BAAI/bge-reranker-base](https://huggingface.co/BAAI/bge-reranker-base)，遵循官方 Transformers cross-encoder 用法，首次下载固定 revision 到项目 `/data`。单个 benchmark 进程保持一个 CPU worker，4线程、float32、batch4、512 token pair 截断；截断只用于排序，提交给问答模型的证据仍为完整片段。候选在权限过滤后才交给重排器。服务失败时直接记录失败，禁止静默回退 BM25；未移植其他项目的重排阈值，也没有根据本开发集调分数阈值。首题耗时含模型加载，不能当稳定吞吐。

标签复核独立输出到 `dev_label_review_v3_20261028/`：25题族100问法已由助手检查，其中14条修改事实关键词要求/同义词，其余保留并增加逐题族语义判据。标注仍未独立人工确认，且复核发生在查看 v2 错误之后。原冻结标签、预测、分数不变。新报告同时保留原标签诊断和 `label_review/` 下的修订诊断。

离线语义评审拆分 correctness（题意、适用条件、完整性）、faithfulness（原子事实是否由引用推出）、citation_support（相邻引用是否支撑）。Judge 需返回原子事实、来源编号、原文摘录、支持判断及理由；程序验证摘录确实存在于已引用原文，但蕴含关系仍是模型判断，不等于人工金标。空事实的合理拒答不强求引用。无配置时只导出 `grading.jsonl`，不会用回答模型冒充独立 Judge。

配置独立评审后，可对已有答案运行（无须重新生成答案）：

```bash
export KB_JUDGE_BASE_URL='你的独立模型服务/v1'
export KB_JUDGE_MODEL='独立模型名称'
bash scripts/run.sh benchmark judge \
  --run-dir /data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/agent-evidence-bm25 \
  --review-dir /data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/label_review \
  --output /data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_ablation_v3_20261028/semantic/agent-evidence-bm25
```

别名检查只能阻止相同模型名，部署者仍需确保评审模型使用不同权重；最终语义成绩需要人工校准。整个流程不训练、不增加 GPU 服务，不自动把实验策略切换到线上网页。

# v4：语义复核、受约束回答与缺口补查

入口：`bash scripts/run_eval_v4_20261028.sh launch|status|stop`。默认输出：
`/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_v4_20261028/`。
同配置 `launch` 可恢复；代码或数据身份变化时拒绝混用旧结果，应另设 `--output`。

流程依次包含：20条评分器正反例检查；对 v3 六组保存答案共600条做离线语义复核；
重新运行固定BM25、guarded BM25、guarded＋CPU重排各100条开发题；四组缺口补查对照各12题；
隔离的真实模型Web验收；开发集报告及条件式测试集准入。全部原始失败保留，不改写v3预测。

语义评分采用独立权重的本地 Qwen3-VL-8B-Instruct，回答模型为 Qwen3-8B；
模型路径与小型权重索引元数据记录在评分配置中。它们属于同一模型系列，不能把评分叫作独立人工准确率。
评分输入去掉策略名、模型名和校准标签；正确性、忠实性、引用支持分开评估，附原子事实、原文和理由。
评分器输出错误最多修复三次，仍失败则保留缺失评分；不把失败当作0分，也不静默从分母删除。
20条合成正反例仅检验基本判断能力，不能证明对所有问题可靠；含可回答却拒答、应澄清却未追问的反例。
可回答题的拒答不能因“不编造”获得正确性分数，该约束由代码校验；忠实性仍可单独得分。完整逐题复核包保留人工裁决空字段。

新回答策略 `guarded_v4`：主机先检索并读取证据，将原文行编为 `S1:3` 等稳定编号；
模型逐项判断题目要求是否有依据、缺证据还是缺用户条件。缺证据时最多补查一次，最多四次模型调用、十二次工具操作；
需要澄清时只能提交追问，代码拒绝夹带命令或“先操作再确认”；回答由原子结论及证据编号组成，代码生成行内引用并再次检查ACL。
证据编号合法只证明引用来自实际已读资料，不证明结论语义正确；题意理解及语义判断仍可能出错。
运行状态、错误反馈、证据选择与补查原因都保存，可中断恢复；没有把题目参考答案/标准证据送给回答模型。

补查诊断集是新建的合成演练资料：8个跨文档题族、2个单事实对照、1个澄清和1个不可回答对照。
每族4种问法共48条，正式诊断预先选择12个题族的直接问法。分别比较允许一次补查/禁止补查，
以及自然检索/第一轮人为移除第二份关键文档两种条件。后续检索可以找回该文档；移除清单不进入模型上下文。
这一干预是测试工具能力的诊断，不是业务准确率；不能用它替代原始测试集。

准入条件在队列 `protocol.json` 中预先冻结：完整评分、开发集语义正确至少比基线多3题、
正常结束至少99/100、20条不可回答/澄清/注入检查全部语义正确、无越权或攻击输出，忠实性和引用支持不退步；
缺证据诊断至少答对6/8且比禁止补查多4题，自然条件最多退步1题；隔离Web验收通过。
若有多个候选，按语义正确数选择，平分选BM25。此门槛是研究实验的工程约定，不代表统计显著或生产放量标准。
满足才生成 `test_release.json` 并执行冻结的300题基线/候选对照，测试结果不用于继续调参；
否则状态为 `complete_dev_gate_not_met`，报告保存每项未通过原因，测试集保持未运行。
现有Web不自动切换，浏览器测试使用独立数据库和临时端口。

Judge运行依赖使用项目自己的 `judge_packages` 只读软链接覆盖；没有升级共享DeepSearch虚拟环境。
所有数据、日志、截图、模型缓存与源码快照都保存在项目 `/data/zjr` 目录。

## v4.1：协议修复与三组对照

入口：`bash scripts/run_eval_v41_20261028.sh launch|status|stop`。
默认输出为 `/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_v41_20261028/`。
本轮复用回答服务 GPU1 和评分服务 GPU0；重排在 CPU 运行，没有训练或新模型部署。

`guarded_v41` 单独保存，旧 `guarded_v4` 策略及评测目录不覆盖：

- 证据数组的数量上限写进工具定义，最多8条，提示优先选1至3条；程序区分数量超限、重复、编号不存在等错误。
- 当前阶段只使用指定名称的一个工具；检测 `finish_reason=length`，截断内容不执行，反馈要求缩短。
  后端错误日志有脱敏状态码、类别和有限错误正文，不记录请求头或密钥。
- 澄清允许“Git服务器”等普通名词，继续拦截实际命令及“先操作再确认”。区分未知用户意图与文档缺少公司真实事实。
- 仍最多四次模型调用、一次定向补查、十二次工具操作；原文编号及权限在主机侧检查。

v4.1评分口径单独版本化：接受等价措辞，不因未重复“推荐”等字样而判错；不要求问题未问的操作细节。
纯拒答无外部断言时忠实性/引用检查通过，但可回答题拒答仍计正确性0。报告同时给出实际回答子集的忠实性，
防止混淆安全拒答与完成任务。26条正反例只是基本检查，并非人工校准。
对旧v4保存答案使用新口径复评分，单独报告“口径变化”和“同口径下策略变化”，旧分数不回填。

正式计划为固定BM25、固定BM25＋CPU重排、guarded_v41 BM25三组各100题；另复核争议评分和旧v4 BM25答案，
复用v4原封不动的合成缺口数据做四组各12题诊断，最后执行隔离Web验收和汇总。
后台队列首先运行22题定向回归，要求无执行错误、代码身份一致，评分校准至少24/26且13条反例全部识别，
通过后才继续三组完整开发对照。三组统一120秒HTTP请求超时，生成token和工具预算保持一致；所有超时均记失败，不删除。
正式准入要求新策略比**两条固定基线分别至少多答对3题**，正常完成至少99/100，20条关键题全部正确，
权限/注入、忠实性、引用支持、补查和浏览器检查均通过。完整门槛随代码快照存入`protocol.json`。
通过后才冻结候选与较强固定基线，执行一次300题正式测试；否则以`complete_dev_gate_not_met`结束，不切换在线Web。
成本与准确率一起报告，不能把“流程完成”当作“语义答对”或把开发集差异当成统计显著结论。

输出：`report.md`、`decision.json`、`comparisons.json`、`completion_breakdown.json`、`human_review.jsonl`。
逐题日志和来源随结果保留；`source/`为代码快照，`preflight/`为诊断与争议复核材料。

## v4.2：先定位故障，再做相同检索配置的对照

入口：`bash scripts/run_eval_v42_20261029.sh launch|status|stop`。
默认输出：`/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_v42_20261029/`。
前置记录：同级 `v42_preflight_20261029/`。所有新实验只用开发题；本队列没有训练或测试集阶段。

本轮只新增版本，不改写 v4/v4.1 答案、评分、策略。三组都使用同一 CPU 重排器、回答模型、
8192 token 模型上下文上限、1400 输出 token 上限、120 秒请求超时；Agent 仍最多四次模型调用、一次补查。

| 对照 | 与原策略的关系 | 用途 |
|---|---|---|
| `fixed42-rerank` | 复用固定链生成，仅由正文实际标记重建引用列表 | 排除引用字段不一致导致的执行失败 |
| `agent-guarded41-rerank` | 保持 v4.1 Agent 策略，接入同一重排器 | 判断检索底座的影响 |
| `agent-guarded42-rerank` | 题意要求绑定用户原文、区分缺口、空检索词回退 | 验证新 Agent 策略能否减少误拒答 |

引用修复不修改正文、不凭空加来源；正文或引用数组出现未读编号仍拒绝，最终继续检查权限。
新 Agent 的要求项增加 `question_span` 和 `scope`：区分文档知识、用户待选条件、组织实际事实。
文档知识缺失且 query 为空时，用缺少的要求构造补查词；组织事实缺失不能靠通用教程猜测。
原文锚点只检查文字来自问题/用户历史，不是题意理解或证据蕴含的正确性证明。

先跑34题诊断：旧错误、固定链与Agent胜负分歧、全部20条关键题及3条稳定正确对照。
该集合有意集中故障，不能拿其准确率代表全部开发题或真实业务流量。
启动前先完成三条评分短验证，后台首先完成30条评分控制（至少28条匹配、15条负例全部拒绝、两条正确性/引用分离控制的内容和引用评分均通过、无缺失评分），
再复评3条已存争议答案、运行BM25/重排CPU检索诊断、三组34题回答和评分、隔离浏览器验收。
候选需无执行错误、20条关键题全部正确、无权限/注入失败、正确数不低于两条对照，
且比相同重排器的旧Agent少误拒答，才自动执行三组各100题的完整开发评测；否则保存失败原因并停止。
门槛存入 `protocol.json`，不能看完结果后放宽。即使100题评测完成也不自动切换在线Web。

v4.2评分器对每条答案发出两个独立请求，均使用结构化JSON输出：内容请求可见题目与标准依据，
不可见候选引用资料；引用请求仅可见候选正文与其实际引用，不可见标准答案/标准证据。
逐条摘录候选实际答案，再判断要求是否满足；代码核对摘录确实属于候选，不能以参考答案补全候选遗漏。
这种输入隔离解决提示同时处理两项评分时容易互相污染的问题，但不保证语义判断本身总是正确。
每题评分至少两次模型调用，最多每阶段三次修复；这是离线评测成本，不计入回答模型调用数。
评分器仍是同系列另一权重的模型，控制题通过不代表独立人工可靠性；逐题包留有未填写的人工裁决字段。
缺失评分单独列出，禁止静默当0或删除；完整开发阶段有缺失时状态为 `complete_with_missing_grades`。

启动会保存完整代码、数据与配置身份。短验证与正式推理路径须相同；仅离线评分代码变化允许复用回答短验证，
但必须重新完成评分控制。运行中源代码变化会阻止继续下一个阶段。监视：

```bash
watch -n 10 bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/run_eval_v42_20261029.sh status
```

输出包括 `pilot_report.md`、`decision.json`、`pilot_comparisons.json`、`pilot_review.jsonl`、
`pilot_coverage.json`。通过诊断才有 `full_release.json` 及对应 `full_*` 结果。
`full_release.json` 仅放行开发集，不是正式测试许可。最终 `report.md` 指向最近完成阶段的内容。

## 2026-10-29：Haystack 成熟组件对照

用户选择先接 Haystack、保留 QAnything 接口。QAnything v2 官方默认依赖 MySQL、Milvus、
Elasticsearch 等服务，当前机器没有 Docker/Podman；本轮不部署这些服务，不冒充已运行 QAnything。

独立环境：`/data/zjr/enterprise_kb_agent/venv-haystack`。Haystack 固定 `3.4.1`，
OpenAI-compatible SDK 固定 `2.6.1`，完整依赖见 `requirements.haystack.lock`。
在线 Web 与原模型环境未修改。复用回答 Qwen3-8B、原 CPU BGE reranker 和原评分服务，无训练、新 GPU 模型。

实际复用 Haystack `InMemoryDocumentStore`、`InMemoryBM25Retriever`、`ChatPromptBuilder`、
`Pipeline`、`OpenAIChatGenerator`、`AnswerBuilder`。宿主仍负责身份/知识库授权、源文档偏移映射、
中文 bigram 预分词、CPU 重排桥接和精确上下文预算。它是固定 RAG 回答链对照，没有声称新增工具 Agent。

两组新策略与 v4.2 的三组已保存输出配对：

| 新组 | 检索 | 回答 |
|---|---|---|
| `haystack-existing-rerank` | 原 SQLite BM25 + BGE，原文档及 900/120 切块 | Haystack 单次自然语言生成与引用解析 |
| `haystack-bm25-rerank` | Haystack BM25Okapi + 同一 BGE，同样 20 候选/最多 5 片段 | 同上一组 |

复用同一 34 题诊断集合，不读测试题做推理。新链不要求 finish 工具调用/JSON 计算图，
只要求一个动作标记和自然语言行内引用。缺少标记保留为 `unclassified`；不存在的引用编号作为质量警告，
原始答案仍保留供复核，不隐藏重试。AnswerBuilder 的来源解析不证明语义支持，故独立报告推理成功率、
动作分类、格式、检索证据覆盖和离线语义评分。不得把更宽松的完成条件解释为回答质量提高。

先执行两组各 4 题真实模型短验证（多文档、澄清、ACL、注入），再启动两组各 34 题。
评分继续用冻结的 v4.2 rubric，错误与缺失分开保留，另输出随机排列的 170 份候选人工复核表；
不为通过门槛而修改评分器，不自动扩大到 100/300 题或切换网页。旧三组与新两组执行时间不同，
共享 GPU 服务的耗时只能作观察值。诊断题刻意富集旧错误，不能代表业务流量准确率。

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/run_haystack_20261029.sh setup
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/run_haystack_20261029.sh test
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/run_haystack_20261029.sh smoke
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/run_haystack_20261029.sh launch
watch -n 10 bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/run_haystack_20261029.sh status
```

`launch` 支持同代码/配置下续跑，`stop` 只停止该队列、不停止模型服务；中断后的未完成题重跑，
已完成题不覆盖。产物位于 `/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_haystack_20261029/`：
逐题 `predictions.jsonl`、原始回答/证据、`comparison.json`、`report.md`、`human_review.jsonl` 和单独的盲化映射。
最后状态 `complete_pending_human_review` 表示计算完成，尚不等于人工质量验收通过。

保留的 `kb/backends/qanything.py` 支持 v2 建库、TXT 上传、文件状态、非流式问答接口：
仅连接本机配置端点，由宿主传入已授权 KB 列表，拒绝返回越权来源，不把来源列表自动当成有效引用。
本轮仅通过 HTTP 模拟契约测试，真实 QAnything 服务、入库和答案质量未验证。
后续部署服务后还需完成文档 ID/字符偏移映射与相同评测适配；它当前不是可切换的在线后端。

相关工作与复用边界：
[Haystack 3.4.1 源码](https://github.com/deepset-ai/haystack/tree/v3.4.1)（Apache-2.0），
[官方 RAG 评测教程](https://haystack.deepset.ai/tutorials/35_evaluating_rag_pipelines)，
[QAnything v2 API](https://github.com/netease-youdao/QAnything/blob/qanything-v2/docs/API.md)（上游 AGPL-3.0）。
本地适配器为独立接口实现，未复制 QAnything 内核，也未将依赖项目贡献归为自研。

