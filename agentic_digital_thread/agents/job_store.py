# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Instance-independent async-job store for the onboarding map/provision jobs.

WHY (B1 root cause): the onboarding ``/map`` and ``/provision`` endpoints run the
work in a BACKGROUND THREAD and return a ``job_id`` the client polls via
``/status/{job_id}``. That job state was a per-PROCESS in-memory ``dict``. On a
multi-replica deploy the status poll can be routed by the load balancer to a
DIFFERENT replica than the one that started the job — which has no such
``job_id`` — so it returns ``"Unknown job_id"``. The client then never receives
the map result and provisions with EMPTY mappings, leaving a ``System`` node with
no ``HAS_SCHEMA`` subgraph, and ``get_system_schema`` reports "No schema found".
(Reproduced in a single process: with the job state visible, the Athena schema
persists correctly — so the fix is job-state VISIBILITY across instances, not the
schema-write code.)

THE FIX: :class:`JobStore` is a dict-like KV that write-throughs to the SHARED
onboarding-sessions DynamoDB table (no new infrastructure — the same durable
store the onboarding session state already uses), namespaced by ``kind`` +
``job_id`` with a short TTL. A job written on one replica is therefore readable
on any other. When DynamoDB is unavailable (local/offline/unit, or on any backend
error) it degrades FAIL-SOFT to an in-memory ``dict`` so the onboarding flow is
never broken (and single-process behaviour is byte-identical to the old dict).

The dict-like surface (``store[jid] = state`` / ``store[jid]`` / ``store.get`` /
``store.pop`` / ``del store[jid]`` / ``jid in store``) means existing call sites
and tests that treated ``_map_jobs`` / ``_provision_jobs`` as plain dicts keep
working unchanged.

This module ALSO hosts one narrow, unrelated-in-spirit primitive at the bottom:
:func:`claim_budget_slots` — an ATOMIC, fail-CLOSED conditional claim over a
dedicated budget item on the same table. It is deliberately NOT part of
:class:`JobStore` (whose blob-in-one-attribute, fail-soft design cannot express
an attribute-level condition and must never authorise spend); see the section
header there for the full rationale.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Reuse the onboarding-sessions table (already deployed) — no new infrastructure.
from .onboarding_session import TABLE_NAME as _SESSIONS_TABLE_NAME

# Short TTL: async onboarding jobs are consumed within minutes; reap abandoned
# job rows quickly so they never accumulate alongside the session items.
_JOB_TTL_SECONDS = 3600  # 1 hour

_KEY_ATTR = "system_id"          # the sessions table partition key (reused)
_STATE_ATTR = "job_state"        # JSON-encoded job state blob
_TTL_ATTR = "ttl"


_UNSET = object()

#: A lease row's top-level attributes, beside its JSON state (which carries
#: them too): the condition expressions below read them.
_LEASE_OWNER_ATTR = "lease_owner"
_LEASE_EXPIRES_ATTR = "lease_expires_at"
#: ``lease_acquire``'s condition: no lease, an expired one, or one held by
#: the owner the caller may replace (``:replace``, :data:`_NO_OWNER` when none).
#: Expired means ``expires_at <= now``, as every reader counts it (:func:`_lease_live`).
LEASE_TAKE_CONDITION = (f"attribute_not_exists({_KEY_ATTR}) OR {_LEASE_EXPIRES_ATTR} <= :now "
                        f"OR {_LEASE_OWNER_ATTR} = :replace")
#: ``lease_release``'s condition: the lease is still this owner's.
LEASE_OWNER_CONDITION = f"{_LEASE_OWNER_ATTR} = :owner"
#: ``lease_renew``'s condition: the lease is still this owner's and still
#: live, so a renewal never brings back a lease that expired (another
#: process may have counted it absent) or was given back.
LEASE_RENEW_CONDITION = f"{_LEASE_OWNER_ATTR} = :owner AND {_LEASE_EXPIRES_ATTR} > :now"
_NO_OWNER = "-"
#: Serializes a memory-only store's lease check-and-set.
_LEASE_LOCK = threading.Lock()


def _lease_live(lease: Any, now: int) -> bool:
    """Whether *lease* (a lease's state) is still live at *now*."""
    if not isinstance(lease, dict):
        return False
    try:
        return int(lease.get("expires_at") or 0) > int(now)
    except (TypeError, ValueError):
        return False


def _item_ttl(item: dict) -> "float | None":
    """A table item's top-level ``ttl`` in epoch seconds, or ``None`` when it
    has none that reads as a finite number. The boto3 resource layer gives a
    ``Decimal``; the low-level client (``get_many``) a ``{"N": "..."}`` map."""
    ttl = item.get(_TTL_ATTR)
    if isinstance(ttl, dict):
        ttl = ttl.get("N")
    if ttl is None or isinstance(ttl, bool):
        return None
    try:
        value = float(ttl)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return value if math.isfinite(value) else None


def _ttl_passed(ttl: "float | None", now: float) -> bool:
    """DynamoDB TTL semantics: an item whose ``ttl`` is at or before *now* has
    expired. DynamoDB deletes it some time later (it can be hours) and serves
    it to every read until then, so a read must decide this itself. An item
    with no readable ``ttl`` never expires."""
    return ttl is not None and ttl <= now


class JobStoreUnavailableError(RuntimeError):
    """The shared table could not be read or written (or there is none):
    raised by the strict accessors (:meth:`JobStore.get_strict`,
    :meth:`JobStore.put_strict`), never by the fail-soft dict surface."""


_MISSING = object()
#: ``_BoundedCopies.put``'s default expiry: ``ttl_seconds`` from now. Distinct
#: from ``None``, which means the copy has no expiry (a table item with no
#: readable ``ttl``, which never expires on read either).
_DEFAULT_EXPIRY = object()


