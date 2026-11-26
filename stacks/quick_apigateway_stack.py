# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Quick API Gateway — REST front door to the ADT app for AWS Quick.

Exposes the ADT async query pair through a regional REST API, reaching the
internal ALB over a V2 VPC Link. Auth is
IAM/SigV4 only, under the `/iam/*` path: every caller signs with an in-account
AWS identity — an instance role or the customer's own account credentials. No stored
secret, no API key (long-lived static keys are against the project's
credential standard).

Long-running queries: the `submit`/`result` routes back the async ADT endpoints
(/api/ask/async + /api/ask/result/{job_id}) so a 90-120s query is never carried
by a single call — required for AWS Quick's 60s MCP per-call timeout.

Deployed only with `-c quick_api_enabled=true` (deploy.sh --quick-api).
"""

from aws_cdk import (
    Aws, Fn, Stack, CfnOutput, RemovalPolicy,
    aws_apigateway as apigw,
    aws_apigatewayv2 as apigwv2,
    aws_ec2 as ec2,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from stacks import _sample_hardening

#: Access-log record for the prod stage: one JSON object per request, no
#: caller identity or user fields; the source IP is kept for abuse review.
#: Fields are API Gateway $context variables.
ACCESS_LOG_FORMAT = (
    '{"requestId":"$context.requestId","ip":"$context.identity.sourceIp",'
    '"requestTime":"$context.requestTime","httpMethod":"$context.httpMethod",'
    '"resourcePath":"$context.resourcePath","status":"$context.status",'
    '"protocol":"$context.protocol","responseLength":"$context.responseLength",'
    '"integrationLatency":"$context.integrationLatency"}'
)

# ── Route table ──────────────────────────────────────────────────────────────
# (path_part, http_method, ALB path). Only the async pair the kiosk MCP
# server calls (`submit_query`, `get_query_result`): the app admits the
# x-quick-auth token on exactly those paths (AuthMiddleware), so any other
# route here would answer 401 to every signed call. The `result` route is
# built separately (it takes a {job_id} path parameter).
_ROUTES = [
    ("submit",           "POST", "/api/ask/async"),
]


class QuickApiGatewayStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, vpc, alb_dns_name: str, alb_arn: str,
                 alb_security_group_id: str,
                 gateway_token_secret: secretsmanager.ISecret, **kwargs):
        super().__init__(scope, construct_id,
                         description="ADT - Quick API Gateway (IAM/SigV4 front door to the ADT app for AWS Quick)",
                         **kwargs)
        self._alb_dns_name = alb_dns_name
        self._alb_arn = alb_arn

        self._gateway_token_header = self._gateway_token_header_value(gateway_token_secret)
        self._vpc_link = self._create_vpc_link(vpc, alb_security_group_id)
        api = self._create_rest_api()
        self._query_model, self._validator = self._create_model_and_validator(api)

        # IAM/SigV4 under /iam/* — the ONLY auth path.
        methods = self._build_iam_branch(api)
        stage = self._deploy_stage(api, methods)

        self.rest_api_id = api.ref
        self.root_resource_id = api.attr_root_resource_id
        self.stage_name = stage.stage_name
        self._emit_outputs(api)

    @staticmethod
    def _gateway_token_header_value(secret: secretsmanager.ISecret) -> str:
        """The x-quick-auth header value: the gateway service token, by reference.

        The app's AuthMiddleware requires a Cognito browser session for
        /api/ask/*. The Quick path has no such session, so API Gateway (which
        is itself IAM-authenticated) injects this shared secret as the
        x-quick-auth header on every integration request; the app bypasses the
        cookie check only when it matches. The secret is generated per account
        by Secrets Manager in ADTComputeStack (quick_gateway_token_secret),
        and the app reads it at task start (QUICK_GATEWAY_TOKEN). This value
        is a quoted static header value around a secretsmanager dynamic
        reference, which CloudFormation resolves when it creates or updates
        the method, so the template holds the secret ARN and never the token.
        On the public ADT CloudFront path the header is stripped at the edge
        (stacks/_quick_header_strip.py) and refused by the middleware on any
        CloudFront-forwarded request. The token grants only the async ask
        endpoints, never admin surfaces.
        """
        return Fn.join("", ["'", secret.secret_value.unsafe_unwrap(), "'"])

    def _create_vpc_link(self, vpc, alb_security_group_id: str):
        """V2 VPC Link — supports the internal ALB directly (V1 requires NLB).

        The internal ALB admits only the CloudFront VPC-origin group, so this
        stack adds one rule letting the VPC link's group reach the listener
        port. The rule lives here, so it goes when the Quick API does."""
        private_subnets = vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        vpc_link_sg = ec2.SecurityGroup(self, "QuickVpcLinkSg", vpc=vpc,
            description="VPC Link to internal ALB for Quick")
        ec2.CfnSecurityGroupIngress(self, "QuickVpcLinkToAlb",
            group_id=alb_security_group_id, ip_protocol="tcp", from_port=80, to_port=80,
            source_security_group_id=vpc_link_sg.security_group_id,
            description="Quick API Gateway VPC link to the internal ALB")
        return apigwv2.CfnVpcLink(self, "QuickVpcLink",
            name="adt-quick-vpclink",
            subnet_ids=private_subnets.subnet_ids,
            security_group_ids=[vpc_link_sg.security_group_id])

    def _create_rest_api(self):
        """The regional REST API with a this-account RESOURCE policy.

        The real access control is the METHOD authorizer — AWS_IAM on /iam/*
        (SigV4 still required on every method). A single-principal Allow here
        would implicitly DENY IAM-user callers, so the principal stays ``*``
        and the ``aws:PrincipalAccount`` condition limits callers to IAM
        principals of this account (the kiosk instance role is one).
        """
        return apigw.CfnRestApi(self, "QuickApi",
            name="adt-quick-mcp",
            endpoint_configuration={"types": ["REGIONAL"]},
            policy={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "execute-api:Invoke",
                    "Resource": "execute-api:/*",
                    "Condition": {"StringEquals": {"aws:PrincipalAccount": Aws.ACCOUNT_ID}},
                }],
            })

    def _create_model_and_validator(self, api):
        """The POST body model + the basic request validator (APIG2)."""
        query_model = apigw.CfnModel(self, "QueryModel",
            rest_api_id=api.ref, name="QueryRequest",
            content_type="application/json",
            schema={
                "$schema": "http://json-schema.org/draft-04/schema#",
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "plant": {"type": "string"},
                },
                "required": ["question"],
            })
        validator = apigw.CfnRequestValidator(self, "QuickRequestValidator",
            rest_api_id=api.ref, name="quick-basic",
            validate_request_body=True, validate_request_parameters=True)
        return query_model, validator

    def _integration(self, http_method: str, alb_path: str,
                     path_params: "dict | None" = None):
        """HTTP_PROXY integration to the internal ALB through the VPC link,
        injecting the gateway service token so the ADT app trusts this request
        came through the (authenticated) API Gateway."""
        req_params = {
            "integration.request.header.x-quick-auth": self._gateway_token_header,
        }
        if path_params:
            # Map REST path params -> integration URI path params.
            req_params.update({
                f"integration.request.path.{k}": f"method.request.path.{v}"
                for k, v in path_params.items()
            })
        return apigw.CfnMethod.IntegrationProperty(
            type="HTTP_PROXY",
            integration_http_method=http_method,
            uri=f"http://{self._alb_dns_name}{alb_path}",
            connection_type="VPC_LINK",
            connection_id=self._vpc_link.ref,
            integration_target=self._alb_arn,
            passthrough_behavior="WHEN_NO_MATCH",
            request_parameters=req_params,
        )

    def _build_iam_branch(self, api, base_part: str = "iam") -> list:
        """Render the full route set under /iam/* with AWS_IAM method auth."""
        methods = []
        base = apigw.CfnResource(self, f"Res-{base_part}",
            rest_api_id=api.ref, parent_id=api.attr_root_resource_id,
            path_part=base_part)

        for path, http_method, alb_path in _ROUTES:
            resource = apigw.CfnResource(self, f"Res-{base_part}-{path}",
                rest_api_id=api.ref, parent_id=base.ref, path_part=path)
            m_kwargs = dict(
                rest_api_id=api.ref,
                resource_id=resource.ref,
                http_method=http_method,
                authorization_type="AWS_IAM",
                api_key_required=False,
                request_validator_id=self._validator.ref,
                method_responses=[{"statusCode": "200"}],
                integration=self._integration(http_method, alb_path),
            )
            if http_method == "POST":
                m_kwargs["request_models"] = {"application/json": self._query_model.ref}
            method = apigw.CfnMethod(self, f"Method-{base_part}-{path}", **m_kwargs)
            method.add_dependency(resource)
            methods.append(method)

        methods.append(self._build_result_route(api, base))
        return methods

    def _build_result_route(self, api, base):
        """`result/{job_id}` GET (async poll) — a job_id path parameter mapped
        onto the ALB path."""
        base_part = base.path_part
        result_base = apigw.CfnResource(self, f"Res-{base_part}-result",
            rest_api_id=api.ref, parent_id=base.ref, path_part="result")
        result_job = apigw.CfnResource(self, f"Res-{base_part}-result-jobid",
            rest_api_id=api.ref, parent_id=result_base.ref, path_part="{job_id}")
        result_method = apigw.CfnMethod(self, f"Method-{base_part}-result",
            rest_api_id=api.ref,
            resource_id=result_job.ref,
            http_method="GET",
            authorization_type="AWS_IAM",
            api_key_required=False,
            request_validator_id=self._validator.ref,
            request_parameters={"method.request.path.job_id": True},
            method_responses=[{"statusCode": "200"}],
            integration=self._integration(
                "GET", "/api/ask/result/{job_id}", {"job_id": "job_id"}),
        )
        result_method.add_dependency(result_job)
        return result_method

    def _deploy_stage(self, api, methods):
        # A deployment is a snapshot of the methods: changing an integration
        # does not reach the stage until a new deployment replaces it, so the
        # logical id is bumped whenever a method or integration changes. V7:
        # the x-quick-auth header moved from a literal (V6) to the secret's
        # dynamic reference. V8: only the submit and result routes remain.
        deployment = apigw.CfnDeployment(self, "QuickDeploymentV8", rest_api_id=api.ref)
        for m in methods:
            deployment.add_dependency(m)
        if _sample_hardening.is_workshop(self.node):
            # A workshop event deploy keeps the stage exactly as rehearsed.
            return apigw.CfnStage(self, "QuickProdStage",
                rest_api_id=api.ref, deployment_id=deployment.ref, stage_name="prod")
        stage = apigw.CfnStage(self, "QuickProdStage",
            rest_api_id=api.ref, deployment_id=deployment.ref, stage_name="prod",
            access_log_setting=apigw.CfnStage.AccessLogSettingProperty(
                destination_arn=self._access_log_group().log_group_arn,
                format=ACCESS_LOG_FORMAT,
            ))
        # The account's API Gateway CloudWatch role is owned by ADTNetworkStack
        # (stacks/_apigw_log_role.py), which this stack depends on.
        return stage

    def _access_log_group(self, construct_id: str = "QuickApiAccessLogs") -> logs.LogGroup:
        """A log group of this stack's own, kept one month."""
        return logs.LogGroup(self, construct_id,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY)

    def _emit_outputs(self, api):
        base_url = f"https://{api.ref}.execute-api.{self.region}.amazonaws.com/prod"
        CfnOutput(self, "ApiGatewayUrl", value=base_url)
        CfnOutput(self, "ApiGatewayIamBase", value=f"{base_url}/iam",
                  description="IAM/SigV4 base path (instance role or customer credentials)")
        CfnOutput(self, "RestApiId", value=api.ref)
