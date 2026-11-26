# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0078 — resolver-affecting graph mutations drop the
per-turn result cache on EVERY exit path; over-bound questions and scheduled
prompts never reach the model without the notice.

Fully offline (every graph seam stubbed). Pinned:

* warm cache → ``MAPS_TO`` change (concept-mapping repoint, field-concept
  mint, steward remap, onboarding provision) → the NEXT turn resolves fresh,
  including when the mutation raises part-way;
* AuthorityRule soft deactivate / reactivate — found, not found or raising —
  drops the cache too;
* a question over the matcher bound yields a FORBIDDING ``input_overflow``
  block, never silence;
* a LOCAL scheduled prompt carries the same notice as a cloud one.

Fixture vocabulary is neutral ``widget_*`` placeholders only.
"""
from __future__ import annotations

import pytest

import agents.metric_layer as ml
import agents.metric_resolver as mr
import agents.metric_term_resolution as mtr
import agents.metric_turn_notice as mtn
from agents.metric_layer import Aggregation
from agents.metric_resolver import Lineage, QueryPlan, ResolvedMetric

_ID = "widget-defect-rate"
_LABEL = "Widget Defect Rate"
_GAP = "no onboarded system maps concept 'widget-defect-count' yet"
_REAL_DEFINED_METRIC_TERMS = mtr.defined_metric_terms


class _Layer:
    def list_metrics(self):
        return []

    def is_graph_defined(self, metric_id):
        return metric_id == _ID


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")
    monkeypatch.setattr(ml, "get_metric_layer", lambda: _Layer())
    match = mtr.DefinedMetricMatch(term="widget defect rate", metric_ids=(_ID,),
                                   label=_LABEL, start=0, end=18)
    monkeypatch.setattr(mtr, "defined_metric_terms",
                        lambda _q, limit=mtr.MAX_METRIC_TERMS: [match])
    mtn.invalidate()
    yield
    mtn.invalidate()


@pytest.fixture
def graph_state(monkeypatch):
    """A resolver whose answer flips once the 'mapping' exists."""
    state = {"mapped": False}

    def _resolve(metric_id, *, authority=None):
        if not state["mapped"]:
            raise mr.MetricResolverError(_GAP)
        plan = QueryPlan(system_id="widget-sys", schema="public", table="widget_lots",
                         column="widget_defects", aggregation=Aggregation.SUM)
        return ResolvedMetric(metric_id=_ID, value=4.0, unit="%", label=_LABEL,
                              query_plan=plan, system_id="widget-sys",
                              lineage=Lineage(metric_id=_ID, kind="direct",
                                              query_plan=plan), sample_count=9)
    monkeypatch.setattr(mr, "resolve_metric", _resolve)
    return state


def _outcome() -> str:
    return mtn.results_for_turn("widget defect rate?")[0]["outcome"]


def _warm(graph_state) -> None:
    assert _outcome() == "withheld"
    graph_state["mapped"] = True
    assert _outcome() == "withheld"          # cached: the TTL would serve it


def _raiser(message):
    def _raise(*_a, **_k):
        raise RuntimeError(message)
    return _raise


# --------------------------------------------------------------------------- #
# MAPS_TO mutations
# --------------------------------------------------------------------------- #
_REPOINT = {"to_system": "widget-sys", "concept": "widget-defect-count",
            "to_field": "widget_lots.widget_defects", "confidence": 1.0}


@pytest.mark.parametrize("fails", [False, True])
def test_concept_mapping_repoint_drops_the_turn_cache(monkeypatch, graph_state, fails):
    import agents.self_learning as sl
    _warm(graph_state)
    monkeypatch.setattr(sl, "_neptune_query", lambda *_a, **_k: [{"n": 1, "v": 1}])
    monkeypatch.setattr(sl, "_neptune_write_loud",
                        _raiser("partial") if fails else (lambda *_a, **_k: True))
    if fails:
        with pytest.raises(RuntimeError, match="partial"):
            sl._repoint_maps_to(dict(_REPOINT))
    else:
        sl._repoint_maps_to(dict(_REPOINT))
    assert _outcome() == "computed"


@pytest.mark.parametrize("fails", [False, True])
def test_field_concept_mint_drops_the_turn_cache(monkeypatch, graph_state, fails):
    import agents.self_learning as sl
    _warm(graph_state)
    monkeypatch.setattr(sl, "_neptune_write_loud",
                        _raiser("partial") if fails else (lambda *_a, **_k: True))
    item = {"concept": "widget-defect-count", "to_value": "Widget Defects",
            "to_system": "widget-sys", "to_field": "widget_lots.widget_defects"}
    if fails:
        with pytest.raises(RuntimeError, match="partial"):
            sl._apply_field_concept_mint(item, 1)
    else:
        sl._apply_field_concept_mint(item, 1)
    assert _outcome() == "computed"


@pytest.mark.parametrize("fails", ["", "write", "bump"])
def test_steward_remap_drops_the_turn_cache(monkeypatch, graph_state, fails):
    import agents.onboard as onboard
    from agents.models import RemapRequest
    _warm(graph_state)
    fid, cid = "widget-sys.widget_lots.widget_defects", "widget-defect-count"
    # A failure part-way (the edge write or the generation bump) answers an
    # error, not a raise.
    monkeypatch.setattr(onboard, "_bump_and_read_generation",
                        _raiser("partial") if fails == "bump" else (lambda _sid: (1, 1)))
    monkeypatch.setattr(onboard, "neptune_write_returning",
                        _raiser("partial") if fails == "write" else
                        (lambda *_a, **_k: [{"field_id": fid, "concept_id": cid}]))
    body = RemapRequest(remaps=[{"field_id": fid, "concept_id": cid,
                                 "role": "identifier", "confidence": 1.0}])
    result = onboard.onboard_remap(body)
    if fails == "write":
        assert result["reason"] == "remap_write_failed" and result["remapped"] == 0
    elif fails == "bump":
        assert result["reason"] == "mapping_generation_unbumped" and result["remapped"] == 1
    else:
        assert result == {"remapped": 1, "written": [fid], "not_written": []}
    assert _outcome() == "computed"


def test_provision_failing_before_c4_still_drops_the_turn_cache(monkeypatch, graph_state):
    """A partial onboarding (field mappings written, then a later phase
    raises) invalidates — the ``finally`` wraps the WHOLE provision."""
    import inspect
    import re

    import agents.onboard as onboard
    _warm(graph_state)
    src = inspect.getsource(onboard._do_provision_base)
    for name in set(re.findall(r"\b(_dp_\w+|_make_provision_state)\(", src)):
        monkeypatch.setattr(onboard, name, lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_dp_identity_key_edges", _raiser("b2c"))
    with pytest.raises(RuntimeError, match="b2c"):
        onboard._do_provision_base(object())
    assert _outcome() == "computed"


def test_every_resolver_affecting_writer_uses_the_one_invalidation_home():
    import agents.metric_authority as mauth
    import agents.metric_turn_invalidation as mti
    import agents.onboard as onboard
    import agents.self_learning as sl
    import agents.server as server
    for fn in (sl._repoint_maps_to, sl._apply_field_concept_mint,
               onboard.onboard_remap, onboard._do_provision_base,
               onboard._global_reset_wipe_graph,
               onboard._reset_delete_graph_artifacts_invalidating,
               server._set_rule_lifecycle, mauth.set_authority):
        assert getattr(fn, mti.MARKER_ATTR, False) is True, fn.__qualname__


# --------------------------------------------------------------------------- #
# AuthorityRule soft lifecycle
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("deactivate", [True, False])
@pytest.mark.parametrize("rows", ["found", "absent", "raise"])
def test_rule_lifecycle_drops_the_turn_cache(monkeypatch, graph_state, deactivate, rows):
    import agents.server as server
    _warm(graph_state)

    def _query(_cypher, params=None):
        if rows == "raise":
            raise RuntimeError("graph write failed")
        return [{"id": params["rid"]}] if rows == "found" else []
    monkeypatch.setattr(server, "neptune_query", _query)
    if rows == "raise":
        with pytest.raises(RuntimeError):
            server._set_rule_lifecycle("ar-widget-sys-widget-defect-count", "steward",
                                       deactivate=deactivate)
    else:
        result = server._set_rule_lifecycle("ar-widget-sys-widget-defect-count",
                                            "steward", deactivate=deactivate)
        assert ("error" in result) is (rows == "absent")
    assert _outcome() == "computed"


# --------------------------------------------------------------------------- #
# Over-bound questions fail CLOSED
# --------------------------------------------------------------------------- #
def test_oversized_question_yields_forbidding_input_overflow_block(monkeypatch, graph_state):
    def _too_long(_q, limit=mtr.MAX_METRIC_TERMS):
        raise mtr.MatcherInputTooLong(mtr.MAX_MATCH_INPUT_CHARS + 1)
    monkeypatch.setattr(mtr, "defined_metric_terms", _too_long)
    results = mtn.results_for_turn("x" * (mtr.MAX_MATCH_INPUT_CHARS + 1))
    assert results == [{"outcome": "input_overflow", "term": ""}]
    assert mtn.sanitize_results(results) == results      # survives receipt
    notice = mtn.render_metric_layer_notice(mtn.sanitize_results(results))
    assert notice.startswith(mtn.METRIC_LAYER_HEADER)
    assert mtn.INPUT_OVERFLOW_LINE in notice
    assert "Give NO number" in notice


def test_real_matcher_over_bound_reaches_the_forbidding_block(monkeypatch):
    monkeypatch.setattr(mtr, "defined_metric_terms", _REAL_DEFINED_METRIC_TERMS)
    results = mtn.results_for_turn("widget " * mtr.MAX_MATCH_INPUT_CHARS)
    assert [r["outcome"] for r in results] == ["input_overflow"]


# --------------------------------------------------------------------------- #
# Scheduled prompts: cloud and local compose the SAME notice
# --------------------------------------------------------------------------- #
def test_local_scheduled_prompt_carries_the_notice(monkeypatch, graph_state):
    import agents.orchestrator as orch
    import agents.server as server
    seen: list[str] = []

    class _Cfg:
        has_runtime = False
    monkeypatch.setattr(server, "get_config", lambda: _Cfg())
    monkeypatch.setattr(orch, "get_orchestrator",
                        lambda: (lambda prompt: seen.append(prompt) or "ok"))
    assert server._execute_prompt_admitted("widget defect rate this week?") == "ok"
    assert seen[0].startswith(mtn.METRIC_LAYER_HEADER)
    assert seen[0].endswith("\n\n---\n\nwidget defect rate this week?")
    assert f"reason: {_GAP}" in seen[0]


def test_local_scheduled_prompt_without_a_defined_metric_is_unchanged(monkeypatch):
    import agents.orchestrator as orch
    import agents.server as server
    seen: list[str] = []
    monkeypatch.setattr(mtr, "defined_metric_terms",
                        lambda _q, limit=mtr.MAX_METRIC_TERMS: [])

    class _Cfg:
        has_runtime = False
    monkeypatch.setattr(server, "get_config", lambda: _Cfg())
    monkeypatch.setattr(orch, "get_orchestrator",
                        lambda: (lambda prompt: seen.append(prompt) or "ok"))
    server._execute_prompt_admitted("which machines are down?")
    assert seen == ["which machines are down?"]


def test_scheduler_session_is_not_excluded_by_the_cloud_hook(graph_state):
    merged = mtn.extra_payload_with_results("widget defect rate?",
                                            "scheduler-0123456789abcdef", None)
    assert merged[mtn.METRIC_LAYER_RESULTS_KEY][0]["outcome"] == "withheld"


# --------------------------------------------------------------------------- #
# GLOBAL reset wipes the graph: cache must not outlive it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fail_at", ["none", "wipe", "reseed"])
def test_global_reset_graph_wipe_drops_the_turn_cache(monkeypatch, graph_state, fail_at):
    import agents.onboard as onboard
    _warm(graph_state)
    calls: list[str] = []

    def _write(cypher, params=None):
        calls.append(cypher)
        if fail_at == "wipe" and "DETACH DELETE" in cypher:
            raise RuntimeError("partial wipe")
        if fail_at == "reseed" and "DETACH DELETE" not in cypher:
            raise RuntimeError("seed failed")  # swallowed per-concept by design
        return True
    monkeypatch.setattr(onboard, "neptune_write", _write)
    monkeypatch.setattr(onboard, "core_concept_merges",
                        lambda: [("MERGE (c:Concept {id: $id})", {"id": "widget_unit"})])
    if fail_at == "wipe":
        with pytest.raises(RuntimeError):
            onboard._global_reset_wipe_graph()
    else:
        onboard._global_reset_wipe_graph()
        assert any("DETACH DELETE" in c for c in calls)
    assert _outcome() == "computed"


def test_global_reset_route_calls_the_decorated_wipe_off_the_event_loop():
    """The async route must not run graph writes inline (offload tripwire) and
    must reach the wipe ONLY through the decorated helper."""
    import inspect
    import agents.onboard as onboard
    src = inspect.getsource(onboard.onboard_reset)
    import agents.onboard as ob
    src = inspect.getsource(ob._global_reset_steps) + inspect.getsource(
        ob._global_reset_cascade) + src
    assert "to_thread(_global_reset_wipe_graph)" in src
    assert "DETACH DELETE" not in src
