"""Run in the isolated GPU-stack environment; these checks allocate no GPU memory."""
import pytest

pytest.importorskip('vllm')


@pytest.mark.parametrize('non_block',[False,True])
def test_vllm_new_executor_contract_preserves_native_outputs(monkeypatch,non_block):
    from kb.rl.vllm_server import BusinessZeroMQExecutor,ExternalZeroMQDistributedExecutor
    from vllm.v1.executor.abstract import Executor
    observed=[]
    def collective(self,method,timeout=None,args=(),kwargs=None):
        observed.append((method,timeout,args,kwargs));return ['worker-output']
    monkeypatch.setattr(ExternalZeroMQDistributedExecutor,'collective_rpc',collective)
    executor=object.__new__(BusinessZeroMQExecutor)
    result=Executor.execute_model(executor,'scheduler-input',non_block=non_block)
    assert (result.result() if non_block else result)=='worker-output'
    assert observed==[('execute_model',None,('scheduler-input',),None)]
