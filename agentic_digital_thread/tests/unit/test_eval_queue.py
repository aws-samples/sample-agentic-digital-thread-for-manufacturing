# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Computed review queue + decision store (agents/eval_queue.py) — pinned
invariants (ADR-0062).

Offline — no AWS, no network. The decision store is an in-memory
``JobStore(..., table=None)`` (the ``test_eval_runs.py`` idiom) wrapped in a
SPY PERSISTENCE SEAM that records every write/delete the module attempts; the
feedback source and the ``eval-report`` store are fakes with the same spy
treatment.

Pins:

* ``compute_queue`` performs ZERO writes — the spy seams see no put/delete
  during a read (pin 21);
* persisted rows are MINIMIZED — a RECURSIVE leaf allowlist over every
  persisted scalar path plus a SENTINEL scan proving no source text value ever
  arrives at the store (pin 6; key-name checks alone are not enough);
* synthetic exclusion ITERATES ``runtime_app._SYNTHETIC_SESSION_PREFIXES`` —
  parametrized over the live tuple, plus a monkeypatched fourth prefix (pin
  11);
* steward decisions survive recomputation byte-identical and the machine has
  NO path to write one (pin 18);
* machine pre-scores are deterministic signal lists labelled literally
  ``"triage-only"`` and carry no verdict;
* a decided source that no longer resolves surfaces ``source_expired: True``
  (never an error, never silently dropped);
* the steward note gets the ``untrusted_text`` treatment and a rejection
  detail NEVER echoes it;
* no cost/dollar-shaped key appears anywhere persisted or returned (pin 10);
* the module is import-pure (no boto3 at import).
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess  # nosec B404 - fixed argv, no shell (fresh-interpreter pin)
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_queue as eq  # noqa: E402
import agents.eval_runs as er  # noqa: E402
from agents.job_store import JobStore  # noqa: E402
from agents.storage import feedback_id_for  # noqa: E402

ADT_ROOT = Path(__file__).resolve().parents[2]

# Distinctive source-text sentinels: if ANY of these substrings ever reaches
# the store the sentinel pin fails, whatever key it hides under.
Q_SENTINEL = "ZQUESTIONTEXTSENTINEL7788"
A_SENTINEL = "ZANSWERTEXTSENTINEL7788"
C_SENTINEL = "ZCOMMENTTEXTSENTINEL7788"
EXCERPT_SENTINEL = "ZREPORTEXCERPTSENTINEL7788"
FAILURE_SENTINEL = "ZREPORTFAILURESENTINEL7788"


# ── harness ─────────────────────────────────────────────────────────────────

class _SpyStore:
    """Spy persistence seam around a real in-memory ``JobStore``.

    Records every write/delete (deep-copied, so a later mutation cannot
    rewrite history) while keeping real JobStore read/write semantics.
    """

    def __init__(self, kind: str):
        self._inner = JobStore(kind, table=None)
        self.writes: list = []
        self.deletes: list = []

    # read surface
    def get(self, key, default=None):
        return self._inner.get(key, default)

    def __getitem__(self, key):
        return self._inner[key]

    def __contains__(self, key):
        return key in self._inner

    # write surface (spied)
    def __setitem__(self, key, value):
        self.writes.append((key, copy.deepcopy(value)))
        self._inner[key] = value

    def __delitem__(self, key):
        self.deletes.append(key)
        del self._inner[key]

    def pop(self, key, *args):
        self.deletes.append(key)
        return self._inner.pop(key, *args)

    # test helpers
    @property
    def written_payloads(self) -> list:
        return [payload for _, payload in self.writes]

    def reset_spy(self) -> None:
        self.writes = []
        self.deletes = []


