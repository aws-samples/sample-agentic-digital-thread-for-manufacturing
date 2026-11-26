# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Equipment registry reads for a system whose connector lists its records
over an API instead of SQL.

The equipment pass (``onboard._equipment_pass``) reads a registry three ways:
the tag pick's evidence (how many values carry an equipment designation,
whether the table reads as the equipment reference table), the rows of the
telemetry match, and the rows of the cross-registry name match. For a SQL
system those are ``pg`` statements. A REST-family connector whose
``RestVendorAdapter`` declares ``listable_entities`` and a ``list_records``
hook can be a registry too: this module reads it through that hook and
answers in the shapes the SQL reads answer, so the same pick, method,
confidence and evidence rules apply to it. A connector without that hook
takes no part, as before.

Bounds: one listing reads at most :data:`MAX_ROWS` records, :data:`PAGE_SIZE`
per request, within :data:`LISTING_BUDGET_SECONDS` of wall-clock time (the
hook is handed the deadline). A listing the connector reports as cut (more
records exist than it returned), one that holds an item that is not a
record, one that answers after its deadline, and one that fails are noted as
an incomplete read (``db.note_incomplete_read``), as a ``pg`` read that is
cut or unavailable is, so the pass writes and retires nothing for that
registry. Inside :func:`listing_memo` (one pass) each entity of a system is
listed once and every later use reads the same answer.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Optional

from .db import note_incomplete_read

logger = logging.getLogger(__name__)

#: Records per request of one listing.
PAGE_SIZE = 100
#: Records one listing returns at most.
MAX_ROWS = 1000
#: Wall-clock seconds one listing may take, every request and retry of it
#: included. The hook gets the deadline this sets; a listing still running
#: then stops and is not whole. The client checks it between requests,
#: retries and body chunks, so one read that is already waiting (at most
#: the client's own read timeout) can end after it.
LISTING_BUDGET_SECONDS = 45.0
#: The clock deadlines are read on (a module seam).
_monotonic = time.monotonic

#: The listings of the innermost :func:`listing_memo` scope (``None`` outside
#: one): ``{(system id, connection type, entity): (records, problem)}``.
_LISTINGS: "ContextVar[dict | None]" = ContextVar("equipment_api_listings", default=None)


def _adapter(connection_type):
    """The REST-family adapter of ``connection_type`` when it lists records
    (both ``listable_entities`` and ``list_records`` set), else ``None``."""
    from .rest_vendor_registry import resolve_vendor  # lazy: the registry imports onboard lazily
    adapter = resolve_vendor(connection_type or "")
    if adapter is None or adapter.list_records is None or not adapter.listable_entities:
        return None
    return adapter


def readable(connection_type) -> bool:
    """True when a system of ``connection_type`` is read through its
    connector's record listing. Pure apart from the registry lookup."""
    return _adapter(connection_type) is not None


def lists(connection_type, entity) -> bool:
    """True when the connector of ``connection_type`` lists ``entity`` (a
    discover table name). Reads nothing."""
    adapter = _adapter(connection_type)
    return adapter is not None and entity in adapter.listable_entities


@contextmanager
def listing_memo():
    """Within it, each entity of a system is listed at most once; a cut or
    failed listing is noted as an incomplete read every time it is used."""
    token = _LISTINGS.set({})
    try:
        yield
    finally:
        _LISTINGS.reset(token)


def records(sys_id: str, connection_type, entity: str) -> list[dict]:
    """The records of ``entity`` of system ``sys_id``, as its connector lists
    them (bounded, see the module docstring). A listing that is cut or fails
    is noted as an incomplete read; a failed one answers ``[]``."""
    memo = _LISTINGS.get()
    key = (sys_id, connection_type, entity)
    if memo is not None and key in memo:
        rows, problem = memo[key]
    else:
        rows, problem = _list(sys_id, connection_type, entity)
        if memo is not None:
            memo[key] = (rows, problem)
    if problem:
        note_incomplete_read(problem)
    return list(rows)


