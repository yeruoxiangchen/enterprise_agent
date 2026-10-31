#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export KB_DATA_DIR="${KB_DATA_DIR:-/data/zjr/enterprise_kb_agent}"
export TMPDIR="$KB_DATA_DIR/tmp" TORCH_HOME="$KB_DATA_DIR/cache/torch"
export HF_HOME="$KB_DATA_DIR/cache/huggingface" HF_HUB_CACHE="$KB_DATA_DIR/cache/huggingface/hub"
export TORCH_EXTENSIONS_DIR="$KB_DATA_DIR/cache/torch_extensions" MPLCONFIGDIR="$KB_DATA_DIR/cache/matplotlib"
export XDG_CACHE_HOME="$KB_DATA_DIR/cache" PIP_CACHE_DIR="$KB_DATA_DIR/cache/pip" PYTHONDONTWRITEBYTECODE=1
export HAYSTACK_TELEMETRY_ENABLED=false HAYSTACK_CONTENT_TRACING_ENABLED=false DD_TRACE_ENABLED=false
export OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=4 TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"
umask 077
mkdir -p "$TMPDIR" "$KB_DATA_DIR/logs" "$KB_DATA_DIR/artifacts"
cd "$PROJECT_DIR"
ACTION="${1:-status}"; shift || true
if [[ "$ACTION" == setup ]]; then
  if [[ ! -x "$KB_DATA_DIR/venv-agentpack/bin/python" ]]; then
    "$KB_DATA_DIR/venv-haystack/bin/python" -m venv "$KB_DATA_DIR/venv-agentpack"
  fi
  "$KB_DATA_DIR/venv-agentpack/bin/python" -m pip install -r requirements.agentpack.txt
  "$KB_DATA_DIR/venv-agentpack/bin/python" -m pip check
  "$KB_DATA_DIR/venv-agentpack/bin/python" -m pip freeze > "$KB_DATA_DIR/agentpack-environment.lock"
  exit
fi
for envfile in model.env private.env judge.env; do
  if [[ -f "$KB_DATA_DIR/$envfile" ]]; then set -a; source "$KB_DATA_DIR/$envfile"; set +a; fi
done
PYTHON="$KB_DATA_DIR/venv-agentpack/bin/python"
if [[ "$ACTION" == fixed-first ]]; then
  SUBACTION="${1:-status}"; shift || true
  exec nice -n 10 "$PYTHON" -B scripts/eval_agentpack.py "$SUBACTION" --variant fixed-first "$@"
elif [[ "$ACTION" == audit ]]; then
  exec "$PYTHON" -B scripts/audit_agentpack_20261030.py "$@"
elif [[ "$ACTION" == test ]]; then
  exec "$PYTHON" -B -m pytest -p no:cacheprovider --basetemp "$KB_DATA_DIR/tmp/agentpack-pytest" tests/test_agentpack.py tests/test_fixed_first.py tests/test_reranker_concurrency.py tests/test_external_rag.py tests/test_application.py "$@"
elif [[ "$ACTION" == serve || "$ACTION" == preview-init ]]; then
  export KB_DATA_DIR="$KB_DATA_DIR/preview_agentpack" KB_MODE=agent KB_AGENT_POLICY=agentpack_v1
  if [[ "$ACTION" == preview-init ]]; then
    "$PYTHON" -B -m kb.cli init
    exec "$PYTHON" -B -m kb.cli demo
  fi
  exec "$PYTHON" -B -m uvicorn kb.app:app --host "${KB_HOST:-127.0.0.1}" --port "${KB_PORT:-8770}" --workers 1 "$@"
else
  exec nice -n 10 "$PYTHON" -B scripts/eval_agentpack.py "$ACTION" "$@"
fi
