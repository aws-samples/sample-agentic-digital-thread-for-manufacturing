# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for :class:`agents.onboarding_session.OnboardingSessionStore`.

No live AWS. ``moto`` is not in the ``[test]`` extras, so DynamoDB is mocked with
**botocore's Stubber** attached to the resource's underlying client. The Stubber
injects success responses and ``ConditionalCheckFailedException`` client errors so
the tests prove that:

* the legal state-machine path (create -> discovered -> mapped -> resolved ->
  provisioning -> onboarded) issues the correct status-gated conditional writes;
* an out-of-order transition (DDB conditional failure) raises ``OutOfOrderError``;
* an illegal transition *pair* raises ``ValueError`` before any DDB call;
* a second concurrent provisioning claim loses with ``DuplicateProvisionError``;
* an oversized inline payload raises ``ItemTooLargeError`` (no truncation/offload).
"""
from __future__ import annotations

import json

import pytest
from botocore.stub import ANY, Stubber
from boto3.dynamodb.types import TypeSerializer

import agents.onboarding_session as oss
from agents.onboarding_session import (
    DuplicateProvisionError,
    ItemTooLargeError,
    OnboardingSessionStore,
    OutOfOrderError,
    SessionExistsError,
    SessionNotFoundError,
    STATUS_DISCOVERED,
    STATUS_DISCOVERING,
    STATUS_MAPPED,
    STATUS_ONBOARDED,
    STATUS_PROVISIONING,
    STATUS_RESOLVED,
    TABLE_NAME,
)

_SERIALIZER = TypeSerializer()


@pytest.fixture()
def store_and_stub(monkeypatch):
    """A store whose underlying DynamoDB client is intercepted by a Stubber."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    store = OnboardingSessionStore(table_name=TABLE_NAME)
    stubber = Stubber(store._table.meta.client)
    stubber.activate()
    try:
        yield store, stubber
        stubber.assert_no_pending_responses()
    finally:
        stubber.deactivate()


def _gated_update(stubber, *, from_status):
    """Queue a successful UpdateItem and assert it carries the status gate."""
    stubber.add_response(
        "update_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Key": ANY,
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": ANY,
            "ConditionExpression": "#status = :from",
        },
    )


def _conditional_failure(stubber, method):
    stubber.add_client_error(
        method,
        service_error_code="ConditionalCheckFailedException",
        service_message="The conditional request failed",
    )


# --------------------------------------------------------------------------- #
# Legal path
# --------------------------------------------------------------------------- #
def test_full_legal_state_machine_succeeds(store_and_stub):
    store, stubber = store_and_stub

    # create_session -> conditional put (attribute_not_exists)
    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(system_id)",
        },
    )
    # each forward transition is a status-gated conditional update
    _gated_update(stubber, from_status=STATUS_DISCOVERING)   # -> discovered
    _gated_update(stubber, from_status=STATUS_DISCOVERED)    # -> mapped
    _gated_update(stubber, from_status=STATUS_MAPPED)        # -> resolved
    _gated_update(stubber, from_status=STATUS_RESOLVED)      # claim provisioning
    _gated_update(stubber, from_status=STATUS_PROVISIONING)  # finalize onboarded

    created = store.create_session("sys-1", initiated_by="user-abc")
    assert created["status"] == STATUS_DISCOVERING
    assert created["initiated_by"] == "user-abc"
    assert "ttl" in created and "created_at" in created

    store.advance_status("sys-1", STATUS_DISCOVERING, STATUS_DISCOVERED,
                         discovered_tables={"orders": ["id", "qty"]})
    store.advance_status("sys-1", STATUS_DISCOVERED, STATUS_MAPPED,
                         mappings={"orders.id": "OrderId"})
    store.advance_status("sys-1", STATUS_MAPPED, STATUS_RESOLVED,
                         rules={"identity": "ok"})
    store.claim_provisioning("sys-1", provision_execution_arn="arn:aws:states:...:exec")
    store.finalize("sys-1", STATUS_ONBOARDED)


def test_create_session_is_conditional(store_and_stub):
    store, stubber = store_and_stub
    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(system_id)",
        },
    )
    item = store.create_session("sys-2", initiated_by="user-1", connection_type="postgres")
    assert item["connection_type"] == "postgres"
    assert item["status"] == STATUS_DISCOVERING


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def test_get_session_decodes_json_blobs(store_and_stub):
    store, stubber = store_and_stub
    stored = {
        "system_id": "sys-1",
        "status": STATUS_DISCOVERED,
        "discovered_tables": json.dumps({"orders": ["id", "qty"]}),
        "initiated_by": "user-abc",
    }
    wire_item = {k: _SERIALIZER.serialize(v) for k, v in stored.items()}
    stubber.add_response("get_item", {"Item": wire_item},
                         expected_params={"TableName": TABLE_NAME, "Key": ANY})

    session = store.get_session("sys-1")
    assert session["status"] == STATUS_DISCOVERED
    # JSON blob round-trips back into a native dict (stored inline as a string).
    assert session["discovered_tables"] == {"orders": ["id", "qty"]}


def test_get_session_missing_raises(store_and_stub):
    store, stubber = store_and_stub
    stubber.add_response("get_item", {},  # no Item key => not found
                         expected_params={"TableName": TABLE_NAME, "Key": ANY})
    with pytest.raises(SessionNotFoundError):
        store.get_session("nope")


