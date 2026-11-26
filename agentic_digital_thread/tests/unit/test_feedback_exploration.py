# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bounded user-feedback exploration (agents/feedback_exploration.py).

Offline — no AWS, no network. The job registry is an in-memory
``JobStore("feedback-explore", table=None)`` (the ``test_tryit_job.py``
idiom); the stream is scripted through the module-level ``_stream_factory``
seam; the worker runs INLINE (called directly — never a live thread).

Each pin names the mutation it kills (plan M10–M18, M22–M23, M27–M31):
dedup removal, rate-cap change, concurrency-slot removal, tool-cap removal,
wall-clamp removal, unfenced comment, comment leaking into proposal fields
or logs, wrong proposal shape/source/writer, memory-write flip, identity
injection, ledger-bump removal, error-detail leak, kill-switch bypass,
raise-on-store-failure, and diff inversion.

Divergence-threshold pins (TH-*, 2026-08-05 — measured routing-variance
noise on Demo): TH-D1 direction-filter removal ('queried fewer' files),
TH-C1 filing from a single run / the union, TH-C2 union-not-intersection,
TH-C3 second-run prompt recomposition or shared session, TH-K1 filing-dedupe
removal, TH-K2 dedupe over-broadened to question-only, TH-K3 dedupe
over-broadened to gap-only, TH-K4 dedupe armed on a gate drop, TH-B1
total-bound removal, TH-L1 new-outcome/ledger collapse.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

import agents.artifact_usage as artifact_usage
import agents.feedback_exploration as fx
import agents.self_learning as self_learning
from agents.events import (
    DoneEvent, ErrorEvent, MessageEvent, MetadataEvent, ToolUseEvent,
)
from agents.job_store import JobStore

_COMMENT = "COMMENT_MARKER_a1b2c3 the totals looked wrong to me"


@pytest.fixture(autouse=True)
def _explorations_enabled(monkeypatch):
    """The path defaults OFF (fail-closed); these tests enable it through the
    runtime admin toggle SEAM (env truthy no longer admits — the admin flag
    is the single ON switch). Kill-switch tests override env/seam locally."""
    import agents.feedback_exploration_settings as fxs
    monkeypatch.delenv("FEEDBACK_EXPLORATION_ENABLED", raising=False)
    fxs._reset()

    class _On:
        def get_flag(self):
            return True

        def set_flag(self, value):  # pragma: no cover - never written here
            raise AssertionError("tests must not write the persisted flag")

    fxs.configure(_On())
    yield
    fxs._reset()


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    """Fresh in-memory registry per test."""
    store = JobStore("feedback-explore", table=None)
    monkeypatch.setattr(fx, "_JOBS", store)
    yield store


@pytest.fixture(autouse=True)
def _inline_worker(monkeypatch):
    """Run the worker INLINE: fx's view of threading.Thread executes the
    target synchronously so tests never race a daemon thread (the global
    threading module is untouched)."""
    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(fx, "threading", SimpleNamespace(Thread=_InlineThread))


def _summary(rules):
    return MetadataEvent(data={"intelligence_summary": {"rules_applied": rules}})


def _script_stream(monkeypatch, events):
    """Script the run through the module-level seam; returns the calls list
    of (prompt, session_id)."""
    calls: list[tuple] = []

    def factory(prompt, session_id):
        calls.append((prompt, session_id))

        def gen():
            yield from events
        return gen()

    monkeypatch.setattr(fx, "_stream_factory", factory)
    return calls


def _spy_lpm(monkeypatch, result="mid-1"):
    calls: list[dict] = []

    def spy(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(self_learning, "log_proposed_mapping", spy)
    return calls


def _spy_ledger(monkeypatch):
    bumps: list[tuple] = []
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda refs, evidence="": bumps.extend(refs) or len(refs))
    return bumps


_DIVERGING_EVENTS = [
    ToolUseEvent(tool_name="sys_a___run_query"),
    ToolUseEvent(tool_name="sys_b___run_query"),
    _summary([{"id": "r1", "label": "rule one"}]),
    MessageEvent(content="answer"),
    DoneEvent(),
]


