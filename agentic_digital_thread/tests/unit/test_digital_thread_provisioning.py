# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for digital-thread provisioning in agent_manager (Wave 1 T7).

The full _start_cloud path makes live AgentCore/Secrets/Cognito calls and is exercised
against a deployed env; here we test the graph-connection LOGIC in isolation:
  - `_runtime_connection_env` routes KnowledgeGraph systems to GRAPH_IDENTIFIER (the
    Neptune connection is via the runtime IAM role, so no DB secret/inline-config)
    while other systems keep their existing env;
  - `ensure_digital_thread_target` is cloud-only and delegates to the shared
    SYSTEM_ID-driven provisioning path with the reserved id + KnowledgeGraph type.
"""

from unittest.mock import Mock

from agents.agent_manager import (
    AgentManager,
    DIGITAL_THREAD_SYSTEM_ID,
    KNOWLEDGE_GRAPH_CONNECTION_TYPE,
)


def test_reserved_constants_are_wired_through():
    assert DIGITAL_THREAD_SYSTEM_ID == "digital-thread"
    assert KNOWLEDGE_GRAPH_CONNECTION_TYPE == "KnowledgeGraph"


def test_connection_env_knowledge_graph_passes_graph_identifier(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-abc123")
    env = AgentManager._runtime_connection_env(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        KNOWLEDGE_GRAPH_CONNECTION_TYPE, connection_secret_arn="", effective_config={}
    )
    # KnowledgeGraph: GRAPH_IDENTIFIER only (Neptune via IAM role), never a DB secret/inline config.
    assert env == {"GRAPH_IDENTIFIER": "g-abc123"}
    assert "CONNECTION_SECRET_ARN" not in env and "CONNECTION_CONFIG" not in env


def test_connection_env_knowledge_graph_empty_when_graph_id_unset(monkeypatch):
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)
    env = AgentManager._runtime_connection_env(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        KNOWLEDGE_GRAPH_CONNECTION_TYPE, connection_secret_arn="", effective_config={}
    )
    assert env == {}


def test_connection_env_prefers_secret_arn_for_db_systems():
    env = AgentManager._runtime_connection_env(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        "PostgreSQL", connection_secret_arn="arn:aws:secretsmanager:...:agent-config/erp",
        effective_config={"host": "db", "password": "p"},
    )
    assert env == {"CONNECTION_SECRET_ARN": "arn:aws:secretsmanager:...:agent-config/erp"}


def test_connection_env_falls_back_to_inline_config_when_no_secret():
    env = AgentManager._runtime_connection_env(  # nosec B106 - offline test fixture/placeholder credential, not a live secret
        "PostgreSQL", connection_secret_arn="", effective_config={"host": "db"},
    )
    assert "CONNECTION_CONFIG" in env
    assert "host" in env["CONNECTION_CONFIG"]


def test_ensure_digital_thread_target_is_noop_in_local_mode():
    mgr = AgentManager(mode="local")
    mgr.start_agent = Mock()  # must NOT be called
    assert mgr.ensure_digital_thread_target() == ""
    mgr.start_agent.assert_not_called()


def test_ensure_digital_thread_target_delegates_to_shared_path_in_cloud_mode():
    mgr = AgentManager(mode="cloud", gateway_id="gw", gateway_url="https://gw/mcp")
    mgr.start_agent = Mock(return_value="https://gw/mcp")
    url = mgr.ensure_digital_thread_target()
    assert url == "https://gw/mcp"
    kwargs = mgr.start_agent.call_args.kwargs
    assert kwargs["system_id"] == DIGITAL_THREAD_SYSTEM_ID            # reserved id → target name + tool prefix
    assert kwargs["connection_type"] == KNOWLEDGE_GRAPH_CONNECTION_TYPE  # graph-connection branch
