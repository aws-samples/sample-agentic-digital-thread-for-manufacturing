# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""VPC-Lambda ENI detach: the two handlers of stacks/_eni_drain.py's detach.

The detach custom resource is deleted BEFORE its VPC Lambda functions (the
function DependsOn the detach), so its Delete runs while the functions still
exist. Taking a function out of the VPC (an UpdateFunctionConfiguration with an
empty VpcConfig) is the Lambda guide's way to have Lambda release the
function's Hyperplane ENIs, which it says takes up to 20 minutes and needs the
function's execution role (the drain keeps the role until it finishes). Lambda
can still leave them on the group for good; the drain, deleted after the
function and before its group, deletes those leftovers (eni_drain.py).

The hard safety rule: never take a function out of the VPC if that function
keeps running afterwards. Detaching a live function cuts it off from its
database. A detach's Delete fires for a function going away and for one that
stays (an update rollback undoes an added detach, a replacement keeps the
function). So the handler decides PER FUNCTION, through the shared
``removal_proven`` (eni_removal.py): it takes a function out of the VPC only
when CloudFormation state proves that function's logical ID is being removed.
Every other state and every error leaves the function attached, so the path is
then exactly what it was before this change.

* Create and Update make no AWS call.
* Delete detaches each proven-removed, VPC-attached function and records it;
  is_complete waits until each detach settles (``LastUpdateStatus`` Successful
  or the function is gone), up to a short budget.
* Neither handler raises. A raised error would reach the framework, which
  answers FAILED once its own timeout passes, leaving the stack DELETE_FAILED;
  a detach that cannot run must not fail the delete, since the drain backstops
  the group.
