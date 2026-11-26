# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Mirror the live story's anchor into SSM for the story site's OPC UA server.

ADR-0092: the live story's telemetry comes from the story site's OPC UA
server (read by SFC into SiteWise), never from a Lambda writing SiteWise. The
server must play its dataset from the same anchor the CMMS order and the MES
checks follow, ``simulation_config.story_anchor_epoch``, but it has no
database access. So each simulator cycle copies the anchor into one SSM
parameter (``STORY_ANCHOR_PARAMETER``, set on a live synth only) and the
server polls it.

* The database row stays the one source of truth: the parameter is a derived
  copy, rewritten only when it differs, so a failed write heals on the next
  cycle (about a minute).
* ``present`` writes the anchor; ``absent`` writes ``"0"`` (the server's
  "no anchor, wall clock", the simulator's own rule); ``unknown`` (the read
  failed this cycle) writes nothing, so the server keeps the clock it has
  rather than guessing the wall clock.
* Never raises: a mirror failure costs at most one cycle of lag and must not
  stop the simulator.
* After a placement the value also carries the server's catch-up
  (``story_feed_catch_up``, written by ``story_replan``) until it ends, so
  the sensor climbs to the new alarm instead of stepping there (SiteWise
  history cannot be rewritten).
* HOLD (ADR-0092 decision 8): when the package auto-places on the first
  sign-in (``HOLD_ENV``, set on a live synth with an auto-place policy) and
  the stored ``story_anchor_source`` is still a default one (nobody placed
  it yet), the value is ``"<anchor>;healthy"`` and the server publishes no
  value (BadWaitingForInitialData, which SFC drops): SiteWise then holds
  nothing that the placement could leave unexplained. A catch-up away from
  such an era climbs from the healthy row (``HEALTHY_FROM``), not from the
  deploy anchor's row. When the source is unreadable the parameter is left
  as it is.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, NamedTuple, Optional

from .logging_config import get_logger

logger = get_logger(__name__)

PARAMETER_ENV = "STORY_ANCHOR_PARAMETER"   # stacks/_workshop_story.ANCHOR_PARAMETER_ENV_NAME
HOLD_ENV = "STORY_FEED_HOLD_UNTIL_PLACED"  # stacks/_workshop_story.FEED_HOLD_ENV_NAME
NO_ANCHOR = "0"
CATCH_UP_KEY = "story_feed_catch_up"       # story_history.CATCH_UP_KEY (written by story_replan)
# The one-time history replay (the replay ADR): story_replan records the
# window ("<H>;<from>;<to>") on the first placement out of the HOLD; this
# module copies it to a SECOND SSM parameter with the replay's identity
# appended ("<H>;<from>;<to>;<id>"), and records story_feed_live_since (the
# identity) once, so the value stays the same across cycles and restarts.
BACKFILL_KEY = "story_feed_backfill"       # story_history.BACKFILL_KEY
LIVE_SINCE_KEY = "story_feed_live_since"   # story_history.LIVE_SINCE_KEY
BACKFILL_PARAMETER_ENV = "STORY_FEED_BACKFILL_PARAMETER"  # _workshop_story.BACKFILL_PARAMETER_ENV_NAME
BACKFILL_PENDING = "pending"               # the second parameter's value before the first placement
SOURCE_KEY = "story_anchor_source"         # story_replan.SOURCE_KEY
# story_history.LIVE_ANCHOR_KEY (ADR-0095): present only after the alarm was
# moved without rewriting the history, which is a placement for the feed
# (the hold ends, the catch-up climbs), whatever the history's own source.
LIVE_ANCHOR_KEY = "story_live_anchor_epoch"
UNPLACED_SOURCES = ("deploy", "packaged")  # story_replan.DEFAULT_SOURCES: nobody placed it
ANCHOR_SOURCES = (*UNPLACED_SOURCES, "placed")   # story_history.ANCHOR_SOURCES
HELD_SUFFIX = ";healthy"                   # clock.mjs HELD_SUFFIX: publish nothing until placed
HEALTHY_FROM = 0                           # clock.mjs HEALTHY_FROM: catch up from the healthy row
_READ_FEED_SQL = "SELECT config_key, config_value FROM simulation_config WHERE config_key = ANY(%s)"


class CatchUp(NamedTuple):
    """A placement's catch-up as ``story_replan`` stored it."""
    previous: int
    start: int
    end: int
    previous_source: Optional[str] = None   # absent on a row written before decision 8


class FeedState(NamedTuple):
    """What the mirror reads besides the anchor (None: absent or unreadable)."""
    catch_up: Optional[CatchUp] = None
    source: Optional[str] = None


def enabled() -> bool:
    """True when a live synth set the parameter this module mirrors into."""
    return bool(os.environ.get(PARAMETER_ENV, "").strip())


def hold_enabled() -> bool:
    """True when a live synth packaged an auto-place policy (decision 8)."""
    return os.environ.get(HOLD_ENV, "").strip() == "1"


