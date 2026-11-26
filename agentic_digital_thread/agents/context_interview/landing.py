# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Landing interview answers into the ONE proposal lifecycle (T3.1 T-B).

An interview answer is the newest untrusted-input surface in the product: free
text, authored by a steward, destined for an LLM prompt. Two rules keep it safe,
and both are enforced here rather than trusted to callers:

1. **Sanitize at write, fence at composition** (T1.7 / ADR-0054 D4-D5). Every
   text field goes through ``sanitize_untrusted`` + ``validate_untrusted``;
   rejection reasons are returned verbatim so the UI can tell the steward what to
   fix. Nothing is written on rejection.
2. **Everything lands as a PROPOSAL** (ADR-0003). No side-door writes: no direct
   Neptune CRUD, no direct AuthorityRule upsert, no business-context PUT. A
   steward accepts (or a pack install pre-accepts); uninstall and audit come free
   because there is exactly one lifecycle.

The business-context landing is deliberately different: BC is a PUT that rolls
runtimes (ADR-0054 + the T1.5 coordinator), so an interview must NOT write it per
answer. Answers accumulate into a DRAFT that a steward reviews and confirms once,
batching every affected system into a single serial roll cycle.

Authority answers are the sharpest edge. ``authoritative_system(concept)`` is
deployment-scoped and a second active rule for the same concept makes resolution
nondeterministic (the live failure class T1.4 now fails loud on). So an authority
answer NEVER writes a rule: it records an intent, and if the concept already has
an active rule naming a different system it returns a ``conflict`` for explicit
steward resolution.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .questions import (
    LAND_AUTHORITY_INTENT,
    LAND_BUSINESS_CONTEXT,
    LAND_CONCEPT_OVERLAY,
    LAND_CONCEPT_RETIRE,
    LAND_DOMAIN_NOTE,
    LAND_IDENTIFIER_SEMANTICS,
    LAND_QUERY_GUIDANCE,
    Question,
)

#: Landings whose answer is HELD until a steward confirms it (the
#: business-context draft, in memory until ``/guided/confirm-context`` writes
#: it). Every other landing files the answer when it is given. A client with
#: no confirm step (the wizard's pre-map card) asks only for the others.
HELD_FOR_CONFIRM = frozenset({LAND_BUSINESS_CONTEXT})

# Outcome kinds. ``draft`` means "held for steward confirm", not "written".
OUTCOME_PROPOSED = "proposed"
OUTCOME_DRAFT = "draft"
OUTCOME_CONFLICT = "conflict"
OUTCOME_REJECTED = "rejected"
OUTCOME_SKIPPED = "skipped"
#: A nonempty answer whose proposal the lifecycle did not file (gated or
#: staged): reported as a skip here; the onboarding interview re-asks it.
NOT_INGESTED = "not_ingested"


@dataclass
class LandingResult:
    """What happened to one answer. ``reason``/``detail`` mirror the pinned
    sanitation reasons so the UI can render an actionable message; ``detail``
    never echoes the answer text (secret-shaped content must not leak)."""

    outcome: str
    question_id: str
    landing: str = ""
    reason: str = ""
    detail: str = ""
    proposal_ids: list[str] = field(default_factory=list)
    draft_text: str = ""
    conflict_with: str = ""
    # The SANITIZED answer for non-rejected outcomes — safe for the review
    # manifest to display (rejected text is never carried; see the module
    # docstring). Empty for skips.
    clean_text: str = ""


def _sanitize(text, max_len: int, field_label: str):
    """Sanitize + validate via the SHARED T1.7 helper. Returns
    ``(clean_text, None)`` or ``(None, LandingResult-args tuple)``."""
    from ..untrusted_text import sanitize_untrusted, validate_untrusted

    clean = sanitize_untrusted(text, max_len=max_len)
    verdict = validate_untrusted(clean, max_len=max_len,
                                 field=f"'{field_label}'", label="answer")
    if verdict is not None:
        return None, verdict
    return clean, None


def _rejected(question: Question, verdict) -> LandingResult:
    reason, detail = verdict
    return LandingResult(outcome=OUTCOME_REJECTED, question_id=question.id,
                         landing=question.landing, reason=reason, detail=detail)


def land_answer(question: Question, answer_text, *,
                system_id: str = "", actor: str = "interview",
                concept_id: str = "") -> LandingResult:
    """Land ONE interview answer. Never writes outside the proposal lifecycle.

    An empty/whitespace answer is a legitimate SKIP — a steward who does not know
    must be able to move on, and a blank forced answer is worse than none.
    """
    clean, verdict = _sanitize(answer_text, question.max_len, question.id)
    if verdict is not None:
        return _rejected(question, verdict)
    if not clean:
        return LandingResult(outcome=OUTCOME_SKIPPED, question_id=question.id,
                             landing=question.landing)

    if question.landing == LAND_BUSINESS_CONTEXT:
        # DRAFT ONLY — see the module docstring on batching runtime rolls.
        return LandingResult(outcome=OUTCOME_DRAFT, question_id=question.id,
                             landing=question.landing, draft_text=clean,
                             clean_text=clean)

    if question.landing == LAND_AUTHORITY_INTENT:
        return _land_authority_intent(question, clean, concept_id=concept_id,
                                      system_id=system_id, actor=actor)

    return _land_as_proposal(question, clean, system_id=system_id, actor=actor,
                             concept_id=concept_id)


