# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Learning admin router (ADR-0052) — ``/api/admin/learning/*``.

The steward-transparency read surface over the self-learning subsystem:

* ``GET /health`` — the pinned learning-health snapshot consumed by the B1
  audit gate (``scripts/audit_learning.py``): response-integrity mode echo,
  usage-ledger rollup, staging census (reported SEPARATELY from pending —
  ADR-0051 P5), pending-age distribution, and quarantine stats. All stats
  are computed by the shared module ``agents.learning_health`` (the B5
  digest consumes the SAME functions — never duplicated queries).

* ``GET /digest`` — the B5 steward digest: top pending items by CONSEQUENCE
  (the shared Task-2.2 ``proposal_scoring`` scorer), expired-unreviewed
  insight count, top artifacts by 30d usage, quarantine + staging stats.
  On-request with a 60 s in-process cache — no scheduler job (the
  ``prompts.add_interval_job`` seam is noted for a future push model).

* ``POST /tryit`` + ``GET /tryit/{job_id}`` — the B2 try-it validation: an
  async ``JobStore("tryit")`` job (the CloudFront 120 s cap forbids a blocking
  POST) that runs one pending proposal's originating question twice,
  STRICTLY SERIALLY — baseline first, then with the candidate injected into
  the Mode-A route seed — and stores an honest diff of what each run DID.

Auth: the ``/api/admin`` prefix is auto-gated by
``auth.middleware.AuthMiddleware`` (``ADMIN_PREFIXES``) — non-admins get 403
before any handler runs; no per-handler check is needed (the
``vocabulary_routes.py`` posture).

Failure contract: a health read NEVER 500s — every backend failure degrades
to the all-zeros shape (``learning_health.zero_health()``), keeping the
audit gate's fetch layer simple and the admin surface honest-but-alive.