class _BoundedCopies(MutableMapping):
    """A :class:`JobStore`'s in-memory copies when it has a ``memory_cap``.

    A mapping that holds a plain dict and, for each copy ``evictable(state)``
    accepts, its expiry, in write order. Two bounds apply to those copies only:
    one is dropped once its expiry is at or before ``clock()`` (by default the
    store's ``ttl_seconds`` after it was written, as the table item's ``ttl``
    is; a copy cached from a table read keeps that item's own ``ttl``), and
    once more than ``cap`` are held the oldest written is dropped. A re-write
    makes a copy the newest. A copy the predicate refuses never expires and is
    never counted or dropped. Pure memory: the shared table is never touched.

    Composition, not a dict subclass, so EVERY mutator (item assignment,
    ``del``, ``pop``, ``popitem``, ``setdefault``, ``update``, ``|=``,
    ``clear``) goes through the bounds, under one lock. Reads treat an expired
    copy as absent, and iteration, ``items``/``keys``/``values`` and ``copy``
    read a snapshot of one moment (a plain dict), so a reader never sees a
    write half done. The predicate and the clock are called outside the lock."""

    def __init__(self, cap: int, evictable, ttl_seconds: int, clock) -> None:
        self._cap = max(0, int(cap))
        self._evictable = evictable
        self._ttl = int(ttl_seconds)
        self._clock = clock
        self._data: dict = {}
        self._expiry: dict = {}         # accepted keys -> expiry, oldest write first
        self._lock = threading.Lock()

    def _accepts(self, state: Any) -> bool:
        try:
            return isinstance(state, dict) and bool(self._evictable(state))
        except Exception:  # noqa: BLE001 - an unreadable copy is kept, never dropped
            return False

    # -- under the lock ----------------------------------------------------- #
    def _drop_locked(self, key) -> Any:
        self._expiry.pop(key, None)
        return self._data.pop(key, _MISSING)

    def _live_locked(self, key, now: float) -> Any:
        """The copy of *key*, or ``_MISSING``; an expired one is dropped."""
        if _ttl_passed(self._expiry.get(key), now):
            self._drop_locked(key)
            return _MISSING
        return self._data.get(key, _MISSING)

    def _sweep_locked(self, now: float) -> None:
        for key in [k for k, at in self._expiry.items() if _ttl_passed(at, now)]:
            self._drop_locked(key)
        while len(self._expiry) > self._cap:
            self._drop_locked(next(iter(self._expiry)))

    def _put_locked(self, key, state, accepted: bool, expires_at: float, now: float) -> None:
        self._data.pop(key, None)       # a re-write is the newest write
        self._data[key] = state
        self._expiry.pop(key, None)
        if accepted:
            self._expiry[key] = expires_at
        self._sweep_locked(now)

    def _snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            self._sweep_locked(now)
            return dict(self._data)

    # -- writes ------------------------------------------------------------- #
    def put(self, key, state, *, expires_at: Any = _DEFAULT_EXPIRY) -> None:
        """Store *state*; an accepted copy expires at *expires_at* (the
        ``ttl`` its table item has or was just given), never when it is
        ``None`` (an item with no ``ttl``; the count bound still applies),
        and by default ``ttl_seconds`` from now."""
        accepted = self._accepts(state)
        now = self._clock()
        expiry = int(now) + self._ttl if expires_at is _DEFAULT_EXPIRY else expires_at
        with self._lock:
            self._put_locked(key, state, accepted, expiry, now)

    def __setitem__(self, key, state) -> None:
        self.put(key, state)

    def setdefault(self, key, default=None):
        accepted = self._accepts(default)
        now = self._clock()
        with self._lock:
            current = self._live_locked(key, now)
            if current is not _MISSING:
                return current
            self._put_locked(key, default, accepted, int(now) + self._ttl, now)
            return default

    def update(self, other=(), /, **kwargs) -> None:
        pairs = ([(k, other[k]) for k in list(other.keys())] if hasattr(other, "keys")
                 else list(other))
        for key, state in [*pairs, *kwargs.items()]:
            self.put(key, state)

    def __ior__(self, other):
        self.update(other)
        return self

    def __delitem__(self, key) -> None:
        now = self._clock()
        with self._lock:
            expired = _ttl_passed(self._expiry.get(key), now)
            if self._drop_locked(key) is _MISSING or expired:
                raise KeyError(key)

    def pop(self, key, default=_MISSING):
        now = self._clock()
        with self._lock:
            expired = _ttl_passed(self._expiry.get(key), now)
            value = self._drop_locked(key)
        if value is _MISSING or expired:
            if default is _MISSING:
                raise KeyError(key)
            return default
        return value

    def popitem(self):
        """Remove and return the newest written live copy, as ``dict`` does."""
        now = self._clock()
        with self._lock:
            self._sweep_locked(now)
            if not self._data:
                raise KeyError("popitem(): no copies")
            key = next(reversed(self._data))
            return key, self._drop_locked(key)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._expiry.clear()

    # -- reads -------------------------------------------------------------- #
    def __getitem__(self, key):
        now = self._clock()
        with self._lock:
            value = self._live_locked(key, now)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __contains__(self, key) -> bool:
        now = self._clock()
        with self._lock:
            return self._live_locked(key, now) is not _MISSING

    def __iter__(self):
        return iter(list(self._snapshot()))

    def __len__(self) -> int:
        return len(self._snapshot())

    def keys(self):
        return self._snapshot().keys()

    def items(self):
        return self._snapshot().items()

    def values(self):
        return self._snapshot().values()

    def copy(self) -> dict:
        return self._snapshot()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._snapshot()!r})"


