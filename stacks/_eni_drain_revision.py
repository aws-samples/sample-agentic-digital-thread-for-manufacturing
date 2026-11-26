# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which ENI drain a deployed stack carries, read from its template.

A stack delete runs the custom resources the stack was last deployed with, not
the ones in this checkout. So before an operator path turns an optional stack
off and deletes it, it reads the deployed template and asks :func:`classify`:

* ``current``: every ENI drain and detach (stacks/_eni_drain.py) carries
  :data:`REVISION_PROPERTY` at :data:`ENI_DRAIN_REVISION` or later, and every
  VPC Lambda function is protected by them (below). A stack with no VPC Lambda
  function is current too: there is nothing for a drain to do. Its delete runs
  a drain at least as good as this checkout's on every function.
* ``old``: a drain or detach without the property, or below this revision, or
  a VPC Lambda function that lacks any part of its protection (no drain at all,
  or two VPC functions with only one of them protected). The operator path
  first updates the stack with the option still on, which installs this
  checkout's drain, and only then deletes it.

A VPC Lambda function (an ``AWS::Lambda::Function`` with a VpcConfig) is
protected when, through the links stacks/_eni_drain.py synthesizes:

* each of its security groups that is a resource of the same template (named
  in ``VpcConfig.SecurityGroupIds`` by ``Ref <group>`` or
  ``Fn::GetAtt [<group>, GroupId]``) has a current drain whose
  ``SecurityGroupId`` is one of those two references to that group and whose
  ``SecurityGroupLogicalId`` is its logical id, and the function's
  ``DependsOn`` lists that drain (so a delete runs function, drain, group);
* it has at least one such group (a function only in groups from outside the
  stack has no drain); groups from outside the stack (an Fn::ImportValue, a
  Ref to a parameter, a literal id) need none, since this stack doesn't delete
  them;
* a current detach lists it in ``Functions`` with its logical id as
  ``LogicalId`` and ``Fn::GetAtt [<function>, Arn]`` as ``Arn``.

Each of these fields is read for its own shape only. A drain whose
``SecurityGroupId`` carries another value (another attribute of the group,
such as its VpcId) or a detach entry whose ``Arn`` is anything else covers
nothing. A VPC function with a security group entry that is neither of this
template nor plainly from outside it (a conditional ``Fn::If``, another
attribute, a reference to no resource) reads ``old``, and so does one whose
VpcConfig is not a mapping (any other type, falsy ones such as ``[]``
included): neither is ever read as an outside group or as no VPC.

A template that can't be read is neither: :func:`classify` raises, and the
caller stops before deleting anything. That includes a VPC function whose
VpcConfig mapping has security groups that aren't a list (an intrinsic, a
missing key) or whose ``DependsOn`` is neither a name nor a list of names.

The drain construct stamps :data:`ENI_DRAIN_REVISION` on every drain and detach
it creates. Raise it when a change to the drain or the detach (their handlers,
grants, ordering or timeouts) is one a stack must have installed before it is
deleted; the next off of a stack deployed before the change then updates it
first. The value is a constant, so a deploy that changes nothing else sends the
drains no Update.

No dependency beyond the standard library, so deploy.sh can run it with any
python3 on PATH:

    aws cloudformation get-template ... --query TemplateBody --output json \\
        | python3 stacks/_eni_drain_revision.py

prints ``current`` or ``old`` and exits 0, or prints why the template can't be
read and exits 1.
"""
from __future__ import annotations

import json
import sys

#: The revision every drain and detach in this checkout carries.
ENI_DRAIN_REVISION = 1
#: The custom resource property that carries it.
REVISION_PROPERTY = "EniDrainRevision"
DRAIN_RESOURCE_TYPE = "Custom::ADTVpcLambdaEniDrain"
DETACH_RESOURCE_TYPE = "Custom::ADTVpcLambdaEniDetach"
_LAMBDA_FUNCTION = "AWS::Lambda::Function"


def _revision(value) -> int | None:
    """A whole, non-negative revision, or None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit() and value.isascii():
        return int(value)
    return None


def _in_vpc(properties: dict) -> bool:
    """Whether a Lambda function's properties put it in a VPC. No VpcConfig, an
    empty mapping, or one whose groups and subnets are both empty lists leaves
    it out. Anything else counts as in a VPC: an intrinsic, and a VpcConfig
    that is not a mapping at all (falsy ones such as ``[]`` included), which
    :func:`_own_groups` then can't read."""
    if "VpcConfig" not in properties:
        return False
    config = properties["VpcConfig"]
    if not isinstance(config, dict):
        return True
    if not config:
        return False
    groups, subnets = config.get("SecurityGroupIds"), config.get("SubnetIds")
    if groups == [] and subnets == [] and set(config) <= {"SecurityGroupIds", "SubnetIds"}:
        return False
    return True


def _ref(value) -> str | None:
    """The logical id in ``{"Ref": <id>}``, or None for anything else."""
    if isinstance(value, dict) and len(value) == 1 and isinstance(value.get("Ref"), str):
        return value["Ref"]
    return None


