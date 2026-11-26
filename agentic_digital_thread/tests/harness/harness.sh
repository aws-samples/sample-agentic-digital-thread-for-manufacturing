#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# harness.sh — Tiered, reproducible cloud integration harness for S0.2.
#
# A single self-documenting CLI over the COLD -> WARM -> HOT feedback loop plus
# a guarded TEARDOWN. Every destructive subcommand is opt-in + dev-account (and,
# where relevant, target-graph) guarded BEFORE it touches anything; the guards
# live in `tests.harness._guard` and are enforced by the python modules this
# script orchestrates (graph_reset / cleanup), never re-implemented here.
#
#   COLD      Full one-script deploy (deploy.sh) + capture deployment env.
#   WARM      Reset Neptune (no redeploy) -> re-onboard cmms -> verify shape +
#             mapping-quality eval. Minutes (LLM-bound).
#   HOT       Re-run resolver integration + audit + mapping eval against the
#             existing graph + a local Postgres. Seconds-to-low-minutes.
#   TEARDOWN  Guarded clean-room teardown (repo cleanup.sh + local PG down -v).
#
# Subcommands print their scope and the EXACT sub-commands they run, so the
# script doubles as living documentation. All operations are idempotent and
# re-runnable. The demo password (ADT_PASSWORD) is NEVER printed.
#
# Usage:
#   tests/harness/harness.sh cold     [--profile <p>] [--region <r>] [--tar <path>]
#   tests/harness/harness.sh warm     [--yes] [--system-id <id>]
#   tests/harness/harness.sh warm2    [--yes]
#   tests/harness/harness.sh warm-cmms2 [--yes]
#   tests/harness/harness.sh hot
#   tests/harness/harness.sh teardown [--yes] [--profile <p>] [--region <r>]

set -euo pipefail

# ── Path resolution ──────────────────────────────────────────────────────────
# This script lives at agentic_digital_thread/tests/harness/harness.sh. The python
# module paths (tests.harness.*, tests.integration.*) and the relative test
# paths (tests/integration/..., tests/e2e/...) all resolve from agentic_digital_thread/,
# so we cd there. deploy.sh / cleanup.sh cd to their own dirs internally.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"      # agentic_digital_thread/
REPO_ROOT="$(cd "$ADT_DIR/.." && pwd)"          # repo root (holds cleanup.sh)
ENV_FILE="$SCRIPT_DIR/.harness-env"            # written by capture_env (gitignored)
COMPOSE_FILE="tests/integration/docker-compose.yml"

cd "$ADT_DIR"

# ── Small helpers ─────────────────────────────────────────────────────────────

# Log to stderr so stdout stays reserved for captured values / report output.
log() { printf '%s\n' "$*" >&2; }

# Print a self-documenting banner: the tier, its scope, and the exact commands.
announce() {
  local tier="$1"; shift
  log ""
  log "==> [$tier] $1"; shift
  log "    runs:"
  local c
  for c in "$@"; do log "      \$ $c"; done
  log ""
}

# Fail fast when POSTGRES_PASSWORD is unset. The integration compose file uses
# the required-env pattern (${POSTGRES_PASSWORD:?...}), and agents/db.py::
# get_conn_params() reads the same variable (fail-closed, no insecure default),
# so both the container and the test clients need it exported up front.
require_pg_password() {
  if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
    log "ERROR: POSTGRES_PASSWORD is not set. The local integration Postgres and the"
    log "       test clients (agents/db.py::get_conn_params) both read it — export a"
    log "       local-only value first, e.g.: export POSTGRES_PASSWORD=<local-dev-value>"
    exit 2
  fi
}

# Source the captured deployment env if present; otherwise warn and continue so
# the python guards (which run regardless) can refuse cleanly with their own
# message rather than this script failing on a missing file.
source_env_optional() {
  if [[ -f "$ENV_FILE" ]]; then
    log "    sourcing $ENV_FILE"
    # shellcheck disable=SC1090  # path is computed at runtime; nothing to follow
    source "$ENV_FILE"
    # Migration guard: a stale (pre-rename) capture exports old-prefix names
    # the harness no longer reads — fail loud instead of running half-configured.
    if [[ -z "${ADT_BASE_URL:-}" ]]; then
      log "ERROR: $ENV_FILE did not export ADT_BASE_URL (stale pre-rename capture?)."
      log "       Re-run 'python -m tests.harness.capture_env' to regenerate it with ADT_* names."
      exit 1
    fi
  else
    log "    NOTE: $ENV_FILE not found — run 'harness.sh cold' (or capture_env) first."
    log "          Destructive guards will refuse without a captured GRAPH_IDENTIFIER."
  fi
}

