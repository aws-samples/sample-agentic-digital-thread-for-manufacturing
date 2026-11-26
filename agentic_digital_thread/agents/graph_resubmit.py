# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Resubmit a graph query when a new request can fix how it failed.

The neptune-graph client makes ONE attempt per call (``db._neptune_client``,
AWS's guidance for ExecuteQuery), so every error reaches the app and this
module decides what is sent again. Before, the client kept SDK defaults: the
pinned botocore's ``legacy`` mode, 5 attempts, resending a 429, a 5xx or a
read timeout itself with ``amz-sdk-request: attempt=2`` under the same
invocation id, so the app saw only the last error. Neptune Analytics answered
such resends with ``UnprocessableException`` "Retry for SDK query requests is
suppressed, please resubmit the query", which failed a whole provision job on
the 2026-10-21 walk. A resubmit from here is a new SDK call (``attempt=1``,
new invocation id): the resubmit that message asks for.

``run`` resubmits only an attempt that ended in a way a new request can fix
(``failure_kind``). A read is resubmitted after anything but a timeout: the
service asked for a resubmit, throttled, failed with a 500 or 503, aborted a
conflict, no connection was made, or the connection dropped. A write is
resubmitted only when it certainly never ran: a 429 throttle, or no
connection was made. After a 422 resubmit request, a 500, a 503 or a 409
conflict a write may or may not have taken effect, so it is resubmitted then
only when its call site states that it is safe to run twice
(``db.resubmit_safe_write``); whether a write is safe to run twice is never
judged from its text. Nothing is resubmitted after a timeout, and a write
after a dropped connection only when its call site also states that a copy
still running alongside the resend leaves the graph as one run
(``db.resubmit_safe_write(after_drop=True)``). Every other error (a
validation error, a denial, a server-side query timeout or limit) is raised
on the first attempt as before. It resubmits at most
``MAX_ATTEMPTS - 1`` times, with jittered exponential backoff, and starts no
resubmit that would end past ``TOTAL_BOUND_S`` from the first attempt.
"""
from __future__ import annotations

import random
import re
import time

from botocore import exceptions as _bce

#: Attempts in total, the first one included.
MAX_ATTEMPTS = 3
#: Backoff before resubmit n is drawn from [step/2, step], where
#: step = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (n - 1)).
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 4.0
#: No resubmit starts if its backoff would end past this, counted from the
#: start of the first attempt.
TOTAL_BOUND_S = 30.0

# Call-time indirection so tests can drive the clock without patching the
# process-wide ``time`` module.
_sleep = time.sleep
_monotonic = time.monotonic
_random = random.random

_RESUBMIT_CODE = "UnprocessableException"


def is_resubmit_request(exc: BaseException) -> bool:
    """Whether ``exc`` is the backend asking for the query to be resubmitted:
    a botocore error with code ``UnprocessableException`` whose message says
    to resubmit. The other ``UnprocessableException`` reasons (query timeout,
    memory, storage or internal limit, partition full) do not ask for that and
    would not be helped by running the query again."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error") or {}
    if error.get("Code") != _RESUBMIT_CODE:
        return False
    return "resubmit" in str(error.get("Message") or "").lower()


#: How an attempt ended, for the errors the app may resubmit.
#:
#: ``REJECTED``: the service throttled the request (HTTP 429) and did not run
#: it (Neptune Analytics answers ThrottlingException when its query queue is
#: full). ``NOT_SENT``: no connection was made, so the request never reached
#: it. These two are the only ends after which the query certainly never ran.
#: ``FAILED``: the service answered and is done with the request, but the
#: query may have started: it failed it (500), was unavailable (503), or asked
#: for it to be resubmitted (422). ``CONFLICT``: it aborted the query on a
#: concurrent modification (409). ``DROPPED``: the connection broke after the
#: request may have reached it (closed connection, TLS error, 502), so the
#: first attempt may still be running. ``TIMED_OUT``: the query outlasted the
#: wait (read timeout, 504) and may still be running. Any other failure while
#: sending (botocore's bare ``HTTPClientError``) is none of these and is not
#: resubmitted.
REJECTED, FAILED, CONFLICT, NOT_SENT, DROPPED, TIMED_OUT = (
    "rejected", "failed", "conflict", "not_sent", "dropped", "timed_out")
