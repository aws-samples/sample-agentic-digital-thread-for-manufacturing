# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Minimized findings store (agents/eval_findings.py) — pinned invariants
(ADR-0062; pins 6, 7, 8, 9, 10, 18, 20, 21).

Offline — no AWS, no network. The three scopes are in-memory
``JobStore(..., table=None)`` instances (the ``test_eval_runs.py`` idiom)
wrapped in a SPY PERSISTENCE SEAM that records every write and delete the
module attempts, so every claim below is proven at the storage boundary rather
than at the return value.

Pins:

* the DURABLE row is minimized — a RECURSIVE leaf allowlist over everything
  the durable store received PLUS a value-level sentinel scan proving no
  excerpt / rationale / mapping value ever lands there, whatever key it hides
  under (pin 6); a non-allowlisted leaf RAISES before any write;
* generated detail is CUSTOMER-DERIVED under a 7 d TTL, labelled as such, and
  its expiry is a normal ``detail_expired`` state (pin 7);
* LLM output is capped + secret-shape rejected before persistence; a rejected
  value is dropped, counted, and absent from every log record emitted during
  the call (pin 8);
* replacement DELETES stale generated rows in scope — 5 imported, 3
  re-imported leaves exactly 3, and the steward annotation survives
  byte-identical (pins 9, 18);
* ``usage`` is the exact measured schema or ABSENT (never zero), and nothing
  cost/dollar-shaped is persisted or returned (pin 10);
* the findings-import writer fires the REAL ``bump_artifact_usage`` ledger with
  ``("eval-finding", <finding_id>)`` refs (pin 20);
* every read path (ledger, digest, expand) performs ZERO writes (pin 21);
* the import BINDS its source: sha match + referential integrity, and the
  uploaded report is NEVER persisted;
* the OPTIONAL transient ``evidence_docs`` bundle only WIDENS referential
  integrity (report ids UNION evidence ids) — an evidence-only ref is kept, a
  ref resolving in neither half is still discarded and counted, and a sentinel
  planted only in the bundle reaches no write and no log record;
* the module is import-pure (no boto3 at import).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import subprocess  # nosec B404 - fixed argv, no shell (fresh-interpreter pin)
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_findings as ef  # noqa: E402
from agents import job_store as js  # noqa: E402
from agents.audit_core import (build_findings_doc,  # noqa: E402
                               report_sha_bytes)
from agents.job_store import JobStore  # noqa: E402

ADT_ROOT = Path(__file__).resolve().parents[2]

# Distinctive customer-derived sentinels. If ANY of these substrings reaches
# the DURABLE store the minimization pin fails, whatever key it hides under.
EXCERPT_SENTINEL = "ZCLAIMEXCERPTSENTINEL4411"
RATIONALE_SENTINEL = "ZRATIONALESENTINEL4411"
MAPPING_SENTINEL = "ZMAPPINGVALUESENTINEL4411"
REPORT_ONLY_SENTINEL = "ZREPORTONLYSENTINEL4411"
# Planted ONLY in the transient evidence bundle: it must reach no store and no
# log record (the bundle is customer-derived — traces, intelligence summaries).
EVIDENCE_ONLY_SENTINEL = "ZEVIDENCEONLYSENTINEL4411"


# ── harness ─────────────────────────────────────────────────────────────────

class _SpyStore:
    """Spy persistence seam around a real in-memory ``JobStore``."""

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
        existed = key in self._inner
        value = self._inner.pop(key, *args)
        if existed:
            self.deletes.append(key)
        return value

    @property
    def written_payloads(self) -> list:
        return [payload for _, payload in self.writes]

    def reset_spy(self) -> None:
        self.writes = []
        self.deletes = []


@pytest.fixture()
def stores(monkeypatch):
    """Fresh spied stores for all three scopes."""
    findings = _SpyStore("eval-finding")
    details = _SpyStore("eval-finding-detail")
    steward = _SpyStore("eval-finding-steward")
    monkeypatch.setattr(ef, "_FINDINGS", findings)
    monkeypatch.setattr(ef, "_DETAILS", details)
    monkeypatch.setattr(ef, "_STEWARD", steward)
    return {"findings": findings, "details": details, "steward": steward}


@pytest.fixture(autouse=True)
def no_real_telemetry(monkeypatch):
    """The usage ledger is a spy by default (its real path needs DynamoDB)."""
    calls: list = []
    import agents.artifact_usage as au
    monkeypatch.setattr(au, "bump_artifact_usage",
                        lambda refs, evidence="": calls.append(
                            (sorted(refs), evidence)) or len(refs))
    return calls


def _report(run_id="run1234", question_ids=("g-one",), extra=None):
    report = {
        "run_id": run_id,
        "run_path": "server",
        "questions": [{"id": qid, "session_id": f"eval-run-{run_id}-q1",
                       "verdict": "fail"} for qid in question_ids],
    }
    if extra:
        report.update(extra)
    return report


