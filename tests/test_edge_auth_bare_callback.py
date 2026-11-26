# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bare Authorization@Edge endpoints redirect into sign-in instead of a 200.

The SAR handlers behind ``/parseauth``, ``/refreshauth`` and ``/signout``
(cloudfront-authorization-at-edge v2.3.2) answer a request that is not part of
a real sign-in flow with an HTTP 200 HTML error page ("Invalid query
string...", "You are already signed out"). A security scanner reads that as
an unauthenticated web page. The fix (ADR-0070 amendment) is a WAF rule in the
distribution's web ACL that answers the BARE form of those requests (no query
string and no cookies) with a 302 to ``/``, where CheckAuth starts the normal
sign-in chain. Every real flow carries one or the other: Cognito's callback
brings ``code``/``state`` (or ``error``), CheckAuth sends ``/refreshauth``
``requestedUri`` and ``nonce``, and sign-out brings the session cookies. So
the rule does not try to re-implement the SAR's checks; anything that is not
bare goes on to the unchanged SAR handler.

Pinned here:

* the rule's logic over a request matrix, with a small model of the three
  WAF statements it uses (UriPath byte match, QueryString and single-header
  ``cookie`` size constraints; no AWS calls);
* the action is Block with a 302 custom response, ``Location: /`` and the
  same security headers the SAR HttpHeaders function sets;
* the rule runs AFTER the three managed rule groups (priority 4);
* all three edge-auth distributions carry it, the managed rules are
  unchanged, and the auth-path behaviors keep the SAR handler on viewer
  request;
* edge auth OFF: the app web ACL is exactly the three managed rules.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import unquote

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks._edge_auth import (
    BARE_AUTH_REDIRECT_RULE_NAME,
    EDGE_AUTH_PATHS,
    create_standard_web_acl,
    edge_http_headers,
)

_HERE = Path(__file__).resolve().parent
COMPUTE = "ADTComputeStack"
ERP = "ADTERPDashboardStack"
HIGHBYTE = "ADTHighByteOpcuaStack"
MANAGED = [
    "AWSManagedRulesCommonRuleSet",
    "AWSManagedRulesKnownBadInputsRuleSet",
    "AWSManagedRulesAmazonIpReputationList",
]
ID_TOKEN = "CognitoIdentityServiceProvider.abc.user%40example.com.idToken=jwt"
NONCE = "spa-auth-edge-nonce=n; spa-auth-edge-nonce-hmac=h; spa-auth-edge-pkce=p"
#: AWS WAF CustomHTTPHeader.Value maximum length (API reference).
WAF_HEADER_VALUE_MAX = 255


# ---------------------------------------------------------------------------
# A small model of the WAF statements the rule uses
# ---------------------------------------------------------------------------

def _transform(value: str, transformations: list) -> str:
    for t in sorted(transformations, key=lambda t: t["Priority"]):
        if t["Type"] == "URL_DECODE":
            value = unquote(value)
        elif t["Type"] != "NONE":
            raise AssertionError(f"model does not know transformation {t['Type']}")
    return value


def _field_value(field: dict, request: dict):
    """The component's value, or None when the request does not have it."""
    if "UriPath" in field:
        return request["path"]
    if "QueryString" in field:
        return request.get("query")
    if "SingleHeader" in field:
        assert field["SingleHeader"] == {"Name": "cookie"}, field
        return request.get("cookie")
    raise AssertionError(f"model does not know field {field}")


