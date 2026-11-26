# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the shared tool-result summarizer.

Focus (the two defects the shared module fixes):

* **Structured error detection** — a valid result set whose DATA contains the
  word 'error'/'Error' (failure-code column values, ``error_code`` fields,
  ``"error": null`` status envelopes) is summarized as records, NOT flagged as a
  "Query error". Only a STRUCTURAL error envelope (truthy top-level
  ``error``/``__error__``, or a failed tool-result flag) is flagged.
* **Robust fallback** — the ``sitewise_list_associated_assets`` list shape is
  summarized like the other list shapes, and a genuinely unexpected shape yields a
  best-effort key/row-count summary — never the opaque "Error processing results.".

All payloads are SYNTHETIC (no dataset literals) and the summarizer is pure/offline.
"""
from __future__ import annotations

import json

from agents.tool_result_summary import summarize_tool_result


def _wrap(payload) -> dict:
    """Wrap a JSON-serializable payload in the tool-result envelope the summarizer
    consumes (``{"content": [{"text": "<json>"}]}``)."""
    return {"content": [{"text": json.dumps(payload)}]}


# --------------------------------------------------------------------------- #
# (a) Structured error detection — data containing 'error' is NOT a failure
# --------------------------------------------------------------------------- #
def test_athena_rows_with_error_word_in_data_not_flagged():
    """An Athena result set whose ROW VALUES contain the word 'error' is summarized
    as N records — NOT mislabeled 'Query error' (the substring-scan false positive)."""
    payload = {
        "columns": ["order_id", "status"],
        "rows": [
            ["A-1", "error"],
            ["A-2", "completed"],
            ["A-3", "Error while processing"],
        ],
        "row_count": 3,
    }
    summary = summarize_tool_result("anycompany___athena_run_query", _wrap(payload))
    assert "record" in summary
    assert summary.startswith("3 record")
    assert "Query error" not in summary


def test_error_code_column_not_flagged():
    """A column literally named ``error_code`` (with data values) is not an error."""
    payload = {
        "columns": ["id", "error_code"],
        "rows": [["1", "E42"], ["2", "OK"]],
        "row_count": 2,
    }
    summary = summarize_tool_result("", _wrap(payload))
    assert "Query error" not in summary
    assert summary.startswith("2 record")


def test_error_null_status_envelope_not_flagged():
    """A status envelope carrying ``"error": null`` alongside data is NOT an error
    (present-but-null key must be treated as data, not a failure)."""
    payload = {"columns": ["seg"], "rows": [["s1"]], "row_count": 1, "error": None}
    summary = summarize_tool_result("", _wrap(payload))
    assert "Query error" not in summary
    assert summary.startswith("1 record")


def test_list_of_data_with_error_value_not_flagged():
    """A list-of-dicts payload whose values contain 'error' is summarized as items."""
    payload = [
        {"name": "seg-1", "state": "error"},
        {"name": "seg-2", "state": "running"},
    ]
    summary = summarize_tool_result("", _wrap(payload))
    assert "Query error" not in summary
    assert summary.startswith("2 items")


# --------------------------------------------------------------------------- #
# (b) Explicit error envelopes ARE flagged
# --------------------------------------------------------------------------- #
def test_explicit_error_envelope_flagged():
    """An explicit ``{"error": "..."}`` envelope is flagged as a query error."""
    payload = {"error": "TABLE_NOT_FOUND: no such table 'foo'"}
    summary = summarize_tool_result("", _wrap(payload))
    assert summary.startswith("Query error")
    assert "TABLE_NOT_FOUND" in summary


def test_error_envelope_with_message_dict_flagged():
    """A nested ``{"error": {"message": ...}}`` envelope surfaces the message."""
    payload = {"error": {"message": "workgroup bytes-scanned limit exceeded"}}
    summary = summarize_tool_result("", _wrap(payload))
    assert summary.startswith("Query error")
    assert "bytes-scanned" in summary


def test_dunder_error_envelope_flagged():
    """A ``__error__`` envelope key is also recognized as structural failure."""
    payload = {"__error__": "boom"}
    summary = summarize_tool_result("", _wrap(payload))
    assert summary.startswith("Query error")


# --------------------------------------------------------------------------- #
# sitewise_list_associated_assets shape — summarized like other list shapes
# --------------------------------------------------------------------------- #
def test_sitewise_list_associated_assets_summarized():
    """The list-of-asset-dicts shape returned by sitewise_list_associated_assets is
    summarized as items (name preview), not dropped to a fake error."""
    payload = [
        {"id": "uuid-1", "name": "Line-1", "assetModelId": "m1"},
        {"id": "uuid-2", "name": "Line-2", "assetModelId": "m1"},
        {"id": "uuid-3", "name": "Line-3", "assetModelId": "m1"},
        {"id": "uuid-4", "name": "Line-4", "assetModelId": "m1"},
    ]
    summary = summarize_tool_result("", _wrap(payload))
    assert summary.startswith("4 items")
    assert "Line-1" in summary
    assert "+1 more" in summary


# --------------------------------------------------------------------------- #
# Genuinely unexpected shape — best-effort summary, never "Error processing results."
# --------------------------------------------------------------------------- #
def test_weird_dict_shape_best_effort_summary():
    """An unrecognized dict shape yields a best-effort key summary, never the opaque
    'Error processing results.'."""
    payload = {"alpha": 1, "beta": 2, "gamma": 3}
    summary = summarize_tool_result("", _wrap(payload))
    assert summary != "Error processing results."
    assert "3 field" in summary
    assert "alpha" in summary


def test_scalar_shape_best_effort_summary():
    """A bare JSON scalar (int) is summarized best-effort, not a fake error."""
    summary = summarize_tool_result("", _wrap(42))
    assert summary != "Error processing results."
    assert "42" in summary


def test_non_json_text_returned_verbatim():
    """Non-JSON tool text is returned as-is, never flagged an error."""
    summary = summarize_tool_result("", {"content": [{"text": "plain status text"}]})
    assert summary.startswith("plain status text")
    assert "Query error" not in summary


def test_empty_result_no_content():
    """An empty content list degrades to a benign empty summary, not an error."""
    summary = summarize_tool_result("", {"content": []})
    assert summary in ("No results.", "Empty result.")
    assert "Query error" not in summary and summary != "Error processing results."


def test_never_returns_error_processing_results():
    """Across a spread of odd inputs the summarizer must never emit the old opaque
    'Error processing results.' string."""
    odd_inputs = [
        {"content": [{"text": "["}]},          # malformed JSON
        {"content": [{"text": json.dumps([])}]},  # empty list
        {"content": [{"text": json.dumps({})}]},  # empty dict
        {"content": [{"text": json.dumps({"nested": {"a": [1, 2]}})}]},
        {},                                      # no content key
        None,                                    # not even a dict
    ]
    for r in odd_inputs:
        assert summarize_tool_result("", r) != "Error processing results."


# --------------------------------------------------------------------------- #
# Recognized shapes still summarize correctly (no regression)
# --------------------------------------------------------------------------- #
def test_schema_shape_summarized():
    payload = {
        "orders": {"columns": ["order_id", "qty"], "concept_map": {}},
        "lines": {"columns": ["line_id"], "concept_map": {}},
    }
    summary = summarize_tool_result("get_system_schema", _wrap(payload))
    assert summary.startswith("Schema: 2 tables")


def test_neptune_shape_no_results():
    summary = summarize_tool_result("explore_graph", _wrap({"results": []}))
    assert summary == "No matching entities found."


# --------------------------------------------------------------------------- #
# i3X envelope-list — element/channel/point counts, not generic truncation
# --------------------------------------------------------------------------- #
def test_i3x_envelope_list_history_counts():
    """A list of per-elementId wrappers with VQT ``values`` lists summarizes as
    element/channel/point counts instead of a generic item preview."""
    payload = [
        {"elementId": "e-1", "result": {"values": [{"value": 1.0}, {"value": 2.0}]}},
        {"elementId": "e-2", "result": {"values": [{"value": 3.0}]}},
    ]
    summary = summarize_tool_result("", _wrap(payload))
    assert summary == "2 element(s), 2 channel(s), 3 point(s)"


def test_i3x_envelope_list_live_values_counted():
    """Single live-VQT wrappers (``objects/value``) count one point per channel."""
    payload = [
        {"elementId": "e-1", "result": {"value": 7.5, "quality": "Good"}},
        {"elementId": "e-2", "result": {"value": None, "quality": "GoodNoData"}},
    ]
    summary = summarize_tool_result("", _wrap(payload))
    assert summary == "2 element(s), 2 channel(s), 2 point(s)"


def test_i3x_envelope_list_capped_channel_counts_stats():
    """A claim-checked channel nested inside a wrapper counts its ``_stats.count``."""
    payload = [
        {"elementId": "e-1", "result": {
            "_stats": {"count": 480, "min": 1, "max": 9, "mean": 5},
            "dataset_ref": "tel-00aa11bb22cc",
            "landmark_values": [{"value": 1}],
        }},
    ]
    summary = summarize_tool_result("", _wrap(payload))
    assert summary == "1 element(s), 1 channel(s), 480 point(s)"


def _claim_check(count: int) -> dict:
    return {
        "_stats": {"count": count, "min": 1, "max": 9, "mean": 5},
        "dataset_ref": "tel-00aa11bb22cc",
        "landmark_values": [{"value": 1}],
    }


def test_i3x_envelope_list_capped_values_dict_counts_stats():
    """REAL capped-history wire shape #1: ``_cap_history_payload``
    replaces the VQT list in place UNDER its ``values`` key, so the claim-check
    arrives as ``result.values = {claim-check}`` — must count as 1 channel with
    ``_stats.count`` points, not 0/0 (which read as 'no data' for a SUCCESSFUL fetch)."""
    payload = [{"elementId": "e-1", "result": {"values": _claim_check(480)}}]
    summary = summarize_tool_result("", _wrap(payload))
    assert summary == "1 element(s), 1 channel(s), 480 point(s)"


def test_i3x_envelope_list_capped_components_counts_stats():
    """REAL capped-history wire shape #2: the maxDepth=0
    composition shape nests per-component claim-checks under
    ``result.components`` — every component counts as a channel."""
    payload = [{"elementId": "e-1", "result": {"components": {
        "e-1:prop-1": {"values": _claim_check(480)},
        "e-1:prop-2": {"values": _claim_check(120)},
    }}}]
    summary = summarize_tool_result("", _wrap(payload))
    assert summary == "1 element(s), 2 channel(s), 600 point(s)"


def test_i3x_envelope_list_components_live_values_counted():
    """Uncapped composition shape: component VQT LISTS still count per point
    (regression guard — the claim-check fix must not break the live shape)."""
    payload = [{"elementId": "e-1", "result": {"components": {
        "e-1:prop-1": {"values": [{"value": 1.0}, {"value": 2.0}]},
    }}}]
    summary = summarize_tool_result("", _wrap(payload))
    assert summary == "1 element(s), 1 channel(s), 2 point(s)"


def test_i3x_envelope_list_not_an_error():
    """i3X wrappers are VALID payloads — never labeled errors (module contract),
    even when a data value contains the word 'error'."""
    payload = [{"elementId": "e-1", "result": {"values": [{"value": "error"}]}}]
    summary = summarize_tool_result("", _wrap(payload))
    assert "Query error" not in summary
    assert summary.startswith("1 element(s)")


def test_plain_asset_list_without_result_key_unchanged():
    """A flat list whose dicts lack the wrapper ``result`` key keeps the existing
    list summary (regression guard on the i3X detection specificity)."""
    payload = [{"elementId": "e-1", "displayName": "Unit-A"}]
    summary = summarize_tool_result("", _wrap(payload))
    assert summary.startswith("1 items")


# --------------------------------------------------------------------------- #
# i3X error envelope — success:false/responseDetail IS a structural error
# --------------------------------------------------------------------------- #
def test_i3x_error_envelope_flagged_with_detail_surfaced():
    payload = {"success": False, "responseDetail": {
        "title": "Not Found", "status": 404, "detail": "element does not exist",
    }}
    summary = summarize_tool_result("", _wrap(payload))
    assert summary.startswith("Query error")
    assert "Not Found" in summary
    assert "404" in summary
    assert "element does not exist" in summary


def test_i3x_error_envelope_message_fallback():
    payload = {"success": False, "responseDetail": {"title": "Bad Request",
                                                    "message": "missing window"}}
    summary = summarize_tool_result("", _wrap(payload))
    assert summary.startswith("Query error")
    assert "Bad Request" in summary and "missing window" in summary


def test_success_false_without_response_detail_not_guessed_into_error():
    """``success: false`` ALONE is not the i3X wire shape — left to the
    best-effort summary rather than guessed into a failure (contract: valid /
    ambiguous payloads must not be labeled errors)."""
    summary = summarize_tool_result("", _wrap({"success": False, "note": "odd"}))
    assert "Query error" not in summary


def test_success_true_envelope_not_an_error():
    """A ``success: true`` status envelope is data, never an error."""
    payload = {"success": True, "responseDetail": {"title": "OK"}}
    summary = summarize_tool_result("", _wrap(payload))
    assert "Query error" not in summary


# --------------------------------------------------------------------------- #
# Claim-check shape — stats summary noting the full series was withheld
# --------------------------------------------------------------------------- #
def test_claim_check_summarized_as_stats():
    payload = {
        "_stats": {"count": 5000, "min": 1.2, "max": 9.8, "mean": 5.1,
                   "first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-01-02T00:00:00Z"},
        "dataset_ref": "tel-0123456789ab",
        "landmark_values": [{"value": 1.2}, {"value": 9.8}],
        "note": "full series withheld; NEVER reconstruct or chart from landmarks",
    }
    summary = summarize_tool_result("", _wrap(payload))
    assert "5000 point(s)" in summary
    assert "min 1.2" in summary and "max 9.8" in summary and "mean 5.1" in summary
    assert "full series withheld" in summary
    assert "tel-0123456789ab" in summary
    assert "Query error" not in summary


def test_claim_check_without_stats_values_still_not_error():
    payload = {"_stats": {}, "dataset_ref": "tel-ffeeddccbbaa", "landmark_values": []}
    summary = summarize_tool_result("", _wrap(payload))
    assert "full series withheld" in summary
    assert "Query error" not in summary


# --------------------------------------------------------------------------- #
# Chart-bearing results — tags stripped, the text kept; "Rendered N chart(s)."
# only when nothing else remains
# --------------------------------------------------------------------------- #
def test_chart_result_summarized_never_echoes_tags():
    text = (
        "Telemetry chart (2 channels, 8h window):\n\n"
        '<chart-block type="line" title="&lt;series-a&gt;" data="[1,2,3]"/>\n\n'
        '<chart-block type="line" title="&lt;series-b&gt;" data="[4,5,6]"/>\n'
    )
    summary = summarize_tool_result("sitewise_get_telemetry_chart",
                                    {"content": [{"text": text}]})
    assert summary == "Telemetry chart (2 channels, 8h window)"
    assert "<chart-block" not in summary


def test_single_chart_and_truncated_tag_not_counted():
    """A truncated/non-self-closing tag never counts — only complete tags do —
    and its fragment is never echoed."""
    text = '<chart-block type="line" data="[1]"/>\n<chart-block type="line" data="[2]'
    summary = summarize_tool_result("", {"content": [{"text": text}]})
    assert summary == "Rendered 1 chart(s)."


_TAG_A = ("<chart-block type=\"line\" title=\"Unit A: vib_x (mm/s)\" "
          "labels='[\"11:00\",\"11:02\"]' datasets='[{\"label\":\"vib_x\",\"data\":[1.9,2.0]}]' />")
_TAG_B = ("<chart-block type=\"line\" title=\"Unit A: coolant_temp (°C)\" "
          "labels='[\"11:00\",\"11:02\"]' datasets='[{\"label\":\"t\",\"data\":[21.0,21.1]}]' />")


def test_answer_with_relayed_charts_keeps_the_answer_text():
    """A Mode A system answer with relayed chart tags (the local in-process
    trace summarizes it before redaction) keeps the answer; the bare chart
    count is only for a result that is nothing but charts."""
    answer = "Vibration on Unit A rose from 1.9 to 2.4 mm/s over the hour."
    summary = summarize_tool_result("sys___query_sys",
                                    {"content": [{"text": f"{answer}\n\n{_TAG_A}\n\n{_TAG_B}"}]})
    assert summary == answer
    tags_only = summarize_tool_result("", {"content": [{"text": f"\n{_TAG_A}\n\n{_TAG_B}\n"}]})
    assert tags_only == "Rendered 2 chart(s)."
    partial = f"{answer}\n\n{_TAG_A}\n\n<chart-block type=\"line\" title=\"cut"
    assert summarize_tool_result("", {"content": [{"text": partial}]}) == answer


def test_i3x_history_with_a_chart_suffix_summarizes_its_json(monkeypatch):
    """The i3X guaranteed-chart suffix rides after claim-checked JSON: the
    summary is the JSON's own summary, not the raw JSON text and not markup."""
    from .test_i3x_guaranteed_charts import ChartServingClient, _batch_out, _history
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)
    raw = _batch_out(ChartServingClient(_history()))
    end = json.JSONDecoder().raw_decode(raw)[1]
    assert "<chart-block" in raw[end:]
    summary = summarize_tool_result("sitewise_get_assets_history", {"content": [{"text": raw}]})
    assert summary == summarize_tool_result("sitewise_get_assets_history",
                                            {"content": [{"text": raw[:end]}]})
    assert summary == "1 element(s), 1 channel(s), 40 point(s)"


