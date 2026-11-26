# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Feedback durability — the silently-discarded-feedback fix (2026-08-17).

The live gap: ``DynamoDBStorage.put_item`` returned True with a DEBUG line when
no table was configured, ``FEEDBACK_TABLE_NAME`` was set by no stack, and no
stack provisioned a feedback table — so ``POST /api/feedback`` answered
``{"status": "success"}`` and wrote nothing, forever.

Offline: a fake table stands in for the boto3 resource Table (the
``test_storage_scan_pagination.py`` seam). Each pin names the regression it
kills:

* a CONFIGURED store actually persists the row, with the table's key/TTL
  attributes stamped on it;
* an UNCONFIGURED store is VISIBLE — a WARNING naming the storage kind and the
  operation — and its deceptive True is pinned as deliberate, so nobody
  "simplifies" the warning away again;
* the time-range read is INDEX-backed (Query per UTC day), not a scan, which is
  what the least-privilege IAM grant assumes;
* the CDK stack literals and the app-side contract stay in lockstep.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import pytest

from agents import storage as st

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_STACK = REPO_ROOT / "stacks" / "adt_database_stack.py"

_RECORD = {
    "user_id": "u-1",
    "timestamp": "2026-08-17T12:00:00+00:00",
    "session_id": "s-1",
    "message_id": "m-1",
    "user_message": "why is line 2 down?",
    "assistant_response": "PM-1182 was deferred.",
    "tools_used": ["query_mes"],
    "sentiment": "negative",
    "user_comment": "wrong shift",
}


class _FakeTable:
    """Records writes; serves canned Query pages keyed by partition value."""

    def __init__(self, pages_by_pk: dict[str, list[list[dict]]] | None = None):
        self.items: list[dict] = []
        self.query_calls: list[dict] = []
        self.scan_calls: list[dict] = []
        self._pages_by_pk = pages_by_pk or {}
        self._cursor: dict[str, int] = {}

    def put_item(self, Item):  # noqa: N803 - boto3 kwarg
        self.items.append(Item)

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return {"Items": []}

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        # The partition value is inside the KeyConditionExpression object; the
        # fake reads it off the recorded call order instead of introspecting
        # boto3 condition internals.
        pk = _pk_of(kwargs["KeyConditionExpression"])
        pages = self._pages_by_pk.get(pk, [[]])
        idx = self._cursor.get(pk, 0)
        self._cursor[pk] = idx + 1
        if idx >= len(pages):
            return {"Items": []}
        resp = {"Items": pages[idx]}
        if idx < len(pages) - 1:
            resp["LastEvaluatedKey"] = {"feedback_date": pk}
        return resp


def _pk_of(condition) -> str:
    """Extract the partition-key value from a boto3 ConditionBase tree."""
    values = getattr(condition, "_values", ())
    for value in values:
        if isinstance(value, str):
            return value
        nested = _pk_of(value)
        if nested:
            return nested
    return ""


def _store(table_name="adt-feedback", table=None):
    store = st.FeedbackStorage(table_name, "us-east-1")
    if table is not None:
        store._db._table = table  # bypass boto3
    return store


@pytest.fixture(autouse=True)
def _fresh_warning_dedupe():
    """The unconfigured warning is deduped per process — reset around each test
    so a caplog assertion is not eaten by an earlier test in the lane."""
    st.reset_unconfigured_warnings()
    yield
    st.reset_unconfigured_warnings()


# --------------------------------------------------------------------------
# (a) a configured table receives the write
# --------------------------------------------------------------------------

def test_configured_table_receives_the_write():
    table = _FakeTable()
    assert _store(table=table).store(dict(_RECORD)) is True
    assert len(table.items) == 1
    assert table.items[0]["user_comment"] == "wrong shift"
    assert table.items[0]["sentiment"] == "negative"


def test_write_carries_the_table_key_contract():
    """PK/SK/GSI-PK/TTL attributes must be on the row or the write is a
    ValidationException (SK/GSI key) or an immortal record (no TTL)."""
    table = _FakeTable()
    _store(table=table).store(dict(_RECORD))
    item = table.items[0]
    assert item["feedback_id"] == st.feedback_id_for("s-1", "m-1")
    assert re.fullmatch(r"fb-[0-9a-f]{32}", item["feedback_id"])
    assert item["timestamp"] == _RECORD["timestamp"]      # sort key preserved
    assert item["feedback_date"] == "2026-08-17"          # GSI partition
    ttl = int(item[st.FEEDBACK_TTL_ATTRIBUTE])
    expected = time.time() + st.FEEDBACK_TTL_DAYS * 86400
    assert abs(ttl - expected) < 120


