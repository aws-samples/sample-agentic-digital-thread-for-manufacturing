# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 3 / Task 3a (ADR-0068) — the onboarding chat's tool-result→checkpoint
dispatch.

The dispatch lives in the pure module-level ``onboard_checkpoint_event``;
``_make_onboard_tool_hook(q)`` binds it to a request's SSE queue. The guard
keys tie the dispatch to the REAL tool results: ``assess_vocabulary_fit``
emits only when the result carries ``fit_pct`` (its error dict does not) and
``propose_concepts`` only on ``proposals`` — so error dicts never become chat
cards. Placeholder data only (``system-a`` / ``widget`` — neutrality ratchet).
"""
import sys
from pathlib import Path
from types import SimpleNamespace

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from agents.onboarding_session import SessionNotFoundError  # noqa: E402

# --------------------------------------------------------------------------- #
# The name→(step, guard_key) table
# --------------------------------------------------------------------------- #


def test_checkpoint_step_table_pins_all_seven_steps():
    """The five wizard steps plus the two ADR-0068 conversational cards ride
    the SAME table; changing a step name or guard key is a frontend contract
    change (ObFlow.ChatCheckpoint switches on these step strings)."""
    assert onboard.ONBOARD_CHECKPOINT_STEPS == {
        'discover': ('discover', 'tables'),
        'map_fields': ('map', 'mappings'),
        'resolve_identities': ('resolve', 'rules'),
        'provision_system': ('provision', 'tool_name'),
        'verify_system': ('verify', None),
        'assess_vocabulary_fit': ('vocab_fit', 'fit_pct'),
        'propose_concepts': ('concept_proposal', 'proposals'),
    }


# --------------------------------------------------------------------------- #
# onboard_checkpoint_event — pure dispatch
# --------------------------------------------------------------------------- #


def _event(tool_name, result):
    return onboard.onboard_checkpoint_event(tool_name, result)


def test_vocab_fit_result_emits_vocab_fit_checkpoint():
    result = {'system_id': 'system-a', 'fit_pct': 42, 'suggestion': 'ask_context'}
    assert _event('assess_vocabulary_fit', result) == {
        'event': 'checkpoint',
        'data': {'step': 'vocab_fit', 'result': result},
    }


def test_propose_concepts_result_emits_concept_proposal_checkpoint():
    result = {'system_id': 'system-a', 'proposals': [], 'insert_count': 0}
    assert _event('propose_concepts', result) == {
        'event': 'checkpoint',
        'data': {'step': 'concept_proposal', 'result': result},
    }


def test_error_dicts_never_become_cards():
    assert _event('assess_vocabulary_fit', {'error': 'no session'}) is None
    assert _event('propose_concepts', {'error': 'invalid JSON'}) is None


def test_unknown_tools_and_unparseable_results_emit_nothing():
    assert _event('get_catalog', {'systems': []}) is None
    assert _event('assess_vocabulary_fit', 'not json at all') is None
    assert _event('assess_vocabulary_fit', '[1, 2]') is None  # non-dict JSON


def test_wizard_steps_keep_their_guards():
    """The pre-existing branches, now table-driven — same emit/suppress
    behavior as the elif chain they replaced."""
    assert _event('discover', {'tables': []})['data']['step'] == 'discover'
    assert _event('discover', {'error': 'x'}) is None
    assert _event('map_fields', {'mappings': []})['data']['step'] == 'map'
    assert _event('resolve_identities', {'rules': []})['data']['step'] == 'resolve'
    assert _event('provision_system', {'tool_name': 'query_x'})['data']['step'] == 'provision'
    assert _event('provision_system', {'error': 'x'}) is None
    assert _event('provision_system', {'onboarding_id': 'x', 'status': 'provisioning'}) is None
    assert _event('verify_system', {})['data']['step'] == 'verify'


def test_json_string_results_are_parsed_like_the_old_hook():
    evt = _event('assess_vocabulary_fit', '{"fit_pct": 10, "suggestion": "consider_pack"}')
    assert evt is not None
    assert evt['data']['step'] == 'vocab_fit'
    assert evt['data']['result']['fit_pct'] == 10


def test_tool_hook_forwards_checkpoints_to_its_queue():
    """The real AfterToolCallEvent hook (bound to a request's SSE queue via
    ``_make_onboard_tool_hook``) puts exactly the dispatch's event dicts —
    and nothing for suppressed results."""
    import queue

    q = queue.Queue()
    hook = onboard._make_onboard_tool_hook(q)
    fit = {'system_id': 'system-a', 'fit_pct': 42, 'suggestion': 'ok'}
    hook._on_tool_done(SimpleNamespace(
        tool_use={'name': 'assess_vocabulary_fit'}, result=fit))
    hook._on_tool_done(SimpleNamespace(
        tool_use={'name': 'assess_vocabulary_fit'}, result={'error': 'x'}))
    hook._on_tool_done(SimpleNamespace(
        tool_use={'name': 'propose_concepts'},
        result={'system_id': 'system-a', 'proposals': []}))

    assert q.get_nowait() == {
        'event': 'checkpoint', 'data': {'step': 'vocab_fit', 'result': fit}}
    assert q.get_nowait()['data']['step'] == 'concept_proposal'
    assert q.empty()  # the error dict was suppressed


# --------------------------------------------------------------------------- #
# Guard keys match the REAL tool result shapes (offline, faked store/graph)
# --------------------------------------------------------------------------- #

_TABLES = [
    {
        'name': 'widget_orders',
        'columns': [
            {'name': 'work_state', 'data_type': 'text'},
            {'name': 'attempt_no', 'data_type': 'integer'},
        ],
        'row_count': 5,
        'sample_values': {},
    },
]


class _FakeStore:
    def __init__(self, sessions):
        self.sessions = sessions

    def get_session(self, system_id):
        if system_id not in self.sessions:
            raise SessionNotFoundError(system_id)
        return dict(self.sessions[system_id])


def test_assess_vocabulary_fit_success_carries_the_guard_key(monkeypatch):
    monkeypatch.setattr(oa, 'neptune_query', lambda *a, **k: [])
    monkeypatch.setattr(oa, '_store', lambda: _FakeStore(
        {'system-a': {'system_id': 'system-a', 'status': 'discovered',
                      'discovered_tables': _TABLES}}))
    result = oa.assess_vocabulary_fit('system-a')
    assert 'fit_pct' in result  # the vocab_fit guard key
    assert _event('assess_vocabulary_fit', result) is not None


def test_assess_vocabulary_fit_error_lacks_the_guard_key(monkeypatch):
    monkeypatch.setattr(oa, '_store', lambda: _FakeStore({}))
    result = oa.assess_vocabulary_fit('system-a')
    assert 'fit_pct' not in result
    assert _event('assess_vocabulary_fit', result) is None


def test_propose_concepts_success_carries_the_guard_key(monkeypatch):
    monkeypatch.setattr(oa, 'neptune_query', lambda *a, **k: [])
    result = oa.propose_concepts(
        'system-a',
        '[{"id": "widget-yield", "name": "Widget yield", '
        '"description": "Share of widgets passing final check."}]',
    )
    assert 'proposals' in result  # the concept_proposal guard key
    assert _event('propose_concepts', result) is not None


def test_propose_concepts_error_lacks_the_guard_key():
    result = oa.propose_concepts('system-a', 'not-json{')
    assert 'proposals' not in result
    assert _event('propose_concepts', result) is None


# --- cloud-mode shapes (measured live 2026-09-11) --------------------------
# In deployed mode the agent's tools ride the onboarding gateway: the tool
# name arrives PREFIXED (`onboarding___discover`) and the result is a Strands
# ToolResult ({"status", "content": [{"text": "<json>"}]}), not the dict. The
# agent reported real fit numbers in chat while ZERO checkpoint events were
# emitted, so no card rendered. Both shapes normalize to the in-process one.
def test_gateway_prefixed_tool_name_dispatches_same_step():
    from agents.onboard import onboard_checkpoint_event
    result = {"system_id": "system-a", "fit_pct": 66, "suggestion": "ask_context"}
    plain = onboard_checkpoint_event("assess_vocabulary_fit", result)
    prefixed = onboard_checkpoint_event("onboarding___assess_vocabulary_fit", result)
    assert plain is not None and prefixed == plain


def test_tool_result_content_envelope_is_unwrapped():
    import json
    from agents.onboard import onboard_checkpoint_event
    payload = {"system_id": "system-a", "proposals": [], "insert_count": 0}
    tool_result = {"toolUseId": "t-1", "status": "success",
                   "content": [{"text": json.dumps(payload)}]}
    evt = onboard_checkpoint_event("onboarding___propose_concepts", tool_result)
    assert evt == {"event": "checkpoint",
                   "data": {"step": "concept_proposal", "result": payload}}


def test_tool_result_error_envelope_never_becomes_a_card():
    import json
    from agents.onboard import onboard_checkpoint_event
    tool_result = {"status": "error",
                   "content": [{"text": json.dumps({"error": "Call discover first."})}]}
    assert onboard_checkpoint_event("onboarding___assess_vocabulary_fit", tool_result) is None
