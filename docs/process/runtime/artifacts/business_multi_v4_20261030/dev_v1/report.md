# 多Agent v4：官方结构化结果组件修复与同批对照

实际复用 LangChain create_agent/ToolStrategy 的解析、ToolMessage校验反馈和structured_response；LangGraph保留图状态/检查点。
正文与来源字段分开，主机按已校验来源渲染引用；纯证据不足答复不伪造引用。来源合法不等于事实被支持。
自建开发数据，同模型同知识资料；调用次数可不同。没有使用正式测试集，不覆盖原v3结果。

| 阶段 | 正常返回 | 真实服务动作 | 草稿原文 | 权限检查 | 模型调用 | p50秒 |
|---|---|---|---|---|---|---|
|multi_v3|20/23|4/4|2/2|23/23|36|5.17|
|multi_v4|23/23|4/4|2/2|23/23|29|14.64|
|new_acceptance|13/13|4/4|2/2|13/13|19|9.87|
|regression|17/17|1/1|1/1|17/17|22|14.02|
|single|23/23|3/4|0/0|23/23|26|4.93|

## 执行失败与业务失败
- single/diagnose-ticket/0: ['service_complete']; error=None
- multi_v3/proxy-access/0: ['execution']; error=ValueError
- multi_v3/auth-loan/0: ['execution']; error=ValueError
- multi_v3/actual-proxy/0: ['execution']; error=ValueError

## 新版语义评审
普通咨询按回答完成度评价，不要求创建工单；明确办理请求才校验真实回执。新评分与v3旧评分分开。
- multi_v3: {"correctness": {"pass": 0, "fail": 0, "unknown": 23}, "citation_support": {"pass": 0, "fail": 0, "unknown": 23}, "task_completion": {"pass": 0, "fail": 0, "unknown": 23}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 23}}
- multi_v4: {"correctness": {"pass": 0, "fail": 0, "unknown": 23}, "citation_support": {"pass": 0, "fail": 0, "unknown": 23}, "task_completion": {"pass": 0, "fail": 0, "unknown": 23}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 23}}
- new_acceptance: {"correctness": {"pass": 0, "fail": 0, "unknown": 13}, "citation_support": {"pass": 0, "fail": 0, "unknown": 13}, "task_completion": {"pass": 0, "fail": 0, "unknown": 13}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 13}}
- regression: {"correctness": {"pass": 0, "fail": 0, "unknown": 17}, "citation_support": {"pass": 0, "fail": 0, "unknown": 17}, "task_completion": {"pass": 0, "fail": 0, "unknown": 17}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 17}}
- single: {"correctness": {"pass": 0, "fail": 0, "unknown": 23}, "citation_support": {"pass": 0, "fail": 0, "unknown": 23}, "task_completion": {"pass": 0, "fail": 0, "unknown": 23}, "factual_fidelity": {"pass": 0, "fail": 0, "unknown": 23}}
工程: {
  "status": "passed",
  "url": "http://127.0.0.1:8774",
  "load": [
    {
      "concurrency": 1,
      "requests": 24,
      "success": 24,
      "p50_seconds": 0.03470973018556833,
      "p95_seconds": 0.058165499940514565,
      "observed_rps": 25.594266827862977,
      "scope": "authenticated read APIs, client creation included; not model generation throughput"
    },
    {
      "concurrency": 4,
      "requests": 24,
      "success": 24,
      "p50_seconds": 0.12019506189972162,
      "p95_seconds": 0.145054267719388,
      "observed_rps": 33.05223928881957,
      "scope": "authenticated read APIs, client creation included; not model generation throughput"
    },
    {
      "concurrency": 8,
      "requests": 24,
      "success": 24,
      "p50_seconds": 0.24439659900963306,
      "p95_seconds": 0.2732255030423403,
      "observed_rps": 31.040381531183595,
      "scope": "authenticated read APIs, client creation included; not model generation throughput"
    }
  ],
  "real_questions": [
    {
      "run_id": "188ea736681349498976ffaa0ecc373b",
      "status": "answered",
      "seconds": 18.451157415285707,
      "has_citations": true
    },
    {
      "run_id": "b442157a6b27410db47945d967d40066",
      "status": "answered",
      "seconds": 28.673608860000968,
      "has_citations": true
    }
  ],
  "browser": {
    "status": "passed",
    "url": "http://127.0.0.1:8774",
    "ticket_id": "76e45608a3464c9bbd4cc7ed7b36f304",
    "checks": [
      "login",
      "business_badge",
      "real_model_draft",
      "trace",
      "confirm",
      "accept",
      "resolve"
    ],
    "page_errors": [],
    "at": 1793361387.8339355
  }
}
评分校准/状态: {
  "status": "calibration_failed",
  "reason": "停止扩量评审，保留逐题复核材料"
}

## 官方参考与复用
- https://docs.langchain.com/oss/python/langchain/multi-agent/router
- https://docs.langchain.com/oss/python/langchain/multi-agent/subagents
- https://docs.langchain.com/oss/python/langchain/structured-output
库源代码读取位置：business-packages-v2/langchain/agents/structured_output.py 与 factory.py。具体版本见 requirements.business.lock。
没有引入额外自由规划Supervisor，不增加未验证的协作步骤；保留领域路由、工单原文、权限和真实回执。
