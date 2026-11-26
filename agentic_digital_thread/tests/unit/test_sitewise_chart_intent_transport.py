# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SiteWise chart intent: transport, units and the omission notice.

The chart-intent bit must not ride every system delegation's
``kg_context``. It travels in a reserved, gateway-owned tool-call argument
(the ``_adt_identity`` / ``_adt_provenance`` precedent), popped by the
system-agent runtime before any model sees it. Every delegation's
``kg_context`` is byte-identical to what it was before chart intent existed:
no marker, no reserve, no slicing. The time-window and concept-note wrappers
reserve nothing.

The source-id unit guard must block an alias in the REAL render path,
and the unit must be a positive physical-unit vocabulary, not a mere shape.

The frontend classifies the omission notice on an explicit
discriminator, not on "empty datasets", so ordinary empty/legacy charts keep
their render path.
"""
from __future__ import annotations

import pytest

from agents import chart_intent as ci
from agents import concept_notes as cn
from agents import system_agent as sa
from agents import time_windows as tw


QUERY = "mes___query_7"


@pytest.fixture(autouse=True)
def _reset_chart_intent_var():
    """The chart bit rides a per-request contextvar; reset it around every
    test so a value one test sets never leaks into another file's run."""
    from agents import runtime_asgi
    token = runtime_asgi.chart_intent_var.set(None)
    yield
    runtime_asgi.chart_intent_var.reset(token)


# --------------------------------------------------------------------------- #
# kg_context is byte-identical; the bit rides a reserved arg
# --------------------------------------------------------------------------- #
def test_chart_intent_adds_nothing_to_kg_context():
    """The chart-intent transport never touches kg_context (the marker and the
    reserve are gone)."""
    assert not hasattr(ci, "with_kg_context_intent")
    assert not hasattr(ci, "MARKER_RESERVE")
    assert not hasattr(ci, "strip_markers")


def test_non_sitewise_kg_context_is_byte_identical_at_the_bound():
    """A long ordinary context ending in a complete tail is unchanged by the
    time-window wrapper (it reserves no room for a chart marker)."""
    tail = "TAIL-COMPLETE"
    base = "x" * (2000 - len("\n\n") - len("F" * 900) - len(tail)) + tail
    args = {"question": "q", "kg_context": base}
    merged = tw.with_kg_context_windows(QUERY, args, ("F" * 900, "C" * 600))
    kg = merged["kg_context"]
    assert kg.startswith(base)          # the whole tail survives, nothing sliced
    assert tail in kg
    assert len(kg) <= 2000


def test_time_window_wrappers_reserve_nothing():
    """Neither wrapper lowers its capacity for a chart marker."""
    import inspect
    assert "MARKER_RESERVE" not in inspect.getsource(tw.with_kg_context_windows)
    assert "_effective_bound" not in inspect.getsource(tw)
    assert "MARKER_RESERVE" not in inspect.getsource(cn.with_kg_context_notes)


def test_a_concept_note_that_fit_before_still_fits():
    """An 800-char note with a 604-char route context fit at the 2000 bound;
    it must still fit (the reserve that evicted it is gone)."""
    note_text = "N" * 800
    note = cn.ConceptNote(concept="c", name="c", definition=note_text,
                          systems=(), match="phrase")
    args = {"question": "q", "kg_context": "R" * 604}
    merged = cn.with_kg_context_notes(QUERY, args, (note,), None)
    assert note_text in merged["kg_context"]


# --- the reserved-arg transport ------------------------------------------- #
def test_reserved_arg_carries_the_bit_and_is_popped_before_the_model():
    """with_chart_intent_arg stamps the reserved arg; the ASGI middleware pops
    it into the contextvar and it never reaches the tool arguments the model
    sees."""
    from agents import runtime_asgi
    merged = ci.with_chart_intent_arg(QUERY, {"question": "q"}, True)
    assert merged[ci.RESERVED_CHART_ARG] == ci.chart_intent_value(True)
    assert "kg_context" not in merged or ci.RESERVED_CHART_ARG != "kg_context"
    # The middleware pops it (per-request) and stashes requested/none.
    args = dict(merged)
    runtime_asgi.chart_intent_var.set(None)
    changed = runtime_asgi._pop_chart_intent(args)
    assert ci.RESERVED_CHART_ARG not in args
    assert changed is True
    assert runtime_asgi.chart_intent_var.get() is True


