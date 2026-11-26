# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Internal candidate-pair telemetry never becomes assistant prose."""
from __future__ import annotations

import json
import time

from agents import server
from agents.events import DoneEvent, MessageEvent
from agents.public_answer import InternalTelemetryFilter, redact_internal_telemetry


def _payload(frame: str) -> tuple[str, dict]:
    lines = frame.strip().splitlines()
    event = lines[0].split(":", 1)[1].strip()
    data = json.loads(lines[1].split(":", 1)[1].strip())
    return event, data


def test_split_internal_counter_is_removed_from_user_facing_answer_text():
    worker = server._StreamWorker(maxsize=8)
    worker.queue.put(MessageEvent("Answer body.\nadditional_candidate_"))
    worker.queue.put(MessageEvent("pairs_observed: 0\nClosing sentence."))
    worker.queue.put(DoneEvent())
    worker.queue.put(None)
    stats = {"token_count": 0, "tools_used": []}

    frames = list(server._drain_event_queue(worker, stats, time.monotonic()))
    public_text = "".join(
        payload["text"] for event, payload in map(_payload, frames)
        if event in {"token", "done"})

    assert public_text == "Answer body.\n\nClosing sentence."
    assert "additional_candidate_pairs_observed" not in public_text
    assert stats["additional_candidate_pairs_observed"] == 0
    assert server._ask_tool_usage(stats) == {
        "tools": [], "additional_candidate_pairs_observed": 0}


def test_unterminated_counter_is_metadata_only_but_similar_prose_survives():
    text = ("additional_candidate_pairs_observed: 7\n"
            "Additional candidate analysis remains in prose.")
    public, counts = redact_internal_telemetry(text)

    assert counts == [7]
    assert public == "\nAdditional candidate analysis remains in prose."
    assert "additional_candidate_pairs_observed" not in public


def test_filter_bounds_and_releases_non_record_prefixes():
    filter_ = InternalTelemetryFilter()
    public = filter_.feed("additional context is public") + filter_.flush()
    assert public == "additional context is public"
    assert filter_.candidate_pair_values == []


# --- a counter line the model wrapped in Markdown emphasis ----------------------------------
# A saved Daily Briefing stream (2026-10-17) carried the counter as
# "*additional_candidate_pairs_observed: 0*". The record is removed and counted with exactly one
# matching wrapper; the web parser (answer-blocks.tsx CANDIDATE_COUNTER_WRAPPERS) uses the same list.

import re  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from agents import public_answer  # noqa: E402
from agents.public_answer import external_answer_text  # noqa: E402

_WRAPPED = [("*", 0), ("_", 3), ("**", 2), ("__", 5)]


@pytest.mark.parametrize("wrapper,count", _WRAPPED)
def test_a_wrapped_counter_line_is_removed_and_counted(wrapper, count):
    text = f"Answer body.\n\n{wrapper}additional_candidate_pairs_observed: {count}{wrapper}\nClosing."
    public, counts = redact_internal_telemetry(text)
    assert counts == [count]
    assert "additional_candidate_pairs_observed" not in public
    assert public == "Answer body.\n\n\nClosing."


@pytest.mark.parametrize("wrapper,count", _WRAPPED)
def test_a_wrapped_counter_split_across_every_chunk_boundary_is_removed(wrapper, count):
    line = f"{wrapper}additional_candidate_pairs_observed: {count}{wrapper}"
    text = f"Answer body.\n{line}\nClosing."
    for cut in range(len("Answer body.\n") + 1, len("Answer body.\n") + len(line) + 1):
        filter_ = InternalTelemetryFilter()
        public = filter_.feed(text[:cut]) + filter_.feed(text[cut:]) + filter_.flush()
        assert public == "Answer body.\n\nClosing.", cut
        assert filter_.candidate_pair_values == [count], cut


@pytest.mark.parametrize("wrapper,count", _WRAPPED)
def test_a_wrapped_counter_streamed_one_character_at_a_time_and_unterminated(wrapper, count):
    filter_ = InternalTelemetryFilter()
    text = f"Done.\n{wrapper}additional_candidate_pairs_observed: {count}{wrapper}"
    public = "".join(filter_.feed(ch) for ch in text) + filter_.flush()
    assert public == "Done.\n"
    assert filter_.candidate_pair_values == [count]


@pytest.mark.parametrize("line", [
    "*additional_candidate_pairs_observed: 1_",     # mismatched wrapper
    "**additional_candidate_pairs_observed: 1*",    # mismatched wrapper
    "***additional_candidate_pairs_observed: 1***",  # more than one wrapper
    "*additional_candidate_pairs_observed: 1* and more",
    "* additional_candidate_pairs_observed: 1",      # a list item, not a wrapper
    "*Note:* the total was capped.",
    "**Bold** text",
    "_italic_ text",
])
def test_lines_that_are_not_exactly_one_wrapped_record_are_left_alone(line):
    public, counts = redact_internal_telemetry(f"{line}\nNext.")
    assert counts == []
    assert public == f"{line}\nNext."


@pytest.mark.parametrize("wrapper,count", _WRAPPED)
def test_external_answer_text_drops_a_wrapped_counter(wrapper, count):
    text = (f"Answer body.\n\n---\n{wrapper}additional_candidate_pairs_observed: {count}{wrapper}\n\n"
            "---FOLLOWUPS---\nQ1: Next question?\nA1: Next action.")
    out = external_answer_text(text)
    assert "additional_candidate_pairs_observed" not in out
    assert out.startswith("Answer body.")
    assert "Follow-up questions:\n- Next question?" in out


def test_server_and_web_parsers_share_one_wrapper_list():
    tsx = (Path(__file__).resolve().parents[2]
           / "frontend/src/shared/thread/answer-blocks.tsx").read_text()
    m = re.search(r"CANDIDATE_COUNTER_WRAPPERS\s*=\s*\[([^\]]*)\]", tsx)
    assert m, "answer-blocks.tsx must declare CANDIDATE_COUNTER_WRAPPERS"
    web = tuple(re.findall(r"'([^']*)'", m.group(1)))
    assert web == public_answer.CANDIDATE_PAIR_WRAPPERS


def test_streamed_filter_equals_the_line_rule_for_random_text_and_chunking():
    """Seeded property check: however the text is chunked, the filter drops exactly the lines
    the record rule matches (keeping their newline) and counts them, and nothing else."""
    import random

    rng = random.Random(20261017)
    pieces = ["additional_candidate_pairs_observed:", " ", "\t", "0", "12", "*", "**", "_", "__",
              "\n", "\r", "x", "a", "Note: ", "- ", "additional", "ADDITIONAL_CANDIDATE_PAIRS_OBSERVED:"]
    for _ in range(3000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))
        expected_parts, expected_counts = [], []
        lines = text.split("\n")
        for i, line in enumerate(lines):
            m = public_answer._CANDIDATE_PAIR_RECORD.fullmatch(line)
            if m and len(line) <= public_answer._MAX_CANDIDATE_RECORD_CHARS:
                expected_counts.append(int(m.group(2)))
                line = ""
            expected_parts.append(line)
        expected = "\n".join(expected_parts)
        filter_ = InternalTelemetryFilter()
        out, pos = [], 0
        while pos < len(text):
            step = rng.randint(1, 5)
            out.append(filter_.feed(text[pos:pos + step]))
            pos += step
        out.append(filter_.flush())
        assert "".join(out) == expected, repr(text)
        assert filter_.candidate_pair_values == expected_counts, repr(text)
