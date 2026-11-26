# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A proposal that restates a ValueMapping already in the graph is not queued.

Onboarding writes ValueMapping nodes straight to the graph (equipment name
matches, registry to historian matches, the Resolve step's value mappings), so
no proposal-ledger row stands behind them. An answer that USED such a mapping
and then emitted a value-mapping proposal for the same pair, or a miner
crosswalk naming the same pair, used to land in the review queue (or in the
staging buffer on its way there) as if it were new. ``log_proposed_mapping``
now reads the graph's ValueMappings between the two systems and, when one has
the same systems, fields and values in either direction, files nothing. A
bare field matches a qualified one only when the system's schema resolves the
bare column to exactly that one ``table.column``; otherwise the proposal
files. When
an accepted ledger row stands behind the node (a steward accepted it), the
restatement counts on that row's ``coverage_evidence_count``, the counter an
accepted rule already gets for an observation it covers.

Unchanged and pinned here: a different value or a different field still
files; a person's own proposal (``source="user"``) still files; a graph read
that fails files as before.

Offline: FakeTable, a monkeypatched ``known_systems`` catalog and graph seam.
Neutral fixture names only.
"""
from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

import pytest
from botocore import exceptions as bce

import agents.db as db
import agents.digital_thread_server as dts
import agents.join_discovery as jd
import agents.onboard as onboard
import agents.self_learning as self_learning
from agents import runtime_app

# The real seams, before any fixture replaces them.
_REAL_SL_QUERY = self_learning._neptune_query
_REAL_EXECUTE = db._neptune_execute

_SYSTEMS = [
    {"id": "asset-book", "name": "Asset Book", "system_type": "CMMS",
     "plant": "East Yard"},
    {"id": "cell-runner", "name": "Cell Runner", "system_type": "MES",
     "plant": "East Yard"},
]

_STORED = {
    "id": "vm-stored-1", "from_system": "asset-book",
    "from_field": "units.unit_tag", "from_value": "U-7781",
    "to_system": "cell-runner", "to_field": "stations.station_code",
    "to_value": "ST-04",
}

_FORWARD = dict(from_system="asset-book", from_field="units.unit_tag",
                from_value="U-7781", to_system="cell-runner",
                to_field="stations.station_code", to_value="ST-04")
_REVERSE = dict(from_system="cell-runner", from_field="stations.station_code",
                from_value="ST-04", to_system="asset-book",
                to_field="units.unit_tag", to_value="U-7781")


class FakeTable:
    def __init__(self):
        self.items: list[dict] = []
        self.updates: list[dict] = []

    def query(self, **kwargs):
        idx = kwargs.get("IndexName")
        vals = kwargs.get("ExpressionAttributeValues", {})
        if idx == "subject-key-index":
            return {"Items": [i for i in self.items
                              if i.get("subject_key") == vals.get(":sk")]}
        if idx == "status-index":
            return {"Items": [i for i in self.items
                              if i.get("status") == vals.get(":status")]}
        return {"Items": [i for i in self.items
                          if i.get("mapping_id") == vals.get(":mid")]}

    def put_item(self, Item):
        self.items.append(dict(Item))

    def update_item(self, **kwargs):
        self.updates.append(kwargs)

    def delete_item(self, Key):
        self.items = [i for i in self.items
                      if not (i.get("mapping_id") == Key["mapping_id"]
                              and i.get("discovered_at") == Key["discovered_at"])]

    def pending(self) -> list[dict]:
        return [i for i in self.items if i.get("status") == "pending"]

    def staged(self) -> list[dict]:
        return [i for i in self.items
                if str(i.get("mapping_id", "")).startswith("staged#")]


def _graph(rows, fields=None, calls=None):
    """The graph seam: ``rows`` answer the ValueMapping read; ``fields``
    (``{system: [(table, column), ...]}``) answer a schema read for one
    column; ``calls`` collects ``(kind, params)`` per read; nothing else."""
    def query(cypher, params=None):
        params = dict(params or {})
        if "ValueMapping" in cypher and "from_value" in cypher:
            if calls is not None:
                calls.append(("value_mapping", params))
            return [dict(r) for r in rows]
        if "HAS_FIELD" in cypher and "col" in params:
            if calls is not None:
                calls.append(("field", params))
            found = [{"table_name": t, "field_name": c}
                     for t, c in (fields or {}).get(params.get("sid"), [])
                     if c.lower() == params["col"]]
            return found[:2]
        return []
    return query


@pytest.fixture()
def wired(monkeypatch):
    fake = FakeTable()
    # Every other graph read in the propose path answers no rows, offline.
    monkeypatch.setattr(db, "_neptune_execute", lambda cypher, params=None: [])
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "known_systems", lambda: list(_SYSTEMS))
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    monkeypatch.setattr(self_learning, "_neptune_query", _graph([_STORED]))
    self_learning._reset_accepted_coverage_cache()
    yield fake
    self_learning._reset_accepted_coverage_cache()


def _log(endpoints, *, proposed_type="value_mapping", source="agent", **extra):
    return self_learning.log_proposed_mapping(
        **endpoints, concept="equipment-id", confidence=0.95,
        query_context="confirmed this turn", proposed_type=proposed_type,
        source=source, **extra)


def _block(endpoints, method="value_format_match") -> str:
    lines = [f"{k}: {v}" for k, v in endpoints.items()]
    lines += [f"method: {method}", "confidence: high",
              "reason: the stored mapping was used this turn"]
    return ("Answer.\n---VALUE-MAPPING-PROPOSAL---\n" + "\n".join(lines)
            + "\n---END-PROPOSAL---\n")


def _capture(text):
    state = SimpleNamespace(response_text=[text], prompt="q", raw_prompt="q",
                            tool_calls_log=[], session_id="s-1")
    runtime_app._capture_value_mapping_proposals(state)


# --------------------------------------------------------------------------- #
# The defect: a restatement of a stored mapping is queued as new
# --------------------------------------------------------------------------- #
def test_answer_block_restating_stored_mapping_is_not_queued(wired):
    _capture(_block(_FORWARD))
    assert wired.pending() == []
    assert wired.staged() == []


def test_reverse_direction_restatement_is_not_queued(wired):
    _capture(_block(_REVERSE))
    assert wired.pending() == []


def test_restatement_is_not_staged_either(wired, monkeypatch):
    # With the default co-reference bar a first observation is buffered;
    # a restatement of a stored mapping is not even buffered.
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 2)
    assert _log(_FORWARD, provenance="orchestrator") is None
    assert wired.items == []


def test_spelling_variants_of_the_same_pair_match(wired):
    # Case and outer spacing on the proposal side.
    variant = {**_FORWARD, "from_value": " u-7781 ", "to_value": "st-04",
               "from_field": "Units.Unit_Tag", "to_field": " Stations.Station_Code"}
    assert _log(variant) is None
    assert wired.items == []


# --------------------------------------------------------------------------- #
# A bare field against a qualified one
# --------------------------------------------------------------------------- #
_BARE_STORED = {**_STORED, "to_field": "station_code"}
_TWO_TABLES = {"cell-runner": [("stations", "station_code"),
                               ("alarms", "station_code")]}


def _graph_with(monkeypatch, rows, fields, calls=None):
    monkeypatch.setattr(self_learning, "_neptune_query",
                        _graph(rows, fields, calls))


def test_bare_stored_field_does_not_match_another_table_with_that_column(
        wired, monkeypatch):
    # Two fields of the system end in station_code; the stored bare field
    # could be either, so a proposal naming the other table files.
    _graph_with(monkeypatch, [_BARE_STORED], _TWO_TABLES)
    assert _log({**_FORWARD, "to_field": "alarms.station_code"}) is not None
    assert len(wired.pending()) == 1


def test_ambiguous_bare_stored_field_matches_neither_table(wired, monkeypatch):
    _graph_with(monkeypatch, [_BARE_STORED], _TWO_TABLES)
    assert _log(_FORWARD) is not None
    assert len(wired.pending()) == 1


def test_bare_stored_field_resolving_to_another_table_files(wired, monkeypatch):
    _graph_with(monkeypatch, [_BARE_STORED],
                {"cell-runner": [("alarms", "station_code")]})
    assert _log(_FORWARD) is not None
    assert len(wired.pending()) == 1


def test_bare_stored_field_the_schema_does_not_hold_files(wired, monkeypatch):
    _graph_with(monkeypatch, [_BARE_STORED], {})
    assert _log(_FORWARD) is not None
    assert len(wired.pending()) == 1


def test_bare_proposal_field_against_a_qualified_stored_field(wired, monkeypatch):
    _graph_with(monkeypatch, [_STORED], _TWO_TABLES)
    assert _log({**_FORWARD, "to_field": "station_code"}) is not None
    assert len(wired.pending()) == 1


def test_a_failing_schema_read_files(wired, monkeypatch):
    def query(cypher, params=None):
        if "HAS_FIELD" in cypher:
            raise RuntimeError("graph unavailable")
        return _graph([_BARE_STORED])(cypher, params)
    monkeypatch.setattr(self_learning, "_neptune_query", query)
    assert _log(_FORWARD) is not None
    assert len(wired.pending()) == 1


def test_bare_field_resolving_to_one_table_column_matches(wired, monkeypatch):
    _graph_with(monkeypatch, [_BARE_STORED],
                {"cell-runner": [("stations", "station_code"),
                                 ("alarms", "alarm_code")]})
    assert _log(_FORWARD) is None
    assert wired.items == []


def test_the_same_bare_field_on_both_sides_needs_no_schema_read(
        wired, monkeypatch):
    calls: list = []
    _graph_with(monkeypatch, [_BARE_STORED], _TWO_TABLES, calls)
    assert _log({**_FORWARD, "to_field": "Station_Code"}) is None
    assert [kind for kind, _ in calls] == ["value_mapping"]


def test_miner_crosswalk_restating_stored_mapping_is_not_queued(wired):
    from agents.miner_contract import MINER_METHOD, MINER_PROVENANCE, MINER_SOURCE
    assert _log(_REVERSE, proposed_type="crosswalk", source=MINER_SOURCE,
                method=MINER_METHOD, provenance=MINER_PROVENANCE) is None
    assert wired.items == []


def test_tool_call_reports_the_mapping_is_already_stored(wired):
    out = json.loads(dts.save_value_mapping(
        **_FORWARD, method="value_format_match", confidence="high",
        reason="the stored mapping was used this turn"))
    assert out["status"] == "already_stored"
    assert wired.items == []


# --------------------------------------------------------------------------- #
# Confirmation evidence on the stored mapping
# --------------------------------------------------------------------------- #
def test_restatement_counts_on_the_accepted_row_behind_the_node(wired):
    wired.items.append({
        "mapping_id": "m-accepted", "discovered_at": "2026-01-01T00:00:00+00:00",
        "status": "accepted", "proposed_type": "value_mapping",
        "neptune_node_id": "vm-stored-1", **_FORWARD})
    self_learning._reset_accepted_coverage_cache()
    assert _log(_REVERSE) is None
    assert wired.pending() == []
    assert len(wired.updates) == 1
    update = wired.updates[0]
    assert update["Key"] == {"mapping_id": "m-accepted",
                             "discovered_at": "2026-01-01T00:00:00+00:00"}
    assert "coverage_evidence_count" in update["UpdateExpression"]


def test_onboarding_mapping_without_a_ledger_row_records_nothing(wired):
    assert _log(_FORWARD) is None
    assert wired.items == [] and wired.updates == []


# --------------------------------------------------------------------------- #
# Guards: what still files
# --------------------------------------------------------------------------- #
def test_a_different_value_still_files(wired):
    assert _log({**_FORWARD, "to_value": "ST-09"}) is not None
    assert len(wired.pending()) == 1


def test_a_different_field_still_files(wired):
    assert _log({**_FORWARD, "to_field": "stations.station_alias"}) is not None
    assert len(wired.pending()) == 1


def test_a_person_proposing_the_same_pair_still_files(wired):
    assert _log(_FORWARD, source="user") is not None
    assert len(wired.pending()) == 1


def test_a_failing_graph_read_files_as_before(wired, monkeypatch):
    def boom(cypher, params=None):
        if "ValueMapping" in cypher:
            raise RuntimeError("graph unavailable")
        return []
    monkeypatch.setattr(self_learning, "_neptune_query", boom)
    assert _log(_FORWARD) is not None
    assert len(wired.pending()) == 1


def test_no_stored_mapping_files_as_before(wired, monkeypatch):
    monkeypatch.setattr(self_learning, "_neptune_query", _graph([]))
    assert _log(_FORWARD) is not None
    assert len(wired.pending()) == 1


# --------------------------------------------------------------------------- #
# The read's cost: one read per system pair per operation, bounded rows and a
# short limit
# --------------------------------------------------------------------------- #
def _body(endpoints) -> str:
    """One proposal block's inner text, as the response capture extracts it."""
    return "\n".join([*(f"{k}: {v}" for k, v in endpoints.items()),
                      "method: value_format_match", "confidence: high",
                      "reason: matched this turn"])


def _vm_reads(calls) -> int:
    return [kind for kind, _ in calls].count("value_mapping")


def _pair(i, *, reverse=False):
    if reverse:
        return {**_REVERSE, "from_value": f"ST-{i}", "to_value": f"U-{i}"}
    return {**_FORWARD, "from_value": f"U-{i}", "to_value": f"ST-{i}"}


def test_many_answer_blocks_for_one_pair_read_the_graph_once(wired, monkeypatch):
    calls: list = []
    stored = [{**_STORED, "id": f"vm-{i}", "from_value": f"U-{i}",
               "to_value": f"ST-{i}"} for i in range(6)]
    _graph_with(monkeypatch, stored, {}, calls)
    blocks = ([_body(_pair(i)) for i in (0, 2, 4)]
              + [_body(_pair(i, reverse=True)) for i in (1, 3, 5)]
              + [_body(_pair(i)) for i in (7, 8)])
    accepted = runtime_app._ingest_value_mapping_proposals(
        blocks, prompt="q", tool_calls_count=0)
    assert accepted == 2  # the six restatements file nothing
    assert _vm_reads(calls) == 1


def test_the_miner_reads_the_graph_once_per_pair(wired, monkeypatch):
    calls: list = []
    _graph_with(monkeypatch, [_STORED], {}, calls)
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: [
        {"table": "units", "field": "unit_ref", "concept": "unit-ref",
         "role": "identifier", "data_type": "text"}])
    values = {"asset-book": ["UNIT-ALPHA", "UNIT-BRAVO", "UNIT-CHARLIE", "UNIT-DELTA"],
              "cell-runner": ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]}
    monkeypatch.setattr(jd, "_sample", lambda sid, tbl, fld: list(values[sid]))
    monkeypatch.setattr(jd, "_neptune_query", lambda c, p=None: [])
    monkeypatch.setattr(jd, "_bump_usage", lambda refs: None)
    filed = jd.mine_value_crosswalks("asset-book", "cell-runner")["crosswalks"]
    assert len(filed) >= 3
    assert _vm_reads(calls) == 1


