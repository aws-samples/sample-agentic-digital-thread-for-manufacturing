# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Findings-ledger routes (agents/eval_findings_routes.py) — pinned API
contract (ADR-0062 D6/D7/D8/D9).

Offline. The harness mounts the PARENT ``eval_routes.router`` (so the child's
inclusion is exercised, not assumed) behind a PURE-ASGI admin-inject
middleware: ``BaseHTTPMiddleware`` owns the request body stream, and one pin
below asserts the import handler never touched that stream, so the middleware
must stay out of the way. Stores are ``test_eval_findings.py``'s spy seam over
in-memory ``JobStore(..., table=None)``.

Pins (plan index 5, 7, 21 plus the route-layer ones):

* **The ingress cap runs BEFORE parsing** — an oversized ``Content-Length``
  is 413 with the stream NEVER read (spy), and a body that overflows the
  ceiling is 413 even when the header lies about its size or is absent
  entirely. A pydantic body check would run after FastAPI had already parsed.
  The cap covers the WHOLE body: an overflow carried entirely by the OPTIONAL
  ``evidence`` part is refused on both paths too.
* **The OPTIONAL third part is transient and only WIDENS validation** — an
  evidence-only ref resolves (report ids UNION evidence ids), the same ref is
  discarded without the bundle, a ref in NEITHER half is still discarded and
  counted, a malformed bundle is a pinned ``invalid_evidence`` 422 rather than
  a silent drop, and an evidence-only sentinel reaches no write, no log record
  and no response body.
* **The import BINDS its source** — both halves required (422), sha mismatch
  422, and the uploaded report is NEVER handed to a store: a report-only
  sentinel key planted by the test appears in no write.
* Off-taxonomy and invented-evidence findings are DISCARDED and COUNTED —
  never a 500.
* 403 for non-admins on EVERY route, and a rejected call writes nothing.
* Ledger and digest GETs perform ZERO writes.
* An expired 7 d detail is ``detail_expired`` on read and a DISTINCT 409 on
  ``file-proposal`` — the mapping values live only in that detail row.
* ``file-proposal`` passes ONLY ``SUGGESTED_MAPPING_KEYS`` kwargs plus
  ``source="answer-audit"`` and the two kwargs ``log_proposed_mapping`` cannot
  go without (pinned as defaultless by introspection); no excerpt, rationale
  or question text reaches the call, and NOTHING files a proposal
  automatically.
