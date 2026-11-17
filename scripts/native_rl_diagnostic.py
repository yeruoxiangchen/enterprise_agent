"""Frozen-source, real-model input/replay audit before agent post-training.

Run with the native harness environment and an isolated frozen source on PYTHONPATH.
Captures final ChatOpenAI HTTP bodies without authentication headers, compares SDK
parsing, replays identical request bytes, and re-executes actual backend effects.
No policy, task assertions, model output, or production service is modified.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(path)


def message_signature(message):
    """Compare actions without request-dependent tool-call IDs or JSON spacing."""
    calls = []
    for call in message.get("tool_calls", []):
        function = call.get("function", call)
        arguments = function.get("arguments", function.get("args", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {"unparsed_arguments": arguments}
        calls.append({"name": function.get("name"), "arguments": arguments})
    return {"content": message.get("content") or "", "tool_calls": calls}


def payload_contract(payload, user_messages):
    texts = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(part.get("text", "") for part in content if isinstance(part, dict))
    combined = "\n".join(texts)
    present = [text in combined for text in user_messages]
    return {"all_user_messages_present": all(present), "user_messages_present": present,
            "missing_user_messages": [text for text, found in zip(user_messages, present) if not found],
            "message_count": len(payload.get("messages", [])),
            "tool_names": [tool["function"]["name"] for tool in payload.get("tools", [])],
            "note": "Exact presence diagnostic; structured HITL approvals and summaries require separate review."}


class WireCapture:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.rows = []
        self.local = threading.local()
        self.lock = threading.RLock()
        self.user_messages = []

    def request(self, request):
        if not request.url.path.endswith("/chat/completions"):
            return
        raw = request.content
        with self.lock:
            index = len(self.rows)
            row = {"index": index, "started_at": time.time(),
                   "metadata": copy.deepcopy(getattr(self.local, "metadata", {})),
                   "request_text": raw.decode(), "request_sha256": hashlib.sha256(raw).hexdigest(),
                   "payload": json.loads(raw)}
            row["input_contract"] = payload_contract(row["payload"], self.user_messages)
            self.rows.append(row)
            request.extensions["native_audit_index"] = index
            self.local.index = index
            write(self.folder / f"request-{index:03d}.json", row)

    def response(self, response):
        index = response.request.extensions.get("native_audit_index")
        if index is None:
            return
        response.read()
        with self.lock:
            row = self.rows[index]
            row.update(status_code=response.status_code, completed_at=time.time())
            try:
                row["response"] = response.json()
            except ValueError:
                row["response_text"] = response.text
            with (self.folder / "wire.jsonl").open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def callback(self):
        from langchain_core.callbacks import BaseCallbackHandler
        capture = self

        class Observe(BaseCallbackHandler):
            run_inline = True

            def on_chat_model_start(self, serialized, messages, **kwargs):
                metadata = kwargs.get("metadata") or {}
                capture.local.metadata = {key: metadata.get(key) for key in (
                    "lc_agent_name", "langgraph_node", "langgraph_checkpoint_ns", "langgraph_step")}
                capture.local.metadata["run_id"] = str(kwargs.get("run_id"))

            def on_llm_end(self, result, **kwargs):
                index = getattr(capture.local, "index", None)
                if index is None:
                    return
                message = result.generations[0][0].message
                parsed = {"content": message.content, "tool_calls": message.tool_calls}
                with capture.lock:
                    row = capture.rows[index]
                    row["sdk_message"] = parsed
                    choices = row.get("response", {}).get("choices", [])
                    row["sdk_matches_provider"] = bool(choices) and (
                        message_signature(choices[0]["message"]) == message_signature(parsed))
                    write(capture.folder / f"parsed-{index:03d}.json", {
                        "sdk_message": parsed, "sdk_matches_provider": row["sdk_matches_provider"]})

        return Observe()


def score_controls(upstream):
    """Actual tools + original scorer: valid no-op passes, forbidden attempt fails."""
    import yaml
    from harness.bench_runner import ConversationTrace, load_tasks
    from harness.tools.environment import create_environment
    import harness.tools.domains  # noqa: F401
    from kb.harness.ports import CRMPort
    from crm_benchmark import finish_deterministic_checks
    from harness_native_worker import deterministic_checks

    tasks = {task.id: task for task in load_tasks(str(upstream / "src/tasks/itsm.yaml"))}
    config = yaml.safe_load((upstream / "src/agents/itsm.yaml").read_text())
    policy = (upstream / "src/tasks/policies/itsm_policy.md").read_text()
    plans = [
        ("lookup", "itsm_ops_001_lookup_ticket_by_id", [("get_ticket", {"ticket_id": "TCK-1001"})], True),
        ("conditional_noop", "itsm_ops_010_reassign_unassigned_ticket_fails",
         [("get_ticket", {"ticket_id": "TCK-1004"})], True),
        ("conditional_forbidden_attempt", "itsm_ops_010_reassign_unassigned_ticket_fails",
         [("get_ticket", {"ticket_id": "TCK-1004"}),
          ("reassign_ticket", {"ticket_id": "TCK-1004", "assignee": "admin.rlee"})], False),
    ]
    results = []
    for label, task_id, plan, expected in plans:
        task = tasks[task_id]
        environment = create_environment("itsm", task.initial_state)
        port = CRMPort(environment, config, policy)
        port.begin_request("Controlled execution check using canonical tool arguments.", "0")
        initial = copy.deepcopy(environment.get_state())
        for index, (name, arguments) in enumerate(plan):
            port.execute(name, arguments, str(index))
        calls = [{**call, "execution_index": i} for i, call in enumerate(port.audit)]
        adapter = SimpleNamespace(environment=environment, tool_calls=calls)
        trace = ConversationTrace(task_id=task_id, expected_actions=list(task.expected_actions))
        finish_deterministic_checks(task, trace, adapter)
        checks = deterministic_checks(task, trace)
        observed = all(checks.values())
        results.append({"case": label, "expected_pass": expected, "observed_pass": observed,
                        "checks": checks, "state_unchanged": initial == environment.get_state(),
                        "backend_execution": [call["backend_executed"] for call in calls],
                        "passed": observed == expected})
    return results


def replay_requests(capture, service, limit=1):
    selected = [row for row in capture.rows if row.get("sdk_message", {}).get("tool_calls")]
    children = [row for row in selected if "get_business_hours" not in row["input_contract"]["tool_names"]]
    selected = (children or selected or capture.rows)[:limit]
    results = []
    with httpx.Client(timeout=95, trust_env=False) as client:
        for row in selected:
            raw = row["request_text"].encode()
            attempts = []
            for repeat in range(2):
                started = time.monotonic()
                response = client.post(service["url"] + "/v1/chat/completions", content=raw,
                    headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
                response.raise_for_status()
                body = response.json()
                original = row["response"]["choices"][0]["message"]
                actual = body["choices"][0]["message"]
                attempts.append({"repeat": repeat, "response": body,
                    "same_signature": message_signature(original) == message_signature(actual),
                    "original_signature": message_signature(original),
                    "replayed_signature": message_signature(actual),
                    "prompt_tokens_equal": body.get("usage", {}).get("prompt_tokens") ==
                        row["response"].get("usage", {}).get("prompt_tokens"),
                    "seconds": time.monotonic() - started})
            results.append({"request_index": row["index"], "request_sha256": hashlib.sha256(raw).hexdigest(),
                            "request_bytes_unchanged": hashlib.sha256(raw).hexdigest() == row["request_sha256"],
                            "attempts": attempts})
    return results


async def run_case(case, arm, output, service):
    import yaml
    from langchain_openai import ChatOpenAI
    from harness.bench_runner import ConversationTrace
    from harness.tools.environment import create_environment
    import harness.tools.domains  # noqa: F401
    from kb.benchmark.crm_native_adapter import NativeCRMAdapter
    from kb.config import Settings
    from crm_benchmark import finish_deterministic_checks
    from harness_native_cases import expanded_gate_ready, mechanism_checks
    from harness_native_worker import task_for_case, deterministic_checks
    from harness_native_reuse import replay_business

    upstream = Path("/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench")
    task = task_for_case(case)
    folder = output / "cases" / case["task_id"] / arm
    capture = WireCapture(folder)
    cfg = Settings(root=folder, mode="agent", llm_url=service["url"] + "/v1",
        llm_model=service["model"], llm_key="local", tokenize_url=service["url"] + "/tokenize",
        context_tokens=8192, output_tokens=1400, timeout=90, harness_model_calls=40,
        harness_tool_calls=48, harness_goal_review=False, harness_compact_handoffs=True)
    http_client = httpx.Client(timeout=95, trust_env=False,
        event_hooks={"request": [capture.request], "response": [capture.response]})
    model = ChatOpenAI(model=cfg.llm_model, base_url=cfg.llm_url, api_key="local", temperature=0,
        max_tokens=cfg.output_tokens, timeout=cfg.timeout, max_retries=0, http_client=http_client,
        callbacks=[capture.callback()],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}, "seed": 20261114})
    adapter = NativeCRMAdapter(create_environment("itsm", task.initial_state), cfg,
        yaml.safe_load((upstream / "src/agents/itsm.yaml").read_text()),
        (upstream / "src/tasks/policies/itsm_policy.md").read_text(), folder, model=model,
        fork_subagents=True, operation_permissions=True, require_dependency_confirmation=True,
        prepare_operations=True, specialist_owned_checks=True)
    if arm == "verbatim_binding":
        adapter.engine.bind_user_request = True
    trace = ConversationTrace(task_id=task.id, expected_actions=list(task.expected_actions))
    started = time.monotonic()
    error = gate_blocked = None
    row = None
    try:
        for turn, item in enumerate(case["turns"]):
            if item.get("when") and not expanded_gate_ready(item["when"], adapter):
                gate_blocked = item["when"]
                break
            trace.turns.append({"role": "user", "content": item["message"]})
            capture.user_messages = [m["content"] for m in trace.turns if m["role"] == "user"]
            kwargs = {"approval": item["approval"]} if item.get("approval") else {}
            response = await asyncio.wait_for(adapter.run(item["message"], trace.turns[:-1], **kwargs), 180)
            trace.turns.append({"role": "assistant", "content": response.content})
            finish_deterministic_checks(task, trace, adapter)
            checks = {**deterministic_checks(task, trace), **mechanism_checks(case, adapter, trace.turns)}
            if all(checks.values()):
                break
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        finish_deterministic_checks(task, trace, adapter)
        checks = {**deterministic_checks(task, trace), **mechanism_checks(case, adapter, trace.turns)}
        row = {"task_id": task.id, "case_kind": case["kind"], "arm": arm,
               "passed": error is None and gate_blocked is None and all(checks.values()),
               "checks": checks, "error": error, "gate_blocked": gate_blocked,
               "tokens": adapter.prompt_tokens + adapter.completion_tokens,
               "model_calls": adapter.model_calls, "usage_missing_calls": adapter.usage_missing_calls,
               "seconds": time.monotonic() - started, "trace": asdict(trace),
               "audit": adapter.port.audit, "effects": adapter.engine.journal.records(),
               "environment": adapter.environment.get_state(), "wire": capture.rows,
               "wire_sdk_matches": all(r.get("sdk_matches_provider") is True for r in capture.rows),
               "output": str(folder)}
        try:
            row["backend_replay_calls"] = await replay_business(row, upstream)
            row["backend_replay_passed"] = True
        except Exception as exc:
            row["backend_replay_passed"] = False
            row["backend_replay_error"] = f"{type(exc).__name__}: {exc}"
        write(folder / "result.json", row)
        await adapter.aclose()
        http_client.close()
    if error is None:
        try:
            row["direct_replays"] = await asyncio.to_thread(replay_requests, capture, service)
        except Exception as exc:
            row["direct_replay_error"] = f"{type(exc).__name__}: {exc}"
    write(folder / "result.json", row)
    return row


def report(output, rows, controls, status):
    arms = {}
    for arm in sorted({row["arm"] for row in rows}):
        selected = [row for row in rows if row["arm"] == arm]
        replay = [attempt for row in selected for record in row.get("direct_replays", [])
                  for attempt in record["attempts"]]
        arms[arm] = {"completed": len(selected), "passed": sum(row["passed"] for row in selected),
                     "execution_errors": sum(bool(row["error"]) for row in selected),
                     "tokens": sum(row["tokens"] for row in selected),
                     "model_calls": sum(row["model_calls"] for row in selected),
                     "wire_calls": sum(len(row["wire"]) for row in selected),
                     "sdk_parsing_match": all(row["wire_sdk_matches"] for row in selected),
                     "backend_replay_match": all(row["backend_replay_passed"] for row in selected),
                     "direct_replay_calls": len(replay),
                     "direct_replay_same_signature": sum(r["same_signature"] for r in replay),
                     "direct_replay_same_prompt_tokens": sum(r["prompt_tokens_equal"] for r in replay)}
    summary = {"status": status, "scope": "fixed scripted ITSM development diagnostics; no new training",
               "controls": controls, "arms": arms, "completed": len(rows),
               "failures": [{key: row.get(key) for key in ("task_id", "case_kind", "arm", "checks", "error", "output")}
                            for row in rows if not row["passed"]]}
    write(output / "summary.json", summary)
    write(output / "rows.json", rows)
    lines = ["# 冻结原生 Agent 的 RL 前诊断", "", summary["scope"], "",
             "|配置|通过/完成|执行异常|SDK解析一致|业务重放一致|直接重放动作一致|",
             "|---|---:|---:|---|---|---:|"]
    for arm, value in arms.items():
        lines.append(f"|{arm}|{value['passed']}/{value['completed']}|{value['execution_errors']}|"
                     f"{value['sdk_parsing_match']}|{value['backend_replay_match']}|"
                     f"{value['direct_replay_same_signature']}/{value['direct_replay_calls']}|")
    lines += ["", "基线逐项采用冻结配置；verbatim_binding 是单独配置对照，仅打开已有的用户原文绑定。",
              "每个直接重放使用相同请求字节、模型端点、采样参数，不执行生成的工具调用。",
              "字节与 Token 数一致但输出不同需检查推理非确定性，不能自动归因到 Agent 框架。",
              "条件、权限、确认、取消、多步顺序、身份续办、状态核验及成本分别审阅。",
              "", "## 任务记录", ""]
    for row in rows:
        lines.append(f"- {row['arm']} / {row['case_kind']}: {row['passed']}; "
                     f"Token={row['tokens']}; calls={row['model_calls']}; error={row['error']}")
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return summary


async def main(args):
    import kb
    import importlib.metadata
    from crm_bench_services import ModelServices, gpu_snapshot, gpu_has_room
    from harness_native_cases import CASES, GUARD_CASES, EXTRA_CASES

    output = Path(args.output).resolve()
    source = Path(args.source).resolve()
    if not output.is_relative_to(Path("/data/zjr/enterprise_kb_agent")):
        raise ValueError("Use project data storage")
    if not Path(kb.__file__).resolve().is_relative_to(source):
        raise RuntimeError("Actor imports must come from the frozen source")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "started.json").exists():
        raise RuntimeError("Keep existing results; select a new output directory")
    os.umask(0o077)
    started = time.time()
    deadline = started + args.max_minutes * 60
    cases = [*CASES, *GUARD_CASES, *EXTRA_CASES]
    jobs = [(case, "frozen_crm") for case in cases]
    jobs += [(case, "verbatim_binding") for case in cases if case["kind"] in
             {"reassign_unassigned", "conditional_continuation", "assessment_only"}]
    protocol = {"source_commit": "a7e379dd3a60b4fc60acf2c660c61205245ce2ac",
                "cases": cases, "jobs": [(case["task_id"], arm) for case, arm in jobs],
                "model": "Qwen3-8B", "context_tokens": 8192, "output_tokens": 1400,
                "seed": 20261114, "temperature": 0, "tensor_parallel_size": 1,
                "max_minutes": args.max_minutes, "workflow_memory": False,
                "role_selection": "Frozen role scopes; no role is trained in this diagnostic.",
                "production_config_difference": "Application enables literal user binding; CRM constructor defaults to disabled. Tools differ between ports.",
                "dependencies": {name: importlib.metadata.version(name) for name in
                                 ["deepagents", "langgraph", "langchain-openai", "httpx"]}}
    write(output / "protocol.json", protocol)
    state = {"pid": os.getpid(), "status": "running", "started_at": started, "completed": 0,
             "total": len(jobs), "deadline_at": deadline}
    write(output / "started.json", state)

    def progress(stage, **extra):
        state.update(stage=stage, updated_at=time.time(), **extra)
        write(output / "status.json", state)
        print(json.dumps(state), flush=True)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    services = ModelServices(output, progress)
    rows = []
    controls = []
    try:
        controls = await asyncio.to_thread(score_controls,
            Path("/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench"))
        write(output / "score-controls.json", controls)
        if not all(row["passed"] for row in controls):
            raise RuntimeError("Scoring control failed before model startup")
        stats = gpu_snapshot()
        candidates = args.gpus.split(",") if args.gpus != "auto" else sorted(stats, key=lambda g: -stats[g]["free_mib"])
        gpu = next((g for g in candidates if g in stats and gpu_has_room(stats[g], args.memory_fraction)), None)
        if gpu is None:
            raise RuntimeError("No GPU meets the explicit service memory budget")
        services.start("actor", gpu, max_model_len=8192, memory_fraction=args.memory_fraction)
        progress("loading_model", gpu=gpu, memory_fraction=args.memory_fraction)
        await asyncio.wait_for(services.ready(), timeout=min(900, deadline-time.time()-60))
        for case, arm in jobs:
            if time.time() + 120 > deadline:
                progress("time_budget_complete", status="time_budget_complete_review_required")
                break
            progress("case", task=case["task_id"], arm=arm, completed=len(rows))
            row = await run_case(case, arm, output, services.owned["actor"])
            rows.append(row)
            report(output, rows, controls, "running")
            progress("case_complete", completed=len(rows), task_passed=row["passed"])
            if row["error"] and any(word in row["error"] for word in ["Connection", "ConnectError", "OutOfMemory"]):
                raise RuntimeError("Model service failure; preserve results and stop")
        else:
            progress("complete", status="complete_review_required", task=None, arm=None)
    except BaseException as exc:
        progress("failed", status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        await services.close()
        progress(state["stage"], resources_released=True, completed=len(rows))
        report(output, rows, controls, state["status"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--gpus", default="auto")
    parser.add_argument("--memory-fraction", type=float, default=.78)
    parser.add_argument("--max-minutes", type=float, default=25)
    args = parser.parse_args()
    sys.path[:0] = [str(Path(args.source).resolve()), str(Path(args.source).resolve() / "scripts")]
    asyncio.run(main(args))
