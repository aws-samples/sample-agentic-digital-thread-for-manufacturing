# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A dict-backed stand-in for the shared onboarding-sessions DynamoDB table
as ``JobStore`` uses it (get_item, put_item, delete_item), with switches
that make reads or writes fail the way an unavailable table does. One
instance shared by two JobStores is two processes sharing the table.

It evaluates the two condition expressions a lease uses
(``job_store.LEASE_TAKE_CONDITION``, ``job_store.LEASE_OWNER_CONDITION``) and
the revision conditions of ``JobStore.mutate`` / ``swap_strict``,
and ``hold_back()`` makes reads that do not ask for ConsistentRead see the
table as it was then (DynamoDB's eventually consistent read, at its worst)."""
from __future__ import annotations

import copy

from botocore.exceptions import ClientError


def _conditional_failure(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                  "Message": "The conditional request failed"}}, op)


class SharedTable:
    def __init__(self):
        self.items: dict = {}
        self.fail_reads = False
        self.fail_puts = False
        self.fail_deletes = False
        #: Every put and delete after this many fails (None: none fails).
        self.fail_writes_after = None
        self.writes = 0
        self.reads = 0
        self.consistent_reads = 0
        self._lagging = None

    def hold_back(self) -> None:
        """From now on an eventually consistent read sees the table as it is
        now, whatever is written after."""
        self._lagging = copy.deepcopy(self.items)

    def _write(self, op: str, fail: bool) -> None:
        self.writes += 1
        over = self.fail_writes_after is not None and self.writes > self.fail_writes_after
        if fail or over:
            raise RuntimeError(f"{op}: ProvisionedThroughputExceededException")

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803 - boto3 kwarg
        self.reads += 1
        self.consistent_reads += bool(ConsistentRead)
        if self.fail_reads:
            raise RuntimeError("ProvisionedThroughputExceededException")
        source = self.items if (ConsistentRead or self._lagging is None) else self._lagging
        item = source.get(Key["system_id"])
        return {"Item": dict(item)} if item else {}

    def _condition_holds(self, expression, values, item, names=None) -> bool:
        from agents.job_store import LEASE_OWNER_CONDITION, LEASE_TAKE_CONDITION
        if expression == LEASE_TAKE_CONDITION:
            # The expiry comparison exactly as the expression writes it.
            inclusive = "lease_expires_at <= :now" in expression
            expires_at = int((item or {}).get("lease_expires_at", 0))
            expired = (expires_at <= values[":now"] if inclusive
                       else expires_at < values[":now"])
            return (item is None or expired
                    or item.get("lease_owner") == values[":replace"])
        if expression == LEASE_OWNER_CONDITION:
            return item is not None and item.get("lease_owner") == values[":owner"]
        # The revision conditions of JobStore.mutate / swap_strict.
        names = names or {}
        if expression == "#rev = :rev":
            return item is not None and item.get(names["#rev"]) == values[":rev"]
        if expression == "attribute_not_exists(#k)":
            return item is None
        if expression == "attribute_exists(#k) AND attribute_not_exists(#rev)":
            return item is not None and names["#rev"] not in item
        raise NotImplementedError(f"SharedTable cannot evaluate {expression!r}")

    def put_item(self, Item, ConditionExpression=None,  # noqa: N803
                 ExpressionAttributeValues=None, ExpressionAttributeNames=None, **_kw):
        self._write("PutItem", self.fail_puts)
        if ConditionExpression and not self._condition_holds(
                ConditionExpression, ExpressionAttributeValues or {},
                self.items.get(Item["system_id"]), ExpressionAttributeNames):
            raise _conditional_failure("PutItem")
        self.items[Item["system_id"]] = dict(Item)

    def delete_item(self, Key, ConditionExpression=None,  # noqa: N803
                    ExpressionAttributeValues=None, **_kw):
        self._write("DeleteItem", self.fail_deletes)
        if ConditionExpression and not self._condition_holds(
                ConditionExpression, ExpressionAttributeValues or {},
                self.items.get(Key["system_id"])):
            raise _conditional_failure("DeleteItem")
        self.items.pop(Key["system_id"], None)
        return {}


# ── Lease renewal and the revision conditions, as a block of their own ──────
# Kept apart from the class body above, so another branch that extends the
# class merges cleanly: this block wraps ``SharedTable._condition_holds`` and
# hands every other expression (and any extra arguments) to it.

_CLASS_CONDITION_HOLDS = SharedTable._condition_holds


def _lease_branch_condition_holds(self, expression, values, item, *args, **kwargs):
    """``job_store.LEASE_RENEW_CONDITION`` (``JobStore.lease_renew``) and the
    revision conditions of ``JobStore.mutate`` / ``swap_strict``, read with the
    attribute names ``job_store`` writes; anything else as the class does."""
    from agents.job_store import _REV_ATTR, LEASE_RENEW_CONDITION
    if expression == LEASE_RENEW_CONDITION:
        return (item is not None and item.get("lease_owner") == values[":owner"]
                and int(item.get("lease_expires_at", 0)) > values[":now"])
    if expression == "#rev = :rev":
        return item is not None and item.get(_REV_ATTR) == values[":rev"]
    if expression == "attribute_not_exists(#k)":
        return item is None
    if expression == "attribute_exists(#k) AND attribute_not_exists(#rev)":
        return item is not None and _REV_ATTR not in item
    return _CLASS_CONDITION_HOLDS(self, expression, values, item, *args, **kwargs)


SharedTable._condition_holds = _lease_branch_condition_holds
