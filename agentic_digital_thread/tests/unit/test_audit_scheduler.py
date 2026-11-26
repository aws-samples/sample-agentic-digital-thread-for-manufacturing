# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bounded triage annotator (agents/audit_scheduler.py) + its default-off
registration wrapper (agents/server.py) — ADR-0062 D11, plan Tasks 4.1/4.2.

Offline: the converse seam is a fake (no boto3, no network), the stores are
in-memory ``JobStore(..., table=None)``, and the budget primitive runs in its
announced in-process mode.

Pins (plan index 10, 13, 14, 15, 16):

* **ONE entry** — ``request_audit(trigger)`` accepts ``trigger`` and NOTHING
  else (signature introspection), and ``trigger`` is PROVENANCE ONLY: a manual
  and a scheduled run over the SAME 11-candidate fixture produce byte-identical
  records apart from run id, timestamps and the trigger label.
* **``_run_audit`` is the SOLE consumer of the converse seam and the SOLE
  findings writer** — proven by an in-module CALL GRAPH (nothing reachable from
  ``request_audit`` can call either) plus a repo scan for other callers.
* **The budget is atomic, global and claimed BEFORE any model call** — one slot
  per converse call, so a race can never exceed the cap; a cloud-mode
  ``BudgetUnavailable`` spends NOTHING and records ``skipped_budget``; an
  exhausted budget refuses with a DISTINCT reason.
* **The scheduled path is EXECUTED, not inferred** — the registered callback is
  captured from a fake scheduler and INVOKED, and it must call
  ``request_audit(trigger="scheduled")``. With the flag unset/falsy nothing is
  registered at all.
* **Memory absence is architectural** — an import-graph walk over
  ``audit_scheduler`` + ``audit_core`` (module level, transitive) plus an
  any-depth AST scan of both files: no chat, session or memory module, and no
  ``write_memory`` anywhere.
* **``usage`` is measured or ABSENT** — never zero-as-unknown, and no
  money-shaped key reaches a record or a persisted row.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.artifact_usage as artifact_usage  # noqa: E402
import agents.audit_core as audit_core  # noqa: E402
import agents.audit_scheduler as asch  # noqa: E402
import agents.eval_findings as ef  # noqa: E402
import agents.eval_findings_routes as efr  # noqa: E402
import agents.eval_queue as eq  # noqa: E402
import agents.eval_routes as eval_routes  # noqa: E402
import agents.job_store as job_store  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
import agents.server as server  # noqa: E402
from agents.job_store import JobStore  # noqa: E402

AGENTS_DIR = Path(asch.__file__).resolve().parent
RUNS_URL = "/api/admin/evals/audit/runs"
STATUS_URL = "/api/admin/evals/audit/status"

#: Modules the annotator must never reach — conversation memory, the chat
#: client, session management. ``audit_learning.py --strict`` does not prove
#: this; the import graph does.
FORBIDDEN_MODULES = ("chat_client", "memory_client", "memory_labels",
                     "session_manager", "runtime_app", "orchestrator")


# ── harness ─────────────────────────────────────────────────────────────────

class _Converse:
    """Fake converse seam: counts calls, returns ONE citing finding."""

    def __init__(self, *, usage=True, delay: float = 0.0, boom: bool = False):
        self.calls: list = []
        self._usage = usage
        self._delay = delay
        self._boom = boom
        self._lock = threading.Lock()

    def __call__(self, *, model_id, max_tokens, system_prompt, user_message,
                 region=None):
        bundle = json.loads(user_message)
        with self._lock:
            self.calls.append({"model_id": model_id, "max_tokens": max_tokens,
                               "question_id": bundle["question_id"]})
        if self._delay:
            time.sleep(self._delay)
        if self._boom:
            raise RuntimeError("throttled")
        finding = {"taxonomy": "mapping_gap",
                   "question_id": bundle["question_id"],
                   "claim_excerpt": "claimed 42 units on hand",
                   "evidence_refs": [bundle["question_id"]],
                   "rationale": "the two systems disagree on the unit"}
        response = {"output": {"message": {"content": [
            {"text": json.dumps([finding])}]}}}
        if self._usage:
            response["usage"] = {"inputTokens": 10, "outputTokens": 5}
        return response

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """In-memory stores, the in-process budget, a stub model id, flag OFF."""
    monkeypatch.setattr(asch, "_JOBS", JobStore("audit-run", table=None))
    monkeypatch.setattr(ef, "_FINDINGS", JobStore("eval-finding", table=None))
    monkeypatch.setattr(ef, "_DETAILS",
                        JobStore("eval-finding-detail", table=None))
    monkeypatch.setattr(ef, "_STEWARD",
                        JobStore("eval-finding-steward", table=None))
    monkeypatch.setattr(asch, "_model_id", lambda region: "test.model.v1")
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda refs, evidence="": len(list(refs)))
    monkeypatch.setattr(artifact_usage, "read_artifact_usage",
                        lambda days=30: {})
    monkeypatch.delenv(asch.SCHEDULED_FLAG, raising=False)
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    job_store.reset_budget_claims()
    yield
    job_store.reset_budget_claims()


