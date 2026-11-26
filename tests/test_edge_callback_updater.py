# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The callback updater registers the SAR's sign-out return URL.

The SAR SignOut handler (v2.3.2) sends the browser to Cognito ``/logout``
with ``logout_uri=https://<host><RedirectPathSignOut>``; ``deploy_edge_auth``
leaves that SAR parameter at its default ``/``. Cognito rejects a
``logout_uri`` that is not one of the client's registered sign-out URLs, so
the updater must register ``https://<host>/`` next to the historical
``https://<host>/signout`` for every served hostname. Sign-out only reaches
Cognito ``/logout`` since application logout stopped expiring the edge
cookies (ADR-0070 decision 4, 2026-11-05).

``LogoutPaths`` is also a custom-resource property, so changing it re-runs
the updater on existing deployments (a Lambda code change alone would not).
"""
from __future__ import annotations

import sys
import types

import aws_cdk as cdk
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk.assertions import Template

from stacks._edge_auth import EDGE_LOGOUT_PATHS, create_callback_updater
from stacks._edge_callback_updater import CALLBACK_UPDATER_CODE


class _FakeCognito:
    def __init__(self):
        self.updates = []

    def describe_user_pool_client(self, **_kwargs):
        return {"UserPoolClient": {}}

    def update_user_pool_client(self, **kwargs):
        self.updates.append(kwargs)


def _run_handler(props: dict, monkeypatch) -> dict:
    fake = _FakeCognito()
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda _n: fake))
    namespace: dict = {}
    exec(compile(CALLBACK_UPDATER_CODE, "callback_updater", "exec"), namespace)  # nosec B102 - repo-owned constant
    namespace["handler"]({"RequestType": "Update", "ResourceProperties": props}, None)
    (update,) = fake.updates
    return update


BASE = {"UserPoolId": "pool", "ClientId": "client", "CloudFrontDomain": "d111.cloudfront.net"}


def test_logout_paths_are_signout_and_the_sar_return_path():
    assert EDGE_LOGOUT_PATHS == ("/signout", "/")


def test_handler_registers_every_logout_path_for_every_host(monkeypatch):
    update = _run_handler(
        {**BASE, "CustomDomain": "app.example.com", "LogoutPaths": ["/signout", "/"]},
        monkeypatch,
    )
    assert update["CallbackURLs"] == [
        "https://d111.cloudfront.net/parseauth", "https://app.example.com/parseauth"]
    assert update["LogoutURLs"] == [
        "https://d111.cloudfront.net/signout", "https://d111.cloudfront.net/",
        "https://app.example.com/signout", "https://app.example.com/",
    ]


def test_handler_without_logout_paths_keeps_the_old_urls(monkeypatch):
    """An in-flight event from before this change still behaves as before."""
    update = _run_handler(BASE, monkeypatch)
    assert update["LogoutURLs"] == ["https://d111.cloudfront.net/signout"]


def test_updater_resource_carries_the_logout_paths():
    stack = cdk.Stack(cdk.App(), "UpdaterProbe", env=cdk.Environment(region="us-east-1"))
    pool = cognito.UserPool(stack, "Pool")
    client = pool.add_client("Client")
    dist = cloudfront.Distribution(
        stack, "Dist",
        default_behavior=cloudfront.BehaviorOptions(origin=origins.HttpOrigin("o.example.com")),
    )
    create_callback_updater(
        stack, function_name="probe-updater", user_pool=pool, client=client,
        distribution=dist, custom_domain=None,
    )
    (resource,) = Template.from_stack(stack).find_resources(
        "AWS::CloudFormation::CustomResource").values()
    assert resource["Properties"]["LogoutPaths"] == ["/signout", "/"]


def test_sar_sign_out_returns_to_root_and_root_is_registered(monkeypatch):
    """Deliberate trade-off (ADR-0070 amendment 2026-11-05): after Cognito
    /logout the browser returns to ``/``, which runs CheckAuth and starts
    sign-in again; with an IdP session (oidc_only) the user lands back in.
    There is no anonymous signed-out landing page, because it would itself be
    an unauthenticated HTML 200. Pinned so the choice can't drift: the SAR's
    RedirectPathSignOut stays at its ``/`` default (``deploy_edge_auth`` does
    not override it) and ``https://<host>/`` stays a registered logout URL."""
    from stacks._edge_auth import deploy_edge_auth, edge_http_headers

    stack = cdk.Stack(cdk.App(), "SignOutProbe", env=cdk.Environment(region="us-east-1"))
    pool = cognito.UserPool(stack, "Pool")
    client = pool.add_client("Client")
    deploy_edge_auth(stack, user_pool=pool, client=client,
                     http_headers=edge_http_headers(allow_websockets=False))
    (app,) = Template.from_stack(stack).find_resources(
        "AWS::Serverless::Application").values()
    params = app["Properties"]["Parameters"]
    assert "RedirectPathSignOut" not in params, "SAR default '/' is the sign-out return"
    assert "/" in EDGE_LOGOUT_PATHS
    update = _run_handler({**BASE, "LogoutPaths": list(EDGE_LOGOUT_PATHS)}, monkeypatch)
    assert "https://d111.cloudfront.net/" in update["LogoutURLs"]
