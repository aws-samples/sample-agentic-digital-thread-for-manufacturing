# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A save-slot claim that landed but whose answer was lost.

DynamoDB commits the claim's PutItem, the answer never arrives (a read
timeout), and botocore retries the identical request. Before the fix the
retry met the caller's own fresh running claim, failed its condition, and
the POST answered 409 "another save is in progress" while that orphaned
claim blocked every switch until the 900 s stale bound. The strict claim's
condition now also takes a slot the same owner already holds; each claim
names a fresh owner, so only that retry can meet it.

The table is a real boto3 ``Table`` on the bounded store client
(``memory_model.store_client_config``: two attempts, botocore standard
mode), so the retry is botocore's own. Its HTTP send is answered in process
through botocore's ``before-send`` event by a small DynamoDB double that
evaluates the condition expressions, commits the first PutItem and then
raises a read timeout. No socket is opened and no AWS call is made.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.unit._memory_model_fakes import NEW
from tests.unit.test_llm_settings_memory_model import (  # noqa: F401 - env is a fixture
    FakeSSM,
    Recorder,
    _body,
    _code,
    _post,
    env,
    mm,
)

_ATOM = re.compile(r"^(?:attribute_(not_)?exists\((#\w+)\)|(#\w+) (=|<>|<|>) (:\w+))$")


def _value(typed: dict):
    ((kind, value),) = typed.items()
    return float(value) if kind == "N" else value


def _passes(expression: str, names: dict, values: dict, item: "dict | None") -> bool:
    """A DynamoDB condition of ORs of ANDs of the forms the slot uses.
    Anything else fails the test rather than being guessed."""
    item = item or {}

    def atom(text: str) -> bool:
        match = _ATOM.match(text.strip())
        assert match, f"condition form this double does not evaluate: {text!r}"
        negated, exists_name, name, op, placeholder = match.groups()
        if exists_name is not None:
            present = names[exists_name] in item
            return not present if negated else present
        attr = item.get(names[name])
        if attr is None:
            return False
        left, right = _value(attr), _value(values[placeholder])
        return {"=": left == right, "<>": left != right,
                "<": left < right, ">": left > right}[op]

    return any(all(atom(part) for part in clause.split(" AND "))
               for clause in expression.split(" OR "))


class _Response:
    def __init__(self, status: int, body: dict):
        from botocore.awsrequest import AWSResponse

        class _Raw:
            def __init__(self, data: bytes):
                self._data = data

            def stream(self, **_kwargs):
                yield self._data
        self.response = AWSResponse(
            "https://dynamodb.us-east-1.amazonaws.com/", status,
            {"x-amzn-RequestId": "test", "content-type": "application/x-amz-json-1.0"},
            _Raw(json.dumps(body).encode()))


class _Dynamo:
    """The table behind the wire: items by key; ``lose_answers`` PutItem
    answers to drop (the item is committed, then a read timeout raised)."""

    def __init__(self, lose_answers: int = 0):
        self.items: dict[str, dict] = {}
        self.lose_answers = lose_answers
        self.put_attempts = 0

    def put_item(self, request, **_kwargs):
        from botocore.exceptions import ReadTimeoutError
        body = json.loads(request.body)
        item = body["Item"]
        key = item["system_id"]["S"]
        self.put_attempts += 1
        condition = body.get("ConditionExpression")
        if condition and not _passes(condition, body.get("ExpressionAttributeNames", {}),
                                     body.get("ExpressionAttributeValues", {}),
                                     self.items.get(key)):
            return _Response(400, {
                "__type": "com.amazonaws.dynamodb.v20120810#ConditionalCheckFailedException",
                "message": "The conditional request failed"}).response
        self.items[key] = item
        if self.lose_answers:
            self.lose_answers -= 1
            raise ReadTimeoutError(endpoint_url=request.url)
        return _Response(200, {}).response

    def get_item(self, request, **_kwargs):
        key = json.loads(request.body)["Key"]["system_id"]["S"]
        item = self.items.get(key)
        return _Response(200, {"Item": item} if item is not None else {}).response


@pytest.fixture
def wire(monkeypatch):
    """A real boto3 Table on the bounded store client, answered by a
    :class:`_Dynamo`; botocore's backoff before its retry is zero."""
    import boto3
    import botocore.retries.standard as standard
    monkeypatch.setattr(standard.ExponentialBackoff, "delay_amount", lambda self, context: 0)
    session = boto3.session.Session(aws_access_key_id="testing",
                                    aws_secret_access_key="testing",
                                    region_name="us-east-1")
    table = session.resource("dynamodb", config=mm.store_client_config()).Table(
        "adt-onboarding-sessions")
    dynamo = _Dynamo()
    events = table.meta.client.meta.events
    events.register("before-send.dynamodb.PutItem", dynamo.put_item)
    events.register("before-send.dynamodb.GetItem", dynamo.get_item)
    return table, dynamo


