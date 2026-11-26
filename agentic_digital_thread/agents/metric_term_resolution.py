# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PROACTIVE metric-term resolution — a deterministic prompt-composition check.

The Task-D ``METRIC-TERM MISS`` prompt rule (``orchestrator._METRIC_TERM_MISS_RULE``)
is REACTIVE: it tells the agent what to do when a vocabulary lookup for the
question's target quantity comes back empty. Measured live (answer-honesty round
2), the failure mode is that the lookup NEVER HAPPENS — asked for an undefined
three-letter rate, the model invented a definition, computed a ratio out of two
unrelated column sums, and presented it as the answer (three runs produced three
different numbers). Nothing was "missed", so nothing could fire.

This module closes that hole DETERMINISTICALLY and BEFORE the model runs: at
prompt-composition time the question is scanned for metric-term-shaped
candidates, each candidate is checked against the vocabulary this deployment
actually knows, and any candidate that does NOT resolve produces an explicit
prompt-level notice naming the exact term. The notice is computed — never
LLM-judged (project philosophy; same posture as ``agents/determinism.py``).

Scope and non-goals (deliberate):

* **Zero domain literals** (ADR-0044 / the engine-neutrality ratchet). Candidate
  extraction is purely SHAPE-based — letters-only all-caps acronyms and
  Title-Cased multiword phrases — gated to a metric-request context. The
  stoplist names only ENGINE/TECH/time vocabulary, never a metric or a site.
* **Identifier-shaped tokens are out of scope.** A candidate acronym must be
  letters only, so identifier-ish tokens that carry digits (site codes, line
  codes, lot ids) are never candidates — those belong to the ADR-0048
  identifier-miss recovery loop, which MAY substitute with disclosure. A metric
  term may not.
* **Bounded**: at most :data:`MAX_CANDIDATES` candidates per turn and NO
  per-turn graph round trip. The vocabulary is (a) the routing seed the prelude
  ALREADY built this turn — itself derived from the graph, so it carries the
  deployment's live concepts, fields, plants and query guidance — (b) the
  in-process metric definition registry, (c) the curated concept vocabulary
  (ids, names, ISA-95 synonym clauses), and (d) graph-resident metric
  definitions (ids, labels, synonyms) served from the ``metric_definition_store``
  TTL snapshot (ADR-0066 D4) — amortized at most one bounded graph read per TTL
  window per process, never one per turn. A metric a steward defines
  post-deploy therefore STOPS being flagged as unresolved within one TTL
  window, with zero redeploys.
* **Fail-OPEN**: every failure path yields NO notice. When the graph read fails
  the prelude has no seed at all and never calls this module, and a COLD
  metric-definition snapshot load failure raises out of the vocabulary build
  (caught at the notice boundary), so a graph outage can never manufacture an
  "undefined in this deployment" claim — the pre-check goes silent instead of
  claiming absence over a vocabulary it could not read (the store's
  warm-stale behavior makes this rare).
* **No gate, no error_type, no response-integrity change.** This is prompt text.

Residual (accepted): ConceptOverlay synonyms live only in the graph and remain
out of the resolution sources here (they ride no TTL snapshot, and reading them
would add the per-turn round trip this design exists to avoid). The notice
therefore carries an explicit escape hatch —
if a lookup the agent makes DOES resolve the term, that definition wins and the
notice is disregarded for that term. A notice can only ever cost one
clarification; a substituted proxy costs a wrong number.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

# The bounded one-pass scan + request gates live in ``metric_term_scan``
# (pure, file-size split); re-exported so this stays the ONE vocabulary home.
from .metric_term_scan import (  # noqa: F401 - re-exported surface
    _CONTEXT_SLICE_CHARS, _IMPERATIVE_RE, _LIST_JOIN_RE, IMPERATIVE_REQUEST_VERBS,
    MAX_MATCH_INPUT_CHARS, MatcherInputTooLong, _keep_longest_spans,
    _scan_defined_forms,
)

logger = logging.getLogger(__name__)

#: Bound on candidates carried into one notice (a question naming more
#: metric-shaped terms than this is a conversation, not a metric request).
MAX_CANDIDATES = 3

#: Bound on a single reported term (the shapes below are far shorter; this is
#: belt-and-braces so no prompt-borne text can grow the notice).
MAX_TERM_CHARS = 60

# Letters-only, 2-6 characters, not glued to another alphanumeric. Digits are
# EXCLUDED by construction (see the module docstring: identifier-shaped tokens
# belong to the identifier-miss loop).
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6})(?![A-Za-z0-9])")

# Title-Cased 2-3 word phrase, letters only (e.g. "First Pass Yield").
_PHRASE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,2})"
                        r"(?![A-Za-z0-9])")

# The "what is/are the X" request shape, anchored at the candidate's start
# offset. The COPULA IS MANDATORY: with it optional a bare "What " gated every
# acronym in the question, so "What CNC machines are down?" claimed CNC was an
# undefined metric. The article stays optional ("what is OEE").
# ``(?<![A-Za-z])`` keeps the word boundary, so "somewhat is the …" cannot match.
_WHAT_IS_RE = re.compile(
    r"(?<![A-Za-z])(?:what\s+(?:is|are|was|were)|what'?s)\s+"
    r"(?:(?:the|our|my|a|an)\s+)?$", re.IGNORECASE)