Import purity (ADR 0020/0021): no boto3/pg8000/graph imports at module
import time — ``learning_health`` (which itself lazy-imports its impure
dependencies) is imported inside the handler.
"""
from __future__ import annotations

import asyncio
import logging

from .job_store import JobStore
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/learning")

# In-process digest cache (the ``artifact_usage._read_cache`` idiom):
# {key: (expires_at_monotonic, result)}. Only successful, NON-DEGRADED builds
# are cached — a degraded zero shape (whether raised here or returned by
# ``build_digest``'s own internal swallow) is recomputed on the next request,
# so a transient backend blip never pins zeros for a full cache window.
_DIGEST_CACHE_TTL_SECONDS = 60.0
_digest_cache: dict[str, tuple[float, dict]] = {}
_digest_cache_lock = threading.Lock()


def _reset_digest_cache() -> None:
    """Test seam: clear the in-process digest cache."""
    with _digest_cache_lock:
        _digest_cache.clear()


@router.get("/health")
async def learning_health():
    """The pinned learning-health snapshot (contract in the module docstring
    of ``agents.learning_health``). Blocking DynamoDB reads run off-loop via
    ``asyncio.to_thread``; any failure degrades to zeros, never 500."""
    from . import learning_health as lh
    try:
        return await asyncio.to_thread(lh.build_health)
    except Exception as e:  # noqa: BLE001 - a health read never 500s
        logger.warning(f"learning health endpoint degraded (non-critical): {e}")
        return lh.zero_health()


@router.get("/digest")
async def learning_digest():
    """The pinned B5 steward-digest snapshot (contract in the module
    docstring of ``agents.learning_health`` — computed by the SAME shared
    builders as ``/health``, ranked by the SHARED Task-2.2 consequence
    scorer). Served on request behind a 60 s in-process cache (successful,
    non-degraded builds ONLY — a zero shape is never cached); no scheduler
    job — a future push model would hang off ``prompts.add_interval_job``
    (the single shared AsyncIOScheduler seam). Blocking reads run off-loop;
    any failure degrades to zeros, never 500."""
    from . import learning_health as lh
    now = time.monotonic()
    with _digest_cache_lock:
        cached = _digest_cache.get("digest")
        if cached and cached[0] > now:
            return cached[1]
    try:
        result = await asyncio.to_thread(lh.build_digest)
    except Exception as e:  # noqa: BLE001 - a digest read never 500s
        logger.warning(f"learning digest endpoint degraded (non-critical): {e}")
        return lh.zero_digest()
    if result != lh.zero_digest():  # never cache a degraded/zero shape
        with _digest_cache_lock:
            _digest_cache["digest"] = (now + _DIGEST_CACHE_TTL_SECONDS, result)
    return result


# ─── B2 try-it (ADR-0052 (d)) — async job, twice-run honest diff ────────────
#
# One representative question runs TWICE, STRICTLY SERIALLY (one runtime, one
# stream at a time — concurrent streams on the same runtime cause
# IncompleteRead): a BASELINE run first, then a run whose Mode-A route seed
# carries the candidate artifact (the RESERVED ``tryItCandidate`` payload key,
# popped unconditionally by the runtime). The result is an HONEST diff of what
# each run DID — systems queried, rules applied, translation steps, answer
# excerpt — never a derived score and never the deleted
# ``tool_calls_before``/``tool_calls_after`` fiction (ADR-0052 (d)).
#
# A job takes ~4–5 minutes; CloudFront caps requests at 120 s and the SWR
# loader at 30 s, so this is a ``JobStore("tryit")``-backed background job
# (the C4 ``_RESET_JOBS`` idiom — replica-safe) polled by fast GETs, NEVER a
# blocking POST. Explicit steward action only — nothing triggers it
# automatically. Try-it runs use ``tryit-<jobid>-{a,b}`` session ids — the
# marker ``runtime_app._is_tryit_session`` keys on to keep synthetic runs out
# of the usage ledger — and never write memory.

_TRYIT_ACTIVE_KEY = "active"          # ONE concurrent job app-wide
_TRYIT_STALE_SECONDS = 900            # 15 min: stuck-job takeover threshold
_TRYIT_RUN_CLAMP_SECONDS = 240        # per-run wall clamp
_TRYIT_EXCERPT_CHARS = 400
_TRYIT_QUESTION_MAX_CHARS = 500       # steward-supplied question cap
_TRYIT_MIN_CONTEXT_CHARS = 8          # shorter query_context ⇒ prompt fragment

# Proposal types with a route-seed shape (mirrors orchestrator._CANDIDATE_SEED_KINDS).
_TRYIT_INJECTABLE_TYPES = frozenset({"join_path", "value_mapping", "identity_rule"})

# Bounded proposal-row subset forwarded as the candidate. Row-level values
# (from_value/to_value) only for a value mapping, whose one proposed pair is
# what it maps: the route seed serves a value mapping only with its values.
_TRYIT_CANDIDATE_FIELDS = ("proposed_type", "from_system", "from_field",
                           "to_system", "to_field", "concept", "transform",
                           "confidence")
_TRYIT_VALUE_FIELDS = ("from_value", "to_value")

_TRYIT_JOBS = None  # lazy JobStore("tryit") — keeps module import pure


def _tryit_jobs():
    """The shared try-it job registry (lazy — ``job_store`` transitively
    imports boto3-importing modules). Tests inject ``JobStore(..., table=None)``
    via the module attribute."""
    global _TRYIT_JOBS
    if _TRYIT_JOBS is None:
        from .job_store import JobStore
        _TRYIT_JOBS = JobStore("tryit")
    return _TRYIT_JOBS


def _reset_tryit_state() -> None:
    """Test seam: drop the job registry (a fresh in-memory store per test)."""
    global _TRYIT_JOBS
    _TRYIT_JOBS = None


def _find_mapping(mapping_id: str) -> "dict | None":
    """The proposal row for ``mapping_id`` (best-effort; None when absent)."""
    if not mapping_id:
        return None
    from .self_learning import get_all_mappings
    for row in get_all_mappings():
        if str(row.get("mapping_id") or "") == mapping_id:
            return row
    return None


def _validate_tryit_mapping(row: "dict | None") -> "tuple[int, str] | None":
    """Pinned POST validation: ``(status_code, honest_copy)`` or None (valid)."""
    if row is None:
        return 404, "unknown mapping"
    if str(row.get("status") or "") != "pending":
        return 400, "only pending proposals can be tried"
    if str(row.get("proposed_type") or "") not in _TRYIT_INJECTABLE_TYPES:
        return 400, ("this proposal type has no route-seed shape and cannot "
                     "be injected for a try-it run")
    return None


def _resolve_tryit_question(
        body: dict, row: dict) -> "tuple[str | None, tuple[int, str] | None]":
    """The question BOTH runs will replay: ``(question, None)`` or
    ``(None, (status, honest_copy))``.

    An explicit steward-supplied ``question`` (stripped, capped at
    ``_TRYIT_QUESTION_MAX_CHARS``) overrides the proposal's ``query_context``
    for BOTH runs. Absent one, the row's ``query_context`` is used ONLY when
    usable — many proposals carry a prompt FRAGMENT there (observed live: the
    baseline answered "I need to understand what question you're asking…"),
    so empty/whitespace or shorter than ``_TRYIT_MIN_CONTEXT_CHARS`` chars
    means the steward must supply one."""
    supplied = (body or {}).get("question")
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()[:_TRYIT_QUESTION_MAX_CHARS], None
    fallback = str(row.get("query_context") or "").strip()
    if len(fallback) < _TRYIT_MIN_CONTEXT_CHARS:
        return None, (400, "proposal has no usable test question — supply one")
    return fallback, None


def _claim_tryit_job(question: str = "") -> "str | None":
    """Check-and-set the single app-wide try-it slot (staleness takeover).

    Returns a fresh job id, or None when a non-stale job is already running
    (the caller answers 409). A ``running`` claim older than
    ``_TRYIT_STALE_SECONDS`` is treated as abandoned (crashed worker/replica)
    and taken over — the ``_RESET_JOBS`` posture. The question the job will
    ask rides the running payload so a poll can show what was asked.
    """
    import uuid
    jobs = _tryit_jobs()
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    # ATOMIC claim (JobStore.claim_slot) with stale takeover; the release in
    # the worker's finally is exact-owner.
    if not jobs.claim_slot(_TRYIT_ACTIVE_KEY,
                           {"status": "running", "job_id": job_id,
                            "started_at": now},
                           stale_seconds=_TRYIT_STALE_SECONDS, now=now):
        return None
    jobs[job_id] = {"status": "running", "started_at": now,
                    "question": question}
    return job_id


def _candidate_from_mapping(row: dict) -> dict:
    """Bounded candidate payload from a proposal row (allowlisted, capped
    strings; row-level values only for a value mapping's one pair)."""
    fields = _TRYIT_CANDIDATE_FIELDS
    if row.get("proposed_type") == "value_mapping":
        fields += _TRYIT_VALUE_FIELDS
    candidate = {key: str(row.get(key))[:256]
                 for key in fields if row.get(key)}
    candidate["mapping_id"] = str(row.get("mapping_id") or "")[:64]
    time_key = row.get("time_key")
    if isinstance(time_key, dict):
        candidate["time_key"] = time_key
    return candidate


def _tryit_stream(question: str, session_id: str, candidate: "dict | None"):
    """One run's event stream via the internal client — the ``/api/ask/sync``
    branch structure: AgentCore runtime when configured, else the local
    integrity-aware client. Returns ``(stream, injected)`` — ``injected`` is
    True only when the candidate is actually forwarded to the runtime prelude
    (the ``extra_payload`` seam). Only the runtime path can inject (the route
    seed is Mode A's runtime prelude); the local path NEVER injects, so a
    local run honestly reports ``injected=False``. ``write_memory=False``
    always (synthetic flow)."""
    from .config import get_config
    if get_config().has_runtime:
        from .chat_client import AgentCoreChatClient
        extra = {"tryItCandidate": candidate} if candidate else None
        stream = AgentCoreChatClient().stream(
            prompt=question, session_id=session_id, user_id="tryit",
            write_memory=False, extra_payload=extra)
        return stream, candidate is not None
    from .chat_client import LocalChatClient
    return LocalChatClient().stream(
        prompt=question, session_id=session_id, user_id="tryit",
        write_memory=False), False


def _systems_from_tool_names(tool_names) -> "list[str]":
    """PURE: distinct owning-system ids from gateway-prefixed tool names.
    Canonical home is ``metrics_registry.systems_from_tool_names`` (public —
    cross-module consumers import it from there); this alias keeps the
    module-internal call sites and their pins unchanged."""
    from .metrics_registry import systems_from_tool_names
    return systems_from_tool_names(tool_names)


def _translation_steps(summary: dict) -> "list[str]":
    """PURE: honest routing/translation context from the run's intelligence
    summary (``planned_with`` — the Mode-A route-seed digest). Reports what
    the routing seed CONTAINED, never a fabricated step trace."""
    planned = summary.get("planned_with") or {}
    steps: list[str] = []
    concepts = [c for c in planned.get("shared_concepts") or []
                if isinstance(c, str) and c]
    if concepts:
        steps.append("shared concepts in routing seed: " + ", ".join(concepts))
    for kind in ("join_paths", "value_mappings", "identity_rules"):
        count = planned.get(kind)
        if isinstance(count, int) and count > 0:
            steps.append(f"{kind.replace('_', ' ')} in routing seed: {count}")
    return steps


def _tryit_single_run(question: str, session_id: str,
                      candidate: "dict | None") -> dict:
    """Run the question once and parse the stream into the pinned per-run
    shape (``injected`` = the candidate was forwarded AND the runtime's
    routing summary says it is in the route seed this run used —
    ``planned_with.tryit_candidate``; always False for baseline and
    local/Mode-B runs). A candidate forwarded but not held adds the private
    ``_forwarded`` flag, which the job pops before storing. Raises on an
    errored or over-clamp run (the job stores the error honestly — never a
    fabricated diff)."""
    from .events import ErrorEvent, MessageEvent, MetadataEvent, ToolUseEvent
    started = time.monotonic()
    tool_names: list[str] = []
    answer_parts: list[str] = []
    summary: dict = {}
    stream, injected = _tryit_stream(question, session_id, candidate)
    try:
        for event in stream:
            if time.monotonic() - started > _TRYIT_RUN_CLAMP_SECONDS:
                raise TimeoutError(
                    f"try-it run exceeded {_TRYIT_RUN_CLAMP_SECONDS}s wall clamp")
            if isinstance(event, ErrorEvent):
                raise RuntimeError("try-it run failed")
            if isinstance(event, ToolUseEvent):
                tool_names.append(event.tool_name)
            elif isinstance(event, MessageEvent):
                answer_parts.append(event.content)
            elif isinstance(event, MetadataEvent):
                block = (event.data or {}).get("intelligence_summary")
                if isinstance(block, dict):
                    summary = block
    finally:
        getattr(stream, "close", lambda: None)()
    rules = [{"id": str(r.get("id") or ""), "label": str(r.get("label") or "")}
             for r in summary.get("rules_applied") or [] if isinstance(r, dict)]
    planned = summary.get("planned_with")
    # Forwarded is not injected: the runtime reports whether the candidate is
    # in the route seed this run used (it can be trimmed to fit the budget).
    retained = isinstance(planned, dict) and planned.get("tryit_candidate") is True
    return {"systems_queried": _systems_from_tool_names(tool_names),
            "rules_applied": rules,
            "translation_steps": _translation_steps(summary),
            "answer_excerpt": "".join(answer_parts)[:_TRYIT_EXCERPT_CHARS],
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "injected": injected and retained,
            **({"_forwarded": True} if injected and not retained else {})}


def _tryit_diff(baseline: dict, candidate: dict, *, forwarded: bool = False) -> dict:
    """PURE: honest run-to-run diff — what changed between what each run DID.

    Pinned shape (X-1 contract — the frontend renders these verbatim):
    ``systems_added``/``systems_removed`` are PLAIN STRING arrays;
    ``rules_added``/``rules_removed`` are ARRAYS OF OBJECTS
    ``[{"id", "label"}]`` — never flattened to strings. When the candidate
    run reports ``injected=False`` the diff gains an additive ``note`` so an
    empty diff is never mistaken for "the candidate changed nothing":
    ``"candidate not injectable in this environment"``, or, when it was
    forwarded but the run's route seed did not hold it (``forwarded``),
    ``"candidate did not fit this run's routing context"``.
    No derived scores; the deleted tool_calls_before/after fiction
    must never return."""
    base_systems = set(baseline.get("systems_queried") or [])
    cand_systems = set(candidate.get("systems_queried") or [])
    base_rules = {r["id"]: r for r in baseline.get("rules_applied") or []}
    cand_rules = {r["id"]: r for r in candidate.get("rules_applied") or []}
    diff = {
        "systems_added": sorted(cand_systems - base_systems),
        "systems_removed": sorted(base_systems - cand_systems),
        "rules_added": [cand_rules[rid]
                        for rid in sorted(set(cand_rules) - set(base_rules))],
        "rules_removed": [base_rules[rid]
                          for rid in sorted(set(base_rules) - set(cand_rules))],
    }
    if not candidate.get("injected"):
        diff["note"] = ("candidate did not fit this run's routing context" if forwarded
                        else "candidate not injectable in this environment")
    return diff


def _run_tryit_job(job_id: str, row: dict, question: str) -> None:
    """Worker: baseline run FIRST, then the candidate run — STRICTLY SERIAL
    (one runtime, one stream at a time; parallel streams on the same runtime
    cause IncompleteRead). ``question`` is the resolved test question
    (steward override or the row's usable query_context) and BOTH runs ask
    it verbatim; every terminal job payload echoes it so GET always shows
    what was asked. Errors are stored honestly (status ``error``,
    ``error="tryit_failed"`` sentinel + ``error_detail`` = one sanitized
    line "<which run> failed: <ExceptionType>" — exception TYPE only, never
    the message/stack, which could carry internals or credentials); the
    single app-wide slot is always released."""
    jobs = _tryit_jobs()
    phase = "baseline run"
    try:
        baseline = _tryit_single_run(question, f"tryit-{job_id}-a", None)
        phase = "candidate run"
        candidate_run = _tryit_single_run(
            question, f"tryit-{job_id}-b", _candidate_from_mapping(row))
        forwarded = bool(candidate_run.pop("_forwarded", False))
        jobs[job_id] = {"status": "done", "question": question, "result": {
            "question": question,
            "baseline": baseline,
            "candidate": candidate_run,
            "candidate_injected": bool(candidate_run.get("injected")),
            "diff": _tryit_diff(baseline, candidate_run, forwarded=forwarded),
        }}
    except Exception as exc:  # noqa: BLE001 - stored honestly, surfaced by the poll
        logger.exception("try-it job %s failed", job_id)
        jobs[job_id] = {"status": "error", "error": "tryit_failed",
                        "question": question,
                        "error_detail": f"{phase} failed: {type(exc).__name__}"}
    finally:
        jobs.release_slot(_TRYIT_ACTIVE_KEY, job_id,
                          {"status": "idle", "job_id": job_id,
                           "finished_at": time.time()})


@router.post("/tryit", status_code=202)
async def tryit_start(body: dict):
    """Start a try-it job for one pending proposal (explicit steward action —
    NEVER triggered automatically). An optional ``question`` (str, stripped,
    ≤500 chars) overrides the proposal's ``query_context`` as the test
    question for BOTH runs; absent one, an unusable ``query_context``
    (empty/whitespace or <8 chars — prompt fragments) is a 400. 202 + job id
    on accept; 409 when a job is already running (ONE concurrent job
    app-wide); 404/400 per validation."""
    from fastapi.responses import JSONResponse
    mapping_id = str((body or {}).get("mapping_id") or "")
    row = await asyncio.to_thread(_find_mapping, mapping_id)
    invalid = _validate_tryit_mapping(row)
    if invalid:
        return JSONResponse(status_code=invalid[0], content={"error": invalid[1]})
    question, invalid = _resolve_tryit_question(body, row)
    if invalid:
        return JSONResponse(status_code=invalid[0], content={"error": invalid[1]})
    job_id = await asyncio.to_thread(_claim_tryit_job, question)
    if job_id is None:
        return JSONResponse(status_code=409,
                            content={"error": "try-it already running"})
    threading.Thread(target=_run_tryit_job, args=(job_id, row, question),
                     daemon=True).start()
    return {"job_id": job_id}


@router.get("/tryit/{job_id}")
async def tryit_status(job_id: str):
    """Fast poll for a try-it job. Replica-safe via the JobStore
    write-through. Pinned job shapes (additive contract — the frontend
    ``TryItPanel`` types mirror this exactly):

    * running: ``{"status": "running", "started_at": <epoch>, "question":
      <the resolved test question — steward override or query_context>}``
    * done: ``{"status": "done", "question": <as above>, "result":
      {question, baseline, candidate,
      candidate_injected, diff}}`` where each run (``baseline`` /
      ``candidate``) is ``{systems_queried: [str], rules_applied:
      [{"id": str, "label": str}], translation_steps: [str],
      answer_excerpt: str, elapsed_seconds: float, injected: bool}``;
      ``candidate_injected`` echoes the candidate run's ``injected``; and
      ``diff`` is ``{systems_added: [str], systems_removed: [str],
      rules_added: [{"id", "label"}], rules_removed: [{"id", "label"}]}``
      — rules stay ARRAYS OF OBJECTS, systems stay plain string arrays —
      plus ``note`` ("candidate not injectable in this environment") when
      ``candidate_injected`` is false.
    * error: ``{"status": "error", "question": <as above>, "error":
      "tryit_failed", "error_detail":
      "<which run> failed: <ExceptionType>"}`` — sanitized, one line, no
      internals/stack/credentials.
    """
    from fastapi.responses import JSONResponse
    if job_id == _TRYIT_ACTIVE_KEY:
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    job = await asyncio.to_thread(_tryit_jobs().get, job_id)
    if not isinstance(job, dict):
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    return job


# ─── E5 schema drift check (ADR-0053) — async job, serial per-system runs ───
#
# Re-discovering a large source can exceed the CloudFront 120 s origin cap, so
# the drift check is a ``JobStore("drift")``-backed background job (the try-it
# idiom above) polled by fast GETs, never a blocking POST. ONE concurrent job
# app-wide (a drift sweep already covers every onboarded system); systems run
# STRICTLY SERIALLY inside the worker. All drift semantics live in
# ``agents.schema_drift`` — this block is transport only.

_DRIFT_ACTIVE_KEY = "active"          # ONE concurrent drift job app-wide
_DRIFT_STALE_SECONDS = 900            # 15 min: stuck-job takeover threshold

_DRIFT_JOBS = None  # lazy JobStore("drift") — keeps module import pure


def _drift_jobs():
    """The shared drift job registry (lazy — the try-it posture). Tests
    inject ``JobStore(..., table=None)`` via the module attribute."""
    global _DRIFT_JOBS
    if _DRIFT_JOBS is None:
        from .job_store import JobStore
        _DRIFT_JOBS = JobStore("drift")
    return _DRIFT_JOBS


def _reset_drift_state() -> None:
    """Test seam: drop the job registry (a fresh in-memory store per test)."""
    global _DRIFT_JOBS
    _DRIFT_JOBS = None


def _claim_drift_job(system_ids: list) -> "str | None":
    """Check-and-set the single app-wide drift slot (staleness takeover —
    the ``_claim_tryit_job`` posture). Returns a fresh job id, or None when
    a non-stale drift job is already running (caller answers 409)."""
    import uuid
    jobs = _drift_jobs()
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    if not jobs.claim_slot(_DRIFT_ACTIVE_KEY,
                           {"status": "running", "job_id": job_id,
                            "started_at": now},
                           stale_seconds=_DRIFT_STALE_SECONDS, now=now):
        return None
    jobs[job_id] = {"status": "running", "started_at": now,
                    "system_ids": list(system_ids)}
    return job_id


def _run_drift_job(job_id: str, system_ids: list) -> None:
    """Worker: run each system's drift check STRICTLY SERIALLY and store the
    pinned result. Per-system failures are already honest skips inside
    ``run_drift_check``; a wholesale failure stores a sanitized error
    (exception CLASS only) on ``error_detail`` — the poll GET surfaces it as
    the contract's ``error`` (``JobStore`` stamps its own coarse sentinel on
    the ``error`` attr). The app-wide slot is always released."""
    jobs = _drift_jobs()
    started = time.monotonic()
    try:
        from . import schema_drift
        results = [schema_drift.run_drift_check(sid) for sid in system_ids]
        jobs[job_id] = {"status": "done", "result": {
            "systems": results,
            "elapsed_seconds": round(time.monotonic() - started, 1)}}
    except Exception as exc:  # noqa: BLE001 - stored honestly, surfaced by the poll
        logger.exception("drift-check job %s failed", job_id)
        jobs[job_id] = {"status": "error",
                        "error_detail": f"drift check failed: {type(exc).__name__}"}
    finally:
        jobs.release_slot(_DRIFT_ACTIVE_KEY, job_id,
                          {"status": "idle", "job_id": job_id,
                           "finished_at": time.time()})


@router.post("/drift-check", status_code=202)
async def drift_check_start(body: "dict | None" = None):
    """Start a schema-drift check job (ADR-0053 E5). Body
    ``{"system_id": "<id>"}`` targets one system; ``{}`` (or no body) sweeps
    ALL onboarded systems. 202 + job id on accept; 409 when a drift job is
    already running (ONE concurrent job app-wide). A system that cannot be
    read reports an honest per-system ``skipped:<reason>`` in the result —
    never a fabricated empty diff."""
    from fastapi.responses import JSONResponse
    from . import schema_drift
    system_id = str((body or {}).get("system_id") or "").strip()
    system_ids = ([system_id] if system_id
                  else await asyncio.to_thread(schema_drift.onboarded_system_ids))
    job_id = await asyncio.to_thread(_claim_drift_job, system_ids)
    if job_id is None:
        return JSONResponse(status_code=409,
                            content={"error": "drift check already running"})
    threading.Thread(target=_run_drift_job, args=(job_id, system_ids),
                     daemon=True).start()
    return {"job_id": job_id}


@router.get("/drift-check/{job_id}")
async def drift_check_status(job_id: str):
    """Fast poll for a drift-check job. Pinned job shapes (plan E5 contract):

    * running: ``{"status": "running", "started_at": <epoch>,
      "system_ids": [str]}``
    * done: ``{"status": "done", "result": {"systems": [{"system_id",
      "status": "ok"|"skipped:<reason>", "drifts_found", "proposals_filed",
      "kinds": {<kind>: n}}], "elapsed_seconds": float}}``
    * error: ``{"status": "error", "error": "drift check failed:
      <ExceptionType>"}`` — sanitized, one line, class only.
    """
    from fastapi.responses import JSONResponse
    if job_id == _DRIFT_ACTIVE_KEY:
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    job = await asyncio.to_thread(_drift_jobs().get, job_id)
    if not isinstance(job, dict):
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    if job.get("status") == "error" and job.get("error_detail"):
        job = {**job, "error": job["error_detail"]}
    return job


# ─── Coverage-gap investigate (steward-gated) — same job idiom, one shot ────
#
# "Is this concept unmapped because the data doesn't exist, or because nobody
# mapped it?" — the steward-facing answer to that question. Reuses the SAME
# JobStore/poll idiom as the drift check (CloudFront's 120 s origin cap rules
# out a blocking POST once a Neptune schema read is involved). DIFFERENT
# scopes may run concurrently (a steward clicking "Investigate" on two
# different cards must not 409 each other) — but a duplicate request for the
# SAME scope while one is in flight COALESCES onto the running job (returns
# the same job id) instead of racing a second identical sweep against the
# same filing/supersede pass. All analysis semantics live in
# ``agents.coverage_gap_analyzer`` — this block is transport only.

_COVERAGE_GAP_JOBS = None  # lazy JobStore("coverage-gap") — keeps import pure
_COVERAGE_GAP_STALE_SECONDS = 300  # bounded cached-schema pass — 5 min is generous


def _coverage_gap_jobs():
    global _COVERAGE_GAP_JOBS
    if _COVERAGE_GAP_JOBS is None:
        from .job_store import JobStore
        _COVERAGE_GAP_JOBS = JobStore("coverage-gap")
    return _COVERAGE_GAP_JOBS


def _reset_coverage_gap_state() -> None:
    """Test seam: drop the job registry (a fresh in-memory store per test)."""
    global _COVERAGE_GAP_JOBS
    _COVERAGE_GAP_JOBS = None


def _coverage_gap_scope_key(system_ids: "list[str] | None") -> str:
    """The per-scope active-slot key (the ``_claim_drift_job`` posture, one
    slot PER SCOPE instead of app-wide)."""
    return "active:" + (",".join(sorted(system_ids)) if system_ids else "all")


def _claim_coverage_gap_job(system_ids: "list[str] | None") -> "tuple[str, bool]":
    """Check-and-set the per-scope slot (staleness takeover). Returns
    ``(job_id, coalesced)`` — when a non-stale job for the SAME scope is
    already running, the running job's id comes back with ``coalesced=True``
    and no new worker is spawned."""
    import uuid
    jobs = _coverage_gap_jobs()
    scope_key = _coverage_gap_scope_key(system_ids)
    active = jobs.get(scope_key)
    if isinstance(active, dict) and active.get("status") == "running":
        try:
            started = float(active.get("started_at") or 0.0)
        except (TypeError, ValueError):
            started = 0.0
        if time.time() - started < _COVERAGE_GAP_STALE_SECONDS and active.get("job_id"):
            return str(active["job_id"]), True
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    jobs[scope_key] = {"status": "running", "job_id": job_id, "started_at": now}
    jobs[job_id] = {"status": "running", "started_at": now,
                    "system_ids": system_ids}
    return job_id, False


def _run_coverage_gap_job(job_id: str, system_ids: "list[str] | None") -> None:
    """Worker: run the coverage-gap check and store the pinned result.
    ``run_coverage_gap_check`` already degrades to an honest all-zero dict on
    any internal failure, so only a truly unexpected exception reaches the
    ``error`` branch here (sanitized, exception class only). The per-scope
    slot is always released."""
    jobs = _coverage_gap_jobs()
    started = time.monotonic()
    try:
        from . import coverage_gap_analyzer
        result = coverage_gap_analyzer.run_coverage_gap_check(system_ids)
        jobs[job_id] = {"status": "done", "result": {
            **result, "elapsed_seconds": round(time.monotonic() - started, 1)}}
    except Exception as exc:  # noqa: BLE001 - stored honestly, surfaced by the poll
        logger.exception("coverage-gap job %s failed", job_id)
        jobs[job_id] = {"status": "error",
                        "error_detail": f"coverage-gap check failed: {type(exc).__name__}"}
    finally:
        jobs[_coverage_gap_scope_key(system_ids)] = {
            "status": "idle", "job_id": job_id, "finished_at": time.time()}


@router.post("/coverage-gap-check", status_code=202)
async def coverage_gap_check_start(body: "dict | None" = None):
    """Start a coverage-gap check job. Body ``{"system_id": "<id>"}`` scopes to
    one system (the UI's per-card "Investigate" action); ``{}`` (or no body)
    checks every onboarded system. 202 + job id — concurrent jobs for
    DIFFERENT scopes are allowed; a duplicate SAME-scope request while one is
    running coalesces onto the in-flight job (same 202 shape, the running
    job's id — see module note above)."""
    system_id = str((body or {}).get("system_id") or "").strip()
    system_ids = [system_id] if system_id else None
    job_id, coalesced = await asyncio.to_thread(
        _claim_coverage_gap_job, system_ids)
    if not coalesced:
        threading.Thread(target=_run_coverage_gap_job, args=(job_id, system_ids),
                         daemon=True).start()
    return {"job_id": job_id}


@router.get("/coverage-gap-check/{job_id}")
async def coverage_gap_check_status(job_id: str):
    """Fast poll for a coverage-gap check job.

    * running: ``{"status": "running", "started_at": <epoch>,
      "system_ids": [str] | null}``
    * done: ``{"status": "done", "result": {"gaps_checked", "systems_checked",
      "findings", "filed", "elapsed_seconds"}}``
    * error: ``{"status": "error", "error": "coverage-gap check failed:
      <ExceptionType>"}`` — sanitized, one line, class only.
    """
    from fastapi.responses import JSONResponse
    if job_id.startswith("active:"):  # per-scope slot keys are not jobs
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    job = await asyncio.to_thread(_coverage_gap_jobs().get, job_id)
    if not isinstance(job, dict):
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    if job.get("status") == "error" and job.get("error_detail"):
        job = {**job, "error": job["error_detail"]}
    return job


# ─── ADR-0058 D1 — on-demand cross-system mining (steward-gated) ────────────
#
# The second half of the two-trigger contract: mining fires automatically
# after every provision (``onboard._run_provision_job``), and HERE on explicit
# steward demand — a named system pair, or all onboarded pairs. Idempotent
# (canonical-key dedup collapses re-runs to dup_pending) and bounded
# (``MINER_MAX_FIELD_PAIRS`` / ``CROSSWALK_MAX_CANDIDATES`` in the miner
# core). All mining semantics live in ``agents.join_discovery`` — this block
# is transport plus the steward gate. Telemetry: the miner core bumps the
# usage ledger ITSELF on file (ADR-0052) — no bump here (double-count guard).


def _check_steward(request: Request) -> None:
    """Steward gate (ADR-0050 — the ``server._check_steward`` shape, local
    because importing ``agents.server`` here would be circular: server mounts
    this router at import time). FAILS CLOSED: an ABSENT ``is_admin``
    attribute (no auth verdict at all) is denied exactly like an explicit
    non-admin verdict. On the real app the ``/api/admin`` prefix middleware
    already 403s non-admins before any handler runs — this dependency makes
    the mining trigger's gate pinnable offline and belt-and-suspenders in
    production."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403,
                            detail="Steward (admin) role required")


def _run_mining(system_a: str, system_b: str) -> dict:
    """Run join discovery + the value-crosswalk miner for one named pair
    (both discovery directions) or, when both ids are empty, ALL onboarded
    pairs. One value-mining pass per UNORDERED pair is complete — emit-side
    canonicalization (``join_discovery._canonical_direction``) makes A→B and
    B→A the same proposal. Returns the miner's outcome report:
    ``{"joins", "crosswalks", "value_crosswalks": [ids], "field_pairs": N,
    "truncated": bool}``."""
    from . import join_discovery as jd
    report: dict = {"joins": [], "crosswalks": [], "value_crosswalks": [],
                    "field_pairs": 0, "truncated": False}
    if system_a:
        for a, b in ((system_a, system_b), (system_b, system_a)):
            d = jd.discover_pair(a, b)
            report["joins"].extend(d.get("joins", []))
            report["crosswalks"].extend(d.get("crosswalks", []))
        value_pairs = [(system_a, system_b)]
    else:
        d = jd.discover_all_joins()
        report["joins"].extend(d.get("joins", []))
        report["crosswalks"].extend(d.get("crosswalks", []))
        systems = jd._onboarded_systems()
        value_pairs = [(a, b) for i, a in enumerate(systems)
                       for b in systems[i + 1:]]
    for a, b in value_pairs:
        mined = jd.mine_value_crosswalks(a, b)
        report["value_crosswalks"].extend(mined.get("crosswalks", []))
        report["field_pairs"] += int(mined.get("field_pairs", 0))
        report["truncated"] = report["truncated"] or bool(mined.get("truncated"))
    return report


#: Single-slot mining job (one mining run at a time — it samples via the
#: system agents and a wide all-pairs sweep runs minutes). Same JobStore
#: machinery as the graph-snapshot import/repair jobs.
_MINING_JOBS = JobStore("crosswalk-mining")
_MINING_JOB_KEY = "global"


async def _run_mining_job(system_a: str, system_b: str) -> None:
    """Background wrapper — records progress, never raises."""
    try:
        report = await asyncio.to_thread(_run_mining, system_a, system_b)
        _MINING_JOBS[_MINING_JOB_KEY] = {"state": "done", "result": report}
    except Exception as exc:  # noqa: BLE001 - sanitized, class only
        logger.warning("mine-crosswalks run failed: %s", type(exc).__name__)
        _MINING_JOBS[_MINING_JOB_KEY] = {
            "state": "error", "error": f"mining failed: {type(exc).__name__}"}


@router.post("/mine-crosswalks", dependencies=[Depends(_check_steward)])
async def mine_crosswalks(body: "dict | None" = None):
    """On-demand cross-system mining (ADR-0058 D1 — explicit steward action).

    Body ``{"system_a": "<id>", "system_b": "<id>"}`` mines one named pair;
    ``{}`` (or no body) mines every onboarded pair. Runs as a BACKGROUND job
    polled at ``GET /mine-crosswalks/status`` — live-found 2026-08-07: even a
    single-pair run (agent-mediated sampling) blew the CloudFront 120 s
    origin timeout as a blocking POST; same fix as the reset/import jobs.
    400 on a half-named or self pair."""
    from fastapi.responses import JSONResponse
    system_a = str((body or {}).get("system_a") or "").strip()
    system_b = str((body or {}).get("system_b") or "").strip()
    if bool(system_a) != bool(system_b):
        return JSONResponse(status_code=400, content={
            "error": "provide both system_a and system_b, or neither"})
    if system_a and system_a == system_b:
        return JSONResponse(status_code=400, content={
            "error": "system_a and system_b must differ"})
    existing = _MINING_JOBS.get(_MINING_JOB_KEY)
    if existing and existing.get("state") == "running":
        return {"status": "already-running"}
    _MINING_JOBS[_MINING_JOB_KEY] = {"state": "running"}
    asyncio.get_event_loop().create_task(_run_mining_job(system_a, system_b))
    return {"status": "started"}


@router.get("/mine-crosswalks/status", dependencies=[Depends(_check_steward)])
async def mine_crosswalks_status():
    """Poll the mining job: none | running | done | error."""
    job = _MINING_JOBS.get(_MINING_JOB_KEY)
    if not job:
        return {"state": "none"}
    return dict(job)


# ─── ADR-0063 cross-system context placement (steward-gated) ────────────────
# Transport only: deterministic detection and proposal semantics live in
# ``agents.context_placement``. Different focus scopes may run concurrently;
# duplicate in-flight scopes coalesce onto one shared JobStore slot.

_CONTEXT_PLACEMENT_JOBS = None
_CONTEXT_PLACEMENT_STALE_SECONDS = 300


def _context_placement_jobs():
    global _CONTEXT_PLACEMENT_JOBS
    if _CONTEXT_PLACEMENT_JOBS is None:
        from .job_store import JobStore
        _CONTEXT_PLACEMENT_JOBS = JobStore("context-placement")
    return _CONTEXT_PLACEMENT_JOBS


def _reset_context_placement_state() -> None:
    """Test seam: drop the context-placement job registry."""
    global _CONTEXT_PLACEMENT_JOBS
    _CONTEXT_PLACEMENT_JOBS = None


def _context_placement_scope_key(system_ids: "list[str] | None") -> str:
    return "active:" + (",".join(sorted(system_ids)) if system_ids else "all")


def _claim_context_placement_job(system_ids: "list[str] | None") -> "tuple[str, bool]":
    import uuid
    jobs = _context_placement_jobs()
    scope_key = _context_placement_scope_key(system_ids)
    active = jobs.get(scope_key)
    if isinstance(active, dict) and active.get("status") == "running":
        try:
            started = float(active.get("started_at") or 0.0)
        except (TypeError, ValueError):
            started = 0.0
        if (time.time() - started < _CONTEXT_PLACEMENT_STALE_SECONDS
                and active.get("job_id")):
            return str(active["job_id"]), True
    job_id, now = uuid.uuid4().hex[:16], time.time()
    jobs[scope_key] = {"status": "running", "job_id": job_id,
                       "started_at": now}
    jobs[job_id] = {"status": "running", "started_at": now,
                    "system_ids": system_ids}
    return job_id, False


def _run_context_placement_job(job_id: str,
                               system_ids: "list[str] | None") -> None:
    jobs, started = _context_placement_jobs(), time.monotonic()
    try:
        from .context_placement import run_context_placement_check
        result = run_context_placement_check(system_ids)
        jobs[job_id] = {"status": "done", "result": {
            **result, "elapsed_seconds": round(time.monotonic() - started, 1)}}
    except Exception as exc:  # noqa: BLE001 - sanitized poll result
        logger.exception("context-placement job %s failed", job_id)
        jobs[job_id] = {"status": "error", "error_detail":
                        f"context-placement check failed: {type(exc).__name__}"}
    finally:
        jobs[_context_placement_scope_key(system_ids)] = {
            "status": "idle", "job_id": job_id, "finished_at": time.time()}


@router.post("/context-placement-check", status_code=202,
             dependencies=[Depends(_check_steward)])
async def context_placement_check_start(body: "dict | None" = None):
    """Start a bounded placement check; an optional system id focuses pairs."""
    system_id = str((body or {}).get("system_id") or "").strip()
    system_ids = [system_id] if system_id else None
    job_id, coalesced = await asyncio.to_thread(
        _claim_context_placement_job, system_ids)
    if not coalesced:
        threading.Thread(target=_run_context_placement_job,
                         args=(job_id, system_ids), daemon=True).start()
    return {"job_id": job_id}


@router.get("/context-placement-check/{job_id}",
            dependencies=[Depends(_check_steward)])
async def context_placement_check_status(job_id: str):
    """Poll a placement check without exposing internal exception messages."""
    from fastapi.responses import JSONResponse
    if job_id.startswith("active:"):
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    job = await asyncio.to_thread(_context_placement_jobs().get, job_id)
    if not isinstance(job, dict):
        return JSONResponse(status_code=404, content={"error": "unknown job"})
    if job.get("status") == "error" and job.get("error_detail"):
        job = {**job, "error": job["error_detail"]}
    return job
