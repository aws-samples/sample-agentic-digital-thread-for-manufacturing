#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""cleanup_fenced_query_context.py — remove proposal rows whose stored
``query_context`` carries a prompt-fence marker.

Background (Demo scan, 2026-08-05): feedback-exploration runs made the
orchestrator file side-effect value_mapping proposals whose ``query_context``
stored the composed exploration prompt — including the untrusted-comment
fence header + origin label (13 rows; all ``concept=unknown``, all pending,
none graph-linked). The write path is fixed (``runtime_app`` suppresses
learning writes on exploration sessions and strips fenced blocks before
storage); this script cleans up the rows already written.

Selection is DELIBERATELY narrow — a row is targeted only when ALL hold:

  * ``query_context`` contains a fence token
    (``untrusted_text.FENCE_TOKEN_PATTERN`` — the ONE shared boundary marker);
  * ``status`` is NOT ``accepted`` (accepted rows are NEVER touched) and NOT
    ``rejected`` (a rejection is a steward claim/tombstone — the machine
    never erases steward evidence);
  * the row carries no ``neptune_node_id`` (graph-linked artifacts are never
    deleted by a table sweep).

Default is a DRY RUN: every candidate is printed (mapping_id + before/after)
and NOTHING is written. Pass ``--apply`` to delete the selected rows.

Usage
-----
  python3 agentic_digital_thread/scripts/cleanup_fenced_query_context.py \\
      [--apply] [--table <name>] [--region <r>] [--profile <p>]

  --apply    actually delete the selected rows (default: dry run, no writes).
  --table    proposals table name (default: $PROPOSED_MAPPINGS_TABLE or
             ``adt-proposed-mappings``).
"""
from __future__ import annotations

import argparse
import os
import re
import sys


def _bootstrap_path() -> None:
    """Make ``agents.*`` imports resolve for standalone runs and unit tests."""
    here = os.path.dirname(os.path.abspath(__file__))
    adt_root = os.path.dirname(here)
    if adt_root not in sys.path:
        sys.path.insert(0, adt_root)


_bootstrap_path()

# Statuses this sweep must NEVER delete: accepted rows are live learned
# artifacts; rejected rows are steward tombstone claims (ADR-0053).
PROTECTED_STATUSES = frozenset({"accepted", "rejected"})

# Fence RESIDUE: the heading line and the ``[source: <label>]`` origin label
# that render_fenced composes around the token. Needed because the 500-char
# query_context cap can truncate the closing/opening token away and leave only
# this residue (measured: 13/13 real polluted rows, 0 of which carry a token).
FENCE_RESIDUE_PATTERN = re.compile(
    r"untrusted steering|\[source:\s*user-feedback:", re.IGNORECASE)


def _scan_all(table) -> "list[dict]":
    """Full paginated scan of the proposals table (read-only)."""
    rows: list[dict] = []
    kwargs: dict = {}
    while True:
        page = table.scan(**kwargs)
        rows.extend(page.get("Items") or [])
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return rows
        kwargs = {"ExclusiveStartKey": last_key}


def fence_marked(row: dict) -> bool:
    """True when ``query_context`` carries fence residue of ANY kind.

    ONE detection home. Matches the fence TOKEN or the HEADING/origin label:
    measured on the Demo table 2026-08-05, the 500-char cap truncates
    mid-origin-label, so all 13 real polluted rows carry the heading and none
    carry a token — a token-only detector selected 0 of 13.
    """
    from agents.untrusted_text import FENCE_TOKEN_PATTERN
    context = str(row.get("query_context") or "")
    return (FENCE_TOKEN_PATTERN.search(context) is not None
            or FENCE_RESIDUE_PATTERN.search(context) is not None)


def is_polluted(row: dict) -> bool:
    """True only for a fence-marked, non-protected, non-graph-linked row."""
    if not fence_marked(row):
        return False
    if str(row.get("status") or "").lower() in PROTECTED_STATUSES:
        return False
    if str(row.get("neptune_node_id") or "").strip():
        return False
    return True


def _describe(row: dict) -> str:
    """One bounded, human-auditable line per candidate row."""
    context_head = str(row.get("query_context") or "")[:120].replace("\n", "\\n")
    return (f"mapping_id={row.get('mapping_id')} "
            f"status={row.get('status')} "
            f"proposed_type={row.get('proposed_type')} "
            f"concept={row.get('concept')} "
            f"provenance={row.get('provenance')} "
            f"query_context[:120]={context_head!r}")


def run_cleanup(table, apply: bool = False, out=print) -> dict:
    """Select fence-polluted rows and (only with ``apply=True``) delete them.

    Prints mapping_id + before/after for every candidate. Returns a summary:
    ``{"scanned", "selected", "deleted", "skipped_protected"}``. Protected
    rows (accepted/rejected/graph-linked) that DO carry a fence marker are
    reported but never touched."""
    rows = _scan_all(table)
    selected: list[dict] = []
    skipped_protected = 0
    for row in rows:
        # Detection lives in ONE place (is_polluted). An inline copy of the
        # token check here would short-circuit before is_polluted ran, so the
        # residue-aware detector never saw the real rows and the sweep
        # selected nothing against a live table.
        if not fence_marked(row):
            continue
        if is_polluted(row):
            selected.append(row)
        else:
            skipped_protected += 1
            out(f"SKIP (protected): {_describe(row)}")
    deleted = 0
    for row in selected:
        out(f"BEFORE: {_describe(row)}")
        if apply:
            table.delete_item(Key={"mapping_id": row["mapping_id"],
                                   "discovered_at": row["discovered_at"]})
            deleted += 1
            out(f"AFTER:  mapping_id={row.get('mapping_id')} DELETED")
        else:
            out(f"AFTER:  mapping_id={row.get('mapping_id')} "
                "WOULD DELETE (dry run — pass --apply)")
    summary = {"scanned": len(rows), "selected": len(selected),
               "deleted": deleted, "skipped_protected": skipped_protected}
    out(f"SUMMARY: {summary} mode={'APPLY' if apply else 'DRY-RUN'}")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove proposal rows whose query_context carries a "
                    "prompt-fence marker (dry run by default).")
    parser.add_argument("--apply", action="store_true",
                        help="delete the selected rows (default: dry run)")
    parser.add_argument("--table", default=os.environ.get(
        "PROPOSED_MAPPINGS_TABLE", "adt-proposed-mappings"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)

    import boto3
    session = (boto3.Session(profile_name=args.profile) if args.profile
               else boto3.Session())
    table = session.resource("dynamodb", region_name=args.region).Table(args.table)
    run_cleanup(table, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