def test_a_value_mapping_written_in_the_operation_is_read_back(wired, monkeypatch):
    # Filing the first block also writes its ValueMapping (an accept inside
    # the same operation); the two later blocks restate it and file nothing.
    calls: list = []
    stored = [dict(_STORED)]
    _graph_with(monkeypatch, stored, {}, calls)
    new = {**_FORWARD, "to_value": "ST-05"}

    def write(cypher, params):
        stored.append({"id": "vm-new", **new})
        return [{"id": "vm-new"}]

    def log_then_write(**kwargs):
        mapping_id = self_learning.log_proposed_mapping(**kwargs)
        if mapping_id and len(stored) == 1:
            onboard._upsert_value_mapping("vm-new", kwargs, write=write)
        return mapping_id

    blocks = [_body(new), _body({**_REVERSE, "from_value": "ST-05"}), _body(new)]
    accepted = runtime_app._ingest_value_mapping_proposals(
        blocks, prompt="q", tool_calls_count=0, log_mapping=log_then_write)
    assert accepted == 1
    assert _vm_reads(calls) == 2  # once before the write, once after


def test_a_read_over_the_row_cap_files(wired, monkeypatch):
    calls: list = []
    many = [dict(_STORED)] + [
        {**_STORED, "id": f"vm-{i}", "from_value": f"U-{i}"} for i in range(1000)]

    def query(cypher, params=None):
        if "ValueMapping" in cypher and "from_value" in cypher:
            calls.append(dict(params or {}))
            limit = (params or {}).get("lim")
            return [dict(r) for r in (many[:limit] if limit else many)]
        return []
    monkeypatch.setattr(self_learning, "_neptune_query", query)
    # The matching row is in the answer, but the answer is over the cap, so
    # the pair is not judged and the proposal files.
    assert _log(_FORWARD) is not None
    assert len(wired.pending()) == 1
    assert 0 < calls[0]["lim"] <= len(many)


