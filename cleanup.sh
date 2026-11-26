#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# cleanup.sh — Full clean-room teardown of the Agentic Digital Thread environment
#
# Handles the correct destruction order:
#   1. Delete AgentCore runtimes (they create ENIs that block SG deletion)
#   2. Delete AgentCore gateway targets (gateway can't delete with targets)
#   3. Clean up SiteWise assets and models
#   4. Delete CDK stacks in reverse dependency order
#   5. Clean up orphaned resources (ENIs, security groups)
#   6. Delete SSM parameters
#
# CDKToolkit is NEVER deleted — it's needed for the next deploy.
#
# Usage: ./cleanup.sh [--profile <aws-profile>] [--region <aws-region>]
set -euo pipefail
cd "$(dirname "$0")"

# Parse flags
while [[ $# -gt 0 ]]; do
  case $1 in
    --profile) export AWS_PROFILE="$2"; shift 2 ;;
    --region) export AWS_DEFAULT_REGION="$2"; shift 2 ;;
    *) echo "Usage: ./cleanup.sh [--profile <aws-profile>] [--region <aws-region>]"; exit 1 ;;
  esac
done

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
# Leading underscore = INTENTIONAL word-splitting (semgrep convention for
# unquoted-variable-expansion-in-command): expands to zero words when no
# AWS_PROFILE is set. Array-free on purpose — bash 3.2 + `set -u` safe.
_PROFILE_FLAG=""
[ -n "${AWS_PROFILE:-}" ] && _PROFILE_FLAG="--profile $AWS_PROFILE"

# Verify credentials
ACCOUNT=$(aws sts get-caller-identity $_PROFILE_FLAG --query Account --output text --region "$REGION")
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Agentic Digital Thread Cleanup                             ║"
echo "║  Account: $ACCOUNT   Region: $REGION            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Helpers ──────────────────────────────────────────────────────────────────

# The region a stack lives in: ADTCertificateStack is pinned to us-east-1
# (CloudFront certificates); every other stack is in $REGION.
cfn_region() {
  if [ "$1" = "ADTCertificateStack" ]; then echo "us-east-1"; else echo "$REGION"; fi
}

wait_stack_delete() {
  local STACK="$1"
  local MAX_WAIT="${2:-600}"  # default 10 min
  local ELAPSED=0
  while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    # Use a temp var to distinguish "stack gone" from "API error"
    local RAW_OUTPUT
    RAW_OUTPUT=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$(cfn_region "$STACK")" $_PROFILE_FLAG \
      --query 'Stacks[0].StackStatus' --output text 2>&1)
    local EXIT_CODE=$?
    
    if [ $EXIT_CODE -ne 0 ]; then
      # Check if it's "does not exist" (stack deleted) vs actual API error
      if echo "$RAW_OUTPUT" | grep -q "does not exist"; then
        echo "    $STACK: deleted ✅"
        return 0
      else
        # API error (expired creds, throttle, etc.) — sleep and retry
        echo "    $STACK: API error, retrying..."
        sleep 10
        ELAPSED=$((ELAPSED + 10))
        continue
      fi
    fi
    
    local STATUS="$RAW_OUTPUT"
    case "$STATUS" in
      DELETE_COMPLETE) echo "    $STACK: deleted ✅"; return 0 ;;
      DELETE_FAILED) echo "    $STACK: DELETE_FAILED ❌"; return 1 ;;
      DELETE_IN_PROGRESS) ;;
      *_COMPLETE)
        # The stack is in a terminal non-delete state (e.g. UPDATE_COMPLETE,
        # UPDATE_ROLLBACK_COMPLETE) while we're waiting for it to delete. CFN
        # canceled the delete — almost always because one of its exports is
        # still in use by another stack that hasn't finished deleting yet
        # (HighByte imports the database endpoint; the databases import the
        # VPC). Re-issue the delete; it succeeds once the importer is gone.
        echo "    $STACK: $STATUS — delete canceled (export in use?), re-issuing"
        aws cloudformation delete-stack --stack-name "$STACK" --region "$(cfn_region "$STACK")" $_PROFILE_FLAG 2>/dev/null || true
        ;;
      *) echo "    $STACK: unexpected status $STATUS — keep waiting"; ;;
    esac
    sleep 10
    ELAPSED=$((ELAPSED + 10))
  done
  echo "    $STACK: timed out after ${MAX_WAIT}s (still DELETE_IN_PROGRESS)"
  return 1
}

