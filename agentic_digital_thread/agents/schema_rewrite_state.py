# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system's schema-rewrite state in the shared store, where no graph query
reaches it.

While a re-provision rewrites a system's schema subgraph (``onboard`` b0 to
b3b), the system carries a MARK: the epoch milliseconds the rewrite started,
which is also the run's token. A rewrite that ends before its field concepts
are settled leaves the FAILED marker instead (epoch ms of the failure). Both
were System-node properties only. ``explore_graph`` refused a query naming
them, but a query looping over ``keys(s)`` and reading ``s[k]`` served them
to the model, so they move into one record per system in a shared
``JobStore`` (the onboarding-sessions table the provision and reset leases
use; memory only where no table is configured, one process). The move takes
two releases, so tasks of two versions running side by side during a deploy
see each other's rewrites: in the first, every writer keeps the System-node
properties too and every reader honours both (``onboard``,
``_SCHEMA_REWRITE_STATES``); the second stops the System-node writes and
reads.

The record is ``{"started": token | None, "failed": ms | None, "begun": the
last start's token}``. Each change is one compare-and-set
(``JobStore.swap_strict``) with the conditions the graph writes have:

* :func:`begin` sets the mark (and ``begun``) whatever was there;
* :func:`end` replaces the run's own mark, or none, never another run's newer
  one: settled, mark and failed marker removed; not settled, the failed
  marker set;
* :func:`clear` removes a stale mark or failed marker only while it still
  holds the exact value its reader saw;
* :func:`forget` empties the record a system's removal finds, unless it has
  changed since.

A reader that must see a field "as a whole provision left it" reads the
record before and after its graph read (:func:`unchanged_across`): ``begun``
changes whenever a rewrite starts, so a rewrite that began, or began and
ended, during the graph read is seen. Every function raises
``job_store.JobStoreUnavailableError`` when the record cannot be read or
written; nothing here reads as "no mark" on a failure. Stdlib only.
"""
from __future__ import annotations

STARTED = "started"
FAILED = "failed"
BEGUN = "begun"
_EMPTY = {STARTED: None, FAILED: None, BEGUN: None}


def _state(record) -> dict:
    """The record with every key present (``None`` when absent)."""
    out = dict(_EMPTY)
    if isinstance(record, dict):
        out.update({k: record.get(k) for k in _EMPTY})
    return out


def read(store, system_id: str) -> dict:
    """``system_id``'s state, read consistently."""
    return _state(store.get_consistent(system_id))


def begin(store, system_id: str, token: int) -> dict:
    """Mark ``system_id`` as rewriting under ``token``; the state written."""
    return _state(store.swap_strict(
        system_id, lambda cur: {**_state(cur), STARTED: token, BEGUN: token}))


def end(store, system_id: str, token, *, failed=None) -> dict:
    """End the rewrite ``token`` started: when the mark is ``token`` or none,
    remove it, and set the failed marker to ``failed`` (``None``: settled,
    any earlier failed marker removed). Another run's mark is left as it is.
    The state the record holds afterwards."""
    def swap(cur):
        state = _state(cur)
        if state[STARTED] is not None and state[STARTED] != token:
            return None
        if state[STARTED] is None and state[FAILED] == failed:
            return None                  # nothing to change: no write
        return {**state, STARTED: None, FAILED: failed}
    return _state(store.swap_strict(system_id, swap))


def clear(store, system_id: str, key: str, value) -> bool:
    """Remove ``key`` (:data:`STARTED` or :data:`FAILED`) while it still holds
    ``value``. True when this call removed it."""
    removed = []

    def swap(cur):
        removed.clear()                  # only the last run of swap decides
        state = _state(cur)
        if value is None or state[key] != value:
            return None
        removed.append(True)
        return {**state, key: None}
    store.swap_strict(system_id, swap)
    return bool(removed)


def unchanged_across(before: dict, after: dict) -> bool:
    """Whether no rewrite started between two reads (``begun`` is the same)."""
    return before.get(BEGUN) == after.get(BEGUN)


def forget(store, system_id: str) -> bool:
    """Empty ``system_id``'s record as it is now, for the system's removal:
    the record is read, then replaced with an empty one only while it still
    holds what was read (a begin landing in between is kept). True when this
    call emptied a record that held something."""
    observed = read(store, system_id)
    if observed == _EMPTY:
        return False
    emptied = []

    def swap(cur):
        emptied.clear()                  # only the last run of swap decides
        if _state(cur) != observed:
            return None
        emptied.append(True)
        return dict(_EMPTY)
    store.swap_strict(system_id, swap)
    return bool(emptied)