class _GraphClient:
    """A neptune-graph client whose stored-ValueMapping read fails with
    ``error``; every other query answers no rows. Records the timeouts it
    was built with."""

    def __init__(self, sent, read_timeout_s, error, connect_timeout_s=None):
        self.sent, self.read_timeout_s, self.error = sent, read_timeout_s, error
        self.connect_timeout_s = connect_timeout_s

    def execute_query(self, **kwargs):
        self.sent.append({"read_timeout_s": self.read_timeout_s,
                          "connect_timeout_s": self.connect_timeout_s, **kwargs})
        query = kwargs["queryString"]
        if "ValueMapping" in query and "from_value" in query:
            raise self.error
        return {"payload": io.BytesIO(b'{"results": []}')}


def _fake_client(monkeypatch, sent, error):
    monkeypatch.setattr(db, "_neptune_execute", _REAL_EXECUTE)
    monkeypatch.setattr(self_learning, "_neptune_query", _REAL_SL_QUERY)
    monkeypatch.setattr(
        db, "_neptune_client",
        lambda region_name=None, read_timeout_s=None, connect_timeout_s=None:
        _GraphClient(sent, read_timeout_s, error, connect_timeout_s))


def _stored_reads(sent) -> list:
    return [s for s in sent
            if "ValueMapping" in s["queryString"] and "from_value" in s["queryString"]]


