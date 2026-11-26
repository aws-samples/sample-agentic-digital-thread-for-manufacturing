# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-system guidance store (ADR-0035 i′) — offline unit lane.

Covers the Task 2A acceptance:

- ``write`` bumps the monotonic version;
- a second ``read`` with no bump does NOT hit the backend (call count on a fake);
- a version bump triggers EXACTLY ONE reload from the backend;
- ``create_system_agent``'s prompt includes the stored guidance for that system_id;
- ``AgentManager._base_env_vars`` includes ``SYSTEM_QUERY_MODE`` and a per-system
  ``SYSTEM_AGENT_MODEL`` override (System-node ``system_model`` field).

Fully offline: a fake backend records ``load`` calls; ``create_system_agent`` is
exercised with the Strands ``Agent`` / ``BedrockModel`` stubbed so no AWS/network.
"""
from __future__ import annotations

import pytest

import agents.system_guidance as sg


class FakeBackend:
    """In-memory guidance backend that counts backend calls.

    Implements the cross-process seam: a persisted ``version`` per system, a cheap
    ``version()`` GET (counted separately from a full ``load()``), and a ``load()``
    that returns ``(text, version)``. Shared between two store objects to simulate
    a writer container and a separate reader container.
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.versions: dict[str, int] = {}
        self.load_calls = 0
        self.version_calls = 0
        self.save_calls = 0

    def version(self, system_id: str) -> int:
        self.version_calls += 1
        return self.versions.get(system_id, 0)

    def load(self, system_id: str) -> tuple[str, int]:
        self.load_calls += 1
        return self.store.get(system_id, ""), self.versions.get(system_id, 0)

    def save(self, system_id: str, text: str, version: int) -> None:
        self.save_calls += 1
        self.store[system_id] = text
        self.versions[system_id] = version



    def snapshot(self, system_id: str) -> sg.GuidanceSnapshot:
        if system_id not in self.store:
            return sg.GuidanceSnapshot.absent(self.versions.get(system_id, 0))
        return sg.GuidanceSnapshot.present(
            self.store[system_id], self.versions.get(system_id, 0))

    def compare_and_set(self, system_id: str, expected: sg.GuidanceSnapshot,
                        text: str) -> sg.GuidanceSnapshot:
        if self.snapshot(system_id) != expected:
            raise sg.GuidanceConflict()
        version = expected.version + 1
        self.save(system_id, text, version)
        return sg.GuidanceSnapshot.present(text, version)

    def restore(self, system_id: str, expected: sg.GuidanceSnapshot,
                target: sg.GuidanceSnapshot) -> sg.GuidanceSnapshot:
        if self.snapshot(system_id) != expected:
            raise sg.GuidanceConflict()
        version = expected.version + 1
        if target.exists:
            self.save(system_id, target.text, version)
            return sg.GuidanceSnapshot.present(target.text, version)
        self.store.pop(system_id, None)
        self.versions[system_id] = version
        return sg.GuidanceSnapshot.absent(version)


class CasBackend(FakeBackend):
    """Modern in-memory backend used by strict publication tests."""

    def snapshot(self, system_id: str) -> sg.GuidanceSnapshot:
        if system_id not in self.store:
            return sg.GuidanceSnapshot.absent(self.versions.get(system_id, 0))
        return sg.GuidanceSnapshot.present(
            self.store[system_id], self.versions.get(system_id, 0))

    def compare_and_set(self, system_id: str, expected: sg.GuidanceSnapshot,
                        text: str) -> sg.GuidanceSnapshot:
        if self.snapshot(system_id) != expected:
            raise sg.GuidanceConflict()
        version = expected.version + 1
        self.save(system_id, text, version)
        return self.snapshot(system_id)
@pytest.fixture
def store():
    """A store backed by a fresh fake, also installed as the module singleton."""
    backend = FakeBackend()
    s = sg.SystemGuidanceStore(backend=backend)
    prior = sg._store
    sg.set_store(s)
    try:
        yield s, backend
    finally:
        sg.set_store(prior)


