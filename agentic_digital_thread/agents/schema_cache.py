# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""In-process, per-system schema-bundle cache keyed by a graph ``schema_version``.

Mirrors the proven cheap-version-check idiom already used for system guidance: the
expensive bundle build fires only when the version changes. A re-onboard bumps the
``System.schema_version`` (see ``onboard`` provision), so the very next question in the
orchestrator runtime picks up the new schema WITHOUT a container restart — the version
mismatch forces a reload and evicts the prior entry.

One entry per ``system_id`` (a newer version overwrites the old), so a re-onboard
replaces rather than accumulates. Thread-safe (the orchestrator runtime serves
concurrent requests). Import-light: :func:`cheap_schema_version` imports
``neptune_query`` lazily so this module stays offline-importable.
"""

import threading

# system_id -> (version, bundle). One entry per system keeps a re-onboard's bumped
# version REPLACING the prior bundle, never accumulating stale versions.
_cache: dict = {}
_lock = threading.Lock()


def get_or_load(system_id: str, version: str, loader):
    """Return the cached bundle for ``(system_id, version)`` or build it once.

    On a cache HIT (same version) the cached bundle is returned without calling
    ``loader``. On a MISS (no entry, or a bumped version) ``loader()`` is called
    once and its result cached, evicting any prior version for ``system_id``.
    """
    with _lock:
        cached = _cache.get(system_id)
        if cached is not None and cached[0] == version:
            return cached[1]
    # Build outside the lock: the loader may do IO (a graph read) and must not
    # block other systems' cache access. Sequential callers still load once.
    bundle = loader()
    with _lock:
        _cache[system_id] = (version, bundle)
    return bundle


def invalidate(system_id: str) -> None:
    """Drop the cached bundle for ``system_id`` (forces a reload next time)."""
    with _lock:
        _cache.pop(system_id, None)


def reset() -> None:
    """Clear the entire cache (tests + recovery; mirrors reset_service_token_cache)."""
    with _lock:
        _cache.clear()


def cheap_schema_version(system_id: str) -> str:
    """Read the current ``System.schema_version`` scalar for ``system_id``.

    A single cheap scalar graph read used as the cache key. Returns ``""`` when the
    version is unset (legacy systems onboarded before versioning) or on any error —
    an empty sentinel still caches coherently because the version only changes on a
    re-provision. ``neptune_query`` is imported lazily to keep this module
    offline-importable.
    """
    try:
        from .db import neptune_query

        rows = neptune_query(
            "MATCH (s:System {id: $sid}) RETURN s.schema_version AS v",
            {"sid": system_id},
        )
        if rows:
            v = rows[0].get("v")
            if v is not None and v != "":
                return str(v)
        return ""
    except Exception:  # nosec B110 - version read is best-effort; empty sentinel caches coherently
        return ""
