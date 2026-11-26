# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The other identifier form of listed items, read by the system that pairs them.

One system can keep a concept under one identifier in one field and another
identifier in a second field, while a second system pairs the two per item
(ADR-0087 amendment 2026-10-15, the IDENTIFIER FORMS block). Crossing from one
form to the other was the orchestrator model's job, and it was skipped about
half the time (workshop measurement, 2026-11-06). The engine now crosses it
(``form_crossing``): it asks the pairing system for the other form of the
listed values, and this module is that system's side of the request.

What it reads: the identifier columns of a stored exact-match join between
concepts on the catalog's crossing allowlist (``catalog.CROSSABLE_CONCEPTS``),
read under the system's schema-scoped read-only login.

What the caller may send. The request rides the delegation tool as the
reserved :data:`RESERVED_ARG` argument and holds exactly two things: the id of
a stored JoinPath and the values. The runtime's ASGI seam (``runtime_asgi``)
pops it before any model sees it and accepts it only on the engine's call
shape (the one query tool name this runtime registered,
:func:`query_tool_name`, the fixed :data:`PAIRING_QUESTION` and nothing else,
never in a batch); the orchestrator strips it from every call its model makes.
No column, table or join comes from the caller.

What the runtime decides, from its own graph and schema (:func:`plan`):

* the JoinPath must be stored, active, internal to this system, and pair two
  of its own tables per item (``identifier_translation.pairs_per_item``); its
  stored key columns are the join;
* the two identifier columns are the measured form endpoints: on each of the
  two tables, the one column that a measured exact join (the exact-agreement
  pass, ``identity_affinity.EXACT_METHOD``) links to the same other system for
  the same concept, with that join's measured value shape. Zero or several
  candidates refuse, and so does a read past :data:`MAX_ENDPOINT_JOINS`;
* every requested value must have one of the two measured shapes;
* no column, as the graph spells it or as it is read, may be named like a
  person-valued or counterparty field of the catalog's own vocabulary
  (``catalog.PERSON_VALUED_FIELDS``, matched as whole words after camelCase,
  PascalCase, digits and separators are split: :func:`person_named`);
* concepts, from the graph's own mappings (:func:`concept_check`): every
  concept each form's exact join records (its agreed concept and the
  catalog-related one its label names), and every concept each endpoint and
  key column maps to (onboarding's ``Field-[:MAPS_TO]->Concept`` edges), must
  be on the server-owned allowlist of plant-generated production records,
  ``catalog.CROSSABLE_CONCEPTS``, and, the second check, not in
  ``catalog.PERSON_CONCEPTS``; each column must have an identifier or
  correlate mapping. Any other concept, an unknown or custom one, or a column
  with no such mapping (unmapped or measure-only), refuses, with one
  exception: a key column with no stored mapping at all takes its join
  partner's (the two key columns name one item; onboarding leaves a
  random-UUID surrogate key unmapped), and those copies pass the same checks.
  Each endpoint
  column must map exactly one concept, one its form's join records, never
  another concept on the list (``concept_mismatch``);
* the system's query path: every column is one of the runtime's own physical
  columns (its query tool reads only its own schema) and a system with a
  declared row exclusion (``row_exclusion``, which the query tool enforces and
  this read cannot carry) is refused. As an extra layer, a column named like
  personal data (``pii.column_looks_pii``) refuses too.

Every refusal is a fixed reply that reads no data. The read itself is one
read-only SELECT on a read-only session with a connect and a statement timeout,
the names composed as identifiers and the values bound. It returns the two
endpoint columns of every joined row that shares a value with a requested
item (one hop), so a caller can see when a value pairs with more than one.
As an extra layer, every returned value must match its column's measured
shape, the identifier grammar and the value-level redaction (``pii.redact_pii``
leaves it unchanged); a pair that fails is dropped and counted.

Generic: no dataset, system, table, column or concept literals.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .joinpath_rule_link import drop_if_rule_inactive
from .rule_status import rule_active

logger = logging.getLogger(__name__)

