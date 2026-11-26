# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A system agent's decline for a stated rule, typed, and the repeat it rules out.

Measured on an analytics deployment: a system agent declined to list stuck
units, citing the stated rule that such a list needs a source that is not
connected; the orchestrator asked the same system again with narrower wording
("list the open rows with their age") and got the rows it was told not to
present. A prompt rule against re-asking did not hold.

Two halves, both deterministic:

* system side. The system agent ends a rule decline with ONE marker line
  (``DECLINED-BY-RULE kind=... concept=...``, stated in its prompt).
  :func:`extract_marker` reads it only when it is exact, unquoted, the final
  non-empty line and the only marker in the answer, with a concept; anything
  else (a quoted source row, a marker mid-answer, a decorated or
  concept-less one) is data. It strips the line, and :func:`engine_line`
  renders the typed engine line
  (``[engine] declined: reason_kind=...; concept=...; rule_id=...``) that
  crosses the query boundary with the answer;
* orchestrator side. The delegation tool (the gateway client wrapper in
  ``orchestrator``) reads it with :func:`parse_engine_line` and records the
  system and the decline's own concept (never the route's other concepts);
  :func:`refusal_for` then refuses a second delegation to that system about
  that concept in the same turn, with a short message. A delegation is about
  the concept when its ROUTE ``canonical_concept`` names it or its question
  mentions it. Other systems are never affected. After a ``not_connected``
  decline, a delegation whose question carries an identifier another
  system returned this turn along an IdentityRule of the declined concept
  into the declining system, which the declined question did not carry, is
  a new request and is sent (:func:`brings_new_facts`). So is one that
  brings the other identifier form the declining system was missing
  (:func:`brings_paired_form`): every decline is recorded, with the concepts
  of the system runtime's own line in the same result that its shape guard
  refused wrong-form lookups the call did not resolve
  (``wrong_form_refused``), whatever concept the model named in its
  decline; after such a decline, a question
  identifier the declined question did not carry, of another of the shapes
  of the turn's IDENTIFIER FORMS line for a refused concept, that a pairing
  system the line names
  returned from its own tool results this turn (``result_identifiers``),
  reopens it.

Values are reduced to a small safe character set, so the line reads the same
inside a JSON answer and nothing can be smuggled through it. Pure.
"""
from __future__ import annotations

import re

MARKER = "DECLINED-BY-RULE"
ENGINE_PREFIX = "[engine] declined: "
#: How the refusal of a repeat delegation starts (:func:`refusal_for`).
REFUSAL_PREFIX = "[engine] Not sent:"
#: The reason kinds a decline can carry.
REASON_KINDS = ("rule", "not_connected", "withheld_metric")
_VALUE_CAP = 64

#: The one exact marker line: no quoting, decoration or indentation, a known
#: kind, a REQUIRED concept token and an optional rule token, nothing else.
_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"  # nosec B105 - a regex for a marker word, not a password
_EXACT_MARKER_RE = re.compile(
    MARKER + r" kind=(?P<kind>" + "|".join(REASON_KINDS) + r") concept=(?P<concept>"
    + _TOKEN + r")(?: rule=(?P<rule>" + _TOKEN + r"))?")
_ENGINE_RE = re.compile(re.escape(ENGINE_PREFIX) + r"(?P<body>[A-Za-z0-9_.:;= -]+)")
_CANONICAL_RE = re.compile(r"^\s*canonical_concept\s*:\s*(?P<value>.+?)\s*$",
                           re.IGNORECASE | re.MULTILINE)
_UNSAFE_RE = re.compile(r"[^a-z0-9_.:-]")
_FOLD_RE = re.compile(r"[\s_-]+")


def _clean(value: str) -> str:
    """Lowercase, spaces to ``_``, only ``[a-z0-9_.:-]`` kept, bounded."""
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "_", text)
    return _UNSAFE_RE.sub("", text)[:_VALUE_CAP].strip("_.:-")


def extract_marker(answer: str):
    """``(answer without the marker line, decline)``, or ``(answer, None)``.

    A decline is read ONLY when the answer's final non-empty line is exactly
    the marker (:data:`_EXACT_MARKER_RE`, trailing spaces aside) and the
    marker text appears nowhere else in the answer. A quoted, decorated,
    indented or concept-less marker, or one that is not the last line, is
    data: the answer is returned as it was (the same object)."""
    if not isinstance(answer, str) or answer.count(MARKER) != 1:
        return answer, None
    head, _sep, last = answer.rstrip().rpartition("\n")
    match = _EXACT_MARKER_RE.fullmatch(last.rstrip())
    if match is None:
        return answer, None
    decline = {"reason_kind": match.group("kind"), "concept": _clean(match.group("concept"))}
    if match.group("rule"):
        decline["rule_id"] = _clean(match.group("rule"))
    if not decline["concept"]:
        return answer, None
    return head.rstrip(), decline


def neutralize(text: str) -> str:
    """``text`` with any copy of the engine's decline prefix made inert, so a
    model-written or data-carried line can never read as the engine's."""
    if not isinstance(text, str) or ENGINE_PREFIX not in text:
        return text
    return text.replace(ENGINE_PREFIX, "declined: ")


