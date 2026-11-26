#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# verify_deploy.sh — post-deploy health readout for the Agentic Digital Thread.
#
# Checks, with real evidence, that a deployment is actually working:
#   * ADT app reachable through CloudFront
#   * (if HighByte deployed) HighByte UI reachable through CloudFront
#   * (if HighByte deployed) MCP endpoint enforces auth (401) and accepts the
#     stored token (200)
#   * (if HighByte deployed) AWS IoT SiteWise has assets and fresh telemetry
#
# Usage: scripts/verify_deploy.sh --profile <PROFILE> --region <REGION>
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

PASS=0; FAIL=0; SKIP=0
ok()   { echo "  [PASS] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
skip() { echo "  [SKIP] $1"; SKIP=$((SKIP+1)); }

cfn_out() { # stack, output-key
  "${AWS[@]}" cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}
# Workshop deployments nest the engine stacks under a ROOT stack, so the flat
# names above do not exist and the children carry GENERATED names. Fall back to
# the root's own output, then to the nested child resolved through the root's
# resources. Keeps deploy.sh deployments on the exact original path.
ROOT_STACK="${ADT_ROOT_STACK:-ADTWorkshopEngineRoot}"
cfn_out_nested() { # flat-stack, output-key
  local value
  value="$(cfn_out "$1" "$2")"
  if [[ -n "$value" && "$value" != "None" ]]; then echo "$value"; return; fi
  value="$(cfn_out "$ROOT_STACK" "$2")"   # re-surfaced root output
  if [[ -n "$value" && "$value" != "None" ]]; then echo "$value"; return; fi
  local child
  child="$("${AWS[@]}" cloudformation describe-stack-resources \
    --stack-name "$ROOT_STACK" \
    --query "StackResources[?LogicalResourceId=='Nested$1'].PhysicalResourceId" \
    --output text 2>/dev/null)"
  [[ -n "$child" && "$child" != "None" ]] && cfn_out "$child" "$2"
}
http_code() { curl -sS -m 30 -o /dev/null -w "%{http_code}" "$@" 2>/dev/null; }

echo "== Agentic Digital Thread — deploy verification (region $REGION) =="

# --- ADT app -------------------------------------------------------------
echo "ADT app:"
ADT_URL="$(cfn_out_nested ADTComputeStack ADTCloudFrontUrl)"
if [[ -z "$ADT_URL" || "$ADT_URL" == "None" ]]; then
  bad "ADTCloudFrontUrl output not found (looked in ADTComputeStack and $ROOT_STACK)"
else
  CODE="$(http_code "$ADT_URL/login.html")"
  [[ "$CODE" =~ ^(200|302|304)$ ]] && ok "app login page reachable ($ADT_URL -> $CODE)" \
                                    || bad "app login page returned $CODE ($ADT_URL)"
fi

# --- HighByte (optional) -------------------------------------------------
echo "HighByte (optional OT/UNS layer):"
HB_URL="$(cfn_out ADTHighByteOpcuaStack HighByteEndpoint)"
if [[ -z "$HB_URL" || "$HB_URL" == "None" ]]; then
  skip "ADTHighByteOpcuaStack not deployed — skipping OT/UNS checks"
else
  CODE="$(http_code "$HB_URL/ui/")"
  [[ "$CODE" == "200" ]] && ok "HighByte UI reachable ($HB_URL/ui/ -> 200)" \
                          || bad "HighByte UI returned $CODE ($HB_URL/ui/)"

  # MCP must reject anonymous and accept the stored token
  NOAUTH="$(http_code -X POST "$HB_URL/mcp" -H 'Content-Type: application/json' \
            -H 'Accept: application/json, text/event-stream' \
            -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"v","version":"1"}}}')"
  [[ "$NOAUTH" == "401" || "$NOAUTH" == "403" ]] && ok "MCP rejects anonymous access ($NOAUTH)" \
                                                  || bad "MCP did NOT require auth (got $NOAUTH; expected 401/403)"

  # Canonical HighByte MCP token secret is adt/conn/highbyte (JSON {"token": ...}).
  RAW_TOKEN="$("${AWS[@]}" secretsmanager get-secret-value --secret-id adt/conn/highbyte \
           --query SecretString --output text 2>/dev/null)"
  TOKEN="$(printf '%s' "$RAW_TOKEN" | python3 -c 'import sys,json
d=sys.stdin.read().strip()
print(json.loads(d).get("token","") if d.startswith("{") else d)' 2>/dev/null)"
  if [[ -z "$TOKEN" || "$TOKEN" == "None" ]]; then
    skip "MCP token secret not set (adt/conn/highbyte) — see post-deploy §1"
  else
    AUTHED="$(http_code -X POST "$HB_URL/mcp" -H "Authorization: Bearer ${TOKEN#Bearer }" \
              -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
              -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"v","version":"1"}}}')"
    [[ "$AUTHED" == "200" ]] && ok "MCP accepts the stored token (200)" \
                              || bad "MCP rejected the stored token ($AUTHED)"
  fi

  # SiteWise asset count + telemetry freshness
  TOTAL=0
  for MID in $("${AWS[@]}" iotsitewise list-asset-models --query "assetModelSummaries[].id" --output text 2>/dev/null); do
    C="$("${AWS[@]}" iotsitewise list-assets --asset-model-id "$MID" --query "length(assetSummaries)" --output text 2>/dev/null || echo 0)"
    TOTAL=$((TOTAL + C))
  done
  if [[ "$TOTAL" -gt 0 ]]; then
    ok "AWS IoT SiteWise has $TOTAL assets"
    # check newest telemetry on the first asset with a DOUBLE/INTEGER property
    MID="$("${AWS[@]}" iotsitewise list-asset-models --query "assetModelSummaries[0].id" --output text 2>/dev/null)"
    AID="$("${AWS[@]}" iotsitewise list-assets --asset-model-id "$MID" --query "assetSummaries[0].id" --output text 2>/dev/null)"
    PID="$("${AWS[@]}" iotsitewise describe-asset --asset-id "$AID" --query "assetProperties[?length(id)>\`30\`].id | [0]" --output text 2>/dev/null)"
    if [[ -n "$PID" && "$PID" != "None" ]]; then
      TS="$("${AWS[@]}" iotsitewise get-asset-property-value --asset-id "$AID" --property-id "$PID" \
            --query "propertyValue.timestamp.timeInSeconds" --output text 2>/dev/null)"
      NOW="$(date +%s)"
      if [[ "$TS" =~ ^[0-9]+$ ]] && [[ $((NOW - TS)) -lt 120 ]]; then
        ok "fresh telemetry ($(date -u -r "$TS" +%H:%M:%SZ 2>/dev/null || echo "$TS"), $((NOW - TS))s old)"
      else
        bad "telemetry stale or missing (last ts: ${TS:-none}). Trial license expired? Trigger disabled?"
      fi
    else
      skip "no measurement property found to check telemetry freshness"
    fi
  else
    bad "AWS IoT SiteWise has 0 assets — pipeline trigger disabled, license expired, or still warming up"
  fi
fi

echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