# --------------------------------------------------------------------------- #
# Illegal / out-of-order transitions
# --------------------------------------------------------------------------- #
def test_illegal_transition_pair_raises_before_any_call(store_and_stub):
    store, _stubber = store_and_stub
    # discovering -> resolved is not a legal edge; rejected before touching DDB
    # (no stub queued, so a call would fail the test).
    with pytest.raises(ValueError):
        store.advance_status("sys-1", STATUS_DISCOVERING, STATUS_RESOLVED)


def test_out_of_order_transition_raises(store_and_stub):
    store, stubber = store_and_stub
    # Legal edge discovered->mapped, but the item is not actually 'discovered':
    # DDB rejects the conditional write -> OutOfOrderError.
    _conditional_failure(stubber, "update_item")
    with pytest.raises(OutOfOrderError):
        store.advance_status("sys-1", STATUS_DISCOVERED, STATUS_MAPPED,
                             mappings={"a": "b"})


def test_finalize_rejects_non_terminal_status(store_and_stub):
    store, _stubber = store_and_stub
    with pytest.raises(ValueError):
        store.finalize("sys-1", STATUS_MAPPED)  # not onboarded/failed


def test_create_session_conflict_raises(store_and_stub):
    store, stubber = store_and_stub
    _conditional_failure(stubber, "put_item")
    with pytest.raises(SessionExistsError):
        store.create_session("sys-1", initiated_by="user-abc")


# --------------------------------------------------------------------------- #
# Provisioning claim dedup (concurrent race)
# --------------------------------------------------------------------------- #
def test_second_provisioning_claim_loses(store_and_stub):
    store, stubber = store_and_stub
    # First claim wins (resolved->provisioning succeeds).
    _gated_update(stubber, from_status=STATUS_RESOLVED)
    # Second concurrent claim: status is now 'provisioning', condition fails.
    _conditional_failure(stubber, "update_item")

    store.claim_provisioning("sys-1")  # winner
    with pytest.raises(DuplicateProvisionError):
        store.claim_provisioning("sys-1")  # loser


# --------------------------------------------------------------------------- #
# Inline size guard (no S3 offload — plan R3 deferred)
# --------------------------------------------------------------------------- #
def test_oversize_payload_raises_on_create(store_and_stub):
    store, _stubber = store_and_stub
    # ~400 KB blob exceeds the 380 KB inline threshold -> raises BEFORE any DDB
    # call (no stub queued).
    huge = {"big": "x" * 400_000}
    with pytest.raises(ItemTooLargeError):
        store.create_session("sys-1", initiated_by="u", discovered_tables=huge)


def test_oversize_payload_raises_on_advance(store_and_stub):
    store, _stubber = store_and_stub
    huge = {"big": "x" * 400_000}
    with pytest.raises(ItemTooLargeError):
        store.advance_status("sys-1", STATUS_DISCOVERING, STATUS_DISCOVERED,
                             discovered_tables=huge)


def test_under_threshold_payload_is_allowed(store_and_stub):
    store, stubber = store_and_stub
    _gated_update(stubber, from_status=STATUS_DISCOVERING)
    small = {"orders": ["id", "qty", "customer_id"]}
    # Should not raise; the gated update is consumed.
    store.advance_status("sys-1", STATUS_DISCOVERING, STATUS_DISCOVERED,
                         discovered_tables=small)


def test_cumulative_size_validation_exception_maps_to_item_too_large(store_and_stub):
    """The per-update _check_size guard only sees this update's delta, so a
    cumulative item that crosses the 400 KB cap slips past it and DDB rejects the
    write with a raw size-limit ValidationException. _conditional_update maps that
    DDB-side error to the typed ItemTooLargeError (the backstop), not a raw
    ClientError."""
    store, stubber = store_and_stub
    # A SMALL delta (passes the proactive _check_size), but DDB raises the
    # cumulative item-size ValidationException at write time.
    stubber.add_client_error(
        "update_item",
        service_error_code="ValidationException",
        service_message="Item size has exceeded the maximum allowed size",
    )
    with pytest.raises(ItemTooLargeError):
        store.advance_status("sys-1", STATUS_DISCOVERING, STATUS_DISCOVERED,
                             discovered_tables={"orders": ["id", "qty"]})


def test_other_validation_exception_is_not_swallowed(store_and_stub):
    """A non-size ValidationException must propagate unchanged (no fallback, not
    mis-mapped to ItemTooLargeError)."""
    store, stubber = store_and_stub
    from botocore.exceptions import ClientError
    stubber.add_client_error(
        "update_item",
        service_error_code="ValidationException",
        service_message="ExpressionAttributeValues contains invalid value",
    )
    with pytest.raises(ClientError):
        store.advance_status("sys-1", STATUS_DISCOVERING, STATUS_DISCOVERED,
                             discovered_tables={"orders": ["id"]})


# --------------------------------------------------------------------------- #
# Conditional delete (the chat's start-over of a half-done onboarding)
# --------------------------------------------------------------------------- #
def test_delete_session_if_status_is_conditional_on_the_status(store_and_stub):
    """The delete carries the status gate, so a session that moved on (a
    provisioning claim) is never deleted."""
    store, stubber = store_and_stub
    stubber.add_response(
        "delete_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Key": {"system_id": "sys-1"},
            "ConditionExpression": "#status = :expected",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {":expected": STATUS_RESOLVED},
        },
    )
    store.delete_session_if_status("sys-1", STATUS_RESOLVED)


def test_delete_session_if_status_raises_when_the_status_changed(store_and_stub):
    store, stubber = store_and_stub
    _conditional_failure(stubber, "delete_item")
    with pytest.raises(OutOfOrderError):
        store.delete_session_if_status("sys-1", STATUS_RESOLVED)
