"""Public ITSM bridge; uses the application's unchanged shared DeepHarness."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from langchain_openai import ChatOpenAI

from kb.context import ContextBudget
from kb.harness.engine import DeepHarness
from kb.harness.ports import CRMPort


class DeepCRMAdapter:
    def __init__(self,environment,settings,config,policy,output,seed=42,memory=None,model=None):
        self.environment,self.settings=environment,settings
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True)
        self.seed=seed;self.turn=0;self.stop=threading.Event();self.turn_times=[]
        self.execution_outcomes=[];self.tool_calls=[]
        self.port=CRMPort(environment,config,policy)
        model=model or ChatOpenAI(model=settings.llm_model,base_url=settings.llm_url,
            api_key=settings.llm_key or 'local',temperature=0,max_tokens=settings.output_tokens,
            timeout=settings.timeout,max_retries=0,
            extra_body={'chat_template_kwargs':{'enable_thinking':False},'seed':seed})
        self.engine=DeepHarness(model=model,port=self.port,workspace=self.output,
            namespace='conversation',memory=memory,context_budget=ContextBudget(settings),
            cancelled=self.check_cancel,goal_review=settings.harness_goal_review,
            max_calls=settings.harness_model_calls,max_tools=settings.harness_tool_calls,
            compact_handoffs=settings.harness_compact_handoffs)

    def check_cancel(self):
        if self.stop.is_set():raise RuntimeError('Evaluation cancelled')

    def emit(self,kind,value):
        self.engine.emit(kind,value)

    def __getattr__(self,name):
        names={'model_calls':'calls','prompt_tokens':'prompt_tokens','completion_tokens':'completion_tokens',
               'usage_missing_calls':'missing_usage','llm_seconds':'llm_seconds'}
        if name in names:return getattr(self.engine,names[name])
        raise AttributeError(name)

    def write_environment(self):
        (self.output/'environment.json').write_text(json.dumps(self.environment.get_state(),ensure_ascii=False))

    async def run(self,user_message,history):
        from harness.models import AgentResponse
        started=time.monotonic();before=len(self.port.audit);calls=self.model_calls
        collected=False
        def collect():
            attempted=[];linked=set()
            for record in self.engine.attempts:
                # Native structured-output and orchestration tools remain in
                # engine events. Only the actual business catalogue crosses
                # the benchmark API boundary.
                if record['name']not in self.engine.specs:continue
                backend=next((c for c in self.port.audit[before:] if c['id']==record.get('effect_id')),None)
                if backend:linked.add(backend['id'])
                executed=bool(backend and backend.get('backend_executed'))
                attempted.append({**record,**(backend or {}),'turn':self.turn,
                                  'execution_index':len(self.tool_calls)+len(attempted),
                                  'backend_executed':executed,'executed':executed,'seconds':0.0})
            for record in self.port.audit[before:]:
                if record['id'] not in linked:
                    attempted.append({**record,'turn':self.turn,'execution_index':len(self.tool_calls)+len(attempted),
                                      'executed':bool(record.get('backend_executed')),'seconds':0.0})
            self.tool_calls.extend(attempted)
            return attempted
        job=asyncio.create_task(asyncio.to_thread(self.engine.invoke,user_message,str(self.turn)))
        try:
            result=await asyncio.shield(job)
            attempted=collect();collected=True
            self.execution_outcomes.append({'turn':self.turn,'status':result.status,'remaining':result.remaining_work,
                'goal_id':self.engine.active_goal.id,'control_passes':self.engine.control_passes})
            self.write_environment()
            return AgentResponse(content=result.answer,tool_calls_made=attempted,
                                 iterations=self.model_calls-calls)
        except asyncio.CancelledError:
            self.stop.set()
            try:await asyncio.wait_for(asyncio.shield(job),self.settings.timeout+10)
            except (Exception,asyncio.CancelledError):pass
            raise
        finally:
            if not collected:collect()
            self.turn_times.append(time.monotonic()-started);self.turn+=1

    async def aclose(self):
        self.engine.close()
