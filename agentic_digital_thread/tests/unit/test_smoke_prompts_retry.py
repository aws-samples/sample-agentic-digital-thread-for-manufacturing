# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""smoke_prompts transient-network retry: NARROW, one retry, fresh session.

Pins the exit-code contract around the retry:
  * only dropped-socket/TLS/timeout failures retry (HTTPError NEVER does);
  * exactly ONE retry, on a distinct '-retry' session id (a half-consumed
    first attempt must not leak conversation state into the retry);
  * a second failure propagates as the same request-error row as before.
"""
import http.client
import ssl
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.smoke_prompts import (  # noqa: E402
    App,
    _is_transient_network_error,
    _run_prompt,
    create_session_namespace,
)


def test_transient_predicate_is_narrow():
    assert _is_transient_network_error(ssl.SSLError("record layer failure"))
    assert _is_transient_network_error(http.client.IncompleteRead(b""))
    assert _is_transient_network_error(ConnectionResetError())
    assert _is_transient_network_error(TimeoutError())
    # Connect-phase failures arrive WRAPPED in URLError.reason.
    assert _is_transient_network_error(urllib.error.URLError(ssl.SSLError("x")))
    # An HTTP status is the backend ANSWERING — never retried.
    assert not _is_transient_network_error(
        urllib.error.HTTPError("http://x", 502, "bad gateway", {}, None)
    )
    # DNS-style URLError (OSError reason) stays a hard failure.
    assert not _is_transient_network_error(urllib.error.URLError("name resolution"))
    assert not _is_transient_network_error(ValueError("unrelated"))


def _args():
    return SimpleNamespace(timeout=5, strict=False, quality_strict=False)


def _prompt():
    return {"title": "T", "text": "question"}


class _FlakyApp(App):
    """First ask_stream call drops the TLS record; the second succeeds.

    Bypasses App.__init__ — the class-level login defaults keep it inert."""

    def __init__(self, result):
        self.calls = []
        self._result = result

    def ask_stream(self, question, session_id, timeout):
        self.calls.append(session_id)
        if len(self.calls) == 1:
            raise ssl.SSLError("[SSL] record layer failure")
        return dict(self._result)


GOOD_RESULT = {
    # The real _consume_ask_sse shape (+ elapsed_seconds added by ask_stream).
    "answer": "a" * 80,
    "summary": {"total_tool_calls": 2, "system_queries": 1, "neptune_queries": 0},
    "integrity": {},
    "errors": [],
    "done": True,
    "tool_summaries": [],
    "tool_names": [],
    "tool_starts": 2,
    "elapsed_seconds": 1.0,
}


def test_one_retry_on_transient_error_uses_fresh_session(monkeypatch):
    import scripts.smoke_prompts as sp

    monkeypatch.setattr(sp.time, "sleep", lambda _s: None)
    app = _FlakyApp(GOOD_RESULT)
    namespace = create_session_namespace(run_id="fixed", prefix="smoke")
    row, problems = _run_prompt(app, _args(), namespace, _prompt(), 1, 1)
    assert problems == []
    assert app.calls == ["smoke-fixed-1", "smoke-fixed-1-retry"]
    assert row["session_id"] == "smoke-fixed-1-retry"


def test_second_transient_failure_propagates_as_request_error(monkeypatch):
    import scripts.smoke_prompts as sp

    monkeypatch.setattr(sp.time, "sleep", lambda _s: None)

    class _AlwaysDown(App):

        def __init__(self):

            pass  # bypasses App.__init__; class-level login defaults keep it inert

        calls = 0

        def ask_stream(self, question, session_id, timeout):
            type(self).calls += 1
            raise ssl.SSLError("[SSL] record layer failure")

    app = _AlwaysDown()
    namespace = create_session_namespace(run_id="fixed", prefix="smoke")
    row, problems = _run_prompt(app, _args(), namespace, _prompt(), 1, 1)
    assert type(app).calls == 2  # exactly one retry — never a loop
    assert problems and "request error" in problems[0]
    assert row["done"] is False


def test_http_error_is_never_retried():
    class _Answers502(App):
        def __init__(self):
            pass  # bypasses App.__init__; class-level login defaults keep it inert

        calls = 0

        def ask_stream(self, question, session_id, timeout):
            type(self).calls += 1
            raise urllib.error.HTTPError("http://x", 502, "bad gateway", {}, None)

    app = _Answers502()
    namespace = create_session_namespace(run_id="fixed", prefix="smoke")
    row, problems = _run_prompt(app, _args(), namespace, _prompt(), 1, 1)
    assert type(app).calls == 1  # the backend answered — report, don't mask
    assert problems and "request error" in problems[0]
