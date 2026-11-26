# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for ``stacks/_workshop_story.py`` (the synth-time story key).

The helper is pure: a duck-typed node with ``try_get_context`` is enough, and
importing it must not import aws_cdk.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from stacks import _workshop_story as ws  # noqa: E402


class _Node:
    def __init__(self, **ctx):
        self._ctx = ctx

    def try_get_context(self, key):
        return self._ctx.get(key)


def test_contract_constants():
    assert ws.CONTEXT_KEY == "workshop_story"
    assert ws.STORIES == ("frozen", "live")
    assert ws.DEFAULT_STORY == "frozen"


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_workshop_default_is_frozen(raw):
    assert ws.resolve(True, raw) == "frozen"


@pytest.mark.parametrize("raw,expected", [("frozen", "frozen"), ("live", "live"), (" LIVE ", "live")])
def test_workshop_accepts_both_stories(raw, expected):
    assert ws.resolve(True, raw) == expected


@pytest.mark.parametrize("raw", [None, "", "frozen", "Frozen"])
def test_non_workshop_resolves_to_none(raw):
    assert ws.resolve(False, raw) is None


def test_live_without_workshop_is_an_error():
    with pytest.raises(ValueError, match="with_workshop"):
        ws.resolve(False, "live")


@pytest.mark.parametrize("with_workshop", [True, False])
def test_unknown_story_is_an_error(with_workshop):
    with pytest.raises(ValueError, match="bogus"):
        ws.resolve(with_workshop, "bogus")


@pytest.mark.parametrize(
    "ctx,expected",
    [
        ({}, None),
        ({"with_workshop": "true"}, "frozen"),
        ({"with_workshop": "True", "workshop_story": "live"}, "live"),
        ({"with_workshop": True, "workshop_story": "frozen"}, "frozen"),
        ({"with_workshop": "false", "workshop_story": "frozen"}, None),
    ],
)
def test_from_node_reads_both_keys(ctx, expected):
    assert ws.from_node(_Node(**ctx)) == expected


def test_from_node_raises_for_live_without_workshop():
    with pytest.raises(ValueError):
        ws.from_node(_Node(workshop_story="live"))


# --- packaged story anchor (D14) -------------------------------------------

#: 2026-10-14T08:00:00-05:00 == 2026-10-14T13:00:00Z.
_ANCHOR_EPOCH = 1791982800


def test_anchor_context_key():
    assert ws.ANCHOR_CONTEXT_KEY == "story_anchor"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_resolve_anchor_unset_is_none(value):
    assert ws.resolve_anchor(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-14T08:00:00-05:00",
        "2026-10-14T13:00:00Z",
        "2026-10-14T13:00:00+00:00",
        " 2026-10-14T15:00:00+02:00 ",
        # The epoch form is what package.py / deploy.sh forward as context.
        "1791982800",
        1791982800,
    ],
)
def test_resolve_anchor_accepts_tz_aware_iso_and_epoch(value):
    assert ws.resolve_anchor(value) == _ANCHOR_EPOCH


def test_resolve_anchor_returns_a_plain_int():
    assert type(ws.resolve_anchor("2026-10-14T13:00:00Z")) is int


@pytest.mark.parametrize("value", ["2026-10-14T08:00:00", "2026-10-14"])
def test_resolve_anchor_rejects_naive_iso(value):
    with pytest.raises(ValueError, match="timezone"):
        ws.resolve_anchor(value)


@pytest.mark.parametrize("value", ["tomorrow", "2026-13-01T00:00:00Z", "-5", "0", "1.5", True, 3.0])
def test_resolve_anchor_rejects_garbage(value):
    with pytest.raises(ValueError, match="story_anchor"):
        ws.resolve_anchor(value)


