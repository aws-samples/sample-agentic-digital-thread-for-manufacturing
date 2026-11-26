# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system agent's KNOWN CONCEPT MAPPINGS, read from the graph per question.

The section used to be a provision-time snapshot: onboarding flattened the
mapper's concept-to-column pairs into the agent-config secret and the system
prompt rendered them once, under "use the mapped field directly". A steward's
later correction moved the graph edge but never the snapshot, so a wrong link
captured at onboarding kept steering every query until the system was
re-provisioned, and re-provisioning re-ran the mapper.

This module gives each delegated question the system's CURRENT
``Field-[:MAPS_TO]->Concept`` edges:

* change detection: every MAPS_TO writer bumps a small per-system
  ``mapping_generation`` on the System node (:data:`BUMP_GENERATION_CYPHER`).
  Each question reads only that value; the mapping rows are re-read when it
  moves (and at least every :data:`RELOAD_AFTER_SECONDS`, for a writer that
  did not bump), so a change is in effect on the next question;
* only edges that exist are rendered (a retired or repointed link is deleted,
  never flagged), and a retired Concept never renders;
* graph text is data. Concept, role, table and column values are collapsed to
  one line and must pass the repository's identifier rules (concept ids as
  vocabulary import writes them, ``schema_fields.validate_table_name``,
  ``metric_binding.validate_column_name``); a row that fails is left out and
  counted. The mapping lines are rendered inside the untrusted-data fence
  (``untrusted_text.render_fenced``); the fixed provenance policy follows the
  fence;
* provenance per line: an edge a person set or approved (``r.confirmed``, or
  a legacy ``user_corrected``/``user_confirmed`` method) is "user-confirmed"
  and keeps "use its field directly"; every other edge was inferred at
  onboarding and renders as "suggested", so it cannot outrank a business
  definition;
* the question's own concepts first: the ROUTE ``canonical_concept`` and the
  concepts the question names are rendered first and WHOLE. One that cannot
  be shown whole (too many lines for the section, or a by-concept read that
  failed or hit its cap) is withheld by name, never sent as a partial set.
  Other concepts follow as whole groups while room remains, with a count of
  the rest;
* when a read fails, the last good read (at most
  :data:`LAST_GOOD_MAX_AGE_SECONDS` old) is used, else the onboarding snapshot,
  each labelled as such and logged. After a failure the graph is not asked
  again for :data:`FAILURE_PAUSE_SECONDS`.

Generic: no system, table or column names. The graph read is a seam
(``query``); the default one uses short deadlines and a single attempt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from .metric_binding import validate_column_name
from .schema_fields import validate_table_name
from .untrusted_text import render_fenced, sanitize_untrusted

logger = logging.getLogger(__name__)

#: Most edges read for one system when its mappings are (re)loaded.
ROW_CAP = 2000
#: Most edges read by concept id for the question's own concepts.
TARGETED_ROW_CAP = 200
#: Rendered section bound (characters), whole concept groups.
SECTION_CAP = 4096
#: Reload the rows at least this often even when the generation stands.
RELOAD_AFTER_SECONDS = 600.0
#: How old the last good read may be and still stand in for a failed one.
LAST_GOOD_MAX_AGE_SECONDS = 900.0
#: How long the graph is left alone after a failed read.
FAILURE_PAUSE_SECONDS = 30.0
#: Legacy ``MAPS_TO.method`` values that mean a person set the edge (edges
#: written before ``r.confirmed`` existed).
CONFIRMED_METHODS = frozenset({"user_corrected", "user_confirmed"})
CONFIRMED = "user-confirmed"
SUGGESTED = "suggested"
_NON_CONCEPTS = frozenset({"", "needs_review", "unknown"})
_CONCEPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_ROLE = re.compile(r"[a-z_]{0,32}")
_GRAPH_CONNECT_TIMEOUT_S = 2
_GRAPH_READ_TIMEOUT_S = 5

