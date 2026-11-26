# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The story site's OPC UA server follows the plant clock on a live synth only (ADR-0092).

``HighByteOpcuaStack._attach_story_clock`` is exercised in-process on a
minimal task definition: the full stack needs the network stack, and the
packaged templates are covered by the workshop packaging gate.
"""
from __future__ import annotations

from types import SimpleNamespace

import aws_cdk as cdk
import pytest
from aws_cdk import aws_ecs as ecs
from aws_cdk.assertions import Template

from stacks import _workshop_story
from stacks.highbyte_opcua_stack import HighByteOpcuaStack

LIVE = {"with_workshop": "true", "workshop_story": "live"}
SITES = [site for site, _construct in HighByteOpcuaStack.OPC_UA_SITES]


def _template(context: dict, site: str) -> dict:
    app = cdk.App(context=context)
    stack = cdk.Stack(app, "S", env=cdk.Environment(account="111122223333", region="us-east-1"))
    task_definition = ecs.FargateTaskDefinition(stack, "Td")
    task_definition.add_container("opcua", image=ecs.ContainerImage.from_registry("opc"),
                                  environment={"SITE_NAME": site})
    HighByteOpcuaStack._attach_story_clock(stack, site, SimpleNamespace(task_definition=task_definition))
    return Template.from_stack(stack).to_json()


def _env(template: dict) -> dict:
    (td,) = [r for r in template["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition"]
    (container,) = td["Properties"]["ContainerDefinitions"]
    return {e["Name"]: e["Value"] for e in container["Environment"]}


def _ssm_statements(template: dict) -> list:
    return [s for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Policy"
            for s in r["Properties"]["PolicyDocument"]["Statement"]
            if "ssm:GetParameter" in str(s["Action"])]


def test_the_story_site_is_a_bundled_server():
    assert _workshop_story.ANCHOR_STORY_SITE in SITES


def test_live_story_mill_polls_the_anchor_parameter_and_nothing_more():
    template = _template({**LIVE, "story_loop_seconds": "4800"}, _workshop_story.ANCHOR_STORY_SITE)
    assert _env(template) == {
        "SITE_NAME": _workshop_story.ANCHOR_STORY_SITE,
        "STORY_ANCHOR_PARAMETER": "/adt/workshop/story-anchor-epoch",
        "AWS_REGION": "us-east-1",
        "STORY_LOOP_SECONDS": "4800",
        # the one-time history replay's second parameter, default ON for live (the replay ADR)
        "STORY_FEED_BACKFILL_PARAMETER": "/adt/workshop/story-feed-backfill",
    }
    granted = {}
    for statement in _ssm_statements(template):
        resource = statement["Resource"]
        assert isinstance(resource, (str, dict)), "one exact resource per statement, never a list or wildcard"
        rendered = resource if isinstance(resource, str) else "".join(
            p if isinstance(p, str) else "${" + p["Ref"] + "}" for p in resource["Fn::Join"][1])
        assert "*" not in rendered, rendered
        assert rendered.rsplit(":parameter/", 1)[0].endswith(":ssm:us-east-1:111122223333"), rendered
        granted[rendered.rsplit(":parameter/", 1)[1]] = statement["Action"]
    # read the anchor; read the replay window and write its done mark, on that one parameter only
    assert granted == {"adt/workshop/story-anchor-epoch": "ssm:GetParameter",
                       "adt/workshop/story-feed-backfill": ["ssm:GetParameter", "ssm:PutParameter"]}


def test_backfill_off_leaves_only_the_anchor_parameter():
    """The switch off (story_feed_backfill=false) ships no second parameter —
    byte-identical to the pre-replay live story."""
    template = _template({**LIVE, "story_feed_backfill": "false"}, _workshop_story.ANCHOR_STORY_SITE)
    env = _env(template)
    assert "STORY_FEED_BACKFILL_PARAMETER" not in env
    assert env["STORY_ANCHOR_PARAMETER"] == "/adt/workshop/story-anchor-epoch"
    (statement,) = _ssm_statements(template)
    assert statement["Action"] == "ssm:GetParameter"


def test_the_default_loop_adds_no_loop_env():
    env = _env(_template(LIVE, _workshop_story.ANCHOR_STORY_SITE))
    assert "STORY_LOOP_SECONDS" not in env and env["STORY_ANCHOR_PARAMETER"]


@pytest.mark.parametrize("context, site", [
    *[(LIVE, site) for site in SITES if site != _workshop_story.ANCHOR_STORY_SITE],
    ({"with_workshop": "true", "workshop_story": "frozen"}, _workshop_story.ANCHOR_STORY_SITE),
    ({}, _workshop_story.ANCHOR_STORY_SITE),
])
def test_every_other_server_keeps_the_wall_clock_and_an_unchanged_template(context, site):
    template = _template(context, site)
    assert _env(template) == {"SITE_NAME": site}
    assert _ssm_statements(template) == []
