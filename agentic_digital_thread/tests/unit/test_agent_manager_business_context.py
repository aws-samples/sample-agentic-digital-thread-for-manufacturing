# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Business-context DELIVERY path (T6 / ADR-0054 D4) — agent_manager + runtime_app.

Pins the three delivery guarantees:

1. **Refresh without reonboard** — ``AgentManager.refresh_business_context``
   does a READ-MODIFY-WRITE merge of the ``adt/agent-config/<id>`` secret
   (``get_secret_value`` -> set/remove ONLY the ``business_context`` key ->
   ``put_secret_value``) so every sibling key (system_prompt / tables_desc /
   concept_mappings / connection creds) is preserved byte-for-byte — the exact
   opposite of ``resync_cloud_connection``'s whole-value overwrite — then
   bumps the runtime version via ``update_agent_runtime`` so the replacement
   container re-reads the secret. Truthful tri-state: ok | failed | skipped.

2. **Re-provision preservation** — ``_start_cloud`` (the wrapper around the
   frozen 468-line ``_start_cloud_base``) re-reads the System node's business
   context (graph = source of truth) and re-includes the key in the freshly
   composed secret payload, so a re-onboard never silently drops steward
   content. An UNREADABLE graph is NOT unset: the existing secret's delivered
   value is carried forward (``resync_cloud_connection`` converges on the
   same ``_apply_business_context`` path — MED-B/C).

3. **Runtime ride-through** — ``runtime_app._extract_prompt_and_conn_params``
   pops system_prompt/tables_desc but deliberately NOT business_context: like
   concept_mappings it rides in connection_config to ``create_system_agent``,
   which pops it before tool building (pinned by T4's
   test_system_agent_business_context.py).