# --------------------------------------------------------------------------- #
# Version + cache semantics
# --------------------------------------------------------------------------- #
def test_write_bumps_monotonic_version(store):
    s, _backend = store
    assert s.current_version("sys1") == 0
    assert s.write("sys1", "guidance A") == 1
    assert s.current_version("sys1") == 1
    assert s.write("sys1", "guidance B") == 2
    assert s.current_version("sys1") == 2


def test_second_read_without_bump_does_not_hit_backend(store):
    s, backend = store
    s.write("sys1", "guidance A")
    assert s.read("sys1") == "guidance A"
    assert backend.load_calls == 1  # first read warms the cache
    # Second read, same version → served from cache, NO backend call.
    assert s.read("sys1") == "guidance A"
    assert backend.load_calls == 1


def test_bump_triggers_exactly_one_reload(store):
    s, backend = store
    s.write("sys1", "guidance A")
    assert s.read("sys1") == "guidance A"
    assert s.read("sys1") == "guidance A"
    assert backend.load_calls == 1  # cache warm and stable

    # A bump invalidates the cache; the next read reloads exactly once.
    s.write("sys1", "guidance B")
    assert s.read("sys1") == "guidance B"
    assert backend.load_calls == 2  # exactly one reload on the bump
    # ...and stays warm afterwards.
    assert s.read("sys1") == "guidance B"
    assert backend.load_calls == 2


def test_read_empty_system_returns_blank_without_backend(store):
    s, backend = store
    assert s.read("") == ""
    assert backend.load_calls == 0


def test_read_unknown_system_before_write_loads_once(store):
    s, backend = store
    assert s.read("neverwritten") == ""
    assert backend.load_calls == 1
    assert s.read("neverwritten") == ""
    assert backend.load_calls == 1  # cached at version 0


def test_per_system_versions_are_independent(store):
    s, _backend = store
    s.write("sys1", "A")
    s.write("sys1", "A2")
    s.write("sys2", "B")
    assert s.current_version("sys1") == 2
    assert s.current_version("sys2") == 1


# --------------------------------------------------------------------------- #
# Cross-process correctness (ADR-0035 zero-downtime): a SEPARATE reader store
# instance picks up a writer store instance's version bump, because the version
# is sourced from the shared BACKEND on every read — not from in-process state.
# --------------------------------------------------------------------------- #
def test_separate_reader_store_picks_up_writer_bump_via_shared_backend():
    backend = FakeBackend()  # the shared "table" both containers talk to
    writer = sg.SystemGuidanceStore(backend=backend)
    reader = sg.SystemGuidanceStore(backend=backend)

    # Reader warms its cache while nothing is written yet.
    assert reader.read("sys1") == ""

    # Writer (a DIFFERENT process/container) publishes guidance.
    assert writer.write("sys1", "v1 guidance") == 1
    # The reader — with NO in-process bump of its own — observes the new version
    # via the cheap backend version GET and reloads.
    assert reader.read("sys1") == "v1 guidance"

    # A second cross-process bump is likewise picked up on the next read.
    assert writer.write("sys1", "v2 guidance") == 2
    assert reader.read("sys1") == "v2 guidance"

    # And a stable version does not force a full reload (cheap check only).
    loads_after = backend.load_calls
    reader.read("sys1")
    assert backend.load_calls == loads_after


def test_read_sources_version_from_backend_every_call():
    """A read always performs the cheap backend version GET (that is what makes it
    cross-process correct); a full load only happens on a version change."""
    backend = FakeBackend()
    s = sg.SystemGuidanceStore(backend=backend)
    s.write("sys1", "A")

    assert s.read("sys1") == "A"
    v_after_first = backend.version_calls
    loads_after_first = backend.load_calls
    # Second read at a stable version: another cheap version GET, but NO reload.
    assert s.read("sys1") == "A"
    assert backend.version_calls > v_after_first
    assert backend.load_calls == loads_after_first


# --------------------------------------------------------------------------- #
# Graceful no-op fallback when the table env is unset
# --------------------------------------------------------------------------- #


def test_publish_exact_requires_exact_readback():
    backend = CasBackend()
    s = sg.SystemGuidanceStore(backend=backend)
    assert s.publish_exact("sys1", "exact guidance") == 1
    assert s.read("sys1") == "exact guidance"


