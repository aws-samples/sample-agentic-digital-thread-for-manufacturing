# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ONE keyed lock per system for read-modify-writes of its CONNECTION state.

A system's connection lives in three places that a change rewrites as a whole:
its ``adt/conn/<id>`` secret (``write_connection`` rewrites the full payload),
its graph ``System`` node (``connection_config`` + ``secret_arn``), and its
runtime agent-config. Since the admin Edit runs in the offload pool, two edits
of one system can both read the old secret and the last whole-payload write
silently drops the other's change; an edit can also land between a
provision's source selection and its node write. The event loop used to
serialize these by accident.

Holders, and the ONE lock order:

* the admin connection Edit (``admin_routes._do_update_connection``): resolve,
  merge, probe, ``write_connection``, catalog sync and the reconnect;
* provision's connection phase (``onboard._dp_connection_phase``): source
  selection, agent start, node write;
* always OUTER to ``runtime_provision_helpers.RUNTIME_CONFIG_LOCK``, which is
  taken only inside it, around one AgentCore full replacement. No holder of
  ``RUNTIME_CONFIG_LOCK`` takes a connection lock (pinned by a test).
* a per-system removal (``onboard._reset_publish_claim``) takes this lock ONLY
  to check for a running provision and publish its ``_RESET_JOBS`` running
  claim, a brief check-and-write, never held across the minutes-long cascade.
  It is the ordering point: the claim fails fast behind a PROVISION or CHANGE
  holder, and provision and the Edit re-check reset state after they take the
  lock, before their first write. The removal cascade later takes
  ``asset_role_pass.ROLE_PASS_LOCK`` around its graph deletes, after this lock
  was released; no ``ROLE_PASS_LOCK`` holder takes a connection lock, and
  provision runs its role pass after its connection phase released this lock,
  so the two locks are never held in opposite orders.

Acquisition is bounded (:data:`CONNECTION_LOCK_TIMEOUT_SECONDS`): a change that
cannot get the lock fails with :class:`ConnectionBusyError` (retryable) rather
than queueing without limit. The calls made under it are themselves bounded
(boto3/HTTP timeouts; no READY polling in the Edit's reconnect). Re-entrant, so
a holder may call another holder's code for the same system. Process-local,
like the catalog: the app runs one task.

Each outermost holder says what it is (``holder``). A provision holds the lock
through runtime creation (minutes), so an admin Edit or scan-budget change
passes ``fail_fast_behind=(PROVISION,)``: while a provision holds the lock it
gets :class:`ConnectionBusyError` with ``holder == PROVISION`` at once instead
of waiting out the timeout. Behind another change (an Edit) it still waits, so
two edits never lose one another's update.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

#: How long a connection change waits for another change of the same system.
#: Below the 300 s ECS stop grace, and well above an Edit's bounded calls.
CONNECTION_LOCK_TIMEOUT_SECONDS = 120.0

#: Holder kinds (``connection_lock(holder=...)``).
CHANGE = "change"
PROVISION = "provision"
#: A per-system removal publishing its running claim (held only for that
#: brief check-and-publish, never across the cascade).
RESET = "reset"

#: How often a waiter that fails fast re-checks who holds the lock.
_FAIL_FAST_POLL_SECONDS = 0.1

_REGISTRY_LOCK = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_HOLDERS: dict[str, str] = {}


class ConnectionBusyError(RuntimeError):
    """Another change of this system's connection holds its lock. ``holder``
    is what held it when the wait ended (:data:`PROVISION`, :data:`CHANGE`,
    or ``""`` when it was released meanwhile)."""

    def __init__(self, system_id, holder: str = ""):
        super().__init__(system_id)
        self.holder = holder


def _lock_for(system_id: str) -> threading.RLock:
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(system_id)
        if lock is None:
            lock = _LOCKS[system_id] = threading.RLock()
        return lock


def holder_of(system_id: str) -> str:
    """What holds ``system_id``'s lock now (``""`` when nothing does)."""
    with _REGISTRY_LOCK:
        return _HOLDERS.get(str(system_id or ""), "")


def _acquire(lock: threading.RLock, key: str, wait: float, fail_fast_behind) -> bool:
    """Take ``lock`` within ``wait`` seconds; give up at once while a holder
    named in ``fail_fast_behind`` holds it (re-checked every
    :data:`_FAIL_FAST_POLL_SECONDS`)."""
    if not fail_fast_behind or lock._is_owned():  # noqa: SLF001 - re-entry never waits
        return lock.acquire(timeout=wait)
    import time
    deadline = time.monotonic() + wait
    while True:
        if holder_of(key) in fail_fast_behind:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if lock.acquire(timeout=min(_FAIL_FAST_POLL_SECONDS, remaining)):
            return True


@contextmanager
def connection_lock(system_id: str, timeout: float | None = None, *,
                    holder: str = CHANGE, fail_fast_behind: tuple = ()):
    """Hold ``system_id``'s connection lock for the ``with`` body; raises
    :class:`ConnectionBusyError` when it cannot be had within ``timeout``
    (default :data:`CONNECTION_LOCK_TIMEOUT_SECONDS`), or at once while a
    holder kind in ``fail_fast_behind`` holds it. ``holder`` names this
    holder (recorded for the outermost hold only)."""
    key = str(system_id or "")
    lock = _lock_for(key)
    wait = CONNECTION_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    reentered = lock._is_owned()  # noqa: SLF001 - RLock owner check
    if not _acquire(lock, key, wait, fail_fast_behind):
        raise ConnectionBusyError(system_id, holder_of(key))
    if not reentered:
        with _REGISTRY_LOCK:
            _HOLDERS[key] = holder
    try:
        yield
    finally:
        if not reentered:
            with _REGISTRY_LOCK:
                _HOLDERS.pop(key, None)
        lock.release()


def is_held(system_id: str) -> bool:
    """True when the CURRENT thread holds ``system_id``'s connection lock."""
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(str(system_id or ""))
    return bool(lock is not None and lock._is_owned())  # noqa: SLF001 - RLock owner check
