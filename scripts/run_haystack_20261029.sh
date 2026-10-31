#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export KB_DATA_DIR="${KB_DATA_DIR:-/data/zjr/enterprise_kb_agent}"
export TMPDIR="$KB_DATA_DIR/tmp"
export TORCH_HOME="$KB_DATA_DIR/cache/torch"
export HF_HOME="$KB_DATA_DIR/cache/huggingface"
export HF_HUB_CACHE="$HF_HOME/hub"
export TORCH_EXTENSIONS_DIR="$KB_DATA_DIR/cache/torch_extensions"
export MPLCONFIGDIR="$KB_DATA_DIR/cache/matplotlib"
export XDG_CACHE_HOME="$KB_DATA_DIR/cache"
export PIP_CACHE_DIR="$KB_DATA_DIR/cache/pip"
export PYTHONDONTWRITEBYTECODE=1 HAYSTACK_TELEMETRY_ENABLED=false
export HAYSTACK_CONTENT_TRACING_ENABLED=false DD_TRACE_ENABLED=false
export OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=4 TOKENIZERS_PARALLELISM=false
export KB_LLM_TIMEOUT_SECONDS=120
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
if [[ "$ACTION" == setup ]]; then
  if [[ ! -x "$PYTHON" ]]; then
    "$KB_DATA_DIR/venv/bin/python" -m venv "$KB_DATA_DIR/venv-haystack"
  fi
  "$PYTHON" -m pip install -r requirements.haystack.lock
  "$PYTHON" -m pip check
  "$PYTHON" -m pip freeze > "$KB_DATA_DIR/artifacts/haystack-installed.txt"
elif [[ "$ACTION" == test ]]; then
  exec "$PYTHON" -m pytest --basetemp "$KB_DATA_DIR/tmp/haystack-pytest" tests/test_external_rag.py "$@"
else
  exec nice -n 10 "$PYTHON" -B scripts/eval_haystack.py "$ACTION" "$@"
fi