#: Plant / data nouns that, sitting IMMEDIATELY after a candidate, make the
#: candidate an ADJECTIVE rather than the question's target quantity ("What is
#: the <term> machines status"). Generic English/plant vocabulary only — never a
#: metric, a site, or a system id (ADR-0044 neutrality). Singular and plural
#: forms both count: "<term> line efficiency" is as adjectival as "<term> lines".
_NOUN_HEAD_WORDS = frozenset({
    "machine", "machines", "chart", "charts", "line", "lines",
    "station", "stations", "system", "systems", "asset", "assets",
    "unit", "units", "table", "tables", "order", "orders",
})

#: Auxiliary / imperative verbs that open a question and are Title-Cased by
#: sentence position, so the phrase shape absorbs them ("Did Line Efficiency").
#: Stripped from a phrase candidate before admission and reporting: a leading
#: verb is never part of the metric name. Closed and generic.
LEADING_VERB_WORDS = frozenset({
    "did", "was", "were", "is", "are", "do", "does", "has", "have",
    "can", "could", "should", "would", "will",
    "compare", "show", "list", "give", "tell", "find", "report", "explain",
})

#: Words whose presence next to a candidate makes the turn a METRIC REQUEST.
#: Generic measurement vocabulary only — the same family the reactive
#: ``METRIC-TERM MISS`` rule already names ("a rate, ratio, count, score, or
#: efficiency term").
METRIC_CONTEXT_WORDS = frozenset({
    "rate", "rates", "ratio", "ratios", "percent", "percentage", "percentages",
    "pct", "score", "scores", "metric", "metrics", "kpi", "kpis",
    "attainment", "efficiency", "efficiencies",
})

#: Engine / technology / time-vocabulary acronyms that are NEVER metric terms.
#: Nothing domain-specific belongs here: a DOMAIN term that this deployment
#: actually defines resolves through the vocabulary instead (that is why a
#: seeded metric such as the stored overall-effectiveness metric is not — and
#: must not be — stoplisted), and a domain term it does NOT define is exactly
#: what this module exists to surface.
#:
#: ``PM`` is DELIBERATELY ABSENT: it is a real maintenance metric
#: family ("PM completion rate"), and stoplisting it bought silence on a genuine
#: metric request. The clock-time reading is already excluded by the
#: metric-context gate — "meeting at 3 PM" carries no measurement word and no
#: "what is the …" shape. ``AM`` stays: it names no metric family. ``YTD``/
#: ``MTD`` stay too — a period QUALIFIER is not a quantity.
TECH_ACRONYM_STOPLIST = frozenset({
    # data / protocol / format
    "SQL", "DDL", "DML", "API", "URL", "URI", "URN", "JSON", "CSV", "TSV",
    "XML", "YAML", "HTML", "HTTP", "HTTPS", "CSS", "REST", "GRPC", "MCP",
    "SSE", "MQTT", "OPC", "UNS", "TLS", "SSL", "DNS", "PDF", "CRUD", "ETL",
    "ELT", "GSI", "LSI", "CDK", "SDK", "CLI", "GUI", "SSH", "FTP", "SFTP",
    # platform / infrastructure
    "AWS", "IAM", "KMS", "VPC", "ECS", "EKS", "EC2", "RDS", "SSM", "ARN",
    "ALB", "NLB", "WAF", "CPU", "GPU", "RAM", "TTL", "UUID", "PII", "LLM",
    "OTEL", "LTM", "STM",
    # enterprise system categories (system TYPES, not metrics)
    "ERP", "MES", "CMMS", "PLM", "MRP", "WMS", "QMS", "EAM", "LIMS", "APS",
    "SCADA", "PLC", "DCS", "HMI", "IIOT", "IOT", "ISA",
    # organisational / time vocabulary
    "SLA", "SLO", "SOP", "ETA", "FAQ", "TBD", "NA", "OK", "ID", "IDS",
    "UTC", "GMT", "EST", "CST", "MST", "PST", "EDT", "CDT", "MDT", "PDT",
    "AM", "YTD", "MTD", "QTD", "WTD", "FY", "EOD", "EOW", "EOM", "EOY",
    # common all-caps English words (a shouted question must not become
    # a wall of "undefined term" claims)
    "THE", "AND", "FOR", "NOT", "ALL", "ANY", "YES", "PER", "VS", "IS",
    "ARE", "WAS", "WERE", "WHAT", "HOW", "WHY", "WHO", "WHEN", "WHERE",
    "OUR", "YOUR", "ITS", "CAN", "DID", "DOES", "GET", "NEW", "OLD", "TOP",
})

#: Above this share of uppercase letters the question is SHOUTED — acronym
#: shape cannot distinguish an acronym from an ordinary word, so the
#: acronym pass is skipped entirely (fail-open, never guess).
_SHOUTING_UPPER_SHARE = 0.8

