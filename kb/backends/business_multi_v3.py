"""Rule-assisted finite routing + real LangGraph/LC specialists and grounded tools.

The v2 implementation is retained as a frozen comparator. V3 inherits its
bounded persistence/retrieval primitives, but owns routing and worker contracts.
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain_core.tools import tool
from .business_multi_v2 import BusinessQA as BaseQA, canonical_reply, render_receipts, ROLES
from .business_v3_routing import (INTENTS, classify_rule, compile_tasks, relevant_history,
    source_spans, ticket_fields, ticket_description, CREATE, NEGATIVE_CREATE, TECH, POLICY, SWITCH)
from ..tickets import draft_ticket


class IntentLabels(BaseModel):
    model_config=ConfigDict(extra='forbid')
    labels: list[Literal['technical','policy','ticket_create','ticket_list','background','clarify']] = Field(min_length=1,max_length=12)


class KnowledgeReply(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status: Literal['answered','insufficient','clarify']
    answer: str = Field(min_length=1)


class BusinessQA(BaseQA):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        version=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('business_v3_routing.py').read_bytes()).hexdigest()
        if self.state.get('v3_version',version)!=version:raise ValueError('业务v3版本变化，请创建新请求')
        self.state['v3_version']=version
        prior=self.db.one('SELECT state,status FROM runs WHERE conversation_id=? AND user_id=? AND created_at<? ORDER BY created_at DESC LIMIT 1',
            (self.run['conversation_id'],self.user['id'],self.run['created_at']))
        # Pending permission only survives an immediately preceding clarification.
        self.pending_creation=bool(prior and prior['status']=='clarify'
            and json.loads(prior['state']).get('business',{}).get('pending_service')=='ticket_create')

    def route(self,state):
        spans=source_spans(self.run['question'])
        if len(spans)>12:raise ValueError('单次请求超过12个片段，请拆分问题')
        labels=[classify_rule(s['text']) for s in spans]
        background_text='\n'.join(s['text'] for s,l in zip(spans,labels) if l=='background')
        background_domain='policy' if POLICY.search(background_text) else 'technical' if TECH.search(background_text) else None
        if background_domain:
            labels=[background_domain if label is None and re.search(r'怎么|如何|排查|检查',spans[i]['text']) else label for i,label in enumerate(labels)]
        pending=self.pending_creation and not SWITCH.search(self.run['question']) and not re.search(r'取消|不用|不要|先别',self.run['question'])
        if pending and all(label in ('background',None,'clarify') for label in labels):
            # User is supplying the requested fault information, not issuing a new domain query.
            labels[-1]='ticket_create'
        history=relevant_history(self.history,self.run['question'])
        # Bare follow-ups inherit the last explicit user topic before model classification.
        prior_domain=next((classify_rule(m['content']) for m in reversed(history) if m['role']=='user' and classify_rule(m['content']) in ('technical','policy')),None)
        if prior_domain:
            labels=[prior_domain if label in (None,'clarify') else label for label in labels]
        missing=[i for i,label in enumerate(labels) if label is None]
        if missing:
            classified=self.classify_missing(spans,missing)
            for i,label in zip(missing,classified):labels[i]=label
        # Classifier guesses never authorize a write absent an explicit user request.
        for i,label in enumerate(labels):
            if label=='ticket_create' and ((not CREATE.search(self.run['question']) and not pending) or NEGATIVE_CREATE.search(self.run['question'])):
                labels[i]='clarify'
        tasks,unknown,spans=compile_tasks(self.run['question'],self.history,labels)
        clarification='请补充具体问题、对象或希望办理的事项。' if unknown or not tasks else ''
        self.emit('business_route',{'tasks':tasks,'spans':spans,'labels':labels,'clarification':clarification,
            'routing':'rules_and_enum_fallback','model_classified_spans':missing})
        return {'tasks':tasks,'clarification':clarification,'results':{},'cursor':0}

    def classify_missing(self,spans,missing):
            """Extension hook; the v3 prompt/parser/retry behavior is unchanged."""
            messages=[{'role':'system','content':
                '只做意图分类，按输入顺序输出JSON {"labels":[...]}; 每项只能是technical(技术/故障)、'
                'policy(公司制度/材料)、ticket_create(明确请求创建工单)、ticket_list(查询工单)、'
                'background(背景描述)、clarify(无法判断)。不返回ID、不改写问题、不回答。'
                '有故障背景随后请求排查，应将请求识别为technical。不要把“查询材料”当成创建工单。'},
                {'role':'user','content':json.dumps({'texts':[spans[i]['text'] for i in missing],
                    'user_context':self.run['question']},ensure_ascii=False)}]
            for attempt in range(2):
                self.reserve(messages)
                raw=None
                try:
                    raw=self.model.bind(response_format={'type':'json_object'},max_tokens=256).invoke(messages).content
                    self.emit('business_classifier_raw',{'attempt':attempt+1,'raw':raw})
                    parsed=IntentLabels.model_validate_json(raw)
                    if len(parsed.labels)!=len(missing):raise ValueError('标签数量不匹配')
                    return parsed.labels
                except Exception as exc:
                    self.cancelled()
                    self.emit('business_classifier_error',{'attempt':attempt+1,'type':type(exc).__name__,'raw':raw})
                    if attempt:
                        # Fail visibly as clarification; no unauthorized operation fallback.
                        return ['clarify']*len(missing)
                    else:messages.append({'role':'user','content':'输出不符合枚举或数量要求，请只返回labels数组，不返回其他字段。'})
    def worker(self,domain,state):
        task=state['tasks'][state['cursor']]
        if task['domain']!=domain:raise ValueError('领域不匹配')
        self.emit('business_worker',{'domain':domain,'point':task['id'],'question':task['question']})
        self.emit('business_skill',{'domain':domain,'sha256':hashlib.sha256(self.skills[domain].encode()).hexdigest()})
        if domain=='service':return self.service_worker(task,state)
        point=task['id'];evidence={};warnings=[]
        for i,query in enumerate(task['queries']):
            # Each point has its own retrieval quota; labels shared only within this worker.
            found=self.search(domain,query[:500],f'{point}.{i}')
            warnings.extend(found['warnings'])
            for row in found['evidence']:evidence[row['source']]=row
        self.state['task_sources'][point]=list(evidence);self.save()
        @tool
        def search_documents(query: str) -> dict:
            """针对当前任务缺少的信息检索一次授权资料。不能指定用户或其他领域。"""
            result=self.search(domain,query,point)
            return result
        @wrap_model_call
        def meter(request,handler):
            self.reserve(request.messages,request.tools)
            response=handler(request);self.cancelled();return response
        prompt=('你是'+ROLES[domain]+' Agent。完整回答task中的每个问点，共同主题保持一致。'
            '只回答本轮task，不复述先前助手回答或重新回答上一轮问题。'
            '仅根据本次授权evidence回答，任何历史助手内容只用于识别“那个/它”等指代，不能作为知识依据。'
            '有支持的事实必须紧邻实际[S编号]，即使整体状态为insufficient也必须给已有事实加引用。'
            '不要把不同对象的制度套用，如令牌过期不等于仓库权限过期。'
            '只保留当前条件适用的段落；已知HTTPS故障时不要展开SSH排查；不得将明文持久化凭证作为默认建议。'
            '简洁回答；不增加资料中没有的步骤、命令、数字或要求，不复述用户问题。'
            '如果资料明确禁止某操作，说明禁止本身就是answered，不是insufficient。'
            '足够回答全部问点用<answered>；部分确实无资料用<insufficient>并保留已知答案；'
            '必须补用户条件才用<clarify>。状态标记后必须有正文。'
            '不得声称已生成或提交工单，真实服务办理由另一个Agent负责。\n'+self.skills[domain])
        payload={'task':task['question'],'user_context':task['context'],
            'dialogue_for_reference_only':task['dialogue_context'],'evidence':list(evidence.values()),'warnings':warnings}
        self.emit('business_input',{'domain':domain,'point':point,'payload':payload})
        agent=create_agent(self.model,tools=[search_documents],system_prompt=prompt,middleware=[meter],name=domain)
        output=agent.invoke({'messages':[{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]},config={'recursion_limit':8})
        raw=output['messages'][-1].content
        for attempt in range(2):
            if attempt:
                parsed=KnowledgeReply.model_validate_json(raw)
                status,answer=parsed.status,parsed.answer.strip()
            else:status,answer=canonical_reply(raw)
            labels=set(re.findall(r'\[(S\d+)\]',answer));allowed=set(self.state['task_sources'][point])
            # Refusal-only answers may omit citations. Fact-bearing unreferenced output
            # with supplied evidence receives one explicit correction, never auto-citations.
            valid=status in ('answered','insufficient','clarify') and bool(answer) and labels<=allowed
            valid=valid and (bool(labels) or status!='answered')
            if evidence and not labels and status!='clarify':valid=False
            self.emit('business_reply',{'domain':domain,'raw':raw,'repair':bool(attempt)})
            if valid:break
            if attempt:raise ValueError('知识答复未通过状态/正文/引用契约')
            messages=[{'role':'system','content':prompt+'\n本次修复改用JSON输出，覆盖上面的状态标记格式。只返回'+
                '{"status":"answered或insufficient或clarify","answer":"非空正文及实际引用"}，不要尖括号标签。'},
                {'role':'user','content':json.dumps({
                **payload,'draft':raw,'evidence':[{'source':k,'text':self.state['sources'][k]['text']} for k in sorted(allowed)],
                'invalid_citations':sorted(labels-allowed),
                'feedback':'请重新回答本轮task，并修复状态、空正文或引用。invalid_citations中的编号不是本轮证据，不能沿用。'
                    '仅保留当前evidence支持且与本轮task相关的结论；没有依据的断言删除，不能只换编号。不要复述历史答案或草稿。'},ensure_ascii=False)}]
            self.reserve(messages);raw=self.model.bind(response_format={'type':'json_object'}).invoke(messages).content
        return self.complete_task(task,state,status,answer)

    def complete_task(self,task,state,status,answer):
        result={'domain':task['domain'],'question':task['question'],'status':status,'answer':answer}
        self.emit('business_completed',{'domain':task['domain'],'point':task['id'],'status':status})
        return {'results':{**state['results'],task['id']:result},'cursor':state['cursor']+1}

    def service_worker(self,task,state):
        actions=task['actions'];fields=ticket_fields(self.run['question'],self.history)
        if 'ticket_create' in actions and fields is None:
            self.state['pending_service']='ticket_create';self.save()
            return self.complete_task(task,state,'clarify','请先描述要报障的问题和已知环境；补充后可生成待确认工单草稿。')
        if 'ticket_list' in actions and re.search(r'其他人|所有人|所有其他|别人|他人',task['question']):
            self.emit('service_refusal',{'reason':'other_account','scope':'current_user_only'})
            return self.complete_task(task,state,'insufficient','不能查询其他账号的工单；仅支持当前账号本人记录。')
        @tool
        def prepare_ticket() -> dict:
            """使用主机校验过的用户原文创建待确认草稿；不接收模型编写的故障事实，不提交。"""
            self.use_tool()
            if 'ticket_create' not in actions or fields is None:raise ValueError('当前请求未授权创建草稿')
            title=next((s['text'] for s in fields['user_facts'] if TECH.search(s['text'])),fields['user_facts'][0]['text'])[:100]
            ticket=draft_ticket(self.db,self.user,self.run['id'],title,ticket_description(fields))
            receipt={'action':'draft','ticket_id':ticket['id'],'title':ticket['title'],'status':ticket['status'],'fields':fields}
            if receipt not in self.state['receipts']:self.state['receipts'].append(receipt)
            self.save();self.emit('ticket_draft',receipt);return receipt
        @tool
        def list_my_tickets() -> list[dict]:
            """读取当前账号自己的最近10张工单，不支持其他用户参数。"""
            self.use_tool()
            if 'ticket_list' not in actions:raise ValueError('当前请求未要求查询工单')
            tickets=self.db.all('SELECT id,title,status,note FROM service_tickets WHERE user_id=? ORDER BY updated_at DESC LIMIT 10',(self.user['id'],))
            self.state['receipts']=[r for r in self.state['receipts'] if r['action']!='list']+[{'action':'list','tickets':tickets}]
            self.save();self.emit('ticket_list',{'scope':'current_user','count':len(tickets)});return tickets
        tools=([prepare_ticket] if 'ticket_create' in actions else [])+([list_my_tickets] if 'ticket_list' in actions else [])
        def satisfied():
            return all(any(r['action']==('draft' if a=='ticket_create' else 'list') for r in self.state['receipts']) for a in actions)
        @wrap_model_call
        def meter(request,handler):
            self.reserve(request.messages,request.tools)
            if not satisfied():request=request.override(tool_choice='required')
            response=handler(request);self.cancelled();return response
        agent=create_agent(self.model,tools=tools,middleware=[meter],name='service',system_prompt=
            '你是服务办理Agent。按当前用户明确意图调用限定工具。必须调用工具得到回执才能宣布完成。'
            '工具自行构建真实数据，不需要补写参数或编造已尝试操作。得到回执后停止调用，简短说明待确认。'+self.skills['service'])
        self.emit('business_input',{'domain':'service','point':task['id'],'payload':{'task':task['question'],'user_fields':fields}})
        agent.invoke({'messages':[{'role':'user','content':json.dumps({'task':task['question'],'actions':actions},ensure_ascii=False)}]},config={'recursion_limit':8})
        if not satisfied():raise ValueError('服务任务缺少真实工具回执')
        # Recheck side effect against current DB, not a model string or stale receipt.
        for receipt in self.state['receipts']:
            if receipt['action']=='draft':
                actual=self.db.one('SELECT * FROM service_tickets WHERE id=? AND user_id=? AND source_run=?',
                    (receipt['ticket_id'],self.user['id'],self.run['id']))
                if not actual or actual['status']!='draft' or actual['description']!=ticket_description(fields):
                    raise ValueError('工单回执与真实记录不一致')
        return self.complete_task(task,state,'answered',render_receipts(self.state['receipts']))

    def merge(self,state):
        if not state['results']:return {'answer':state['clarification'],'status':'clarify'}
        values=list(state['results'].values())
        answer='\n\n'.join(ROLES[r['domain']]+'：\n'+r['answer'] for r in values)
        statuses=[r['status'] for r in values]
        if state['clarification']:answer+='\n\n'+state['clarification'];statuses.append('clarify')
        return {'answer':answer,'status':'clarify' if 'clarify' in statuses else 'insufficient' if 'insufficient' in statuses else 'answered'}
