#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""graph_hygiene.py — run the steward-gated graph repair (incl. ValueMapping
hygiene) against a deployed ADT and print its report.

Calls ``POST /api/neptune/graph/repair`` — the SAME route the admin uses — so
there is no second write path: the server plans with
``agents.graph_hygiene.plan_value_mapping_hygiene`` and (unless ``--dry-run``)
retires malformed learned rows and duplicate generated ``vm-equip-*`` rows,
recreates missing edges and deletes orphans, then reports an exact readback.
``subj-*`` steward rows always survive. In a dry run only the
``value_mappings`` section is exact (the report's ``dry_run_note``).

Usage
-----
  export ADT_PASSWORD=...     # the steward login (kept off the command line)
  python3 scripts/graph_hygiene.py --base-url https://<dist>.cloudfront.net [--dry-run]

The request can outlive the 120 s CloudFront origin timeout (a 504 while the
server keeps working): re-running is idempotent; confirm with ``--dry-run``.

Exit: 0 report printed and ``value_mappings`` verified; 1 the route failed,
the section is missing, or it is not verified (a failed scan — dry run
included — or a readback that did not verify); 2 usage/login error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onboarding_smoketest import App  # noqa: E402  (reuse — do NOT duplicate)

REPAIR_ROUTE = "/api/neptune/graph/repair"


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Steward graph repair + ValueMapping hygiene")
    ap.add_argument("--base-url", default=os.environ.get("ADT_BASE_URL", ""))
    ap.add_argument("--username", default=os.environ.get("ADT_USERNAME", "ADT"))
    ap.add_argument("--password", default=os.environ.get("ADT_PASSWORD", ""))
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only — report what WOULD be retired/repaired, write nothing")
    return ap


def run(app, *, dry_run: bool) -> tuple[int, dict]:
    """Call the repair route; return (exit code, report)."""
    try:
        report = app.call("POST", REPAIR_ROUTE, {"dry_run": dry_run}, timeout=300)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return 1, {"error": f"HTTP {exc.code}", "body": body[:2000]}
    if "error" in report:
        return 1, report
    return (0 if _verified(report.get("value_mappings"), dry_run=dry_run) else 1), report


def _verified(section, *, dry_run: bool) -> bool:
    """The section exists, its scan did not fail, it says ``verified`` and,
    when applied, its readback verified too."""
    if not isinstance(section, dict) or section.get("scan_failed") or not section.get("verified"):
        return False
    readback = section.get("readback")
    return dry_run or readback is None or bool(readback.get("verified"))


def main(argv=None, *, app_factory=App) -> int:
    args = _build_arg_parser().parse_args(argv)
    if not args.base_url or not args.password:
        print("--base-url and --password (or ADT_BASE_URL / ADT_PASSWORD) are required",
              file=sys.stderr)
        return 2
    app = app_factory(args.base_url)
    login = app.call("POST", "/api/auth/login",
                     {"email": args.username, "password": args.password})
    if login.get("status") != "ok":
        # Only the status: the reply body can echo session or credential details.
        print(f"login failed (status {login.get('status')!r})", file=sys.stderr)
        return 2
    code, report = run(app, dry_run=args.dry_run)
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
