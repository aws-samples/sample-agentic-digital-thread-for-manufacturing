# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Whether one gateway delegation went out, said by the layer that knows.

The per-turn gateway client is a stack of wrappers
(``orchestrator._build_gateway_client``). Some of them answer a call without
sending it: the decline guard and the identifier-provenance guard refuse it
with an error result of their own, and the session keeper returns an error
for a call its closed MCP session refused before sending. A wrapper above
them that records what the system was sent (the route-facts wrapper,
``route_facts.note_call``) cannot tell that from the result: a system's own
error can carry any words. So the layer that decides says how the call went:

* :attr:`CallDelivery.SENT`: the call reached the gateway and the result is
  its answer;
* :attr:`CallDelivery.MAY_HAVE_ACTED`: the call was sent and its session
  closed under it, so the result is the keeper's error, not an answer;
* :attr:`CallDelivery.NOT_SENT`: the call never left the client.

:func:`tracking` opens one call's record for the duration of the call and
:func:`record` writes the outcome of an attempt into it. The record lives in a
context variable, so it is the current task's own: two calls in flight at
once, each its own asyncio task, never share one. It is a mutable object, so a
write from inside the call reaches the opener even when it is made in a copied
context (a task the call started). With no record open, :func:`record` does
nothing. Stdlib only.
"""
from __future__ import annotations

import contextlib
import contextvars
import enum


class CallDelivery(enum.Enum):
    """How one delegation went (see the module docstring)."""

    SENT = "sent"
    MAY_HAVE_ACTED = "may_have_acted"
    NOT_SENT = "not_sent"


class CallRecord:
    """One call's delivery, ``None`` until a layer below records one."""

    __slots__ = ("delivery",)

    def __init__(self) -> None:
        self.delivery: "CallDelivery | None" = None


_OPEN: "contextvars.ContextVar[CallRecord | None]" = contextvars.ContextVar(
    "adt_call_delivery", default=None)


@contextlib.contextmanager
def tracking():
    """Open a record for the call made inside the block, and yield it. The
    record is closed when the block ends, however it ends."""
    opened = CallRecord()
    token = _OPEN.set(opened)
    try:
        yield opened
    finally:
        _OPEN.reset(token)


def record(delivery: CallDelivery) -> None:
    """Write how one attempt of the open call went. A retried call can be
    attempted more than once: an attempt that was not sent leaves an earlier
    attempt's delivery as it was, since that attempt did go out."""
    opened = _OPEN.get()
    if opened is None:
        return
    if delivery is CallDelivery.NOT_SENT and opened.delivery is not None:
        return
    opened.delivery = delivery


def outcome(opened: CallRecord) -> CallDelivery:
    """The call's delivery once it returned: :attr:`CallDelivery.NOT_SENT`
    when no layer recorded one, as no layer below reported sending it."""
    return opened.delivery or CallDelivery.NOT_SENT
