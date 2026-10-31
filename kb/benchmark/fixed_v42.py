"""Same fixed RAG generation; derive the reference list from visible markers.

Never append citations to an ungrounded answer or edit its substantive content.
Read membership and ACL are still enforced by Harness.finish.
"""
import re
from .fixed import FixedRAG


def canonical_citations(answer,citations,sources):
    if not isinstance(answer,str) or not isinstance(citations,list):
        raise ValueError('需要答案正文和引用列表')
    inline=list(dict.fromkeys(re.findall(r'\[(S\d+)\]',answer)))
    if any(not isinstance(c,str) or c not in sources for c in [*inline,*citations]):
        raise ValueError('引用编号不存在，不能通过规范化掩盖伪造来源')
    return inline


class FixedRAGV42(FixedRAG):
    def finish(self,run,state,answer,citations,status):
        canonical=canonical_citations(answer,citations,state['sources'])
        if canonical!=citations:
            self.db.event(run['id'],'citation_normalization',{'original':citations,'canonical':canonical,
                'answer_changed':False,'policy':'inline_markers_from_read_sources'})
        return super().finish(run,state,answer,canonical,status)
