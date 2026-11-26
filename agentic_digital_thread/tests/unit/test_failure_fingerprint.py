# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Failures the user sees as "Query failed." must be NAMED in the log, payload-free.

``QUERY_FAILURE`` used to carry only the internal catch-all category, so a read
timeout, a connection the service closed mid-chunk and a bug in our own frame
parser were indistinguishable (13 occurrences on one rehearsal account in four
days, none diagnosable). These tests pin what the line now says (phase, type,
cause, frames, AWS code, timings, last frame shape, runtime session) and that
it still carries no data: digits in the message are masked, frame keys outside
the allowlist are never named, and the fingerprint never raises.
"""
from __future__ import annotations

import logging
import re

import pytest

from agents import failure_fingerprint as fp


def _head(record: str) -> str:
    """The ``head=`` tail of a fingerprint line (frames legitimately carry line numbers)."""
    return record.split(" head=", 1)[1] if " head=" in record else ""


class _ProtocolError(Exception):
    pass


def _inner_read():
    raise _ProtocolError("Connection broken: IncompleteRead(1234 bytes read, 98 more expected)")


def _outer_read():
    try:
        _inner_read()
    except _ProtocolError as exc:
        raise RuntimeError("stream torn down at row 42") from exc


class TestExceptionFingerprint:
    def test_names_type_cause_and_frames_but_masks_message_digits(self):
        with pytest.raises(RuntimeError) as info:
            _outer_read()
        out = fp.exception_fingerprint(info.value)
        assert out["exc_type"] == "RuntimeError"           # builtins stay short
        assert out["cause"].endswith("_ProtocolError")     # the real cause survives
        assert "test_failure_fingerprint.py:" in out["where"]
        assert out["where"].endswith(":_outer_read")
        assert out["head"] == "stream torn down at row ##"
        assert not re.search(r"\d", out["head"])

    def test_aws_code_status_and_request_id(self):
        exc = Exception("throttled")
        exc.response = {"Error": {"Code": "ThrottlingException"},
                        "ResponseMetadata": {"HTTPStatusCode": 429,
                                             "RequestId": "abc-123-def"}}
        out = fp.exception_fingerprint(exc)
        assert (out["code"], out["http"], out["request_id"]) == (
            "ThrottlingException", "429", "abc-123-def")

    def test_non_identifier_aws_fields_are_dropped(self):
        exc = Exception("x")
        exc.response = {"Error": {"Code": "bad code with value 98.6"},
                        "ResponseMetadata": {"HTTPStatusCode": "500",
                                             "RequestId": "has spaces in it"}}
        out = fp.exception_fingerprint(exc)
        assert (out["code"], out["http"], out["request_id"]) == ("", "", "")

    def test_unprintable_exception_keeps_its_type(self):
        class Unprintable(Exception):
            def __str__(self):
                raise ValueError("no")

        out = fp.exception_fingerprint(Unprintable())
        assert out["exc_type"].endswith("Unprintable")
        assert out["head"] == "<unprintable>"

    def test_never_raises_on_hostile_input(self):
        class Hostile(Exception):
            @property
            def __traceback__(self):  # pragma: no cover - property access is the test
                raise RuntimeError("boom")

        out = fp.exception_fingerprint(Hostile("x"))
        assert set(out) >= {"exc_type", "cause", "where", "head"}

    def test_head_is_one_line_and_bounded(self):
        assert fp.masked_head("a\nb\t c" + "x" * 500) .startswith("a b c")
        assert len(fp.masked_head("y" * 500)) == 160


class TestFrameLabel:
    def test_known_keys_are_named_unknown_ones_only_counted(self):
        assert fp.frame_label({"data": "hi", "customer_ANYCOMPANY_42": 1}) == "data+other"

    def test_nested_event_names_only_allowlisted_inner_keys(self):
        label = fp.frame_label({"event": {"contentBlockDelta": {}, "row_98": 1}})
        assert label == "event.contentBlockDelta"

    def test_shapes(self):
        assert fp.frame_label([]) == "non-dict"
        assert fp.frame_label({}) == "empty"
        assert fp.frame_label({"zzz": 1}) == "other"


class TestFormatFields:
    def test_one_token_per_value_and_head_last(self):
        line = fp.format_fields({"head": "two words", "a": "x y", "b": "", "c": 3})
        assert line == "a=x_y b=- c=3 head=two words"


class _Body:
    def __init__(self, lines, exc):
        self._lines, self._exc, self.closed = lines, exc, False

    def iter_lines(self, chunk_size=None):
        yield from self._lines
        raise self._exc

    def close(self):
        self.closed = True


def _state():
    from agents.chat_client import ThinkingFilter, _CloudStreamState
    return _CloudStreamState(
        ThinkingFilter(), integrity_mode=False,
        log=logging.getLogger("agentcore_client"),
        owner_id="u", session_id="s", runtime_session_id="session-abc123",
    )


def _failure_line(caplog) -> str:
    lines = [r.getMessage() for r in caplog.records if "QUERY_FAILURE" in r.getMessage()]
    assert len(lines) == 1, lines
    return lines[0]


class TestCloudStreamFailureLine:
    def test_read_failure_names_phase_type_last_frame_and_session(self, caplog):
        from agents.chat_client import StreamCancellation, _stream_cloud_response
        body = _Body([b'data: {"data": "Batch 7391 shipped"}', b""],
                     _ProtocolError("Response ended prematurely after 547 lines"))
        with caplog.at_level(logging.INFO, logger="agentcore_client"):
            events = list(_stream_cloud_response({"response": body}, _state(),
                                                 StreamCancellation()))
        line = _failure_line(caplog)
        for needle in ("category=RUNTIME_UNAVAILABLE", "public_category=stream_error",
                       "phase=read", "cancelled=no", "last_frame=data",
                       "runtime_session=session-abc123", "exc_type=",
                       "_ProtocolError", "where=", "elapsed_s=", "idle_s="):
            assert needle in line, (needle, line)
        assert _head(line) == "Response ended prematurely after ### lines"
        assert "7391" not in line              # the frame's data never reaches the log
        start = [r.getMessage() for r in caplog.records if "QUERY_STREAM_START" in r.getMessage()]
        assert start == ["QUERY_STREAM_START runtime_session=session-abc123"]
        assert [type(e).__name__ for e in events][-2:] == ["ErrorEvent", "DoneEvent"]

    def test_parse_failure_is_told_apart_from_a_read_failure(self, caplog, monkeypatch):
        from agents.chat_client import _stream_cloud_response
        state = _state()

        def _boom(_data):
            raise KeyError("toolUseId")
        monkeypatch.setattr(state, "events_for", _boom)
        body = _Body([b'data: {"message": {}}'], AssertionError("never reached"))
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            list(_stream_cloud_response({"response": body}, state))
        line = _failure_line(caplog)
        assert "phase=parse" in line and "exc_type=KeyError" in line
        assert "last_frame=message" in line
        assert ":_boom" in line                # the frame that raised is named

    def test_completion_failure_has_its_own_phase(self, caplog, monkeypatch):
        from agents.chat_client import _stream_cloud_response
        state = _state()
        monkeypatch.setattr(state, "completion_events",
                            lambda: (_ for _ in ()).throw(ValueError("bad 12")))

        class _Empty:
            def iter_lines(self, chunk_size=None):
                return iter([])

        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            list(_stream_cloud_response({"response": _Empty()}, state))
        assert "phase=completion" in _failure_line(caplog)

    def test_a_cancelled_stream_says_so(self, caplog):
        from agents.chat_client import StreamCancellation, _stream_cloud_response
        handle = StreamCancellation()
        handle.cancel()
        body = _Body([], OSError("closed"))
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            list(_stream_cloud_response({"response": body}, _state(), handle))
        assert "cancelled=yes" in _failure_line(caplog)

    def test_skipped_non_data_lines_are_counted(self, caplog):
        from agents.chat_client import _stream_cloud_response
        body = _Body([b": keepalive", b"data: not json", b""], OSError("reset"))
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            list(_stream_cloud_response({"response": body}, _state()))
        assert "skipped=2" in _failure_line(caplog)

    def test_a_fingerprint_fault_still_logs_the_failure(self, caplog, monkeypatch):
        from agents import chat_client
        monkeypatch.setattr(chat_client, "exception_fingerprint",
                            lambda exc: (_ for _ in ()).throw(RuntimeError("fp")))
        body = _Body([], OSError("reset"))
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            events = list(chat_client._stream_cloud_response({"response": body}, _state()))
        assert "fingerprint=unavailable" in _failure_line(caplog)
        assert type(events[-1]).__name__ == "DoneEvent"

    def test_success_line_carries_elapsed(self, caplog):
        from agents.chat_client import _stream_cloud_response

        class _Ok:
            def iter_lines(self, chunk_size=None):
                return iter([b'data: {"data": "ok"}'])

        with caplog.at_level(logging.INFO, logger="agentcore_client"):
            list(_stream_cloud_response({"response": _Ok()}, _state()))
        ok = [r.getMessage() for r in caplog.records if "QUERY_SUCCESS" in r.getMessage()]
        assert len(ok) == 1 and "elapsed_s=" in ok[0]


class TestInvokeFailureLine:
    def test_invoke_failure_names_code_and_phase(self, caplog, monkeypatch):
        from agents.chat_client import AgentCoreChatClient

        exc = Exception("Runtime arn:aws:bedrock-agentcore:111122223333 not ready")
        exc.response = {"Error": {"Code": "ResourceNotFoundException"},
                        "ResponseMetadata": {"HTTPStatusCode": 404, "RequestId": "req-1"}}
        client = AgentCoreChatClient.__new__(AgentCoreChatClient)

        def _impl(*_a, **_k):
            raise exc
            yield  # pragma: no cover - makes this a generator
        monkeypatch.setattr(client, "_stream_impl", _impl)
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            events = list(client.stream("q", "s", "u"))
        line = _failure_line(caplog)
        assert "phase=invoke" in line and "code=ResourceNotFoundException" in line
        assert "http=404" in line and "request_id=req-1" in line
        assert "111122223333" not in line
        assert type(events[0]).__name__ == "ErrorEvent"

    def test_a_fingerprint_fault_on_the_invoke_path_still_yields_the_public_pair(
            self, caplog, monkeypatch):
        from agents import chat_client
        client = chat_client.AgentCoreChatClient.__new__(chat_client.AgentCoreChatClient)

        def _impl(*_a, **_k):
            raise OSError("connect failed")
            yield  # pragma: no cover - makes this a generator
        monkeypatch.setattr(client, "_stream_impl", _impl)
        monkeypatch.setattr(chat_client, "exception_fingerprint",
                            lambda exc: (_ for _ in ()).throw(RuntimeError("fp")))
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            events = list(client.stream("q", "s", "u"))
        assert [type(e).__name__ for e in events] == ["ErrorEvent", "DoneEvent"]
        assert "fingerprint=unavailable" in _failure_line(caplog)

    def test_a_response_without_a_stream_is_an_invoke_failure(self, caplog):
        from agents.chat_client import _stream_cloud_response
        with caplog.at_level(logging.ERROR, logger="agentcore_client"):
            events = list(_stream_cloud_response({"statusCode": 200}, _state()))
        assert "phase=invoke" in _failure_line(caplog)
        assert [type(e).__name__ for e in events] == ["ErrorEvent", "DoneEvent"]


class TestOnboardingJoinDiagnostics:
    def test_count_by_source_splits_self_and_peers(self):
        from agents.onboard import _count_by_source
        sources = ["sys-x", "sys-x", "sys-x", "peer-a", "peer b; drop", "peer b; drop", None]
        assert _count_by_source(sources, {"sys-x", "sys"}) == (3, "?:3,peer-a:1")
        assert _count_by_source([], {"x"}) == (0, "none")

    def test_nodes_collect_dropped_are_counted_as_unknown(self):
        """collect() drops a null source; count() beside it keeps the node."""
        from agents.onboard import _count_by_source
        assert _count_by_source(["sys-x"], {"sys-x"}, matched=3) == (1, "?:2")
        assert _count_by_source([], {"sys-x"}, matched=0) == (0, "none")
        assert _count_by_source(["peer-a"], {"sys-x"}, matched=1) == (0, "peer-a:1")

    def test_snapshot_query_pairs_each_collect_with_a_count(self):
        """Pins the query shape the null-source accounting depends on."""
        from agents.onboard import _RESET_LEARNED_SNAPSHOT as q
        assert q.count("OPTIONAL MATCH") == 2
        assert "collect(jp.from_system) AS jp_src, count(jp) AS jp_n" in q
        assert "collect(ir.source_system) AS ir_src, count(ir) AS ir_n" in q

    def test_reset_counts_a_null_source_node_under_unknown(self, caplog, monkeypatch):
        from agents import onboard
        monkeypatch.setattr(onboard, "neptune_query_checked", lambda q, p=None: [
            {"jp_src": ["sys-a"], "jp_n": 2, "ir_src": [], "ir_n": 0}])
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._log_reset_learned_rows("sys-a", "sys-a")
        rec = [r for r in caplog.records if "RESET_LEARNED_ROWS" in r.getMessage()]
        assert rec[0].levelno == logging.WARNING
        assert "joinpaths_from_self=1 joinpaths_from_peers=?:1" in rec[0].getMessage()

    def test_reset_logs_peer_rows_as_a_warning_from_one_read(self, caplog, monkeypatch):
        from agents import onboard
        calls = []

        def _checked(q, p=None):
            calls.append(p)
            return [{"jp_src": ["sys-a", "sys-a", "peer-p"], "jp_n": 3, "ir_src": ["sys-a"], "ir_n": 1}]
        monkeypatch.setattr(onboard, "neptune_query_checked", _checked)
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._log_reset_learned_rows("sys-a", "sys-a")
        assert calls == [{"ids": ["sys-a"]}]          # ONE graph round trip
        rec = [r for r in caplog.records if "RESET_LEARNED_ROWS" in r.getMessage()]
        assert len(rec) == 1 and rec[0].levelno == logging.WARNING
        assert "joinpaths_from_self=2 joinpaths_from_peers=peer-p:1" in rec[0].getMessage()
        assert "identity_rules_from_self=1 identity_rules_from_peers=none" in rec[0].getMessage()

    def test_no_peer_rows_is_info(self, caplog, monkeypatch):
        from agents import onboard
        monkeypatch.setattr(onboard, "neptune_query_checked",
                            lambda q, p=None: [{"jp_src": [], "ir_src": []}])
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._log_reset_learned_rows("sys-a", "sys")
        rec = [r for r in caplog.records if "RESET_LEARNED_ROWS" in r.getMessage()]
        assert rec[0].levelno == logging.INFO and "joinpaths_from_peers=none" in rec[0].getMessage()

    def test_a_failed_read_through_the_real_wrapper_is_unavailable_not_none(
            self, caplog, monkeypatch):
        """The fail-soft ``neptune_query`` would turn this into ``[]`` and log a
        false "no peers"; the diagnostic uses the checked read instead."""
        from agents import db, onboard
        monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")

        def _execute(q, p=None):
            raise TimeoutError("read timed out after 60 s")
        monkeypatch.setattr(db, "_neptune_execute", _execute)
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._log_reset_learned_rows("sys-a", "sys")
        msg = [r.getMessage() for r in caplog.records if "RESET_LEARNED_ROWS" in r.getMessage()]
        assert msg == ["RESET_LEARNED_ROWS system=sys-a at=reset unavailable category=TimeoutError"]

    def test_reset_diagnostic_never_raises(self, caplog, monkeypatch):
        from agents import onboard
        monkeypatch.setattr(onboard, "neptune_query_checked",
                            lambda q, p=None: [{"jp_src": 5}])   # malformed row
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._log_reset_learned_rows("sys-a", "sys")
        assert any("RESET_LEARNED_ROWS system=sys-a at=reset unavailable" in r.getMessage()
                   for r in caplog.records)

    # -- provision: the delete scripts/reonboard.py actually runs -----------------

    def test_provision_delete_cypher_is_unchanged(self):
        """Building the deletes from the shared WHERE kept them byte-identical."""
        from agents import onboard
        assert onboard._ONBOARDING_JOINPATH_DELETE_CYPHER == (
            "MATCH (jp:JoinPath) WHERE (jp.from_system = $sid OR jp.to_system = $sid) "
            "AND jp.id STARTS WITH 'jp-' AND coalesce(jp.method, '') <> 'user_confirmed' "
            "DETACH DELETE jp")
        assert onboard._ONBOARDING_RULE_DELETE_CYPHER == (
            "MATCH (ir:IdentityRule) WHERE (ir.source_system = $sid OR ir.target_system = $sid) "
            "AND ir.id STARTS WITH 'ir-' AND coalesce(ir.method, '') <> 'user_confirmed' "
            # A rule a steward taught without its examples is kept too
            # (resolve.USER_UNVERIFIED_METHOD, test_thread_learn_unverified.py).
            "AND coalesce(ir.method, '') <> 'user_unverified' "
            "DETACH DELETE ir")

    def test_provision_snapshot_counts_exactly_what_the_delete_removes(self):
        from agents import onboard
        q = onboard._PROVISION_LEARNED_SNAPSHOT
        assert q.count("OPTIONAL MATCH") == 2
        assert ("OPTIONAL MATCH (jp:JoinPath) " + onboard._ONBOARDING_JOINPATH_WHERE) in q
        assert ("OPTIONAL MATCH (ir:IdentityRule) " + onboard._ONBOARDING_RULE_WHERE) in q
        assert "collect(jp.from_system) AS jp_src, count(jp) AS jp_n" in q
        assert "collect(ir.source_system) AS ir_src, count(ir) AS ir_n" in q
        assert "DELETE" not in q.upper()      # a read, never a write

    def test_provision_logs_peer_rows_before_it_deletes(self, caplog, monkeypatch):
        """The 2026-10-08 loss: a peer-written cmms -> mes join removed by the
        re-onboard's provision delete, which RESET_LEARNED_ROWS at=reset never saw."""
        from agents import onboard
        order = []

        def _checked(q, p=None):
            order.append(("read", p))
            return [{"jp_src": ["mes-x", "cmms", "cmms-y"], "jp_n": 3, "ir_src": [], "ir_n": 0}]

        def _write(q, p=None):
            order.append(("delete", q.split()[1]))
            return True
        monkeypatch.setattr(onboard, "neptune_query_checked", _checked)
        monkeypatch.setattr(onboard, "neptune_write", _write)
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._delete_stale_onboarding_rule_artifacts("mes-x")
        assert order == [("read", {"sid": "mes-x"}),
                         ("delete", "(ir:IdentityRule)"), ("delete", "(jp:JoinPath)")]
        rec = [r for r in caplog.records if "RESET_LEARNED_ROWS" in r.getMessage()]
        assert len(rec) == 1 and rec[0].levelno == logging.WARNING
        assert rec[0].getMessage() == (
            "RESET_LEARNED_ROWS system=mes-x at=provision joinpaths_from_self=1 "
            "joinpaths_from_peers=cmms:1,cmms-y:1 identity_rules_from_self=0 "
            "identity_rules_from_peers=none")

    def test_a_failed_provision_snapshot_never_blocks_the_delete(self, caplog, monkeypatch):
        from agents import onboard
        deletes = []

        def _checked(q, p=None):
            raise TimeoutError("read timed out after 60 s")
        monkeypatch.setattr(onboard, "neptune_query_checked", _checked)
        monkeypatch.setattr(onboard, "neptune_write",
                            lambda q, p=None: deletes.append(q) or True)
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._delete_stale_onboarding_rule_artifacts("mes-x")
        assert deletes == [onboard._ONBOARDING_RULE_DELETE_CYPHER,
                           onboard._ONBOARDING_JOINPATH_DELETE_CYPHER]
        assert [r.getMessage() for r in caplog.records if "RESET_LEARNED_ROWS" in r.getMessage()] \
            == ["RESET_LEARNED_ROWS system=mes-x at=provision unavailable category=TimeoutError"]

    def test_a_failed_provision_delete_still_fails_loud(self, monkeypatch):
        from agents import onboard
        monkeypatch.setattr(onboard, "neptune_query_checked",
                            lambda q, p=None: [{"jp_src": [], "jp_n": 0, "ir_src": [], "ir_n": 0}])
        monkeypatch.setattr(onboard, "neptune_write", lambda q, p=None: False)
        with pytest.raises(RuntimeError, match="stale onboarding IdentityRule cleanup failed"):
            onboard._delete_stale_onboarding_rule_artifacts("mes-x")

    def test_a_hung_read_never_holds_up_the_deletes(self, caplog, monkeypatch):
        """The snapshot ran inline, so a read that never returned held
        both mandatory deletes. Now it is abandoned at its budget, and while it is
        still stuck the next snapshot is skipped instead of queued behind it."""
        import threading
        import time

        from agents import onboard
        release = threading.Event()
        started = []

        def _hung(q, p=None):
            started.append(p)
            release.wait(30)
            return [{"jp_src": [], "jp_n": 0, "ir_src": [], "ir_n": 0}]
        deletes = []
        monkeypatch.setattr(onboard, "neptune_query_checked", _hung)
        monkeypatch.setattr(onboard, "neptune_write",
                            lambda q, p=None: deletes.append(q) or True)
        monkeypatch.setattr(onboard, "LEARNED_ROWS_READ_TIMEOUT_S", 0.2)
        monkeypatch.setattr(onboard, "_learned_rows_inflight", None)
        try:
            with caplog.at_level(logging.INFO, logger="agents.onboard"):
                t0 = time.monotonic()
                onboard._delete_stale_onboarding_rule_artifacts("mes-x")
                first = time.monotonic() - t0
                t1 = time.monotonic()
                onboard._log_reset_learned_rows("mes-y", "mes")   # read still stuck
                second = time.monotonic() - t1
            assert deletes == [onboard._ONBOARDING_RULE_DELETE_CYPHER,
                               onboard._ONBOARDING_JOINPATH_DELETE_CYPHER]
            assert first < 2.0 and second < 0.1
            assert len(started) == 1                       # the skip queued nothing
            msgs = [r.getMessage() for r in caplog.records if "RESET_LEARNED_ROWS" in r.getMessage()]
            assert msgs == [
                "RESET_LEARNED_ROWS system=mes-x at=provision unavailable category=timeout",
                "RESET_LEARNED_ROWS system=mes-y at=reset unavailable category=busy"]
        finally:
            release.set()
            onboard._learned_rows_inflight.result(timeout=5)

    def test_the_next_read_runs_once_the_stuck_one_finishes(self, caplog, monkeypatch):
        from agents import onboard
        monkeypatch.setattr(onboard, "_learned_rows_inflight", None)
        monkeypatch.setattr(onboard, "neptune_query_checked", lambda q, p=None: [
            {"jp_src": ["peer-p"], "jp_n": 1, "ir_src": [], "ir_n": 0}])
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._log_reset_learned_rows("sys-a", "sys-a")
            onboard._log_reset_learned_rows("sys-a", "sys-a")
        msgs = [r.getMessage() for r in caplog.records if "RESET_LEARNED_ROWS" in r.getMessage()]
        assert len(msgs) == 2 and all("joinpaths_from_peers=peer-p:1" in m for m in msgs)

    def test_provision_summary_groups_cross_system_joins_by_peer(self, caplog):
        from types import SimpleNamespace

        from agents import onboard
        ctx = SimpleNamespace(system_id="sys-a", rules_written=2, rules_rejected=[{}],
                              join_paths_written=3, value_mappings_written=0,
                              authority_rules_written=1)
        joins = [{"from_system": "sys-a", "to_system": "sys-z"},
                 {"from_system": "peer-p", "to_system": "sys-a"},
                 {"from_system": "sys-a", "to_system": "sys-a"},
                 {"from_system": "sys-a", "to_system": "sys-z"}]
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._log_provision_summary(ctx, joins)
        msg = next(r.getMessage() for r in caplog.records
                   if "PROVISION_SUMMARY" in r.getMessage())
        assert "identity_rules_written=2 identity_rules_rejected=1" in msg
        assert "joinpaths_internal=1 joinpaths_cross=peer-p:1,sys-z:2" in msg

    def test_provision_summary_never_raises(self, caplog):
        from agents import onboard
        with caplog.at_level(logging.INFO, logger="agents.onboard"):
            onboard._log_provision_summary(object(), None)
        assert any("PROVISION_SUMMARY" in r.getMessage() and "unavailable" in r.getMessage()
                   for r in caplog.records)


class TestRuntimeIntegrityInvocationFailure:
    """The opt-in integrity path logged a bare category; it now records the
    exception type and a digit-masked traceback in the MESSAGE (the runtime's
    OTel handler drops exc_info), like the legacy path already did."""

    def test_failure_is_named_and_still_scrubbed(self, caplog, monkeypatch):
        import asyncio

        from agents import runtime_app

        def _boom(*_a, **_k):
            raise ValueError("bad row 4242")
        monkeypatch.setattr(runtime_app, "_pop_tryit_candidate", _boom)

        async def _drain():
            return [e async for e in runtime_app._invoke_integrity_runtime(
                {"sessionId": "sess-a", "prompt": "q"}, None, lambda **_k: None)]
        with caplog.at_level(logging.ERROR, logger=runtime_app.logger.name):
            out = asyncio.run(_drain())
        assert out == [{"error": "Query failed."}]
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "session=sess-a" in msg and "type=ValueError" in msg
        assert "4242" not in msg