class _FakeFeedback:
    """Feedback source double: in-memory rows + a spied ``store``."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.writes: list = []
        self.range_calls: list = []

    def store(self, record):  # pragma: no cover - must never be called
        self.writes.append(copy.deepcopy(record))
        return True

    def get_by_time_range(self, start, end, sentiment=None):
        self.range_calls.append((start, end, sentiment))
        out = []
        for row in self.rows:
            stamp = str(row.get("timestamp") or "")
            if not (start <= stamp <= end):
                continue
            if sentiment and row.get("sentiment") != sentiment:
                continue
            out.append(copy.deepcopy(row))
        out.sort(key=lambda row: str(row.get("timestamp") or ""))
        return out


def _feedback_row(*, session_id="sess-1", message_id="msg-1",
                  sentiment="negative", days_ago=1, comment=True):
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    row = {
        "feedback_id": feedback_id_for(session_id, message_id),
        "timestamp": stamp,
        "feedback_date": stamp[:10],
        "session_id": session_id,
        "message_id": message_id,
        "user_id": "steward-a",
        "sentiment": sentiment,
        "user_message": f"why is {Q_SENTINEL} late?",
        "assistant_response": f"because {A_SENTINEL}",
        "tools_used": ["erp_query"],
        "cited_systems": ["erp"],
    }
    if comment:
        row["user_comment"] = f"wrong: {C_SENTINEL}"
    return row


def _report(run_id="run1234", question_ids=("g-one",), verdict="fail"):
    return {
        "run_id": run_id,
        "run_path": "server",
        "suite": {"pass": 0, "fail": len(question_ids), "skipped": 0,
                  "total": len(question_ids)},
        "questions": [
            {"id": qid, "session_id": f"eval-run-{run_id}-q1",
             "verdict": verdict,
             "failures": [f"missing pattern {FAILURE_SENTINEL}"],
             "systems_queried": 0, "neptune_queries": 0,
             "elapsed_seconds": 12.0,
             "answer_excerpt": f"answer {EXCERPT_SENTINEL}",
             "honesty": {"absence_markers_matched": [],
                         "fabrication_flags": ["quantified-without-source"]},
             "metrics": None}
            for qid in question_ids
        ],
    }


@pytest.fixture()
def stores(monkeypatch):
    """Fresh spied stores for the module under test AND for the report store
    it reads through ``eval_runs``."""
    decisions = _SpyStore("eval-decision")
    reports = _SpyStore("eval-report")
    jobs = _SpyStore("eval-run")
    feedback = _FakeFeedback()
    monkeypatch.setattr(eq, "_DECISIONS", decisions)
    monkeypatch.setattr(eq, "_FEEDBACK", feedback)
    monkeypatch.setattr(er, "_REPORTS", reports)
    monkeypatch.setattr(er, "_JOBS", jobs)
    return {"decisions": decisions, "reports": reports, "jobs": jobs,
            "feedback": feedback}


def _persist_report(stores, report):
    """Seed the report store the way ``eval_runs`` does, then clear the spy so
    later write assertions only see the module under test."""
    run_id = report["run_id"]
    stores["reports"][run_id] = report
    stores["reports"]["index"] = {"runs": [{"run_id": run_id,
                                            "status": "complete",
                                            "started_at": 1.0,
                                            "finished_at": 2.0,
                                            "run_path": "server"}]}
    stores["jobs"][run_id] = {"status": "complete"}
    for spy in ("reports", "jobs"):
        stores[spy].reset_spy()


def _all_writes(stores) -> list:
    out = []
    for name in ("decisions", "reports", "jobs"):
        out.extend(stores[name].writes)
    return out


def _leaf_keys(payload) -> set:
    keys: set = set()

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(str(key))
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)

    _walk(payload)
    return keys


def _blob(payload) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


# ── source refs + opaque hashing ────────────────────────────────────────────

def test_source_refs_use_the_stores_stable_ids():
    """The feedback ref rides the store's own stable id (session+message
    digest) — not a positional queue index."""
    fid = feedback_id_for("sess-9", "msg-3")
    ref = eq.feedback_source_ref(fid)
    assert ref == f"feedback:{fid}"
    assert eq.source_of(ref) == eq.SOURCE_FEEDBACK
    golden = eq.golden_failure_source_ref("run1234", "g-one")
    assert golden == "golden-failure:run1234:g-one"
    assert eq.source_of(golden) == eq.SOURCE_GOLDEN_FAILURE


def test_ref_sha_is_opaque_stable_and_key_derived():
    ref = eq.feedback_source_ref(feedback_id_for("s", "m"))
    sha = eq.ref_sha(ref)
    assert len(sha) == 16 and sha == eq.ref_sha(ref)
    assert ref not in sha
    assert eq.decision_key(ref) == f"dec-{sha}"


@pytest.mark.parametrize("bogus", [
    None, 42, "", "feedback:", "feedback:not-a-digest",
    "why is order 42 late?",                      # free text
    "feedback:fb-" + "z" * 32,                    # wrong charset
    "golden-failure:run1234",                     # missing question id
    "golden-failure:run1234:q one",               # space smuggled in
    "golden-failure:run1234:" + "q" * 200,        # unbounded
])
def test_unrecognized_source_refs_are_rejected(stores, bogus):
    """Shape validation is the anti-smuggling gate: free text can never be
    persisted in the guise of a reference."""
    assert eq.source_of(bogus) is None
    row, problem = eq.decide(bogus, "incorrect")
    assert row is None and problem[0] == "invalid_source_ref"
    assert _all_writes(stores) == []


# ── compute_queue: pure read ────────────────────────────────────────────────

def test_compute_queue_performs_zero_writes(stores):
    """Pin 21 — computed-on-read means NO writer fires during a GET."""
    stores["feedback"].rows = [_feedback_row(days_ago=1)]
    _persist_report(stores, _report())
    result = eq.compute_queue()
    assert len(result["items"]) == 2
    assert _all_writes(stores) == []
    assert stores["feedback"].writes == []
    for name in ("decisions", "reports", "jobs"):
        assert stores[name].deletes == []


def test_queue_joins_both_sources_with_live_text_and_triage_prescores(stores):
    stores["feedback"].rows = [_feedback_row(days_ago=1)]
    _persist_report(stores, _report())
    items = eq.compute_queue()["items"]
    feedback_item = next(i for i in items if i["source"] == eq.SOURCE_FEEDBACK)
    golden_item = next(i for i in items
                       if i["source"] == eq.SOURCE_GOLDEN_FAILURE)

    # Display text is joined LIVE (transient) — that is the whole point of
    # persisting references only.
    assert Q_SENTINEL in feedback_item["question"]
    assert A_SENTINEL in feedback_item["answer_excerpt"]
    assert C_SENTINEL in feedback_item["user_comment"]
    assert EXCERPT_SENTINEL in golden_item["answer_excerpt"]
    assert golden_item["question_id"] == "g-one"
    assert golden_item["run_id"] == "run1234"

    # Machine pre-scores: deterministic signal ids, literal label, no verdict.
    for item in (feedback_item, golden_item):
        assert item["machine_prescore"]["label"] == "triage-only"
        assert item["machine_prescore"]["signals"] == sorted(
            item["machine_prescore"]["signals"])
        assert set(item["machine_prescore"]) == {"label", "signals"}
        assert item["decision"] is None
        assert item["source_expired"] is False
    assert "user-flagged-negative" in feedback_item["machine_prescore"]["signals"]
    assert {"golden-verdict-fail", "fabrication-flagged", "no-systems-queried",
            "no-neptune-query"} <= set(golden_item["machine_prescore"]["signals"])


def test_prescore_is_deterministic_and_never_carries_a_verdict():
    facts = {"sentiment": "negative", "has_comment": True,
             "cited_systems": [], "cited_rules": []}
    first = eq.machine_prescore(eq.SOURCE_FEEDBACK, facts)
    second = eq.machine_prescore(eq.SOURCE_FEEDBACK, dict(facts))
    assert first == second
    assert not any(key in _leaf_keys(first)
                   for key in ("verdict", "decision", "score", "confidence"))


def test_negative_feedback_is_ordered_first_then_window_then_goldens(stores):
    stores["feedback"].rows = [
        _feedback_row(session_id="s-pos", message_id="m-pos",
                      sentiment="positive", days_ago=1, comment=False),
        _feedback_row(session_id="s-neg", message_id="m-neg",
                      sentiment="negative", days_ago=3),
    ]
    _persist_report(stores, _report())
    items = eq.compute_queue()["items"]
    assert [i.get("sentiment") or i["source"] for i in items] == [
        "negative", "positive", eq.SOURCE_GOLDEN_FAILURE]


def test_window_and_limit_are_bounded(stores):
    stores["feedback"].rows = [
        _feedback_row(session_id=f"s-{n}", message_id=f"m-{n}", days_ago=n)
        for n in range(1, 6)
    ]
    result = eq.compute_queue(window_days=10_000, limit=2)
    assert result["window_days"] == eq.MAX_WINDOW_DAYS
    assert result["limit"] == 2
    assert len(result["items"]) == 2
    # Unparseable inputs fall back to the defaults rather than exploding.
    fallback = eq.compute_queue(window_days="nope", limit=None)
    assert fallback["window_days"] == eq.DEFAULT_WINDOW_DAYS
    assert fallback["limit"] == eq.DEFAULT_LIMIT


def test_rows_outside_the_window_are_not_read(stores):
    stores["feedback"].rows = [_feedback_row(days_ago=40)]
    assert eq.compute_queue(window_days=7)["items"] == []


def test_only_failed_report_rows_enter_the_queue(stores):
    _persist_report(stores, _report(question_ids=("g-ok",), verdict="pass"))
    assert eq.compute_queue()["items"] == []


def test_counts_are_computed_and_carry_no_cost_shaped_key(stores):
    stores["feedback"].rows = [_feedback_row()]
    result = eq.compute_queue()
    assert result["counts"] == {"total": 1, "decided": 0, "undecided": 1,
                                "source_expired": 0}
    blob = _blob(result).lower()
    for marker in ("dollar", "cost_usd", "estimate", "price", "$"):
        assert marker not in blob


# ── synthetic containment (pin 11) ─────────────────────────────────────────

def _live_prefixes():
    from agents.runtime_app import _SYNTHETIC_SESSION_PREFIXES
    return _SYNTHETIC_SESSION_PREFIXES


@pytest.mark.parametrize("prefix", list(_live_prefixes()))
def test_every_live_synthetic_prefix_is_excluded(stores, prefix):
    """Parametrized over the LIVE tuple — a new prefix auto-extends the pin."""
    stores["feedback"].rows = [
        _feedback_row(session_id=f"{prefix}job-42-a", message_id="m-syn"),
        _feedback_row(session_id="real-session", message_id="m-real"),
    ]
    items = eq.compute_queue()["items"]
    assert len(items) == 1
    assert items[0]["source_ref"] == eq.feedback_source_ref(
        feedback_id_for("real-session", "m-real"))


def test_a_new_prefix_extends_the_filter_without_a_code_edit(stores,
                                                             monkeypatch):
    from agents import runtime_app
    monkeypatch.setattr(
        runtime_app, "_SYNTHETIC_SESSION_PREFIXES",
        tuple(_live_prefixes()) + ("brand-new-synthetic-",))
    stores["feedback"].rows = [
        _feedback_row(session_id="brand-new-synthetic-1", message_id="m-new")]
    assert eq.compute_queue()["items"] == []


def test_golden_failures_are_not_dropped_by_the_synthetic_filter(stores):
    """Self-referential contamination guard: eval-run sessions are synthetic
    BY CONSTRUCTION, so the prefix filter must apply to feedback only — the
    golden suite's own failures stay a first-class queue source."""
    _persist_report(stores, _report())
    items = eq.compute_queue()["items"]
    assert [i["source"] for i in items] == [eq.SOURCE_GOLDEN_FAILURE]
    assert items[0]["source_ref"] == "golden-failure:run1234:g-one"


