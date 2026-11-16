"""Validate the service lease covers both physical GPUs before starting TP."""
from types import SimpleNamespace

import pytest

import crm_bench_services as services


def service(tmp_path, monkeypatch, second_free=15000):
    root = tmp_path/'runtime'
    (root/'models/Qwen3-8B').mkdir(parents=True)
    (root/'models/Qwen3-8B/config.json').write_text('{}')
    monkeypatch.setattr(services, 'RUNTIME', root)
    monkeypatch.setattr(services, 'gpu_snapshot', lambda: {
        '1': {'total_mib':24576, 'free_mib':15000},
        '5': {'total_mib':24576, 'free_mib':second_free}})
    monkeypatch.setattr(services, 'identity', lambda pid:'fixed-start')
    launches = []
    def launch(command, **kwargs):
        launches.append((command,kwargs['env']))
        return SimpleNamespace(pid=123456)
    monkeypatch.setattr(services.subprocess,'Popen',launch)
    return services.ModelServices(tmp_path,lambda *args,**kwargs:None),launches


def test_two_rank_service_is_scoped_to_two_admitted_cards(tmp_path,monkeypatch):
    instance,launches=service(tmp_path,monkeypatch)
    instance.start('actor','1,5',memory_fraction=.5,max_model_len=8192,tensor_parallel_size=2)
    command,env=launches[0]
    assert env['CUDA_VISIBLE_DEVICES']=='1,5'
    assert command[command.index('--tensor-parallel-size')+1]=='2'
    assert instance.owned['actor']['physical_gpus']==['1','5']
    assert set(instance.owned['actor']['snapshot_before_start'])=={'1','5'}


def test_second_card_insufficient_memory_prevents_any_model_start(tmp_path,monkeypatch):
    instance,launches=service(tmp_path,monkeypatch,second_free=12000)
    with pytest.raises(RuntimeError,match='GPU 5 has insufficient'):
        instance.start('actor','1,5',memory_fraction=.5,tensor_parallel_size=2)
    assert not launches and not instance.owned


def test_two_rank_service_rejects_duplicate_physical_devices(tmp_path,monkeypatch):
    instance,launches=service(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='distinct physical GPU'):
        instance.start('actor','1,1',memory_fraction=.5,tensor_parallel_size=2)
    assert not launches


def test_background_launcher_preserves_fork_mode_and_two_card_budget(tmp_path,monkeypatch):
    import sys
    import harness_native_check as control
    launches=[]
    monkeypatch.setattr(control,'OUTPUT',tmp_path)
    monkeypatch.setattr(control,'ARMS',control.ARMS)
    monkeypatch.setattr(control,'CASES',control.CASES)
    monkeypatch.setattr(sys,'argv',['harness_native_check.py','launch','--tag','fork',
        '--gpus','1,5','--tensor-parallel-size','2','--memory-fraction','0.5',
        '--fork-comparison','--confirmation-guard','--candidate-only',
        '--suite','expanded16','--reuse-from','previous'])
    monkeypatch.setattr(control.subprocess,'Popen',lambda command,**kwargs:
        (launches.append(command)or SimpleNamespace(pid=123456)))
    control.main()
    command=launches[0]
    assert '--fork-comparison'in command and '--confirmation-guard'in command
    assert '--candidate-only'in command
    assert command[command.index('--suite')+1]=='expanded16'
    assert command[command.index('--reuse-from')+1]=='previous'
    assert command[command.index('--tensor-parallel-size')+1]=='2'
    assert command[command.index('--memory-fraction')+1]=='0.5'
