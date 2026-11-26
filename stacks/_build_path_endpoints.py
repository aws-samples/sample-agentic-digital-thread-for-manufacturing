# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which workshop child owns the VPC endpoints: ADTNetworkStack or ADTComputeStack.

Two groups of endpoints are placed:

* the build path: the S3 gateway and the ECR api/dkr interface endpoints. The
  Workshop Studio root runs a VPC CodeBuild project (``Custom::ADTExternalImages``)
  minutes after ADTNetworkStack exists and long before ADTComputeStack (test
  event 2026-10-18: its first S3 fetch rode a 3-minute-old NAT and died).
* the service endpoints: Secrets Manager and the two Amazon Neptune Analytics
  services (``neptune-graph-data``, ``neptune-graph``), all with private DNS.
  A private-DNS endpoint takes over the service's public name inside the VPC
  when it is created, and carried no traffic for about two minutes after
  CloudFormation reported it complete (a fresh deployment, 2026-11-11: Compute's Secrets
  Manager endpoint came up while ADTERPLambdaStack's story Lambda, started at
  the same moment, was calling Secrets Manager; the call timed out and the
  create failed). Created with the VPC, the endpoints exist before any VPC
  workload starts, since every child that runs one depends on the network.

A FRESH workshop create wants both groups in the Network stack. A stack created
earlier holds a group in ADTComputeStack, and one template cannot move it:

* CloudFormation updates Network before Compute, so a Network-owned copy would be
  created while Compute's still exists. A VPC takes one private-DNS interface
  endpoint per service and one S3 gateway route per route table, so that create
  fails.
* Compute's S3 gateway endpoint imports the VPC route tables from Network. A
  template where Compute no longer references them drops those exports, and the
  Network update fails with "Cannot delete export ... in use".

So in workshop mode BOTH stacks declare every endpoint, with the construct ids
(and so the logical ids) each placement already used, under a condition on the
``BuildPathEndpoints`` parameter. Its three values are the three placements a
stack can have (:data:`PLACEMENT`), and each keeps its meaning, so an update
that keeps a stack's value never moves an endpoint:

* ``compute``: both groups in Compute (stacks created before either move);
* ``network``: build path in Network, service endpoints in Compute (stacks
  created after the build-path move and before the service move);
* ``network-all``: both groups in Network (a fresh create now).

Exactly one stack creates each endpoint for any value. Compute's copy keeps its
references to Network's route tables, so Network keeps those exports whichever
stack owns the endpoints.

Defaults: a child template takes ``compute`` (a raw ``deploy.sh --workshop`` or
``cdk deploy`` has no early VPC build, and keeps the placement such a deploy
always had). The Workshop Studio root declares the parameter with default
``network-all`` and passes it to both children, so an event (which passes only
the assets parameters) gets every endpoint with the VPC. Updating a running
root must pass the value it was created with; ``tools/rehearse-event.sh update``
in the workshop repo reads the owner off the running stack and passes it.

Standalone (non-workshop) templates are unchanged: Compute creates the endpoints
unconditionally and no parameter is declared.
"""
from __future__ import annotations

from aws_cdk import CfnCondition, CfnParameter, CfnResource, Fn, Stack, aws_ec2 as ec2

PARAMETER = "BuildPathEndpoints"
NETWORK = "network"
COMPUTE = "compute"
NETWORK_ALL = "network-all"
OWNERS = (NETWORK, COMPUTE, NETWORK_ALL)
#: The two stacks an endpoint group can live in.
STACKS = (NETWORK, COMPUTE)
#: A child template's default: the placement a deploy.sh --workshop stack has.
CHILD_DEFAULT = COMPUTE
#: The Workshop Studio root's default: a fresh event create.
ROOT_DEFAULT = NETWORK_ALL
#: Endpoint groups.
BUILD_PATH = "build-path"
SERVICE = "service"
#: {parameter value: {group: the stack that creates that group}}.
PLACEMENT = {
    COMPUTE: {BUILD_PATH: COMPUTE, SERVICE: COMPUTE},
    NETWORK: {BUILD_PATH: NETWORK, SERVICE: COMPUTE},
    NETWORK_ALL: {BUILD_PATH: NETWORK, SERVICE: NETWORK},
}
#: Construct ids, shared by both placements (each stack's logical ids follow).
S3_ID, ECR_API_ID, ECR_DKR_ID = "S3Endpoint", "EcrApi", "EcrDkr"
SECRETS_ID, NEPTUNE_DATA_ID, NEPTUNE_ID = "SecretsEndpoint", "NeptuneGraphData", "NeptuneGraph"
_CONDITION_PREFIX = {BUILD_PATH: "BuildPathEndpointsIn", SERVICE: "ServiceEndpointsIn"}

DESCRIPTION = (
    "Which child stack owns the VPC endpoints. network-all: all of them in the network "
    "stack (a fresh create). network: Amazon S3 gateway and Amazon ECR api/dkr in the "
    "network stack, AWS Secrets Manager and Amazon Neptune Analytics in the compute stack. "
    "compute: all of them in the compute stack. Keep the value an existing stack was "
    "created with: moving an endpoint between the two stacks fails the update."
)


def condition_name(owner: str, group: str = BUILD_PATH) -> str:
    """The condition under which stack ``owner`` creates ``group``."""
    return _CONDITION_PREFIX[group] + owner.capitalize()


def values_creating(owner: str, group: str) -> tuple[str, ...]:
    """The parameter values for which stack ``owner`` creates ``group``."""
    return tuple(v for v in OWNERS if PLACEMENT[v][group] == owner)


def owner_parameter(stack: Stack, default: str = CHILD_DEFAULT) -> CfnParameter:
    """The stack's ``BuildPathEndpoints`` parameter (declared once)."""
    if default not in OWNERS:
        raise ValueError(f"{PARAMETER} default {default!r} is not one of {OWNERS}")
    existing = stack.node.try_find_child(PARAMETER)
    if existing is not None:
        return existing
    return CfnParameter(stack, PARAMETER, type="String", allowed_values=list(OWNERS),
                        default=default, description=DESCRIPTION)


