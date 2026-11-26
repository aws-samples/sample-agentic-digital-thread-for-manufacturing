# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Suppressed stream errors must leave a payload-safe fingerprint.

The client only ever sees "Query failed."; before this, the original error was
discarded, so a live investigation could not tell which error fired. The log
line must never carry measurements — an error string can quote row data, and the
claim-check policy keeps telemetry out of logs as well as the token path.
"""
from __future__ import annotations

from agents.runtime_app import _redacted_error_head


class TestRedactedErrorHead:
    def test_digits_are_masked(self):
        out = _redacted_error_head("batch B-2847 value 98.6 rows=15")
        assert "2847" not in out and "98.6" not in out
        assert "batch B-####" in out

    def test_keeps_the_diagnostic_shape(self):
        """Masking must not destroy WHICH error fired."""
        out = _redacted_error_head("ThrottlingException: rate exceeded")
        assert "ThrottlingException" in out

    def test_truncates(self):
        assert len(_redacted_error_head("x" * 500)) == 120

    def test_handles_non_string_errors(self):
        assert _redacted_error_head({"code": 500}) == "{'code': ###}"
        assert _redacted_error_head(None) == "None"


class TestConsumeLogsSuppressedError:
    def test_error_event_is_logged_then_scrubbed(self, caplog):
        """The relayed text stays scrubbed; the log keeps the fingerprint."""
        import logging

        from agents.runtime_app import _RuntimeIntegrityInvocation

        state = _RuntimeIntegrityInvocation.__new__(_RuntimeIntegrityInvocation)
        state.error_seen = False
        with caplog.at_level(logging.ERROR, logger="agents.runtime_app"):
            out = state.consume({"error": "ValidationException: bad field 42"})
        assert out == [{"error": "Query failed."}]
        assert state.error_seen is True
        rec = " ".join(r.getMessage() for r in caplog.records)
        assert "STREAM_ERROR_EVENT" in rec
        assert "ValidationException" in rec      # which error still visible
        assert "42" not in rec                   # the measurement is not


class TestRuntimeErrorFrameFingerprint:
    """The runtime's error TEXT is visible in exactly one place.

    Downstream, a server-side filter replaces the frame with a generic
    "Query failed."; upstream, the runtime scrubs its own copy. With both ends
    opaque a reproducible failure cannot be named, so the parse site logs a
    payload-safe fingerprint.
    """

    def test_redact_digits_keeps_the_error_class_and_drops_values(self):
        from agents.chat_client import _redact_digits
        out = _redact_digits("ThrottlingException on batch B-2847 qty 98.6")
        assert "ThrottlingException" in out
        assert "2847" not in out and "98.6" not in out

    def test_redact_digits_truncates(self):
        from agents.chat_client import _redact_digits
        assert len(_redact_digits("x " * 450)) == 160

    def test_redact_digits_withholds_one_token_too_long_to_judge(self):
        """Past the redaction window an unbroken token could be a partial
        secret no pattern matches, so none of it is logged."""
        from agents.chat_client import _redact_digits
        from agents.failure_fingerprint import LONG_TOKEN_WITHHELD
        assert _redact_digits("x" * 900) == LONG_TOKEN_WITHHELD

    def test_error_frame_logs_fingerprint_and_still_returns_the_event(self, caplog):
        import logging

        from agents.chat_client import _dict_to_sse_event
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            ev = _dict_to_sse_event({"event": "error",
                                     "data": {"text": "ValidationException: field 42 bad"}})
        rec = " ".join(r.getMessage() for r in caplog.records)
        assert "RUNTIME_ERROR_FRAME" in rec
        assert "ValidationException" in rec
        assert "42" not in rec
        # the event carries ONLY the fixed per-category public text — the
        # digit-masked log line is the sole survivor of the runtime's text.
        from agents.events import public_error_message
        assert ev.category == "runtime_error"
        assert ev.message == public_error_message("runtime_error")
        assert "ValidationException" not in ev.message and "42" not in ev.message


class TestAskStreamWorkerLogsBeforeScrub:
    """The /api/ask worker's except was the last holder of the real failure.

    It emitted ErrorEvent(str(e)) and logged NOTHING, while the downstream frame
    filter replaced the public frame with a generic "Query failed." — so the
    exception was invisible on both sides.
    """

    def test_worker_logs_type_and_masked_message_then_still_emits(self, caplog):
        import logging

        from agents.server import _start_stream_worker

        class Boom:
            def stream(self, **kwargs):
                yield {"event": "token", "data": {"text": "partial"}}
                raise RuntimeError("ValidationException: rows=42 value 98.6")

        with caplog.at_level(logging.ERROR, logger="agents.server"):
            # The producer now returns a handle (bounded queue + stop flag, P0-A).
            worker = _start_stream_worker(Boom(), "q", "s", "u", False, None)
            drained = []
            while True:
                item = worker.queue.get(timeout=10)
                if item is None:
                    break
                drained.append(item)

        rec = " ".join(r.getMessage() for r in caplog.records)
        assert "ASK_STREAM_WORKER_FAILED" in rec
        assert "RuntimeError" in rec
        assert "ValidationException" in rec   # the class survives
        # The values do not. Only the message head is checked: ``where=`` names
        # file:line:function frames, whose LINE NUMBERS are code, not data.
        head = rec.split(" head=", 1)[1]
        assert "42" not in head and "98.6" not in head
        assert "where=" in rec and ":stream" in rec   # the raising frame is named
        # the error frame is still produced for the client
        assert any(type(x).__name__ == "ErrorEvent" for x in drained)


class TestGenericErrorDictFingerprint:
    """The runtime's raw error dict is the last unlogged origin.

    An 8-cell interleaved A/B produced 6 failures with the other three candidate
    sites instrumented and SILENT, so the dict arrives here. The KEY SET is the
    useful part — it names the emitting path, which the scrubbed message cannot.
    """

    def test_logs_key_set_and_masked_error_then_scrubs(self, caplog):
        import logging

        from agents.chat_client import _CloudStreamState

        state = _CloudStreamState.__new__(_CloudStreamState)
        state.runtime_error_seen = False
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            out = state._generic_events({"error": "Boom rows=42", "sessionId": "s"})

        rec = " ".join(r.getMessage() for r in caplog.records)
        assert "GENERIC_ERROR_DICT" in rec
        assert "sessionId" in rec                  # key set names the path
        assert "42" not in rec                     # values masked
        # Fixed per-category public text (never the raw error) — see events.py.
        from agents.events import public_error_message
        assert out[0].category == "runtime_error"
        assert out[0].message == public_error_message("runtime_error")
        assert "42" not in out[0].message

    def test_second_error_is_suppressed_and_not_relogged(self, caplog):
        import logging

        from agents.chat_client import _CloudStreamState

        state = _CloudStreamState.__new__(_CloudStreamState)
        state.runtime_error_seen = True
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            out = state._generic_events({"error": "second"})
        assert out == []
        assert "GENERIC_ERROR_DICT" not in " ".join(r.getMessage() for r in caplog.records)


class TestClassifyErrorTextShapes:
    """Closed taxonomy is selected by protocol/exception SHAPES — never by a
    quoted data value (error-taxonomy invariant)."""

    def test_observed_gateway_403_is_gateway_auth(self):
        from agents.chat_client import classify_error_text
        text = ("Failed to load tool <strands.tools.mcp.mcp_client.MCPClient object at 0x1>: "
                "Failed to start MCP client: the client initialization failed: unhandled errors "
                "in a TaskGroup: Client error '403 Forbidden' for url 'https://gw/mcp'")
        assert classify_error_text(text) == "gateway_auth"

    def test_quoted_data_values_never_classify(self):
        from agents.chat_client import classify_error_text
        for text in (
            "QueryError: column status has value 'Forbidden' in 3 rows",
            "row failed: cell_state=403, site=Unauthorized-Zone",
            "field 'timeout' is not a known column (CONNECTION_TIMEOUT counted 112)",
            "value 'MCP client' rejected by schema",
        ):
            assert classify_error_text(text) == "runtime_error", text

    def test_timeout_shapes(self):
        from agents.chat_client import classify_error_text
        assert classify_error_text("httpx.ReadTimeout: timed out") == "timeout"
        assert classify_error_text('{"error_type": "query_timeout"}') == "timeout"
        assert classify_error_text("MaxTokensReachedException mid tool-use") == "runtime_error"
