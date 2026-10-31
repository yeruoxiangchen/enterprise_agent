"""QAnything v2 HTTP boundary; no deployment or local stand-in is implied.

Reference: netease-youdao/QAnything, qanything-v2/docs/API.md (AGPL-3.0).
Caller is trusted server code: it resolves identities and allowed KBs, not the browser.
"""
import re
from urllib.parse import urlsplit
import httpx


class QAnythingClient:
    def __init__(self, endpoint, user_id, settings, allowed_kb_ids=(), client=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
            raise ValueError('Invalid backend URL')
        if parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('This experimental adapter permits loopback only')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', user_id):
            raise ValueError('Invalid backend namespace')
        self.url, self.user_id, self.settings = endpoint.rstrip('/'), user_id, settings
        self.allowed = frozenset(allowed_kb_ids)
        self.owns_client = client is None
        self.client = client or httpx.Client(timeout=settings.timeout, trust_env=False, follow_redirects=False)

    def close(self):
        if self.owns_client:
            self.client.close()

    def _request(self, name, body=None, files=None):
        body = {'user_id': self.user_id, **(body or {})}
        try:
            response = self.client.post(self.url+'/api/local_doc_qa/'+name,
                **({'data': body, 'files': files} if files else {'json': body}))
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError):
            # Upstream exceptions may echo request keys or document content.
            raise RuntimeError('QAnything request failed: '+name) from None
        if not isinstance(result, dict) or result.get('code') != 200:
            raise RuntimeError('QAnything rejected request: '+name)
        return result

    def health(self):
        return self._request('list_knowledge_base')

    def create_base(self, name):
        return self._request('new_knowledge_base', {'kb_name': name})['data']

    def _check(self, kb_ids):
        if not kb_ids or not set(kb_ids) <= self.allowed:
            raise PermissionError('KB scope must be authorized by the host')

    def upload_text(self, kb_id, filename, text):
        self._check([kb_id])
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.txt', filename):
            raise ValueError('Use a canonical .txt file name')
        # No request retries: an upload timeout may have created a server-side file.
        return self._request('upload_files', {'kb_id': kb_id, 'mode': 'soft'},
                             files={'files': (filename, text.encode('utf-8'), 'text/plain')})

    def list_files(self, kb_id, page_id=1):
        self._check([kb_id])
        return self._request('list_files', {'kb_id': kb_id, 'page_id': page_id, 'page_limit': 100})

    def chat(self, question, history, kb_ids):
        self._check(kb_ids)
        pairs = []
        for i in range(0, len(history), 2):
            if i+1 >= len(history) or history[i]['role'] != 'user' or history[i+1]['role'] != 'assistant':
                raise ValueError('QAnything history must be complete user/assistant pairs')
            pairs.append([history[i]['content'], history[i+1]['content']])
        cfg = self.settings
        result = self._request('local_doc_chat', {'kb_ids': list(kb_ids), 'question': question,
            'history': pairs, 'streaming': False, 'networking': False, 'rerank': True,
            'hybrid_search': True, 'only_need_search_results': False, 'api_base': cfg.llm_url,
            'api_key': cfg.llm_key or 'local-not-used', 'api_context_length': cfg.context_tokens,
            'max_token': cfg.output_tokens, 'model': cfg.llm_model, 'temperature': 0, 'top_p': 1})
        if not isinstance(result.get('response'), str):
            raise RuntimeError('QAnything response schema changed')
        for key in ('source_documents', 'retrieval_documents'):
            for doc in result.get(key, []):
                if doc.get('kb_id') not in self.allowed or doc.get('kb_id') not in kb_ids:
                    raise PermissionError('Backend returned an unknown or unauthorized source')
        # Sources are raw evidence, NOT automatically treated as verified citations.
        return {k: result.get(k) for k in ('response', 'source_documents', 'retrieval_documents', 'time_record', 'tokens')}
