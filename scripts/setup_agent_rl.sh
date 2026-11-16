#!/usr/bin/env bash
set -euo pipefail
task_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$task_source/scripts/rl_env.sh"
task_upstream="$KB_RL_ROOT/upstream/agent-lightning-v0.3.1"
task_commit=6db3c73dc0af2e50fe80e56873a57c1703f1b2a7
if [[ ! -d "$task_upstream/.git" ]]; then
  git clone --no-checkout --filter=blob:none https://github.com/microsoft/agent-lightning.git "$task_upstream"
  git -C "$task_upstream" checkout --detach "$task_commit"
fi
task_actual="$(git -C "$task_upstream" rev-parse HEAD)"
if [[ "$task_actual" != "$task_commit" ]]; then
  echo "Expected Agent Lightning $task_commit; found $task_actual. Existing checkout retained." >&2
  exit 1
fi
for task_kind in runner trainer; do
  if [[ "$task_kind" == runner ]]; then
    task_base=/data/zjr/enterprise_kb_agent/venv-harness/bin/python
  else
    task_base=/data/zjr/deepsearch_rl/venv/bin/python
  fi
  task_env="$KB_RL_ROOT/envs/$task_kind"
  if [[ ! -x "$task_env/bin/python" ]]; then
    "$task_base" -B -m venv --system-site-packages "$task_env"
  fi
  # venv does not inherit a parent venv's site-packages. Reuse its compatible
  # installed libraries read-only, while all additions go into this overlay.
  "$task_base" -B - "$task_env" <<'PY'
import pathlib, sys, sysconfig
dest=pathlib.Path(sys.argv[1])/f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
(dest/'task-parent.pth').write_text(sysconfig.get_path('purelib')+'\n')
PY
  "$task_env/bin/python" -B -m pip install --disable-pip-version-check -e "$task_upstream" \
    -r "$task_source/requirements.rl-proxy.lock"
done
"$KB_RL_ROOT/envs/trainer/bin/python" -B -m pip install --no-deps --force-reinstall 'verl==0.6.0'
for task_kind in runner trainer; do
  "$KB_RL_ROOT/envs/$task_kind/bin/python" -B -m pip freeze > "$KB_RL_ROOT/logs/$task_kind-packages.txt"
done
