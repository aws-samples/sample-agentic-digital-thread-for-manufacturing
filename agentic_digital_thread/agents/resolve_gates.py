# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evidence gates for LLM-proposed identity rules (pure, generic).

``_do_resolve`` measures cross-system value overlap for every field pair that
shares a concept and hands the measurements to the model as evidence. The model
is told to "strongly prefer measured overlap" — so a 7 % overlap of decimal
prices (``120.00``, ``45.00``) or five shared small integers (``1 … 5``) read
as licence to propose an identity rule. Live 2026-09-18: six ``cost`` rules,
an ``inspection`` rule on ``log_id ↔ sample_number``, a ``schedule`` rule on
one shared date. None identifies an entity.

These gates sit between the resolver and the affinity merge (the
``identity_affinity`` rules carry their own ``value_core_match`` guards and are
NOT overlap-gated here; only the category check below applies to them). They
read only what the resolver already produced — the
rule's concept / field names and the evidence row for that pair — so they hold
for any schema, domain or naming convention:

* never on measure / temporal concepts (cost, price, quantity, date, time …);
* never on category / status concepts (failure mode, defect type, order
  status, machine state …): a shared reason code is shared by many records,
  so it names a kind of thing, not one record. This check reads only the
  concept and runs as its own pass, :func:`gate_category_rules`, on every path
  that proposes or writes a rule (the model's rules before this gate, the
  affinity rules after it, the chat's resolve, the provision writer and the
  proposal accept);
* with measured evidence: >= ``MIN_SHARED_VALUES`` distinct shared values AND
  overlap >= ``MIN_OVERLAP_RATIO`` of the smaller side, and the shared values
  must not all have a non-identifier shape (decimal measures, dates/timestamps,
  booleans, a small dense integer range). A rule that reads values unchanged
  needs that bar on the shared values that are the same whole value on both
  sides (``unchanged_count``): the runtime joins whole values, and the
  overlap's tokens set padding aside and read a JSON value's members out;
* a translation pattern, on a pair that shares values or one that shares
  none, is judged on what it makes of the sampled values
  (:data:`TRANSFORM_CHECKS_KEY`), on the same bar;
* measured with nothing shared (both fields sampled, no value in common:
  :data:`UNSHARED_PAIRS_KEY`): never for a rule whose patterns read a value
  unchanged, which those samples contradict. The reason says what was measured;
* without measured evidence (one side could not be sampled): both field
  names must look identifier-like (id, num, number, code, key, lot, batch,
  serial, tag …);
* with AMBIGUOUS evidence (a rule that dropped a table, matching more than one
  measured pair, whether they share values or not): never; the gate does not
  guess which pair was meant.

Rejected rules are kept on the result under ``rejected_rules`` (with a
``rejected_reason``) so the checkpoint can show WHY, and the resolver log
records each drop.

After every other gate, :func:`gate_storable_rules` runs provision's own
pattern check (:func:`pattern_rejection`) so the checkpoint never lists a rule
provision would refuse to store.

