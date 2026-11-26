# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Exact COUNT / SUM / MIN / MAX past a per-query scan cap, inside the connector.

An Athena workgroup can enforce a per-query bytes-scanned cutoff the app
cannot raise. When the engine refuses a query for scan size
(:class:`athena_tools.AthenaEngineScanLimitError`) and the query has a shape
whose result can be added up from parts (``athena_exact_shape``), this module
computes the exact answer by tiling:

1. it reads the table's catalog entry (Iceberg format, column types, field
   ids) and pins the latest snapshot (``"<table>$snapshots"``): every piece
   reads that one snapshot, so a table committing while the pieces run can
   neither drop rows from the total nor count them twice. No readable,
   pinnable snapshot id, no split;
2. it reads ``"<table>$files"`` into groups of files (``athena_exact_plan``)
   and plans boxes (``athena_exact_boxes``) over the table's partition source
   column and the query's own bounded temporal columns: each box is one
   half-open range per column, and the boxes tile every column's whole
   domain, NULLs included, so every row is in exactly one piece (the
   ``$files`` rows are kept per table snapshot and SQL in a small cache, so
   a refusal repeated on the same snapshot reads them once);
3. each piece is the model's SQL, byte-identical apart from ONE injected
   predicate (the box, through ``sql_where_inject``), ``FOR VERSION AS OF``
   the pinned snapshot, and the current time (``current_timestamp`` and the
   like) read at ONE instant taken when the split starts
   (``athena_exact_shape.frozen_sql``); a grouped query's pieces drop its own
   LIMIT and read every group, up to ``exact_split_max_groups`` each (a
   piece's top N do not add up to the whole query's top N; per-group counts
   over disjoint rows do), every page of every piece charged to one budget
   (:class:`_RowBudget`: the result's cell and column caps, and
   :data:`GROUP_BYTES_BUDGET` bytes in all) that stops the fetch before the
   merge; pieces run with bounded concurrency, and a piece
   the engine still refuses is split again within the caps;
4. the rows are merged server-side (``athena_exact_merge``), the query's
   ORDER BY and LIMIT applied once, and returned as ONE result that reads
   like one query, with its lineage; a grouped result that does not show
   every group says how many there are and how many the bounded result
   shows, and when the result's size limits cut rows or columns.

How big a piece may be is learnt, not fixed. The estimate (column sizes from
the file statistics) can be off either way, and the costs are not alike: a
piece planned too big costs one refused query (at most the per-query limit,
a few seconds) and a split; a plan too fine costs ``piece_cap``, no answer.
So the first plan is optimistic: the estimate is taken at its word unless
the refused query itself proves it low (it read at least the limit), and when
a plan at :data:`HEADROOM` of the limit needs more pieces than a third of the
piece cap (enough for every piece to be refused and split once), it is
planned closer to the limit (:data:`PLAN_FRACTIONS`). The biggest piece
then runs alone first, as a probe. Every finished piece recalibrates the
estimate (the highest measured ratio of scanned bytes to estimate, and the
lower bound each refused piece gives), and the queued pieces are re-planned
at the new calibration before they run: one still too big is split, and two
that together make a box within the target are merged back into it (the
leaves still tile every row exactly once). A first plan over the piece cap or
the byte cap is not refused before that evidence exists: it is planned at up
to :data:`PROBE_PLAN_FACTOR` times the cap, its biggest piece runs as the
probe, and the split stops with ``piece_cap`` or ``byte_cap`` only when the
re-planned queue still does not fit in what is left. A piece whose query does not finish in its
poll window (a stalled poll is a real transient on the engine) is run once
more as a fresh query when the wall budget leaves :data:`RETRY_MIN_SECONDS`;
a second timeout gives no total.

The total byte cap (``exact_split_max_total_bytes``) is PREDICTIVE: a running
scan cannot be stopped at an exact byte, so the split keeps what may still be
scanned within it before it starts anything. Every byte the engine reports is
spent (the refused query, the metadata reads, every piece, a refused piece,
and the timed-out attempt of a retried piece or of the plain query itself);
before a piece starts, every running piece's calibrated estimate is reserved
beside it, and no more pieces run at once than the remaining budget could pay
for if each read the whole per-query limit (:func:`_may_start`), so
concurrency falls as the budget tightens. A piece whose real bytes exceed
its calibrated estimate can still take the total past the cap by that much.

The split shares its caller's time. It keeps one absolute deadline for its
whole run, taken before it builds a client: its own wall cap
(``exact_split_max_seconds``) from its start, never past a caller's deadline
less the reserve the caller keeps back for its own work after the split
(:class:`CapsLimit`). Its own thread waits for no SDK call past that
deadline: every catalog, workgroup and metadata call (and any client it
builds) runs in a worker it stops waiting for there (:func:`_within`), and
after it the split waits at most :data:`DEADLINE_OVERRUN_SECONDS` for its
stop calls and pieces (:func:`_stop_running`). It reuses the Athena client
that ran the refused query; a client it builds itself has short timeouts and
one retry, so one call on it ends within :data:`CALL_SECONDS_BOUND`. A
metadata query is submitted with a ClientRequestToken, so one whose outcome
is unknown is found and stopped (:func:`_submit`). Every call a split starts
(a catalog, workgroup or metadata call, a client build, a piece, a stop call)
takes one of :data:`MAX_SPLIT_CALLS` process-wide permits before it starts and
gives it back when it ends, not when the split stops waiting for it
(:class:`_Permits`); a split waits for a permit up to its own deadline and
refuses (``wall_time``) only when none frees by then. Calls it stopped waiting
for are tracked until they end, and a new split never uses a client one of
them still holds (:func:`_refuse_while_busy`).

A caller that keeps a reserve (the system agent) has more work after the
split, so the split's deadline is :data:`DEADLINE_OVERRUN_SECONDS` earlier
still, which leaves it the whole reserve, and the split also hands time back
to it early. It declines
(``wall_time``) at its start, before reading anything, when what is left is
shorter than one piece (:data:`PIECE_SECONDS_FLOOR`), and again after
planning, before any piece runs, when the plan cannot fit what is left
(:func:`_check_time`): its pieces in rounds at the concurrency the byte
budget allows, each taking the shortest query time this split has measured,
and two pieces for each OR branch not yet planned. A plan over a cap is not
probed with less than :data:`PROBE_MIN_SECONDS` left. Each decline says not
enough time was left (:data:`NO_TIME_DETAIL`), so the caller can write a
narrower query while it still has time to. A caller that keeps no reserve (a
dashboard card refresh, a chat turn's metric notice) has nothing else to
spend the time on: its split tries to its deadline, as it did before.

No total is returned when anything is uncertain: a piece failed, timed out,
had its rows cut, held more groups than the group cap, the grouped pieces'
rows went over their budget (``group_bytes``), a cap (pieces, bytes, wall time) was
reached, the snapshot list, file list or catalog was unreadable, the latest
snapshot id cannot be pinned, the statistics give no
split that fits, or the table is not an Iceberg table. :class:`ExactSplitRefused`
names why; the connector then keeps the honest scan-limit error and adds that
reason. A caller may tighten the caps for its own context (:func:`limited`).
"""
from __future__ import annotations

import contextvars
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from . import athena_exact_boxes as boxes
from . import athena_exact_merge as merge
from . import athena_exact_plan as plan
from . import athena_exact_shape as shape_mod

logger = logging.getLogger(__name__)

#: Per-system ``connection_config`` keys (admin knobs; no steward work).
CONFIG_MAX_PIECES = "exact_split_max_pieces"
CONFIG_MAX_TOTAL_BYTES = "exact_split_max_total_bytes"
CONFIG_MAX_SECONDS = "exact_split_max_seconds"
CONFIG_MAX_GROUPS = "exact_split_max_groups"
DEFAULT_MAX_PIECES = 16
DEFAULT_MAX_TOTAL_BYTES = 12 * 1024 ** 3
DEFAULT_MAX_SECONDS = 150.0
#: Groups one piece of a grouped query may hold. Each piece reads all of its
#: groups (the merge applies the query's LIMIT once); more than this and the
#: split gives no result (``group_cap``). The bound a metadata read has.
DEFAULT_MAX_GROUPS = 20_000
#: Hard ceilings on the per-system piece and group knobs: a larger value is
#: clamped to these and logged. Measured on a 60-day element-shape breakdown
#: (an analytics lake, 2026-10-16): 3 pieces, at most 9,928 groups a piece.
MAX_PIECES_CEILING = 64
MAX_GROUPS_CEILING = 50_000
#: Bytes, as compact JSON (the measure of the model-facing result bound),
#: that the rows of all of one split's grouped pieces may hold together
#: before the merge; past it the fetch stops and the split refuses
#: (``group_bytes``). The measured breakdown above held 21,081 piece rows of
#: three short columns, under 1 MiB; this is about eight times that.
GROUP_BYTES_BUDGET = 8 * 1024 ** 2
CONCURRENCY = 4
#: A piece is planned to read at most this share of the per-query cap...
HEADROOM = 0.6
#: ...after its estimate is multiplied by the calibration, which starts at
#: this prior (the estimate taken at its word) unless evidence raises it.
CALIBRATION = 1.0
#: Shares of the cap tried in order until a plan needs no more pieces than
#: a third of the piece cap.
PLAN_FRACTIONS = (HEADROOM, 0.75, 0.9)
#: A finished piece whose estimate is under this share of the target says
#: too little about the estimate's error to recalibrate from.
MIN_MEASURED_SHARE = 0.25
#: A first plan over the piece cap is still probed when it needs at most
#: this many times the cap (the estimate may be that much too high).
PROBE_PLAN_FACTOR = 4
#: A timed-out piece is retried only when at least this much wall time is left.
RETRY_MIN_SECONDS = 5.0
#: The time one piece is taken to need when the split has measured none of
#: its own queries yet: under the fastest finished piece measured (a
#: three-branch split on an analytics lake, 2026-10-23: its pieces took
#: 2.3-6.6 s and its metadata reads 2.9-4.0 s).
PIECE_SECONDS_FLOOR = 2.0
#: A plan over a cap is probed only with at least this long left. Its probe
#: is its biggest piece, planned to read up to 60-90% of the per-query limit
#: (:data:`PLAN_FRACTIONS`), and a probe that shows the estimate high still
#: leaves at least one more piece. So: two reads near the limit, at the
#: slowest measured (an analytics lake, 2026-10-23: a probe piece took 13.2 s
#: and refused queries that read the limit 9.0-12.6 s).
PROBE_MIN_SECONDS = 2 * 13.2
#: The fewest pieces an OR branch not yet planned can need (a plan of fewer
#: is refused, ``unsplittable``).
MIN_BRANCH_PIECES = 2
#: Why a split declined for time (``wall_time``): the agent still has time
#: for a narrower query.
NO_TIME_DETAIL = "not enough time is left for the pieces, so run a narrower query instead"
#: Why a split stopped for time at its start and while planning or running
#: pieces, as it said before the reserve (a caller that keeps none).
_NO_TIME_START_DETAIL = "no time is left for the pieces"
_PIECES_LATE_DETAIL = "the pieces did not finish within the time limit"
#: Why a split was refused while a call an earlier split stopped waiting for
#: is still running on a client it would use (:func:`_busy`).
_BUSY_DETAIL = "calls an earlier exact split stopped waiting for are still running"
#: Why a split was refused when no permit came free before its deadline
#: (:data:`MAX_SPLIT_CALLS`, :func:`_no_slot`).
_NO_SLOT_DETAIL = ("every permit for exact-split queries in this process is in use and none "
                   "came free in time")
#: Every client the split builds itself (Glue, and Athena when the refused
#: query left none to reuse) connects within this long, waits this long for
#: each read of its answer, and makes this many attempts (one retry, botocore
#: standard mode). Measured on a rehearsal account (2026-10-23): a branch's
#: Glue table read and workgroup read took 0.4-0.5 s together, and a whole
#: metadata read (submission, polls, result pages) 2.8-4.0 s.
CLIENT_CONNECT_TIMEOUT_SECONDS = 1.0
CLIENT_READ_TIMEOUT_SECONDS = 3.0
CLIENT_MAX_ATTEMPTS = 2
#: botocore standard mode waits up to 1 s before a second attempt, and up to
#: 5 s more when the service asks for it (``x-amz-retry-after``).
CLIENT_RETRY_WAIT_SECONDS = 1.0 + 5.0
#: The longest one call on such a client can take. The read timeout counts the
#: wait for each read of the socket, so an answer that keeps arriving in parts
#: can take longer; the Athena and Glue answers the split reads are one small
#: JSON body each.
CALL_SECONDS_BOUND = (CLIENT_MAX_ATTEMPTS * (CLIENT_CONNECT_TIMEOUT_SECONDS
                                             + CLIENT_READ_TIMEOUT_SECONDS)
                      + (CLIENT_MAX_ATTEMPTS - 1) * CLIENT_RETRY_WAIT_SECONDS)
#: The most the split waits past its own deadline, for its stop calls and the
#: piece queries still finishing; it waits for no other call past it. Measured
#: on a rehearsal account (2026-10-23): a split stopped at its deadline made
#: its stop calls 0.0-0.1 s after it, its last API call ended 0.2 s after it,
#: and the tool call returned 0.3 s after it. A caller that keeps a reserve has
#: the split's deadline set this much earlier as well, so it keeps the whole
#: reserve.
DEADLINE_OVERRUN_SECONDS = 1.0
#: Permits for the calls exact splits run at once in this process, running or
#: left running (:class:`_Permits`). A split holds at most :data:`CONCURRENCY`
#: pieces and one stop call at once. The app resolves at most 4 metrics on its
#: resolver lane (``offload.RESOLVER_MAX_CONCURRENCY``) and 2 for metric
#: notices (``metric_turn_notice.METRIC_TURN_MAX_WORKERS``) at once, and in the
#: measured dry runs (2026-10-23) a system agent's Athena calls ran one after
#: another. So six splits at full concurrency never wait for a permit; only
#: calls left running by stalled splits take them away.
MAX_SPLIT_CALLS = 6 * (CONCURRENCY + 1)
#: A piece above the partition column's NULL end that reads more than this
#: many times its estimate (at the current calibration) shows the NULL-end
#: placement guess wrong.
GUESS_SLACK = 1.5
#: Rows a metadata read may return (the planning read is one row per group).
MAX_META_ROWS = 20_000
#: Layout reads (the ``$files`` partition fields and the planning rows) kept
#: in this process per (table, pinned snapshot, SQL), most recent first, so
#: a refusal the split already decided on that snapshot costs no layout
#: read (measured: five identical refusals in fifteen minutes on one table,
#: about 11 s of metadata reads each). A read of more rows is not kept.
LAYOUT_CACHE_SIZE = 8
LAYOUT_CACHE_MAX_ROWS = 5_000
_LAYOUT_CACHE: "OrderedDict[tuple, list[list]]" = OrderedDict()
_LAYOUT_LOCK = threading.Lock()


class ExactSplitRefused(Exception):
    """The split gave no total; ``reason`` is a fixed code, ``detail`` plain words."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "applied": False, "reason": self.reason,
            "detail": (f"The exact total could not be computed from partition pieces: "
                       f"{self.detail}. Give no total; do not add numbers yourself."),
        }


