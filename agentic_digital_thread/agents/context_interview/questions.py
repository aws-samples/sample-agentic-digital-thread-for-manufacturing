# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The domain-context question bank (T3.1 stage 1 + stage 2).

``docs/domain-context-intake.md`` establishes TEN questions whose answers
measurably change agent behavior. This module is their DECLARATIVE form — data,
not prose scattered through request handlers — plus the pure selection rule that
decides which questions a given system actually gets asked.

Two properties matter more than the content:

* **Deterministic, no LLM in the decision path** (contract C2 discipline). Which
  questions get asked, and in what order, is computed from schema facts. The
  conversational agent may rephrase a question; it never chooses the set.
* **Domain-neutral wording** (ADR-0044). Every prompt here composes into a
  steward-facing surface, and the neutrality ratchet scans this module. Examples
  must be placeholder-shaped (``<Site>/<Line>``), never a real customer's values.

The selection rule exists because nine questions per system across N systems is
fatigue, and fatigue produces empty answers — worse than not asking. So a
question is asked only when the schema gives it something to point AT
(``answerable``) and the answer can still change something (``material``), capped
per system and ordered map-time-first because map-time answers are the ones that
get expensive after onboarding.

Generic stdlib-only module: import-pure, safe to import anywhere.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field, replace

# Landing surfaces — where an answer goes. Consumed by
# ``context_interview.landing``; kept as plain strings so this module stays
# dependency-free.
LAND_BUSINESS_CONTEXT = "business_context_draft"
LAND_CONCEPT_OVERLAY = "concept_overlay"
LAND_CONCEPT_RETIRE = "concept_retire"
LAND_IDENTIFIER_SEMANTICS = "identifier_semantics"
LAND_AUTHORITY_INTENT = "authority_intent"
LAND_QUERY_GUIDANCE = "query_guidance"
LAND_DOMAIN_NOTE = "domain_note"

SCOPE_DEPLOYMENT = "deployment"
SCOPE_SYSTEM = "system"
# Runtime scope (ADR-0059): questions the failure-signal sweep composes AFTER
# onboarding. Never in ``QUESTIONS`` — runtime questions are composed on demand
# by :func:`runtime_question`, so onboarding selection cannot pick them up.
SCOPE_RUNTIME = "runtime"

PHASE_MAP_TIME = "map-time"
PHASE_QUERY_TIME = "query-time"

# Max questions asked per system. Four is a deliberate compromise: enough to
# cover the map-time set when a schema triggers all of them, few enough that a
# steward answers rather than skips.
MAX_QUESTIONS_PER_SYSTEM = 4

# Producer bounds for client-supplied discover tables (the raw-tables probe
# body is UNTRUSTED input). Capping per house style rather than rejecting: the
# question cap is 4 and triggers read at most 5 sample values, so facts beyond
# these bounds cannot change any selection outcome — they only cost CPU.
MAX_FACT_TABLES = 200
MAX_FACT_COLUMNS = 2000
MAX_SAMPLE_VALUES = 5

# Answer length bound. Interview answers are untrusted free text and land through
# the same sanitation as business context, whose ADR-0054 bound is 2000.
ANSWER_MAX_LEN = 2000


@dataclass(frozen=True)
class Question:
    """One intake question. ``trigger`` names the schema fact that makes it
    answerable; ``None`` means always answerable (deployment scope)."""

    id: str
    scope: str
    treatment_phase: str
    prompt: str
    landing: str
    trigger: str | None = None
    max_len: int = ANSWER_MAX_LEN
    # Deployment answers can pre-empt a per-system question of the same topic.
    topic: str = ""
    examples: tuple[str, ...] = field(default_factory=tuple)
    # When set, the answer can be DERIVED from schema facts with no human input,
    # so fully automated onboarding still produces context. Named deriver in
    # DERIVERS; a derivable question is never ASKED.
    deriver: str | None = None
    # The INFORMATION GOAL: what a complete answer contains. Consumed by the
    # bounded clarifier (T3.2) to decide whether ONE follow-up would materially
    # improve completeness — and by nothing else. Empty = never clarified.
    goal: str = ""


