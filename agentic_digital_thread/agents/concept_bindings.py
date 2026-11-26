# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bounded, revision-pinned per-FIELD concept binding read (ADR-0067 hook).

WHY THIS EXISTS
---------------
``response_integrity_concepts`` implements the deterministic label-vs-provenance
rule but is PURE: it takes already-read graph rows. Until something supplied
those rows every binding stayed ``unresolved`` and every shadow verdict was
honestly ``unknown`` — the check was inert. This module is that supplier: the
one bounded graph read that answers "which canonical concepts does THIS system's
THIS physical field map to, and did we enumerate them all?".

CONTRACT
--------
* **Per FIELD, never per system.** The rule convicts on the EXACT cited field;
  one system may legitimately hold several concepts in different fields. The
  read is keyed ``(system_id, "table.column")`` exactly as
  ``SourceEvidence.physical_field`` spells it.
* **ONE traversal home.** The field-evidence predicate is
  :data:`concept_authority.FIELD_EVIDENCE_TRAVERSAL` — COMPOSED here, never
  re-spelled. Entity-level ``MAPS_TO`` must never satisfy the predicate, and a
  divergent copy would make one surface convict where another acquits. The
  ``SchemaEntry``/``Field`` nodes are bound out of the matched PATH by label so
  this composition stays agnostic to the fragment's internal shape.
* **``complete`` is affirmative, and expensive to earn.** The graph reports the
  TOTAL distinct mapping count alongside a capped list; the binding is
  :data:`~agents.response_integrity_concepts.BINDING_COMPLETE` only when that
  count is present, strictly UNDER :data:`MAX_CONCEPTS_PER_FIELD`, and equal to
  the number of rows actually returned. Anything else — including a read
  exactly AT the cap, where truncation is indistinguishable from exact
  enumeration — is ``capped`` ⇒ the verdict is
  ``unknown``. A wrongly-claimed ``complete`` manufactures FALSE violations,
  which is the precise failure mode this workstream exists to correct.
* **Revision-pinned.** Every resolved row carries its system's
  ``System.schema_version`` (the same stamp ``schema_cache`` keys on, bumped by
  every re-onboard). A row with no version is DROPPED — an unpinnable mapping
  cannot support a completeness claim. The pin handed to the checker is one
  opaque digest over the ``(system, schema_version)`` pairs the read actually
  observed, so any re-onboard of any cited system changes it and every binding
  from one read agrees with every other.
* **Bounded.** Distinct fields per read, systems per read, concepts per field,
  wall-clock budget, cache entries and cache age all have explicit caps.
* **Fail-open, absolutely.** Unavailable client, graph error, timeout, breaker
  open, unparseable rows, missing revision — all return NO mappings, which
  leaves bindings ``unresolved`` and the verdict ``unknown``. Nothing here
  raises to a caller, nothing retries, and a timed-out read opens a cooldown
  breaker so a sick graph can never be re-dialled per turn.
* **Shadow only.** Nothing in this module can change an answer byte, add a
  reason code, or block a turn.

Stdlib + the frozen contracts only; the graph client is imported lazily inside
the ONE seam (:func:`_field_concepts_graph_read`) so this module stays
offline-importable and the offline lanes have a single thing to patch.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from typing import Any, Callable, Iterable, Mapping

from . import concept_authority
from .lease_guard import submit_in_context

logger = logging.getLogger(__name__)

# Per-field mapping cap. A field mapping to more canonical concepts than this
# is not enumerable under the cap, so its binding can only ever be "capped".
MAX_CONCEPTS_PER_FIELD = 8

# Read fan-out caps. Evidence beyond these caps is simply NOT resolved (it stays
# "unresolved" ⇒ "unknown"), never partially claimed as complete.
MAX_FIELDS_PER_READ = 32
MAX_SYSTEMS_PER_READ = 8

# Wall-clock budget for the whole read. A shadow surface must never lend its
# latency to a real turn, and botocore's own timeouts are far longer than any
# answer can afford to wait.
READ_TIMEOUT_SECONDS = 2.0

# After a timed-out read, stop dialling the graph for this long. One turn does
# at most ONE round trip; a sick graph gets none at all.
BREAKER_COOLDOWN_SECONDS = 60.0

# Bounded in-process result cache. Short by design: a re-onboard inside the TTL
# would serve internally-consistent but stale bindings, and this is a
# shadow-only signal, so freshness beats hit rate.
CACHE_TTL_SECONDS = 60.0
MAX_CACHE_ENTRIES = 64

