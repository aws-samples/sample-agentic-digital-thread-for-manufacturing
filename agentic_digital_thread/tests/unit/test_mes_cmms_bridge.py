# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the MES-downtime → CMMS work-order bridge.

The bridge (erp_simulation/mes_simulator.py) creates a corrective CMMS work
order in the SEED database when a MECHANICAL/ELECTRICAL MES downtime event
fires. Pins the fixes for the silent cloud failure:

* the CMMS write goes through the SEED-DB connection, never the ERP one;
* one source of truth for CMMS site prefixes (DET/MKE/CRR — Cedar Rapids is
  CRR, matching lambda-adt-seed/004_cmms_maintenance.sql, not the old CR);
* asset pick is site-prefixed and prefers a reason-derived asset type with a
  deterministic (asset_tag-ordered) fallback;
* the work order carries the MES downtime event id in the description
  (free-text) and the reason as failure_code, into work_orders_raw
  (cmms work_orders is a non-insertable-with-ON-CONFLICT view);
* missing seed-DB environment ⇒ ONE loud warning and a skip — no raise,
  no silent swallow.

Fully offline: connections/cursors are mocks; no sockets, no AWS.
"""

import sys
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Repo root (for the erp_simulation package).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp_simulation import mes_simulator as mes_mod  # noqa: E402
from erp_simulation.mes_simulator import MESSimulator  # noqa: E402


@pytest.fixture()
def sim(monkeypatch):
    """MESSimulator with config loading stubbed out (no Secrets Manager)."""
    monkeypatch.setattr(mes_mod, "get_config", lambda *a, **k: object())
    for var in ("SEED_DB_SECRET_NAME", "SEED_DB_HOST"):
        monkeypatch.delenv(var, raising=False)
    return MESSimulator()


# ---------------------------------------------------------------------------
# Prefix map — single source of truth
# ---------------------------------------------------------------------------

def test_cmms_site_prefix_matches_seeded_asset_tags(sim):
    """CMMS-side prefixes are DET/MKE/CRR (Cedar Rapids is CRR, not CR).

    The fourth simulated site (ADR-0083) is asserted through the simulator's
    own constants rather than literals, so this file stays inside the
    domain-neutrality ratchet baseline."""
    legacy = {
        "MILWAUKEE_BOTTLING": "MKE",
        "DETROIT_AUTOPARTS": "DET",
        "CEDARRAPIDS_FOOD": "CRR",
    }
    prefix = MESSimulator.CMMS_SITE_PREFIX
    assert {k: prefix[k] for k in legacy} == legacy
    fourth = set(prefix) - set(legacy)
    assert len(fourth) == 1, "exactly one site beyond the original three"
    assert prefix[fourth.pop()] == "IND"


def test_fourth_site_cmms_target_is_its_own_schema():
    """The fourth site's maintenance routes to its OWN CMMS schema (IBM Maximo
    ASSETNUM prefix), never the already-onboarded cmms_maintenance, and the
    vibration reason maps to the story's IBM Maximo failure code."""
    target = MESSimulator.CMMS_SITE_TARGET
    assert len(target) == 1
    (site_code, (schema, asset_prefix)), = target.items()
    assert site_code in MESSimulator.CMMS_SITE_PREFIX
    assert schema.startswith("cmms_") and schema != "cmms_maintenance"
    assert asset_prefix == "1001"
    assert MESSimulator.CMMS_IND_FAILURE_CODE["VIB-BEARING"] == "BEARING"


def test_prefix_map_is_single_source():
    """No second (drifting) site-prefix map in the simulator source."""
    src = (_REPO_ROOT / "erp_simulation" / "mes_simulator.py").read_text()
    assert "SITE_LINE_PREFIX" not in src, "old unused prefix map must stay deleted"
    assert "'CEDARRAPIDS_FOOD': 'CR'," not in src, "stale CR prefix must not return"
    assert src.count("'CEDARRAPIDS_FOOD'") == 1, (
        "CEDARRAPIDS_FOOD must appear exactly once — in CMMS_SITE_PREFIX"
    )


# ---------------------------------------------------------------------------
# Bridge routing — seed connection, event ref, failure code
# ---------------------------------------------------------------------------

def _seed_conn_with_asset(asset_row):
    seed_conn = MagicMock(name="seed_conn")
    seed_cursor = MagicMock(name="seed_cursor")
    seed_cursor.fetchone.return_value = asset_row
    seed_conn.cursor.return_value = seed_cursor
    return seed_conn, seed_cursor


