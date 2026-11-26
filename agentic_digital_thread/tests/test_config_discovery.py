# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for AgentCore Memory id discovery (config.py, ADR-0006).

The cross-stack MEMORY_ID env injection has been removed, so discovery-by-name
is now the PRIMARY id source in BOTH local and cloud mode:

  * MEMORY_ID set      -> used verbatim, discovery NOT called (explicit override).
  * MEMORY_ID unset    -> discovery runs in BOTH local AND cloud mode.

`_discover_memory_id` matches ONLY by the exact stack name prefix
`adt_memory` (never a substring like `memory`, never the
unrelated leftover prototype memories) and FAILS LOUD on ambiguity: >1 ACTIVE
match logs an ERROR and returns None rather than binding the wrong memory.

No real AWS: the bedrock-agentcore-control client is faked via boto3.client.

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/test_config_discovery.py
  * standalone:  python agentic_digital_thread/tests/test_config_discovery.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

# Make `agents...` importable (agentic_digital_thread/ is one level up from tests/).
_ADT_ROOT = Path(__file__).resolve().parents[1]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents import config as cfg  # noqa: E402

# Env vars this suite manipulates; cleared per test for isolation.
_MEM_ENV_KEYS = ("MEMORY_ID", "AGENT_MODE", "DEV_MODE", "AGENTCORE_RUNTIME_ARN",
                 "AGENTCORE_GATEWAY_URL", "AGENTCORE_GATEWAY_ID", "AWS_REGION")


class _FakeControl:
    """Fake bedrock-agentcore-control client. Records whether list_memories ran."""

    def __init__(self, memories):
        self._memories = memories
        self.list_memories_called = False

    def list_memories(self, **kwargs):
        self.list_memories_called = True
        return {"memories": self._memories}

    def list_agent_runtimes(self):
        return {"agentRuntimes": []}


def _clean_env():
    """Return an os.environ override dict that unsets all memory-relevant keys."""
    return {k: "" for k in _MEM_ENV_KEYS}


def _patch_boto(control):
    def fake_client(service_name, **kwargs):
        if service_name == "bedrock-agentcore-control":
            return control
        raise RuntimeError(f"unexpected boto3 client in test: {service_name}")
    return patch("boto3.client", fake_client)


# ---------------------------------------------------------------------------
# (a) MEMORY_ID set -> used verbatim, discovery NOT called (both modes)
# ---------------------------------------------------------------------------
def test_memory_id_env_wins_no_discovery_local():
    control = _FakeControl([{"id": "adt_memory-LIVE", "status": "ACTIVE"}])
    env = _clean_env()
    env.update({"MEMORY_ID": "mem-from-env", "AGENT_MODE": "local",
                "AGENTCORE_RUNTIME_ARN": "arn:dummy"})
    with patch.dict("os.environ", env, clear=False), _patch_boto(control):
        conf = cfg.AppConfig.from_env()

    assert conf.memory_id == "mem-from-env"
    assert conf.has_memory is True
    assert control.list_memories_called is False   # env short-circuits discovery


def test_memory_id_env_wins_no_discovery_cloud():
    control = _FakeControl([{"id": "adt_memory-LIVE", "status": "ACTIVE"}])
    env = _clean_env()
    # No AGENT_MODE/DEV_MODE -> cloud mode. Env still wins, no discovery call.
    env.update({"MEMORY_ID": "mem-from-env", "AGENTCORE_RUNTIME_ARN": "arn:dummy"})
    with patch.dict("os.environ", env, clear=False), _patch_boto(control):
        conf = cfg.AppConfig.from_env()

    assert conf.memory_id == "mem-from-env"
    assert conf.has_memory is True
    assert control.list_memories_called is False


# ---------------------------------------------------------------------------
# (b) MEMORY_ID unset -> discovery runs in BOTH local AND cloud mode
# ---------------------------------------------------------------------------
def test_unset_local_runs_discovery_and_uses_value():
    control = _FakeControl([
        {"id": "adt_memory-ABCD", "status": "ACTIVE"},
    ])
    env = _clean_env()
    env.update({"AGENT_MODE": "local", "AGENTCORE_RUNTIME_ARN": "arn:dummy"})
    with patch.dict("os.environ", env, clear=False), _patch_boto(control):
        conf = cfg.AppConfig.from_env()

    assert control.list_memories_called is True
    assert conf.memory_id == "adt_memory-ABCD"
    assert conf.has_memory is True


def test_unset_cloud_runs_discovery_and_uses_value():
    # Cloud mode (no AGENT_MODE/DEV_MODE) now ALSO discovers, since the
    # cross-stack MEMORY_ID env injection has been removed.
    control = _FakeControl([
        {"id": "adt_memory-CLOUD", "status": "ACTIVE"},
    ])
    env = _clean_env()
    env.update({"AGENTCORE_RUNTIME_ARN": "arn:dummy"})
    with patch.dict("os.environ", env, clear=False), _patch_boto(control):
        conf = cfg.AppConfig.from_env()

    assert control.list_memories_called is True
    assert conf.memory_id == "adt_memory-CLOUD"
    assert conf.has_memory is True


