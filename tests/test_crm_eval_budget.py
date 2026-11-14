"""The judge must receive all evidence while fitting the actual server context."""
import asyncio
import json

import httpx
import pytest

from crm_bench_services import LocalEvalClient
from harness.models import ChatMessage, Role


@pytest.mark.parametrize('count,server_limit,expected', [
    (4566, 8192, 3562), (4228, 8192, 3900), (1000, 8192, 4096),
    (4228, 6144, 1852),
])
def test_exact_prompt_budget_prevents_previous_judge_http400(tmp_path, monkeypatch, count, server_limit, expected):
    client = LocalEvalClient('http://local.invalid', 'test', tmp_path/'judge.jsonl', 42, context_tokens=8192)
    sent = []
    messages = [ChatMessage(role=Role.SYSTEM, content='Judge the entire conversation.'),
                ChatMessage(role=Role.USER, content='FULL immutable evidence: 09:00–17:00.')]
    original = [m.content for m in messages]
    async def post(url, *, json):
        sent.append((url, json))
        if url.endswith('/tokenize'):
            body = {'count': count, 'max_model_len': server_limit}
        else:
            assert json['max_tokens'] + count + 64 <= server_limit
            body = {'choices': [{'message': {'content': '{"pass":true}'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': count, 'completion_tokens': 10, 'total_tokens': count + 10}}
        return httpx.Response(200, request=httpx.Request('POST', url), json=body)
    monkeypatch.setattr(client.client, 'post', post)
    async def run():
        try:
            return await client.chat(messages, max_tokens=4096, response_format={
                'type': 'json_schema', 'json_schema': {'name': 'test', 'schema': {
                    'type': 'object', 'properties': {'pass': {'type': 'boolean'}}, 'required': ['pass']}}})
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(sent) == 2 and sent[1][1]['max_tokens'] == expected
    assert sent[0][1]['messages'] == sent[1][1]['messages']
    assert 'OUTPUT FORMAT' in sent[0][1]['messages'][0]['content']
    assert sent[1][1]['messages'][1]['content'] == original[1]
    assert [m.content for m in messages] == original
    assert client.usage['num_calls'] == client.metered_calls == 1
    assert client.usage['failed_calls'] == 0
    assert json.loads((tmp_path/'judge.jsonl').read_text())['context_budget']['output_tokens'] == expected


@pytest.mark.parametrize('kind', ['no_room', 'tokenizer_error'])
def test_insufficient_context_preserves_evidence_and_does_not_send_completion(tmp_path, monkeypatch, kind):
    client = LocalEvalClient('http://local.invalid', 'test', tmp_path/'judge.jsonl', 42, context_tokens=8192)
    sent = []
    async def post(url, *, json):
        sent.append(url)
        assert url.endswith('/tokenize')
        return httpx.Response(500 if kind == 'tokenizer_error' else 200, request=httpx.Request('POST', url),
                              json={'count': 8190, 'max_model_len': 8192})
    monkeypatch.setattr(client.client, 'post', post)
    async def run():
        try:
            await client.chat([ChatMessage(role=Role.USER, content='Keep this entire evidence.')])
        finally:
            await client.aclose()
    with pytest.raises((RuntimeError, httpx.HTTPError)):
        asyncio.run(run())
    assert len(sent) == 1
    assert client.usage['num_calls'] == client.metered_calls == 0
    assert client.usage['failed_calls'] == 1
    audit = json.loads((tmp_path/'judge.jsonl').read_text())
    assert audit['request']['messages'][0]['content'] == 'Keep this entire evidence.'
    assert audit['response'] is None and audit['error']
