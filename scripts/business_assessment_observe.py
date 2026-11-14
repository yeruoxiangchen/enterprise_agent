"""Evaluation-process-only callbacks; no change to the deployed application."""
from contextlib import contextmanager
import json
import time
from unittest.mock import patch

from langchain_core.callbacks import BaseCallbackHandler
from kb.agent import Harness
from kb.backends.business_multi_v2 import BusinessQA


class Capture(BaseCallbackHandler):
    raise_error = True

    def __init__(self, db, run_id):
        self.db, self.run_id = db, run_id

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.db.event(self.run_id, 'assessment_model_input', {
            'call_id': str(run_id), 'messages': [
                {'role': m.type, 'content': m.content} for m in messages[0]]})

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = (response.llm_output or {}).get('token_usage', {})
        if not usage:
            message = response.generations[0][0].message
            metadata = getattr(message, 'usage_metadata', None) or {}
            usage = {'prompt_tokens': metadata.get('input_tokens'), 'completion_tokens': metadata.get('output_tokens')}
        self.db.event(self.run_id, 'assessment_model_end', {'call_id': str(run_id), 'usage': usage})

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.db.event(self.run_id, 'assessment_model_error', {'call_id': str(run_id), 'error': type(error).__name__})


@contextmanager
def instrument():
    original_init, original_execute = BusinessQA.__init__, Harness.execute

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.model.callbacks = list(self.model.callbacks or []) + [Capture(self.db, self.run['id'])]

    def execute(self, run_id):
        self.db.event(run_id, 'assessment_execution_start', {'unix': time.time()})
        try:
            return original_execute(self, run_id)
        finally:
            self.db.event(run_id, 'assessment_execution_end', {'unix': time.time()})

    with patch.object(BusinessQA, '__init__', init), patch.object(Harness, 'execute', execute):
        yield


def observation(db, run_id):
    events = [{'kind': e['kind'], 'payload': json.loads(e['payload']), 'created_at': e['created_at']}
              for e in db.all('SELECT kind,payload,created_at FROM events WHERE run_id=? ORDER BY id', (run_id,))]
    starts = [e['payload']['unix'] for e in events if e['kind'] == 'assessment_execution_start']
    ends = [e['payload']['unix'] for e in events if e['kind'] == 'assessment_execution_end']
    calls = [e for e in events if e['kind'] == 'assessment_model_input']
    returned = [e for e in events if e['kind'] == 'assessment_model_end']
    complete = len(calls) == len(returned) and all(
        type(e['payload']['usage'].get(k)) is int for e in returned for k in ('prompt_tokens', 'completion_tokens'))
    row = db.one('SELECT created_at FROM runs WHERE id=?', (run_id,))
    usage = {'complete': complete, 'started_calls': len(calls), 'returned_calls': len(returned)}
    for key in ('prompt_tokens', 'completion_tokens'):
        usage[key] = sum(e['payload']['usage'].get(key, 0) or 0 for e in returned)
    return {'usage': usage, 'queue_seconds': max(0, starts[0]-row['created_at']) if starts else None,
            'execution_seconds': ends[-1]-starts[0] if starts and ends else None,
            'model_inputs': [e['payload'] for e in calls],
            'domain_inputs': [e['payload'] for e in events if e['kind'] == 'business_input']}