def _list(sys_id: str, connection_type, entity: str) -> "tuple[list[dict], str]":
    """``(records, problem)`` of one listing; ``problem`` is ``""`` for a
    whole answer, else what kept it from being whole (see
    :func:`_judge_answer`), or the failure's category (the exception class
    name, never its text). The hook is handed the listing's deadline,
    :data:`LISTING_BUDGET_SECONDS` from now on the monotonic clock; an
    answer that comes after it is ``"over_budget"``, whole or not."""
    adapter = _adapter(connection_type)
    if adapter is None or entity not in adapter.listable_entities:
        return [], "not_listable"
    deadline = _monotonic() + LISTING_BUDGET_SECONDS
    try:
        answer = adapter.list_records(sys_id, entity, page_size=PAGE_SIZE, max_rows=MAX_ROWS,
                                      deadline=deadline)
    except Exception as exc:  # noqa: BLE001 - a failed listing costs only this registry
        logger.info("Equipment ValueMapping: listing %s of %s failed category=%s",
                    entity, sys_id, type(exc).__name__)
        return [], type(exc).__name__
    rows, problem = _judge_answer(answer)
    if not problem and _monotonic() > deadline:
        problem = "over_budget"
    if problem:
        logger.info("Equipment ValueMapping: listing %s of %s is not whole (%s, %d records "
                    "kept)", entity, sys_id, problem, len(rows))
    return rows, problem


def _judge_answer(answer) -> "tuple[list[dict], str]":
    """``(records, problem)`` of a hook's answer. ``"malformed_listing"``
    when it holds no record list, or when any item of the list is not a
    record (a mapping of field to value): what that item held is unknown, so
    the listing proves no record absent. ``"partial"`` when the connector
    says more records exist, or it returned more than :data:`MAX_ROWS`. The
    records kept are the record items, at most :data:`MAX_ROWS`. Pure."""
    rows = answer.get("rows") if isinstance(answer, dict) else None
    if not isinstance(rows, list):
        return [], "malformed_listing"
    kept = [r for r in rows if isinstance(r, dict)]
    if len(kept) != len(rows):
        return kept[:MAX_ROWS], "malformed_listing"
    if answer.get("partial") or len(kept) > MAX_ROWS:
        return kept[:MAX_ROWS], "partial"
    return kept, ""


def value(record: dict, field: Optional[str]):
    """A record's value of ``field``; ``None`` for no field, a missing value
    or a blank string (an API answers an absent value either way). Pure."""
    if not field:
        return None
    v = record.get(field)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return v


def distinct_texts(rows: Iterable[dict], field: str, limit: int) -> list[str]:
    """Up to ``limit`` distinct values of ``field`` as text, in listing
    order: what the SQL value sample (``SELECT DISTINCT CAST(field AS TEXT)
    ... LIMIT n``) answers. Pure."""
    out: list[str] = []
    for r in rows:
        v = value(r, field)
        if v is not None and str(v) not in out:
            out.append(str(v))
            if len(out) >= limit:
                break
    return out


def reference_counts(rows: Iterable[dict], tag_field: str, name_field: str,
                     limit: int) -> "tuple[int, int, int, int]":
    """``(rows, distinct tags, named rows, distinct names)`` over the first
    ``limit`` records that carry a tag: the SQL reference check's counts.
    Pure."""
    tagged = [r for r in rows if value(r, tag_field) is not None][:limit]
    tags = [str(value(r, tag_field)) for r in tagged]
    names = [str(n) for n in (value(r, name_field) for r in tagged) if n is not None]
    return len(tags), len(set(tags)), len(names), len(set(names))


def row_tuples(rows: Iterable[dict], cols: list) -> list[tuple]:
    """The records as fixed tuples of ``cols`` (``None`` for an absent
    column), ordered by the first column as text with absent values last:
    what the SQL row read (``SELECT ... ORDER BY 1``) answers. Pure."""
    out = [tuple(value(r, c) for c in cols) for r in rows]
    return sorted(out, key=lambda t: (t[0] is None, "" if t[0] is None else str(t[0])))
