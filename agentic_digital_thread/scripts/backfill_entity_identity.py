#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""backfill_entity_identity.py — thin CLI wrapper over ``agents.entity_rescan``.

ADR-0040 (D6). The reusable core (selection, classification reuse, MERGE-only
writes, per-system summary) lives in :mod:`agents.entity_rescan` — the SAME library
the admin API (``POST /api/admin/entities/rescan[/{system_id}]``) calls. This script
only parses CLI args, sets AWS env, and prints the summary; there is NO duplicated
logic here. It remains:

  * ADDITIVE  — only MERGEs new edges/nodes; nothing is ever deleted.
  * IDEMPOTENT — a SchemaEntry that already has an entity edge is never re-selected,
    so re-running is a safe no-op (``entities_written == 0``).

Because the Neptune graph is VPC-private (ADR-0024), this is run manually from inside
the deployed environment; for the deployed app, prefer the admin endpoint.

Usage
-----
  python3 agentic_digital_thread/scripts/backfill_entity_identity.py \
      [--all | --systems <a>,<b>,<c>] \
      [--dry-run] [--region <r>] [--profile <p>]

  --all           rescan every onboarded system missing entity edges (default).
  --systems       restrict to a comma-separated list of system ids (processed in order).
  --dry-run       classify + print what WOULD be written; make no graph writes.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

logger = logging.getLogger("backfill_entity_identity")


def _bootstrap_path() -> None:
    """Add the ``agentic_digital_thread`` root to sys.path so ``agents.*`` imports resolve
    whether this is run as a standalone script or imported by the unit test."""
    here = os.path.dirname(os.path.abspath(__file__))
    adt_root = os.path.dirname(here)  # .../agentic_digital_thread
    if adt_root not in sys.path:
        sys.path.insert(0, adt_root)


_bootstrap_path()

# Re-exported for back-compat (offline unit tests import the pure selectors from
# this module); the implementations live in the library.
from agents.entity_rescan import (  # noqa: E402
    UnknownSystemError,
    rescan_entities,
    restrict_to_systems,
    tables_missing_entity_edge,
)

__all__ = ["tables_missing_entity_edge", "restrict_to_systems",
           "rescan_entities", "UnknownSystemError", "main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--all", action="store_true",
                     help="rescan every onboarded system missing entity edges (default)")
    grp.add_argument("--systems", default="",
                     help="comma-separated system ids to restrict to (processed in order)")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify + print what WOULD be written; make no graph writes")
    parser.add_argument("--region", default="", help="AWS region (sets AWS_DEFAULT_REGION)")
    parser.add_argument("--profile", default="", help="AWS profile (sets AWS_PROFILE)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.region:
        os.environ["AWS_DEFAULT_REGION"] = args.region
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile

    systems = [s.strip() for s in args.systems.split(",") if s.strip()] if args.systems else None
    rc = 0
    if systems:
        # Front-loaded, in the caller's order — one library call per system.
        combined: dict = {"dry_run": args.dry_run, "systems": {}}
        for sid in systems:
            try:
                out = rescan_entities(sid, dry_run=args.dry_run)
            except UnknownSystemError as e:
                logger.error("%s", e)
                rc = 1
                continue
            combined["systems"].update(out.get("systems", {}))
        summary = combined
    else:
        summary = rescan_entities(None, dry_run=args.dry_run)

    logger.info("Rescan complete:\n%s", json.dumps(summary, indent=2, default=str))
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