def _service_error(code: str, status: int) -> bce.ClientError:
    return bce.ClientError({"Error": {"Code": code, "Message": code},
                            "ResponseMetadata": {"HTTPStatusCode": status}},
                           "ExecuteQuery")


_SERVER_TIMEOUT = bce.ClientError(
    {"Error": {"Code": "UnprocessableException", "Message": "Operation timed out"},
     "reason": "QUERY_TIMEOUT", "ResponseMetadata": {"HTTPStatusCode": 422}},
    "ExecuteQuery")
_READ_TIMEOUT = bce.ReadTimeoutError(endpoint_url="https://graph.invalid")


@pytest.mark.parametrize("error", [_SERVER_TIMEOUT, _READ_TIMEOUT],
                         ids=["server-timeout", "read-timeout"])
def test_a_timed_out_read_files_promptly(wired, monkeypatch, error):
    sent: list = []
    _fake_client(monkeypatch, sent, error)
    started = time.monotonic()
    accepted = runtime_app._ingest_value_mapping_proposals(
        [_body(_FORWARD), _body({**_FORWARD, "to_value": "ST-05"})],
        prompt="q", tool_calls_count=0)
    elapsed = time.monotonic() - started
    reads = _stored_reads(sent)
    assert accepted == 2  # the read failed, so nothing is judged stored
    assert len(reads) == 1  # one read for the pair, not sent again
    assert reads[0]["queryTimeoutMilliseconds"] <= 5_000
    assert reads[0]["read_timeout_s"] <= 10
    assert elapsed < 2.0