#: Reserved tool-call argument carrying one pairing request (a JSON string).
RESERVED_ARG = "_adt_pair_forms"
#: The one question the engine's pairing call carries (the call's shape).
PAIRING_QUESTION = "Engine request: the other identifier form of listed items."
#: The request and answer version.
VERSION = 2
#: The ``engine`` member every answer carries, so a caller tells it from prose.
ENGINE_TAG = "pair_forms"
MAX_VALUES = 32
MAX_REQUEST_CHARS = 4000
#: Most pairs one read returns; one more is fetched to tell a cut read.
MAX_PAIRS = 256
#: Most measured exact joins of this system read to find the form endpoints;
#: a read that returns more is refused (``endpoint_limit``), never cut.
MAX_ENDPOINT_JOINS = 200
#: Most concept mappings read for the four columns; more is refused (``concept_limit``).
MAX_CONCEPT_ROWS = 64
#: Mapping roles that make a column a key of its concept.
KEY_ROLES = frozenset({"identifier", "correlate"})
STATEMENT_TIMEOUT_MS = 10_000
CONNECT_TIMEOUT_S = 5
CONNECTION_TYPE = "PostgreSQL"

_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:#-]{0,127}")
_JOIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}")


def _reply(status: str, **extra) -> str:
    return json.dumps({"engine": ENGINE_TAG, "v": VERSION, "status": status, **extra},
                      separators=(",", ":"), ensure_ascii=True)


#: The fixed reply to a reserved argument on any other call shape (``runtime_asgi``).
SHAPE_REFUSAL = _reply("refused", reason="call_shape")


#: The query tool name each system's Mode A server registered in this
#: process (one served runtime per process), keyed by system id.
_QUERY_TOOLS: dict = {}


def register_query_tool(system_id: str, name: str) -> None:
    """Record the name ``system_agent`` registered for ``system_id``'s query
    tool: ``<tool_name_prefix>query_<safe id>``. The cloud entry point
    (``runtime_app._run_system_agent``) passes no prefix, so it is bare there
    and the gateway adds ``<target>___``; local mode
    (``AgentManager._start_local``) emulates that prefix, so it is prefixed
    there."""
    _QUERY_TOOLS[system_id] = name


def query_tool_name(system_id) -> str | None:
    """The one tool a pairing request may call: the query tool name this
    runtime registered for ``system_id`` (:func:`register_query_tool`), or
    None when it registered none (every pairing request is then refused)."""
    return _QUERY_TOOLS.get(system_id) if isinstance(system_id, str) else None

#: The pairing JoinPath a request names, read from this runtime's graph.
PAIRING_CYPHER = (
    "MATCH (jp:JoinPath) WHERE jp.id = $id AND jp.from_system = $sid "
    f"AND jp.to_system = $sid AND {rule_active('jp')} "
    + drop_if_rule_inactive()
    + "RETURN jp.from_table AS from_table, jp.from_field AS from_field, "
    "jp.to_table AS to_table, jp.to_field AS to_field, jp.method AS method, "
    "jp.basis AS basis LIMIT 2")

#: This system's measured exact joins to other systems (the form endpoints).
ENDPOINTS_CYPHER = (
    "MATCH (jp:JoinPath) WHERE jp.method = $exact AND "
    "((jp.from_system = $sid AND jp.to_system <> $sid) OR "
    f"(jp.to_system = $sid AND jp.from_system <> $sid)) AND {rule_active('jp')} "
    + drop_if_rule_inactive()
    + "RETURN jp.from_system AS from_system, jp.from_table AS from_table, "
    "jp.from_field AS from_field, jp.to_system AS to_system, jp.to_table AS to_table, "
    "jp.to_field AS to_field, jp.transform AS transform, "
    "coalesce(jp.provenance, jp.origin_context) AS provenance "
    f"ORDER BY jp.id LIMIT {MAX_ENDPOINT_JOINS + 1}")

