# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every person the seed data names is an approved fictitious name.

The frozen seed (``lambda-adt-seed/*.sql``) names the people who reported, wrote up, were assigned, bought, ran and
inspected things. This source is shared with customers, so each such value is
one of:

* an approved fictitious full name in ``_APPROVED_NAMES``, such as ``Diego Ramirez``;
* the short form of one, its first initial and surname (``D. Ramirez``);
* a value in ``_NOT_A_PERSON`` (a crew code, or an MES operator badge the MES
  bridge relayed).

``_APPROVED_NAMES`` is fixed here. It is never read from the files being
checked, so a new name in the seed or in the ERP generator's contact lists
(``erp_simulation/seed_data.py``, which must stay a subset) is approved only
by adding it to that list. Any other value fails by its shape: an
``Initial. Surname`` value whose surname and initial match no approved name,
or a full name that is not approved.

Which columns hold a person is not guessed from their names. ``_SEED_COLUMNS``
classifies every textual seed column (and every view column computed from a
string literal) as a person, a code, the name of a thing, or other text. A new
textual column fails until it is classified. Person values
get the approved-name check, code values must be space-free.

The SQL reader is closed: it reads the statement forms the seed uses and fails
on any other. That covers comments and quoting, every column declaration,
ALTER TABLE actions, views, VALUES rows, column defaults, and the assignments
outside VALUES (``UPDATE ... SET``, ``INSERT ... ON CONFLICT DO UPDATE SET`` and
``INSERT ... SELECT`` lists). The seed re-run tests prove names converge on
existing accounts; this file proves which names they converge to.

Offline: the checks parse the SQL and Python text. Nothing is executed.
"""
from __future__ import annotations

import ast
import functools
import importlib.util
import re
from pathlib import Path
from typing import NamedTuple

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SEED_FILES = sorted((_REPO / "lambda-adt-seed").glob("*.sql"))
_SITE_CMMS = _REPO / "lambda-adt-seed" / "011_cmms_indianapolis.sql"
_GENERATOR = _REPO / "erp_simulation" / "seed_data.py"

#: Files that carry the Indianapolis story rows' short-form names: the seed,
#: and the tests that build rows like them.
_SHORT_FORM_FILES = (
    _SITE_CMMS,
    _REPO / "agentic_digital_thread" / "tests" / "unit" / "test_condition_order_lifecycle.py",
    _REPO / "agentic_digital_thread" / "tests" / "integration" / "test_condition_orders_pg.py",
    _REPO / "erp_simulation" / "tests" / "test_story_history.py",
)

#: Approved fictitious names the seed data and the ERP generator may use.
_APPROVED_NAMES = frozenset({
    # The ERP generator's customer and supplier contact names.
    "Akua Mansa", "Ana Carolina Silva", "Carlos Salazar", "Diego Ramirez", "Efua Owusu",
    "Jane Doe", "John Stiles", "Li Juan", "Martha Rivera", "Mary Major",
    "Mateo Jackson", "Saanvi Sarkar", "Shirley Rodriguez", "Wang Xiulan",
    # Used by the seed only.
    "Kwaku Mensah", "Pat Candella", "Richard Roe", "Terry Whitlock",
})

#: Values in a person column that name no person.
_NOT_A_PERSON = frozenset({
    # An MES operator badge on a work order the MES raised.
    "OP-0101 via MES", "OP-0102 via MES",
    # Maintenance crew codes.
    "maint_team_alpha", "maint_team_beta", "maint_team_gamma",
    "crew_red", "crew_blue", "crew_green",
})

# --------------------------------------------------------------------------
# Classification of every textual column and key
# --------------------------------------------------------------------------
#: A person: every value is an approved name, its short form, or in ``_NOT_A_PERSON``.
_PERSON = "person"
#: An identifier, status, type or team code: every value is space-free.
_CODE_KIND = "code"
#: The name of a thing (an asset, part, company or place): not checked.
_THING = "thing"
#: Other text (descriptions, notes, units, enumerations with spaces, dates as text): not checked.
_TEXT = "text"
_KINDS = frozenset({_PERSON, _CODE_KIND, _THING, _TEXT})

#: Seed file -> table (or view) -> textual column -> kind. Every CHAR, VARCHAR,
#: TEXT (or similar) column a seed file creates is listed, and every view
#: column computed from a string literal.
_SEED_COLUMNS: dict[str, dict[str, dict[str, str]]] = {
    "003_mes_line3.sql": {
        "production_runs": {
            "batch_number": "code", "work_order_ref": "code", "line_id": "code", "equipment_id": "code",
            "product_code": "code", "status": "code", "operator_badge": "code", "shift": "code",
            "notes": "text",
        },
        "quality_checks": {
            "batch_number": "code", "check_type": "code", "parameter": "code", "result": "code",
            "operator_badge": "code", "measurement_equipment": "code",
        },
        "equipment": {
            "equipment_id": "code", "equipment_name": "thing", "line_id": "code",
            "equipment_type": "code", "status": "code",
        },
    },
    "004_cmms_maintenance.sql": {
        "assets": {
            "asset_tag": "code", "asset_name": "thing", "asset_type": "code", "facility": "thing",
            "area": "code", "manufacturer": "thing", "model_number": "code", "serial_number": "code",
            "operational_status": "code", "criticality": "code",
        },
        "work_orders_raw": {
            "wo_number": "code", "wo_type": "code", "priority": "code", "description": "text",
            "assigned_to": "person", "status_hint": "code", "failure_code": "code",
        },
        "maintenance_schedules_raw": {
            "maintenance_type": "code", "assigned_team": "code",
        },
        # A view column computed from string literals (the CASE status).
        "maintenance_schedules": {
            "status": "text",
        },
        "spare_parts": {
            "part_number": "code", "part_name": "thing", "compatible_asset_type": "code", "supplier": "thing",
        },
    },
    "005_plm_engineering.sql": {
        "products": {
            "item_id": "code", "item_name": "thing", "description": "text", "item_type": "text",
            "revision": "code", "lifecycle_state": "text", "owning_group": "code",
        },
        "specifications": {
            "item_id": "code", "parameter": "code", "unit": "text", "spec_class": "code",
            "inspection_method": "thing", "revision": "code",
        },
        "bom_items": {
            "parent_item": "code", "child_item": "code", "unit": "code", "material_spec": "text",
            "approved_vendors": "thing",
        },
    },
    "006_erp_detroit.sql": {
        "suppliers": {
            "vendor_id": "code", "company_name": "thing", "status": "code", "payment_terms": "code",
            "region": "thing",
        },
        "materials": {
            "item_code": "code", "description": "text", "category": "text", "subcategory": "text",
            "uom": "code", "vendor_id": "code", "lot_id": "code", "qa_status": "code",
            "material_grade": "thing",
        },
        "purchase_orders": {
            "po_number": "code", "po_type": "code", "item_code": "code", "plant_code": "code", "status": "code",
            "buyer_name": "person", "ship_to_location": "thing",
        },
    },
    "007_mes_detroit.sql": {
        "production_runs": {
            "run_number": "code", "work_order_id": "code", "cell_id": "code", "product_number": "code",
            "run_status": "code", "operator_name": "person",
        },
        "quality_records": {
            "run_number": "code", "inspection_type": "code", "characteristic": "text", "disposition": "code",
            "inspector_name": "person",
        },
    },
    "008_cmms_detroit.sql": {
        "equipment": {
            "equipment_id": "code", "description": "text", "equipment_category": "code",
            "functional_location": "code", "plant": "code", "status": "code", "manufacturer": "thing",
            "model_number": "code",
        },
        "maintenance_notifications": {
            "notification_id": "code", "equipment_id": "code", "notification_type": "code",
            "description": "text", "reported_by": "person", "status": "code",
        },
        "pm_orders": {
            "order_id": "code", "notification_id": "code", "equipment_id": "code", "order_type": "code",
            "status": "code", "damage_code": "code", "cause_code": "code", "activity_type": "code",
        },
        # Written only by generated INSERT ... SELECT rows, which the reader does
        # not take apart, so held as text. A person or code column here would fail.
        "sensor_readings": {
            "equipment_id": "text", "measurement_point": "text", "reading_type": "text", "unit": "text",
            "status": "text",
        },
    },
    "009_historian.sql": {
        "equipment_registry": {
            "equipment_id": "code", "equipment_name": "thing", "plant": "thing", "area": "code", "line": "code",
            "equipment_type": "code", "status": "code",
        },
        # Written only by generated INSERT ... SELECT rows (as above).
        "sensor_readings": {
            "equipment_id": "text", "plant": "text", "measurement_type": "text", "unit": "text",
            "quality": "text",
        },
        "aggregated_metrics": {
            "equipment_id": "text", "metric": "text",
        },
    },
    "010_cmms2_synonyms.sql": {
        "equipment": {
            "tag_code": "code", "equip_desc": "thing", "machine_class": "code", "plant_site": "thing",
            "cell": "code", "vendor": "thing", "run_state": "code", "crit_level": "code",
        },
        "repair_tickets": {
            "ticket_no": "code", "ticket_class": "code", "urgency": "code", "summary": "text", "tech": "person",
            "state": "code", "fault_code": "code",
        },
        "pm_plan": {
            "plan_id": "code", "pm_kind": "code", "crew": "code",
        },
        "stockroom": {
            "sku": "code", "sku_desc": "thing", "fits_class": "code", "vendor": "thing",
        },
    },
    "011_cmms_indianapolis.sql": {
        "assets": {
            "asset_num": "code", "description": "text", "site_id": "code", "location": "code",
            "asset_type": "code", "status": "code", "manufacturer": "thing", "model_number": "code",
            "serial_number": "code",
        },
        "pm_schedules": {
            "pm_num": "code", "asset_num": "code", "description": "text", "frequency_units": "code",
            "assigned_crew": "code", "pm_status": "code",
        },
        "work_orders": {
            "wonum": "code", "asset_num": "code", "work_type": "code", "description": "text", "status": "code",
            "pm_num": "code", "reported_by": "person", "failure_code": "code", "crew": "code",
        },
        "work_order_log": {
            "wonum": "code", "author": "person", "log_type": "code", "entry": "text",
        },
    },
    "012_supplier_master.sql": {
        "vendors": {
            "vendor_number": "code", "legal_name": "thing", "vendor_group": "code",
            "payment_terms": "code", "tax_id": "code", "status": "code",
        },
        "vendor_spend_summary": {
            "vendor_number": "code",
        },
    },
}


#: Only a hint in the "classify it" message; classification itself is the map above.
_PERSONISH = re.compile(
    r"_by$|_name$|author|operator|inspector|buyer|tech|assign|crew|owner|user|contact|engineer|planner"
    r"|approver|requester|supervisor|employee|staff|worker|reviewer|reporter|manager|recipient|submitter"
    r"|creator|modifier|maintainer|participant|attendee|(?:^|_)lead(?:er)?$",
    re.IGNORECASE,
)
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHORT_FORM = re.compile(r"([A-Z])\. ([A-Z][a-z]+(?:[-'][A-Z][a-z]+)?)")
_QUOTED_SHORT_FORM = re.compile(r"""(?<![\w.])["']([A-Z]\. [A-Z][a-z]+(?:[-'][A-Z][a-z]+)?)["']""")


def _personish(name: str) -> bool:
    """Whether a column or key name looks like it holds a person (camelCase read as snake_case)."""
    return bool(_PERSONISH.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)))


def _classify_hint(name: str) -> str:
    return " (its name suggests a person)" if _personish(name) else ""


# --------------------------------------------------------------------------
# Approved names
# --------------------------------------------------------------------------
# Type-parameter nodes (Python 3.12+) bind a name too.
_TYPE_PARAMS = tuple(getattr(ast, n) for n in ("TypeVar", "ParamSpec", "TypeVarTuple") if hasattr(ast, n))
# Builtins that run code the generator check cannot read.
_DYNAMIC_CODE = {"exec", "eval", "compile", "__import__"}


def _contact_name_bound(target: ast.AST) -> bool:
    """Whether an assignment target binds the name ``contact_name`` (also inside a tuple, list or star)."""
    if isinstance(target, ast.Name):
        return target.id == "contact_name"
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_contact_name_bound(e) for e in target.elts)
    if isinstance(target, ast.Starred):
        return _contact_name_bound(target.value)
    return False


def _contact_name_field(target: ast.AST) -> bool:
    """Whether a target stores a record's contact name (``rec['contact_name']``, ``obj.contact_name``)."""
    if isinstance(target, ast.Attribute):
        return target.attr == "contact_name"
    if isinstance(target, ast.Subscript):
        return isinstance(target.slice, ast.Constant) and target.slice.value == "contact_name"
    return False


def _is_contact_name(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Name) and node.id == "contact_name"


def _choice_list(value: ast.AST | None, line: int) -> list[str]:
    """The names in ``random.choice([<string literals>])``; anything else fails."""
    where = f"{_GENERATOR.name} line {line}: contact_name"
    assert isinstance(value, ast.Call), f"{where} is not drawn with random.choice([...])"
    func = value.func
    assert (isinstance(func, ast.Attribute) and func.attr == "choice"
            and isinstance(func.value, ast.Name) and func.value.id == "random"), (
        f"{where} is drawn with something other than random.choice")
    assert len(value.args) == 1 and not value.keywords, f"{where}: random.choice takes exactly one list"
    (arg,) = value.args
    assert isinstance(arg, ast.List) and arg.elts, f"{where}: random.choice is given something other than a list"
    assert all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in arg.elts), (
        f"{where}: the list holds a value that is not a string literal")
    return [e.value for e in arg.elts]


def _generator_contact_lists(source: str) -> list[list[str]]:
    """Each list the ERP generator draws ``contact_name`` from.

    Every assignment to ``contact_name`` (plain, annotated or walrus) must be
    exactly ``random.choice([<string literals>])``. Any other binding of the
    name (an augmented assignment, unpacking, a loop, ``with`` or import
    target, a parameter, a match pattern capture, a ``def``, ``class`` or
    ``type`` statement, a type parameter) fails, and so does a star import or
    dynamic code (``exec``, ``eval``, ``compile``), which could bind it
    unseen. A record's ``contact_name`` field (a dict key, a subscript or
    attribute store, a keyword argument) may only be given the variable
    ``contact_name``. The string ``'contact_name'`` may appear only as such a
    dict key or subscript, so ``setattr`` and the like fail too.
    """
    lists = []
    tree = ast.parse(source)
    drawn = set()  # the contact_name targets whose value passed the random.choice check
    field_names = set()  # the 'contact_name' constants read as a dict key or subscript
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            field_names |= {id(k) for k in node.keys if isinstance(k, ast.Constant) and k.value == "contact_name"}
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            field_names.add(id(node.slice))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None) or getattr(getattr(node, "target", None), "lineno", 0)
        where = f"{_GENERATOR.name} line {line}"
        if isinstance(node, ast.Constant) and node.value == "contact_name":
            assert id(node) in field_names, f"{where}: 'contact_name' used other than as a record field"
        if isinstance(node, ast.Assign):
            if any(_contact_name_bound(t) for t in node.targets):
                assert len(node.targets) == 1 and _is_contact_name(node.targets[0]), (
                    f"{where}: contact_name assigned together with other names")
                lists.append(_choice_list(node.value, line))
                drawn.add(id(node.targets[0]))
            for target in node.targets:
                if _contact_name_field(target):
                    assert _is_contact_name(node.value), f"{where}: a record's contact_name set to something else"
        elif isinstance(node, ast.AnnAssign):
            if _is_contact_name(node.target):
                lists.append(_choice_list(node.value, line))
                drawn.add(id(node.target))
            elif _contact_name_field(node.target):
                assert _is_contact_name(node.value), f"{where}: a record's contact_name set to something else"
        elif isinstance(node, ast.NamedExpr) and _is_contact_name(node.target):
            lists.append(_choice_list(node.value, line))
            drawn.add(id(node.target))
        elif isinstance(node, ast.AugAssign):
            assert not (_contact_name_bound(node.target) or _contact_name_field(node.target)), (
                f"{where}: contact_name changed by an augmented assignment")
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            assert not _contact_name_bound(node.target), f"{where}: contact_name bound by a loop"
        elif isinstance(node, ast.withitem):
            assert not (node.optional_vars and _contact_name_bound(node.optional_vars)), (
                f"{where}: contact_name bound by a with statement")
        elif isinstance(node, ast.arg):
            assert node.arg != "contact_name", f"{where}: contact_name is a parameter"
        elif isinstance(node, ast.ExceptHandler):
            assert node.name != "contact_name", f"{where}: contact_name bound by an except clause"
        elif isinstance(node, ast.alias):
            # The name an import binds: its alias, or the top-level package of a dotted import.
            bound = node.asname or node.name.split(".", 1)[0]
            assert bound != "contact_name", f"{where}: contact_name bound by an import"
            assert node.name != "*", f"{where}: a star import, which could bind contact_name unseen"
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            assert "contact_name" not in node.names, f"{where}: contact_name declared global or nonlocal"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assert node.name != "contact_name", f"{where}: contact_name bound by a def or class statement"
        elif isinstance(node, _TYPE_PARAMS):
            assert node.name != "contact_name", f"{where}: contact_name bound as a type parameter"
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            assert node.name != "contact_name", f"{where}: contact_name bound by a match pattern"
        elif isinstance(node, ast.MatchMapping):
            assert node.rest != "contact_name", f"{where}: contact_name bound by a match pattern"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in _DYNAMIC_CODE, f"{where}: {node.func.id}() runs code this check cannot read"
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "contact_name":
                    assert _is_contact_name(value), f"{where}: a record's contact_name set to something else"
        elif isinstance(node, ast.keyword) and node.arg == "contact_name":
            assert _is_contact_name(node.value), f"{where}: a record's contact_name set to something else"
    # Any other statement that stores to the name (a type alias, for one) is a binding the checks
    # above did not approve.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "contact_name" and isinstance(node.ctx, ast.Store):
            assert id(node) in drawn, (
                f"{_GENERATOR.name} line {node.lineno}: contact_name bound other than by random.choice")
    return lists


def _unapproved(lists: list[list[str]]) -> list[str]:
    return sorted({name for names in lists for name in names} - _APPROVED_NAMES)


def _problem(value: str | None) -> str | None:
    """Why ``value`` may not stand in a person field, or ``None`` when it may."""
    if value is None or value in _NOT_A_PERSON or value in _APPROVED_NAMES:
        return None
    short = _SHORT_FORM.fullmatch(value)
    if short:
        initial, surname = short.groups()
        if any(n.split()[0][0] == initial and n.split()[-1] == surname for n in _APPROVED_NAMES):
            return None
        return "short form of no approved name"
    return "not an approved name or a listed non-person value"


def _kind_problem(kind: str, value: str | None) -> str | None:
    """Why ``value`` may not be held in a column or key of ``kind``, or ``None``."""
    if kind == _PERSON:
        return _problem(value)
    if kind == _CODE_KIND and value and not _CODE.fullmatch(value):
        return "not a code"
    return None


# --------------------------------------------------------------------------
# Seed SQL lexer and statements
# --------------------------------------------------------------------------
def _line(text: str, i: int) -> int:
    return text.count("\n", 0, i) + 1


def _sql_code(sql: str) -> str:
    """The seed SQL as code: ``--`` and ``/* */`` comments removed, quoted
    literals kept, and the ``$$`` markers of a ``DO $$ ... $$`` block blanked
    so its body reads as code. Quoting the reader does not model (a prefixed
    literal such as ``E'...'``, any other dollar quote, a quoted identifier, a
    backslash) fails, so it cannot hide code or a value from the checks."""
    out: list[str] = []
    i, n, in_do = 0, len(sql), False
    while i < n:
        c = sql[i]
        if c == "'":
            assert not (i and (sql[i - 1].isalnum() or sql[i - 1] in "_&")), (
                f"line {_line(sql, i)}: a prefixed string literal; write a plain '...' literal")
            j = i + 1
            while True:
                k = sql.find("'", j)
                assert k >= 0, f"line {_line(sql, i)}: unterminated string literal"
                if sql.startswith("''", k):
                    j = k + 2
                    continue
                break
            out.append(sql[i:k + 1])
            i = k + 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while depth:
                opening, closing = sql.find("/*", j), sql.find("*/", j)
                assert closing >= 0, f"line {_line(sql, i)}: unterminated block comment"
                if 0 <= opening < closing:
                    depth, j = depth + 1, opening + 2
                else:
                    depth, j = depth - 1, closing + 2
            out.append(" ")
            i = j
        elif c == "$":
            assert sql.startswith("$$", i) and (in_do or re.search(r"(?i)\bDO\s*$", "".join(out))), (
                f"line {_line(sql, i)}: dollar quoting other than a DO $$ ... $$ block")
            in_do = not in_do
            out.append("  ")
            i += 2
        else:
            assert c not in "\"\\", f"line {_line(sql, i)}: {c!r} outside a string literal"
            out.append(c)
            i += 1
    assert not in_do, "unterminated DO $$ block"
    return "".join(out)


def _mask_literals(text: str) -> str:
    """``text`` with each quoted literal's contents blanked, so keyword and
    parenthesis searches skip them. Positions are unchanged."""
    return re.sub(r"'(?:[^']|'')*'", lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", text)


def _statements(code: str) -> list[str]:
    """``code`` split on the semicolons outside quoted literals (a DO block's
    body splits into its own statements)."""
    parts, start, quoted = [], 0, False
    for i, c in enumerate(code):
        if c == "'":
            quoted = not quoted  # '' toggles twice, so an escaped quote is neutral
        elif c == ";" and not quoted:
            parts.append(code[start:i])
            start = i + 1
    parts.append(code[start:])
    return [p for p in parts if p.strip()]


def _closing(masked: str, i: int) -> int:
    """The index of the ``)`` that closes the ``(`` at ``masked[i]``."""
    depth = 0
    for j in range(i, len(masked)):
        if masked[j] == "(":
            depth += 1
        elif masked[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    raise AssertionError(f"unbalanced parentheses: {' '.join(masked[i:i + 60].split())!r}")


def _clause_end(masked: str, start: int, stops: str) -> int:
    """Where a clause starting at ``start`` ends: the first top-level stop
    word, the ``)`` that closes the enclosing parentheses, or the end."""
    stop = re.compile(rf"(?:{stops})\b", re.I)
    depth = 0
    for i in range(start, len(masked)):
        c = masked[i]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and not (masked[i - 1].isalnum() or masked[i - 1] == "_") and stop.match(masked, i):
            return i
    return len(masked)


def _spans(masked: str, start: int, end: int) -> list[tuple[int, int]]:
    """``masked[start:end]`` split on its top-level commas, as index spans."""
    spans, depth, begin = [], 0, start
    for i in range(start, end):
        c = masked[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            spans.append((begin, i))
            begin = i + 1
    spans.append((begin, end))
    return [(a, b) for a, b in spans if masked[a:b].strip()]


def _tuples(code: str, i: int) -> tuple[list[list[str]], int]:
    """The ``(...)`` row tuples from ``code[i:]`` as raw field texts, and where they end."""
    rows: list[list[str]] = []
    n = len(code)
    while True:
        while i < n and (code[i].isspace() or code[i] == ","):
            i += 1
        if i >= n or code[i] != "(":
            return rows, i
        depth, quoted, field, fields = 0, False, [], []
        while i < n:
            c = code[i]
            i += 1
            if quoted:
                field.append(c)
                if c == "'":
                    if i < n and code[i] == "'":
                        field.append("'")
                        i += 1
                    else:
                        quoted = False
            elif c == "'":
                quoted = True
                field.append(c)
            elif c == "(":
                depth += 1
                if depth > 1:
                    field.append(c)
            elif c == ")":
                depth -= 1
                if depth == 0:
                    fields.append("".join(field).strip())
                    break
                field.append(c)
            elif c == "," and depth == 1:
                fields.append("".join(field).strip())
                field = []
            else:
                field.append(c)
        rows.append(fields)


_NOT_LITERAL = object()


def _literal(field: str):
    """A quoted literal's text (casts allowed), ``None`` for NULL, else ``_NOT_LITERAL``."""
    if field.strip().upper() == "NULL":
        return None
    m = re.fullmatch(r"'((?:[^']|'')*)'(?:::\w+(?:\(\d+(?:,\s*\d+)?\))?)?", field.strip(), re.S)
    return m.group(1).replace("''", "'") if m else _NOT_LITERAL


