# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The workshop story as a SYNTH-TIME CDK context value.

``workshop_story`` picks which storyline a workshop deploy ships
(``frozen`` = the seeded storyline, ``live`` = the anchored simulator story).
It is read from CDK context at synth, never a CloudFormation parameter, so one
template can never pair live pages with frozen data.

Rules (:func:`resolve`):

* workshop deploy: empty/absent -> ``DEFAULT_STORY``; ``frozen``/``live`` as given;
* non-workshop deploy: empty/absent or ``frozen`` -> ``None`` (no story);
  ``live`` is an error (the live story needs the workshop profile);
* any other non-empty value is an error.

Packaged story anchor (``story_anchor``, :func:`resolve_anchor`): an optional
synth-time instant for the live story clock (``package.py --story-anchor`` /
``deploy.sh --story-anchor``). The context carries whole epoch seconds as a
decimal string; a tz-aware ISO 8601 string is also accepted. It is only valid
on the ``live`` story (:func:`anchor_from_node`). Snapping to the telemetry
grid is the consumer's job; this module only parses.

Packaged loop length (``story_loop_seconds``, :func:`resolve_loop_seconds`):
an optional synth-time loop length for the live story (``package.py
--story-loop-minutes``), forwarded as env ``STORY_LOOP_SECONDS``. Unset or the
8 h default -> ``None`` (byte-identical templates); live only
(:func:`loop_from_node`).

Auto-place policy (``story_auto_place_minutes``,
:func:`resolve_auto_place_minutes`, ADR-0091 Design C): an optional synth-time
"alarm in N minutes" the UI places on the attendee's first sign-in
(``package.py --auto-place-minutes``), forwarded ONLY to the compute task as
env ``STORY_AUTO_PLACE_MINUTES``. The backend only publishes the policy; the
frontend decides when to fire. Unset or ``0`` -> ``None`` (disabled,
byte-identical templates); else whole minutes in 5..loop/60; live only
(:func:`auto_place_from_node`). A policy also sets ``FEED_HOLD_ENV_NAME``
(``"1"``, no minutes) on the simulation Lambda so the story site's OPC UA
server publishes nothing until the first placement (ADR-0092 decision 8).

Pure and import-light: no aws_cdk import, the node is duck-typed (anything
with ``try_get_context``), so tests and packaging tools can use it offline.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

CONTEXT_KEY = "workshop_story"
WITH_WORKSHOP_KEY = "with_workshop"
ANCHOR_CONTEXT_KEY = "story_anchor"
STORIES = ("frozen", "live")
DEFAULT_STORY = "frozen"
# Packaged story loop length (ADR-0091): context -> env on the live targets.
LOOP_CONTEXT_KEY = "story_loop_seconds"
LOOP_ENV_NAME = "STORY_LOOP_SECONDS"
DEFAULT_LOOP_SECONDS = 28800           # the 8 h dataset loop
LOOP_GRID_SECONDS = 300                # whole telemetry grid slots
# Inclusive. Capped at the 8 h default: above it the history's failed batch
# can overlap a normal line-3 run (42600..51300 s, 75600..86400 s; ADR-0091).
LOOP_RANGE_SECONDS = (3600, DEFAULT_LOOP_SECONDS)
# Auto-place policy (ADR-0091 Design C): context -> env on the compute task only.
AUTO_PLACE_CONTEXT_KEY = "story_auto_place_minutes"
AUTO_PLACE_ENV_NAME = "STORY_AUTO_PLACE_MINUTES"
AUTO_PLACE_MIN_MINUTES = 5             # the placement POST's alarm_in_minutes floor
DEFAULT_AUTO_PLACE_MINUTES = 80        # package.py's workshop-profile live default
# Story-anchor mirror (ADR-0092): live synths only. The simulation Lambda
# copies simulation_config.story_anchor_epoch into this SSM parameter every
# cycle; the story site's OPC UA server polls it. The database row stays the
# one source of truth; the parameter is a derived copy, never written by hand.
ANCHOR_PARAMETER_NAME = "/adt/workshop/story-anchor-epoch"
ANCHOR_PARAMETER_ENV_NAME = "STORY_ANCHOR_PARAMETER"
ANCHOR_PARAMETER_PENDING = "pending"   # the value before the first mirror (OPC UA: no value yet)
ANCHOR_STORY_SITE = "Indianapolis"     # the OPC UA site that carries the story mill
# One-time history replay (the replay ADR): a SECOND SSM parameter the
# simulation Lambda writes with the replay window and the story site's OPC UA
# server polls to replay the history once on its history nodes. Live synths
# only, and switchable by context ``story_feed_backfill`` (default ON for the
# workshop live profile): turning it off packages no second parameter, no
# history nodes and no SFC history source — a byte-identical template.
BACKFILL_PARAMETER_NAME = "/adt/workshop/story-feed-backfill"
BACKFILL_PARAMETER_ENV_NAME = "STORY_FEED_BACKFILL_PARAMETER"
BACKFILL_PARAMETER_PENDING = "pending"  # before the first placement records a window
BACKFILL_CONTEXT_KEY = "story_feed_backfill"
BACKFILL_DEFAULT_ON = True             # workshop live default: the replay is on unless turned off
# ADR-0092 decision 8: a live synth WITH an auto-place policy tells the
# simulation Lambda (yes/no only; the minutes stay on the compute task) that
# the first sign-in will place the clock, so its anchor mirror holds the story
# site's OPC UA server (no value published) until then.
FEED_HOLD_ENV_NAME = "STORY_FEED_HOLD_UNTIL_PLACED"
# Live synths only: the simulation Lambda gives no simulator work to the story
# machine's line (erp_simulation/mes_simulator.py MESSimulator.reserved_lines),
# so that line runs only the story's own runs. An env value, not the anchor row
# alone, because it must hold from the Lambda's first cycle, before the
# story-history custom resource has written the anchor, and through a reset
# that wipes the row until the custom resource rewrites it.
STORY_LINE_ENV_NAME = "STORY_LINE_RESERVED"
# Live synths only (ADR-0092 decision 6 follow-up): the ERP dashboard stack
# packages no SiteWise degradation-feed Lambda, and this env var on the
# dashboard container tells the OT row to show its plant-clock caption and
# never the feed button (there is no Lambda to invoke).
OT_FEED_ENV_NAME = "OT_FEED_SOURCE"
OT_FEED_FROM_OPCUA = "opcua"


