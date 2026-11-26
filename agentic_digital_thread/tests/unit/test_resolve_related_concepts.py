# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Resolve measures field pairs across concepts the vocabulary declares related.

``onboard._do_resolve`` only measures (samples + overlap) field pairs whose
concepts match. The field mapper can file one person column under either of two
concepts the vocabulary defines as the same kind of thing at different breadth
(a technician login mapped ``personnel`` in one run, ``operator`` in another).
When the new column lands on one and its real partner on the other, the pair was
never measured, the model never saw it, and the identity rule appeared in some
runs and not others. ``catalog.NARROWER_CONCEPTS`` declares those pairs; resolve
measures across them exactly like a same-concept pair. A declared relation is
never evidence by itself: an unmeasured or non-overlapping pair is still dropped
by the evidence gates.

Fixtures are synthetic (``tech_*`` logins, ``sys-*`` ids).
"""
from unittest.mock import patch

import pytest

_NEW = "sys-maint"
_PEER = "sys-exec"


def _catalog_get(sid):
    return {
        _NEW: {"schema": "maint", "connection_type": "PostgreSQL"},
        _PEER: {"schema": "exec", "connection_type": "PostgreSQL"},
    }.get(sid, {})


def _peer_fields(concept="operator"):
    return [{"sys_id": _PEER, "sys_name": "Exec", "sys_schema": "exec",
             "field_name": "closed_by", "concept": concept, "table_name": "stop_events",
             "role": "correlate"}]


def _nq(existing):
    def _query(query, params=None):
        if "IDENTIFIED_BY" in query:
            return []
        return existing
    return _query


def _samples(new_values, peer_values):
    def _sample(**kw):
        if kw.get("system_id") == _NEW and kw.get("field") == "author":
            return list(new_values)
        if kw.get("system_id") == _PEER and kw.get("field") == "closed_by":
            return list(peer_values)
        return []
    return _sample


_LOGINS = ["tech_01", "tech_02", "tech_03", "lead_a"]


def _run(new_concept, peer_concept, new_values=_LOGINS, peer_values=_LOGINS):
    from agents.models import OnboardResolveRequest
    from agents.onboard import _do_resolve
    body = OnboardResolveRequest(system_id=_NEW, mappings=[
        {"table": "log_entries", "field": "author", "concept": new_concept, "confidence": 0.8},
    ])
    with patch("agents.onboard.neptune_query", side_effect=_nq(_peer_fields(peer_concept))), \
            patch("agents.onboard.catalog") as cat, \
            patch("agents.onboard.bedrock_call",
                  return_value='{"rules": [], "value_mappings": []}') as llm, \
            patch("agents.connection_resolver.resolve_connection", return_value={}), \
            patch("agents.resolve_evidence.sample_field_values",
                  side_effect=_samples(new_values, peer_values)):
        cat.get.side_effect = _catalog_get
        result = _do_resolve(body)
    prompt = llm.call_args[0][0] if llm.call_args else ""
    return result, prompt, llm.call_count


@pytest.mark.parametrize("new_concept,peer_concept",
                         [("personnel", "operator"), ("operator", "personnel")])
def test_related_concept_pair_is_measured_in_both_directions(new_concept, peer_concept):
    result, prompt, calls = _run(new_concept, peer_concept)
    assert calls == 1
    ev = [e for e in result["evidence"]
          if e["source_field"] == "log_entries.author" and e["target_field"] == "stop_events.closed_by"]
    assert ev, result["evidence"]
    assert ev[0]["target_system"] == _PEER
    assert ev[0]["overlap_count"] == 4
    # The model is shown the pair and told the two concepts are declared related.
    assert "stop_events.closed_by" in prompt
    assert "declared related" in prompt


def test_same_concept_pair_unchanged():
    result, prompt, calls = _run("operator", "operator")
    assert calls == 1
    assert [e["overlap_count"] for e in result["evidence"]] == [4]
    assert "declared related" not in prompt


def test_unrelated_concepts_are_still_not_paired():
    """A concept pair the vocabulary does NOT declare related is not measured:
    resolve returns before any model call, as before."""
    result, _prompt, calls = _run("personnel", "supplier")
    assert calls == 0
    assert result["rules"] == []


def test_related_pair_without_overlap_adds_no_evidence():
    """The relation only decides what is measured; values still have to agree."""
    result, _prompt, calls = _run("personnel", "operator",
                                  new_values=["tech_01", "tech_02"], peer_values=["OP-7", "OP-8"])
    assert calls == 1
    assert result["evidence"] == []


def test_coverage_gap_recommendations_ignore_related_targets():
    """``overlap_by_concept`` (coverage-gap recommendations) stays keyed to
    same-concept measurements: a related-only pair neither creates nor
    suppresses a recommendation for the new field's concept."""
    # _do_resolve imports it from resolve_evidence at call time.
    with patch("agents.resolve_evidence.coverage_gap_recommendations",
               return_value=[]) as gaps:
        _run("personnel", "operator")
    assert gaps.call_args[0][0] == {}