#: How many words either side of a candidate count as "adjacent".
_CONTEXT_WINDOW_WORDS = 2

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def normalize_term(text: str) -> str:
    """Lower-case, separator-insensitive form used for every comparison.

    ``"Spare-Parts_On Hand"`` and ``"spare parts on hand"`` normalize alike, so
    a hyphenated concept id and a spoken phrase resolve against each other.
    Pure."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def _collapse(normalized: str) -> str:
    """Separator-free form (``"work order"`` → ``"workorder"``). Pure."""
    return normalized.replace(" ", "")


def _initials(normalized: str) -> str:
    """Initialism of a 2-6 word normalized phrase, else ``""``.

    Lets ``"FPY"`` resolve against a metric labelled "First Pass Yield" without
    any per-deployment configuration. Pure."""
    words = normalized.split()
    if not 2 <= len(words) <= 6:
        return ""
    return "".join(w[0] for w in words)


def _is_shouting(question: str) -> bool:
    """True when the question's letters are overwhelmingly uppercase. Pure."""
    letters = [c for c in str(question or "") if c.isalpha()]
    if len(letters) < 8:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) >= _SHOUTING_UPPER_SHARE


def _in_metric_context(question: str, start: int, end: int) -> bool:
    """Is the span ``[start:end)`` a METRIC REQUEST occurrence?

    Two deterministic gates (either suffices):

    * a measurement word from :data:`METRIC_CONTEXT_WORDS` sits within
      :data:`_CONTEXT_WINDOW_WORDS` words on either side ("<term> rate",
      "attainment for <term>");
    * the question asks "what IS/ARE the <term>" immediately before the span —
      the copula is mandatory, so a bare "What <term> …" (a question ABOUT
      equipment, not a metric request) does not gate.

    Pure."""
    before = question[:start]
    after = question[end:]
    if _WHAT_IS_RE.search(before):
        return True
    prev_words = _WORD_RE.findall(before)[-_CONTEXT_WINDOW_WORDS:]
    next_words = _WORD_RE.findall(after)[:_CONTEXT_WINDOW_WORDS]
    return any(w.lower() in METRIC_CONTEXT_WORDS
               for w in (*prev_words, *next_words))


def _followed_by_noun_head(question: str, end: int) -> bool:
    """Is the word IMMEDIATELY after the span a plant/data noun head?

    ``"What is the <term> machines status"`` uses ``<term>`` as an ADJECTIVE, so
    the question's target quantity is not ``<term>`` and calling it an undefined
    metric is a false positive. Only the FIRST following word
    counts — an intervening word ("<term> rate for orders") means the noun is
    the scope of the metric, not its head. Pure."""
    following = _WORD_RE.findall(question[end:])[:1]
    return bool(following) and following[0].lower() in _NOUN_HEAD_WORDS


def _strip_leading_verbs(phrase: str) -> str:
    """Drop leading auxiliary/imperative verbs from a phrase candidate.

    ``_PHRASE_RE`` cannot tell a Title-Cased metric word from a sentence-initial
    verb, so ``"Did Line Efficiency Rate drop…"`` matched as
    ``"Did Line Efficiency"``. A leading verb is never part of a metric name, and
    leaving it in ALSO defeats resolution — the deployment defines
    ``"Line Efficiency"``, not ``"Did Line Efficiency"``. Pure."""
    words = str(phrase or "").split()
    while words and words[0].lower() in LEADING_VERB_WORDS:
        words.pop(0)
    return " ".join(words)


def _strip_trailing_context_words(phrase: str) -> str:
    """Drop trailing measurement words from a phrase candidate.

    Used two ways: as the ADMISSION test (``"Line Utilization Rate"`` names the
    quantity ``"Line Utilization"``; a phrase left with fewer than two words is
    discarded — one bare capitalised word is too weak a signal to call
    undefined) and as an extra RESOLUTION form, since a defined metric label may
    itself end in a measurement word. The candidate REPORTED is always the full
    phrase the question used. Pure."""
    words = phrase.split()
    while words and words[-1].lower() in METRIC_CONTEXT_WORDS:
        words.pop()
    return " ".join(words)


def _contains_context_word(term: str) -> bool:
    """Does the candidate itself carry a measurement word ("Sigma Delta Rate")?

    Such a phrase IS the metric request — the measure word is inside the span,
    so the adjacency window would look straight past it. Pure."""
    return any(w.lower() in METRIC_CONTEXT_WORDS for w in str(term or "").split())


