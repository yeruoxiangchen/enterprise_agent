"""Typed specialist results using LangChain's actual ToolStrategy implementation.

Design references (not vendored implementations):
https://docs.langchain.com/oss/python/langchain/multi-agent/router
https://docs.langchain.com/oss/python/langchain/multi-agent/subagents
https://docs.langchain.com/oss/python/langchain/structured-output

V3 routing, authorized retrieval, checkpointing and grounded service receipts are
retained. Only knowledge specialist output is changed. Source existence checks
do not prove semantic entailment; the latter is assessed separately.
"""
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .business_multi_v3 import BusinessQA as V3, IntentLabels
from .business_multi_v2 import ROLES
from .business_v3_routing import relevant_history


class EvidenceFact(BaseModel):
    """一条由授权文档支持的结论；来源单独填写，不手写引用标记。"""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=1500, description='事实或建议正文，不写S编号/引用括号，不声称已执行任何操作')
    sources: list[str] = Field(min_length=1, max_length=5, description='确实支持这条结论的本轮来源编号，例如S1；不可沿用历史编号')


def response_schema(allowed):
    """Scope is live so an authorized follow-up search can add new sources."""
    class KnowledgeResult(BaseModel):
        """完成本领域问答，返回有依据的事实和未解决部分。无需继续调用工具时提交此结果。"""
        model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
        facts: list[EvidenceFact] = Field(max_length=8, description='仅列资料支持、与本轮问题直接相关的结论；完全没有依据时为空')
        resolution: Literal['complete','missing_evidence','needs_user_input'] = Field(description='已回答全部问点/仍缺文档证据/必须向用户补问')
        unresolved_question: str = Field(max_length=500, description='仅写未能解决的用户问题或待补充条件，不写新的事实；complete时为空')

        @model_validator(mode='after')
        def validate_scope(self):
            for fact in self.facts:
                if not set(fact.sources) <= set(allowed):
                    raise ValueError('引用不在本任务授权来源内，可用来源：'+','.join(sorted(allowed)))
                if re.search(r'\bS\d+\b', fact.text):
                    raise ValueError('引用编号仅放在sources中；程序会将编号渲染到正文')
            if self.resolution == 'complete' and (not self.facts or self.unresolved_question):
                raise ValueError('complete必须有事实且没有未解决问题')
            if self.resolution != 'complete' and not self.unresolved_question:
                raise ValueError('未完成时必须说明缺少证据的问题或待补充条件')
            return self
    return KnowledgeResult


def render_result(result):
    # The application renders only source IDs explicitly supplied and validated
    # by ToolStrategy. It never guesses or substitutes citations after generation.
    lines=[f.text+''.join('['+s+']' for s in dict.fromkeys(f.sources)) for f in result.facts]
    if result.resolution == 'missing_evidence':
        lines.append('现有授权资料不足以回答：'+result.unresolved_question)
    elif result.resolution == 'needs_user_input':
        lines.append('请补充以下信息：'+result.unresolved_question)
    status={'complete':'answered','missing_evidence':'insufficient','needs_user_input':'clarify'}[result.resolution]
    return status,'\n'.join(lines)


