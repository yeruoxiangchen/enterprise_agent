import httpx
import pytest

from kb.config import Settings
from kb.context import ContextBudget


@pytest.mark.parametrize("tokenizer_key,expected", [
    (None, "Bearer generation-gateway-key"),
    ("upstream-tokenizer-key", "Bearer upstream-tokenizer-key"),
    ("", None),
])
def test_tokenizer_credentials_are_independent(monkeypatch, tokenizer_key, expected):
    original = httpx.Client
    seen = []

    def handle(request):
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"count": 222, "max_model_len": 8192})

    monkeypatch.setattr("kb.context.httpx.Client", lambda **kwargs:
                        original(transport=httpx.MockTransport(handle), **kwargs))
    cfg = Settings(llm_key="generation-gateway-key", tokenize_key=tokenizer_key,
                   tokenize_url="http://tokenizer/tokenize")
    count, mode = ContextBudget(cfg).count([{"role": "user", "content": "test"}], [])
    assert (count, mode) == (222, "server_tokenizer")
    assert seen == [expected]
