# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The simulator mirrors the story anchor into SSM for the story OPC UA server (ADR-0092)."""
import pytest

from erp_simulation import simulation_orchestrator as orch_mod
from erp_simulation import story_anchor_mirror as mirror

PARAM = "/adt/workshop/story-anchor-epoch"
ANCHOR = 1_790_000_010.0


class _Ssm:
    def __init__(self, value="pending", fail=None):
        self.value, self.fail, self.puts = value, fail, []

    def get_parameter(self, Name):
        if self.fail == "get":
            raise RuntimeError("throttled")
        return {"Parameter": {"Name": Name, "Value": self.value}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        if self.fail == "put":
            raise RuntimeError("denied")
        assert (Name, Type, Overwrite) == (PARAM, "String", True)
        self.puts.append(Value)
        self.value = Value


@pytest.fixture
def param(monkeypatch):
    monkeypatch.setenv(mirror.PARAMETER_ENV, PARAM)


def test_the_clock_state_names_are_the_orchestrators():
    assert {orch_mod.STORY_CLOCK_PRESENT, orch_mod.STORY_CLOCK_ABSENT, orch_mod.STORY_CLOCK_UNKNOWN} \
        == {"present", "absent", "unknown"}


@pytest.mark.parametrize("state, anchor, value", [
    ("present", ANCHOR, "1790000010"),
    ("present", ANCHOR + 0.5, repr(ANCHOR + 0.5)),
    ("absent", None, "0"),                      # no anchor row: the server's wall clock
    ("unknown", None, None),                    # read failed: leave the server's clock alone
    ("present", None, None),
])
def test_mirror_value(state, anchor, value):
    assert mirror.mirror_value(state, anchor) == value


def test_a_catch_up_rides_along_until_it_ends():
    catch_up = (1_789_990_010, 1_790_000_130, 1_790_019_610)
    value = mirror.mirror_value("present", ANCHOR, catch_up, now=1_790_000_020)
    assert value == "1790000010;1789990010;1790000130;1790019610"
    assert mirror.mirror_value("present", ANCHOR, catch_up, now=catch_up[2]) == "1790000010"
    assert mirror.mirror_value("present", ANCHOR, (int(ANCHOR), 1, 2), now=0) == "1790000010"
    assert mirror.mirror_value("absent", None, catch_up, now=0) == "0"


@pytest.mark.parametrize("raw, parsed", [
    ("1789990010;1790000130;1790019610", (1789990010, 1790000130, 1790019610, None)),
    ("1;2;3;deploy", (1, 2, 3, "deploy")), ("1;2;3;packaged", (1, 2, 3, "packaged")),
    ("1;2;3;placed", (1, 2, 3, "placed")),
    (None, None), ("", None), ("1;2", None), ("1;3;2", None), ("0;1;2", None), ("a;1;2", None),
    ("1;2;3;4", None), ("1.5;2;3", None), ("1;2;3;elsewhere", None), ("1;2;3;deploy;x", None),
    ("1;3;2;deploy", None),
])
def test_parse_catch_up_is_strict(raw, parsed):
    assert mirror.parse_catch_up(raw) == parsed


class _Conn:
    def __init__(self, value=None, fail=False, source=None, live=None):
        self.value, self.fail, self.source, self.live, self.rollbacks = value, fail, source, live, 0

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params):
                if conn.fail:
                    raise RuntimeError("connection reset")
                assert params == ([mirror.CATCH_UP_KEY, mirror.SOURCE_KEY, mirror.LIVE_ANCHOR_KEY],)

            def fetchall(self):
                rows = [(mirror.CATCH_UP_KEY, conn.value), (mirror.SOURCE_KEY, conn.source),
                        (mirror.LIVE_ANCHOR_KEY, conn.live)]
                return [(key, value) for key, value in rows if value is not None]
        return _Cur()

    def rollback(self):
        self.rollbacks += 1


def test_read_catch_up_never_raises():
    assert mirror.read_catch_up(_Conn("1;2;3")) == (1, 2, 3, None)
    assert mirror.read_catch_up(_Conn(None)) is None
    assert mirror.read_catch_up(_Conn(fail=True)) is None


def test_read_feed_state_reads_the_catch_up_and_the_source_in_one_go():
    assert mirror.read_feed_state(_Conn("1;2;3;deploy", source="placed")) == ((1, 2, 3, "deploy"), "placed")
    assert mirror.read_feed_state(_Conn(None, source="deploy")) == (None, "deploy")
    assert mirror.read_feed_state(_Conn(fail=True)) == (None, None)


