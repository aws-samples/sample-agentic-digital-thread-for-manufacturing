# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the durable re-onboard helper (scripts/_reonboard_lib.py
and scripts/reonboard.py).

Everything here is fully offline: the AgentCore control-plane client is a stub
(no boto3, no live AWS) and the onboarding REST pipeline is a stub callable (no
HTTP). ``sleep``/``monotonic`` are injected so waits are deterministic and
instant. Mirrors the stub style in ``test_agent_manager_fastpath_runtime.py`` and
``test_provisioner_states.py``.

Coverage (task deliverable (c)):
  * ``wait_runtimes_ready`` returns only when READY / times out / raises on
    terminal CREATE_FAILED (with the AgentCore failureReason surfaced);
  * ``wait_targets_ready`` happy path + timeout;
  * ``reonboard_systems`` provisions in the GIVEN order and BLOCKS on runtime
    READY — NOT merely catalog=onboarded;
  * the bounded (exactly-once) re-trigger on a stranded CREATE_FAILED runtime,
    and that a second terminal failure propagates;
  * the ``adt_<id>`` / target naming matches ``AgentManager`` (DRY, no drift).

Run:
  * pytest:     pytest agentic_digital_thread/tests/unit/test_reonboard_lib.py -q
  * standalone: python agentic_digital_thread/tests/unit/test_reonboard_lib.py
