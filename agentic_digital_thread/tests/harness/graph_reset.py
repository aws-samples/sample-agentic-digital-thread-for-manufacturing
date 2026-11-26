# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gated graph reset/reseed + read-only verify for the S0.2 harness.

Two operations, one CLI:

``reset_graph`` is DESTRUCTIVE. It empties the onboarding artifacts from the
dev Neptune graph and reseeds the core concept vocabulary. Because it writes,
it calls the :mod:`tests.harness._guard` safety guards *first* — opt-in, then
dev-account, then a real target graph — and only ever touches the graph after
all three pass. Two modes:

  * ``mode="endpoint"`` (default): log in with the app's cookie auth and POST
    ``{ADT_BASE_URL}/api/onboard/reset`` — the same server handler the "Reset"
    button uses (``agents.onboard.onboard_reset``), which wipes the onboarding
    labels and reseeds ``CORE_CONCEPT_DEFINITIONS``, and also cleans up the
    AgentCore runtimes/targets.
  * ``mode="direct"`` (fallback for when the app is unreachable): replay the
    exact wipe ``onboard_reset`` performs — ``DETACH DELETE`` per onboarding
    label — then re-MERGE the core concepts from ``catalog.core_concept_merges()``
    (the same single seed source ``deploy.sh`` step [2/6] and ``onboard_reset``
    now use, ADR 0020). Uses ``agents.db.neptune_write`` only — no bespoke
    Neptune client.

``verify_graph`` is NON-destructive. It delegates to the existing read-only
shape audit (:func:`tests.audit.audit_onboarding_graph.run_audit`) and returns
its report; the CLI exits non-zero only when ``status == "fail"``.

CLI::

    python -m tests.harness.graph_reset reset  [--mode endpoint|direct] [--yes]
    python -m tests.harness.graph_reset verify [--system-id cmms]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from agents.catalog import core_concept_merges
from agents.db import neptune_write
from tests.audit import audit_onboarding_graph
from tests.harness import _guard

logger = logging.getLogger(__name__)

# The EXACT onboarding label list wiped by agents.onboard.onboard_reset
# (onboard.py lines 42-43). Direct mode replays this verbatim so the two modes
# converge on the same emptied graph. 'Concept' is wiped here and reseeded after.
ONBOARDING_LABELS = (
    "OnboardingLog", "DecisionBoundary", "AuthorityRule", "Correlation",
    "ValueMapping", "JoinPath", "IdentityRule", "Field", "SchemaEntry",
    "System", "Plant", "Concept",
)

LOGIN_PATH = "/api/auth/login"
#: The synchronous reset (every release has it): answers when the reset has ended.
RESET_PATH = "/api/onboard/reset"
#: The background reset job: started here, its status polled at RESET_JOB_PATH/<job id>.
#: An app of the previous release answers it 405 (or 404); the harness then
#: falls back to RESET_PATH.
RESET_JOB_PATH = "/api/onboard/reset-all"
DEFAULT_USERNAME = "ADT"
#: Per request: the job start answers once the job is started, each poll at once.
HTTP_TIMEOUT = 60.0
#: The synchronous fallback waits for the AgentCore sweep (runtimes then
#: targets, 2 x 180 s) before it answers.
SYNC_RESET_TIMEOUT = 420.0
#: How long the job is polled before giving up (its AgentCore sweep alone can
#: take a few minutes), and how often.
RESET_JOB_DEADLINE_S = 1800.0
RESET_POLL_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Reset (DESTRUCTIVE — guarded)
# ---------------------------------------------------------------------------

def reset_graph(base_url: str | None = None, mode: str = "endpoint", yes: bool = False) -> dict:
    """Empty + reseed the dev graph. Guards run FIRST; writes only after they pass.

    Order is deliberate: ``require_opt_in`` (cheapest, no AWS) -> ``assert_dev_account``
    (STS identity) -> ``assert_target_graph`` (real graph id). Any failure raises
    :class:`tests.harness._guard.GuardError` *before* a single write is attempted.
    """
    _guard.require_opt_in(yes)
    _guard.assert_dev_account()
    _guard.assert_target_graph()

    if mode == "endpoint":
        return _reset_via_endpoint(base_url)
    if mode == "direct":
        return _reset_via_direct()
    raise ValueError(f"unknown reset mode {mode!r} (expected 'endpoint' or 'direct')")


def _reset_via_endpoint(base_url: str | None) -> dict:
    """POST the app's own reset handler using cookie auth (no new Neptune
    client), then poll its background job until it ends."""
    import httpx

    url = (base_url or os.environ.get("ADT_BASE_URL", "")).rstrip("/")
    if not url:
        raise _guard.GuardError(
            "endpoint mode needs ADT_BASE_URL — run capture_env and source .harness-env first."
        )
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        _login(client, url)
        resp = client.post(f"{url}{RESET_JOB_PATH}")
        path = RESET_JOB_PATH
        if getattr(resp, "status_code", None) in (404, 405):
            # An app of the previous release: no job route; the synchronous reset.
            path = RESET_PATH
            resp = client.post(f"{url}{RESET_PATH}", timeout=SYNC_RESET_TIMEOUT)
        if getattr(resp, "status_code", None) == 503:
            # Retryable: the reset did not start; nothing was wiped (server contract).
            raise RuntimeError(f"reset incomplete, retry: {_error_detail(resp)}")
        resp.raise_for_status()
        payload = (_await_reset_job(client, url, resp.json()) if path == RESET_JOB_PATH
                   else resp.json())
    logger.info("graph reset via endpoint mode (POST %s%s)", url, path)
    payload.setdefault("mode", "endpoint")
    return payload


