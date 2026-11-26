# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0051 P7 — self-map suppression → field-level corroboration.

Audited defect (pilot env): the agent kept proposing cross-system value_mappings
whose two sides are the SAME value (a site code appearing identically in two
systems — the SITE9→SITE9 family) even though a DIRECT-transform JoinPath
already covers the field pair. Such a row is not information; it is
corroboration that the covering rule WORKS.

Behavior pinned here (offline — FakeTable + monkeypatched graph seam):
  * identity value pair + covering direct JoinPath ⇒ NOT stored as a VM row;
  * instead the P5 staging corroboration count for the field pair's
    ``join_path`` subject is bumped (UUID and name variants alike);
  * identity pair WITHOUT a covering rule ⇒ still stored (it IS information);
  * user source bypasses (ADR-0003 C).
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning

_UUID = "12345678-1234-1234-1234-123456789abc"

# A direct-transform JoinPath covering sys-a.sites.site_code ↔ sys-b.locations.site_code.
_COVERING_JP = [{
    "from_system": "sys-a", "from_field": "sites.site_code",
    "to_system": "sys-b", "to_field": "locations.site_code",
    "transform": "direct",
}]


class FakeTable:
    def __init__(self):
        self.items: list[dict] = []

    def query(self, **kwargs):
        idx = kwargs.get("IndexName")
        vals = kwargs.get("ExpressionAttributeValues", {})
        if idx == "subject-key-index":
            return {"Items": [i for i in self.items
                              if i.get("subject_key") == vals.get(":sk")]}
        if idx == "status-index":
            return {"Items": [i for i in self.items
                              if i.get("status") == vals.get(":status")]}
        return {"Items": [i for i in self.items
                          if i.get("mapping_id") == vals.get(":mid")]}

    def put_item(self, Item):
        self.items.append(dict(Item))

    def delete_item(self, Key):
        self.items = [i for i in self.items
                      if not (i.get("mapping_id") == Key["mapping_id"]
                              and i.get("discovered_at") == Key["discovered_at"])]


@pytest.fixture()
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    # Staging buffer off for the direct-store control cases; the collapse path
    # runs BEFORE staging, so corroboration rows are unaffected by this.
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    self_learning._reset_accepted_coverage_cache()
    return fake


def _graph(monkeypatch, rows):
    monkeypatch.setattr(self_learning, "_neptune_query",
                        lambda c, p=None: rows if "JoinPath" in c else [])


def _log_self_map(value, *, source="agent"):
    return self_learning.log_proposed_mapping(
        "sys-a", "sites.site_code", value,
        "sys-b", "locations.site_code", value,
        "site-id", 0.9, f"resolved {value} across systems",
        proposed_type="value_mapping", source=source,
    )


def _staged_rows(fake):
    return [i for i in fake.items
            if str(i.get("mapping_id", "")).startswith("staged#")]


@pytest.mark.parametrize("value", ["SITE9", _UUID])
def test_identity_value_pair_with_direct_joinpath_not_stored(
        monkeypatch, table, value):
    # Named regression: the SITE9→SITE9 self-map family collapses — covers the
    # name variant AND the UUID variant (normalized value equality).
    _graph(monkeypatch, _COVERING_JP)
    assert _log_self_map(value) is None
    # No lifecycle row of any status — only the staged corroboration exists.
    assert [i for i in table.items if "status" in i] == []


def test_corroboration_counted_instead(monkeypatch, table):
    _graph(monkeypatch, _COVERING_JP)
    assert _log_self_map("SITE9") is None
    (staged,) = _staged_rows(table)
    assert staged["subject_key"].startswith("staged#join_path|")
    assert len(staged["staged_digests"]) == 1
    # A second distinct collapsed observation bumps the SAME buffer row.
    assert _log_self_map("MKE1") is None
    (staged,) = _staged_rows(table)
    assert len(staged["staged_digests"]) == 2
    assert self_learning.get_pending_mappings() == []


def test_identity_pair_without_covering_joinpath_still_stored(
        monkeypatch, table):
    # No covering rule ⇒ the identical value crossing systems IS information.
    _graph(monkeypatch, [])
    mid = _log_self_map("SITE9")
    assert mid is not None
    (stored,) = [i for i in table.items if i.get("mapping_id") == mid]
    assert stored["status"] == "pending"
    assert _staged_rows(table) == []


def test_non_direct_transform_joinpath_does_not_collapse(monkeypatch, table):
    # A transforming rule does not explain a verbatim-identical pair.
    rows = [dict(_COVERING_JP[0], transform="strip_prefix:PH-")]
    _graph(monkeypatch, rows)
    assert _log_self_map("SITE9") is not None


def test_reversed_direction_joinpath_still_collapses(monkeypatch, table):
    rows = [{
        "from_system": "sys-b", "from_field": "locations.site_code",
        "to_system": "sys-a", "to_field": "sites.site_code",
        "transform": "identity",
    }]
    _graph(monkeypatch, rows)
    assert _log_self_map("SITE9") is None


def test_user_source_never_collapsed(monkeypatch, table):
    _graph(monkeypatch, _COVERING_JP)
    assert _log_self_map("SITE9", source="user") is not None
