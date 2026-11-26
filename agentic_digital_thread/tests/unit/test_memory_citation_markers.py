# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Retrieved long-term memory reaches the model without citation markers.

``_inject_ltm_into_user_turn`` moves the SDK's ``<user_context>`` block into
the current user message, where ``strip_earlier_answers`` (which reads earlier
assistant answers) never sees it (review 2026-10-29). A retrieved summary can
carry an earlier answer's ``[1]``, ``[1, 2]`` or ``[E1]``: copied into this
turn's answer it shows plain text, or a chip for this turn's citation 1, a
different result. The model's copy of the block now has every earlier label
removed and every free-standing number or group up to the per-turn citation
cap spelled in fullwidth brackets ``\uff3b1\uff3d``, which the client never links but which keeps a recalled
value such as a tolerance ``[3, 5]`` (``citation_ordinals.without_memory_citations``), with the client's
refusals: an index such as ``sensor[1]``, a footnote line ``[1]: ...``, code
and chart tags stay as written. The stored records are not changed.
Synthetic, domain-neutral text; both query modes.
"""
from __future__ import annotations

import copy

import pytest

import agents.orchestrator as orch
from agents import citation_ordinals as co
from tests.unit.test_orchestrator_output_cap_recovery import (
    _FakeMemoryPlane, _answer, _errors, _run, _tool_call,
)

_MODES = ("agent", "orchestrator")
QUESTION = "and how many failed now?"
RECORDS = [
    "Earlier answer: three failed [1]; both rose [1, 2]; before [E1] and [E1, E2].",
    "[1]: counted from the records system. Read sensor[1] and a[1, 2]; baseline [2024].",
    "Shown as `rows[1]` in the table.",
]
WITHOUT = [
    "Earlier answer: three failed \uff3b1\uff3d; both rose \uff3b1, 2\uff3d; before and.",
    "[1]: counted from the records system. Read sensor[1] and a[1, 2]; baseline [2024].",
    "Shown as `rows[1]` in the table.",
]


def _block(records):
    return "<user_context>" + "\n".join(records) + "</user_context>"


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)


# --------------------------------------------------------------------------- #
# The merge
# --------------------------------------------------------------------------- #
def test_the_merged_block_carries_no_citation_marker():
    messages = [{"role": "user", "content": [{"text": QUESTION}]},
                {"role": "assistant", "content": [{"text": _block(RECORDS)}]}]
    assert orch._inject_ltm_into_user_turn(messages) is True
    assert messages == [{"role": "user", "content": [{"text": _block(WITHOUT)}, {"text": QUESTION}]}]


def test_a_block_without_markers_is_merged_unchanged():
    block = "<user_context>Rates are a share of good parts [2024].</user_context>"
    messages = [{"role": "user", "content": [{"text": QUESTION}]},
                {"role": "assistant", "content": [{"text": block}]}]
    orch._inject_ltm_into_user_turn(messages)
    assert messages[0]["content"][0] == {"text": block}


def test_the_bound_is_the_turn_citation_cap():
    """Numbers past the per-turn cap on citations are never a citation."""
    cap = co.MAX_TURN_CITATIONS
    assert co.without_memory_citations(f"a [{cap}] b [{cap + 1}] c [1, {cap + 1}].") == (
        f"a \uff3b{cap}\uff3d b [{cap + 1}] c [1, {cap + 1}].")


@pytest.mark.parametrize(("recalled", "read"), [
    # Recalled memory has no binding to any turn's citations, so a numeric
    # group may be a value. It keeps its numbers, spelled so the client never
    # links it (review 2026-10-29).
    ("The approved tolerance is [3, 5] mm/s.", "The approved tolerance is \uff3b3, 5\uff3d mm/s."),
    ("Escalate steps [1] and [2] before restart.", "Escalate steps \uff3b1\uff3d and \uff3b2\uff3d before restart."),
    ("Both rose [1][2].", "Both rose \uff3b1\uff3d\uff3b2\uff3d."),
    # An earlier label is reserved syntax and goes, alone or in a group.
    ("Same as before [E1], and [1, E2].", "Same as before, and."),
])
def test_recalled_numbers_keep_their_value(recalled, read):
    assert co.without_memory_citations(recalled) == read


def test_the_recalled_spelling_is_never_linked():
    from agents.citation_markers import linked_markers
    read = co.without_memory_citations("Steps [1] and [1, 2] passed.")
    assert linked_markers(read, current={1, 2}) == []


@pytest.mark.parametrize(("marker", "current", "earlier"), [("[1]", {1}, ()), ("[E1]", (), {1})])
def test_a_real_marker_right_after_a_recalled_value_still_links(marker, current, earlier):
    """Review 2026-10-29 (round 5): with an ASCII ``]`` closing the recalled
    value, a real marker written right after it was refused as attached."""
    from agents.citation_markers import linked_markers
    read = co.without_memory_citations("The approved tolerance is [3, 5] mm/s.")
    answer = read.replace(" mm/s.", marker + " mm/s.")
    assert len(linked_markers(answer, current=current, earlier=earlier)) == 1


# --------------------------------------------------------------------------- #
# The real runtime, both query modes
# --------------------------------------------------------------------------- #
class _RecallPlane(_FakeMemoryPlane):
    """The memory plane, plus a session summary that kept an earlier answer's
    citation markers."""

    def __init__(self):
        super().__init__()
        self.records = [{"content": {"text": text}} for text in RECORDS]
        self.kept = copy.deepcopy(self.records)

    def retrieve_memory_records(self, **params):
        # bedrock-agentcore 1.9.1+ retrieves by path prefix (namespacePath).
        namespace = params.get("namespace") or params.get("namespacePath", "")
        if namespace.startswith("/summaries/"):
            return {"memoryRecordSummaries": copy.deepcopy(self.records)}
        return {"memoryRecordSummaries": []}


def _user_texts(message):
    return [b["text"] for b in message["content"] if isinstance(b.get("text"), str)]


def _summary(events):
    return next(e["intelligence_summary"] for e in events
                if isinstance(e, dict) and "intelligence_summary" in e)


@pytest.mark.parametrize("mode", _MODES)
def test_the_model_reads_recalled_memory_without_markers(monkeypatch, mode):
    """This turn's citation 1 is a new result. The model's first call reads
    the recalled block without the old [1], [1, 2] and [E1]; the result it
    then reads carries this turn's [1]; there is no earlier source, so the
    answer's [1] is this turn's chip and the Sources row lists only it."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", mode)
    plane = _RecallPlane()
    events, model = _run(monkeypatch, [_tool_call("t-1", "which records failed now"),
                                       _answer("Now four failed [1].")],
                         prompt=QUESTION, session_id="s-recall", plane=plane)
    assert _errors(events) == []
    first = model.calls[0]
    recalled = [t for t in _user_texts(first[-1]) if t.startswith("<user_context>")]
    assert recalled == [_block(WITHOUT)], mode
    second = model.calls[1]
    result = next(b["toolResult"] for m in second for b in m["content"]
                  if b.get("toolResult", {}).get("toolUseId") == "t-1")
    assert result["content"][-1] == {"text": co.citation_stamp(1)}
    assert not _summary(events).get("earlier_sources")
    # The Sources row: this turn's one call, as chip 1, and nothing earlier.
    provenance = next(e["provenance"] for e in events if isinstance(e, dict) and "provenance" in e)
    assert [(r["citation_num"], r["tool_id"]) for r in provenance["records"]] == [(1, "t-1")]
    assert "".join(e["data"] for e in events
                   if isinstance(e, dict) and isinstance(e.get("data"), str)) == "Now four failed [1]."
    assert plane.records == plane.kept
