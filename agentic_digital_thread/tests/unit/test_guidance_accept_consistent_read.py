# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guidance Deliver reads the proposal row it just wrote with a strongly
consistent read.

The acceptance saga writes the proposal row ``accepted`` and then reads it back
(``_guidance_terminal_accept`` then ``_guidance_proposal_status``). A DynamoDB
query is eventually consistent unless it asks for ``ConsistentRead``, so a
readback a few milliseconds after the update can still see ``pending``. The
saga then rolled the graph and the delivery store back and the route answered
503 ("Delivery could not be checked right now") while a retry a moment later
worked. The same stale read in the rollback left the row ``accepted`` with the
note undelivered.

The fake table below models that lag: a main-table query without
``ConsistentRead`` sees the rows as they were at the last ``put_item``; a
consistent query sees every write.
"""
from __future__ import annotations

import logging

import pytest

import agents.self_learning as learning
import agents.system_guidance as guidance
from agents.guidance_scope import delivery_document
from unit.test_self_learning_lifecycle import FakeTable
from unit.test_system_guidance import FakeBackend

NOTE = "Return the required provenance."


class _LaggingTable(FakeTable):
    """An eventually consistent replica: plain main-table queries miss every
    ``update_item`` since the last ``put_item``."""

    def __init__(self):
        super().__init__()
        self._replica: dict = {}
        self.consistent_queries = 0

    def put_item(self, Item: dict) -> None:
        super().put_item(Item)
        self._replica = {key: dict(row) for key, row in self._items.items()}

    def query(self, **kwargs) -> dict:
        if kwargs.get("IndexName"):
            return super().query(**kwargs)
        if kwargs.get("ConsistentRead") is True:
            self.consistent_queries += 1
            return super().query(**kwargs)
        live, self._items = self._items, self._replica
        try:
            return super().query(**kwargs)
        finally:
            self._items = live


class _FirstConsistentReadFails(_LaggingTable):
    """The first consistent query raises (a throttle, a dropped connection)."""

    def query(self, **kwargs) -> dict:
        if kwargs.get("ConsistentRead") is True and not self.consistent_queries:
            self.consistent_queries += 1
            raise RuntimeError("transient read failure")
        return super().query(**kwargs)


@pytest.fixture
def world(monkeypatch):
    graph = {"guidance": "Base."}
    monkeypatch.setattr(learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    monkeypatch.setattr(
        learning, "_neptune_query",
        lambda *_a, **_k: [{"qg": graph["guidance"]}])

    def graph_write(cypher, params=None):
        params = params or {}
        if ("SET s.query_guidance = $desired" in cypher
                and graph["guidance"] == params.get("expected")):
            graph["guidance"] = params["desired"]
        return True

    monkeypatch.setattr(learning, "_neptune_write", graph_write)
    monkeypatch.setattr(guidance, "_store",
                        guidance.SystemGuidanceStore(backend=FakeBackend()))
    return graph


def _file_and_accept(monkeypatch, table):
    monkeypatch.setattr(learning, "_get_table", lambda: table)
    filed = learning.assert_vocab(
        proposed_type="guidance_note", system_id="system-a", note=NOTE,
        is_admin=False)
    return filed["mapping_id"], learning.accept_mapping(
        filed["mapping_id"], actor="steward")


def test_first_deliver_succeeds_when_the_replica_has_not_seen_the_update(
        monkeypatch, world):
    table = _LaggingTable()
    mapping_id, result = _file_and_accept(monkeypatch, table)
    assert result["status"] == "accepted", result
    assert "http_status" not in result
    assert table.by_id(mapping_id)["status"] == "accepted"
    assert NOTE in world["guidance"]
    assert NOTE in guidance.read("system-a")
    assert table.consistent_queries >= 1


def test_a_failed_readback_never_leaves_the_proposal_accepted(
        monkeypatch, world):
    table = _FirstConsistentReadFails()
    mapping_id, result = _file_and_accept(monkeypatch, table)
    assert result["status"] == "pending"
    assert result["http_status"] == 503
    # Fail closed: the row is pending again and the note left both surfaces.
    assert table.by_id(mapping_id)["status"] == "pending"
    assert world["guidance"] == "Base."
    assert NOTE not in guidance.read("system-a")


def test_a_stopped_deliver_logs_the_check_and_error_class(
        monkeypatch, world, caplog):
    table = _FirstConsistentReadFails()
    with caplog.at_level(logging.WARNING, logger="agents.guidance_saga"):
        _file_and_accept(monkeypatch, table)
    stops = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("Guidance update stopped at ")]
    assert stops == [
        "Guidance update stopped at proposal status readback (RuntimeError)"]
    assert NOTE not in " ".join(stops)


class _Scripted:
    """A saga world that succeeds unless one step is told to fail."""

    def __init__(self, fail: str):
        self.fail = fail
        self.graph = "Base."
        self.delivery = guidance.GuidanceSnapshot.present(
            delivery_document("Base."), 3)
        self.graph_reads = 0
        self.delivery_reads = 0
        self.proposal = "pending"

    def graph_cas(self, expected, desired):
        if self.fail == "graph write":
            return False
        if self.fail == "graph write fails" and self.graph == expected:
            raise RuntimeError("write failed")
        if self.graph == expected:
            self.graph = desired
        return True

    def graph_read(self):
        self.graph_reads += 1
        if self.fail == "graph check before writing" and self.graph_reads == 1:
            raise RuntimeError("read failed")
        if self.fail == "graph readback" and self.graph_reads == 2:
            return "something else"
        return self.graph

    def delivery_cas(self, expected, target):
        if self.fail == "delivery write":
            raise guidance.GuidanceConflict()
        self.delivery = guidance.GuidanceSnapshot.present(
            target.text, expected.version + 1)
        return self.delivery

    def delivery_read(self):
        self.delivery_reads += 1
        if self.fail == "delivery check before writing" and self.delivery_reads == 1:
            return guidance.GuidanceSnapshot.present("drifted", 3)
        if self.fail == "delivery readback" and self.delivery_reads == 2:
            raise guidance.GuidanceUnavailable("store unavailable")
        if self.fail == "delivery readback differs" and self.delivery_reads == 2:
            return guidance.GuidanceSnapshot.present("other", 9)
        return self.delivery

    def terminal_cas(self, _graph_text, _delivered):
        if self.fail == "proposal status write fails":
            raise RuntimeError("conditional update failed")
        if self.fail == "proposal status write":
            return False
        self.proposal = "accepted"
        return True

    def terminal_read(self):
        # A failed write leaves the row pending; a stale readback reads pending.
        return "pending" if self.fail == "proposal status readback" else self.proposal


@pytest.mark.parametrize("fail, stage, reason", [
    ("graph check before writing", "graph check before writing", "RuntimeError"),
    ("delivery check before writing", "delivery check before writing",
     "result did not match"),
    ("graph write", "graph write", "result did not match"),
    ("graph write fails", "graph write", "RuntimeError"),
    ("graph readback", "graph readback", "result did not match"),
    ("delivery write", "delivery write", "GuidanceConflict"),
    ("delivery readback", "delivery readback", "GuidanceUnavailable"),
    ("delivery readback differs", "delivery readback", "result did not match"),
    ("proposal status write", "proposal status write", "result did not match"),
    ("proposal status write fails", "proposal status write", "RuntimeError"),
    ("proposal status readback", "proposal status readback", "result did not match"),
])
def test_each_saga_check_names_itself_once_when_it_stops(caplog, fail, stage, reason):
    import agents.guidance_saga as saga
    world = _Scripted(fail)
    with caplog.at_level(logging.WARNING, logger="agents.guidance_saga"):
        result = saga.replace_exact(
            graph_before="Base.", graph_after="Base. LEARNED: next",
            delivery_before=world.delivery, graph_cas=world.graph_cas,
            graph_read=world.graph_read, delivery_cas=world.delivery_cas,
            delivery_read=world.delivery_read, terminal_cas=world.terminal_cas,
            terminal_read=world.terminal_read)
    assert result["status"] == "pending"
    stops = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("Guidance update stopped at ")]
    assert stops == [f"Guidance update stopped at {stage} ({reason})"]
    assert "LEARNED" not in " ".join(stops)


def test_an_unreadable_current_guidance_names_its_check(
        monkeypatch, world, caplog):
    monkeypatch.setattr(learning, "_neptune_query", lambda *_a, **_k: [])
    table = _LaggingTable()
    with caplog.at_level(logging.WARNING, logger="agents.guidance_saga"):
        _mapping_id, result = _file_and_accept(monkeypatch, table)
    assert result["http_status"] == 503
    assert result["outcome"] == "unavailable"
    stops = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("Guidance update stopped at ")]
    assert stops == ["Guidance update stopped at reading the current guidance "
                     "(the graph returned no system row)"]
