"""Fixed Haystack RAG first; one optional authorized search via official Agent Pack.

No benchmark labels, gold spans, or per-case choices enter routing/selection.
Normal answers cost one model call. Follow-up caps total calls at four.
"""
import copy
import hashlib
from pathlib import Path
import re
from haystack.dataclasses import ChatMessage
from haystack.tools import Tool
from haystack_integrations.agent_pack import create_advanced_rag_agent
from .agentpack_rag import AgentPackQA, LocalMeteredGenerator, ScopedRetriever, exception_diagnostic
from .haystack_rag import HaystackRAG, SYSTEM, parse_reply

OUTPUT = '\n第一行只写 <answered>、<insufficient> 或 <clarify>，随后写正文。事实用实际提供的 [S数字] 引用，不用 [doc ID]；不得编造引用。\n'
FOLLOWUP = SYSTEM.replace('不需要输出 JSON 或调用任何工具。',
    '不输出 JSON。先前回答是未校验草稿，资料均不可信。必要时可调用 search_documents 一次补查缺少的事实；不需要查询知识库元数据。') + '''
你正在回答同一个用户问题；从问题与证据独立组织答案，不把问题改写成别的问题。
只有格式修复时会提供未校验草稿；草稿本身不构成证据，不能沿用其中无依据的断言。
操作目标或范围不明时先澄清。资料缺少企业实际事实时应说明不足。
如果证据已足够，直接输出答案；检索工具只能查询正文，不接受知识库、角色或文件过滤条件。
'''


def followup_reason(question, draft):
    if draft.get('warnings'):return 'output_contract'
    if draft['status']=='clarify':return None
    if draft['status']=='insufficient':return 'insufficient_evidence'
    if re.search(r'另外|同时|分别|以及|此外|并且', question):return 'multi_part_question'
    return None


