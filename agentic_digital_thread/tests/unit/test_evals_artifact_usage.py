# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evals writer telemetry — the REAL ledger, per writer, with its read path
(plan pin 20; ADR-0052 "a learning writer ships with its read path + usage
telemetry"; ADR-0062 D10).

Offline. Two halves, deliberately kept apart:

**Each writer, tested SEPARATELY.** "Counters exist after an import" proves
nothing about the proposal-filing writer, so every writer gets its own pin
over a spy on ``artifact_usage.bump_artifact_usage``:

* the findings-IMPORT writer fires ``("eval-finding", <finding_id>)`` refs and
  NOTHING else (its bump lives in ``eval_findings.replace_findings``);
* the PROPOSAL-FILED writer fires exactly one
  ``("proposed-mapping", <mapping_id>)`` ref, ONLY on an explicit steward
  ``file-proposal`` that LANDED a mapping — never on an import, never on a
  gated filing, never on an annotate, never on a GET;
* the Wave-4 bounded-triage annotator's ``audit-run`` kind is a SLOT: the read
  path already reports it (unfired today, refs-bearing the moment that writer
  bumps it), which is what makes the writer arrive with its read path.

**The read path**, ``GET /api/admin/evals/writer-telemetry``, reads those rows
back through the SAME ``artifact_usage.read_artifact_usage`` entry point
``learning_health.usage_ledger_stats`` wraps — no parallel counter contract. It
returns REFS per writer kind, performs ZERO writes (a fake table whose
``update_item`` raises proves it), carries no dollar/price/estimate-shaped key
at any depth, and carries no customer-derived text: only opaque ids.
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.artifact_usage as artifact_usage  # noqa: E402
import agents.eval_findings as ef  # noqa: E402
import agents.eval_findings_routes as efr  # noqa: E402
import agents.eval_routes as eval_routes  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents.audit_core import build_findings_doc, report_sha_bytes  # noqa: E402
from agents.job_store import JobStore  # noqa: E402

ADT_ROOT = Path(__file__).resolve().parents[2]

IMPORT_URL = "/api/admin/evals/findings/import"
LEDGER_URL = "/api/admin/evals/findings"
TELEMETRY_URL = "/api/admin/evals/writer-telemetry"

# Customer-derived sentinels: if one reaches a ledger ref or the telemetry
# payload under ANY key, the leak pin fails.
EXCERPT_SENTINEL = "ZTELEXCERPTSENTINEL4242"
RATIONALE_SENTINEL = "ZTELRATIONALESENTINEL4242"
MAPPING_SENTINEL = "ZTELMAPPINGSENTINEL4242"

#: Money/estimate-shaped key fragments that must never appear in telemetry
#: (ADR-0053: measured tokens or ABSENT — never a priced figure).
PRICE_SHAPED = ("dollar", "usd", "price", "cost", "estimate", "spend")


# ── harness (the Task-2.3 route harness, kept identical on purpose) ──────────

class _AdminScopeMiddleware:
    """PURE-ASGI admin inject: writes ``scope["state"]`` without wrapping the
    request body stream."""

    def __init__(self, app, is_admin: bool, email: str = ""):
        self._app = app
        self._is_admin = is_admin
        self._email = email

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})
            scope["state"]["is_admin"] = self._is_admin
            if self._email:
                scope["state"]["user"] = SimpleNamespace(email=self._email)
        await self._app(scope, receive, send)


def _build_client(is_admin: bool, email: str = "") -> TestClient:
    """Mount the PARENT router — the child inclusion is under test too."""
    app = FastAPI()
    app.include_router(eval_routes.router)
    app.add_middleware(_AdminScopeMiddleware, is_admin=is_admin, email=email)
    return TestClient(app)


class _SpyStore:
    """Spy persistence seam over a real in-memory ``JobStore``."""

    def __init__(self, kind: str):
        self._inner = JobStore(kind, table=None)
        self.writes: list = []
        self.deletes: list = []

    def get(self, key, default=None):
        return self._inner.get(key, default)

    def __getitem__(self, key):
        return self._inner[key]

    def __contains__(self, key):
        return key in self._inner

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

    def reset_spy(self) -> None:
        self.writes = []
        self.deletes = []


