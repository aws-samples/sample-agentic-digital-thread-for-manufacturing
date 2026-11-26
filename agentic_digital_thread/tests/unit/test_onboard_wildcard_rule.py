# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A ``{*}`` IdentityRule (ADR-0086) through the onboarding provision path.

``_augment_with_affinity_rules`` is covered in
``test_resolve_identity_affinity.py``; this pins the steps after it that a
wildcard rule also crosses, offline: the semantic-role gate, the live sample
pattern check (``transform_id`` must return non-None on a real sample value)
and the JoinPath writer's ``transform`` string. All impure seams are stubbed.
"""
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402

_FORWARD = {"concept": "product", "source_field": "runs.product_code", "source_table": "runs",
            "source_pattern": "{id}", "target_system": "sys-b",
            "target_field": "items.item_id", "target_table": "items",
            "target_pattern": "PRT-{id}-R-{*}", "confidence": 0.85,
            "method": "value_core_match"}
_REVERSE = {**_FORWARD, "source_field": "items.item_id", "source_table": "items",
            "source_pattern": "PRT-{id}-R-{*}", "target_field": "runs.product_code",
            "target_table": "runs", "target_pattern": "{id}", "target_system": "sys-a"}


def _ctx(connection_type="PostgreSQL"):
    return onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id="sys-a", system_name="Sys A",
        plant="", schema="sys_a", rules=[], mappings=[], connection_type=connection_type,
        cm_items=[], committable=set())


#: Column -> table of the fixture, and what each rule's TARGET column holds:
#: provision also reads the target column (the reverse reader check).
_TABLES = {"item_id": "items", "product_code": "runs"}
_TARGET_VALUES = {"items": "PRT-5101-R-A", "runs": "5101"}


def _sample(monkeypatch, value, source_table="runs"):
    """The source column holds ``value``; the target column its usual value."""
    monkeypatch.setattr(onboard, "catalog", SimpleNamespace(
        get=lambda sid: {"schema": sid.replace("-", "_"), "connection_type": "PostgreSQL"}))

    def pg(_schema, sql, params=None, **_k):
        if "information_schema" in sql:
            return {"rows": [[_TABLES[params[1]], "character varying"]], "columns": ["t", "d"]}
        held = value if f"FROM {source_table} " in sql else _TARGET_VALUES[sql.split("FROM ")[1].split()[0]]
        return {"rows": [[held]], "columns": ["v"]}
    monkeypatch.setattr(onboard, "pg", pg)


@pytest.mark.parametrize("rule", [_FORWARD, _REVERSE])
def test_the_semantic_role_gate_accepts_a_wildcard_rule(rule):
    assert onboard._is_valid_identity_rule(rule) == (True, "")


def test_the_sample_check_passes_a_forward_wildcard_rule(monkeypatch):
    _sample(monkeypatch, "5101")
    assert onboard._dp_rule_pattern_check(_ctx(), _FORWARD) == (None, "{id}", True)


def test_the_sample_check_passes_a_reverse_wildcard_rule(monkeypatch):
    _sample(monkeypatch, "PRT-5101-R-C", source_table="items")
    assert onboard._dp_rule_pattern_check(_ctx(), _REVERSE) == (None, "PRT-{id}-R-{*}", True)


def test_the_sample_check_rejects_a_value_the_wildcard_cannot_match(monkeypatch):
    _sample(monkeypatch, "PRT-5101-R-C-D", source_table="items")
    reason, pattern, passed = onboard._dp_rule_pattern_check(_ctx(), _REVERSE)
    assert "doesn't match sample value 'PRT-5101-R-C-D'" in reason
    assert (pattern, passed) == ("PRT-{id}-R-{*}", False)


def test_the_joinpath_writer_carries_the_wildcard_in_its_transform(monkeypatch):
    writes = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "catalog", SimpleNamespace(get=lambda _s: None))
    ctx = _ctx()
    onboard._dp_write_rule_joinpath(ctx, _FORWARD, "runs", "product_code", "sys-b",
                                    "items", "item_id")
    merge = next(p for c, p in writes if c.startswith("MERGE (jp:JoinPath"))
    assert merge["xform"] == "pattern:{id}->PRT-{id}-R-{*}"
    assert merge["method"] == "value_format_match"
    assert ctx.join_paths_written == 1