#: The concept mappings onboarding stored for the given columns of this
#: system (``Field.id`` is ``<system id>.<table>.<column>``).
CONCEPTS_CYPHER = (
    "MATCH (f:Field)-[r:MAPS_TO]->(c:Concept) WHERE f.system_id = $sid "
    "AND toLower(f.id) IN $fields "
    "RETURN toLower(f.id) AS field, c.id AS concept, r.role AS role, "
    "coalesce(c.retired, false) AS retired "
    f"ORDER BY field, concept LIMIT {MAX_CONCEPT_ROWS + 1}")


@dataclass(frozen=True)
class Request:
    """One pairing request: the stored JoinPath id and the values."""
    join: str
    values: tuple


@dataclass(frozen=True)
class Endpoint:
    """One measured form endpoint: a table, its column (lowercased), the
    measured shape, the concept the measured exact join agrees on, the
    catalog-related concept its label names (the form's own concept when the
    join spans a related pair), and the column as the graph spells it."""
    table: str
    column: str
    shape: str
    concept: str = ""
    related: str = ""
    spelling: str = ""

    @property
    def name(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def concepts(self) -> tuple:
        """The concepts the form's exact join records: the agreed one and the
        related one, if any. The column maps exactly one of them."""
        return tuple(c for c in (self.concept, self.related) if c)


@dataclass(frozen=True)
class Plan:
    """What one read does: the two endpoints and the stored key columns."""
    a: Endpoint
    b: Endpoint
    key_a: str
    key_b: str


def request_arg(join: str, values) -> str:
    """The reserved argument's value for one request."""
    return json.dumps({"v": VERSION, "join": join, "values": list(values)},
                      separators=(",", ":"))


def parse_request(raw) -> Request | None:
    """The request ``raw`` holds (exactly the version, a JoinPath id and the
    values), or None when it is not one this runtime reads."""
    if not isinstance(raw, str) or len(raw) > MAX_REQUEST_CHARS:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"v", "join", "values"} or data["v"] != VERSION:
        return None
    join, values = data["join"], data["values"]
    if (not isinstance(join, str) or not _JOIN_ID.fullmatch(join)
            or not isinstance(values, list) or not 0 < len(values) <= MAX_VALUES
            or not all(isinstance(v, str) and _VALUE.fullmatch(v) for v in values)):
        return None
    return Request(join, tuple(dict.fromkeys(values)))


def _end(table, field) -> tuple | None:
    """``(table, column)`` of one stored JoinPath end, schema dropped, lowercased."""
    text = str(field or "")
    if "." not in text and table:
        text = f"{table}.{text}"
    parts = [p for p in text.replace('"', "").split(".") if p]
    if len(parts) < 2 or not all(_NAME.fullmatch(p) for p in parts[-2:]):
        return None
    return parts[-2].lower(), parts[-1].lower()


def _spelling(field) -> str:
    """The column of one stored JoinPath end as the graph spells it (case kept)."""
    return str(field or "").replace('"', "").split(".")[-1]


def pairing_keys(rows) -> tuple | None:
    """``((table, key), (table, key))`` of the stored pairing JoinPath
    ``rows`` holds: exactly one row, a per-item pairing, two different tables."""
    from .identifier_translation import pairs_per_item
    if not isinstance(rows, list) or len(rows) != 1 or not pairs_per_item(rows[0]):
        return None
    ends = (_end(rows[0].get("from_table"), rows[0].get("from_field")),
            _end(rows[0].get("to_table"), rows[0].get("to_field")))
    if None in ends or ends[0][0] == ends[1][0]:
        return None
    return ends


