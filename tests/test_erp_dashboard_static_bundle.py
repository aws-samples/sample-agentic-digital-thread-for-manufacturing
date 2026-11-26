# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ERP dashboard's Streamlit bundle must not run through Lambda@Edge.

Streamlit's index.html preloads about 110 hashed files. Behind the guarded
default behavior each one runs CheckAuth and HttpHeaders, and on a fresh
account the first page load after the Cognito redirect stayed blank (title
"Streamlit") until a reload. ``/static/*`` is therefore its own ungated,
edge-cached behavior (``stacks/_edge_auth.py::public_bundle_behavior``) while
the document, ``/_stcore/*`` and everything else stay behind CheckAuth.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk.assertions import Template

from stacks._edge_auth import STREAMLIT_STATIC_PATH, public_bundle_behavior

_HERE = Path(__file__).resolve().parent
# Managed policy ids (AWS-published, stable).
CACHING_OPTIMIZED_ID = "658327ea-f89d-4fab-a63d-7e88639e58f6"
SECURITY_HEADERS_ID = "67f7725c-6f97-4210-82d7-5512b31e9d03"


def _load_synth_helper():
    spec = importlib.util.spec_from_file_location(
        "_cognito_domain_pinning", _HERE / "test_cognito_domain_pinning.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._synth_template


def _distribution_config(template: dict) -> dict:
    dists = [
        r for r in template["Resources"].values()
        if r["Type"] == "AWS::CloudFront::Distribution"
    ]
    assert len(dists) == 1, dists
    return dists[0]["Properties"]["DistributionConfig"]


def _assert_public_bundle(behavior: dict) -> None:
    assert "LambdaFunctionAssociations" not in behavior, behavior
    assert "FunctionAssociations" not in behavior, behavior
    assert behavior["CachePolicyId"] == CACHING_OPTIMIZED_ID
    assert behavior["ResponseHeadersPolicyId"] == SECURITY_HEADERS_ID
    assert "OriginRequestPolicyId" not in behavior, "nothing forwarded to the origin"
    assert behavior["AllowedMethods"] == ["GET", "HEAD"]
    assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"


def test_public_bundle_behavior_shape():
    """Pure CDK: no edge functions, edge-cached, managed security headers."""
    stack = cdk.Stack(cdk.App(), "PublicBundleProbe")
    origin = origins.HttpOrigin("origin.example.com")
    cloudfront.Distribution(
        stack,
        "Dist",
        default_behavior=cloudfront.BehaviorOptions(origin=origin),
        additional_behaviors={STREAMLIT_STATIC_PATH: public_bundle_behavior(origin=origin)},
    )
    config = _distribution_config(Template.from_stack(stack).to_json())
    (behavior,) = config["CacheBehaviors"]
    assert behavior["PathPattern"] == "/static/*"
    _assert_public_bundle(behavior)


@pytest.fixture(scope="module")
def erp_dashboard_template(tmp_path_factory) -> dict:
    return _load_synth_helper()(
        tmp_path_factory,
        stack="ADTERPDashboardStack",
        context={"region": "us-east-1", "with_simulator": "true"},
        output_slug="cdk_synth_erp_static_bundle",
    )


def test_erp_dashboard_static_bundle_skips_edge_auth(erp_dashboard_template):
    config = _distribution_config(erp_dashboard_template)
    behaviors = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    _assert_public_bundle(behaviors["/static/*"])
    # Everything else stays gated: the document and /_stcore/* fall to the
    # default behavior, which still runs CheckAuth + HttpHeaders.
    default_events = {
        a["EventType"] for a in config["DefaultCacheBehavior"]["LambdaFunctionAssociations"]
    }
    assert default_events == {"viewer-request", "viewer-response"}
    assert set(behaviors) == {"/static/*", "/parseauth", "/refreshauth", "/signout"}