# --------------------------------------------------------------------------- #
# The bank. Order within a phase is the asking order.
# --------------------------------------------------------------------------- #
QUESTIONS: tuple[Question, ...] = (
    # ---- deployment scope (asked ONCE, before any system is onboarded) ----- #
    Question(
        id="unit-of-work",
        scope=SCOPE_DEPLOYMENT,
        treatment_phase=PHASE_MAP_TIME,
        topic="unit-of-work",
        prompt=("What is the basic unit of work this operation tracks — the thing "
                "one record represents as it moves through the process?"),
        landing=LAND_BUSINESS_CONTEXT,
        examples=("an order", "a job", "a discrete item on a route"),
        goal=("A named unit of work, whether the flow is discrete or batched, "
              "and what one record represents."),
    ),
    Question(
        id="negative-vocabulary",
        scope=SCOPE_DEPLOYMENT,
        treatment_phase=PHASE_MAP_TIME,
        topic="negative-vocabulary",
        prompt=("Which common manufacturing terms do NOT apply to this operation? "
                "Naming them stops the assistant quietly answering a different "
                "question than the one asked."),
        landing=LAND_CONCEPT_RETIRE,
        examples=("we have no batches or lots", "we do not track shifts"),
        goal=("At least one named term that does not apply, or an explicit "
              "statement that all common terms apply."),
    ),
    Question(
        id="authority-split",
        scope=SCOPE_DEPLOYMENT,
        treatment_phase=PHASE_QUERY_TIME,
        topic="authority",
        prompt=("When two systems both hold a value — for example a machine's "
                "state — which one should be treated as the source of truth?"),
        landing=LAND_AUTHORITY_INTENT,
        goal=("For each overlapping value the steward can name: which system "
              "wins, stated as a system-per-concept preference."),
    ),
    Question(
        id="kpi-definitions",
        scope=SCOPE_DEPLOYMENT,
        treatment_phase=PHASE_QUERY_TIME,
        topic="kpi-definitions",
        prompt=("Which calculated measures (KPIs) do people ask about most, and "
                "how is each one computed? Spell out the exact numerator and "
                "denominator — especially what counts in the denominator (the "
                "basis or 'opportunities') and which records are excluded. A "
                "KPI whose formula is not written down gets computed a "
                "different way each time someone asks."),
        landing=LAND_BUSINESS_CONTEXT,
        examples=("<rate KPI> = <defect events> / <units produced>, excluding "
                  "<bookkeeping states>",
                  "<on-time KPI> counts only <records with an achievable due "
                  "date> in its basis"),
        goal=("For each named KPI: its name (and any synonyms people use), the "
              "numerator, the exact denominator basis, and any exclusions — "
              "enough that two people would compute the same number."),
    ),
    # ---- per-system scope (asked after discover, before map) --------------- #
    Question(
        id="retry-semantics",
        scope=SCOPE_SYSTEM,
        treatment_phase=PHASE_MAP_TIME,
        topic="retry-semantics",
        trigger="state_and_attempt_columns",
        prompt=("This system records a state and an attempt or version. When work "
                "is retried, what happens to the earlier record — is it closed "
                "out as failed or cancelled as bookkeeping? If so, raw failure "
                "counts will overstate real problems."),
        landing=LAND_BUSINESS_CONTEXT,
        goal=("What happens to the earlier record on retry, and whether any "
              "closing state should be excluded from failure counts."),
    ),
    Question(
        id="failure-taxonomy",
        scope=SCOPE_SYSTEM,
        treatment_phase=PHASE_MAP_TIME,
        topic="failure-taxonomy",
        trigger="failure_code_column",
        prompt=("Which values in this system's failure or reason column are REAL "
                "problems, and which are routine workflow outcomes that should be "
                "excluded when someone asks about failures?"),
        landing=LAND_BUSINESS_CONTEXT,
        goal=("A partition of the code values into real problems versus "
              "routine workflow outcomes, with an explicit exclusion rule."),
    ),
    Question(
        id="identifier-family",
        scope=SCOPE_SYSTEM,
        treatment_phase=PHASE_MAP_TIME,
        topic="identifier-family",
        trigger="structured_identifier_values",
        prompt=("These columns look like identifiers. What shape do they follow, "
                "and does another system refer to the same real-world thing by a "
                "different identifier?"),
        landing=LAND_IDENTIFIER_SEMANTICS,
        examples=("<SITE>_<MACHINE>_<NN>", "an opaque UUID in the other system"),
        goal=("The identifier's shape or pattern, and whether another system "
              "names the same thing differently (and how the two relate)."),
    ),
    Question(
        id="equipment-naming",
        scope=SCOPE_SYSTEM,
        treatment_phase=PHASE_QUERY_TIME,
        topic="equipment-naming",
        trigger="equipment_name_columns",
        prompt=("What do people on the floor call the equipment involved here? "
                "Name any operational equipment terms that do NOT appear in this "
                "system's data — declaring them stops the assistant substituting "
                "a different machine when someone uses a floor name."),
        landing=LAND_DOMAIN_NOTE,
        examples=("the finishing unit everyone calls <FloorName> is not in "
                  "this system", "<FloorName> is a different machine from "
                  "<OtherName>"),
        goal=("Floor names for the equipment, which of them exist in this "
              "system's data, and which do not (so absence is declared, not "
              "guessed)."),
    ),
    Question(
        id="enum-semantics",
        scope=SCOPE_SYSTEM,
        treatment_phase=PHASE_QUERY_TIME,
        topic="enum-semantics",
        trigger="enum_coded_column",
        prompt=("This system stores coded values like the samples shown. What do "
                "these codes mean, and when someone asks a question, which code "
                "family should be used to answer it?"),
        landing=LAND_QUERY_GUIDANCE,
        goal=("What the code values mean in plain language, and which kinds of "
              "question each code family answers."),
    ),
    Question(
        id="coverage-boundary",
        scope=SCOPE_SYSTEM,
        treatment_phase=PHASE_QUERY_TIME,
        topic="coverage-boundary",
        trigger="always_null_column",
        deriver="derive_coverage_boundary",
        prompt=("Some columns here exist but are never populated, and coverage may "
                "not span every site. What does this system NOT cover? Declaring "
                "the gaps is what lets the assistant say \"no data\" instead of "
                "inventing a number."),
        landing=LAND_DOMAIN_NOTE,
    ),
    Question(
        id="query-strategy",
        scope=SCOPE_SYSTEM,
        treatment_phase=PHASE_QUERY_TIME,
        topic="query-strategy",
        trigger="large_table",
        deriver="derive_query_strategy",
        prompt=("This system holds large tables. How should it be queried for "
                "counts and distributions — for example aggregates rather than "
                "reading rows page by page?"),
        landing=LAND_QUERY_GUIDANCE,
    ),
)

