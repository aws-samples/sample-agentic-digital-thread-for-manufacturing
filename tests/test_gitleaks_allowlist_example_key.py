# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The gitleaks allowlist entry for AWS's documentation example key.

The entry is written as a regex whose last letter is a one-character class, so
``.gitleaks.toml`` itself holds no key-shaped literal (a secret scanner reading
the file would otherwise report the allowlist as a leaked key). It must still
match the documented example key, and only that key.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS documentation example key, built so this file has no literal


def _allow_regexes() -> list[str]:
    cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    return cfg["allowlist"]["regexes"]


def test_the_allowlist_still_matches_the_documented_example_key():
    assert any(re.search(rx, _EXAMPLE_KEY) for rx in _allow_regexes())


def test_the_allowlist_does_not_match_a_neighbouring_key():
    other = _EXAMPLE_KEY[:-1] + "F"
    assert not any(re.search(rx, other) for rx in _allow_regexes())


def test_the_file_holds_no_key_shaped_literal():
    text = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    assert not re.search(r"(A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}", text)
