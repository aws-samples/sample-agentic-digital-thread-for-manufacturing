# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Verifier contracts for cross-worker system-guidance CAS."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import threading

import pytest

import agents.system_guidance as guidance


class _ConditionalFailure(Exception):
    pass


class _RaceBackend:
    """Shared durable state; each store still owns an independent process lock."""

    def __init__(self):
        self._snapshot = guidance.GuidanceSnapshot.absent()
        self._lock = threading.Lock()

    def snapshot(self, _system_id):
        with self._lock:
            return self._snapshot

    def compare_and_set(self, _system_id, expected, text):
        with self._lock:
            if self._snapshot != expected:
                raise guidance.GuidanceConflict()
            self._snapshot = guidance.GuidanceSnapshot.present(
                text, expected.version + 1)
            return self._snapshot

    def version(self, system_id):
        return self.snapshot(system_id).version

    def load(self, system_id):
        snapshot = self.snapshot(system_id)
        return snapshot.text, snapshot.version


class _Table:
    def __init__(self, item=None):
        self.item = dict(item or {})
        self.get_calls = []
        self.update_kwargs = None

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        return {"Item": dict(self.item)} if self.item else {}

    def _check_condition(self, kwargs):
        expected = kwargs["ExpressionAttributeValues"]
        names = kwargs["ExpressionAttributeNames"]
        condition = kwargs["ConditionExpression"]
        for alias in ("#g", "#v", "#d"):
            attribute = names[alias]
            if f"attribute_not_exists({alias})" in condition:
                if attribute in self.item:
                    raise _ConditionalFailure()
            if f"attribute_exists({alias})" in condition:
                if attribute not in self.item:
                    raise _ConditionalFailure()
        if ":expected_version" in expected:
            if str(self.item.get(names["#v"])) != expected[":expected_version"]:
                raise _ConditionalFailure()
        if ":expected_digest" in expected:
            if self.item.get(names["#d"]) != expected[":expected_digest"]:
                raise _ConditionalFailure()
        if ":expected_text" in expected:
            if self.item.get(names["#g"]) != expected[":expected_text"]:
                raise _ConditionalFailure()

    def update_item(self, **kwargs):
        self.update_kwargs = kwargs
        self._check_condition(kwargs)
        expected = kwargs["ExpressionAttributeValues"]
        self.item["system_id"] = kwargs["Key"]["system_id"]
        self.item["version"] = expected[":next_version"]
        if ":text" in expected:
            self.item["guidance"] = expected[":text"]
            self.item["digest"] = expected[":next_digest"]
        else:
            self.item.pop("guidance", None)
            self.item.pop("digest", None)
        return {"Attributes": dict(self.item)}


class _ScanTable(_Table):
    def __init__(self, pages):
        super().__init__()
        self.pages = list(pages)
        self.scan_calls = []

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return self.pages.pop(0)


class _Resource:
    def __init__(self, table):
        self.table = table

    def Table(self, _name):
        return self.table


def _dynamo(table):
    backend = guidance.DynamoGuidanceBackend("guidance-table")
    backend._dynamodb = _Resource(table)
    return backend


def _item(system_id, text, version):
    return {"system_id": system_id, "guidance": text,
            "version": str(version), "digest": guidance.guidance_digest(text)}


def test_snapshot_is_immutable_and_carries_full_digest_existence_and_legacy():
    absent = _dynamo(_Table()).snapshot("system-a")
    assert absent.text == "" and absent.version == 0
    assert absent.exists is False and absent.existence is False
    assert absent.legacy is False
    assert len(absent.full_digest.removeprefix("sha256:")) == 64
    assert absent.digest == absent.full_digest
    with pytest.raises(FrozenInstanceError):
        absent.version = 1

    legacy = _dynamo(_Table({
        "system_id": "system-a", "guidance": "legacy text", "version": "7",
    })).snapshot("system-a")
    assert legacy.text == "legacy text" and legacy.version == 7
    assert legacy.exists is True and legacy.existence is True
    assert legacy.legacy is True
    assert legacy.full_digest == guidance.guidance_digest("legacy text")


