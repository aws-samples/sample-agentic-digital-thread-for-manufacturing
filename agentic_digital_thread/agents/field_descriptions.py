# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The described-field record a system agent's FIELD DESCRIPTIONS section lists.

A connector's discovery can record what a field holds after the field's type
(``schema_format.type_description``). Provision keeps every described field it
maps (``onboard._field_description_items``, over the trusted mappings, through
:func:`from_columns`) and persists each mapped field's type on its graph
``Field`` node, so the graph holds the same record. A start that writes the
agent-config from a fresh connection instead of a provision (the startup
restore, the connection resync of a Save and Reconnect) rebuilds the record
from there with :func:`rebuilt`, so it outlives a restart that recreates the
runtime and a reconnect that rewrites the whole config.

The graph read is optional prompt context, so it never holds a start up.
Each read kind (the graph read, and the current agent-config read a fallback
makes) has one tracked worker (:func:`_single_flight`): a wall-clock budget
(:data:`GRAPH_BUDGET_S`, :data:`FALLBACK_BUDGET_S`), a breaker that skips the
kind for :data:`COOLDOWN_S` after a failed or late read, and no second worker
while one still runs (the caller takes its fallback at once). Each client
deadline ends inside its budget: a graph page gets one attempt with what is
left of the budget, the server asked to stop first
(:func:`_short_deadline_read`); the current agent-config read uses
:func:`fallback_client_config`. One rebuild waits at most the two budgets,
and a timed-out worker ends on its own client deadline (a local development
graph has none; its worker is still the only one). The startup restore reads
every system it restores in one read (:func:`prefetched`).

The read is paged (:func:`_paged_records`): candidate rows (a type that
holds a description separator) come back in the canonical order, a page at a
time after the last row's key, until each listed system's record is full,
the rows run out, or the budget does. A type with a separator and no meaning
(``decimal (12, 4)``) is only a candidate, so a crowd of them never hides a
described field. A read the budget cuts short is not used, although its rows
are a prefix of the canonical order: a record cut there would miss fields
provision kept, and the carried or current record is the one provision
wrote. Rows that come back out of the canonical order are not used either:
then a read stopped before the end could not show it holds the first fields.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterable, Optional

from .schema_format import DESCRIPTION_STARTS, type_description

logger = logging.getLogger(__name__)

#: The agent-config key that carries the record (popped by the system agent).
KEY = "field_descriptions"
#: Bounds of the record (each description, how many fields, their text in
#: all), so it stays a small part of the agent-config secret.
MAX_CHARS = 200
MAX_ITEMS = 150
TOTAL_CHARS = 8000
#: Candidate rows per page of the graph read, and pages one read reads at most.
GRAPH_PAGE_ROWS = 1000
GRAPH_MAX_PAGES = 30
#: Least budget left for which another page is read.
MIN_PAGE_S = 0.2
#: Wall-clock budgets: the graph read, then the current agent-config read a
#: fallback makes. A rebuild waits at most their sum.
GRAPH_BUDGET_S = 4.0
FALLBACK_BUDGET_S = 3.0
#: How long a read kind is skipped after a failed or late read.
COOLDOWN_S = 300.0
#: Longest connect timeout of a graph page (its read timeout is the rest of
#: what is left of the budget), and how much earlier the server is asked to
#: stop than the client.
PAGE_CONNECT_S = 1.0
SERVER_MARGIN_S = 0.25
#: The current agent-config read's client deadlines (their sum stays below
#: FALLBACK_BUDGET_S).
FALLBACK_CONNECT_S = 1.0
FALLBACK_READ_S = 1.5
#: The read's parameters for the separators a description follows
#: (``schema_format.DESCRIPTION_STARTS``): ``$sep0``, ``$sep1``, ...
_SEPARATORS = {f"sep{i}": start for i, start in enumerate(DESCRIPTION_STARTS)}
#: One page of the candidate rows of the listed systems' schemas (a field
#: whose recorded type holds one of the separators, so a bare type is never
#: read), in the canonical (system, table, column) order, after the page key
#: ``$after_sid``, ``$after_tbl``, ``$after_col`` (the last row read).
_GRAPH_PAGE = (
    "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field) "
    "WHERE s.id IN $sids AND f.data_type IS NOT NULL AND ("
    + " OR ".join(f"f.data_type CONTAINS ${name}" for name in _SEPARATORS) + ") "
    "AND (s.id > $after_sid OR (s.id = $after_sid AND (se.name > $after_tbl "
    "OR (se.name = $after_tbl AND f.name > $after_col)))) "
    "RETURN s.id AS sid, se.name AS tbl, f.name AS col, f.data_type AS dtype "
    "ORDER BY sid, tbl, col LIMIT {limit}"
)

