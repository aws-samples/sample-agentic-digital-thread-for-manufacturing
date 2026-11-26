# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evidence-based concept suggestion at MAP time (a value-overlap classifier).

This is the RESOLVE step's measured evidence, applied EARLIER — during mapping.
When the semantic LLM step leaves a column UNCERTAIN (``concept == 'needs_review'``
or best confidence below the needs_review floor), we cross-check the column's
sampled values against the VALUE POPULATIONS of concepts already established in
the graph by previously-onboarded PEER systems, and suggest the concept whose
population the column's values overlap.

Why this reaches, e.g., a ``cell`` column to the ``site`` concept with ZERO name
assumptions: the suggestion is driven purely by the fact that the column's values
(``SITE-A``, ``SITE-B`` …) overlap the population a peer system already mapped to
``site`` — no dataset-specific literal, no name heuristic, is encoded here.

Design (mirrors ``resolve_evidence``: heavy logic in PURE, unit-testable helpers;
IO isolated and best-effort):
  * PURE helpers — ``is_uncertain``, ``looks_identifier_ish``, ``strong_overlap``,
    ``evidence_confidence``, ``extract_synonyms`` / ``isa95_synonym_candidates``,
    and ``rank_candidates`` — no IO, fully offline-assertable.
  * IO — ``_peer_concept_populations`` (one bounded graph query + bounded
    per-concept value sampling, cached) and the orchestrator
    ``suggest_concepts_by_value_overlap`` wired into the map job.

DRY: value tokenization / overlap MEASUREMENT and multi-source sampling are NOT
re-implemented here — we reuse ``resolve_evidence.value_overlap`` and
``resolve_evidence.sample_field_values`` (and their bounded caps) verbatim.