def test_snapshot_is_consistent_and_rejects_digest_that_does_not_match_text():
    table = _Table(_item("system-a", "exact", 3))
    assert _dynamo(table).snapshot("system-a").text == "exact"
    assert table.get_calls[-1]["ConsistentRead"] is True

    corrupt = _Table(_item("system-a", "exact", 3))
    corrupt.item["digest"] = guidance.guidance_digest("different")
    with pytest.raises(guidance.GuidanceUnavailable, match="digest mismatch"):
        _dynamo(corrupt).snapshot("system-a")


def test_dynamo_new_row_cas_requires_guidance_version_and_digest_to_be_absent():
    table = _Table()
    updated = _dynamo(table).compare_and_set(
        "system-a", guidance.GuidanceSnapshot.absent(), "first")

    condition = table.update_kwargs["ConditionExpression"]
    assert "attribute_not_exists(#g)" in condition
    assert "attribute_not_exists(#v)" in condition
    assert "attribute_not_exists(#d)" in condition
    assert updated == guidance.GuidanceSnapshot.present("first", 1)


def test_dynamo_versioned_absence_cas_checks_tombstone_version():
    table = _Table({"system_id": "system-a", "version": "4"})
    updated = _dynamo(table).compare_and_set(
        "system-a", guidance.GuidanceSnapshot.absent(4), "restored")
    kwargs = table.update_kwargs
    assert "#v = :expected_version" in kwargs["ConditionExpression"]
    assert kwargs["ExpressionAttributeValues"][":expected_version"] == "4"
    assert updated.version == 5

    stale = _Table({"system_id": "system-a", "version": "4"})
    with pytest.raises(guidance.GuidanceConflict):
        _dynamo(stale).compare_and_set(
            "system-a", guidance.GuidanceSnapshot.absent(3), "lost race")


def test_dynamo_digest_row_cas_uses_version_and_full_digest_condition():
    initial = guidance.GuidanceSnapshot.present("before", 4)
    table = _Table(_item("system-a", initial.text, initial.version))
    updated = _dynamo(table).compare_and_set("system-a", initial, "after")

    kwargs = table.update_kwargs
    assert "#v = :expected_version" in kwargs["ConditionExpression"]
    assert "#d = :expected_digest" in kwargs["ConditionExpression"]
    values = kwargs["ExpressionAttributeValues"]
    assert values[":expected_digest"] == initial.full_digest
    assert values[":next_digest"] == guidance.guidance_digest("after")
    assert updated == guidance.GuidanceSnapshot.present("after", 5)


def test_dynamo_legacy_cas_checks_full_text_and_absent_digest():
    legacy = guidance.GuidanceSnapshot(
        text="legacy", version=2,
        full_digest=guidance.guidance_digest("legacy"),
        exists=True, legacy=True)
    table = _Table({"system_id": "system-a", "guidance": "legacy",
                    "version": "2"})
    updated = _dynamo(table).compare_and_set("system-a", legacy, "modern")
    condition = table.update_kwargs["ConditionExpression"]
    assert "#g = :expected_text" in condition
    assert "attribute_not_exists(#d)" in condition
    assert updated.legacy is False


def test_dynamo_restore_can_create_versioned_absence_with_exact_readback():
    initial = guidance.GuidanceSnapshot.present("before", 8)
    table = _Table(_item("system-a", "before", 8))
    restored = _dynamo(table).restore(
        "system-a", initial, guidance.GuidanceSnapshot.absent())
    assert restored == guidance.GuidanceSnapshot.absent(9)
    assert "guidance" not in table.item and "digest" not in table.item


def test_dynamo_update_rejects_non_exact_return_values():
    class AlteringReturnTable(_Table):
        def update_item(self, **kwargs):
            response = super().update_item(**kwargs)
            response["Attributes"]["guidance"] += " altered"
            response["Attributes"]["digest"] = guidance.guidance_digest(
                response["Attributes"]["guidance"])
            return response

    with pytest.raises(guidance.GuidanceUnavailable,
                       match="update readback failed"):
        _dynamo(AlteringReturnTable()).compare_and_set(
            "system-a", guidance.GuidanceSnapshot.absent(), "exact")