def test_local_tool_trace_keeps_the_answer_beside_relayed_charts():
    """The local in-process path (AfterToolCallEvent, before redaction)."""
    import queue
    from unittest.mock import MagicMock
    from agents.chat_client import _ToolResultHook
    answer = "Vibration on Unit A rose from 1.9 to 2.4 mm/s over the hour."
    events: queue.Queue = queue.Queue()
    event = MagicMock()
    event.tool_use = {"name": "sys___query_sys", "toolUseId": "t1", "input": {}}
    event.result = {"status": "success",
                    "content": [{"text": f"{answer}\n\n{_TAG_A}\n\n{_TAG_B}"}]}
    _ToolResultHook(events)._on_tool_done(event)
    item = events.get_nowait()
    assert item["event"] == "tool_result"
    assert item["data"]["summary"] == answer
    assert "<chart-block" not in item["data"]["summary"]


# --------------------------------------------------------------------------- #
# Byte-identical regression pins — existing shapes must not change AT ALL
# --------------------------------------------------------------------------- #
def test_existing_fixture_summaries_byte_identical():
    """Exact-output pins captured from the pre-B4 module: the new i3X / chart /
    claim-check branches must leave every previously-recognized shape's summary
    byte-identical."""
    cases = [
        ("anycompany___athena_run_query",
         _wrap({"columns": ["order_id", "status"],
                "rows": [["A-1", "error"], ["A-2", "completed"], ["A-3", "open"]],
                "row_count": 3}),
         "3 records found: A-1 (error), A-2 (completed), A-3 (open)"),
        ("get_system_schema",
         _wrap({"orders": {"columns": ["order_id", "qty"], "concept_map": {}},
                "lines": {"columns": ["line_id"], "concept_map": {}}}),
         "Schema: 2 tables — orders(2 cols), lines(1 cols)"),
        ("explore_graph",
         _wrap({"results": [{"n": {"~labels": ["Asset"],
                                   "~properties": {"name": "Widget-Line",
                                                   "domain": "production"}}}]}),
         "Asset: Widget-Line (production)"),
        ("explore_graph",
         _wrap({"results": [{"r": {"~labels": ["IdentityRule"],
                                   "~properties": {"from_system": "erp",
                                                   "to_system": "mes",
                                                   "concept": "work-order"}}}]}),
         "IdentityRule: erp → mes (work-order)"),
        ("explore_graph", _wrap({"results": []}), "No matching entities found."),
        ("", _wrap({"site": "s", "area": "a", "line": "l",
                    "processes": {"proc-a": {}, "proc-b": {}}}),
         "2 process assets: proc-a, proc-b"),
        ("", _wrap({"id1_p1": [{"value": 1}, {"value": 2}], "id2_p2": [{"value": 3}]}),
         "2 time-series: 3 data points total"),
        ("", _wrap([{"id": "uuid-1", "name": "Line-1"}, {"id": "uuid-2", "name": "Line-2"},
                    {"id": "uuid-3", "name": "Line-3"}, {"id": "uuid-4", "name": "Line-4"}]),
         "4 items: Line-1, Line-2, Line-3 +1 more"),
        ("", _wrap({"assetId": "a-1", "assetName": "Mixer",
                    "properties": [{"p": 1}, {"p": 2}]}),
         "Asset: Mixer (2 properties)"),
        ("", _wrap({"error": "TABLE_NOT_FOUND: no such table 'foo'"}),
         "Query error — TABLE_NOT_FOUND: no such table 'foo'"),
        ("", _wrap({"alpha": 1, "beta": 2, "gamma": 3}),
         "Result with 3 fields: alpha, beta, gamma"),
        ("", {"content": [{"text": "plain status text"}]}, "plain status text"),
        ("", _wrap([]), "0 items"),
    ]
    for tool_name, result, expected in cases:
        assert summarize_tool_result(tool_name, result) == expected