All AWS/graph access is stubbed — no sockets, no boto3 network calls.
"""
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agent_manager import (  # noqa: E402
    AgentManager,
    _business_context_secret_value,
)

_SIBLINGS = {
    "workgroup": "primary",
    "database": "erp",
    "host": "db.example.internal",
    "password": "pw-SECRET-XYZ",
    "system_prompt": "You are the ERP agent.",
    "tables_desc": "orders(id: int, qty: int)",
    "concept_mappings": [{"concept": "work_order", "field": "orders.id"}],
}

_ENABLED_STATE = {"business_context": "Plant 2 runs three shifts.",
                  "enabled": True, "updated_at": "2026-07-27T00:00:00Z",
                  "context_hash": "abc123def456"}
_DISABLED_STATE = {**_ENABLED_STATE, "enabled": False}
_EMPTY_STATE = {"business_context": "", "enabled": True,
                "updated_at": None, "context_hash": None}
# The honest-empty READABLE shape (genuinely unset) — distinct from None
# (graph read FAILED); see business_context.read_business_context (MED-B).
_UNSET_STATE = {"business_context": "", "enabled": False,
                "updated_at": None, "context_hash": None}


class _FakeSecrets:
    """Secrets Manager stand-in with a readable current value (RMW support)."""

    class ResourceNotFoundException(Exception):
        pass

    class ResourceExistsException(Exception):
        pass

    def __init__(self, current: dict | None = None, exists: bool = True):
        self._current = current
        self._exists = exists
        self.get_calls = []
        self.put_calls = []
        self.create_calls = []
        self.exceptions = types.SimpleNamespace(
            ResourceNotFoundException=self.ResourceNotFoundException,
            ResourceExistsException=self.ResourceExistsException,
        )

    def get_secret_value(self, SecretId=None):  # noqa: N803 - boto3 kwarg name
        self.get_calls.append(SecretId)
        if self._current is None:
            raise self.ResourceNotFoundException(SecretId)
        return {"SecretString": json.dumps(self._current)}

    def put_secret_value(self, SecretId=None, SecretString=None):  # noqa: N803
        self.put_calls.append({"SecretId": SecretId, "SecretString": SecretString})
        return {"ARN": f"arn:aws:secretsmanager:::secret:{SecretId}"}

    def create_secret(self, **kwargs):
        self.create_calls.append(kwargs)
        if self._exists:
            raise self.ResourceExistsException()
        return {"ARN": f"arn:aws:secretsmanager:::secret:{kwargs.get('Name')}"}

    def describe_secret(self, SecretId=None):  # noqa: N803 - boto3 kwarg name
        return {"ARN": f"arn:aws:secretsmanager:::secret:{SecretId}"}


class _FakeControl:
    """bedrock-agentcore-control stand-in (READY runtime, update recorded)."""

    def __init__(self, runtime_found: bool = True, update_error: bool = False,
                 runtime_name: str = "adt_erp"):
        self._runtime_found = runtime_found
        self._update_error = update_error
        self._runtime_name = runtime_name
        self.update_calls = []

    def list_agent_runtimes(self, **kwargs):
        if not self._runtime_found:
            return {"agentRuntimes": []}
        return {"agentRuntimes": [{
            "agentRuntimeName": self._runtime_name, "agentRuntimeId": "rt-1",
            "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/rt-1",
            "status": "READY",
        }]}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        return {
            "status": "READY",
            "environmentVariables": {"SYSTEM_ID": "erp", "AWS_REGION": "us-east-1"},
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "uri:1"}},
            "roleArn": "arn:role",
            "networkConfiguration": {
                "networkMode": "VPC",
                "networkModeConfig": {"subnets": ["s-1"], "securityGroups": ["sg-1"],
                                      "requireServiceS3Endpoint": True},
            },
            "authorizerConfiguration": {
                "customJWTAuthorizer": {"discoveryUrl": "https://d/",
                                        "allowedClients": ["c"]}},
        }

    def update_agent_runtime(self, **kwargs):
        if self._update_error:
            raise RuntimeError("ValidationException: update failed")
        self.update_calls.append(kwargs)
        return {"agentRuntimeId": kwargs.get("agentRuntimeId")}


def _fake_boto3(secrets, control):
    def _client(service, region_name=None):
        if service == "secretsmanager":
            return secrets
        if service == "bedrock-agentcore-control":
            return control
        raise AssertionError(f"unexpected boto3 client: {service}")
    return types.SimpleNamespace(client=_client)


def _mgr(mode="cloud"):
    return AgentManager(mode=mode, gateway_id="", gateway_url="https://gw/mcp",
                        ecr_image_uri="uri:1", runtime_role_arn="arn:role")


def _patch_graph_state(state):
    return patch("agents.business_context.read_business_context",
                 lambda system_id: dict(state))


# --------------------------------------------------------------------------- #
# refresh_business_context — the merge + version bump (no reonboard)
# --------------------------------------------------------------------------- #

def test_refresh_merges_and_preserves_sibling_secret_keys():
    """THE sibling-key-preservation pin: after a refresh, every pre-existing
    secret key is byte-equal and business_context is ADDED — never resync's
    whole-value overwrite."""
    secrets = _FakeSecrets(current=dict(_SIBLINGS))
    control = _FakeControl(runtime_found=True)
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
            _patch_graph_state(_ENABLED_STATE):
        outcome = _mgr().refresh_business_context("erp")

    assert outcome == "ok"
    assert secrets.get_calls == ["adt/agent-config/erp"], secrets.get_calls
    assert len(secrets.put_calls) == 1, secrets.put_calls
    assert secrets.put_calls[0]["SecretId"] == "adt/agent-config/erp"
    merged = json.loads(secrets.put_calls[0]["SecretString"])
    for key, value in _SIBLINGS.items():
        assert merged[key] == value, f"sibling key {key!r} clobbered: {merged}"
    assert merged["business_context"] == _ENABLED_STATE["business_context"]
    # Version bump: exactly one update_agent_runtime on the existing runtime.
    assert len(control.update_calls) == 1, control.update_calls
    upd = control.update_calls[0]
    assert upd["agentRuntimeId"] == "rt-1", upd
    assert "requireServiceS3Endpoint" not in upd["networkConfiguration"]["networkModeConfig"]
    assert upd["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == ["c"]


def test_refresh_removes_key_when_disabled_or_empty():
    """Disabled or empty context ⇒ the key is REMOVED from the secret (siblings
    intact) and the runtime is still bumped so the change takes effect."""
    for state in (_DISABLED_STATE, _EMPTY_STATE):
        stored = {**_SIBLINGS, "business_context": "old steward text"}
        secrets = _FakeSecrets(current=stored)
        control = _FakeControl(runtime_found=True)
        with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
                _patch_graph_state(state):
            outcome = _mgr().refresh_business_context("erp")
        assert outcome == "ok", state
        merged = json.loads(secrets.put_calls[0]["SecretString"])
        assert "business_context" not in merged, merged
        for key, value in _SIBLINGS.items():
            assert merged[key] == value, f"sibling key {key!r} clobbered"
        assert len(control.update_calls) == 1


def test_refresh_returns_failed_on_update_runtime_error():
    """Secret merge succeeded but the version bump failed ⇒ 'failed' (truthful
    — the API reports runtime_refresh honestly), never an exception."""
    secrets = _FakeSecrets(current=dict(_SIBLINGS))
    control = _FakeControl(runtime_found=True, update_error=True)
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
            _patch_graph_state(_ENABLED_STATE):
        outcome = _mgr().refresh_business_context("erp")
    assert outcome == "failed"
    assert len(secrets.put_calls) == 1  # durable merge DID land


def test_refresh_skipped_in_local_mode():
    """Local mode has no cloud runtime/secret channel ⇒ 'skipped', zero AWS calls."""
    boto3_guard = MagicMock(side_effect=AssertionError("boto3 must not be used"))
    with patch("agents.agent_manager.boto3",
               types.SimpleNamespace(client=boto3_guard)):
        outcome = _mgr(mode="local").refresh_business_context("erp")
    assert outcome == "skipped"
    boto3_guard.assert_not_called()


def test_refresh_skipped_when_secret_missing():
    """No adt/agent-config secret (never provisioned / KG system) ⇒ 'skipped'."""
    secrets = _FakeSecrets(current=None)
    control = _FakeControl(runtime_found=True)
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
            _patch_graph_state(_ENABLED_STATE):
        outcome = _mgr().refresh_business_context("erp")
    assert outcome == "skipped"
    assert secrets.put_calls == []
    assert control.update_calls == []


def test_refresh_skipped_when_no_runtime():
    """Secret merged but no cloud runtime exists ⇒ 'skipped' (D4: skipped =
    local mode / no cloud runtime), NOT 'failed'."""
    secrets = _FakeSecrets(current=dict(_SIBLINGS))
    control = _FakeControl(runtime_found=False)
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
            _patch_graph_state(_ENABLED_STATE):
        outcome = _mgr().refresh_business_context("erp")
    assert outcome == "skipped"
    assert len(secrets.put_calls) == 1  # durable merge DID land


def test_refresh_failed_on_graph_read_error():
    def _boom(system_id):
        raise RuntimeError("neptune down")
    secrets = _FakeSecrets(current=dict(_SIBLINGS))
    control = _FakeControl()
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
            patch("agents.business_context.read_business_context", _boom):
        outcome = _mgr().refresh_business_context("erp")
    assert outcome == "failed"
    assert secrets.put_calls == []


def test_refresh_failed_when_graph_unreadable_zero_secret_writes():
    """THE MED-B honesty pin: read_business_context() -> None means the graph
    read FAILED (a Neptune blip), NOT "no context set". The refresh must
    report 'failed' and perform ZERO secret operations — the old behavior
    (treating [] as unset) would REMOVE delivered context from the secret and
    report runtime_refresh='ok', a silent drop dressed as success."""
    secrets = _FakeSecrets(current={**_SIBLINGS, "business_context": "delivered"})
    control = _FakeControl(runtime_found=True)
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
            patch("agents.business_context.read_business_context",
                  lambda system_id: None):
        outcome = _mgr().refresh_business_context("erp")
    assert outcome == "failed"
    assert secrets.get_calls == []       # the secret was never even read
    assert secrets.put_calls == []       # zero secret mutation
    assert control.update_calls == []    # no runtime bump either


def test_refresh_genuinely_unset_removal_still_works():
    """The counterpart pin: a READABLE honest-empty state (genuinely unset /
    cleared) still removes the key — MED-B must not break legitimate
    removal."""
    secrets = _FakeSecrets(current={**_SIBLINGS, "business_context": "old text"})
    control = _FakeControl(runtime_found=True)
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
            _patch_graph_state(_UNSET_STATE):
        outcome = _mgr().refresh_business_context("erp")
    assert outcome == "ok"
    merged = json.loads(secrets.put_calls[0]["SecretString"])
    assert "business_context" not in merged, merged
    for key, value in _SIBLINGS.items():
        assert merged[key] == value, f"sibling key {key!r} clobbered"
    assert len(control.update_calls) == 1


def test_refresh_never_logs_secret_or_context_values():
    """Sibling secret values AND the steward text must never reach the logs —
    only system id, length, and hash."""
    secrets = _FakeSecrets(current=dict(_SIBLINGS))
    control = _FakeControl(runtime_found=True)
    fake_logger = MagicMock()
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
            patch("agents.agent_manager.logger", fake_logger), \
            _patch_graph_state(_ENABLED_STATE):
        outcome = _mgr().refresh_business_context("erp")
    assert outcome == "ok"
    rendered = []
    for level in (fake_logger.info, fake_logger.warning, fake_logger.error):
        for call in level.call_args_list:
            args = call.args
            if not args:
                continue
            try:
                rendered.append(str(args[0]) % tuple(args[1:]))
            except Exception:
                rendered.append(" ".join(str(a) for a in args))
    blob = " ".join(rendered)
    assert "pw-SECRET-XYZ" not in blob, blob
    assert _ENABLED_STATE["business_context"] not in blob, blob


# --------------------------------------------------------------------------- #
# _merged_agent_config_payload — the pure RMW seam
# --------------------------------------------------------------------------- #

def test_merged_payload_rejects_non_object_secret():
    import pytest
    with pytest.raises(ValueError):
        AgentManager._merged_agent_config_payload("[1, 2]", _ENABLED_STATE)


def test_business_context_secret_value_include_rule():
    """The ONE include rule: text iff enabled AND non-empty; else None."""
    assert _business_context_secret_value(_ENABLED_STATE) == \
        _ENABLED_STATE["business_context"]
    assert _business_context_secret_value(_DISABLED_STATE) is None
    assert _business_context_secret_value(_EMPTY_STATE) is None
    assert _business_context_secret_value(None) is None
    assert _business_context_secret_value(
        {"business_context": "   ", "enabled": True}) is None


# --------------------------------------------------------------------------- #
# _start_cloud wrapper — re-provision re-include (D4 preservation)
# --------------------------------------------------------------------------- #

def _run_start_cloud(secrets, control, connection_config=None):
    mgr = _mgr()
    with patch("boto3.client", _fake_boto3(secrets, control).client), \
            patch.object(AgentManager, "_warmup_runtime",
                         lambda self, url, region: None), \
            patch.object(AgentManager, "_ensure_credential_provider",
                         lambda self, control, region: ""):
        return mgr._start_cloud(
            system_id="erp", system_name="ERP", schema="public",
            system_prompt="You are the ERP agent.",
            tables_desc="orders(id: int, qty: int)",
            connection_type="PostgreSQL",
            connection_config=connection_config or {"host": "db.example.internal"},
            conn_params={"host": "db.example.internal", "port": 5432,
                         "dbname": "erp", "user": "u", "password": "pw-SECRET-XYZ"},
        )


def test_start_cloud_payload_includes_enabled_context():
    """THE re-provision-re-include pin: a re-onboard composes a fresh secret —
    the System node's enabled context must be re-included alongside the
    prompt/schema sibling keys, and must NOT leak into the runtime env."""
    secrets = _FakeSecrets(current=None, exists=False)
    control = _FakeControl(runtime_found=True, runtime_name="adt_erp")
    with _patch_graph_state(_ENABLED_STATE):
        _run_start_cloud(secrets, control)

    assert len(secrets.create_calls) == 1, secrets.create_calls
    payload = json.loads(secrets.create_calls[0]["SecretString"])
    assert payload["business_context"] == _ENABLED_STATE["business_context"]
    assert payload["system_prompt"] == "You are the ERP agent."
    assert payload["tables_desc"] == "orders(id: int, qty: int)"
    assert payload["password"] == "pw-SECRET-XYZ"  # conn params still merged
    # Secret path taken ⇒ env carries the ARN, never the context text.
    assert len(control.update_calls) == 1
    env = control.update_calls[0]["environmentVariables"]
    assert "business_context" not in json.dumps(env)


def test_start_cloud_payload_excludes_disabled_context_and_stale_key():
    """Disabled context ⇒ key absent — even when the CALLER passed a stale
    business_context in connection_config (graph is the source of truth)."""
    secrets = _FakeSecrets(current=None, exists=False)
    control = _FakeControl(runtime_found=True, runtime_name="adt_erp")
    with _patch_graph_state(_DISABLED_STATE):
        _run_start_cloud(secrets, control,
                         connection_config={"host": "db.example.internal",
                                            "business_context": "stale copy"})
    payload = json.loads(secrets.create_calls[0]["SecretString"])
    assert "business_context" not in payload, payload
    assert payload["system_prompt"] == "You are the ERP agent."


def test_start_cloud_graph_failure_is_best_effort():
    """A graph read failure must NOT fail the provision. With no existing
    secret to carry forward from (never provisioned), the key is simply
    absent."""
    def _boom(system_id):
        raise RuntimeError("neptune down")
    secrets = _FakeSecrets(current=None, exists=False)
    control = _FakeControl(runtime_found=True, runtime_name="adt_erp")
    with patch("agents.business_context.read_business_context", _boom):
        _run_start_cloud(secrets, control)
    payload = json.loads(secrets.create_calls[0]["SecretString"])
    assert "business_context" not in payload, payload
    assert payload["system_prompt"] == "You are the ERP agent."


def test_start_cloud_unreadable_graph_carries_forward_delivered_context():
    """MED-B pin for the provision path: absent-because-UNREADABLE must never
    drop delivered context — the existing secret's value is carried forward
    into the fresh payload."""
    secrets = _FakeSecrets(
        current={**_SIBLINGS, "business_context": "delivered steward text"},
        exists=False)
    control = _FakeControl(runtime_found=True, runtime_name="adt_erp")
    with patch("agents.business_context.read_business_context",
               lambda system_id: None):
        _run_start_cloud(secrets, control)
    payload = json.loads(secrets.create_calls[0]["SecretString"])
    assert payload["business_context"] == "delivered steward text", payload
    assert payload["system_prompt"] == "You are the ERP agent."


# --------------------------------------------------------------------------- #
# _start_local wrapper — LOCAL-mode delivery (the context channel is not
# write-only): graph truth rides into the agent's configuration at agent start
# --------------------------------------------------------------------------- #

def _run_start_local(connection_config=None):
    """Run _start_local with the server and port stubbed; return the
    configuration the agent's server would have been built with."""
    mgr = _mgr(mode="local")
    captured = {}

    def fake_server(system_id, host, port, build, stop_wait):
        captured["config"] = build.keywords
        return MagicMock()  # started fine

    with patch("agents.agent_manager.LocalAgentServer", fake_server), \
            patch.object(AgentManager, "_allocate_port", lambda self: 9109):
        mgr._start_local(
            system_id="erp", system_name="ERP", schema="public",
            system_prompt="You are the ERP agent.",
            tables_desc="orders(id: int, qty: int)",
            conn_params={"host": "db.local", "port": 5432},
            connection_type="PostgreSQL",
            connection_config=connection_config,
        )
    return captured["config"]


