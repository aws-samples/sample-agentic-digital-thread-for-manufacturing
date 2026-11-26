# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-site cycle metrics (``simulation_events``) and the dashboard know every
MES site, including the fourth (machining) site added by ADR-0072.

Characterization tests (written first, green on the pre-change HEAD) pin the
legacy three sites: the ``_record_simulation_event`` column -> value mapping,
the sparkline metric keys, and the full Streamlit call trace of
``ERPDashboard.render`` (a JSON golden per scenario in
``fixtures/dashboard_render_traces.json``, captured from the pre-split render,
so the render decomposition is a pure extraction). Streamlit, pandas and
plotly are faked; the dashboard module is loaded from its path with the fakes
in place. The fake ``st.rerun`` raises, as Streamlit's does, so a trace stops
where the real script run stops.

Golden maintenance (a deliberate render change only, with a reviewed reason):

    ERP_SIM_REGEN_RENDER_GOLDEN=1 python -m pytest erp_simulation/tests/test_simulation_event_sites.py -q -k render_call_trace

``ERP_SIM_TEST_DASHBOARD_PATH`` renders another copy of the dashboard (e.g. the
pre-split ``git show`` of the file) against the same golden.

The PostgreSQL acceptance (skipped unless ``ERP_SIM_TEST_PG_DSN`` is set) runs
real ``_record_simulation_event`` cycles against the lambda-db-init schema.

    python -m pytest erp_simulation/tests/test_simulation_event_sites.py -q