def _start(monkeypatch=None, *, user="u1", session="s1", msg="m1",
           question="which lots were affected?", comment=_COMMENT,
           systems=(), rules=()):
    return fx.maybe_start_exploration(user, session, msg, question, comment,
                                      list(systems), list(rules))


def _release_slot():
    fx._jobs()[fx._ACTIVE_KEY] = {"status": "idle"}


# ── kill switch / start gates ───────────────────────────────────────────────

def test_env_kill_skips_exploration_despite_admin_on(monkeypatch):
    """M29: env KILL ignored — a set non-truthy env value must win over the
    admin ON flag the autouse fixture configured."""
    monkeypatch.setenv("FEEDBACK_EXPLORATION_ENABLED", "0")
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    assert _start() is None


def test_default_is_off(monkeypatch):
    """Nothing set anywhere (no env, no admin flag) ⇒ OFF — a new untrusted
    path stays opt-in."""
    import agents.feedback_exploration_settings as fxs
    fxs._reset()
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    assert _start() is None


@pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
def test_truthy_env_alone_does_not_admit(monkeypatch, value):
    """A14: a truthy env value is NOT an enabler — without the admin flag the
    exploration must not start (the env var is only a kill switch now)."""
    import agents.feedback_exploration_settings as fxs
    fxs._reset()                              # drop the fixture's admin ON
    monkeypatch.setenv("FEEDBACK_EXPLORATION_ENABLED", value)
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    assert _start() is None


def test_admin_flag_enables_with_truthy_env(monkeypatch):
    """Admin ON + truthy env (no kill) ⇒ enabled — the legacy `=1` deploys
    keep the toggle usable rather than being killed by their own env."""
    monkeypatch.setenv("FEEDBACK_EXPLORATION_ENABLED", "1")
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    assert _start() is not None


@pytest.mark.parametrize("value", ["", "  ", "banana", "enabled", "2", "TRUE_"])
def test_unrecognized_value_fails_closed(monkeypatch, value):
    """Fail-closed kill: any set non-truthy value disables even with the
    admin flag ON."""
    monkeypatch.setenv("FEEDBACK_EXPLORATION_ENABLED", value)
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    assert _start() is None


def test_second_exploration_same_session_question_is_skipped(monkeypatch):
    """M10: dedup removed."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    assert _start(question="q-one") is not None
    _release_slot()
    assert _start(question="q-one") is None            # same (session, question)
    assert _start(question="q-two") is not None        # different question OK


def test_fourth_exploration_in_hour_is_skipped(monkeypatch):
    """M11: rate cap != 3/hr."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    for n in range(fx._MAX_PER_USER_PER_HOUR):
        assert _start(question=f"q-{n}") is not None
        _release_slot()
    assert _start(question="q-final") is None
    # a DIFFERENT user is not throttled by u1's stamps
    assert _start(user="u2", session="s2", question="q-final") is not None


def test_running_slot_blocks_new_start_until_stale(monkeypatch):
    """M12: concurrency slot removed / stale takeover broken."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    jobs = fx._jobs()
    jobs[fx._ACTIVE_KEY] = {"status": "running", "job_id": "other",
                            "started_at": time.time()}
    assert _start() is None
    jobs[fx._ACTIVE_KEY] = {"status": "running", "job_id": "other",
                            "started_at": time.time() - fx._STALE_SECONDS - 1}
    assert _start() is not None


def test_start_never_raises_on_store_failure(monkeypatch):
    """M30: start raises on backend failure."""
    def _boom():
        raise RuntimeError("registry down")
    monkeypatch.setattr(fx, "_jobs", _boom)
    assert fx.maybe_start_exploration("u", "s", "m", "q", "c") is None


# ── per-run containment ─────────────────────────────────────────────────────

def test_run_aborts_after_tool_call_cap(monkeypatch):
    """M13: tool cap removed — the 13th tool call aborts the run and nothing
    is filed."""
    events = [ToolUseEvent(tool_name=f"sys_{n}___q")
              for n in range(fx._MAX_TOOL_CALLS + 1)] + [DoneEvent()]
    _script_stream(monkeypatch, events)
    lpm = _spy_lpm(monkeypatch)
    bumps = _spy_ledger(monkeypatch)
    with pytest.raises(RuntimeError):
        fx._single_run("p", "sess")
    exploration_id = _start()
    assert exploration_id is not None
    job = fx._jobs().get(exploration_id)
    assert job["status"] == "error"
    assert lpm == []
    assert ("feedback-exploration", "error") in bumps


def test_run_aborts_after_wall_clamp(monkeypatch):
    """M14: wall clamp removed — with a zero clamp the first event trips it."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    monkeypatch.setattr(fx, "_RUN_CLAMP_SECONDS", -1)
    with pytest.raises(TimeoutError):
        fx._single_run("p", "sess")


