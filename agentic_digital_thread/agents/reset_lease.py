# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The reset lease and the provision leases: rows in the shared reset store
(``JobStore("reset")``, on the onboarding-sessions table) through which a
removal, a reset of all systems and a provision exclude each other in every
process: the app's task (two of them during a rolling deploy) and the
onboarding runtime container.

* The reset lease (:data:`RESET_LEASE_KEY`) is ONE row. A removal or Reset
  All takes it with a conditional write before it removes anything (scope:
  the system id, or :data:`ALL`) and gives it back, owner-checked, when it
  ends. The onboarding writers refuse while a live one covers their system.
* A provision takes its system's provision lease (:func:`provision_key`)
  before its first write and gives it back when it ends; a removal of that
  system and Reset All refuse while one is live. The Step Functions
  provisioner path (origin :data:`PROVISIONER`) finishes in another process
  and gives nothing back: its lease counts only while the system's
  onboarding session still says ``provisioning``.

Every lease carries an explicit ``expires_at``; an expired lease counts as
absent, so one whose release failed or whose owner died stops counting by
itself. Each side writes its own lease before it reads the other's, so of
two that start together at least one sees the other.

While its operation runs, the process that took a lease renews it
(:class:`Renewal`): every :data:`RENEW_SECONDS` it moves ``expires_at`` on
with a write that lands only while the lease is still its own and still
live. A lease whose renewal is refused, or that has not been renewed by
:data:`LOST_MARGIN_SECONDS` before its ``expires_at``, is LOST: from then on
:meth:`Renewal.check` raises :class:`LeaseLostError`, and the operation's
graph writes refuse (``db.write_guard``), so it stops before its next write.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

RESET_LEASE_KEY = "__reset_lease__"
PROVISION_LEASE_PREFIX = "__provision_lease__:"
#: The reset lease's scope while all systems are being reset.
ALL = "all"
#: Origins of a provision lease.
APP = "app"
PROVISIONER = "provisioner"
#: Wait between attempts to give a lease back (doubled each time).
RELEASE_BACKOFF_S = 0.2
#: This process, in every owner token it writes.
PROCESS_TOKEN = uuid.uuid4().hex[:12]
#: How often a held lease is renewed.
RENEW_SECONDS = 60.0
#: A renewal whose write failed is tried again this soon.
RENEW_RETRY_SECONDS = 10.0
#: A lease counts as lost this long before its ``expires_at`` unless renewed
#: by then: the margin covers clock skew between processes and the time a
#: write takes, so the operation stops before another process can count the
#: lease absent.
LOST_MARGIN_SECONDS = 60
#: A lease is renewed for at most this many times its own length after it
#: was taken (:class:`Renewal`).
MAX_RENEW_FACTOR = 4


def now() -> int:
    """The time leases are read against, in epoch seconds."""
    return int(time.time())


def new(scope: str, seconds: int, **fields) -> dict:
    """A lease of *scope* for an operation starting now: a fresh owner token
    (this process and the operation), ``started_at`` and ``expires_at``
    *seconds* later, and *fields*."""
    started = now()
    return {"owner": f"{PROCESS_TOKEN}-{uuid.uuid4().hex[:12]}", "scope": scope,
            "started_at": started, "expires_at": started + int(seconds), **fields}


def provision_key(system_id: str) -> str:
    return PROVISION_LEASE_PREFIX + str(system_id)


def release(store, key: str, lease: dict) -> bool:
    """Give *lease* back (owner-checked, retried). A release that keeps
    failing is logged; the lease then stops counting at its expires_at."""
    released = store.lease_release(key, lease["owner"], backoff_s=RELEASE_BACKOFF_S)
    if not released:
        logger.error("reset_lease_not_released: %s (scope %s) was not given back; it stops "
                     "counting at %s", key, lease.get("scope"), lease.get("expires_at"))
    return released


def reset_holding(store, system_id: str, *, shared_only: bool = False) -> "str | None":
    """:data:`ALL` while a live reset lease covers every system, *system_id*
    while one covers that system, else ``None``. Raises
    ``JobStoreUnavailableError`` when the lease cannot be read."""
    lease = store.lease_read(RESET_LEASE_KEY, now=now(), shared_only=shared_only)
    scope = (lease or {}).get("scope")
    if scope == ALL or (scope and scope == system_id):
        return scope
    return None


def provision_holding(store, system_id: str, session_status, *,
                      shared_only: bool = False) -> bool:
    """Whether a provision of *system_id* holds its live provision lease. A
    :data:`PROVISIONER` lease counts only while ``session_status(system_id)``
    (the onboarding session's status, read consistently) is
    ``provisioning``. Raises when a read fails."""
    lease = store.lease_read(provision_key(system_id), now=now(), shared_only=shared_only)
    if not lease:
        return False
    if lease.get("origin") == PROVISIONER:
        return session_status(system_id) == "provisioning"
    return True


def take_provision(store, system_id: str, origin: str, seconds: int, session_status) -> "dict | None":
    """Take *system_id*'s provision lease for a provision starting now: the
    lease taken, or ``None`` while another provision of it holds a live one.
    A :data:`PROVISIONER` lease whose session has moved on is taken over.
    Raises ``JobStoreUnavailableError`` when the table cannot be written."""
    lease = new(system_id, seconds, origin=origin)
    key = provision_key(system_id)
    held = store.lease_acquire(key, lease, now=now())
    if held is not None and held.get("origin") == PROVISIONER and \
            session_status(system_id) != "provisioning":
        held = store.lease_acquire(key, lease, now=now(), replace_owner=held.get("owner", ""))
    return None if held is not None else lease