#: The ends after which the query certainly never ran, so any write may be
#: sent again.
NEVER_RAN = frozenset({REJECTED, NOT_SENT})
#: The further ends after which a write its call site states is safe to run
#: twice (``db.resubmit_safe_write``) is sent again: the service is done with
#: the request, but whether the query took effect is open.
OPT_IN_ENDS = frozenset({FAILED, CONFLICT})
#: The ends for a write whose call site also states that a copy of it still
#: running alongside the resend leaves the graph as one run
#: (``db.resubmit_safe_write(after_drop=True)``): ``OPT_IN_ENDS`` and a
#: dropped connection. Never a timeout.
OPT_IN_AFTER_DROP_ENDS = OPT_IN_ENDS | {DROPPED}

# A throttle code is REJECTED by its 429 status; one carrying another status
# (not something neptune-graph is modeled to send) counts as FAILED, so a
# write is not sent again after it without the opt-in.
_FAILED_CODES = frozenset({
    "ThrottlingException", "Throttling", "ThrottledException", "TooManyRequestsException",
    "RequestThrottledException", "RequestLimitExceeded", "InternalServerException",
    "InternalServerError", "InternalFailure", "ServiceUnavailable",
    "ServiceUnavailableException",
})
_FAILED_STATUS = frozenset({429, 500, 503})