def _evaluate(statement: dict, request: dict) -> bool:
    if "OrStatement" in statement:
        return any(_evaluate(s, request) for s in statement["OrStatement"]["Statements"])
    if "AndStatement" in statement:
        return all(_evaluate(s, request) for s in statement["AndStatement"]["Statements"])
    if "NotStatement" in statement:
        return not _evaluate(statement["NotStatement"]["Statement"], request)
    if "RegexMatchStatement" in statement:
        s = statement["RegexMatchStatement"]
        value = _field_value(s["FieldToMatch"], request)
        return value is not None and re.search(
            s["RegexString"], _transform(value, s["TextTransformations"])) is not None
    if "ByteMatchStatement" in statement:
        s = statement["ByteMatchStatement"]
        assert s["PositionalConstraint"] == "EXACTLY", s
        value = _field_value(s["FieldToMatch"], request)
        return value is not None and _transform(value, s["TextTransformations"]) == s["SearchString"]
    if "SizeConstraintStatement" in statement:
        s = statement["SizeConstraintStatement"]
        assert s["ComparisonOperator"] == "GT" and s["Size"] == 0, s
        value = _field_value(s["FieldToMatch"], request)
        # A missing component does not match (WAF default for absent parts).
        return value is not None and len(_transform(value, s["TextTransformations"])) > 0
    raise AssertionError(f"model does not know statement {sorted(statement)}")


def _acl_rules(headers: str | None) -> list[dict]:
    stack = cdk.Stack(cdk.App(), "BareCallbackProbe")
    create_standard_web_acl(
        stack, construct_id="Acl", metric_name="Acl",
        bare_callback_redirect_headers=headers,
    )
    (acl,) = Template.from_stack(stack).find_resources("AWS::WAFv2::WebACL").values()
    return acl["Properties"]["Rules"]


@pytest.fixture(scope="module")
def rule() -> dict:
    rules = _acl_rules(edge_http_headers(allow_websockets=True))
    (match,) = [r for r in rules if r["Name"] == BARE_AUTH_REDIRECT_RULE_NAME]
    return match


def _redirects(rule: dict, path: str, *, query=None, cookie=None) -> bool:
    return _evaluate(rule["Statement"], {"path": path, "query": query, "cookie": cookie})


# ---------------------------------------------------------------------------
# Rule logic
# ---------------------------------------------------------------------------

def test_auth_paths_are_the_sar_defaults():
    assert EDGE_AUTH_PATHS == ("/parseauth", "/refreshauth", "/signout")


@pytest.mark.parametrize("path", ["/parseauth", "/refreshauth", "/signout"])
@pytest.mark.parametrize("query,cookie", [(None, None), ("", None), (None, ""), ("", "")])
def test_bare_auth_paths_redirect(rule, path, query, cookie):
    """What the scanner sends: no (or an empty) query string and Cookie header."""
    assert _redirects(rule, path, query=query, cookie=cookie)


@pytest.mark.parametrize("path,query,cookie", [
    # Cognito callback: code + state, with CheckAuth's nonce/pkce cookies.
    ("/parseauth", "code=c&state=s", NONCE),
    # IdP error callback: must reach SAR, or "/" would loop through the IdP.
    ("/parseauth", "error=access_denied&state=s", NONCE),
    # Callback whose cookies were lost: SAR shows its CSRF/confirm page.
    ("/parseauth", "code=c&state=s", None),
    # CheckAuth's refresh redirect: requestedUri + nonce, with cookies.
    ("/refreshauth", "requestedUri=%2F&nonce=n", f"{ID_TOKEN}; {NONCE}"),
    # Real sign-out from the app: the session cookies come along.
    ("/signout", None, ID_TOKEN),
    # Any cookie at all goes to SAR; the rule does not judge cookie names.
    ("/signout", None, "unrelated=x"),
])
def test_requests_that_are_not_bare_reach_the_sar_handler(rule, path, query, cookie):
    assert not _redirects(rule, path, query=query, cookie=cookie)


def test_encoded_path_spelling_redirects(rule):
    assert _redirects(rule, "/parse%61uth")


#: All 12 spellings seen reaching the SAR auth behaviors that an exact-path
#: match missed: each returned the SAR's 200 page anonymously on a live
#: distribution, 2026-11-16.
CLOUDFRONT_ROUTED_SPELLINGS = [
    "//signout", "///signout", "////parseauth", "//refreshauth",
    "/%2Fsignout", "/%2Fparseauth", "/%2fparseauth", "//%2Fsignout",
    "//./signout", "/%2e/signout", "/.%2fsignout", "//%2e/parseauth",
]