def _columns(text: str) -> list[str]:
    return [c.strip().lower() for c in text.split(",")]


def _flat(text: str) -> str:
    return " ".join(text.split())


#: Statement openings the seed uses. A DO block's body splits into statements
#: that open with its control words.
_STATEMENT_OPENING = re.compile(
    r"\s*(?:CREATE\s+SCHEMA|CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS|CREATE\s+(?:OR\s+REPLACE\s+)?VIEW"
    r"|CREATE\s+(?:UNIQUE\s+)?INDEX|DROP\s+VIEW|DROP\s+INDEX|SET|INSERT\s+INTO|UPDATE|WITH|ALTER\s+TABLE"
    r"|DELETE\s+FROM|DO|BEGIN|IF|ELSIF|ELSE|END)\b",
    re.I,
)
#: Words that run, load or store something the reader does not follow.
_UNREAD_WORDS = re.compile(
    r"\b(?:COPY|MERGE|EXECUTE|PERFORM|CALL|PREPARE|FUNCTION|PROCEDURE|TRIGGER|RULE|NOTIFY|LISTEN"
    r"|DBLINK\w*|LO_IMPORT|PG_READ_FILE)\b",
    re.I,
)


def _census_problems(code: str) -> list[str]:
    """Statements in a form the reader does not know."""
    problems = []
    for stmt in _statements(code):
        masked = _mask_literals(stmt)
        if not _STATEMENT_OPENING.match(masked):
            problems.append(f"statement form this reader doesn't know: {_flat(stmt)[:80]!r}")
        for m in _UNREAD_WORDS.finditer(masked):
            problems.append(f"{m.group(0).upper()} is not read by this check: {_flat(stmt)[:80]!r}")
        for m in re.finditer(r"\bINTO\b", masked, re.I):
            if not re.search(r"\bINSERT\s*$", masked[:m.start()], re.I):
                problems.append(f"INTO outside INSERT INTO: {_flat(stmt)[:80]!r}")
        for m in re.finditer(r"\bCREATE\b", masked, re.I):
            if masked[:m.start()].strip():
                problems.append(f"CREATE inside another statement: {_flat(stmt)[:80]!r}")
    return problems