def test_read_feed_state_reports_a_live_anchor_as_placed():
    """ADR-0095: while the alarm was moved without rewriting the history, the
    feed follows a PLACED clock whatever the history's own source (the hold
    ends, the catch-up climbs)."""
    assert mirror.read_feed_state(_Conn(None, source="deploy", live="1790000000.0")) == (None, "placed")
    assert mirror.read_feed_state(_Conn("1;2;3;deploy", source="packaged", live="1.0")) == ((1, 2, 3, "deploy"), "placed")
    assert mirror.LIVE_ANCHOR_KEY == "story_live_anchor_epoch"


def test_the_keys_and_sources_are_the_story_writers():
    from erp_simulation import story_history as sh
    from erp_simulation import story_replan as sr
    assert mirror.CATCH_UP_KEY == sh.CATCH_UP_KEY
    assert mirror.SOURCE_KEY == sr.SOURCE_KEY
    assert mirror.UNPLACED_SOURCES == sr.DEFAULT_SOURCES
    assert set(mirror.ANCHOR_SOURCES) == set(sh.ANCHOR_SOURCES)


def test_the_hold_env_is_the_one_a_live_synth_sets():
    from stacks import _workshop_story
    assert mirror.HOLD_ENV == _workshop_story.FEED_HOLD_ENV_NAME
    assert mirror.PARAMETER_ENV == _workshop_story.ANCHOR_PARAMETER_ENV_NAME


# ------------------------------------------- hold (ADR-0092 decision 8) ---

CATCH_UP = (1_789_990_010, 1_790_000_130, 1_790_019_610)


@pytest.mark.parametrize("source", ["deploy", "packaged"])
def test_an_unplaced_clock_is_held_healthy_when_the_package_auto_places(source):
    assert mirror.mirror_value("present", ANCHOR, source=source, hold=True) == "1790000010;healthy"
    # a catch-up from an earlier era (a reset back onto the default) does not apply: held is held
    assert mirror.mirror_value("present", ANCHOR, (*CATCH_UP, "placed"), now=CATCH_UP[1],
                               source=source, hold=True) == "1790000010;healthy"


@pytest.mark.parametrize("source", ["deploy", "packaged", "placed", None])
def test_without_auto_place_the_mill_follows_its_anchor_exactly_as_before(source):
    assert mirror.mirror_value("present", ANCHOR, source=source) == "1790000010"
    assert mirror.mirror_value("present", ANCHOR, (*CATCH_UP, "deploy"), now=CATCH_UP[1], source=source) \
        == "1790000010;1789990010;1790000130;1790019610"


def test_the_first_placement_climbs_from_the_healthy_row():
    value = mirror.mirror_value("present", ANCHOR, (*CATCH_UP, "deploy"), now=CATCH_UP[1],
                                source="placed", hold=True)
    assert value == "1790000010;0;1790000130;1790019610"
    assert mirror.mirror_value("present", ANCHOR, (*CATCH_UP, "deploy"), now=CATCH_UP[2],
                               source="placed", hold=True) == "1790000010"


@pytest.mark.parametrize("previous_source", ["placed", None])
def test_a_later_placement_climbs_from_the_previous_anchor(previous_source):
    value = mirror.mirror_value("present", ANCHOR, (*CATCH_UP, previous_source), now=CATCH_UP[1],
                                source="placed", hold=True)
    assert value == "1790000010;1789990010;1790000130;1790019610"


@pytest.mark.parametrize("source", [None, "garbage"])
def test_an_unreadable_source_leaves_a_held_parameter_alone(source):
    assert mirror.mirror_value("present", ANCHOR, source=source, hold=True) is None
    assert mirror.mirror_value("absent", None, source=source, hold=True) == "0"


@pytest.mark.parametrize("raw, held", [("1", True), (" 1 ", True), ("", False), ("0", False),
                                       ("true", False), (None, False)])
def test_the_hold_env_is_a_strict_yes(monkeypatch, raw, held):
    if raw is None:
        monkeypatch.delenv(mirror.HOLD_ENV, raising=False)
    else:
        monkeypatch.setenv(mirror.HOLD_ENV, raw)
    assert mirror.hold_enabled() is held


def test_sync_holds_only_with_the_env(param, monkeypatch):
    monkeypatch.delenv(mirror.HOLD_ENV, raising=False)
    ssm = _Ssm()
    assert mirror.sync("present", ANCHOR, lambda: ssm, source="deploy") == "written"
    monkeypatch.setenv(mirror.HOLD_ENV, "1")
    assert mirror.sync("present", ANCHOR, lambda: ssm, source="deploy") == "written"
    assert mirror.sync("present", ANCHOR, lambda: ssm, source=None) == "skip"
    assert mirror.sync("present", ANCHOR, lambda: ssm, source="placed") == "written"
    assert ssm.puts == ["1790000010", "1790000010;healthy", "1790000010"]