class JobStore:
    """Dict-like async-job store, shared across replicas via DynamoDB (fail-soft).

    Args:
        kind: logical namespace (e.g. ``"map"`` / ``"provision"``) so different
            job families never collide on the same ``job_id``.
        table: an explicit DynamoDB Table resource (mainly for tests). When
            ``None`` the table is resolved lazily from the environment; if that
            fails, the store runs in-memory only.
        ttl_seconds: DynamoDB TTL horizon for items of this kind. Defaults to
            the short async-job TTL (``_JOB_TTL_SECONDS`` — jobs are consumed
            within minutes); kinds with a deliberately longer lifetime (e.g.
            the provenance-query store) pass their own value. Additive — the
            default keeps every existing kind byte-identical.
        memory_cap: keep at most this many in-memory copies that
            ``memory_evictable`` accepts, dropping the oldest written first,
            and drop such a copy once the store's TTL has passed for it (the
            ``ttl`` its table item would carry, or that item's own ``ttl`` when
            the copy came from a table read) (:class:`_BoundedCopies`). Only
            this process's copy is dropped, never the shared table's item. On
            a memory-only store the copy is the record, so this is its expiry.
            ``None`` (the default) keeps the plain dict, byte-identical for
            every other kind.
        memory_evictable: ``state -> bool``, which copies ``memory_cap`` counts
            and may drop. Required with ``memory_cap``.
        clock: epoch seconds for this store's TTL stamps and expiry checks (a
            seam). ``None`` (the default) reads ``time.time()`` at each call.
        client_config: a ``botocore.config.Config`` for the DynamoDB resource
            the store builds for the shared table (fail-soft and strict paths
            alike): a kind whose callers answer a request within a bound
            passes its timeouts and attempts here. ``None`` (the default)
            builds the resource exactly as before, with botocore's defaults,
            for every other kind. An injected ``table`` is used as given.

    Every read of the shared table (``get``, ``get_many``, ``get_strict``)
    treats an item whose ``ttl`` is at or before now as absent: DynamoDB
    deletes an expired item lazily and serves it until then. Leases are read
    by their own ``expires_at`` and ``mutate`` by the revision it writes
    against; see :meth:`lease_read` and :meth:`mutate`.
    """

    def __init__(self, kind: str, *, table: Any | None = _UNSET,
                 ttl_seconds: int = _JOB_TTL_SECONDS,
                 memory_cap: "int | None" = None, memory_evictable=None,
                 clock=None, client_config: Any | None = None) -> None:
        self._kind = kind
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._client_config = client_config
        if memory_cap is not None and memory_evictable is None:
            raise ValueError("memory_cap needs memory_evictable")
        self._mem: dict[str, dict] = (
            {} if memory_cap is None
            else _BoundedCopies(memory_cap, memory_evictable, ttl_seconds, self._now))
        self._table = table if table is not _UNSET else None
        # When an explicit table (incl. None) is passed, honour it and do not
        # attempt lazy env resolution. Only auto-resolve when nothing was passed.
        self._resolve_table = table is _UNSET
        self._backend_ok = True

    def _now(self) -> float:
        """Epoch seconds: the injected clock, else ``time.time()`` read now."""
        return self._clock() if self._clock is not None else time.time()

    def _cache(self, job_id: str, state: dict, ttl: "float | None") -> None:
        """Keep *state* as this process's copy, read from or just written to
        the table with *ttl*. A bounded copy expires at that same *ttl*, so
        the memory fallback never answers past the table's horizon; ``None``
        (an item with no ``ttl``) never expires, as the item does not on read."""
        if isinstance(self._mem, _BoundedCopies):
            self._mem.put(job_id, state, expires_at=ttl)
        else:
            self._mem[job_id] = state

    def _new_ttl(self) -> int:
        """The ``ttl`` a write stamps: ``ttl_seconds`` from the store's clock.
        Read ONCE per write and given to both the table item and the memory
        copy, so a table write that takes long enough for the clock to cross
        a second cannot leave the copy outliving the item."""
        return int(self._now()) + self._ttl_seconds

    # -- backend plumbing --------------------------------------------------- #
    def _key(self, job_id: str) -> str:
        return f"__job__:{self._kind}:{job_id}"

    def _open_table(self):
        """The shared table's resource, built from the environment's Region:
        ``boto3.resource("dynamodb", region_name=...)`` exactly as before
        when the store has no ``client_config``, else with it."""
        import boto3
        region = os.environ.get("AWS_REGION", "us-east-1")
        if self._client_config is None:
            resource = boto3.resource("dynamodb", region_name=region)
        else:
            resource = boto3.resource("dynamodb", region_name=region,
                                      config=self._client_config)
        return resource.Table(_SESSIONS_TABLE_NAME)

    def _get_table(self):
        """Return the DynamoDB Table, or ``None`` if unavailable (fail-soft).

        When no explicit table was injected, the shared backend is used ONLY if
        ``ONBOARDING_SESSIONS_TABLE`` is set in the environment — the signal that
        a durable, cross-replica sessions table is provisioned for this process
        (deployed multi-replica). Absent it (local dev / unit / single-replica
        default) the store stays in-memory, byte-identical to the previous dict.
        """
        if not self._backend_ok:
            return None
        if self._table is not None:
            return self._table
        if not self._resolve_table:
            return None
        if not os.environ.get("ONBOARDING_SESSIONS_TABLE", "").strip():
            # Stable-key breadcrumb: this silent degradation hid the live
            # cloud gap where provenance-query redemption 404'd across
            # workers/replicas because the app env lacked the table signal.
            logger.info(
                "job_store_memory_only: JobStore(%s) has no "
                "ONBOARDING_SESSIONS_TABLE — state is per-process only",
                self._kind,
            )
            self._backend_ok = False  # no durable table configured -> memory only
            return None
        try:
            self._table = self._open_table()
            return self._table
        except Exception as exc:  # nosec B110 - fail-soft to in-memory
            logger.debug(
                "JobStore(%s): DynamoDB unavailable category=%s",
                self._kind, type(exc).__name__,
            )
            self._backend_ok = False
            return None

    def _disable_backend(self, exc: Exception) -> None:
        # A backend error must never break onboarding — degrade to memory-only.
        # Emit a stable-key WARNING ('job_store_fallback') so an empty-mappings
        # provision (a map job whose result was lost because a /status poll hit a
        # replica that only has the in-memory copy) is diagnosable in CloudWatch.
        logger.warning(
            "job_store_fallback: JobStore(%s) backend category=%s; "
            "map/provision state may be unavailable across replicas",
            self._kind, type(exc).__name__,
        )
        self._backend_ok = False

    def _remote_get(self, job_id: str):
        """Read from the backend.

        Returns ``(state, ttl)`` (the decoded state and the item's ``ttl``),
        ``_UNSET`` when the backend is reachable but has no such job (or the
        item's ``ttl`` has passed: DynamoDB deletes it later, it is already
        gone), or ``None`` when there is NO backend / the read errored (caller
        falls back to the in-memory cache in that case).
        """
        table = self._get_table()
        if table is None:
            return None  # no backend -> caller uses memory
        try:
            resp = table.get_item(Key={_KEY_ATTR: self._key(job_id)})
        except Exception as exc:  # nosec B110 - fail-soft
            self._disable_backend(exc)
            return None  # backend errored -> caller uses memory
        item = resp.get("Item")
        if not item or _STATE_ATTR not in item:
            return _UNSET  # backend authoritative: job genuinely absent
        ttl = _item_ttl(item)
        if _ttl_passed(ttl, self._now()):
            return _UNSET  # expired, not yet deleted: absent all the same
        try:
            return json.loads(item[_STATE_ATTR]), ttl
        except (ValueError, TypeError):
            return _UNSET

    def _remote_put(self, job_id: str, state: dict,
                    condition: "dict | None" = None, *, extra: "dict | None" = None,
                    ttl: "int | None" = None) -> bool:
        """Write *state*; with *condition* (boto3 ``ConditionExpression`` kwargs)
        the write is conditional and ``False`` means the condition refused it.
        *extra* adds top-level item attributes (``mutate``'s revision); *ttl*
        is the item's ``ttl`` (:meth:`_new_ttl` when ``None``).

        Returns ``True`` when the write landed OR there is no backend / the
        backend errored (fail-soft, the caller keeps its in-memory copy).
        """
        table = self._get_table()
        if table is None:
            return True
        item = self._item(job_id, state, extra, ttl=ttl)
        try:
            table.put_item(Item=item, **(condition or {}))
        except Exception as exc:  # nosec B110 - fail-soft; job still tracked in-memory
            if condition is not None and _is_conditional_failure(exc):
                return False
            self._disable_backend(exc)
        return True

    def _item(self, job_id: str, state: dict, extra: "dict | None" = None, *,
              ttl: "int | None" = None) -> dict:
        item = {
            _KEY_ATTR: self._key(job_id),
            _STATE_ATTR: json.dumps(state, default=str),
            _TTL_ATTR: self._new_ttl() if ttl is None else ttl,
        }
        item.update(_slot_attrs(state))
        item.update(extra or {})
        return item

    # -- strict access: the shared table or an error ------------------------ #
    def has_shared_table(self) -> bool:
        """Whether this store is backed by a shared table at all (an injected
        one, or ``ONBOARDING_SESSIONS_TABLE`` configured), WITHOUT opening it,
        so it never raises. ``False`` is the memory-only configuration (local
        dev / unit / single-replica), where there are no durable records and a
        caller may treat a durable read's ``None`` as an authoritative
        absence rather than a transient failure."""
        return self._table is not None or (
            self._resolve_table
            and bool(os.environ.get("ONBOARDING_SESSIONS_TABLE", "").strip()))

    def _strict_table(self):
        """The shared table for a read or write that must not fall back to
        memory: the injected table, or the configured one
        (``ONBOARDING_SESSIONS_TABLE``), whatever an earlier fail-soft error
        did to the fail-soft path. ``None`` when this store is memory-only by
        configuration. Raises :class:`JobStoreUnavailableError` when the
        configured table cannot be opened."""
        if self._table is not None:
            return self._table
        if not self._resolve_table or not os.environ.get("ONBOARDING_SESSIONS_TABLE", "").strip():
            return None
        try:
            self._table = self._open_table()
            return self._table
        except Exception as exc:
            raise JobStoreUnavailableError(
                f"JobStore({self._kind}): the shared table could not be opened "
                f"({type(exc).__name__})") from exc

    def get_strict(self, job_id: str) -> "dict | None":
        """Read *job_id* from the shared table: its state, or ``None`` when the
        table has no such job (or its item's ``ttl`` has passed). For a
        decision another process must be able to
        trust (another process's removal is running), so it never answers from
        memory and it reads with ``ConsistentRead`` (it sees every write the
        table acknowledged before it): with no shared table, a failed read or
        an unreadable item it raises :class:`JobStoreUnavailableError`. A
        failure is not remembered: the next call asks the table again."""
        return self._read_strict(job_id, honour_ttl=True)

    def _read_strict(self, job_id: str, *, honour_ttl: bool) -> "dict | None":
        """:meth:`get_strict`; ``honour_ttl=False`` returns an item whose
        ``ttl`` has passed too (a lease, read by its own ``expires_at``)."""
        table = self._strict_table()
        if table is None:
            raise JobStoreUnavailableError(f"JobStore({self._kind}) has no shared table")
        try:
            item = (table.get_item(Key={_KEY_ATTR: self._key(job_id)},
                                   ConsistentRead=True) or {}).get("Item")
        except Exception as exc:
            raise JobStoreUnavailableError(
                f"JobStore({self._kind}): read failed ({type(exc).__name__})") from exc
        if not item or _STATE_ATTR not in item:
            return None
        if honour_ttl and _ttl_passed(_item_ttl(item), self._now()):
            return None
        try:
            return json.loads(item[_STATE_ATTR])
        except (ValueError, TypeError) as exc:
            raise JobStoreUnavailableError(
                f"JobStore({self._kind}): unreadable state for {job_id!r}") from exc

    def put_strict(self, job_id: str, state: dict) -> None:
        """Write *state* so every process sharing the table sees it, or raise
        :class:`JobStoreUnavailableError` and keep nothing (memory is updated
        only once the table write landed). A write that lands shows the table
        is back, so the fail-soft surface uses it again. A store that is
        memory-only by configuration (no shared table) keeps the state in
        memory, as every write of it does."""
        table = self._strict_table()
        ttl = self._new_ttl()
        if table is not None:
            try:
                table.put_item(Item=self._item(job_id, state, ttl=ttl))
            except Exception as exc:
                raise JobStoreUnavailableError(
                    f"JobStore({self._kind}): write failed ({type(exc).__name__})") from exc
            self._backend_ok = True
        self._cache(job_id, state, ttl)

    def put_durable(self, job_id: str, state: dict, *,
                    ttl: "int | None" = None) -> bool:
        """Write *state* to the SHARED table only, returning ``True`` only when
        the backend acknowledged the write. Returns ``False`` when there is no
        shared table (memory-only configuration), the table cannot be opened,
        or the write throttles/errors, and in every ``False`` case it keeps
        NOTHING in process memory. Unlike :meth:`put_strict` it never raises
        and never caches a memory fallback, so a caller can treat a durable
        capability as present only when this returns ``True``. *ttl* overrides
        the store's default horizon (:meth:`_new_ttl`)."""
        try:
            table = self._strict_table()
        except JobStoreUnavailableError:
            return False
        if table is None:
            return False
        try:
            table.put_item(Item=self._item(job_id, state,
                                           ttl=self._new_ttl() if ttl is None else ttl))
        except Exception:  # nosec B110 - durable-or-False; no memory fallback
            return False
        self._backend_ok = True
        return True

    def get_durable(self, job_id: str) -> "dict | None":
        """Consistent SHARED-table read that never answers from memory and
        never raises: the state, or ``None`` when there is no shared table,
        the read fails, or the item is absent/expired/unreadable. A caller
        uses ``None`` as an authoritative 'no durable record here' only when a
        shared table is configured; a memory-only store always returns
        ``None`` (it has no durable records)."""
        try:
            return self._read_strict(job_id, honour_ttl=True)
        except JobStoreUnavailableError:
            return None

    # -- leases: a conditional claim with an owner and an explicit expiry ---- #
    def lease_read(self, job_id: str, *, now: int, shared_only: bool = False) -> "dict | None":
        """The lease *job_id* while it is live (its ``expires_at`` is after
        *now*), else ``None``: an expired lease reads as absent, whether or
        not it was ever given back. Read consistently, never from memory
        (:meth:`get_strict`), but by the lease's own ``expires_at`` and not
        its item's ``ttl``: ``expires_at`` is what :meth:`lease_acquire`'s
        conditional write tests, so the two always agree on whether a lease
        holds. A store that is memory-only by configuration is
        one process, so it answers from memory, unless *shared_only* (a
        process that shares no memory with the lease's holders), which then
        raises :class:`JobStoreUnavailableError`."""
        if self._strict_table() is None and not shared_only:
            with _LEASE_LOCK:
                lease = self._mem.get(job_id)
        else:
            lease = self._read_strict(job_id, honour_ttl=False)
        return lease if _lease_live(lease, now) else None

    def lease_acquire(self, job_id: str, lease: dict, *, now: int,
                      replace_owner: str = "") -> "dict | None":
        """Take the lease *job_id* for *lease* (its ``owner`` token and
        ``expires_at``, epoch seconds) with a conditional write that lands only
        while no lease is there, the one there has expired, or
        *replace_owner* holds it. ``None`` once taken; else the live lease
        that holds it. Raises :class:`JobStoreUnavailableError` when the
        table cannot be written or read. A write that lands shows the table
        is back, so the fail-soft surface uses it again."""
        table = self._strict_table()
        if table is None:
            with _LEASE_LOCK:
                held = self._mem.get(job_id)
                if _lease_live(held, now) and held.get("owner") != replace_owner:
                    return held
                self._mem[job_id] = lease
                return None
        item = self._item(job_id, lease, {_LEASE_OWNER_ATTR: lease["owner"],
                                          _LEASE_EXPIRES_ATTR: int(lease["expires_at"])})
        values = {":now": int(now), ":replace": replace_owner or _NO_OWNER}
        held = None
        for _attempt in range(3):
            try:
                table.put_item(Item=item, ConditionExpression=LEASE_TAKE_CONDITION,
                               ExpressionAttributeValues=values)
            except Exception as exc:
                if not _is_conditional_failure(exc):
                    raise JobStoreUnavailableError(
                        f"JobStore({self._kind}): lease write failed "
                        f"({type(exc).__name__})") from exc
                held = self.lease_read(job_id, now=now, shared_only=True)
                if held is not None:
                    return held
                continue  # given back or expired in between: try again
            self._backend_ok = True
            return None
        return held or {"owner": "", "scope": ""}

    def lease_renew(self, job_id: str, lease: dict, *, now: int) -> bool:
        """Move the lease *job_id* to *lease* (the same ``owner``, a later
        ``expires_at``) with a conditional write that lands only while that
        owner holds it and it is still live at *now*. True once renewed;
        False when it is not this owner's or has expired or is gone (the
        lease is lost: another process may hold it or may have counted it
        absent). Raises :class:`JobStoreUnavailableError` when the table
        cannot be written."""
        table = self._strict_table()
        if table is None:
            with _LEASE_LOCK:
                held = self._mem.get(job_id)
                if not _lease_live(held, now) or held.get("owner") != lease["owner"]:
                    return False
                self._mem[job_id] = lease
                return True
        item = self._item(job_id, lease, {_LEASE_OWNER_ATTR: lease["owner"],
                                          _LEASE_EXPIRES_ATTR: int(lease["expires_at"])})
        try:
            table.put_item(Item=item, ConditionExpression=LEASE_RENEW_CONDITION,
                           ExpressionAttributeValues={":owner": lease["owner"],
                                                      ":now": int(now)})
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise JobStoreUnavailableError(
                f"JobStore({self._kind}): lease renewal failed ({type(exc).__name__})") from exc
        self._backend_ok = True
        return True

    def lease_release(self, job_id: str, owner: str, *, attempts: int = 3,
                      backoff_s: float = 0.2, sleep=time.sleep) -> bool:
        """Give back the lease *job_id* only while *owner* holds it (a
        conditional delete), so a lease another owner took once this one
        expired is never removed. A failed write is retried, *attempts* times
        in all, waiting *backoff_s* (doubling) in between. True once the lease
        is gone or is not this owner's; False when every attempt failed: the
        lease then stops counting at its ``expires_at``, as one whose owner
        died does."""
        table = self._strict_table()
        if table is None:
            with _LEASE_LOCK:
                if (self._mem.get(job_id) or {}).get("owner") == owner:
                    self._mem.pop(job_id, None)
            return True
        for attempt in range(attempts):
            try:
                table.delete_item(Key={_KEY_ATTR: self._key(job_id)},
                                  ConditionExpression=LEASE_OWNER_CONDITION,
                                  ExpressionAttributeValues={":owner": owner})
            except Exception as exc:
                if _is_conditional_failure(exc):
                    return True  # not this owner's lease (or already gone)
                logger.warning("lease_release: JobStore(%s) %s attempt %d failed: %s",
                               self._kind, job_id, attempt + 1, type(exc).__name__)
                if attempt + 1 < attempts:
                    sleep(backoff_s * (2 ** attempt))
                continue
            self._backend_ok = True
            return True
        return False

    def _remote_delete(self, job_id: str) -> None:
        table = self._get_table()
        if table is None:
            return
        try:
            table.delete_item(Key={_KEY_ATTR: self._key(job_id)})
        except Exception as exc:  # nosec B110 - fail-soft
            self._disable_backend(exc)

    # -- dict-like surface -------------------------------------------------- #
    def __setitem__(self, job_id: str, state: dict) -> None:
        if isinstance(state, dict) and state.get("status") == "error":
            state = {**state, "error": f"{self._kind}_failed"}
        ttl = self._new_ttl()
        self._cache(job_id, state, ttl)
        self._remote_put(job_id, state, ttl=ttl)

    def get(self, job_id: str, default: Any = None) -> Any:
        remote = self._remote_get(job_id)
        if remote is _UNSET:
            # Backend is reachable and authoritative: the job is genuinely gone
            # (deleted/expired on another replica, or past its ttl and not yet
            # deleted). Drop any stale local cache so a poll here agrees with
            # the rest of the fleet.
            self._mem.pop(job_id, None)
            return default
        if remote is not None:
            state, ttl = remote
            self._cache(job_id, state, ttl)  # refresh local cache
            return state
        # remote is None: no backend / read errored -> in-memory fallback.
        return self._mem.get(job_id, default)

    def __getitem__(self, job_id: str) -> dict:
        val = self.get(job_id, _UNSET)
        if val is _UNSET:
            raise KeyError(job_id)
        return val

    def get_many(self, job_ids: "list[str]") -> "dict[str, dict]":
        """Read several jobs at once — ``{job_id: state}`` for those present.

        Remote: DynamoDB ``BatchGetItem`` in pages of 100 keys (one round-trip
        per page instead of one ``GetItem`` per key). Absent keys, and items
        whose ``ttl`` has passed, are dropped from the local cache exactly like
        :meth:`get`. No backend / a backend
        error falls back to the in-memory copies (fail-soft, as everywhere).
        """
        ids = [j for j in dict.fromkeys(job_ids) if isinstance(j, str)]
        if not ids:
            return {}
        table = self._get_table()
        if table is None:
            return self._mem_copies(ids)
        found: dict[str, dict] = {}
        ttls: dict = {}
        now = self._now()
        try:
            client = table.meta.client
            table_name = table.name
            for start in range(0, len(ids), 100):
                page = ids[start:start + 100]
                request = {table_name: {"Keys": [{_KEY_ATTR: self._key(j)} for j in page]}}
                while request:
                    resp = client.batch_get_item(RequestItems=request)
                    for item in (resp.get("Responses") or {}).get(table_name, []):
                        raw_key = item.get(_KEY_ATTR)
                        key = raw_key.get("S") if isinstance(raw_key, dict) else raw_key
                        state = item.get(_STATE_ATTR)
                        state = state.get("S") if isinstance(state, dict) else state
                        if not isinstance(key, str) or not isinstance(state, str):
                            continue
                        ttl = _item_ttl(item)
                        if _ttl_passed(ttl, now):
                            continue  # expired, not yet deleted: absent
                        job_id = key.split(":", 2)[-1]
                        try:
                            found[job_id] = json.loads(state)
                        except (ValueError, TypeError):
                            continue
                        ttls[job_id] = ttl
                    request = resp.get("UnprocessedKeys") or {}
        except Exception as exc:  # nosec B110 - fail-soft
            self._disable_backend(exc)
            return self._mem_copies(ids)
        for j in ids:
            if j in found:
                self._cache(j, found[j], ttls.get(j))
            else:
                self._mem.pop(j, None)
        return found

    def _mem_copies(self, ids: "list[str]") -> "dict[str, dict]":
        """This process's copies of *ids* (one read each, so a bounded copy
        that expires in between is simply left out)."""
        copies = {j: self._mem.get(j, _UNSET) for j in ids}
        return {j: s for j, s in copies.items() if s is not _UNSET}

    def __contains__(self, job_id: str) -> bool:
        return self.get(job_id, _UNSET) is not _UNSET

    def __delitem__(self, job_id: str) -> None:
        self._mem.pop(job_id, None)
        self._remote_delete(job_id)

    def pop(self, job_id: str, default: Any = _UNSET) -> Any:
        val = self.get(job_id, _UNSET)
        if val is _UNSET:
            if default is _UNSET:
                raise KeyError(job_id)
            return default
        self._mem.pop(job_id, None)
        self._remote_delete(job_id)
        return val

    # -- single-slot ownership (claim / exact-owner release) ---------------- #
    #
    # Several modules keep ONE "active" record per kind as a mutual-exclusion
    # slot (eval runs, feedback exploration, try-it, drift). A plain
    # read-then-``__setitem__`` check-then-set with an UNCONDITIONAL release
    # would let a stale worker release a NEWER worker's slot so two expensive
    # runs overlap. The two methods below make the decision atomic:
    #
    #   * remote: the slot's ``status`` / owner / ``started_at`` are mirrored
    #     as TOP-LEVEL item attributes (``_slot_attrs``) so DynamoDB can
    #     evaluate a ``ConditionExpression`` over them — the blob itself is
    #     opaque to it;
    #   * memory (no table — local dev, unit lane, single replica): the same
    #     predicate under a process lock, correct for ONE process.
    #
    # A backend error stays fail-soft (the module contract): the decision is
    # then made against the in-memory copy, exactly as every other JobStore
    # read/write degrades. ``strict=True`` is the exception for a slot that
    # must hold across app tasks: with a table configured, a backend error or
    # a backend the fail-soft path has disabled refuses (claim and touch
    # False, release False) and nothing is decided from memory; with no table
    # configured it is the same locked in-memory CAS.

    def _strict_slot_put(self, job_id: str, state: dict, condition: dict) -> bool:
        """A strict slot write to the shared table, under ``_SLOT_LOCK``:
        ``True`` only when the conditional write landed (the copy is then
        cached). A refused condition, a backend error or a disabled backend is
        ``False``; it never disables the backend, so a later strict call asks
        the table again."""
        if not self._backend_ok:
            return False
        try:
            table = self._strict_table()
        except JobStoreUnavailableError:
            return False
        ttl = self._new_ttl()
        try:
            table.put_item(Item=self._item(job_id, state, ttl=ttl), **condition)
        except Exception as exc:  # nosec B110 - strict: refused, never memory
            if not _is_conditional_failure(exc):
                logger.warning("job_store_strict_refused: JobStore(%s) slot backend "
                               "category=%s", self._kind, type(exc).__name__)
            return False
        self._cache(job_id, state, ttl)
        return True

    def _strict_slot(self, strict: bool) -> bool:
        """Whether a slot call goes to :meth:`_strict_slot_put`: strict, and a
        shared table configured (an injected one or ``ONBOARDING_SESSIONS_TABLE``)."""
        return strict and self.has_shared_table()

    def claim_slot(self, job_id: str, state: dict, *, stale_seconds: float,
                   now: "float | None" = None, strict: bool = False) -> bool:
        """Atomically take *job_id* unless a FRESH running holder exists.

        *state* is the record to write on success (``status: "running"`` with
        a ``run_id``/``job_id`` owner and ``started_at``). A holder whose
        ``started_at`` is older than *stale_seconds* is taken over. *strict*:
        see the section comment above; a strict claim on the shared table also
        takes a slot this same owner already holds, so botocore's retry of a
        claim that landed but whose answer was lost succeeds instead of
        refusing its own claim (each claim names a fresh owner, so only that
        retry can meet it). The fail-soft claim's condition is unchanged.
        """
        now = time.time() if now is None else now
        attrs = _slot_attrs(state)
        if attrs.get(_SLOT_STATUS_ATTR) != "running" or _SLOT_OWNER_ATTR not in attrs:
            raise ValueError("claim_slot state needs status='running' and an owner")
        stale_before = int(now - stale_seconds)
        condition = {
            "ConditionExpression": (
                f"attribute_not_exists(#k) OR attribute_not_exists(#st) "
                f"OR #st <> :running OR attribute_not_exists(#sa) "
                f"OR #sa < :stale_before"
            ),
            "ExpressionAttributeNames": {
                "#k": _KEY_ATTR, "#st": _SLOT_STATUS_ATTR, "#sa": _SLOT_STARTED_ATTR,
            },
            "ExpressionAttributeValues": {
                ":running": "running", ":stale_before": stale_before,
            },
        }
        with _SLOT_LOCK:
            if self._strict_slot(strict):
                return self._strict_slot_put(
                    job_id, state, _own_claim_condition(condition, attrs[_SLOT_OWNER_ATTR]))
            if self._get_table() is None:
                if not _memory_slot_free(self._mem.get(job_id), stale_before):
                    return False
                self._mem[job_id] = state
                return True
            ttl = self._new_ttl()
            if not self._remote_put(job_id, state, condition, ttl=ttl):
                return False
            if self._get_table() is None:  # backend just degraded mid-call
                if not _memory_slot_free(self._mem.get(job_id), stale_before):
                    return False
            self._cache(job_id, state, ttl)
            return True

    def release_slot(self, job_id: str, owner: str, state: dict, *,
                     strict: bool = False) -> bool:
        """Write the idle *state* iff *owner* still holds the slot.

        ``False`` (and NO write) when another owner has since taken it over —
        a finishing stale worker must never release a newer worker's claim.
        *strict*: see the section comment above.
        """
        condition = {
            "ConditionExpression": "attribute_not_exists(#k) OR #o = :me",
            "ExpressionAttributeNames": {"#k": _KEY_ATTR, "#o": _SLOT_OWNER_ATTR},
            "ExpressionAttributeValues": {":me": owner},
        }
        with _SLOT_LOCK:
            if self._strict_slot(strict):
                return self._strict_slot_put(job_id, state, condition)
            if self._get_table() is None:
                if not _memory_slot_owned_by(self._mem.get(job_id), owner):
                    return False
                self._mem[job_id] = state
                return True
            ttl = self._new_ttl()
            if not self._remote_put(job_id, state, condition, ttl=ttl):
                return False
            if self._get_table() is None:  # backend just degraded mid-call
                if not _memory_slot_owned_by(self._mem.get(job_id), owner):
                    return False
            self._cache(job_id, state, ttl)
            return True

    def touch_slot(self, job_id: str, owner: str, *,
                   now: "float | None" = None, strict: bool = False) -> bool:
        """Heartbeat: refresh *owner*'s running claim to ``started_at=now`` so
        a job that legitimately outlives ``stale_seconds`` is not taken over.

        ``False`` (and NO write) when *owner* no longer holds a running claim
        (released, or taken over here or on another replica). A ``True``
        return also means the claim cannot go stale for another
        ``stale_seconds``, so the owner may act on it right away. *strict*:
        see the section comment above.
        """
        now = time.time() if now is None else now
        condition = {
            "ConditionExpression": "#o = :me AND #st = :running",
            "ExpressionAttributeNames": {"#o": _SLOT_OWNER_ATTR, "#st": _SLOT_STATUS_ATTR},
            "ExpressionAttributeValues": {":me": owner, ":running": "running"},
        }
        with _SLOT_LOCK:
            current = self._mem.get(job_id)
            if (_slot_owner(current) != owner
                    or _slot_attrs(current).get(_SLOT_STATUS_ATTR) != "running"):
                return False
            state = {**current, "started_at": now}
            if self._strict_slot(strict):
                return self._strict_slot_put(job_id, state, condition)
            ttl = self._new_ttl()
            if not self._remote_put(job_id, state, condition, ttl=ttl):
                return False
            self._cache(job_id, state, ttl)
            return True

    def finish_if_running(self, job_id: str, state: dict) -> bool:
        """Write the terminal *state* iff the job's record is still ``running``.

        ``False`` (and NO write) when the record is missing or already
        terminal: a worker finishing after its job was expired (the
        terminal tombstone) cannot recreate or overwrite it, and an expiry
        racing a worker that just finished cannot overwrite its answer.
        Like ``__setitem__``, an ``error`` record's ``error`` field is the
        ``<kind>_failed`` sentinel.
        """
        if isinstance(state, dict) and state.get("status") == "error":
            state = {**state, "error": f"{self._kind}_failed"}
        condition = {
            "ConditionExpression": "attribute_exists(#k) AND #st = :running",
            "ExpressionAttributeNames": {"#k": _KEY_ATTR, "#st": _SLOT_STATUS_ATTR},
            "ExpressionAttributeValues": {":running": "running"},
        }
        with _SLOT_LOCK:
            if self._get_table() is None:
                if _slot_attrs(self._mem.get(job_id)).get(_SLOT_STATUS_ATTR) != "running":
                    return False
                self._mem[job_id] = state
                return True
            ttl = self._new_ttl()
            if not self._remote_put(job_id, state, condition, ttl=ttl):
                return False
            if self._get_table() is None:  # backend just degraded mid-call
                if _slot_attrs(self._mem.get(job_id)).get(_SLOT_STATUS_ATTR) != "running":
                    return False
            self._cache(job_id, state, ttl)
            return True

    # -- optimistic read-modify-write (a record shared by every replica) ----- #

    def mutate(self, job_id: str, fn, *, attempts: int = 8) -> bool:
        """Replace *job_id*'s record with ``fn(current)`` atomically across
        replicas; ``fn`` returning ``None`` means "no change" (``False``).

        Remote: the item carries a revision attribute; the write is
        conditional on the revision read (``attribute_not_exists`` for a new
        item), and a conflict re-reads and calls ``fn`` again, up to
        *attempts* times (``False`` when it never lands). ``fn`` must be pure:
        it may run more than once. Memory (no table): ``fn`` under the
        process lock. A backend error degrades to memory, like every other
        JobStore call. Every writer of a key used this way must use
        ``mutate``: a plain write drops the revision. It reads the item as its
        conditional write will find it, ``ttl`` or not: treating an expired
        item that DynamoDB has not deleted yet as absent would make every
        write fail its revision condition until the deletion. ``fn`` judges
        a stale record by its own timestamps (the ask slot's ``started_at``
        and lease times do).
        """
        with _SLOT_LOCK:
            for _ in range(max(1, attempts)):
                if self._get_table() is None:
                    new = fn(self._mem.get(job_id))
                    if new is None:
                        return False
                    self._mem[job_id] = new
                    return True
                read = self._remote_get_raw(job_id)
                if read is None:          # backend just degraded: memory next round
                    continue
                current, rev = read
                new = fn(current)
                if new is None:
                    return False
                ttl = self._new_ttl()
                if self._remote_put(job_id, new, _revision_condition(current, rev),
                                    extra={_REV_ATTR: (rev or 0) + 1}, ttl=ttl):
                    self._cache(job_id, new, ttl)
                    return True
            return False

    def _remote_get_raw(self, job_id: str):
        """``(state | None, revision | None)`` from the backend, or ``None``
        when the read errored (the backend is then disabled)."""
        table = self._get_table()
        try:
            item = table.get_item(Key={_KEY_ATTR: self._key(job_id)}).get("Item") or {}
        except Exception as exc:  # nosec B110 - fail-soft
            self._disable_backend(exc)
            return None
        state = None
        if _STATE_ATTR in item:
            try:
                state = json.loads(item[_STATE_ATTR])
            except (ValueError, TypeError):
                state = None
        rev = item.get(_REV_ATTR)
        return state, (int(rev) if rev is not None else None)

    # -- strict compare-and-set (a record other processes coordinate on) ---- #

    def get_consistent(self, job_id: str) -> "dict | None":
        """*job_id*'s record as :meth:`swap_strict` finds it: a consistent read
        of the shared table (an item whose ``ttl`` has passed included, as a
        conditional write finds it), or, on a store that is memory-only by
        configuration (one process), the memory copy. Never falls back to
        memory: raises :class:`JobStoreUnavailableError` when the table cannot
        be read."""
        if self._strict_table() is None:
            with _SLOT_LOCK:
                return copy.deepcopy(self._mem.get(job_id))
        return self._read_strict_raw(job_id)[0]

    def swap_strict(self, job_id: str, fn, *, attempts: int = 8) -> "dict | None":
        """Replace *job_id*'s record with ``fn(current)`` atomically across
        processes and return the record it holds afterwards: ``fn``'s result
        once written, or the record read when ``fn`` returns ``None`` (no
        change). Unlike :meth:`mutate` it is strict: the read is consistent,
        the write is conditional on the revision read, and nothing falls back
        to memory. A read or write that fails, or a revision that keeps
        moving for *attempts* rounds, raises :class:`JobStoreUnavailableError`
        with nothing written. ``fn`` must be pure (it may run more than once)
        and sees an item whose ``ttl`` has passed as it is, so it judges a
        stale record by its own timestamps. On a store that is memory-only by
        configuration ``fn`` runs under the process lock."""
        table = self._strict_table()
        if table is None:
            with _SLOT_LOCK:
                current = copy.deepcopy(self._mem.get(job_id))
                new = fn(current)
                if new is None:
                    return current
                self._mem[job_id] = copy.deepcopy(new)
                return new
        for _ in range(max(1, attempts)):
            current, rev = self._read_strict_raw(job_id)
            new = fn(copy.deepcopy(current))
            if new is None:
                return current
            item = self._item(job_id, new, {_REV_ATTR: (rev or 0) + 1})
            try:
                table.put_item(Item=item, **_revision_condition(current, rev))
            except Exception as exc:
                if _is_conditional_failure(exc):
                    continue  # another process wrote in between: read again
                raise JobStoreUnavailableError(
                    f"JobStore({self._kind}): compare-and-set write failed "
                    f"({type(exc).__name__})") from exc
            self._backend_ok = True
            return new
        raise JobStoreUnavailableError(
            f"JobStore({self._kind}): {job_id!r} kept changing for {attempts} attempts")

    def _read_strict_raw(self, job_id: str):
        """``(state | None, revision | None)`` from a consistent read of the
        shared table, the item's ``ttl`` not applied. Raises
        :class:`JobStoreUnavailableError` when it fails or the state cannot
        be decoded."""
        table = self._strict_table()
        try:
            item = (table.get_item(Key={_KEY_ATTR: self._key(job_id)},
                                   ConsistentRead=True) or {}).get("Item") or {}
        except Exception as exc:
            raise JobStoreUnavailableError(
                f"JobStore({self._kind}): read failed ({type(exc).__name__})") from exc
        if _STATE_ATTR not in item:
            return None, None
        try:
            state = json.loads(item[_STATE_ATTR])
        except (ValueError, TypeError) as exc:
            raise JobStoreUnavailableError(
                f"JobStore({self._kind}): unreadable state for {job_id!r}") from exc
        rev = item.get(_REV_ATTR)
        return state, (int(rev) if rev is not None else None)