usage() {
  cat >&2 <<'EOF'
harness.sh — tiered cloud integration harness (COLD -> WARM -> HOT, + TEARDOWN)

Usage:
  harness.sh cold     [--profile <p>] [--region <r>] [--tar <path>]
      Full deploy via deploy.sh, then capture the deployment env (.harness-env).
  harness.sh warm     [--yes] [--system-id <id>]   (default system-id: cmms)
      Guarded graph reset -> re-onboard -> verify shape + mapping-quality eval.
  harness.sh warm2    [--yes]
      Guarded graph reset -> onboard cmms -> onboard erp -> verify shape.
      Two-system variant that creates the cross-system IdentityRules / LINKS_CONCEPT
      edges (ADR-0011 F1) which tests/integration/test_two_system_identity.py guards.
  harness.sh warm-cmms2 [--yes]
      Onboard the SYNONYM system cmms2 -> verify shape -> name-independence mapping
      eval (ADR-0012, W5: same meaning, different names, same thresholds as cmms).
      COLD PRECONDITION: cmms2_maintenance must be seeded into the deployed demo RDS
      (COLD redeploy+reseed) before onboarding can discover it — WARM alone cannot
      land the seed. With cmms2 absent the eval prints nothing_to_eval and exits 0.
  harness.sh hot
      Bring up + seed local Postgres, then run integration + audit + mapping eval.
  harness.sh teardown [--yes] [--profile <p>] [--region <r>]
      Guarded clean-room teardown (repo cleanup.sh + local Postgres down -v).

Destructive subcommands (warm/warm2/teardown) require opt-in: pass --yes or set
ADT_HARNESS_ALLOW_DESTRUCTIVE=1, and must run against the dev account/region.
EOF
}

# ── COLD ──────────────────────────────────────────────────────────────────────
# Full one-script deploy + capture the deployment outputs into .harness-env.
# Surfaces a non-zero deploy.sh exit; on success capture_env prints the captured
# GRAPH_IDENTIFIER / ADT_BASE_URL (it references ADT_PASSWORD by name only).
cmd_cold() {
  local profile="" region="" tar=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --profile) profile="$2"; shift 2 ;;
      --region)  region="$2";  shift 2 ;;
      --tar)     tar="$2";     shift 2 ;;
      *) log "ERROR: cold: unknown argument '$1'"; usage; exit 2 ;;
    esac
  done

  # Build the deploy invocation. JSII_SILENCE... quiets the CDK Node-version
  # warning exactly as a manual deploy would. `env` keeps it scoped to deploy.sh.
  local deploy=(env JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 bash "$ADT_DIR/deploy.sh")
  [[ -n "$profile" ]] && deploy+=(--profile "$profile")
  [[ -n "$region"  ]] && deploy+=(--region "$region")
  [[ -n "$tar"     ]] && deploy+=(--tar "$tar")

  local capture=(python -m tests.harness.capture_env)
  [[ -n "$profile" ]] && capture+=(--profile "$profile")
  [[ -n "$region"  ]] && capture+=(--region "$region")

  announce "COLD" \
    "deploy all stacks + seed core concepts + orchestrator READY, then capture env" \
    "${deploy[*]}" "${capture[*]}"

  "${deploy[@]}"           # set -e surfaces a non-zero deploy.sh exit
  "${capture[@]}"          # prints GRAPH_IDENTIFIER / ADT_BASE_URL (never the password)
  log "==> [COLD] done. Source the env: source $ENV_FILE"
}