#: The read kinds, each with one worker and one breaker.
_GRAPH, _CURRENT = "graph", "current"
_lock = threading.Lock()
_paused_until: dict = {_GRAPH: 0.0, _CURRENT: 0.0}
_workers: dict = {_GRAPH: None, _CURRENT: None}
#: The records the innermost :func:`prefetched` scope read (``None``: none).
_PREFETCHED: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "field_descriptions_prefetched", default=None)
#: The ``time.monotonic`` deadline of the graph read running in this context.
_DEADLINE: "contextvars.ContextVar[float | None]" = contextvars.ContextVar(
    "field_descriptions_deadline", default=None)


class ReadSkipped(RuntimeError):
    """The read was not made: a recent read of its kind failed or ran late."""


class ReadBusy(RuntimeError):
    """The read was not made: its kind's worker is still running."""


class _OutOfOrder(RuntimeError):
    """The rows did not come back in the canonical order."""


class _Incomplete(RuntimeError):
    """The read stopped (its budget or its page limit) before the system's
    record was complete."""


def _monotonic() -> float:
    return time.monotonic()


def reset() -> None:
    """Close both breakers and forget the workers (tests)."""
    with _lock:
        for kind in _paused_until:
            _paused_until[kind] = 0.0
            _workers[kind] = None


def _canonical(columns: Iterable) -> list[tuple]:
    """``(table, column, meaning)`` of each described column, in the ONE
    canonical order: table, then column. Provision and the graph rebuild both
    reach the caps in it, so they keep the same fields whatever order a
    discovery or a read gave them in, and the graph can give that order back
    from what it stores. Stable: of a column given twice, its first described
    occurrence."""
    described = []
    for table, column, data_type in columns:
        table, column = str(table or ""), str(column or "")
        meaning = type_description(data_type)[:MAX_CHARS]
        if column and meaning:
            described.append((table, column, meaning))
    return sorted(described, key=lambda entry: (entry[0], entry[1]))


def from_columns(columns: Iterable) -> list[dict]:
    """``[{"field": "table.column", "description": text}]`` for the
    ``(table, column, data_type)`` triples whose type records what the field
    holds, in the canonical order (:func:`_canonical`), each field once,
    within the bounds above. A field whose type records no meaning (a bare or
    engine type) is left out. Pure."""
    items: list[dict] = []
    seen: set = set()
    total = 0
    for table, column, meaning in _canonical(columns):
        field = f"{table}.{column}" if table else column
        if field in seen:
            continue
        if len(items) >= MAX_ITEMS or total + len(field) + len(meaning) > TOTAL_CHARS:
            break
        seen.add(field)
        total += len(field) + len(meaning)
        items.append({"field": field, "description": meaning})
    return items


def _client_deadlines() -> tuple:
    """``(connect, read)`` timeouts that end at the current read's deadline
    (:data:`_DEADLINE`, else :data:`GRAPH_BUDGET_S` from now), never shorter
    than :data:`MIN_PAGE_S` in all."""
    deadline = _DEADLINE.get() or time.monotonic() + GRAPH_BUDGET_S
    left = max(deadline - time.monotonic(), MIN_PAGE_S)
    connect = min(PAGE_CONNECT_S, left / 3)
    return connect, left - connect


def _short_deadline_read(cypher: str, parameters: dict) -> list:
    """One graph page that ends inside what is left of the read's budget:
    the local development graph through ``db``; else Neptune Analytics, one
    attempt, connect and read timeouts from :func:`_client_deadlines`, and a
    server limit :data:`SERVER_MARGIN_S` shorter, so the server stops the
    query before the client gives up. Raises on any failure."""
    if os.environ.get("GRAPH_BACKEND", "").strip().lower() == "local":
        from .db import neptune_query_checked
        return neptune_query_checked(cypher, parameters)
    graph = os.environ.get("GRAPH_IDENTIFIER", "").strip()
    if not graph:
        raise RuntimeError("GRAPH_IDENTIFIER is not set on this runtime")
    import boto3
    from botocore.config import Config
    connect, read = _client_deadlines()
    client = boto3.client(
        "neptune-graph", region_name=os.environ.get("AWS_REGION") or None,
        config=Config(connect_timeout=connect, read_timeout=read,
                      retries={"total_max_attempts": 1, "mode": "standard"}))
    response = client.execute_query(
        graphIdentifier=graph, queryString=cypher, language="OPEN_CYPHER",
        parameters=parameters,
        queryTimeoutMilliseconds=max(100, int((read - SERVER_MARGIN_S) * 1000)))
    return json.loads(response["payload"].read().decode()).get("results", [])


#: The default graph read (a seam: the unit lane replaces it).
_graph_read = _short_deadline_read