Before any of these, :func:`keep_own_candidates` leaves out a candidate that
is not the resolved system's at all: the model is shown peer fields of EVERY
connected system and can pair two OTHER systems' fields (live 2026-10-19: a
tag hub's checkpoint listed eleven withheld rules, each a telemetry field
paired with an MES column). Such a row is not withheld; it is not listed.
"""
from __future__ import annotations

import logging
import re

from .catalog import CORE_CONCEPT_DEFINITIONS

logger = logging.getLogger(__name__)

#: Shared distinct values required before measured overlap counts as identity.
MIN_SHARED_VALUES = 3
#: Overlap as a fraction of the SMALLER side's distinct token set.
MIN_OVERLAP_RATIO = 0.5
#: Integers at or below this are "small" — sequence / sample numbers, not ids.
SMALL_INTEGER_MAX = 999
#: The Resolve result key listing the field pairs sampled on BOTH sides that
#: share no value: each row is shaped like an ``evidence`` row (overlap 0,
#: each side's sampled count in ``a_tokens`` / ``b_tokens``), plus ``probe``
#: when the target column was searched for the new side's values
#: (:data:`PROBE_NONE_FOUND`, with ``probed_values``) or could not be
#: (:data:`PROBE_UNAVAILABLE`). Kept apart from ``evidence``, which the model
#: prompt and the checkpoint's Value Overlap Evidence read as matches.
UNSHARED_PAIRS_KEY = "unshared_pairs"
PROBE_NONE_FOUND, PROBE_UNAVAILABLE = "none found", "unavailable"
#: The Resolve result key listing what a translation rule's pattern made of
#: the new side's sampled values on a measured-empty pair, one row per pair
#: and pattern pair: the pair's fields, ``source_pattern`` /
#: ``target_pattern``, the source sample size (``a_tokens``), how many of
#: those values the pattern fits (``fit``), how many distinct values it gave
#: (``translated``), the target sample size (``b_tokens``), how many
#: translated values the target holds (``overlap_count``, ``examples``,
#: ``ratio`` of the smaller side) and where they were looked for (``probe``:
#: :data:`PROBE_SEARCHED` its sample and a search, :data:`PROBE_UNAVAILABLE`
#: its sample only, :data:`PROBE_PATTERN` a wildcard target compared with the
#: sample only). Built by the server from the samples; never read from a rule.
TRANSFORM_CHECKS_KEY = "transform_checks"
PROBE_SEARCHED, PROBE_PATTERN = "searched", "pattern"
#: The key a rule carries when :func:`gate_storable_rules` set its pattern
#: pair from the measurement rather than taking the model's: why, in a
#: sentence. Server-owned: the gate drops it from every rule it is handed.
PATTERN_ORIGIN_KEY = "pattern_origin"
#: The key a rule carries when Resolve proposed it from a measured pair no
#: other rule named (:func:`measured_pair_rules`): what was measured, in a
#: sentence. Server-owned: stripped from every model rule
#: (:data:`SERVER_OWNED_RULE_FIELDS`).
MEASURED_PAIR_KEY = "measured_pair"
#: The key a measured row carries when Resolve measured it for a rule that
#: spelled its pair differently from every candidate (a tag's last segment,
#: two concepts): the field pair it measured, as a candidate row spells it
#: (``target_system``, ``source_field``, ``target_field``). Server-built.
MEASURED_AS_KEY = "measured_as"

#: Concept tokens that name a measure or a point in time — never an identity.
MEASURE_TEMPORAL_CONCEPT_TOKENS = frozenset({
    "cost", "price", "amount", "quantity", "qty", "total", "value", "measure",
    "rate", "weight", "temperature", "pressure", "speed", "duration",
    "date", "time", "timestamp", "datetime", "schedule", "scheduled",
    "start", "end", "due", "created", "updated", "modified",
})
#: The core concepts (``catalog.CORE_CONCEPT_DEFINITIONS``) that name a
#: classification (a category or status a record HAS), never one record: two
#: rows sharing a reason code are two different stops, two rows sharing a
#: status two different orders. A core concept is decided by this list alone,
#: never by its name; each of the 65 core definitions was read, and these nine
#: are the ones whose values are categories (a test pins the set).
CORE_CATEGORY_STATUS_CONCEPTS = frozenset({
    "asset-class", "criticality", "defect-type", "failure-mode", "inspection-result",
    "lifecycle-state", "machine-state", "order-status", "product-category",
})
_unknown_core = sorted(CORE_CATEGORY_STATUS_CONCEPTS - set(CORE_CONCEPT_DEFINITIONS))
if _unknown_core:
    raise ValueError(f"CORE_CATEGORY_STATUS_CONCEPTS names no core concept: {_unknown_core}")
#: Name tokens that mark a classification in a concept OUTSIDE the core
#: vocabulary (a concept the mapper proposed or onboarding minted), matched as
#: whole tokens. Together they spell all nine core classifications above, and
#: no core identity concept carries one. reason, kind, disposition, priority
#: and severity name no core id; they catch proposed concepts such as
#: ``downtime-reason`` or ``reason-code``. reason, disposition, priority and
#: severity are field names the core definitions list for failure-mode,
#: inspection-result and criticality; kind is a synonym of type. A concept the
#: graph records as entity-layer is exempt (:func:`is_category_or_status_concept`).
CATEGORY_STATUS_CONCEPT_TOKENS = frozenset({
    "mode", "type", "status", "state", "category", "class", "reason", "result",
    "kind", "disposition", "criticality", "priority", "severity",
})
#: Which of ``$ids`` the graph records as entity-layer concepts: a table's
#: record type, minted from the table name (``layer = 'entity'``).
ENTITY_LAYER_READ = ("MATCH (c:Concept) WHERE c.id IN $ids AND c.layer = 'entity' "
                     "RETURN c.id AS id")
#: Field-name tokens that mark an identifier-shaped column.
IDENTIFIER_NAME_TOKENS = frozenset({
    "id", "ident", "identifier", "uid", "uuid", "guid", "num", "number", "no",
    "code", "key", "lot", "batch", "serial", "tag", "sku", "ref", "reference",
    "barcode", "asset", "order", "wo",
})

_DECIMAL_RE = re.compile(r"^[+-]?\d+\.\d+$")
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
    r"|^\d{1,2}/\d{1,2}/\d{2,4}$")
_BOOLEAN_VALUES = frozenset({"true", "false", "yes", "no", "y", "n", "t", "f"})


def _tokens(name: str) -> list[str]:
    """Lower-cased alnum tokens of a concept / field name, split on any
    non-alnum and on camelCase boundaries."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
    return [t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t]


def _column(field: str) -> str:
    return str(field or "").strip().split(".")[-1].lower()


def is_measure_or_temporal_concept(concept: str) -> bool:
    return any(t in MEASURE_TEMPORAL_CONCEPT_TOKENS for t in _tokens(concept))


def _concept_key(concept) -> str:
    return str(concept or "").strip().lower()


def _named_as_category(concept) -> bool:
    return any(t in CATEGORY_STATUS_CONCEPT_TOKENS for t in _tokens(concept))


def is_category_or_status_concept(concept: str, entity_concepts=frozenset()) -> bool:
    """Whether ``concept`` names a category or status. A core concept is
    decided by :data:`CORE_CATEGORY_STATUS_CONCEPTS`. Any other concept by its
    name tokens, unless it is in ``entity_concepts`` (the ids the graph records
    as entity-layer, :func:`entity_layer_concepts`): an entity concept is a
    table's record type, so it names records whatever its name says (a minted
    ``state-transitions`` is a table of transitions, not a state)."""
    key = _concept_key(concept)
    if key in CORE_CONCEPT_DEFINITIONS:
        return key in CORE_CATEGORY_STATUS_CONCEPTS
    if key in {_concept_key(c) for c in entity_concepts or ()}:
        return False
    return _named_as_category(concept)


def entity_layer_concepts(concepts, query) -> frozenset:
    """The ids among ``concepts`` the graph records as entity-layer
    (:data:`ENTITY_LAYER_READ`), lower-cased. ``query`` is the caller's graph
    read; it is called once, and only for the non-core concepts whose names
    would be withheld (none: no read). A failed read answers none: the name
    rule then decides, so a rule is withheld rather than written on a layer
    nobody read."""
    ids = sorted({str(c).strip() for c in concepts or ()
                  if _concept_key(c) and _concept_key(c) not in CORE_CONCEPT_DEFINITIONS
                  and _named_as_category(c)})
    if not ids:
        return frozenset()
    try:
        rows = query(ENTITY_LAYER_READ, {"ids": ids}) or []
    except Exception as exc:  # noqa: BLE001 - a failed read withholds, never writes
        logger.info("[RESOLVE] entity-layer read failed (%s); concept names decide",
                    type(exc).__name__)
        return frozenset()
    return frozenset(_concept_key(r.get("id")) for r in rows
                     if isinstance(r, dict) and _concept_key(r.get("id")))


