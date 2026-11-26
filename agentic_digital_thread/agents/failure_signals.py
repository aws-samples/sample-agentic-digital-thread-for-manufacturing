# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Failure-signal accumulator — ADR-0059 mechanism 2, Task 1B core.

Counts DETERMINISTIC failure shapes observed at runtime and, past a
threshold, files ONE steward question per shape-subject through the normal
proposal lifecycle (``steward_question`` grain — never-auto-accept, Wave 0).

Store: the self_learning DynamoDB table (key-prefix cohabitation,
``failure_signal#…`` — ADR-0059 D1: operational state never enters the
graph; JobStore rejected as per-replica/non-atomic). Rows carry a 90-day
TTL attribute (decay, the tombstone precedent) and a per-turn dedup set so
one turn never double-counts.

Import-pure: boto3 is reached only through ``self_learning._get_table()``
at call time. Every public write path is a never-raise boundary (the
coverage-gap precedent): a broken accumulator must never break a chat turn.

ADR-0063 D5 adds the ``context_not_reflected`` shape — a saved, enabled
business-context CORRECTION that subsequent answers still contradict — with
bounded, evidence-bearing accumulation (the edit's context hash plus the
answers that contradicted it) and :func:`requirement_phrasing`, the one source
of truth for the ANSWER-REQUIREMENT wording the promote-to-requirement route
files as an additive ``guidance_note`` proposal.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

#: ADR-0063 D5 shape: business context for a system was SAVED and enabled, and
#: subsequent answers still contradict it. Measured twice: two goldens resisted
#: context edits for days and flipped on the first attempt once the same
#: knowledge was phrased as an ANSWER REQUIREMENT (a ``guidance_note``).
CONTEXT_NOT_REFLECTED = "context_not_reflected"

#: Observations (distinct turns) required before a question is filed.
THRESHOLDS = {"terminology_miss": 3, "empty_join": 3, "unit_confusion": 2,
              # Two contradicting answers over one context version is the
              # measured shape; one is noise (a single off answer happens).
              CONTEXT_NOT_REFLECTED: 2}
#: Counter rows decay like tombstones (ADR-0053 precedent).
SIGNAL_TTL_DAYS = 90
#: Max questions filed per sweep — the queue was flooded once (496 pending,
#: 2026-08-06); elicitation must never re-flood it.
SWEEP_FILE_CAP = 5
#: Row-key prefix (self_learning table cohabitation — ADR-0059 D1).
SIGNAL_PREFIX = "failure_signal#"
#: Bounded evidence: how many contradicting-answer references ONE signal row
#: keeps. Evidence must be nameable, not exhaustive — after the cap the counter
#: keeps counting and the stored refs stay the FIRST ones seen (ADR-0063 D5:
#: bounded evidence storage; no claim without evidence).
MAX_EVIDENCE_REFS = 5
#: One evidence reference's length bound (ids, never answer text).
MAX_EVIDENCE_REF_LEN = 120
#: The table's SORT key (mapping_id + discovered_at composite — live-found
#: 2026-08-07: hash-only Keys are a ValidationException). Counter rows need
#: a STABLE sort value so updates hit one row; a timestamp would fork rows.
SIGNAL_SORT_VALUE = "signal#v1"


def _digest(subject: dict | str) -> str:
    """12-hex stable digest of the subject facts (sorted-JSON normalized)."""
    if isinstance(subject, dict):
        raw = json.dumps(subject, sort_keys=True, separators=(",", ":"))
    else:
        raw = str(subject).strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def shape_key(shape: str, system_id: str, subject: dict | str) -> str:
    """Canonical row key: ``failure_signal#<shape>|<system>|<digest12>``."""
    return f"{SIGNAL_PREFIX}{shape}|{system_id}|{_digest(subject)}"


def _ttl_epoch(now: float | None = None) -> int:
    return int((now or time.time()) + SIGNAL_TTL_DAYS * 86400)