@pytest.fixture()
def converse(monkeypatch):
    fake = _Converse()
    monkeypatch.setattr(audit_core, "_converse_factory", fake)
    return fake


@pytest.fixture()
def no_proposals(monkeypatch):
    """Spy: NOTHING files a proposal automatically."""
    filed: list = []
    monkeypatch.setattr(self_learning, "log_proposed_mapping",
                        lambda **kwargs: filed.append(kwargs) or "map-0001")
    return filed


def _golden_item(index: int, run_id: str = "run4711") -> dict:
    """One golden-failure queue item (the ``eval_queue`` shape)."""
    ref = f"{index:016x}"
    return {"source_ref": f"golden-failure:{run_id}:g-{index:02d}",
            "ref_sha": ref, "source": eq.SOURCE_GOLDEN_FAILURE,
            "occurred_at": "2026-08-18T00:00:00+00:00", "run_id": run_id,
            "question_id": f"g-{index:02d}", "run_path": "server",
            "answer_excerpt": "we shipped 42 units",
            "failures": ["expected 7, answer said 42"],
            "decision": None, "source_expired": False}


def _feedback_item(index: int, sentiment: str = "negative") -> dict:
    ref = f"{index:016x}"
    return {"source_ref": f"feedback:fb-{'a' * 32}", "ref_sha": ref,
            "source": eq.SOURCE_FEEDBACK, "sentiment": sentiment,
            "occurred_at": "2026-08-18T00:00:00+00:00",
            "question": "how many units shipped?",
            "answer_excerpt": "42 units", "user_comment": "that is wrong",
            "cited_systems": ["erp"], "decision": None,
            "source_expired": False}


def _queue(monkeypatch, items: list) -> None:
    monkeypatch.setattr(eq, "compute_queue",
                        lambda *a, **k: {"items": list(items),
                                         "counts": {"total": len(items)}})


def _eleven(monkeypatch) -> None:
    """The pinned 11-candidate fixture (cap 10 ⇒ 10 audited + 1 over cap)."""
    _queue(monkeypatch, [_golden_item(index) for index in range(1, 12)])


def _await_run(run_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = asch.get_run(run_id)
        if isinstance(record, dict) and \
                record.get("status") != asch.STATUS_RUNNING:
            return record
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} never finished")


class _AdminScopeMiddleware:
    """PURE-ASGI admin inject (the ``test_eval_findings_routes`` harness)."""

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


def _client(is_admin: bool = True) -> TestClient:
    """Mount the PARENT router — the child inclusion is under test too."""
    app = FastAPI()
    app.include_router(eval_routes.router)
    app.add_middleware(_AdminScopeMiddleware, is_admin=is_admin,
                       email="steward@example.com")
    return TestClient(app)


# ── (6a) ONE entry: the signature IS the contract ───────────────────────────

def test_request_audit_accepts_only_trigger():
    params = inspect.signature(asch.request_audit).parameters
    assert list(params) == ["trigger"]
    assert params["trigger"].default is inspect.Parameter.empty
    for banned in ("count", "cap", "model", "model_id", "source", "limit",
                   "max_tokens", "candidates", "ids"):
        assert banned not in params


def test_request_audit_rejects_an_unknown_trigger(converse):
    with pytest.raises(ValueError):
        asch.request_audit("nightly")
    assert converse.count == 0


