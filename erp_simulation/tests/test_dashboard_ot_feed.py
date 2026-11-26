# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the dashboard OT-feed control row (ADR-0088 D7).

The degradation-feed button is keyed by DATA, never by the story setting:
a ``simulation_config.story_anchor_epoch`` row means the live story clock
drives the sensor feed, so the row shows a read-only caption instead of the
button (a click would overwrite the anchored grid with a wall-clock ramp).
No row keeps the button exactly as before, and a database error falls back
to the button so frozen behaviour never breaks. Streamlit, pandas and plotly
are stubbed; no network, no database:

    python -m pytest erp_simulation/tests/test_dashboard_ot_feed.py -q
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "streamlit_dashboard.py"
_BUTTON_LABEL = "📉 Start machine degradation feed"


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _State(dict):
    """``st.session_state``: a dict with attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class _FakeSt:
    """Records every streamlit call as (name, args, kwargs)."""

    def __init__(self, pressed=False):
        self.calls, self._pressed = [], pressed
        self.session_state = _State()

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "columns":
                return [_Ctx() for _ in args[0]]
            if name == "button":
                return self._pressed
            if name == "dialog":
                return lambda fn: fn
            return None
        return call

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class _Cursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
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
    def __init__(self, conn=None, connect_raises=False):
        self._conn, self._connect_raises = conn, connect_raises

    def connection(self):
        if self._connect_raises:
            raise RuntimeError("database unreachable")
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
    spec = importlib.util.spec_from_file_location("_d7_streamlit_dashboard", _DASHBOARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patch.undo()
    return module


def _render(dash, monkeypatch, db, pressed=False):
    st = _FakeSt(pressed)
    monkeypatch.setattr(dash, "st", st)
    board = dash.ERPDashboard.__new__(dash.ERPDashboard)
    board.db = db
    started = []
    board.start_ot_degradation_feed = lambda: started.append(True)
    board.render_ot_feed_controls()
    return st, started


def _assert_button_as_today(st):
    buttons = st.named("button")
    assert len(buttons) == 1
    _, args, kwargs = buttons[0]
    assert args == (_BUTTON_LABEL,)
    assert kwargs["key"] == "ot_feed_start"
    assert kwargs["type"] == "secondary"
    assert kwargs["use_container_width"] is True
    assert st.named("caption") == []


def test_anchor_row_shows_caption_and_no_button(dash, monkeypatch):
    conn = _Conn(row=("1790000000.0",))
    st, started = _render(dash, monkeypatch, _DB(conn), pressed=True)
    assert st.named("button") == []
    assert started == []
    captions = st.named("caption")
    assert len(captions) == 1
    text = captions[0][1][0]
    assert "plant clock shown in the ADT app header" in text
    assert "answer key" not in text
    assert "\u2014" not in text and "\u2013" not in text
    assert conn.executed == [(
        "SELECT 1 FROM simulation_config WHERE config_key = %s", ("story_anchor_epoch",))]


def test_no_anchor_row_keeps_the_button_unchanged(dash, monkeypatch):
    st, started = _render(dash, monkeypatch, _DB(_Conn(row=None)))
    _assert_button_as_today(st)
    assert started == []


def test_no_anchor_row_button_click_starts_the_feed(dash, monkeypatch):
    st, started = _render(dash, monkeypatch, _DB(_Conn(row=None)), pressed=True)
    _assert_button_as_today(st)
    assert started == [True]


@pytest.mark.parametrize("db", [
    _DB(_Conn(raises=True)),
    _DB(connect_raises=True),
], ids=["query-error", "connect-error"])
def test_db_error_falls_back_to_the_button(dash, monkeypatch, db):
    st, _ = _render(dash, monkeypatch, db)
    _assert_button_as_today(st)
    assert st.named("error") == []


def test_story_clock_active_reads_the_anchor_key(dash):
    assert dash._story_clock_active(_Conn(row=(1,))) is True
    assert dash._story_clock_active(_Conn(row=None)) is False
    with pytest.raises(RuntimeError):
        dash._story_clock_active(_Conn(raises=True))


@pytest.mark.parametrize("db", [
    _DB(_Conn(row=None)),
    _DB(_Conn(raises=True)),
    _DB(connect_raises=True),
], ids=["no-anchor-row", "query-error", "connect-error"])
def test_opcua_package_never_offers_the_button(dash, monkeypatch, db):
    """A live package ships no feed Lambda (ADR-0092): OT_FEED_SOURCE=opcua
    shows the caption without reading the database, so neither a missing
    anchor row nor a database error can bring back a button with nothing to
    invoke."""
    monkeypatch.setenv("OT_FEED_SOURCE", "opcua")
    st, started = _render(dash, monkeypatch, db, pressed=True)
    assert st.named("button") == []
    assert started == []
    assert [c[1][0] for c in st.named("caption")] == [dash._STORY_CLOCK_CAPTION]
    if db._conn is not None:
        assert db._conn.executed == []


def test_other_feed_source_values_keep_todays_behaviour(dash, monkeypatch):
    monkeypatch.setenv("OT_FEED_SOURCE", "lambda")
    st, _ = _render(dash, monkeypatch, _DB(_Conn(row=None)))
    _assert_button_as_today(st)