# ── prompt composition (untrusted fence) ────────────────────────────────────

def test_prompt_contains_exactly_one_fence_pair_and_origin_label():
    """M15: comment unfenced."""
    prompt = fx._compose_prompt("which lots were affected?", _COMMENT, "msg-9")
    assert prompt.count("<<<BUSINESS_CONTEXT") == 1
    assert prompt.count("BUSINESS_CONTEXT>>>") == 1
    assert "user-feedback:msg-9" in prompt
    assert fx._FENCE_HEADING.split("(")[0].strip() in prompt
    assert "which lots were affected?" in prompt
    # the comment sits BETWEEN the fences
    open_at = prompt.index("<<<BUSINESS_CONTEXT")
    close_at = prompt.index("BUSINESS_CONTEXT>>>", open_at + 1)
    assert open_at < prompt.index(_COMMENT) < close_at


# ── proposal filing ─────────────────────────────────────────────────────────

def test_files_exactly_one_insight_via_log_proposed_mapping_source_user_feedback(monkeypatch):
    """M18: wrong source/type/writer."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    lpm = _spy_lpm(monkeypatch)
    bumps = _spy_ledger(monkeypatch)
    exploration_id = _start(systems=["sys-a"], rules=[])
    assert exploration_id is not None
    assert len(lpm) == 1
    kwargs = lpm[0]
    assert kwargs["proposed_type"] == "insight"
    assert kwargs["source"] == "user-feedback"
    assert kwargs["method"] == "feedback-exploration"
    assert kwargs["provenance"] == "feedback-exploration"
    assert kwargs["confidence"] == 0.3
    # M2: the originating message_id rides query_context (the review-queue
    # field) for auditability back to the feedback record.
    assert kwargs["query_context"].startswith(
        "User-feedback exploration (message m1): ")
    assert "which lots were affected?" in kwargs["query_context"]
    # M3: no fabricated system pair — an exploration insight is a narrative
    # routing finding, never an alphabetical from→to claim.
    assert kwargs["from_system"] == ""
    assert kwargs["to_system"] == ""
    job = fx._jobs().get(exploration_id)
    assert job["status"] == "done" and job["outcome"] == "filed"
    assert ("feedback-exploration", "filed") in bumps


def test_filed_proposal_never_contains_comment_text(monkeypatch):
    """M16: comment in proposal fields."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    lpm = _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    assert _start(systems=["sys-a"]) is not None
    assert len(lpm) == 1
    for value in lpm[0].values():
        assert "COMMENT_MARKER_a1b2c3" not in str(value)


def test_no_log_record_contains_comment_text(monkeypatch, caplog):
    """M17: comment in logs — success AND error paths."""
    _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
        assert _start(question="q-ok") is not None
        _release_slot()
        _script_stream(monkeypatch, [ErrorEvent(message="boom")])
        assert _start(question="q-err") is not None
    for record in caplog.records:
        assert "COMMENT_MARKER_a1b2c3" not in record.getMessage()


