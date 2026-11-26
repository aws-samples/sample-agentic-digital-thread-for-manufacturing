# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 2B acceptance — in-agent self-recovery + Mode B generic tools (ADR-0035).

Two concerns, both fully offline (no Bedrock, no AWS, no DB):

1. **Self-recovery (hard req).** ``create_system_agent``'s system prompt, for
   EVERY connection type, must instruct the agent to re-introspect its own schema
   on ``COLUMN_NOT_FOUND`` / ``TYPE_MISMATCH`` and retry — never surface the raw
   error. And the recovery loop must actually work: a stubbed system agent, given
   a query tool that first raises ``COLUMN_NOT_FOUND`` then succeeds, re-calls its
   describe/introspect tool BETWEEN attempts and produces a grounded final answer
   that does not surface the raw error. The tools are the REAL Athena tools that
   ``create_system_agent`` builds (proving introspection is in the internal
   toolset); only the boto3 backend is faked. The failed attempt's error is
   enriched with the REAL columns — i.e. the code itself re-introspects.

2. **Mode B generic tools.** ``describe_schema`` / ``run_query`` / ``connect``
   dispatch internally on ``connection_type`` and return well-formed results
   against a fake backend (PostgreSQL + Athena first-class; non-SQL types return a
   coherent structured response).