def record_failure_signal(shape: str, system_id: str, subject: dict | str,
                          turn_id: str, evidence_ref: str = "") -> bool:
    """Count one observation of *shape* for (system, subject) — NEVER raises.

    Conditional update: the ``turn_ids`` string-set membership dedups so one
    turn increments at most once (a retried/streamed turn re-invoking the
    hook is a no-op). Returns True iff the observation was counted.

    ``evidence_ref`` (ADR-0063 D5) names WHAT was observed — an answer/session
    reference, never answer text. It is appended to a ``evidence_refs`` list
    bounded at :data:`MAX_EVIDENCE_REFS` by a ``size()`` condition in the SAME
    conditional write, so the bound holds under concurrency. A refused write is
    ambiguous (duplicate turn OR full evidence list), so it is retried ONCE
    without the append — the counter must keep counting past the evidence cap.
    """
    if shape not in THRESHOLDS or not system_id or not turn_id:
        return False
    ref = _bounded_ref(evidence_ref)
    if ref:
        counted = _count_observation(shape, system_id, subject, turn_id, ref)
        if counted is not None:
            return counted
    return bool(_count_observation(shape, system_id, subject, turn_id, ""))


def _bounded_ref(value) -> str:
    """One evidence reference, normalized and bounded ('' when unusable)."""
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return "".join(c for c in text if c.isprintable())[:MAX_EVIDENCE_REF_LEN]


def _observation_update(shape: str, system_id: str, subject: dict | str,
                        turn_id: str, ref: str) -> tuple[str, str, dict]:
    """The (UpdateExpression, ConditionExpression, values) triple for ONE
    observation. With *ref*, the bounded evidence append and its ``size()``
    guard ride the SAME single conditional write."""
    update = ("ADD turn_ids :tset, sig_count :one "
              "SET last_seen = :now, first_seen = if_not_exists(first_seen, :now), "
              "shape = :shape, system_id = :sys, subject_json = :subj, "
              "ttl_epoch = :ttl")
    condition = "attribute_not_exists(turn_ids) OR NOT contains(turn_ids, :turn)"
    values = {
        ":tset": {turn_id}, ":turn": turn_id, ":one": 1, ":now": _now_iso(),
        ":shape": shape, ":sys": system_id, ":ttl": _ttl_epoch(),
        ":subj": json.dumps(subject, sort_keys=True, default=str)
        if isinstance(subject, dict) else str(subject),
    }
    if ref:
        update += (", evidence_refs = list_append("
                   "if_not_exists(evidence_refs, :empty), :ref)")
        condition = (f"({condition}) AND (attribute_not_exists(evidence_refs) "
                     "OR size(evidence_refs) < :cap)")
        values.update({":empty": [], ":ref": [ref], ":cap": MAX_EVIDENCE_REFS})
    return update, condition, values


