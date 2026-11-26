# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Runtime admin toggle for the feedback-exploration path (default OFF).

Precedence (strict, pinned): **env kill > admin flag > default OFF**.

* ``FEEDBACK_EXPLORATION_ENABLED`` is an **ops kill switch**, not an enabler:
  unset or an explicit truthy value (``1/true/yes/on``, case-insensitive)
  means "no kill"; ANY other set value (``0``, ``false``, empty, a typo like
  ``banana``) is a hard kill that no admin flag can override — fail-closed,
  so a mangled env value can never silently enable the path. A truthy env
  value NEVER force-enables anything: the admin flag is the single ON
  switch.
* The admin flag is a persisted boolean over the
  ``/adt/feedback-exploration-enabled`` SSM parameter. The persister is
  duck-typed (``get_flag() -> Optional[bool]`` / ``set_flag(bool)`` — the
  ``live_settings``/``demo`` seam contract) and resolved LAZILY on first
  read via :func:`_default_persister`, which mirrors ``server.lifespan``'s
  ``ADT_SETTINGS_STORE=none`` opt-out contract (the
  ``context_interview.state._store`` precedent) — so local mode never reads
  or writes a deployed account's parameter, and ``server.py`` needs no
  wiring. Tests inject a fake through :func:`configure`.
* Nothing set anywhere ⇒ **OFF** (a new untrusted path stays opt-in).

Reads are TTL-cached (60 s read-through, the ``demo/db_routing`` idiom): the
feedback POST hot path costs at most one SSM read per minute per process.
Failed/unconfigured reads cache ``None`` (boot-resilient ⇒ default OFF).
Writes (:func:`set_enabled`) go through the persister FIRST and fail loud —
the admin PUT must surface a failed persist, and a failed write never
mutates the cached state (no phantom toggles).

Import-pure module (``catalog.py`` discipline): stdlib only at import;
``storage``/boto3 are imported lazily inside the default-persister fallback.
Never mutates ``os.environ``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

PARAM_NAME = "/adt/feedback-exploration-enabled"

_ENV_VAR = "FEEDBACK_EXPLORATION_ENABLED"
_NO_KILL_VALUES = ("1", "true", "yes", "on")
_TTL_SECONDS = 60.0

# Persister slot. ``_UNSET`` = resolve lazily via _default_persister on first
# use (production); ``None`` = explicitly no persistence (tests / opt-out).
_UNSET = object()
_persister = _UNSET

# TTL read-through cache: (value_or_None, expires_monotonic). ``None`` slot
# means "not populated". A cached ``None`` VALUE (flag absent/unreadable)
# is cached too so an unconfigured env does not re-hit SSM per request.
_cache: "Optional[Tuple[Optional[bool], float]]" = None
_lock = threading.Lock()

# Clock seam (the ``rederive`` idiom): tests advance time without touching
# the global ``time`` module.
_now = time.monotonic


def _default_persister():
    """The production persister, or ``None`` when persistence is opted out.

    Mirrors ``server.lifespan``'s ``ADT_SETTINGS_STORE=none`` contract
    (the ``context_interview.state._store`` precedent) so local mode never
    inherits a deployed account's toggle."""
    if os.environ.get("ADT_SETTINGS_STORE", "").strip().lower() == "none":
        return None
    try:
        from .storage import SsmSettingsStore
        return SsmSettingsStore(PARAM_NAME)
    except Exception as exc:  # nosec B110 - persistence is optional, never fatal
        logger.warning("feedback-exploration store unavailable: %s", exc)
        return None


def _resolve_persister():
    """Resolve (and memoize) the persister on first use."""
    global _persister
    with _lock:
        if _persister is _UNSET:
            _persister = _default_persister()
        return _persister


def configure(persister) -> None:
    """Inject a duck-typed persister (test seam / explicit wiring); drops any
    cached read."""
    global _persister, _cache
    with _lock:
        _persister = persister
        _cache = None


def _reset() -> None:
    """Test seam: explicitly-no-persister, cold-cache state (hermetic — the
    lazy production default is NOT re-armed; use ``configure`` or set the
    module ``_persister`` to ``_UNSET`` to exercise it)."""
    configure(None)


def env_kill_active() -> bool:
    """True iff the env var is SET to anything but an explicit truthy value.

    Unset ⇒ no kill (the admin flag governs). Truthy ⇒ no kill (legacy
    deploys that exported ``=1`` keep the toggle usable — they do NOT
    force-enable). Anything else ⇒ hard kill, fail-closed."""
    raw = os.environ.get(_ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() not in _NO_KILL_VALUES


def _admin_flag() -> "Optional[bool]":
    """TTL-cached read of the persisted admin flag (``None`` = not set /
    unreadable / no persister). A failed read is swallowed and cached as
    ``None`` — boot-resilient by contract (default OFF applies)."""
    global _cache
    persister = _resolve_persister()
    with _lock:
        if _cache is not None and _now() < _cache[1]:
            return _cache[0]
    # SSM read OUTSIDE the lock (mirrors the write path): holding it across a
    # network call would block every exploration_enabled()/effective()/admin-GET
    # caller for a full boto timeout when SSM is slow.
    value: "Optional[bool]" = None
    if persister is not None:
        try:
            value = persister.get_flag()
        except Exception:  # nosec B110 - boot-resilient read; default OFF applies
            value = None
    with _lock:
        # Re-check: a concurrent caller may have refreshed while we fetched.
        if _cache is not None and _now() < _cache[1]:
            return _cache[0]
        _cache = (value, _now() + _TTL_SECONDS)
        return value


def effective() -> "Tuple[bool, str]":
    """``(enabled, source)`` with source ∈ ``env-kill|admin-flag|default``."""
    if env_kill_active():
        return (False, "env-kill")
    flag = _admin_flag()
    if flag is not None:
        return (bool(flag), "admin-flag")
    return (False, "default")


def exploration_enabled() -> bool:
    """The one gate the exploration path consults."""
    return effective()[0]


def set_enabled(value: bool) -> None:
    """Persist the admin flag. FAIL-LOUD: persister exceptions propagate (the
    admin PUT surfaces them), and a missing persister raises rather than
    pretending an unpersistable toggle took (no phantom writes). The cache is
    updated only AFTER a successful write."""
    global _cache
    persister = _resolve_persister()
    if persister is None:
        raise RuntimeError("no settings persister configured "
                           "(ADT_SETTINGS_STORE=none or unavailable)")
    persister.set_flag(bool(value))
    with _lock:
        _cache = (bool(value), _now() + _TTL_SECONDS)