def _finding(local_id="finding-001", taxonomy="mapping_gap",
             question_id="g-one", *, mapping=True, excerpt=None,
             rationale=None):
    item = {
        "id": local_id,
        "taxonomy": taxonomy,
        "question_id": question_id,
        "claim_excerpt": excerpt if excerpt is not None
        else f"claimed {EXCERPT_SENTINEL} units",
        "evidence_refs": [question_id],
        "rationale": rationale if rationale is not None
        else f"because {RATIONALE_SENTINEL}",
    }
    if mapping:
        item["suggested_mapping"] = {"from_system": "erp",
                                     "from_field": MAPPING_SENTINEL,
                                     "to_system": "mes"}
    return item


def _doc(findings, *, report=None, usage=None, run_path="server"):
    """``(findings_doc, report bytes)`` — the two halves the import BINDS."""
    report = report if report is not None else _report()
    raw = json.dumps(report).encode("utf-8")
    doc = build_findings_doc(source_report_sha=report_sha_bytes(raw),
                             run_path=run_path, findings=findings,
                             discarded_invalid=0, usage=usage)
    return doc, raw


def _import(findings, **kwargs):
    doc, raw = _doc(findings, **kwargs)
    summary, problem = ef.import_findings(doc, raw)
    assert problem is None, problem
    return summary


def _all_writes(stores) -> list:
    out = []
    for name in ("findings", "details", "steward"):
        out.extend(stores[name].writes)
    return out


def _reset_spies(stores) -> None:
    """Forget the import's own writes so a later assertion only sees the READ
    path under test."""
    for name in ("findings", "details", "steward"):
        stores[name].reset_spy()


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


# ── minimization: recursive allowlist + sentinel spy seam (pin 6) ───────────

def test_allowlist_constants_are_pinned():
    """Deliberate-edit gate: growing a persisted row means editing THESE
    literal sets (and the ADR), not just the module."""
    assert ef.PERSISTED_FINDING_KEYS == frozenset({
        "finding_id", "taxonomy", "source_report_sha", "source_ref_sha",
        "question_ref", "run_path", "status", "created_at",
        "usage", "input_tokens", "output_tokens"})
    assert ef.PERSISTED_INDEX_KEYS == frozenset({
        "findings", "finding_id", "taxonomy", "source_report_sha",
        "question_ref", "created_at"})
    assert ef.PERSISTED_STEWARD_KEYS == frozenset({
        "finding_id", "status", "steward_note", "annotated_at",
        "annotated_by"})
    # The durable allowlist cannot express generated detail at all.
    for leaked in ("claim_excerpt", "rationale", "suggested_mapping",
                   "question_id", "answer", "text"):
        assert leaked not in ef.PERSISTED_FINDING_KEYS
        assert leaked not in ef.PERSISTED_INDEX_KEYS


def test_every_durable_leaf_key_is_allowlisted(stores):
    """RECURSIVE allowlist over EVERYTHING the durable store received."""
    _import([_finding(), _finding("finding-002", "ux_gap")],
            usage={"input_tokens": 11, "output_tokens": 7})
    allowed = ef.PERSISTED_FINDING_KEYS | ef.PERSISTED_INDEX_KEYS
    assert stores["findings"].writes, "expected persisted durable rows"
    for key, payload in stores["findings"].writes:
        assert key == "index" or re.match(r"^f-[0-9a-f]{16}$", key)
        offenders = _leaf_keys(payload) - allowed
        assert not offenders, f"non-allowlisted durable keys: {offenders}"


def test_no_customer_derived_value_reaches_the_durable_scope(stores):
    """Value-level pin: the excerpt/rationale/mapping sentinels appear ONLY in
    the 7 d customer-derived scope — never in the durable rows or the index."""
    _import([_finding()])
    durable = _blob(stores["findings"].writes)
    for sentinel in (EXCERPT_SENTINEL, RATIONALE_SENTINEL, MAPPING_SENTINEL):
        assert sentinel not in durable
    detail = _blob(stores["details"].writes)
    for sentinel in (EXCERPT_SENTINEL, RATIONALE_SENTINEL, MAPPING_SENTINEL):
        assert sentinel in detail


def test_question_and_source_refs_are_opaque(stores):
    """Refs are sha-16 digests: the question id itself is not persisted."""
    _import([_finding(question_id="g-one")])
    sha = _doc([])[0]["source_report_sha"]
    row = ef.get_finding(ef.finding_id_for(sha, "finding-001"))
    assert re.match(r"^[0-9a-f]{16}$", row["question_ref"])
    assert re.match(r"^[0-9a-f]{16}$", row["source_ref_sha"])
    assert "g-one" not in _blob(stores["findings"].writes)
    assert row["question_ref"] == ef.question_ref("g-one")


def test_a_future_non_allowlisted_field_is_refused_at_write_time(stores,
                                                                monkeypatch):
    """The module's own guard: a row that grows a field without the allowlist
    growing with it RAISES rather than persisting."""
    monkeypatch.setattr(ef, "PERSISTED_FINDING_KEYS",
                        frozenset(ef.PERSISTED_FINDING_KEYS) - {"taxonomy"})
    doc, raw = _doc([_finding()])
    with pytest.raises(ValueError) as excinfo:
        ef.import_findings(doc, raw)
    assert "taxonomy" in str(excinfo.value)
    assert _all_writes(stores) == []


