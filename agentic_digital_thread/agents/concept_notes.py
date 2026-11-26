# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Question-matched concept notes: steward definitions of the terms a question uses.

A steward writes a Concept's ``description`` to say what a business term means,
and often which source can or cannot answer it ("a current count comes only
from the live tracking service, not from the event log"). Nothing used to
deliver that text to a turn: the route seed carried concept ids only, and a
system agent's concept-to-column mappings carry no description, so the model
read one only when it wrote its own graph query for it.

This module matches the question against the concept vocabulary BEFORE the
model runs and renders the matched descriptions, bounded, as reference text:

* :func:`match_notes` is conservative and general. A concept matches when the
  question carries one of its forms (id, name without a trailing qualifier in
  parentheses, a one-token parenthetical acronym, a ``synonyms`` entry) as a
  whole phrase, after case, separator (``-``/``_``/space) and simple plural
  folding; or the same words in another order inside one window; or a single
  DISTINCTIVE word: one that appears in the forms of exactly one concept (no
  other concept carries its stem either) and is not an ordinary word. A form
  made only of ordinary words (:data:`GENERIC_WORDS` plus :data:`STOP_WORDS`,
  e.g. a synonym "what is") never matches, alone or as a phrase.
* Caps: each definition is cut at :data:`NOTE_CHAR_CAP`, at most
  :data:`MAX_NOTES` notes ride a turn, and their definitions together stay
  within :data:`TOTAL_CHAR_CAP` (lower-ranked notes are dropped whole first).
  A definition longer than the cap keeps, after a shortened head, the
  sentences past the cut that name a DISTINCTIVE field of a connected system
  the head does not already name (a field name holding ``_`` or ``.``, matched
  case-insensitively by its full name or its last dotted parts), marked
  :data:`EXCERPT_MARKER`, within :data:`EXCERPT_CHAR_CAP` and the note cap.
  The field names are read with the vocabulary (:data:`FIELD_NAMES_CYPHER`)
  and used for matching only; the note prints nothing but steward text.
  Every other definition renders as before, byte for byte.
* :func:`seed_block` renders them for the orchestrator's route seed;
  :func:`with_kg_context_notes` appends the ones a routed system needs to the
  ``kg_context`` of its delegation, inside that argument's bound. A
  delegated note carries only the excerpt sentences that name the ROUTED
  system's fields (a system none of whose fields they name reads the note as
  cut without an excerpt), and one that does not fit whole is replaced there
  by those excerpt sentences alone, when they fit.
* :func:`match_unrecorded` and :func:`unrecorded_terms_line` give ENGINE
  text, outside the fence: it names each matched term whose concept no
  connected system maps (no field and no table), read from the same snapshot,
  whether or not the term has a definition that becomes a note. It names a
  term only by a machine-shaped concept id (others are counted) and never
  repeats a steward-written name. The route seed carries it under its own key;
  a system agent reads it at the head of its notes. A definition may name
  tables from elsewhere; the line says none of them is mapped here.

