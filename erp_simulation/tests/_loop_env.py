# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run story-module code in a fresh interpreter under a ``STORY_LOOP_SECONDS``.

``condition_monitor`` reads the loop length ONCE at import (ADR-0091) and
every story module derives its constants from it at import, so a non-default
loop needs a fresh process: reloading in-process would leave the already
imported dependents (and every other test in the lane) on mixed geometry.

``run_under_loop(loop, code)`` executes ``code`` with the repo root on
``sys.path`` and returns the JSON the code prints last (``emit(obj)`` is
predefined). ``loop=None`` runs with the variable unset.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - runs this interpreter on test-authored code only
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOP_ENV = "STORY_LOOP_SECONDS"
_PRELUDE = "import json, sys\ndef emit(obj):\n    print(json.dumps(obj, sort_keys=True))\n"


def loop_env(loop: Optional[str]) -> dict:
    env = {k: v for k, v in os.environ.items() if k != LOOP_ENV}
    if loop is not None:
        env[LOOP_ENV] = loop
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def run_under_loop(loop: Optional[str], code: str, *, expect_ok: bool = True):
    """``(returncode, stdout, stderr)`` when ``expect_ok`` is False; else the
    parsed JSON of the last stdout line (asserting a zero exit)."""
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", _PRELUDE + code], cwd=str(REPO_ROOT), env=loop_env(loop),
        capture_output=True, text=True, timeout=120, check=False)
    if not expect_ok:
        return proc.returncode, proc.stdout, proc.stderr
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])
