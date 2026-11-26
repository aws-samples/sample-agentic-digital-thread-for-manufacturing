# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Usage loop primitives (Wave 3 Task 3.1 — item C).

- ``usage_tracking.extract_rule_node_ids``: pure regex scan of tool-result
  text for applied-node ids (``subj-<16hex>`` per ADR-0003 D, plus legacy
  ``vm-learned-<8hex>``) — exact-length only, so no false positives on
  subject_key digests, longer hex runs, or UUIDs.
- ``self_learning.record_rule_usage``: atomic ``ADD usage_count`` +
  ``SET last_used_at`` on ACCEPTED rows whose ``neptune_node_id`` matched;
  best-effort (a raising backend returns 0, never raises).

Offline: no AWS, no Neptune, no network. Table mocked via the
``monkeypatch.setattr(self_learning, "_get_table", ...)`` seam
(``test_self_learning_lifecycle.py`` conventions).
"""
from __future__ import annotations

import agents.self_learning as self_learning
from agents.usage_tracking import extract_rule_node_ids


# --------------------------------------------------------------------------- #
# extract_rule_node_ids — pure extraction
# --------------------------------------------------------------------------- #
def test_extracts_subject_id_from_prose():
    text = "Applied rule subj-a1b2c3d4e5f60718 to translate the work-order id."
    assert extract_rule_node_ids([text]) == {"subj-a1b2c3d4e5f60718"}


def test_extracts_legacy_vm_learned_id():
    text = "join via vm-learned-0badf00d (accepted mapping)"
    assert extract_rule_node_ids([text]) == {"vm-learned-0badf00d"}


def test_multiple_texts_and_duplicates_dedupe():
    texts = [
        "used subj-0123456789abcdef here",
        "and subj-0123456789abcdef again, plus vm-learned-deadbeef",
    ]
    assert extract_rule_node_ids(texts) == {
        "subj-0123456789abcdef",
        "vm-learned-deadbeef",
    }


def test_no_false_positive_on_longer_hex_run():
    # A full sha256 digest after "subj-" (e.g. a raw subject_key hash leaked
    # into a result) must NOT match as a truncated 16-hex prefix.
    digest64 = "a" * 64
    assert extract_rule_node_ids([f"subj-{digest64}"]) == set()
    # 17 hex chars — one too many — also rejected.
    assert extract_rule_node_ids(["subj-0123456789abcdef0"]) == set()
    # vm-learned with 9 hex chars — rejected.
    assert extract_rule_node_ids(["vm-learned-deadbeef0"]) == set()


def test_no_false_positive_on_uuid_or_uppercase():
    assert extract_rule_node_ids([
        "mapping_id 550e8400-e29b-41d4-a716-446655440000",
        "SUBJ-0123456789ABCDEF",  # ids are lowercase hex by construction
        "subject_key insight|erp|correlation",
    ]) == set()


def test_boundary_punctuation_still_matches():
    # Ids embedded in JSON-ish result text (quotes, commas, parens) match.
    text = '{"neptune_node_id": "subj-00ff00ff00ff00ff", "v": 2}'
    assert extract_rule_node_ids([text]) == {"subj-00ff00ff00ff00ff"}


def test_empty_and_non_string_inputs():
    assert extract_rule_node_ids([]) == set()
    assert extract_rule_node_ids(None) == set()  # defensive
    assert extract_rule_node_ids(["", None, 42]) == set()  # type: ignore[list-item]


def test_module_is_import_pure():
    # Stdlib-only (ADR-0020/0021): usage_tracking must not import boto3 or
    # pg8000 — mirrors the import-purity assertions in test_proposal_scoring.py.
    import agents.usage_tracking as ut

    src = open(ut.__file__).read()
    assert "import boto3" not in src
    assert "import pg8000" not in src and "import pgdriver" not in src


# --------------------------------------------------------------------------- #
# record_rule_usage — atomic bump on matched ACCEPTED rows, best-effort
# --------------------------------------------------------------------------- #
class RecordingTable:
    """Fake adt-proposed-mappings table: canned status-index query + update spy."""

    def __init__(self, items, *, raise_on_query=False, raise_on_update=False):
        self._items = items
        self._raise_on_query = raise_on_query
        self._raise_on_update = raise_on_update
        self.updates = []  # list of update_item kwargs

    def query(self, **kwargs):
        if self._raise_on_query:
            raise RuntimeError("query boom")
        assert kwargs.get("IndexName") == "status-index"
        status = kwargs["ExpressionAttributeValues"][":status"]
        return {"Items": [i for i in self._items if i.get("status") == status]}

    def update_item(self, **kwargs):
        if self._raise_on_update:
            raise RuntimeError("update boom")
        self.updates.append(kwargs)


def _accepted(mapping_id, node_id):
    return {"mapping_id": mapping_id, "discovered_at": f"2026-07-13T00:00:0{mapping_id[-1]}",
            "status": "accepted", "neptune_node_id": node_id}


def test_bumps_only_matching_accepted_rows(monkeypatch):
    table = RecordingTable([
        _accepted("m1", "subj-a1b2c3d4e5f60718"),
        _accepted("m2", "subj-ffffffffffffffff"),  # accepted but NOT used
    ])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    bumped = self_learning.record_rule_usage({"subj-a1b2c3d4e5f60718"})

    assert bumped == 1
    assert len(table.updates) == 1
    upd = table.updates[0]
    assert upd["Key"] == {"mapping_id": "m1", "discovered_at": "2026-07-13T00:00:01"}
    # Atomic counter: ADD (absent-safe) + last_used_at stamp in ONE update.
    assert "ADD usage_count :one" in upd["UpdateExpression"]
    assert "SET last_used_at = :now" in upd["UpdateExpression"]
    assert upd["ExpressionAttributeValues"][":one"] == 1
    assert upd["ExpressionAttributeValues"][":now"]  # ISO timestamp present


def test_multiple_ids_bump_multiple_rows(monkeypatch):
    table = RecordingTable([
        _accepted("m1", "subj-a1b2c3d4e5f60718"),
        _accepted("m2", "vm-learned-deadbeef"),
    ])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    bumped = self_learning.record_rule_usage(
        {"subj-a1b2c3d4e5f60718", "vm-learned-deadbeef", "subj-0000000000000000"}
    )
    assert bumped == 2
    assert len(table.updates) == 2


def test_empty_ids_short_circuits_no_table_access(monkeypatch):
    def _boom():
        raise AssertionError("_get_table must not be called for empty ids")
    monkeypatch.setattr(self_learning, "_get_table", _boom)
    assert self_learning.record_rule_usage(set()) == 0


def test_swallows_raising_query(monkeypatch):
    table = RecordingTable([], raise_on_query=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    assert self_learning.record_rule_usage({"subj-a1b2c3d4e5f60718"}) == 0


def test_swallows_raising_update(monkeypatch):
    table = RecordingTable([_accepted("m1", "subj-a1b2c3d4e5f60718")],
                           raise_on_update=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    # Never raises — best-effort returns 0 (ADR-0003 F bulk category).
    assert self_learning.record_rule_usage({"subj-a1b2c3d4e5f60718"}) == 0


# --------------------------------------------------------------------------- #
# record_rule_usage — LastEvaluatedKey pagination (F3 regression)
# --------------------------------------------------------------------------- #
class PaginatedTable:
    """Fake table whose status-index query returns MULTIPLE pages.

    ``pages`` is a list of item lists; each query call serves the next page,
    carrying ``LastEvaluatedKey`` until the final page. Records the
    ``ExclusiveStartKey`` seen per call so the loop's key-threading is provable.
    """

    def __init__(self, pages, *, always_more=False):
        self._pages = pages
        self._always_more = always_more
        self.query_calls = []  # ExclusiveStartKey (or None) per call
        self.updates = []

    def query(self, **kwargs):
        assert kwargs.get("IndexName") == "status-index"
        self.query_calls.append(kwargs.get("ExclusiveStartKey"))
        page_idx = len(self.query_calls) - 1
        items = self._pages[page_idx] if page_idx < len(self._pages) else []
        out = {"Items": items}
        if self._always_more or page_idx < len(self._pages) - 1:
            out["LastEvaluatedKey"] = {"mapping_id": f"page-{page_idx}"}
        return out

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


def test_rule_on_page_two_gets_its_usage_bump(monkeypatch):
    """REGRESSION (F3): the status-index query paginates on LastEvaluatedKey —
    an accepted rule past the first DynamoDB page must still get its bump."""
    table = PaginatedTable([
        [_accepted("m1", "subj-ffffffffffffffff")],       # page 1 — not used
        [_accepted("m2", "subj-a1b2c3d4e5f60718")],       # page 2 — USED
    ])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    bumped = self_learning.record_rule_usage({"subj-a1b2c3d4e5f60718"})

    assert bumped == 1
    assert len(table.updates) == 1
    assert table.updates[0]["Key"]["mapping_id"] == "m2"
    # Two query calls: first without ExclusiveStartKey, second threading the
    # LastEvaluatedKey from page 1.
    assert table.query_calls == [None, {"mapping_id": "page-0"}]


def test_pagination_is_bounded_at_twenty_pages(monkeypatch):
    """Defensive cap: a backend that ALWAYS returns LastEvaluatedKey must not
    spin — the loop stops after 20 pages and still returns best-effort."""
    table = PaginatedTable([[] for _ in range(30)], always_more=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    assert self_learning.record_rule_usage({"subj-a1b2c3d4e5f60718"}) == 0
    assert len(table.query_calls) == 20