# The failures the shared read path resubmits (``graph_resubmit``): no
# connection made, a throttle, a 5xx, a conflict, a dropped connection. The
# optional read makes ONE attempt with a short connect timeout and files.
@pytest.mark.parametrize("error", [
    bce.EndpointConnectionError(endpoint_url="https://graph.invalid"),
    _service_error("ThrottlingException", 429),
    _service_error("InternalServerException", 500),
    _service_error("ServiceUnavailableException", 503),
    _service_error("ConflictException", 409),
    bce.ConnectionClosedError(endpoint_url="https://graph.invalid"),
], ids=["endpoint-connection", "throttle-429", "internal-500", "unavailable-503",
        "conflict-409", "dropped-connection"])
def test_a_retryable_read_failure_is_one_attempt_and_files(wired, monkeypatch, error):
    sent: list = []
    _fake_client(monkeypatch, sent, error)
    started = time.monotonic()
    accepted = runtime_app._ingest_value_mapping_proposals(
        [_body(_FORWARD), _body({**_FORWARD, "to_value": "ST-05"})],
        prompt="q", tool_calls_count=0)
    elapsed = time.monotonic() - started
    reads = _stored_reads(sent)
    assert accepted == 2  # the read failed, so nothing is judged stored
    assert len(reads) == 1  # one attempt: not resubmitted
    assert reads[0]["connect_timeout_s"] is not None
    assert reads[0]["connect_timeout_s"] <= 2
    assert reads[0]["queryTimeoutMilliseconds"] <= 5_000
    assert elapsed < 0.5


