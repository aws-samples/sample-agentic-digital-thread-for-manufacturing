# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Feedback-exploration write hygiene (Demo scan, 2026-08-05).

Two measured defects, both pinned here:

(A) An exploration run made the ORCHESTRATOR file side-effect value_mapping
    proposals (13 rows, all concept=unknown — the rows tripping audit probe
    5). An exploration is a synthetic ANALYSIS run: it files exactly its ONE
    corroborated insight (``feedback_exploration._file_proposal``) and
    nothing else. The no-learning signal is the SESSION-ID PREFIX
    (``feedback_exploration.SESSION_PREFIX`` →
    ``runtime_app._SYNTHETIC_SESSION_PREFIXES``) — the same transport class
    ``write_memory=False`` rides (a chat-client kwarg landing in the runtime
    payload), recognized by the ONE shared gate ``_skip_post_turn_capture``
    in BOTH the legacy finally-block and the integrity mirror.

(B) ``runtime_app._ingest_value_mapping_proposals`` stored the composed
    prompt as ``query_context`` — during exploration that embeds the fenced
    untrusted user comment (13 Demo rows stored the fence header + origin
    label; a ~20-char-shorter question would have stored real comment text).
    Now: the stored context is the USER QUESTION (``rawPrompt``), and
    ``_query_context_for_storage`` guarantees a fenced untrusted block is
    NEVER storable. Safe for dedup: the value_mapping canonical key digests
    the six endpoint slots only (``proposal_key._DISPATCH``), never
    ``query_context``.

Named mutations each pin kills:

* FX-A1 — remove ``"feedback-explore-"`` from
  ``_SYNTHETIC_SESSION_PREFIXES`` (or the prefix check itself): the
  side-effect proposals return on both invocation branches.
* FX-A2 — revert the waste-analysis persist gate to ``_is_tryit_session``.
* FX-A3 — suppress the exploration's OWN insight along with the side
  effects (e.g. gate ``_file_proposal``/``_resolve_outcome`` on the
  synthetic session).
* FX-A4 — drift either side of the prefix cross-pin
  (``feedback_exploration.SESSION_PREFIX`` vs the runtime gate).
* FX-B1 — revert ``query_context`` to the composed ``prompt[:500]``: the
  fence-never-stored pin fails (marker AND comment text land inside 500).
* FX-B2 — revert legacy ``s.raw_prompt`` to the app-enriched payload prompt.
* FX-B3 — drop the ``question`` threading on the integrity mirror.

Offline: no AWS, no network — every writer seam is a recorder (the
``test_tryit_containment`` idiom); the exploration worker runs INLINE.

Local-branch note (verified 2026-08-05): ``LocalChatClient`` runs the
orchestrator in-process and has NO value-mapping post-pass — only
``runtime_app`` parses VALUE-MAPPING-PROPOSAL blocks — so the local branch
cannot file side-effect proposals today. The pins here still close the local
leg: the prefixed session id reaches ``LocalChatClient`` verbatim, so if a
local post-pass is ever added through the shared gate, suppression follows.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agents.artifact_usage as artifact_usage
import agents.feedback_exploration as fx
from agents import runtime_app, self_learning
from agents.events import DoneEvent, MessageEvent, MetadataEvent, ToolUseEvent
from agents.job_store import JobStore
from agents.untrusted_text import FENCE_CLOSE, FENCE_OPEN

_COMMENT = "COMMENT_MARKER_x9y8z7 the totals looked wrong to me"
_QUESTION = "which lots were affected?"

_RESPONSE_WITH_PROPOSALS = (
    "Answer prose.\n"
    "---CORRELATION-PROPOSAL---\n"
    "Pattern: Machine faults in CMMS correlate with scrap spikes in ERP\n"
    "Confidence: HIGH\n"
    "---END-PROPOSAL---\n"
    "---VALUE-MAPPING-PROPOSAL---\n"
    "from_system: cmms\n"
    "from_field: assets.asset_ref\n"
    "from_value: A-77\n"
    "to_system: erp\n"
    "to_field: equipment.equipment_no\n"
    "to_value: 4400077\n"
    "concept: equipment-reference\n"
    "confidence: high\n"
    "method: value_format_match\n"
    "---END-PROPOSAL---\n"
)

_VM_BLOCK = (
    "from_system: cmms\n"
    "from_field: assets.asset_ref\n"
    "from_value: A-77\n"
    "to_system: erp\n"
    "to_field: equipment.equipment_no\n"
    "to_value: 4400077\n"
    "concept: equipment-reference\n"
    "confidence: high\n"
    "method: foreign_key_match\n"
)