# Opaque revision prefix; ``1`` versions the digest recipe, not the graph.
REVISION_PREFIX = "cb1-"
REVISION_DIGEST_CHARS = 24

# ONE round trip per turn. Pinned by test: the read is a single query, never a
# per-field loop.
MAX_GRAPH_ROUND_TRIPS_PER_READ = 1

# The per-field mapping read. It COMPOSES the single field-evidence traversal
# home; ``SchemaEntry``/``Field`` are recovered from the matched path by LABEL
# rather than by re-spelling the relationship chain with bound variables.
#
# ``concept_count`` is the TOTAL distinct mapping count and ``concepts`` the
# capped list: comparing them is what earns the affirmative "complete" marker.
_FIELD_CONCEPTS_CYPHER = (
    "UNWIND $specs AS spec "
    "MATCH path = (s:System {id: spec.system})"
    f"{concept_authority.FIELD_EVIDENCE_TRAVERSAL}(c:Concept) "
    "WITH spec, s, c, "
    "[n IN nodes(path) WHERE 'SchemaEntry' IN labels(n)][0] AS se, "
    "[n IN nodes(path) WHERE 'Field' IN labels(n)][0] AS fld "
    "WHERE toLower(coalesce(se.name, '')) IN spec.tables "
    "AND toLower(coalesce(fld.name, '')) = spec.column "
    "WITH spec, s, collect(DISTINCT coalesce(c.id, c.name)) AS concepts "
    "RETURN spec.system AS system_id, spec.field AS field, "
    "coalesce(s.schema_version, '') AS schema_version, "
    "size(concepts) AS concept_count, "
    f"concepts[0..{MAX_CONCEPTS_PER_FIELD}] AS concepts"
)

_lock = threading.Lock()
_cache: "dict[tuple, tuple[float, dict, str]]" = {}
_breaker_until = 0.0
_executor: "ThreadPoolExecutor | None" = None
_round_trips = 0


def reset() -> None:
    """Clear the cache, the breaker and the round-trip counter (tests + recovery)."""
    global _breaker_until, _round_trips
    with _lock:
        _cache.clear()
        _breaker_until = 0.0
        _round_trips = 0


def round_trips() -> int:
    """Graph round trips this process has spent on concept bindings (telemetry/tests)."""
    with _lock:
        return _round_trips


def _text(value: object) -> str:
    return str(value or "").strip()


def field_specs(evidence: Iterable[Any]) -> "tuple[dict, ...]":
    """Bounded, deterministic read specs for one turn's evidence.

    One spec per DISTINCT ``(system_id, physical_field)``. Evidence with no
    system or no ``table.column`` physical field yields no spec — a bare
    semantic field cannot identify a graph ``Field`` unambiguously, and guessing
    is exactly how a false ``complete`` would be born. ``tables`` carries both
    the full table spelling and its last dot-segment so a schema-qualified
    projection still matches an unqualified ``SchemaEntry.name``.
    """
    specs: "dict[tuple[str, str], dict]" = {}
    systems: list[str] = []
    for item in evidence:
        system = _text(getattr(item, "system_id", ""))
        field = _text(getattr(item, "physical_field", ""))
        table, _, column = field.rpartition(".")
        table, column = table.strip(), column.strip()
        if not system or not table or not column:
            continue
        key = (system.casefold(), field.casefold())
        if key in specs:
            continue
        if system not in systems:
            if len(systems) >= MAX_SYSTEMS_PER_READ:
                continue
            systems.append(system)
        tables = list(dict.fromkeys(
            (table.casefold(), table.rpartition(".")[2].casefold())))
        specs[key] = {"system": system, "field": field,
                      "tables": [t for t in tables if t], "column": column.casefold()}
        if len(specs) >= MAX_FIELDS_PER_READ:
            break
    return tuple(specs.values())


def _field_concepts_graph_read(
    specs: "tuple[dict, ...]", query: "Callable[..., Any] | None" = None,
) -> list:
    """The ONE graph seam for concept bindings (offline lanes patch THIS).

    ``query`` is an injectable client seam for tests; production uses
    :func:`agents.db.neptune_query`, which already returns ``[]`` on failure.
    """
    if query is None:
        from .db import neptune_query

        query = neptune_query
    return query(_FIELD_CONCEPTS_CYPHER, {"specs": list(specs)}) or []


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="concept-binding-read")
        return _executor