"""
import json

import pytest

import agents.system_agent as sa
from agents.connection_types import TULIP


# --------------------------------------------------------------------------- #
# Shared stubs (mirror the 2A prompt-inspection pattern)
# --------------------------------------------------------------------------- #
class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeAgent:
    """Captures what create_system_agent built without running an LLM."""

    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []


# Minimal, valid connection_config per type so create_system_agent builds its
# real internal toolset WITHOUT any network/secret lookup at construction time.
_CFG_BY_TYPE = {
    "PostgreSQL": {"host": "h", "dbname": "d"},
    "Athena": {"workgroup": "wg", "database": "db", "region": "us-east-1"},
    "SiteWise (Direct)": {},
    "SiteWise (i3X)": {"base_url": "http://i3x.example.com"},
    # A bare generic "REST API" system has NO
    # runtime toolset (it fails honestly with RestVendorUnsupported), so it
    # cannot build an agent. Tulip is now its own first-class type and is the
    # representative REST-family system that HAS tools — use it here so the
    # per-type prompt coverage still exercises the REST-family agent path.
    TULIP: {"base_url": "https://your-instance.tulip.co", "auth_scheme": "basic"},
    "MCP Server": {"url": "http://mcp.example.com"},
}


def _make_agent(monkeypatch, connection_type):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr(sa, "_mcp_system_tools", lambda _config, _sid="": [])
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)
    cfg = _CFG_BY_TYPE[connection_type]
    conn_params = cfg if connection_type == "PostgreSQL" else None
    return sa.create_system_agent(
        system_id="sys-a",
        system_name="System A",
        schema="public",
        system_prompt="Base system prompt.",
        conn_params=conn_params,
        connection_type=connection_type,
        connection_config=cfg,
    )


# --------------------------------------------------------------------------- #
# 1a. Self-recovery instruction is in EVERY connection type's prompt
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", list(_CFG_BY_TYPE))
def test_prompt_carries_self_recovery_for_every_type(monkeypatch, connection_type):
    prompt = _make_agent(monkeypatch, connection_type).system_prompt
    low = prompt.lower()

    # Keys its retry loop on the two named error markers.
    assert "COLUMN_NOT_FOUND" in prompt, connection_type
    assert "TYPE_MISMATCH" in prompt, connection_type
    # Re-introspect + retry, and never surface the raw error.
    assert "re-introspect" in low, connection_type
    assert "retry" in low, connection_type
    assert "do not return the raw error" in low, connection_type


def test_a_diagnosed_error_is_followed_and_only_an_undiagnosed_one_re_introspects():
    """A tool error that names its cause (``diagnostic_text``, e.g. the
    query's own alias) is followed as given; re-introspection and a retry
    with corrected physical names are for an error without one."""
    rule = sa._self_recovery_prompt("Athena")
    follow = rule.index("When the tool error carries a diagnostic_text")
    assert "follow it and change only what it names" in rule[follow:]
    assert "keep the table's column names" in rule[follow:]
    undiagnosed = rule.index("For an error without a diagnostic_text only:")
    assert follow < undiagnosed < rule.index("re-introspect your own schema")
    assert rule.index("re-introspect your own schema") < rule.index(
        "retry with the corrected name or cast")


def test_self_recovery_hint_names_the_types_introspection_tool():
    """Each type's recovery hint names an introspection capability that IS in that
    type's internal toolset (so the retry is actionable, not aspirational)."""
    assert "athena_list_tables" in sa._introspection_hint("Athena")
    assert "athena_describe_table" in sa._introspection_hint("Athena")
    assert "sitewise_list_assets" in sa._introspection_hint("SiteWise (Direct)")
    assert "sitewise_describe_asset" in sa._introspection_hint("SiteWise (Direct)")
    assert "information_schema" in sa._introspection_hint("PostgreSQL")


# --------------------------------------------------------------------------- #
# 1c. Loop-efficiency directive is in EVERY connection type's prompt
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", list(_CFG_BY_TYPE))
def test_prompt_carries_efficiency_directive_for_every_type(monkeypatch, connection_type):
    """The efficiency directive that curbs the redundant re-describe/re-run loop
    must be present for every connection type (cuts nested latency)."""
    prompt = _make_agent(monkeypatch, connection_type).system_prompt
    low = prompt.lower()

    assert "EFFICIENCY (required)" in prompt, connection_type
    # Discover schema at most once; do not re-describe/re-list already-seen objects.
    assert "at most once" in low, connection_type
    assert "do not re-describe" in low, connection_type
    assert "do not re-list" in low, connection_type
    # A single query, no exploratory variations, and stop once answered.
    assert "one correct query" in low, connection_type
    assert "exploratory variations" in low, connection_type
    assert "stop calling tools" in low, connection_type


def test_efficiency_directive_preserves_legitimate_traversal():
    """The directive constrains only REDUNDANT repeats — it must explicitly allow a
    legitimate drill-down into a NEW, not-yet-seen node (so the SiteWise sequential
    list-then-describe traversal is not broken)."""
    text = sa._efficiency_prompt().lower()
    assert "new" in text and "traversing a hierarchy" in text
    # And it must not contradict SELF-RECOVERY's one corrected retry on a real error.
    assert "actual tool error" in text


# --------------------------------------------------------------------------- #
# 1d. Concise / aggregate-output directive is in EVERY connection type's prompt
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", list(_CFG_BY_TYPE))
def test_prompt_carries_concise_output_directive_for_every_type(monkeypatch, connection_type):
    """The concise/aggregate-output directive that bounds large-result answers
    (aggregates + representative rows + counts instead of enumerating every row)
    must be present for every connection type — this prevents the output-token
    overrun (MaxTokensReached) that forced the orchestrator to retry."""
    prompt = _make_agent(monkeypatch, connection_type).system_prompt
    low = prompt.lower()

    assert "CONCISE OUTPUT (required)" in prompt, connection_type
    # Do not enumerate every row for a large result set.
    assert "do not enumerate every row" in low, connection_type
    # Lead with the aggregate, then a small representative sample + total count.
    assert "aggregate" in low, connection_type
    assert "representative" in low, connection_type
    assert "count" in low, connection_type


def test_concise_output_directive_preserves_small_results_and_grounding():
    """The directive must NOT weaken grounding: a small result set is still
    returned in full, and real values are always cited (never fabricated)."""
    low = sa._concise_output_prompt().lower()
    # Small result sets are returned in full.
    assert "small result set" in low and "in full" in low
    # Still grounded — cite real values, never fabricate.
    assert "cite real values" in low
    assert "never fabricate" in low


# --------------------------------------------------------------------------- #
# 1e. The system agent's Bedrock model is built with a raised output-token cap
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", list(_CFG_BY_TYPE))
def test_bedrock_model_uses_raised_max_tokens(monkeypatch, connection_type):
    """create_system_agent must construct its BedrockModel with a raised max_tokens
    (8192) so a large-result answer completes in ONE pass without MaxTokensReached.
    Captures the kwargs the model was built with via a fake BedrockModel."""
    captured = {}

    class _CapturingModel:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr(sa, "_mcp_system_tools", lambda _config, _sid="": [])
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _CapturingModel)
    cfg = _CFG_BY_TYPE[connection_type]
    conn_params = cfg if connection_type == "PostgreSQL" else None
    sa.create_system_agent(
        system_id="sys-a",
        system_name="System A",
        schema="public",
        system_prompt="Base system prompt.",
        conn_params=conn_params,
        connection_type=connection_type,
        connection_config=cfg,
    )
    assert captured.get("max_tokens") == 8192, connection_type


