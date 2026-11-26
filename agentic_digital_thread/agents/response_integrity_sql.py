# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fail-closed SQL projection provenance for deterministic evidence capture."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from .athena_scope import Token, tokenize

PhysicalSchema = Mapping[str, Mapping[str, object]]
_RESERVED_PREFIXES = (
    "__adt_source_time__", "__adt_source_start__", "__adt_source_end__",
)
_TIMESTAMP_TYPES = ("timestamp", "datetime", "date", "time")


@dataclass(frozen=True)
class DirectProjection:
    output_name: str
    table: str
    column: str
    data_type: str
    # "" = direct column; else the DB-computed aggregate ("count"/"sum"/"avg"/
    # "min"/"max"). Aggregates over a proven base relation are deterministic
    # server-owned values — the DATABASE computed them, not the model.
    aggregate: str = ""

    @property
    def is_reserved_time(self) -> bool:
        return self.output_name.lower().startswith(_RESERVED_PREFIXES)


_AGGREGATES = {"count", "sum", "avg", "min", "max"}
_AGGREGATE_TYPES = {"count": "bigint", "sum": "numeric", "avg": "numeric"}


@dataclass(frozen=True)
class ProjectionProof:
    projections: tuple[DirectProjection, ...]
    valid: bool
    reason: str = ""

    def by_output(self) -> dict[str, DirectProjection]:
        return {item.output_name.lower(): item for item in self.projections}


_TABLE_DESC_RE = re.compile(
    r"(?:^|\.\s*)([A-Za-z_][\w$]*)(?:\s*\[([^\]]+)\])?\s*\((.*?)\)(?=\s*\.|\s*$)"
)
_COLUMN_RE = re.compile(r"^\s*([A-Za-z_][\w$]*)\s*:\s*(.+?)\s*$")
# The samples-enriched render (schema_format.format_schema_with_samples) appends
# capped example values per column: ``name: type e.g. v1, v2`` — strip them so
# the declared type survives. Values may contain commas, so when the column list
# is ``;``-separated (the samples renderer always is), split on ``;`` instead.
_SAMPLE_SUFFIX_RE = re.compile(r"\s+e\.g\.\s.*$", re.DOTALL)


def parse_physical_schema(tables_desc: str) -> dict[str, dict[str, object]]:
    """Parse server-owned relation kind plus direct physical columns.

    Accepts BOTH tables_desc renders that provisioning persists:
    ``tbl [kind](c1: t1, c2: t2)`` (columns-only) and
    ``tbl [kind](c1: t1 e.g. v1, v2; c2: t2)`` (samples-enriched, ``;``-separated).
    """
    schema: dict[str, dict[str, object]] = {}
    for match in _TABLE_DESC_RE.finditer(tables_desc or ""):
        columns: dict[str, str] = {}
        body = match.group(3)
        parts = body.split(";") if ";" in body else body.split(",")
        for raw in parts:
            column = _COLUMN_RE.match(raw)
            if column:
                dtype = _SAMPLE_SUFFIX_RE.sub("", column.group(2)).strip().lower()
                columns[column.group(1).lower()] = dtype
        if columns:
            schema[match.group(1).lower()] = {
                "relation_kind": (match.group(2) or "unknown").strip().lower().replace(" ", "_"),
                "columns": columns,
            }
    return schema


def _relation_schema(schema: PhysicalSchema, table: str) -> tuple[str, Mapping[str, str]]:
    raw = schema[table]
    nested = raw.get("columns") if isinstance(raw, Mapping) else None
    if isinstance(nested, Mapping):
        kind = str(raw.get("relation_kind") or "unknown").lower().replace(" ", "_")
        columns = {str(key).lower(): str(value) for key, value in nested.items()}
        return kind, columns
    return "unknown", {str(key).lower(): str(value) for key, value in raw.items()}


def _has_multiple_relations(tokens: list[Token]) -> bool:
    return any(
        token.depth == 0 and (
            token.text == "," or _upper(token) in {
                "JOIN", "CROSS", "UNION", "INTERSECT", "EXCEPT",
            }
        )
        for token in tokens
    )


def _upper(token: Token) -> str:
    return token.value.upper() if token.kind == "word" else token.value


def _top_level_bounds(tokens: list[Token]) -> tuple[int, int] | None:
    select = [i for i, token in enumerate(tokens) if token.depth == 0 and _upper(token) == "SELECT"]
    if len(select) != 1 or select[0] != 0:
        return None
    start = select[0] + 1
    end = next((i for i in range(start, len(tokens))
                if tokens[i].depth == 0 and _upper(tokens[i]) == "FROM"), -1)
    return (start, end) if end > start else None


def _split_items(tokens: list[Token], start: int, end: int) -> list[list[Token]] | None:
    items: list[list[Token]] = []
    item_start = start
    for index in range(start, end):
        if tokens[index].depth == 0 and tokens[index].text == ",":
            if index == item_start:
                return None
            items.append(tokens[item_start:index])
            item_start = index + 1
    if item_start == end:
        return None
    items.append(tokens[item_start:end])
    return items


