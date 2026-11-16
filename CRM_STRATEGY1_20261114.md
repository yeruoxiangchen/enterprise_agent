# strategy1：执行进度、处理经验与故障材料

日期：2026-11-14。保持主管Agent与专业Agent结构，使用现有Qwen3-8B，新增实现不涉及模型训练。实验、OCR组件、材料、截图和视频均保存在`/data/zjr/enterprise_kb_agent/`。按当前用户约定，预计两小时以内的验证由助手完成并分析。

完整复测更新：`strategy1_official_itsm66_20261114_r1`已完成132/132，当前experience方案与官方均48/66，未判定分别0/1。当前相对官方Token少36.59%、Agent任务p95少28.20%；旧r2为53/66，当前丢7题、改善2题，经验策略继续作为可选项。下文四题结果属于开发记录，完整结论见[当前状态](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

截图修复更新：CPU区域检测、分栏及边缘留白已应用。同8张公开图、同答案及评分脚本，关键文字4/8→8/8；企业8例的26字段与工单闭环保持全部通过。17项定向检查通过，详见[修复记录](OCR_REPAIR_20261114.md)。

## 执行策略

`KB_SUPERVISOR_STRATEGY`及CRM的`--supervisor-strategy`支持：

- `baseline`：保留原主管行为，作为本轮对照。
- `recovery`：保留澄清中的原任务；从实际工具观察、业务终态和已提交回执生成进度账本。重复观察时提示检查进度；主管过早结束且仍有授权步骤未办时，每轮最多追加一次恢复。
- `experience`：在recovery上，按当前请求和专业领域检索最多两条已审核经验，字符预算1100，不增加独立模型调用。简单读取不额外加载经验。

缺少输入时继续澄清；写入结果未知时保留核实状态；已提交步骤不重新执行。恢复与正常调用共享原模型、工具、上下文和超时预算。重复观察提示不禁止合法的重复查询。进度来自可见请求、真实观察与回执，不读取任务名、评分条件或期望答案。

最终版本`business_v6_external_v4_supervisor_strategy1_r2`还将用户提供的问题描述保存在目标线索中：先询问“哪个工单”，接受问题描述或编号；用户给出描述后，专业Agent从真实工单列表定位，匹配不唯一时继续澄清。

实现：[crm_strategy.py](kb/benchmark/crm_strategy.py)、[crm_supervisor.py](kb/benchmark/crm_supervisor.py)。

## 经验的增量维护

[experience.py](kb/experience.py)复用应用的中英文分词和SQLite FTS5/BM25，初始条目为通用处理方法。新条目及修改后的指导默认进入candidate，只有显式审核后才参与检索；增量合并保留其他经验。运行期间固定策略和经验版本，避免续办时悄悄切换。

```bash
bash scripts/crm_bench_run.sh experience init --memory /data/zjr/enterprise_kb_agent/experience/memory.json
bash scripts/crm_bench_run.sh experience query --memory /data/zjr/enterprise_kb_agent/experience/memory.json --query 'comment then update status remaining'
bash scripts/crm_bench_run.sh experience update --memory /data/zjr/enterprise_kb_agent/experience/memory.json --delta /data/zjr/enterprise_kb_agent/experience/delta.json --reviewed-ids reviewed-entry
```

条目不存具体测试工单号或预期答案。运行反馈记录所用经验、剩余步骤及终态；CLI的feedback增量统计使用后的结果，重复导入不会重复计数。这些计数是观察统计，不表示某条经验带来的因果收益。中文业务v6也可通过`KB_SUPERVISOR_STRATEGY=experience`按领域加载指导，原写入授权及证据来源校验继续生效。

## 故障截图与日志

新入口位于对话输入框下方“故障截图／日志”，登录成员可上传PNG、JPEG、WebP或TXT、LOG、Markdown，单文件5 MiB以内，文字日志256 KB以内。截图使用CPU Tesseract：等比缩放、灰度、深色背景反色及对比度归一化；原图单独保留。

字段包括错误码、运行环境、软件和故障现象，关联实际识别文字、行号、字符范围与截图区域。用户核对后，可将已识别字段带入知识库排查，或修改标题、描述并生成待确认工单草稿。草稿沿用原有提交、管理员接单、解决和员工查询流程。

材料、原图和草稿均校验当前用户归属；材料中的指令不会自动创建或修改工单。草稿确认绑定材料摘要，同一确认重放复用原草稿，变更确认内容产生明确冲突。识别不清的字段由用户补充。

```bash
bash scripts/setup_incident_ocr.sh
bash scripts/crm_bench_run.sh incident-acceptance --output /data/zjr/enterprise_kb_agent/artifacts/strategy1_20261114/new_incident_run
```

依赖：应用环境可安装`.[incident]`；本机脚本将Pillow、Tesseract和中英语言文件放在项目的`/data`环境中。浏览器自动验收使用已有`/data/zjr/enterprise_kb_agent/venv`中的Playwright。

## 验证记录

输出根目录：`/data/zjr/enterprise_kb_agent/artifacts/strategy1_20261114/`。

- 第一组联合回归201项通过；后续最新模式53项、真实OCR及接口11项、主管修订80项和经验维护9项均通过。XML重叠项去重，并排除被参数化用例替换的旧OCR用例后，共228项当前用例通过，见`test-summary.json`。
- `incidents_r1`与`incidents_r2`：12个合成日志／渲染截图样例中字段召回率97.5%、精确率100%，12条工单链路及引用检索通过。深色截图中Linux被低置信度误识别，保留原始记录。
- `incidents_r3`：同12个样例、同输入，加入灰度／深色反色归一化后，40个预期字段全部提取，字段精确率及召回率100%，来源定位12/12、检索引用12/12、实际工单流转12/12。这是可执行功能验收，数据为程序生成的样例。
- 网页实操通过：截图识别区域、字段核对、知识库排查、草稿确认、管理员接单／解决及员工回查。额外检查了图像与标注层几何对齐和CSP样式限制，修复标注层被内联样式限制影响的问题。最终截图、视频与浏览器结果在`incidents_r3/browser/`。

## 真实模型结果

四题选择普通查询、备注后更新状态、澄清工单、多轮精确备注与状态更新。共享Qwen3-8B、GPU1、78%显存预算，8轮／180秒开发协议，每种模式各一次。最终与基线的开场4/4相同，运行异常、未判定和用量缺失均为0。

| 指标 | baseline | experience r2 |
| --- | ---: | ---: |
| 通过 | 2/4 | 4/4 |
| Agent Token合计 | 76,314 | 61,742 |
| 模型调用 | 52 | 40 |
| 业务工具尝试 | 8 | 11 |
| 已提交写入 | 3 | 5 |
| 任务Agent耗时p95 | 35.62秒 | 22.18秒 |
| 每成功任务Token | 38,157 | 15,435.5 |

Token降低19.09%，模型调用降低23.08%，每成功任务Token降低59.55%。工具尝试增加来自补齐原未完成的合法工作；主管和专业Agent调用均计入表内，模拟用户与裁判另存。普通查询Token为4666→5129、普通备注加更新为10234→12158，经验与进度上下文本身有开销；总体节省主要来自减少漏办后的重复交互。

首轮experience为3/4，Token40719、模型调用30；澄清工单仍失败。轨迹显示助手持续索要编号、模拟用户重复追问。r2接受问题描述作为线索并通过实际列表定位，最终该题完成。两轮原始记录全部保留，本轮四题是已见公开开发任务，最近完整66题成绩仍为此前r2的53/66。

三个运行标签：`strategy1_baseline4_20261114_r1`、`strategy1_experience4_20261114_r1`、`strategy1_experience4_20261114_r2`；输出在`/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/`。全部队列已结束，GPU模型服务均已释放。对比汇总见`strategy1_20261114/real-comparison.json`。

线上应用已在原`http://127.0.0.1:8768`更新并通过健康检查，保留原账号和数据。临时浏览器验收服务已停止。策略入口为`--supervisor-strategy experience`；线上问答策略继续由`KB_AGENT_POLICY`显式选择，故障材料功能独立可用。

来源：[Magentic-One](https://arxiv.org/abs/2411.04468)启发任务与进度账本；[ACE](https://arxiv.org/abs/2510.04618)启发可增量维护的处理经验。本轮实现没有运行GEPA搜索。
