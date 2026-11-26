# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tulip table-id resolution in the WIZARD (REST) onboarding path.

The wizard's REST discover writes no onboarding session, so a table-id lookup
that read only the chat session's saved ``discovered_tables`` would resolve NO
id: every Tulip sample would come back ``[]``, the membership probe could not
resolve a table, and the resolve step would consider no Tulip<->ERP pair.

The fix resolves the id from the Tulip Tables API itself (one bounded, read-only
``GET /api/v3/tables`` through the shared ``_TulipClient``) when no saved session
discovery has it, matching the discovered table NAME (label) exactly, caching the
name->id map for the resolve job's lifetime, failing closed to ``""`` on any
error. The session path stays first.

Fully synthetic — ``_TulipClient.get`` and the onboarding-session store are
stubbed, no network, no AWS.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.identity_affinity as ia  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.resolve_evidence as re_mod  # noqa: E402
import agents.system_agent as sa  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402

#: One Tulip table named "Orders" with id "t1" and one run-number column.
_TABLES_LIST = [
    {"id": "t1", "label": "Orders"},
    {"id": "t2", "label": "Scrap"},
]
_RECORDS = [
    {"lzwyu_erp_order_id": "RUN-20261021-00001", "qty": 10},
    {"lzwyu_erp_order_id": "RUN-20261021-00002", "qty": 20},
    {"lzwyu_erp_order_id": "RUN-20261021-00001", "qty": 30},   # duplicate
    {"lzwyu_erp_order_id": "", "qty": 40},                      # dropped
]


@pytest.fixture(autouse=True)
def _reset_memo():
    re_mod._reset_sample_memo()
    if hasattr(re_mod, "_reset_tulip_table_id_memo"):
        re_mod._reset_tulip_table_id_memo()
    yield
    re_mod._reset_sample_memo()
    if hasattr(re_mod, "_reset_tulip_table_id_memo"):
        re_mod._reset_tulip_table_id_memo()


@pytest.fixture
def _no_session(monkeypatch):
    """The wizard path: NO onboarding session exists, so the store raises
    SessionNotFoundError (exactly what the REST discover leaves behind)."""
    from agents.onboarding_session import SessionNotFoundError

    class _FakeStore:
        def get_session(self, system_id):
            raise SessionNotFoundError(system_id)

    monkeypatch.setattr("agents.onboard_agent._store", lambda: _FakeStore())


@pytest.fixture
def _stub_conn(monkeypatch):
    """A resolved connection carrying base_url + secret_arn, no node/catalog hit."""
    monkeypatch.setattr(re_mod, "_tulip_connection_config",
                        lambda system_id, cfg: {"base_url": "https://h", "secret_arn": "arn",
                                                **(cfg or {})})


def _stub_client(monkeypatch, tables=_TABLES_LIST, records=_RECORDS, capture=None, fail_list=False):
    """Stub ``_TulipClient.get`` to answer the tables LIST and a records read."""
    def _get(self, path, params=None):
        scoped = self._scoped(path)
        if capture is not None:
            capture.append({"path": path, "scoped": scoped, "params": dict(params or {})})
        if scoped.endswith("/tables"):
            if fail_list:
                raise RuntimeError("tables list down")
            return tables
        return records
    monkeypatch.setattr(sa._TulipClient, "get", _get)


# --------------------------------------------------------------------------- #
# 1. _sample_tulip_records resolves the id from a stubbed GET /tables
# --------------------------------------------------------------------------- #

def test_sample_resolves_id_from_live_tables_list_when_no_session(
        monkeypatch, _no_session, _stub_conn):
    cap = []
    _stub_client(monkeypatch, capture=cap)
    vals = re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=5, field="Orders.lzwyu_erp_order_id", table="Orders")
    assert vals == ["RUN-20261021-00001", "RUN-20261021-00002"]
    # It listed the tables, then read records of the resolved id "t1".
    paths = [c["scoped"] for c in cap]
    assert "/api/v3/tables" in paths
    assert "/api/v3/tables/t1/records" in paths


# --------------------------------------------------------------------------- #
# 2. _probe_tulip_records resolves the id the same way
# --------------------------------------------------------------------------- #

def test_probe_resolves_id_from_live_tables_list_when_no_session(
        monkeypatch, _no_session, _stub_conn):
    import json

    def _get(self, path, params=None):
        scoped = self._scoped(path)
        if scoped.endswith("/tables"):
            return _TABLES_LIST
        filters = json.loads((params or {}).get("filters") or "[]")
        wanted = {f["arg"] for f in filters if f.get("functionType") == "equal"}
        present = {"RUN-20261021-00001"}
        return [{"lzwyu_erp_order_id": v} for v in sorted(present & wanted)]
    monkeypatch.setattr(sa._TulipClient, "get", _get)
    found = re_mod.probe_field_values(
        system_id="tulip", connection_type=TULIP, schema="",
        table="Orders", field="Orders.lzwyu_erp_order_id",
        values=["RUN-20261021-00001", "RUN-20261021-99999"])
    assert found == ["RUN-20261021-00001"]


