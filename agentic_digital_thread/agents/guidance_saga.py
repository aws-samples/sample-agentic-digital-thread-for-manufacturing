# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Exact-replacement saga shared by guidance accept, prune, and clear."""
from __future__ import annotations

import logging

from .guidance_scope import delivery_document
from .system_guidance import GuidanceSnapshot

logger = logging.getLogger(__name__)

_ERROR = "guidance_update_failed"


def note_stopped(stage: str, exc: "BaseException | None" = None,
                 reason: str = "result did not match") -> None:
    """Log the one check that stopped a guidance update and why: the error
    class it raised, else ``reason``. Never guidance text; the caller's
    reply stays the fixed opaque shape."""
    logger.warning("Guidance update stopped at %s (%s)", stage,
                   type(exc).__name__ if exc is not None else reason)


def pending_result(*, graph_drift: bool = False,
                   delivery_drift: bool = False,
                   proposal_drift: bool = False) -> dict:
    """One fixed opaque failure shape; source drift remains distinguishable.
    ``proposal`` is set when an accepted proposal status could not be proven
    back to pending after the rest of the update was undone."""
    return {"status": "pending", "error": _ERROR,
            "drift": {"graph": bool(graph_drift),
                      "delivery": bool(delivery_drift),
                      "proposal": bool(proposal_drift)}}


def _same_content(left: GuidanceSnapshot,
                  right: GuidanceSnapshot) -> bool:
    return (left.text == right.text and left.digest == right.digest
            and left.exists == right.exists)


def _delivery_target(graph_text: str) -> GuidanceSnapshot:
    return GuidanceSnapshot.present(delivery_document(graph_text), 0)


def _delivery_parity(graph_text: str, delivery: GuidanceSnapshot) -> bool:
    target = _delivery_target(graph_text)
    if _same_content(delivery, target):
        return True
    if delivery.exists:
        return False
    from .guidance_scope import scoped_entries
    return not scoped_entries(target.text, plain_is_base=False)


def _read_or_none(reader):
    return _attempt(reader)[0]


def _attempt(call):
    """``(value, None)``, or ``(None, the exception)`` when ``call`` raised."""
    try:
        return call(), None
    except Exception as exc:
        return None, exc


def _preflight(graph_before: str, delivery_before: GuidanceSnapshot,
               graph_read, delivery_read) -> dict | None:
    graph_current, graph_error = _attempt(graph_read)
    delivery_current, delivery_error = _attempt(delivery_read)
    graph_drift = graph_current != graph_before
    delivery_drift = (delivery_current != delivery_before
                      or not _delivery_parity(graph_before, delivery_before))
    if graph_drift:
        note_stopped("graph check before writing", graph_error)
    elif delivery_drift:
        note_stopped("delivery check before writing", delivery_error)
    if graph_drift or delivery_drift:
        return pending_result(graph_drift=graph_drift,
                              delivery_drift=delivery_drift)
    return None


def _apply_graph(before: str, after: str, graph_cas, graph_read) -> bool:
    stage = "graph write"
    try:
        if graph_cas(before, after):
            stage = "graph readback"
            if graph_read() == after:
                return True
        note_stopped(stage)
    except Exception as exc:
        note_stopped(stage, exc)
    return False


def _apply_delivery(before: GuidanceSnapshot, target: GuidanceSnapshot,
                    delivery_cas, delivery_read):
    stage = "delivery write"
    try:
        written = delivery_cas(before, target)
        stage = "delivery readback"
        observed = delivery_read()
        if not _same_content(written, target) or observed != written:
            note_stopped(stage)
            return None
        return written
    except Exception as exc:
        note_stopped(stage, exc)
        return None


def _restore_delivery(expected: GuidanceSnapshot | None,
                      before: GuidanceSnapshot, delivery_cas,
                      delivery_read) -> bool:
    observed = _read_or_none(delivery_read)
    if observed == before:
        return True
    if expected is None or observed != expected:
        return False
    try:
        restored = delivery_cas(expected, before)
        readback = delivery_read()
        return readback == restored and _same_content(readback, before)
    except Exception:
        return False