def _count_observation(shape: str, system_id: str, subject: dict | str,
                       turn_id: str, ref: str) -> bool | None:
    """One conditional write. True = counted, False = refused/failed, None =
    refused while carrying evidence (ambiguous — the caller retries bare)."""
    try:
        from .self_learning import _get_table
        update, condition, values = _observation_update(
            shape, system_id, subject, turn_id, ref)
        _get_table().update_item(
            Key={"mapping_id": shape_key(shape, system_id, subject),
                 "discovered_at": SIGNAL_SORT_VALUE},
            UpdateExpression=update, ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — never-raise boundary by contract
        if type(exc).__name__ == "ConditionalCheckFailedException":
            return None if ref else False  # duplicate turn — expected, silent
        logger.warning("record_failure_signal failed: category=%s",
                       type(exc).__name__)
        return False


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# ADR-0063 D5 — the context edit that did not take
#
# Measured twice: business context was written, enabled and verifiably reaching
# the agent, yet answers kept contradicting it for DAYS; both cases flipped on
# the FIRST attempt once the same knowledge was phrased as an ANSWER
# REQUIREMENT. Nothing detected the "edit demonstrably did not change behavior"
# case, so this shape does.
#
# Detection is DETERMINISTIC and cheap — no LLM anywhere (ADR-0053: computed
# verdicts only). It judges exactly ONE statement class it can judge honestly:
# a steward CORRECTION ("X, not Y" / "X rather than Y" / "never Y"). Purely
# descriptive context yields no directives and therefore no signal — honest
# absence beats a guess. Business context is never written or rewritten here
# (ADR-0054: the steward's text is the steward's); the promotion this signal
# offers is an ADDITIVE ``guidance_note`` PROPOSAL.
# --------------------------------------------------------------------------- #

#: Max corrective directives read out of ONE context version.
MAX_DIRECTIVES = 6
#: One directive term's length bound (both sides).
MAX_DIRECTIVE_TERM_LEN = 60
#: Bounded scan windows (the context cap is 2000; answers are unbounded text).
_MAX_CONTEXT_SCAN = 4000
_MAX_ANSWER_SCAN = 20000
#: Edge characters stripped off a parsed term.
_TERM_EDGE = " \"'`.,;:!?()[]"
#: One clause: no sentence/clause punctuation, bounded length.
_CLAUSE = r"[^,;:.!?\"()\[\]]{2,60}"
#: The corrective forms, in precedence order. Each yields a ``negated`` term
#: (the wording the steward ruled OUT) and optionally an ``asserted`` clause.
_CORRECTIVE_PATTERNS = (
    re.compile(rf"(?P<asserted>{_CLAUSE}?),\s*(?:and\s+)?not\s+"
               rf"(?P<negated>{_CLAUSE})", re.IGNORECASE),
    re.compile(rf"(?P<asserted>{_CLAUSE}?)\s+(?:rather than|instead of)\s+"
               rf"(?P<negated>{_CLAUSE})", re.IGNORECASE),
    re.compile(rf"\bnever\s+(?P<negated>{_CLAUSE})", re.IGNORECASE),
)


def _directive_term(value) -> str:
    """Normalize ONE term: whitespace-collapsed, printable-only, edge
    punctuation stripped, length-bounded. ``''`` when nothing usable."""
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    text = "".join(c for c in text if c.isprintable()).strip(_TERM_EDGE)
    return text[:MAX_DIRECTIVE_TERM_LEN].strip(_TERM_EDGE)


def _asserted_term(clause) -> str:
    """The asserted side reduced to its TRAILING token — the element parallel
    to the negated term in a correction ("recorded in <UnitA>, not <UnitB>" ⇒
    "<UnitA>"). A whole clause could never appear verbatim in an answer, which
    would make the asserted-absent test vacuous; one token can and does."""
    tokens = _directive_term(clause).split()
    return _directive_term(tokens[-1]) if tokens else ""


def context_directives(text) -> list[dict]:
    """PURE bounded parse of steward business context for CORRECTIONS.

    Returns up to :data:`MAX_DIRECTIVES` ``{"asserted", "negated"}`` dicts in
    text order, deduplicated on the negated term. Non-string, empty, or purely
    descriptive text returns ``[]``.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for segment in re.split(r"[.;\n!?]", text[:_MAX_CONTEXT_SCAN]):
        for pattern in _CORRECTIVE_PATTERNS:
            match = pattern.search(segment)
            if match is None:
                continue
            negated = _directive_term(match.group("negated"))
            asserted = _asserted_term(match.groupdict().get("asserted") or "")
            if not negated or negated == asserted or negated.lower() in seen:
                continue
            seen.add(negated.lower())
            out.append({"asserted": asserted, "negated": negated})
            if len(out) >= MAX_DIRECTIVES:
                return out
            break  # one directive per segment — the first pattern wins
    return out


def _term_present(term: str, text: str) -> bool:
    """Word-boundary, whitespace-flexible containment — NEVER a bare substring
    (the pinned-needle principle the error taxonomy already enforces)."""
    words = [re.escape(w) for w in str(term).split()]
    if not words:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def contradicted_directives(directives, answer_text) -> list[dict]:
    """The directives *answer_text* CONTRADICTS: the ruled-out wording appears
    and the asserted wording does not. Pure, deterministic, bounded.

    An answer that echoes the correction itself ("<UnitA>, not <UnitB>")
    carries the asserted term and is therefore NOT a contradiction.
    """
    if not isinstance(answer_text, str) or not answer_text.strip():
        return []
    text = answer_text[:_MAX_ANSWER_SCAN]
    return [d for d in (directives or [])
            if _term_present(d.get("negated", ""), text)
            and not _term_present(d.get("asserted", ""), text)]


def _context_version(context) -> str:
    """The SAVED and ENABLED context version's hash — ``''`` when the edit
    cannot be NAMED (unreadable graph, unset, disabled, or never saved). No
    nameable edit ⇒ no signal, by construction."""
    if not isinstance(context, dict) or not context.get("enabled"):
        return ""
    if not str(context.get("updated_at") or ""):
        return ""
    return _bounded_ref(context.get("context_hash"))


def record_context_not_reflected(system_id: str, context, answer_text,
                                 *, answer_ref: str, turn_id: str) -> list[dict]:
    """Accumulate ``context_not_reflected`` evidence — NEVER raises.

    *context* is the ``business_context.read_business_context`` shape
    (``business_context`` / ``enabled`` / ``updated_at`` / ``context_hash``).
    NOTHING accumulates unless every piece of evidence is present: a saved,
    enabled context version with a hash; a corrective directive inside it; an
    answer that contradicts that directive; and an ``answer_ref`` NAMING the
    answer. The row is keyed per (system, context version, directive), so a new
    edit starts a fresh count instead of inheriting the previous version's.

    Returns the subjects actually counted (``[]`` on absence or store failure).
    """
    try:
        ref = _bounded_ref(answer_ref)
        version = _context_version(context)
        if not ref or not version:
            return []
        hits = contradicted_directives(
            context_directives(context.get("business_context")), answer_text)
        recorded = []
        for directive in hits:
            subject = {"context_hash": version, "term": directive["negated"],
                       "asserted": directive["asserted"]}
            if record_failure_signal(CONTEXT_NOT_REFLECTED, system_id, subject,
                                     turn_id, evidence_ref=ref):
                recorded.append(subject)
        return recorded
    except Exception as exc:  # noqa: BLE001 — never-raise boundary
        logger.warning("record_context_not_reflected failed: category=%s",
                       type(exc).__name__)
        return []


def requirement_phrasing(asserted, negated) -> str:
    """The answer-REQUIREMENT sentence for a correction — the ONE source of
    truth (the promote-to-requirement route imports it, never re-words it).

    Business context is reference material the agent MAY use; a requirement is
    an instruction it MUST follow — the measured difference behind ADR-0063 D5.
    Both terms are bounded here, so an untrusted request body cannot smuggle a
    long or non-printable payload into the filed note. ``''`` when there is no
    ruled-out wording to require against.
    """
    ruled_out = _directive_term(negated)
    if not ruled_out:
        return ""
    required = _directive_term(asserted)
    if required:
        return f"REQUIRED: the answer must say '{required}', not '{ruled_out}'."
    return f"REQUIRED: the answer must not say '{ruled_out}'."


def _scan_signal_rows(table) -> list[dict]:
    """All failure_signal# rows, paginated (LastEvaluatedKey precedent)."""
    rows: list[dict] = []
    kwargs: dict = {}
    for _ in range(50):
        resp = table.scan(**kwargs)
        rows.extend(i for i in resp.get("Items", [])
                    if str(i.get("mapping_id", "")).startswith(SIGNAL_PREFIX))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return rows


def signals_over_threshold() -> list[dict]:
    """Signal rows whose count crossed their shape threshold. Never raises
    (returns [] on store failure — the sweep simply files nothing)."""
    try:
        from .self_learning import _get_table
        rows = _scan_signal_rows(_get_table())
    except Exception as exc:  # noqa: BLE001
        logger.warning("signals_over_threshold failed: category=%s",
                       type(exc).__name__)
        return []
    out = []
    for r in rows:
        shape = r.get("shape", "")
        if int(r.get("sig_count", 0) or 0) >= THRESHOLDS.get(shape, 10**9):
            out.append(r)
    out.sort(key=lambda r: (-int(r.get("sig_count", 0) or 0),
                            str(r.get("mapping_id", ""))))
    return out


def _subject_facts(row: dict) -> dict:
    """The stored subject as a facts dict for the question composer."""
    raw = row.get("subject_json", "")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"term": str(parsed)}
    except Exception:  # noqa: BLE001 — legacy/plain-string subjects
        return {"term": str(raw)} if raw else {}


