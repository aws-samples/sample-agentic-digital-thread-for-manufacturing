# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The provenance frame each answered turn streamed, kept for Recent sessions.

A live answer's Sources chips open their evidence cards from the turn's
``provenance`` metadata frame (``agents.provenance``, D1). The frame is built
from state that lives only while the turn runs (the integrity verdict, typed
evidence, the turn's rule labels), and AgentCore memory stores the turn's
messages, not its metadata frames. So the app keeps the frame the browser
received: ``server`` folds the turn's frames the way the chat's reducer does
(:func:`fold`) and keeps the result once the stream ends (:func:`keep`), under
the turn's identity (:func:`turn_identity`: the question, read through the one
shared :func:`turn_question` so the redacted stored copy reads the same, and
the set of call ids the stream showed); ``GET /api/memory/events`` reads the
session's frames once and returns each on the one reloaded question with the
same identity (:func:`for_turns`). Turns nothing tells apart get no frame.
The chat then replays it through the same reducer, so a reopened answer opens
the cards it opened live. The card's technical detail is redeemed on demand,
as live, from its own 24 h stores.

Thin wrapper over :class:`agents.job_store.JobStore` (``kind="turn-evidence"``,
the shared onboarding-sessions table, fail-soft to in-memory, TTL'd); no new
infrastructure. One item per actor and session, keyed by the full sha256 of
the actor id and the session id, so another actor's read is a different item;
the item also holds the owner digest, checked on read. Written with
``JobStore.mutate`` (a conditional write, the PutItem the table already
allows) and read with one ``GetItem`` per reload.

Bounds: a frame is kept only when it has 1 to ``MAX_PROVENANCE_RECORDS``
records that each carry a tool id and its compact JSON is within the
builder's own ``MAX_PAYLOAD_BYTES``; it is never cut. A session item holds at
most :data:`MAX_FRAMES` frames and :data:`MAX_SESSION_BYTES`, oldest dropped
first; nothing else ever drops an entry. Each kept frame records whether its
stream ran the response-integrity gate, so a turn the reload shows gated
never gets a frame an ungated stream sent. Logs carry digests and counts only.

The same item also records which tool charts each turn's stream sent the
browser (:class:`DeliveredCharts`), so a reload replays a stored chart only
when the live page got it: short fingerprints of the chart tags
(``orchestrator.chart_fingerprint``), never tag text, at most
:data:`MAX_TURN_CHARTS` per turn. They are kept in their own list beside the
frames, at most :data:`MAX_CHART_TURNS` turns and
:data:`MAX_CHART_RECORD_BYTES`, oldest dropped first, so the frames' own
bounds are exactly what they were: a chart record never evicts a frame. Same
write condition (a turn memory stores), TTL and item as the frames; an item
with no chart record has no ``charts`` key.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
from typing import Any, Iterable, Mapping

from agents.job_store import JobStore
from agents.provenance import (
    MAX_GRAPH_QUERY_REFS,
    MAX_PAYLOAD_BYTES,
    MAX_PROVENANCE_RECORDS,
)
from agents.provenance_store import SESSION_ID_RE, digest12

logger = logging.getLogger(__name__)

_KIND = "turn-evidence"
#: Like the route-context store: a reopened chat is often the same day's,
#: sometimes a later day's. An expired item reloads its answers without cards
#: and the chat says so.
TTL_SECONDS = 7 * 24 * 3600
#: Frames per session item (the newest kept) and bytes of their compact JSON.
MAX_FRAMES = 40
MAX_SESSION_BYTES = 128 * 1024
#: In-memory copies kept by one process (the fallback without a shared table).
_MEMORY_SESSIONS = 64
_TOOL_ID_RE = re.compile(r"^[A-Za-z0-9._:#-]{1,128}$")
#: The frame keys the chat's reducer reads, plus the builder's honest flag.
_FRAME_KEYS = ("records", "truncated", "graph_queries")
#: Chart fingerprints kept per turn (the first ones sent; ``cut`` says more
#: were), chart records per session item (the newest kept) and the most bytes
#: their compact JSON may take (a full record is about 0.7 KiB).
MAX_TURN_CHARTS = 32
MAX_CHART_TURNS = MAX_FRAMES
MAX_CHART_RECORD_BYTES = 32 * 1024
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
_CHART_TAG_RE = re.compile(r"<chart-block\b[^>]*/>")
_CHART_MARK = "<chart-block"
#: An incomplete tag longer than this is let go: a reload never replays a tag
#: that large (``memory_turns.MAX_RELOAD_CHART_BYTES`` is 1 MiB).
_MAX_CANDIDATE_CHARS = 2 * 1024 * 1024

_job_store: JobStore | None = None


def _jobs() -> JobStore:
    global _job_store
    if _job_store is None:
        _job_store = JobStore(kind=_KIND, ttl_seconds=TTL_SECONDS,
                              memory_cap=_MEMORY_SESSIONS,
                              memory_evictable=lambda _state: True)
    return _job_store


def _owner(owner_id: object) -> str:
    return hashlib.sha256(str(owner_id).encode("utf-8")).hexdigest()


def item_key(owner_id: str, session_id: str) -> "str | None":
    """The session item's key, or ``None`` when the ids are not usable."""
    if not owner_id or not (isinstance(session_id, str) and SESSION_ID_RE.match(session_id)):
        return None
    return f"{_owner(owner_id)}:{session_id}"


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))