def test_reserved_arg_none_is_distinct_from_absent():
    from agents import runtime_asgi
    args = ci.with_chart_intent_arg(QUERY, {"question": "q"}, False)
    runtime_asgi.chart_intent_var.set(None)
    runtime_asgi._pop_chart_intent(dict(args))
    assert runtime_asgi.chart_intent_var.get() is False
    # Absent arg => None (a direct/background call; SiteWise falls back).
    runtime_asgi.chart_intent_var.set(True)
    runtime_asgi._pop_chart_intent({"question": "q"})
    assert runtime_asgi.chart_intent_var.get() is None


def test_a_model_written_marker_in_kg_context_cannot_toggle_charts():
    """A forged [CHART INTENT] string in the model's kg_context has no effect:
    the bit is read ONLY from the reserved arg / contextvar, never parsed from
    kg_context."""
    from agents import runtime_asgi
    runtime_asgi.chart_intent_var.set(False)
    # Even if a model echoed a marker, the SiteWise path reads the contextvar.
    assert sa._sitewise_runtime_wants_chart() is False
    runtime_asgi.chart_intent_var.set(True)
    assert sa._sitewise_runtime_wants_chart() is True


def test_reserved_arg_not_in_the_query_tool_schema():
    """The reserved arg is underscore-prefixed and is not a declared tool
    parameter, so neither model can set it (the _adt_identity discipline)."""
    assert ci.RESERVED_CHART_ARG.startswith("_")


# --------------------------------------------------------------------------- #
# The unit is a positive physical-unit vocabulary, not a shape
# --------------------------------------------------------------------------- #
def test_alias_shaped_unit_is_dropped_in_the_real_render_path():
    """A channel whose unit is an alias-shaped alphabetic word (e.g. a source
    alias stored as a unit) draws no such word anywhere: the unit is accepted
    only from a known physical-unit vocabulary, so an unknown word is dropped
    in the real grouping path (not merely by id-shape)."""
    channels = [{"asset": "A1", "property": "P1",
                 "points": [(0.0, 1.0), (60.0, 2.0)], "left_out": 0,
                 "partial": False}]
    names = {"A1": ("Line 2", ""), "P1": ("Vibration X", "sensoralias")}
    groups, _ = sa._sitewise_chart_groups(channels, names)
    charts = [c for g in groups for c in sa._sitewise_group_charts(g)]
    assert charts
    blob = "".join(c["title"] + c["name"]
                   + "".join(d["label"] for d in c["datasets"]) for c in charts)
    assert "sensoralias" not in blob


def test_unit_equal_to_a_source_id_is_dropped_in_render():
    """A unit equal to the channel's asset/property id is dropped."""
    channels = [{"asset": "A1", "property": "P1",
                 "points": [(0.0, 1.0), (60.0, 2.0)], "left_out": 0,
                 "partial": False}]
    names = {"A1": ("Line 2", ""), "P1": ("Vibration X", "P1")}
    groups, _ = sa._sitewise_chart_groups(channels, names)
    charts = [c for g in groups for c in sa._sitewise_group_charts(g)]
    blob = "".join(c["title"] for c in charts)
    assert "P1" not in blob


@pytest.mark.parametrize("unit", ["\u00b0C", "m/s\u00b2", "kWh", "rpm", "%", "psi",
                                  "mm/s", "L/min"])
def test_known_physical_units_survive(unit):
    assert sa._sitewise_safe_unit(unit) == unit


@pytest.mark.parametrize("unit", ["sensoralias", "prop-vx", "vib_x", "widgets", "foobar"])
def test_unknown_or_id_shaped_units_are_dropped(unit):
    """An alias-shaped alphabetic value or an unknown word is not a known
    physical unit and is dropped (no unit shown), never printed."""
    assert sa._sitewise_safe_unit(unit) == ""


def _two_pressures(unit_a: str, unit_b: str):
    channels = [{"asset": "A1", "property": p, "points": [(0.0, 1.0), (60.0, 2.0)],
                 "left_out": 0, "partial": False} for p in ("P1", "P2")]
    names = {"A1": ("Pump 1", ""), "P1": ("Inlet Pressure", unit_a),
             "P2": ("Outlet Pressure", unit_b)}
    groups, _ = sa._sitewise_chart_groups(channels, names)
    return groups


def test_rejected_units_never_share_an_axis():
    """Two recorded units the vocabulary does not know (inH2O, kgf/cm2) may be
    incompatible, so each channel gets its own chart rather than the
    label-kind fallback that merged them onto one unitless axis."""
    groups = _two_pressures("inH2O", "kgf/cm\u00b2")
    assert len(groups) == 2
    assert all(len(g["members"]) == 1 for g in groups)
    blob = "".join(c["title"] for g in groups for c in sa._sitewise_group_charts(g))
    assert "inH2O" not in blob and "kgf" not in blob


