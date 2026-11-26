# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The async ask path delivers the answer without the web UI's control blocks.

/api/ask/result's only consumer is an external MCP client (the Quick query
bridge relays it verbatim), which has no parser for the orchestrator's
proposal blocks, its candidate-pair counter or the ---FOLLOWUPS--- block.
Rehearsal 2026-10-03 (Quick probe): the module 1b answer arrived with
a raw VALUE-MAPPING-PROPOSAL block, a CORRELATION-PROPOSAL ending in "Save this
pattern? (yes / no / set a threshold)", the counter line and the Q1:/A1: lines.
"""

import asyncio
import time
import types

import pytest

import agents.server as server
from agents.eval_core import split_followups, strip_followups
from agents.job_store import JobStore
from agents.models import AskRequest
from agents.public_answer import external_answer_text

RAW = (
    "## Lot exposure\n\n"
    "Three customers received parts from the failed run [1].\n\n"
    "---VALUE-MAPPING-PROPOSAL---\n"
    "from_system: mes-ind\nfrom_field: production_runs.lot_number\n"
    "to_system: erp\nto_field: shipment_items.batch_id\n"
    "---END-PROPOSAL---\n"
    "additional_candidate_pairs_observed: 0\n\n"
    "---CORRELATION-PROPOSAL---\n"
    "Pattern: material-lot correlates with shipped-batch risk\n"
    "Save this pattern? (yes / no / set a threshold)\n"
    "---END-PROPOSAL---\n\n"
    "---FOLLOWUPS---\n"
    "Q1: Show the bore readings for the failed run?\n"
    "Q2: None\n"
    "A1: Hold shipments from the failed run.\n"
)


def test_control_blocks_are_removed_and_followups_become_plain_lists():
    out = external_answer_text(RAW)
    assert out == (
        "## Lot exposure\n\n"
        "Three customers received parts from the failed run [1].\n\n"
        "Follow-up questions:\n"
        "- Show the bore readings for the failed run?\n\n"
        "Suggested actions:\n"
        "- Hold shipments from the failed run."
    )
    for leaked in ("PROPOSAL", "additional_candidate_pairs_observed",
                   "Save this pattern", "FOLLOWUPS", "Q1:", "A1:"):
        assert leaked not in out


def test_text_without_blocks_is_unchanged_but_for_trailing_space():
    prose = "Line one.\n\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
    assert external_answer_text(prose) == prose.rstrip()
    assert external_answer_text("") == ""
    assert external_answer_text(None) == ""


def test_an_opened_proposal_block_is_cut_and_a_tolerant_marker_still_splits():
    raw = ("Answer.\n\n---CORRELATION-PROPOSAL---\nPattern: x\n"
           "\n**FOLLOWUPS**\nQ1: Next question?\n")
    assert external_answer_text(raw) == (
        "Answer.\n\nFollow-up questions:\n- Next question?")


def test_an_orphan_end_sentinel_never_reaches_the_reader():
    """Event 26 (2026-10-04, module 3 refusal): the model emitted the closing
    ``---END-PROPOSAL---`` with no opener, and the literal line rendered under
    the refusal. A closing sentinel is machine payload whether or not its
    opener survived."""
    text = ("I can't share that data.\n\n---END-PROPOSAL---\n\n"
            "additional_candidate_pairs_observed: 0\n")
    assert external_answer_text(text) == "I can't share that data."
    assert external_answer_text("Done.\n--- END-PROPOSAL ---\nMore.") == "Done.\nMore."


def test_an_opener_with_a_different_dash_count_still_closes_the_block():
    """The same protocol block written with two or four dashes (or spaces
    inside the marker) is still the block, not prose."""
    text = ("Answer.\n--VALUE-MAPPING-PROPOSAL--\nfrom_system: a\n--END-PROPOSAL--\n"
            "After.\n---- CORRELATION PROPOSAL ----\nx\n----END-PROPOSAL----\nEnd.")
    assert external_answer_text(text) == "Answer.\n\nAfter.\n\nEnd."


def test_prose_mentioning_followups_or_a_counter_is_left_alone():
    prose = ("We saw additional_candidate_pairs_observed: 1 in the data, "
             "and three followups were planned.")
    assert external_answer_text(prose) == prose


def test_split_followups_mirrors_strip_followups():
    body, block = split_followups("Body.\n---FOLLOWUPS---\nQ1: q?")
    assert (body, block) == ("Body.", "\nQ1: q?")
    assert strip_followups("Body.\n---FOLLOWUPS---\nQ1: q?") == "Body."
    assert split_followups("No marker.") == ("No marker.", None)


class _BlockClient:
    def __init__(self):
        pass

    def stream(self, prompt, session_id, user_id, write_memory=True, **_k):
        from agents.events import MessageEvent
        half = len(RAW) // 2
        yield MessageEvent(content=RAW[:half])
        yield MessageEvent(content=RAW[half:])


@pytest.fixture
def _async_env(monkeypatch):
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(server, "_ASK_JOBS", JobStore("ask", table=None))
    monkeypatch.setattr(server, "get_config",
                        lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _BlockClient)


def test_the_async_job_stores_the_external_form(_async_env):
    job_id = server.ask_async(AskRequest(question="lot exposure?"))["job_id"]
    deadline = time.time() + 5
    while server._ask_jobs().get(job_id, {}).get("status") == "running":
        assert time.time() < deadline
        time.sleep(0.02)
    result = asyncio.run(server.ask_result(job_id))
    assert result == {"status": "done", "answer": external_answer_text(RAW)}


# Module 5 walk (2026-10-27): the model put the opener's dashes on the line
# above as a horizontal rule, then the marker alone on its line. The orphan-END
# rule removed only the closing line and the Pattern/Observed body rendered.
_RULE_OPENER = (
    "The PM deferral preceded the failure [3].*\n\n"
    "---\nCORRELATION-PROPOSAL\n"
    "Pattern: deferred PM work orders in CMMS correlate with bearing failures\n"
    "Confidence: high\nSave this pattern? (yes / no / set a threshold)\n"
    "---END-PROPOSAL---\n\n"
    "additional_candidate_pairs_observed: 0\n"
)


def test_an_opener_under_a_rule_line_is_still_the_block():
    out = external_answer_text(_RULE_OPENER)
    assert out == "The PM deferral preceded the failure [3].*"
    for leaked in ("PROPOSAL", "Pattern:", "Save this pattern", "---"):
        assert leaked not in out


def test_an_unclosed_bare_marker_line_cuts_the_rest():
    raw = "Answer.\n\nCORRELATION-PROPOSAL\nPattern: x\n"
    assert external_answer_text(raw) == "Answer."


def test_the_marker_words_inside_a_sentence_are_prose():
    prose = "A CORRELATION-PROPOSAL card appears below the answer.\n\nMore."
    assert external_answer_text(prose) == prose
