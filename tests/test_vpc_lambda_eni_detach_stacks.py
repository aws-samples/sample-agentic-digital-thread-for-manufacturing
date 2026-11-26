# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every VPC Lambda a profile or flag change removes in place also detaches
from the VPC before it is deleted.

The drain (tests/test_vpc_lambda_eni_drain_stacks.py) waits for Lambda to reap
a deleted function's ENIs, which it does on its own schedule. To make the
normal path deterministic, each removable VPC function also carries a detach
custom resource (stacks/_eni_drain.py), deleted before the function, that takes
the function out of the VPC so Lambda releases its ENIs under a minute. Delete
order per function: the function's invoker custom resources, then the detach
(which detaches the function), then the function, then the drain, then the group.

* the detach DependsOn the function, so CloudFormation deletes the detach
  (its Delete detaches the function) before it deletes the function;
* each invoker custom resource (the reader-role setup) DependsOn the detach, so
  its own Delete (which drops a database login) finishes before the detach cuts
  the function off from the VPC;
* the detach names the functions it detaches by ARN and logical id, and its
  handlers run outside the VPC;
* its only AWS calls are lambda:GetFunctionConfiguration /
  lambda:UpdateFunctionConfiguration on the named functions,
  cloudformation:DescribeStacks (no resource), and cloudformation:GetTemplate
  on its own stack.

One offline synth of the engine app in the workshop live profile with the hub,
SFC and both mocks on.
"""
from __future__ import annotations

import json

import pytest

from _app_synth import synth_app

CONTEXT = {"with_workshop": "true", "with_simulator": "true", "workshop_story": "live",
           "highbyte_enabled": "true", "highbyte_accept_eula": "true", "sfc_enabled": "true",
           "tulip_enabled": "true"}
DETACH_TYPE = "Custom::ADTVpcLambdaEniDetach"
ON_EVENT = "eni_detach.on_event"
IS_COMPLETE = "eni_detach.is_complete"
#: Stacks whose VPC functions are removed in place (same set the drain covers).
REMOVABLE = ("ADTHighByteOpcuaStack",)


@pytest.fixture(scope="module")
def templates(tmp_path_factory) -> dict[str, dict]:
    return synth_app(tmp_path_factory.mktemp("enidetach"), CONTEXT)


def _resources(template: dict, rtype: str | None = None) -> dict[str, dict]:
    return {lid: r for lid, r in template["Resources"].items() if rtype is None or r["Type"] == rtype}


def _target(value) -> str | None:
    if isinstance(value, dict):
        if "Ref" in value:
            return value["Ref"]
        if "Fn::GetAtt" in value:
            return value["Fn::GetAtt"][0]
    return None


def _vpc_functions(template: dict) -> set[str]:
    return {lid for lid, fn in _resources(template, "AWS::Lambda::Function").items()
            if fn["Properties"].get("VpcConfig", {}).get("SecurityGroupIds")}


def _detaches(template: dict) -> dict[str, dict]:
    return _resources(template, DETACH_TYPE)


def _drain_revision(template: dict) -> str:
    """The one EniDrainRevision the stack's drains carry."""
    values = {r["Properties"].get("EniDrainRevision")
              for r in _resources(template, "Custom::ADTVpcLambdaEniDrain").values()}
    assert len(values) == 1 and None not in values, values
    return values.pop()


@pytest.mark.parametrize("stack", REMOVABLE)
def test_each_removable_vpc_function_has_a_detach(templates, stack):
    template = templates[stack]
    functions = _vpc_functions(template)
    detaches = _detaches(template)
    assert functions, stack
    assert len(detaches) == len(functions), (stack, sorted(detaches), sorted(functions))


@pytest.mark.parametrize("stack", REMOVABLE)
def test_the_detach_is_deleted_before_its_function(templates, stack):
    """detach DependsOn function, so a delete removes the detach (which
    detaches the function) while the function still exists."""
    template = templates[stack]
    resources = template["Resources"]
    functions = _vpc_functions(template)
    for lid, detach in _detaches(template).items():
        waited_on = set(detach.get("DependsOn", []))
        covered = waited_on & functions
        assert covered, (lid, waited_on)
        # The function must NOT depend on the detach (that would delete the
        # function first and there would be nothing to detach).
        for fn in covered:
            assert lid not in resources[fn].get("DependsOn", []), (fn, lid)


@pytest.mark.parametrize("stack", REMOVABLE)
def test_the_detach_names_its_functions_by_arn_and_logical_id(templates, stack):
    template = templates[stack]
    functions = _vpc_functions(template)
    for lid, detach in _detaches(template).items():
        entries = detach["Properties"].get("Functions")
        assert isinstance(entries, list) and entries, lid
        # Plus the drain revision a teardown reads (stacks/_eni_drain_revision.py),
        # the same constant the drain carries.
        assert set(detach["Properties"]) == {"ServiceToken", "Functions", "EniDrainRevision"}, lid
        assert detach["Properties"]["EniDrainRevision"] == _drain_revision(template), lid
        for entry in entries:
            assert set(entry) == {"Arn", "LogicalId"}, entry
            # The logical id names a real function resource in this stack.
            assert entry["LogicalId"] in functions, (lid, entry["LogicalId"])


@pytest.mark.parametrize("stack", REMOVABLE)
def test_the_detach_functions_stay_out_of_the_vpc(templates, stack):
    template = templates[stack]
    handlers = [(lid, r["Properties"])
                for lid, r in _resources(template, "AWS::Lambda::Function").items()
                if r["Properties"].get("Handler") in (ON_EVENT, IS_COMPLETE)]
    seen = {props["Handler"] for _lid, props in handlers}
    assert seen == {ON_EVENT, IS_COMPLETE}, (stack, sorted(seen))
    # Every detach provider's handler functions, across all providers in the
    # stack, stay out of the VPC.
    for lid, props in handlers:
        assert "VpcConfig" not in props, lid


@pytest.mark.parametrize("stack", REMOVABLE)
def test_the_detach_iam_is_scoped_to_the_named_functions(templates, stack):
    template = templates[stack]
    policies = _resources(template, "AWS::IAM::Policy")
    actions = set()
    for props in (r["Properties"] for r in policies.values()):
        for statement in props["PolicyDocument"]["Statement"]:
            act = statement.get("Action")
            for a in ([act] if isinstance(act, str) else act or []):
                if a.startswith("lambda:") or a.startswith("cloudformation:"):
                    actions.add(a)
    assert {"lambda:GetFunctionConfiguration", "lambda:UpdateFunctionConfiguration",
            "cloudformation:DescribeStacks", "cloudformation:GetTemplate"} <= actions, (
        stack, sorted(actions))
    # The lambda write and GetTemplate must not be on "*": they are scoped.
    for props in (r["Properties"] for r in policies.values()):
        for statement in props["PolicyDocument"]["Statement"]:
            act = statement.get("Action")
            acts = [act] if isinstance(act, str) else act or []
            if any(a.startswith("lambda:Update") or a == "cloudformation:GetTemplate"
                   for a in acts):
                assert statement["Resource"] != "*", json.dumps(statement)
