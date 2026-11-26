# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit test for the pure ``mapToolToSystemId`` helper.

The helper lives in ``frontend/src/lib/architecture-util.js`` and drives the
System-Map / Investigation-graph "marching ants" flow animation: it maps a raw
tool name from a tool_start/tool_result event to the system id whose spoke edge
and node should light up. It is DOM-free and node-requirable, so we exercise the
real function via a small node subprocess and assert the parsing contract:

  * gateway "<system_id>___<tool>" → "<system_id>"
  * "explore_graph"                → null (the knowledge-graph hub, not a leaf)
  * legacy "query_<id>"            → dash-cased "<id>"
  * anything else / empty          → null

Skips cleanly when node is unavailable so the offline lane stays green.
Fully offline: no AWS / DB / network.
"""
from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_UTIL_JS = _ROOT / "frontend" / "src" / "lib" / "architecture-util.js"

_NODE = shutil.which("node")
_needs_node = pytest.mark.skipif(_NODE is None, reason="node not available")


def _run(tool_name):
    """Invoke mapToolToSystemId in node and return the parsed result (str/None)."""
    # The frontend package.json "type":"module" makes the IIFE load as ESM,
    # where the CJS module.exports guard is skipped but the globalThis
    # publication still runs.
    script = (
        f"const m=require({json.dumps(str(_UTIL_JS))});"
        "const f=m.mapToolToSystemId||globalThis.mapToolToSystemId;"
        f"const a={json.dumps(tool_name)};"
        "process.stdout.write(JSON.stringify(f(a)));"
    )
    out = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


# --- Module surface ----------------------------------------------------------

def test_util_file_exists() -> None:
    assert _UTIL_JS.is_file()


def test_helper_is_exported_and_node_requirable() -> None:
    src = _UTIL_JS.read_text(encoding="utf-8")
    assert "mapToolToSystemId" in src
    assert "module.exports" in src


# --- Parsing contract (via node) ---------------------------------------------

@_needs_node
def test_gateway_format_returns_system_id_prefix() -> None:
    # The canonical gateway tool shape: "<system_id>___<tool>".
    assert _run("a___b") == "a"
    assert _run("lake-athena___athena_run_query") == "lake-athena"
    assert _run("telem-sitewise___sitewise_get_asset_property_value") == "telem-sitewise"


@_needs_node
def test_gateway_format_with_legacy_query_tool_suffix() -> None:
    # A gateway name whose tool half happens to be a legacy "query_*" verb — the
    # split on "___" still wins and yields the system id prefix.
    assert _run("mes-mil___query_mes_mil") == "mes-mil"


@_needs_node
def test_explore_graph_is_the_hub_not_a_leaf() -> None:
    assert _run("explore_graph") is None


@_needs_node
def test_legacy_query_prefix_is_dash_cased() -> None:
    assert _run("query_mes_mil") == "mes-mil"
    assert _run("query_sap_erp_indy") == "sap-erp-indy"


@_needs_node
def test_non_system_tools_and_empty_return_null() -> None:
    assert _run("get_system_schema") is None
    assert _run("") is None
    assert _run(None) is None


# --- Investigation-graph NL helpers: describeToolInput / describeToolResult ---
# These pure helpers (appended alongside mapToolToSystemId) turn a tool call's
# request/result into short plain-English descriptions for the reasoning-trace
# graph — no raw Cypher/SQL, no LLM. Exercised via the same node subprocess.


def _run_fn(fn, *args):
    """Invoke an exported helper in node and return the parsed JSON result."""
    argsjs = ",".join(json.dumps(a) for a in args)
    script = (
        f"const m=require({json.dumps(str(_UTIL_JS))});"
        f"const f=m.{fn}||globalThis.{fn};"
        f"process.stdout.write(JSON.stringify(f({argsjs})));"
    )
    out = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_new_helpers_are_exported() -> None:
    src = _UTIL_JS.read_text(encoding="utf-8")
    assert "describeToolInput" in src
    assert "describeToolResult" in src
    # Additive export augmentation must not clobber the mapToolToSystemId export.
    assert "module.exports.describeToolInput" in src
    assert "module.exports = { mapToolToSystemId: mapToolToSystemId }" in src
    assert "module.exports.plainSummaryText" in src
    assert "module.exports.readsPastTable" in src


# describeToolInput -----------------------------------------------------------

@_needs_node
def test_describe_input_mode_a_question_passthrough() -> None:
    q = "How many work orders failed final inspection yesterday?"
    assert _run_fn("describeToolInput", "mes-mil___query_mes_mil", {"question": q}) == q


@_needs_node
def test_describe_input_long_question_is_truncated() -> None:
    q = "word " * 60  # ~300 chars
    out = _run_fn("describeToolInput", "erp___query_erp", {"question": q})
    assert out.endswith("…")
    assert len(out) <= 122


@_needs_node
def test_describe_input_cypher_is_englishized_not_raw() -> None:
    cy = "MATCH (s:System)-[:MAPS_TO_CONCEPT]->(c:Concept) WHERE c.name='availability' RETURN s LIMIT 20"
    out = _run_fn("describeToolInput", "digital-thread___explore_graph", {"cypher": cy})
    assert "MATCH" not in out  # not raw Cypher
    assert "System" in out and "Concept" in out
    assert "MAPS_TO_CONCEPT" not in out  # relationship type excluded
    assert "availability" in out


@_needs_node
def test_describe_input_save_value_mapping_phrasing() -> None:
    out = _run_fn(
        "describeToolInput",
        "digital-thread___save_value_mapping",
        {"from_system": "erp", "to_system": "mes-mil", "concept": "order_status"},
    )
    assert out == "Saving learned mapping: erp → mes-mil (order_status)"


@_needs_node
def test_describe_input_save_correlation_phrasing() -> None:
    out = _run_fn(
        "describeToolInput",
        "digital-thread___save_correlation",
        {"pattern": "x", "systems": ["erp", "mes-mil"]},
    )
    assert out == "Saving learned correlation: erp → mes-mil — “x”"


@_needs_node
def test_describe_input_empty_returns_empty_string() -> None:
    assert _run_fn("describeToolInput", "erp___query_erp", {}) == ""


@_needs_node
def test_describe_input_english_return_on_system_query_is_not_cypher() -> None:
    # Regression: a natural-language system-agent request containing "return"
    # must NOT be mislabeled as knowledge-graph exploration (query_* tools are
    # English by contract).
    q = "List failed quality checks this week and return the count per site"
    out = _run_fn("describeToolInput", "lake-athena___query_lake_athena", {"question": q})
    assert out == q
    assert "knowledge graph" not in out


@_needs_node
def test_describe_input_match_pattern_still_englishizes() -> None:
    # A real Cypher MATCH( pattern still triggers englishization even without
    # the explore_graph half or an explicit cypher key.
    out = _run_fn("describeToolInput", "graph_query", "MATCH (s:System) RETURN s")
    assert "MATCH" not in out
    assert "System" in out
    assert "knowledge graph" in out


@_needs_node
def test_describe_input_explore_graph_half_still_englishizes() -> None:
    # The explore_graph half alone classifies as Cypher, even when the text
    # has no MATCH( pattern.
    out = _run_fn(
        "describeToolInput", "digital-thread___explore_graph", {"question": "RETURN 1"}
    )
    assert out == "Explored the knowledge graph"


# describeToolResult ----------------------------------------------------------

# A markdown table in a reply is the reply's own layout: its rows are not a
# count of what the call returned, and the request's words do not say what a
# row is. No count is read from it; the line reads past the table (the vitest
# twin is architecture-util.test.ts "describeToolResult").

@_needs_node
def test_describe_result_reply_table_gives_no_count_and_no_request_noun() -> None:
    md = "Three work orders matched.\n\n| id | status |\n|---|---|\n| 1 | ok |\n| 2 | bad |\n| 3 | ok |"
    out = _run_fn("describeToolResult", "mes-mil___query_mes_mil", md, "how many work orders")
    assert out == "Three work orders matched."


@_needs_node
def test_describe_result_two_tables_are_not_added_into_one_count() -> None:
    # Reproduction (walk 2026-10-28, ERP Request 2): a 3-row table of batches
    # and a 1-row total table read "Returned ~5 shipments".
    md = "\n".join([
        "The 47 shipment line items came from 3 distinct production batches.",
        "",
        "| batch | line items |", "|---|---|", "| RUN-A | 1 |", "| RUN-B | 1 |", "| RUN-C | 45 |",
        "",
        "| total line items |", "|---|", "| 47 |",
    ])
    out = _run_fn("describeToolResult", "erp___query_erp", md,
                  "List the shipments of part 7742 in the last 7 days")
    assert out == "The 47 shipment line items came from 3 distinct production batches."


@_needs_node
def test_describe_result_table_only_reply_has_no_line() -> None:
    md = "| id |\n|---|\n| 1 |\n| 2 |"
    assert _run_fn("describeToolResult", "erp___query_erp", md, "list the orders") == ""


@_needs_node
def test_describe_result_records_found_is_a_typed_count() -> None:
    out = _run_fn("describeToolResult", "lake___athena_run_query", "12 records found: A, B, C, D...", "")
    assert out == "Returned 12 records"


@_needs_node
def test_describe_result_no_matching_data() -> None:
    assert _run_fn("describeToolResult", "erp___query_erp", "No matching records found.", "") == "No matching data"


@_needs_node
def test_reads_past_table_is_true_only_where_the_line_reads_past_a_reply_table() -> None:
    table = "| id |\n|---|\n| 1 |\n| 2 |"
    assert _run_fn("readsPastTable", "erp___query_erp", table) is True
    assert _run_fn("readsPastTable", "erp___query_erp", f"Two orders:\n\n{table}") is True
    assert _run_fn("readsPastTable", "erp___query_erp", "No matching records found.") is False
    assert _run_fn("readsPastTable", "erp___query_erp", "The average was 3.2 mm/s.") is False
    assert _run_fn("readsPastTable", "erp___query_erp", "| a |\n|---|") is False
    assert _run_fn("readsPastTable", "erp___query_erp", f"12 records found\n{table}") is False
    # A graph result has no count line either, so its line reads past a table;
    # a failed graph query keeps its own line.
    assert _run_fn("readsPastTable", "digital-thread___explore_graph", table) is True
    assert _run_fn("readsPastTable", "digital-thread___explore_graph",
                   f"Error: Graph query failed\n{table}") is False
    assert _run_fn("readsPastTable", "digital-thread___save_correlation", table) is False
    assert _run_fn("readsPastTable", "erp___query_erp", "") is False


# Nothing typed on a knowledge-graph result says how many entities came back:
# the summary shows at most 8 lines, a row can be a scalar, and the routing
# step is prose. So its line is the first line as written, with no count (the
# vitest twin is architecture-util.test.ts "explore_graph gives no count ...").

@_needs_node
def test_describe_result_explore_graph_gives_no_count_from_summary_lines() -> None:
    summary = "System A — foo\nSystem B — bar\nConcept C — baz"
    out = _run_fn("describeToolResult", "digital-thread___explore_graph", summary, "")
    assert out == "System A — foo"


@_needs_node
def test_describe_result_graph_routing_step_reads_its_first_sentence() -> None:
    seed = ("Looked up this question in the knowledge graph before asking any system.\n"
            "- Connected systems: System A, System B\n"
            "- Shared concepts linking them: lot, order")
    out = _run_fn("describeToolResult", "explore_graph", seed, "")
    assert out == "Looked up this question in the knowledge graph before asking any system."


@_needs_node
def test_describe_result_cut_graph_list_does_not_read_as_its_shown_lines() -> None:
    summary = "\n".join(f"Concept: c{i} (domain)" for i in range(1, 9)) + " (+12 more)"
    out = _run_fn("describeToolResult", "digital-thread___explore_graph", summary, "")
    assert out == "Concept: c1 (domain)"


@_needs_node
def test_describe_result_scalar_graph_row_is_not_an_entity() -> None:
    assert _run_fn("describeToolResult", "explore_graph", "42", "") == "42"
    assert _run_fn("describeToolResult", "explore_graph",
                   "No matching entities found.", "") == "No matching data"


@_needs_node
def test_describe_result_save_without_payload_is_completed_not_logged() -> None:
    out = _run_fn("describeToolResult", "digital-thread___save_value_mapping",
                  "Result with 3 fields: status, mapping_id, type", "")
    assert out == "Mapping save completed"


@_needs_node
def test_describe_result_empty_summary() -> None:
    assert _run_fn("describeToolResult", "erp___query_erp", "", "") == ""


# The prose line is the first line of real content, as plain text: markdown
# structure never becomes the line, and inline syntax is removed (the vitest
# twin is architecture-util.test.ts "prose line never shows markdown syntax").

_PROSE_TOOL = "plant-ops___query_plant_ops"


def _prose(summary: str) -> str:
    return _run_fn("describeToolResult", _PROSE_TOOL, summary, "")


@_needs_node
def test_describe_result_prose_skips_a_heading_only_line() -> None:
    summary = ("## Result\n\nThe average cycle time for the line was 41.2 seconds "
               "across the shift.\n\nMore detail.")
    assert _prose(summary) == "The average cycle time for the line was 41.2 seconds across the shift."


@_needs_node
def test_describe_result_prose_skips_a_bold_only_line() -> None:
    line = "Three orders shipped after their promised date this week."
    assert _prose(f"**Result:**\n\n{line}") == line
    assert _prose(f"**Summary**\n\n{line}") == line


@_needs_node
def test_describe_result_prose_skips_a_fence_and_its_lines() -> None:
    line = "The query returned one order object for the requested window."
    assert _prose(f'```json\n{{"orders": 1}}\n```\n{line}') == line


@_needs_node
def test_describe_result_prose_skips_uncounted_table_rows_and_rules() -> None:
    line = "The header was listed without any rows under it in this reply."
    assert _prose(f"| a | b |\n|---|---|\n\n---\n\n{line}") == line


@_needs_node
def test_describe_result_prose_removes_inline_markdown() -> None:
    summary = ("- **Total:** 42 *open* orders and `3` on hold, see "
               "[the report](https://example.invalid/r) for the rest")
    assert _prose(summary) == "Total: 42 open orders and 3 on hold, see the report for the rest"
    assert _prose("> _Note_: the press reported its last cycle at shift change and nothing since then") == (
        "Note: the press reported its last cycle at shift change and nothing since then")


@_needs_node
def test_describe_result_prose_keeps_underscores_inside_names() -> None:
    line = "Column run_number was read from table prod_runs; tool ops___query_ops ran twice today."
    assert _prose(line) == line


@_needs_node
def test_describe_result_prose_keeps_a_bold_result_line() -> None:
    assert _prose("**42 orders shipped late.**") == "42 orders shipped late."
    assert _prose("**Average cycle time: 41.2 seconds**\nMore detail follows.") == (
        "Average cycle time: 41.2 seconds")


@_needs_node
def test_describe_result_prose_keeps_a_one_word_bold_answer() -> None:
    assert _prose("**Yes**") == "Yes"
    assert _prose("**Failed**\nThe last step did not finish.") == "Failed"
    line = "Three orders shipped after their promised date this week."
    assert _prose(f"**Results**\n\n{line}") == line


@_needs_node
def test_describe_result_prose_keeps_tildes_inside_a_code_span() -> None:
    assert _prose("Current token: `~~NOT_A_VALUE~~` is what the reader holds") == (
        "Current token: ~~NOT_A_VALUE~~ is what the reader holds")
    assert _prose("**`3` orders are on hold** until the next shift starts") == (
        "3 orders are on hold until the next shift starts")
    assert _prose("Token  `A  B`   matched the key the reader sent") == (
        "Token A  B matched the key the reader sent")


@_needs_node
def test_describe_result_prose_drops_struck_through_text() -> None:
    line = "17 open orders remain on the board today"
    assert _prose(f"~~42 open orders~~ {line}") == line
    assert _prose(f"~~42 open orders~~\n{line}") == line


@_needs_node
def test_describe_result_prose_is_empty_for_markdown_structure_only() -> None:
    assert _prose("## Result") == ""
    assert _prose("## Result\n\n| a |\n|---|") == ""


@_needs_node
def test_describe_result_prose_reads_any_script_as_content() -> None:
    # Review: a line was content only with an ASCII letter or digit, so a
    # reply in Japanese or Arabic had no line at all.
    assert _prose("検査結果は正常です。") == "検査結果は正常です。"
    assert _prose("نتيجة الفحص طبيعية.") == "نتيجة الفحص طبيعية."
    assert _prose("- **第一項**\n- 第二項") == "第一項"


# plainSummaryText: a summary's content as plain text, one line per content
# line (the vitest twin is architecture-util.test.ts "plainSummaryText"). The
# graph node tooltip and a label that joins lines into one read it. It keeps
# what the Result line skips: a table's cells, by the GFM table grammar the
# drill-down renders with, and fenced code as written.

def _plain(text) -> str:
    return _run_fn("plainSummaryText", text)


@_needs_node
def test_plain_summary_text_removes_markdown_and_keeps_table_cells_and_code() -> None:
    # Reproduction (walk 2026-10-28): the data node tooltip showed this reply's
    # raw markdown.
    reply = "\n".join([
        "Based on the query results, there are **no vibration sensor readings available "
        "for the asset** in that window.",
        "",
        "The system indicates that:",
        "- **Requested window:** 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z",
        "- *Data available from:* 2026-10-24T17:10:10Z",
        "",
        "## Telemetry read",
        "",
        "| property | points |", "|---|---|", "| Vibration_X | 0 |",
        "```", "raw", "```",
    ])
    assert _plain(reply) == "\n".join([
        "Based on the query results, there are no vibration sensor readings available "
        "for the asset in that window.",
        "The system indicates that:",
        "Requested window: 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z",
        "Data available from: 2026-10-24T17:10:10Z",
        "property · points",
        "Vibration_X · 0",
        "raw",
    ])


@_needs_node
def test_plain_summary_text_drops_every_list_marker() -> None:
    # Reproduction (walk 2026-10-28): the Knowledge Graph label read
    # "... before asking any system. - Conne…".
    seed = ("Looked up this question in the knowledge graph before asking any system.\n"
            "- Connected systems: Kiln Log, Order Book\n"
            "1. Shared concepts linking them: lot")
    assert _plain(seed).replace("\n", " ") == (
        "Looked up this question in the knowledge graph before asking any system. "
        "Connected systems: Kiln Log, Order Book Shared concepts linking them: lot")


@_needs_node
def test_plain_summary_text_keeps_plain_text_and_is_empty_for_structure_only() -> None:
    assert _plain("Concept — identity") == "Concept — identity"
    assert _plain("KG: which systems hold lot K-4471?\nKG: how do they join?") == (
        "KG: which systems hold lot K-4471?\nKG: how do they join?")
    assert _plain("## Result\n\n---\n\n**Summary**") == ""
    assert _plain("") == ""
    assert _plain(None) == ""


@_needs_node
def test_plain_summary_text_reads_any_script_as_content() -> None:
    # Review: these gave "", so the tooltip and the Knowledge Graph label
    # lost them.
    assert _plain("検査結果は正常です。") == "検査結果は正常です。"
    assert _plain("نتيجة الفحص طبيعية.") == "نتيجة الفحص طبيعية."
    assert _plain("- 第一項\n- 第二項") == "第一項\n第二項"


@_needs_node
def test_plain_summary_text_keeps_a_table_only_result_as_its_cells() -> None:
    # Review: every table row was dropped, so a table-only result had no text.
    table = "\n".join([
        "| batch | line items |",
        "|:---|---:|",
        "| B-1 | **1** |",
        "| a \\| b | 45 | extra cell |",
        "| --- | --- |",
        "| B-3 |",
    ])
    assert _plain(table) == "\n".join(["batch · line items", "B-1 · 1", "a | b · 45", "B-3"])
    assert _plain("The result:\n| a | b |\n|---|---|\n| 1 | 2 |\n\nEnd of list.") == (
        "The result:\na · b\n1 · 2\nEnd of list.")


@_needs_node
def test_plain_summary_text_keeps_a_code_only_result_as_written() -> None:
    # Review: every fenced line was dropped, so a code-only result had no text.
    code = "\n".join([
        "```sql",
        "SELECT run_id, COUNT(*) FROM runs -- **as written**",
        "| a | b |",
        "|---|---|",
        "~~~",
        "```",
    ])
    assert _plain(code) == "\n".join([
        "SELECT run_id, COUNT(*) FROM runs -- **as written**", "| a | b |", "|---|---|", "~~~"])
    assert _plain("~~~\nline one\n```\n~~~\nAfter the block.") == "line one\n```\nAfter the block."


@_needs_node
def test_plain_summary_text_reads_pipes_in_prose_as_prose() -> None:
    # Review: any line with two pipes was read as a table row and dropped.
    assert _plain("Filter: status = 'open' || status = 'held'\nThe filter matched 3 orders.") == (
        "Filter: status = 'open' || status = 'held'\nThe filter matched 3 orders.")
    assert _plain("a | b | c\nd | e | f") == "a | b | c\nd | e | f"
    # A head row whose cell count is not the delimiter row's is no table.
    assert _plain("| a | b |\n|---|\n| 1 |") == "| a | b |\n| 1 |"


@_needs_node
def test_plain_summary_text_recognizes_a_table_without_outer_pipes() -> None:
    # Review: a valid table without outer pipes kept its raw "|" separators.
    assert _plain("property | points\n--- | ---:\nVibration_X | 0") == (
        "property · points\nVibration_X · 0")
