# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A two-system world for provision's re-measurement of a measured rule whose
target system rewrites its schema (``test_identity_rule_provision_r8*.py``).

A cell line (``cell-a``, MES at Plant A) whose run codes are the lot
references of a ledger (``ledger-b``, ERP, all sites). The two columns map to
a catalog-related identifier pair, as the seed's run and lot columns do, so no
concept is shared. SQLite stands in for PostgreSQL. The graph is a fake that
holds fields and their MAPS_TO edges and IdentityRule nodes, and answers each
read the provision code makes: the target-field snapshot (field and concepts
in one read), and the two separate reads 7d0e6784 made (the mark, then the
concepts). ``query`` is ``neptune_query`` (a failed read degrades to ``[]``),
``query_checked`` is ``neptune_query_checked`` (a failed read raises). The
schema-rewrite mark and failed marker are kept, as the provision code keeps
them while the mark moves out of the graph, both on the System node (the
graph fake applies the mark statements to ``marks`` and ``failed``) and in
the shared store (``RewriteStore``, ``onboard._SCHEMA_REWRITE_STATES``, over
the same ``marks`` and ``failed`` plus ``begun``): one mark, wherever it is
read. A fake clock runs the wait: each sleep and each read (``read_cost``)
advances it.
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.db as agents_db  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents import resolve_evidence  # noqa: E402
from agents.catalog import RELATED_IDENTIFIER_CONCEPTS  # noqa: E402
from agents.db import GraphQueryError  # noqa: E402
from agents.identity_affinity import EXACT_METHOD  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402
from tests.unit._rule_check_db import FakeCatalog, RuleCheckDb  # noqa: E402

SRC_SYS, TGT_SYS = "cell-a", "ledger-b"
SYSTEMS = {SRC_SYS: {"system_type": "MES", "plant": "Plant A"},
           TGT_SYS: {"system_type": "ERP", "plant": "All Sites"}}
#: A catalog-related identifier pair: the source column maps the first, the
#: target column the second, and no concept is shared.
SRC_CONCEPT, TGT_CONCEPT = sorted(RELATED_IDENTIFIER_CONCEPTS)[0]
SHARED = [f"CX-2026-{n:05d}" for n in range(101, 109)]          # 8 values in both columns
SRC_ONLY = [f"CX-2026-{n:05d}" for n in range(201, 205)]
TGT_ONLY = [f"CX-2026-{n:05d}" for n in range(301, 305)]
MARK = "schema_rewrite_started"


def _mapping(table, column, concept) -> dict:
    """A map-step row as the seed sends it to Resolve and to provision."""
    return {"table": table, "field": f"{table}.{column}", "concept": concept, "confidence": 0.95,
            "mappings": [{"concept": concept, "role": "identifier", "confidence": 0.95}]}


SRC_MAPPINGS = [_mapping("runs", "run_code", SRC_CONCEPT)]
TGT_MAPPINGS = [_mapping("lot_lines", "lot_ref", TGT_CONCEPT)]
TARGET_FIELD = (TGT_SYS, "lot_lines", "lot_ref")


def schema_of(system_id: str) -> str:
    return system_id.replace("-", "_") + "_db"


def _rows(values) -> list:
    return [(i, v) for i, v in enumerate(values, start=1)]


class _Catalog(FakeCatalog):
    def get_all(self):
        return list(self.entries.values())


