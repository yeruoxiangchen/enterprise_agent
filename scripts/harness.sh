#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HARNESS_ROOT=/data/zjr/enterprise_kb_agent
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$HARNESS_ROOT/tmp/harness" TORCH_HOME="$HARNESS_ROOT/cache/torch"
export HF_HOME="$HARNESS_ROOT/cache/huggingface" HF_HUB_CACHE="$HARNESS_ROOT/cache/huggingface/hub"
export TORCH_EXTENSIONS_DIR="$HARNESS_ROOT/cache/torch_extensions" MPLCONFIGDIR="$HARNESS_ROOT/cache/matplotlib"
export XDG_CACHE_HOME="$HARNESS_ROOT/cache" PIP_CACHE_DIR="$HARNESS_ROOT/cache/pip"
export CUDA_CACHE_PATH="$HARNESS_ROOT/cache/cuda" TRITON_CACHE_DIR="$HARNESS_ROOT/cache/triton"
export TORCHINDUCTOR_CACHE_DIR="$HARNESS_ROOT/cache/torchinductor"
export LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false
export NO_PROXY="127.0.0.1,localhost,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
export PYTHONPATH="$PROJECT_DIR:$PROJECT_DIR/scripts:$HARNESS_ROOT/benchmarks/crm-agent-bench/src"
mkdir -p "$TMPDIR" "$HARNESS_ROOT/artifacts/harness_impl_20261114"
cd "$PROJECT_DIR"
ACTION="${1:-status}";shift || true
case "$ACTION" in
  setup)
    [[ -x "$HARNESS_ROOT/venv-harness/bin/python" ]] || python -m venv "$HARNESS_ROOT/venv-harness"
    "$HARNESS_ROOT/venv-harness/bin/python" -m pip install -r requirements.harness.lock
    "$HARNESS_ROOT/venv-harness/bin/python" -m pip freeze > "$HARNESS_ROOT/artifacts/harness_impl_20261114/requirements.lock"
    ;;
  test) exec "$HARNESS_ROOT/venv-harness/bin/python" -B -m pytest -p no:cacheprovider --basetemp "$TMPDIR/pytest-$$" "$@" ;;
  memory) exec "$HARNESS_ROOT/venv-harness/bin/python" -B -u scripts/harness_memory.py "$@" ;;
  native) exec "$HARNESS_ROOT/venv-harness/bin/python" -B -u scripts/harness_native_check.py "$@" ;;
  serve)
    export KB_MODE=agent KB_AGENT_POLICY=business_native_v1
    export KB_HARNESS_GOAL_REVIEW="${KB_HARNESS_GOAL_REVIEW:-0}"
    exec "$HARNESS_ROOT/venv-harness/bin/python" -B -m uvicorn kb.app:app --host 127.0.0.1 --port 18768 "$@"
    ;;
  run) exec "$HARNESS_ROOT/venv-harness/bin/python" -B "$@" ;;
  *) exec "$HARNESS_ROOT/venv-harness/bin/python" -B -u scripts/harness_assessment.py "$ACTION" "$@" ;;
esac
