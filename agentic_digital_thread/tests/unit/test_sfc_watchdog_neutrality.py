# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The SFC freshness watchdog's code names no plant and cites no maintainer history.

The Lambda (``lambda-sfc-watchdog/``) and its construct
(``stacks/_sfc_watchdog.py``) sit outside the trees the engine
domain-neutrality ratchet scans, so this applies the ratchet's pinned
denylist to them directly: every site, asset and source name reaches the
watchdog as configuration computed at synth from ``agents/demo/sfc_config.py``.
The same files also carry no merge-request numbers, rehearsal-account
nicknames or notes on the code-review process (the repository-residue forms).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_engine_domain_neutrality import DENYLIST
from tests.unit.test_prompt_no_maintainer_citations import _CITATION, _repo_form

ROOT = Path(__file__).resolve().parents[3]
FILES = sorted((ROOT / "lambda-sfc-watchdog").glob("*.py")) + [ROOT / "stacks" / "_sfc_watchdog.py"]


def test_the_scan_reads_the_watchdog_files():
    assert {p.name for p in FILES} == {"watchdog_decision.py", "handler.py", "watchdog_clock.py", "_sfc_watchdog.py"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_demo_literal(path):
    text = path.read_text(encoding="utf-8")
    hits = {name: re.findall(pattern, text) for name, pattern in DENYLIST.items()}
    assert {k: v for k, v in hits.items() if v} == {}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_maintainer_citation_or_account_nickname(path):
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if _repo_form(ln) or _CITATION.search(ln)]
    assert lines == []