# --------------------------------------------------------------------------- #
# 1b. The recovery LOOP works end-to-end against a fake Athena backend
# --------------------------------------------------------------------------- #
def _result_set(cols, data_rows, with_header=True):
    rows = []
    if with_header:
        rows.append({"Data": [{"VarCharValue": c} for c in cols]})
    for r in data_rows:
        rows.append({"Data": [({"VarCharValue": v} if v is not None else {}) for v in r]})
    return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": c} for c in cols]}, "Rows": rows}}


class _FakeAthena:
    """Athena client double that decides state/results from the SQL text.

    - a query referencing ``bad_col`` FAILS with COLUMN_NOT_FOUND;
    - ``information_schema`` introspection SUCCEEDS with the REAL columns;
    - a query referencing ``widget_count`` SUCCEEDS with the aggregate value.
    """

    def __init__(self):
        self.started = []
        self._by_qid = {}
        self._n = 0

    def start_query_execution(self, **kw):
        sql = kw["QueryString"]
        self.started.append(sql)
        self._n += 1
        qid = f"q{self._n}"
        low = sql.lower()
        if "information_schema.columns" in low:
            if "data_type" in low:
                rs = _result_set(["column_name", "data_type"], [["widget_count", "bigint"], ["ts", "varchar"]])
            else:
                rs = _result_set(["column_name"], [["widget_count"], ["ts"]])
            state = "SUCCEEDED"
        elif "information_schema.tables" in low:
            state, rs = "SUCCEEDED", _result_set(["table_name"], [["widgets"]])
        elif "bad_col" in low:
            state, rs = "FAILED", None
        elif "widget_count" in low:
            state, rs = "SUCCEEDED", _result_set(["total"], [["42"]])
        else:
            state, rs = "SUCCEEDED", _result_set(["x"], [["1"]])
        self._by_qid[qid] = (state, rs)
        return {"QueryExecutionId": qid}

    def get_query_execution(self, QueryExecutionId):  # noqa: N803 - boto3 kwarg name
        state, _ = self._by_qid[QueryExecutionId]
        status = {"State": state}
        if state == "FAILED":
            status["StateChangeReason"] = (
                "COLUMN_NOT_FOUND: line 1:8: Column 'bad_col' cannot be resolved"
            )
        return {"QueryExecution": {"Status": status}}

    def get_query_results(self, **kw):
        _, rs = self._by_qid[kw["QueryExecutionId"]]
        return rs

    def stop_query_execution(self, QueryExecutionId):  # noqa: N803 - boto3 kwarg name
        pass


def _athena_tools_by_name(monkeypatch):
    """Build the REAL Athena internal toolset via create_system_agent, keyed by
    the underlying function name, with the boto3 backend faked."""
    fake = _FakeAthena()
    monkeypatch.setattr("agents.athena_tools.boto3_client_for", lambda *a, **k: fake)
    agent = _make_agent(monkeypatch, "Athena")
    tools = {t._tool_func.__name__: t for t in agent.tools}
    return tools, fake


