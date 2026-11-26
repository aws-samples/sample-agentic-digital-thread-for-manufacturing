# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the dashboard's guarded operator controls.

On a live story (the same check as the OT row's caption: an
``OT_FEED_SOURCE=opcua`` package or a ``story_anchor_epoch`` row), Pause,
Reset All Data and the scenario buttons sit in one collapsed "Operator
controls" expander and Reset takes two clicks. Frozen and demo deployments
(no anchor row, or a database error) keep the header buttons and the
scenario bar. Streamlit, pandas and plotly are stubbed; no network, no
database:

    python -m pytest erp_simulation/tests/test_dashboard_operator_controls.py -q
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "streamlit_dashboard.py"
_EXPANDER = ("expander", "🛠️ Operator controls")
_RESET, _PAUSE = "🔄 Reset All Data", "⏸️ Pause"
_SCENARIO_LABELS = {
    "✓ Normal", "📈 Demand Surge", "🔴 MES Outage", "🚚 Supply Crisis", "⚠️ Equipment Down",
    "💰 Cash Crunch", "🔍 Quality Issues", "🚀 Capacity+", "⚙️ Advanced",
}


class _State(dict):
    """``st.session_state``: a dict with attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class _Block:
    """A layout context (column, expander, container, spinner) on the fake's stack."""

    def __init__(self, st, kind, label):
        self._st, self._frame = st, (kind, label)

    def __enter__(self):
        self._st.stack.append(self._frame)
        return self

    def __exit__(self, *exc):
        self._st.stack.pop()
        return False


class _FakeSt:
    """Records every streamlit call as (name, args, kwargs, enclosing blocks)."""

    def __init__(self, pressed=(), state=None):
        self.calls, self.stack, self.pressed = [], [], set(pressed)
        self.session_state = state if isinstance(state, _State) else _State(state or {})

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs, tuple(self.stack)))
            if name == "columns":
                count = args[0] if isinstance(args[0], int) else len(args[0])
                return [_Block(self, "column", index) for index in range(count)]
            if name in ("expander", "container", "spinner"):
                return _Block(self, name, args[0] if args else None)
            if name == "button":
                return args[0] in self.pressed or kwargs.get("key") in self.pressed
            if name == "dialog":
                return lambda fn: fn
            if name == "empty":
                return self        # the placeholder records its own calls
            return None
        return call

    def named(self, name):
        return [c for c in self.calls if c[0] == name]

    def buttons(self):
        """(label, inside the operator expander) per button, in render order."""
        return [(c[1][0], _EXPANDER in c[3]) for c in self.named("button")]


class _Cursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append(" ".join(sql.split()))
        if self._conn.raises:
            raise RuntimeError("simulation_config unreadable")

    def fetchone(self):
        return self._conn.row


class _Conn:
    def __init__(self, row=None, raises=False):
        self.row, self.raises, self.executed = row, raises, []

    def cursor(self):
        return _Cursor(self)


class _DB:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Scope:
            def __enter__(self):
                return conn

            def __exit__(self, *exc):
                return False
        return _Scope()