# Retry a DELETE_FAILED stack by retaining only the resources that genuinely
# could not be deleted (e.g. replicated Lambda@Edge functions). Retaining only
# DELETE_FAILED resources lets CloudFormation delete everything else, so we
# don't needlessly orphan healthy resources (a Cognito user pool, an S3 bucket)
# that simply hadn't been attempted yet because a sibling failed first.
retry_with_retain() {
  local STACK="$1"
  echo "    $STACK stuck — retrying, retaining only the resources that failed to delete..."
  # Leading underscore = INTENTIONAL word-splitting: space-separated logical
  # IDs expanded as separate --retain-resources arguments.
  _FAILED=$(aws cloudformation describe-stack-resources --stack-name "$STACK" --region "$(cfn_region "$STACK")" $_PROFILE_FLAG \
    --query "StackResources[?ResourceStatus=='DELETE_FAILED'].LogicalResourceId" \
    --output text 2>/dev/null | tr '\t' ' ' | tr -s ' ' | sort -u || echo "")
  if [ -n "$_FAILED" ]; then
    echo "    Retaining: $_FAILED"
    aws cloudformation delete-stack --stack-name "$STACK" --region "$(cfn_region "$STACK")" $_PROFILE_FLAG \
      --retain-resources $_FAILED 2>/dev/null || true
    wait_stack_delete "$STACK" 300 || true
  else
    echo "    No DELETE_FAILED resources found — cannot retry with retain"
  fi
}

# ── 1. Delete AgentCore Runtimes ─────────────────────────────────────────────
echo "==> [1/6] Deleting AgentCore runtimes..."
# AgentCore runtimes are created outside CloudFormation: the orchestrator
# (adt_orchestrator) and one per onboarded system (adt_<system_id>). All carry
# the "adt_" prefix, so deletion is scoped to that prefix — this leaves any
# unrelated AgentCore runtimes untouched if the account hosts other projects.
ADT_RUNTIME_PREFIX="adt_"
# legacy pre-rename names: environments deployed before the vf->adt rename
# created runtimes with the "vf_" prefix — keep deleting those too so teardown
# of an older environment still works.
LEGACY_RUNTIME_PREFIX="vf_"
RUNTIME_IDS=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" $_PROFILE_FLAG \
  --query "agentRuntimes[?starts_with(agentRuntimeName, '${ADT_RUNTIME_PREFIX}') || starts_with(agentRuntimeName, '${LEGACY_RUNTIME_PREFIX}')].agentRuntimeId" --output text 2>/dev/null || echo "")
if [ -n "$RUNTIME_IDS" ]; then
  for RT_ID in $RUNTIME_IDS; do
    RT_NAME=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" $_PROFILE_FLAG \
      --query "agentRuntimes[?agentRuntimeId=='$RT_ID'].agentRuntimeName | [0]" --output text 2>/dev/null || echo "$RT_ID")
    echo "    Deleting runtime: $RT_NAME ($RT_ID)"
    aws bedrock-agentcore-control delete-agent-runtime \
      --agent-runtime-id "$RT_ID" --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true
  done
  # Wait for runtimes to fully delete (not ENIs — those release after runtimes are gone).
  # Once list-agent-runtimes returns 0, the runtimes are gone and ENIs will release
  # within a few minutes. Tier 1+2 deletion takes 20+ min which gives ENIs time to clear
  # before ADTNetworkStack needs the subnets.
  echo "    Waiting for runtimes to finish deleting..."
  for _ in $(seq 1 60); do  # up to 10 minutes
    # Count via name list + wc: list-agent-runtimes paginates, and a per-page
    # `length(@)` query emits one number per page (e.g. "1\n0"), which breaks
    # the numeric comparison below. Counting words across the text output is
    # robust to pagination.
    RT_COUNT=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" $_PROFILE_FLAG \
      --query "agentRuntimes[?starts_with(agentRuntimeName, '${ADT_RUNTIME_PREFIX}') || starts_with(agentRuntimeName, '${LEGACY_RUNTIME_PREFIX}')].agentRuntimeName" --output text 2>/dev/null | wc -w | tr -d ' ')
    [ -z "$RT_COUNT" ] && RT_COUNT=0
    if [ "$RT_COUNT" = "0" ]; then
      echo "    All runtimes deleted ✅"
      break
    fi
    echo "    Still waiting... ($RT_COUNT runtimes remaining)"
    sleep 10
  done
  if [ "$RT_COUNT" != "0" ]; then
    echo "    ⚠️  $RT_COUNT runtimes still deleting — proceeding (ENIs may block ADTNetworkStack later)"
  fi
