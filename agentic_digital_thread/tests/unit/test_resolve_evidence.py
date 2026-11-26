# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the evidence-based, multi-source resolve generalization.

All fixtures use SYNTHETIC identifiers that mirror the SHAPE of real data
(nested-JSON values, dotted concepts, cross-system value overlap) without any
literal customer/dataset values. The live integration test reads its target
from env vars, so no real identifiers are hardcoded in this source.

Three layers:
  * UNIT — pure helpers (token extraction, overlap, recs) + sample dispatch.
  * COMPONENT-INTEGRATION — the real ``onboard._do_resolve`` wiring with only the
    external I/O boundaries mocked, asserting evidence + recommendations.
  * LIVE INTEGRATION (opt-in, env-driven) — sample_field_values against a real
    Athena workgroup supplied entirely via environment variables.
"""
import os
from unittest.mock import patch

import pytest

from agents import resolve_evidence as rev


# =========================================================================
# UNIT — extract_join_tokens
# =========================================================================

def test_extract_tokens_plain_scalar():
    assert rev.extract_join_tokens("ALPHA-1") == ["ALPHA-1"]


def test_extract_tokens_none_and_empty():
    assert rev.extract_join_tokens(None) == []
    assert rev.extract_join_tokens("") == []
    assert rev.extract_join_tokens("   ") == []


def test_extract_tokens_from_json_object_and_array():
    # Mirrors the shape of a source that serializes a nested struct as a JSON
    # blob: the join key ("ALPHA-1") is buried under a scalar leaf.
    blob = '[{"meta":{"count":1,"kind":"UNIT"},"label":"NODE-NAME-1","ref":"ALPHA-1"}]'
    toks = rev.extract_join_tokens(blob)
    assert "NODE-NAME-1" in toks and "ALPHA-1" in toks


def test_extract_tokens_dedup_preserves_order():
    assert rev.extract_join_tokens('["a","a","b"]') == ["a", "b"]


def test_extract_tokens_non_json_text_is_whole_value():
    assert rev.extract_join_tokens("not-json {oops") == ["not-json {oops"]


# =========================================================================
# UNIT — value_overlap
# =========================================================================

def test_overlap_matches_scalar_against_nested_json():
    ov = rev.value_overlap(["ALPHA-1", "OTHER"], ['[{"ref":"ALPHA-1"}]', "Z"])
    assert ov["overlap_count"] == 1
    assert ov["examples"] == ["ALPHA-1"]
    assert 0 < ov["ratio"] <= 1


def test_overlap_none_when_disjoint():
    ov = rev.value_overlap(["a", "b"], ["c", "d"])
    assert ov["overlap_count"] == 0
    assert ov["examples"] == []


def test_overlap_empty_inputs():
    ov = rev.value_overlap([], [])
    assert ov["overlap_count"] == 0
    assert ov["a_tokens"] == 0 and ov["b_tokens"] == 0


# =========================================================================
# UNIT — coverage_gap_recommendations
# =========================================================================

def test_recommendations_only_for_zero_overlap_concepts():
    recs = rev.coverage_gap_recommendations({"concept-x": 0, "concept-y": 3, "concept-z": 0})
    concepts = {r["concept"] for r in recs}
    assert concepts == {"concept-x", "concept-z"}
    assert all(r["severity"] == "info" for r in recs)
    assert all("optional" in r["message"].lower() or "best results" in r["message"].lower() for r in recs)


def test_recommendations_empty_when_all_bridged():
    assert rev.coverage_gap_recommendations({"a": 2, "b": 5}) == []


# =========================================================================
# UNIT — sample_field_values dispatch (mocked backends)
# =========================================================================

def test_sample_dispatch_postgres():
    with patch("agents.db.pg", return_value={"rows": [["v1"], ["v2"]]}), \
            patch("agents.db.validate_identifier", side_effect=lambda x: x):
        vals = rev.sample_field_values(system_id="sys-sql", connection_type="PostgreSQL",
                                       schema="sch", table="tbl", field="col")
    assert vals == ["v1", "v2"]


def test_sample_dispatch_athena():
    with patch("agents.athena_tools.run_athena_query", return_value={"columns": ["c"], "rows": [["ALPHA-1"]]}):
        vals = rev.sample_field_values(system_id="sys-wh", connection_type="Athena",
                                       table="tbl", field="col",
                                       connection_config={"workgroup": "wg", "database": "db"})
    assert vals == ["ALPHA-1"]


def test_sample_athena_strips_leading_table_qualifier():
    """Mappings carry a table-qualified ``field`` (``"{table}.{column}"``). The
    Athena column is the unqualified remainder — the leading ``{table}.`` must be
    stripped or Athena raises COLUMN_NOT_FOUND and sampling silently returns []."""
    captured = {}

    def _capture(sql, cfg, max_rows=None):
        captured["sql"] = sql
        return {"columns": ["c"], "rows": [["ALPHA-1"]]}

    with patch("agents.athena_tools.run_athena_query", side_effect=_capture):
        vals = rev.sample_field_values(system_id="sys-wh", connection_type="Athena",
                                       table="records", field="records.key_ref",
                                       connection_config={"workgroup": "wg", "database": "db"})
    assert vals == ["ALPHA-1"]
    # The SELECT targets the unqualified column, never the doubled qualifier.
    assert '"key_ref"' in captured["sql"]
    assert "records.key_ref" not in captured["sql"]


def test_sample_athena_preserves_dotted_nested_column_after_strip():
    """A flattened nested-struct column (``a.b.c``) qualified with its table
    (``records.a.b.c``) must strip ONLY the leading table segment, keeping the
    dotted nested path intact as the real Athena column name."""
    captured = {}

    def _capture(sql, cfg, max_rows=None):
        captured["sql"] = sql
        return {"columns": ["c"], "rows": [["SITE-A"]]}

    with patch("agents.athena_tools.run_athena_query", side_effect=_capture):
        vals = rev.sample_field_values(
            system_id="sys-wh", connection_type="Athena", table="records",
            field="records.entity.request.cell",
            connection_config={"workgroup": "wg", "database": "db"})
    assert vals == ["SITE-A"]
    # Leading table segment removed; the nested dotted path is preserved verbatim.
    assert '"entity.request.cell"' in captured["sql"]
    assert '"records.entity.request.cell"' not in captured["sql"]


def test_sample_athena_bare_column_unchanged():
    """A field with no table qualifier is used verbatim (no over-stripping)."""
    captured = {}

    def _capture(sql, cfg, max_rows=None):
        captured["sql"] = sql
        return {"columns": ["c"], "rows": [["X"]]}

    with patch("agents.athena_tools.run_athena_query", side_effect=_capture):
        rev.sample_field_values(system_id="sys-wh", connection_type="Athena",
                                table="records", field="cell",
                                connection_config={"workgroup": "wg", "database": "db"})
    assert '"cell"' in captured["sql"]


# =========================================================================
# UNIT — Athena sampling assumes the cross-account role (query-time parity)
# =========================================================================

def test_sample_athena_merges_cross_account_credentials_when_missing():
    """The resolve/map/provision sampling paths pass the NON-SECRET snapshot
    (workgroup/database only) — it lacks the STS ``external_id`` (a secret) and may
    lack ``role_arn``. Sampling must resolve the FULL config the same way
    query-time does (node + secret via ``resolve_connection``) and thread the
    cross-account credentials to the Athena client, or it is denied on the
    cross-account workgroup while query-time succeeds."""
    captured = {}

    def _capture(sql, cfg, max_rows=None):
        captured["cfg"] = cfg
        return {"columns": ["c"], "rows": [["SITE-A"]]}

    resolved = {
        "system_id": "sys-wh", "connection_type": "Athena",
        "workgroup": "wg", "database": "db",
        "role_arn": "arn:aws:iam::444455556666:role/CrossAcctReadOnly",
        "external_id": "ext-secret-123", "region": "us-west-2",
    }
    with patch("agents.connection_resolver.resolve_connection", return_value=resolved) as rc, \
            patch("agents.athena_tools.run_athena_query", side_effect=_capture):
        vals = rev.sample_field_values(
            system_id="sys-wh", connection_type="Athena", table="records",
            field="records.cell",
            connection_config={"workgroup": "wg", "database": "db"})
    assert vals == ["SITE-A"]
    rc.assert_called_once_with("sys-wh")
    # The cross-account credentials reached the Athena client config so boto3
    # STS-AssumeRoles into the source account (never ambient deploy-account creds).
    assert captured["cfg"]["role_arn"] == "arn:aws:iam::444455556666:role/CrossAcctReadOnly"
    assert captured["cfg"]["external_id"] == "ext-secret-123"


def test_sample_athena_does_not_reresolve_when_config_already_complete():
    """When the passed config ALREADY carries role_arn + external_id (e.g. Mode-B
    body input), no extra node/secret resolution is done — the supplied credentials
    are used verbatim."""
    captured = {}

    def _capture(sql, cfg, max_rows=None):
        captured["cfg"] = cfg
        return {"columns": ["c"], "rows": [["X"]]}

    with patch("agents.connection_resolver.resolve_connection") as rc, \
            patch("agents.athena_tools.run_athena_query", side_effect=_capture):
        rev.sample_field_values(
            system_id="sys-wh", connection_type="Athena", table="records", field="cell",
            connection_config={"workgroup": "wg", "database": "db",
                               "role_arn": "arn:aws:iam::111:role/R", "external_id": "e1"})
    rc.assert_not_called()
    assert captured["cfg"]["role_arn"] == "arn:aws:iam::111:role/R"
    assert captured["cfg"]["external_id"] == "e1"


def test_sample_athena_blank_system_id_skips_resolution():
    """Pre-registration Mode-B discovery passes ``system_id=""`` — there is no node
    to resolve, so the passed connection_config is used unchanged (best-effort)."""
    captured = {}

    def _capture(sql, cfg, max_rows=None):
        captured["cfg"] = cfg
        return {"columns": ["c"], "rows": [["X"]]}

    with patch("agents.connection_resolver.resolve_connection") as rc, \
            patch("agents.athena_tools.run_athena_query", side_effect=_capture):
        rev.sample_field_values(
            system_id="", connection_type="Athena", table="records", field="cell",
            connection_config={"workgroup": "wg", "database": "db"})
    rc.assert_not_called()
    assert "role_arn" not in captured["cfg"] or not captured["cfg"]["role_arn"]


def test_with_cross_account_credentials_fills_only_missing_keys():
    """Helper unit: a caller-supplied non-credential field (workgroup) is preserved;
    only the MISSING cross-account keys are filled from the resolved node/secret."""
    resolved = {"role_arn": "arn:aws:iam::222:role/R2", "external_id": "e2",
                "region": "eu-central-1", "workgroup": "resolved-wg"}
    with patch("agents.connection_resolver.resolve_connection", return_value=resolved):
        cfg = rev._with_cross_account_credentials(
            "sys-x", {"workgroup": "caller-wg", "database": "db"})
    assert cfg["workgroup"] == "caller-wg"        # caller field preserved
    assert cfg["database"] == "db"
    assert cfg["role_arn"] == "arn:aws:iam::222:role/R2"  # filled from resolved
    assert cfg["external_id"] == "e2"
    assert cfg["region"] == "eu-central-1"


def test_with_cross_account_credentials_resolve_failure_is_best_effort():
    """A resolve failure leaves the passed config unchanged (fail-soft) — sampling
    then degrades to [] downstream rather than crashing the resolve step."""
    with patch("agents.connection_resolver.resolve_connection",
               side_effect=RuntimeError("neptune down")):
        cfg = rev._with_cross_account_credentials("sys-x", {"workgroup": "wg"})
    assert cfg == {"workgroup": "wg"}


def test_with_cross_account_credentials_falls_back_to_conn_secret(monkeypatch):
    """M4: pre-provision there is NO System node yet (node-only resolve →
    unconfigured), but the registration-time connection secret
    ``adt/conn/<id>`` (minted by connect_new_system when the
    submitted config carried external_id) supplies the missing cross-account
    keys — it is tried BEFORE the agent-config fallback and, once complete, the
    agent-config secret is not consulted."""
    import agents.system_agent as system_agent

    fetched: list[str] = []

    def _fetch(name):
        fetched.append(name)
        if name == "adt/conn/sys-x":
            return {"external_id": "e-reg", "region": "us-west-2",
                    "role_arn": "arn:aws:iam::111122223333:role/ReadRole"}
        raise RuntimeError("no such secret")

    monkeypatch.setattr(system_agent, "_fetch_credentials", _fetch)
    with patch("agents.connection_resolver.resolve_connection",
               return_value={"system_id": "sys-x", "connection_type": None,
                             "connection_string": "", "secret_arn": "",
                             "_unconfigured": True}):
        cfg = rev._with_cross_account_credentials("sys-x", {"workgroup": "wg"})

    assert cfg["workgroup"] == "wg"
    assert cfg["external_id"] == "e-reg"
    assert cfg["role_arn"] == "arn:aws:iam::111122223333:role/ReadRole"
    assert cfg["region"] == "us-west-2"
    assert fetched == ["adt/conn/sys-x"]


def test_sample_athena_samples_distinct_values_not_first_rows():
    """A plain ``LIMIT n`` reads only the first split(s), so on a large
    clustered/partitioned column it returns a SINGLE cluster's value
    (non-representative — observed live as one facility code), silently starving
    value-overlap even when the columns genuinely share values elsewhere. The
    sampler must request DISTINCT join-key values (``GROUP BY`` the column) so the
    sample spreads across the value space; on columnar storage this scans only the
    single column's bytes, so it stays cheap while being representative."""
    captured = {}

    def _capture(sql, cfg, max_rows=None):
        captured["sql"] = sql
        return {"columns": ["c"], "rows": [["SITE-A"], ["SITE-B"], ["SITE-C"]]}

    with patch("agents.athena_tools.run_athena_query", side_effect=_capture):
        vals = rev.sample_field_values(system_id="sys-wh", connection_type="Athena",
                                       table="records", field="records.cell",
                                       connection_config={"workgroup": "wg", "database": "db"})
    assert vals == ["SITE-A", "SITE-B", "SITE-C"]
    sql = captured["sql"]
    # Distinct join-key sampling: GROUP BY the sampled column (not a bare LIMIT).
    assert "GROUP BY" in sql.upper()
    assert '"cell"' in sql
    # The GROUP BY targets the SAME unqualified column that is selected.
    assert 'GROUP BY "cell"' in sql