def test_no_divergence_files_nothing_and_bumps_no_change(monkeypatch):
    """M27 (no_change leg): a matching re-run files NO proposal."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    lpm = _spy_lpm(monkeypatch)
    bumps = _spy_ledger(monkeypatch)
    exploration_id = _start(
        systems=["sys-a", "sys-b"],
        rules=[{"id": "r1", "label": "rule one"}])
    assert exploration_id is not None
    assert lpm == []
    job = fx._jobs().get(exploration_id)
    assert job["status"] == "done" and job["outcome"] == "no_change"
    assert ("feedback-exploration", "no_change") in bumps


def test_gate_dropped_outcome_when_proposal_filtered(monkeypatch):
    """Divergence found but ``log_proposed_mapping``
    returned ``None`` (gate drop/absorption/staging hold) is a THIRD outcome
    — ``gate_dropped`` — distinct from ``no_change`` (re-run matched), so the
    ledger never misreports a filtered proposal as a clean match."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    lpm = _spy_lpm(monkeypatch, result=None)   # the writer drops it
    bumps = _spy_ledger(monkeypatch)
    exploration_id = _start()                  # cited=() → divergence
    assert exploration_id is not None
    assert len(lpm) == 1                       # filing WAS attempted
    job = fx._jobs().get(exploration_id)
    assert job["status"] == "done" and job["outcome"] == "gate_dropped"
    assert ("feedback-exploration", "gate_dropped") in bumps
    assert ("feedback-exploration", "no_change") not in bumps


def test_ledger_bumped_filed_and_error_outcomes(monkeypatch):
    """M27: ledger bump removed."""
    _spy_lpm(monkeypatch)
    bumps = _spy_ledger(monkeypatch)
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    assert _start(question="q-filed") is not None
    _release_slot()
    _script_stream(monkeypatch, [ErrorEvent(message="boom")])
    assert _start(question="q-error") is not None
    assert ("feedback-exploration", "filed") in bumps
    assert ("feedback-exploration", "error") in bumps


def test_error_stored_with_exception_type_only(monkeypatch):
    """M28: error re-raises or leaks the message."""
    def factory(prompt, session_id):
        raise ValueError("secret internal detail xyz")
    monkeypatch.setattr(fx, "_stream_factory", factory)
    _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    exploration_id = _start()
    assert exploration_id is not None  # worker error never propagates
    job = fx._jobs().get(exploration_id)
    assert job["status"] == "error"
    assert job["error_detail"] == "exploration failed: ValueError"
    assert "secret internal detail" not in str(job)
    # the single app-wide slot is released
    assert fx._jobs().get(fx._ACTIVE_KEY)["status"] == "idle"


# ── stream kwargs (memory / identity) ───────────────────────────────────────

def _capture_stream_kwargs(monkeypatch, *, has_runtime: bool):
    captured: dict = {}

    class _FakeClient:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return iter(())

    import agents.chat_client as chat_client
    import agents.config as config
    monkeypatch.setattr(config, "get_config",
                        lambda: SimpleNamespace(has_runtime=has_runtime))
    monkeypatch.setattr(chat_client, "AgentCoreChatClient", lambda: _FakeClient())
    monkeypatch.setattr(chat_client, "LocalChatClient", lambda: _FakeClient())
    stream = fx._default_stream("p", "sess")
    list(stream)
    return captured


@pytest.mark.parametrize("has_runtime", [False, True])
def test_stream_called_with_write_memory_false(monkeypatch, has_runtime):
    """M22: write_memory flipped (both client branches)."""
    kwargs = _capture_stream_kwargs(monkeypatch, has_runtime=has_runtime)
    assert kwargs["write_memory"] is False
    assert kwargs["user_id"] == "feedback-exploration"


@pytest.mark.parametrize("has_runtime", [False, True])
def test_exploration_stream_kwargs_carry_no_adt_identity(monkeypatch, has_runtime):
    """M23: _adt_identity supplied (or an extra payload smuggled in)."""
    kwargs = _capture_stream_kwargs(monkeypatch, has_runtime=has_runtime)
    assert "_adt_identity" not in kwargs
    assert "extra_payload" not in kwargs


# ── pure diff ───────────────────────────────────────────────────────────────