def fallback_client_config():
    """The client config of the current agent-config read a fallback makes:
    one attempt, connect and read timeouts whose sum stays below
    :data:`FALLBACK_BUDGET_S`."""
    from botocore.config import Config
    return Config(connect_timeout=FALLBACK_CONNECT_S, read_timeout=FALLBACK_READ_S,
                  retries={"total_max_attempts": 1, "mode": "standard"})


def _run_worker(kind: str, fn: Callable, box: dict, done: threading.Event, context) -> None:
    """A worker's body: ``fn()`` in the caller's context, its answer or error
    in ``box``; the worker's slot is freed before ``done`` is set."""
    try:
        box["value"] = context.run(fn)
    except BaseException as exc:  # noqa: BLE001 - handed to the caller
        box["error"] = exc
    finally:
        with _lock:
            if _workers[kind] is threading.current_thread():
                _workers[kind] = None
        done.set()


def _single_flight(kind: str, fn: Callable, budget_s: float):
    """``fn()``'s answer within ``budget_s`` seconds, in the ONE worker of
    read ``kind``. :class:`ReadSkipped` while the kind's breaker is open,
    :class:`ReadBusy` while its worker still runs (a read that timed out is
    never sent again until it ends). A failed or late read opens the breaker
    (:func:`_open_breaker`) and raises (``TimeoutError`` when late)."""
    box: dict = {}
    done = threading.Event()
    with _lock:
        if _monotonic() < _paused_until[kind]:
            raise ReadSkipped(f"a recent {kind} read failed")
        worker = _workers[kind]
        if worker is not None and worker.is_alive():
            raise ReadBusy(f"the {kind} read still runs")
        worker = threading.Thread(target=_run_worker, name=f"field-descriptions-{kind}",
                                  args=(kind, fn, box, done, contextvars.copy_context()),
                                  daemon=True)
        _workers[kind] = worker
        worker.start()
    if not done.wait(budget_s):
        _open_breaker(kind, "TimeoutError")
        raise TimeoutError(f"no answer within {budget_s:g} s")
    if "error" in box:
        _open_breaker(kind, type(box["error"]).__name__)
        raise box["error"]
    return box.get("value")


def _open_breaker(kind: str, reason: str) -> None:
    """Skip reads of ``kind`` for :data:`COOLDOWN_S` (logged with ``reason``)."""
    with _lock:
        _paused_until[kind] = _monotonic() + COOLDOWN_S
    logger.warning("field descriptions: %s read failed (%s); it is skipped for %g s",
                   kind, reason, COOLDOWN_S)


def _row_key(row: dict) -> tuple:
    return (str(row.get("sid") or ""), str(row.get("tbl") or ""), str(row.get("col") or ""))


def _is_full(columns: list) -> bool:
    """Whether the record of ``columns`` (read in the canonical order)
    already stops at a cap, so no later row can change it."""
    described = {f"{t}.{c}" if t else c for t, c, _m in _canonical(columns)}
    return len(described) > len(from_columns(columns))


def _page(read: Callable, system_ids: list, after: tuple) -> list:
    """One page of candidate rows after the key ``after``; raises
    :class:`_OutOfOrder` unless every row's key is greater than the one
    before it (the canonical order, as Python compares it)."""
    params = {"sids": list(system_ids), **_SEPARATORS,
              "after_sid": after[0], "after_tbl": after[1], "after_col": after[2]}
    rows = [r for r in read(_GRAPH_PAGE.format(limit=int(GRAPH_PAGE_ROWS)), params) or ()
            if isinstance(r, dict)]
    keys = [_row_key(r) for r in rows]
    if any(later <= earlier for earlier, later in zip([after, *keys], keys)):
        raise _OutOfOrder("rows out of the canonical order")
    return rows


def _paged_records(system_ids: list, read: Callable, deadline: float) -> tuple:
    """``(records, complete)``: the record of every listed system whose rows
    were read to the end or to a full record, from candidate pages read in
    the canonical order until each record is full, the rows run out, the
    page limit, or ``deadline`` (a ``time.monotonic`` value, also the one
    each page's client deadline ends at); ``complete`` is whether that covers
    every listed system. Rows arrive in the canonical order, so a full
    record and a passed system are final."""
    columns: dict = {sid: [] for sid in system_ids}
    token = _DEADLINE.set(deadline)
    try:
        open_ids = _read_pages(read, list(system_ids), columns, deadline)
    finally:
        _DEADLINE.reset(token)
    done = [sid for sid in system_ids if sid not in open_ids]
    return {sid: from_columns(columns[sid]) for sid in done}, not open_ids


