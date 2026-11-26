# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Hardening that applies to every deploy except the workshop profile.

The workshop (``with_workshop``: ``deploy.sh --workshop``) is packaged and rehearsed
as-is, so these settings leave its templates byte-identical: on a workshop
synth every helper here does nothing.

app.py calls :func:`harden` once, after the stacks are built. The settings
are applied to the built constructs rather than inside the stack files on
purpose: stacks/highbyte_opcua_stack.py hashes its own source into the OPC UA
server fingerprint (``server_fingerprint``) that the SFC task carries as
OPC_UA_SERVER_REVISIONS, so any edit to that file changes the workshop's SFC
template and restarts SFC on an update.
"""

from __future__ import annotations

from typing import Any, Optional

from aws_cdk import Stack, aws_apigateway as apigw

from stacks import _apigw_log_role

#: The context key the workshop profile sets (app.py, package.py PROFILE_CONTEXT).
WITH_WORKSHOP_KEY = "with_workshop"

#: ALB attribute that rejects requests whose header names hold characters
#: outside [-A-Za-z0-9] (checkov CKV_AWS_131).
DROP_INVALID_HEADERS_ATTRIBUTE = "routing.http.drop_invalid_header_fields.enabled"


def is_workshop(node: Any) -> bool:
    """True on a workshop synth; only the string ``true`` (any case) counts,
    matching app.py's reading of the same key."""
    return str(node.try_get_context(WITH_WORKSHOP_KEY)).lower() == "true"


def offer_totp_mfa(pool: Any) -> None:
    """MFA OPTIONAL with TOTP (authenticator app) as the only second factor on
    a ``cognito.UserPool`` that signs in people, outside the workshop.

    A user who registers an authenticator app is then always challenged for
    a code; a user who has not signs in exactly as before. SMS stays off (it
    needs an SNS role and per-message cost). These are the properties the L2
    ``mfa=Mfa.OPTIONAL, mfa_second_factor=MfaSecondFactor(otp=True)`` sets.
    On the workshop the pool keeps the CloudFormation default (MFA off).
    """
    if is_workshop(pool.node):
        return
    cfn_pool = pool.node.default_child
    cfn_pool.mfa_configuration = "OPTIONAL"
    cfn_pool.enabled_mfas = ["SOFTWARE_TOKEN_MFA"]


def cog2_reason(node: Any, pool: str, workshop_reason: str) -> str:
    """The AwsSolutions-COG2 suppression reason for *pool*.

    cdk-nag's COG2 passes only on MFA REQUIRED, so the suppression stays on
    both paths; only its wording changes off the workshop. The workshop keeps
    *workshop_reason* verbatim (it is written into the template metadata).
    """
    if is_workshop(node):
        return workshop_reason
    return (
        f"{pool}: MFA is OPTIONAL with TOTP, so any user can register an "
        "authenticator app and is then always challenged for it. It is not "
        "REQUIRED so the documented demo user can sign in without enrolling "
        "a device. To require MFA, set MfaConfiguration ON in "
        "stacks/_sample_hardening.py offer_totp_mfa (see README, Security)."
    )


def drop_invalid_header_fields(alb: Any) -> None:
    """Set the drop-invalid-header-fields attribute on *alb* outside the
    workshop. The ADT app's own ALB already sets it on every profile
    (stacks/adt_compute_stack.py)."""
    if not is_workshop(alb.node):
        alb.set_attribute(DROP_INVALID_HEADERS_ATTRIBUTE, "true")


def _rest_api_stacks(network: Any) -> list:
    """The app's stacks (other than *network*) that hold a REST API stage."""
    app = network.node.scope
    return [stack for stack in app.node.children
            if isinstance(stack, Stack) and stack is not network
            and any(isinstance(c, apigw.CfnStage) for c in stack.node.find_all())]


def own_account_log_role(network: Any) -> Optional[Any]:
    """Outside the workshop, make *network* (ADTNetworkStack) the one owner of
    the account's API Gateway CloudWatch role setting, and make every other
    stack of the app that holds a REST API stage depend on it, so the owner
    is created first and deleted last (stacks/_apigw_log_role.py). Call it
    after every stack is built. Returns the owning custom resource, or None
    on a workshop synth."""
    if is_workshop(network.node):
        return None
    setting = _apigw_log_role.ensure_account_log_role(network)
    for stack in _rest_api_stacks(network):
        stack.add_dependency(network)
    return setting


def harden(*, agentcore: Any, erp_dashboard: Optional[Any] = None,
           highbyte: Optional[Any] = None, network: Optional[Any] = None) -> None:
    """Apply the non-workshop hardening to the built stacks.

    - MFA OPTIONAL + TOTP on the pools that sign in people: adt-app
      (ADTAgentCoreStack), erp-dashboard-users and highbyte-ui-users. The
      M2M pool (client credentials, no users) is left as is.
    - drop-invalid-header-fields on the ERP dashboard and HighByte hub ALBs.
    - with *network*: the account's API Gateway CloudWatch role setting is
      owned by ADTNetworkStack, and every REST API stack depends on it
      (:func:`own_account_log_role`).

    *erp_dashboard* and *highbyte* are None when not deployed; a HighByte
    stack without the hub (OPC UA servers only) has no pool or hub ALB.
    """
    offer_totp_mfa(agentcore.adt_user_pool)
    if erp_dashboard is not None:
        offer_totp_mfa(erp_dashboard.user_pool)
        drop_invalid_header_fields(erp_dashboard.alb)
    if highbyte is not None:
        pool = getattr(highbyte, "user_pool", None)
        if pool is not None:
            offer_totp_mfa(pool)
        service = getattr(highbyte, "highbyte_service", None)
        if service is not None:
            drop_invalid_header_fields(service.load_balancer)
    if network is not None:
        own_account_log_role(network)
