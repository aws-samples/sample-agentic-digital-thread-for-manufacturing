# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure investigation-trace capture module (Wave 6, Task A1 — W6-F2/F3 pins).

Pins the ONE caps/bounding home ``agents/investigation_trace.py``:

* per-field byte caps hold on hostile oversize inputs (UTF-8 clean-boundary
  — never a split codepoint), ``*_truncated`` present ONLY when true,
  ``*_bytes`` always the honest full size;
* the step cap drops OLDEST (last 40 kept, ``steps_dropped`` counted, ``seq``
  gap visible);
* the 64 KB whole-entry ratchet (oldest-dropped, then answer/question
  hard-truncation with flags — never raises, never a silent drop);
* reasoning extraction is toolUseId-anchored (second toolUse of the same
  message ⇒ '', unknown id ⇒ '', interleaved concurrent histories never
  mis-attribute);
* the serializer strips ``_adt_*`` reserved keys (structural leak pin);
* import purity: stdlib only (the ``provenance.py`` posture);
* the locally-spelled metadata bounds mirror ``agents.provenance`` exactly.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import agents.investigation_trace as it

# --------------------------------------------------------------------------- #
# _bounded_utf8 — clean-boundary caps, honest flags/sizes
# --------------------------------------------------------------------------- #

def test_bounded_utf8_under_limit_passes_through():
    text, truncated, total = it._bounded_utf8("hello", 100)
    assert (text, truncated, total) == ("hello", False, 5)


def test_bounded_utf8_truncates_at_clean_codepoint_boundary():
    # '€' is 3 UTF-8 bytes; a 4-byte limit would split the second one.
    text, truncated, total = it._bounded_utf8("€€", 4)
    assert text == "€"                    # split codepoint dropped, not broken
    assert truncated is True
    assert total == 6
    text.encode("utf-8")                  # still valid UTF-8


def test_bounded_utf8_non_string_coerces():
    assert it._bounded_utf8(None, 10) == ("", False, 0)
    text, truncated, total = it._bounded_utf8(12345, 3)
    assert text == "123" and truncated and total == 5


# --------------------------------------------------------------------------- #
# serialize_tool_payload — compact, defensive, reserved-key strip (leak pin)
# --------------------------------------------------------------------------- #

def test_serializer_strings_pass_through():
    assert it.serialize_tool_payload("SELECT 1") == "SELECT 1"


def test_serializer_strips_adt_reserved_keys():
    out = it.serialize_tool_payload({
        "question": "q", "_adt_provenance": {"session_id": "s"},
        "_adt_identity": {"sub": "u"}, it.TRACE_STATE_KEY: ["x"],
    })
    assert "_adt_provenance" not in out
    assert "_adt_identity" not in out
    assert it.TRACE_STATE_KEY not in out
    assert json.loads(out) == {"question": "q"}


def test_serializer_never_raises_on_exotic_types():
    assert isinstance(it.serialize_tool_payload({"x": object()}), str)
    assert it.serialize_tool_payload(None) == "null"


# --------------------------------------------------------------------------- #
# record_step_start / _pop_pending — Before→After timing
# --------------------------------------------------------------------------- #

def test_step_start_then_record_yields_duration_and_started_at():
    state: dict = {}
    it.record_step_start(state, {"toolUseId": "t-1"})
    it.record_step(state, tool_use={"toolUseId": "t-1", "name": "execute_sql",
                                    "input": {"sql": "SELECT 1"}},
                   result={"status": "success", "content": [{"text": "ok"}]},
                   exception=None, retry=False, messages=[])
    (step,), dropped = it.collect_trace_steps(state)
    assert dropped == 0
    assert step["duration_ms"] is not None and step["duration_ms"] >= 0
    assert step["started_at"].endswith("Z")
    assert step["status"] == "success"


def test_missing_before_stamp_records_honest_null_duration():
    state: dict = {}
    it.record_step(state, tool_use={"toolUseId": "t-x", "name": "t"},
                   result={"status": "success"}, exception=None, retry=False,
                   messages=[])
    (step,), _ = it.collect_trace_steps(state)
    assert step["duration_ms"] is None
    assert step["started_at"].endswith("Z")


