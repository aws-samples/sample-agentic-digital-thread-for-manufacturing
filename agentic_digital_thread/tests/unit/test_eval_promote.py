# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Promoted-golden draft builder (agents/eval_promote.py) — ADR-0062 D5.

Offline, stdlib-only. Pins:

* the builder PERSISTS NOTHING — it owns no store handle, opens no file,
  touches no ``tempfile``, and a spy over the decision store records zero
  writes during a build (the route-level seam is in
  ``test_eval_queue_routes.py``);
* generated drafts carry NO ``answer_must_match`` and no code path can emit
  one (constant-level + built-draft + source-grep pins);
* observed counters live ONLY in a non-gating ``hints`` block: hint keys are
  disjoint from the gate keys, and scoring a question with and without the
  block yields IDENTICAL verdicts/failures/fabrication flags;
* provenance is exactly ``{source: "promoted", source_ref_sha}`` — NO
  ``decided_by``/identity leaf anywhere in the draft, at any nesting;
* the draft round-trips through ``eval_core.load_golden_set`` wrapped as
  ``{version, questions: [draft]}`` (the loader already ignores unknown
  per-question keys — pinned here so a future loader tightening cannot break
  the interchange silently);
* the build is DETERMINISTIC (no timestamp/uuid/randomness) and involves no
  LLM (no model/bedrock/strands import anywhere in the module).