def test_start_local_config_includes_enabled_context():
    """THE local-delivery pin: saved+enabled context reaches the
    local agent via connection_config: the channel is no longer
    write-only in local mode."""
    with _patch_graph_state(_ENABLED_STATE):
        config = _run_start_local(connection_config={"host": "db.local"})
    assert config["connection_config"]["business_context"] == \
        _ENABLED_STATE["business_context"]
    assert config["connection_config"]["host"] == "db.local"
    assert config["system_prompt"] == "You are the ERP agent."


def test_start_local_config_excludes_disabled_context_and_stale_key():
    """Disabled ⇒ key absent, and a stale caller-supplied copy is dropped
    (graph is the source of truth — parity with the cloud wrapper)."""
    with _patch_graph_state(_DISABLED_STATE):
        config = _run_start_local(
            connection_config={"host": "db.local",
                               "business_context": "stale copy"})
    assert "business_context" not in config["connection_config"]
    assert config["connection_config"]["host"] == "db.local"


def test_start_local_unreadable_graph_omits_key_and_still_starts():
    """An unreadable graph must NOT fail the local start; with no delivered
    secret copy to carry forward (local has no secret), the key is simply
    absent — the next start re-reads graph truth."""
    with patch("agents.business_context.read_business_context",
               lambda system_id: None):
        config = _run_start_local(connection_config={"host": "db.local"})
    assert "business_context" not in config["connection_config"]
    assert config["system_id"] == "erp"


