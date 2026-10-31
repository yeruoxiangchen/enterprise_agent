# 官方 Agent Pack 接入与开发对照（2026-10-30）

## 目的与复用边界

上一轮 Multi-Agent 的拒答/澄清协议、局部步数耗尽和汇总展示冲突已有逐题证据，见 [失败分析](MULTI_AGENT_ANALYSIS_20261030.md)。本轮接入真正的官方完整工作流，减少自行设计的规划器、专家结果协议和汇总循环。

直接调用 `agent-pack-haystack==1.0.0` 的 `create_advanced_rag_agent`，使用其 Agent 循环、元数据查询工具、检索工具、按条件取文档工具及 `BackupAnswerHook`。Haystack 固定为 3.4.1。上游为实验性组件，接入不代表效果已经达标。

- [上游完整实现](https://github.com/deepset-ai/haystack-core-integrations/tree/main/integrations/agent_pack/src/haystack_integrations/agent_pack/advanced_rag)
- [官方说明](https://docs.haystack.deepset.ai/docs/3.3/advanced-rag-agent)
- 许可证：Apache-2.0；没有把上游代码声称为自主开发。

本地实现负责权限、SQLite 检索与 CPU 重排适配、调用预算、引用映射、持久化、取消、网页接入和评测。此候选是**单 Agent 多工具流程**，不是上一版多 Agent 编排，也不是部署了 QAnything。QAnything 接口继续保留。

## 实际调用链

授权用户及知识库 → 请求级授权文档库 → 官方 Advanced RAG Agent → 元数据/检索/取文档工具 → 官方正文回答 → 来源与引用校验 → 应用原子提交。

1. 每次请求只将用户可访问的文档放进独立内存库；工具调用前复核权限，文档返回及最终引用时复核正文未变化。
2. `search_documents` 复用现有 BM25 和 CPU BGE 重排：授权范围内取 20 个候选，重排后按可选元数据条件过滤，返回最多 5 段。**元数据过滤位于候选重排之后**，因此不等同于全库预过滤后取 top-k；模型也可用官方按条件直接取文档工具。
3. 原官方提示词保留，追加中文业务规则和 `<answered>/<insufficient>/<clarify>` 文本标记；不再要求模型通过自定义 `submit_result` 同时填写 message/claims。
4. 为本地 Qwen 强制执行官方第一步 `list_metadata_fields`，之后要求至少一次正文检索，再恢复 `tool_choice=auto`。避免仅凭元数据就断言资料不足。这是明确记录的本地适配，不称作完全未经修改的官方运行配置。
5. 官方 `[doc 八位ID]` 只可映射到本次真实取回且仍有权限的资料，再转为应用 `[S1]` 引用。编号正确不等于语义正确，仍需独立评测。
6. 正常 Agent 最多 6 步，官方收尾最多另加一次。主循环和收尾显式配置**同一本地服务**，无默认云端模型。失败请求也计入预算，SDK 自动重试关闭。
7. 每次调用前按真实模型模板与工具 schema 计数；上下文不足明确报错，不静默截断证据。未完成请求恢复时重新进入官方循环，但保留已消耗预算；不是循环内部逐节点无损恢复。

## 文件与运行环境

| 文件 | 用途 |
|---|---|
| `kb/backends/agentpack_rag.py` | 官方工厂接入、授权工具适配、计量和引用校验 |
| `kb/agentpack_runtime.py` | 会话、检查点、取消、事件与原子提交 |
| `kb/agent.py` | 新增可选 `agentpack_v1` 分支 |
| `scripts/eval_agentpack.py` | 预检、两组开发对照、冻结、评分及复核包 |
| `scripts/run_agentpack_20261030.sh` | 独立环境、测试、预览及后台队列入口 |
| `tests/test_agentpack.py` | 真实官方循环配脚本化模型的工程测试 |
| `requirements.agentpack.txt` | 固定依赖 |

环境 `/data/zjr/enterprise_kb_agent/venv-agentpack`；完整依赖快照 `/data/zjr/enterprise_kb_agent/agentpack-environment.lock`。缓存、测试数据和评测均在 `/data`。复用回答/评分服务和 CPU 重排，不训练、不加载额外 GPU 模型。

在线默认网页 8768 未切换。隔离预览可用 `bash scripts/run.sh agentpack preview-init` 后 `bash scripts/run.sh agentpack serve`，端口默认 8770，数据库位于 `/data/zjr/enterprise_kb_agent/preview_agentpack`；预览尚不是生产发布。

## 联调失败也保留

- `agentpack_preflight_20261030`：工具传入但 Qwen 未调用；可答题直接拒答，企业事实缺失误作澄清。
- `agentpack_preflight_20261030_r2`：补中文流程提示后仍未检索。旧预检只要求不可答题动作正确，错误地放过可答题拒答；已收紧为所有预检题必须动作匹配。此目录的通过回执不能作为接入成功证据。
- `_r3`：只强制元数据查询仍不足；模型有时把元数据字段不含主题误认为正文没有答案，还有状态标签与拒答正文不一致的问题。
- `_r4`：增加至少一次正文检索约束。保留各轮失败记录，不覆盖既有结果；最终联调回执以该目录及正式队列 smoke 结果为准。

## 冻结开发对照

最终接入代码的 33 项工程回归已通过，覆盖真实上游工具循环、授权隔离、来源变化、引用映射、调用计量、上下文超限、收尾调用及应用原子提交。测试模型为脚本化响应，不能替代真实模型评测。

另新增并通过 1 项诊断门槛测试：质量失败可进入离线对照，但权限泄漏、请求执行错误、从未实际取回文档不能通过接入检查。

真实 Qwen 服务 `_r4` 三题预检为 3/3 通过动作与引用契约检查，分别返回 answered / insufficient / clarify，各 4 次模型调用。仍观察到答案措辞精度问题；该回执只证明联调，不证明语义质量或收益。

相同冻结的 34 道开发诊断题、授权语料、回答模型、CPU 重排，比较 `fixed-rerank` 与 `official-agentpack`。固定分支一次生成，Agent 最多 7 次且增加元数据取文档能力；这是流程方案比较，**不是等预算或单变量消融**。只使用开发集，不打开测试集。

六题扩大联调（旧目录 `dev_agentpack_20261030/smoke`）暴露了复杂题耗尽步数后的错误拒答，以及可答题引用缺失。三题通过不能推广为所有场景通过。因此本轮明确转为**离线诊断对照**，冻结候选，不再依据少数题反复修改提示词。

该轮 6/6 请求正常完成，5/6 输出契约通过，4/6 动作匹配；1 题无引用、2 题错误拒答，无禁止内容或越权引用。复杂题及注入题各触发一次官方收尾（共 7 次模型调用）。这不是回答正确率统计，也不是收益结论。

队列顺序：6 题接入检查 → 固定 RAG 34 题 → 官方 Agent 34 题 → 初步报告 → 两组 v42 语义评分 → 最终报告及盲化人工复核包。接入检查要求工具确实取回文档、请求可完成且无权限泄漏或禁止内容；回答契约和动作错误单独记录为质量失败，继续纳入诊断，不因错误而删除样本或宣称通过上线门槛。评分失败保留未知，完成不等于可替换线上流程。

```bash
# 在项目目录运行
bash scripts/run.sh agentpack launch
bash scripts/run.sh agentpack status
bash scripts/run.sh agentpack stop
```

默认产物目录：`/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_agentpack_diagnostic_20261030/`。旧六题联调目录独立保留，不复用为新冻结对照结果。

2026-10-30 已后台启动，队列 PID `3424003`；交付时核对为 `running / smoke / indexing`，进程存活，错误为空。PID 和阶段只是交付时快照，后续以 `status` 为准。

其中 `protocol.json`、各组 `config.json`、`source_snapshot/` 固定代码、参数、数据和上游源码 hash；`queue.log` 为队列日志，`report.md`、`comparison.json` 为汇总。每题保留工具轨迹、模型用量、失败原因链和检查点。日志不记录 SDK 密钥或 HTTP 请求头。

评测启动后不要修改冻结的推理代码；要改策略请使用新输出目录。应根据完整配对结果再决定是否接入默认网页。
