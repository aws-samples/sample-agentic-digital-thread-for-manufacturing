# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One answer to "is this system being removed": the graph marker plus the
catalog state (``catalog.systems_being_removed`` for system ids,
``catalog.removal_aliases`` for model-written fields, which also match a
display name).

Two fixes for the same race met on main: one reads the catalog state a
removal sets in this process (``removing``, then ``removal_failed``; knows
display names), the other the ``removal_incomplete`` marker the removal
writes on the System node first (durable, seen by every process and inside
one Cypher statement). Every check reads both through the one helper, so
neither path loses the other's case. Fixture ids are generic.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.catalog as catalog_module  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from agents.catalog import REMOVING, SystemCatalog  # noqa: E402

GONE, GONE_NAME = "site-api", "Site API"
MARKED = "line-mes"   # marked in the graph; another process's catalog does not know
LIVE = "corp-erp"
NEW = "plant-cmms"


def _catalog(status: str = REMOVING) -> SystemCatalog:
    return SystemCatalog([
        {"id": GONE, "name": GONE_NAME, "status": status, "connection_type": "REST API"},
        {"id": LIVE, "name": "Corp ERP", "status": "onboarded", "connection_type": "PostgreSQL"},
    ])


def test_the_answer_is_the_marker_plus_the_catalog_ids_and_by_name_for_model_fields():
    assert catalog_module.systems_being_removed({MARKED}, _catalog()) == {MARKED, GONE}
    assert catalog_module.removal_aliases({MARKED}, _catalog()) == {MARKED, GONE, GONE_NAME}
    assert catalog_module.systems_being_removed({MARKED}, _catalog("onboarded")) == {MARKED}
    assert catalog_module.removal_aliases({MARKED}, _catalog("onboarded")) == {MARKED}


def test_each_reader_combines_the_two_signals():
    cat = _catalog()
    rows = [{"id": MARKED, "removal_incomplete": True}, {"id": LIVE, "removal_incomplete": False}]
    assert catalog_module.removed_ids(rows, "id", cat) == {MARKED, GONE}
    assert catalog_module.removing_system_ids([{"sid": MARKED}], cat) == {MARKED, GONE}
    assert catalog_module.removing_system_aliases([{"sid": MARKED}], cat) == {MARKED, GONE, GONE_NAME}


def test_a_failed_marker_read_leaves_the_catalogs_answer():
    assert catalog_module.removing_system_ids([], _catalog()) == {GONE}
    assert catalog_module.removing_system_aliases([], _catalog()) == {GONE, GONE_NAME}


def test_a_catalog_without_the_removal_read_adds_nothing():
    assert catalog_module.systems_being_removed({MARKED}, object()) == {MARKED}


def test_the_default_catalog_is_read_when_the_check_runs(monkeypatch):
    monkeypatch.setattr(catalog_module, "catalog", _catalog())
    assert catalog_module.systems_being_removed() == {GONE}


def test_provision_inventory_leaves_out_a_system_only_the_catalog_knows_is_removing(monkeypatch):
    """The marker column reads false (the read raced the marker write); the
    catalog already says removing."""
    rows = [{"id": GONE, "removal_incomplete": False}, {"id": LIVE, "removal_incomplete": False},
            {"id": MARKED, "removal_incomplete": True}]
    monkeypatch.setattr(onboard, "catalog", _catalog())
    monkeypatch.setattr(onboard, "neptune_query_checked", lambda cypher, params=None: rows)
    assert onboard._graph_system_ids_checked() == {LIVE}


def test_the_chats_resolve_drops_a_rule_naming_a_removing_system_by_display_name(monkeypatch):
    """The marker read finds nothing (another process's removal, not yet
    marked when read, or a failed read); the catalog knows the name."""
    monkeypatch.setattr(oa, "catalog", _catalog())
    monkeypatch.setattr(oa, "neptune_query", lambda cypher, params=None: [])
    rules = [{"concept": "equipment-id", "target_system": GONE_NAME},
             {"concept": "equipment-id", "target_system": LIVE}]
    vms = [{"from_system": NEW, "to_system": GONE}, {"from_system": NEW, "to_system": LIVE}]
    kept_rules, kept_vms = oa._without_peers_being_removed(NEW, rules, vms)
    assert [r["target_system"] for r in kept_rules] == [LIVE]
    assert [v["to_system"] for v in kept_vms] == [LIVE]


def test_the_resolve_filter_drops_a_marked_system_the_catalog_does_not_know(monkeypatch):
    """The other direction: this process's catalog shows nothing, the graph
    marker does (the removal runs in another app task)."""
    monkeypatch.setattr(onboard, "catalog", _catalog("onboarded"))
    monkeypatch.setattr(onboard, "neptune_query", lambda cypher, params=None: [{"sid": MARKED}])
    result = {"rules": [{"target_system": MARKED}, {"target_system": LIVE},
                        {"source_system": MARKED, "target_system": LIVE}],
              "rejected_rules": [{"target_system": MARKED}],
              "value_mappings": [], "evidence": []}
    kept = onboard._drop_removed_candidates(result, NEW)
    assert kept["rules"] == [{"target_system": LIVE}]
    assert kept["rejected_rules"] == []


def test_a_malformed_system_name_does_not_break_the_filter():
    kept = catalog_module.without_systems([{"target_system": ["x"]}, {"target_system": GONE}],
                                          {GONE}, ("target_system",))
    assert kept == [{"target_system": ["x"]}]
