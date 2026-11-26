# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Inline code used by the shared Cognito app-client callback updater.

All three Authorization@Edge frontends (the ADT app, ERP dashboard, and
HighByte UI) use the post-distribution custom resource built in
``stacks._edge_auth``: the CloudFront domain is a
CFN token at synth time, so it cannot go in the app client definition without
a circular dependency (client → SAR app → distribution → client). The custom
resource runs after the distribution exists and patches the client's
callback/logout URLs — for BOTH the raw CloudFront domain and the optional
custom-domain alias — merging all other client settings (UpdateUserPoolClient
is a full-replace API; omitting a field clears it).

ONE inline-code home on purpose: the shared facade owns every construct that
uses this constant.
"""

CALLBACK_UPDATER_CODE = '''
import json
import boto3

def handler(event, context):
    print(json.dumps({k: v for k, v in event.items() if k != "ResourceProperties"}))
    request_type = event.get("RequestType")

    if request_type == "Delete":
        # No-op on delete — the user pool is destroyed with the stack.
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "callback-updater")}

    props = event["ResourceProperties"]
    user_pool_id = props["UserPoolId"]
    client_id = props["ClientId"]
    domain = props["CloudFrontDomain"]
    # Optional custom domain (e.g. erp.adt-demo.example.com) — when set,
    # register callbacks for BOTH the raw CloudFront domain and the alias so
    # sign-in works through either hostname.
    custom_domain = (props.get("CustomDomain") or "").strip()

    domains = [domain]
    if custom_domain and custom_domain != domain:
        domains.append(custom_domain)

    cognito = boto3.client("cognito-idp")

    # Describe first so we can merge all existing settings — UpdateUserPoolClient
    # is a full-replace operation; omitting a field clears it.
    desc = cognito.describe_user_pool_client(
        UserPoolId=user_pool_id,
        ClientId=client_id,
    )["UserPoolClient"]

    kwargs = {
        "UserPoolId": user_pool_id,
        "ClientId": client_id,
        "CallbackURLs": [f"https://{d}/parseauth" for d in domains],
        # Every sign-out return path for every host: the SAR SignOut handler
        # passes https://<host>/ (its RedirectPathSignOut) as logout_uri, and
        # Cognito rejects one that is not registered. Absent = historical.
        "LogoutURLs": [
            f"https://{d}{p}" for d in domains
            for p in (props.get("LogoutPaths") or ["/signout"])
        ],
        # Preserve all existing settings
        "AllowedOAuthFlows": desc.get("AllowedOAuthFlows", ["code"]),
        "AllowedOAuthScopes": desc.get("AllowedOAuthScopes", ["openid", "email", "profile"]),
        "AllowedOAuthFlowsUserPoolClient": desc.get("AllowedOAuthFlowsUserPoolClient", True),
        "SupportedIdentityProviders": desc.get("SupportedIdentityProviders", ["COGNITO"]),
        "ExplicitAuthFlows": desc.get("ExplicitAuthFlows", []),
    }
    if desc.get("AccessTokenValidity"):
        kwargs["AccessTokenValidity"] = desc["AccessTokenValidity"]
    if desc.get("IdTokenValidity"):
        kwargs["IdTokenValidity"] = desc["IdTokenValidity"]
    if desc.get("RefreshTokenValidity"):
        kwargs["RefreshTokenValidity"] = desc["RefreshTokenValidity"]
    # The validity numbers above are meaningless without their units: omitting
    # TokenValidityUnits makes the API re-read them as hours/days (a 60-minute
    # id token would become 60 hours and be rejected). Carry the units along.
    if desc.get("TokenValidityUnits"):
        kwargs["TokenValidityUnits"] = desc["TokenValidityUnits"]

    cognito.update_user_pool_client(**kwargs)
    print(f"Updated client {client_id} callbacks to domains {domains}")

    return {
        "PhysicalResourceId": f"callback-updater-{client_id}",
        "Data": {"CloudFrontDomain": domain},
    }
'''