def backfill_enabled() -> bool:
    """True when a live synth set the second (history-replay) parameter."""
    return bool(os.environ.get(BACKFILL_PARAMETER_ENV, "").strip())


def parse_catch_up(raw: Optional[str]) -> Optional[CatchUp]:
    """The stored row ``"<previous anchor>;<start>;<end>[;<previous source>]"``,
    or None when it is absent or malformed (three positive integers with
    start < end, then optionally one of :data:`ANCHOR_SOURCES`)."""
    parts = str(raw or "").split(";")
    numbers, source = parts[:3], parts[3] if len(parts) == 4 else None
    if len(parts) not in (3, 4) or not all(p.isascii() and p.isdigit() for p in numbers):
        return None
    if len(parts) == 4 and source not in ANCHOR_SOURCES:
        return None
    previous, start, end = (int(p) for p in numbers)
    return CatchUp(previous, start, end, source) if previous > 0 and 0 < start < end else None


def _served_from(catch_up: CatchUp, hold: bool) -> int:
    """Where the server climbs from: the healthy row when it was held."""
    return HEALTHY_FROM if hold and catch_up.previous_source in UNPLACED_SOURCES else catch_up.previous


def mirror_value(clock_state: str, anchor: Optional[float], catch_up: Optional[CatchUp] = None,
                 now: float = 0.0, source: Optional[str] = None, hold: bool = False) -> Optional[str]:
    """The parameter value for this cycle's clock, or ``None`` to leave it.
    While a placement's catch-up has not ended it rides along as
    ``"<anchor>;<previous anchor>;<start>;<end>"`` (clock.mjs applies it).
    With ``hold``, an unplaced ``source`` is ``"<anchor>;healthy"`` and an
    unknown one leaves the parameter alone."""
    if clock_state == "present" and anchor is not None and anchor > 0:
        value = str(int(anchor)) if float(anchor).is_integer() else repr(float(anchor))
        if hold and source not in ANCHOR_SOURCES:
            return None
        if hold and source in UNPLACED_SOURCES:
            return value + HELD_SUFFIX
        catch_up = None if catch_up is None else CatchUp(*catch_up)
        if catch_up is not None and now < catch_up.end and catch_up.previous != anchor:
            value += f";{_served_from(catch_up, hold)};{catch_up.start};{catch_up.end}"
        return value
    if clock_state == "absent":
        return NO_ANCHOR
    return None


def read_feed_state(conn: Any) -> FeedState:
    """The stored catch-up (:func:`parse_catch_up`) and anchor source, in one
    read; both None on any error. While a live anchor is stored (the alarm was
    moved without rewriting the history, ADR-0095) the source is ``placed``:
    the feed follows a placed clock whatever the history's own source. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(_READ_FEED_SQL, ([CATCH_UP_KEY, SOURCE_KEY, LIVE_ANCHOR_KEY],))
            rows = dict(cur.fetchall())
        conn.rollback()
        source = "placed" if rows.get(LIVE_ANCHOR_KEY) else rows.get(SOURCE_KEY)
        return FeedState(parse_catch_up(rows.get(CATCH_UP_KEY)), source)
    except Exception as e:
        logger.warning(f"Story anchor mirror: feed state not read (no catch-up or hold this cycle): {e}")
        return FeedState()


def read_catch_up(conn: Any) -> Optional[CatchUp]:
    """The stored catch-up alone (:func:`read_feed_state`). Never raises."""
    return read_feed_state(conn).catch_up


# ---------------------------------------------------------------------------
# One-time history replay (the replay ADR): the SECOND SSM parameter
# ---------------------------------------------------------------------------

_READ_BACKFILL_SQL = "SELECT config_key, config_value FROM simulation_config WHERE config_key = ANY(%s)"
_BACKFILL_FACT = re.compile(r"^(\d+);(\d+);(\d+)$")
BACKFILL_DONE_SUFFIX = ";done"             # clock.mjs BACKFILL_DONE: written by the server when finished


def backfill_value(fact: Optional[str], live_since: Optional[float],
                   now: float) -> tuple[str, Optional[int]]:
    """The second parameter's value and the ``story_feed_live_since`` to store.

    ``fact`` is ``story_replan``'s ``"<H>;<from>;<to>"`` or None. Until a
    placement records one (or when it is malformed, or ``from >= to``) the
    value is ``"pending"`` and nothing is recorded, so the server replays
    nothing. Once there is a fact the value is ``"<H>;<from>;<to>;<id>"``:
    ``id`` is the recorded ``live_since`` if present, else ``now`` (recorded
    then). It is the replay's IDENTITY, not its start: the server paces the
    replay from its own first sight of the window, and a Reset clears both
    keys so the next placement gets a new identity."""
    m = _BACKFILL_FACT.match(str(fact or "").strip())
    if not m:
        return BACKFILL_PENDING, None
    _h, frm, to = (int(g) for g in m.groups())
    if frm >= to:
        return BACKFILL_PENDING, None
    start = int(live_since) if live_since is not None else int(now)
    return f"{m.group(0)};{start}", start


def read_backfill_state(conn: Any) -> tuple[Optional[str], Optional[float]]:
    """The stored backfill fact and ``story_feed_live_since`` (both None on any
    error or when absent), in one read. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(_READ_BACKFILL_SQL, ([BACKFILL_KEY, LIVE_SINCE_KEY],))
            rows = dict(cur.fetchall())
        conn.rollback()
        since = rows.get(LIVE_SINCE_KEY)
        return rows.get(BACKFILL_KEY), (float(since) if since is not None else None)
    except Exception as e:
        logger.warning(f"Story backfill state not read this cycle: {e}")
        return None, None


