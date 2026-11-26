# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The VPC-Lambda ENI drain's handlers (lambda-eni-drain/eni_drain.py).

The drain is a custom resource deleted after a VPC Lambda function and before
that function's security group (stacks/_eni_drain.py). Its Delete waits, through
the CDK provider framework's isComplete polling, until no network interface
references the group, so CloudFormation's own delete of the group does not
give up on the Lambda Hyperplane ENIs Lambda has not reaped yet.

* Create and Update do nothing and make no AWS call.
* Delete asks, through the shared removal_proven (lambda-eni-drain/eni_removal.py),
  whether its security group is actually being removed. Only then does on_event
  stamp a deadline; otherwise it stamps none, and is_complete completes at once,
  so a rollback that deletes a newly created drain while the group stays no
  longer waits. With a deadline, is_complete keeps answering "not yet" while an
  ENI is on the group and the deadline is ahead, and answers complete when the
  ENIs are gone, at the deadline, on a missing or bad deadline, on a permanent
  AWS error, and on a transient one once the deadline is reached.
* Lambda can leave its network interfaces behind on the group for good
  (``available``, no attachment), and the group's delete then fails. So once a
  short grace period has passed since the Delete started, is_complete deletes
  the interfaces that are provably Lambda leftovers on this group: the group's
  removal proven by on_event, the group the interface's only group,
  status ``available`` with no attachment, not requester-managed, a plain
  ``interface`` type, and a Lambda VPC ENI description. Anything else, and any
  doubt, is left alone. A delete error never fails the Delete: it is logged and
  the drain keeps waiting.
* Neither handler raises, so the framework never answers FAILED: a failed
  Delete would leave the stack DELETE_FAILED or turn a rollback into
  ROLLBACK_FAILED.

Offline: a fake EC2 client, a fake clock, and a stubbed removal decision (or
the real one over a fake CloudFormation client).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

botocore = pytest.importorskip("botocore")
from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = REPO_ROOT / "lambda-eni-drain" / "eni_drain.py"
GROUP = "sg-0d738d98a47ac7d23"
GROUP_LID = "HighByteReaderRoleSg8C684E4A"
NOW = 1_000_000.0


