# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The app task role may run every AgentCore call the app makes, and no more.

The app provisions, updates and deletes AgentCore runtimes and gateway targets
at run time (agents/agent_manager.py, agents/onboard.py, agents/admin_routes.py,
agents/chat_client.py, agents/memory_client.py). AgentCore also authorizes the
implicit side effects of those calls against the CALLER: CreateAgentRuntime
creates the DEFAULT runtime endpoint and the runtime's workload identity, and
DeleteAgentRuntime deletes them (live-proven in the workshop engine deploy,
workshop_infra/workshop_engine_stack.py, and on a sample deploy where
onboarding failed AccessDenied on CreateAgentRuntime without them).

The grant is an enumerated action list, never ``bedrock-agentcore:*``. Only
the five actions with no resource type sit on ``*``; the rest are scoped to
AgentCore resources in this account and Region. The test reads the task role's
synthesized policy from a credential-blind ``cdk synth``.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import _role_policies

_SPEC = importlib.util.spec_from_file_location(
    "quick_stacks_synth", Path(__file__).with_name("test_quick_stacks.py"))
_QS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_QS)

#: Every bedrock-agentcore action the app task makes, directly or as an
#: implicit side effect AgentCore authorizes against the caller.
APP_ACTIONS = {
    # runtimes (agent_manager, onboard, admin_routes, provisioner)
    "CreateAgentRuntime", "UpdateAgentRuntime", "GetAgentRuntime",
    "DeleteAgentRuntime", "ListAgentRuntimes", "ListAgentRuntimeVersions",
    # implicit: CreateAgentRuntime makes the DEFAULT endpoint and the workload
    # identity; DeleteAgentRuntime removes them; Update rewrites the endpoint
    "CreateAgentRuntimeEndpoint", "GetAgentRuntimeEndpoint",
    "UpdateAgentRuntimeEndpoint", "DeleteAgentRuntimeEndpoint",
    "ListAgentRuntimeEndpoints",
    "CreateWorkloadIdentity", "GetWorkloadIdentity", "DeleteWorkloadIdentity",
    # invoke (chat_client, server)
    "InvokeAgentRuntime", "StopRuntimeSession",
    # gateway targets
    "CreateGatewayTarget", "UpdateGatewayTarget", "GetGatewayTarget",
    "DeleteGatewayTarget", "ListGatewayTargets", "SynchronizeGatewayTargets",
    # implicit: SynchronizeGatewayTargets runs the OAuth handshake as the caller
    "GetWorkloadAccessToken", "GetWorkloadAccessTokenForJWT",
    "GetWorkloadAccessTokenForUserId", "GetResourceOauth2Token",
    # credential providers
    "CreateOauth2CredentialProvider", "ListOauth2CredentialProviders",
    # memory (config, memory_client, memory_model, memory_routes)
    "ListMemories", "GetMemory", "UpdateMemory", "ListSessions", "ListEvents",
    "ListMemoryRecords", "GetMemoryRecord", "DeleteMemoryRecord",
}
#: Actions with no resource type in the Service Authorization Reference: only
#: these may be on "*", and each must be (a resource ARN would not grant them).
NO_RESOURCE_ACTIONS = {"CreateAgentRuntime", "ListAgentRuntimes", "ListAgentRuntimeEndpoints",
                       "ListAgentRuntimeVersions", "ListMemories"}


@pytest.fixture(scope="session")
def task_role_statements(tmp_path_factory):
    assembly = _QS._synth_assembly(tmp_path_factory, context={},
                                   output_slug="cdk_synth_agentcore_iam")
    template = _QS._template(assembly, "ADTComputeStack")
    (role_id,) = [lid for lid, r in template["Resources"].items()
                  if r["Type"] == "AWS::IAM::Role" and lid.startswith("AdtTaskRole")]
    return _role_policies.statements(template, role_id)


def _agentcore_grants(statements) -> dict[str, list]:
    """bedrock-agentcore action (without prefix) -> the Resource of each grant."""
    grants: dict[str, list] = {}
    for s in statements:
        if s.get("Effect") != "Allow":
            continue
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        for a in actions:
            if a.startswith("bedrock-agentcore:"):
                grants.setdefault(a.split(":", 1)[1], []).append(s["Resource"])
    return grants


def test_no_service_wildcard(task_role_statements):
    assert "*" not in _agentcore_grants(task_role_statements)


def test_every_app_action_is_granted(task_role_statements):
    missing = APP_ACTIONS - set(_agentcore_grants(task_role_statements))
    assert not missing, sorted(missing)


def test_only_no_resource_actions_are_on_star(task_role_statements):
    for action, resources in _agentcore_grants(task_role_statements).items():
        for resource in resources:
            if resource == "*" or resource == ["*"]:
                assert action in NO_RESOURCE_ACTIONS, action
            else:
                text = json.dumps(resource)
                assert "bedrock-agentcore" in text and "AWS::AccountId" in text, (action, text)


def test_every_no_resource_action_is_granted_on_star(task_role_statements):
    grants = _agentcore_grants(task_role_statements)
    for action in NO_RESOURCE_ACTIONS:
        assert any(r in ("*", ["*"]) for r in grants.get(action, [])), action