def test_feedback_id_is_stable_and_turn_scoped():
    same = st.feedback_id_for("s-1", "m-1")
    assert st.feedback_id_for("s-1", "m-1") == same
    assert st.feedback_id_for("s-1", "m-2") != same
    assert st.feedback_id_for("s-2", "m-1") != same


def test_repeat_thumbs_on_one_message_do_not_overwrite():
    """Same feedback_id, different timestamp ⇒ two rows, full history kept."""
    table = _FakeTable()
    store = _store(table=table)
    store.store(dict(_RECORD))
    store.store({**_RECORD, "timestamp": "2026-08-17T12:05:00+00:00",
                 "sentiment": "positive"})
    ids = {i["feedback_id"] for i in table.items}
    assert len(ids) == 1 and len(table.items) == 2
    assert {i["timestamp"] for i in table.items} == {
        "2026-08-17T12:00:00+00:00", "2026-08-17T12:05:00+00:00"}


def test_caller_supplied_key_fields_win():
    table = _FakeTable()
    _store(table=table).store({**_RECORD, "feedback_id": "fb-explicit",
                               "ttl": 42})
    assert table.items[0]["feedback_id"] == "fb-explicit"
    assert table.items[0]["ttl"] == 42


def test_missing_timestamp_is_defaulted_not_written_empty():
    """timestamp is the SORT key; an empty one would fail the write outright."""
    table = _FakeTable()
    _store(table=table).store({k: v for k, v in _RECORD.items()
                               if k != "timestamp"})
    item = table.items[0]
    assert item["timestamp"]
    assert item["feedback_date"] == item["timestamp"][:10]


# --------------------------------------------------------------------------
# (b) an UNCONFIGURED table is visible
# --------------------------------------------------------------------------

def test_unconfigured_put_warns_naming_the_storage_kind(caplog):
    with caplog.at_level(logging.WARNING, logger="agents.storage"):
        result = _store(table_name=None).store(dict(_RECORD))
    # The deceptive success is DELIBERATE (local dev / offline lane run
    # table-less) — but it must never again be silent.
    assert result is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "unconfigured put_item must warn, not whisper at DEBUG"
    text = warnings[0].getMessage()
    assert "storage_unconfigured" in text
    assert "FeedbackStorage" in text
    assert "put_item" in text


def test_unconfigured_warning_is_deduped_per_kind_and_op(caplog):
    """Once per (kind, op) per process — UsageStorage writes every chat turn and
    a per-call warning would flood the log group until someone mutes it."""
    with caplog.at_level(logging.DEBUG, logger="agents.storage"):
        store = _store(table_name=None)
        store.store(dict(_RECORD))
        store.store(dict(_RECORD))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert any(r.levelno == logging.DEBUG and "storage_unconfigured" in r.getMessage()
               for r in caplog.records)


def test_unconfigured_warning_names_each_sibling_kind(caplog):
    with caplog.at_level(logging.WARNING, logger="agents.storage"):
        st.UsageStorage(None).store({"timestamp": "t"})
        st.GuardrailStorage(None).store({"timestamp": "t"})
    kinds = {r.getMessage().split(": ")[1].split(".")[0]
             for r in caplog.records if r.levelno == logging.WARNING}
    assert kinds == {"UsageStorage", "GuardrailStorage"}


def test_is_configured_is_the_honest_seam():
    assert st.FeedbackStorage(None)._db.is_configured is False
    assert st.FeedbackStorage("adt-feedback")._db.is_configured is True


