#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CRM_DATA_ROOT=/data/zjr/enterprise_kb_agent
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$CRM_DATA_ROOT/tmp" TORCH_HOME="$CRM_DATA_ROOT/cache/torch"
export HF_HOME="$CRM_DATA_ROOT/cache/huggingface" HF_HUB_CACHE="$CRM_DATA_ROOT/cache/huggingface/hub"
export TORCH_EXTENSIONS_DIR="$CRM_DATA_ROOT/cache/torch_extensions" MPLCONFIGDIR="$CRM_DATA_ROOT/cache/matplotlib"
export XDG_CACHE_HOME="$CRM_DATA_ROOT/cache" PIP_CACHE_DIR="$CRM_DATA_ROOT/cache/pip"
export CUDA_CACHE_PATH="$CRM_DATA_ROOT/cache/cuda" TRITON_CACHE_DIR="$CRM_DATA_ROOT/cache/triton"
export TORCHINDUCTOR_CACHE_DIR="$CRM_DATA_ROOT/cache/torchinductor" VLLM_CACHE_ROOT="$CRM_DATA_ROOT/cache/vllm"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_USAGE_STATS=0 DO_NOT_TRACK=1
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false
export LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false
export NO_PROXY="127.0.0.1,localhost,::1${NO_PROXY:+,$NO_PROXY}" no_proxy="127.0.0.1,localhost,::1${no_proxy:+,$no_proxy}"
export PYTHONPATH="$CRM_DATA_ROOT/business-packages-v2:$CRM_DATA_ROOT/benchmarks/crm-agent-bench/src:$PROJECT_DIR:$PROJECT_DIR/scripts"
umask 077
cd "$PROJECT_DIR"
if [[ "${1:-status}" == experience ]]; then
  shift
  exec "$CRM_DATA_ROOT/venv-agentpack/bin/python" -B "$PROJECT_DIR/scripts/experience_memory.py" "$@"
fi
if [[ "${1:-status}" == incident-acceptance ]]; then
  shift
  exec "$CRM_DATA_ROOT/venv-agentpack/bin/python" -B -u "$PROJECT_DIR/scripts/incident_acceptance.py" "$@"
fi
if [[ "${1:-status}" == acceptance ]]; then
  shift
  if [[ "${1:-status}" == watch ]]; then
    shift
    exec watch -n 5 bash "$PROJECT_DIR/scripts/crm_bench_run.sh" acceptance status "$@"
  fi
  exec "$CRM_DATA_ROOT/venv-agentpack/bin/python" -B -u "$PROJECT_DIR/scripts/crm_acceptance.py" "$@"
fi
if [[ "${1:-status}" == small ]]; then
  shift
  exec "$CRM_DATA_ROOT/venv-agentpack/bin/python" -B -u "$PROJECT_DIR/scripts/crm_small_check.py" "$@"
fi
if [[ "${1:-status}" == test ]]; then
  shift
  exec "$CRM_DATA_ROOT/venv-agentpack/bin/python" -B -m pytest -p no:cacheprovider \
    --basetemp "$CRM_DATA_ROOT/tmp/crm-benchmark-tests" "$@"
fi
if [[ "${1:-status}" == latency ]]; then
  shift
  exec "$CRM_DATA_ROOT/venv-agentpack/bin/python" -B -u "$PROJECT_DIR/scripts/crm_latency_probe.py" "$@"
fi
if [[ "${1:-status}" == watch ]]; then
  shift
  exec watch -n 5 bash "$PROJECT_DIR/scripts/crm_bench_run.sh" status "$@"
fi
exec "$CRM_DATA_ROOT/venv-agentpack/bin/python" -B -u "$PROJECT_DIR/scripts/crm_benchmark.py" "$@"
