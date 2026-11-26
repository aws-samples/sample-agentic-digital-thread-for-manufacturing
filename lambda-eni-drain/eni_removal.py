# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The per-resource removal decision shared by the ENI detach and drain.

Both custom resources protect one resource (the detach a function, the drain a
security group) and both get a Delete whenever CloudFormation removes the custom
resource, which is not the same as removing the protected resource. A delete
removes the custom resource during a whole-stack delete, during a failed-create
rollback, during a succeeded update's cleanup, during an update rollback that
undoes a create, and during a replacement. Only some of those also remove the
protected resource.

``removal_proven`` answers "is the resource I protect actually being removed?"
fail-closed: it returns True only on positive evidence and False on every other
status and every error, so an unreadable or unexpected state behaves exactly as
the code did before the detach and drain existed (no detach, drain completes at
once). It is proven when:

* the stack is ``DELETE_IN_PROGRESS`` (the whole stack, including a removed
  nested stack, is going);
* the stack is ``ROLLBACK_IN_PROGRESS`` (a failed create: the whole stack goes);
* the stack is ``UPDATE_COMPLETE_CLEANUP_IN_PROGRESS`` and the protected logical
  ID is absent from the current processed template, so a succeeded update's
  cleanup is removing that resource.

An update rollback (``UPDATE_ROLLBACK_*``) is deliberately not proven, even
though it can be removing a resource the failed update created, because it can
equally be undoing an added custom resource while the pre-existing protected
resource stays, and the two cannot be told apart cheaply. That gap is no worse
than the behaviour before this change.
"""
from __future__ import annotations

import json

#: Statuses where the whole stack (and everything in it) is being removed.
_WHOLE_STACK_REMOVAL = frozenset({"DELETE_IN_PROGRESS", "ROLLBACK_IN_PROGRESS"})
#: The one update status where a resource's absence from the processed template
#: proves the (succeeded) update is removing it.
_UPDATE_CLEANUP = "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS"


def _log(message: str, **fields) -> None:
    print(json.dumps({"message": message, **fields}, default=str))


def _stack_status(cfn, stack_id: str) -> str | None:
    try:
        stacks = cfn.describe_stacks(StackName=stack_id).get("Stacks") or []
    except Exception as exc:  # noqa: BLE001 - fail closed, never raise
        _log("removal: could not read the stack status; treating as not removed",
             stack=stack_id, error=f"{type(exc).__name__}: {exc}")
        return None
    return stacks[0].get("StackStatus") if stacks else None


def _logical_id_present(cfn, stack_id: str, logical_id: str) -> bool | None:
    """True/False if the processed template clearly does/does not carry
    ``logical_id``; None if it could not be read."""
    try:
        body = cfn.get_template(StackName=stack_id, TemplateStage="Processed").get("TemplateBody")
    except Exception as exc:  # noqa: BLE001 - fail closed
        _log("removal: could not read the processed template; treating as not removed",
             stack=stack_id, error=f"{type(exc).__name__}: {exc}")
        return None
    if isinstance(body, dict):
        return logical_id in (body.get("Resources") or {})
    if isinstance(body, str):
        # A YAML/JSON string body: the logical ID is a resource key, so its
        # literal absence from the text means the resource is gone. A present
        # substring is treated as still there (fail closed).
        return logical_id in body
    _log("removal: unexpected template body; treating as not removed", stack=stack_id)
    return None


def removal_proven(cfn, stack_id: str, logical_id: str) -> bool:
    """Whether ``logical_id`` in ``stack_id`` is being removed (see module
    docstring). Fail-closed: any uncertainty returns False."""
    if not logical_id:
        return False
    status = _stack_status(cfn, stack_id)
    if status in _WHOLE_STACK_REMOVAL:
        return True
    if status == _UPDATE_CLEANUP:
        present = _logical_id_present(cfn, stack_id, logical_id)
        if present is False:
            _log("removal: update cleanup is removing the resource", stack=stack_id,
                 logical_id=logical_id)
            return True
        return False
    _log("removal: stack status does not prove removal; treating as not removed",
         stack=stack_id, status=status, logical_id=logical_id)
    return False
