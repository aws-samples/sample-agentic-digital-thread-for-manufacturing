# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""_reonboard_lib.py — durable re-onboard primitives (readiness + deletion waits).

This module is the reusable, importable, offline-unit-testable core behind
``scripts/reonboard.py``. It knows nothing about any specific system id, dataset,
model, or connection type — it operates purely on whatever system ids it is
handed and on the AgentCore control-plane client that is *injected* into every
function (so unit tests can stub it and no live AWS is required).

Why it exists
-------------
Re-onboarding a system deletes+recreates its AgentCore *runtime*. The recreate
can transiently strand (e.g. the AZ-strip self-heal in
``agents/agent_manager.py::_start_cloud`` racing a ``ConflictException`` while the
old runtime is still deleting), leaving the first-provisioned system in a
non-READY state. Meanwhile ``scripts/onboarding_smoketest.py`` intentionally
polls ``catalog=onboarded`` (a fast "usable enough" signal) — NOT runtime READY —
so tooling can report "done" before the backing runtime actually is.

These helpers close that gap: they block until the AgentCore **runtime** AND the
gateway **target** are truly ``READY``, and they surface terminal
``CREATE_FAILED`` / ``UPDATE_FAILED`` runtime states as clear, typed errors so the
caller can apply a *bounded* re-trigger rather than looping forever or masking
the failure.

Naming convention (single source of truth)
-------------------------------------------
``runtime_name`` / ``target_name`` mirror the convention
``agents/agent_manager.py::_start_cloud`` uses to create/find the runtime and
gateway target. ``AgentManager._runtime_name`` is the authoritative in-app
implementation; ``tests/unit/test_reonboard_lib.py`` pins these to it so the two
can never drift. (The scripts run from the repo root where the ``agents``
package is not importable, so we mirror rather than import — exactly as
``onboarding_smoketest.py`` already does.)
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping

# Terminal AgentCore runtime states. Reaching one of these is NOT recoverable by
# waiting — the caller must delete + recreate (a bounded re-trigger).
READY = "READY"
TERMINAL_RUNTIME_FAILURES = ("CREATE_FAILED", "UPDATE_FAILED")


# --------------------------------------------------------------------------- #
# Typed errors — everything propagates (no silent fallback / no fake data).
# --------------------------------------------------------------------------- #
class ReonboardError(RuntimeError):
    """Base class for all re-onboard readiness/deletion failures."""


class RuntimeReadyTimeoutError(ReonboardError):
    """One or more runtimes did not reach READY before the deadline."""

    def __init__(self, pending: Mapping[str, str], deadline_s: float):
        self.pending = dict(pending)
        self.deadline_s = deadline_s
        detail = ", ".join(f"{name}={status}" for name, status in sorted(self.pending.items()))
        super().__init__(
            f"runtimes not READY after {deadline_s:g}s (pending: {detail or 'none'})"
        )


class RuntimeCreateFailedError(ReonboardError):
    """A runtime reached a terminal CREATE_FAILED / UPDATE_FAILED state.

    ``failure_reason`` carries the AgentCore ``failureReason`` verbatim when it
    could be read (it names the offending subnet(s) for the AZ-strip case)."""

    def __init__(self, system_id: str, runtime_name: str, status: str, failure_reason: str = ""):
        self.system_id = system_id
        self.runtime_name = runtime_name
        self.status = status
        self.failure_reason = failure_reason
        suffix = f": {failure_reason}" if failure_reason else ""
        super().__init__(
            f"runtime {runtime_name} for system '{system_id}' is terminal {status}{suffix}"
        )


class TargetReadyTimeoutError(ReonboardError):
    """One or more gateway targets did not reach READY before the deadline."""

    def __init__(self, pending: Mapping[str, str], deadline_s: float):
        self.pending = dict(pending)
        self.deadline_s = deadline_s
        detail = ", ".join(f"{name}={status}" for name, status in sorted(self.pending.items()))
        super().__init__(
            f"gateway targets not READY after {deadline_s:g}s (pending: {detail or 'none'})"
        )


class DeletionTimeoutError(ReonboardError):
    """A resource did not finish deleting before the deadline."""


# --------------------------------------------------------------------------- #
# Naming — mirrors agent_manager._start_cloud (pinned in the unit tests).
# --------------------------------------------------------------------------- #
def runtime_name(system_id: str) -> str:
    """AgentCore runtime name for *system_id*: ``adt_<id>``, hyphens→underscores, ≤48.

    Mirrors ``AgentManager._runtime_name`` (the authoritative implementation) so
    the wait/delete helpers look up exactly the runtime ``_start_cloud`` creates.
    """
    return f"adt_{system_id}".replace("-", "_")[:48]