* Rejected untrusted values never appear in a response body or a log record.
"""
from __future__ import annotations

import copy
import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_findings as ef  # noqa: E402
import agents.eval_findings_routes as efr  # noqa: E402
import agents.eval_routes as eval_routes  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents.audit_core import (SUGGESTED_MAPPING_KEYS,  # noqa: E402
                               build_findings_doc, report_sha_bytes)
from agents.job_store import JobStore  # noqa: E402

ADT_ROOT = Path(__file__).resolve().parents[2]

IMPORT_URL = "/api/admin/evals/findings/import"
LEDGER_URL = "/api/admin/evals/findings"

# Distinctive sentinels: if one reaches a store, a response or a log where it
# does not belong, the pin fails whatever key it hides under.
EXCERPT_SENTINEL = "ZROUTEEXCERPTSENTINEL7711"
RATIONALE_SENTINEL = "ZROUTERATIONALESENTINEL7711"
MAPPING_SENTINEL = "ZROUTEMAPPINGSENTINEL7711"
REPORT_ONLY_SENTINEL = "ZREPORTONLYSENTINEL7711"
EVIDENCE_ONLY_SENTINEL = "ZEVIDENCEONLYSENTINEL7711"
NOTE_SENTINEL = "ZSTEWARDNOTESENTINEL7711"


# ── harness ─────────────────────────────────────────────────────────────────

class _AdminScopeMiddleware:
    """PURE-ASGI admin inject: writes ``scope["state"]`` (what
    ``request.state`` reads) without wrapping the body stream."""

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


@pytest.fixture(autouse=True)
def no_real_telemetry(monkeypatch):
    """The usage ledger is a spy by default (its real path needs DynamoDB)."""
    import agents.artifact_usage as au
    monkeypatch.setattr(au, "bump_artifact_usage",
                        lambda refs, evidence="": len(list(refs)))


@pytest.fixture()
def filed(monkeypatch):
    """Spy over the EXISTING proposal plumbing (never the real writer)."""
    calls: list = []
    monkeypatch.setattr(self_learning, "log_proposed_mapping",
                        lambda **kwargs: calls.append(kwargs) or "map-0001")
    return calls


@pytest.fixture()
def client():
    return _build_client(is_admin=True, email="steward@example.com")


# ── fixtures for the two halves the import binds ────────────────────────────

def _report(run_id="run7711", question_ids=("g-one",)):
    return {
        "run_id": run_id,
        "run_path": "server",
        # A key that exists ONLY in the uploaded report: it must never appear
        # in anything persisted.
        "report_only_marker": REPORT_ONLY_SENTINEL,
        "questions": [{"id": qid, "session_id": f"eval-run-{run_id}-q1",
                       "verdict": "fail"} for qid in question_ids],
    }


def _finding(local_id="finding-001", taxonomy="mapping_gap",
             question_id="g-one", *, mapping=True, evidence_refs=None,
             rationale=None):
    item = {
        "id": local_id,
        "taxonomy": taxonomy,
        "question_id": question_id,
        "claim_excerpt": f"claimed {EXCERPT_SENTINEL} units",
        "evidence_refs": list(evidence_refs or [question_id]),
        "rationale": rationale if rationale is not None
        else f"because {RATIONALE_SENTINEL}",
    }
    if mapping:
        item["suggested_mapping"] = {"from_system": "erp",
                                     "from_field": MAPPING_SENTINEL,
                                     "to_system": "mes"}
    return item


def _payload(findings, *, report=None, drop=None, extra=None,
             break_sha=False) -> dict:
    report = report if report is not None else _report()
    text = json.dumps(report)
    doc = build_findings_doc(source_report_sha=report_sha_bytes(
        text.encode("utf-8")), run_path="server", findings=findings,
        discarded_invalid=0)
    if break_sha:
        doc["source_report_sha"] = "0" * 64
    body = {"findings_file": doc, "source_report": text}
    for key in drop or ():
        body.pop(key, None)
    body.update(extra or {})
    return body


def _sha(report=None) -> str:
    report = report if report is not None else _report()
    return report_sha_bytes(json.dumps(report).encode("utf-8"))


def _finding_id(local_id="finding-001") -> str:
    return ef.finding_id_for(_sha(), local_id)


def _all_writes(stores) -> list:
    out = []
    for name in ("findings", "details", "steward"):
        out.extend(stores[name].writes)
    return out


def _reset_spies(stores) -> None:
    for name in ("findings", "details", "steward"):
        stores[name].reset_spy()


def _imported(client, stores, findings=None, **kwargs):
    resp = client.post(IMPORT_URL, json=_payload(
        findings if findings is not None else [_finding()], **kwargs))
    assert resp.status_code == 200, resp.text
    _reset_spies(stores)
    return resp.json()


# ── router wiring ───────────────────────────────────────────────────────────

def test_findings_routes_are_included_in_the_one_evals_parent(client):
    """D9: ONE router — the child is included into the existing parent, so
    ``server.py`` keeps its single mount and auth boundary."""
    # Behavioural: the parent SERVES each child path (405/422/404 all prove a
    # route matched; only 404-with-no-route would not, so the ledger GET is
    # asserted 200 and the others merely not-404-from-absence).
    assert client.get(LEDGER_URL).status_code == 200
    assert client.post(IMPORT_URL, json={}).status_code in (413, 422)
    # A param route matched (422 = body validation, not a missing route).
    assert client.post(f"{LEDGER_URL}/f-does-not-exist/annotate",
                       json={}).status_code in (404, 422)
    assert client.post(f"{LEDGER_URL}/f-does-not-exist/file-proposal",
                       json={}).status_code in (404, 409, 422)
    source = Path(eval_routes.__file__).read_text(encoding="utf-8")
    assert "router.include_router(eval_findings_routes.router)" in source
    # The Wave-2 peer's include stays untouched beside ours.
    assert "router.include_router(eval_queue_routes.router)" in source


def test_findings_router_declares_no_prefix_of_its_own():
    """The parent owns ``/api/admin/evals`` — a second prefix would double it."""
    assert efr.router.prefix == ""


# ── 403 on every route ──────────────────────────────────────────────────────

@pytest.mark.parametrize("method,url,body", [
    ("post", IMPORT_URL, {"findings_file": {}, "source_report": "{}"}),
    ("get", LEDGER_URL, None),
    ("get", f"{LEDGER_URL}?window=7d", None),
    ("get", f"{LEDGER_URL}/f-0123456789abcdef", None),
    ("post", f"{LEDGER_URL}/f-0123456789abcdef/annotate", {"status": "filed"}),
    ("post", f"{LEDGER_URL}/f-0123456789abcdef/file-proposal", None),
], ids=["import", "ledger", "digest", "detail", "annotate", "file-proposal"])
def test_non_admin_is_forbidden_on_every_route(stores, filed, method, url, body):
    anon = _build_client(is_admin=False)
    resp = getattr(anon, method)(url, json=body) if body is not None \
        else getattr(anon, method)(url)
    assert resp.status_code == 403
    assert _all_writes(stores) == []
    assert filed == []


# ── the ingress cap runs BEFORE parsing ─────────────────────────────────────

@pytest.fixture()
def stream_spy(monkeypatch):
    """Record every ``Request.stream()`` invocation (the body-read seam)."""
    import starlette.requests as sr
    calls: list = []
    original = sr.Request.stream

    def spy(self):
        calls.append(self.url.path)
        return original(self)

    monkeypatch.setattr(sr.Request, "stream", spy)
    return calls


def test_oversized_content_length_is_413_before_the_body_is_read(
        stores, client, stream_spy):
    """The cap is a CAP: an oversized declaration is refused with the stream
    untouched — nothing is parsed, nothing is written."""
    resp = client.post(IMPORT_URL, content=b'{"findings_file": {}}',
                       headers={"content-length": str(efr.MAX_IMPORT_BYTES + 1),
                                "content-type": "application/json"})
    assert resp.status_code == 413
    assert resp.json()["detail"]["reason"] == "payload_too_large"
    assert stream_spy == []
    assert _all_writes(stores) == []


def test_lying_content_length_still_hits_the_stream_ceiling(stores, client):
    """A header that understates the body buys nothing: the read aborts at the
    ceiling, so the parser never sees the payload."""
    oversized = b'{"findings_file": "' + b"x" * (efr.MAX_IMPORT_BYTES + 64) \
        + b'"}'
    resp = client.post(IMPORT_URL, content=oversized,
                       headers={"content-length": "12",
                                "content-type": "application/json"})
    assert resp.status_code == 413
    assert resp.json()["detail"]["reason"] == "payload_too_large"
    assert _all_writes(stores) == []


def test_absent_content_length_still_hits_the_stream_ceiling(stores, client):
    """Chunked upload (no ``Content-Length`` at all) — same ceiling."""
    chunk = b"x" * 64 * 1024

    def chunks():
        yield b'{"findings_file": "'
        for _ in range(12):
            yield chunk
        yield b'"}'

    resp = client.post(IMPORT_URL, content=chunks(),
                       headers={"content-type": "application/json"})
    assert resp.status_code == 413
    assert _all_writes(stores) == []


def test_cap_is_declared_once_and_is_the_documented_512kb(stores, client):
    assert efr.MAX_IMPORT_BYTES == 512 * 1024


def test_oversized_content_length_is_413_with_an_evidence_part_too(
        stores, client, stream_spy):
    """The optional third part shares the SAME ceiling — it does not buy extra
    headroom, and the declared-size gate still refuses before the stream is
    touched."""
    body = json.dumps(_payload([_finding()],
                               extra={"evidence": [{"id": "ev-77"}]}))
    resp = client.post(IMPORT_URL, content=body.encode("utf-8"),
                       headers={"content-length": str(efr.MAX_IMPORT_BYTES + 1),
                                "content-type": "application/json"})
    assert resp.status_code == 413
    assert resp.json()["detail"]["reason"] == "payload_too_large"
    assert stream_spy == []
    assert _all_writes(stores) == []


@pytest.mark.parametrize("declared", ["12", None],
                         ids=["lying-content-length", "absent-content-length"])
def test_oversize_carried_by_the_evidence_part_still_hits_the_ceiling(
        stores, client, declared):
    """The overflow lives ENTIRELY in the optional third part: two bound halves
    that would fit on their own plus a huge bundle is still 413, whether the
    header understates the size or is absent altogether (chunked)."""
    body = _payload([_finding()])
    body["evidence"] = [{"id": "ev-77",
                         "blob": "x" * (efr.MAX_IMPORT_BYTES + 64)}]
    raw = json.dumps(body).encode("utf-8")
    assert len(raw) > efr.MAX_IMPORT_BYTES
    headers = {"content-type": "application/json"}
    content: object = raw
    if declared is not None:
        headers["content-length"] = declared
    else:
        content = iter([raw[i:i + 64 * 1024]
                        for i in range(0, len(raw), 64 * 1024)])
    resp = client.post(IMPORT_URL, content=content, headers=headers)
    assert resp.status_code == 413
    assert resp.json()["detail"]["reason"] == "payload_too_large"
    assert _all_writes(stores) == []


# ── the import binds its source (D7) ────────────────────────────────────────

@pytest.mark.parametrize("drop", [("source_report",), ("findings_file",),
                                  ("findings_file", "source_report")],
                         ids=["no-report", "no-findings", "neither"])
def test_both_halves_are_required(stores, client, drop):
    resp = client.post(IMPORT_URL, json=_payload([_finding()], drop=drop))
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "missing_fields"
    assert _all_writes(stores) == []


def test_unknown_top_level_keys_are_rejected(stores, client):
    resp = client.post(IMPORT_URL,
                       json=_payload([_finding()], extra={"source_run_id": "r"}))
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "unknown_keys"
    assert _all_writes(stores) == []


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'"x"', b"null"],
                         ids=["garbage", "list", "string", "null"])
def test_non_object_bodies_are_rejected(stores, client, raw):
    resp = client.post(IMPORT_URL, content=raw,
                       headers={"content-type": "application/json"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] in {"invalid_json", "missing_fields"}
    assert _all_writes(stores) == []


def test_sha_mismatch_is_fail_closed_422(stores, client):
    """No "resolve the referenced report if we have it" path exists: a report
    that does not hash to the findings file's binding is refused."""
    resp = client.post(IMPORT_URL, json=_payload([_finding()], break_sha=True))
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "source_report_mismatch"
    assert _all_writes(stores) == []


