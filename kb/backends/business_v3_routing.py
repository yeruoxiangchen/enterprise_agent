"""Finite intent selection; host owns spans, ordering and contextual dependencies.

Rules cover explicit business commands/topics. Ambiguous spans alone may use the
model's enum classifier. No case IDs, expected answers or benchmark imports.
"""
import re
from .business_multi_v2 import source_spans

INTENTS = ('technical','policy','ticket_create','ticket_list','background','clarify')
SWITCH = re.compile(r'换个(?:问题|话题)|换一个(?:问题|话题)|另一个话题|不说这个|改问')
QUESTION = re.compile(r'[？?]|怎么|如何|多少|多久|什么|是否|能否|可否|帮.*(?:排查|检查)|请.*(?:解释|查询|查看|告诉)')
TECH = re.compile(r'git|ssh|https?|credential|proxy|permission denied|token|公钥|私钥|令牌|代理|服务器|报错|蓝屏|连接|口令|配置|缓存',re.I)
POLICY = re.compile(r'制度|政策|报销|住宿|餐饮|补贴|采购|预算|借用|归还|远程办公|出差|入职|申请.*(?:材料|权限)|审批|获批|延期|续期|有效期|赔偿|报修.*(?:信息|材料)|权限.*(?:材料|批准|多久)')
CREATE = re.compile(r'(?:生成|创建|建个|建一|开个|开一|帮.*建|登记|发起).{0,16}(?:工单|报障|报修)|转人工')
LIST = re.compile(r'(?:查|列|看|给我).{0,20}工单|工单.{0,12}(?:状态|进度|列表|列给|给我看)')
NEGATIVE_CREATE = re.compile(r'(?:不|别|不要|无需|不用|暂不).{0,10}(?:生成|创建|建|登记|转人工)')


def classify_rule(text):
    if NEGATIVE_CREATE.search(text):
        # A negative instruction is context, never authority to create a ticket.
        return 'technical' if re.search(r'只.*(?:排查|检查)|帮.*排查',text) else 'background'
    if CREATE.search(text):return 'ticket_create'
    if LIST.search(text) and not re.search(r'需要.*(?:材料|信息)|怎么.*(?:创建|申请)',text):return 'ticket_list'
    if not QUESTION.search(text) and re.search(r'^(?:我的?|当前|环境|系统|设备|已经|已|Ubuntu|Windows|Linux)|(?:报\d{3}|提示|故障|已确认|已检查)',text,re.I):
        return 'background'
    if POLICY.search(text):return 'policy'
    if TECH.search(text):return 'technical'
    if re.fullmatch(r'(?:这个|那个|这|那)?(?:怎么弄|怎么办|什么意思)[？?。\s]*',text):return 'clarify'
    return None


def relevant_history(history, question):
    if SWITCH.search(question):return []
    # Direct, self-contained questions do not inherit another topic.
    if not re.search(r'^(?:那|这|完整的|刚才|上述|继续|再)|(?:那个|它|上面|续期|延期)|(?:生成|创建).{0,8}工单',question):return []
    return [m for m in history[-4:] if m['role'] in ('user','assistant')]


def compile_tasks(question, history, labels):
    spans=source_spans(question)
    if len(labels)!=len(spans) or any(label not in INTENTS for label in labels):
        raise ValueError('意图标签数量或枚举不合法')
    historical=relevant_history(history,question)
    # Assistants may resolve a pronoun; they never become citation evidence or ticket facts.
    # Source numbers are local to a run. Strip historical assistant citation
    # markers so the model cannot mistake an old S2 for current evidence S2.
    dialogue='\n'.join(('用户：'+m['content']) if m['role']=='user' else
        '先前助手（仅用于解析指代，不作事实证据）：'+re.sub(r'\[S\d+\]','',m['content']) for m in historical)
    backgrounds=[s for s,label in zip(spans,labels) if label=='background']
    groups={};unknown=[]
    for span,label in zip(spans,labels):
        if label=='background':continue
        if label=='clarify':unknown.append(span);continue
        domain='service' if label.startswith('ticket_') else label
        task=groups.setdefault(domain,{'domain':domain,'parts':[],'actions':[]})
        task['parts'].append(span)
        if domain=='service' and label not in task['actions']:task['actions'].append(label)
    # Preserve independent uncertainty alongside successful tasks.
    tasks=[]
    for domain in ('technical','policy','service'):
        if domain not in groups:continue
        task=groups[domain]
        task.update({'id':'P'+str(len(tasks)+1),'question':'\n'.join(s['text'] for s in task['parts']),
            'span_ids':[s['id'] for s in task['parts']],
            'context':'\n'.join(s['text'] for s in backgrounds), 'context_ids':[s['id'] for s in backgrounds],
            'dialogue_context':dialogue})
        # Anaphoric follow-up within a group keeps earlier user text in its retrieval query.
        queries=[]
        for i,part in enumerate(task['parts']):
            preceding=task['parts'][i-1]['text'] if i and re.search(r'^(?:那|这|要|还|它|能|多久|什么时候)',part['text']) else ''
            user_history='\n'.join(m['content'] for m in historical if m['role']=='user')
            queries.append('\n'.join(filter(None,[user_history,task['context'],preceding,part['text']])))
        task['queries']=queries
        tasks.append(task)
    return tasks,unknown,spans


def ticket_user_sources(question, history):
    prior=relevant_history(history,question)
    sources=[]
    for i,m in enumerate(prior):
        if m['role']=='user':sources.extend(source_spans(m['content'],f'H{i}_'))
    sources.extend(source_spans(question))
    return [{**s,'origin':'user'} for s in sources]


def ticket_fields(question, history):
    """A quote-based form: models cannot author or rewrite already-tried facts."""
    sources=ticket_user_sources(question,history)
    facts=[s for s in sources if not CREATE.search(s['text']) and not LIST.search(s['text'])]
    # If the fault and create request share a sentence, keep that full original quote.
    if not facts:facts=[s for s in sources if TECH.search(s['text'])]
    if not facts:return None
    attempts=[s for s in facts if re.search(r'已(?:经)?|尝试过|做过|确认过|重启过',s['text'])]
    return {'user_facts':facts,'user_attempts':attempts,
        'suggested_next_steps':['待支持人员核对故障并给出下一步建议；此栏不表示用户已执行。']}


def ticket_description(fields):
    lines=['用户原始描述（原文，不视为已核实事实）：']
    lines += [f"[{s['id']}] {s['text']}" for s in fields['user_facts']]
    lines += ['用户提及的已尝试操作（原文，保留否定及不确定性）：']
    lines += [f"[{s['id']}] {s['text']}" for s in fields['user_attempts']] or ['未提供。']
    lines += ['建议下一步（尚未执行）：',*fields['suggested_next_steps']]
    result='\n'.join(lines)
    if len(result)>3000:raise ValueError('工单原文超过3000字，请缩小描述范围，未静默截断')
    return result
