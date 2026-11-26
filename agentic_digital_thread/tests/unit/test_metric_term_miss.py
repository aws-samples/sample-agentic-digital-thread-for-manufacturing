# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Answer honesty — metric-term miss: disambiguation over proxy.

Fully offline (no AWS, no sockets). Pins two complementary pieces:

1. PROMPT (both modes + both Quick surfaces): the METRIC-TERM MISS rule —
   when the question's TARGET QUANTITY does not resolve in vocabulary, the
   agent asks which definition is meant or states the unresolved term with
   candidates, and NEVER silently substitutes a related metric. The rule is
   shared-section placed (survives the Mode-B Quick scrub) and never names
   the fenced marker (the Mode-B prompt is pinned marker-free). Its ADR-0066 D5
   capture paragraph (Wave 3 Task 3B) is pinned ADDITIVELY: present on every
   surface that carries the rule, with the pre-existing sentences byte-identical
   (an exact prefix) and the tool reference conditional, because the in-process
   capture tool is not exposed to AWS Quick.
2. RUNTIME (``runtime_app._record_metric_term_miss_signal``): the
   deterministic, telemetry-only miss branch — a fenced marker surviving into
   the FINAL answer records exactly ONE (system, term) observation through
   the existing writer (``record_failure_signal`` → threshold → pending
   ``steward_question``; never-auto-accept), mutates nothing, auto-accepts
   nothing, and honors the synthetic/briefing gates.