#: The per-system mapping generation (one small read per question).
GENERATION_CYPHER = "MATCH (s:System {id: $sid}) RETURN s.mapping_generation AS g"
#: Written by every Field-MAPS_TO writer, for each system it touched.
BUMP_GENERATION_CYPHER = (
    "MATCH (s:System {id: $sid}) "
    "SET s.mapping_generation = coalesce(s.mapping_generation, 0) + 1")
#: The same bump, answering the generation it wrote (a provision reads it
#: back before it reports provisioned).
BUMP_GENERATION_RETURNING_CYPHER = (
    BUMP_GENERATION_CYPHER + " RETURN s.mapping_generation AS g")

_MATCH = ("MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->"
          "(f:Field)-[r:MAPS_TO]->(c:Concept) "
          "WHERE coalesce(c.retired, false) = false ")
_RETURN = ("RETURN c.id AS concept, c.name AS name, se.name AS table, f.name AS column, "
           "r.role AS role, r.method AS method, r.confirmed AS confirmed "
           "ORDER BY concept, table, column ")


def mappings_cypher() -> str:
    """This system's field-level concept edges, bounded by :data:`ROW_CAP`.
    Only edges that exist are returned (a retired or repointed link is
    deleted, never flagged), and a retired Concept is left out."""
    return f"{_MATCH}{_RETURN}LIMIT {int(ROW_CAP) + 1}"


def targeted_cypher() -> str:
    """The same edges for the given concept ids only."""
    return f"{_MATCH}AND c.id IN $concepts {_RETURN}LIMIT {int(TARGETED_ROW_CAP) + 1}"


_lock = threading.Lock()
_snapshots: dict[str, tuple] = {}     # system id -> onboarding snapshot items
_loaded: dict[str, dict] = {}         # system id -> {gen, items, complete, at, dropped}
_paused_until: dict[str, float] = {}  # system id -> monotonic
_logged: dict[str, tuple] = {}        # system id -> last logged (source, gen, lines)
_client_state: dict = {"client": None}


def _monotonic() -> float:
    return time.monotonic()


# --------------------------------------------------------------------------- #
# Rows -> validated items
# --------------------------------------------------------------------------- #
def _one_line(value, cap: int = 256) -> str:
    """``value`` as one stripped line: control characters removed and every
    run of whitespace (newlines included) collapsed to a space."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", sanitize_untrusted(value)).strip()[:cap]


def _valid_column(table: str, column: str) -> bool:
    try:
        validate_table_name(table)
        validate_column_name(column)
    except ValueError:
        return False
    return True


def _item(concept: str, name: str, table: str, column: str, role: str,
          confirmed: bool) -> dict:
    return {"concept": concept, "name": name, "field": f"{table}.{column}", "role": role,
            "provenance": CONFIRMED if confirmed else SUGGESTED}


def parse_rows(rows) -> tuple:
    """``(items, dropped)``: graph rows as validated items (deduplicated,
    sorted by concept then field) and how many rows were left out because a
    value is not a plain identifier. Non-concept ids and rows without a
    table or column are skipped without counting."""
    seen: set = set()
    items, dropped = [], 0
    for row in list(rows or ()):
        if not isinstance(row, dict):
            continue
        concept = _one_line(row.get("concept"), 160)
        table, column = _one_line(row.get("table")), _one_line(row.get("column"))
        role = _one_line(row.get("role"), 64)
        if concept in _NON_CONCEPTS or not table or not column:
            continue
        if not (_CONCEPT_ID.fullmatch(concept) and _ROLE.fullmatch(role)
                and _valid_column(table, column)):
            dropped += 1
            continue
        if (concept, f"{table}.{column}") in seen:
            continue
        seen.add((concept, f"{table}.{column}"))
        confirmed = (row.get("confirmed") is True
                     or _one_line(row.get("method"), 64) in CONFIRMED_METHODS)
        items.append(_item(concept, _one_line(row.get("name"), 128), table, column,
                           role, confirmed))
    items.sort(key=lambda it: (it["concept"], it["field"]))
    return tuple(items), dropped


def items_from_rows(rows) -> tuple:
    """The validated items of :func:`parse_rows`."""
    return parse_rows(rows)[0]


def snapshot_items(payload_items) -> tuple:
    """The onboarding snapshot (``concept_mappings`` from the agent config)
    as items, under the same rules. It carries no provenance, so every line
    is "suggested"."""
    rows = []
    for it in payload_items or ():
        if not isinstance(it, dict):
            continue
        table, _sep, column = str(it.get("field") or "").partition(".")
        rows.append({"concept": it.get("concept"), "table": table, "column": column,
                     "role": it.get("role")})
    return items_from_rows(rows)


# --------------------------------------------------------------------------- #
# The question's own concepts
# --------------------------------------------------------------------------- #
_CANONICAL_RE = re.compile(r"^\s*canonical_concept\s*:\s*(?P<value>.+?)\s*$",
                           re.IGNORECASE | re.MULTILINE)


def _fold(text: str) -> str:
    return re.sub(r"[\s_-]+", " ", str(text or "").lower()).strip()


def route_concepts(kg_context: str) -> list:
    """Concept ids the ROUTE block's ``canonical_concept`` names, in order."""
    out: list = []
    for match in _CANONICAL_RE.finditer(kg_context if isinstance(kg_context, str) else ""):
        for part in re.split(r"[,/;|]|\band\b", match.group("value")):
            value = _one_line(part, 160)
            if _CONCEPT_ID.fullmatch(value) and value.lower() != "none" and value not in out:
                out.append(value)
    return out