def test_sample_limit_is_representative_for_overlap_detection():
    """Guard: value-overlap is a JOIN-KEY detector — the shared keys are often a
    small fraction of a field's distinct values, so the per-field sample cap must
    be large enough to actually contain them. A tiny cap (the old 10) silently
    misses real overlaps. Keep it representative but below the SQL row cap."""
    from agents.sql_safety import MAX_ROWS
    assert rev._SAMPLE_LIMIT >= 100
    assert rev._SAMPLE_LIMIT <= MAX_ROWS


def test_sample_dispatch_sitewise_uses_graph_assets():
    with patch("agents.db.neptune_query", return_value=[{"name": "NODE-1"}, {"name": "NODE-2"}]):
        vals = rev.sample_field_values(system_id="sys-hist", connection_type="SiteWise (Direct)")
    assert vals == ["NODE-1", "NODE-2"]


def test_sample_dispatch_unknown_type_returns_empty():
    assert rev.sample_field_values(system_id="x", connection_type="Weird") == []


def test_sample_is_best_effort_on_error():
    with patch("agents.db.pg", side_effect=RuntimeError("boom")), \
            patch("agents.db.validate_identifier", side_effect=lambda x: x):
        assert rev.sample_field_values(system_id="sys-sql", connection_type="PostgreSQL",
                                       schema="sch", table="tbl", field="col") == []