def category_rejection(rule: dict, entity_concepts=frozenset()) -> str:
    """The reason a rule on a category or status concept is withheld, else
    ``""``. Reads only the rule's concept (and ``entity_concepts``), so it
    holds for a rule however it was found (the model, the identifier-core
    affinity pass, the chat) and wherever it is written (the provision
    writer, a proposal accept)."""
    concept = str((rule or {}).get("concept") or "") if isinstance(rule, dict) else ""
    if is_category_or_status_concept(concept, entity_concepts):
        return f"concept '{concept.strip()}' names a category or status, not one record"
    return ""


def is_identifier_like_name(field: str) -> bool:
    return any(t in IDENTIFIER_NAME_TOKENS for t in _tokens(_column(field)))


def non_identifier_shape(values) -> str:
    """Name the non-identifier shape ALL of ``values`` share, or ``""``.

    Shapes: ``decimal measure`` (every value has a fractional part),
    ``date/timestamp``, ``boolean``, ``small dense integer range`` (every value
    an integer <= ``SMALL_INTEGER_MAX`` and the span no wider than twice the
    count — a sequence, not a key). Sparse or large integers are left alone:
    surrogate keys are integers too.
    """
    vals = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not vals:
        return ""
    if all(_DECIMAL_RE.match(v) for v in vals):
        return "decimal measure"
    if all(_DATE_RE.match(v) for v in vals):
        return "date/timestamp"
    if all(v.lower() in _BOOLEAN_VALUES for v in vals):
        return "boolean"
    if all(_INTEGER_RE.match(v) for v in vals):
        ints = sorted(int(v) for v in vals)
        span = ints[-1] - ints[0] + 1
        if abs(ints[-1]) <= SMALL_INTEGER_MAX and span <= 2 * len(ints):
            return "small dense integer range"
    return ""


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _ends_match(rule_end, evidence_end) -> bool:
    """Whether a rule's field names the measured field: the same spelling
    once the leading qualifiers one side leaves out are set aside (case and
    padding aside too). The model sometimes drops the table (``code`` for
    ``jobs.code``) or adds the system id it was shown in the samples
    (``sys.jobs.code``); ``tasks.code`` never names ``jobs.code``."""
    a, b = _norm(rule_end), _norm(evidence_end)
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _same_concept(rule: dict, row: dict) -> bool:
    concept = _norm(rule.get("concept"))
    return bool(concept) and concept in {_norm(row.get("concept")),
                                         _norm(row.get("related_concept"))}


def evidence_match(rule: dict, evidence: list) -> tuple:
    """``(row, candidates)``: the measured evidence row for this rule's field
    pair and how many rows could be it. The exact table-qualified pair first
    (``(row, 1)``); else the rows whose target system and concept (or declared
    related concept) match and whose ends match by :func:`_ends_match`. One
    such row is the pair; none is ``(None, 0)``; more than one is ambiguous,
    ``(None, n)``: the rule dropped a table that two measured pairs differ by."""
    source, target = _norm(rule.get("source_field")), _norm(rule.get("target_field"))
    system = _norm(rule.get("target_system"))
    rows = [e for e in evidence or [] if isinstance(e, dict)
            and _norm(e.get("target_system")) == system]
    for e in rows:
        if _norm(e.get("source_field")) == source and _norm(e.get("target_field")) == target:
            return e, 1
    loose = [e for e in rows if _same_concept(rule, e)
             and _ends_match(rule.get("source_field"), e.get("source_field"))
             and _ends_match(rule.get("target_field"), e.get("target_field"))]
    if len(loose) == 1:
        return loose[0], 1
    return None, len(loose)


def _source_spelling(rule: dict) -> str:
    """The source field a rule names, read with its ``source_table`` when it
    gives one and the field does not already carry that table: ``author``
    with table ``work_log`` is ``work_log.author``."""
    field = str(rule.get("source_field") or "").strip()
    table = str(rule.get("source_table") or "").strip()
    if not table or not field:
        return field
    f, t = _norm(field), _norm(table)
    leaf = t.rsplit(".", 1)[-1]
    if f.startswith(t + ".") or f.startswith(leaf + ".") or f".{leaf}." in f:
        return field
    return f"{table}.{field}"


def _path_suffix(whole: str, part: str) -> bool:
    """``part`` is ``whole`` or its trailing part on a ``.`` or ``/`` boundary
    (a dotted field or a nested tag path); never part of one segment."""
    return bool(part) and (whole == part or whole.endswith("." + part)
                           or whole.endswith("/" + part))


def _spells_endpoint(spelling: str, table: str, field: str) -> bool:
    """Whether a normalized source spelling names the normalized own endpoint
    ``field`` (of ``table``): the endpoint itself or a trailing part of it
    (``orders.order_id``, a bare ``order_id``, a tag's leaf ``Count`` or
    ``CellA/Count``), the endpoint with qualifiers in front (a database, a
    schema or the system id), or the endpoint's whole table followed by a
    trailing part of its column (``get_values.Count`` for the tag
    ``get_values.Ent/Site/Line1/CellA/Count``). The column is always checked:
    a matching table segment alone names nothing."""
    if _path_suffix(field, spelling) or spelling.endswith("." + field):
        return True
    if not table or not field.startswith(table + "."):
        return False
    column, lead = field[len(table) + 1:], table + "."
    start = 0
    while (at := spelling.find(lead, start)) >= 0:
        if (at == 0 or spelling[at - 1] == ".") and _path_suffix(column, spelling[at + len(lead):]):
            return True
        start = at + 1
    return False


def names_own_field(rule: dict, own_fields) -> bool:
    """Whether ``rule``'s source field is one of ``own_fields``, the
    ``(table, table-qualified field)`` pairs of the system being resolved, by
    :func:`_spells_endpoint` (case and padding aside; ``source_table`` read
    in front of a field that does not carry it). ``Spindle.author`` is not
    ``work_log.author``, and ``archive.orders.other_column`` is not
    ``sales.orders.order_id`` though both name a table ``orders``."""
    spelling = _norm(_source_spelling(rule))
    return bool(spelling) and any(_spells_endpoint(spelling, _norm(t), _norm(f))
                                  for t, f in own_fields or ())


def _names(values) -> set:
    return {_norm(v) for v in values or () if _norm(v)}


