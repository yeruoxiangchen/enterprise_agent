# 固定 RAG 默认流程切换记录

2026-10-30 用户授权将项目主线收敛到固定 RAG。本次已完成代码、验证及在线网页切换。

## 当前行为

用户问题 → 授权范围内 BM25 检索 → CPU BGE 重排 → top-5 资料 → 一次模型生成 → 引用/权限检查 → 原子提交。

默认策略 `fixed_rag_v1` 复用已经验证的固定 RAG 阶段，明确设置 `enable_followup=False`。普通题、复合题、资料不足、需要澄清都不会触发 Agent 补查或二次改写。一次请求最多预留一次生成调用；失败或中断的调用也计数，恢复不会偷偷再次生成。输出不合法时明确报告失败，不编造引用或自动改走另一条链路。

资料不足仍需要模型判断，固定流程不保证所有拒答或回答都正确。工程验收不替代真实业务正确率评估。

文档上传、解析、知识库及角色权限、会话历史、原文引用、取消、反馈保持原应用机制。旧 Agent / fixed-first / 多 Agent 代码与实验记录保留为显式候选，不在默认流程执行。

内部 `KB_MODE=agent` 是旧接口的模型问答兼容字段；真正控制执行的是 `KB_AGENT_POLICY=fixed_rag_v1`。登录后 `/api/me` 返回 `workflow=fixed_rag` 和 `reranking=true`，网页显示“知识库问答”。

## 改动位置

- `kb/config.py`：默认策略及在线 CPU 重排配置读取。
- `kb/agent.py`、`kb/agentpack_runtime.py`：固定策略分发、会话、检查点与原子提交。
- `kb/backends/fixed_first_rag.py`：关闭补查时只允许一次预留生成调用，恢复不扩大预算。
- `kb/reranking.py`：CPU worker 初始化及请求/响应串行保护，避免网页搜索和后台问答并发时串用评分。
- `kb/app.py`、`kb/web/app.js`：公开当前工作流，显示知识库问答说明。
- `scripts/run.sh`：在线命令读取独立 `app.env`、使用对应运行环境；实验包装脚本不加载该在线配置。
- `scripts/fixed_rag_ops.py`：检查、隔离真实模型验收、带备份的切换及登录后验证；切换失败自动恢复先前配置。

## 验证证据

1. 50 项回归通过，另 1 项并发重排测试通过；Shell、Python、JavaScript 语法检查通过。
2. 最终代码在独立数据库完成真实模型 API 验收：
   - 虚构住宿政策问题回答“600 元”，引用正确；一次生成、0 工具调用、0 补查。
   - 缺失的企业 Git 地址问题返回 `insufficient`；一次生成、0 工具调用、0 补查。
   - 普通成员访问管理员知识库被拒绝，公开问答未泄露验收识别码。
   - 文档上传解析、登录、问答读取、引用和反馈成功；验收未向线上会话写入测试问题。
3. 验收记录源码指纹；部署前核对一致性。切换前确认问答和入库队列均为 0。
4. 切换后真实 HTTP 检查：网页 200、既有账号登录成功、工作流及重排配置正确，原有 1 个知识库、3 份文档可访问。

验收与发布记录位于 `/data/zjr/enterprise_kb_agent/artifacts/fixed_rag_release_20261030/`：

- `smoke_receipt.json`：最终真实模型验收与源码指纹。
- `latest.json`：发布结果、进程、备份位置和验证摘要。
- `deploy-1793335567144057061/knowledge.sqlite3`：切换前一致性数据库备份。
- 同目录 `rollback.env`：之前应用模式及 Python 环境的恢复配置；权限 600。

原 `private.env`、账号密码、上传文件和历史实验输出未改写。没有停止/重启模型服务，没有新增 GPU 模型或训练任务。

## 已运行状态与使用

发布时进程 PID `3890698`，地址 `http://127.0.0.1:8768`，模型 `kb-qwen3-8b`，CPU 重排已开启。此 PID 是发布时快照，之后以状态命令为准。

线上配置：`/data/zjr/enterprise_kb_agent/app.env`。应用使用已有 `/data/zjr/enterprise_kb_agent/venv-agentpack/bin/python`。本次没有重建运行环境。

```bash
cd /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent
bash scripts/run.sh status
bash scripts/run.sh fixed-rag verify
```

刷新原网页即可使用；原 VS Code / SSH 的 8768 端口转发继续有效。无需启动评测队列。

若需要恢复先前模式，先通过 `fixed-rag inspect` 确认没有活动请求，再停止本项目网页、用上面备份目录的 `rollback.env` 覆盖 `app.env`（保持 600 权限），然后 `bash scripts/run.sh start`。不要恢复数据库来撤销单纯的策略配置，否则会丢弃切换后的新会话。