_CATALOG = [{"id": "erp", "name": "ERP"}, {"id": "cmms", "name": "CMMS"}]


class _RecordingTable:
    def __init__(self):
        self.writes: list = []

    def put_item(self, **kwargs):
        self.writes.append(("put_item", kwargs))
        return {}

    def update_item(self, **kwargs):
        self.writes.append(("update_item", kwargs))
        return {}

    def query(self, **_kwargs):
        return {"Items": []}

    def scan(self, **_kwargs):
        return {"Items": []}

    def get_item(self, **_kwargs):
        return {}


def _arm_write_recorders(monkeypatch):
    """Recorders on EVERY DynamoDB-backed writer seam + the table itself
    (the ``test_tryit_containment`` idiom)."""
    writes: list = []
    table = _RecordingTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(
        self_learning, "log_proposed_mapping",
        lambda **k: writes.append(("proposal", k)) or f"m-{len(writes)}")
    monkeypatch.setattr(
        self_learning, "record_rule_usage",
        lambda *a, **k: writes.append(("rule-bump", None)) or 1)
    monkeypatch.setattr(
        self_learning, "append_query_guidance_note",
        lambda *a, **k: writes.append(("guidance", None)) or True,
        raising=False)
    monkeypatch.setattr(
        artifact_usage, "bump_artifact_usage",
        lambda refs, evidence="": writes.append(("ledger", None)) or 1)
    monkeypatch.setattr(self_learning, "known_systems", lambda: _CATALOG)
    return writes, table


def _legacy_turn(session_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        prompt="compare supplier ids", raw_prompt="compare supplier ids",
        session_id=session_id,
        tool_calls_log=[{"name": "query_erp", "toolUseId": "t1"}],
        tool_inputs={"t1": "{}"}, tool_results={"t1": "rows"},
        response_text=[_RESPONSE_WITH_PROPOSALS],
    )


def _integrity_state():
    return SimpleNamespace(
        tool_names={"t1": "query_erp"},
        response_text=[_RESPONSE_WITH_PROPOSALS],
        seed_rule_index={},
        context=SimpleNamespace(tool_system_ids=None),
        rule_ids={"ir-x"},
        matched_rows=[{"neptune_node_id": "ir-x", "concept": "supplier"}],
        agent_result=None,
    )


_FX_SESSION = fx.SESSION_PREFIX + "a1b2c3d4e5f60718-r1"


# --------------------------------------------------------------------------- #
# (A) side-effect learning writes suppressed on exploration sessions
# --------------------------------------------------------------------------- #
def test_capture_writers_skip_feedback_exploration_sessions(monkeypatch):
    """FX-A1 (legacy finally-block branch): an exploration-session turn
    records ZERO learning writes at every seam."""
    writes, table = _arm_write_recorders(monkeypatch)
    s = _legacy_turn(_FX_SESSION)

    runtime_app._post_turn_learning_checks(s)
    runtime_app._capture_correlation_insights(s)
    runtime_app._capture_value_mapping_proposals(s)

    assert writes == []
    assert table.writes == []


def test_integrity_post_turn_path_skips_feedback_exploration_sessions(monkeypatch):
    """FX-A1 (integrity mirror branch): the FULL post-turn path with an
    exploration session writes NOTHING."""
    writes, table = _arm_write_recorders(monkeypatch)

    runtime_app._integrity_post_turn_learning(
        _integrity_state(), "compare supplier ids", _FX_SESSION)

    assert writes == []
    assert table.writes == []


def test_normal_sessions_still_write_after_the_gate_change(monkeypatch):
    """Over-suppression tripwire: an ordinary chat session still files."""
    writes, _table = _arm_write_recorders(monkeypatch)
    s = _legacy_turn("chat-session-1")

    runtime_app._capture_value_mapping_proposals(s)

    assert [w for w in writes if w[0] == "proposal"], "normal turn must file"


def test_payload_session_id_reaches_the_skip_gate(monkeypatch):
    """FX-A1 transport (AgentCore-runtime branch): the chat-client payload
    ``sessionId`` lands in the turn state and trips the shared gate —
    the same payload-field transport ``writeMemory`` rides."""
    monkeypatch.setattr(runtime_app, "_prelude_with_planned",
                        lambda raw, cand: (raw, [], None))
    s = runtime_app._new_turn_state(
        {"prompt": "q", "sessionId": _FX_SESSION}, None)
    assert runtime_app._skip_post_turn_capture(s)

    normal = runtime_app._new_turn_state(
        {"prompt": "q", "sessionId": "sess-1"}, None)
    assert not runtime_app._skip_post_turn_capture(normal)


