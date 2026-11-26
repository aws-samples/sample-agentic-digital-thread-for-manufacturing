#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# deploy.sh — Deploy the Agentic Digital Thread sample: CDK stacks + AgentCore agents
#
# Usage:
#   ./deploy.sh [--profile <aws-profile>] [--region <aws-region>]
#               [--seed]                 # add static sample data (engine stays default base)
#               [--simulator]            # add the live ERP simulation (implies --seed)
#               [--highbyte]             # add the HighByte OT/UNS layer (independent of --seed/--simulator)
#               [--accept-highbyte-eula] # accept the HighByte EULA, needed once before HighByte deploys (sticky)
#               [--highbyte-sitewise-pipeline enabled|disabled]  # HighByte's NameSpace_to_SiteWise pipeline (needs HighByte; sticky; default disabled with SFC, else enabled; ADR-0082)
#               [--opcua]                # add only the simulated OPC UA servers, no HighByte (sticky; implied by --highbyte)
#               [--sfc]                  # add SFC, the OPC UA -> SiteWise + S3 Tables writer (sticky; implies --opcua; ADR-0082)
#               [--observability]        # opt-in: CloudWatch Transaction Search span ingestion + in-app drill-down (sticky; trace-ingestion COST)
#               [--quick-api]            # opt-in: AWS Quick API Gateway front door + query-bridge gateway target (NOT sticky)
#               [--quick-connector]      # opt-in: AWS Quick MCP connector path only (bridge-only gateway + client secret; ADR-0093; NOT sticky)
#               [--edge-auth on|off]     # Cognito hosted-UI redirect at the edge for the app (sticky; default ON)
#               [--oidc-issuer <https-url> --oidc-client-id <id> --oidc-secret-arn <arn> [--oidc-name <ProviderName>]]
#                                        # optional external OIDC identity provider on every hosted UI (sticky; see docs/deployment.md)
#               [--oidc-only on|off]     # make that provider the ONLY sign-in on every edge client (no local Cognito form; sticky)
#               [--quick-oidc-discovery <https-url> --quick-oidc-audience <QuickGatewayUrl>[,<url>...]]
#                                        # Quick gateway accepts delegated user tokens from an external OIDC IdP (needs --quick-api or --quick-connector; sticky; ADR-0079)
#               [--query-mode agent|orchestrator]        # how cross-system SQL is composed (sticky; default agent — ADR-0038)
#               [--integrity-mode annotate|enforce]      # response-integrity posture (sticky; unset = code default report-only — ADR-0047)
#               [--auto-accept off|on]   # self-learning auto-accept of high-confidence value mappings (default off)
#               [--cross-account-role <arn>[,<arn>...]|convention]   # data-account role ARN(s) the app/runtime roles may assume
#               [--domain <fqdn> --hosted-zone-id <id> --hosted-zone-name <zone>]   # custom domain on the app distribution (sticky)
#
# Data mode (default = BASE): with no data flags the deploy is engine-only — no
# pre-connected systems; you onboard your own data sources post-deploy. --seed adds
# the bundled static sample data; --simulator adds the live ERP simulation + dashboard
# (and implies --seed). These
# are NOT sticky — an environment that should keep the
# full demo MUST pass the flags on every deploy, or the demo-data stacks are removed.
#
# Edge auth (--edge-auth on|off): Cognito Authorization@Edge on the app
# distribution (SAR cloudfront-authorization-at-edge — the same gate as the ERP
# dashboard/HighByte UI). Unauthenticated browsers are redirected to the Cognito
# HOSTED UI (an externally verifiable auth boundary); /api/* and /health bypass the gate by
# design (APIs keep their own 401 auth, so the CLI eval/smoke lanes and the SSE
# stream are unaffected). Sticky via SSM /adt/edge-auth-enabled; default ON.
#
# External OIDC identity provider (--oidc-issuer/--oidc-client-id/
# --oidc-secret-arn, optional --oidc-name): bring your own OpenID Connect
# provider. Every user pool with an Authorization@Edge client (ADT app, ERP
# dashboard, HighByte UI) gets a Cognito OIDC provider and its hosted UI shows
# a second sign-in button next to the local Cognito directory. The client
# secret is read from the given Secrets Manager secret at deploy time (never
# in a template or argument). Sticky via SSM /adt/oidc-*; off-switch = delete
# those parameters. --oidc-only on removes the local Cognito directory from
# every edge client so the hosted-UI chooser is skipped and an unauthenticated
# browser is sent straight to the external provider (sticky, SSM
# /adt/oidc-only). Details + redirect-URI derivation: docs/deployment.md.
#
# Without --highbyte, the HighByte OPC UA stack is skipped. With --highbyte, the stack
# deploys and runs the public highbyte/intelligence-hub image from Docker Hub. The
# container runs only with ACCEPT_EULA=Y, which agrees to the HighByte EULA, so the
# first HighByte deploy also needs --accept-highbyte-eula (sticky, SSM
# /adt/highbyte-eula-accepted). The enable flag is sticky (persisted in SSM),
# so later deploys keep HighByte without it. --highbyte is independent of the demo
# data: it deploys the OPC UA -> HighByte -> AWS IoT SiteWise layer on top of whatever
# data mode is active (base alone, or with --seed/--simulator). When --simulator is
# also set, the HighByte setup additionally wires the ERP/historian Postgres connections.
set -euo pipefail
cd "$(dirname "$0")"

# ── Per-run scratch dir ──────────────────────────────────────────────────────
# Scratch files for THIS run (the CDK log, AWS error text captured by the
# post-CDK steps) live in one private dir, so two deploys on one host never
# read or overwrite each other's files. The dir is removed on a clean exit and
# kept, with its path printed, when the deploy fails and wrote something there
# (so the CDK log can still be read). An interrupt (Ctrl-C, kill) exits with
# the usual 128+signal status, so it counts as a failure and keeps the logs.
# Step 2f's wait bookkeeping (runtime ids, versions, last status) is removed
# either way; no runtime settings are ever written here. This is the script's
# only EXIT trap: put any other exit-time cleanup in _deploy_tmp_on_exit,
# because a second `trap ... EXIT` would replace this one.
_deploy_tmp_on_exit() {
  local rc=$?
  if [ -n "${DEPLOY_TMP:-}" ] && [ -d "$DEPLOY_TMP" ]; then
    rm -rf "$DEPLOY_TMP/rt_refresh/state"
    rmdir "$DEPLOY_TMP/rt_refresh" 2>/dev/null || true
    if [ "$rc" -ne 0 ] && [ -n "$(ls -A "$DEPLOY_TMP" 2>/dev/null)" ]; then
      echo "    Logs from this run (CDK output, AWS error text): $DEPLOY_TMP" >&2
    else
      rm -rf "$DEPLOY_TMP"
    fi
  fi
  return "$rc"
}
setup_deploy_tmp() {
  DEPLOY_TMP=$(mktemp -d "${TMPDIR:-/tmp}/adt-deploy.XXXXXX")
  # Absolute, so a later cd (the script moves between dirs) still finds it.
  # CDPATH is cleared so cd cannot print the dir into the substitution.
  DEPLOY_TMP=$(CDPATH= cd -P -- "$DEPLOY_TMP" && pwd -P)
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap _deploy_tmp_on_exit EXIT
}
setup_deploy_tmp

# Activate a project virtualenv that can import aws_cdk, if one isn't already
# active. The CDK app (app.py) imports aws_cdk and cdk_nag, which are installed
# from ../requirements.txt into the repo-root ../.venv. A package-local .venv
# built only for unit tests (pip install -e '.[test]') does NOT contain aws_cdk,
# so activating it blindly still fails `npx cdk` with ModuleNotFoundError.
# Probe known venv locations and activate the first whose python imports
# aws_cdk — so either the repo-root venv or a fully-provisioned package venv
# works, instead of hard-coding one path.
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  for _venv in ../.venv .venv ../venv venv; do
    if [[ -x "$_venv/bin/python" ]] && "$_venv/bin/python" -c "import aws_cdk" >/dev/null 2>&1; then
      # shellcheck disable=SC1091
      source "$_venv/bin/activate"
      break
    fi
  done
fi

# ── Pre-flight: required tooling ─────────────────────────────────────────────
# Fail fast with a clear checklist if any required CLI is missing or the venv
# can't import aws_cdk (the CDK app needs it). Docker is required because the
# agent runtime + app images are built as CDK DockerImageAssets during deploy.
check_prereqs() {
  local missing=0
  echo "==> Checking prerequisites..."
  for tool in aws docker npm python3; do
    if command -v "$tool" >/dev/null 2>&1; then
      echo "    [ok]   $tool"
    else
      echo "    [MISSING] $tool not found on PATH"
      missing=1
    fi
  done
  if python3 -c "import aws_cdk" >/dev/null 2>&1; then
    echo "    [ok]   python aws_cdk importable"
  else
    echo "    [MISSING] python aws_cdk not importable. Build the repo-root CDK venv:"
    echo "                python3 -m venv ../.venv && source ../.venv/bin/activate && pip install -r ../requirements.txt"
    echo "              (a package-local .venv from 'pip install -e .[test]' has TEST deps only, NOT aws_cdk/cdk_nag)"
    missing=1
  fi
  if [ "$missing" -ne 0 ]; then
    echo "ERROR: missing prerequisites listed above; aborting." >&2
    exit 1
  fi
}
check_prereqs

# Parse flags
# NOTE: `_`-prefixed flag accumulators (here and the *_CDK_FLAG(S) vars below)
# signal INTENTIONAL word-splitting — the semgrep convention for
# unquoted-variable-expansion-in-command. Each expands to zero-or-more CLI
# words. Array-free on purpose: bash 3.2 + `set -u` safe (see subnet lists).
_AWS_PROFILE_FLAG=""
HB_ENABLE=0
HB_EULA_ACCEPT=0     # --accept-highbyte-eula: the deployer accepts the HighByte EULA (sticky, SSM /adt/highbyte-eula-accepted)
HB_SITEWISE_PIPELINE=""      # --highbyte-sitewise-pipeline enabled|disabled (sticky, SSM /adt/highbyte-sitewise-pipeline)
HB_SITEWISE_PIPELINE_SET=0
OPCUA_ENABLE=0
SFC_ENABLE=0
OBS_ENABLE=0         # --observability: opt-in span ingestion + drill-down (sticky, default OFF — cost)
QUICK_API_ENABLE=0   # --quick-api: opt-in AWS Quick API Gateway + query-bridge target (NOT sticky)
QUICK_CONNECTOR_ENABLE=0 # --quick-connector: opt-in Quick MCP connector path (ADR-0093; NOT sticky)
EDGE_AUTH_MODE=""    # --edge-auth on|off: Cognito hosted-UI redirect at the edge (sticky, default ON)
WITH_SEED=""
WITH_SIMULATOR=""
QUERY_MODE="agent"   # ADR-0035/0038 A/B toggle: agent (Mode A, DEFAULT) | orchestrator (Mode B)
INTEGRITY_MODE=""    # ADR-0047 posture: ""=code default (report-only) | annotate (recommended) | enforce
INTEGRITY_MODE_SET=0        # 1 = --integrity-mode passed explicitly (drives its SSM sticky write)
_INTEGRITY_CDK_FLAG=""
QUERY_MODE_SET=0            # 1 = --query-mode passed explicitly (drives the SSM sticky write;
                            # unset = eligible for the sticky SSM read further below)
AUTO_ACCEPT="off"    # self-learning: auto-accept high-confidence value_mappings (off|on)
# Cross-account data role: cdk.json ships a GENERIC empty default (no real account id
# in the sample). Override here (flag) or via the CROSS_ACCOUNT_DATA_ROLE_ARN env to
# grant the app/runtime sts:AssumeRole for onboarding REAL cross-account Athena/SiteWise
# data during testing. Value forms (stacks/_cross_account.py):
#   <arn>              single role ARN (original behavior)
#   <arn>,<arn>,...    comma-separated list of role ARNs (NO spaces — the value must
#                      survive unquoted shell word-splitting into the cdk -c flag)
#   convention         naming-convention wildcard arn:aws:iam::*:role/ADTCrossAccount*
#                      (self-service: any data account with the standard trust-pinned
#                      role template becomes onboardable with no redeploy; the data
#                      account's trust policy + sts:ExternalId remain the real gate)
# Empty => no grant (the generic sample default).
CROSS_ACCOUNT_ROLE="${CROSS_ACCOUNT_DATA_ROLE_ARN:-}"
# Optional custom domain (Route 53 + ACM) for the ADT app distribution — all
# three must be provided together on the first enabling deploy (validated in
# app.py); afterwards they are sticky via SSM /adt/custom-domain{,-zone-id,-zone-name}.
CUSTOM_DOMAIN=""
HOSTED_ZONE_ID=""
HOSTED_ZONE_NAME=""
# Optional external OIDC identity provider — issuer, client id and client-secret
# ARN must be provided together on the first enabling deploy (validated in
# app.py); the provider name is optional (default CorporateIdP). Sticky via
# SSM /adt/oidc-{issuer-url,client-id,client-secret-arn,provider-name}.
OIDC_ISSUER=""
OIDC_CLIENT_ID=""
OIDC_SECRET_ARN=""
OIDC_NAME=""
OIDC_ONLY_MODE=""   # --oidc-only on|off: external provider as the SOLE sign-in on every edge client (sticky)
# Delegated user tokens on the dedicated Quick gateway (ADR-0079): the external
# OIDC identity provider's discovery URL + the audience (= the QuickGatewayUrl
# output, incl. /mcp). Both together; sticky via SSM /adt/quick-oidc-*.
QUICK_OIDC_DISCOVERY=""
QUICK_OIDC_AUDIENCE=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --profile) export AWS_PROFILE="$2"; _AWS_PROFILE_FLAG="--profile $2"; shift 2 ;;
    --region) export AWS_DEFAULT_REGION="$2"; shift 2 ;;
    --highbyte) HB_ENABLE=1; shift ;;
    --accept-highbyte-eula) HB_EULA_ACCEPT=1; shift ;;
    --highbyte-sitewise-pipeline)
      # Validated here, before any AWS call: an unknown value never reaches SSM or CDK.
      if [[ $# -lt 2 ]]; then echo "ERROR: --highbyte-sitewise-pipeline needs enabled|disabled" >&2; exit 1; fi
      case "$2" in
        enabled|disabled) ;;
        *) echo "ERROR: --highbyte-sitewise-pipeline must be 'enabled' or 'disabled' (got '$2')." >&2; exit 1 ;;
      esac
      HB_SITEWISE_PIPELINE="$2"; HB_SITEWISE_PIPELINE_SET=1; shift 2 ;;
    --opcua) OPCUA_ENABLE=1; shift ;;
    --sfc) SFC_ENABLE=1; shift ;;
    --observability) OBS_ENABLE=1; shift ;;
    --quick-api) QUICK_API_ENABLE=1; shift ;;
    --quick-connector) QUICK_CONNECTOR_ENABLE=1; shift ;;
    --edge-auth) EDGE_AUTH_MODE="$2"; shift 2 ;;
    --seed) WITH_SEED="true"; shift ;;
    --simulator) WITH_SIMULATOR="true"; shift ;;
    --query-mode) QUERY_MODE="$2"; QUERY_MODE_SET=1; shift 2 ;;
    --integrity-mode) INTEGRITY_MODE="$2"; INTEGRITY_MODE_SET=1; shift 2 ;;
    --auto-accept) AUTO_ACCEPT="$2"; shift 2 ;;
    --cross-account-role) CROSS_ACCOUNT_ROLE="$2"; shift 2 ;;
    --domain) CUSTOM_DOMAIN="$2"; shift 2 ;;
    --hosted-zone-id) HOSTED_ZONE_ID="$2"; shift 2 ;;
    --hosted-zone-name) HOSTED_ZONE_NAME="$2"; shift 2 ;;
    --oidc-issuer) OIDC_ISSUER="$2"; shift 2 ;;
    --oidc-client-id) OIDC_CLIENT_ID="$2"; shift 2 ;;
    --oidc-secret-arn) OIDC_SECRET_ARN="$2"; shift 2 ;;
    --oidc-name) OIDC_NAME="$2"; shift 2 ;;
    --oidc-only) OIDC_ONLY_MODE="$2"; shift 2 ;;
    --quick-oidc-discovery) QUICK_OIDC_DISCOVERY="$2"; shift 2 ;;
    --quick-oidc-audience) QUICK_OIDC_AUDIENCE="$2"; shift 2 ;;
    *) echo "Usage: ./deploy.sh [--profile <p>] [--region <r>] [--seed] [--simulator] [--highbyte] [--accept-highbyte-eula] [--highbyte-sitewise-pipeline enabled|disabled] [--opcua] [--sfc] [--observability] [--quick-api] [--quick-connector] [--edge-auth on|off] [--query-mode agent|orchestrator] [--integrity-mode annotate|enforce] [--auto-accept off|on] [--cross-account-role <arn>[,<arn>...]|convention] [--domain <fqdn> --hosted-zone-id <id> --hosted-zone-name <zone>] [--oidc-issuer <https-url> --oidc-client-id <id> --oidc-secret-arn <arn> [--oidc-name <ProviderName>]] [--oidc-only on|off] [--quick-oidc-discovery <https-url> --quick-oidc-audience <QuickGatewayUrl>]"; exit 1 ;;
  esac
done
# Propagate the query mode to (a) the ECS app via CDK context (agent_manager reads it
# when provisioning system runtimes) and (b) the orchestrator runtime env via the
# contract's ${SYSTEM_QUERY_MODE:-agent} default (built inside the §2c driver).
# One flag flips the whole stack for the A/B comparison.
export SYSTEM_QUERY_MODE="$QUERY_MODE"
_MODE_CDK_FLAG=" -c system_query_mode=$QUERY_MODE"
# Propagate a Bedrock model override to the ECS app too, so the app env and the
# orchestrator runtime stay in lockstep on ONE source of truth. BEDROCK_MODEL feeds
# the orchestrator runtime env through the runtime-env contract inside the
# provisioning driver (§2c, scripts/provision_runtimes.py);
# adt_compute_stack reads `-c bedrock_model` for the app/orchestrator model.
# Unset = cdk.json default.
_MODEL_CDK_FLAG=""
[[ -n "${BEDROCK_MODEL:-}" ]] && _MODEL_CDK_FLAG=" -c bedrock_model=$BEDROCK_MODEL"
# System-agent model override (the app's SYSTEM_AGENT_MODEL); unset = the
# catalog's AGENT default (stacks/adt_compute_stack.py).
[[ -n "${SYSTEM_AGENT_MODEL:-}" ]] && _MODEL_CDK_FLAG+=" -c system_agent_model=$SYSTEM_AGENT_MODEL"
# Optional model allowlist for the Admin LLM settings page (comma-separated
# catalog family ids, NO spaces so the unquoted flag stays one word); passed
# through as `-c allowed_models` (stacks/_model_allowlist.py). Unset = no limit.
[[ -n "${ALLOWED_MODELS:-}" ]] && _MODEL_CDK_FLAG+=" -c allowed_models=$ALLOWED_MODELS"
# Self-learning auto-accept (high-confidence value_mappings only). Wired to BOTH the
# ECS app (CDK context) and the orchestrator runtime env (§2c driver) since proposals are
# logged in both. Default off — the sample ships conservative; enable per demo.
export SELF_LEARNING_AUTO_ACCEPT="$AUTO_ACCEPT"
_AUTO_ACCEPT_CDK_FLAG=" -c self_learning_auto_accept=$AUTO_ACCEPT"
# Cross-account role override -> CDK context (stacks read try_get_context; empty in
# cdk.json = no grant). Lets the generic sample default stand while testing against
# real cross-account data via `--cross-account-role <arn>[,<arn>...]|convention` or
# CROSS_ACCOUNT_DATA_ROLE_ARN (comma lists must have NO spaces so the unquoted flag
# expansion below stays a single word).
_XACCT_CDK_FLAG=""
[[ -n "$CROSS_ACCOUNT_ROLE" ]] && _XACCT_CDK_FLAG=" -c crossAccountDataRoleArn=$CROSS_ACCOUNT_ROLE"