def test_history_summary_counts_only_series_not_metadata_keys():
    """A history read carries metadata beside its series: the source block and
    the sampling summary, both under underscore-prefixed keys. The trace counts
    only the series and only their points."""
    payload = {
        "a1_p1": [{"value": 1, "timestamp": 1}, {"value": 2, "timestamp": 2}],
        "_adt_source": {"boundary": "sitewise", "system_id": "plant-x", "items": 1},
        "_sampling": {"note": "sample", "series": {"a1_p1": {"points_returned": 2,
                                                            "values_read": 40}}},
    }
    assert summarize_tool_result("", _wrap(payload)) == "1 time-series: 2 data points total"
    four = {f"a{i}_p{i}": [{"value": i}] for i in range(4)}
    four.update({"_adt_source": {"items": 4}, "_sampling": {"note": "n", "series": {}}})
    assert summarize_tool_result("", _wrap(four)) == "4 time-series: 4 data points total"


def test_only_the_known_history_metadata_keys_are_skipped():
    """The history branch skips exactly the source block and the sampling
    summary. Any other underscore-prefixed key stays, so a non-history result
    with a scalar status beside a list is not read as a time series."""
    records = {"records": [{"id": "r1"}], "_partial": True}
    assert "time-series" not in summarize_tool_result("", _wrap(records))
    only_meta = {"_adt_source": {"items": 0}, "_sampling": {"note": "n", "series": {}}}
    assert "time-series" not in summarize_tool_result("", _wrap(only_meta))
    asset = {"assetId": "a-9", "assetName": "Kiln", "properties": [{"p": 1}],
             "_adt_source": {"items": 1}}
    assert summarize_tool_result("", _wrap(asset)) == "Asset: Kiln (1 properties)"


def test_history_metadata_keys_match_their_producers():
    from agents import response_integrity_contracts, sitewise_read
    from agents import tool_result_summary as trs

    assert trs._HISTORY_METADATA_KEYS == frozenset({
        response_integrity_contracts.SOURCE_METADATA_KEY,
        sitewise_read.HISTORY_SAMPLING_KEY,
        sitewise_read.HISTORY_WINDOW_KEY,
    })