# ── WARM ────────────────────────────────────────────────────────────────────--
# Reset the dev graph (guarded) -> re-onboard to runtime-READY via the Playwright
# flow -> verify graph shape -> print the mapping-quality score. WARM is GREEN
# only when BOTH the shape verify AND the mapping eval pass (set -e stops on the
# first failure). The reset's own guards refuse cleanly without opt-in.
cmd_warm() {
  local yes=0 system_id="cmms"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)       yes=1; shift ;;
      --system-id) system_id="$2"; shift 2 ;;
      *) log "ERROR: warm: unknown argument '$1'"; usage; exit 2 ;;
    esac
  done

  local reset=(python -m tests.harness.graph_reset reset)
  [[ "$yes" -eq 1 ]] && reset+=(--yes)
  # `python -m pytest` (not bare `pytest`) so the local `agents` package resolves
  # via the agentic_digital_thread CWD on sys.path — same import path every `python -m
  # tests.*` call here relies on; avoids needing an editable install / active venv.
  local onboard=(python -m pytest tests/e2e/test_onboarding.py -m e2e)
  local verify=(python -m tests.harness.graph_reset verify --system-id "$system_id")
  local eval=(python -m tests.integration.mapping_eval --system-id "$system_id")

  announce "WARM" \
    "reset graph (guarded) -> re-onboard $system_id -> verify shape -> mapping-quality eval" \
    "${reset[*]}" "${onboard[*]}" "${verify[*]}" "${eval[*]}"

  source_env_optional
  "${reset[@]}"            # guarded: refuses w/o opt-in (require_opt_in fires first)
  "${onboard[@]}"          # Playwright onboarding -> status flips only when runtime READY
  "${verify[@]}"           # exits 1 on shape fail
  "${eval[@]}"             # exits 1 on threshold fail
  log "==> [WARM] GREEN: shape verify AND mapping eval passed for '$system_id'."
}

# ── WARM2 ─────────────────────────────────────────────────────────────────────
# Two-system variant of WARM for the ADR-0011 F1 cross-system identity guard.
# Reset the dev graph (guarded) -> onboard cmms -> onboard erp through the SAME
# Playwright e2e flow (parameterized by ADT_ONBOARD_SYSTEM) -> verify graph shape.
# A >=2-system onboard is the only configuration that creates IdentityRules /
# LINKS_CONCEPT edges (a lone system creates none — ADR-0011 F1), so this is the
# tier that arms tests/integration/test_two_system_identity.py. WARM2 is GREEN
# only when both onboards succeed AND the shape verify passes (set -e stops on
# the first failure). The reset's own guards refuse cleanly without opt-in.
cmd_warm2() {
  local yes=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) yes=1; shift ;;
      *) log "ERROR: warm2: unknown argument '$1'"; usage; exit 2 ;;
    esac
  done

  local reset=(python -m tests.harness.graph_reset reset)
  [[ "$yes" -eq 1 ]] && reset+=(--yes)
  # `python -m pytest` (not bare `pytest`) so the local `agents` package resolves
  # via the agentic_digital_thread CWD on sys.path — same import path the rest of this
  # script relies on. ADT_ONBOARD_SYSTEM selects which catalog system the e2e flow
  # onboards (default cmms); we drive it twice for the 2-system graph.
  local onboard_cmms=(env ADT_ONBOARD_SYSTEM=cmms python -m pytest tests/e2e/test_onboarding.py -m e2e)
  local onboard_erp=(env ADT_ONBOARD_SYSTEM=erp python -m pytest tests/e2e/test_onboarding.py -m e2e)
  local verify=(python -m tests.harness.graph_reset verify)

  announce "WARM2" \
    "reset graph (guarded) -> onboard cmms -> onboard erp -> verify shape (2-system F1 guard)" \
    "${reset[*]}" "${onboard_cmms[*]}" "${onboard_erp[*]}" "${verify[*]}"

  source_env_optional
  "${reset[@]}"            # guarded: refuses w/o opt-in (require_opt_in fires first)
  "${onboard_cmms[@]}"     # Playwright onboarding of cmms -> status flips when runtime READY
  "${onboard_erp[@]}"      # Playwright onboarding of erp  -> creates cross-system IdentityRules
  "${verify[@]}"           # exits 1 on shape fail
  log "==> [WARM2] GREEN: cmms + erp onboarded and shape verify passed (2-system graph)."
}