QUESTIONS_BY_ID: dict[str, Question] = {q.id: q for q in QUESTIONS}

# --------------------------------------------------------------------------- #
# Trigger predicates over DISCOVERED SCHEMA FACTS (not live data reads).
# Each takes the schema-facts mapping and returns the evidence string to show the
# steward, or None when the question is not answerable for this system.
# The evidence matters: a question that cites the actual column gets a better
# answer than a generic one, and it proves we are not guessing.
# --------------------------------------------------------------------------- #
_STATE_RE = re.compile(r"(?:^|_)(state|status)(?:$|_)", re.I)
_ATTEMPT_RE = re.compile(r"attempt|version|revision|retry|seq", re.I)
_FAILURE_RE = re.compile(r"fail|error|reason|fault|defect|reject", re.I)
_ID_RE = re.compile(r"(?:^|_)(id|code|number|no|key|tag|serial)(?:$|_)", re.I)
_STRUCTURED_VALUE_RE = re.compile(r"^[A-Za-z0-9]+([_-][A-Za-z0-9]+){1,}$")

LARGE_TABLE_ROWS = 1_000_000


def _columns(facts: dict) -> list[dict]:
    return [c for c in (facts.get("columns") or []) if isinstance(c, dict)]


def _names(facts: dict) -> list[str]:
    return [str(c.get("name") or "") for c in _columns(facts)]


def trig_state_and_attempt_columns(facts: dict) -> str | None:
    states = [n for n in _names(facts) if _STATE_RE.search(n)]
    attempts = [n for n in _names(facts) if _ATTEMPT_RE.search(n)]
    if states and attempts:
        return f"columns {states[0]} and {attempts[0]}"
    return None


def trig_failure_code_column(facts: dict) -> str | None:
    hits = [n for n in _names(facts) if _FAILURE_RE.search(n)]
    return f"column {hits[0]}" if hits else None