# ── decisions: the only writer ─────────────────────────────────────────────

def test_decision_is_persisted_and_survives_recomputation_byte_identical(
        stores):
    stores["feedback"].rows = [_feedback_row()]
    ref = eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1"))
    row, problem = eq.decide(ref, "incorrect", ["wrong_join"],
                             note="join went through the wrong key",
                             decided_by="steward-a")
    assert problem is None and row["verdict"] == "incorrect"

    first = eq.compute_queue()["items"][0]["decision"]
    stores["decisions"].reset_spy()
    second = eq.compute_queue()["items"][0]["decision"]
    assert _blob(first) == _blob(second)
    assert first["verdict"] == "incorrect"
    assert first["taxonomy_tags"] == ["wrong_join"]
    assert first["steward_note"] == "join went through the wrong key"
    # Recomputation is still a pure read — the steward row is untouched.
    assert stores["decisions"].writes == []
    assert _blob(eq.get_decision(ref)) == _blob(row)


def test_decide_upserts_one_row_and_one_bounded_index_entry(stores):
    ref = eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1"))
    eq.decide(ref, "correct")
    eq.decide(ref, "needs_context")
    keys = [key for key, _ in stores["decisions"].writes]
    assert keys == [eq.decision_key(ref), "index",
                    eq.decision_key(ref), "index"]
    assert eq.get_decision(ref)["verdict"] == "needs_context"
    entries = eq.list_decisions()["decisions"]
    assert len(entries) == 1 and entries[0]["source_ref"] == ref