# --------------------------------------------------------------------------
# Seed schema reader
# --------------------------------------------------------------------------
_TEXT_TYPES = frozenset({"char", "character", "varchar", "character varying", "text", "citext", "bpchar",
                         "name", "json", "jsonb", "xml"})
_OTHER_TYPES = frozenset({"smallint", "integer", "int", "int2", "int4", "int8", "bigint", "smallserial",
                          "serial", "bigserial", "decimal", "numeric", "real", "double precision", "float",
                          "float4", "float8", "boolean", "bool", "date", "time", "timestamp", "timestamptz",
                          "timestamp with time zone", "timestamp without time zone", "interval", "uuid"})
_TYPE = re.compile(
    r"\s+(?P<type>double\s+precision|character\s+varying|time(?:stamp)?\s+with(?:out)?\s+time\s+zone"
    r"|[a-z][a-z0-9_]*)(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?(?P<array>(?:\s*\[\s*\])*)",
    re.I,
)
_COLUMN_CONSTRAINT = re.compile(
    r"\s*(?:(?P<plain>NOT\s+NULL|NULL|PRIMARY\s+KEY|UNIQUE)"
    r"|(?P<ref>REFERENCES\s+(?:\w+\.)?\w+\s*\(\s*\w+\s*\)"
    r"(?:\s+ON\s+(?:DELETE|UPDATE)\s+(?:CASCADE|RESTRICT|NO\s+ACTION|SET\s+NULL|SET\s+DEFAULT))*)"
    r"|(?P<check>CHECK\s*(?=\())"
    r"|DEFAULT\s+(?P<default>'[^']*'(?:::\w+(?:\s*\(\s*\d+\s*\))?)?|-?\d+(?:\.\d+)?|INTERVAL\s+'[^']*'"
    r"|\w+\s*\(\s*\)|CURRENT_TIMESTAMP|CURRENT_DATE|TRUE|FALSE|NULL))(?![\w'])",
    re.I,
)
_TABLE_CONSTRAINT = re.compile(
    r"\s*(?:CONSTRAINT\s+\w+\s+)?(?:(?:PRIMARY\s+KEY|UNIQUE)\s*\([\w\s,]+\)"
    r"|FOREIGN\s+KEY\s*\([\w\s,]+\)\s*REFERENCES\s+(?:\w+\.)?\w+\s*\([\w\s,]+\)|CHECK\s*(?=\())",
    re.I,
)