def _bounded_read(specs: "tuple[dict, ...]") -> "list | None":
    """Run the seam under a wall-clock budget. ``None`` means "no rows, be quiet".

    A single worker plus the cooldown breaker is deliberate: a hung graph must
    degrade to instant ``unknown`` verdicts, never to a per-turn retry storm.
    """
    global _breaker_until, _round_trips
    with _lock:
        if time.monotonic() < _breaker_until:
            return None
    try:
        future = submit_in_context(_pool(), _field_concepts_graph_read, specs)
    except Exception as exc:  # nosec B110 - shadow read; never fails a turn
        logger.warning("concept binding read could not start: %r", exc)
        return None
    with _lock:
        _round_trips += 1
    try:
        return future.result(timeout=READ_TIMEOUT_SECONDS)
    except _FutureTimeout:
        future.cancel()
        with _lock:
            _breaker_until = time.monotonic() + BREAKER_COOLDOWN_SECONDS
        logger.warning(
            "concept binding read exceeded %.1fs; bindings stay unresolved",
            READ_TIMEOUT_SECONDS)
        return None
    except Exception as exc:  # nosec B110 - shadow read; never fails a turn
        logger.warning("concept binding read failed: %r", exc)
        return None


def _revision(versions: "Mapping[str, str]") -> str:
    """One opaque pin over the ``(system, schema_version)`` pairs observed."""
    payload = ";".join(f"{system}={versions[system]}" for system in sorted(versions))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{REVISION_PREFIX}{digest[:REVISION_DIGEST_CHARS]}"


def _index(rows: "Iterable[Any]") -> "tuple[dict, str]":
    """Index graph rows into resolver input plus the pinned revision.

    A row is usable only when it names a system, a field, a non-empty
    ``schema_version`` AND a well-formed concept list (a list-typed column with
    every id non-blank — the traversal cannot legitimately produce a blank or
    empty enumeration, so such a row is malformed and dropped). ``complete``
    additionally requires the reported total count to be present, strictly
    UNDER the cap, and equal to the returned list length — at/over the cap the
    slice makes truncation indistinguishable from exact enumeration, so the
    boundary degrades to capped (⇒ ``unknown``), never to an absence claim.
    """
    mappings: dict = {}
    versions: "dict[str, str]" = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        system = _text(row.get("system_id"))
        field = _text(row.get("field"))
        version = _text(row.get("schema_version"))
        if not system or not field or not version:
            continue
        raw = row.get("concepts")
        if not isinstance(raw, (list, tuple)):
            continue  # malformed column ⇒ unusable row, never a claim
        concepts = [_text(name) for name in raw if _text(name)]
        if not concepts or len(concepts) != len(raw):
            continue  # blank/absent concept ids ⇒ unusable row
        try:
            total = int(row.get("concept_count"))
        except (TypeError, ValueError):
            total = -1  # unknown enumeration ⇒ never affirmatively complete
        complete = (0 <= total < MAX_CONCEPTS_PER_FIELD and len(concepts) == total)
        mappings[(system, field)] = {
            "concepts": concepts[:MAX_CONCEPTS_PER_FIELD], "complete": complete,
        }
        versions[system] = version
    if not mappings:
        return {}, ""
    return mappings, _revision(versions)


def _cache_get(key: tuple) -> "tuple[dict, str] | None":
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        stamp, mappings, revision = entry
        if now - stamp > CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        return dict(mappings), revision


def _cache_put(key: tuple, mappings: dict, revision: str) -> None:
    with _lock:
        if len(_cache) >= MAX_CACHE_ENTRIES:
            for stale in sorted(_cache, key=lambda k: _cache[k][0])[
                    : max(1, len(_cache) - MAX_CACHE_ENTRIES + 1)]:
                _cache.pop(stale, None)
        _cache[key] = (time.monotonic(), dict(mappings), revision)


def read_field_concepts(evidence: Iterable[Any]) -> "tuple[dict, str]":
    """Field-to-concept mappings plus the pinned revision for one turn's evidence.

    The return value feeds
    :func:`agents.response_integrity_concepts.resolve_evidence_concepts`
    verbatim. ``({}, "")`` — the answer to every uncertainty — leaves bindings
    ``unresolved``, so the shadow verdict is ``unknown``.
    """
    specs = field_specs(evidence)
    if not specs:
        return {}, ""
    key = tuple(sorted((spec["system"], spec["field"]) for spec in specs))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    rows = _bounded_read(specs)
    if rows is None:
        return {}, ""
    mappings, revision = _index(rows)
    _cache_put(key, mappings, revision)
    return mappings, revision
