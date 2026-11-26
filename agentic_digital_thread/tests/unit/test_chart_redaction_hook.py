# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ChartRedactionHook — deterministic chart delivery (replaces the chat_client-side injection).

The Strands ``MessageAddedEvent`` hook on the orchestrator Agent redacts
``<chart-block .../>`` tags from model-visible toolResult content IN PLACE
(string / list-of-parts / json part shapes), replaces them with an
"ALREADY RENDERED" stub counting the charts (enforce mode: an honest
"NOT delivered" stub), and queues the extracted tags on
``agent._pending_chart_blocks``. The runtime stream loop injects them as
plain data events with content-hash dedup (``drain_pending_charts`` — title
+ tag hash: same-title different-window charts all render).

Contract pinned here:

* string-shaped toolResult content: all tags queued, content replaced by a
  stub counting the charts and naming no title, no ``<chart-block`` remains,
  non-chart text kept;
* list-of-parts shape redacted in place, untouched parts left alone;
* json-part shape: tags inside nested JSON extracted UNESCAPED and the
  redacted payload re-parses clean;
* the toolResult gate: model-authored messages carrying chart markup are
  never redacted or queued (hand-built SQL charts stay legal);
* an unrecognized content shape carrying chart markup logs a WARNING naming
  the content types (shape-drift observability) and leaves the message intact;
* an exception inside redaction is swallowed — the agent loop never breaks;
* ``drain_pending_charts`` drains the queue and dedups by title per session;
* the hook is registered on BOTH production (build_orchestrator) and local
  (get_orchestrator) Agent constructions — the upstream root-cause was
  local-mode-only registration;
* the earlier chat_client-side injection seam is fully retired (grep gate).

