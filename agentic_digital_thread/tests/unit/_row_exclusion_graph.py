# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A fake graph for the row-exclusion tests: System nodes with their
row-exclusion manifest, SchemaEntries with the stored fact and its provenance,
and typed Fields. It answers every read and write ``row_exclusion`` and
``row_exclusion_manifest`` send, records each call by kind, and injects
faults by kind: ``fail`` raises, ``fail_next`` raises for the next N calls of
a kind only (``{kind: N}``), ``refuse`` answers a write with ``None`` (the
graph refused it), ``conflict_next`` raises ``db.GraphWriteConflict`` for the
next N writes of a kind (the graph aborted it on a concurrent modification,
so it did not run), ``hidden`` answers a SchemaEntry's fact read with no row.
Synthetic names only."""
from __future__ import annotations

import json


def _kinds() -> dict:
    from agents import row_exclusion as rx
    from agents import row_exclusion_manifest as manifest
    from agents import schema_fields
    out = {manifest.READ_MANIFEST: "manifest", manifest.SWAP_MANIFEST: "swap",
           schema_fields.SCHEMA_ENTRY_EXISTS: "exists", rx.FIELD_TYPES: "field_types",
           rx.READ_ROW_EXCLUSION: "fact", rx.SET_ROW_EXCLUSION: "set_fact",
           schema_fields.SYSTEM_SCHEMA_ENTRY_NAMES: "entry_names"}
    for name, kind in (("READ_ROW_EXCLUSIONS", "facts"), ("FIELD_TYPES_BATCH", "fields"),
                       ("READ_ROW_EXCLUSION_RECORDS", "records")):
        if hasattr(rx, name):
            out[getattr(rx, name)] = kind
    return out


class RxGraph:
    """One or more systems, each with ``{table: {column: data_type}}``."""

    def __init__(self, systems: dict):
        self.manifests: dict = {sid: None for sid in systems}
        self.entries: dict = {}
        for sid, tables in systems.items():
            for table, columns in tables.items():
                self.entries[f"{sid}.{table}"] = {"sid": sid, "name": table,
                                                  "fields": dict(columns), "props": {}}
        self.calls: list = []
        self.fail: set = set()
        self.fail_next: dict = {}
        self.refuse: set = set()
        self.conflict_next: dict = {}
        self.hidden: set = set()
        self.before_write = None

    # ------------------------------------------------------------------ setup
    def list_table(self, sid: str, table: str, *, token: str = "t0", pending: bool = False):
        stored = json.loads(self.manifests[sid]) if self.manifests.get(sid) else {"tables": {}}
        stored["tables"][table] = {"token": token, "pending": pending}
        self.manifests[sid] = json.dumps(stored)

    def store(self, sid: str, table: str, fact: dict | None) -> None:
        self.entries[f"{sid}.{table}"]["props"] = (
            {"row_exclusion": json.dumps(fact), "row_exclusion_by": "steward",
             "row_exclusion_at": "2026-10-20T00:00:00Z", "row_exclusion_source": "owner"}
            if fact is not None else {})

    def manifest(self, sid: str) -> dict:
        raw = self.manifests.get(sid)
        return json.loads(raw)["tables"] if raw else {}

    def rewrite_marker(self, sid: str):
        """The re-provision marker the System's manifest holds, else ``None``."""
        raw = self.manifests.get(sid)
        return json.loads(raw).get("rewrite") if raw else None

    def delete_schema(self, sid: str) -> None:
        """A re-provision's schema delete: every SchemaEntry of ``sid`` (its
        stored fact with it) and its Fields; the System node and its manifest
        stay."""
        self.entries = {k: e for k, e in self.entries.items() if e["sid"] != sid}

    def recreate(self, sid: str, tables: dict) -> None:
        """A re-provision's schema write: fresh SchemaEntries with these
        ``{table: {column: data_type}}`` Fields and nothing stored."""
        for table, columns in tables.items():
            self.entries[f"{sid}.{table}"] = {"sid": sid, "name": table,
                                              "fields": dict(columns), "props": {}}

    def stored(self, sid: str, table: str):
        value = self.entries[f"{sid}.{table}"]["props"].get("row_exclusion")
        return json.loads(value) if value else None

    def kinds(self) -> list:
        return [kind for kind, _params in self.calls]

    def seam(self):
        """For ``monkeypatch.setattr(row_exclusion, "_graph_query", g.seam())``."""
        return lambda: self.query

    # --------------------------------------------------------------- dispatch
    def _kind(self, cypher: str) -> str:
        kind = _kinds().get(cypher)
        if kind is None:
            raise AssertionError(f"unexpected graph statement: {cypher[:120]}")
        return kind

    def _faulted(self, kind: str) -> bool:
        if kind in self.fail:
            return True
        if self.fail_next.get(kind, 0) > 0:
            self.fail_next[kind] -= 1
            return True
        return False

    def query(self, cypher, params=None):
        params = dict(params or {})
        kind = self._kind(cypher)
        self.calls.append((kind, params))
        if self._faulted(kind):
            raise RuntimeError(f"graph down ({kind})")
        return getattr(self, f"_read_{kind}")(params)

    def write(self, cypher, params=None):
        params = dict(params or {})
        kind = self._kind(cypher)
        self.calls.append((kind, params))
        if self._faulted(kind):
            raise RuntimeError(f"graph down ({kind})")
        if self.before_write is not None:
            self.before_write(kind, params)
        if self.conflict_next.get(kind, 0) > 0:
            from agents import db
            self.conflict_next[kind] -= 1
            conflict = getattr(db, "GraphWriteConflict", None)
            if conflict is None:     # a seam with no conflict type answers refused
                return None
            raise conflict(f"concurrent modification ({kind})")
        if kind in self.refuse:
            return None
        return getattr(self, f"_write_{kind}")(params)

    # ------------------------------------------------------------------ reads
    def _read_manifest(self, p):
        if p["sid"] not in self.manifests:
            return []
        return [{"row_exclusion_manifest": self.manifests[p["sid"]]}]

    def _read_entry_names(self, p):
        """SYSTEM_SCHEMA_ENTRY_NAMES: every SchemaEntry name the system owns, so
        the steward route can resolve a submitted name to a real entry."""
        return [{"name": e["name"]} for e in self.entries.values() if e["sid"] == p["sid"]]

    def _entry(self, sid, seid):
        entry = self.entries.get(seid)
        return entry if entry is not None and entry["sid"] == sid else None

    def _read_exists(self, p):
        return [{"n": 1 if self._entry(p["sid"], p["seid"]) else 0}]

    def _read_field_types(self, p):
        entry = self._entry(p["sid"], p["seid"])
        return [{"name": c, "data_type": t} for c, t in (entry or {}).get("fields", {}).items()]

    def _fact_row(self, seid, entry) -> dict:
        props = entry["props"]
        return {"seid": seid, "row_exclusion": props.get("row_exclusion"),
                "row_exclusion_by": props.get("row_exclusion_by"),
                "row_exclusion_at": props.get("row_exclusion_at"),
                "row_exclusion_source": props.get("row_exclusion_source")}

    def _read_fact(self, p):
        entry = self._entry(p["sid"], p["seid"])
        if entry is None or p["seid"] in self.hidden:
            return []
        row = self._fact_row(p["seid"], entry)
        row.pop("seid")
        return [row]

    def _read_facts(self, p):
        out = []
        for seid in p["seids"]:
            entry = self._entry(p["sid"], seid)
            if entry is not None and seid not in self.hidden:
                props = entry["props"]
                out.append({"seid": seid, "row_exclusion": props.get("row_exclusion"),
                            "set_at": props.get("row_exclusion_at"),
                            "set_source": props.get("row_exclusion_source")})
        return out

    def _read_records(self, p):
        out = []
        for seid in p["seids"]:
            entry = self._entry(p["sid"], seid)
            if entry is not None and seid not in self.hidden:
                out.append(self._fact_row(seid, entry))
        return out

    def _read_fields(self, p):
        out = []
        for seid in p["seids"]:
            entry = self._entry(p["sid"], seid)
            for column, data_type in (entry or {}).get("fields", {}).items():
                out.append({"seid": seid, "name": column, "data_type": data_type})
        return out

    # ----------------------------------------------------------------- writes
    def _write_swap(self, p):
        if p["sid"] not in self.manifests or (self.manifests[p["sid"]] or "") != p["expected"]:
            return []
        self.manifests[p["sid"]] = p["manifest"]
        return [{"id": p["sid"]}]

    def _write_set_fact(self, p):
        entry = self._entry(p["sid"], p["seid"])
        if entry is None:
            return []
        # The write is conditional on the System's manifest still being the one
        # the writer read (``row_exclusion.SET_ROW_EXCLUSION``).
        if "expected" in p and (self.manifests.get(p["sid"]) or "") != p["expected"]:
            return []
        if p["row_exclusion"]:
            entry["props"] = {"row_exclusion": p["row_exclusion"], "row_exclusion_by": p["by"],
                              "row_exclusion_at": p["at"], "row_exclusion_source": p["source"]}
        else:
            entry["props"] = {}
        return [{"seid": p["seid"]}]
