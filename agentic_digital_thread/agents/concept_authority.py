# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Computed per-concept AUTHORITY BOUNDARIES for the orchestrator synthesis layer.

WHY THIS EXISTS
---------------
Steward business context is delivered to SYSTEM AGENTS only (``system_agent.
_business_context_prompt``); the ORCHESTRATOR — the layer that writes the final
prose — never sees it, and it must not: business context is UNTRUSTED steward
text with no evidence weight, and the orchestrator prompt is already large
(ADR-0054). The measured consequence of that gap was a RELABELLING failure: a
system agent correctly returned and self-labelled a PROXY quantity, and the
orchestrator retitled the same table as the quantity the user had asked for,
for a site with no coverage of it at all.

The fix is not to forward the prose. It is to give the synthesis layer the one
piece of truth it lacked, in COMPUTED form: for a concept, which connected
system(s) the GRAPH supports as its source, and which connected systems carry
no field mapped to it at all. A system in the second set can only ever return a
related proxy, so its own label must survive into the answer.

CONTRACT
--------
* **Computed, never LLM-judged** — every field is derived from graph
  field-to-concept mapping coverage and ACTIVE ``AuthorityRule`` rows.
* **Honest and fail-open** — absent or unreadable input yields NO claim
  (``None``). A system is judged non-authoritative ONLY when its mapping
  coverage is genuinely KNOWN: a system with zero mapped concepts (for example
  a structural-only or telemetry-spine system) has UNKNOWN coverage, never
  "no coverage", and is never named. A coverage list that hit its read cap is
  likewise unknown.
* **Machine claims never vouch for machine claims** (ADR-0052) — an
  ``AuthorityRule`` naming a system that is KNOWN to carry no field mapping
  for its own concept is an EVIDENCE ORPHAN (a rule naming a system whose
  coverage is unknown — zero mapped concepts, or a capped list — is never
  called one). A machine-originated orphan is PREVENTED
  from establishing authority AND makes its concept undecidable (no boundary
  is claimed for it, so the orphan can never invert the boundary); a
  steward-confirmed orphan keeps authority but is labelled
  ``steward-asserted-rule`` so the orchestrator attributes rather than
  measures it. Either way the orphan is reported (detection).
* **Bounded** — the serialized block never exceeds :data:`MAX_BLOCK_CHARS`;
  every list, count and string has its own documented cap and any reduction is
  disclosed through ``truncated``.
* **No gate, no error type** — this is additive routing/synthesis context. It
  never fails a turn and never blocks a write.