# --------------------------------------------------------------------------- #
# 3. provision known-tables seed resolves ids in the wizard path
# --------------------------------------------------------------------------- #

def test_provision_seed_gets_ids_from_live_list_when_no_session(
        monkeypatch, _no_session, _stub_conn):
    _stub_client(monkeypatch)
    rows = onboard._tulip_tables_from_discovery(
        _ctx(system_id="tulip", base_url="https://h", secret_arn="arn"),
        {"base_url": "https://h", "secret_arn": "arn"})
    by_name = {name: (tid, cols) for name, tid, cols in rows}
    assert by_name.get("Orders", (None,))[0] == "t1"
    assert by_name.get("Scrap", (None,))[0] == "t2"


# --------------------------------------------------------------------------- #
# 4. the live list goes through _TulipClient, once per system, workspace-scoped
# --------------------------------------------------------------------------- #

def test_live_list_is_workspace_scoped(monkeypatch, _no_session):
    monkeypatch.setattr(re_mod, "_tulip_connection_config",
                        lambda s, c: {"base_url": "https://h", "secret_arn": "arn",
                                      "workspace_id": "ws1"})
    cap = []
    _stub_client(monkeypatch, capture=cap)
    re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn", "workspace_id": "ws1"},
        limit=5, field="lzwyu_erp_order_id", table="Orders")
    listed = [c for c in cap if c["scoped"].endswith("/tables")]
    assert listed and all(c["scoped"] == "/api/v3/w/ws1/tables" for c in listed)


def test_live_list_runs_once_per_system_for_many_fields(monkeypatch, _no_session, _stub_conn):
    cap = []
    _stub_client(monkeypatch, capture=cap)
    for field in ("Orders.lzwyu_erp_order_id", "Orders.qty"):
        re_mod._sample_tulip_records(
            "tulip", {"base_url": "https://h", "secret_arn": "arn"},
            limit=5, field=field, table="Orders")
    listed = [c for c in cap if c["scoped"].endswith("/tables")]
    assert len(listed) == 1, f"expected ONE /tables list, saw {len(listed)}"


# --------------------------------------------------------------------------- #
# 5. an invalid id from the list is never used
# --------------------------------------------------------------------------- #

def test_invalid_id_from_list_is_never_used(monkeypatch, _no_session, _stub_conn):
    cap = []
    _stub_client(monkeypatch, tables=[{"id": "../etc/passwd", "label": "Orders"}], capture=cap)
    vals = re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=5, field="lzwyu_erp_order_id", table="Orders")
    assert vals == []
    assert not any("etc/passwd" in c["scoped"] or "/records" in c["scoped"] for c in cap)


# --------------------------------------------------------------------------- #
# 6. a list failure fails closed exactly as before
# --------------------------------------------------------------------------- #

def test_sample_list_failure_returns_empty(monkeypatch, _no_session, _stub_conn):
    _stub_client(monkeypatch, fail_list=True)
    assert re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=5, field="lzwyu_erp_order_id", table="Orders") == []


def test_probe_list_failure_returns_none(monkeypatch, _no_session, _stub_conn):
    _stub_client(monkeypatch, fail_list=True)
    assert re_mod.probe_field_values(
        system_id="tulip", connection_type=TULIP, schema="",
        table="Orders", field="Orders.lzwyu_erp_order_id",
        values=["RUN-20261021-00001"]) is None


def test_provision_seed_list_failure_degrades_to_names(monkeypatch, _no_session, _stub_conn):
    _stub_client(monkeypatch, fail_list=True)
    rows = onboard._tulip_tables_from_discovery(
        _ctx(system_id="tulip", base_url="https://h", secret_arn="arn"),
        {"base_url": "https://h", "secret_arn": "arn"})
    assert rows == []   # no ids and no names from the list -> degrade path (names from cm_items)


# --------------------------------------------------------------------------- #
# 7. the saved session still wins (no extra /tables call when it has the table)
# --------------------------------------------------------------------------- #

def test_session_path_still_first_no_live_list(monkeypatch, _stub_conn):
    class _FakeStore:
        def get_session(self, system_id):
            return {"discovered_tables": [
                {"name": "Orders", "table_id": "sess-id",
                 "columns": [{"name": "lzwyu_erp_order_id"}]}]}

    monkeypatch.setattr("agents.onboard_agent._store", lambda: _FakeStore())
    cap = []
    _stub_client(monkeypatch, capture=cap)
    vals = re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=5, field="lzwyu_erp_order_id", table="Orders")
    assert vals == ["RUN-20261021-00001", "RUN-20261021-00002"]
    assert "/api/v3/tables" not in [c["scoped"] for c in cap]       # session hit, no list
    assert "/api/v3/tables/sess-id/records" in [c["scoped"] for c in cap]