class Graph:
    """The graph reads and writes of this path (see the module docstring)."""

    def __init__(self, state):
        self.state = state
        self.fields: dict = {}          # (sid, table, field) -> [(concept, role, confidence)]
        self.field_ids: dict = {}       # Field id -> (sid, table, field)
        self.marks: dict = {}           # sid -> the schema-rewrite mark (epoch ms)
        self.failed: dict = {}          # sid -> the failed marker (epoch ms)
        self.begun: dict = {}           # sid -> the last rewrite's token
        self.mark_writes: list = []     # (cypher, params) of every System-node mark write
        #: (sid, record before, record written) of every write of the store.
        self.store_log: list = []
        self.store = RewriteStore(self)
        #: ``cypher -> bool``: which writes the graph refuses (``neptune_write`` False).
        self.fail_writes = None
        self.nodes: dict = {}
        #: Called once, right after the store read that ends a target-field
        #: snapshot (the state it returns is the one read before the call).
        self.after_mark_read = None
        self._snapshot_read = False
        #: ``cypher -> bool``: which reads fail.
        self.fail_reads = None
        #: Seconds each read of the target field (or its mark) takes on the fake clock.
        self.read_cost = 0.0

    def add_field(self, sid, table, field, *edges):
        self.fields[(sid, table, field)] = list(edges)
        self.field_ids[f"{sid}.{table}.{field}"] = (sid, table, field)

    def write(self, cypher, params=None):
        params = params or {}
        if self.fail_writes and self.fail_writes(cypher):
            return False
        if cypher.startswith("MERGE (ir:IdentityRule"):
            node = self.nodes.setdefault(params["rid"], {})
            for prop, param in re.findall(r"ir\.(\w+) = \$(\w+)", cypher):
                node[prop] = params[param]
        elif "ir.method IS NULL" in cypher and "SET ir.method = 'onboarding-llm'" in cypher:
            for rid, node in self.nodes.items():
                if rid.startswith(params["prefix"]) and not node.get("method"):
                    node["method"] = "onboarding-llm"
        elif cypher == onboard._SCHEMA_SUBGRAPH_DELETE_CYPHER:
            self.fields = {k: v for k, v in self.fields.items() if k[0] != params["sid"]}
        elif cypher == onboard._RETIRED_LINK_DELETE_CYPHER:          # the retired-link sweep
            key = self.field_ids.get(params["fid"])
            if key in self.fields:
                self.fields[key] = [e for e in self.fields[key] if e[0] != params["concept"]]
        elif cypher.startswith("MERGE (f:Field {id: $fid})"):
            sid, table = params["schema_id"].split(".", 1)
            self.fields.setdefault((sid, table, params["name"]), [])
            self.field_ids[params["fid"]] = (sid, table, params["name"])
        elif "MERGE (f)-[r:MAPS_TO]->(c)" in cypher:
            key = self.field_ids[params["fid"]]
            self.fields.setdefault(key, []).append((params["cid"], params["role"], params["conf"]))
        elif "schema_rewrite_" in cypher:
            self.mark_writes.append((cypher, dict(params)))
            self._mark_write(cypher, params)
        return True

    def _mark_write(self, cypher, params):
        """A write of the System node's schema-rewrite properties: its WHERE
        (the owner and compare-and-clear conditions the provision code
        writes), then its SET and REMOVE clauses."""
        sid = params["sid"]
        props = {"schema_rewrite_started": self.marks, "schema_rewrite_failed": self.failed}
        where = re.search(r"\bWHERE (.*?)(?: SET | REMOVE |$)", cypher)
        for clause in re.split(r" AND ", where.group(1)) if where else ():
            ok = any(_holds(alt.strip().strip("()"), props, sid, params)
                     for alt in clause.split(" OR "))
            if not ok:
                return
        sets = re.search(r"\bSET (.*?)(?: REMOVE |$)", cypher)
        for prop, param in re.findall(r"s\.(schema_rewrite_\w+) = \$(\w+)", sets.group(1) if sets else ""):
            props[prop][sid] = params[param]
        removes = re.search(r"\bREMOVE (.*)$", cypher)
        for prop in re.findall(r"s\.(schema_rewrite_\w+)", removes.group(1) if removes else ""):
            props[prop].pop(sid, None)

    def _mark_read(self, cypher, params) -> list:
        sid = params.get("sid")
        if sid not in SYSTEMS:
            return []
        if "MAPS_TO" not in cypher:                          # 7d0e6784's mark read
            return [{"started": self.marks.get(sid)}]
        edges = self.fields.get((sid, params["tbl"], params["fld"]))     # one snapshot
        self._snapshot_read = True
        return [{"started": self.marks.get(sid), "failed": self.failed.get(sid),
                 "fields": 0 if edges is None else 1,
                 "edges": [{"concept": c, "confidence": f} for c, _r, f in edges or ()]}]

    def record(self, sid):
        """The store's record of ``sid`` (``None`` when it holds nothing)."""
        record = {"started": self.marks.get(sid), "failed": self.failed.get(sid),
                  "begun": self.begun.get(sid)}
        return record if any(v is not None for v in record.values()) else None

    def put_record(self, sid, record) -> None:
        for key, values in (("started", self.marks), ("failed", self.failed),
                            ("begun", self.begun)):
            if record.get(key) is None:
                values.pop(sid, None)
            else:
                values[sid] = record[key]

    def store_read(self, sid):
        """A consistent read of the store; ends a snapshot's hook."""
        record = self.record(sid)
        if self._snapshot_read:
            self._snapshot_read = False
            hook, self.after_mark_read = self.after_mark_read, None
            if hook:
                hook()
        return record

    def _read(self, cypher, params) -> list:
        if cypher == onboard._RETIRED_LINK_COUNT_CYPHER:              # the sweep's readback
            edges = self.fields.get(self.field_ids.get(params["fid"]), ())
            return [{"n": sum(1 for e in edges if e[0] == params["concept"])}]
        if MARK in cypher:
            return self._mark_read(cypher, params)
        if "WHERE s.id <> $sid" in cypher and "MAPS_TO" in cypher:     # Resolve's peer read
            rows = []
            for (sid, table, field), edges in self.fields.items():
                for concept, role, _conf in edges if sid != params["sid"] else ():
                    if role in ("identifier", "correlate"):
                        rows.append({"sys_id": sid, "sys_schema": schema_of(sid), "table_name": table,
                                     "field_name": field, "concept": concept, "role": role,
                                     "concepts": [c for c, _r, _f in edges],
                                     "concept_scores": [{"concept": c, "confidence": f}
                                                        for c, _r, f in edges],
                                     "removal_incomplete": False})
            return rows
        if "MAPS_TO" in cypher and {"sid", "tbl", "fld"} <= set(params):   # 7d0e6784's field read
            edges = self.fields.get((params["sid"], params["tbl"], params["fld"]), ())
            return [{"concept": c, "confidence": f} for c, _r, f in edges]
        return []

    def _tick(self, cypher):
        if target_field_reads(cypher):
            self.state.now += self.read_cost

    def query(self, cypher, params=None):
        self._tick(cypher)
        if self.fail_reads and self.fail_reads(cypher):
            return []                                        # neptune_query degrades a failure
        return self._read(cypher, params or {})

    def query_checked(self, cypher, params=None):
        self._tick(cypher)
        if self.fail_reads and self.fail_reads(cypher):
            raise GraphQueryError("ServiceUnavailableException", "graph unavailable")
        return self._read(cypher, params or {})


