# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Runtime pause/resume toggle for live dashboard auto-updates (cost control).

The ``CARD_LIVE`` startup env flag WIRES the live-card feature at boot (the poll
job registration, the MQTT stream driver, the SSE ``card_update`` ripple). This
seam adds a *runtime* on/off that is checked on EVERY background tick so the
recurring backend queries (Athena scans / SiteWise reads) driven by the poll and
the stream driver can be PAUSED to save cost WITHOUT a redeploy — and resumed the
same way.

It deliberately mirrors the ``agents.demo`` runtime-override seam (S4.4b): a
persisted runtime *override* wins over the default; precedence is
``override (if set) → LIVE_UPDATES_ENABLED env → default ON``. The **default is
ENABLED** (unlike the demo seam's default-off) because ``CARD_LIVE=1`` is the
deployed state — the toggle only turns the already-wired live updates OFF to
save cost, and a fresh/unconfigured environment keeps today's behavior.

To keep the seam pure, persistence is supplied by an injected, duck-typed
persister (``get_flag() -> Optional[bool]`` / ``set_flag(value: bool) -> None``)
— the same ``SsmSettingsStore`` the demo toggle uses (a different SSM parameter).
The seam never imports ``storage`` or ``boto3``. Reads are fail-soft: any
unreadable persisted flag leaves the default ON, so a settings glitch can never
silently stop live updates at boot.

The seam also carries a runtime **snapshot-interval override** (seconds between
periodic SSE dashboard snapshots) with the same precedence shape: override (if
set) → ``DASHBOARD_SNAPSHOT_INTERVAL`` env → 120s default. Its persister
(duck-typed ``get_interval() -> Optional[int]`` / ``set_interval(value: int)``,
a second SSM parameter on the same ``SsmSettingsStore``) is resolved LAZILY on
first use via the ``feedback_exploration_settings._default_persister``
precedent — honoring the ``ADT_SETTINGS_STORE=none`` local opt-out and needing
no ``server.py`` wiring; tests inject a fake through :func:`configure_interval`.
Values outside 30..3600 seconds or non-numeric values are ignored fail-soft
(``None`` — env/default applies).
"""

import os
from typing import Optional

_ENABLED_VALUES = {"1", "true", "yes", "on"}
_DISABLED_VALUES = {"0", "false", "no", "off"}

# Snapshot-interval bounds (seconds). Below 30s the recurring backend queries
# get expensive fast; above 1h the dashboard is effectively on-demand anyway.
INTERVAL_MIN_SECONDS = 30
INTERVAL_MAX_SECONDS = 3600

# The env/default the interval override sits in front of (owned here so the
# precedence chain has ONE home; ``dashboard._snapshot_interval_seconds``
# delegates to :func:`snapshot_interval_seconds`).
_INTERVAL_DEFAULT_SECONDS = 120.0

# Runtime override cache. ``None`` means "no override set" → fall back to env/default.
_override: Optional[bool] = None

# Injected, duck-typed persister (``get_flag``/``set_flag``). ``None`` = offline.
_persister = None

# Snapshot-interval runtime override (seconds). ``None`` = no override → the
# ``DASHBOARD_SNAPSHOT_INTERVAL`` env / 120s default applies.
_interval_override: Optional[int] = None

# Interval persister slot. ``_UNSET`` = resolve lazily on first use
# (production); ``None`` = explicitly no persistence (tests / opt-out).
_UNSET = object()
_interval_persister = _UNSET

# The persisted interval is loaded lazily ONCE (fail-soft) on first read.
_interval_loaded = False


def configure(persister) -> None:
    """Inject a duck-typed persister for the runtime override.

    The persister must expose ``get_flag() -> Optional[bool]`` and
    ``set_flag(value: bool) -> None``. Persistence (SSM/boto3) lives in the
    injected object, never in this seam — preserving a pure, importable module.
    """
    global _persister
    _persister = persister


def load_persisted() -> None:
    """Load the persisted flag override (if any) into the in-memory cache.

    Called once at boot. A missing/``None`` persisted flag is normal and means
    "no override" — the env/default applies (live updates ON). (The interval
    override loads itself lazily on first read — see
    :func:`_ensure_interval_loaded`.)
    """
    global _override
    if _persister is None:
        return
    value = _persister.get_flag()
    if value is not None:
        _override = value


def coerce_interval(value) -> Optional[int]:
    """Validate a candidate snapshot interval: an int within
    [``INTERVAL_MIN_SECONDS``, ``INTERVAL_MAX_SECONDS``], else ``None``
    (out-of-bounds or non-numeric values are IGNORED — fail-soft, mirroring
    the flag seam's unrecognized-env handling). Booleans are rejected (they
    are ints in Python but never a valid interval).
    """
    if isinstance(value, bool):
        return None
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if INTERVAL_MIN_SECONDS <= seconds <= INTERVAL_MAX_SECONDS:
        return seconds
    return None


def configure_interval(persister) -> None:
    """Inject a duck-typed interval persister (``get_interval() ->
    Optional[int]`` / ``set_interval(value: int)``) — test seam / explicit
    wiring. ``None`` = explicitly no persistence. Re-arms the lazy load."""
    global _interval_persister, _interval_override, _interval_loaded
    _interval_persister = persister
    _interval_override = None
    _interval_loaded = False


def _default_interval_persister():
    """The production interval persister, or ``None`` when persistence is
    opted out — resolved LAZILY on first use (the
    ``feedback_exploration_settings._default_persister`` precedent, so
    ``server.py`` needs no wiring). Honors the ``ADT_SETTINGS_STORE=none``
    local opt-out; ``storage``/boto3 are imported lazily so this module stays
    stdlib-only at import."""
    if os.environ.get("ADT_SETTINGS_STORE", "").strip().lower() == "none":
        return None
    try:
        from .storage import SsmSettingsStore
        return SsmSettingsStore(
            "/adt/live-updates-enabled",
            interval_param_name="/adt/live-updates-interval")
    except Exception:  # nosec B110 - persistence is optional, never fatal
        return None


def _resolve_interval_persister():
    """Resolve (and memoize) the interval persister on first use."""
    global _interval_persister
    if _interval_persister is _UNSET:
        _interval_persister = _default_interval_persister()
    return _interval_persister


def _ensure_interval_loaded() -> None:
    """Lazily load the persisted interval override once (fail-soft: a missing,
    unreadable, or out-of-bounds persisted value leaves the override unset so
    the env/default cadence applies)."""
    global _interval_loaded, _interval_override
    if _interval_loaded:
        return
    _interval_loaded = True
    persister = _resolve_interval_persister()
    if persister is None:
        return
    try:
        value = persister.get_interval()
    except Exception:  # nosec B110 - boot-resilient read; env/default applies
        value = None
    coerced = coerce_interval(value)
    if coerced is not None:
        _interval_override = coerced


def set_snapshot_interval(value: Optional[int]) -> None:
    """Set (or clear) the runtime snapshot-interval override (seconds).

    A valid value (30..3600) is written through the persister FIRST when one
    is configured (persister exceptions propagate so the admin route can
    surface a failed persist — no phantom overrides), then becomes the
    override. ``None`` clears the in-memory override (reverts to the
    env/default; not written through, mirroring the flag). An out-of-bounds or
    non-numeric value is IGNORED — the current override stands.
    """
    global _interval_override, _interval_loaded
    _ensure_interval_loaded()
    if value is None:
        _interval_override = None
        return
    coerced = coerce_interval(value)
    if coerced is None:
        return
    persister = _resolve_interval_persister()
    if persister is not None:
        persister.set_interval(coerced)
    _interval_override = coerced


def snapshot_interval_override() -> Optional[int]:
    """The runtime interval override in seconds, or ``None`` when unset."""
    _ensure_interval_loaded()
    return _interval_override


def snapshot_interval_seconds() -> float:
    """Effective seconds between periodic SSE dashboard snapshots.

    Precedence mirrors the flag seam: the runtime override (if set) wins;
    otherwise the ``DASHBOARD_SNAPSHOT_INTERVAL`` env value; otherwise the
    120s default. Fail-soft: a non-numeric env value keeps the default.
    """
    _ensure_interval_loaded()
    if _interval_override is not None:
        return float(_interval_override)
    try:
        return float(os.environ.get(
            "DASHBOARD_SNAPSHOT_INTERVAL", str(_INTERVAL_DEFAULT_SECONDS)))
    except ValueError:
        return _INTERVAL_DEFAULT_SECONDS


def set_live_updates(value: Optional[bool]) -> None:
    """Set (or clear) the runtime live-updates override.

    ``True``/``False`` force live updates on/off regardless of the env default;
    ``None`` clears the override and reverts to the env/default. When a persister
    is configured and the value is not ``None``, the flag is written through.
    Exceptions from the persister propagate so callers (e.g. the admin route) can
    surface a failed persist.
    """
    global _override
    _override = value
    if _persister is not None and value is not None:
        _persister.set_flag(bool(value))


def live_updates_enabled() -> bool:
    """Return True iff live dashboard auto-updates are currently enabled.

    Precedence: a runtime override (if set) wins; otherwise the
    ``LIVE_UPDATES_ENABLED`` env flag is consulted; otherwise the default is ON.
    Truthy/falsy values (case-insensitive): ``1/true/yes/on`` and
    ``0/false/no/off``. Fail-soft: any unrecognized env value keeps the default
    ON, so a misconfiguration never silently stops live updates.
    """
    if _override is not None:
        return _override
    raw = os.environ.get("LIVE_UPDATES_ENABLED")
    if raw is not None:
        val = raw.strip().lower()
        if val in _DISABLED_VALUES:
            return False
        if val in _ENABLED_VALUES:
            return True
    return True  # default ON — CARD_LIVE=1 is the deployed state
