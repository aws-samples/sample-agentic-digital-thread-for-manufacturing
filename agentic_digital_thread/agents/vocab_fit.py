# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic PRE-map vocabulary-fit report (ADR-0068).

Answers ONE question before any mapping happens: *does the current concept
vocabulary have something to say about this system's discovered columns?*
The onboarding agent's ``assess_vocabulary_fit`` tool calls :func:`compute_fit`
and relays the result verbatim — the fit signal is COMPUTED here, never
LLM-judged (the same discipline as ``agents/determinism.py``), and the numbers
mirror the POST-map ``_vocab_fit_block`` counters in ``onboard.py`` without
requiring mappings to exist yet.

Candidate detection is a normalized-token match of each column name against
concept ids, names, and the ``Synonyms (…):`` clause concept descriptions
carry. That is a vocabulary-vs-schema comparison — no dataset literal and no
domain-specific name heuristic is encoded here (the ``concept_suggest.py``
header rules). Generic bookkeeping columns (bare ``id``, audit timestamps)
are counted but EXCLUDED from the signal: a vocabulary is not "fitting" a
schema by matching its ``created_at`` columns.

``extract_synonyms``/``_SYNONYM_RE`` live HERE because this module must stay
import-pure (stdlib only — the ``context_interview/questions.py`` discipline)
while ``concept_suggest.py`` imports the graph/db seams at module level.
``concept_suggest`` re-exports from this home so the parser has exactly
one implementation (do not add a second regex).

