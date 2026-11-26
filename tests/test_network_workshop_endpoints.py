# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Workshop mode: ADTNetworkStack declares every VPC endpoint the engine uses.

Test event 2026-10-18 (Team 1): the Workshop Studio root's external-images
CodeBuild runs in the VPC's private subnets and depends only on the network
child, so its first command (``aws s3 cp`` of cr-code.zip) rode a NAT gateway
that was 3 minutes old and died on a socket timeout; the deployment failed.
ADTComputeStack's endpoints would have carried that privately, but Compute is
created much later. A fresh workshop create (``BuildPathEndpoints=network``,
the root's default) therefore gets the three endpoints with the VPC. A stack
created before that move keeps them in Compute (``compute``); both copies are
conditional, and tests/test_build_path_endpoint_owner.py pins the one-owner
rule. Standalone deploys are unchanged (no endpoints here).

A fresh deployment, 2026-11-11: Compute's private-DNS Secrets Manager endpoint came up
while ADTERPLambdaStack's story Lambda was calling Secrets Manager, and the
call timed out. So the Secrets Manager and the two Neptune Analytics endpoints
are declared here too, created on a fresh create (``network-all``).

Synthesized in-process (no CDK CLI, no AWS calls).
"""
from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.network_stack import NetworkStack

_REPO = Path(__file__).resolve().parents[1]
_CDK_CONTEXT = json.loads((_REPO / "cdk.json").read_text(encoding="utf-8"))["context"]
_ENV = cdk.Environment(account="111122223333", region="us-east-1")

_S3 = "com.amazonaws.us-east-1.s3"
_ECR_API = "com.amazonaws.us-east-1.ecr.api"
_ECR_DKR = "com.amazonaws.us-east-1.ecr.dkr"
_SECRETS = "com.amazonaws.us-east-1.secretsmanager"
_NEPTUNE_DATA = "com.amazonaws.us-east-1.neptune-graph-data"
_NEPTUNE = "com.amazonaws.us-east-1.neptune-graph"


def _network_template(**extra_context) -> Template:
    app = cdk.App(context={**_CDK_CONTEXT, "aws:cdk:bundling-stacks": [], **extra_context})
    return Template.from_stack(NetworkStack(app, "ADTNetworkStack", env=_ENV))


def _endpoint_services(template: Template) -> dict[str, str]:
    """{service name: VpcEndpointType} for every endpoint in the template."""
    return {
        _endpoint_services_of(res): res["Properties"].get("VpcEndpointType", "Gateway")
        for res in template.find_resources("AWS::EC2::VPCEndpoint").values()
    }


def test_workshop_network_owns_the_build_path_endpoints():
    services = _endpoint_services(_network_template(with_workshop="true"))
    assert services.get(_S3) == "Gateway"
    assert services.get(_ECR_API) == "Interface"
    assert services.get(_ECR_DKR) == "Interface"


def test_workshop_network_owns_the_service_endpoints():
    """Secrets Manager and Neptune Analytics come up with the VPC, before any
    VPC workload that calls them, created only on a ``network-all`` create."""
    template = _network_template(with_workshop="true")
    services = _endpoint_services(template)
    for service in (_SECRETS, _NEPTUNE_DATA, _NEPTUNE):
        assert services.get(service) == "Interface", service
    conditions = template.to_json()["Conditions"]
    for res in template.find_resources("AWS::EC2::VPCEndpoint").values():
        if _endpoint_services_of(res) in (_SECRETS, _NEPTUNE_DATA, _NEPTUNE):
            expression = conditions[res["Condition"]]
            assert expression == {"Fn::Equals": [{"Ref": "BuildPathEndpoints"}, "network-all"]}


def test_workshop_s3_gateway_routes_every_private_route_table():
    """A gateway endpoint only helps a subnet whose route table it is attached
    to; the VPC CodeBuild runs in the private subnets."""
    template = _network_template(with_workshop="true")
    private_tables = {
        logical for logical, res in template.find_resources("AWS::EC2::RouteTable").items()
        if "Private" in logical
    }
    assert private_tables, "no private route tables synthesized"
    (s3,) = [res for res in template.find_resources("AWS::EC2::VPCEndpoint").values()
             if _endpoint_services_of(res) == _S3]
    routed = {ref["Ref"] for ref in s3["Properties"]["RouteTableIds"]}
    assert private_tables <= routed


def test_workshop_interface_endpoints_use_private_dns():
    """The build reaches ECR by its public hostnames; private DNS is what makes
    those names resolve to the endpoint instead of the NAT path."""
    template = _network_template(with_workshop="true")
    interfaces = [res for res in template.find_resources("AWS::EC2::VPCEndpoint").values()
                  if res["Properties"].get("VpcEndpointType") == "Interface"]
    assert len(interfaces) == 5
    for res in interfaces:
        assert res["Properties"]["PrivateDnsEnabled"] is True


def test_standalone_network_has_no_endpoints():
    """Standalone deploys keep the endpoints in ADTComputeStack; the network
    stack must not add a second copy (one private-DNS endpoint per service
    per VPC)."""
    assert _endpoint_services(_network_template()) == {}
    assert _endpoint_services(_network_template(with_workshop="false")) == {}


def _endpoint_services_of(res: dict) -> str:
    service = res["Properties"]["ServiceName"]
    if isinstance(service, dict):
        service = "".join(
            p if isinstance(p, str) else "us-east-1" for p in service["Fn::Join"][1])
    return service