def _condition(stack: Stack, owner: str, group: str) -> CfnCondition:
    if owner not in STACKS:
        raise ValueError(f"owner {owner!r} is not one of {STACKS}")
    value = owner_parameter(stack).value_as_string
    tests = [Fn.condition_equals(value, v) for v in values_creating(owner, group)]
    expression = tests[0] if len(tests) == 1 else Fn.condition_or(*tests)
    return CfnCondition(stack, condition_name(owner, group), expression=expression)


def _only_when(condition: CfnCondition, endpoints) -> None:
    """The endpoint and everything it brings (an interface endpoint's security
    group) exist only under the condition."""
    for endpoint in endpoints:
        for child in endpoint.node.find_all():
            if isinstance(child, CfnResource):
                child.cfn_options.condition = condition


def _private_subnets() -> ec2.SubnetSelection:
    return ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)


def add_build_path_endpoints(stack: Stack, vpc: ec2.IVpc, owner: str) -> None:
    """The S3/ECR endpoints in ``stack``, created only when ``owner`` owns them."""
    condition = _condition(stack, owner, BUILD_PATH)
    _only_when(condition, (
        ec2.GatewayVpcEndpoint(stack, S3_ID, vpc=vpc,
                               service=ec2.GatewayVpcEndpointAwsService.S3),
        ec2.InterfaceVpcEndpoint(stack, ECR_API_ID, vpc=vpc,
                                 service=ec2.InterfaceVpcEndpointAwsService.ECR,
                                 subnets=_private_subnets()),
        ec2.InterfaceVpcEndpoint(stack, ECR_DKR_ID, vpc=vpc,
                                 service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
                                 subnets=_private_subnets()),
    ))


def service_endpoints(stack: Stack, vpc: ec2.IVpc) -> tuple[ec2.InterfaceVpcEndpoint, ...]:
    """Secrets Manager and the two Neptune Analytics endpoints, private DNS on.

    Amazon Neptune Analytics is private (public_connectivity=False), so the
    VPC-attached app task and AgentCore runtimes reach the graph through
    PrivateLink. The data-plane service (neptune-graph-data, private DNS
    *.neptune-graph.amazonaws.com) covers execute_query against the graph
    endpoint; the control-plane service (neptune-graph) covers get_graph."""
    return (
        ec2.InterfaceVpcEndpoint(stack, SECRETS_ID, vpc=vpc,
                                 service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
                                 subnets=_private_subnets()),
        ec2.InterfaceVpcEndpoint(stack, NEPTUNE_DATA_ID, vpc=vpc,
                                 service=ec2.InterfaceVpcEndpointService(
                                     f"com.amazonaws.{stack.region}.neptune-graph-data", 443),
                                 private_dns_enabled=True, subnets=_private_subnets()),
        ec2.InterfaceVpcEndpoint(stack, NEPTUNE_ID, vpc=vpc,
                                 service=ec2.InterfaceVpcEndpointService(
                                     f"com.amazonaws.{stack.region}.neptune-graph", 443),
                                 private_dns_enabled=True, subnets=_private_subnets()),
    )


def add_service_endpoints(stack: Stack, vpc: ec2.IVpc, owner: str) -> None:
    """The service endpoints in ``stack``, created only when ``owner`` owns them."""
    condition = _condition(stack, owner, SERVICE)
    _only_when(condition, service_endpoints(stack, vpc))
