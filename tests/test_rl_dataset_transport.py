"""Exact task round-trip through the actual upstream Arrow/collation boundary."""
import pytest

pytest.importorskip('torch')


def test_all_task_families_survive_upstream_loaded_dataset_and_collation():
    from agentlightning.verl.dataset import LoadedDataset
    from verl.utils.dataset.rl_dataset import collate_fn
    from kb.rl.tasks import make_task,pack_task,unpack_task,environment_for,FAMILIES
    samples=[make_task('train',i) for i in range(len(FAMILIES))]
    loaded=LoadedDataset([pack_task(sample)for sample in samples])
    batch=collate_fn([loaded[i]for i in range(len(samples))])
    for i,expected in enumerate(samples):
        restored=unpack_task({k:v[i]for k,v in batch.items()})
        assert restored==expected
        assert environment_for(restored).get_state()==expected['task']['initial_state']


def test_corrupted_or_misbound_task_is_rejected():
    from kb.rl.tasks import make_task,pack_task,unpack_task
    row=pack_task(make_task('train',0))
    with pytest.raises(ValueError,match='identity mismatch'):
        unpack_task({**row,'id':'another-task'})
    with pytest.raises(ValueError,match='hash mismatch'):
        unpack_task({**row,'task_json':row['task_json']+' '})