def _load():
    import sys
    if str(HANDLER_PATH.parent) not in sys.path:
        sys.path.insert(0, str(HANDLER_PATH.parent))
    spec = importlib.util.spec_from_file_location("eni_drain_under_test", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def drain():
    return _load()


@pytest.fixture(autouse=True)
def _no_real_ec2(drain, monkeypatch):
    """No test can reach a real EC2 client (this host may have instance
    credentials): a handler that falls back to building one gets a client
    that refuses every call."""
    monkeypatch.setattr(drain, "_ec2_client", lambda: NoCalls())
    monkeypatch.setattr(drain, "_ec2_delete_client", lambda: NoCalls(), raising=False)


class FakeEc2:
    """describe_network_interfaces over pages; an exception raises once per
    queued entry."""

    def __init__(self, pages=None, error=None):
        self.pages = list(pages or [[]])
        self.error = error
        self.calls = []

    def describe_network_interfaces(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        index = len(self.calls) - 1
        page = {"NetworkInterfaces": [
            {"NetworkInterfaceId": eni, "Status": "available",
             "Description": "AWS Lambda VPC ENI-adt-demo-NestedHighByte-HighByteReaderRoleFn"}
            for eni in self.pages[index]]}
        if index + 1 < len(self.pages):
            page["NextToken"] = f"token-{index + 1}"
        return page


class NoCalls:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected EC2 call {name}")


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "DescribeNetworkInterfaces")


def _event(request: str, **extra) -> dict:
    event = {
        "RequestType": request,
        "RequestId": "req-1",
        "StackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/s/1",
        "LogicalResourceId": "VpcLambdaEniDrainHighByteReaderRoleSg",
        "ResourceType": "Custom::ADTVpcLambdaEniDrain",
        "ResourceProperties": {"ServiceToken": "arn:x", "SecurityGroupId": GROUP,
                               "SecurityGroupLogicalId": GROUP_LID},
    }
    if request != "Create":
        event["PhysicalResourceId"] = f"adt-eni-drain-{GROUP}"
    event.update(extra)
    return event


def _proven(*removed):
    removed = set(removed)
    return lambda cfn, stack_id, logical_id: logical_id in removed


def _delete_event(drain, deadline=NOW + 60.0, **extra) -> dict:
    return _event("Delete", **{drain.DEADLINE_KEY: deadline, **extra})


# --- on_event ------------------------------------------------------------------

@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_create_and_update_name_the_group_and_set_no_deadline(drain, request_type):
    result = drain.on_event(_event(request_type), None, clock=lambda: NOW)
    assert result == {"PhysicalResourceId": f"adt-eni-drain-{GROUP}"}


def test_an_update_to_another_group_moves_the_physical_id(drain):
    """A replaced security group gets a new physical id, so CloudFormation
    sends the old one a Delete in cleanup and the old group is drained too."""
    event = _event("Update", ResourceProperties={"SecurityGroupId": "sg-new"})
    assert drain.on_event(event, None, clock=lambda: NOW)["PhysicalResourceId"] == "adt-eni-drain-sg-new"


def test_delete_stamps_the_deadline_when_the_group_removal_is_proven(drain):
    result = drain.on_event(_event("Delete"), None, cfn_client="cfn",
                            removal_proven=_proven(GROUP_LID), clock=lambda: NOW)
    assert result["PhysicalResourceId"] == f"adt-eni-drain-{GROUP}"
    assert result[drain.DEADLINE_KEY] == NOW + drain.DRAIN_BUDGET_SECONDS


def test_delete_stamps_no_deadline_when_the_group_is_not_proven_removed(drain):
    """A rollback that deletes a newly created drain while the group stays: no
    deadline, so is_complete completes at once instead of waiting 55 minutes."""
    result = drain.on_event(_event("Delete"), None, cfn_client="cfn",
                            removal_proven=_proven(), clock=lambda: NOW)
    assert result["PhysicalResourceId"] == f"adt-eni-drain-{GROUP}"
    assert drain.DEADLINE_KEY not in result
    # is_complete then completes at once with no AWS call.
    assert drain.is_complete(result | {"RequestType": "Delete",
                             "ResourceProperties": {"SecurityGroupId": GROUP}},
                             None, ec2=NoCalls(), clock=lambda: NOW) == {"IsComplete": True}


def test_on_event_never_raises_on_a_malformed_delete(drain):
    result = drain.on_event({"RequestType": "Delete"}, None, cfn_client="cfn",
                            removal_proven=_proven(), clock=lambda: NOW)
    assert "PhysicalResourceId" in result
    assert drain.DEADLINE_KEY not in result


# --- is_complete: Create / Update ----------------------------------------------

@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_create_and_update_complete_at_once_with_no_aws_call(drain, request_type):
    assert drain.is_complete(_event(request_type), None, ec2=NoCalls(), clock=lambda: NOW) == {
        "IsComplete": True}


# --- is_complete: Delete --------------------------------------------------------

def test_delete_waits_while_an_eni_is_on_the_group(drain):
    ec2 = FakeEc2(pages=[["eni-1", "eni-2"]])
    result = drain.is_complete(_delete_event(drain), None, ec2=ec2, clock=lambda: NOW)
    assert result == {"IsComplete": False}
    assert ec2.calls == [{"Filters": [{"Name": "group-id", "Values": [GROUP]}]}]


def test_delete_completes_once_the_enis_are_gone(drain):
    result = drain.is_complete(_delete_event(drain), None, ec2=FakeEc2(pages=[[]]), clock=lambda: NOW)
    assert result == {"IsComplete": True}


def test_delete_follows_every_page(drain):
    ec2 = FakeEc2(pages=[[], [], ["eni-3"]])
    assert drain.is_complete(_delete_event(drain), None, ec2=ec2, clock=lambda: NOW) == {
        "IsComplete": False}
    assert [call.get("NextToken") for call in ec2.calls] == [None, "token-1", "token-2"]


def test_delete_completes_at_the_deadline_and_logs_what_remained(drain, capsys):
    ec2 = FakeEc2(pages=[["eni-1"]])
    result = drain.is_complete(_delete_event(drain, deadline=NOW), None, ec2=ec2, clock=lambda: NOW)
    assert result == {"IsComplete": True}
    out = capsys.readouterr().out
    assert "eni-1" in out and GROUP in out


@pytest.mark.parametrize("deadline", [None, "soon", float("nan")])
def test_a_missing_or_bad_deadline_completes_without_waiting(drain, deadline):
    event = _event("Delete")
    if deadline is not None:
        event[drain.DEADLINE_KEY] = deadline
    assert drain.is_complete(event, None, ec2=FakeEc2(pages=[["eni-1"]]), clock=lambda: NOW) == {
        "IsComplete": True}


def test_a_delete_without_a_group_completes_without_an_aws_call(drain):
    event = _delete_event(drain, ResourceProperties={})
    assert drain.is_complete(event, None, ec2=NoCalls(), clock=lambda: NOW) == {"IsComplete": True}


@pytest.mark.parametrize("code", ["UnauthorizedOperation", "AccessDenied", "InvalidParameterValue"])
def test_a_permanent_aws_error_completes(drain, code):
    ec2 = FakeEc2(error=_client_error(code))
    assert drain.is_complete(_delete_event(drain), None, ec2=ec2, clock=lambda: NOW) == {
        "IsComplete": True}


@pytest.mark.parametrize("error", [
    _client_error("RequestLimitExceeded"),
    _client_error("InternalError"),
    EndpointConnectionError(endpoint_url="https://ec2.us-east-1.amazonaws.com"),
])
def test_a_transient_aws_error_keeps_waiting_before_the_deadline(drain, error):
    ec2 = FakeEc2(error=error)
    assert drain.is_complete(_delete_event(drain), None, ec2=ec2, clock=lambda: NOW) == {
        "IsComplete": False}


def test_a_transient_aws_error_at_the_deadline_completes(drain):
    ec2 = FakeEc2(error=_client_error("RequestLimitExceeded"))
    assert drain.is_complete(_delete_event(drain, deadline=NOW), None, ec2=ec2, clock=lambda: NOW) == {
        "IsComplete": True}


def test_any_other_failure_completes_instead_of_raising(drain):
    ec2 = FakeEc2(error=RuntimeError("boom"))
    assert drain.is_complete(_delete_event(drain), None, ec2=ec2, clock=lambda: NOW) == {
        "IsComplete": True}


def test_a_failing_client_constructor_completes_instead_of_raising(drain, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("no region")

    monkeypatch.setattr(drain, "_ec2_client", broken)
    assert drain.is_complete(_delete_event(drain), None, clock=lambda: NOW) == {"IsComplete": True}


def test_the_ec2_client_makes_bounded_calls(drain):
    """Each poll is one bounded call: short connect/read timeouts and a few
    attempts, well inside the isComplete function's own timeout."""
    config = drain.CLIENT_CONFIG
    assert config.connect_timeout <= 5 and config.read_timeout <= 10
    assert config.retries["total_max_attempts"] <= 3
    worst = config.retries["total_max_attempts"] * (config.connect_timeout + config.read_timeout)
    assert worst < drain.IS_COMPLETE_TIMEOUT_SECONDS


def test_the_budget_leaves_room_inside_the_framework_timeout(drain):
    """The deadline must come before the provider framework's total timeout,
    whose expiry answers FAILED ("Operation timed out"), with at least two
    polls of margin."""
    from stacks import _eni_drain

    total = _eni_drain.TOTAL_TIMEOUT.to_seconds()
    interval = _eni_drain.QUERY_INTERVAL.to_seconds()
    assert total == 3600  # the framework's maximum
    assert drain.DRAIN_BUDGET_SECONDS + 2 * interval <= total
    assert _eni_drain.IS_COMPLETE_TIMEOUT.to_seconds() == drain.IS_COMPLETE_TIMEOUT_SECONDS


def test_the_on_event_cfn_budget_covers_the_longest_path(drain):
    """on_event's longest path is the update-cleanup case: DescribeStacks then
    GetTemplate, two calls on the drain's own CloudFormation client. Bound each
    by attempts x (connect + read) plus standard-mode retry backoff (20 s per
    retry) and keep at least 10 s of margin under ON_EVENT_TIMEOUT. Computed
    from the real CFN_CLIENT_CONFIG so a change to it breaks this test."""
    from stacks import _eni_drain

    config = drain.CFN_CLIENT_CONFIG
    attempts = config.retries["total_max_attempts"]
    per_call = attempts * (config.connect_timeout + config.read_timeout) + (attempts - 1) * 20
    worst = 2 * per_call
    on_event_timeout = _eni_drain.ON_EVENT_TIMEOUT.to_seconds()
    assert worst + 10 <= on_event_timeout, (per_call, worst, on_event_timeout)


# --- deleting the leftover Lambda network interfaces ---------------------------

ENI_A, ENI_B = "eni-0aaaaaaaaaaaaaaa1", "eni-0bbbbbbbbbbbbbbb2"
OTHER_GROUP = "sg-0ffffffffffffffff"
#: on_event's clock: the Delete starts here, so the grace and deadline count from it.
STARTED = NOW
_ABSENT = object()


def _leftover(eni=ENI_A, **overrides) -> dict:
    """An interface exactly as Lambda leaves it on the group for good: status
    available, no attachment, not requester-managed, a plain interface, a
    Lambda VPC ENI description, and the drained group as its only group."""
    record = {
        "NetworkInterfaceId": eni,
        "Status": "available",
        "RequesterManaged": False,
        "InterfaceType": "interface",
        "Description": "AWS Lambda VPC ENI-adt-highbyte-reader-setup",
        "Groups": [{"GroupId": GROUP, "GroupName": "HighByteReaderRoleSg"}],
        "SubnetId": "subnet-0123456789abcdef0",
        "VpcId": "vpc-0123456789abcdef0",
    }
    record.update(overrides)
    return {key: value for key, value in record.items() if value is not _ABSENT}


class Ec2:
    """One unpaginated describe over ``records`` and a delete that records each
    call and removes the interface; ``delete_errors`` maps an interface id to
    the exception its delete raises."""

    def __init__(self, records, delete_errors=None):
        self.records = list(records)
        self.delete_errors = dict(delete_errors or {})
        self.describes = []
        self.deletes = []

    def describe_network_interfaces(self, **kwargs):
        self.describes.append(kwargs)
        return {"NetworkInterfaces": list(self.records)}

    def delete_network_interface(self, **kwargs):
        self.deletes.append(kwargs)
        eni = kwargs.get("NetworkInterfaceId")
        if eni in self.delete_errors:
            raise self.delete_errors[eni]
        self.records = [r for r in self.records
                        if not (isinstance(r, dict) and r.get("NetworkInterfaceId") == eni)]
        return {}


def _stamped(drain, **stamps) -> dict:
    """A Delete poll event carrying what on_event stamps when the group's
    removal is proven (its deadline and the end of the grace period)."""
    values = {drain.DEADLINE_KEY: STARTED + drain.DRAIN_BUDGET_SECONDS,
              drain.DELETE_AFTER_KEY: STARTED + drain.GRACE_SECONDS}
    values.update(stamps)
    return _event("Delete", **{key: value for key, value in values.items() if value is not _ABSENT})


def _poll(drain, ec2, at, event=None) -> dict:
    return drain.is_complete(event if event is not None else _stamped(drain), None,
                             ec2=ec2, ec2_delete=ec2, clock=lambda: at)


def _after_grace(drain) -> float:
    return STARTED + drain.GRACE_SECONDS


def _deleted(ec2) -> list:
    return [call.get("NetworkInterfaceId") for call in ec2.deletes]


def test_delete_stamps_the_grace_only_when_its_own_group_removal_is_proven(drain):
    asked = []

    def proven_for(*removed):
        def decide(cfn, stack_id, logical_id):
            asked.append(logical_id)
            return logical_id in removed
        return decide

    proven = drain.on_event(_event("Delete"), None, cfn_client="cfn",
                            removal_proven=proven_for(GROUP_LID), clock=lambda: STARTED)
    assert proven[drain.DELETE_AFTER_KEY] == STARTED + drain.GRACE_SECONDS
    assert proven[drain.DELETE_AFTER_KEY] < proven[drain.DEADLINE_KEY]
    # Proven for some other resource is not proven for this group.
    other = drain.on_event(_event("Delete"), None, cfn_client="cfn",
                           removal_proven=proven_for("SomeOtherGroupSg"), clock=lambda: STARTED)
    assert drain.DELETE_AFTER_KEY not in other and drain.DEADLINE_KEY not in other
    assert asked == [GROUP_LID, GROUP_LID]


def test_a_lambda_leftover_is_deleted_after_the_grace_then_the_group_drains(drain):
    ec2 = Ec2([_leftover(ENI_A), _leftover(ENI_B)])
    assert _poll(drain, ec2, _after_grace(drain)) == {"IsComplete": False}
    assert _deleted(ec2) == [ENI_A, ENI_B]
    # The delete poll does not answer complete itself: the next poll sees the
    # group empty, and only then does CloudFormation delete the group.
    assert _poll(drain, ec2, _after_grace(drain) + 30) == {"IsComplete": True}
    assert len(ec2.deletes) == 2


def test_an_explicit_null_attachment_is_no_attachment(drain):
    ec2 = Ec2([_leftover(Attachment=None)])
    _poll(drain, ec2, _after_grace(drain))
    assert _deleted(ec2) == [ENI_A]


#: One case per way an interface can fail a gate: overrides on the leftover
#: shape, and the gate eni_refusal names for it.
REFUSED = {
    # The drained group must be the interface's only group.
    "a second group": ({"Groups": [{"GroupId": GROUP}, {"GroupId": OTHER_GROUP}]}, "groups"),
    "another group only": ({"Groups": [{"GroupId": OTHER_GROUP}]}, "groups"),
    "no group": ({"Groups": []}, "groups"),
    "groups missing": ({"Groups": _ABSENT}, "groups"),
    "groups not a list": ({"Groups": {"GroupId": GROUP}}, "groups"),
    "group entry not a mapping": ({"Groups": [GROUP]}, "groups"),
    # Status available with no attachment.
    "in use": ({"Status": "in-use"}, "status"),
    "attaching": ({"Status": "attaching"}, "status"),
    "detaching": ({"Status": "detaching"}, "status"),
    "status missing": ({"Status": _ABSENT}, "status"),
    "in use and attached": ({"Status": "in-use", "Attachment": {
        "AttachmentId": "ela-attach-0123", "Status": "attached",
        "InstanceOwnerId": "amazon-aws"}}, "status"),
    "available with an attachment record": ({"Attachment": {
        "AttachmentId": "ela-attach-0123", "Status": "detached"}}, "attachment"),
    "available with an empty attachment": ({"Attachment": {}}, "attachment"),
    # Not requester-managed, and a plain interface (not Lambda's own type).
    "requester-managed": ({"RequesterManaged": True}, "requester-managed"),
    "requester-managed missing": ({"RequesterManaged": _ABSENT}, "requester-managed"),
    "requester-managed not a boolean": ({"RequesterManaged": "false"}, "requester-managed"),
    "lambda's own interface type": ({"InterfaceType": "lambda"}, "interface-type"),
    "another interface type": ({"InterfaceType": "vpc_endpoint"}, "interface-type"),
    "interface type missing": ({"InterfaceType": _ABSENT}, "interface-type"),
    # A Lambda VPC ENI description.
    "another service's description": ({"Description": "ELB app/adt-alb/0123456789abcdef"},
                                      "description"),
    "prefix not at the start": ({"Description": "Copy of AWS Lambda VPC ENI-adt-highbyte"},
                                "description"),
    "prefix in another case": ({"Description": "aws lambda vpc eni-adt-highbyte"}, "description"),
    "description missing": ({"Description": _ABSENT}, "description"),
    "empty description": ({"Description": ""}, "description"),
    # The interface must name itself.
    "id missing": ({"NetworkInterfaceId": _ABSENT}, "id"),
    "not an interface id": ({"NetworkInterfaceId": "i-0123456789abcdef0"}, "id"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_an_interface_failing_a_gate_is_kept_while_a_leftover_beside_it_goes(drain, case):
    """Both directions in one poll: the interface that fails one gate is left
    alone, the leftover next to it is deleted."""
    overrides, _gate = REFUSED[case]
    refused = _leftover(ENI_A, **overrides)
    ec2 = Ec2([refused, _leftover(ENI_B)])
    assert _poll(drain, ec2, _after_grace(drain)) == {"IsComplete": False}
    assert _deleted(ec2) == [ENI_B]
    # The refused interface is still on the group, so the drain keeps waiting.
    assert _poll(drain, ec2, _after_grace(drain) + 30) == {"IsComplete": False}
    assert _deleted(ec2) == [ENI_B]


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_the_refusal_names_the_gate_that_failed(drain, case):
    overrides, gate = REFUSED[case]
    assert drain.eni_refusal(_leftover(**overrides), GROUP) == gate
    assert drain.eni_refusal(_leftover(), GROUP) is None


@pytest.mark.parametrize("record", ["eni-0aaaaaaaaaaaaaaa1", None, ["eni-1"], 7])
def test_an_unexpected_record_shape_is_refused_and_still_waited_on(drain, record):
    assert drain.eni_refusal(record, GROUP) == "shape"
    ec2 = Ec2([record, _leftover(ENI_B)])
    assert _poll(drain, ec2, _after_grace(drain)) == {"IsComplete": False}
    assert _deleted(ec2) == [ENI_B]
    assert _poll(drain, ec2, _after_grace(drain) + 30) == {"IsComplete": False}


def test_the_refused_gate_is_logged_for_each_interface(drain, capsys):
    ec2 = Ec2([_leftover(ENI_A, Status="in-use"), _leftover(ENI_B)])
    _poll(drain, ec2, _after_grace(drain))
    out = capsys.readouterr().out
    assert ENI_A in out and '"refused": "status"' in out
    assert ENI_B in out and "deleted a leftover Lambda network interface" in out


def test_without_the_proven_removal_stamp_nothing_is_deleted(drain):
    """Gate 1 both ways: a deadline alone (no stamp from a proven removal)
    never deletes; the stamp does."""
    late = STARTED + drain.DRAIN_BUDGET_SECONDS - 1
    ec2 = Ec2([_leftover()])
    assert _poll(drain, ec2, late, _stamped(drain, **{drain.DELETE_AFTER_KEY: _ABSENT})) == {
        "IsComplete": False}
    assert ec2.deletes == []
    assert _poll(drain, ec2, late) == {"IsComplete": False}
    assert _deleted(ec2) == [ENI_A]


@pytest.mark.parametrize("value", ["soon", float("nan"), float("inf"), None, [1]])
def test_a_bad_grace_stamp_never_deletes(drain, value):
    ec2 = Ec2([_leftover()])
    _poll(drain, ec2, STARTED + drain.DRAIN_BUDGET_SECONDS - 1,
          _stamped(drain, **{drain.DELETE_AFTER_KEY: value}))
    assert ec2.deletes == []
    _poll(drain, ec2, _after_grace(drain))
    assert _deleted(ec2) == [ENI_A]


def test_nothing_is_deleted_before_the_grace_has_passed(drain):
    ec2 = Ec2([_leftover()])
    assert _poll(drain, ec2, _after_grace(drain) - 1) == {"IsComplete": False}
    assert ec2.deletes == []
    assert _poll(drain, ec2, _after_grace(drain)) == {"IsComplete": False}
    assert _deleted(ec2) == [ENI_A]


def test_lambdas_own_release_inside_the_grace_needs_no_delete(drain):
    """Lambda gets the first chance: when it releases the interfaces during
    the grace, the drain completes without a delete."""
    ec2 = Ec2([_leftover()])
    assert _poll(drain, ec2, STARTED + 30) == {"IsComplete": False}
    ec2.records = []  # Lambda deleted it
    assert _poll(drain, ec2, STARTED + 60) == {"IsComplete": True}
    assert ec2.deletes == []


#: Errors a delete can raise; none of them may fail the Delete.
DELETE_ERRORS = {
    "not found": _client_error("InvalidNetworkInterfaceID.NotFound"),
    "in use": _client_error("InvalidNetworkInterface.InUse"),
    "throttled": _client_error("RequestLimitExceeded"),
    "unauthorized": _client_error("UnauthorizedOperation"),
    "access denied": _client_error("AccessDenied"),
    "network": EndpointConnectionError(endpoint_url="https://ec2.us-east-1.amazonaws.com"),
    "unexpected": RuntimeError("boom"),
}


@pytest.mark.parametrize("case", sorted(DELETE_ERRORS))
def test_a_delete_error_is_logged_and_the_drain_keeps_waiting(drain, case, capsys):
    ec2 = Ec2([_leftover(ENI_A), _leftover(ENI_B)], delete_errors={ENI_A: DELETE_ERRORS[case]})
    assert _poll(drain, ec2, _after_grace(drain)) == {"IsComplete": False}
    # The failed delete does not stop the next one.
    assert _deleted(ec2) == [ENI_A, ENI_B]
    out = capsys.readouterr().out
    assert "could not delete a leftover Lambda network interface" in out and ENI_A in out
    # The next poll tries again ...
    assert _poll(drain, ec2, _after_grace(drain) + 30) == {"IsComplete": False}
    assert _deleted(ec2) == [ENI_A, ENI_B, ENI_A]
    # ... and the existing deadline still ends the wait.
    assert _poll(drain, ec2, STARTED + drain.DRAIN_BUDGET_SECONDS) == {"IsComplete": True}


def test_a_failing_delete_client_keeps_waiting_instead_of_raising(drain, monkeypatch):
    def broken():
        raise RuntimeError("no region")

    monkeypatch.setattr(drain, "_ec2_delete_client", broken)
    ec2 = Ec2([_leftover()])
    assert drain.is_complete(_stamped(drain), None, ec2=ec2, clock=lambda: _after_grace(drain)) == {
        "IsComplete": False}
    assert ec2.deletes == []


def test_a_poll_deletes_at_most_the_per_poll_cap(drain):
    count = drain.MAX_DELETES_PER_POLL + 2
    ec2 = Ec2([_leftover(f"eni-{index:017x}") for index in range(count)])
    _poll(drain, ec2, _after_grace(drain))
    assert len(ec2.deletes) == drain.MAX_DELETES_PER_POLL
    _poll(drain, ec2, _after_grace(drain) + 30)
    assert len(ec2.deletes) == count
    assert _poll(drain, ec2, _after_grace(drain) + 60) == {"IsComplete": True}


def test_the_deadline_still_answers_complete_without_a_delete(drain):
    ec2 = Ec2([_leftover()])
    assert _poll(drain, ec2, STARTED + drain.DRAIN_BUDGET_SECONDS) == {"IsComplete": True}
    assert ec2.deletes == []


def test_the_describe_is_one_unpaginated_call_filtered_to_the_group(drain):
    ec2 = Ec2([_leftover()])
    _poll(drain, ec2, _after_grace(drain))
    assert ec2.describes == [{"Filters": [{"Name": "group-id", "Values": [GROUP]}]}]


def _per_call(config) -> int:
    attempts = config.retries["total_max_attempts"]
    return attempts * (config.connect_timeout + config.read_timeout) + (attempts - 1) * 20


def test_a_delete_poll_fits_inside_the_is_complete_timeout(drain):
    """A delete poll is the describe (one unpaginated call) plus at most the
    per-poll cap of deletes, each bounded like on_event's calls (attempts x
    (connect + read) plus 20 s of standard-mode backoff per retry), with at
    least 10 s of margin. Deletes make one attempt: the next poll is the
    retry, on fresh state."""
    from stacks import _eni_drain

    assert drain.DELETE_CLIENT_CONFIG.retries["total_max_attempts"] == 1
    worst = (_per_call(drain.CLIENT_CONFIG)
             + drain.MAX_DELETES_PER_POLL * _per_call(drain.DELETE_CLIENT_CONFIG))
    assert worst + 10 <= drain.IS_COMPLETE_TIMEOUT_SECONDS, worst
    assert _eni_drain.IS_COMPLETE_TIMEOUT.to_seconds() == drain.IS_COMPLETE_TIMEOUT_SECONDS


def test_the_deadline_and_grace_count_from_the_start_of_the_delete(drain):
    """on_event reads the clock before its removal decision's CloudFormation
    calls, so the time they take is inside the drain's budget, and the poll
    that answers at the deadline (at most one query interval and one
    is_complete timeout after it) still answers well inside CloudFormation's
    one-hour custom resource limit."""
    from stacks import _eni_drain

    now = [STARTED]

    def slow_proof(cfn, stack_id, logical_id):
        now[0] += 2 * _per_call(drain.CFN_CLIENT_CONFIG)  # both calls at their bound
        return True

    stamps = drain.on_event(_event("Delete"), None, cfn_client="cfn",
                            removal_proven=slow_proof, clock=lambda: now[0])
    assert stamps[drain.DEADLINE_KEY] == STARTED + drain.DRAIN_BUDGET_SECONDS
    assert stamps[drain.DELETE_AFTER_KEY] == STARTED + drain.GRACE_SECONDS
    last_answer = (drain.DRAIN_BUDGET_SECONDS + _eni_drain.QUERY_INTERVAL.to_seconds()
                   + drain.IS_COMPLETE_TIMEOUT_SECONDS)
    assert last_answer <= 3600 - 60, last_answer


def test_the_grace_gives_lambda_a_first_chance_and_leaves_the_drain_room(drain):
    from stacks import _eni_drain

    interval = _eni_drain.QUERY_INTERVAL.to_seconds()
    poll = interval + drain.IS_COMPLETE_TIMEOUT_SECONDS
    # Lambda's own release gets several polls before any delete ...
    assert drain.GRACE_SECONDS >= 4 * interval
    # ... and the delete poll plus the poll that sees the group empty use
    # well under half of the drain's budget, so delete retries have room.
    assert drain.GRACE_SECONDS + 2 * poll <= drain.DRAIN_BUDGET_SECONDS // 2


# --- end to end through the real removal decision ------------------------------

class FakeCfn:
    def __init__(self, status, template_ids=()):
        self.status = status
        self.template_ids = tuple(template_ids)

    def describe_stacks(self, StackName):  # noqa: N803
        return {"Stacks": [{"StackStatus": self.status}]}

    def get_template(self, **_kwargs):
        return {"TemplateBody": {"Resources": {lid: {"Type": "X"} for lid in self.template_ids}}}


#: Every path on which a drained group is really removed, with that stack's
#: group logical id: they all get the same delete.
REMOVALS = {
    "HighByte off (the retained OPC UA stack's update cleanup)": (
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", ("HighByteSG5DB85108",),
        "HighByteSetupLambdaSecurityGroupB1C35765"),
    "Tulip off (its nested stack deleted)": (
        "DELETE_IN_PROGRESS", (), "HighByteReaderRoleSg8C684E4A"),
    "a failed create rolling back": ("ROLLBACK_IN_PROGRESS", (), "HighByteReaderRoleSg8C684E4A"),
}
#: Paths on which the group stays: no deadline, no stamp, no EC2 call at all.
KEPT = {
    "an update rollback deleting a newly created drain": ("UPDATE_ROLLBACK_IN_PROGRESS", ()),
    "an update rollback's cleanup": ("UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS", ()),
    "a replacement cleanup with the group still in the template": (
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", (GROUP_LID,)),
    "an ordinary update": ("UPDATE_IN_PROGRESS", ()),
}


def _group_event(group_lid: str) -> dict:
    return _event("Delete", ResourceProperties={"ServiceToken": "arn:x", "SecurityGroupId": GROUP,
                                                "SecurityGroupLogicalId": group_lid})


@pytest.mark.parametrize("path", sorted(REMOVALS))
def test_every_proven_removal_deletes_the_same_leftovers(drain, path):
    status, template_ids, group_lid = REMOVALS[path]
    event = _group_event(group_lid)
    stamps = drain.on_event(event, None, cfn_client=FakeCfn(status, template_ids),
                            clock=lambda: STARTED)
    poll = {**event, **stamps}  # the framework merges on_event's answer into each poll
    ec2 = Ec2([_leftover(ENI_A), _leftover(ENI_B)])
    assert _poll(drain, ec2, _after_grace(drain) - 1, poll) == {"IsComplete": False}
    assert ec2.deletes == []
    assert _poll(drain, ec2, _after_grace(drain), poll) == {"IsComplete": False}
    assert _deleted(ec2) == [ENI_A, ENI_B]
    assert _poll(drain, ec2, _after_grace(drain) + 30, poll) == {"IsComplete": True}


@pytest.mark.parametrize("path", sorted(KEPT))
def test_a_path_that_keeps_the_group_never_touches_an_interface(drain, path):
    status, template_ids = KEPT[path]
    event = _group_event(GROUP_LID)
    stamps = drain.on_event(event, None, cfn_client=FakeCfn(status, template_ids),
                            clock=lambda: STARTED)
    assert drain.DEADLINE_KEY not in stamps and drain.DELETE_AFTER_KEY not in stamps
    assert drain.is_complete({**event, **stamps}, None, ec2=NoCalls(), ec2_delete=NoCalls(),
                             clock=lambda: _after_grace(drain)) == {"IsComplete": True}
