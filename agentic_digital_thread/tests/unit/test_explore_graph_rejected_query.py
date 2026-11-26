# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A REJECTED ``explore_graph`` query is a failed call, never an empty graph.

Evidence (2026-10-02): an ERP shipment-lines turn
made nine ``explore_graph`` calls and never reached the ERP agent. Three of the
nine openCypher queries were rejected by Neptune (``ValidationException``), but
``neptune_query`` degrades a failure to ``[]``, so the tool answered "No
nodes/edges matched ... do NOT broaden or retry" and the determinism score
counted 9/9 grounded. The model was told data did not exist when its query had
never run.

Pinned here: ``explore_graph`` runs the model's query through
``neptune_query_checked`` and returns a failure envelope whose head carries the
error marker; the inventory query does not run; ``neptune_query`` itself keeps
its return-[]-on-failure degrade (ADR-0069). Fully offline.
"""
import json
import logging

import pytest

import agents.db as db
import agents.graph_tools as graph_tools
from agents import query_intelligence as qi


class _ValidationException(Exception):
    """Stand-in for botocore's ClientError (carries ``response``)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.response = {"Error": {"Code": "ValidationException", "Message": message}}


_ValidationException.__name__ = "ValidationException"


def _reject(*_a, **_k):
    raise db.GraphQueryError("ValidationException", "Unknown function 'keys'")


def test_rejected_query_returns_a_failure_envelope_not_an_empty_graph(monkeypatch):
    lenient_calls = []
    monkeypatch.setattr(graph_tools, "neptune_query_checked", _reject)
    monkeypatch.setattr(graph_tools, "neptune_query",
                        lambda *a, **k: lenient_calls.append(a) or [])

    raw = graph_tools.explore_graph("MATCH (n) RETURN keys(n)")
    out = json.loads(raw)

    assert out["query_failed"] is True
    assert out["results"] == []
    assert out["error"] == "Graph query failed (ValidationException): Unknown function 'keys'"
    assert "empty" not in out and "graph_node_types" not in out
    assert "NOT an empty result" in out["note"]
    assert lenient_calls == [], "a rejected query must not trigger the empty-graph inventory"
    # The error leads the payload, so the envelope head classifies it.
    assert raw.startswith('{"error": "Graph query failed')


def test_rejected_query_scores_as_a_failed_call(monkeypatch):
    monkeypatch.setattr(graph_tools, "neptune_query_checked", _reject)
    raw = graph_tools.explore_graph("MATCH (n) RETURN keys(n)")
    assert qi.failure_marker(raw) == ("error", "query failed")
    assert qi.is_failed_result(raw) is True


def test_empty_graph_result_is_still_not_a_failure(monkeypatch):
    monkeypatch.setattr(graph_tools, "neptune_query_checked", lambda *a, **k: [])
    monkeypatch.setattr(graph_tools, "neptune_query",
                        lambda *a, **k: [{"label": "System", "count": 3}])
    raw = graph_tools.explore_graph("MATCH (o:Order) RETURN o")
    assert json.loads(raw)["empty"] is True
    assert qi.failure_marker(raw) is None


def test_checked_query_raises_with_the_backend_message_masked(monkeypatch, caplog):
    msg = ("Invalid query for graph arn:aws:neptune-graph:us-east-1:111122223333:graph/g-abc "
           "in account 111122223333: " + "x" * 400)

    def _boom(*_a, **_k):
        raise _ValidationException(msg)

    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    monkeypatch.setattr(db, "_neptune_execute", _boom)
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        with pytest.raises(db.GraphQueryError) as info:
            db.neptune_query_checked("MATCH (n RETURN n")
    err = info.value
    assert err.category == "ValidationException"
    assert "111122223333" not in err.detail and "arn:" not in err.detail
    assert err.detail.startswith("Invalid query for graph <arn> in account <account>:")
    assert len(err.detail) <= 300
    # Same failure log line as the lenient path; no query text, no message.
    assert "Neptune query failed backend=neptune category=ValidationException" in caplog.text
    assert "MATCH" not in caplog.text


def test_lenient_query_keeps_its_empty_list_degrade(monkeypatch):
    def _boom(*_a, **_k):
        raise _ValidationException("bad query")

    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    monkeypatch.setattr(db, "_neptune_execute", _boom)
    assert db.neptune_query("MATCH (n RETURN n") == []


def test_lenient_query_logs_why_the_engine_refused(monkeypatch, caplog):
    """The [] degrade hides a rejected query from its caller, so the log is the
    only place the reason can show. On 2026-10-10 the route-seed rules query
    failed on every build of a deployed account and the log said only
    ``category=ValidationException``; the engine's message, which named the
    cause (too many ORDER BY keys), was dropped. The lenient path logs that
    message, masked and bounded like ``GraphQueryError.detail``; still no
    query text."""
    msg = ("ORDER BY with more than 5 sort keys is not supported in this release. "
           "graph arn:aws:neptune-graph:us-east-1:111122223333:graph/g-abc")

    def _boom(*_a, **_k):
        raise _ValidationException(msg)

    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    monkeypatch.setattr(db, "_neptune_execute", _boom)
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        assert db.neptune_query("MATCH (n) WITH n ORDER BY n.a RETURN n") == []
    assert "ORDER BY with more than 5 sort keys is not supported in this release." in caplog.text
    assert "category=ValidationException" in caplog.text
    assert "111122223333" not in caplog.text and "arn:" not in caplog.text
    assert "MATCH" not in caplog.text