def extract_metric_term_candidates(question: str) -> list[str]:
    """Metric-term-shaped candidates from ``question``, first-appearance order.

    Two shapes, both gated by :func:`_in_metric_context`: letters-only all-caps
    acronyms (2-6 chars) and Title-Cased 2-3 word phrases, the latter with any
    sentence-initial auxiliary/imperative verb stripped first. A candidate
    immediately followed by a plant/data noun head is adjectival and never
    admitted. Stoplisted and measurement words never survive; the result is
    de-duplicated on the normalized form and capped at :data:`MAX_CANDIDATES`.
    Pure — no I/O, no vocabulary knowledge (resolution is the caller's job)."""
    text = str(question or "")
    if not text:
        return []
    found: list[tuple[int, str]] = []
    seen: set[str] = set()

    def _offer(term: str, start: int, end: int) -> None:
        normalized = normalize_term(term)
        if not normalized or normalized in seen:
            return
        if len(term) > MAX_TERM_CHARS:
            return
        if term.upper() in TECH_ACRONYM_STOPLIST or normalized in METRIC_CONTEXT_WORDS:
            return
        if _followed_by_noun_head(text, end):
            return
        if not (_contains_context_word(term) or _in_metric_context(text, start, end)):
            return
        seen.add(normalized)
        found.append((start, term))

    if not _is_shouting(text):
        for m in _ACRONYM_RE.finditer(text):
            _offer(m.group(1), m.start(1), m.end(1))
    for m in _PHRASE_RE.finditer(text):
        # A sentence-initial auxiliary/imperative verb is not part of the metric
        # name — strip it (and advance the start offset with it) before both the
        # admission test and reporting.
        matched = m.group(1)
        phrase = _strip_leading_verbs(matched)
        if not phrase:
            continue
        start = m.start(1) + (len(matched) - len(phrase))
        # ADMISSION on the measure-word-stripped form, REPORTING on the phrase
        # the question actually used (minus the leading verb).
        if len(_strip_trailing_context_words(phrase).split()) >= 2:
            _offer(phrase, start, m.end(1))
    found.sort(key=lambda pair: pair[0])
    return [term for _start, term in found[:MAX_CANDIDATES]]


# --------------------------------------------------------------------------- #
# Vocabulary (no PER-TURN graph round trip — see the module docstring)
# --------------------------------------------------------------------------- #
def _add_vocabulary_forms(forms: set[str], value: object) -> None:
    """Add one vocabulary entry's normalized, collapsed and initialism forms.
    Pure."""
    normalized = normalize_term(str(value or ""))
    if not normalized:
        return
    forms.add(normalized)
    forms.add(_collapse(normalized))
    initials = _initials(normalized)
    if initials:
        forms.add(initials)


def known_metric_vocabulary() -> frozenset[str]:
    """Normalized vocabulary forms known without a per-turn graph round trip.

    Sources: the in-process metric-definition registry
    (``metric_layer.get_metric_layer()`` — ``lru_cache``d) contributing every
    metric id and label; the curated concept vocabulary
    (``catalog.CORE_CONCEPT_DEFINITIONS`` ids, ``catalog.CONCEPT_NAME`` display
    names, and the ISA-95 ``Synonyms (…)`` clause read through the ONE existing
    parser, ``concept_suggest.extract_synonyms``); and the graph-resident metric
    definitions served from the ``metric_definition_store`` TTL snapshot
    (ADR-0066 D4) — each definition's id, label and synonyms, at most one
    bounded graph read per TTL window per process, ZERO when the snapshot is
    warm. Each form is stored normalized, collapsed, and — for multiword forms
    — as its initialism, so an acronym resolves against a spelled-out label.

    Raises on a broken source rather than degrading silently — including a COLD
    store-snapshot load failure: the caller treats any failure as "emit
    nothing" (fail-open), because a partial vocabulary would produce false
    "undefined in this deployment" claims. A warm store serves its last good
    snapshot instead of raising."""
    from . import metric_definition_store
    from .catalog import CONCEPT_NAME, CORE_CONCEPT_DEFINITIONS
    from .concept_suggest import extract_synonyms
    from .metric_layer import get_metric_layer

    forms: set[str] = set()

    def _add(value: object) -> None:
        _add_vocabulary_forms(forms, value)

    for metric in get_metric_layer().list_metrics():
        _add(getattr(metric, "id", ""))
        _add(getattr(metric, "label", ""))
    for concept_id, definition in CORE_CONCEPT_DEFINITIONS.items():
        _add(concept_id)
        for synonym in extract_synonyms(definition or ""):
            _add(synonym)
    for name in CONCEPT_NAME.values():
        _add(name)
    for doc in metric_definition_store.load_definitions().values():
        _add(doc.get("id", ""))
        _add(doc.get("label", ""))
        for synonym in doc.get("synonyms") or ():
            _add(synonym)
    return frozenset(forms)


# --------------------------------------------------------------------------- #
# DEFINED-metric matching (ADR-0078 D2) — which served metrics a question names
# --------------------------------------------------------------------------- #
#: Bound on defined metrics resolved for one turn (ADR-0078 D3) — the same
#: "more than this is a conversation, not a metric request" bound as above.
MAX_METRIC_TERMS = 3

#: A vocabulary form shorter than this never matches a question (two-letter
#: forms collide with ordinary words and abbreviations).
MIN_FORM_CHARS = 3

_WHATS_RE = re.compile(r"(?<![A-Za-z])what['’]s(?![A-Za-z])", re.IGNORECASE)


@dataclass(frozen=True)
class DefinedMetricMatch:
    """One span of the NORMALIZED question naming a served metric.

    ``metric_ids`` has one id for a clean match and 2+ for an ambiguous span
    (``label`` is then ``""``). ``start``/``end`` index the normalized text."""

    term: str
    metric_ids: tuple[str, ...]
    label: str
    start: int
    end: int

    @property
    def ambiguous(self) -> bool:
        return len(self.metric_ids) > 1


