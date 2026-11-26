# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AWS Quick web connector ONLY (``quick_connector_enabled``, ADR-0093).

The narrow Quick path: an AWS Quick MCP connector with Service
authentication (OAuth 2.0 client credentials) against the dedicated,
bridge-only gateway ``adt-quick-gw`` (ADR-0071). Everything that path needs,
and nothing else:

* the gateway itself, built by ``AgentCoreStack._build_quick_gateway`` (app.py
  passes the gateway flag when this flag is on);
* the app task env that makes the app provision the ``query-bridge`` target on
  that gateway (``QUICK_API_ENABLED`` + ``QUICK_GATEWAY_ID``/``URL``, the same
  values ``ADTComputeStack._env_quick_integration`` emits under
  ``quick_api_enabled``);
* the ``x-quick-auth`` shared secret the bridge runtime presents to the app.
  As under ``quick_api_enabled``, CloudFormation generates it per account in
  Secrets Manager (``ADTComputeStack.quick_gateway_token_secret``; no synth-time
  value in the template, so a packaged template is the same for every
  account and still carries no secret). The bridge runtime gets the secret's
  ARN (``QUICK_GATEWAY_TOKEN_REF``) and reads the value with the runtime role,
  which may read this one secret
  (``ADTComputeStack.wire_quick_bridge_token_ref``);
* ALB ingress from the AgentCore runtime security group on :80, so the bridge
  runtime reaches the app at ``ADT_APP_INTERNAL_URL`` (the internal ALB only
  admits the CloudFront VPC-origin security group otherwise);
* the connector values as ``ADTAgentCoreStack`` outputs (``QuickTokenUrl``,
  ``QuickClientSecretName``; ``QuickGatewayUrl`` and ``QuickClientId``
  already exist) and the secret of Quick's own client ``adt-quick-service``
  (accepted by ``adt-quick-gw`` only; ``stacks/_quick_gateway_auth.py``)
  copied into the
  Secrets Manager secret ``adt-quick-client-secret``, so the connector form can
  be filled from outputs plus one secret read.

NOT ``QuickApiGatewayStack`` (the SigV4 front door).
Additive post-construction wiring, like ``stacks/_observability.py``: the
opt-in stays one removable seam and no oversized constructor grows. Flag off
(every deploy.sh and engine/frozen/live workshop package by default) leaves
every template byte-identical.
"""
from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_secretsmanager as secretsmanager,
)
from cdk_nag import NagPackSuppression, NagSuppressions

#: The fixed name the connector instructions and the participant policy use.
#: Under the ``adt-*`` prefix the workshop's participant policy already reads.
CLIENT_SECRET_NAME = "adt-quick-client-secret"  # nosec B105 - secret NAME, not a credential
#: CDK context key (app.py; deploy.sh --quick-connector).
CONTEXT_KEY = "quick_connector_enabled"


def connector_enabled(app) -> bool:
    """The opt-in, from ``-c quick_connector_enabled=true`` (not sticky)."""
    return str(app.node.try_get_context(CONTEXT_KEY)).strip().lower() == "true"


def wire_quick_connector(agentcore, compute, *, enabled: bool) -> None:
    """Wire the connector-only Quick path onto the two engine stacks.

    No-op when ``enabled`` is false. Raises when the Quick gateway is missing
    (app.py must build it whenever this flag is on), so a wiring mistake fails
    the synth instead of shipping a bridge with nowhere to live.
    """
    if not enabled:
        return
    if getattr(agentcore, "quick_gateway", None) is None:
        raise ValueError(
            f"{CONTEXT_KEY} needs the adt-quick-gw gateway: pass the Quick gateway "
            "flag to AgentCoreStack whenever the connector is enabled (app.py).")
    _connector_values(agentcore)
    _allow_bridge_to_alb(compute)
    if not compute.quick_api_enabled:
        # quick_api_enabled already emits this env and the token secret the
        # Quick API Gateway injects; both paths together keep that one secret.
        _bridge_env(agentcore, compute)


def _connector_values(agentcore) -> None:
    """Token URL + client secret (by fixed name) for Service authentication.

    The secret is Quick's OWN client's (``adt-quick-service``), never the
    app's service client: attendees read this secret, and only
    ``adt-quick-gw`` accepts that client, so it reaches the bridge tools and
    no system target.
    """
    secret = secretsmanager.Secret(
        agentcore, "QuickClientSecret",
        secret_name=CLIENT_SECRET_NAME,
        description=(
            "Client secret of the adt-quick-service app client, for the AWS Quick "
            "MCP connector (Service authentication). Retrieve: aws secretsmanager "
            f"get-secret-value --secret-id {CLIENT_SECRET_NAME} --query SecretString "
            "--output text"),
        secret_string_value=agentcore.quick_service_client.user_pool_client_secret,
        removal_policy=RemovalPolicy.DESTROY,
    )
    _no_rotation(secret, "a copy of the adt-quick-service client secret; rotating the "
                         "Cognito client secret is a separate operator action")
    # base_url() reads the domain resource's Ref, which is the DEPLOYED prefix
    # even when _pin_domain_to_account overrides it per account.
    CfnOutput(agentcore, "QuickTokenUrl",
              value=f"{agentcore.adt_app_domain.base_url()}/oauth2/token",
              description="OAuth 2.0 token URL for the AWS Quick MCP connector")
    CfnOutput(agentcore, "QuickClientSecretName", value=CLIENT_SECRET_NAME,
              description="AWS Secrets Manager secret holding the connector client secret")


def _allow_bridge_to_alb(compute) -> None:
    """The query-bridge runtime calls the app through the internal ALB."""
    alb_sg = compute.node.find_child("AdtAlbSg")
    runtime_sg = compute.node.find_child("AgentCoreRuntimeSg")
    alb_sg.add_ingress_rule(
        runtime_sg, ec2.Port.tcp(80),
        "AWS Quick query-bridge runtime to the internal ALB")


def _bridge_env(agentcore, compute) -> None:
    """The env that makes the app provision the bridge on adt-quick-gw."""
    container = compute.node.find_child("AdtTaskDef").default_container
    container.add_environment("QUICK_API_ENABLED", "true")
    container.add_environment("QUICK_GATEWAY_ID", agentcore.quick_gateway_id)
    container.add_environment("QUICK_GATEWAY_URL", agentcore.quick_gateway_url)
    # The per-account x-quick-auth secret, shared with --quick-api.
    compute.quick_gateway_token_secret()


def _no_rotation(secret, why: str) -> None:
    """cdk-nag AwsSolutions-SMG4 (no automatic rotation), scoped to *secret*."""
    NagSuppressions.add_resource_suppressions(secret, [NagPackSuppression(
        id="AwsSolutions-SMG4",
        reason=f"Demo sample, no automatic rotation: {why}.")])
