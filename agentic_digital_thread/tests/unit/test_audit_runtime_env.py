# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/audit_runtime_env.py — pure diff logic, offline (stubbed control).

Pins the three COMPUTED drift classes and their gating semantics:
missing/stale gate ``--strict``; foreign is informational only; operator-owned
keys (SYSTEM_REQUIRED_GROUP) are NEVER flagged foreign; live-swap keys
(BEDROCK_MODEL et al., ADR-0055) are NEVER stale; report
output is digest-only — env values never appear.
"""

import dataclasses
import importlib.util
import sys
from pathlib import Path

from agents.runtime_env_contract import (
    ONBOARDING_SESSIONS_TABLE_DEFAULT,
    RuntimeEnvSources,
    build_env,
)

_ADT_ROOT = Path(__file__).resolve().parents[2]


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "audit_runtime_env", _ADT_ROOT / "scripts" / "audit_runtime_env.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("audit_runtime_env", module)
    spec.loader.exec_module(module)
    return module


audit = _load_audit()

_SOURCES = RuntimeEnvSources(
    region="us-east-1", graph_id="g-1", gateway_url="https://gw",
    gateway_id="GW1", app_service_client_id="c1", app_user_pool_id="p1",
    onboarding_sessions_table="live-table")


def _healthy_orch_env() -> dict:
    return build_env("orchestrator", _SOURCES)


# --- role classification ------------------------------------------------------

def test_classify_role_by_runtime_name():
    assert audit.classify_role("adt_orchestrator") == "orchestrator"
    assert audit.classify_role("adt_onboarding") == "onboarding"
    assert audit.classify_role("adt_erp") == "system"
    assert audit.classify_role("adt_digital_thread") == "system"
    assert audit.classify_role("someone_elses_runtime") is None


# --- the three classes --------------------------------------------------------

def test_healthy_runtime_has_no_findings():
    finding = audit.audit_runtime_env(
        "adt_orchestrator", _healthy_orch_env(), _SOURCES)
    assert finding.missing == [] and finding.stale == [] and finding.foreign == []
    assert finding.gating is False


def test_missing_names_the_starved_required_key():
    env = _healthy_orch_env()
    env["ONBOARDING_SESSIONS_TABLE"] = ""
    finding = audit.audit_runtime_env("adt_orchestrator", env, _SOURCES)
    assert finding.missing == ["ONBOARDING_SESSIONS_TABLE"]
    assert finding.gating is True


def test_empty_identity_arn_is_not_missing_allow_all_is_legitimate():
    env = build_env("system", _SOURCES, system_id="erp", system_name="E",
                    schema="s", connection_type="PostgreSQL",
                    system_agent_model="m")
    allow_all_value = ""
    env["SYSTEM_IDENTITY_CLAIM_SECRET_ARN"] = allow_all_value  # ADR-0049: empty = allow-all
    finding = audit.audit_runtime_env("adt_erp", env, _SOURCES)
    assert finding.missing == []


def test_stale_fires_on_cfn_derived_drift_with_digests_only():
    env = _healthy_orch_env()
    env["AGENTCORE_GATEWAY_URL"] = "https://old-gw"
    finding = audit.audit_runtime_env("adt_orchestrator", env, _SOURCES)
    keys = [k for k, _, _ in finding.stale]
    assert keys == ["AGENTCORE_GATEWAY_URL"]
    report = audit.render_report([finding])
    assert "https://old-gw" not in report and "https://gw" not in report
    assert "AGENTCORE_GATEWAY_URL" in report


def test_model_swapped_env_is_not_stale():
    """BEDROCK_MODEL swaps live (ADR-0055) — never stale."""
    env = _healthy_orch_env()
    env["BEDROCK_MODEL"] = "us.anthropic.claude-sonnet-5"
    env["SYSTEM_QUERY_MODE"] = "orchestrator"  # sticky-SSM flip
    finding = audit.audit_runtime_env("adt_orchestrator", env, _SOURCES)
    assert finding.stale == []
    assert finding.gating is False


def test_unset_expectation_never_alarms_stale():
    env = _healthy_orch_env()
    env["GRAPH_IDENTIFIER"] = "g-other"
    bare = RuntimeEnvSources(region="us-east-1")  # no graph expectation
    finding = audit.audit_runtime_env("adt_orchestrator", env, bare)
    assert all(k != "GRAPH_IDENTIFIER" for k, _, _ in finding.stale)


def test_foreign_is_informational_and_operator_keys_never_flag():
    env = build_env("system", _SOURCES, system_id="erp", system_name="E",
                    schema="s", connection_type="PostgreSQL",
                    system_agent_model="m")
    env["SYSTEM_REQUIRED_GROUP"] = "plant-ops"   # operator-owned: allowlisted
    connection_arn = "arn:conn"
    env["CONNECTION_SECRET_ARN"] = connection_arn  # composed per-system: known
    env["TOTALLY_UNKNOWN_KEY"] = "x"
    finding = audit.audit_runtime_env("adt_erp", env, _SOURCES)
    assert finding.foreign == ["TOTALLY_UNKNOWN_KEY"]
    assert finding.gating is False  # foreign NEVER gates --strict


# --- audit_all + strict exit --------------------------------------------------

def _runtimes():
    return [{"agentRuntimeName": n, "agentRuntimeId": n}
            for n in ("adt_orchestrator", "adt_erp", "not_ours")]


def test_audit_all_skips_non_adt_runtimes():
    envs = {"adt_orchestrator": _healthy_orch_env(), "adt_erp": {}}
    findings = audit.audit_all(
        _runtimes(), lambda rt: envs.get(rt["agentRuntimeName"], {}), _SOURCES)
    assert [f.runtime_name for f in findings] == ["adt_orchestrator", "adt_erp"]


def test_starved_runtime_flips_strict_with_the_key_named(monkeypatch, capsys):
    starved = {"SYSTEM_ID": "erp"}  # no ONBOARDING_SESSIONS_TABLE

    class _Control:
        def list_agent_runtimes(self, **kwargs):
            return {"agentRuntimes": [
                {"agentRuntimeName": "adt_erp", "agentRuntimeId": "rt-1"}]}

        def get_agent_runtime(self, agentRuntimeId):
            return {"environmentVariables": starved}

    class _Boto3:
        @staticmethod
        def client(*a, **k):
            return _Control()

    monkeypatch.setitem(sys.modules, "boto3", _Boto3)
    rc = audit.main(["--strict", "--region", "us-east-1"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "missing: ONBOARDING_SESSIONS_TABLE" in out
    assert ONBOARDING_SESSIONS_TABLE_DEFAULT not in out  # digest-only logging


def test_healthy_fleet_exits_zero_under_strict(monkeypatch, capsys):
    class _Control:
        def list_agent_runtimes(self, **kwargs):
            return {"agentRuntimes": [
                {"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "r1"}]}

        def get_agent_runtime(self, agentRuntimeId):
            return {"environmentVariables": _healthy_orch_env()}

    class _Boto3:
        @staticmethod
        def client(*a, **k):
            return _Control()

    monkeypatch.setitem(sys.modules, "boto3", _Boto3)
    monkeypatch.setattr(audit, "RuntimeEnvSources",
                        type("S", (), {"from_ambient":
                                       staticmethod(lambda **kw: _SOURCES)}))
    rc = audit.main(["--strict"])
    assert rc == 0
    assert "0 with missing/stale drift" in capsys.readouterr().out


# --- current-replica gate-health annotation ---------------------------------

_GATE_PAYLOAD = {
    "scope": "current-replica",
    "admission": {
        "state": "busy",
        "current_owner_kind": "briefing",
        "tokens_outstanding": 1,
        "briefings_admitted": 3,
        "leak_signal": True,
        "leak_threshold_seconds": 3600,
        "oldest_token_age_seconds": 3601.0,
    },
    "card_live_mode": "poll-only",
    "ask_stream_queue_bound": 512,
}


def test_gate_health_report_is_measured_or_absent_and_has_no_verdict():
    text = audit.render_gate_health(_GATE_PAYLOAD)
    assert "admission=busy owner=briefing outstanding=1" in text
    assert "briefings_admitted=3" in text
    assert "briefings_skipped_busy" not in text
    assert "LEAK signal=true oldest_seconds=3601.0 threshold_seconds=3600" in text
    assert "card_live_mode=poll-only" in text
    assert "ask_stream_queue_bound=512" in text
    assert "PASS" not in text and "FAIL" not in text


def test_gate_health_annotation_never_changes_strict_exit(monkeypatch, capsys):
    class _Control:
        def list_agent_runtimes(self, **kwargs):
            return {"agentRuntimes": [
                {"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "r1"}]}

        def get_agent_runtime(self, agentRuntimeId):
            return {"environmentVariables": _healthy_orch_env()}

    class _Boto3:
        @staticmethod
        def client(*args, **kwargs):
            return _Control()

    monkeypatch.setitem(sys.modules, "boto3", _Boto3)
    monkeypatch.setattr(audit, "RuntimeEnvSources",
                        type("S", (), {"from_ambient":
                                       staticmethod(lambda **kw: _SOURCES)}))
    monkeypatch.setattr(audit, "fetch_gate_health",
                        lambda *args, **kwargs: _GATE_PAYLOAD)

    rc = audit.main(["--strict", "--base-url", "https://app.example",
                     "--password", "not-printed"])

    output = capsys.readouterr().out
    assert rc == 0
    assert "LEAK signal=true" in output
    assert "not-printed" not in output


def test_gate_health_unavailable_annotation_includes_safe_http_status():
    class _Forbidden(Exception):
        code = 403

    text = audit.render_gate_health_error(_Forbidden("credential detail"))

    assert text == "gate-health unavailable (HTTP status=403) — annotation only"
    assert "credential detail" not in text


def test_fetch_gate_health_reuses_shared_lane_login_client(monkeypatch):
    calls = []

    class _App:
        def __init__(self, base_url):
            calls.append(("init", base_url, None, None))

        def call(self, method, path, body=None, timeout=60):
            calls.append((method, path, body, timeout))
            return {"status": "ok"} if path == "/api/auth/login" else _GATE_PAYLOAD

    monkeypatch.setitem(
        sys.modules, "scripts.smoke_prompts", type("Smoke", (), {"App": _App}))
    payload = audit.fetch_gate_health(
        "https://app.example/", "operator", "not-printed", timeout=7)

    assert payload == _GATE_PAYLOAD
    assert calls == [
        ("init", "https://app.example/", None, None),
        ("POST", "/api/auth/login",
         {"email": "operator", "password": "not-printed"}, 7),
        ("GET", "/api/admin/agents/gate-health", None, 7),
    ]


def test_gate_health_fetch_failure_is_annotation_only_and_keeps_drift_report(
        monkeypatch, capsys):
    class _Control:
        def list_agent_runtimes(self, **kwargs):
            return {"agentRuntimes": [
                {"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "r1"}]}

        def get_agent_runtime(self, agentRuntimeId):
            env = _healthy_orch_env()
            env["ONBOARDING_SESSIONS_TABLE"] = ""
            return {"environmentVariables": env}

    class _Boto3:
        @staticmethod
        def client(*args, **kwargs):
            return _Control()

    monkeypatch.setitem(sys.modules, "boto3", _Boto3)
    monkeypatch.setattr(audit, "RuntimeEnvSources",
                        type("S", (), {"from_ambient":
                                       staticmethod(lambda **kw: _SOURCES)}))
    monkeypatch.setattr(audit, "fetch_gate_health",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))

    rc = audit.main(["--base-url", "https://app.example",
                     "--password", "not-printed"])

    output = capsys.readouterr().out
    assert rc == 0
    assert "missing/stale drift" in output
    assert "gate-health unavailable (OSError) — annotation only" in output

# --- per-runtime error isolation -------------------------------------------
# A live AgentCredentialProvider discovery-document failure was observed
# killing the whole lane, so one unreadable runtime destroyed the drift
# report for every other runtime. These pin the isolation.

def test_one_unreadable_runtime_does_not_lose_the_other_findings(monkeypatch):
    """A transient per-runtime API error must not abort audit_all."""
    import scripts.audit_runtime_env as m

    runtimes = [{"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "a"},
                {"agentRuntimeName": "adt_erp", "agentRuntimeId": "b"}]

    def get_env(runtime):
        if runtime["agentRuntimeId"] == "a":
            raise RuntimeError("discovery document fetch failed")
        return {}

    sources = m.RuntimeEnvSources(region="us-east-1")
    findings = m.audit_all(runtimes, get_env, sources)
    names = [f.runtime_name for f in findings]
    assert "adt_orchestrator" in names and "adt_erp" in names, names
    bad = [f for f in findings if f.runtime_name == "adt_orchestrator"][0]
    assert bad.unauditable == "RuntimeError"
    # The readable runtime was still audited for real drift.
    good = [f for f in findings if f.runtime_name == "adt_erp"][0]
    assert good.unauditable == ""


def test_unauditable_never_gates_strict():
    """Drift-unknown must not flap a pre-push gate on a transient AWS error."""
    import scripts.audit_runtime_env as m
    f = m.RuntimeFinding(runtime_name="adt_erp", role="system",
                         unauditable="ValidationException")
    assert f.gating is False
    out = m.render_report([f])
    assert "UNAUDITABLE" in out and "ValidationException" in out
    assert "drift unknown" in out


def test_unauditable_reason_carries_no_message_detail():
    """Only the exception TYPE — a message can contain ARNs/endpoints."""
    import scripts.audit_runtime_env as m

    def get_env(runtime):
        raise RuntimeError("arn:aws:cognito-idp:us-east-1:111122223333:pool/x")

    sources = m.RuntimeEnvSources(region="us-east-1")
    findings = m.audit_all(
        [{"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "a"}],
        get_env, sources)
    rendered = m.render_report(findings)
    assert "arn:aws" not in rendered and "111122223333" not in rendered
    assert findings[0].unauditable == "RuntimeError"


def test_absent_boot_required_key_is_missing_not_ok():
    """Measured live (ADR-0069): a pre-ADR-0069 onboarding runtime with NO
    GRAPH_IDENTIFIER at all audited `ok` — absent keys were never stale-checked
    and `missing` covered only the fill-policy keys — while the boot gate
    would refuse to start that runtime. The audit's notion of "missing" is the
    contract's own `missing_required`, so a pre-deploy audit names exactly
    what the boot gate would fail on."""
    env = build_env("onboarding", _SOURCES)
    del env["GRAPH_IDENTIFIER"]
    finding = audit.audit_runtime_env("adt_onboarding", env, _SOURCES)
    assert "GRAPH_IDENTIFIER" in finding.missing
    assert finding.gating is True


# --- MEMORY_LTM_SCOPE posture (workshop shared-login memory scoping) ----------

def _workshop_orch_env() -> dict:
    return build_env("orchestrator", dataclasses.replace(_SOURCES, workshop=True))


def test_workshop_memory_scope_is_known_and_reported_as_posture():
    finding = audit.audit_runtime_env(
        "adt_orchestrator", _workshop_orch_env(), _SOURCES)
    assert finding.foreign == [] and finding.gating is False
    assert finding.posture == {"MEMORY_LTM_SCOPE": "session"}
    assert "posture: MEMORY_LTM_SCOPE=session" in audit.render_report([finding])


def test_absent_memory_scope_reports_the_actor_default():
    finding = audit.audit_runtime_env(
        "adt_orchestrator", _healthy_orch_env(), _SOURCES)
    assert finding.posture == {"MEMORY_LTM_SCOPE": "actor (default)"}


def test_unrecognised_memory_scope_is_labelled_never_echoed_and_never_gates():
    env = _healthy_orch_env()
    env["MEMORY_LTM_SCOPE"] = "secret-looking-value"
    finding = audit.audit_runtime_env("adt_orchestrator", env, _SOURCES)
    assert finding.posture == {"MEMORY_LTM_SCOPE": "unrecognised"}
    assert finding.gating is False
    assert "secret-looking-value" not in audit.render_report([finding])


def test_memory_scope_posture_is_orchestrator_only():
    env = build_env("onboarding", _SOURCES)
    finding = audit.audit_runtime_env("adt_onboarding", env, _SOURCES)
    assert finding.posture == {}
