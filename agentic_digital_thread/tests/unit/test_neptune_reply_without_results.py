# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A Neptune Analytics reply with no ``results`` list is not an answer of no rows.

``_neptune_execute`` reads ``results`` from the ExecuteQuery payload. A reply
without that list used to read as ``[]``, the same as a query that matched
nothing. Inside ``recording_incomplete_reads`` a caller that deletes what a
read did not return (the equipment mapping reconcile) must be able to tell
the two apart, so such a reply is noted as an incomplete read. Its return
value stays ``[]`` for every other caller."""
from __future__ import annotations

import io
import json

import pytest

import agents.db as db


class _Client:
    def __init__(self, body):
        self.body = body

    def execute_query(self, **_kwargs):
        return {"payload": io.BytesIO(json.dumps(self.body).encode())}


@pytest.fixture
def reply(monkeypatch):
    def use(body):
        monkeypatch.setattr(db.boto3, "client", lambda *a, **k: _Client(body))
    return use


@pytest.mark.parametrize("body", [{}, {"results": None}, {"results": {"n": 1}}])
def test_a_reply_without_a_results_list_is_an_incomplete_read(reply, body):
    reply(body)
    with db.recording_incomplete_reads() as sink:
        assert db._neptune_execute("MATCH (n) RETURN n") == []
    assert len(sink) == 1


@pytest.mark.parametrize("body, rows", [({"results": []}, []),
                                        ({"results": [{"n": 1}]}, [{"n": 1}])])
def test_a_reply_with_a_results_list_is_read_as_before(reply, body, rows):
    reply(body)
    with db.recording_incomplete_reads() as sink:
        assert db._neptune_execute("MATCH (n) RETURN n") == rows
    assert sink == []


def test_outside_a_recording_scope_nothing_is_noted_and_the_reply_reads_empty(reply):
    reply({})
    assert db._neptune_execute("MATCH (n) RETURN n") == []
    assert db.incomplete_read_count() == 0


def test_the_lenient_read_a_reconcile_uses_sees_it_as_incomplete(reply, monkeypatch):
    """Through ``neptune_query``, the read the equipment pass's discovery makes:
    an empty discovery from such a reply must not read as "no registry"."""
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")
    reply({})
    with db.recording_incomplete_reads():
        assert db.neptune_query("MATCH (s:System) RETURN s.id AS sys_id") == []
        assert db.incomplete_read_count() == 1