# =========================================================================
# COMPONENT-INTEGRATION — real _do_resolve wiring, mocked I/O boundaries
# =========================================================================
#
# Synthetic topology (mirrors a real cross-system layout):
#   NEW system  "sys-new"  (Athena)     : records.key_ref -> concept-x
#                                          records.alt_ref -> concept-y
#   TARGET      "sys-hist" (SiteWise)    : nodes.label     -> concept-x
#   TARGET      "sys-rel"  (PostgreSQL)  : items.code      -> concept-y
# concept-x overlaps (shared value ALPHA-1); concept-y is disjoint -> recommendation.

_NEW = "sys-new"


def _sample_side_effect(**kw):
    sid, field = kw.get("system_id"), kw.get("field")
    if sid == _NEW and field == "key_ref":
        return ["ALPHA-1", "ALPHA-2"]
    if sid == _NEW and field == "alt_ref":
        return ["BETA-1"]
    if sid == "sys-hist":
        return ["ALPHA-1", "NODE-OTHER"]      # asset names from the graph
    if sid == "sys-rel" and field == "code":
        return ["GAMMA-9"]
    return []


def _catalog_get(sid):
    return {
        _NEW: {"schema": "", "connection_type": "Athena",
               "connection_config": {"workgroup": "wg", "database": "db", "region": "us-west-2"}},
        "sys-hist": {"connection_type": "SiteWise (Direct)", "schema": ""},
        "sys-rel": {"connection_type": "PostgreSQL", "schema": "sch"},
    }.get(sid, {})