class BusinessQA(V3):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        version=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if self.state.get('v4_version',version)!=version:raise ValueError('v4代码变化，请创建新请求')
        self.state['v4_version']=version

    def classify_missing(self,spans,missing):
        class RouteLabels(IntentLabels):
            """按原顺序返回每段用户请求的领域标签。不能把已知领域的咨询当成缺少对象。"""
            @model_validator(mode='after')
            def check_count(self):
                if len(self.labels)!=len(missing):raise ValueError('标签数量与待分类片段数量不一致')
                return self
        calls=0
        @wrap_model_call
        def meter(request,handler):
            nonlocal calls
            if calls>=2:raise RuntimeError('意图分类已达2次调用上限')
            calls+=1
            self.reserve(([request.system_message] if request.system_message else [])+list(request.messages),[RouteLabels])
            response=handler(request);self.cancelled()
            for message in response.result:
                self.emit('business_classifier_raw',{'attempt':calls,'raw':message.content,'tool_calls':getattr(message,'tool_calls',[])})
            return response
        prompt=('你负责将每个用户请求分派到专业Agent，仅输出RouteLabels。'
            'technical：软件、连接、故障、命令和技术安全；'
            'policy：企业或演示制度、设备借还、报销采购、权限申请、期限和办理材料；'
            'ticket_create：明确要求现在创建工单；ticket_list：明确要求查询已有工单；'
            'background：只提供背景且没有提问；clarify：确实无法识别任务对象或领域。'
            '询问怎么办理、期限、规则属于咨询，不能据此创建工单。'
            '当前没有提供制度答案不影响把制度问题分派到policy；资料是否充分由专业Agent检索后判断。'
            '不要因为问题是模拟/演示场景而要求用户澄清。每个输入片段对应一个标签，保持顺序。')
        agent=create_agent(self.model,tools=[],system_prompt=prompt,middleware=[meter],
            response_format=ToolStrategy(RouteLabels,handle_errors=True))
        output=agent.invoke({'messages':[{'role':'user','content':json.dumps({
            'texts':[spans[i]['text'] for i in missing],'user_context':self.run['question']},ensure_ascii=False)}]},
            config={'recursion_limit':6})
        return output['structured_response'].labels

    def worker(self,domain,state):
        task=state['tasks'][state['cursor']]
        if task['domain']!=domain:raise ValueError('领域不匹配')
        if domain=='service':return super().worker(domain,state)
        point=task['id'];evidence={};warnings=[]
        self.emit('business_worker',{'domain':domain,'point':point,'question':task['question']})
        self.emit('business_skill',{'domain':domain,'sha256':hashlib.sha256(self.skills[domain].encode()).hexdigest()})
        for i,query in enumerate(task['queries']):
            found=self.search(domain,query[:500],f'{point}.{i}')
            warnings.extend(found['warnings'])
            evidence.update({r['source']:r for r in found['evidence']})
        self.state['task_sources'][point]=list(evidence);self.save()
        allowed=set(evidence);schema=response_schema(allowed)
        searches=0;model_calls=0;validation_errors=0

        @tool
        def search_documents(query: str) -> dict:
            """针对当前问题尚未找到的证据补充检索一次，仅检索当前领域授权资料。"""
            nonlocal searches
            if searches>=1:return {'evidence':[],'warnings':['补充检索次数已用完，请据现有证据提交结果或说明不足。']}
            searches+=1
            found=self.search(domain,query[:500],point)
            allowed.update(r['source'] for r in found['evidence'])
            return found

        def schema_feedback(exc):
            nonlocal validation_errors
            validation_errors+=1
            self.emit('business_schema_feedback',{'point':point,'attempt':validation_errors,'error':str(exc)[:2000]})
            if validation_errors>1:raise exc
            return '请修正KnowledgeResult并重新提交。'+str(exc)[:1500]

        @wrap_model_call
        def meter(request,handler):
            nonlocal model_calls
            if model_calls>=4:raise RuntimeError('本专业Agent达到4次模型调用上限')
            model_calls+=1
            # Include the synthetic result tool and system prompt in budget checks.
            self.reserve(([request.system_message] if request.system_message else [])+list(request.messages),
                list(request.tools)+[schema])
            response=handler(request)
            self.cancelled()
            for message in response.result:
                self.emit('business_structured_raw',{'point':point,'raw':message.content,
                    'tool_calls':getattr(message,'tool_calls',[])})
            return response

        prompt=(f'你是{ROLES[domain]}专业Agent。仅处理task，使用本轮授权evidence；'
            'history仅用于解析用户指代，不作为事实依据。回答每个问点，不重复上一轮回答。'
            '只回答最后一条用户消息的task。先根据历史确定代词指向，再回答当前问句；不得复制历史答复。'
            '回答保持简短，仅给当前问题必需的结论；不要扩展与当前问题无关的故障建议或缓存操作。'
            '事实和建议必须由文档直接支持，使用KnowledgeResult中的facts分别填写text和sources。'
            'text不写来源编号，sources只选支持该事实的本轮编号。不要使用<answered>等文本标记。'
            '资料明确禁止某操作时，应回答禁止，不要误称资料不足。'
            '若问题明确询问演示/模拟制度，按该资料回答，不需要证明它是现实公司的制度。'
            '已标明新版本替代旧版本时，使用明确生效的新版本；只有未解决的真实冲突才说明不确定。'
            '只回答当前问题所需细节，不因未提供无关细节而拒答。不编造金额、服务器地址、命令或已执行操作。'
            '已知HTTPS问题不展开SSH建议。用户要求办理的真实操作由服务Agent执行。'
            '证据足够就提交KnowledgeResult，不必额外搜索；证据不够可以补查一次。'
            '完全缺证据时facts=[]，resolution=missing_evidence，并用unresolved_question指出未回答的问点。'
            '部分有依据时保留facts，再指出缺失部分；必须补用户条件时选needs_user_input。\n'+self.skills[domain])
        payload={'task':task['question'],'user_context':task['context'],
            'evidence':list(evidence.values()),'warnings':warnings}
        # Official subagent-input pattern: pass selected history as real message
        # roles, then the current task last. Historical citation IDs stay local.
        history=[]
        for message in relevant_history(self.history,self.run['question']):
            content=message['content']
            if message['role']=='assistant':
                content='历史答复，仅用于理解指代，不作事实依据：'+re.sub(r'\[S\d+\]','',content)
            history.append({'role':message['role'],'content':content})
        self.emit('business_input',{'domain':domain,'point':point,'payload':payload})
        agent=create_agent(self.model,tools=[search_documents],name=domain,system_prompt=prompt,
            middleware=[meter],response_format=ToolStrategy(schema,handle_errors=schema_feedback,
                tool_message_content='本领域结构化结果已通过格式及来源范围检查。'))
        output=agent.invoke({'messages':history+[{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]},
            config={'recursion_limit':12})
        result=output.get('structured_response')
        if not isinstance(result,schema):raise ValueError('专业Agent未返回有效结构化结果')
        status,answer=render_result(result)
        self.state.setdefault('specialist_results',{})[point]=result.model_dump();self.save()
        self.emit('business_structured_reply',{'domain':domain,'point':point,'result':result.model_dump(),
            'renderer':'host_citations','validation_errors':validation_errors})
        return self.complete_task(task,state,status,answer)
