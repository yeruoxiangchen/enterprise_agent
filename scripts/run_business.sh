#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME=/data/zjr/enterprise_kb_agent
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$RUNTIME/tmp" TORCH_HOME="$RUNTIME/cache/torch"
export HF_HOME="$RUNTIME/cache/huggingface" HF_HUB_CACHE="$RUNTIME/cache/huggingface/hub"
export TORCH_EXTENSIONS_DIR="$RUNTIME/cache/torch_extensions" MPLCONFIGDIR="$RUNTIME/cache/matplotlib"
export PIP_CACHE_DIR="$RUNTIME/cache/pip" XDG_CACHE_HOME="$RUNTIME/cache"
export HAYSTACK_TELEMETRY_ENABLED=false OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false
export LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false
umask 077
for file in model.env private.env; do
  if [[ -f "$RUNTIME/$file" ]]; then set -a; source "$RUNTIME/$file"; set +a; fi
done
# SDK clients inherit proxy settings, unlike our explicit trust_env=False health
# checks. Local model/tool APIs must stay local, including detached queues.
export NO_PROXY="127.0.0.1,localhost,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="127.0.0.1,localhost,::1${no_proxy:+,$no_proxy}"
export PYTHONPATH="$RUNTIME/business-packages-v2:$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="$RUNTIME/venv-agentpack/bin/python"
cd "$PROJECT_DIR"
ACTION="${1:-status}"; shift || true
case "$ACTION" in
  setup)
    "$PYTHON" -m pip install --target "$RUNTIME/business-packages-v2" -r requirements.business.lock
    "$PYTHON" -m pip list --path "$RUNTIME/business-packages-v2" --format=freeze > "$RUNTIME/business-requirements.lock"
    ;;
  test) exec "$PYTHON" -m pytest -p no:cacheprovider --basetemp "$RUNTIME/tmp/business-tests" "$@" ;;
  browser) exec env -u PYTHONPATH "$RUNTIME/venv/bin/python" -B scripts/business_browser.py ;;
  v2) exec "$PYTHON" -B scripts/business_v2_ops.py "$@" ;;
  v3)
    export KB_RERANKER_START_TIMEOUT=600
    if [[ -f "$RUNTIME/judge.env" ]]; then set -a; source "$RUNTIME/judge.env"; set +a; fi
    exec "$PYTHON" -B scripts/business_v3_ops.py "$@" ;;
  v4)
    export KB_RERANKER_START_TIMEOUT=600
    if [[ -f "$RUNTIME/judge.env" ]]; then set -a; source "$RUNTIME/judge.env"; set +a; fi
    exec "$PYTHON" -B scripts/business_v4_ops.py "$@" ;;
  supplement)
    export KB_RERANKER_START_TIMEOUT=600
    if [[ -f "$RUNTIME/judge.env" ]]; then set -a; source "$RUNTIME/judge.env"; set +a; fi
    exec "$PYTHON" -B scripts/business_v4_supplement.py "$@" ;;
  stop) exec "$PYTHON" -B scripts/business_control.py ;;
  *) exec "$PYTHON" -B scripts/business_ops.py "$ACTION" "$@" ;;
esac
