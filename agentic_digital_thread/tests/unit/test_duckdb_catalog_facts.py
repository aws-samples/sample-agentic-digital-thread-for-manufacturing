# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DuckDB-side equivalent of ``athena_tools.storage_facts``: the catalog
facts the three catalog window rules read (table, bounded columns, storage
columns, column types, iceberg flag), built from DuckDB metadata (DESCRIBE)
and the baked schema registries, for an S3 Tables system (scope 2).

Relation names in DuckDB SQL are ``s3t.<namespace>.<table>``; the rule
modules key tables by terminal name, so the facts resolve the terminal name
for a 3-part name. Dataset-neutral: the fixture uses a namespace/table the
shipped seed does not use.

Offline: a fake column provider stands in for the live DESCRIBE; the baked
storage columns come from a rendered ``tables_desc`` through the same
``storage_bounds`` registry the Athena path reads.
"""
import agents.storage_bounds as storage_bounds
from agents.duckdb_catalog import DuckDBCatalog

SYS = "sys-neutral"


class _FakeReader:
    """Stands in for _S3TablesReader.columns: returns ColumnInfo-like rows."""

    def __init__(self, types):
        self._types = types

    def columns(self, namespace, table):
        return [type("ColumnInfo", (), {"name": n, "type": t})()
                for n, t in self._types.items()]


def _catalog(types):
    return DuckDBCatalog(_FakeReader(types), source_system=SYS)


def setup_function(_):
    storage_bounds.reset()


def teardown_function(_):
    storage_bounds.reset()


def test_storage_facts_resolves_terminal_name_for_three_part_relation():
    cat = _catalog({"observed_at": "TIMESTAMP", "reading_value": "DOUBLE"})
    facts = cat.storage_facts(
        "SELECT count(*) FROM s3t.sensors.readings "
        "WHERE observed_at >= TIMESTAMP '2026-10-05'",
        require_bound=True, include_same_column_or=True)
    assert facts is not None
    table, bounded, storage, types, iceberg = facts
    assert table.rpartition(".")[2] == "readings"
    assert "observed_at" in bounded
    assert iceberg is True
    assert types.get("observed_at") == "TIMESTAMP"


def test_storage_facts_reports_baked_partition_sources_as_storage_columns():
    # A baked tables_desc whose terminal table is hidden-partitioned on a
    # date derived from observed_at: the storage set must include observed_at.
    desc = "readings(observed_at: timestamp, reading_value: double) (hidden partitioning by observed_at_day)"
    storage_bounds.remember_tables_desc(SYS, desc)
    cat = _catalog({"observed_at": "TIMESTAMP", "reading_value": "DOUBLE"})
    facts = cat.storage_facts(
        "SELECT count(*) FROM s3t.sensors.readings "
        "WHERE observed_at >= TIMESTAMP '2026-10-05'",
        require_bound=True, include_same_column_or=True)
    _t, _b, storage, _ty, _ice = facts
    assert "observed_at" in storage


def test_storage_facts_none_for_a_non_s3t_catalog():
    cat = _catalog({"observed_at": "TIMESTAMP"})
    facts = cat.storage_facts(
        "SELECT count(*) FROM other.ns.readings WHERE observed_at >= TIMESTAMP '2026-10-05'",
        require_bound=True, include_same_column_or=True)
    assert facts is None


def test_storage_facts_none_for_a_multi_table_read():
    cat = _catalog({"observed_at": "TIMESTAMP"})
    facts = cat.storage_facts(
        "SELECT count(*) FROM s3t.sensors.readings a JOIN s3t.sensors.other b ON a.id=b.id "
        "WHERE a.observed_at >= TIMESTAMP '2026-10-05'",
        require_bound=False, include_same_column_or=True)
    assert facts is None


def test_flat_column_types_flattens_struct_leaves():
    cat = _catalog({"observed_at": "TIMESTAMP",
                    "quality": 'STRUCT("defect_type" VARCHAR, "ts" TIMESTAMP)'})
    flat = cat.flat_column_types("s3t.sensors.readings")
    assert flat.get("observed_at") == "TIMESTAMP"
    assert "quality.defect_type" in flat
    assert "quality.ts" in flat


def test_flat_column_types_raises_unavailable_when_describe_fails():
    # A DESCRIBE failure is NOT an empty type map: it fails closed so the
    # record-time rule does not run with no sibling types.
    import pytest

    from agents.duckdb_catalog import CatalogFactsUnavailable

    class _Boom:
        def columns(self, namespace, table):
            raise RuntimeError("describe failed")
    cat = DuckDBCatalog(_Boom(), source_system=SYS)
    with pytest.raises(CatalogFactsUnavailable):
        cat.flat_column_types("s3t.sensors.readings")