def resolve(with_workshop: bool, raw: Any) -> Optional[str]:
    """Return the story for this synth, or ``None`` outside the workshop.

    Raises ``ValueError`` for an unknown value, or for ``live`` without
    ``with_workshop``.
    """
    value = str(raw).strip().lower() if raw is not None else ""
    if value and value not in STORIES:
        raise ValueError(
            f"{CONTEXT_KEY}={raw!r} is not one of {', '.join(STORIES)}"
        )
    if with_workshop:
        return value or DEFAULT_STORY
    if value == "live":
        raise ValueError(
            f"{CONTEXT_KEY}=live requires {WITH_WORKSHOP_KEY}=true"
        )
    return None


def from_node(node: Any) -> Optional[str]:
    """Resolve the story from a construct node's CDK context."""
    with_workshop = str(node.try_get_context(WITH_WORKSHOP_KEY)).lower() == "true"
    return resolve(with_workshop, node.try_get_context(CONTEXT_KEY))


def resolve_anchor(value: Any) -> Optional[int]:
    """Parse a packaged story anchor into whole epoch seconds.

    ``None``/empty -> ``None`` (no packaged anchor). Accepts a positive integer
    epoch (int or decimal string, the forwarded context form) or an ISO 8601
    string WITH a timezone. Raises ``ValueError`` for a naive ISO string (the
    instant would depend on the synth host's zone) and for anything else.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{ANCHOR_CONTEXT_KEY}={value!r} is not an epoch or ISO 8601 string")
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        epoch = int(text)
    else:
        epoch = _iso_epoch(text)
    if epoch <= 0:
        raise ValueError(f"{ANCHOR_CONTEXT_KEY}={value!r} must be a positive epoch")
    return epoch


def _iso_epoch(text: str) -> int:
    """Whole epoch seconds of a tz-aware ISO 8601 string (``ValueError`` otherwise)."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"{ANCHOR_CONTEXT_KEY}={text!r} is not ISO 8601 "
            "(e.g. 2026-10-14T08:00:00-05:00) or an epoch"
        ) from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            f"{ANCHOR_CONTEXT_KEY}={text!r} has no timezone; add an offset "
            "(e.g. -05:00 or Z) so the instant does not depend on the host"
        )
    return int(moment.timestamp())


def anchor_from_node(node: Any) -> Optional[int]:
    """The packaged anchor from a construct node's CDK context.

    Raises ``ValueError`` when an anchor is set on anything but the ``live``
    story (frozen and non-workshop synths have no story clock).
    """
    anchor = resolve_anchor(node.try_get_context(ANCHOR_CONTEXT_KEY))
    if anchor is not None and from_node(node) != "live":
        raise ValueError(f"{ANCHOR_CONTEXT_KEY} requires {CONTEXT_KEY}=live")
    return anchor


