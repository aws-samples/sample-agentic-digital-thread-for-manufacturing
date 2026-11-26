# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Withhold switched-off JoinPaths and IdentityRules from an ``explore_graph``
result, by exact identity.

The contract a free-form reader follows is the data's own status: an
IdentityRule or JoinPath whose status is ``deactivated`` or ``retired`` is
soft-disabled, and the metamodel and the tool description give the WHERE
clause that leaves it out (:func:`rule_status.active_where`); deactivating an
IdentityRule writes that status on its JoinPaths (:mod:`joinpath_rule_link`).
On top of that contract, before a result goes back to the model:

* a row that carries a switched-off JoinPath or IdentityRule (its node, a map
  with its ``id``, or the id string, at any depth inside maps) is withheld;
* an item inside a list that carries one is dropped and the row kept;
* the envelope names the switched-off JoinPaths and IdentityRules the result
  carried and says what was left out.

A JoinPath is switched off by its own status or by its IdentityRule's, so a
JoinPath written before its rule's deactivation marked it is withheld too.
Nothing else is compared: field and endpoint names are not identities (the
same names recur across systems), and counts, aggregates and other
projections come back as the query computed them.

The lookups are scoped to the strings the result holds; a result with more
distinct strings than :data:`SCOPE_VALUE_CAP` falls back to the bounded
global reads. A value nested deeper than :data:`WALK_DEPTH` is never checked,
so it is dropped (fail closed). A failed lookup keeps the rows (the primary
result still stands) and logs it.
"""
from __future__ import annotations

import logging
import re

from . import joinpath_rule_link as _jp_rule
from .rule_status import rule_inactive

logger = logging.getLogger(__name__)

NOTE = (
    "The JoinPaths in inactive_join_paths and the IdentityRules in "
    "inactive_identity_rules are switched off: deactivated or retired (a JoinPath "
    "also when the identity rule it was made from is). They are not joins or "
    "identity translations. Do not use them to join the two systems, to translate "
    "identifiers between those fields, or to say how the systems connect. Result "
    "rows that returned one of them, and items inside a row's lists that held one, "
    "were left out (withheld_rows, dropped_items)."
)
#: Most switched-off JoinPaths, and most IdentityRules, one envelope names.
LIST_CAP = 20
#: Most distinct result strings a scoped lookup sends.
SCOPE_VALUE_CAP = 5000
#: Longer strings are not ids.
_SCOPE_STRING_MAX = 256
#: Values nested deeper than this are dropped (fail closed).
WALK_DEPTH = 32
DEEP_NOTE = (
    f"Values nested more than {WALK_DEPTH} levels deep are not returned: rows and list "
    "items holding one were left out (withheld_rows, dropped_items)."
)

_RULE_FIELDS = ("RETURN ir.id AS id, ir.concept AS concept, ir.source_system AS source_system, "
                "ir.source_field AS source_field, ir.target_system AS target_system, "
                "ir.target_field AS target_field, ir.status AS status ORDER BY ir.id")

#: The switched-off IdentityRules among the result's strings, by exact id.
INACTIVE_RULES_SCOPED_CYPHER = (
    f"MATCH (ir:IdentityRule) WHERE ir.id IN $values AND {rule_inactive('ir')} " + _RULE_FIELDS)

#: Every switched-off IdentityRule, bounded (the many-strings fallback).
INACTIVE_RULES_CYPHER = (
    f"MATCH (ir:IdentityRule) WHERE {rule_inactive('ir')} " + _RULE_FIELDS + " LIMIT 1000")

#: Every read this module makes (tests that count the model's own queries
#: leave these out).
OWN_READS = (_jp_rule.INACTIVE_JOINPATHS_SCOPED_CYPHER, _jp_rule.INACTIVE_JOINPATHS_CYPHER,
             INACTIVE_RULES_SCOPED_CYPHER, INACTIVE_RULES_CYPHER)

_DROP = object()
#: A node pattern with no label: ``(n)``, ``(n {..})`` or ``()``, not a call.
_UNLABELLED_NODE = re.compile(r"(?<![\w.])\(\s*(?:[A-Za-z_]\w*)?\s*[{)]")
_LABELS = (("JoinPath", "joinpath", "jp-"), ("IdentityRule", "identityrule", "ir-"))


def _properties(value: dict) -> "dict | None":
    """A graph element's properties, else None."""
    if "~properties" in value:
        return value.get("~properties") or {}
    return None