def error_facts(exc: BaseException) -> "tuple[str, str, str]":
    """``(code, http_status, reason)`` of ``exc``, ``-`` where it has none.
    A modeled neptune-graph error carries its ``reason`` (QUERY_TIMEOUT,
    CONCURRENT_MODIFICATION, ...) at the top of the parsed response."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return type(exc).__name__, "-", "-"
    code = (response.get("Error") or {}).get("Code") or type(exc).__name__
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or "-"
    return str(code), str(status), str(response.get("reason") or "-")


def failure_kind(exc: BaseException) -> "str | None":
    """How the attempt that raised ``exc`` ended (``REJECTED``, ``FAILED``,
    ``CONFLICT``, ``NOT_SENT``, ``DROPPED`` or ``TIMED_OUT``), or ``None`` for
    an error a resubmit would not help: a rejected query, a denial, a
    server-side query timeout or limit, or anything that is not a botocore
    error."""
    if is_resubmit_request(exc):
        return FAILED
    if isinstance(exc, _bce.ReadTimeoutError):
        return TIMED_OUT
    if isinstance(exc, (_bce.EndpointConnectionError, _bce.ConnectTimeoutError,
                        _bce.ProxyConnectionError)):
        return NOT_SENT
    # A closed connection or a TLS failure (the connection errors botocore's
    # default ``legacy`` mode resent). A bare ``HTTPClientError`` is botocore
    # wrapping an unexpected exception while sending; legacy mode never resent
    # it, and neither does this.
    if isinstance(exc, (_bce.ConnectionClosedError, _bce.ConnectionError)):
        return DROPPED
    if not isinstance(getattr(exc, "response", None), dict):
        return None
    code, status, _ = error_facts(exc)
    if status == "429":
        return REJECTED
    if status == "504":
        return TIMED_OUT
    if status == "502":
        return DROPPED
    if code == "ConflictException" or status == "409":
        return CONFLICT
    if code in _FAILED_CODES or (status.isdigit() and int(status) in _FAILED_STATUS):
        return FAILED
    return None


# String literals, quoted identifiers and comments, blanked out first so a
# keyword inside one (a property value "SET later") is not read as a clause.
_OPAQUE_RE = re.compile(
    r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`|//[^\n]*|/\*.*?\*/", re.S)
# Every openCypher clause that can change the graph (FOREACH only holds these),
# and the ones that run code this check cannot see (a procedure, a subquery,
# a load).
_WRITE_CLAUSE_RE = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE|CALL|LOAD)\b", re.I)


def writes(cypher: str) -> bool:
    """Whether ``cypher`` may change the graph: it has a write clause
    (``CREATE``, ``MERGE``, ``SET``, ``DELETE``, ``REMOVE``) or a ``CALL`` or
    ``LOAD`` outside string literals, quoted names and comments, or it is
    empty. Only a statement with none of them is a read; this decides that a
    statement changes nothing, never that a write is safe to run twice."""
    text = _OPAQUE_RE.sub(" ", cypher or "")
    return not text.strip() or bool(_WRITE_CLAUSE_RE.search(text))


def _plan(kind: str, write: bool, resubmit_safe: bool, attempt: int,
          started: float, after_drop: bool = False) -> "tuple[float, str]":
    """``(delay, "")`` to resubmit after an attempt that ended as ``kind``
    (``failure_kind``), or ``(0, "<log phrase>: <why>")`` when it is not
    resubmitted:

    * ``TIMED_OUT``: nothing, since the query may still be running and sending
      it again would double both the wait and the load;
    * a read: after any other end;
    * a write: after ``REJECTED`` (429) or ``NOT_SENT``, since it certainly
      never ran; after ``FAILED`` (a 422 resubmit request, a 500, a 503) or
      ``CONFLICT`` (409) only when ``resubmit_safe``, the caller's statement
      that it has the same effect run twice as once; after ``DROPPED`` only
      when ``after_drop`` too, the caller's statement that a copy still
      running alongside the resend leaves the graph as one run;
    * nothing once ``MAX_ATTEMPTS`` or ``TOTAL_BOUND_S`` would be passed."""
    if kind == TIMED_OUT:
        return 0, "not resubmitted: the first attempt timed out and may still be running"
    opted = (OPT_IN_AFTER_DROP_ENDS if after_drop else OPT_IN_ENDS) if resubmit_safe else ()
    if write and kind not in NEVER_RAN and kind not in opted:
        return 0, "not resubmitted: the write may have run or still be running"
    if attempt >= MAX_ATTEMPTS:
        return 0, f"resubmit gave up: attempts={attempt}"
    step = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1))
    delay = step / 2 + _random() * step / 2
    if _monotonic() - started + delay > TOTAL_BOUND_S:
        return 0, f"resubmit gave up: attempts={attempt}, past the {TOTAL_BOUND_S:.0f}s bound"
    return delay, ""


def run(execute, cypher: str, *, log, resubmit_safe: bool = False, after_drop: bool = False):
    """Call ``execute()`` and answer what it returns. When it raises, call it
    again (a new request) only as ``_plan`` allows; ``resubmit_safe`` is the
    caller's statement that ``cypher`` has the same effect run twice as once,
    ``after_drop`` that a copy still running alongside the resend does too.
    Every other error, and the last one once the bounds are spent, propagates
    unchanged. Each resubmit logs one line, the first also naming the first
    attempt's error code, HTTP status and ``reason`` (which SDK retries used
    to hide); a transient error that is not resubmitted logs one line saying
    why."""
    started, attempt, write, first = _monotonic(), 1, None, None
    while True:
        try:
            return execute()
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is resubmitted
            kind = failure_kind(exc)
            if kind is None:
                raise
            facts = (type(exc).__name__, *error_facts(exc))
            first = first or facts[1:]
            write = writes(cypher) if write is None else write
            delay, refusal = _plan(kind, write, resubmit_safe, attempt, started, after_drop)
            if refusal:
                log.warning("Neptune query %s category=%s code=%s status=%s reason=%s",
                            refusal, *facts)
                raise
            note = (" first_error=%s first_status=%s first_reason=%s" % first
                    if attempt == 1 else "")
            attempt += 1
            log.warning("Neptune query resubmitted category=%s code=%s status=%s reason=%s "
                        "attempt=%d/%d after=%.2fs%s", *facts, attempt, MAX_ATTEMPTS,
                        delay, note)
            _sleep(delay)