def test_waste_analysis_never_persists_for_exploration_sessions(monkeypatch):
    """FX-A2: the analysis persist (field_annotation proposals / guidance
    promotions) is gated for exploration sessions too."""
    persisted: list = []
    monkeypatch.setattr(
        runtime_app, "_analysis_call_log", lambda s: (["line"], 1))
    monkeypatch.setattr(
        runtime_app, "_analysis_prompt_text", lambda *a: "prompt")
    monkeypatch.setattr(
        runtime_app, "_invoke_analysis_model", lambda _p: "{}")
    monkeypatch.setattr(
        runtime_app, "_parsed_analysis",
        lambda *_a: ({"wasted_calls": [{"step": 1}], "deterministic_pct": 50},
                     [{"system": "sys-a", "table": "t", "field": "f",
                       "annotation": "note"}]))
    monkeypatch.setattr(
        runtime_app, "_persist_analysis_recommendations",
        lambda *a, **k: persisted.append(a))
    s = _legacy_turn(_FX_SESSION)
    list(runtime_app._waste_analysis_events(s, {"wasted_steps": [1]}, 3, 0))
    assert persisted == []


# --------------------------------------------------------------------------- #
# (A) cross-pin + the exploration's own insight survives
# --------------------------------------------------------------------------- #
@pytest.fixture()
def _exploration_harness(monkeypatch):
    """Inline exploration worker with a scripted diverging stream (the
    ``test_feedback_exploration`` idiom)."""
    import agents.feedback_exploration_settings as fxs
    monkeypatch.delenv("FEEDBACK_EXPLORATION_ENABLED", raising=False)
    fxs._reset()
    fxs.configure(SimpleNamespace(get_flag=lambda: True,
                                  set_flag=lambda v: None))
    monkeypatch.setattr(fx, "_JOBS", JobStore("feedback-explore", table=None))

    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(fx, "threading",
                        SimpleNamespace(Thread=_InlineThread))
    calls: list[tuple] = []
    events = [
        ToolUseEvent(tool_name="sys_a___run_query"),
        ToolUseEvent(tool_name="sys_b___run_query"),
        MetadataEvent(data={"intelligence_summary": {
            "rules_applied": [{"id": "r1", "label": "rule one"}]}}),
        MessageEvent(content="answer"),
        DoneEvent(),
    ]

    def factory(prompt, session_id):
        calls.append((prompt, session_id))

        def gen():
            yield from events
        return gen()

    monkeypatch.setattr(fx, "_stream_factory", factory)
    yield calls
    fxs._reset()


def test_exploration_sessions_carry_the_recognized_suppression_prefix(
        monkeypatch, _exploration_harness):
    """FX-A4 cross-pin: BOTH worker-minted run sessions start with
    ``fx.SESSION_PREFIX`` AND that prefix trips the runtime gate — drifting
    either side breaks this test."""
    lpm: list = []
    monkeypatch.setattr(self_learning, "log_proposed_mapping",
                        lambda **k: lpm.append(k) or "mid-1")
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda refs, evidence="": len(refs))
    assert fx.maybe_start_exploration(
        "u1", "s1", "m1", _QUESTION, _COMMENT, ["sys-a"], []) is not None
    calls = _exploration_harness
    assert len(calls) == 2
    for _prompt, session_id in calls:
        assert session_id.startswith(fx.SESSION_PREFIX)
        gate_view = SimpleNamespace(session_id=session_id, prompt="x")
        assert runtime_app._skip_post_turn_capture(gate_view)


def test_exploration_own_insight_still_filed_under_suppression(
        monkeypatch, _exploration_harness):
    """FX-A3: the suppression must NOT swallow the exploration's own filing —
    exactly ONE insight proposal is written even though both run sessions
    are no-learning sessions."""
    lpm: list = []
    monkeypatch.setattr(self_learning, "log_proposed_mapping",
                        lambda **k: lpm.append(k) or "mid-1")
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda refs, evidence="": len(refs))
    exploration_id = fx.maybe_start_exploration(
        "u1", "s1", "m1", _QUESTION, _COMMENT, ["sys-a"], [])
    assert exploration_id is not None
    assert len(lpm) == 1
    assert lpm[0]["proposed_type"] == "insight"
    assert lpm[0]["source"] == "user-feedback"
    assert fx._jobs().get(exploration_id)["outcome"] == "filed"


