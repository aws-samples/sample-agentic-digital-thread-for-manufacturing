# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""How a metric's Athena query is kept under the per-query scan limit.

The metric layer owns what a metric MEANS; the Athena connector owns how a
query runs. Before this module, every metric on a large Athena table was
narrowed up front, silently: to the table's most recent partitions, else the
last 30 days of its event-time column. That changed the value (a lifetime
COUNT became a recent count) without saying so.

Now:

* a DECOMPOSABLE metric (COUNT, SUM, MIN, MAX over one table) runs as defined,
  through ``athena_tools.run_athena_query_exact``: when the engine refuses it
  for size, the connector computes the exact value from partition pieces.
  Only when that is impossible (the table is not an Iceberg table with a
  partition column, a cap was reached) is the query narrowed, and the result
  says so;
* any other metric (AVG, LATEST, a joined grouping) is narrowed up front as
  before, and the result now says the window was narrowed and to what;
* a duration metric resolved with an explicit turn window (a chat turn's
  absolute ``[start, end)``) is NEVER narrowed: the window is the bound. A
  recent-days bound next to an older window reads no rows, which the notice
  would report as no data. A scan refusal of such a read is
  :func:`refuse_windowed` (not computed in time in a chat turn, carrying the
  window and the table so the system agent can compute it with ONE query
  inside that window, applying the definition; else the refusal itself),
  never a narrowed retry and never an exact split.

The disclosure rides the metric lineage as ``scan``:
``{"mode": "exact_split", "pieces": N, "exact": ..., "atomic": ..., ...}`` or
``{"mode": "narrowed", "where": "<predicate>", "reason": "..."}``. It is
absent when the query ran as defined in one piece (payloads byte-identical).
A split is called exact only when the connector says both ``exact`` (no
floating-point sum) and ``atomic`` (every piece read one snapshot), and no
piece read its own current time (the connector freezes it once:
``clock_frozen_at``); otherwise :func:`scan_text` says what it is and
:func:`inexact` marks it.

What a split may cost depends on who asked (:func:`read_scope`):

* a dashboard or card refresh (the live path: no execution trace) gets its
  own lower caps (:data:`CARD_MAX_PIECES` pieces, :data:`CARD_MAX_GROUPS`
  groups a piece, :data:`CARD_MAX_SECONDS` seconds); past them, or past the
  size the groups may hold, the value is narrowed WITH the disclosure;
* a chat turn's metric notice (``:metric-turn`` trace) gets its timebox as
  the split's deadline; a split that does not finish in time raises
  ``metric_resolver.ExactValueNotInTime``, which the notice reports as a
  distinct status: the system agent, which has the full split budget, can
  compute it;
* a steward's dry-run preview keeps the system's own caps.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import NoReturn

from .metric_layer import Aggregation

DECOMPOSABLE = frozenset({Aggregation.COUNT, Aggregation.SUM, Aggregation.MIN, Aggregation.MAX})
REASON_SHAPE = ("this aggregate cannot be computed exactly from partition pieces, so the "
                "scan is bounded to stay under the per-query scan limit")
REASON_REFUSED = ("the whole query is over the per-query scan limit and could not be "
                  "computed exactly from partition pieces")
MAX_WHERE_CHARS = 160
CARD_MAX_PIECES = 4
CARD_MAX_SECONDS = 30
#: Groups a piece of a card's grouped metric may hold. A card shows at most
#: ``metric_dimension.MAX_GROUPS`` (200) rows; this is ten times that.
CARD_MAX_GROUPS = 2_000
#: The split's deadline ends this long before the notice's timebox does.
TURN_MARGIN_SECONDS = 1.5
REASON_CARD = (f"the exact computation from partition pieces is over a dashboard refresh's limits "
               f"({CARD_MAX_PIECES} pieces, {CARD_MAX_GROUPS} groups a piece, {CARD_MAX_SECONDS} s, "
               "and the size its groups may hold), so the scan is bounded instead")