"""
from __future__ import annotations

import ast
import difflib
import importlib.util
import io
import json
import logging
import os
import re
import sys
import types
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from erp_simulation import console_login as dash_console_login
from erp_simulation import simulation_orchestrator as orch_mod
from erp_simulation import sites as sites_mod
from erp_simulation import sql_console as dash_sql_console
from erp_simulation.tests._pg_sql import stmt

_SIM_DIR = Path(orch_mod.__file__).parent
_DASHBOARD_PATH = Path(os.environ.get("ERP_SIM_TEST_DASHBOARD_PATH") or _SIM_DIR / "streamlit_dashboard.py")
_GOLDEN_PATH = Path(__file__).with_name("fixtures") / "dashboard_render_traces.json"
_DDL_PATH = _SIM_DIR / "schema" / "11_simulation_events.sql"
_LEGACY_SITES = ("MILWAUKEE_BOTTLING", "DETROIT_AUTOPARTS", "CEDARRAPIDS_FOOD")
_IND_COLUMNS = ("mes_ind_scheduled", "mes_ind_running", "mes_ind_completed")


def _ddl_columns():
    body = _DDL_PATH.read_text(encoding="utf-8").split("CREATE TABLE IF NOT EXISTS simulation_events (")[1]
    return re.findall(r"^\s+([a-z_]+)\s+[A-Z]", body.split(");")[0], re.MULTILINE)


@pytest.fixture(autouse=True)
def _event_columns_known():
    """Offline cycles see the full DDL table (as a fresh db-init creates it)."""
    orch_mod._reset_event_columns_cache(_ddl_columns())
    yield
    orch_mod._reset_event_columns_cache()


# --------------------------------------------------------------------------
# _record_simulation_event
# --------------------------------------------------------------------------

class _EventCursor:
    def __init__(self, site_rows, table_columns=None):
        self.calls = []
        self._site_rows = site_rows
        self._table_columns = table_columns

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (7,)

    def fetchall(self):
        if "information_schema.columns" in self.calls[-1][0]:
            return [(c,) for c in self._table_columns]
        return list(self._site_rows)


class _EventConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


class _EventDB:
    def __init__(self, conn):
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


def _metrics():
    keys = (
        "orders_created", "orders_pending_to_confirmed", "orders_confirmed_to_ready",
        "orders_confirmed_to_backordered", "shipments_created", "production_runs_created",
        "production_planned_to_released", "mes_work_orders_created", "production_runs_completed",
        "pos_created", "pos_in_transit", "pos_received", "shipping_preparing_to_picked",
        "shipping_picked_to_packed", "shipping_packed_to_shipped", "shipping_shipped_to_in_transit",
        "shipping_in_transit_to_delivered", "invoices_generated", "payments_recorded",
        "finance_revenue", "finance_collected", "mes_production_events_created",
        "mes_quality_checks_created",
    )
    return {key: (i + 1) * 10 for i, key in enumerate(keys)}


def _site_rows(sites):
    return [(site, 100 * (i + 1) + 1, 100 * (i + 1) + 2, 100 * (i + 1) + 3)
            for i, site in enumerate(sites)]


def _record(site_rows, table_columns=None):
    cursor = _EventCursor(site_rows, table_columns)
    conn = _EventConn(cursor)
    orch = orch_mod.SimulationOrchestrator.__new__(orch_mod.SimulationOrchestrator)
    orch.db = _EventDB(conn)
    orch._record_simulation_event(1, _metrics())
    return cursor, conn


def _inserted(cursor):
    sql, params = next(c for c in cursor.calls if c[0].startswith("INSERT INTO simulation_events"))
    columns = [c.strip() for c in re.search(r"\((.*?)\) VALUES", sql).group(1).split(",")]
    assert columns[0] == "event_timestamp" and re.search(r"VALUES \( ?NOW\(\),", sql)
    assert sql.count("%s") == len(params) == len(columns) - 1
    return dict(zip(columns[1:], params))


def _legacy_expected():
    m = _metrics()
    expected = {
        "cycle_number": 7,
        "orders_new": m["orders_created"],
        "orders_pending": m["orders_pending_to_confirmed"],
        "orders_confirmed": m["orders_confirmed_to_ready"] + m["orders_confirmed_to_backordered"],
        "orders_backordered": m["orders_confirmed_to_backordered"],
        "orders_shipped": m["shipments_created"],
        "orders_in_production": m["production_runs_created"],
        "production_new": m["production_runs_created"],
        "production_planned": m["production_runs_created"],
        "production_released": m["production_planned_to_released"],
        "production_in_progress": m["mes_work_orders_created"],
        "production_completed": m["production_runs_completed"],
        "procurement_sent": m["pos_created"],
        "procurement_in_transit": m["pos_in_transit"],
        "procurement_received": m["pos_received"],
        "shipping_preparing": m["shipments_created"],
        "shipping_picked": m["shipping_preparing_to_picked"],
        "shipping_packed": m["shipping_picked_to_packed"],
        "shipping_shipped": m["shipping_packed_to_shipped"],
        "shipping_in_transit": m["shipping_shipped_to_in_transit"],
        "shipping_delivered": m["shipping_in_transit_to_delivered"],
        "finance_invoices": m["invoices_generated"],
        "finance_paid": m["payments_recorded"],
        "finance_revenue": m["finance_revenue"],
        "finance_collected": m["finance_collected"],
        "mes_scheduled": m["mes_work_orders_created"],
        "mes_running": m["mes_production_events_created"],
        "mes_completed": m["mes_quality_checks_created"],
        "inventory_transactions": m["pos_received"] + m["production_runs_completed"],
    }
    for i, prefix in enumerate(("mke", "det", "cr")):
        for j, state in enumerate(("scheduled", "running", "completed")):
            expected[f"mes_{prefix}_{state}"] = 100 * (i + 1) + j + 1
    return expected


def test_characterization_legacy_event_columns_and_statement_order():
    cursor, conn = _record(_site_rows(_LEGACY_SITES))
    row = _inserted(cursor)
    expected = _legacy_expected()
    assert {k: row[k] for k in expected} == expected
    statements = [sql.split(" ")[0] for sql, _ in cursor.calls]
    assert statements == ["SELECT", "SELECT", "INSERT", "DELETE"]
    assert "FROM mes_work_orders" in cursor.calls[1][0] and "GROUP BY site" in cursor.calls[1][0]
    assert "INTERVAL '24 hours'" in cursor.calls[3][0]
    assert conn.commits == 1


def test_characterization_missing_sites_record_zero():
    row = _inserted(_record([])[0])
    assert all(row[f"mes_{p}_{s}"] == 0 for p in ("mke", "det", "cr")
               for s in ("scheduled", "running", "completed"))


def test_characterization_failure_is_swallowed():
    class _Boom:
        @contextmanager
        def connection(self):
            raise RuntimeError("db down")
            yield  # pragma: no cover

    orch = orch_mod.SimulationOrchestrator.__new__(orch_mod.SimulationOrchestrator)
    orch.db = _Boom()
    orch._record_simulation_event(1, {})  # must not raise


def test_site_event_prefix_keeps_legacy_sites_and_adds_the_fourth():
    assert list(orch_mod.SITE_EVENT_PREFIX)[:3] == list(_LEGACY_SITES)
    # The fourth built-in site (ADR-0072).
    assert list(orch_mod.SITE_EVENT_PREFIX)[3] == "INDIANAPOLIS_MACHINING"
    assert len(orch_mod.SITE_EVENT_PREFIX) == 4
    assert len(set(orch_mod.SITE_EVENT_PREFIX.values())) == 4


def test_insert_carries_every_site_including_the_fourth():
    sites = tuple(orch_mod.SITE_EVENT_PREFIX)
    row = _inserted(_record(_site_rows(sites))[0])
    for i, prefix in enumerate(orch_mod.SITE_EVENT_PREFIX.values()):
        if prefix in sites_mod.OPTIONAL_SITE_EVENT_PREFIXES:
            continue                       # not in the base DDL (see the next test)
        assert [row[f"mes_{prefix}_{s}"] for s in orch_mod.SITE_EVENT_STATES] == [
            100 * (i + 1) + 1, 100 * (i + 1) + 2, 100 * (i + 1) + 3]
    fourth = list(orch_mod.SITE_EVENT_PREFIX.values())[3]
    assert {f"mes_{fourth}_{s}" for s in orch_mod.SITE_EVENT_STATES} <= set(row)
    assert {k: row[k] for k in _legacy_expected()} == _legacy_expected()


def test_no_optional_site_event_prefixes_ship():
    """No optional plant ships in the sample, so there are no optional
    per-site event prefixes and the INSERT carries only the built-in sites."""
    assert sites_mod.OPTIONAL_SITE_EVENT_PREFIXES == frozenset()
    orch_mod._reset_event_columns_cache()
    with caplog.at_level(logging.WARNING, logger=orch_mod.logger.name):
        without = _inserted(_record(_site_rows(sites), _ddl_columns())[0])
    assert not set(optional) & set(without)
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_insert_columns_match_the_schema_ddl():
    """Every inserted column exists in the simulator DDL home (both homes are pinned by T3)."""
    ddl = _DDL_PATH.read_text(encoding="utf-8")
    row = _inserted(_record([])[0])
    missing = [c for c in row if not re.search(rf"\b{c}\b", ddl)]
    assert missing == []


def _insert_sql(cursor):
    return next(c for c in cursor.calls if c[0].startswith("INSERT INTO simulation_events"))


def _schema_reads(cursor):
    return [c for c in cursor.calls if "information_schema.columns" in c[0]]


def test_insert_skips_columns_the_upgraded_table_lacks(caplog):
    """db-init did not re-run: the table has no mes_ind_* columns; the cycle still records."""
    orch_mod._reset_event_columns_cache()
    old_table = [c for c in _ddl_columns() if c not in _IND_COLUMNS]
    sites = tuple(sites_mod.SITE_EVENT_PREFIX)
    with caplog.at_level(logging.WARNING, logger=orch_mod.logger.name):
        first, conn = _record(_site_rows(sites), old_table)
        second, _ = _record(_site_rows(sites), old_table)
    row = _inserted(first)
    assert not set(_IND_COLUMNS) & set(row)
    assert {k: row[k] for k in _legacy_expected()} == _legacy_expected()
    assert _insert_sql(first) == _insert_sql(second) and conn.commits == 1
    assert len(_schema_reads(first)) == 1 and _schema_reads(second) == []  # once per process
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert all(c in warnings[0].getMessage() for c in _IND_COLUMNS)


def test_insert_is_unchanged_when_every_column_exists(caplog):
    primed, _ = _record(_site_rows(_LEGACY_SITES))
    orch_mod._reset_event_columns_cache()
    with caplog.at_level(logging.WARNING, logger=orch_mod.logger.name):
        read, _ = _record(_site_rows(_LEGACY_SITES), _ddl_columns())
    assert _insert_sql(read) == _insert_sql(primed)
    assert len(_schema_reads(read)) == 1
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_empty_column_read_inserts_everything_and_rereads():
    """No rows (table not visible) is unknown, not "no columns": insert all, retry next cycle."""
    orch_mod._reset_event_columns_cache()
    first, _ = _record([], [])
    second, _ = _record([], [])
    assert set(_IND_COLUMNS) <= set(_inserted(first))
    assert len(_schema_reads(first)) == len(_schema_reads(second)) == 1


# --------------------------------------------------------------------------
# erp_simulation/sites.py: the stdlib-only leaf both consumers import
# --------------------------------------------------------------------------

def _imported_modules(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def test_sites_is_a_stdlib_leaf():
    imported = _imported_modules(sites_mod.__file__)
    assert all(not name.startswith(".") and name.split(".")[0] in sys.stdlib_module_names
               for name in imported), imported


def test_orchestrator_re_exports_the_site_constants():
    assert orch_mod.SITE_EVENT_PREFIX is sites_mod.SITE_EVENT_PREFIX
    assert orch_mod.SITE_EVENT_STATES is sites_mod.SITE_EVENT_STATES


def test_dashboard_does_not_import_the_orchestrator():
    imported = _imported_modules(_SIM_DIR / "streamlit_dashboard.py")
    assert "erp_simulation.sites" in imported
    assert not {n for n in imported if "simulation_orchestrator" in n}


# --------------------------------------------------------------------------
# Dashboard harness: fake streamlit / pandas / plotly / boto3
# --------------------------------------------------------------------------

class _State(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


def _norm(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items()}
    return getattr(value, "trace_name", type(value).__name__)


class _Ctx:
    def __init__(self, trace, name):
        self._trace, self.trace_name = trace, name

    def __enter__(self):
        self._trace.append(["enter", self.trace_name])
        return self

    def __exit__(self, *exc):
        self._trace.append(["exit", self.trace_name])
        return False


class _RerunRequested(BaseException):
    """The fake ``st.rerun``: like Streamlit's, it halts the script run (and is not an Exception)."""


