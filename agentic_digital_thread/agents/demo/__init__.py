# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo addon seam (ADR 0019 — demo-as-addon).

Demo content is an optional addon, gated behind the ``ADT_DEMO`` flag and isolated
here in ``agents.demo``. The core runs, imports, and passes tests with the demo
disabled; the demo deployment enables it. This package depends only on the stdlib
and its sibling ``prompt_pack`` — it MUST NOT import ``agents.prompts`` or
``agents.main`` (one-way dependency: callers route through the seam, never the
reverse).

Runtime toggle (S4.4b): the demo can be flipped at runtime via an admin console.
A persisted runtime *override* wins over the ``ADT_DEMO`` env default; precedence is
``override (if set) → ADT_DEMO env → off``. To keep the seam pure, persistence is
supplied by an injected, duck-typed persister (``get_flag() -> Optional[bool]`` /
``set_flag(value: bool) -> None``) — the seam never imports ``storage`` or
``boto3``.
"""

import os
from typing import Optional

from .prompt_pack import DEMO_PROMPTS, DEMO_QUESTIONS

_ENABLED_VALUES = {"1", "true", "yes", "on"}

# Runtime override cache. ``None`` means "no override set" → fall back to env.
_override: Optional[bool] = None

# Injected, duck-typed persister (``get_flag``/``set_flag``). ``None`` = offline.
_persister = None


def configure(persister) -> None:
    """Inject a duck-typed persister for the runtime override.

    The persister must expose ``get_flag() -> Optional[bool]`` and
    ``set_flag(value: bool) -> None``. Persistence (SSM/boto3) lives in the
    injected object, never in this seam — preserving the one-way dependency.
    """
    global _persister
    _persister = persister


def load_persisted() -> None:
    """Load the persisted override (if any) into the in-memory cache.

    Called once at boot, before seeding. A missing/``None`` persisted flag is
    normal and means "no override" — the env default applies.
    """
    global _override
    if _persister is None:
        return
    value = _persister.get_flag()
    if value is not None:
        _override = value


def set_override(value: Optional[bool]) -> None:
    """Set (or clear) the runtime override.

    ``True``/``False`` force the demo on/off regardless of ``ADT_DEMO``; ``None``
    clears the override and reverts to the env default. When a persister is
    configured and the value is not ``None``, the flag is written through.
    Exceptions from the persister propagate so callers (e.g. the admin route)
    can surface a failed persist.
    """
    global _override
    _override = value
    if _persister is not None and value is not None:
        _persister.set_flag(bool(value))


def is_enabled() -> bool:
    """Return True iff the demo addon is enabled.

    Precedence: a runtime override (if set) wins; otherwise the ``ADT_DEMO`` env
    flag is consulted. Truthy values (case-insensitive): ``1``, ``true``,
    ``yes``, ``on``. Default is OFF.
    """
    if _override is not None:
        return _override
    return os.environ.get("ADT_DEMO", "").lower() in _ENABLED_VALUES


def demo_prompts() -> list[dict]:
    """Return a shallow copy of the curated demo prompt pack."""
    return list(DEMO_PROMPTS)


def demo_questions() -> list[str]:
    """Return a copy of the curated CLI demo questions."""
    return list(DEMO_QUESTIONS)