def test_re_serialized_report_object_is_refused(stores, client):
    """The binding is over the EXACT bytes the auditor hashed, so the report
    travels as text; an object cannot reproduce the digest and is refused
    with the pinned reason rather than silently mis-binding."""
    body = _payload([_finding()])
    body["source_report"] = _report()
    resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_source_report"
    assert _all_writes(stores) == []


def test_unsupported_findings_version_is_refused(stores, client):
    body = _payload([_finding()])
    body["findings_file"]["version"] = 99
    resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "unsupported_version"
    assert _all_writes(stores) == []


def test_happy_import_persists_minimized_rows_and_never_the_report(stores,
                                                                   client):
    resp = client.post(IMPORT_URL, json=_payload([_finding()]))
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["written"] == 1
    assert summary["discarded_invalid"] == 0
    assert summary["taxonomy_counts"] == {"mapping_gap": 1}
    assert summary["source_report_sha"] == _sha()
    # The uploaded report reached NO store — not its body, not its marker key.
    persisted = json.dumps(_all_writes(stores))
    assert REPORT_ONLY_SENTINEL not in persisted
    assert "report_only_marker" not in persisted
    # ... and the durable scope carries no customer-derived text.
    durable = json.dumps(stores["findings"].writes)
    for sentinel in (EXCERPT_SENTINEL, RATIONALE_SENTINEL, MAPPING_SENTINEL):
        assert sentinel not in durable