@pytest.mark.parametrize("verdict", list(eq.VERDICTS))
def test_every_pinned_verdict_is_accepted(stores, verdict):
    ref = eq.golden_failure_source_ref("run1234", "g-one")
    row, problem = eq.decide(ref, verdict)
    assert problem is None and row["verdict"] == verdict


@pytest.mark.parametrize("verdict", ["", None, "pass", "fail", "rejected",
                                     "CORRECT", 1])
def test_unknown_verdicts_are_refused_without_a_write(stores, verdict):
    ref = eq.golden_failure_source_ref("run1234", "g-one")
    row, problem = eq.decide(ref, verdict)
    assert row is None and problem[0] == "invalid_verdict"
    assert _all_writes(stores) == []


@pytest.mark.parametrize("tags", [
    "wrong_join", ["Wrong Join"], ["x" * 60], [None],
    ["t%d" % n for n in range(eq.MAX_TAGS + 1)],
])
def test_bad_taxonomy_tags_are_refused_without_a_write(stores, tags):
    ref = eq.golden_failure_source_ref("run1234", "g-one")
    row, problem = eq.decide(ref, "incorrect", tags)
    assert row is None and problem[0] == "invalid_tags"
    assert _all_writes(stores) == []


def test_machine_paths_never_write_a_decision(stores):
    """Pin 18 — no machine code path can set a decision: ``decide`` is the
    ONLY writer, and it is source-grep-pinned as the only assignment site."""
    stores["feedback"].rows = [_feedback_row()]
    _persist_report(stores, _report())
    eq.compute_queue()
    eq.machine_prescore(eq.SOURCE_FEEDBACK, {"sentiment": "negative"})
    eq.list_decisions()
    eq.get_decision(eq.golden_failure_source_ref("run1234", "g-one"))
    assert _all_writes(stores) == []

    source = (ADT_ROOT / "agents" / "eval_queue.py").read_text()
    writers = sorted(line.strip() for line in source.splitlines()
                     if "_decisions()[" in line or "store[" in line)
    assert writers == sorted([
        '_decisions()[decision_key(source_ref)] = row',
        'store[_INDEX_KEY] = {"decisions": entries[:_MAX_INDEX_DECISIONS]}'])
    # ...and the index writer is reachable only from decide().
    assert "_update_index(row)" in source
    assert source.count("_update_index(") == 2