class RewriteStore:
    """``JobStore``'s strict surface as ``schema_rewrite_state`` uses it
    (``get_consistent``, ``swap_strict``) over the graph fake's dicts."""

    def __init__(self, graph):
        self.graph = graph

    def get_consistent(self, sid):
        return self.graph.store_read(sid)

    def swap_strict(self, sid, fn, *, attempts=8):
        current = self.graph.record(sid)
        new = fn(dict(current) if current else None)
        if new is None:
            return current
        self.graph.store_log.append((sid, current, dict(new)))
        self.graph.put_record(sid, new)
        return new


def _holds(condition: str, props: dict, sid: str, params: dict) -> bool:
    """One ``s.<prop> IS NULL`` / ``s.<prop> = $param`` condition of a mark write."""
    m = re.fullmatch(r"s\.(schema_rewrite_\w+) IS NULL", condition)
    if m:
        return props[m.group(1)].get(sid) is None
    m = re.fullmatch(r"s\.(schema_rewrite_\w+) = \$(\w+)", condition)
    if m:
        value = props[m.group(1)].get(sid)
        return value is not None and value == params.get(m.group(2))
    raise AssertionError(f"unmodelled mark condition: {condition}")


def target_field_reads(cypher: str) -> bool:
    """The reads of the target field and of its system's mark."""
    return MARK in cypher or ("MAPS_TO" in cypher and "$fld" in cypher)