def test_pending_map_is_bounded():
    state: dict = {}
    for i in range(it._MAX_PENDING_STARTS + 10):
        it.record_step_start(state, {"toolUseId": f"t-{i}"})
    assert len(state[it._TRACE_PENDING_KEY]) == it._MAX_PENDING_STARTS


def test_bad_tool_use_id_never_stamps():
    state: dict = {}
    it.record_step_start(state, {"toolUseId": ""})
    it.record_step_start(state, {})
    it.record_step_start(state, None)
    assert state.get(it._TRACE_PENDING_KEY) in (None, {})


# --------------------------------------------------------------------------- #
# record_step — field caps, flags, statuses, retry, step-cap drop
# --------------------------------------------------------------------------- #

def _record(state: dict, i: int, **overrides) -> None:
    kwargs = dict(
        tool_use={"toolUseId": f"t-{i}", "name": "execute_sql",
                  "input": {"sql": f"SELECT {i}"}},
        result={"status": "success", "content": [{"text": f"row {i}"}]},
        exception=None, retry=False, messages=[])
    kwargs.update(overrides)
    it.record_step(state, **kwargs)


def test_oversize_input_output_capped_with_honest_flags_and_bytes():
    state: dict = {}
    big_in = "x" * (it.MAX_STEP_INPUT_BYTES * 2)
    big_out = "y" * (it.MAX_STEP_OUTPUT_BYTES * 3)
    _record(state, 1,
            tool_use={"toolUseId": "t-1", "name": "t", "input": {"sql": big_in}},
            result={"status": "success", "content": big_out})
    (step,), _ = it.collect_trace_steps(state)
    assert len(step["tool_input"].encode("utf-8")) <= it.MAX_STEP_INPUT_BYTES
    assert step["input_truncated"] is True
    assert step["input_bytes"] == len(
        it.serialize_tool_payload({"sql": big_in}).encode("utf-8"))
    assert len(step["tool_output"].encode("utf-8")) <= it.MAX_STEP_OUTPUT_BYTES
    assert step["output_truncated"] is True
    assert step["output_bytes"] == len(big_out.encode("utf-8"))


def test_truncated_flags_absent_when_not_truncated():
    state: dict = {}
    _record(state, 1)
    (step,), _ = it.collect_trace_steps(state)
    for flag in ("input_truncated", "output_truncated", "reasoning_truncated"):
        assert flag not in step
    assert step["input_bytes"] > 0 and step["output_bytes"] > 0


def test_multibyte_payload_truncation_stays_valid_utf8():
    state: dict = {}
    _record(state, 1, result={"status": "success",
                              "content": "€" * it.MAX_STEP_OUTPUT_BYTES})
    (step,), _ = it.collect_trace_steps(state)
    step["tool_output"].encode("utf-8")   # no split codepoint
    assert step["output_truncated"] is True


def test_exception_step_records_exception_status_and_empty_output():
    state: dict = {}
    _record(state, 1, exception=RuntimeError("boom"))
    (step,), _ = it.collect_trace_steps(state)
    assert step["status"] == "exception"
    assert step["tool_output"] == "" and step["output_bytes"] == 0
    assert "boom" not in json.dumps(step)   # exception text never captured


def test_retry_true_records_nothing():
    state: dict = {}
    _record(state, 1, retry=True)
    steps, dropped = it.collect_trace_steps(state)
    assert steps == [] and dropped == 0


def test_status_normalization():
    for raw, expect in (("success", "success"), ("error", "error"),
                        ("cancelled", "cancelled"), ("weird", "unknown"),
                        (None, "unknown")):
        state: dict = {}
        _record(state, 1, result={"status": raw, "content": "x"})
        (step,), _ = it.collect_trace_steps(state)
        assert step["status"] == expect
        assert step["status"] in it.TRACE_STATUSES


