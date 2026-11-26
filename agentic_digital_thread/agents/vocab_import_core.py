# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Zero-write vocabulary-import classifier — the ONE shared home.

Extracted from ``vocabulary_routes.py`` so non-router
consumers (e.g. the onboarding runtime's read-only ``propose_concepts``
preview) can validate and classify import rows WITHOUT importing FastAPI
or any graph seam. This module is import-pure and stdlib-only: no
fastapi, no boto3, no pg8000, no graph reads. Callers supply
``existing_ids`` themselves — the router keeps its ONE lazy
``_read_existing_ids`` pre-read; other consumers bring their own bounded
read.

The C6/C7 no-drift-by-construction contract lives here: apply
(``POST /import``) and preview (``POST /import/preview``) — and every
future zero-write consumer — share the ONE ``classify_import_rows``
helper, so predictions cannot drift from apply behavior.
``collision_key`` is the C6 normalization (lowercase, ``-``→``_``) — the
underscore-drift trap: import ids are slugified to ``_``-form while core
graph ids use ``-``, so ``widget_rate`` must not shadow ``widget-rate``.
"""
from __future__ import annotations

import json
import re

from .untrusted_text import sanitize_untrusted, validate_untrusted

# Boundary caps for the import body (LOW-severity hardening): bound the
# batch size and field lengths so a single request cannot flood the
# graph writer.
IMPORT_MAX_CONCEPTS = 500
IMPORT_MAX_ID_LEN = 128
IMPORT_MAX_NAME_LEN = 256
IMPORT_MAX_DESCRIPTION_LEN = 4096
#: Bounds on a concept's synonyms (the single-concept write routes).
CONCEPT_MAX_SYNONYMS = 16
CONCEPT_MAX_SYNONYM_LEN = 128

_SLUG_KEEP_RE = re.compile(r"[^a-z0-9_]+")


def concept_text_error(*, name=None, description=None, synonyms=None) -> str:
    """``""`` when steward-written concept text may be stored, else the reason.

    Concept names, descriptions and synonyms reach agent prompts (the route
    seed's ``concept_notes``, a system agent's ``kg_context``), so every write
    path applies the canonical untrusted-text rules
    (:func:`untrusted_text.validate_untrusted`: length after sanitation,
    secret shapes, the reserved fence delimiters). ``None`` skips a field.
    ``synonyms`` must be a list of strings, at most
    :data:`CONCEPT_MAX_SYNONYMS`, each at most :data:`CONCEPT_MAX_SYNONYM_LEN`.
    The reason never quotes the text. Pure."""
    fields = [("name", "concept name", name, IMPORT_MAX_NAME_LEN),
              ("description", "concept description", description, IMPORT_MAX_DESCRIPTION_LEN)]
    if synonyms is not None:
        if not isinstance(synonyms, (list, tuple)):
            return "'synonyms' must be a list of strings"
        if len(synonyms) > CONCEPT_MAX_SYNONYMS:
            return f"'synonyms' has more than {CONCEPT_MAX_SYNONYMS} entries"
        for synonym in synonyms:
            if not isinstance(synonym, str):
                return "every synonym must be a string"
            fields.append(("synonym", "concept synonym", synonym, CONCEPT_MAX_SYNONYM_LEN))
    for field, label, value, cap in fields:
        if value is None:
            continue
        failed = validate_untrusted(sanitize_untrusted(value), max_len=cap,
                                    field=f"'{field}'", label=label)
        if failed:
            return failed[1]
    return ""


def clean_synonyms(synonyms) -> list[str]:
    """Sanitized, non-empty synonyms (call after :func:`concept_text_error`)."""
    return [text for text in (sanitize_untrusted(s) for s in (synonyms or [])) if text]


def encode_synonyms(synonyms) -> str:
    """A concept's synonyms as the ONE string property the graph stores.

    Neptune openCypher rejects a list as a property value, so the list is
    stored as a JSON array string (a synonym may itself contain a comma).
    Read it back with :func:`decode_synonyms`. Pure."""
    return json.dumps([str(s) for s in synonyms or ()], ensure_ascii=False)


def decode_synonyms(value) -> list[str]:
    """A Concept ``synonyms`` property as ``list[str]``, whatever shape it was
    stored in: the JSON array string :func:`encode_synonyms` writes, a list
    (an in-memory graph returns one), or a legacy comma-separated string.
    Non-string items and blanks are dropped; anything else is ``[]``. Pure."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                value = parsed
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def slugify(text) -> str:
    """Lowercase ``[a-z0-9_]`` slug (whitespace/hyphens → ``_``); '' if nothing
    survives — the caller treats an empty slug as a boundary-validation failure."""
    s = str(text or "").strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = _SLUG_KEEP_RE.sub("", s)
    return s.strip("_")


