#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export KB_DATA_DIR="${KB_DATA_DIR:-/data/zjr/enterprise_kb_agent}"
export TMPDIR="$KB_DATA_DIR/tmp" TORCH_HOME="$KB_DATA_DIR/cache/torch"
export HF_HOME="$KB_DATA_DIR/cache/huggingface" HF_HUB_CACHE="$KB_DATA_DIR/cache/huggingface/hub"
export TORCH_EXTENSIONS_DIR="$KB_DATA_DIR/cache/torch_extensions" MPLCONFIGDIR="$KB_DATA_DIR/cache/matplotlib"
export XDG_CACHE_HOME="$KB_DATA_DIR/cache" PYTHONDONTWRITEBYTECODE=1
export HAYSTACK_TELEMETRY_ENABLED=false HAYSTACK_CONTENT_TRACING_ENABLED=false DD_TRACE_ENABLED=false
export OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=4 TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"
umask 077
mkdir -p "$TMPDIR" "$KB_DATA_DIR/logs" "$KB_DATA_DIR/artifacts"
for envfile in model.env private.env judge.env; do
  if [[ -f "$KB_DATA_DIR/$envfile" ]]; then
    set -a
    source "$KB_DATA_DIR/$envfile"
    set +a
  fi
done
cd "$PROJECT_DIR"
PYTHON="$KB_DATA_DIR/venv-haystack/bin/python"
ACTION="${1:-status}"
shift || true
if [[ "$ACTION" == test ]]; then
  exec "$PYTHON" -B -m pytest -p no:cacheprovider --basetemp "$KB_DATA_DIR/tmp/multi-pytest" tests/test_multi_agent.py tests/test_external_rag.py tests/test_application.py "$@"
elif [[ "$ACTION" == serve || "$ACTION" == preview-init ]]; then
  export KB_DATA_DIR="$KB_DATA_DIR/preview_multi_agent"
  export KB_MODE=agent KB_AGENT_POLICY=multi_agent_v1
  if [[ "$ACTION" == preview-init ]]; then
    "$PYTHON" -B -m kb.cli init
    exec "$PYTHON" -B -m kb.cli demo
  fi
  exec "$PYTHON" -B -m uvicorn kb.app:app --host "${KB_HOST:-127.0.0.1}" --port "${KB_PORT:-8769}" --workers 1 "$@"
else
  exec nice -n 10 "$PYTHON" -B scripts/eval_multi_agent.py "$ACTION" "$@"
fi