def _exact_endpoint(row, system_id: str) -> tuple | None:
    """``((table, column), shape, peer, (concept, related), spelling)`` of
    this system's end of one measured exact join, or None (not the exact
    pass's, no shape)."""
    from .identity_affinity import (
        EXACT_TRANSFORM,
        exact_concepts_from_reasoning,
        exact_shape_from_reasoning,
    )
    if not isinstance(row, dict) or row.get("transform") not in (None, "", EXACT_TRANSFORM):
        return None
    ends = (("from_system", "from_table", "from_field"), ("to_system", "to_table", "to_field"))
    own = [e for e in ends if row.get(e[0]) == system_id]
    other = [e for e in ends if row.get(e[0]) not in (None, "", system_id)]
    shape = exact_shape_from_reasoning(row.get("provenance"))
    if len(own) != 1 or len(other) != 1 or not shape:
        return None
    end = _end(row.get(own[0][1]), row.get(own[0][2]))
    if end is None:
        return None
    concepts = exact_concepts_from_reasoning(row.get("provenance")) or ("", "")
    return end, shape, row.get(other[0][0]), concepts, _spelling(row.get(own[0][2]))


def _endpoint(entry, shapes: dict, concept: str) -> Endpoint:
    (table, column), related, spelling = entry
    return Endpoint(table, column, shapes[(table, column)], concept, related, spelling)


def form_endpoints(rows, system_id: str, tables: tuple) -> dict | str:
    """``{(Endpoint, Endpoint)}`` keyed by the column pair: for each other
    system and agreed concept, the one measured column on each of ``tables``
    (one label and one spelling). A read past :data:`MAX_ENDPOINT_JOINS` rows
    is ``"endpoint_limit"`` (a row past the cap could hide a second column);
    a column read with two shapes, or one column pair whose joins record two
    different form concepts, is ``"ambiguous_form_endpoints"``."""
    rows = list(rows or [])
    if len(rows) > MAX_ENDPOINT_JOINS:
        return "endpoint_limit"
    groups: dict = {}
    shapes: dict = {}
    for row in rows:
        found = _exact_endpoint(row, system_id)
        if found is None or found[0][0] not in tables:
            continue
        end, shape, peer, (concept, related), spelling = found
        if shapes.setdefault(end, shape) != shape:
            return "ambiguous_form_endpoints"
        groups.setdefault((peer, concept), {}).setdefault(end[0], set()).add(
            (end, related, spelling))
    out: dict = {}
    for (_peer, concept), by_table in groups.items():
        a, b = (by_table.get(t, set()) for t in tables)
        if len(a) == 1 and len(b) == 1:
            pair = (_endpoint(next(iter(a)), shapes, concept),
                    _endpoint(next(iter(b)), shapes, concept))
            if pair[0].shape == pair[1].shape:
                continue
            if out.setdefault((pair[0].column, pair[1].column), pair) != pair:
                return "ambiguous_form_endpoints"
    return out


def choose(candidates: dict, values) -> tuple | str:
    """The one endpoint pair whose two shapes cover every value, else a reason."""
    from .identity_affinity import literal_shape
    if not candidates:
        return "no_form_endpoints"
    fits = [pair for pair in candidates.values()
            if all(literal_shape(v) in (pair[0].shape, pair[1].shape) for v in values)]
    if not fits:
        return "value_shape"
    return fits[0] if len(fits) == 1 else "ambiguous_form_endpoints"


def column_policy_ok(physical, system_id: str, columns) -> bool:
    """The query path's column scope: every ``(table, column)`` is one of the
    runtime's own physical columns and the system declares no row exclusion;
    as an extra layer, no column is named like personal data."""
    from .pii import column_looks_pii
    from .row_exclusion import has_exclusions
    if has_exclusions(system_id):
        return False
    for table, column in columns:
        entry = physical.get(table) if isinstance(physical, dict) else None
        known = entry.get("columns") if isinstance(entry, dict) else None
        if not isinstance(known, dict) or column not in known or column_looks_pii(column):
            return False
    return True


def _field_id(system_id: str, table: str, column: str) -> str:
    """The id onboarding gives a column's ``Field`` node, lowercased."""
    return f"{system_id}.{table}.{column}".lower()