def _compose_question(shape: str, facts: dict):
    """The ONE question for a shape: the DECLARATIVE bank
    (``RUNTIME_QUESTION_TEMPLATES``) first, then the shapes this sweep composes
    itself. Bank-preferring, so moving a template into the bank later needs no
    change here. Returns a ``Question`` or ``None`` (unmodelled shape)."""
    from .context_interview.questions import runtime_question
    question = runtime_question(shape, facts)
    if question is not None:
        return question
    if shape == CONTEXT_NOT_REFLECTED:
        return _context_requirement_question(facts)
    return None


def _context_requirement_question(facts: dict):
    """Compose the ADR-0063 D5 question. Placeholder-shaped and interpolated
    from BOUNDED subject facts (the neutrality ratchet scans this module); the
    id digests the composed prompt so one shape-subject yields one stable
    question (the ADR-0059 D3 grain digests the same text)."""
    from .context_interview.questions import (LAND_QUERY_GUIDANCE,
                                              PHASE_QUERY_TIME, SCOPE_RUNTIME,
                                              Question)
    term = _directive_term(facts.get("term")) or "that wording"
    system = _directive_term(facts.get("system")) or "this system"
    asserted = _directive_term(facts.get("asserted"))
    states = (f"states '{asserted}' rather than '{term}'" if asserted
              else f"rules out '{term}'")
    prompt = (
        f"The business context saved for {system} {states}, but later answers "
        f"still said '{term}'. Business context is reference material the agent "
        "MAY use; an answer requirement is an instruction it MUST follow. "
        "Promote this to an answer requirement, or state what the answer must "
        "say instead.")
    digest = hashlib.sha256(
        f"{CONTEXT_NOT_REFLECTED}|{prompt}".encode("utf-8")).hexdigest()[:12]
    return Question(
        id=f"runtime-context-not-reflected-{digest}",
        scope=SCOPE_RUNTIME, treatment_phase=PHASE_QUERY_TIME,
        topic="runtime-context-not-reflected", prompt=prompt,
        landing=LAND_QUERY_GUIDANCE,
        examples=("the answer must say <TermA>, never <TermB>",),
        goal=("The requirement the answer must satisfy, phrased as an "
              "instruction rather than as a fact."),
    )