class _FakeSt:
    def __init__(self, trace, pressed=(), selects=None, texts=None):
        self._trace, self._pressed = trace, set(pressed)
        self._selects, self._texts = selects or {}, texts or {}
        self._n = 0
        self.session_state = _State()

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self._trace.append([name, _norm(args), _norm(dict(sorted(kwargs.items())))])
            return self._result(name, args, kwargs)
        return call

    def _result(self, name, args, kwargs):
        self._n += 1
        if name == "rerun":
            raise _RerunRequested()
        if name == "columns":
            spec = args[0]
            count = spec if isinstance(spec, int) else len(spec)
            return [_Ctx(self._trace, f"col{self._n}.{i}") for i in range(count)]
        if name in ("spinner", "container", "expander"):
            return _Ctx(self._trace, f"{name}{self._n}")
        if name == "button":
            return kwargs.get("key", args[0]) in self._pressed or args[0] in self._pressed
        if name == "selectbox":
            return self._selects.get(args[0], args[1][0])
        if name == "text_area":
            return self._texts.get(args[0], kwargs.get("value", ""))
        if name == "number_input":
            return kwargs["value"]
        if name == "dialog":
            return lambda fn: fn
        if name == "empty":
            return self  # the placeholder records its own calls
        return None


class _FakeFrame:
    def __init__(self, columns=None, empty=False):
        self._columns = columns or {}
        self.empty = empty
        self.columns = list(self._columns)

    def __getitem__(self, column):
        return _FakeSeries(self._columns[column])

    def iterrows(self):
        size = len(next(iter(self._columns.values()), []))
        for i in range(size):
            yield i, {c: v[i] for c, v in self._columns.items()}