@pytest.fixture()
def stores(monkeypatch):
    findings = _SpyStore("eval-finding")
    details = _SpyStore("eval-finding-detail")
    steward = _SpyStore("eval-finding-steward")
    monkeypatch.setattr(ef, "_FINDINGS", findings)
    monkeypatch.setattr(ef, "_DETAILS", details)
    monkeypatch.setattr(ef, "_STEWARD", steward)
    return {"findings": findings, "details": details, "steward": steward}


@pytest.fixture()
def bump_spy(monkeypatch):
    """Spy over the REAL ledger writer — records ``(refs, evidence)`` calls.

    Patched on ``artifact_usage`` itself, which is where BOTH writers resolve
    it (each imports it lazily inside its own helper), so no writer can slip
    past this seam through a stale module-level alias.
    """
    calls: list = []

    def spy(refs, evidence: str = ""):
        refs = sorted(refs or ())
        calls.append((refs, evidence))
        return len(refs)

    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", spy)
    return calls


@pytest.fixture()
def filed(monkeypatch):
    """Spy over the EXISTING proposal plumbing (never the real writer)."""
    calls: list = []
    monkeypatch.setattr(self_learning, "log_proposed_mapping",
                        lambda **kwargs: calls.append(kwargs) or "map-0001")
    return calls


@pytest.fixture()
def gated(monkeypatch):
    """A filing the learning pipeline gates/absorbs: nothing lands."""
    monkeypatch.setattr(self_learning, "log_proposed_mapping",
                        lambda **kwargs: None)


@pytest.fixture()
def client():
    return _build_client(is_admin=True, email="steward@example.com")


@pytest.fixture(autouse=True)
def _clear_ledger_cache():
    """The ledger read is cached in-process for 60 s — never across tests."""
    artifact_usage._reset_read_cache()
    yield
    artifact_usage._reset_read_cache()


# ── import fixtures (findings + the report they bind to) ─────────────────────

def _report(run_id="run4242", question_ids=("g-one",)):
    return {"run_id": run_id, "run_path": "server",
            "questions": [{"id": qid, "session_id": f"eval-run-{run_id}-q1",
                           "verdict": "fail"} for qid in question_ids]}


def _finding(local_id="finding-001", taxonomy="mapping_gap",
             question_id="g-one", *, mapping=True):
    item = {"id": local_id, "taxonomy": taxonomy, "question_id": question_id,
            "claim_excerpt": f"claimed {EXCERPT_SENTINEL} units",
            "evidence_refs": [question_id],
            "rationale": f"because {RATIONALE_SENTINEL}"}
    if mapping:
        item["suggested_mapping"] = {"from_system": "erp",
                                     "from_field": MAPPING_SENTINEL,
                                     "to_system": "mes"}
    return item


def _payload(findings, report=None) -> dict:
    report = report if report is not None else _report()
    text = json.dumps(report)
    doc = build_findings_doc(
        source_report_sha=report_sha_bytes(text.encode("utf-8")),
        run_path="server", findings=findings, discarded_invalid=0)
    return {"findings_file": doc, "source_report": text}


def _import(client, findings=None) -> dict:
    resp = client.post(IMPORT_URL, json=_payload(
        findings if findings is not None else [_finding()]))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _kinds(calls: list) -> set:
    return {kind for refs, _ in calls for kind, _ in refs}


def _leaf_keys(payload) -> set:
    """Every key reachable in ``payload`` (recursive — any nesting)."""
    keys: set = set()

    def _walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(str(key))
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)

    _walk(payload)
    return keys


# ── writer 1: findings IMPORT (already shipped in Wave 1 — confirmed here) ──

def test_the_import_writer_fires_only_its_own_eval_finding_refs(
        stores, client, bump_spy):
    """The import writer's refs are its OWN kind — and its finding ids, which
    are opaque sha-16 handles, not question or excerpt text."""
    summary = _import(client, [_finding("a"), _finding("b", question_id="g-one")])

    assert len(bump_spy) == 1
    refs, evidence = bump_spy[0]
    expected = sorted(("eval-finding", entry["finding_id"])
                      for entry in ef._index_entries())
    assert refs == expected and evidence == ""
    assert summary["usage_bumped"] == 2
    assert all(ref.startswith("f-") and len(ref) == 18 for _, ref in refs)