_PROPOSED_TYPE_BY_LANDING = {
    LAND_CONCEPT_OVERLAY: "concept_overlay",
    LAND_CONCEPT_RETIRE: "concept_vocab",
    LAND_IDENTIFIER_SEMANTICS: "value_mapping",
    LAND_QUERY_GUIDANCE: "guidance_note",
    LAND_DOMAIN_NOTE: "guidance_note",
}

#: Proposed types whose item contract carries the answer text in ``to_value``
#: (``self_learning``: guidance_note, ``from_system`` = system id, ``to_value``
#: = note text). The review card, delivery preview, acceptance,
#: reconciliation, retire and the guidance canonical key all read
#: ``to_value``; with the text in ``description`` alone the note reads as
#: empty and every interview note on one system shares one subject.
_ANSWER_IN_TO_VALUE = frozenset({"guidance_note"})


def _land_as_proposal(question: Question, clean: str, *, system_id: str,
                      actor: str, concept_id: str) -> LandingResult:
    """Route through ``log_proposed_mapping`` — the ONE ingestion path.

    Pre-accept is deliberately NOT set: an interview answer is human judgment
    about semantics, exactly the class ADR-0050 keeps off the auto-accept path.
    ``log_proposed_mapping`` may return ``None`` (ingestion gate drop or staging
    hold); that is reported honestly rather than treated as success.
    """
    from ..self_learning import log_proposed_mapping

    proposed_type = _PROPOSED_TYPE_BY_LANDING.get(question.landing, "value_mapping")
    to_value = clean if proposed_type in _ANSWER_IN_TO_VALUE else ""
    mapping_id = log_proposed_mapping(
        from_system=system_id or "deployment",
        from_field="", from_value="",
        to_system="", to_field="", to_value=to_value,
        concept=concept_id or "",
        confidence=0.0,
        query_context=f"context-interview:{question.id}",
        proposed_type=proposed_type,
        # ADR-0052 keeps steward and machine claims distinct: a DERIVED answer
        # must not be recorded as something a person asserted.
        source="derived" if actor == "derived" else "steward",
        description=clean,
        method="context_interview",
        provenance=f"interview:{actor}",
    )
    if not mapping_id:
        return LandingResult(outcome=OUTCOME_SKIPPED, question_id=question.id,
                             landing=question.landing,
                             reason=NOT_INGESTED,
                             detail="the proposal was gated or staged upstream")
    return LandingResult(outcome=OUTCOME_PROPOSED, question_id=question.id,
                         landing=question.landing, proposal_ids=[mapping_id],
                         clean_text=clean)


def _land_authority_intent(question: Question, clean: str, *, concept_id: str,
                           system_id: str, actor: str) -> LandingResult:
    """Record an authority INTENT; refuse to create a second active rule.

    Reading the existing rules first is what prevents the nondeterministic
    two-rules-one-concept state: authority is deployment-scoped, so a competing
    rule does not overwrite — it coexists and resolution flip-flops.
    """
    existing = _active_authority_system(concept_id) if concept_id else ""
    if existing and system_id and existing != system_id:
        return LandingResult(
            outcome=OUTCOME_CONFLICT, question_id=question.id,
            landing=question.landing, conflict_with=existing,
            reason="authority_conflict",
            detail=(f"concept already has an active authority rule naming "
                    f"'{existing}'; deactivate it explicitly before assigning "
                    f"another system"))
    return _land_as_proposal(question, clean, system_id=system_id, actor=actor,
                             concept_id=concept_id)


def _active_authority_system(concept_id: str) -> str:
    """Return the system named by the concept's active AuthorityRule, or ``""``.

    Graph unavailable ⇒ ``""`` (no conflict asserted). Same lazy-seam discipline
    the rest of the engine uses: never import a client at module import.
    """
    try:
        from ..db import neptune_query
        rows = neptune_query(
            "MATCH (a:AuthorityRule {concept: $cid}) "
            "WHERE coalesce(a.status, 'active') <> 'deactivated' "
            "RETURN a.system_id AS system_id LIMIT 2", {"cid": concept_id})
    except Exception:  # nosec B110 - intentional fallback; absence is not a conflict
        return ""
    systems = {r.get("system_id") for r in (rows or []) if r.get("system_id")}
    return next(iter(systems)) if len(systems) == 1 else ""


def merge_draft_context(existing: str, additions: list[str], *,
                        max_len: int = 2000) -> str:
    """Compose the business-context draft from held answers, bounded.

    Truncation is a REJECTION upstream (the sanitizer), so here we only need to
    stop the merge from exceeding the bound: additions that would overflow are
    dropped whole rather than cut mid-sentence, because a half sentence in a
    prompt is worse than an absent one.
    """
    out = (existing or "").strip()
    for add in additions:
        piece = " ".join(str(add or "").split())
        if not piece:
            continue
        candidate = f"{out} {piece}".strip() if out else piece
        if len(candidate) > max_len:
            continue
        out = candidate
    return out