def test_a_non_allowlisted_detail_leaf_is_refused_before_any_write(stores,
                                                                  monkeypatch):
    monkeypatch.setattr(ef, "PERSISTED_DETAIL_KEYS",
                        frozenset(ef.PERSISTED_DETAIL_KEYS) - {"rationale"})
    doc, raw = _doc([_finding()])
    with pytest.raises(ValueError):
        ef.import_findings(doc, raw)
    assert _all_writes(stores) == []


# ── customer-derived detail: 7 d TTL + expiry state (pin 7) ─────────────────

def test_detail_rows_are_labelled_customer_derived(stores):
    _import([_finding()])
    assert stores["details"].writes, "expected a detail row"
    for _, payload in stores["details"].writes:
        assert payload["data_class"] == ef.CUSTOMER_DERIVED_LABEL
        offenders = _leaf_keys(payload) - ef.PERSISTED_DETAIL_KEYS
        assert not offenders, f"non-allowlisted detail keys: {offenders}"


def test_ttls_are_seven_days_for_detail_and_ninety_for_the_durable_row():
    assert ef.DETAIL_TTL_SECONDS == 7 * 24 * 3600
    assert ef.FINDING_TTL_SECONDS == 90 * 24 * 3600
    assert ef.STEWARD_TTL_SECONDS == 180 * 24 * 3600


class _RecordingTable:
    """Minimal DynamoDB Table double capturing the TTL attribute per item."""

    def __init__(self):
        self.items: dict = {}
        self.puts: list = []

    def get_item(self, Key):  # noqa: N803 - boto3 kwarg name
        item = self.items.get(Key["system_id"])
        return {"Item": item} if item else {}

    def put_item(self, Item):  # noqa: N803 - boto3 kwarg name
        self.items[Item["system_id"]] = Item
        self.puts.append(dict(Item))

    def delete_item(self, Key):  # noqa: N803 - boto3 kwarg name
        self.items.pop(Key["system_id"], None)


def test_each_scope_is_constructed_with_its_own_ttl(monkeypatch):
    """The wiring pin AND the attribute pin: the detail item carries a ttl 7 d
    out, the durable item 90 d, the steward item 180 d."""
    table = _RecordingTable()
    created: list = []
    real = js.JobStore

    def _factory(kind, *, ttl_seconds=3600, **_kwargs):
        created.append((kind, ttl_seconds))
        return real(kind, table=table, ttl_seconds=ttl_seconds)

    monkeypatch.setattr(js, "JobStore", _factory)
    ef._reset_state()
    try:
        summary = _import([_finding()])
        finding_id = ef.list_findings()["groups"]["mapping_gap"][0]["finding_id"]
        ef.annotate(finding_id, status="annotated", annotated_by="steward-a")
    finally:
        ef._reset_state()
    assert dict(created) == {"eval-finding": ef.FINDING_TTL_SECONDS,
                             "eval-finding-detail": ef.DETAIL_TTL_SECONDS,
                             "eval-finding-steward": ef.STEWARD_TTL_SECONDS}
    assert summary["written"] == 1
    now = int(time.time())
    horizons = {}
    for item in table.puts:
        kind = item["system_id"].split(":")[1]
        horizons[kind] = item["ttl"] - now
    assert abs(horizons["eval-finding-detail"] - ef.DETAIL_TTL_SECONDS) < 120
    assert abs(horizons["eval-finding"] - ef.FINDING_TTL_SECONDS) < 120
    assert abs(horizons["eval-finding-steward"] - ef.STEWARD_TTL_SECONDS) < 120


def test_an_expired_detail_row_reads_as_detail_expired(stores):
    """7 d expiry is a NORMAL state — never an error, never a missing finding.
    (The ``file-proposal`` 409 that hangs off it is the route's pin.)"""
    _import([_finding()])
    finding_id = ef.list_findings()["groups"]["mapping_gap"][0]["finding_id"]
    assert ef.get_finding(finding_id)["detail_expired"] is False

    del stores["details"][finding_id]          # the 7 d row aged out
    _reset_spies(stores)
    row = ef.get_finding(finding_id)
    assert row["detail_expired"] is True and row["detail"] is None
    assert row["taxonomy"] == "mapping_gap"    # the durable row still reads
    assert _all_writes(stores) == []


def test_expanding_a_finding_is_the_only_place_detail_resolves(stores):
    _import([_finding()])
    _reset_spies(stores)
    ledger = ef.list_findings()
    digest = ef.weekly_digest()
    for payload in (ledger, digest):
        assert EXCERPT_SENTINEL not in _blob(payload)
        assert RATIONALE_SENTINEL not in _blob(payload)
    finding_id = ledger["groups"]["mapping_gap"][0]["finding_id"]
    expanded = ef.get_finding(finding_id)
    assert EXCERPT_SENTINEL in expanded["detail"]["claim_excerpt"]
    assert expanded["detail"]["data_class"] == ef.CUSTOMER_DERIVED_LABEL
    assert _all_writes(stores) == []


