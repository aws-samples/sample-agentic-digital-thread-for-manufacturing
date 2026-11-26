# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Chart relay, TLS passthrough, ValueMapping ids, walk counters, chart-tag regex.

* Mode A chart chain: neither deployed mode exposes the sitewise_*
  tools to the orchestrator (Mode A = query_<id>, Mode B = run_query), so
  chart delivery must NOT depend on the inner system agent's model re-emitting
  tags (its own prompt forbids that). Pinned: ChartRedactionHook is registered
  on the INNER system agent, and ``_relay_inner_chart_tags`` deterministically
  appends the drained tags to the Mode A query answer (raw per-call drain, no
  session dedup — the inner agent is a shared runtime singleton; enforce mode
  drains and drops).
* ``I3X_TLS_VERIFY`` reaches AgentCore runtimes via
  ``AgentManager._base_env_vars`` (was ECS-app-only: self-signed deployments
  onboarded fine, then every runtime telemetry tool failed cert validation).
* equipment ValueMapping MERGE keys are content-derived (the old
  enumeration-index key minted new node ids for unchanged mappings on every
  provision).
* ``provision_i3x_hierarchy`` returns honest counters + warnings; a
  zero-asset walk is distinguishable from success.
* chart-tag regex cross-pin between the backend hook and the frontend
  harvester (answer-segments.ts) so the two can never silently drift.

All payloads are SYNTHETIC (no dataset literals); everything runs offline.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import agents.system_agent as sa
from agents.orchestrator import _CHART_TAG_RE, ChartRedactionHook

TAG_A = '<chart-block type="line" title="series-a" labels="[1,2]" data="[10,20]"/>'
TAG_B = '<chart-block type="line" title="series-b" labels="[1,2]" data="[30,40]"/>'


# --------------------------------------------------------------------------- #
# Inner-agent hook registration + deterministic relay
# --------------------------------------------------------------------------- #
def test_system_agent_hooks_include_chart_redaction():
    hooks = sa._system_agent_hooks("sys-x", "PostgreSQL", "", [])
    assert any(isinstance(h, ChartRedactionHook) for h in hooks)


