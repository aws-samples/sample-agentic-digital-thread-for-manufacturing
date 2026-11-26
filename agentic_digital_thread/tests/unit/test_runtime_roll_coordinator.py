# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""T1.5 (G2) — the batched serial runtime-roll coordinator
(``AgentManager.refresh_systems_batched``) + the pinned no-roll delivery rule.

A steward text edit must be INCAPABLE of taking the data plane down. Pins:

1. **Dedupe** — N changed artifacts touching one system ⇒ exactly ONE
   ``update_agent_runtime`` call for that system per operation.
2. **Serial ordering** — system B's roll is NOT issued until system A's
   runtime reports READY again (call ORDER asserted, not just counts):
   never two system runtimes down simultaneously.
3. **Warm-up verification** — after READY, ONE MCP session-init probe;
   the result surfaces as ``ready_verified`` / ``ready_unverified``
   (ADR-0056's lesson: every bricked runtime reported READY).
4. **Tri-state preserved** (``ok/failed/skipped``, ADR-0054 D4) — incl. a
   refresh failure never rolling back the durable graph write (route-level).
5. **Bounded operation** — the overall timeout yields a clear partial
   report, never an indefinite hang.
6. **The no-roll delivery rule** — per-turn/per-session artifact classes
   (query guidance / graph artifacts / domain notes) trigger ZERO runtime
   updates; only secret-delivered payloads roll. Pinned as a TABLE
   (``artifact_class_requires_roll``), not call-site judgement, so T2.2
   inherits it.

All AWS/graph access is stubbed (the same injected-fake seam as
``test_agent_manager_business_context.py``) — no sockets, no real boto3.
"""
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agent_manager import (  # noqa: E402
    NO_ROLL_ARTIFACT_CLASSES,
    ROLL_REQUIRED_ARTIFACT_CLASSES,
    AgentManager,
    artifact_class_requires_roll,
)


# --------------------------------------------------------------------------- #
# Fakes (seam mirrors test_agent_manager_business_context.py)
# --------------------------------------------------------------------------- #

_SIBLINGS = {"system_prompt": "You are the ERP agent.",
             "tables_desc": "orders(id: int)"}
_ENABLED_STATE = {"business_context": "Plant 2 runs three shifts.",
                  "enabled": True, "updated_at": "2026-07-29T00:00:00Z",
                  "context_hash": "abc123def456"}


class _FakeSecrets:
    class ResourceNotFoundException(Exception):
        pass

    def __init__(self, current: dict | None = None):
        self._current = current
        self.put_calls = []
        self.exceptions = types.SimpleNamespace(
            ResourceNotFoundException=self.ResourceNotFoundException)

    def get_secret_value(self, SecretId=None):  # noqa: N803 - boto3 kwarg
        if self._current is None:
            raise self.ResourceNotFoundException(SecretId)
        return {"SecretString": json.dumps(self._current)}

    def put_secret_value(self, SecretId=None, SecretString=None):  # noqa: N803
        self.put_calls.append({"SecretId": SecretId, "SecretString": SecretString})
        return {}


class _FakeControl:
    """bedrock-agentcore-control stand-in with a SCRIPTABLE readiness
    sequence and a shared event log (for call-ORDER assertions).

    ``not_ready_polls`` = per-runtime-name count of readiness list calls that
    report the runtime NOT READY before it flips READY (models the roll's
    UPDATING window)."""

    def __init__(self, system_ids=("erp",), events: list | None = None,
                 not_ready_polls: dict | None = None, update_error: bool = False):
        self._names = {f"adt_{sid}": sid for sid in system_ids}
        self.events = events if events is not None else []
        self._not_ready_polls = dict(not_ready_polls or {})
        self._update_error = update_error
        self.update_calls = []

    def _status_for(self, name: str) -> str:
        left = self._not_ready_polls.get(name, 0)
        if left > 0:
            self._not_ready_polls[name] = left - 1
            return "UPDATING"
        return "READY"

    def list_agent_runtimes(self, **kwargs):
        self.events.append(("poll",))
        return {"agentRuntimes": [
            {"agentRuntimeName": name, "agentRuntimeId": f"rt-{sid}",
             "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/rt-{sid}",
             "status": self._status_for(name)}
            for name, sid in self._names.items()
        ]}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        return {
            "status": "READY",
            "environmentVariables": {"AWS_REGION": "us-east-1"},
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "uri:1"}},
            "roleArn": "arn:role",
            "protocolConfiguration": {"serverProtocol": "MCP"},
            "networkConfiguration": {"networkMode": "VPC",
                                     "networkModeConfig": {"subnets": ["s-1"],
                                                           "securityGroups": ["sg-1"]}},
        }

    def update_agent_runtime(self, **kwargs):
        if self._update_error:
            raise RuntimeError("ValidationException: update failed")
        self.update_calls.append(kwargs)
        self.events.append(("update", kwargs.get("agentRuntimeId")))
        return {}


def _fake_boto3(control, secrets=None):
    def _client(service, region_name=None, **kw):
        if service == "bedrock-agentcore-control":
            return control
        if service == "secretsmanager" and secrets is not None:
            return secrets
        raise AssertionError(f"unexpected boto3 client: {service}")
    return types.SimpleNamespace(client=_client)


def _forbidden_boto3():
    guard = MagicMock(side_effect=AssertionError("boto3 must not be used"))
    return types.SimpleNamespace(client=guard), guard


class _FakeTime:
    """Deterministic clock: each monotonic() call advances *step* 'seconds'."""

    def __init__(self, step: float = 60.0):
        self.now = 0.0
        self.step = step

    def monotonic(self) -> float:
        current = self.now
        self.now += self.step
        return current

    def sleep(self, _seconds) -> None:
        pass


def _mgr() -> AgentManager:
    mgr = AgentManager(mode="cloud", gateway_id="", gateway_url="https://gw/mcp",
                       ecr_image_uri="uri:1", runtime_role_arn="arn:role")
    mgr.READY_POLL_SECONDS = 0  # no real sleeping in unit tests
    return mgr


def _patch_graph_state(state):
    return patch("agents.business_context.read_business_context",
                 lambda system_id: dict(state))


# --------------------------------------------------------------------------- #
# 1. Dedupe — one update_agent_runtime per system per operation
# --------------------------------------------------------------------------- #

def test_dedupe_n_artifacts_one_system_one_update_call():
    """THREE changed secret-delivered artifacts naming the same system ⇒
    exactly ONE update_agent_runtime call (through the REAL
    refresh_business_context, so the shared ADR-0056 builder runs too)."""
    secrets = _FakeSecrets(current=dict(_SIBLINGS))
    control = _FakeControl(system_ids=("erp",))
    mgr = _mgr()
    with patch("agents.agent_manager.boto3", _fake_boto3(control, secrets)), \
            _patch_graph_state(_ENABLED_STATE):
        report = mgr.refresh_systems_batched(
            ["erp", "erp", "erp"], reason="test",
            artifact_classes=("business_context", "system_prompt", "tables_desc"))

    assert len(control.update_calls) == 1, control.update_calls
    assert list(report) == ["erp"]
    assert report["erp"]["status"] == "ok"
    # ADR-0056: the shared builder round-tripped protocolConfiguration.
    assert control.update_calls[0]["protocolConfiguration"] == {
        "serverProtocol": "MCP"}


# --------------------------------------------------------------------------- #
# 2. Serial ordering — B's roll waits for A's READY (order, not counts)
# --------------------------------------------------------------------------- #

def test_serial_ordering_b_not_rolled_until_a_ready():
    """System B's update is NOT issued until A polls READY again: with A's
    runtime reporting UPDATING for two readiness polls, both polls (and A's
    READY flip) must precede B's update in the shared event log."""
    events: list = []
    control = _FakeControl(system_ids=("erp", "mes"), events=events,
                           not_ready_polls={"adt_erp": 2})
    mgr = _mgr()
    rolls = []

    def refresh_fn(sid):
        events.append(("roll", sid))
        rolls.append(sid)
        return "ok"

    with patch("agents.agent_manager.boto3", _fake_boto3(control)):
        report = mgr.refresh_systems_batched(["erp", "mes"], reason="test",
                                             refresh_fn=refresh_fn)

    assert rolls == ["erp", "mes"]
    roll_a = events.index(("roll", "erp"))
    roll_b = events.index(("roll", "mes"))
    polls_between = [e for e in events[roll_a:roll_b] if e == ("poll",)]
    # Two UPDATING polls + the READY poll all happen BEFORE mes rolls.
    assert len(polls_between) >= 3, events
    assert roll_b > roll_a
    assert report["erp"]["status"] == "ok"
    assert report["mes"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# 3. Warm-up verification — ready_verified / ready_unverified
# --------------------------------------------------------------------------- #

def test_warmup_probe_success_reports_ready_verified():
    control = _FakeControl(system_ids=("erp",))
    mgr = _mgr()
    probes = []
    with patch("agents.agent_manager.boto3", _fake_boto3(control)), \
            patch.object(AgentManager, "_get_m2m_token", lambda self, r: "tok"), \
            patch.object(AgentManager, "_send_mcp_initialize",
                         lambda self, url, token, timeout=30, client_name="warmup":
                         probes.append((url, client_name))):
        report = mgr.refresh_systems_batched(["erp"], refresh_fn=lambda sid: "ok")

    assert report["erp"] == {"status": "ok", "readiness": "ready_verified",
                             "detail": None}
    # Exactly ONE probe, named so it never looks like user traffic.
    assert len(probes) == 1 and probes[0][1] == "roll-verify"
    assert "rt-erp" in probes[0][0]


def test_warmup_probe_failure_reports_ready_unverified():
    def _boom(self, url, token, timeout=30, client_name="warmup"):
        raise OSError("connection reset")

    control = _FakeControl(system_ids=("erp",))
    mgr = _mgr()
    with patch("agents.agent_manager.boto3", _fake_boto3(control)), \
            patch.object(AgentManager, "_get_m2m_token", lambda self, r: "tok"), \
            patch.object(AgentManager, "_send_mcp_initialize", _boom):
        report = mgr.refresh_systems_batched(["erp"], refresh_fn=lambda sid: "ok")
    assert report["erp"]["readiness"] == "ready_unverified"
    assert report["erp"]["status"] == "ok"  # the roll itself succeeded


def test_warmup_probe_without_m2m_token_is_honest_unverified():
    """No M2M config ⇒ the probe cannot run ⇒ ready_unverified — an honest
    'could not verify', never a guessed 'verified'."""
    control = _FakeControl(system_ids=("erp",))
    mgr = _mgr()
    with patch("agents.agent_manager.boto3", _fake_boto3(control)), \
            patch.object(AgentManager, "_get_m2m_token", lambda self, r: ""):
        report = mgr.refresh_systems_batched(["erp"], refresh_fn=lambda sid: "ok")
    assert report["erp"]["readiness"] == "ready_unverified"


# --------------------------------------------------------------------------- #
# 4. Tri-state preserved + mid-roll failure stops the batch
# --------------------------------------------------------------------------- #

def test_tri_state_passthrough_and_failure_aborts_tail():
    """ADR-0054 D4 semantics unchanged: skipped passes through; a FAILED roll
    stops the batch — the tail reports skipped with a NAMED abort detail and
    receives zero rolls (rolling on could put two runtimes down)."""
    mgr = _mgr()
    rolls = []

    def refresh_fn(sid):
        rolls.append(sid)
        return {"cmms": "skipped", "erp": "failed"}.get(sid, "ok")

    boto3_stub, _guard = _forbidden_boto3()  # no READY gate should ever run
    with patch("agents.agent_manager.boto3", boto3_stub):
        report = mgr.refresh_systems_batched(["cmms", "erp", "mes"],
                                             refresh_fn=refresh_fn)

    assert rolls == ["cmms", "erp"]  # mes never attempted
    assert report["cmms"]["status"] == "skipped"
    assert report["erp"]["status"] == "failed"
    assert report["mes"] == {"status": "skipped", "readiness": None,
                             "detail": "aborted — erp failed"}


def test_refresh_exception_becomes_failed_never_raises():
    def _boom(sid):
        raise RuntimeError("kaboom")

    mgr = _mgr()
    boto3_stub, _ = _forbidden_boto3()
    with patch("agents.agent_manager.boto3", boto3_stub):
        report = mgr.refresh_systems_batched(["erp"], refresh_fn=_boom)
    assert report["erp"]["status"] == "failed"
    assert report["erp"]["detail"] == "RuntimeError"


def test_stuck_runtime_never_ready_aborts_tail():
    """A rolled runtime that never comes READY within the bound ⇒ its entry
    is ok/not_ready (the roll DID land) and the tail is aborted — never two
    runtimes down at once, never an indefinite hang. Deterministic fake clock:
    the first attempt fits the 90 s bound, the readiness wait expires it."""
    control = _FakeControl(system_ids=("erp", "mes"),
                           not_ready_polls={"adt_erp": 10_000})
    mgr = _mgr()
    with patch("agents.agent_manager.boto3", _fake_boto3(control)), \
            patch("agents.agent_manager.time", _FakeTime(step=60.0)):
        report = mgr.refresh_systems_batched(["erp", "mes"], timeout_seconds=90,
                                             refresh_fn=lambda sid: "ok")
    assert report["erp"]["status"] == "ok"
    assert report["erp"]["readiness"] == "not_ready"
    assert report["mes"]["status"] == "skipped"
    assert "erp" in report["mes"]["detail"]


# --------------------------------------------------------------------------- #
# 5. Overall timeout ⇒ clear partial report
# --------------------------------------------------------------------------- #

def test_overall_timeout_produces_partial_report():
    """With a 90 s bound and a clock advancing 60 s per reading, system A is
    attempted (and gated) but system B's slot lands past the deadline ⇒ B is
    reported skipped with the batch-timeout detail — a PARTIAL report, not a
    hang and not silence."""
    control = _FakeControl(system_ids=("erp", "mes"))
    mgr = _mgr()
    rolls = []

    def refresh_fn(sid):
        rolls.append(sid)
        return "ok"

    with patch("agents.agent_manager.boto3", _fake_boto3(control)), \
            patch("agents.agent_manager.time", _FakeTime(step=60.0)):
        report = mgr.refresh_systems_batched(["erp", "mes"], timeout_seconds=90,
                                             refresh_fn=refresh_fn)

    assert rolls == ["erp"]
    assert report["erp"]["status"] == "ok"
    assert report["mes"] == {"status": "skipped", "readiness": None,
                             "detail": "batch timeout — not attempted"}


# --------------------------------------------------------------------------- #
# 6. The pinned no-roll delivery rule
# --------------------------------------------------------------------------- #

def test_no_roll_table_pinned():
    """The delivery rule is a TABLE, pinned: only secret-delivered payloads
    roll; per-turn/per-session classes never do; unknown classes default to
    NO roll (fail-safe — registering a new secret-delivered class is a
    deliberate edit HERE, and forgetting shows as a truthful 'skipped')."""
    assert ROLL_REQUIRED_ARTIFACT_CLASSES == {"business_context",
                                              "system_prompt", "tables_desc"}
    assert NO_ROLL_ARTIFACT_CLASSES == {"query_guidance", "graph_artifact",
                                        "domain_note"}
    for cls in ROLL_REQUIRED_ARTIFACT_CLASSES:
        assert artifact_class_requires_roll(cls) is True, cls
    for cls in NO_ROLL_ARTIFACT_CLASSES:
        assert artifact_class_requires_roll(cls) is False, cls
    assert artifact_class_requires_roll("some_future_class") is False


@pytest.mark.parametrize("artifact_class",
                         sorted(NO_ROLL_ARTIFACT_CLASSES))
def test_no_roll_classes_zero_update_calls(artifact_class):
    """Guidance/graph/domain-note changes ⇒ ZERO runtime updates, ZERO AWS
    calls — every system reports skipped with the named no-roll detail."""
    mgr = _mgr()
    boto3_stub, guard = _forbidden_boto3()
    refresh_spy = MagicMock(side_effect=AssertionError("must not roll"))
    with patch("agents.agent_manager.boto3", boto3_stub):
        report = mgr.refresh_systems_batched(["erp", "mes"],
                                             artifact_classes=(artifact_class,),
                                             refresh_fn=refresh_spy)
    refresh_spy.assert_not_called()
    guard.assert_not_called()
    assert report == {
        "erp": {"status": "skipped", "readiness": None,
                "detail": "no-roll artifact class"},
        "mes": {"status": "skipped", "readiness": None,
                "detail": "no-roll artifact class"},
    }


def test_mixed_classes_with_one_secret_delivered_still_rolls():
    """A batch carrying BOTH a no-roll class and a secret-delivered class
    rolls (the secret-delivered change still needs delivery)."""
    control = _FakeControl(system_ids=("erp",))
    mgr = _mgr()
    with patch("agents.agent_manager.boto3", _fake_boto3(control)):
        report = mgr.refresh_systems_batched(
            ["erp"], artifact_classes=("query_guidance", "business_context"),
            refresh_fn=lambda sid: "ok")
    assert report["erp"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# 7. Route level — BC PUT carries readiness; failure never rolls back the write
# --------------------------------------------------------------------------- #

class _AdminInjectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.is_admin = True
        return await call_next(request)


class _CoordinatorManager:
    """Manager stand-in exposing the T1.5 coordinator seam."""

    def __init__(self, report):
        self.report = report
        self.calls = []

    def refresh_systems_batched(self, system_ids, reason="", *,
                                artifact_classes=("business_context",),
                                timeout_seconds=None, refresh_fn=None):
        self.calls.append({"system_ids": list(system_ids), "reason": reason,
                           "artifact_classes": tuple(artifact_classes),
                           "timeout_seconds": timeout_seconds})
        return self.report


@pytest.fixture
def bc_app(monkeypatch):
    """The REAL admin router with in-memory graph IO (no AWS, no Neptune)."""
    import agents.admin_routes as admin_routes
    import agents.business_context as bc
    from agents import catalog as catalog_mod
    from agents import connection_resolver as cr

    store: dict[str, dict] = {}
    monkeypatch.setattr(catalog_mod.catalog, "get",
                        lambda sid: {"id": sid} if sid == "erp" else None)

    def fake_write(system_id, text, enabled):
        store[system_id] = {"business_context": text, "enabled": bool(enabled)}
        return True

    monkeypatch.setattr(bc, "write_business_context", fake_write)
    monkeypatch.setattr(cr, "audit_connection_event",
                        lambda *a, **k: None)
    admin_routes.set_agent_manager(None)
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_AdminInjectMiddleware)
    yield app, store, admin_routes
    admin_routes.set_agent_manager(None)


def test_bc_put_routes_through_coordinator_and_carries_readiness(bc_app):
    app, store, admin_routes = bc_app
    mgr = _CoordinatorManager({"erp": {"status": "ok",
                                       "readiness": "ready_verified",
                                       "detail": None}})
    admin_routes.set_agent_manager(mgr)
    resp = TestClient(app).put("/api/admin/business-context/erp",
                               json={"business_context": "three shifts",
                                     "enabled": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_refresh"] == "ok"
    assert body["readiness"] == {"erp": {"status": "ok",
                                         "readiness": "ready_verified",
                                         "detail": None}}
    # ONE coordinator call, business_context class, CloudFront-safe bound.
    assert mgr.calls == [{"system_ids": ["erp"],
                          "reason": "business-context PUT",
                          "artifact_classes": ("business_context",),
                          "timeout_seconds": 100}]


def test_bc_put_refresh_failure_never_rolls_back_graph_write(bc_app):
    """The durable graph write survives a coordinator failure: 200, honest
    runtime_refresh=failed + readiness detail, and the store keeps the text."""
    app, store, admin_routes = bc_app
    mgr = _CoordinatorManager({"erp": {"status": "failed", "readiness": None,
                                       "detail": "RuntimeError"}})
    admin_routes.set_agent_manager(mgr)
    resp = TestClient(app).put("/api/admin/business-context/erp",
                               json={"business_context": "three shifts",
                                     "enabled": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"          # the WRITE landed
    assert body["runtime_refresh"] == "failed"  # the refresh reported honestly
    assert body["readiness"]["erp"]["status"] == "failed"
    assert store["erp"]["business_context"] == "three shifts"  # no rollback


def test_bc_put_without_coordinator_falls_back_to_t6_seam(bc_app):
    """Older/injected managers without refresh_systems_batched keep working
    (additive API surface): the T6 best-effort seam answers, readiness null."""
    app, store, admin_routes = bc_app

    class _LegacyManager:
        def refresh_business_context(self, system_id):
            return "ok"

    admin_routes.set_agent_manager(_LegacyManager())
    resp = TestClient(app).put("/api/admin/business-context/erp",
                               json={"business_context": "x", "enabled": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_refresh"] == "ok"
    assert body["readiness"] is None