# ── untrusted LLM output (pin 8) ────────────────────────────────────────────

@pytest.mark.parametrize("value,reason", [
    ("AKIAIOSFODNN7EXAMPLE", "secret_like_content"),
    ("api_key: sk-not-a-real-secret", "secret_like_content"),
    ("bearer abcdef0123456789abcd", "secret_like_content"),
    ("<<<BUSINESS_CONTEXT smuggled", "fence_delimiter"),
])
def test_secret_shaped_or_smuggling_llm_fields_are_dropped_and_counted(
        stores, caplog, value, reason):
    """Dropped + counted + NEVER echoed — not in the summary, not in a log."""
    caplog.set_level(logging.DEBUG)
    summary = _import([_finding(rationale=value)])
    assert summary["rejected_fields"] == {reason: 1}
    assert summary["written"] == 1                 # the finding still lands
    persisted = _blob(_all_writes(stores))
    logged = " ".join(record.getMessage() for record in caplog.records)
    for haystack in (persisted, _blob(summary), logged):
        assert value not in haystack
    detail = stores["details"].written_payloads[0]
    assert "rationale" not in detail
    assert EXCERPT_SENTINEL in detail["claim_excerpt"]   # siblings survive


@pytest.mark.parametrize("value", [42, {"nested": "object"}, ["a"], 1.5, True])
def test_non_string_llm_fields_are_rejected_by_type(value):
    """The TYPE cap matters for a direct caller (the audit worker): a bundle
    that never went through ``audit_core``'s string coercion must not be able
    to smuggle a structure into the detail row."""
    detail, rejected = ef.sanitize_llm_output({"claim_excerpt": value,
                                               "rationale": value})
    assert detail == {}
    assert rejected == {"claim_excerpt": "invalid_input",
                        "rationale": "invalid_input"}


def test_a_rejected_mapping_value_drops_only_that_key(stores):
    finding = _finding()
    finding["suggested_mapping"] = {"from_system": "erp",
                                    "from_field": "AKIAIOSFODNN7EXAMPLE"}
    summary = _import([finding])
    assert summary["rejected_fields"] == {"secret_like_content": 1}
    mapping = stores["details"].written_payloads[0]["suggested_mapping"]
    assert mapping == {"from_system": "erp"}


def test_llm_strings_are_capped_not_persisted_whole(stores):
    _import([_finding(excerpt="E" * 5000, rationale="R" * 5000)])
    detail = stores["details"].written_payloads[0]
    assert len(detail["claim_excerpt"]) == ef.CLAIM_EXCERPT_CHARS == 300
    assert len(detail["rationale"]) == ef.RATIONALE_MAX_CHARS


def test_sanitize_llm_output_never_returns_a_rejected_value():
    detail, rejected = ef.sanitize_llm_output({
        "claim_excerpt": "AKIAIOSFODNN7EXAMPLE",
        "rationale": "  a plain\r\nrationale\x07  ",
        "suggested_mapping": {"from_field": "x" * 900, "bogus": "y"}})
    assert rejected == {"claim_excerpt": "secret_like_content",
                        "suggested_mapping": "unknown_keys"}
    assert detail["rationale"] == "a plain\nrationale"
    assert len(detail["suggested_mapping"]["from_field"]) == \
        ef.MAPPING_VALUE_MAX_CHARS
    assert "AKIA" not in _blob((detail, rejected))
    assert ef.sanitize_llm_output("not a dict") == ({}, {"finding":
                                                         "invalid_input"})


def test_a_finding_with_no_usable_detail_still_persists_its_durable_row(
        stores):
    summary = _import([_finding(excerpt="", rationale="", mapping=False)])
    assert summary["written"] == 1
    assert stores["details"].writes == []
    assert ef.get_finding(
        ef.list_findings()["groups"]["mapping_gap"][0]["finding_id"]
    )["detail_expired"] is True


# ── replacement DELETES (pins 9, 18) ───────────────────────────────────────

def _five_findings():
    return [_finding(f"finding-{n:03d}", "ux_gap" if n % 2 else "mapping_gap")
            for n in range(1, 6)]


def test_reimport_deletes_stale_generated_rows_in_scope(stores):
    """5 imported, 3 re-imported ⇒ exactly 3 durable rows remain and the two
    dropped ids are GONE from both generated scopes (deletion, not dedupe)."""
    first = _import(_five_findings())
    assert first["written"] == 5 and first["deleted"] == 0
    sha = _doc([])[0]["source_report_sha"]
    ids = [ef.finding_id_for(sha, f"finding-{n:03d}") for n in range(1, 6)]
    stores["findings"].reset_spy()
    stores["details"].reset_spy()

    second = _import(_five_findings()[:3])
    assert second["written"] == 3
    assert second["deleted"] == 5                      # the whole prior scope
    assert set(stores["findings"].deletes) == set(ids)
    assert set(stores["details"].deletes) == set(ids)
    assert ef.list_findings()["total"] == 3
    for gone in ids[3:]:
        assert ef.get_finding(gone) is None
        assert stores["details"].get(gone) is None
    for kept in ids[:3]:
        assert ef.get_finding(kept) is not None