@pytest.mark.parametrize(
    "ctx,expected",
    [
        ({}, None),
        ({"with_workshop": "true", "workshop_story": "live"}, None),
        ({"with_workshop": "true", "workshop_story": "live", "story_anchor": ""}, None),
        ({"with_workshop": "true", "workshop_story": "live", "story_anchor": "1791982800"}, _ANCHOR_EPOCH),
        (
            {"with_workshop": "true", "workshop_story": "live",
             "story_anchor": "2026-10-14T08:00:00-05:00"},
            _ANCHOR_EPOCH,
        ),
    ],
)
def test_anchor_from_node(ctx, expected):
    assert ws.anchor_from_node(_Node(**ctx)) == expected


@pytest.mark.parametrize(
    "ctx",
    [
        {"story_anchor": "1791982800"},
        {"with_workshop": "true", "story_anchor": "1791982800"},
        {"with_workshop": "true", "workshop_story": "frozen", "story_anchor": "1791982800"},
    ],
)
def test_anchor_from_node_requires_the_live_story(ctx):
    with pytest.raises(ValueError, match="live"):
        ws.anchor_from_node(_Node(**ctx))


def test_anchor_from_node_rejects_a_naive_iso():
    ctx = {"with_workshop": "true", "workshop_story": "live", "story_anchor": "2026-10-14T08:00:00"}
    with pytest.raises(ValueError, match="timezone"):
        ws.anchor_from_node(_Node(**ctx))


# --- packaged story loop length ---------------------------------------------

_LIVE = {"with_workshop": "true", "workshop_story": "live"}


def test_loop_contract_constants():
    assert ws.LOOP_CONTEXT_KEY == "story_loop_seconds"
    assert ws.LOOP_ENV_NAME == "STORY_LOOP_SECONDS"
    assert ws.DEFAULT_LOOP_SECONDS == 28800
    assert ws.LOOP_GRID_SECONDS == 300
    assert ws.LOOP_RANGE_SECONDS == (3600, 28800)   # capped: no failed-batch / line-3 overlap


@pytest.mark.parametrize("value", [None, "", "  ", 28800, "28800"])
def test_resolve_loop_unset_or_default_is_none(value):
    """Unset and the 8 h default both normalise to None: byte-identical templates."""
    assert ws.resolve_loop_seconds(value) is None


@pytest.mark.parametrize("value,expected", [
    (3600, 3600), ("10800", 10800), (" 10800 ", 10800), (21600, 21600), ("28500", 28500),
])
def test_resolve_loop_accepts_grid_multiples_in_range(value, expected):
    result = ws.resolve_loop_seconds(value)
    assert result == expected and type(result) is int


@pytest.mark.parametrize("value", [
    3300, 28900, 29100, "36000", 43200, 86400, 86700, 0, -3600, "10740", 7000, "abc", "1e4", "10800.0",
    10800.0, True, [10800],
])
def test_resolve_loop_rejects_off_grid_out_of_range_and_garbage(value):
    with pytest.raises(ValueError, match="story_loop_seconds"):
        ws.resolve_loop_seconds(value)


@pytest.mark.parametrize("ctx,expected", [
    ({}, None),
    (_LIVE, None),
    ({**_LIVE, "story_loop_seconds": ""}, None),
    ({**_LIVE, "story_loop_seconds": "28800"}, None),
    ({**_LIVE, "story_loop_seconds": "10800"}, 10800),
])
def test_loop_from_node(ctx, expected):
    assert ws.loop_from_node(_Node(**ctx)) == expected


@pytest.mark.parametrize("ctx", [
    {"story_loop_seconds": "10800"},
    {"with_workshop": "true", "story_loop_seconds": "10800"},
    {"with_workshop": "true", "workshop_story": "frozen", "story_loop_seconds": "10800"},
    # Even the default value is refused off live: set means "I asked for a loop".
    {"with_workshop": "true", "workshop_story": "frozen", "story_loop_seconds": "28800"},
])
def test_loop_from_node_requires_the_live_story(ctx):
    with pytest.raises(ValueError, match="story_loop_seconds requires workshop_story=live"):
        ws.loop_from_node(_Node(**ctx))


