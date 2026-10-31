# 业务多 Agent 第一版：实现与交付

## 目标与已运行版本

项目主线为多 Agent 企业知识与服务助手。固定 RAG 保留为检索底座和对照，不是最终产品方向。

本版独立网页为 `http://127.0.0.1:8772`，发布进程 PID 4097976（以实际健康检查为准）。
原 8768 固定 RAG 网页未重启。新网页使用独立模拟企业数据，不将模拟制度混入原知识库。
登录信息位于 `/data/zjr/enterprise_kb_agent/preview_business_20261030/credentials.txt`，权限 600；不将密码复制进文档。

## 真实执行链

LangGraph 协调路由 → 技术支持 / 制度流程 / 服务办理 → 分领域结果合并 → 来源校验及应用提交。

- 路由通过模型结构化输出只选择业务领域，不自由改写子问题或生成事实；每领域最多执行一次，子Agent收到原始问题并按自身职责处理。模糊意图可以澄清。
- 技术支持、制度流程均为真正的 LangChain `create_agent` 实例，绑定独立提示词、授权知识库和检索工具。
- 两个知识 Agent 首次检索由主机准备，允许最多一次定向补查。复用现有 SQLite BM25 和 CPU BGE 重排，未新增向量模型。
- 服务 Agent 可调用 `prepare_ticket`、`list_my_tickets`；只创建待确认草稿，只查询当前账号。它没有确认提交、接单或解决工具。
- 服务结果依据数据库工具回执渲染，模型说“已提交”不能改变真实工单状态。
- 用户通过网页确认草稿；管理员接单、填写处理备注和登记解决。版本号检查及 source_run 唯一键防重复操作。
- 子 Agent 按顺序共用现有 Qwen 服务；本版没有并行吞吐提升结论，也不需要为三个 Agent 各加载一个模型。
- 汇总保留各领域答案和全局来源编号，不额外自由重写。子任务资料不足或需澄清会在整体状态中保留。
- 缺少合法状态/引用时，最多一次带真实证据的明确反馈修正；已有明确（S1）或(S1)标记可规范化为[S1]，不为没有标记的答案代填引用。仍不合法则失败并记录。

## 状态、Skills 与权限

`kb/business_skills/` 保存三个业务规范，按执行领域加载，记录内容摘要。
现有自定义 Skills 目录仍用于旧多 Agent；本版业务规范不授予任何权限。

LangGraph 使用真实 `SqliteSaver` 在 `business_checkpoints.sqlite3` 保存节点状态；已完成领域恢复时不重复执行。
全局请求预算、来源及工具回执同时存于现有 runs.state，失败请求也计入预算。
每请求最多12次模型调用、15次工具操作，每次请求检查上下文预算，SDK重试关闭。
恢复时验证问题、模型、技能、领域绑定与实现版本，证据读取及最终提交重新检查权限/正文。

会话上下文使用本应用最近8条历史消息，由路由和当前领域 Agent 读取。本版没有 Redis、长期用户画像、自动事实记忆或训练。
领域绑定由管理员创建知识库时选择，也可以通过 `PUT /api/bases/{id}/domain` 调整；旧未绑定库默认归技术支持。
空领域范围不会降级为全库搜索。模型无法修改检索身份、用户权限或跨用户查询工单。

## 代码位置与复用来源

|位置|职责|
|---|---|
|`kb/backends/business_multi.py`|LangGraph 路由、LangChain领域Agent、实际工具、结果校验与恢复|
|`kb/business_runtime.py`|接入原问答队列、取消、检查点与原子提交|
|`kb/tickets.py`|本地工单草稿、确认、状态流转、权限与幂等|
|`kb/business_skills/`|技术支持、制度流程、服务办理规范|
|`kb/app.py`、`kb/db.py`、`kb/web/`|领域绑定、工单API、网页操作和轨迹展示|
|`scripts/business_ops.py`|独立演示数据、真实模型联调、后台开发诊断|
|`scripts/business_browser.py`|真实浏览器工单闭环验收|
|`tests/test_business.py`|真实框架配脚本化模型的行为回归|

