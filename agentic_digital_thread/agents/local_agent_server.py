# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A local-mode system agent's MCP server, served from this process.

Local mode (``AGENT_MODE=local``) runs each onboarded system's agent as a
streamable-HTTP MCP server on loopback. Each one gets its own thread and its
own event loop, the way ``FastMCP.run(transport="streamable-http")`` serves
it, so its configuration (connection parameters and password included) stays
a Python object in this process and never reaches a command line.

The thread starts in an empty ``contextvars`` context, as a separate process
did: a provision's lease binding (``lease_guard``) never follows the agent
past the provision that started it. Signals stay with the main thread
(uvicorn installs no handlers off it).
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import uvicorn

logger = logging.getLogger(__name__)


class _Server(uvicorn.Server):
    """A uvicorn server that sets *listening* once it accepts connections."""

    def __init__(self, config: uvicorn.Config, listening: threading.Event):
        super().__init__(config)
        self._listening = listening

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self._listening.set()


class LocalAgentServer:
    """One system agent's MCP server on *host*:*port*, on its own thread.

    *build* returns the agent's ``FastMCP`` app. It runs on the server
    thread, before that thread's event loop starts, so a build that runs an
    event loop of its own (an MCP-type system lists its tools with
    ``asyncio.run``) never meets a running one. *stop_wait* bounds each
    phase of :meth:`stop`.
    """

    def __init__(self, system_id: str, host: str, port: int, build: Callable,
                 stop_wait: float):
        self.system_id, self.host, self.port = system_id, host, port
        self._build, self._stop_wait = build, stop_wait
        self._server: _Server | None = None
        self._error: BaseException | None = None
        self._listening = threading.Event()
        self._finished = threading.Event()
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(
            target=contextvars.Context().run, args=(self._run,),
            name=f"local-agent-{system_id}", daemon=True)

    def start(self, timeout: float) -> None:
        """Start serving and return once the server listens. Raises
        ``RuntimeError`` naming the reason when the build or the bind fails,
        or the server is not listening within *timeout* seconds (it is then
        asked to stop)."""
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._listening.wait(0.05):
            if self._finished.is_set():
                reason = (f"{type(self._error).__name__}: {self._error}"
                          if self._error else "the server exited")
                raise RuntimeError(
                    f"System agent {self.system_id} failed to start: {reason[:500]}")
            if time.monotonic() >= deadline:
                self._stop_requested.set()
                if self._server is not None:
                    self._server.should_exit = True
                raise RuntimeError(f"System agent {self.system_id} failed to start: "
                                   f"not listening after {timeout} s")

    def is_running(self) -> bool:
        """True while the server thread runs."""
        return self._thread.is_alive()

    def stop(self) -> None:
        """Shut the server down: it stops listening at once, its open
        requests get *stop_wait* seconds to finish and are then cancelled.
        Raises ``TimeoutError`` when the thread has still not ended another
        *stop_wait* seconds later (the port is free by then)."""
        self._stop_requested.set()
        server = self._server
        if server is not None:
            server.should_exit = True
        self._thread.join(2 * self._stop_wait)
        if self._thread.is_alive():
            raise TimeoutError(f"Local agent {self.system_id} did not stop")

    def _serve(self, sock: socket.socket) -> None:
        """Serve on *sock* until the server exits, on an event loop of this
        thread's own. ``asyncio.run`` would then wait up to 300 s for the
        loop's ``to_thread`` workers; this ends without waiting for them.

        shortcut: a worker blocked in a connector call (a SQL statement, a
        model call) cannot be interrupted from Python, so it runs on to that
        call's own end (the 60 s PostgreSQL statement timeout, a Bedrock
        read timeout) after the stop, and its result is dropped. A separate
        process could be killed instead; upgrade to one only if a connector
        call ever needs a hard stop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)                     # as asyncio.run does
        executor = ThreadPoolExecutor(thread_name_prefix=f"local-agent-{self.system_id}")
        loop.set_default_executor(executor)
        try:
            loop.run_until_complete(self._server.serve(sockets=[sock]))
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
                asyncio.set_event_loop(None)
                loop.close()

    def _run(self) -> None:
        sock = None
        try:
            sock = socket.create_server((self.host, self.port))
            mcp = self._build()
            config = uvicorn.Config(
                mcp.streamable_http_app(), log_config=None,
                log_level=mcp.settings.log_level.lower(),
                timeout_graceful_shutdown=self._stop_wait)
            self._server = _Server(config, self._listening)
            if self._stop_requested.is_set():   # stopped while building
                return
            self._serve(sock)
        except BaseException as exc:  # noqa: BLE001 - reported by start()
            self._error = exc
            if self._listening.is_set():
                logger.error("Local agent %s server failed: %s", self.system_id, exc)
        finally:
            if sock is not None:
                sock.close()
            self._finished.set()
