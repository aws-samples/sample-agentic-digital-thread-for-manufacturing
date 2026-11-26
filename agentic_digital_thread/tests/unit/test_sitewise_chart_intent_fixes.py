# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SiteWise (Direct) history charts: user-driven intent, names-only titles.

Three behaviours are pinned here:

* Chart intent comes from the USER's question, carried deterministically in
  each delegation's ``kg_context`` (a reserved marker), NOT inferred from the
  model-rewritten delegated question. The delegated question is a fallback
  only when no marker is present (a direct/background call).
* An asked-for chart whose every series is unnamed still delivers the
  ``N series not charted: no name recorded`` notice (through the chart-tag
  delivery path), never silently nothing, and never an id.
* A resolved name that IS an id (equals the source id, or is an id-shaped
  whole token: UUID / long hex / long digits) is rejected; a unit is sanitised
  (short, printable, no id shape) or omitted. No id, hex, numeric id or hostile
  unit reaches any title, legend, label or notice.

Fully synthetic: the reader and the graph lookup are monkeypatched; no AWS.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

import agents.chart_intent as ci
import agents.sitewise_read as sitewise_read
import agents.system_agent as sa

_FIXED_NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
_T0 = int(datetime(2026, 1, 5, 11, 0, tzinfo=timezone.utc).timestamp())
_TAG = re.compile(r"<chart-block\b[^>]*/>")

_ASSET = "asset-1"
_VX, _VY = "prop-vx", "prop-vy"
_UUID_ASSET = "0f6c2a9e-1111-4222-8333-444455556666"
_UUID_PROP = "9a8b7c6d-1111-4222-8333-444455556666"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(sa, "utc_now", lambda: _FIXED_NOW)
    monkeypatch.setattr(sa, "attach_source_metadata",
                        lambda payload, **meta: {**payload, "_meta": meta})
    yield
    clear = getattr(sa, "_sitewise_chart_cache_clear", None)
    if clear is not None:
        clear()


def _graph(monkeypatch, known, system="sys-x"):
    def fake_read(cypher, parameters):
        if parameters.get("sys") != system:
            return []
        return [{"id": key, "name": name, "unit": unit}
                for key, (name, unit) in known.items() if key in parameters["ids"]]

    monkeypatch.setattr(sa, "_sitewise_chart_graph_read", fake_read, raising=False)
    clear = getattr(sa, "_sitewise_chart_cache_clear", None)
    if clear is not None:
        clear()


def _points(values, start=_T0, step=150):
    out = []
    for index, value in enumerate(values):
        stamp = start + index * step
        out.append({"value": value, "timestamp": stamp,
                    "time": datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(),
                    "quality": "GOOD"})
    return out


def _series(n, base=1.0):
    return _points([round(base + 0.0137 * i, 6) for i in range(n)])


def _install(monkeypatch, payload):
    monkeypatch.setattr(sitewise_read, "get_property_history",
                        lambda cfg, region, entries, **kw: payload)


def _tool():
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={},
                                          system_id="sys-x")
    return next(t for t in tools if t.tool_name == "sitewise_get_asset_property_history")


def _entries(*pairs):
    return json.dumps([{"assetId": a, "propertyId": p} for a, p in pairs])


def _read(monkeypatch, payload, *pairs, delegated, wants=None, **window):
    """Run the history tool inside a Mode A call built the way
    ``_run_mode_a_query`` builds it: ``delegated`` is the model-rewritten
    sub-question. ``wants`` is this turn's chart-intent bit as the runtime
    popped it from the reserved arg into ``runtime_asgi.chart_intent_var``
    (``True``/``False``); ``None`` means no turn context (a direct/background
    call), so the SiteWise path falls back to the delegated question."""
    from agents import runtime_asgi
    _install(monkeypatch, payload)
    call = sa._mode_a_call_for(delegated, "")
    token = sa._MODE_A_CALL.set(call)
    chart_token = runtime_asgi.chart_intent_var.set(wants)
    try:
        return _tool()._tool_func(entries_json=_entries(*pairs), **(window or {"hours_back": 1}))
    finally:
        sa._MODE_A_CALL.reset(token)
        runtime_asgi.chart_intent_var.reset(chart_token)


def _chart_free(payload):
    return sa._sitewise_json({**payload, "_meta": {
        "boundary": "sitewise", "system_id": "sys-x", "queried_at": _FIXED_NOW}})


def _chart_text(out):
    assert isinstance(out, dict), "a delivered notice/chart returns a two-part result"
    assert out["status"] == "success"
    texts = [part["text"] for part in out["content"]]
    assert len(texts) == 2
    return texts[1]




# --------------------------------------------------------------------------- #
# Intent is the USER's, carried in kg_context, not the delegation
# --------------------------------------------------------------------------- #
def test_user_asked_delegation_omitted_still_draws(monkeypatch):
    """The user asked for a chart; the model dropped the word while rewriting.
    The engine-stamped marker says the user asked, so charts are drawn."""
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), _VX: ("vib_x", "mm/s")})
    payload = {f"{_ASSET}_{_VX}": _series(24, 1.9)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="current vibration for asset 1",   # no chart word
                wants=True)
    assert "Unit A: vib_x" in _chart_text(out)


def test_user_did_not_ask_delegation_says_trend_draws_nothing(monkeypatch):
    """The user did not ask; the model ADDED `trend` while decomposing. The
    marker says the user did not ask, so nothing is drawn."""
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), _VX: ("vib_x", "mm/s")})
    payload = {f"{_ASSET}_{_VX}": _series(24, 1.9)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="show the vibration trend over time",
                wants=False)
    assert out == _chart_free(payload)