def _running(owner: str, at: float) -> dict:
    return {"status": "running", "job_id": owner, "started_at": at}


def _slot(dynamo: _Dynamo, store) -> dict:
    return dynamo.items[store._key(mm.SAVE_SLOT_KEY)]


# -- the store primitive ------------------------------------------------------------------

def test_a_strict_claim_whose_answer_was_lost_is_taken_on_botocores_retry(wire):
    from agents.job_store import JobStore
    table, dynamo = wire
    dynamo.lose_answers = 1
    store = JobStore("memory-model", table=table)
    assert store.claim_slot(mm.SAVE_SLOT_KEY, _running("owner-one", 1000.0),
                            stale_seconds=mm.SAVE_SLOT_STALE_SECONDS, now=1000.0,
                            strict=True) is True
    assert dynamo.put_attempts == 2          # botocore retried the committed write
    item = _slot(dynamo, store)
    assert item["slot_owner"] == {"S": "owner-one"}
    assert item["slot_status"] == {"S": "running"}
    # The claim is this owner's to heartbeat and release.
    assert store.touch_slot(mm.SAVE_SLOT_KEY, "owner-one", now=1010.0, strict=True)


def test_the_same_owner_clause_does_not_let_another_owner_in(wire):
    from agents.job_store import JobStore
    table, dynamo = wire
    first, second = JobStore("memory-model", table=table), JobStore("memory-model", table=table)
    assert first.claim_slot(mm.SAVE_SLOT_KEY, _running("owner-one", 1000.0),
                            stale_seconds=mm.SAVE_SLOT_STALE_SECONDS, now=1000.0, strict=True)
    assert not second.claim_slot(mm.SAVE_SLOT_KEY, _running("owner-two", 1010.0),
                                 stale_seconds=mm.SAVE_SLOT_STALE_SECONDS, now=1010.0,
                                 strict=True)
    assert _slot(dynamo, first)["slot_owner"] == {"S": "owner-one"}
    # A stale holder is still taken over.
    later = 1000.0 + mm.SAVE_SLOT_STALE_SECONDS + 1
    assert second.claim_slot(mm.SAVE_SLOT_KEY, _running("owner-two", later),
                             stale_seconds=mm.SAVE_SLOT_STALE_SECONDS, now=later, strict=True)
    assert _slot(dynamo, first)["slot_owner"] == {"S": "owner-two"}


def test_the_fail_soft_claim_condition_is_unchanged():
    """Only the strict path adds the same-owner clause; every other slot
    consumer sends exactly the condition it sent before."""
    from agents.job_store import JobStore

    class Recording:
        def __init__(self):
            self.conditions: list = []

        def put_item(self, Item, **kwargs):  # noqa: N803
            self.conditions.append(kwargs)

    before = ("attribute_not_exists(#k) OR attribute_not_exists(#st) OR #st <> :running "
              "OR attribute_not_exists(#sa) OR #sa < :stale_before")
    soft, strict = Recording(), Recording()
    assert JobStore("slot-soft", table=soft).claim_slot(
        "active", _running("a", 1000.0), stale_seconds=300, now=1000.0)
    assert JobStore("slot-strict", table=strict).claim_slot(
        "active", _running("a", 1000.0), stale_seconds=300, now=1000.0, strict=True)
    ((soft_kwargs,), (strict_kwargs,)) = soft.conditions, strict.conditions
    assert soft_kwargs == {
        "ConditionExpression": before,
        "ExpressionAttributeNames": {"#k": "system_id", "#st": "slot_status",
                                     "#sa": "slot_started_at"},
        "ExpressionAttributeValues": {":running": "running", ":stale_before": 700}}
    assert strict_kwargs["ConditionExpression"] == before + " OR #o = :owner"
    assert strict_kwargs["ExpressionAttributeNames"]["#o"] == "slot_owner"
    assert strict_kwargs["ExpressionAttributeValues"][":owner"] == "a"


# -- the POST ------------------------------------------------------------------------------

def test_a_post_whose_claim_answer_was_lost_answers_202_and_starts_the_worker(env, wire):
    from agents.job_store import JobStore
    table, dynamo = wire
    dynamo.lose_answers = 1
    store = JobStore("memory-model", table=table)
    env.setattr("agents.memory_model._SAVE_SLOTS", store)
    started: list = []
    env.setattr("agents.memory_model_job._start_worker",
                lambda target, *args: started.append(args))
    res = _post({"memory_model": NEW}, FakeSSM(), Recorder([]))
    assert _code(res) == 202, _body(res)
    job_id = _body(res)["job_id"]
    assert [args[0] for args in started] == [job_id]   # the worker started, as this owner
    assert dynamo.put_attempts == 2
    item = _slot(dynamo, store)
    assert item["slot_owner"] == {"S": job_id} and item["slot_status"] == {"S": "running"}