def test_41_steps_keep_last_40_with_visible_seq_gap():
    state: dict = {}
    for i in range(1, 42):
        _record(state, i)
    steps, dropped = it.collect_trace_steps(state)
    assert len(steps) == it.MAX_TRACE_STEPS == 40
    assert dropped == 1
    assert steps[0]["seq"] == 2            # the gap shows the drop honestly
    assert steps[-1]["seq"] == 41


# --------------------------------------------------------------------------- #
# extract_reasoning — toolUseId anchor, dedup, honest-empty
# --------------------------------------------------------------------------- #

def _assistant_msg(text_blocks: list[str], tool_use_ids: list[str]) -> dict:
    content: list = [{"text": t} for t in text_blocks]
    content += [{"toolUse": {"toolUseId": tid, "name": "t", "input": {}}}
                for tid in tool_use_ids]
    return {"role": "assistant", "content": content}


def test_reasoning_found_for_anchored_message():
    messages = [
        {"role": "user", "content": [{"text": "question"}]},
        _assistant_msg(["Checking orders first."], ["t-1"]),
    ]
    assert it.extract_reasoning(messages, "t-1", set()) == "Checking orders first."


def test_second_tool_use_of_same_message_is_honest_empty():
    messages = [_assistant_msg(["One thought."], ["t-1", "t-2"])]
    seen: set = set()
    assert it.extract_reasoning(messages, "t-1", seen) == "One thought."
    assert it.extract_reasoning(messages, "t-2", seen) == ""


def test_already_attributed_anchor_is_empty_on_second_call():
    messages = [_assistant_msg(["Once."], ["t-1"])]
    seen: set = set()
    assert it.extract_reasoning(messages, "t-1", seen) == "Once."
    assert it.extract_reasoning(messages, "t-1", seen) == ""


def test_unknown_tool_use_id_is_honest_empty():
    messages = [_assistant_msg(["Text."], ["t-1"])]
    assert it.extract_reasoning(messages, "t-nope", set()) == ""
    assert it.extract_reasoning([], "t-1", set()) == ""
    assert it.extract_reasoning(None, "", set()) == ""


def test_interleaved_concurrent_histories_never_misattribute():
    """Two invocations' messages interleaved (the shared-singleton hazard):
    each toolUseId resolves to ITS OWN message's text, scanning from the end."""
    messages = [
        _assistant_msg(["Invocation A step one."], ["a-1"]),
        _assistant_msg(["Invocation B step one."], ["b-1"]),
        _assistant_msg(["Invocation A step two."], ["a-2"]),
        _assistant_msg(["Invocation B step two."], ["b-2"]),
    ]
    assert it.extract_reasoning(messages, "a-2", set()) == "Invocation A step two."
    assert it.extract_reasoning(messages, "b-1", set()) == "Invocation B step one."


def test_reasoning_bounded_in_recorded_step_with_flag():
    state: dict = {}
    long_text = "r" * (it.MAX_REASONING_BYTES * 2)
    _record(state, 1, messages=[_assistant_msg([long_text], ["t-1"])])
    (step,), _ = it.collect_trace_steps(state)
    assert len(step["reasoning"].encode("utf-8")) <= it.MAX_REASONING_BYTES
    assert step["reasoning_truncated"] is True


# --------------------------------------------------------------------------- #
# build_trace_entry — shape, caps, the 64 KB ratchet
# --------------------------------------------------------------------------- #

def _step(seq: int, out_bytes: int = 16) -> dict:
    return {"seq": seq, "reasoning": "", "tool_name": "t",
            "tool_input": "{}", "input_bytes": 2,
            "tool_output": "z" * out_bytes, "output_bytes": out_bytes,
            "status": "success", "duration_ms": 5,
            "started_at": "2026-08-04T18:00:00Z"}


def _entry(**overrides) -> dict:
    kwargs = dict(session_id="sid-1", query_ref="q-0123456789ab",
                  system_id="erp", question="How many open orders?",
                  kg_context_attached=False, steps=[_step(1)],
                  steps_dropped=0, final_answer="Seven.")
    kwargs.update(overrides)
    return it.build_trace_entry(**kwargs)