def _strings(value, out: set, depth: int = 0) -> "tuple[bool, bool]":
    """Add the short strings ``value`` holds to ``out``. ``(within_cap,
    too_deep)``: whether ``out`` stayed within :data:`SCOPE_VALUE_CAP`, and
    whether some value lies deeper than :data:`WALK_DEPTH`."""
    if depth > WALK_DEPTH:
        return True, True
    if isinstance(value, str):
        if len(value) <= _SCOPE_STRING_MAX:
            out.add(value)
        return len(out) <= SCOPE_VALUE_CAP, False
    if isinstance(value, dict):
        props = _properties(value)
        items = (props if props is not None else value).values()
    elif isinstance(value, list):
        items = value
    else:
        return True, False
    deep = False
    for item in items:
        within, too_deep = _strings(item, out, depth + 1)
        deep = deep or too_deep
        if not within:
            return False, deep
    return True, deep


class _Inactive:
    """The switched-off JoinPaths and IdentityRules a result is checked against."""

    def __init__(self, joins: list, rules: list):
        self.joins = {str(r["id"]): r for r in joins if isinstance(r, dict) and r.get("id")}
        self.rules = {str(r["id"]): r for r in rules if isinstance(r, dict) and r.get("id")}

    def __bool__(self) -> bool:
        return bool(self.joins or self.rules)

    def names(self, value: str) -> bool:
        return value in self.joins or value in self.rules

    def node_hit(self, value: dict, props: dict) -> "str | None":
        node_id = str(props.get("id") or "")
        labels = value.get("~labels") or []
        if ("JoinPath" in labels and node_id in self.joins) or (
                "IdentityRule" in labels and node_id in self.rules):
            return node_id
        return None


def _scan_list(value: list, ctx: _Inactive, depth: int):
    kept, named, dropped = [], set(), 0
    for item in value:
        k, n, d = _scan(item, ctx, depth + 1)
        named |= n
        dropped += d + (k is _DROP)
        if k is not _DROP:
            kept.append(k)
    return kept, named, dropped


def _scan_map(value: dict, ctx: _Inactive, depth: int):
    props = _properties(value)
    if props is not None:
        hit = ctx.node_hit(value, props)
        return (_DROP, {hit}, 0) if hit else (value, set(), 0)
    if isinstance(value.get("id"), str) and ctx.names(value["id"]):
        return _DROP, {value["id"]}, 0
    kept, named, dropped = {}, set(), 0
    for key, item in value.items():
        k, n, d = _scan(item, ctx, depth + 1)
        named |= n
        dropped += d
        if k is _DROP:
            return _DROP, named, dropped
        kept[key] = k
    return kept, named, dropped


def _scan(value, ctx: _Inactive, depth: int):
    """``(kept, named, dropped)``: ``kept`` is ``_DROP`` when the value itself
    carries a switched-off JoinPath or IdentityRule (or lies deeper than
    :data:`WALK_DEPTH`), else the value with the list items that do removed;
    ``named`` the ids it carried; ``dropped`` list items removed."""
    if depth > WALK_DEPTH:
        return _DROP, set(), 0
    if isinstance(value, str):
        return (_DROP, {value}, 0) if ctx.names(value) else (value, set(), 0)
    if isinstance(value, list):
        return _scan_list(value, ctx, depth)
    if isinstance(value, dict):
        return _scan_map(value, ctx, depth)
    return value, set(), 0


