#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# update-links.sh — Update demo app quick-link URLs after optional stacks deploy.
#
# Run this after deploying any of these stacks:
#   - ADTHighByteOpcuaStack  (provides HIGHBYTE_URL)
#   - ADTERPDashboardStack   (provides ERP_DASHBOARD_URL)
#   - i3X Server stack    (provides SITEWISE_I3X_URL + SITEWISE_I3X_TOKEN)
#
# Usage:
#   ./update-links.sh --profile <aws-profile> [--region <region>]
#
# Safe to run multiple times — idempotent.

set -euo pipefail
cd "$(dirname "$0")/.."

# Leading underscore = INTENTIONAL word-splitting (semgrep convention for
# unquoted-variable-expansion-in-command): expands to zero words when no
# --profile was given. Array-free on purpose — bash 3.2 + `set -u` safe.
_AWS_PROFILE_FLAG=""
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --profile) export AWS_PROFILE="$2"; _AWS_PROFILE_FLAG="--profile $2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    *) echo "Usage: ./update-links.sh [--profile <profile>] [--region <region>]"; exit 1 ;;
  esac
done

ACCOUNT=$(aws sts get-caller-identity $_AWS_PROFILE_FLAG --query Account --output text --region "$REGION")
echo "==> Account: $ACCOUNT  Region: $REGION"