def _relation(tokens: list[Token], from_index: int, schema: PhysicalSchema):
    tail = tokens[from_index + 1:]
    if not tail or tail[0].kind not in {"word", "identifier"}:
        return None
    clauses = {"WHERE", "ORDER", "LIMIT", "GROUP", "HAVING", "OFFSET"}
    relation_end = next(
        (i for i, token in enumerate(tail) if token.depth == 0 and _upper(token) in clauses),
        len(tail),
    )
    relation_tokens, remainder = tail[:relation_end], tail[relation_end:]
    if _has_multiple_relations(relation_tokens):
        return ("", "", "multiple_relations")
    forbidden = {"FROM", "JOIN", "CROSS", "UNION", "INTERSECT", "EXCEPT"}
    if any(_upper(token) in forbidden for token in remainder):
        return ("", "", "multiple_relations")
    if any(token.depth != 0 for token in relation_tokens):
        return None
    index = 0
    parts = [relation_tokens[index].value]
    while index + 2 < len(relation_tokens) and relation_tokens[index + 1].text == ".":
        if relation_tokens[index + 2].kind not in {"word", "identifier"}:
            return None
        parts.append(relation_tokens[index + 2].value)
        index += 2
    table = parts[-1].lower()
    index += 1
    alias = table
    if index < len(relation_tokens) and _upper(relation_tokens[index]) == "AS":
        index += 1
    if index < len(relation_tokens) and relation_tokens[index].kind in {"word", "identifier"}:
        alias, index = relation_tokens[index].value.lower(), index + 1
    if index != len(relation_tokens) or table not in schema:
        return None
    relation_kind, _columns = _relation_schema(schema, table)
    if relation_kind not in {"base_table", "base"}:
        return (table, alias, "non_base_relation")
    return table, alias, ""


def _aggregate_item(item: list[Token], table: str, alias: str, schema: PhysicalSchema):
    """Admit ``agg(column)`` / ``count(*)`` / ``count(distinct column)`` items.

    The aggregate is computed by the DATABASE over the already-proven single
    base relation, so the value is deterministic server-owned evidence. Only
    one bare physical column (or ``*``/``distinct column`` for count) may
    appear inside the parens — expressions stay rejected.
    """
    if len(item) < 3 or item[0].kind != "word" or item[0].value.lower() not in _AGGREGATES:
        return None
    func = item[0].value.lower()
    if item[1].text != "(" or item[-1].text != ")":
        return None
    inner = item[2:-1]
    if func == "count" and len(inner) == 1 and inner[0].text == "*":
        return DirectProjection(func, table, "*", _AGGREGATE_TYPES[func], aggregate=func)
    if func == "count" and len(inner) == 2 and _upper(inner[0]) == "DISTINCT":
        inner = inner[1:]
    if len(inner) == 3 and inner[1].text == ".":
        if inner[0].value.lower() not in {alias, table}:
            return None
        inner = inner[2:3]
    if len(inner) != 1 or inner[0].kind not in {"word", "identifier"}:
        return None
    column = inner[0].value.lower()
    _kind, columns = _relation_schema(schema, table)
    if column not in columns:
        return None
    data_type = _AGGREGATE_TYPES.get(func, columns[column])
    return DirectProjection(f"{func}_{column}", table, column, data_type, aggregate=func)


def _direct_item(item: list[Token], table: str, alias: str, schema: PhysicalSchema):
    if not item or any(token.kind in {"literal", "number", "bind"} for token in item):
        return None
    output = None
    if len(item) >= 3 and _upper(item[-2]) == "AS":
        output, item = item[-1].value, item[:-2]
    elif len(item) in {2, 4} and item[-1].kind in {"word", "identifier"}:
        output, item = item[-1].value, item[:-1]
    elif (len(item) >= 5 and item[-1].kind in {"word", "identifier"}
          and item[-2].text == ")"):
        # bare alias after an aggregate: ``count(x) cnt``
        output, item = item[-1].value, item[:-1]
    aggregated = _aggregate_item(item, table, alias, schema)
    if aggregated is not None:
        return DirectProjection(
            output or aggregated.output_name, aggregated.table,
            aggregated.column, aggregated.data_type, aggregated.aggregate,
        )
    if len(item) == 1 and item[0].kind in {"word", "identifier"}:
        column = item[0].value.lower()
    elif len(item) == 3 and item[1].text == ".":
        if item[0].value.lower() not in {alias, table}:
            return None
        column = item[2].value.lower()
    else:
        return None
    _kind, columns = _relation_schema(schema, table)
    if column not in columns:
        return None
    return DirectProjection(output or column, table, column, columns[column])


def validate_direct_projections(sql: str, schema: PhysicalSchema) -> ProjectionProof:
    """Prove every output is one unambiguous direct physical-column projection."""
    tokens, invalid = tokenize(sql or "")
    if invalid or any(token.kind == "semicolon" for token in tokens):
        return ProjectionProof((), False, "invalid_sql")
    bounds = _top_level_bounds(tokens)
    if bounds is None:
        return ProjectionProof((), False, "unsupported_query_shape")
    relation = _relation(tokens, bounds[1], schema)
    items = _split_items(tokens, *bounds)
    if relation is None or items is None:
        return ProjectionProof((), False, "unsupported_query_shape")
    table, alias, relation_reason = relation
    if relation_reason:
        return ProjectionProof((), False, relation_reason)
    projections = tuple(_direct_item(item, table, alias, schema) for item in items)
    if any(item is None for item in projections):
        return ProjectionProof((), False, "non_physical_projection")
    direct = tuple(item for item in projections if item is not None)
    aliases = [item.output_name.lower() for item in direct]
    if len(aliases) != len(set(aliases)):
        return ProjectionProof((), False, "duplicate_output_alias")
    for item in direct:
        if item.is_reserved_time and not any(kind in item.data_type for kind in _TIMESTAMP_TYPES):  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
            return ProjectionProof((), False, "reserved_alias_not_timestamp")
    return ProjectionProof(direct, True)
