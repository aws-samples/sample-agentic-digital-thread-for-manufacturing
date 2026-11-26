# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A failed Deliver proves the proposal row is pending again, or says it is not.

When the ``accepted`` status write lands but a later check fails, the saga
puts the graph and the delivery store back and then undoes the status write.
That undo used to be fire-and-forget: it read the status first and skipped
the undo when the read failed, and the saga ignored what it returned, so a
second failure left the row ``accepted`` while the note had been removed,
and the reply still said nothing was accepted.

Now the undo is a verified step. It is a conditional ``accepted`` to
``pending`` update with no read first, and the saga then reads the status with
a strongly consistent read. Only a ``pending`` read proves it. Anything else
is reported as ``drift.proposal`` and logged with the check that stopped.
"""
from __future__ import annotations

import logging

import pytest

import agents.guidance_saga as saga
import agents.self_learning as learning
import agents.system_guidance as guidance
from unit.test_guidance_accept_consistent_read import NOTE, _file_and_accept, world  # noqa: F401
from unit.test_guidance_saga import _before, _Delivery, _Graph, _run
from unit.test_self_learning_lifecycle import ConditionalCheckFailedException, FakeTable


def _stops(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("Guidance update stopped at ")]


def _terminal_written_then_unreadable(proposal: dict):
    """A terminal write that lands, then a readback that fails once."""
    def terminal_cas(_graph_text, _delivered):
        proposal["status"] = "accepted"
        return True

    def terminal_read():
        proposal["reads"] += 1
        if proposal["reads"] == 1:
            raise RuntimeError("read failed")
        return proposal["status"]

    return terminal_cas, terminal_read


def _run_with_restore(restore, proposal=None):
    proposal = proposal if proposal is not None else {"status": "pending", "reads": 0}
    events: list = []
    graph = _Graph("Base.", events)
    delivery = _Delivery(_before(), events)
    terminal_cas, terminal_read = _terminal_written_then_unreadable(proposal)
    result = _run(graph, delivery, terminal_cas=terminal_cas,
                  terminal_read=terminal_read, terminal_restore=restore)
    return result, graph, delivery, proposal


def test_every_failure_shape_carries_the_proposal_drift_flag():
    assert saga.pending_result()["drift"] == {
        "graph": False, "delivery": False, "proposal": False}
    assert saga.pending_result(proposal_drift=True)["drift"]["proposal"] is True


def test_a_restore_that_returns_false_is_reported_as_proposal_drift(caplog):
    with caplog.at_level(logging.WARNING, logger="agents.guidance_saga"):
        result, graph, delivery, proposal = _run_with_restore(lambda: False)
    assert result == saga.pending_result(proposal_drift=True)
    assert graph.text == "Base."
    assert delivery.value.text == _before().text
    assert proposal["status"] == "accepted"
    assert _stops(caplog) == [
        "Guidance update stopped at proposal status readback (RuntimeError)",
        "Guidance update stopped at proposal status restore (result did not match)"]


def test_a_restore_that_raises_is_reported_as_proposal_drift(caplog):
    def restore():
        raise RuntimeError("update failed")

    with caplog.at_level(logging.WARNING, logger="agents.guidance_saga"):
        result, graph, _delivery, _proposal = _run_with_restore(restore)
    assert result == saga.pending_result(proposal_drift=True)
    assert graph.text == "Base."
    assert _stops(caplog)[-1] == (
        "Guidance update stopped at proposal status restore (RuntimeError)")


def test_a_restore_is_proven_only_by_a_pending_read(caplog):
    proposal = {"status": "pending", "reads": 0}
    with caplog.at_level(logging.WARNING, logger="agents.guidance_saga"):
        result, *_rest = _run_with_restore(lambda: True, proposal)
    # The restore said yes, but the row still reads accepted.
    assert result == saga.pending_result(proposal_drift=True)
    assert _stops(caplog)[-1] == (
        "Guidance update stopped at proposal status restore readback "
        "(result did not match)")


def test_a_restore_readback_that_raises_is_not_proof(caplog):
    proposal = {"status": "pending", "reads": 0}

    def restore():
        proposal["status"] = "pending"
        proposal["reads"] = -10  # every later read raises
        return True

    def terminal_read():
        proposal["reads"] += 1
        if proposal["reads"] == 1 or proposal["reads"] < 0:
            raise RuntimeError("read failed")
        return proposal["status"]

    events: list = []
    with caplog.at_level(logging.WARNING, logger="agents.guidance_saga"):
        result = _run(
            _Graph("Base.", events), _Delivery(_before(), events),
            terminal_cas=lambda *_a: proposal.update(status="accepted") or True,
            terminal_read=terminal_read, terminal_restore=restore)
    assert result == saga.pending_result(proposal_drift=True)
    assert _stops(caplog)[-1] == (
        "Guidance update stopped at proposal status restore readback (RuntimeError)")


def test_a_proven_restore_reports_no_drift(caplog):
    proposal = {"status": "pending", "reads": 0}

    def restore():
        proposal["status"] = "pending"
        return True

    with caplog.at_level(logging.WARNING, logger="agents.guidance_saga"):
        result, *_rest = _run_with_restore(restore, proposal)
    assert result == saga.pending_result()
    assert _stops(caplog) == [
        "Guidance update stopped at proposal status readback (RuntimeError)"]


def test_with_no_restore_step_an_accepted_read_is_proposal_drift():
    events: list = []
    result = _run(
        _Graph("Base.", events), _Delivery(_before(), events),
        terminal_cas=lambda *_a: True, terminal_read=lambda: "accepted",
        terminal_restore=None)
    # The terminal readback said accepted, so this run succeeded; a failing
    # one is the case below.
    assert result["status"] == "synchronized"

    events = []
    reads = iter(["unknown", "accepted"])
    failed = _run(
        _Graph("Base.", events), _Delivery(_before(), events),
        terminal_cas=lambda *_a: True, terminal_read=lambda: next(reads),
        terminal_restore=None)
    assert failed == saga.pending_result(proposal_drift=True)


# --------------------------------------------------------------------------- #
# The proposal table's own restore (self_learning._guidance_terminal_restore)
# --------------------------------------------------------------------------- #
class _ScriptedTable(FakeTable):
    """Records every main-table call; fails the calls it is told to fail."""

    def __init__(self, *, fail_queries=(), fail_pending_update=False):
        super().__init__()
        self.calls: list[str] = []
        self._consistent = 0
        self._fail_queries = set(fail_queries)
        self._fail_pending_update = fail_pending_update

    def query(self, **kwargs) -> dict:
        if kwargs.get("IndexName"):
            return super().query(**kwargs)
        if kwargs.get("ConsistentRead") is True:
            self._consistent += 1
            self.calls.append(f"read#{self._consistent}")
            if self._consistent in self._fail_queries:
                raise RuntimeError("read failed")
        return super().query(**kwargs)

    def update_item(self, **kwargs) -> None:
        status = kwargs["ExpressionAttributeValues"].get(":status")
        expected = kwargs["ExpressionAttributeValues"].get(":expected")
        self.calls.append(f"set {status} if {expected}")
        if status == "pending" and self._fail_pending_update:
            raise RuntimeError("update failed")
        super().update_item(**kwargs)


def test_restore_writes_pending_without_reading_first(monkeypatch, world):
    # The terminal readback and the next read both fail: the old restore
    # read first, failed, and never undid the accept.
    table = _ScriptedTable(fail_queries={1, 2})
    mapping_id, result = _file_and_accept(monkeypatch, table)
    assert table.by_id(mapping_id)["status"] == "pending"
    after_accept = table.calls[table.calls.index("set accepted if pending") + 1:]
    assert after_accept[:2] == ["read#1", "set pending if accepted"]
    assert result["status"] == "pending"
    assert result["drift"]["proposal"] is True  # read#2 failed: not proven
    assert world["guidance"] == "Base."


def test_restore_proven_by_a_consistent_read(monkeypatch, world):
    table = _ScriptedTable(fail_queries={1})
    mapping_id, result = _file_and_accept(monkeypatch, table)
    assert table.by_id(mapping_id)["status"] == "pending"
    assert result["drift"] == {"graph": False, "delivery": False, "proposal": False}
    assert result["http_status"] == 503
    assert NOTE not in guidance.read("system-a")


def test_a_failed_restore_update_is_reported_and_logged(monkeypatch, world, caplog):
    table = _ScriptedTable(fail_queries={1}, fail_pending_update=True)
    with caplog.at_level(logging.WARNING, logger="agents.guidance_saga"):
        mapping_id, result = _file_and_accept(monkeypatch, table)
    assert table.by_id(mapping_id)["status"] == "accepted"
    assert result["status"] == "pending"
    assert result["http_status"] == 503
    assert result["drift"]["proposal"] is True
    assert world["guidance"] == "Base."
    assert _stops(caplog)[-1] == (
        "Guidance update stopped at proposal status restore (RuntimeError)")


def test_restore_leaves_a_row_that_was_never_accepted(monkeypatch, world):
    # The accept write itself fails, so the row stays pending; the restore's
    # conditional update finds nothing to undo and the read proves pending.
    class _AcceptFails(_ScriptedTable):
        def update_item(self, **kwargs) -> None:
            if kwargs["ExpressionAttributeValues"].get(":status") == "accepted":
                self.calls.append("set accepted if pending")
                raise RuntimeError("accept write failed")
            super().update_item(**kwargs)

    table = _AcceptFails()
    mapping_id, result = _file_and_accept(monkeypatch, table)
    assert table.by_id(mapping_id)["status"] == "pending"
    assert "set pending if accepted" in table.calls
    assert result["drift"] == {"graph": False, "delivery": False, "proposal": False}


def test_the_conditional_miss_is_recognised_by_its_error_name():
    table = FakeTable()
    item = {"mapping_id": "m-1", "discovered_at": "2026-10-27T00:00:00",
            "status": "pending"}
    table.put_item(Item=item)
    assert learning._guidance_terminal_restore(table, item) is True
    with pytest.raises(ConditionalCheckFailedException):
        learning._update_status(table, "m-1", item["discovered_at"], "pending",
                                expect_status="accepted")
