# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generic, fail-soft identifier → human-name resolution for rendered output.

When a rendered answer / result / tool-trace surfaces a raw identifier value (a
SiteWise ``asset_id`` UUID, an ``event_id``, …) that the knowledge graph can
resolve to a human name, annotate it as ``Name (a380d7e4…)`` so the user sees the
name, with the id kept as a short secondary reference. If NO name is known the id
is left EXACTLY as-is.

Design contract (mirrors ``entity_render`` / ``coverage`` — ADR 0001/0021):
  * GENERIC — driven purely by graph id→label lookups over generic id/name node
    properties; carries NO dataset literals (no asset names, ids, or system ids).
  * FAIL-SOFT — any lookup/parse/format failure leaves the text unchanged; an
    unknown id is never altered. Enrichment must never break a render.
  * The graph read is an INJECTABLE, LAZY, no-graph-safe seam (imports ``.db`` on
    first call; degrades to ``[]`` when the runtime deps/graph are absent), so the
    pure helpers unit-test with ZERO AWS.

The id→name source is the graph itself: SiteWise/asset nodes are seeded as
``(:Asset {source_id: <id>, name: <human name>})`` (see onboard.py), and the
generic lookup also reads common id/name properties on ANY node, so it resolves
whatever id→label mappings the graph holds without per-connector code.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager, nullcontext

logger = logging.getLogger(__name__)

# Canonical UUID token — the shape SiteWise asset_ids and event_ids take. Matched
# with word-ish boundaries so a UUID embedded in prose/CSV/JSON is found but a
# longer hex run is not partially matched.
_UUID_RE = re.compile(
    r"(?<![0-9a-fA-F-])"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(?![0-9a-fA-F-])"
)

# Bound the per-render lookup so a huge result cannot issue an unbounded graph
# query; ids beyond the cap are simply left unresolved (fail-soft).
_MAX_IDS_PER_LOOKUP = 50

# How many leading chars of the id to keep as the secondary reference.
_ID_PREFIX_CHARS = 8

# Process-local memo of id → name (and id → "" for a confirmed miss) so repeated
# renders of the same ids don't re-query the graph. Bounded, best-effort.
_CACHE: dict[str, str] = {}
_CACHE_MAX = 2000

#: Limits of one name lookup (``db.single_attempt_graph_reads``: one attempt,
#: never resubmitted). A name is display-only, so a slow graph leaves the id as
#: it is rather than holding the render that asked.
LOOKUP_TIMEOUT_MS = 3_000
LOOKUP_CONNECT_TIMEOUT_S = 2.0
#: After a lookup that failed, no lookup is sent for this long: during an
#: outage a render pays for one failed attempt, not one per id-bearing text.
FAILURE_PAUSE_S = 30.0
#: Monotonic time before which no lookup is sent (set by a failed lookup).
_PAUSED_UNTIL = 0.0
#: One lookup at a time in the process, so at the start of an outage one
#: request reaches the failing graph; a render that finds the slot taken waits
#: for it (at most :data:`LOOKUP_WAIT_S`), then sees its names or its pause.
_LOOKUP_SLOT = threading.Lock()
#: How long a render waits for another render's lookup. A healthy lookup takes
#: well under this; a render that waits longer sends nothing and shows the ids
#: whose names are already known.
LOOKUP_WAIT_S = 3.0
#: Most lookups :func:`annotate_ids_in_lines` sends for one set of lines; ids
#: past them are named only from names already known. A trace step's lines
#: hold fewer ids than one lookup asks for, so this never cuts a stored step.
MAX_LINE_LOOKUPS = 2


def _now() -> float:
    return time.monotonic()


def neptune_query(cypher: str, parameters=None) -> list:
    """The graph-read seam — a lazy, no-graph-safe Neptune read. Imports ``.db`` on
    first call so this module imports without pg8000/boto3; degrades to ``[]``
    when the graph/runtime deps are absent or the query fails. A failed query is
    noted as an incomplete read (``db.note_incomplete_read``), so
    :func:`resolve_id_names` does not cache its ids as confirmed misses."""
    try:
        from .db import neptune_query as _nq
    except Exception:  # nosec B110 - graph/runtime deps absent → no-graph safe
        return []
    try:
        return _nq(cypher, parameters)
    except Exception:  # nosec B110 - fail-soft, but the failure is noted
        _note_failed_lookup("name_lookup_failed")
        return []


def _note_failed_lookup(category: str) -> None:
    try:
        from .db import note_incomplete_read
        note_incomplete_read(category)
    except Exception:  # nosec B110 - no graph layer, nothing to record
        pass


@contextmanager
def _failed_lookups():
    """The incomplete reads recorded inside (``db.recording_incomplete_reads``:
    a graph read degraded to no rows), or an empty list when the graph layer
    is absent. Each is noted again on exit for an enclosing recording scope,
    which an inner scope would otherwise hide."""
    try:
        from .db import recording_incomplete_reads
    except Exception:  # nosec B110 - no graph layer, nothing can be recorded
        yield []
        return
    with recording_incomplete_reads() as failures:
        yield failures
    for category in failures:
        _note_failed_lookup(category)