def test_publish_exact_raises_when_backend_does_not_store_exact_text():
    class AlteringBackend(CasBackend):
        def compare_and_set(self, system_id, expected, text):
            return super().compare_and_set(
                system_id, expected, text + " altered")

    s = sg.SystemGuidanceStore(backend=AlteringBackend())
    with pytest.raises(sg.GuidanceUnavailable, match="exact readback failed"):
        s.publish_exact("sys1", "expected guidance")


def test_dynamo_backend_is_read_only_fallback_when_table_unset():
    backend = sg.DynamoGuidanceBackend(table_name="")
    assert backend.version("sys1") == 0
    assert backend.load("sys1") == ("", 0)
    with pytest.raises(sg.GuidanceUnavailable):
        backend.snapshot("sys1")
    with pytest.raises(sg.GuidanceUnavailable):
        backend.compare_and_set(
            "sys1", sg.GuidanceSnapshot.absent(), "not dropped")
    assert not hasattr(backend, "save")


def test_legacy_backend_write_is_compatible_but_not_usable_for_strict_cas():
    class LegacyBackend:
        def __init__(self):
            self.text = ""
            self.version_value = 0

        def version(self, _system_id):
            return self.version_value

        def load(self, _system_id):
            return self.text, self.version_value

        def save(self, _system_id, text, version):
            self.text = text
            self.version_value = version

    backend = LegacyBackend()
    s = sg.SystemGuidanceStore(backend=backend)
    assert s.write("sys1", "legacy mirror") == 1
    expected = sg.GuidanceSnapshot.present(
        "legacy mirror", 1, legacy=True)
    with pytest.raises(sg.GuidanceUnavailable,
                       match="does not support guidance CAS"):
        s.compare_and_set("sys1", expected, "acceptance write")
    with pytest.raises(sg.GuidanceUnavailable,
                       match="does not support guidance CAS"):
        s.publish_exact("sys1", "acceptance write", expected)


def test_default_backend_falls_back_to_noop(monkeypatch):
    monkeypatch.delenv("ADT_SYSTEM_GUIDANCE_TABLE", raising=False)
    assert isinstance(sg._default_backend(), sg.NoOpBackend)


def test_module_api_delegates_to_singleton():
    backend = FakeBackend()
    prior = sg._store
    sg.set_store(sg.SystemGuidanceStore(backend=backend))
    try:
        assert sg.current_version("s") == 0
        assert sg.write("s", "hello") == 1
        assert sg.read("s") == "hello"
        assert sg.current_version("s") == 1
    finally:
        sg.set_store(prior)


# --------------------------------------------------------------------------- #
# Per-question guidance injection (ADR-0035 i′): guidance is NOT baked into the
# system prompt at construction — it is consulted per-question via the store's
# cheap version check and fenced as a NON-AUTHORITATIVE block.
# --------------------------------------------------------------------------- #
class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeAgent:
    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools")


def test_create_system_agent_does_not_bake_guidance_into_system_prompt(store, monkeypatch):
    import agents.system_agent as sa

    s, _backend = store
    guidance = "SYSTEM-LOCAL: always filter by the partition column first."
    s.write("sys1", guidance)

    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)

    agent = sa.create_system_agent(
        system_id="sys1",
        system_name="Example Corp Source",
        schema="public",
        system_prompt="Base system prompt.",
        connection_type="SiteWise (Direct)",
        connection_config={},
    )
    # Guidance is per-question now — it must NOT be frozen into the system prompt.
    assert guidance not in agent.system_prompt
    assert "NON-AUTHORITATIVE" not in agent.system_prompt


def test_augment_question_injects_guidance_as_non_authoritative_block(store):
    import agents.system_agent as sa

    s, _backend = store
    guidance = "SYSTEM-LOCAL: always filter by the partition column first."
    s.write("sys1", guidance)

    augmented = sa._augment_question_with_guidance("sys1", "How many units shipped?")
    # Legacy flat delivery remains visible but is fenced without inferred intent.
    assert augmented.startswith("How many units shipped?")
    assert guidance in augmented
    assert "BEGIN LEGACY UNSCOPED GUIDANCE" in augmented
    assert "END LEGACY UNSCOPED GUIDANCE" in augmented
    assert "unknown intent" in augmented
    assert "subordinate to evidence" in augmented