def test_diff_findings_pure_shape():
    """M31: diff inverted."""
    run = {"systems_queried": ["sys-a", "sys-b"],
           "rules_applied": [{"id": "r1", "label": "one"},
                             {"id": "r2", "label": "two"}]}
    diff = fx._diff_findings(run, ["sys-a", "sys-c"],
                             [{"id": "r1", "label": "one"},
                              {"id": "r9", "label": "nine"}])
    assert diff["systems_queried"] == ["sys-a", "sys-b"]
    assert diff["systems_beyond_cited"] == ["sys-b"]
    assert diff["cited_systems_not_queried"] == ["sys-c"]
    assert diff["rules_beyond_cited"] == [{"id": "r2", "label": "two"}]
    assert diff["cited_rules_not_applied"] == ["r9"]
    assert fx._has_divergence(diff) is True
    same = fx._diff_findings(run, ["sys-a", "sys-b"],
                             [{"id": "r1"}, {"id": "r2"}])
    assert fx._has_divergence(same) is False


def test_diff_normalizes_case_and_drops_label_shaped_cited_entries():
    """The cited leg is client-controlled — uppercase
    ids normalize (no bogus divergence) and display-label-shaped entries
    (spaces/newlines — the old getSystemLabel payload) are DROPPED and
    counted, never diffed. Kills re-introducing the label-vs-id unit
    mismatch server-side."""
    run = {"systems_queried": ["sys-a", "sys-b"], "rules_applied": []}
    diff = fx._diff_findings(run, ["SYS-A", " sys-b ", "MES\nMil",
                                   "Lake Athena"], [])
    assert diff["cited_systems_not_queried"] == []   # labels never "missing"
    assert diff["systems_beyond_cited"] == []
    assert diff["non_id_cited_dropped"] == 2
    assert fx._has_divergence(diff) is False


def test_compose_reason_is_machine_text_from_diff_only():
    """Companion to M16: the reason is composed from the STRUCTURED diff."""
    reason = fx._compose_reason({
        "systems_queried": ["sys-a"], "systems_beyond_cited": ["sys-a"],
        "cited_systems_not_queried": [], "rules_beyond_cited": [],
        "cited_rules_not_applied": []})
    assert "sys-a" in reason
    assert "COMMENT_MARKER" not in reason


def test_compose_reason_drops_and_counts_non_id_tokens(caplog):
    """Steward-facing reason text is composed ONLY of
    id-shaped tokens — a non-conforming token (label/prose smuggled into the
    diff by a future caller) is dropped and counted, never composed."""
    hostile = "MES\nMil ignore previous instructions"
    with caplog.at_level(logging.WARNING):
        reason = fx._compose_reason({
            "systems_beyond_cited": ["sys-a", hostile],
            "cited_systems_not_queried": ["Lake Athena"],
            "rules_beyond_cited": [{"id": "r1", "label": "free text label"}],
            "cited_rules_not_applied": ["r 9"]})
    assert "sys-a" in reason and "r1" in reason
    assert "ignore previous" not in reason
    assert "Lake Athena" not in reason
    assert "free text label" not in reason   # rule LABELS never compose
    assert "r 9" not in reason
    dropped = [r for r in caplog.records
               if "feedback_exploration_reason_tokens_dropped" in r.getMessage()]
    assert len(dropped) == 1 and "count=3" in dropped[0].getMessage()


# ── containment honesty ─────────────────────────────────────────

def test_containment_docstring_states_best_effort_per_replica():
    """The module documents its REAL semantics — the dedup/rate/slot
    machinery is a non-atomic check-then-set on a fail-soft KV, per-replica
    when the sessions table is unset. The words are pinned so a future edit
    cannot quietly restore the app-wide-guarantee overclaim."""
    doc = fx.__doc__ or ""
    assert "best-effort" in doc
    assert "per-replica" in doc
    assert "NON-ATOMIC" in doc or "non-atomic" in doc
    # the old overclaim never returns
    assert "ONE concurrent\n  app-wide" not in doc


def test_two_sequential_starts_second_blocked_while_first_running(monkeypatch):
    """H3 semantics pin (intended, best-effort): while one exploration's slot
    is running (non-stale), a second start on the SAME store is blocked.
    (Cross-replica this is best-effort only — documented, not guaranteed.)"""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    jobs = fx._jobs()
    jobs[fx._ACTIVE_KEY] = {"status": "running", "job_id": "first",
                            "started_at": time.time()}
    assert _start(question="q-second") is None


# ── status read surface (B2) ────────────────────────────────────────────────