def trig_structured_identifier_values(facts: dict) -> str | None:
    """Two or more identifier-ish columns, OR one whose sampled values look
    structured (delimited segments) — the shape that needs a crosswalk."""
    id_cols = [n for n in _names(facts) if _ID_RE.search(n)]
    for col in _columns(facts):
        for val in (col.get("sample_values") or [])[:5]:
            if isinstance(val, str) and _STRUCTURED_VALUE_RE.match(val):
                return f"column {col.get('name')} holds structured values"
    if len(id_cols) >= 2:
        return f"columns {id_cols[0]}, {id_cols[1]}"
    return None


def trig_always_null_column(facts: dict) -> str | None:
    for col in _columns(facts):
        if col.get("all_null") is True:
            return f"column {col.get('name')} is present but never populated"
    return None


def trig_large_table(facts: dict) -> str | None:
    rows = _finite_rows(facts)
    if rows is not None and rows >= LARGE_TABLE_ROWS:
        return f"about {rows:,} rows"
    return None


_EQUIPMENT_RE = re.compile(
    r"(?:^|_|\.)(equipment|machine|asset|device|station|unit)(?:$|_|\.|s(?:$|_|\.))",
    re.I)
# Enum-shaped sample value: multi-word CamelCase, an UPPER_SNAKE token of two or
# more words, or a "Family: Value" pair — coded vocabulary, not free text or an
# identifier (identifiers carry digits/delimited segments; see _STRUCTURED_VALUE_RE).
_ENUM_VALUE_RE = re.compile(
    r"^(?:[A-Z][a-z0-9]+){2,}$"                      # CamelCaseWords
    r"|^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,}$"           # UPPER_SNAKE_CODE
    r"|^[A-Za-z][\w]*: ?[A-Z]\w*$")                  # Family: CodeValue
_ENUM_NAME_RE = re.compile(
    r"(?:^|_|\.)(type|category|class|kind|code|status|mode|disposition|reason)"
    r"(?:$|_|\.)", re.I)


def trig_equipment_name_columns(facts: dict) -> str | None:
    """A column (or dotted path segment) that names equipment/assets — the
    schema fact that makes 'what does the floor call it?' answerable."""
    hits = [n for n in _names(facts) if _EQUIPMENT_RE.search(n)]
    if hits:
        return f"column {hits[0]} names equipment"
    return None


def trig_enum_coded_column(facts: dict) -> str | None:
    """A name-suggestive column whose sampled values look like coded vocabulary.
    The evidence carries the actual samples: recognition beats recall, and the
    steward annotates values they can see rather than values they must remember."""
    for col in _columns(facts):
        name = str(col.get("name") or "")
        if not _ENUM_NAME_RE.search(name):
            continue
        coded = [v for v in (col.get("sample_values") or [])[:MAX_SAMPLE_VALUES]
                 if isinstance(v, str) and _ENUM_VALUE_RE.match(v.strip())]
        if coded:
            shown = "; ".join(c.strip()[:60] for c in coded[:3])
            return f"column {name} holds coded values such as: {shown}"
    return None


def _finite_rows(facts: dict) -> int | None:
    """Row estimate as a finite int, else ``None``.

    Python's JSON parser accepts ``Infinity``/``NaN``, and ``int(inf)`` raises
    OverflowError — which reached the probe endpoint as a 500 for any
    authenticated caller. External data must never be trusted to be finite.
    """
    rows = facts.get("row_estimate")
    if isinstance(rows, bool) or not isinstance(rows, (int, float)):
        return None
    if not math.isfinite(rows) or rows < 0:
        return None
    return int(rows)


TRIGGERS = {
    "state_and_attempt_columns": trig_state_and_attempt_columns,
    "failure_code_column": trig_failure_code_column,
    "structured_identifier_values": trig_structured_identifier_values,
    "always_null_column": trig_always_null_column,
    "large_table": trig_large_table,
    "equipment_name_columns": trig_equipment_name_columns,
    "enum_coded_column": trig_enum_coded_column,
}