def test_augment_question_picks_up_version_bump_without_reconstruction(store):
    """A per-question call reflects the CURRENT guidance — a version bump is seen
    on the next question with no agent reconstruction / container restart."""
    import agents.system_agent as sa

    s, _backend = store
    s.write("sys1", "guidance v1")
    assert "guidance v1" in sa._augment_question_with_guidance("sys1", "Q?")

    s.write("sys1", "guidance v2")
    out = sa._augment_question_with_guidance("sys1", "Q?")
    assert "guidance v2" in out
    assert "guidance v1" not in out


def test_augment_question_no_guidance_returns_question_unchanged(store):
    import agents.system_agent as sa

    # No guidance written for this system → question returned untouched (no block).
    out = sa._augment_question_with_guidance("sys-empty", "What is the OEE?")
    assert out == "What is the OEE?"
    assert "NON-AUTHORITATIVE" not in out


def test_create_system_agent_no_guidance_leaves_prompt_clean(store, monkeypatch):
    import agents.system_agent as sa

    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)

    agent = sa.create_system_agent(
        system_id="sys-empty",
        system_name="Example Corp Source",
        schema="public",
        system_prompt="Base system prompt.",
        connection_type="SiteWise (Direct)",
        connection_config={},
    )
    # Guidance is never baked into the system prompt (per-question path only).
    assert "System-local guidance:" not in agent.system_prompt
    assert "NON-AUTHORITATIVE" not in agent.system_prompt


# --------------------------------------------------------------------------- #
# _base_env_vars: SYSTEM_QUERY_MODE + per-system SYSTEM_AGENT_MODEL
# --------------------------------------------------------------------------- #
def _mgr():
    from agents.agent_manager import AgentManager

    return AgentManager(mode="cloud")


@pytest.fixture
def _no_saved_agent_model(monkeypatch):
    """No Admin > Settings save: the SSM agent-model read returns "" offline
    (the saved-model path is pinned in test_provision_saved_agent_model.py).
    Used only by the four _base_env_vars tests below."""
    from agents import model_config
    monkeypatch.setattr(model_config, "_ssm_value", lambda _name, _region: "")


@pytest.mark.usefixtures("_no_saved_agent_model")
def test_start_cloud_env_defaults(monkeypatch):
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    monkeypatch.delenv("SYSTEM_AGENT_MODEL", raising=False)
    env = _mgr()._base_env_vars("sys1", "Example Corp", "public", "Athena", "us-east-1", {})
    assert env["SYSTEM_QUERY_MODE"] == "agent"
    # No hardcoded model fallback: the catalog's AGENT default is the single source.
    from agents.model_catalog import AGENT, default_model
    assert env["SYSTEM_AGENT_MODEL"] == default_model(AGENT, "us-east-1")


@pytest.mark.usefixtures("_no_saved_agent_model")
def test_start_cloud_env_per_system_model_override(monkeypatch):
    monkeypatch.delenv("SYSTEM_AGENT_MODEL", raising=False)
    cfg = {"system_model": "us.anthropic.claude-sonnet-4-6"}
    env = _mgr()._base_env_vars("sys1", "Example Corp", "public", "Athena", "us-east-1", cfg)
    # Per-system model wins over the deployment-wide default.
    assert env["SYSTEM_AGENT_MODEL"] == "us.anthropic.claude-sonnet-4-6"
    assert env["SYSTEM_QUERY_MODE"] == "agent"


@pytest.mark.usefixtures("_no_saved_agent_model")
def test_start_cloud_env_query_mode_override(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    env = _mgr()._base_env_vars("sys1", "Example Corp", "public", "Athena", "us-east-1", {})
    assert env["SYSTEM_QUERY_MODE"] == "orchestrator"


@pytest.mark.usefixtures("_no_saved_agent_model")
def test_start_cloud_env_falls_back_to_env_model(monkeypatch):
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", "amazon.nova-pro-v1:0")
    # No per-system override → deployment-wide env default is used.
    env = _mgr()._base_env_vars("sys1", "Example Corp", "public", "Athena", "us-east-1", {})
    assert env["SYSTEM_AGENT_MODEL"] == "amazon.nova-pro-v1:0"