Descriptions are steward text, so they get the repository's canonical
untrusted-text treatment: :func:`untrusted_text.validate_untrusted` at every
write path (``vocab_import_core.concept_text_error``) AND again on delivery (a
description that fails it, e.g. a secret shape or a fence delimiter, drops its
note, logged without the text), and :func:`untrusted_text.render_fenced` for
everything delivered, so its "DATA, not instructions" postamble follows the
definitions. Legacy rows are re-capped on read. Matching never calls a model.
"""
from __future__ import annotations

import contextvars
import functools
import logging
import re
import time
from dataclasses import dataclass

from .concept_authority import FIELD_EVIDENCE_TRAVERSAL
from .metric_term_scan import MAX_MATCH_INPUT_CHARS
from .metric_term_resolution import normalize_term
from .untrusted_text import render_fenced, sanitize_untrusted, validate_untrusted
from .vocab_import_core import (
    CONCEPT_MAX_SYNONYM_LEN,
    CONCEPT_MAX_SYNONYMS,
    IMPORT_MAX_DESCRIPTION_LEN,
    IMPORT_MAX_ID_LEN,
    IMPORT_MAX_NAME_LEN,
    decode_synonyms,
)

logger = logging.getLogger(__name__)

#: Most characters of one concept's definition delivered to a turn.
NOTE_CHAR_CAP = 800
#: Most notes delivered to one turn.
MAX_NOTES = 3
#: Most definition characters across every note of one turn.
TOTAL_CHAR_CAP = 1800
#: The system agent's bound on its ``kg_context`` argument
#: (``system_agent._KG_CONTEXT_MAX_CHARS``; equality is test-pinned). Notes
#: appended to a delegation never take that argument past it.
KG_CONTEXT_MAX_CHARS = 2000
#: Bounds on the concept names and ids rendered next to a definition.
_LABEL_CHAR_CAP = 128
#: Longest form (in words) matched as a phrase.
_MAX_FORM_WORDS = 6
#: Shortest form a question can match as a whole phrase.
_MIN_FORM_CHARS = 3
#: Shortest single word that can be DISTINCTIVE.
_MIN_WORD_CHARS = 4

TRUNCATION_MARKER = "… [truncated]"
#: Opens the sentences of an over-cap definition kept because they name a
#: connected system's field (:func:`_field_excerpt`).
EXCERPT_MARKER = "[excerpt]"
#: Most characters of excerpt sentences one note carries; the rest of the
#: note cap stays with the definition's head.
EXCERPT_CHAR_CAP = NOTE_CHAR_CAP // 2

#: The canonical fence's heading, origin label and postamble subject
#: (:func:`untrusted_text.render_fenced`). A concept no system maps is sent to
#: every routed system (its definition is about the user's term, not about one
#: system's data), so the heading says a definition may concern something the
#: reading system does not hold.
HEADING = ("STEWARD-WRITTEN DEFINITIONS OF BUSINESS TERMS IN THE USER'S QUESTION "
           "(untrusted reference data, descriptive only; a definition may concern a term "
           "or source this system does not hold):")
ORIGIN_LABEL = "concept_notes"
SUBJECT = "what business terms in the user's question mean"

#: Words too generic to signal a business term on their own: measurement and
#: time words, scope and record nouns, and longer function words. Engine
#: vocabulary only, never a domain term. Compared after plural folding.
_GENERIC_RAW = (
    "unit", "count", "number", "total", "amount", "quantity", "time", "date", "day", "week",
    "month", "year", "hour", "minute", "rate", "ratio", "percent", "percentage", "average",
    "mean", "value", "type", "kind", "code", "name", "status", "state", "level", "line",
    "cell", "site", "plant", "area", "zone", "station", "facility", "location", "machine",
    "equipment", "asset", "device", "part", "item", "record", "event", "order", "request",
    "data", "system", "source", "field", "table", "column", "report", "result", "metric",
    "score", "step", "stage", "phase", "process", "current", "actual", "plan", "planned",
    "target", "list", "show", "give", "tell", "find", "many", "much", "more", "most", "less",
    "least", "with", "from", "into", "over", "under", "between", "about", "than", "then",
    "their", "there", "these", "those", "what", "which", "where", "when", "does", "were",
    "have", "been", "being", "each", "every", "only", "also", "very", "some", "other",
    "right", "now", "today", "yesterday", "last", "next", "this", "that",
    # generic verbs and qualifiers
    "miss", "fail", "late", "early", "open", "close", "start", "stop", "hold", "wait",
    "delay", "change", "cause", "issue", "problem", "error", "high", "first", "second",
)


def _fold(word: str) -> str:
    """Simple plural folding, applied alike to question and concept words."""
    if len(word) <= 3 or word.isdigit():
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _stem(word: str) -> str:
    """A rougher stem (``scheduled``/``schedule`` -> ``schedul``), used only
    to decide whether a word is DISTINCTIVE: a word whose stem another
    concept also carries is not."""
    if word.endswith("ing") and len(word) > 5:
        word = word[:-3]
    elif word.endswith("ed") and len(word) > 4:
        word = word[:-2]
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


GENERIC_WORDS = frozenset(_fold(word) for word in _GENERIC_RAW)

#: Short function words. A form made only of these and :data:`GENERIC_WORDS`
#: is ORDINARY: it never matches. Engine vocabulary only.
_STOP_RAW = (
    "a", "an", "the", "is", "are", "was", "be", "am", "of", "in", "on", "at", "to", "for",
    "by", "and", "or", "not", "no", "it", "its", "as", "if", "so", "do", "did", "does",
    "how", "why", "who", "what", "which", "when", "where", "we", "you", "they", "i", "me",
    "my", "our", "your", "he", "she", "his", "her", "us", "them", "any", "all", "per",
    "vs", "via", "can", "may", "will", "would", "should", "could", "has", "had", "get",
    "got", "out", "up", "down", "off",
)
STOP_WORDS = frozenset(_fold(word) for word in _STOP_RAW)
_ORDINARY = GENERIC_WORDS | STOP_WORDS


@dataclass(frozen=True)
class Concept:
    """One vocabulary concept as the snapshot reads it."""

    id: str
    name: str
    description: str = ""
    synonyms: tuple = ()
    #: Ids of the connected systems with a field mapped to this concept.
    systems: tuple = ()
    #: Ids of the connected systems with a TABLE mapped to this concept (an
    #: entity-level mapping: the system holds records about it). Never routes
    #: a note; only decides whether some connected system records the term.
    entity_systems: tuple = ()


@dataclass(frozen=True)
class ConceptNote:
    """One matched concept, ready to render. ``match`` is ``phrase``,
    ``reordered`` or ``word``; ``definition`` is sanitized and capped.
    ``excerpt`` holds the definition's kept field sentences
    (:func:`_field_excerpt`) as ``(sentence, system ids)`` pairs, the systems
    being those whose fields the sentence names; ``()`` for every other note.
    ``cut`` is, for a note with an excerpt, the definition as it is cut
    without one (what a system none of whose fields the excerpt names
    reads); ``""`` otherwise."""

    concept: str
    name: str
    definition: str
    systems: tuple
    match: str
    excerpt: tuple = ()
    cut: str = ""


class Vocabulary(tuple):
    """The concept snapshot: a tuple of :class:`Concept` (read like any tuple
    of them) that also carries the connected systems' distinctive field names,
    ``fields``: system id -> frozenset of the case-folded names a definition
    can name them by (:func:`field_names_from_rows`). Built from raw names, so
    ``Vocabulary(concepts, fields={"sys": ("col_a",)})`` is the same as the
    snapshot of a system with field ``col_a``."""

    fields: dict

    def __new__(cls, concepts=(), fields=None):
        vocabulary = super().__new__(cls, tuple(concepts or ()))
        vocabulary.fields = _field_index(fields)
        return vocabulary


# --------------------------------------------------------------------------- #
# The connected systems' field names (matching only, never printed)
# --------------------------------------------------------------------------- #
#: A field name as a schema gives it: identifier parts joined by ``.``.
_FIELD_NAME_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
#: Longest field name read; most systems kept.
_FIELD_NAME_CAP = 256
_FIELD_SYSTEM_CAP = 64


def _distinctive_field(name: str) -> bool:
    """A field name that marks a sentence as naming a system's data: holding
    ``_`` or ``.`` inside it (not only at its edges), with a letter, at least
    :data:`_MIN_WORD_CHARS` long, and not an ordinary word."""
    core = name.strip("_.")
    return (len(core) >= _MIN_WORD_CHARS and ("_" in core or "." in core)
            and any(ch.isalpha() for ch in core) and _fold(core) not in _ORDINARY)


def _field_tokens(name: object) -> set:
    """The case-folded forms a definition can name field ``name`` by: the
    full name and each of its trailing dotted parts, the distinctive ones
    only."""
    if not isinstance(name, str) or len(name) > _FIELD_NAME_CAP \
            or not _FIELD_NAME_RE.fullmatch(name):
        return set()
    parts = name.casefold().split(".")
    return {form for form in (".".join(parts[i:]) for i in range(len(parts)))
            if _distinctive_field(form)}


def _field_index(fields) -> dict:
    """``{system id: frozenset of forms}`` from ``{system id: names}``; a
    system with no distinctive field is left out. Idempotent."""
    index: dict = {}
    if not isinstance(fields, dict):
        return index
    for system, names in list(fields.items())[:_FIELD_SYSTEM_CAP]:
        if not isinstance(system, str) or not system.strip() \
                or isinstance(names, (str, bytes)):
            continue
        forms: set = set()
        for name in names or ():
            forms |= _field_tokens(name)
        if forms:
            index[system.strip()[:IMPORT_MAX_ID_LEN]] = frozenset(forms)
    return index


def _fields_named(text: str, fields: dict) -> dict:
    """``{system id: forms}`` of the connected systems' fields ``text``
    names: a name-shaped token (or one of its trailing dotted parts, so a
    table-qualified name counts) equal to a form, case folded."""
    named: dict = {}
    if not fields:
        return named
    for match in _FIELD_NAME_RE.finditer(text):
        parts = match.group(0).casefold().split(".")
        for form in (".".join(parts[i:]) for i in range(len(parts))):
            for system, forms in fields.items():
                if form in forms:
                    named.setdefault(system, set()).add(form)
    return named


def _fields_of(concepts, fields) -> dict:
    """The field index a match reads: ``fields`` when given, else the
    vocabulary's own (:class:`Vocabulary`), else none."""
    if fields is not None:
        return _field_index(fields)
    return concepts.fields if isinstance(concepts, Vocabulary) else {}