# --------------------------------------------------------------------------- #
# DERIVERS — answers a machine can write without asking anyone.
#
# The split matters for fully automated onboarding: an API caller that never runs
# an interview should still end up with real context rather than none. Only the
# questions whose answers encode human INTENT (unit of work, negative
# vocabulary, retry bookkeeping, real-vs-noise failure taxonomy) genuinely need a
# person; coverage gaps and query strategy are visible in the schema itself.
#
# Derived text is deliberately conservative and domain-neutral: it states the
# observation, never an interpretation the data cannot support.
# --------------------------------------------------------------------------- #
def derive_coverage_boundary(facts: dict) -> str | None:
    """Name columns observed empty, WITHOUT asserting they always will be.

    Dormant by design: ``all_null`` has no producer yet (discover does not
    measure null-ness). When one is added, the wording must stay a dated
    observation — a snapshot cannot support "never", and a permanent claim would
    instruct the agent to deny real data after a backfill.
    """
    empties = [str(c.get("name")) for c in _columns(facts)
               if c.get("all_null") is True and c.get("name")]
    if not empties:
        return None
    listed = ", ".join(sorted(empties)[:8])
    return (f"At last check these columns were present but held no values: "
            f"{listed}. Prefer saying the data is unavailable over inferring "
            f"values from them, and re-check before treating that as permanent.")


def derive_query_strategy(facts: dict) -> str | None:
    """State the aggregate-over-pagination rule when the table is large."""
    # Guard HERE too, not only in the trigger: relying on derive_answers gating
    # every deriver behind its trigger is exactly the trigger/deriver drift this
    # module warns about, and int(float("inf")) raises.
    rows = _finite_rows(facts)
    if rows is None or rows < LARGE_TABLE_ROWS:
        return None
    return (f"This system holds large tables (about {rows:,} rows). For "
            f"counts and distributions use aggregate queries "
            f"(COUNT(*)/GROUP BY with WHERE and LIMIT) rather than reading rows "
            f"page by page.")


DERIVERS = {
    "derive_coverage_boundary": derive_coverage_boundary,
    "derive_query_strategy": derive_query_strategy,
}


def schema_facts_from_table(table: dict) -> dict:
    """Normalize ONE discover table into the fact shape the rules consume.

    This is the producer whose absence made derivation unreachable: discover
    emits ``{"name", "columns": [{"name", "data_type"}], "row_count",
    "sample_values": {col: [...]}}``, which is NOT the fact shape. Mapping it
    here — once, in the same module as the consumers — is what keeps the two from
    drifting again.

    Deliberately does NOT synthesize ``all_null``: an absent sample list is not
    evidence a column is empty, and inventing that flag would make the agent deny
    data that exists. Columns are reported with whatever samples discover took.
    """
    if not isinstance(table, dict):
        return {"columns": [], "row_estimate": 0}
    samples = table.get("sample_values")
    samples = samples if isinstance(samples, dict) else {}
    columns = []
    for col in table.get("columns") or []:
        if not isinstance(col, dict):
            continue
        name = str(col.get("name") or "")
        if not name:
            continue
        vals = samples.get(name)
        columns.append({
            "name": name,
            "sample_values": list(vals)[:MAX_SAMPLE_VALUES]
                             if isinstance(vals, list) else [],
        })
    rows = table.get("row_count")
    facts = {"columns": columns}
    if isinstance(rows, (int, float)) and not isinstance(rows, bool):
        facts["row_estimate"] = rows
    return facts


def schema_facts_from_tables(tables) -> dict:
    """Merge a system's discover tables into ONE fact set for per-system probing.

    Questions are per SYSTEM, not per table, so the union of columns and the
    LARGEST row estimate is the right summary — a system with one huge table
    needs the aggregate-query guidance regardless of its small tables.

    Bounded: at most ``MAX_FACT_TABLES`` tables are read (the ``largest``
    estimate comes from the capped slice only) and at most ``MAX_FACT_COLUMNS``
    merged columns are kept — client-supplied tables are untrusted input.
    """
    columns: list[dict] = []
    seen: set[str] = set()
    largest = 0
    for table in (tables or [])[:MAX_FACT_TABLES]:
        facts = schema_facts_from_table(table)
        for col in facts["columns"]:
            if len(columns) >= MAX_FACT_COLUMNS:
                break
            if col["name"] not in seen:
                seen.add(col["name"])
                columns.append(col)
        rows = _finite_rows(facts)
        if rows and rows > largest:
            largest = rows
    return {"columns": columns, "row_estimate": largest}