def _revision_condition(current: Any, rev: "int | None") -> dict:
    """The ``mutate`` write condition: no item yet, the item without a
    revision, or exactly the revision read."""
    names = {"#k": _KEY_ATTR, "#rev": _REV_ATTR}
    if rev is not None:
        return {"ConditionExpression": "#rev = :rev",
                "ExpressionAttributeNames": {"#rev": _REV_ATTR},
                "ExpressionAttributeValues": {":rev": rev}}
    if current is None:
        return {"ConditionExpression": "attribute_not_exists(#k)",
                "ExpressionAttributeNames": {"#k": _KEY_ATTR}}
    return {"ConditionExpression": "attribute_exists(#k) AND attribute_not_exists(#rev)",
            "ExpressionAttributeNames": names}


# Slot mirror attributes (top-level, so DynamoDB conditions can read them).
_SLOT_STATUS_ATTR = "slot_status"
_SLOT_OWNER_ATTR = "slot_owner"
_SLOT_STARTED_ATTR = "slot_started_at"
_SLOT_LOCK = threading.Lock()
#: ``JobStore.mutate``'s per-item revision (a top-level number attribute).
_REV_ATTR = "job_rev"


def _slot_owner(state: Any) -> "str | None":
    if not isinstance(state, dict):
        return None
    owner = state.get("run_id") or state.get("job_id")
    return owner if isinstance(owner, str) and owner else None