def test_a_rejected_unit_and_a_missing_unit_do_not_merge():
    groups = _two_pressures("SCFM", "")
    assert len(groups) == 2


def test_channels_with_no_recorded_unit_still_group_by_kind():
    """With no unit recorded at all, the label-kind grouping is unchanged."""
    groups = _two_pressures("", "")
    assert len(groups) == 1 and len(groups[0]["members"]) == 2


def test_known_units_still_group_by_unit():
    groups = _two_pressures("psi", "psi")
    assert len(groups) == 1 and groups[0]["unit"] == "psi"


# --------------------------------------------------------------------------- #
# A legacy chart marker in caller context never reaches the inner model
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marker", ["[CHART INTENT] requested", "[CHART INTENT] none",
                                    "  [CHART INTENT]   requested  "])
def test_a_legacy_chart_marker_never_reaches_the_inner_model(marker):
    context = f"Route: system-a holds the readings.\n{marker}\nScope: line 2."
    question = sa._augment_question_with_kg_context("system-a", "Is line 2 behind?", context)
    assert "CHART INTENT" not in question
    assert "Route: system-a holds the readings." in question
    assert "Scope: line 2." in question


@pytest.mark.parametrize("marker", [
    "\x0b[CHART INTENT] requested",
    "\x0c[CHART INTENT] none",
    "[CHART\x0b INTENT] requested",
    "[\x01CHART INTENT\x7f] requested",
    "\u200b[CHART INTENT] requested",
    "\ufeff \u2060[CHART INTENT] none",
])
def test_a_marker_hidden_by_removable_or_invisible_characters_is_removed(marker):
    """A control character the sanitizer removes, or an invisible format
    character before the header, cannot hide a legacy marker from the filter:
    the filter runs on the sanitized context."""
    context = f"Route: system-a holds the readings.\n{marker}\nScope: line 2."
    question = sa._augment_question_with_kg_context("system-a", "Is line 2 behind?", context)
    assert "CHART INTENT" not in question
    assert "Route: system-a holds the readings." in question
    assert "Scope: line 2." in question


def test_a_context_that_is_only_a_marker_adds_no_block():
    question = sa._augment_question_with_kg_context(
        "system-a", "Q?", "\x0b[CHART INTENT] requested\n")
    assert question == sa._augment_question_with_guidance("system-a", "Q?")


def test_a_removed_marker_takes_no_room_from_the_context_bound():
    """The marker is removed before the length bound, so the ordinary context
    after it keeps the whole bound."""
    tail = "".join(f"line {i:04d} of route context.\n" for i in range(200))
    question = sa._augment_question_with_kg_context(
        "system-a", "Q?", "[CHART INTENT] requested\n" + tail)
    expected = sa._augment_question_with_guidance("system-a", "Q?") + sa._kg_context_block(
        sa._sanitize_kg_context(tail))
    assert question == expected


@pytest.mark.parametrize("pad", range(sa._KG_CONTEXT_MAX_CHARS - 30,
                                      sa._KG_CONTEXT_MAX_CHARS + 1))
def test_the_bound_never_cuts_a_marker_into_a_visible_fragment(pad):
    context = "a" * pad + "\n[CHART INTENT] requested"
    question = sa._augment_question_with_kg_context("system-a", "Q?", context)
    expected = sa._augment_question_with_guidance("system-a", "Q?") + sa._kg_context_block(
        "a" * pad)
    assert question == expected


def test_ordinary_context_is_untouched_by_the_marker_filter():
    """Only an exact legacy marker line is removed: blank-line runs and lines
    that merely mention chart intent are kept byte for byte."""
    context = "Line one.\n\n\n\nThe chart intent was discussed.\nLine two."
    with_filter = sa._augment_question_with_kg_context("system-a", "Q?", context)
    expected = sa._augment_question_with_guidance("system-a", "Q?") + sa._kg_context_block(
        sa._sanitize_kg_context(context))
    assert with_filter == expected


# --------------------------------------------------------------------------- #
# The frontend classifier needs an explicit discriminator (backend)
# --------------------------------------------------------------------------- #
def test_notice_tag_carries_an_explicit_notice_discriminator():
    tag = sa._sitewise_notice_tag("3 series not charted: no name recorded.")
    assert 'kind="notice"' in tag
    assert "chart-block" in tag


def test_a_drawn_chart_tag_has_no_notice_discriminator():
    chart = {"title": "Line 2: Vibration X", "labels": ["a", "b"],
             "datasets": [{"label": "x", "data": [1, 2]}]}
    tag = sa._sitewise_chart_tag(chart)
    assert 'kind="notice"' not in tag