def test_unconfigured_read_warns_and_returns_empty(caplog):
    with caplog.at_level(logging.WARNING, logger="agents.storage"):
        rows = _store(table_name=None).get_by_time_range(
            "2026-08-17T00:00:00+00:00", "2026-08-17T23:59:59+00:00")
    assert rows == []
    assert any("FeedbackStorage.query" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


# --------------------------------------------------------------------------
# time-range reads are index-backed, not a full scan
# --------------------------------------------------------------------------

def test_time_range_queries_the_date_index_per_day_never_scans():
    table = _FakeTable({
        "2026-08-16": [[{"timestamp": "2026-08-16T09:00:00+00:00"}]],
        "2026-08-17": [[{"timestamp": "2026-08-17T08:00:00+00:00"}]],
    })
    rows = _store(table=table).get_by_time_range(
        "2026-08-16T00:00:00+00:00", "2026-08-17T23:59:59+00:00")
    assert [r["timestamp"] for r in rows] == [
        "2026-08-16T09:00:00+00:00", "2026-08-17T08:00:00+00:00"]
    assert table.scan_calls == []
    assert len(table.query_calls) == 2
    assert all(c["IndexName"] == st.FEEDBACK_DATE_INDEX
               for c in table.query_calls)


def test_time_range_query_paginates():
    """Feedback rows carry a full assistant response each — one 1MB page is not
    a day. An unpaginated Query would silently truncate the admin listing."""
    table = _FakeTable({"2026-08-17": [
        [{"timestamp": "2026-08-17T01:00:00+00:00"}],
        [{"timestamp": "2026-08-17T02:00:00+00:00"}],
    ]})
    rows = _store(table=table).get_by_time_range(
        "2026-08-17T00:00:00+00:00", "2026-08-17T23:59:59+00:00")
    assert len(rows) == 2
    assert "ExclusiveStartKey" in table.query_calls[1]


def test_sentiment_becomes_a_filter_not_a_key():
    table = _FakeTable({"2026-08-17": [[]]})
    _store(table=table).get_by_time_range(
        "2026-08-17T00:00:00+00:00", "2026-08-17T23:59:59+00:00",
        sentiment="negative")
    assert "FilterExpression" in table.query_calls[0]


def test_unparseable_range_warns_and_returns_empty(caplog):
    table = _FakeTable()
    with caplog.at_level(logging.WARNING, logger="agents.storage"):
        assert _store(table=table).get_by_time_range("not-a-date", "also-not") == []
    assert table.query_calls == []
    assert any("feedback_range_unparseable" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


def test_inverted_range_returns_empty_without_querying():
    table = _FakeTable()
    assert _store(table=table).get_by_time_range(
        "2026-08-18T00:00:00+00:00", "2026-08-17T00:00:00+00:00") == []
    assert table.query_calls == []


def test_range_fan_out_is_clamped_beyond_the_retention_horizon():
    """A silly client range must not issue thousands of queries; the clamp is
    far beyond FEEDBACK_TTL_DAYS so no live row can hide behind it."""
    days = st._utc_days_in_range("1990-01-01T00:00:00+00:00",
                                 "2026-08-17T00:00:00+00:00")
    assert len(days) == st._MAX_RANGE_DAYS
    assert days[-1] == "2026-08-17"
    assert st._MAX_RANGE_DAYS > st.FEEDBACK_TTL_DAYS


# --------------------------------------------------------------------------
# (c) CDK ↔ app-side contract lockstep
# --------------------------------------------------------------------------

def test_database_stack_and_storage_agree_on_the_table_contract():
    """Two files must spell the same literals; a rename in one silently breaks
    the deployed read/write path (the class of bug this whole fix is about)."""
    source = DB_STACK.read_text()
    assert f'FEEDBACK_TABLE_NAME = "{st.FEEDBACK_TABLE_DEFAULT}"' in source
    assert f'FEEDBACK_DATE_INDEX_NAME = "{st.FEEDBACK_DATE_INDEX}"' in source
    assert f'FEEDBACK_TTL_ATTRIBUTE = "{st.FEEDBACK_TTL_ATTRIBUTE}"' in source


def test_database_stack_declares_the_feedback_table_shape():
    source = DB_STACK.read_text()
    assert 'table_name=FEEDBACK_TABLE_NAME' in source
    assert 'name="feedback_id"' in source
    assert 'name="feedback_date"' in source
    assert 'time_to_live_attribute=FEEDBACK_TTL_ATTRIBUTE' in source
    assert 'index_name=FEEDBACK_DATE_INDEX_NAME' in source


def test_compute_stack_wires_env_and_least_privilege_iam():
    """The env var is the whole point of the fix; the grant must stay narrower
    than the shared six-action DynamoDB statement."""
    source = (REPO_ROOT / "stacks" / "adt_compute_stack.py").read_text()
    assert f'"FEEDBACK_TABLE_NAME": "{st.FEEDBACK_TABLE_DEFAULT}"' in source
    assert '"dynamodb:PutItem", "dynamodb:Query"' in source
    assert f"table/{st.FEEDBACK_TABLE_DEFAULT}/index/{st.FEEDBACK_DATE_INDEX}" in source
    feedback_policy = source.split('"DynamoDBFeedback"', 1)[1][:1200]
    for forbidden in ("dynamodb:Scan", "dynamodb:DeleteItem",
                      "dynamodb:UpdateItem", "dynamodb:GetItem"):
        assert forbidden not in feedback_policy
