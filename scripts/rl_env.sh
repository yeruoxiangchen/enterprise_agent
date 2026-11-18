#!/usr/bin/env bash
# Source before any RL process; all generated state lives on the data volume.
export KB_RL_ROOT="${KB_RL_ROOT:-/data/zjr/enterprise_kb_agent/rl}"
export TMPDIR="$KB_RL_ROOT/tmp"
export TORCH_HOME="$KB_RL_ROOT/cache/torch"
export HF_HOME="$KB_RL_ROOT/cache/huggingface"
export HF_HUB_CACHE="$HF_HOME/hub"
export TORCH_EXTENSIONS_DIR="$KB_RL_ROOT/cache/torch_extensions"
export TORCHINDUCTOR_CACHE_DIR="$KB_RL_ROOT/cache/inductor"
export TRITON_CACHE_DIR="$KB_RL_ROOT/cache/triton"
export VLLM_CACHE_ROOT="$KB_RL_ROOT/cache/vllm"
export MPLCONFIGDIR="$KB_RL_ROOT/cache/matplotlib"
export XDG_CACHE_HOME="$KB_RL_ROOT/cache/xdg"
export PIP_CACHE_DIR="$KB_RL_ROOT/cache/pip"
export RAY_TMPDIR="$KB_RL_ROOT/tmp/ray"
export PYTHONDONTWRITEBYTECODE=1
export TOKENIZERS_PARALLELISM=false
export WANDB_MODE=offline
# Local model servers must bypass workstation HTTP proxies. Keep the existing
# remote proxy configuration and both spellings of the user's bypass list.
task_rl_no_proxy="${NO_PROXY:-}"
if [[ -n "${no_proxy:-}" && "$no_proxy" != "$task_rl_no_proxy" ]]; then
  task_rl_no_proxy="${task_rl_no_proxy:+$task_rl_no_proxy,}$no_proxy"
fi
for task_rl_host in 127.0.0.1 localhost ::1; do
  case ",$task_rl_no_proxy," in
    *",$task_rl_host,"*) ;;
    *) task_rl_no_proxy="${task_rl_no_proxy:+$task_rl_no_proxy,}$task_rl_host" ;;
  esac
done
export NO_PROXY="$task_rl_no_proxy"
export no_proxy="$task_rl_no_proxy"
unset task_rl_no_proxy task_rl_host
mkdir -p "$TMPDIR" "$TORCH_HOME" "$HF_HUB_CACHE" "$TORCH_EXTENSIONS_DIR" \
  "$MPLCONFIGDIR" "$XDG_CACHE_HOME" "$PIP_CACHE_DIR" "$RAY_TMPDIR" \
  "$TORCHINDUCTOR_CACHE_DIR" "$TRITON_CACHE_DIR" "$VLLM_CACHE_ROOT" \
  "$KB_RL_ROOT/outputs" "$KB_RL_ROOT/datasets" "$KB_RL_ROOT/logs"
