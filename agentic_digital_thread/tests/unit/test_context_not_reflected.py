# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0063 D5 — the context edit that did not take, and its promotion.

Two mechanisms, both offline:

1. ``failure_signals`` gains the ``context_not_reflected`` shape: a SAVED and
   ENABLED business-context CORRECTION that subsequent answers still
   contradict. Pins: the signal accumulates ONLY with evidence (the edit's
   context hash AND the answers that contradicted it are named), never
   otherwise; evidence storage is BOUNDED while counting continues; threshold
   and per-turn dedup match the module's other shapes; determinism; the
   never-raise boundary.
2. ``POST /api/admin/vocabulary/guidance/{system_id}/promote-requirement``
   files the requirement phrasing as a ``guidance_note`` through the EXISTING
   ``/propose`` path (``source="user"`` / ``provenance="human-asserted"``),
   which is in the never-auto-accept deny-tuple.

Across both: ``business_context.write_business_context`` is monkeypatched to
RAISE, so any attempt to machine-rewrite the steward's own text fails the test
(ADR-0054 — the steward's text is the steward's; the note is ADDITIVE).

Engine-neutral throughout: generic system ids (``sys-a``) and generic
manufacturing wording only.
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.failure_signals as fs  # noqa: E402
import agents.self_learning as sl  # noqa: E402
import agents.vocabulary_routes as vr  # noqa: E402

SYSTEM = "sys-a"

# A steward context version carrying three CORRECTIONS, one per supported form.
CONTEXT_TEXT = (
    "Cycle time is recorded in minutes, not seconds. "
    "Availability is measured per line rather than per station. "
    "Never abbreviate the line identifier."
)


def _context(*, enabled=True, saved=True, text=CONTEXT_TEXT, digest="abc123def456"):
    """The ``business_context.read_business_context`` shape."""
    return {"business_context": text, "enabled": enabled,
            "updated_at": "2026-08-18T00:00:00Z" if saved else None,
            "context_hash": digest}


# ── fakes ────────────────────────────────────────────────────────────────────


class _Conditional(Exception):
    pass


_Conditional.__name__ = "ConditionalCheckFailedException"


