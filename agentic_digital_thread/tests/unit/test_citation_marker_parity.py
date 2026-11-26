# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Server and client agree on which citation markers the chat links.

ONE shared case list, ``tests/fixtures/citation_marker_cases.json``, is read
here (``agents.citation_markers.linked_markers``, the grammar
``CitationOrdinalHook`` removes earlier answers' markers by) AND by
``frontend/src/test/citation-marker-parity.test.ts`` (``buildAnswerModel``, the
client's render path: chart tags split out, then ``injectCitationLinks``). A
change on either side that the other does not mirror fails one of the two
lanes. Review 2026-10-29: a grouped ``[1, 2]``, an index like ``sensor[2]``, a
number like ``[12]`` and a four-backtick fence were each treated differently
by the server than by the client; a group whose members are all citations is
now linked as one chip per member on both sides. A later review: an index
such as ``sensor[1]`` or ``a[1, 2]`` and a footnote line ``[1]: ...`` are never
a citation on either side, whatever the turn's numbers.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agents import citation_markers

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "citation_marker_cases.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]
# The client's chart split drops a markdown part that is only whitespace, so a
# case with one could not be reassembled there; none has one.
_CHART = re.compile(r"""<chart-block((?:[^>'"]|'[^']*'|"[^"]*")*)/?>""")


def _canonical(case) -> str:
    text = case["text"]
    out, last = [], 0
    for group in citation_markers.linked_markers(text, case["current"], case["earlier"]):
        labels = [f"E{m.number}" if m.earlier else str(m.number) for m in group.members]
        out += [text[last:group.start], " ".join("{{" + label + "}}" for label in labels)]
        last = group.end
    out.append(text[last:])
    return "".join(out)


def test_the_case_list_covers_both_kinds_and_both_outcomes():
    assert len(_CASES) > 30
    assert len({c["name"] for c in _CASES}) == len(_CASES)
    assert any("{{E" in c["linked"] for c in _CASES)
    assert any(re.search(r"\{\{\d", c["linked"]) for c in _CASES)
    assert any(c["linked"] == c["text"] and c["current"] for c in _CASES)
    # Linked groups, current and earlier, and a group that stays as written.
    assert any(re.search(r"\}\} \{\{\d", c["linked"]) for c in _CASES)
    assert any("}} {{E" in c["linked"] for c in _CASES)
    assert any(re.search(r"\[\d+, \d+\]", c["linked"]) and c["current"] for c in _CASES)
    for case in _CASES:
        parts = _CHART.split(case["text"])[::2]
        assert all(part == "" or part.strip() for part in parts), case["name"]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_the_server_links_what_the_client_links(case):
    assert _canonical(case) == case["linked"]


def test_the_client_side_reads_the_same_file():
    client = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "test"
              / "citation-marker-parity.test.ts")
    assert "'tests', 'fixtures', 'citation_marker_cases.json'" in client.read_text(
        encoding="utf-8")