def test_local_client_branch_receives_the_prefixed_session(monkeypatch):
    """Local-branch transport: ``_default_stream`` forwards the prefixed
    session id verbatim to LocalChatClient (no local post-pass exists today —
    module docstring — but the signal is in place if one is added)."""
    captured: dict = {}

    class _FakeClient:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return iter(())

    import agents.chat_client as chat_client
    import agents.config as config
    monkeypatch.setattr(config, "get_config",
                        lambda: SimpleNamespace(has_runtime=False))
    monkeypatch.setattr(chat_client, "LocalChatClient", lambda: _FakeClient())
    list(fx._default_stream("p", _FX_SESSION))
    assert captured["session_id"] == _FX_SESSION
    assert captured["write_memory"] is False


# --------------------------------------------------------------------------- #
# (B) query_context: user question only, fence NEVER storable
# --------------------------------------------------------------------------- #
def _ingest_with(prompt: str) -> dict:
    stored: list[dict] = []
    count = runtime_app._ingest_value_mapping_proposals(
        [_VM_BLOCK], prompt=prompt, tool_calls_count=2,
        log_mapping=lambda **k: stored.append(k) or "mid-1")
    assert count == 1
    return stored[0]


def test_fenced_block_never_storable_in_query_context():
    """FX-B1: feed the REAL composed exploration prompt (instruction +
    question + ``render_fenced`` comment) through the value-mapping ingest —
    the stored context carries NO fence marker, NO comment text, NO origin
    label, and still carries the question. Reverting to ``prompt[:500]``
    fails this: the fence opens at ~char 400, so marker AND comment text
    land inside the cap."""
    prompt = fx._compose_prompt(_QUESTION, _COMMENT, "m1")
    assert FENCE_OPEN in prompt  # the hazard is real in this fixture
    stored = _ingest_with(prompt)["query_context"]
    assert FENCE_OPEN not in stored
    assert FENCE_CLOSE not in stored
    assert "COMMENT_MARKER_x9y8z7" not in stored
    assert "user-feedback:m1" not in stored          # origin label
    assert "USER FEEDBACK ON A PRIOR ANSWER" not in stored  # fence heading
    assert _QUESTION in stored


def test_fence_free_prompt_stored_bounded_and_unchanged():
    stored = _ingest_with("plain question " * 60)["query_context"]
    assert stored == ("plain question " * 60)[:500]


def test_user_typed_fence_token_is_never_stored():
    """A fence token typed INTO a chat question (no renderer shape) still
    never reaches storage — conservative cut, empty context beats leakage."""
    stored = _ingest_with(f"echo {FENCE_OPEN} please")["query_context"]
    assert FENCE_OPEN not in stored


def test_new_turn_state_raw_prompt_is_the_user_question(monkeypatch):
    """FX-B2 (legacy branch): ``s.raw_prompt`` is the payload ``rawPrompt``
    (the un-enriched question), not the app-enriched prompt."""
    # The rawPrompt differs from the prompt, so the stub also gets the
    # routing question (test_route_seed_user_question.py).
    monkeypatch.setattr(runtime_app, "_prelude_with_planned",
                        lambda raw, cand, **_routing: (raw, [], None))
    s = runtime_app._new_turn_state(
        {"prompt": "[Temporal context: now] which lots were affected?",
         "rawPrompt": "which lots were affected?", "sessionId": "sess-1"},
        None)
    assert s.raw_prompt == "which lots were affected?"
    # fallback: callers that send no rawPrompt keep the pre-prelude prompt
    bare = runtime_app._new_turn_state(
        {"prompt": "plain q", "sessionId": "sess-1"}, None)
    assert bare.raw_prompt == "plain q"


def test_integrity_mirror_stores_the_question_as_query_context(monkeypatch):
    """FX-B3 (integrity branch): the state-carried question
    (``state.prompt`` — the payload's ``rawPrompt``) — not the enriched
    prompt — becomes the stored value_mapping query_context."""
    writes, _table = _arm_write_recorders(monkeypatch)
    state = _integrity_state()
    state.prompt = "compare supplier ids"

    runtime_app._integrity_post_turn_learning(
        state, "[Temporal context: now] compare supplier ids", "sess-1")

    vm = [k for kind, k in writes
          if kind == "proposal" and k.get("proposed_type") == "value_mapping"]
    assert vm, "value_mapping proposal expected on a normal session"
    assert vm[0]["query_context"] == "compare supplier ids"