# --------------------------------------------------------------------------- #
# Text hygiene
# --------------------------------------------------------------------------- #
_FENCE_RUN_RE = re.compile(r"`{3,}|~{3,}")
_PARENTHETICAL_RE = re.compile(r"\(([^()]*)\)")


def _flat(text: str) -> str:
    """Sanitized steward text as one line: code-fence runs removed and
    whitespace (newlines included) collapsed."""
    return " ".join(_FENCE_RUN_RE.sub("", text).split())


def _one_line(text: str, cap: int) -> str:
    """Sanitized steward text as one bounded line (:func:`_flat`), cut at
    ``cap`` with :data:`TRUNCATION_MARKER`. Callers validate first
    (:func:`_unsafe`)."""
    text = _flat(text)
    if len(text) <= cap:
        return text
    head = text[:cap - len(TRUNCATION_MARKER) - 1]
    if " " in head[-40:]:
        head = head[:head.rfind(" ")]
    return head.rstrip() + " " + TRUNCATION_MARKER


def _unsafe(text: str, cap: int, field: str) -> str:
    """The canonical untrusted-text verdict on sanitized ``text``: ``""``, or
    the pinned reason (``too_long``, ``secret_like_content``,
    ``fence_delimiter``). Never the text itself."""
    failed = validate_untrusted(text, max_len=cap, field=field)
    return failed[0] if failed else ""


def _words(text: str) -> tuple:
    return tuple(_fold(word) for word in normalize_term(text).split())


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _forms(concept: Concept) -> list:
    """The phrase forms of one concept, as folded word tuples."""
    raw = [concept.id, _PARENTHETICAL_RE.sub(" ", concept.name)]
    for inner in _PARENTHETICAL_RE.findall(concept.name):
        if len(normalize_term(inner).split()) == 1:
            raw.append(inner)          # an acronym; a longer one is a qualifier
    raw.extend(concept.synonyms)
    forms = []
    for value in raw:
        words = _words(value)
        if words and len(" ".join(words)) >= _MIN_FORM_CHARS and words not in forms:
            forms.append(words)
    return forms


@dataclass(frozen=True)
class _Index:
    exact: dict          # word tuple -> concept ids
    reordered: dict      # sorted word tuple -> concept ids (multiword forms)
    distinctive: dict    # word -> the one concept id whose forms carry it
    by_id: dict          # concept id -> Concept (a described one wins a shared id)


@functools.lru_cache(maxsize=4)
def _index(concepts: tuple) -> _Index:
    exact: dict = {}
    reordered: dict = {}
    carriers: dict = {}
    stem_carriers: dict = {}
    by_id: dict = {}
    for concept in concepts:
        if concept.description.strip():
            by_id.setdefault(concept.id, concept)
    for concept in concepts:
        by_id.setdefault(concept.id, concept)
        for form in _forms(concept):
            for word in form:
                carriers.setdefault(word, set()).add(concept.id)
                stem_carriers.setdefault(_stem(word), set()).add(concept.id)
            if len(form) > _MAX_FORM_WORDS:
                continue
            if all(word in _ORDINARY for word in form):
                continue           # an ordinary form ("what is") names no term
            exact.setdefault(form, set()).add(concept.id)
            if len(form) > 1:
                reordered.setdefault(tuple(sorted(form)), set()).add(concept.id)
    distinctive = {word: next(iter(ids)) for word, ids in carriers.items()
                   if len(ids) == 1 and len(stem_carriers[_stem(word)]) == 1
                   and len(word) >= _MIN_WORD_CHARS
                   and not word.isdigit() and word not in _ORDINARY}
    return _Index(exact, reordered, distinctive, by_id)