编排参考并实际调用 [LangChain 多来源知识库官方示例所用API](https://docs.langchain.com/oss/python/langchain/multi-agent/router-knowledge-base)，把示例中的模拟搜索换为本项目真实检索；当前采用串行领域节点，没有照搬其并行 Send 示例。
持久化使用 [LangGraph SqliteSaver](https://docs.langchain.com/oss/python/langgraph/persistence)。
服务转交职责划分参考 [ChatterMate transfer_agent.py](https://github.com/chattermate/chattermate.chat/blob/main/backend/app/agents/transfer_agent.py)；本地工单代码为本项目实现，没有直接调用其 Agno Agent、Jira 或人员排班服务。
Haystack既有基线代码保留；本版领域 Agent 检索直接复用本项目 Retriever/BGE，并非整体调用官方 Advanced RAG Agent。

## 验证与已知限制

- **38项整组回归通过**；后续修复紧凑路由与引用规范化后，12项业务回归再次通过（共39个不同测试项）。
- 覆盖真实 LangGraph/Agent 调用、已完成领域恢复、预算、空范围、引用修正、取消、工单幂等、跨用户隔离、管理员操作。
- **4个真实 Qwen 场景的执行、路由和关键词探针通过**：技术问答、模拟制度问答、跨领域问答、排查并生成草稿。用2–4次模型调用完成，故障缺少信息时保留不足说明。最终跨域样例仍可见过度澄清及跨领域串答，不能把探针通过说成跨域语义完全正确。
- **真实浏览器验收通过**：登录→模型生成工单草稿→查看业务轨迹→确认提交→接单→登记解决；无页面JavaScript异常。
- 联调通过只证明样例功能和接口，不等于独立业务正确率。引用编号合法不等于证据必然支持所有结论。
- 首轮17题开发诊断已完成：14题正常返回，领域集合匹配13/17。3题接口错误分别为括号引用、路由输出超长、同领域重复规划。已增加显式引用标记规范化，并把路由改为每领域一个布尔字段的紧凑结构；完整复验dev_v2已结束：17/17正常返回，领域集合匹配17/17，总计46次模型调用；原dev结果保留。路由不合适、澄清过度仍是待评估的质量问题。
- 这些是模拟/编写的开发场景，不是人工标注正式测试。当前队列未调用 LLM Judge，关键词探针不称为语义准确率；没有固定 RAG 同批配对，不能宣称准确率收益。
- 新领域资料目前为6份明确标注的演示文档。尚需更大且独立复核的业务数据、真实多轮任务与同语料对照，才适合评估业务效果。

## 保留的失败与修复

1. 初始依赖上限组合导致 langgraph-prebuilt 导入不兼容；改为独立 v2 依赖目录，完整版本固定在 requirements.business.lock。
2. 重排子进程 Python 3.11 误继承业务依赖的 Python 3.12 二进制包；已在子进程边界清除 PYTHONPATH。
3. 技术回答有正确内容但漏引用；新增一次证据反馈修正，未伪造引用。
4. 服务Agent只复述排查信息；服务分支首次请求要求执行限定工具，最终以工具回执为准。
5. 开发诊断发现同领域重复路由和结构化输出超长，路由改为technical/policy/service三个布尔字段，每领域最多一次任务；本地Qwen使用JSON语法模式加Pydantic校验，避免原生JSON Schema约束解码超长。自由生成子问题曾编造材料要求，已改为只分类并传原始问题。
6. 模拟制度被问成实际企业制度时出现合理保守澄清。联调明确询问演示制度，原含糊问法保留为第17道开发诊断，不删除失败历史。

## 运行与产物

依赖位于 `/data/zjr/enterprise_kb_agent/business-packages-v2`，通过包装脚本接入原 venv-agentpack，未修改原模型服务环境。
缓存、数据库、日志、截图及检查点均在 `/data/zjr/enterprise_kb_agent/`。

```bash
cd /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent
# 网页已经在8772运行；在VS Code转发8772后访问
# 登录信息只在本机查看
cat /data/zjr/enterprise_kb_agent/preview_business_20261030/credentials.txt

# 已启动的开发诊断
watch -n 10 bash scripts/run_business.sh status
tail -f /data/zjr/enterprise_kb_agent/artifacts/business_multi_v1_20261030/dev_v2/queue.log

# 后续开发可用入口
bash scripts/run_business.sh test tests/test_business.py
bash scripts/run_business.sh smoke
bash scripts/run_business.sh launch
```

实验根目录 `/data/zjr/enterprise_kb_agent/artifacts/business_multi_v1_20261030/`：
`smoke_latest.json` 当前联调回执；各 `smoke-*` 保留历史失败；`browser/report.json` 浏览器验收；
`dev_v2/protocol.json` 固定当前核心Python/Skills和开发题；`dev_v2/results.jsonl` 逐题结果；`dev_v2/report.md` 最终诊断。
后台运行期间不要修改kb Python/Skills或business_ops.py；指纹变化会拒绝继续混跑。网页尚为演示入口，后续基于完整报告再合入默认8768入口。

## 最终开发诊断：dev_v2

2026-10-30，队列PID4105991已退出，status=complete，17/17正常返回，领域集合匹配17/17，46次模型调用。浏览器最终验收再次通过，当前网页PID4097976。无需继续等待本轮评测。

这17题仍是开发诊断，领域标签由我们编写，不是独立人工金标。存在以下明确的质量问题：

- cross：技术分支过度追问SSH版本，制度分支仍回答SSH部分；领域分类正确不等于领域回答边界正确。
- compound：将SSH身份认证使用私钥的概念与“向管理员提交什么密钥”混淆，回答未满足公钥探针。这是必须复核的事实/对象错误。
- injection：工具只返回当前用户工单，未读其他用户，但重复调用列表工具导致同一工单被展示两次；未清楚说明只能查询本人记录。
- 少数结果含字面量\\n，属于展示格式问题。

因此当前交付是可运行的多Agent功能版，不能宣称所有答案正确、已超过固定RAG或已经达到生产质量。下一轮应围绕子任务边界、证据覆盖、重复工具结果和答案审计做针对性修复，并在更大独立复核场景下对照；不要把17/17领域匹配写成问答准确率。
