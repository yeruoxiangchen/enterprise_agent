# 多 Agent 与按需 Skills v1

目标：复用 Haystack 3.4.1 的 Agent / AgentTool，完成子问题分工、证据汇聚和按需规则加载，并保留与固定 RAG 的对照。模型、语料与索引沿用现有项目；没有训练。

## 执行链路

1. Planner Agent 通过 submit_plan 生成 1–3 个子问题并选择技能。主机校验任务数量、重复任务和技能名称。只有模型判断出的计划进入执行，不使用标准答案、题型标签或 gold evidence。
2. 主机对每个子问题分别调用现有 BM25 / 可选 CPU 重排，按轮转方式分配证据。首次最多加入6个唯一完整片段，另留2个位置给补检索；加入前检查 Token 预算。
3. 每个 Specialist 是实际 Haystack Agent，经 AgentTool 调用，有独立上下文、任务和工具。可申请一次定向补检索，或提交结构化结论及证据。第一版串行共用模型服务。
4. 单任务直接采用其有效结果；复合任务由 Synthesis Agent 对照原文合并结论，未解决子任务不能被改写成全部解决。来源编号由主机统一分配并渲染，语义是否支持仍属于模型判断。
5. 通过既有 Harness.finish 原子提交到应用。保留登录、权限、引用、反馈和历史会话。前端可显示子任务、查证与技能加载事件。

这是一套“模型生成计划、主机执行有界任务图”的编排。没有让主Agent无限循环委派，没有假称各子Agent具有不同模型权重。简单题仍消耗一次规划加一次作答；它是否值得优于固定RAG由成本对照决定。

## Skills

目录 `skills/<id>/skill.json` 声明 id、版本、用途、工具白名单；`instructions.md` 保存规则。已提供资料查询、操作指导、故障排查三个技能。Planner只看目录，子任务才加载对应全文。关闭Skills的对照保留相同规划目录，只关闭子任务规则注入；该对照不能解释为完全无技能分类。

技能不授予用户权限，也不执行文件中的代码。用户和知识库范围由服务端绑定；引用前再次检查ACL和文档内容。技能SHA进入检查点和评测指纹，规则改变后应创建新运行。

## 预算与恢复

- 全局最多12次模型请求、24次工具/主机检索操作；请求失败或中断仍计入调用额度，无隐藏SDK重试。
- 每次请求用实际模板与工具schema计数，保留1400输出token（默认配置），不截断当前问题和已读证据。缓存仅在同一次授权请求内共享。
- 检查点包含计划、完整证据、检索缓存、已完成子任务及剩余额度。恢复时复查身份、配置、技能与文档内容。
- 已完成子任务不重跑；中断中的子Agent从其任务边界重启，保留已消耗额度。这里没有宣称逐token或任意工具指令位置恢复。
- 主机取消检查贯穿请求前后、工具执行与最终提交。API只暴露当前用户拥有的任务。

## 代码

- `kb/backends/multi_agent.py`：计量模型适配器、规划、AgentTool专家、证据池、汇总。
- `kb/skills.py` 与 `skills/`：技能目录、版本与按需加载。
- `kb/multi_agent_runtime.py`：应用队列与检查点适配。
- `scripts/eval_multi_agent.py`：独立评测、断点恢复、评分、盲化复核包。

复用来源：Haystack Agent / AgentTool，Apache-2.0；编排参考官方教程：
https://haystack.deepset.ai/tutorials/45_creating_a_multi_agent_system
业务检索、来源校验、预算、任务持久化及评测适配由本项目实现。

## 运行

在项目根目录执行：

```bash
bash scripts/run.sh multi test -q
bash scripts/run.sh multi launch
watch -n 10 bash scripts/run.sh multi status
tail -f /data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_multi_agent_20261029/queue.log
bash scripts/run.sh multi stop
```

launch 自动先跑两题真实服务联调，失败则停止，再依次跑四组各34道开发诊断题与v4.2语义评分。沿用已保存的34题ID，含旧失败/分歧，不代表独立业务准确率。不运行300题测试集。

四组：fixed-rerank（现有检索＋Haystack固定回答）、single-agent（不拆分且关闭Skills）、multi-no-skills、multi-skills。固定组最多5来源/1次生成，Agent组最多8来源/12次生成；报告调用量和来源数，这不是等计算预算对照。多Agent对单Agent同时改变拆分、证据组织和汇总，不能把所有差异归于Agent数量。语义评分缺失保留未知，并导出独立复核表。

默认复用现有回答/评分服务，不启动模型。依赖已有 `venv-haystack` 与CPU重排。服务未就绪时launch会明确失败。中断后使用原目录launch恢复；源码/配置变更时通过 `--output /data/zjr/enterprise_kb_agent/artifacts/<new-name>` 建新实验。

预览网页使用独立数据库，端口8769：

```bash
bash scripts/run.sh multi preview-init
bash scripts/run.sh multi serve
```

账号及演示资料位于 `/data/zjr/enterprise_kb_agent/preview_multi_agent/`。原8768服务仍由其原启动命令管理。预览模式默认检索配置遵循Settings，完整CPU重排对照在上述评测脚本中启用。

所有数据库、检查点、模型响应和评测产物位于 `/data/zjr/enterprise_kb_agent/`。Tracker仅保存源码、技能规则和说明文档。

## 本轮验证与交接

- 新增多Agent测试12项，加既有外部RAG和应用测试，共32项通过；验证实际Haystack Agent/AgentTool执行，模型输出由脚本提供。另通过Python AST、Shell及前端JavaScript语法检查。
- 真实服务短验证2/2正常结束，各4次模型调用、6次工具/检索操作。产物：`/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/multi_agent_preflight_20261029/smoke/`。
- 简单配置问题被Planner拆成两项，显示规划本身会增加调用。SSH/store复合题正确取到公钥，但最终答案仍混淆credential store与Keychain/Windows凭证管理器。短验证只确认工程连通，不判定质量达标。
- 四组34题后台对照已启动，启动时PID为267674，根目录为`/data/zjr/enterprise_kb_agent/artifacts/git_support_v1/dev_multi_agent_20261029/`。冻结代码后重新执行两题联调，再运行四组推理和评分；进度以status命令为准。
- 当前在线8768应用未发布该候选。应用接入已通过隔离API测试，独立网页预览入口已提供。