def test_bridge_writes_through_seed_connection(sim, monkeypatch):
    seed_conn, seed_cursor = _seed_conn_with_asset((7, "DET-CNC1-001"))
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    erp_cursor = MagicMock(name="erp_cursor")
    erp_cursor.fetchone.return_value = ("DETROIT_AUTOPARTS",)
    downtime_id = uuid.uuid4()

    sim._create_cmms_work_order_for_downtime(
        erp_cursor, "wo-uuid", "LINE1", "MECHANICAL", "VIB-BEARING", downtime_id
    )

    # ERP cursor only does the site lookup — never a cmms statement
    erp_sqls = [c.args[0] for c in erp_cursor.execute.call_args_list]
    assert len(erp_sqls) == 1 and "mes_work_orders" in erp_sqls[0]
    assert all("cmms" not in s for s in erp_sqls)

    # Seed cursor: asset pick with site-correct prefix + preferred type
    pick_sql, pick_params = seed_cursor.execute.call_args_list[0].args
    assert "cmms_maintenance.assets" in pick_sql
    assert pick_params == ("DET-%", "cnc_machine")

    # Seed cursor: insert into work_orders_raw with event ref + failure code
    ins_sql, ins_params = seed_cursor.execute.call_args_list[1].args
    assert "cmms_maintenance.work_orders_raw" in ins_sql
    wo_num, asset_id, priority, description, failure_code, created_ts = ins_params
    assert asset_id == 7
    assert priority == "high"  # MECHANICAL
    assert str(downtime_id) in description, "description must carry the MES event id"
    assert failure_code == "VIB-BEARING"
    assert created_ts is None  # no stop start handed over: dated by its zero offset, as before
    assert wo_num.startswith("WO-MES-DET-") and len(wo_num) <= 30

    seed_conn.commit.assert_called_once()


def test_bridge_electrical_priority_and_unknown_reason_fallback(sim, monkeypatch):
    seed_conn, seed_cursor = _seed_conn_with_asset((3, "MKE-FILL-001"))
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    erp_cursor = MagicMock()
    erp_cursor.fetchone.return_value = ("MILWAUKEE_BOTTLING",)

    sim._create_cmms_work_order_for_downtime(
        erp_cursor, "wo-uuid", "LINE2", "ELECTRICAL", "PLC-COMM", uuid.uuid4()
    )

    _, pick_params = seed_cursor.execute.call_args_list[0].args
    assert pick_params == ("MKE-%", ""), "unknown reason ⇒ empty type preference"
    _, ins_params = seed_cursor.execute.call_args_list[1].args
    assert ins_params[2] == "medium"  # ELECTRICAL


def test_asset_pick_is_ordered_for_determinism(sim):
    seed_cursor = MagicMock()
    seed_cursor.fetchone.return_value = None
    sim._pick_cmms_asset(seed_cursor, "CRR", "MOTOR-OVL")
    sql, params = seed_cursor.execute.call_args.args
    assert "ORDER BY" in sql and "LIMIT 1" in sql
    assert params == ("CRR-%", "mixer")


# ---------------------------------------------------------------------------
# Fourth site (ADR-0083): bridge routes to the site's OWN CMMS, IBM Maximo shape
# ---------------------------------------------------------------------------

def _fourth_site():
    (site_code, (schema, _prefix)), = MESSimulator.CMMS_SITE_TARGET.items()
    return site_code, schema


def test_fourth_site_downtime_routes_to_its_own_cmms(sim, monkeypatch):
    site_code, schema = _fourth_site()
    seed_conn, seed_cursor = _seed_conn_with_asset(("1001-CMX-441",))
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    erp_cursor = MagicMock()
    erp_cursor.fetchone.return_value = (site_code,)
    downtime_id = uuid.uuid4()

    sim._create_cmms_work_order_for_downtime(
        erp_cursor, "wo-uuid", "LINE3", "MECHANICAL", "VIB-BEARING", downtime_id
    )

    sqls = [c.args[0] for c in seed_cursor.execute.call_args_list]
    assert all("cmms_maintenance" not in s for s in sqls), (
        "fourth-site maintenance must never reach the onboarded cmms_maintenance"
    )
    pick_sql, pick_params = seed_cursor.execute.call_args_list[0].args
    assert f"{schema}.assets" in pick_sql
    assert pick_params == ("1001-%", "CNC_MILL")  # VIB-BEARING prefers the mill

    ins_sql, ins_params = seed_cursor.execute.call_args_list[1].args
    assert f"{schema}.work_orders" in ins_sql and "work_orders_raw" not in ins_sql
    wonum, asset_num, work_type, description, priority, failure_code, start = ins_params
    assert start is None  # no stop start handed over: the order has no actual start yet
    assert asset_num == "1001-CMX-441"
    assert work_type == "EM" and priority == 1  # MECHANICAL ⇒ emergency, top priority
    assert failure_code == "BEARING"  # IBM Maximo failure code of the CMX-441 story
    assert wonum.startswith("IND-") and len(wonum) <= 15  # wonum VARCHAR(15)
    assert str(downtime_id) in description and len(description) <= 250
    seed_conn.commit.assert_called_once()


