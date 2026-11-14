"""Fresh-process crash worker for graph checkpoints and durable business receipts."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

import yaml
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite import SqliteSaver
from harness.tools.environment import create_environment
import harness.tools.domains

from kb.benchmark import crm_adapter
from kb.config import Settings
from kb.context import ContextBudget


class PlanningModel(BaseChatModel):
    @property
    def _llm_type(self):
        return 'checkpoint-crash-planner'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        supervisor = 'coordinate three specialists' in str(messages[0].content).lower()
        tools = [m for m in messages if m.type == 'tool']
        if supervisor and not any((m.name or '').startswith('delegate_') for m in tools):
            name, args = 'delegate_ticket_operations__tickets', {'request': 'Add the requested comment.'}
        elif not supervisor and not any(m.name == 'add_ticket_comment' for m in tools):
            name, args = 'add_ticket_comment', {'ticket_id': 'TCK-1001', 'comment': 'Once'}
        else:
            msg = AIMessage(content='The requested operation has been processed.',
                            usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
            return ChatResult(generations=[ChatGeneration(message=msg)])
        msg = AIMessage(content='', tool_calls=[{'id': name, 'name': name, 'args': args}],
                        usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
        return ChatResult(generations=[ChatGeneration(message=msg)])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('phase', choices=['committed', 'unknown'])
    parser.add_argument('mode', choices=['crash', 'resume'])
    parser.add_argument('--delay-checkpoints', action='store_true')
    args = parser.parse_args()
    args.folder.mkdir(exist_ok=True, parents=True)
    if args.mode == 'crash' and args.delay_checkpoints:
        # Force process death while graph checkpoints still lag behind the
        # already-persisted business journal, rather than a graceful unwind.
        for method in ['put', 'put_writes']:
            original = getattr(SqliteSaver, method)
            def delayed(*a, _original=original, **kw):
                time.sleep(3)
                return _original(*a, **kw)
            setattr(SqliteSaver, method, delayed)
    upstream = Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
    saved = args.folder/'environment.json'
    state = json.loads(saved.read_text()) if saved.exists() else {}
    crm_adapter.ChatOpenAI = lambda **kw: PlanningModel()
    ContextBudget.count = lambda *a, **kw: (100, 'crash-test')
    owner = crm_adapter.CRMAdapter(arm='multi_v6', execution_adapter='v4', environment=create_environment('itsm', state),
        settings=Settings(root=args.folder, llm_url='http://unused.invalid/v1', llm_model='crash-test',
                          context_tokens=8192, output_tokens=1400),
        config=yaml.safe_load((upstream/'src/agents/itsm.yaml').read_text()),
        policy=(upstream/'src/tasks/policies/itsm_policy.md').read_text(), seed=42, output=args.folder)
    actual = owner.registry.execute_with_status
    attempts = args.folder/'backend-writes.jsonl'

    async def execute(name, arguments):
        result = await actual(name, arguments)
        if name == 'add_ticket_comment':
            with attempts.open('a') as stream:
                stream.write(json.dumps(arguments)+'\n')
                stream.flush()
                os.fsync(stream.fileno())
            if args.mode == 'crash' and args.phase == 'unknown':
                # Represent an external backend that committed before its
                # acknowledgement reached the harness.
                owner.write_environment()
                os._exit(78)
        return result
    owner.registry.execute_with_status = execute
    emit = owner.emit

    def observe(kind, value):
        emit(kind, value)
        if (args.mode == 'crash' and args.phase == 'committed' and kind == 'external_tool'
                and value.get('business_outcome') == 'committed'):
            os._exit(77)
    owner.emit = observe
    response = asyncio.run(owner.run('Add note "Once" to TCK-1001.', []))
    result = {'answer': response.content, 'status': owner.execution_outcomes[-1]['status'],
        'comments': owner.environment.get_state()['tickets']['TCK-1001']['comments'],
        'backend_writes': len(attempts.read_text().splitlines())}
    (args.folder/'resumed.json').write_text(json.dumps(result)+'\n')


if __name__ == '__main__':
    main()
