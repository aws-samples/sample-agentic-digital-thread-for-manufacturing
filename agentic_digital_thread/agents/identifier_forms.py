# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The identifier forms a system's own graph facts produce, read server-side.

A system agent's answer may state an identifier its results did not hold
verbatim but the graph's machinery produces from one they did hold: an
IdentityRule transform (``LOT-{id}`` to ``B-{id}``, a fixed prefix, fixed
padding digits written into the pattern, a case change) or the other end of a
trusted stored value mapping. The unreturned-identifier check
(``answer_ids``) must accept those, and only those.

The system agent runtime reads them itself, for its own system id, from the
graph it already reads per question (``live_concept_mappings``): the active
IdentityRules with this system at either end, and the stored ValueMappings
with this system at either end that ``identifier_translation.value_mapping_trusted``
keeps. Nothing the caller sends is read: the MCP caller (the orchestrator
through the gateway, or any other client that can reach the runtime) is not
an authenticated party for this, so a value it supplies could only be used to
silence the check. A transform described in free text never counts.

Bounded: at most :data:`MAX_RULES` rules and :data:`MAX_MAPPINGS` mappings
per read, each text at most :data:`MAX_TEXT` characters. A wildcard
(``{*}``) rule names a candidate set, not one identifier, and is left out.
Cached per system for :data:`CACHE_SECONDS`. A failed read gives no forms
(fail closed: every identifier is checked as before), and the graph is left
alone for :data:`FAILURE_PAUSE_SECONDS`.

Stdlib only, apart from the pattern grammar and the value-mapping trust rule
(``identifier_translation``).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .identifier_translation import (CONFIRMED_VALUE_MAPPING_METHODS,
                                     DETERMINISTIC_VALUE_MAPPING_METHODS, has_wildcard,
                                     value_mapping_trusted)
from .rule_status import rule_active

logger = logging.getLogger(__name__)

MAX_RULES = 256
MAX_MAPPINGS = 2000
MAX_TEXT = 128
CACHE_SECONDS = 60.0
FAILURE_PAUSE_SECONDS = 30.0

RULES_CYPHER = (
    "MATCH (ir:IdentityRule) "
    "WHERE (ir.source_system = $sid OR ir.target_system = $sid) "
    f"AND {rule_active('ir')} "
    "RETURN ir.source_pattern AS source_pattern, ir.target_pattern AS target_pattern "
    "ORDER BY coalesce(ir.id, '') "
    f"LIMIT {MAX_RULES + 1}")
MAPPINGS_CYPHER = (
    "MATCH (vm:ValueMapping) "
    "WHERE (vm.from_system = $sid OR vm.to_system = $sid) AND vm.method IN $methods "
    "RETURN vm.id AS id, vm.method AS method, vm.from_value AS from_value, "
    "vm.to_value AS to_value, vm.confirmation_mode AS confirmation_mode, "
    "vm.accepted_by AS accepted_by "
    "ORDER BY coalesce(vm.id, '') "
    f"LIMIT {MAX_MAPPINGS + 1}")


@dataclass(frozen=True)
class IdentifierForms:
    """``rules``: ``(source pattern, target pattern)`` pairs, both directions
    of each rule; ``pairs``: ``(value, value)`` ends of each trusted value
    mapping, both directions, case folded."""
    rules: tuple = ()
    pairs: tuple = ()


NO_FORMS = IdentifierForms()

_lock = threading.Lock()
_cache: dict = {}         # system id -> (read at, IdentifierForms)
_paused_until: dict = {}  # system id -> monotonic


def _monotonic() -> float:
    return time.monotonic()


def reset() -> None:
    """Forget every cached read and pause (tests)."""
    with _lock:
        _cache.clear()
        _paused_until.clear()


def _default_query(cypher: str, parameters: dict) -> list:
    """The system runtime's own graph read (short deadlines, one attempt)."""
    from .live_concept_mappings import _graph_read
    return _graph_read(cypher, parameters)


def _text(value) -> "str | None":
    """``value`` as a bounded one-line string, or ``None``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        return None
    return None if any(ch in value for ch in "\r\n\x00") else value


def _rules(rows) -> tuple:
    rules: list = []
    for row in list(rows or [])[:MAX_RULES]:
        if not isinstance(row, dict):
            continue
        src, tgt = _text(row.get("source_pattern")), _text(row.get("target_pattern"))
        if src and tgt and src != tgt and not (has_wildcard(src) or has_wildcard(tgt)):
            rules.extend([(src, tgt), (tgt, src)])
    return tuple(dict.fromkeys(rules))


def _pairs(rows) -> tuple:
    pairs: list = []
    for row in list(rows or [])[:MAX_MAPPINGS]:
        if not value_mapping_trusted(row):
            continue
        a, b = _text(row.get("from_value")), _text(row.get("to_value"))
        if a and b and a.casefold() != b.casefold():
            pairs.extend([(a.casefold(), b.casefold()), (b.casefold(), a.casefold())])
    return tuple(dict.fromkeys(pairs))


def _read(system_id: str, query) -> IdentifierForms:
    methods = sorted(set(DETERMINISTIC_VALUE_MAPPING_METHODS) | set(CONFIRMED_VALUE_MAPPING_METHODS))
    rule_rows = query(RULES_CYPHER, {"sid": system_id})
    mapping_rows = query(MAPPINGS_CYPHER, {"sid": system_id, "methods": methods})
    if len(rule_rows or []) > MAX_RULES or len(mapping_rows or []) > MAX_MAPPINGS:
        logger.info("IDENTIFIER_FORMS system=%s read at its cap; the rest is not used", system_id)
    return IdentifierForms(rules=_rules(rule_rows), pairs=_pairs(mapping_rows))


def forms_for_system(system_id: str, *, query=None) -> IdentifierForms:
    """This system's identifier forms from the graph (cached), or
    :data:`NO_FORMS` when there is no system id or the read fails. Never raises."""
    if not isinstance(system_id, str) or not system_id:
        return NO_FORMS
    now = _monotonic()
    with _lock:
        cached = _cache.get(system_id)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        if now < _paused_until.get(system_id, 0.0):
            return NO_FORMS
    try:
        forms = _read(system_id, query or _default_query)
    except Exception as exc:  # noqa: BLE001 - fail closed: no forms, every identifier checked
        logger.warning("IDENTIFIER_FORMS system=%s graph read failed (%s); none used",
                       system_id, type(exc).__name__)
        with _lock:
            _cache.pop(system_id, None)
            _paused_until[system_id] = _monotonic() + FAILURE_PAUSE_SECONDS
        return NO_FORMS
    with _lock:
        _cache[system_id] = (_monotonic(), forms)
    return forms