def states_peer_source(rule: dict, own_names, peer_names) -> bool:
    """Whether ``rule`` states a ``source_system`` that names another system
    (an id or display name in ``peer_names``) and not the system being
    resolved (``own_names``, which win a shared name). An absent or unknown
    value states nothing, and the field decides."""
    stated = _norm(rule.get("source_system"))
    return bool(stated) and stated not in _names(own_names) and stated in _names(peer_names)


#: Keys the server owns on an identity rule: fields the deterministic passes
#: write, never the model. Neither resolve prompt (the wizard concept pass,
#: the chat's resolve) asks the model for any of them, so a model rule that
#: carries one copied it from the samples or the prompt. ``method`` and
#: ``transform`` mark an affinity or exact-agreement rule and ``shared_count``
#: its count (:data:`AFFINITY_METHOD` / ``identity_affinity``); ``value_check``
#: is a server-measured evidence field, :data:`MEASURED_PAIR_KEY` marks a
#: rule Resolve proposed from its own measurement, and :data:`MEASURED_AS_KEY`
#: belongs on a measured row, never a rule. Stripped from every parsed model
#: rule so a model cannot forge measured evidence by writing one of these
#: itself.
SERVER_OWNED_RULE_FIELDS = ("value_check", "method", "transform", "shared_count",
                            MEASURED_PAIR_KEY, MEASURED_AS_KEY)


def sanitize_model_rules(rules):
    """``rules`` with :data:`SERVER_OWNED_RULE_FIELDS` dropped from every
    model rule, so only the deterministic passes can add a measurement.
    Returns a new list; a non-dict rule is left as it is (the gates reject
    it). Pure, and run by both resolve parsers before any merge, gate or
    checkpoint."""
    out = []
    for rule in rules or []:
        if isinstance(rule, dict) and any(k in rule for k in SERVER_OWNED_RULE_FIELDS):
            out.append({k: v for k, v in rule.items() if k not in SERVER_OWNED_RULE_FIELDS})
        else:
            out.append(rule)
    return out


def keep_own_candidates(result: dict, own_fields, own_names=(), peer_names=()) -> dict:
    """``result`` without the rules and withheld rows that are not the
    system being resolved's: a stated ``source_system`` that names another
    system (:func:`states_peer_source`; the resolve prompts ask for the new
    system's id there), or a source field that is not one of the system's
    (:func:`names_own_field`).

    ``own_fields`` are that system's ``(table, table-qualified field)`` pairs
    (its mappings and identity keys); the ``source_field`` of every measured
    ``evidence`` row counts too, since the concept pass samples only the
    system's own fields. ``own_names`` / ``peer_names`` are the ids and
    display names of the system and of every other system. Such a candidate
    pairs two OTHER systems: it is left out, not withheld (logged by count),
    so the checkpoint never lists another system's pair and provision never
    writes one as this system's rule. A stated own ``source_system`` never
    makes another system's field the system's. A row with no source field (a
    malformed rule) is left to the gates. With no field and no peer known the
    result is returned as it is, and so it is when nothing is left out."""
    own = [(str(t or ""), str(f)) for t, f in own_fields or () if str(f or "").strip()]
    own += [("", str(e.get("source_field"))) for e in result.get("evidence") or []
            if isinstance(e, dict) and str(e.get("source_field") or "").strip()]
    if not own and not _names(peer_names):
        return result

    def foreign(row) -> bool:
        if not isinstance(row, dict):
            return False
        if states_peer_source(row, own_names, peer_names):
            return True
        return (bool(own) and bool(str(row.get("source_field") or "").strip())
                and not names_own_field(row, own))

    out, left_out = dict(result), []
    for key in ("rules", "rejected_rules"):
        items = result.get(key)
        if not isinstance(items, list):
            continue
        out[key] = [row for row in items if not foreign(row)]
        left_out += [row for row in items if foreign(row)]
    if not left_out:
        return result
    logger.info("[RESOLVE] %d candidate(s) left out, another system's source: %s",
                len(left_out), "; ".join(f"{r.get('source_system') or '?'}:{r.get('source_field')}"
                                         f" -> {r.get('target_system')}.{r.get('target_field')}"
                                         for r in left_out[:5]))
    return out


def evidence_for_rule(rule: dict, evidence: list) -> dict | None:
    """The measured evidence row for this rule's field pair
    (:func:`evidence_match`); ``None`` when the pair was never measured or the
    match is ambiguous."""
    return evidence_match(rule, evidence)[0]


def reads_values_unchanged(rule: dict) -> bool:
    """Whether a rule's pattern pair reads a value unchanged: no pattern, or
    the same text on both sides (the bare ``*``, the shared values, or the
    same ``{placeholder}`` pattern twice), so a value of one field must be a
    value of the other as it is."""
    return (str(rule.get("source_pattern") or "").strip()
            == str(rule.get("target_pattern") or "").strip())


def _values(n: int) -> str:
    return f"{n} value{'' if n == 1 else 's'}"


def _unshared_reason(row: dict) -> str:
    """What was measured for a pair that shares no value, as a sentence."""
    target = f"{row.get('target_system')} {row.get('target_field')}"
    reason = (f"measured, nothing shared: none of the {_values(int(row.get('a_tokens') or 0))} "
              f"sampled from {row.get('source_field')} is among the "
              f"{int(row.get('b_tokens') or 0)} sampled from {target}")
    if row.get("probe") == PROBE_NONE_FOUND:
        reason += f"; a search of {target} for all {int(row.get('probed_values') or 0)} found none"
    elif row.get("probe") == PROBE_UNAVAILABLE:
        reason += (f"; {row.get('target_system')} could not be searched by value, so only the "
                   "samples were compared")
    return reason


def _patterns(rule: dict) -> tuple:
    return (str(rule.get("source_pattern") or "").strip(),
            str(rule.get("target_pattern") or "").strip())


def _proposed_patterns(check: dict) -> tuple:
    """The pattern pair a translation check was made for: the rule's own,
    which the check's ``source_pattern`` / ``target_pattern`` are unless
    Resolve completed it (``proposed_source_pattern`` / ``proposed_target_pattern``)."""
    if "proposed_source_pattern" in check or "proposed_target_pattern" in check:
        return (str(check.get("proposed_source_pattern") or "").strip(),
                str(check.get("proposed_target_pattern") or "").strip())
    return _patterns(check)