_KIND_RANK = {"phrase": 0, "reordered": 1, "word": 2}


def _spans(words: list, index: _Index) -> list:
    """Every ``(start, end, ids, kind)`` phrase span, longest first."""
    spans = []
    for start in range(len(words)):
        for length in range(min(_MAX_FORM_WORDS, len(words) - start), 0, -1):
            window = tuple(words[start:start + length])
            if window in index.exact:
                spans.append((start, start + length, index.exact[window], "phrase"))
            elif length > 1 and tuple(sorted(window)) in index.reordered:
                spans.append((start, start + length,
                              index.reordered[tuple(sorted(window))], "reordered"))
    spans.sort(key=lambda s: (-(s[1] - s[0]), _KIND_RANK[s[3]], s[0]))
    return spans


def _ranked_matches(words: list, index: _Index) -> list:
    """``(rank key, concept id, kind)`` for every matched concept, best first.
    Phrases claim their words longest first; distinctive single words are
    read only from words no phrase claimed."""
    taken = [False] * len(words)
    best: dict = {}

    def offer(cid, start, end, kind):
        size = end - start
        key = (0 if size > 1 else 1, _KIND_RANK[kind], -size, start)
        if cid in index.by_id and (cid not in best or key < best[cid][0]):
            best[cid] = (key, kind)

    for start, end, ids, kind in _spans(words, index):
        if any(taken[start:end]):
            continue
        taken[start:end] = [True] * (end - start)
        for cid in sorted(ids):
            offer(cid, start, end, kind)
    for position, word in enumerate(words):
        if not taken[position] and word in index.distinctive:
            offer(index.distinctive[word], position, position + 1, "word")
    return sorted(((key, cid, kind) for cid, (key, kind) in best.items()),
                  key=lambda entry: (entry[0], entry[1]))


def _matched(question: object, concepts) -> list:
    """``(Concept, kind)`` for every concept ``question`` names, best first;
    ``[]`` without a question or a vocabulary. The scanned question is bounded
    by ``MAX_MATCH_INPUT_CHARS``. Pure."""
    if not isinstance(question, str) or not question.strip() or not concepts:
        return []
    index = _index(tuple(concepts))
    words = list(_words(question[:MAX_MATCH_INPUT_CHARS]))
    return [(index.by_id[cid], kind) for _key, cid, kind in _ranked_matches(words, index)]


#: The most matched concept ids the turn keeps (:func:`matched_ids`).
MAX_MATCHED_IDS = 16


def matched_ids(question: object, concepts) -> tuple:
    """The ids of the concepts ``question`` names, best first, the same
    matching the notes use, at most :data:`MAX_MATCHED_IDS`; ``()`` without a
    question or a vocabulary. Pure."""
    return tuple(dict.fromkeys(concept.id for concept, _kind
                               in _matched(question, concepts)))[:MAX_MATCHED_IDS]


def match_notes(question: object, concepts, fields=None) -> tuple:
    """The notes for the concepts ``question`` names, best first, within
    :data:`MAX_NOTES` and :data:`TOTAL_CHAR_CAP`. ``()`` when nothing matches,
    when there is no question, and for any concept without a description.
    A concept whose description or name fails the canonical untrusted-text
    check is dropped (logged with the reason, never the text) and the next
    match takes its place. ``fields`` (``{system id: field names}``) are the
    connected systems' fields an over-cap definition's excerpt is read for;
    by default the vocabulary's own (:class:`Vocabulary`), none for a plain
    tuple. The scanned question is bounded by ``MAX_MATCH_INPUT_CHARS``.
    Pure apart from that log line."""
    notes: list = []
    used = 0
    index = None
    for concept, kind in _matched(question, concepts):
        if len(notes) >= MAX_NOTES:
            break
        if not concept.description.strip():
            continue
        if index is None:
            index = _fields_of(concepts, fields)
        note = _note(concept, kind, index)
        if note is None or used + len(note.definition) > TOTAL_CHAR_CAP:
            continue
        used += len(note.definition)
        notes.append(note)
    return tuple(notes)


def _note(concept: Concept, kind: str, fields=None) -> "ConceptNote | None":
    """One renderable note, or ``None`` when the concept's text fails the
    canonical check (:func:`_unsafe`) or leaves no definition. An over-cap
    definition with field sentences past its cut (:func:`_field_excerpt`)
    keeps them after a head shortened to fit :data:`NOTE_CHAR_CAP`."""
    description = sanitize_untrusted(concept.description)
    name = sanitize_untrusted(concept.name) or sanitize_untrusted(concept.id)
    reason = (_unsafe(description, IMPORT_MAX_DESCRIPTION_LEN, "description")
              or _unsafe(name, IMPORT_MAX_NAME_LEN, "name"))
    if reason:
        logger.warning("concept notes: note dropped concept=%s reason=%s",
                       _one_line(concept.id, 64), reason)
        return None
    definition = _one_line(description, NOTE_CHAR_CAP)
    if not definition:
        return None
    excerpt = _field_excerpt(description, definition, fields or {})
    cut = ""
    if excerpt:
        suffix = f" {EXCERPT_MARKER} " + " ".join(sentence for sentence, _ in excerpt)
        cut, definition = definition, _one_line(description, NOTE_CHAR_CAP - len(suffix)) + suffix
    return ConceptNote(concept=_one_line(concept.id, _LABEL_CHAR_CAP),
                       name=_one_line(name, _LABEL_CHAR_CAP), definition=definition,
                       systems=concept.systems, match=kind, excerpt=excerpt, cut=cut)