def test_cap_is_a_module_constant_read_inside_the_worker():
    """No entry point may pass a cap: the worker reads the constant itself."""
    source = (AGENTS_DIR / "audit_scheduler.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    worker = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "_run_audit")
    names = {node.id for node in ast.walk(worker) if isinstance(node, ast.Name)}
    assert "DAILY_AUDIT_CAP" in names
    assert asch.DAILY_AUDIT_CAP == 10


# ── (4) trigger is PROVENANCE ONLY: identical outcomes ──────────────────────

def _run_once(trigger: str) -> dict:
    run_id, reason = asch.request_audit(trigger)
    assert reason is None and run_id
    return _await_run(run_id)


def test_manual_and_scheduled_runs_are_identical_apart_from_provenance(
        monkeypatch, converse, no_proposals):
    _eleven(monkeypatch)
    manual = _run_once(asch.TRIGGER_MANUAL)
    # Same starting state for the second arm: a fresh UTC-day budget and empty
    # ledger scopes, so ONLY the trigger differs between the two runs.
    job_store.reset_budget_claims()
    monkeypatch.setattr(ef, "_FINDINGS", JobStore("eval-finding", table=None))
    monkeypatch.setattr(ef, "_DETAILS",
                        JobStore("eval-finding-detail", table=None))
    scheduled = _run_once(asch.TRIGGER_SCHEDULED)

    varying = ("run_id", "trigger", "started_at", "finished_at")
    assert {k: v for k, v in manual.items() if k not in varying} == \
           {k: v for k, v in scheduled.items() if k not in varying}
    assert manual["trigger"] == "manual"
    assert scheduled["trigger"] == "scheduled"
    assert manual["run_id"] != scheduled["run_id"]
    assert manual["counts"]["candidates"] == 11
    assert manual["counts"]["audited"] == asch.DAILY_AUDIT_CAP
    assert manual["counts"]["skipped_over_cap"] == 1
    assert manual["counts"]["skipped_budget"] == 0
    assert manual["status"] == asch.STATUS_COMPLETE
    assert converse.count == 2 * asch.DAILY_AUDIT_CAP
    assert no_proposals == []                # NEVER files a proposal


def test_a_run_writes_findings_through_the_ledger(monkeypatch, converse):
    _queue(monkeypatch, [_golden_item(1), _feedback_item(2)])
    record = _run_once(asch.TRIGGER_MANUAL)
    assert record["counts"]["findings"] == 2
    ledger = ef.list_findings()
    assert ledger["total"] == 2
    # The feedback scope and the report scope are SEPARATE replace scopes, so
    # re-annotating one never deletes the other's rows.
    scopes = {entry["source_report_sha"]
              for entry in ledger["groups"]["mapping_gap"]}
    assert len(scopes) == 2


def test_findings_survive_reannotation_by_deterministic_id(monkeypatch,
                                                          converse):
    _queue(monkeypatch, [_golden_item(1)])
    first = _run_once(asch.TRIGGER_MANUAL)
    ids_before = [entry["finding_id"]
                  for entry in ef.list_findings()["groups"]["mapping_gap"]]
    job_store.reset_budget_claims()
    second = _run_once(asch.TRIGGER_SCHEDULED)
    ids_after = [entry["finding_id"]
                 for entry in ef.list_findings()["groups"]["mapping_gap"]]
    assert ids_before == ids_after                    # replace, not accrete
    assert first["counts"]["replaced"] == 0
    assert second["counts"]["replaced"] == 1


# ── (5) + (13) the budget: atomic, global, cap-bounded, fail-closed ─────────

def test_budget_is_claimed_before_any_model_call(monkeypatch, converse):
    """A single slot left ⇒ exactly ONE converse call over 11 candidates."""
    _eleven(monkeypatch)
    day = job_store.utc_day_key()
    assert job_store.claim_budget_slots(asch.BUDGET_KIND, day,
                                        asch.DAILY_AUDIT_CAP - 1,
                                        asch.DAILY_AUDIT_CAP) == 9
    record = _run_once(asch.TRIGGER_MANUAL)
    assert converse.count == 1
    assert record["counts"]["audited"] == 1
    assert record["counts"]["skipped_budget"] == 9
    assert record["slots_claimed"] == 1


def test_exhausted_budget_refuses_with_a_distinct_reason(monkeypatch,
                                                         converse):
    _eleven(monkeypatch)
    day = job_store.utc_day_key()
    job_store.claim_budget_slots(asch.BUDGET_KIND, day, asch.DAILY_AUDIT_CAP,
                                 asch.DAILY_AUDIT_CAP)
    run_id, reason = asch.request_audit(asch.TRIGGER_MANUAL)
    assert (run_id, reason) == (None, asch.REASON_BUDGET_EXHAUSTED)
    assert converse.count == 0
    runs = asch.audit_status()["runs"]
    assert runs[0]["status"] == asch.STATUS_SKIPPED_BUDGET


def test_cloud_mode_budget_unavailable_fails_closed(monkeypatch, converse):
    """A budget that cannot be CONSULTED spends nothing (D11 fail-closed)."""
    _eleven(monkeypatch)

    def _unavailable(*args, **kwargs):
        raise job_store.BudgetUnavailable("budget_claim_unavailable: test")

    monkeypatch.setattr(job_store, "claim_budget_slots_detailed", _unavailable)
    run_id, reason = asch.request_audit(asch.TRIGGER_MANUAL)
    assert (run_id, reason) == (None, asch.REASON_BUDGET_UNAVAILABLE)
    assert converse.count == 0
    runs = asch.audit_status()["runs"]
    assert runs[0]["status"] == asch.STATUS_SKIPPED_BUDGET
    assert runs[0]["counts"] == {"audited": 0}


def test_mid_run_budget_unavailability_stops_the_worker(monkeypatch,
                                                        converse):
    """The admission slot is spent; the next claim is unavailable ⇒ STOP."""
    _eleven(monkeypatch)

    def _unavailable(*args, **kwargs):
        raise job_store.BudgetUnavailable("budget_claim_unavailable: test")

    real_detailed = job_store.claim_budget_slots_detailed
    monkeypatch.setattr(job_store, "claim_budget_slots", _unavailable)
    monkeypatch.setattr(job_store, "claim_budget_slots_detailed",
                        real_detailed)
    record = _run_once(asch.TRIGGER_MANUAL)
    assert converse.count == 1
    # 11 candidates, 10 selected: the admission slot bought the first call and
    # the remaining 9 were refused a slot.
    assert record["counts"]["skipped_budget"] == 9


def test_two_racing_runs_never_exceed_the_cap(monkeypatch):
    """The race pin: whatever interleaving, model calls stay within the cap."""
    fake = _Converse(delay=0.02)
    monkeypatch.setattr(audit_core, "_converse_factory", fake)
    _eleven(monkeypatch)
    outcomes: list = []
    lock = threading.Lock()

    def _request():
        result = asch.request_audit(asch.TRIGGER_MANUAL)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=_request) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    for run_id, _reason in outcomes:
        if run_id:
            _await_run(run_id)

    assert fake.count <= asch.DAILY_AUDIT_CAP
    refusals = [reason for run_id, reason in outcomes if run_id is None]
    assert refusals, "one caller must be refused (active run or budget)"
    assert set(refusals) <= {asch.REASON_ACTIVE, asch.REASON_BUDGET_EXHAUSTED,
                             asch.REASON_BUDGET_UNAVAILABLE}