# --------------------------------------------------------------------------- #
# resync_cloud_connection — MED-C: the whole-value rewrite must not drop
# delivered business context (converges on _apply_business_context)
# --------------------------------------------------------------------------- #

def _run_resync(secrets, control, connection_config=None):
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)):
        return _mgr().resync_cloud_connection(
            system_id="erp", connection_type="MCP Server",
            connection_config=connection_config or {"url": "https://new/mcp"})


def test_resync_reincludes_enabled_business_context():
    """MED-C pin: after a reconnect, the rewritten secret carries the graph's
    enabled business context — the whole-value overwrite can no longer drop
    steward content."""
    secrets = _FakeSecrets(current=None, exists=False)
    control = _FakeControl(runtime_found=True, runtime_name="adt_erp")
    with _patch_graph_state(_ENABLED_STATE):
        ok = _run_resync(secrets, control)
    assert ok is True
    payload = json.loads(secrets.create_calls[0]["SecretString"])
    assert payload["business_context"] == _ENABLED_STATE["business_context"]
    assert payload["url"] == "https://new/mcp"


def test_resync_carries_forward_context_when_graph_unreadable():
    """MED-C + MED-B: an unreadable graph during resync carries the existing
    secret's delivered value forward — never a silent drop."""
    secrets = _FakeSecrets(
        current={**_SIBLINGS, "business_context": "delivered steward text"},
        exists=True)
    control = _FakeControl(runtime_found=True, runtime_name="adt_erp")
    with patch("agents.business_context.read_business_context",
               lambda system_id: None):
        ok = _run_resync(secrets, control)
    assert ok is True
    payload = json.loads(secrets.put_calls[0]["SecretString"])
    assert payload["business_context"] == "delivered steward text", payload
    assert payload["url"] == "https://new/mcp"


