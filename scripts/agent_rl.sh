#!/usr/bin/env bash
set -euo pipefail
task_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$task_source/scripts/rl_env.sh"
export PYTHONPATH="$task_source:$task_source/scripts:/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$task_source"
exec /data/zjr/enterprise_kb_agent/venv-harness/bin/python -B -m kb.rl.control "$@"
