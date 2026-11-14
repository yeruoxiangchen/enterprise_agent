# 2026-11-14 发布：主管多Agent执行与检查点优化

源码版本：`business_v6_external_v4_supervisor_r2_perf1`。同步当前应用、ITSM评测适配、主管/专业Agent、执行保护、回放定位工具、测试与过程文档。

## 可浏览的验收材料

- [完整66题r2报告](itsm66_r2/report.md)、[结构化成绩](itsm66_r2/summary.json)、[冻结协议](itsm66_r2/protocol.json)、[失败案例审阅](itsm66_r2/review.md)：两组132次，候选53/66、参考48/66；模型、模拟用户和裁判共享Qwen3-8B。
- [检查点优化报告](latency/report.md)、[固定响应回放](latency/replay-comparison.json)：112次模型调用的两轮对照，总墙钟198.66→163.63秒。
- [真实模型四题验证](latency/real-validation.json)：候选4/4、参考2/4，双方异常及未判定0。
- [148项代码测试报告](latency/verified-tests.xml)、[验证汇总](latency/verification.json)：包含强制退出后的恢复与重复写入保护。
- [最新实现及结果说明](../../../CRM_LATENCY_PERF1_20261114.md)、[当前进度与待办](../../../CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

完整66题分数对应r2；perf1的本轮真实模型范围为四题，性能主结果为受控回放。应用默认仍为固定RAG，最新主管实现通过ITSM适配运行；产品接入和跨轮待办问题记录在当前进度文档。

源码清单见[source-manifest.json](source-manifest.json)。此发布不包含模型、数据库、账号凭据或运行缓存。历史过程档案继续保留在`docs/process/`。
