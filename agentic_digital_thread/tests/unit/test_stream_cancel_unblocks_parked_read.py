# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cancelling a turn must not wait for the runtime's next bytes.

Live evidence (a rehearsal account, 2026-10-17 15:39-15:40 UTC): a browser
left two /api/ask turns while the orchestrator was waiting on system calls.
On each disconnect the app's event loop stopped for as long as the runtime
stayed silent (15.5 s, then 3.7 s): no request was served, the container
health check was answered late, a scheduler job ran 4.28 s late, and two
browser page loads failed. Each freeze ended the moment the runtime sent its
next bytes, and the abandoned read then died with ``AttributeError``
(``QUERY_FAILURE ... cancelled=yes exc_type=AttributeError
where=response.py:1005:_fp_read``).

Cause: :meth:`StreamCancellation.cancel` closes the invocation's response
body from the disconnect handler, on the event loop. A urllib3 body's
``close()`` takes the same buffer lock as the read the producer thread is
parked in, so it waits until that read returns. The old tests used a fake
body whose ``close()`` released the read at once, which a real socket body
does not do.

These tests drive a REAL urllib3 response (inside botocore's StreamingBody,
as ``invoke_agent_runtime`` returns it) over a loopback socket whose server
goes quiet after the first line. Offline: 127.0.0.1 only.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import urllib3
from botocore.response import StreamingBody

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.chat_client import StreamCancellation  # noqa: E402

# A real socket body is the behavior under test; loopback only, opted out of
# the lane-wide --disable-socket guard the same way test_model_stream_stall_retry is.
pytestmark = pytest.mark.allow_hosts(["127.0.0.1"])

#: How long the server stays silent after its first line. A cancel that
#: waits for the next bytes takes about this long; one that does not, well
#: under a second.
_SILENCE_S = 3.0