def test_an_import_never_fires_the_proposal_filed_ref(stores, client, bump_spy):
    """Pin the SEPARATION: an import is not evidence the filing writer fired."""
    _import(client, [_finding()])
    assert _kinds(bump_spy) == {"eval-finding"}
    assert ef.WRITER_KIND_PROPOSAL_FILED not in _kinds(bump_spy)


def test_an_import_of_nothing_usable_fires_no_telemetry(stores, client,
                                                        bump_spy):
    """No row written ⇒ no use to record (never a fabricated bump)."""
    _import(client, [])
    assert bump_spy == []


# ── writer 2: steward PROPOSAL-FILED ────────────────────────────────────────

def _import_then_reset(client, stores, bump_spy) -> str:
    """Import one mapping-shaped finding, then clear the spies so what follows
    is attributable to the filing writer alone."""
    _import(client, [_finding()])
    finding_id = ef._index_entries()[0]["finding_id"]
    bump_spy.clear()
    for store in stores.values():
        store.reset_spy()
    return finding_id


def test_the_proposal_filed_writer_fires_its_own_ledger_ref(
        stores, client, bump_spy, filed):
    """ONE bump, the ``proposed-mapping`` kind, the LANDED mapping id as its
    ref — the evidence that THIS writer fired."""
    finding_id = _import_then_reset(client, stores, bump_spy)

    resp = client.post(f"{LEDGER_URL}/{finding_id}/file-proposal")

    assert resp.status_code == 200 and resp.json()["mapping_id"] == "map-0001"
    assert bump_spy == [([("proposed-mapping", "map-0001")], "")]
    assert len(filed) == 1


def test_the_filed_ref_kind_is_the_module_constant(stores, client, bump_spy,
                                                   filed):
    """The route and the read path agree on the kind by SHARING the constant —
    a renamed kind cannot silently split the writer from its read path."""
    finding_id = _import_then_reset(client, stores, bump_spy)
    client.post(f"{LEDGER_URL}/{finding_id}/file-proposal")
    (refs, _), = bump_spy
    assert refs == [(ef.WRITER_KIND_PROPOSAL_FILED, "map-0001")]
    assert ef.WRITER_KIND_PROPOSAL_FILED in ef.TELEMETRY_WRITER_KINDS


def test_a_gated_filing_fires_no_telemetry(stores, client, bump_spy, gated):
    """Nothing landed for a steward to review ⇒ nothing to record."""
    finding_id = _import_then_reset(client, stores, bump_spy)

    resp = client.post(f"{LEDGER_URL}/{finding_id}/file-proposal")

    assert resp.status_code == 200 and resp.json()["status"] == "gated"
    assert bump_spy == []


def test_a_filing_refused_for_an_expired_detail_fires_no_telemetry(
        stores, client, bump_spy, filed):
    """The 409 path never reaches the writer (nor the proposal plumbing)."""
    finding_id = _import_then_reset(client, stores, bump_spy)
    stores["details"].pop(finding_id, None)      # the 7 d detail aged out

    resp = client.post(f"{LEDGER_URL}/{finding_id}/file-proposal")

    assert resp.status_code == 409
    assert resp.json()["detail"] == efr.DETAIL_EXPIRED
    assert bump_spy == [] and filed == []


def test_an_annotation_fires_no_telemetry(stores, client, bump_spy):
    """Annotating is a steward note, not a learning-artifact use."""
    finding_id = _import_then_reset(client, stores, bump_spy)
    resp = client.post(f"{LEDGER_URL}/{finding_id}/annotate",
                       json={"status": "annotated", "note": "looks right"})
    assert resp.status_code == 200
    assert bump_spy == []


@pytest.mark.parametrize("url", [
    LEDGER_URL, f"{LEDGER_URL}?window=7d", TELEMETRY_URL,
], ids=["ledger", "digest", "telemetry"])
def test_a_read_fires_no_telemetry(stores, client, bump_spy, ledger, url):
    """A GET records nothing — telemetry is written by writers only."""
    _import_then_reset(client, stores, bump_spy)
    ledger([])
    assert client.get(url).status_code == 200
    assert bump_spy == []