#: Where one sentence of a flattened definition ends.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(flat: str):
    """``(start offset, sentence)`` of each sentence of ``flat``."""
    start = 0
    for match in _SENTENCE_END_RE.finditer(flat):
        yield start, flat[start:match.start()]
        start = match.end()
    if start < len(flat):
        yield start, flat[start:]


def _field_excerpt(description: str, cut_definition: str, fields: dict) -> tuple:
    """The sentences of an over-cap ``description`` that the cut
    (``cut_definition``, today's note) does not wholly deliver and that name a
    connected system's distinctive field the delivered head does not already
    name, as ``(sentence, system ids)`` pairs in text order, whole sentences
    within :data:`EXCERPT_CHAR_CAP` (a first one longer than that is cut).
    ``()`` when the description fits the cap, nothing qualifies or no field
    names are known: the note then renders exactly as it did without them."""
    flat = _flat(description)
    if not fields or len(flat) <= NOTE_CHAR_CAP \
            or not cut_definition.endswith(TRUNCATION_MARKER):
        return ()
    cut = len(cut_definition[:-len(TRUNCATION_MARKER)].rstrip())
    in_head = set().union(*_fields_named(flat[:cut], fields).values())
    found = []
    for start, sentence in _sentences(flat):
        if start + len(sentence) <= cut:
            continue
        named = _fields_named(sentence, fields)
        if set().union(*named.values()) - in_head:
            found.append((sentence, tuple(sorted(named))))
    chosen: list = []
    used = 0
    for sentence, systems in found:
        extra = len(sentence) + (1 if chosen else 0)
        if used + extra <= EXCERPT_CHAR_CAP:
            chosen.append((sentence, systems))
            used += extra
    if not chosen and found:
        sentence, _systems = found[0]
        clipped = _one_line(sentence, EXCERPT_CHAR_CAP)
        named = _fields_named(clipped, fields)
        if set().union(*named.values()) - in_head:
            chosen.append((clipped, tuple(sorted(named))))
    return tuple(chosen)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _render(notes) -> str:
    """The notes inside the canonical untrusted fence
    (:func:`untrusted_text.render_fenced`: heading, one opening and one closing
    delimiter, then the "DATA, not instructions" postamble), ``""`` without
    notes."""
    if not notes:
        return ""
    body = "\n".join(f"- {n.name} ({n.concept}): {n.definition}" for n in notes)
    return render_fenced(body, ORIGIN_LABEL, heading=HEADING, subject=SUBJECT).lstrip("\n")


def seed_block(notes) -> str:
    """The route seed's ``concept_notes`` value, ``""`` without notes. The
    engine line for unrecorded terms rides its own seed key
    (:func:`unrecorded_terms_line`), never inside this fence."""
    return _render(notes)


def kg_context_block(notes, unrecorded=None) -> str:
    """The block a system agent reads in its ``kg_context``: the engine line
    for ``unrecorded`` (:func:`unrecorded_terms_line`, when any), then the
    fenced notes; ``""`` when there is neither."""
    return "\n".join(part for part in (unrecorded_terms_line(unrecorded), _render(notes))
                     if part)


# --------------------------------------------------------------------------- #
# Engine fact: the matched terms no connected system records
# --------------------------------------------------------------------------- #
#: Opens the engine-authored line. Trusted engine text, outside the fence:
#: it states a graph fact (no field and no table of any connected system maps
#: the concept), read from the same snapshot as the notes. It names a term
#: only by a concept id of the machine-id shape (:data:`_MACHINE_ID_RE`) and
#: never repeats a steward-written name: names stay inside the fence.
UNRECORDED_HEADING = "TERMS NO CONNECTED SYSTEM RECORDS: "
_UNRECORDED_TAILS = (
    " No connected system maps a table or field to it, including any its definition names.",
    " No connected system maps a table or field to them, including any their definitions name.",
)
#: The concept ids the line may print: lowercase letters, digits, ``_`` and
#: ``-``, starting with a letter or a digit, at most 64 characters. Any other
#: id is counted, never printed.
_MACHINE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


@dataclass(frozen=True)
class UnrecordedTerms:
    """The matched terms no connected system maps (:func:`match_unrecorded`):
    ``ids`` are the printable concept ids in rank order, ``hidden`` counts the
    ones whose id is not of the machine-id shape. At most :data:`MAX_NOTES`
    in all."""

    ids: tuple = ()
    hidden: int = 0

    def __bool__(self) -> bool:
        return bool(self.ids) or self.hidden > 0


NO_UNRECORDED = UnrecordedTerms()


def _printable_id(value: object) -> bool:
    """Is ``value`` a concept id the engine line may print? The machine-id
    shape, and the canonical untrusted-text check on top."""
    return (isinstance(value, str) and _MACHINE_ID_RE.fullmatch(value) is not None
            and not _unsafe(value, IMPORT_MAX_ID_LEN, "id"))


def match_unrecorded(question: object, concepts) -> UnrecordedTerms:
    """The concepts ``question`` names that no connected system maps at field
    or table level, best first, at most :data:`MAX_NOTES`. Taken from the
    matches themselves: a term counts whether or not its definition becomes a
    note (none, or one the untrusted-text check drops). A term some connected
    system maps is never counted. Pure."""
    ids: list = []
    hidden = 0
    for concept, _kind in _matched(question, concepts):
        if concept.systems or concept.entity_systems:
            continue
        if len(ids) + hidden >= MAX_NOTES:
            break
        if _printable_id(concept.id):
            ids.append(concept.id)
        else:
            hidden += 1
    return UnrecordedTerms(tuple(ids), hidden)


