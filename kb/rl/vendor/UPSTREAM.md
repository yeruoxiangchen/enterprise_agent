# Upstream provenance

## Agent Lightning

- Project: https://github.com/microsoft/agent-lightning
- Commit: `2070bad5113e52f1aa2e5bdba822d4b24b197c05` (2026-11-12)
- License: MIT; copied in `agent_lightning_v1/LICENSE`.
- `agentlightning/verl/rollout_level_advantage.py`: git blob `2979fb6a16008898a1d2d3d7d58fa28c372ae0ec`.
- `agentlightning/verl/per_rollout_loss.py`: git blob `447ad6fe9122f1fea58d9e10accd757f3bf00f98`.

The two Python modules are copied verbatim. They supply rollout grouping,
validation, scalar broadcasting, precision-safe rollout token normalization and
the clipped PPO objective. `kb.rl.rollout_training` adapts their use to the
installed veRL 0.6 API. It does not call the upstream v1 worker registration
hook, which expects a newer veRL/vLLM stack. CISPO is not enabled.

## veRL actor compatibility

`kb.rl.rollout_training.RolloutPPOActor.update_policy` is adapted from
[`verl/workers/actor/dp_actor.py`, v0.6.0](https://github.com/volcengine/verl/blob/v0.6.0/verl/workers/actor/dp_actor.py).
The microbatch forward, reference KL and clipped optimizer implementations are
called from that installed upstream package. The adaptation keeps one complete
rollout batch per optimizer update and applies the same rollout token weights
to PG and KL. Changes from the original are described in that method's docstring.

Original copyright notices for the adapted actor method:

Copyright 2024 Bytedance Ltd. and/or its affiliates.
Copyright 2023-2024 SGLang Team.
Copyright 2025 ModelBest Inc. and/or its affiliates.

Licensed under the Apache License, Version 2.0. A copy is provided in
`LICENSE.verl`. The adapted method is distributed under those terms.

## Other direct library reuse

- DeepAgents 0.7.38: native task/subagent graphs, forks and checkpoint lifecycle.
- LangChain 1.8: advertised tool schemas and model/tool middleware. The typed
  delegation contract and its SQLite bindings are this project's engineering.
- TRL 0.21.0: `SFTTrainer`, `SFTConfig` and response-mask data collator, installed
  without changing existing training dependencies.
- PEFT 0.15.2: native LoRA and `merge_and_unload(safe_merge=True)` for a standalone
  SFT policy base before starting RL.

ToolRL's tool-name/argument rewards and AgentFlow's planner training are research
references for subsequent experiments; neither is claimed as implemented here.
