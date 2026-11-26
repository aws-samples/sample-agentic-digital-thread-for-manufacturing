# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which ENI drain a deployed template carries (stacks/_eni_drain_revision.py).

An operator path reads a stack's deployed template before turning it off, and
the classifier answers:

* ``current``: every ``Custom::ADTVpcLambdaEniDrain`` and
  ``Custom::ADTVpcLambdaEniDetach`` carries ``EniDrainRevision`` at this
  checkout's revision or later (a whole number, as text or a number), and
  every VPC Lambda function is protected by them: each of its security groups
  in the template has a drain that names that group and that the function
  DependsOn, and a detach lists the function. A template with no VPC Lambda
  function at all is current;
* ``old``: a drain or detach without the revision, below it or with anything
  that is not a whole number, or a VPC Lambda function that lacks any part of
  that protection (two VPC functions with only one protected is old). Each
  link is read for its own shape (a drain's SecurityGroupId is Ref or GetAtt
  GroupId of the group, a detach's Arn is GetAtt Arn of the function), and a
  security group entry that can't be told apart or a VpcConfig that is not a
  mapping reads old, never an outside group or no VPC;
* anything that is not a template, or a VPC function whose security groups
  (in a VpcConfig mapping) or DependsOn can't be read: an error, never a
  class.

The command line reads the template on stdin and prints the class (exit 0) or
the reason it can't (exit 1), and runs on whatever python3 deploy.sh finds.
tests/test_vpc_lambda_eni_drain_stacks.py checks the synthesized stacks against
it; tests/test_eni_drain_rollout.py checks deploy.sh's use of it.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess  # nosec B404 - fixed internal argv lists only
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER = REPO_ROOT / "stacks" / "_eni_drain_revision.py"


def _load():
    spec = importlib.util.spec_from_file_location("eni_drain_revision_under_test", CLASSIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rev():
    return _load()


def _function(vpc: bool = True) -> dict:
    props = {"Handler": "index.handler", "Role": {"Fn::GetAtt": ["FnRole", "Arn"]}}
    if vpc:
        props["VpcConfig"] = {"SecurityGroupIds": [{"Fn::GetAtt": ["FnSg", "GroupId"]}],
                              "SubnetIds": ["subnet-1", "subnet-2"]}
    return {"Type": "AWS::Lambda::Function", "Properties": props}


def _template(marker=None, *, drains: bool = True, detach_marker="same", vpc: bool = True) -> dict:
    """A mock-shaped template, tied together the way stacks/_eni_drain.py
    synthesizes it. ``marker`` None: no EniDrainRevision (the shape a drain had
    before the marker); ``drains`` False: no drain or detach at all (the shape
    before the drain); ``vpc`` False: no VPC function (a standalone mock)."""
    resources = {"Fn": _function(vpc), "FnSg": {"Type": "AWS::EC2::SecurityGroup", "Properties": {}}}
    if drains:
        resources["Fn"]["DependsOn"] = ["Drain"]
        drain = {"ServiceToken": "arn:token", "SecurityGroupId": {"Fn::GetAtt": ["FnSg", "GroupId"]},
                 "SecurityGroupLogicalId": "FnSg"}
        detach = {"ServiceToken": "arn:token2",
                  "Functions": [{"Arn": {"Fn::GetAtt": ["Fn", "Arn"]}, "LogicalId": "Fn"}]}
        if marker is not None:
            drain["EniDrainRevision"] = marker
        detach_value = marker if detach_marker == "same" else detach_marker
        if detach_value is not None:
            detach["EniDrainRevision"] = detach_value
        resources["Drain"] = {"Type": "Custom::ADTVpcLambdaEniDrain", "Properties": drain}
        resources["Detach"] = {"Type": "Custom::ADTVpcLambdaEniDetach", "Properties": detach,
                               "DependsOn": ["Fn"]}
    return {"Resources": resources}


def _current(rev) -> dict:
    return _template(str(rev.ENI_DRAIN_REVISION))


def test_a_template_with_this_revision_on_every_drain_and_detach_is_current(rev):
    assert rev.classify(_current(rev)) == "current"
    assert rev.classify(_template(rev.ENI_DRAIN_REVISION)) == "current"   # a number works too
    assert rev.classify(_template(str(rev.ENI_DRAIN_REVISION + 1))) == "current"


def test_a_drain_without_the_marker_is_old(rev):
    assert rev.classify(_template(None)) == "old"


def test_vpc_functions_with_no_drain_at_all_are_old(rev):
    assert rev.classify(_template(drains=False)) == "old"


def test_a_stack_with_no_vpc_function_is_current(rev):
    assert rev.classify(_template(drains=False, vpc=False)) == "current"
    assert rev.classify({"Resources": {}}) == "current"
    empty = _template(drains=False)
    empty["Resources"]["Fn"]["Properties"]["VpcConfig"] = {"SecurityGroupIds": [], "SubnetIds": []}
    assert rev.classify(empty) == "current"


def test_a_detach_without_the_marker_is_old_even_when_the_drain_has_it(rev):
    assert rev.classify(_template(str(rev.ENI_DRAIN_REVISION), detach_marker=None)) == "old"


@pytest.mark.parametrize("value", ["0", 0, "", "abc", "1.0", True, None, [], {"Ref": "X"}, -1])
def test_a_marker_below_this_revision_or_not_a_whole_number_is_old(rev, value):
    template = _template(str(rev.ENI_DRAIN_REVISION))
    template["Resources"]["Drain"]["Properties"]["EniDrainRevision"] = value
    assert rev.classify(template) == "old"


# Protection is per VPC function: each of its groups in the template has a
# current drain that names the group (SecurityGroupId and
# SecurityGroupLogicalId) and that the function DependsOn, and a current
# detach lists the function (LogicalId, and an Arn that references it).

DRAIN, DETACH = "Custom::ADTVpcLambdaEniDrain", "Custom::ADTVpcLambdaEniDetach"


def _add_function(template: dict, name: str, *, drain: bool = True, order: bool = True,
                  detach: bool = True, marker: str = "1") -> dict:
    """Add VPC function ``name`` in its own group ``<name>Sg``, with the parts
    of its protection asked for: ``drain`` (a drain naming the group),
    ``order`` (the function DependsOn that drain), ``detach`` (a detach listing
    the function)."""
    resources = template["Resources"]
    group = f"{name}Sg"
    resources[group] = {"Type": "AWS::EC2::SecurityGroup", "Properties": {}}
    fn = _function()
    fn["Properties"]["VpcConfig"]["SecurityGroupIds"] = [{"Fn::GetAtt": [group, "GroupId"]}]
    resources[name] = fn
    if drain:
        resources[f"{name}Drain"] = {"Type": DRAIN, "Properties": {
            "ServiceToken": "arn:token", "SecurityGroupId": {"Fn::GetAtt": [group, "GroupId"]},
            "SecurityGroupLogicalId": group, "EniDrainRevision": marker}}
        if order:
            fn["DependsOn"] = [f"{name}Drain"]
    if detach:
        resources[f"{name}Detach"] = {"Type": DETACH, "DependsOn": [name], "Properties": {
            "ServiceToken": "arn:token2", "EniDrainRevision": marker,
            "Functions": [{"Arn": {"Fn::GetAtt": [name, "Arn"]}, "LogicalId": name}]}}
    return template


def test_two_vpc_functions_each_with_its_own_drain_and_detach_are_current(rev):
    assert rev.classify(_add_function(_current(rev), "Other")) == "current"


def test_two_vpc_functions_with_only_one_protected_are_old(rev):
    mixed = _add_function(_current(rev), "Other", drain=False, detach=False)
    assert rev.classify(mixed) == "old"


@pytest.mark.parametrize("missing", ["drain", "order", "detach"])
def test_a_vpc_function_missing_any_part_of_its_protection_is_old(rev, missing):
    template = _add_function(_current(rev), "Other", drain=missing != "drain",
                             order=missing != "order", detach=missing != "detach")
    assert rev.classify(template) == "old"


def test_a_drain_counts_only_for_the_group_it_names(rev):
    # Other's drain names Fn's group: Other's own group has none.
    template = _add_function(_current(rev), "Other")
    props = template["Resources"]["OtherDrain"]["Properties"]
    props["SecurityGroupId"], props["SecurityGroupLogicalId"] = {"Fn::GetAtt": ["FnSg", "GroupId"]}, "FnSg"
    assert rev.classify(template) == "old"
    # A drain whose two group fields disagree names neither.
    template = _current(rev)
    template["Resources"]["Drain"]["Properties"]["SecurityGroupLogicalId"] = "OtherSg"
    assert rev.classify(template) == "old"
    # A Ref names the group as well as GetAtt GroupId does.
    template = _current(rev)
    template["Resources"]["Drain"]["Properties"]["SecurityGroupId"] = {"Ref": "FnSg"}
    assert rev.classify(template) == "current"


def test_a_detach_counts_only_for_the_function_it_names(rev):
    template = _add_function(_current(rev), "Other")
    entry = template["Resources"]["OtherDetach"]["Properties"]["Functions"][0]
    entry["Arn"] = {"Fn::GetAtt": ["Fn", "Arn"]}           # LogicalId Other, Arn of Fn
    assert rev.classify(template) == "old"
    entry["Arn"], entry["LogicalId"] = {"Fn::GetAtt": ["Fn", "Arn"]}, "Fn"   # a second entry for Fn
    assert rev.classify(template) == "old"
    for shape in (None, "Other", [{"LogicalId": "Other"}], ["Other"]):
        template["Resources"]["OtherDetach"]["Properties"]["Functions"] = shape
        assert rev.classify(template) == "old", shape


def test_one_detach_may_list_several_functions(rev):
    template = _add_function(_current(rev), "Other", detach=False)
    template["Resources"]["Detach"]["Properties"]["Functions"].append(
        {"Arn": {"Fn::GetAtt": ["Other", "Arn"]}, "LogicalId": "Other"})
    assert rev.classify(template) == "current"


def test_a_function_in_two_of_the_stack_s_groups_needs_a_drain_on_each(rev):
    template = _add_function(_current(rev), "Other", detach=False)
    resources = template["Resources"]
    del resources["Other"]       # keep OtherSg and OtherDrain; put Fn in both groups
    resources["Fn"]["Properties"]["VpcConfig"]["SecurityGroupIds"].append(
        {"Fn::GetAtt": ["OtherSg", "GroupId"]})
    assert rev.classify(template) == "old"      # Fn does not DependsOn OtherDrain
    resources["Fn"]["DependsOn"].append("OtherDrain")
    assert rev.classify(template) == "current"


def test_a_group_from_outside_the_template_needs_no_drain_but_a_function_needs_one_of_its_own(rev):
    template = _current(rev)
    groups = template["Resources"]["Fn"]["Properties"]["VpcConfig"]["SecurityGroupIds"]
    groups += [{"Fn::ImportValue": "SharedSg"}, "sg-0123456789abcdef0", {"Ref": "GroupParameter"}]
    assert rev.classify(template) == "current"
    # A function that runs only in groups this stack doesn't own has no drain.
    template = _add_function(_current(rev), "Other", drain=False)
    template["Resources"]["Other"]["Properties"]["VpcConfig"]["SecurityGroupIds"] = [
        {"Fn::ImportValue": "SharedSg"}]
    assert rev.classify(template) == "old"


def test_a_depends_on_written_as_one_name_counts(rev):
    template = _current(rev)
    template["Resources"]["Fn"]["DependsOn"] = "Drain"
    assert rev.classify(template) == "current"


@pytest.mark.parametrize("change", [
    {"VpcConfig": {"Fn::If": ["InVpc", {"SecurityGroupIds": [], "SubnetIds": []},
                              {"Ref": "AWS::NoValue"}]}},
    {"VpcConfig": {"SecurityGroupIds": {"Fn::Split": [",", "sg-1,sg-2"]}, "SubnetIds": ["subnet-1"]}},
    {"VpcConfig": {"SubnetIds": ["subnet-1"]}},
    {"DependsOn": 3},
    {"DependsOn": [{"Ref": "Drain"}]},
])
def test_a_vpc_function_whose_groups_or_depends_on_can_t_be_read_raises(rev, change):
    template = _current(rev)
    fn = template["Resources"]["Fn"]
    if "DependsOn" in change:
        fn["DependsOn"] = change["DependsOn"]
    else:
        fn["Properties"]["VpcConfig"] = change["VpcConfig"]
    with pytest.raises(ValueError):
        rev.classify(template)


# Each association field is read for its own shape: a drain's SecurityGroupId
# is Ref <group> or GetAtt <group>.GroupId, a detach entry's Arn is GetAtt
# <function>.Arn. A security group entry that is neither a group of the
# template nor plainly an outside one, and a VpcConfig that is not a mapping,
# read old: never an outside group, never no VPC.

@pytest.mark.parametrize("value", [
    {"Fn::GetAtt": ["FnSg", "VpcId"]},
    {"Fn::GetAtt": "FnSg.VpcId"},
    {"Fn::GetAtt": ["FnSg", "GroupId", "Extra"]},
    {"Fn::GetAtt": ["FnSg"]},
])
def test_a_drain_whose_security_group_id_is_not_the_group_s_id_covers_nothing(rev, value):
    template = _current(rev)
    template["Resources"]["Drain"]["Properties"]["SecurityGroupId"] = value
    assert rev.classify(template) == "old"
    # The group's id in the dotted form of the same GetAtt still names it.
    template["Resources"]["Drain"]["Properties"]["SecurityGroupId"] = {"Fn::GetAtt": "FnSg.GroupId"}
    assert rev.classify(template) == "current"


@pytest.mark.parametrize("entry", [
    {"Fn::If": ["UseOther", {"Fn::GetAtt": ["OtherSg", "GroupId"]}, {"Ref": "AWS::NoValue"}]},
    {"Fn::GetAtt": ["MissingSg", "GroupId"]},
    {"Fn::Select": [0, [{"Fn::GetAtt": ["OtherSg", "GroupId"]}]]},
    {"Fn::ImportValue": "SharedSg", "Ref": "OtherSg"},
    3,
])
def test_a_security_group_entry_that_can_t_be_told_apart_reads_old(rev, entry):
    # Fn has one protected group of its own; the second entry may be a group
    # of this stack with no drain, so it is never read as an outside group.
    template = _current(rev)
    template["Resources"]["OtherSg"] = {"Type": "AWS::EC2::SecurityGroup", "Properties": {}}
    template["Resources"]["Fn"]["Properties"]["VpcConfig"]["SecurityGroupIds"].append(entry)
    assert rev.classify(template) == "old"


@pytest.mark.parametrize("config", [[], "", 0, False, None, "vpc-config", ["sg-1"], 3])
def test_a_vpc_config_that_is_not_a_mapping_reads_old_never_no_vpc(rev, config):
    template = _current(rev)
    template["Resources"]["Fn"]["Properties"]["VpcConfig"] = config
    assert rev.classify(template) == "old"
    # The same function with no drain or detach at all: old too, not no VPC.
    bare = _template(drains=False)
    bare["Resources"]["Fn"]["Properties"]["VpcConfig"] = config
    assert rev.classify(bare) == "old"


@pytest.mark.parametrize("arn", [
    {"Ref": "Fn"},
    {"Fn::GetAtt": ["Fn", "SnapStartResponse"]},
    {"Fn::GetAtt": ["Fn", "Arn", "Extra"]},
])
def test_a_detach_entry_whose_arn_is_not_the_function_s_arn_covers_nothing(rev, arn):
    template = _current(rev)
    template["Resources"]["Detach"]["Properties"]["Functions"][0]["Arn"] = arn
    assert rev.classify(template) == "old"


def test_a_template_body_returned_as_a_json_string_is_read(rev):
    assert rev.classify(json.dumps(_current(rev))) == "current"
    assert rev.classify(json.dumps(_template(None))) == "old"


@pytest.mark.parametrize("bad", [[], "not json", "[]", {"NoResources": {}}, {"Resources": []},
                                 {"Resources": {"X": "not a mapping"}},
                                 {"Resources": {"X": {"Type": "AWS::Lambda::Function",
                                                      "Properties": "nope"}}}])
def test_an_unreadable_template_raises(rev, bad):
    with pytest.raises(ValueError):
        rev.classify(bad)


def _cli(stdin: str) -> subprocess.CompletedProcess:
    # deploy.sh runs the classifier with whatever python3 is on PATH, which
    # may be older than the venv's, so the CLI is run with that one.
    python3 = shutil.which("python3")
    if not python3:
        pytest.skip("python3 not found")
    return subprocess.run([python3, str(CLASSIFIER)], input=stdin,  # nosec B603 - fixed argv
                          capture_output=True, text=True, timeout=60)


def test_the_cli_prints_the_class_and_exits_zero(rev):
    for template, expected in ((_current(rev), "current"), (_template(None), "old")):
        result = _cli(json.dumps(template))
        assert (result.returncode, result.stdout.strip()) == (0, expected), result.stderr


@pytest.mark.parametrize("stdin", ["", "{", "[]", '{"Resources": 3}'])
def test_the_cli_fails_on_an_unreadable_template_with_the_reason(stdin):
    result = _cli(stdin)
    assert result.returncode == 1
    assert result.stdout.strip() and result.stdout.strip() not in ("current", "old")


def test_the_cli_reads_one_unprotected_vpc_function_as_old_and_an_unreadable_one_as_an_error(rev):
    mixed = _add_function(_current(rev), "Other", drain=False, detach=False)
    result = _cli(json.dumps(mixed))
    assert (result.returncode, result.stdout.strip()) == (0, "old"), result.stderr
    unreadable = _current(rev)
    unreadable["Resources"]["Fn"]["Properties"]["VpcConfig"] = {"SubnetIds": ["subnet-1"]}
    result = _cli(json.dumps(unreadable))
    assert result.returncode == 1 and "Fn" in result.stdout, result.stdout


def test_the_cli_reads_a_falsy_vpc_config_and_an_unknown_group_entry_as_old(rev):
    for change in ("config", "entry"):
        template = _current(rev)
        vpc = template["Resources"]["Fn"]["Properties"]
        if change == "config":
            vpc["VpcConfig"] = []
        else:
            vpc["VpcConfig"]["SecurityGroupIds"].append({"Fn::If": ["C", {"Ref": "FnSg"}, "sg-1"]})
        result = _cli(json.dumps(template))
        assert (result.returncode, result.stdout.strip()) == (0, "old"), (change, result.stdout)


def test_the_stacks_stamp_the_classifier_s_own_revision_and_names(rev):
    text = (REPO_ROOT / "stacks" / "_eni_drain.py").read_text(encoding="utf-8")
    assert "from stacks._eni_drain_revision import" in text
    assert rev.REVISION_PROPERTY == "EniDrainRevision"
    assert rev.DRAIN_RESOURCE_TYPE == "Custom::ADTVpcLambdaEniDrain"
    assert rev.DETACH_RESOURCE_TYPE == "Custom::ADTVpcLambdaEniDetach"