def test_declared_pairs_name_distinct_core_concepts():
    from agents.catalog import CORE_CONCEPT_DEFINITIONS, NARROWER_CONCEPTS, comparable_concepts
    assert ("operator", "personnel") in NARROWER_CONCEPTS
    for narrower, broader in NARROWER_CONCEPTS:
        assert narrower != broader
        assert narrower in CORE_CONCEPT_DEFINITIONS and broader in CORE_CONCEPT_DEFINITIONS
    assert comparable_concepts("operator") == frozenset({"operator", "personnel"})
    assert comparable_concepts("personnel") == frozenset({"personnel", "operator"})
    assert comparable_concepts("supplier") == frozenset({"supplier"})
    assert comparable_concepts("") == frozenset()


# ---------------------------------------------------------------- through the real gates

class _SitedCatalog:
    """Ids, names and plants, as the site gate reads them (``get``, ``get_all``)."""

    _ENTRIES = {
        _NEW: {"id": _NEW, "name": "Maint", "plant": "Plant A", "system_type": "CMMS",
               "schema": "maint", "connection_type": "PostgreSQL"},
        _PEER: {"id": _PEER, "name": "Exec A", "plant": "Plant A", "system_type": "MES",
                "schema": "exec", "connection_type": "PostgreSQL"},
        "sys-exec-b": {"id": "sys-exec-b", "name": "Exec B", "plant": "Plant B",
                       "system_type": "MES", "schema": "exec_b", "connection_type": "PostgreSQL"},
    }

    def get(self, sid):
        return self._ENTRIES.get(sid)

    def get_all(self):
        return list(self._ENTRIES.values())


def test_resolve_job_gates_a_related_concept_rule(monkeypatch):
    """The model proposes the person-column rule to the same plant's system,
    another plant's, and a weakly overlapping column. The real evidence gate
    and site gate decide: same plant kept (four shared logins), other plant
    withheld, two shared values rejected."""
    import copy

    from agents import onboard, self_learning
    from agents.models import OnboardResolveRequest

    peers = [
        {"sys_id": _PEER, "sys_name": "Exec A", "sys_schema": "exec", "field_name": "closed_by",
         "concept": "operator", "table_name": "stop_events", "role": "correlate"},
        {"sys_id": _PEER, "sys_name": "Exec A", "sys_schema": "exec", "field_name": "inspector",
         "concept": "operator", "table_name": "checks", "role": "correlate"},
        {"sys_id": "sys-exec-b", "sys_name": "Exec B", "sys_schema": "exec_b",
         "field_name": "closed_by", "concept": "operator", "table_name": "stop_events",
         "role": "correlate"},
    ]

    def _query(query, params=None):
        if "HAS_FIELD" in query and "IDENTIFIED_BY" not in query:
            return copy.deepcopy(peers)
        return []

    def _sample(**kw):
        sid, field = kw.get("system_id"), kw.get("field")
        if sid == _NEW and field == "author":
            return list(_LOGINS)
        if field == "closed_by":
            return list(_LOGINS)
        if sid == _PEER and field == "inspector":
            return ["tech_01", "tech_02", "qa_9", "qa_8", "qa_7"]
        return []

    def _rule(target, field):
        return {"concept": "personnel", "source_field": "log_entries.author",
                "source_pattern": "{login}", "target_system": target, "target_field": field,
                "target_pattern": "{login}", "confidence": 0.8, "reasoning": "shared logins"}

    model = {"rules": [_rule(_PEER, "stop_events.closed_by"), _rule(_PEER, "checks.inspector"),
                       _rule("sys-exec-b", "stop_events.closed_by")], "value_mappings": []}
    import json as _json
    monkeypatch.setattr(onboard, "catalog", _SitedCatalog())
    monkeypatch.setattr(onboard, "neptune_query", _query)
    monkeypatch.setattr(onboard, "bedrock_call", lambda prompt: _json.dumps(model))
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", lambda body, result: result)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": [], "crosswalks": []})
    monkeypatch.setattr("agents.resolve_evidence.sample_field_values", _sample)
    monkeypatch.setattr("agents.connection_resolver.resolve_connection", lambda sid: {})
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: _SitedCatalog().get_all())
    body = OnboardResolveRequest(system_id=_NEW, mappings=[
        {"table": "log_entries", "field": "author", "concept": "personnel", "confidence": 0.8},
    ])
    onboard._run_resolve_job("job-related", body)
    job = onboard._resolve_jobs.pop("job-related")
    assert job["status"] == "done", job
    result = job["result"]
    assert [(r["target_system"], r["target_field"]) for r in result["rules"]] == [
        (_PEER, "stop_events.closed_by")]
    reasons = {(r["target_system"], r["target_field"]): r["rejected_reason"]
               for r in result["rejected_rules"]}
    assert reasons[(_PEER, "checks.inspector")].startswith("only 2 shared value(s)")
    assert reasons[("sys-exec-b", "stop_events.closed_by")].startswith(
        "different plants (Plant A, Plant B)")
