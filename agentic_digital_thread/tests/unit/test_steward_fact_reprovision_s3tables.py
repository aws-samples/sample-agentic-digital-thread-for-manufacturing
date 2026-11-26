# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two live gaps on an S3 Tables system whose SchemaEntry is the qualified name
``process_data_global.segment_record_details_raw``:

1. GET record-key / row-exclusion by the TERMINAL name returned empty; only the
   qualified name worked. Both GETs must resolve the name like the PUT.
2. A re-provision wiped the row exclusion (and the record key): the carry across
   the schema rewrite (``row_exclusion.begin_rewrite`` / ``restore_after_rewrite``)
   was gated Athena-only, and the record key had no carry at all.

These tests run the REAL sync routes and the REAL provision rewrite path with
the graph faked. Athena is the regression guard.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agents.admin_routes as admin_routes
import agents.onboard as onboard
import agents.record_key as record_key
import agents.row_exclusion as row_exclusion
import agents.row_exclusion_manifest as manifest
import agents.schema_fields as schema_fields
from agents.connection_types import ATHENA, S3TABLES_DUCKDB

SYS = "s3t-steward"
TERMINAL = "segment_record_details_raw"
QUALIFIED = f"process_data_global.{TERMINAL}"
COLUMNS = {"id": "varchar", "segment_type": "varchar", "route_name": "varchar"}
RK = [{"column": "id", "coalesced": False}, {"column": "segment_type", "coalesced": False}]
RX_LABEL = "non-record rows"
RX_CONDITIONS = [{"column": "route_name", "op": "not_empty"}]


