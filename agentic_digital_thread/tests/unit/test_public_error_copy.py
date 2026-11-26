# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pins the attendee-facing copy of the public ``timeout`` error frame.

The chat shows ``PUBLIC_ERROR_MESSAGES["timeout"]`` verbatim. It must tell
the reader what to do next, and it must keep a word-boundary "timed out"
token so the smoke/eval error taxonomy still labels the frame TIMEOUT
(``scripts/smoke_prompts.py`` classifies stream error TEXT, not the
category field). Offline; no AWS / network.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents.events import public_error_message  # noqa: E402
from scripts.smoke_prompts import classify_error_labels  # noqa: E402


def test_timeout_copy_says_what_to_do_next():
    assert public_error_message("timeout") == (
        "The question timed out. Ask again, or ask a narrower question."
    )


def test_timeout_copy_still_classifies_as_timeout_in_the_smoke_taxonomy():
    assert classify_error_labels([public_error_message("timeout")]) == ["TIMEOUT"]