# ── steward note: untrusted text ───────────────────────────────────────────

def test_note_is_sanitized_and_bounded(stores):
    ref = eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1"))
    row, problem = eq.decide(ref, "needs_context",
                             note="  line one\r\nline\x07 two  ")
    assert problem is None
    assert row["steward_note"] == "line one\nline two"

    row, problem = eq.decide(ref, "needs_context",
                             note="x" * (eq.NOTE_MAX_CHARS + 1))
    assert row is None and problem[0] == "too_long"


@pytest.mark.parametrize("note,reason", [
    ("AKIAIOSFODNN7EXAMPLE", "secret_like_content"),
    ("password: hunter2-not-a-real-secret", "secret_like_content"),
    ("<<<BUSINESS_CONTEXT smuggled", "fence_delimiter"),
    (7, "invalid_input"),
])
def test_secret_shaped_or_smuggling_notes_are_refused_and_never_echoed(
        stores, note, reason):
    ref = eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1"))
    row, problem = eq.decide(ref, "incorrect", note=note)
    assert row is None and problem[0] == reason
    assert str(note) not in problem[1]
    assert _all_writes(stores) == []


def test_an_empty_note_is_simply_absent(stores):
    ref = eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1"))
    row, problem = eq.decide(ref, "correct", note="   ")
    assert problem is None and "steward_note" not in row