def test_the_save_value_mapping_tool_is_one_attempt_on_a_connect_failure(
        wired, monkeypatch):
    sent: list = []
    _fake_client(monkeypatch, sent,
                 bce.EndpointConnectionError(endpoint_url="https://graph.invalid"))
    started = time.monotonic()
    out = json.loads(dts.save_value_mapping(
        **_FORWARD, method="value_format_match", confidence="high",
        reason="matched this turn"))
    assert out["status"] == "logged"
    assert len(_stored_reads(sent)) == 1
    assert time.monotonic() - started < 0.5


def test_other_graph_reads_keep_the_shared_resubmit(monkeypatch):
    # Outside the optional read, a throttled read is still resubmitted.
    from agents import graph_resubmit
    sent: list = []
    monkeypatch.setattr(graph_resubmit, "_sleep", lambda s: None)

    class Throttled:
        def __init__(self, read_timeout_s=None, connect_timeout_s=None):
            self.connect_timeout_s = connect_timeout_s

        def execute_query(self, **kwargs):
            sent.append(self.connect_timeout_s)
            raise _service_error("ThrottlingException", 429)

    monkeypatch.setattr(db, "_neptune_client",
                        lambda region_name=None, read_timeout_s=None,
                        connect_timeout_s=None: Throttled(read_timeout_s,
                                                          connect_timeout_s))
    monkeypatch.setattr(db, "_neptune_execute", _REAL_EXECUTE)
    with pytest.raises(bce.ClientError):
        db._neptune_execute("MATCH (s:System) RETURN s.id AS id")
    assert len(sent) == graph_resubmit.MAX_ATTEMPTS
    assert all(c is None for c in sent)  # the default connect timeout


# --------------------------------------------------------------------------- #
# ValueMapping mutations in another context invalidate the memo
# (``stored_value_mapping_memo``: a process-wide mutation generation)
# --------------------------------------------------------------------------- #
def _in_another_context(fn):
    """Run ``fn`` on a new thread: a separate contextvars context, as a
    concurrent request or job has. Re-raises what it raised."""
    import threading
    errors: list = []

    def run():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


