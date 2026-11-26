# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The VPC-Lambda ENI detach's handlers (lambda-eni-drain/eni_detach.py).

The detach is a custom resource deleted BEFORE its VPC Lambda functions, so on
a delete it can take each function out of the VPC while the function still
exists. Lambda then releases the function's Hyperplane ENIs in under a minute,
so the drain and the surrounding delete waiters no longer race Lambda's own
reaping schedule.

The hard safety rule: never take a function out of the VPC if that function
keeps running afterwards. The detach decides per function, through the shared
``removal_proven`` (lambda-eni-drain/eni_removal.py): it detaches a function
only when CloudFormation state proves that function is being removed (the stack
is deleting or rolling back a failed create, or a succeeded update's cleanup is
removing that logical ID). Every other state and every error detaches nothing,
so the path is then exactly what it was before this change.

* Create and Update make no AWS call.
* Delete detaches each proven-removed, VPC-attached function and records it;
  is_complete waits until each detach settles or the function is gone.
* Neither handler raises, so the framework never answers FAILED.

Offline: a fake Lambda client, a fake CloudFormation client, a fake clock, and
a stubbed removal decision.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

botocore = pytest.importorskip("botocore")
from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = REPO_ROOT / "lambda-eni-drain" / "eni_detach.py"
STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/NestedHighByteStack/abc"
FN_A = "arn:aws:lambda:us-east-1:111122223333:function:adt-highbyte-reader-setup"
FN_B = "arn:aws:lambda:us-east-1:111122223333:function:adt-highbyte"
LID_A = "HighByteReaderRoleFn696114EC"
LID_B = "HighByteReaderFnD8C4EAD6"
NOW = 1_000_000.0


def _load():
    import sys
    if str(HANDLER_PATH.parent) not in sys.path:
        sys.path.insert(0, str(HANDLER_PATH.parent))
    spec = importlib.util.spec_from_file_location("eni_detach_under_test", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def detach():
    return _load()


def _max_functions_per_detach() -> int:
    """The most functions any synthesized detach custom resource carries, from
    a real synth of the live workshop profile (so the budget test tracks the
    real worst case, not a hardcoded number)."""
    import sys
    if str(REPO_ROOT / "tests") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "tests"))
    import tempfile

    from _app_synth import synth_app
    context = {"with_workshop": "true", "with_simulator": "true", "workshop_story": "live", "highbyte_enabled": "true",
               "highbyte_accept_eula": "true", "sfc_enabled": "true", "tulip_enabled": "true"}
    templates = synth_app(Path(tempfile.mkdtemp()), context)
    counts = [len(r["Properties"]["Functions"])
              for t in templates.values() for r in t["Resources"].values()
              if r["Type"] == "Custom::ADTVpcLambdaEniDetach"]
    assert counts, "no detach custom resource synthesized"
    return max(counts)


class FakeLambda:
    def __init__(self, configs, update_error=None, get_error=None):
        self.configs = {name: dict(cfg) for name, cfg in configs.items()}
        self.update_error = update_error
        self.get_error = get_error
        self.updates = []
        self.gets = []

    def get_function_configuration(self, FunctionName):  # noqa: N803
        self.gets.append(FunctionName)
        if self.get_error is not None:
            raise self.get_error
        if FunctionName not in self.configs:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
                "GetFunctionConfiguration")
        return dict(self.configs[FunctionName])

    def update_function_configuration(self, **kwargs):  # noqa: N803
        self.updates.append(kwargs)
        if self.update_error is not None:
            raise self.update_error
        name = kwargs["FunctionName"]
        self.configs[name] = {"VpcConfig": {"SubnetIds": [], "SecurityGroupIds": []},
                              "LastUpdateStatus": "Successful"}
        return dict(self.configs[name])


class NoLambda:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected Lambda call {name}")


def _attached(subnets=("subnet-1",), status="Successful"):
    return {"VpcConfig": {"SubnetIds": list(subnets), "SecurityGroupIds": ["sg-1"]},
            "LastUpdateStatus": status}


def _functions(*pairs):
    return [{"Arn": arn, "LogicalId": lid} for arn, lid in pairs]


def _event(request, functions=None, **extra):
    event = {
        "RequestType": request,
        "RequestId": "req-1",
        "StackId": STACK_ID,
        "LogicalResourceId": "VpcLambdaEniDetachHighByteReaderRoleFn",
        "ResourceType": "Custom::ADTVpcLambdaEniDetach",
        "ResourceProperties": {"ServiceToken": "arn:x",
                               "Functions": functions if functions is not None
                               else _functions((FN_A, LID_A), (FN_B, LID_B))},
    }
    if request != "Create":
        event["PhysicalResourceId"] = "adt-eni-detach-x"
    event.update(extra)
    return event