def extract_id_tokens(text: str) -> list[str]:
    """Return the distinct UUID-shaped identifier tokens present in ``text``.

    Order-preserving + de-duplicated. Pure. An empty/non-string input yields ``[]``.
    """
    if not text or not isinstance(text, str):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _UUID_RE.findall(text):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def resolve_id_names(ids, query=neptune_query) -> dict[str, str]:
    """Resolve identifier values → human names via the graph. Fail-soft → ``{}``.

    GENERIC: matches any node carrying one of the common identifier properties
    (``source_id`` / ``id`` / ``event_id``) equal to a requested id, and returns
    the first human-name-ish property (``name`` / ``label`` / ``title`` /
    ``display_name``). A row whose name is empty or merely echoes the id is
    discarded (no useful name). Uses a process-local cache; only un-cached ids hit
    the graph, one lookup at a time in the process (:func:`_single_lookup`), each
    one short attempt (:data:`LOOKUP_TIMEOUT_MS`). After a lookup that failed
    none is sent for :data:`FAILURE_PAUSE_S`, and its ids are not cached as
    misses, so they are asked again once the pause has passed. Never raises:
    any failure resolves those ids to nothing.
    """
    wanted = [i for i in (ids or []) if isinstance(i, str) and i]
    if not wanted:
        return {}
    known = cached_names(wanted)
    to_query = [i for i in wanted if _CACHE.get(i) is None]
    if to_query and _now() >= _PAUSED_UNTIL:
        known.update(_single_lookup(to_query, query))
    return known


def cached_names(ids) -> dict[str, str]:
    """The names already known for ``ids``, with no graph lookup."""
    return {i: _CACHE[i] for i in ids or [] if isinstance(i, str) and _CACHE.get(i)}


def prefetch_names(ids, *, budget_s: float, query=neptune_query) -> dict[str, str]:
    """The names of ``ids``, looked up in lookups of the usual size, starting
    none once ``budget_s`` has passed. For a caller that then names many sets
    of lines with them (``annotate_ids_in_lines(..., names=...)``): whenever
    the lookups finish inside the budget, each set is named exactly as its
    own :func:`annotate_ids_in_lines` would have named it."""
    wanted = [i for i in dict.fromkeys(ids or []) if isinstance(i, str)]
    names = cached_names(wanted)
    unknown = [i for i in wanted if i not in names]
    started = _now()
    for at in range(0, len(unknown), _MAX_IDS_PER_LOOKUP):
        if _now() - started > budget_s:
            break
        names.update(resolve_id_names(unknown[at:at + _MAX_IDS_PER_LOOKUP], query=query))
    return names


def _single_lookup(to_query: list, query) -> dict[str, str]:
    """The names of the still-unknown ids of ``to_query`` (at most one
    lookup's worth), looked up holding the process's one lookup slot and
    cached. A render that cannot take the slot within :data:`LOOKUP_WAIT_S`
    sends nothing. Inside the slot the pause and the cache are read again, so
    the renders that waited for a failed lookup send none, and ids another
    render just named are not asked twice. A lookup that failed starts the
    pause and caches no miss."""
    global _PAUSED_UNTIL
    if not _LOOKUP_SLOT.acquire(timeout=LOOKUP_WAIT_S):
        return {}
    try:
        known = cached_names(to_query)
        if _now() < _PAUSED_UNTIL:
            return known
        batch = [i for i in to_query if _CACHE.get(i) is None][:_MAX_IDS_PER_LOOKUP]
        if not batch:
            return known
        rows, failed = _query_names(batch, query)
        if failed:
            _PAUSED_UNTIL = _now() + FAILURE_PAUSE_S
        found = _names_in(rows, batch)
        # A real name, or "" for a confirmed miss; a failed lookup confirms none.
        for i in batch:
            if i in found or not failed:
                _cache_put(i, found.get(i, ""))
        return {**known, **found}
    finally:
        _LOOKUP_SLOT.release()


def _names_in(rows: list, batch: list) -> dict[str, str]:
    """The usable names in a lookup's ``rows``: an id the lookup asked for,
    with a name that is not empty and not the id itself."""
    asked = _id_set(batch)
    found: dict[str, str] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        name = r.get("name")
        if not isinstance(rid, str) or rid not in asked:
            continue
        if isinstance(name, str) and name.strip() and name.strip() != rid:
            found[rid] = name.strip()
    return found


def _query_names(batch: list, query) -> "tuple[list, bool]":
    """``(rows, failed)`` of one name lookup, sent as one short attempt
    (:func:`_short_lookup`): ``failed`` when it raised or the graph layer
    recorded it as incomplete (a read degraded to no rows)."""
    with _short_lookup(), _failed_lookups() as failures:
        try:
            rows = query(
                "MATCH (n) WHERE n.source_id IN $ids OR n.id IN $ids "
                "OR n.event_id IN $ids "
                "RETURN coalesce(n.source_id, n.id, n.event_id) AS id, "
                "coalesce(n.name, n.label, n.title, n.display_name) AS name",
                {"ids": batch},
            ) or []
        except Exception as e:  # noqa: BLE001 - fail-soft; leave these ids unresolved
            logger.info("id→name resolve skipped: %s", e)
            return [], True
    return rows, bool(failures)


