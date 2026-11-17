"""Checks for diagnostic evidence integrity, not model-quality scores."""
import json

import httpx
import pytest

from scripts.native_rl_diagnostic import WireCapture, message_signature, payload_contract


def test_wire_capture_preserves_request_bytes_and_excludes_credentials(tmp_path):
    capture = WireCapture(tmp_path)
    raw = b'{ "messages": [{"role":"user","content":"Leave it unchanged."}], "model": "unit" }'
    request = httpx.Request("POST", "http://unit.invalid/v1/chat/completions",
                            content=raw, headers={"Authorization": "Bearer private-secret"})
    capture.request(request)
    response = httpx.Response(200, request=request, json={
        "choices": [{"message": {"content": "Unchanged."}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2},
    })
    capture.response(response)
    row = capture.rows[0]
    assert row["request_text"].encode() == raw
    assert row["response"]["choices"][0]["message"]["content"] == "Unchanged."
    saved = (tmp_path / "wire.jsonl").read_text()
    assert "private-secret" not in saved and "Authorization" not in saved
    assert response.json()["usage"]["prompt_tokens"] == 12


def test_signature_normalizes_tool_ids_and_json_spacing_but_keeps_arguments():
    first = {"content": None, "tool_calls": [{"id": "one", "function": {
        "name": "get_ticket", "arguments": '{ "ticket_id": "TCK-1001" }'}}]}
    second = {"content": "", "tool_calls": [{"id": "two", "function": {
        "name": "get_ticket", "arguments": '{"ticket_id":"TCK-1001"}'}}]}
    assert message_signature(first) == message_signature(second)
    second["tool_calls"][0]["function"]["arguments"] = '{"ticket_id":"TCK-1002"}'
    assert message_signature(first) != message_signature(second)


def test_contract_checks_actual_messages_and_never_counts_tool_descriptions_as_user_input():
    original = "If it is unassigned, leave it unchanged."
    payload = {"messages": [{"role": "user", "content": original}], "tools": []}
    assert payload_contract(payload, [original])["all_user_messages_present"]
    payload = {"messages": [{"role": "user", "content": "Reassign it."}],
               "tools": [{"function": {"name": "unit_tool", "description": original}}]}
    assert not payload_contract(payload, [original])["all_user_messages_present"]


def test_signature_compares_sdk_parsed_arguments_with_raw_provider_call():
    raw = {"tool_calls": [{"function": {"name": "close_ticket",
        "arguments": json.dumps({"ticket_id": "TCK-1002"})}}]}
    parsed = {"tool_calls": [{"name": "close_ticket", "args": {"ticket_id": "TCK-1002"}}]}
    assert message_signature(raw) == message_signature(parsed)


@pytest.mark.parametrize("binding", [False, True])
def test_actual_chat_client_transmits_user_condition_and_observed_state_to_fork(
        tmp_path, monkeypatch, binding):
    """Controlled provider checks serialization/handoff, with real CRM reads."""
    import asyncio
    from pathlib import Path
    import yaml
    from langchain_openai import ChatOpenAI
    from harness.tools.environment import create_environment
    import harness.tools.domains
    from kb.benchmark.crm_native_adapter import NativeCRMAdapter
    from kb.config import Settings
    from kb.context import ContextBudget

    capture = WireCapture(tmp_path / "wire")
    original = "Reassign TCK-1004 to admin.rlee. If it is unassigned, leave it unchanged."
    capture.user_messages = [original]
    proposed = "Reassign it if unassigned." if binding else original
    replies = iter([
        {"name": "get_ticket", "args": {"ticket_id": "TCK-1004"}},
        {"name": "task", "args": {"subagent_type": "ticket_operations__tickets", "description": proposed}},
        "The ticket is unassigned and unchanged.",
        "The ticket is unassigned and unchanged.",
    ])

    def provider(request):
        next_reply = next(replies)
        message = {"role": "assistant", "content": next_reply if isinstance(next_reply, str) else None}
        if isinstance(next_reply, dict):
            message["tool_calls"] = [{"id": "call-" + next_reply["name"], "type": "function",
                "function": {"name": next_reply["name"], "arguments": json.dumps(next_reply["args"])}}]
        return httpx.Response(200, json={"id": "chat-unit", "object": "chat.completion",
            "created": 1, "model": "unit", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})

    client = httpx.Client(transport=httpx.MockTransport(provider),
        event_hooks={"request": [capture.request], "response": [capture.response]})
    model = ChatOpenAI(model="unit", base_url="http://unit.invalid/v1", api_key="unit",
        http_client=client, callbacks=[capture.callback()], max_retries=0)
    root = Path("/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench")
    monkeypatch.setattr(ContextBudget, "count", lambda *args: (100, "controlled-unit"))
    adapter = NativeCRMAdapter(create_environment("itsm"), Settings(root=tmp_path),
        yaml.safe_load((root / "src/agents/itsm.yaml").read_text()),
        (root / "src/tasks/policies/itsm_policy.md").read_text(), tmp_path / "actor",
        model=model, fork_subagents=True, operation_permissions=True, prepare_operations=True,
        specialist_owned_checks=True, require_dependency_confirmation=True)
    adapter.engine.bind_user_request = binding
    try:
        asyncio.run(adapter.run(original, []))
        assert len(capture.rows) == 4
        child = capture.rows[2]
        assert child["input_contract"]["all_user_messages_present"]
        messages = json.dumps(child["payload"]["messages"], ensure_ascii=False)
        assert "assigned_to" in messages and "TCK-1004" in messages
        assert all(row["sdk_matches_provider"] for row in capture.rows)
        if binding:
            assert proposed in messages
            assert 'authoritative_customer_requests' in messages
            assert 'Original user requests take priority' in messages
        assert adapter.environment.get_state()["tickets"]["TCK-1004"]["assigned_to"] == ""
    finally:
        asyncio.run(adapter.aclose())
        client.close()