def _unrecorded_line(ids: tuple, hidden: int) -> str:
    total = len(ids) + hidden
    if not total:
        return ""
    if ids:
        named = ("concept id " if len(ids) == 1 else "concept ids ") + ", ".join(ids)
        if hidden:
            named += f" and {hidden} more matched term" + ("" if hidden == 1 else "s")
    elif hidden == 1:
        named = "1 matched term whose id is not shown"
    else:
        named = f"{hidden} matched terms whose ids are not shown"
    tail = _UNRECORDED_TAILS[0] if total == 1 else _UNRECORDED_TAILS[1]
    return f"{UNRECORDED_HEADING}{named}.{tail}"


#: Longest line :func:`unrecorded_terms_line` can return: the widest mix of
#: longest printable ids and counted ones (by construction).
UNRECORDED_LINE_CHAR_CAP = max(
    len(_unrecorded_line(("a" * 64,) * shown, MAX_NOTES - shown))
    for shown in range(MAX_NOTES + 1))


def unrecorded_terms_line(unrecorded) -> str:
    """One engine line for ``unrecorded`` (:class:`UnrecordedTerms`), ``""``
    when it names nothing. Re-checked here: an id that is not printable is
    counted instead, and the total is held to :data:`MAX_NOTES`, so the line
    stays within :data:`UNRECORDED_LINE_CHAR_CAP` for any input. Pure."""
    if not isinstance(unrecorded, UnrecordedTerms):
        return ""
    raw_ids = tuple(unrecorded.ids)[:MAX_NOTES]
    ids = tuple(cid for cid in raw_ids if _printable_id(cid))
    hidden = len(raw_ids) - len(ids)
    if isinstance(unrecorded.hidden, int) and unrecorded.hidden > 0:
        hidden += unrecorded.hidden
    return _unrecorded_line(ids, min(hidden, MAX_NOTES - len(ids)))


#: Parses the named-id clause of an unrecorded line back into (ids, hidden) so
#: a computed metric this turn can drop its bound concept.
_UNRECORDED_NAMED_RE = re.compile(
    re.escape(UNRECORDED_HEADING)
    + r"concept ids? (?P<ids>[a-z0-9_, -]+?)"
    + r"(?: and (?P<more>\d+) more matched terms?)?\.")
_UNRECORDED_HIDDEN_ONLY_RE = re.compile(
    re.escape(UNRECORDED_HEADING)
    + r"(?:1 matched term whose id is not shown"
    + r"|(?P<more>\d+) matched terms whose ids are not shown)\.")


def drop_terms_from_unrecorded_line(line: str, drop_ids) -> str:
    """Remove the concept ids in ``drop_ids`` from a rendered unrecorded line
    and re-render it (an engine fact, "no system records X":
    must not contradict a metric the engine COMPUTED for X this turn). Pure.

    ``""`` and a line with no named ids pass through unchanged (nothing a
    computed metric could be said to contradict there, beyond the counted-only
    case this leaves alone). Dropping the only named id, with no hidden count,
    empties the line."""
    if not isinstance(line, str) or not line.strip():
        return ""
    drop = {str(d) for d in (drop_ids or ())}
    if not drop:
        return line
    match = _UNRECORDED_NAMED_RE.match(line)
    if match is None:
        return line  # counted-only or an unrecognised shape, leave as is
    ids = [tok for tok in re.split(r",\s*", match.group("ids").strip()) if tok]
    kept = [cid for cid in ids if cid not in drop]
    if kept == ids:
        return line  # nothing dropped
    hidden = int(match.group("more") or 0)
    return _unrecorded_line(tuple(kept), hidden)


def _is_query_tool(tool_name: str) -> bool:
    tool = tool_name.split("___", 1)[1] if "___" in tool_name else tool_name
    return tool.startswith("query_") or "_query_" in tool


def _names_system(tool_name: str, system_id: str) -> bool:
    """Is ``tool_name`` the natural-language query tool of ``system_id``?
    Gateway tools are ``<target>___<tool>`` with the target derived from the
    system id; a local tool is ``query_<system id with underscores>``."""
    safe = system_id.replace("-", "_")
    if "___" in tool_name:
        target, tool = tool_name.split("___", 1)
        return target == system_id.replace("_", "-")[:100] and _is_query_tool(tool)
    return tool_name == f"query_{safe}" or tool_name.endswith(f"_query_{safe}")


def _for_tool(note: ConceptNote, tool_name: str) -> bool:
    """A note goes to the systems that map its concept; a concept no system
    maps goes to every routed system (its definition is about the term)."""
    if not note.systems:
        return True
    return any(_names_system(tool_name, system) for system in note.systems)


def _routed_sentences(note: ConceptNote, tool_name: str) -> list:
    """The excerpt sentences of ``note`` that name the fields of the system
    ``tool_name`` queries, in text order."""
    return [sentence for sentence, systems in note.excerpt
            if any(_names_system(tool_name, system) for system in systems)]