def test_a_second_request_while_one_is_active_is_refused(monkeypatch):
    fake = _Converse(delay=0.05)
    monkeypatch.setattr(audit_core, "_converse_factory", fake)
    _eleven(monkeypatch)
    first, reason = asch.request_audit(asch.TRIGGER_MANUAL)
    assert reason is None
    assert asch.request_audit(asch.TRIGGER_SCHEDULED) == (None,
                                                          asch.REASON_ACTIVE)
    _await_run(first)


# ── (10) usage is MEASURED or ABSENT; no money-shaped keys ──────────────────

def test_usage_is_measured_or_absent(monkeypatch):
    _queue(monkeypatch, [_golden_item(1)])
    measured = _Converse()
    monkeypatch.setattr(audit_core, "_converse_factory", measured)
    record = _run_once(asch.TRIGGER_MANUAL)
    assert record["usage"] == {"input_tokens": 10, "output_tokens": 5}

    job_store.reset_budget_claims()
    unmeasured = _Converse(usage=False)
    monkeypatch.setattr(audit_core, "_converse_factory", unmeasured)
    second = _run_once(asch.TRIGGER_SCHEDULED)
    assert "usage" not in second                      # ABSENT, never zero


def _leaf_keys(node, out: set) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            out.add(str(key))
            _leaf_keys(value, out)
    elif isinstance(node, list):
        for value in node:
            _leaf_keys(value, out)


