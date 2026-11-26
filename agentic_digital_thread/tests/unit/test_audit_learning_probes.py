# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the PURE probe functions in scripts/audit_learning.py.

No network — every probe is a pure function over parsed JSON fixtures.
scripts/ lives at the repo root (outside the agentic_digital_thread
PYTHONPATH), so we add the repo root to sys.path and import
`scripts.audit_learning` as a namespace package (same mechanism as
test_smoke_prompts_classifiers.py).
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.audit_learning import (  # noqa: E402
    live_mappings,
    live_rules,
    probe_auto_accept,
    probe_cross_site,
    probe_duplicates,
    probe_forbidden_pairings,
    probe_id_shapes,
    probe_trends,
    probe_unresolved_drift,
    probe_usage_ledger,
    run_probes,
)

_HEX16 = "0123456789abcdef"


def _jp(jid, from_sys, from_field, to_sys, to_field):
    return {"id": jid, "from_system": from_sys, "from_field": from_field,
            "to_system": to_sys, "to_field": to_field}


def _rule(rid, src_sys="sys-a", src_field="orders.item_id", tgt_sys="sys-b",
          tgt_field="parts.item_id", concept="item-id", status="active"):
    return {"id": rid, "source_system": src_sys, "source_field": src_field,
            "target_system": tgt_sys, "target_field": tgt_field,
            "concept": concept, "status": status}


def _summary(rules=None, join_paths=None, authorities=None):
    return {"rules": rules or [], "join_paths_detail": join_paths or [],
            "authority_rules_detail": authorities or []}


# ── Probe 1 — duplicate census ───────────────────────────────────────────────

def test_seventeen_fake_dups_regression_table_qualified_keys_do_not_flag():
    """THE 17-fake-dup-groups false alarm, pinned (2026-07-24).

    17 join paths share systems and the same COLUMN LEAF on both sides but
    join DIFFERENT TABLES — a concept-coarse (leaf-keyed) census groups them
    into fake duplicates; the table-qualified key must report ZERO groups.
    """
    tables = [f"table_{i:02d}" for i in range(17)]
    jps = [_jp(f"jp-x-{_HEX16}"[:5] + f"{i:02d}-{_HEX16}",
               "sys-a", f"{t}.operator_id", "sys-b", f"{t}_remote.operator_id")
           for i, t in enumerate(tables)]
    # Prove the fixture is adversarial: a COARSE key (systems + column leaf)
    # would have produced one big fake group of all 17.
    coarse = {}
    for j in jps:
        key = (j["from_system"], j["from_field"].split(".")[-1],
               j["to_system"], j["to_field"].split(".")[-1])
        coarse.setdefault(key, []).append(j["id"])
    assert any(len(ids) == 17 for ids in coarse.values())
    result = probe_duplicates(_summary(join_paths=jps))
    assert result["joinpath_dup_groups"] == []
    assert result["count"] == 0


def test_real_joinpath_duplicate_is_flagged():
    jps = [_jp("jp-a", "sys-a", "orders.op_id", "sys-b", "shifts.op_id"),
           _jp("jp-b", "sys-a", "orders.op_id", "sys-b", "shifts.op_id"),
           _jp("jp-c", "sys-a", "orders.op_id", "sys-b", "crews.op_id")]
    result = probe_duplicates(_summary(join_paths=jps))
    assert result["count"] == 1
    assert sorted(result["joinpath_dup_groups"][0]["ids"]) == ["jp-a", "jp-b"]


def test_identity_duplicate_census_includes_concept_and_skips_deactivated():
    rules = [_rule("ir-a"), _rule("ir-b"),
             _rule("ir-dead", status="deactivated"),
             _rule("ir-c", concept="other-concept")]
    result = probe_duplicates(_summary(rules=rules))
    assert result["count"] == 1
    assert sorted(result["identity_dup_groups"][0]["ids"]) == ["ir-a", "ir-b"]


def test_joinpath_key_is_case_insensitive_but_table_scoped():
    jps = [_jp("jp-a", "SYS-A", "Orders.Op_Id", "sys-b", "shifts.op_id"),
           _jp("jp-b", "sys-a", "orders.op_id", "sys-b", "shifts.op_id")]
    assert probe_duplicates(_summary(join_paths=jps))["count"] == 1


# ── Probe 2 — id shapes ──────────────────────────────────────────────────────

