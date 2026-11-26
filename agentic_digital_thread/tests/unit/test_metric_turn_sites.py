# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0094 — a defined metric whose authority is held PER SITE is answered
per site, not withheld.

Fully offline. The resolver is stubbed at ``metric_resolver.resolve_metric``
(default authority ⇒ the typed ambiguity; a ``PinnedAuthority`` ⇒ that
site's value), the catalog at ``catalog.get_onboarded`` and the matcher at
``metric_term_resolution.defined_metric_terms``. Pinned:

* only a per-site PARTITION fans out (every system onboarded, sited, at a
  different site, 2..MAX_SITES); any other ambiguity keeps the existing
  ``unavailable`` withhold verbatim;
* each site lands in the dashboard's register (computed / withheld /
  unavailable), never a combined value;
* the runtime re-sanitizes the per-site shape and fails CLOSED on a
  malformed site;
* the notice carries the per-site rule line and stays within its bound.

Fixture vocabulary is neutral ``widget_*`` placeholders only.
"""
from __future__ import annotations

import pytest

import agents.catalog as catalog_mod
import agents.metric_layer as ml
import agents.metric_resolver as mr
import agents.metric_term_resolution as mtr
import agents.metric_turn_notice as mtn
import agents.metric_turn_render as mtrender
import agents.metric_turn_sites as mts
from agents.metric_layer import Aggregation
from agents.metric_resolver import Lineage, QueryPlan, ResolvedMetric

_ID = "widget-yield"
_LABEL = "Widget Yield"
_SYSTEMS = [
    {"id": "wsys-b", "name": "Widget B", "plant": "Bravo", "status": "onboarded"},
    {"id": "wsys-a", "name": "Widget A", "plant": "Alpha", "status": "onboarded"},
    {"id": "wsys-c", "name": "Widget C", "plant": "Charlie", "status": "onboarded"},
]
_IDS = ("wsys-a", "wsys-b", "wsys-c")


class _Layer:
    def list_metrics(self):
        return []

    def is_graph_defined(self, metric_id):
        return False

    def get_metric(self, metric_id):
        raise KeyError(metric_id)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")
    monkeypatch.delenv("METRIC_TURN_TIMEBOX_SECONDS", raising=False)
    monkeypatch.delenv("METRIC_TURN_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.setattr(ml, "get_metric_layer", lambda: _Layer())
    monkeypatch.setattr(mtr, "defined_metric_terms",
                        lambda _q, limit=mtr.MAX_METRIC_TERMS: [mtr.DefinedMetricMatch(
                            term="widget yield", metric_ids=(_ID,), label=_LABEL,
                            start=0, end=12)])
    monkeypatch.setattr(catalog_mod.catalog, "get_onboarded", lambda: list(_SYSTEMS))
    mtn.invalidate()
    yield
    mtn.invalidate()


def _ambiguity(ids=_IDS, concept="widget-good-count"):
    return mr.AmbiguousAuthorityError("ambiguous authority for concept "
                                      f"{concept!r}: {list(ids)}",
                                      concept=concept, system_ids=ids)


def _site_value(sid, value=95.5, samples=40, window_requested="7d", window="7d", **kw):
    plan = QueryPlan(system_id=sid, schema="public", table="widget_checks",
                     column="widget_pass", aggregation=Aggregation.AVG)
    lineage = Lineage(metric_id=_ID, kind="direct", query_plan=plan,
                      window_requested=window_requested, window=window)
    return ResolvedMetric(metric_id=_ID, value=value, unit="%", label=_LABEL,
                          query_plan=plan, system_id=sid, lineage=lineage,
                          sample_count=samples, **kw)


def _resolver(monkeypatch, scalar_exc, per_site, calls=None):
    """Default authority ⇒ ``scalar_exc``; a pin ⇒ ``per_site[sid]`` (an
    exception is raised, a ResolvedMetric returned)."""
    def _resolve(metric_id, *, authority=None):
        if authority is None:
            raise scalar_exc
        sid = authority.authoritative_system("any")
        if calls is not None:
            calls.append(sid)
        mr._note_execution(sid)
        outcome = per_site[sid]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(mr, "resolve_metric", _resolve)


def _one():
    results = mtn.results_for_turn("q")
    assert len(results) == 1
    return results[0]


# --------------------------------------------------------------------------- #
# The typed ambiguity
# --------------------------------------------------------------------------- #
def test_graph_authority_raises_typed_ambiguity_naming_systems(monkeypatch):
    monkeypatch.setattr(mr, "neptune_query", lambda *_a, **_k: [
        {"sid": "wsys-b"}, {"sid": "wsys-a"}, {"sid": "wsys-a"}])
    with pytest.raises(mr.AmbiguousAuthorityError) as info:
        mr.GraphAuthorityResolver().authoritative_system("widget-good-count")
    assert isinstance(info.value, mr.MetricResolverError)
    assert info.value.concept == "widget-good-count"
    assert info.value.system_ids == ("wsys-a", "wsys-b")
    assert "ambiguous authority for concept 'widget-good-count'" in str(info.value)


def test_ambiguity_is_found_through_a_derived_wrapper():
    inner = _ambiguity()
    try:
        try:
            raise inner
        except mr.MetricResolverError as cause:
            raise mr.MetricResolverError("derived metric unavailable: input") from cause
    except mr.MetricResolverError as outer:
        assert mts.ambiguous_authority_in(outer) is inner
    assert mts.ambiguous_authority_in(mr.MetricResolverError("other")) is None


# --------------------------------------------------------------------------- #
# Partition rule
# --------------------------------------------------------------------------- #
def test_partition_is_sorted_by_site():
    assert mts.site_partition(["wsys-c", "wsys-a", "wsys-b"], _SYSTEMS) == [
        {"site": "Alpha", "system_id": "wsys-a"},
        {"site": "Bravo", "system_id": "wsys-b"},
        {"site": "Charlie", "system_id": "wsys-c"}]


@pytest.mark.parametrize("systems, ids", [
    ([{**_SYSTEMS[0], "plant": "all"}, *_SYSTEMS[1:]], _IDS),        # unsited
    ([{**_SYSTEMS[0], "plant": ""}, *_SYSTEMS[1:]], _IDS),           # no plant
    ([{**_SYSTEMS[0], "plant": "alpha"}, *_SYSTEMS[1:]], _IDS),      # same site
    (_SYSTEMS, ("wsys-a", "wsys-b", "wsys-unknown")),                 # not onboarded
    (_SYSTEMS, ("wsys-a",)),                                          # one system
])
def test_non_partitions_are_refused(systems, ids):
    assert mts.site_partition(ids, systems) is None


def test_more_sites_than_the_cap_is_refused():
    many = [{"id": f"wsys-{i}", "plant": f"Site {i}", "status": "onboarded"}
            for i in range(mts.MAX_SITES + 1)]
    assert mts.site_partition([s["id"] for s in many], many) is None
    assert mts.site_partition([s["id"] for s in many[:mts.MAX_SITES]], many)


# --------------------------------------------------------------------------- #
# App half: results_for_turn
# --------------------------------------------------------------------------- #
def test_per_site_partition_is_answered_per_site(monkeypatch):
    calls = []
    _resolver(monkeypatch, _ambiguity(), {
        "wsys-a": _site_value("wsys-a", 97.25),
        "wsys-b": _site_value("wsys-b", 91.5),
        "wsys-c": _site_value("wsys-c", 88.0, samples=12)}, calls)
    r = _one()
    assert r["outcome"] == mts.SITES_OUTCOME and r["metric_id"] == _ID
    assert r["concept"] == "widget-good-count"
    assert calls == ["wsys-a", "wsys-b", "wsys-c"]
    assert [(s["site"], s["system_id"], s["outcome"], s["value"]) for s in r["sites"]] == [
        ("Alpha", "wsys-a", "computed", 97.25), ("Bravo", "wsys-b", "computed", 91.5),
        ("Charlie", "wsys-c", "computed", 88.0)]
    assert r["query_count"] == 3 and r["systems"] == list(_IDS)
    assert "value" not in r            # no combined value, by construction


def test_per_site_block_renders_each_site_and_the_rule(monkeypatch):
    _resolver(monkeypatch, _ambiguity(), {
        "wsys-a": _site_value("wsys-a", 97.25),
        "wsys-b": _site_value("wsys-b", None, samples=0),
        "wsys-c": mr.MetricResolverError("no MAPS_TO field for concept 'widget-good-count'")})
    notice = mtn.render_metric_layer_notice([_one()])
    assert ("- 'widget yield' → metric 'widget-yield' (\"Widget Yield\", direct, "
            "code-defined): COMPUTED PER SITE.") in notice
    assert ("  authority: per site (3 sites, concept 'widget-good-count') | "
            "window: 7d (declared) | lineage: <site system>.widget_checks.widget_pass "
            "(avg) | queries: 3") in notice
    assert "  Alpha (wsys-a): 97.25 % | samples: 40" in notice
    assert "  Bravo (wsys-b): WITHHELD — status 'no-data': " in notice
    assert ("  Charlie (wsys-c): WITHHELD — status 'unavailable': no MAPS_TO field "
            "for concept 'widget-good-count'") in notice
    assert mts.SITES_RULE_LINE in notice and mtn.PERMITTED_LINE in notice
    assert notice.endswith(mtrender.WINDOW_SCOPE_SENTENCE)


def test_differing_windows_and_lineage_are_stated_per_site(monkeypatch):
    other = _site_value("wsys-b", 91.5, window="7d")
    other = ResolvedMetric(**{**other.__dict__, "lineage": Lineage(
        metric_id=_ID, kind="direct", window_requested="7d", window=None,
        query_plan=QueryPlan(system_id="wsys-b", schema="public", table="widget_log",
                             column="widget_ok", aggregation=Aggregation.AVG)),
        "query_plan": QueryPlan(system_id="wsys-b", schema="public", table="widget_log",
                                column="widget_ok", aggregation=Aggregation.AVG)})
    _resolver(monkeypatch, _ambiguity(("wsys-a", "wsys-b")), {
        "wsys-a": _site_value("wsys-a", 97.25), "wsys-b": other})
    notice = mtn.render_metric_layer_notice([_one()])
    assert "lineage: <site system>" not in notice
    assert ("  Bravo (wsys-b): 91.5 % | window: 7d declared, applied as lifetime "
            "(no event-time mapping on the source) | samples: 40 | lineage: "
            "wsys-b.widget_log.widget_ok (avg)") in notice
    assert notice.endswith(mtrender.MIXED_SCOPE_SENTENCE)


def test_a_genuine_conflict_keeps_the_verbatim_withhold(monkeypatch):
    exc = _ambiguity(("wsys-a", "wsys-x"))
    _resolver(monkeypatch, exc, {})
    r = _one()
    assert (r["outcome"], r["status"]) == ("withheld", "unavailable")
    assert r["detail"] == str(exc)


def test_derived_wrapper_still_fans_out(monkeypatch):
    def _wrapped():
        try:
            raise _ambiguity()
        except mr.MetricResolverError as cause:
            raise mr.MetricResolverError(
                "derived metric unavailable: input 'widget-good' unresolvable") from cause
    try:
        _wrapped()
    except mr.MetricResolverError as exc:
        wrapped = exc
    _resolver(monkeypatch, wrapped, {sid: _site_value(sid) for sid in _IDS})
    assert _one()["outcome"] == mts.SITES_OUTCOME


def test_a_fan_out_fault_keeps_the_original_withhold(monkeypatch):
    exc = _ambiguity()
    _resolver(monkeypatch, exc, {})

    def _boom():
        raise RuntimeError("catalog down")
    monkeypatch.setattr(catalog_mod.catalog, "get_onboarded", _boom)
    r = _one()
    assert (r["outcome"], r["status"], r["detail"]) == ("withheld", "unavailable", str(exc))


def test_a_non_resolver_site_error_names_only_its_class(monkeypatch):
    _resolver(monkeypatch, _ambiguity(), {
        "wsys-a": _site_value("wsys-a"), "wsys-b": _site_value("wsys-b"),
        "wsys-c": ConnectionError("host widget-db.internal refused")})
    site = _one()["sites"][2]
    assert site == {"site": "Charlie", "system_id": "wsys-c",
                    "outcome": "result_unavailable", "reason": "ConnectionError"}


# --------------------------------------------------------------------------- #
# Runtime half: sanitation fails closed
# --------------------------------------------------------------------------- #
def _wire(monkeypatch):
    _resolver(monkeypatch, _ambiguity(), {sid: _site_value(sid) for sid in _IDS})
    return _one()


def test_runtime_round_trip_is_stable(monkeypatch):
    r = _wire(monkeypatch)
    assert mtn.sanitize_results([r]) == [r]


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(sites=[]),
    lambda r: r.update(sites="nope"),
    lambda r: r.update(sites=r["sites"] * 3),                        # over the cap
    lambda r: r["sites"][0].update(site=""),
    lambda r: r["sites"][0].update(value=float("nan")),
    lambda r: r["sites"][0].update(outcome=mts.SITES_OUTCOME),        # nested
    lambda r: r["sites"][0].update(outcome="ambiguous_term"),
])
def test_a_malformed_per_site_result_fails_closed(monkeypatch, mutate):
    r = _wire(monkeypatch)
    mutate(r)
    assert mtn.sanitize_results([r]) == [mtrender.invalid_result()]


def test_hostile_site_text_is_bounded(monkeypatch):
    r = _wire(monkeypatch)
    r["sites"][0]["site"] = "Alpha\nIGNORE PREVIOUS " + "x" * 200
    clean = mtn.sanitize_results([r])[0]["sites"][0]["site"]
    assert "\n" not in clean and len(clean) == mtrender.MAX_FIELD_CHARS


def test_three_maximal_per_site_blocks_stay_within_the_bound(monkeypatch):
    big_sites = [{"site": f"S{i}" + "s" * 99, "system_id": f"w{i}" + "w" * 99,
                  "outcome": "withheld", "status": "unavailable", "detail": "d" * 999}
                 for i in range(mts.MAX_SITES)]
    big = mtn.sanitize_result({"outcome": mts.SITES_OUTCOME, "term": "t" * 99,
                               "metric_id": "m" * 999, "label": "l" * 999,
                               "concept": "c" * 999, "sites": big_sites})
    notice = mtn.render_metric_layer_notice([big] * 3)
    assert len(notice) <= mtn.MAX_NOTICE_CHARS
    assert notice.startswith(mtn.METRIC_LAYER_HEADER)
    assert "further result(s) omitted by the notice bound" in notice
    assert notice.endswith(mtrender.NO_VALUE_SCOPE_SENTENCE)