def _positive_int(value, key: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a positive integer, got {value!r}")
    try:
        number = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{key} must be a positive integer, got {value!r}") from exc
    if number <= 0:
        raise ValueError(f"{key} must be a positive integer, got {value!r}")
    return number


def coerce_config(cfg: dict) -> dict:
    """The set exact-split knobs of ``cfg``, coerced fail-closed (a typo
    raises rather than silently removing a cap). Unset keys are omitted."""
    from .athena_tools import AthenaConfigError
    out = {}
    try:
        for key in (CONFIG_MAX_PIECES, CONFIG_MAX_TOTAL_BYTES, CONFIG_MAX_SECONDS, CONFIG_MAX_GROUPS):
            number = _positive_int((cfg or {}).get(key), key)
            if number is not None:
                out[key] = number
    except ValueError as exc:
        raise AthenaConfigError(str(exc)) from exc
    return out


@dataclass(frozen=True)
class Caps:
    max_pieces: int
    max_total_bytes: int
    max_seconds: float
    max_groups: int = DEFAULT_MAX_GROUPS


@dataclass(frozen=True)
class CapsLimit:
    """A caller's tighter caps for its own context: any of pieces, groups a
    piece, seconds, or an absolute ``time.monotonic()`` deadline, and the
    seconds the caller keeps back from that deadline for its own work after
    the split (``reserve_seconds``; it applies to a ``deadline`` only)."""
    max_pieces: int | None = None
    max_seconds: float | None = None
    deadline: float | None = None
    max_groups: int | None = None
    reserve_seconds: float | None = None


_LIMIT: ContextVar[CapsLimit | None] = ContextVar("athena_exact_split_limit", default=None)
_LISTENER: ContextVar = ContextVar("athena_exact_split_listener", default=None)


@contextmanager
def limited(limit: CapsLimit | None = None, on_start=None):
    """Tighten the caps of every split started in this context, and call
    ``on_start()`` when one starts (both restored afterwards)."""
    token_limit, token_listener = _LIMIT.set(limit), _LISTENER.set(on_start)
    try:
        yield
    finally:
        _LIMIT.reset(token_limit)
        _LISTENER.reset(token_listener)


def _ceiling(knobs: dict, key: str, default: int, ceiling: int) -> int:
    """The knob's value, clamped to its hard ceiling (logged when it is)."""
    value = knobs.get(key, default)
    if value > ceiling:
        logger.warning("%s=%s is over its ceiling; using %s", key, value, ceiling)
        return ceiling
    return value


def resolve_caps(cfg: dict) -> Caps:
    knobs = coerce_config(cfg)
    caps = Caps(_ceiling(knobs, CONFIG_MAX_PIECES, DEFAULT_MAX_PIECES, MAX_PIECES_CEILING),
                knobs.get(CONFIG_MAX_TOTAL_BYTES, DEFAULT_MAX_TOTAL_BYTES),
                float(knobs.get(CONFIG_MAX_SECONDS, DEFAULT_MAX_SECONDS)),
                _ceiling(knobs, CONFIG_MAX_GROUPS, DEFAULT_MAX_GROUPS, MAX_GROUPS_CEILING))
    limit = _LIMIT.get()
    if limit is None:
        return caps
    seconds = caps.max_seconds
    if limit.max_seconds is not None:
        seconds = min(seconds, float(limit.max_seconds))
    end, _reserve = _caller_end()
    if end is not None:
        seconds = min(seconds, max(0.0, end - time.monotonic()))
    pieces = min(caps.max_pieces, limit.max_pieces) if limit.max_pieces else caps.max_pieces
    groups = min(caps.max_groups, limit.max_groups) if limit.max_groups else caps.max_groups
    return Caps(pieces, caps.max_total_bytes, seconds, groups)


def _caller_end() -> tuple[float | None, float]:
    """The caller's absolute ``time.monotonic()`` deadline less what it keeps
    back, and its reserve: ``(None, 0.0)`` when it set no deadline (a reserve
    applies to a deadline only). A caller that keeps a reserve also keeps back
    :data:`DEADLINE_OVERRUN_SECONDS`, the most the split runs past its own
    deadline, so the whole reserve is left to it."""
    limit = _LIMIT.get()
    if limit is None or limit.deadline is None:
        return None, 0.0
    reserve = max(0.0, float(limit.reserve_seconds or 0.0))
    kept = reserve + DEADLINE_OVERRUN_SECONDS if reserve else 0.0
    return limit.deadline - kept, reserve


def _deadline_and_caps(cfg: dict) -> tuple[float, Caps, float]:
    """A split's absolute deadline, its caps and its caller's reserve, taken
    once before anything else (a client, a catalog read) can use the time:
    its own wall cap from now, never past the caller's deadline less the
    reserve."""
    began = time.monotonic()
    caps = resolve_caps(cfg)
    end, reserve = _caller_end()
    deadline = began + caps.max_seconds
    return (deadline if end is None else min(deadline, end)), caps, reserve


class _RowBudget:
    """What the rows of one split's grouped pieces may hold, shared by every
    page of every piece (pieces run in threads): no cell over the result's
    cell cap, no more columns than its column cap, and at most ``limit``
    bytes in all, each row counted as its compact JSON. Past any of them
    :meth:`take` refuses (``group_bytes``), so the fetch stops before the
    merge holds more than the model-facing bound could ever use."""

    def __init__(self, limit: int):
        self.limit, self.used = limit, 0
        self._lock = threading.Lock()

    def width(self, columns: int) -> None:
        from .response_integrity_query import MAX_QUERY_COLUMNS
        if columns > MAX_QUERY_COLUMNS:
            raise ExactSplitRefused("group_bytes", f"a piece returned more than {MAX_QUERY_COLUMNS} columns")

    def take(self, row: list) -> None:
        from .response_integrity_query import MAX_QUERY_CELL_BYTES, _encoded_size
        self.width(len(row))
        size = len(row) + 1                    # the brackets and commas
        for cell in row:
            cell_bytes = _encoded_size(cell)
            if cell_bytes > MAX_QUERY_CELL_BYTES:
                raise ExactSplitRefused("group_bytes", "a piece returned a value larger than "
                                        f"{MAX_QUERY_CELL_BYTES} bytes")
            size += cell_bytes
        with self._lock:
            self.used += size
            if self.used > self.limit:
                raise ExactSplitRefused("group_bytes", "the groups the pieces hold are over "
                                        f"{self.limit} bytes in all")


@dataclass
class _Run:
    """The I/O context and running totals of one split."""
    cfg: dict
    athena: object
    caps: Caps
    deadline: float
    spent: int = 0
    calibration: float = CALIBRATION
    cap: int = 0
    fraction: float = HEADROOM
    bounds: list = field(default_factory=list)
    measured: list = field(default_factory=list)
    pieces_run: int = 0
    refused: int = 0
    retried: int = 0
    timed_out_bytes: int = 0
    clock_at: datetime | None = None
    over_cap_plan: bool = False
    low_end_dropped: bool = False
    layout: object = None
    meta_reads: list = field(default_factory=list)
    budget: _RowBudget = field(default_factory=lambda: _RowBudget(GROUP_BYTES_BUDGET))
    #: Wall seconds of each of this split's own queries that finished
    #: (metadata reads and pieces), for :func:`_piece_seconds`.
    query_seconds: list = field(default_factory=list)
    #: Seconds the caller keeps back from its deadline; when it keeps any,
    #: the split hands time back early (:func:`_check_time`).
    reserve_seconds: float = 0.0

    @property
    def target(self) -> float:
        """The planned estimate of one piece at the current calibration."""
        return self.cap * self.fraction / self.calibration

    def recalibrate(self) -> None:
        """The highest measured ratio once pieces finished, else the prior,
        never below a proven lower bound."""
        base = max(self.measured) if self.measured else CALIBRATION
        self.calibration = max([base, *self.bounds])


# --------------------------------------------------------------------------- #
# Catalog, limits, snapshot, file list
# --------------------------------------------------------------------------- #
def _names(shape: shape_mod.Shape, cfg: dict) -> tuple[str | None, str, str]:
    parts = [p.lower() for p in shape.table_parts]
    if len(parts) == 1:
        return None, cfg["database"].lower(), parts[0]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    return parts[0], parts[1], parts[2]


def _meta_relation(catalog: str | None, database: str, table: str, suffix: str) -> str:
    from .athena_tools import _quoted_identifier
    head = f"{_quoted_identifier(catalog)}." if catalog else ""
    return f"{head}{_quoted_identifier(database)}.{_quoted_identifier(table + suffix)}"


def _out_of_time(run: _Run) -> ExactSplitRefused:
    """The refusal of a split whose deadline came before its pieces: for a
    caller that keeps a reserve, :data:`NO_TIME_DETAIL` (it can still run a
    narrower query); otherwise the words it had before the reserve."""
    return ExactSplitRefused("wall_time", NO_TIME_DETAIL if run.reserve_seconds
                             else _PIECES_LATE_DETAIL)


def _time_left(run: _Run) -> float:
    """Seconds left before the split's deadline, checked before a planning
    read starts: none left is :func:`_out_of_time`."""
    left = run.deadline - time.monotonic()
    if left <= 0:
        raise _out_of_time(run)
    return left


def _within(run: _Run, call, *args, _clients=(), _gave_up=None, **kwargs):
    """``call(*args, **kwargs)`` (one SDK call, or a client build and a call),
    waited on only until the split's deadline: it runs in a daemon thread that
    holds a permit (:func:`_take`) from before it starts until it ends, and
    when it has not answered by then the split stops (:func:`_out_of_time`),
    sets ``_gave_up`` and tracks the thread as left running on ``_clients``
    (:func:`_leave`) until it ends; its late answer is dropped. The call's own
    error is raised as it is."""
    _time_left(run)
    permits = _take(run, wait=True)
    left = run.deadline - time.monotonic()
    if left <= 0:
        permits.give()
        raise _out_of_time(run)
    outcome: dict = {}
    answered = threading.Event()
    context = contextvars.copy_context()

    def work():
        try:
            outcome["value"] = context.run(call, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller below
            outcome["error"] = exc
        finally:
            permits.give()
            answered.set()
    worker = threading.Thread(target=work, name="exact-split-metadata-call", daemon=True)
    try:
        worker.start()
    except BaseException:
        permits.give()
        raise
    if not answered.wait(left):
        if _gave_up is not None:
            _gave_up.set()
        _leave(_clients, worker.is_alive)
        raise _out_of_time(run)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


# --------------------------------------------------------------------------- #
# Permits
# --------------------------------------------------------------------------- #
class _Permits:
    """A count of the calls exact splits run at once, never above ``total``:
    a permit is taken before a call starts (waiting up to a timeout for one)
    and given back when it ends. ``peak`` is the most ever in use."""

    def __init__(self, total: int):
        self.total, self.used, self.peak = total, 0, 0
        self._free = threading.Condition()

    def take(self, timeout: float) -> bool:
        """One permit, waiting at most ``timeout`` seconds for one; ``False``
        when none came free in that time."""
        ends = time.monotonic() + max(0.0, timeout)
        with self._free:
            while self.used >= self.total:
                left = ends - time.monotonic()
                if left <= 0:
                    return False
                self._free.wait(left)
            self.used += 1
            self.peak = max(self.peak, self.used)
            return True

    def give(self) -> None:
        with self._free:
            if self.used <= 0:
                raise RuntimeError("an exact-split permit was given back twice")
            self.used -= 1
            self._free.notify()


_PERMITS = _Permits(MAX_SPLIT_CALLS)


def _no_slot(reserve: float) -> ExactSplitRefused:
    """The refusal of a split that found no permit free before its deadline."""
    detail = f"{_NO_SLOT_DETAIL}, so run a narrower query instead" if reserve else _NO_SLOT_DETAIL
    return ExactSplitRefused("wall_time", detail)


def _take(run: _Run, *, wait: bool):
    """A permit from the process's pool (returned, so it is given back to the
    pool it came from), or ``None``. With ``wait`` it waits up to the
    split's deadline and refuses (:func:`_no_slot`) when none came free.
    Only the split's own thread takes permits, and it waits only while it
    holds none (see :func:`_execute`): a piece or a worker never takes one, so
    no split waits on a permit it holds itself."""
    permits = _PERMITS
    timeout = max(0.0, run.deadline - time.monotonic()) if wait else 0.0
    if permits.take(timeout):
        return permits
    if wait:
        raise _no_slot(run.reserve_seconds)
    return None


# --------------------------------------------------------------------------- #
# Calls the split stopped waiting for
# --------------------------------------------------------------------------- #
@dataclass(eq=False)
class _Left:
    """A call a split stopped waiting for, the clients it uses, and whether it
    is still running."""
    clients: tuple
    alive: object


_LEFT: list = []
_LEFT_LOCK = threading.Lock()


def _leave(clients, alive) -> None:
    """Track a call left running on ``clients`` while ``alive()``."""
    with _LEFT_LOCK:
        _LEFT.append(_Left(tuple(c for c in clients if c is not None), alive))


def _still_running() -> list:
    """The calls a split stopped waiting for that are still running (the
    ended ones are forgotten)."""
    with _LEFT_LOCK:
        _LEFT[:] = [left for left in _LEFT if left.alive()]
        return list(_LEFT)


def clear_abandoned() -> None:
    """Forget every tracked call and start a fresh permit pool (for tests; a
    running call still ends and gives its permit back to its own pool)."""
    global _PERMITS
    with _LEFT_LOCK:
        _LEFT.clear()
    _PERMITS = _Permits(MAX_SPLIT_CALLS)


def _busy(reserve: float) -> ExactSplitRefused:
    """The refusal of a split started while calls an earlier one left are
    still running."""
    detail = f"{_BUSY_DETAIL}, so run a narrower query instead" if reserve else _BUSY_DETAIL
    return ExactSplitRefused("wall_time", detail)


def _refuse_while_busy(reserve: float, *clients) -> None:
    """Refuse a new split (:func:`_busy`) before it reads anything when a call
    a split stopped waiting for is still running on a client this split would
    use. How many calls run in the process is bounded by the permits
    (:func:`_take`), not here."""
    mine = [c for c in clients if c is not None]
    if any(held is c for left in _still_running() for held in left.clients for c in mine):
        raise _busy(reserve)


# --------------------------------------------------------------------------- #
# The clients the split builds
# --------------------------------------------------------------------------- #
def _client_config():
    """The botocore config of every client the split builds
    (:data:`CALL_SECONDS_BOUND`)."""
    from botocore.config import Config
    return Config(connect_timeout=CLIENT_CONNECT_TIMEOUT_SECONDS,
                  read_timeout=CLIENT_READ_TIMEOUT_SECONDS,
                  retries={"total_max_attempts": CLIENT_MAX_ATTEMPTS, "mode": "standard"})


def _split_client(service: str, cfg: dict):
    """A ``service`` client for ``cfg`` with :func:`_client_config`. For a
    cross-account connection it comes from the session the refused query
    already assumed (``aws_session.session_for`` keeps it), so no role is
    assumed again."""
    from .athena_tools import boto3_client_for
    return boto3_client_for(service, cfg, default_region=cfg["region"], config=_client_config())


def _glue_catalog_id(run: _Run, catalog: str | None) -> str | None:
    name = catalog or run.cfg.get("catalog") or "AwsDataCatalog"
    if name.lower() == "awsdatacatalog":
        return None
    if run.athena is None:                       # the OR column type read, given no client
        run.athena = _within(run, _split_client, "athena", run.cfg)
    found = _within(run, run.athena.get_data_catalog, Name=name,
                    _clients=(run.athena,))["DataCatalog"]
    if str(found.get("Type", "")).upper() != "GLUE":
        raise ExactSplitRefused("not_iceberg", "the table is not in a Glue catalog")
    return (found.get("Parameters") or {}).get("catalog-id")


def _read_table_meta(glue_client, cfg: dict, catalog_id, database: str, table: str):
    """The Glue table read, on ``glue_client`` or on a client built here
    (:func:`_split_client`), so a slow build is waited on no longer than the
    read."""
    glue = glue_client or _split_client("glue", cfg)
    return plan.read_table_meta(glue, catalog_id, database, table)


def _table_meta(run: _Run, shape, glue_client) -> plan.TableMeta:
    catalog, database, table = _names(shape, run.cfg)
    try:
        catalog_id = _glue_catalog_id(run, catalog)
        meta = _within(run, _read_table_meta, glue_client, run.cfg, catalog_id, database, table,
                       _clients=(glue_client,))
    except ExactSplitRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable catalog never yields a total
        raise ExactSplitRefused("catalog_unreadable", "the table's catalog entry could not be read") from exc
    if not meta.iceberg:
        raise ExactSplitRefused("not_iceberg", "the table is not an Iceberg table")
    return meta


def _piece_cap(run: _Run, refusal) -> int:
    """The per-query byte limit: the smallest of the app budget, the
    workgroup cutoff, and what the refused query had scanned when cut."""
    from .athena_tools import resolve_scan_budget
    known = [b for b in (resolve_scan_budget(run.cfg), getattr(refusal, "scanned_bytes", None))
             if isinstance(b, int) and not isinstance(b, bool) and b > 0]
    try:
        group = _within(run, run.athena.get_work_group, WorkGroup=run.cfg["workgroup"],
                        _clients=(run.athena,))
        cutoff = group["WorkGroup"]["Configuration"].get("BytesScannedCutoffPerQuery")
        if isinstance(cutoff, int) and cutoff > 0:
            known.append(cutoff)
    except ExactSplitRefused:
        raise
    except Exception:  # noqa: BLE001  # nosec B110 - the workgroup read is best-effort
        pass
    if not known:
        raise ExactSplitRefused("limit_unknown", "the per-query scan limit is not known")
    return min(known)


def _result_rows(athena, query_id: str, max_rows: int) -> list[list]:
    """Every row of a finished internal metadata read (header dropped),
    paged with no byte bound; more than ``max_rows`` raises."""
    rows: list[list] = []
    token, header = None, None
    while True:
        kwargs = {"QueryExecutionId": query_id, "MaxResults": 1000}
        if token:
            kwargs["NextToken"] = token
        page = athena.get_query_results(**kwargs)
        result_set = page.get("ResultSet", {})
        if header is None:
            header = [c.get("Name", "") for c in result_set.get("ResultSetMetadata", {}).get("ColumnInfo", [])]
        for row in result_set.get("Rows", []):
            cells = [item.get("VarCharValue") for item in row.get("Data", [])]
            if header is not None and not rows and cells == header:
                header = []
                continue
            rows.append(cells)
        if len(rows) > max_rows:
            raise ValueError("metadata read too large")
        token = page.get("NextToken")
        if not token:
            return rows


class _MetaReadLate(Exception):
    """A metadata read cut at the split's deadline: its poll stopped it there,
    or its submission answered after the split stopped waiting for it."""


def _outcome_unknown(exc: Exception) -> bool:
    """Whether a submission that raised ``exc`` may still have started its
    query: anything but a request botocore refused to send or an answer from
    the service below a 5xx."""
    from botocore.exceptions import ClientError, ParamValidationError
    if isinstance(exc, ParamValidationError):
        return False
    if isinstance(exc, ClientError):
        status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return not isinstance(status, int) or status >= 500
    return True


def _stop_quietly(athena, query_id: str) -> None:
    try:
        athena.stop_query_execution(QueryExecutionId=query_id)
    except Exception:  # noqa: BLE001  # nosec B110 - stopping is best-effort
        pass


def _stop_by_token(athena, cfg: dict, capped: str, token: str) -> None:
    """Find a metadata query whose submission's answer was lost and stop it:
    submitted again with the same ClientRequestToken, Athena returns that
    query and starts no other (or, when the first never arrived, starts it
    now and it is stopped at once). One attempt; a failure is logged."""
    from . import athena_tools as at
    try:
        query_id = at._start_athena_query(athena, cfg, capped, client_request_token=token)
    except Exception as exc:  # noqa: BLE001 - logged; the read already gives no total
        logger.warning("exact split: a metadata query's submission could not be confirmed (%s)",
                       type(exc).__name__)
        return
    _stop_quietly(athena, query_id)


def _submit(athena, cfg: dict, capped: str, token: str, gave_up) -> str:
    """Submit one metadata read with ``token`` as its ClientRequestToken. One
    whose outcome is unknown (:func:`_outcome_unknown`) is found by its token
    and stopped before the error is raised (:func:`_stop_by_token`), and one
    that answers after the split stopped waiting for it (``gave_up``) is
    stopped, so no metadata scan is left running untracked."""
    from . import athena_tools as at
    try:
        query_id = at._start_athena_query(athena, cfg, capped, client_request_token=token)
    except Exception as exc:
        if _outcome_unknown(exc):
            _stop_by_token(athena, cfg, capped, token)
        raise
    if gave_up.is_set():
        _stop_quietly(athena, query_id)
        raise _MetaReadLate()
    return query_id


def _meta_read(athena, cfg: dict, capped: str, deadline: float, token: str, gave_up):
    """One metadata read, in a worker (:func:`_within`): submitted
    (:func:`_submit`), polled no longer than the time left before
    ``deadline`` (a read not finished by then is stopped there by the poll),
    then its rows. ``(query_id, state, statistics, rows, seconds)``; ``rows``
    is ``None`` unless it succeeded."""
    from . import athena_tools as at
    started = time.monotonic()
    query_id = _submit(athena, cfg, capped, token, gave_up)
    configured = at.resolve_poll_timeout(cfg)
    timeout = min(configured, deadline - time.monotonic())
    try:
        state, _reason, statistics = at._poll_athena_query(
            athena, query_id, timeout, at.resolve_scan_budget(cfg))
    except at.AthenaQueryTimeoutError as exc:
        if timeout < configured:
            raise _MetaReadLate() from exc
        raise
    rows = _result_rows(athena, query_id, MAX_META_ROWS) if state == "SUCCEEDED" else None
    return query_id, state, statistics, rows, time.monotonic() - started


def _meta_query(run: _Run, sql: str, what: str) -> list[list]:
    """One internal metadata read (``$files``/``$snapshots``), counted in the
    bytes spent. Its rows never reach a model, so no byte bound applies. Its
    submission, polls and result pages run in a worker the split waits on no
    longer than its deadline (:func:`_meta_read`)."""
    from .sql_safety import apply_row_cap, ensure_select_only
    try:
        ensure_select_only(sql)
        capped, _ = apply_row_cap(sql, MAX_META_ROWS + 1)
        gave_up = threading.Event()
        query_id, state, statistics, rows, seconds = _within(
            run, _meta_read, run.athena, run.cfg, capped, run.deadline, uuid.uuid4().hex,
            gave_up, _clients=(run.athena,), _gave_up=gave_up)
        run.meta_reads.append(query_id)
        _count_bytes(run, {"statistics": statistics})
        if state != "SUCCEEDED":
            raise RuntimeError(f"metadata read {state.lower()}")
        run.query_seconds.append(seconds)
        return rows
    except ExactSplitRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable metadata relation never yields a total
        if time.monotonic() >= run.deadline or isinstance(exc, _MetaReadLate):
            raise _out_of_time(run) from exc
        raise ExactSplitRefused("files_unreadable", f"the table's {what} could not be read") from exc


def _count_bytes(run: _Run, telemetry: dict) -> int:
    scanned = (telemetry.get("statistics") or {}).get("DataScannedInBytes")
    scanned = int(scanned) if isinstance(scanned, (int, float)) and not isinstance(scanned, bool) else 0
    run.spent += scanned
    return scanned


def _layout_cache_get(key: tuple) -> list[list] | None:
    """A copy of the kept rows of ``key``, else ``None``."""
    with _LAYOUT_LOCK:
        rows = _LAYOUT_CACHE.get(key)
        if rows is None:
            return None
        _LAYOUT_CACHE.move_to_end(key)
        return [list(row) for row in rows]


def _layout_cache_put(key: tuple, rows: list[list]) -> None:
    """Keep a copy of ``rows`` under ``key`` (not when over
    :data:`LAYOUT_CACHE_MAX_ROWS`), dropping the least recent past
    :data:`LAYOUT_CACHE_SIZE`."""
    if len(rows) > LAYOUT_CACHE_MAX_ROWS:
        return
    with _LAYOUT_LOCK:
        _LAYOUT_CACHE[key] = [list(row) for row in rows]
        _LAYOUT_CACHE.move_to_end(key)
        while len(_LAYOUT_CACHE) > LAYOUT_CACHE_SIZE:
            _LAYOUT_CACHE.popitem(last=False)


def clear_layout_cache() -> None:
    with _LAYOUT_LOCK:
        _LAYOUT_CACHE.clear()


def _layout_rows(run: _Run, sql: str, snapshot: int) -> list[list]:
    """The rows of one ``$files`` layout read on the pinned ``snapshot``:
    kept from an earlier read of the same SQL on the same snapshot (the SQL
    names the table; the catalog, region and role name the account), else
    read now (:func:`_meta_query`) and kept. A failed read is not kept."""
    key = (run.cfg.get("catalog"), run.cfg.get("region"), run.cfg.get("role_arn"), snapshot, sql)
    rows = _layout_cache_get(key)
    if rows is None:
        rows = _meta_query(run, sql, "file list")
        _layout_cache_put(key, rows)
    return rows


def _latest_snapshot(run: _Run, relation: str) -> int:
    """The table's latest snapshot id, which every piece is pinned to. An
    unreadable list (``snapshot_unreadable``) or an id that is not an integer
    (``snapshot_unpinnable``) gives no split: unpinned pieces would read a
    table that may commit while they run."""
    sql = f"SELECT snapshot_id FROM {relation} ORDER BY committed_at DESC LIMIT 1"  # nosec B608 - relation quoted fail-closed
    try:
        rows = _meta_query(run, sql, "snapshot list")
    except ExactSplitRefused as exc:
        if exc.reason == "wall_time":
            raise
        raise ExactSplitRefused("snapshot_unreadable", "the table's snapshot list could not be read, "
                                "so the pieces cannot all read one snapshot") from exc
    text = str(rows[0][0]).strip() if rows and rows[0] and rows[0][0] is not None else ""
    if not text.lstrip("-").isdigit():
        raise ExactSplitRefused("snapshot_unpinnable", "the table's latest snapshot id could not be "
                                "read as a snapshot to pin the pieces to")
    return int(text)


def _layout(run: _Run, shape, meta, snapshot: int) -> plan.Layout:
    catalog, database, table = _names(shape, run.cfg)
    files = _meta_relation(catalog, database, table, "$files")
    coverage = {str(r[0]): plan._int(r[1]) for r in _layout_rows(run, plan.coverage_sql(files), snapshot)
                if r and r[0] is not None}
    options = plan.candidates(coverage, meta)
    cand = options[0] if options else None
    dims = plan.split_dims(cand, shape, meta)
    if not dims:
        raise ExactSplitRefused("no_split_column", "the table has no partition field on a date or "
                                "timestamp column and the query bounds no such column")
    sql, newest = plan.planning_sql(files, cand, dims, meta, shape)
    try:
        return plan.parse_plan(_layout_rows(run, sql, snapshot), cand, dims, newest)
    except ValueError as exc:
        raise ExactSplitRefused("files_unreadable", "the table's file list could not be read") from exc


def _check_expanded(shape, layout: plan.Layout) -> None:
    """The box predicate names its columns unqualified: a list expansion
    giving a column the same name as one of them would make it ambiguous."""
    clash = sorted(d.column for d in layout.dims if d.column.lower() in shape.expanded)
    if clash:
        raise ExactSplitRefused("not_decomposable", "the list expansion names a column the pieces are "
                                f"split on ({', '.join(clash)})")


def _fit(run: _Run, layout: plan.Layout) -> list:
    """The plan at the first share of the cap that needs at most a third of
    the piece cap, else the finest plan within the cap at the largest share.
    Sets ``run.fraction``."""
    budget = max(2, run.caps.max_pieces // 3)
    found = None
    for fraction in PLAN_FRACTIONS:
        run.fraction = fraction
        try:
            leaves = boxes.plan_boxes(layout.dims, layout.groups, run.target, run.caps.max_pieces)
        except boxes.TooManyPieces:
            continue
        found = (fraction, leaves)
        if len(leaves) <= budget:
            break
    if found is None:
        return _over_cap_plan(run, layout)
    run.fraction = found[0]
    return found[1]


def _over_cap_plan(run: _Run, layout: plan.Layout) -> list:
    """A plan over the piece cap, kept for a probe (the estimate may be high):
    at the largest share of the cap, up to :data:`PROBE_PLAN_FACTOR` times
    the piece cap."""
    run.fraction = PLAN_FRACTIONS[-1]
    try:
        leaves = boxes.plan_boxes(layout.dims, layout.groups, run.target,
                                  run.caps.max_pieces * PROBE_PLAN_FACTOR)
    except boxes.TooManyPieces as exc:
        raise ExactSplitRefused("piece_cap", f"it needs more than {run.caps.max_pieces} pieces") from exc
    run.over_cap_plan = True
    return leaves


def _plan(run: _Run, layout: plan.Layout, cap: int, refusal) -> list:
    """The first boxes, planned optimistically (see the module doc).

    The refused query read at least the cap, so ``seen / total`` is a lower
    bound on the ratio of real bytes to estimate. Refused before anything
    runs when the statistics give no split, a box must read more than the cap
    whatever the split, or the plan is over a cap."""
    total = layout.total_est
    if not total:
        raise ExactSplitRefused("unsplittable", "the file statistics give no estimate to split by")
    seen = getattr(refusal, "scanned_bytes", None) or cap
    run.cap = cap
    run.bounds.append(seen / total)
    run.recalibrate()
    leaves = _fit(run, layout)
    if len(leaves) < 2 or any(boxes.irreducible(layout.groups, b, layout.dims) > cap for b in leaves):
        raise ExactSplitRefused("unsplittable", "files every piece would have to read are over the "
                                "per-query scan limit, so no split fits")
    planned = sum(boxes.estimate(layout.groups, b, layout.dims) for b in leaves) * run.calibration
    if run.spent + planned > run.caps.max_total_bytes * PROBE_PLAN_FACTOR:
        raise ExactSplitRefused("byte_cap", "it would scan more than the total byte cap")
    if run.spent + planned > run.caps.max_total_bytes:
        run.over_cap_plan = True                # probed first, checked again after it
    return leaves


def _slots(run: _Run) -> int:
    """How many pieces may run at once: no more than the remaining byte
    budget could pay for if each read the whole per-query limit, at least
    one, at most :data:`CONCURRENCY` (:func:`_may_start`)."""
    slots = max(1, int((run.caps.max_total_bytes - run.spent) // max(run.cap, 1)))
    return min(CONCURRENCY, slots)


def _waves(pieces: int, slots: int) -> int:
    """The rounds ``pieces`` run in, ``slots`` at a time. More than two run
    the biggest alone first, as the probe (:func:`_execute`)."""
    slots = max(1, slots)
    if pieces > 2:
        return 1 + math.ceil((pieces - 1) / slots)
    return math.ceil(pieces / slots)


def _piece_seconds(run: _Run) -> float:
    """The time one piece is taken to need: the shortest of this split's own
    queries that finished, else :data:`PIECE_SECONDS_FLOOR`. The shortest, so
    a plan is declined only when it would not fit even if every piece ran as
    fast as that."""
    return min(run.query_seconds) if run.query_seconds else PIECE_SECONDS_FLOOR


def _check_time(run: _Run, leaves: list, later_branches: int) -> None:
    """Decline a plan that cannot fit the time left, before any of its pieces
    runs (``wall_time``, :data:`NO_TIME_DETAIL`): its pieces in rounds at
    the slot rule's concurrency, plus :data:`MIN_BRANCH_PIECES` for each of
    ``later_branches`` (OR branches not yet planned; their layout reads may
    be kept from an earlier split, so they are not counted), each taking
    :func:`_piece_seconds`. A plan over a cap is not probed with less than
    :data:`PROBE_MIN_SECONDS` left. Only for a caller that keeps a reserve:
    one that keeps none has nothing else to spend the time on, so its split
    tries to its deadline."""
    if not run.reserve_seconds:
        return
    left = run.deadline - time.monotonic()
    slots = _slots(run)
    waves = _waves(len(leaves), slots) + later_branches * _waves(MIN_BRANCH_PIECES, slots)
    if waves * _piece_seconds(run) > left:
        raise ExactSplitRefused("wall_time", NO_TIME_DETAIL)
    if run.over_cap_plan and left < PROBE_MIN_SECONDS:
        raise ExactSplitRefused("wall_time", NO_TIME_DETAIL)


def _piece(layout: plan.Layout, box) -> "_Piece":
    return _Piece(box, boxes.estimate(layout.groups, box, layout.dims))


def _replan(run: _Run, layout: plan.Layout, box) -> list:
    """``box`` cut to the current target, or ``[box]`` when no cut helps."""
    if boxes.floor(layout.groups, box, layout.dims) >= run.target:
        return [box]
    try:
        return boxes.plan_boxes(layout.dims, layout.groups, run.target, run.caps.max_pieces, start=box)
    except boxes.TooManyPieces as exc:
        raise ExactSplitRefused("piece_cap", f"it needs more than {run.caps.max_pieces} pieces") from exc


def _queue_at(run: _Run, layout: plan.Layout, queue: list) -> list:
    """The queue re-planned at the current target: split, then merged."""
    out = []
    for piece in queue:
        if piece.est <= run.target:
            out.append(piece)
            continue
        out.extend(_piece(layout, b) for b in _replan(run, layout, piece.box))
    return sorted(_merge_queue(run, layout, out), key=lambda p: -p.est)


def _merge_queue(run: _Run, layout: plan.Layout, queue: list) -> list:
    """Queued pieces merged pairwise, smallest union first, while two of them
    make one box within the current target."""
    queue = list(queue)
    while True:
        best = None
        for i, a in enumerate(queue):
            for b in queue[i + 1:]:
                box = boxes.union(a.box, b.box)
                est = boxes.estimate(layout.groups, box, layout.dims) if box else None
                if est is not None and est <= run.target and (best is None or est < best[0]):
                    best = (est, a, b, box)
        if best is None:
            return queue
        _est, a, b, box = best
        queue = [p for p in queue if p is not a and p is not b] + [_piece(layout, box)]


def _replan_queue(run: _Run, layout: plan.Layout, queue: list) -> list:
    """Queued pieces re-planned at the current calibration before they run:
    one too big is split, and two that fit together are merged. When that
    leaves less than half the pieces still allowed for refusals, the queue is
    planned closer to the limit (the next of :data:`PLAN_FRACTIONS`)."""
    room = run.caps.max_pieces - run.pieces_run
    best = None
    for fraction in [f for f in PLAN_FRACTIONS if f >= run.fraction]:
        run.fraction = fraction
        try:
            out = _queue_at(run, layout, queue)
        except ExactSplitRefused:
            continue
        if best is None or len(out) < len(best[1]):
            best = (fraction, out)
        if len(out) <= max(1, room // 2):
            break
    if best is None:
        raise ExactSplitRefused("piece_cap", f"it needs more than {run.caps.max_pieces} pieces")
    run.fraction = best[0]
    return best[1]


def _refused_children(run: _Run, layout: plan.Layout, piece) -> list:
    """The pieces replacing one the engine refused: it read at least the cap,
    which bounds the estimate's error from below; re-planned at that, else
    split in two."""
    if piece.est:
        run.bounds.append(run.cap / piece.est)
        run.recalibrate()
    try:
        children = _replan(run, layout, piece.box)
    except ExactSplitRefused:
        children = [piece.box]                 # the queue re-plan finds a coarser fit
    if len(children) < 2:
        halves = boxes.split_box(layout.groups, piece.box, layout.dims)
        if halves is None:
            raise ExactSplitRefused("piece_refused", "a piece was still over the scan limit and cannot be split")
        children = list(halves)
    return [_piece(layout, b) for b in children]


def _measured(run: _Run, piece) -> None:
    """Recalibrate from a finished piece (one big enough to say something)."""
    if piece.est and piece.est >= MIN_MEASURED_SHARE * run.target:
        run.measured.append(piece.scanned / piece.est)
        run.recalibrate()


# --------------------------------------------------------------------------- #
# Running pieces
# --------------------------------------------------------------------------- #
@dataclass
class _Piece:
    box: tuple
    est: int
    rows: merge.PieceRows | None = None
    query_id: str | None = None
    scanned: int = 0
    telemetry: dict = field(default_factory=dict)
    retried: bool = False
    started: float = 0.0
    #: Set once its query's thread has ended and given its permit back.
    ended: threading.Event = field(default_factory=threading.Event)


def _run_piece(run: _Run, sql: str, telemetry: dict, grouped: bool = False) -> tuple[dict, dict]:
    """One piece's query. A grouped piece reads every group it holds, up to
    one past the group cap (:func:`_accept` refuses that one)."""
    from . import athena_tools
    remaining = run.deadline - time.monotonic()
    if remaining <= 1:
        raise ExactSplitRefused("wall_time", _PIECES_LATE_DETAIL)
    whole = {"max_rows": run.caps.max_groups + 1, "_row_budget": run.budget} if grouped else {}
    try:
        result = athena_tools.run_athena_query(
            sql, run.cfg, client=run.athena, _enrich=False,
            poll_timeout=min(athena_tools.resolve_poll_timeout(run.cfg), remaining),
            _telemetry=telemetry, **whole)
    except Exception as exc:
        exc.adt_telemetry = telemetry
        raise
    return result, telemetry


def _accept(shape, piece: _Piece, result: dict, telemetry: dict, max_groups: int) -> None:
    rows = result.get("rows") or []
    if (shape.grouped or shape.has_percentile) and (
            result.get("truncated") or len(rows) > max_groups):
        raise ExactSplitRefused("group_cap", f"a piece holds more than {max_groups} groups")
    if result.get("truncated"):
        raise ExactSplitRefused("piece_at_limit",
                                "a piece returned as many rows as its LIMIT, so groups may be missing")
    piece.rows = merge.PieceRows(list(result.get("columns") or []),
                                 list(telemetry.get("column_types") or []), [list(r) for r in rows])
    piece.query_id = telemetry.get("query_id")


def _sql_for(shape, layout: plan.Layout, box, snapshot: int, clock_at: datetime | None = None) -> str:
    from .athena_tools import _quoted_identifier
    if isinstance(snapshot, bool) or not isinstance(snapshot, int):
        raise ExactSplitRefused("snapshot_unpinnable", "a piece could not be pinned to the snapshot")
    if shape.clock and clock_at is None:
        raise ExactSplitRefused("clock_unfrozen", "the current time was not fixed for the pieces")
    predicate = boxes.box_predicate(layout.dims, box, _quoted_identifier)
    # A grouped piece reads every group: the merge applies the LIMIT once. A
    # percentile piece returns a serialized qdigest per group (digest=True),
    # merged by a later query.
    whole = shape_mod.unlimited(shape) if shape.grouped else shape
    text = (shape_mod.piece_sql(whole, predicate, snapshot, clock_at, digest=shape.has_percentile)
            if predicate else None)
    if not text:
        raise ExactSplitRefused("not_decomposable", "the piece condition could not be placed in the query")
    return text


def _reserved(run: _Run, running: dict) -> float:
    """The calibrated estimate of every piece still running."""
    return sum(p.est for p in running.values()) * run.calibration


def _may_start(run: _Run, running: dict, piece: _Piece) -> bool:
    """Whether ``piece`` may start beside the ``running`` ones (the byte cap is
    predictive; see the module doc). It must fit the total byte cap with every
    running piece's calibrated estimate reserved; one that does not waits for
    them, and is refused (``byte_cap``) only when nothing runs. And no more
    run at once than the remaining budget could pay for if each read the
    whole per-query limit, at least one."""
    planned = run.spent + _reserved(run, running) + piece.est * run.calibration
    if planned > run.caps.max_total_bytes:
        if not running:
            raise ExactSplitRefused("byte_cap", "it would scan more than the total byte cap")
        return False
    return len(running) < _slots(run)


def _admit(run: _Run, piece: _Piece) -> None:
    if run.pieces_run >= run.caps.max_pieces:
        raise ExactSplitRefused("piece_cap", f"it needs more than {run.caps.max_pieces} pieces")
    if time.monotonic() >= run.deadline:
        raise ExactSplitRefused("wall_time", _PIECES_LATE_DETAIL)
    run.pieces_run += 1


def _guess_shown_wrong(run: _Run, piece, scanned: int | None) -> bool:
    """Drop the NULL-end placement guess (``athena_exact_plan.LOW_END``) when a
    piece above that end was refused (``scanned`` None) or read far more than
    its estimate, and the files placed there by the guess would be in it.
    Evidence gathered under the guess is discarded; ``True`` when dropped."""
    layout = run.layout
    if run.low_end_dropped or not getattr(layout, "low_end_files", 0) or not layout.dims:
        return False
    if layout.dims[0].kind != "partition" or piece.box[0][0] is None:
        return False
    everywhere = layout.without_low_end_guess()
    est = boxes.estimate(everywhere.groups, piece.box, everywhere.dims)
    if est <= piece.est:
        return False
    if scanned is not None and scanned <= GUESS_SLACK * max(run.calibration, 1.0) * piece.est:
        return False
    run.layout, run.low_end_dropped = everywhere, True
    run.bounds, run.measured = run.bounds[:1], []
    piece.est = est
    return True


def _outcome(run, shape, layout, piece, future) -> list[_Piece]:
    """The pieces still to run after one finished: none, or those replacing it."""
    from .athena_tools import AthenaEngineScanLimitError, AthenaQueryTimeoutError, AthenaScanBudgetError
    try:
        result, telemetry = future.result()
    except (AthenaEngineScanLimitError, AthenaScanBudgetError) as exc:
        run.refused += 1
        _count_bytes(run, getattr(exc, "adt_telemetry", {}))
        _guess_shown_wrong(run, piece, None)
        layout = _current_estimate(run, piece)
        try:
            return _refused_children(run, layout, piece)
        except ExactSplitRefused as why:
            raise why from exc
    except AthenaQueryTimeoutError as exc:
        return [_retry(run, layout, piece, exc)]
    except ExactSplitRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - any other piece failure: no total
        raise ExactSplitRefused("piece_failed", f"a piece failed ({type(exc).__name__})") from exc
    run.query_seconds.append(time.monotonic() - piece.started)
    piece.scanned = _count_bytes(run, telemetry)
    _accept(shape, piece, result, telemetry, run.caps.max_groups)
    _guess_shown_wrong(run, piece, piece.scanned)
    _current_estimate(run, piece)
    _measured(run, piece)
    return []


def _current_estimate(run: _Run, piece) -> plan.Layout:
    """The piece's estimate on the run's current layout (a piece launched
    before the placement guess was dropped carries the old one)."""
    piece.est = boxes.estimate(run.layout.groups, piece.box, run.layout.dims)
    return run.layout


def _retry(run: _Run, layout: plan.Layout, piece, exc) -> "_Piece":
    """The same box as a fresh query, for a piece whose query did not finish
    in its poll window (the poll already stopped it). Once only, and only
    with :data:`RETRY_MIN_SECONDS` of wall time left. The stopped attempt
    credits only the engine waiting its own poll recorded
    (``athena_call_time.PollWatch``), never its poll window as such."""
    run.timed_out_bytes += _count_bytes(run, {"statistics": getattr(exc, "statistics", {})})
    if piece.retried or run.deadline - time.monotonic() < RETRY_MIN_SECONDS:
        raise ExactSplitRefused("piece_timed_out", "a piece did not finish in time") from exc
    run.retried += 1
    fresh = _piece(layout, piece.box)
    fresh.retried = True
    return fresh


def _check_room(run: _Run, queue: list, running: dict) -> None:
    """Every queued piece needs at least one more query: stop now, before
    reading more, when they cannot all fit in the piece cap, or beside the
    running pieces in the byte cap."""
    if run.pieces_run + len(queue) > run.caps.max_pieces:
        raise ExactSplitRefused("piece_cap", f"it needs more than {run.caps.max_pieces} pieces")
    planned = sum(p.est for p in queue) * run.calibration + _reserved(run, running)
    if run.spent + planned > run.caps.max_total_bytes:
        raise ExactSplitRefused("byte_cap", "it would scan more than the total byte cap")


def _stop_queries(athena, query_ids: list) -> None:
    for query_id in query_ids:
        _stop_quietly(athena, query_id)


def _stop_running(run: _Run, pool, running: dict) -> None:
    """Stop every piece still running and drop the queued ones, waiting at
    most :data:`DEADLINE_OVERRUN_SECONDS` for the stop calls and the running
    pieces to end. Whatever is still running then is left running and
    tracked (:func:`_leave`): the stops in one worker, each piece in its own."""
    for future in running:
        future.cancel()
    ids = [p.telemetry.get("query_id") for p in running.values() if p.telemetry.get("query_id")]
    stopper = _start_stopper(run, ids) if ids else None
    pool.shutdown(wait=False, cancel_futures=True)
    ends = time.monotonic() + DEADLINE_OVERRUN_SECONDS
    if stopper is not None:
        stopper.join(max(0.0, ends - time.monotonic()))
    if running:
        wait(list(running), timeout=max(0.0, ends - time.monotonic()))
    if stopper is not None and stopper.is_alive():
        _leave((run.athena,), stopper.is_alive)
    for piece in running.values():
        if not piece.ended.is_set():
            _leave((run.athena,), lambda piece=piece: not piece.ended.is_set())


def _start_stopper(run: _Run, ids: list):
    """The thread stopping the running pieces' queries, holding a permit, or
    ``None`` when no permit is free: the split does not wait for one at this
    point, and each piece's own poll, bounded by the split's deadline, stops
    its query then."""
    permits = _take(run, wait=False)
    if permits is None:
        return None

    def stop():
        try:
            _stop_queries(run.athena, ids)
        finally:
            permits.give()
    stopper = threading.Thread(target=contextvars.copy_context().run, args=(stop,),
                               name="exact-split-stop", daemon=True)
    try:
        stopper.start()
    except BaseException:
        permits.give()
        raise
    return stopper


def _launch(run: _Run, pool, shape, layout, snapshot, piece: _Piece, permits):
    """Submit ``piece``'s query, holding ``permits``' permit (taken by the
    caller) until its thread ends: the future's done callback gives it back,
    which also covers a future cancelled before it ran."""
    try:
        _admit(run, piece)
        sql = _sql_for(shape, layout, piece.box, snapshot, run.clock_at)
        piece.started = time.monotonic()
        # A copy of the context, so a model tool call's deadline and engine-wait
        # record (athena_call_time) reach the piece's poll.
        future = pool.submit(contextvars.copy_context().run, _run_piece, run, sql,
                             piece.telemetry, shape.grouped or shape.has_percentile)
    except BaseException:
        permits.give()
        raise

    def ended(_future):
        permits.give()
        piece.ended.set()
    future.add_done_callback(ended)
    return future


def _execute(run: _Run, shape, leaves, snapshot) -> list[_Piece]:
    """Run the pieces, the biggest first and alone (a probe) when there are
    more than two, as many at once as :func:`_may_start` allows, re-planning
    the queue after every finished piece (on ``run.layout``, which a dropped
    placement guess replaces)."""
    queue = sorted((_piece(run.layout, b) for b in leaves), key=lambda p: -p.est)
    probing = len(queue) > 2
    done: list[_Piece] = []
    running: dict = {}
    # Not a ``with`` block: it would wait for every piece thread after a
    # refusal, past the deadline (:func:`_stop_running` waits a bounded time).
    pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
    try:
        while queue or running:
            while (queue and len(running) < (1 if probing else CONCURRENCY)
                   and _may_start(run, running, queue[0])):
                # Wait for a permit only while no piece of this split runs (it
                # then holds none); otherwise take one only if one is free, and
                # wait for a running piece instead.
                permits = _take(run, wait=not running)
                if permits is None:
                    break
                piece = queue.pop(0)
                running[_launch(run, pool, shape, run.layout, snapshot, piece, permits)] = piece
            finished, _ = wait(running, timeout=max(0.05, run.deadline - time.monotonic()),
                               return_when=FIRST_COMPLETED)
            if not finished:
                raise ExactSplitRefused("wall_time", _PIECES_LATE_DETAIL)
            for future in finished:
                piece = running.pop(future)
                later = _outcome(run, shape, run.layout, piece, future)
                if later:
                    queue.extend(later)
                else:
                    done.append(piece)
            probing = False
            for queued in queue:
                _current_estimate(run, queued)
            queue = _replan_queue(run, run.layout, queue)
            _check_room(run, queue, running)
            if run.spent > run.caps.max_total_bytes:
                raise ExactSplitRefused("byte_cap", "it scanned more than the total byte cap")
    except BaseException:
        _stop_running(run, pool, running)
        raise
    pool.shutdown(wait=True)
    return sorted(done, key=lambda p: boxes.box_order(p.box))


# --------------------------------------------------------------------------- #
# The one result
# --------------------------------------------------------------------------- #
def _lineage(shape, layout: plan.Layout, pieces, merged: merge.Merged, snapshot, run: _Run) -> dict:
    from .engine_names import Withheld
    partition = next((d for d in layout.dims if d.kind == "partition"), None)
    exact = merged.exact
    # The lineage reaches the model: a name that is not a plain identifier
    # (engine_names) is a placeholder, counted in names_withheld.
    names = Withheld()
    out = {
        "applied": True, "pieces": len(pieces),
        "table": names.show(".".join(shape.table_parts), "table"),
        "partition_column": names.show(partition.column) if partition else None,
        "split_columns": [names.show(d.column) for d in layout.dims],
        "ranges": [boxes.box_label(layout.dims, p.box) for p in pieces],
        "query_ids": [p.query_id for p in pieces],
        "row_counts": [len(p.rows.rows) if p.rows else 0 for p in pieces],
        "scanned_bytes": [p.scanned for p in pieces],
        "total_scanned_bytes": run.spent, "refused_pieces": run.refused,
        "retried_pieces": run.retried, "timed_out_attempt_bytes": run.timed_out_bytes,
        "calibration": round(run.calibration, 3), "plan_fraction": run.fraction,
        "snapshot_id": snapshot, "snapshot_pinned": True, "atomic": True, "exact": exact,
        "groups": merged.groups,
    }
    if names.count:
        out["names_withheld"] = names.count
    if shape.has_percentile:
        out["approximate"] = True
        out["percentile_accuracy"] = shape_mod.QDIGEST_ACCURACY
        out["percentile_value_type"] = shape.percentiles[0].value_type
    if shape.grouped:
        out["group_cap"] = run.caps.max_groups
        out["ordered"] = bool(shape.order)
    if shape.clock:
        out["clock_frozen_at"] = _instant_text(run.clock_at)
    if run.over_cap_plan:
        out["first_plan_over_cap"] = True
    if layout.low_end_files:
        out["files_placed_at_partition_low_end"] = layout.low_end_files
    if run.low_end_dropped:
        out["partition_low_end_guess_dropped"] = True
    if layout.newest:
        out["newest_in_file_statistics"] = dict(layout.newest)
    return out


def _note(lineage: dict) -> str:
    """The result's note. A table or split column name that is not a plain
    identifier (``engine_names``) is never printed; the note says how many
    names it withheld."""
    from .engine_names import Withheld
    names = Withheld()
    names.add(lineage.get("names_withheld") or 0)
    table = names.show(lineage["table"], "table")
    columns = " and ".join(names.show(c) for c in lineage["split_columns"])
    snapshot = f" at table snapshot {lineage['snapshot_id']}"
    if lineage.get("approximate"):
        accuracy = lineage.get("percentile_accuracy")
        exactness = (f" This is an APPROXIMATE percentile from {lineage['pieces']} pieces "
                     f"(qdigest accuracy {accuracy}), the same kind as approx_percentile; "
                     "it is not exact.")
    else:
        exactness = ("" if lineage["exact"] else
                     " A floating-point SUM was added, so that total may differ in its last digits.")
    clock = (f" The query's current time (for example current_timestamp) was fixed at "
             f"{lineage['clock_frozen_at']} for every piece." if lineage.get("clock_frozen_at") else "")
    branches = lineage.get("branches")
    how = (f"Computed from {lineage['pieces']} partition pieces across {branches} branch queries "
           f"of {table} split on {columns}{snapshot}, because the whole query is over the "
           "per-query scan limit, and its states are combined from per-state branches."
           if branches else
           f"Computed from {lineage['pieces']} partition pieces of {table} split on "
           f"{columns}{snapshot}, because the whole query is over the per-query scan limit.")
    return (f"{how} This is ONE result: use its rows as they are and do not add them to "
            f"other results.{exactness}{clock}{_groups_note(lineage)}{names.sentence()}")


def _groups_note(lineage: dict) -> str:
    """What a grouped result shows when it is not every group, read off the
    bounded result (:func:`_shown`): how many groups there are, which are
    shown, and when the result's size limits cut rows or columns, that too,
    so an answer can say exactly what it covers."""
    groups, shown = lineage.get("groups", 0), lineage.get("groups_shown", 0)
    if "ordered" not in lineage:
        return ""
    note = ""
    if shown < groups:
        if shown == 0:
            which = "none of them are shown"
        elif lineage["ordered"]:
            which = f"the first {shown} by its ORDER BY are shown"
        else:
            which = f"the {shown} shown are in no set order, so a group not shown may be larger"
        size = "; the rows after them did not fit the result's size limits" if lineage.get(
            "cut_by_size") else ""
        if shown == 0 and size:
            size = ": the first row did not fit the result's size limits"
        note = f" The query has {groups} groups in all; {which}{size}."
    if "columns_shown" in lineage:
        note += (f" Only the first {lineage['columns_shown']} of its {lineage['columns']} columns "
                 "are shown.")
    return note


def _shown(lineage: dict, merged: merge.Merged, result: dict) -> dict:
    """``lineage`` with what the bounded ``result`` actually holds: its rows
    (``groups_shown``), whether the size bound cut rows the merge kept
    (``cut_by_size``), and its columns when the bound dropped some."""
    out = {**lineage, "groups_shown": int(result.get("row_count") or 0)}
    if out["groups_shown"] < len(merged.rows):
        out["cut_by_size"] = True
    kept = len(result.get("columns") or [])
    if kept < len(merged.columns):
        out.update(columns_shown=kept, columns=len(merged.columns))
    return out


def _result(merged: merge.Merged, lineage: dict, source_system: str, capped_sql: str,
            queried_at) -> dict:
    from .response_integrity import attach_source_metadata
    from .response_integrity_query import bounded_query_result, mark_rows_cut
    result = bounded_query_result(merged.columns, merged.rows, False)
    if merged.cut:
        result = {**mark_rows_cut(result), "capped_at_limit": True}
    lineage = _shown(lineage, merged, result)
    result = {**result, "exact_split": lineage, "note": _note(lineage),
              "scanned_bytes": lineage["total_scanned_bytes"]}
    if not source_system:
        return result
    enriched = attach_source_metadata(result, boundary="athena", system_id=source_system,
                                      queried_at=queried_at)
    return {**enriched, "_adt_executed_query": capped_sql}


def _start(sql: str, connection_config: dict, refusal, client, glue_client=None) -> tuple:
    """The query's shape and a fresh run: its deadline, caps and reserve
    (:func:`_deadline_and_caps`), and its Athena client, ``client`` (the one
    that ran the refused query, or the caller's) or one built with a bounded
    config. Refused before anything is read while calls an earlier split left
    are still running (:func:`_refuse_while_busy`) or no time is left."""
    from .athena_tools import _resolve_config
    try:
        shape = shape_mod.parse_shape(sql)
    except shape_mod.ShapeRefusal as exc:
        raise ExactSplitRefused(exc.reason, exc.detail) from exc
    listener = _LISTENER.get()
    if listener is not None:
        listener()
    cfg = _resolve_config(connection_config)
    deadline, caps, reserve = _deadline_and_caps(cfg)
    _refuse_while_busy(reserve, client, glue_client)
    if reserve:
        if caps.max_seconds < PIECE_SECONDS_FLOOR:
            raise ExactSplitRefused("wall_time", NO_TIME_DETAIL)
    elif caps.max_seconds <= 1:
        raise ExactSplitRefused("wall_time", _NO_TIME_START_DETAIL)
    run = _Run(cfg, client, caps, deadline, reserve_seconds=reserve)
    if run.athena is None:
        # Nothing to reuse: a client built with a bounded config, waited on no
        # longer than the deadline (taken before it, so its build counts).
        run.athena = _within(run, _split_client, "athena", cfg)
    if shape.clock:
        run.clock_at = _clock_now()     # one instant for every piece, fraction of a second kept
    _count_bytes(run, {"statistics": {"DataScannedInBytes": getattr(refusal, "scanned_bytes", 0) or 0}})
    # The plain query's timed-out first attempt, when it was retried (athena_tools).
    stalled = {"statistics": {"DataScannedInBytes": getattr(refusal, "timed_out_attempt_bytes", 0) or 0}}
    run.timed_out_bytes += _count_bytes(run, stalled)
    return shape, run


def _instant_text(moment: datetime) -> str:
    """ISO 8601 UTC with the microseconds when there are any, so the lineage
    shows the instant every piece's clock literal was rendered from."""
    utc = moment.astimezone(timezone.utc)
    return f"{utc:%Y-%m-%dT%H:%M:%S.%fZ}" if utc.microsecond else f"{utc:%Y-%m-%dT%H:%M:%SZ}"


def _clock_now() -> datetime:
    """The instant a split's pieces read as the current time (UTC, to the
    microsecond; each clock use renders it at its own precision)."""
    return datetime.now(timezone.utc)


@dataclass
class _Computed:
    """One query's split: the merged rows, its lineage, and the run it used."""
    merged: merge.Merged
    lineage: dict
    run: _Run
    shape: shape_mod.Shape
    snapshot: int


def _compute(sql: str, connection_config: dict, *, refusal, client=None, glue_client=None,
             snapshot: int | None = None) -> _Computed:
    """Split ``sql`` into pieces and merge them. ``snapshot`` pins the pieces
    to a snapshot the caller already read; ``None`` reads the table's latest.
    Builds a fresh run (one query's own budget)."""
    shape, run = _start(sql, connection_config, refusal, client, glue_client)
    return _compute_against(run, shape, refusal=refusal, glue_client=glue_client,
                            snapshot=snapshot)


def _reset_transient(run: _Run) -> None:
    """Reset the per-query planning state before a new branch runs against a
    SHARED run, keeping the accumulating budget (spent bytes, piece count,
    grouped-row budget, deadline, refusal accounting)."""
    run.calibration = CALIBRATION
    run.cap = 0
    run.fraction = HEADROOM
    run.bounds = []
    run.measured = []
    run.over_cap_plan = False
    run.low_end_dropped = False
    run.layout = None


def _compute_against(run: _Run, shape, *, refusal, glue_client, snapshot: int | None,
                     max_rows: int | None = None, later_branches: int = 0) -> _Computed:
    """Plan and execute ``shape`` against ``run`` (its budget, deadline, piece
    and byte accounting), reading the catalog, pinning the snapshot (given or
    latest), planning boxes, running pieces and merging. Used for the one-query
    split and for each OR branch against a shared run. ``max_rows`` bounds the
    merged rows; an OR branch passes a large bound so ALL its groups survive
    into the cross-branch combine (the shared grouped-row byte budget already
    bounds the piece fetch), and the real ``MAX_ROWS`` plus ORDER BY and LIMIT
    apply once after the combine. ``later_branches`` is how many OR branches
    run after this one, for the time check after planning (:func:`_check_time`)."""
    from .sql_safety import MAX_ROWS
    rows_cap = MAX_ROWS if max_rows is None else max_rows
    meta = _table_meta(run, shape, glue_client)
    if shape.has_percentile:
        column_types = {col: info[1] for col, info in meta.columns.items()}
        try:
            shape = shape_mod.with_percentile_types(shape, column_types)
        except shape_mod.ShapeRefusal as exc:
            raise ExactSplitRefused(exc.reason, exc.detail) from exc
    cap = _piece_cap(run, refusal)
    catalog, database, table = _names(shape, run.cfg)
    if snapshot is None:
        snapshot = _latest_snapshot(run, _meta_relation(catalog, database, table, "$snapshots"))
    layout = _layout(run, shape, meta, snapshot)
    _check_expanded(shape, layout)
    leaves = _plan(run, layout, cap, refusal)
    _check_time(run, leaves, later_branches)
    run.layout = layout
    pieces = _execute(run, shape, leaves, snapshot)
    if shape.has_percentile:
        merged = _merge_percentile(run, shape, pieces)
    else:
        try:
            merged = merge.merge_pieces(shape, [p.rows for p in pieces], max_rows=rows_cap)
        except merge.MergeError as exc:
            raise ExactSplitRefused(exc.reason, exc.detail) from exc
    lineage = _lineage(shape, run.layout, pieces, merged, snapshot, run)
    return _Computed(merged, lineage, run, shape, snapshot)


def _merge_percentile(run: _Run, shape, pieces) -> merge.Merged:
    """Combine the pieces' per-group quantile digests with one later merge
    query (``athena_exact_percentile``), accumulate any COUNT/SUM/MIN/MAX
    columns beside it exactly, and build the result rows. The result is
    approximate (``exact=False``)."""
    from . import athena_exact_percentile as pct
    from .sql_safety import MAX_ROWS
    columns, types = _percentile_columns(shape, pieces)
    try:
        order, digests, accs, display = pct.collect(shape, [p.rows for p in pieces])
        by_gid = pct.quantiles_by_group(
            lambda sql: _meta_query(run, sql, "percentile merge"), shape, order, digests)
        rows, groups = pct.build_rows(shape, order, by_gid, accs, display, types)
    except pct.PercentileMergeError as exc:
        raise ExactSplitRefused(exc.reason, exc.detail) from exc
    except merge.MergeError as exc:
        raise ExactSplitRefused(exc.reason, exc.detail) from exc
    cut = len(rows) > MAX_ROWS
    return merge.Merged(columns, types, rows[:MAX_ROWS], exact=False, cut=cut, groups=groups)


def _percentile_columns(shape, pieces) -> tuple[list[str], list[str]]:
    """The result's column names and types: the GROUP BY keys as the pieces
    named them, the ordinary aggregates with their piece types, and the
    percentile output under its own alias, typed as its VALUE family -- a
    scalar is ``<value_type>``, an ARRAY form ``array(<value_type>)`` -- so it
    is not mislabelled varchar (the cell is still the engine's text)."""
    piece_cols = list(pieces[0].rows.columns) if pieces and pieces[0].rows else []
    piece_types = list(pieces[0].rows.types) if pieces and pieces[0].rows else []
    use = shape.percentiles[0]
    value_type = (use.value_type or "double").strip().lower()
    pctl_type = f"array({value_type})" if use.array else value_type
    columns, types = [], []
    for i, output in enumerate(shape.outputs):
        if output.kind == "percentile":
            columns.append(output.alias or (piece_cols[i] if i < len(piece_cols) else "p"))
            types.append(pctl_type)
        else:
            columns.append(piece_cols[i] if i < len(piece_cols) else (output.alias or f"c{i}"))
            types.append(piece_types[i] if i < len(piece_types) else "varchar")
    return columns, types


def _compute_or(branches, sql: str, connection_config: dict, *, refusal,
                source_system: str, client, glue_client, queried_at) -> dict:
    """Run each OR branch against ONE shared run (its budget, byte and piece
    caps, deadline and grouped-row budget apply across every branch), pinned to
    ONE shared snapshot, and combine the pieces (``athena_exact_or``). Each
    branch returns ALL of its groups; the original ORDER BY and LIMIT apply
    once in the combine. No total when any branch cannot be computed exactly;
    every existing guarantee holds across the combine."""
    from . import athena_exact_or as or_mod
    from .sql_safety import MAX_ROWS, apply_row_cap
    # ONE run and budget for the whole OR: the refusal's bytes are counted once
    # (not once per branch), and the snapshot is read against this run.
    first_shape, run = _start(branches.branch_sqls[0], connection_config, refusal, client,
                              glue_client)
    catalog, database, table = _names(first_shape, run.cfg)
    snapshot = _latest_snapshot(run, _meta_relation(catalog, database, table, "$snapshots"))
    computed: list[_Computed] = []
    for index, branch_sql in enumerate(branches.branch_sqls):
        branch_shape = first_shape if index == 0 else _branch_shape(branch_sql)
        _reset_transient(run)
        computed.append(_compute_against(run, shape_mod.all_groups(branch_shape), refusal=refusal,
                                         glue_client=glue_client, snapshot=snapshot,
                                         max_rows=GROUP_BYTES_BUDGET,
                                         later_branches=len(branches.branch_sqls) - 1 - index))
    merged = or_mod.combine([c.merged for c in computed], branches.shape, max_rows=MAX_ROWS)
    lineage = _or_lineage(branches.shape, [c.lineage for c in computed], snapshot, run,
                          merged.groups)
    capped_sql, _ = apply_row_cap(sql.strip().rstrip(";").strip(), MAX_ROWS)
    return _result(merged, lineage, source_system, capped_sql, queried_at)


def _branch_shape(branch_sql: str) -> shape_mod.Shape:
    try:
        return shape_mod.parse_shape(branch_sql)
    except shape_mod.ShapeRefusal as exc:
        raise ExactSplitRefused(exc.reason, exc.detail) from exc


def _or_lineage(shape, branch_lineages: list[dict], snapshot: int, run: _Run,
                groups: int) -> dict:
    """One lineage for the whole OR combine. The byte, piece and refusal totals
    come from the ONE shared run (counted once, not summed per branch); the
    per-branch detail lists (ranges, query ids, row counts, scanned bytes) are
    concatenated; ``groups`` is the COMBINED distinct group count."""
    first = branch_lineages[0]
    out = {
        "applied": True, "branches": len(branch_lineages), "branch_queries": len(branch_lineages),
        "pieces": run.pieces_run,
        "table": first["table"],
        "split_columns": first["split_columns"],
        "partition_column": first.get("partition_column"),
        "ranges": [r for line in branch_lineages for r in line["ranges"]],
        "query_ids": [q for line in branch_lineages for q in line["query_ids"]],
        "row_counts": [n for line in branch_lineages for n in line["row_counts"]],
        "scanned_bytes": [b for line in branch_lineages for b in line["scanned_bytes"]],
        "total_scanned_bytes": run.spent, "refused_pieces": run.refused,
        "retried_pieces": run.retried, "timed_out_attempt_bytes": run.timed_out_bytes,
        "snapshot_id": snapshot, "snapshot_pinned": True, "atomic": True,
        "exact": all(line["exact"] for line in branch_lineages),
        "groups": groups,
    }
    withheld = sum(line.get("names_withheld", 0) for line in branch_lineages)
    if withheld:
        out["names_withheld"] = withheld
    if shape.grouped:
        out["group_cap"] = first.get("group_cap")
        out["ordered"] = bool(shape.order)
    if any(line.get("first_plan_over_cap") for line in branch_lineages):
        out["first_plan_over_cap"] = True
    return out


def compute_exact(sql: str, connection_config: dict, *, refusal, source_system: str = "",
                  client=None, glue_client=None) -> dict:
    """The exact result of ``sql`` from partition pieces, or :class:`ExactSplitRefused`.

    When ``sql`` is an OR of per-branch single-window aggregates over one
    shared pinned column (``athena_exact_or``), each branch is split on its own
    and the disjoint pieces are combined; otherwise the one query is split.
    """
    from . import athena_exact_or as or_mod
    from .response_integrity import utc_now
    from .sql_safety import MAX_ROWS, apply_row_cap
    queried_at = utc_now()
    # Before the OR combine's column type read, the first catalog call.
    _refuse_while_busy(_caller_end()[1], client, glue_client)
    branches = or_mod.parse_or_branches(
        sql, column_type=_column_type_oracle(sql, connection_config, client, glue_client))
    if branches is not None:
        return _compute_or(branches, sql, connection_config, refusal=refusal,
                           source_system=source_system, client=client, glue_client=glue_client,
                           queried_at=queried_at)
    computed = _compute(sql, connection_config, refusal=refusal, client=client,
                        glue_client=glue_client)
    capped_sql, _ = apply_row_cap(computed.shape.sql, MAX_ROWS)
    return _result(computed.merged, computed.lineage, source_system, capped_sql, queried_at)


def _column_type_oracle(sql: str, connection_config: dict, client, glue_client):
    """A ``name -> Glue type`` lookup for the OR parser's disjointness check,
    reading the query's table from the catalog ONCE on first use (cached). Any
    failure (unparsable table, unreadable catalog) returns ``None`` for every
    column, so the OR combine is declined and the single split runs."""
    cache: dict = {}

    def lookup(name: str):
        if "columns" not in cache:
            cache["columns"] = _read_column_types(sql, connection_config, client, glue_client)
        return (cache["columns"] or {}).get(str(name).lower())
    return lookup


def _read_column_types(sql: str, connection_config: dict, client, glue_client) -> dict | None:
    """The query's table's column types, or ``None`` (the OR combine is then
    declined). The catalog read comes before the split starts, so it has the
    deadline the split would get; one cut at that deadline refuses the split
    (``wall_time``) rather than handing it a client still in use."""
    from .athena_tools import _resolve_config
    try:
        shape = shape_mod.parse_shape(sql)
        cfg = _resolve_config(connection_config)
        deadline, caps, reserve = _deadline_and_caps(cfg)
        run = _Run(cfg, client, caps, deadline, reserve_seconds=reserve)
        meta = _table_meta(run, shape, glue_client)
        return {col: info[1] for col, info in meta.columns.items()}
    except ExactSplitRefused as exc:
        if exc.reason == "wall_time":
            raise
        return None
    except Exception:  # noqa: BLE001 - an unreadable type just declines the OR combine
        return None