False-link safety: strong measured evidence produces a strong SUGGESTION (an
upgraded proposed concept + a ``basis`` annotation), never an auto-committed hard
cross-system identity link. The link itself is still formed — and confirmed —
downstream in the resolve step. A weak/coincidental overlap never upgrades the
concept; it may only surface as a ranked, basis-tagged candidate.
"""
from __future__ import annotations

import logging
import re

from .catalog import CORE_CONCEPT_DEFINITIONS, catalog
from .db import neptune_query
from .join_discovery import camel_split, is_identifier_like, is_measure_like
from .resolve_evidence import sample_field_values, value_overlap
# "Synonyms (…): a, b, c." clause parsing — the ONE parser home is
# ``vocab_fit`` (import-pure, stdlib-only); re-exported here so existing
# consumers (``metric_term_resolution``, tests) keep their import path while
# exactly one regex/implementation exists.
from .vocab_fit import _SYNONYM_RE, extract_synonyms  # noqa: F401 - re-export

logger = logging.getLogger(__name__)

# ── Generic thresholds (NO dataset/domain specifics) ─────────────────────────
# Kept in sync with the per-field needs_review decision in onboard._run_map_job.
_UNCERTAIN_FLOOR = 0.4
# A single shared value can be coincidental; require a minimum distinct-value
# support before treating an overlap as evidence.
_MIN_DISTINCT_SUPPORT = 2
# …and require it to cover a meaningful fraction of the smaller sampled token set
# (size-normalized), so a couple of stray matches against a large population do
# not qualify as "strong".
_MIN_OVERLAP_RATIO = 0.5
# Evidence-backed confidence band. Deliberately capped BELOW 1.0 (false-link
# safety): strong evidence => strong, still-confirmable suggestion — never a
# certainty that would read as an auto-committed link.
_EVIDENCE_CONF_MIN = 0.5
_EVIDENCE_CONF_MAX = 0.85
# Name-synonym (ISA-95) candidates: a mild positive signal — above the uncertain
# floor (so it beats a weak semantic guess) but below any measured value evidence.
_ISA95_CONF = 0.45
# Bounded per-concept peer population size (cost gate; each sample call is itself
# bounded by resolve_evidence's own sample cap).
_PEER_VALUE_CAP = 40

_EMPTY_CONCEPTS = (None, "", "needs_review", "unknown")
# Peer edges whose values form an IDENTITY population. An 'identifier' edge
# names the concept, so its values are evidence whatever the column is called.
# A 'correlate' edge only describes or classifies (a status, a description), and
# an edge with no role predates role tags: either counts only when the peer
# column is itself named like a key (join_discovery.is_identifier_like). A
# 'measure' edge (a quantity, rate or timestamp) never counts: numbers coincide
# across unrelated systems.
_IDENTITY_ROLE = "identifier"
_KEY_NAMED_ROLES = frozenset({"correlate", ""})
# A measure word in a column name (count, rate, size, total ...) marks a measure,
# unless the name ends in one of these key words: rate_code, size_code and
# total_id are codes, and a code is exactly what value overlap can match.
_KEY_SUFFIX_TOKENS = frozenset({"id", "code", "key", "ref", "no", "num", "sku", "tag"})

# ISO-ish date/datetime, e.g. 2026-07-01, 2026/07/01, 2026-07-01T14:23, …
_TS_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
# Free-text heuristics.
_FREE_TEXT_MAXLEN = 60
_FREE_TEXT_MAX_SPACES = 3
# Basis strength for tie-breaking a ranked candidate list. overlay_synonym
# (T1.3 — pack/interview-supplied synonyms via ConceptOverlay nodes) is the
# SAME mild name-signal strength as the built-in ISA-95 synonym clause.
_BASIS_RANK = {"value_overlap": 3, "isa95_synonym": 2, "overlay_synonym": 2,
               "semantic": 1}


# ---------------------------------------------------------------------------
# PURE helpers (no IO — offline-assertable)
# ---------------------------------------------------------------------------

def _clean_concept(concept) -> bool:
    """True when ``concept`` is a real, usable concept id (not a sentinel)."""
    return bool(concept) and concept not in _EMPTY_CONCEPTS


def is_uncertain(mapping, floor: float = _UNCERTAIN_FLOOR) -> bool:
    """True when the semantic map step left this column UNCERTAIN.

    Uncertain == concept is a sentinel (``needs_review``/``unknown``/empty) OR the
    best confidence is below ``floor`` (kept in sync with the per-field
    needs_review decision in ``onboard._run_map_job``)."""
    if not isinstance(mapping, dict):
        return False
    if mapping.get("concept") in _EMPTY_CONCEPTS:
        return True
    try:
        return float(mapping.get("confidence", 0) or 0) < floor
    except (TypeError, ValueError):
        return True


def _is_timestamp_like(token: str) -> bool:
    return bool(_TS_RE.match(token.strip()))


def _is_free_text(token: str) -> bool:
    t = token.strip()
    return len(t) > _FREE_TEXT_MAXLEN or t.count(" ") >= _FREE_TEXT_MAX_SPACES


def looks_identifier_ish(values, min_values: int = 1) -> bool:
    """True when sampled ``values`` look like identifier/coded tokens.

    Value-overlap evidence is only trustworthy for identifier-ish/coded columns:
    free-text and raw timestamps overlap coincidentally across unrelated systems.
    This gate returns False when the MAJORITY of non-empty sampled values look like
    timestamps or free text, and requires at least ``min_values`` usable samples.
    Pure — safe offline."""
    toks = [str(v).strip() for v in (values or []) if v is not None and str(v).strip()]
    if len(toks) < max(1, min_values):
        return False
    bad = sum(1 for t in toks if _is_timestamp_like(t) or _is_free_text(t))
    return bad * 2 <= len(toks)  # majority (>= half) are identifier-ish


def _key_named(column_name: str) -> bool:
    """True when a name ends in a key word (``size_code``, ``total_id``,
    ``line_id``). Pure."""
    toks = [t for t in re.split(r"[^a-z0-9]+", camel_split(column_name or "").lower()) if t]
    return bool(toks) and toks[-1] in _KEY_SUFFIX_TOKENS


def is_measure_named(column_name: str) -> bool:
    """True when a column's name marks it as a measure (count, quantity, rate,
    size, total ...) and does not end in a key word (``rate_code``,
    ``size_code`` and ``total_id`` are codes). Pure."""
    return is_measure_like(column_name) and not _key_named(column_name)


def _is_number(token: str) -> bool:
    try:
        float(token)
    except (TypeError, ValueError):
        return False
    return True


def numeric_measure(column_name: str, values) -> bool:
    """True when a measure-named column holds numbers (most non-empty sampled
    values parse as numbers). Numbers coincide across unrelated systems, so
    such a column's values are not identity evidence, on either side of the
    comparison. A measure-named column holding codes (a ``size`` of S/M/L) is
    a classification and still counts. Pure."""
    if not is_measure_named(column_name):
        return False
    toks = [str(v).strip() for v in (values or []) if v is not None and str(v).strip()]
    return bool(toks) and sum(_is_number(t) for t in toks) * 2 > len(toks)


def is_identity_edge(role, field_name: str) -> bool:
    """True when a peer Field-[:MAPS_TO]->Concept edge MAY serve as an identity
    population. An 'identifier' edge may, whatever the column is called; a
    'correlate' or role-less edge may when the peer column is named like a key
    (``is_identifier_like``, or a measure word ending in a key word, the same
    exception :func:`is_measure_named` makes). The sampled values decide the
    rest (:func:`numeric_measure`). Pure."""
    r = str(role or "").lower()
    if r == _IDENTITY_ROLE:
        return True
    return r in _KEY_NAMED_ROLES and (is_identifier_like(field_name or "")
                                      or _key_named(field_name))


def strong_overlap(overlap: dict, *, min_support: int = _MIN_DISTINCT_SUPPORT,
                   min_ratio: float = _MIN_OVERLAP_RATIO) -> bool:
    """True when a measured ``value_overlap`` result is STRONG and non-coincidental:
    at least ``min_support`` distinct shared tokens AND a size-normalized ratio at
    or above ``min_ratio``. Pure."""
    if not overlap:
        return False
    return (overlap.get("overlap_count", 0) >= min_support
            and overlap.get("ratio", 0) >= min_ratio)


def evidence_confidence(overlap: dict) -> float:
    """Map a measured overlap to an evidence-backed confidence in the capped band
    [``_EVIDENCE_CONF_MIN``, ``_EVIDENCE_CONF_MAX``] — scaled by the overlap ratio.
    Never returns 1.0 (false-link safety). Pure."""
    ratio = 0.0
    try:
        ratio = min(float(overlap.get("ratio", 0) or 0), 1.0)
    except (TypeError, ValueError):
        ratio = 0.0
    conf = _EVIDENCE_CONF_MIN + (_EVIDENCE_CONF_MAX - _EVIDENCE_CONF_MIN) * ratio
    return round(min(_EVIDENCE_CONF_MAX, conf), 3)


def _name_tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


def isa95_synonym_candidates(column_name: str, concept_definitions: dict) -> list[str]:
    """Concepts whose ISA-95 synonym list contains the column name (token match).

    Reads the ``Synonyms (…)`` clause the catalog concept definitions carry, so it
    is generic — no dataset-specific literals. A synonym matches when its token set
    is a subset of the column-name token set (e.g. column ``work_center`` matches
    the synonym ``work center``). Pure."""
    name_tokens = _name_tokens(column_name)
    if not name_tokens:
        return []
    out: list[str] = []
    for concept, definition in (concept_definitions or {}).items():
        for syn in extract_synonyms(definition):
            syn_tokens = _name_tokens(syn)
            if syn_tokens and syn_tokens <= name_tokens:
                out.append(concept)
                break
    return out


def overlay_synonym_candidates(column_name: str,
                               overlay_synonyms: dict) -> list[str]:
    """Concepts whose OVERLAY synonyms contain the column name (T1.3).

    ``overlay_synonyms`` is ``{concept_id: [synonym term, ...]}`` (built by
    :func:`_overlay_synonyms_by_concept` from the ConceptOverlay nodes).
    Token-subset matching identical to :func:`isa95_synonym_candidates` so the
    two synonym surfaces cannot diverge in strictness. Overlay text was
    sanitized at write time and is used HERE for matching only — it is never
    rendered into a prompt by this path. Pure."""
    name_tokens = _name_tokens(column_name)
    if not name_tokens:
        return []
    out: list[str] = []
    for concept, synonyms in (overlay_synonyms or {}).items():
        for syn in synonyms or []:
            syn_tokens = _name_tokens(syn)
            if syn_tokens and syn_tokens <= name_tokens:
                out.append(concept)
                break
    return out


def _overlay_synonyms_by_concept() -> dict:
    """``{concept_id: [synonym terms]}`` from the ConceptOverlay read path.

    Reuses ``catalog.get_concept_overlays`` (lazy graph seam — ``{}`` when the
    graph is unavailable, so this surface degrades exactly like the prompt
    block). Splits each overlay's free-text ``synonyms`` field on ``,``/``;``.
    Best-effort."""
    from .catalog import get_concept_overlays
    out: dict[str, list[str]] = {}
    for cid, rows in get_concept_overlays().items():
        terms = [t.strip().lower()
                 for row in rows
                 for t in re.split(r"[,;]", str(row.get("synonyms") or ""))
                 if t.strip()]
        if terms:
            out[cid] = terms
    return out


def rank_candidates(*, semantic: dict | None = None, isa95=None,
                    overlay=None, value_overlap: list | None = None) -> list[dict]:
    """Produce a RANKED, basis-tagged candidate-concept list for one uncertain column.

    Each candidate carries a ``basis`` in {``value_overlap``, ``isa95_synonym``,
    ``overlay_synonym``, ``semantic``} and a ``confidence``. When a concept is
    supported by more than one basis, the strongest-basis / highest-confidence
    entry is kept (one entry per concept). Ordering: confidence desc, then basis
    strength desc — so STRONG value-evidence outranks a weak semantic guess
    (prefer value-evidence on disagreement), while a weak semantic guess is
    still surfaced when no strong evidence exists. ``overlay`` (T1.3) carries
    the ConceptOverlay synonym matches at the same mild strength as ``isa95``.
    Pure — no IO, no mutation of inputs."""
    cands: list[dict] = []
    for vc in value_overlap or []:
        c = dict(vc)
        c.setdefault("basis", "value_overlap")
        if _clean_concept(c.get("concept")):
            cands.append(c)
    for concept in isa95 or []:
        if _clean_concept(concept):
            cands.append({"concept": concept, "basis": "isa95_synonym",
                          "confidence": _ISA95_CONF})
    for concept in overlay or []:
        if _clean_concept(concept):
            cands.append({"concept": concept, "basis": "overlay_synonym",
                          "confidence": _ISA95_CONF})
    if semantic and _clean_concept(semantic.get("concept")):
        conf = 0.0
        try:
            conf = float(semantic.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        cands.append({"concept": semantic["concept"], "basis": "semantic", "confidence": conf})

    def _key(c):
        return (c.get("confidence", 0) or 0, _BASIS_RANK.get(c.get("basis"), 0))

    by_concept: dict = {}
    for c in cands:
        cid = c["concept"]
        cur = by_concept.get(cid)
        if cur is None or _key(c) > _key(cur):
            by_concept[cid] = c
    return sorted(by_concept.values(), key=_key, reverse=True)


# ---------------------------------------------------------------------------
# IO — peer population gathering + orchestration (best-effort)
# ---------------------------------------------------------------------------

def _peer_meta(system_id: str, fallback_schema: str = ""):
    """Resolve a peer system's (connection_type, schema, connection_config) for
    sampling — mirrors the target-metadata lookup used by the resolve step."""
    te = catalog.get(system_id) or {}
    cfg = te.get("connection_config") or {}
    if not cfg:
        try:
            from .connection_resolver import resolve_connection
            rc = resolve_connection(system_id) or {}
            cfg = {k: rc[k] for k in ("workgroup", "database", "catalog", "output_location", "region")
                   if rc.get(k)}
        except Exception:  # noqa: BLE001 - best-effort connection lookup
            cfg = {}
    return (te.get("connection_type", "PostgreSQL"), te.get("schema", "") or fallback_schema, cfg)


def _peer_concept_populations(system_id: str, *, per_concept_cap: int = _PEER_VALUE_CAP,
                              identity_only: bool = False) -> dict:
    """Gather value POPULATIONS for concepts established by PEER systems.

    One bounded graph query for every peer field→concept mapping, then bounded
    value sampling (reusing ``resolve_evidence.sample_field_values``) accumulated
    per concept up to ``per_concept_cap`` distinct values. Returns
    ``{concept: {"values": [...], "systems": [peer_sids]}}``; concepts with no
    sampled values are dropped. ``{}`` when no peers exist (bootstrapping no-op).
    ``identity_only`` keeps only peer edges whose values are identity evidence
    (:func:`is_identity_edge`), the populations map-time suggestion may use;
    the default keeps every edge.
    Best-effort — any error degrades to ``{}``."""
    rows = neptune_query(
        "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field)-[r:MAPS_TO]->(c:Concept) "
        "WHERE s.id <> $sid "
        "RETURN s.id AS sys_id, s.schema AS sys_schema, se.name AS table_name, "
        "f.name AS field_name, c.id AS concept, r.role AS role",
        {"sid": system_id},
    )
    if not rows:
        return {}
    meta: dict = {}
    pops: dict = {}
    for r in rows:
        concept = r.get("concept")
        if not _clean_concept(concept):
            continue
        if identity_only and not is_identity_edge(r.get("role"), r.get("field_name", "")):
            continue
        entry = pops.setdefault(concept, {"values": [], "systems": []})
        if len(entry["values"]) >= per_concept_cap:
            continue
        sid = r.get("sys_id")
        if sid not in meta:
            meta[sid] = _peer_meta(sid, r.get("sys_schema", ""))
        ct, sch, cfg = meta[sid]
        vals = sample_field_values(system_id=sid, connection_type=ct, schema=sch,
                                   table=r.get("table_name", ""), field=r.get("field_name", ""),
                                   connection_config=cfg)
        if identity_only and vals and numeric_measure(r.get("field_name", ""), vals):
            continue
        if vals:
            room = per_concept_cap - len(entry["values"])
            entry["values"].extend(vals[:room])
            if sid not in entry["systems"]:
                entry["systems"].append(sid)
    return {c: e for c, e in pops.items() if e["values"]}


def suggest_concepts_by_value_overlap(system_id: str, mappings, *, floor: float = _UNCERTAIN_FLOOR):
    """Upgrade/annotate UNCERTAIN map columns using measured value-overlap evidence.

    For each uncertain, identifier-ish column, sample its values (bounded) and
    MEASURE overlap against every peer concept population. Attach a ranked,
    basis-tagged ``candidates`` list to the mapping; when STRONG value evidence
    exists, upgrade the column's proposed ``concept``/``confidence`` and tag
    ``suggestion_basis='value_overlap'`` (a strong SUGGESTION — the hard
    cross-system link is still formed and confirmed downstream by resolve).

    Cost-gated to uncertain identifier-ish columns; peer populations are gathered
    once and cached; bounded sampling throughout. Bootstrapping no-op when no peer
    populations exist. Mutates ``mappings`` in place and returns it. Best-effort:
    any IO error leaves the semantic result untouched."""
    try:
        uncertain = [m for m in mappings if isinstance(m, dict) and is_uncertain(m, floor)]
        if not uncertain:
            return mappings

        peer_pops = _peer_concept_populations(system_id, identity_only=True)
        if not peer_pops:
            return mappings  # bootstrapping: no peers yet -> fall back to semantic

        entry = catalog.get(system_id) or {}
        sample_meta = {
            "connection_type": entry.get("connection_type", "PostgreSQL"),
            "schema": entry.get("schema", ""),
            "connection_config": entry.get("connection_config", {}) or {},
        }
        # T1.3: overlay synonyms feed the SAME name-signal surface as the
        # ISA-95 clause — fetched ONCE per run; {} when the graph is absent.
        overlay_syns = _overlay_synonyms_by_concept()
        for m in uncertain:
            _suggest_for_mapping(m, system_id=system_id, peer_pops=peer_pops,
                                 sample_meta=sample_meta,
                                 overlay_syns=overlay_syns)
        return mappings
    except Exception as e:  # noqa: BLE001 - suggestion is best-effort, never breaks mapping
        logger.info("suggest_concepts_by_value_overlap skipped (%s): %s", system_id, e)
        return mappings


def _suggest_for_mapping(m: dict, *, system_id: str, peer_pops: dict,
                         sample_meta: dict, overlay_syns: dict) -> None:
    """Evidence pass for ONE uncertain mapping (pure code motion out of the
    suggest loop — 50-line gate). Samples the column, ranks candidates across
    all bases, attaches ``candidates``, and upgrades only on STRONG value
    evidence. Mutates ``m`` in place."""
    tbl, fld = m.get("table", ""), m.get("field", "")
    col_name = (fld or "").split(".")[-1]
    vals = sample_field_values(system_id=system_id,
                               connection_type=sample_meta["connection_type"],
                               schema=sample_meta["schema"], table=tbl, field=fld,
                               connection_config=sample_meta["connection_config"])
    # Gate: a measure-named column holding numbers (a count, quantity, rate,
    # total ...): numbers coincide across unrelated systems, so its values are
    # not identity evidence. Leave the mapper's result.
    if numeric_measure(col_name, vals):
        return
    # Gate: only identifier-ish/coded columns — skip free-text & raw timestamps.
    if not looks_identifier_ish(vals):
        return

    vo_cands = _value_overlap_candidates(vals, peer_pops)
    semantic = None
    if _clean_concept(m.get("concept")):
        semantic = {"concept": m["concept"], "confidence": m.get("confidence", 0)}
    isa95 = isa95_synonym_candidates(col_name, CORE_CONCEPT_DEFINITIONS)
    overlay = overlay_synonym_candidates(col_name, overlay_syns)
    ranked = rank_candidates(semantic=semantic, isa95=isa95,
                             overlay=overlay, value_overlap=vo_cands)
    if not ranked:
        return

    m["candidates"] = ranked
    top = ranked[0]
    # Upgrade the proposed concept ONLY on strong value evidence — a strong,
    # still-confirmable SUGGESTION, never an auto-committed hard link.
    if top.get("basis") == "value_overlap":
        _apply_value_overlap_upgrade(m, top, system_id, tbl, col_name)


def _value_overlap_candidates(vals, peer_pops: dict) -> list[dict]:
    """Measured value-overlap candidates for one column against every peer
    concept population (pure code motion out of the suggest loop). Pure."""
    vo_cands: list[dict] = []
    for concept, pop in peer_pops.items():
        ov = value_overlap(vals, pop["values"])
        if strong_overlap(ov):
            vo_cands.append({
                "concept": concept,
                "basis": "value_overlap",
                "confidence": evidence_confidence(ov),
                "overlap_count": ov["overlap_count"],
                "ratio": ov["ratio"],
                "examples": ov["examples"],
                "peer_systems": list(pop["systems"]),
            })
    return vo_cands


def _apply_value_overlap_upgrade(m: dict, top: dict, system_id: str,
                                 tbl: str, col_name: str) -> None:
    """Apply a STRONG value-overlap suggestion to the mapping in place (pure
    code motion out of the suggest loop; mutates ``m``, logs the upgrade)."""
    m["concept"] = top["concept"]
    m["confidence"] = top["confidence"]
    m["suggestion_basis"] = "value_overlap"
    # Provision derives the Field-[:MAPS_TO]->Concept edges from
    # m["mappings"] (falling back to the top-level concept only when
    # mappings is EMPTY — onboard.py provision graph-write). An uncertain
    # column can carry a non-empty-but-all-low-confidence role-mapping
    # list, so we must OVERWRITE mappings/concepts here or provision would
    # persist the OLD low-confidence concept and silently discard this
    # evidence-backed suggestion (resolve would then never see it).
    m["mappings"] = [{
        "concept": top["concept"],
        "role": "identifier",
        "confidence": top["confidence"],
    }]
    m["concepts"] = [{
        "concept": top["concept"],
        "confidence": top["confidence"],
    }]
    logger.info(
        "concept_suggestion system_id=%s field=%s.%s upgraded concept=%s "
        "basis=value_overlap overlap=%d ratio=%.3f peers=%s",
        system_id, tbl, col_name, top["concept"], top.get("overlap_count", 0),
        top.get("ratio", 0), top.get("peer_systems"),
    )