def _metric_forms(value: object) -> set[str]:
    """Normalized/collapsed/initialism forms of ``value``, minimum length
    applied. Pure — the ONE form builder (:func:`_add_vocabulary_forms`)."""
    forms: set[str] = set()
    _add_vocabulary_forms(forms, value)
    return {form for form in forms if len(form) >= MIN_FORM_CHARS}


def _graph_synonyms(layer) -> dict[str, list]:
    """Synonyms of GRAPH-served definitions, keyed by id.

    Only ids the composed layer serves as graph-defined contribute (a
    code-shadowed doc names the code metric, which wins — ADR-0066). A store
    failure degrades to ``{}`` with a log line: the caller keeps the layer's
    own forms, because silence here IS the defect ADR-0078 closes."""
    is_graph = getattr(layer, "is_graph_defined", None)
    if not callable(is_graph):
        return {}
    from . import metric_definition_store
    try:
        docs = metric_definition_store.load_definitions()
        return {doc_id: list(doc.get("synonyms") or ())
                for doc_id, doc in (docs or {}).items() if is_graph(doc_id)}
    except Exception as exc:  # noqa: BLE001 - degrade to layer forms, never silence
        logger.info("defined-metric synonyms unavailable category=%s",
                    type(exc).__name__)
        return {}


def _defined_metric_forms() -> dict[str, dict[str, str]]:
    """form → {metric_id: label} over every served metric (code + graph).

    Ids and labels come from ``get_metric_layer().list_metrics()``; graph
    synonyms from the store TTL snapshot (:func:`_graph_synonyms`). No
    per-turn graph round trip beyond that snapshot."""
    from . import metric_layer
    layer = metric_layer.get_metric_layer()
    index: dict[str, dict[str, str]] = {}
    labels: dict[str, str] = {}
    for metric in layer.list_metrics():
        metric_id = str(getattr(metric, "id", "") or "")
        if not metric_id:
            continue
        label = str(getattr(metric, "label", "") or metric_id)
        labels[metric_id] = label
        for value in (metric_id, label):
            for form in _metric_forms(value):
                index.setdefault(form, {})[metric_id] = label
    for metric_id, synonyms in _graph_synonyms(layer).items():
        if metric_id not in labels:
            continue
        for synonym in synonyms:
            for form in _metric_forms(synonym):
                index.setdefault(form, {})[metric_id] = labels[metric_id]
    return index


# ADR-0078: a bare single-word metric that IS the question's subject
# ("throughput?", "how is throughput", "did throughput improve", "is oee up")
# must not slip past the request gate. Anchored on BOTH sides: nothing (or a
# generic interrogative opener) before the form, and at most a few words
# after it. ``normalize_term`` has already dropped punctuation. Engine
# vocabulary only.
_QUESTION_OPENER_RE = re.compile(
    r"^(?:(?:so|and|ok|okay)\s+)?(?:how\s+(?:is|was|are|were|about)|did|does|do|is|was|are|were|has|have|had"
    r"|what\s+about|any\s+change\s+in|update\s+on|status\s+of)\s+(?:the\s+|our\s+|my\s+|its\s+)?$"
)
# A bare form with NO opener admits only when nothing follows, or when a short
# tail opens with a generic state/trend predicate ("throughput improved this
# week"); "WDR team meeting notes" (a noun phrase) stays a casual mention.
_BARE_PREDICATE_RE = re.compile(
    r"^\s*(?:improved?|improving|up|down|dropped|dropping|rose|fell|falling|rising|better|worse"
    r"|higher|lower|ok|okay|fine|good|bad|trending|changed|status|today|yesterday|now|this"
    r"|last|so\s+far|recently|lately|over|since|for|in|at|on|by|vs|versus)\b"
)
_BARE_TAIL_WORDS = 4
_OPENER_TAIL_WORDS = 2


def _is_question_target(normalized: str, start: int, end: int) -> bool:
    """True when the bare form is the question's subject. Pure, bounded."""
    head = normalized[:start]
    tail = normalized[end:]
    tail_words = len(_WORD_RE.findall(tail))
    if not head.strip():
        if tail_words == 0:
            return True
        return tail_words <= _BARE_TAIL_WORDS and bool(_BARE_PREDICATE_RE.match(tail))
    if start <= _CONTEXT_SLICE_CHARS and _QUESTION_OPENER_RE.match(head):
        return tail_words <= _OPENER_TAIL_WORDS
    return False


# ADR-0078 (measured live): a declared metric written AS AN ACRONYM
# inside a question ("what would the <ACRONYM> impact have been?") is the
# user naming the metric, wherever it sits in the sentence. An acronym is
# not casual prose the way a single English word is, so the interrogative
# shape alone admits it. Casing comes from the ORIGINAL text (normalization
# lower-cases). Statements ("<ACRONYM> team meeting notes") stay unadmitted.
_INTERROGATIVE_RE = re.compile(
    r"^(?:\w+\s+){0,3}(?:what|which|how|why|when|where|who|did|does|do|is|was|are|were"
    r"|would|could|should|can|will|has|have|had)\b"
)