def test_off_without_the_parameter_env(monkeypatch):
    monkeypatch.delenv(mirror.PARAMETER_ENV, raising=False)
    assert mirror.sync("present", ANCHOR, lambda: pytest.fail("no client without the env")) == "off"


def test_writes_only_when_the_copy_differs(param):
    ssm = _Ssm()
    assert mirror.sync("present", ANCHOR, lambda: ssm) == "written"
    assert mirror.sync("present", ANCHOR, lambda: ssm) == "same"
    assert ssm.puts == ["1790000010"]


def test_an_unknown_clock_never_touches_the_parameter(param):
    assert mirror.sync("unknown", None, lambda: pytest.fail("no client when unknown")) == "skip"


@pytest.mark.parametrize("fail", ["get", "put"])
def test_failures_never_raise(param, fail):
    assert mirror.sync("present", ANCHOR, lambda: _Ssm(fail=fail)) == "failed"


# ------------------------------------------------------ real PostgreSQL ---

@pytest.fixture(scope="module")
def pg_erp():
    from erp_simulation.tests.test_story_live import _fresh_erp_db
    with _fresh_erp_db() as conn:
        yield conn


def _set(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO simulation_config (config_key, config_value, default_value, data_type,
                                           category, description)
            VALUES (%s, %s, %s, 'string', 'story', 'test')
            ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value
        """, (key, value, value))
    conn.commit()


def _get(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT config_value FROM simulation_config WHERE config_key = %s", (key,))
        return cur.fetchone()[0]


def test_confirm_records_the_anchor_and_is_a_cas_on_the_stored_anchor(pg_erp):
    _set(pg_erp, "story_anchor_epoch", repr(ANCHOR))
    _set(pg_erp, "story_telemetry_anchor", repr(ANCHOR))
    _set(pg_erp, "story_telemetry_cursor_epoch", "1")
    assert mirror.confirm(pg_erp, ANCHOR, ANCHOR + 1234) == 2
    assert float(_get(pg_erp, "story_telemetry_anchor")) == ANCHOR
    assert _get(pg_erp, "story_telemetry_cursor_epoch") == str(int((ANCHOR + 1234) // 300) * 300)

    _set(pg_erp, "story_anchor_epoch", repr(ANCHOR + 3600))      # a placement moved it meanwhile
    assert mirror.confirm(pg_erp, ANCHOR, ANCHOR + 9999) == 0
    assert _get(pg_erp, "story_telemetry_cursor_epoch") == str(int((ANCHOR + 1234) // 300) * 300)


def test_confirm_never_raises():
    class Broken:
        def cursor(self):
            raise RuntimeError("connection lost")

        def rollback(self):
            raise RuntimeError("still lost")

    assert mirror.confirm(Broken(), ANCHOR, ANCHOR) == 0



def test_the_orchestrator_mirrors_the_stored_catch_up(param, monkeypatch):
    """The simulator step reads the placement's catch-up and the anchor
    source and hands them to the mirror with the cycle time; without the
    parameter it reads nothing."""
    from contextlib import contextmanager
    calls = []
    monkeypatch.setattr(mirror, "sync", lambda state, anchor, **kw: calls.append((state, anchor, kw)) or "skip")
    monkeypatch.setattr(mirror, "read_feed_state", lambda conn: mirror.FeedState((1, 2, 3), "placed"))
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1234.0)

    class _Db:
        @contextmanager
        def connection(self):
            yield object()

    orch = orch_mod.SimulationOrchestrator.__new__(orch_mod.SimulationOrchestrator)
    orch.db, orch._story_clock, orch._story_anchor = _Db(), orch_mod.STORY_CLOCK_PRESENT, ANCHOR
    assert orch._run_story_anchor_mirror() == "skip"
    assert calls == [("present", ANCHOR, {"catch_up": (1, 2, 3), "now": 1234.0, "source": "placed"})]
    monkeypatch.delenv(mirror.PARAMETER_ENV)
    orch._run_story_anchor_mirror()
    assert calls[-1][2]["catch_up"] is None and calls[-1][2]["source"] is None


def test_read_feed_state_on_postgres(pg_erp):
    _set(pg_erp, mirror.CATCH_UP_KEY, "1789990010;1790000130;1790019610;deploy")
    _set(pg_erp, mirror.SOURCE_KEY, "placed")
    assert mirror.read_feed_state(pg_erp) == ((1789990010, 1790000130, 1790019610, "deploy"), "placed")
    with pg_erp.cursor() as cur:
        cur.execute("DELETE FROM simulation_config WHERE config_key = ANY(%s)",
                    ([mirror.CATCH_UP_KEY, mirror.SOURCE_KEY],))
    pg_erp.commit()
    assert mirror.read_feed_state(pg_erp) == (None, None)
