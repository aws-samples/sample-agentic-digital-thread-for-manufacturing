# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The hub's MCP Configuration Tools stay OFF on HighByte 4.5 and later.

HighByte 4.5.0 replaced the 4.4 ``config_pipeline_*`` MCP tools with
``config_connection``, ``config_connection_io``, ``config_model``,
``config_instance`` and ``config_pipeline``, each taking
``action: create|update``. With ``mcpConfigTools`` enabled, a 4.5 hub lists
them to every MCP client holding the key, and ADT's system agent
(``_mcp_system_tools``) hands every tool the hub lists to the model. So on
4.5+ an enabled setting gives the assistant write access to the hub's
connections, inputs, outputs, models, instances and pipelines (measured on a
local 4.5.3 hub, 2026-10-11: 13 tools with the setting on, ``NameSpace_MCP``
only with it off).

The rule is keyed to the pinned hub VERSION: the published tag the digest was
read from, as the stack and its pin file each spell it (the stack's
``HIGHBYTE_IMAGE_SOURCE`` and ``stacks/_external_images.py``'s
``EXTERNAL_IMAGES["highbyte-hub"]``). A pin this test cannot read as
``<major>.<minor>.<patch>`` fails rather than skipping the rule.

Offline: no AWS, no docker, no synth.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS = (REPO_ROOT / "highbyte" / "components" / "highbyte-settings"
            / "intelligencehub-settings.json")
#: The first HighByte release whose MCP config tools edit more than pipelines.
WRITE_TOOLS_SINCE = (4, 5)


def _hub_pins() -> dict[str, str]:
    from stacks._external_images import EXTERNAL_IMAGES
    from stacks.highbyte_opcua_stack import HIGHBYTE_IMAGE_SOURCE
    return {
        "stacks.highbyte_opcua_stack.HIGHBYTE_IMAGE_SOURCE": HIGHBYTE_IMAGE_SOURCE,
        "stacks._external_images.EXTERNAL_IMAGES[highbyte-hub]":
            EXTERNAL_IMAGES["highbyte-hub"]["source"],
    }


def _version(image_ref: str) -> tuple[int, int, int]:
    """(major, minor, patch) of a ``highbyte/intelligence-hub:<x.y.z>`` ref."""
    match = re.fullmatch(r"(?:[^@\s]+/)?highbyte/intelligence-hub:(\d+)\.(\d+)\.(\d+)", image_ref)
    if match is None:
        raise ValueError(f"not a highbyte/intelligence-hub:<x.y.z> ref: {image_ref!r}")
    return tuple(int(part) for part in match.groups())


def _config_tools_setting() -> str:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return settings["settings"]["restDataServer"]["mcpConfigTools"]


@pytest.mark.parametrize("ref, version", [
    ("highbyte/intelligence-hub:4.4.2", (4, 4, 2)),
    ("highbyte/intelligence-hub:4.5.3", (4, 5, 3)),
    ("docker.io/highbyte/intelligence-hub:10.0.1", (10, 0, 1)),
])
def test_the_version_is_read_from_the_tag(ref, version):
    assert _version(ref) == version


@pytest.mark.parametrize("ref", [
    "highbyte/intelligence-hub:latest",
    "highbyte/intelligence-hub:4.5",
    "highbyte/intelligence-hub@sha256:" + "a" * 64,
    "other/image:4.5.3",
])
def test_an_unreadable_pin_is_an_error_not_a_skip(ref):
    with pytest.raises(ValueError, match="not a highbyte/intelligence-hub"):
        _version(ref)


def test_both_hub_pins_name_the_same_release():
    """The demo and the workshop run one hub release, so one rule covers both."""
    assert len({_version(ref) for ref in _hub_pins().values()}) == 1, _hub_pins()


def test_config_tools_are_off_when_the_pinned_hub_is_4_5_or_later():
    for where, ref in _hub_pins().items():
        if _version(ref)[:2] >= WRITE_TOOLS_SINCE:
            assert _config_tools_setting() == "disabled", (
                f"{where} pins {ref}: from HighByte 4.5 on, mcpConfigTools 'enabled' lists "
                "config_connection/config_model/config_instance/config_pipeline (create and "
                "update) to the ADT assistant; ship highbyte-settings with it 'disabled'")
