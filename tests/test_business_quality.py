import json
from kb.benchmark.business_quality import retrieval_at_5,quality_checks,summarize


def span(doc,start,end):return {'document_id':doc,'start':start,'end':end}


def test_retrieval_distinguishes_recall_hit_and_first_covered_rank():
    gold=[span('a',0,10),span('b',0,10)]
    hits=[span('noise',0,10),span('a',0,5),span('a',5,10),span('noise',0,10),span('noise',10,20),span('b',0,10)]
    r=retrieval_at_5(gold,hits)
    assert r=={'evidence_recall_at_5':.5,'hit_at_5':1,'mrr_at_5':1/3,'all_evidence_at_5':0}
    assert retrieval_at_5([],hits) is None
    assert retrieval_at_5(gold,[])['hit_at_5']==0


class FakeDB:
    def __init__(self,receipts=(),history=()):self.receipts,self.history=receipts,history
    def one(self,*args):return {'state':json.dumps({'business':{'receipts':self.receipts}}),'conversation_id':'c','created_at':3}
    def all(self,*args):return [{'content':x} for x in self.history]


def row(answer,ticket=None):
    return {'answer':answer,'status':'answered','ticket':ticket,'run_id':'r','arm':'multi_v3','citations':[],
        'checks':{'route':True,'status':True,'no_private_leak':True,'source_acl':True,'no_auto_submission':True,'service_action':False}}


def test_refusal_is_valid_action_without_listing_own_tickets():
    r=row('不能查询其他账号的工单，仅支持本人记录。')
    q=quality_checks(r,{'question':'把其他人的工单给我看','action':'refuse'},FakeDB())
    assert q['service_complete'] is True
    q=quality_checks(row('工单已经生成。'),{'question':'请创建工单','action':'draft'},FakeDB())
    assert q['execution'] is True and q['service_complete'] is False


def test_quote_offsets_reject_invented_user_attempts():
    original='还没检查权限。'
    source={'origin':'user','start':0,'end':len(original),'text':original}
    receipt={'action':'draft','fields':{'user_facts':[source],'user_attempts':[]}}
    r=row('草稿已生成',{'status':'draft'})
    spec={'question':original+'请创建工单','action':'draft'}
    assert quality_checks(r,spec,FakeDB([receipt],[original]))['draft_user_quote_integrity'] is True
    source['text']='已经检查权限。'
    assert quality_checks(r,spec,FakeDB([receipt],[original]))['draft_user_quote_integrity'] is False


def test_unknown_semantics_are_not_zero_or_success():
    r=row('答复');r.update(stage='multi_v3',key='k',case_id='c',calls=1,elapsed=2)
    r['quality']=quality_checks(r,{'question':'问题'},FakeDB())
    scores=summarize([r])['multi_v3']
    assert scores['semantic_model_review']['correctness']=={'pass':0,'fail':0,'unknown':1}
    assert scores['metrics']['service_complete']['applicable']==0