class _Graph:
    """A graph that honours the record-key, row-exclusion, manifest, schema and
    name-resolution reads/writes the steward routes and the provision rewrite
    issue. ``entries`` maps the real SchemaEntry id -> {name, fields, props}."""

    def __init__(self, entry_name=QUALIFIED, columns=None):
        self.entries = {f"{SYS}.{entry_name}": {"name": entry_name,
                                                 "fields": dict(columns or COLUMNS), "props": {}}}
        self.system_props: dict = {}     # System node props (row_exclusion_manifest)

    # -- reads -------------------------------------------------------------
    def query(self, cypher, params=None):
        p = dict(params or {})
        seid = p.get("seid")
        if "RETURN se.name AS name" in cypher and "seid" not in p:
            return [{"name": e["name"]} for e in self.entries.values()]
        if "RETURN count(se) AS n" in cypher:
            return [{"n": 1 if seid in self.entries else 0}]
        if "f.name AS name, f.data_type AS data_type" in cypher and "se.id IN $seids" in cypher:
            out = []
            for s in (p.get("seids") or []):
                for n, t in self.entries.get(s, {}).get("fields", {}).items():
                    out.append({"seid": s, "name": n, "data_type": t})
            return out
        if "f.name AS name, f.data_type AS data_type" in cypher:
            # FIELD_TYPES (one table by seid)
            return [{"name": n, "data_type": t}
                    for n, t in self.entries.get(seid, {}).get("fields", {}).items()]
        if "RETURN f.name AS name" in cypher and "HAS_FIELD" in cypher:
            return [{"name": n} for n in self.entries.get(seid, {}).get("fields", {})]
        if "s.row_exclusion_manifest AS row_exclusion_manifest" in cypher:
            return [{"row_exclusion_manifest": self.system_props.get("row_exclusion_manifest")}]
        if "s.record_key_rewrite AS record_key_rewrite" in cypher:
            return [{"record_key_rewrite": self.system_props.get("record_key_rewrite")}]
        if "RETURN se.record_key AS record_key" in cypher:
            if seid not in self.entries:
                return []  # a deleted SchemaEntry yields no row (not a row with a null key)
            pr = self.entries.get(seid, {}).get("props", {})
            return [{"record_key": pr.get("record_key"), "record_key_by": pr.get("record_key_by"),
                     "record_key_at": pr.get("record_key_at"),
                     "record_key_source": pr.get("record_key_source")}]
        if "se.id AS seid" in cypher and "se.id IN $seids" in cypher:
            # READ_ROW_EXCLUSIONS or READ_ROW_EXCLUSION_RECORDS (records has _by)
            records = "row_exclusion_by AS row_exclusion_by" in cypher
            out = []
            for s in (p.get("seids") or []):
                pr = self.entries.get(s, {}).get("props", {})
                if not pr.get("row_exclusion"):
                    continue
                row = {"seid": s, "row_exclusion": pr.get("row_exclusion")}
                if records:
                    row.update(row_exclusion_by=pr.get("row_exclusion_by"),
                               row_exclusion_at=pr.get("row_exclusion_at"),
                               row_exclusion_source=pr.get("row_exclusion_source"))
                else:
                    row.update(set_at=pr.get("row_exclusion_at"),
                               set_source=pr.get("row_exclusion_source"))
                out.append(row)
            return out
        if "RETURN se.row_exclusion AS row_exclusion" in cypher:
            pr = self.entries.get(seid, {}).get("props", {})
            return [{"row_exclusion": pr.get("row_exclusion"),
                     "row_exclusion_by": pr.get("row_exclusion_by"),
                     "row_exclusion_at": pr.get("row_exclusion_at"),
                     "row_exclusion_source": pr.get("row_exclusion_source")}]
        raise AssertionError(f"unexpected read cypher: {cypher!r}")

    # -- writes ------------------------------------------------------------
    def write(self, cypher, params=None):
        p = dict(params or {})
        if "SET s.row_exclusion_manifest = $manifest" in cypher:
            if (self.system_props.get("row_exclusion_manifest") or "") != (p.get("expected") or ""):
                return []
            self.system_props["row_exclusion_manifest"] = p.get("manifest")
            return [{"id": SYS}]
        if "SET s.record_key_rewrite = $carry" in cypher:
            self.system_props["record_key_rewrite"] = p.get("carry")
            return [{"id": SYS}]
        seid = p.get("seid")
        if "SET se.record_key = $record_key" in cypher:
            e = self.entries.get(seid)
            if e is None:
                return []
            e["props"].update(record_key=p.get("record_key"), record_key_by=p.get("by"),
                              record_key_at=p.get("at"), record_key_source=p.get("source"))
            return [{"seid": seid}]
        if "SET se.row_exclusion = $row_exclusion" in cypher:
            if (self.system_props.get("row_exclusion_manifest") or "") != (p.get("expected") or ""):
                return []
            e = self.entries.get(seid)
            if e is None:
                return []
            e["props"].update(row_exclusion=p.get("row_exclusion"), row_exclusion_by=p.get("by"),
                              row_exclusion_at=p.get("at"), row_exclusion_source=p.get("source"))
            return [{"seid": seid}]
        return []

    # -- rewrite seams -----------------------------------------------------
    def delete_schema(self, sid):
        self.entries = {k: e for k, e in self.entries.items() if not k.startswith(f"{sid}.")}
        return True

    def recreate(self, sid, entry_name, columns=None):
        self.entries[f"{sid}.{entry_name}"] = {"name": entry_name,
                                                "fields": dict(columns or COLUMNS), "props": {}}


@pytest.fixture(autouse=True)
def _fresh():
    for mod in (record_key, row_exclusion):
        mod.reset()
    yield
    for mod in (record_key, row_exclusion):
        mod.reset()


def _set_record_key(g, table=TERMINAL, ctype=S3TABLES_DUCKDB):
    return admin_routes.set_record_key_sync(
        SYS, {"table": table, "record_key": RK}, "steward",
        connection_type=ctype, seams=(g.write, g.query))


def _set_row_exclusion(g, table=TERMINAL, ctype=S3TABLES_DUCKDB):
    return admin_routes.set_row_exclusion_sync(
        SYS, {"table": table, "label": RX_LABEL, "conditions": RX_CONDITIONS}, "steward",
        connection_type=ctype, seams=(g.write, g.query))


def _body(status):
    return json.loads(bytes(status.body)) if hasattr(status, "body") else status


# --------------------------------------------------------------------------- #
# 1. Both GETs resolve the submitted name (terminal or qualified)             #
# --------------------------------------------------------------------------- #
def test_get_record_key_resolves_terminal_name(monkeypatch):
    g = _Graph()
    monkeypatch.setattr(admin_routes, "_record_key_graph_seams", lambda: (g.write, g.query))
    assert _set_record_key(g).get("status") == "ok"
    by_terminal = admin_routes._get_record_key_sync(SYS, TERMINAL)
    by_qualified = admin_routes._get_record_key_sync(SYS, QUALIFIED)
    assert by_terminal["declared_key"] and by_terminal == by_qualified


