#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# post_deploy.sh — reconcile the ADT app with HighByte after a HighByte deploy.
#
# On deploy, the setup Lambda already (a) sets the HighByte admin password,
# (b) imports the project with the SiteWise trigger enabled, and (c) auto-creates
# the MCP API key into the adt/conn/highbyte secret (JSON
# {"token": ...}, read by the ADT app). This
# script handles what the Lambda can't: the HighByte CloudFront URL changes when
# the stack is recreated, so it refreshes the SSM parameter the ADT app reads and
# restarts the app task to pick up the new URL + token. It then prints the only
# remaining manual steps and runs the health check.
#
# Usage: highbyte/post_deploy.sh --profile <PROFILE> --region <REGION>
set -uo pipefail

PROFILE=""; REGION="us-east-1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --region)  REGION="$2";  shift 2;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done
AWS=(aws --region "$REGION"); [[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HB_URL="$("${AWS[@]}" cloudformation describe-stacks --stack-name ADTHighByteOpcuaStack \
  --query "Stacks[0].Outputs[?OutputKey=='HighByteEndpoint'].OutputValue" --output text 2>/dev/null)"
if [[ -z "$HB_URL" || "$HB_URL" == "None" ]]; then
  echo "ADTHighByteOpcuaStack not deployed (no HighByteEndpoint output). Nothing to do."
  exit 0
fi
echo "==> HighByte endpoint: $HB_URL"

# 1. Refresh the SSM parameter the ADT app reads for the HighByte quick-link / MCP URL.
echo "==> Refreshing /adt/highbyte-url"
"${AWS[@]}" ssm put-parameter --name /adt/highbyte-url \
  --value "$HB_URL" --type String --overwrite >/dev/null
echo "    set to $HB_URL"

# 2. Wait for HighByte to answer through CloudFront (it can take a few minutes).
echo "==> Waiting for HighByte UI to respond (up to ~5 min)..."
for i in $(seq 1 30); do
  CODE="$(curl -sS -m 15 -o /dev/null -w "%{http_code}" "$HB_URL/ui/" 2>/dev/null)"
  [[ "$CODE" == "200" ]] && { echo "    HighByte UI is up (200)"; break; }
  sleep 10
done

# 3. Restart the ADT app task so it re-reads the SSM URL + the MCP token secret.
ADT_CLUSTER="$("${AWS[@]}" ecs list-clusters --query "clusterArns[?contains(@,'ADTComputeStack')] | [0]" --output text 2>/dev/null)"
if [[ -n "$ADT_CLUSTER" && "$ADT_CLUSTER" != "None" ]]; then
  ADT_SVC="$("${AWS[@]}" ecs list-services --cluster "$ADT_CLUSTER" --query "serviceArns[0]" --output text 2>/dev/null)"
  if [[ -n "$ADT_SVC" && "$ADT_SVC" != "None" ]]; then
    echo "==> Restarting ADT app task to pick up the new URL/token"
    "${AWS[@]}" ecs update-service --cluster "$ADT_CLUSTER" --service "$ADT_SVC" \
      --force-new-deployment >/dev/null && echo "    restart triggered"
  fi
fi

cat <<EOF

==> Automatic on deploy: admin password set, project imported, SiteWise trigger
    enabled, and the MCP token created + stored. Remaining manual steps:

    1. Onboard systems in the ADT app  (Architecture -> Onboard) — REQUIRED.
    2. Upload a HighByte license       (HighByte UI -> Settings -> License -> Import)
       — recommended; without it HighByte runs a ~2h trial, then data stops.

==> Verifying deployment health...
EOF

VERIFY="$HERE/../scripts/verify_deploy.sh"
if [[ -x "$VERIFY" ]]; then
  ARGS=(--region "$REGION"); [[ -n "$PROFILE" ]] && ARGS+=(--profile "$PROFILE")
  "$VERIFY" "${ARGS[@]}"
else
  echo "(scripts/verify_deploy.sh not found/executable — skipping health check)"
fi