Generic stdlib-only module: import-pure, deterministic, safe to import in the
onboarding runtime and the app process alike.
"""
from __future__ import annotations

import re

# Suggestion thresholds (percent of signal columns with at least one
# vocabulary candidate). At or above FIT_OK_PCT the vocabulary fits well
# enough to proceed ("ok"); between the two, steward context is worth asking
# for ("ask_context"); below FIT_LOW_PCT a domain pack is likely the faster
# fix ("consider_pack"). Pinned module constants — tests assert the exact
# boundary behavior.
FIT_OK_PCT = 70
FIT_LOW_PCT = 40

SUGGEST_OK = "ok"
SUGGEST_ASK_CONTEXT = "ask_context"
SUGGEST_CONSIDER_PACK = "consider_pack"

# Producer bounds for the discover-table input (untrusted size, same posture
# and values as ``context_interview.questions.MAX_FACT_TABLES/-COLUMNS``).
MAX_TABLES = 200
MAX_COLUMNS = 2000

# Names reported but excluded from the fit signal: bare surrogate keys and
# audit-timestamp bookkeeping present in nearly every schema. Deliberately
# narrow — a qualified key like ``widget_id`` is a real foreign identifier
# and stays in the signal.
_GENERIC_COL_RE = re.compile(
    r"^(?:id|uuid|guid|pk|row_id|rowid)$"
    r"|^(?:created|updated|modified|deleted|inserted)(?:_(?:at|on|by|date|time|ts))?$"
    r"|^last_(?:updated|modified|changed)(?:_at|_on|_date|_time|_ts)?$"
    r"|^(?:timestamp|created_timestamp|updated_timestamp)$",
    re.IGNORECASE,
)

# "Synonyms (…): a, b, c." clause the concept definitions carry — the SAME
# idiom as ``concept_suggest._SYNONYM_RE`` (single home is here; see module
# docstring).
_SYNONYM_RE = re.compile(r"Synonyms\s*\([^)]*\)\s*:\s*([^.]+)\.", re.IGNORECASE)

# Cap on the name-only unmatched-columns sample carried in the report.
_UNMATCHED_CAP = 20


def extract_synonyms(definition_text: str) -> list[str]:
    """Pull the lower-cased synonym terms from a concept definition's
    ``Synonyms (…): a, b, c.`` clause. Returns [] when no such clause exists.
    Pure."""
    m = _SYNONYM_RE.search(definition_text or "")
    if not m:
        return []
    return [p.strip().lower() for p in re.split(r"[,;]", m.group(1)) if p.strip()]


def _name_tokens(text: str) -> frozenset[str]:
    """Normalized token set of a name-ish string (lower-cased, split on any
    non-alphanumeric run). Pure."""
    return frozenset(t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t)


def is_generic_column(name: str) -> bool:
    """True for bookkeeping columns excluded from the fit signal (bare
    surrogate keys, audit timestamps). Pure."""
    return bool(_GENERIC_COL_RE.match((name or "").strip()))


def _concept_term_sets(concepts: list[dict] | None) -> list[frozenset[str]]:
    """Token sets a column name can match against: one per concept id, name,
    and description-synonym. Empty token sets are dropped (they would match
    everything). Pure."""
    terms: list[frozenset[str]] = []
    for c in concepts or []:
        if not isinstance(c, dict):
            continue
        for text in (c.get("id"), c.get("name")):
            toks = _name_tokens(str(text)) if text else frozenset()
            if toks:
                terms.append(toks)
        for syn in extract_synonyms(str(c.get("description") or "")):
            toks = _name_tokens(syn)
            if toks:
                terms.append(toks)
    return terms


def _column_names(tables: list[dict] | None) -> list[str]:
    """Flatten discover tables into a bounded, order-preserving column-name
    list (``MAX_TABLES``/``MAX_COLUMNS`` caps — untrusted input size). Pure."""
    names: list[str] = []
    for table in (tables or [])[:MAX_TABLES]:
        if not isinstance(table, dict):
            continue
        for col in table.get("columns") or []:
            if len(names) >= MAX_COLUMNS:
                return names
            if isinstance(col, dict) and col.get("name"):
                names.append(str(col["name"]))
    return names


def _suggestion(fit_pct: int, signal_columns: int) -> str:
    """Threshold rule. Zero signal columns ⇒ ``ok``: nothing to fit is not a
    vocabulary gap, and the report must never nag on empty input. Pure."""
    if signal_columns <= 0 or fit_pct >= FIT_OK_PCT:
        return SUGGEST_OK
    if fit_pct >= FIT_LOW_PCT:
        return SUGGEST_ASK_CONTEXT
    return SUGGEST_CONSIDER_PACK


def compute_fit(tables: list[dict], concepts: list[dict]) -> dict:
    """The deterministic pre-map vocabulary-fit report.

    ``tables`` is the session store's ``discovered_tables`` shape
    (``{"name", "columns": [{"name", "data_type"}], "row_count",
    "sample_values": {...}}`` — only column names are read); ``concepts`` is
    ``[{id, name, description}]``. A column HAS A CANDIDATE when some concept
    term's token set is a subset of the column-name token set (e.g. concept
    name ``work state`` matches column ``work_state_code``). Same input twice
    ⇒ identical dict. Pure — no IO, no LLM, no mutation of inputs."""
    names = _column_names(tables)
    terms = _concept_term_sets(concepts)
    matched = 0
    generic = 0
    unmatched: list[str] = []
    seen_unmatched: set[str] = set()
    for name in names:
        if is_generic_column(name):
            generic += 1
            continue
        col_tokens = _name_tokens(name)
        if col_tokens and any(t <= col_tokens for t in terms):
            matched += 1
        elif name not in seen_unmatched:
            seen_unmatched.add(name)
            if len(unmatched) < _UNMATCHED_CAP:
                unmatched.append(name)
    signal = len(names) - generic
    fit_pct = int(matched / signal * 100) if signal > 0 else 0
    return {
        "total_columns": len(names),
        "columns_with_candidate": matched,
        "fit_pct": fit_pct,
        "vocabulary_size": sum(1 for c in concepts or []
                               if isinstance(c, dict) and c.get("id")),
        "unmatched_columns": unmatched,
        "generic_columns": generic,
        "suggestion": _suggestion(fit_pct, signal),
    }