All payloads are SYNTHETIC (no dataset literals); everything runs offline.
"""
from __future__ import annotations

import inspect
import json
import logging
import queue
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agents
from agents.orchestrator import (
    ChartRedactionHook, _CHART_TAG_RE, _CHART_TITLE_RE, _redact_chart_text,
    drain_pending_charts,
)

TAG_A = '<chart-block type="line" title="series-a" labels="[1,2]" data="[10,20]"/>'
TAG_B = '<chart-block type="line" title="series-b" labels="[1,2]" data="[30,40]"/>'
# Same TITLE as TAG_A, different data — must dedup against TAG_A.
TAG_A_ALT = '<chart-block type="line" title="series-a" labels="[3,4]" data="[50,60]"/>'
TAG_UNTITLED = '<chart-block type="line" labels="[1]" data="[1]"/>'


def _fire(content, agent=None):
    """Run the hook handler over a message with the given content list."""
    agent = agent if agent is not None else SimpleNamespace()
    event = SimpleNamespace(message={"role": "user", "content": content}, agent=agent)
    ChartRedactionHook()._on_message_added(event)
    return agent


# --------------------------------------------------------------------------- #
# (1) String-shaped toolResult content (gateway MCP results)
# --------------------------------------------------------------------------- #
def test_string_shape_two_tags_queued_and_stubbed():
    text = f"Telemetry summary\n{TAG_A}\nmid prose\n{TAG_B}\ntail"
    content = [{"toolResult": {"toolUseId": "t-1", "content": text}}]
    agent = _fire(content)
    redacted = content[0]["toolResult"]["content"]
    assert "<chart-block" not in redacted
    assert "[2 chart(s) ALREADY RENDERED" in redacted
    assert "series-a" not in redacted and "series-b" not in redacted
    # Non-chart text (headers, stats) is preserved around the stub.
    assert "Telemetry summary" in redacted
    assert "mid prose" in redacted and "tail" in redacted
    assert agent._pending_chart_blocks == [TAG_A, TAG_B]


# --------------------------------------------------------------------------- #
# (2) List-of-parts shape redacted in place
# --------------------------------------------------------------------------- #
def test_list_of_parts_shape_redacted_in_place():
    parts = [{"text": f"stats header\n{TAG_A}"}, {"text": "no charts in this part"}]
    content = [{"toolResult": {"toolUseId": "t-2", "content": parts}}]
    agent = _fire(content)
    assert "<chart-block" not in parts[0]["text"]
    assert "ALREADY RENDERED" in parts[0]["text"]
    assert "stats header" in parts[0]["text"]
    assert parts[1]["text"] == "no charts in this part"
    assert agent._pending_chart_blocks == [TAG_A]


# --------------------------------------------------------------------------- #
# (3) json-part shape: tags extracted UNESCAPED, payload re-parses clean
# --------------------------------------------------------------------------- #
def test_json_part_extracted_unescaped_and_reparses_clean():
    payload = {"columns": ["m"], "rows": [["1"]], "chart_markup": f"ready:\n{TAG_A}"}
    part = {"json": payload}
    content = [{"toolResult": {"toolUseId": "t-3", "content": [part]}}]
    agent = _fire(content)
    # Queued tag is byte-identical to the original — i.e. UNESCAPED, even
    # though the tag's quotes were \"-escaped inside the serialized JSON.
    assert agent._pending_chart_blocks == [TAG_A]
    # The redacted payload re-parsed clean back into a structure...
    assert isinstance(part["json"], dict)
    blob = json.dumps(part["json"])
    assert "<chart-block" not in blob
    # ...and non-chart fields survived intact.
    assert part["json"]["columns"] == ["m"]
    assert part["json"]["rows"] == [["1"]]
    assert "chart redacted" in part["json"]["chart_markup"]


# --------------------------------------------------------------------------- #
# (3b) H1 regression: an external string carrying "<chart-block" with NO
# complete tag must leave the payload byte-identical (the old blob-level
# regex gobbled across JSON string boundaries and deleted sibling keys).
# --------------------------------------------------------------------------- #
def test_json_part_incomplete_tag_never_deletes_sibling_keys(caplog):
    payload = {
        "note": "malicious or truncated: <chart-block type=\"line\" title=\"x\"",
        "sibling": "MUST SURVIVE",
        "rows": [["1", "2"]],
    }
    original = json.loads(json.dumps(payload))
    part = {"json": payload}
    content = [{"toolResult": {"toolUseId": "t-h1", "content": [part]}}]
    with caplog.at_level(logging.WARNING, logger="agents.orchestrator"):
        agent = _fire(content)
    # Nothing queued, payload untouched — sibling keys intact.
    assert getattr(agent, "_pending_chart_blocks", None) is None
    assert part["json"] == original
    assert any("H1 guard" in r.getMessage() or "no complete" in r.getMessage()
               for r in caplog.records)


def test_json_part_complete_tag_next_to_incomplete_string_is_bounded():
    """A complete tag in one string redacts; an incomplete marker in a SIBLING
    string stays intact — matches are bounded to one JSON string (H1)."""
    payload = {
        "chart_markup": f"ready:\n{TAG_A}",
        "trap": "<chart-block never closed",
        "keep": "untouched",
    }
    part = {"json": payload}
    content = [{"toolResult": {"toolUseId": "t-h1b", "content": [part]}}]
    agent = _fire(content)
    assert agent._pending_chart_blocks == [TAG_A]
    assert part["json"]["trap"] == "<chart-block never closed"
    assert part["json"]["keep"] == "untouched"
    assert "<chart-block" not in part["json"]["chart_markup"]


# --------------------------------------------------------------------------- #
# (3c) M2: under RESPONSE_INTEGRITY_MODE=enforce the drained tags are DROPPED,
# so the stub must not claim the charts were rendered.
# --------------------------------------------------------------------------- #
def test_enforce_mode_stub_never_claims_rendered(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    out = _redact_chart_text(f"header\n{TAG_A}\ntail")
    assert "ALREADY RENDERED" not in out
    assert "[1 chart(s) withheld and NOT delivered" in out
    assert "series-a" not in out  # a count, never a title
    assert "<chart-block" not in out


# --------------------------------------------------------------------------- #
# (4) toolResult gate: model-authored chart markup is never redacted/queued
# --------------------------------------------------------------------------- #
def test_assistant_message_without_tool_result_untouched():
    content = [{"text": f"Here is a chart I built from SQL results: {TAG_A}"}]
    agent = _fire(content)
    assert content == [{"text": f"Here is a chart I built from SQL results: {TAG_A}"}]
    assert getattr(agent, "_pending_chart_blocks", None) is None


# --------------------------------------------------------------------------- #
# (5) Unrecognized shape with tags ⇒ WARNING logged, message intact
# --------------------------------------------------------------------------- #
def test_unrecognized_shape_warns_and_leaves_message_intact(caplog):
    weird = {"weird_nested": TAG_A}
    content = [{"toolResult": {"toolUseId": "t-5", "content": weird}}]
    with caplog.at_level(logging.WARNING, logger="agents.orchestrator"):
        agent = _fire(content)
    assert content[0]["toolResult"]["content"] is weird
    assert weird == {"weird_nested": TAG_A}
    assert getattr(agent, "_pending_chart_blocks", None) is None
    warning = next(r for r in caplog.records if "CHART_REDACTION" in r.getMessage())
    assert "shape" in warning.getMessage()
    assert "dict" in warning.getMessage()  # names the content types seen


# --------------------------------------------------------------------------- #
# (6) Exception inside redaction is swallowed — the agent loop never breaks
# --------------------------------------------------------------------------- #
def test_redaction_exception_swallowed(caplog):
    text = f"header {TAG_A}"
    content = [{"toolResult": {"toolUseId": "t-6", "content": text}}]
    with caplog.at_level(logging.ERROR, logger="agents.orchestrator"), \
         patch("agents.orchestrator._redact_chart_text",
               side_effect=RuntimeError("boom")):
        agent = _fire(content)  # must not raise
    assert content[0]["toolResult"]["content"] == text  # left intact
    assert getattr(agent, "_pending_chart_blocks", None) is None
    assert any("CHART_REDACTION failed" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Redaction text helper
# --------------------------------------------------------------------------- #
def test_redact_chart_text_no_tags_is_identity():
    noise = '<chart-block type="line" title="open-tag-no-slash">\nbody\n</chart-block>\n'
    assert _redact_chart_text(noise) == noise
    assert _redact_chart_text("no charts") == "no charts"


def test_redact_chart_text_untitled_tag():
    out = _redact_chart_text(TAG_UNTITLED)
    assert "[1 chart(s) ALREADY RENDERED" in out
    assert "<chart-block" not in out


# --------------------------------------------------------------------------- #
# A notice-only tag (kind="notice") is not a chart: the model is told no chart
# was drawn, never that one was rendered, in either integrity mode.
# --------------------------------------------------------------------------- #
TAG_NOTICE = ('<chart-block type="line" kind="notice" '
              'title="2 series not charted: no name recorded." '
              "labels='[]' datasets='[]' />")


def test_notice_tag_stub_says_no_chart_was_drawn():
    out = _redact_chart_text(f"header\n{TAG_NOTICE}")
    assert out.startswith("header\n")
    assert "ALREADY RENDERED" not in out
    assert "No chart was drawn" in out
    assert "2 series not charted: no name recorded." in out
    assert "<chart-block" not in out


def test_notice_tag_stub_in_enforce_mode_never_claims_rendered(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    out = _redact_chart_text(TAG_NOTICE)
    assert "ALREADY RENDERED" not in out
    assert "No chart was drawn" in out


def test_chart_and_notice_tags_get_separate_notes():
    out = _redact_chart_text(f"{TAG_A}\n{TAG_NOTICE}")
    assert "[1 chart(s) ALREADY RENDERED to the user. " in out
    assert "series-a" not in out
    assert "2 chart(s)" not in out
    assert "No chart was drawn" in out
    assert "<chart-block" not in out


def test_notice_tag_is_still_queued_for_the_page():
    content = [{"toolResult": {"toolUseId": "t-n", "content": f"readings\n{TAG_NOTICE}"}}]
    agent = _fire(content)
    assert agent._pending_chart_blocks == [TAG_NOTICE]
    assert "No chart was drawn" in content[0]["toolResult"]["content"]


def test_json_part_notice_tag_says_no_chart_was_drawn():
    part = {"json": {"answer": f"readings:\n{TAG_NOTICE}"}}
    content = [{"toolResult": {"toolUseId": "t-nj", "content": [part]}}]
    agent = _fire(content)
    assert agent._pending_chart_blocks == [TAG_NOTICE]
    assert "No chart was drawn" in part["json"]["answer"]
    assert "<chart-block" not in part["json"]["answer"]


# --------------------------------------------------------------------------- #
# drain_pending_charts — content-hash dedup (H2: same title, different window
# ⇒ BOTH charts render; byte-identical re-render ⇒ once)
# --------------------------------------------------------------------------- #
def test_drain_empty_agent_returns_nothing():
    assert drain_pending_charts(SimpleNamespace()) == []


def test_drain_dedups_identical_tags_keeps_distinct_windows():
    agent = SimpleNamespace(_pending_chart_blocks=[TAG_A, TAG_B])
    assert drain_pending_charts(agent) == [TAG_A, TAG_B]
    assert agent._pending_chart_blocks == []  # queue cleared
    # A byte-identical re-render (guaranteed-charts overlap) is skipped, but a
    # SAME-TITLE tag with different data (a different time window)
    # passes: title alone dropped "compare last hour vs yesterday"'s 2nd chart.
    agent._pending_chart_blocks.extend([TAG_A_ALT, TAG_A])
    assert drain_pending_charts(agent) == [TAG_A_ALT]
    agent._pending_chart_blocks.append(
        '<chart-block type="line" title="series-c" labels="[1]" data="[9]"/>')
    fresh = drain_pending_charts(agent)
    assert len(fresh) == 1 and 'title="series-c"' in fresh[0]


def test_drain_untitled_tags_dedup_by_content():
    agent = SimpleNamespace(_pending_chart_blocks=[TAG_UNTITLED])
    assert drain_pending_charts(agent) == [TAG_UNTITLED]
    agent._pending_chart_blocks.append(TAG_UNTITLED)
    assert drain_pending_charts(agent) == []


# --------------------------------------------------------------------------- #
# Local-mode drain (chat_client) — same primitive, token-event seam
# --------------------------------------------------------------------------- #
def test_local_drain_emits_single_token_event_and_dedups():
    from agents import chat_client
    q: queue.Queue = queue.Queue()
    agent = SimpleNamespace(_pending_chart_blocks=[TAG_A, TAG_B])
    chat_client._drain_local_pending_charts(q, agent)
    item = q.get_nowait()
    assert item["event"] == "token"
    assert TAG_A in item["data"]["text"] and TAG_B in item["data"]["text"]
    assert q.empty()
    # Byte-identical re-render is skipped; a same-title different-window tag
    # passes (H2 content-hash dedup).
    agent._pending_chart_blocks.append(TAG_A)
    chat_client._drain_local_pending_charts(q, agent)
    assert q.empty()
    agent._pending_chart_blocks.append(TAG_A_ALT)
    chat_client._drain_local_pending_charts(q, agent)
    assert TAG_A_ALT in q.get_nowait()["data"]["text"]


def test_local_drain_failure_is_swallowed():
    from agents import chat_client

    class _Hostile:
        @property
        def _pending_chart_blocks(self):
            raise RuntimeError("boom")

    q: queue.Queue = queue.Queue()
    chat_client._drain_local_pending_charts(q, _Hostile())  # must not raise
    assert q.empty()


def test_local_drain_runs_only_on_the_non_integrity_branch():
    """Integrity mode (ADR-0046 enforce) never injects into the public answer."""
    from agents import chat_client
    src = inspect.getsource(chat_client._run_local_agent)
    assert src.count("_drain_local_pending_charts") == 1
    # The drain sits in the else-branch, after the integrity emitter.
    integrity_pos = src.index("_emit_local_integrity_result")
    assert src.index("_drain_local_pending_charts") > integrity_pos


# --------------------------------------------------------------------------- #
# Hook registration — PRODUCTION path included (the upstream root-cause)
# --------------------------------------------------------------------------- #
def test_register_hooks_subscribes_message_added():
    from strands.hooks import MessageAddedEvent
    hook = ChartRedactionHook()
    registry = MagicMock()
    hook.register_hooks(registry)
    registry.add_callback.assert_called_once_with(
        MessageAddedEvent, hook._on_message_added)


def test_hook_wired_into_both_agent_constructions():
    from agents import orchestrator
    # ONE shared kwargs helper registers the hook… (later hooks may follow
    # in the same list; these two must lead it)
    assert re.search(r"hooks=\[ChartRedactionHook\(\), CitationOrdinalHook\(\)[,\]]",
                     inspect.getsource(orchestrator._base_agent_kwargs))
    # …and BOTH constructions (production build_orchestrator + the local
    # get_orchestrator singleton) route through it — the upstream root-cause
    # was local-mode-only registration.
    assert "_base_agent_kwargs(" in inspect.getsource(orchestrator.build_orchestrator)
    assert "_base_agent_kwargs(" in inspect.getsource(orchestrator.get_orchestrator)


# --------------------------------------------------------------------------- #
# Regex drift pin: the hook and the summarizer keep private copies of the
# chart-tag regex (importing across them is circular). Byte-equality so the
# redaction seam and the summarizer can never silently drift apart.
# --------------------------------------------------------------------------- #
def test_chart_tag_regex_identical_across_modules():
    from agents.tool_result_summary import _CHART_TAG_RE as summary_re

    assert _CHART_TAG_RE.pattern == summary_re.pattern
    assert _CHART_TAG_RE.flags == summary_re.flags


def test_title_regex_matches_dedup_contract():
    assert _CHART_TITLE_RE.search(TAG_A).group(1) == "series-a"
    assert _CHART_TITLE_RE.search(TAG_UNTITLED) is None


# --------------------------------------------------------------------------- #
# (9) Retirement grep gate: the earlier chat_client injection seam is GONE
# --------------------------------------------------------------------------- #
def test_round1_injection_seam_fully_retired():
    agents_dir = Path(agents.__file__).resolve().parent
    retired = ("charts_injected", "_chart_events", "_extract_chart_blocks",
               "_CHART_BLOCK_RE")
    offenders = []
    for path in sorted(agents_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        offenders += [f"{path.name}:{symbol}" for symbol in retired
                      if symbol in source]
    assert offenders == []
