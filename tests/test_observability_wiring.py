# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""assertions.Template tests for ``stacks/_observability.wire_observability_reader``.

The opt-in drill-down reader wiring must:
  * ALWAYS set ``AGENTS_OBSERVABILITY_DRILLDOWN`` on the adt-app container —
    "true" when enabled, "false" when not (the routes feature-detect
    deterministically);
  * add the span read IAM (logs:StartQuery scoped to ``aws/spans`` and the
    ADT runtimes' own log groups — the ``adt_`` name prefix the app gives
    every runtime, never another application's AgentCore runtime groups —
    logs:DescribeLogGroups to list those groups,
    logs:GetQueryResults/StopQuery, xray:GetTraceSegmentDestination) ONLY
    when enabled — a deploy without the flag synthesizes the same task-role
    policies as before (cost-bearing feature stays strictly opt-in).

Synthesized fully offline via ``assertions.Template`` against a minimal stack
carrying the same construct ids the wiring locates ("AdtTaskDef"/"AdtTaskRole"
— the ADTComputeStack seam), so no CDK CLI/Node/context lookups are needed.

Run:  PYTHONPATH=. python -m pytest tests/test_observability_wiring.py -v
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import aws_cdk as cdk
from aws_cdk import assertions, aws_ecs as ecs, aws_iam as iam

from stacks._observability import wire_observability_reader