def concept_refusal(concept) -> str | None:
    """None for a concept the crossing may carry (``catalog.crossable_concept``),
    else why not: ``person_concept`` (``catalog.PERSON_CONCEPTS``),
    ``concept_not_allowed`` (a core concept off the allowlist) or
    ``concept_unknown`` (an unknown or custom concept)."""
    from .catalog import CORE_CONCEPT_DEFINITIONS, PERSON_CONCEPTS, crossable_concept
    if not isinstance(concept, str) or not concept:
        return "concept_unknown"
    if concept in PERSON_CONCEPTS:
        return "person_concept"
    if crossable_concept(concept):
        return None
    return "concept_not_allowed" if concept in CORE_CONCEPT_DEFINITIONS else "concept_unknown"


def _live_mappings(rows) -> dict | str:
    """``{field id: [row]}`` of the live mappings, or a reason: a malformed
    row is ``concept_unknown`` and a person concept refuses even when retired."""
    from .catalog import PERSON_CONCEPTS
    by_field: dict = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("concept"), str):
            return "concept_unknown"
        if row["concept"] in PERSON_CONCEPTS:
            return "person_concept"
        if row.get("retired") is not True:
            by_field.setdefault(str(row.get("field") or "").lower(), []).append(row)
    return by_field


def _form_refusal(endpoint: Endpoint) -> str | None:
    """None when every concept the endpoint's exact join records may be
    crossed (:func:`concept_refusal`) and a catalog-related label names a pair
    the catalog declares (``catalog.RELATED_IDENTIFIER_CONCEPTS``)."""
    from .catalog import RELATED_IDENTIFIER_CONCEPTS
    labelled = (endpoint.concept, endpoint.related) if endpoint.related else (endpoint.concept,)
    refused = next(filter(None, map(concept_refusal, labelled)), None)
    if refused is None and endpoint.related and (
            (endpoint.related, endpoint.concept) not in RELATED_IDENTIFIER_CONCEPTS):
        return "concept_unknown"
    return refused


def _column_refusal(mapped: list) -> str | None:
    """None when a column's live mappings may be crossed and one has a key
    role; a column with none, or only a measure one, is ``concept_unknown``."""
    if not mapped:
        return "concept_unknown"
    refused = next(filter(None, (concept_refusal(r["concept"]) for r in mapped)), None)
    if refused or any(r.get("role") in KEY_ROLES for r in mapped):
        return refused
    return "concept_unknown"


def _key_mappings(rows: list, by_field: dict, system_id: str, keys, endpoints) -> dict:
    """``by_field`` with an unmapped key column given its join partner's live
    mappings. The two key columns of the stored per-item pairing name one
    item, and onboarding leaves a random-UUID surrogate key unmapped, so one
    side often has none (a fresh workshop seat, 2026-11-11, left one MES
    table's surrogate key unmapped and every pairing read refused). Only a
    key column with no stored row at all inherits, and only when its partner
    has live mappings; the copies then pass the same checks as the partner's
    own rows. A column the read returns (a form endpoint) never inherits,
    even when it is also a key column: it must map its own form concept."""
    ids = [_field_id(system_id, t, c) for t, c in keys]
    returned = {_field_id(system_id, e.table, e.column) for e in endpoints}
    stored = {str(r.get("field") or "").lower() for r in rows if isinstance(r, dict)}
    if len(ids) != 2 or ids[0] == ids[1]:
        return by_field
    for dst, src in ((ids[0], ids[1]), (ids[1], ids[0])):
        if dst not in stored and dst not in returned and by_field.get(src):
            return {**by_field, dst: [{**r, "field": dst} for r in by_field[src]]}
    return by_field