def resolve_loop_seconds(value: Any) -> Optional[int]:
    """Parse a packaged story loop length (seconds) for ``story_loop_seconds``.

    ``None``/blank and the 8 h default both return ``None`` so a default
    package stays byte-identical. Otherwise a base-10 integer (int or decimal
    string) that is a multiple of ``LOOP_GRID_SECONDS`` inside
    ``LOOP_RANGE_SECONDS`` (inclusive); anything else raises ``ValueError``.
    Same rules as ``erp_simulation.condition_monitor``'s env reader.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{LOOP_CONTEXT_KEY}={value!r} is not an integer number of seconds")
    text = str(value).strip()
    if not text:
        return None
    if not (text.isascii() and text.lstrip("-").isdigit()):
        raise ValueError(f"{LOOP_CONTEXT_KEY}={value!r} is not an integer number of seconds")
    seconds = int(text, 10)
    low, high = LOOP_RANGE_SECONDS
    if seconds % LOOP_GRID_SECONDS or not low <= seconds <= high:
        raise ValueError(
            f"{LOOP_CONTEXT_KEY}={value!r} must be a multiple of {LOOP_GRID_SECONDS} "
            f"in [{low}, {high}]"
        )
    return None if seconds == DEFAULT_LOOP_SECONDS else seconds


def loop_from_node(node: Any) -> Optional[int]:
    """The packaged loop length from a construct node's CDK context.

    Raises ``ValueError`` when ANY non-blank loop (even the default) is set on
    anything but the ``live`` story: setting it means "I asked for a loop".
    """
    raw = node.try_get_context(LOOP_CONTEXT_KEY)
    loop = resolve_loop_seconds(raw)
    if raw is not None and str(raw).strip() and from_node(node) != "live":
        raise ValueError(f"{LOOP_CONTEXT_KEY} requires {CONTEXT_KEY}=live")
    return loop


def resolve_auto_place_minutes(value: Any, loop_seconds: Optional[int] = None) -> Optional[int]:
    """Parse the auto-place policy (whole minutes) for ``story_auto_place_minutes``.

    ``None``/blank and ``0`` return ``None`` (disabled, byte-identical). Else a
    base-10 integer (int or decimal string) in ``AUTO_PLACE_MIN_MINUTES`` ..
    one loop in minutes (``loop_seconds`` or the 8 h default, inclusive: the
    placement POST's ``alarm_in_minutes`` range); anything else raises ``ValueError``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{AUTO_PLACE_CONTEXT_KEY}={value!r} is not a whole number of minutes")
    text = str(value).strip()
    if not text:
        return None
    if not (text.isascii() and text.lstrip("-").isdigit()):
        raise ValueError(f"{AUTO_PLACE_CONTEXT_KEY}={value!r} is not a whole number of minutes")
    minutes = int(text, 10)
    if minutes == 0:
        return None
    high = (loop_seconds or DEFAULT_LOOP_SECONDS) // 60
    if not AUTO_PLACE_MIN_MINUTES <= minutes <= high:
        raise ValueError(f"{AUTO_PLACE_CONTEXT_KEY}={value!r} must be 0 (off) or in "
                         f"[{AUTO_PLACE_MIN_MINUTES}, {high}] minutes (one loop)")
    return minutes


def auto_place_from_node(node: Any) -> Optional[int]:
    """The auto-place policy from a construct node's CDK context, bounded by
    the packaged loop. Raises ``ValueError`` when ANY non-blank value (even
    ``0``) is set on anything but the ``live`` story."""
    raw = node.try_get_context(AUTO_PLACE_CONTEXT_KEY)
    if raw is not None and str(raw).strip() and from_node(node) != "live":
        raise ValueError(f"{AUTO_PLACE_CONTEXT_KEY} requires {CONTEXT_KEY}=live")
    return resolve_auto_place_minutes(raw, loop_from_node(node))


def resolve_backfill(value: Any) -> bool:
    """Whether the one-time history replay is on (the replay ADR).

    ``None``/blank -> :data:`BACKFILL_DEFAULT_ON` (on for the workshop live
    profile). ``true``/``1``/``on``/``yes`` -> True; ``false``/``0``/``off``/``no``
    -> False (a byte-identical template with no replay). Anything else raises.
    """
    if value is None:
        return BACKFILL_DEFAULT_ON
    text = str(value).strip().lower()
    if not text:
        return BACKFILL_DEFAULT_ON
    if text in ("true", "1", "on", "yes"):
        return True
    if text in ("false", "0", "off", "no"):
        return False
    raise ValueError(f"{BACKFILL_CONTEXT_KEY}={value!r} must be a boolean (true/false)")


def backfill_from_node(node: Any) -> bool:
    """Whether to package the one-time history replay for this synth: only on
    the ``live`` story (every other synth has no story clock to replay), and
    only when :func:`resolve_backfill` of the context is on. Raises
    ``ValueError`` when the flag is set true on a non-live synth."""
    raw = node.try_get_context(BACKFILL_CONTEXT_KEY)
    on = resolve_backfill(raw)
    if from_node(node) != "live":
        if raw is not None and str(raw).strip() and on:
            raise ValueError(f"{BACKFILL_CONTEXT_KEY} requires {CONTEXT_KEY}=live")
        return False
    return on