Fixture literals are placeholder-shaped (neutrality ratchet scans tests).
"""
from __future__ import annotations

import agents.orchestrator as orch
import agents.runtime_app as ra
from agents.workflow_guidance import compose_quick_system_prompt

# The exact fragments the disambiguation contract keys on.
_MISS_RULE_HEADING = "METRIC-TERM MISS — DISAMBIGUATE, NEVER SUBSTITUTE A PROXY"
_MISS_RULE_FRAGMENTS = (
    _MISS_RULE_HEADING,
    "MUST NOT silently substitute the nearest related metric",
    "ask ONE clarifying question that names the unresolved term",
    "list the\n    nearest candidates as OPTIONS",
    "a disclosed proxy is still the wrong\nnumber",
)

# ADR-0066 D5 (Wave 3 Task 3B) — the ADDITIVE capture paragraph appended to the
# same rule: after the user answers with a definition, capture it, say it is
# pending steward review, and answer from NOTHING until a steward accepts it.
_CAPTURE_RULE_HEADING = (
    "CAPTURE THE USER'S OWN DEFINITION — FILED PENDING, NEVER APPLIED BY YOU")
_CAPTURE_RULE_FRAGMENTS = (
    _CAPTURE_RULE_HEADING,
    "if you have a `propose_metric_definition` tool",
    "NEVER pass prose as a\nformula",
    "pending steward review",
    "MUST NOT use it to compute, estimate, or\npresent a number",
    "not in this turn and not in any later turn",
)

# The rule text as it stood BEFORE the capture paragraph, byte-exact. Task 3B is
# ADDITIVE ONLY: the disambiguation contract above may not drift by a single
# character, so the appended paragraph must leave this an exact PREFIX.
_MISS_RULE_TEXT_BEFORE_CAPTURE = (
    "\n\nMETRIC-TERM MISS — DISAMBIGUATE, NEVER SUBSTITUTE A PROXY:\n"
    "When the question's TARGET QUANTITY — the metric/KPI-shaped term naming the thing\n"
    "being measured (a rate, ratio, count, score, or efficiency term) — does NOT resolve\n"
    "through the vocabulary/graph lookup (no matching Concept, Field mapping, or metric\n"
    "definition), you MUST NOT silently substitute the nearest related metric and present\n"
    "it as the answer. Do ONE of:\n"
    "(a) ask ONE clarifying question that names the unresolved term and lists the\n"
    "    candidate interpretations you found, or\n"
    "(b) answer what you CAN from the terms that DID resolve, state explicitly that the\n"
    "    target term is unresolved in the connected systems' vocabulary, and list the\n"
    "    nearest candidates as OPTIONS for the user to choose between.\n"
    "This is STRICTER than the row-identifier recovery's disclosure rule: a row identifier\n"
    "with a confirmed graph translation may be substituted WITH disclosure; a metric term\n"
    "with NO confirmed mapping is never substituted — a disclosed proxy is still the wrong\n"
    "number."
)


# --------------------------------------------------------------------------- #
# 1. Prompt pins — both modes, both Quick surfaces
# --------------------------------------------------------------------------- #
def test_metric_term_miss_rule_present_in_both_mode_prompts():
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        for fragment in _MISS_RULE_FRAGMENTS:
            assert fragment in prompt, (mode, fragment)


def test_metric_term_miss_rule_survives_both_quick_prompts():
    for mode in ("agent", "orchestrator"):
        quick = compose_quick_system_prompt(mode)
        for fragment in _MISS_RULE_FRAGMENTS:
            assert fragment in quick, (mode, fragment)


def test_rule_is_shared_section_not_in_the_delegate_block():
    # Inside _MODE_A_DELEGATE_BLOCK the Mode-B Quick replace() scrub would
    # silently drop the rule — it must live in a shared fragment.
    assert _MISS_RULE_HEADING not in orch._MODE_A_DELEGATE_BLOCK


def test_rule_never_names_the_fenced_marker():
    # test_orchestrator_identifier_miss_prompt.py pins the Mode-B prompt
    # marker-free; the shared rule must therefore never say its name.
    assert "IDENTIFIER-MISS" not in orch._METRIC_TERM_MISS_RULE
    assert "IDENTIFIER-MISS" not in orch._build_orchestrator_prompt("orchestrator")


# --------------------------------------------------------------------------- #
# 1b. ADR-0066 D5 capture paragraph — ADDITIVE: present on every surface that
#     carries the rule, with the pre-existing sentences BYTE-UNCHANGED.
# --------------------------------------------------------------------------- #
def test_pre_existing_rule_text_is_byte_identical_and_a_prefix():
    assert orch._METRIC_TERM_MISS_RULE.startswith(_MISS_RULE_TEXT_BEFORE_CAPTURE)
    assert orch._METRIC_TERM_MISS_RULE == (
        _MISS_RULE_TEXT_BEFORE_CAPTURE + orch._METRIC_TERM_CAPTURE_RULE
        + orch._NO_EXAMPLE_NUMBERS_RULE)


def test_capture_paragraph_present_in_both_mode_prompts():
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        for fragment in _CAPTURE_RULE_FRAGMENTS:
            assert fragment in prompt, (mode, fragment)


def test_capture_paragraph_survives_both_quick_prompts():
    for mode in ("agent", "orchestrator"):
        quick = compose_quick_system_prompt(mode)
        for fragment in _CAPTURE_RULE_FRAGMENTS:
            assert fragment in quick, (mode, fragment)


def test_capture_paragraph_tool_reference_stays_conditional():
    # The in-process capture tool is NOT exposed to AWS Quick, so the
    # instruction is conditional ("if you have") — the same phrasing the
    # ADR-0048 recovery text uses for save_value_mapping. An unconditional
    # instruction would tell Quick to call a tool it does not have.
    assert "if you have a `propose_metric_definition` tool" in (
        orch._METRIC_TERM_CAPTURE_RULE)
    assert "IDENTIFIER-MISS" not in orch._METRIC_TERM_CAPTURE_RULE


def test_capture_paragraph_is_shared_section_not_in_the_delegate_block():
    assert _CAPTURE_RULE_HEADING not in orch._MODE_A_DELEGATE_BLOCK


# --------------------------------------------------------------------------- #
# 2. Runtime miss branch — fixtures
# --------------------------------------------------------------------------- #
_TOKEN = "DPX-RATE-77"  # nosec B105 - a metric-term marker, not a credential
_MISS_BLOCK = (
    "```\n"
    "IDENTIFIER-MISS:\n"
    f"token: {_TOKEN}\n"
    "looked_in: metrics.metric_code\n"
    "my_format: MTR-<4 digits>, e.g. MTR-1001, MTR-1002\n"
    "```"
)


def _turn(answer_has_marker=True, result_has_marker=True, token=_TOKEN):
    tool_calls_log = [
        {"toolUseId": "tid-0", "name": "explore_graph"},
        {"toolUseId": "tid-1", "name": "sys-a___query_sys_a"},
    ]
    result_block = _MISS_BLOCK if token == _TOKEN else _MISS_BLOCK.replace(_TOKEN, token)
    tool_results = {
        "tid-0": "graph result",
        "tid-1": (f"{result_block}\nNot my vocabulary."
                  if result_has_marker else "rows: none"),
    }
    response_text = (
        [f"I could not resolve that term.\n{_MISS_BLOCK}\n", "Candidates: a, b."]
        if answer_has_marker else ["All resolved fine."]
    )
    return tool_calls_log, tool_results, response_text


class _Writer:
    def __init__(self, ret=True):
        self.calls: list[tuple] = []
        self.ret = ret

    def __call__(self, shape, system_id, subject, turn_id, evidence_ref=""):
        self.calls.append((shape, system_id, subject, turn_id))
        return self.ret


def _tripwire(*_a, **_k):
    raise AssertionError("proposal writer must NOT be called by the miss branch")


def test_surviving_marker_records_exactly_one_observation(monkeypatch):
    writer = _Writer()
    monkeypatch.setattr("agents.failure_signals.record_failure_signal", writer)
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping", _tripwire)
    calls, results, response = _turn()
    before = list(response)
    counted = ra._record_metric_term_miss_signal(calls, results, response, "q", "sess-1")
    assert counted is True
    # Exactly ONE observation, same (shape, system, subject) grammar and the
    # SAME turn_id (the carrying toolUseId) as the orchestrator-side hook —
    # the accumulator's turn_ids dedup collapses both into one count.
    assert writer.calls == [
        ("terminology_miss", "sys-a", {"term": _TOKEN.lower()}, "tid-1")]
    # Marker survival: the branch is telemetry-only — the answer is untouched.
    assert response == before
    assert _MISS_BLOCK in "".join(response)


def test_observation_semantics_are_never_auto_accept():
    # Lifecycle pin: the shape is threshold-gated (an observation, not an
    # instant proposal), and what the sweep eventually files is a
    # steward_question — a never-auto-accept proposal type.
    import inspect

    from agents import failure_signals, self_learning
    assert "terminology_miss" in failure_signals.THRESHOLDS
    assert "steward_question" in self_learning._NEVER_AUTO_ACCEPT_TYPES
    src = inspect.getsource(ra._record_metric_term_miss_signal)
    assert "record_failure_signal" in src          # the ONE writer
    assert "log_proposed_mapping" not in src       # never files directly
    assert "accept(" not in src                    # nothing auto-accepted


def test_no_marker_in_answer_writes_nothing(monkeypatch):
    writer = _Writer()
    monkeypatch.setattr("agents.failure_signals.record_failure_signal", writer)
    calls, results, response = _turn(answer_has_marker=False)
    assert ra._record_metric_term_miss_signal(calls, results, response, "q", "s") is False
    assert writer.calls == []


def test_unattributable_marker_writes_nothing(monkeypatch):
    # Answer carries the marker but NO tool result carried the same token —
    # deterministic attribution or nothing.
    writer = _Writer()
    monkeypatch.setattr("agents.failure_signals.record_failure_signal", writer)
    calls, results, response = _turn(result_has_marker=False)
    assert ra._record_metric_term_miss_signal(calls, results, response, "q", "s") is False
    calls2, results2, response2 = _turn(token="OTHER-TOKEN-99")  # nosec B106 - a metric-term marker, not a credential
    assert ra._record_metric_term_miss_signal(calls2, results2, response2, "q", "s") is False
    assert writer.calls == []


def test_synthetic_and_briefing_gates_suppress_the_write(monkeypatch):
    writer = _Writer()
    monkeypatch.setattr("agents.failure_signals.record_failure_signal", writer)
    calls, results, response = _turn()
    for session in ("tryit-1", "feedback-explore-2", "eval-run-3"):
        assert ra._record_metric_term_miss_signal(
            calls, results, response, "q", session) is False
    assert ra._record_metric_term_miss_signal(
        calls, results, response, "Morning Briefing please", "sess-1") is False
    assert writer.calls == []


def test_branch_is_wired_into_post_turn_learning_checks():
    import inspect

    src = inspect.getsource(ra._post_turn_learning_checks)
    assert "_record_metric_term_miss_signal" in src


# --------------------------------------------------------------------------- #
# 3. Persistence bounding — the term is bounded BEFORE digest + write
# --------------------------------------------------------------------------- #
def _turn_with_token(token: str):
    """A turn whose ANSWER and TOOL RESULT both carry the SAME miss token."""
    block = (
        "```\n"
        "IDENTIFIER-MISS:\n"
        f"token: {token}\n"
        "looked_in: metrics.metric_code\n"
        "```"
    )
    tool_calls_log = [{"toolUseId": "tid-1", "name": "sys-a___query_sys_a"}]
    tool_results = {"tid-1": f"{block}\nNot my vocabulary."}
    response_text = [f"I could not resolve that term.\n{block}\n"]
    return tool_calls_log, tool_results, response_text


def test_overlong_term_is_capped_before_persistence(monkeypatch):
    writer = _Writer()
    monkeypatch.setattr("agents.failure_signals.record_failure_signal", writer)
    calls, results, response = _turn_with_token("X" * 500)
    assert ra._record_metric_term_miss_signal(
        calls, results, response, "q", "sess-1") is True
    assert ra._MISS_TERM_MAX_LEN == 120
    assert writer.calls == [
        ("terminology_miss", "sys-a", {"term": "x" * 120}, "tid-1")]


def test_control_character_term_writes_nothing(monkeypatch):
    writer = _Writer()
    monkeypatch.setattr("agents.failure_signals.record_failure_signal", writer)
    for hostile in ("DPX-\x07-77", "DPX-\x1b[31m-77", "tab\tinside"):
        calls, results, response = _turn_with_token(hostile)
        assert ra._record_metric_term_miss_signal(
            calls, results, response, "q", "sess-1") is False, hostile
    assert writer.calls == []


def test_bounded_miss_term_helper_contract():
    # Single-line, printable, capped — reject (never normalize) on violation.
    assert ra._bounded_miss_term("  DPX-Rate-77 ") == "dpx-rate-77"
    assert ra._bounded_miss_term("a" * 500) == "a" * 120
    assert ra._bounded_miss_term("line1\nline2") == ""
    assert ra._bounded_miss_term("carriage\rreturn") == ""
    assert ra._bounded_miss_term("bell\x07term") == ""
    assert ra._bounded_miss_term("") == ""
    assert ra._bounded_miss_term(None) == ""
    assert ra._bounded_miss_term(42) == ""


# --------------------------------------------------------------------------- #
# 1c. ADR-0078 D5/D6 — the metric-layer RESULT rule: a SEPARATE constant
#     appended at the same anchor, so every pin above stays byte-identical.
# --------------------------------------------------------------------------- #
_LAYER_RULE_HEADING = (
    "A DEFINED METRIC IS ANSWERED FROM THE METRIC LAYER — NEVER RE-DERIVED BY YOU")
_LAYER_RULE_FRAGMENTS = (
    _LAYER_RULE_HEADING,
    "[METRIC LAYER — DETERMINISTIC RESULT]",
    "never recompute it from\nraw columns or another metric",
    "give NO number for that metric",
    "not even labelled as approximate or \"for illustration\"",
    "the block is the warning",
)


def test_metric_layer_rule_present_in_both_mode_prompts():
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        for fragment in _LAYER_RULE_FRAGMENTS:
            assert fragment in prompt, (mode, fragment)


def test_metric_layer_rule_is_left_out_of_both_quick_prompts():
    """The metric-layer block is written into the orchestrator's own turn by
    its runtime; a Quick client on the query gateway never receives one, so
    its prompts leave the rule out (``workflow_guidance.
    _QUICK_ORCHESTRATOR_ONLY_RULES``). The miss rule, which governs a term
    with no definition, stays on every surface (pinned above)."""
    from agents.metric_turn_notice import METRIC_LAYER_HEADER
    for mode in ("agent", "orchestrator"):
        quick = compose_quick_system_prompt(mode)
        assert _LAYER_RULE_HEADING not in quick, mode
        assert METRIC_LAYER_HEADER not in quick, mode
        assert orch._METRIC_TERM_MISS_RULE in quick, mode


def test_metric_layer_rule_follows_capture_paragraph_and_leaves_pins_untouched():
    assert orch._METRIC_TERM_MISS_RULE == (
        _MISS_RULE_TEXT_BEFORE_CAPTURE + orch._METRIC_TERM_CAPTURE_RULE
        + orch._NO_EXAMPLE_NUMBERS_RULE)
    assert _LAYER_RULE_HEADING not in orch._METRIC_TERM_MISS_RULE
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        assert (orch._METRIC_TERM_MISS_RULE + orch._METRIC_LAYER_RESULT_RULE) in prompt


def test_metric_layer_rule_never_names_the_fenced_marker():
    assert "IDENTIFIER-MISS" not in orch._METRIC_LAYER_RESULT_RULE


def test_metric_layer_rule_is_shared_section_not_in_the_delegate_block():
    assert _LAYER_RULE_HEADING not in orch._MODE_A_DELEGATE_BLOCK


def test_metric_layer_rule_names_the_header_the_runtime_renders():
    from agents.metric_turn_notice import METRIC_LAYER_HEADER
    assert METRIC_LAYER_HEADER in orch._METRIC_LAYER_RESULT_RULE


def test_metric_layer_rule_forbids_illustrative_substitutes():
    rule = orch._METRIC_LAYER_RESULT_RULE
    for fragment in ("give NO number", "\"for illustration\"", "never recompute it",
                     "do not compute, estimate, or\nillustrate a substitute"):
        assert fragment in rule, fragment


# --------------------------------------------------------------------------- #
# 2026-09-27 — no example numbers for a quantity the agent is not reporting
#     (measured: "(e.g. TP90 <= 180 seconds)" in a clarifying question and a
#     scale factor inside an honest withhold both read as data claims).
#     APPENDED after the capture paragraph; every prior byte is a prefix.
# --------------------------------------------------------------------------- #
def test_no_example_numbers_rule_is_appended_and_reaches_every_surface():
    rule = orch._NO_EXAMPLE_NUMBERS_RULE
    assert rule.startswith("\nNO EXAMPLE NUMBERS FOR A QUANTITY YOU ARE NOT REPORTING:")
    assert "no example, placeholder, illustrative" in rule
    assert "no formula with constants, no scale factor" in rule
    assert orch._METRIC_TERM_MISS_RULE.endswith(rule)
    assert orch._METRIC_TERM_MISS_RULE.startswith(
        _MISS_RULE_TEXT_BEFORE_CAPTURE + orch._METRIC_TERM_CAPTURE_RULE)
    for mode in ("agent", "orchestrator"):
        assert rule in orch._build_orchestrator_prompt(mode)
        assert rule in compose_quick_system_prompt(mode)
