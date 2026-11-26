# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Safety policy for the opt-in ADR-0063 browser mutation checks."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

MUTATION_FLAG = "ADT_E2E_ADR0063_MUTATIONS"
PROTECTED_OVERRIDE_FLAG = "ADT_E2E_ADR0063_ALLOW_PROTECTED"
SYNTHETIC_SYSTEM_ENV = "ADT_E2E_SYNTHETIC_SYSTEM_ID"
PROTECTED_MARKERS_ENV = "ADT_PROTECTED_URL_MARKERS"

_SYSTEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAPPING_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PROMOTE_RE = re.compile(
    r"^/api/admin/vocabulary/guidance/([^/]+)/promote-requirement$"
)
_REJECT_RE = re.compile(r"^/api/proposed-mappings/([^/]+)/reject$")
_PLACEMENT_PATH = "/api/admin/learning/context-placement-check"


def _env(source: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if source is None else source


def _decode(value: str) -> str:
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def protected_markers(source: Mapping[str, str] | None = None) -> list[str]:
    raw = _env(source).get(PROTECTED_MARKERS_ENV, "")
    return [_decode(part.strip()).casefold() for part in raw.split(",") if part.strip()]


_PROTECTED_TIER_TOKENS = frozenset({
    bytes((112, 114, 111, 100)).decode("ascii"),
    bytes((112, 114, 111, 100, 117, 99, 116, 105, 111, 110)).decode("ascii"),
})


def _searchable_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return _decode(f"{parsed.hostname or ''} {parsed.path}").casefold()


def _is_named_protected_tier_url(base_url: str) -> bool:
    tokens = [part for part in re.split(r"[^a-z0-9]+", _searchable_url(base_url)) if part]
    return any(token in _PROTECTED_TIER_TOKENS for token in tokens)


def _configured_marker(base_url: str, markers: list[str]) -> str | None:
    searchable = _searchable_url(base_url)
    for marker in markers:
        pattern = rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])"
        if re.search(pattern, searchable):
            return marker
    return None


def protected_mutation_refusal(
    base_url: str, source: Mapping[str, str] | None = None
) -> str | None:
    """Return a refusal unless protected-tier mutation is separately authorized."""
    env = _env(source)
    if env.get(PROTECTED_OVERRIDE_FLAG) == "1":
        return None
    marker = _configured_marker(base_url, protected_markers(env))
    named_tier = _is_named_protected_tier_url(base_url)
    if named_tier or marker:
        reason = "protected-tier URL" if named_tier else f"protected marker {marker!r}"
        return (
            f"REFUSED: {reason}; {MUTATION_FLAG}=1 is not sufficient. "
            f"Set {PROTECTED_OVERRIDE_FLAG}=1 only with separate explicit authorization."
        )
    return None


def synthetic_system_id(source: Mapping[str, str] | None = None) -> str:
    value = _env(source).get(SYNTHETIC_SYSTEM_ENV, "").strip()
    if (
        not _SYSTEM_RE.fullmatch(value)
        or value.casefold() in {"all", "any", "none"}
    ):
        raise ValueError(
            f"{SYNTHETIC_SYSTEM_ENV} must name one explicitly designated synthetic system"
        )
    return value


def mutation_request_allowed(path: str, body: object) -> bool:
    """Allow only pending promotion, scoped placement, and explicit reject cleanup."""
    promote = _PROMOTE_RE.fullmatch(path)
    if promote:
        values = body if isinstance(body, dict) else {}
        return (
            bool(_SYSTEM_RE.fullmatch(unquote(promote.group(1))))
            and set(values) == {"asserted", "negated"}
            and all(
                isinstance(values[key], str) and values[key].startswith("synthetic-")
                for key in ("asserted", "negated")
            )
        )
    if path == _PLACEMENT_PATH:
        values = body if isinstance(body, dict) else {}
        return set(values) == {"system_id"} and bool(
            _SYSTEM_RE.fullmatch(str(values.get("system_id", "")))
        )
    reject = _REJECT_RE.fullmatch(path)
    return bool(
        reject
        and _MAPPING_RE.fullmatch(unquote(reject.group(1)))
        and body in (None, {})
    )


# --- Read-only classification for the DEFAULT (non-mutating) closure checks ---
# The closure checks assert that a read-only steward surface changes no steward
# or graph state. Classifying by method SHAPE alone was dishonest in both
# directions: it flagged two audited POST-shaped READS that the dashboard panels
# legitimately issue, and it ignored any method outside a hardcoded write set.
#
# This classifier is SEMANTIC and FAIL-CLOSED: a request is a mutation unless it
# is a read-shaped method or an exact (POST, path) pair that a human has audited
# and listed below. A newly added mutating POST therefore breaks the closure
# checks until someone audits it — that narrowness is the whole mechanism.
_READ_SHAPED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Audited POST-shaped READS. Both resolve and compose a payload; neither writes.
# They are POST only because the request carries a body. A closure test that
# intentionally displays the dashboard (the steward digest/precision surface)
# legitimately issues them.
#
# NOT listed, deliberately:
#   • /api/ask — mutation-capable in general (conversation memory, plus the
#     post-turn auto-accept hook). The briefing's write_memory:false body does
#     not make the ENDPOINT a read; a briefing appearing on a closure surface is
#     a lifecycle bug to fix, never a waiver.
#   • /api/auth/login — changes session state. The fixtures install their
#     recorder AFTER login instead of declaring login read-only.
#   • other computational POSTs (/api/metrics/resolve, /api/kg/analyze, …) —
#     no default closure surface was observed to need one, so they stay
#     forbidden until one does AND its implementation is audited.
_AUDITED_POST_READ_PATHS = frozenset({
    "/api/metrics/resolve-by-dimension",
    "/api/metrics/resolve-heatmap",
})


# The dashboard streams its own AI briefing through /api/ask on landing
# (frontend views/dashboard/briefing.ts: session_id "briefing-…",
# write_memory false). That is ambient page behavior, not a control a
# read-only check clicked — but /api/ask is NOT an audited read, so the
# checks STUB it locally (it never reaches the server) rather than exempt
# it. Only this exact shape is stubbed; every other /api/ask still records.
BRIEFING_SESSION_PREFIX = "briefing-"
BRIEFING_STUB_SSE = 'event: done\ndata: {"text": ""}\n\n'


def is_dashboard_briefing_turn(method: str, url: str, post_data: str | None) -> bool:
    """True only for the dashboard's synthetic briefing POST to /api/ask."""
    if (method or "").strip().upper() != "POST" or urlsplit(url).path != "/api/ask":
        return False
    try:
        body = json.loads(post_data or "")
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    session_id = body.get("session_id")
    return (
        isinstance(session_id, str)
        and session_id.startswith(BRIEFING_SESSION_PREFIX)
        and body.get("write_memory") is False
    )


def is_state_changing_request(method: str, url: str) -> bool:
    """True unless ``(method, path)`` is an audited read. Fail-closed.

    Matching is on the EXACT ``urlsplit`` path — query strings, fragments and
    host never affect the verdict, and no prefix/suffix inherits the allowlist.
    An unknown or malformed method counts as a mutation."""
    normalized = (method or "").strip().upper()
    if normalized in _READ_SHAPED_METHODS:
        return False
    if normalized == "POST" and urlsplit(url).path in _AUDITED_POST_READ_PATHS:
        return False
    return True