def test_loop_validator_parity_with_condition_monitor():
    """ONE rule set: the synth validator and the env reader in
    erp_simulation.condition_monitor accept exactly the same values.
    Skips if ``condition_monitor.loop_seconds_from`` is missing."""
    cm = pytest.importorskip("erp_simulation.condition_monitor")
    reader = getattr(cm, "loop_seconds_from", None)
    if reader is None:
        pytest.skip("condition_monitor.loop_seconds_from is missing")
    for value in ("", "3600", "3300", "10800", "10740", "28500", "28800", "29100", "36000", "86400", "86700", "abc"):
        try:
            synth = ws.resolve_loop_seconds(value)
        except ValueError:
            synth = "rejected"
        try:
            env = reader(value)
        except ValueError:
            env = "rejected"
        expected = ws.DEFAULT_LOOP_SECONDS if synth is None else synth
        assert env == expected, (value, synth, env)


# --- auto-place policy (ADR-0091 Design C) ----------------------------------

def test_auto_place_contract_constants():
    assert ws.AUTO_PLACE_CONTEXT_KEY == "story_auto_place_minutes"
    assert ws.AUTO_PLACE_ENV_NAME == "STORY_AUTO_PLACE_MINUTES"
    assert ws.AUTO_PLACE_MIN_MINUTES == 5
    assert ws.DEFAULT_AUTO_PLACE_MINUTES == 80


@pytest.mark.parametrize("value", [None, "", "  ", 0, "0"])
def test_resolve_auto_place_unset_or_zero_is_disabled(value):
    """Unset and 0 both mean disabled: None, so the template stays byte-identical."""
    assert ws.resolve_auto_place_minutes(value) is None


@pytest.mark.parametrize("value,loop,expected", [
    (80, None, 80), ("80", None, 80), (" 5 ", None, 5), ("480", None, 480),
    ("180", 10800, 180), ("60", 3600, 60),
])
def test_resolve_auto_place_accepts_minutes_up_to_one_loop(value, loop, expected):
    result = ws.resolve_auto_place_minutes(value, loop)
    assert result == expected and type(result) is int


@pytest.mark.parametrize("value,loop", [
    (4, None), ("481", None), ("181", 10800), ("61", 3600), ("-5", None), ("abc", None), ("80.0", None),
    (80.0, None), (True, None), ([80], None), ("1e2", None),
])
def test_resolve_auto_place_rejects_out_of_range_and_garbage(value, loop):
    with pytest.raises(ValueError, match="story_auto_place_minutes"):
        ws.resolve_auto_place_minutes(value, loop)


@pytest.mark.parametrize("ctx,expected", [
    ({}, None),
    (_LIVE, None),
    ({**_LIVE, "story_auto_place_minutes": ""}, None),
    ({**_LIVE, "story_auto_place_minutes": "0"}, None),
    ({**_LIVE, "story_auto_place_minutes": "80"}, 80),
    ({**_LIVE, "story_loop_seconds": "10800", "story_auto_place_minutes": "180"}, 180),
])
def test_auto_place_from_node(ctx, expected):
    assert ws.auto_place_from_node(_Node(**ctx)) == expected


def test_auto_place_from_node_bounds_by_the_packaged_loop():
    with pytest.raises(ValueError, match="story_auto_place_minutes"):
        ws.auto_place_from_node(_Node(**_LIVE, story_loop_seconds="10800", story_auto_place_minutes="181"))


@pytest.mark.parametrize("ctx", [
    {"story_auto_place_minutes": "80"},
    {"with_workshop": "true", "story_auto_place_minutes": "80"},
    {"with_workshop": "true", "workshop_story": "frozen", "story_auto_place_minutes": "0"},
])
def test_auto_place_from_node_requires_the_live_story(ctx):
    with pytest.raises(ValueError, match="story_auto_place_minutes requires workshop_story=live"):
        ws.auto_place_from_node(_Node(**ctx))


def test_module_does_not_import_cdk():
    src = (_REPO_ROOT / "stacks" / "_workshop_story.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert not mods & {"aws_cdk", "constructs", "boto3"}, mods