cfn_output() {
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" $_AWS_PROFILE_FLAG \
    --query "Stacks[0].Outputs[?OutputKey==\`$2\`].OutputValue" --output text 2>/dev/null || echo ""
}

# ── 1. Read URLs from deployed stacks ────────────────────────────────────────
echo ""
echo "==> Reading stack outputs..."

HIGHBYTE_URL=$(cfn_output "ADTHighByteOpcuaStack" "HighByteEndpoint")
ERP_DASHBOARD_URL=$(cfn_output "ADTERPDashboardStack" "DashboardUrl")
# Optional external integration: set these to your i3X/SiteWise explorer stack names.
SITEWISE_I3X_URL=""
SITEWISE_I3X_TOKEN=""
if [ -n "${I3X_CLOUDFRONT_STACK:-}" ]; then
  SITEWISE_I3X_URL=$(cfn_output "$I3X_CLOUDFRONT_STACK" "ApiUrl")
fi
if [ -n "${I3X_ECS_STACK:-}" ]; then
  SITEWISE_I3X_TOKEN=$(cfn_output "$I3X_ECS_STACK" "AuthToken")
fi

echo "  HIGHBYTE_URL:       ${HIGHBYTE_URL:-not deployed}"
echo "  ERP_DASHBOARD_URL:  ${ERP_DASHBOARD_URL:-not deployed}"
echo "  SITEWISE_I3X_URL:   ${SITEWISE_I3X_URL:-not deployed}"
echo "  SITEWISE_I3X_TOKEN: ${SITEWISE_I3X_TOKEN:+[set]}"

# ── 2. Write non-secret URLs to SSM and tokens to Secrets Manager ─────────────
echo ""
echo "==> Writing quick links and token secrets..."

write_ssm() {
  local name="$1"
  local value="$2"
  if [ -n "$value" ] && [ "$value" != "None" ]; then
    aws ssm put-parameter --name "$name" --value "$value" \
      --type "String" --overwrite --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null
    echo "  ✅ $name"
  else
    echo "  ⏭  $name (skipped — stack not deployed)"
  fi
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
  echo "  ✅ $name"
}

secret_arn() {
  local arn
  arn=$(aws secretsmanager describe-secret --secret-id "$1" --region "$REGION" \
    $_AWS_PROFILE_FLAG --query ARN --output text 2>/dev/null || true)
  if [ -n "$arn" ] && [ "$arn" != "None" ]; then
    printf '%s' "$arn"
  fi
}

# The ADT task definition resolves these SSM parameters at TASK START
# (ecs.Secret.from_ssm_parameter in adt_compute_stack.py). deploy.sh seeds
# every referenced parameter before the first CDK deploy, so skipping an
# absent value here keeps the previous setting rather than breaking launch.
write_ssm "/adt/highbyte-url"       "$HIGHBYTE_URL"
write_ssm "/adt/erp-dashboard-url"  "$ERP_DASHBOARD_URL"
write_ssm "/adt/sitewise-i3x-url"   "$SITEWISE_I3X_URL"
# TLS verification for i3X calls (task-start-resolved like the URLs above):
# "true" = verify (secure default, seeded by deploy.sh); set I3X_TLS_VERIFY=false
# when the i3X server presents a self-signed certificate. Written only when the
# caller supplies a value so a plain run never clobbers the current setting.
write_ssm "/adt/i3x-tls-verify"     "${I3X_TLS_VERIFY:-}"

if [ -n "$SITEWISE_I3X_TOKEN" ] && [ "$SITEWISE_I3X_TOKEN" != "None" ]; then
  upsert_secret "adt/sitewise-i3x-token" "$SITEWISE_I3X_TOKEN"
fi
if [ -n "${HIGHBYTE_I3X_TOKEN:-}" ] && [ "$HIGHBYTE_I3X_TOKEN" != "None" ]; then
  upsert_secret "adt/highbyte-i3x-token" "$HIGHBYTE_I3X_TOKEN"
fi
SITEWISE_I3X_SECRET_ARN=$(secret_arn "adt/sitewise-i3x-token")
HIGHBYTE_I3X_SECRET_ARN=$(secret_arn "adt/highbyte-i3x-token")

# ── 3. Update ECS task definition ────────────────────────────────────────────
echo ""
echo "==> Updating ECS task definition..."

ADT_CLUSTER=$(aws ecs list-clusters --region "$REGION" $_AWS_PROFILE_FLAG \
  --query "clusterArns[?contains(@,'ADTCompute')]" --output text | awk '{print $1}')

if [ -z "$ADT_CLUSTER" ]; then
  echo "  ERROR: ADTComputeStack cluster not found. Deploy ADTComputeStack first."
  exit 1
fi

ADT_SERVICE=$(aws ecs list-services --cluster "$ADT_CLUSTER" --region "$REGION" $_AWS_PROFILE_FLAG \
  --query 'serviceArns[0]' --output text)
TASKDEF=$(aws ecs describe-services --cluster "$ADT_CLUSTER" --services "$ADT_SERVICE" \
  --region "$REGION" $_AWS_PROFILE_FLAG --query 'services[0].taskDefinition' --output text)

aws ecs describe-task-definition --task-definition "$TASKDEF" --region "$REGION" $_AWS_PROFILE_FLAG \
  --query "taskDefinition" --output json > /tmp/adt_td_links.json

export SITEWISE_I3X_SECRET_ARN HIGHBYTE_I3X_SECRET_ARN
python3 <<'PYEOF'
import json
import os

with open('/tmp/adt_td_links.json') as f:
    td = json.load(f)
for k in ['taskDefinitionArn','revision','status','requiresAttributes','placementConstraints','compatibilities','registeredAt','registeredBy']:
    td.pop(k, None)
# Quick-link URLs + I3X_TLS_VERIFY are resolved from SSM at TASK START
# (ecs.Secret.from_ssm_parameter in adt_compute_stack.py) — no env patching
# needed; the SSM writes above + the service roll below re-point the app.
# Only strip any LEGACY plaintext env entries left by pre-task-start-resolution
# task definitions so they can't shadow the secrets-delivered values.
legacy_env_names = {
    'HIGHBYTE_URL', 'ERP_DASHBOARD_URL', 'SITEWISE_I3X_URL', 'I3X_TLS_VERIFY',
}
secret_updates = {
    'SITEWISE_I3X_TOKEN': os.environ.get('SITEWISE_I3X_SECRET_ARN', ''),
    'HIGHBYTE_I3X_TOKEN': os.environ.get('HIGHBYTE_I3X_SECRET_ARN', ''),
}
for container in td.get('containerDefinitions', []):
    # Always remove legacy plaintext token + quick-link entries, even when no
    # replacement secret currently exists.
    container['environment'] = [
        e for e in container.get('environment', [])
        if e.get('name') not in secret_updates
        and e.get('name') not in legacy_env_names
    ]

    secrets = [s for s in container.get('secrets', []) if s.get('name') not in secret_updates]
    for key, value_from in secret_updates.items():
        if value_from and value_from != 'None':
            secrets.append({'name': key, 'valueFrom': value_from})
    container['secrets'] = secrets
with open('/tmp/adt_td_links_patched.json', 'w') as f:
    json.dump(td, f)
PYEOF

EXEC_ROLE_ARN=$(aws ecs describe-task-definition --task-definition "$TASKDEF" --region "$REGION" \
  $_AWS_PROFILE_FLAG --query 'taskDefinition.executionRoleArn' --output text)
EXEC_ROLE_NAME="${EXEC_ROLE_ARN##*/}"
if [ -n "$EXEC_ROLE_NAME" ] && [ "$EXEC_ROLE_NAME" != "None" ] && \
   { [ -n "$SITEWISE_I3X_SECRET_ARN" ] || [ -n "$HIGHBYTE_I3X_SECRET_ARN" ]; }; then
  python3 <<'PYEOF'
import json
import os

resources = [
    arn for arn in (
        os.environ.get('SITEWISE_I3X_SECRET_ARN', ''),
        os.environ.get('HIGHBYTE_I3X_SECRET_ARN', ''),
    ) if arn and arn != 'None'
]
with open('/tmp/adt_i3x_secret_policy.json', 'w') as f:
    json.dump({
        'Version': '2012-10-17',
        'Statement': [{
            'Effect': 'Allow',
            'Action': ['secretsmanager:GetSecretValue'],
            'Resource': resources,
        }],
    }, f)
PYEOF
  aws iam put-role-policy --role-name "$EXEC_ROLE_NAME" --policy-name ADTAppI3XSecretRead \
    --policy-document file:///tmp/adt_i3x_secret_policy.json $_AWS_PROFILE_FLAG >/dev/null
fi

NEW_TD=$(aws ecs register-task-definition --cli-input-json file:///tmp/adt_td_links_patched.json \
  --region "$REGION" $_AWS_PROFILE_FLAG --query "taskDefinition.taskDefinitionArn" --output text)

aws ecs update-service --cluster "$ADT_CLUSTER" --service "$ADT_SERVICE" \
  --task-definition "$NEW_TD" --force-new-deployment \
  --region "$REGION" $_AWS_PROFILE_FLAG > /dev/null

echo "  ✅ ECS service updated — new task: $NEW_TD"
echo "  ⏳ New container will be running in ~2 minutes"
echo ""
echo "==> Done. Quick-link URLs updated in demo app."