def test_get_row_exclusion_resolves_terminal_name(monkeypatch):
    g = _Graph()
    monkeypatch.setattr(admin_routes, "_row_exclusion_graph_seams", lambda: (g.write, g.query))
    assert _set_row_exclusion(g).get("status") == "ok"
    by_terminal = admin_routes._get_row_exclusion_sync(SYS, TERMINAL)
    by_qualified = admin_routes._get_row_exclusion_sync(SYS, QUALIFIED)
    assert by_terminal["predicate"] and by_terminal == by_qualified


def test_get_ambiguous_terminal_is_400(monkeypatch):
    g = _Graph()
    g.recreate(SYS, f"other_ns.{TERMINAL}")     # a second namespace, same terminal
    monkeypatch.setattr(admin_routes, "_record_key_graph_seams", lambda: (g.write, g.query))
    monkeypatch.setattr(admin_routes, "_row_exclusion_graph_seams", lambda: (g.write, g.query))
    rk = admin_routes._get_record_key_sync(SYS, TERMINAL)
    rx = admin_routes._get_row_exclusion_sync(SYS, TERMINAL)
    assert getattr(rk, "status_code", None) == 400 and _body(rk)["reason"] == "ambiguous_table"
    assert getattr(rx, "status_code", None) == 400 and _body(rx)["reason"] == "ambiguous_table"


def test_get_unknown_qualified_name_is_the_empty_answer(monkeypatch):
    g = _Graph()
    monkeypatch.setattr(admin_routes, "_record_key_graph_seams", lambda: (g.write, g.query))
    # A wrong qualified name resolves to no entry; the store answers empty.
    out = admin_routes._get_record_key_sync(SYS, "wrong_ns.nope")
    assert out["declared_key"] == []


def test_get_athena_single_part_name_unchanged(monkeypatch):
    g = _Graph(entry_name="segments", columns={"id": "varchar", "route_name": "varchar"})
    monkeypatch.setattr(admin_routes, "_record_key_graph_seams", lambda: (g.write, g.query))
    admin_routes.set_record_key_sync(
        SYS, {"table": "segments", "record_key": [{"column": "id", "coalesced": False}]},
        "steward", connection_type=ATHENA, seams=(g.write, g.query))
    out = admin_routes._get_record_key_sync(SYS, "segments")
    assert [c["column"] for c in out["declared_key"]] == ["id"]


# --------------------------------------------------------------------------- #
# 2. A re-provision carries every steward fact across the schema rewrite       #
# --------------------------------------------------------------------------- #
def _run_rewrite(monkeypatch, g, *, connection_type, recreate_name=QUALIFIED):
    """b0's start (take record keys + row exclusions, mark, delete), the
    replacement SchemaEntry (b..b2a), then the restores -- the real pipeline
    steps for the schema rewrite."""
    monkeypatch.setattr(row_exclusion, "_provision_graph", lambda: (g.write, g.query))
    monkeypatch.setattr(onboard, "neptune_query", g.query)
    monkeypatch.setattr(onboard, "neptune_query_checked", g.query)
    monkeypatch.setattr(onboard, "neptune_write_resubmit_safe", g.write)
    monkeypatch.setattr(onboard, "_mark_schema_rewrite", lambda _sid: 1)
    monkeypatch.setattr(onboard, "_delete_stale_schema_subgraph", lambda sid: g.delete_schema(sid))
    ctx = SimpleNamespace(system_id=SYS, connection_type=connection_type,
                          tables_seen={recreate_name}, row_exclusions_owner=None,
                          row_exclusions_taken=None, row_exclusions_restored=None,
                          record_keys_taken=None, record_keys_restored=False)
    onboard._begin_schema_rewrite(ctx)
    g.recreate(SYS, recreate_name)
    onboard._dp_restore_record_keys(ctx)
    onboard._dp_restore_row_exclusions(ctx)
    return ctx