def _delegated(note: ConceptNote, tool_name: str) -> "ConceptNote | None":
    """``note`` as the system ``tool_name`` queries reads it whole: its
    excerpt keeps, after the same head, only the sentences naming that
    system's fields; with none of them, the definition as cut without an
    excerpt (the note as it renders without field names). A note without
    an excerpt is unchanged; ``None`` when no such cut is known."""
    if not note.excerpt:
        return note
    routed = _routed_sentences(note, tool_name)
    if len(routed) == len(note.excerpt):
        return note
    suffix = f" {EXCERPT_MARKER} " + " ".join(sentence for sentence, _ in note.excerpt)
    if routed and note.definition.endswith(suffix):
        head = note.definition[:-len(suffix)]
        definition = f"{head} {EXCERPT_MARKER} " + " ".join(routed)
        excerpt = tuple(pair for pair in note.excerpt if pair[0] in routed)
    elif note.cut:
        definition, excerpt = note.cut, ()
    else:
        return None
    return ConceptNote(concept=note.concept, name=note.name, definition=definition,
                       systems=note.systems, match=note.match, excerpt=excerpt,
                       cut=note.cut if excerpt else "")


def _routed_excerpt(note: ConceptNote, tool_name: str) -> "ConceptNote | None":
    """The note made of only its excerpt sentences that name the fields of
    the system ``tool_name`` queries, ``None`` when it has none."""
    sentences = _routed_sentences(note, tool_name)
    if not sentences:
        return None
    return ConceptNote(concept=note.concept, name=note.name,
                       definition=f"{EXCERPT_MARKER} " + " ".join(sentences),
                       systems=note.systems, match=note.match)


def with_kg_context_notes(tool_name: object, arguments, notes, unrecorded=None):
    """``arguments`` with the routed system's notes, and the engine line for
    ``unrecorded`` (every routed system reads it), added to its
    ``kg_context``, or ``arguments`` itself (unchanged) when there is nothing
    to add or no room. Only a system query tool the model already passed a
    string ``kg_context`` to is touched (that is the tools whose schema takes
    it); the line goes first when it fits, then whole notes while the argument
    stays within :data:`KG_CONTEXT_MAX_CHARS`: first the notes for concepts
    this system maps, then the notes for concepts no system maps, each group
    in rank order (the system holds its own concepts' records; an unmapped
    note only says what a word means, so it never crowds one out). Each note
    carries only the excerpt sentences naming this system's fields
    (:func:`_delegated`); one that does not fit whole goes as those excerpt
    sentences alone (:func:`_routed_excerpt`) when they fit. The caller's
    dict is never mutated."""
    if not (notes or unrecorded) or not isinstance(arguments, dict) \
            or not isinstance(tool_name, str):
        return arguments
    existing = arguments.get("kg_context")
    if not isinstance(existing, str) or not _is_query_tool(tool_name):
        return arguments
    base = existing if existing.strip() else ""
    separator = "\n\n" if base else ""
    room = KG_CONTEXT_MAX_CHARS - len(base) - len(separator)
    line = unrecorded_terms_line(unrecorded)
    terms = unrecorded if line and len(line) <= room else None
    routed = [note for note in notes or () if _for_tool(note, tool_name)]
    chosen: list = []
    for note in [n for n in routed if n.systems] + [n for n in routed if not n.systems]:
        for candidate in (_delegated(note, tool_name),
                          _routed_excerpt(note, tool_name) if note.excerpt else None):
            if candidate is not None \
                    and len(kg_context_block([*chosen, candidate], terms)) <= room:
                chosen.append(candidate)
                break
    if not chosen and terms is None:
        return arguments
    merged = dict(arguments)
    merged["kg_context"] = base + separator + kg_context_block(chosen, terms)
    return merged


# --------------------------------------------------------------------------- #
# The per-turn hand-off: the route seed matches once, the delegation reads it
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _TurnTerms:
    notes: tuple = ()
    unrecorded: UnrecordedTerms = NO_UNRECORDED
    vocabulary: tuple = ()


#: This turn's notes and unrecorded terms, as one value so one reset clears
#: both. A bare tuple (``()``) reads as no notes and no unrecorded terms.
_TURN_NOTES: "contextvars.ContextVar" = contextvars.ContextVar(
    "adt_turn_concept_notes", default=())


def remember_turn_notes(notes, unrecorded=None, vocabulary=()) -> contextvars.Token:
    """Record the notes and unrecorded terms this turn's route seed matched,
    and the concept vocabulary it matched them from (``()`` and ``None`` for
    none)."""
    terms = unrecorded if isinstance(unrecorded, UnrecordedTerms) else NO_UNRECORDED
    return _TURN_NOTES.set(_TurnTerms(tuple(notes or ()), terms, tuple(vocabulary or ())))


def turn_notes() -> tuple:
    """The notes the current turn's route seed matched."""
    value = _TURN_NOTES.get()
    return value.notes if isinstance(value, _TurnTerms) else tuple(value or ())


def turn_unrecorded() -> UnrecordedTerms:
    """The unrecorded terms the current turn's route seed matched."""
    value = _TURN_NOTES.get()
    return value.unrecorded if isinstance(value, _TurnTerms) else NO_UNRECORDED


def turn_vocabulary() -> tuple:
    """The concept vocabulary the current turn's route seed matched from."""
    value = _TURN_NOTES.get()
    return value.vocabulary if isinstance(value, _TurnTerms) else ()


def reset_turn_notes() -> None:
    """Clear the current turn's notes and unrecorded terms (start of every
    invocation)."""
    _TURN_NOTES.set(())


# --------------------------------------------------------------------------- #
# The concept vocabulary snapshot (one bounded graph read per TTL window)
# --------------------------------------------------------------------------- #
SNAPSHOT_TTL_SECONDS = 120.0
SNAPSHOT_ROW_CAP = 2000
_SYSTEM_CAP = 32

