# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one-time history replay's SIMULATION half (the replay ADR).

Pure-function tests (no Postgres), so they run in the offline unit lane:

* ``story_history`` names the two new contract keys and clears them with the
  story (``story_feed_backfill`` and ``story_feed_live_since``);
* ``story_replan`` records the backfill WINDOW as a fact on a full re-plan
  whose previous era was unplaced (same pattern as ``catch_up_row``), bounded
  by SiteWise's 7-day floor;
* ``story_anchor_mirror`` turns that fact plus a once-recorded
  ``story_feed_live_since`` into the second SSM parameter's value
  ``"<H>;<from>;<to>;<replay start>"``, and leaves it ``pending`` until a
  placement records a backfill.
"""
import pytest

from erp_simulation import condition_monitor as cm
from erp_simulation import story_anchor_mirror as mirror
from erp_simulation import story_history as sh
from erp_simulation import story_replan as sr

ANCHOR = 1_790_000_010
GRID = cm.TELEMETRY_GRID_SECONDS


# --------------------------------------------------------- story_history ---

def test_the_backfill_keys_are_named_and_cleared_with_the_story():
    assert sh.BACKFILL_KEY == "story_feed_backfill"
    assert sh.LIVE_SINCE_KEY == "story_feed_live_since"
    # NOT plan rows (written on a placement, like the catch-up): absent from CONFIG_KEYS
    assert sh.BACKFILL_KEY not in sh.CONFIG_KEYS
    assert sh.LIVE_SINCE_KEY not in sh.CONFIG_KEYS


class _ClearConn:
    def __init__(self):
        self.deleted_keys = []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params):
                conn.deleted_keys.append(tuple(params[0]))
                self.rowcount = len(params[0])

            @property
            def rowcount(self):
                return self._rc

            @rowcount.setter
            def rowcount(self, v):
                self._rc = v
        return _Cur()

    def commit(self):
        pass

    def rollback(self):
        pass


def test_clear_story_config_also_clears_the_backfill_keys():
    conn = _ClearConn()
    sh.clear_story_config(conn)
    cleared = conn.deleted_keys[0]
    assert sh.BACKFILL_KEY in cleared and sh.LIVE_SINCE_KEY in cleared
    assert sh.CATCH_UP_KEY in cleared and sh.LIVE_ANCHOR_KEY in cleared


# ---------------------------------------------------------- story_replan ---

def test_backfill_window_is_the_story_depth_cut_to_the_deepest_window():
    now = ANCHOR + 90 * 60                         # placement 90 min after the anchor
    frm, to = sh.backfill_window(ANCHOR, now)
    # ends at the grid floor of the placement and starts exactly 3 days before
    # the anchor, on the anchor's grid
    assert (frm, to) == (ANCHOR - sh.BACKFILL_DAYS * sh.DAY, cm.grid_floor(now))
    assert frm % GRID == ANCHOR % GRID
    # a very old anchor is cut to the deepest window back from the placement,
    # never widened to SiteWise's 7-day floor
    late = ANCHOR + 30 * sh.DAY
    frm2, to2 = sh.backfill_window(ANCHOR, late)
    assert late - sh.BACKFILL_MAX_DEPTH_SECONDS <= frm2 < late - sh.BACKFILL_MAX_DEPTH_SECONDS + GRID
    assert frm2 % GRID == ANCHOR % GRID
    assert (to2 - frm2) // GRID <= sh.BACKFILL_MAX_DEPTH_SECONDS // GRID
    # every point of it is still accepted by SiteWise a whole horizon later
    assert late + sh.REPLAY_HORIZON_SECONDS - frm2 <= sh.SITEWISE_WINDOW_SECONDS


def test_backfill_row_records_the_window_as_a_fact():
    now = ANCHOR + 90 * 60
    frm, to = sh.backfill_window(ANCHOR, now)
    row = sh.backfill_row(ANCHOR, now)
    assert row.key == sh.BACKFILL_KEY
    assert row.value == f"{ANCHOR};{frm};{to}"


# ----------------------------------------------------- story_anchor_mirror ---

def test_backfill_value_adds_the_replay_identity_and_is_pending_until_a_placement():
    # no backfill fact yet (pending until the first placement records one)
    assert mirror.backfill_value(None, live_since=None, now=ANCHOR + 50) == ("pending", None)
    fact = f"{ANCHOR};{ANCHOR - 3 * sh.DAY};{ANCHOR - 600}"
    # first time: records live_since = now and appends it as the replay's identity
    value, live_since = mirror.backfill_value(fact, live_since=None, now=ANCHOR + 50)
    assert value == f"{fact};{ANCHOR + 50}" and live_since == ANCHOR + 50
    # thereafter: keeps the recorded identity (the same value every cycle)
    value2, live_since2 = mirror.backfill_value(fact, live_since=ANCHOR + 50, now=ANCHOR + 999)
    assert value2 == f"{fact};{ANCHOR + 50}" and live_since2 == ANCHOR + 50


class _FakeSsm:
    def __init__(self, value):
        self.value, self.puts = value, []

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.value}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        self.value = Value
        self.puts.append(Value)


def test_the_mirror_keeps_the_servers_done_mark_for_the_same_window(monkeypatch):
    monkeypatch.setenv(mirror.BACKFILL_PARAMETER_ENV, "/adt/workshop/story-feed-backfill")
    fact = f"{ANCHOR};{ANCHOR - 3 * sh.DAY};{ANCHOR - 600}"
    value, _ = mirror.backfill_value(fact, ANCHOR + 50, ANCHOR + 60)
    ssm = _FakeSsm(value + mirror.BACKFILL_DONE_SUFFIX)
    assert mirror.sync_backfill(fact, ANCHOR + 50, ANCHOR + 60, lambda: ssm) == "same"
    assert ssm.puts == [], "reverting the done mark would replay the window after a restart"
    # a new window (another identity) replaces the old done mark ...
    assert mirror.sync_backfill(fact, ANCHOR + 900, ANCHOR + 960, lambda: ssm) == "written"
    assert ssm.value == f"{fact};{ANCHOR + 900}"
    # ... and no window (a Reset cleared it) is "pending", even over a done mark
    ssm.value = value + mirror.BACKFILL_DONE_SUFFIX
    assert mirror.sync_backfill(None, None, ANCHOR + 60, lambda: ssm) == "written"
    assert ssm.value == mirror.BACKFILL_PENDING


def test_backfill_value_rejects_a_malformed_fact():
    for bad in ["", "1;2", "a;b;c", f"{ANCHOR};{ANCHOR};{ANCHOR - 10}"]:
        assert mirror.backfill_value(bad, live_since=None, now=ANCHOR)[0] == "pending"


def test_the_backfill_param_name_and_key_match_the_stack_and_the_history():
    from stacks import _workshop_story
    assert mirror.BACKFILL_KEY == sh.BACKFILL_KEY
    assert mirror.LIVE_SINCE_KEY == sh.LIVE_SINCE_KEY
    assert mirror.BACKFILL_PARAMETER_ENV == _workshop_story.BACKFILL_PARAMETER_ENV_NAME


class _BackfillConn:
    def __init__(self, backfill=None, live_since=None):
        self.backfill, self.live_since = backfill, live_since

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params):
                self._keys = list(params[0])

            def fetchall(self):
                rows = [(mirror.BACKFILL_KEY, conn.backfill), (mirror.LIVE_SINCE_KEY, conn.live_since)]
                return [(k, v) for k, v in rows if v is not None]
        return _Cur()

    def rollback(self):
        pass


def test_read_backfill_state_never_raises():
    fact = f"{ANCHOR};{ANCHOR - 600};{ANCHOR - 300}"
    assert mirror.read_backfill_state(_BackfillConn(fact, "123")) == (fact, 123)
    assert mirror.read_backfill_state(_BackfillConn(None, None)) == (None, None)