# ── WARM-CMMS2 ────────────────────────────────────────────────────────────────
# Name-independence generalization variant (ADR-0012, W5). Onboard the SYNONYM
# maintenance system cmms2 -> verify graph shape -> score its mappings against the
# authored cmms2 key (SAME meaning, DIFFERENT names, SAME thresholds as cmms).
#
# COLD PRECONDITION: cmms2_maintenance must already be SEEDED INTO THE DEPLOYED demo
# RDS before onboarding can discover it. `discover` introspects the deployed RDS
# (NOT the local Postgres), and the 010_cmms2_synonyms.sql seed only lands in the
# adt-seed-data Lambda bundle on a stack redeploy. So this requires a COLD
# redeploy+reseed first (`harness.sh cold ...` then a reseed) — WARM alone is
# insufficient. With cmms2 absent, the mapping eval prints `nothing_to_eval` and
# exits 0 (skip-clean).
#
# Mirrors WARM's structure: reset the graph (guarded) is NOT done here so an
# existing cmms onboard is preserved; this only ADDS cmms2 to the graph. WARM-CMMS2
# is GREEN only when both the shape verify AND the cmms2 mapping eval pass (set -e
# stops on the first failure).
cmd_warm_cmms2() {
  local yes=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) yes=1; shift ;;
      *) log "ERROR: warm-cmms2: unknown argument '$1'"; usage; exit 2 ;;
    esac
  done

  # `python -m pytest` (not bare `pytest`) so the local `agents` package resolves
  # via the agentic_digital_thread CWD on sys.path — same import path the rest of this
  # script relies on. ADT_ONBOARD_SYSTEM=cmms2 selects the synonym catalog system.
  local onboard=(env ADT_ONBOARD_SYSTEM=cmms2 python -m pytest tests/e2e/test_onboarding.py -m e2e)
  local verify=(python -m tests.harness.graph_reset verify --system-id cmms2)
  local eval=(python -m tests.integration.mapping_eval --system-id cmms2)

  announce "WARM-CMMS2" \
    "onboard cmms2 (synonym schema) -> verify shape -> name-independence mapping eval" \
    "${onboard[*]}" "${verify[*]}" "${eval[*]}"

  # Onboarding is opt-in-gated like the other LLM-driven flows; honour --yes via env.
  [[ "$yes" -eq 1 ]] && export ADT_HARNESS_ALLOW_DESTRUCTIVE=1
  source_env_optional
  "${onboard[@]}"          # Playwright onboarding of cmms2 -> status flips when runtime READY
  "${verify[@]}"           # exits 1 on shape fail
  "${eval[@]}"             # exits 1 on threshold fail; 0 + nothing_to_eval if cmms2 absent
  log "==> [WARM-CMMS2] GREEN: cmms2 shape verify AND name-independence mapping eval passed."
}