"""
from __future__ import annotations

import json
import math
import time

from eni_removal import removal_proven as _removal_proven

#: How long is_complete waits for the detach updates to settle. Detaching is a
#: Lambda control-plane update that normally settles in seconds; the budget is
#: a backstop, well inside the provider framework's one-hour total timeout.
DETACH_BUDGET_SECONDS = 5 * 60
#: The event keys on_event hands is_complete (the framework merges on_event's
#: answer into every is_complete request).
DETACHED_KEY = "DetachedFunctionArns"
DEADLINE_KEY = "DetachDeadline"
PHYSICAL_ID = "adt-eni-detach"
#: Error codes a later poll may outlive.
TRANSIENT_ERROR_CODES = frozenset({
    "RequestLimitExceeded", "Throttling", "ThrottlingException", "TooManyRequestsException",
    "InternalError", "InternalFailure", "ServiceException", "ServiceUnavailable", "Unavailable",
})

try:  # botocore ships with the Lambda runtime; the import is guarded for tests.
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    Config = None
    BotoCoreError = ClientError = ()

#: Tight bounds so on_event's worst case (a status read, a get and an update,
#: at most 2 attempts each at connect 2 + read 5) fits inside ON_EVENT_TIMEOUT
#: (stacks/_eni_drain.py), so the handler's own try/except runs before the
#: framework's hard timeout. A test pins this.
CLIENT_CONFIG = (Config(connect_timeout=2, read_timeout=5,
                        retries={"total_max_attempts": 2, "mode": "standard"})
                 if Config is not None else None)


def _log(message: str, **fields) -> None:
    print(json.dumps({"message": message, **fields}, default=str))


def _functions(event: dict) -> list[dict]:
    value = (event.get("ResourceProperties") or {}).get("Functions") or []
    return [f for f in value if isinstance(f, dict) and f.get("Arn") and f.get("LogicalId")]


def _lambda_client():
    import boto3  # the Lambda runtime's; imported here so tests need none

    return boto3.client("lambda", config=CLIENT_CONFIG)


def _cfn_client():
    import boto3

    return boto3.client("cloudformation", config=CLIENT_CONFIG)


def _is_attached(config: dict) -> bool:
    return bool((config.get("VpcConfig") or {}).get("SubnetIds"))


def on_event(event, _context, *, lambda_client=None, cfn_client=None,
             removal_proven=_removal_proven, clock=time.time):
    """Create/Update: nothing. Delete: take each function proven to be being
    removed out of the VPC, and record the ones detached so is_complete waits
    on them."""
    physical = {"PhysicalResourceId": event.get("PhysicalResourceId") or PHYSICAL_ID}
    if event.get("RequestType") != "Delete":
        return physical

    stack_id = event.get("StackId") or ""
    cfn = cfn_client if cfn_client is not None else _cfn_client()
    lam = lambda_client if lambda_client is not None else _lambda_client()
    detached: list[str] = []
    for fn in _functions(event):
        arn, logical_id = fn["Arn"], fn["LogicalId"]
        if not removal_proven(cfn, stack_id, logical_id):
            _log("delete: function not proven to be removed; leaving it attached",
                 function=arn, logical_id=logical_id)
            continue
        try:
            config = lam.get_function_configuration(FunctionName=arn)
        except Exception as exc:  # noqa: BLE001
            _log("delete: could not read the function; skipping it", function=arn,
                 error=f"{type(exc).__name__}: {exc}")
            continue
        if not _is_attached(config):
            _log("delete: function already out of the VPC; nothing to detach", function=arn)
            continue
        try:
            lam.update_function_configuration(FunctionName=arn, VpcConfig={
                "SubnetIds": [], "SecurityGroupIds": []})
        except Exception as exc:  # noqa: BLE001
            _log("delete: could not take the function out of the VPC; the drain will wait it out",
                 function=arn, error=f"{type(exc).__name__}: {exc}")
            continue
        _log("delete: took the function out of the VPC", function=arn)
        detached.append(arn)

    if not detached:
        return physical
    return {**physical, DETACHED_KEY: detached, DEADLINE_KEY: clock() + DETACH_BUDGET_SECONDS}


def _deadline(event: dict) -> float | None:
    try:
        value = float(event.get(DEADLINE_KEY))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _transient(exc: Exception) -> bool:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "") in TRANSIENT_ERROR_CODES
    return isinstance(exc, BotoCoreError)


def is_complete(event, _context, *, lambda_client=None, clock=time.time):
    """``{"IsComplete": bool}``; never raises. Complete once every detached
    function's update has settled (LastUpdateStatus Successful or the function
    is gone), on the deadline, and on a permanent error."""
    if event.get("RequestType") != "Delete":
        return {"IsComplete": True}
    detached = [str(arn) for arn in (event.get(DETACHED_KEY) or []) if arn]
    if not detached:
        return {"IsComplete": True}

    deadline = _deadline(event)
    lam = lambda_client if lambda_client is not None else _lambda_client()
    pending = []
    for arn in detached:
        try:
            config = lam.get_function_configuration(FunctionName=arn)
        except Exception as exc:  # noqa: BLE001 - a detach never fails a delete
            code = (exc.response.get("Error", {}).get("Code", "")
                    if isinstance(exc, ClientError) else "")
            if code == "ResourceNotFoundException":
                continue  # the function is gone: its update settled or it was deleted
            if _transient(exc) and (deadline is None or clock() < deadline):
                _log("delete: poll failed, retrying at the next poll", function=arn,
                     error=f"{type(exc).__name__}: {exc}")
                return {"IsComplete": False}
            _log("delete: could not poll the function; treating its detach as settled",
                 function=arn, error=f"{type(exc).__name__}: {exc}")
            continue
        if config.get("LastUpdateStatus") != "Successful":
            pending.append(arn)

    if not pending:
        _log("delete: every function's detach has settled", functions=detached)
        return {"IsComplete": True}
    if deadline is not None and clock() >= deadline:
        _log("delete: time bound reached with detaches still in progress; answering complete",
             pending=pending)
        return {"IsComplete": True}
    _log("delete: waiting for detaches to settle", pending=pending)
    return {"IsComplete": False}