def concept_check(rows, system_id: str, endpoints, keys) -> str | None:
    """None when every concept the endpoints' exact joins record
    (:func:`_form_refusal`) and every live stored concept of the endpoint and
    key columns may be crossed, each column has an identifier or correlate
    mapping (:func:`_column_refusal`; an unmapped key column takes its join
    partner's, :func:`_key_mappings`), and each endpoint column maps exactly
    one concept, one its form's join records (else ``concept_mismatch``);
    else a reason code. ``rows`` are :data:`CONCEPTS_CYPHER` rows; ``keys``
    are the ``(table, column)`` key columns."""
    rows = list(rows or [])
    if len(rows) > MAX_CONCEPT_ROWS:
        return "concept_limit"
    refused = next(filter(None, map(_form_refusal, endpoints)), None)
    by_field = _live_mappings(rows) if refused is None else refused
    if isinstance(by_field, str):
        return by_field
    by_field = _key_mappings(rows, by_field, system_id, keys, endpoints)
    columns = (*((e.table, e.column) for e in endpoints), *keys)
    for table, column in columns:
        refused = _column_refusal(by_field.get(_field_id(system_id, table, column), []))
        if refused:
            return refused
    for endpoint in endpoints:
        own = {r["concept"] for r in by_field[_field_id(system_id, endpoint.table,
                                                        endpoint.column)]}
        if len(own) != 1 or not own <= set(endpoint.concepts):
            return "concept_mismatch"
    return None


def person_named(endpoints, pairing_row: dict, columns) -> bool:
    """Whether any read column, as the graph spells it (the endpoints' and the
    pairing JoinPath's stored fields) or as it is read, is named like a
    person-valued field (``catalog.person_valued_field``)."""
    from .catalog import person_valued_field
    spelled = (*(e.spelling for e in endpoints),
               *(_spelling(pairing_row.get(f)) for f in ("from_field", "to_field")))
    return any(map(person_valued_field, (*spelled, *(c for _t, c in columns))))


def plan(request: Request, physical, read_graph, system_id: str) -> Plan | str:
    """The read for ``request`` from this runtime's own graph and schema
    (see the module docstring), else a refusal reason code."""
    from .identity_affinity import EXACT_METHOD
    pairing = read_graph(PAIRING_CYPHER, {"id": request.join, "sid": system_id})
    keys = pairing_keys(pairing)
    if keys is None:
        return "no_stored_pairing"
    (table_a, key_a), (table_b, key_b) = keys
    candidates = form_endpoints(
        read_graph(ENDPOINTS_CYPHER, {"sid": system_id, "exact": EXACT_METHOD}),
        system_id, (table_a, table_b))
    if isinstance(candidates, str):
        return candidates
    chosen = choose(candidates, request.values)
    if isinstance(chosen, str):
        return chosen
    a, b = chosen
    columns = ((a.table, a.column), (b.table, b.column), (table_a, key_a), (table_b, key_b))
    if not column_policy_ok(physical, system_id, columns):
        return "column_policy"
    if person_named((a, b), pairing[0], columns):
        return "person_field"
    fields = sorted({_field_id(system_id, t, c) for t, c in columns})
    refused = concept_check(read_graph(CONCEPTS_CYPHER, {"sid": system_id, "fields": fields}),
                            system_id, (a, b), columns[2:])
    return refused or Plan(a, b, key_a, key_b)


def statement(plan_: Plan):
    """The read-only SELECT (``pgdriver.sql`` composition: every name an
    Identifier; both value lists and the limit bound parameters). It returns
    every joined row that shares a value with a row of a requested value."""
    from .pgdriver import sql as psql
    names = {"ta": psql.Identifier(plan_.a.table), "tb": psql.Identifier(plan_.b.table),
             "ca": psql.Identifier(plan_.a.column), "cb": psql.Identifier(plan_.b.column),
             "ka": psql.Identifier(plan_.key_a), "kb": psql.Identifier(plan_.key_b)}
    return psql.SQL(
        "WITH m AS (SELECT a.{ca} AS x, b.{cb} AS y FROM {ta} AS a JOIN {tb} AS b "
        "ON a.{ka} = b.{kb} WHERE CAST(a.{ca} AS TEXT) = ANY(%s) "
        "OR CAST(b.{cb} AS TEXT) = ANY(%s)) "
        "SELECT DISTINCT CAST(a.{ca} AS TEXT), CAST(b.{cb} AS TEXT) FROM {ta} AS a "
        "JOIN {tb} AS b ON a.{ka} = b.{kb} "
        "WHERE (a.{ca} IN (SELECT x FROM m) OR b.{cb} IN (SELECT y FROM m)) "
        "AND a.{ca} IS NOT NULL AND b.{cb} IS NOT NULL LIMIT %s").format(**names)