# App version (git-derived, delivered to the running task). Short SHA (+ "-dirty" when
# the working tree has local changes) + UTC build date. Passed as `-c app_version`
# QUOTED on the deploy line below (the value contains spaces, so it cannot ride
# the unquoted composed-flags variables); adt_compute_stack forwards it as the
# adt-app container's APP_VERSION ENV VAR (not an image build-arg — a build-arg
# would put the daily date into the asset hash and rebuild/push the image every
# deploy) and agents/server.py serves it on /health + /api/version (the UI
# version badge). Falls back to "dev" outside a git checkout.
APP_VERSION="dev"
if GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null)"; then
  GIT_DIRTY=""
  [[ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]] && GIT_DIRTY="-dirty"
  APP_VERSION="$GIT_SHA$GIT_DIRTY · $(date -u +%Y-%m-%d)"
fi
echo "==> App version: $APP_VERSION"

# Demo-data deployment mode (default = base / engine-only). --simulator implies --seed
# (the live simulator writes cross-system records into seed-defined schemas). --highbyte
# is independent (it does NOT imply --simulator): the HighByte stack deploys on top of
# whatever data mode is active. These map to CDK context flags consumed by app.py to
# gate the ERP simulator stacks and the static-seed custom resource.
# stacks app.py creates only with with_simulator.
# >>> data-mode context
if [[ "$WITH_SIMULATOR" == "true" ]]; then WITH_SEED="true"; fi
_DATA_CDK_FLAGS=""
[[ "$WITH_SEED" == "true" ]] && _DATA_CDK_FLAGS+=" -c with_seed=true"
[[ "$WITH_SIMULATOR" == "true" ]] && _DATA_CDK_FLAGS+=" -c with_simulator=true"
# <<< data-mode context
if [[ "$WITH_SIMULATOR" == "true" ]]; then
  echo "==> Data mode: FULL (static seed + live ERP simulator + ERP dashboard)"
elif [[ "$WITH_SEED" == "true" ]]; then
  echo "==> Data mode: SEED (static sample data only; no live simulator)"
else
  echo "==> Data mode: BASE (engine only; no pre-connected systems — onboard your own)."
  echo "    NOTE: if this environment previously deployed with --seed/--simulator, deploying"
  echo "    now WITHOUT them will TEAR DOWN the ERP simulator stacks (incl. its Aurora data)."
fi

REGION="${AWS_DEFAULT_REGION:-us-east-1}"

# Verify credentials
ACCOUNT=$(aws sts get-caller-identity $_AWS_PROFILE_FLAG --query Account --output text --region "$REGION")
echo "==> Account: $ACCOUNT  Region: $REGION"


# >>> database CIDR state (tests/test_deploy_db_cidr_phases.py runs this block)
# Each demo database admits PostgreSQL from its client groups only
# (stacks/_db_access.py), and most client rules live in stacks that update
# after the database stacks. A database stack from before those rules still
# admits the whole VPC; dropping that rule in the same deploy would cut its
# clients off until their own stacks update. So each database stack in this
# deploy is read here, before anything is changed, and classified on its own
# (the "database CIDR phases" block below deploys accordingly):
# - old (a settled stack with no DatabaseCidrIngressState output) or keep (an
#   earlier phase 2 did not finish): it admits the whole VPC already. Phase 1
#   deploys it with DatabaseCidrIngress=keep, so its rule stays while every
#   client rule is created; phase 2 then moves it to remove.
# - remove, or absent (including a failed create or a never-executed change
#   set, which cdk deploy replaces): deployed with remove in phase 1. It never
#   gets the rule, not even when phase 1 stops partway.
# - anything else stops the deploy: a read that fails other than "does not
#   exist", a stack mid-operation or in a failed state, another output value.
#   An unreadable state is never taken as old or new. There is no override:
#   the same read is what cdk deploy needs, and a stack mid-operation or in a
#   failed state cannot be updated by it either.
_DB_CIDR_STACKS=()
[[ "$WITH_SEED" == "true" ]] && _DB_CIDR_STACKS+=(ADTSeedDataStack)
[[ "$WITH_SIMULATOR" == "true" ]] && _DB_CIDR_STACKS+=(ADTERPDatabaseStack)
# absent | old | keep | remove on stdout; otherwise the reason, and status 1.
_db_cidr_state() {
  local out status value
  if ! out=$(aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" $_AWS_PROFILE_FLAG \
      --query "Stacks[0].[StackStatus, Outputs[?OutputKey=='DatabaseCidrIngressState'].OutputValue | [0]]" \
      --output text 2>&1); then
    case "$out" in *"does not exist"*) echo absent; return 0 ;; esac
    echo "describe-stacks failed: ${out##*$'\n'}"; return 1
  fi
  read -r status value <<<"${out##*$'\n'}" || true
  case "$status" in
    ROLLBACK_COMPLETE|REVIEW_IN_PROGRESS) echo absent; return 0 ;;
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE|IMPORT_COMPLETE|IMPORT_ROLLBACK_COMPLETE) ;;
    *) echo "the stack is ${status:-in an unreadable state}"; return 1 ;;
  esac
  case "$value" in
    keep|remove) echo "$value" ;;
    None) echo old ;;
    *) echo "unexpected DatabaseCidrIngressState '$value'"; return 1 ;;
  esac
}
_DB_CIDR_PHASE1_ARGS=()   # --parameters per database stack: its own phase-1 value
_DB_CIDR_KEPT=()          # the stacks phase 1 deploys with keep (phase 2 moves them)
_DB_CIDR_TWO_PHASES=0
for _db_stack in ${_DB_CIDR_STACKS[@]+"${_DB_CIDR_STACKS[@]}"}; do
  if ! _db_state=$(_db_cidr_state "$_db_stack"); then
    echo "ERROR: could not read the DatabaseCidrIngress state of $_db_stack: $_db_state." >&2
    echo "       Nothing has been changed. Whether it still admits PostgreSQL from the whole VPC" >&2
    echo "       decides how it is deployed, so this deploy stops: fix the read (credentials, or" >&2
    echo "       wait for the stack's operation to finish, or resolve its failed state), then re-run." >&2
    exit 1
  fi
  _db_value=remove
  if [[ "$_db_state" == "old" || "$_db_state" == "keep" ]]; then
    _db_value=keep; _DB_CIDR_KEPT+=("$_db_stack"); _DB_CIDR_TWO_PHASES=1
    echo "    $_db_stack still admits PostgreSQL from the whole VPC ($_db_state): it keeps that rule in phase 1 and drops it in phase 2."
  fi
  _DB_CIDR_PHASE1_ARGS+=(--parameters "$_db_stack:DatabaseCidrIngress=$_db_value")
done
# <<< database CIDR state

# ── HighByte (optional OT/UNS layer) ─────────────────────────────────────────
# HighByte Intelligence Hub runs the public highbyte/intelligence-hub image
# from Docker Hub; no licensed binary is shipped or staged. Pass --highbyte to
# enable the layer; the flag is sticky (persisted in SSM) so later deploys keep
# HighByte without it. Without it (and with no prior enable), HighByte is
# skipped and the rest of the demo deploys as a reference architecture without
# the OT/UNS layer.
#
# The hub container runs only with ACCEPT_EULA=Y, and setting it agrees to the
# HighByte Intelligence Hub EULA. So a deploy that includes HighByte needs the
# deployer's explicit acceptance, given ONCE with --accept-highbyte-eula: it
# prints the EULA link and records SSM /adt/highbyte-eula-accepted. Every
# HighByte deploy then passes -c highbyte_accept_eula=true, the only source of
# ACCEPT_EULA=Y in the stack (stacks/_highbyte_options.py). This section runs
# right after the Region and credential check, so a deploy without a recorded
# acceptance stops here with the link before it installs or builds anything
# (CDK CLI, i3X Explorer bundle), writes to AWS (no SSM write precedes this
# section) or runs CDK. Deleting the parameter
# withdraws the acceptance: the next HighByte deploy stops again.
HB_EULA_URL="https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf"
HB_DOCKER_GUIDE_URL="https://guide.highbyte.com/release/setup/docker/"
_HB_CDK_FLAGS=""
# >>> highbyte sitewise pipeline sticky reads (tests/test_highbyte_sitewise_pipeline_flag.py runs this block)
# The sticky values that decide HighByte's SiteWise pipeline (below, after the
# SFC section): /adt/highbyte-enabled, /adt/sfc-enabled and
# /adt/highbyte-sitewise-pipeline. All three are read HERE, before the first
# SSM write and before CDK, through _ssm_sticky_get: only ParameterNotFound
# means "not set". Any other failure (access denied, throttling, expired
# credentials, a transport error) stops the deploy naming the parameter,
# because reading a saved "true" as absent could leave HighByte's pipeline on
# beside SFC (two SiteWise writers) or switch it off by surprise.
_ssm_sticky_get() {
  local name="$1" out errf rc=0
  errf="$(mktemp "${TMPDIR:-/tmp}/adt-ssm-get.XXXXXX")"
  out=$(aws ssm get-parameter --name "$name" --region "$REGION" $_AWS_PROFILE_FLAG \
        --query "Parameter.Value" --output text 2>"$errf") || rc=$?
  local err
  err="$(cat "$errf")"; rm -f "$errf"
  if [[ "$rc" -eq 0 ]]; then printf '%s\n' "$out"; return 0; fi
  if [[ "$err" == *"ParameterNotFound"* ]]; then return 0; fi
  echo "ERROR: could not read SSM parameter $name (aws exited $rc). This deploy needs it to decide HighByte, SFC and HighByte's SiteWise pipeline." >&2
  echo "    AWS said: ${err:-<no error text>}" >&2
  echo "    Nothing was written and CDK was not run. Fix the credentials, permissions or network, then re-run." >&2
  return 1
}
# A read an explicit flag makes irrelevant is skipped, so a failure on it
# cannot stop the deploy: --highbyte decides HighByte, --sfc decides SFC, an
# explicit --highbyte-sitewise-pipeline decides the pipeline and the warning.
HB_ENABLED=""
if [[ "$HB_ENABLE" -ne 1 ]]; then
  HB_ENABLED=$(_ssm_sticky_get "/adt/highbyte-enabled") || exit 1
fi
SFC_ENABLED_STICKY=""
if [[ "$SFC_ENABLE" -ne 1 ]]; then
  SFC_ENABLED_STICKY=$(_ssm_sticky_get "/adt/sfc-enabled") || exit 1
fi
_HB_IN_DEPLOY=0
[[ "$HB_ENABLE" -eq 1 || "$HB_ENABLED" == "true" ]] && _HB_IN_DEPLOY=1
# --highbyte-sitewise-pipeline switches the hub's pipeline, so it needs a
# deploy that includes HighByte.
if [[ "$HB_SITEWISE_PIPELINE_SET" -eq 1 && "$_HB_IN_DEPLOY" -ne 1 ]]; then
  echo "ERROR: --highbyte-sitewise-pipeline applies only to a deploy that includes HighByte: add --highbyte." >&2
  exit 1
fi
_HB_PIPELINE_STICKY=""
if [[ "$_HB_IN_DEPLOY" -eq 1 && "$HB_SITEWISE_PIPELINE_SET" -ne 1 ]]; then
  _HB_PIPELINE_STICKY=$(_ssm_sticky_get "/adt/highbyte-sitewise-pipeline") || exit 1
  case "$_HB_PIPELINE_STICKY" in
    enabled|disabled|"") ;;
    *)
      echo "ERROR: SSM /adt/highbyte-sitewise-pipeline is '$_HB_PIPELINE_STICKY', not enabled or disabled." >&2
      echo "    Re-run with --highbyte-sitewise-pipeline enabled|disabled, or delete the parameter for the default." >&2
      exit 1 ;;
  esac
