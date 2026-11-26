#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# deploy_sitewise.sh — Clean-room SiteWise deployment for HighByte integration
# =============================================================================
#
# This script performs a full clean-room deployment of the SiteWise schema
# for the Agentic Digital Thread sample. It handles:
#   1. Cleanup of any existing SiteWise models/assets
#   2. Model creation (28 models with full measurement schemas + correct dataTypes)
#   3. HighByte config re-import via setup Lambda
#
# Asset creation is handled by the HighByte pipeline (create:true) — this script
# only seeds the model schemas, then the HighByte first pipeline cycle creates
# all 170 assets using the hierarchy-keyed transform in pipeline_sitewise.json.
#
# PREREQUISITES:
#   - AWS CLI credentials configured (via env vars, IAM role, or named profile)
#   - Python 3 with boto3 installed
#   - HighByte ECS service running (for step 3)
#
# USAGE:
#   ./highbyte/deploy_sitewise.sh [--region us-east-1] [--profile <YOUR_PROFILE>]
#   # Profile is optional; if omitted, AWS_PROFILE env var or default chain is used.
#
# TIMING: ~5-10 minutes total (cleanup ~30s, models ~2min, HighByte first cycle ~30s)
# =============================================================================

set -e

REGION="${1:-us-east-1}"
PROFILE="${2:-${AWS_PROFILE:-}}"

# Leading underscore = INTENTIONAL word-splitting (semgrep convention):
# expands to zero words when no profile is set.
_PROFILE_FLAG=""
[ -n "$PROFILE" ] && _PROFILE_FLAG="--profile $PROFILE"

echo "============================================"
echo " SiteWise Clean-Room Deployment"
echo " Region:  $REGION"
echo " Profile: ${PROFILE:-<default credential chain>}"
echo "============================================"
echo ""

# Step 1: Cleanup existing SiteWise resources
echo "=== Step 1/3: Cleaning up existing SiteWise resources ==="
echo "(Disassociates assets, deletes assets, then deletes models in dependency order)"
echo ""
# Reviewed (unquoted-variable-expansion): ${VAR:+...} zero-or-two-word idiom, value quoted.
# nosemgrep
python3 highbyte/cleanup_sitewise.py ${PROFILE:+--profile "$PROFILE"} --region "$REGION"
echo ""
echo "Waiting 5s for SiteWise to stabilize..."
sleep 5

# Step 2: Create models with full measurement schemas
echo ""
echo "=== Step 2/3: Creating SiteWise models ==="
echo "(28 models: 24 process + Line + Area + Site + Enterprise)"
echo ""
# Reviewed (unquoted-variable-expansion): ${VAR:+...} zero-or-two-word idiom, value quoted.
# nosemgrep
python3 highbyte/DeploySitewiseModels.py ${PROFILE:+--profile "$PROFILE"} --region "$REGION"
echo ""
echo "Waiting 5s for models to stabilize..."
sleep 5

# Step 3: Re-import HighByte config
echo ""
echo "=== Step 3/3: Re-importing HighByte configuration ==="
echo "(Invokes setup Lambda to push config with correct region/endpoints)"
echo "After import, reset the HighByte SiteWise connection cache and enable the pipeline."
echo ""
aws lambda invoke \
    --function-name highbyte-setup \
    --payload '{}' \
    --cli-binary-format raw-in-base64-out \
    /tmp/highbyte-setup-output.json \
    $_PROFILE_FLAG \
    --region "$REGION"
echo ""
echo "Setup Lambda invoked successfully."

echo ""
echo "============================================"
echo " DEPLOYMENT COMPLETE"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Open HighByte UI and clear the SiteWise connector cache"
echo "     (Connections → SIteWise → Clear Cache)"
echo "  2. Enable the NameSpace_to_SiteWise pipeline trigger"
echo "     (Pipelines → NameSpace_to_SiteWise → Triggers → Enable)"
echo "  3. Verify data flow in SiteWise console"
echo "     (IoT SiteWise → Assets → AnyCompany → drill into any asset)"
echo ""
echo "Troubleshooting:"
echo "  - If 'does not match model type' errors appear, clear HB cache again"
echo "  - If '_path' errors appear, verify Line model has _path property:"
echo "    aws iotsitewise describe-asset-model --asset-model-id <LINE_MODEL_ID> \\"
echo "      --query 'assetModelProperties[*].name' $_PROFILE_FLAG --region $REGION"
echo ""