def test_trending_behind_schedule_marker_false_draws_nothing(monkeypatch):
    """`trending behind schedule` is business wording; the user-side matcher
    does not treat it as a chart request, so the marker is false."""
    assert ci.wants_chart("the order is trending behind schedule") is False
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), _VX: ("vib_x", "mm/s")})
    payload = {f"{_ASSET}_{_VX}": _series(6)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="is the order trending behind schedule",
                wants=ci.wants_chart("is the order trending behind schedule"))
    assert out == _chart_free(payload)


def test_no_marker_falls_back_to_delegated_question(monkeypatch):
    """A direct/background call carries no marker: the delegated question is
    the only signal, so an explicit chart word there still draws."""
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), _VX: ("vib_x", "mm/s")})
    payload = {f"{_ASSET}_{_VX}": _series(6)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="plot the vibration", wants=None)   # no turn context
    assert "Unit A: vib_x" in _chart_text(out)
    # ...and a neutral delegated question with no marker draws nothing.
    out2 = _read(monkeypatch, payload, (_ASSET, _VX),
                 delegated="is line 2 going to miss today?", wants=None)
    assert out2 == _chart_free(payload)


# --------------------------------------------------------------------------- #
# All-unnamed asked-for chart still delivers the count, no id
# --------------------------------------------------------------------------- #
def test_all_unnamed_asked_for_chart_delivers_notice_not_silence(monkeypatch):
    _graph(monkeypatch, {})                                   # nothing resolves
    payload = {f"{_UUID_ASSET}_{_UUID_PROP}": _series(6)}
    out = _read(monkeypatch, payload, (_UUID_ASSET, _UUID_PROP),
                delegated="plot the vibration", wants=True)
    text = _chart_text(out)
    assert "1 series not charted: no name recorded" in text
    assert _UUID_ASSET[:8] not in text and _UUID_PROP[:8] not in text and "…" not in text
    # The notice rides the chart-tag delivery path (a chart-block tag), so it
    # crosses the query boundary deterministically like any chart.
    assert _TAG.search(text) is not None


def test_all_unnamed_but_no_intent_still_draws_nothing(monkeypatch):
    """No chart was asked for: not even the notice is delivered."""
    _graph(monkeypatch, {})
    payload = {f"{_UUID_ASSET}_{_UUID_PROP}": _series(6)}
    out = _read(monkeypatch, payload, (_UUID_ASSET, _UUID_PROP),
                delegated="is line 2 going to miss", wants=False)
    assert out == _chart_free(payload)


# --------------------------------------------------------------------------- #
# Names-only: ids, hex, numeric ids and hostile units never leak
# --------------------------------------------------------------------------- #
def test_numeric_id_name_is_rejected(monkeypatch):
    """A resolved name that is a long bare-digit id must not title a chart."""
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), _VX: ("111122223333", "mm/s")})
    payload = {f"{_ASSET}_{_VX}": _series(6)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="chart it", wants=True)
    text = _chart_text(out)
    assert "111122223333" not in text
    assert "1 series not charted: no name recorded" in text


def test_hex_id_name_is_rejected(monkeypatch):
    """A long hex id passes `printable` as a plain identifier but is an id."""
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), _VX: ("a1b2c3d4e5f6a7b8", "mm/s")})
    payload = {f"{_ASSET}_{_VX}": _series(6)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="chart it", wants=True)
    text = _chart_text(out)
    assert "a1b2c3d4e5f6a7b8" not in text
    assert "1 series not charted: no name recorded" in text


def test_name_equal_to_source_id_is_rejected(monkeypatch):
    """A resolved name that equals the channel's own source id is an id."""
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), _VX: (_VX, "mm/s")})
    payload = {f"{_ASSET}_{_VX}": _series(6)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="chart it", wants=True)
    text = _chart_text(out)
    assert "1 series not charted: no name recorded" in text


def test_hostile_or_id_shaped_unit_is_dropped_from_title(monkeypatch):
    """A unit that is id-shaped or non-printable is omitted, not printed."""
    _graph(monkeypatch, {_ASSET: ("Unit A", ""),
                         _VX: ("vib_x", "9a8b7c6d-1111-4222-8333-444455556666")})
    payload = {f"{_ASSET}_{_VX}": _series(6)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="chart it", wants=True)
    text = _chart_text(out)
    assert "9a8b7c6d" not in text
    assert "Unit A: vib_x" in text            # drawn, just no unit suffix
    assert "(9a8b" not in text


def test_a_clean_short_unit_survives(monkeypatch):
    _graph(monkeypatch, {_ASSET: ("Unit A", ""), _VX: ("vib_x", "mm/s")})
    payload = {f"{_ASSET}_{_VX}": _series(6)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="chart it", wants=True)
    assert "Unit A: vib_x (mm/s)" in _chart_text(out)


def test_a_numeric_suffix_in_a_human_name_survives(monkeypatch):
    """`Line 2` is a human name; its bare-number suffix is allowed."""
    _graph(monkeypatch, {_ASSET: ("Line 2", ""), _VX: ("vib_x", "mm/s")})
    payload = {f"{_ASSET}_{_VX}": _series(6)}
    out = _read(monkeypatch, payload, (_ASSET, _VX),
                delegated="chart it", wants=True)
    assert "Line 2: vib_x (mm/s)" in _chart_text(out)