def test_finding_ids_are_deterministic_across_reimport(stores):
    _import(_five_findings())
    before = sorted(entry["finding_id"]
                    for entry in ef.list_findings()["groups"]["ux_gap"])
    _import(_five_findings())
    after = sorted(entry["finding_id"]
                   for entry in ef.list_findings()["groups"]["ux_gap"])
    assert before == after


def test_a_steward_annotation_survives_reimport_byte_identical(stores):
    _import(_five_findings())
    sha = _doc([])[0]["source_report_sha"]
    finding_id = ef.finding_id_for(sha, "finding-001")
    row, problem = ef.annotate(finding_id, status="filed",
                               note="filed as a mapping proposal",
                               annotated_by="steward-a")
    assert problem is None
    stored = _blob(ef.get_annotation(finding_id))
    stores["steward"].reset_spy()

    _import(_five_findings()[:3])
    assert stores["steward"].writes == [] and stores["steward"].deletes == []
    assert _blob(ef.get_annotation(finding_id)) == stored
    assert _blob(row) == stored
    # ...and the steward status wins over the regenerated "open".
    assert ef._findings().get(finding_id)["status"] == ef.GENERATED_STATUS
    assert ef.get_finding(finding_id)["status"] == "filed"


def test_another_scope_is_untouched_by_a_replacement(stores):
    other_report = _report(run_id="run5678", question_ids=("g-two",))
    _import([_finding(question_id="g-two")], report=other_report)
    _import(_five_findings())
    assert ef.list_findings()["total"] == 6
    stores["findings"].reset_spy()
    _import(_five_findings()[:1])
    assert ef.list_findings()["total"] == 2       # 1 in scope + the other one
    other_sha = _doc([], report=other_report)[0]["source_report_sha"]
    assert ef.get_finding(ef.finding_id_for(other_sha, "finding-001")) \
        is not None


def test_the_index_is_bounded_and_replace_not_accrete(stores, monkeypatch):
    monkeypatch.setattr(ef, "MAX_INDEX_FINDINGS", 3)
    _import(_five_findings())
    index_writes = [payload for key, payload in stores["findings"].writes
                    if key == "index"]
    assert len(index_writes) == 1
    assert len(index_writes[0]["findings"]) == 3


# ── the machine never writes a steward verdict (pin 18) ────────────────────

def test_generated_rows_are_always_open_and_rejected_is_not_a_status(stores):
    _import(_five_findings())
    assert "rejected" not in ef.STATUSES
    for key, payload in stores["findings"].writes:
        if key == "index":
            continue
        assert payload["status"] == ef.GENERATED_STATUS == "open"
    source = (ADT_ROOT / "agents" / "eval_findings.py").read_text()
    assert '"rejected"' not in source
    # ``annotate`` is the ONLY writer into the steward scope.
    assert source.count("_steward()[") == 1


@pytest.mark.parametrize("status", ["rejected", "REJECTED", "", 1, "closed"])
def test_unknown_annotation_statuses_are_refused_without_a_write(stores,
                                                                status):
    _import([_finding()])
    finding_id = ef.list_findings()["groups"]["mapping_gap"][0]["finding_id"]
    stores["steward"].reset_spy()
    row, problem = ef.annotate(finding_id, status=status)
    assert row is None and problem[0] == "invalid_status"
    assert stores["steward"].writes == []


def test_annotating_an_unknown_finding_is_refused(stores):
    for bogus in (None, 42, "", "f-nope", "steward-f-0123456789abcdef"):
        row, problem = ef.annotate(bogus, status="dismissed")
        assert row is None and problem[0] == "unknown_finding"
    assert _all_writes(stores) == []


@pytest.mark.parametrize("note,reason", [
    ("AKIAIOSFODNN7EXAMPLE", "secret_like_content"),
    ("password: hunter2-not-a-real-secret", "secret_like_content"),
    ("BUSINESS_CONTEXT>>> smuggled", "fence_delimiter"),
    ("x" * (ef.NOTE_MAX_CHARS + 1), "too_long"),
    (7, "invalid_input"),
])
def test_steward_notes_are_untrusted_text_and_never_echoed(stores, note,
                                                           reason):
    _import([_finding()])
    finding_id = ef.list_findings()["groups"]["mapping_gap"][0]["finding_id"]
    stores["steward"].reset_spy()
    row, problem = ef.annotate(finding_id, note=note)
    assert row is None and problem[0] == reason
    assert str(note) not in problem[1]
    assert stores["steward"].writes == []


def test_annotated_by_must_be_id_shaped(stores):
    _import([_finding()])
    finding_id = ef.list_findings()["groups"]["mapping_gap"][0]["finding_id"]
    row, problem = ef.annotate(finding_id, annotated_by="steward one <hi>")
    assert row is None and problem[0] == "invalid_annotated_by"
    assert stores["steward"].writes == []


# ── bound import: sha + referential integrity, report never persisted ───────

def test_the_import_binds_findings_to_their_source_report(stores):
    doc, raw = _doc([_finding()])
    summary, problem = ef.import_findings(doc, raw)
    assert problem is None
    assert summary["source_report_sha"] == report_sha_bytes(raw)
    assert summary["discarded_invalid"] == 0