def test_the_filed_ref_carries_no_customer_derived_text(stores, client,
                                                        bump_spy, filed):
    """Refs only: no excerpt, rationale or mapping VALUE reaches the ledger."""
    finding_id = _import_then_reset(client, stores, bump_spy)
    client.post(f"{LEDGER_URL}/{finding_id}/file-proposal")
    blob = json.dumps(bump_spy)
    for sentinel in (EXCERPT_SENTINEL, RATIONALE_SENTINEL, MAPPING_SENTINEL):
        assert sentinel not in blob


# ── the READ path: the same ledger, read back per writer ────────────────────

def _bucket(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _ledger_row(kind: str, ref: str, count: int = 1, *, days_ago: int = 0,
                evidence: str | None = None) -> dict:
    row = {"mapping_id": f"usage#{kind}#{ref}", "discovered_at": _bucket(days_ago),
           "count": count,
           "last_used_at": f"{_bucket(days_ago)}T05:00:00+00:00"}
    if evidence is not None:
        row["evidence"] = evidence
    return row


class _LedgerTable:
    """Fake proposals table: serves ledger rows, REFUSES every write."""

    def __init__(self, rows: list):
        self._rows = rows
        self.scans = 0

    def scan(self, **kwargs):
        self.scans += 1
        return {"Items": list(self._rows)}

    def update_item(self, **kwargs):
        raise AssertionError("a telemetry READ must never write")

    def put_item(self, **kwargs):
        raise AssertionError("a telemetry READ must never write")

    def delete_item(self, **kwargs):
        raise AssertionError("a telemetry READ must never write")


@pytest.fixture()
def ledger(monkeypatch):
    """Install ledger rows behind the REAL ``read_artifact_usage`` path."""

    def install(rows: list) -> _LedgerTable:
        table = _LedgerTable(rows)
        monkeypatch.setattr(self_learning, "_get_table", lambda: table)
        artifact_usage._reset_read_cache()
        return table

    return install


def test_the_read_path_returns_each_writers_recorded_refs(client, ledger):
    """Per writer kind: its refs, its counted uses, when it last fired."""
    ledger([_ledger_row("eval-finding", "f-0123456789abcdef", 3),
            _ledger_row("proposed-mapping", "map-0001", 2),
            _ledger_row("audit-run", "audit-77", 1)])

    body = client.get(TELEMETRY_URL).json()

    writers = body["writers"]
    assert set(writers) == set(ef.TELEMETRY_WRITER_KINDS)
    assert [ref["ref"] for ref in writers["eval-finding"]["refs"]] == \
        ["f-0123456789abcdef"]
    assert writers["eval-finding"]["uses"] == 3
    assert writers["proposed-mapping"]["refs"][0]["ref"] == "map-0001"
    assert writers["proposed-mapping"]["uses"] == 2
    for kind in ef.TELEMETRY_WRITER_KINDS:
        assert writers[kind]["fired"] is True
        assert writers[kind]["last_used_at"]
    assert body["window_days"] == ef.TELEMETRY_WINDOW_DAYS


def test_an_unfired_writer_is_reported_unfired_not_hidden(client, ledger):
    """The whole point of pin 20: refs for the import writer are NOT evidence
    that the filing writer or the annotator fired."""
    ledger([_ledger_row("eval-finding", "f-0123456789abcdef", 4)])

    writers = client.get(TELEMETRY_URL).json()["writers"]

    assert writers["eval-finding"]["fired"] is True
    for kind in ("proposed-mapping", "audit-run"):
        assert writers[kind] == {"kind": kind, "fired": False, "artifacts": 0,
                                 "uses": 0, "last_used_at": None, "refs": [],
                                 "refs_truncated": False,
                                 "evidence_classes": []}


def test_the_wave4_audit_run_slot_needs_no_change_when_that_writer_lands(
        client, ledger):
    """The Wave-4 annotator's SEAM: its kind already reads back, so the writer
    ships with its read path the moment it bumps the ledger."""
    assert ef.WRITER_KIND_AUDIT_RUN == "audit-run"
    ledger([_ledger_row("audit-run", "audit-2026-08-18-01", 1)])

    slot = client.get(TELEMETRY_URL).json()["writers"][ef.WRITER_KIND_AUDIT_RUN]

    assert slot["fired"] is True
    assert [ref["ref"] for ref in slot["refs"]] == ["audit-2026-08-18-01"]


def test_other_subsystems_ref_kinds_never_surface_here(client, ledger):
    """Concept refs are vocabulary WORDS (possibly customer terms) and rule
    refs belong to another surface — this read publishes neither."""
    ledger([_ledger_row("concept", "customer_specific_term", 9),
            _ledger_row("rule", "node-1", 5),
            _ledger_row("feedback-exploration", "filed", 4),
            _ledger_row("eval-finding", "f-0123456789abcdef", 1)])

    body = client.get(TELEMETRY_URL).json()

    assert set(body["writers"]) == set(ef.TELEMETRY_WRITER_KINDS)
    assert "customer_specific_term" not in json.dumps(body)
    assert body["writers"]["eval-finding"]["uses"] == 1


def test_the_read_path_writes_nothing(stores, client, ledger, bump_spy):
    """Computed on read: the fake table raises on ANY write, the JobStore
    spies record none, and no ledger bump fires."""
    table = ledger([_ledger_row("eval-finding", "f-0123456789abcdef", 2)])

    assert client.get(TELEMETRY_URL).status_code == 200

    assert table.scans >= 1
    assert all(store.writes == [] and store.deletes == []
               for store in stores.values())
    assert bump_spy == []


def test_stale_day_buckets_fall_out_of_the_window(client, ledger):
    """The window is the ledger's own trailing-day semantics — not re-invented
    here: a bucket older than the window simply is not counted."""
    ledger([_ledger_row("eval-finding", "f-0123456789abcdef", 3, days_ago=40)])

    default_window = client.get(TELEMETRY_URL).json()["writers"]
    wide_window = client.get(f"{TELEMETRY_URL}?window=60d").json()["writers"]

    assert default_window["eval-finding"]["fired"] is False
    assert wide_window["eval-finding"]["uses"] == 3


def test_refs_are_bounded_and_ordered_busiest_first(client, ledger):
    """A bounded list with an honest truncation flag — never an unbounded fan
    of ids on a panel read."""
    rows = [_ledger_row("eval-finding", f"f-{index:016x}", index + 1)
            for index in range(ef.TELEMETRY_REF_LIMIT + 5)]
    ledger(rows)

    slot = client.get(TELEMETRY_URL).json()["writers"]["eval-finding"]

    assert len(slot["refs"]) == ef.TELEMETRY_REF_LIMIT
    assert slot["refs_truncated"] is True
    assert slot["artifacts"] == ef.TELEMETRY_REF_LIMIT + 5
    uses = [ref["uses"] for ref in slot["refs"]]
    assert uses == sorted(uses, reverse=True)


def test_the_ledger_evidence_classes_are_surfaced_as_recorded(client, ledger):
    """Evidence rides the ledger's own class contract (a row without the attr
    folds as the ADR-0052 (b) legacy default) — never reinterpreted here."""
    ledger([_ledger_row("eval-finding", "f-0123456789abcdef", 1,
                        evidence="relationship-proof"),
            _ledger_row("proposed-mapping", "map-0001", 1)])

    writers = client.get(TELEMETRY_URL).json()["writers"]

    assert writers["eval-finding"]["evidence_classes"] == ["relationship-proof"]
    assert writers["proposed-mapping"]["evidence_classes"] == ["tool-text"]


# ── read-path contract details ──────────────────────────────────────────────

def test_the_read_path_is_the_real_ledger_entry_point(client, ledger):
    """No parallel aggregation: the SAME reader ``learning_health`` wraps."""
    source = (ADT_ROOT / "agents" / "eval_findings.py").read_text(
        encoding="utf-8")
    assert "from .artifact_usage import read_artifact_usage" in source
    # …and no re-implemented ledger scan lives in this module.
    assert "usage#" not in source and "begins_with" not in source


def test_the_window_is_clamped_by_one_owner(client, ledger):
    ledger([])
    assert client.get(f"{TELEMETRY_URL}?window=500d").json()["window_days"] \
        == ef.MAX_TELEMETRY_WINDOW_DAYS
    assert client.get(f"{TELEMETRY_URL}?window=0").json()["window_days"] == 1
    bad = client.get(f"{TELEMETRY_URL}?window=lots")
    assert bad.status_code == 422
    assert bad.json()["detail"]["reason"] == "invalid_window"


def test_a_backend_failure_degrades_to_unfired_slots(client, monkeypatch):
    """Best-effort like every ledger read: a telemetry read never 500s."""
    def boom():
        raise RuntimeError("table gone")

    monkeypatch.setattr(self_learning, "_get_table", boom)
    artifact_usage._reset_read_cache()

    body = client.get(TELEMETRY_URL).json()

    assert set(body["writers"]) == set(ef.TELEMETRY_WRITER_KINDS)
    assert all(slot["fired"] is False for slot in body["writers"].values())


def test_no_price_shaped_key_appears_in_the_telemetry_payload(client, ledger):
    """Measured uses only (ADR-0053): no dollar/price/estimate-shaped key at
    ANY depth, in the read payload or in what a writer records."""
    ledger([_ledger_row("eval-finding", "f-0123456789abcdef", 3),
            _ledger_row("proposed-mapping", "map-0001", 1)])

    body = client.get(TELEMETRY_URL).json()

    for key in _leaf_keys(body):
        assert not any(shape in key.lower() for shape in PRICE_SHAPED), key


def test_the_telemetry_payload_carries_refs_only(stores, client, filed,
                                                 ledger, monkeypatch):
    """End-to-end: import + file a proposal against the REAL ledger writer,
    then read back — customer-derived text appears nowhere in the payload."""
    recorded: list = []
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda refs, evidence="": recorded.extend(refs))
    _import(client, [_finding()])
    finding_id = ef._index_entries()[0]["finding_id"]
    client.post(f"{LEDGER_URL}/{finding_id}/file-proposal")
    assert ("proposed-mapping", "map-0001") in recorded

    ledger([_ledger_row(kind, ref) for kind, ref in recorded])
    blob = json.dumps(client.get(TELEMETRY_URL).json())

    for sentinel in (EXCERPT_SENTINEL, RATIONALE_SENTINEL, MAPPING_SENTINEL):
        assert sentinel not in blob
    assert finding_id in blob and "map-0001" in blob


