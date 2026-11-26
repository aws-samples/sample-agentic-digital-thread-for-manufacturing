# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 2A (ADR-0058 D1) — M1 wiring: post-provision mining trigger +
on-demand steward route. Offline.

Provision hook (``agents/onboard.py::_run_provision_job``):

- mining fires AFTER the coverage-gap check, scoped to the just-provisioned
  system, fanned out against every OTHER onboarded system;
- a raising miner NEVER fails the provision result (best-effort by
  contract — the coverage-gap boundary);
- the wiring adds NO usage-ledger bump — the miner core bumps ITSELF on file
  (``join_discovery._bump_usage``, ADR-0052); a second bump here would
  double-count (pinned by seam-recording AND a source tripwire).

On-demand route (``agents/learning_routes.py::mine_crosswalks``):

- ``POST /api/admin/learning/mine-crosswalks`` — steward-gated FAIL-CLOSED
  (explicit non-admin ⇒ 403, ABSENT ``is_admin`` verdict ⇒ 403, and the
  mining seam is never reached behind a failed gate);
- body ``{"system_a", "system_b"}`` mines one named pair (BOTH discovery
  directions, ONE value-mining pass — emit-side canonicalization makes A→B
  and B→A the same proposal); ``{}`` mines all onboarded pairs (unordered);
- 400 on a half-named or self pair; run failures come back sanitized
  (exception class only, never the message).