"""
from __future__ import annotations

import ast
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_core as eval_core  # noqa: E402
import agents.eval_promote as ep  # noqa: E402
import agents.eval_queue as eq  # noqa: E402
from agents.job_store import JobStore  # noqa: E402
from agents.storage import feedback_id_for  # noqa: E402

Q_SENTINEL = "ZPROMOTEQUESTIONSENTINEL7733"

#: Every identity/text leaf that must never appear in an exported draft.
FORBIDDEN_LEAVES = {
    "decided_by", "decided_at", "source_ref", "steward_note", "verdict",
    "taxonomy_tags", "user_id", "email", "session_id", "message_id",
    "answer_excerpt", "user_comment", "run_id", "ref_sha",
}


# ── fixtures ────────────────────────────────────────────────────────────────

def _ref() -> str:
    return eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1"))


def _decision(**over) -> dict:
    row = {"source_ref": _ref(), "source": eq.SOURCE_FEEDBACK,
           "ref_sha": eq.ref_sha(_ref()), "verdict": "incorrect",
           "taxonomy_tags": ["wrong-number"],
           "steward_note": "checked against the ERP row",
           "decided_at": "2026-08-18T00:00:00+00:00",
           "decided_by": "steward@example.com"}
    row.update(over)
    return row


def _turn(**over) -> dict:
    item = {"source_ref": _ref(), "ref_sha": eq.ref_sha(_ref()),
            "source": eq.SOURCE_FEEDBACK,
            "question": f"why is {Q_SENTINEL} late?",
            "answer_excerpt": "because the line was down",
            "user_comment": "wrong plant",
            "cited_systems": ["erp", "mes"],
            "machine_prescore": {"label": "triage-only", "signals": []},
            "source_expired": False}
    item.update(over)
    return item


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


class _SpyStore:
    """Spy persistence seam over an in-memory ``JobStore``."""

    def __init__(self, kind: str):
        self._inner = JobStore(kind, table=None)
        self.writes: list = []
        self.deletes: list = []

    def get(self, key, default=None):
        return self._inner.get(key, default)

    def __getitem__(self, key):
        return self._inner[key]

    def __setitem__(self, key, value):
        self.writes.append((key, copy.deepcopy(value)))
        self._inner[key] = value

    def __delitem__(self, key):
        self.deletes.append(key)
        del self._inner[key]

    def pop(self, key, *args):
        self.deletes.append(key)
        return self._inner.pop(key, *args)


# ── the draft's content contract ────────────────────────────────────────────

def test_draft_has_no_answer_must_match_and_none_is_generatable():
    """D5: no generated phrase gate — in the constant, in the built draft, or
    anywhere in the module source."""
    draft = ep.draft_golden(_decision(), _turn())
    assert ep.FORBIDDEN_EXPECT_KEY == "answer_must_match"
    assert ep.FORBIDDEN_EXPECT_KEY not in ep.DEFAULT_EXPECT
    assert ep.FORBIDDEN_EXPECT_KEY not in draft["expect"]
    assert ep.FORBIDDEN_EXPECT_KEY not in _leaf_keys(draft)
    source = Path(ep.__file__).read_text(encoding="utf-8")
    # The only mentions may be the constant + docstring prose, never an
    # assignment INTO an expect block.
    assert '"answer_must_match"]' not in source
    assert "answer_must_match':" not in source
    assert '"answer_must_match":' not in source


def test_draft_shape_is_the_pinned_minimum():
    draft = ep.draft_golden(_decision(), _turn())
    assert set(draft) == set(ep.DRAFT_TOP_KEYS)
    assert draft["id"] == f"promoted-{eq.ref_sha(_ref())}"
    assert Q_SENTINEL in draft["text"]
    assert draft["expect"] == ep.DEFAULT_EXPECT
    assert draft["expect"] is not ep.DEFAULT_EXPECT  # no shared mutable state


def test_provenance_is_minimized_and_carries_no_identity():
    draft = ep.draft_golden(_decision(), _turn())
    assert draft["provenance"] == {"source": "promoted",
                                   "source_ref_sha": eq.ref_sha(_ref())}
    assert set(draft["provenance"]) == set(ep.PROVENANCE_KEYS)
    # Recursive: a renamed or nested identity field fails too.
    assert not (FORBIDDEN_LEAVES & _leaf_keys(draft))
    body = json.dumps(draft)
    assert "steward@example.com" not in body
    assert _ref() not in body            # no RAW source ref, only the sha
    assert "checked against the ERP row" not in body


def test_leaf_allowlist_rejects_a_future_unlisted_field(monkeypatch):
    """The allowlist is the gate, not code review: an added draft field must
    extend it (and the ADR) or the build fails loudly."""
    monkeypatch.setitem(ep.DEFAULT_EXPECT, "decided_by", "steward@example.com")
    with pytest.raises(ValueError, match="non-allowlisted"):
        ep.draft_golden(_decision(), _turn())


@pytest.mark.parametrize("over", [
    {"question": ""}, {"question": "   "}, {"question": None},
], ids=["empty", "blank", "null"])
def test_draft_refuses_a_turn_without_question_text(over):
    with pytest.raises(ValueError):
        ep.draft_golden(_decision(), _turn(**over))


def test_draft_refuses_a_decision_without_a_recognized_ref():
    with pytest.raises(ValueError):
        ep.draft_golden({"source_ref": "why is order 42 late?"}, _turn())
    with pytest.raises(ValueError):
        ep.draft_golden({}, _turn())


def test_ref_sha_is_derived_when_the_row_predates_the_field():
    draft = ep.draft_golden(_decision(ref_sha=None), _turn())
    assert draft["provenance"]["source_ref_sha"] == eq.ref_sha(_ref())


def test_text_is_bounded():
    long_question = "q" * (ep.TEXT_MAX_CHARS + 200)
    draft = ep.draft_golden(_decision(), _turn(question=long_question))
    assert len(draft["text"]) == ep.TEXT_MAX_CHARS


# ── hints: observed counters, non-gating ────────────────────────────────────

def test_hints_carry_observed_counters_from_the_intelligence_summary():
    turn = _turn(intelligence_summary={
        "system_queries": 3, "neptune_queries": 0, "total_tool_calls": 7,
        "rules_applied": ["r-1", "r-2"], "graph_coverage_pct": 42})
    hints = ep.draft_golden(_decision(), turn)["hints"]
    assert hints == {"observed_systems": 3, "observed_neptune_queries": 0,
                     "observed_tool_calls": 7, "observed_rules_applied": 2}
    assert set(hints) <= ep.HINT_KEYS


def test_unmeasured_counters_are_absent_never_zero():
    """Honest-data rule: a missing counter is an ABSENT key, not a 0."""
    hints = ep.draft_golden(_decision(), _turn(
        cited_systems=[], intelligence_summary={"neptune_queries": None}))["hints"]
    assert hints == {"observed_systems": 0}  # measured: zero cited systems
    assert "observed_neptune_queries" not in hints
    assert ep.observed_hints({}) == {}


def test_hint_keys_never_collide_with_gate_keys():
    gate_keys = set(ep.DEFAULT_EXPECT) | {ep.FORBIDDEN_EXPECT_KEY,
                                          "must_not_quantify",
                                          "answer_must_not_match"}
    assert not (ep.HINT_KEYS & gate_keys)


def test_hints_are_ignored_by_scoring():
    """Non-gating, computed: scoring reads ``expect`` only, so removing the
    hints block cannot change a verdict, a failure list or a honesty flag."""
    draft = ep.draft_golden(_decision(), _turn(intelligence_summary={
        "system_queries": 9, "neptune_queries": 9, "total_tool_calls": 9}))
    bare = {key: value for key, value in draft.items() if key != "hints"}
    answer = "The order is late because line 3 was down."
    summary = {"system_queries": 1, "neptune_queries": 0, "rules_applied": []}
    with_hints = eval_core.score_question(
        draft["expect"], summary, answer, [], question_text=draft["text"])
    without = eval_core.score_question(
        bare["expect"], summary, answer, [], question_text=bare["text"])
    assert with_hints == without == ("pass", [])
    assert eval_core.fabrication_flags(draft["expect"], draft["text"],
                                       answer) == []


# ── interchange: the loader round-trip ──────────────────────────────────────

def test_draft_bundle_round_trips_through_load_golden_set(tmp_path):
    """The download's own shape loads through the SAME loader
    ``eval_answers.py --golden-set`` uses — including the unknown-to-the-loader
    ``hints``/``provenance`` keys (ignore-unknown, pinned)."""
    draft = ep.draft_golden(_decision(), _turn())
    bundle = ep.draft_bundle(draft)
    assert bundle == {"version": eval_core.GOLDEN_SET_VERSION,
                      "questions": [draft]}
    path = tmp_path / "external_golden.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    questions = eval_core.load_golden_set(str(path))
    assert len(questions) == 1
    assert questions[0]["hints"] == draft["hints"]
    assert questions[0]["provenance"]["source"] == "promoted"


def test_loader_ignores_unknown_question_and_document_keys(tmp_path):
    """The ignore-unknown contract the interchange depends on, pinned directly
    (a future loader tightening must break THIS test, not a live download)."""
    draft = ep.draft_golden(_decision(), _turn())
    doc = ep.draft_bundle(draft)
    doc["exported_by_tool"] = "adt"
    doc["questions"][0]["future_key"] = {"nested": ["value"]}
    path = tmp_path / "unknown_keys.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert len(eval_core.load_golden_set(str(path))) == 1


# ── determinism + no persistence, no LLM ────────────────────────────────────

def test_build_is_deterministic():
    first = ep.draft_golden(_decision(), _turn())
    second = ep.draft_golden(_decision(), _turn())
    assert json.dumps(first, sort_keys=True) == json.dumps(second,
                                                           sort_keys=True)
    assert not ({"created_at", "generated_at", "timestamp", "uuid", "nonce"}
                & _leaf_keys(first))


def test_building_a_draft_writes_nothing(monkeypatch):
    """Persistence seam: the store the module could reach records no write —
    and the builder never opens a file or a temp file."""
    spy = _SpyStore("eval-decision")
    monkeypatch.setattr(eq, "_DECISIONS", spy)
    opened: list = []
    real_open = open
    monkeypatch.setattr("builtins.open",
                        lambda *a, **k: opened.append(a) or real_open(*a, **k))
    draft = ep.draft_golden(_decision(), _turn())
    assert draft["text"]
    assert spy.writes == [] and spy.deletes == []
    assert opened == []


def test_module_owns_no_store_no_file_and_no_model_seam():
    """Architectural pin (AST, not prose): the builder imports only stdlib +
    the two pure agents modules, calls no ``open``, and reaches no model seam —
    the draft is DETERMINISTIC, so no LLM exists in this path."""
    tree = ast.parse(Path(ep.__file__).read_text(encoding="utf-8"))
    imported: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            if node.level:  # relative: also record the names pulled in
                imported.update(alias.name for alias in node.names)
    imported.discard("")  # ``from . import x`` carries no module name
    assert imported <= {"__future__", "copy", "re", "annotations",
                        "eval_core", "eval_queue", "_unexpected_leaves",
                        "ref_sha", "source_of"}
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not ({"open", "print", "eval", "exec"} & called)


def test_download_headers_are_the_pinned_no_store_attachment():
    draft = ep.draft_golden(_decision(), _turn())
    headers = ep.download_headers(draft)
    sha = eq.ref_sha(_ref())
    assert headers == {
        "Content-Disposition": f'attachment; filename="golden-draft-{sha}.json"',
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
    }
    # The filename is OPAQUE: no question text, no ids, just the sha.
    assert Q_SENTINEL not in headers["Content-Disposition"]
    assert ep.draft_filename(draft) == f"golden-draft-{sha}.json"


def test_filename_requires_an_opaque_sha():
    with pytest.raises(ValueError):
        ep.draft_filename({"provenance": {"source_ref_sha": "not-a-sha"}})


# --------------------------------------------------------------------------- #
# ADR-0063 late-arrival advice: WIRED here (2026-08-19), and the reason it is
# still silent is recorded rather than hidden.
#
# The advice shipped 2026-08-18 with tests and NO caller. Two goldens then
# failed on exactly the drift it exists to warn about (a count that outgrew
# its pinned band; a rate that gained a decimal place). Wiring it revealed a
# SECOND gap: its only input is a measured row count (`observed_count`), and
# NOTHING in the engine produces one — so it cannot fire yet. These tests pin
# both halves: the seam is connected, and the silence is honest-absence, not
# a swallowed failure. Do not "fix" the silence by scraping a number out of
# answer prose — a hint named `observed_*` must be MEASURED.
# --------------------------------------------------------------------------- #
def test_draft_hints_run_through_the_stability_advice_seam():
    from agents import eval_core, eval_promote
    # The allowlist must ADMIT the advice keys, or the leak guard would raise
    # the moment a producer appears.
    assert eval_core.STABILITY_ADVICE_KEYS <= eval_promote.ALLOWED_DRAFT_LEAVES
    draft = eval_promote.draft_golden(
        {"ref_sha": "a" * 16},
        {"question": "How many rows completed in the window?",
         "intelligence_summary": {"system_queries": 1}})
    # Advice keys are ABSENT today (no measured count), never fabricated.
    assert not (eval_core.STABILITY_ADVICE_KEYS & set(draft["hints"]))
    assert draft["hints"]["observed_systems"] == 1


def test_advice_reaches_the_draft_once_the_evidence_is_measured():
    """The seam works — proven by supplying evidence the engine cannot yet.

    The advice needs MORE than a count: an accrual signal, from measured
    `late_arrival_rows` or a window-closed/latest-ingested instant pair. That
    is the true size of the missing producer, recorded here so nobody assumes
    a row count alone would switch it on.
    """
    from agents import eval_core, eval_promote
    hints = eval_core.with_stability_advice({
        "observed_systems": 1,
        eval_core.OBSERVED_COUNT_KEY: 5_305_145,
        "late_arrival_rows": 109,   # the +109 drift the golden itself recorded
    })
    assert eval_core.STABILITY_ADVICE_KEYS & set(hints), (
        "advice must appear once the accrual evidence is measured")
    assert hints["accrual_signal"] == eval_core.ACCRUAL_ACCRUING
    # And it stays ADVICE: never a generated gate (ADR-0062).
    assert eval_promote.FORBIDDEN_EXPECT_KEY not in hints
    assert eval_promote.FORBIDDEN_EXPECT_KEY not in eval_promote.DEFAULT_EXPECT


def test_a_count_alone_yields_no_advice():
    """Honest absence: a count with no accrual evidence cannot tell, so the
    advice stays absent rather than guessing 'settled'."""
    from agents import eval_core
    hints = eval_core.with_stability_advice(
        {eval_core.OBSERVED_COUNT_KEY: 5_305_145})
    assert not (eval_core.STABILITY_ADVICE_KEYS & set(hints))


def test_no_engine_producer_of_observed_count_yet():
    """Pins the KNOWN gap so it is discovered by a failing test, not by a
    golden silently drifting again. When a producer lands, this test should
    be updated to assert the producer instead of its absence."""
    import pathlib
    agents_dir = pathlib.Path(__file__).resolve().parents[2] / "agents"
    producers = [p.name for p in agents_dir.glob("*.py")
                 if "observed_count" in p.read_text(encoding="utf-8")
                 and p.name != "eval_core.py"]
    assert producers == [], f"a producer appeared: {producers} — update the advice wiring"