def test_reprovision_keeps_row_exclusion_and_record_key_on_s3tables(monkeypatch):
    g = _Graph()
    monkeypatch.setattr(admin_routes, "_record_key_graph_seams", lambda: (g.write, g.query))
    monkeypatch.setattr(admin_routes, "_row_exclusion_graph_seams", lambda: (g.write, g.query))
    assert _set_record_key(g).get("status") == "ok"
    assert _set_row_exclusion(g).get("status") == "ok"

    _run_rewrite(monkeypatch, g, connection_type=S3TABLES_DUCKDB)

    # Both steward facts survive, readable by the terminal name after the rewrite.
    rk = admin_routes._get_record_key_sync(SYS, TERMINAL)
    rx = admin_routes._get_row_exclusion_sync(SYS, TERMINAL)
    assert [c["column"] for c in rk["declared_key"]] == ["id", "segment_type"]
    assert rx["predicate"] and rx["conditions"]


def test_reprovision_keeps_steward_facts_on_athena(monkeypatch):
    g = _Graph(entry_name="segments", columns={"id": "varchar", "route_name": "varchar"})
    monkeypatch.setattr(admin_routes, "_record_key_graph_seams", lambda: (g.write, g.query))
    monkeypatch.setattr(admin_routes, "_row_exclusion_graph_seams", lambda: (g.write, g.query))
    admin_routes.set_record_key_sync(
        SYS, {"table": "segments", "record_key": [{"column": "id", "coalesced": False}]},
        "steward", connection_type=ATHENA, seams=(g.write, g.query))
    admin_routes.set_row_exclusion_sync(
        SYS, {"table": "segments", "label": RX_LABEL, "conditions": RX_CONDITIONS},
        "steward", connection_type=ATHENA, seams=(g.write, g.query))

    _run_rewrite(monkeypatch, g, connection_type=ATHENA, recreate_name="segments")

    rk = admin_routes._get_record_key_sync(SYS, "segments")
    rx = admin_routes._get_row_exclusion_sync(SYS, "segments")
    assert [c["column"] for c in rk["declared_key"]] == ["id"]
    assert rx["predicate"]


