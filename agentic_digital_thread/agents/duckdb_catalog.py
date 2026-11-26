# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Catalog facts for the S3 Tables (DuckDB) window rules, without Athena.

The three catalog window rules (storage window, state time, record time) read
the same shaped facts whatever the engine: ``(table, bounded columns, storage
columns, column types, iceberg flag)`` plus a flattened column-type map for the
record-own-time check. :mod:`agents.athena_tools` builds those from an Athena
client; this module builds them from DuckDB metadata (``DESCRIBE`` through the
connector's own reader) and the baked schema registries (:mod:`storage_bounds`),
so the guard runs the same five rules on an S3 Tables system (ADR-0099, scope 2).

Relation names in DuckDB SQL are ``s3t.<namespace>.<table>``. The rule modules
key tables by terminal name (``rpartition('.')[2]``), so the facts carry the
dotted relation as ``table`` and the registries resolve the terminal name the
same way they do for Athena's ``database.table``.

Dataset-neutral: nothing here knows a table or column name; it reads whatever
the bucket's Iceberg catalog and the baked schema hold.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .duckdb_tools import CATALOG
from .storage_bound_rule import StorageBound, StorageBoundError

logger = logging.getLogger(__name__)


class CatalogFactsUnavailable(StorageBoundError):
    """The S3 Tables catalog facts a window rule needs could not be read (a
    ``DESCRIBE`` failed) for a GENUINE read outage -- an I/O error, credentials
    or a timeout, not a nonexistent table. A :class:`StorageBoundError` so it
    rides the guard's refusal path and surfaces a RETRYABLE
    ``catalog_facts_unavailable`` result, rather than letting the rule run with
    no temporal types."""

    def __init__(self, detail: str = ""):
        super().__init__(StorageBound("", (), (), ()), (), ())
        self._detail = detail

    def to_dict(self) -> dict:
        return {
            "error": "catalog_facts_unavailable",
            "error_type": "unavailable",
            "error_name": "CATALOG_FACTS_UNAVAILABLE",
            "reason": "catalog_facts_unavailable",
            "retryable": True,
            "guidance": (
                "Refused before running: this S3 Tables system's catalog metadata could not be "
                "read, so the storage, state-time and record-time window rules cannot be checked. "
                "This is transient; try the query again in a moment."),
        }


class UnknownTable(StorageBoundError):
    """The query referenced a table or namespace that does not exist (a DuckDB
    CatalogException). NON-retryable: retrying the same wrong name can never
    succeed. Names the table the query used and lists the system's real
    qualified tables so the agent can correct the name. Never leaks a path or
    ARN. A :class:`StorageBoundError` so it rides the guard's refusal path."""

    def __init__(self, queried: str = "", real_tables=None):
        super().__init__(StorageBound("", (), (), ()), (), ())
        self._queried = str(queried or "")
        self._real = [str(t) for t in (real_tables or [])]

    def to_dict(self) -> dict:
        from .duckdb_tools import safe_relation_name
        # Only safe SQL identifiers reach the public message: a quoted relation
        # carrying path/ARN/URL characters is withheld, never echoed. The baked
        # names are our own render, but run them through the same allowlist.
        safe_real = [r for r in (safe_relation_name(t) for t in self._real) if r]
        names = ", ".join(safe_real) if safe_real else "none are baked"
        safe_queried = safe_relation_name(self._queried)
        where = f" It referenced {safe_queried}." if safe_queried else ""
        return {
            "error": "unknown_table",
            "error_type": "invalid",
            "error_name": "UNKNOWN_TABLE",
            "reason": "unknown_table",
            "retryable": False,
            "guidance": (
                "Refused before running: the query named a table or namespace this S3 Tables "
                f"system does not have.{where} Its tables are: {names}. Use one of those exact "
                "names (s3t.<namespace>.<table>)."),
        }


#: A struct field head inside a DuckDB ``STRUCT(...)`` type, e.g. ``"ts" TIMESTAMP``
#: or ``defect_type VARCHAR``. Used only to flatten one level of struct leaves,
#: which is what the record-own-time check reads (sibling leaves of one column).
_STRUCT_FIELD_RE = re.compile(r'(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s+([A-Za-z].*)')


def _reltype_parts(sql: str, *, include_same_column_or: bool):
    """``(parts, bounded)`` for the one single-table read ``sql`` bounds, else
    ``None`` (several tables, none, or an uncertain parse). Reuses the engine
    neutral SQL reader the Athena path uses."""
    from .storage_bound_rule import bounded_columns
    read = bounded_columns(sql, include_same_column_or=include_same_column_or)
    if not read:
        return None
    return read


def _is_s3t_relation(parts: tuple) -> bool:
    """Whether a relation's name parts address the attached ``s3t`` catalog: a
    one or two part name (namespace[.table]) in the default catalog, or a three
    part name whose first part is ``s3t``. A name with a different catalog, or
    more than three parts, is not this system's and reads no facts."""
    if not parts or len(parts) > 3:
        return False
    if len(parts) == 3:
        return str(parts[0]).lower() == CATALOG
    return True


def _split_struct_fields(inner: str) -> list:
    """Split the body of a DuckDB ``STRUCT(...)`` into its top-level
    ``name type`` field strings, respecting nested parentheses and angle
    brackets so a nested STRUCT or list type is not split mid-way."""
    fields, depth, start = [], 0, 0
    for i, ch in enumerate(inner):
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
        elif ch == "," and depth == 0:
            fields.append(inner[start:i])
            start = i + 1
    tail = inner[start:]
    if tail.strip():
        fields.append(tail)
    return fields


def _flatten_struct(name: str, data_type: str, out: dict) -> None:
    """Record ``name`` and, when ``data_type`` is a ``STRUCT(...)``, each of its
    top-level leaves as ``name.<leaf>`` with the leaf's own type (one level,
    which is what the record-own-time check needs). Names are lowercased."""
    out[name.lower()] = data_type
    upper = data_type.strip().upper()
    if not upper.startswith("STRUCT(") or not upper.endswith(")"):
        return
    inner = data_type.strip()[len("STRUCT("):-1]
    for field in _split_struct_fields(inner):
        match = _STRUCT_FIELD_RE.match(field.strip())
        if match is None:
            continue
        leaf = match.group(1) or match.group(2)
        out[f"{name.lower()}.{leaf.lower()}"] = match.group(3).strip()


class DuckDBCatalog:
    """The DuckDB-side catalog facts provider for the window rules.

    ``reader`` is the connector's own ``_S3TablesReader`` (its ``columns``
    gives one table's ``ColumnInfo`` rows via a metadata-only ``DESCRIBE``);
    tests inject a fake with the same ``columns(namespace, table)`` method.
    ``source_system`` keys the baked storage registry read for partition
    sources, the same registry the Athena storage check reads."""

    def __init__(self, reader, *, source_system: str = "", real_tables=None, scope=None):
        self._reader = reader
        self._system = source_system
        self._real_tables = [str(t) for t in (real_tables or [])]
        # The connection's read scope (duckdb_tools.ScopeSpec). The live
        # listing fallback covers the whole bucket, so it is filtered to this
        # scope; None (no scope supplied) lists nothing rather than everything.
        self._scope = scope

    @staticmethod
    def runs_storage_bound_check() -> bool:
        """The guard must run the competing-storage-bound rule
        (``storage_bound_rule.decide``) for DuckDB, which has no connector-side
        check like Athena's ``athena_tools._storage_bound_check`` (review
        R4-F1). The Athena provider does not define this, so it stays False
        there and Athena keeps its single connector check (no double-run)."""
        return True

    def _column_types(self, namespace: str, table: str) -> dict:
        """``{column: type}`` for one table from a metadata-only DESCRIBE.

        A read FAILURE is NOT an empty type map: it raises
        :class:`CatalogFactsUnavailable` so the guard fails closed with a
        ``catalog_facts_unavailable`` refusal rather than run the window rules
        with no temporal types. An empty column list from a
        successful read (a table with no columns) is an honest ``{}``."""
        from .duckdb_tools import CATALOG, CatalogMissingRelation, _scope_allows
        if self._scope is not None and not _scope_allows(self._scope, namespace, table):
            # Outside the read scope: never DESCRIBE it. Answered like a
            # relation that does not exist, so its existence is not disclosed.
            # No catalog listing here either: only names already known in scope.
            queried = ".".join(p for p in (CATALOG, namespace, table) if p)
            raise UnknownTable(queried=queried,
                               real_tables=self._scoped_real_tables() or self._scope_names())
        try:
            cols = self._reader.columns(namespace, table)
        except CatalogMissingRelation:
            # The referenced table/namespace does not exist: NON-retryable.
            queried = ".".join(p for p in (CATALOG, namespace, table) if p)
            logger.info("duckdb catalog miss on a nonexistent relation")
            raise UnknownTable(queried=queried,
                               real_tables=self._scoped_real_tables()
                               or self._live_table_names()) from None
        except Exception as exc:  # a genuine read outage -> retryable
            logger.info("duckdb column read failed (%s)", type(exc).__name__)
            raise CatalogFactsUnavailable(
                f"column metadata unreadable ({type(exc).__name__})") from None
        return {str(c.name).lower(): str(c.type) for c in cols}

    def _scoped_real_tables(self) -> list:
        """The baked qualified names, kept to the read scope (all of them when
        no scope was supplied), so a refusal never names an out-of-scope table."""
        from .duckdb_tools import CATALOG, _scope_allows
        if self._scope is None:
            return list(self._real_tables)
        out: list = []
        for name in self._real_tables:
            parts = name.split(".")
            if (len(parts) == 3 and parts[0] == CATALOG
                    and _scope_allows(self._scope, parts[1], parts[2])):
                out.append(name)
        return out

    def _scope_names(self) -> list:
        """The configured scope's own ``s3t.<ns>.<table>`` pairs that the full
        scope (namespaces and tables together) allows; no catalog read."""
        from .duckdb_tools import CATALOG, _scope_allows
        return [f"{CATALOG}.{ns}.{tbl}" for ns, tbl in (self._scope or ())
                if _scope_allows(self._scope, ns, tbl)]

    def _live_table_names(self) -> list:
        """The system's real qualified ``s3t.<ns>.<table>`` names from the
        connector's catalog listing, the last-resort source for the
        unknown_table refusal when no baked desc or scope was supplied, kept
        to the connection's read scope (never another namespace's names). Best
        effort: a listing fault yields ``[]`` (the refusal then says none are
        baked) rather than failing the refusal."""
        from .duckdb_tools import CATALOG, _scope_allows
        if self._scope is None:
            return []
        try:
            rows = self._reader.list_tables()
        except Exception as exc:  # noqa: BLE001 - listing is best-effort
            logger.info("duckdb table listing for the refusal failed (%s)", type(exc).__name__)
            return []
        out: list = []
        for ns, tbl in rows or ():
            if not _scope_allows(self._scope, str(ns), str(tbl)):
                continue
            name = f"{CATALOG}.{ns}.{tbl}"
            if name not in out:
                out.append(name)
        return out

    def _namespace_table(self, parts: tuple):
        """``(namespace, table)`` for an in-``s3t`` relation's name parts: the
        last two parts (the namespace defaults to the configured scope's when
        a bare table name is written is out of scope here; a two or three part
        name carries its own)."""
        if len(parts) >= 2:
            return str(parts[-2]), str(parts[-1])
        return "", str(parts[-1])

    def storage_facts(self, sql: str, *, require_bound: bool = True,
                      include_same_column_or: bool = False):
        """``(table, bounded, storage, types, iceberg)`` for a single-table
        ``s3t`` read that bounds a column, else ``None`` (the same contract as
        :func:`athena_tools.storage_facts`). S3 Tables are always Iceberg.
        ``storage`` is the table's partition sources from the baked registry."""
        read = _reltype_parts(sql, include_same_column_or=include_same_column_or)
        if read is None:
            return None
        parts, bounded = read
        if (require_bound and not bounded) or not _is_s3t_relation(tuple(parts)):
            return None
        namespace, table = self._namespace_table(tuple(parts))
        types = self._column_types(namespace, table) if table else {}
        storage = self._baked_storage(table)
        return ".".join(parts), bounded, storage, types, True

    def _baked_storage(self, table: str) -> set:
        """The table's storage (partition source) columns from the baked
        schema registry, keyed by terminal name, lowercased; ``set()`` when the
        system baked none. Same source the Athena check reads for an Iceberg
        table whose partition sources the baked schema names."""
        from . import storage_bounds
        baked = {str(k).lower(): v
                 for k, v in storage_bounds.storage_map_for(self._system).items()}
        return {str(c).lower() for c in baked.get(table.lower(), ())}

    def flat_column_types(self, table: str) -> dict:
        """``{column: type}`` for one ``s3t`` table with one level of struct
        leaves flattened to dotted paths (the record-own-time check reads a
        struct's sibling leaves), ``{}`` when the table is unknown. ``table``
        is the dotted relation name ``storage_facts`` returned."""
        parts = tuple(p for p in str(table or "").split(".") if p)
        if not parts or not _is_s3t_relation(parts):
            return {}
        namespace, terminal = self._namespace_table(parts)
        if not terminal:
            return {}
        raw = self._column_types(namespace, terminal)
        flat: dict = {}
        for name, data_type in raw.items():
            _flatten_struct(name, data_type, flat)
        return flat