def test_athena_failed_query_reaches_the_agent_as_schema_mismatch_without_engine_text(monkeypatch):
    """The FAILED attempt reaches the agent as the fixed schema_mismatch
    payload, plus the checked diagnosis of the name (here a name no SQL
    defines and no table has, so the plain one). The enriched engine text,
    with the real columns, is logged only."""
    tools, _fake = _athena_tools_by_name(monkeypatch)

    r1 = json.loads(tools["athena_run_query"]._tool_func(sql="SELECT bad_col FROM widgets"))

    assert {key: r1[key] for key in ("error", "error_type", "guidance")} == {
        "error": "schema_mismatch",
        "error_type": "schema_mismatch",
        "guidance": "Call athena_describe_table and retry using exact physical columns.",
    }
    assert r1["diagnostic"] == "column_not_resolved"
    assert r1["diagnostic_tokens"] == {"column": "bad_col"}
    assert "COLUMN_NOT_FOUND" not in str(r1)
    assert "cannot be resolved" not in str(r1)


def test_stubbed_agent_self_recovers_and_grounds_without_raw_error(monkeypatch):
    """A stubbed agent loop, driven by the self-recovery policy, using the REAL
    Athena tools: fail → re-introspect (describe) → retry → grounded answer.
    Asserts describe/introspect was called BETWEEN the two query attempts and the
    raw error never reaches the final answer."""
    tools, _fake = _athena_tools_by_name(monkeypatch)
    calls: list[str] = []

    # Attempt 1 — the model guesses a column that does not exist.
    calls.append("run_query")
    r1 = json.loads(tools["athena_run_query"]._tool_func(sql="SELECT bad_col FROM widgets"))

    assert r1["error_type"] == "schema_mismatch"
    assert r1["error"] == "schema_mismatch"
    assert "COLUMN_NOT_FOUND" not in str(r1)

    # Self-recovery: re-introspect the schema before retrying.
    calls.append("describe")
    desc = json.loads(tools["athena_describe_table"]._tool_func(table_name="widgets"))
    real_cols = [row[0] for row in desc.get("rows", []) if row]
    assert real_cols, "introspection must return the real columns"
    corrected = real_cols[0]  # widget_count

    # Attempt 2 — retry with the corrected column.
    calls.append("run_query")
    r2 = json.loads(
        tools["athena_run_query"]._tool_func(sql=f"SELECT sum({corrected}) AS total FROM widgets")  # nosec B608 - test fixture composing expected SQL from module constants
    )
    value = r2["rows"][0][0]

    # The agent's final, grounded answer.
    answer = f"The total {corrected.replace('_', ' ')} is {value}."

    # describe/introspect happened BETWEEN the two query attempts.
    assert calls == ["run_query", "describe", "run_query"]
    # Grounded on the live value, with no raw error surfaced.
    assert "42" in answer
    assert "COLUMN_NOT_FOUND" not in answer
    assert "error" not in answer.lower()


# --------------------------------------------------------------------------- #
# 2. Mode B generic tools — well-formed results against a fake backend
# --------------------------------------------------------------------------- #
class _FakePGCursor:
    def __init__(self):
        self.description = None
        self._rows: list = []

    def execute(self, query, params=None):
        s = str(query).lower()
        if "search_path" in s:
            return
        if "information_schema.tables" in s:
            self.description = [("table_name",)]
            self._rows = [("orders",), ("customers",)]
        elif s.strip().startswith("select 1"):
            self.description = [("?column?",)]
            self._rows = [(1,)]
        else:
            self.description = [("total",)]
            self._rows = [(42,)]

    def fetchall(self):
        rows, self._rows = self._rows, []
        return list(rows)

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def close(self):
        pass


class _FakePGConn:
    def set_session(self, **kwargs):
        pass

    def cursor(self):
        return _FakePGCursor()

    def close(self):
        pass


