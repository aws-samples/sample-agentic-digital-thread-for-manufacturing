# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the opt-in per-session trace drill-down
(``agents/agents_trace_routes.py`` + the shared ``agents/_insights.py`` runner).

Covers, per the Wave O2 contract:
  1. the FEATURE GATE fast path — ``AGENTS_OBSERVABILITY_DRILLDOWN`` unset /
     not-"true" returns ``{"enabled": false}`` with ZERO boto3 calls;
  2. QUERY CONSTRUCTION — both Insights queries are generated from the
     attribute tables (query and parser cannot drift), and the session id is
     allowlist-validated before interpolation (no query injection);
  3. the TABLE-DRIVEN SPAN PARSER — fixture span documents in the shapes the
     ADOT auto-instrumentation can emit (nested attributes, flat dotted
     attribute keys, alternate gen_ai.* usage names, missing tokens, error
     status, end-time-only duration);
  4. ERROR PATHS — Insights failures (start, poll, terminal status,
     exhaustion) surface HONEST errors with empty rows, never fabricated data;
  5. SPAN SOURCES — spans are read from ``aws/spans`` AND the ``spans`` stream
     of the log group of each ADT runtime the control plane lists now (never
     another application's AgentCore runtime, never a retired ADT runtime's
     leftover group), in exactly ONE StartQuery per request; more sources
     than one query can read, a failed runtime listing, a failed discovery or
     an unreadable group is an honest error, never a partial answer;
  6. BOUNDED DISCOVERY — working out the sources costs at most 10
     ListAgentRuntimes pages of 100 and one DescribeLogGroups by name, however
     many runtimes or pages the account holds.

No AWS, no network: the logs client is a fake; the socket-blocked lane
enforces zero leakage.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agents._insights as ins
import agents.agents_trace_routes as atr


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _admin_request(is_admin: bool = True):
    return SimpleNamespace(state=SimpleNamespace(is_admin=is_admin))


RUNTIMES = "/aws/bedrock-agentcore/runtimes/"
RUNTIME_ORCH_ID = "adt_orchestrator-AbC123"
RUNTIME_MES_ID = "adt_mes-DeF456"
RUNTIME_ORCH = f"{RUNTIMES}{RUNTIME_ORCH_ID}-DEFAULT"
RUNTIME_MES = f"{RUNTIMES}{RUNTIME_MES_ID}-DEFAULT"
#: Another application's AgentCore runtime in the same account and Region.
FOREIGN_RUNTIME_ID = "customer_otherapp-AbC"
FOREIGN_GROUP = f"{RUNTIMES}{FOREIGN_RUNTIME_ID}-DEFAULT"
#: The log group a deleted (retired) ADT runtime left behind.
RETIRED_GROUP = f"{RUNTIMES}adt_mes-Old999-DEFAULT"


def _runtime(runtime_id: str) -> dict:
    """A ``list_agent_runtimes`` summary (the id is ``<name>-<suffix>``)."""
    return {"agentRuntimeName": runtime_id.rsplit("-", 1)[0],
            "agentRuntimeId": runtime_id, "status": "READY"}


