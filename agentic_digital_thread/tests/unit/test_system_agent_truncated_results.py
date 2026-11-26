# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System agents never present a set read off a truncated result as complete.

Measured on a rehearsal account (2026-10-07): asked for every shipment of one
product to one customer this week, the agent sorted newest first, hit the row
cap, and got a result marked truncated that lacked the oldest line (the one the
question was about). It noticed the truncation and ran a total count, then
listed the distinct batches it saw in the truncated rows as the complete set
with no note that anything was missing, so the orchestrator could not
disclose the gap. The rule is an ENGINE rule in the policy tail, so every
connection type gets it and neither a custom registration prompt nor steward
context can displace it.

Fully offline (no Bedrock, no AWS, no DB, no sockets). Pins:

1. The rule is in the BUILT system prompt of EVERY connection type, once.
2. It sits in the policy tail right after RECORD IDENTIFIERS and right before
   BUDGET, after the fenced business context, and survives a custom
   registration ``system_prompt``.
3. Report-only and enforce layering are unchanged.
4. The key clauses: the triggers (truncated, status partial, a next page
   token or cursor, a more-results flag), read the partial note instead of
   assuming, nothing read off the rows is complete, SQL recovery is ONE query
   at the set's own grain, paged sources read the following pages, other
   sources cover the ORIGINAL scope with non-overlapping parts (never one
   narrower part), and an unrecoverable cut is stated with what was read and
   what was not. EFFICIENCY step 3 names a partial result as a reason to
   re-query, so the two rules agree.
5. The engine half: every bounded result that is partial carries a
   plain-language ``partial_note`` that matches its kind (row cut, column
   cap only, full Tulip page), on the PostgreSQL tool, the shared helper,
   the Athena capped path and the Tulip records path; a column cap alone is
   no longer reported as a row cut; and the measured failure (544 rows, newest
   first, 500-row cap) now reaches the model with a note saying later rows
   were not returned and a set read off them is not the full result.
6. No demo literal: no story identifier, no identifier-shaped token other
   than the ``partial_note`` key itself, digits only as list markers, and
   only known prompt keywords in capitals, in the rule AND in the note.
