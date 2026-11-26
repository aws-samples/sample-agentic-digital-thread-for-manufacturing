#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# mint-query-token.sh: mint a short-lived bearer JWT for an ADT AgentCore gateway (CUSTOM_JWT).
#
# Local dev helper. Discovers the adt-app Cognito pool, the named
# client_credentials client, the hosted-UI domain and the named gateway from
# the account, so it keeps working after a redeploy changes the generated IDs.
#
# Usage:
#   AWS_PROFILE=<your-profile> ./scripts/mint-query-token.sh            # prints "Authorization: Bearer ..."
#   ./scripts/mint-query-token.sh --header                                  # same, header line only
#   ./scripts/mint-query-token.sh --json                                    # raw token endpoint JSON
#   ./scripts/mint-query-token.sh --quick [--json]                          # Quick's own token for adt-quick-gw
#
# By default the token is the app's own adt-app-service-v2 token (scope
# adt/query) for the shared gateway adt-query-gw: row (a) of
# docs/quick-integration.md. --quick mints Quick's own adt-quick-service token
# (scope adt-quick/bridge) for adt-quick-gw, row (b), the credentials the
# Quick connector holds. The gateway's MCP URL is printed on stderr.
#
# Env overrides: AWS_PROFILE, AWS_REGION, POOL_NAME (default adt-app), and
# CLIENT_NAME, SCOPE, GATEWAY_NAME (defaults adt-app-service-v2, adt/query,
# adt-query-gw; with --quick adt-quick-service, adt-quick/bridge, adt-quick-gw).
# The original adt-app-service client is retired: no gateway accepts it.
set -euo pipefail

MODE="--header"
TARGET="query"
for arg in "$@"; do
  case "$arg" in
    --header|--json) MODE="$arg" ;;
    --quick) TARGET="quick" ;;
    *) echo "unknown argument: $arg (use --header | --json | --quick)" >&2; exit 2 ;;
  esac
done

PROFILE="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-east-1}"
POOL_NAME="${POOL_NAME:-adt-app}"
if [ "$TARGET" = "quick" ]; then
  CLIENT_NAME="${CLIENT_NAME:-adt-quick-service}"
  SCOPE="${SCOPE:-adt-quick/bridge}"
  GATEWAY_NAME="${GATEWAY_NAME:-adt-quick-gw}"
else
  CLIENT_NAME="${CLIENT_NAME:-adt-app-service-v2}"
  SCOPE="${SCOPE:-adt/query}"
  GATEWAY_NAME="${GATEWAY_NAME:-adt-query-gw}"
fi

aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

POOL_ID=$(aws_ cognito-idp list-user-pools --max-results 60 \
  --query "UserPools[?Name=='$POOL_NAME'].Id | [0]" --output text)
[ "$POOL_ID" = "None" ] || [ -z "$POOL_ID" ] && { echo "pool '$POOL_NAME' not found (profile=$PROFILE)" >&2; exit 1; }

# The client by NAME, not "the first client_credentials client": a deploy with
# the Quick gateway also has Quick's own adt-quick-service client, which only
# adt-quick-gw accepts.
CLIENT=$(aws_ cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --max-results 60 \
  --query "UserPoolClients[?ClientName=='$CLIENT_NAME'].ClientId | [0]" --output text)
[ "$CLIENT" = "None" ] || [ -z "$CLIENT" ] && { echo "client '$CLIENT_NAME' not found in pool $POOL_ID" >&2; exit 1; }

SECRET=$(aws_ cognito-idp describe-user-pool-client --user-pool-id "$POOL_ID" --client-id "$CLIENT" \
  --query 'UserPoolClient.ClientSecret' --output text)
DOMAIN=$(aws_ cognito-idp describe-user-pool --user-pool-id "$POOL_ID" \
  --query 'UserPool.Domain' --output text)
TOKEN_URL="https://$DOMAIN.auth.$REGION.amazoncognito.com/oauth2/token"

# Secret rides STDIN into curl (--data-urlencode client_secret@-), never argv:
# argv is visible in `ps` for the life of the process.
RESP=$(printf '%s' "$SECRET" | curl -s -X POST "$TOKEN_URL" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d grant_type=client_credentials -d "client_id=$CLIENT" \
  --data-urlencode client_secret@- -d "scope=$SCOPE")

GW=$(aws_ bedrock-agentcore-control list-gateways \
  --query "items[?name=='$GATEWAY_NAME'].gatewayId | [0]" --output text 2>/dev/null || echo "")
if [ -n "$GW" ] && [ "$GW" != "None" ]; then
  echo "# Gateway MCP URL: https://$GW.gateway.bedrock-agentcore.$REGION.amazonaws.com/mcp" >&2
else
  echo "# gateway '$GATEWAY_NAME' not found (profile=$PROFILE); the token is still printed" >&2
fi

case "$MODE" in
  --json) echo "$RESP" ;;
  *)
    TOKEN=$(printf '%s' "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
    echo "# token valid ~1h; client=$CLIENT scope=$SCOPE gateway=$GATEWAY_NAME" >&2
    echo "Authorization: Bearer $TOKEN"
    ;;
esac
