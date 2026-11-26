# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Vocabulary admin router (ADR 0050) — ``/api/admin/vocabulary/*``.

The write surface for the concept vocabulary and its usage telemetry:

* ``GET  /concepts``  — the unified read model: ``vocabulary.list_concepts``
  composed with the ``artifact_usage`` ledger (both off-loop via
  ``asyncio.to_thread``, the ``get_proposed_mappings`` idiom).
* ``POST /concepts`` / ``PATCH /concepts/{id}`` / ``POST /concepts/{id}/retire``
  — human assertions routed through the ONE ADR-0003 lifecycle
  (``self_learning.assert_vocab``; never raw CRUD). ``id`` is immutable
  (PATCH carrying ``id`` → 400); retiring a mapped concept → 409 (the
  ``ValueError`` surfaced by the apply path's MAPS_TO guard).
* ``POST /import``    — bulk vocabulary import, the sanctioned generalization
  of the pack-install path (``admin_routes.install_concept_pack``): validate
  at the boundary, pre-read existing ids in ONE query, skip those, MERGE the
  rest with provenance ``pack:custom:<slug>``, per-row failure isolation
  (a bad row never aborts the batch). Rows whose NORMALIZED id (lowercase,
  ``-``→``_``) collides with an existing graph id without being an exact
  match are skipped and reported in the additive ``collisions`` array
  (``skipped_ids`` keeps meaning exact-id skips only).
* ``POST /import/preview`` — dry-run of ``/import``: same boundary
  validation and the SAME classification helper as the apply path (so
  predictions cannot drift), ZERO graph writes; per-row
  ``insert | skip_exact | skip_collision`` actions plus counters.
* ``GET  /retirement-candidates`` — report-only retirement sweep: a
  deterministic rule (non-core, not retired, 0 mapped fields, 0 uses in
  30 days) over ``vocabulary.list_concepts`` + the usage ledger. Retiring
  happens through the EXISTING ``POST /concepts/{id}/retire`` lifecycle
  route — this endpoint never writes.
* ``POST /guidance/{system_id}/propose`` — a ``guidance_note`` proposal into
  the open lifecycle (``log_proposed_mapping``; accept delegates to the
  existing ``append_query_guidance_note``). Optional ``scope``:
  ``query_hint`` (default) or ``answer_requirement``; anything else is 400.
* ``POST /guidance/{system_id}/promote-requirement`` — ADR-0063 D5: promote a
  business-context correction to an ANSWER REQUIREMENT. Composes the
  requirement sentence through ``failure_signals.requirement_phrasing`` and
  files it through the ``/propose`` handler above (no second filing path);
  business context is never written.
* ``GET  /metrics`` — code, accepted graph, pending, and shadowed/invalid
  metric definitions composed on read; ZERO writes.
* ``POST /metrics/propose`` — validate a metric definition fail-loud at the
  boundary, then file it PENDING through the ONE proposal lifecycle. There is
  deliberately no admin immediate-accept path: formulas always receive the
  existing proposal-queue diff review.
* ``GET  /metrics/{mapping_id}/preview`` — read-only structural/formula diff
  delegated to ``preview_metric_definition_mapping``; ``?resolve=1`` adds the
  ADR-0080 dry-run card value (``&with=<id>,…`` names pending companions),
  computed on the resolver offload lane — still ZERO writes.
* ``GET  /usage``     — the 30-day artifact-usage ledger.

Auth: the ``/api/admin`` prefix is auto-gated by
``auth.middleware.AuthMiddleware`` (``ADMIN_PREFIXES``) — non-admins get 403
before any handler runs; no per-handler check is needed.

Import purity (ADR 0020/0021): no boto3/pg8000/graph imports at module
import time — every impure dependency (``vocabulary``, ``artifact_usage``,
``self_learning``, ``agents.db``) is imported lazily inside the handlers, and
the graph seams are module-level lazy wrappers (``_neptune_query`` /
``_neptune_write``) so tests can monkeypatch them directly. The import
boundary validation and the C6/C7 classifier live in their ONE shared home,
``vocab_import_core`` (import-pure, stdlib-only), so apply and preview — and
any non-router consumer — cannot drift from each other by construction.
"""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .offload import offload, offload_resolver
from .untrusted_text import sanitize_untrusted as _sanitize_untrusted
from .vocab_import_core import (
    classify_import_rows as _classify_import_rows,
    clean_synonyms as _clean_synonyms,
    concept_text_error as _concept_text_error,
    parse_import_body as _parse_import_body,
    preview_rows as _preview_rows,
    slugify as _slug,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/vocabulary")

# The import path's id charset rule ([a-z0-9_] after lowercasing) — an
# EXPLICIT id on POST /concepts must satisfy the same rule (400 otherwise).
_ID_RE = re.compile(r"[a-z0-9_]+")


# --------------------------------------------------------------------------- #
# Seams (lazy — import-pure at module load, monkeypatchable in tests)
# --------------------------------------------------------------------------- #
def _neptune_query(cypher: str, parameters=None) -> list:
    """Lazy graph-read seam (mirrors ``vocabulary.neptune_query``)."""
    from .db import neptune_query
    return neptune_query(cypher, parameters)


def _neptune_write(cypher: str, parameters=None):
    """Lazy graph-write seam (the ``install_concept_pack`` idiom)."""
    from .db import neptune_write
    return neptune_write(cypher, parameters)


def _request_actor(request: Request) -> str:
    """The acting user's identity for provenance stamps — replicated from
    ``server.py``'s accept route: session email, else username; empty string
    in auth-disabled/local mode (provenance stays unattributed)."""
    try:
        from .auth import get_current_user
        user = get_current_user(request)
        return str(getattr(user, "email", "") or getattr(user, "username", "") or "")
    except Exception:
        return ""


def _assertion_response(result) -> dict:
    """Map an ``assert_vocab`` result onto the contract response shape:
    ``{"status": "accepted"|"pending", "mapping_id": str, "evidence_score": float|null}``."""
    result = result or {}
    score = result.get("evidence_score")
    return {
        "status": str(result.get("status") or "pending"),
        "mapping_id": str(result.get("mapping_id") or ""),
        "evidence_score": float(score) if score is not None else None,
    }


# --------------------------------------------------------------------------- #
# Read surface
# --------------------------------------------------------------------------- #
@router.get("/concepts")
async def get_concepts():
    """The unified concept list: read model + usage ledger, composed off-loop."""
    from . import artifact_usage, vocabulary
    usage = await asyncio.to_thread(artifact_usage.read_artifact_usage, 30)
    concepts = await asyncio.to_thread(vocabulary.list_concepts, usage=usage)
    return {"concepts": concepts, "total": len(concepts)}


# --------------------------------------------------------------------------- #
# Metric definitions (ADR-0066 — read/propose/preview; one existing accept door)
# --------------------------------------------------------------------------- #
def _metric_list_row(
    doc: dict, *, source: str, status: str, editable: bool,
    mapping_id: str = "",
) -> dict:
    """Project a C1 document onto the unified admin read-model row."""
    from .metric_definition import render_formula

    row = {
        "id": str(doc.get("id") or ""),
        "label": str(doc.get("label") or ""),
        "kind": str(doc.get("kind") or ""),
        "unit": str(doc.get("unit") or ""),
        "formula": render_formula(doc),
        "definition": dict(doc),
        "source": source,
        "status": status,
        "editable": editable,
    }
    if mapping_id:
        row["mapping_id"] = mapping_id
    return row


def _pending_metric_row(item: dict) -> dict:
    """Render one pending proposal, keeping stale invalid rows visible."""
    from .metric_definition import MetricDefinitionError, parse_definition

    mapping_id = str(item.get("mapping_id") or "")
    source = str(item.get("source") or "user")
    try:
        doc = parse_definition(item.get("query_context") or "")
        return _metric_list_row(
            doc, source=source, status="pending", editable=True,
            mapping_id=mapping_id)
    except MetricDefinitionError as exc:
        return {
            "id": str(item.get("to_value") or ""),
            "label": "",
            "kind": "",
            "unit": "",
            "formula": "",
            "definition": item.get("query_context") or "",
            "source": source,
            "status": "pending",
            "editable": True,
            "mapping_id": mapping_id,
            "reason": str(exc),
        }


def _shadowed_metric_row(skipped: dict) -> dict:
    """Project one graph row excluded by the store onto the visible surface."""
    return {
        "id": str(skipped.get("id") or ""),
        "label": "",
        "kind": "",
        "unit": "",
        "formula": "",
        "definition": None,
        "source": "graph",
        "status": "shadowed",
        "editable": False,
        "reason": str(skipped.get("reason") or "invalid graph row"),
    }


def _read_metric_rows() -> list[dict]:
    """Build the complete metric-definition read model without writing.

    Uses ``_query_by_status`` instead of ``get_pending_mappings``: the generic
    helper may expire unrelated insights, but this GET has a ZERO-write contract.
    """
    from . import metric_definition_store, self_learning
    from .metric_layer import CodeMetricLayer

    definitions = metric_definition_store.load_definitions()
    pending = self_learning._query_by_status("pending", newest_first=True)
    rows = [
        _metric_list_row(
            self_learning._code_metric_document(metric),
            source="code", status="code", editable=False)
        for metric in CodeMetricLayer().list_metrics()
    ]
    rows.extend(
        _metric_list_row(doc, source="graph", status="accepted", editable=True)
        for _metric_id, doc in sorted(definitions.items())
    )
    rows.extend(
        _pending_metric_row(item) for item in pending
        if item.get("proposed_type") == "metric_definition"
    )
    rows.extend(
        _shadowed_metric_row(skipped)
        for skipped in metric_definition_store.shadowed_or_invalid()
    )
    rank = {"code": 0, "accepted": 1, "pending": 2, "shadowed": 3}
    rows.sort(key=lambda row: (
        rank.get(str(row.get("status")), 99), str(row.get("id") or ""),
        str(row.get("mapping_id") or ""),
    ))
    return rows


@router.get("/metrics")
async def get_metrics():
    """Unified metric read model, computed off-loop and guaranteed read-only."""
    metrics = await asyncio.to_thread(_read_metric_rows)
    return {"metrics": metrics, "total": len(metrics)}


def _parse_validate_and_file_metric(body: dict) -> str | None:
    """Boundary-validate one C1 document, then file it PENDING.

    There is intentionally no admin immediate-accept switch. A formula defines
    what a number means, so acceptance remains behind the existing
    ``/api/proposed-mappings/{id}/accept`` steward-review door.
    """
    from . import self_learning
    from .metric_definition import (
        canonical_json, parse_definition, validate_definition,
    )
    from .metric_dimension import WINDOW_VOCAB

    doc = parse_definition(body)
    concepts, metric_ids, code_ids, definitions = (
        self_learning._metric_definition_context())
    validate_definition(
        doc,
        known_concepts=concepts,
        known_metric_ids=metric_ids,
        code_metric_ids=code_ids,
        window_vocab=WINDOW_VOCAB,
        definitions=definitions,
    )
    return self_learning.log_proposed_mapping(
        from_system="", from_field="", from_value="",
        to_system="", to_field="", to_value=doc["id"], concept="",
        confidence=1.0, query_context=canonical_json(doc),
        proposed_type="metric_definition", source="user",
        provenance="human-asserted",
    )


@router.post("/metrics/propose")
async def propose_metric_definition(request: Request, body: dict):
    """Validate and file a PENDING metric definition; never accept it here."""
    from .metric_definition import MetricDefinitionError

    try:
        mapping_id = await asyncio.to_thread(
            _parse_validate_and_file_metric, body)
    except (MetricDefinitionError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if not mapping_id:
        return JSONResponse(
            status_code=400,
            content={"error": "metric definition proposal was dropped at ingestion"},
        )
    return {"status": "pending", "mapping_id": mapping_id}


def preview_metric_definition(mapping_id: str, resolve: bool = False,
                              with_param: str | None = None):
    """Sync body of the preview route (runs on the resolver offload lane).

    Without ``resolve`` the helper is called exactly as before (structural
    diff, byte-unchanged). ``resolve`` adds the ADR-0080 ``dry_run`` block;
    ``with`` names pending companions resolved together (400 when malformed).
    """
    from .self_learning import preview_metric_definition_mapping

    if not resolve:
        return _preview_response(preview_metric_definition_mapping(mapping_id))
    from .metric_preview import PreviewParameterError, parse_with_ids
    try:
        with_ids = parse_with_ids(with_param, mapping_id)
    except PreviewParameterError as exc:
        return JSONResponse(status_code=400, headers=_NO_STORE, content={
            "error": "invalid_with_parameter", "detail": str(exc)})
    return _preview_response(preview_metric_definition_mapping(
        mapping_id, resolve=True, with_ids=with_ids), no_store=True)


@router.get("/metrics/{mapping_id}/preview")
async def preview_metric_definition_route(
    mapping_id: str,
    resolve: bool = False,
    with_param: str | None = Query(default=None, alias="with"),
):
    """Return the computed formula diff for one pending proposal — and, with
    ``resolve``, its read-only dry-run card value (ADR-0080); ZERO writes."""
    return await offload_resolver(
        preview_metric_definition, mapping_id, resolve, with_param)


#: A dry-run value is computed per request from CURRENT data — no browser or
#: intermediary may reuse it (ADR-0080: nothing about a preview persists).
_NO_STORE = {"Cache-Control": "private, no-store"}


def _preview_response(result: dict, *, no_store: bool = False):
    """The helper's ``http_status`` becomes the response status code; a
    resolved (dry-run) response is always ``Cache-Control: private, no-store``."""
    code = int(result.get("http_status", 200))
    payload = {key: value for key, value in result.items()
               if key != "http_status"}
    if no_store:
        return JSONResponse(status_code=code, headers=_NO_STORE,
                            content=jsonable_encoder(payload))
    return (JSONResponse(status_code=code, content=payload)
            if code != 200 else payload)


@router.get("/usage")
async def get_usage():
    """The 30-day artifact-usage ledger (deterministic order: most-used first)."""
    from . import artifact_usage
    usage = await asyncio.to_thread(artifact_usage.read_artifact_usage, 30)
    artifacts = sorted(
        usage.values(),
        key=lambda a: (-int(a.get("usage_30d") or 0), str(a.get("artifact_id") or "")),
    )
    return {
        "artifacts": [
            {
                "artifact_id": a.get("artifact_id"),
                "kind": a.get("kind"),
                "usage_30d": int(a.get("usage_30d") or 0),
                "last_used_at": a.get("last_used_at"),
            }
            for a in artifacts
        ]
    }


# --------------------------------------------------------------------------- #
# Single-concept assertions (the ADR-0003 lifecycle — never raw CRUD)
# --------------------------------------------------------------------------- #
@router.post("/concepts")
async def create_concept(request: Request, body: dict):
    """Assert a new concept (op ``add``) through the lifecycle. A missing
    ``id`` is derived from the name (lowercase ``[a-z0-9_]`` slug); an
    EXPLICIT id must satisfy the same charset rule the import path applies
    (``[a-z0-9_]`` after lowercasing) — 400 otherwise. Name, description and
    synonyms pass the canonical untrusted-text check first
    (``vocab_import_core.concept_text_error``; they reach agent prompts) —
    400 otherwise, before the lifecycle runs."""
    name = _sanitize_untrusted(str(body.get("name") or "")).strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "'name' is required"})
    explicit_id = str(body.get("id") or "").strip().lower()
    if explicit_id and not _ID_RE.fullmatch(explicit_id):
        return JSONResponse(
            status_code=400,
            content={"error": "'id' may only contain [a-z0-9_] (after lowercasing)"},
        )
    concept = explicit_id or _slug(name)
    description = _sanitize_untrusted(str(body.get("description") or ""))
    synonyms = body.get("synonyms")
    unsafe = _concept_text_error(name=name, description=description, synonyms=synonyms)
    if unsafe:
        return JSONResponse(status_code=400, content={"error": unsafe})
    from . import self_learning
    try:
        result = self_learning.assert_vocab(
            op="add",
            concept=concept,
            name=name,
            description=description,
            synonyms=_clean_synonyms(synonyms),
            evidence_system=str(body.get("evidence_system") or ""),
            evidence_field=str(body.get("evidence_field") or ""),
            override_evidence=bool(body.get("override_evidence") or False),
            actor=_request_actor(request),
            is_admin=bool(getattr(request.state, "is_admin", False)),
        )
    except ValueError as e:
        # Client-actionable rejection (the accept-route convention) — not a 500.
        return JSONResponse(status_code=400, content={"error": str(e)})
    return _assertion_response(result)


@router.patch("/concepts/{concept_id}")
async def edit_concept(concept_id: str, request: Request, body: dict):
    """Assert an edit (op ``edit``). The id is immutable — a body carrying
    ``id`` is rejected at the boundary. A body WITHOUT a usable ``name`` passes
    ``name=None`` through to the lifecycle (the apply path preserves the
    existing name on absence — never fabricate one here, and never the id).
    The same untrusted-text check as ``POST /concepts`` applies (400)."""
    if "id" in body:
        return JSONResponse(
            status_code=400,
            content={"error": "'id' is immutable — it cannot appear in an edit body"},
        )
    name = _sanitize_untrusted(str(body.get("name") or "")).strip() or None
    description = _sanitize_untrusted(str(body.get("description") or ""))
    synonyms = body.get("synonyms")
    unsafe = _concept_text_error(name=name, description=description, synonyms=synonyms)
    if unsafe:
        return JSONResponse(status_code=400, content={"error": unsafe})
    from . import self_learning
    try:
        result = self_learning.assert_vocab(
            op="edit",
            concept=concept_id,
            name=name,
            description=description,
            synonyms=_clean_synonyms(synonyms),
            actor=_request_actor(request),
            is_admin=bool(getattr(request.state, "is_admin", False)),
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return _assertion_response(result)


@router.post("/concepts/{concept_id}/retire")
async def retire_concept(concept_id: str, request: Request):
    """Soft-retire a concept (op ``retire``). A concept with inbound MAPS_TO
    edges is refused by the apply path's guard (ValueError) → 409."""
    from . import self_learning
    try:
        self_learning.assert_vocab(
            op="retire",
            concept=concept_id,
            actor=_request_actor(request),
            is_admin=bool(getattr(request.state, "is_admin", False)),
        )
    except ValueError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    return {"status": "retired"}


# --------------------------------------------------------------------------- #
# Bulk import (the sanctioned pack-install generalization)
# --------------------------------------------------------------------------- #
# Boundary caps, row/body validation (`_parse_import_body`), and the C6/C7
# classifier (`_classify_import_rows` / `_preview_rows`) live in their ONE
# shared home, ``vocab_import_core`` — imported at the top of this module.


def _read_existing_ids(ids: list[str]) -> set[str]:
    """The ONE import pre-read: existing graph ids matching the candidates
    EXACTLY or on the normalized collision key (``ids`` are already slugified
    ``_``-form, so a colliding graph id normalizes into the same list). A
    pre-read failure degrades to none-existing — MERGE is idempotent."""
    try:
        rows = _neptune_query(
            "MATCH (c:Concept) WHERE c.id IN $ids "
            "OR replace(toLower(c.id), '-', '_') IN $ids "
            "RETURN c.id AS id",
            {"ids": ids},
        )
    except Exception as e:
        logger.warning(f"vocabulary import pre-read failed (continuing): {e}")
        rows = []
    return {r.get("id") for r in (rows or []) if r.get("id")}


def _drift_pass_report(slug: str, inserted_ids: list[str]) -> dict:
    """ADR-0081 post-commit hook: file PENDING binding proposals for the
    just-imported concepts (same worker thread as the import). A drift
    failure NEVER fails the import — coarse error class only, no upstream
    text (an OTel handler discards tracebacks; the class is the signal)."""
    try:
        from .vocabulary_drift import propose_bindings
        return propose_bindings(inserted_ids, provenance=f"import:{slug}")
    except Exception as exc:  # noqa: BLE001 - isolation by contract
        logger.warning("vocabulary drift pass failed (import ok): %s",
                       type(exc).__name__)
        return {"error": type(exc).__name__}


def _run_import(slug: str, rows: list[dict]) -> dict:
    """The install_concept_pack idiom, generalized: pre-read existing ids in
    ONE query, skip exact matches (``skipped_ids``) and normalized-key
    collisions (``collisions``, C6 — neither inserted nor MERGEd), MERGE the
    rest with provenance ``pack:custom:<slug>`` — per-row failure isolation
    (a bad row never aborts the batch). Sync (called off-loop via
    ``asyncio.to_thread``). After the commit, the ADR-0081 vocabulary-drift
    pass files PENDING binding proposals for the inserted concepts
    (``vocabulary_drift`` key; failure-isolated)."""
    classified = _classify_import_rows(
        rows, _read_existing_ids([r["id"] for r in rows]))
    source = f"pack:custom:{slug}"
    inserted_ids: list[str] = []
    failed = 0
    skipped_ids: list[str] = []
    collisions: list[dict] = []
    for row, action, collides_with in classified:
        if action == "skip_exact":
            skipped_ids.append(row["id"])
            continue
        if action == "skip_collision":
            collisions.append({"id": row["id"], "existing_id": collides_with,
                               "reason": "normalized-id collision"})
            continue
        try:
            _neptune_write(
                "MERGE (c:Concept {id: $id}) "
                "SET c.name = $name, c.description = $description, c.source = $source",
                {"id": row["id"], "name": row["name"],
                 "description": row["description"], "source": source},
            )
            inserted_ids.append(row["id"])
        except Exception as e:  # per-row isolation — never abort the batch
            logger.warning(f"vocabulary import MERGE failed for {row['id']}: {e}")
            failed += 1
    return {
        "status": "ok",
        "slug": slug,
        "inserted": len(inserted_ids),
        "skipped": len(skipped_ids),
        "failed": failed,
        "skipped_ids": skipped_ids,
        "collisions": collisions,
        "vocabulary_drift": _drift_pass_report(slug, inserted_ids),
    }


@router.post("/import")
async def import_vocabulary(request: Request, body: dict):
    """Bulk-import a flat custom pack: ``{metadata: {name, ...},
    concepts: [{id, name, description}]}``. Skips ids already in the graph
    (reported as ``skipped_ids``) and normalized-id collisions (reported as
    ``collisions`` — C6); MERGEs the rest with provenance
    ``pack:custom:<slug>``; a failing row increments ``failed`` and never
    aborts the batch."""
    slug, rows, error = _parse_import_body(body)
    if error:
        return JSONResponse(status_code=400, content={"error": error})
    return await asyncio.to_thread(_run_import, slug, rows)


def _run_preview(slug: str, rows: list[dict]) -> dict:
    """The ``/import/preview`` projection: the SAME pre-read + classification
    as ``_run_import`` (shared ``vocab_import_core`` helper — no drift by
    construction), rendered as per-row basis-tagged actions plus counters.
    Zero graph writes."""
    return {
        "status": "preview",
        "slug": slug,
        **_preview_rows(rows, _read_existing_ids([r["id"] for r in rows])),
    }


@router.post("/import/preview")
async def preview_import_vocabulary(request: Request, body: dict):
    """Dry-run of ``POST /import`` (C7): identical boundary validation, ZERO
    graph writes. Reports what the apply path WOULD do per row
    (``insert`` / ``skip_exact`` / ``skip_collision`` with the colliding id),
    in request-body order."""
    slug, rows, error = _parse_import_body(body)
    if error:
        return JSONResponse(status_code=400, content={"error": error})
    return await asyncio.to_thread(_run_preview, slug, rows)


# --------------------------------------------------------------------------- #
# Manual vocabulary-drift trigger (ADR-0081 D4 — the steward re-run)
# --------------------------------------------------------------------------- #
# Cheap DoS bound on the manual body (the import batch cap idea).
_DRIFT_IDS_CAP = 500


def run_vocabulary_drift(body: dict):
    """Sync body (offloaded): validate, then run the ADR-0081 pass with
    ``provenance="manual"``. Proposals only — the pass never writes the graph."""
    ids = body.get("concept_ids") if isinstance(body, dict) else None
    if (not isinstance(ids, list) or not ids
            or not all(isinstance(i, str) and i.strip() for i in ids)):
        return JSONResponse(status_code=400, content={
            "error": "'concept_ids' must be a non-empty list of concept id "
                     "strings"})
    if len(ids) > _DRIFT_IDS_CAP:
        return JSONResponse(status_code=400, content={
            "error": f"'concept_ids' accepts at most {_DRIFT_IDS_CAP} ids, "
                     f"got {len(ids)}"})
    from .vocabulary_drift import propose_bindings
    return propose_bindings([i.strip() for i in ids], provenance="manual")


@router.post("/drift/run")
async def run_vocabulary_drift_route(request: Request, body: dict):
    """ADR-0081 manual steward trigger — admin-gated by the ``/api/admin``
    prefix; sync work offloaded (EVENT-LOOP OFFLOAD PATTERN, agents/offload.py)."""
    return await offload(run_vocabulary_drift, body)


# --------------------------------------------------------------------------- #
# Retirement sweep (C8 — report-only; retire via the existing lifecycle route)
# --------------------------------------------------------------------------- #
def _is_retirement_candidate(concept: dict, core_ids: set) -> bool:
    """The deterministic C8 rule (no LLM): candidate iff NOT retired AND
    0 mapped fields AND 0 uses in 30 days AND non-core (by ``source`` AND by
    id — core is never a candidate)."""
    return (
        not bool(concept.get("retired"))
        and int(concept.get("mapped_field_count") or 0) == 0
        and int(concept.get("usage_30d") or 0) == 0
        and str(concept.get("source") or "") != "core"
        and str(concept.get("id") or "") not in core_ids
    )


def _retirement_candidate_row(concept: dict) -> dict:
    """Project a read-model row onto the C8 candidate shape. ``reasons`` names
    the data-driven clauses (the rule is conjunctive, so both always hold)."""
    return {
        "id": str(concept.get("id") or ""),
        "name": str(concept.get("name") or ""),
        "source": str(concept.get("source") or ""),
        "mapped_field_count": int(concept.get("mapped_field_count") or 0),
        "usage_30d": int(concept.get("usage_30d") or 0),
        "last_used_at": concept.get("last_used_at"),
        "reasons": ["0 mapped fields", "0 uses in the last 30 days"],
    }


@router.get("/retirement-candidates")
async def get_retirement_candidates():
    """Report-only retirement sweep (C8): compose the read model with the
    30-day usage ledger off-loop (the ``GET /concepts`` idiom) and apply the
    deterministic candidate rule. NEVER writes — retiring goes through the
    existing ``POST /concepts/{id}/retire`` lifecycle route, whose MAPS_TO
    guard fails a stale candidate safe (409) at retire time."""
    from . import artifact_usage, vocabulary
    usage = await asyncio.to_thread(artifact_usage.read_artifact_usage, 30)
    concepts = await asyncio.to_thread(vocabulary.list_concepts, usage=usage)
    from .catalog import CORE_CONCEPTS  # lazy (ADR 0020/0021 import purity)
    core_ids = set(CORE_CONCEPTS)
    candidates = sorted(
        (_retirement_candidate_row(c) for c in concepts
         if _is_retirement_candidate(c, core_ids)),
        key=lambda row: row["id"],
    )
    return {"candidates": candidates}


# --------------------------------------------------------------------------- #
# Guidance-note proposals (open lifecycle)
# --------------------------------------------------------------------------- #
def _system_known(system_id: str) -> bool:
    """Best-effort system-existence check for guidance proposals — ONE read of
    the same catalog seam the rest of the app lists systems from
    (``catalog.get_all``, the ``onboard_agent.get_catalog`` idiom).

    Degrades OPEN — availability over strictness for a demo app: a system is
    "definitively unknown" only when the catalog HAS entries and none match.
    An empty catalog (e.g. pre-hydration at startup) or a lookup failure
    returns True rather than rejecting a legitimate proposal."""
    try:
        from .catalog import catalog  # lazy (ADR 0020/0021 import purity)
        systems = catalog.get_all()
        if not systems:
            return True
        return any(str(s.get("id") or "") == system_id for s in systems)
    except Exception as e:
        logger.warning(f"guidance propose: system lookup failed (degrading open): {e}")
        return True


async def _file_guidance_proposal(system_id: str, note: str, scope: str):
    if not note:
        return JSONResponse(status_code=400, content={"error": "'note' is required"})
    if not await asyncio.to_thread(_system_known, system_id):
        return JSONResponse(
            status_code=404, content={"error": f"unknown system '{system_id}'"})
    from . import self_learning
    mapping_id = await asyncio.to_thread(
        lambda: self_learning.log_proposed_mapping(
            from_system=system_id, from_field="", from_value="",
            to_system="", to_field="", to_value=note, concept="",
            confidence=1.0, query_context="human-asserted guidance note",
            proposed_type="guidance_note", source="user",
            provenance="human-asserted", guidance_scope=scope))
    if not mapping_id:
        return JSONResponse(
            status_code=400, content={"error": "proposal was dropped at ingestion"})
    return {"status": "pending", "mapping_id": mapping_id}


#: Scopes a steward may file through ``/propose``. ``legacy_unscoped`` is
#: compatibility content only, never a new filing.
_PROPOSABLE_GUIDANCE_SCOPES = ("query_hint", "answer_requirement")


@router.post("/guidance/{system_id}/propose")
async def propose_guidance_note(system_id: str, request: Request, body: dict):
    """File a guidance proposal for steward review.

    ``scope`` is optional: ``query_hint`` (the default) or
    ``answer_requirement``, a free-text rule the orchestrator applies when it
    writes the final answer. Either way the note lands PENDING and only a
    steward accepts it."""
    scope = body.get("scope")
    if scope is None:
        scope = _PROPOSABLE_GUIDANCE_SCOPES[0]
    if not isinstance(scope, str) or scope not in _PROPOSABLE_GUIDANCE_SCOPES:
        return JSONResponse(
            status_code=400,
            content={"error": "'scope' must be 'query_hint' or "
                              "'answer_requirement'"})
    note = str(body.get("note") or "").strip()
    return await _file_guidance_proposal(system_id, note, scope)


@router.post("/guidance/{system_id}/promote-requirement")
async def promote_answer_requirement(system_id: str, request: Request, body: dict):
    """Promote a business-context CORRECTION to an ANSWER REQUIREMENT (ADR-0063
    D5) — the action behind a ``context_not_reflected`` steward question.

    Measured twice: a fact written as business context (reference material the
    agent MAY use) kept losing to the answer, while the SAME knowledge phrased
    as a requirement (an instruction it MUST follow) took on the first attempt.

    This route is ADDITIVE and files nothing new of its own: the requirement
    sentence comes from ``failure_signals.requirement_phrasing`` (the one
    source of truth, which also BOUNDS both untrusted body terms) and is filed
    through the SAME ``POST /guidance/{system_id}/propose`` handler — same
    lifecycle, ``source="user"`` / ``provenance="human-asserted"``, and
    ``guidance_note`` sits in ``self_learning._NEVER_AUTO_ACCEPT_TYPES`` so the
    note lands PENDING and only a steward accepts it.

    The steward's business context is NEVER read, rewritten, or written here
    (ADR-0054: their text is theirs).
    """
    from .failure_signals import requirement_phrasing
    from .guidance_scope import GuidanceScope
    note = requirement_phrasing(body.get("asserted"), body.get("negated"))
    if not note:
        return JSONResponse(
            status_code=400,
            content={"error": "'negated' is required — the wording the answer "
                              "must not use"},
        )
    result = await _file_guidance_proposal(
        system_id, note, GuidanceScope.ANSWER_REQUIREMENT.value)
    if isinstance(result, JSONResponse):
        return result
    return {**result, "requirement": note}
