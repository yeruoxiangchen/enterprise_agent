"""Application adapter for the same DeepHarness used in public ITSM tests."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import time

from langchain_openai import ChatOpenAI

from kb.context import ContextBudget
from .engine import DeepHarness
from .memory import WorkflowStore
from .ports import ApplicationPort


def execute(h,run_id):
    db=h.db
    if not db.execute("UPDATE runs SET status='running',updated_at=? WHERE id=? AND status='queued'",
                      (time.time(),run_id)):return
    run=db.one('SELECT * FROM runs WHERE id=?',(run_id,))
    user=db.one('SELECT id,role,username FROM users WHERE id=?',(run['user_id'],))
    memory=None
    try:
        port=ApplicationPort(db,h.settings,user,json.loads(run['base_ids']),run_id)
        model=ChatOpenAI(model=h.settings.llm_model,base_url=h.settings.llm_url,
            api_key=h.settings.llm_key or 'local',temperature=0,max_tokens=h.settings.output_tokens,
            timeout=h.settings.timeout,max_retries=0,
            extra_body={'chat_template_kwargs':{'enable_thinking':False}})
        scope=hashlib.sha256(json.dumps([user,port.base_ids],sort_keys=True).encode()).hexdigest()[:20]
        workspace=h.settings.root/'harness/conversations'/run['conversation_id']/scope
        memory=WorkflowStore(Path(h.settings.harness_workflow_store) if h.settings.harness_workflow_store
            else h.settings.root/'harness/workflows.sqlite3') if h.settings.supervisor_strategy=='experience' else None
        engine=DeepHarness(model=model,port=port,workspace=workspace,namespace=run['conversation_id'],
            memory=memory,context_budget=ContextBudget(h.settings),goal_review=h.settings.harness_goal_review,
            max_calls=h.settings.harness_model_calls,max_tools=h.settings.harness_tool_calls,
            compact_handoffs=h.settings.harness_compact_handoffs,
            event=lambda k,v:db.event(run_id,k,v),cancelled=lambda:h.check_cancel(run_id))
    except Exception as exc:
        if memory:memory.close()
        h.checkpoint(run_id,{'policy':'business_deep_v1','business':{'resumable':False}})
        db.execute('UPDATE runs SET status=?,error=?,updated_at=? WHERE id=?',
                   ('error',type(exc).__name__,time.time(),run_id))
        db.event(run_id,'harness_initialization_error',{'kind':type(exc).__name__,'message':str(exc)[:500]})
        return
    try:
        result=engine.invoke(run['question'],request_id=run_id)
        # Restore persistent search evidence through the same ACL-checked API.
        for effect in engine.journal.records():
            for evidence in effect['result'].get('evidence',[]) if isinstance(effect['result'],dict) else []:
                label=evidence['source']
                current=port.retriever.chunk(user,evidence['id'],port.base_ids)
                if current['text']!=evidence['text']:raise ValueError('Source changed; request fresh evidence')
                port.sources[label]=current
        citations=list(dict.fromkeys(re.findall(r'\[(S\d+)\]',result.answer)))
        status={'completed':'answered','needs_user_input':'clarify','refused':'insufficient',
                'partial':'partial_success','needs_reconciliation':'insufficient'}[result.status]
        receipts=port.receipts or [{'action':e['name'],'evidence_id':e['id']} for e in engine.journal.records()]
        state={'policy':'business_deep_v1','sources':port.sources,'user':user,'base_ids':port.base_ids,
               'business':{'receipts':receipts,'remaining_work':result.remaining_work,
                   'deferred_user_actions':result.deferred_user_actions,
                   'goal_id':engine.active_goal.id,'control_passes':engine.control_passes,
                   'resumable':result.status in ('partial','needs_reconciliation'),
                   'workspace':str(workspace),'calls':engine.turn_calls,'tools':engine.turn_tools,
                   'max_calls':engine.max_calls,'max_tools':engine.max_tools,
                   'completed_tasks':{e['id']:{'result':{'status':'answered'}}
                       for e in engine.journal.records() if e['status'] in ('observed','committed')}}}
        h.finish(run,state,result.answer,citations,status)
    except BaseException as exc:
        from kb.agent import Cancelled
        status='cancelled' if isinstance(exc,Cancelled) else 'error'
        h.checkpoint(run_id,{'policy':'business_deep_v1','business':{
            'resumable':status=='error','calls':engine.turn_calls,'tools':engine.turn_tools,
            'max_calls':engine.max_calls,'max_tools':engine.max_tools,
            'workspace':str(workspace),'remaining_work':['Resume the existing native checkpoint.']}})
        db.execute('UPDATE runs SET status=?,error=?,updated_at=? WHERE id=?',
                   (status,type(exc).__name__,time.time(),run_id))
        db.event(run_id,status,{'kind':type(exc).__name__,'message':str(exc)[:500]})
        if not isinstance(exc,Exception):raise
    finally:
        engine.close()
        if memory:memory.close()
