#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
V6_TEST_ROOT="/data/zjr/enterprise_kb_agent/artifacts/harness_v6_repair_20261111/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$V6_TEST_ROOT"
# All models are scripted. The old crash worker validates configuration before
# applying its scripted model; dummy local settings also prevent using a live API.
export KB_LLM_BASE_URL=http://127.0.0.1:9/v1 KB_LLM_MODEL=cpu-scripted-test
export KB_TOKENIZE_URL='' KB_EMBED_BASE_URL='' KB_EMBED_MODEL='' KB_RERANKER_PATH=''
cd "$PROJECT_DIR"
node --check kb/web/app.js
exec bash "$PROJECT_DIR/scripts/crm_bench_run.sh" test \
  tests/test_crm_v6.py tests/test_crm_v6_contract.py tests/test_crm_dialogue.py \
  tests/test_crm_user_wire.py tests/test_crm_overnight.py tests/test_crm_resume.py tests/test_crm_benchmark.py \
  tests/test_business_v6.py \
  tests/test_business_v5.py::test_real_service_persists_inline_fields_and_reuses_completed_result \
  tests/test_business_v5.py::test_abrupt_process_exit_recovers_without_duplicate_work \
  tests/test_business.py::test_ticket_confirmation_acl_and_lifecycle \
  --basetemp "$V6_TEST_ROOT/tmp" --junitxml "$V6_TEST_ROOT/report.xml" "$@"