# --------------------------------------------------------------------------- #
# Review live-carry: the three record-key carry findings (folded probes)       #
# --------------------------------------------------------------------------- #
def test_record_key_snapshot_failure_aborts_before_schema_delete(monkeypatch):
    # F1: the snapshot is a CHECKED read; a read fault raises so the provision
    # aborts BEFORE the schema delete, never losing the key with record_keys_taken=None.
    ctx = SimpleNamespace(system_id=SYS, connection_type=S3TABLES_DUCKDB,
                          tables_seen={QUALIFIED})
    monkeypatch.setattr(onboard, "neptune_query_checked",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph unavailable")))
    with pytest.raises(RuntimeError, match="record key"):
        onboard._take_record_keys(ctx)


def test_record_key_with_removed_column_is_not_restored():
    # F2: a key whose column is gone from the recreated Fields is dropped, not
    # written back as an invalid stored key.
    g = _Graph(columns=COLUMNS)
    out = admin_routes.set_record_key_sync(
        SYS, {"table": QUALIFIED, "record_key": RK}, "steward",
        connection_type=S3TABLES_DUCKDB, seams=(g.write, g.query))
    assert out.get("status") == "ok"
    carried = record_key.take_for_rewrite(SYS, [QUALIFIED], query=g.query)
    g.delete_schema(SYS)
    g.recreate(SYS, QUALIFIED, columns={"id": "varchar"})     # segment_type gone

    outcome = record_key.restore_after_rewrite(SYS, carried, write=g.write, query=g.query)

    assert not g.entries[f"{SYS}.{QUALIFIED}"]["props"].get("record_key")
    assert QUALIFIED in outcome.get("dropped", [])


def test_restore_does_not_overwrite_steward_write_after_recreate():
    # F3: a steward key set after the SchemaEntry is recreated is not clobbered
    # by the carry's restore.
    g = _Graph(columns=COLUMNS)
    assert admin_routes.set_record_key_sync(
        SYS, {"table": QUALIFIED, "record_key": RK}, "first-steward",
        connection_type=S3TABLES_DUCKDB, seams=(g.write, g.query)).get("status") == "ok"
    carried = record_key.take_for_rewrite(SYS, [QUALIFIED], query=g.query)
    g.delete_schema(SYS)
    g.recreate(SYS, QUALIFIED, columns=COLUMNS)
    assert admin_routes.set_record_key_sync(
        SYS, {"table": QUALIFIED, "record_key": ["id"]}, "second-steward",
        connection_type=S3TABLES_DUCKDB, seams=(g.write, g.query)).get("status") == "ok"

    record_key.restore_after_rewrite(SYS, carried, write=g.write, query=g.query)

    current = record_key.read_record_key(SYS, QUALIFIED, query=g.query)
    assert [t["column"] for t in current["declared_key"]] == ["id"]
    assert current["record_key_by"] == "second-steward"


def test_record_key_put_is_409_while_the_rewrite_marker_stands(monkeypatch):
    # F3: the record-key PUT is refused with rewrite_in_progress while the
    # schema-rewrite marker stands, like the row-exclusion PUT.
    g = _Graph(columns=COLUMNS)
    # Raise the rewrite marker on the System node (the row-exclusion carry's own).
    ctx = SimpleNamespace(system_id=SYS, connection_type=S3TABLES_DUCKDB,
                          tables_seen=set(), row_exclusions_owner=None)
    monkeypatch.setattr(row_exclusion, "_provision_graph", lambda: (g.write, g.query))
    g.system_props["row_exclusion_manifest"] = manifest.serialize({}, rewrite={},
                                                                  owner="owner-tok")
    resp = admin_routes.set_record_key_sync(
        SYS, {"table": QUALIFIED, "record_key": RK}, "steward",
        connection_type=S3TABLES_DUCKDB, seams=(g.write, g.query))
    assert getattr(resp, "status_code", None) == 409
    assert _body(resp)["reason"] == "rewrite_in_progress"


def test_finally_does_not_mark_missing_schemaentry_restore_complete(monkeypatch):
    # F2: a finally-path restore that matches no SchemaEntry (a schema step
    # failed before recreating the table) must not set record_keys_restored=True.
    carried = {QUALIFIED: {"record_key": json.dumps(RK), "record_key_by": "steward",
                           "record_key_at": "2026-11-17T00:00:00Z", "record_key_source": "src"}}
    ctx = SimpleNamespace(system_id=SYS, record_keys_taken=carried, record_keys_restored=False)
    g = _Graph(columns=COLUMNS)
    g.delete_schema(SYS)        # the replacement SchemaEntry was never written
    monkeypatch.setattr(onboard, "neptune_write_resubmit_safe", g.write)
    monkeypatch.setattr(onboard, "neptune_query_checked", g.query)

    onboard._dp_restore_record_keys(ctx)

    assert ctx.record_keys_restored is False


# --------------------------------------------------------------------------- #
# Review live-carry-r2: durable retry carry + checked restore reads            #
# --------------------------------------------------------------------------- #
def test_failed_rewrite_keeps_record_key_snapshot_for_retry(monkeypatch):
    # F1: a failed restore persists the carry on the System node, so a retry's
    # take_for_rewrite recovers it after the SchemaEntries were deleted.
    g = _Graph(columns=COLUMNS)
    assert _set_record_key(g).get("status") == "ok"
    carried = record_key.take_for_rewrite(SYS, [QUALIFIED], query=g.query)
    g.delete_schema(SYS)
    ctx = SimpleNamespace(system_id=SYS, record_keys_taken=carried,
                          record_keys_restored=False)
    monkeypatch.setattr(onboard, "neptune_write_resubmit_safe", g.write)
    monkeypatch.setattr(onboard, "neptune_query_checked", g.query)

    onboard._dp_restore_record_keys(ctx)
    retry_snapshot = record_key.take_for_rewrite(SYS, [QUALIFIED], query=g.query)

    assert ctx.record_keys_restored is False
    assert retry_snapshot == carried          # recovered from the durable carry


def test_fail_soft_current_key_read_cannot_overwrite_new_steward_key(monkeypatch):
    # F2: the restore must read the current key through the CHECKED seam. A
    # fail-soft [] on the UNCHECKED seam (what the pre-fix code used) would read
    # as "no newer key" and overwrite the second steward's key; the checked read
    # sees it and skips. Pre-fix (uses neptune_query) overwrites -> FAIL;
    # post-fix (uses neptune_query_checked) keeps the newer key.
    g = _Graph(columns=COLUMNS)
    assert _set_record_key(g, ctype=S3TABLES_DUCKDB).get("status") == "ok"
    carried = record_key.take_for_rewrite(SYS, [QUALIFIED], query=g.query)
    g.delete_schema(SYS)
    g.recreate(SYS, QUALIFIED, columns=COLUMNS)
    assert admin_routes.set_record_key_sync(
        SYS, {"table": QUALIFIED, "record_key": ["id"]}, "second-steward",
        connection_type=S3TABLES_DUCKDB, seams=(g.write, g.query)).get("status") == "ok"

    def fail_soft_current_key(cypher, params=None):
        if "RETURN se.record_key AS record_key" in cypher:
            return []                         # fail-soft degrade on the unchecked seam
        return g.query(cypher, params)

    ctx = SimpleNamespace(system_id=SYS, record_keys_taken=carried,
                          record_keys_restored=False)
    monkeypatch.setattr(onboard, "neptune_query", fail_soft_current_key)   # pre-fix seam
    monkeypatch.setattr(onboard, "neptune_query_checked", g.query)         # post-fix seam
    monkeypatch.setattr(onboard, "neptune_write_resubmit_safe", g.write)

    onboard._dp_restore_record_keys(ctx)
    current = record_key.read_record_key(SYS, QUALIFIED, query=g.query)

    assert [term["column"] for term in current["declared_key"]] == ["id"]
    assert current["record_key_by"] == "second-steward"   # not overwritten


def test_current_steward_key_supersedes_stale_durable_carry():
    """A durable carry a FAILED attempt persisted must not mask a key the
    steward declared between that attempt and the retry: the current CHECKED
    SchemaEntry key wins per table, so the snapshot (and the carry the caller
    re-persists) carries the newest steward key, never a stale one.

    The carry holds an old-steward key; the live SchemaEntry holds a newer one
    from a different steward. ``take_for_rewrite`` must return the live key."""
    stale = {
        QUALIFIED: {
            "record_key": json.dumps(RK),
            "record_key_by": "old-steward",
            "record_key_at": "2026-11-16T00:00:00Z",
            "record_key_source": "old",
        }
    }
    current = [{"column": "id", "coalesced": False}]

    def query(cypher, params=None):
        if "record_key_rewrite" in cypher:
            return [{"record_key_rewrite": json.dumps(stale)}]
        if "RETURN se.record_key AS record_key" in cypher:
            return [{
                "record_key": json.dumps(current),
                "record_key_by": "new-steward",
                "record_key_at": "2026-11-17T00:00:00Z",
                "record_key_source": "new",
            }]
        raise AssertionError(cypher)

    carried = record_key.take_for_rewrite(SYS, [QUALIFIED], query=query)

    assert json.loads(carried[QUALIFIED]["record_key"]) == current
    assert carried[QUALIFIED]["record_key_by"] == "new-steward"


def test_stale_carry_kept_when_live_schema_entry_is_already_gone():
    """On a genuine retry the SchemaEntries are deleted, so the live read
    returns nothing and the carried key is the only source: it is kept."""
    stale = {QUALIFIED: {"record_key": json.dumps(RK), "record_key_by": "steward",
                         "record_key_at": "2026-11-16T00:00:00Z", "record_key_source": "x"}}

    def query(cypher, params=None):
        if "record_key_rewrite" in cypher:
            return [{"record_key_rewrite": json.dumps(stale)}]
        if "RETURN se.record_key AS record_key" in cypher:
            return []                          # SchemaEntry already deleted
        raise AssertionError(cypher)

    carried = record_key.take_for_rewrite(SYS, [QUALIFIED], query=query)
    assert carried[QUALIFIED]["record_key_by"] == "steward"


def test_current_steward_clear_removes_stale_durable_carry():
    """A steward CLEAR must win over a stale durable carry: when the checked
    current SchemaEntry row EXISTS but has no key, that table is dropped from the
    carry so the next rewrite cannot resurrect the cleared key (review
    live-config-refresh-r2 F4)."""
    stale = {
        QUALIFIED: {
            "record_key": json.dumps(RK),
            "record_key_by": "old-steward",
            "record_key_at": "2026-11-16T00:00:00Z",
            "record_key_source": "old",
        }
    }

    def query(cypher, params=None):
        if "record_key_rewrite" in cypher:
            return [{"record_key_rewrite": json.dumps(stale)}]
        if "RETURN se.record_key AS record_key" in cypher:
            return [{"record_key": None, "record_key_by": "new-steward",
                     "record_key_at": "2026-11-17T00:00:00Z", "record_key_source": None}]
        raise AssertionError(cypher)

    carried = record_key.take_for_rewrite(SYS, [QUALIFIED], query=query)
    assert QUALIFIED not in carried