#: Every live concept with its synonyms, the connected systems that map a
#: field to it, and the connected systems that map a table to it (an entity
#: mapping). A retired concept is not vocabulary; a system whose removal has
#: started is not a routing target.
_ENTITY_MAPPING_TRAVERSAL = "-[:HAS_SCHEMA]->(:SchemaEntry)-[:MAPS_TO]->"
CONCEPT_NOTES_CYPHER = f"""
MATCH (c:Concept)
WHERE coalesce(c.retired, false) = false
OPTIONAL MATCH (s:System){FIELD_EVIDENCE_TRAVERSAL}(c)
WHERE coalesce(s.removal_incomplete, false) = false
WITH c, collect(DISTINCT s.id) AS systems
OPTIONAL MATCH (e:System){_ENTITY_MAPPING_TRAVERSAL}(c)
WHERE coalesce(e.removal_incomplete, false) = false
WITH c, systems, collect(DISTINCT e.id) AS entity_systems
RETURN c.id AS id, c.name AS name, c.description AS description,
       c.synonyms AS synonyms, systems, entity_systems
ORDER BY id
LIMIT {SNAPSHOT_ROW_CAP}
""".strip()


def _strings(value, cap: int, item_cap: int = 256) -> tuple:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip()[:item_cap] for item in value
                 if isinstance(item, str) and item.strip())[:cap]


def _capped(value, cap: int) -> str:
    return value.strip()[:cap] if isinstance(value, str) else ""


def concepts_from_rows(rows) -> tuple:
    """Parse snapshot rows, skipping any without an id. Tolerant of every
    other field's shape (a concept without a description still counts for
    which words are distinctive). Every string is re-capped to the write
    bounds (``vocab_import_core``): rows written before those bounds existed,
    or by a path that does not apply them, cannot carry more."""
    concepts = []
    for row in list(rows or ())[:SNAPSHOT_ROW_CAP]:
        if not isinstance(row, dict):
            continue
        cid = _capped(row.get("id"), IMPORT_MAX_ID_LEN)
        if not cid:
            continue
        concepts.append(Concept(
            id=cid,
            name=_capped(row.get("name"), IMPORT_MAX_NAME_LEN) or cid,
            description=_capped(row.get("description"), IMPORT_MAX_DESCRIPTION_LEN),
            synonyms=_strings(decode_synonyms(row.get("synonyms")), CONCEPT_MAX_SYNONYMS,
                              CONCEPT_MAX_SYNONYM_LEN),
            systems=_strings(row.get("systems"), _SYSTEM_CAP),
            entity_systems=_strings(row.get("entity_systems"), _SYSTEM_CAP)))
    return tuple(concepts)


_snapshot: "tuple | None" = None   # (expires at, Vocabulary)

#: Most field rows the snapshot reads.
FIELD_ROW_CAP = 10000
#: The connected systems' field names that can be distinctive (a name holding
#: ``_`` or ``.``), for the excerpt of an over-cap definition. Read with the
#: concepts, once per TTL window; a system whose removal has started is not
#: a connected system.
FIELD_NAMES_CYPHER = f"""
MATCH (s:System)-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(f:Field)
WHERE coalesce(s.removal_incomplete, false) = false
  AND (f.name CONTAINS '_' OR f.name CONTAINS '.')
RETURN DISTINCT s.id AS system, f.name AS name
ORDER BY system, name
LIMIT {FIELD_ROW_CAP}
""".strip()


def field_names_from_rows(rows) -> dict:
    """``{system id: frozenset of case-folded forms}`` from field rows
    (``system``, ``name``), skipping any row of another shape, any name that
    is not a plain field name within :data:`_FIELD_NAME_CAP` and any name
    that is not distinctive (:func:`_field_tokens`)."""
    names: dict = {}
    for row in list(rows or ())[:FIELD_ROW_CAP]:
        if not isinstance(row, dict):
            continue
        system = _capped(row.get("system"), IMPORT_MAX_ID_LEN)
        if system:
            names.setdefault(system, []).append(row.get("name"))
    return _field_index(names)


def _read_field_names(query, previous) -> dict:
    """The field index, or ``previous`` when the read fails (logged)."""
    try:
        return field_names_from_rows(query(FIELD_NAMES_CYPHER))
    except Exception as exc:  # noqa: BLE001 - the excerpt is additive; never break a turn
        logger.info("concept notes: field names read failed category=%s", type(exc).__name__)
        return previous


def _monotonic() -> float:
    return time.monotonic()


def load_concepts(query=None) -> tuple:
    """The concept vocabulary (a :class:`Vocabulary` carrying the connected
    systems' field names), read at most once per :data:`SNAPSHOT_TTL_SECONDS`
    per process: the concepts, then the field names. A failed concept read
    keeps the previous snapshot (or gives none) and is retried on the next
    call; a failed field read keeps the previous field names (or none).
    ``query`` is the graph seam (default: the checked graph read, which raises
    on failure)."""
    global _snapshot
    now = _monotonic()
    current = _snapshot
    if current is not None and current[0] > now:
        return current[1]
    try:
        if query is None:
            from . import db
            query = db.neptune_query_checked
        concepts = concepts_from_rows(query(CONCEPT_NOTES_CYPHER))
    except Exception as exc:  # noqa: BLE001 - notes are additive; never break a turn
        logger.info("concept notes: vocabulary read failed category=%s", type(exc).__name__)
        return current[1] if current is not None else ()
    previous = getattr(current[1], "fields", {}) if current is not None else {}
    vocabulary = Vocabulary(concepts, fields=_read_field_names(query, previous))
    _snapshot = (now + SNAPSHOT_TTL_SECONDS, vocabulary)
    return vocabulary