def test_off_taxonomy_findings_are_discarded_and_counted(stores, client):
    """Outside the pinned 7 ⇒ DISCARDED + counted; never a 500."""
    body = _payload([_finding(), _finding("finding-002",
                                          taxonomy="hallucination")])
    resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 200
    assert resp.json() | {"written": 1, "discarded_invalid": 1} == resp.json()


def test_invented_evidence_refs_are_discarded_and_counted(stores, client):
    """Referential integrity is re-checked server-side against the supplied
    bundle — an invented id is discarded, not repaired."""
    body = _payload([_finding("finding-001",
                              evidence_refs=["not-in-the-bundle"])])
    resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 200
    assert resp.json()["written"] == 0
    assert resp.json()["discarded_invalid"] == 1


# ── the OPTIONAL transient evidence part ────────────────────────────────────

def _evidence_bundle():
    """An ``audit_answer.py --evidence`` bundle: ids the report does not carry,
    plus a customer-derived leaf that must not survive the request."""
    return [{"trace_id": "trace-ev-77",
             "steps": [{"step_id": "step-ev-88",
                        "observation": EVIDENCE_ONLY_SENTINEL}]}]


@pytest.mark.parametrize("ref", ["trace-ev-77", "step-ev-88"])
def test_an_evidence_only_ref_resolves_when_the_bundle_is_supplied(
        stores, client, ref):
    """The fix: a finding citing an id that exists ONLY in the CLI's evidence
    bundle is KEPT, because the known-id set is report ids UNION evidence
    ids."""
    body = _payload([_finding("finding-001", evidence_refs=[ref])],
                    extra={"evidence": _evidence_bundle()})
    resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"] == 1
    assert resp.json()["discarded_invalid"] == 0