def validate_import_row(c) -> tuple[dict, str]:
    """One import row → ``(row, error)``. A non-empty ``error`` names the
    ACTUAL problem (missing name vs missing id vs charset vs length)."""
    if not isinstance(c, dict) or not sanitize_untrusted(str(c.get("name") or "")).strip():
        return {}, "every concept row needs a non-empty 'name'"
    name = sanitize_untrusted(str(c.get("name"))).strip()
    if not str(c.get("id") or "").strip():
        return {}, f"concept row {name!r} is missing 'id'"
    cid = slugify(c.get("id"))
    if not cid:
        return {}, (f"concept id {str(c.get('id'))!r} has no [a-z0-9_] "
                    "characters left after slugify")
    description = sanitize_untrusted(str(c.get("description") or ""))
    if len(cid) > IMPORT_MAX_ID_LEN:
        return {}, f"concept id for {name!r} exceeds {IMPORT_MAX_ID_LEN} characters"
    if len(name) > IMPORT_MAX_NAME_LEN:
        return {}, f"concept name for id {cid!r} exceeds {IMPORT_MAX_NAME_LEN} characters"
    if len(description) > IMPORT_MAX_DESCRIPTION_LEN:
        return {}, (f"concept description for id {cid!r} exceeds "
                    f"{IMPORT_MAX_DESCRIPTION_LEN} characters")
    unsafe = concept_text_error(name=name, description=description)
    if unsafe:
        return {}, f"concept {cid!r}: {unsafe}"
    return {"id": cid, "name": name, "description": description}, ""


def parse_import_body(body: dict) -> tuple[str, list[dict], str]:
    """Boundary validation for ``POST /import``. Returns ``(slug, rows, error)``
    — a non-empty ``error`` means 400 (shape errors are a client problem, and
    are raised BEFORE any write, never as a partial batch)."""
    metadata = body.get("metadata") or {}
    if not isinstance(metadata, dict) or not str(metadata.get("name") or "").strip():
        return "", [], "'metadata.name' is required"
    concepts = body.get("concepts")
    if not isinstance(concepts, list) or not concepts:
        return "", [], "'concepts' must be a non-empty list"
    if len(concepts) > IMPORT_MAX_CONCEPTS:
        return "", [], (f"'concepts' exceeds the {IMPORT_MAX_CONCEPTS}-row "
                        f"batch cap (got {len(concepts)}) — split the import")
    slug = slugify(metadata.get("name"))
    if not slug:
        return "", [], "'metadata.name' yields an empty slug"
    rows: list[dict] = []
    for c in concepts:
        row, err = validate_import_row(c)
        if err:
            return "", [], err
        rows.append(row)
    return slug, rows, ""


def collision_key(x) -> str:
    """The C6 normalization key (lowercase, ``-``→``_``) — the
    underscore-drift trap: import ids are slugified to ``_``-form while core
    graph ids use ``-``, so ``widget_rate`` must not shadow ``widget-rate``."""
    return str(x).strip().lower().replace("-", "_")


def classify_import_rows(
    rows: list[dict], existing_ids: set[str]
) -> list[tuple[dict, str, str | None]]:
    """Classify every import row as ``insert`` / ``skip_exact`` /
    ``skip_collision`` — the ONE helper shared by the apply path and every
    dry-run preview (C7), so predictions cannot drift from apply behavior.
    Exact-id matches win over collisions; a later in-batch row normalizing
    to an already-claimed key is a collision against the earlier row.
    Returns ``[(row, action, collides_with)]`` in request order."""
    existing_by_key: dict[str, str] = {}
    for gid in sorted(existing_ids):  # sorted: deterministic collision target
        existing_by_key.setdefault(collision_key(gid), gid)
    batch_by_key: dict[str, str] = {}
    out: list[tuple[dict, str, str | None]] = []
    for row in rows:
        rid = row["id"]
        key = collision_key(rid)
        if rid in existing_ids:
            out.append((row, "skip_exact", None))
        elif key in existing_by_key:
            out.append((row, "skip_collision", existing_by_key[key]))
        elif key in batch_by_key:
            out.append((row, "skip_collision", batch_by_key[key]))
        else:
            batch_by_key[key] = rid
            out.append((row, "insert", None))
    return out


def preview_rows(rows: list[dict], existing_ids: set[str]) -> dict:
    """The dry-run projection: classification rendered as per-row
    basis-tagged actions plus counters. ZERO writes — pure computation over
    caller-supplied ``existing_ids``."""
    classified = classify_import_rows(rows, existing_ids)
    actions = [action for _, action, _ in classified]
    return {
        "rows": [
            {"id": row["id"], "name": row["name"], "action": action,
             "collides_with": collides_with}
            for row, action, collides_with in classified
        ],
        "insert_count": actions.count("insert"),
        "skip_count": actions.count("skip_exact"),
        "collision_count": actions.count("skip_collision"),
    }