_IDENTITY_RE = re.compile(r"^[0-9a-f]{64}$")


@functools.lru_cache(maxsize=1)
def _seed_step_ids() -> frozenset:
    """The fixed tool ids of the Knowledge Graph step a seeded turn streams
    first (``runtime_app._seed_trace_events``). The same in every turn, so
    they carry nothing about which turn it is, and the step is not one of the
    agent's stored messages."""
    from .orchestrator import GRAPH_FIRST_SEED_TOOL_ID, ROUTE_SEED_TOOL_ID
    return frozenset({GRAPH_FIRST_SEED_TOOL_ID, ROUTE_SEED_TOOL_ID})


def turn_question(text: str) -> str:
    """The turn's question as both sides read it, the ONE canonical form the
    turn identity hashes. The app passes the prompt it sent, the reload the
    stored user text, which the memory write redacted whole
    (``orchestrator._redact_session_message``, ``pii.redact_pii``) before the
    reload strips it. So, in order:

    1. the server seed and its separator are cut off
       (``memory_labels.turn_after_seed``, a ``str.find``): a seed of any size
       is never scanned or copied. No redaction match can reach across the
       separator, so the stored text after it is the redacted prompt;
    2. that short rest is redacted, as the memory write redacted it, including
       the temporal and plant preambles (a client locale is rendered inside
       the temporal one). Redaction is idempotent, so the stored copy's second
       pass changes nothing;
    3. the preambles are peeled (``memory_labels.peel_question_preambles``),
       as the reload peels the stored copy."""
    from .memory_labels import peel_question_preambles, turn_after_seed
    from .pii import redact_pii
    return peel_question_preambles(redact_pii(turn_after_seed(text or ""))).strip()


