from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import business_v4_ops as ops
from business_v4_judge import calibration


def test_repair_and_comparison_sets_preserve_existing_cases():
    assert sum(len(c['turns']) for _,_,cases,_ in ops.stages(True) for c in cases)==12
    assert sum(len(c['turns']) for _,_,cases,_ in ops.stages(False) for c in cases)==99
    arms=[(a,cases) for _,a,cases,_ in ops.stages(False) if a in ops.POLICIES and cases==ops.CASES]
    assert {a for a,_ in arms}==set(ops.POLICIES)


class WrongActionJudge:
    def grade(self,job,target):
        # Mirrors the observed v3 evaluation bug: consulting is incorrectly
        # penalized for not creating a ticket. Calibration must reject it.
        return dict(review_status='ok',correctness=1,citation_support=1,task_completion=0,factual_fidelity=1)


def test_calibration_rejects_knowledge_requires_ticket_bug(tmp_path):
    assert not calibration(WrongActionJudge(),tmp_path)