def _mentions(question: str, form: str) -> bool:
    words = [re.escape(w) for w in _fold(form).split()]
    if not words or len("".join(words)) < 3:
        return False
    pattern = r"(?<![a-z0-9])" + r"[\s_-]+".join(words) + r"(?:s|es)?(?![a-z0-9])"
    return re.search(pattern, str(question or "").lower()) is not None


def requested_concepts(items, question: str, kg_context: str) -> list:
    """The concepts this question is about: the route's, then those whose id
    or name the question uses, in that order. Unknown route ids are kept (a
    by-concept read checks them)."""
    out = list(route_concepts(kg_context))
    folded = {_fold(c) for c in out}
    for it in items or ():
        concept = it["concept"]
        if _fold(concept) in folded:
            continue
        if _mentions(question, concept) or (it.get("name") and _mentions(question, it["name"])):
            out.append(concept)
            folded.add(_fold(concept))
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
_HEADERS = {
    "graph": "read from the knowledge graph for this question",
    "graph_cached": ("read from the knowledge graph earlier; the graph could not be "
                     "read for this question"),
    "snapshot": ("from the onboarding snapshot; the knowledge graph could not be read, "
                 "so changes made since onboarding are not shown"),
}
_SUBJECT = "this system's concept-to-column mappings"
_CONFIRMED_RULE = ("A user-confirmed mapping was set by a data steward: when the request "
                   "mentions that concept, use its field directly and do NOT guess another "
                   "column for it.")
_SUGGESTED_RULE = ("A suggested mapping was inferred when the system was onboarded and nobody "
                   "has confirmed it: check it against the schema, and when the business "
                   "context, a guidance note or the request defines the term another way, "
                   "follow that definition instead of the mapping.")


def _line(item: dict) -> str:
    role = f" [{item['role']}]" if item.get("role") else ""
    return f"- {item['concept']} -> {item['field']}{role} ({item['provenance']})"


def _groups(items) -> dict:
    groups: dict = {}
    for it in items or ():
        groups.setdefault(it["concept"], []).append(it)
    return groups


def _withheld_line(concepts, why: str) -> str:
    return (f"{', '.join(concepts)}: its mappings could not {why}, so none are shown; "
            "do not guess a column for it, and say its column could not be confirmed.")