def engine_line(decline: dict) -> str:
    """The typed engine line for ``decline`` (values re-cleaned)."""
    kind = _clean(decline.get("reason_kind", ""))
    parts = [f"reason_kind={kind if kind in REASON_KINDS else 'rule'}"]
    for key in ("concept", "rule_id"):
        value = _clean(decline.get(key, ""))
        if value:
            parts.append(f"{key}={value}")
    return ENGINE_PREFIX + "; ".join(parts)


def parse_engine_line(text: str):
    """The decline the LAST engine line in ``text`` carries, else ``None``.
    Works on a prose answer and on a JSON answer's reserved note alike. A
    line without a known kind and a concept is not a decline."""
    if not isinstance(text, str) or ENGINE_PREFIX not in text:
        return None
    matches = list(_ENGINE_RE.finditer(text))
    if not matches:
        return None
    fields = {}
    for part in matches[-1].group("body").split(";"):
        key, sep, value = part.partition("=")
        if sep:
            fields[key.strip()] = _clean(value)
    if fields.get("reason_kind") not in REASON_KINDS or not fields.get("concept"):
        return None
    decline = {"reason_kind": fields["reason_kind"], "concept": fields["concept"]}
    if fields.get("rule_id"):
        decline["rule_id"] = fields["rule_id"]
    return decline


def fold(term: str) -> str:
    """Case and ``-``/``_``/space folded, for matching concept forms."""
    return _FOLD_RE.sub(" ", str(term or "").lower()).strip()


def delegation_concepts(arguments) -> set:
    """The folded concepts a delegation's ROUTE ``canonical_concept`` names."""
    kg = arguments.get("kg_context") if isinstance(arguments, dict) else ""
    out: set = set()
    for match in _CANONICAL_RE.finditer(kg if isinstance(kg, str) else ""):
        for part in re.split(r"[,/;|]|\band\b", match.group("value")):
            folded = fold(part)
            if folded and folded != "none":
                out.add(folded)
    return out


def _mentions(question: str, concept: str) -> bool:
    if not concept:
        return False
    words = [re.escape(w) for w in concept.split()]
    pattern = r"(?<![a-z0-9])" + r"[\s_-]+".join(words) + r"(?:s|es)?(?![a-z0-9])"
    return re.search(pattern, str(question or "").lower()) is not None


def _question(arguments) -> str:
    question = arguments.get("question") if isinstance(arguments, dict) else None
    return question if isinstance(question, str) else ""