Pure and stdlib-only: no graph client, no AWS, no LLM. The caller supplies
already-read rows (``agents.orchestrator.build_route_seed``).
"""
from __future__ import annotations

import json

# Steward-confirmed provenance methods (the ONE home — ``orchestrator.
# _provenance_tier`` imports this rather than restating the literals).
STEWARD_CONFIRMED_METHODS = frozenset({"user_confirmed", "crosswalk_confirmed"})

# The ONE spelling of FIELD-LEVEL concept evidence, as an openCypher path
# fragment to be concatenated between a System pattern and a Concept pattern:
#
#     f"MATCH (s:System){FIELD_EVIDENCE_TRAVERSAL}(c:Concept)"
#
# THE RULE IT ENCODES: only a ``Field``-level ``MAPS_TO`` counts as evidence
# that a system carries the concept. An ENTITY-level mapping
# (``(:SchemaEntry)-[:MAPS_TO]->(:Concept)``, ADR-0021's second layer) says the
# system holds records ABOUT the concept — it names no column that carries the
# value — so it must NEVER satisfy this predicate. Three DISTINCT computations
# rest on exactly this predicate and must never drift apart:
#
#   * ``learning_health._AUTHORITY_RULE_EVIDENCE_CYPHER`` — active-rule
#     evidence-orphan detection for the steward health surface;
#   * ``onboard._dp_field_evidenced_concepts`` — the mint/lifecycle gate that
#     decides which AuthorityRules this re-onboard may assert (and
#     ``onboard._dp_without_retired_only``, the same gate's check that a
#     concept has a field edge besides the links a steward retired, through
#     :func:`field_evidence_traversal`);
#   * ``orchestrator._ROUTE_SEED_CYPHER`` — the per-system ``mapped_concepts``
#     coverage column feeding :func:`build_concept_authority`.
#
# Keeping the computations separate is deliberate (different scopes, different
# failure modes); keeping the PREDICATE in one place is not optional — a
# divergence would make one surface report an evidence orphan that another
# surface considers evidenced.
FIELD_EVIDENCE_TRAVERSAL = (
    "-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(:Field)-[:MAPS_TO]->"
)


def field_evidence_traversal(field_var: str) -> str:
    """:data:`FIELD_EVIDENCE_TRAVERSAL` with its Field node bound to
    ``field_var``, for a read that must say which field carries the concept.
    Composed from the constant, so the predicate keeps its one home."""
    return FIELD_EVIDENCE_TRAVERSAL.replace("(:Field)", f"({field_var}:Field)", 1)

# Per-system mapped-concept read cap applied in the graph traversal. A system
# whose coverage list reaches this length is treated as UNKNOWN coverage (the
# list may be truncated, and a truncated list would manufacture a FALSE
# non-authority claim), so it is never named as non-authoritative.
COVERAGE_LIST_CAP = 500

# Block caps. MAX_BLOCK_CHARS is the HARD documented bound on the serialized
# block, enforced by dropping the lowest-priority entries until it fits; the
# route seed's own 8 KB budget is a separate, later bound.
#
# Sizing (measured): the fixed NOTE is ~650 chars and a boundary entry with
# realistic ids is ~175, so 6 boundaries plus 4 orphans land near 2 KB — a
# quarter of the seed budget. Deliberately allowed: under seed-budget pressure
# rule GRAINS degrade first (``orchestrator._trim_route_seed_rules``), and a
# missing join-path grain costs a re-query while a missing boundary costs a
# WRONG label. A deployment needing more than 6 concurrent boundaries has a
# coverage problem the steward should see in the graph, not in a prompt.
MAX_BOUNDARIES = 6
MAX_SYSTEMS_PER_SIDE = 4
MAX_ORPHANS = 4
MAX_TEXT_LEN = 64
MAX_BLOCK_CHARS = 2_000

TRUNCATION_MARKER = "…"

# Basis labels, weakest evidence LAST — the label of an entry is the weakest
# basis any of its authority holders rests on.
BASIS_FIELD_MAPPING = "concept-field-mapping"
BASIS_AUTHORITY_RULE = "authority-rule"
BASIS_STEWARD_ASSERTED = "steward-asserted-rule"

# Fixed, generic instruction shipped with the block. Deliberately short: it
# counts against MAX_BLOCK_CHARS. It states what the fields MEAN and what the
# absence of a concept means; it asserts nothing about any domain.
NOTE = (
    "Computed from graph field-to-concept coverage plus active authority rules. "
    "A system under not_authoritative carries NO field mapped to that concept: "
    "anything it returns for the concept is a RELATED PROXY, so keep the system "
    "agent's own label for it, state the substitution, and never retitle it as "
    "the requested concept. authority_basis=steward-asserted-rule rests on "
    "steward assertion, not measured evidence — attribute it. evidence_orphans "
    "are authority claims with no field-mapping evidence in the named system "
    "(prevented=true ⇒ not used to establish authority). A concept absent here "
    "has no computable boundary — make no authority claim about it."
)


def _text(value: object) -> str:
    """Bounded string form; empty for missing/blank input."""
    if value is None:
        return ""
    out = str(value).strip()
    if len(out) > MAX_TEXT_LEN:
        out = out[: MAX_TEXT_LEN - len(TRUNCATION_MARKER)].rstrip() + TRUNCATION_MARKER
    return out


def _coverage_index(coverage: object) -> "tuple[dict[str, set[str]], set[str]]":
    """Normalize ``{system_id: [concept, ...]}`` into ``(covered, judgeable)``.

    ``judgeable`` holds the systems whose coverage is genuinely KNOWN — at
    least one mapped concept and below :data:`COVERAGE_LIST_CAP`. Only those
    systems can ever be named non-authoritative.
    """
    covered: dict[str, set[str]] = {}
    judgeable: set[str] = set()
    if not isinstance(coverage, dict):
        return covered, judgeable
    for raw_system, concepts in coverage.items():
        system = _text(raw_system)
        if not system or not isinstance(concepts, (list, tuple, set)):
            continue
        names = {_text(name) for name in concepts}
        names.discard("")
        covered[system] = names
        if 0 < len(names) < COVERAGE_LIST_CAP:
            judgeable.add(system)
    return covered, judgeable


def _authority_index(rows: object, known_systems: "set[str]") -> "dict[str, list[dict]]":
    """Group active authority rows by concept, keeping only known systems."""
    grouped: dict[str, list[dict]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        concept = _text(row.get("concept"))
        system = _text(row.get("system_id"))
        if not concept or not system or system not in known_systems:
            continue
        grouped.setdefault(concept, []).append({
            "system": system,
            "rule_id": _text(row.get("id")),
            "steward": str(row.get("method") or "").strip().lower()
                       in STEWARD_CONFIRMED_METHODS,
        })
    for entries in grouped.values():
        entries.sort(key=lambda entry: (entry["system"], entry["rule_id"]))
    return grouped


# A rule whose named system's coverage is NOT judgeable (zero mapped concepts,
# or a list that reached COVERAGE_LIST_CAP and may be truncated) can never be
# called an evidence orphan: "carries no field mapped to this concept" is
# UNKNOWN there, and asserting ``prevented: true`` off a capped list would be a
# POSITIVE FALSE claim about the steward's graph — strictly worse than a
# boundary this run cannot sharpen. The module already refuses to name such a
# system non-authoritative; the orphan verdict obeys the same rule. Failing open
# lands on the boundary, never on the orphan claim, so the rule keeps authority.
def _resolve_authority(
    concept: str, cov: "set[str]", rules: "list[dict]", judgeable: "set[str]",
) -> "tuple[list[str], str, list[dict]]":
    """Split a concept's authority rules into holders, basis, and orphans.

    A rule whose named system also carries field-mapping evidence for the
    concept is a normal holder. A rule whose named system verifiably does NOT
    (see the judgeability note above) is an EVIDENCE ORPHAN:

    * steward-confirmed ⇒ it keeps authority, labelled
      :data:`BASIS_STEWARD_ASSERTED` so the claim is attributed, not measured;
    * otherwise ⇒ PREVENTED. A machine assertion that nothing vouches for
      cannot establish authority (ADR-0052), and it must not silently hand
      authority to whichever system happens to carry a field mapping either —
      that would INVERT the boundary. So the concept becomes undecidable:
      holders come back EMPTY and no boundary is claimed for it, while the
      orphan is still reported.
    """
    holders: list[str] = []
    orphans: list[dict] = []
    basis = BASIS_AUTHORITY_RULE if rules else BASIS_FIELD_MAPPING
    blocked = False
    for rule in rules:
        if rule["system"] in cov or rule["system"] not in judgeable:
            holders.append(rule["system"])
            continue
        prevented = not rule["steward"]
        orphan = {"concept": concept, "system": rule["system"],
                  "prevented": prevented}
        if rule["rule_id"]:
            orphan["rule_id"] = rule["rule_id"]
        orphans.append(orphan)
        if prevented:
            blocked = True
        else:
            holders.append(rule["system"])
            basis = BASIS_STEWARD_ASSERTED
    if blocked:
        return [], basis, orphans
    if not holders:
        return sorted(cov), BASIS_FIELD_MAPPING, orphans
    return sorted(set(holders)), basis, orphans


def _boundaries(
    covered: "dict[str, set[str]]",
    judgeable: "set[str]",
    grouped: "dict[str, list[dict]]",
) -> "tuple[list[dict], list[dict]]":
    """Compute every decidable boundary plus every detected evidence orphan."""
    concepts = {name for names in covered.values() for name in names}
    concepts.update(grouped)
    entries: list[dict] = []
    orphans: list[dict] = []
    for concept in sorted(concepts):
        cov = {system for system, names in covered.items() if concept in names}
        holders, basis, concept_orphans = _resolve_authority(
            concept, cov, grouped.get(concept) or [], judgeable)
        orphans.extend(concept_orphans)
        if not holders:
            continue
        missing = sorted(judgeable - cov - set(holders))
        if not missing:
            continue
        entries.append({"concept": concept, "authority": holders,
                        "authority_basis": basis, "not_authoritative": missing})
    return entries, orphans


def _cap_entries(entries: "list[dict]") -> "tuple[list[dict], bool]":
    """Order by decisiveness (deterministic tie-break) and apply the caps."""
    truncated = False
    entries.sort(key=lambda entry: (-len(entry["not_authoritative"]),
                                    entry["concept"]))
    if len(entries) > MAX_BOUNDARIES:
        entries, truncated = entries[:MAX_BOUNDARIES], True
    for entry in entries:
        for field in ("authority", "not_authoritative"):
            if len(entry[field]) > MAX_SYSTEMS_PER_SIDE:
                entry[field] = entry[field][:MAX_SYSTEMS_PER_SIDE]
                truncated = True
    return entries, truncated


def serialized_length(block: object) -> int:
    """Serialized size of the block exactly as the route seed encodes it."""
    return len(json.dumps(block, default=str, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")))


def _fit_block(block: dict) -> "dict | None":
    """Shrink the block until it honors :data:`MAX_BLOCK_CHARS`.

    Boundaries are dropped from the tail (least decisive first), then orphans;
    any reduction is disclosed. ``None`` when even an entry-free block cannot
    fit — no claim is preferable to an unbounded one.
    """
    while serialized_length(block) > MAX_BLOCK_CHARS:
        if block["boundaries"]:
            block["boundaries"].pop()
        elif block["evidence_orphans"]:
            block["evidence_orphans"].pop()
        else:
            return None
        block["truncated"] = True
    if not block["boundaries"] and not block["evidence_orphans"]:
        return None
    return block


def build_concept_authority(coverage: object, authority_rows: object) -> "dict | None":
    """Build the bounded concept-authority block, or ``None`` for no claim.

    ``coverage`` maps system id to the concept ids that system's schema fields
    map to (already read; ``COVERAGE_LIST_CAP``-bounded). ``authority_rows``
    are the ACTIVE authority rows (``id``/``concept``/``system_id``/
    ``method``); pass ``None`` when that read FAILED — unknown assertions mean
    no boundary can be claimed at all, so the result is ``None``.

    ``None`` is also returned when coverage is unusable, when fewer than two
    systems are known, and when nothing is decidable — so a turn with nothing
    to say carries a byte-identical route seed.
    """
    if authority_rows is None:
        return None
    covered, judgeable = _coverage_index(coverage)
    if len(covered) < 2 or not judgeable:
        return None
    grouped = _authority_index(authority_rows, set(covered))
    entries, orphans = _boundaries(covered, judgeable, grouped)
    entries, truncated = _cap_entries(entries)
    orphans.sort(key=lambda orphan: (orphan["concept"], orphan["system"],
                                     orphan.get("rule_id") or ""))
    if len(orphans) > MAX_ORPHANS:
        orphans, truncated = orphans[:MAX_ORPHANS], True
    if not entries and not orphans:
        return None
    return _fit_block({
        "note": NOTE,
        "boundaries": entries,
        "evidence_orphans": orphans,
        "boundary_cap": MAX_BOUNDARIES,
        "truncated": truncated,
    })