def _question_acronyms(text: str, normalized: str) -> frozenset[str]:
    """Lower-cased all-caps tokens of an interrogative question, else empty."""
    if not (text.rstrip().endswith("?") or _INTERROGATIVE_RE.match(normalized)):
        return frozenset()
    return frozenset(m.group(1).lower() for m in _ACRONYM_RE.finditer(text))


#: Count-request cues that name a COUNT metric over records ("how many work
#: orders", "number of work orders", "work orders total"). Separate from
#: :data:`METRIC_CONTEXT_WORDS` so the unresolved-term path is unchanged.
_COUNT_WORDS = frozenset({"count", "counts", "total", "totals", "number"})
_COUNT_REQUEST_RE = re.compile(
    r"(?:how\s+many|(?:the\s+)?(?:number|count|total(?:\s+number)?)\s+of|total)\s+"
    r"(?:\w+\s+){0,2}$")


def _has_measurement_word(form: str) -> bool:
    """Does the label itself say it is a quantity ("defect rate", "good count")?"""
    return any(w in METRIC_CONTEXT_WORDS or w in _COUNT_WORDS
               for w in form.split())


#: A tail this long after the label is a records request with conditions,
#: not "report <metric> for the week".
_PLAIN_NOUN_TAIL_WORDS = 6
_TAIL_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _scoped_tail(tail: str) -> bool:
    """Does the text after the label scope it to an entity (a token with a
    digit: line 3, l3 m1, cmx 441) or run on as a records request?"""
    words = _TAIL_TOKEN_RE.findall(tail)
    return (len(words) > _PLAIN_NOUN_TAIL_WORDS
            or any(any(c.isdigit() for c in w) for w in words))


def _plain_noun_request(normalized: str, start: int, end: int,
                        tail_end: int | None = None) -> bool:
    """A records noun phrase ("work orders") names its COUNT metric only when
    the question asks for a quantity: a count cue just before it, a count or
    measurement word right around it, a "what is the" shape, the phrase as
    the question's own subject, or an imperative whose tail (up to the next
    defined form) does not scope it to an entity ("report work orders for
    the week" yes, "list the work orders for cmx 441 and their status" no).
    Pure."""
    head = normalized[:start]
    if _COUNT_REQUEST_RE.search(head[-_CONTEXT_SLICE_CHARS:]):
        return True
    low = max(0, start - _CONTEXT_SLICE_CHARS)
    window = normalized[low:end + _CONTEXT_SLICE_CHARS]
    if _in_metric_context(window, start - low, end - low):
        return True
    next_words = _WORD_RE.findall(normalized[end:])[:_CONTEXT_WINDOW_WORDS]
    prev_words = _WORD_RE.findall(head)[-_CONTEXT_WINDOW_WORDS:]
    if any(w.lower() in _COUNT_WORDS for w in (*prev_words, *next_words)):
        return True
    if (start <= _CONTEXT_SLICE_CHARS and _IMPERATIVE_RE.match(head)
            and not _scoped_tail(normalized[end:tail_end])):
        return True
    return _is_question_target(normalized, start, end)


def _admit_defined_span(normalized: str, span: tuple[int, int, str],
                        prev_end: int | None = None,
                        acronyms: frozenset[str] = frozenset(),
                        next_start: int | None = None) -> bool:
    """A multiword label that carries a measurement word admits outright; a
    plain-noun multiword label ("work orders") only under a count or metric
    request (it once fired on every scoped records question). A single
    word or an initialism only under a request gate (ordinary-English labels
    must not fire on a casual mention): listed right after an admitted form,
    an anchored imperative ("show me the <term>"), or the shared
    metric-request gate. Pure."""
    start, end, form = span
    if prev_end is not None and _LIST_JOIN_RE.match(normalized[prev_end:start]):
        return True
    if " " in form:
        return (_has_measurement_word(form)
                or _plain_noun_request(normalized, start, end, next_start))
    if form in acronyms:
        return True
    if start <= _CONTEXT_SLICE_CHARS and _IMPERATIVE_RE.match(normalized[:start]):
        return True
    if _is_question_target(normalized, start, end):
        return True
    low = max(0, start - _CONTEXT_SLICE_CHARS)
    window = normalized[low:end + _CONTEXT_SLICE_CHARS]
    return _in_metric_context(window, start - low, end - low)


def defined_metric_terms(question: str,
                         limit: int = MAX_METRIC_TERMS) -> list[DefinedMetricMatch]:
    """The served metrics ``question`` names, first-appearance order.

    Deterministic and bounded (``limit``, default :data:`MAX_METRIC_TERMS`); a
    caller that must DETECT overflow asks for ``MAX_METRIC_TERMS + 1`` — the
    scan stops there (ADR-0078 D3 overflow block). A metric named twice
    is one match. A question over :data:`MAX_MATCH_INPUT_CHARS` RAISES
    :class:`MatcherInputTooLong` (the caller fails CLOSED). May raise on a
    broken LAYER (the caller fails open to no notice); a broken STORE degrades
    to the layer's forms. Never an LLM."""
    text = str(question or "")
    if not text.strip():
        return []
    if len(text) > MAX_MATCH_INPUT_CHARS:
        raise MatcherInputTooLong(len(text))
    forms = _defined_metric_forms()
    normalized = normalize_term(_WHATS_RE.sub("what is", text))
    acronyms = _question_acronyms(text, normalized)
    matches: list[DefinedMetricMatch] = []
    seen: set[tuple[str, ...]] = set()
    prev_end: int | None = None
    spans = list(_keep_longest_spans(_scan_defined_forms(normalized, forms)))
    for index, span in enumerate(spans):
        next_start = spans[index + 1][0] if index + 1 < len(spans) else None
        if not _admit_defined_span(normalized, span, prev_end, acronyms, next_start):
            continue
        prev_end = span[1]
        by_id = forms[span[2]]
        ids = tuple(sorted(by_id))
        if ids in seen:
            continue
        seen.add(ids)
        matches.append(DefinedMetricMatch(
            term=normalized[span[0]:span[1]][:MAX_TERM_CHARS], metric_ids=ids,
            label=by_id[ids[0]] if len(ids) == 1 else "",
            start=span[0], end=span[1]))
        if len(matches) >= limit:
            break
    return matches