# --------------------------------------------------------------------------- #
# 8. WIZARD end-to-end: no session -> affinity pass CONSIDERS the Tulip<->ERP pair
# --------------------------------------------------------------------------- #

def _ctx(system_id, base_url, secret_arn):
    """Minimal provision-state stand-in for _tulip_tables_from_discovery."""
    class _Ctx:
        pass
    c = _Ctx()
    c.system_id = system_id
    c.base_url = base_url
    c.secret_arn = secret_arn
    c.token = ""
    c.cm_items = []
    return c


def test_affinity_pass_considers_tulip_erp_pair_with_no_session(monkeypatch, _no_session):
    """At the affinity-pass seam: a Tulip system with NO chat session and a
    peer ERP field holding the same run numbers. The Tulip side samples
    non-empty, so the pair is CONSIDERED. ``production-order`` is
    catalog-related to the ERP's ``production-run``, so the pass emits one
    exact-agreement rule for the pair."""
    import json

    shared = [f"RUN-20261021-{i:05d}" for i in range(40)]

    # Tulip connection (no node/catalog): base_url + secret_arn present.
    monkeypatch.setattr(re_mod, "_tulip_connection_config",
                        lambda s, c: {"base_url": "https://h", "secret_arn": "arn", **(c or {})})

    def _tulip_get(self, path, params=None):
        scoped = self._scoped(path)
        if scoped.endswith("/tables"):
            return [{"id": "t1", "label": "Orders"}]
        # records read (sample) and membership probe both hit /records
        filters = json.loads((params or {}).get("filters") or "[]")
        if filters:
            wanted = {f["arg"] for f in filters}
            return [{"lzwyu_erp_order_id": v} for v in shared if v in wanted]
        return [{"lzwyu_erp_order_id": v} for v in shared]
    monkeypatch.setattr(sa._TulipClient, "get", _tulip_get)

    # Catalog: the Tulip system (new) and the ERP peer.
    entries = {
        "tulip": {"connection_type": TULIP, "schema": "",
                  "connection_config": {"base_url": "https://h", "secret_arn": "arn"},
                  "system_type": "MES"},
        "erp": {"connection_type": "PostgreSQL", "schema": "erp",
                "connection_config": {}, "system_type": "ERP"},
    }
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: entries.get(sid))
    monkeypatch.setattr(onboard.catalog, "get_onboarded_ids", lambda: {"erp"})

    # The peer ERP identifier field (graph row), tagged production-run.
    monkeypatch.setattr(onboard, "neptune_query", lambda *a, **k: [{
        "sys_id": "erp", "sys_schema": "erp", "table_name": "production_runs",
        "field_name": "run_number", "concept": "production-run", "concepts": ["production-run"],
        onboard.REMOVAL_STARTED_COLUMN.split(" AS ")[-1].strip(): None,
    }])
    monkeypatch.setattr(onboard, "removed_ids", lambda *a, **k: set())

    # The ERP side (PostgreSQL) sampled + probed via the postgres helpers.
    monkeypatch.setattr(re_mod, "_sample_postgres",
                        lambda schema, table, field, limit: list(shared))

    def _pg_probe(schema, table, field, vals):
        return {"rows": [[v] for v in vals if v in shared]}
    monkeypatch.setattr(re_mod, "_probe_query", lambda s, t, f, v: _pg_probe(s, t, f, v))

    # The Tulip mapping the wizard produced: ERP Order ID tagged production-order.
    from agents.identity_affinity import ExactPassStats, find_affinity_rules
    new_fields = onboard._affinity_new_fields("tulip", [{
        "table": "Orders", "field": "Orders.lzwyu_erp_order_id",
        "concept": "production-order", "role": "identifier",
        "concepts": [{"concept": "production-order", "role": "identifier"}],
    }])
    peer_fields = onboard._affinity_existing_fields("tulip")
    assert new_fields and peer_fields
    assert all(f.values for f in new_fields), "Tulip side must now sample non-empty"

    stats = ExactPassStats()
    rules = find_affinity_rules(new_fields, peer_fields, probe=onboard._affinity_probe(), stats=stats)
    assert stats.considered >= 1, f"pair never considered: {stats.summary()}"
    # production-order is catalog-related to production-run, so the pair qualifies.
    assert stats.refused_concept == 0, f"unexpected concept refusal: {stats.summary()}"
    assert stats.emitted == 1, f"expected one emitted rule: {stats.summary()}"
    assert len(rules) == 1
    assert rules[0]["method"] == ia.EXACT_METHOD
    assert rules[0]["concept"] == "production-order"
