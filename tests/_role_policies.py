# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read every policy statement a role in a synthesized template carries.

A role's grants can sit in three places: inline on the role (``Policies``),
in an ``AWS::IAM::Policy`` or in an ``AWS::IAM::ManagedPolicy`` that lists the
role (``Roles``), or a managed policy the role lists in ``ManagedPolicyArns``.
The app task role keeps its grants in managed policies, and each moved
statement has the Sid ``<former policy name><index>``
(``ADTComputeStack._attach_task_role_managed_policies``), so ``by_name`` can
still group them under the inline policy names the tests use.
"""
from __future__ import annotations

import re

_MOVED_SID = re.compile(r"^(?P<name>[A-Za-z]+?)\d+$")


def named_documents(template: dict) -> list[tuple[str, dict]]:
    """(name, policy document) for every inline policy on every role, plus the
    moved statements of every managed policy grouped under their former name."""
    out: list[tuple[str, dict]] = []
    for r in template.get("Resources", {}).values():
        if r.get("Type") == "AWS::IAM::Role":
            out += [(p["PolicyName"], p["PolicyDocument"])
                    for p in r["Properties"].get("Policies", []) or []]
        elif r.get("Type") == "AWS::IAM::ManagedPolicy":
            groups: dict[str, list[dict]] = {}
            for s in r["Properties"]["PolicyDocument"]["Statement"]:
                moved = _MOVED_SID.match(s.get("Sid", ""))
                groups.setdefault(moved["name"] if moved else "", []).append(s)
            out += [(name, {"Statement": st}) for name, st in groups.items()]
    return out


def statements(template: dict, role_id: str) -> list[dict]:
    """Every statement of every policy attached to *role_id*."""
    return [s for docs in by_name(template, role_id).values() for s in docs]


def by_name(template: dict, role_id: str) -> dict[str, list[dict]]:
    """Policy name -> statements, for every policy attached to *role_id*.

    Inline policies keep their PolicyName. A moved statement is filed under
    the name in its Sid; any other attached statement under ``<logical id>``.
    """
    resources = template["Resources"]
    out: dict[str, list[dict]] = {}
    for p in resources[role_id]["Properties"].get("Policies", []) or []:
        out.setdefault(p["PolicyName"], []).extend(p["PolicyDocument"]["Statement"])
    ref = {"Ref": role_id}
    arns = resources[role_id]["Properties"].get("ManagedPolicyArns", [])
    for lid, r in resources.items():
        if r["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        if ref not in r["Properties"].get("Roles", []) and {"Ref": lid} not in arns:
            continue
        for s in r["Properties"]["PolicyDocument"]["Statement"]:
            moved = _MOVED_SID.match(s.get("Sid", "")) if r["Type"] == "AWS::IAM::ManagedPolicy" else None
            out.setdefault(moved["name"] if moved else lid, []).append(s)
    return out
