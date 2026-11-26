# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Verifier contracts for the graph/delivery/proposal guidance saga."""
from __future__ import annotations

import agents.guidance_saga as saga
import agents.system_guidance as guidance
from agents.guidance_scope import delivery_document


class _Graph:
    def __init__(self, text, events, fail_restore=False):
        self.text = text
        self.events = events
        self.fail_restore = fail_restore
        self.calls = 0

    def cas(self, expected, desired):
        self.events.append(("graph_cas", expected, desired))
        self.calls += 1
        if self.text != expected or (self.fail_restore and self.calls > 1):
            return False
        self.text = desired
        return True

    def read(self):
        self.events.append(("graph_read", self.text))
        return self.text


class _Delivery:
    def __init__(self, snapshot, events, fail_restore=False):
        self.value = snapshot
        self.events = events
        self.fail_restore = fail_restore
        self.calls = 0

    def cas(self, expected, desired):
        self.events.append(("delivery_cas", expected, desired))
        self.calls += 1
        if self.value != expected or (self.fail_restore and self.calls > 1):
            raise guidance.GuidanceConflict()
        version = expected.version + 1
        self.value = (guidance.GuidanceSnapshot.present(desired.text, version)
                      if desired.exists
                      else guidance.GuidanceSnapshot.absent(version))
        return self.value

    def read(self):
        self.events.append(("delivery_read", self.value))
        return self.value


def _before(graph_text="Base."):
    return guidance.GuidanceSnapshot.present(delivery_document(graph_text), 3)


def _run(graph, delivery, **extra):
    return saga.replace_exact(
        graph_before="Base.", graph_after="Base. LEARNED: next",
        delivery_before=delivery.value, graph_cas=graph.cas,
        graph_read=graph.read, delivery_cas=delivery.cas,
        delivery_read=delivery.read, **extra)


def test_saga_rejects_preexisting_delivery_parity_drift_before_any_cas():
    events = []
    graph = _Graph("Base.", events)
    delivery = _Delivery(guidance.GuidanceSnapshot.present("different", 3), events)
    result = _run(graph, delivery)
    assert result == saga.pending_result(delivery_drift=True)
    assert [event[0] for event in events] == ["graph_read", "delivery_read"]


def test_saga_rejects_preexisting_graph_snapshot_drift_before_any_cas():
    events = []
    graph = _Graph("changed", events)
    delivery = _Delivery(_before(), events)
    result = _run(graph, delivery)
    assert result == saga.pending_result(graph_drift=True)
    assert not any(event[0].endswith("cas") for event in events)


def test_saga_orders_preflight_graph_delivery_and_terminal_readbacks():
    events = []
    graph = _Graph("Base.", events)
    delivery = _Delivery(_before(), events)

    def terminal_cas(graph_text, delivered):
        events.append(("terminal_cas", graph_text, delivered))
        return True

    def terminal_read():
        events.append(("terminal_read",))
        return "accepted"

    result = _run(
        graph, delivery, terminal_cas=terminal_cas,
        terminal_read=terminal_read)
    assert result["status"] == "synchronized"
    assert [event[0] for event in events] == [
        "graph_read", "delivery_read", "graph_cas", "graph_read",
        "delivery_cas", "delivery_read", "terminal_cas", "terminal_read"]


def test_terminal_failure_reverses_delivery_then_graph_with_exact_expected_cas():
    events = []
    graph = _Graph("Base.", events)
    delivery = _Delivery(_before(), events)

    def terminal_cas(graph_text, delivered):
        events.append(("terminal_cas", graph_text, delivered))
        raise RuntimeError("private backend detail")

    result = _run(
        graph, delivery, terminal_cas=terminal_cas,
        terminal_read=lambda: "pending")
    assert result == saga.pending_result()
    terminal_index = [event[0] for event in events].index("terminal_cas")
    compensation = events[terminal_index + 1:]
    assert [event[0] for event in compensation] == [
        "delivery_read", "delivery_cas", "delivery_read",
        "graph_read", "graph_cas", "graph_read"]
    assert compensation[1][1].text == delivery_document(
        "Base. LEARNED: next")
    assert compensation[4][1:] == ("Base. LEARNED: next", "Base.")
    assert delivery.value.text == delivery_document("Base.")
    assert graph.text == "Base."
    assert "private backend detail" not in repr(result)


def test_compensation_failures_report_delivery_and_graph_drift_distinctly():
    events = []
    graph = _Graph("Base.", events, fail_restore=True)
    delivery = _Delivery(_before(), events, fail_restore=True)
    result = _run(
        graph, delivery,
        terminal_cas=lambda *_args: (_ for _ in ()).throw(RuntimeError("failure")),
        terminal_read=lambda: "pending")
    assert result == saga.pending_result(
        graph_drift=True, delivery_drift=True)


def test_absent_delivery_is_valid_only_when_graph_projects_no_notes():
    events = []
    graph = _Graph("Base.", events)
    delivery = _Delivery(guidance.GuidanceSnapshot.absent(), events)
    result = _run(
        graph, delivery, terminal_cas=lambda *_args: False,
        terminal_read=lambda: "pending")
    assert result == saga.pending_result()
    assert delivery.value.exists is False and delivery.value.text == ""

    drift_events = []
    drift_graph = _Graph("Base. LEARNED: existing", drift_events)
    drift_delivery = _Delivery(guidance.GuidanceSnapshot.absent(), drift_events)
    drift = saga.replace_exact(
        graph_before=drift_graph.text, graph_after=drift_graph.text,
        delivery_before=drift_delivery.value, graph_cas=drift_graph.cas,
        graph_read=drift_graph.read, delivery_cas=drift_delivery.cas,
        delivery_read=drift_delivery.read)
    assert drift == saga.pending_result(delivery_drift=True)


def test_unchanged_exact_replacement_proves_parity_without_mutation():
    events = []
    graph = _Graph("Base.", events)
    delivery = _Delivery(_before(), events)
    result = saga.replace_exact(
        graph_before="Base.", graph_after="Base.",
        delivery_before=delivery.value, graph_cas=graph.cas,
        graph_read=graph.read, delivery_cas=delivery.cas,
        delivery_read=delivery.read)
    assert result["status"] == "synchronized"
    assert [event[0] for event in events] == ["graph_read", "delivery_read"]


def test_terminal_readback_failure_restores_proposal_pending():
    events = []
    graph = _Graph("Base.", events)
    delivery = _Delivery(_before(), events)
    proposal = {"status": "pending", "reads": 0}

    def terminal_cas(_graph_text, _delivered):
        proposal["status"] = "accepted"
        return True

    def terminal_read():
        proposal["reads"] += 1
        return "unknown" if proposal["reads"] == 1 else proposal["status"]

    def terminal_restore():
        assert delivery.value.text == delivery_document("Base.")
        assert graph.text == "Base."
        proposal["status"] = "pending"
        return True

    result = _run(
        graph, delivery, terminal_cas=terminal_cas,
        terminal_read=terminal_read, terminal_restore=terminal_restore)
    assert result == saga.pending_result()
    assert proposal["status"] == "pending"
    assert proposal["reads"] == 2