class FakeControl:
    """``bedrock-agentcore-control`` double: lists ``runtimes`` over two
    pages (so the listing must follow ``nextToken``), or raises ``exc``."""

    def __init__(self, runtimes=(), exc=None):
        self.runtimes = list(runtimes)
        self.exc = exc
        self.calls: list[dict] = []

    def list_agent_runtimes(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        half = len(self.runtimes) // 2
        if kwargs.get("nextToken") == "page-2":
            return {"agentRuntimes": self.runtimes[half:]}
        if half:
            return {"agentRuntimes": self.runtimes[:half], "nextToken": "page-2"}
        return {"agentRuntimes": self.runtimes}


def _emulate_insights(query: str, rows: list) -> list:
    """What ONE Logs Insights query over several groups returns: a
    ``stats ... by session_id`` aggregates every group's rows per session,
    then ``sort`` and ``limit`` apply once to the whole result."""
    found = re.search(r"\| limit (\d+)", query)
    limit = int(found.group(1)) if found else len(rows)
    if "stats count(*) as span_count" not in query:
        return rows[:limit]
    merged: dict[str, dict] = {}
    for row in rows:
        r = ins.parse_row(row)
        if not r.get("session_id"):   # the query's `filter ispresent(session_id)`
            continue
        cur = merged.setdefault(r["session_id"], {
            "session_id": r["session_id"], "span_count": 0,
            "first_ts": r["first_ts"], "last_ts": r["last_ts"]})
        cur["span_count"] += int(r["span_count"])
        cur["first_ts"] = min(cur["first_ts"], r["first_ts"])
        cur["last_ts"] = max(cur["last_ts"], r["last_ts"])
    ordered = sorted(merged.values(), key=lambda s: s["last_ts"], reverse=True)
    return [_insights_row(**{**s, "span_count": str(s["span_count"])})
            for s in ordered[:limit]]


class FakeLogs:
    """Minimal Logs Insights client double.

    ``groups`` maps each EXISTING log group to the rows a query over it
    returns. A query over one group returns that group's rows as given (the
    fixture already is Insights output); a query over several is aggregated
    across them the way Insights does (:func:`_emulate_insights`).
    By default only ``aws/spans`` exists, holding ``results``. A query naming
    a group that does not exist fails the way StartQuery does
    (ResourceNotFoundException); a group in ``denied`` fails AccessDenied.
    DescribeLogGroups answers by name, as measured live: names that do not
    exist are left out, 1 to 50 names per call; ``describe_next_token``
    makes it return a ``nextToken`` too.
    """

    def __init__(self, results=None, status="Complete",
                 start_exc=None, poll_exc=None, groups=None,
                 describe_exc=None, denied=(), describe_next_token=None):
        self.results = results or []
        self.groups = groups if groups is not None else {"aws/spans": self.results}
        self.status = status
        self.start_exc = start_exc
        self.poll_exc = poll_exc
        self.describe_exc = describe_exc
        self.describe_next_token = describe_next_token
        self.denied = set(denied)
        self.start_calls: list[dict] = []
        self.describe_calls: list[dict] = []
        self.poll_calls = 0
        self._rows: dict[str, list] = {}

    def _queried_groups(self, kwargs) -> list[str]:
        if "logGroupName" in kwargs:
            return [kwargs["logGroupName"]]
        return list(kwargs.get("logGroupNames") or [])

    def start_query(self, **kwargs):
        self.start_calls.append(kwargs)
        if self.start_exc:
            raise self.start_exc
        rows: list = []
        queried = self._queried_groups(kwargs)
        for group in queried:
            if group in self.denied:
                raise RuntimeError(f"AccessDeniedException: not authorized on {group}")
            if group not in self.groups:
                raise RuntimeError(f"ResourceNotFoundException: {group}")
            rows.extend(self.groups[group])
        if len(queried) > 1:
            rows = _emulate_insights(kwargs["queryString"], rows)
        qid = f"q-{len(self.start_calls)}"
        self._rows[qid] = rows
        return {"queryId": qid}

    def get_query_results(self, queryId):  # noqa: N803 - boto3 API casing
        self.poll_calls += 1
        if self.poll_exc:
            raise self.poll_exc
        return {"status": self.status, "results": self._rows.get(queryId, [])}

    def describe_log_groups(self, **kwargs):
        self.describe_calls.append(kwargs)
        if self.describe_exc:
            raise self.describe_exc
        names = kwargs.get("logGroupIdentifiers") or []
        if not 1 <= len(names) <= 50:   # botocore's own parameter bound
            raise RuntimeError(f"ParamValidationError: {len(names)} identifiers")
        resp = {"logGroups": [{"logGroupName": n} for n in sorted(names)
                              if n in self.groups]}
        if self.describe_next_token:
            resp["nextToken"] = self.describe_next_token
        return resp


def _wire(monkeypatch, fake_logs, enabled=True, runtimes=(), control=None):
    """Enable the gate and route _boto_session to the fake clients: the logs
    client and an AgentCore control client listing ``runtimes``."""
    monkeypatch.setenv("AGENTS_OBSERVABILITY_DRILLDOWN", "true" if enabled else "false")
    clients = {"logs": fake_logs,
               "bedrock-agentcore-control": control or FakeControl(runtimes)}
    session = SimpleNamespace(client=lambda name: clients[name])
    monkeypatch.setattr(atr, "_boto_session", lambda: session)
    return fake_logs


def _insights_row(**fields) -> list[dict]:
    return [{"field": k, "value": v} for k, v in fields.items()]


def _message_row(doc: dict) -> list[dict]:
    return _insights_row(**{"@timestamp": "2026-07-19 12:00:00.000",
                            "@message": json.dumps(doc)})


# ────────────────────────────────────────────────────────────────────────────
# 1. Feature gate — fast path, zero boto3
# ────────────────────────────────────────────────────────────────────────────

class TestFeatureGate:
    @pytest.mark.parametrize("value", [None, "", "false", "1", "TRUE", "yes"])
    def test_sessions_gated_off(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("AGENTS_OBSERVABILITY_DRILLDOWN", raising=False)
        else:
            monkeypatch.setenv("AGENTS_OBSERVABILITY_DRILLDOWN", value)
        monkeypatch.setattr(atr, "_boto_session",
                            lambda: pytest.fail("boto3 touched on gated-off path"))
        assert atr.list_trace_sessions(_admin_request()) == {"enabled": False}

    def test_detail_gated_off(self, monkeypatch):
        monkeypatch.delenv("AGENTS_OBSERVABILITY_DRILLDOWN", raising=False)
        monkeypatch.setattr(atr, "_boto_session",
                            lambda: pytest.fail("boto3 touched on gated-off path"))
        out = atr.get_trace_session("sess-1", _admin_request())
        assert out == {"enabled": False}

    def test_gate_off_skips_session_id_validation(self, monkeypatch):
        # Gate is checked FIRST — no work at all when off.
        monkeypatch.delenv("AGENTS_OBSERVABILITY_DRILLDOWN", raising=False)
        assert atr.get_trace_session('bad"id', _admin_request()) == {"enabled": False}

    def test_non_admin_forbidden(self, monkeypatch):
        monkeypatch.setenv("AGENTS_OBSERVABILITY_DRILLDOWN", "true")
        with pytest.raises(HTTPException) as exc:
            atr.list_trace_sessions(_admin_request(is_admin=False))
        assert exc.value.status_code == 403


# ────────────────────────────────────────────────────────────────────────────
# 2. Query construction (table-driven, injection-safe)
# ────────────────────────────────────────────────────────────────────────────

class TestQueryConstruction:
    def test_sessions_query_built_from_attribute_table(self):
        q = atr.build_sessions_query(limit=50)
        for path in atr.SESSION_ID_FIELDS:
            assert path in q
        assert "coalesce(" in q
        assert "filter ispresent(session_id)" in q
        assert "stats count(*) as span_count" in q
        assert "min(@timestamp) as first_ts" in q
        assert "max(@timestamp) as last_ts" in q
        assert "| limit 50" in q

    def test_detail_query_filters_every_candidate_field(self):
        q = atr.build_session_detail_query("sess-42", limit=1000)
        for path in atr.SESSION_ID_FIELDS:
            assert f'{path} = "sess-42"' in q
        assert "@message" in q
        assert "sort @timestamp asc" in q
        assert "| limit 1000" in q

    def test_route_queries_target_aws_spans_log_group(self, monkeypatch):
        # aws/spans-only account (older agents): one query, over aws/spans.
        logs = _wire(monkeypatch, FakeLogs(results=[]))
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        atr.list_trace_sessions(_admin_request())
        assert len(logs.start_calls) == 1
        assert logs.start_calls[0]["logGroupNames"] == ["aws/spans"]
        assert logs.start_calls[0]["queryString"] == atr.build_sessions_query()

    def test_queries_read_only_the_spans_stream_of_runtime_groups(self):
        # A runtime log group also holds the agent's application logs; only
        # its `spans` stream holds spans. aws/spans is all spans.
        source = 'filter @logStream = "spans" or @log like /:aws\\/spans$/'
        assert source in atr.build_sessions_query()
        assert source in atr.build_session_detail_query("sess-42")

    @pytest.mark.parametrize("bad", ['s"1', "s'1", "a b", "x|y", "s\\1", "", "🙂"])
    def test_invalid_session_id_rejected_before_query(self, monkeypatch, bad):
        logs = _wire(monkeypatch, FakeLogs())
        with pytest.raises(HTTPException) as exc:
            atr.get_trace_session(bad, _admin_request())
        assert exc.value.status_code == 400
        assert logs.start_calls == []          # never reached Insights

    def test_hours_clamped(self, monkeypatch):
        assert atr._clamp_hours(0) == 1.0
        assert atr._clamp_hours(24) == 24.0
        assert atr._clamp_hours(10_000) == atr._MAX_HOURS
        assert atr._clamp_hours("junk") == 24.0


# ────────────────────────────────────────────────────────────────────────────
# 3. Table-driven span parser (fixture spans)
# ────────────────────────────────────────────────────────────────────────────

class TestSpanParser:
    FROZEN_SPAN_KEYS = {"name", "start_ms", "duration_ms", "status",
                        "tokens_in", "tokens_out", "kind"}

    def test_model_span_flat_attribute_keys(self):
        # OTEL JSON flat-encodes dotted attribute names inside "attributes".
        doc = {
            "name": "chat claude",
            "startTimeUnixNano": 1_700_000_000_000_000_000,
            "durationNano": 2_500_000_000,
            "status": {"code": "OK"},
            "attributes": {
                "gen_ai.usage.input_tokens": 120,
                "gen_ai.usage.output_tokens": 45,
                "session.id": "sess-1",
            },
        }
        span = atr.parse_span(doc)
        assert set(span) == self.FROZEN_SPAN_KEYS
        assert span == {
            "name": "chat claude",
            "start_ms": 1_700_000_000_000,     # 1.7e18 ns → epoch ms
            "duration_ms": 2_500,
            "status": "OK",
            "tokens_in": 120,
            "tokens_out": 45,
            "kind": "model",
        }

    def test_model_span_nested_attributes(self):
        doc = {
            "name": "invoke_model",
            "startTimeUnixNano": "1700000000000000000",   # string numerics too
            "durationNano": "1000000",
            "status": {"code": 2},
            "attributes": {"gen_ai": {"usage": {"input_tokens": "7",
                                                "output_tokens": "3"}}},
        }
        span = atr.parse_span(doc)
        assert span["tokens_in"] == 7
        assert span["tokens_out"] == 3
        assert span["duration_ms"] == 1
        assert span["status"] == "ERROR"
        assert span["kind"] == "model"

    def test_alternate_token_attribute_names(self):
        doc = {"name": "llm", "attributes": {
            "gen_ai.usage.prompt_tokens": 11,
            "gen_ai.usage.completion_tokens": 22,
        }}
        span = atr.parse_span(doc)
        assert (span["tokens_in"], span["tokens_out"]) == (11, 22)

    def test_tool_span_gateway_name_pattern(self):
        doc = {"name": "erp___run_query", "startTimeUnixNano": 1e18,
               "durationNano": 5e8,
               "attributes": {"gen_ai.usage.input_tokens": 1}}
        span = atr.parse_span(doc)
        assert span["kind"] == "tool"          # tool wins over token presence

    def test_internal_span_missing_tokens(self):
        doc = {"name": "middleware", "startTimeUnixNano": 1e18}
        span = atr.parse_span(doc)
        assert span["tokens_in"] is None
        assert span["tokens_out"] is None
        assert span["kind"] == "internal"
        assert span["duration_ms"] is None     # honest: unknown, not 0
        assert span["status"] == "UNSET"

    def test_duration_falls_back_to_end_minus_start(self):
        doc = {"name": "s", "startTimeUnixNano": 1_000_000_000,
               "endTimeUnixNano": 4_000_000_000}
        assert atr.parse_span(doc)["duration_ms"] == 3000   # 3e9 ns → ms

    def test_status_code_string_variants(self):
        assert atr.parse_span({"name": "s", "status": {"code": "STATUS_CODE_OK"}})["status"] == "OK"
        assert atr.parse_span({"name": "s", "statusCode": "Error"})["status"] == "ERROR"
        assert atr.parse_span({"name": "s"})["status"] == "UNSET"

    def test_attribute_tables_are_gen_ai_data_not_code(self):
        # The deployed-env-confirmable contract: renaming an attribute is a
        # TABLE edit. Guard the tables' shape so the seam stays data-driven.
        for path in atr.TOKENS_IN_FIELDS + atr.TOKENS_OUT_FIELDS:
            assert path.startswith("attributes.gen_ai.usage.")
        assert all(isinstance(p, str) for p in atr.SESSION_ID_FIELDS)
        for path, factor in atr.START_TIME_FIELDS + atr.END_TIME_FIELDS + atr.DURATION_FIELDS:
            assert isinstance(path, str) and factor > 0


# ────────────────────────────────────────────────────────────────────────────
# 4. Routes — happy paths against canned Insights fixtures
# ────────────────────────────────────────────────────────────────────────────

class TestSessionsRoute:
    def test_sessions_list_shape(self, monkeypatch):
        rows = [
            _insights_row(session_id="sess-b", span_count="12",
                          first_ts="2026-07-19 11:00:00.000",
                          last_ts="2026-07-19 11:05:30.500"),
            _insights_row(session_id="sess-a", span_count="3",
                          first_ts="2026-07-19 10:00:00",
                          last_ts="2026-07-19 10:00:20"),
        ]
        _wire(monkeypatch, FakeLogs(results=rows))
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request(), hours=24)
        assert out["enabled"] is True
        assert "error" not in out
        assert [s["session_id"] for s in out["sessions"]] == ["sess-b", "sess-a"]
        first = out["sessions"][0]
        assert set(first) == {"session_id", "start_ms", "end_ms",
                              "last_seen_ms", "span_count"}
        assert first["span_count"] == 12
        assert first["last_seen_ms"] == first["end_ms"]
        assert first["end_ms"] - first["start_ms"] == 330_500   # 5m30.5s window

    def test_rows_without_session_id_skipped(self, monkeypatch):
        rows = [_insights_row(span_count="5"),
                _insights_row(session_id="s1", span_count="1")]
        _wire(monkeypatch, FakeLogs(results=rows))
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert [s["session_id"] for s in out["sessions"]] == ["s1"]


class TestDetailRoute:
    def test_frozen_detail_schema_and_totals(self, monkeypatch):
        docs = [
            {"name": "orchestrator turn", "startTimeUnixNano": 2e18,
             "durationNano": 9e9, "status": {"code": "OK"},
             "attributes": {"session.id": "sess-1"}},
            {"name": "chat claude", "startTimeUnixNano": 1e18,
             "durationNano": 2e9, "status": {"code": "OK"},
             "attributes": {"gen_ai.usage.input_tokens": 100,
                            "gen_ai.usage.output_tokens": 40}},
            {"name": "mes___run_query", "startTimeUnixNano": 1.5e18,
             "durationNano": 1e9, "status": {"code": "ERROR"},
             "attributes": {}},
        ]
        _wire(monkeypatch, FakeLogs(results=[_message_row(d) for d in docs]))
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.get_trace_session("sess-1", _admin_request(), hours=24)

        assert set(out) == {"enabled", "session_id", "spans", "totals"}
        assert out["enabled"] is True
        assert out["session_id"] == "sess-1"
        # Ordered by start time (fixture arrived out of order).
        assert [s["name"] for s in out["spans"]] == [
            "chat claude", "mes___run_query", "orchestrator turn"]
        assert out["totals"] == {"tokens_in": 100, "tokens_out": 40,
                                 "tool_calls": 1}
        for span in out["spans"]:
            assert set(span) == TestSpanParser.FROZEN_SPAN_KEYS

    def test_non_json_messages_skipped(self, monkeypatch):
        rows = [_insights_row(**{"@message": "plain text, not a span"}),
                _insights_row(**{"@message": '["a","list"]'}),
                _message_row({"name": "real", "startTimeUnixNano": 1e18})]
        _wire(monkeypatch, FakeLogs(results=rows))
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.get_trace_session("sess-1", _admin_request())
        assert [s["name"] for s in out["spans"]] == ["real"]


# ────────────────────────────────────────────────────────────────────────────
# 4b. Span sources — aws/spans AND each runtime log group's `spans` stream
# ────────────────────────────────────────────────────────────────────────────

def _session_row(session_id, count, first, last):
    return _insights_row(session_id=session_id, span_count=str(count),
                         first_ts=first, last_ts=last)


class TestSpanSources:
    """AgentCore's unified span destination writes a runtime's spans to the
    `spans` stream of its OWN log group; older agents keep aws/spans. Only
    THIS deployment's live ADT runtimes are read, in one query."""

    def test_sources_are_aws_spans_and_live_adt_runtime_groups(self):
        logs = FakeLogs(groups={
            "aws/spans": [], "aws/spans-archive": [],
            RUNTIME_ORCH: [], RUNTIME_MES: [], FOREIGN_GROUP: [],
            RETIRED_GROUP: [], "/aws/lambda/unrelated": [],
        })
        control = FakeControl([_runtime(RUNTIME_ORCH_ID), _runtime(RUNTIME_MES_ID),
                               _runtime(FOREIGN_RUNTIME_ID)])
        assert atr.span_log_groups(logs, control) == ["aws/spans", RUNTIME_MES, RUNTIME_ORCH]
        # Existence is checked by name in one call, never a prefix walk.
        assert logs.describe_calls == [
            {"logGroupIdentifiers": ["aws/spans", RUNTIME_MES, RUNTIME_ORCH]}]
        assert control.calls == [{"maxResults": 100},
                                 {"maxResults": 100, "nextToken": "page-2"}]

    def test_foreign_agentcore_group_is_never_queried(self, monkeypatch):
        # Another application's runtime shares the account: the control
        # plane lists it and its log group exists, but it is not ADT's.
        foreign_span = {"name": "other app span", "startTimeUnixNano": 1e18,
                        "attributes": {"session.id": "sess-1"}}
        logs = _wire(monkeypatch, FakeLogs(groups={
            "aws/spans": [],
            RUNTIME_ORCH: [_session_row("sess-1", 2, "2026-10-22 09:00:00",
                                        "2026-10-22 09:01:00")],
            FOREIGN_GROUP: [_session_row("other-app-session", 9, "2026-10-22 09:30:00",
                                         "2026-10-22 09:40:00"),
                            _message_row(foreign_span)],
        }), runtimes=[_runtime(RUNTIME_ORCH_ID), _runtime(FOREIGN_RUNTIME_ID)])
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert [s["session_id"] for s in out["sessions"]] == ["sess-1"]
        detail = atr.get_trace_session("sess-1", _admin_request())
        assert "other app span" not in [s["name"] for s in detail["spans"]]
        for call in logs.start_calls:
            assert FOREIGN_GROUP not in call["logGroupNames"]

    def test_retired_adt_group_is_never_queried(self, monkeypatch):
        # A deleted ADT runtime's log group outlives it; the control plane no
        # longer lists that runtime, so its leftover spans are not read.
        logs = _wire(monkeypatch, FakeLogs(groups={
            "aws/spans": [],
            RUNTIME_ORCH: [_session_row("sess-live", 1, "2026-10-22 09:00:00",
                                        "2026-10-22 09:00:01")],
            RETIRED_GROUP: [_session_row("sess-retired", 5, "2026-10-22 08:00:00",
                                         "2026-10-22 08:10:00")],
        }), runtimes=[_runtime(RUNTIME_ORCH_ID)])
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert [s["session_id"] for s in out["sessions"]] == ["sess-live"]
        assert logs.start_calls[0]["logGroupNames"] == ["aws/spans", RUNTIME_ORCH]

    def test_live_runtime_without_a_log_group_yet_is_skipped(self, monkeypatch):
        # A runtime has no log group until its first invocation; StartQuery
        # would fail on the missing group, so it is left out, not an error.
        logs = _wire(monkeypatch, FakeLogs(groups={"aws/spans": [], RUNTIME_ORCH: []}),
                     runtimes=[_runtime(RUNTIME_ORCH_ID), _runtime(RUNTIME_MES_ID)])
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert out == {"enabled": True, "sessions": []}
        assert logs.start_calls[0]["logGroupNames"] == ["aws/spans", RUNTIME_ORCH]

    def test_runtime_listing_failure_is_an_honest_error(self, monkeypatch):
        # Never fall back to aws/spans alone (or to every runtime group).
        logs = _wire(monkeypatch, FakeLogs(groups={
            "aws/spans": [_session_row("s1", 1, "2026-10-22 09:00:00",
                                       "2026-10-22 09:00:01")],
            RUNTIME_ORCH: []}),
            control=FakeControl(exc=RuntimeError(
                "AccessDeniedException: bedrock-agentcore:ListAgentRuntimes")))
        out = atr.list_trace_sessions(_admin_request())
        assert out["sessions"] == []
        assert out["error_category"] == "upstream_error"
        assert "Reference: " in out["error"] and "AccessDenied" not in out["error"]
        detail = atr.get_trace_session("s1", _admin_request())
        assert detail["spans"] == [] and "Reference: " in detail["error"]
        assert logs.start_calls == []

    def test_sessions_found_in_runtime_group_spans_stream(self, monkeypatch):
        # Unified destination: aws/spans holds nothing for this agent.
        logs = _wire(monkeypatch, FakeLogs(groups={
            "aws/spans": [],
            RUNTIME_ORCH: [_session_row("sess-new", 499, "2026-10-22 09:00:00",
                                        "2026-10-22 09:59:00")],
        }), runtimes=[_runtime(RUNTIME_ORCH_ID)])
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert "error" not in out
        assert [s["session_id"] for s in out["sessions"]] == ["sess-new"]
        assert out["sessions"][0]["span_count"] == 499
        assert logs.start_calls[0]["logGroupNames"] == ["aws/spans", RUNTIME_ORCH]

    def test_runtime_groups_without_aws_spans_group(self, monkeypatch):
        # An account with no aws/spans group at all is not an error: there
        # are simply no spans there.
        logs = _wire(monkeypatch, FakeLogs(groups={
            RUNTIME_ORCH: [_session_row("s1", 3, "2026-10-22 09:00:00",
                                        "2026-10-22 09:01:00")],
        }), runtimes=[_runtime(RUNTIME_ORCH_ID)])
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert "error" not in out
        assert [s["session_id"] for s in out["sessions"]] == ["s1"]
        assert logs.start_calls[0]["logGroupNames"] == [RUNTIME_ORCH]

    def test_sessions_merged_across_both_sources(self, monkeypatch):
        # ONE query over both sources: Insights' `stats ... by session_id`
        # merges a session across groups before the newest-50 limit applies.
        logs = _wire(monkeypatch, FakeLogs(groups={
            "aws/spans": [
                _session_row("sess-old", 4, "2026-10-21 08:00:00", "2026-10-21 08:05:00"),
                _session_row("sess-both", 2, "2026-10-22 07:00:00", "2026-10-22 07:10:00"),
            ],
            RUNTIME_ORCH: [
                _session_row("sess-both", 10, "2026-10-22 07:05:00", "2026-10-22 07:30:00"),
                _session_row("sess-new", 7, "2026-10-22 09:00:00", "2026-10-22 09:02:00"),
            ],
        }), runtimes=[_runtime(RUNTIME_ORCH_ID)])
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert len(logs.start_calls) == 1
        by_id = {s["session_id"]: s for s in out["sessions"]}
        # Newest activity first, one row per session.
        assert [s["session_id"] for s in out["sessions"]] == [
            "sess-new", "sess-both", "sess-old"]
        both = by_id["sess-both"]
        assert both["span_count"] == 12
        assert both["start_ms"] == atr._ts_to_ms("2026-10-22 07:00:00")
        assert both["end_ms"] == both["last_seen_ms"] == atr._ts_to_ms("2026-10-22 07:30:00")

    def test_detail_merges_spans_from_both_sources(self, monkeypatch):
        old = {"name": "old agent span", "startTimeUnixNano": 1e18,
               "attributes": {"session.id": "sess-1"}}
        new = {"name": "chat claude", "startTimeUnixNano": 2e18,
               "attributes": {"session.id": "sess-1",
                              "gen_ai.usage.input_tokens": 50,
                              "gen_ai.usage.output_tokens": 5},
               "resource": {"attributes": {"service.name": "adt_orchestrator.DEFAULT"}}}
        logs = _wire(monkeypatch, FakeLogs(groups={
            "aws/spans": [_message_row(old)], RUNTIME_ORCH: [_message_row(new)]}),
            runtimes=[_runtime(RUNTIME_ORCH_ID)])
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.get_trace_session("sess-1", _admin_request())
        assert "error" not in out
        assert [s["name"] for s in out["spans"]] == ["old agent span", "chat claude"]
        assert out["totals"] == {"tokens_in": 50, "tokens_out": 5, "tool_calls": 0}
        assert len(logs.start_calls) == 1
        assert logs.start_calls[0]["logGroupNames"] == ["aws/spans", RUNTIME_ORCH]
        assert logs.start_calls[0]["queryString"] == atr.build_session_detail_query("sess-1")

    def test_no_span_log_groups_is_an_empty_state_without_a_query(self, monkeypatch):
        # A live runtime that was never invoked has no log group yet.
        logs = _wire(monkeypatch, FakeLogs(groups={"/aws/lambda/x": []}),
                     runtimes=[_runtime(RUNTIME_ORCH_ID)])
        out = atr.list_trace_sessions(_admin_request())
        assert out == {"enabled": True, "sessions": []}
        assert logs.start_calls == []
        detail = atr.get_trace_session("sess-1", _admin_request())
        assert detail["spans"] == [] and "error" not in detail
        assert logs.start_calls == []

    def test_discovery_failure_is_an_honest_error(self, monkeypatch):
        # DescribeLogGroups denied: never fall back to aws/spans alone, which
        # would hide the runtime groups behind a quiet-looking result.
        logs = _wire(monkeypatch, FakeLogs(
            results=[_session_row("s1", 1, "2026-10-22 09:00:00", "2026-10-22 09:00:01")],
            describe_exc=RuntimeError("AccessDeniedException: logs:DescribeLogGroups")),
            runtimes=[_runtime(RUNTIME_ORCH_ID)])
        out = atr.list_trace_sessions(_admin_request())
        assert out["sessions"] == []
        assert out["error_category"] == "upstream_error"
        assert "Reference: " in out["error"] and "AccessDenied" not in out["error"]
        assert logs.start_calls == []
        detail = atr.get_trace_session("s1", _admin_request())
        assert detail["spans"] == [] and "Reference: " in detail["error"]

    def test_unreadable_runtime_group_is_an_honest_error(self, monkeypatch):
        # StartQuery denied on the runtime group: no partial aws/spans answer.
        _wire(monkeypatch, FakeLogs(
            groups={"aws/spans": [_session_row("s-old", 1, "2026-10-22 09:00:00",
                                               "2026-10-22 09:00:01")],
                    RUNTIME_ORCH: []},
            denied={RUNTIME_ORCH}), runtimes=[_runtime(RUNTIME_ORCH_ID)])
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert out["sessions"] == []
        assert "Reference: " in out["error"] and "AccessDenied" not in out["error"]

    @staticmethod
    def _adt_fleet(count: int) -> tuple[dict, list[dict]]:
        """aws/spans plus ``count`` live ADT runtimes, each with a log group."""
        ids = [f"adt_sys{i:02d}-X{i:02d}" for i in range(count)]
        groups = {"aws/spans": [_session_row("s-shared", 1, "2026-10-22 08:00:00",
                                             "2026-10-22 08:00:01")]}
        groups.update({f"{RUNTIMES}{rid}-DEFAULT": [] for rid in ids})
        groups[f"{RUNTIMES}{ids[-1]}-DEFAULT"] = [
            _session_row("s-shared", 2, "2026-10-22 08:30:00", "2026-10-22 09:00:00")]
        return groups, [_runtime(rid) for rid in ids]

    def test_more_sources_than_one_query_reads_is_an_error_without_a_query(
            self, monkeypatch):
        # 50 runtime groups + aws/spans = 51 EXISTING sources: one StartQuery
        # takes at most 50, and splitting would multiply the scan work and
        # break the per-session counts, so the routes refuse instead.
        groups, runtimes = self._adt_fleet(ins.MAX_LOG_GROUPS_PER_QUERY)
        logs = _wire(monkeypatch, FakeLogs(groups=groups), runtimes=runtimes)
        out = atr.list_trace_sessions(_admin_request())
        assert out["sessions"] == []
        assert out["error_category"] == "upstream_error" and "Reference: " in out["error"]
        detail = atr.get_trace_session("s-shared", _admin_request())
        assert detail["spans"] == [] and "Reference: " in detail["error"]
        assert logs.start_calls == []

    def test_fifty_runtime_groups_are_read_when_aws_spans_is_absent(self, monkeypatch):
        # 50 runtime groups and no aws/spans = 50 sources: exactly what one
        # query reads, so the absent shared group must not take a slot.
        groups, runtimes = self._adt_fleet(ins.MAX_LOG_GROUPS_PER_QUERY)
        del groups["aws/spans"]
        logs = _wire(monkeypatch, FakeLogs(groups=groups), runtimes=runtimes)
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert "error" not in out
        assert out["sessions"][0]["session_id"] == "s-shared"
        assert len(logs.start_calls) == 1
        assert sorted(logs.start_calls[0]["logGroupNames"]) == sorted(groups)
        assert len(logs.describe_calls) <= 2

    def test_listed_runtime_without_a_log_group_takes_no_slot(self, monkeypatch):
        # 50 listed ADT runtimes, one never invoked (no log group yet), plus
        # aws/spans = 50 existing sources: readable in one query.
        groups, runtimes = self._adt_fleet(ins.MAX_LOG_GROUPS_PER_QUERY)
        del groups[f"{RUNTIMES}{runtimes[0]['agentRuntimeId']}-DEFAULT"]
        logs = _wire(monkeypatch, FakeLogs(groups=groups), runtimes=runtimes)
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert "error" not in out
        assert len(logs.start_calls) == 1
        assert sorted(logs.start_calls[0]["logGroupNames"]) == sorted(groups)
        assert len(logs.describe_calls) <= 2

    def test_more_listed_runtimes_than_one_query_reads_is_an_error(self, monkeypatch):
        # 51 listed ADT runtimes cannot fit one query even with no aws/spans:
        # refused during the listing, before any existence check or query.
        groups, runtimes = self._adt_fleet(ins.MAX_LOG_GROUPS_PER_QUERY + 1)
        del groups["aws/spans"]
        logs = _wire(monkeypatch, FakeLogs(groups=groups), runtimes=runtimes)
        out = atr.list_trace_sessions(_admin_request())
        assert out["sessions"] == [] and "Reference: " in out["error"]
        assert logs.describe_calls == [] and logs.start_calls == []

    def test_each_request_makes_exactly_one_start_query(self, monkeypatch):
        # 49 runtime groups + aws/spans = 50 sources: the most one query reads.
        groups, runtimes = self._adt_fleet(ins.MAX_LOG_GROUPS_PER_QUERY - 1)
        logs = _wire(monkeypatch, FakeLogs(groups=groups), runtimes=runtimes)
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert len(logs.start_calls) == 1
        assert sorted(logs.start_calls[0]["logGroupNames"]) == sorted(groups)
        assert out["sessions"][0]["session_id"] == "s-shared"
        assert out["sessions"][0]["span_count"] == 3
        atr.get_trace_session("s-shared", _admin_request())
        assert len(logs.start_calls) == 2
        assert "| limit 1000" in logs.start_calls[1]["queryString"]


class PagedControl:
    """``list_agent_runtimes`` over ``pages`` pages, the way the API pages:
    up to ``maxResults`` summaries a page (fewer when ``per_page`` is
    smaller, as the API may return) and a ``nextToken`` on every page but the
    last. Page ``p`` holds ``make(p, i)``; every call is recorded."""

    def __init__(self, pages, make, per_page=100):
        self.pages, self.make, self.per_page = pages, make, per_page
        self.calls: list[dict] = []

    def list_agent_runtimes(self, **kwargs):
        self.calls.append(kwargs)
        page = int(kwargs.get("nextToken") or 0)
        size = min(self.per_page, kwargs.get("maxResults", 10))
        resp = {"agentRuntimes": [self.make(page, i) for i in range(size)]}
        if page + 1 < self.pages:
            resp["nextToken"] = str(page + 1)
        return resp


def _other_app_runtime(page, i):
    return _runtime(f"otherapp_{page}_{i}-Z{page:04d}{i:03d}")


def _adt_runtime(page, i):
    return _runtime(f"adt_sys_{page}_{i}-Q{page:04d}{i:03d}")


class TestBoundedDiscovery:
    """Working out the span sources costs a fixed number of calls whatever
    the account holds: at most 10 ListAgentRuntimes pages of 100, at most two
    DescribeLogGroups by name (one when the candidates fit 50 names), then the
    one StartQuery. Past either bound the
    request is an honest error, never a partial source list."""

    PAGE_CAP = 10       # 10 pages of 100 = 1,000 runtimes
    PAGE_SIZE = 100

    def _assert_bounded_error(self, out):
        assert out["sessions"] == []
        assert out["error_category"] == "upstream_error"
        assert "Reference: " in out["error"]

    @pytest.mark.parametrize("pages", [11, 1_000])
    def test_runtime_listing_stops_at_the_page_cap(self, monkeypatch, pages):
        control = PagedControl(pages, _other_app_runtime)
        logs = _wire(monkeypatch, FakeLogs(), control=control)
        self._assert_bounded_error(atr.list_trace_sessions(_admin_request()))
        assert len(control.calls) == self.PAGE_CAP
        assert all(c["maxResults"] == self.PAGE_SIZE for c in control.calls)
        detail = atr.get_trace_session("sess-1", _admin_request())
        assert detail["spans"] == [] and "Reference: " in detail["error"]
        assert len(control.calls) == 2 * self.PAGE_CAP
        assert logs.describe_calls == [] and logs.start_calls == []

    @pytest.mark.parametrize("per_page, calls", [(100, 1), (10, 6), (7, 8)])
    def test_listing_stops_once_one_query_cannot_read_the_adt_groups(
            self, monkeypatch, per_page, calls):
        # 1,000 pages of ADT runtimes: the listing stops on the page where
        # the ADT groups alone first pass 50 (aws/spans may not exist, so it
        # takes no slot until the existence check).
        control = PagedControl(1_000, _adt_runtime, per_page=per_page)
        logs = _wire(monkeypatch, FakeLogs(), control=control)
        self._assert_bounded_error(atr.list_trace_sessions(_admin_request()))
        assert len(control.calls) == calls
        assert logs.describe_calls == [] and logs.start_calls == []

    def test_listing_that_ends_on_the_last_allowed_page_is_read(self, monkeypatch):
        def make(page, i):
            return _adt_runtime(page, i) if page == 9 and i < 2 else _other_app_runtime(page, i)
        adt = sorted(f"{RUNTIMES}{_adt_runtime(9, i)['agentRuntimeId']}-DEFAULT"
                     for i in range(2))
        control = PagedControl(self.PAGE_CAP, make)
        logs = _wire(monkeypatch, FakeLogs(groups={"aws/spans": [], **{g: [] for g in adt}}),
                     control=control)
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.list_trace_sessions(_admin_request())
        assert out == {"enabled": True, "sessions": []}
        assert len(control.calls) == self.PAGE_CAP
        assert logs.describe_calls == [{"logGroupIdentifiers": ["aws/spans", *adt]}]
        assert [c["logGroupNames"] for c in logs.start_calls] == [["aws/spans", *adt]]

    def test_existence_is_checked_in_one_call_by_name(self, monkeypatch):
        # 49 live ADT runtimes + aws/spans: the most one request reads.
        groups, runtimes = TestSpanSources._adt_fleet(ins.MAX_LOG_GROUPS_PER_QUERY - 1)
        logs = _wire(monkeypatch, FakeLogs(groups=groups), runtimes=runtimes)
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        atr.list_trace_sessions(_admin_request())
        assert len(logs.describe_calls) == 1
        names = logs.describe_calls[0]["logGroupIdentifiers"]
        assert names[0] == "aws/spans" and sorted(names) == sorted(groups)
        assert len(logs.start_calls) == 1

    def test_describe_next_token_is_an_honest_error(self, monkeypatch):
        logs = _wire(monkeypatch, FakeLogs(groups={"aws/spans": [], RUNTIME_ORCH: []},
                                           describe_next_token="more"),
                     runtimes=[_runtime(RUNTIME_ORCH_ID)])
        self._assert_bounded_error(atr.list_trace_sessions(_admin_request()))
        assert len(logs.describe_calls) == 1
        assert logs.start_calls == []


# ────────────────────────────────────────────────────────────────────────────
# 5. Error paths — honest errors, never fabricated data
# ────────────────────────────────────────────────────────────────────────────

class TestErrorPaths:
    def test_sessions_start_failure_returns_honest_error(self, monkeypatch):
        _wire(monkeypatch, FakeLogs(start_exc=RuntimeError("AccessDenied")))
        out = atr.list_trace_sessions(_admin_request())
        assert out["enabled"] is True
        assert out["sessions"] == []           # empty, not invented
        assert out["error_category"] == "upstream_error"
        assert "Reference: " in out["error"] and "AccessDenied" not in out["error"]

    def test_detail_terminal_failure_returns_honest_error(self, monkeypatch):
        _wire(monkeypatch, FakeLogs(status="Failed"))
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.get_trace_session("sess-1", _admin_request())
        assert out["enabled"] is True
        assert out["spans"] == []
        assert out["totals"] == {"tokens_in": 0, "tokens_out": 0, "tool_calls": 0}
        assert out["error_category"] == "upstream_error" and "Reference: " in out["error"]

    def test_detail_poll_exception_returns_honest_error(self, monkeypatch):
        _wire(monkeypatch, FakeLogs(poll_exc=RuntimeError("throttled")))
        monkeypatch.setattr(ins, "DEFAULT_POLL_INTERVAL_S", 0)
        out = atr.get_trace_session("sess-1", _admin_request())
        assert out["spans"] == [] and "Reference: " in out["error"]
        assert "throttled" not in out["error"]


# ────────────────────────────────────────────────────────────────────────────
# 6. Shared Insights runner (agents/_insights.py)
# ────────────────────────────────────────────────────────────────────────────

class TestRunInsights:
    def test_complete_returns_rows(self):
        rows = [[{"field": "a", "value": "1"}]]
        logs = FakeLogs(results=rows)
        out = ins.run_insights(logs, "aws/spans", "fields @message", 24,
                               max_polls=3, poll_interval_s=0, sleep=lambda _: None)
        assert out == rows
        assert logs.start_calls[0]["limit"] == 1000

    def test_start_failure_raises(self):
        logs = FakeLogs(start_exc=RuntimeError("no credentials"))
        with pytest.raises(ins.InsightsError, match="failed to start"):
            ins.run_insights(logs, "aws/spans", "q", 1, sleep=lambda _: None)

    @pytest.mark.parametrize("status", ["Failed", "Cancelled", "Timeout", "Unknown"])
    def test_terminal_status_raises(self, status):
        logs = FakeLogs(status=status)
        with pytest.raises(ins.InsightsError, match=f"ended {status}"):
            ins.run_insights(logs, "aws/spans", "q", 1,
                             max_polls=3, sleep=lambda _: None)

    def test_poll_exhaustion_raises_not_empty(self):
        logs = FakeLogs(status="Running")
        with pytest.raises(ins.InsightsError, match="did not complete"):
            ins.run_insights(logs, "aws/spans", "q", 1,
                             max_polls=2, sleep=lambda _: None)
        assert logs.poll_calls == 2

    def test_parse_row(self):
        assert ins.parse_row([{"field": "x", "value": "1"},
                              {"field": "y", "value": "2"}]) == {"x": "1", "y": "2"}

    def test_group_list_uses_log_group_names(self):
        logs = FakeLogs(groups={"aws/spans": [], RUNTIME_ORCH: []})
        ins.run_insights(logs, ["aws/spans", RUNTIME_ORCH], "q", 1,
                         max_polls=1, poll_interval_s=0, sleep=lambda _: None)
        assert logs.start_calls[0]["logGroupNames"] == ["aws/spans", RUNTIME_ORCH]
        assert "logGroupName" not in logs.start_calls[0]

    @pytest.mark.parametrize("count", [0, 51])
    def test_group_list_outside_start_query_limit_raises(self, count):
        logs = FakeLogs()
        names = [f"g{i}" for i in range(count)]
        with pytest.raises(ins.InsightsError, match="log groups"):
            ins.run_insights(logs, names, "q", 1, sleep=lambda _: None)
        assert logs.start_calls == []

    def test_existing_log_groups_keeps_requested_order_and_drops_missing(self):
        logs = FakeLogs(groups={RUNTIME_ORCH: [], RUNTIME_MES: [], "aws/spans": []})
        wanted = ["aws/spans", RUNTIME_ORCH, RETIRED_GROUP, RUNTIME_MES]
        assert ins.existing_log_groups(logs, wanted) == [
            "aws/spans", RUNTIME_ORCH, RUNTIME_MES]
        assert logs.describe_calls == [{"logGroupIdentifiers": wanted}]

    @pytest.mark.parametrize("count", [0, 51])
    def test_existing_log_groups_outside_identifier_limit_raises(self, count):
        logs = FakeLogs()
        with pytest.raises(ins.InsightsError, match="log groups"):
            ins.existing_log_groups(logs, [f"g{i}" for i in range(count)])
        assert logs.describe_calls == []

    def test_existing_log_groups_failure_raises(self):
        logs = FakeLogs(describe_exc=RuntimeError("AccessDeniedException"))
        with pytest.raises(ins.InsightsError, match="exist"):
            ins.existing_log_groups(logs, ["aws/spans"])

    def test_existing_log_groups_next_token_is_an_error_not_a_partial_answer(self):
        logs = FakeLogs(describe_next_token="more")
        with pytest.raises(ins.InsightsError, match="nextToken"):
            ins.existing_log_groups(logs, ["aws/spans"])
        assert len(logs.describe_calls) == 1