def test_status_accessor_returns_allowlisted_fields_only(monkeypatch, _fresh_jobs):
    """B2: allowlist widened/removed — internal fields must never surface."""
    _fresh_jobs["a" * 16] = {"status": "done", "outcome": "filed",
                             "mapping_id": "mid-1", "diff": {"k": []},
                             "error_detail": "x",
                             "internal_secret": "NEVER"}
    record = fx.get_exploration_status("a" * 16)
    assert record == {"status": "done", "outcome": "filed",
                      "mapping_id": "mid-1", "diff": {"k": []},
                      "error_detail": "x"}
    assert "internal_secret" not in record


@pytest.mark.parametrize("key", ["active", "rate:u1", "dedup:s1:aaaaaaaaaaaaaaaa",
                                 "A" * 16, "a" * 15, "a" * 17, "", "../active"])
def test_status_accessor_rejects_reserved_and_malformed_keys(_fresh_jobs, key):
    """B2: shape anchor removed — reserved registry keys (active/rate:*/
    dedup:*) and malformed ids must read as None, never as records."""
    _fresh_jobs[fx._ACTIVE_KEY] = {"status": "running", "job_id": "x"}
    _fresh_jobs["rate:u1"] = {"stamps": [1.0]}
    _fresh_jobs["dedup:s1:aaaaaaaaaaaaaaaa"] = {"job_id": "x"}
    assert fx.get_exploration_status(key) is None


def test_status_accessor_unknown_id_is_none(_fresh_jobs):
    assert fx.get_exploration_status("b" * 16) is None


def test_status_accessor_non_string_is_none(_fresh_jobs):
    assert fx.get_exploration_status(None) is None  # type: ignore[arg-type]


# ── divergence threshold (TH-*: direction, corroboration, dedupe, bound) ────

def _events(*system_stubs, rules=()):
    """A run's event stream querying the given systems (tool-name stubs like
    ``sys_a`` → system id ``sys-a``) and applying the given rules."""
    events = [ToolUseEvent(tool_name=f"{stub}___run_query")
              for stub in system_stubs]
    events += [_summary(list(rules)), MessageEvent(content="answer"),
               DoneEvent()]
    return events


def _script_stream_per_run(monkeypatch, runs):
    """Script DIFFERENT events per corroboration run (call N gets runs[N]);
    returns the calls list of (prompt, session_id)."""
    calls: list[tuple] = []

    def factory(prompt, session_id):
        events = runs[min(len(calls), len(runs) - 1)]
        calls.append((prompt, session_id))

        def gen():
            yield from events
        return gen()

    monkeypatch.setattr(fx, "_stream_factory", factory)
    return calls


def test_queried_fewer_only_diff_files_nothing(monkeypatch):
    """TH-D1 (direction filter): a re-run that merely queried FEWER systems /
    applied fewer rules than were cited is variance or a cheaper route, NOT a
    finding — outcome ``no_change``, nothing filed; the 'queried fewer' facts
    stay in the recorded diff (honest observability). Kills: re-adding
    cited_systems_not_queried / cited_rules_not_applied to the filing gate
    (the measured fx-unconnected-system-feedback review-queue noise)."""
    _script_stream(monkeypatch, _events("sys_a"))
    lpm = _spy_lpm(monkeypatch)
    bumps = _spy_ledger(monkeypatch)
    exploration_id = _start(systems=["sys-a", "sys-b"],
                            rules=[{"id": "r9", "label": "nine"}])
    assert exploration_id is not None
    assert lpm == []
    job = fx._jobs().get(exploration_id)
    assert job["status"] == "done" and job["outcome"] == "no_change"
    assert job["diff"]["cited_systems_not_queried"] == ["sys-b"]
    assert job["diff"]["cited_rules_not_applied"] == ["r9"]
    assert ("feedback-exploration", "no_change") in bumps


def test_gap_diff_still_files(monkeypatch):
    """TH-D1 companion (the filter must not over-block): a genuine GAP —
    the re-run consulted a system the cited answer did not — files, and the
    reason names the gap. Kills: inverting/removing the gap leg."""
    _script_stream(monkeypatch, _events("sys_a", "sys_b"))
    lpm = _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    exploration_id = _start(systems=["sys-a"])
    assert exploration_id is not None
    assert len(lpm) == 1
    assert "sys-b" in lpm[0]["reason"]
    assert fx._jobs().get(exploration_id)["outcome"] == "filed"