@pytest.fixture(scope="module")
def dash():
    patch = pytest.MonkeyPatch()
    for name in ("pandas", "plotly", "plotly.graph_objects", "plotly.express"):
        patch.setitem(sys.modules, name, types.ModuleType(name))
    patch.setitem(sys.modules, "streamlit", _FakeSt())
    spec = importlib.util.spec_from_file_location("_opctl_streamlit_dashboard", _DASHBOARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patch.undo()
    return module


@pytest.fixture(autouse=True)
def _no_feed_source(monkeypatch):
    monkeypatch.delenv("OT_FEED_SOURCE", raising=False)


def _board(dash, db):
    board = dash.ERPDashboard.__new__(dash.ERPDashboard)
    board.db = db
    board._auto_run_enabled = lambda: False
    board.apply_scenario = lambda scenario: None
    board.render_advanced_controls = lambda: dash.st.markdown("advanced-body")
    return board


def _layout(dash, monkeypatch, db, state=None, pressed=()):
    """Header row, scenario area and OT row: everything above the KPI cards."""
    st = _FakeSt(pressed, {"dark_mode": True, **(state or {})})
    monkeypatch.setattr(dash, "st", st)
    board = _board(dash, db)
    board._render_header_row()
    board._render_scenario_area("normal")
    board.render_ot_feed_controls()
    return st


# ── the two-step Reset as a pure step ────────────────────────────────────

@pytest.mark.parametrize("armed_at, clicked, auto_rerun, expected", [
    (None, True, False, (1000.0, False)),      # first click arms
    (None, True, True, (1000.0, False)),
    (995.0, True, False, (None, True)),        # second click runs the reset
    (995.0, False, True, (995.0, False)),      # the refresh clock keeps the arm
    (995.0, False, False, (None, False)),      # any other interaction disarms
    (None, False, True, (None, False)),
    (None, False, False, (None, False)),
    (979.0, True, False, (1000.0, False)),     # an expired arm is a first click again
    (979.0, False, True, (None, False)),       # and expires on the refresh clock
    (980.0, True, False, (None, True)),        # exactly the window still runs
    (1001.0, True, False, (1000.0, False)),    # an arm from the future is not live
])
def test_reset_step(dash, armed_at, clicked, auto_rerun, expected):
    assert dash._RESET_ARM_SECONDS == 20.0
    assert dash._reset_step(armed_at, clicked, auto_rerun, 1000.0) == expected


# ── the two-step Reset as rendered ───────────────────────────────────────

def _reset_run(dash, monkeypatch, state, now, clicked=False, auto_rerun=False):
    if auto_rerun:
        state[dash._AUTO_RERUN_KEY] = True
    st = _FakeSt({"reset_data"} if clicked else (), state)
    monkeypatch.setattr(dash, "st", st)
    monkeypatch.setattr(dash, "time", types.SimpleNamespace(time=lambda: now, sleep=lambda s: None))
    board = dash.ERPDashboard.__new__(dash.ERPDashboard)
    resets = []
    board.reset_all_data = lambda: resets.append(now) or False
    board._render_guarded_reset_button()
    return [c[1][0] for c in st.named("warning")], resets


def test_first_click_arms_and_asks_for_a_second(dash, monkeypatch):
    state = _State()
    warnings, resets = _reset_run(dash, monkeypatch, state, 100.0, clicked=True)
    assert warnings == ["Click Reset again to wipe all plant history"]
    assert resets == []
    assert state[dash._RESET_ARMED_AT_KEY] == 100.0


def test_second_click_after_refresh_reruns_wipes_once(dash, monkeypatch):
    state = _State()
    _reset_run(dash, monkeypatch, state, 100.0, clicked=True)
    for now in (101.0, 102.0, 103.0):
        warnings, resets = _reset_run(dash, monkeypatch, state, now, auto_rerun=True)
        assert warnings == [dash._RESET_CONFIRM_TEXT] and resets == []
        assert dash._AUTO_RERUN_KEY not in state      # each marker is consumed
    warnings, resets = _reset_run(dash, monkeypatch, state, 104.0, clicked=True)
    assert resets == [104.0]
    assert warnings == []
    assert state[dash._RESET_ARMED_AT_KEY] is None


def test_any_other_interaction_disarms(dash, monkeypatch):
    state = _State()
    _reset_run(dash, monkeypatch, state, 100.0, clicked=True)
    warnings, resets = _reset_run(dash, monkeypatch, state, 101.0)
    assert warnings == [] and resets == []
    assert state[dash._RESET_ARMED_AT_KEY] is None
    warnings, resets = _reset_run(dash, monkeypatch, state, 102.0, clicked=True)
    assert warnings == [dash._RESET_CONFIRM_TEXT] and resets == []


def test_an_old_arm_expires_on_the_refresh_clock(dash, monkeypatch):
    state = _State()
    _reset_run(dash, monkeypatch, state, 100.0, clicked=True)
    warnings, _ = _reset_run(dash, monkeypatch, state, 121.0, auto_rerun=True)
    assert warnings == []
    _, resets = _reset_run(dash, monkeypatch, state, 122.0, clicked=True)
    assert resets == []


def test_disarmed_runs_still_emit_the_confirm_slot(dash, monkeypatch):
    """Every live run ends in the refresh loop's st.rerun(), so Streamlit never
    clears a stale element. The slot must be emitted on EVERY run so a disarmed
    run replaces the old warning instead of leaving it on screen, greyed out
    (seen live on Streamlit 1.62, 2026-10-04)."""
    state = _State()
    for now, clicked, auto in ((100.0, True, False), (121.0, False, True), (122.0, False, True)):
        if auto:
            state[dash._AUTO_RERUN_KEY] = True
        st = _FakeSt({"reset_data"} if clicked else (), state)
        monkeypatch.setattr(dash, "st", st)
        monkeypatch.setattr(dash, "time", types.SimpleNamespace(time=lambda: now, sleep=lambda s: None))
        board = dash.ERPDashboard.__new__(dash.ERPDashboard)
        board.reset_all_data = lambda: False
        board._render_guarded_reset_button()
        names = [c[0] for c in st.calls]
        assert names[:2] == ["button", "empty"], names
        assert names.count("warning") == (1 if clicked else 0)


# ── layout keyed by the anchor row ───────────────────────────────────────

def test_anchor_row_moves_pause_reset_and_scenarios_into_a_collapsed_expander(dash, monkeypatch):
    conn = _Conn(row=("1790000000.0",))
    st = _layout(dash, monkeypatch, _DB(conn))
    expanders = st.named("expander")
    assert [(c[1], c[2]) for c in expanders] == [(("🛠️ Operator controls",), {"expanded": False})]
    buttons = st.buttons()
    outside = [label for label, inside in buttons if not inside]
    inside = [label for label, inside in buttons if inside]
    assert outside == ["☀️ Light", "▶️ Auto Run", "⏭️ Single Cycle"]
    assert inside[:2] == [_PAUSE, _RESET]
    assert set(inside[2:]) == _SCENARIO_LABELS
    assert "▼ Expand" not in outside                  # no second collapse layer
    assert [c[1][0] for c in st.named("caption")] == [dash._STORY_CLOCK_CAPTION]
    assert conn.executed == ["SELECT 1 FROM simulation_config WHERE config_key = %s"]


def test_live_package_guards_without_a_database_read(dash, monkeypatch):
    monkeypatch.setenv("OT_FEED_SOURCE", "opcua")
    conn = _Conn(row=None)
    st = _layout(dash, monkeypatch, _DB(conn))
    assert [label for label, inside in st.buttons() if inside][:2] == [_PAUSE, _RESET]
    assert conn.executed == []


def test_guarded_advanced_controls_do_not_nest_an_expander(dash, monkeypatch):
    st = _layout(dash, monkeypatch, _DB(_Conn(row=(1,))), state={"show_advanced_controls": True})
    assert [c[1][0] for c in st.named("expander")] == ["🛠️ Operator controls"]
    body = [c for c in st.named("markdown") if c[1] == ("advanced-body",)]
    assert len(body) == 1 and _EXPANDER in body[0][3]


@pytest.mark.parametrize("conn", [_Conn(row=None), _Conn(raises=True)], ids=["no-anchor-row", "db-error"])
def test_frozen_and_demo_keep_todays_layout(dash, monkeypatch, conn):
    st = _layout(dash, monkeypatch, _DB(conn))
    assert st.named("expander") == []
    assert st.buttons() == [
        ("☀️ Light", False), ("▶️ Auto Run", False), (_PAUSE, False),
        ("⏭️ Single Cycle", False), (_RESET, False), ("▼ Expand", False),
        ("📉 Start machine degradation feed", False),
    ]
    assert [c[1][0] for c in st.named("columns")][:2] == [[2.5, 2.5, 0.8, 0.7, 0.5], 3]
    assert len(conn.executed) == 1                    # one anchor read per run, as before


def test_frozen_reset_stays_one_click(dash, monkeypatch):
    st = _FakeSt({"reset_data"}, {"dark_mode": True})
    monkeypatch.setattr(dash, "st", st)
    monkeypatch.setattr(dash, "time", types.SimpleNamespace(time=lambda: 0.0, sleep=lambda s: None))
    board = _board(dash, _DB(_Conn(row=None)))
    resets = []
    board.reset_all_data = lambda: resets.append(True) or False
    board._render_header_row()
    assert resets == [True]
    assert st.named("warning") == []


def test_frozen_expanded_scenarios_render_outside_any_expander(dash, monkeypatch):
    st = _layout(dash, monkeypatch, _DB(_Conn(row=None)),
                 state={"show_scenarios": True, "show_advanced_controls": True})
    labels = [label for label, inside in st.buttons() if not inside]
    assert _SCENARIO_LABELS <= set(labels)
    assert [c[1][0] for c in st.named("expander")] == ["⚙️ Advanced Parameter Controls"]


# ── scenario bar colour ──────────────────────────────────────────────────

def test_scenario_status_uses_the_theme_text_colour(dash):
    html = dash._scenario_status_html("mes_outage")
    assert f"color: {dash.THEME['text']};" in html
    assert "MES Outage" in html and "#dc3545" in html
    assert "#28a745" in dash._scenario_status_html("normal")
    assert "color: black" not in _DASHBOARD_PATH.read_text(encoding="utf-8")


# ── Advanced controls on a row a scenario inserted ───────────────────────
# apply_scenario inserts rows with no description or bounds; pandas reads the
# NULLs as NaN, which reached st.text_input as the label and failed the whole
# panel (ADT-DEMO 2026-10-28, "bad argument type for built-in operation").

def _config_row(**over):
    nan = float("nan")
    row = {"config_key": "active_scenario", "config_value": "normal", "data_type": "string",
           "category": "dashboard", "description": nan, "min_value": nan, "max_value": nan,
           "default_value": "normal"}
    row.update(over)
    return row


@pytest.mark.parametrize("data_type,value", [
    ("string", "normal"), ("int", "5"), ("float", "120"), ("boolean", "true")])
def test_advanced_widget_labels_a_null_description_with_the_key(dash, monkeypatch, data_type, value):
    st = _FakeSt()
    monkeypatch.setattr(dash, "st", st)
    dash.ERPDashboard._advanced_config_widget(
        _config_row(config_key="k_" + data_type, data_type=data_type, config_value=value))
    (call,) = [c for c in st.calls if c[0] in ("text_input", "slider", "checkbox")]
    assert call[1][0] == "k_" + data_type
    if call[0] == "slider":
        assert call[2]["min_value"] == 0 and call[2]["max_value"] in (1, 1.0, 1000)


def test_advanced_widget_keeps_a_real_description_and_bounds(dash, monkeypatch):
    st = _FakeSt()
    monkeypatch.setattr(dash, "st", st)
    dash.ERPDashboard._advanced_config_widget(_config_row(
        config_key="procurement_delivery_variability_minutes", data_type="float",
        config_value="120", description="Delivery-time spread (min) for OTD",
        min_value=0.0, max_value=1440.0))
    (call,) = st.named("slider")
    assert call[1][0] == "Delivery-time spread (min) for OTD"
    assert (call[2]["min_value"], call[2]["max_value"], call[2]["step"]) == (0.0, 1440.0, 0.1)