def _client_error(code, op="UpdateFunctionConfiguration"):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _proven(*removed):
    removed = set(removed)
    return lambda cfn, stack_id, logical_id: logical_id in removed


# --- on_event ------------------------------------------------------------------

@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_create_and_update_make_no_aws_call(detach, request_type):
    result = detach.on_event(_event(request_type), None, lambda_client=NoLambda(),
                             cfn_client="no", removal_proven=_proven(LID_A, LID_B),
                             clock=lambda: NOW)
    assert "PhysicalResourceId" in result


def test_delete_detaches_every_proven_removed_attached_function(detach):
    lam = FakeLambda({FN_A: _attached(), FN_B: _attached()})
    result = detach.on_event(_event("Delete"), None, lambda_client=lam, cfn_client="cfn",
                             removal_proven=_proven(LID_A, LID_B), clock=lambda: NOW)
    assert {u["FunctionName"] for u in lam.updates} == {FN_A, FN_B}
    assert all(u["VpcConfig"] == {"SubnetIds": [], "SecurityGroupIds": []} for u in lam.updates)
    assert set(result.get(detach.DETACHED_KEY)) == {FN_A, FN_B}


def test_delete_detaches_only_the_functions_proven_removed(detach):
    """A per-function decision: one function is being removed, the other
    stays (for example a replacement), so only the removed one is detached."""
    lam = FakeLambda({FN_A: _attached(), FN_B: _attached()})
    result = detach.on_event(_event("Delete"), None, lambda_client=lam, cfn_client="cfn",
                             removal_proven=_proven(LID_A), clock=lambda: NOW)
    assert {u["FunctionName"] for u in lam.updates} == {FN_A}
    assert result.get(detach.DETACHED_KEY) == [FN_A]


def test_delete_detaches_nothing_when_no_function_is_proven_removed(detach):
    """The update-rollback case and the replacement case: the function stays,
    so the safety rule forbids a detach."""
    lam = FakeLambda({FN_A: _attached(), FN_B: _attached()})
    result = detach.on_event(_event("Delete"), None, lambda_client=lam, cfn_client="cfn",
                             removal_proven=_proven(), clock=lambda: NOW)
    assert lam.updates == []
    assert not result.get(detach.DETACHED_KEY)


def test_delete_skips_a_function_that_is_already_gone(detach):
    lam = FakeLambda({FN_B: _attached()})  # FN_A missing
    detach.on_event(_event("Delete"), None, lambda_client=lam, cfn_client="cfn",
                    removal_proven=_proven(LID_A, LID_B), clock=lambda: NOW)
    assert {u["FunctionName"] for u in lam.updates} == {FN_B}


def test_delete_skips_a_function_with_no_vpc_config(detach):
    lam = FakeLambda({FN_A: {"LastUpdateStatus": "Successful"}, FN_B: _attached()})
    detach.on_event(_event("Delete"), None, lambda_client=lam, cfn_client="cfn",
                    removal_proven=_proven(LID_A, LID_B), clock=lambda: NOW)
    assert {u["FunctionName"] for u in lam.updates} == {FN_B}


def test_on_event_never_raises_on_an_update_error(detach):
    lam = FakeLambda({FN_A: _attached()}, update_error=_client_error("TooManyRequestsException"))
    result = detach.on_event(_event("Delete", functions=_functions((FN_A, LID_A))), None,
                             lambda_client=lam, cfn_client="cfn", removal_proven=_proven(LID_A),
                             clock=lambda: NOW)
    assert "PhysicalResourceId" in result
    assert not result.get(detach.DETACHED_KEY)


def test_on_event_never_raises_on_a_malformed_delete(detach):
    result = detach.on_event({"RequestType": "Delete"}, None, lambda_client=NoLambda(),
                             cfn_client="cfn", removal_proven=_proven(LID_A), clock=lambda: NOW)
    assert "PhysicalResourceId" in result


# --- is_complete ---------------------------------------------------------------

@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_is_complete_create_update_complete_with_no_call(detach, request_type):
    assert detach.is_complete(_event(request_type), None, lambda_client=NoLambda(),
                              clock=lambda: NOW) == {"IsComplete": True}


def test_is_complete_without_detached_functions_completes(detach):
    assert detach.is_complete(_event("Delete"), None, lambda_client=NoLambda(),
                              clock=lambda: NOW) == {"IsComplete": True}