def _signal_provenance(row: dict) -> str:
    """Evidence stamp for a filed question. For ``context_not_reflected`` it
    NAMES the edit (its context hash) and the answers that contradicted it —
    references only, bounded, never answer text (ADR-0063 D5: no claim without
    that evidence)."""
    parts = [f"failure-signal count={row.get('sig_count')}",
             f"first_seen={str(row.get('first_seen', ''))[:10]}"]
    version = _bounded_ref(_subject_facts(row).get("context_hash"))
    if version:
        parts.append(f"context_hash={version}")
    refs = [r for r in (_bounded_ref(x) for x in
                        (row.get("evidence_refs") or [])) if r]
    if refs:
        parts.append("contradicting_answers=" + ",".join(refs[:MAX_EVIDENCE_REFS]))
    return " ".join(parts)


def _file_question(row: dict) -> str | None:
    """Compose + file ONE steward question for an over-threshold signal.

    Files through the ONE writer (``log_proposed_mapping``,
    ``proposed_type="steward_question"`` — the grain's structural dedupe
    makes re-files ``dup_pending``). Returns the mapping id or None."""
    from .self_learning import log_proposed_mapping
    shape = row.get("shape", "")
    system_id = row.get("system_id", "")
    facts = dict(_subject_facts(row))
    facts.setdefault("system", system_id)
    q = _compose_question(shape, facts)
    if q is None:
        return None
    from_value = (_directive_term(facts.get("asserted"))
                  if shape == CONTEXT_NOT_REFLECTED else "")
    to_value = (_directive_term(facts.get("term"))
                if shape == CONTEXT_NOT_REFLECTED else "")
    return log_proposed_mapping(
        from_system=system_id, from_field="", from_value=from_value,
        to_system="", to_field="", to_value=to_value,
        concept=str(facts.get("concept", "") or facts.get("term", "")),
        confidence=1.0,  # the SIGNAL count is fact; the answer is the steward's
        query_context=f"context-interview:runtime-{shape}-{q.id}",
        proposed_type="steward_question", source="agent",
        description=q.prompt, reason=shape,
        provenance=_signal_provenance(row),
    )