def test_do_resolve_produces_evidence_and_recommendations():
    from agents.onboard import _do_resolve
    from agents.models import OnboardResolveRequest

    existing_fields = [
        {"sys_id": "sys-hist", "sys_name": "Hist", "sys_schema": "", "field_name": "label",
         "concept": "concept-x", "table_name": "nodes", "role": "identifier"},
        {"sys_id": "sys-rel", "sys_name": "Rel", "sys_schema": "sch", "field_name": "code",
         "concept": "concept-y", "table_name": "items", "role": "identifier"},
    ]
    body = OnboardResolveRequest(system_id=_NEW, mappings=[
        {"table": "records", "field": "key_ref", "concept": "concept-x", "confidence": 0.9},
        {"table": "records", "field": "alt_ref", "concept": "concept-y", "confidence": 0.8},
    ])
    bedrock_json = ('{"rules": [{"concept":"concept-x","source_field":"records.key_ref",'
                    '"target_system":"sys-hist","target_field":"nodes.label","confidence":0.9}], '
                    '"value_mappings": []}')

    with patch("agents.onboard.neptune_query", return_value=existing_fields), \
            patch("agents.onboard.catalog") as mock_cat, \
            patch("agents.onboard.bedrock_call", return_value=bedrock_json), \
            patch("agents.connection_resolver.resolve_connection", return_value={}), \
            patch("agents.resolve_evidence.sample_field_values", side_effect=_sample_side_effect):
        mock_cat.get.side_effect = _catalog_get
        result = _do_resolve(body)

    # Evidence: concept-x has a measured overlap on the shared synthetic value.
    ev = [e for e in result["evidence"] if e["concept"] == "concept-x"]
    assert ev, result
    assert ev[0]["overlap_count"] >= 1
    assert "ALPHA-1" in ev[0]["examples"]
    assert ev[0]["target_system"] == "sys-hist"

    # Recommendation: concept-y is shared but disjoint -> optional coverage-gap rec.
    rec_concepts = {r["concept"] for r in result["recommendations"]}
    assert "concept-y" in rec_concepts
    assert "concept-x" not in rec_concepts  # bridged -> no rec

    assert any(r.get("concept") == "concept-x" for r in result["rules"])
    assert "value_mappings" in result