def test_gap_in_one_run_only_is_uncorroborated_not_filed(monkeypatch):
    """TH-C1 (corroboration): a gap that appears in run 1 but NOT run 2 is
    routing nondeterminism — outcome ``uncorroborated`` (distinct from
    ``no_change``), nothing filed, residue recorded in the diff and visible
    through the status read. Kills: filing from a single run or from the
    UNION of the two runs (the measured fx-nonsense-feedback noise)."""
    _script_stream_per_run(monkeypatch,
                           [_events("sys_a", "sys_b"), _events("sys_a")])
    lpm = _spy_lpm(monkeypatch)
    bumps = _spy_ledger(monkeypatch)
    exploration_id = _start(systems=["sys-a"])
    assert exploration_id is not None
    assert lpm == []
    job = fx._jobs().get(exploration_id)
    assert job["status"] == "done" and job["outcome"] == "uncorroborated"
    assert job["diff"]["systems_beyond_cited"] == []
    assert job["diff"]["uncorroborated_systems_beyond_cited"] == ["sys-b"]
    assert ("feedback-exploration", "uncorroborated") in bumps
    assert ("feedback-exploration", "no_change") not in bumps
    # TH-L1 adjunct: the WHY is readable through the allowlisted status
    # surface without widening the allowlist.
    record = fx.get_exploration_status(exploration_id)
    assert record["outcome"] == "uncorroborated"
    assert record["diff"]["uncorroborated_systems_beyond_cited"] == ["sys-b"]


def test_gap_in_both_runs_files_the_intersection_only(monkeypatch):
    """TH-C2 (intersection, not union): run 1 gaps {sys-b, sys-c}, run 2
    gaps {sys-b, sys-d} — only the corroborated sys-b files; sys-c/sys-d
    land in the uncorroborated legs and never compose into the
    steward-facing reason. Kills: union instead of intersection."""
    _script_stream_per_run(monkeypatch,
                           [_events("sys_a", "sys_b", "sys_c"),
                            _events("sys_a", "sys_b", "sys_d")])
    lpm = _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    exploration_id = _start(systems=["sys-a"])
    assert exploration_id is not None
    assert len(lpm) == 1
    reason = lpm[0]["reason"]
    assert "sys-b" in reason
    assert "sys-c" not in reason and "sys-d" not in reason
    job = fx._jobs().get(exploration_id)
    assert job["outcome"] == "filed"
    assert job["diff"]["systems_beyond_cited"] == ["sys-b"]
    assert job["diff"]["uncorroborated_systems_beyond_cited"] == \
        ["sys-c", "sys-d"]


def test_second_run_reuses_the_same_prompt_on_a_distinct_session(monkeypatch):
    """TH-C3: exactly two runs; the second REUSES the composed prompt
    byte-identically (ONE fenced-comment composition — no second untrusted
    path) on a DISTINCT session id. Kills: recomposing/re-fencing per run,
    or collapsing both runs onto one session."""
    calls = _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    assert _start(systems=["sys-a"]) is not None
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]                     # same prompt bytes
    assert calls[0][0].count("<<<BUSINESS_CONTEXT") == 1  # one fence pair
    assert calls[0][1] != calls[1][1]                     # independent runs