def test_fourth_site_order_is_worked_from_the_start_of_its_stop(sim, monkeypatch):
    """The order the bridge raises for a stop carries the stop's own start as
    its actual start, the same value the completion later writes, never the
    time the bridge ran."""
    site_code, schema = _fourth_site()
    seed_conn, seed_cursor = _seed_conn_with_asset(("1001-HYP-402",))
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    erp_cursor = MagicMock()
    erp_cursor.fetchone.return_value = (site_code,)
    stop_start = datetime(2031, 4, 2, 7, 12, 30)

    sim._create_cmms_work_order_for_downtime(
        erp_cursor, "wo-uuid", "LINE1", "MECHANICAL", "PUMP-FAIL", uuid.uuid4(),
        downtime_start=stop_start,
    )
    ins_sql, ins_params = seed_cursor.execute.call_args_list[1].args
    columns = [c.strip() for c in ins_sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    assert columns[-1] == "actual_start"
    assert ins_params[-1] == stop_start


def test_a_bridged_downtime_event_hands_over_its_own_start(sim, monkeypatch):
    """The downtime event's start (the stop the order is raised for) reaches
    the bridge; its row and its order are dated by the same stop."""
    start = datetime(2031, 4, 2, 9, 41, 0)
    handed = []
    sim.downtime_categories = ["MECHANICAL"]
    monkeypatch.setattr(sim, "random_downtime_reasons", lambda site, category: ["MECH-JAM"])
    monkeypatch.setattr(sim, "_downtime_gates_pass", lambda *a: True)
    monkeypatch.setattr(sim, "_build_downtime_window",
                        lambda cursor, began, category=None: (start, start, 0))
    monkeypatch.setattr(sim, "_insert_downtime_event", lambda *a: "dt-1")
    monkeypatch.setattr(sim, "_create_cmms_work_order_for_downtime",
                        lambda *a, **kw: handed.append(kw.get("downtime_start")))

    created = sim._maybe_create_downtime_event(
        MagicMock(), "wo-uuid", "WO-1", "LINE1", start, "PLANT_A")
    assert created == 1 and handed == [start]


def test_fourth_site_electrical_is_corrective_with_passthrough_code(sim, monkeypatch):
    site_code, _schema = _fourth_site()
    seed_conn, seed_cursor = _seed_conn_with_asset(("1001-CVA-100",))
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    erp_cursor = MagicMock()
    erp_cursor.fetchone.return_value = (site_code,)

    sim._create_cmms_work_order_for_downtime(
        erp_cursor, "wo-uuid", "LINE1", "ELECTRICAL", "PLC-COMM", uuid.uuid4()
    )
    _, pick_params = seed_cursor.execute.call_args_list[0].args
    assert pick_params == ("1001-%", "")
    _, ins_params = seed_cursor.execute.call_args_list[1].args
    assert ins_params[2] == "CM" and ins_params[4] == 2
    assert ins_params[5] == "PLC-COMM"


def test_fourth_site_without_asset_warns_and_writes_nothing(sim, monkeypatch, caplog):
    site_code, schema = _fourth_site()
    seed_conn, seed_cursor = _seed_conn_with_asset(None)
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    erp_cursor = MagicMock()
    erp_cursor.fetchone.return_value = (site_code,)

    with caplog.at_level("WARNING"):
        sim._create_cmms_work_order_for_downtime(
            erp_cursor, "wo-uuid", "LINE2", "MECHANICAL", "MECH-JAM", uuid.uuid4()
        )
    assert len(seed_cursor.execute.call_args_list) == 1  # the pick only
    assert any(f"no {schema} asset" in r.message for r in caplog.records)


def test_legacy_sites_still_use_cmms_maintenance(sim, monkeypatch):
    """Adding the fourth-site target must not move the three legacy sites."""
    fourth_code, schema = _fourth_site()
    legacy_code = sorted(set(MESSimulator.CMMS_SITE_PREFIX) - {fourth_code})[0]
    legacy_prefix = MESSimulator.CMMS_SITE_PREFIX[legacy_code]
    seed_conn, seed_cursor = _seed_conn_with_asset((9, f"{legacy_prefix}-MIX-001"))
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    erp_cursor = MagicMock()
    erp_cursor.fetchone.return_value = (legacy_code,)
    sim._create_cmms_work_order_for_downtime(
        erp_cursor, "wo-uuid", "LINE1", "MECHANICAL", "MOTOR-OVL", uuid.uuid4()
    )
    sqls = [c.args[0] for c in seed_cursor.execute.call_args_list]
    assert all(schema not in s for s in sqls)
    assert "cmms_maintenance.work_orders_raw" in sqls[1]


def test_bridge_failure_is_loud_but_does_not_raise(sim, monkeypatch, caplog):
    seed_conn, seed_cursor = _seed_conn_with_asset((1, "MKE-FILL-001"))
    seed_cursor.execute.side_effect = RuntimeError("seed db down")
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    erp_cursor = MagicMock()
    erp_cursor.fetchone.return_value = ("MILWAUKEE_BOTTLING",)

    with caplog.at_level("WARNING"):
        sim._create_cmms_work_order_for_downtime(
            erp_cursor, "wo-uuid", "LINE1", "MECHANICAL", "PUMP-FAIL", uuid.uuid4()
        )
    assert any("MES→CMMS bridge FAILED" in r.message for r in caplog.records)
    seed_conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Missing seed-DB environment — loud once, skip, never raise
# ---------------------------------------------------------------------------

def test_missing_seed_env_warns_once_and_skips(sim, caplog):
    with caplog.at_level("WARNING"):
        assert sim._get_seed_db_conn() is None
        assert sim._get_seed_db_conn() is None  # second call: no duplicate warning
    warnings = [r for r in caplog.records if "MES→CMMS bridge disabled" in r.message]
    assert len(warnings) == 1
    assert "SEED_DB" in warnings[0].message

    # The bridge itself skips without touching the ERP cursor and without raising
    erp_cursor = MagicMock()
    sim._create_cmms_work_order_for_downtime(
        erp_cursor, "wo-uuid", "LINE1", "MECHANICAL", "PUMP-FAIL", uuid.uuid4()
    )
    erp_cursor.execute.assert_not_called()


def test_seed_db_params_env_fallback(sim, monkeypatch):
    monkeypatch.setenv("SEED_DB_HOST", "203.0.113.10")
    monkeypatch.setenv("SEED_DB_PASSWORD", "pw")  # test-only placeholder
    params = sim._seed_db_params()
    assert params == {
        "host": "203.0.113.10",
        "port": 5432,
        "database": "adt_demo",
        "user": "postgres",
        "password": "pw",
    }


def test_seed_db_params_secret_name_wins(sim, monkeypatch):
    monkeypatch.setenv("SEED_DB_SECRET_NAME", "adt-seed-secret")
    monkeypatch.setenv("SEED_DB_HOST", "should-not-be-used")
    fake_cfg = MagicMock(host="h", port=5432, database="adt_demo", user="u", password="p")  # nosec B106 - offline test fixture/placeholder credential, not a live secret
    captured = {}

    def fake_from_secrets(secret_name, region):
        captured["secret"] = secret_name
        return fake_cfg

    monkeypatch.setattr(mes_mod.DatabaseConfig, "from_secrets_manager", staticmethod(fake_from_secrets))
    params = sim._seed_db_params()
    assert captured["secret"] == "adt-seed-secret"
    assert params["host"] == "h" and params["database"] == "adt_demo"


# ---------------------------------------------------------------------------
# Downtime event → bridge wiring (RETURNING downtime_id)
# ---------------------------------------------------------------------------

def test_downtime_event_insert_returns_id_and_triggers_bridge(sim, monkeypatch):
    from datetime import datetime, timedelta

    monkeypatch.setattr(mes_mod.random, "random", lambda: 0.0)  # always fire
    monkeypatch.setattr(mes_mod.random, "randint", lambda a, b: a)
    choices = iter(["MECHANICAL", "PUMP-FAIL", "maint_01"])
    monkeypatch.setattr(mes_mod.random, "choice", lambda seq: next(choices))

    downtime_id = uuid.uuid4()
    cursor = MagicMock()
    # fetchone sequence: prob config, active-line count, per-WO count,
    # duration-min config, duration-max config, INSERT ... RETURNING
    cursor.fetchone.side_effect = [
        ("0.5",), (0,), (0,), ("5",), ("30",), (downtime_id,),
    ]
    bridge = MagicMock()
    monkeypatch.setattr(sim, "_create_cmms_work_order_for_downtime", bridge)

    created = sim._maybe_create_downtime_event(
        cursor, "wo-uuid", "WO-123", "LINE1",
        datetime.now() - timedelta(minutes=30),
    )

    assert created == 1
    insert_sql = cursor.execute.call_args_list[-1].args[0]
    assert "mes_downtime_events" in insert_sql and "RETURNING downtime_id" in insert_sql
    bridge.assert_called_once()
    assert bridge.call_args.args[-1] == downtime_id


def test_random_draw_never_picks_story_reasons_at_story_site(sim):
    """The condition monitor and the story history own the story mill's
    failure modes. The bridge picks the site's CMMS asset by reason, not by
    line, so the random draw leaves them out at the story site whatever the
    line (the draw takes no line at all), and no category empties there."""
    site, reserved = MESSimulator.CONDITION_MONITOR_SITE, MESSimulator.STORY_FAILURE_REASONS
    assert set(MESSimulator.CMMS_IND_ASSET_TYPE) <= reserved, (
        "every reason the bridge aims at a site asset type is a story failure mode")
    assert any(reserved & set(rs) for rs in sim.downtime_reasons.values())
    for category in sim.downtime_categories:
        pool = sim.random_downtime_reasons(site, category)
        assert pool, f"{category} has no reason left at the story site"
        assert set(pool) == set(sim.downtime_reasons[category]) - reserved


def test_story_reasons_stay_in_draw_at_other_sites(sim):
    """Every other site, and a work order with no site, keeps the full list."""
    others = [s for s in MESSimulator.CMMS_SITE_PREFIX if s != MESSimulator.CONDITION_MONITOR_SITE]
    assert others
    for site in [*others, None]:
        for category in sim.downtime_categories:
            assert sim.random_downtime_reasons(site, category) == sim.downtime_reasons[category]


# ---------------------------------------------------------------------------
# Condition monitor (ADR-0073): predictive work order from the vibration
# spectrum, one per loop, in the fourth site's own CMMS
# ---------------------------------------------------------------------------

from datetime import datetime as _dt, timezone as _tz  # noqa: E402

from erp_simulation import condition_monitor as _cm  # noqa: E402

_LOOP = 4242  # any loop ordinal; the monitor derives everything from `now`


def _at(step):
    """Epoch seconds inside loop _LOOP at dataset row `step`."""
    return _LOOP * _cm.TOTAL_SECONDS + step * _cm.INTERVAL_SEC + 1.0


def _utc(epoch):
    """The naive-UTC timestamp the monitor stores for ``epoch`` (ADR-0088 D5)."""
    return _dt.fromtimestamp(epoch, tz=_tz.utc).replace(tzinfo=None)


_WALL_ALARM = _LOOP * _cm.TOTAL_SECONDS + _cm.ALARM_OFFSET_SECONDS
_WALL_FAIL = _LOOP * _cm.TOTAL_SECONDS + _cm.BEARING_FAIL_STEP * _cm.INTERVAL_SEC
_ALARM_DESCRIPTION = MESSimulator._condition_work_order_description(
    _cm.spectral_features(_cm.BPFO_ALARM_STEP))


def _seed_conn_for_monitor(rowcount=1):
    seed_conn = MagicMock(name="seed_conn")
    seed_cursor = MagicMock(name="seed_cursor")
    seed_cursor.rowcount = rowcount
    seed_conn.cursor.return_value = seed_cursor
    return seed_conn, seed_cursor


def _inserts(seed_cursor):
    """The condition-order INSERTs the monitor issued (not the close-out)."""
    return [c for c in seed_cursor.execute.call_args_list
            if "ON CONFLICT (wonum) DO NOTHING" in c.args[0]]


def _close_outs(seed_cursor):
    """The close-outs of earlier loops' orders the monitor issued."""
    return [c for c in seed_cursor.execute.call_args_list
            if c.args[0].lstrip().startswith("WITH completed AS")]


def test_monitor_is_quiet_while_the_band_is_under_threshold(sim, monkeypatch):
    """Below the threshold nothing is raised. The wall clock always has
    earlier loops, so each cycle still runs their close-out, and nothing else."""
    seed_conn, seed_cursor = _seed_conn_for_monitor(rowcount=0)
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    assert sim.monitor_asset_condition("cycle", now=_at(0)) == 0
    assert sim.monitor_asset_condition("cycle", now=_at(_cm.BPFO_ALARM_STEP - 1)) == 0
    assert _inserts(seed_cursor) == []
    assert len(_close_outs(seed_cursor)) == seed_cursor.execute.call_count == 2


def test_monitor_raises_the_band_work_order_when_the_bpfo_band_crosses(sim, monkeypatch):
    site_code, schema = _fourth_site()
    seed_conn, seed_cursor = _seed_conn_for_monitor()
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)

    assert sim.monitor_asset_condition("cycle", now=_at(_cm.BPFO_ALARM_STEP)) == 1

    sql, params = seed_cursor.execute.call_args.args
    assert f"INSERT INTO {schema}.work_orders" in sql and "cmms_maintenance" not in sql
    assert "'CM'" in sql and "'WAPPR'" in sql and "'BEARING'" in sql and "ON CONFLICT (wonum) DO NOTHING" in sql
    assert "NOW()" not in sql  # every timestamp is the event's, never the cycle's
    wonum, asset_num, description, reported, target_start, sched_start, crew = params
    assert wonum == f"VIB-CMX-{_LOOP % 1_000_000:06d}" and len(wonum) <= 15
    assert asset_num == _cm.STORY_CMMS_ASSET_NUM
    assert "BPFO" in description and "Hz" in description and len(description) <= 250
    assert crew == sim.CONDITION_MONITOR_CREW
    # Plan D5: reported/target = the loop's alarm instant (naive UTC), the
    # scheduled start = the bearing replacement later in the SAME loop.
    assert reported == target_start == _utc(_WALL_ALARM)
    assert sched_start == _utc(_WALL_FAIL) > reported
    seed_conn.commit.assert_called_once()