def _slot_attrs(state: Any) -> dict:
    """The slot mirror for *state* (empty for records that are not slots)."""
    if not isinstance(state, dict):
        return {}
    attrs: dict = {}
    status = state.get("status")
    if isinstance(status, str):
        attrs[_SLOT_STATUS_ATTR] = status
    owner = _slot_owner(state)
    if owner is not None:
        attrs[_SLOT_OWNER_ATTR] = owner
    try:
        started = state.get("started_at")
        if started is not None:
            attrs[_SLOT_STARTED_ATTR] = int(float(started))
    except (TypeError, ValueError):
        pass
    return attrs


def _own_claim_condition(condition: dict, owner: str) -> dict:
    """The strict claim's *condition*, also met by a slot *owner* already
    holds: a retried claim whose first write landed then finds its own claim
    there. Only the strict path uses it."""
    return {
        "ConditionExpression": condition["ConditionExpression"] + " OR #o = :owner",
        "ExpressionAttributeNames": {**condition["ExpressionAttributeNames"],
                                     "#o": _SLOT_OWNER_ATTR},
        "ExpressionAttributeValues": {**condition["ExpressionAttributeValues"],
                                      ":owner": owner},
    }


def _memory_slot_free(current: Any, stale_before: int) -> bool:
    attrs = _slot_attrs(current)
    if attrs.get(_SLOT_STATUS_ATTR) != "running":
        return True
    started = attrs.get(_SLOT_STARTED_ATTR)
    return started is None or started < stale_before