_CARD_CAP_REASONS = frozenset({"piece_cap", "wall_time", "byte_cap", "group_cap", "group_bytes"})


def exact_eligible(aggregation: Aggregation, *, joined: bool = False) -> bool:
    """True when the connector can compute this metric exactly from pieces."""
    return aggregation in DECOMPOSABLE and not joined


#: The exact-split summary of the last Athena metric read in this context. The
#: resolvers keep their execution seams (``_execute_row``, ``_execute_grouped``)
#: unchanged; the read records here and the caller collects it.
_LAST_SCAN: ContextVar[dict | None] = ContextVar("metric_athena_scan", default=None)


def record(result: dict) -> None:
    """Note how a connector result was computed (called right after the read)."""
    _LAST_SCAN.set(from_result(result))


def collecting(run, *args, **kwargs):
    """``(run(*args, **kwargs), scan recorded during it)``; the context's
    previous value is restored afterwards."""
    token = _LAST_SCAN.set(None)
    try:
        return run(*args, **kwargs), _LAST_SCAN.get()
    finally:
        _LAST_SCAN.reset(token)


def narrowed(where: str, reason: str) -> dict:
    return {"mode": "narrowed", "where": where, "reason": reason}


def from_result(result: dict) -> dict | None:
    """The exact-split summary of a connector result, else ``None``."""
    split = result.get("exact_split") if isinstance(result, dict) else None
    if not isinstance(split, dict) or split.get("applied") is not True:
        return None
    return {"mode": "exact_split", "pieces": split.get("pieces"),
            "partition_column": split.get("partition_column"),
            "snapshot_id": split.get("snapshot_id"), "atomic": split.get("atomic") is True,
            "exact": split.get("exact") is True,
            "clock_frozen_at": split.get("clock_frozen_at"),
            "clock_per_piece": split.get("clock_per_piece") is True}


def inexact(scan: dict | None) -> bool:
    """A split result the connector does not call both exact and atomic, or
    whose pieces each read their own current time."""
    return (isinstance(scan, dict) and scan.get("mode") == "exact_split"
            and not (scan.get("exact") is True and scan.get("atomic") is True
                     and scan.get("clock_per_piece") is not True))


def is_scan_refusal(exc: BaseException) -> bool:
    """The engine refused the query for scan size (directly or as the cause)."""
    from .athena_tools import AthenaEngineScanLimitError
    seen = 0
    while exc is not None and seen < 4:
        if isinstance(exc, AthenaEngineScanLimitError):
            return True
        exc, seen = exc.__cause__, seen + 1
    return False


_TURN: ContextVar[dict | None] = ContextVar("metric_athena_turn", default=None)


@contextmanager
def turn_budget(deadline: float | None, signal: dict | None = None):
    """Inside a chat turn's metric resolve: the absolute ``time.monotonic()``
    deadline its exact splits must meet, and a dict the reads note into
    (``system_id``, ``lineage``, ``split`` once a split started)."""
    token = _TURN.set({"deadline": deadline, "signal": signal})
    try:
        yield
    finally:
        _TURN.reset(token)


def profile() -> str:
    """``card`` (live path), ``turn`` (a chat turn's notice) or ``preview``."""
    from .metric_execution_trace import TURN_SUFFIX, active_trace
    trace = active_trace()
    if trace is None:
        return "card"
    return "turn" if trace.suffix == TURN_SUFFIX else "preview"