def test_the_same_ref_is_discarded_without_the_bundle(stores, client):
    """The limitation the third part exists to fix — unchanged when nothing is
    supplied. Nothing widens implicitly."""
    body = _payload([_finding("finding-001", evidence_refs=["step-ev-88"])])
    resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 200
    assert resp.json() | {"written": 0, "discarded_invalid": 1} == resp.json()


def test_a_single_evidence_object_is_accepted_as_well_as_a_list(stores,
                                                               client):
    """One document is the common CLI case; it needs no wrapper array."""
    body = _payload([_finding("finding-001", evidence_refs=["trace-ev-77"])],
                    extra={"evidence": _evidence_bundle()[0]})
    resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"] == 1


@pytest.mark.parametrize("evidence", ["a string", 42, ["not an object"],
                                      [{"id": "ev"}, 7], True],
                         ids=["string", "number", "list-of-strings",
                              "mixed-list", "bool"])
def test_a_malformed_evidence_part_is_a_422_never_a_silent_drop(stores, client,
                                                               evidence):
    """Dropping a malformed bundle would re-open the failure this input fixes:
    the steward supplies evidence, the refs still resolve nowhere, and the
    findings come back discarded with nothing saying why."""
    body = _payload([_finding("finding-001", evidence_refs=["step-ev-88"])],
                    extra={"evidence": evidence})
    resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_evidence"
    assert _all_writes(stores) == []


def test_an_unknown_ref_is_still_discarded_when_evidence_is_supplied(stores,
                                                                    client):
    """HONEST REPORTING: supplying evidence widens the id set, it does not
    silence discards. A finding citing an id in NEITHER half is still
    discarded and still counted beside a sibling the bundle resolves."""
    body = _payload([_finding("finding-001", evidence_refs=["trace-ev-77"]),
                     _finding("finding-002",
                              evidence_refs=["id-in-neither-half"])],
                    extra={"evidence": _evidence_bundle()})
    resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"] == 1
    assert resp.json()["discarded_invalid"] == 1