def _read_pages(read: Callable, open_ids: list, columns: dict, deadline: float) -> list:
    """Read candidate pages into ``columns`` (system id -> rows); the systems
    still open when paging stopped (none when every record is final)."""
    after = ("", "", "")
    for _page_no in range(GRAPH_MAX_PAGES):
        if time.monotonic() > deadline - MIN_PAGE_S:
            break
        rows = _page(read, open_ids, after)
        for r in rows:
            if r.get("sid") in columns:
                columns[r["sid"]].append((str(r.get("tbl") or ""), str(r.get("col") or ""),
                                          r.get("dtype")))
        if len(rows) < GRAPH_PAGE_ROWS:
            return []
        after = _row_key(rows[-1])
        open_ids = [sid for sid in open_ids if sid >= after[0] and not _is_full(columns[sid])]
        if not open_ids:
            return []
    return open_ids


def _read_records(system_ids: list, query: Optional[Callable] = None) -> dict:
    """``{system id: record}`` for every listed system the bounded, paged
    read (:func:`_paged_records`) completed. Raises when the graph is not
    read; a read cut short opens the breaker, and the systems it did not
    complete are left out."""
    read = query or _graph_read
    records, complete = _single_flight(
        _GRAPH, lambda: _paged_records(system_ids, read, time.monotonic() + GRAPH_BUDGET_S),
        GRAPH_BUDGET_S)
    if not complete:
        _open_breaker(_GRAPH, "budget or page limit")
    return records


def read_from_graph(system_id: str, query: Optional[Callable] = None) -> list[dict]:
    """The record rebuilt from the system's persisted ``Field`` types
    (:func:`from_columns`), read under the budget and the breaker. Raises
    when the graph is not read, or not to the end of the record, which is
    never read as "no described field"."""
    records = _read_records([system_id], query)
    if system_id not in records:
        raise _Incomplete(system_id)
    return records[system_id]


@contextmanager
def prefetched(system_ids: Iterable, query: Optional[Callable] = None):
    """Read the records of every system in ``system_ids`` in ONE bounded,
    paged read for the code inside (the startup restore), so :func:`rebuilt`
    reads none of them again. A read that fails or runs late opens the
    breaker; the systems it did not complete fall back on their own. Yields
    the records read, or ``None``."""
    ids = sorted({str(sid) for sid in system_ids if sid})
    records = None
    if ids:
        try:
            records = _read_records(ids, query)
        except Exception as exc:  # noqa: BLE001 - optional context; each rebuild falls back
            logger.info("field descriptions: no one-read prefetch (%s)", type(exc).__name__)
    token = _PREFETCHED.set(records)
    try:
        yield records
    finally:
        _PREFETCHED.reset(token)


def usable(items) -> list[dict]:
    """The well-formed records of a stored record value (each a mapping with
    a non-empty ``field`` and ``description`` string), at most MAX_ITEMS."""
    if not isinstance(items, list):
        return []
    out = [{"field": it["field"], "description": it["description"]} for it in items
           if isinstance(it, dict) and isinstance(it.get("field"), str) and it["field"]
           and isinstance(it.get("description"), str) and it["description"].strip()]
    return out[:MAX_ITEMS]


def _current_record(current: Optional[Callable[[], Optional[dict]]]) -> list[dict]:
    """The usable record of ``current()`` (a system's current agent-config),
    read in the current-config worker within :data:`FALLBACK_BUDGET_S`
    (:func:`_single_flight`); ``[]`` when there is none, it cannot be read in
    time, or the read is skipped (logged by type only)."""
    if current is None:
        return []
    try:
        config = _single_flight(_CURRENT, current, FALLBACK_BUDGET_S)
    except Exception as exc:  # noqa: BLE001 - the record is prompt context, never required
        logger.warning("field descriptions: current agent-config not read (%s)", type(exc).__name__)
        return []
    return usable((config or {}).get(KEY)) if isinstance(config, dict) else []


def rebuilt(system_id: str, cfg: Optional[dict], *,
            current: Optional[Callable[[], Optional[dict]]] = None,
            query: Optional[Callable] = None) -> Optional[dict]:
    """A copy of ``cfg`` whose :data:`KEY` is the record rebuilt from the
    graph (the :func:`prefetched` one, else :func:`read_from_graph`): none
    when the graph holds no described field, even over a stale one ``cfg``
    carried. When the graph is not read (a failure, a late answer, the open
    breaker), the record ``cfg`` already carries, else the one of
    ``current()`` (the system's current agent-config, read only then and
    within its budget), else none. ``cfg`` itself is never changed; ``None``
    with no record stays ``None``."""
    base = {k: v for k, v in (cfg or {}).items() if k != KEY}
    known = _PREFETCHED.get()
    try:
        items = known[system_id] if known and system_id in known else read_from_graph(
            system_id, query=query)
    except Exception as exc:  # noqa: BLE001 - the record is prompt context, never required
        logger.warning("field descriptions of %s not read from the graph (%s); the current "
                       "record is kept", system_id, type(exc).__name__)
        items = usable((cfg or {}).get(KEY)) or _current_record(current)
    if items:
        return {**base, KEY: items}
    return base if cfg is not None else None
