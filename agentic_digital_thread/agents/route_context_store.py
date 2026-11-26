# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ROUTE context each system delegation was sent, kept for Recent sessions.

The gateway merges the route seed's facts into a delegation's ``kg_context``
(``route_facts``) after the model wrote its arguments, so the stored turn the
Recent sessions reload rebuilds from holds only the model's own arguments.
The live chat receives the merged block with the call's tool result
(``chat_client``, ``route_context``) and puts it here; ``GET
/api/memory/events`` reads it back for the same session's calls and replays
it on their tool results (:func:`for_session`). Display metadata only: it
never goes back to the model, and it is neither written nor read while the
response-integrity gate runs (the callers skip it).

Thin wrapper over :class:`agents.job_store.JobStore` (``kind="route-context"``,
the shared onboarding-sessions table, fail-soft to in-memory, TTL'd), keyed
``{session_id}:{tool_use_id}``. Each entry holds the owner's digest, and a read
for another owner misses. Bounded at write: one block of at most
:data:`MAX_CHARS` characters per call, at most :data:`MAX_READ` calls per read
(one ``GetItem`` each, the read the provenance-query store already makes on
this table).
Logs carry lengths and digests only.
"""
from __future__ import annotations

import logging
import re

from agents.job_store import JobStore
from agents.provenance_store import SESSION_ID_RE, digest12

logger = logging.getLogger(__name__)

_KIND = "route-context"
#: A reopened chat is often the same day's, sometimes a later day's; the
#: entries are small and bounded, and an expired one just reloads without a
#: routing context (the stored model arguments, as before).
ROUTE_CONTEXT_TTL_SECONDS = 7 * 24 * 3600
MAX_CHARS = 4000
MAX_READ = 64
_TOOL_USE_ID_RE = re.compile(r"^[A-Za-z0-9._:#-]{1,128}$")

_job_store: JobStore | None = None


def _jobs() -> JobStore:
    global _job_store
    if _job_store is None:
        _job_store = JobStore(kind=_KIND, ttl_seconds=ROUTE_CONTEXT_TTL_SECONDS)
    return _job_store


def _key(session_id: object, tool_use_id: object) -> "str | None":
    if not (isinstance(session_id, str) and SESSION_ID_RE.match(session_id)):
        return None
    if not (isinstance(tool_use_id, str) and _TOOL_USE_ID_RE.match(tool_use_id)):
        return None
    return f"{session_id}:{tool_use_id}"


def put(owner_id: str, session_id: str, tool_use_id: str, route_context: str) -> bool:
    """Keep the route context one call was sent; ``False`` when the ids or
    the text are not usable. Never raises."""
    key = _key(session_id, tool_use_id)
    if key is None or not owner_id or not isinstance(route_context, str) \
            or not route_context.strip():
        return False
    try:
        _jobs()[key] = {"owner": digest12(str(owner_id)),
                        "route_context": route_context[:MAX_CHARS]}
    except Exception:  # noqa: BLE001 - display metadata never breaks the stream
        logger.debug("route_context_store: put skipped", exc_info=True)
        return False
    logger.info("route_context_store: put sid_sha=%s chars=%d", digest12(session_id),
                min(len(route_context), MAX_CHARS))
    return True


def for_session(owner_id: str, session_id: str, tool_use_ids) -> dict:
    """``{tool_use_id: route context}`` for the given calls of the owner's
    session (at most :data:`MAX_READ`); ``{}`` on any miss. Never raises."""
    keys = {}
    for tool_use_id in list(dict.fromkeys(tool_use_ids or ()))[:MAX_READ]:
        key = _key(session_id, tool_use_id)
        if key is not None:
            keys[key] = tool_use_id
    if not keys or not owner_id:
        return {}
    owner, found = digest12(str(owner_id)), {}
    try:
        for key, tool_use_id in keys.items():
            entry = _jobs().get(key)
            if isinstance(entry, dict) and entry.get("owner") == owner \
                    and isinstance(entry.get("route_context"), str):
                found[tool_use_id] = entry["route_context"]
    except Exception:  # noqa: BLE001 - a reload without the context is the fallback
        logger.debug("route_context_store: read skipped", exc_info=True)
    return found
