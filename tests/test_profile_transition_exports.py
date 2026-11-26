# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A profile change never drops an export the deployed stacks still import.

CloudFormation refuses to delete or change an export another stack imports.
``cdk deploy --all`` updates an exporter before the stacks that depend on it,
and never deletes a stack that has left the app, so on a move from profile A
to profile B every export a stack carries in A, and that some stack imports
in A, must still be there, unchanged, when the same stack is synthesized for
B. Otherwise the exporter's update fails ("Export ... cannot be deleted as it
is in use by ...") and the deploy stops there.

The profiles are the data modes deploy.sh switches between in place (base,
``--seed``, ``--simulator``, ``--workshop``), each with the hub off and with
the hub and SFC on. HighByte, the OPC UA servers and SFC are sticky (app.py),
so a transition never turns them off; each group below keeps them fixed.
"""
from __future__ import annotations

import itertools
import json

import pytest

from _app_synth import synth_app

_HUB = {"highbyte_enabled": "true", "highbyte_accept_eula": "true", "sfc_enabled": "true"}
_LIVE = {"with_workshop": "true", "workshop_story": "live"}

GROUPS = {
    "no hub": {
        "base": {},
        "seed": {"with_seed": "true"},
        "simulator": {"with_simulator": "true"},
    },
    "hub and SFC": {
        "base": dict(_HUB),
        "seed": {"with_seed": "true", **_HUB},
        "workshop live": {**_LIVE, **_HUB},
    },
}


def _exports(template: dict) -> dict[str, str]:
    """Export name -> its value (as JSON, to compare)."""
    out = {}
    for output in template.get("Outputs", {}).values():
        name = output.get("Export", {}).get("Name")
        if isinstance(name, str):
            out[name] = json.dumps(output.get("Value"), sort_keys=True)
    return out


def _imports(node, found: set[str]) -> set[str]:
    if isinstance(node, dict):
        if set(node) == {"Fn::ImportValue"} and isinstance(node["Fn::ImportValue"], str):
            found.add(node["Fn::ImportValue"])
        for value in node.values():
            _imports(value, found)
    elif isinstance(node, list):
        for value in node:
            _imports(value, found)
    return found


def _lost_exports(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
    """Exports in use in ``before`` that a stack of both synths drops or changes."""
    in_use = set()
    for template in before.values():
        _imports(template.get("Resources", {}), in_use)
        _imports(template.get("Outputs", {}), in_use)
    lost = []
    for stack in sorted(set(before) & set(after)):
        kept = _exports(after[stack])
        for name, value in sorted(_exports(before[stack]).items()):
            if name in in_use and kept.get(name) != value:
                lost.append(f"{stack}: {name} ({'changed' if name in kept else 'removed'})")
    return lost


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_no_profile_change_drops_an_export_in_use(group, tmp_path_factory):
    synths = {name: synth_app(tmp_path_factory.mktemp("profile"), context)
              for name, context in GROUPS[group].items()}
    problems = {f"{a} -> {b}": lost for a, b in itertools.permutations(synths, 2)
                if (lost := _lost_exports(synths[a], synths[b]))}
    assert problems == {}, json.dumps(problems, indent=1)