def _sub_phrases(term: str) -> list[str]:
    """Sub-phrases of ``term`` to try as RESOLUTION forms, longest first.

    Trailing measurement words are dropped progressively (a defined metric label
    may itself end in one) AND leading tokens are dropped progressively, because
    a candidate can still carry a word that is not part of the metric name. Any
    sub-phrase that resolves suppresses the notice — the invariant is
    never-flag-a-term-this-deployment-knows, so widening the RESOLUTION side is
    always the safe direction.

    A leading-dropped sub-phrase must keep at least two words and must not be
    made only of measurement words; otherwise ``"Sigma Delta Rate"`` would
    "resolve" on the bare word ``"rate"``, which appears in almost any seed. The
    ORIGINAL candidate is always a form, so a one-word acronym still resolves.
    Pure."""
    words = str(term or "").split()
    if not words:
        return []
    tails: list[list[str]] = [list(words)]
    trimmed = list(words)
    while trimmed and trimmed[-1].lower() in METRIC_CONTEXT_WORDS:
        trimmed = trimmed[:-1]
        tails.append(list(trimmed))
    forms: list[str] = []
    for tail in tails:
        for index in range(len(tail)):
            sub = tail[index:]
            if not sub:
                continue
            if index and (len(sub) < 2
                          or all(w.lower() in METRIC_CONTEXT_WORDS for w in sub)):
                continue
            candidate = " ".join(sub)
            if candidate not in forms:
                forms.append(candidate)
    return forms


def _resolves(term: str, vocabulary: frozenset[str], seed_normalized: str) -> bool:
    """Does ``term`` resolve against the vocabulary or this turn's seed?

    Every sub-phrase from :func:`_sub_phrases` is tried in its normalized and
    collapsed forms. Seed matching is word-boundary on the NORMALIZED seed text,
    so the deployment's live graph vocabulary (concepts, mapped fields, plants,
    query guidance) resolves a term with zero extra queries. Pure."""
    forms: list[str] = []
    for raw in _sub_phrases(term):
        normalized = normalize_term(raw)
        if not normalized:
            continue
        for form in (normalized, _collapse(normalized)):
            if form not in forms:
                forms.append(form)
    if not forms:
        return True                      # nothing to resolve ⇒ never flag
    if any(form in vocabulary for form in forms):
        return True
    if seed_normalized:
        for form in forms:
            pattern = r"(?<![a-z0-9])" + re.escape(form) + r"(?![a-z0-9])"
            if re.search(pattern, seed_normalized):
                return True
    return False


def unresolved_metric_terms(question: str, seed_text: "str | None" = None) -> list[str]:
    """Candidates from ``question`` that this deployment does NOT define.

    Deterministic and bounded (:data:`MAX_CANDIDATES`). ``seed_text`` is the
    routing seed already composed for this turn — pass it so graph-known
    vocabulary counts. Raises nothing of its own; a vocabulary source failure
    propagates to :func:`metric_term_gap_notice`, which fails open."""
    candidates = extract_metric_term_candidates(question)
    if not candidates:
        return []
    vocabulary = known_metric_vocabulary()
    seed_normalized = normalize_term(seed_text) if seed_text else ""
    return [term for term in candidates
            if not _resolves(term, vocabulary, seed_normalized)]


# --------------------------------------------------------------------------- #
# The notice
# --------------------------------------------------------------------------- #
#: Header the notice always opens with (operator-visible marker in the trace,
#: and the anchor unit tests pin).
NOTICE_HEADER = "[UNRESOLVED METRIC TERM — DETERMINISTIC PRE-CHECK]"

#: The non-negotiable sentence. Pinned by
#: ``tests/unit/test_metric_term_resolution.py`` — a wording change that drops
#: it turns the notice back into a hint the model may optimise away.
NEVER_SUBSTITUTE_SENTENCE = (
    "Computing a substitute quantity — a related ratio, the nearest available "
    "metric, or a formula inferred from column or property names — is a WRONG "
    "ANSWER, not an approximation, even if you disclose it."
)