def transform_check_for(rule: dict, checks, row: dict) -> dict | None:
    """The :data:`TRANSFORM_CHECKS_KEY` row the server measured for
    ``rule``'s pattern pair (as the rule proposed it) on the measured pair
    ``row``, or None."""
    pair = tuple(_norm(row.get(k)) for k in ("source_field", "target_system", "target_field"))
    for check in checks or ():
        if (isinstance(check, dict) and _proposed_patterns(check) == _patterns(rule)
                and tuple(_norm(check.get(k)) for k in
                          ("source_field", "target_system", "target_field")) == pair):
            return check
    return None


def completed_patterns(check) -> tuple | None:
    """The pattern pair Resolve completed a translation check's rule to (the
    literal text its measured values share moved into both patterns), or
    None when the check measured the rule as proposed."""
    if not isinstance(check, dict) or _proposed_patterns(check) == _patterns(check):
        return None
    return _patterns(check)


def _searched_where(check: dict) -> str:
    """Where a translation check looked for its translated values."""
    if check.get("probe") == PROBE_SEARCHED:
        return " (its sample and a search)"
    if check.get("probe") == PROBE_PATTERN:
        return " (its sample; a translated value with a wildcard is compared with the sample only)"
    return f" (its sample; {check.get('target_system')} could not be searched by value)"


#: A translation check's ``readers`` when one side of the pair has no
#: literal text and its values share none to add (``one_way_side`` names it),
#: and when the two identifier readers read a sampled value differently
#: (``odd_value``).
READERS_ONE_WAY, READERS_DIFFER = "one way", "differ"


def _readers_rejection(check: dict, row: dict) -> str:
    """Why a translation check shows the two identifier readers cannot read
    the rule alike from both sides, else ``""`` (:data:`READERS_ONE_WAY`,
    :data:`READERS_DIFFER`)."""
    if check.get("readers") == READERS_ONE_WAY:
        side = "target" if check.get("one_way_side") == "target" else "source"
        pattern = _proposed_patterns(check)[side == "target"]
        values = ("the values sampled from it" if side == "source"
                  else "the values its placeholder takes there")
        field = row.get("source_field") if side == "source" else (
            f"{row.get('target_system')} {row.get('target_field')}")
        return (f"measured through its pattern: its {side} pattern '{pattern}' has no literal "
                f"text, so the question reader never translates a value of {field}, and "
                f"{values} share no literal text to add")
    if check.get("readers") == READERS_DIFFER:
        from .resolve_evidence import quoted_text
        sp, tp = _patterns(check)
        return (f"measured through its pattern: the two identifier readers would read value "
                f"{quoted_text(check.get('odd_value'))} differently with '{sp}' -> '{tp}'")
    return ""


def _transform_rejection(rule: dict, row: dict, checks) -> str:
    """Why a translation (a pattern pair that does not read values
    unchanged) on the measured pair ``row`` is withheld, else ``""``:
    judged on what its pattern made of the new side's sampled values and how
    many of those the target holds (:func:`transform_check_for`), on the
    usual bar, whether or not the pair's tokens already overlap. A pattern no
    check was made for (it cannot be stored, so it translates nothing) is
    withheld with provision's reason, or what was measured."""
    check = transform_check_for(rule, checks, row)
    shared = int(row.get("overlap_count") or 0)
    if check is None:
        return pattern_rejection(rule) or (_unshared_reason(row) if shared == 0 else (
            f"its pattern was not applied to the values sampled from {row.get('source_field')}, "
            f"so the {_values(shared)} the two fields share say nothing about it"))
    readers = _readers_rejection(check, row)
    if readers:
        return readers
    src, target = row.get("source_field"), f"{row.get('target_system')} {row.get('target_field')}"
    sampled, fit = int(check.get("a_tokens") or 0), int(check.get("fit") or 0)
    found, translated = int(check.get("overlap_count") or 0), int(check.get("translated") or 0)
    if fit == 0:
        return (f"{'measured, nothing shared' if shared == 0 else 'measured through its pattern'}: "
                f"its pattern '{_patterns(rule)[0]}' fits none of the "
                f"{_values(sampled)} sampled from {src}")
    if found < MIN_SHARED_VALUES:
        return (f"measured through its pattern: {fit} of the {_values(sampled)} sampled from "
                f"{src} translate, and {found} of the translated values "
                f"{'is' if found == 1 else 'are'} in {target}{_searched_where(check)} "
                f"(need >= {MIN_SHARED_VALUES})")
    ratio = float(check.get("ratio") or 0.0)
    if ratio < MIN_OVERLAP_RATIO:
        return (f"measured through its pattern: {found} of the {translated} translated values of "
                f"{src} are in {target}{_searched_where(check)}, {ratio:.0%} of the smaller side "
                f"(need >= {MIN_OVERLAP_RATIO:.0%})")
    shape = non_identifier_shape(check.get("examples"))
    return f"shared values are a {shape}, not identifiers" if shape else ""