def _restore_graph(expected: str, before: str,
                   graph_cas, graph_read) -> bool:
    observed = _read_or_none(graph_read)
    if observed == before:
        return True
    if observed != expected:
        return False
    try:
        return bool(graph_cas(expected, before)) and graph_read() == before
    except Exception:
        return False


def _compensate(*, graph_after, graph_before, delivery_written,
                delivery_before, graph_cas, graph_read,
                delivery_cas, delivery_read) -> dict:
    """Reverse causal writes without overwriting an unrelated current state."""
    delivery_ok = _restore_delivery(
        delivery_written, delivery_before, delivery_cas, delivery_read)
    graph_ok = _restore_graph(graph_after, graph_before, graph_cas, graph_read)
    return pending_result(graph_drift=not graph_ok,
                          delivery_drift=not delivery_ok)


def _terminal_ok(terminal_cas, terminal_read, graph_after: str,
                 delivery_written: GuidanceSnapshot) -> bool:
    if terminal_cas is None:
        return True
    stage = "proposal status write"
    try:
        if terminal_cas(graph_after, delivery_written):
            stage = "proposal status readback"
            if terminal_read() == "accepted":
                return True
        note_stopped(stage)
    except Exception as exc:
        note_stopped(stage, exc)
    return False


def _restore_terminal(terminal_restore, terminal_read) -> bool:
    """True only when the proposal status is proven pending again: the
    restore step reports it and a fresh read agrees (with no restore step,
    the read alone). Anything else is logged at the check that stopped."""
    stage = "proposal status restore"
    try:
        if terminal_restore is None or terminal_restore():
            stage = "proposal status restore readback"
            if terminal_read() == "pending":
                return True
        note_stopped(stage)
    except Exception as exc:
        note_stopped(stage, exc)
    return False


def _success(graph_after: str, delivery_written: GuidanceSnapshot) -> dict:
    from .system_guidance import guidance_digest
    return {"status": "synchronized", "error": None,
            "drift": {"graph": False, "delivery": False, "proposal": False},
            "graph_digest": guidance_digest(graph_after),
            "delivery_digest": delivery_written.digest,
            "delivery_version": delivery_written.version}


def replace_exact(*, graph_before: str, graph_after: str,
                  delivery_before: GuidanceSnapshot, graph_cas, graph_read,
                  delivery_cas, delivery_read, terminal_cas=None,
                  terminal_read=None, terminal_restore=None) -> dict:
    """Prove parity, replace graph and delivery, then commit terminal status."""
    preflight = _preflight(
        graph_before, delivery_before, graph_read, delivery_read)
    if preflight:
        return preflight
    if graph_before == graph_after:
        return _success(graph_after, delivery_before)
    if not _apply_graph(graph_before, graph_after, graph_cas, graph_read):
        graph_ok = _restore_graph(
            graph_after, graph_before, graph_cas, graph_read)
        return pending_result(graph_drift=not graph_ok)
    target = _delivery_target(graph_after)
    delivery_written = _apply_delivery(
        delivery_before, target, delivery_cas, delivery_read)
    if delivery_written is None:
        return _compensate(
            graph_after=graph_after, graph_before=graph_before,
            delivery_written=None, delivery_before=delivery_before,
            graph_cas=graph_cas, graph_read=graph_read,
            delivery_cas=delivery_cas, delivery_read=delivery_read)
    if not _terminal_ok(
            terminal_cas, terminal_read, graph_after, delivery_written):
        failure = _compensate(
            graph_after=graph_after, graph_before=graph_before,
            delivery_written=delivery_written, delivery_before=delivery_before,
            graph_cas=graph_cas, graph_read=graph_read,
            delivery_cas=delivery_cas, delivery_read=delivery_read)
        restored = _restore_terminal(terminal_restore, terminal_read)
        return pending_result(graph_drift=failure["drift"]["graph"],
                              delivery_drift=failure["drift"]["delivery"],
                              proposal_drift=not restored)
    return _success(graph_after, delivery_written)