@pytest.mark.parametrize("path", CLOUDFRONT_ROUTED_SPELLINGS)
def test_cloudfront_routed_spellings_redirect(rule, path):
    assert _redirects(rule, path)


@pytest.mark.parametrize("path", ["//signout", "/%2Fparseauth", "//./refreshauth"])
def test_cloudfront_routed_spellings_with_cookies_still_reach_the_sar_handler(rule, path):
    assert not _redirects(rule, path, cookie=ID_TOKEN)


@pytest.mark.parametrize("path", [
    "/", "/index.html", "/api/health", "/parseauth/", "/PARSEAUTH",
    "/signout/x", "/login.html", "/ui/",
    "/x/signout", "/api/signout", "/signoutx", "/xsignout", "//", "/./",
])
def test_other_paths_are_untouched(rule, path):
    assert not _redirects(rule, path)


def test_cookie_statement_inspects_the_single_header(rule):
    """WAF inspects a single header in full, with no 8 KB / 200-header
    window, so a large Cookie header is never mistaken for an absent one."""
    statement = json.dumps(rule["Statement"])
    assert statement.count('"SingleHeader"') == 1
    assert '"Cookies"' not in statement and '"Headers"' not in statement
    assert "OversizeHandling" not in statement
    big_cookie = "; ".join(f"c{i}={'x' * 100}" for i in range(300))
    assert not _redirects(rule, "/signout", cookie=big_cookie)


def test_action_is_a_302_to_root_with_the_sar_security_headers(rule):
    response = rule["Action"]["Block"]["CustomResponse"]
    assert response["ResponseCode"] == 302
    headers = {h["Name"]: h["Value"] for h in response["ResponseHeaders"]}
    expected = {**json.loads(edge_http_headers(allow_websockets=True)),
                "X-Frame-Options": "DENY", "Location": "/"}
    assert headers == expected
    assert "Content-Type" not in headers  # WAF rejects a custom content-type
    for name, value in headers.items():
        assert len(value) <= WAF_HEADER_VALUE_MAX, name
    assert rule["VisibilityConfig"]["MetricName"] == BARE_AUTH_REDIRECT_RULE_NAME


def test_rule_runs_after_the_managed_groups():
    rules = _acl_rules(edge_http_headers(allow_websockets=False))
    assert [(r["Name"], r["Priority"]) for r in rules] == [
        (MANAGED[0], 1), (MANAGED[1], 2), (MANAGED[2], 3),
        (BARE_AUTH_REDIRECT_RULE_NAME, 4),
    ]


def test_without_headers_the_acl_is_the_three_managed_rules():
    rules = _acl_rules(None)
    assert [r["Name"] for r in rules] == MANAGED
    assert [r["Priority"] for r in rules] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Synthesized stacks
# ---------------------------------------------------------------------------

