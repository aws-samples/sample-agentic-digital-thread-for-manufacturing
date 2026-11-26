# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Opt-in observability drill-down wiring for ADTComputeStack.

The ``--observability`` deploy flag (sticky SSM parameter
``/adt/observability-enabled``, HighByte discipline — deploy.sh
owns the write, app.py reads it fail-soft via
``stacks/_sticky_context.resolve_sticky_bool``) enables CloudWatch Transaction
Search span ingestion plus the in-app per-session drill-down READER on the ECS
app:

- ``AGENTS_OBSERVABILITY_DRILLDOWN`` task env ("true"/"false" — the span-reader
  routes feature-detect this and return ``{"enabled": false}`` when off, with
  no boto3 calls);
- flag-gated IAM on the app task role to run Logs Insights queries against the
  span log groups — ``aws/spans`` (Transaction Search's shared destination,
  used by older agents) and the ADT runtimes' own log groups
  (``/aws/bedrock-agentcore/runtimes/adt_*``, whose ``spans`` stream holds the
  spans of agents on AgentCore's unified span destination) — to check which
  of those groups exist, and to read the account-level X-Ray trace-segment
  destination.

Default OFF: span ingestion bills continuously, so the reader side is strictly
opt-in. Span EMISSION on the agent runtimes (``AGENT_OBSERVABILITY_ENABLED``)
is on with this flag (``AGENT_SPAN_EMISSION``); without
a span destination every export fails, so off turns the exporters off.

This is deliberately an ADDITIVE post-construction module (applied from app.py
after the stack is built, via ``wire_observability_reader``) rather than more
lines inside ``ADTComputeStack._create_ecs_service``: the opt-in feature stays
a self-contained, removable seam instead of growing an already-oversized
function (the repo's changed-function maintainability guard caps touched
function bodies at 50 lines).
"""
from __future__ import annotations

from aws_cdk import aws_iam as iam


#: The name prefix the app gives every ADT AgentCore runtime (``adt_<id>``).
#: Must equal ``agentic_digital_thread/agents/fleet_metrics._RUNTIME_PREFIX``
#: (pinned by tests/test_observability_wiring.py): the drill-down reads only
#: those runtimes' log groups, and this grant covers exactly those.
ADT_RUNTIME_NAME_PREFIX = "adt_"


def _span_read_statements(stack) -> list[iam.PolicyStatement]:
    """IAM statements for reading AgentCore spans (least-privilege).

    These are what the drill-down reader needs, scoped to its sources. On
    ADTComputeStack's app task role they add no new access: that role already
    has ``logs:StartQuery`` and the other Logs reads on ``"*"`` from
    ``ADTComputeStack._agentcore_logs_network_statements`` (the Agents page's
    other queries), and narrowing that is a separate change. Here the boundary
    that keeps other applications' runtime groups out of the drill-down is the
    routes' own source selection (live ``adt_`` runtimes plus ``aws/spans``).
    """
    log_group_arn = f"arn:aws:logs:{stack.region}:{stack.account}:log-group"
    return [
        iam.PolicyStatement(
            # StartQuery on the two span destinations: aws/spans (older
            # agents) and the ADT runtimes' own groups (their `spans`
            # stream). Runtimes are created by deploy.sh, so their group names
            # are unknown at synth; the `adt_` prefix wildcard (which also
            # covers each group ARN's ":*" form) leaves other apps' groups out.
            actions=["logs:StartQuery"],
            resources=[
                f"{log_group_arn}:aws/spans",
                f"{log_group_arn}:aws/spans:*",
                f"{log_group_arn}:/aws/bedrock-agentcore/runtimes/"
                f"{ADT_RUNTIME_NAME_PREFIX}*",
            ],
        ),
        iam.PolicyStatement(
            # The routes check by name which span groups exist; the action has
            # no resource-level permissions (metadata only, not log events).
            # ListAgentRuntimes is not added: the task role already has
            # bedrock-agentcore:* (ADTComputeStack._agentcore_core_statements).
            actions=["logs:DescribeLogGroups"],
            resources=["*"],
        ),
        iam.PolicyStatement(
            # GetQueryResults/StopQuery operate on query IDs and do not
            # support resource-level permissions, so they are granted on "*".
            actions=["logs:GetQueryResults", "logs:StopQuery"],
            resources=["*"],
        ),
        iam.PolicyStatement(
            # Account-level X-Ray configuration read (used to report whether
            # Transaction Search ingestion is active). The API has no
            # resource-level permissions, so it is granted on "*".
            actions=["xray:GetTraceSegmentDestination"],
            resources=["*"],
        ),
    ]


def wire_observability_reader(stack, *, enabled: bool,
                              span_emission: bool | None = None) -> None:
    """Wire the drill-down reader env (+ IAM when enabled) onto the ECS app.

    Always sets ``AGENTS_OBSERVABILITY_DRILLDOWN`` ("true"/"false") on the
    adt-app container so the routes can feature-detect deterministically,
    and ``AGENT_SPAN_EMISSION`` ("true"/"false", *span_emission*, default
    *enabled*), which the app writes onto the runtimes it provisions
    (agents/runtime_env_contract.py).
    The span read IAM is added ONLY when ``enabled`` — a deploy without the
    flag synthesizes the same task-role policies as before.

    The constructs are located through the constructs tree by their fixed ids
    ("AdtTaskDef" / "AdtTaskRole" in ``_create_ecs_service``); a missing child
    raises loudly (no fallback, per project steering) so an id rename cannot
    silently drop the wiring.
    """
    task_def = stack.node.find_child("AdtTaskDef")
    container = task_def.default_container
    if container is None:  # pragma: no cover - single-container task def
        container = task_def.find_container("adt-app")
    container.add_environment(
        "AGENTS_OBSERVABILITY_DRILLDOWN", "true" if enabled else "false"
    )
    emission = enabled if span_emission is None else span_emission
    container.add_environment("AGENT_SPAN_EMISSION", "true" if emission else "false")
    if not enabled:
        return
    task_role: iam.Role = stack.node.find_child("AdtTaskRole")
    for statement in _span_read_statements(stack):
        task_role.add_to_policy(statement)