def record_for(decline: dict, arguments, refused_concepts=()) -> dict | None:
    """What to remember about a decline: its own concept, ``None`` when it
    names none, and the identifiers the declined delegation's question
    carried (``carried_values.question_identifiers``). The delegation's route
    concepts are never taken in its place (a decline about one concept must
    not block the others). ``refused_concepts``: the folded concepts of the
    declining system's own line in the same result that its shape guard
    refused wrong-form lookups the call did not resolve
    (``wrong_form_refused``), whatever concept the model named in its
    decline; the evidence :func:`brings_paired_form` needs, kept apart from
    the concepts the decline latches."""
    from .carried_values import question_identifiers, question_scan_overflowed
    concept = fold((decline or {}).get("concept", ""))
    if not concept:
        return None
    question = _question(arguments)
    refused = frozenset(fold(c) for c in (refused_concepts or ())) - {""}
    return {"concepts": {concept}, "label": decline["concept"],
            "reason_kind": decline.get("reason_kind", "rule"),
            "asked": frozenset(_canonical(t) for t in question_identifiers(question)),
            "asked_overflowed": question_scan_overflowed(question),
            "wrong_form": bool(refused), "wrong_form_concepts": refused}


def _canonical(token: str) -> str:
    """One identifier in the form declined and later questions are compared
    in: casefolded, so a case-only respelling is the same identifier."""
    return str(token).casefold()


def brings_new_facts(record: dict, arguments, target, rules=(), results=()) -> bool:
    """Whether a delegation to ``target`` after ``record``'s decline carries
    what a ``not_connected`` decline said was missing, as the engine can
    prove it: an identifier of its QUESTION (never its route context) that
    the declined question did not carry, and that another system's
    successful result this turn returned along an IdentityRule of the
    declined concept into ``target`` (``carried_values.carried`` over
    ``rules`` and ``results``, the route facts' rules and recorded results:
    under the rule's field label, or matching the rule's pattern when
    unlabelled; engine lines and IDENTIFIER-MISS blocks were never recorded).
    Then it is a different request, not a repeat.

    Measured on a rehearsal deployment: a system declined ``not_connected``
    because it held two forms of one identifier and could not pair them; the
    orchestrator got the other form from a system the IdentityRules join to
    it for that concept and asked again with those values, and both asks
    were refused as repeats. A narrower rewording (the repeat this guard was
    built for), however many ordinary values a peer returned, values the
    declining system gave itself, values under no rule's field, a rule of
    another concept, and a ``rule`` or ``withheld_metric`` decline (a stated
    rule does not change with more data) never reopen it."""
    from .carried_values import carried, question_identifiers
    if record.get("reason_kind") != "not_connected" or not target or not rules:
        return False
    # A declined question with more identifiers than the scan reads: which of
    # them it carried is not known, so nothing can be shown to be new.
    if record.get("asked_overflowed", True):
        return False
    asked = frozenset(record.get("asked") or ())
    # Compared canonically; the original spelling goes to the provenance lookup.
    new = [t for t in question_identifiers(_question(arguments))
           if _canonical(t) not in asked]
    concepts = frozenset(record.get("concepts") or ())
    linking = [r for r in rules if fold(r.concept) in concepts]
    peers = [r for r in results if r.system != target]
    if not (new and linking and peers):
        return False
    return bool(carried(linking, peers, target, " ".join(new)).rules)


def _paired_value(token: str, refused: frozenset, crossing, paired: dict, target) -> bool:
    """Whether ``token`` has one of ``crossing``'s listed form shapes other
    than the ``refused`` ones (the declined question's own), and a pairing
    system the line names, never ``target``, returned that exact value from
    its own tool results this turn (``paired``: ``{system: folded values}``,
    ``result_identifiers``)."""
    from .identity_affinity import literal_shape
    shape = literal_shape(token)
    if shape in refused or shape not in {form.shape for form in crossing.forms}:
        return False
    value = _canonical(token)
    return any(peer != target and value in (paired.get(peer) or ()) for peer in crossing.peers)