def test_resync_omits_context_when_readable_and_unset():
    """A READABLE genuinely-unset state omits the key — even a stale
    caller-supplied copy is dropped (graph is the source of truth)."""
    secrets = _FakeSecrets(current=None, exists=False)
    control = _FakeControl(runtime_found=True, runtime_name="adt_erp")
    with _patch_graph_state(_UNSET_STATE):
        ok = _run_resync(secrets, control,
                         connection_config={"url": "https://new/mcp",
                                            "business_context": "stale copy"})
    assert ok is True
    payload = json.loads(secrets.create_calls[0]["SecretString"])
    assert "business_context" not in payload, payload
    assert payload["url"] == "https://new/mcp"


# --------------------------------------------------------------------------- #
# runtime_app — the secret-carried key rides through to create_system_agent
# --------------------------------------------------------------------------- #

def test_load_agent_config_keeps_business_context_for_agent(monkeypatch):
    """Delivery pin (adapted from the plan's pop-in-runtime_app wording): T4
    landed the concept_mappings-style carry, so `_extract_prompt_and_conn_params`
    must POP system_prompt/tables_desc but PRESERVE business_context in
    connection_config — `create_system_agent` pops it before tool building
    (pinned by test_system_agent_business_context.py). Popping it here would
    sever delivery."""
    from agents.runtime_app import _extract_prompt_and_conn_params

    monkeypatch.delenv("SYSTEM_PROMPT", raising=False)
    monkeypatch.delenv("TABLES_DESC", raising=False)
    cfg = {"workgroup": "primary", "database": "erp",
           "system_prompt": "You are the ERP agent.",
           "tables_desc": "orders(id: int)",
           "concept_mappings": [{"concept": "work_order"}],
           "business_context": "Plant 2 runs three shifts."}
    system_prompt, tables_desc, conn_params, out = \
        _extract_prompt_and_conn_params(cfg, "Athena")

    assert system_prompt == "You are the ERP agent."
    assert tables_desc == "orders(id: int)"
    assert conn_params is None
    assert "system_prompt" not in out and "tables_desc" not in out
    # The carry: business_context (and concept_mappings) stay for create_system_agent.
    assert out["business_context"] == "Plant 2 runs three shifts."
    assert out["concept_mappings"] == [{"concept": "work_order"}]