def target_name(system_id: str) -> str:
    """Gateway target name for *system_id*: underscores→hyphens, ≤100.

    Mirrors the ``system_id.replace("_", "-")[:100]`` convention ``_start_cloud``
    uses when it upserts the gateway target.
    """
    return system_id.replace("_", "-")[:100]


def _is_already_deleting_error(exc: Exception) -> bool:
    """True iff *exc* is the control-plane's already-deleting rejection.

    Both delete APIs reject a delete issued while a previous delete is still in
    flight (runtime: ``ConflictException ... Current status: DELETING``; target:
    ``ValidationException ... in Deleting state``). For an idempotent re-entry
    (e.g. a re-run after a deletion-wait timeout) that rejection just means
    "keep waiting", not "fail". Message-based classification (no botocore
    import; works with injected fakes)."""
    msg = str(exc)
    return (
        ("ConflictException" in msg or "ValidationException" in msg)
        and ("DELETING" in msg.upper() or "deleting state" in msg.lower())
    )


# --------------------------------------------------------------------------- #
# Control-plane list helpers (tolerant of the boto3 response-shape variants the
# rest of the codebase already handles: agentRuntimeSummaries|agentRuntimes,
# items|targets).
# --------------------------------------------------------------------------- #
def _runtime_map(control) -> dict[str, dict]:
    """Return ``{agentRuntimeName: summary}`` from ``list_agent_runtimes``.

    PAGINATES via ``nextToken``: an account accumulates runtimes over time, and an
    unpaginated read silently drops anything past the first page — the waiter then
    reports a READY runtime as ABSENT and times out (live incident: adt_<system>
    READY but reported ``ABSENT`` after 600s with ~dozens of runtimes present).

    Each summary is normalized to always carry an ``id`` (agentRuntimeId, falling
    back to the tail of the ARN) and a ``status`` alongside the raw fields.
    """
    out: dict[str, dict] = {}
    kwargs: dict = {}
    while True:
        resp = control.list_agent_runtimes(**kwargs)
        items = resp.get("agentRuntimeSummaries", resp.get("agentRuntimes", []))
        for rt in items:
            name = rt.get("agentRuntimeName", "")
            if not name:
                continue
            rid = rt.get("agentRuntimeId") or (rt.get("agentRuntimeArn", "").rsplit("/", 1)[-1])
            out[name] = {**rt, "id": rid, "status": rt.get("status", "")}
        token = resp.get("nextToken")
        if not token:
            return out
        kwargs["nextToken"] = token


def _target_map(control, gateway_id: str) -> dict[str, dict]:
    """Return ``{targetName: item}`` from ``list_gateway_targets`` (paginated)."""
    out: dict[str, dict] = {}
    kwargs: dict = {"gatewayIdentifier": gateway_id}
    while True:
        resp = control.list_gateway_targets(**kwargs)
        items = resp.get("items", resp.get("targets", []))
        for t in items:
            name = t.get("name", "")
            if not name:
                continue
            out[name] = {**t, "id": t.get("targetId", ""), "status": t.get("status", "")}
        token = resp.get("nextToken")
        if not token:
            return out
        kwargs["nextToken"] = token


def _runtime_failure_reason(control, runtime_id: str) -> str:
    """Best-effort read of the AgentCore ``failureReason`` for a terminal runtime.

    Failure to read it must not mask the terminal state — return ""; the typed
    error is still raised by the caller.
    """
    if not runtime_id:
        return ""
    try:
        detail = control.get_agent_runtime(agentRuntimeId=runtime_id)
        return detail.get("failureReason", "") or ""
    except Exception:  # noqa: BLE001 — best-effort enrichment only
        return ""