def test_decided_by_must_be_id_shaped(stores):
    ref = eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1"))
    row, problem = eq.decide(ref, "correct", decided_by="steward one <hi>")
    assert row is None and problem[0] == "invalid_decided_by"
    assert _all_writes(stores) == []


# ── minimization: recursive allowlist + sentinel spy seam (pin 6) ───────────

def _decide_both_sources(stores):
    stores["feedback"].rows = [_feedback_row()]
    _persist_report(stores, _report())
    eq.decide(eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1")),
              "incorrect", ["wrong_join", "missing_context"],
              note="explanation with no customer text", decided_by="steward-a")
    eq.decide(eq.golden_failure_source_ref("run1234", "g-one"),
              "needs_context", note="needs the plant scope")
    eq.compute_queue()


def test_allowlist_constants_are_pinned(stores):
    """Deliberate-edit gate: extending a persisted row means editing THIS
    literal set (and the ADR), not just the module."""
    assert eq.PERSISTED_DECISION_KEYS == frozenset({
        "source_ref", "source", "ref_sha", "verdict", "taxonomy_tags",
        "steward_note", "decided_at", "decided_by"})
    assert eq.PERSISTED_INDEX_KEYS == frozenset({
        "decisions", "source_ref", "source", "ref_sha", "decided_at"})


def test_every_persisted_leaf_key_is_allowlisted(stores):
    """RECURSIVE allowlist over EVERYTHING the store received — a renamed or
    nested text field fails regardless of its key name."""
    _decide_both_sources(stores)
    allowed = eq.PERSISTED_DECISION_KEYS | eq.PERSISTED_INDEX_KEYS
    assert stores["decisions"].writes, "expected persisted decision rows"
    for key, payload in stores["decisions"].writes:
        assert key == "index" or key.startswith("dec-")
        offenders = _leaf_keys(payload) - allowed
        assert not offenders, f"non-allowlisted persisted keys: {offenders}"


def test_no_source_text_value_ever_reaches_the_store(stores):
    """Spy persistence seam, VALUE-level: no source text substring appears in
    anything the store received (the pin key-name checks would miss)."""
    _decide_both_sources(stores)
    persisted = _blob(_all_writes(stores))
    for sentinel in (Q_SENTINEL, A_SENTINEL, C_SENTINEL, EXCERPT_SENTINEL,
                     FAILURE_SENTINEL):
        assert sentinel not in persisted
    # ...while the transient queue item still carries the live text.
    assert Q_SENTINEL in _blob(eq.compute_queue()["items"])


def test_a_future_non_allowlisted_field_is_refused_at_write_time(stores,
                                                                 monkeypatch):
    """The module's own guard: a row that grows a field without the allowlist
    growing with it raises rather than persisting."""
    monkeypatch.setattr(eq, "PERSISTED_DECISION_KEYS",
                        frozenset(eq.PERSISTED_DECISION_KEYS) - {"verdict"})
    with pytest.raises(ValueError):
        eq.decide(eq.golden_failure_source_ref("run1234", "g-one"), "correct")
    assert _all_writes(stores) == []