def _short_lookup():
    """``db.single_attempt_graph_reads`` with the lookup limits, or nothing
    when the graph layer is absent."""
    try:
        from .db import single_attempt_graph_reads
    except Exception:  # nosec B110 - no graph layer, nothing to limit
        return nullcontext()
    return single_attempt_graph_reads(timeout_ms=LOOKUP_TIMEOUT_MS,
                                      connect_timeout_s=LOOKUP_CONNECT_TIMEOUT_S)


def annotate_ids_in_lines(lines: list, query=neptune_query, *, names: "dict | None" = None) -> list:
    """``lines`` with graph-known ids named, each line as
    :func:`annotate_ids_in_text` would name it alone, but with the ids of all
    the lines looked up together: at most :data:`MAX_LINE_LOOKUPS` lookups,
    a count and never elapsed time, so the same lines with the same graph
    answers are named the same way whoever asks. Given ``names`` (from
    :func:`prefetch_names`) it sends no lookup and names from those and the
    names already known. Each line is annotated with the names of its own
    ids only, so a name holding another line's id is shown as the graph gave
    it. Fail-soft: a line keeps its text when no name is found."""
    try:
        texts = [line if isinstance(line, str) else "" for line in lines or []]
        ids = extract_id_tokens("\n".join(texts))
        known = cached_names(ids)
        if names is not None:
            known.update({i: names[i] for i in ids if names.get(i)})
        else:
            step = _MAX_IDS_PER_LOOKUP
            for at in range(0, min(len(ids), step * MAX_LINE_LOOKUPS), step):
                known.update(resolve_id_names(ids[at:at + step], query=query))
        if not known:
            return list(lines or [])
        return [annotate_text(line, {i: known[i] for i in extract_id_tokens(line) if i in known})
                if line else line for line in texts]
    except Exception:  # nosec B110 - enrichment is best-effort, never break a render
        return list(lines or [])


def annotate_text(text: str, id_to_name: dict[str, str],
                  id_prefix_chars: int = _ID_PREFIX_CHARS) -> str:
    """Substitute each known id in ``text`` with ``Name (idprefix…)``. Fail-soft.

    Only ids present in ``id_to_name`` with a non-empty, non-identical name are
    annotated; every other token (including unknown ids) is left UNCHANGED.
    One pass over the original text: every id is found there, and an inserted
    name is never searched again, so a name holding another known id is shown
    exactly as the graph gave it (the evidence card's trace names its answer
    the same way, ``frontend/src/chat/trace-id-names.ts``). Pure and naturally
    idempotent: the full id is replaced by ``name (prefix…)`` so the original
    id token no longer matches on a second pass. No dataset literals.
    """
    if not text or not isinstance(text, str) or not id_to_name:
        return text
    known = {i: n for i, n in id_to_name.items()
             if isinstance(i, str) and i and isinstance(n, str) and n and n != i and i in text}
    if not known:
        return text
    ids = re.compile("|".join(re.escape(i) for i in sorted(known, key=len, reverse=True)))

    def _named(match) -> str:
        prefix = match.group(0)[:id_prefix_chars]
        name = known[match.group(0)]
        return f"{name} ({prefix}…)" if prefix else name

    return ids.sub(_named, text)


def annotate_ids_in_text(text: str, query=neptune_query) -> str:
    """Resolve any graph-known identifier tokens in ``text`` to human names and
    annotate them in place. Fail-soft — returns ``text`` unchanged on any problem
    or when no id resolves. This is the one-call entry point for render paths.

    No graph call is made when ``text`` carries no id tokens (the common case), so
    non-id renders pay nothing.
    """
    try:
        if not text or not isinstance(text, str):
            return text
        ids = extract_id_tokens(text)
        if not ids:
            return text
        names = resolve_id_names(ids, query=query)
        if not names:
            return text
        return annotate_text(text, names)
    except Exception:  # nosec B110 - enrichment is best-effort, never break a render
        return text


def names_in_text(text: str, query=neptune_query) -> dict[str, str]:
    """``{id: name}`` for the graph-known ids in ``text``, each name as the
    graph gave it, in the order the ids first appear: the first
    :data:`_MAX_IDS_PER_LOOKUP` ids, looked up through the same bounded
    resolver as :func:`annotate_ids_in_text` (:func:`resolve_id_names`: at
    most one lookup). For a page that places the names itself after it has
    parsed the text, so a name is never read as markup. Fail-soft: ``{}`` on
    any problem, with no id, or when no name is found."""
    try:
        ids = extract_id_tokens(text)[:_MAX_IDS_PER_LOOKUP]
        if not ids:
            return {}
        names = resolve_id_names(ids, query=query)
        return {i: names[i] for i in ids if names.get(i)}
    except Exception:  # nosec B110 - enrichment is best-effort, never break a render
        return {}


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _id_set(ids) -> set[str]:
    return {i for i in ids if isinstance(i, str)}


def _cache_put(key: str, value: str) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()  # bounded, best-effort — simplest safe eviction
    _CACHE[key] = value
