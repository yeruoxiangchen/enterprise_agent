# strategy1：最终实现与验证

主管Agent与专业Agent结构保留，使用原Qwen3-8B，无模型训练。实现执行进度账本、有限恢复、审核经验检索与增量维护，以及故障截图／日志的来源定位、知识排查和确认建单。

## 真实模型开发对照

同GPU1、Qwen3-8B、8轮／180秒协议，各四题一次；四题开场完全相同。

| 指标 | baseline | experience r2 |
| --- | ---: | ---: |
| 通过 | 2/4 | 4/4 |
| 未判定／运行异常 | 0/0 | 0/0 |
| Agent Token | 76,314 | 61,742 |
| 模型调用 | 52 | 40 |
| 业务工具尝试 | 8 | 11 |
| 已提交写入 | 3 | 5 |
| 任务Agent耗时p95 | 35.62秒 | 22.18秒 |
| 每成功任务Token | 38,157 | 15,435.5 |

Token降低19.09%，模型调用降低23.08%。工具尝试增加来自完成此前缺失的合法操作。简单查询Token增加4666→5129，普通备注及更新10234→12158；策略上下文有开销，整体收益主要来自减少失败后的反复交互。首轮experience为3/4，保留其记录；r2明确接受问题描述并从真实列表定位后，澄清工单题通过。此处为四个已见公开开发任务的对照；最近完整66题成绩仍为此前r2的53/66。

逐题与历史结果：[real-comparison.json](real-comparison.json)。

## 代码、经验与功能验收

五份XML报告去重后228项当前用例全部通过，包含未知写入核实、崩溃恢复、澄清续办、只补剩余步骤、有限重试、审核门槛、跨账号材料保护、草稿幂等和原权限检查。已审核经验使用现有分词与SQLite FTS5/BM25；最多两条、1100字符，不增加独立模型调用。新增或修改指导进入候选，重复导入不能绕过审核。

12个程序生成的日志／截图样例使用真实CPU OCR与真实API，40个字段全部提取，来源定位12/12、检索引用12/12、确认／提交／管理员处理12/12通过。改进前深色截图漏识别Linux，召回97.5%；相同输入加入灰度、反色和对比度归一化后为100%。这些结果对应本次合成材料验收。

OCR截图分析p50为0.381秒、p95为0.403秒；含多账号登录、检索和工单流转的完整案例p50为4.124秒、p95为7.861秒。分位数采用线性插值，详情：[incident-summary.json](incident-summary.json)。

真实浏览器验证上传→识别区域→字段确认→知识库排查→草稿→员工提交→管理员接单／解决→员工回查。检查了页面脚本、样式限制及图像／标注层几何对齐。最终结果、五张截图、视频位于[incidents_r3/browser/](incidents_r3/browser/)。浏览器使用隔离数据和原文检索模式。

代码报告：[test-summary.json](test-summary.json)。经验增量维护验证：[experience_validation/result.json](experience_validation/result.json)。总验收：[verification.json](verification.json)。

## 服务与存储

原应用已在http://127.0.0.1:8768更新，账号与数据保留，PID3413437。临时验收服务已停止；三轮GPU模型服务全部释放。运行产物、OCR工具和模型缓存均在/data/zjr/enterprise_kb_agent/。

CRM启用：--harness-version v6 --execution-adapter v4 --supervisor-strategy experience。中文业务v6可设置KB_SUPERVISOR_STRATEGY=experience使用审核经验。原线上问答策略由KB_AGENT_POLICY选择，故障材料功能可独立使用。