# ---------------------------------------------------------------------------
# (c) exactly-1 ACTIVE prefix match -> returned
# ---------------------------------------------------------------------------
def test_discovery_matches_only_exact_prefix():
    memories = [
        {"id": "mfg_thread_memory-0001", "status": "ACTIVE"},
        {"id": "factoryfloor-0002", "status": "ACTIVE"},
        {"id": "customersupport-0003", "status": "ACTIVE"},
        {"id": "adt_memory-WINNER", "status": "ACTIVE"},
    ]
    control = _FakeControl(memories)
    with _patch_boto(control):
        result = cfg._discover_memory_id("us-east-1")

    assert result == "adt_memory-WINNER"


def test_discovery_matches_on_name_field_too():
    # Some APIs surface the friendly name in `name` while `id` is opaque.
    memories = [
        {"id": "abc123", "name": "adt_memory", "status": "ACTIVE"},
        {"id": "xyz789", "name": "mfg_thread_memory", "status": "ACTIVE"},
    ]
    control = _FakeControl(memories)
    with _patch_boto(control):
        result = cfg._discover_memory_id("us-east-1")

    assert result == "abc123"


def test_discovery_ignores_non_active_match():
    # A single match that is not ACTIVE is not a usable binding -> None.
    memories = [
        {"id": "adt_memory-CREATING", "status": "CREATING"},
    ]
    control = _FakeControl(memories)
    with _patch_boto(control):
        result = cfg._discover_memory_id("us-east-1")

    assert result is None


# ---------------------------------------------------------------------------
# (d) the 5 leftover prototype names are rejected -> None
# ---------------------------------------------------------------------------
def test_discovery_returns_none_when_only_leftovers():
    memories = [
        {"id": "mfg_thread_memory-0001", "status": "ACTIVE"},
        {"id": "mfg_thread_discovery_memory-0009", "status": "ACTIVE"},
        {"id": "factoryfloor-0002", "status": "ACTIVE"},
        {"id": "customersupport-0003", "status": "ACTIVE"},
        {"id": "testharness-0004", "status": "ACTIVE"},
    ]
    control = _FakeControl(memories)
    with _patch_boto(control):
        result = cfg._discover_memory_id("us-east-1")

    assert result is None


# ---------------------------------------------------------------------------
# (e) >1 ACTIVE match -> returns None AND logs ERROR (fail loud, refuse to guess)
# ---------------------------------------------------------------------------
def test_discovery_ambiguous_returns_none_and_logs_error(caplog):
    memories = [
        {"id": "adt_memory-AAAA", "status": "ACTIVE"},
        {"id": "adt_memory-BBBB", "status": "ACTIVE"},
    ]
    control = _FakeControl(memories)
    with _patch_boto(control), caplog.at_level(logging.ERROR, logger=cfg.logger.name):
        result = cfg._discover_memory_id("us-east-1")

    assert result is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log on ambiguous discovery"
    msg = errors[0].getMessage()
    assert "AMBIGUOUS" in msg
    # The actionable message must list BOTH matching ids.
    assert "adt_memory-AAAA" in msg
    assert "adt_memory-BBBB" in msg


# ---------------------------------------------------------------------------
# (f) 0 matches -> None (quiet; no ERROR)
# ---------------------------------------------------------------------------
def test_discovery_returns_none_when_empty(caplog):
    control = _FakeControl([])
    with _patch_boto(control), caplog.at_level(logging.ERROR, logger=cfg.logger.name):
        result = cfg._discover_memory_id("us-east-1")

    assert result is None
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


# ---------------------------------------------------------------------------
# API error -> None AND logs ERROR with exception info (de-silenced)
# ---------------------------------------------------------------------------
def test_discovery_api_error_returns_none_and_logs_error(caplog):
    class _BoomControl:
        def list_memories(self, **kwargs):
            raise RuntimeError("boom: list_memories blew up")

    with _patch_boto(_BoomControl()), caplog.at_level(logging.ERROR, logger=cfg.logger.name):
        result = cfg._discover_memory_id("us-east-1")

    assert result is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log on API failure"
    assert errors[0].exc_info is not None, "API-error log must include exc_info"


if __name__ == "__main__":
    # Lightweight standalone runner (no caplog): exercise the no-arg tests.
    import inspect

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                print(f"SKIP {name} (needs pytest fixture)")
                continue
            fn()
            print(f"PASS {name}")
    print("RESULT: PASS — discovery-primary gating + exact-prefix + fail-loud ambiguity"
          if failures == 0 else "RESULT: FAIL")


# ── Regression: pagination must not spin forever on a non-string nextToken ────
class _NonStringTokenControl:
    """list_memories always returns a TRUTHY, NON-STRING nextToken — the
    pathological shape a stubbed/mocked boto3 client produces (a MagicMock).
    With the pre-fix ``if not next_token: break`` this looped forever and grew
    memory into the GBs; the type-guarded break must terminate after one page."""

    def __init__(self):
        self.calls = 0

    def list_memories(self, **kwargs):
        self.calls += 1
        if self.calls > 1000:  # test safety net — should never be hit post-fix
            raise AssertionError("pagination did not terminate (infinite loop)")
        return {"memories": [], "nextToken": object()}  # truthy, not a str

    def list_agent_runtimes(self):
        return {"agentRuntimes": []}


def test_discover_memory_id_terminates_on_nonstring_next_token():
    """A truthy non-string nextToken must terminate pagination (return None), not
    spin forever. Regression for the ~9GB memory-leak / hang."""
    fake = _NonStringTokenControl()
    with patch("boto3.client", return_value=fake):
        result = cfg._discover_memory_id("us-east-1")
    assert result is None
    assert fake.calls == 1, f"expected 1 page then break, got {fake.calls}"