def _memory_slot_owned_by(current: Any, owner: str) -> bool:
    current_owner = _slot_owner(current)
    return current_owner is None or current_owner == owner


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    code = ""
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
    return (code == "ConditionalCheckFailedException"
            or type(exc).__name__ == "ConditionalCheckFailedException")


# =========================================================================== #
# Atomic budget claim — a NARROW conditional-write primitive (NOT JobStore)
#
# WHY THIS IS NOT A JobStore METHOD: everything above is a deliberately
# FAIL-SOFT, dict-like blob store — the whole state of a job is one
# ``job_state`` JSON attribute, so DynamoDB can express no attribute-level
# condition over it, and every backend error degrades to memory so onboarding
# is never broken. Both properties are exactly WRONG for a spend budget:
# ``eval_runs.py``'s own docstring admits its JobStore lock is "a NON-ATOMIC
# check-then-set", and a fail-soft budget silently authorises spend.
#
# So a budget lives in its OWN dedicated item with a real numeric attribute
# (``claimed``) and is moved ONLY by a conditional ``UpdateItem``:
#
#     ADD claimed :n   CONDITION attribute_not_exists(claimed)
#                                OR claimed <= :cap - :n
#
# Two racing claimants therefore cannot jointly exceed ``cap``: whichever write
# lands second is evaluated against the FIRST one's committed total, and is
# rejected if it would overshoot. There is no read-then-write anywhere on this
# path (the refused-with-no-room case learns the current total from the failed
# condition's own ``ReturnValuesOnConditionCheckFailure`` payload, not a
# ``GetItem``).
#
# The primitive is generic: it knows about a ``kind`` (a budget namespace), a
# ``day_key`` (any bucket string — a UTC day for a daily cap), ``n`` slots and a
# ``cap``. It contains NO caller-specific policy; picking the cap, the bucket
# and what to do with a refusal belongs to the caller.
#
# HONESTY ABOUT THE LOCAL SEAM (the feedback-durability lesson): with no table
# configured — the offline unit lane and local dev — the claim falls back to an
# in-process counter. That is a correct guarantee for ONE process and NO
# guarantee at all for a deployed fleet. A silent fail-soft no-op that reports
# success is precisely how the dropped-feedback bug hid for months, so the
# fallback (a) is announced at WARNING under the stable key
# ``budget_claim_memory_only`` (the ``storage_unconfigured`` precedent in
# ``agents/storage.py``), and (b) is visible in the RETURN VALUE:
# :class:`BudgetClaim` carries ``durable``. Callers that must not spend money on
# a non-durable guarantee can read that flag; callers that only need the count
# can use the plain ``claim_budget_slots``.
#
# CLOUD MODE FAILS CLOSED: when a table IS configured but the conditional claim
# errors, :class:`BudgetUnavailable` is raised. Refused ("no slots left",
# granted 0) and unavailable ("the budget could not be consulted", exception)
# are therefore distinguishable, and neither can be mistaken for success.
# =========================================================================== #