class _FakeSeries:
    def __init__(self, values):
        self._values = list(values)
        self.iloc = self._values

    def fillna(self, fill):
        return _FakeSeries([fill if v is None else v for v in self._values])

    def tolist(self):
        return list(self._values)


class _FakeDataFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_csv(self, index=False):
        return f"csv:{len(self.rows)}"


class _FixedDateTime:
    @staticmethod
    def now():
        return datetime(2026, 1, 2, 3, 4, 5)


def _fake_modules():
    mods = {name: types.ModuleType(name) for name in (
        "pandas", "plotly", "plotly.graph_objects", "plotly.express")}
    mods["pandas"].DataFrame = _FakeDataFrame
    mods["streamlit"] = _FakeSt([])  # `import streamlit as st` binds this object
    return mods


@pytest.fixture(scope="module")
def dash():
    patch = pytest.MonkeyPatch()
    for name, module in _fake_modules().items():
        patch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("_t10_streamlit_dashboard", _DASHBOARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patch.undo()
    return module


class _SqlCursor:
    def __init__(self, trace, cfg):
        self._trace, self._cfg = trace, cfg
        self.description = [("status",), ("count",)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._trace.append(["sql", " ".join(sql.split())[:60], _norm(params)])
        if self._cfg.get("query_raises") and "simulation_config" not in sql:
            raise RuntimeError("bad query")
        if self._cfg.get("scenario_raises") and "active_scenario" in sql:
            raise RuntimeError("config table unreadable")
        self._anchor_query = params == ("story_anchor_epoch",)

    def fetchone(self):
        if getattr(self, "_anchor_query", False):
            return self._cfg.get("anchor_row")  # frozen/demo: no live-story anchor row
        return self._cfg.get("scenario_row")

    def fetchall(self):
        return [] if self._cfg.get("no_rows") else [("PENDING", 3), ("SHIPPED", 5)]


class _DashDB:
    def __init__(self, trace, cfg):
        self._trace, self._cfg = trace, cfg

    @contextmanager
    def connection(self):
        cursor = _SqlCursor(self._trace, self._cfg)
        # rollback/autocommit/close: the SQL console's read-only transaction
        # (sql_console.run_readonly_query) rolls back on success too, and
        # closes a connection its cleanup cannot prove clean.
        yield types.SimpleNamespace(cursor=lambda: cursor, autocommit=False,
                                    rollback=lambda: self._trace.append(["rollback"]),
                                    close=lambda: self._trace.append(["close"]))

    def execute_query_pandas(self, query, params=None):
        self._trace.append(["pandas", " ".join(query.split())[:60], _norm(params)])
        if self._cfg.get("refresh_raises"):
            raise RuntimeError("refresh config unreadable")
        if self._cfg.get("refresh_missing"):
            return _FakeFrame(empty=True)
        return _FakeFrame({"refresh_seconds": [60]})


def _fake_boto3(trace, function_error, raises=()):
    class _Client:
        def __init__(self, service):
            self.trace_name = service

        def __getattr__(self, op):
            def call(**kwargs):
                trace.append(["boto3", self.trace_name, op, _norm(kwargs)])
                if op in raises:
                    raise RuntimeError(f"{op} denied")
                if op == "describe_rule":
                    return {"State": "ENABLED"}
                response = {"Payload": io.BytesIO(b'{"ok": true}')}
                if function_error:
                    response["FunctionError"] = "Unhandled"
                return response
            return call

    boto3 = types.ModuleType("boto3")
    boto3.Session = lambda: types.SimpleNamespace(client=lambda s, region_name=None: _Client(s))
    return boto3


def _kpis(sites):
    return {
        "orders": (1, 2, 3, 4, 5, 6, 7), "production": (11, 12, 13, 14, 15, 16),
        "shipping": (21, 22, 23, 24, 25, 26, 27, 28), "inventory": (31, 32, 33, 34),
        "procurement": (41, 42, 43, 44, 45), "finance": (51, 52, 5300.0, 5400.0, 55),
        "mes_per_site": [(s, 60 + i, 1, 2, 3) for i, s in enumerate(sites)],
    }


class _TraceLogger:
    """The dashboard logger: the swallowed-exception branches show up in the trace."""

    def __init__(self, trace):
        self._trace = trace

    def __getattr__(self, level):
        def log(msg, *args, **kwargs):
            self._trace.append(["log", level, str(msg) % args if args else str(msg)])
        return log


def _stub_board(dash, trace, cfg, kpis, sites):
    board = dash.ERPDashboard.__new__(dash.ERPDashboard)
    board.db = _DashDB(trace, cfg)

    def recorder(name, result=None):
        def call(*args, **kwargs):
            trace.append([name, _norm(args), _norm(kwargs)])
            return result
        return call

    for name in ("render_advanced_controls", "render_details_popup_trigger", "show_system_schema",
                 "apply_scenario", "render_system_card"):
        setattr(board, name, recorder(name))
    board.get_system_kpis = recorder("get_system_kpis", _kpis(sites) if kpis else None)
    board.render_flow_diagram = recorder("render_flow_diagram", "FLOW")
    board.render_activity_timeline = recorder("render_activity_timeline", cfg.get("timeline"))
    board.reset_all_data = recorder("reset_all_data", True)
    return board


def _fake_console_query(db, sql):
    """Stands in for console_login.run_console_query: the real read-only
    transaction over the fake connection (the login itself is covered by
    test_console_login.py)."""
    with db.connection() as conn:
        return dash_sql_console.run_readonly_query(conn, sql)


def _render_trace(dash, monkeypatch, *, pressed=(), selects=None, texts=None, session=None,
                  cfg=None, kpis=True, sites=_LEGACY_SITES):
    cfg = {"scenario_row": ("normal",), **(cfg or {})}
    trace = []
    st = _FakeSt(trace, pressed, selects, texts)
    st.session_state.update({"dark_mode": True, **(session or {})})
    monkeypatch.setattr(dash, "st", st)
    monkeypatch.setattr(dash, "datetime", _FixedDateTime)
    monkeypatch.setattr(dash, "time", types.SimpleNamespace(
        time=lambda: 1000.0, sleep=lambda s: trace.append(["sleep", s])))
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3(
        trace, cfg.get("function_error"), cfg.get("boto_raises", ())))
    monkeypatch.setitem(sys.modules, "pandas", _fake_modules()["pandas"])
    monkeypatch.setenv("AWS_REGION", "us-test-1")
    monkeypatch.setattr(dash, "logger", _TraceLogger(trace))
    monkeypatch.setattr(dash_console_login, "run_console_query", _fake_console_query)
    board = _stub_board(dash, trace, cfg, kpis, sites)
    try:
        board.render()
    except _RerunRequested:
        trace.append(["halted", "rerun"])
    trace.append(["session", _norm(dict(sorted(st.session_state.items())))])
    return trace


_SCENARIO_BUTTONS = {
    "normal": "✓ Normal", "demand_surge": "📈 Demand Surge", "mes_outage": "🔴 MES Outage",
    "supply_crisis": "🚚 Supply Crisis", "equipment_down": "⚠️ Equipment Down",
    "cash_crunch": "💰 Cash Crunch", "quality_issues": "🔍 Quality Issues",
    "capacity_plus": "🚀 Capacity+",
}

_RENDER_SCENARIOS = {
    "idle": {},
    "scenarios": {"pressed": ("🔍 Quality Issues", "⚙️ Advanced"),
                  "session": {"show_scenarios": True, "show_advanced_controls": True,
                              "active_scenario": "demand_surge", "sim_status_message": "hello"},
                  "cfg": {"timeline": "TL"}},
    "controls": {"pressed": ("auto_run_sim", "pause_sim", "single_cycle", "reset_data", "▶️ Execute"),
                 "selects": {"Quick Templates": "Show orders by status"}},
    "errors": {"pressed": ("single_cycle", "▶️ Execute", "toggle_scenarios"),
               "texts": {"SQL Query": "SELECT * FROM t WHERE DROP"},
               "cfg": {"function_error": True, "scenario_row": None}},
    "not_select": {"pressed": ("▶️ Execute",), "texts": {"SQL Query": "UPDATE x SET y = 1"}},
    "query_fails": {"pressed": ("▶️ Execute",), "texts": {"SQL Query": "SELECT broken"},
                    "cfg": {"query_raises": True}},
    "no_kpis_refresh_due": {"kpis": False,
                            "session": {"last_refresh_timestamp": 0.0, "sparkline_events": "x"},
                            "cfg": {"refresh_missing": True}},
    # Branches the first seven stop short of (st.rerun halts the run, as in Streamlit).
    "theme_toggle": {"pressed": ("theme_toggle",)},
    "theme_toggle_light": {"pressed": ("theme_toggle",), "session": {"dark_mode": False}},
    "auto_run_fails": {"pressed": ("auto_run_sim",), "cfg": {"boto_raises": ("enable_rule",)}},
    "pause": {"pressed": ("pause_sim",)},
    "pause_fails": {"pressed": ("pause_sim",), "cfg": {"boto_raises": ("disable_rule",)}},
    "single_cycle": {"pressed": ("single_cycle",)},
    "single_cycle_fails": {"pressed": ("single_cycle",), "cfg": {"boto_raises": ("invoke",)}},
    "rule_unreadable": {"cfg": {"boto_raises": ("describe_rule",)}},
    "reset_data": {"pressed": ("reset_data",)},
    "scenario_load_fails": {"cfg": {"scenario_raises": True}},
    "toggle_scenarios_collapse": {"pressed": ("toggle_scenarios",), "session": {"show_scenarios": True}},
    **{f"scenario_button_{key}": {"pressed": (label,), "session": {"show_scenarios": True}}
       for key, label in _SCENARIO_BUTTONS.items()},
    "advanced_open": {"session": {"show_scenarios": True, "show_advanced_controls": True}},
    "sql_template_execute": {"pressed": ("▶️ Execute",),
                             "selects": {"Quick Templates": "Show orders by status"}},
    "sql_no_results": {"pressed": ("▶️ Execute",), "texts": {"SQL Query": "SELECT 1"},
                       "cfg": {"no_rows": True}},
    "refresh_config_fails": {"cfg": {"refresh_raises": True}},
    # Live story (a story_anchor_epoch row): Pause, Reset and the scenarios sit
    # in the collapsed operator expander and Reset takes two clicks.
    "live_story_idle": {"cfg": {"anchor_row": (1,)}},
    "live_story_reset_first_click": {"pressed": ("reset_data",), "cfg": {"anchor_row": (1,)}},
    "live_story_reset_second_click": {"pressed": ("reset_data",), "session": {"reset_armed_at": 995.0},
                                      "cfg": {"anchor_row": (1,)}},
    "live_story_advanced_open": {"session": {"show_advanced_controls": True}, "cfg": {"anchor_row": (1,)}},
}


def _trace_lines(trace):
    return [json.dumps(step, ensure_ascii=False, sort_keys=True) for step in trace]


def _load_golden():
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


def _write_golden(dash, monkeypatch):
    """One JSON object; one trace step per line so a reviewed diff reads step by step."""
    blocks = []
    for name in sorted(_RENDER_SCENARIOS):
        steps = ",\n  ".join(_trace_lines(_render_trace(dash, monkeypatch, **_RENDER_SCENARIOS[name])))
        blocks.append(f" {json.dumps(name)}: [\n  {steps}\n ]")
    _GOLDEN_PATH.parent.mkdir(exist_ok=True)
    _GOLDEN_PATH.write_text("{\n" + ",\n".join(blocks) + "\n}\n", encoding="utf-8")


def test_golden_covers_every_render_scenario():
    assert sorted(_load_golden()) == sorted(_RENDER_SCENARIOS)


@pytest.mark.parametrize("scenario", sorted(_RENDER_SCENARIOS))
def test_characterization_render_call_trace(dash, monkeypatch, scenario):
    if os.environ.get("ERP_SIM_REGEN_RENDER_GOLDEN") == "1" and scenario == min(_RENDER_SCENARIOS):
        _write_golden(dash, monkeypatch)
    expected = _load_golden()[scenario]
    trace = json.loads(json.dumps(_render_trace(dash, monkeypatch, **_RENDER_SCENARIOS[scenario])))
    if trace != expected:
        diff = difflib.unified_diff(_trace_lines(expected), _trace_lines(trace),
                                    f"golden/{scenario}", f"render/{scenario}", lineterm="")
        pytest.fail(f"render call trace changed for {scenario!r}:\n" + "\n".join(diff), pytrace=False)


def test_rerun_halts_the_run_like_streamlit(dash, monkeypatch):
    trace = _render_trace(dash, monkeypatch, pressed=("theme_toggle",))
    reruns = [i for i, step in enumerate(trace) if step[0] == "rerun"]
    assert len(reruns) == 1 and trace[-2] == ["halted", "rerun"]
    assert {step[0] for step in trace[reruns[0] + 1:-2]} <= {"exit"}  # only unwinding `with` blocks
    assert not any(step[0] == "get_system_kpis" for step in trace)
    assert trace[-1][1]["dark_mode"] is False


def test_characterization_render_mes_cards_for_legacy_sites(dash, monkeypatch):
    trace = _render_trace(dash, monkeypatch)
    cards = [c[1] for c in trace if c[0] == "render_system_card"]
    assert [c[0] for c in cards] == [
        "Orders", "Production", "Shipping",
        "MES - Milwaukee (Beverages)", "MES - Detroit (Automotive)", "MES - Cedar Rapids (Food)",
        "Inventory", "Procurement", "Finance",
    ]
    assert [c[5] for c in cards[3:6]] == [f"mes_{s}" for s in _LEGACY_SITES]


# --------------------------------------------------------------------------
# Sparkline metric keys
# --------------------------------------------------------------------------

_LEGACY_COLUMN_MAP = {
    ("orders", "New Orders"): "orders_new", ("orders", "Pending"): "orders_pending",
    ("orders", "Confirmed"): "orders_confirmed", ("orders", "🔴 BACKORDERED"): "orders_backordered",
    ("orders", "Shipped"): "orders_shipped", ("orders", "In Production"): "orders_in_production",
    ("production", "Total Runs"): "production_new", ("production", "Planned"): "production_planned",
    ("production", "Released"): "production_released",
    ("production", "In Progress"): "production_in_progress",
    ("production", "Completed"): "production_completed",
    ("procurement", "Sent"): "procurement_sent", ("procurement", "In Transit"): "procurement_in_transit",
    ("procurement", "Received"): "procurement_received",
    ("shipping", "Preparing"): "shipping_preparing", ("shipping", "Picked"): "shipping_picked",
    ("shipping", "Packed"): "shipping_packed", ("shipping", "Shipped"): "shipping_shipped",
    ("shipping", "In Transit"): "shipping_in_transit", ("shipping", "Delivered"): "shipping_delivered",
    ("finance", "Invoices"): "finance_invoices", ("finance", "Paid"): "finance_paid",
    ("finance", "Revenue"): "finance_revenue", ("finance", "Collected"): "finance_collected",
    ("mes", "Scheduled"): "mes_scheduled", ("mes", "Running"): "mes_running",
    ("mes", "Completed"): "mes_completed",
    ("mes_MILWAUKEE_BOTTLING", "Scheduled"): "mes_mke_scheduled",
    ("mes_MILWAUKEE_BOTTLING", "Running"): "mes_mke_running",
    ("mes_MILWAUKEE_BOTTLING", "Completed"): "mes_mke_completed",
    ("mes_DETROIT_AUTOPARTS", "Scheduled"): "mes_det_scheduled",
    ("mes_DETROIT_AUTOPARTS", "Running"): "mes_det_running",
    ("mes_DETROIT_AUTOPARTS", "Completed"): "mes_det_completed",
    ("mes_CEDARRAPIDS_FOOD", "Scheduled"): "mes_cr_scheduled",
    ("mes_CEDARRAPIDS_FOOD", "Running"): "mes_cr_running",
    ("mes_CEDARRAPIDS_FOOD", "Completed"): "mes_cr_completed",
    ("inventory", "Recent Transactions"): "inventory_transactions",
}
_LEGACY_TOTALS = {
    ("mes", "Work Orders"): ["mes_scheduled", "mes_running", "mes_completed"],
    ("mes_MILWAUKEE_BOTTLING", "Work Orders"): ["mes_mke_scheduled", "mes_mke_running", "mes_mke_completed"],
    ("mes_DETROIT_AUTOPARTS", "Work Orders"): ["mes_det_scheduled", "mes_det_running", "mes_det_completed"],
    ("mes_CEDARRAPIDS_FOOD", "Work Orders"): ["mes_cr_scheduled", "mes_cr_running", "mes_cr_completed"],
    ("procurement", "Total POs"): ["procurement_sent", "procurement_in_transit", "procurement_received"],
    ("shipping", "Total"): ["shipping_preparing", "shipping_picked", "shipping_packed",
                            "shipping_shipped", "shipping_in_transit", "shipping_delivered"],
}


def _event_columns(extra=()):
    names = sorted({*_LEGACY_COLUMN_MAP.values(), *extra})
    return {name: [i, i + 100, None if i % 5 == 0 else i + 200] for i, name in enumerate(names, 1)}


def _sparkline(dash, monkeypatch, columns, key, count=3):
    st = _FakeSt([])
    st.session_state.update({"sparkline_event_count": count,
                             "sparkline_events": _FakeFrame(columns)})
    monkeypatch.setattr(dash, "st", st)
    board = dash.ERPDashboard.__new__(dash.ERPDashboard)
    board.db = None
    return board.get_sparkline_events(*key)


def test_characterization_sparkline_legacy_single_columns(dash, monkeypatch):
    columns = _event_columns()
    for key, column in _LEGACY_COLUMN_MAP.items():
        expected = [0 if v is None else v for v in columns[column]]
        assert _sparkline(dash, monkeypatch, columns, key) == expected, key


def test_characterization_sparkline_legacy_totals(dash, monkeypatch):
    columns = {name: [1, 2, 3] for names in _LEGACY_TOTALS.values() for name in names}
    for key, names in _LEGACY_TOTALS.items():
        assert _sparkline(dash, monkeypatch, columns, key) == [len(names) * v for v in (1, 2, 3)], key


def test_characterization_sparkline_unknown_missing_and_padding(dash, monkeypatch):
    columns = {"orders_new": [4, 5]}
    assert _sparkline(dash, monkeypatch, columns, ("orders", "New Orders"), count=4) == [0, 0, 4, 5]
    assert _sparkline(dash, monkeypatch, columns, ("orders", "Pending")) == [0, 0, 0]
    assert _sparkline(dash, monkeypatch, columns, ("nope", "Nope")) == [0, 0, 0]


# --------------------------------------------------------------------------
# The fourth site on the dashboard
# --------------------------------------------------------------------------

def test_site_labels_cover_every_event_site(dash):
    assert set(dash.SITE_LABELS) == set(orch_mod.SITE_EVENT_PREFIX)
    assert all(label.strip() for label in dash.SITE_LABELS.values())


def test_sparkline_maps_are_generated_from_the_site_map(dash):
    for site, prefix in orch_mod.SITE_EVENT_PREFIX.items():
        columns = [f"mes_{prefix}_{state}" for state in orch_mod.SITE_EVENT_STATES]
        assert dash.SPARKLINE_TOTAL_METRICS[(f"mes_{site}", "Work Orders")] == columns
        assert [dash.SPARKLINE_COLUMN_MAP[(f"mes_{site}", s.title())]
                for s in orch_mod.SITE_EVENT_STATES] == columns
    assert {k: v for k, v in dash.SPARKLINE_COLUMN_MAP.items() if k in _LEGACY_COLUMN_MAP} == _LEGACY_COLUMN_MAP
    extra = set(dash.SPARKLINE_COLUMN_MAP) - set(_LEGACY_COLUMN_MAP)
    assert {k[0] for k in extra} == {f"mes_{s}" for s in orch_mod.SITE_EVENT_PREFIX} - {
        f"mes_{s}" for s in _LEGACY_SITES}


def test_sparkline_reads_the_fourth_site_columns(dash, monkeypatch):
    site, prefix = list(orch_mod.SITE_EVENT_PREFIX.items())[3]
    extra = [f"mes_{prefix}_{s}" for s in orch_mod.SITE_EVENT_STATES]
    columns = _event_columns(extra)
    assert _sparkline(dash, monkeypatch, columns, (f"mes_{site}", "Running")) == [
        0 if v is None else v for v in columns[f"mes_{prefix}_running"]]
    total = _sparkline(dash, monkeypatch, {c: [1, 2, 3] for c in extra}, (f"mes_{site}", "Work Orders"))
    assert total == [3, 6, 9]


def test_render_labels_the_fourth_site_card(dash, monkeypatch):
    sites = tuple(orch_mod.SITE_EVENT_PREFIX)
    trace = _render_trace(dash, monkeypatch, sites=sites)
    mes_cards = [c[1] for c in trace if c[0] == "render_system_card" and c[1][0].startswith("MES - ")]
    assert [c[0] for c in mes_cards] == [f"MES - {dash.SITE_LABELS[s]}" for s in sites]
    assert [c[5] for c in mes_cards] == [f"mes_{s}" for s in sites]


# --------------------------------------------------------------------------
# Real PostgreSQL (throwaway container): ERP_SIM_TEST_PG_DSN; skipped otherwise.
# --------------------------------------------------------------------------
_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_HANDLER_PATH = Path(__file__).resolve().parents[2] / "lambda-db-init" / "handler.py"


def _load_db_init_handler():
    spec = importlib.util.spec_from_file_location("db_init_handler_t10", _HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    stand_in = "boto3" not in sys.modules
    if stand_in:
        sys.modules["boto3"] = types.ModuleType("boto3")
    try:
        spec.loader.exec_module(module)
    finally:
        if stand_in:
            del sys.modules["boto3"]
    return module


@contextmanager
def _fresh_erp_db():
    """A throwaway database built by lambda-db-init (schema + seed)."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    handler = _load_db_init_handler()
    db_name = "t10_" + uuid.uuid4().hex[:8]
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=db_name))
    conn = pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": db_name}))
    try:
        handler.create_schema(conn)
        handler.seed_data(conn)
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE {db}", db=db_name))
        admin.close()


@pytest.fixture(scope="module")
def pg_erp():
    with _fresh_erp_db() as conn:
        yield conn


class _PgDb:
    def __init__(self, conn):
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


def _pg_cycle(conn):
    orch = orch_mod.SimulationOrchestrator.__new__(orch_mod.SimulationOrchestrator)
    orch.db = _PgDb(conn)
    orch._record_simulation_event(1, _metrics())


def _completed_work_orders(cur, site, count):
    cur.execute("SELECT product_id FROM products ORDER BY product_id LIMIT 1")
    product_id = cur.fetchone()[0]
    for n in range(count):
        tag = f"{site[:3]}-{n}-{uuid.uuid4().hex[:6]}"
        cur.execute("""
            INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site, line)
            VALUES (%s, %s, 10, 'COMPLETED', %s, 'LINE1') RETURNING run_id
        """, (f"RUN-T10-{tag}", product_id, site))
        run_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id, site, line,
                                         planned_quantity, status, scheduled_start_time)
            VALUES (%s, %s, %s, %s, 'LINE1', 10, 'COMPLETED', NOW())
        """, (f"WO-T10-{tag}", run_id, product_id, site))