@contextmanager
def read_scope(system_id: str, lineage: str):
    """Around ONE metric read on Athena: the split caps of this context."""
    from .athena_exact_split import CapsLimit, limited
    kind, limit, listener = profile(), None, None
    if kind == "card":
        limit = CapsLimit(max_pieces=CARD_MAX_PIECES, max_seconds=CARD_MAX_SECONDS,
                          max_groups=CARD_MAX_GROUPS)
    elif kind == "turn":
        turn = _TURN.get() or {}
        if turn.get("deadline") is not None:
            limit = CapsLimit(deadline=turn["deadline"])
        signal = turn.get("signal")
        if signal is not None:
            signal.update(system_id=system_id, lineage=lineage)
            listener = (lambda: signal.__setitem__("split", True))
    with limited(limit, listener):
        yield kind


def split_reason(exc: BaseException) -> str | None:
    """The exact split's refusal reason carried by a scan refusal, if any."""
    from .athena_tools import AthenaEngineScanLimitError
    seen = 0
    while exc is not None and seen < 4:
        if isinstance(exc, AthenaEngineScanLimitError):
            return (getattr(exc, "exact_split", None) or {}).get("reason")
        exc, seen = exc.__cause__, seen + 1
    return None


def refusal_reason(exc: BaseException, system_id: str, lineage: str) -> str:
    """The narrowing reason for a read the engine refused, or (in a chat turn
    whose split ran out of time) ``ExactValueNotInTime``."""
    kind, reason = profile(), split_reason(exc)
    if kind == "turn" and reason == "wall_time":
        from .metric_resolver import ExactValueNotInTime
        raise ExactValueNotInTime(system_id, lineage) from exc
    if kind == "card" and reason in _CARD_CAP_REASONS:
        return REASON_CARD
    return REASON_REFUSED


def refuse_windowed(exc: BaseException, plan) -> NoReturn:
    """A read bounded by an explicit turn window that the engine refused for
    scan size. It is never narrowed: a recent-days bound next to an older
    window reads no rows, which would be reported as no data. In a chat turn
    it is ``ExactValueNotInTime`` carrying the plan's table and window (the
    system agent can compute it with one query inside that window); anywhere
    else the refusal itself is raised. Always raises."""
    if profile() == "turn":
        from .metric_resolver import ExactValueNotInTime, plan_lineage
        table = getattr(plan, "table", None) or getattr(plan, "measure_table", "")
        raise ExactValueNotInTime(plan.system_id, plan_lineage(plan), table=table,
                                  window=plan.absolute_window) from exc
    raise exc


def exact_runner(source_system: str = ""):
    """The connector runner a metric's Athena query goes through."""
    from . import athena_tools

    def run(sql: str, connection_config: dict) -> dict:
        return athena_tools.run_athena_query_exact(sql, connection_config,
                                                   source_system=source_system)
    return run


def scan_text(scan: dict | None) -> str:
    """One plain sentence for the metric notice, ``""`` when nothing to say."""
    if not isinstance(scan, dict):
        return ""
    if scan.get("mode") == "exact_split":
        pieces, why = scan.get("pieces"), "because the whole query is over the per-query scan limit"
        if not inexact(scan):
            clock = (f" with the current time fixed at {scan['clock_frozen_at']}"
                     if scan.get("clock_frozen_at") else "")
            return f"computed exactly from {pieces} partition pieces{clock}, {why}"
        if scan.get("clock_per_piece") is True:
            return (f"computed from {pieces} partition pieces that each read their own current "
                    f"time, so they match no single query time (NOT EXACT), {why}")
        if scan.get("atomic") is not True:
            return (f"computed from {pieces} partition pieces NOT pinned to one table snapshot, so "
                    f"rows written meanwhile may be missed or counted twice (NOT EXACT), {why}")
        return (f"computed from {pieces} partition pieces as a floating-point sum, which can differ "
                f"from one query's result in the last digits (NOT EXACT), {why}")
    if scan.get("mode") == "narrowed":
        where = " ".join(str(scan.get("where") or "").split())
        if len(where) > MAX_WHERE_CHARS:
            where = where[:MAX_WHERE_CHARS - 3] + "..."
        return f"NARROWED to rows where {where}, not every recorded row: {scan.get('reason')}"
    return ""