#: Status answers that say nothing about the job: a task of the previous
#: release has no status route (404, or 405), and the status route answers 503
#: when it cannot read the job right now. The job runs on: ask again.
_STATUS_UNKNOWN = (404, 405, 503)


def _await_reset_job(client, url: str, started: dict) -> dict:
    """Poll the Reset All job *started* names until it is done: its result.
    During a rolling deploy a poll can reach a task of the previous release;
    its answer (and the status route's own 503) is asked again until
    :data:`RESET_JOB_DEADLINE_S`, never answered by starting the synchronous
    reset: the job exists, and a second reset must not start. A job that
    ended in error raises (``reset incomplete, retry`` when the server says it
    is retryable); so does one that cannot be found or that outlives the
    deadline."""
    job_id = str((started or {}).get("job_id") or "")
    if not job_id:
        raise RuntimeError(f"the reset did not start a job: {started!r}")
    deadline = time.monotonic() + RESET_JOB_DEADLINE_S
    while time.monotonic() < deadline:
        resp = client.get(f"{url}{RESET_JOB_PATH}/{job_id}")
        if getattr(resp, "status_code", None) in _STATUS_UNKNOWN:
            time.sleep(RESET_POLL_SECONDS)
            continue
        resp.raise_for_status()
        status = resp.json()
        state = status.get("state")
        if state == "done":
            return dict(status.get("result") or {"status": "reset"})
        if state == "error":
            retry = status.get("reason") in ("reset_incomplete", "reset_lease_lost",
                                             "reset_stopped")
            raise RuntimeError(("reset incomplete, retry: " if retry else "reset failed: ")
                               + str(status.get("error", "")))
        if state != "running":
            raise RuntimeError(f"the reset job {job_id} was not found (state {state!r})")
        time.sleep(RESET_POLL_SECONDS)
    raise RuntimeError(f"the reset job {job_id} did not finish within {RESET_JOB_DEADLINE_S:g} s")


def _error_detail(resp) -> str:
    """The server's ``detail`` for an error response; raw text if not JSON."""
    try:
        return str(resp.json().get("detail", ""))
    except ValueError:
        return str(getattr(resp, "text", ""))


def _login(client, url: str) -> None:
    """Perform the app's form login so the session cookie protects the reset POST."""
    password = os.environ.get("ADT_PASSWORD")
    if not password:
        logger.warning("ADT_PASSWORD unset — attempting reset without auth (may 401)")
        return
    username = os.environ.get("ADT_USERNAME", DEFAULT_USERNAME)
    resp = client.post(f"{url}{LOGIN_PATH}", json={"email": username, "password": password})
    resp.raise_for_status()


def _reset_via_direct() -> dict:
    """Fallback wipe+reseed via neptune_write, mirroring onboard_reset + deploy.sh [2/6]."""
    for label in ONBOARDING_LABELS:
        neptune_write(f"MATCH (n:{label}) DETACH DELETE n")
    seeded = _reseed_core_concepts()
    logger.info(
        "graph reset via direct mode (%d labels wiped, %d core statements reseeded)",
        len(ONBOARDING_LABELS), seeded,
    )
    return {
        "status": "reset",
        "mode": "direct",
        "labels_cleared": list(ONBOARDING_LABELS),
        "concepts_seeded": seeded,
    }


def _reseed_core_concepts() -> int:
    """Re-MERGE the core concept vocabulary from catalog.core_concept_merges() (ADR 0020).

    Same single seed source as onboard_reset and deploy.sh [2/6] — every concept carries
    name + isa95_level + source='core' + description.
    """
    seeded = 0
    for stmt, params in core_concept_merges():
        if neptune_write(stmt, params):
            seeded += 1
    return seeded


# ---------------------------------------------------------------------------
# Verify (NON-destructive — delegates to the read-only audit)
# ---------------------------------------------------------------------------

def verify_graph(system_id: str | None = None) -> dict:
    """Run the read-only onboarding-graph shape audit and return its report dict."""
    return audit_onboarding_graph.run_audit(system_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tests.harness.graph_reset",
        description="Gated graph reset/reseed (destructive) and read-only verify.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    reset = sub.add_parser("reset", help="DESTRUCTIVE: empty + reseed the dev graph (guarded).")
    reset.add_argument("--mode", choices=("endpoint", "direct"), default="endpoint",
                       help="endpoint: POST the app reset; direct: neptune_write fallback.")
    reset.add_argument("--yes", action="store_true",
                       help="Opt in to the destructive op (or set ADT_HARNESS_ALLOW_DESTRUCTIVE=1).")

    verify = sub.add_parser("verify", help="Read-only graph-shape audit (non-destructive).")
    verify.add_argument("--system-id", dest="system_id", default=None,
                        help="System id to verify (default: auto-pick a system with MAPS_TO).")
    return parser.parse_args(argv)


def _run_reset(args: argparse.Namespace) -> int:
    try:
        payload = reset_graph(mode=args.mode, yes=args.yes)
    except _guard.GuardError as exc:
        print(f"ERROR (guard): {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — surface app/network errors without a traceback
        print(f"ERROR: reset failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, default=str))
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    report = verify_graph(args.system_id)  # run_audit already prints the report
    return 1 if report.get("status") == "fail" else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "reset":
        return _run_reset(args)
    return _run_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