def test_a_report_that_does_not_match_the_sha_is_refused_without_a_write(
        stores):
    doc, _ = _doc([_finding()])
    other = json.dumps(_report(run_id="run9999")).encode("utf-8")
    summary, problem = ef.import_findings(doc, other)
    assert summary is None and problem[0] == "source_report_mismatch"
    assert _all_writes(stores) == []


@pytest.mark.parametrize("report_bytes", [
    None, 42, b"not json", b"[]", b'"a string"', "{".encode("utf-8"),
    b"\xff\xfe\x00",
])
def test_an_unusable_source_report_is_refused_without_a_write(stores,
                                                             report_bytes):
    doc, _ = _doc([_finding()])
    summary, problem = ef.import_findings(doc, report_bytes)
    assert summary is None and problem[0] == "invalid_source_report"
    assert _all_writes(stores) == []


@pytest.mark.parametrize("doc,reason", [
    (None, "invalid_findings_doc"),
    ({"version": 99, "findings": []}, "unsupported_version"),
    ({"version": 1, "findings": "nope"}, "invalid_findings_doc"),
    ({"version": 1, "findings": [{}] * 201}, "too_many_findings"),
])
def test_a_malformed_findings_document_is_refused_without_a_write(stores, doc,
                                                                 reason):
    summary, problem = ef.import_findings(doc, json.dumps(_report()).encode())
    assert summary is None and problem[0] == reason
    assert _all_writes(stores) == []


def test_findings_failing_referential_integrity_are_discarded_and_counted(
        stores):
    """``audit_core`` is the validator — invented evidence ids are DISCARDED,
    counted, and never a crash."""
    invented = _finding("finding-002")
    invented["evidence_refs"] = ["id-the-model-invented"]
    bad_taxonomy = _finding("finding-003", taxonomy="made_up")
    doc, raw = _doc([_finding(), invented, bad_taxonomy])
    summary, problem = ef.import_findings(doc, raw)
    assert problem is None
    assert summary["written"] == 1 and summary["discarded_invalid"] == 2
    assert summary["taxonomy_counts"] == {"mapping_gap": 1}


def test_the_uploaded_report_is_never_persisted(stores):
    """Spy seam: a sentinel that exists ONLY in the report must not appear in
    anything any store received."""
    report = _report(extra={"operator_note": REPORT_ONLY_SENTINEL})
    _import([_finding()], report=report)
    assert REPORT_ONLY_SENTINEL not in _blob(_all_writes(stores))
    source = (ADT_ROOT / "agents" / "eval_findings.py").read_text()
    assert "report_bytes" in source and "_parse_report" in source
    # No store call anywhere receives the parsed report object.
    assert "] = report" not in source


def test_replace_findings_requires_a_report_sha_scope(stores):
    for bogus in (None, "", "not-a-sha", "a" * 63, 42):
        with pytest.raises(ValueError):
            ef.replace_findings(bogus, [_finding()])
    assert _all_writes(stores) == []


# ── the OPTIONAL transient evidence bundle (report ids UNION evidence ids) ──

def _evidence_docs():
    """An ``audit_answer.py --evidence`` bundle: ids the REPORT does not carry
    (a trace step, a graph rule) plus one customer-derived leaf that must not
    survive the call."""
    return [
        {"trace_id": "trace-evidence-only-77",
         "steps": [{"step_id": "step-evidence-only-88",
                    "observation": EVIDENCE_ONLY_SENTINEL}]},
        {"rules": [{"rule_id": "rule-evidence-only-99"}]},
    ]


@pytest.mark.parametrize("ref", ["trace-evidence-only-77",
                                 "step-evidence-only-88",
                                 "rule-evidence-only-99"])
def test_evidence_only_refs_resolve_through_the_union(stores, ref):
    """A ref that exists ONLY in the evidence bundle is KEPT: the known-id set
    is report ids UNION evidence ids, walked by the same
    ``audit_core.collect_known_ids`` call the CLI uses."""
    finding = _finding()
    finding["evidence_refs"] = [ref]
    doc, raw = _doc([finding])
    summary, problem = ef.import_findings(doc, raw,
                                          evidence_docs=_evidence_docs())
    assert problem is None
    assert summary["written"] == 1 and summary["discarded_invalid"] == 0


def test_the_same_ref_is_discarded_without_the_bundle(stores):
    """The complement of the pin above — this is the limitation the optional
    input exists to fix, and it is still the behaviour when nothing is
    supplied (no implicit widening anywhere)."""
    finding = _finding()
    finding["evidence_refs"] = ["step-evidence-only-88"]
    doc, raw = _doc([finding])
    summary, problem = ef.import_findings(doc, raw)
    assert problem is None
    assert summary["written"] == 0 and summary["discarded_invalid"] == 1