_BUDGET_KEY_PREFIX = "__budget__"    # never collides with the "__job__:" keys
_CLAIMED_ATTR = "claimed"
# 48 h: a UTC-day bucket must outlive its own day in every timezone offset, and
# nothing reads a budget bucket after the following day.
_BUDGET_TTL_SECONDS = 48 * 3600
# Bounded conditional retries: attempt 1 asks for the full n; a refusal with
# room left retries ONCE for exactly the room; a further refusal means a racing
# claimant took it, and we refuse rather than loop (under-granting is safe for
# a spend cap, over-granting is not).
_MAX_CLAIM_ATTEMPTS = 3

# In-process fallback counters: {(kind, day_key): claimed}. Guarded by a lock so
# the fallback is at least atomic within this process (single replica).
_MEM_CLAIMS: dict[tuple[str, str], int] = {}
_MEM_LOCK = threading.Lock()
_MEMORY_ONLY_WARNED: set[str] = set()


class BudgetUnavailable(RuntimeError):
    """A configured budget backend could not be consulted — FAIL CLOSED.

    Distinct from a refusal (``granted == 0``): a refusal means the budget was
    read atomically and is exhausted; this means the guarantee is UNKNOWN, so
    the caller must not spend.
    """


@dataclass(frozen=True)
class BudgetClaim:
    """Outcome of a claim attempt.

    Attributes:
        granted: slots actually reserved (0 = refused; may be < requested when
            only part of the budget remained).
        durable: True when the claim was committed by a conditional write to the
            shared table (a fleet-wide guarantee); False when it was satisfied
            by the in-process fallback (this process only — NOT durable, NOT
            cross-replica). Never inferred; set by the path that ran.
        requested: slots asked for, so a partial grant is self-describing.
        kind / day_key: the budget bucket that was moved.
    """

    granted: int
    durable: bool
    requested: int
    kind: str
    day_key: str

    @property
    def refused(self) -> bool:
        return self.granted == 0

    @property
    def partial(self) -> bool:
        return 0 < self.granted < self.requested


