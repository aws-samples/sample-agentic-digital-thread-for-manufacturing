# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ADT app distribution strips ``x-quick-auth`` from viewer requests.

The Quick gateway shared secret admits ``/api/ask/async``,
``/api/ask/result/*`` and ``GET /api/prompts`` without a Cognito session.
Its legitimate senders reach the internal ALB directly; the public app
distribution forwards viewer headers (``ALL_VIEWER_EXCEPT_HOST_HEADER``), so
a viewer-request CloudFront Function deletes the header there
(``stacks/_quick_header_strip.py``). The app's own refusal of the secret on
CloudFront-forwarded requests is pinned in
``agentic_digital_thread/tests/unit/test_quick_gateway_auth.py``.

Pinned here:

* the function code deletes exactly ``x-quick-auth`` and nothing else, and
  the header name equals the middleware's ``QUICK_GATEWAY_HEADER``;
* edge auth OFF (the ``--workshop`` default): ONE function, on the default
  behavior, which serves every path;
* edge auth ON: the function rides the ``/api/*`` behavior ONLY; the default
  behavior keeps CheckAuth on viewer request and no CloudFront Function
  (one event type cannot carry both);
* the ERP dashboard and HighByte distributions are untouched.

The synth tests need the vendored CDK CLI under node_modules and skip without
it (mirrors tests/test_edge_external_oidc.py).
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess  # nosec B404 - fixed internal argv lists only
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk.assertions import Template

from stacks._quick_header_strip import (
    QUICK_GATEWAY_HEADER,
    STRIP_FUNCTION_CODE,
    create_quick_header_strip,
    quick_header_strip_associations,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_BIN = REPO_ROOT / "node_modules" / ".bin" / "cdk"
MIDDLEWARE = REPO_ROOT / "agentic_digital_thread" / "agents" / "auth" / "middleware.py"
COMPUTE = "ADTComputeStack"
ERP = "ADTERPDashboardStack"
HIGHBYTE = "ADTHighByteOpcuaStack"


# ---------------------------------------------------------------------------
# The function itself
# ---------------------------------------------------------------------------

def _middleware_header_name() -> str:
    """``AuthMiddleware.QUICK_GATEWAY_HEADER`` read from source (no app import)."""
    tree = ast.parse(MIDDLEWARE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AuthMiddleware":
            for stmt in node.body:
                if (isinstance(stmt, ast.Assign)
                        and any(getattr(t, "id", None) == "QUICK_GATEWAY_HEADER"
                                for t in stmt.targets)):
                    return ast.literal_eval(stmt.value)
    raise AssertionError("AuthMiddleware.QUICK_GATEWAY_HEADER not found")


def test_header_name_matches_the_middleware():
    assert QUICK_GATEWAY_HEADER == _middleware_header_name() == "x-quick-auth"


def test_function_code_deletes_exactly_the_quick_header():
    assert STRIP_FUNCTION_CODE.count("delete ") == 1
    assert "delete request.headers['x-quick-auth'];" in STRIP_FUNCTION_CODE
    assert "return request;" in STRIP_FUNCTION_CODE


def test_function_behaves_as_written_under_node():
    """Run the handler on a CloudFront Functions event shape: the secret goes,
    every other header (and any repeated value) is left as it was."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    event = {"request": {"method": "POST", "uri": "/api/ask/async", "headers": {
        "x-quick-auth": {"value": "secret", "multiValue": [
            {"value": "secret"}, {"value": "second"}]},
        "cookie": {"value": "adt_session=abc"},
        "content-type": {"value": "application/json"},
    }}}
    script = STRIP_FUNCTION_CODE + (
        "\nprocess.stdout.write(JSON.stringify(handler("
        + json.dumps(event) + ")));\n")
    result = subprocess.run(  # nosec B603 - fixed argv, internal script
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    request = json.loads(result.stdout)
    assert request["uri"] == "/api/ask/async"
    assert request["headers"] == {
        "cookie": {"value": "adt_session=abc"},
        "content-type": {"value": "application/json"},
    }


def test_helper_builds_a_js2_viewer_request_function():
    stack = cdk.Stack(cdk.App(), "QuickHeaderStripProbe")
    origin = origins.HttpOrigin("origin.example.com")
    cloudfront.Distribution(
        stack, "Dist",
        default_behavior=cloudfront.BehaviorOptions(
            origin=origin,
            function_associations=quick_header_strip_associations(
                create_quick_header_strip(stack)),
        ),
    )
    template = Template.from_stack(stack).to_json()
    (fn_lid, fn), = _resources(template, "AWS::CloudFront::Function").items()
    assert fn["Properties"]["FunctionCode"] == STRIP_FUNCTION_CODE
    assert fn["Properties"]["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    assert fn["Properties"]["AutoPublish"] is True
    config = _distribution_config(template)
    assert config["DefaultCacheBehavior"]["FunctionAssociations"] == [
        {"EventType": "viewer-request",
         "FunctionARN": {"Fn::GetAtt": [fn_lid, "FunctionARN"]}},
    ]


# ---------------------------------------------------------------------------
# Synthesized stacks
# ---------------------------------------------------------------------------

def _synth(tmp_path_factory, *, stacks, context, slug) -> dict[str, dict]:
    """Credential-blind synth of the given stacks; templates by stack name."""
    if not CDK_BIN.exists():
        pytest.skip("CDK CLI not found under node_modules/.bin (run `npm install`)")
    out_dir = tmp_path_factory.mktemp(slug)
    env = {
        **os.environ,
        "AWS_DEFAULT_REGION": "us-east-1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    env.pop("CDK_DEFAULT_ACCOUNT", None)
    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(key, None)
    env["AWS_CONFIG_FILE"] = os.devnull
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    args = [str(CDK_BIN), "synth", *stacks, "--exclusively", "--no-lookups"]
    for key, value in context.items():
        args.extend(["-c", f"{key}={value}"])
    args.extend(["--output", str(out_dir), "--quiet"])
    result = subprocess.run(  # nosec B603 - fixed argv list, no shell
        args, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=900,
    )
    templates = {}
    for stack in stacks:
        path = out_dir / f"{stack}.template.json"
        if not path.exists():
            pytest.skip(f"synth did not produce {stack}:\n{result.stderr[-2000:]}")
        templates[stack] = json.loads(path.read_text())
    return templates


@pytest.fixture(scope="module")
def edge_off(tmp_path_factory) -> dict[str, dict]:
    return _synth(
        tmp_path_factory,
        stacks=(COMPUTE,),
        context={"region": "us-east-1", "edge_auth": "false", "cdk_nag_disabled": "true"},
        slug="cdk_synth_quick_strip_edge_off",
    )


@pytest.fixture(scope="module")
def edge_on(tmp_path_factory) -> dict[str, dict]:
    return _synth(
        tmp_path_factory,
        stacks=(COMPUTE, ERP, HIGHBYTE),
        context={
            "region": "us-east-1",
            "with_simulator": "true",
            "highbyte_enabled": "true",
            "highbyte_accept_eula": "true",
            "edge_auth": "true",
            "cdk_nag_disabled": "true",
        },
        slug="cdk_synth_quick_strip_edge_on",
    )


def _resources(template: dict, cfn_type: str) -> dict:
    return {lid: r for lid, r in template.get("Resources", {}).items()
            if r.get("Type") == cfn_type}


def _distribution_config(template: dict) -> dict:
    (dist,) = _resources(template, "AWS::CloudFront::Distribution").values()
    return dist["Properties"]["DistributionConfig"]


def _strip_function(template: dict) -> str:
    """Logical id of the ONE strip function; asserts its code and runtime."""
    functions = _resources(template, "AWS::CloudFront::Function")
    assert len(functions) == 1, sorted(functions)
    (lid, fn), = functions.items()
    assert fn["Properties"]["FunctionCode"] == STRIP_FUNCTION_CODE
    assert fn["Properties"]["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    assert fn["Properties"]["AutoPublish"] is True
    return lid


def _strip_association(lid: str) -> list:
    return [{"EventType": "viewer-request",
             "FunctionARN": {"Fn::GetAtt": [lid, "FunctionARN"]}}]


def test_edge_off_strips_on_the_default_behavior(edge_off):
    template = edge_off[COMPUTE]
    lid = _strip_function(template)
    config = _distribution_config(template)
    default = config["DefaultCacheBehavior"]
    assert default["FunctionAssociations"] == _strip_association(lid)
    assert "LambdaFunctionAssociations" not in default
    # The default behavior serves every path: no other behavior exists that
    # could carry /api/* past the function.
    assert config.get("CacheBehaviors", []) == []


def test_edge_on_strips_on_the_api_behavior_only(edge_on):
    template = edge_on[COMPUTE]
    lid = _strip_function(template)
    config = _distribution_config(template)
    default = config["DefaultCacheBehavior"]
    assert "FunctionAssociations" not in default
    assert {a["EventType"] for a in default["LambdaFunctionAssociations"]} == {
        "viewer-request", "viewer-response"}
    behaviors = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    api = behaviors.pop("/api/*")
    assert api["FunctionAssociations"] == _strip_association(lid)
    assert "LambdaFunctionAssociations" not in api
    assert set(behaviors) == {
        "/health", "/js/vendor/*", "/parseauth", "/refreshauth", "/signout"}
    for pattern, behavior in behaviors.items():
        assert "FunctionAssociations" not in behavior, pattern


def test_other_distributions_are_untouched(edge_on):
    """Only the ADT app distribution strips the Quick secret."""
    for stack in (ERP, HIGHBYTE):
        assert QUICK_GATEWAY_HEADER not in json.dumps(edge_on[stack]), stack
    assert _resources(edge_on[ERP], "AWS::CloudFront::Function") == {}
    # HighByte keeps exactly its own root-redirect function.
    (fn,) = _resources(edge_on[HIGHBYTE], "AWS::CloudFront::Function").values()
    assert "/ui/" in fn["Properties"]["FunctionCode"]
