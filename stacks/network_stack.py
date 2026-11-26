# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Network Stack for ERP Beverage Simulation System

This stack creates the foundational networking infrastructure:
- VPC with public and private subnets across 2 availability zones
- Internet Gateway for public subnet internet access
- NAT Gateway for private subnet internet access
- Exports (VPC ID + subnet IDs) for use by dependent stacks

The demo/OT/bastion security groups (Aurora, Lambda, ECS, ALB, Bastion,
HighByte, OPC UA) are intentionally NOT defined here — they are owned by the
opt-in demo stacks that consume them, so a base (engine-only) deploy carries
no demo networking. See stacks/erp_database_stack.py, erp_dashboard_stack.py,
and highbyte_opcua_stack.py.

Requirements: 3.2, 7.1, 7.2, 7.3, 7.4, 7.5
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_logs as logs,
)
from constructs import Construct

from stacks import _build_path_endpoints


class NetworkStack(Stack):
    """
    Network Stack creating VPC, subnets, and NAT gateway.

    This stack has no dependencies (and no dependency on any demo stack) and
    must be deployed first.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, description="ADT - VPC, subnets, NAT gateway, security groups", **kwargs)

        # Get configuration from context (cdk.json)
        vpc_config = self.node.try_get_context("vpc")
        vpc_cidr = vpc_config.get("cidr", "10.0.0.0/16")
        max_azs = vpc_config.get("max_azs", 2)
        nat_gateways = vpc_config.get("nat_gateways", 1)
        public_subnet_cidr_mask = vpc_config.get("public_subnet_cidr_mask", 24)
        private_subnet_cidr_mask = vpc_config.get("private_subnet_cidr_mask", 24)

        # Create VPC with public and private subnets
        # Create the VPC with public and private subnets across two AZs
        self.vpc = ec2.Vpc(
            self,
            "ErpVpc",
            ip_addresses=ec2.IpAddresses.cidr(vpc_cidr),
            max_azs=max_azs,
            nat_gateways=nat_gateways,
            subnet_configuration=[
                # Public subnets for ALB and NAT Gateway
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=public_subnet_cidr_mask,
                ),
                # Private subnets for Lambda, ECS, Aurora, HighByte, OPC UA
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=private_subnet_cidr_mask,
                ),
            ],
            enable_dns_hostnames=True,
            enable_dns_support=True,
        )

        flow_log_group = logs.LogGroup(
            self,
            "VpcFlowLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.vpc.add_flow_log(
            "VpcFlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

        # NAT Gateway and routing are automatically handled by CDK
        # - NAT Gateway is created in the first public subnet
        # - Route tables are automatically configured for private subnets
        # - Internet Gateway is automatically created for public subnets

        # NOTE (decoupling): the demo/OT/bastion security groups (Aurora, Lambda,
        # ECS, ALB, Bastion, HighByte, OPC UA) do NOT live here. They are
        # ONLY consumed by the opt-in demo stacks (ADTERPDatabaseStack,
        # ADTERPLambdaStack, ADTERPDashboardStack) and the OT stack
        # (ADTHighByteOpcuaStack); creating them here would make a base
        # (engine-only) deploy fail — dropping the demo stacks leaves
        # CloudFormation unable to delete the SG exports ("Cannot delete export
        # ... in use by ADTERPDashboardStack"). Each SG (and every one of its
        # ingress/egress rules) lives in the demo stack that owns it, so
        # ADTNetworkStack has no dependency on any demo stack. This stack
        # provides only the VPC, subnets, NAT gateway, and the VPC/subnet
        # exports the engine uses.

        # Workshop deploys only: the VPC endpoints can be created HERE, with
        # the VPC, instead of in ADTComputeStack, as BuildPathEndpoints says
        # (see _create_build_path_endpoints).
        if self._with_workshop():
            self._create_build_path_endpoints()

        # Export shared VPC identifiers
        self._create_exports()

    def _with_workshop(self) -> bool:
        """The deterministic workshop profile (deploy.sh --workshop / the
        Workshop Studio packaging synth) sets ``with_workshop=true``."""
        return str(self.node.try_get_context("with_workshop")).lower() == "true"

    def _create_build_path_endpoints(self) -> None:
        """The VPC endpoints, created with the VPC.

        Test event 2026-10-18 (Team 1): the Workshop Studio root's
        ``Custom::ADTExternalImages`` CodeBuild runs in this VPC's private
        subnets and depends only on this stack, so it started 3 minutes after
        the NAT gateway existed and rode it for its very first command, the
        ``aws s3 cp`` of cr-code.zip. That call died on a socket timeout and
        the deployment failed. The endpoints ADTComputeStack creates would have
        carried that traffic privately, but Compute is created much later.

        A fresh deployment, 2026-11-11: Compute's private-DNS Secrets Manager endpoint
        came up while ADTERPLambdaStack (started at the same moment) was
        calling Secrets Manager; the call timed out and the create failed.

        So a fresh workshop create (``network-all``) puts every endpoint here:
        S3 gateway, ECR api/dkr, Secrets Manager and the two Neptune Analytics
        services. Each private-DNS endpoint has taken over its service's name
        before any VPC workload starts. Stacks created before a move keep the
        moved endpoints in ADTComputeStack and are updated with the value they
        were created with; both stacks declare every endpoint under a
        condition (stacks/_build_path_endpoints.py). Standalone deploys keep
        the Compute placement unconditionally.
        """
        _build_path_endpoints.add_build_path_endpoints(
            self, self.vpc, _build_path_endpoints.NETWORK)
        _build_path_endpoints.add_service_endpoints(
            self, self.vpc, _build_path_endpoints.NETWORK)

    def _create_exports(self) -> None:
        """
        Export VPC and security group IDs for use by dependent stacks.
        
        Exports:
        - VPC ID
        - Public subnet IDs
        - Private subnet IDs
        - All security group IDs
        
        Requirements: 7.5
        """

        # Export VPC ID
        CfnOutput(
            self,
            "VpcId",
            value=self.vpc.vpc_id,
            description="VPC ID",
            export_name=f"{self.stack_name}-VpcId",
        )

        # Export public subnet IDs
        public_subnet_ids = [subnet.subnet_id for subnet in self.vpc.public_subnets]
        CfnOutput(
            self,
            "PublicSubnetIds",
            value=",".join(public_subnet_ids),
            description="Public subnet IDs (comma-separated)",
            export_name=f"{self.stack_name}-PublicSubnetIds",
        )

        # Export private subnet IDs
        private_subnet_ids = [subnet.subnet_id for subnet in self.vpc.private_subnets]
        CfnOutput(
            self,
            "PrivateSubnetIds",
            value=",".join(private_subnet_ids),
            description="Private subnet IDs (comma-separated)",
            export_name=f"{self.stack_name}-PrivateSubnetIds",
        )

        # NOTE (decoupling): there are no per-security-group CfnOutputs (Aurora,
        # Lambda, ECS, ALB, HighByte, OPC UA, Bastion) here. The SGs are created
        # and exported (where needed) by the demo/OT stacks that own them, so
        # ADTNetworkStack exports only VPC and subnet identifiers.