def test_no_money_shaped_key_reaches_a_record(monkeypatch, converse):
    _queue(monkeypatch, [_golden_item(1)])
    record = _run_once(asch.TRIGGER_MANUAL)
    keys: set = set()
    _leaf_keys(record, keys)
    _leaf_keys(asch.audit_status(), keys)
    _leaf_keys(ef.list_findings(), keys)
    for key in keys:
        lowered = key.lower()
        for banned in ("dollar", "usd", "price", "cost", "estimate"):
            assert banned not in lowered, key


# ── candidate selection: FLAGGED ONLY ───────────────────────────────────────

def test_only_flagged_undecided_resolvable_items_are_candidates(monkeypatch,
                                                                converse):
    decided = _golden_item(2)
    decided["decision"] = {"verdict": "incorrect"}
    expired = _golden_item(3)
    expired["source_expired"] = True
    _queue(monkeypatch, [_golden_item(1), decided, expired,
                         _feedback_item(4, sentiment="positive"),
                         _feedback_item(5, sentiment="negative")])
    record = _run_once(asch.TRIGGER_MANUAL)
    assert record["counts"]["candidates"] == 2
    audited = {call["question_id"] for call in converse.calls}
    assert audited == {"g-01", f"{5:016x}"}


def test_a_model_failure_is_counted_and_the_run_still_completes(monkeypatch):
    _queue(monkeypatch, [_golden_item(1)])
    monkeypatch.setattr(audit_core, "_converse_factory", _Converse(boom=True))
    record = _run_once(asch.TRIGGER_MANUAL)
    assert record["status"] == asch.STATUS_COMPLETE
    assert record["counts"]["model_errors"] == 1
    assert record["counts"]["audited"] == 0
    assert "usage" not in record


# ── (20) the annotator's OWN ledger writer (ADR-0052 / D10) ────────────────

def test_a_completed_run_fires_the_audit_run_ledger_ref(monkeypatch, converse):
    refs: list = []
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda given, evidence="": refs.extend(list(given))
                        or len(list(given)))
    _queue(monkeypatch, [_golden_item(1)])
    record = _run_once(asch.TRIGGER_MANUAL)
    assert (ef.WRITER_KIND_AUDIT_RUN, record["run_id"]) in refs
    # The ref is the OPAQUE run id — no excerpt, rationale or question text.
    assert all(isinstance(ref, tuple) and len(ref) == 2 for ref in refs)


def test_a_refused_run_fires_no_ledger_ref(monkeypatch, converse):
    refs: list = []
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda given, evidence="": refs.extend(list(given))
                        or len(list(given)))
    _eleven(monkeypatch)
    job_store.claim_budget_slots(asch.BUDGET_KIND, job_store.utc_day_key(),
                                 asch.DAILY_AUDIT_CAP, asch.DAILY_AUDIT_CAP)
    assert asch.request_audit(asch.TRIGGER_SCHEDULED)[0] is None
    assert refs == []


def test_a_telemetry_failure_never_fails_the_run(monkeypatch, converse):
    """The annotator's own bump is best-effort: telemetry is evidence, never a
    dependency. (Only the ``audit-run`` bump is failed here — the real
    ``bump_artifact_usage`` swallows its own backend failures.)"""
    def _boom_on_audit_run(given, evidence=""):
        refs = list(given)
        if any(kind == ef.WRITER_KIND_AUDIT_RUN for kind, _ref in refs):
            raise RuntimeError("ledger down")
        return len(refs)

    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        _boom_on_audit_run)
    _queue(monkeypatch, [_golden_item(1)])
    record = _run_once(asch.TRIGGER_MANUAL)
    assert record["status"] == asch.STATUS_COMPLETE
    assert record["counts"]["findings"] == 1