def test_an_unknown_ref_is_still_discarded_when_evidence_is_supplied(stores):
    """Supplying evidence WIDENS the id set; it is not a way to make discards
    disappear. A ref resolving in NEITHER half is still discarded and still
    counted, alongside a sibling the bundle does resolve."""
    invented = _finding("finding-002")
    invented["evidence_refs"] = ["id-in-neither-half"]
    resolved = _finding("finding-003")
    resolved["evidence_refs"] = ["rule-evidence-only-99"]
    doc, raw = _doc([invented, resolved])
    summary, problem = ef.import_findings(doc, raw,
                                          evidence_docs=_evidence_docs())
    assert problem is None
    assert summary["written"] == 1 and summary["discarded_invalid"] == 1


def test_the_evidence_bundle_is_never_persisted_or_logged(stores, caplog):
    """Spy persistence seam + log capture: a sentinel planted ONLY in the
    evidence bundle must appear in NO write and in NO log record. The bundle
    is customer-derived (traces, intelligence summaries) — parsed, used for
    validation, dropped."""
    finding = _finding()
    finding["evidence_refs"] = ["step-evidence-only-88"]
    doc, raw = _doc([finding])
    with caplog.at_level(logging.DEBUG):
        summary, problem = ef.import_findings(doc, raw,
                                              evidence_docs=_evidence_docs())
    assert problem is None and summary["written"] == 1
    assert EVIDENCE_ONLY_SENTINEL not in _blob(_all_writes(stores))
    assert EVIDENCE_ONLY_SENTINEL not in _blob(stores["findings"].writes)
    assert EVIDENCE_ONLY_SENTINEL not in _blob(stores["details"].writes)
    assert EVIDENCE_ONLY_SENTINEL not in caplog.text
    assert EVIDENCE_ONLY_SENTINEL not in _blob(summary)
    # ... and the ids it carried are not persisted as evidence either: the
    # durable row keeps refs, not documents.
    assert "observation" not in _blob(_all_writes(stores))


# ── usage: measured or ABSENT (pin 10) ─────────────────────────────────────

def test_measured_usage_is_stamped_on_the_durable_rows(stores):
    _import([_finding()], usage={"input_tokens": 120, "output_tokens": 45})
    rows = [payload for key, payload in stores["findings"].writes
            if key != "index"]
    assert rows and all(row["usage"] == {"input_tokens": 120,
                                        "output_tokens": 45} for row in rows)


@pytest.mark.parametrize("usage", [
    None, {}, {"input_tokens": 1}, {"input_tokens": 1, "output_tokens": None},
    {"input_tokens": "1", "output_tokens": "2"},
    {"input_tokens": 1, "output_tokens": 2, "cost_usd": 0.01},
    {"input_tokens": True, "output_tokens": 2},
    {"input_tokens": -1, "output_tokens": 2},
])
def test_unmeasured_usage_is_absent_never_zero(stores, usage):
    _import([_finding()], usage=usage)
    rows = [payload for key, payload in stores["findings"].writes
            if key != "index"]
    assert rows and all("usage" not in row for row in rows)


def test_nothing_cost_shaped_is_persisted_or_returned(stores):
    summary = _import([_finding()],
                      usage={"input_tokens": 3, "output_tokens": 4})
    blob = (_blob(_all_writes(stores)) + _blob(summary)
            + _blob(ef.weekly_digest()) + _blob(ef.list_findings())).lower()
    for marker in ("dollar", "cost_usd", "estimate", "price", "$"):
        assert marker not in blob


# ── digest + ledger are computed ON READ (pins 13, 21) ─────────────────────

def test_digest_and_ledger_reads_perform_zero_writes(stores):
    _import(_five_findings())
    _reset_spies(stores)
    digest = ef.weekly_digest()
    ledger = ef.list_findings()
    finding_id = ledger["groups"]["ux_gap"][0]["finding_id"]
    ef.get_finding(finding_id)
    ef.get_annotation(finding_id)
    assert _all_writes(stores) == []
    for name in ("findings", "details", "steward"):
        assert stores[name].deletes == []
    assert digest["total"] == 5


def test_the_digest_counts_taxonomies_and_lists_bounded_ux_gap_refs(stores):
    _import([_finding(f"finding-{n:03d}", "ux_gap")
             for n in range(1, ef.UX_GAP_REF_LIMIT + 6)])
    digest = ef.weekly_digest()
    assert digest["taxonomy_counts"]["ux_gap"] == ef.UX_GAP_REF_LIMIT + 5
    assert digest["taxonomy_counts"]["code_bug"] == 0
    assert len(digest["ux_gap_refs"]) == ef.UX_GAP_REF_LIMIT
    assert digest["ux_gap_refs_truncated"] is True
    # Refs stay REFS — nothing is resolved on a digest read.
    for ref in digest["ux_gap_refs"]:
        assert set(ref) == {"finding_id", "question_ref"}