class _Column(NamedTuple):
    name: str
    textual: bool
    defaults: tuple[str, ...]  # each DEFAULT expression this column was declared with


class _Seed(NamedTuple):
    code: str
    statements: tuple[str, ...]
    tables: dict[str, dict[str, _Column]]  # table -> column -> declaration, in column order
    views: dict[str, dict[str, str]]  # view -> column -> its expression, for columns computed from a literal
    schemas: dict[str, str | None]  # table -> the schema it is created in
    paths: tuple[str | None, ...]  # the search_path schema in force at each statement


def _declaration(text: str, masked: str) -> _Column | None:
    """A column declaration, or ``None`` for a table constraint. Every word of
    the declaration must be read, so nothing in it goes unchecked."""
    where = f"declaration this reader doesn't know: {_flat(text)!r}"
    constraint = _TABLE_CONSTRAINT.match(masked)
    if constraint:
        end = constraint.end()
        if constraint.group(0).rstrip().upper().endswith("CHECK"):
            end = _closing(masked, end) + 1
        assert not masked[end:].strip(), where
        return None
    name = re.match(r"\s*([a-z_][a-z0-9_]*)", masked, re.I)
    typ = name and _TYPE.match(masked, name.end())
    assert typ, where
    base = " ".join(typ.group("type").lower().split())
    assert base in _TEXT_TYPES or base in _OTHER_TYPES, f"type {base!r} this reader doesn't know: {_flat(text)!r}"
    defaults, i = [], typ.end()
    while masked[i:].strip():
        m = _COLUMN_CONSTRAINT.match(masked, i)
        assert m, where
        if m.group("check"):
            i = _closing(masked, m.end()) + 1
            continue
        if m.group("default") is not None:
            defaults.append(text[m.start("default"):m.end("default")])
        i = m.end()
    return _Column(name.group(1).lower(), base in _TEXT_TYPES, tuple(defaults))


def _alter(table: str, actions: str, masked: str, tables: dict[str, dict[str, _Column]]) -> None:
    """Apply one ALTER TABLE's actions; any action this reader doesn't know fails."""
    for a, b in _spans(masked, 0, len(masked)):
        act, act_masked = actions[a:b], masked[a:b]
        where = f"ALTER TABLE {table} action this reader doesn't know: {_flat(act)!r}"
        rename_table = re.fullmatch(r"\s*RENAME\s+TO\s+(\w+)\s*", act_masked, re.I)
        rename_column = re.fullmatch(r"\s*RENAME\s+(?:COLUMN\s+)?(\w+)\s+TO\s+(\w+)\s*", act_masked, re.I)
        drop = re.fullmatch(r"\s*DROP\s+(CONSTRAINT\s+)?(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?(\w+)"
                            r"(?:\s+(?:CASCADE|RESTRICT))?\s*", act_masked, re.I)
        add = re.match(r"\s*ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?", act_masked, re.I)
        # A NOT NULL change adds or removes no column and writes no value.
        nullability = re.fullmatch(r"\s*ALTER\s+(?:COLUMN\s+)?(\w+)\s+(?:DROP|SET)\s+NOT\s+NULL\s*",
                                   act_masked, re.I)
        if rename_table:
            if table in tables:
                tables[rename_table.group(1).lower()] = tables.pop(table)
        elif rename_column:
            assert table in tables, f"ALTER TABLE {table}: a table this file does not create"
            old, new = rename_column.group(1).lower(), rename_column.group(2).lower()
            column = tables[table].pop(old)
            tables[table][new] = column._replace(name=new)
        elif drop:
            if not drop.group(1) and table in tables:
                tables[table].pop(drop.group(2).lower(), None)
        elif add:
            column = _declaration(act[add.end():], act_masked[add.end():])
            if column is not None:
                assert table in tables, f"ALTER TABLE {table}: a table this file does not create"
                known = tables[table].get(column.name)
                if known:
                    column = known._replace(textual=known.textual or column.textual,
                                            defaults=known.defaults + column.defaults)
                tables[table][column.name] = column
        elif nullability:
            assert table in tables and nullability.group(1).lower() in tables[table], (
                f"ALTER TABLE {table}: {nullability.group(1)} is not a column this file creates")
        else:
            raise AssertionError(where)


def _view(stmt: str, masked: str, tables: dict[str, dict[str, _Column]]) -> tuple[str, dict[str, str]]:
    """A view's name and the columns it computes from a string literal. Its
    other columns must be columns of the one table it reads."""
    head = re.match(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:\w+\.)?(\w+)\s+AS\s+SELECT\b", masked, re.I)
    assert head, f"view form this reader doesn't know: {_flat(stmt)[:80]!r}"
    end = _clause_end(masked, head.end(), "FROM")
    source = re.fullmatch(r"FROM\s+(?:\w+\.)?(\w+)\s*", masked[end:], re.I)
    assert source and source.group(1).lower() in tables, (
        f"view {head.group(1)} must read one table this file creates: {_flat(stmt[end:])[:80]!r}")
    columns = tables[source.group(1).lower()]
    computed = {}
    for a, b in _spans(masked, head.end(), end):
        item, item_masked = stmt[a:b].strip(), masked[a:b].strip()
        bare = re.fullmatch(r"(\w+)(?:\s+AS\s+\w+)?", item_masked, re.I)
        if bare:
            assert bare.group(1).lower() in columns, f"view {head.group(1)}: {bare.group(1)} is not a column"
            continue
        alias = re.search(r"\s+AS\s+(\w+)$", item_masked, re.I)
        assert alias, f"view {head.group(1)}: computed column without a name: {_flat(item)[:60]!r}"
        if "'" in item_masked:
            computed[alias.group(1).lower()] = item[:alias.start()].strip()
    return head.group(1).lower(), computed