"""
import json
import re

import pytest

import agents.system_agent as sa
from agents.connection_types import TULIP
from agents.demo import story_lookup
from agents.response_integrity_query import (
    MAX_QUERY_COLUMNS,
    PARTIAL_NOTE_KEY,
    REPEAT_NOTE_KEY,
    bounded_query_result,
    fetch_max_plus_one,
    partial_note,
)
from agents.untrusted_text import FENCE_CLOSE


class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeAgent:
    """Captures what create_system_agent built without running an LLM."""

    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []


# Same minimal per-type configs as test_system_agent_record_identifiers.py.
_CFG_BY_TYPE = {
    "PostgreSQL": {"host": "h", "dbname": "d"},
    "Athena": {"workgroup": "wg", "database": "db", "region": "us-east-1"},
    "SiteWise (Direct)": {},
    "SiteWise (i3X)": {"base_url": "http://i3x.example.com"},
    TULIP: {"base_url": "https://your-instance.tulip.co", "auth_scheme": "basic"},
    "MCP Server": {"url": "http://mcp.example.com"},
}

_HEADER = "TRUNCATED RESULTS (required)"

_ALL_VARIANTS = ["PostgreSQL", "Athena", "SiteWise (Direct)", "SiteWise (i3X)",
                 TULIP, "REST API", "MCP Server", ""]


def _make_agent(monkeypatch, connection_type, system_prompt="Base system prompt."):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr(sa, "_mcp_system_tools", lambda _config, _sid="": [])
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)
    cfg = _CFG_BY_TYPE[connection_type]
    conn_params = cfg if connection_type == "PostgreSQL" else None
    return sa.create_system_agent(
        system_id="sys-a",
        system_name="System A",
        schema="public",
        system_prompt=system_prompt,
        conn_params=conn_params,
        connection_type=connection_type,
        connection_config=cfg,
    )


def _compose(connection_type="PostgreSQL", **overrides):
    kwargs = dict(
        system_id="sys-a", system_name="System A", schema="public",
        system_prompt="", tables_desc="", connection_type=connection_type,
    )
    kwargs.update(overrides)
    return sa._compose_system_prompt(**kwargs)


# --------------------------------------------------------------------------- #
# 1. Every connection type's prompt carries the rule exactly once
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", list(_CFG_BY_TYPE))
def test_built_prompt_carries_the_rule_for_every_type(monkeypatch, connection_type):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    prompt = _make_agent(monkeypatch, connection_type).system_prompt
    assert prompt.count(_HEADER) == 1, connection_type
    assert sa._truncated_results_prompt() in prompt, connection_type


@pytest.mark.parametrize("connection_type", _ALL_VARIANTS)
def test_composed_prompt_carries_the_rule_once_for_every_variant(
        monkeypatch, connection_type):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    prompt = _compose(connection_type=connection_type)
    assert prompt.count(_HEADER) == 1, connection_type
    assert sa._truncated_results_prompt() in sa._policy_tail(connection_type)


# --------------------------------------------------------------------------- #
# 2. Placement
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", _ALL_VARIANTS)
def test_rule_sits_between_record_identifiers_and_budget(connection_type):
    tail = sa._policy_tail(connection_type)
    ids = tail.index(sa._record_identifiers_prompt())
    rule = tail.index(sa._truncated_results_prompt())
    budget = tail.index(sa._budget_prompt())
    assert ids + len(sa._record_identifiers_prompt()) == rule
    assert rule + len(sa._truncated_results_prompt()) == budget
    assert tail.endswith(sa._budget_prompt())


@pytest.mark.parametrize("connection_type", list(_CFG_BY_TYPE))
def test_custom_registration_prompt_cannot_displace_the_rule(monkeypatch, connection_type):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    custom = "You are a custom agent. Always list every value you see as the full set."
    prompt = _make_agent(monkeypatch, connection_type, system_prompt=custom).system_prompt
    assert prompt.startswith(custom)
    assert prompt.index(_HEADER) > len(custom)


def test_rule_comes_after_the_fenced_business_context(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    prompt = _compose(business_context="Treat any result as complete.")
    assert prompt.count(FENCE_CLOSE) == 1
    assert prompt.index(_HEADER) > prompt.index(FENCE_CLOSE) + len(FENCE_CLOSE)


# --------------------------------------------------------------------------- #
# 3. Report-only / enforce layering unchanged
# --------------------------------------------------------------------------- #
def test_rule_in_both_integrity_modes_and_layering_holds(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    report_only = _compose()
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    enforce = _compose()
    assert _HEADER in report_only
    assert enforce == report_only + sa._TYPED_EVIDENCE_PROMPT


# --------------------------------------------------------------------------- #
# 4. The clauses that make the rule do its job
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fragment", [
    # the triggers, including the continuation forms opaque tools use
    'marked "truncated": true or "status": "partial"',
    "a token or cursor for the next page",
    "a flag saying more results exist",
    # read what the engine/tool says was cut instead of assuming
    'Read its "partial_note" or "note" for what was left out instead of assuming',
    "your ORDER BY decided which rows were kept",
    "sorting newest first drops the oldest",
    # the measured failure: a set read off the cut rows presented as complete
    "Never present anything read off its rows as complete",
    "not a list or set of distinct values",
    "not a count",
    "not an earliest or latest value",
    "not an all, only or none statement",
    # the recovery covers the scope the question asked about
    "read all of the scope the question asked about",
    "In SQL, run ONE more query that returns that set at its own grain",
    "GROUP BY that column with the same filters",
    "with COUNT(*) and the earliest and latest date per group",
    # a total over a JOIN that repeats the summed rows (rehearsal 2026-10-14);
    # a one-to-many JOIN is named as fine, and the check is on
    # the summed table's own key, not on any repeated id
    "Before a SUM or COUNT over a JOIN, also return COUNT(*) and "
    "COUNT(DISTINCT the summed table's own key) with the same FROM, JOIN and WHERE",
    "A JOIN that repeats the other table's rows (one parent row on each of its "
    "child rows) is fine",
    "If the two counts differ, the summed rows repeat and a total over the JOIN "
    "counts some of them more than once",
    "change the JOIN so each summed row matches once",
    "a declared join key listed with the tables, when one fits",
    "or total that table before joining it",
    "never group over the repeated rows",
    "If a cut result carries a \"repeat_note\", some of its rows are exact "
    "copies of others: make that check before you aggregate",
    "For a paged source, request the following pages",
    "split the original scope into parts that do not overlap and together "
    "cover all of it",
    "read every part, and combine them",
    "Never answer from one narrower part as if it were the whole",
    # the fallback: say so, with what was and was not read
    "If you still cannot read all of it, say so in your answer",
    "how much you did read (the number of rows or the span covered)",
    "which part of the scope was not read",
])
def test_rule_carries_the_key_clauses(fragment):
    assert fragment in sa._truncated_results_prompt()


def test_non_sql_recovery_never_answers_from_one_narrowed_slice():
    # An earlier rule told non-SQL sources to "narrow the request" until the result
    # was no longer truncated and then answer from it: one narrower window
    # answered as the whole recreates the bug. The wording must be gone.
    text = sa._truncated_results_prompt()
    assert "narrow the request" not in text
    assert "until the result is no longer truncated" not in text


def test_efficiency_names_a_partial_result_as_a_reason_to_requery():
    text = sa._efficiency_prompt()
    # The tool-error clause is unchanged...
    assert "Re-query ONLY on an ACTUAL tool error" in text
    assert "per SELF-RECOVERY" in text
    # ...and the partial case is named in the same sentence, so EFFICIENCY
    # and TRUNCATED RESULTS do not contradict each other.
    step3 = text[text.index("\n3. "):text.index("\n4. ")]
    assert "or on a partial (truncated) result" in step3
    assert "that lacks rows or columns the question needs" in step3
    assert "then follow TRUNCATED RESULTS" in step3


def test_a_columns_only_cut_does_not_trigger_the_record_steps():
    # A column cap keeps every row, so the rule must not tell
    # the agent to hedge counts or re-query for rows it already has.
    text = sa._truncated_results_prompt()
    columns_clause = text.index("If it says only columns were left out")
    steps = text.index("Before you answer from a result that is missing records")
    assert columns_clause < steps
    assert "the rows are complete: answer from the columns you have" in text
    assert ("query again only when the question needs a column that was left "
            "out") in text


# --------------------------------------------------------------------------- #
# 5. The marker the rule names is the one the read paths emit
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, n_rows):
        self.description = [("batch_id",), ("shipped_at",)]
        self._rows = iter([(f"b{i}", f"t{i}") for i in range(n_rows)])

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return next(self._rows, None)


class _FakeConn:
    def __init__(self, n_rows):
        self._cursor = _FakeCursor(n_rows)

    def set_session(self, **_kwargs):
        return None

    def cursor(self):
        return self._cursor

    def close(self):
        return None


@pytest.mark.parametrize("n_rows, expect_truncated", [
    (sa._MAX_ROWS + 1, True),   # over the cap: cut, marked
    (sa._MAX_ROWS, True),       # exactly at an injected cap: can't prove whole
    (sa._MAX_ROWS - 1, False),  # under the cap: whole, unmarked
])
def test_postgres_tool_marks_a_capped_result_truncated(monkeypatch, n_rows, expect_truncated):
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _FakeConn(n_rows))
    result, executed = sa._execute_direct_query(
        "public", {"host": "h"}, "SELECT batch_id, shipped_at FROM t ORDER BY shipped_at DESC")
    assert executed.rstrip().endswith(f"LIMIT {sa._MAX_ROWS}")
    assert result["row_count"] == min(n_rows, sa._MAX_ROWS)
    assert result.get("truncated", False) is expect_truncated
    if expect_truncated:
        assert result["partial_reason"] == "row_limit"
        assert result[PARTIAL_NOTE_KEY] == partial_note(
            sa._MAX_ROWS, 2, cut="rows")
        # The serialized payload the model reads carries the same keys.
        assert '"truncated":true' in json.dumps(result, separators=(",", ":"))
    else:
        assert PARTIAL_NOTE_KEY not in result


def test_measured_failure_reaches_the_model_with_a_plain_note(monkeypatch):
    # The case seen on a rehearsal account: 544 matching rows, newest first,
    # 500-row cap. What the agent reads must say in words that later rows are
    # missing and that a set read off these rows is not the full result.
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _FakeConn(544))
    tool = sa._make_direct_query_tool("public", {"host": "h"}, "sys-a")
    payload = json.loads(tool._tool_func(
        sql="SELECT batch_id, shipped_at FROM t ORDER BY shipped_at DESC"))
    assert payload["row_count"] == 500 and payload["truncated"] is True
    note = payload[PARTIAL_NOTE_KEY]
    assert "Only the first 500 rows are included" in note
    assert "every row after them was not returned" in note
    assert "query's ORDER BY" in note
    assert "is not the full result" in note


def test_shared_bounded_result_marks_truncation_with_the_named_key():
    cut = bounded_query_result(["a"], [[1]], truncated=True)
    whole = bounded_query_result(["a"], [[1]], truncated=False)
    assert cut["truncated"] is True
    assert cut[PARTIAL_NOTE_KEY] == partial_note(1, 1, cut="rows")
    assert "truncated" not in whole and PARTIAL_NOTE_KEY not in whole


def test_a_column_cap_alone_is_not_reported_as_a_row_cut():
    columns = [f"c{i}" for i in range(MAX_QUERY_COLUMNS + 1)]
    rows = [list(range(len(columns))) for _ in range(3)]

    class _Cursor:
        description = [(c,) for c in columns]

        def __init__(self):
            self._it = iter(rows)

        def fetchone(self):
            return next(self._it, None)

    kept, exceeded = fetch_max_plus_one(_Cursor(), 10, columns)
    assert len(kept) == 3 and exceeded is False
    result = bounded_query_result(columns, kept, exceeded)
    assert result["truncated"] is True
    assert result["partial_reason"] == "column_limit"
    note = result[PARTIAL_NOTE_KEY]
    assert f"Only the first {MAX_QUERY_COLUMNS} columns are included" in note
    assert "Every row is included." in note
    assert "rows are included" not in note
    assert "not the full result" not in note


def test_a_byte_cut_is_a_row_cut_with_the_same_note():
    big = "x" * 8_000
    result = bounded_query_result(["a"], [[big]] * 100, truncated=False)
    assert result["truncated"] is True
    assert result["partial_reason"] == "byte_limit"
    assert result[PARTIAL_NOTE_KEY] == partial_note(
        result["row_count"], 1, cut="rows")


def test_athena_result_marks_a_capped_result_with_the_named_key(monkeypatch):
    import agents.athena_tools as at

    monkeypatch.setattr(at, "_collect_results",
                        lambda _athena, _qid, max_rows: {"columns": ["a"],
                                                         "rows": [[1]] * max_rows,
                                                         "row_count": max_rows})
    out = at._athena_result(None, "q", 3, True, "sys-a", None, "SELECT a FROM t LIMIT 3")
    assert out["truncated"] is True
    assert out["partial_reason"] == "row_limit"
    assert out[PARTIAL_NOTE_KEY] == partial_note(3, 1, cut="rows")


def test_athena_keeps_the_collectors_own_note(monkeypatch):
    import agents.athena_tools as at

    collected = bounded_query_result(["a"], [[1]] * 3, truncated=True)
    monkeypatch.setattr(at, "_collect_results", lambda *_a, **_k: dict(collected))
    out = at._athena_result(None, "q", 3, True, "sys-a", None, "SELECT a FROM t LIMIT 3")
    assert out[PARTIAL_NOTE_KEY] == collected[PARTIAL_NOTE_KEY]


def test_athena_cap_on_a_column_capped_result_names_both_cuts(monkeypatch):
    # The collector saw only a column cap (129 columns), but the
    # injected LIMIT was reached, so later rows may exist too.
    import agents.athena_tools as at

    columns = [f"c{i}" for i in range(MAX_QUERY_COLUMNS + 1)]
    collected = bounded_query_result(
        columns, [list(range(len(columns)))] * 3, truncated=False)
    assert collected["partial_reason"] == "column_limit"
    assert "Every row is included." in collected[PARTIAL_NOTE_KEY]
    monkeypatch.setattr(at, "_collect_results", lambda *_a, **_k: dict(collected))
    out = at._athena_result(None, "q", 3, True, "sys-a", None, "SELECT * FROM t LIMIT 3")
    assert out["truncated"] is True and out["capped_at_limit"] is True
    assert out[PARTIAL_NOTE_KEY] == partial_note(
        3, MAX_QUERY_COLUMNS, cut="rows", columns_cut=True)
    assert "Only the first 3 rows are included" in out[PARTIAL_NOTE_KEY]
    assert "columns were omitted" in out[PARTIAL_NOTE_KEY]
    assert "Every row is included." not in out[PARTIAL_NOTE_KEY]


class _BigBodyResp:
    headers = {"content-type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield b"[" + b'{"v": 1},' * 40_000


def test_rest_body_cut_is_a_response_cut_not_a_row_cut(monkeypatch):
    # A byte cut of a REST body has no rows and no ORDER BY.
    import contextlib

    import agents.query_router as qr
    from agents import pinned_http

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, _method, _url, headers=None):
            return _BigBodyResp()

    # Patch the one seam the REST reader sends through, not the httpx module,
    # so the result does not depend on which modules an earlier test imported.
    @contextlib.contextmanager
    def _fake_client(**_kwargs):
        yield _Client()

    monkeypatch.setattr(pinned_http, "client", _fake_client)
    monkeypatch.setattr(qr, "_openapi_target", lambda *_a: ("https://api.example.com/x", {}))
    out = qr._query_openapi("sys-rest", "GET /x")
    assert out["truncated"] is True
    assert out["partial_reason"] == "response_limit"
    # A 200 KB body is one cell over the cell cap, so nothing is included,
    # and the note says so instead of inventing rows or an ORDER BY.
    assert out["row_count"] == 0
    assert out[PARTIAL_NOTE_KEY] == partial_note(0, 1, cut="response")
    assert "none of it could be included" in out[PARTIAL_NOTE_KEY]
    assert "ORDER BY" not in out[PARTIAL_NOTE_KEY]


def test_response_cut_that_fits_keeps_its_prefix_note():
    out = bounded_query_result(["result"], [["short prefix"]], truncated=True,
                               reason="response_limit")
    assert out["row_count"] == 1 and out["partial_reason"] == "response_limit"
    assert "Only the first part of the response was returned" in out[PARTIAL_NOTE_KEY]


def test_mcp_text_cut_is_a_response_cut():
    import agents.query_router as qr

    out = qr._mcp_content_result("plain text prefix", True)
    assert out["partial_reason"] == "response_limit"
    assert "ORDER BY" not in out[PARTIAL_NOTE_KEY]
    assert "the content may be incomplete" in out[PARTIAL_NOTE_KEY]


def test_mcp_tools_own_truncated_flag_is_a_source_partial():
    import agents.query_router as qr

    text = json.dumps({"columns": ["a"], "rows": [[1], [2]], "truncated": True})
    out = qr._mcp_content_result(text, False)
    assert out["truncated"] is True
    assert out["partial_reason"] == "source_partial"
    assert out[PARTIAL_NOTE_KEY] == partial_note(2, 1, cut="source")
    assert "ORDER BY" not in out[PARTIAL_NOTE_KEY]


def test_mcp_whole_rows_stay_unmarked():
    import agents.query_router as qr

    out = qr._mcp_content_result(json.dumps({"columns": ["a"], "rows": [[1]]}), False)
    assert "truncated" not in out and PARTIAL_NOTE_KEY not in out


class _TulipClient:
    def __init__(self, n_records):
        self.n = n_records
        self.params = None

    def get(self, _path, params=None):
        self.params = params
        return [{"id": f"r{i}", "v": i} for i in range(min(self.n, params["limit"]))]


def test_tulip_full_page_is_partial_and_says_to_read_the_next_page():
    client = _TulipClient(250)
    out = json.loads(sa._tulip_records_impl(client, "t", 100, 0, "", ""))
    assert out["row_count"] == 100
    assert out["truncated"] is True
    assert out["partial_reason"] == "page_limit"
    assert "request the next page" in out[PARTIAL_NOTE_KEY]
    assert "is not the full result" in out[PARTIAL_NOTE_KEY]


def test_tulip_short_page_is_complete():
    out = json.loads(sa._tulip_records_impl(_TulipClient(7), "t", 100, 0, "", ""))
    assert out["row_count"] == 7
    assert "truncated" not in out and PARTIAL_NOTE_KEY not in out


# --------------------------------------------------------------------------- #
# 6. Domain neutrality
# --------------------------------------------------------------------------- #
def _all_notes() -> list[str]:
    return [partial_note(7, 3, cut="rows"),
            partial_note(7, 3, cut="", columns_cut=True),
            partial_note(7, 3, cut="rows", columns_cut=True),
            partial_note(7, 3, cut="page"),
            partial_note(7, 3, cut="response"),
            partial_note(0, 3, cut="response"),
            partial_note(7, 3, cut="source")]


@pytest.mark.parametrize("text", [sa._truncated_results_prompt()] + _all_notes())
def test_rule_and_note_name_no_story_identifier(text):
    low = text.lower()
    for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                  story_lookup.STORY_CMMS_ASSET_NUM, story_lookup.DEFECT_CHECK_TYPE,
                  story_lookup.BEARING_FAILURE_CODE):
        assert token.lower() not in low, token
    for word in ("shipment", "customer", "supplier", "machine"):
        assert word not in low, word


def test_rule_has_no_identifier_shaped_literal():
    text = sa._truncated_results_prompt()
    # Digits only as the three list markers.
    assert re.findall(r"\d+", text) == ["1", "2", "3"]
    # The identifier-shaped tokens are the two engine keys the rule points at.
    assert re.findall(r"\b[A-Za-z0-9]+[-_][A-Za-z0-9]+\b", text) == [
        PARTIAL_NOTE_KEY, REPEAT_NOTE_KEY]
    capitals = set(re.findall(r"\b[A-Z]{2,}\b", text))
    assert capitals <= {"TRUNCATED", "RESULTS", "ORDER", "BY", "ONE", "SQL",
                        "GROUP", "COUNT", "SUM", "JOIN", "DISTINCT", "FROM",
                        "WHERE"}, capitals


@pytest.mark.parametrize("note", _all_notes())
def test_note_has_no_identifier_shaped_literal(note):
    # Digits only from the counts passed in (7 rows, 3 columns).
    assert set(re.findall(r"\d+", note)) <= {"7", "3"}
    assert not re.search(r"\b[A-Za-z0-9]+[-_][A-Za-z0-9]+\b", note)
    assert set(re.findall(r"\b[A-Z]{2,}\b", note)) <= {"ORDER", "BY"}
