#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""reconcile_query_gateway.py — heal STALE query-gateway target ARNs after a deploy.

Thin CLI wrapper over ``_reconcile_query_gateway_lib.reconcile_query_gateway``.
It builds the real ``bedrock-agentcore-control`` client (the library takes an
injected client so its logic stays offline-unit-testable) and reconciles every
per-system target on the QUERY AgentCore Gateway against the CURRENT runtime
ARNs.

Why
---
A per-system agent's ``adt_<id>`` runtime gets a NEW ARN whenever it is recreated
(manual re-provision, ``reonboard.py``, etc.). ``deploy.sh`` re-syncs the
ONBOARDING gateway target but NOT the query gateway targets, so a query target
can be left pointing at a stale/deleted ARN — the live symptom is
``No endpoint or agent found with qualifier 'DEFAULT'`` for exactly the systems
whose runtime id changed. This driver detects that drift and repoints each
affected target at the live ARN (idempotent; a healthy gateway is a no-op).

``deploy.sh`` runs this automatically as a post-deploy step; it is also safe to
run by hand.

Usage
-----
  python3 scripts/reconcile_query_gateway.py \
    --gateway-id <query-gw-id>        # or env AGENTCORE_GATEWAY_ID
    --region <r> [--profile <p>] [--dry-run]

Exit codes: 0 = reconciled (or already in sync); 1 = a fail-loud error (e.g. a
target whose backing runtime no longer exists); 2 = bad invocation.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

# Allow sibling-module imports whether run as a script or imported by tests.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _reconcile_query_gateway_lib import (  # noqa: E402
    QueryGatewayReconcileError,
    reconcile_query_gateway,
)

logger = logging.getLogger("reconcile_query_gateway")


def _control_client(profile: str, region: str):
    import boto3

    session = (
        boto3.Session(profile_name=profile, region_name=region)
        if profile
        else boto3.Session(region_name=region)
    )
    return session.client("bedrock-agentcore-control")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile query-gateway targets against current runtime ARNs"
    )
    ap.add_argument("--gateway-id", default=os.environ.get("AGENTCORE_GATEWAY_ID", ""))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="detect and report drift only; do not update any target",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.gateway_id:
        logger.error("--gateway-id (or AGENTCORE_GATEWAY_ID) is required")
        return 2

    control = _control_client(args.profile, args.region)

    try:
        report = reconcile_query_gateway(
            control, args.gateway_id, args.region, apply=not args.dry_run
        )
    except QueryGatewayReconcileError as exc:
        logger.error("reconcile FAILED: %s: %s", type(exc).__name__, exc)
        return 1
    except Exception as exc:  # noqa: BLE001 — surface, never mask
        logger.error("reconcile FAILED (unexpected): %s: %s", type(exc).__name__, exc)
        return 1

    verb = "would update" if args.dry_run else "updated"
    logger.info(
        "query-gateway reconcile: checked=%d in_sync=%d drifted=%d %s=%d skipped=%d",
        report["checked"],
        report["in_sync"],
        report["drifted"],
        verb,
        report["updated"] if not args.dry_run else report["drifted"],
        report["skipped"],
    )
    for change in report["changes"]:
        logger.info(
            "  %s (%s): %s -> %s",
            change["target"],
            change["system"],
            change["stale_arn"],
            change["current_arn"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