def test_legacy_index_keyed_id_flagged_digest_and_steward_pass():
    rules = [_rule(f"ir-alpha-{_HEX16}"), _rule("subj-steward-row"),
             _rule("ir-alpha-14")]
    jps = [_jp(f"jp-alpha-{_HEX16}", "a", "t.c", "b", "t.c"),
           _jp("jp-alpha-7", "a", "t.x", "b", "t.x")]
    result = probe_id_shapes(_summary(rules=rules, join_paths=jps))
    assert sorted(result["legacy_ids"]) == ["ir-alpha-14", "jp-alpha-7"]
    assert result["count"] == 2


def test_deactivated_legacy_id_not_flagged():
    rules = [_rule("ir-alpha-14", status="deactivated")]
    assert probe_id_shapes(_summary(rules=rules))["count"] == 0


# ── Probe 3 — forbidden pairings ─────────────────────────────────────────────

def test_identifier_to_measure_pairing_flagged():
    rules = [_rule("ir-m", src_field="orders.operator_id",
                   tgt_field="metrics.CycleCount")]
    result = probe_forbidden_pairings(_summary(rules=rules))
    assert result["count"] == 1
    assert "measure" in result["violations"][0]["reason"]
    assert "is_identifier_like" in result["semantics"]


def test_stream_suffix_values_flagged_camel_and_snake():
    for field in ("telemetry.Line1_DeviceState", "telemetry.unit_alarm",
                  "telemetry.line_oee"):
        rules = [_rule("ir-s", src_field="orders.operator_id", tgt_field=field)]
        result = probe_forbidden_pairings(_summary(rules=rules))
        assert result["count"] == 1, field
        assert "stream/state value" in result["violations"][0]["reason"]


def test_site_equipment_grain_pairing_flagged():
    rules = [_rule("ir-g", src_field="plants.site_id",
                   tgt_field="assets.machine_id")]
    result = probe_forbidden_pairings(_summary(rules=rules))
    assert result["count"] == 1
    assert "containment" in result["violations"][0]["reason"]


def test_clean_identifier_rule_passes_and_deactivated_skipped():
    rules = [_rule("ir-ok", src_field="orders.operator_id",
                   tgt_field="crews.operator_code"),
             _rule("ir-dead", src_field="orders.id",
                   tgt_field="metrics.scrap_count", status="deactivated")]
    assert probe_forbidden_pairings(_summary(rules=rules))["count"] == 0


# ── Probe 4 — auto-accept audit ──────────────────────────────────────────────

_UUID = "12345678-1234-1234-1234-123456789abc"


def _mapping(mid, status="pending", **kw):
    return {"mapping_id": mid, "status": status, **kw}


def test_accepted_hierarchy_position_flagged_pending_not():
    mappings = [_mapping("m1", status="accepted", method="hierarchy_position"),
                _mapping("m2", status="pending", method="hierarchy_position")]
    result = probe_auto_accept(mappings)
    assert result["hierarchy_position_accepted"] == ["m1"]
    assert result["count"] == 1


def test_accepted_uuid_target_requires_verified_true():
    mappings = [_mapping("m1", status="accepted", to_value=_UUID),
                _mapping("m2", status="accepted", to_value=_UUID,
                         verified="true"),
                _mapping("m3", status="accepted", to_value="PLANT-7")]
    result = probe_auto_accept(mappings)
    assert result["unverified_uuid_targets"] == ["m1"]
    assert result["count"] == 1


# ── Probe 5 — cross-site vouching ────────────────────────────────────────────

def _cross_site(mid, reason=None, concept="operator-id"):
    m = _mapping(mid, proposed_type="join_path", from_system="mes-alpha",
                 to_system="mes-beta", concept=concept)
    if reason is not None:
        m["review_reason"] = reason
    return m


def test_cross_site_pending_with_review_reason_extracts_authority_id():
    reason = ("cross-site link vouched only by unverified authority "
              "ar-maint-operator")
    result = probe_cross_site([_cross_site("m1", reason)], _summary())
    row = result["pending"][0]
    assert row["review_reason_present"] is True
    assert row["authority_id"] == "ar-maint-operator"
    assert row["legacy"] is False
    assert result["unexplained"] == []