def test_relay_appends_drained_tags_and_clears_queue(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    agent = SimpleNamespace(_pending_chart_blocks=[TAG_A, TAG_B])
    out = sa._relay_inner_chart_tags(agent, "The temperature rose steadily.")
    assert out.startswith("The temperature rose steadily.")
    assert TAG_A in out and TAG_B in out
    assert agent._pending_chart_blocks == []


def test_relay_has_no_session_dedup(monkeypatch):
    """The inner agent is a long-lived runtime singleton shared by every
    caller session — a later request for the same chart must relay again
    (cross-turn dedup belongs to the ORCHESTRATOR layer)."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    agent = SimpleNamespace(_pending_chart_blocks=[TAG_A])
    first = sa._relay_inner_chart_tags(agent, "answer one")
    assert TAG_A in first
    agent._pending_chart_blocks.append(TAG_A)
    second = sa._relay_inner_chart_tags(agent, "answer two")
    assert TAG_A in second


def test_relay_skips_tags_already_in_answer(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    agent = SimpleNamespace(_pending_chart_blocks=[TAG_A])
    out = sa._relay_inner_chart_tags(agent, f"already inline: {TAG_A}")
    assert out.count(TAG_A) == 1


def test_relay_enforce_mode_drains_and_drops(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    agent = SimpleNamespace(_pending_chart_blocks=[TAG_A])
    out = sa._relay_inner_chart_tags(agent, '{"answer": "gated"}')
    assert out == '{"answer": "gated"}'
    assert agent._pending_chart_blocks == []  # drained, never accumulates


def test_relay_is_fail_soft():
    class _Hostile:
        @property
        def _pending_chart_blocks(self):
            raise RuntimeError("boom")

    assert sa._relay_inner_chart_tags(_Hostile(), "answer") == "answer"


def test_mode_a_query_path_calls_the_relay():
    src = inspect.getsource(sa._run_mode_a_query) + inspect.getsource(sa._finish_mode_a_answer)
    assert "_relay_inner_chart_tags" in src


# --------------------------------------------------------------------------- #
# I3X_TLS_VERIFY passthrough to AgentCore runtimes
# --------------------------------------------------------------------------- #
def _base_env(monkeypatch):
    from agents.agent_manager import AgentManager
    return AgentManager._base_env_vars(
        "sys-x", "System X", "schema_x", "SiteWise (i3X)", "us-east-1")


def test_i3x_tls_verify_forwarded_to_runtime_env(monkeypatch):
    monkeypatch.setenv("I3X_TLS_VERIFY", "false")
    assert _base_env(monkeypatch)["I3X_TLS_VERIFY"] == "false"


def test_i3x_tls_verify_defaults_on(monkeypatch):
    monkeypatch.delenv("I3X_TLS_VERIFY", raising=False)
    assert _base_env(monkeypatch)["I3X_TLS_VERIFY"] == "true"


# --------------------------------------------------------------------------- #
# Content-derived equipment ValueMapping ids
# --------------------------------------------------------------------------- #
def test_equipment_vm_id_is_content_derived_and_order_free():
    from agents.onboard import _equipment_vm_id
    vm = {"from_system": "reg", "from_field": "asset_tag", "from_value": "TAG-1",
          "to_system": "hist", "to_field": "elementId", "to_value": "obj-1"}
    first = _equipment_vm_id(vm)
    assert first.startswith("vm-equip-")
    # Same content ⇒ same id (idempotent MERGE across provisions, regardless
    # of enumeration order); different content ⇒ different id.
    assert _equipment_vm_id(dict(vm)) == first
    assert _equipment_vm_id({**vm, "to_value": "obj-2"}) != first


def test_equipment_vm_writers_use_the_content_key():
    """The LIVE writer (_persist_equipment_value_mappings, the V2 of the
    retired c3b block) uses the content-derived key. The legacy c3b block
    inside _do_provision keeps the old f-string — it is DEAD code (gated on a
    catalog status value that never exists) inside a gate-frozen >50-line
    function; editing it would trip the test_response_integrity_iteration5/6 maintainability gates."""
    import agents.onboard as onboard
    src = inspect.getsource(onboard._persist_equipment_value_mappings)
    assert "_equipment_vm_id(vm)" in src
    assert "vm-equip-{" not in src  # the old index-keyed f-string is gone


# --------------------------------------------------------------------------- #
# Provision walk returns honest counters + warnings
# --------------------------------------------------------------------------- #
def test_provision_walk_failure_returns_warning_not_silence(monkeypatch):
    import agents.onboard_i3x as oi

    def boom(*_a, **_k):
        raise RuntimeError("TLS handshake failed")

    monkeypatch.setattr(oi, "make_i3x_client", boom)
    result = oi.provision_i3x_hierarchy("sys-x", "System X", "https://i3x.example")
    assert result["roots"] == 0 and result["assets"] == 0
    assert any("walk failed" in w for w in result["warnings"])


def test_provision_zero_asset_walk_is_flagged(monkeypatch):
    import agents.onboard_i3x as oi

    monkeypatch.setattr(oi, "make_i3x_client",
                        lambda *a, **k: (lambda *_a, **_k: [], lambda *_a, **_k: []))
    monkeypatch.setattr(oi, "_i3x_type_names", lambda _get: {})
    monkeypatch.setattr(oi, "neptune_write", lambda *_a, **_k: None)
    monkeypatch.setattr(oi, "neptune_query", lambda *_a, **_k: [{"n": 0}])
    result = oi.provision_i3x_hierarchy("sys-x", "System X", "https://i3x.example")
    assert result["assets"] == 0
    assert any("ZERO Asset" in w for w in result["warnings"])


def test_provision_counters_reflect_graph_counts(monkeypatch):
    import agents.onboard_i3x as oi

    roots = [{"elementId": "root-1", "displayName": "Root"}]
    monkeypatch.setattr(oi, "make_i3x_client",
                        lambda *a, **k: (lambda *_a, **_k: roots, lambda *_a, **_k: []))
    monkeypatch.setattr(oi, "_i3x_type_names", lambda _get: {})
    monkeypatch.setattr(oi, "_write_root", lambda *_a, **_k: None)
    monkeypatch.setattr(oi, "neptune_write", lambda *_a, **_k: None)
    counts = iter([[{"n": 7}], [{"n": 21}]])
    monkeypatch.setattr(oi, "neptune_query", lambda *_a, **_k: next(counts))
    result = oi.provision_i3x_hierarchy("sys-x", "System X", "https://i3x.example")
    assert result == {"roots": 1, "assets": 7, "properties": 21, "warnings": []}


# --------------------------------------------------------------------------- #
# Backend/frontend chart-tag regex cross-pin
# --------------------------------------------------------------------------- #
def test_chart_tag_regex_pinned_across_backend_and_frontend():
    """The hook (Python), the harvester (answer-segments.ts) and the summarizer
    keep private copies of the tag regex. Pin the shared core so they can never
    silently drift (title-attr QUOTE style differs by design: the server emits
    double quotes; the frontend tolerates both — content-hash dedup makes that
    cosmetic)."""
    core = r"<chart-block\b[^>]*"
    assert _CHART_TAG_RE.pattern == core + "/>"
    ts_path = (Path(sa.__file__).resolve().parents[1]
               / "frontend" / "src" / "chat" / "answer-segments.ts")
    ts_src = ts_path.read_text(encoding="utf-8")
    assert "/<chart-block\\b[^>]*\\/>/g" in ts_src