def connect(conn_params: dict):
    """A read-only, autocommit connection with the connect timeout."""
    from . import pgdriver
    conn = pgdriver.connect(**{**conn_params, "connect_timeout": CONNECT_TIMEOUT_S})
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _execute(schema: str, conn_params: dict, query, values_a: list, values_b: list) -> list:
    """The rows of one read, scoped to ``schema``, under the statement timeout."""
    from .db import STATEMENT_TIMEOUT_SQL, search_path_sql
    conn = connect(conn_params)
    try:
        cursor = conn.cursor()
        cursor.execute(STATEMENT_TIMEOUT_SQL, (str(STATEMENT_TIMEOUT_MS),))
        cursor.execute(search_path_sql(schema))
        cursor.execute(query, (values_a, values_b, MAX_PAIRS + 1))
        return list(cursor.fetchall())
    finally:
        conn.close()


def safe_value(value, shape: str) -> bool:
    """A returned value the reply may carry: the identifier grammar, the
    column's measured shape, and unchanged by the personal-data redaction."""
    from .identity_affinity import literal_shape
    from .pii import redact_pii
    return (isinstance(value, str) and bool(_VALUE.fullmatch(value))
            and literal_shape(value) == shape and redact_pii(value) == value)


def checked_pairs(rows, plan_: Plan) -> tuple:
    """``(pairs, dropped, cut)``: the distinct pairs whose two values are
    both safe for their column (:func:`safe_value`), how many were dropped,
    and whether the read returned more than :data:`MAX_PAIRS`."""
    found: dict = {}
    dropped = 0
    for row in rows[:MAX_PAIRS]:
        a, b = (row[0], row[1]) if isinstance(row, (list, tuple)) and len(row) >= 2 else (None, None)
        if safe_value(a, plan_.a.shape) and safe_value(b, plan_.b.shape):
            found.setdefault((a, b), None)
        else:
            dropped += 1
    return [list(pair) for pair in found], dropped, len(rows) > MAX_PAIRS


def answer(raw, *, system_id: str, connection_type: str, schema: str, physical,
           conn_params=None, read_graph=None) -> str:
    """The JSON answer to one pairing request (see the module docstring).
    ``conn_params`` is a callable returning the connection parameters (called
    only for a request that is read); ``read_graph`` the graph read. Never raises."""
    from .identity_affinity import literal_shape
    if connection_type != CONNECTION_TYPE:
        return _reply("unsupported")
    request = parse_request(raw)
    if request is None:
        return _reply("refused", reason="bad_request")
    try:
        planned = plan(request, physical, read_graph or _graph_read, system_id)
        if isinstance(planned, str):
            logger.info("FORM_PAIRING refused system=%s reason=%s", system_id, planned)
            return _reply("refused", reason=planned)
        values_a = [v for v in request.values if literal_shape(v) == planned.a.shape]
        values_b = [v for v in request.values if literal_shape(v) == planned.b.shape]
        params = conn_params() if callable(conn_params) else conn_params
        rows = _execute(schema, params or {}, statement(planned), values_a, values_b)
    except Exception as exc:  # noqa: BLE001 - a failed read is reported, never raised
        logger.warning("FORM_PAIRING read failed system=%s (%s)", system_id, type(exc).__name__)
        return _reply("failed", error=type(exc).__name__)
    pairs, dropped, cut = checked_pairs(rows, planned)
    logger.info("FORM_PAIRING system=%s values=%d pairs=%d dropped=%d cut=%s", system_id,
                len(request.values), len(pairs), dropped, cut)
    return _reply("ok", fields=[planned.a.name, planned.b.name], pairs=pairs, cut=cut,
                  dropped=dropped)


def _graph_read(cypher: str, params: dict) -> list:
    """This runtime's own graph read (``live_concept_mappings``: short deadlines, one attempt)."""
    from .live_concept_mappings import _graph_read as read
    return read(cypher, params)
