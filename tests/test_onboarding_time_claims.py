# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The presenter script and the post-deploy guide state no onboarding duration.

Onboarding time depends on the system (schema size, and whether its agent
runtime is being created for the first time), and the two documents gave
figures that disagreed with each other and with a measured fresh deploy. They
now say what the time depends on instead of quoting a number.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DOCS = ["agentic_digital_thread/frontend/public/demo-script.md", "docs/post-deploy.md"]
_DURATION = re.compile(r"\d+\s*(?:[–-]\s*\d+\s*)?(?:seconds?|minutes?|mins?|secs?)\b", re.I)


def _onboarding_lines(text: str) -> list[str]:
    """The answer to the script's onboarding question and the guide's status line."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "How long does onboarding take" in line or "**Onboarded**" in line:
            out.extend(lines[max(0, i - 2):i + 3])
    return out


def test_no_onboarding_duration_is_stated():
    for rel in _DOCS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        near = _onboarding_lines(text)
        assert near, f"{rel}: the onboarding passage moved; update this test"
        hits = [ln for ln in near if _DURATION.search(ln)]
        assert not hits, f"{rel}: onboarding duration stated: {hits}"