@functools.lru_cache(maxsize=None)
def _read(sql: str) -> _Seed:
    """The seed file's code, statements, tables and views. Anything the reader
    does not know fails here, so it can never be skipped."""
    code = _sql_code(sql)
    statements = tuple(_statements(code))
    tables: dict[str, dict[str, _Column]] = {}
    views: dict[str, dict[str, str]] = {}
    schemas: dict[str, str | None] = {}
    paths: list[str | None] = []
    path = None
    for stmt in statements:
        masked = _mask_literals(stmt)
        search_path = re.match(r"\s*SET\s+search_path\s+(?:TO|=)\s+(\w+)\s*$", masked, re.I)
        if search_path:
            path = search_path.group(1).lower()
        else:
            assert not re.match(r"\s*SET\s+search_path\b", masked, re.I), (
                f"search_path form this reader doesn't know: {_flat(stmt)!r}")
        paths.append(path)
        create = re.match(r"\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?:(\w+)\.)?(\w+)\s*\(", masked, re.I)
        if create:
            table = create.group(2).lower()
            assert table not in tables, f"two tables named {table}"
            schemas[table] = create.group(1).lower() if create.group(1) else path
            close = _closing(masked, create.end() - 1)
            assert not masked[close + 1:].strip(), f"CREATE TABLE {table}: text after the column list"
            body, body_masked = stmt[create.end():close], masked[create.end():close]
            columns: dict[str, _Column] = {}
            for a, b in _spans(body_masked, 0, len(body_masked)):
                column = _declaration(body[a:b], body_masked[a:b])
                if column is not None:
                    assert column.name not in columns, f"{table}: {column.name} declared twice"
                    columns[column.name] = column
            tables[table] = columns
        elif re.match(r"\s*CREATE\b", masked, re.I) and re.search(r"\bTABLE\b", masked[:80], re.I):
            raise AssertionError(f"CREATE TABLE form this reader doesn't know: {_flat(stmt)[:80]!r}")
        elif re.match(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\b", masked, re.I):
            name, computed = _view(stmt, masked, tables)
            views[name] = computed
        for m in re.finditer(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?:\w+\.)?(\w+)\s+", masked, re.I):
            _alter(m.group(1).lower(), stmt[m.end():], masked[m.end():], tables)
        for table in set(tables) - set(schemas):  # renamed by ALTER TABLE ... RENAME TO
            schemas[table] = path
    return _Seed(code, statements, tables, views, schemas, tuple(paths))


def _target_problem(seed: _Seed, index: int, qualifier: str | None, table: str) -> str | None:
    """Why a write's target is not the table this file created under that name, or ``None``."""
    if table not in seed.tables:
        return f"{table} is not a table this file creates"
    schema = qualifier.lower() if qualifier else seed.paths[index]
    if schema != seed.schemas[table]:
        return f"{schema or '(no schema)'}.{table} is not the {table} this file creates"
    return None


def _targets(masked: str) -> set[tuple[str | None, str]]:
    """Every ``(schema, table)`` a statement inserts into or updates."""
    found = {(m.group(1), m.group(2).lower())
             for m in re.finditer(r"\bINSERT\s+INTO\s+(?:(\w+)\.)?(\w+)", masked, re.I)}
    found |= {(m.group(1), m.group(2).lower()) for m in re.finditer(
        r"\bUPDATE\s+(?!SET\b)(?:ONLY\s+)?(?:(\w+)\.)?(\w+)", masked, re.I)}
    return found


def _seed_rows(sql: str) -> list[tuple[str, list[str], list[list[str]]]]:
    """Every ``(table, columns, rows)`` a seed file writes through a VALUES list.

    Reads the forms the seed uses: ``INSERT INTO t VALUES`` (the table's
    column order), ``INSERT INTO t (cols) VALUES``, ``FROM (VALUES ...) AS v
    (cols)`` and a CTE ``s (cols) AS (VALUES ...)`` feeding an INSERT or an
    UPDATE. A statement whose VALUES feed more than one table, and any other
    placement of VALUES, fails, so a new form is read or reported, never skipped.
    """
    seed = _read(sql)
    out = []
    for index, stmt in enumerate(seed.statements):
        masked = _mask_literals(stmt)
        values = list(re.finditer(r"\bVALUES\b", masked, re.I))
        if not values:
            continue
        targets = _targets(masked)
        assert len({t for _, t in targets}) == 1, (
            f"VALUES feeding {sorted(t for _, t in targets) or 'no table'}: {_flat(stmt)[:80]!r}")
        table = next(iter(targets))[1]
        for qualifier, _ in targets:
            problem = _target_problem(seed, index, qualifier, table)
            assert problem is None, f"{problem}: {_flat(stmt)[:80]!r}"
        for m in values:
            before = masked[:m.start()]
            rows, end = _tuples(stmt, m.end())
            cte = re.search(r"(?:\bWITH|,)\s*\w+\s*\(([^)]*)\)\s*AS\s*\(\s*$", before, re.I)
            header = re.search(r"\bINSERT INTO\s+(?:\w+\.)?\w+\s*(?:\(([^)]*)\))?\s*$", before, re.I)
            if cte:
                cols = _columns(cte.group(1))
            elif re.search(r"\bFROM\s*\(\s*$", before, re.I):
                alias = re.match(r"\s*\)\s*AS\s+\w+\s*\(([^)]*)\)", masked[end:], re.I)
                assert alias, f"{table}: FROM (VALUES ...) without an AS alias (columns)"
                cols = _columns(alias.group(1))
            elif header:
                cols = _columns(header.group(1)) if header.group(1) else list(seed.tables[table])
            else:
                raise AssertionError(f"{table}: VALUES in a form this reader does not know: {before[-80:]!r}")
            for row in rows:
                assert len(row) == len(cols), f"{table}: {len(row)} values for {len(cols)} columns: {row[:3]}"
            out.append((table, cols, rows))
    return out


def _kind(kinds: dict, table: str, column: str) -> str | None:
    return kinds.get(table, {}).get(column)


def _classification_problems(name: str, sql: str, kinds: dict[str, dict[str, str]]) -> list[str]:
    """Textual columns missing from ``kinds``, and entries in it that name none."""
    seed = _read(sql)
    expected = {(t, c) for t, cols in seed.tables.items() for c, col in cols.items() if col.textual}
    expected |= {(v, c) for v, cols in seed.views.items() for c in cols}
    given = {(t, c): k for t, cols in kinds.items() for c, k in cols.items()}
    problems = [f"{name}: {t}.{c} is textual; classify it in _SEED_COLUMNS as person, code, thing or text"
                + _classify_hint(c) for t, c in sorted(expected - given.keys())]
    problems += [f"{name}: _SEED_COLUMNS lists {t}.{c}, which is not a textual column here"
                 for t, c in sorted(given.keys() - expected)]
    problems += [f"{name}: {t}.{c} has kind {k!r}" for (t, c), k in sorted(given.items()) if k not in _KINDS]
    problems += [f"{name}: view {v} computes person column {c}; a view may only pass a person column through"
                 for v, cols in seed.views.items() for c in cols if given.get((v, c)) == _PERSON]
    return problems


def _value_problems(sql: str, kinds: dict[str, dict[str, str]]) -> list[str]:
    """Person and code values that fail their check: VALUES rows, column
    defaults and the literals of a view's computed code columns."""
    seed = _read(sql)
    problems = []
    for table, cols, rows in _seed_rows(sql):
        if table not in seed.tables:
            problems.append(f"{table}: rows for a table this file does not create")
            continue
        for j, col in enumerate(cols):
            column = seed.tables[table].get(col)
            if column is None:
                problems.append(f"{table}: {col} is not a column of {table}")
                continue
            kind = _kind(kinds, table, col)
            if not column.textual or kind not in (_PERSON, _CODE_KIND):
                continue
            for row in rows:
                value = _literal(row[j])
                problem = "not a literal" if value is _NOT_LITERAL else _kind_problem(kind, value)
                if problem:
                    problems.append(f"{table}.{col} = {row[j]}: {problem}")
    for table, cols in seed.tables.items():
        for column in cols.values():
            kind = _kind(kinds, table, column.name)
            if column.textual and kind in (_PERSON, _CODE_KIND):
                for default in column.defaults:
                    value = _literal(default)
                    problem = "not a literal" if value is _NOT_LITERAL else _kind_problem(kind, value)
                    if problem:
                        problems.append(f"{table}.{column.name} DEFAULT {default}: {problem}")
    for view, cols in seed.views.items():
        for col, expr in cols.items():
            if _kind(kinds, view, col) == _CODE_KIND:
                for literal in re.findall(r"'((?:[^']|'')*)'", expr):
                    if _kind_problem(_CODE_KIND, literal):
                        problems.append(f"view {view}.{col}: {literal!r} is not a code")
    return sorted(set(problems))


def _person_values(path: Path) -> list[tuple[str, str, object]]:
    """``(table, column, value)`` for every person column a seed file writes through VALUES."""
    kinds = _SEED_COLUMNS.get(path.name, {})
    return [(table, col, _literal(row[j]))
            for table, cols, rows in _seed_rows(path.read_text(encoding="utf-8"))
            for j, col in enumerate(cols) if _kind(kinds, table, col) == _PERSON
            for row in rows]


# --------------------------------------------------------------------------
# Writes outside VALUES lists
# --------------------------------------------------------------------------
_SQL_WORDS = frozenset({"as", "cross", "full", "group", "inner", "join", "left", "limit", "natural",
                        "on", "order", "outer", "returning", "right", "set", "union", "using", "where"})
_SET_CLAUSE = re.compile(
    r"\bUPDATE\s+(?:(?:ONLY\s+)?(?:(?P<schema>\w+)\.)?(?P<table>\w+)(?:\s+(?:AS\s+)?(?!SET\b)\w+)?\s+)?SET\b", re.I)
_INSERT_HEAD = re.compile(
    r"\bINSERT INTO\s+(?:(?P<schema>\w+)\.)?(\w+)\s*(?:\(([^)]*)\))?\s*(VALUES|SELECT)?\b", re.I)
_REF = re.compile(r"(\w+)\.(\w+)(?:::\w+(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)?")
_UNKNOWN_FORM = "write in a form this reader doesn't know"


def _values_sources(stmt: str, masked: str) -> dict[str, frozenset[str] | None]:
    """Qualifier -> columns of each VALUES list in ``stmt`` (which ``_seed_rows``
    reads): a CTE ``s (cols) AS (VALUES ...)`` by its name and by any FROM
    alias, and a ``FROM (VALUES ...) AS v (cols)`` by its alias."""
    sources: dict[str, frozenset[str] | None] = {}
    for m in re.finditer(r"(?:\bWITH|,)\s*(\w+)\s*\(([^)]*)\)\s*AS\s*\(\s*VALUES\b", masked, re.I):
        sources[m.group(1).lower()] = frozenset(_columns(m.group(2)))
    for m in re.finditer(r"\bFROM\s*\(\s*VALUES\b", masked, re.I):
        _, end = _tuples(stmt, m.end())
        alias = re.match(r"\s*\)\s*AS\s+(\w+)\s*\(([^)]*)\)", masked[end:], re.I)
        if alias:
            sources[alias.group(1).lower()] = frozenset(_columns(alias.group(2)))
    for m in re.finditer(r"(?:\bFROM|\bJOIN|,)\s+(\w+)\s+(?:AS\s+)?(\w+)\b", masked, re.I):
        name, alias = m.group(1).lower(), m.group(2).lower()
        if name in sources and alias not in _SQL_WORDS:
            sources.setdefault(alias, sources[name])
    return sources


def _write_problem(seed: _Seed, kinds: dict, table: str | None, column: str | None, expr: str,
                   sources: dict) -> str | None:
    """Why a write of ``expr`` to ``table.column`` is not checked, or ``None`` when it is.

    A person or code column may be written only from a literal that passes its
    check, from the same-named column of a VALUES list the reader reads, or
    from the same-named ``EXCLUDED`` column in a ``DO UPDATE SET``."""
    if column is None or table is None:
        return _UNKNOWN_FORM
    if table not in seed.tables:
        return f"{table} is not a table this file creates"
    declared = seed.tables[table].get(column)
    if declared is None:
        return f"{column} is not a column of {table}"
    kind = _kind(kinds, table, column)
    if not declared.textual or kind in (_THING, _TEXT):
        return None
    if kind is None:
        return f"{table}.{column} is not classified"
    value = _literal(expr)
    if value is not _NOT_LITERAL:
        return _kind_problem(kind, value)
    ref = _REF.fullmatch(expr.strip())
    if ref:
        qualifier, source = ref.group(1).lower(), ref.group(2).lower()
        if source == column and qualifier in sources and (sources[qualifier] is None or source in sources[qualifier]):
            return None
    return f"{kind} column written in a form this reader doesn't know"


def _writes(sql: str, kinds: dict[str, dict[str, str]]) -> list[tuple[str, str | None, str | None, str, str | None]]:
    """Every column assignment a seed file makes outside a VALUES list, as
    ``(form, table, column, expression, problem)``.

    ``form`` is ``SET`` (``UPDATE ... SET`` and ``ON CONFLICT DO UPDATE SET``)
    or ``SELECT`` (an ``INSERT ... SELECT`` list entry). A SELECT list that
    writes no person or code column is skipped (the generated readings). A
    write the reader cannot take apart comes back with column ``None``.
    """
    seed = _read(sql)
    out = []

    def add(form, index, schema, table, column, expr, sources):
        problem = None if table is None else _target_problem(seed, index, schema, table)
        problem = problem or _write_problem(seed, kinds, table, column, expr, sources)
        out.append((form, table, column, expr, problem))

    for index, stmt in enumerate(seed.statements):
        masked = _mask_literals(stmt)
        sources = _values_sources(stmt, masked)
        inserted = _INSERT_HEAD.search(masked)
        sets = list(_SET_CLAUSE.finditer(masked))
        if len(sets) != len(re.findall(r"\bUPDATE\b", masked, re.I)):
            add("SET", index, None, None, None, _flat(stmt)[:120], {})
        for m in sets:
            allowed = dict(sources)
            schema, table = m.group("schema"), m.group("table")
            if re.search(r"\bDO\s*$", masked[:m.start()], re.I):
                allowed["excluded"] = None  # any column of the INSERT's own rows
                schema, table = (inserted.group("schema"), inserted.group(2)) if inserted else (None, None)
            table = table.lower() if table else None
            for a, b in _spans(masked, m.end(), _clause_end(masked, m.end(), "FROM|WHERE|RETURNING")):
                part = stmt[a:b].strip()
                assign = re.fullmatch(r"(\w+)\s*=\s*(.+)", part, re.S)
                if assign:
                    add("SET", index, schema, table, assign.group(1).lower(), assign.group(2).strip(), allowed)
                else:
                    add("SET", index, schema, table, None, _flat(part), {})
        for m in _INSERT_HEAD.finditer(masked):
            schema, table, cols, verb = m.group("schema"), m.group(2).lower(), m.group(3), m.group(4)
            if verb is None:
                add("INSERT", index, schema, table, None, _flat(stmt[m.start():m.end() + 40]), {})
                continue
            if verb.upper() != "SELECT":
                continue
            if _target_problem(seed, index, schema, table):
                add("SELECT", index, schema, table, None, f"INSERT INTO {table}", {})
                continue
            columns = _columns(cols) if cols else list(seed.tables[table])
            if not any(_kind(kinds, table, c) in (_PERSON, _CODE_KIND) or c not in seed.tables[table]
                       for c in columns):
                continue  # writes no person or code column
            start = m.end()
            distinct = re.match(r"\s*DISTINCT\b", masked[start:], re.I)
            if distinct:
                start += distinct.end()
            spans = _spans(masked, start, _clause_end(masked, start, "FROM|WHERE|ON|UNION|GROUP|ORDER|LIMIT"))
            if len(spans) != len(columns):
                add("SELECT", index, schema, table, None, f"{table}: {len(spans)} values for {len(columns)} columns", {})
                continue
            for col, (a, b) in zip(columns, spans):
                add("SELECT", index, schema, table, col, stmt[a:b].strip(), sources)
    return out


def _write_problems(sql: str, kinds: dict[str, dict[str, str]]) -> list[str]:
    return sorted({f"{form} {table or '?'}.{column or '?'} = {expr}: {problem}"
                   for form, table, column, expr, problem in _writes(sql, kinds) if problem})


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------
def test_generator_contact_names_are_approved():
    """The ERP generator draws contact names only from approved names."""
    lists = _generator_contact_lists(_GENERATOR.read_text(encoding="utf-8"))
    assert len(lists) >= 2, f"{_GENERATOR.name}: expected the customer and supplier contact lists, found {len(lists)}"
    assert _unapproved(lists) == [], f"{_GENERATOR.name}: not approved"


@pytest.mark.parametrize("path", _SEED_FILES, ids=lambda p: p.name)
def test_every_seed_statement_is_a_form_the_reader_knows(path):
    problems = _census_problems(_read(path.read_text(encoding="utf-8")).code)
    assert problems == [], f"{path.name}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("path", _SEED_FILES, ids=lambda p: p.name)
def test_every_textual_seed_column_is_classified(path):
    """A new CHAR, VARCHAR or TEXT column (or a view column computed from a
    literal) fails here until ``_SEED_COLUMNS`` says what it holds."""
    problems = _classification_problems(path.name, path.read_text(encoding="utf-8"),
                                        _SEED_COLUMNS.get(path.name, {}))
    assert problems == [], "\n  ".join(problems)


def test_seed_column_map_names_only_seed_files():
    assert set(_SEED_COLUMNS) <= {p.name for p in _SEED_FILES}


@pytest.mark.parametrize("path", _SEED_FILES, ids=lambda p: p.name)
def test_seed_values_match_their_column_kinds(path):
    """Person values are approved names and code values are space-free, in
    VALUES rows, column defaults and views."""
    problems = _value_problems(path.read_text(encoding="utf-8"), _SEED_COLUMNS.get(path.name, {}))
    assert problems == [], f"{path.name}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("path", _SEED_FILES, ids=lambda p: p.name)
def test_every_seed_write_outside_values_is_checked(path):
    """``UPDATE ... SET``, ``ON CONFLICT DO UPDATE SET`` and ``INSERT ...
    SELECT`` write a person or code column only from a checked literal, from
    the same column of a VALUES list the reader reads, or from ``EXCLUDED``."""
    problems = _write_problems(path.read_text(encoding="utf-8"), _SEED_COLUMNS.get(path.name, {}))
    assert problems == [], f"{path.name}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("path", _SEED_FILES, ids=lambda p: p.name)
def test_every_seed_person_column_is_read(path):
    """Each person column a file creates yields values through a VALUES list,
    and every INSERT into its table carries one."""
    seed = _read(path.read_text(encoding="utf-8"))
    kinds = _SEED_COLUMNS.get(path.name, {})
    read = {(t, c) for t, c, _ in _person_values(path)}
    defined = {(t, c) for t, cols in seed.tables.items() for c in cols if _kind(kinds, t, c) == _PERSON}
    assert defined <= read, f"{path.name}: no values read for {sorted(defined - read)}"
    for stmt in seed.statements:
        masked = _mask_literals(stmt)
        target = re.search(r"\bINSERT INTO\s+(?:\w+\.)?(\w+)", masked, re.I)
        if target and any(_kind(kinds, target.group(1).lower(), c) == _PERSON
                          for c in seed.tables.get(target.group(1).lower(), ())):
            assert re.search(r"\bVALUES\b", masked, re.I), f"{path.name}: {target.group(1)} rows not from a VALUES list"


def test_site_cmms_story_rows_are_read():
    """The Indianapolis work orders and log entries are all read."""
    by_column: dict[str, int] = {}
    for table, col, _ in _person_values(_SITE_CMMS):
        by_column[f"{table}.{col}"] = by_column.get(f"{table}.{col}", 0) + 1
    assert by_column == {"work_orders.reported_by": 13, "work_order_log.author": 6}, by_column


def test_site_cmms_convergence_writes_are_read():
    """The re-run convergence in 011 is seen as three person writes, each from a checked source."""
    kinds = _SEED_COLUMNS[_SITE_CMMS.name]
    writes = [(form, table, column, expr, problem)
              for form, table, column, expr, problem in _writes(_SITE_CMMS.read_text(encoding="utf-8"), kinds)
              if column and _kind(kinds, table, column) == _PERSON]
    assert writes == [("SET", "work_orders", "reported_by", "EXCLUDED.reported_by", None),
                      ("SET", "work_order_log", "author", "s.author", None),
                      ("SELECT", "work_order_log", "author", "s.author", None)], writes


@pytest.mark.parametrize("path", _SHORT_FORM_FILES, ids=lambda p: p.name)
def test_short_form_names_in_story_files_are_approved(path):
    """Every quoted ``Initial. Surname`` value in a file that carries the story
    rows shortens an approved name."""
    hits = _QUOTED_SHORT_FORM.findall(path.read_text(encoding="utf-8"))
    assert hits, f"{path.name}: no short-form names found (moved or renamed?)"
    bad = sorted({h for h in hits if _problem(h)})
    assert bad == [], f"{path.name}: {bad}"


# --------------------------------------------------------------------------
# The checks bite
# --------------------------------------------------------------------------
def test_name_check_rejects_names_off_the_list():
    for ok in ("R. Roe", "Richard Roe", "J. Stiles", "D. Ramirez", "Terry Whitlock", "OP-0101 via MES", None):
        assert _problem(ok) is None, ok
    for bad in ("X. Placeholder", "Unlisted Placeholder", "Not A Listed Name", "Unlisted", "R. Stiles", "X. Roe", "r. roe",
                "OP-0199 via MES"):
        assert _problem(bad), bad
    assert _problem("X. Placeholder") == "short form of no approved name"
    assert _kind_problem(_CODE_KIND, "Not A Listed Name") == "not a code"
    assert _kind_problem(_CODE_KIND, "") is None and _kind_problem(_TEXT, "Not A Listed Name") is None


def test_generator_check_does_not_approve_its_own_names():
    source = ("contact_name = random.choice(['Jane Doe', 'Not A Listed Name'])\n"
              "contact_name: str = random.choice(['Li Juan'])\n"
              "rows = [(contact_name := random.choice(['Mary Major']))]\n"
              "record = {'contact_name': contact_name}\nrecord['contact_name'] = contact_name\n")
    lists = _generator_contact_lists(source)
    assert lists == [["Jane Doe", "Not A Listed Name"], ["Li Juan"], ["Mary Major"]]
    assert _unapproved(lists) == ["Not A Listed Name"]


@pytest.mark.parametrize("source", [
    "contact_name = random.choice(['Jane Doe'])\ncontact_name = 'Not A Listed Name'\n",
    "contact_name = secrets.choice(['Jane Doe'])\n",
    "contact_name = choice(['Jane Doe'])\n",
    "contact_name = random.choice(['Jane Doe'], ['Not A Listed Name'])\n",
    "contact_name = random.choice(seq=['Jane Doe'])\n",
    "contact_name = random.choice(('Jane Doe',))\n",
    "contact_name = random.choice(['Jane Doe', name])\n",
    "contact_name = random.choice(NAMES)\n",
    "contact_name: str = 'Not A Listed Name'\n",
    "contact_name, other = 'Not A Listed Name', 1\n",
    "contact_name = other = random.choice(['Jane Doe'])\n",
    "contact_name += ' Name'\n",
    "x = (contact_name := 'Not A Listed Name')\n",
    "for contact_name in ['Not A Listed Name']:\n    pass\n",
    "rows = [contact_name for contact_name in ['Not A Listed Name']]\n",
    "with open('names') as contact_name:\n    pass\n",
    "def make(contact_name='Not A Listed Name'):\n    pass\n",
    "from names import placeholder as contact_name\n",
    "record = {'contact_name': 'Not A Listed Name'}\n",
    "record['contact_name'] = 'Not A Listed Name'\n",
    "record.contact_name = 'Not A Listed Name'\n",
    "record = dict(contact_name='Not A Listed Name')\n",
    "setattr(record, 'contact_name', 'Not A Listed Name')\n",
    "contact_name = random.choice(['Jane Doe'])\nmatch 'Not A Listed Name':\n    case contact_name:\n        pass\n",
    "match 'Not A Listed Name':\n    case str() as contact_name:\n        pass\n",
    "match ['Not A Listed Name']:\n    case [*contact_name]:\n        pass\n",
    "match {'name': 'Not A Listed Name'}:\n    case {**contact_name}:\n        pass\n",
    "match {'name': 'Not A Listed Name'}:\n    case {'name': contact_name}:\n        pass\n",
    "def contact_name():\n    return 'Not A Listed Name'\n",
    "async def contact_name():\n    return 'Not A Listed Name'\n",
    "class contact_name:\n    pass\n",
    "type contact_name = str\n",
    "def make[contact_name]():\n    pass\n",
    "from names import *\n",
    "contact_name = random.choice(['Jane Doe'])\nimport contact_name.submodule\n",
    "exec(\"contact_name = 'Not A Listed Name'\")\n",
    "eval(\"record.update(contact_name='Not A Listed Name')\")\n",
])
def test_generator_check_rejects_every_other_contact_name_binding(source):
    with pytest.raises(AssertionError):
        _generator_contact_lists(source)


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (E'Not A Listed Name');\n",
    "INSERT INTO t VALUES (U&'Not A Listed Name');\n",
    "INSERT INTO t VALUES ($q$Not A Listed Name$q$);\n",
    "INSERT INTO t VALUES ($$Not A Listed Name$$);\n",
    'INSERT INTO "t" VALUES (1);\n',
    "INSERT INTO t VALUES ('a\\'b');\n",
    "INSERT INTO t VALUES ('unterminated);\n",
    "/* it's */ INSERT INTO t VALUES (1);\n/* unterminated\n",
])
def test_lexer_refuses_quoting_it_does_not_model(sql):
    with pytest.raises(AssertionError):
        _sql_code(sql)


