<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0067: Shadow-only deterministic concept-evidence (label-vs-provenance) check

## Status

Accepted — 2026-09-08. Amends [ADR-0047](0047-response-integrity-enforcement-is-opt-in.md)
(the "annotation territory" boundary) and the `agents/concept_authority.py`
"no gate, no error type" contract.

## Context

A live-reproduced failure (3/3 after a context-level fix failed) put genuine,
fully evidenced rows from one canonical concept's field under a heading
claiming a different concept, for a site with no coverage of the claimed
concept. Every token in the answer was real, so every existing surface passed
it:

- **`annotate` (ADR-0047)** verifies numeric/identifier TOKENS against
  captured observations and blocks only the zero-evidence case
  (`response_integrity_annotate.py`). Mislabeled real payload verifies
  perfectly. ADR-0047 explicitly ruled everything short of zero-evidence
  fabrication "annotation territory, never blocking territory" — that boundary
  assumed the only semantic failure worth machine detection was fabrication.
  This defect is label-vs-provenance, a class that boundary never considered.
- **annotate runs inside the system agent** (`system_agent.
  _legacy_mode_a_answer`), before the orchestrator synthesis step where the
  relabeling happens.
- **The typed contract could not express the defect**: `SourceEvidence`
  carried `system_id`/`semantic_field`/`physical_field` but no canonical
  concept id, and `AnswerClaim` carried a model-authored free-text `label`
  with no concept binding. The runtime literally could not say "labeled
  concept X but sourced from evidence for concept Y".
- **The route-seed `concept_authority` block cannot be the gate**: its own
  contract is additive fail-open prompt context ("no gate, no error type"),
  and it is trimmed away under seed budget pressure. A gate must never read a
  prompt artifact.

## Decision

### 1. Server-derived field-to-concept bindings on trusted evidence

`SourceEvidence` gains three fields, filled ONLY by
`response_integrity_concepts.resolve_evidence_concepts` from already-read
graph field-to-concept mapping rows at a pinned opaque schema revision —
never model-authored:

- `concept_ids: tuple[str, ...]` — canonical concept ids the EXACT field maps to;
- `concept_binding: "unresolved" | "complete" | "capped"` — `complete` is the
  affirmative completeness marker and the only state permitting absence
  inference; `capped` means the mapping list hit a read cap;
- `concept_schema_revision: str` — the pinned revision the rows were read at.

The defaults (`()` / `"unresolved"` / `""`) mean "no server resolution ran",
which the check maps to `unknown`.

### 2. Claims propose; the server decides (ADR-0046 authority rules)

`AnswerClaim` gains optional `proposed_concept_id` — an LLM-proposed typed
draft field. It participates in NO binder, NO derivation, and NO rendering;
its only consumer is the deterministic shadow check.

### 3. The deterministic rule

`response_integrity_concepts.concept_shadow_check` runs in the validation
layer (`validate_final_answer`) alongside the existing claim binders, per
concept-proposing claim, over the claim's EXACT evidence fields:

- **`supported`** — some cited evidence field maps positively to the claimed
  concept. A positive mapping needs no completeness: a capped list may
  support, never convict. One system legitimately holding several concepts in
  different fields is handled by construction — only the cited field decides.
- **`violation` (`CONCEPT_EVIDENCE_MISMATCH`)** — every cited evidence field
  is affirmatively completely mapped at a matching pinned revision, all map
  positively to other concepts, and none maps to the claimed concept.
- **`unknown`** — anything less: graph read failure (unresolved binding),
  capped mapping set, unmapped physical field, unpinned or stale or
  disagreeing schema revision, or missing evidence. Absence is never inferred
  from a possibly-incomplete list.

The rule is computed; no LLM call participates (pinned by test).

### 4. Bounded sidecar

The verdict is a bounded dict (`context.concept_shadow`, logged by
`gate_final_result` as `concept-shadow: {...}`): verdict, claim id, claimed
concept, evidence concepts, source systems, reason word — capped in entry
count and serialized size. NO answer excerpts, NO raw rows, NO model
rationale.

### 5. SHADOW ONLY

Zero answer bytes change in any mode; nothing blocks; no `ReasonCode`/error
type is added (`CONCEPT_EVIDENCE_MISMATCH` exists only in the sidecar); no
mode flips. The shadow invocation fails open — an internal defect logs a
warning and never fails a turn. Enforcement is explicitly out of scope.

## What this amends

- **ADR-0047's annotation-territory boundary**: "unverified tokens never
  block" remains true, but the boundary's implicit claim — that token
  verification plus the zero-evidence block covers the machine-detectable
  failure classes — is amended: label-vs-provenance is a distinct,
  deterministically detectable class, measured (for now) rather than gated.
- **`concept_authority`'s "no gate" contract**: that block remains fail-open
  prompt context and still gates nothing. This ADR adds the gate-grade
  counterpart it disclaimed — reading graph-derived bindings carried on
  evidence, never the prompt artifact.

## Criteria that would later justify enforcement

Enforcement (e.g. re-labeling the claim by its evidence concept, or a new
reason code rejecting the claim) requires ALL of:

1. A measured shadow false-positive rate of ~zero over a full golden lane plus
   representative live traffic (the project was just burned by a
   false-positive detector; `violation` must be effectively always right).
2. The graph resolution hook wired end-to-end (see below) so `unknown` is the
   exception, not the norm — enforcing on a mostly-`unknown` signal is noise.
3. Steward-visible telemetry of every violation for a soak period, with the
   sidecar retained as the audit trail.
4. Its own ADR: enforcement changes answer bytes and needs an explicit
   error-type/rendering decision under ADR-0046's authority rules.

## Deferred: runtime graph-resolution hooks

`resolve_evidence_concepts` is pure and takes already-read rows (same pattern
as `concept_authority`). Wiring it requires call sites that hold graph rows:

- the orchestrator already reads per-system field-to-concept coverage for the
  route seed (`_ROUTE_SEED_CYPHER` / `FIELD_EVIDENCE_TRAVERSAL`); a per-field,
  revision-pinned variant of that read should feed the resolver — deferred
  because `agents/orchestrator.py` was under concurrent edit when this landed;
- alternatively the system agent can resolve bindings for its own evidence at
  envelope build. Until a hook lands, bindings stay unresolved and every
  verdict is honestly `unknown` — fail-open by design.

## Consequences

- The runtime can now EXPRESS "labeled X, evidenced Y"; before this the
  contract had no vocabulary for it.
- Old envelopes without the new fields still validate (additive defaults).
- The replay corpus (`tests/unit/test_concept_evidence_shadow.py`) pins the
  reproduced defect → `violation`, the same rows correctly labeled →
  `supported`, honest-limitation and owner-system answers → `supported`, and
  every uncertain basis → `unknown`, plus byte-identity, boundedness, no-LLM,
  and domain-neutrality pins.