def utc_day_key(now: datetime | None = None) -> str:
    """``YYYY-MM-DD`` in **UTC** — the canonical daily budget bucket.

    UTC (never local time) so every replica and both trigger paths agree on when
    the day rolls over.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def reset_budget_claims() -> None:
    """Clear in-process counters and the warning dedupe (test seam only)."""
    with _MEM_LOCK:
        _MEM_CLAIMS.clear()
    _MEMORY_ONLY_WARNED.clear()


def _budget_key(kind: str, day_key: str) -> str:
    return f"{_BUDGET_KEY_PREFIX}:{kind}:{day_key}"


def _resolve_budget_table(kind: str) -> Any | None:
    """Return the shared table for budget claims, or ``None`` for memory mode.

    Mirrors :meth:`JobStore._get_table`'s env signal
    (``ONBOARDING_SESSIONS_TABLE``) so "cloud mode" means the same thing for
    both. Unlike JobStore, a resolution FAILURE while the signal is present is
    NOT fail-soft: it raises so the caller fails closed.
    """
    if not os.environ.get("ONBOARDING_SESSIONS_TABLE", "").strip():
        return None
    try:
        import boto3
        region = os.environ.get("AWS_REGION", "us-east-1")
        return boto3.resource("dynamodb", region_name=region).Table(_SESSIONS_TABLE_NAME)
    except Exception as exc:
        raise BudgetUnavailable(
            f"budget_claim_unavailable: cannot resolve the budget table for "
            f"{kind} (category={type(exc).__name__})"
        ) from exc


def _conditional_failure_claimed(exc: Exception) -> int | None:
    """For a ConditionalCheckFailed error, the ``claimed`` total that refused it.

    Returns ``None`` when *exc* is not a conditional-check failure at all (the
    caller must then treat it as unavailable and fail closed), or ``-1`` when it
    is a conditional failure whose old item is not available (refuse without
    knowing the remaining room — conservative).
    """
    response = getattr(exc, "response", None)
    code = ""
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
    if code != "ConditionalCheckFailedException" and \
            type(exc).__name__ != "ConditionalCheckFailedException":
        return None
    item = (response or {}).get("Item") if isinstance(response, dict) else None
    if not isinstance(item, dict) or _CLAIMED_ATTR not in item:
        return -1
    try:
        return int(item[_CLAIMED_ATTR])
    except (TypeError, ValueError):
        return -1


def _next_desired_after_refusal(exc: Exception, kind: str, day_key: str,
                                cap: int, desired: int) -> int:
    """Slots worth a retry after a refused conditional write (0 = give up).

    Raises:
        BudgetUnavailable: *exc* was NOT a condition failure (throttling, access
            denied, a missing table…) so the budget is unknown — fail closed.
    """
    claimed = _conditional_failure_claimed(exc)
    if claimed is None:
        logger.warning(
            "budget_claim_unavailable: %s/%s conditional claim failed "
            "category=%s; caller must fail closed",
            kind, day_key, type(exc).__name__,
        )
        raise BudgetUnavailable(
            f"budget_claim_unavailable: {kind}/{day_key} "
            f"(category={type(exc).__name__})"
        ) from exc
    if claimed < 0:
        return 0  # refused, remaining room unknown -> under-grant
    room = cap - claimed
    if room <= 0:
        return 0  # exhausted
    retry = min(desired, room)
    # Refused yet the reported room would have allowed it: a racing claimant
    # moved the total between the write and its own report. Refuse, don't spin.
    return retry if retry < desired else 0


def _claim_remote(table: Any, kind: str, day_key: str, n: int, cap: int) -> int:
    """Reserve up to *n* slots with conditional ``UpdateItem``s. Returns granted.

    ONE UpdateItem on the happy path. No GetItem, ever: the only extra call is a
    second CONDITIONAL write for the exact remaining room, derived from the
    first refusal's own returned old item.
    """
    desired = min(n, cap)
    for _ in range(_MAX_CLAIM_ATTEMPTS):
        if desired <= 0:
            return 0
        try:
            table.update_item(
                Key={_KEY_ATTR: _budget_key(kind, day_key)},
                UpdateExpression=(
                    f"ADD {_CLAIMED_ATTR} :n SET #ttl = :ttl, #day = :day"
                ),
                ConditionExpression=(
                    f"attribute_not_exists({_CLAIMED_ATTR}) "
                    f"OR {_CLAIMED_ATTR} <= :room"
                ),
                ExpressionAttributeNames={"#ttl": _TTL_ATTR, "#day": "day_key"},
                ExpressionAttributeValues={
                    ":n": desired,
                    ":room": cap - desired,
                    ":ttl": int(time.time()) + _BUDGET_TTL_SECONDS,
                    ":day": day_key,
                },
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
            return desired
        except Exception as exc:
            desired = _next_desired_after_refusal(exc, kind, day_key, cap, desired)
    return 0


def _claim_memory(kind: str, day_key: str, n: int, cap: int) -> int:
    key = (kind, day_key)
    with _MEM_LOCK:
        claimed = _MEM_CLAIMS.get(key, 0)
        granted = max(0, min(n, cap - claimed))
        if granted:
            _MEM_CLAIMS[key] = claimed + granted
    return granted


def _warn_memory_only(kind: str) -> None:
    """Announce the non-durable fallback. Stable key: ``budget_claim_memory_only``."""
    if kind in _MEMORY_ONLY_WARNED:
        logger.debug(
            "budget_claim_memory_only: %s claimed against the in-process "
            "counter (not durable)", kind,
        )
        return
    _MEMORY_ONLY_WARNED.add(kind)
    logger.warning(
        "budget_claim_memory_only: the %s budget is being claimed against an "
        "IN-PROCESS counter because no ONBOARDING_SESSIONS_TABLE is configured "
        "— the cap holds for THIS process only and is NOT a durable, "
        "cross-replica guarantee. Expected in local development and the "
        "offline test lane; in a deployed environment it means the task "
        "definition is missing the table env var.",
        kind,
    )


def claim_budget_slots_detailed(kind: str, day_key: str, n: int, cap: int,
                                *, table: Any | None = _UNSET) -> BudgetClaim:
    """Atomically reserve up to *n* of *cap* slots in the ``kind``/``day_key`` bucket.

    Args:
        kind: budget namespace (e.g. ``"audit-budget"``).
        day_key: bucket id — use :func:`utc_day_key` for a daily cap.
        n: slots requested (>= 1). Fewer may be granted (partial grant).
        cap: total slots the bucket may ever grant.
        table: explicit DynamoDB Table (tests / injection). ``None`` forces the
            in-process fallback; omitted resolves from the environment.

    Returns:
        A :class:`BudgetClaim`. ``granted == 0`` means REFUSED (the budget was
        consulted and has no room). ``durable`` says whether the guarantee is
        fleet-wide or process-local.

    Raises:
        BudgetUnavailable: cloud mode only — a configured backend could not be
            consulted. Callers MUST fail closed; there is no success path here.
        ValueError: invalid arguments (a money primitive does not guess).
    """
    if not kind or not day_key:
        raise ValueError("claim_budget_slots requires a kind and a day_key")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"n must be a positive int, got {n!r}")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise ValueError(f"cap must be a non-negative int, got {cap!r}")

    resolved = _resolve_budget_table(kind) if table is _UNSET else table
    if resolved is None:
        _warn_memory_only(kind)
        return BudgetClaim(granted=_claim_memory(kind, day_key, n, cap),
                           durable=False, requested=n, kind=kind, day_key=day_key)
    return BudgetClaim(granted=_claim_remote(resolved, kind, day_key, n, cap),
                       durable=True, requested=n, kind=kind, day_key=day_key)


def claim_budget_slots(kind: str, day_key: str, n: int, cap: int,
                       *, table: Any | None = _UNSET) -> int:
    """Slots granted (0 = refused) — the count-only form of the claim.

    Deliberately hides ``durable``: use :func:`claim_budget_slots_detailed` when
    the caller must know whether the cap it just claimed against is a durable
    fleet-wide guarantee or the in-process fallback. Raises
    :class:`BudgetUnavailable` in cloud mode exactly as the detailed form does.
    """
    return claim_budget_slots_detailed(kind, day_key, n, cap, table=table).granted
