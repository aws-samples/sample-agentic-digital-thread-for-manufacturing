# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""VPC-Lambda ENI drain: the two handlers of stacks/_eni_drain.py's provider.

The drain custom resource is deleted after its VPC Lambda function and before
that function's security group. Lambda releases a deleted function's Hyperplane
ENIs on its own schedule, and only while the function's execution role still
grants it the EC2 calls; Lambda can also leave them on the group for good
(status ``available``, no attachment), and CloudFormation's own delete of the
group then gives up with "has a dependent object". On Delete, ``on_event`` asks
the shared ``removal_proven`` (eni_removal.py) whether its security group is
actually being removed; only then does it stamp a deadline and the end of a
short grace period. Without them (a rollback deleting a newly created drain
while the group stays, or any unreadable or unexpected state) ``is_complete``
completes at once with no EC2 call, so the drain never slows or touches a path
on which the group is not going away.

With them, the CDK provider framework calls ``is_complete`` every query
interval until it answers complete. Each poll lists the network interfaces on
the group: none left means complete. Until the grace has passed, Lambda gets
the first chance to release them. After it, the poll deletes each interface
that is provably a Lambda leftover on this group (``eni_refusal`` returns no
gate) and keeps waiting; the next poll sees the group empty. At the deadline it
answers complete whatever is left (and logs it), and CloudFormation deletes the
group, which still gets its own retries.

The hard safety rule: never delete an interface that a running function could
use. An interface is deleted only when ALL of these hold, and any doubt (a
lookup error, a missing or unexpected field) leaves it alone:

1. the drain's own security group is proven to be being removed (on_event
   stamped the grace only after ``removal_proven`` said so);
2. the interface's groups are exactly that one group;
3. its status is ``available`` and it has no attachment;
4. it is not requester-managed, and its type is a plain ``interface`` (the
   ``lambda`` type is a Hyperplane ENI Lambda still manages; the ones it
   leaves behind are plain interfaces);
5. its description starts with ``AWS Lambda VPC ENI-``;
6. the grace period has passed since the Delete started.

The group is dedicated to the functions the drain follows, and CloudFormation
has already deleted them (they DependsOn the drain), so no function in the
stack can use the interface; an attached one is never touched, and EC2 itself
refuses to delete an interface that became attached since the poll listed it.

Create and Update make no AWS call: a deploy never does work here.