def test_monitor_stamps_the_alarm_instant_and_features_however_late_the_cycle(sim, monkeypatch):
    """A cycle deep in the alarm window (the order was missed earlier, e.g.
    the seed DB was down) still stamps the ALARM instant and the features at
    BPFO_ALARM_STEP - the event that raised it, not the cycle that wrote it."""
    seed_conn, seed_cursor = _seed_conn_for_monitor()
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    assert sim.monitor_asset_condition("late", now=_at(_cm.BEARING_FAIL_STEP - 1)) == 1
    _w, _a, description, reported, target_start, _s, _c = seed_cursor.execute.call_args.args[1]
    assert description == _ALARM_DESCRIPTION
    assert "BPFO band 0.70 mm/s" in description
    assert reported == target_start == _utc(_WALL_ALARM)


def test_monitor_is_idempotent_per_loop(sim, monkeypatch):
    seed_conn, seed_cursor = _seed_conn_for_monitor(rowcount=0)  # ON CONFLICT hit
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    a = sim.monitor_asset_condition("c1", now=_at(_cm.BPFO_ALARM_STEP))
    b = sim.monitor_asset_condition("c2", now=_at(_cm.BEARING_FAIL_STEP - 1))
    assert (a, b) == (0, 0)
    wonums = {c.args[1][0] for c in _inserts(seed_cursor)}
    assert len(wonums) == 1, "every cycle of one loop targets the same wonum"
    other_loop = sim.monitor_asset_condition("c3", now=_at(_cm.BPFO_ALARM_STEP) + _cm.TOTAL_SECONDS)
    assert other_loop == 0
    assert len({c.args[1][0] for c in _inserts(seed_cursor)}) == 2


