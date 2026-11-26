# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 2 (Task 2.2) — ``_mode_b_describe_schema`` returns columns + types (+ samples),
NOT just table names. Runs fully offline with a mocked pgdriver cursor. Synthetic data
only — no dataset literals.
"""
import agents.system_agent as sa


class _FakeCursor:
    def __init__(self):
        self._last = ""

    def execute(self, sql, params=None):
        self._last = sql

    def fetchall(self):
        s = self._last
        if "information_schema.tables" in s:
            return [("widgets",), ("orders",)]
        if "information_schema.columns" in s:
            return [
                ("widgets", "widget_code", "varchar"),
                ("widgets", "email", "varchar"),
                ("orders", "order_id", "integer"),
            ]
        if "SELECT DISTINCT" in s:
            if "widget_code" in s:
                return [("W-1",), ("W-2",)]
            if "order_id" in s:
                return [("O-1",)]
            return []
        return []


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


def _describe_pg(monkeypatch, include_samples=False):
    monkeypatch.setattr(sa, "_pg_readonly_conn", lambda _p: _FakeConn())
    return sa._mode_b_describe_schema(
        "PostgreSQL", "widget_schema", {"host": "h", "dbname": "d"}, None,
        include_samples=include_samples,
    )


def test_pg_describe_returns_columns_and_types(monkeypatch):
    res = _describe_pg(monkeypatch)
    rendered = res["rendered"]
    # Columns + types are present — NOT just table names (the regression this fixes).
    assert "widget_code: varchar" in rendered
    assert "order_id: integer" in rendered
    # structured columns map retained/added
    assert {"name": "order_id", "data_type": "integer"} in res["columns"]["orders"]
    # backward-compat: the table-names list is still present
    assert set(res["tables"]) == {"widgets", "orders"}


def test_pg_describe_bakes_samples_when_opted_in(monkeypatch):
    # Samples require BOTH the env flag AND an explicit include_samples=True (the
    # per-column sampling is expensive, so it is opt-in per call — never on the
    # latency-sensitive default/self-correction path).
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "on")
    res = _describe_pg(monkeypatch, include_samples=True)
    rendered = res["rendered"]
    # non-PII column carries example values
    assert "e.g. W-1, W-2" in rendered
    assert "e.g. O-1" in rendered
    # PII-named column: no raw values surfaced
    assert "email: varchar [redacted]" in rendered


def test_pg_describe_metadata_only_by_default(monkeypatch):
    # DEFAULT (no include_samples) is metadata-only EVEN when the env flag is on —
    # this is the latency-safety default that prevents the wide-table sampling hang.
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "on")
    res = _describe_pg(monkeypatch)  # include_samples defaults False
    rendered = res["rendered"]
    assert "e.g." not in rendered
    assert "widget_code: varchar" in rendered


def test_pg_describe_samples_disabled_by_env(monkeypatch):
    # Env off suppresses samples even when explicitly opted in.
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    res = _describe_pg(monkeypatch, include_samples=True)
    rendered = res["rendered"]
    assert "e.g." not in rendered
    # columns+types still present
    assert "widget_code: varchar" in rendered


def test_nonsql_types_unchanged(monkeypatch):
    # SiteWise (i3X) / REST API / MCP Server return the same steer note as before.
    for ct in ("SiteWise (i3X)", "REST API", "MCP Server"):
        res = sa._mode_b_describe_schema(ct, "", None, None)
        assert res == {
            "connection_type": ct,
            "note": (
                "non-SQL system — call connect to verify reachability, then use the "
                "system's structured explore tools to discover its shape"
            ),
        }