def test_pg_one_cycle_records_the_fourth_site(pg_erp):
    fourth, fourth_prefix = list(orch_mod.SITE_EVENT_PREFIX.items())[3]
    legacy_prefix = orch_mod.SITE_EVENT_PREFIX[_LEGACY_SITES[0]]
    with pg_erp.cursor() as cur:
        cur.execute("SELECT is_active FROM sites WHERE site_code = %s", (fourth,))
        assert cur.fetchone() == (True,)  # the dashboard KPI query lists active sites
        _completed_work_orders(cur, fourth, 2)
        _completed_work_orders(cur, _LEGACY_SITES[0], 1)
    pg_erp.commit()

    orch_mod._reset_event_columns_cache()  # the real information_schema read
    _pg_cycle(pg_erp)
    with pg_erp.cursor() as cur:
        cur.execute(stmt("SELECT cycle_number, {fourth}, {legacy}, orders_new FROM simulation_events"
                         " ORDER BY cycle_number DESC LIMIT 1",
                         fourth=f"mes_{fourth_prefix}_completed", legacy=f"mes_{legacy_prefix}_completed"))
        cycle, fourth_completed, legacy_completed, orders_new = cur.fetchone()
    assert fourth_completed == 2 and legacy_completed == 1
    assert orders_new == _metrics()["orders_created"] and cycle >= 1


def test_pg_cycle_records_on_a_table_without_the_fourth_site_columns(caplog):
    """An upgraded environment whose db-init did not re-run: no mes_ind_* columns."""
    orch_mod._reset_event_columns_cache()
    with _fresh_erp_db() as conn:
        with conn.cursor() as cur:
            for column in _IND_COLUMNS:
                cur.execute(stmt("ALTER TABLE simulation_events DROP COLUMN {column}", column=column))
            cur.execute("SELECT COUNT(*) FROM simulation_events")
            before = cur.fetchone()[0]
        conn.commit()
        with caplog.at_level(logging.WARNING, logger=orch_mod.logger.name):
            _pg_cycle(conn)
            _pg_cycle(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), MAX(orders_new) FROM simulation_events")
            after, orders_new = cur.fetchone()
    assert after == before + 2 and orders_new == _metrics()["orders_created"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and all(c in warnings[0] for c in _IND_COLUMNS)