class _QuietServer:
    """Loopback HTTP/1.1 server: headers, one chunked line, then silence."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n")
            conn.sendall(b"7\r\nfirst\n\n\r\n")  # a line then a blank line, like an SSE frame
            self._done.wait(_SILENCE_S)
            try:
                conn.sendall(b"6\r\nlater\n\r\n0\r\n\r\n")
            except OSError:
                pass
        finally:
            time.sleep(0.2)
            conn.close()
            self._sock.close()

    def stop(self) -> None:
        self._done.set()


def _open_body(port: int) -> StreamingBody:
    raw = urllib3.PoolManager().request(
        "GET", f"http://127.0.0.1:{port}/", preload_content=False)
    return StreamingBody(raw, None)


def _park_reader(body: StreamingBody) -> tuple[threading.Thread, list]:
    """A producer thread reading lines, parked in the read after line one.
    ``chunk_size=1`` is how ``_CloudStreamState.iter_stream`` reads the
    runtime body (a larger chunk would wait for that many bytes)."""
    got: list = []

    def _read() -> None:
        try:
            for line in body.iter_lines(chunk_size=1):
                got.append(line)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            got.append(exc)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    deadline = time.monotonic() + 3
    while not got and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)  # now blocked waiting for the next chunk
    return t, got


class TestCancelDoesNotWaitForTheRuntime:
    def test_cancel_returns_while_the_producer_is_parked_in_a_real_read(self):
        server = _QuietServer()
        try:
            body = _open_body(server.port)
            reader, got = _park_reader(body)
            assert got and got[0] == b"first"
            handle = StreamCancellation()
            handle.arm(body)

            t0 = time.monotonic()
            handle.cancel()
            took = time.monotonic() - t0

            assert took < 1.0, (
                f"cancel() blocked {took:.2f}s waiting for the runtime's next bytes; "
                "it runs on the event loop, so every request waits with it")
            assert handle.cancelled is True
        finally:
            server.stop()

    def test_the_parked_producer_ends_promptly_after_cancel(self):
        """The point of cancelling: the abandoned read stops now, not when the
        runtime next speaks (the producer would otherwise keep the turn alive)."""
        server = _QuietServer()
        try:
            body = _open_body(server.port)
            reader, got = _park_reader(body)
            handle = StreamCancellation()
            handle.arm(body)

            # cancel() runs on the event loop in the app; time the reader from
            # the moment cancel STARTS, whatever cancel itself does.
            threading.Thread(target=handle.cancel, daemon=True).start()
            reader.join(1.0)

            assert not reader.is_alive(), "the producer is still parked in its read"
            assert b"later" not in got, "the read ended only because more bytes arrived"
        finally:
            server.stop()

    def test_cancel_after_a_normal_release_is_harmless(self):
        """A body already finished and released has no socket to shut: no raise."""
        server = _QuietServer()
        server.stop()
        body = _open_body(server.port)
        list(body.iter_lines())
        handle = StreamCancellation()
        handle.arm(body)
        handle.release()
        handle.cancel()
        assert handle.cancelled is True

    def test_a_body_without_a_socket_is_still_closed(self):
        """Non-urllib3 bodies (the local gate, test fakes) keep the old close."""
        class _Plain:
            closed = False

            def close(self):
                self.closed = True

        body = _Plain()
        handle = StreamCancellation()
        handle.arm(body)
        handle.cancel()
        assert body.closed is True


class _SocketClient:
    """A cloud-shaped chat client: arms the real body, reads it as production does."""

    def __init__(self, port: int) -> None:
        self.port = port

    def stream(self, cancel=None, **kwargs):
        from agents.events import MessageEvent
        body = _open_body(self.port)
        if cancel is not None:
            cancel.arm(body)
        for line in body.iter_lines(chunk_size=1):
            if line:
                yield MessageEvent(content=line.decode())


class _RequestState:
    user = None
    session = None


class _Request:
    state = _RequestState()


def _disconnect_mid_turn() -> tuple[list[float], float]:
    """Drive one /api/ask turn through the production response, leave it
    while the producer is parked in its read, and return the event loop's
    tick gaps during teardown and how long teardown took."""
    import asyncio
    from unittest.mock import patch

    import agents.server as server
    from agents.models import AskRequest

    quiet = _QuietServer()
    first_body = None
    gaps: list[float] = []

    async def _main() -> float:
        nonlocal first_body
        first_body = asyncio.Event()
        with patch.object(server, "get_chat_client", lambda: _SocketClient(quiet.port)):
            response = await server.ask(
                AskRequest(question="is vibration trending up?", session_id="s-loop"), _Request())

        async def _send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()

        async def _receive():
            await first_body.wait()
            await asyncio.sleep(0.2)  # the producer is parked in its read by now
            return {"type": "http.disconnect"}

        done = asyncio.Event()

        async def _ticker():
            last = time.monotonic()
            while not done.is_set():
                await asyncio.sleep(0.05)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(_ticker())
        scope = {"type": "http", "method": "POST", "path": "/api/ask", "headers": [],
                 "asgi": {"version": "3.0", "spec_version": "2.1"}}
        t0 = time.monotonic()
        try:
            await response(scope, _receive, _send)
        finally:
            done.set()
            await ticker
        return time.monotonic() - t0

    try:
        took = asyncio.run(_main())
    finally:
        quiet.stop()

    return gaps, took


class TestDisconnectKeepsTheEventLoopServing:
    """The live symptom, through the production /api/ask response: a browser
    leaves mid-turn and the loop must keep serving everyone else."""

    def test_a_disconnect_does_not_stop_the_event_loop(self):
        from agents.query_admission import get_admission

        gaps, took = _disconnect_mid_turn()
        worst = max(gaps or [0.0])
        assert worst < 0.5, (
            f"the event loop stopped for {worst:.2f}s after the disconnect: no other "
            "request (health check, page load) is served while it waits")
        assert took < 1.5, f"the disconnected response took {took:.2f}s to tear down"
        assert get_admission().busy is False, "the admission token leaked"

    def test_a_close_that_waits_still_leaves_the_event_loop_serving(self, monkeypatch):
        """If waking the parked read ever fails (a socket error, or a future
        botocore that hides the urllib3 response), the close waits for the
        runtime again. That wait must happen off the event loop."""
        import agents.chat_client as chat_client
        from agents.query_admission import get_admission

        monkeypatch.setattr(chat_client, "_shutdown_reads", lambda body: None)
        gaps, _took = _disconnect_mid_turn()
        worst = max(gaps or [0.0])
        assert worst < 0.5, (
            f"the event loop stopped for {worst:.2f}s while a close waited for the "
            "runtime: the turn's teardown must not run that close on the loop")
        assert get_admission().busy is False, "the admission token leaked"


class TestCancelOffLoop:
    """``_cancel_off_loop`` runs the turn's cancel off the event loop without
    losing what the inline call guaranteed."""

    def test_an_error_in_the_cancel_still_reaches_the_teardown(self):
        import asyncio

        import agents.server as server

        class _Owner:
            def cancel(self):
                raise ValueError("teardown failed")

        async def _main():
            with pytest.raises(ValueError, match="teardown failed"):
                await server._cancel_off_loop(_Owner())

        asyncio.run(_main())

    def test_a_cancelled_teardown_still_finishes_the_cancel(self):
        import asyncio

        import agents.server as server

        finished = threading.Event()

        class _Owner:
            def cancel(self):
                time.sleep(0.3)
                finished.set()

        async def _main():
            task = asyncio.create_task(server._cancel_off_loop(_Owner()))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_main())
        assert finished.wait(2.0), "the cancel never ran to the end"


def test_the_app_declares_the_urllib3_that_has_shutdown():
    """``HTTPResponse.shutdown()`` first ships in urllib3 2.3.0; the deployed
    image's hash lock carries it, and pyproject must not let a plain install
    resolve an older urllib3 that would bring the freeze back."""
    import re
    import tomllib

    project = tomllib.loads((_ADT_ROOT / "pyproject.toml").read_text())["project"]
    declared = [d for d in project["dependencies"] if re.match(r"urllib3\b", d)]
    assert len(declared) == 1, declared
    pin = re.fullmatch(r"urllib3==(\d+)\.(\d+)\.\d+", declared[0])
    assert pin and (2, 3) <= (int(pin.group(1)), int(pin.group(2))) < (3, 0), declared
    assert hasattr(urllib3.response.BaseHTTPResponse, "shutdown")
    for lock in ("app", "runtime"):
        text = (_ADT_ROOT / "requirements" / f"{lock}-py312-linux-arm64.lock").read_text()
        pinned = re.search(r"^urllib3==(\d+)\.(\d+)\.", text, re.M)
        assert pinned, lock
        assert (2, 3) <= (int(pinned.group(1)), int(pinned.group(2))) < (3, 0), (lock, pinned.group(0))