def _notes(withheld: dict, omitted: int, dropped: int) -> list:
    notes = []
    for why in ("all be shown", "be checked"):
        names = [c for c, w in withheld.items() if w == why]
        if names:
            notes.append(_withheld_line(names, why))
    if omitted:
        notes.append(f"[{omitted} other concepts not shown]")
    if dropped:
        notes.append(f"[{dropped} mappings not shown: their names are not plain identifiers]")
    return notes


def _select(items, requested, unverified, budget: int) -> tuple:
    """``(lines, withheld {concept: why}, omitted)`` within ``budget``:
    requested concepts first and whole, then whole other groups."""
    groups = _groups(items)
    lines, withheld, size = [], {}, 0
    for concept in requested:
        if concept in unverified:
            withheld[concept] = "be checked"
            continue
        group = [_line(it) for it in groups.get(concept, ())]
        need = sum(len(ln) + 1 for ln in group)
        if group and size + need > budget:
            withheld[concept] = "all be shown"
        elif group:
            lines += group
            size += need
    omitted = 0
    for concept, group_items in groups.items():
        if concept in requested:
            continue
        group = [_line(it) for it in group_items]
        need = sum(len(ln) + 1 for ln in group)
        if size + need > budget:
            omitted += 1
            continue
        lines += group
        size += need
    return lines, withheld, omitted


def render_section(items, *, source: str = "graph", cap: int = SECTION_CAP,
                   requested=(), unverified=(), dropped: int = 0, origin: str = "") -> str:
    """The KNOWN CONCEPT MAPPINGS section for one question, ``""`` for none.

    The mapping lines sit inside the untrusted-data fence; the withheld and
    omitted notes and the provenance rules (only the kinds present) follow
    it. Concept groups are whole; the section stays within ``cap``."""
    items = tuple(items or ())
    requested = [c for c in requested if c]
    heading = (f"KNOWN CONCEPT MAPPINGS (concept -> field), "
               f"{_HEADERS.get(source, _HEADERS['graph'])}:")
    rules_all = f"{_CONFIRMED_RULE} {_SUGGESTED_RULE}"
    overhead = len(render_fenced("x", origin, heading=heading, subject=_SUBJECT)) + len(rules_all)
    notes_room = 220 * (2 + bool(dropped)) + 40 * len(requested)
    lines, withheld, omitted = _select(items, requested, set(unverified),
                                       max(cap - overhead - notes_room, 0))
    notes = _notes(withheld, omitted, dropped)
    if not lines and not notes:
        return ""
    shown = {line.rsplit("(", 1)[-1].rstrip(")") for line in lines}
    rules = " ".join(rule for kind, rule in ((CONFIRMED, _CONFIRMED_RULE),
                                             (SUGGESTED, _SUGGESTED_RULE)) if kind in shown)
    body = render_fenced("\n".join(lines), origin, heading=heading,
                         subject=_SUBJECT).strip() if lines else heading
    return "\n".join(part for part in (body, *notes, rules) if part)


# --------------------------------------------------------------------------- #
# State and graph reads
# --------------------------------------------------------------------------- #
def remember_snapshot(system_id: str, payload_items) -> None:
    """Keep the onboarding snapshot for ``system_id`` as the read-failure
    fallback (called once, when the system agent is built)."""
    if not system_id:
        return
    with _lock:
        _snapshots[system_id] = snapshot_items(payload_items)


def reset() -> None:
    """Forget every snapshot, loaded read and pause (tests)."""
    with _lock:
        _snapshots.clear()
        _loaded.clear()
        _paused_until.clear()
        _logged.clear()


#: The mapping roles whose columns can hold another system's key: the roles
#: the identity-affinity pass reads as cross-system candidates
#: (``onboard._affinity_existing_fields``). The mapper gives one business key
#: either role, from system to system.
_KEY_ROLES = frozenset({"identifier", "correlate"})