def turn_identity(turn_text: str, call_ids: Iterable[str]) -> str:
    """What tells one turn of a session from another, as both the live stream
    and the stored turn show it: the turn's question (:func:`turn_question`
    of the prompt the app sent or of the stored user text) and the set of the
    turn's call ids (the runtime's toolUseIds, unique within a turn but not
    across turns; the seed step's fixed ids left out). A sha256 digest, so
    the kept entry holds neither the question nor the ids."""
    ids = sorted({i for i in call_ids if isinstance(i, str) and i} - _seed_step_ids())
    canonical = json.dumps([turn_question(turn_text), ids], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _valid_identity(value: object) -> bool:
    return isinstance(value, str) and bool(_IDENTITY_RE.match(value))


def fold(current: "dict | None", provenance: object) -> "dict | None":
    """``current`` after one more ``provenance`` frame of the same turn, the
    way the chat's reducer folds them (``thread-store.ts`` ``onMetadata``): a
    frame carrying a records list replaces the records (and the truncated
    flag), and one carrying graph queries replaces those."""
    if not isinstance(provenance, dict):
        return current
    folded = dict(current or {})
    if isinstance(provenance.get("records"), list):
        folded["records"] = provenance["records"]
        folded["truncated"] = provenance.get("truncated") is True
    if isinstance(provenance.get("graph_queries"), list) and provenance["graph_queries"]:
        folded["graph_queries"] = provenance["graph_queries"]
    return folded or current


class DeliveredCharts:
    """The complete chart tags one turn's stream sent the browser, harvested
    the way the chat harvests them (``answer-segments.ts`` ``pushToken``):
    matched in the answer text of the current run, which each tool call ends
    (:meth:`boundary`), a repeat counted once. Holds only fingerprints
    (``orchestrator.chart_fingerprint``), the first :data:`MAX_TURN_CHARTS`
    distinct ones in order; ``cut`` is True when more were sent. The run text
    kept between chunks is the unmatched rest from the first possible tag on,
    so a tag split across chunks still counts."""

    __slots__ = ("_run", "fingerprints", "cut")

    def __init__(self) -> None:
        self._run = ""
        self.fingerprints: list[str] = []
        self.cut = False

    def boundary(self) -> None:
        self._run = ""

    def feed(self, text: object) -> None:
        if not isinstance(text, str) or not text:
            return
        run = self._run + text
        if _CHART_MARK in run:
            end = 0
            for match in _CHART_TAG_RE.finditer(run):
                self._add(match.group(0))
                end = match.end()
            run = run[end:]
        start = run.find(_CHART_MARK)
        self._run = run[start:] if start >= 0 else run[-(len(_CHART_MARK) - 1):]
        if len(self._run) > _MAX_CANDIDATE_CHARS:
            self._run = ""

    def _add(self, tag: str) -> None:
        from .orchestrator import chart_fingerprint
        fingerprint = chart_fingerprint(tag)
        if fingerprint in self.fingerprints:
            return
        if len(self.fingerprints) >= MAX_TURN_CHARTS:
            self.cut = True
            return
        self.fingerprints.append(fingerprint)


def _chart_entry(turn: str, charts: object) -> "dict | None":
    """The chart record kept for a turn whose stream sent ``charts``
    (:class:`DeliveredCharts`), an empty list when it sent none: the reload
    then knows the live page showed no chart, which a missing record cannot
    say. ``None`` when ``charts`` is not a delivery record or holds a value
    that is not a fingerprint."""
    if not isinstance(charts, DeliveredCharts):
        return None
    fingerprints = list(charts.fingerprints)[:MAX_TURN_CHARTS]
    if not all(isinstance(f, str) and _FINGERPRINT_RE.match(f) for f in fingerprints):
        return None
    return {"turn": turn, "charts": fingerprints, "cut": charts.cut is True}


def _chart_record(entry: object) -> "dict | None":
    """A kept chart record in the shape the reload reads, or ``None`` when
    it is not one. An empty list is a record: the stream sent no chart."""
    if not isinstance(entry, dict) or not _valid_identity(entry.get("turn")):
        return None
    fingerprints = entry.get("charts")
    if not (isinstance(fingerprints, list) and len(fingerprints) <= MAX_TURN_CHARTS
            and all(isinstance(f, str) and _FINGERPRINT_RE.match(f) for f in fingerprints)):
        return None
    return {"fingerprints": list(fingerprints), "cut": entry.get("cut") is True}


def _tool_ids(frame: object) -> "list[str] | None":
    """The record tool ids of a frame in the kept shape, or ``None`` when the
    frame is not one (no records, too many, a record without a tool id,
    malformed graph queries, or over the payload bound)."""
    if not isinstance(frame, dict) or set(frame) - set(_FRAME_KEYS):
        return None
    records = frame.get("records")
    if not isinstance(records, list) or not 0 < len(records) <= MAX_PROVENANCE_RECORDS:
        return None
    ids = [r.get("tool_id") if isinstance(r, dict) else None for r in records]
    if not all(isinstance(i, str) and _TOOL_ID_RE.match(i) for i in ids):
        return None
    queries = frame.get("graph_queries", [])
    if not (isinstance(queries, list) and len(queries) <= MAX_GRAPH_QUERY_REFS
            and all(isinstance(q, dict) for q in queries)):
        return None
    return list(dict.fromkeys(ids)) if _size(frame) <= MAX_PAYLOAD_BYTES else None


def _kept_shape(frame: dict) -> dict:
    return {k: frame[k] for k in _FRAME_KEYS if k in frame}


def _bounded(entries: list) -> list:
    """The newest entries within :data:`MAX_FRAMES` and :data:`MAX_SESSION_BYTES`
    (the compact JSON of the list), dropping the oldest first; the newest
    entry is always kept (each is within ``MAX_PAYLOAD_BYTES``)."""
    entries = entries[-MAX_FRAMES:]
    sizes = [_size(e) + 1 for e in entries]  # each entry and its separator
    total, start = sum(sizes) + 1, 0         # plus the brackets
    while start < len(entries) - 1 and total > MAX_SESSION_BYTES:
        total -= sizes[start]
        start += 1
    return entries[start:]


def _bounded_charts(entries: list) -> list:
    """The newest chart records within :data:`MAX_CHART_TURNS` and
    :data:`MAX_CHART_RECORD_BYTES`, dropping the oldest first."""
    entries = entries[-MAX_CHART_TURNS:]
    while len(entries) > 1 and _size(entries) > MAX_CHART_RECORD_BYTES:
        entries = entries[1:]
    return entries


def keep(owner_id: str, session_id: str, frame: object, *, turn: str, gated: bool,
         charts: object = None) -> bool:
    """Keep one answered turn's folded frame and the chart tags its stream
    sent (``charts``, a :class:`DeliveredCharts`) for the owner's session
    under the turn's identity (:func:`turn_identity`); ``False`` when the ids
    or the identity are not usable, there is neither a usable frame nor a
    delivery record, or the write did not land. Another turn's entry is never
    replaced: only the bounds drop entries, oldest first, the frames' and the
    chart records' each in their own list. Never raises."""
    key = item_key(owner_id, session_id)
    frame = _kept_shape(frame) if isinstance(frame, dict) else frame
    ids = _tool_ids(frame)
    chart_entry = _chart_entry(turn, charts) if _valid_identity(turn) else None
    if key is None or not _valid_identity(turn) or (ids is None and chart_entry is None):
        logger.info("turn_evidence_store: frame not kept sid_sha=%s",
                    digest12(str(session_id)))
        return False
    owner = digest12(_owner(owner_id))
    entry = {"turn": turn, "gated": bool(gated), "provenance": frame} if ids is not None else None

    def _append(current):
        owned = isinstance(current, dict) and current.get("owner") == owner
        frames, records = (_listed(current, "frames"), _listed(current, "charts")) \
            if owned else ([], [])
        new = {"owner": owner, "frames": _bounded(frames + [entry]) if entry else frames}
        records = _bounded_charts(records + [chart_entry]) if chart_entry else records
        if records:
            new["charts"] = records
        return new

    try:
        landed = _jobs().mutate(key, _append)
    except Exception:  # noqa: BLE001 - display metadata never breaks the stream
        logger.debug("turn_evidence_store: keep skipped", exc_info=True)
        return False
    logger.info("turn_evidence_store: kept sid_sha=%s records=%d charts=%d landed=%s",
                digest12(session_id), len(ids or ()),
                len(chart_entry["charts"]) if chart_entry else 0, landed)
    return landed


def _listed(item: dict, name: str) -> list:
    """The dict entries of one of the item's lists, ``[]`` when it has none."""
    values = item.get(name)
    return [e for e in values if isinstance(e, dict)] if isinstance(values, list) else []


def _read_item(owner_id: str, session_id: str) -> "dict | None":
    """The owner's session item, or ``None`` on any miss. One read."""
    key = item_key(owner_id, session_id)
    if key is None:
        return None
    try:
        item = _jobs().get(key)
    except Exception:  # noqa: BLE001 - a reload without cards is the fallback
        logger.debug("turn_evidence_store: read skipped", exc_info=True)
        return None
    if not (isinstance(item, dict) and item.get("owner") == digest12(_owner(owner_id))):
        return None
    return item


def _frame_entries(item: "dict | None") -> list:
    """The item's usable frame entries, oldest first."""
    if item is None:
        return []
    return [e for e in _listed(item, "frames")[-MAX_FRAMES:]
            if _valid_identity(e.get("turn")) and _tool_ids(e.get("provenance")) is not None]


def _single_matches(turns: "Mapping[int, tuple[str, bool]]", entries: list) -> dict:
    """``{turn key: entry}`` where exactly one kept entry has the turn's
    identity and exactly one reloaded turn has the entry's (fail closed)."""
    keys_by_turn: dict = {}
    for key, (identity, _gated) in turns.items():
        keys_by_turn.setdefault(identity, []).append(key)
    entries_by_turn: dict = {}
    for entry in entries:
        entries_by_turn.setdefault(entry["turn"], []).append(entry)
    return {keys[0]: entries_by_turn[identity][0] for identity, keys in keys_by_turn.items()
            if len(keys) == 1 and len(entries_by_turn.get(identity, [])) == 1}


def _frames_for(turns: "Mapping[int, tuple[str, bool]]", item: "dict | None") -> dict:
    """``{turn key: frame}`` from the item (:func:`for_turns`)."""
    found: dict = {}
    for key, entry in _single_matches(turns, _frame_entries(item) if turns else []).items():
        if turns[key][1] and entry.get("gated") is not True:
            continue  # an ungated stream's frame is never shown in a gated view
        found[key] = entry["provenance"]
    return found


def for_turns(owner_id: str, session_id: str,
              turns: "Mapping[int, tuple[str, bool]]") -> dict:
    """``{turn key: frame}`` for the owner's session. ``turns`` maps each
    reloaded question to (its :func:`turn_identity`, whether the reload shows
    that turn gated). A frame goes to a turn only when exactly one kept entry
    has the turn's identity and exactly one reloaded turn has the entry's:
    two turns that nothing tells apart, or two kept frames for one identity,
    get no frame (fail closed). A gated turn takes only a frame kept from a
    gated stream. ``{}`` on any miss. Never raises."""
    return _frames_for(turns, _read_item(owner_id, session_id) if turns else None)


def kept_for_turns(owner_id: str, session_id: str,
                   turns: "Mapping[int, tuple[str, bool]]") -> dict:
    """``{turn key: {"provenance": frame, "charts": record}}`` (either key
    only when kept) for the owner's session, from ONE read: the frame as
    :func:`for_turns` gives it, and the chart record (``{"fingerprints",
    "cut"}``) of what the turn's stream sent, matched the same fail-closed
    way. A gated turn gets no chart record: the reload replays no chart on
    it. ``{}`` on any miss. Never raises."""
    item = _read_item(owner_id, session_id) if turns else None
    found = {key: {"provenance": frame} for key, frame in _frames_for(turns, item).items()}
    records = [e for e in _listed(item, "charts")[-MAX_CHART_TURNS:]
               if _chart_record(e) is not None] if item else []
    for key, entry in _single_matches(turns, records).items():
        if not turns[key][1]:
            found.setdefault(key, {})["charts"] = _chart_record(entry)
    return found
