# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The coded-value pattern in the context interview runs in linear time.

_ENUM_VALUE_RE matches sample column values. A nested quantifier in its
"Family: CodeValue" alternative backtracked exponentially on a long run of
capitals that fails at the end, and Python's re holds the GIL, so one such
value stalled the whole server. The timing check runs in a subprocess with a
timeout, so a regression fails instead of hanging the test run.
"""
from __future__ import annotations

import subprocess  # nosec B404 - fixed argv, this test's own interpreter
import sys
import time
from pathlib import Path

import pytest

from agents.context_interview.questions import _ENUM_VALUE_RE

_APP_DIR = Path(__file__).resolve().parents[2]
_PROBE = (
    "import time\n"
    "from agents.context_interview.questions import _ENUM_VALUE_RE\n"
    "t = time.perf_counter()\n"
    "m = _ENUM_VALUE_RE.match('A: ' + 'A' * 28 + '!')\n"
    "print(m is None, time.perf_counter() - t)\n"
)


def test_a_long_failing_value_is_rejected_quickly():
    start = time.perf_counter()
    try:
        out = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-c", _PROBE], cwd=_APP_DIR,
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout.split()
    except subprocess.TimeoutExpired:
        pytest.fail(f"_ENUM_VALUE_RE took over 20 s ({time.perf_counter() - start:.1f} s)")
    rejected, seconds = out[0] == "True", float(out[1])
    assert rejected
    assert seconds < 1.0, f"_ENUM_VALUE_RE.match took {seconds:.2f} s"


@pytest.mark.parametrize("value", [
    "PartNumber", "IN_PROGRESS", "Status: Released", "Family: CodeValue", "Disp:SCRAP_HOLD",
])
def test_coded_values_match(value):
    assert _ENUM_VALUE_RE.match(value)


@pytest.mark.parametrize("value", [
    "Status: released", "A: ", "Part 12", "lot-123", "Status: Released!",
])
def test_other_values_do_not_match(value):
    assert _ENUM_VALUE_RE.match(value) is None