def rule_rejection(rule: dict, evidence: list, unshared=(), checks=()) -> str:
    """Why this rule must NOT be proposed, or ``""`` when it may be.

    The rule is paired with every measured pair at once, the ones that share
    values (``evidence``) and the ones measured with nothing shared
    (``unshared``, :data:`UNSHARED_PAIRS_KEY`): more than one candidate is
    ambiguous, whichever kind. A rule is then judged on what its runtime
    reader compares. One that reads values unchanged needs shared values
    that are the same whole value on both sides (:func:`_unchanged_rejection`;
    on a measured-empty pair the samples contradict it). A translation is
    judged on its own check (``checks``, :func:`_transform_rejection`), on a
    pair that shares tokens as on one that shares none: the values it joins
    are the ones its pattern gives. A pattern provision cannot store
    translates nothing: on a pair that shares values it passes this gate on
    the shared values, and the storable gate either rebuilds it into the
    exact-agreement rule, judged here on whole values, or withholds it with
    provision's reason (:func:`gate_storable_rules`). Only a pair never
    measured (a side gave no sample) falls to the field names."""
    concept = str(rule.get("concept") or "")
    if is_measure_or_temporal_concept(concept):
        return f"concept '{concept}' names a measure or point in time, not an identity"
    ev, candidates = evidence_match(rule, list(evidence or []) + list(unshared or []))
    if ev is None and candidates > 1:
        return (f"the measured evidence is ambiguous: {candidates} measured field pairs "
                "share these column names")
    if ev is None:
        if is_identifier_like_name(rule.get("source_field")) and \
                is_identifier_like_name(rule.get("target_field")):
            return ""
        return "no measured value overlap and the fields are not identifier-like"
    shared = int(ev.get("overlap_count") or 0)
    unchanged_reader = reads_values_unchanged(rule)
    if not unchanged_reader and (shared == 0 or not storage_rejection(rule)):
        return _transform_rejection(rule, ev, checks)
    if shared == 0:
        return _unshared_reason(ev)
    ratio = float(ev.get("ratio") or 0.0)
    if shared < MIN_SHARED_VALUES:
        return f"only {shared} shared value(s) (need >= {MIN_SHARED_VALUES})"
    if ratio < MIN_OVERLAP_RATIO:
        return f"overlap {ratio:.0%} of the smaller side (need >= {MIN_OVERLAP_RATIO:.0%})"
    shape = non_identifier_shape(ev.get("examples"))
    if shape:
        return f"shared values are a {shape}, not identifiers"
    return _unchanged_rejection(ev, shared, ratio) if unchanged_reader else ""


def _unchanged_rejection(row: dict, shared: int, ratio: float) -> str:
    """Why the shared values of ``row`` do not support a rule that reads
    values unchanged, else ``""``. Such a rule is joined at runtime on the
    whole value: the JoinPath reader compares the target column, cast to
    text, with the value as it is (``resolve._join_row``), and the route seed
    carries the identifier it found as it is
    (``identifier_translation.find_owners``). So the bar is on the shared
    values that are the same whole value on both sides (``unchanged_count``;
    a row without one has none), not on the tokens the overlap's reader
    finds once it sets padding aside or reads a JSON value's members out."""
    unchanged = min(int(row.get("unchanged_count") or 0), shared)
    if unchanged < MIN_SHARED_VALUES:
        return (f"{unchanged} of the {shared} shared values "
                f"{'is' if unchanged == 1 else 'are'} the same whole value in both fields "
                f"(need >= {MIN_SHARED_VALUES}); the others are shared only once padding is "
                "set aside or a JSON value's members are read out, and a rule that reads "
                "values unchanged joins whole values")
    whole = ratio * unchanged / shared
    if whole < MIN_OVERLAP_RATIO:
        return (f"{unchanged} of the {shared} shared values are the same whole value in both "
                f"fields, {whole:.0%} of the smaller side (need >= {MIN_OVERLAP_RATIO:.0%})")
    return ""


def _pair_key(row: dict) -> tuple:
    """A measured pair's field-pair triple: target system and both fields, normalized."""
    return tuple(_norm(row.get(k)) for k in ("target_system", "source_field", "target_field"))


def _named_pair_keys(rule: dict, rows: list) -> set:
    """The field pairs (:func:`_pair_key`) ``rule`` names among the measured
    ``rows`` (sharing values or not): the ONE row the evidence gate judges it
    on (:func:`evidence_match`), with the field pair Resolve measured that row
    for when it was measured for a rule's own spelling
    (:data:`MEASURED_AS_KEY`). Empty when the gate pairs it with no row or is
    ambiguous: a shorthand such as ``author`` that two measured pairs end
    with names neither of them."""
    row, _candidates = evidence_match(rule, rows)
    if row is None:
        return set()
    measured_as = row.get(MEASURED_AS_KEY)
    return {_pair_key(row)} | ({_pair_key(measured_as)} if isinstance(measured_as, dict) else set())


def measured_pair_rules(system_id: str, result: dict) -> list:
    """Rules for the measured pairs (``result['evidence']``) that no rule of
    ``result`` names (:func:`_named_pair_keys`), one per field pair, in
    measured order: each reads its values unchanged, carries the pair's
    concept and its overlap as its confidence, and is kept only when it
    meets the measured evidence bar (:func:`rule_rejection`: shared count,
    overlap, shape, whole values, never a measure or a point in time). The
    model sees these pairs and can leave one out of its answer; a pair the
    app measured above the bar is then still a candidate. A pair counts as
    named only when the gate judges a rule on it, so a rule the gate finds
    ambiguous suppresses nothing. A row measured for a rule's own spelling
    (:data:`MEASURED_AS_KEY`) is named by that rule and is never a source.
    Every later gate (category, plants, storability) judges a rule made here
    as it judges a model rule."""
    evidence = [e for e in result.get("evidence") or [] if isinstance(e, dict)]
    unshared = list(result.get(UNSHARED_PAIRS_KEY) or [])
    rows = evidence + [u for u in unshared if isinstance(u, dict)]
    seen: set = set()
    for r in list(result.get("rules") or []) + list(result.get("rejected_rules") or []):
        if isinstance(r, dict):
            seen |= _named_pair_keys(r, rows)
    out: list = []
    for row in evidence:
        key = _pair_key(row)
        if key in seen or isinstance(row.get(MEASURED_AS_KEY), dict):
            continue
        seen.add(key)
        shared = int(row.get("overlap_count") or 0)
        rule = {"concept": row.get("concept"), "source_system": system_id,
                "source_field": row.get("source_field"), "source_pattern": "",
                "target_system": row.get("target_system"), "target_field": row.get("target_field"),
                "target_pattern": "", "confidence": round(float(row.get("ratio") or 0.0), 3),
                "reasoning": "Resolve measured the values the two fields share; "
                             "the model proposed no rule for this pair.",
                MEASURED_PAIR_KEY: (f"proposed from the measurement, not the model: the two "
                                    f"fields share {_values(shared)} unchanged")}
        if not rule_rejection(rule, evidence, unshared):
            out.append(rule)
    return out


