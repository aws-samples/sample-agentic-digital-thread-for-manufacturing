# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every VPC Lambda a profile or flag change removes in place has an ENI drain.

Turning an optional stack off in place deletes its VPC Lambda functions and,
right after, their security groups. Lambda reaps a deleted function's
Hyperplane ENIs on its own schedule, and CloudFormation's delete of the group
gives up after about 17 minutes ("has a dependent object"): the 2026-10-24
a rehearsal left a removable nested stack DELETE_FAILED that way. So each
security group of a VPC function in a removable stack carries a drain custom
resource (stacks/_eni_drain.py), ordered function -> drain -> group on delete:

* the function DependsOn its drain, and the drain references the group, so
  CloudFormation deletes the function, then runs the drain's Delete (wait for
  the group's ENIs, deleting the ones Lambda leaves behind), then deletes the
  group;
* the drain depends on nothing of the function's, so no cycle forms;
* the drain also DependsOn each function's execution role, so the role (which
  Lambda needs to release the function's ENIs itself) outlives the drain;
* the drain's own functions run outside the VPC; is_complete may describe
  network interfaces and delete one only inside the drained functions' VPC
  and subnets;
* one provider per stack, polling every 30 s for up to the framework's one
  hour.

The removable stack is the HighByte half of the
OPC UA stack (``--highbyte off`` drops the hub's setup function in place).

One offline synth of the engine app (tests/_app_synth.py) in the workshop live
profile with the ERP simulator, the hub and SFC on.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from _app_synth import synth_app

REVISION_PATH = Path(__file__).resolve().parents[1] / "stacks" / "_eni_drain_revision.py"


@pytest.fixture(scope="module")
def revision():
    """stacks/_eni_drain_revision.py, loaded by path (it needs no CDK)."""
    spec = importlib.util.spec_from_file_location("eni_drain_revision_for_stacks", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

CONTEXT = {"with_workshop": "true", "with_simulator": "true", "workshop_story": "live",
           "highbyte_enabled": "true", "highbyte_accept_eula": "true", "sfc_enabled": "true"}
#: Stack -> the security groups (logical-id prefix) of its VPC functions.
REMOVABLE = {
    "ADTHighByteOpcuaStack": ("HighByteSetupLambdaSecurityGroup",),
}
DRAIN_TYPE = "Custom::ADTVpcLambdaEniDrain"
ON_EVENT = "eni_drain.on_event"
IS_COMPLETE = "eni_drain.is_complete"


@pytest.fixture(scope="module")
def templates(tmp_path_factory) -> dict[str, dict]:
    return synth_app(tmp_path_factory.mktemp("enidrain"), CONTEXT)


def _resources(template: dict, rtype: str | None = None) -> dict[str, dict]:
    return {lid: r for lid, r in template["Resources"].items() if rtype is None or r["Type"] == rtype}


def _target(value) -> str | None:
    if isinstance(value, dict):
        if "Ref" in value:
            return value["Ref"]
        if "Fn::GetAtt" in value:
            return value["Fn::GetAtt"][0]
    return None


def _refs(node) -> set[str]:
    """Every logical id ``node`` names by Ref or GetAtt, at any depth."""
    found: set[str] = set()
    todo = [node]
    while todo:
        item = todo.pop()
        if isinstance(item, dict):
            target = _target(item)
            if target:
                found.add(target)
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
    return found


def _depends(resources: dict, start: str) -> set[str]:
    """Everything ``start`` waits for: DependsOn plus Ref/GetAtt, transitively."""
    seen, todo = set(), [start]

    def refs(node, out):
        if isinstance(node, dict):
            target = _target(node)
            if target in resources:
                out.add(target)
            for value in node.values():
                refs(value, out)
        elif isinstance(node, list):
            for value in node:
                refs(value, out)

    while todo:
        res = resources[todo.pop()]
        direct = set(res.get("DependsOn", []))
        refs(res.get("Properties", {}), direct)
        for dep in direct - seen:
            seen.add(dep)
            todo.append(dep)
    return seen


def _vpc_functions(template: dict) -> dict[str, list[str]]:
    """VPC function logical id -> the logical ids of its in-stack groups."""
    out = {}
    for lid, fn in _resources(template, "AWS::Lambda::Function").items():
        groups = fn["Properties"].get("VpcConfig", {}).get("SecurityGroupIds", [])
        local = [_target(g) for g in groups if _target(g) in template["Resources"]]
        if groups:
            out[lid] = local
    return out


def _drains(template: dict) -> dict[str, dict]:
    return _resources(template, DRAIN_TYPE)


def _drain_for(template: dict, group: str) -> str:
    matches = [lid for lid, r in _drains(template).items()
               if r["Properties"].get("SecurityGroupId") == {"Fn::GetAtt": [group, "GroupId"]}]
    assert len(matches) == 1, (group, matches)
    return matches[0]


@pytest.mark.parametrize("stack", sorted(REMOVABLE))
def test_each_vpc_function_group_in_a_removable_stack_has_one_drain(templates, stack):
    template = templates[stack]
    vpc_functions = _vpc_functions(template)
    groups = {g for local in vpc_functions.values() for g in local}
    assert vpc_functions and all(vpc_functions.values()), vpc_functions
    assert sorted(groups) == sorted(
        lid for lid in _resources(template, "AWS::EC2::SecurityGroup")
        if lid.startswith(REMOVABLE[stack])), groups
    assert len(_drains(template)) == len(groups)
    for group in groups:
        _drain_for(template, group)


@pytest.mark.parametrize("stack", sorted(REMOVABLE))
def test_delete_order_is_function_then_drain_then_group(templates, stack):
    template = templates[stack]
    resources = template["Resources"]
    for fn, groups in _vpc_functions(template).items():
        for group in groups:
            drain = _drain_for(template, group)
            # The function is deleted before its drain runs ...
            assert drain in resources[fn].get("DependsOn", []), (fn, drain)
            # ... and the drain before the group.
            assert group in _depends(resources, drain), (drain, group)
            # No cycle: the drain and the group never wait for the function.
            assert fn not in _depends(resources, drain), (drain, fn)
            assert drain not in _depends(resources, group), (group, drain)


@pytest.mark.parametrize("stack", sorted(REMOVABLE))
def test_the_drain_functions_stay_out_of_the_vpc_with_describe_and_a_scoped_delete(templates, stack):
    template = templates[stack]
    resources = template["Resources"]
    fns = {r["Properties"]["Handler"]: (lid, r["Properties"])
           for lid, r in _resources(template, "AWS::Lambda::Function").items()
           if r["Properties"].get("Handler") in (ON_EVENT, IS_COMPLETE)}
    assert set(fns) == {ON_EVENT, IS_COMPLETE}
    policies = _resources(template, "AWS::IAM::Policy")
    for handler, (lid, props) in fns.items():
        assert "VpcConfig" not in props, lid
        role = _target(props["Role"])
        managed = json.dumps(resources[role]["Properties"].get("ManagedPolicyArns", []))
        assert "AWSLambdaBasicExecutionRole" in managed and "VPCAccess" not in managed, managed
        statements = [s for p in policies.values()
                      if {"Ref": role} in p["Properties"]["Roles"]
                      for s in p["Properties"]["PolicyDocument"]["Statement"]]
        if handler == IS_COMPLETE:
            # The describe takes no resource; the delete of the leftover
            # interfaces is the one scoped write (its scope is checked below).
            assert [s["Action"] for s in statements] == [
                "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"], statements
            assert statements[0] == {"Action": "ec2:DescribeNetworkInterfaces", "Effect": "Allow",
                                     "Resource": "*"}
        else:
            # on_event reads CloudFormation state for the removal decision:
            # DescribeStacks (no resource) and GetTemplate scoped to the stack.
            expected = [
                {"Action": "cloudformation:DescribeStacks", "Effect": "Allow", "Resource": "*"},
                {"Action": "cloudformation:GetTemplate", "Effect": "Allow",
                 "Resource": {"Ref": "AWS::StackId"}}]
            assert statements == expected, (handler, statements)


def _delete_statements(template: dict) -> list[tuple[str, dict]]:
    """(role, statement) for every inline or attached-policy statement in the
    stack that grants ec2:DeleteNetworkInterface."""
    found = []
    for lid, resource in template["Resources"].items():
        if resource["Type"] == "AWS::IAM::Policy":
            roles = [_target(r) for r in resource["Properties"].get("Roles", [])]
            statements = resource["Properties"]["PolicyDocument"]["Statement"]
        elif resource["Type"] == "AWS::IAM::Role":
            roles = [lid]
            statements = [s for p in resource["Properties"].get("Policies", [])
                          for s in p["PolicyDocument"]["Statement"]]
        else:
            continue
        for statement in statements:
            actions = statement["Action"]
            if "ec2:DeleteNetworkInterface" in ([actions] if isinstance(actions, str) else actions):
                found += [(role, statement) for role in roles]
    return found


@pytest.mark.parametrize("stack", sorted(REMOVABLE))
def test_the_interface_delete_is_scoped_to_the_drained_functions_vpc_and_subnets(templates, stack):
    """ec2:DeleteNetworkInterface has no condition key for a security group,
    a status, an attachment or a description (those gates live in the
    handler), but it does take ec2:Vpc and ec2:Subnet. The grant is limited to
    interfaces in the drained groups' VPC AND in the drained functions'
    subnets, it is given only to the drain's is_complete role, and only once."""
    template = templates[stack]
    resources = template["Resources"]
    (is_complete,) = [lid for lid, r in _resources(template, "AWS::Lambda::Function").items()
                      if r["Properties"].get("Handler") == IS_COMPLETE]
    granted = _delete_statements(template)
    assert [role for role, _s in granted] == [_target(resources[is_complete]["Properties"]["Role"])]
    ((_role, statement),) = granted
    assert statement["Action"] == "ec2:DeleteNetworkInterface" and statement["Effect"] == "Allow"
    resource = json.dumps(statement["Resource"])
    assert statement["Resource"] != "*" and ":network-interface/*" in resource, resource
    assert resource.count("*") == 1, resource
    assert set(statement["Condition"]) == {"ArnEquals"}, statement["Condition"]
    arn_equals = statement["Condition"]["ArnEquals"]
    assert set(arn_equals) == {"ec2:Vpc", "ec2:Subnet"}, arn_equals

    drained = {r["Properties"]["SecurityGroupLogicalId"] for r in _drains(template).values()}
    group_vpcs = {json.dumps(resources[g]["Properties"]["VpcId"]) for g in drained}
    assert len(group_vpcs) == 1, group_vpcs
    vpc = json.dumps(arn_equals["ec2:Vpc"])
    assert ":vpc/" in vpc and next(iter(group_vpcs)) in vpc, vpc

    function_subnets = {json.dumps(subnet)
                        for fn, groups in _vpc_functions(template).items() if set(groups) & drained
                        for subnet in resources[fn]["Properties"]["VpcConfig"]["SubnetIds"]}
    subnet_arns = [json.dumps(arn) for arn in arn_equals["ec2:Subnet"]]
    assert all(":subnet/" in arn for arn in subnet_arns), subnet_arns
    assert len(subnet_arns) == len(function_subnets), (subnet_arns, function_subnets)
    assert {subnet for subnet in function_subnets
            for arn in subnet_arns if subnet in arn} == function_subnets, subnet_arns

    # IAM cannot name the group, so the delete is safe only because each
    # drained group is dedicated: inside the stack only its own functions (each
    # ordered before its drain), its drain and security-group rules reference
    # it, and no output hands it to another stack.
    rule_types = {"AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress"}
    for group in drained:
        users = {lid: r["Type"] for lid, r in resources.items()
                 if group in _refs(r.get("Properties", {}))}
        drain = _drain_for(template, group)
        functions = {lid for lid, rtype in users.items() if rtype == "AWS::Lambda::Function"}
        assert functions, (group, users)
        assert all(drain in resources[fn].get("DependsOn", []) for fn in functions), functions
        assert set(users.values()) <= rule_types | {"AWS::Lambda::Function", DRAIN_TYPE}, users
        assert group not in _refs(template.get("Outputs", {})), group


@pytest.mark.parametrize("stack", sorted(REMOVABLE))
def test_each_drain_finishes_before_its_functions_execution_role_goes(templates, stack):
    """Lambda releases a function's network interfaces with the function's own
    execution role and cannot once the role is deleted, so the interfaces stay
    behind. Each drain therefore DependsOn the execution role of every
    function in its group: a delete runs the function, then the drain (Lambda
    can still release its interfaces during the grace), then the role. The
    role carries the managed VPC access policy Lambda uses for that, and the
    role never waits for the drain (no cycle)."""
    template = templates[stack]
    resources = template["Resources"]
    for fn, groups in _vpc_functions(template).items():
        role = _target(resources[fn]["Properties"]["Role"])
        assert resources[role]["Type"] == "AWS::IAM::Role", role
        managed = json.dumps(resources[role]["Properties"].get("ManagedPolicyArns", []))
        assert "service-role/AWSLambdaVPCAccessExecutionRole" in managed, (role, managed)
        for group in groups:
            drain = _drain_for(template, group)
            assert role in resources[drain].get("DependsOn", []), (drain, role)
            assert drain not in _depends(resources, role), (role, drain)
            assert fn not in _depends(resources, drain), (drain, fn)


@pytest.mark.parametrize("stack", sorted(REMOVABLE))
def test_one_provider_per_stack_polls_for_up_to_an_hour(templates, stack):
    template = templates[stack]
    machines = [r for lid, r in _resources(template, "AWS::StepFunctions::StateMachine").items()
                if lid.startswith("VpcLambdaEniDrain")]
    assert len(machines) == 1
    definition = json.dumps(machines[0]["Properties"]["DefinitionString"])
    assert '\\"IntervalSeconds\\":30' in definition and '\\"MaxAttempts\\":120' in definition, definition
    services = {json.dumps(r["Properties"]["ServiceToken"]) for r in _drains(template).values()}
    assert len(services) == 1


@pytest.mark.parametrize("stack", sorted(REMOVABLE))
def test_the_drain_carries_only_the_group_and_its_revision_so_a_deploy_never_reruns_it(
        templates, revision, stack):
    template = templates[stack]
    groups = set(_resources(template, "AWS::EC2::SecurityGroup"))
    for lid, drain in _drains(template).items():
        assert set(drain["Properties"]) == {
            "ServiceToken", "SecurityGroupId", "SecurityGroupLogicalId", "EniDrainRevision"}, lid
        # The logical id names a real group in this stack (for the removal
        # decision, eni_removal.py).
        assert drain["Properties"]["SecurityGroupLogicalId"] in groups, lid
        # A constant: only a new drain revision sends the drain an Update,
        # which its handlers answer without an AWS call.
        assert drain["Properties"]["EniDrainRevision"] == str(revision.ENI_DRAIN_REVISION), lid


@pytest.mark.parametrize("stack", sorted(REMOVABLE))
def test_a_teardown_reads_this_template_as_current_and_the_earlier_shapes_as_old(
        templates, revision, stack):
    """deploy.sh reads a mock's deployed template before deleting it
    (stacks/_eni_drain_revision.py): this checkout's template must read as
    current, so a teardown deletes it with no extra update, and the two shapes
    an earlier deploy left must read as old, so the teardown updates first:
    drains and detaches without the revision, and no drain or detach at all."""
    template = templates[stack]
    marked = {lid: r for lid, r in template["Resources"].items()
              if r["Type"] in (DRAIN_TYPE, "Custom::ADTVpcLambdaEniDetach")}
    assert {r["Type"] for r in marked.values()} == {DRAIN_TYPE, "Custom::ADTVpcLambdaEniDetach"}
    assert revision.classify(template) == "current"
    unmarked = copy.deepcopy(template)
    for lid in marked:
        del unmarked["Resources"][lid]["Properties"][revision.REVISION_PROPERTY]
    assert revision.classify(unmarked) == "old"
    undrained = copy.deepcopy(template)
    for lid in marked:
        del undrained["Resources"][lid]
    assert revision.classify(undrained) == "old"


DETACH_TYPE = "Custom::ADTVpcLambdaEniDetach"


def _detach_entries(template: dict, fn: str) -> list[tuple[str, dict]]:
    """(detach logical id, entry) for every detach entry that names ``fn``."""
    return [(lid, entry) for lid, detach in _resources(template, DETACH_TYPE).items()
            for entry in detach["Properties"]["Functions"] if entry["LogicalId"] == fn]


def _unprotect(template: dict, fn: str, how: str) -> dict:
    """A copy of ``template`` with one part of ``fn``'s protection taken away
    and everything else left as synthesized."""
    out = copy.deepcopy(template)
    resources = out["Resources"]
    drains = {_drain_for(template, g) for g in _vpc_functions(template)[fn]}
    if how == "order":            # fn no longer DependsOn its drains
        resources[fn]["DependsOn"] = [d for d in resources[fn]["DependsOn"] if d not in drains]
    elif how == "drain":          # its groups' drains are gone
        for drain in drains:
            del resources[drain]
        resources[fn]["DependsOn"] = [d for d in resources[fn]["DependsOn"] if d not in drains]
    elif how == "detach":         # no detach lists it
        for lid, _entry in _detach_entries(template, fn):
            del resources[lid]
    elif how == "other-group":    # its drains name another group of the stack
        others = [g for g in _resources(template, "AWS::EC2::SecurityGroup")
                  if g not in _vpc_functions(template)[fn]]
        for drain in drains:
            resources[drain]["Properties"]["SecurityGroupId"] = {"Fn::GetAtt": [others[0], "GroupId"]}
            resources[drain]["Properties"]["SecurityGroupLogicalId"] = others[0]
    return out


@pytest.mark.parametrize("stack", sorted(REMOVABLE))
def test_the_classifier_ties_each_vpc_function_to_its_own_drain_and_detach(
        templates, revision, stack):
    """stacks/_eni_drain_revision.py reads current only when every VPC function
    is protected by its own drain and detach, through the association the
    synthesized templates carry: each of the function's groups has a drain
    whose SecurityGroupId is GetAtt <group>.GroupId and SecurityGroupLogicalId
    is <group>, and the function DependsOn it; one detach lists the function
    as {Arn: GetAtt <function>.Arn, LogicalId: <function>} and DependsOn it.
    Taking any one part away from any one function reads old, and so does a
    second VPC function with nothing of its own (a stack with two VPC
    functions and only one protected)."""
    template = templates[stack]
    resources = template["Resources"]
    vpc_functions = _vpc_functions(template)
    assert vpc_functions and revision.classify(template) == "current"
    for fn, groups in vpc_functions.items():
        assert groups, fn
        for group in groups:
            drain = _drain_for(template, group)
            assert resources[drain]["Properties"]["SecurityGroupLogicalId"] == group, drain
            assert drain in resources[fn]["DependsOn"], (fn, drain)
        ((detach, entry),) = _detach_entries(template, fn)
        assert entry == {"Arn": {"Fn::GetAtt": [fn, "Arn"]}, "LogicalId": fn}, entry
        assert fn in resources[detach]["DependsOn"], (detach, fn)
        hows = ["order", "drain", "detach"]
        if len(_resources(template, "AWS::EC2::SecurityGroup")) > len(groups):
            hows.append("other-group")
        for how in hows:
            assert revision.classify(_unprotect(template, fn, how)) == "old", (fn, how)
        # A copy of the function in a new group of its own, with no drain or
        # detach: the stack's other drains and detaches don't cover it.
        mixed = copy.deepcopy(template)
        copy_fn = copy.deepcopy(resources[fn])
        copy_fn["Properties"]["VpcConfig"]["SecurityGroupIds"] = [
            {"Fn::GetAtt": [f"{fn}CopySg", "GroupId"]}]
        copy_fn["DependsOn"] = [d for d in copy_fn["DependsOn"]
                                if resources.get(d, {}).get("Type") != DRAIN_TYPE]
        mixed["Resources"][f"{fn}Copy"] = copy_fn
        mixed["Resources"][f"{fn}CopySg"] = copy.deepcopy(resources[groups[0]])
        assert revision.classify(mixed) == "old", fn


def test_the_shared_erp_lambda_group_gets_no_drain(templates):
    """The ERP database stack's shared Lambda group outlives any single
    function (db-init uses it), so turning a function off deletes no security
    group and there is nothing to drain."""
    template = templates["ADTERPDatabaseStack"]
    assert not _drains(template)