# --------------------------------------------------------------------------- #
# Readiness waits
# --------------------------------------------------------------------------- #
def wait_runtimes_ready(
    control,
    system_ids: Iterable[str],
    deadline_s: float = 600,
    poll_s: float = 10,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, dict]:
    """Block until the ``adt_<id>`` runtime for EVERY system in *system_ids* is READY.

    Polls ``control.list_agent_runtimes`` and returns ``{system_id: summary}`` only
    once all backing runtimes report ``status == "READY"``. A runtime that is
    absent (e.g. still being (re)created) counts as pending and is polled until
    the deadline. Raises:

      * :class:`RuntimeCreateFailedError` immediately if any runtime reaches a
        terminal ``CREATE_FAILED`` / ``UPDATE_FAILED`` state (the AgentCore
        ``failureReason`` is attached), and
      * :class:`RuntimeReadyTimeoutError` if the deadline elapses with any runtime
        still not READY.

    ``control`` is dependency-injected (never constructed here) so tests can stub
    it; ``sleep`` / ``monotonic`` are injectable for deterministic offline tests.
    """
    ids = list(system_ids)
    names = {sid: runtime_name(sid) for sid in ids}
    t0 = monotonic()
    while True:
        rmap = _runtime_map(control)
        pending: dict[str, str] = {}
        for sid, name in names.items():
            rt = rmap.get(name)
            status = rt.get("status", "") if rt else "ABSENT"
            if status == READY:
                continue
            if status in TERMINAL_RUNTIME_FAILURES:
                reason = _runtime_failure_reason(control, rt.get("id", "") if rt else "")
                raise RuntimeCreateFailedError(sid, name, status, reason)
            pending[name] = status
        if not pending:
            return {sid: rmap[names[sid]] for sid in ids}
        if monotonic() - t0 >= deadline_s:
            raise RuntimeReadyTimeoutError(pending, deadline_s)
        sleep(poll_s)


def wait_targets_ready(
    control,
    gateway_id: str,
    target_names: Iterable[str],
    deadline_s: float = 600,
    poll_s: float = 10,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, dict]:
    """Block until EVERY gateway target in *target_names* reports ``status == READY``.

    Polls ``control.list_gateway_targets`` and returns ``{target_name: item}`` once
    all named targets are READY. A target that is absent or in any non-READY state
    (``CREATING`` etc.) counts as pending. Raises :class:`TargetReadyTimeoutError`
    if the deadline elapses with any target still not READY.

    ``gateway_id`` must be supplied (there is no fallback); ``control`` is injected.
    """
    wanted = list(target_names)
    t0 = monotonic()
    while True:
        tmap = _target_map(control, gateway_id)
        pending: dict[str, str] = {}
        for name in wanted:
            t = tmap.get(name)
            status = t.get("status", "") if t else "ABSENT"
            if status != READY:
                pending[name] = status
        if not pending:
            return {name: tmap[name] for name in wanted}
        if monotonic() - t0 >= deadline_s:
            raise TargetReadyTimeoutError(pending, deadline_s)
        sleep(poll_s)


# --------------------------------------------------------------------------- #
# Deletion waits — used before a re-onboard recreates the runtime/target.
# --------------------------------------------------------------------------- #
def delete_target(
    control,
    gateway_id: str,
    system_id: str,
    deadline_s: float = 120,
    poll_s: float = 3,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Delete the gateway target for *system_id* (if present) and wait until gone.

    Returns ``True`` if a target existed and a delete was issued, ``False`` if
    there was nothing to delete. Raises :class:`DeletionTimeoutError` if the
    target does not disappear within the deadline.
    """
    name = target_name(system_id)
    existing = _target_map(control, gateway_id).get(name)
    if not existing:
        return False
    try:
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=existing["id"])
    except Exception as exc:  # noqa: BLE001 - classified below, re-raised if unrelated
        if not _is_already_deleting_error(exc):
            raise
        # An earlier (interrupted) delete is still in flight — fall through to
        # the wait loop below instead of failing (idempotent re-entry).
    t0 = monotonic()
    while name in _target_map(control, gateway_id):
        if monotonic() - t0 >= deadline_s:
            raise DeletionTimeoutError(
                f"gateway target {name} still present after {deadline_s:g}s"
            )
        sleep(poll_s)
    return True


def delete_runtime(
    control,
    system_id: str,
    deadline_s: float = 180,
    poll_s: float = 5,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Delete the ``adt_<id>`` runtime for *system_id* (if present) and wait until gone.

    Returns ``True`` if a runtime existed and a delete was issued, ``False`` if
    there was nothing to delete. Raises :class:`DeletionTimeoutError` if the
    runtime does not disappear within the deadline.
    """
    name = runtime_name(system_id)
    existing = _runtime_map(control).get(name)
    if not existing:
        return False
    try:
        control.delete_agent_runtime(agentRuntimeId=existing["id"])
    except Exception as exc:  # noqa: BLE001 - classified below, re-raised if unrelated
        if not _is_already_deleting_error(exc):
            raise
        # An earlier (interrupted) delete is still in flight — fall through to
        # the wait loop below instead of failing (idempotent re-entry).
    t0 = monotonic()
    while name in _runtime_map(control):
        if monotonic() - t0 >= deadline_s:
            raise DeletionTimeoutError(f"runtime {name} still present after {deadline_s:g}s")
        sleep(poll_s)
    return True