@dataclass(frozen=True)
class DerivedAnswer:
    question: Question
    text: str
    evidence: str


def derive_answers(schema_facts: dict,
                   answered_topics: set[str] | None = None) -> list[DerivedAnswer]:
    """Answers a machine can supply from the discovered schema alone.

    Deterministic and side-effect free — the caller decides whether to land them.
    """
    done = answered_topics or set()
    out: list[DerivedAnswer] = []
    for q in QUESTIONS:
        if not q.deriver or q.topic in done:
            continue
        trigger = TRIGGERS.get(q.trigger or "")
        deriver = DERIVERS.get(q.deriver)
        if trigger is None or deriver is None:
            continue
        evidence = trigger(schema_facts)
        if not evidence:
            continue
        text = deriver(schema_facts)
        if text:
            out.append(DerivedAnswer(q, text, evidence))
    out.sort(key=lambda d: d.question.id)
    return out


@dataclass(frozen=True)
class SelectedQuestion:
    question: Question
    evidence: str


def _phase_rank(q: Question) -> int:
    """map-time first: those answers get expensive to apply after onboarding."""
    return 0 if q.treatment_phase == PHASE_MAP_TIME else 1


def deployment_questions(answered_topics: set[str] | None = None) -> list[SelectedQuestion]:
    """Stage-1 questions, asked ONCE per deployment. Already-answered topics drop."""
    done = answered_topics or set()
    out = [SelectedQuestion(q, "") for q in QUESTIONS
           if q.scope == SCOPE_DEPLOYMENT and q.topic not in done]
    out.sort(key=lambda s: _phase_rank(s.question))
    return out


def select_questions(schema_facts: dict,
                     answered_topics: set[str] | None = None,
                     *, limit: int = MAX_QUESTIONS_PER_SYSTEM
                     ) -> list[SelectedQuestion]:
    """Stage-2 per-system selection: ANSWERABLE ∧ MATERIAL, ranked, capped.

    ``schema_facts`` is the NORMALIZED fact shape — build it with
    :func:`schema_facts_from_table` from a discover table, never by hand. Shape:
    ``{"columns": [{"name", "sample_values", "all_null"}...],
    "row_estimate": int}``. ``all_null`` is OPTIONAL and currently has NO
    producer: discover reports columns and row counts but does not measure
    null-ness, so any question keyed on it stays dormant until a null-probe
    exists. Nothing fabricates it (see the note on ``derive_coverage_boundary``).
    ``answered_topics`` are topics already settled (e.g. at deployment scope) —
    those questions are not material a second time.

    Pure: no I/O, no LLM, deterministic for the same inputs.
    """
    done = answered_topics or set()
    picked: list[SelectedQuestion] = []
    for q in QUESTIONS:
        if q.scope != SCOPE_SYSTEM or q.topic in done:
            continue
        if q.deriver:
            continue  # derived, never asked — see derive_answers
        trigger = TRIGGERS.get(q.trigger or "")
        if trigger is None:
            continue
        evidence = trigger(schema_facts)
        if not evidence:
            continue
        picked.append(SelectedQuestion(q, evidence))
    picked.sort(key=lambda s: (_phase_rank(s.question), s.question.id))
    return picked[:max(0, limit)]


# --------------------------------------------------------------------------- #
# RUNTIME questions (ADR-0059) — the interview continues after onboarding.
#
# The failure-signal accumulator notices recurring failure shapes and asks;
# these templates are the DECLARATIVE wording, one per shape, and
# ``runtime_question`` is the deterministic composer the sweep calls. Same
# non-negotiables as the onboarding bank: no LLM chooses or words a question,
# and every template is placeholder-shaped (the neutrality ratchet scans this
# module — subject FACTS are interpolated at runtime, never baked in here).
#
# Landing reuses the existing machinery untouched: a composed runtime Question
# routes through ``landing.land_answer`` exactly like an onboarding one —
# identifier answers become pending ``value_mapping`` proposals, unit/meaning
# answers become pending ``guidance_note`` proposals. No new landing surface,
# no side door (ADR-0059 D4).
# --------------------------------------------------------------------------- #