# ── dangling / expired sources ─────────────────────────────────────────────

def test_a_decided_source_that_expired_surfaces_source_expired(stores):
    """Feedback rows carry a 90-day TTL: a decision can outlive its source.
    That is a distinct STATE, never an error and never a silent drop."""
    ref = eq.feedback_source_ref(feedback_id_for("sess-gone", "msg-gone"))
    eq.decide(ref, "incorrect", note="decided while the row still existed")
    stores["feedback"].rows = []          # the source aged out
    stores["decisions"].reset_spy()

    result = eq.compute_queue()
    item = next(i for i in result["items"] if i["source_ref"] == ref)
    assert item["source_expired"] is True
    assert item["question"] is None and item["answer_excerpt"] is None
    assert item["decision"]["verdict"] == "incorrect"
    assert item["machine_prescore"] == {"label": "triage-only", "signals": []}
    assert result["counts"]["source_expired"] == 1
    assert stores["decisions"].writes == []   # still a pure read


def test_a_golden_report_that_aged_out_surfaces_source_expired(stores):
    _persist_report(stores, _report())
    ref = eq.golden_failure_source_ref("run1234", "g-one")
    eq.decide(ref, "correct")
    assert eq.compute_queue()["items"][0]["source_expired"] is False

    stores["reports"]["index"] = {"runs": []}   # rolled out of the window
    stores["decisions"].reset_spy()
    items = eq.compute_queue()["items"]
    assert [i["source_expired"] for i in items] == [True]
    assert items[0]["decision"]["verdict"] == "correct"


def test_a_live_source_is_never_reported_expired(stores):
    stores["feedback"].rows = [_feedback_row()]
    eq.decide(eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1")),
              "correct")
    result = eq.compute_queue()
    assert [i["source_expired"] for i in result["items"]] == [False]
    assert result["counts"]["decided"] == 1


def test_a_malformed_index_entry_does_not_crash_the_read(stores):
    stores["decisions"]["index"] = {"decisions": [None, {"source_ref": 42},
                                                 {"nope": "x"}]}
    stores["decisions"].reset_spy()
    assert eq.compute_queue()["items"] == []
    assert stores["decisions"].writes == []


# ── module hygiene ─────────────────────────────────────────────────────────

def test_reset_state_drops_both_handles(monkeypatch):
    monkeypatch.setattr(eq, "_DECISIONS", object())
    monkeypatch.setattr(eq, "_FEEDBACK", object())
    eq._reset_state()
    assert eq._DECISIONS is None and eq._FEEDBACK is None


def test_module_is_import_pure():
    src = (ADT_ROOT / "agents" / "eval_queue.py").read_text()
    assert "import boto3" not in src
    assert "import pg8000" not in src and "import pgdriver" not in src


def test_module_imports_with_boto3_absent():
    """Fresh interpreter with boto3 BOMBED must still import the module
    (subprocess fresh-interpreter convention)."""
    code = (
        "import sys\n"
        "sys.modules['boto3'] = None\n"
        "import agents.eval_queue as eq\n"
        "assert eq.source_of('golden-failure:r1:q1') == 'golden-failure'\n"
        "assert eq.PRESCORE_LABEL == 'triage-only'\n"
        "print('IMPORT-PURE-OK')\n"
    )
    env = dict(os.environ, PYTHONPATH=str(ADT_ROOT))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ADT_ROOT),  # nosec B603 - fixed argv, no shell
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout


def test_no_refresh_writer_or_persisted_queue_exists():
    """80/20 cut 1 — the queue is computed on read; a refresh entry point (or
    an ``eval-queue`` store kind) would resurrect the staleness class the
    design removed. Scoped to IDENTIFIERS so the prose may still explain it."""
    src = (ADT_ROOT / "agents" / "eval_queue.py").read_text()
    assert not re.search(r"\bdef\s+\w*refresh\w*\s*\(", src)
    assert not re.search(r"\b\w*refresh\w*\s*=", src)
    assert 'JobStore("eval-queue"' not in src