def join_key_fields(system_id: str, column_types=None) -> dict:
    """``{(table, column): concepts}`` (lowercased keys, sorted concept ids) of
    this system's mapped columns that can hold a cross-system key: an
    identifier or correlate mapping whose column join discovery accepts as a
    join key (``join_discovery.is_identifier_like``: the role, the column
    name and its data type, ``column_types`` ``{"table.column": type}``),
    with the concepts those mappings name. Read from the last graph read
    when it is recent enough to be served (:data:`LAST_GOOD_MAX_AGE_SECONDS`),
    else the onboarding snapshot. No new read. Empty when neither holds any."""
    from .join_discovery import is_identifier_like
    with _lock:
        state = _loaded.get(system_id)
        snapshot = _snapshots.get(system_id, ())
    fresh = state is not None and _monotonic() - state["read_at"] <= LAST_GOOD_MAX_AGE_SECONDS
    types = {str(k).lower(): v for k, v in (column_types or {}).items()}
    out: dict = {}
    for item in (state["items"] if fresh else snapshot) or ():
        table, _dot, column = str(item.get("field") or "").lower().partition(".")
        role = item.get("role")
        if (role in _KEY_ROLES and table and column
                and is_identifier_like(column, role, types.get(f"{table}.{column}"))):
            out.setdefault((table, column), set()).add(str(item.get("concept") or ""))
    return {key: tuple(sorted(c for c in concepts if c)) for key, concepts in out.items()}


def _graph_client():
    with _lock:
        client = _client_state["client"]
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client(
                "neptune-graph", region_name=os.environ.get("AWS_REGION") or None,
                config=Config(connect_timeout=_GRAPH_CONNECT_TIMEOUT_S,
                              read_timeout=_GRAPH_READ_TIMEOUT_S,
                              retries={"total_max_attempts": 1, "mode": "standard"}))
            _client_state["client"] = client
        return client


def _graph_read(cypher: str, parameters: dict) -> list:
    """The default read: the local development graph through ``db``, else
    Neptune Analytics with short deadlines. Raises on any failure."""
    if os.environ.get("GRAPH_BACKEND", "").strip().lower() == "local":
        from .db import neptune_query_checked
        return neptune_query_checked(cypher, parameters)
    graph = os.environ.get("GRAPH_IDENTIFIER", "").strip()
    if not graph:
        raise RuntimeError("GRAPH_IDENTIFIER is not set on this runtime")
    response = _graph_client().execute_query(
        graphIdentifier=graph, queryString=cypher, language="OPEN_CYPHER",
        parameters=parameters)
    return json.loads(response["payload"].read().decode()).get("results", [])