def test_evidence_bundle_travels_transiently_and_is_never_persisted(
        stores, client, caplog):
    """Spy persistence seam + log capture: the bundle is customer-derived, so
    a sentinel planted ONLY there appears in no write, no log record and no
    response body."""
    evidence = _evidence_bundle() + [
        {"id": "ev-99", "report_only_marker": REPORT_ONLY_SENTINEL}]
    body = _payload([_finding("finding-001", evidence_refs=["step-ev-88"])],
                    extra={"evidence": evidence})
    with caplog.at_level(0):
        resp = client.post(IMPORT_URL, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"] == 1
    persisted = json.dumps(_all_writes(stores))
    for sentinel in (EVIDENCE_ONLY_SENTINEL, REPORT_ONLY_SENTINEL):
        assert sentinel not in persisted
        assert sentinel not in caplog.text
        assert sentinel not in resp.text
    assert "observation" not in persisted


def test_secret_shaped_rationale_is_counted_never_echoed(stores, client,
                                                         caplog):
    """LLM output is untrusted (D8): the value is dropped and counted, and it
    reaches neither the response nor a log record."""
    caplog.set_level("DEBUG")
    rationale = f"password: AKIAIOSFODNN7EXAMPLE {RATIONALE_SENTINEL}"
    resp = client.post(IMPORT_URL,
                       json=_payload([_finding(rationale=rationale)]))
    assert resp.status_code == 200
    assert resp.json()["rejected_fields"]
    assert RATIONALE_SENTINEL not in resp.text
    assert RATIONALE_SENTINEL not in caplog.text
    assert RATIONALE_SENTINEL not in json.dumps(_all_writes(stores))


# ── reads perform zero writes (computed on read) ─────────────────────────────

def test_ledger_get_is_grouped_by_taxonomy_and_writes_nothing(stores, client):
    _imported(client, stores)
    resp = client.get(LEDGER_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [row["finding_id"] for row in body["groups"]["mapping_gap"]] == \
        [_finding_id()]
    assert body["counts"]["mapping_gap"] == 1
    assert _all_writes(stores) == []
    for name in ("findings", "details", "steward"):
        assert stores[name].deletes == []
    # Refs stay refs: the ledger resolves no detail.
    assert EXCERPT_SENTINEL not in resp.text


def test_digest_get_writes_nothing(stores, client):
    """Pin 21: the digest is COMPUTED ON READ — no digest writer exists."""
    _imported(client, stores)
    resp = client.get(f"{LEDGER_URL}?window=7d")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 7
    assert body["taxonomy_counts"]["mapping_gap"] == 1
    assert body["ux_gap_refs"] == []
    assert _all_writes(stores) == []
    for name in ("findings", "details", "steward"):
        assert stores[name].deletes == []


@pytest.mark.parametrize("window,days", [("7d", 7), ("7", 7), ("30d", 30),
                                         ("999d", 90)],
                         ids=["7d", "bare-7", "30d", "clamped"])
def test_window_parsing_and_clamping(stores, client, window, days):
    """Bounds have ONE home: ``weekly_digest`` clamps, the route only parses."""
    _imported(client, stores)
    resp = client.get(f"{LEDGER_URL}?window={window}")
    assert resp.status_code == 200
    assert resp.json()["window_days"] == days


@pytest.mark.parametrize("window", ["abc", "7 days", "-3", "7d7", ""],
                         ids=["word", "phrase", "negative", "trailing", "empty"])
def test_malformed_window_is_422(stores, client, window):
    resp = client.get(f"{LEDGER_URL}?window={window}")
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_window"


def test_detail_route_is_the_only_ref_resolving_door(stores, client):
    _imported(client, stores)
    resp = client.get(f"{LEDGER_URL}/{_finding_id()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["detail_expired"] is False
    assert EXCERPT_SENTINEL in body["detail"]["claim_excerpt"]
    assert body["detail"]["suggested_mapping"]["from_field"] == MAPPING_SENTINEL
    assert _all_writes(stores) == []


def test_unknown_finding_id_is_404(stores, client):
    _imported(client, stores)
    assert client.get(f"{LEDGER_URL}/f-0000000000000000").status_code == 404
    assert client.get(f"{LEDGER_URL}/index").status_code == 404


def test_expired_detail_is_a_state_not_an_error(stores, client):
    """The 7 d TTL firing is NORMAL (D6)."""
    _imported(client, stores)
    stores["details"].pop(_finding_id(), None)
    body = client.get(f"{LEDGER_URL}/{_finding_id()}").json()
    assert body["detail_expired"] is True
    assert body["detail"] is None
    assert body["finding_id"] == _finding_id()


# ── steward annotation ──────────────────────────────────────────────────────

def test_annotate_records_a_steward_row(stores, client):
    _imported(client, stores)
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/annotate",
                       json={"status": "dismissed", "note": "known ERP gap"})
    assert resp.status_code == 200
    row = resp.json()["annotation"]
    assert row["status"] == "dismissed"
    assert row["steward_note"] == "known ERP gap"
    assert row["annotated_by"] == "steward@example.com"
    # The steward write lands in the steward scope ONLY.
    assert stores["findings"].writes == [] and stores["details"].writes == []
    assert [key for key, _ in stores["steward"].writes] == \
        [ef.steward_key(_finding_id())]


def test_annotated_status_wins_on_read(stores, client):
    _imported(client, stores)
    client.post(f"{LEDGER_URL}/{_finding_id()}/annotate",
                json={"status": "dismissed"})
    assert client.get(f"{LEDGER_URL}/{_finding_id()}").json()["status"] == \
        "dismissed"


@pytest.mark.parametrize("body", [{"unknown": "x"}, {"status": "rejected"},
                                  {"status": "OPEN"}, {"annotated_by": "me"}],
                         ids=["unknown-key", "rejected", "uppercase", "actor"])
def test_annotate_rejects_bad_bodies(stores, client, body):
    """``extra="forbid"`` plus the pinned lifecycle enum — and the actor is
    NEVER client-supplied (it comes from the session)."""
    _imported(client, stores)
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/annotate", json=body)
    assert resp.status_code == 422
    assert _all_writes(stores) == []


def test_annotate_unknown_finding_is_404(stores, client):
    resp = client.post(f"{LEDGER_URL}/f-0000000000000000/annotate",
                       json={"status": "filed"})
    assert resp.status_code == 404
    assert _all_writes(stores) == []


def test_secret_shaped_note_is_rejected_and_never_echoed(stores, client,
                                                         caplog):
    caplog.set_level("DEBUG")
    _imported(client, stores)
    note = f"password: AKIAIOSFODNN7EXAMPLE {NOTE_SENTINEL}"
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/annotate",
                       json={"note": note})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "secret_like_content"
    assert NOTE_SENTINEL not in resp.text
    assert "AKIAIOSFODNN7EXAMPLE" not in resp.text
    assert NOTE_SENTINEL not in caplog.text
    assert _all_writes(stores) == []


# ── file-proposal: the ONE door for mapping values ──────────────────────────

def test_file_proposal_passes_only_allowlist_kwargs(stores, client, filed):
    """The mapping-derived kwargs are exactly the ALLOWLIST; the only other
    kwargs are ``source`` and the two the callee cannot go without."""
    _imported(client, stores)
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/file-proposal")
    assert resp.status_code == 200
    assert resp.json()["mapping_id"] == "map-0001"
    assert len(filed) == 1
    kwargs = filed[0]
    unavoidable = {"source", "confidence", "query_context"}
    assert set(kwargs) - unavoidable <= set(SUGGESTED_MAPPING_KEYS)
    assert kwargs["source"] == "answer-audit"
    assert kwargs["confidence"] == efr.FILED_CONFIDENCE
    assert kwargs["from_field"] == MAPPING_SENTINEL
    # Provenance only — an OPAQUE finding id, never question or answer text.
    assert kwargs["query_context"] == f"answer-audit:{_finding_id()}"
    serialized = json.dumps(kwargs)
    assert EXCERPT_SENTINEL not in serialized
    assert RATIONALE_SENTINEL not in serialized


def test_file_proposal_forwards_a_full_mapping(stores, client, filed):
    mapping = {key: f"v-{key}" for key in SUGGESTED_MAPPING_KEYS}
    finding = _finding()
    finding["suggested_mapping"] = mapping
    _imported(client, stores, [finding])
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/file-proposal")
    assert resp.status_code == 200
    kwargs = filed[0]
    assert set(kwargs) == set(SUGGESTED_MAPPING_KEYS) | {
        "source", "confidence", "query_context"}
    assert {key: kwargs[key] for key in SUGGESTED_MAPPING_KEYS} == mapping


def test_file_proposal_records_the_filed_status(stores, client, filed):
    _imported(client, stores)
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/file-proposal")
    assert resp.json()["status"] == "filed"
    assert resp.json()["annotation"]["status"] == "filed"
    assert [key for key, _ in stores["steward"].writes] == \
        [ef.steward_key(_finding_id())]
    assert stores["findings"].writes == []


def test_expired_detail_is_a_distinct_409_and_files_nothing(stores, client,
                                                            filed):
    """The mapping values live ONLY in the 7 d detail row, so filing after
    expiry is a refusal (D6) — not a filing of something nobody can review."""
    _imported(client, stores)
    stores["details"].pop(_finding_id(), None)
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/file-proposal")
    assert resp.status_code == 409
    assert resp.json()["detail"] == efr.DETAIL_EXPIRED
    assert filed == []
    assert _all_writes(stores) == []


def test_finding_without_a_mapping_is_a_different_409(stores, client, filed):
    """DISTINCT details — never collapse "aged out" with "never had one"."""
    _imported(client, stores, [_finding(mapping=False)])
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/file-proposal")
    assert resp.status_code == 409
    assert resp.json()["detail"] == efr.DETAIL_NO_MAPPING
    assert efr.DETAIL_NO_MAPPING != efr.DETAIL_EXPIRED
    assert filed == []


def test_file_proposal_unknown_finding_is_404(stores, client, filed):
    resp = client.post(f"{LEDGER_URL}/f-0000000000000000/file-proposal")
    assert resp.status_code == 404
    assert filed == []


def test_gated_proposal_is_reported_and_not_marked_filed(stores, client,
                                                         monkeypatch):
    """``log_proposed_mapping`` gates, absorbs and stages — ``None`` is normal,
    and nothing landed, so the finding is NOT marked filed."""
    monkeypatch.setattr(self_learning, "log_proposed_mapping",
                        lambda **kwargs: None)
    _imported(client, stores)
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/file-proposal")
    assert resp.status_code == 200
    assert resp.json() == {"status": "gated", "finding_id": _finding_id(),
                           "mapping_id": None}
    assert stores["steward"].writes == []


def test_filing_failure_never_echoes_a_value(stores, client, monkeypatch,
                                             caplog):
    caplog.set_level("DEBUG")

    def boom(**kwargs):
        raise RuntimeError(f"backend refused {MAPPING_SENTINEL}")

    monkeypatch.setattr(self_learning, "log_proposed_mapping", boom)
    _imported(client, stores)
    resp = client.post(f"{LEDGER_URL}/{_finding_id()}/file-proposal")
    assert resp.status_code == 502
    assert MAPPING_SENTINEL not in resp.text
    assert MAPPING_SENTINEL not in caplog.text
    assert stores["steward"].writes == []


# ── the filing contract, pinned by introspection + source scan ──────────────

def test_required_mapping_keys_are_the_defaultless_allowlist_keys():
    """Why "" fills some keys and not others: these are the allowlist keys
    ``log_proposed_mapping`` has no default for, so the call needs them."""
    params = inspect.signature(self_learning.log_proposed_mapping).parameters
    defaultless = {key for key in SUGGESTED_MAPPING_KEYS
                   if params[key].default is inspect.Parameter.empty}
    assert set(efr.REQUIRED_MAPPING_KEYS) == defaultless


def test_confidence_and_query_context_are_unavoidable_kwargs():
    """They are not extra data smuggled in — the callee REQUIRES them, which
    is why the route supplies fixed, non-customer-derived values."""
    params = inspect.signature(self_learning.log_proposed_mapping).parameters
    for name in ("confidence", "query_context"):
        assert params[name].default is inspect.Parameter.empty
        assert name not in SUGGESTED_MAPPING_KEYS


def test_answer_audit_source_never_auto_accepts():
    assert efr.PROPOSAL_SOURCE in self_learning._NEVER_AUTO_ACCEPT_SOURCES


def test_nothing_files_an_answer_audit_proposal_automatically():
    """Steward-TRIGGERED only: the sole module that both names the source and
    CALLS the proposal writer is this route module. ``self_learning`` matches
    because it DEFINES the writer and names the source in its
    never-auto-accept guard (asserted above)."""
    filing = set()
    for path in sorted((ADT_ROOT / "agents").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if efr.PROPOSAL_SOURCE in text and "log_proposed_mapping(" in text:
            filing.add(path.name)
    assert filing == {"eval_findings_routes.py", "self_learning.py"}


def test_the_generated_path_cannot_file_a_proposal():
    """The importer and the auditor core reach no proposal writer at all."""
    for name in ("eval_findings.py", "audit_core.py"):
        text = (ADT_ROOT / "agents" / name).read_text(encoding="utf-8")
        assert "log_proposed_mapping(" not in text
