# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Neptune Analytics graph data-plane IAM — the ONE statement helper (ADR-0069).

Why this exists: three roles reach the knowledge graph (the ECS app task role,
the shared AgentCore runtime role, and the onboarding runtime role). A role
that hand-rolls its own ``neptune-graph:*`` statement — or has none — can miss
a grant tier its tools need; for the ``adt_onboarding`` runtime (whose image
carries ``ONBOARD_TOOLS`` and whose server exports the graph-reading subset in
``RUNTIME_TOOLS``) that means it cannot read the graph, and
``neptune_query``'s return-``[]``-on-failure contract turns the denial into
false "the catalog is empty" answers. One helper
means a role cannot silently miss the grant tier its tools need (ADR-0069).

Action tiers (Neptune Analytics openCypher requirements):

* read  — ``ExecuteQuery`` (the data-plane API every query rides) +
  ``ReadDataViaQuery`` (MATCH/RETURN), plus the ``GetGraph``/``ListGraphs``
  control-plane describes the endpoint resolution and health paths use.
* write — adds ``WriteDataViaQuery`` (CREATE/MERGE/SET).
* delete — adds ``DeleteDataViaQuery`` (DELETE/DETACH DELETE). Neptune
  Analytics authorizes each query kind against its matching action, so a role
  that runs ``DETACH DELETE`` (offboard/reset, replace-not-accrete) needs this
  tier — and a role that never deletes (the orchestrator/system runtimes only
  CREATE/MERGE/SET proposals and usage) must NOT carry it (least privilege).

Resource scoping preserves each call site's posture: pass ``graph_id`` where
the stack references the graph (the app task role); ``graph_id=None`` yields
the account/Region-bounded ``graph/*`` used by roles whose stack cannot
reference the graph object without a dependency cycle (the runtime roles —
the digital-thread graph lives in a separate stack).
"""

from __future__ import annotations

from aws_cdk import aws_iam as iam

_READ_ACTIONS = [
    "neptune-graph:ExecuteQuery",
    "neptune-graph:ReadDataViaQuery",
    "neptune-graph:GetGraph",
    "neptune-graph:ListGraphs",
]
_WRITE_ACTIONS = ["neptune-graph:WriteDataViaQuery"]
_DELETE_ACTIONS = ["neptune-graph:DeleteDataViaQuery"]


def graph_data_plane_statement(
    *, region: str, account: str, graph_id: str | None = None,
    write: bool = False, delete: bool = False,
) -> iam.PolicyStatement:
    """The neptune-graph statement for one role (see module docstring).

    ``write=False`` grants the read tier only; ``write=True`` adds
    ``WriteDataViaQuery``; ``delete=True`` adds ``DeleteDataViaQuery``
    (implies write). ``graph_id`` scopes to that graph's ARN; ``None``
    scopes to ``graph/*`` bounded to *account*/*region*.
    """
    resource = f"graph/{graph_id}" if graph_id else "graph/*"
    write = write or delete
    return iam.PolicyStatement(
        actions=[*_READ_ACTIONS, *(_WRITE_ACTIONS if write else []),
                 *(_DELETE_ACTIONS if delete else [])],
        resources=[f"arn:aws:neptune-graph:{region}:{account}:{resource}"],
    )