def generation_from_rows(rows):
    """The ``g`` generation value of a generation read or bump, else ``None``."""
    row = (rows or [{}])[0] if isinstance(rows, list) else {}
    value = row.get("g") if isinstance(row, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _load(system_id: str, query) -> dict:
    """The current loaded state, re-reading the rows only when the
    generation moved or the reload interval passed. Raises on a read failure."""
    gen = generation_from_rows(query(GENERATION_CYPHER, {"sid": system_id}))
    now = _monotonic()
    with _lock:
        state = _loaded.get(system_id)
    if state and state["gen"] == gen and now - state["read_at"] < RELOAD_AFTER_SECONDS:
        return state
    rows = query(mappings_cypher(), {"sid": system_id})
    rows = list(rows or [])
    complete = len(rows) <= ROW_CAP
    items, dropped = parse_rows(rows[:ROW_CAP])
    state = {"gen": gen, "items": items, "complete": complete, "dropped": dropped,
             "read_at": _monotonic()}
    with _lock:
        _loaded[system_id] = state
    return state


def _targeted(system_id: str, concepts, query) -> tuple:
    """``(items, unverified concepts)`` for route concepts the loaded rows
    may have cut off: one by-concept read; a failure or a read at its cap
    leaves them unverified."""
    try:
        rows = list(query(targeted_cypher(), {"sid": system_id, "concepts": list(concepts)})
                    or [])
    except Exception as exc:  # noqa: BLE001 - the concepts are withheld by name
        logger.warning("LIVE_CONCEPT_MAPPINGS system=%s by-concept read failed (%s)",
                       system_id, type(exc).__name__)
        return (), set(concepts)
    if len(rows) > TARGETED_ROW_CAP:
        return (), set(concepts)
    return items_from_rows(rows), set()


def _log_once(system_id: str, source: str, gen, items) -> None:
    key = (source, gen, len(items))
    with _lock:
        if _logged.get(system_id) == key:
            return
        _logged[system_id] = key
    confirmed = sum(1 for it in items if it["provenance"] == CONFIRMED)
    logger.info("LIVE_CONCEPT_MAPPINGS system=%s source=%s generation=%s lines=%d confirmed=%d",
                system_id, source, gen, len(items), confirmed)


def _fallback(system_id: str, reason: str, question: str, kg_context: str) -> str:
    now = _monotonic()
    with _lock:
        last = _loaded.get(system_id)
        snapshot = _snapshots.get(system_id, ())
    if last is not None and now - last["read_at"] <= LAST_GOOD_MAX_AGE_SECONDS:
        logger.warning("LIVE_CONCEPT_MAPPINGS system=%s graph read unavailable (%s); "
                       "using the read from %.0f s ago", system_id, reason, now - last["read_at"])
        items, source = last["items"], "graph_cached"
    else:
        logger.warning("LIVE_CONCEPT_MAPPINGS system=%s graph read unavailable (%s); "
                       "using the onboarding snapshot (%d lines)", system_id, reason,
                       len(snapshot))
        items, source = snapshot, "snapshot"
    requested = requested_concepts(items, question, kg_context)
    return render_section(items, source=source, requested=requested,
                          origin=f"concept_mappings:{system_id}")


def _from_graph(system_id: str, question: str, kg_context: str, query) -> str:
    state = _load(system_id, query)
    items = state["items"]
    requested = requested_concepts(items, question, kg_context)
    unverified: set = set()
    known = {it["concept"] for it in items}
    missing = [c for c in requested if c not in known]
    if missing and not state["complete"]:
        extra, unverified = _targeted(system_id, missing, query)
        have = {(it["concept"], it["field"]) for it in items}
        items = tuple(sorted(list(items) + [it for it in extra
                                            if (it["concept"], it["field"]) not in have],
                             key=lambda it: (it["concept"], it["field"])))
    _log_once(system_id, "graph", state["gen"], items)
    return render_section(items, source="graph", requested=requested, unverified=unverified,
                          dropped=state["dropped"], origin=f"concept_mappings:{system_id}")


def section_for_question(system_id: str, question: str = "", kg_context: str = "", *,
                         query=None) -> str:
    """The KNOWN CONCEPT MAPPINGS section for this question (``""`` for none).

    One generation read, the rows only when it moved; on failure the
    fallback chain described in the module docstring. Never raises."""
    if not system_id:
        return ""
    try:
        with _lock:
            paused = _monotonic() < _paused_until.get(system_id, 0.0)
        if paused:
            return _fallback(system_id, "paused after a failed read", question, kg_context)
        try:
            return _from_graph(system_id, question, kg_context, query or _graph_read)
        except Exception as exc:  # noqa: BLE001 - any read failure falls back
            with _lock:
                _paused_until[system_id] = _monotonic() + FAILURE_PAUSE_SECONDS
            return _fallback(system_id, type(exc).__name__, question, kg_context)
    except Exception as exc:  # noqa: BLE001 - mappings are additive; never break a question
        logger.warning("LIVE_CONCEPT_MAPPINGS system=%s failed (%s); no section",
                       system_id, type(exc).__name__)
        return ""
