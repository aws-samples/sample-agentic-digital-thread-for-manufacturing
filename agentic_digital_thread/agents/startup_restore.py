# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Whether the startup restore is still rebuilding the onboarded systems in
the background, and the refusal of the writes that would race it.

The app starts serving before a slow startup restore ends (``server.lifespan``,
``server.STARTUP_RESTORE_WAIT_S``). Until it ends, a write to what it is
rebuilding (catalog entries, registered agents, a system's runtime, gateway
target, connection and agent config, the knowledge-graph System and schema
records) can be overwritten by the restore or leave a record the restore then
contradicts. The routes and in-app chat tools that make such writes answer a
retryable refusal instead (:func:`refuse_while_restoring`, :func:`refusal`).
Reads are not refused: they may see only the systems restored so far.
``tests/unit/test_startup_restore_writers.py`` finds every route that reaches
such a write and fails when one carries no refusal and no recorded reason.

The 503 body carries ``reason`` (:data:`RESTORING_REASON`) beside the plain
words, so a client tells it from another 503 on the same route without
matching text (:func:`install` registers the response on the app).

Set only by ``server._AfterRestore``: from the moment the app starts serving
with the restore still running until the restore and the startup steps that
waited for it off the event loop have ended, or the app shuts down.
"""

import threading

from fastapi import HTTPException
from fastapi.responses import JSONResponse

#: The refusal's text: the 503 ``detail`` and the chat tools' ``error``.
RESTORING_DETAIL = ("The app is still restoring onboarded systems after a restart. "
                    "Try again in a minute.")
#: The stable ``reason`` of the refusal: in the 503 body and a chat tool's answer.
RESTORING_REASON = "startup_restore"
#: Seconds the 503 asks a client to wait (``Retry-After``), matching the text.
RETRY_AFTER_S = 60

_restoring = threading.Event()


class StartupRestoring(HTTPException):
    """The 503 a refused route raises (answered by :func:`restoring_response`)."""

    def __init__(self) -> None:
        super().__init__(status_code=503, detail=RESTORING_DETAIL,
                         headers={"Retry-After": str(RETRY_AFTER_S)})


async def restoring_response(_request, exc: StartupRestoring) -> JSONResponse:
    """``{"detail": ..., "reason": "startup_restore"}`` with ``Retry-After``."""
    return JSONResponse({"detail": exc.detail, "reason": RESTORING_REASON},
                        status_code=exc.status_code, headers=exc.headers)


def install(app) -> None:
    """Answer :class:`StartupRestoring` with :func:`restoring_response` on ``app``.
    Without it the refusal is still a 503 with the detail (FastAPI's
    HTTPException response), only without ``reason``."""
    app.add_exception_handler(StartupRestoring, restoring_response)


def begin() -> None:
    """The app serves while the startup restore still runs: refuse the writes."""
    _restoring.set()


def end() -> None:
    """The startup restore ended (or the app is shutting down)."""
    _restoring.clear()


def restoring() -> bool:
    return _restoring.is_set()


def refuse_while_restoring() -> None:
    """Route dependency (or in-body call): a retryable 503 while the startup
    restore runs. On an admin route call it after the admin check, so a
    non-admin keeps getting 403 (``admin_routes.admin_refuse_while_restoring``)."""
    if _restoring.is_set():
        raise StartupRestoring()


def refusal(system_id: str = "") -> "dict | None":
    """A chat tool's answer while the startup restore runs, else ``None``."""
    if not _restoring.is_set():
        return None
    answer = {"error": RESTORING_DETAIL, "reason": RESTORING_REASON}
    if system_id:
        answer["system_id"] = system_id
    return answer