def test_two_independent_stores_racing_one_expected_snapshot_exactly_one_wins():
    backend = _RaceBackend()
    first = guidance.SystemGuidanceStore(backend=backend)
    second = guidance.SystemGuidanceStore(backend=backend)
    expected = first.snapshot("system-a")
    barrier = threading.Barrier(3)
    outcomes = []

    def run(store, text):
        barrier.wait()
        try:
            outcomes.append((
                "ok", store.compare_and_set("system-a", expected, text)))
        except guidance.GuidanceConflict:
            outcomes.append(("conflict", None))

    threads = [threading.Thread(target=run, args=(first, "first")),
               threading.Thread(target=run, args=(second, "second"))]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("conflict") == 1
    assert backend.snapshot("system-a").text in {"first", "second"}


def test_process_lock_is_not_held_during_backend_cas():
    class InspectingBackend(_RaceBackend):
        def __init__(self):
            super().__init__()
            self.store = None

        def compare_and_set(self, system_id, expected, text):
            assert self.store is not None
            assert self.store._lock.acquire(blocking=False), (
                "process lock may protect only cache state, never durable CAS")
            self.store._lock.release()
            return super().compare_and_set(system_id, expected, text)

    backend = InspectingBackend()
    store = guidance.SystemGuidanceStore(backend=backend)
    backend.store = store
    store.compare_and_set("system-a", store.snapshot("system-a"), "text")


def test_publish_exact_performs_separate_strict_readback():
    class AlteringReadbackBackend(_RaceBackend):
        def compare_and_set(self, system_id, expected, text):
            desired = guidance.GuidanceSnapshot.present(
                text, expected.version + 1)
            with self._lock:
                self._snapshot = guidance.GuidanceSnapshot.present(
                    text + " altered", expected.version + 1)
            return desired

    store = guidance.SystemGuidanceStore(backend=AlteringReadbackBackend())
    with pytest.raises(guidance.GuidanceUnavailable,
                       match="exact readback failed"):
        store.publish_exact("system-a", "exact")


def test_delivery_listing_reads_all_pages_with_hard_bounds():
    table = _ScanTable([
        {"Items": [_item("system-a", "a", 1)],
         "LastEvaluatedKey": {"system_id": "system-a"}},
        {"Items": [_item("system-b", "b", 2)]},
    ])
    snapshots = _dynamo(table).list_snapshots(max_items=3, max_pages=2)
    assert set(snapshots) == {"system-a", "system-b"}
    assert snapshots["system-b"].text == "b"
    assert table.scan_calls == [
        {"Limit": 3, "ConsistentRead": True},
        {"Limit": 2, "ConsistentRead": True,
         "ExclusiveStartKey": {"system_id": "system-a"}},
    ]


def test_delivery_listing_fails_closed_instead_of_returning_partial_rows():
    item_limited = _ScanTable([
        {"Items": [_item("system-a", "a", 1)],
         "LastEvaluatedKey": {"system_id": "system-a"}},
    ])
    with pytest.raises(guidance.GuidanceUnavailable, match="scan bounded"):
        _dynamo(item_limited).list_snapshots(max_items=1, max_pages=2)

    page_limited = _ScanTable([
        {"Items": [_item("system-a", "a", 1)],
         "LastEvaluatedKey": {"system_id": "system-a"}},
    ])
    with pytest.raises(guidance.GuidanceUnavailable, match="scan bounded"):
        _dynamo(page_limited).list_snapshots(max_items=2, max_pages=1)


@pytest.mark.parametrize("max_items,max_pages", [(0, 1), (1001, 1), (1, 0),
                                                   (1, 11)])
def test_delivery_listing_rejects_unbounded_or_empty_limits(max_items, max_pages):
    with pytest.raises(ValueError, match="bounds out of range"):
        _dynamo(_ScanTable([])).list_snapshots(
            max_items=max_items, max_pages=max_pages)