def gate_overlap_rules(result: dict) -> dict:
    """Split ``result['rules']`` into kept rules and ``rejected_rules`` (each
    carrying ``rejected_reason``, after any already there, one row per
    candidate: :func:`merge_withheld`, so a rule the model sent twice is
    listed once). Every other key, including the measured ``evidence``
    itself, passes through unchanged. A pair measured with nothing shared is
    read from :data:`UNSHARED_PAIRS_KEY`, a translation's check from
    :data:`TRANSFORM_CHECKS_KEY`."""
    evidence = list(result.get("evidence") or [])
    unshared = list(result.get(UNSHARED_PAIRS_KEY) or [])
    checks = list(result.get(TRANSFORM_CHECKS_KEY) or [])
    kept: list = []
    rejected: list = []
    for rule in result.get("rules") or []:
        if not isinstance(rule, dict):
            rejected.append({"rule": rule, "rejected_reason": "malformed rule"})
            continue
        reason = rule_rejection(rule, evidence, unshared, checks)
        if not reason:
            kept.append(rule)
            continue
        # Only the storable gate sets the marker; a withheld row never shows one.
        rejected.append({**{k: v for k, v in rule.items() if k != PATTERN_ORIGIN_KEY},
                         "rejected_reason": reason})
        logger.info("[RESOLVE] identity rule dropped (%s): %s -> %s.%s", reason,
                    rule.get("source_field"), rule.get("target_system"), rule.get("target_field"))
    return {**result, "rules": kept,
            "rejected_rules": merge_withheld(result.get("rejected_rules") or [], rejected)}


def same_candidate(a: dict, b: dict) -> bool:
    """Whether two rules MAY name one candidate: the same concept and target
    system, and field ends that match by :func:`_ends_match` (case, padding
    and a dropped table aside). A loose match alone does not make them one:
    :func:`merge_withheld` also needs it to be the only one."""
    return (_norm(a.get("concept")) == _norm(b.get("concept"))
            and _norm(a.get("target_system")) == _norm(b.get("target_system"))
            and _ends_match(a.get("source_field"), b.get("source_field"))
            and _ends_match(a.get("target_field"), b.get("target_field")))


def _candidate_key(row) -> tuple | None:
    """A withheld row's candidate: normalized (concept, target system,
    source field, target field); ``None`` for a row without a concept or
    field pair (a malformed rule), which is never merged."""
    if not isinstance(row, dict):
        return None
    key = (_norm(row.get("concept")), _norm(row.get("target_system")),
           _norm(row.get("source_field")), _norm(row.get("target_field")))
    return key if key[0] and key[2] and key[3] else None


def _no_more_qualified(short: tuple, long: tuple) -> bool:
    """Every end of ``short`` is ``long``'s end or a suffix of it."""
    return all(b == a or b.endswith("." + a) for a, b in zip(short[2:], long[2:]))


def merge_withheld(prior: list, new: list) -> list:
    """``prior`` then ``new`` withheld rows, one row per candidate.

    Two rows are one candidate when their keys (:func:`_candidate_key`) are
    equal, or when they match loosely (:func:`same_candidate`, a table left
    out) and each is the other's ONLY loose match among all the rows: the
    first-listed row then stays, with its reason. A loose reading that could
    be several candidates resolves to none of them: a row whose ends are
    a shorter spelling of two or more listed candidates (``code`` beside
    ``live.code`` and ``archive.code``) is not listed, and those candidates
    are, each with its own reason. Rows without a field pair are kept as
    they are."""
    rows = list(prior or []) + list(new or [])
    keyed = {}
    for row in rows:
        key = _candidate_key(row)
        if key is not None:
            keyed.setdefault(key, row)
    loose = {key: {other for other, row in keyed.items()
                   if other != key and same_candidate(keyed[key], row)} for key in keyed}
    out: list = []
    listed: set = set()
    for row in rows:
        key = _candidate_key(row)
        if key is None:
            out.append(row)
            continue
        if key in listed:
            continue
        matches = loose[key]
        if len(matches) >= 2 and all(_no_more_qualified(key, m) for m in matches):
            continue
        if len(matches) == 1:
            (other,) = matches
            if other in listed and loose[other] == {key}:
                continue
        listed.add(key)
        out.append(row)
    return out


def gate_category_rules(result: dict, entity_concepts=frozenset()) -> dict:
    """``result`` with every rule on a category or status concept
    (:func:`category_rejection`, ``entity_concepts`` exempt) moved from
    ``rules`` to ``rejected_rules``, appended after any already there, each
    with its ``rejected_reason``, one row per candidate (:func:`merge_withheld`):
    after the affinity pass re-adds a pair the model's rule was withheld for,
    the checkpoint (which shows no method) lists it once, with the first
    reason. Every other key passes unchanged; ``result`` itself when nothing
    is withheld."""
    rules = result.get("rules")
    if not isinstance(rules, list) or not rules:
        return result
    kept: list = []
    withheld: list = []
    for rule in rules:
        reason = category_rejection(rule, entity_concepts) if isinstance(rule, dict) else ""
        if not reason:
            kept.append(rule)
            continue
        withheld.append({**rule, "rejected_reason": reason})
        logger.info("[RESOLVE] identity rule dropped (%s): %s -> %s.%s", reason,
                    rule.get("source_field"), rule.get("target_system"), rule.get("target_field"))
    if not withheld:
        return result
    return {**result, "rules": kept,
            "rejected_rules": merge_withheld(result.get("rejected_rules") or [], withheld)}


def storage_rejection(rule: dict) -> str:
    """Why ``rule``'s pattern pair is not valid to store
    (``resolve.identity_pattern_problem``, the one check every IdentityRule
    writer uses), else ``""``."""
    from .resolve import identity_pattern_problem
    src, tgt = rule.get("source_pattern", ""), rule.get("target_pattern", "")
    problem = identity_pattern_problem(src, tgt)
    return f"pattern '{src}' -> '{tgt}' is not a valid pattern: {problem}" if problem else ""


#: The methods of the rules the deterministic affinity pass builds from
#: measured value cores (``identity_affinity``): their one-way reading of a
#: side with no literal text is the documented design (a bare batch number to
#: many part revisions), and they are held to that pass's own gates.
_MEASURED_METHODS = frozenset({"value_core_match", "value_exact_match"})


