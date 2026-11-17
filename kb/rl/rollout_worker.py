"""Use the native veRL worker lifecycle with a rollout-balanced PPO actor."""
from verl.single_controller.base.decorator import Dispatch, register
from verl.workers.fsdp_workers import AsyncActorRolloutRefWorker


class BusinessAsyncActorWorker(AsyncActorRolloutRefWorker):
    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        # init_model imports this symbol locally before model.external_lib is
        # loaded. Scope its replacement to this private Ray worker invocation.
        import verl.workers.actor as actors
        from .rollout_training import RolloutPPOActor
        original = actors.DataParallelPPOActor
        actors.DataParallelPPOActor = RolloutPPOActor
        try:
            return super().init_model()
        finally:
            actors.DataParallelPPOActor = original
