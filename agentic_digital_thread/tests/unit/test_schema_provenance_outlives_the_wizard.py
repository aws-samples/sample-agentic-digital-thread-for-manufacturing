# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The schema a Discover stored must outlive the wizard it belongs to.

Discover persists the connector's physical schema as a revision in
``JobStore("schema-provenance")``; Map and Provision rejoin the client's tables
and mappings to it and answer ``400 schema_provenance_invalid`` when it is gone.
The store used the async-job default TTL of one hour, so an onboarding left open
past that (a Provision Retry after a dropped connection, or a long Confirm
Mappings review) could never finish: every Retry sent the same expired revision.

These tests drive the real module store against a shared-table double with a
controlled clock, so they read the TTL the store actually stamps. Its
in-process copies are bounded the same way (TTL and count), so a long-running
server does not keep every revision it ever discovered.
"""
from __future__ import annotations

import pytest

from agents import onboard
from agents.job_store import _BoundedCopies
from tests.unit._shared_table import SharedTable

HOUR = 3600


@pytest.fixture
def provenance_store(monkeypatch):
    """The module's own store, backed by a table double and a fake clock, with
    fresh in-process copies built from the module's own bounds."""
    clock = {"now": 1_000_000.0}
    store = onboard._schema_provenance
    table = SharedTable()
    monkeypatch.setattr(store, "_table", table)
    monkeypatch.setattr(store, "_resolve_table", False)
    monkeypatch.setattr(store, "_backend_ok", True)
    monkeypatch.setattr(store, "_clock", lambda: clock["now"])
    bounds = store._mem
    fresh = (_BoundedCopies(bounds._cap, bounds._evictable, store._ttl_seconds, store._now)
             if isinstance(bounds, _BoundedCopies) else {})
    monkeypatch.setattr(store, "_mem", fresh)
    return clock, table


def _discover(system_id: str, table_name: str = "assets") -> str:
    result = {"tables": [{
        "name": table_name,
        "relation_kind": "base_table",
        "columns": [{"name": "asset_num", "data_type": "varchar"}],
    }]}
    stored = onboard._store_discovered_provenance(system_id, result)
    return stored["schema_revision"]


def test_a_discovered_schema_still_validates_hours_later(provenance_store):
    clock, _ = provenance_store
    revision = _discover("cmms-ind")
    clock["now"] += 3 * HOUR
    onboard._schema_provenance._mem.clear()   # another replica, or a restart
    tables = onboard._load_schema_provenance("cmms-ind", revision)
    assert "assets" in tables


def test_a_discovered_schema_still_expires_eventually(provenance_store):
    clock, _ = provenance_store
    revision = _discover("cmms-ind")
    clock["now"] += onboard.SCHEMA_PROVENANCE_TTL_SECONDS
    onboard._schema_provenance._mem.clear()
    with pytest.raises(ValueError):
        onboard._load_schema_provenance("cmms-ind", revision)


def test_the_in_process_copy_also_expires_at_the_ttl(provenance_store):
    clock, table = provenance_store
    revision = _discover("cmms-ind")
    table.fail_reads = True                   # memory is all this process has
    clock["now"] += 3 * HOUR
    assert "assets" in onboard._load_schema_provenance("cmms-ind", revision)
    clock["now"] += onboard.SCHEMA_PROVENANCE_TTL_SECONDS
    with pytest.raises(ValueError):
        onboard._load_schema_provenance("cmms-ind", revision)
    assert revision not in onboard._schema_provenance._mem


def test_the_in_process_copies_are_bounded(provenance_store):
    cap = onboard.SCHEMA_PROVENANCE_MEMORY_MAX
    revisions = [_discover("cmms-ind", f"t{i}") for i in range(cap + 5)]
    held = onboard._schema_provenance._mem
    assert len(held) == cap
    assert revisions[0] not in held and revisions[-1] in held   # oldest dropped first


def test_the_provenance_store_bounds():
    store = onboard._schema_provenance
    assert onboard.SCHEMA_PROVENANCE_TTL_SECONDS >= 8 * HOUR
    assert store._ttl_seconds == onboard.SCHEMA_PROVENANCE_TTL_SECONDS
    assert isinstance(store._mem, _BoundedCopies)
    assert store._mem._cap == onboard.SCHEMA_PROVENANCE_MEMORY_MAX