else
  echo "    No runtimes found"
fi

# ── 2. Delete AgentCore Gateway Targets ──────────────────────────────────────
echo "==> [2/6] Deleting AgentCore gateway targets..."
GW_ID=$(aws cloudformation describe-stacks --stack-name ADTAgentCoreStack --region "$REGION" $_PROFILE_FLAG \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" --output text 2>/dev/null || echo "")
if [ -n "$GW_ID" ] && [ "$GW_ID" != "None" ]; then
  TARGETS=$(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "$GW_ID" --region "$REGION" $_PROFILE_FLAG \
    --query 'items[].targetId' --output text 2>/dev/null || echo "")
  if [ -n "$TARGETS" ]; then
    for TID in $TARGETS; do
      echo "    Deleting target: $TID"
      aws bedrock-agentcore-control delete-gateway-target \
        --gateway-identifier "$GW_ID" --target-id "$TID" \
        --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true
    done
    echo "    Waiting 15s for targets to delete..."
    sleep 15
  else
    echo "    No targets found"
  fi
else
  echo "    No gateway found (ADTAgentCoreStack may not exist)"
fi

# Delete ONBOARDING gateway targets too: the onboarding CfnGateway has an
# imperative MCP target created by deploy.sh [2e], and a gateway cannot be
# deleted while it still has targets attached.
ONB_GW_ID=$(aws cloudformation describe-stacks --stack-name ADTOnboardingStack --region "$REGION" $_PROFILE_FLAG \
  --query "Stacks[0].Outputs[?OutputKey=='OnboardingGatewayId'].OutputValue" --output text 2>/dev/null || echo "")
if [ -n "$ONB_GW_ID" ] && [ "$ONB_GW_ID" != "None" ]; then
  ONB_TARGETS=$(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "$ONB_GW_ID" --region "$REGION" $_PROFILE_FLAG \
    --query 'items[].targetId' --output text 2>/dev/null || echo "")
  if [ -n "$ONB_TARGETS" ]; then
    for TID in $ONB_TARGETS; do
      echo "    Deleting onboarding target: $TID"
      aws bedrock-agentcore-control delete-gateway-target \
        --gateway-identifier "$ONB_GW_ID" --target-id "$TID" \
        --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true
    done
    echo "    Waiting 15s for onboarding targets to delete..."
    sleep 15
  else
    echo "    No onboarding targets found"
  fi
else
  echo "    No onboarding gateway found (ADTOnboardingStack may not exist)"
fi

# Delete the AWS Quick gateway's targets too (--quick-api / --quick-connector):
# the app provisions the query-bridge target on adt-quick-gw at startup, outside
# CloudFormation, and that gateway cannot be deleted while it has a target.
QUICK_GW_ID=$(aws cloudformation describe-stacks --stack-name ADTAgentCoreStack --region "$REGION" $_PROFILE_FLAG \
  --query "Stacks[0].Outputs[?OutputKey=='QuickGatewayId'].OutputValue" --output text 2>/dev/null || echo "")
if [ -n "$QUICK_GW_ID" ] && [ "$QUICK_GW_ID" != "None" ]; then
  # Each pass lists the targets and deletes what it finds; only a SUCCESSFUL
  # list of zero ends the loop, so an unreadable list is never taken as empty.
  echo "    Clearing the Quick gateway's targets (up to 2 min)..."
  _QUICK_LEFT="unknown"
  for _ in $(seq 1 "${QUICK_TARGET_POLLS:-24}"); do
    if _QUICK_IDS=$(aws bedrock-agentcore-control list-gateway-targets \
        --gateway-identifier "$QUICK_GW_ID" --region "$REGION" $_PROFILE_FLAG \
        --query 'items[].targetId' --output text 2>/dev/null); then
      _QUICK_IDS=${_QUICK_IDS//None/}
      if [ -z "${_QUICK_IDS//[[:space:]]/}" ]; then
        _QUICK_LEFT="0"
        break
      fi
      _QUICK_LEFT="$_QUICK_IDS"
      for TID in $_QUICK_IDS; do
        echo "    Deleting Quick gateway target: $TID"
        aws bedrock-agentcore-control delete-gateway-target \
          --gateway-identifier "$QUICK_GW_ID" --target-id "$TID" \
          --region "$REGION" $_PROFILE_FLAG > /dev/null 2>&1 || true
      done
    else
      _QUICK_LEFT="unknown"
    fi
    sleep "${QUICK_TARGET_POLL_SECONDS:-5}"
  done
  if [ "$_QUICK_LEFT" = "0" ]; then
    echo "    Quick gateway has no targets"
  else
    echo "    ⚠️  Quick gateway $QUICK_GW_ID still lists targets (${_QUICK_LEFT}); ADTAgentCoreStack's delete may fail. Re-run cleanup.sh."
  fi
fi

# Delete OAuth2 credential provider (stale credentials cause sync failures on redeploy)
echo "    Deleting OAuth2 credential provider..."
aws bedrock-agentcore-control delete-oauth2-credential-provider \
  --name "adt-m2m" --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true

# ── 3. Clean up SiteWise assets and models ───────────────────────────────────
echo "==> [3/6] Cleaning up SiteWise assets and models..."
if [ -f "highbyte/cleanup_sitewise.py" ]; then
  # Reviewed (unquoted-variable-expansion): ${VAR:+...} zero-or-two-word idiom, value quoted.
  # nosemgrep
  python3 highbyte/cleanup_sitewise.py ${AWS_PROFILE:+--profile "$AWS_PROFILE"} --region "$REGION" || \
    echo "    [WARN] SiteWise cleanup had errors (non-fatal)"
else
  echo "    [WARN] highbyte/cleanup_sitewise.py not found — skipping SiteWise cleanup"
fi

# ── 4. Delete CDK stacks in reverse dependency order ─────────────────────────
echo "==> [4/6] Deleting CDK stacks (reverse dependency order)..."

# Every stack app.py can synthesize is in exactly one tier below
# (tests/test_cleanup_stack_tiers.py synthesizes the app with every opt-in and
# fails on a stack missing here). Each tier is deleted only after the one
# before it is gone, so no stack is deleted while a stack that imports its
# exports, or depends on it, is still there.
#
# Tier 0: QuickKioskStack (depends on QuickApiGatewayStack).
TIER0="QuickKioskStack"
# Tier 0b: stacks that depend on tier 1 stacks: QuickApiGatewayStack (VPC Link
# to ADTComputeStack's internal ALB) and ADTSfcStack (ADTHighByteOpcuaStack or
# ADTOpcuaServersStack, ADTSfcS3TablesStack).
TIER0B="QuickApiGatewayStack ADTSfcStack"
# Tier 1: leaf stacks (no other stack depends on them). ADTOnboardingStack is a
# leaf too — ADTComputeStack imports its OnboardingGatewayUrl, so CFN cancels its
# delete until ADTComputeStack is gone; wait_stack_delete re-issues on that
# "export in use" cancel, so both clear within tier 1 before tier 2 (ADTAgentCoreStack).
TIER1="ADTComputeStack ADTOnboardingStack ADTERPDashboardStack ADTERPLambdaStack ADTHighByteOpcuaStack ADTIdentityCenterStack ADTSfcS3TablesStack"
# Tier 2: mid-level stacks. ADTCertificateStack (us-east-1, see cfn_region)
# goes after its consumers (ADTComputeStack, ADTERPDashboardStack,
# ADTHighByteOpcuaStack) in tier 1. ADTOpcuaServersStack (the OPC UA servers in
# their own stack, -c opcua_servers_stack=true with HighByte) goes after its
# readers, ADTSfcStack (tier 0b) and ADTHighByteOpcuaStack (tier 1).
TIER2="ADTDatabaseStack ADTSeedDataStack ADTERPDatabaseStack ADTAgentCoreStack ADTCertificateStack ADTOpcuaServersStack"
# Tier 3: foundation. Outside the workshop it also owns the account's API
# Gateway CloudWatch role setting that every REST API stage logs through, so
# it is deleted only when no other ADT stack is left (see below).
TIER3="ADTNetworkStack"
# ADTCertificateStack is in us-east-1 whatever the deploy Region was. Touch it
# only when this cleanup runs in us-east-1, so a cleanup in another Region
# never deletes or waits on a us-east-1 stack.
if [ "$REGION" != "us-east-1" ]; then
  echo "Skipping ADTCertificateStack: it is in us-east-1 and this cleanup runs in $REGION."
  TIER2="${TIER2/ ADTCertificateStack/}"
fi

stack_status() {  # stack_status <stack>: its status, or DOES_NOT_EXIST
  aws cloudformation describe-stacks --stack-name "$1" --region "$(cfn_region "$1")" $_PROFILE_FLAG \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST"
}

# delete_tier <timeout-secs> <stack>...: delete the tier's stacks in parallel,
# wait for each, then retry any DELETE_FAILED retaining only failed resources.
# After a retry, a stack whose delete CloudFormation kept canceling while the
# retried stack still imported its exports (ADTOnboardingStack behind a
# Lambda@Edge ADTComputeStack) is deleted again before the next tier.
delete_tier() {
  local TIMEOUT="$1"; shift
  local STACK STATUS RETRIED=0
  for STACK in "$@"; do
    STATUS=$(stack_status "$STACK")
    if [ "$STATUS" != "DOES_NOT_EXIST" ] && [ "$STATUS" != "DELETE_COMPLETE" ]; then
      echo "    Deleting $STACK ($STATUS)..."
      aws cloudformation delete-stack --stack-name "$STACK" --region "$(cfn_region "$STACK")" $_PROFILE_FLAG 2>/dev/null || true
    fi
  done
  for STACK in "$@"; do
    STATUS=$(stack_status "$STACK")
    if [ "$STATUS" != "DOES_NOT_EXIST" ] && [ "$STATUS" != "DELETE_COMPLETE" ]; then
      wait_stack_delete "$STACK" "$TIMEOUT" || true
    fi
  done
  for STACK in "$@"; do
    if [ "$(stack_status "$STACK")" = "DELETE_FAILED" ]; then
      retry_with_retain "$STACK"
      RETRIED=1
    fi
  done
  [ "$RETRIED" = "1" ] || return 0
  for STACK in "$@"; do
    STATUS=$(stack_status "$STACK")
    case "$STATUS" in
      DOES_NOT_EXIST|DELETE_COMPLETE|DELETE_FAILED) ;;
      *)
        echo "    Re-issuing the delete of $STACK ($STATUS) after the retry..."
        aws cloudformation delete-stack --stack-name "$STACK" --region "$(cfn_region "$STACK")" $_PROFILE_FLAG 2>/dev/null || true
        wait_stack_delete "$STACK" "$TIMEOUT" || true ;;
    esac
  done
}