# ── route wiring ────────────────────────────────────────────────────────────

def test_the_telemetry_route_is_on_the_one_evals_parent(client, ledger):
    """Asserted BEHAVIOURALLY: the one evals parent SERVES the path.

    Route-table introspection is version-dependent — in the CI image
    ``app.routes`` is only materialised once the TestClient has started, so a
    pin that inspects it reports "absent" for a route the same app answers 200
    on. Serving the path is the contract; membership in a list is not.
    """
    ledger([])
    assert client.get(TELEMETRY_URL).status_code == 200


def test_the_ledger_read_runs_off_the_event_loop():
    """The ledger read is a bounded DynamoDB scan — blocking work belongs on a
    thread (the ``vocabulary_routes`` ``read_artifact_usage`` idiom)."""
    source = (ADT_ROOT / "agents" / "eval_findings_routes.py").read_text(
        encoding="utf-8")
    assert "await asyncio.to_thread(eval_findings.writer_telemetry" in source


def test_the_telemetry_path_cannot_be_shadowed_by_a_finding_id(client, ledger):
    """Its own segment, NOT ``/findings/telemetry``: a path parameter route
    can never capture it, whatever order the parent includes children in."""
    ledger([])
    resp = client.get(TELEMETRY_URL)
    assert resp.status_code == 200 and "writers" in resp.json()
    # The shadow path must NOT be served (a finding-id route could otherwise
    # capture it). 404 = no such route; 422 would mean it matched a param route.
    assert client.get(f"{LEDGER_URL}/telemetry").status_code == 404


def test_non_admin_is_forbidden_on_the_read_path(stores, bump_spy):
    anon = _build_client(is_admin=False)
    assert anon.get(TELEMETRY_URL).status_code == 403
    assert bump_spy == []
