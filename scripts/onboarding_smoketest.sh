#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# onboarding_smoketest.sh — resolve the deployed app URL + demo password, then run
# the deterministic onboarding smoke-test harness (scripts/onboarding_smoketest.py).
# Onboards the demo systems through the SAME REST onboarding API the UI uses
# (no special-case path) and asserts the outcomes.
#
# Usage: ./scripts/onboarding_smoketest.sh [--profile <p>] [--region <r>] [--systems a,b,c] [-- <extra py args>]
# --include-ot also seeds the OT systems; a 'tulip' system must first be registered in
# Connect New System with its API key and secret (the token mint cannot complete it).
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

PROFILE=""; REGION="${AWS_DEFAULT_REGION:-us-east-1}"; SYSTEMS=""; INCLUDE_OT=""; EXTRA=()
while [[ $# -gt 0 ]]; do
  case $1 in
    --profile) PROFILE="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    --systems) SYSTEMS="$2"; shift 2 ;;
    --include-ot) INCLUDE_OT="1"; shift ;;
    --) shift; EXTRA=("$@"); break ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done
AWS=(aws --region "$REGION"); [[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

echo "==> Resolving deployed app URL + demo password..."
BASE_URL="$("${AWS[@]}" cloudformation describe-stacks --stack-name ADTComputeStack \
  --query "Stacks[0].Outputs[?OutputKey=='ADTCloudFrontUrl'].OutputValue" --output text)"
PASSWORD="$("${AWS[@]}" secretsmanager get-secret-value --secret-id adt-demo-password \
  --query SecretString --output text)"
if [[ -z "$BASE_URL" || "$BASE_URL" == "None" || -z "$PASSWORD" ]]; then
  echo "ERROR: could not resolve ADTCloudFrontUrl or adt-demo-password" >&2
  exit 2
fi
echo "    app: $BASE_URL"

# Resolve and EXPORT the deploy-derived connection env so the harness can SUPPLY
# each system's connection as INPUT (Step 3, Task 4.1). The harness builds the
# connection_string/token from these env vars per the seed manifest's
# connection_bootstrap descriptor — it does NOT rely on a server-side catalog
# snapshot. Best-effort: a lookup that returns empty just means that system is
# skipped by the harness (the SQL gate still runs). In LOCAL mode these come from
# .env.local in the same shell and this resolution is skipped.
_q() { local v; v="$("${AWS[@]}" "$@" 2>/dev/null || true)"; [[ "$v" == "None" ]] && v=""; printf '%s' "$v"; }

echo "==> Resolving deploy-derived connection env (best-effort)..."
# demo database (cmms/plm + non-ERP schemas) → POSTGRES_*
# The adt_demo RDS is created by ADTSeedDataStack (NOT ADTDatabaseStack,
# which is Neptune-only). Its endpoint/secret are exposed there as
# ADTRdsEndpoint / ADTRdsSecretArn.
_ADT_DB_HOST="$(_q cloudformation describe-stacks --stack-name ADTSeedDataStack \
  --query "Stacks[0].Outputs[?OutputKey=='ADTRdsEndpoint'].OutputValue" --output text)"
[[ -n "$_ADT_DB_HOST" ]] && export POSTGRES_HOST="$_ADT_DB_HOST"
_ADT_SECRET_ARN="$(_q cloudformation describe-stacks --stack-name ADTSeedDataStack \
  --query "Stacks[0].Outputs[?OutputKey=='ADTRdsSecretArn'].OutputValue" --output text)"
# ERP/Aurora (erp_*/mes_* schemas) → ERP_POSTGRES_*
_ERP_DB_HOST="$(_q cloudformation describe-stacks --stack-name ADTERPDatabaseStack \
  --query "Stacks[0].Outputs[?OutputKey=='ClusterEndpoint'].OutputValue" --output text)"
[[ -n "$_ERP_DB_HOST" ]] && export ERP_POSTGRES_HOST="$_ERP_DB_HOST"
_ERP_DB_PORT="$(_q cloudformation describe-stacks --stack-name ADTERPDatabaseStack \
  --query "Stacks[0].Outputs[?OutputKey=='ClusterPort'].OutputValue" --output text)"
[[ -n "$_ERP_DB_PORT" ]] && export ERP_POSTGRES_PORT="$_ERP_DB_PORT"
_ERP_SECRET_ARN="$(_q cloudformation describe-stacks --stack-name ADTERPDatabaseStack \
  --query "Stacks[0].Outputs[?OutputKey=='SecretArn'].OutputValue" --output text)"

# Export the DB credentials from the RDS secret(s) so the harness can build a
# usable DSN to SUPPLY as input. The RDS secret is JSON ({username,password,...});
# parse with python3 (guaranteed — we exec it below). Without these the harness
# omits connection_string and the server uses its catalog fallback (still-present
# until the Task 4.2 cut-over). Never echoed.
_export_db_creds() {  # $1=secret-arn  $2=user-var  $3=pass-var  $4=db-var
  [[ -z "$1" ]] && return 0
  local json; json="$(_q secretsmanager get-secret-value --secret-id "$1" \
    --query SecretString --output text)"
  [[ -z "$json" ]] && return 0
  local user pass dbn
  user="$(printf '%s' "$json" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("username",""))' 2>/dev/null || true)"
  pass="$(printf '%s' "$json" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("password",""))' 2>/dev/null || true)"
  dbn="$(printf '%s' "$json" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("dbname",""))' 2>/dev/null || true)"
  [[ -n "$user" ]] && export "$2"="$user"
  [[ -n "$pass" ]] && export "$3"="$pass"
  [[ -n "$dbn"  ]] && export "$4"="$dbn"
}
_export_db_creds "$_ADT_SECRET_ARN"  POSTGRES_USER     POSTGRES_PASSWORD     POSTGRES_DB
_export_db_creds "$_ERP_SECRET_ARN" ERP_POSTGRES_USER ERP_POSTGRES_PASSWORD ERP_POSTGRES_DB

if [[ -n "$INCLUDE_OT" ]]; then
  # HighByte (MCP) — URL from SSM, bearer token from Secrets Manager. The token
  # now lives in the canonical conn/highbyte JSON secret ({"token":"<jwt>"}).
  _HB_URL="$(_q ssm get-parameter --name /adt/highbyte-url \
    --query "Parameter.Value" --output text)"
  [[ -n "$_HB_URL" ]] && export HIGHBYTE_URL="$_HB_URL"
  _HB_TOKEN_JSON="$(_q secretsmanager get-secret-value --secret-id adt/conn/highbyte \
    --query SecretString --output text)"
  _HB_TOKEN="$(printf '%s' "$_HB_TOKEN_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)"
  [[ -n "$_HB_TOKEN" ]] && export HIGHBYTE_I3X_TOKEN="$_HB_TOKEN"
fi

ARGS=(--base-url "$BASE_URL" --password "$PASSWORD" --region "$REGION" --assert-runtimes)
[[ -n "$PROFILE" ]] && ARGS+=(--profile "$PROFILE")
[[ -n "$SYSTEMS" ]] && ARGS+=(--systems "$SYSTEMS")
[[ -n "$INCLUDE_OT" ]] && ARGS+=(--include-ot)

exec python3 scripts/onboarding_smoketest.py "${ARGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"}