def test_cross_site_pending_without_reason_matching_authority_is_legacy():
    authorities = [{"id": "ar-maint-operator", "concept": "Operator-ID",
                    "system_id": "cmms-main", "rule": "authoritative"}]
    result = probe_cross_site([_cross_site("m1")],
                              _summary(authorities=authorities))
    row = result["pending"][0]
    assert row["review_reason_present"] is False
    assert row["authority_id"] == "ar-maint-operator"
    assert row["legacy"] is True
    assert result["unexplained"] == []


def test_cross_site_pending_with_neither_reason_nor_authority_is_unexplained():
    result = probe_cross_site([_cross_site("m1")], _summary())
    assert result["unexplained"] == ["m1"]


def test_cross_site_ignores_different_class_same_system_and_accepted():
    different_class = _mapping("m1", proposed_type="join_path",
                               from_system="mes-alpha", to_system="erp-alpha")
    same_system = _mapping("m2", proposed_type="join_path",
                           from_system="mes-alpha", to_system="mes-alpha")
    accepted = dict(_cross_site("m3"), status="accepted")
    narrative = _mapping("m4", proposed_type="insight",
                         from_system="mes-alpha", to_system="mes-beta")
    result = probe_cross_site(
        [different_class, same_system, accepted, narrative], _summary())
    assert result["count"] == 0


# ── Probe 6 — trends (never gates) ───────────────────────────────────────────

def test_pending_ages_and_quarantine_counts():
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    mappings = [
        _mapping("m1", discovered_at=(now - timedelta(days=2)).isoformat()),
        _mapping("m2", discovered_at=(now - timedelta(hours=12)).isoformat(),
                 quarantined="true"),
        _mapping("m3", status="accepted",
                 discovered_at=(now - timedelta(days=9)).isoformat()),
    ]
    health = {"quarantine": {"pending_quarantined": 1},
              "staging": {"rows": 3, "distinct_subjects": 2}}
    result = probe_trends(mappings, health, now)
    assert result["pending_total"] == 2
    assert result["pending_ages_days"] == [0.5, 2.0]
    assert result["pending_quarantined"] == 1
    assert result["health_staging"] == {"rows": 3, "distinct_subjects": 2}
    assert result["gates"] is False


def test_trends_tolerates_bad_timestamps_and_empty_health():
    result = probe_trends([_mapping("m1", discovered_at="not-a-date")], {}, None)
    assert result["pending_ages_days"] == []
    assert result["age_distribution"]["count"] == 0


# ── Probe 7 — usage-ledger liveness ──────────────────────────────────────────

def test_ledger_rows_present_is_ok():
    health = {"integrity_mode": "report-only",
              "usage_ledger": {"rows_30d": 4, "artifacts": 2,
                               "evidence_classes": ["route-intersection"]}}
    result = probe_usage_ledger(health)
    assert result["verdict"] == "ok"
    assert result["evidence_classes"] == ["route-intersection"]


def test_ledger_zero_rows_report_only_is_expected_gap_not_failure():
    health = {"integrity_mode": "report-only",
              "usage_ledger": {"rows_30d": 0, "artifacts": 0,
                               "evidence_classes": []}}
    assert probe_usage_ledger(health)["verdict"] == "expected-gap"


def test_ledger_zero_rows_enforcing_env_is_starved():
    health = {"integrity_mode": "enforce",
              "usage_ledger": {"rows_30d": 0, "artifacts": 0,
                               "evidence_classes": []}}
    assert probe_usage_ledger(health)["verdict"] == "starved"


def test_missing_health_endpoint_reports_unavailable():
    assert probe_usage_ledger({})["verdict"] == "unavailable"


# ── Probe 8 — unresolved-drift age ───────────────────────────────────────────

_NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def _drift(mid, days_old, status="pending"):
    return _mapping(mid, status=status, proposed_type="schema_drift",
                    discovered_at=(_NOW - timedelta(days=days_old)).isoformat())


def test_drift_probe_ages_and_strict_gate():
    """Ages come from discovered_at; only rows past max_age_days land in
    over_age (keyed by mapping_id ONLY — no shape/site inference)."""
    rows = [_drift("m-old", 9.0), _drift("m-fresh", 1.0),
            _drift("m-acked", 30.0, status="accepted")]
    result = probe_unresolved_drift(rows, now=_NOW, max_age_days=7.0)
    assert result["verdict"] == "over-age"
    assert result["pending"] == 2
    assert result["over_age"] == ["m-old"]
    assert result["oldest_age_days"] == 9.0
    assert result["max_age_days"] == 7.0
    # All-fresh pendings: honest ok, nothing over age.
    ok = probe_unresolved_drift([_drift("m-fresh", 1.0)], now=_NOW)
    assert ok["verdict"] == "ok"
    assert ok["over_age"] == []