def test_is_complete_waits_until_every_detach_settles(detach):
    event = _event("Delete", **{detach.DETACHED_KEY: [FN_A, FN_B], detach.DEADLINE_KEY: NOW + 600})
    lam = FakeLambda({FN_A: _attached(status="InProgress"),
                      FN_B: _attached(subnets=[], status="Successful")})
    assert detach.is_complete(event, None, lambda_client=lam, clock=lambda: NOW) == {
        "IsComplete": False}


def test_is_complete_done_when_all_updates_are_successful(detach):
    event = _event("Delete", **{detach.DETACHED_KEY: [FN_A, FN_B], detach.DEADLINE_KEY: NOW + 600})
    lam = FakeLambda({FN_A: _attached(subnets=[], status="Successful"),
                      FN_B: _attached(subnets=[], status="Successful")})
    assert detach.is_complete(event, None, lambda_client=lam, clock=lambda: NOW) == {
        "IsComplete": True}


def test_is_complete_done_when_a_detached_function_is_gone(detach):
    event = _event("Delete", **{detach.DETACHED_KEY: [FN_A], detach.DEADLINE_KEY: NOW + 600})
    assert detach.is_complete(event, None, lambda_client=FakeLambda({}), clock=lambda: NOW) == {
        "IsComplete": True}


def test_is_complete_completes_at_the_deadline(detach):
    event = _event("Delete", **{detach.DETACHED_KEY: [FN_A], detach.DEADLINE_KEY: NOW})
    lam = FakeLambda({FN_A: _attached(status="InProgress")})
    assert detach.is_complete(event, None, lambda_client=lam, clock=lambda: NOW) == {
        "IsComplete": True}


@pytest.mark.parametrize("error", [
    _client_error("TooManyRequestsException", "GetFunctionConfiguration"),
    EndpointConnectionError(endpoint_url="https://lambda.us-east-1.amazonaws.com"),
])
def test_is_complete_transient_error_keeps_waiting_before_the_deadline(detach, error):
    event = _event("Delete", **{detach.DETACHED_KEY: [FN_A], detach.DEADLINE_KEY: NOW + 600})
    lam = FakeLambda({FN_A: _attached(status="InProgress")}, get_error=error)
    assert detach.is_complete(event, None, lambda_client=lam, clock=lambda: NOW) == {
        "IsComplete": False}


def test_is_complete_permanent_error_completes(detach):
    event = _event("Delete", **{detach.DETACHED_KEY: [FN_A], detach.DEADLINE_KEY: NOW + 600})
    lam = FakeLambda({FN_A: _attached(status="InProgress")},
                     get_error=_client_error("AccessDenied", "GetFunctionConfiguration"))
    assert detach.is_complete(event, None, lambda_client=lam, clock=lambda: NOW) == {
        "IsComplete": True}


def test_is_complete_never_raises_on_an_unexpected_error(detach):
    event = _event("Delete", **{detach.DETACHED_KEY: [FN_A], detach.DEADLINE_KEY: NOW + 600})
    lam = FakeLambda({FN_A: _attached(status="InProgress")}, get_error=RuntimeError("boom"))
    assert detach.is_complete(event, None, lambda_client=lam, clock=lambda: NOW) == {
        "IsComplete": True}


# --- timeout fit ---------------------------------------------------------------

def test_the_budget_leaves_room_inside_the_framework_timeout(detach):
    from stacks import _eni_drain

    total = _eni_drain.TOTAL_TIMEOUT.to_seconds()
    interval = _eni_drain.QUERY_INTERVAL.to_seconds()
    assert detach.DETACH_BUDGET_SECONDS + 2 * interval <= total


def test_the_on_event_budget_covers_the_longest_path(detach):
    """on_event's longest path is the update-cleanup case, which for the detach
    makes four SDK calls PER protected function (DescribeStacks, GetTemplate,
    GetFunctionConfiguration, UpdateFunctionConfiguration). Bound each call by
    attempts x (connect + read) plus standard-mode retry backoff (20 s per
    retry), times the most functions any synthesized detach carries, and keep
    at least 10 s of margin under ON_EVENT_TIMEOUT. Computed from the real
    Config and the real synthesized function counts, so a change to either
    breaks this test."""
    from stacks import _eni_drain

    config = detach.CLIENT_CONFIG
    attempts = config.retries["total_max_attempts"]
    per_call = attempts * (config.connect_timeout + config.read_timeout) + (attempts - 1) * 20
    calls_per_function = 4
    worst = calls_per_function * _max_functions_per_detach() * per_call
    on_event_timeout = _eni_drain.ON_EVENT_TIMEOUT.to_seconds()
    assert worst + 10 <= on_event_timeout, (per_call, worst, on_event_timeout)