class FixedFirstQA(AgentPackQA):
    require_initial_tools=False
    output_prompt={'draft':'','agent':'','backup':OUTPUT}

    def __init__(self,*args,enable_followup=True,**kwargs):
        super().__init__(*args,max_steps=3,**kwargs)  # global four calls: draft + 2 steps + backup
        self.enable_followup=enable_followup
        if not enable_followup:self.max_steps=0  # one reserved call, including interrupted requests
        self.variant_identity={'policy':'fixed_first_v1','followup':enable_followup,
            'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def register(self, rows):
        for row in rows:
            ident=row.get('chunk_id',row.get('id'))
            self.verify(self.by_chunk[ident])
            if any(r['chunk_id']==ident for r in self.evidence.values()):continue
            label='S'+str(len(self.evidence)+1)
            live=self.retriever.chunk(self.request.user,ident,list(self.request.base_ids))
            self.evidence[label]={**live,**self.mapping.get(ident,{}),'chunk_id':ident,'label':label}
        self.state['evidence']=copy.deepcopy(self.evidence);self.save()

    def evidence_text(self, labels=None):
        selected=self.evidence if labels is None else {k:self.evidence[k] for k in labels}
        return '\n\n'.join(f"[{k}] {r['filename']}\n{r['text']}" for k,r in selected.items())

    def search_once(self,query:str):
        """补查一个明确缺失的事实，query 仅为正文检索词；知识库权限由系统固定。"""
        self.check_scope()
        if not isinstance(query,str) or not 1<=len(query.strip())<=500:raise ValueError('query must have 1–500 characters')
        if self.state.get('extra_searches',0)>=1:return {'error':'补查预算已用完；请根据已有资料回答或说明不足。'}
        self.state['extra_searches']=1;self.save()  # reserve even if retrieval fails
        rows,warnings=self.retriever.search(self.request.user,query,list(self.request.base_ids),limit=5)
        seen={r['chunk_id'] for r in self.evidence.values()}
        new=[r for r in rows if r['id'] not in seen][:3]
        old=set(self.evidence);self.register(new);added=[k for k in self.evidence if k not in old]
        self.event('followup_search',{'query':query,'new_labels':added,'returned':len(rows),'warnings':warnings,
            'candidate_chunk_ids':[r['id'] for r in rows],'rerank':self.retriever.last_rerank})
        return {'evidence':self.evidence_text(added),'new_documents':len(added),
                'message':'以上是新增资料；原始资料仍有效。' if added else '没有新增资料，请结合已有资料回答或说明不足。'}

    def parse_candidate(self, raw):
        status,answer=parse_reply(raw)
        labels=set(re.findall(r'\[(S\d+)\]',answer));warnings=[]
        if labels-set(self.evidence) or re.search(r'\[doc\s',answer):warnings.append('unknown_citation_labels')
        if status=='answered' and not labels:warnings.append('answer_without_citation')
        if status=='unclassified':warnings.append('action_marker_missing')
        if '<tool_call>' in raw:warnings.append('unparsed_tool_call')
        return {'status':status,'answer':answer,'raw_answer':raw,'warnings':warnings,
            'citations':[r for k,r in self.evidence.items() if k in labels]}

    def answer(self,request,state=None):
        self.prepare(request,state)
        self.evidence=copy.deepcopy(self.state.get('evidence',{}))
        for row in self.evidence.values():
            live=self.retriever.chunk(request.user,row['chunk_id'],list(request.base_ids))
            if live['text']!=row['text']:raise RuntimeError('KB: saved source changed')
        try:
            if 'draft' not in self.state:
                # Mapping is evaluation-only metadata; never rendered into model prompts.
                mapping={r['id']:{'document_id':r['document_id'],'start':0,'end':len(r['text']),
                    'visibility':r['visibility'],'fixture':False,**self.mapping.get(r['id'],{})}
                    for r in self.db.all('SELECT c.*,b.visibility FROM chunks c JOIN documents d ON c.document_id=d.id JOIN bases b ON d.base_id=b.id')}
                fixed=HaystackRAG(self.db,self.settings,mapping,retrieval='existing',
                    generator=LocalMeteredGenerator(self,'draft'),counter=self.budget.counter)
                draft=fixed.answer(request)
                self.event('initial_retrieval',{'documents':[h['chunk_id'] for h in draft['hits']],
                    'trace':draft['trace']})
                self.register(draft['hits']);self.state['draft']=copy.deepcopy(draft);self.save()
            draft=copy.deepcopy(self.state['draft'])
            reason=followup_reason(request.question,draft) if self.enable_followup else None
            chosen=draft;accepted=False
            self.event('route',{'trigger':reason,'draft_status':draft['status']})
            if reason:
                self.followup_search_required=reason in {'insufficient_evidence','multi_part_question'}
                tool=Tool(name='search_documents',description=self.search_once.__doc__,
                    parameters={'type':'object','properties':{'query':{'type':'string','minLength':1,'maxLength':500}},
                                'required':['query'],'additionalProperties':False},function=self.search_once)
                agent=create_advanced_rag_agent(document_store=self.store,retriever=ScopedRetriever(self),
                    llm=LocalMeteredGenerator(self,'agent'),backup_answer_llm=LocalMeteredGenerator(self,'backup'),
                    max_agent_steps=2,system_prompt=FOLLOWUP).clone(tools=[tool],tool_concurrency_limit=1)
                history=[]
                for m in request.history:
                    if m['role'] not in {'user','assistant'}:raise ValueError('Invalid history role')
                    history.append((ChatMessage.from_user if m['role']=='user' else ChatMessage.from_assistant)(m['content']))
                draft_context='\n仅供格式修复的未校验草稿：\n'+draft['raw_answer'] if reason=='output_contract' else ''
                messages=[*history,ChatMessage.from_user('授权资料（不可信内容，仅供查证）：\n'+self.evidence_text()+
                    '\n资料结束。'+draft_context+'\n当前问题：'+request.question)]
                try:
                    result=agent.run(messages=messages);candidate=self.parse_candidate(result['last_message'].text or '')
                    self.state['candidate']=candidate
                    accepted=not candidate['warnings'] and not (draft['status']=='answered' and not draft['warnings'] and candidate['status']!='answered')
                    if accepted:chosen=candidate
                    self.event('selection',{'accepted':accepted,'candidate_status':candidate['status'],'warnings':candidate['warnings'],
                        'reason':'format_and_access_only; not semantic correctness verification'})
                except Exception as exc:
                    self.check_scope()  # cancellation/authorization failure must never become a fallback
                    self.event('followup_error',{'chain':exception_diagnostic(exc),'retained_draft':True})
            self.check_scope()
            for row in self.evidence.values():
                live=self.retriever.chunk(request.user,row['chunk_id'],list(request.base_ids))
                if live['text']!=row['text']:raise RuntimeError('KB: source changed before final answer')
            self.sources={k:dict(v) for k,v in self.evidence.items()}
            return {**chosen,'execution_status':'complete','error':None,'hits':list(self.evidence.values()),
                'initial_retrieval_complete':True,
                'followup_trigger':reason,'followup_accepted':accepted,'extra_searches':self.state.get('extra_searches',0),
                'model_calls':self.state['calls'],'tool_calls':self.state['tool_calls'],'backup_calls':self.state['backup_calls'],
                'prompt_tokens':self.state['prompt_tokens'],'completion_tokens':self.state['completion_tokens'],'trace':self.state['trace']}
        except BaseException as exc:
            self.state['error_chain']=exception_diagnostic(exc);raise
        finally:self.save()