def _load_synth():
    spec = importlib.util.spec_from_file_location(
        "_quick_header_strip_tests", _HERE / "test_quick_header_strip.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._synth


@pytest.fixture(scope="module")
def edge_on(tmp_path_factory) -> dict[str, dict]:
    return _load_synth()(
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
        slug="cdk_synth_bare_callback_edge_on",
    )


@pytest.fixture(scope="module")
def edge_off(tmp_path_factory) -> dict[str, dict]:
    return _load_synth()(
        tmp_path_factory,
        stacks=(COMPUTE,),
        context={"region": "us-east-1", "edge_auth": "false", "cdk_nag_disabled": "true"},
        slug="cdk_synth_bare_callback_edge_off",
    )


def _resources(template: dict, cfn_type: str) -> list[dict]:
    return [r for r in template["Resources"].values() if r["Type"] == cfn_type]


def _web_acl_rules(template: dict) -> list[dict]:
    (acl,) = _resources(template, "AWS::WAFv2::WebACL")
    return acl["Properties"]["Rules"]


def _distribution_config(template: dict) -> dict:
    (dist,) = _resources(template, "AWS::CloudFront::Distribution")
    return dist["Properties"]["DistributionConfig"]


def _sar_http_headers(template: dict) -> dict:
    (app,) = _resources(template, "AWS::Serverless::Application")
    return json.loads(app["Properties"]["Parameters"]["HttpHeaders"])


@pytest.mark.parametrize("stack", [COMPUTE, ERP, HIGHBYTE])
def test_edge_auth_distributions_redirect_bare_callbacks(edge_on, stack, rule):
    rules = _web_acl_rules(edge_on[stack])
    assert [(r["Name"], r["Priority"]) for r in rules] == [
        (MANAGED[0], 1), (MANAGED[1], 2), (MANAGED[2], 3),
        (BARE_AUTH_REDIRECT_RULE_NAME, 4),
    ]
    redirect = rules[3]
    assert redirect["Statement"] == rule["Statement"]
    headers = {h["Name"]: h["Value"]
               for h in redirect["Action"]["Block"]["CustomResponse"]["ResponseHeaders"]}
    # The 302 carries the same security headers this stack's SAR sets.
    assert headers == {**_sar_http_headers(edge_on[stack]),
                       "X-Frame-Options": "DENY", "Location": "/"}


@pytest.mark.parametrize("stack", [COMPUTE, ERP, HIGHBYTE])
def test_auth_path_behaviors_keep_sar_on_viewer_request(edge_on, stack):
    """The fix lives in WAF: the SAR associations are not moved or replaced."""
    config = _distribution_config(edge_on[stack])
    behaviors = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    for path in EDGE_AUTH_PATHS:
        behavior = behaviors[path]
        (assoc,) = behavior["LambdaFunctionAssociations"]
        assert assoc["EventType"] == "viewer-request", (stack, path)
        assert "FunctionAssociations" not in behavior, (stack, path)
    default_events = {a["EventType"] for a in config["DefaultCacheBehavior"]["LambdaFunctionAssociations"]}
    assert default_events == {"viewer-request", "viewer-response"}


def test_edge_off_app_acl_has_no_redirect_rule(edge_off):
    rules = _web_acl_rules(edge_off[COMPUTE])
    assert [r["Name"] for r in rules] == MANAGED


# ---------------------------------------------------------------------------
# HighByte: the REST Data Server's Swagger page needs sign-in
# ---------------------------------------------------------------------------

DOC_PATHS = ("/data/doc", "/data/doc/*")


def test_highbyte_swagger_page_is_behind_edge_auth(edge_on):
    """``/data/doc`` is an HTML Swagger UI the origin serves without a token.
    It gets its own behaviors (same origin as ``/data/*``) carrying the same
    CheckAuth + HttpHeaders pair as the default; every other ``/data/*``
    request keeps bearer-token-only access."""
    config = _distribution_config(edge_on[HIGHBYTE])
    ordered = [b["PathPattern"] for b in config["CacheBehaviors"]]
    behaviors = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    data = behaviors["/data/*"]
    assert "LambdaFunctionAssociations" not in data
    default_assocs = config["DefaultCacheBehavior"]["LambdaFunctionAssociations"]
    for path in DOC_PATHS:
        doc = behaviors[path]
        # CloudFront uses the first matching pattern: doc must precede /data/*.
        assert ordered.index(path) < ordered.index("/data/*"), ordered
        assert doc["TargetOriginId"] == data["TargetOriginId"], path
        assert doc["LambdaFunctionAssociations"] == default_assocs, path
        assert doc["CachePolicyId"] == data["CachePolicyId"], path
        assert doc["ViewerProtocolPolicy"] == "redirect-to-https", path
