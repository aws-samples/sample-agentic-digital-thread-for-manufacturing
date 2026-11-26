# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``MEMORY_LTM_SCOPE`` — which long-term-memory namespaces a turn RETRIEVES.

Defect (workshop R2b, defect C): one shared login makes every attendee the
same ``actorId``, so the actor-scoped ``/facts/{actorId}/`` and
``/preferences/{actorId}/`` namespaces leaked other attendees' chats into a
fresh chat — the orchestrator answered from memory with one tool call and no
Sources. ``session`` keeps ONLY the per-chat summary namespace; ``actor`` (the
default) is the unchanged behaviour. Writes are never affected.

Pinned here: config parsing (invalid fails loud), the retrieval_config shape
for both scopes (bedrock_agentcore imports replaced by fakes), the orchestrator
boot gate, and the 50-line gate on the helpers this change added.
"""

import inspect
import sys
import types

import pytest

import agents.orchestrator as orch
from agents import runtime_app
from agents.config import MEMORY_LTM_SCOPES, memory_ltm_scope
from agents.runtime_env_contract import STALE_CHECKED_KEYS

_SUMMARY_NS = "/summaries/{actorId}/{sessionId}/"
_ACTOR_NS = ("/facts/{actorId}/", "/preferences/{actorId}/")


# --------------------------------------------------------------------------- #
# config parsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_is_the_actor_default(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("MEMORY_LTM_SCOPE", raising=False)
    else:
        monkeypatch.setenv("MEMORY_LTM_SCOPE", raw)
    assert memory_ltm_scope() == "actor"


@pytest.mark.parametrize("raw,expected", [
    ("actor", "actor"), ("session", "session"),
    (" Session ", "session"), ("ACTOR", "actor"),
])
def test_valid_values_are_normalised(monkeypatch, raw, expected):
    monkeypatch.setenv("MEMORY_LTM_SCOPE", raw)
    assert memory_ltm_scope() == expected


@pytest.mark.parametrize("raw", ["sessions", "user", "none", "global", "0"])
def test_invalid_value_fails_loud_naming_the_key(monkeypatch, raw):
    monkeypatch.setenv("MEMORY_LTM_SCOPE", raw)
    with pytest.raises(ValueError, match="MEMORY_LTM_SCOPE"):
        memory_ltm_scope()


def test_explicit_environ_mapping_wins_and_is_not_mutated(monkeypatch):
    monkeypatch.setenv("MEMORY_LTM_SCOPE", "actor")
    environ = {"MEMORY_LTM_SCOPE": "session"}
    assert memory_ltm_scope(environ) == "session"
    assert environ == {"MEMORY_LTM_SCOPE": "session"}


def test_vocabulary_is_the_closed_pair():
    assert MEMORY_LTM_SCOPES == ("actor", "session")


# --------------------------------------------------------------------------- #
# retrieval_config shape (bedrock_agentcore replaced by fakes)
# --------------------------------------------------------------------------- #

class _FakeRetrievalConfig:
    def __init__(self, top_k, relevance_score):
        self.top_k = top_k
        self.relevance_score = relevance_score


class _FakeMemoryConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeSessionManager:
    def __init__(self, memory_config, region_name=None):
        self.memory_config = memory_config
        self.region_name = region_name

    def retrieve_customer_context(self, event):  # pragma: no cover - wrapped only
        return None

    def create_message(self, *args, **kwargs):  # pragma: no cover - wrapped only
        return None


def _install_fake_sdk(monkeypatch):
    base = "bedrock_agentcore.memory.integrations.strands"
    config_mod = types.ModuleType(f"{base}.config")
    config_mod.AgentCoreMemoryConfig = _FakeMemoryConfig
    config_mod.RetrievalConfig = _FakeRetrievalConfig
    sm_mod = types.ModuleType(f"{base}.session_manager")
    sm_mod.AgentCoreMemorySessionManager = _FakeSessionManager
    parts = base.split(".")
    for i in range(1, len(parts) + 1):
        name = ".".join(parts[:i])
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, f"{base}.config", config_mod)
    monkeypatch.setitem(sys.modules, f"{base}.session_manager", sm_mod)


def _built_retrieval(monkeypatch, scope):
    _install_fake_sdk(monkeypatch)
    if scope is None:
        monkeypatch.delenv("MEMORY_LTM_SCOPE", raising=False)
    else:
        monkeypatch.setenv("MEMORY_LTM_SCOPE", scope)
    cfg = types.SimpleNamespace(memory_id="mem-1", aws_region="us-east-1")
    sm = orch._make_session_manager("sess-1", "user-1", cfg)
    assert isinstance(sm, _FakeSessionManager), "session manager not built"
    kwargs = sm.memory_config.kwargs
    assert kwargs["actor_id"] == "user-1" and kwargs["session_id"] == "sess-1"
    return kwargs["retrieval_config"]


@pytest.mark.parametrize("scope", [None, "actor"])
def test_actor_scope_keeps_all_three_namespaces_unchanged(monkeypatch, scope):
    retrieval = _built_retrieval(monkeypatch, scope)
    assert set(retrieval) == {*_ACTOR_NS, _SUMMARY_NS}
    shape = {ns: (rc.top_k, rc.relevance_score) for ns, rc in retrieval.items()}
    assert shape == {
        "/facts/{actorId}/": (10, 0.0),
        "/preferences/{actorId}/": (5, 0.0),
        _SUMMARY_NS: (3, 0.0),
    }


def test_session_scope_retrieves_only_the_per_chat_summaries(monkeypatch):
    retrieval = _built_retrieval(monkeypatch, "session")
    assert list(retrieval) == [_SUMMARY_NS]
    assert (retrieval[_SUMMARY_NS].top_k,
            retrieval[_SUMMARY_NS].relevance_score) == (3, 0.0)
    # Nothing actor-wide (i.e. cross-chat under a shared login) is retrieved.
    assert not any("{sessionId}" not in ns for ns in retrieval)


def test_invalid_scope_never_falls_back_to_actor_wide_retrieval(monkeypatch):
    """An invalid scope must not silently re-open the cross-chat leak: the
    session manager is NOT built (memory degrades loudly, chat continues)."""
    _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("MEMORY_LTM_SCOPE", "bogus")
    monkeypatch.setattr(orch, "_memory_degraded", False)
    monkeypatch.setattr(orch, "_memory_degraded_count", 0)
    cfg = types.SimpleNamespace(memory_id="mem-1", aws_region="us-east-1")
    assert orch._make_session_manager("sess-1", "user-1", cfg) is None
    assert orch.memory_degraded() is True


# --------------------------------------------------------------------------- #
# Synthetic traffic never reads actor-wide long-term memory. The
# golden lane's follow-up mode keeps memory ON for the session (remembering the
# prior turn is what it tests), so under a shared actor the eval-run, smoke
# and fx-eval sessions were retrieving Facts and Preferences written by every
# earlier run ("you previously selected Option B"). Automated sessions keep
# only their own chat's summaries, whatever MEMORY_LTM_SCOPE says.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("session_id", [
    "eval-run-abc123-q4", "eval-golden-7f-q2", "smoke-9a1-q1", "fx-eval-x-q3",
    "tryit-1", "feedback-explore-2", "diag-3", "warmup-4",
])
def test_automated_sessions_retrieve_only_their_own_summaries(monkeypatch, session_id):
    _install_fake_sdk(monkeypatch)
    monkeypatch.delenv("MEMORY_LTM_SCOPE", raising=False)  # actor scope
    cfg = types.SimpleNamespace(memory_id="mem-1", aws_region="us-east-1")
    sm = orch._make_session_manager(session_id, "smoke", cfg)
    retrieval = sm.memory_config.kwargs["retrieval_config"]
    assert list(retrieval) == [_SUMMARY_NS]


def test_a_real_chat_under_actor_scope_still_gets_facts_and_preferences(monkeypatch):
    _install_fake_sdk(monkeypatch)
    monkeypatch.delenv("MEMORY_LTM_SCOPE", raising=False)
    cfg = types.SimpleNamespace(memory_id="mem-1", aws_region="us-east-1")
    sm = orch._make_session_manager("session-1790976664678-yqml68tq", "ADT", cfg)
    assert set(sm.memory_config.kwargs["retrieval_config"]) == {*_ACTOR_NS, _SUMMARY_NS}


def test_automated_prefix_policy_is_the_memory_client_one():
    """One list, not two: the retrieval gate reuses the session-list policy."""
    from agents.memory_client import AUTOMATED_SESSION_PREFIXES, _is_automated_session
    assert orch._is_automated_memory_session is _is_automated_session
    assert "eval-run-" in AUTOMATED_SESSION_PREFIXES


# --------------------------------------------------------------------------- #
# orchestrator boot gate — an invalid scope fails CLOSED at process start
# --------------------------------------------------------------------------- #

def _complete_cloud_env(monkeypatch, role):
    for flag in ("AGENT_MODE", "DEV_MODE"):
        monkeypatch.delenv(flag, raising=False)
    for key in STALE_CHECKED_KEYS[role]:
        monkeypatch.setenv(key, f"value-{key.lower()}")


def test_orchestrator_boot_rejects_an_invalid_scope(monkeypatch):
    _complete_cloud_env(monkeypatch, "orchestrator")
    monkeypatch.setenv("MEMORY_LTM_SCOPE", "bogus")
    with pytest.raises(ValueError, match="MEMORY_LTM_SCOPE"):
        runtime_app._assert_runtime_env("orchestrator")


@pytest.mark.parametrize("scope", ["", "actor", "session"])
def test_orchestrator_boot_accepts_valid_or_absent_scope(monkeypatch, scope):
    _complete_cloud_env(monkeypatch, "orchestrator")
    monkeypatch.setenv("MEMORY_LTM_SCOPE", scope)
    runtime_app._assert_runtime_env("orchestrator")


def test_system_role_boot_ignores_the_orchestrator_only_knob(monkeypatch):
    _complete_cloud_env(monkeypatch, "system")
    monkeypatch.setenv("MEMORY_LTM_SCOPE", "bogus")
    runtime_app._assert_runtime_env("system")


# --------------------------------------------------------------------------- #
# 50-line gate on the functions this change added/changed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fn", [
    memory_ltm_scope, orch._ltm_retrieval_config, runtime_app._assert_runtime_env,
    orch._make_session_manager, orch._wrap_session_manager,
    orch._note_memory_degraded,
])
def test_changed_functions_stay_under_fifty_lines(fn):
    assert len(inspect.getsource(fn).splitlines()) <= 50
