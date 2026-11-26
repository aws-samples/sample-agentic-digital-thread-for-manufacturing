# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The app's own openCypher stays inside Neptune Analytics' ORDER BY limit.

Neptune Analytics rejects a query whose ORDER BY has more than five sort keys
("ORDER BY with more than 5 sort keys is not supported in this release",
ValidationException). The unit lane runs every query against fakes, so a query
over the limit passes here and fails only on a deployed graph. That happened to
the route-seed rules query: a stable tie-break after provenance tier and
confidence took each rule collection's ORDER BY to six or seven keys,
``neptune_query`` degraded the rejection to ``[]``, and every route seed reached
the model with no join paths, value mappings or identity rules.

So this file scans every string literal in the package source for ORDER BY
clauses and bounds their key count, and pins the route seed's tie-break shape:
tier, confidence, then ONE list-valued key holding the rule's id and endpoint
ids. Neptune orders lists element by element (checked on a live graph), which
is the same order as the tuple from ``orchestrator._rule_identity_key``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from agents import orchestrator as orch

#: Neptune Analytics' limit on ORDER BY sort keys.
NEPTUNE_MAX_ORDER_BY_KEYS = 5

_PACKAGE = Path(orch.__file__).resolve().parent.parent
_ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
#: Clause keywords that end an ORDER BY list (``WITH`` only when it is not part
#: of ``STARTS WITH`` / ``ENDS WITH``).
_CLAUSE_END = frozenset({
    "WITH", "RETURN", "SKIP", "LIMIT", "UNION", "MATCH", "OPTIONAL", "WHERE",
    "UNWIND", "CALL", "CREATE", "MERGE", "SET", "DELETE", "DETACH", "REMOVE", "FOREACH"})
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def order_by_clauses(cypher: str) -> "list[list[str]]":
    """Every ORDER BY clause in ``cypher`` as its list of top-level sort keys.

    Commas inside parentheses, lists, maps, strings and CASE ... END do not
    split keys. A clause ends at the next top-level clause keyword, a ``;`` or
    the end of the text."""
    clauses = []
    for match in _ORDER_BY.finditer(cypher):
        i, depth, case_depth, quote = match.end(), 0, 0, ""
        keys, start, prev_word = [], match.end(), ""
        while i < len(cypher):
            ch = cypher[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
                i += 1
                continue
            if ch in "'\"`":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0 and case_depth == 0:
                keys.append(cypher[start:i])
                start = i + 1
            elif ch == ";" and depth == 0:
                break
            elif ch.isalpha() or ch == "_":
                word = _WORD.match(cypher, i).group(0)
                upper = word.upper()
                if depth == 0:
                    if upper == "CASE":
                        case_depth += 1
                    elif upper == "END" and case_depth:
                        case_depth -= 1
                    elif (case_depth == 0 and upper in _CLAUSE_END
                          and not (upper == "WITH" and prev_word in ("STARTS", "ENDS"))):
                        break
                prev_word = upper
                i += len(word)
                continue
            i += 1
        keys.append(cypher[start:i])
        clauses.append([" ".join(k.split()) for k in keys if k.strip()])
    return clauses


def _rendered_literals(tree: ast.AST):
    """(line, text) for every str constant and f-string in ``tree``; an
    f-string's replacement fields render as ``0`` (they never hold sort keys)
    and its literal pieces are not yielded again on their own."""
    pieces = {id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
              for v in node.values}
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = [v.value if isinstance(v, ast.Constant) else "0" for v in node.values]
            yield node.lineno, "".join(str(p) for p in parts)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in pieces:
            yield node.lineno, node.value


def _package_order_by_clauses():
    found = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        rel = path.relative_to(_PACKAGE).parts
        if rel[0] in ("tests", "build") or "node_modules" in rel:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, text in _rendered_literals(tree):
            for keys in order_by_clauses(text):
                found.append((f"{path.relative_to(_PACKAGE)}:{lineno}", keys))
    return found


# ── the counter itself ────────────────────────────────────────────────────────

def test_counter_splits_only_top_level_commas():
    cypher = (
        "MATCH (n) WITH n ORDER BY CASE WHEN n.m IN ['a', 'b'] THEN 0 ELSE 1 END, "
        "coalesce(n.c, 0) DESC, [coalesce(n.id, ''), coalesce(n.x, '')], {k: 1, j: 2}.k, "
        "n.name STARTS WITH 'x, y' WITH n RETURN n ORDER BY n.id SKIP 1 LIMIT 2")
    assert order_by_clauses(cypher) == [
        ["CASE WHEN n.m IN ['a', 'b'] THEN 0 ELSE 1 END", "coalesce(n.c, 0) DESC",
         "[coalesce(n.id, ''), coalesce(n.x, '')]", "{k: 1, j: 2}.k",
         "n.name STARTS WITH 'x, y'"],
        ["n.id"],
    ]


def test_counter_reads_the_query_neptune_rejected():
    """The six- and seven-key form Neptune refused counts as over the limit."""
    rejected = (
        "WITH s, jp ORDER BY CASE WHEN jp.method IN ['user_confirmed'] THEN 0 ELSE 2 END, "
        "coalesce(jp.confidence, 0) DESC, coalesce(jp.id, ''), coalesce(jp.from_system, ''), "
        "coalesce(jp.to_system, ''), coalesce(jp.from_field, ''), coalesce(jp.to_field, '') "
        "WITH s, collect(jp.id) AS ids RETURN s, ids")
    (keys,) = order_by_clauses(rejected)
    assert len(keys) == 7 > NEPTUNE_MAX_ORDER_BY_KEYS


# ── the package's queries ─────────────────────────────────────────────────────

def test_the_scan_finds_the_route_seed_queries():
    """Guard against a scan that silently reads nothing."""
    where = {loc.split(":")[0] for loc, _ in _package_order_by_clauses()}
    assert "agents/orchestrator.py" in where
    # shared concepts, the value-mapping pairs before they are grouped, the
    # three rule collections, and the final system order
    assert len(order_by_clauses(orch._ROUTE_SEED_CYPHER)) == 6


@pytest.mark.parametrize("where,keys", _package_order_by_clauses(), ids=lambda v: v if isinstance(v, str) else "")
def test_no_app_query_sorts_on_more_keys_than_neptune_accepts(where, keys):
    assert len(keys) <= NEPTUNE_MAX_ORDER_BY_KEYS, (
        f"{where}: ORDER BY has {len(keys)} sort keys; Neptune Analytics rejects more than "
        f"{NEPTUNE_MAX_ORDER_BY_KEYS} (combine tie-breaks into one list key): {keys}")


# ── the route seed's tie-break ────────────────────────────────────────────────

_RULE_TIE_BREAK = {
    "jp": ("id", "from_system", "to_system", "from_field", "to_field"),
    "vm": ("from_system", "to_system", "from_field", "to_field"),
    "ir": ("id", "source_system", "target_system", "source_field", "target_field"),
}

#: Value mappings are ordered AFTER they are grouped per field pair, on the
#: grouped columns (method, confidence and the four endpoints).
_GROUPED_VM = {"method": "vmethod", "confidence": "vconf", "from_system": "vfs",
               "to_system": "vts", "from_field": "vff", "to_field": "vtf"}


def _column(var: str, prop: str) -> str:
    return _GROUPED_VM[prop] if var == "vm" else f"{var}.{prop}"


@pytest.mark.parametrize("var", sorted(_RULE_TIE_BREAK))
def test_each_rule_collection_keeps_a_total_tie_break_in_one_list_key(var):
    """Tier, confidence, then the rule's id and endpoint ids as ONE list key,
    in ``_rule_identity_key``'s field order: equal-ranked rules never fall back
    to database order at the per-collection cap, within five sort keys."""
    clauses = [keys for keys in order_by_clauses(orch._ROUTE_SEED_CYPHER)
               if keys and keys[0].startswith(f"CASE WHEN {_column(var, 'method')}")]
    assert len(clauses) == 1, clauses
    keys = clauses[0]
    expected = "[" + ", ".join(f"coalesce({_column(var, p)}, '')" for p in _RULE_TIE_BREAK[var]) + "]"
    assert keys[1:] == [f"coalesce({_column(var, 'confidence')}, 0) DESC", expected]


def test_the_list_key_follows_the_python_identity_key_order():
    """The Cypher list holds the same fields, in the same order, as the Python
    tie-break, so both layers agree on which equal-ranked rule comes first."""
    jp = {"id": "jp-1", "from_system": "a", "to_system": "b", "from_field": "f", "to_field": "g"}
    ir = {"id": "ir-1", "source_system": "a", "target_system": "b",
          "source_field": "f", "target_field": "g"}
    vm = {"from_system": "a", "to_system": "b", "from_field": "f", "to_field": "g"}
    assert orch._rule_identity_key(jp) == tuple(jp[p] for p in _RULE_TIE_BREAK["jp"])
    assert orch._rule_identity_key(ir) == tuple(ir[p] for p in _RULE_TIE_BREAK["ir"])
    assert orch._rule_identity_key(vm) == ("",) + tuple(vm[p] for p in _RULE_TIE_BREAK["vm"])
