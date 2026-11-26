# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure helpers that build CloudWatch console deep-links for an AgentCore agent.

Kept dependency-free and PURE (no AWS, no env, no I/O) so the Agents-page link
row is unit-testable and the JSON contract stays additive. Two links per agent:

  * the CloudWatch **GenAI Observability** page (AgentCore tab), region-qualified;
  * the exact **runtime log-group** deep-link (double-URL-encoded log-group path).

Both formats are console-verified. The log-group path uses ``$252F`` for each
``/`` — the CloudWatch console fragment is itself URL-encoded, so a literal ``/``
in a log-group name becomes ``$252F`` (``$`` + the double-encoding of ``/``).
"""
from __future__ import annotations

_DEFAULT_REGION = "us-east-1"


def _cloudwatch_base(region: str) -> str:
    return f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"


def logs_group_deeplink(region: str, log_group: str) -> str | None:
    """Deep-link to a CloudWatch Logs log group, or ``None`` if no group.

    Each ``/`` in the log-group name is replaced with ``$252F`` (the console
    fragment's double-URL-encoding of a path separator)."""
    if not log_group:
        return None
    enc = log_group.replace("/", "$252F")
    return f"{_cloudwatch_base(region)}#logsV2:log-groups/log-group/{enc}"


def agentcore_console_links(region: str, log_group: str | None) -> dict:
    """Build the per-agent console deep-links.

    Returns a dict with ``observability_url`` (always) and ``logs_url`` (the exact
    runtime log-group deep-link, or ``None`` when ``log_group`` is missing/empty).
    Fail-soft: a blank/None region falls back to ``us-east-1`` so the link is
    still well-formed."""
    region = (region or _DEFAULT_REGION).strip() or _DEFAULT_REGION
    return {
        "observability_url": f"{_cloudwatch_base(region)}#gen-ai-observability",
        "logs_url": logs_group_deeplink(region, log_group or ""),
    }
