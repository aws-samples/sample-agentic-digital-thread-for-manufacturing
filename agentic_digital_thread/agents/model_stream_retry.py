# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Retry a Bedrock model stream ONCE when it goes quiet before any content.

The failure (seen live on a rehearsal account, 2026-10-10): ``converse_stream``
returns its HTTP headers, then no event bytes arrive for the client's read
timeout (strands' ``BedrockModel`` default, 120 s). botocore's retries cover
only the request, so the timeout surfaces later, while strands iterates the
event stream, as ``urllib3.exceptions.ReadTimeoutError``. strands' event loop
retries only throttling, so the whole answer fails with "Query failed."

At that point nothing has been handed to the caller and no tool has run (tools
run only after a model call completes), so sending the same request again is
safe. :func:`guard_stream_stalls` wraps a model's ``stream`` so that:

* on the first attempt, leading ``messageStart`` chunks are HELD (they carry no
  content) and released with the first content chunk, so a stall right after
  ``messageStart`` still counts as "nothing sent". The final attempt has no
  retry left, so it passes every chunk straight through;
* a ``urllib3`` read timeout while nothing has been released is retried ONCE
  with the identical arguments, and logged with :data:`STALL_RETRY_LOG_MARKER`,
  unless the turn was cancelled (``Agent.cancel()``) or the caller's
  :func:`retry_window` has less than :data:`STALL_RETRY_MIN_REMAINING_S` left;
* once any content chunk has been released, every error propagates unchanged,
  because a retry would duplicate or corrupt the message the caller is
  assembling;
* every other error (throttling, validation, connection resets, botocore's own
  request-phase timeout, which botocore handles under its retry policy)
  propagates unchanged.

Bounded cost: at most one extra model call, and at most one extra read-timeout
wait, per model call.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import time

logger = logging.getLogger(__name__)

#: Stable text in the retry warning, so a live check can count retries.
STALL_RETRY_LOG_MARKER = "model stream stalled before any content"

#: Total stream attempts per model call: the first one plus one retry.
STALL_MAX_ATTEMPTS = 2

#: Time a caller's window must still have left for a retry to be worth sending:
#: room for the retried model call to answer and the rest of the call (a data
#: tool and the closing model call) to finish. A measured heuristic, not a
#: guarantee: on a rehearsal account, 2026-10-10 (goldens + strict
#: smoke + reruns, 376 Mode-A system calls that completed), the slowest WHOLE
#: system call took 78.5 s, p90 under 46 s per system. The retry's own worst
#: case (a second stall) is not counted: that call fails either way.
STALL_RETRY_MIN_REMAINING_S = 100.0

_GUARD_FLAG = "_adt_stream_stall_guard"

#: ``invocation_state`` key: total seconds this invocation lost to stalls that
#: were retried. A wall-clock budget measured from the start of the question
#: (the system agent's ``_ToolCallBudget``) subtracts it, so a recovered stall
#: does not leave the retried answer with no time for its data tools.
STALL_SECONDS_STATE_KEY = "_adt_stream_stall_seconds"

_window_deadline: contextvars.ContextVar["float | None"] = contextvars.ContextVar(
    "adt_stall_retry_deadline", default=None)


@contextlib.contextmanager
def retry_window(seconds: float):
    """Declare that the caller stops listening *seconds* from now.

    Inside the block a stall is retried only while at least
    :data:`STALL_RETRY_MIN_REMAINING_S` remain. The deadline is a context
    variable, so it follows the work into ``asyncio.to_thread`` and strands'
    ``run_async`` worker thread (both copy the context).
    """
    token = _window_deadline.set(time.monotonic() + float(seconds))
    try:
        yield
    finally:
        _window_deadline.reset(token)


def retry_window_remaining() -> "float | None":
    deadline = _window_deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def _is_stream_stall(exc: BaseException) -> bool:
    """True for the read timeout urllib3 raises while the event stream is being
    read. botocore's ``ReadTimeoutError`` (headers never arrived) is excluded on
    purpose: botocore raises it after applying its own retry policy."""
    from urllib3.exceptions import ReadTimeoutError
    return isinstance(exc, ReadTimeoutError)


def _carries_content(event) -> bool:
    """Everything except a bare ``messageStart`` commits the stream."""
    return not (isinstance(event, dict) and set(event) == {"messageStart"})


def _turn_cancelled(kwargs) -> bool:
    """strands 1.42.0 passes ``invocation_state`` (which carries the agent) to
    ``model.stream``; ``Agent.cancel()`` sets the agent's ``_cancel_signal``."""
    state = kwargs.get("invocation_state")
    agent = state.get("agent") if isinstance(state, dict) else None
    signal = getattr(agent, "_cancel_signal", None)
    is_set = getattr(signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _skip_reason(kwargs) -> "str | None":
    if _turn_cancelled(kwargs):
        return "turn cancelled"
    remaining = retry_window_remaining()
    if remaining is not None and remaining < STALL_RETRY_MIN_REMAINING_S:
        return (f"caller window has {max(remaining, 0.0):.0f} s left, "
                f"a retry needs {STALL_RETRY_MIN_REMAINING_S:.0f} s")
    return None


def _record_stall(kwargs, seconds: float) -> None:
    state = kwargs.get("invocation_state")
    if isinstance(state, dict):
        state[STALL_SECONDS_STATE_KEY] = float(state.get(STALL_SECONDS_STATE_KEY) or 0.0) + seconds


def stalled_seconds(invocation_state) -> float:
    """Seconds this invocation lost to retried stalls (0.0 when none)."""
    if not isinstance(invocation_state, dict):
        return 0.0
    try:
        return max(0.0, float(invocation_state.get(STALL_SECONDS_STATE_KEY) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def is_guarded(model) -> bool:
    return bool(getattr(model, _GUARD_FLAG, False))


def guard_stream_stalls(model):
    """Install the stall retry on *model*'s ``stream`` (in place) and return it.

    The model keeps its class, config and client, so everything that inspects
    it (strands, the cost and model-id plumbing, tests) sees the same object.
    Idempotent; an object without a ``stream`` is returned untouched.
    """
    inner = getattr(model, "stream", None)
    if inner is None or is_guarded(model):
        return model

    async def stream(*args, **kwargs):
        for attempt in range(1, STALL_MAX_ATTEMPTS + 1):
            final = attempt >= STALL_MAX_ATTEMPTS
            held: list = []
            released = final            # the last attempt never holds
            started = time.monotonic()
            try:
                async for event in inner(*args, **kwargs):
                    if not released:
                        if not _carries_content(event):
                            held.append(event)
                            continue
                        released = True
                        for early in held:
                            yield early
                        held = []
                    yield event
            except Exception as exc:  # noqa: BLE001 - re-raised unless it is the one safe case
                if released or final or not _is_stream_stall(exc):
                    raise
                if not _approve_retry(model, kwargs, attempt, exc,
                                      time.monotonic() - started):
                    raise
                continue
            for early in held:          # a stream that ended with no content
                yield early
            return

    model.stream = stream
    setattr(model, _GUARD_FLAG, True)
    return model


def _approve_retry(model, kwargs, attempt: int, exc: BaseException, waited: float) -> bool:
    """Decide whether a stall that sent nothing is retried; log either way and
    record a retried stall's wait on the invocation."""
    skip = _skip_reason(kwargs)
    if skip:
        logger.warning("%s; not retrying: %s (model=%s waited_s=%.1f)",
                       STALL_RETRY_LOG_MARKER, skip, _model_id(model), waited)
        return False
    logger.warning("%s; retrying once (model=%s attempt=%d waited_s=%.1f error=%s)",
                   STALL_RETRY_LOG_MARKER, _model_id(model), attempt, waited,
                   type(exc).__name__)
    _record_stall(kwargs, waited)
    return True


def _model_id(model) -> str:
    config = getattr(model, "config", None)
    if isinstance(config, dict):
        return str(config.get("model_id") or "?")
    return "?"
