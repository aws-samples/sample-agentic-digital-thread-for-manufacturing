#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# dev_neptune.sh — manage a dedicated Neptune Analytics graph for local dev.
#
# CAVEAT (verified): the graph is created PRIVATE (public_connectivity=False),
# matching the CDK. Its data-plane endpoint is only reachable from INSIDE the
# VPC, so a laptop CANNOT query it — Neptune calls fail soft in pure-local dev.
# This script is only useful to a process running in the VPC. See
# docs/adr/0024-neptune-private-graph-not-reachable-locally.md.
#
# The graph settings mirror the CDK graph in stacks/adt_database_stack.py
# (provisioned_memory=16, replica_count=0, public_connectivity=False,
# deletion_protection=False). The local app reaches it via the regional
# neptune-graph ExecuteQuery data-plane API (boto3), so no public endpoint is
# needed — only that GRAPH_IDENTIFIER points at the graph id.
#
# Usage:
#   scripts/dev_neptune.sh create   [--profile P] [--region R] [--name N]
#   scripts/dev_neptune.sh status   [--profile P] [--region R] [--name N]
#   scripts/dev_neptune.sh id       [--profile P] [--region R] [--name N]
#   scripts/dev_neptune.sh destroy  [--profile P] [--region R] [--name N]
#
# After `create`, put the printed graph id into GRAPH_IDENTIFIER in .env.local.
set -euo pipefail

CMD="${1:-}"; shift || true
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROFILE="${AWS_PROFILE:-}"
NAME="adt-localdev"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    --name)    NAME="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

AWS=(aws neptune-graph --region "$REGION")
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

# Resolve a graph id by graph name (empty string if not found).
graph_id_by_name() {
  "${AWS[@]}" list-graphs \
    --query "graphs[?name=='${NAME}'].id | [0]" --output text 2>/dev/null | sed 's/^None$//'
}

case "$CMD" in
  create)
    existing="$(graph_id_by_name)"
    if [[ -n "$existing" ]]; then
      echo "Graph '${NAME}' already exists: ${existing}"
      echo "Set GRAPH_IDENTIFIER=${existing} in .env.local"
      exit 0
    fi
    echo "==> Creating Neptune Analytics graph '${NAME}' in ${REGION} (this takes a few minutes)..."
    gid="$("${AWS[@]}" create-graph \
      --graph-name "$NAME" \
      --provisioned-memory 16 \
      --replica-count 0 \
      --no-public-connectivity \
      --no-deletion-protection \
      --query 'id' --output text)"
    echo "    graph id: ${gid}"
    echo "==> Waiting for graph to become AVAILABLE..."
    "${AWS[@]}" wait graph-available --graph-identifier "$gid" || {
      echo "WARN: 'wait graph-available' not supported by this CLI version — poll 'status' manually." >&2
    }
    echo ""
    echo "Done. Add this to .env.local:"
    echo "    GRAPH_IDENTIFIER=${gid}"
    ;;
  status)
    gid="$(graph_id_by_name)"
    [[ -z "$gid" ]] && { echo "No graph named '${NAME}' in ${REGION}."; exit 1; }
    "${AWS[@]}" get-graph --graph-identifier "$gid" \
      --query '{id:id,name:name,status:status,memory:provisionedMemory}' --output table
    ;;
  id)
    gid="$(graph_id_by_name)"
    [[ -z "$gid" ]] && { echo "No graph named '${NAME}' in ${REGION}." >&2; exit 1; }
    echo "$gid"
    ;;
  destroy)
    gid="$(graph_id_by_name)"
    [[ -z "$gid" ]] && { echo "No graph named '${NAME}' in ${REGION} — nothing to destroy."; exit 0; }
    echo "==> Deleting graph '${NAME}' (${gid}) in ${REGION}..."
    "${AWS[@]}" delete-graph --graph-identifier "$gid" --skip-snapshot \
      --query '{id:id,status:status}' --output table
    echo "Delete initiated. Remember to clear GRAPH_IDENTIFIER in .env.local."
    ;;
  *)
    echo "Usage: $0 {create|status|id|destroy} [--profile P] [--region R] [--name N]" >&2
    exit 2
    ;;
esac
