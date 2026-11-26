# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``agents/console_links.py`` — the pure helper that
builds per-agent CloudWatch console deep-links for the Agents page.

No AWS, no env, no I/O — pure functions over strings. Verifies:
  • the GenAI Observability URL is region-qualified with the exact fragment;
  • the runtime log-group URL is the double-URL-encoded (``$252F``) deep-link;
  • an empty/None log group still yields observability_url, with logs_url None;
  • the region argument is honored (and blank/None falls back to us-east-1).
"""
from __future__ import annotations

import agents.console_links as cl


def test_observability_url_region_qualified_exact_fragment():
    links = cl.agentcore_console_links("eu-central-1", "")
    assert links["observability_url"] == (
        "https://eu-central-1.console.aws.amazon.com/cloudwatch/home"
        "?region=eu-central-1#gen-ai-observability"
    )


def test_logs_url_double_encoded_log_group():
    lg = "/aws/bedrock-agentcore/runtimes/adt_orchestrator-BkJ8vc7IDF-DEFAULT"
    links = cl.agentcore_console_links("us-east-1", lg)
    enc = lg.replace("/", "$252F")
    assert links["logs_url"] == (
        "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
        f"?region=us-east-1#logsV2:log-groups/log-group/{enc}"
    )
    # every "/" is encoded, none survive raw in the fragment path
    frag = links["logs_url"].split("#", 1)[1]
    assert "$252F" in frag
    assert "/" not in frag.split("log-group/", 1)[1]


def test_empty_log_group_keeps_observability_drops_logs():
    for empty in ("", None):
        links = cl.agentcore_console_links("us-west-2", empty)
        assert links["observability_url"].endswith("#gen-ai-observability")
        assert "region=us-west-2" in links["observability_url"]
        assert links["logs_url"] is None


def test_region_honored_across_both_urls():
    links = cl.agentcore_console_links("ap-southeast-2", "/aws/foo/bar")
    assert links["observability_url"].startswith(
        "https://ap-southeast-2.console.aws.amazon.com/"
    )
    assert "region=ap-southeast-2" in links["logs_url"]
    assert links["logs_url"].endswith("#logsV2:log-groups/log-group/$252Faws$252Ffoo$252Fbar")


def test_blank_region_falls_back_to_us_east_1():
    for blank in ("", None, "   "):
        links = cl.agentcore_console_links(blank, "")
        assert "region=us-east-1" in links["observability_url"]


def test_logs_group_deeplink_none_for_empty():
    assert cl.logs_group_deeplink("us-east-1", "") is None
    assert cl.logs_group_deeplink("us-east-1", "/a/b").endswith("$252Fa$252Fb")
