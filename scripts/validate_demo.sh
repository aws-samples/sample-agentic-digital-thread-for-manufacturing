#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# validate_demo.sh — one green/red gate that proves a deployment delivers the demo.
#
# Chains the validation stages and aggregates them into a single exit code:
#   1. verify_deploy.sh   — infra/health readout (app reachable, HighByte/SiteWise if present)
#   2. onboarding_smoketest.sh — deterministically onboard all demo systems (idempotent)
#   3. smoke_prompts.py   — run demo prompts, assert grounded + cross-system answers
#   4. pytest tests/e2e   — Playwright UI suite (only with --with-e2e or if installed)
#
# The demo IS the post-deploy smoke test: if this is green, the deployment works.
#
# Usage:
#   scripts/validate_demo.sh --profile <P> [--region us-east-1] [--count 4]
#                            [--systems a,b,c] [--with-e2e] [--skip-onboard]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PROFILE=""; REGION="${AWS_DEFAULT_REGION:-us-east-1}"
COUNT=4; SYSTEMS=""; WITH_E2E=0; SKIP_ONBOARD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --region)  REGION="$2";  shift 2;;
    --count)   COUNT="$2";   shift 2;;
    --systems) SYSTEMS="$2"; shift 2;;
    --with-e2e) WITH_E2E=1; shift;;
    --skip-onboard) SKIP_ONBOARD=1; shift;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done
AWS=(aws --region "$REGION"); [[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

# --- resolve URL + password once, share with every stage via env ---
echo "==> Resolving deployed app URL + demo password..."
ADT_BASE_URL="$("${AWS[@]}" cloudformation describe-stacks --stack-name ADTComputeStack \
  --query "Stacks[0].Outputs[?OutputKey=='ADTCloudFrontUrl'].OutputValue" --output text 2>/dev/null)"
ADT_PASSWORD="$("${AWS[@]}" secretsmanager get-secret-value --secret-id adt-demo-password \
  --query SecretString --output text 2>/dev/null)"
if [[ -z "$ADT_BASE_URL" || "$ADT_BASE_URL" == "None" || -z "$ADT_PASSWORD" ]]; then
  echo "ERROR: could not resolve ADTCloudFrontUrl or adt-demo-password" >&2
  exit 2
fi
export ADT_BASE_URL ADT_PASSWORD ADT_USERNAME="${ADT_USERNAME:-ADT}"
echo "    app: $ADT_BASE_URL"

RESULTS=()
run_stage() {  # name, command...
  local name="$1"; shift
  echo; echo "########################################################"
  echo "# STAGE: $name"
  echo "########################################################"
  if "$@"; then RESULTS+=("PASS  $name"); else RESULTS+=("FAIL  $name"); fi
}
skip_stage() { RESULTS+=("SKIP  $1"); echo; echo "# STAGE: $1 — SKIPPED ($2)"; }

PROF_ARGS=(--region "$REGION"); [[ -n "$PROFILE" ]] && PROF_ARGS+=(--profile "$PROFILE")

# 1. infra/health
run_stage "verify_deploy" scripts/verify_deploy.sh "${PROF_ARGS[@]}"

# 2. onboarding (idempotent)
if [[ "$SKIP_ONBOARD" == "1" ]]; then
  skip_stage "onboarding_smoketest" "--skip-onboard"
else
  ON_ARGS=("${PROF_ARGS[@]}"); [[ -n "$SYSTEMS" ]] && ON_ARGS+=(--systems "$SYSTEMS")
  run_stage "onboarding_smoketest" scripts/onboarding_smoketest.sh "${ON_ARGS[@]}"
fi

# 3. grounded + cross-system prompt assertions
run_stage "smoke_prompts" python3 scripts/smoke_prompts.py \
  --base-url "$ADT_BASE_URL" --password "$ADT_PASSWORD" --count "$COUNT"

# 4. Playwright UI suite (opt-in / only if installed)
if python3 -c "import playwright" >/dev/null 2>&1 || [[ "$WITH_E2E" == "1" ]]; then
  if [[ "$WITH_E2E" == "1" ]]; then
    (cd agentic_digital_thread && pip install -q -e '.[test]' && python -m playwright install chromium) || true
  fi
  run_stage "e2e_playwright" bash -c "cd agentic_digital_thread && python -m pytest tests/e2e -q"
else
  skip_stage "e2e_playwright" "playwright not installed; pass --with-e2e to run"
fi

# --- aggregate ---
echo; echo "########################################################"
echo "# VALIDATION SUMMARY"
echo "########################################################"
fail=0
for r in "${RESULTS[@]}"; do
  echo "  $r"
  [[ "$r" == FAIL* ]] && fail=1
done
echo
if [[ "$fail" == "0" ]]; then
  echo "== RESULT: PASS — deployment delivers the demo =="
  exit 0
fi
echo "== RESULT: FAIL — see stages above =="
exit 1