# ── HOT ─────────────────────────────────────────────────────────────────────--
# Ensure a seeded local Postgres, source the deployment env (if any), then run
# the integration + audit suites. Everything degrades to clean SKIPs: no docker
# -> no local PG (seed_pg self-skips), no creds/graph -> graph cases skip.
cmd_hot() {
  [[ $# -eq 0 ]] || { log "ERROR: hot takes no arguments (got '$*')"; usage; exit 2; }
  require_pg_password

  local up=(docker compose -f "$COMPOSE_FILE" up -d)
  local seed=(python tests/integration/seed_pg.py)
  # `python -m pytest` so the local `agents` package resolves via the CWD on
  # sys.path (bare `pytest` may not put CWD on the path -> ModuleNotFoundError).
  local test=(python -m pytest tests/integration tests/audit -m "integration or audit")

  announce "HOT" \
    "ensure local Postgres up+seeded, then run resolver integration + audit + mapping eval" \
    "${up[*]}" "${seed[*]}" "${test[*]}"

  if command -v docker >/dev/null 2>&1; then
    log "    starting local Postgres..."
    if "${up[@]}"; then
      wait_pg_ready
    else
      log "    WARNING: compose up failed — continuing (seed_pg will skip if PG unreachable)."
    fi
  else
    log "    docker not found — skipping local Postgres start (seed_pg will skip gracefully)."
  fi
  "${seed[@]}"             # exits 0 with a skip line when no PG is reachable
  source_env_optional      # graph cases skip when GRAPH_IDENTIFIER is unset

  log "    running test suites..."
  local rc=0
  "${test[@]}" || rc=$?    # surface pytest's exit (0 when all-skipped, non-zero on real failure)
  if [[ "$rc" -eq 0 ]]; then
    log "==> [HOT] pytest exit 0 (passes and/or clean skips)."
  else
    log "==> [HOT] pytest exit $rc — see output above."
  fi
  exit "$rc"
}

# Bounded, tolerant wait for the compose Postgres to accept connections, using
# the same pg_isready check the compose healthcheck uses. Never fails the run —
# if it times out, seed_pg's own connect-or-skip handles it.
wait_pg_ready() {
  local _
  for _ in $(seq 1 30); do
    if docker compose -f "$COMPOSE_FILE" exec -T postgres \
         pg_isready -U postgres -d adt_demo >/dev/null 2>&1; then
      log "    local Postgres ready."
      return 0
    fi
    sleep 1
  done
  log "    WARNING: Postgres not ready after 30s — seed_pg will skip if still unreachable."
}

# ── TEARDOWN ────────────────────────────────────────────────────────────────--
# Guarded clean-room teardown: opt-in + dev-account FIRST (no target-graph guard
# since we are destroying everything), then the repo cleanup.sh and a local
# Postgres `down -v`.
cmd_teardown() {
  local yes=0 profile="" region=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)     yes=1; shift ;;
      --profile) profile="$2"; shift 2 ;;
      --region)  region="$2";  shift 2 ;;
      *) log "ERROR: teardown: unknown argument '$1'"; usage; exit 2 ;;
    esac
  done

  local cleanup=(bash "$REPO_ROOT/cleanup.sh")
  [[ -n "$profile" ]] && cleanup+=(--profile "$profile")
  [[ -n "$region"  ]] && cleanup+=(--region "$region")
  # `down -v` still interpolates the compose file's required ${POSTGRES_PASSWORD:?},
  # but the value is irrelevant for teardown — supply a placeholder when unset so
  # the local volume is always removed.
  local down=(env "POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-teardown-placeholder}"
              docker compose -f "$COMPOSE_FILE" down -v)

  announce "TEARDOWN" \
    "guard (opt-in + dev account), then destroy cloud stacks + local Postgres volume" \
    "${cleanup[*]}" "${down[*]}"

  # Reuse the python guards verbatim — opt-in honours --yes (passed via env) or
  # ADT_HARNESS_ALLOW_DESTRUCTIVE=1; assert_dev_account does an STS identity read.
  [[ "$yes" -eq 1 ]] && export ADT_HARNESS_ALLOW_DESTRUCTIVE=1
  python - <<'PY'
import sys
from tests.harness import _guard
try:
    _guard.require_opt_in()
    _guard.assert_dev_account()
except _guard.GuardError as exc:
    print(f"ERROR (guard): {exc}", file=sys.stderr)
    sys.exit(1)
PY

  "${cleanup[@]}"          # repo-root clean-room teardown of the CDK stacks
  if command -v docker >/dev/null 2>&1; then
    "${down[@]}" || log "    WARNING: compose down -v failed (local PG may already be gone)."
  else
    log "    docker not found — skipping local Postgres 'down -v'."
  fi
  log "==> [TEARDOWN] done."
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
main() {
  local cmd="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "$cmd" in
    cold)         cmd_cold "$@" ;;
    warm)         cmd_warm "$@" ;;
    warm2)        cmd_warm2 "$@" ;;
    warm-cmms2)   cmd_warm_cmms2 "$@" ;;
    hot)          cmd_hot "$@" ;;
    teardown)     cmd_teardown "$@" ;;
    -h|--help|help) usage; exit 0 ;;
    "")           log "ERROR: missing subcommand."; usage; exit 2 ;;
    *)            log "ERROR: unknown subcommand: '$cmd'"; usage; exit 2 ;;
  esac
}

main "$@"