def one_way_rejection(rule: dict) -> str:
    """Why ``rule`` is read by only one identifier reader from one side,
    else ``""``. Each runtime reader applies a pair in both directions, but
    the route seed (``identifier_translation.apply``, which finds identifiers
    in a person's question) never reads FROM a pattern with no literal text:
    a bare ``{id}`` would match every word. So a translation whose side has
    none works for the resolver both ways and for the route seed one way. A
    pair that reads values unchanged translates nothing and is not one-way;
    an affinity rule (:data:`_MEASURED_METHODS`) keeps its documented reading."""
    from .identifier_translation import pattern_regex
    if reads_values_unchanged(rule) or str(rule.get("method") or "") in _MEASURED_METHODS:
        return ""
    src, tgt = _patterns(rule)
    for side, pattern in (("source", src), ("target", tgt)):
        if pattern_regex(pattern) is None:
            return (f"pattern '{src}' -> '{tgt}' works in one direction only: its {side} pattern "
                    "has no literal text, so the question reader never translates a value from "
                    "that side")
    return ""


def pattern_rejection(rule: dict) -> str:
    """Why ``rule``'s pattern pair may not be stored, as provision says it,
    else ``""``: it is not valid to store (:func:`storage_rejection`), or one
    identifier reader could not read it from one side (:func:`one_way_rejection`).
    Provision refuses a rule with this reason (``onboard._pattern_pair_rejection``);
    Resolve withholds one with it (:func:`gate_storable_rules`)."""
    return storage_rejection(rule) or one_way_rejection(rule)


def shares_values_unchanged(row) -> bool:
    """Whether a measured evidence row's shared values are the same whole text
    on both sides (its ``unchanged_count`` is its ``overlap_count``), not only
    after the overlap's token normalization (padding stripped, the values
    inside a JSON blob read out): what an exact-agreement rule joins on."""
    if not isinstance(row, dict):
        return False
    shared = int(row.get("overlap_count") or 0)
    return shared > 0 and row.get("unchanged_count") == shared


def _measured_exact_rule(rule: dict, evidence: list, unshared: list, refusal: str) -> dict | None:
    """The exact-agreement rule (no pattern) that the measurement of
    ``rule``'s own field pair supports, marked with
    :data:`PATTERN_ORIGIN_KEY` (which quotes provision's ``refusal`` of the
    model's pair), or None: the rule must name ONE measured pair among all of
    them (sharing values or not), its shared values must be unchanged on both
    sides (:func:`shares_values_unchanged`) and the rule without a pattern
    must pass the evidence gate on it."""
    row, _candidates = evidence_match(rule, list(evidence) + list(unshared))
    if not shares_values_unchanged(row):
        return None
    exact = {**rule, "source_pattern": "", "target_pattern": ""}
    if rule_rejection(exact, evidence, unshared):
        return None
    exact[PATTERN_ORIGIN_KEY] = (
        f"set from the measurement, not the model ({refusal}): the "
        f"{int(row['overlap_count'])} values the two fields share are the same text in both, "
        "so the rule matches values unchanged")
    return exact


def _completed_rule(rule: dict, evidence: list, unshared: list, checks: list) -> dict:
    """``rule`` with the pattern pair Resolve completed it to on its
    measured pair (:func:`completed_patterns`, the evidence gate judged the
    rule on that pair's check), marked with :data:`PATTERN_ORIGIN_KEY`; else
    ``rule`` itself."""
    row, _candidates = evidence_match(rule, list(evidence) + list(unshared))
    completed = completed_patterns(transform_check_for(rule, checks, row)) if row else None
    if completed is None:
        return rule
    sp, tp = _patterns(rule)
    return {**rule, "source_pattern": completed[0], "target_pattern": completed[1],
            PATTERN_ORIGIN_KEY: (
                f"set from the measurement, not the model: '{sp}' -> '{tp}' has no literal text on "
                f"one side, so the literal text every value its placeholder took shares was added "
                "to both patterns, and both identifier readers read the pair alike")}


def gate_storable_rules(result: dict) -> dict:
    """``result`` with every rule provision would refuse to store
    (:func:`pattern_rejection`) either replaced by the exact-agreement rule
    the measurement of its own field pair supports
    (:func:`_measured_exact_rule`) or moved to ``rejected_rules`` with
    provision's reason (one row per candidate, :func:`merge_withheld`). A
    rule Resolve completed on its measured values (:func:`_completed_rule`)
    is judged in that form. A regular expression never becomes a transform.
    Run once per Resolve, after
    every other gate: it drops :data:`PATTERN_ORIGIN_KEY` from every rule it
    is handed, so only this gate ever sets it. Every other key passes
    unchanged; ``result`` itself when it has no rules."""
    rules = result.get("rules")
    if not isinstance(rules, list) or not rules:
        return result
    evidence = list(result.get("evidence") or [])
    unshared = list(result.get(UNSHARED_PAIRS_KEY) or [])
    checks = list(result.get(TRANSFORM_CHECKS_KEY) or [])
    kept: list = []
    withheld: list = []
    for rule in rules:
        if not isinstance(rule, dict):
            kept.append(rule)
            continue
        rule = _completed_rule({k: v for k, v in rule.items() if k != PATTERN_ORIGIN_KEY},
                               evidence, unshared, checks)
        reason = pattern_rejection(rule)
        if not reason:
            kept.append(rule)
            continue
        exact = _measured_exact_rule(rule, evidence, unshared, reason)
        if exact is not None:
            kept.append(exact)
            logger.info("[RESOLVE] identity rule pattern set from the measurement: %s -> %s.%s",
                        rule.get("source_field"), rule.get("target_system"), rule.get("target_field"))
            continue
        withheld.append({**rule, "rejected_reason": reason})
        logger.info("[RESOLVE] identity rule dropped (%s): %s -> %s.%s", reason,
                    rule.get("source_field"), rule.get("target_system"), rule.get("target_field"))
    if not withheld:
        return {**result, "rules": kept}
    return {**result, "rules": kept,
            "rejected_rules": merge_withheld(result.get("rejected_rules") or [], withheld)}