def sync_backfill(fact: Optional[str], live_since: Optional[float], now: float,
                  client_factory: Optional[Callable[[], Any]] = None) -> str:
    """Bring the SECOND SSM parameter in line with the stored backfill fact;
    returns ``off``/``same``/``written``/``failed``. Never raises. The parameter
    stays ``pending`` until a placement records a window. The server's own
    ``"<value>;done"`` mark for the same window counts as in line (it is never
    reverted, or a restart would replay the window again); any other value is
    overwritten. ``story_feed_live_since`` is recorded by the orchestrator from
    :func:`backfill_value`."""
    name = os.environ.get(BACKFILL_PARAMETER_ENV, "").strip()
    if not name:
        return "off"
    value, _record_since = backfill_value(fact, live_since, now)
    try:
        client = (client_factory or _default_client)()
        current = client.get_parameter(Name=name)["Parameter"]["Value"]
        if current == value or (value != BACKFILL_PENDING and current == value + BACKFILL_DONE_SUFFIX):
            return "same"
        client.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
        logger.info(f"Story backfill mirror: {name} {current!r} -> {value!r}")
        return "written"
    except Exception as e:
        logger.warning(f"Story backfill mirror failed (retried next cycle): {e}")
        return "failed"


def _default_client() -> Any:
    import boto3  # deferred: demo and frozen synths never set the parameter

    region = os.environ.get("REGION") or os.environ.get("AWS_REGION")
    return boto3.client("ssm", region_name=region)


def sync(clock_state: str, anchor: Optional[float],
         client_factory: Callable[[], Any] = _default_client,
         catch_up: Optional[CatchUp] = None, now: float = 0.0, source: Optional[str] = None) -> str:
    """Bring the parameter in line with this cycle's clock; returns what it did
    (``off``, ``skip``, ``same``, ``written`` or ``failed``). Never raises.
    ``source`` is the stored ``story_anchor_source`` (the hold reads it)."""
    name = os.environ.get(PARAMETER_ENV, "").strip()
    if not name:
        return "off"
    value = mirror_value(clock_state, anchor, catch_up, now, source, hold_enabled())
    if value is None:
        return "skip"
    try:
        client = client_factory()
        current = client.get_parameter(Name=name)["Parameter"]["Value"]
        if current == value:
            return "same"
        client.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
        logger.info(f"Story anchor mirror: {name} {current!r} -> {value!r}")
        return "written"
    except Exception as e:
        logger.warning(f"Story anchor mirror failed (retried next cycle): {e}")
        return "failed"


# The lookup's telemetry status (agents/demo/story_lookup.py::_telemetry_status)
# reads these two rows: which anchor the OPC UA feed follows, and the last grid
# point at which the mirror confirmed it. story_history / story_replan reset
# them on every era, so the status reads "not yet" until a cycle confirms. The
# compare-and-set is on the LIVE anchor (the live key when present, else the
# history anchor), the same value story_live.read_anchor returns.
CONFIRM_SQL = """
    UPDATE simulation_config SET config_value = CASE config_key
            WHEN 'story_telemetry_anchor' THEN %(anchor)s
            ELSE %(confirmed)s END,
        updated_at = NOW()
    WHERE config_key IN ('story_telemetry_anchor', 'story_telemetry_cursor_epoch')
      AND EXISTS (SELECT 1 FROM (
                      SELECT config_value FROM simulation_config
                      WHERE config_key IN ('story_live_anchor_epoch', 'story_anchor_epoch')
                      ORDER BY config_key = 'story_live_anchor_epoch' DESC LIMIT 1) a
                  WHERE a.config_value ~ '^[0-9]+(\\.[0-9]+)?$'
                    AND a.config_value::double precision = %(anchor_value)s)
"""


def confirm(conn: Any, anchor: float, now: float, grid_seconds: int = 300) -> int:
    """Record that the parameter now carries ``anchor``; a no-op (0 rows) when
    a placement moved the stored anchor meanwhile (compare-and-set). Commits;
    never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(CONFIRM_SQL, {"anchor": repr(float(anchor)), "anchor_value": float(anchor),
                                      "confirmed": str(int(now // grid_seconds) * grid_seconds)})
            updated = cur.rowcount
        conn.commit()
        return updated
    except Exception as e:
        logger.warning(f"Story anchor mirror confirmation not recorded: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