"""
import itertools
import sys
from pathlib import Path

# agentic_digital_thread/ (for agents.*) and repo-root scripts/ (for the helpers under test).
_ADT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ADT_ROOT.parent / "scripts"
for _p in (str(_ADT_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

import reonboard  # noqa: E402
from _reonboard_lib import (  # noqa: E402
    ReonboardError,
    RuntimeCreateFailedError,
    RuntimeReadyTimeoutError,
    TargetReadyTimeoutError,
    runtime_name,
    target_name,
    wait_runtimes_ready,
    wait_targets_ready,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _clock(step=1, start=0):
    """A deterministic monotonic() replacement that advances by *step* each call."""
    c = itertools.count(start, step)
    return lambda: next(c)


class _FakeControl:
    """AgentCore control-plane stub.

    Runtimes/targets are scripted with a status SEQUENCE that advances one step
    per ``list_*`` call (the last element repeats), so a runtime can transition
    CREATING→…→READY across polls. Deletes remove the resource so the readiness
    waits observe it disappear."""

    def __init__(self):
        self.runtimes: dict[str, dict] = {}
        self.targets: dict[str, dict] = {}
        self.deleted_runtimes: list[str] = []
        self.deleted_targets: list[str] = []
        self.list_runtime_calls = 0
        self.list_target_calls = 0

    # -- scripting helpers (test-only) --
    def add_runtime(self, name, statuses, failure_reason=""):
        self.runtimes[name] = {
            "id": f"{name}-id",
            "arn": f"arn:aws:bedrock-agentcore:us-east-1:444455556666:runtime/{name}-id",
            "statuses": list(statuses),
            "failureReason": failure_reason,
        }

    set_runtime = add_runtime  # replace semantics

    def add_target(self, name, statuses):
        self.targets[name] = {"id": f"{name}-tgt", "statuses": list(statuses)}

    @staticmethod
    def _advance(statuses):
        cur = statuses[0]
        if len(statuses) > 1:
            statuses.pop(0)
        return cur

    # -- runtime control-plane --
    def list_agent_runtimes(self):
        self.list_runtime_calls += 1
        items = []
        for name, e in self.runtimes.items():
            items.append({
                "agentRuntimeName": name,
                "agentRuntimeId": e["id"],
                "agentRuntimeArn": e["arn"],
                "status": self._advance(e["statuses"]),
            })
        return {"agentRuntimes": items}

    def get_agent_runtime(self, agentRuntimeId):  # noqa: N803 - boto3 kwarg name
        for e in self.runtimes.values():
            if e["id"] == agentRuntimeId:
                return {"status": e["statuses"][0], "failureReason": e["failureReason"]}
        raise KeyError(agentRuntimeId)

    def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
        for name, e in list(self.runtimes.items()):
            if e["id"] == agentRuntimeId:
                del self.runtimes[name]
                self.deleted_runtimes.append(name)
                return {}
        raise KeyError(agentRuntimeId)

    # -- gateway-target control-plane --
    def list_gateway_targets(self, gatewayIdentifier):  # noqa: N803
        self.list_target_calls += 1
        items = []
        for name, e in self.targets.items():
            items.append({
                "name": name,
                "targetId": e["id"],
                "status": self._advance(e["statuses"]),
            })
        return {"items": items}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        for name, e in list(self.targets.items()):
            if e["id"] == targetId:
                del self.targets[name]
                self.deleted_targets.append(name)
                return {}
        raise KeyError(targetId)


class _FakeApp:
    """REST-client stub exposing ``catalog`` and ``call`` (all the driver needs)."""

    def __init__(self, catalog=None, call_error=None):
        self._catalog = dict(catalog or {})
        self.calls: list[tuple] = []
        self._call_error = call_error

    def catalog(self):
        return dict(self._catalog)

    def mark_onboarded(self, sid):
        self._catalog.setdefault(sid, {"name": sid})["status"] = "onboarded"

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self._call_error is not None:
            raise self._call_error
        return {"deleted": 0, "checked": 0}


def _resolver(_catalog, systems, _env):
    """Connection resolver stub: every system gets a non-empty connection."""
    return {s: {"connection_string": f"dsn://{s}", "connection_type": "PostgreSQL"} for s in systems}


# --------------------------------------------------------------------------- #
# wait_runtimes_ready
# --------------------------------------------------------------------------- #
def test_wait_runtimes_ready_returns_only_when_all_ready():
    control = _FakeControl()
    control.add_runtime(runtime_name("sys-1"), ["CREATING", "CREATING", "READY"])
    control.add_runtime(runtime_name("sys-2"), ["READY"])
    res = wait_runtimes_ready(
        control, ["sys-1", "sys-2"], deadline_s=1000, poll_s=0,
        sleep=lambda _s: None, monotonic=_clock(),
    )
    assert res["sys-1"]["status"] == "READY"
    assert res["sys-2"]["status"] == "READY"
    # sys-1 needed three polls before it flipped READY (proves it waited).
    assert control.list_runtime_calls == 3


def test_wait_runtimes_ready_times_out():
    control = _FakeControl()
    control.add_runtime(runtime_name("sys"), ["CREATING"])  # never becomes READY
    with pytest.raises(RuntimeReadyTimeoutError) as exc:
        wait_runtimes_ready(
            control, ["sys"], deadline_s=50, poll_s=0,
            sleep=lambda _s: None, monotonic=_clock(step=100),
        )
    assert runtime_name("sys") in exc.value.pending


def test_wait_runtimes_ready_raises_on_create_failed_with_reason():
    control = _FakeControl()
    reason = "unsupported availability zone subnet-abc123"
    control.add_runtime(runtime_name("sys"), ["CREATE_FAILED"], failure_reason=reason)
    with pytest.raises(RuntimeCreateFailedError) as exc:
        wait_runtimes_ready(
            control, ["sys"], deadline_s=1000, poll_s=0,
            sleep=lambda _s: None, monotonic=_clock(),
        )
    assert exc.value.status == "CREATE_FAILED"
    assert exc.value.failure_reason == reason
    assert reason in str(exc.value)


def test_wait_runtimes_ready_absent_runtime_is_pending_then_times_out():
    # A runtime that never appears (still (re)creating) counts as pending, not READY.
    control = _FakeControl()  # no runtimes at all
    with pytest.raises(RuntimeReadyTimeoutError):
        wait_runtimes_ready(
            control, ["sys"], deadline_s=10, poll_s=0,
            sleep=lambda _s: None, monotonic=_clock(step=100),
        )


# --------------------------------------------------------------------------- #
# wait_targets_ready
# --------------------------------------------------------------------------- #
def test_wait_targets_ready_happy():
    control = _FakeControl()
    control.add_target(target_name("sys"), ["CREATING", "READY"])
    res = wait_targets_ready(
        control, "gw-1", [target_name("sys")], deadline_s=1000, poll_s=0,
        sleep=lambda _s: None, monotonic=_clock(),
    )
    assert res[target_name("sys")]["status"] == "READY"
    assert control.list_target_calls == 2


def test_wait_targets_ready_times_out():
    control = _FakeControl()
    control.add_target(target_name("sys"), ["CREATING"])  # never READY
    with pytest.raises(TargetReadyTimeoutError) as exc:
        wait_targets_ready(
            control, "gw-1", [target_name("sys")], deadline_s=50, poll_s=0,
            sleep=lambda _s: None, monotonic=_clock(step=100),
        )
    assert target_name("sys") in exc.value.pending


# --------------------------------------------------------------------------- #
# reonboard_systems — ordering + blocks-on-runtime-READY
# --------------------------------------------------------------------------- #
def test_reonboard_provisions_in_given_order():
    control = _FakeControl()
    order = []

    def prov(app, sid, *, meta, connection_string, token):
        order.append(sid)
        control.add_runtime(runtime_name(sid), ["READY"])
        control.add_target(target_name(sid), ["READY"])

    app = _FakeApp()
    res = reonboard.reonboard_systems(
        app, control, "gw-1", ["b", "a", "c"],
        env={}, provision_fn=prov, connection_resolver=_resolver,
        poll_s=0, sleep=lambda _s: None, monotonic=_clock(),
        log=lambda _m: None,
    )
    # Provisioned in the EXACT order given (operator front-loads the fragile system).
    assert order == ["b", "a", "c"]
    assert res == {"b": "ready", "a": "ready", "c": "ready"}


def test_reonboard_sweeps_blocked_after_all_ready():
    """After every system is READY, a SINGLE best-effort reconcile-blocked sweep runs
    (idempotent cleanup of proposals logged mid-re-onboard against a transiently
    unavailable schema)."""
    control = _FakeControl()

    def prov(app, sid, *, meta, connection_string, token):
        control.add_runtime(runtime_name(sid), ["READY"])
        control.add_target(target_name(sid), ["READY"])

    app = _FakeApp()
    res = reonboard.reonboard_systems(
        app, control, "gw-1", ["a", "b"],
        env={}, provision_fn=prov, connection_resolver=_resolver,
        poll_s=0, sleep=lambda _s: None, monotonic=_clock(),
        log=lambda _m: None,
    )
    assert res == {"a": "ready", "b": "ready"}
    sweeps = [c for c in app.calls if c[1] == "/api/proposed-mappings/reconcile-blocked"]
    assert sweeps == [("POST", "/api/proposed-mappings/reconcile-blocked", {})]


def test_reonboard_sweep_failure_never_fails_reonboard():
    """A sweep error is advisory — the systems are already READY, so the re-onboard
    still succeeds (the cleanup failure is swallowed and logged)."""
    control = _FakeControl()

    def prov(app, sid, *, meta, connection_string, token):
        control.add_runtime(runtime_name(sid), ["READY"])
        control.add_target(target_name(sid), ["READY"])

    app = _FakeApp(call_error=RuntimeError("gateway 500"))
    res = reonboard.reonboard_systems(
        app, control, "gw-1", ["a"],
        env={}, provision_fn=prov, connection_resolver=_resolver,
        poll_s=0, sleep=lambda _s: None, monotonic=_clock(),
        log=lambda _m: None,
    )
    assert res == {"a": "ready"}
    assert any(c[1] == "/api/proposed-mappings/reconcile-blocked" for c in app.calls)


def test_reonboard_sweep_can_be_disabled():
    control = _FakeControl()

    def prov(app, sid, *, meta, connection_string, token):
        control.add_runtime(runtime_name(sid), ["READY"])
        control.add_target(target_name(sid), ["READY"])

    app = _FakeApp()
    reonboard.reonboard_systems(
        app, control, "gw-1", ["a"],
        env={}, provision_fn=prov, connection_resolver=_resolver,
        poll_s=0, sleep=lambda _s: None, monotonic=_clock(),
        log=lambda _m: None, sweep_blocked=False,
    )
    assert not any(c[1] == "/api/proposed-mappings/reconcile-blocked" for c in app.calls)


def test_reonboard_blocks_on_runtime_ready_not_catalog_onboarded():
    """Even when the catalog reports 'onboarded' (the fast smoke-test signal), the
    driver must keep blocking on the backing runtime — so a runtime stuck CREATING
    yields a timeout, NOT a false 'done'."""
    control = _FakeControl()

    def prov(app, sid, *, meta, connection_string, token):
        control.add_runtime(runtime_name(sid), ["CREATING"])  # never READY
        control.add_target(target_name(sid), ["READY"])
        app.mark_onboarded(sid)  # catalog says done...

    app = _FakeApp({"x": {"name": "X", "status": "registered"}})
    with pytest.raises(RuntimeReadyTimeoutError):
        reonboard.reonboard_systems(
            app, control, "gw-1", ["x"],
            env={}, provision_fn=prov, connection_resolver=_resolver,
            runtime_deadline_s=30, poll_s=0,
            sleep=lambda _s: None, monotonic=_clock(step=100),
            log=lambda _m: None,
        )
    # ...but the runtime never was READY, and the catalog flip did not fool us.
    assert app.catalog()["x"]["status"] == "onboarded"


def test_reonboard_bounded_retrigger_recovers_from_create_failed():
    control = _FakeControl()
    calls = {"n": 0}

    def prov(app, sid, *, meta, connection_string, token):
        calls["n"] += 1
        if calls["n"] == 1:
            control.set_runtime(runtime_name(sid), ["CREATE_FAILED"],
                                failure_reason="unsupported availability zone subnet-x")
        else:
            control.set_runtime(runtime_name(sid), ["READY"])  # recovered on re-trigger
        control.add_target(target_name(sid), ["READY"])

    app = _FakeApp()
    res = reonboard.reonboard_systems(
        app, control, "gw-1", ["sys"],
        env={}, provision_fn=prov, connection_resolver=_resolver,
        runtime_deadline_s=1000, poll_s=0,
        sleep=lambda _s: None, monotonic=_clock(),
        log=lambda _m: None,
    )
    assert res == {"sys": "ready"}
    assert calls["n"] == 2, "must re-trigger the create exactly once"
    # The stranded CREATE_FAILED runtime was deleted before the re-trigger.
    assert runtime_name("sys") in control.deleted_runtimes


def test_reonboard_bounded_retrigger_is_exactly_once_then_propagates():
    control = _FakeControl()
    calls = {"n": 0}

    def prov(app, sid, *, meta, connection_string, token):
        calls["n"] += 1
        control.set_runtime(runtime_name(sid), ["CREATE_FAILED"],
                            failure_reason="unsupported availability zone subnet-x")
        control.add_target(target_name(sid), ["READY"])

    app = _FakeApp()
    with pytest.raises(RuntimeCreateFailedError):
        reonboard.reonboard_systems(
            app, control, "gw-1", ["sys"],
            env={}, provision_fn=prov, connection_resolver=_resolver,
            runtime_deadline_s=1000, poll_s=0,
            sleep=lambda _s: None, monotonic=_clock(),
            log=lambda _m: None,
        )
    # Provisioned once, re-triggered once, then the second failure propagated.
    assert calls["n"] == 2


def test_reonboard_missing_connection_propagates():
    control = _FakeControl()

    def _empty_resolver(_catalog, systems, _env):
        return {s: {"connection_string": ""} for s in systems}

    with pytest.raises(ReonboardError):
        reonboard.reonboard_systems(
            _FakeApp(), control, "gw-1", ["sys"],
            env={}, provision_fn=lambda *a, **k: None, connection_resolver=_empty_resolver,
            poll_s=0, sleep=lambda _s: None, monotonic=_clock(),
            log=lambda _m: None,
        )


def test_reonboard_requires_gateway_id():
    with pytest.raises(ReonboardError):
        reonboard.reonboard_systems(
            _FakeApp(), _FakeControl(), "", ["sys"],
            env={}, provision_fn=lambda *a, **k: None, connection_resolver=_resolver,
            poll_s=0, sleep=lambda _s: None, monotonic=_clock(),
            log=lambda _m: None,
        )


# --------------------------------------------------------------------------- #
# Server-side completion GRACE phase — the client deadline expiring with a
# PENDING resource keeps polling (every GRACE_POLL_S) for up to grace_s more
# instead of hard-failing (observed twice live: cmms, then lake-athena — the
# server finished provisioning minutes after the client gave up).
# --------------------------------------------------------------------------- #
def test_grace_recovers_runtime_ready_after_deadline():
    """A runtime still CREATING at the deadline that flips READY during grace
    completes the re-onboard — with the distinguishable 'completed in grace' log."""
    control = _FakeControl()

    def prov(app, sid, *, meta, connection_string, token):
        control.add_runtime(runtime_name(sid), ["CREATING", "CREATING", "READY"])
        control.add_target(target_name(sid), ["READY"])

    logs = []
    res = reonboard.reonboard_systems(
        _FakeApp(), control, "gw-1", ["sys"],
        env={}, provision_fn=prov, connection_resolver=_resolver,
        runtime_deadline_s=30, grace_s=600, poll_s=0,
        sleep=lambda _s: None, monotonic=_clock(step=100),
        log=logs.append,
    )
    assert res == {"sys": "ready"}
    assert any("deadline exceeded — server-side completion grace" in m for m in logs)
    assert any("completed in grace" in m for m in logs)


def test_grace_expiry_raises_typed_timeout_with_total_wait():
    """Grace also expiring is the HARD timeout: the typed error propagates,
    carrying the TOTAL wait (deadline + grace), after a 'hard timeout' log line."""
    control = _FakeControl()

    def prov(app, sid, *, meta, connection_string, token):
        control.add_runtime(runtime_name(sid), ["CREATING"])  # never READY
        control.add_target(target_name(sid), ["READY"])

    logs = []
    with pytest.raises(RuntimeReadyTimeoutError) as exc:
        reonboard.reonboard_systems(
            _FakeApp(), control, "gw-1", ["sys"],
            env={}, provision_fn=prov, connection_resolver=_resolver,
            runtime_deadline_s=30, grace_s=600, poll_s=0,
            sleep=lambda _s: None, monotonic=_clock(step=100),
            log=logs.append,
        )
    assert exc.value.deadline_s == 630  # deadline + grace, not just the deadline
    assert runtime_name("sys") in exc.value.pending
    assert any("hard timeout" in m for m in logs)
    assert any("server-side completion grace" in m for m in logs)


def test_grace_zero_disables_grace_phase():
    """grace_s=0 restores the plain deadline behaviour — no grace polls, no grace logs."""
    control = _FakeControl()

    def prov(app, sid, *, meta, connection_string, token):
        control.add_runtime(runtime_name(sid), ["CREATING"])  # never READY
        control.add_target(target_name(sid), ["READY"])

    logs = []
    with pytest.raises(RuntimeReadyTimeoutError) as exc:
        reonboard.reonboard_systems(
            _FakeApp(), control, "gw-1", ["sys"],
            env={}, provision_fn=prov, connection_resolver=_resolver,
            runtime_deadline_s=30, grace_s=0, poll_s=0,
            sleep=lambda _s: None, monotonic=_clock(step=100),
            log=logs.append,
        )
    assert exc.value.deadline_s == 30
    assert not any("grace" in m for m in logs)


def test_target_wait_grace_is_symmetric():
    """The gateway-target wait carries the SAME grace phase as the runtime wait."""
    control = _FakeControl()

    def prov(app, sid, *, meta, connection_string, token):
        control.add_runtime(runtime_name(sid), ["READY"])
        control.add_target(target_name(sid), ["CREATING", "CREATING", "READY"])

    logs = []
    res = reonboard.reonboard_systems(
        _FakeApp(), control, "gw-1", ["sys"],
        env={}, provision_fn=prov, connection_resolver=_resolver,
        target_deadline_s=30, grace_s=600, poll_s=0,
        sleep=lambda _s: None, monotonic=_clock(step=100),
        log=logs.append,
    )
    assert res == {"sys": "ready"}
    assert any("gateway target deadline exceeded — server-side completion grace" in m
               for m in logs)
    assert any("gateway target READY — completed in grace" in m for m in logs)


def test_grace_never_masks_terminal_failure():
    """A terminal CREATE_FAILED surfacing DURING grace propagates unchanged —
    grace only tolerates PENDING, never a genuine failure."""
    calls = {"n": 0}

    def wait_once(deadline):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeReadyTimeoutError({"adt_sys": "CREATING"}, deadline)
        raise RuntimeCreateFailedError("sys", "adt_sys", "CREATE_FAILED", "boom")

    with pytest.raises(RuntimeCreateFailedError):
        reonboard._wait_ready_with_grace(
            wait_once, "[sys] runtime", deadline_s=30, grace_s=600,
            log=lambda _m: None, sleep=lambda _s: None, monotonic=_clock(step=100),
        )
    assert calls["n"] == 2  # one deadline attempt + one grace poll, then it raised


# --------------------------------------------------------------------------- #
# Naming — must match AgentManager (DRY / no drift)
# --------------------------------------------------------------------------- #
def test_naming_matches_agent_manager_source_of_truth():
    from agents.agent_manager import AgentManager
    for sid in ["a-b-c", "a_b_c", "plain", "x" * 80]:
        assert runtime_name(sid) == AgentManager._runtime_name(sid)
    # Target naming folds underscores→hyphens and caps at 100.
    assert target_name("a_b") == "a-b"
    assert len(target_name("y" * 200)) == 100


_TESTS = [
    test_wait_runtimes_ready_returns_only_when_all_ready,
    test_wait_runtimes_ready_times_out,
    test_wait_runtimes_ready_raises_on_create_failed_with_reason,
    test_wait_runtimes_ready_absent_runtime_is_pending_then_times_out,
    test_wait_targets_ready_happy,
    test_wait_targets_ready_times_out,
    test_reonboard_provisions_in_given_order,
    test_reonboard_sweeps_blocked_after_all_ready,
    test_reonboard_sweep_failure_never_fails_reonboard,
    test_reonboard_sweep_can_be_disabled,
    test_reonboard_blocks_on_runtime_ready_not_catalog_onboarded,
    test_reonboard_bounded_retrigger_recovers_from_create_failed,
    test_reonboard_bounded_retrigger_is_exactly_once_then_propagates,
    test_reonboard_missing_connection_propagates,
    test_reonboard_requires_gateway_id,
    test_grace_recovers_runtime_ready_after_deadline,
    test_grace_expiry_raises_typed_timeout_with_total_wait,
    test_grace_zero_disables_grace_phase,
    test_target_wait_grace_is_symmetric,
    test_grace_never_masks_terminal_failure,
    test_naming_matches_agent_manager_source_of_truth,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} re-onboard helper tests passed (no real AWS/network)")


# --------------------------------------------------------------------------- #
# --all discovery: the register-only exercise systems are never bulk defaults.
# --------------------------------------------------------------------------- #

class _CatalogApp:
    def __init__(self, meta):
        self._meta = meta

    def catalog(self):
        return self._meta


def test_discover_all_fallback_drops_onboard_exclude():
    """Nothing onboarded yet ⇒ --all falls back to the catalog, MINUS the
    workshop register-only ids (the attendee exercise) in demo AND workshop."""
    from agents.demo.workshop import ONBOARD_EXCLUDE
    assert ONBOARD_EXCLUDE
    meta = {sid: {"status": "registered"} for sid in ("sys-b", "sys-a", *ONBOARD_EXCLUDE)}
    assert reonboard._discover_all_systems(_CatalogApp(meta)) == ["sys-a", "sys-b"]


def test_discover_all_prefers_onboarded_systems():
    meta = {"sys-b": {"status": "onboarded"}, "sys-a": {"status": "registered"}}
    assert reonboard._discover_all_systems(_CatalogApp(meta)) == ["sys-b"]