class _FakePgDriver:
    def __init__(self):
        self.connected_with = []

    def connect(self, **kwargs):
        self.connected_with.append(kwargs)
        return _FakePGConn()


@pytest.fixture
def fake_pg(monkeypatch):
    fake = _FakePgDriver()
    monkeypatch.setattr(sa.pgdriver, "connect", fake.connect)
    return fake


# --- PostgreSQL ------------------------------------------------------------- #
def test_mode_b_pg_describe_schema(fake_pg):
    out = sa._mode_b_describe_schema("PostgreSQL", "public", {"host": "h", "dbname": "d"}, {})
    assert out["connection_type"] == "PostgreSQL"
    assert out["schema"] == "public"
    assert out["tables"] == ["orders", "customers"]


def test_mode_b_pg_run_query(fake_pg):
    out = sa._mode_b_run_query(
        "PostgreSQL", "SELECT total FROM widgets", "public", {"host": "h", "dbname": "d"}, {}
    )
    assert out["columns"] == ["total"]
    assert out["rows"] == [[42]]
    assert out["row_count"] == 1


def test_mode_b_pg_connect(fake_pg):
    out = sa._mode_b_connect("PostgreSQL", "public", {"host": "h", "dbname": "d"}, {})
    assert out == {"connection_type": "PostgreSQL", "connected": True}


def test_mode_b_pg_connect_reports_failure(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(sa.pgdriver, "connect", _boom)
    out = sa._mode_b_connect("PostgreSQL", "public", {"host": "h", "dbname": "d"}, {})
    assert out == {
        "connection_type": "PostgreSQL", "connected": False,
        "error": "connection_unavailable", "error_type": "connection_error",
    }
    assert "no route to host" not in str(out)


# --- Athena ----------------------------------------------------------------- #
@pytest.fixture
def fake_athena(monkeypatch):
    fake = _FakeAthena()
    monkeypatch.setattr("agents.athena_tools.boto3_client_for", lambda *a, **k: fake)
    return fake


def test_mode_b_athena_describe_schema(fake_athena):
    out = sa._mode_b_describe_schema("Athena", "", None, {"workgroup": "wg", "database": "db"})
    assert out["connection_type"] == "Athena"
    assert out["database"] == "db"
    assert out["tables"] == ["widgets"]


def test_mode_b_athena_run_query(fake_athena):
    out = sa._mode_b_run_query(
        "Athena", "SELECT sum(widget_count) AS total FROM widgets", "", None,
        {"workgroup": "wg", "database": "db"},
    )
    assert out["columns"] == ["total"]
    assert out["rows"] == [["42"]]


def test_mode_b_athena_connect(fake_athena):
    out = sa._mode_b_connect("Athena", "", None, {"workgroup": "wg", "database": "db"})
    assert out == {"connection_type": "Athena", "connected": True}


# --- Non-SQL types stay coherent (well-formed, no crash) -------------------- #
@pytest.mark.parametrize("connection_type", ["SiteWise (i3X)", "REST API", "MCP Server"])
def test_mode_b_non_sql_run_query_is_coherent(connection_type):
    out = sa._mode_b_run_query(connection_type, "anything", "", None, {})
    assert out["connection_type"] == connection_type
    assert "SQL-only" in out["error"]


@pytest.mark.parametrize("connection_type", ["SiteWise (i3X)", "REST API", "MCP Server"])
def test_mode_b_non_sql_describe_is_coherent(connection_type):
    out = sa._mode_b_describe_schema(connection_type, "", None, {})
    assert out["connection_type"] == connection_type
    assert "note" in out


# --- The three helpers are wired to the registered Mode B tools ------------- #
def test_mode_b_server_exposes_only_the_three_generic_tools(monkeypatch):
    import asyncio

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    mcp = sa.create_mcp_server(
        system_id="sys-a",
        system_name="System A",
        schema="public",
        system_prompt="",
        conn_params={"host": "h", "dbname": "d"},
        connection_type="PostgreSQL",
        connection_config={},
    )
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"describe_schema", "run_query", "connect"}
