"""Count successful model responses per role through native LangChain callbacks."""
from threading import Lock
from langchain_core.callbacks import BaseCallbackHandler


class RoleUsage(BaseCallbackHandler):
    run_inline=True
    raise_error=True

    def __init__(self,role,counts,lock):
        self.role,self.counts,self.lock=role,counts,lock
        counts[role]={'started':0,'completed':0}

    def on_chat_model_start(self,*args,**kwargs):
        with self.lock:self.counts[self.role]['started']+=1

    def on_llm_end(self,*args,**kwargs):
        with self.lock:self.counts[self.role]['completed']+=1