def brings_paired_form(record: dict, arguments, target, crossings=(), paired=None) -> bool:
    """Whether a delegation to ``target`` after ``record``'s decline brings
    the other identifier form the declining system was missing, as the
    engine can prove it without a field label:

    * the record carries the declining system's own evidence that its shape
      guard refused a wrong-form lookup that the call did not resolve, with
      the concept it refused (``record["wrong_form_concepts"]``, read from
      the runtime's line, never from the model's decline, whose concept is
      the model's own word and need not be the refused one);
    * ``crossings`` (``form_crossing.crossings_for`` for ``target``) hold an
      IDENTIFIER FORMS line for a refused concept;
    * the QUESTION carries an identifier the declined question did not carry,
      whose literal shape is one of that line's form shapes other than the
      declined question's own (the form the guard refused), and a pairing
      system the line names, never ``target``, returned that exact value
      from its own tool results this turn (``paired``, the runtime's
      ``result_identifiers`` line, less every value that system's delegation
      itself carried).

    Measured on a test event (2026-11-11): the model fetched the other form
    from the pairing system and asked the holder again with it; the pairing
    system's answer gave the value under no IdentityRule's field, so
    :func:`brings_new_facts` refused it. Measured on a rehearsal account
    (2026-11-12): the holder's guard refused the run form under ``batch``,
    its model labelled the decline ``lot-number``, and the re-ask with the
    form the pairing system returned was refused as a repeat about the
    label. The decline's concept is the model's word, so a genuine
    ``not_connected`` decline about another concept, in a call that also
    had a wrong-form refusal, reopens the same way for a question carrying
    such a value: one more delegation, which the system's own rules still
    answer. A narrower rewording, a value the
    pairing system's answer only echoes (its results never returned it, or
    the model sent it), one only the declining system or another system
    returned, one of the refused form, and anything after a decline without
    that evidence, or after a ``rule`` or ``withheld_metric`` decline, never
    reopen it."""
    from .carried_values import question_identifiers
    from .identity_affinity import literal_shape
    refused_concepts = frozenset(record.get("wrong_form_concepts") or ())
    if (record.get("reason_kind") != "not_connected" or not refused_concepts
            or not target or not paired or record.get("asked_overflowed", True)):
        return False
    lines = [c for c in crossings if fold(c.concept) in refused_concepts]
    asked = frozenset(record.get("asked") or ())
    refused = frozenset(literal_shape(t) for t in asked)
    new = [t for t in question_identifiers(_question(arguments)) if _canonical(t) not in asked]
    return any(_paired_value(token, refused, crossing, paired, target)
               for crossing in lines for token in new)


def refusal_for(records, arguments, target=None, rules=(), results=(),
                crossings=(), paired=None) -> str | None:
    """The short refusal for a delegation that repeats a declined concept to
    the same system this turn, else ``None``. ``target``, ``rules`` and
    ``results``: the delegation's system and the turn's route facts
    (:func:`brings_new_facts`); ``crossings`` and ``paired``: the turn's
    IDENTIFIER FORMS lines for ``target`` and the identifiers each pairing
    system's own tool results returned (:func:`brings_paired_form`); without
    them no decline is reopened. The text names no reason code: answers that
    relayed it (2026-11-11) told the user a system "declined (reason:
    not_connected)", which reads like a broken link."""
    named = delegation_concepts(arguments)
    question = _question(arguments)
    for record in records or ():
        if brings_new_facts(record, arguments, target, rules, results) or \
                brings_paired_form(record, arguments, target, crossings, paired):
            continue
        if named & record["concepts"] or any(_mentions(question, c) for c in record["concepts"]):
            return (f"{REFUSAL_PREFIX} this system already declined a request about "
                    f"'{record['label']}' earlier in this turn. Do not ask it again about "
                    "that this turn. Ask another system, or tell the user plainly, in their "
                    "terms, what this system could not provide. Never quote [engine] lines "
                    "or their codes to the user.")
    return None
