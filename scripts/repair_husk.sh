#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# repair_husk.sh — one-command repair for the offboard-reset 504 husk strand.
#
# An interrupted `POST /api/onboard/reset/<id>` (the CloudFront VPC origin
# read_timeout is 120 s; the reset blocks for minutes) can leave a system's
# connection record as an all-null husk and drop its catalog entry entirely.
# The documented repair ("Offboard reset through CloudFront can 504")
# is three manual steps; this script runs them end to end:
#
#   1. Force a new ECS deployment of the ADT app and wait for steady state —
#      demo systems re-register in the onboard catalog at startup.
#   2. Derive the demo seed-DB connection from the sanctioned SSM pointer
#      (/adt/demo/seed-db) + its Secrets Manager secret, exported
#      as POSTGRES_* for reonboard's --from-env escape hatch (the husk record
#      is unusable by definition, so records-first cannot help).
#   3. Run `scripts/reonboard.py --systems <ids> --from-env`, which deletes the
#      stranded runtime/target, re-runs discover→map→resolve→provision, and
#      BLOCKS until the runtime AND gateway target are READY.
#
# Everything is DISCOVERED live (CFN outputs/resources, SSM pointer, gateway
# list) — nothing environment-specific is hardcoded, so it survives redeploys.
# Secrets are exported into the process env only and never printed.
#
# Usage:
#   scripts/repair_husk.sh --systems cmms[,erp,...] [--profile <p>] [--region <r>]
#                          [--base-url <url>] [--password <pw>] [--yes]
set -euo pipefail

SYSTEMS="" PROFILE="${AWS_PROFILE:-}" REGION="${AWS_REGION:-us-east-1}"
BASE_URL="${ADT_BASE_URL:-}" PASSWORD="${ADT_PASSWORD:-}" ASSUME_YES="false"

while [ $# -gt 0 ]; do
  case "$1" in
    --systems)  SYSTEMS="$2"; shift 2 ;;
    --profile)  PROFILE="$2"; shift 2 ;;
    --region)   REGION="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --password) PASSWORD="$2"; shift 2 ;;
    --yes)      ASSUME_YES="true"; shift ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done
[ -n "$SYSTEMS" ] || { echo "--systems <id[,id...]> is required" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_FLAG=(); [ -n "$PROFILE" ] && PROFILE_FLAG=(--profile "$PROFILE")
aws_() { aws "$@" --region "$REGION" "${PROFILE_FLAG[@]}"; }

cfn_output() { # <stack> <output-key>
  aws_ cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue | [0]" --output text 2>/dev/null || echo ""
}

# ── Discover the deployment ─────────────────────────────────────────────────
[ -n "$BASE_URL" ] || BASE_URL=$(cfn_output "ADTComputeStack" "ADTCloudFrontUrl")
[ -n "$BASE_URL" ] && [ "$BASE_URL" != "None" ] || { echo "cannot resolve ADT_BASE_URL (is ADTComputeStack deployed in $REGION?)" >&2; exit 1; }
if [ -z "$PASSWORD" ]; then
  PASSWORD=$(aws_ secretsmanager get-secret-value --secret-id adt-demo-password \
    --query SecretString --output text 2>/dev/null || echo "")
fi
[ -n "$PASSWORD" ] || { echo "cannot resolve the demo password (pass --password or grant adt-demo-password read)" >&2; exit 1; }

CLUSTER=$(aws_ cloudformation describe-stack-resources --stack-name "ADTComputeStack" \
  --query "StackResources[?ResourceType=='AWS::ECS::Cluster'].PhysicalResourceId | [0]" --output text)
SERVICE_ARN=$(aws_ cloudformation describe-stack-resources --stack-name "ADTComputeStack" \
  --query "StackResources[?ResourceType=='AWS::ECS::Service'].PhysicalResourceId | [0]" --output text)
[ -n "$CLUSTER" ] && [ "$CLUSTER" != "None" ] && [ -n "$SERVICE_ARN" ] && [ "$SERVICE_ARN" != "None" ] \
  || { echo "cannot discover the ADT app ECS cluster/service from ADTComputeStack" >&2; exit 1; }
SERVICE="${SERVICE_ARN##*/}"

GW=$(aws_ bedrock-agentcore-control list-gateways \
  --query "items[?ends_with(name,'-query-gw')].gatewayId | [0]" --output text 2>/dev/null || echo "")
[ -n "$GW" ] && [ "$GW" != "None" ] || { echo "query gateway not found (is the env deployed?)" >&2; exit 1; }

echo "==> repair plan"
echo "    base url : $BASE_URL"
echo "    cluster  : $CLUSTER"
echo "    service  : $SERVICE"
echo "    gateway  : $GW"
echo "    systems  : $SYSTEMS"
if [ "$ASSUME_YES" != "true" ]; then
  read -r -p "Force a new ECS deployment and re-onboard these systems? [y/N] " REPLY
  case "$REPLY" in [Yy]*) ;; *) echo "aborted"; exit 1 ;; esac
fi

# ── 1. Restart the app so demo systems re-register ──────────────────────────
echo "==> forcing new ECS deployment (demo systems re-register at startup)"
aws_ ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
  --force-new-deployment --query 'service.serviceName' --output text > /dev/null
echo "==> waiting for service steady state (this takes a few minutes)"
aws_ ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"

# ── 2. Seed-DB connection for --from-env (SSM pointer → secret) ─────────────
echo "==> deriving seed-DB connection from the SSM demo pointer"
POINTER=$(aws_ ssm get-parameter --name "/adt/demo/seed-db" \
  --query Parameter.Value --output text 2>/dev/null || echo "")
[ -n "$POINTER" ] || { echo "SSM pointer /adt/demo/seed-db not found — was the env deployed with --seed?" >&2; exit 1; }
POSTGRES_HOST=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('host',''))" "$POINTER")
POSTGRES_PORT=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('port','5432'))" "$POINTER")
POSTGRES_DB=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('dbname',''))" "$POINTER")
SECRET_ARN=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('secret_arn',''))" "$POINTER")
[ -n "$POSTGRES_HOST" ] && [ -n "$SECRET_ARN" ] || { echo "seed-db pointer is incomplete: $POINTER" >&2; exit 1; }
SECRET_JSON=$(aws_ secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query SecretString --output text)
POSTGRES_USER=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('username','postgres'))" "$SECRET_JSON")
POSTGRES_PASSWORD=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('password',''))" "$SECRET_JSON")
unset SECRET_JSON
[ -n "$POSTGRES_PASSWORD" ] || { echo "seed-db secret has no password field" >&2; exit 1; }
export POSTGRES_HOST POSTGRES_PORT POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD

# ── 3. Re-onboard (blocks until runtime + gateway target READY) ─────────────
echo "==> reonboard --from-env: $SYSTEMS"
python3 "$SCRIPT_DIR/reonboard.py" \
  --base-url "$BASE_URL" --password "$PASSWORD" \
  --gateway-id "$GW" --systems "$SYSTEMS" --from-env \
  --region "$REGION" ${PROFILE:+--profile "$PROFILE"}

echo "==> repair complete — verify with: python3 $SCRIPT_DIR/smoke_prompts.py --base-url $BASE_URL --password \$ADT_PASSWORD"