Harness idioms: ``test_coverage_gap_route.py`` (provision-job monkeypatch
seams) + ``test_steward_gate.py`` (stamping middleware for the gate).
No AWS, no Neptune, no network.
"""
from __future__ import annotations

import inspect
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.join_discovery as jd
import agents.learning_routes as lr
import agents.onboard as onboard


# --------------------------------------------------------------------------- #
# Provision-job hook — the test_coverage_gap_route.py seam set
# --------------------------------------------------------------------------- #

@pytest.fixture
def _provision_seams(monkeypatch):
    """Neutralize every provision-job side effect except the hooks under test."""
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", lambda body: {"ok": True})
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_provision_jobs", {})

    import agents.schema_drift as schema_drift
    monkeypatch.setattr(schema_drift, "sweep_resolved_drift",
                        lambda *_a, **_k: None)

    import agents.coverage_gap_analyzer as cga
    monkeypatch.setattr(cga, "run_coverage_gap_check",
                        lambda ids: {"gaps_checked": 0, "systems_checked": 0,
                                     "findings": 0, "filed": 0})
    return types.SimpleNamespace(system_id="sys-new", mappings=[])


def test_provision_job_fires_mining_after_the_coverage_gap_check(
        monkeypatch, _provision_seams):
    """Ordering pin: coverage-gap first, THEN mining — scoped to this system."""
    events: list = []

    import agents.coverage_gap_analyzer as cga
    monkeypatch.setattr(cga, "run_coverage_gap_check",
                        lambda ids: events.append(("gap", ids)) or {})
    monkeypatch.setattr(onboard, "_post_provision_mining",
                        lambda sid: events.append(("mine", sid)) or {})

    onboard._run_provision_job("job-1", _provision_seams)

    assert events == [("gap", ["sys-new"]), ("mine", "sys-new")]
    assert onboard._provision_jobs["job-1"]["status"] == "done"


def test_a_raising_miner_never_fails_the_provision_result(
        monkeypatch, _provision_seams):
    """PINNED (ADR-0058 D1): the never-raise boundary — provisioning stands
    DONE with its result intact even when the miner blows up."""
    def boom(_sid):
        raise RuntimeError("graph unreachable")

    monkeypatch.setattr(onboard, "_post_provision_mining", boom)

    onboard._run_provision_job("job-1", _provision_seams)

    assert onboard._provision_jobs["job-1"]["status"] == "done"
    assert onboard._provision_jobs["job-1"]["result"] == {"ok": True}


def test_mining_fans_out_against_every_other_onboarded_system(monkeypatch):
    """The just-provisioned system is value-mined against each OTHER system
    (never itself) and join discovery runs once for the new system."""
    discovered: list = []
    mined: list = []
    monkeypatch.setattr(jd, "discover_joins_for_system",
                        lambda sid: discovered.append(sid) or
                        {"joins": ["j1"], "crosswalks": []})
    monkeypatch.setattr(jd, "_onboarded_systems",
                        lambda: ["sys-a", "sys-b", "sys-new"])
    monkeypatch.setattr(jd, "mine_value_crosswalks",
                        lambda a, b: mined.append((a, b)) or
                        {"crosswalks": ["v1"], "field_pairs": 2, "truncated": False})

    counts = onboard._post_provision_mining("sys-new")

    assert discovered == ["sys-new"]
    assert mined == [("sys-new", "sys-a"), ("sys-new", "sys-b")]  # never self
    assert counts == {"joins": 1, "crosswalks": 0, "value_crosswalks": 2}


def test_wiring_never_bumps_the_usage_ledger_itself(monkeypatch):
    """No double telemetry (ADR-0052): the miner CORE bumps on file; the
    wiring must not add a second bump. With the miner stubbed out, any
    ledger call observed here could only come from the wiring — assert none."""
    import agents.artifact_usage as artifact_usage
    bumps: list = []
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda refs: bumps.append(refs))
    monkeypatch.setattr(jd, "discover_joins_for_system",
                        lambda sid: {"joins": ["j1"], "crosswalks": ["c1"]})
    monkeypatch.setattr(jd, "_onboarded_systems", lambda: ["sys-a", "sys-new"])
    monkeypatch.setattr(jd, "mine_value_crosswalks",
                        lambda a, b: {"crosswalks": ["v1"], "field_pairs": 1,
                                      "truncated": False})

    onboard._post_provision_mining("sys-new")
    lr._run_mining("sys-new", "sys-a")

    assert bumps == []


def test_source_tripwire_bump_lives_in_the_miner_core_not_the_wiring():
    """Structural pin: the ledger bump appears in the miner core and in
    NEITHER wiring function (a future inline bump would double-count)."""
    assert "_bump_usage" in inspect.getsource(jd.mine_value_crosswalks)
    for fn in (onboard._post_provision_mining, lr._run_mining):
        src = inspect.getsource(fn)
        assert "bump_artifact_usage" not in src
        assert "_bump_usage" not in src


# --------------------------------------------------------------------------- #
# On-demand route — steward gate (fail-closed) + contract
# --------------------------------------------------------------------------- #

def _client(is_admin: "bool | None" = None) -> TestClient:
    """Bare app mounting the real router; a tiny middleware stamps
    ``request.state.is_admin`` the way AuthMiddleware does in production.
    ``is_admin=None`` mounts NO middleware — the attribute is ABSENT, the
    fail-closed posture under test (the test_steward_gate.py idiom)."""
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    app.include_router(lr.router)
    return TestClient(app)


_URL = "/api/admin/learning/mine-crosswalks"


def test_router_lives_under_the_admin_gated_prefix():
    assert lr.router.prefix == "/api/admin/learning"


@pytest.mark.parametrize("is_admin", [False, None], ids=["non-admin", "no-verdict"])
def test_non_admin_is_403_and_mining_never_runs(monkeypatch, is_admin):
    """PINNED: 403 for an explicit non-admin AND for an absent verdict
    (fail-closed) — and the mining seam is never reached behind the gate."""
    def boom(*_a, **_k):
        raise AssertionError("mining must not run behind a failed steward gate")

    monkeypatch.setattr(lr, "_run_mining", boom)

    resp = _client(is_admin=is_admin).post(_URL, json={})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Steward (admin) role required"


def _poll_to_terminal(client, deadline_s: float = 10.0):
    """Drive the event loop until the mining job leaves 'running'."""
    import time as _time
    end = _time.monotonic() + deadline_s
    while _time.monotonic() < end:
        job = client.get(_URL + "/status").json()
        if job.get("state") in ("done", "error"):
            return job
        _time.sleep(0.02)
    raise AssertionError(f"mining job never terminal: {job}")


def _poll_to_done(client):
    job = _poll_to_terminal(client)
    assert job["state"] == "done", job
    return job["result"]


@pytest.fixture(autouse=True)
def _fresh_mining_jobs(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.setattr(lr, "_MINING_JOBS", JobStore("crosswalk-mining", table=None))


def test_named_pair_runs_both_directions_and_one_value_pass(monkeypatch):
    """A named pair runs discovery in BOTH directions but value-mines ONCE —
    emit-side canonicalization makes the reverse pass the same proposal."""
    discover_calls: list = []
    mine_calls: list = []
    monkeypatch.setattr(jd, "discover_pair",
                        lambda a, b: discover_calls.append((a, b)) or
                        {"joins": [f"j-{a}-{b}"], "crosswalks": []})
    monkeypatch.setattr(jd, "mine_value_crosswalks",
                        lambda a, b: mine_calls.append((a, b)) or
                        {"crosswalks": ["v1", "v2"], "field_pairs": 4,
                         "truncated": True})

    client = _client(is_admin=True)
    resp = client.post(_URL, json={"system_a": "sys-a", "system_b": "sys-b"})

    # ASYNC-JOB contract (live-found 2026-08-07: a blocking run 504s at the
    # CloudFront 120 s cap — same fix as reset/import): POST starts the job.
    assert resp.status_code == 200
    assert resp.json() == {"status": "started"}
    body = _poll_to_done(client)
    assert discover_calls == [("sys-a", "sys-b"), ("sys-b", "sys-a")]
    assert mine_calls == [("sys-a", "sys-b")]
    assert body["joins"] == ["j-sys-a-sys-b", "j-sys-b-sys-a"]
    assert body["value_crosswalks"] == ["v1", "v2"]
    assert body["field_pairs"] == 4
    assert body["truncated"] is True


def test_no_body_sweeps_all_onboarded_pairs_unordered(monkeypatch):
    """``{}`` sweeps every UNORDERED onboarded pair once and aggregates —
    ``truncated`` is an OR across pairs."""
    mine_calls: list = []
    per_pair = iter([
        {"crosswalks": ["v1"], "field_pairs": 2, "truncated": False},
        {"crosswalks": [], "field_pairs": 3, "truncated": True},
        {"crosswalks": ["v2"], "field_pairs": 1, "truncated": False},
    ])
    monkeypatch.setattr(jd, "discover_all_joins",
                        lambda: {"joins": ["j1"], "crosswalks": ["c1"]})
    monkeypatch.setattr(jd, "_onboarded_systems",
                        lambda: ["sys-a", "sys-b", "sys-c"])
    monkeypatch.setattr(jd, "mine_value_crosswalks",
                        lambda a, b: mine_calls.append((a, b)) or next(per_pair))

    client = _client(is_admin=True)
    resp = client.post(_URL)

    assert resp.status_code == 200
    assert resp.json() == {"status": "started"}
    body = _poll_to_done(client)
    assert mine_calls == [("sys-a", "sys-b"), ("sys-a", "sys-c"),
                          ("sys-b", "sys-c")]
    assert body["joins"] == ["j1"]
    assert body["crosswalks"] == ["c1"]
    assert body["value_crosswalks"] == ["v1", "v2"]
    assert body["field_pairs"] == 6
    assert body["truncated"] is True


@pytest.mark.parametrize("payload", [
    {"system_a": "sys-a"},
    {"system_b": "sys-b"},
    {"system_a": "sys-a", "system_b": "sys-a"},
], ids=["half-named-a", "half-named-b", "self-pair"])
def test_invalid_pair_bodies_are_400(monkeypatch, payload):
    def boom(*_a, **_k):
        raise AssertionError("mining must not run on an invalid body")

    monkeypatch.setattr(lr, "_run_mining", boom)

    resp = _client(is_admin=True).post(_URL, json=payload)

    assert resp.status_code == 400
    assert "error" in resp.json()


def test_run_failure_surfaces_sanitized_error(monkeypatch):
    """Exception CLASS only — never the message (could carry internals).
    The failure lands in the JOB STATUS (async contract), never an HTTP 500."""
    def boom(a, b):
        raise TimeoutError("host graph.internal unreachable")

    monkeypatch.setattr(lr, "_run_mining", boom)
    client = _client(is_admin=True)

    resp = client.post(_URL, json={})

    assert resp.status_code == 200 and resp.json() == {"status": "started"}
    job = _poll_to_terminal(client)
    assert job["state"] == "error"
    assert "TimeoutError" in job["error"]
    assert "graph.internal" not in str(job)
