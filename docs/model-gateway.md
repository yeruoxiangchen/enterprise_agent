# 模型网关接入

2026-11-16 的应用调用链为：

```text
浏览器 → FastAPI /api/chat → business_native_v1
       → DeepAgents 主管与专业 Agent
       → OpenAI 兼容 Go 模型网关 → Qwen3-8B
```

网关作为独立服务提供应用 Key、请求编号、Token 额度与用量记录。
生成接口和 Token 计数接口可以分别配置凭证，配置名见
[`private.env.example`](../private.env.example)。实际 Key 保存在本机环境文件。

本轮真实 HTTP 流程覆盖带引用检索、专业 Agent 创建草稿、用户确认、管理员解决、
同一会话追问。10 次模型请求完成；应用、网关、账户均记录 28,268 Token，临时占用为零。
[逐项检查](releases/20261116/gateway-checks.json)。

网关地址使用现有模型 URL 配置项；直接连接 OpenAI 兼容模型服务同样适用。
独立网关的实现和部署由其各自仓库维护，本仓库包含应用侧接入代码。