def test_lexer_strips_comments_and_reads_do_blocks():
    code = _sql_code("/* don't /* nested */ */ SELECT 1; -- it's\nDO $$ BEGIN UPDATE t SET a = 'x'; END $$;\n")
    assert "don" not in code and "it's" not in code and "$" not in code
    assert [_flat(s) for s in _statements(code)] == ["SELECT 1", "DO BEGIN UPDATE t SET a = 'x'", "END"]


@pytest.mark.parametrize("sql", [
    "COPY t FROM STDIN;\n",
    "SELECT 'Not A Listed Name' AS reviewer INTO t2;\n",
    "MERGE INTO t USING s ON true WHEN NOT MATCHED THEN INSERT VALUES ('Not A Listed Name');\n",
    "DO $$ BEGIN EXECUTE 'INSERT INTO t VALUES (''Not A Listed Name'')'; END $$;\n",
    "CREATE FUNCTION f() RETURNS void AS 'SELECT 1' LANGUAGE sql;\n",
    "COMMENT ON COLUMN t.c IS 'Not A Listed Name';\n",
    "CREATE TYPE person AS ENUM ('Not A Listed Name');\n",
])
def test_statement_census_refuses_forms_it_does_not_read(sql):
    assert _census_problems(_sql_code(sql))


def test_seed_reader_reads_every_form_and_keeps_quotes():
    sql = (
        "CREATE TABLE IF NOT EXISTS t (\n    id INT,\n    reported_by VARCHAR(60)\n);\n"
        "INSERT INTO t VALUES\n(1, 'R. Roe'),\n(2, 'it''s -- not a comment')\nON CONFLICT (id) DO NOTHING;\n"
        "-- INSERT INTO t VALUES (9, 'Commented Out');\n"
        "INSERT INTO t (reported_by, id) VALUES ('J. Stiles', 3);\n"
        "INSERT INTO t (id, reported_by)\nSELECT v.id::INT, v.reported_by\nFROM (VALUES\n('4', 'X. Placeholder')\n"
        ") AS v (id, reported_by)\nWHERE NOT EXISTS (SELECT 1 FROM t WHERE t.id = v.id::INT);\n"
        "WITH seed (id, reported_by) AS (VALUES\n('5', NULL), ('6', 'D. Ramirez')\n)\n"
        "INSERT INTO t (id, reported_by) SELECT s.id::INT, s.reported_by FROM seed s;\n"
        "WITH s2 (id, reported_by) AS (VALUES ('7', 'Not A Listed Name'))\n"
        "UPDATE t SET reported_by = s.reported_by FROM s2 s WHERE t.id = s.id::INT;\n"
    )
    values = [_literal(row[cols.index("reported_by")]) for _, cols, rows in _seed_rows(sql) for row in rows]
    assert values == ["R. Roe", "it's -- not a comment", "J. Stiles", "X. Placeholder", None, "D. Ramirez",
                      "Not A Listed Name"]
    kinds = {"t": {"reported_by": _PERSON}}
    assert len(_value_problems(sql, kinds)) == 3
    for bad in ("CREATE TABLE IF NOT EXISTS t (\n    id INT\n);\nINSERT INTO t VALUES (1, 2);\n",
                "SELECT * FROM (VALUES (1)) AS v (id);\n",
                "CREATE TABLE IF NOT EXISTS t (\n    id INT\n);\nCREATE TABLE IF NOT EXISTS u (\n    id INT\n);\n"
                "WITH s (id) AS (VALUES (1)), x AS (UPDATE u SET id = s.id FROM s) INSERT INTO t SELECT s.id FROM s;\n",
                "SET search_path TO a;\nCREATE TABLE IF NOT EXISTS t (\n    id INT\n);\nINSERT INTO b.t VALUES (1);\n",
                "SET search_path TO a;\nCREATE TABLE IF NOT EXISTS t (\n    id INT\n);\nSET search_path TO b;\n"
                "INSERT INTO t VALUES (1);\n",
                "INSERT INTO elsewhere VALUES (1);\n"):
        with pytest.raises(AssertionError):
            _seed_rows(bad)