def _may_carry(cypher: str, strings: set) -> "dict[str, bool]":
    """Per label, whether the rows can carry one at all (that lookup is
    skipped otherwise): the query names the label, matches a node without a
    label, or a row holds a string with the label's id prefix."""
    text = cypher.lower()
    unlabelled = bool(_UNLABELLED_NODE.search(cypher))
    return {label: (name in text or unlabelled or any(s.startswith(prefix) for s in strings))
            for label, name, prefix in _LABELS}


def _lookup(query, wanted: dict, values: set, scoped: bool) -> "tuple[list, list]":
    """The switched-off JoinPaths and IdentityRules the result can name."""
    params = {"values": sorted(values)}
    joins = rules = []
    if wanted["JoinPath"]:
        joins = (query(_jp_rule.INACTIVE_JOINPATHS_SCOPED_CYPHER, params) if scoped
                 else query(_jp_rule.INACTIVE_JOINPATHS_CYPHER)) or []
    if wanted["IdentityRule"]:
        rules = (query(INACTIVE_RULES_SCOPED_CYPHER, params) if scoped
                 else query(INACTIVE_RULES_CYPHER)) or []
    return joins, rules


def _endpoint(system, table, field) -> str:
    parts = [str(p).strip(".") for p in (system, table, field) if p]
    return ".".join(p for p in parts if p)


def _join_entry(row: dict) -> dict:
    return {"id": row.get("id"),
            "from": _endpoint(row.get("from_system"), row.get("from_table"), row.get("from_field")),
            "to": _endpoint(row.get("to_system"), row.get("to_table"), row.get("to_field")),
            "why": ("its identity rule was deactivated or retired"
                    if row.get("reason") == "rule_inactive"
                    else "the JoinPath was deactivated or retired")}


def _rule_entry(row: dict) -> dict:
    return {"id": row.get("id"), "concept": row.get("concept"),
            "from": _endpoint(row.get("source_system"), None, row.get("source_field")),
            "to": _endpoint(row.get("target_system"), None, row.get("target_field")),
            "status": row.get("status")}


def withhold(cypher: str, results: list, query) -> "tuple[list, dict]":
    """``(rows, extra)``: ``results`` without what carries a switched-off
    JoinPath or IdentityRule, and the envelope fields naming them (``{}``
    when nothing applies). ``query`` is the checked graph seam
    ``query(cypher, params)``."""
    values: set = set()
    scoped, deep = _strings(results, values)
    # Past the cap the walk stopped early, so depth is unknown: scan anyway.
    deep = deep or not scoped
    wanted = _may_carry(cypher, values)
    ctx = _Inactive([], [])
    if values and any(wanted.values()):
        try:
            ctx = _Inactive(*_lookup(query, wanted, values, scoped))
        except Exception as exc:  # noqa: BLE001 - the primary result still stands
            logger.warning("switched-off rule check failed; rows kept: %s", type(exc).__name__)
            if not deep:
                return results, {}
    if not ctx and not deep:
        return results, {}
    kept, named, withheld, dropped = [], set(), 0, 0
    for row in results:
        k, n, d = _scan(row, ctx, 0) if isinstance(row, dict) else (row, set(), 0)
        named |= n
        dropped += d
        if k is _DROP:
            withheld += 1
        else:
            kept.append(k)
    joins = sorted(named & set(ctx.joins))
    rules = sorted(named & set(ctx.rules))
    extra: dict = {}
    if joins:
        extra["inactive_join_paths"] = [_join_entry(ctx.joins[i]) for i in joins[:LIST_CAP]]
    if rules:
        extra["inactive_identity_rules"] = [_rule_entry(ctx.rules[i]) for i in rules[:LIST_CAP]]
    if extra:
        extra["inactive_join_paths_note"] = NOTE
    elif withheld or dropped:
        extra["withheld_note"] = DEEP_NOTE
    if withheld:
        extra["withheld_rows"] = withheld
    if dropped:
        extra["dropped_items"] = dropped
    return kept, extra