# One interpolated subject fact's length bound. Subjects derive from live query
# traffic (user-authored terms, field names), so they are untrusted for shape
# and size; the full answer path re-sanitizes at write (land_answer).
RUNTIME_FACT_MAX_LEN = 80

# Neutral fill-ins when a signal row lacks a fact — a generic question still
# beats a hole in the prompt, and format_map must never KeyError.
_RUNTIME_FACT_FALLBACKS: dict[str, str] = {
    "term": "that term",
    "system": "this system",
    "other_system": "another system",
    "field": "that field",
    "concept": "that value",
}

RUNTIME_QUESTION_TEMPLATES: dict[str, Question] = {
    "terminology_miss": Question(
        id="runtime-terminology-miss",
        scope=SCOPE_RUNTIME,
        treatment_phase=PHASE_QUERY_TIME,
        topic="runtime-terminology-miss",
        prompt=("Questions that use the term '{term}' repeatedly fail to match "
                "anything in {system}. What does that term refer to in this "
                "operation, and does {system} record it under a different name "
                "or identifier?"),
        landing=LAND_IDENTIFIER_SEMANTICS,
        examples=("people say <FloorName>; the system stores it as <SYS>_<NN>",
                  "that term is not tracked in this system at all"),
        goal=("What the term refers to, and the name or identifier this system "
              "records it under — or an explicit statement that it is not in "
              "this system."),
    ),
    "empty_join": Question(
        id="runtime-empty-join",
        scope=SCOPE_RUNTIME,
        treatment_phase=PHASE_QUERY_TIME,
        topic="runtime-empty-join",
        prompt=("Queries joining {system} and {other_system} repeatedly return "
                "no rows when matching on {field}. Do the two systems refer to "
                "the same real-world things by different identifiers — and if "
                "so, how does one translate to the other?"),
        landing=LAND_IDENTIFIER_SEMANTICS,
        examples=("one side uses <SITE>_<NN>, the other a plain number for the "
                  "same thing", "values match after removing the <PREFIX>-"),
        goal=("Whether the two systems name the same things differently, and "
              "the translation between the identifiers (or a statement that "
              "the systems genuinely do not overlap)."),
    ),
    "unit_confusion": Question(
        id="runtime-unit-confusion",
        scope=SCOPE_RUNTIME,
        treatment_phase=PHASE_QUERY_TIME,
        topic="runtime-unit-confusion",
        prompt=("Answers involving {concept} have repeatedly drawn corrections "
                "that point to a unit or meaning mismatch. What unit is "
                "{concept} recorded in, and what exactly does one recorded "
                "value represent?"),
        landing=LAND_DOMAIN_NOTE,
        examples=("recorded per <container>, not per <item>",
                  "the value is a rate over <period>, not a count"),
        goal=("The unit the value is recorded in, what one recorded value "
              "represents, and any conversion people apply when discussing it."),
    ),
}


def _runtime_fact(value, fallback: str) -> str:
    """Normalize and bound ONE subject fact; non-strings and blanks fall back."""
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return text[:RUNTIME_FACT_MAX_LEN] if text else fallback


def runtime_question(shape: str, subject) -> Question | None:
    """Compose the ONE question for a failure shape. Pure and deterministic.

    ``shape`` keys :data:`RUNTIME_QUESTION_TEMPLATES` (unknown ⇒ ``None`` —
    the sweep skips shapes it does not model rather than raising).
    ``subject`` is the signal row's fact mapping (``term``/``system``/
    ``other_system``/``field``/``concept`` as available); anything else —
    missing keys, non-dict, non-string values — degrades to neutral fallbacks,
    never an exception. The id carries a digest of the composed prompt so one
    shape-subject yields one stable question and a reworded subject yields a
    new one (the proposal grain's dedup key digests the same text, ADR-0059 D3).
    """
    template = RUNTIME_QUESTION_TEMPLATES.get(shape)
    if template is None:
        return None
    raw = subject if isinstance(subject, dict) else {}
    facts = {key: _runtime_fact(raw.get(key), fallback)
             for key, fallback in _RUNTIME_FACT_FALLBACKS.items()}
    prompt = template.prompt.format_map(facts)
    digest = hashlib.sha256(f"{shape}|{prompt}".encode()).hexdigest()[:12]
    return replace(template, id=f"{template.id}-{digest}", prompt=prompt)