# ── (6b) sole converse consumer + sole findings writer (call graph) ─────────

def _module_call_graph(path: Path) -> tuple:
    """``({function: {callee names}}, {function: {called attrs}})``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    local: dict = {}
    attrs: dict = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local[node.name] = set()
        attrs[node.name] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Name):
                local[node.name].add(func.id)
            elif isinstance(func, ast.Attribute):
                attrs[node.name].add(func.attr)
                local[node.name].add(func.attr)
    return local, attrs


def _reachable(graph: dict, start: str) -> set:
    seen: set = set()
    stack = [start]
    while stack:
        current = stack.pop()
        for callee in graph.get(current, set()):
            if callee not in seen:
                seen.add(callee)
                stack.append(callee)
    return seen


def test_only_the_worker_reaches_the_converse_seam_and_the_ledger_writer():
    local, attrs = _module_call_graph(AGENTS_DIR / "audit_scheduler.py")
    from_entry = _reachable(local, "request_audit")
    from_worker = _reachable(local, "_run_audit")
    seams = {"audit_bundle", "replace_findings"}
    assert seams & from_entry == set(), from_entry & seams
    assert seams <= from_worker
    # The seam is reached through audit_core, never re-implemented locally.
    assert "audit_bundle" in attrs["_annotate_candidate"]


def test_no_other_module_writes_findings_or_calls_the_converse_seam():
    writers, callers = [], []
    for path in sorted(AGENTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "replace_findings(" in text and path.name != "eval_findings.py":
            writers.append(path.name)
        if "audit_bundle(" in text and path.name != "audit_core.py":
            callers.append(path.name)
    assert writers == ["audit_scheduler.py"]
    assert callers == ["audit_scheduler.py"]


# ── (8) + (16) memory absence is ARCHITECTURAL (import graph) ───────────────

def _module_level_imports(path: Path) -> set:
    """Sibling ``agents`` modules imported at MODULE level by one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            names.update(alias.name for alias in node.names)
            if node.module:
                names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1]
                         for alias in node.names
                         if alias.name.startswith("agents."))
    return {name for name in names if (AGENTS_DIR / f"{name}.py").exists()}


def test_import_graph_of_the_worker_reaches_no_chat_session_or_memory_module():
    seen: set = set()
    stack = ["audit_scheduler", "audit_core"]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        stack.extend(_module_level_imports(AGENTS_DIR / f"{module}.py"))
    for forbidden in FORBIDDEN_MODULES:
        assert forbidden not in seen, f"{forbidden} reachable: {sorted(seen)}"


def test_neither_file_imports_a_memory_path_at_any_depth():
    """Lazy imports count too: a function-level chat/memory import would be a
    memory path the module-level walk cannot see."""
    for name in ("audit_scheduler.py", "audit_core.py"):
        tree = ast.parse((AGENTS_DIR / name).read_text(encoding="utf-8"))
        imported: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
                imported.add((node.module or "").split(".")[-1])
            elif isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[-1]
                                for alias in node.names)
        for forbidden in FORBIDDEN_MODULES:
            assert forbidden not in imported, f"{name} imports {forbidden}"
        assert "write_memory" not in (AGENTS_DIR / name).read_text(
            encoding="utf-8")


# ── (3) the route calls the ONE entry with trigger="manual" ─────────────────

def test_route_starts_a_run_with_the_manual_trigger(monkeypatch):
    seen: list = []
    monkeypatch.setattr(asch, "request_audit",
                        lambda trigger: seen.append(trigger) or ("run-1", None))
    monkeypatch.setattr(asch, "get_run",
                        lambda run_id: {"run_id": run_id,
                                        "status": asch.STATUS_RUNNING})
    response = _client().post(RUNS_URL)
    assert response.status_code == 202
    assert response.json()["run_id"] == "run-1"
    assert seen == [asch.TRIGGER_MANUAL]


def test_route_body_forbids_every_override(monkeypatch, converse):
    _eleven(monkeypatch)
    client = _client()
    for body in ({"count": 3}, {"cap": 50}, {"model": "claude"},
                 {"max_tokens": 9}, {"source": "feedback"},
                 {"trigger": "scheduled"}):
        assert client.post(RUNS_URL, json=body).status_code == 422
    assert converse.count == 0