fi
# <<< highbyte sitewise pipeline sticky reads
HB_EULA_ACCEPTED=$(aws ssm get-parameter --name "/adt/highbyte-eula-accepted" \
  --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
if [[ "$HB_ENABLE" -eq 1 || "$HB_ENABLED" == "true" ]]; then
  if [[ "$HB_EULA_ACCEPT" -ne 1 && "$HB_EULA_ACCEPTED" != "true" ]]; then
    echo "ERROR: HighByte is part of this deploy, but the HighByte Intelligence Hub End User License Agreement has not been accepted." >&2
    echo "    The hub container runs only with ACCEPT_EULA=Y, and setting it means you agree to that agreement." >&2
    echo "    Read it: $HB_EULA_URL" >&2
    echo "    HighByte's Docker guide: $HB_DOCKER_GUIDE_URL" >&2
    echo "    To accept it, re-run this deploy with --accept-highbyte-eula (needed once; recorded in SSM /adt/highbyte-eula-accepted)." >&2
    exit 1
  fi
  if [[ "$HB_EULA_ACCEPT" -eq 1 ]]; then
    aws ssm put-parameter --name "/adt/highbyte-eula-accepted" --value "true" \
      --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
    echo "==> HighByte EULA accepted with --accept-highbyte-eula (recorded in SSM /adt/highbyte-eula-accepted; later deploys need no flag)."
  else
    echo "==> HighByte EULA: accepted earlier (SSM /adt/highbyte-eula-accepted)."
  fi
  echo "    HighByte Intelligence Hub End User License Agreement: $HB_EULA_URL"
  _HB_CDK_FLAGS+=" -c highbyte_accept_eula=true"
elif [[ "$HB_EULA_ACCEPT" -eq 1 ]]; then
  echo "ERROR: --accept-highbyte-eula applies only to a deploy that includes HighByte: add --highbyte." >&2
  exit 1
fi
if [[ "$HB_ENABLE" -eq 1 ]]; then
  _HB_CDK_FLAGS+=" -c highbyte_enabled=true"
  # Persist the sticky enable flag so FUTURE deploys keep the HighByte stack.
  aws ssm put-parameter --name "/adt/highbyte-enabled" --value "true" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> HighByte enabled (sticky): running the public Docker Hub image; future deploys keep HighByte without --highbyte."
  echo "    NOTE: you are responsible for obtaining any required HighByte license (the hub boots in trial mode without one)."
elif [[ "$HB_ENABLED" == "true" ]]; then
  _HB_CDK_FLAGS+=" -c highbyte_enabled=true"
  echo "==> HighByte is enabled (sticky): keeping the HighByte stack (public Docker Hub image)."
  echo "    NOTE: you are responsible for obtaining any required HighByte license (the hub boots in trial mode without one)."
else
  echo "==> Deploying without HighByte (--highbyte not provided). 7-stack reference architecture; ERP/MES/CMMS/PLM + agents will work, OT/UNS layer is skipped."
fi

# ── Pre-flight: ensure local AWS CDK CLI is installed ────────────────────────
# The CDK CLI is a Node.js binary; the project pins it via package.json so
# every deploy uses a consistent version regardless of what's installed
# globally. Run `npm install` in the repo root if node_modules is missing.
REPO_ROOT="$(cd .. && pwd)"
if [ ! -x "$REPO_ROOT/node_modules/.bin/cdk" ]; then
  echo "==> Installing local CDK CLI (one-time, from package.json)..."
  ( cd "$REPO_ROOT" && npm install --silent ) 2>&1 | tail -5
fi
CDK_VERSION="$("$REPO_ROOT/node_modules/.bin/cdk" --version)"
echo "==> CDK CLI: $CDK_VERSION"

# ── Pre-flight: build the embedded i3X Explorer web bundle ───────────────────
# The admin panel embeds the MIT-licensed i3X Explorer (vendored at ../i3x-explorer)
# served at /i3x-explorer/. Its build output is not committed, so build it here
# into static/i3x-explorer/ — the adt-app image (Dockerfile: COPY static/) bakes it
# in during `cdk deploy`. Node/npm is already required (for the CDK CLI).
# Rebuilds when the SOURCE HASH changes: sha256 over i3x-explorer/{src,public,
# index.html,package.json,package-lock.json,vite.config.ts,tsconfig*.json,
# tailwind/postcss configs} is stored in static/i3x-explorer/.source-hash, so a
# source change can never ship a stale bundle (the previous "skip when
# index.html exists" check did exactly that). `npm ci` only — a lockfile that
# does not match package.json is a real error, not something to paper over with
# `npm install`. Non-fatal: the explorer is an optional admin viewer, so a build
# hiccup must not block the rest of the demo deploy.
I3X_SRC="$REPO_ROOT/i3x-explorer"
I3X_DEST="$(pwd)/static/i3x-explorer"
i3x_source_hash() {
  ( cd "$I3X_SRC" && find src public index.html package.json package-lock.json vite.config.ts \
        tsconfig.json tsconfig.node.json tailwind.config.js postcss.config.js -type f 2>/dev/null \
      | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -d' ' -f1 )
}
if [ -d "$I3X_SRC" ]; then
  I3X_HASH="$(i3x_source_hash)"
  I3X_BUILT_HASH="$(cat "$I3X_DEST/.source-hash" 2>/dev/null || echo "")"
  if [ ! -f "$I3X_DEST/index.html" ] || [ "$I3X_HASH" != "$I3X_BUILT_HASH" ]; then
    echo "==> Building embedded i3X Explorer web bundle (source hash ${I3X_HASH:0:12})..."
    if ( cd "$I3X_SRC" \
         && export npm_config_registry="https://registry.npmjs.org/" \
         && npm ci --silent \
         && npm run build:vite ); then
      rm -rf "$I3X_DEST" && mkdir -p "$I3X_DEST" && cp -R "$I3X_SRC/dist/." "$I3X_DEST/"
      echo "$I3X_HASH" > "$I3X_DEST/.source-hash"
      touch "$I3X_DEST/.gitkeep"  # tracked placeholder — keeps the dir in git (Dockerfile COPY static/)
      echo "    Embedded i3X Explorer: ✅ ($I3X_DEST)"
    else
      echo "    [WARN] i3X Explorer build failed (npm ci needs a lockfile that matches package.json) — the embedded explorer will be unavailable (non-fatal)."
    fi
  else
    echo "==> Embedded i3X Explorer bundle is current (source hash ${I3X_HASH:0:12}) — skipping build."
  fi
else
  echo "    [WARN] i3x-explorer/ source not found — embedded explorer unavailable (non-fatal)."
fi

# OPC UA servers without HighByte (optional, sticky; --highbyte implies them).
# app.py resolves opcua_enabled = highbyte_enabled OR -c opcua_enabled OR SSM
# /adt/opcua-enabled, so the only job here is persisting the enable once.
if [[ "$OPCUA_ENABLE" -eq 1 ]]; then
  _HB_CDK_FLAGS+=" -c opcua_enabled=true"
  aws ssm put-parameter --name "/adt/opcua-enabled" --value "true" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> OPC UA servers enabled (sticky) — future deploys keep them without --opcua."
elif [[ "$(aws ssm get-parameter --name "/adt/opcua-enabled" --region "$REGION" $_AWS_PROFILE_FLAG \
          --query "Parameter.Value" --output text 2>/dev/null || echo "")" == "true" ]]; then
  _HB_CDK_FLAGS+=" -c opcua_enabled=true"
  echo "==> OPC UA servers are enabled (sticky): keeping them."
fi

# SFC (optional, sticky; implies the OPC UA servers — app.py resolves
# opcua_enabled from sfc_enabled too). Same persistence pattern as --opcua.
if [[ "$SFC_ENABLE" -eq 1 ]]; then
  _HB_CDK_FLAGS+=" -c sfc_enabled=true"
  aws ssm put-parameter --name "/adt/sfc-enabled" --value "true" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> SFC enabled (sticky) — future deploys keep it without --sfc."
elif [[ "$SFC_ENABLED_STICKY" == "true" ]]; then
  # Read with the HighByte sticky values, before any SSM write (_ssm_sticky_get).
  _HB_CDK_FLAGS+=" -c sfc_enabled=true"
  echo "==> SFC is enabled (sticky): keeping it."
fi

# HighByte's NameSpace_to_SiteWise pipeline (ADR-0082): CDK context
# highbyte_sitewise_pipeline=enabled|disabled (stacks/_highbyte_options.py).
# `disabled` imports the pipeline with every trigger off and gives the hub's
# task role no SiteWise permission, so HighByte stays the UNS; when SFC is
# enabled it is then the only SiteWise writer and the SFC freshness watchdog
# is built (stacks/_sfc_watchdog.py). Without SFC nothing writes SiteWise. Order: an explicit --highbyte-sitewise-pipeline
# (persisted in SSM /adt/highbyte-sitewise-pipeline), else that sticky value,
# else the default: disabled when this deploy has both HighByte and SFC,
# enabled when it has HighByte alone (the hub is then the SiteWise writer).
# The default is never persisted. Deleting the parameter returns to it.
# >>> highbyte sitewise pipeline (tests/test_highbyte_sitewise_pipeline_flag.py runs this block)
_HB_PIPELINE_DEFAULT_APPLIED=0   # 1 = this deploy applies the default `disabled` (HighByte + SFC)
if [[ "$_HB_CDK_FLAGS" == *"highbyte_enabled=true"* ]]; then
  _HB_SFC_ON=0
  [[ "$_HB_CDK_FLAGS" == *"sfc_enabled=true"* ]] && _HB_SFC_ON=1
  if [[ "$HB_SITEWISE_PIPELINE_SET" -eq 1 ]]; then
    aws ssm put-parameter --name "/adt/highbyte-sitewise-pipeline" --value "$HB_SITEWISE_PIPELINE" \
      --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
    _HB_PIPELINE_WHY="set with --highbyte-sitewise-pipeline; recorded in SSM /adt/highbyte-sitewise-pipeline, later deploys keep it"
  elif [[ -n "$_HB_PIPELINE_STICKY" ]]; then
    # Read and validated with the other sticky values (_ssm_sticky_get, above).
    HB_SITEWISE_PIPELINE="$_HB_PIPELINE_STICKY"
    _HB_PIPELINE_WHY="set earlier, SSM /adt/highbyte-sitewise-pipeline; delete it to return to the default"
  elif [[ "$_HB_SFC_ON" -eq 1 ]]; then
    HB_SITEWISE_PIPELINE="disabled"
    _HB_PIPELINE_DEFAULT_APPLIED=1
    _HB_PIPELINE_WHY="default with HighByte and SFC: HighByte stays the UNS, SFC is the only SiteWise writer; pass --highbyte-sitewise-pipeline enabled for both writers"
  else
    HB_SITEWISE_PIPELINE="enabled"
    _HB_PIPELINE_WHY="default with HighByte and no SFC: the hub is the SiteWise writer"
  fi
  _HB_CDK_FLAGS+=" -c highbyte_sitewise_pipeline=$HB_SITEWISE_PIPELINE"
  # Every deploy that applies the default says so, the same way each time
  # (no state kept for it), before CDK. Before this switch existed, --highbyte
  # --sfc ran the pipeline beside SFC; this line names how to keep that.
  if [[ "$_HB_PIPELINE_DEFAULT_APPLIED" -eq 1 ]]; then
    echo "==> HighByte SiteWise pipeline: disabled by default because SFC is on (SFC is the only SiteWise writer). Pass --highbyte-sitewise-pipeline enabled to have HighByte write SiteWise too."
  else
    echo "==> HighByte SiteWise pipeline: $HB_SITEWISE_PIPELINE ($_HB_PIPELINE_WHY)."
  fi
  if [[ "$HB_SITEWISE_PIPELINE" == "enabled" && "$_HB_SFC_ON" -eq 1 ]]; then
    echo "    NOTE: HighByte and SFC both write the same SiteWise asset properties, so the SFC freshness watchdog is not built."
  elif [[ "$HB_SITEWISE_PIPELINE" == "disabled" && "$_HB_SFC_ON" -ne 1 ]]; then
    echo "    [WARN] HighByte's SiteWise pipeline is off and SFC is not deployed: no continuous SiteWise writer."
  fi
fi
# <<< highbyte sitewise pipeline

# OPC UA datasets (gzip CSVs, gitignored) are baked into the OPC server image.
# `cdk synth` is PURE — it never writes into the checkout — so the deterministic
# generator (random.seed(42)) runs HERE when the OPC UA servers are active
# (--highbyte, --opcua or --sfc) and any site dataset is missing. ~1-2 min on first
# run; skipped when all four exist.
if [[ "$_HB_CDK_FLAGS" == *"highbyte_enabled=true"* || "$_HB_CDK_FLAGS" == *"opcua_enabled=true"* || "$_HB_CDK_FLAGS" == *"sfc_enabled=true"* ]]; then
  _DS_DIR="$REPO_ROOT/highbyte/datasets"
  if [[ ! -f "$_DS_DIR/site1_milwaukee_bottling.csv.gz" || ! -f "$_DS_DIR/site2_detroit_automotive.csv.gz" || ! -f "$_DS_DIR/site3_cedarrapids_food.csv.gz" || ! -f "$_DS_DIR/site4_indianapolis_machining.csv.gz" ]]; then
    echo "==> Generating OPC UA datasets (highbyte/generate_datasets.py)..."
    ( cd "$REPO_ROOT" && python3 highbyte/generate_datasets.py ) || { echo "ERROR: dataset generation failed"; exit 1; }
  fi
fi

# ── Observability drill-down (optional, opt-in — sticky) ─────────────────────
# Mirrors the HighByte sticky-flag convention above. --observability enables
# CloudWatch Transaction Search span ingestion (account/region-level) plus the
# in-app per-session drill-down reader (AGENTS_OBSERVABILITY_DRILLDOWN env +
# span read IAM on the ECS app, wired by stacks/_observability.py via the
# `observability` CDK context resolved in app.py). Default OFF: span ingestion
# bills continuously.
# The flag is sticky (SSM /adt/observability-enabled); app.py does
# the same fail-soft read (stacks/_sticky_context.py) so raw `cdk deploy` is
# covered too. Off-switch = delete the SSM parameter (same semantics as
# HighByte) and optionally revert the X-Ray destination (see below). Span
# EMISSION on the agent runtimes (AGENT_OBSERVABILITY_ENABLED) is on with this
# flag (as app.py decides it for the app's own runtimes): SPAN_EMISSION
# reaches the runtime driver as AGENT_SPAN_EMISSION, and off turns the runtime
# exporters off (no span destination to send to).
_OBS_CDK_FLAG=""
SPAN_EMISSION=false
OBS_ENABLED_SSM=$(aws ssm get-parameter --name "/adt/observability-enabled" \
  --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
if [[ "$OBS_ENABLE" -eq 1 ]]; then
  _OBS_CDK_FLAG=" -c observability=true"
  SPAN_EMISSION=true
  # Persist the sticky enable flag so FUTURE deploys keep the drill-down.
  aws ssm put-parameter --name "/adt/observability-enabled" --value "true" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> Observability drill-down enabled (sticky) — future deploys keep it without --observability."
elif [[ "$OBS_ENABLED_SSM" == "true" ]]; then
  _OBS_CDK_FLAG=" -c observability=true"
  SPAN_EMISSION=true
  echo "==> Observability drill-down is enabled (sticky, from SSM /adt/observability-enabled)."
else
  echo "==> Deploying without the observability drill-down (--observability not provided; default off — span ingestion has cost)."
fi
if [[ -n "$_OBS_CDK_FLAG" ]]; then
  # Account/region-level CloudWatch Transaction Search enablement. Idempotent
  # (current state is checked first) and NON-FATAL (a failure warns and the
  # deploy continues — the drill-down simply reports no spans until enabled
  # manually). Sampling percentage is conservative by default; override with
  # OBSERVABILITY_SAMPLING_PCT=<0-100> in the environment.
  OBS_SAMPLING_PCT="${OBSERVABILITY_SAMPLING_PCT:-10}"
  echo "    ⚠️  COST WARNING: Transaction Search ingests OTEL trace spans into CloudWatch Logs"
  echo "       (each runtime log group's 'spans' stream and 'aws/spans', indexed at ${OBS_SAMPLING_PCT}%). Ingestion + storage bill"
  echo "       continuously while enabled. Disable: delete the SSM parameter"
  echo "       /adt/observability-enabled, then"
  echo "       'aws xray update-trace-segment-destination --destination XRay'."
  OBS_CURRENT_DEST=$(aws xray get-trace-segment-destination --region "$REGION" $_AWS_PROFILE_FLAG \
    --query "Destination" --output text 2>/dev/null || echo "")
  if [[ "$OBS_CURRENT_DEST" == "CloudWatchLogs" ]]; then
    echo "    Transaction Search already active (destination=CloudWatchLogs) — leaving it (and any custom indexing rule) unchanged."
  else
    if aws xray update-trace-segment-destination --destination CloudWatchLogs \
        --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null 2>&1; then
      echo "    Transaction Search destination set to CloudWatchLogs."
    else
      echo "    [WARN] Could not set the X-Ray trace-segment destination (non-fatal). Enable"
      echo "           Transaction Search manually: CloudWatch console → Application Signals →"
      echo "           Transaction Search. The drill-down shows no spans until then."
    fi
    if aws xray update-indexing-rule --name "Default" \
        --rule "{\"Probabilistic\":{\"DesiredSamplingPercentage\":${OBS_SAMPLING_PCT}}}" \
        --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null 2>&1; then
      echo "    Span indexing rule set (Probabilistic ${OBS_SAMPLING_PCT}%)."
    else
      echo "    [WARN] Could not set the span indexing rule (non-fatal); the service default applies."
    fi
  fi
fi

# ── Edge auth (Cognito hosted-UI redirect on the app distribution — sticky) ──
# Mirrors the observability sticky-flag convention above. The SAR
# cloudfront-authorization-at-edge gate redirects unauthenticated browsers to
# the Cognito HOSTED UI, so the application never serves its own credential
# form — the same pattern as the ERP dashboard and the HighByte UI. /api/* and
# /health bypass the gate by design (APIs keep their own 401 auth), so the
# CLI eval/smoke lanes (POST /api/auth/login) and the /api/ask SSE stream are
# unaffected. Sticky via SSM /adt/edge-auth-enabled (stores "true"/"false" —
# unlike the enable-only flags, OFF must persist too since the default is ON);
# app.py does the same fail-soft read (stacks/_sticky_context.py). Default =
# ON.
_EDGE_AUTH_CDK_FLAG=""
EDGE_AUTH_SSM=$(aws ssm get-parameter --name "/adt/edge-auth-enabled" \
  --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
if [[ -n "$EDGE_AUTH_MODE" ]]; then
  case "$EDGE_AUTH_MODE" in
    on)  EDGE_AUTH_VALUE="true" ;;
    off) EDGE_AUTH_VALUE="false" ;;
    *) echo "ERROR: --edge-auth takes 'on' or 'off' (got '$EDGE_AUTH_MODE')." >&2; exit 1 ;;
  esac
  _EDGE_AUTH_CDK_FLAG=" -c edge_auth=$EDGE_AUTH_VALUE"
  # Persist the sticky value so FUTURE deploys keep this mode without the flag.
  aws ssm put-parameter --name "/adt/edge-auth-enabled" --value "$EDGE_AUTH_VALUE" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> Edge auth: $EDGE_AUTH_MODE (persisted sticky in SSM /adt/edge-auth-enabled)."
elif [[ "$EDGE_AUTH_SSM" == "true" || "$EDGE_AUTH_SSM" == "false" ]]; then
  EDGE_AUTH_VALUE="$EDGE_AUTH_SSM"
  echo "==> Edge auth: $([[ "$EDGE_AUTH_VALUE" == "true" ]] && echo on || echo off) (sticky, from SSM /adt/edge-auth-enabled — app.py resolves the same value)."
else
  EDGE_AUTH_VALUE="true"
  echo "==> Edge auth: on (default — unauthenticated browsers are redirected to the Cognito hosted UI; disable with --edge-auth off)."
fi

# ── Custom domain (optional, opt-in — sticky) ────────────────────────────────
# Serve the ADT app on your own domain: --domain <fqdn> --hosted-zone-id <id>
# --hosted-zone-name <zone> (all three together on the first enabling deploy;
# app.py fails loud on partial config). Requires a Route 53 PUBLIC hosted zone
# in THIS account that is authoritative for the domain. app.py then adds a
# us-east-1 ACM certificate stack (ADTCertificateStack, DNS-validated) and
# wires the CloudFront alias + A/AAAA records (stacks/_custom_domain.py).
# Sticky (SSM /adt/custom-domain{,-zone-id,-zone-name}) so a flag-less deploy
# cannot silently drop the domain from a live environment. Off-switch = delete
# the three SSM parameters (the next deploy removes alias + records; delete
# the orphaned ADTCertificateStack manually).
_DOMAIN_CDK_FLAGS=""
if [[ -n "$CUSTOM_DOMAIN" || -n "$HOSTED_ZONE_ID" || -n "$HOSTED_ZONE_NAME" ]]; then
  if [[ -z "$CUSTOM_DOMAIN" || -z "$HOSTED_ZONE_ID" || -z "$HOSTED_ZONE_NAME" ]]; then
    echo "ERROR: --domain, --hosted-zone-id and --hosted-zone-name must be provided together." >&2
    exit 1
  fi
  _DOMAIN_CDK_FLAGS=" -c custom_domain=$CUSTOM_DOMAIN -c hosted_zone_id=$HOSTED_ZONE_ID -c hosted_zone_name=$HOSTED_ZONE_NAME"
  # Persist the sticky values so FUTURE deploys keep the domain without the flags.
  aws ssm put-parameter --name "/adt/custom-domain" --value "$CUSTOM_DOMAIN" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  aws ssm put-parameter --name "/adt/custom-domain-zone-id" --value "$HOSTED_ZONE_ID" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  aws ssm put-parameter --name "/adt/custom-domain-zone-name" --value "$HOSTED_ZONE_NAME" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> Custom domain https://$CUSTOM_DOMAIN enabled (sticky) — future deploys keep it without the flags."
else
  STICKY_DOMAIN=$(aws ssm get-parameter --name "/adt/custom-domain" \
    --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
  if [[ -n "$STICKY_DOMAIN" && "$STICKY_DOMAIN" != "None" ]]; then
    echo "==> Custom domain: $STICKY_DOMAIN (sticky, from SSM /adt/custom-domain — app.py resolves the zone params)."
  fi
fi

# ── External OIDC identity provider (optional, opt-in — sticky) ──────────────
# Bring your own OpenID Connect provider for the hosted-UI sign-in:
# --oidc-issuer <https-url> --oidc-client-id <id> --oidc-secret-arn <arn>
# (all three together on the first enabling deploy; app.py fails loud on partial
# config) plus optional --oidc-name <ProviderName> (default CorporateIdP — the
# hosted-UI button label). The secret must already exist in Secrets Manager in
# this account/Region with the client secret as its plain SecretString; the
# template only carries a {{resolve:secretsmanager:...}} dynamic reference.
# Register https://<hosted-ui-domain>/oauth2/idpresponse at the provider for
# EVERY pool that gets the provider (docs/deployment.md shows how to derive the
# domains from the account id before deploying). Sticky (SSM /adt/oidc-*) so a
# flag-less deploy cannot silently drop the corporate sign-in button;
# off-switch = delete the SSM parameters and redeploy.
_OIDC_CDK_FLAGS=""
if [[ -n "$OIDC_ISSUER" || -n "$OIDC_CLIENT_ID" || -n "$OIDC_SECRET_ARN" || -n "$OIDC_NAME" ]]; then
  if [[ -z "$OIDC_ISSUER" || -z "$OIDC_CLIENT_ID" || -z "$OIDC_SECRET_ARN" ]]; then
    echo "ERROR: --oidc-issuer, --oidc-client-id and --oidc-secret-arn must be provided together (--oidc-name is optional)." >&2
    exit 1
  fi
  _OIDC_CDK_FLAGS=" -c oidc_issuer_url=$OIDC_ISSUER -c oidc_client_id=$OIDC_CLIENT_ID -c oidc_client_secret_arn=$OIDC_SECRET_ARN"
  [[ -n "$OIDC_NAME" ]] && _OIDC_CDK_FLAGS+=" -c oidc_provider_name=$OIDC_NAME"
  # Persist the sticky values so FUTURE deploys keep the provider without the flags.
  aws ssm put-parameter --name "/adt/oidc-issuer-url" --value "$OIDC_ISSUER" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  aws ssm put-parameter --name "/adt/oidc-client-id" --value "$OIDC_CLIENT_ID" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  aws ssm put-parameter --name "/adt/oidc-client-secret-arn" --value "$OIDC_SECRET_ARN" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  if [[ -n "$OIDC_NAME" ]]; then
    aws ssm put-parameter --name "/adt/oidc-provider-name" --value "$OIDC_NAME" \
      --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  fi
  echo "==> External OIDC identity provider ${OIDC_NAME:-CorporateIdP} ($OIDC_ISSUER) enabled (sticky) — future deploys keep it without the flags."
else
  STICKY_OIDC=$(aws ssm get-parameter --name "/adt/oidc-issuer-url" \
    --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
  if [[ -n "$STICKY_OIDC" && "$STICKY_OIDC" != "None" ]]; then
    echo "==> External OIDC identity provider: $STICKY_OIDC (sticky, from SSM /adt/oidc-issuer-url — app.py resolves the client params)."
  fi
fi
# --oidc-only on|off: the external provider becomes the SOLE identity provider on
# every edge client (COGNITO removed), so Cognito skips the hosted-UI chooser and
# /oauth2/authorize redirects straight to the provider — no local-password form
# is reachable on the surface. Requires the provider trio (flags now or sticky);
# app.py fails loud otherwise. Sticky via SSM /adt/oidc-only ("true"/"false").
if [[ -n "$OIDC_ONLY_MODE" ]]; then
  case "$OIDC_ONLY_MODE" in
    on)  OIDC_ONLY_VALUE="true" ;;
    off) OIDC_ONLY_VALUE="false" ;;
    *) echo "ERROR: --oidc-only takes 'on' or 'off' (got '$OIDC_ONLY_MODE')." >&2; exit 1 ;;
  esac
  _OIDC_CDK_FLAGS+=" -c oidc_only=$OIDC_ONLY_VALUE"
  aws ssm put-parameter --name "/adt/oidc-only" --value "$OIDC_ONLY_VALUE" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> External OIDC provider is the ONLY sign-in on every edge client: $OIDC_ONLY_MODE (persisted sticky in SSM /adt/oidc-only)."
fi

# ── Sticky query mode + cross-account data role ──────────────────────────────
# Mirrors the HighByte sticky-flag convention above: an explicit flag persists
# the value in SSM; a deploy WITHOUT the flag re-applies the persisted value.
# Without this, a plain ./deploy.sh silently reverted the ECS app to the default
# query mode (Mode B) and DROPPED the cross-account sts:AssumeRole grant.
# app.py performs the same fail-soft SSM read (stacks/_sticky_context.py) so a
# raw `cdk deploy` is covered too. When neither the flag nor the SSM parameter
# exists, behavior is identical to before (default mode, no grant).
if [[ "$QUERY_MODE_SET" -eq 1 ]]; then
  # Persist the sticky query mode so FUTURE deploys keep it without --query-mode.
  aws ssm put-parameter --name "/adt/system-query-mode" --value "$QUERY_MODE" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> Query mode '$QUERY_MODE' persisted (sticky) — future deploys reuse it without --query-mode."