def test_table_reader_parses_every_declaration():
    seed = _read(
        "CREATE TABLE IF NOT EXISTS t (\n"
        "    id INT PRIMARY KEY, reviewer VARCHAR(60) NOT NULL DEFAULT 'R. Roe',\n"
        "    tag CHAR(2) CHECK (tag IN ('A,B', 'C')), at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),\n"
        "    other_id INT REFERENCES u(id), CONSTRAINT k UNIQUE (id, tag)\n);\n"
        "ALTER TABLE t ADD COLUMN IF NOT EXISTS note TEXT DEFAULT 'x', ADD CONSTRAINT u2 UNIQUE (note),\n"
        "    DROP COLUMN IF EXISTS tag;\n"
        "ALTER TABLE t RENAME COLUMN note TO shift_lead;\nALTER TABLE gone RENAME TO t_old;\n")
    assert [(c.name, c.textual, c.defaults) for c in seed.tables["t"].values()] == [
        ("id", False, ()), ("reviewer", True, ("'R. Roe'",)), ("at", False, ("NOW()",)),
        ("other_id", False, ()), ("shift_lead", True, ("'x'",))]
    for bad in (
        "CREATE TABLE IF NOT EXISTS t (\n    id INT GENERATED ALWAYS AS (1) STORED\n);\n",
        "CREATE TABLE IF NOT EXISTS t (\n    id INT DEFAULT lower('Not A Listed Name')\n);\n",
        "CREATE TABLE IF NOT EXISTS t (\n    who person_name\n);\n",
        "CREATE TABLE IF NOT EXISTS t (\n    who BYTEA\n);\n",
        "CREATE TABLE IF NOT EXISTS t (\n    id INT\n) INHERITS (u);\n",
        "CREATE TABLE IF NOT EXISTS t AS SELECT 'Not A Listed Name' AS reviewer;\n",
        "CREATE TABLE t (\n    id INT\n);\n",
        "CREATE TABLE IF NOT EXISTS t (\n    id INT\n);\nALTER TABLE t ALTER COLUMN id SET DEFAULT 1;\n",
        "CREATE TABLE IF NOT EXISTS t (\n    id INT\n);\nALTER TABLE u ADD COLUMN reviewer TEXT;\n",
        "CREATE TABLE IF NOT EXISTS t (\n    id INT\n);\nCREATE OR REPLACE VIEW v AS SELECT x FROM t;\n",
        "CREATE TABLE IF NOT EXISTS t (\n    id INT\n);\nCREATE VIEW v AS SELECT id FROM t JOIN u ON true;\n",
    ):
        with pytest.raises(AssertionError):
            _read(bad)