def _get_att(value, attribute: str) -> str | None:
    """The logical id in ``{"Fn::GetAtt": [<id>, <attribute>]}`` (or its
    ``"<id>.<attribute>"`` form) for this one attribute, or None for anything
    else, another attribute of the same resource included."""
    if not isinstance(value, dict) or len(value) != 1 or "Fn::GetAtt" not in value:
        return None
    parts = value["Fn::GetAtt"]
    if isinstance(parts, str):
        parts = parts.split(".", 1)
    if (isinstance(parts, list) and len(parts) == 2 and all(isinstance(p, str) and p for p in parts)
            and parts[1] == attribute):
        return parts[0]
    return None


def _group_reference(value) -> str | None:
    """The security group a security group id value names: ``Ref <group>`` or
    ``Fn::GetAtt [<group>, GroupId]``. None for anything else."""
    group = _ref(value)
    return group if group is not None else _get_att(value, "GroupId")


def _depends_on(logical_id: str, resource: dict) -> set[str]:
    """The names in a resource's DependsOn; ValueError when it is neither a
    name nor a list of names."""
    value = resource.get("DependsOn", [])
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(name, str) for name in value):
        return set(value)
    raise ValueError(f"resource {logical_id} has a DependsOn that is not a name or a list of names")


def _own_groups(logical_id: str, properties: dict, resources: dict) -> list[str] | None:
    """The logical ids of a VPC function's security groups that are resources
    of this template. None when they can't be told apart, which reads old: a
    VpcConfig that is not a mapping, or an entry that is neither a group of
    this template (``Ref`` or ``Fn::GetAtt GroupId`` of a resource) nor one
    from outside it (an ``Fn::ImportValue``, a ``Ref`` to no resource such as
    a parameter, a literal id). ValueError when a VpcConfig mapping's security
    groups are not a list."""
    config = properties.get("VpcConfig")
    if not isinstance(config, dict):
        return None
    entries = config.get("SecurityGroupIds")
    if not isinstance(entries, list):
        raise ValueError(f"function {logical_id} is in a VPC, but its security groups are not a list")
    groups = []
    for entry in entries:
        if isinstance(entry, str):
            continue
        group = _ref(entry)
        if group is not None:
            if group in resources:
                groups.append(group)
            continue
        group = _get_att(entry, "GroupId")
        if group is not None and group in resources:
            groups.append(group)
        elif not (isinstance(entry, dict) and len(entry) == 1 and "Fn::ImportValue" in entry):
            return None
    return groups


def _drained_group(properties: dict) -> str | None:
    """The group a drain names: its SecurityGroupId is ``Ref <group>`` or
    ``Fn::GetAtt [<group>, GroupId]`` and its SecurityGroupLogicalId is
    ``<group>``. None for any other SecurityGroupId, or when they disagree."""
    group = _group_reference(properties.get("SecurityGroupId"))
    return group if group is not None and properties.get("SecurityGroupLogicalId") == group else None


def _detached_functions(properties: dict) -> set[str]:
    """The functions a detach lists: an entry's LogicalId, when its Arn is
    ``Fn::GetAtt [<that function>, Arn]``."""
    entries = properties.get("Functions")
    if not isinstance(entries, list):
        return set()
    return {entry["LogicalId"] for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("LogicalId"), str)
            and _get_att(entry.get("Arn"), "Arn") == entry["LogicalId"]}


def classify(template) -> str:
    """``"current"`` or ``"old"`` for a template (a mapping, or the JSON text
    of one); ValueError for anything that can't be read as a template."""
    if isinstance(template, str):
        try:
            template = json.loads(template)
        except ValueError as error:
            raise ValueError(f"the template is not JSON ({error})") from None
    if not isinstance(template, dict):
        raise ValueError(f"the template is a {type(template).__name__}, not a mapping")
    resources = template.get("Resources")
    if not isinstance(resources, dict):
        raise ValueError("the template has no Resources mapping")
    old = False
    drains: dict[str, set[str]] = {}       # group -> the current drains naming it
    detached: set[str] = set()             # functions a current detach lists
    functions: dict[str, tuple[list[str] | None, set[str]]] = {}   # VPC function -> (groups, DependsOn)
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            raise ValueError(f"resource {logical_id} is not a mapping")
        properties = resource.get("Properties", {})
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            raise ValueError(f"resource {logical_id} has Properties that are not a mapping")
        kind = resource.get("Type")
        if kind in (DRAIN_RESOURCE_TYPE, DETACH_RESOURCE_TYPE):
            revision = _revision(properties.get(REVISION_PROPERTY))
            if revision is None or revision < ENI_DRAIN_REVISION:
                old = True
            elif kind == DRAIN_RESOURCE_TYPE:
                group = _drained_group(properties)
                if group is not None:
                    drains.setdefault(group, set()).add(logical_id)
            else:
                detached |= _detached_functions(properties)
        elif kind == _LAMBDA_FUNCTION and _in_vpc(properties):
            functions[logical_id] = (_own_groups(logical_id, properties, resources),
                                     _depends_on(logical_id, resource))
    if old:
        return "old"
    for logical_id, (groups, depends_on) in functions.items():
        if not groups or logical_id not in detached:
            return "old"
        if any(not drains.get(group, set()) & depends_on for group in groups):
            return "old"
    return "current"


def main(stdin=None, stdout=None) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    text = stdin.read()
    try:
        if not text.strip():
            raise ValueError("no template was read")
        print(classify(json.loads(text)), file=stdout)
    except ValueError as error:
        print(f"unreadable template: {error}", file=stdout)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