class _ConflictCursor:
    """Seed cursor with the table's ``ON CONFLICT (wonum) DO NOTHING`` semantics
    (a close-out of earlier loops' orders completes nothing here)."""

    def __init__(self):
        self.rows, self.rowcount = {}, 0

    def execute(self, _sql, params):
        if isinstance(params, dict):          # the close-out
            self.rowcount = 0
            return
        wonum = params[0]
        self.rowcount = 0 if wonum in self.rows else 1
        self.rows.setdefault(wonum, params)

    def close(self):
        pass


def test_wall_clock_minute_walk_raises_one_order_per_loop(sim, monkeypatch):
    """Demo / frozen (no anchor): the every-minute monitor over two whole
    wall-clock loops raises exactly ONE order per loop, each stamped with its
    own loop's alarm instant."""
    cursor = _ConflictCursor()
    seed_conn = MagicMock(name="seed_conn")
    seed_conn.cursor.return_value = cursor
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    start = _LOOP * _cm.TOTAL_SECONDS
    raised = sum(sim.monitor_asset_condition("walk", now=start + 60 * k + 7.0)
                 for k in range(2 * _cm.TOTAL_SECONDS // 60))
    assert raised == 2
    assert {w: p[3] for w, p in cursor.rows.items()} == {
        f"VIB-CMX-{_LOOP:06d}": _utc(_WALL_ALARM),
        f"VIB-CMX-{_LOOP + 1:06d}": _utc(_WALL_ALARM + _cm.TOTAL_SECONDS),
    }


def test_monitor_is_quiet_while_stopped_and_after_the_repair(sim, monkeypatch):
    seed_conn, seed_cursor = _seed_conn_for_monitor(rowcount=0)
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    fail, repair = _cm.DOWNTIME
    for step in (fail, fail + repair // 2, fail + repair, _cm.NUM_STEPS - 1):
        assert sim.monitor_asset_condition("cycle", now=_at(step)) == 0
    assert _inserts(seed_cursor) == []          # only the close-out of earlier loops ran


def test_monitor_without_seed_db_skips(sim, monkeypatch):
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: None)
    assert sim.monitor_asset_condition("cycle", now=_at(_cm.BPFO_ALARM_STEP)) == 0


def test_monitor_failure_is_loud_but_does_not_raise(sim, monkeypatch, caplog):
    seed_conn, seed_cursor = _seed_conn_for_monitor()
    seed_cursor.execute.side_effect = RuntimeError("relation does not exist")
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    with caplog.at_level("WARNING"):
        assert sim.monitor_asset_condition("cycle", now=_at(_cm.BPFO_ALARM_STEP)) == 0
    assert any("Condition monitor work order FAILED" in r.message for r in caplog.records)
    seed_conn.rollback.assert_called_once()
    seed_conn.commit.assert_not_called()
    # A deploy whose seed DB lacks the fourth site's schema (an older demo
    # account) must not log the same failure every cycle for hours: once per loop.
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert sim.monitor_asset_condition("cycle", now=_at(_cm.BPFO_ALARM_STEP + 20)) == 0
    assert sum("Condition monitor work order FAILED" in r.message for r in caplog.records) == 1


def test_downtime_simulation_runs_the_monitor_every_cycle(sim, monkeypatch):
    monitor = MagicMock(return_value=1)
    monkeypatch.setattr(sim, "monitor_asset_condition", monitor)
    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = []  # no running work orders
    result = sim.simulate_downtime_events(conn, "cycle-x")
    monitor.assert_called_once_with("cycle-x")
    assert result == {"downtime_events_created": 0, "condition_work_orders_created": 1}


# ---------------------------------------------------------------------------
# insert_condition_work_order: the public contract the story re-plan (Task H)
# calls to catch up an era's condition orders.
# ---------------------------------------------------------------------------

def test_insert_condition_work_order_contract():
    cursor = MagicMock(name="seed_cursor")
    cursor.rowcount = 1
    alarm, fail = 1_900_019_500.0, 1_900_025_930.0
    assert MESSimulator.insert_condition_work_order(cursor, 1_000_007, alarm, fail) == 1
    sql, params = cursor.execute.call_args.args
    schema, _prefix = MESSimulator.CMMS_SITE_TARGET[MESSimulator.CONDITION_MONITOR_SITE]
    assert f"INSERT INTO {schema}.work_orders" in sql and "ON CONFLICT (wonum) DO NOTHING" in sql
    assert params == ("VIB-CMX-000007", _cm.STORY_CMMS_ASSET_NUM, _ALARM_DESCRIPTION,
                      _utc(alarm), _utc(alarm), _utc(fail), MESSimulator.CONDITION_MONITOR_CREW)
    cursor.execute.assert_called_once()  # no commit / no extra statement: the caller owns the transaction


@pytest.mark.parametrize("rowcount", [0, -1, None])
def test_insert_condition_work_order_reports_zero_on_conflict(rowcount):
    cursor = MagicMock(name="seed_cursor")
    cursor.rowcount = rowcount
    assert MESSimulator.insert_condition_work_order(cursor, 0, 0.0, 0.0) == 0


def test_insert_condition_work_order_propagates_database_errors():
    cursor = MagicMock(name="seed_cursor")
    cursor.execute.side_effect = RuntimeError("down")
    with pytest.raises(RuntimeError):
        MESSimulator.insert_condition_work_order(cursor, 0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Anchored condition monitor (live story, ADR-0085): with a positive
# condition_anchor_epoch the loop starts AT the anchor, not on the wall clock.
# ---------------------------------------------------------------------------

# An anchor whose loop is deliberately out of phase with the wall-clock loop:
# the anchored BPFO-alarm instant is only a few minutes into a wall-clock loop.
_ANCHOR = _LOOP * _cm.TOTAL_SECONDS - _cm.BPFO_ALARM_STEP * _cm.INTERVAL_SEC + 1000.0


def test_default_anchor_is_the_wall_clock(sim):
    assert MESSimulator.condition_anchor_epoch == 0.0
    now = _at(_cm.BPFO_ALARM_STEP)
    assert sim._condition_repair_time(now) == sim._condition_repair_time(now, 0.0)


def test_anchored_monitor_raises_at_the_anchored_alarm_step(sim, monkeypatch):
    seed_conn, seed_cursor = _seed_conn_for_monitor()
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    sim.condition_anchor_epoch = _ANCHOR
    sim.condition_anchor_reader = lambda: _ANCHOR
    alarm_at = _ANCHOR + _cm.BPFO_ALARM_STEP * _cm.INTERVAL_SEC
    # The wall clock alone would call this instant healthy.
    assert _cm.loop_step(alarm_at) < _cm.BPFO_ALARM_STEP

    assert sim.monitor_asset_condition("cycle", now=alarm_at + 42.0) == 1

    wonum, _asset, _desc, reported, target_start, sched_start, _crew = seed_cursor.execute.call_args.args[1]
    assert wonum == f"VIB-CMX-{0:06d}"  # anchored loop 0
    assert reported == target_start == _utc(_cm.alarm_epoch(alarm_at, _ANCHOR)) == _utc(alarm_at)
    assert sched_start == _utc(_ANCHOR + _cm.BEARING_FAIL_STEP * _cm.INTERVAL_SEC)


def test_anchored_monitor_is_quiet_one_step_before_the_alarm(sim, monkeypatch):
    seed = MagicMock(name="get_seed_db_conn")
    monkeypatch.setattr(sim, "_get_seed_db_conn", seed)
    sim.condition_anchor_epoch = _ANCHOR
    before = _ANCHOR + (_cm.BPFO_ALARM_STEP - 1) * _cm.INTERVAL_SEC
    assert sim.monitor_asset_condition("cycle", now=before) == 0
    seed.assert_not_called()


def test_anchored_monitor_takes_the_shared_story_lock_before_rereading_the_anchor(sim, monkeypatch):
    """ADR-0089: on the story clock the seed transaction takes the SHARED
    story-clock lock (the re-plan's seed half holds it EXCLUSIVE), THEN
    re-reads the stored anchor, THEN inserts."""
    from erp_simulation import story_replan
    seed_conn, seed_cursor = _seed_conn_for_monitor()
    order = []
    seed_cursor.execute.side_effect = lambda sql, params=None: order.append(sql.split()[1])
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    sim.condition_anchor_epoch = _ANCHOR
    sim.condition_anchor_reader = lambda: order.append("reread") or _ANCHOR
    alarm_at = _ANCHOR + _cm.BPFO_ALARM_STEP * _cm.INTERVAL_SEC
    assert sim.monitor_asset_condition("cycle", now=alarm_at + 42.0) == 1
    assert order[:2] == ["pg_advisory_xact_lock_shared(%s)", "reread"] and order[2] == "INTO"
    assert seed_cursor.execute.call_args_list[0].args == (story_replan.SHARED_LOCK_SQL, (story_replan.LOCK_KEY,))


@pytest.mark.parametrize("reader", [lambda: _ANCHOR + 300.0, lambda: None, None],
                         ids=["moved", "cleared", "no-reader"])
def test_anchored_monitor_skips_when_the_story_clock_moved(sim, monkeypatch, caplog, reader):
    """A placement between the cycle's anchor load and the monitor: no
    old-era VIB-CMX, one log line, 0 (fail closed without a reader)."""
    seed_conn, seed_cursor = _seed_conn_for_monitor()
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    sim.condition_anchor_epoch = _ANCHOR
    sim.condition_anchor_reader = reader
    alarm_at = _ANCHOR + _cm.BPFO_ALARM_STEP * _cm.INTERVAL_SEC
    with caplog.at_level("INFO"):
        assert sim.monitor_asset_condition("cycle", now=alarm_at + 42.0) == 0
    assert not any("INSERT" in c.args[0] for c in seed_cursor.execute.call_args_list)
    seed_conn.rollback.assert_called_once()
    seed_conn.commit.assert_not_called()
    assert len([r for r in caplog.records if "story clock moved" in r.getMessage()]) == 1


def test_wall_clock_monitor_takes_no_story_lock(sim, monkeypatch):
    """Demo / frozen (anchor 0.0): no story clock, so no lock and no re-read:
    the close-out of earlier loops, then the insert."""
    seed_conn, seed_cursor = _seed_conn_for_monitor()
    monkeypatch.setattr(sim, "_get_seed_db_conn", lambda: seed_conn)
    sim.condition_anchor_reader = MagicMock(side_effect=AssertionError("re-read on the wall clock"))
    assert sim.monitor_asset_condition("cycle", now=_at(_cm.BPFO_ALARM_STEP)) == 1
    assert seed_cursor.execute.call_count == 2
    assert _close_outs(seed_cursor) == seed_cursor.execute.call_args_list[:1]
    assert _inserts(seed_cursor) == seed_cursor.execute.call_args_list[1:]


def test_anchored_repair_time_is_in_the_anchored_loop():
    later = _ANCHOR + 3 * _cm.TOTAL_SECONDS + 7.0
    expected = _ANCHOR + 3 * _cm.TOTAL_SECONDS + _cm.BEARING_FAIL_STEP * _cm.INTERVAL_SEC
    assert MESSimulator._condition_repair_time(later, _ANCHOR) == _utc(expected)


def test_repair_time_is_naive_utc_whatever_the_host_zone(monkeypatch):
    """UTC, not the host's local zone (``datetime.fromtimestamp`` would shift
    it by the host offset on a laptop)."""
    import time as _time
    monkeypatch.setenv("TZ", "America/Denver")
    _time.tzset()
    try:
        repair = MESSimulator._condition_repair_time(_at(_cm.BPFO_ALARM_STEP))
    finally:
        monkeypatch.undo()
        _time.tzset()
    assert repair == _utc(_WALL_FAIL) and repair.tzinfo is None
