import json

import httpx
import pytest

from kb.config import Settings
from kb.context import ContextBudget


@pytest.mark.parametrize('configured,expected',[('', 'policy'),('expert','expert')])
def test_tokenizer_endpoint_uses_its_own_model_alias(monkeypatch,configured,expected):
    def handle(request):
        assert json.loads(request.content)['model']==expected
        return httpx.Response(200,json={'count':19,'max_model_len':8192})
    original=httpx.Client
    monkeypatch.setattr('kb.context.httpx.Client',lambda **kwargs:
        original(transport=httpx.MockTransport(handle),**kwargs))
    cfg=Settings(llm_model='policy',tokenize_model=configured,
        tokenize_url='http://tokenizer.local/tokenize',tokenize_key='')
    assert ContextBudget(cfg).count([{'role':'user','content':'hello'}],[])==(19,'server_tokenizer')
