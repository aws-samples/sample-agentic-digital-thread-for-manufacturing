# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline guard for the virtual-table-name leak fix (BUG #3).

A single source table with >100 columns is split into synthetic virtual chunk
tables (named ``{real}_{prefix}_{n}``) ONLY to bound the map-prompt size. Those
synthetic names must never leak into the emitted mappings — otherwise downstream
value-sampling in ``resolve_evidence.sample_field_values`` queries a table that
does not exist. These tests assert emitted mappings carry the REAL source-table
name, using SYNTHETIC data only (no dataset-specific literals).

Two layers, both hermetic (no AWS, no graph, no DB, no LLM):
  * the pure ``_remap_virtual_tables`` helper rewrites table + field prefixes;
  * a full ``_run_map_job`` run over a synthetic >100-column table with a mocked
    ``bedrock_call`` that ECHOES the virtual table names it is shown proves the
    real name survives to the finished mappings.
"""
from __future__ import annotations

import json
import re

import agents.onboard as onboard


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper: _remap_virtual_tables
# ─────────────────────────────────────────────────────────────────────────────
def test_remap_rewrites_table_and_field_prefix():
    v2r = {"orders_raw_orders_raw_1": "orders_raw"}
    mappings = [
        {"field": "orders_raw_orders_raw_1.widget_code", "table": "orders_raw_orders_raw_1",
         "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.9}]},
    ]
    onboard._remap_virtual_tables(mappings, v2r)
    assert mappings[0]["table"] == "orders_raw"
    assert mappings[0]["field"] == "orders_raw.widget_code"


def test_remap_preserves_dotted_column_path():
    """The real column path may itself contain dots — only the leading table
    segment is swapped."""
    v2r = {"tags_ns_1": "tags"}
    mappings = [{"field": "tags_ns_1.a.b.c", "table": "tags_ns_1", "mappings": []}]
    onboard._remap_virtual_tables(mappings, v2r)
    assert mappings[0]["table"] == "tags"
    assert mappings[0]["field"] == "tags.a.b.c"


def test_remap_noop_when_map_empty():
    """Non-chunked path: an empty map leaves mappings byte-identical."""
    mappings = [{"field": "t.c", "table": "t", "mappings": []}]
    before = json.loads(json.dumps(mappings))
    onboard._remap_virtual_tables(mappings, {})
    assert mappings == before


def test_remap_leaves_unknown_tables_untouched():
    v2r = {"known_1": "known"}
    mappings = [{"field": "other.c", "table": "other", "mappings": []}]
    onboard._remap_virtual_tables(mappings, v2r)
    assert mappings[0]["table"] == "other"
    assert mappings[0]["field"] == "other.c"


def test_remap_tolerates_non_dict_entries():
    onboard._remap_virtual_tables([None, "junk", 3], {"a_1": "a"})  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: _run_map_job over a synthetic >100-column table
# ─────────────────────────────────────────────────────────────────────────────
def _fake_bedrock_echoing_virtual_names(prompt: str, timeout: int = 90, model=None) -> str:
    """Stand-in for db.bedrock_call: parse the virtual table + first column out of
    the prompt and echo them back as a mapping, mimicking the real mapper (which
    copies the table name it is shown into each mapping)."""
    # The prompt lists tables as "  <table>:" then "    - <column> ..." lines.
    tbl_match = re.search(r"^\s{2}(\S+):\s*$", prompt, re.MULTILINE)
    col_match = re.search(r"^\s{4}- (\S+)", prompt, re.MULTILINE)
    if not tbl_match or not col_match:
        return "[]"
    tbl = tbl_match.group(1)
    col = col_match.group(1)
    return json.dumps([{
        "field": f"{tbl}.{col}",
        "table": tbl,
        "mappings": [{"concept": "equipment-id", "role": "identifier", "confidence": 0.9}],
    }])


def test_run_map_job_emits_real_table_name_for_big_table(monkeypatch):
    real_table = "device_registry"
    # >100 columns forces the virtual-table split.
    columns = [{"name": f"col_{i}", "data_type": "varchar"} for i in range(150)]
    tables = [{"name": real_table, "relation_kind": "base_table",
               "schema_revision": "a" * 32, "columns": columns, "row_count": 150,
               "sample_values": {}}]

    monkeypatch.setattr(onboard, "bedrock_call", _fake_bedrock_echoing_virtual_names)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: ["equipment-id", "site"])
    # SQL system (not MCP) so the concept filter/route stays on the SQL path.
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: {"connection_type": "PostgreSQL"})

    job_id = "vtjob"
    onboard._map_jobs[job_id] = {"status": "running"}
    onboard._run_map_job(job_id, "some-system", tables)

    job = onboard._map_jobs.pop(job_id)
    assert job["status"] == "done", job
    result_mappings = job["result"]["mappings"]
    assert result_mappings, "expected at least one emitted mapping"
    # Every emitted mapping must carry the REAL table name — no synthetic chunk
    # names (which contain the real name doubled, e.g. device_registry_..._1).
    for m in result_mappings:
        assert m["table"] == real_table, f"synthetic name leaked: {m['table']}"
        assert m["field"].startswith(real_table + "."), m["field"]
        # The doubled-name signature of the leak must be absent.
        assert f"{real_table}_{real_table}" not in m["table"]
        assert f"{real_table}_{real_table}" not in m["field"]
    # FULL-COVERAGE GUARANTEE (C3/M4): the echoing mock only ever returns ONE
    # mapping per prompt, so most columns come back via reconciliation — every
    # one of the 150 input columns must appear EXACTLY once in the result
    # (worst case as a synthesized `reconciled: True` needs_review row). This
    # test previously passed with 1/150 coverage; it now pins the guarantee.
    emitted_fields = [m["field"] for m in result_mappings]
    expected_fields = [f"{real_table}.col_{i}" for i in range(150)]
    assert sorted(emitted_fields) == sorted(expected_fields), (
        f"coverage broken: {len(emitted_fields)} rows for 150 input columns"
    )
    # Synthesized rows survive provenance attach (real (table, column) re-join)
    # and ride the SQL needs_review path.
    synthesized = [m for m in result_mappings if m.get("reconciled")]
    assert synthesized, "expected reconciliation-synthesized rows for the echo mock"
    for m in synthesized:
        assert m["concept"] == "needs_review"
        assert m["confidence"] == 0
        assert m["data_type"] == "varchar"  # provenance attached ⇒ not dropped