def test_route_refusals_are_distinct(monkeypatch):
    reasons = iter([asch.REASON_ACTIVE, asch.REASON_BUDGET_EXHAUSTED,
                    asch.REASON_BUDGET_UNAVAILABLE])
    monkeypatch.setattr(asch, "request_audit",
                        lambda trigger: (None, next(reasons)))
    client = _client()
    active = client.post(RUNS_URL)
    exhausted = client.post(RUNS_URL)
    unavailable = client.post(RUNS_URL)
    assert active.status_code == 409
    assert active.json()["detail"] == efr.AUDIT_ACTIVE_DETAIL
    assert exhausted.status_code == 429
    assert exhausted.json()["detail"] == efr.AUDIT_BUDGET_DETAIL
    assert unavailable.status_code == 429
    assert unavailable.json()["detail"] == \
        efr.AUDIT_BUDGET_UNAVAILABLE_DETAIL
    assert efr.AUDIT_BUDGET_DETAIL != efr.AUDIT_BUDGET_UNAVAILABLE_DETAIL


def test_poll_route_returns_the_record_or_404(monkeypatch, converse):
    _queue(monkeypatch, [_golden_item(1)])
    client = _client()
    run_id = client.post(RUNS_URL).json()["run_id"]
    record = _await_run(run_id)
    polled = client.get(f"{RUNS_URL}/{run_id}").json()
    assert polled["status"] == record["status"] == asch.STATUS_COMPLETE
    assert polled["trigger"] == asch.TRIGGER_MANUAL
    assert client.get(f"{RUNS_URL}/nope").status_code == 404


def test_status_route_matches_the_panel_contract(monkeypatch, converse):
    _queue(monkeypatch, [_golden_item(1)])
    monkeypatch.setattr(ef, "writer_telemetry", lambda days=30: {
        "window_days": days,
        "writers": {"eval-finding": {"kind": "eval-finding", "fired": True,
                                     "uses": 12, "refs": []},
                    "audit-run": {"kind": "audit-run", "fired": True,
                                  "uses": 2, "refs": []},
                    "proposed-mapping": {"kind": "proposed-mapping",
                                         "fired": False, "uses": 0,
                                         "refs": []}}})
    client = _client()
    _await_run(client.post(RUNS_URL).json()["run_id"])
    body = client.get(STATUS_URL).json()
    assert body["scheduled_enabled"] is False
    assert body["runs"][0]["trigger"] == asch.TRIGGER_MANUAL
    assert body["budget"] == {"day_key": job_store.utc_day_key(),
                              "claimed": 1,
                              "cap": asch.DAILY_AUDIT_CAP, "durable": False}
    # Folded from the ADR-0052 ledger read path — unfired writers are omitted.
    assert body["telemetry"] == {"eval-finding": 12, "audit-run": 2}


def test_status_reports_the_flag_state(monkeypatch):
    monkeypatch.setenv(asch.SCHEDULED_FLAG, "1")
    assert _client().get(STATUS_URL).json()["scheduled_enabled"] is True


def test_audit_routes_require_admin(monkeypatch, converse):
    _queue(monkeypatch, [_golden_item(1)])
    client = _client(is_admin=False)
    assert client.post(RUNS_URL).status_code == 403
    assert client.get(STATUS_URL).status_code == 403
    assert client.get(f"{RUNS_URL}/anything").status_code == 403
    assert converse.count == 0


def test_status_and_poll_reads_write_nothing(monkeypatch, converse):
    _queue(monkeypatch, [_golden_item(1)])
    client = _client()
    run_id = client.post(RUNS_URL).json()["run_id"]
    _await_run(run_id)
    before = json.dumps(asch._jobs().get(asch._INDEX_KEY), sort_keys=True)
    client.get(STATUS_URL)
    client.get(f"{RUNS_URL}/{run_id}")
    assert json.dumps(asch._jobs().get(asch._INDEX_KEY),
                      sort_keys=True) == before


# ── (1) (2) (7) registration: default OFF, and the SCHEDULED PATH EXECUTED ──