ACCOUNT = "111122223333"   # AWS documentation placeholder
REGION = "us-east-1"
SPANS_ARN = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:aws/spans"
RUNTIME_GROUPS_ARN = (
    f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/adt_*")
ANY_RUNTIME_GROUP_ARN = (
    f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/*")
#: The app's one copy of the runtime name prefix (pure module, no imports).
_FLEET_METRICS = (Path(__file__).resolve().parents[1]
                  / "agentic_digital_thread" / "agents" / "fleet_metrics.py")


def _synth(enabled: bool, span_emission: bool | None = None) -> assertions.Template:
    """Minimal stack with the ADTComputeStack construct ids the wiring targets."""
    app = cdk.App()
    stack = cdk.Stack(app, "TestStack",
                      env=cdk.Environment(account=ACCOUNT, region=REGION))
    task_role = iam.Role(stack, "AdtTaskRole",
                         assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
    task_def = ecs.FargateTaskDefinition(stack, "AdtTaskDef", task_role=task_role)
    task_def.add_container(
        "adt-app",
        image=ecs.ContainerImage.from_registry("test/adt-app:offline"),
        memory_limit_mib=512,
    )
    wire_observability_reader(stack, enabled=enabled, span_emission=span_emission)
    return assertions.Template.from_stack(stack)


def _adt_app_env(template: assertions.Template) -> dict:
    """{Name: Value} env map of the adt-app container definition."""
    for resource in template.find_resources("AWS::ECS::TaskDefinition").values():
        for cdef in resource["Properties"].get("ContainerDefinitions", []):
            if cdef.get("Name") == "adt-app":
                return {e["Name"]: e["Value"] for e in cdef.get("Environment", [])}
    pytest.fail("adt-app container definition not found in template")


def _all_policy_statements(template: assertions.Template) -> list[dict]:
    statements: list[dict] = []
    for resource in template.find_resources("AWS::IAM::Policy").values():
        statements.extend(
            resource["Properties"]["PolicyDocument"].get("Statement", []))
    return statements


def _statements_with_action(template: assertions.Template, action: str) -> list[dict]:
    hits = []
    for stmt in _all_policy_statements(template):
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if action in actions:
            hits.append(stmt)
    return hits


# ── Env var set BOTH ways ────────────────────────────────────────────────────

def test_env_true_when_enabled():
    env = _adt_app_env(_synth(enabled=True))
    assert env["AGENTS_OBSERVABILITY_DRILLDOWN"] == "true"


def test_env_false_when_disabled():
    # The var is still PRESENT ("false") so the routes feature-detect
    # deterministically rather than falling through on a missing key.
    env = _adt_app_env(_synth(enabled=False))
    assert env["AGENTS_OBSERVABILITY_DRILLDOWN"] == "false"


# ── Span emission on the app's runtimes (AGENT_SPAN_EMISSION) ───────────────

@pytest.mark.parametrize("enabled", [True, False])
def test_span_emission_follows_the_option_by_default(enabled):
    env = _adt_app_env(_synth(enabled=enabled))
    assert env["AGENT_SPAN_EMISSION"] == ("true" if enabled else "false")


def test_span_emission_can_be_on_without_the_reader():
    env = _adt_app_env(_synth(enabled=False, span_emission=True))
    assert (env["AGENT_SPAN_EMISSION"], env["AGENTS_OBSERVABILITY_DRILLDOWN"]) == ("true", "false")


@pytest.mark.parametrize("context,expected", [
    ({}, "false"),
    ({"observability": "true"}, "true"),
], ids=["default", "observability"])
def test_app_sets_span_emission_from_the_deploy_options(tmp_path_factory, context, expected):
    from _app_synth import synth_app  # no asset bundling: the seed layer would need Docker

    template = synth_app(tmp_path_factory.mktemp("cdk_synth_span_emission"), context)["ADTComputeStack"]
    assert _adt_app_env(assertions.Template.from_json(template))["AGENT_SPAN_EMISSION"] == expected


# ── IAM present ONLY when enabled ────────────────────────────────────────────

def test_enabled_grants_scoped_start_query():
    template = _synth(enabled=True)
    stmts = _statements_with_action(template, "logs:StartQuery")
    assert len(stmts) == 1, json.dumps(_all_policy_statements(template), indent=2)
    resources = stmts[0]["Resource"]
    if isinstance(resources, str):
        resources = [resources]
    # StartQuery is pinned to the two span destinations — never "*": the
    # shared aws/spans group (older agents) and the ADT runtimes' own log
    # groups (unified span destination, the `spans` stream of each agent's
    # own group; names are only known after deploy.sh creates the runtimes,
    # so the grant is the `adt_` name prefix every ADT runtime carries).
    assert set(resources) == {SPANS_ARN, f"{SPANS_ARN}:*", RUNTIME_GROUPS_ARN}
    # Never every AgentCore runtime group in the account: another
    # application's runtime groups (and their traces) stay unreadable.
    assert ANY_RUNTIME_GROUP_ARN not in resources
    assert stmts[0]["Effect"] == "Allow"


def test_runtime_group_grant_uses_the_apps_runtime_name_prefix():
    # The routes read only groups of runtimes named with the app's prefix;
    # the grant must cover exactly those, so the two prefixes cannot drift.
    import stacks._observability as obs
    spec = importlib.util.spec_from_file_location("_adt_fleet_metrics", _FLEET_METRICS)
    fleet_metrics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet_metrics)
    assert obs.ADT_RUNTIME_NAME_PREFIX == fleet_metrics._RUNTIME_PREFIX == "adt_"


def test_enabled_grants_log_group_discovery():
    # The routes discover the runtime log groups at request time.
    # DescribeLogGroups has no resource-level permissions, so it is "*".
    template = _synth(enabled=True)
    stmts = _statements_with_action(template, "logs:DescribeLogGroups")
    assert len(stmts) == 1
    assert stmts[0]["Resource"] == "*"
    assert stmts[0]["Action"] == "logs:DescribeLogGroups"


def test_enabled_grants_query_results_and_xray_read():
    template = _synth(enabled=True)
    assert _statements_with_action(template, "logs:GetQueryResults")
    assert _statements_with_action(template, "logs:StopQuery")
    assert _statements_with_action(template, "xray:GetTraceSegmentDestination")


def test_disabled_synthesizes_no_span_read_iam():
    template = _synth(enabled=False)
    for action in ("logs:StartQuery", "logs:GetQueryResults",
                   "logs:StopQuery", "logs:DescribeLogGroups",
                   "xray:GetTraceSegmentDestination"):
        assert _statements_with_action(template, action) == [], action
    # No statements at all were added to the task role → no default policy.
    template.resource_count_is("AWS::IAM::Policy", 0)


def test_missing_task_def_raises_loudly():
    # The wiring must FAIL, not silently no-op, if the construct id drifts.
    app = cdk.App()
    stack = cdk.Stack(app, "EmptyStack",
                      env=cdk.Environment(account=ACCOUNT, region=REGION))
    with pytest.raises(Exception):
        wire_observability_reader(stack, enabled=True)