# Tier 0 and 0b: the kiosk's NLB and the SFC tasks are quick; 20 min.
delete_tier 1200 $TIER0
delete_tier 1200 $TIER0B
# Tier 1 includes ADTHighByteOpcuaStack, whose CloudFront distribution + VPC
# origins take 20-30 min to tear down. Tier 1 MUST fully complete before
# Tier 2: HighByte imports the database endpoint export, so a database
# delete is canceled if HighByte is still around. Allow 35 min.
delete_tier 2100 $TIER1
# Wait up to 40 min — Neptune graph deletion is the bottleneck and can exceed
# the previous 20 min limit; if it times out mid-delete the script would advance
# to ADTNetworkStack and cancel it (VPC export still in use).
delete_tier 2400 $TIER2

# --- Tier 3: ADTNetworkStack — only after Tier 2 is fully gone ---
# Check ADTNetworkStack status. Use the same never-fails pattern as the tier loops
# (|| echo DOES_NOT_EXIST) so a deleted stack doesn't make the command
# substitution return non-zero and trip `set -e`, which would skip the orphan
# and SSM/secret cleanup below.
NS_STATUS=$(aws cloudformation describe-stacks --stack-name ADTNetworkStack --region "$REGION" $_PROFILE_FLAG \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
# Any other ADT stack still standing (a REST API stage logging through the
# account role ADTNetworkStack owns, or an importer of its VPC exports): keep
# ADTNetworkStack, so its custom resource never resets the account setting
# while a consumer remains.
NS_BLOCKERS=""
for STACK in $TIER0 $TIER0B $TIER1 $TIER2; do
  STATUS=$(stack_status "$STACK")
  if [ "$STATUS" != "DOES_NOT_EXIST" ] && [ "$STATUS" != "DELETE_COMPLETE" ]; then
    NS_BLOCKERS="$NS_BLOCKERS $STACK($STATUS)"
  fi
done
if [ "$NS_STATUS" = "DOES_NOT_EXIST" ] || [ "$NS_STATUS" = "DELETE_COMPLETE" ]; then
  echo "    ADTNetworkStack: already deleted ✅"
elif [ -n "$NS_BLOCKERS" ]; then
  echo "    ⚠  Keeping ADTNetworkStack: other ADT stacks remain:$NS_BLOCKERS"
  echo "        Re-run cleanup.sh once they are gone."
else
  # AgentCore runtime ENIs (interface-type "agentic_ai", ela-attach) are torn
  # down asynchronously by AWS AFTER the runtimes are deleted, and can linger
  # well beyond list-agent-runtimes returning empty. They cannot be
  # force-detached. If they are still attached when ADTNetworkStack is deleted,
  # the subnets/VPC delete fails and leaves an orphaned VPC — which, on a
  # VPC-quota-capped account, blocks the next deploy. So gate the delete on the
  # ENIs actually clearing. Default 45 min; override via CLEANUP_ENI_WAIT_SECS.
  SKIP_NETWORKSTACK=""
  ADT_VPC=$(aws cloudformation describe-stack-resources --stack-name ADTNetworkStack --region "$REGION" $_PROFILE_FLAG \
    --query "StackResources[?ResourceType=='AWS::EC2::VPC'].PhysicalResourceId | [0]" --output text 2>/dev/null || echo "")
  if [ -n "$ADT_VPC" ] && [ "$ADT_VPC" != "None" ]; then
    ENI_WAIT=${CLEANUP_ENI_WAIT_SECS:-2700}
    echo "    Waiting for AgentCore ENIs to release from $ADT_VPC (up to $((ENI_WAIT / 60)) min)..."
    WAITED=0; ENI_COUNT=1
    while [ "$WAITED" -lt "$ENI_WAIT" ]; do
      ENI_COUNT=$(aws ec2 describe-network-interfaces --region "$REGION" $_PROFILE_FLAG \
        --filters "Name=vpc-id,Values=$ADT_VPC" \
        --query "NetworkInterfaces[?InterfaceType=='agentic_ai'].NetworkInterfaceId" --output text 2>/dev/null | wc -w | tr -d ' ')
      [ -z "$ENI_COUNT" ] && ENI_COUNT=0
      [ "$ENI_COUNT" = "0" ] && { echo "    AgentCore ENIs released ✅"; break; }
      echo "    Still waiting... ($ENI_COUNT AgentCore ENI(s) attached)"
      sleep 30; WAITED=$((WAITED + 30))
    done
    if [ "$ENI_COUNT" != "0" ]; then
      SKIP_NETWORKSTACK=1
      echo "    ⚠️  AgentCore ENIs still attached after $((ENI_WAIT / 60)) min — these release on AWS's"
      echo "        async schedule and cannot be force-detached (ela-attach). Skipping ADTNetworkStack"
      echo "        deletion to avoid an orphaned VPC. Re-run cleanup.sh once this returns nothing:"
      echo "        aws ec2 describe-network-interfaces --filters Name=vpc-id,Values=$ADT_VPC \\"
      echo "          --query \"NetworkInterfaces[?InterfaceType=='agentic_ai'].NetworkInterfaceId\""
    fi
  fi
  if [ -z "$SKIP_NETWORKSTACK" ]; then
    echo "    Deleting ADTNetworkStack ($NS_STATUS)..."
    aws cloudformation delete-stack --stack-name ADTNetworkStack --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true
    wait_stack_delete "ADTNetworkStack" 900 || true
  fi
fi

# Handle ADTNetworkStack DELETE_FAILED (subnets/VPC blocked by leftover ENIs)
NS_STATUS=$(aws cloudformation describe-stacks --stack-name ADTNetworkStack --region "$REGION" $_PROFILE_FLAG \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
if [ "$NS_STATUS" = "DELETE_FAILED" ]; then
  retry_with_retain "ADTNetworkStack"
fi

# ── 5. Clean up orphaned resources ──────────────────────────────────────────
echo "==> [5/6] Cleaning up orphaned resources..."

# Delete orphaned security groups (retained from failed stack deletes)
# "VF ECS Service" is a legacy pre-rename SG description — kept so teardown of
# an environment deployed before the vf->adt rename still finds its orphans.
ORPHAN_SGS=$(aws ec2 describe-security-groups --region "$REGION" $_PROFILE_FLAG \
  --filters "Name=description,Values=ADT ECS Service,VF ECS Service,AgentCore Runtime ENIs*" \
  --query 'SecurityGroups[].GroupId' --output text 2>/dev/null || echo "")
for SG in $ORPHAN_SGS; do
  echo "    Deleting orphaned SG: $SG"
  # First try to delete any detached ENIs in this SG
  ENIS=$(aws ec2 describe-network-interfaces --region "$REGION" $_PROFILE_FLAG \
    --filters "Name=group-id,Values=$SG" "Name=status,Values=available" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null || echo "")
  for ENI in $ENIS; do
    aws ec2 delete-network-interface --network-interface-id "$ENI" --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true
  done
  aws ec2 delete-security-group --group-id "$SG" --region "$REGION" $_PROFILE_FLAG 2>/dev/null || \
    echo "    (could not delete $SG — may still have in-use ENIs, will clear eventually)"
done

# Release unassociated Elastic IPs — OFF by default. ADTNetworkStack's NAT gateway
# EIPs are released by CloudFormation when the NAT gateway is deleted, so this
# blanket release is only a defensive backstop. It is account-wide (it cannot
# tell an orphaned ADT EIP from another workload's), so it is gated behind an
# explicit opt-in to stay safe in shared accounts.
if [ -n "${CLEANUP_RELEASE_EIPS:-}" ]; then
  UNASSOC_EIPS=$(aws ec2 describe-addresses --region "$REGION" $_PROFILE_FLAG \
    --query "Addresses[?AssociationId==null].AllocationId" --output text 2>/dev/null || echo "")
  for EIP in $UNASSOC_EIPS; do
    echo "    Releasing unassociated EIP: $EIP"
    aws ec2 release-address --allocation-id "$EIP" --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true
  done
else
  echo "    Skipping unassociated-EIP release (set CLEANUP_RELEASE_EIPS=1 to enable)"
fi

# ── 6. Delete SSM parameters ────────────────────────────────────────────────
echo "==> [6/6] Deleting SSM parameters..."
# --recursive: nested parameters (/adt/demo/seed-db, /adt/demo/erp-db) too.
SSM_PARAMS=$(aws ssm get-parameters-by-path --path "/adt/" --recursive --region "$REGION" $_PROFILE_FLAG \
  --query 'Parameters[].Name' --output text 2>/dev/null || echo "")
if [ -n "$SSM_PARAMS" ]; then
  for PARAM in $SSM_PARAMS; do
    echo "    Deleting: $PARAM"
    aws ssm delete-parameter --name "$PARAM" --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true
  done
else
  echo "    No SSM parameters found"
fi

# Delete agent config secrets from Secrets Manager (stale configs cause authorizer mismatch on redeploy)
echo "    Deleting agent-config secrets from Secrets Manager..."
AGENT_SECRETS=$(aws secretsmanager list-secrets --region "$REGION" $_PROFILE_FLAG \
  --filters "Key=name,Values=adt/agent-config" \
  --query 'SecretList[].Name' --output text 2>/dev/null || echo "")
for SECRET in $AGENT_SECRETS; do
  echo "    Deleting secret: $SECRET"
  aws secretsmanager delete-secret --secret-id "$SECRET" --force-delete-without-recovery \
    --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true
done
# Delete connection secrets (adt/conn/*, incl. the HighByte MCP
# token at adt/conn/highbyte; recreated on next deploy/onboarding)
echo "    Deleting connection secrets (adt/conn/*) from Secrets Manager..."
CONN_SECRETS=$(aws secretsmanager list-secrets --region "$REGION" $_PROFILE_FLAG \
  --filters "Key=name,Values=adt/conn" \
  --query 'SecretList[].Name' --output text 2>/dev/null || echo "")
for SECRET in $CONN_SECRETS; do
  echo "    Deleting secret: $SECRET"
  aws secretsmanager delete-secret --secret-id "$SECRET" --force-delete-without-recovery \
    --region "$REGION" $_PROFILE_FLAG 2>/dev/null || true
done

# ── 7. Done ─────────────────────────────────────────────────────────────────
echo "==> [6/6] Cleanup steps complete."

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Cleanup complete                                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "    Region: $REGION"
echo "    Account: $ACCOUNT"

# Final check — only ADT's own stacks (every tier, each in its own region; a
# stack in any state but deleted counts), so unrelated stacks in a
# shared account don't trigger a spurious "remaining" warning. CDKToolkit is
# always kept and is never in these tiers.
ADT_STACKS="$TIER0 $TIER0B $TIER1 $TIER2 $TIER3"
REMAINING=""
for s in $ADT_STACKS; do
  STATUS=$(stack_status "$s")
  if [ "$STATUS" != "DOES_NOT_EXIST" ] && [ "$STATUS" != "DELETE_COMPLETE" ]; then
    REMAINING="$REMAINING $s"
  fi
done
REMAINING=$(echo "$REMAINING" | xargs 2>/dev/null || true)
if [ -n "$REMAINING" ]; then
  echo ""
  echo "    ⚠️  Remaining stacks: $REMAINING"
  echo "    Run cleanup again or investigate manually."
else
  echo "    All stacks deleted ✅ (CDKToolkit retained for next deploy)"
fi

# Left on purpose (non-workshop deploys): one IAM role, the API Gateway
# CloudWatch Logs role ADTNetworkStack created for this account and Region
# (stacks/_apigw_log_role.py). The account's API Gateway setting may name it,
# and other APIs in the account may log through it, so it is retained.
_APIGW_ROLES=$(aws iam list-roles --path-prefix /adt/apigateway-cloudwatch/ $_PROFILE_FLAG \
  --query "Roles[?starts_with(RoleName, 'ADTNetworkStack-ApiGatewayAccountLogRole')].RoleName" \
  --output text 2>/dev/null || echo "")
if [ -n "$_APIGW_ROLES" ] && [ "$_APIGW_ROLES" != "None" ]; then
  echo ""
  echo "    Kept on purpose: IAM role(s) $_APIGW_ROLES"
  echo "    (path /adt/apigateway-cloudwatch/), the API Gateway CloudWatch Logs role the"
  echo "    account setting may name. Delete it only once no API Gateway API in this"
  echo "    account and Region logs to CloudWatch:"
  echo "      aws apigateway get-account --region $REGION   # cloudwatchRoleArn"
  echo "      aws iam detach-role-policy --role-name <name> \\"
  echo "        --policy-arn arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
  echo "      aws iam delete-role --role-name <name>"
fi