def test_textual_columns_must_be_classified():
    """A person column on a line with another declaration, or under a name
    no vocabulary lists, fails until it is classified."""
    one_line = ("CREATE TABLE IF NOT EXISTS design_reviews (\n    review_id INT PRIMARY KEY, reviewer VARCHAR(60)\n);\n"
                "INSERT INTO design_reviews VALUES (1, 'Not A Listed Name') ON CONFLICT (review_id) DO NOTHING;\n")
    participant = ("CREATE TABLE IF NOT EXISTS sessions (\n    session_id INT PRIMARY KEY,\n"
                   "    participant VARCHAR(60)\n);\n"
                   "INSERT INTO sessions VALUES (1, 'Not A Listed Name') ON CONFLICT (session_id) DO NOTHING;\n")
    (problem,) = _classification_problems("f.sql", one_line, {})
    assert "design_reviews.reviewer is textual; classify it" in problem and "suggests a person" in problem
    (problem,) = _classification_problems("f.sql", participant, {})
    assert "sessions.participant is textual; classify it" in problem
    assert _classification_problems("f.sql", participant, {"sessions": {"participant": "worker"}})
    assert _classification_problems("f.sql", participant, {"sessions": {"participant": "person", "x": "text"}})
    assert _classification_problems("f.sql", participant, {"sessions": {"participant": "person"}}) == []
    assert _value_problems(participant, {"sessions": {"participant": "person"}}) == [
        "sessions.participant = 'Not A Listed Name': not an approved name or a listed non-person value"]
    assert _value_problems(one_line, {"design_reviews": {"reviewer": "code"}}) == [
        "design_reviews.reviewer = 'Not A Listed Name': not a code"]


def test_value_check_reads_defaults_and_views():
    sql = ("CREATE TABLE IF NOT EXISTS t (\n    id INT,\n    reported_by VARCHAR(60) DEFAULT 'Not A Listed Name',\n"
           "    crew VARCHAR(20) DEFAULT 'crew red'\n);\n"
           "CREATE OR REPLACE VIEW v AS SELECT id, reported_by AS who, CASE WHEN id > 1 THEN 'a b' END AS state,\n"
           "    'Not A Listed Name' AS reviewer FROM t;\n")
    kinds = {"t": {"reported_by": _PERSON, "crew": _CODE_KIND}, "v": {"state": _CODE_KIND, "reviewer": _PERSON}}
    assert _value_problems(sql, kinds) == [
        "t.crew DEFAULT 'crew red': not a code",
        "t.reported_by DEFAULT 'Not A Listed Name': not an approved name or a listed non-person value",
        "view v.state: 'a b' is not a code"]
    assert _classification_problems("f.sql", sql, kinds) == [
        "f.sql: view v computes person column reviewer; a view may only pass a person column through"]
    assert any("v.reviewer is textual" in p for p in _classification_problems("f.sql", sql, {"t": kinds["t"]}))


_T = "CREATE TABLE IF NOT EXISTS t (\n    id INT,\n    reported_by VARCHAR(60),\n    crew VARCHAR(20),\n    note TEXT\n);\n"
_T_KINDS = {"t": {"reported_by": _PERSON, "crew": _CODE_KIND, "note": _TEXT}}


def test_write_check_accepts_checked_sources():
    sql = _T + (
        "INSERT INTO t VALUES (1, 'R. Roe', 'crew_red', 'x')\n"  # nosec B608 - linter input text, never executed
        "ON CONFLICT (id) DO UPDATE SET reported_by = EXCLUDED.reported_by, crew = EXCLUDED.crew,\n"
        "    note = EXCLUDED.note\nWHERE t.reported_by IS DISTINCT FROM EXCLUDED.reported_by;\n"
        "UPDATE t SET reported_by = 'J. Stiles', note = 'anything' WHERE id = 1;\n"
        "UPDATE t SET reported_by = NULL WHERE id = 2;\n"
        "WITH seed (id, reported_by) AS (VALUES ('3', 'D. Ramirez')), moved AS (\n"
        "    UPDATE t x SET reported_by = s.reported_by FROM seed s WHERE x.id = s.id::INT\n)\n"
        "INSERT INTO t (id, reported_by, crew, note)\n"
        "SELECT s.id::INT, s.reported_by, 'crew_blue', NULL FROM seed s;\n"
        "INSERT INTO t (id, reported_by)\nSELECT v.id::INT, v.reported_by::VARCHAR(60)\n"
        "FROM (VALUES ('4', 'R. Roe')) AS v (id, reported_by);\n"
        "INSERT INTO t (id, note) SELECT s.* FROM (SELECT 5, 'generated') AS s (id, note);\n"
    )
    assert _write_problems(sql, _T_KINDS) == []


@pytest.mark.parametrize(("stmt", "problem"), [
    ("UPDATE t SET reported_by = 'Not A Listed Name' WHERE id = 1", "not an approved name or a listed non-person value"),
    ("UPDATE t SET reported_by = 'X. Placeholder'", "short form of no approved name"),
    ("update t set reported_by = 'Not A Listed Name'", "not an approved name or a listed non-person value"),
    ("UPDATE t SET crew = 'Not A Listed Name'", "not a code"),
    ("UPDATE t SET reported_by = note", "person column written in a form this reader doesn't know"),
    ("UPDATE t SET reported_by = 'Earlier ' || reported_by", "person column written in a form this reader doesn't know"),
    ("UPDATE t SET reported_by = (SELECT 'R. Roe')", "person column written in a form this reader doesn't know"),
    ("UPDATE t SET reported_by = EXCLUDED.reported_by", "person column written in a form this reader doesn't know"),
    ("INSERT INTO t VALUES (1, 'R. Roe', 'crew_red', 'x') ON CONFLICT (id) DO UPDATE SET reported_by = EXCLUDED.note",
     "person column written in a form this reader doesn't know"),
    ("WITH seed (id, note) AS (VALUES ('1', 'x')) UPDATE t SET reported_by = s.note FROM seed s",
     "person column written in a form this reader doesn't know"),
    ("UPDATE t SET reported_by = u.reported_by FROM other u", "person column written in a form this reader doesn't know"),
    ("UPDATE t SET crew = u.crew FROM other u", "code column written in a form this reader doesn't know"),
    ("UPDATE t SET (reported_by, note) = ('R. Roe', 'x')", _UNKNOWN_FORM),
    ("UPDATE t SET reviewer = 'Not A Listed Name'", "reviewer is not a column of t"),
    ("UPDATE other SET buyer_name = 'Not A Listed Name'", "other is not a table this file creates"),
    ("UPDATE elsewhere.t SET reported_by = 'R. Roe'", "elsewhere.t is not the t this file creates"),
    ("INSERT INTO elsewhere.t (id, reported_by) SELECT 1, 'R. Roe'", "elsewhere.t is not the t this file creates"),
    ("INSERT INTO t (id, reported_by) SELECT 1, 'Not A Listed Name'", "not an approved name or a listed non-person value"),
    ("INSERT INTO t (id, reported_by) SELECT v.id, 'Not A Listed Name' FROM (VALUES ('1', 'R. Roe')) AS v (id, reported_by)",
     "not an approved name or a listed non-person value"),
    ("INSERT INTO t (id, reported_by) SELECT v.id FROM (VALUES ('1')) AS v (id)", _UNKNOWN_FORM),
    ("INSERT INTO t (id, reported_by) SELECT s.* FROM (SELECT 1, 'Not A Listed Name') AS s (id, reported_by)",
     _UNKNOWN_FORM),
    ("INSERT INTO t (id, reported_by) WITH s (id, reported_by) AS (VALUES ('1', 'R. Roe')) SELECT s.id, s.reported_by FROM s",
     _UNKNOWN_FORM),
    ("INSERT INTO t DEFAULT VALUES", _UNKNOWN_FORM),
])
def test_write_check_rejects_unchecked_and_unknown_writes(stmt, problem):
    found = _write_problems(_T + stmt + ";\n", _T_KINDS)
    assert len(found) == 1 and found[0].endswith(problem), found


def test_short_form_scan_finds_quoted_initials():
    text = """reported_by="X. Placeholder"; sql = "SET reported_by = 'R. Roe'"; x = "U.S. Army"; y = 'No. Seven'"""
    assert _QUOTED_SHORT_FORM.findall(text) == ["X. Placeholder", "R. Roe"]