def sweep_and_file_questions() -> dict:
    """Sweep entrypoint (the ``run_coverage_gap_check`` shape): file up to
    ``SWEEP_FILE_CAP`` questions for over-threshold signals, then supersede
    pending questions whose signal has decayed below threshold. NEVER raises.

    Steward-wins: the supersede is a conditional write on
    ``status = pending AND source = agent`` — a row the steward already
    touched is never machine-mutated (machine never writes steward statuses).
    """
    result = {"filed": [], "skipped": 0, "superseded": [], "error": ""}
    try:
        over = signals_over_threshold()
        live_keys = set()
        for row in over:
            live_keys.add(_question_subject(row))
            if len(result["filed"]) >= SWEEP_FILE_CAP:
                result["skipped"] += 1
                continue
            mid = _file_question(row)
            if mid:
                result["filed"].append(mid)
                _bump_question_telemetry(mid)
            else:
                result["skipped"] += 1
        result["superseded"] = _supersede_stale_questions(live_keys)
    except Exception as exc:  # noqa: BLE001 — never-raise boundary
        logger.warning("sweep_and_file_questions failed: category=%s",
                       type(exc).__name__)
        result["error"] = type(exc).__name__
    return result


def _question_subject(row: dict) -> str:
    """The (system, shape) identity a pending question is keyed on."""
    return f"{row.get('system_id', '')}|{row.get('shape', '')}"


def _bump_question_telemetry(mapping_id: str) -> None:
    """ADR-0052: the writer ships with usage telemetry — best-effort."""
    try:
        from .artifact_usage import bump_artifact_usage
        bump_artifact_usage([("steward_question", mapping_id)],
                            evidence="failure-signal sweep filed")
    except Exception as exc:  # noqa: BLE001
        logger.debug("question telemetry bump skipped: %s", type(exc).__name__)


def _supersede_stale_questions(live_keys: set) -> list[str]:
    """Replace-not-accrete: a PENDING machine question whose signal decayed
    is superseded (conditional: still pending AND machine-sourced)."""
    from .self_learning import _get_table, get_all_mappings
    superseded = []
    for m in get_all_mappings():
        if m.get("proposed_type") != "steward_question":
            continue
        if m.get("status") != "pending" or m.get("source") != "agent":
            continue
        if f"{m.get('from_system', '')}|{m.get('reason', '')}" in live_keys:
            continue
        try:
            _get_table().update_item(
                Key={"mapping_id": m["mapping_id"],
                     "discovered_at": m.get("discovered_at", "")},
                UpdateExpression=("SET #s = :sup, superseded_reason = :why, "
                                  "superseded_at = :now"),
                ConditionExpression="#s = :pending AND #src = :agent",
                ExpressionAttributeNames={"#s": "status", "#src": "source"},
                ExpressionAttributeValues={
                    ":sup": "superseded", ":pending": "pending",
                    ":agent": "agent", ":why": "signal-decayed",
                    ":now": _now_iso()},
            )
            superseded.append(m["mapping_id"])
        except Exception as exc:  # noqa: BLE001 — steward-wins condition lost
            logger.debug("supersede skipped for %s: %s",
                         m.get("mapping_id"), type(exc).__name__)
    return superseded