def test_same_gap_same_question_suppressed_within_window(monkeypatch):
    """TH-K1 (filing dedupe): the same corroborated gap for the same question
    files ONCE per window — a second exploration (different session, so the
    start-gate dedup does not apply) is ``dedup_suppressed``: no second
    write, distinct ledger outcome. Kills: dedupe removal."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    lpm = _spy_lpm(monkeypatch)
    bumps = _spy_ledger(monkeypatch)
    assert _start(session="s1", systems=["sys-a"]) is not None
    _release_slot()
    second = _start(session="s2", systems=["sys-a"])
    assert second is not None
    assert len(lpm) == 1                                  # only the first filed
    job = fx._jobs().get(second)
    assert job["status"] == "done" and job["outcome"] == "dedup_suppressed"
    assert job["mapping_id"] == ""
    assert ("feedback-exploration", "dedup_suppressed") in bumps
    assert bumps.count(("feedback-exploration", "filed")) == 1


def test_a_different_gap_for_the_same_question_still_files(monkeypatch):
    """TH-K2: the dedupe keys on the GAP SET, not the question alone — a
    DIFFERENT corroborated gap for the same question still files. Kills:
    over-broadening the dedupe to question-only."""
    lpm = _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    _script_stream(monkeypatch, _events("sys_a", "sys_b"))
    assert _start(session="s1", systems=["sys-a"]) is not None
    _release_slot()
    _script_stream(monkeypatch, _events("sys_a", "sys_c"))
    second = _start(session="s2", systems=["sys-a"])
    assert second is not None
    assert len(lpm) == 2
    assert fx._jobs().get(second)["outcome"] == "filed"


def test_the_same_gap_for_a_different_question_still_files(monkeypatch):
    """TH-K3: the dedupe keys on the QUESTION too — the same gap on a
    different question still files. Kills: over-broadening to gap-only."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    lpm = _spy_lpm(monkeypatch)
    _spy_ledger(monkeypatch)
    assert _start(question="q-one", systems=["sys-a"]) is not None
    _release_slot()
    assert _start(question="q-two", systems=["sys-a"]) is not None
    assert len(lpm) == 2


def test_gate_drop_does_not_arm_the_filing_dedupe(monkeypatch):
    """TH-K4: a gate-dropped filing must NOT arm the dedupe — the proposal
    never reached the queue, so an identical corroborated gap may still file
    later. Kills: writing the dedupe record before (or regardless of) a
    successful write."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    results = iter([None, "mid-2"])
    lpm: list = []

    def spy(**kwargs):
        lpm.append(kwargs)
        return next(results)

    monkeypatch.setattr(self_learning, "log_proposed_mapping", spy)
    _spy_ledger(monkeypatch)
    first = _start(session="s1", systems=["sys-a"])
    assert fx._jobs().get(first)["outcome"] == "gate_dropped"
    _release_slot()
    second = _start(session="s2", systems=["sys-a"])
    assert fx._jobs().get(second)["outcome"] == "filed"
    assert len(lpm) == 2


def test_total_bound_enforced_across_both_runs(monkeypatch):
    """TH-B1: the explicit TOTAL bound — with it zeroed the exploration
    aborts (TimeoutError → error outcome, type-only detail) and files
    nothing. The per-run caps alone cannot bound the exploration across two
    runs. Kills: removing the deadline plumbed through ``_single_run``."""
    monkeypatch.setattr(fx, "_TOTAL_CLAMP_SECONDS", 0)
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    lpm = _spy_lpm(monkeypatch)
    bumps = _spy_ledger(monkeypatch)
    exploration_id = _start(systems=["sys-a"])
    assert exploration_id is not None
    job = fx._jobs().get(exploration_id)
    assert job["status"] == "error"
    assert job["error_detail"] == "exploration failed: TimeoutError"
    assert lpm == []
    assert ("feedback-exploration", "error") in bumps


def test_single_run_honors_an_explicit_deadline(monkeypatch):
    """TH-B1 adjunct: ``_single_run`` raises on an already-passed deadline
    and stays back-compatible without one."""
    _script_stream(monkeypatch, list(_DIVERGING_EVENTS))
    with pytest.raises(TimeoutError):
        fx._single_run("p", "sess", deadline=time.monotonic() - 1)
    run = fx._single_run("p", "sess")
    assert run["systems_queried"] == ["sys-a", "sys-b"]


def test_docstring_documents_the_doubled_cost_and_total_bound():
    """Cost honesty (companion to H3): the module docstring states the
    corroboration doubling and names the total bound — a future edit cannot
    quietly hide the 2x cost."""
    doc = fx.__doc__ or ""
    assert "TWICE" in doc
    assert "doubled" in doc.lower()
    assert "_TOTAL_CLAMP_SECONDS" in doc
