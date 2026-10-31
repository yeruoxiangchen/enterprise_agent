from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import business_v3_ops as ops
from business_v3_judge import validate


def test_extended_preparation_is_idempotent_and_public_scope_stays_public(tmp_path):
    _,db=ops.prepare(tmp_path/'app',True)
    before=db.all('SELECT id,base_id FROM documents ORDER BY id')
    ops.prepare(tmp_path/'app',True)
    assert db.all('SELECT id,base_id FROM documents ORDER BY id')==before
    restricted=db.all("SELECT d.filename FROM documents d JOIN bases b ON b.id=d.base_id WHERE b.visibility='admin'")
    assert restricted==[{'filename':'内部发布口令.md'}]


def test_full_queue_size_and_judge_validation():
    assert len(ops.old.CASES)+4*sum(len(c['turns']) for c in ops.CASES)+sum(len(c['turns']) for c in ops.NEW_CASES)==122
    grade={k:1 for k in ('correctness','citation_support','task_completion','factual_fidelity')}
    grade.update(reason='有依据',answer_quote='不存在的原句')
    with pytest.raises(ValueError):validate(grade,{'answer':'检查令牌过期。'})
    grade['answer_quote']='检查令牌过期。';validate(grade,{'answer':'检查令牌过期。'})
