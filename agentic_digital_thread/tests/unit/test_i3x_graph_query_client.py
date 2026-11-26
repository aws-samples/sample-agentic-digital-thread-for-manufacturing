# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The i3X telemetry tools' graph reads use the app's one graph client and
its resubmit path.

``system_agent._i3x_graph_query`` built its own neptune-graph client with SDK
defaults: botocore's ``legacy`` retry mode, 5 attempts, resending a 429, a
5xx or a read timeout itself with ``amz-sdk-request: attempt=2`` (the resend
Neptune Analytics refuses with "Retry for SDK query requests is suppressed,
please resubmit the query"). It now goes through ``db``: one attempt per
call, a server-side limit below the read timeout, and ``graph_resubmit``
deciding what is sent again. Its own contract is kept: the region comes from
``AWS_REGION`` (else boto3's chain), ``GRAPH_IDENTIFIER`` is required, and a
failure raises to the tool, which reports it.

Graph labels and properties are invented: the fix is generic.
"""
from __future__ import annotations

import io
import json

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

import agents.db as db
import agents.system_agent as sa

CYPHER = "MATCH (a:Gizmo {name: $name}) RETURN a.name AS name"
PARAMS = {"name": "gizmo-7"}


def _throttled() -> ClientError:
    return type("ThrottlingException", (ClientError,), {})(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"},
         "ResponseMetadata": {"HTTPStatusCode": 429}}, "ExecuteQuery")


class _Graph:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls: list[dict] = []

    def execute_query(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return {"payload": io.BytesIO(json.dumps({"results": [{"name": "gizmo-7"}]}).encode())}


@pytest.fixture
def graph(monkeypatch):
    import agents.graph_resubmit as gr
    monkeypatch.setattr(gr, "_sleep", lambda _s: None)
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    built: list[tuple] = []

    def use(errors=()):
        fake = _Graph(errors)

        def factory(*args, **kwargs):
            built.append((args, kwargs))
            return fake
        monkeypatch.setattr(sa.boto3, "client", factory)
        return fake
    use.built = built
    return use


def test_the_i3x_graph_read_uses_the_one_attempt_client(graph):
    graph()
    assert sa._i3x_graph_query(CYPHER, PARAMS) == [{"name": "gizmo-7"}]
    (args, kwargs), = graph.built
    assert args == ("neptune-graph",)
    assert kwargs["region_name"] == "eu-central-1"
    config = kwargs.get("config")
    assert config is not None, "the i3X read builds its client with SDK defaults"
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert config.read_timeout == db.GRAPH_READ_TIMEOUT_S
    assert config.connect_timeout == db.GRAPH_CONNECT_TIMEOUT_S


def test_the_i3x_graph_read_carries_the_server_side_limit(graph):
    fake = graph()
    sa._i3x_graph_query(CYPHER, PARAMS)
    call, = fake.calls
    assert call["queryTimeoutMilliseconds"] == db.GRAPH_QUERY_TIMEOUT_MS
    assert call["graphIdentifier"] == "g-unit"
    assert call["parameters"] == PARAMS and call["language"] == "OPEN_CYPHER"


def test_a_throttled_i3x_read_is_resubmitted(graph):
    fake = graph([_throttled()])
    assert sa._i3x_graph_query(CYPHER, PARAMS) == [{"name": "gizmo-7"}]
    assert len(fake.calls) == 2


def test_a_timed_out_i3x_read_is_not_sent_again(graph):
    fake = graph([ReadTimeoutError(endpoint_url="https://g-unit.example/queries")])
    with pytest.raises(Exception):
        sa._i3x_graph_query(CYPHER, PARAMS)
    assert len(fake.calls) == 1


def test_an_unset_region_is_left_to_the_boto3_chain(graph, monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    graph()
    sa._i3x_graph_query(CYPHER, PARAMS)
    (_args, kwargs), = graph.built
    assert kwargs["region_name"] is None


def test_a_missing_graph_identifier_raises_before_any_call(graph, monkeypatch):
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)
    fake = graph()
    with pytest.raises(KeyError):
        sa._i3x_graph_query(CYPHER, PARAMS)
    assert fake.calls == []