@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off"])
def test_flag_unset_or_falsy_registers_nothing(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(asch.SCHEDULED_FLAG, raising=False)
    else:
        monkeypatch.setenv(asch.SCHEDULED_FLAG, value)
    calls: list = []
    monkeypatch.setattr(server, "add_interval_job",
                        lambda *args, **kwargs: calls.append(args))
    server.register_nightly_audit_job()
    assert calls == []


def test_flag_enabled_registers_the_pinned_job(monkeypatch):
    monkeypatch.setenv(asch.SCHEDULED_FLAG, "true")
    calls: list = []
    monkeypatch.setattr(server, "add_interval_job",
                        lambda job_id, func, seconds:
                        calls.append((job_id, func, seconds)))
    server.register_nightly_audit_job()
    assert len(calls) == 1
    job_id, func, seconds = calls[0]
    assert job_id == "nightly-audit" == asch.SCHEDULED_JOB_ID
    assert seconds == 86400 == asch.SCHEDULED_INTERVAL_SECONDS
    assert func is server._run_scheduled_audit


def test_the_registered_callback_requests_a_scheduled_run(monkeypatch):
    """The scheduled path is EXECUTED here, not inferred from the flag."""
    monkeypatch.setenv(asch.SCHEDULED_FLAG, "1")
    captured: dict = {}
    monkeypatch.setattr(server, "add_interval_job",
                        lambda job_id, func, seconds:
                        captured.update(func=func))
    triggers: list = []
    monkeypatch.setattr(asch, "request_audit",
                        lambda trigger: triggers.append(trigger)
                        or ("run-9", None))
    server.register_nightly_audit_job()
    captured["func"]()                      # simulate ONE scheduler tick
    assert triggers == [asch.TRIGGER_SCHEDULED]


def test_the_callback_never_retries_and_never_raises(monkeypatch):
    refusals: list = []
    monkeypatch.setattr(asch, "request_audit",
                        lambda trigger: refusals.append(trigger)
                        or (None, asch.REASON_BUDGET_EXHAUSTED))
    server._run_scheduled_audit()
    assert refusals == [asch.TRIGGER_SCHEDULED]      # ONE attempt, no retry

    def _boom(trigger):
        raise RuntimeError("store down")

    monkeypatch.setattr(asch, "request_audit", _boom)
    server._run_scheduled_audit()                    # must not raise


def test_registration_rides_the_lifespan_seam_after_the_scheduler_starts(
        monkeypatch):
    """Ordering pin, in two halves.

    BEHAVIOURAL: the annotator registration rides ``register_auto_accept_job``
    — the seam the lifespan calls — and it runs BEFORE that function's own
    SELF_LEARNING_AUTO_ACCEPT gate, so the learning flag can never suppress it.
    SOURCE: that seam is called after ``start_scheduler()``, which is what makes
    every registration land on a live shared scheduler: the lifespan runs the
    step that starts the scheduler before the seam, both at once or both after
    the startup restore (``_AfterRestore``), in that order either way.
    """
    monkeypatch.setenv(asch.SCHEDULED_FLAG, "1")
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    order: list = []
    monkeypatch.setattr(server, "add_interval_job",
                        lambda job_id, func, seconds: order.append(job_id))
    import agents.schema_drift as schema_drift
    monkeypatch.setattr(schema_drift, "register_drift_check_job",
                        lambda: order.append("schema-drift"))

    server.register_auto_accept_job()

    assert order == ["schema-drift", asch.SCHEDULED_JOB_ID]

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "\n    start_scheduler()" in source[source.index("def _start_prompt_scheduler"):
                                         source.index("def _register_runtime_keepwarm")]
    assert source.index("\n    after.run(_start_prompt_scheduler)") < \
        source.index("\n    after.run(register_auto_accept_job)")
    assert source.count("register_nightly_audit_job()") == 2   # def + one call


def test_scheduled_enabled_is_the_one_flag_home():
    """The flag is READ in ONE place — the registration wrapper and the status
    surface both go through ``audit_scheduler.scheduled_enabled``."""
    server_source = Path(server.__file__).read_text(encoding="utf-8")
    assert f'environ.get("{asch.SCHEDULED_FLAG}"' not in server_source
    assert f"environ.get('{asch.SCHEDULED_FLAG}'" not in server_source
    assert "scheduled_enabled()" in server_source