def test_a_write_in_another_context_is_seen_by_the_operation(wired, monkeypatch):
    # The operation reads the pair before another request stores a mapping;
    # its later restatement of that mapping is not filed as new.
    from agents.stored_value_mapping_memo import operation_scope
    calls: list = []
    stored = [dict(_STORED)]
    _graph_with(monkeypatch, stored, {}, calls)
    new = {**_FORWARD, "to_value": "ST-05"}

    def write(cypher, params):
        stored.append({"id": "vm-new", **new})
        return [{"id": "vm-new"}]

    with operation_scope():
        assert _log({**_FORWARD, "to_value": "ST-06"}) is not None  # reads the pair
        _in_another_context(
            lambda: onboard._upsert_value_mapping("vm-new", new, write=write))
        assert _log({**_REVERSE, "from_value": "ST-05"}) is None
    assert _vm_reads(calls) == 2


def test_a_delete_in_another_context_is_seen_by_the_operation(wired, monkeypatch):
    # The operation reads the pair, then a steward requeue deletes the stored
    # mapping; the operation's later proposal for that pair files.
    from agents.stored_value_mapping_memo import operation_scope
    calls: list = []
    stored = [dict(_STORED)]
    _graph_with(monkeypatch, stored, {}, calls)

    def delete(cypher, params=None):
        stored[:] = [r for r in stored if r["id"] != (params or {}).get("nid")]
        return True
    monkeypatch.setattr(self_learning, "_neptune_write", delete)

    with operation_scope():
        assert _log(_FORWARD) is None  # restates the stored mapping
        _in_another_context(lambda: self_learning._undo_graph_artifact(
            {"proposed_type": "value_mapping", "neptune_node_id": "vm-stored-1"}))
        assert _log(_REVERSE) is not None
    assert len(wired.pending()) == 1
    assert _vm_reads(calls) == 2


def test_a_hygiene_retirement_in_another_context_is_seen(wired, monkeypatch):
    from agents import graph_hygiene
    from agents.stored_value_mapping_memo import operation_scope
    calls: list = []
    stored = [dict(_STORED)]
    _graph_with(monkeypatch, stored, {}, calls)

    def write(cypher, params):
        stored[:] = [r for r in stored if r["id"] != params["vmid"]]
        return True
    plan = {"malformed_learned": ["vm-stored-1"], "duplicate_generated": [],
            "kept": []}
    with operation_scope():
        assert _log(_FORWARD) is None
        _in_another_context(lambda: graph_hygiene.apply_value_mapping_hygiene(
            plan, write=write, read=lambda cypher, params=None: []))
        assert _log(_REVERSE) is not None
    assert _vm_reads(calls) == 2


def test_a_mutation_during_the_read_makes_the_next_lookup_read_again(wired):
    # A mutation that lands while the read is in flight: the answer is kept
    # for nothing, the next lookup reads again.
    from agents.stored_value_mapping_memo import memoized, operation_scope
    reads: list = []

    def read_while_another_request_writes():
        reads.append(1)
        if len(reads) == 1:
            _in_another_context(lambda: onboard._upsert_value_mapping(
                "vm-x", dict(_STORED), write=lambda c, p: [{"id": "vm-x"}]))
        return ["rows"]

    with operation_scope():
        memoized(("k",), read_while_another_request_writes)
        memoized(("k",), read_while_another_request_writes)
        memoized(("k",), read_while_another_request_writes)
    assert len(reads) == 2


def test_without_mutations_the_read_stays_memoized(wired):
    from agents.stored_value_mapping_memo import memoized, operation_scope
    reads: list = []
    with operation_scope():
        for _ in range(3):
            memoized(("k",), lambda: reads.append(1) or ["rows"])
    assert len(reads) == 1


