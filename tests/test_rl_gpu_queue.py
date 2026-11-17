"""The RL queue acquires available devices and leaves occupied devices alone."""
import fcntl

import pytest

from kb.rl.control import acquire_gpu_leases


def test_queue_waits_then_acquires_both_leases_without_loading_models(tmp_path, monkeypatch):
    snapshots = iter([{'5':{'free_mib':6000},'6':{'free_mib':6000}},
                      {'5':{'free_mib':24126},'6':{'free_mib':24126}}])
    events=[]
    monkeypatch.setattr('kb.rl.control.time.sleep', lambda _: None)
    leases, snapshot=acquire_gpu_leases(['5','6'],tmp_path,wait_minutes=1,
        report=lambda **event: events.append(event),snapshot_fn=lambda: next(snapshots))
    try:
        assert len(events)==1 and events[0]['stage']=='waiting_for_gpus'
        assert snapshot['5']['free_mib']==24126 and len(leases)==2
        with (tmp_path/'gpu-5.lock').open('a') as another:
            with pytest.raises(BlockingIOError):
                fcntl.flock(another,fcntl.LOCK_EX|fcntl.LOCK_NB)
    finally:
        for lease in leases:lease.close()


def test_occupied_gpu_preflight_terminates_without_acquiring_a_partial_lease(tmp_path):
    with pytest.raises(RuntimeError,match='GPU preflight failed'):
        acquire_gpu_leases(['5','6'],tmp_path,wait_minutes=0,report=lambda **event:None,
            snapshot_fn=lambda:{'5':{'free_mib':24126},'6':{'free_mib':6000}})
    assert not list(tmp_path.glob('*.lock'))


def test_conflicting_second_lease_releases_the_first(tmp_path):
    with (tmp_path/'gpu-6.lock').open('a') as occupied:
        fcntl.flock(occupied,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(RuntimeError,match='leased_by_another_rl_job'):
            acquire_gpu_leases(['5','6'],tmp_path,wait_minutes=0,report=lambda **event:None,
                snapshot_fn=lambda:{'5':{'free_mib':24126},'6':{'free_mib':24126}})
        with (tmp_path/'gpu-5.lock').open('a') as released:
            fcntl.flock(released,fcntl.LOCK_EX|fcntl.LOCK_NB)


@pytest.mark.parametrize('arguments,protocol,review',[
    ([], 'native', 'errors'),
    (['--delegation-protocol','explicit','--completion-review','none'],'explicit','none')])
def test_new_rl_jobs_use_repaired_profile_with_explicit_historical_replay(monkeypatch,arguments,protocol,review):
    from kb.rl.control import main
    captured=[]
    monkeypatch.setattr('sys.argv',['agent_rl','_run',*arguments])
    monkeypatch.setattr('kb.rl.control.run_job',lambda args:captured.append(args))
    main()
    assert len(captured)==1
    assert captured[0].delegation_protocol==protocol
    assert captured[0].completion_review==review