def render_metric_term_gap_notice(terms: "list[str] | tuple[str, ...]") -> str:
    """Render the prompt-level notice for ``terms``, or ``""`` when empty. Pure."""
    listed = [str(t)[:MAX_TERM_CHARS] for t in (terms or ()) if str(t).strip()]
    if not listed:
        return ""
    quoted = ", ".join(f"'{t}'" for t in listed[:MAX_CANDIDATES])
    return (
        f"{NOTICE_HEADER}\n"
        f"A deterministic pre-check of this question against the vocabulary this "
        f"deployment actually knows (the routing seed above, the metric "
        f"definition registry, and the concept vocabulary) found NO definition "
        f"for: {quoted}.\n"
        f"Treat {quoted} as UNDEFINED IN THIS DEPLOYMENT. "
        f"{NEVER_SUBSTITUTE_SENTENCE}\n"
        f"REQUIRED RESPONSE: name the unresolved term(s) explicitly, state that "
        f"the term is not defined in the connected systems' vocabulary, and ask "
        f"which definition is meant — listing as OPTIONS only candidate "
        f"interpretations you actually found. If a system's query guidance says "
        f"the authoritative source for this term is not connected, state THAT "
        f"instead of asking. Answer whatever part of the question DID resolve, "
        f"and never present a number as the value of an undefined term.\n"
        f"If a vocabulary or graph lookup you make DOES resolve one of these "
        f"terms to a defined concept or metric definition, use that definition "
        f"and disregard this notice for that term."
    )


def metric_term_gap_notice(question: str, seed_text: "str | None" = None) -> str:
    """The notice for ``question``, or ``""`` when every candidate resolves.

    THE fail-open boundary: any extraction/vocabulary failure yields ``""``, so
    an ordinary turn stays byte-identical and a broken lookup can never block a
    turn or fabricate an "undefined" claim."""
    try:
        return render_metric_term_gap_notice(
            unresolved_metric_terms(question, seed_text))
    except Exception:  # noqa: BLE001 - fail-OPEN: a pre-check never breaks a turn
        return ""


# --------------------------------------------------------------------------- #
# Resolution view of the seed (guidance prose is instructions, not vocabulary)
# --------------------------------------------------------------------------- #
#: Seed-payload keys whose values are GUIDANCE PROSE — steward/learned notes
#: and onboarded guidance text. Prose here INSTRUCTS the model about a term
#: (often to refuse deriving it); it never makes the term MEASURABLE, so it
#: must not resolve a candidate. Structured vocabulary (mapped_concepts,
#: shared_concepts, fields, plants, system names) stays resolution-eligible.
_SEED_GUIDANCE_PROSE_KEYS = frozenset({
    "query_guidance", "query_hints", "answer_requirements", "legacy_unscoped",
    "business_context",
    # Route-seed shared sections (orchestrator._share_default_guidance): the
    # same connection-type guidance prose, delivered once for many systems.
    "shared_guidance",
    # Question-matched steward definitions (agents.concept_notes): prose that
    # explains a term, often to say no connected source can answer it.
    "concept_notes",
    # The engine line naming matched terms no connected system maps: it says
    # a term is NOT measurable here, so it must never resolve one.
    "unrecorded_terms",
})


def _without_guidance_prose(value: object) -> object:
    """Deep-copy ``value`` with guidance-prose entries removed. Pure."""
    if isinstance(value, dict):
        return {key: _without_guidance_prose(item)
                for key, item in value.items()
                if key not in _SEED_GUIDANCE_PROSE_KEYS}
    if isinstance(value, list):
        return [_without_guidance_prose(item) for item in value]
    return value


def resolution_seed_view(seed_text: "str | None",
                         payload_marker: str = "ROUTE SEED RESULT:\n") -> str:
    """``seed_text`` with guidance PROSE stripped, for term RESOLUTION only.

    WHY: a learned guidance note that mentions a term — typically to FORBID
    deriving it ("a count in units means X units; never derive…") — used to
    RESOLVE that term and suppress the deterministic gap notice, leaving
    compliance to per-turn model obedience (measured live: the same question
    alternated between an honest grain-disclosed answer and a derived,
    mislabelled count). A note is an instruction, not a definition: the term
    is still unmeasurable in the connected systems, so the notice must fire
    and COMPOSE with the note (its own text already says to state a
    guidance-declared unavailability rather than ask).

    Only the marker's JSON payload is filtered, structurally (never regex over
    prose); any text AFTER the JSON (the identifier-translations block, the
    ADR-0086 wildcard note) is kept verbatim. No marker, or ANY parse failure
    ⇒ the original text unchanged — fail-open toward the pre-change behavior,
    so a malformed seed can never ADD flags. Delivery is untouched: callers
    pass the FULL seed to the model and THIS view only to
    :func:`metric_term_gap_notice`.
    """
    if not isinstance(seed_text, str) or payload_marker not in seed_text:
        return seed_text or ""
    prefix, _, payload = seed_text.partition(payload_marker)
    try:
        fragment = payload.lstrip()
        decoded, end = json.JSONDecoder().raw_decode(fragment)
        return prefix + payload_marker + json.dumps(
            _without_guidance_prose(decoded), ensure_ascii=False, default=str) + fragment[end:]
    except Exception:  # noqa: BLE001 - fail-open: never widen flags on a bad seed
        return seed_text