def _value_mapping_mutators():
    import agents.graph_hygiene as graph_hygiene
    import agents.graph_snapshot as graph_snapshot
    import agents.server as server
    return [
        onboard._upsert_value_mapping, onboard._reset_delete_graph_artifacts,
        onboard._global_reset_wipe_graph, onboard._retire_equipment_vm_duplicates,
        onboard._sweep_stale_equipment_vms, self_learning._undo_graph_artifact,
        self_learning.reset_all_accepted, self_learning.clear_all_learnings,
        graph_hygiene.apply_value_mapping_hygiene, server._repair_value_mapping_edges,
        graph_snapshot.wipe_graph, graph_snapshot.import_snapshot,
    ]


def test_every_value_mapping_writer_and_deleter_bumps_the_generation():
    from agents import stored_value_mapping_memo as memo
    for fn in _value_mapping_mutators():
        assert getattr(fn, memo.MARKER_ATTR, False) is True, fn.__qualname__


#: Functions whose statements write a ``{label}`` placeholder that is never
#: ValueMapping (the labels are listed at the call site), MERGE only edges
#: onto nodes another marked function handles, or build a statement that only
#: a marked function sends.
_LABEL_WRITERS_NOT_VALUE_MAPPING = {
    ("self_learning.py", "_write_between_served_systems"),  # JoinPath, Correlation
    ("server.py", "_apply_rule_lifecycle"),  # IdentityRule, AuthorityRule, JoinPath
    ("server.py", "_repair_system_edges"),  # schema/rule labels; VMs: _repair_value_mapping_edges
    ("server.py", "_delete_orphan_nodes"),  # Field, SchemaEntry
    ("onboard.py", "_value_mapping_upsert"),  # builds it; _upsert_value_mapping sends it
}


def _statement_texts(fn_node, lookup) -> list[str]:
    """Every non-docstring string a function holds, plus the string
    constants it names (``lookup``: name -> flattened text or ``""``)."""
    import ast
    doc = ast.get_docstring(fn_node, clean=False)
    texts = []
    for node in ast.walk(fn_node):
        if isinstance(node, ast.JoinedStr):
            texts.append("".join(v.value if isinstance(v, ast.Constant) else "{label}"
                                 if "label" in ast.unparse(v.value) else "{x}"
                                 for v in node.values))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value != doc:
            texts.append(node.value)
        elif isinstance(node, ast.Name):
            texts.append(lookup(node.id))
    return texts


def _flatten(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (tuple, list, set, frozenset)):
        return " ".join(_flatten(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in (*value.keys(), *value.values()))
    return ""


def test_no_unmarked_function_writes_value_mappings():
    """Tripwire: a function whose statements create, merge, set or delete a
    ValueMapping (or write a ``{label}`` node) is marked, or listed above
    with the reason its label is never ValueMapping."""
    import ast
    import importlib
    import re
    from pathlib import Path
    from agents import stored_value_mapping_memo as memo
    write = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE)\b")
    agents_dir = Path(self_learning.__file__).parent
    paths = [p for p in sorted(agents_dir.glob("*.py"))
             if "ValueMapping" in p.read_text() or ":{label}" in p.read_text()]
    modules = {p: importlib.import_module(f"agents.{p.stem}") for p in paths}

    def constants_of(module) -> dict:
        return {name: _flatten(value) for name, value in vars(module).items()
                if name.isupper() or name.startswith("_")}
    per_module = {p: constants_of(m) for p, m in modules.items()}
    every = {}
    for consts in per_module.values():
        for name, text in consts.items():
            every.setdefault(name, text)
    unmarked = []
    for path, module in modules.items():
        own = per_module[path]

        def lookup(name, own=own):
            # The module's own constant, else one imported inside a function.
            return own[name] if name in own else every.get(name, "")
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            hits = [t for t in _statement_texts(node, lookup) if write.search(t)
                    and ("ValueMapping" in t or ":{label}" in t)]
            if not hits or (path.name, node.name) in _LABEL_WRITERS_NOT_VALUE_MAPPING:
                continue
            fn = getattr(module, node.name, None)
            if not getattr(fn, memo.MARKER_ATTR, False):
                unmarked.append(f"{path.name}:{node.name}")
    assert unmarked == []
