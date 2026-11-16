# 2026-11-16 源码与演示发布

当前应用为企业多模态知识问答与服务协作多 Agent，默认业务核心
`business_native_v1` 复用 DeepAgents/LangGraph 主管、专业委派、上下文和检查点，
应用层实现资料/附件权限、引用核验、工单草稿、显式确认、幂等回执及网页状态。
最新代码加入 OpenAI 兼容模型网关的生成与 Token 计数凭证配置。

## 版本与复现

- [固定简历源码](https://github.com/yeruoxiangchen/enterprise_agent/tree/resume-20261116)
- [最新 main](https://github.com/yeruoxiangchen/enterprise_agent/tree/main)
- [源码文件及 SHA-256 清单](source-manifest.json)
- [本次发布检查](verification.json)
- [实际网页操作录屏与截图](../../demos/README.md)

运行入口、依赖与环境示例保留在根 README、`requirements*.lock` 和
`private.env.example`；模型权重、数据库、运行日志和实际凭证使用本机数据目录。

## 简历指标对应材料

| 项目能力与结果 | 冻结记录 |
| --- | --- |
| Pro Git 证据检索 Recall@5 83.33%、Recall@10 92.26% | [汇总](retrieval-metrics.json)、根目录 EVAL_PROGRESS_20261028.md |
| 100 道技术知识问答自动评分 93% | 根目录 EVAL_PROGRESS_20261028.md 的 dev_v41 记录 |
| ITSM66：53/66；官方 GraphAgent 48/66 | [r2 完整协议与原始汇总](../20261114/itsm66_r2/report.md)，执行核心为当时的 v6/r2 |
| 原生网页业务 4/4、截图/日志 HTTP 2/2、工程检查 68 项 | [口径与耗时](native-web-metrics.json)、[逐项行为](browser-checks.json)、[实现说明](../../../HARNESS_WEB_INTEGRATION_20261115.md) |
| 模型网关 10 次请求，三处用量同为 28,268 Token | [用量与逐项检查](gateway-checks.json)、[接入说明](../../model-gateway.md) |

当前发布汇集源码与历史验收入口，表内指标按对应日期、任务协议和执行核心解读。
11 月 15 日验收时的小范围源码指纹保留在 [acceptance-source-20261115.json](acceptance-source-20261115.json)；
本次完整源码清单单独记录。历史 ITSM 与记忆策略记录继续保留。
