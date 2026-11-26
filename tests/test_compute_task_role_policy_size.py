# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The app task role's permissions fit IAM's size limits, with nothing lost.

IAM caps the inline policies of one role at 10,240 characters in total, and
each customer managed policy at 6,144. The app task role (``AdtTaskRole``)
outgrew the inline cap (a deploy failed "Maximum policy size of 10240 bytes
exceeded"), so its statements live in customer managed policies attached to
it, split under the per-policy cap. What the role may do must not change:
every (action, resource, condition) grant the role had as inline policies
is still granted. The fixtures hold those grants, read from the synthesized
template before the move, for a default deploy and for one with every option
that adds grants.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _app_synth import synth_app

FIXTURES = {
    "defaults": Path(__file__).with_name("fixtures") / "adt_task_role_grants.json",
    "all_options": Path(__file__).with_name("fixtures") / "adt_task_role_grants_all_options.json",
}
INLINE_TOTAL_LIMIT = 10_240
MANAGED_POLICY_LIMIT = 6_144
#: Room for tokens that resolve longer than their synthesized form (ARNs).
MARGIN = 1_024


def _compact(doc) -> int:
    return len(json.dumps(doc, separators=(",", ":"), sort_keys=True))


def task_role_policies(template: dict) -> tuple[str, list, list]:
    """(role logical id, its inline policy documents, its managed policy documents)."""
    (role_id,) = [lid for lid, r in template["Resources"].items()
                  if r["Type"] == "AWS::IAM::Role" and lid.startswith("AdtTaskRole")]
    ref = {"Ref": role_id}
    inline = [p["PolicyDocument"] for p in
              template["Resources"][role_id]["Properties"].get("Policies", [])]
    arns = template["Resources"][role_id]["Properties"].get("ManagedPolicyArns", [])
    managed = []
    for lid, r in template["Resources"].items():
        roles = r.get("Properties", {}).get("Roles", [])
        if ref not in roles and {"Ref": lid} not in arns:
            continue
        if r["Type"] == "AWS::IAM::Policy":
            inline.append(r["Properties"]["PolicyDocument"])
        elif r["Type"] == "AWS::IAM::ManagedPolicy":
            managed.append(r["Properties"]["PolicyDocument"])
    return role_id, inline, managed


def grants(docs: list) -> set[str]:
    """Every (action, resource, condition) the documents allow, as JSON strings."""
    out = set()
    for doc in docs:
        for s in doc["Statement"]:
            if s.get("Effect") != "Allow":
                continue
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            resources = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
            cond = json.dumps(s.get("Condition"), sort_keys=True)
            for a in actions:
                for r in resources:
                    out.add(json.dumps([a, r, cond], sort_keys=True))
    return out


#: Every deploy option that adds grants to the task role, switched on.
_ALL_OPTIONS = {
    "with_seed": "true", "with_simulator": "true", "quick_api_enabled": "true",
    "observability": "true", "highbyte_enabled": "true", "highbyte_accept_eula": "true",
    "sfc_enabled": "true", "opcua_enabled": "true",
    "quick_oidc_discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "quick_oidc_audience": "https://gw.example.com/mcp",
}


@pytest.fixture(scope="session", params=["defaults", "all_options"])
def task_role_template(request, tmp_path_factory):
    context = {} if request.param == "defaults" else _ALL_OPTIONS
    # synth_app skips asset bundling: the seed layer would need Docker.
    out_dir = tmp_path_factory.mktemp(f"cdk_synth_task_role_size_{request.param}")
    return synth_app(out_dir, context)["ADTComputeStack"]


@pytest.fixture(scope="session")
def task_role(task_role_template):
    return task_role_policies(task_role_template)


def test_the_role_has_no_large_inline_policy_left(task_role):
    _, inline, _ = task_role
    total = sum(_compact(d) for d in inline)
    assert total + MARGIN <= INLINE_TOTAL_LIMIT, total


def test_each_managed_policy_fits_its_limit(task_role):
    _, _, managed = task_role
    assert managed
    for doc in managed:
        assert _compact(doc) + MARGIN <= MANAGED_POLICY_LIMIT, _compact(doc)


def test_every_grant_the_role_had_is_still_granted(task_role, request):
    _, inline, managed = task_role
    fixture = FIXTURES[request.node.callspec.params["task_role_template"]]
    expected = set(json.loads(fixture.read_text(encoding="utf-8")))
    current = grants(inline + managed)
    assert not expected - current, sorted(expected - current)[:10]
    assert not current - expected, sorted(current - expected)[:10]


def test_managed_policies_exist_before_the_role_drops_its_inline_grants(task_role, request):
    """The role lists each managed policy in ManagedPolicyArns, and no managed
    policy names the role, so CloudFormation creates the policies before it
    updates the role (which removes the old inline policies). With Roles on
    the policy the order is reversed and the task runs without grants."""
    assembly_template = request.getfixturevalue("task_role_template")
    role_id, _, _ = task_role
    resources = assembly_template["Resources"]
    policies = [lid for lid, r in resources.items() if r["Type"] == "AWS::IAM::ManagedPolicy"
                and lid.startswith("AdtTaskPolicy")]
    assert policies
    arns = resources[role_id]["Properties"].get("ManagedPolicyArns", [])
    for lid in policies:
        assert {"Ref": lid} in arns, lid
        assert "Roles" not in resources[lid]["Properties"], lid


def test_gateway_sync_token_read_is_granted(task_role):
    _, inline, managed = task_role
    granted = {json.loads(g)[0] for g in grants(inline + managed)}
    assert "bedrock-agentcore:GetWorkloadAccessToken" in granted