def test_do_resolve_no_shared_concepts_returns_clean_empty():
    from agents.onboard import _do_resolve
    from agents.models import OnboardResolveRequest
    existing_fields = [
        {"sys_id": "sys-rel", "sys_schema": "sch", "field_name": "code",
         "concept": "concept-y", "table_name": "items", "role": "identifier"},
    ]
    body = OnboardResolveRequest(system_id=_NEW, mappings=[
        {"table": "records", "field": "key_ref", "concept": "concept-x", "confidence": 0.9},
    ])
    with patch("agents.onboard.neptune_query", return_value=existing_fields), \
            patch("agents.onboard.catalog") as mock_cat, \
            patch("agents.onboard.bedrock_call") as mock_bedrock:
        mock_cat.get.side_effect = _catalog_get
        result = _do_resolve(body)
    mock_bedrock.assert_not_called()
    assert result == {"rules": [], "evidence": [], "recommendations": []}


def test_do_resolve_no_false_negative_recommendation_when_sampling_empty():
    """A shared concept whose target side could NOT be sampled (empty/failed) must
    NOT emit a coverage-gap recommendation — that would misreport a non-measurement
    as 'no overlapping values found'."""
    from agents.onboard import _do_resolve
    from agents.models import OnboardResolveRequest

    existing_fields = [
        {"sys_id": "sys-hist", "sys_name": "Hist", "sys_schema": "", "field_name": "label",
         "concept": "concept-x", "table_name": "nodes", "role": "identifier"},
    ]
    body = OnboardResolveRequest(system_id=_NEW, mappings=[
        {"table": "records", "field": "key_ref", "concept": "concept-x", "confidence": 0.9},
    ])

    def _empty_target(**kw):
        if kw.get("system_id") == _NEW:
            return ["ALPHA-1"]
        return []  # target sampling yields nothing (e.g. graph not reachable)

    with patch("agents.onboard.neptune_query", return_value=existing_fields), \
            patch("agents.onboard.catalog") as mock_cat, \
            patch("agents.onboard.bedrock_call", return_value='{"rules": [], "value_mappings": []}'), \
            patch("agents.connection_resolver.resolve_connection", return_value={}), \
            patch("agents.resolve_evidence.sample_field_values", side_effect=_empty_target):
        mock_cat.get.side_effect = _catalog_get
        result = _do_resolve(body)

    assert result["recommendations"] == []   # NOT measured -> no false-negative rec
    assert result["evidence"] == []


