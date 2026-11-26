# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read an S3 Tables table's Iceberg partition fields live, the way the storage
window rule needs them (follow-up to Task A risk 1).

DuckDB 2.0's ``iceberg_metadata(ref)`` returns one row per manifest ENTRY with
columns manifest_path, manifest_sequence_number, manifest_content, status,
content ('DATA'), file_path, file_format, record_count, data_sequence_number,
file_sequence_number. It has NO ``name`` column and NO partition-spec rows, so
the first implementation (``SELECT name ... WHERE content = 'partition'``) raised
a Binder Error live and returned ``[]``, leaving the baked hidden-partitioning
marker empty for a real partitioned table.

The partition fields are instead read from one manifest-entry ``file_path`` (one
row, metadata-only, no data scan): a Hive-style partitioned path carries the
field names as ``field=value`` segments, in spec order. That matches the shape
Athena's ``$partitions`` path returns (the field names as written, transform
suffix included, e.g. ``data_creation_date_day``), so ``storage_bound_rule
.field_sources`` maps them to source columns identically on either engine.

Dataset-neutral: the fixture path names are a neutral shape, not tied to a
column the shipped seed uses.
"""
import agents.duckdb_tools as d

NS, T = "ns_x", "table_x"
# One DATA manifest entry whose file_path is Hive-partitioned, exactly the shape
# DuckDB 2.0 iceberg_metadata returns (the real live columns, DATA content).
_FILE_PATH = ("s3://warehouse/data/region_code=EU/created_day=2026-11-09/"
              "00001-7-abc.parquet")


class _FakeReader:
    """Mimics DuckDB 2.0 iceberg_metadata: a `name`/partition-spec query raises
    a Binder Error (as it did live); a file_path query returns one DATA row."""

    def fetch(self, sql, params=(), max_rows=None):
        low = " ".join(sql.lower().split())
        if "iceberg_metadata" not in low:
            return []
        if "select file_path" not in low:
            # The original broken query selected a column that does not exist.
            raise RuntimeError(
                'Binder Error: Referenced column "name" not found in FROM clause! '
                'Candidate bindings: "manifest_path", "content", "file_path"')
        # The query projects file_path only, so one row of one column.
        return [[_FILE_PATH]]


def test_read_partition_fields_parses_hive_segments_from_file_path():
    out = d._read_partition_fields(_FakeReader(), NS, T)
    assert out == ["region_code", "created_day"]


def test_read_partition_fields_empty_for_an_unpartitioned_file_path():
    class _Flat(_FakeReader):
        def fetch(self, sql, params=(), max_rows=None):
            low = " ".join(sql.lower().split())
            if "select file_path" not in low:
                raise RuntimeError("Binder Error")
            return [["s3://warehouse/data/00001-7-abc.parquet"]]
    assert d._read_partition_fields(_Flat(), NS, T) == []


def test_read_partition_fields_unavailable_when_no_manifest_entries():
    # No data manifest entry -> the current spec cannot be established -> this
    # is UNAVAILABLE, not confirmed-unpartitioned.
    import pytest

    class _None(_FakeReader):
        def fetch(self, sql, params=(), max_rows=None):
            return []
    with pytest.raises(d.PartitionSpecUnavailable):
        d._read_partition_fields(_None(), NS, T)


def test_partition_fields_map_to_source_columns_like_athena():
    # storage_bound_rule.field_sources must map the transform-suffixed names the
    # same way it maps Athena's $partitions fields, given the table's columns.
    from agents.storage_bound_rule import field_sources
    fields = d._read_partition_fields(_FakeReader(), NS, T)
    column_types = {"region_code": "varchar", "created": "timestamp"}
    sources = field_sources(fields, column_types)
    assert "region_code" in sources          # identity transform
    assert "created" in sources               # created_day -> created (day transform)


def test_s3tables_partition_fields_unavailable_on_a_bad_table_name():
    import pytest
    with pytest.raises(d.PartitionSpecUnavailable):
        d.s3tables_partition_fields("no_dot", {"table_bucket_arn":
            "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"})


# --- F6: deterministic spec selection, data-root scope, URL-decode ----------

def test_warehouse_prefix_field_value_is_ignored():
    # A field=value segment in the prefix (before /data/) is NOT a partition.
    path = "s3://wh/tenant=corp/data/region_code=EU/created_day=2026-11-09/f.parquet"
    assert d._partition_fields_from_path(path) == ["region_code", "created_day"]


def test_value_containing_equals_is_split_on_the_first_equals():
    path = "s3://wh/data/region_code=a=b/created_day=2026-11-09/f.parquet"
    assert d._partition_fields_from_path(path) == ["region_code", "created_day"]


def test_url_encoded_field_name_is_decoded():
    path = "s3://wh/data/region%20code=EU/f.parquet"
    assert d._partition_fields_from_path(path) == ["region code"]


def test_no_data_root_returns_empty():
    assert d._partition_fields_from_path("s3://wh/region_code=EU/f.parquet") == []


def test_newest_entry_is_chosen_after_spec_evolution():
    # Two manifest entries: an OLD one (lower sequence) partitioned by region
    # only, a NEW one (higher sequence) partitioned by region + day. The query
    # orders by sequence DESC, so the fake returns the newest first.
    new = "s3://wh/data/region_code=EU/created_day=2026-11-11/new.parquet"

    class _Evolved(_FakeReader):
        def fetch(self, sql, params=(), max_rows=None):
            low = " ".join(sql.lower().split())
            assert "order by data_sequence_number desc" in low  # deterministic
            return [[new]]
    assert d._read_partition_fields(_Evolved(), NS, T) == ["region_code", "created_day"]


# --- F7: s3tables_partition_fields closes its handle on every path ----------

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"


class _TrackingHandle:
    instances = []

    def __init__(self, cfg, connection_factory=None):
        self.closed = False
        self._raise = False
        _TrackingHandle.instances.append(self)

    def query_layer(self):
        if self._raise:
            raise RuntimeError("boom")
        return _FakeReader()

    def close(self):
        self.closed = True


def test_partition_fields_closes_handle_on_success(monkeypatch):
    _TrackingHandle.instances = []
    monkeypatch.setattr(d, "_EngineHandle", _TrackingHandle)
    d.s3tables_partition_fields(f"{NS}.{T}", {"table_bucket_arn": ARN})
    assert _TrackingHandle.instances and all(h.closed for h in _TrackingHandle.instances)


def test_partition_fields_closes_handle_on_exception(monkeypatch):
    _TrackingHandle.instances = []

    class _Boom(_TrackingHandle):
        def query_layer(self):
            raise RuntimeError("metadata read failed")

    monkeypatch.setattr(d, "_EngineHandle", _Boom)
    import pytest
    with pytest.raises(d.PartitionSpecUnavailable):
        d.s3tables_partition_fields(f"{NS}.{T}", {"table_bucket_arn": ARN})
    # The handle is still closed on the exception path (review F7).
    assert _TrackingHandle.instances and all(h.closed for h in _TrackingHandle.instances)