class LeaseLostError(BaseException):
    """A lease this process took is no longer known to be its own: its
    renewal was refused, it was not renewed in time, or its operation gave it
    back. Another process may hold it now, so the operation stops.

    A ``BaseException``, as ``asyncio.CancelledError`` is, so the
    ``except Exception`` boundaries of best-effort steps (a concept seed, a
    post-provision pass) cannot absorb it and let the operation go on: only
    a handler that names it (the jobs that record why they stopped) or a
    cleanup that re-raises sees it."""


class Renewal:
    """Keeps one lease this process took alive while its operation runs.

    :meth:`start` starts a daemon thread that renews the lease every
    :data:`RENEW_SECONDS` (:data:`RENEW_RETRY_SECONDS` after a write that
    failed), and :meth:`stop` ends it before the lease is given back. A
    renewal lands only while the lease is still this owner's and still live
    (``JobStore.lease_renew``), so it never brings back a lease that expired
    or was given back. No lease is renewed past ``max_seconds`` after it was
    taken (default :data:`MAX_RENEW_FACTOR` times ``seconds``): a claim that
    was never released (a bug, a cancelled request) then stops counting as
    it did before renewals, and its operation, if it still runs, stops at its
    next write. ``on_renewed(lease)`` runs after each renewal that
    landed, under the renewal's lock, and never once :meth:`stop` has
    returned: a record that mirrors the lease's ``expires_at`` moves on with
    it, and the record's terminal write, made after :meth:`stop`, is never
    overwritten."""

    def __init__(self, store, key: str, lease: dict, seconds: int, *, on_renewed=None,
                 max_seconds: "int | None" = None):
        self.store = store
        self.key = key
        self.lease = dict(lease)
        self._seconds = int(seconds)
        started = int(self.lease.get("started_at") or now())
        self._renew_until = started + int(max_seconds if max_seconds is not None
                                          else MAX_RENEW_FACTOR * self._seconds)
        self._on_renewed = on_renewed
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._lost = False

    def start(self) -> "Renewal":
        threading.Thread(target=self._run, daemon=True,
                         name=f"lease-renewal-{self.lease.get('scope')}").start()
        return self

    def _run(self) -> None:
        wait = RENEW_SECONDS
        while not self._stopped.wait(wait):
            renewed = self.renew_once()
            if renewed is None:
                return
            wait = RENEW_SECONDS if renewed else RENEW_RETRY_SECONDS

    def held(self) -> bool:
        """Whether the lease is still this operation's: not stopped, not
        lost, and its last confirmed ``expires_at`` more than
        :data:`LOST_MARGIN_SECONDS` away. No I/O."""
        if self._stopped.is_set() or self._lost:
            return False
        return now() < int(self.lease["expires_at"]) - LOST_MARGIN_SECONDS

    def check(self) -> None:
        """Raise :class:`LeaseLostError` unless the lease is :meth:`held`."""
        if not self.held():
            raise LeaseLostError(f"{self.key} (scope {self.lease.get('scope')}) is no longer "
                                 "held by this process")

    def renew_once(self) -> "bool | None":
        """One renewal: True once it landed, False when its write failed (try
        again soon), None when the lease is lost or stopped (nothing more to
        renew)."""
        if not self.held():
            self._mark_lost("it was not renewed before its expiry margin")
            return None
        if now() >= self._renew_until:
            self._mark_lost("it reached the longest time a lease is renewed")
            return None
        lease = {**self.lease, "expires_at": min(now() + self._seconds, self._renew_until)}
        try:
            landed = self.store.lease_renew(self.key, lease, now=now())
        except Exception as exc:  # noqa: BLE001 - retried soon; held() bounds how long
            logger.warning("lease_renewal_failed: %s (scope %s): %s; retrying in %ss", self.key,
                           lease.get("scope"), type(exc).__name__, RENEW_RETRY_SECONDS)
            return False
        if not landed:
            self._mark_lost("its renewal was refused (not this owner's, expired or gone)")
            return None
        with self._lock:
            if self._stopped.is_set():
                return None
            self.lease = lease
            self._renewed(lease)
        return True

    def _renewed(self, lease: dict) -> None:
        """``on_renewed``, logged when it fails (the lease itself was renewed).
        Called with the lock held."""
        if self._on_renewed is None:
            return
        try:
            self._on_renewed(lease)
        except Exception as exc:  # noqa: BLE001 - the lease is renewed; the next renewal retries
            logger.warning("lease_renewal_record_failed: %s (scope %s): %s", self.key,
                           lease.get("scope"), type(exc).__name__)

    def _mark_lost(self, why: str) -> None:
        if self._stopped.is_set():
            return
        self._lost = True
        logger.error("lease_lost: %s (scope %s): %s; its operation stops before its next write",
                     self.key, self.lease.get("scope"), why)

    def stop(self) -> None:
        """End the renewal; returns once no ``on_renewed`` runs or will run."""
        with self._lock:
            self._stopped.set()
