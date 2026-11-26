#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""place_story_clock.py - place the live workshop story clock (ADR-0089).

Calls ``POST /api/admin/workshop/story/clock`` - the steward-gated route - so
there is no second write path: the server previews (``--dry-run``, nothing is
invoked) or asks the ``erp-story-history`` Lambda to re-plan the story era
onto the new anchor (CAS-guarded on ``--expect-anchor``).

Usage
-----
  export ADT_PASSWORD=...     # the steward login - read from the env ONLY (no flag)
  python3 scripts/place_story_clock.py --base-url https://<dist>.cloudfront.net \\
      --alarm-at 2026-10-01T14:10:00Z --expect-anchor <GET clock.anchor_epoch> [--dry-run]
  python3 scripts/place_story_clock.py ... --alarm-in-minutes 90 --expect-anchor <epoch> --dry-run
  python3 scripts/place_story_clock.py ... --phase bpfo_alarm --elapsed-minutes 10 \\
      --expect-anchor none --force
  python3 scripts/place_story_clock.py ... --reset --expect-anchor <epoch> [--dry-run]
  python3 scripts/place_story_clock.py ... --alarm-in-minutes 10 --keep-history \\
      --expect-anchor <GET clock.live_anchor_epoch> [--dry-run]

``--keep-history`` (ADR-0095) moves ONLY the live clock: the sensor feed, the
live work orders and the alarm follow; the three-day history, its failed batch
and every run number stay as written, so it is safe at any point of the
workshop. Its CAS anchor is ``clock.live_anchor_epoch``. 422 when the new alarm
would land inside the written history (then place the whole clock).

``--reset`` (ADR-0091 Design C, the failsafe) re-plans onto the DEFAULT anchor
the story-history resource recorded (packaged or deploy) instead of a new one;
it takes no placement form. 409 ``no_default_anchor`` when none is stored.

``--expect-anchor none`` skips the compare-and-swap and needs ``--force``
(the route 422s a null anchor without ``force: true``). Read the current anchor
from ``GET /api/admin/workshop/story`` (``clock.anchor_epoch``). Afterwards,
GET until ``clock.telemetry.backfill_complete`` is true.

Exit: 0 the route answered 2xx (report printed); 1 the route failed (HTTP
error printed: 404 not a live workshop, 409 stale anchor or frozen story,
409 another placement in flight or no default to reset to, 422 invalid input, 502 the Lambda failed); 2 usage/login error
(a missing ADT_PASSWORD is a usage error). The password is never printed and a
failed login prints only an HTTP status or exception class, never the reply body.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onboarding_smoketest import App  # noqa: E402  (reuse - do NOT duplicate)

CLOCK_ROUTE = "/api/admin/workshop/story/clock"
LOGIN_ROUTE = "/api/auth/login"


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Place the live workshop story clock. "
                    "The steward password is read from $ADT_PASSWORD only.")
    ap.add_argument("--base-url", default=os.environ.get("ADT_BASE_URL", ""))
    ap.add_argument("--username", default=os.environ.get("ADT_USERNAME", "ADT"))
    ap.add_argument("--alarm-at", default="", help="ISO 8601 BPFO alarm instant WITH a timezone")
    ap.add_argument("--alarm-in-minutes", type=int, default=None,
                    help="BPFO alarm in N whole minutes (5 up to one loop (480 on the 8 h default); the server snaps it up to 5 min)")
    ap.add_argument("--phase", default="", help="degrading | bpfo_alarm | repair | healthy")
    ap.add_argument("--elapsed-minutes", type=float, default=None,
                    help="minutes into --phase at the time of the call")
    ap.add_argument("--reset", action="store_true",
                    help="re-plan onto the stored default anchor (packaged or deploy); no placement form")
    ap.add_argument("--keep-history", action="store_true",
                    help="move only the live clock (sensor feed, live work orders, alarm); the history, "
                         "its failed batch and every run number stay. With --alarm-at or --alarm-in-minutes; "
                         "--expect-anchor is then the LIVE anchor (clock.live_anchor_epoch)")
    ap.add_argument("--expect-anchor", default="",
                    help="the current anchor epoch (CAS), or 'none' to skip the check")
    ap.add_argument("--force", action="store_true",
                    help="required with --expect-anchor none: place without the CAS check")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview the new anchor/alarm; invoke nothing, write nothing")
    return ap


def _expected(raw: str):
    """``none`` -> None; an integer epoch (``123`` or ``123.0``) -> int; else ValueError."""
    if raw.strip().lower() == "none":
        return None
    value = float(raw)
    if not value.is_integer():
        raise ValueError(f"--expect-anchor must be a whole epoch, got {raw!r}")
    return int(value)


def _form(args) -> dict:
    """The one placement form of *args*; ValueError unless exactly one is given."""
    by_phase = bool(args.phase or args.elapsed_minutes is not None)
    if bool(args.alarm_at) + (args.alarm_in_minutes is not None) + by_phase + bool(args.reset) != 1:
        raise ValueError("pass exactly one of --alarm-at, --alarm-in-minutes, "
                         "--phase + --elapsed-minutes or --reset")
    if args.reset:
        return {"reset": True}
    if by_phase and (not args.phase or args.elapsed_minutes is None):
        raise ValueError("--phase and --elapsed-minutes go together")
    if args.alarm_at:
        return {"alarm_at": args.alarm_at}
    if args.alarm_in_minutes is not None:
        return {"alarm_in_minutes": args.alarm_in_minutes}
    return {"phase": args.phase, "elapsed_minutes": args.elapsed_minutes}


def request_body(args) -> dict:
    """The POST body for *args*; ValueError on a usage error."""
    body = _form(args)
    if args.keep_history:
        if "alarm_at" not in body and "alarm_in_minutes" not in body:
            raise ValueError("--keep-history goes with --alarm-at or --alarm-in-minutes only")
        body["keep_history"] = True
    if not args.expect_anchor:
        raise ValueError("--expect-anchor EPOCH|none is required")
    body["expected_anchor_epoch"] = _expected(args.expect_anchor)
    if (body["expected_anchor_epoch"] is None) != bool(args.force):
        raise ValueError("--expect-anchor none requires --force (and --force only goes with none)")
    if args.force:
        body["force"] = True
    body["dry_run"] = bool(args.dry_run)
    return body


def run(app, body: dict) -> tuple[int, dict]:
    """POST the placement; return (exit code, report)."""
    try:
        return 0, app.call("POST", CLOCK_ROUTE, body, timeout=150)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        return 1, {"error": f"HTTP {exc.code}", "body": text[:2000]}


def _login(app, username: str, password: str) -> str | None:
    """None on success, else a credential-safe reason (an HTTP status or an
    exception class) - the login reply body is never echoed."""
    try:
        login = app.call("POST", LOGIN_ROUTE, {"email": username, "password": password})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        code = getattr(exc, "code", None)
        return f"HTTP {code}" if isinstance(code, int) else type(exc).__name__
    return None if isinstance(login, dict) and login.get("status") == "ok" else "rejected"


def main(argv=None, *, app_factory=App) -> int:
    args = _build_arg_parser().parse_args(argv)
    password = os.environ.get("ADT_PASSWORD", "")
    if not args.base_url or not password:
        print("--base-url (or ADT_BASE_URL) and the ADT_PASSWORD env var are required",
              file=sys.stderr)
        return 2
    try:
        body = request_body(args)
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 2
    app = app_factory(args.base_url)
    reason = _login(app, args.username, password)
    if reason:
        print(f"login failed ({reason})", file=sys.stderr)
        return 2
    code, report = run(app, body)
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