def test_the_digest_window_is_bounded_and_filters_by_created_at(stores):
    _import([_finding()])
    stale = ef._index_entries()
    stale[0] = {**stale[0], "created_at": "2020-01-01T00:00:00+00:00"}
    stores["findings"]["index"] = {"findings": stale}
    stores["findings"].reset_spy()
    assert ef.weekly_digest()["total"] == 0
    assert ef.weekly_digest(window_days=100_000)["window_days"] == \
        ef.MAX_DIGEST_WINDOW_DAYS
    assert ef.weekly_digest(window_days="nope")["window_days"] == \
        ef.DEFAULT_DIGEST_WINDOW_DAYS
    assert ef.weekly_digest(window_days=100_000)["total"] == 0
    assert ef.list_findings()["total"] == 1     # the ledger still lists it
    assert stores["findings"].writes == []


def test_no_digest_writer_and_no_clustering_exists():
    """80/20 cuts 3 + 4: read-time aggregation only, and no normalized-token
    clustering. Scoped to IDENTIFIERS so the prose may still explain it."""
    source = (ADT_ROOT / "agents" / "eval_findings.py").read_text()
    assert not re.search(r"\bdef\s+\w*cluster\w*\s*\(", source)
    assert not re.search(r"\bdef\s+\w*normaliz\w*\s*\(", source)
    assert 'JobStore("eval-digest"' not in source
    assert '_store("eval-digest"' not in source


def test_a_malformed_index_does_not_crash_a_read(stores):
    stores["findings"]["index"] = {"findings": [None, {"nope": 1},
                                               {"taxonomy": "ux_gap"}]}
    stores["findings"].reset_spy()
    assert ef.weekly_digest()["total"] == 0      # no created_at ⇒ out of window
    assert ef.list_findings()["total"] == 2
    assert stores["findings"].writes == []


@pytest.mark.parametrize("key", ["index", None, 42, "", "f-nope",
                                 "steward-f-0123456789abcdef"])
def test_reading_a_non_finding_key_returns_none(stores, key):
    """The index item lives in the same store — it must never be readable as a
    finding (and an unknown id is simply absent, never an error)."""
    _import([_finding()])
    _reset_spies(stores)
    assert ef.get_finding(key) is None
    assert _all_writes(stores) == []


# ── real telemetry (pin 20) ────────────────────────────────────────────────

def test_the_import_writer_fires_the_real_usage_ledger(stores,
                                                       no_real_telemetry):
    summary = _import(_five_findings())
    assert len(no_real_telemetry) == 1
    refs, evidence = no_real_telemetry[0]
    ids = {entry["finding_id"] for entry in ef._index_entries()}
    assert refs == sorted(("eval-finding", fid) for fid in ids)
    assert evidence == ""
    assert summary["usage_bumped"] == 5


def test_an_empty_import_fires_no_telemetry(stores, no_real_telemetry):
    summary = _import([])
    assert no_real_telemetry == [] and summary["usage_bumped"] == 0


def test_the_ledger_call_is_the_real_artifact_usage_entry_point():
    """No invented counter contract: the module calls ADR-0052's ledger."""
    source = (ADT_ROOT / "agents" / "eval_findings.py").read_text()
    assert "from .artifact_usage import bump_artifact_usage" in source
    assert '("eval-finding", row["finding_id"])' in source


# ── module hygiene ─────────────────────────────────────────────────────────

def test_reset_state_drops_all_three_handles(monkeypatch):
    monkeypatch.setattr(ef, "_FINDINGS", object())
    monkeypatch.setattr(ef, "_DETAILS", object())
    monkeypatch.setattr(ef, "_STEWARD", object())
    ef._reset_state()
    assert ef._FINDINGS is None and ef._DETAILS is None and ef._STEWARD is None


def test_validators_are_reused_never_reimplemented():
    """audit_core owns the taxonomy + validation; untrusted_text owns the
    secret-shape patterns. Neither may be copied here."""
    from agents import audit_core
    source = (ADT_ROOT / "agents" / "eval_findings.py").read_text()
    assert "from .audit_core import" in source
    assert "from .untrusted_text import" in source
    assert "AKIA" not in source and "PRIVATE KEY" not in source
    assert "data_gap" not in source          # the taxonomy tuple is imported
    assert ef.TAXONOMY is audit_core.TAXONOMY
    assert ef.CLAIM_EXCERPT_CHARS == audit_core.CLAIM_EXCERPT_CHARS


def test_module_is_import_pure():
    source = (ADT_ROOT / "agents" / "eval_findings.py").read_text()
    assert "import boto3" not in source
    assert "import pg8000" not in source and "import pgdriver" not in source


def test_module_imports_with_boto3_absent():
    """Fresh interpreter with boto3 BOMBED must still import the module."""
    code = (
        "import sys\n"
        "sys.modules['boto3'] = None\n"
        "import agents.eval_findings as ef\n"
        "assert ef.DETAIL_TTL_SECONDS == 7 * 24 * 3600\n"
        "detail, rejected = ef.sanitize_llm_output({'rationale': 'ok'})\n"
        "assert detail == {'rationale': 'ok'} and rejected == {}\n"
        "print('IMPORT-PURE-OK')\n"
    )
    env = dict(os.environ, PYTHONPATH=str(ADT_ROOT))
    result = subprocess.run([sys.executable, "-c", code], cwd=str(ADT_ROOT),  # nosec B603 - fixed argv, no shell
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, \
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "IMPORT-PURE-OK" in result.stdout