class FakeTable:
    """DynamoDB double covering the conditions this module writes: the
    turn-dedup ADD and the ``size(evidence_refs) < :cap`` bounded append.
    The COMPOSITE key is required (the live-found ValidationException lesson)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.updates: list[tuple[str, str]] = []

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ConditionExpression="", ExpressionAttributeNames=None):
        self.updates.append((UpdateExpression, ConditionExpression))
        assert "mapping_id" in Key and "discovered_at" in Key
        v = ExpressionAttributeValues
        row = self.rows.get(Key["mapping_id"])
        with_ref = ":ref" in v
        if with_ref:
            assert "list_append" in UpdateExpression
            assert "size(evidence_refs) < :cap" in ConditionExpression
        if row is not None and v[":turn"] in row.get("turn_ids", set()):
            raise _Conditional()
        if with_ref and row is not None \
                and len(row.get("evidence_refs", [])) >= v[":cap"]:
            raise _Conditional()
        row = row or {"mapping_id": Key["mapping_id"], "turn_ids": set(),
                      "sig_count": 0, "evidence_refs": [],
                      "first_seen": v[":now"]}
        row["turn_ids"] = set(row["turn_ids"]) | set(v[":tset"])
        row["sig_count"] += v[":one"]
        row.update(last_seen=v[":now"], shape=v[":shape"], system_id=v[":sys"],
                   subject_json=v[":subj"], ttl_epoch=v[":ttl"])
        if with_ref:
            row["evidence_refs"] = list(row["evidence_refs"]) + list(v[":ref"])
        self.rows[Key["mapping_id"]] = row

    def scan(self, **kwargs):
        return {"Items": [dict(r, turn_ids=list(r.get("turn_ids", [])))
                          for r in self.rows.values()]}


class RaisingTable:
    def update_item(self, **kwargs):
        raise RuntimeError("store down")

    def scan(self, **kwargs):
        raise RuntimeError("store down")


@pytest.fixture()
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(sl, "_get_table", lambda: t)
    return t


@pytest.fixture(autouse=True)
def context_write_forbidden(monkeypatch):
    """Write-forbidden guard: nothing on this path may write business context."""
    def _boom(*_a, **_k):
        raise AssertionError("business_context.write_business_context called — "
                             "the steward's text is never machine-written")
    monkeypatch.setattr("agents.business_context.write_business_context", _boom)
    monkeypatch.setattr("agents.business_context.toggle_business_context", _boom)


# ── directive parsing (pure) ────────────────────────────────────────────────


def test_directives_read_each_corrective_form():
    got = fs.context_directives(CONTEXT_TEXT)
    assert {"asserted": "minutes", "negated": "seconds"} in got
    assert {"asserted": "line", "negated": "per station"} in got
    assert any(d["negated"] == "abbreviate the line identifier"
               and d["asserted"] == "" for d in got)


def test_descriptive_context_yields_no_directives():
    """Honest absence: a context version with no correction cannot be judged."""
    plain = ("This system records production orders for the assembly area. "
             "Values are refreshed hourly by the interface job.")
    assert fs.context_directives(plain) == []


@pytest.mark.parametrize("bad", [None, "", "   ", 17, {"a": 1}])
def test_directives_reject_unusable_input(bad):
    assert fs.context_directives(bad) == []


def test_directives_are_bounded_and_deduped():
    text = " ".join(f"Metric {i} is in unit-a{i}, not unit-b{i}." for i in range(30))
    got = fs.context_directives(text)
    assert len(got) == fs.MAX_DIRECTIVES
    repeated = "Values are in minutes, not seconds. Totals are in minutes, not seconds."
    assert len(fs.context_directives(repeated)) == 1


def test_directive_terms_are_length_bounded():
    long_word = "z" * 200
    got = fs.context_directives(f"Values are in minutes, not {long_word}")
    assert got and len(got[0]["negated"]) <= fs.MAX_DIRECTIVE_TERM_LEN


def test_directive_parsing_is_deterministic():
    assert fs.context_directives(CONTEXT_TEXT) == fs.context_directives(CONTEXT_TEXT)


# ── contradiction detection (pure) ──────────────────────────────────────────


def test_contradiction_needs_ruled_out_term_and_missing_assertion():
    directives = fs.context_directives(CONTEXT_TEXT)
    hits = fs.contradicted_directives(directives, "Cycle time averaged 45 seconds.")
    assert [h["negated"] for h in hits] == ["seconds"]


def test_answer_echoing_the_correction_is_not_a_contradiction():
    directives = fs.context_directives(CONTEXT_TEXT)
    answer = "Cycle time is 3 minutes, which is 180 seconds."
    assert fs.contradicted_directives(directives, answer) == []


def test_compliant_answer_yields_nothing():
    directives = fs.context_directives(CONTEXT_TEXT)
    assert fs.contradicted_directives(directives, "Cycle time was 3 minutes.") == []


def test_term_match_is_word_bounded_never_a_bare_substring():
    directives = [{"asserted": "", "negated": "unit"}]
    assert fs.contradicted_directives(directives, "reported in units") == []
    assert fs.contradicted_directives(directives, "reported in unit") != []


@pytest.mark.parametrize("answer", [None, "", "   ", 5])
def test_no_contradiction_without_an_answer(answer):
    assert fs.contradicted_directives(
        fs.context_directives(CONTEXT_TEXT), answer) == []


# ── accumulation: evidence-bearing only ─────────────────────────────────────


_DEFAULT = object()


def _record(_table=None, *, system_id=SYSTEM, context=_DEFAULT,
            answer_text="Cycle time was 45 seconds.",
            answer_ref="session-1:turn-1", turn_id="turn-1"):
    """One detector invocation with the happy-path defaults."""
    return fs.record_context_not_reflected(
        system_id, _context() if context is _DEFAULT else context,
        answer_text, answer_ref=answer_ref, turn_id=turn_id)


def test_signal_accumulates_naming_the_edit_and_the_answer(table):
    recorded = _record(table)
    assert recorded == [{"context_hash": "abc123def456", "term": "seconds",
                         "asserted": "minutes"}]
    key = fs.shape_key(fs.CONTEXT_NOT_REFLECTED, SYSTEM, recorded[0])
    row = table.rows[key]
    assert row["shape"] == fs.CONTEXT_NOT_REFLECTED
    assert row["sig_count"] == 1
    assert row["evidence_refs"] == ["session-1:turn-1"]
    assert "abc123def456" in row["subject_json"]


def test_a_new_edit_starts_a_fresh_signal_row(table):
    _record(table)
    _record(table, context=_context(digest="99988877766"), turn_id="turn-2")
    assert len(table.rows) == 2


@pytest.mark.parametrize("absent", [
    {"context": None},                             # unreadable graph
    {"context": _context(enabled=False)},          # not delivered to the agent
    {"context": _context(saved=False)},            # never saved
    {"context": _context(digest="")},              # edit cannot be named
    {"context": _context(text="Orders are refreshed hourly.")},  # no directive
    {"answer_text": "Cycle time was 3 minutes."},  # nothing contradicted
    {"answer_ref": ""},                            # answer cannot be named
    {"turn_id": ""},                               # observation unattributable
])
def test_no_signal_without_every_piece_of_evidence(table, absent):
    assert _record(table, **absent) == []
    assert table.rows == {}


def test_unknown_system_never_records(table):
    assert fs.record_context_not_reflected(
        "", _context(), "45 seconds", answer_ref="r", turn_id="t") == []
    assert table.rows == {}


def test_recording_never_raises_when_the_store_is_down(monkeypatch):
    monkeypatch.setattr(sl, "_get_table", lambda: RaisingTable())
    assert _record(None) == []


def test_recording_never_raises_on_a_hostile_context(table):
    class Hostile(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("hostile")
    assert fs.record_context_not_reflected(
        SYSTEM, Hostile(), "45 seconds", answer_ref="r", turn_id="t") == []


# ── threshold / dedup parity with the module's other shapes ─────────────────


def test_threshold_is_two_distinct_turns(table):
    assert fs.THRESHOLDS[fs.CONTEXT_NOT_REFLECTED] == 2
    _record(table, turn_id="turn-1", answer_ref="a-1")
    assert fs.signals_over_threshold() == []
    _record(table, turn_id="turn-2", answer_ref="a-2")
    over = fs.signals_over_threshold()
    assert len(over) == 1 and over[0]["shape"] == fs.CONTEXT_NOT_REFLECTED


def test_same_turn_never_double_counts(table):
    assert _record(table, turn_id="turn-1") != []
    for _ in range(4):
        assert _record(table, turn_id="turn-1") == []
    row = next(iter(table.rows.values()))
    assert row["sig_count"] == 1 and row["evidence_refs"] == ["session-1:turn-1"]


def test_ttl_attribute_is_stamped(table):
    _record(table)
    assert int(next(iter(table.rows.values()))["ttl_epoch"]) > 0


# ── bounded evidence storage ────────────────────────────────────────────────


def test_evidence_storage_is_bounded_while_counting_continues(table):
    for i in range(fs.MAX_EVIDENCE_REFS + 4):
        _record(table, turn_id=f"turn-{i}", answer_ref=f"answer-{i}")
    row = next(iter(table.rows.values()))
    assert len(row["evidence_refs"]) == fs.MAX_EVIDENCE_REFS
    assert row["sig_count"] == fs.MAX_EVIDENCE_REFS + 4
    assert row["evidence_refs"][0] == "answer-0"  # first-seen refs are kept


def test_one_evidence_reference_is_length_bounded(table):
    _record(table, answer_ref="r" * 500)
    row = next(iter(table.rows.values()))
    assert len(row["evidence_refs"][0]) == fs.MAX_EVIDENCE_REF_LEN


def test_sibling_shapes_keep_the_single_bare_write(table):
    """No evidence_ref ⇒ byte-identical behavior to the ADR-0059 shapes."""
    assert fs.record_failure_signal(
        "terminology_miss", SYSTEM, {"term": "a-term"}, "turn-1") is True
    assert len(table.updates) == 1
    update, condition = table.updates[0]
    assert "list_append" not in update and "evidence_refs" not in condition
    assert fs.record_failure_signal(
        "terminology_miss", SYSTEM, {"term": "a-term"}, "turn-1") is False


def test_unknown_shape_is_refused(table):
    assert fs.record_failure_signal("not_a_shape", SYSTEM, {}, "t") is False
    assert table.updates == []


# ── the filed question names the evidence ───────────────────────────────────


def test_sweep_files_a_steward_question_naming_edit_and_answers(table, monkeypatch):
    filed: list = []
    monkeypatch.setattr(sl, "log_proposed_mapping",
                        lambda **kw: filed.append(kw) or "sq-1")
    monkeypatch.setattr(sl, "get_all_mappings", lambda: [])
    _record(table, turn_id="turn-1", answer_ref="answer-1")
    _record(table, turn_id="turn-2", answer_ref="answer-2")
    result = fs.sweep_and_file_questions()
    assert result["filed"] == ["sq-1"] and result["error"] == ""
    kw = filed[0]
    assert kw["proposed_type"] == "steward_question"
    assert kw["reason"] == fs.CONTEXT_NOT_REFLECTED
    assert kw["from_system"] == SYSTEM
    assert kw["from_value"] == "minutes"
    assert kw["to_value"] == "seconds"
    assert "context_hash=abc123def456" in kw["provenance"]
    assert "contradicting_answers=answer-1,answer-2" in kw["provenance"]
    assert "seconds" in kw["description"] and "MUST follow" in kw["description"]


def test_filed_question_is_never_auto_accepted():
    assert "steward_question" in sl._NEVER_AUTO_ACCEPT_TYPES


def test_question_composition_is_deterministic_and_bank_preferring():
    facts = {"term": "seconds", "asserted": "minutes", "system": SYSTEM}
    first = fs._compose_question(fs.CONTEXT_NOT_REFLECTED, facts)
    assert first is not None and first.id == fs._compose_question(
        fs.CONTEXT_NOT_REFLECTED, dict(facts)).id
    assert first.landing == "query_guidance"
    assert fs._compose_question("not_a_shape", facts) is None


def test_question_survives_missing_subject_facts():
    q = fs._compose_question(fs.CONTEXT_NOT_REFLECTED, {})
    assert q is not None and "that wording" in q.prompt


# ── requirement phrasing (the one source of truth) ──────────────────────────


def test_requirement_phrasing_both_forms():
    assert fs.requirement_phrasing("minutes", "seconds") == (
        "REQUIRED: the answer must say 'minutes', not 'seconds'.")
    assert fs.requirement_phrasing("", "seconds") == (
        "REQUIRED: the answer must not say 'seconds'.")


@pytest.mark.parametrize("negated", [None, "", "   ", 42, {"a": 1}])
def test_requirement_phrasing_absent_without_a_ruled_out_term(negated):
    assert fs.requirement_phrasing("minutes", negated) == ""


def test_requirement_phrasing_bounds_untrusted_terms():
    note = fs.requirement_phrasing("a" * 400, "b" * 400)
    assert len(note) < 2 * fs.MAX_DIRECTIVE_TERM_LEN + 80
    assert "\n" not in fs.requirement_phrasing("x\ny", "p\tq")


# ── the promote-to-answer-requirement route ─────────────────────────────────


class _AdminInject(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.is_admin = True
        return await call_next(request)


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(vr.router)
    app.add_middleware(_AdminInject)
    return TestClient(app)


@pytest.fixture()
def known_system(monkeypatch):
    import agents.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod.catalog, "get_all", lambda: [{"id": SYSTEM}])


PROMOTE = f"/api/admin/vocabulary/guidance/{SYSTEM}/promote-requirement"


def test_promote_files_a_guidance_note_through_the_propose_path(
        client, known_system, monkeypatch):
    calls: list = []
    monkeypatch.setattr(sl, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "gn-1")
    resp = client.post(PROMOTE, json={"asserted": "minutes", "negated": "seconds"})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "pending", "mapping_id": "gn-1",
        "requirement": "REQUIRED: the answer must say 'minutes', not 'seconds'."}
    assert len(calls) == 1
    kw = calls[0]
    assert kw["proposed_type"] == "guidance_note"
    assert kw["source"] == "user"
    assert kw["provenance"] == "human-asserted"
    assert kw["from_system"] == SYSTEM
    assert kw["to_value"] == resp.json()["requirement"]


def test_promoted_note_type_is_in_the_never_auto_accept_deny_tuple():
    assert "guidance_note" in sl._NEVER_AUTO_ACCEPT_TYPES


def test_promote_without_a_ruled_out_term_is_400(client, monkeypatch):
    calls: list = []
    monkeypatch.setattr(sl, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "gn-x")
    resp = client.post(PROMOTE, json={"asserted": "minutes"})
    assert resp.status_code == 400
    assert calls == []


def test_promote_unknown_system_is_404(client, monkeypatch):
    import agents.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod.catalog, "get_all",
                        lambda: [{"id": "sys-b"}, {"id": "sys-c"}])
    calls: list = []
    monkeypatch.setattr(sl, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "gn-x")
    resp = client.post(PROMOTE, json={"negated": "seconds"})
    assert resp.status_code == 404
    assert calls == []


def test_promote_reports_an_ingestion_drop_honestly(client, known_system,
                                                    monkeypatch):
    monkeypatch.setattr(sl, "log_proposed_mapping", lambda **kw: None)
    resp = client.post(PROMOTE, json={"negated": "seconds"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_promote_bounds_hostile_body_terms(client, known_system, monkeypatch):
    calls: list = []
    monkeypatch.setattr(sl, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "gn-2")
    resp = client.post(PROMOTE, json={"asserted": "a" * 400,
                                      "negated": "b" * 400})
    assert resp.status_code == 200
    assert len(calls[0]["to_value"]) < 2 * fs.MAX_DIRECTIVE_TERM_LEN + 80


def test_promote_reuses_the_scoped_filing_helper_rather_than_duplicating_it():
    """One filing path: promotion selects scope but never reimplements storage."""
    import inspect
    source = inspect.getsource(vr.promote_answer_requirement)
    assert "_file_guidance_proposal" in source
    assert "log_proposed_mapping" not in source


def test_promote_explicitly_files_answer_requirement_scope(
        client, known_system, monkeypatch):
    calls: list = []
    monkeypatch.setattr(sl, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "gn-scoped")

    resp = client.post(PROMOTE, json={"negated": "an unsupported claim"})

    assert resp.status_code == 200
    assert calls[0]["guidance_scope"] == "answer_requirement"