# =========================================================================
# LIVE INTEGRATION (opt-in, env-driven — no hardcoded identifiers)
# =========================================================================

_IT_WG = os.environ.get("ADT_ATHENA_IT_WORKGROUP")
_IT_DB = os.environ.get("ADT_ATHENA_IT_DATABASE")
_IT_TBL = os.environ.get("ADT_ATHENA_IT_TABLE")
_IT_FLD = os.environ.get("ADT_ATHENA_IT_FIELD")
_IT_REGION = os.environ.get("ADT_ATHENA_IT_REGION", "us-east-1")


@pytest.mark.skipif(
    not all([_IT_WG, _IT_DB, _IT_TBL, _IT_FLD]),
    reason="live Athena integration: set ADT_ATHENA_IT_WORKGROUP/DATABASE/TABLE/FIELD "
           "(+ AWS creds/region) to run against a real workgroup",
)
def test_sample_field_values_athena_live():
    cfg = {"workgroup": _IT_WG, "database": _IT_DB, "region": _IT_REGION}
    vals = rev.sample_field_values(system_id="live-sys", connection_type="Athena",
                                   table=_IT_TBL, field=_IT_FLD, connection_config=cfg)
    assert vals, "expected real distinct values from the configured live workgroup"
    assert all(isinstance(v, str) and v for v in vals)
