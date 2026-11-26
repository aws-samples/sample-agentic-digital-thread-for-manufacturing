# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The bastion's connect-db helper parses the secret with working jq filters.

Unquoted, ``jq -r .port // 5432`` hands ``//`` and ``5432`` to jq as input
files, so DB_PORT and DB_NAME came back empty and psql fell back to a
database named after the user. Every jq filter in the helper must be one
quoted argument when it uses the ``//`` fallback. Source-level check, no synth.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STACK = Path(__file__).resolve().parents[1] / "stacks" / "erp_database_stack.py"


def _helper_lines() -> list[str]:
    text = STACK.read_text()
    return [m.group(0) for m in re.finditer(r"jq -r [^)]*", text)]


def test_every_fallback_filter_is_one_quoted_argument():
    lines = [line for line in _helper_lines() if "//" in line]
    assert lines, "connect-db no longer parses port/dbname with a fallback"
    for line in lines:
        assert re.search(r"jq -r \\\\?'[^']*//[^']*\\\\?'", line), line


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize("filt,expected", [
    (".port // 5432", "5432"),
    ('.dbname // "erp_simulation"', "erp_simulation"),
])
def test_the_quoted_filter_falls_back(filt, expected):
    out = subprocess.run(["jq", "-r", filt], input='{"host": "db"}',
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == expected