def test_entry_shape_and_defaults():
    entry = _entry()
    assert entry["origin"] == "system-agent"
    assert entry["query_ref"] == "q-0123456789ab"
    assert entry["session_id"] == "sid-1"
    assert entry["kg_context_attached"] is False
    assert entry["steps_dropped"] == 0                # always present (honest)
    assert entry["final_answer_bytes"] == len("Seven.")
    assert "question_truncated" not in entry
    assert "final_answer_truncated" not in entry
    assert entry["captured_at"].endswith("Z")


def test_question_and_answer_caps_with_flags():
    entry = _entry(question="q" * (it.MAX_QUESTION_BYTES + 100),
                   final_answer="a" * (it.MAX_FINAL_ANSWER_BYTES + 100))
    assert len(entry["question"].encode()) <= it.MAX_QUESTION_BYTES
    assert entry["question_truncated"] is True
    assert len(entry["final_answer"].encode()) <= it.MAX_FINAL_ANSWER_BYTES
    assert entry["final_answer_truncated"] is True
    assert entry["final_answer_bytes"] == it.MAX_FINAL_ANSWER_BYTES + 100


def test_64kb_ratchet_drops_oldest_steps_and_counts():
    steps = [_step(i, out_bytes=it.MAX_STEP_OUTPUT_BYTES)
             for i in range(1, it.MAX_TRACE_STEPS + 1)]     # ~160 KB of steps
    entry = _entry(steps=steps)
    assert it._entry_bytes(entry) <= it.MAX_TRACE_ENTRY_BYTES
    assert entry["steps_dropped"] > 0
    assert entry["steps"], "some steps must survive"
    # survivors are the NEWEST (oldest dropped)
    assert entry["steps"][-1]["seq"] == it.MAX_TRACE_STEPS
    assert entry["steps"][0]["seq"] == entry["steps_dropped"] + 1


def test_zero_step_overcap_truncates_answer_then_question_never_raises():
    entry = _entry(steps=[],
                   final_answer="a" * (it.MAX_TRACE_ENTRY_BYTES * 2),
                   question="q" * (it.MAX_TRACE_ENTRY_BYTES * 2))
    assert it._entry_bytes(entry) <= it.MAX_TRACE_ENTRY_BYTES
    assert entry["final_answer_truncated"] is True
    assert entry["question_truncated"] is True


def test_collect_is_read_only_copies():
    state: dict = {}
    _record(state, 1)
    steps, _ = it.collect_trace_steps(state)
    steps[0]["tool_output"] = "mutated"
    (fresh,), _ = it.collect_trace_steps(state)
    assert fresh["tool_output"] != "mutated"


def test_collect_on_foreign_state_is_honest_empty():
    assert it.collect_trace_steps({}) == ([], 0)
    assert it.collect_trace_steps(None) == ([], 0)
    assert it.collect_trace_steps({it.TRACE_STATE_KEY: "junk"}) == ([], 0)


# --------------------------------------------------------------------------- #
# Import purity + mirror-constant pins
# --------------------------------------------------------------------------- #

def test_module_imports_stdlib_only():
    """The provenance.py posture: no boto3, no strands, no intra-package
    imports — the store imports THIS module, never the reverse."""
    source = Path(it.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    allowed = {"__future__", "json", "time", "datetime", "typing"}
    assert imported <= allowed, f"non-stdlib imports: {imported - allowed}"
    for name in imported - {"__future__", "typing"}:
        assert name in sys.stdlib_module_names


def test_local_bounds_mirror_provenance_home():
    """The locally-spelled metadata bounds (import-purity concession) must
    equal the provenance home values — drift breaks here."""
    from agents.provenance import MAX_SYSTEM_ID_LEN, MAX_TOOL_NAME_LEN
    assert it.MAX_TRACE_SYSTEM_ID_LEN == MAX_SYSTEM_ID_LEN
    assert it.MAX_TRACE_TOOL_NAME_LEN == MAX_TOOL_NAME_LEN