def test_drift_probe_unavailable_pre_deploy_never_gates():
    """ZERO schema_drift rows of any status ⇒ 'unavailable' (probe-7 idiom):
    a pre-drift-feature env is indistinguishable from a zero-drift env, and
    neither may gate — even under --strict (pinned in the report tests)."""
    result = probe_unresolved_drift(
        [_mapping("m1", proposed_type="join_path")], now=_NOW)
    assert result["verdict"] == "unavailable"
    assert result["pending"] == 0
    assert result["over_age"] == []
    assert result["oldest_age_days"] is None


def test_drift_probe_ignores_non_drift_types():
    """An ancient pending join_path never counts toward drift age."""
    rows = [_mapping("m-jp", proposed_type="join_path",
                     discovered_at=(_NOW - timedelta(days=99)).isoformat()),
            _drift("m-drift", 1.0)]
    result = probe_unresolved_drift(rows, now=_NOW, max_age_days=7.0)
    assert result["verdict"] == "ok"
    assert result["pending"] == 1
    assert result["over_age"] == []


def test_drift_probe_tolerates_bad_timestamps():
    """An unparseable discovered_at is skipped, never a crash or a flag."""
    rows = [_mapping("m-bad", proposed_type="schema_drift",
                     discovered_at="not-a-date")]
    result = probe_unresolved_drift(rows, now=_NOW)
    assert result["verdict"] == "ok"
    assert result["pending"] == 1
    assert result["oldest_age_days"] is None


# ── Staged rows excluded EVERYWHERE ──────────────────────────────────────────

def test_staged_rows_excluded_from_every_probe():
    """A staged# row shaped adversarially like a violation in every probe
    must be structurally invisible (ADR-0051 P5 — evidence, not lifecycle)."""
    staged = _mapping("staged#evil", status="accepted",
                      method="hierarchy_position", to_value=_UUID,
                      proposed_type="join_path", from_system="mes-alpha",
                      to_system="mes-beta",
                      discovered_at="2026-01-01T00:00:00+00:00",
                      quarantined="true")
    proposals = {"mappings": [staged]}
    assert live_mappings(proposals) == []
    probes = run_probes(_summary(), proposals, {})
    assert probes["auto_accept"]["count"] == 0
    assert probes["cross_site"]["count"] == 0
    assert probes["trends"]["pending_total"] == 0


def test_live_rules_filters_deactivated_only():
    rules = [_rule("ir-live"), _rule("ir-dead", status="deactivated"),
             _rule("ir-nostatus", status="")]
    assert [r["id"] for r in live_rules(_summary(rules=rules))] == [
        "ir-live", "ir-nostatus"]


def test_run_probes_returns_all_eight():
    probes = run_probes(_summary(), {"mappings": []}, {})
    assert sorted(probes) == sorted([
        "duplicates", "id_shapes", "forbidden_pairings", "auto_accept",
        "cross_site", "trends", "usage_ledger", "unresolved_drift"])