_TESTS = [
    test_refresh_merges_and_preserves_sibling_secret_keys,
    test_refresh_removes_key_when_disabled_or_empty,
    test_refresh_returns_failed_on_update_runtime_error,
    test_refresh_skipped_in_local_mode,
    test_refresh_skipped_when_secret_missing,
    test_refresh_skipped_when_no_runtime,
    test_refresh_failed_on_graph_read_error,
    test_refresh_failed_when_graph_unreadable_zero_secret_writes,
    test_refresh_genuinely_unset_removal_still_works,
    test_refresh_never_logs_secret_or_context_values,
    test_merged_payload_rejects_non_object_secret,
    test_business_context_secret_value_include_rule,
    test_start_cloud_payload_includes_enabled_context,
    test_start_cloud_payload_excludes_disabled_context_and_stale_key,
    test_start_cloud_graph_failure_is_best_effort,
    test_start_cloud_unreadable_graph_carries_forward_delivered_context,
    test_resync_reincludes_enabled_business_context,
    test_resync_carries_forward_context_when_graph_unreadable,
    test_resync_omits_context_when_readable_and_unset,
]


class TestAgentModelLiveRefresh:
    """An agent-model change used to require a full re-onboard because
    SYSTEM_AGENT_MODEL is baked into the runtime env at provision. The runtime
    env bump (same seam as the business-context refresh) replaces that."""

    def test_refresh_agent_model_overrides_only_that_env_key(self, monkeypatch):
        from agents.agent_manager import AgentManager
        captured = {}

        def fake_bump(self, system_id, region, env_overrides=None):
            captured["system_id"] = system_id
            captured["env_overrides"] = env_overrides
            return "ok"

        monkeypatch.setattr(AgentManager, "_bump_runtime_version", fake_bump)
        mgr = AgentManager.__new__(AgentManager)
        mgr.mode = "cloud"
        assert mgr.refresh_agent_model("erp", "openai.gpt-5.6-luna") == "ok"
        assert captured["system_id"] == "erp"
        assert captured["env_overrides"] == {"SYSTEM_AGENT_MODEL": "openai.gpt-5.6-luna"}

    def test_local_mode_is_skipped_not_failed(self):
        from agents.agent_manager import AgentManager
        mgr = AgentManager.__new__(AgentManager)
        mgr.mode = "local"
        assert mgr.refresh_agent_model("erp", "openai.gpt-5.6-luna") == "skipped"

    def test_bump_preserves_existing_env_and_merges_overrides(self):
        """The override must not clobber sibling env (SYSTEM_ID, GRAPH_IDENTIFIER…)."""
        existing = {"SYSTEM_ID": "erp", "SYSTEM_AGENT_MODEL": "old-model",
                    "GRAPH_IDENTIFIER": "g-1"}
        merged = {**existing, **{"SYSTEM_AGENT_MODEL": "openai.gpt-5.6-luna"}}
        assert merged["SYSTEM_ID"] == "erp" and merged["GRAPH_IDENTIFIER"] == "g-1"
        assert merged["SYSTEM_AGENT_MODEL"] == "openai.gpt-5.6-luna"

    def test_bump_reports_unchanged_when_model_already_set(self, monkeypatch):
        """Applying the model a system already runs must not spend a version
        bump — that is what lets the caller always apply and self-correct the
        SSM-vs-runtime drift that made a switch a silent no-op."""
        from agents import agent_manager as am

        class FakeControl:
            def get_agent_runtime(self, agentRuntimeId=None):
                return {"environmentVariables": {"SYSTEM_AGENT_MODEL": "openai.gpt-5.6-luna"},
                        "networkConfiguration": {"networkMode": "PUBLIC"}}
            def update_agent_runtime(self, **kw):
                raise AssertionError("must not update when already correct")

        monkeypatch.setattr(am.boto3, "client", lambda *a, **k: FakeControl())
        monkeypatch.setattr(am, "list_all_agent_runtimes",
                            lambda c: [{"agentRuntimeName": "adt_erp", "agentRuntimeId": "r1"}])
        mgr = am.AgentManager.__new__(am.AgentManager)
        mgr.mode = "cloud"
        monkeypatch.setattr(am.AgentManager, "_runtime_name", lambda self, sid: "adt_erp")
        assert mgr._bump_runtime_version(
            "erp", "us-east-1",
            env_overrides={"SYSTEM_AGENT_MODEL": "openai.gpt-5.6-luna"}) == "unchanged"