else
  STICKY_QUERY_MODE=$(aws ssm get-parameter --name "/adt/system-query-mode" \
    --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
  if [[ -n "$STICKY_QUERY_MODE" && "$STICKY_QUERY_MODE" != "None" ]]; then
    QUERY_MODE="$STICKY_QUERY_MODE"
    export SYSTEM_QUERY_MODE="$QUERY_MODE"
    _MODE_CDK_FLAG=" -c system_query_mode=$QUERY_MODE"
    echo "==> Query mode: $QUERY_MODE (sticky, from SSM /adt/system-query-mode)."
  fi
fi
# Response-integrity posture (ADR-0047), sticky like the query mode above.
# Empty ⇒ the code default (report-only) — existing envs are untouched.
# `annotate` is the ADR's RECOMMENDED posture: the model answer stays the
# transport, the capture layer audits it (grounding sidecar + honest refusal
# when the turn captured ZERO trusted observations). `enforce` is the strict
# ADR-0046 gate, which regressed this demo's answer path.
if [[ "$INTEGRITY_MODE_SET" -eq 1 ]]; then
  aws ssm put-parameter --name "/adt/response-integrity-mode" --value "$INTEGRITY_MODE" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> Response-integrity mode '$INTEGRITY_MODE' persisted (sticky)."
else
  STICKY_INTEGRITY_MODE=$(aws ssm get-parameter --name "/adt/response-integrity-mode" \
    --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
  if [[ -n "$STICKY_INTEGRITY_MODE" && "$STICKY_INTEGRITY_MODE" != "None" ]]; then
    INTEGRITY_MODE="$STICKY_INTEGRITY_MODE"
  fi
fi
if [[ -n "$INTEGRITY_MODE" ]]; then
  export RESPONSE_INTEGRITY_MODE="$INTEGRITY_MODE"
  _INTEGRITY_CDK_FLAG=" -c response_integrity_mode=$INTEGRITY_MODE"
  echo "==> Response-integrity mode: $INTEGRITY_MODE (app + all runtimes)."
fi
# ── Saved orchestrator model (Admin LLM settings) ──
# The orchestrator model saved on the Admin LLM settings page (SSM
# /adt/llm-orchestrator-model, written by PUT /api/admin/llm-settings) survives
# a redeploy. Without it, a deploy with no BEDROCK_MODEL rebuilt the
# orchestrator and onboarding runtimes on the built-in default while the
# Settings page kept showing the saved model. An explicit BEDROCK_MODEL wins,
# and a saved model the deployment's
# ALLOWED_MODELS does not offer the orchestrator is ignored (the default
# stands). The system-agent
# model needs nothing here: provisioning already reads its saved value.
if [[ -z "${BEDROCK_MODEL:-}" ]]; then
  SAVED_ORCH_MODEL=$(aws ssm get-parameter --name "/adt/llm-orchestrator-model" \
    --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
  if [[ -n "$SAVED_ORCH_MODEL" && "$SAVED_ORCH_MODEL" != "None" ]]; then
    if python3 ../stacks/_model_allowlist.py allows "${ALLOWED_MODELS:-}" "$SAVED_ORCH_MODEL" orchestrator; then
      export BEDROCK_MODEL="$SAVED_ORCH_MODEL"
      _MODEL_CDK_FLAG+=" -c bedrock_model=$BEDROCK_MODEL"
      echo "==> Orchestrator model: $BEDROCK_MODEL (saved on the Admin LLM settings page)."
    else
      echo "==> Saved orchestrator model $SAVED_ORCH_MODEL is outside ALLOWED_MODELS; using the default."
    fi
  fi
fi
# ── End saved orchestrator model ──

# ── Saved memory extraction model (Admin LLM settings) ──
# The AgentCore Memory extraction model saved on the Admin LLM settings page
# (SSM /adt/llm-memory-model, written by PUT /api/admin/llm-settings after the
# memory's strategies were switched) survives a redeploy: without it, the
# deploy would put the stack's model back on the strategies. An explicit
# MEMORY_EXTRACTION_MODEL wins, and a saved model that is not a memory
# extraction model ALLOWED_MODELS offers is ignored (the stack's default
# stands). The stack resolves the id for the deploy Region. The read is
# get-parameters, which SUCCEEDS for a parameter that does not exist (it lists
# the name under InvalidParameters, and Parameters[0].Value is None), so a
# missing parameter keeps the default while a read that failed (AccessDenied,
# expired credentials, throttling) stops the deploy here, before CDK: deploying
# the default then would put the old model back on a memory the Admin page
# had switched.
if [[ -z "${MEMORY_EXTRACTION_MODEL:-}" ]]; then
  if ! SAVED_MEMORY_MODEL=$(aws ssm get-parameters --names "/adt/llm-memory-model" \
      --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameters[0].Value" --output text); then
    echo "ERROR: the saved memory extraction model (SSM /adt/llm-memory-model) could not be read (see the AWS error above), so this deploy stops before CDK rather than put the default model on the memory. Fix the read access, or set MEMORY_EXTRACTION_MODEL=<model id> to deploy with that model." >&2
    exit 1
  fi
  if [[ -n "$SAVED_MEMORY_MODEL" && "$SAVED_MEMORY_MODEL" != "None" ]]; then
    if python3 ../stacks/_model_allowlist.py allows "${ALLOWED_MODELS:-}" "$SAVED_MEMORY_MODEL" memory; then
      _MODEL_CDK_FLAG+=" -c memory_extraction_model=$SAVED_MEMORY_MODEL"
      echo "==> Memory extraction model: $SAVED_MEMORY_MODEL (saved on the Admin LLM settings page)."
    else
      echo "==> Saved memory extraction model $SAVED_MEMORY_MODEL is not a memory model ALLOWED_MODELS offers; ignored, using the default."
    fi
  fi
fi
# ── End saved memory extraction model ──

if [[ -n "$CROSS_ACCOUNT_ROLE" ]]; then
  # Explicit (--cross-account-role or CROSS_ACCOUNT_DATA_ROLE_ARN env): persist
  # the sticky role so FUTURE deploys keep the sts:AssumeRole grant without it.
  aws ssm put-parameter --name "/adt/cross-account-data-role-arn" --value "$CROSS_ACCOUNT_ROLE" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  echo "==> Cross-account data role persisted (sticky) — future deploys keep the grant without --cross-account-role."
else
  STICKY_XACCT_ROLE=$(aws ssm get-parameter --name "/adt/cross-account-data-role-arn" \
    --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
  if [[ -n "$STICKY_XACCT_ROLE" && "$STICKY_XACCT_ROLE" != "None" ]]; then
    CROSS_ACCOUNT_ROLE="$STICKY_XACCT_ROLE"
    _XACCT_CDK_FLAG=" -c crossAccountDataRoleArn=$CROSS_ACCOUNT_ROLE"
    echo "==> Cross-account data role: $CROSS_ACCOUNT_ROLE (sticky, from SSM)."
  fi
fi

# ── Helpers ──────────────────────────────────────────────────────────────────

cfn_output() {
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" $_AWS_PROFILE_FLAG \
    --query "Stacks[0].Outputs[?OutputKey==\`$2\`].OutputValue" --output text 2>/dev/null
}

# The post-CDK steps ([2f] per-system runtime refresh, [3b] query-target
# reconcile, [4/4] CloudFront invalidation) read AWS with `2>/dev/null || echo ""`,
# so a credential that expires or disappears after CDK finishes turned every one
# of them into a quiet "nothing to do" and the deploy still exited 0, leaving the
# per-system agents on their OLD image (seen 2026-10-06). Check the session at
# the start of each of those steps and stop the deploy if it is gone.
require_aws_session() {
  local step="$1" sts_err remaining
  # Every step name used below has its own case. An unknown name is a bug in
  # this script, not something to guess at: stop and name it.
  case "$step" in
    2f)  remaining="[2f] per-system runtime refresh, [3b] query-target reconcile and [4/4] CloudFront invalidation" ;;
    3b)  remaining="[3b] query-target reconcile and [4/4] CloudFront invalidation" ;;
    4/4) remaining="[4/4] CloudFront invalidation" ;;
    *)
      echo "" >&2
      echo "ERROR: deploy.sh internal error: require_aws_session got an unknown step name '$step'" >&2
      echo "    (known: 2f, 3b, 4/4). Fix the call in deploy.sh." >&2
      exit 2 ;;
  esac
  if ! sts_err=$(aws sts get-caller-identity --region "$REGION" $_AWS_PROFILE_FLAG 2>&1 >/dev/null); then
    echo "" >&2
    echo "ERROR: AWS credentials stopped working before step [$step]:" >&2
    printf '%s\n' "$sts_err" | sed -e '/^[[:space:]]*$/d' -e 's/^/    /' | sed -n '1,3p' >&2
    echo "    The CloudFormation stacks are deployed, but $remaining" >&2
    echo "    did NOT run. Refresh the credentials and re-run deploy.sh with the same flags." >&2
    exit 1
  fi
}

# Post-CDK steps that could not do their job but let the later independent steps
# run still fail the deploy at the end (see the end of this file). One entry per
# line; record_postcdk_failure appends one.
POSTCDK_FAILED=""
record_postcdk_failure() {
  local nl=$'\n'
  POSTCDK_FAILED="${POSTCDK_FAILED:+$POSTCDK_FAILED$nl}$1"
}

upsert_secret() {
  local name="$1"
  local value="$2"
  if aws secretsmanager describe-secret --secret-id "$name" --region "$REGION" \
      $_AWS_PROFILE_FLAG >/dev/null 2>&1; then
    printf '%s' "$value" | aws secretsmanager put-secret-value --secret-id "$name" \
      --secret-string file:///dev/stdin --region "$REGION" $_AWS_PROFILE_FLAG >/dev/null
  else
    printf '%s' "$value" | aws secretsmanager create-secret --name "$name" \
      --secret-string file:///dev/stdin --region "$REGION" $_AWS_PROFILE_FLAG >/dev/null
  fi
  echo "==> Token stored in Secrets Manager ($name)."
}

# Store optional bearer tokens before CDK so ECS can resolve them on its first
# launch. Secret values travel over stdin and never enter CDK context, SSM, or
# CloudFormation. Existing secrets remain usable on later deploys when callers
# do not re-supply the token.
SITEWISE_I3X_TOKEN=""
if [ -n "${I3X_ECS_STACK:-}" ]; then
  SITEWISE_I3X_TOKEN=$(cfn_output "$I3X_ECS_STACK" "AuthToken" 2>/dev/null || echo "")
fi
if [ -n "${HIGHBYTE_I3X_TOKEN:-}" ]; then
  upsert_secret "adt/highbyte-i3x-token" "$HIGHBYTE_I3X_TOKEN"
fi
if [ -n "$SITEWISE_I3X_TOKEN" ] && [ "$SITEWISE_I3X_TOKEN" != "None" ]; then
  upsert_secret "adt/sitewise-i3x-token" "$SITEWISE_I3X_TOKEN"
fi

_TOKEN_CDK_FLAGS=""
HIGHBYTE_I3X_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "adt/highbyte-i3x-token" --region "$REGION" $_AWS_PROFILE_FLAG \
  --query ARN --output text 2>/dev/null || echo "")
if [ -n "$HIGHBYTE_I3X_SECRET_ARN" ] && [ "$HIGHBYTE_I3X_SECRET_ARN" != "None" ]; then
  _TOKEN_CDK_FLAGS+=" -c highbyte_i3x_token_secret_arn=$HIGHBYTE_I3X_SECRET_ARN"
fi
SITEWISE_I3X_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "adt/sitewise-i3x-token" --region "$REGION" $_AWS_PROFILE_FLAG \
  --query ARN --output text 2>/dev/null || echo "")
if [ -n "$SITEWISE_I3X_SECRET_ARN" ] && [ "$SITEWISE_I3X_SECRET_ARN" != "None" ]; then
  _TOKEN_CDK_FLAGS+=" -c sitewise_i3x_token_secret_arn=$SITEWISE_I3X_SECRET_ARN"
fi

# Ensure the quick-link SSM parameters exist before CDK deploys. The ADT app
# task definition resolves them at TASK START via ecs.Secret.from_ssm_parameter
# — every referenced parameter MUST exist or the task fails to launch. The app
# treats the "placeholder" sentinel as unconfigured (admin_routes quick-links).
#
# ── AWS Quick integration (optional, opt-in — NOT sticky) ────────────────
# With --quick-api the task definition also references /adt/quick-desktop-url,
# so its placeholder is seeded here too. The x-quick-auth token is a Secrets
# Manager secret CloudFormation generates (ADTComputeStack).
_QUICK_CDK_FLAGS=""
if [[ "$QUICK_API_ENABLE" -eq 1 ]]; then
  _QUICK_CDK_FLAGS=" -c quick_api_enabled=true"
  echo "==> AWS Quick: API Gateway enabled (NOT sticky — pass --quick-api on every deploy that should keep it)."
fi
# --quick-connector: the connector-only path (ADR-0093): the bridge-only gateway adt-quick-gw, the
# bridge env on the app task, the ALB ingress for the bridge runtime and the
# adt-quick-client-secret copy, and none of QuickApiGatewayStack. It combines
# with --quick-api (stacks/_quick_connector.py keeps its token).
# Kept out of _QUICK_CDK_FLAGS on purpose: that variable means "the task
# references the SSM Quick parameters", which only the API path does.
_QUICK_CONNECTOR_CDK_FLAG=""
if [[ "$QUICK_CONNECTOR_ENABLE" -eq 1 ]]; then
  _QUICK_CONNECTOR_CDK_FLAG=" -c quick_connector_enabled=true"
  echo "==> AWS Quick: MCP connector path enabled (NOT sticky — pass --quick-connector on every deploy that should keep it)."
fi


# ── Delegated user tokens on the Quick gateway (ADR-0079; optional — sticky) ──
# --quick-oidc-discovery https://<issuer>/.well-known/openid-configuration
# --quick-oidc-audience <QuickGatewayUrl>[,<url>...] switch the dedicated Quick
# gateway's authorizer (in place) to the external OIDC identity provider and
# attach the REQUEST interceptor that mints the ADR-0049 identity claim. TWO-STEP
# by construction: the audience IS the gateway URL, so the gateway must already
# exist (deploy --quick-api or --quick-connector first, register that URL at the provider, then pass
# these flags). Checked HERE, before anything is deployed or persisted. Sticky
# (SSM /adt/quick-oidc-{discovery-url,audience}) so a flag-less deploy never
# flips the gateway back to Cognito; off-switch = delete both parameters.
_QUICK_OIDC_CDK_FLAGS=""
_QO_FROM_FLAGS=0
if [[ -n "$QUICK_OIDC_DISCOVERY" || -n "$QUICK_OIDC_AUDIENCE" ]]; then
  _QO_DISCOVERY="$QUICK_OIDC_DISCOVERY"; _QO_AUDIENCE="$QUICK_OIDC_AUDIENCE"; _QO_FROM_FLAGS=1
else
  _QO_DISCOVERY=$(aws ssm get-parameter --name "/adt/quick-oidc-discovery-url" \
    --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
  _QO_AUDIENCE=$(aws ssm get-parameter --name "/adt/quick-oidc-audience" \
    --region "$REGION" $_AWS_PROFILE_FLAG --query "Parameter.Value" --output text 2>/dev/null || echo "")
  [[ "$_QO_DISCOVERY" == "None" ]] && _QO_DISCOVERY=""
  [[ "$_QO_AUDIENCE" == "None" ]] && _QO_AUDIENCE=""
fi
if [[ -n "$_QO_DISCOVERY" || -n "$_QO_AUDIENCE" ]]; then
  if [[ -z "$_QO_DISCOVERY" || -z "$_QO_AUDIENCE" ]]; then
    echo "ERROR: --quick-oidc-discovery and --quick-oidc-audience must be provided together (or both SSM parameters /adt/quick-oidc-* exist)." >&2
    exit 1
  fi
  if [[ -z "$_QUICK_CDK_FLAGS$_QUICK_CONNECTOR_CDK_FLAG" ]]; then
    echo "ERROR: delegated user tokens on the Quick gateway are configured but neither --quick-api nor --quick-connector is set. Pass one of them, or delete the SSM parameters /adt/quick-oidc-discovery-url and /adt/quick-oidc-audience to switch them off." >&2
    exit 1
  fi
  _QG_URL=$(cfn_output "ADTAgentCoreStack" "QuickGatewayUrl")
  if [[ -z "$_QG_URL" || "$_QG_URL" == "None" ]]; then
    echo "ERROR: the Quick gateway does not exist yet (no QuickGatewayUrl output). Deploy with --quick-api or --quick-connector first, register the gateway URL at the identity provider as the audience, then enable delegated user tokens." >&2
    exit 1
  fi
  if [[ "$_QO_AUDIENCE" != "$_QG_URL" ]]; then
    echo "ERROR: --quick-oidc-audience ($_QO_AUDIENCE) must be EXACTLY the Quick gateway URL ($_QG_URL), one value, no list. Every extra audience is another resource whose tokens this gateway would accept."
    exit 1
  fi
  if [[ "$_QO_FROM_FLAGS" -eq 1 ]]; then
    aws ssm put-parameter --name "/adt/quick-oidc-discovery-url" --value "$_QO_DISCOVERY" \
      --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
    aws ssm put-parameter --name "/adt/quick-oidc-audience" --value "$_QO_AUDIENCE" \
      --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  fi
  _QUICK_OIDC_CDK_FLAGS=" -c quick_oidc_discovery_url=$_QO_DISCOVERY -c quick_oidc_audience=$_QO_AUDIENCE"
  echo "==> Quick gateway accepts delegated user tokens from $_QO_DISCOVERY (audience $_QG_URL; sticky in SSM /adt/quick-oidc-*)."
fi
_QUICKLINK_PARAMS=("/adt/highbyte-url" "/adt/erp-dashboard-url" "/adt/sitewise-i3x-url")
if [[ -n "$_QUICK_CDK_FLAGS" ]]; then
  _QUICKLINK_PARAMS+=("/adt/quick-desktop-url")
fi
for PARAM in "${_QUICKLINK_PARAMS[@]}"; do
  EXISTING=$(aws ssm get-parameter --name "$PARAM" --region "$REGION" $_AWS_PROFILE_FLAG \
    --query "Parameter.Value" --output text 2>/dev/null || echo "")
  if [ -z "$EXISTING" ]; then
    aws ssm put-parameter --name "$PARAM" --value "placeholder" --type "String" \
      --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  fi
done
# i3X TLS verification (also task-start-resolved): default "true" (verify ON).
# Deployments whose i3X server presents a self-signed certificate flip it to
# "false" afterwards (update-links.sh with I3X_TLS_VERIFY=false). Seeded only
# when absent so a deploy never clobbers an operator's setting.
TLS_PARAM="/adt/i3x-tls-verify"
EXISTING=$(aws ssm get-parameter --name "$TLS_PARAM" --region "$REGION" $_AWS_PROFILE_FLAG \
  --query "Parameter.Value" --output text 2>/dev/null || echo "")
if [ -z "$EXISTING" ]; then
  aws ssm put-parameter --name "$TLS_PARAM" --value "true" --type "String" \
    --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
fi

# ── Quick gateway present but no Quick flag: stop before CDK ────────────────
# The Quick flags are NOT sticky, so a deploy without one would remove
# adt-quick-gw. The app provisions the query-bridge target on that gateway at
# startup (outside CloudFormation) and AgentCore will not delete a gateway that
# still has a target, so ADTAgentCoreStack's update would fail half way.
# A forgotten flag is also the likely cause, so refuse instead of removing the
# Quick integration as a side effect; removing it is a deliberate step.
if [[ -z "$_QUICK_CDK_FLAGS$_QUICK_CONNECTOR_CDK_FLAG" ]]; then
  _OLD_QUICK_GW=$(cfn_output "ADTAgentCoreStack" "QuickGatewayId" || echo "")
  if [[ -n "$_OLD_QUICK_GW" && "$_OLD_QUICK_GW" != "None" ]]; then
    # Fail closed: an unreadable list counts as "has targets".
    _OLD_QUICK_TARGETS=$(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$_OLD_QUICK_GW" \
      --region "$REGION" $_AWS_PROFILE_FLAG --query 'length(items)' --output text 2>/dev/null || echo "unknown")
    if [[ "$_OLD_QUICK_TARGETS" != "0" ]]; then
      {
        echo "ERROR: this account has the AWS Quick gateway adt-quick-gw ($_OLD_QUICK_GW) with targets"
        echo "    (${_OLD_QUICK_TARGETS}), but no Quick flag was given, so this deploy would try to delete it."
        echo "    To keep Quick, re-run with the flag it was deployed with (--quick-connector or --quick-api)."
        echo "    To remove Quick, delete the gateway's targets, wait until none are listed, then re-run:"
        echo "      aws bedrock-agentcore-control list-gateway-targets --gateway-identifier $_OLD_QUICK_GW --region $REGION $_AWS_PROFILE_FLAG"
        echo "      aws bedrock-agentcore-control delete-gateway-target --gateway-identifier $_OLD_QUICK_GW --target-id <id> --region $REGION $_AWS_PROFILE_FLAG"
      } >&2
      exit 1
    fi
    echo "==> AWS Quick: adt-quick-gw ($_OLD_QUICK_GW) has no targets and no Quick flag was given, so this deploy removes it."
  fi
fi

# ── Pre-flight: create AgentCore service-linked role if it doesn't exist ─────
aws iam create-service-linked-role \
  --aws-service-name bedrock-agentcore.amazonaws.com \
  $_AWS_PROFILE_FLAG > /dev/null 2>&1 || true  # Ignore error if already exists

# ── 1. CDK deploy ───────────────────────────────────────────────────────────
echo "==> [1/4] Deploying CDK stacks..."
# Delete any stacks in ROLLBACK_COMPLETE state so CDK can recreate them
for STACK in ADTNetworkStack ADTERPDatabaseStack ADTERPLambdaStack ADTERPDashboardStack ADTSeedDataStack ADTDatabaseStack ADTAgentCoreStack ADTComputeStack ADTOnboardingStack ADTHighByteOpcuaStack; do
  STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" $_AWS_PROFILE_FLAG \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
  if [ "$STATUS" = "ROLLBACK_COMPLETE" ]; then
    echo "    Deleting $STACK (ROLLBACK_COMPLETE)..."
    aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION" $_AWS_PROFILE_FLAG
    aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION" $_AWS_PROFILE_FLAG 2>/dev/null || true
    echo "    $STACK deleted."
  fi
done
# Ensure CDK is bootstrapped in this region
BOOTSTRAP_STATUS=$(aws cloudformation describe-stacks --stack-name CDKToolkit --region "$REGION" $_AWS_PROFILE_FLAG \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
if [ "$BOOTSTRAP_STATUS" = "DOES_NOT_EXIST" ]; then
  echo "    Bootstrapping CDK in $REGION..."
  cd .. && npx cdk bootstrap "aws://$ACCOUNT/$REGION" --force 2>&1 | tail -3
  cd agentic_digital_thread
fi
# ── 1a. Deploy SiteWise asset models BEFORE CDK (models must exist before HighByte starts) ──
# Models include the full measurement schema for every process type, with correct dataTypes
# (DOUBLE for pump sensor values, INTEGER for counts, BOOLEAN for state). With models fully
# defined, the HighByte pipeline can run with create:true and ONLY create assets — never
# mutate model schemas. This avoids the LimitExceededException / "Cannot create property"
# class of errors that plagued earlier deployments.
# Idempotent: safe to re-run; existing models are reused.
#
# Two deploy modes need these models pre-created host-side:
#   1. HighByte: its pipeline boots with create:true and creates assets under
#      models that must already exist, and the hub container can start before any
#      stack would create them.
#   2. The ERP simulator (--simulator): it
#      ships the erp-sitewise-degradation-feed Lambda (ADTERPDashboardStack, the
#      dashboard's "Start machine degradation feed" button), the supported
#      no-HighByte SiteWise writer. That Lambda resolves the CNC asset model by
#      name and fails loudly if it is absent (lambda-sitewise-feed/handler.py);
#      it can create the ASSET but has no CreateAssetModel grant, so it cannot
#      create the model itself. The one exception, a LIVE workshop story, ships
#      no feed Lambda and forces SFC on, so SFC covers it — but WITH_SIMULATOR is
#      still set there and this step runs anyway, which is harmless (idempotent,
#      SFC reuses the models).
# SFC (when enabled) creates the same 28 models itself at deploy time from its own
# stack (SfcStack's lambda-sfc-sitewise-tree custom resource runs
# DeploySitewiseModels.deploy_models), so an SFC-only deploy needs no host-side
# step. A deploy with none of these — a bare base or --seed deploy — has no
# SiteWise writer at all ("Without SFC nothing writes SiteWise", below), so the
# models would be created with no assets ever attached = pure clutter. Gate the
# step on a deploy that actually has a host-side SiteWise-model consumer.
if [[ "$_HB_IN_DEPLOY" -eq 1 || "$WITH_SIMULATOR" == "true" ]]; then
  echo "    Deploying SiteWise asset models (idempotent)..."
  # Reviewed (unquoted-variable-expansion): ${VAR:+...} expands to zero words or
  # a quoted --profile pair — intentional; quoting the whole expansion would
  # pass an empty argument when no profile is set.
  # nosemgrep
  if ! python3 ../highbyte/DeploySitewiseModels.py ${AWS_PROFILE:+--profile "$AWS_PROFILE"} --region "$REGION" 2>&1 | tail -10; then
    echo "==> ERROR: SiteWise model deployment failed. The HighByte pipeline and the ERP SiteWise degradation feed will not work without these models."
    exit 1
  fi
else
  echo "    Skipping host-side SiteWise asset model deploy: no host-side SiteWise-model consumer in this deploy (no HighByte, no simulator)."
  echo "      (SFC, when enabled, creates these models from its own stack; a bare base/--seed deploy needs none.)"
fi

# Note: SiteWise asset creation is handled by the HighByte pipeline itself (create:true).
# The transform in pipeline_sitewise.json uses hierarchy-name keys + arrays which let
# HighByte associate assets correctly without trying to mutate model schemas.

# Optional: isolate the CDK synth/output directory so a deploy can run concurrently
# with another CDK invocation from this same repo without the "Other CLIs are
# currently reading from cdk.out" lock. Set CDK_OUTPUT_DIR=<dir> to override the
# default (cdk.out). No-op when unset, so existing behavior is unchanged.
_CDK_OUT_FLAG=""
[[ -n "${CDK_OUTPUT_DIR:-}" ]] && _CDK_OUT_FLAG="--output $CDK_OUTPUT_DIR"
# >>> cdk deploy command (tests/test_deploy_db_cidr_phases.py and tests/test_eni_drain_rollout.py run this block)
# Every cdk deploy below (the ENI drain update before a mock's teardown, and
# both database CIDR phases) runs this one command line, so all of them
# synthesize the same app.
_cdk_deploy() {
  # Strict sticky reads (stacks/_sticky_context.py): if the CDK app cannot read
  # a persisted setting from SSM it stops, instead of treating it as unset and
  # dropping it from the stacks (2026-11-16: a blank OIDC read nearly removed
  # the external identity provider).
  (cd .. && export ADT_STICKY_STRICT=1 && npx cdk deploy "$@" --require-approval never --force $_CDK_OUT_FLAG -c region="$REGION" -c app_version="$APP_VERSION" $_HB_CDK_FLAGS $_TOKEN_CDK_FLAGS $_DATA_CDK_FLAGS$_DOMAIN_CDK_FLAGS$_OIDC_CDK_FLAGS$_MODE_CDK_FLAG$_INTEGRITY_CDK_FLAG$_MODEL_CDK_FLAG$_AUTO_ACCEPT_CDK_FLAG$_XACCT_CDK_FLAG$_OBS_CDK_FLAG$_EDGE_AUTH_CDK_FLAG$_QUICK_CDK_FLAGS$_QUICK_CONNECTOR_CDK_FLAG$_QUICK_OIDC_CDK_FLAGS 2>&1 | tee -a "$DEPLOY_TMP/cdk_deploy.log"; exit "${PIPESTATUS[0]}")
}
# <<< cdk deploy command
# >>> eni drain update (tests/test_eni_drain_rollout.py runs this block)
# A stack delete runs the custom resources the stack was last deployed with,
# not this checkout's. A mock deployed before this checkout's ENI drain
# (stacks/_eni_drain.py) would be deleted by its old drain, or by none, and its
# security groups can then end DELETE_FAILED on the interfaces Lambda leaves
# behind. So before a mock's teardown deletes it:
# - its status must be settled first, whatever its template says. A stack whose
#   operation is still running, or has failed, stops the deploy with nothing
#   read or deleted. The exceptions are a stack whose resources are gone or
#   already being deleted (ROLLBACK_COMPLETE, REVIEW_IN_PROGRESS,
#   DELETE_IN_PROGRESS, DELETE_FAILED): it can't take an update and has nothing
#   for one to install, so its teardown goes on as before, unread;
# - then its deployed template is read and classified
#   (stacks/_eni_drain_revision.py). current (every drain and detach carries
#   this checkout's EniDrainRevision or later and each VPC Lambda has its own,
#   or it has no VPC Lambda at all): deleted as before. old: first deployed
#   alone (--exclusively) with its option kept on, which installs this
#   checkout's drain, its delete grant and its role ordering; read again; only
#   then deleted;
# - right before the delete, after the template reads and any update, its
#   status is read again and must still pass the same check: another operation
#   can start on the stack after the first read. DELETE_IN_PROGRESS then only
#   waits (no second delete), and a stack that no longer exists is done.
# Each of these also stops the deploy with nothing deleted: a template that
# can't be read, a failed update, a stack still old after it, a status that
# can't be read. A deploy that keeps the option on, or has no mock to turn
# off, never gets here.
_ENI_DRAIN_REVISION_PY="$REPO_ROOT/stacks/_eni_drain_revision.py"
# _eni_drain_status_gate STACK STATUS: what STACK's teardown may do from
# STATUS, on stdout. read: one of the five settled states, so its template is
# read before anything is deleted. delete: ROLLBACK_COMPLETE,
# REVIEW_IN_PROGRESS or DELETE_FAILED, which can't take an update and have
# nothing for one to install: deleted unread. wait: DELETE_IN_PROGRESS, a
# delete already running: waited for, never issued again. gone: the stack no
# longer exists. Any other status: the reason on stderr, and status 1.
_eni_drain_status_gate() {
  case "$2" in
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE|IMPORT_COMPLETE|IMPORT_ROLLBACK_COMPLETE) echo read ;;
    ROLLBACK_COMPLETE|REVIEW_IN_PROGRESS|DELETE_FAILED) echo delete ;;
    DELETE_IN_PROGRESS) echo wait ;;
    DOES_NOT_EXIST|DELETE_COMPLETE) echo gone ;;
    *)
      echo "ERROR: $1 is $2, which is not settled: an operation on it is still running, or" >&2
      echo "       has failed. Nothing has been deleted. Settle it first (wait for its operation to finish," >&2
      echo "       or continue its update rollback), then re-run." >&2
      return 1 ;;
  esac
}
# current | old on stdout; otherwise the reason, and status 1.
_eni_drain_state() {
  local out
  if ! out=$(aws cloudformation get-template --stack-name "$1" --region "$REGION" $_AWS_PROFILE_FLAG \
      --template-stage Original --query TemplateBody --output json 2>&1); then
    echo "get-template failed: ${out##*$'\n'}"; return 1
  fi
  printf '%s\n' "$out" | python3 "$_ENI_DRAIN_REVISION_PY"
}
# _eni_drain_update STACK STATUS CDK_ARGS...: before STACK is deleted, make sure
# its delete runs this checkout's drain. CDK_ARGS keep its option on.
_eni_drain_update() {
  local stack="$1" status="$2" state gate
  shift 2
  gate=$(_eni_drain_status_gate "$stack" "$status") || return 1
  [[ "$gate" == "read" ]] || return 0
  if ! state=$(_eni_drain_state "$stack"); then
    echo "ERROR: could not read which ENI drain $stack was deployed with: ${state:-no answer}." >&2
    echo "       Nothing has been deleted. Its delete runs that drain, so this deploy stops: fix the" >&2
    echo "       read (credentials, or python3 on PATH), then re-run." >&2
    return 1
  fi
  if [[ "$state" == "current" ]]; then return 0; fi
  echo "    $stack was deployed before this checkout's ENI drain: updating it alone, with its option kept on, so its delete runs this checkout's drain..."
  if ! _cdk_deploy "$stack" --exclusively "$@"; then
    echo "ERROR: the update of $stack that installs this checkout's ENI drain failed (full CDK output:" >&2
    echo "       $DEPLOY_TMP/cdk_deploy.log). Nothing has been deleted. Fix the error, then re-run. If the" >&2
    echo "       update needs newer stacks it imports from, deploy once with the option still on," >&2
    echo "       which updates them too, then turn it off." >&2
    return 1
  fi
  if ! state=$(_eni_drain_state "$stack") || [[ "$state" != "current" ]]; then
    echo "ERROR: $stack still does not carry this checkout's ENI drain after its update (${state:-no answer})." >&2
    echo "       Nothing has been deleted. Re-run deploy.sh." >&2
    return 1
  fi
  echo "    $stack now carries this checkout's ENI drain."
}
# <<< eni drain update
# >>> database CIDR phases (tests/test_deploy_db_cidr_phases.py runs this block)
# Deploys by the states read in the "database CIDR state" block above
# (stacks/_db_cidr_ingress.py). Phase 1 deploys every stack, each database with
# its own value; when a database was kept, phase 2 deploys only the kept
# databases with remove, which changes nothing but that rule. The value is
# always passed: cdk deploy would otherwise keep a stack's previous value.
# Both phases use the one command line of the "cdk deploy command" block.
if [[ "$_DB_CIDR_TWO_PHASES" -eq 1 ]]; then
  echo "==> Phase 1 of 2: every stack. Keeping the VPC-wide PostgreSQL rule they already have while each client rule is created: ${_DB_CIDR_KEPT[*]}. Any other database is deployed without it."
fi
CDK_EXIT=0
_cdk_deploy --all ${_DB_CIDR_PHASE1_ARGS[@]+"${_DB_CIDR_PHASE1_ARGS[@]}"} || CDK_EXIT=$?

if [ "$CDK_EXIT" -ne 0 ]; then
  echo "==> ERROR: CDK deploy failed (exit code $CDK_EXIT). Check output above."
  echo "    Full CDK output: $DEPLOY_TMP/cdk_deploy.log (kept after this run)."
  echo "    Stacks that succeeded will remain. Fix the error and re-run."
  if [[ "$_DB_CIDR_TWO_PHASES" -eq 1 ]]; then
    echo "    This was phase 1. Deployed with keep, each already admitting the whole VPC: ${_DB_CIDR_KEPT[*]}. Every other database was deployed with remove, so no client lost its path and no database gained the rule. Re-running deploy.sh reads the states again."
  fi
  exit 1
fi
if [[ "$_DB_CIDR_TWO_PHASES" -eq 1 ]]; then
  echo "==> Phase 2 of 2: dropping the VPC-wide PostgreSQL rule (every client rule exists now): ${_DB_CIDR_KEPT[*]}."
  _db_phase2_args=()
  for _db_stack in "${_DB_CIDR_KEPT[@]}"; do
    _db_phase2_args+=(--parameters "$_db_stack:DatabaseCidrIngress=remove")
  done
  if ! _cdk_deploy "${_DB_CIDR_KEPT[@]}" --exclusively "${_db_phase2_args[@]}"; then
    echo "==> ERROR: phase 2 failed. Check output above (full CDK output: $DEPLOY_TMP/cdk_deploy.log)."
    echo "    A database whose update failed rolls back, so these may still admit the whole VPC until phase 2 succeeds: ${_DB_CIDR_KEPT[*]}. Every client keeps its own rule."
    echo "    Re-run deploy.sh with the same flags to retry phase 2."
    exit 1
  fi
fi
# A database stack that does not report remove may still admit the whole
# VPC: the deploy fails at its end (after the independent post-CDK steps).
for _db_stack in ${_DB_CIDR_STACKS[@]+"${_DB_CIDR_STACKS[@]}"}; do
  if ! _db_state=$(_db_cidr_state "$_db_stack") || [[ "$_db_state" != "remove" ]]; then
    record_postcdk_failure "$_db_stack: DatabaseCidrIngress is not confirmed 'remove' ($_db_state): phase 2 has not run or cannot be confirmed, so it may still admit PostgreSQL from the whole VPC. Re-run deploy.sh with the same flags."
  fi
done
# <<< database CIDR phases

# Write quick-link URLs to SSM after stacks are deployed
HIGHBYTE_URL=$(aws cloudformation describe-stacks --stack-name ADTHighByteOpcuaStack --region "$REGION" \
  $_AWS_PROFILE_FLAG --query "Stacks[0].Outputs[?OutputKey=='HighByteEndpoint'].OutputValue" --output text 2>/dev/null || echo "")
ERP_DASHBOARD_URL=$(aws cloudformation describe-stacks --stack-name ADTERPDashboardStack --region "$REGION" \
  $_AWS_PROFILE_FLAG --query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue" --output text 2>/dev/null || echo "")
if [ -n "$HIGHBYTE_URL" ] && [ "$HIGHBYTE_URL" != "None" ]; then
  aws ssm put-parameter --name "/adt/highbyte-url" --value "$HIGHBYTE_URL" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
fi
if [ -n "$ERP_DASHBOARD_URL" ] && [ "$ERP_DASHBOARD_URL" != "None" ]; then
  aws ssm put-parameter --name "/adt/erp-dashboard-url" --value "$ERP_DASHBOARD_URL" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
else
  # ADTERPDashboardStack absent (base/seed deploy): overwrite with the sentinel so
  # a URL persisted by an earlier simulator deploy never renders a dead ERP
  # Dashboard quick-link. The app normalizes "placeholder" to empty/unconfigured
  # (agents/admin_routes.py quick_links).
  aws ssm put-parameter --name "/adt/erp-dashboard-url" --value "placeholder" \
    --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
fi
# Optional external integration: if you operate a separate i3X/SiteWise explorer deployment,
# set I3X_CLOUDFRONT_STACK and I3X_ECS_STACK (env vars) to your own CloudFormation stack
# names to surface its quick-links. Skipped entirely when those vars are unset.
if [ -n "${I3X_CLOUDFRONT_STACK:-}" ] && [ -n "${I3X_ECS_STACK:-}" ]; then
  SITEWISE_I3X_URL=$(aws cloudformation describe-stacks --stack-name "$I3X_CLOUDFRONT_STACK" --region "$REGION" \
    $_AWS_PROFILE_FLAG --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text 2>/dev/null || echo "")
  SITEWISE_I3X_TOKEN=$(aws cloudformation describe-stacks --stack-name "$I3X_ECS_STACK" --region "$REGION" \
    $_AWS_PROFILE_FLAG --query "Stacks[0].Outputs[?OutputKey=='AuthToken'].OutputValue" --output text 2>/dev/null || echo "")
  if [ -n "$SITEWISE_I3X_URL" ] && [ "$SITEWISE_I3X_URL" != "None" ]; then
    aws ssm put-parameter --name "/adt/sitewise-i3x-url" --value "$SITEWISE_I3X_URL" \
      --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
  fi
fi

# ── Demo DB SSM pointers (ADR-0044 Decision 2) ──────────────────────────────
# Publish /adt/demo/{seed-db,erp-db} connection POINTERS as JSON
# {host,port,dbname,secret_arn} from the demo stacks' CloudFormation outputs so
# the demo bootstrap (agents/demo/db_routing.py) can resolve them env→SSM.
# Only the Secrets Manager secret ARN (a pointer) is written — NEVER a secret
# value. Non-sticky mirror of the data flags (ADR-0030): a deploy WITHOUT
# --seed/--simulator DELETES the corresponding pointer(s), so a base deploy
# leaves no stale demo pointer behind (belt-and-suspenders against the
# non-sticky-flags footgun).
if [[ "$WITH_SEED" == "true" ]]; then
  DEMO_SEED_DB_HOST=$(cfn_output "ADTSeedDataStack" "ADTRdsEndpoint" 2>/dev/null || echo "")
  DEMO_SEED_DB_SECRET_ARN=$(cfn_output "ADTSeedDataStack" "ADTRdsSecretArn" 2>/dev/null || echo "")
  if [ -n "$DEMO_SEED_DB_HOST" ] && [ "$DEMO_SEED_DB_HOST" != "None" ] && \
     [ -n "$DEMO_SEED_DB_SECRET_ARN" ] && [ "$DEMO_SEED_DB_SECRET_ARN" != "None" ]; then
    # dbname "adt_demo" below is the fictitious demo company's database — demo content, not engine identity (see rename plan)
    aws ssm put-parameter --name "/adt/demo/seed-db" \
      --value "{\"host\":\"$DEMO_SEED_DB_HOST\",\"port\":\"5432\",\"dbname\":\"adt_demo\",\"secret_arn\":\"$DEMO_SEED_DB_SECRET_ARN\"}" \
      --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
    echo "==> Demo seed-db pointer published to SSM (/adt/demo/seed-db)."
  else
    echo "==> WARNING: --seed is active but ADTSeedDataStack outputs (ADTRdsEndpoint/ADTRdsSecretArn) did not resolve; seed-db pointer NOT published."
  fi
else
  aws ssm delete-parameter --name "/adt/demo/seed-db" \
    --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null 2>&1 || true
fi
if [[ "$WITH_SIMULATOR" == "true" ]]; then
  DEMO_ERP_DB_HOST=$(cfn_output "ADTERPDatabaseStack" "ClusterEndpoint" 2>/dev/null || echo "")
  DEMO_ERP_DB_PORT=$(cfn_output "ADTERPDatabaseStack" "ClusterPort" 2>/dev/null || echo "")
  DEMO_ERP_DB_SECRET_ARN=$(cfn_output "ADTERPDatabaseStack" "SecretArn" 2>/dev/null || echo "")
  if [ -z "$DEMO_ERP_DB_PORT" ] || [ "$DEMO_ERP_DB_PORT" == "None" ]; then
    DEMO_ERP_DB_PORT="5432"
  fi
  if [ -n "$DEMO_ERP_DB_HOST" ] && [ "$DEMO_ERP_DB_HOST" != "None" ] && \
     [ -n "$DEMO_ERP_DB_SECRET_ARN" ] && [ "$DEMO_ERP_DB_SECRET_ARN" != "None" ]; then
    aws ssm put-parameter --name "/adt/demo/erp-db" \
      --value "{\"host\":\"$DEMO_ERP_DB_HOST\",\"port\":\"$DEMO_ERP_DB_PORT\",\"dbname\":\"erp_simulation\",\"secret_arn\":\"$DEMO_ERP_DB_SECRET_ARN\"}" \
      --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
    echo "==> Demo erp-db pointer published to SSM (/adt/demo/erp-db)."
  else
    echo "==> WARNING: --simulator is active but ADTERPDatabaseStack outputs (ClusterEndpoint/SecretArn) did not resolve; erp-db pointer NOT published."
  fi
else
  aws ssm delete-parameter --name "/adt/demo/erp-db" \
    --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null 2>&1 || true
fi

# ── 2. Seed Neptune Analytics graph with core concepts ──────────────────────
# Neptune core concepts (the universal manufacturing vocabulary) are seeded
# in-VPC by the application on startup — see the lifespan handler in
# agentic_digital_thread/agents/server.py (_restore). The Neptune graph is private
# (no public connectivity), so it is NOT reachable from the deploy host;
# seeding from inside the VPC is both correct and idempotent. Industry-specific
# concept packs are still installed on demand from the Admin UI
# (/api/admin/concept-packs).
echo "==> [2/4] Neptune core concepts: seeded in-VPC by the app on startup (no host-side seed)"

# ── 2b. OAuth2 credential provider ──────────────────────────────────────────
# The "adt-m2m" OAuth2 credential provider (gateway → runtime M2M
# auth) is now created and managed by CDK as part of ADTAgentCoreStack
# (CfnOAuth2CredentialProvider), wired to the Cognito M2M client + secret. No
# imperative sync step is needed here.

# ── 2b-idc. Identity-claim signing secret (ADR-0049, Wave I2) ────────────────
# Shared HMAC key for the app-signed user-identity claim the orchestrator
# attaches to gateway tool calls on INTERACTIVE turns (reserved arg
# `_adt_identity`; system agents verify + pop it). Idempotent describe-first:
# create ONCE with a generated value, never rotate here (rotation = manual
# put-secret-value + runtime restarts). Readable by the AgentCore runtime role's
# existing `adt*` GetSecretValue grant (stacks/agentcore_stack.py).
# Non-fatal: on failure we WARN and continue — the claim plumbing degrades to
# attach-nothing/allow-all, never blocks a deploy. The key value is generated
# in-process, piped over stdin, and never echoed.
# NOTE: SYSTEM_REQUIRED_GROUP (the per-system enforcement hook) is a PER-SYSTEM
# operator choice — deliberately set NOWHERE by this script.
echo ""
echo "==> [2b-idc] Ensuring identity-claim signing secret (ADR-0049)..."
IDENTITY_CLAIM_SECRET_NAME="adt/identity-claim-key"
IDENTITY_CLAIM_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id "$IDENTITY_CLAIM_SECRET_NAME" --region "$REGION" $_AWS_PROFILE_FLAG \
  --query ARN --output text 2>/dev/null || echo "")
if [ -n "$IDENTITY_CLAIM_SECRET_ARN" ] && [ "$IDENTITY_CLAIM_SECRET_ARN" != "None" ]; then
  echo "    Identity-claim secret exists: $IDENTITY_CLAIM_SECRET_ARN"
else
  IDENTITY_CLAIM_SECRET_ARN=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))" | \
    aws secretsmanager create-secret --name "$IDENTITY_CLAIM_SECRET_NAME" \
      --description "ADT app-signed user-identity claim HMAC key (ADR-0049)" \
      --secret-string file:///dev/stdin --region "$REGION" $_AWS_PROFILE_FLAG \
      --query ARN --output text 2>/dev/null || echo "")
  if [ -n "$IDENTITY_CLAIM_SECRET_ARN" ] && [ "$IDENTITY_CLAIM_SECRET_ARN" != "None" ]; then
    echo "    ✅ Identity-claim secret created: $IDENTITY_CLAIM_SECRET_ARN"
  else
    IDENTITY_CLAIM_SECRET_ARN=""
    echo "    ⚠️  Could not create $IDENTITY_CLAIM_SECRET_NAME — continuing without it."
    echo "        Interactive turns will attach no identity claim; systems with"
    echo "        SYSTEM_REQUIRED_GROUP set would deny. Re-run deploy to retry."
  fi
fi
[ "$IDENTITY_CLAIM_SECRET_ARN" = "None" ] && IDENTITY_CLAIM_SECRET_ARN=""

# ── 2c. Orchestrator AgentCore Runtime (VPC mode, AZ-resilient) ─────────────
# The adt_orchestrator runtime is created imperatively here, NOT by CDK. AgentCore
# VPC runtimes can only launch in service-supported AZs, and the supported list is
# expressed as physical AZ *IDs* (e.g. use1-az1/az2/az4) while CDK subnets carry AZ
# *names* (us-east-1a/b/…) — and the name→ID mapping is account-specific, so it
# cannot be resolved at CDK synth (which is why a declarative CfnRuntime pinning all
# private subnets fails to deploy). We create it in VPC mode with all private subnets
# and, on a CREATE_FAILED "unsupported availability zone" failure, strip the offending
# subnet(s) and retry — mirroring agents/agent_manager.py and the proven per-system
# agent path (see ADR 0010). VPC mode is required because the Neptune Analytics graph
# is private (public_connectivity=False, ADR 0003) and reachable only from inside the
# VPC. The app discovers this runtime by name at runtime
# (agents/config.py:_discover_orchestrator_arn), so no SSM/env wiring from CDK is needed.
echo ""
echo "==> [2c] Creating orchestrator AgentCore Runtime (VPC mode)..."
RUNTIME_ROLE=$(cfn_output "ADTAgentCoreStack" "RuntimeRoleArn" 2>/dev/null || echo "")
GW_ID=$(cfn_output "ADTAgentCoreStack" "GatewayId" 2>/dev/null || echo "")
GW_URL=$(cfn_output "ADTAgentCoreStack" "GatewayUrl" 2>/dev/null || echo "")
GRAPH_ID=$(cfn_output "ADTDatabaseStack" "NeptuneGraphId" 2>/dev/null || echo "")
ORCH_SG=$(cfn_output "ADTComputeStack" "AgentCoreRuntimeSgId" 2>/dev/null || echo "")
ORCH_SUBNETS_CSV=$(cfn_output "ADTComputeStack" "AgentCoreRuntimeSubnetIds" 2>/dev/null || echo "")
ORCH_IMAGE=$(cfn_output "ADTComputeStack" "OrchestratorRuntimeImageUri" 2>/dev/null || echo "")
# adt-app-service client-credentials wiring for background/scheduled orchestrator
# turns (P5, ADR-0033). The query gateway is CUSTOM_JWT(adt-app); a non-interactive
# turn has no forwarded end-user token, so the orchestrator mints a adt-app-service
# client_credentials token via agents/orchestrator._fetch_service_token. That mint
# needs the SERVICE client id (AppServiceClientId) AND the adt-app pool id it lives
# in (AppUserPoolId) — the runtime then fetches the client secret via
# describe_user_pool_client and derives the Cognito token endpoint from region +
# account (STS) at runtime, so no secret/endpoint env is required. The ECS app gets
# these from the same outputs (stacks/adt_compute_stack.py); the RUNTIME must too, or
# background gateway auth fails closed. Mirrors the §2e output-fetch pattern below.
APP_SERVICE_CLIENT_ID=$(cfn_output "ADTAgentCoreStack" "AppServiceClientId" 2>/dev/null || echo "")
APP_USER_POOL_ID=$(cfn_output "ADTAgentCoreStack" "AppUserPoolId" 2>/dev/null || echo "")

# Orchestrator runtime — ensured by the ONE provisioning driver
# (scripts/provision_runtimes.py): pagination-safe list, create-or-update
# (update kwargs via runtime_provision_helpers.runtime_update_kwargs,
# ADR-0056), AZ-strip retry, READY waits. The runtime env is built INSIDE the
# driver by the ONE contract module (agents/runtime_env_contract.build_env) —
# the exports below are the same contract inputs the Phase 0 shim consumed
# (the BEDROCK_MODEL region-keyed default and the ONBOARDING_SESSIONS_TABLE
# fallback live in the contract, not here; bounded-parallel delegation cap
# ADR-0039: ORCHESTRATOR_DELEGATION_CONCURRENCY, default 3). Driver contract:
# EXACTLY one JSON object on stdout ({"orchestrator": {"arn", "runtime_id",
# "action"}}), diagnostics on stderr, nonzero exit on ANY failure. Fail-LOUD:
# a driver failure ABORTS the deploy — update failures and non-AZ create
# failures are FATAL (deliberate tightening vs the old warn-and-continue
# bash; same class as the Phase 0 shim abort).
if [ -z "$ORCH_IMAGE" ] || [ "$ORCH_IMAGE" = "None" ] || [ -z "$RUNTIME_ROLE" ] || [ "$RUNTIME_ROLE" = "None" ]; then
  echo "    ⚠️  Skipping orchestrator creation — required stack outputs missing (image/role)."
else
  ORCH_JSON=$(GRAPH_IDENTIFIER="$GRAPH_ID" AWS_REGION="$REGION" \
    BEDROCK_MODEL="${BEDROCK_MODEL:-}" \
    AGENTCORE_GATEWAY_URL="$GW_URL" AGENTCORE_GATEWAY_ID="$GW_ID" \
    ADT_APP_SERVICE_CLIENT_ID="$APP_SERVICE_CLIENT_ID" \
    ADT_APP_USER_POOL_ID="$APP_USER_POOL_ID" \
    SYSTEM_QUERY_MODE="${SYSTEM_QUERY_MODE:-}" \
    SELF_LEARNING_AUTO_ACCEPT="${SELF_LEARNING_AUTO_ACCEPT:-}" \
    ORCHESTRATOR_DELEGATION_CONCURRENCY="${ORCHESTRATOR_DELEGATION_CONCURRENCY:-}" \
    SYSTEM_IDENTITY_CLAIM_SECRET_ARN="$IDENTITY_CLAIM_SECRET_ARN" \
    ONBOARDING_SESSIONS_TABLE="${ONBOARDING_SESSIONS_TABLE:-}" \
    AGENT_SPAN_EMISSION="$SPAN_EMISSION" \
    PYTHONPATH=. python3 scripts/provision_runtimes.py --role orchestrator \
      --region "$REGION" --image "$ORCH_IMAGE" --role-arn "$RUNTIME_ROLE" \
      --subnets "$ORCH_SUBNETS_CSV" --sg "$ORCH_SG" $_AWS_PROFILE_FLAG) \
    || { echo "❌ [2c] provision_runtimes driver FAILED — aborting deploy." >&2; exit 1; }
  [[ -n "$ORCH_JSON" ]] || { echo "❌ [2c] provision_runtimes emitted no JSON — aborting deploy." >&2; exit 1; }
  # jq-free JSON consumption (python3 one-liner, same style as the rest of
  # this script). ORCH_ARN feeds §2c-eval and the §3 summary downstream.
  ORCH_ARN=$(printf '%s' "$ORCH_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['orchestrator']['arn'])") \
    || { echo "❌ [2c] could not parse the driver JSON — aborting deploy." >&2; exit 1; }
  ORCH_ACTION=$(printf '%s' "$ORCH_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['orchestrator']['action'])") \
    || { echo "❌ [2c] could not parse the driver JSON — aborting deploy." >&2; exit 1; }
  echo "    Orchestrator runtime: ✅ $ORCH_ACTION ($ORCH_ARN)"
fi

# ── 2c-eval. Persist the orchestrator runtime ARN (CDK-owned evaluations) ───
# ("2f" is taken by the per-system runtime refresh below — pinned by a unit
# test — so this orchestrator-scoped step keys off §2c instead.)
# The managed AgentCore evaluation config for the adt_orchestrator runtime is
# a CDK-OWNED CloudFormation resource (stacks/_evaluations.py —
# AWS::BedrockAgentCore::OnlineEvaluationConfig + its execution role), no
# longer an imperative ensure step. This step only persists the READY
# runtime's ARN to SSM /adt/orchestrator-runtime-arn; app.py
# resolves it at the NEXT synth (sticky: -c orchestrator_runtime_arn > SSM >
# "") and the CDK resource consumes it — the config AND the
# EVAL_CONFIG_ORCHESTRATOR env (taken directly from the resource's id
# attribute) land TOGETHER in that deploy's stack update.
# FIRST-DEPLOY CHICKEN-AND-EGG (narrowed): deploy #1's CDK synth ([1]) ran
# before this write, so evaluations begin with deploy #2. Evals COMPLEMENT
# scripts/smoke_prompts.py (never replace it): evals are continuous/
# statistical over production traces; smoke_prompts stays the deterministic
# grounding gate. Teardown is the ordinary stack delete. Non-fatal: a failed
# write only delays evaluations to a later deploy.
echo ""
echo "==> [2c-eval] Persisting orchestrator runtime ARN for CDK-owned evaluations..."
if [ -n "${ORCH_ARN:-}" ] && [ "$ORCH_ARN" != "None" ]; then
  if aws ssm put-parameter --name "/adt/orchestrator-runtime-arn" \
      --value "$ORCH_ARN" --type "String" --overwrite \
      --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null 2>&1; then
    echo "    Runtime ARN persisted: ✅ $ORCH_ARN"
    echo "    (SSM /adt/orchestrator-runtime-arn — the CDK-owned"
    echo "     OnlineEvaluationConfig + EVAL_CONFIG_ORCHESTRATOR env land together"
    echo "     at the NEXT deploy's stack update; Agents-tab scores follow once"
    echo "     sampled evaluations accumulate.)"
  else
    echo "    ⚠️  Could not persist the runtime ARN to SSM (non-fatal) — evaluations"
    echo "        stay off until /adt/orchestrator-runtime-arn is written."
  fi
else
  echo "    ⚠️  Skipping — orchestrator runtime ARN unavailable (see [2c] above)."
fi

# ── 2d. (retired) App restart after orchestrator creation ────────────────────
# Until 2026-09-10 this step force-redeployed the ECS service because the app
# discovered the orchestrator ARN once at startup and cached a miss for the life
# of the process. agents/config.py now re-attempts discovery every 15 s while the
# ARN is unresolved and pins it once found (ADR-0065 §2), so the running task
# heals itself within seconds of [2c] finishing. Kept as a numbered placeholder
# so §2e/§3 references stay stable.

# ── 2e. Onboarding AgentCore Runtime + gateway target (VPC mode, AZ-resilient) ──
# The adt_onboarding runtime is created imperatively here (NOT by CDK), mirroring
# the orchestrator (§2c). AgentCore VPC runtimes only launch in service-supported
# AZs, whose list is expressed as physical AZ IDs while CDK subnets carry AZ names
# (name→ID mapping is account-specific, unresolvable at synth), so a declarative
# CfnRuntime pinning all private subnets CREATE_FAILEDs in an unsupported AZ (see
# ADR 0010). We create it VPC mode with all private onboarding subnets and, on a
# CREATE_FAILED "availability zone" failure, strip the offending subnet(s) and
# retry — keeping the deploy IN-PLACE on the existing VPC (no AZ/VPC change).
# Then we create/sync the SINGLE onboarding gateway target pointing at the runtime
# MCP URL through the adt-m2m OAuth provider — exactly like the query
# gateway's system-agent targets are created by agents/agent_manager.py (never
# declaratively, because the target needs the runtime MCP URL, unknown at synth).
echo ""
echo "==> [2e] Creating onboarding AgentCore Runtime (VPC mode) + gateway target..."
ONB_ROLE=$(cfn_output "ADTOnboardingStack" "OnboardingRuntimeRoleArn" 2>/dev/null || echo "")
ONB_SG=$(cfn_output "ADTOnboardingStack" "OnboardingRuntimeSgId" 2>/dev/null || echo "")
ONB_SUBNETS_CSV=$(cfn_output "ADTOnboardingStack" "OnboardingRuntimeSubnetIds" 2>/dev/null || echo "")
ONB_TABLE=$(cfn_output "ADTOnboardingStack" "OnboardingSessionsTableName" 2>/dev/null || echo "")
ONB_GW_ID=$(cfn_output "ADTOnboardingStack" "OnboardingGatewayId" 2>/dev/null || echo "")
# Runtime image = the shared system-agent/orchestrator image asset (SYSTEM_ID
# selects the onboarding entrypoint in runtime_app). The M2M inbound authorizer +
# outbound credential provider come from ADTAgentCoreStack (mirrors agent_manager).
ONB_IMAGE=$(cfn_output "ADTComputeStack" "OrchestratorRuntimeImageUri" 2>/dev/null || echo "")
M2M_DISCOVERY=$(cfn_output "ADTAgentCoreStack" "M2MCognitoDiscoveryUrl" 2>/dev/null || echo "")
M2M_CLIENT_ID=$(cfn_output "ADTAgentCoreStack" "M2MClientId" 2>/dev/null || echo "")
M2M_PROVIDER_ARN=$(cfn_output "ADTAgentCoreStack" "M2MCredentialProviderArn" 2>/dev/null || echo "")

# Onboarding runtime + gateway target — ensured by the ONE provisioning
# driver (scripts/provision_runtimes.py), same fail-LOUD stdout-JSON contract
# as §2c. The driver applies the MCP protocol override + the M2M
# customJWTAuthorizer on BOTH create and update (ADR-0056 repair semantics —
# a plain round-trip would perpetuate a previously-wiped HTTP state), then
# creates/reuses the SINGLE onboarding gateway target through the adt-m2m
# OAuth provider (30 s warmup + bounded sync — the agent_manager semantics,
# via agents/provisioner/handlers). Env is built INSIDE the driver by the
# contract module; decided corner (golden-pinned): a failed CFN-output read
# never writes an empty ONBOARDING_SESSIONS_TABLE — the contract fills the
# CDK-fixed default.
if [ -z "$ONB_IMAGE" ] || [ "$ONB_IMAGE" = "None" ] || [ -z "$ONB_ROLE" ] || [ "$ONB_ROLE" = "None" ] || [ -z "$ONB_GW_ID" ] || [ "$ONB_GW_ID" = "None" ]; then
  echo "    ⚠️  Skipping onboarding runtime creation — required stack outputs missing (image/role/gateway). Is ADTOnboardingStack deployed?"
else
  ONB_JSON=$(AWS_REGION="$REGION" ONBOARDING_SESSIONS_TABLE="$ONB_TABLE" \
    GRAPH_IDENTIFIER="$GRAPH_ID" \
    AWS_ACCOUNT_ID="${ACCOUNT:-}" \
    AGENT_SPAN_EMISSION="$SPAN_EMISSION" \
    PYTHONPATH=. python3 scripts/provision_runtimes.py --role onboarding \
      --region "$REGION" --image "$ONB_IMAGE" --role-arn "$ONB_ROLE" \
      --subnets "$ONB_SUBNETS_CSV" --sg "$ONB_SG" \
      --gateway-id "$ONB_GW_ID" --m2m-discovery-url "$M2M_DISCOVERY" \
      --m2m-client-id "$M2M_CLIENT_ID" --provider-arn "$M2M_PROVIDER_ARN" $_AWS_PROFILE_FLAG) \
    || { echo "❌ [2e] provision_runtimes driver FAILED — aborting deploy." >&2; exit 1; }
  [[ -n "$ONB_JSON" ]] || { echo "❌ [2e] provision_runtimes emitted no JSON — aborting deploy." >&2; exit 1; }
  # jq-free JSON consumption; ONB_ARN feeds the §3 summary downstream.
  ONB_ARN=$(printf '%s' "$ONB_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['onboarding']['arn'])") \
    || { echo "❌ [2e] could not parse the driver JSON — aborting deploy." >&2; exit 1; }
  ONB_ACTION=$(printf '%s' "$ONB_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['onboarding']['action'])") \
    || { echo "❌ [2e] could not parse the driver JSON — aborting deploy." >&2; exit 1; }
  ONB_TGT_ID=$(printf '%s' "$ONB_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['target_id'])") \
    || { echo "❌ [2e] could not parse the driver JSON — aborting deploy." >&2; exit 1; }
  echo "    Onboarding runtime: ✅ $ONB_ACTION ($ONB_ARN)"
  echo "    Onboarding gateway target synced: ✅ $ONB_TGT_ID"
fi

# ── 2f. Refresh per-system agent runtimes to the freshly-built image ────────
# CDK builds/publishes ONE shared agent image (ADTComputeStack DockerImageAsset)
# and §2c/§2e update the orchestrator + onboarding runtimes to it — but the
# PER-SYSTEM agent runtimes (one per onboarded system, created imperatively by
# agent_manager / the provisioner, named ``adt_<system_id>`` with hyphens folded
# to underscores — see AgentManager._runtime_name) were left on their OLD image
# every deploy, so a code fix never reached them until the system was
# re-onboarded. This step updates EACH per-system runtime to the new image,
# EXACTLY mirroring the proven orchestrator/onboarding update pattern (the
# control flow §2c/§2e now run through scripts/provision_runtimes.py):
# re-read the current networkConfiguration, strip the immutable
# requireServiceS3Endpoint (same python3 one-liner), and re-submit with the
# runtime's OWN role, PRESERVED environment variables (CONNECTION_SECRET_ARN /
# SYSTEM_ID / etc.), and PRESERVED Cognito customJWTAuthorizer (dropping it
# breaks MCP auth). Refreshed set = every ``adt_`` runtime EXCEPT the two infra
# runtimes with their own update steps (adt_orchestrator §2c / adt_onboarding §2e);
# adt_digital_thread IS refreshed here — no other step updates its image, so
# excluding it left it on a stale image indefinitely. A hardcoded per-system name
# list would never keep up, so we filter on ``adt_`` minus that infra
# exclusion set (this matches every ``adt_<system>`` runtime name).
# A runtime counts as refreshed only once it reports READY after its update
# (see the wait after the loop). One that could not be refreshed (ARN, role or
# env unreadable, update rejected, UPDATE_FAILED, not READY in time) is
# surfaced and the others carry on; any such runtime, a failed list or a
# missing agent image then fails the deploy at the end, naming the systems.
# No per-system runtimes is a clean no-op.
echo ""
echo "==> [2f] Refreshing per-system agent runtimes to the current image..."
require_aws_session 2f
if [ -z "$ORCH_IMAGE" ] || [ "$ORCH_IMAGE" = "None" ]; then
  echo "    ⚠️  Per-system runtimes were NOT refreshed: agent image URI missing (see [2c])."
  echo "        The deploy will exit non-zero; re-run it once the error above is fixed."
  record_postcdk_failure "[2f] per-system runtime refresh (agent image URI missing)"
else
  # Same image var + artifact shape the orchestrator update uses (DRY).
  RT_ARTIFACT="{\"containerConfiguration\":{\"containerUri\":\"$ORCH_IMAGE\"}}"
  # Every adt_ runtime that is NOT one of the two infra runtimes with their own
  # update steps (§2c/§2e) is refreshed here — including adt_digital_thread,
  # which has NO other image-update step. Server-side filter so an empty list
  # is a no-op. A FAILED list is not an empty list: report it instead of
  # printing "nothing to refresh".
  RT_LIST_FAILED=0
  if ! RT_NAMES=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --cli-connect-timeout 10 --cli-read-timeout 30 \
    --query "agentRuntimes[?starts_with(agentRuntimeName, 'adt_') && agentRuntimeName != 'adt_orchestrator' && agentRuntimeName != 'adt_onboarding'].agentRuntimeName" \
    --output text 2>"$DEPLOY_TMP/rt_list_err"); then
    RT_NAMES=""; RT_LIST_FAILED=1
  fi
  if [ "$RT_LIST_FAILED" = "1" ]; then
    echo "    ⚠️  Could not list agent runtimes — per-system runtimes were NOT refreshed:"
    sed -n 's/^/        /;1,3p' "$DEPLOY_TMP/rt_list_err" 2>/dev/null || true
    echo "        The deploy will exit non-zero; re-run it once the error above is fixed."
    record_postcdk_failure "[2f] per-system runtime refresh (could not list runtimes)"
  elif [ -z "${RT_NAMES// /}" ]; then
    echo "    No per-system agent runtimes found — nothing to refresh (no-op)."
  else
    RT_OK=0; RT_FAIL=0; RT_FAILED_NAMES=""
    # One "name (reason)" entry per runtime that was not refreshed.
    rt_failed() {
      RT_FAIL=$((RT_FAIL + 1))
      RT_FAILED_NAMES="${RT_FAILED_NAMES:+$RT_FAILED_NAMES, }$1 ($2)"
    }
    # The wait below keeps a little bookkeeping per runtime under
    # $RT_STATE/state: its id, the version its update returned and its last
    # status (bash 3.2 has no associative arrays). No runtime settings are
    # written: a retry rebuilds them with rt_prepare. AWS error text stays in
    # $RT_STATE/<name>.err and .status_err.
    RT_STATE="$DEPLOY_TMP/rt_refresh"
    mkdir -p "$RT_STATE/state"
    rt_put() { printf '%s' "$3" > "$RT_STATE/state/$1.$2"; }
    rt_get() { cat "$RT_STATE/state/$1.$2" 2>/dev/null || true; }
    # rt_prepare NAME: builds NAME's update from the runtime's CURRENT state and
    # sets RT_ID, RT_ROLE, RT_ENV, RT_AUTH and RT_NETCFG. On failure it prints
    # why, sets RT_PREP_REASON and returns 1. It runs for the first update and
    # AGAIN before a retry, so a retry never re-sends what was read before
    # another writer's update (that would silently revert it).
    # Every control-plane call here is bounded (connect 10 s, read 30 s; the
    # update 60 s). The CLI retries a read timeout (3 attempts with
    # aws-cli 2.36, measured against a socket that never answers), so one call
    # can take about three times its read timeout; the refresh limit below
    # stops 2f starting another runtime once it has run out.
    rt_prepare() {
      RT_PREP_REASON=""
      RT_ARN=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
        --cli-connect-timeout 10 --cli-read-timeout 30 \
        --query "agentRuntimes[?agentRuntimeName=='$1'].agentRuntimeArn | [0]" \
        --output text 2>/dev/null || echo "None")
      RT_ARN=$(printf '%s\n' "$RT_ARN" | { grep -v '^None$' || true; } | head -1); RT_ARN="${RT_ARN:-None}"
      if [ -z "$RT_ARN" ] || [ "$RT_ARN" = "None" ]; then
        echo "      ⚠️  $1: could not resolve ARN — skipping."
        RT_PREP_REASON="could not resolve ARN"; return 1
      fi
      RT_ID="${RT_ARN##*/}"
      # Read the FULL current runtime once; preserve role + env + authorizer.
      RT_JSON=$(aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "$RT_ID" --region "$REGION" \
        --cli-connect-timeout 10 --cli-read-timeout 30 \
        --output json 2>/dev/null || echo "{}")
      RT_ROLE=$(printf '%s' "$RT_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin).get('roleArn') or '')" 2>/dev/null || echo "")
      if [ -z "$RT_ROLE" ] || [ "$RT_ROLE" = "None" ]; then
        echo "      ⚠️  $1: could not read roleArn — skipping."
        RT_PREP_REASON="could not read roleArn"; return 1
      fi
      # Preserve existing environment variables verbatim (CONNECTION_SECRET_ARN,
      # SYSTEM_ID, etc.); default to {} if somehow absent.
      RT_ENV=$(printf '%s' "$RT_JSON" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin).get('environmentVariables') or {}))" 2>/dev/null || echo "{}")
      # Required-key reconcile via the ONE contract module
      # (agents/runtime_env_contract.py --role system --reconcile):
      # ONBOARDING_SESSIONS_TABLE is fill-if-absent-or-empty (Waves 5/6 — an
      # existing value is authoritative; source = the ADTOnboardingStack CFN
      # output when read, else the CDK-fixed default), and
      # SYSTEM_IDENTITY_CLAIM_SECRET_ARN is OVERWRITTEN whenever the deploy
      # resolved an ARN (ADR-0049 rotation/propagation), AWS_ACCOUNT_ID
      # is set to this deploy's account (so the runtime reads its own
      # full-ARN secrets without an STS call), and the span keys follow
      # SPAN_EMISSION (AGENT_OBSERVABILITY_ENABLED and the OTEL_*_EXPORTER
      # keys, as a new runtime gets them). Every other env var
      # — SYSTEM_REQUIRED_GROUP, CONNECTION_SECRET_ARN, SYSTEM_ID, … — is
      # preserved verbatim by reconcile_env's fill-only contract. Fail LOUD
      # per runtime: on shim failure or empty output this runtime is SKIPPED
      # (env untouched), recorded by rt_failed, and the others carry on.
      if RT_ENV_NEW=$(printf '%s' "$RT_ENV" | \
          SYSTEM_IDENTITY_CLAIM_SECRET_ARN="$IDENTITY_CLAIM_SECRET_ARN" \
          ONBOARDING_SESSIONS_TABLE="${ONB_TABLE:-}" \
          AWS_ACCOUNT_ID="${ACCOUNT:-}" \
          AGENT_SPAN_EMISSION="$SPAN_EMISSION" \
          PYTHONPATH=. python3 -m agents.runtime_env_contract --role system --reconcile) \
          && [ -n "$RT_ENV_NEW" ]; then
        RT_ENV="$RT_ENV_NEW"
      else
        echo "      ⚠️  $1: runtime_env_contract reconcile FAILED — skipping (env left untouched)."
        RT_PREP_REASON="env reconcile failed"; return 1
      fi
      # Preserve the inbound Cognito M2M authorizer verbatim (empty ⇒ omit the flag).
      RT_AUTH=$(printf '%s' "$RT_JSON" | python3 -c "import sys,json;a=json.load(sys.stdin).get('authorizerConfiguration');print(json.dumps(a) if a else '')" 2>/dev/null || echo "")
      # Re-read networkConfiguration so the VPC config is not wiped on update.
      RT_NETCFG=$(printf '%s' "$RT_JSON" | python3 -c "import sys,json;n=json.load(sys.stdin).get('networkConfiguration');print(json.dumps(n) if n else '')" 2>/dev/null || echo "")
      if [ -z "$RT_NETCFG" ] || [ "$RT_NETCFG" = "null" ]; then
        RT_NETCFG="{\"networkMode\":\"PUBLIC\"}"
      else
        # Drop requireServiceS3Endpoint: IMMUTABLE for runtimes created after
        # 2026-06-11 — echoing it back makes update-agent-runtime fail. Same
        # strip the §2c/§2e driver applies via runtime_provision_helpers (DRY).
        RT_NETCFG=$(printf '%s' "$RT_NETCFG" | python3 -c "import sys,json; n=json.load(sys.stdin); (n.get('networkModeConfig') or {}).pop('requireServiceS3Endpoint', None); print(json.dumps(n))" 2>/dev/null || printf '%s' "$RT_NETCFG")
      fi
      # Every other optional setting the runtime carries (description, request
      # headers, lifecycle, filesystems, capacity provider, metadata): the
      # update is a full replacement, so one left out is reset. The field list
      # is runtime_provision_helpers.RUNTIME_UPDATE_CARRIED_FIELDS, the one the
      # Python update builder carries. Sent through --cli-input-json; "{}" when
      # the runtime has none.
      if ! RT_SETTINGS=$(printf '%s' "$RT_JSON" | PYTHONPATH=. python3 -c "import sys,json; from agents.runtime_provision_helpers import carried_runtime_settings as c; print(json.dumps(c(json.load(sys.stdin)), separators=(',', ':')))") \
          || [ -z "$RT_SETTINGS" ]; then
        echo "      ⚠️  $1: could not read its optional settings, skipping."
        RT_PREP_REASON="could not read its optional settings"; return 1
      fi
    }
    # rt_send_update NAME: one update-agent-runtime call built by rt_prepare,
    # carrying the runtime's optional settings (RT_SETTINGS) as well.
    # The authorizer flag is sent only when the runtime carries one (preserve,
    # never fabricate). Sets RT_SENT_VER to the version the update returned
    # (empty when it returned none). AWS error text lands in $RT_STATE/NAME.err.
    rt_send_update() {
      local out
      RT_SENT_VER=""
      if [ -n "$RT_AUTH" ]; then
        out=$(aws bedrock-agentcore-control update-agent-runtime \
          --agent-runtime-id "$RT_ID" --region "$REGION" \
          --cli-connect-timeout 10 --cli-read-timeout 60 \
          --agent-runtime-artifact "$RT_ARTIFACT" \
          --protocol-configuration '{"serverProtocol":"MCP"}' \
          --role-arn "$RT_ROLE" \
          --network-configuration "$RT_NETCFG" \
          --environment-variables "$RT_ENV" \
          --authorizer-configuration "$RT_AUTH" \
          --cli-input-json "$RT_SETTINGS" \
          --query agentRuntimeVersion --output text 2>"$RT_STATE/$1.err") || return 1
      else
        out=$(aws bedrock-agentcore-control update-agent-runtime \
          --agent-runtime-id "$RT_ID" --region "$REGION" \
          --cli-connect-timeout 10 --cli-read-timeout 60 \
          --agent-runtime-artifact "$RT_ARTIFACT" \
          --protocol-configuration '{"serverProtocol":"MCP"}' \
          --role-arn "$RT_ROLE" \
          --network-configuration "$RT_NETCFG" \
          --environment-variables "$RT_ENV" \
          --cli-input-json "$RT_SETTINGS" \
          --query agentRuntimeVersion --output text 2>"$RT_STATE/$1.err") || return 1
      fi
      out=$(printf '%s' "$out" | tr -d '[:space:]')
      if [ "$out" != "None" ]; then RT_SENT_VER="$out"; fi
    }
    # rt_accepted NAME: records an accepted update for the wait below.
    rt_accepted() {
      rt_put "$1" id "$RT_ID"; rt_put "$1" ver "$RT_SENT_VER"; rt_put "$1" last ""
      if [ -n "$RT_SENT_VER" ]; then
        echo "      $1: update accepted (version $RT_SENT_VER), waiting for READY."
      else
        echo "      $1: update accepted (no version returned; its first READY counts), waiting for READY."
      fi
      if [ -z "$RT_AUTH" ]; then
        echo "      ⚠️  $1 carries NO inbound JWT authorizer. Gateway targets send a"
        echo "          Cognito M2M JWT — a runtime without the customJWTAuthorizer rejects those"
        echo "          calls ('Authorization error when sending message', seen 2026-07-15 on"
        echo "          adt_digital_thread). Align it with the sibling runtimes' authorizer."
      fi
    }
    # rt_read_status NAME: sets RT_ST (runtime status), RT_VER (its version,
    # empty when none) and RT_REASON (its failureReason, empty when none).
    # Non-zero when the status can't be read. A bounded CLI call, so one slow
    # read cannot hold the wait far past its limit.
    rt_read_status() {
      local out
      RT_ST=""; RT_VER=""; RT_REASON=""
      out=$(aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "$(rt_get "$1" id)" --region "$REGION" \
        --cli-connect-timeout 10 --cli-read-timeout 30 \
        --query '[status, agentRuntimeVersion, failureReason]' --output text \
        2>"$RT_STATE/$1.status_err") || return 1
      # failureReason is free text, so it comes last and takes the rest.
      IFS=$'\t' read -r RT_ST RT_VER RT_REASON <<<"$out" || true
      if [ "$RT_VER" = "None" ]; then RT_VER=""; fi
      if [ "$RT_REASON" = "None" ]; then RT_REASON=""; fi
      [ -n "$RT_ST" ] && [ "$RT_ST" != "None" ]
    }
    # rt_version_reached HAVE WANT: HAVE is WANT or a later version.
    rt_version_reached() {
      [ "$1" = "$2" ] && return 0
      case "$1$2" in *[!0-9]*) return 1 ;; esac
      [ "${#1}" -le 18 ] && [ "${#2}" -le 18 ] && [ "$1" -gt "$2" ]
    }
    # rt_read_image NAME: the image the runtime runs now (a bounded CLI call).
    rt_read_image() {
      aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "$(rt_get "$1" id)" --region "$REGION" \
        --cli-connect-timeout 10 --cli-read-timeout 30 \
        --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text \
        2>"$RT_STATE/$1.status_err"
    }
    RT_WAITING=""  # update accepted, waiting for READY
    RT_RETRY=""    # update rejected because the runtime was already UPDATING
    # rt_seconds_setting NAME VALUE DEFAULT: sets RT_SECONDS to VALUE when it
    # is a whole number of seconds from 1 to 86400, else to DEFAULT (warning).
    rt_seconds_setting() {
      RT_SECONDS="$2"
      case "$2" in
        ''|*[!0-9]*) RT_SECONDS="" ;;
        *) if [ "${#2}" -gt 5 ] || [ "$2" -lt 1 ] || [ "$2" -gt 86400 ]; then RT_SECONDS=""; fi ;;
      esac
      if [ -z "$RT_SECONDS" ]; then
        echo "    ⚠️  $1='$2' is not a whole number of seconds from 1 to 86400; using $3."
        RT_SECONDS="$3"
      fi
    }
    # 900 s, the same as the [2c]/[2e] create wait: an in-place update of six
    # runtimes can take longer than 600 s.
    rt_seconds_setting ADT_RUNTIME_READY_TIMEOUT_SECONDS "${ADT_RUNTIME_READY_TIMEOUT_SECONDS:-900}" 900
    RT_READY_TIMEOUT="$RT_SECONDS"
    rt_seconds_setting ADT_RUNTIME_READY_POLL_SECONDS "${ADT_RUNTIME_READY_POLL_SECONDS:-10}" 10
    RT_POLL_INTERVAL="$RT_SECONDS"
    # ONE limit for the whole refresh, counted from the first update: the
    # updates below and the READY wait after them share it. Each control-plane
    # call is bounded, but they run one runtime at a time, so without a shared
    # limit a slow control plane could hold 2f for minutes per runtime before
    # the wait even started. Once the limit has run out no further runtime is
    # prepared or updated; each one left is named and fails the deploy.
    RT_WAIT_START=$SECONDS
    rt_deadline_passed() { [ $((SECONDS - RT_WAIT_START)) -ge "$RT_READY_TIMEOUT" ]; }
    for RT_NAME in $RT_NAMES; do
      if rt_deadline_passed; then
        echo "    ⚠️  $RT_NAME: not sent: the ${RT_READY_TIMEOUT}s refresh limit ran out before its update."
        rt_failed "$RT_NAME" "not sent: the ${RT_READY_TIMEOUT}s refresh limit ran out before its update"
        continue
      fi
      echo "    → $RT_NAME: refreshing to current image..."
      if ! rt_prepare "$RT_NAME"; then
        rt_failed "$RT_NAME" "$RT_PREP_REASON"; continue
      fi
      # Per runtime: surface the error and continue (the deploy fails at the
      # end, naming it).
      if rt_send_update "$RT_NAME"; then
        rt_accepted "$RT_NAME"
        RT_WAITING="$RT_WAITING $RT_NAME"
      elif grep -q 'ConflictException' "$RT_STATE/$RT_NAME.err" 2>/dev/null; then
        echo "      ⏳ $RT_NAME: already UPDATING (update rejected with ConflictException);"
        echo "          the update is rebuilt from its current settings and retried once when that finishes."
        rt_put "$RT_NAME" id "$RT_ID"; rt_put "$RT_NAME" last ""
        RT_RETRY="$RT_RETRY $RT_NAME"
      else
        echo "      ⚠️  $RT_NAME: update FAILED — stays on its CURRENT image:"
        sed 's/^/          /' "$RT_STATE/$RT_NAME.err" 2>/dev/null | head -3
        rt_failed "$RT_NAME" "update rejected"
      fi
    done
    # An accepted update is not a finished one: AgentCore applies it
    # asynchronously and it can still end UPDATE_FAILED. Like the provisioning
    # driver's READY wait for §2c/§2e (scripts/provision_runtimes.py, statuses
    # from agents/provisioner/handlers.py wait_ready), poll every runtime
    # together until READY, within the refresh limit started before the
    # updates: ADT_RUNTIME_READY_TIMEOUT_SECONDS (default 900), polled every
    # ADT_RUNTIME_READY_POLL_SECONDS (default 10), each 1 to 86400.
    # CREATING/UPDATING keep waiting; CREATE_FAILED/UPDATE_FAILED, any other
    # status, an unreadable status or the time limit is a failure naming the
    # runtime. READY counts only at the version the update returned (or a
    # later one): a read straight after the update can still describe the
    # previous version. When the update returned no version, or the read has
    # none, the first READY counts (what wait_ready does). A runtime that was
    # already UPDATING gets its update rebuilt and re-sent once when it leaves
    # UPDATING. The limit is checked before every poll and retry, not only
    # once per round.
    if [ -n "${RT_WAITING// /}${RT_RETRY// /}" ]; then
      echo "    Waiting for the refreshed runtimes to reach READY (up to ${RT_READY_TIMEOUT}s from the first update; $((SECONDS - RT_WAIT_START))s used)..."
    fi
    while [ -n "${RT_WAITING// /}${RT_RETRY// /}" ]; do
      RT_NEXT_WAITING=""; RT_NEXT_RETRY=""
      for RT_NAME in $RT_WAITING $RT_RETRY; do
        case " $RT_RETRY " in *" $RT_NAME "*) RT_IS_RETRY=1 ;; *) RT_IS_RETRY=0 ;; esac
        if rt_deadline_passed; then
          # Out of time: no more polls or retries; reported after this round.
          if [ "$RT_IS_RETRY" = "1" ]; then
            RT_NEXT_RETRY="$RT_NEXT_RETRY $RT_NAME"
          else
            RT_NEXT_WAITING="$RT_NEXT_WAITING $RT_NAME"
          fi
          continue
        fi
        if ! rt_read_status "$RT_NAME"; then
          echo "      ⚠️  $RT_NAME: could not read its status:"
          sed 's/^/          /' "$RT_STATE/$RT_NAME.status_err" 2>/dev/null | head -3
          rt_failed "$RT_NAME" "could not read status"; continue
        fi
        rt_put "$RT_NAME" last "$RT_ST"
        case "$RT_ST" in
          CREATING|UPDATING)
            if [ "$RT_IS_RETRY" = "1" ]; then
              RT_NEXT_RETRY="$RT_NEXT_RETRY $RT_NAME"
            else
              RT_NEXT_WAITING="$RT_NEXT_WAITING $RT_NAME"
            fi ;;
          READY|CREATE_FAILED|UPDATE_FAILED)
            if [ "$RT_IS_RETRY" = "1" ]; then
              if rt_deadline_passed; then
                RT_NEXT_RETRY="$RT_NEXT_RETRY $RT_NAME"; continue
              fi
              echo "      $RT_NAME: retrying the update once (no longer UPDATING, status $RT_ST),"
              echo "          rebuilt from its current settings..."
              if ! rt_prepare "$RT_NAME"; then
                rt_failed "$RT_NAME" "$RT_PREP_REASON, before the retry"
              elif rt_send_update "$RT_NAME"; then
                rt_accepted "$RT_NAME"
                RT_NEXT_WAITING="$RT_NEXT_WAITING $RT_NAME"
              else
                echo "      ⚠️  $RT_NAME: update FAILED again — stays on its CURRENT image:"
                sed 's/^/          /' "$RT_STATE/$RT_NAME.err" 2>/dev/null | head -3
                rt_failed "$RT_NAME" "update rejected again after waiting"
              fi
            elif [ "$RT_ST" = "READY" ]; then
              RT_WANT=$(rt_get "$RT_NAME" ver)
              if [ -z "$RT_WANT" ] || [ -z "$RT_VER" ] || [ "$RT_VER" = "$RT_WANT" ]; then
                echo "      ✅ $RT_NAME: READY on the current image."
                RT_OK=$((RT_OK + 1))
              elif rt_version_reached "$RT_VER" "$RT_WANT"; then
                # A later version: another writer updated the runtime after
                # this deploy did. An in-app runtime bump re-sends the image it
                # read before, so the later version counts only if it still
                # runs this deploy's image.
                RT_IMG_OK=1
                RT_IMG=$(rt_read_image "$RT_NAME") || RT_IMG_OK=0
                if [ "$RT_IMG_OK" = 1 ] && [ "$RT_IMG" = "$ORCH_IMAGE" ]; then
                  echo "      ✅ $RT_NAME: READY on the current image (version $RT_VER)."
                  RT_OK=$((RT_OK + 1))
                elif [ "$RT_IMG_OK" = 0 ]; then
                  echo "      ⚠️  $RT_NAME: could not read its image after a later update (version $RT_VER):"
                  sed 's/^/          /' "$RT_STATE/$RT_NAME.status_err" 2>/dev/null | head -3
                  rt_failed "$RT_NAME" "could not read its image after a later update (version $RT_VER)"
                else
                  echo "      ⚠️  $RT_NAME: a later update (version $RT_VER) runs $RT_IMG, not $ORCH_IMAGE"
                  rt_failed "$RT_NAME" "a later update (version $RT_VER) replaced this deploy's image"
                fi
              else
                # Still the previous version: keep waiting for this update's.
                rt_put "$RT_NAME" last "READY at version $RT_VER, waiting for version $RT_WANT"
                RT_NEXT_WAITING="$RT_NEXT_WAITING $RT_NAME"
              fi
            else
              echo "      ⚠️  $RT_NAME: $RT_ST${RT_REASON:+: $RT_REASON}"
              rt_failed "$RT_NAME" "$RT_ST${RT_REASON:+: $RT_REASON}"
            fi ;;
          *)
            echo "      ⚠️  $RT_NAME: unexpected status $RT_ST after the update."
            rt_failed "$RT_NAME" "unexpected status $RT_ST" ;;
        esac
      done
      RT_WAITING="$RT_NEXT_WAITING"; RT_RETRY="$RT_NEXT_RETRY"
      if [ -z "${RT_WAITING// /}${RT_RETRY// /}" ]; then break; fi
      if rt_deadline_passed; then
        for RT_NAME in $RT_WAITING; do
          RT_LAST=$(rt_get "$RT_NAME" last)
          if [ -n "$RT_LAST" ]; then
            RT_WHY="not READY after ${RT_READY_TIMEOUT}s, last status $RT_LAST"
          else
            RT_WHY="not READY after ${RT_READY_TIMEOUT}s, not polled since its update was sent"
          fi
          echo "      ⚠️  $RT_NAME: $RT_WHY."
          rt_failed "$RT_NAME" "$RT_WHY"
        done
        for RT_NAME in $RT_RETRY; do
          RT_LAST=$(rt_get "$RT_NAME" last)
          case "$RT_LAST" in
            CREATING|UPDATING) RT_WHY="still $RT_LAST after ${RT_READY_TIMEOUT}s, update not retried" ;;
            '') RT_WHY="update not retried within ${RT_READY_TIMEOUT}s, status not read" ;;
            *) RT_WHY="update not retried within ${RT_READY_TIMEOUT}s, last status $RT_LAST" ;;
          esac
          echo "      ⚠️  $RT_NAME: $RT_WHY."
          rt_failed "$RT_NAME" "$RT_WHY"
        done
        break
      fi
      RT_ELAPSED=$((SECONDS - RT_WAIT_START))
      RT_SLEEP=$RT_POLL_INTERVAL
      # Never sleep past the limit.
      if [ $((RT_READY_TIMEOUT - RT_ELAPSED)) -lt "$RT_SLEEP" ]; then
        RT_SLEEP=$((RT_READY_TIMEOUT - RT_ELAPSED))
      fi
      echo "      ... still waiting on:$RT_WAITING$RT_RETRY (${RT_ELAPSED}s of ${RT_READY_TIMEOUT}s)"
      sleep "$RT_SLEEP"
    done
    rm -rf "$RT_STATE/state"
    echo "    Per-system runtime refresh: $RT_OK refreshed (READY), $RT_FAIL failed."
    if [ "$RT_FAIL" -gt 0 ]; then
      echo "        Not refreshed: $RT_FAILED_NAMES. The deploy will exit non-zero."
      record_postcdk_failure "[2f] per-system runtime refresh failed for: $RT_FAILED_NAMES"
    fi
  fi
fi

# ── 3. Summary ──────────────────────────────────────────────────────────────
GW_ID=$(cfn_output "ADTAgentCoreStack" "GatewayId" 2>/dev/null || echo "")
GW_URL=$(cfn_output "ADTAgentCoreStack" "GatewayUrl" 2>/dev/null || echo "")
# The orchestrator runtime is created imperatively above (ADR 0010); the agent
# image is built/published by CDK (ADTComputeStack DockerImageAsset). The ARN below
# is the one just created/updated, or discovered from the AgentCore control plane.
if [ -z "${ORCH_ARN:-}" ] || [ "${ORCH_ARN}" = "None" ]; then
  ORCH_ARN=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='adt_orchestrator'].agentRuntimeArn | [0]" \
    --output text 2>/dev/null || echo "")
fi
echo ""
echo "==> [3/4] Deploy complete!"
echo "  AgentCore Gateway:      ${GW_ID:-not deployed}"
echo "  Gateway URL:            ${GW_URL:-not deployed}"
echo "  Orchestrator Runtime:   ${ORCH_ARN:-NOT CREATED — check [2c] output above}"
echo "  Onboarding Runtime:     ${ONB_ARN:-not created — check [2e] output above}"
echo "  Onboarding Gateway:     ${ONB_GW_ID:-not deployed}"
echo "  Neptune Graph:          ${GRAPH_ID:-not deployed}"
echo "  Edge auth:              $([[ "${EDGE_AUTH_VALUE:-true}" == "true" ]] && echo "on (Cognito hosted-UI redirect)" || echo "off (login form)")"
echo "  Region:                 $REGION"

# ── 3b. Reconcile QUERY gateway targets against current runtime ARNs ─────────
# Each per-system agent's adt_<id> runtime gets a NEW ARN whenever it is recreated
# (manual re-provision, scripts/reonboard.py, or any path that deletes+recreates
# the runtime). The step above re-syncs the ONBOARDING gateway target, but nothing
# re-syncs the QUERY gateway targets — so a query target can be left pointing at a
# stale/deleted ARN, whose live symptom is "No endpoint or agent found with
# qualifier 'DEFAULT'" for exactly the drifted systems (a system whose target
# happened to already be current keeps working, so the failure looks selective).
# This step detects that drift and repoints each affected target at the live ARN.
# It is idempotent (a healthy gateway is a no-op). A reconcile that fails or
# cannot run (gateway id unreadable) is surfaced here, lets [4/4] run, and then
# fails the deploy at the end (the stacks are already up). See
# scripts/reconcile_query_gateway.py.
echo ""
echo "==> [3b] Reconciling query gateway targets (auto-heal stale runtime ARNs)..."
require_aws_session 3b
# GW_ID was read for the summary above; read it again if that read came back
# empty (the credential check above passing means an empty value now is real).
if [ -z "${GW_ID:-}" ] || [ "${GW_ID}" = "None" ]; then
  GW_ID=$(cfn_output "ADTAgentCoreStack" "GatewayId" 2>/dev/null || echo "")
fi
if [ -n "${GW_ID:-}" ] && [ "${GW_ID}" != "None" ]; then
  RECONCILE_RC=0
  python3 "$REPO_ROOT/scripts/reconcile_query_gateway.py" \
      --gateway-id "$GW_ID" --region "$REGION" \
      ${AWS_PROFILE:+--profile "$AWS_PROFILE"} || RECONCILE_RC=$? # nosemgrep -- ${VAR:+...} zero-or-two-word idiom, value quoted
  if [ "$RECONCILE_RC" -eq 0 ]; then
    echo "    Query gateway targets reconciled: ✅"
  else
    echo "    ⚠️  Query gateway reconcile FAILED (exit $RECONCILE_RC, see above)."
    echo "        The deploy will exit non-zero; fix the cause and re-run."
    record_postcdk_failure "[3b] query-target reconcile (reconcile_query_gateway.py exited $RECONCILE_RC)"
  fi
else
  echo "    ⚠️  Query gateway id unreadable (ADTAgentCoreStack GatewayId) — reconcile did NOT run."
  echo "        The deploy will exit non-zero; re-run it once the stack output is readable."
  record_postcdk_failure "[3b] query-target reconcile (query gateway id unreadable)"
fi

# ── 4. Invalidate CloudFront cache ──────────────────────────────────────────
# Static files (JS/HTML/CSS) are served through CloudFront. After every deploy
# the cache must be invalidated so users get the new version immediately.
echo ""
echo "==> [4/4] Invalidating CloudFront cache..."
require_aws_session 4/4
# Resolve the APP distribution from ADTComputeStack's own ADTCloudFrontUrl output
# (always the distribution's *.cloudfront.net domain, custom domain or not). The
# old first-match on an ELB origin is ambiguous wherever a second ALB-fronted
# distribution exists (the ERP dashboard on --simulator, HighByte): both qualify
# and `| [0]` took whichever came first, the ERP one on adt-demo (2026-10-06).
CF_DIST=""
CF_URL=$(cfn_output "ADTComputeStack" "ADTCloudFrontUrl" 2>/dev/null || echo "")
if [ -n "$CF_URL" ] && [ "$CF_URL" != "None" ]; then
  CF_DOMAIN=$(echo "$CF_URL" | sed 's|https://||' | sed 's|/.*||')
  CF_DIST=$(aws cloudfront list-distributions --region us-east-1 $_AWS_PROFILE_FLAG \
    --query "DistributionList.Items[?DomainName=='$CF_DOMAIN'].Id | [0]" \
    --output text 2>/dev/null || echo "")
fi
if [ -n "$CF_DIST" ] && [ "$CF_DIST" != "None" ]; then
  INV_ID=$(aws cloudfront create-invalidation \
    --distribution-id "$CF_DIST" \
    --paths "/*" \
    --region us-east-1 $_AWS_PROFILE_FLAG \
    --query 'Invalidation.Id' --output text 2>"$DEPLOY_TMP/cf_invalidation_err" || echo "")
  if [ -n "$INV_ID" ] && [ "$INV_ID" != "None" ]; then
    echo "    CloudFront invalidation created: $INV_ID (distribution: $CF_DIST)"
    echo "    Waiting for invalidation to complete..."
    # Not fatal: the invalidation was accepted and CloudFront finishes it on
    # its own even if this wait gives up first.
    if aws cloudfront wait invalidation-completed \
      --distribution-id "$CF_DIST" \
      --id "$INV_ID" \
      --region us-east-1 $_AWS_PROFILE_FLAG 2>/dev/null; then
      echo "    CloudFront cache cleared: ✅"
    else
      echo "    CloudFront invalidation $INV_ID is still in progress (the wait gave up);"
      echo "        it finishes on its own, usually within a few minutes."
    fi
  else
    echo "    ⚠️  CloudFront invalidation request FAILED for $CF_DIST — the app cache was NOT cleared:"
    sed -e '/^[[:space:]]*$/d' -e 's/^/        /' "$DEPLOY_TMP/cf_invalidation_err" 2>/dev/null | head -3 || true
    record_postcdk_failure "[4/4] CloudFront invalidation (request failed for $CF_DIST)"
  fi
else
  echo "    ⚠️  App CloudFront distribution not found (ADTComputeStack ADTCloudFrontUrl: ${CF_URL:-unreadable})"
  echo "        — the app cache was NOT cleared; browsers may serve the previous UI until it expires."
  record_postcdk_failure "[4/4] CloudFront invalidation (app distribution not found)"
fi

# ── AWS Quick MCP connector values (--quick-connector) ───────────────────
# What the Quick connector form asks for (Service authentication, docs/
# quick-web-connector.md). The secret VALUE is never printed, only the
# command that reads it.
if [[ "${QUICK_CONNECTOR_ENABLE:-0}" -eq 1 ]]; then
  echo ""
  echo "==> AWS Quick MCP connector: waiting for the query-bridge target on adt-quick-gw..."
  # The app provisions the target from its startup thread, after the ECS
  # rollout CDK waited for, so a green deploy alone does not prove it exists.
  _QC_GW=$(cfn_output ADTAgentCoreStack QuickGatewayId || echo "")
  _QC_STATUS=""
  if [[ -n "$_QC_GW" && "$_QC_GW" != "None" ]]; then
    for _QC_TRY in $(seq 1 "${QUICK_BRIDGE_WAIT_POLLS:-30}"); do
      _QC_STATUS=$(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$_QC_GW" \
        --region "$REGION" $_AWS_PROFILE_FLAG \
        --query "items[?name=='query-bridge'].status | [0]" --output text 2>/dev/null || echo "")
      [[ "$_QC_STATUS" == "READY" || "$_QC_STATUS" == "FAILED" ]] && break
      sleep "${QUICK_BRIDGE_WAIT_SECONDS:-20}"
    done
  fi
  if [[ "$_QC_STATUS" == "READY" ]]; then
    echo "    query-bridge target: READY ✅"
  else
    echo "    ⚠  query-bridge target on ${_QC_GW:-<no QuickGatewayId output>} is ${_QC_STATUS:-missing} (expected READY)."
    echo "        Check the app log for 'query-bridge' and re-run deploy.sh with the same flags."
    record_postcdk_failure "AWS Quick query-bridge target on adt-quick-gw (${_QC_STATUS:-missing})"
  fi
  echo "    MCP server URL : $(cfn_output ADTAgentCoreStack QuickGatewayUrl)"
  if [[ -n "${_QUICK_OIDC_CDK_FLAGS:-}" ]]; then
    # ADR-0079: this gateway now accepts only the external provider's
    # delegated user tokens; the Cognito service client is not allowed on it.
    echo "    Authentication : User authentication against the external OIDC provider (ADR-0079;"
    echo "                     docs/quick-web-connector.md). The Cognito service values do not apply."
  else
    echo "    Authentication : Service authentication (OAuth 2.0 client credentials)"
    echo "    Token URL      : $(cfn_output ADTAgentCoreStack QuickTokenUrl)"
    # Quick's own client (adt-quick-service), accepted by adt-quick-gw only;
    # never AppServiceClientId, the app's client that adt-query-gw accepts.
    echo "    Client ID      : $(cfn_output ADTAgentCoreStack QuickClientId)"
    echo "    Scope          : adt-quick/bridge"
    _QC_SECRET=$(cfn_output ADTAgentCoreStack QuickClientSecretName || echo "")
    echo "    Client secret  : aws secretsmanager get-secret-value --secret-id ${_QC_SECRET:-adt-quick-client-secret} --query SecretString --output text --region $REGION $_AWS_PROFILE_FLAG"
  fi
fi

if [ -n "${POSTCDK_FAILED:-}" ]; then
  echo "" >&2
  echo "ERROR: the stacks are deployed but these post-CDK steps did not complete:" >&2
  printf '%s\n' "$POSTCDK_FAILED" | sed 's/^/    - /' >&2
  echo "    See the messages above, fix the cause and re-run deploy.sh with the same flags." >&2
  exit 1
fi