@pytest.fixture
def world(monkeypatch):
    resolve_evidence._reset_sample_memo()
    state = SimpleNamespace(now=1000.0, sleeps=[])
    graph = Graph(state)
    state.graph = graph
    entries = {sid: {"id": sid, "name": sid, "schema": schema_of(sid),
                     "connection_type": "PostgreSQL", **meta} for sid, meta in SYSTEMS.items()}
    monkeypatch.setattr(onboard, "neptune_write", graph.write)
    monkeypatch.setattr(onboard, "neptune_query", graph.query)
    monkeypatch.setattr(onboard, "neptune_query_checked", graph.query_checked)
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_STATES", graph.store, raising=False)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [SRC_CONCEPT, TGT_CONCEPT])
    monkeypatch.setattr(onboard, "catalog", _Catalog(entries))
    monkeypatch.setattr(onboard, "_classify_table_entity", lambda *_a, **_k: {"entity": None})
    # b0's other deletes (rule artifacts, derived records) read the graph
    # through their own checked seam; they are not what these files are about.
    monkeypatch.setattr(onboard, "_delete_stale_onboarding_rule_artifacts", lambda _sid: None)
    monkeypatch.setattr(onboard, "_delete_stale_pg_derived_records", lambda _sid: None)
    monkeypatch.setattr(onboard.self_learning, "accepted_retired_concept_links", lambda _sid: [])
    monkeypatch.setattr(onboard.self_learning, "accepted_user_concept_mappings", lambda _sid: {})
    monkeypatch.setattr(onboard.self_learning, "known_systems", lambda: list(entries.values()))
    monkeypatch.setattr(onboard.self_learning, "_authority_vouch_verdict",
                        lambda concept, _entries, known_owner_only=False: ("none", ""))

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds
        if getattr(state, "on_sleep", None):
            state.on_sleep(len(state.sleeps))
    # The re-measurement's wait and its clock (seams; 7d0e6784 has no clock).
    monkeypatch.setattr(onboard, "_schema_rewrite_sleep", sleep, raising=False)
    # The in-window sweep's retry pause (absent before 2026-10-21 round 8d).
    monkeypatch.setattr(onboard, "_settle_retry_sleep", state.sleeps.append, raising=False)
    monkeypatch.setattr(onboard, "_schema_rewrite_clock", lambda: state.now, raising=False)
    db = RuleCheckDb({
        schema_of(SRC_SYS): {"runs": (["row_id", "run_code"], _rows([*SHARED, *SRC_ONLY]))},
        schema_of(TGT_SYS): {"lot_lines": (["row_id", "lot_ref"], _rows([*SHARED, *TGT_ONLY]))},
    })
    monkeypatch.setattr(onboard, "pg", db)
    monkeypatch.setattr(agents_db, "pg", db)  # the Resolve sampler and probe read here
    # The target as its own earlier provision left it.
    graph.add_field(*TARGET_FIELD, (TGT_CONCEPT, "identifier", 0.95))
    yield state
    resolve_evidence._reset_sample_memo()


def resolve_rules() -> list:
    """Resolve's identifier-core affinity pass for the source system, as the
    seed's resolve request runs it, with no model rule beside it."""
    body = SimpleNamespace(system_id=SRC_SYS, mappings=[dict(m) for m in SRC_MAPPINGS])
    rules = onboard._augment_with_affinity_rules(body, {"rules": []})["rules"]
    assert [(r["method"], r["concept"], r["target_field"]) for r in rules] == [
        (EXACT_METHOD, TGT_CONCEPT, "lot_lines.lot_ref")]
    return rules


def provision_body(system_id, mappings, rules=()) -> ProvisionRequest:
    return ProvisionRequest(system_id=system_id, system_name=system_id, schema=schema_of(system_id),
                            plant=SYSTEMS[system_id]["plant"], rules=[dict(r) for r in rules],
                            mappings=[dict(m) for m in mappings], connection_type="PostgreSQL")


def provision_ctx(system_id, mappings, rules=()):
    ctx = onboard._make_provision_state(provision_body(system_id, mappings, rules))
    ctx.tables_seen = {m["table"] for m in mappings}
    return ctx


def provision_rules(rules):
    """The seed's provision request for the source system (the same mapping
    rows, Resolve's rules, no checkpoint flag): its identity-rule phase."""
    ctx = provision_ctx(SRC_SYS, SRC_MAPPINGS, rules)
    onboard._dp_identity_rules(ctx)
    onboard._stamp_identity_rule_provenance(SRC_SYS)
    return ctx


def the_node(state) -> dict:
    (node,) = state.graph.nodes.values()
    return node


def target_rewrite_starts():
    """The target's re-provision runs its real b0: the schema subgraph is
    deleted (with the mark, the System node is marked first)."""
    ctx = provision_ctx(TGT_SYS, TGT_MAPPINGS)
    onboard._dp_schema_entries(ctx)
    return ctx


def target_rewrite_ends(ctx):
    """The target's re-provision runs its real b2 (the field and its concept
    are written again), settles its field concepts (b3b) and clears its mark."""
    onboard._dp_field_mappings(ctx)
    onboard._dp_settle_field_concepts(ctx)
    onboard._dp_schema_rewritten(ctx)