Neither handler raises. A raised error would reach the framework, which
answers FAILED once its own timeout passes; a failed Delete leaves the stack
DELETE_FAILED, or turns an update rollback into UPDATE_ROLLBACK_FAILED. A
permanent AWS error on the poll completes at once; a throttle or a network
error is retried by the next poll until the deadline. A delete error of any
kind is logged and the drain keeps waiting, so the deadline is the fallback.
"""
from __future__ import annotations

import json
import math
import time

from eni_removal import removal_proven as _removal_proven

#: How long a Delete waits for the group's ENIs, from the Delete's on_event.
#: The provider framework's total timeout is its one-hour maximum; this leaves
#: it ten polls of margin, so the framework's own timeout (which answers
#: FAILED) is never reached.
DRAIN_BUDGET_SECONDS = 55 * 60
#: How long after the Delete starts the drain leaves the group's interfaces to
#: Lambda before it deletes the leftovers. The function is already deleted
#: when the Delete starts, so this is not what makes a delete safe (the gates
#: are); it keeps the drain from racing a release Lambda is making itself, and
#: stays short because an interface Lambda leaves behind is never released.
GRACE_SECONDS = 5 * 60
#: The is_complete function's timeout (stacks/_eni_drain.py pins it equal).
IS_COMPLETE_TIMEOUT_SECONDS = 120
#: The event keys on_event hands is_complete (the framework merges on_event's
#: answer into every is_complete request). The grace key is stamped only when
#: the group's removal is proven, so its presence is gate 1.
DEADLINE_KEY = "DrainDeadline"
DELETE_AFTER_KEY = "DrainEniDeleteAfter"
PHYSICAL_ID_PREFIX = "adt-eni-drain-"
#: What Lambda writes in the description of every network interface it creates.
LAMBDA_ENI_DESCRIPTION_PREFIX = "AWS Lambda VPC ENI-"
#: The most interfaces one poll deletes; the rest wait for the next poll. Lambda
#: makes one per subnet for a dedicated group, two in the workshop VPC.
MAX_DELETES_PER_POLL = 4
#: Error codes a later poll may outlive.
TRANSIENT_ERROR_CODES = frozenset({
    "RequestLimitExceeded", "Throttling", "ThrottlingException", "InternalError",
    "InternalFailure", "ServiceUnavailable", "Unavailable",
})

try:  # botocore ships with the Lambda runtime; the import is guarded for tests.
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    Config = None
    BotoCoreError = ClientError = ()

#: The poll's describe is at most three attempts at these timeouts.
CLIENT_CONFIG = (Config(connect_timeout=3, read_timeout=10,
                        retries={"total_max_attempts": 3, "mode": "standard"})
                 if Config is not None else None)
#: A delete makes one attempt: the next poll is its retry, on a fresh listing.
#: The describe plus MAX_DELETES_PER_POLL deletes fit inside the is_complete
#: timeout with room to spare; a test pins it.
DELETE_CLIENT_CONFIG = (Config(connect_timeout=2, read_timeout=5,
                               retries={"total_max_attempts": 1, "mode": "standard"})
                        if Config is not None else None)
#: on_event's CloudFormation client is tighter than is_complete's EC2 client so
#: its two-call update-cleanup path (DescribeStacks, GetTemplate) fits inside
#: ON_EVENT_TIMEOUT (stacks/_eni_drain.py) with room to spare; a test pins it.
CFN_CLIENT_CONFIG = (Config(connect_timeout=2, read_timeout=5,
                            retries={"total_max_attempts": 2, "mode": "standard"})
                     if Config is not None else None)


def _log(message: str, **fields) -> None:
    print(json.dumps({"message": message, **fields}, default=str))


def _group(event: dict) -> str:
    return str((event.get("ResourceProperties") or {}).get("SecurityGroupId") or "")


def _group_logical_id(event: dict) -> str:
    return str((event.get("ResourceProperties") or {}).get("SecurityGroupLogicalId") or "")


def _cfn_client():
    import boto3

    return boto3.client("cloudformation", config=CFN_CLIENT_CONFIG)


def on_event(event, _context, *, cfn_client=None, removal_proven=_removal_proven, clock=time.time):
    """Create/Update: name the group, nothing else. Delete: ONLY when the group
    is proven to be being removed, stamp the deadline is_complete waits up to
    and the end of the grace after which it may delete leftovers; otherwise
    stamp neither, so is_complete completes at once and deletes nothing."""
    group = _group(event)
    if event.get("RequestType") != "Delete":
        return {"PhysicalResourceId": PHYSICAL_ID_PREFIX + group}

    # The deadline and the grace count from the start of the Delete, before
    # the removal decision's CloudFormation calls, so their time is inside the
    # budget and the last poll still answers inside CloudFormation's hour.
    started = clock()
    physical = {"PhysicalResourceId": event.get("PhysicalResourceId")
                or PHYSICAL_ID_PREFIX + group}
    stack_id = event.get("StackId") or ""
    cfn = cfn_client if cfn_client is not None else _cfn_client()
    if not removal_proven(cfn, stack_id, _group_logical_id(event)):
        _log("delete: group not proven to be removed; not waiting", group=group)
        return physical
    _log("delete: waiting for the group's network interfaces", group=group,
         budget_seconds=DRAIN_BUDGET_SECONDS, grace_seconds=GRACE_SECONDS)
    return {**physical, DEADLINE_KEY: started + DRAIN_BUDGET_SECONDS,
            DELETE_AFTER_KEY: started + GRACE_SECONDS}


def _ec2_client():
    import boto3  # the Lambda runtime's; imported here so tests need none

    return boto3.client("ec2", config=CLIENT_CONFIG)


def _ec2_delete_client():
    import boto3

    return boto3.client("ec2", config=DELETE_CLIENT_CONFIG)


def _timestamp(event: dict, key: str) -> float | None:
    value = event.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _interfaces(ec2, group: str) -> list:
    """Every network interface record that references ``group`` (all pages).
    The request sets no MaxResults, so EC2 answers it unpaginated in one call;
    the token loop is a guard."""
    found, token = [], None
    while True:
        kwargs = {"Filters": [{"Name": "group-id", "Values": [group]}]}
        if token:
            kwargs["NextToken"] = token
        page = ec2.describe_network_interfaces(**kwargs)
        records = page.get("NetworkInterfaces") or []
        # A record list of an unexpected shape is kept as one record, which
        # eni_refusal refuses, so the drain waits on it and deletes nothing.
        found += records if isinstance(records, list) else [records]
        token = page.get("NextToken")
        if not token:
            return found


def eni_refusal(eni, group: str) -> str | None:
    """The first gate that keeps ``eni`` (one DescribeNetworkInterfaces record)
    from being deleted as a Lambda leftover on ``group``, or None when every
    per-interface gate holds. Gates 1 and 6 (proven removal, grace) are the
    poll's, not the record's. Any missing or unexpected field refuses."""
    if not isinstance(eni, dict):
        return "shape"
    eni_id = eni.get("NetworkInterfaceId")
    if not isinstance(eni_id, str) or not eni_id.startswith("eni-"):
        return "id"
    groups = eni.get("Groups")
    if (not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict)
            or groups[0].get("GroupId") != group or not group):
        return "groups"
    if eni.get("Status") != "available":
        return "status"
    if eni.get("Attachment", None) is not None:
        return "attachment"
    if eni.get("RequesterManaged") is not False:
        return "requester-managed"
    if eni.get("InterfaceType") != "interface":
        return "interface-type"
    description = eni.get("Description")
    if not isinstance(description, str) or not description.startswith(LAMBDA_ENI_DESCRIPTION_PREFIX):
        return "description"
    return None


def _summary(eni, group: str) -> dict | str:
    """What a poll logs about one interface: enough to see why it was or was
    not deleted."""
    if not isinstance(eni, dict):
        return str(eni)
    groups = eni.get("Groups")
    return {"id": eni.get("NetworkInterfaceId"), "status": eni.get("Status"),
            "attached": eni.get("Attachment") is not None,
            "requester_managed": eni.get("RequesterManaged"),
            "interface_type": eni.get("InterfaceType"),
            "groups": [g.get("GroupId") if isinstance(g, dict) else g for g in groups]
            if isinstance(groups, list) else groups,
            "description": eni.get("Description"), "refused": eni_refusal(eni, group)}


def _error(exc: Exception) -> str:
    code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc, ClientError) else ""
    return f"{type(exc).__name__}{' ' + code if code else ''}: {exc}"


def _delete_leftovers(event: dict, interfaces: list, group: str, now: float,
                      ec2_delete) -> None:
    """Delete up to MAX_DELETES_PER_POLL interfaces that pass every gate.
    Logs and swallows every error: the poll keeps waiting either way."""
    delete_after = _timestamp(event, DELETE_AFTER_KEY)
    if delete_after is None:
        return  # gate 1: on_event did not prove the group's removal
    if now < delete_after:
        return  # gate 6: Lambda still has the first chance
    leftovers = [eni for eni in interfaces if eni_refusal(eni, group) is None]
    if not leftovers:
        return
    try:
        client = ec2_delete if ec2_delete is not None else _ec2_delete_client()
    except Exception as exc:  # noqa: BLE001 - a drain never fails a delete
        _log("delete: could not build the EC2 client to delete leftovers; still waiting",
             group=group, error=_error(exc))
        return
    for eni in leftovers[:MAX_DELETES_PER_POLL]:
        eni_id = eni["NetworkInterfaceId"]
        try:
            client.delete_network_interface(NetworkInterfaceId=eni_id)
        except Exception as exc:  # noqa: BLE001 - logged; the next poll retries
            _log("delete: could not delete a leftover Lambda network interface; still waiting",
                 group=group, interface=eni_id, error=_error(exc))
            continue
        _log("delete: deleted a leftover Lambda network interface", group=group,
             interface=eni_id, description=eni.get("Description"))


def _transient(exc: Exception) -> bool:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "") in TRANSIENT_ERROR_CODES
    return isinstance(exc, BotoCoreError)


def is_complete(event, _context, *, ec2=None, ec2_delete=None, clock=time.time):
    """``{"IsComplete": bool}``; never raises (see the module docstring)."""
    if event.get("RequestType") != "Delete":
        return {"IsComplete": True}
    group, deadline = _group(event), _timestamp(event, DEADLINE_KEY)
    if not group or deadline is None:
        _log("delete: nothing to wait on", group=group, deadline=event.get(DEADLINE_KEY))
        return {"IsComplete": True}
    try:
        interfaces = _interfaces(ec2 or _ec2_client(), group)
    except Exception as exc:  # noqa: BLE001 - a drain never fails a delete
        if _transient(exc) and clock() < deadline:
            _log("delete: poll failed, retrying at the next poll", group=group,
                 error=f"{type(exc).__name__}: {exc}")
            return {"IsComplete": False}
        _log("delete: could not poll network interfaces; answering complete", group=group,
             error=f"{type(exc).__name__}: {exc}")
        return {"IsComplete": True}
    if not interfaces:
        _log("delete: no network interface references the group", group=group)
        return {"IsComplete": True}
    try:
        remaining = [_summary(eni, group) for eni in interfaces]
    except Exception:  # noqa: BLE001 - logging never fails a delete
        remaining = [str(eni) for eni in interfaces]
    now = clock()
    if now >= deadline:
        _log("delete: time bound reached with network interfaces still on the group; "
             "answering complete", group=group, remaining=remaining)
        return {"IsComplete": True}
    _log("delete: waiting for network interfaces", group=group, remaining=remaining,
         seconds_left=int(deadline - now))
    try:
        _delete_leftovers(event, interfaces, group, now, ec2_delete)
    except Exception as exc:  # noqa: BLE001 - a drain never fails a delete
        _log("delete: could not delete leftovers this poll; still waiting", group=group,
             error=f"{type(exc).__name__}: {exc}")
    return {"IsComplete": False}
