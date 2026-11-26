#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""normalize_provenance_phrasing.py — one-shot cleanup of session-flavored
provenance phrasing in EXISTING machine-authored proposal rows.

The writer fix (``agents.self_learning._normalize_provenance_phrasing``, applied
at ``log_proposed_mapping``) stops NEW machine-sourced rows from carrying
'prior/previous/earlier/past session(s)' phrasing — factually wrong (the machine
learns through graph rows, not session memory), parroted into answers, and a
smoke memory-reference gate trip (2026-08-04 live diagnosis: 3 pilot-env rows). This
script sweeps rows written BEFORE that fix.

SINGLE SOURCE: the rewrite is the SAME function the writer uses, imported from
``agents.self_learning`` — this script contains no duplicated patterns.

Scope discipline (mirrors the writer):
  * only MACHINE-authored rows (``source != 'user'``) are candidates — steward
    text is never rewritten (ADR-0054);
  * only the ``reason`` and ``analysis`` fields are touched;
  * ``--dry-run`` is the DEFAULT — nothing is written without ``--apply``;
  * every candidate prints its mapping_id with before/after text.

Usage
-----
  python3 scripts/normalize_provenance_phrasing.py \\
      [--table adt-proposed-mappings] [--profile <p>] [--region <r>] [--apply]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "agentic_digital_thread",
    ),
)
from agents.self_learning import (  # noqa: E402 — the writer's own normalizer
    _SESSION_PHRASE_RE,
    _normalize_provenance_phrasing,
)

_FIELDS = ("reason", "analysis")


def _scan_all(table) -> list[dict]:
    """Full paginated scan (the proposals table is small — steward-review scale)."""
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def find_candidates(items: list[dict]) -> list[tuple[dict, dict[str, str]]]:
    """(row, {field: normalized_text}) for machine-authored rows whose reason
    or analysis matches the session patterns. Steward rows (source='user') are
    never candidates, regardless of content."""
    out: list[tuple[dict, dict[str, str]]] = []
    for row in items:
        if row.get("source") == "user":
            continue
        fixes = {
            f: _normalize_provenance_phrasing(row[f])
            for f in _FIELDS
            if row.get(f) and _SESSION_PHRASE_RE.search(row[f])
        }
        if fixes:
            out.append((row, fixes))
    return out


def apply_fix(table, row: dict, fixes: dict[str, str]) -> None:
    """Rewrite only the matched fields; key = (mapping_id, discovered_at)."""
    table.update_item(
        Key={"mapping_id": row["mapping_id"], "discovered_at": row["discovered_at"]},
        UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fixes))),
        ExpressionAttributeNames={f"#f{i}": f for i, f in enumerate(fixes)},
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(fixes.values())},
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--table", default="adt-proposed-mappings")
    p.add_argument("--profile", default=None)
    p.add_argument("--region", default=None)
    p.add_argument("--apply", action="store_true",
                   help="write the normalized text back (default: dry-run)")
    args = p.parse_args(argv)

    import boto3  # deferred: --help must work without AWS config
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = session.resource("dynamodb").Table(args.table)

    candidates = find_candidates(_scan_all(table))
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {len(candidates)} machine-authored row(s) with session phrasing "
          f"in table '{args.table}'")
    for row, fixes in candidates:
        print(f"\nmapping_id: {row['mapping_id']}  "
              f"(source={row.get('source', '')!r}, method={row.get('method', '')!r})")
        for field, after in fixes.items():
            print(f"  {field} before: {row[field]}")
            print(f"  {field} after:  {after}")
        if args.apply:
            apply_fix(table, row, fixes)
            print("  -> written")
    if candidates and not args.apply:
        print("\nDry-run only — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