class TestLiveFalseAlarms20260725:
    """Pins the two live false alarms from the first deployed run."""

    def test_structural_joinpaths_never_flag_as_legacy(self):
        summary = {"rules": [], "join_paths_detail": [
            {"id": "jp-internal-cmms-work_orders_raw-asset_id-assets"},
            {"id": "jp-vo-erp-item_suppliers-supplier_id-material_lots-supplier_id"},
            {"id": "jp-samerow-erp-customers-company_name-contact_name"},
        ]}
        assert probe_id_shapes(summary)["count"] == 0

    def test_truly_legacy_ids_still_flag(self):
        summary = {"rules": [{"id": "ir-cmms-14"}], "join_paths_detail": [
            {"id": "jp-plm-erp-3"}]}
        assert probe_id_shapes(summary)["count"] == 2

    def test_structural_rows_with_leaf_fields_are_not_duplicates(self):
        # distinct table joins, identical leaf fields — the 2026-07-25 alarm
        jps = [
            {"id": "jp-internal-cmms-work_orders_raw-asset_id-assets",
             "from_system": "cmms", "from_field": "asset_id",
             "to_system": "cmms", "to_field": "asset_id"},
            {"id": "jp-internal-cmms-maintenance_schedules_raw-asset_id-assets",
             "from_system": "cmms", "from_field": "asset_id",
             "to_system": "cmms", "to_field": "asset_id"},
        ]
        res = probe_duplicates({"rules": [], "join_paths_detail": jps})
        assert res["count"] == 0

    def test_true_structural_duplicate_ids_still_flag(self):
        jps = [{"id": "jp-internal-cmms-t1-k-t2", "from_system": "cmms",
                "from_field": "k", "to_system": "cmms", "to_field": "k"}] * 2
        res = probe_duplicates({"rules": [], "join_paths_detail": jps})
        assert res["count"] == 1

    def test_distinct_digest_ids_are_distinct_content_by_construction(self):
        # Pilot env 2026-07-25: leaf-collapsed fields tied, digests differed
        jps = [
            {"id": "jp-site9-a-c3364d45cfa9cc09", "from_system": "a",
             "from_field": "cell", "to_system": "b", "to_field": "line"},
            {"id": "jp-site9-a-663de51859a01719", "from_system": "a",
             "from_field": "cell", "to_system": "b", "to_field": "line"},
        ]
        assert probe_duplicates({"rules": [], "join_paths_detail": jps})["count"] == 0

    def test_mixed_digest_and_legacy_group_still_flags(self):
        jps = [
            {"id": "jp-site9-a-c3364d45cfa9cc09", "from_system": "a",
             "from_field": "cell", "to_system": "b", "to_field": "line"},
            {"id": "jp-site9-a-b-line-0", "from_system": "a",
             "from_field": "cell", "to_system": "b", "to_field": "line"},
        ]
        assert probe_duplicates({"rules": [], "join_paths_detail": jps})["count"] == 1


class TestCrossSiteConnectionTypeVeto:
    """connection_type (optional catalog) vetoes the leading-token proxy —
    2026-07-25 pilot-env false alarm: the two system ids shared a leading
    token ('site9' here) but are Athena vs SiteWise systems."""

    _ROW = {"mapping_id": "x", "status": "pending", "proposed_type": "join_path",
            "from_system": "site9-lake", "to_system": "site9-telem", "concept": "c"}

    def test_different_connection_types_excluded(self):
        catalog = [{"id": "site9-lake", "connection_type": "Athena"},
                   {"id": "site9-telem", "connection_type": "SiteWise (Direct)"}]
        res = probe_cross_site([dict(self._ROW)], {"authority_rules_detail": []}, catalog)
        assert res["count"] == 0

    def test_same_connection_types_still_flag(self):
        catalog = [{"id": "site9-lake", "connection_type": "PostgreSQL"},
                   {"id": "site9-telem", "connection_type": "PostgreSQL"}]
        res = probe_cross_site([dict(self._ROW)], {"authority_rules_detail": []}, catalog)
        assert res["count"] == 1

    def test_absent_catalog_keeps_proxy_behavior(self):
        res = probe_cross_site([dict(self._ROW)], {"authority_rules_detail": []}, None)
        assert res["count"] == 1


def test_steward_joins_on_different_tables_are_not_duplicates():
    """Live false alarm 2026-09-27: same leaf field, different source tables."""
    summary = {"join_paths_detail": [
        {"id": "subj-0b3a0b2e14752464", "from_system": "mes-mil", "from_table": "work_orders",
         "from_field": "work_order_id", "to_system": "erp", "to_table": "lot_mes_tracking",
         "to_field": "work_order_id"},
        {"id": "subj-2a4c773667421b6d", "from_system": "mes-mil", "from_table": "quality_checks",
         "from_field": "work_order_id", "to_system": "erp", "to_table": "lot_mes_tracking",
         "to_field": "work_order_id"},
    ]}
    assert probe_duplicates(summary)["count"] == 0


def test_same_table_qualified_steward_joins_still_flag():
    row = {"from_system": "mes-mil", "from_table": "work_orders", "from_field": "work_order_id",
           "to_system": "erp", "to_table": "lot_mes_tracking", "to_field": "work_order_id"}
    summary = {"join_paths_detail": [{**row, "id": "subj-aaaaaaaaaaaaaaaa"},
                                     {**row, "id": "subj-bbbbbbbbbbbbbbbb"}]}
    assert probe_duplicates(summary)["count"] == 1
