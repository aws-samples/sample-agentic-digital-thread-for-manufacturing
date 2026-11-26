# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for ``DELETE /api/memory/record`` (memory-suite stage).

No AWS, no network. Two layers, mirroring ``tests/test_memory_client.py``:

  * ``MemoryClient.delete_record`` with the bedrock-agentcore client stubbed —
    namespace scoping is the security contract: a record whose namespaces are
    NOT all inside the requesting actor's ``/facts|/summaries|/preferences/
    {user}/`` prefixes raises ``MemoryRecordNotFoundError`` and the data-plane
    ``delete_memory_record`` is NEVER called.
  * The route coroutine driven directly (``asyncio.run``) with
    ``memory_routes._client`` monkeypatched — status mapping 401/404/503/200.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import patch

import pytest

import agents.memory_client as mc
import agents.memory_routes as routes
from agents.memory_client import (
    MemoryClient,
    MemoryRecordNotFoundError,
    MemoryUnavailableError,
)


def _fake_config(*, has_memory: bool = True, memory_id: str = "mem-123",
                 region: str = "us-east-1"):
    return types.SimpleNamespace(
        has_memory=has_memory, memory_id=memory_id, aws_region=region,
    )


class ResourceNotFoundException(Exception):
    """Stubbed boto3 not-found (matched by class name in _is_not_found)."""


class _FakeAgentCore:
    """Stubbed bedrock-agentcore data-plane client for the delete path."""

    def __init__(self, *, record=None, get_raises=None, delete_raises=None):
        self._record = record
        self._get_raises = get_raises
        self._delete_raises = delete_raises
        self.get_kwargs = None
        self.delete_kwargs = None
        self.delete_called = False

    def get_memory_record(self, **kwargs):
        self.get_kwargs = kwargs
        if self._get_raises:
            raise self._get_raises
        return {"memoryRecord": self._record} if self._record is not None else {}

    def delete_memory_record(self, **kwargs):
        self.delete_called = True
        self.delete_kwargs = kwargs
        if self._delete_raises:
            raise self._delete_raises
        return {"memoryRecordId": kwargs.get("memoryRecordId")}


def _make_client(fake, cfg=None):
    cfg = cfg or _fake_config()
    with patch.object(mc, "get_config", return_value=cfg):
        client = MemoryClient()
    client._client = lambda: fake
    return client, cfg


def _delete(client, cfg, record_id="rec-1", user_id="alice"):
    with patch.object(mc, "get_config", return_value=cfg):
        client.delete_record(record_id=record_id, user_id=user_id)


# ---------------------------------------------------------------------------
# MemoryClient.delete_record — namespace scoping (the security contract)
# ---------------------------------------------------------------------------

def test_delete_own_fact_record_succeeds():
    fake = _FakeAgentCore(record={"namespaces": ["/facts/alice/"]})
    client, cfg = _make_client(fake)
    _delete(client, cfg)
    assert fake.delete_called
    assert fake.delete_kwargs == {"memoryId": "mem-123", "memoryRecordId": "rec-1"}
    assert fake.get_kwargs == {"memoryId": "mem-123", "memoryRecordId": "rec-1"}


def test_delete_own_summary_record_succeeds():
    # summaries namespace carries the session segment — prefix match applies.
    fake = _FakeAgentCore(record={"namespaces": ["/summaries/alice/sess-9/"]})
    client, cfg = _make_client(fake)
    _delete(client, cfg)
    assert fake.delete_called


def test_delete_other_actors_record_is_not_found_and_never_deletes():
    fake = _FakeAgentCore(record={"namespaces": ["/facts/bob/"]})
    client, cfg = _make_client(fake)
    with pytest.raises(MemoryRecordNotFoundError):
        _delete(client, cfg, user_id="alice")
    assert not fake.delete_called  # scoping check happens BEFORE any delete


def test_prefix_collision_actor_id_is_rejected():
    # 'alice2' must NOT be deletable by 'alice' (trailing slash in the prefix).
    fake = _FakeAgentCore(record={"namespaces": ["/facts/alice2/"]})
    client, cfg = _make_client(fake)
    with pytest.raises(MemoryRecordNotFoundError):
        _delete(client, cfg, user_id="alice")
    assert not fake.delete_called


def test_record_without_namespaces_is_rejected():
    fake = _FakeAgentCore(record={"namespaces": []})
    client, cfg = _make_client(fake)
    with pytest.raises(MemoryRecordNotFoundError):
        _delete(client, cfg)
    assert not fake.delete_called


def test_mixed_namespaces_require_all_to_belong_to_user():
    fake = _FakeAgentCore(record={"namespaces": ["/facts/alice/", "/facts/bob/"]})
    client, cfg = _make_client(fake)
    with pytest.raises(MemoryRecordNotFoundError):
        _delete(client, cfg, user_id="alice")
    assert not fake.delete_called


def test_lookup_not_found_maps_to_record_not_found():
    fake = _FakeAgentCore(get_raises=ResourceNotFoundException("no such record"))
    client, cfg = _make_client(fake)
    with pytest.raises(MemoryRecordNotFoundError):
        _delete(client, cfg)
    assert not fake.delete_called


def test_lookup_api_error_maps_to_unavailable():
    fake = _FakeAgentCore(get_raises=RuntimeError("AccessDeniedException"))
    client, cfg = _make_client(fake)
    with pytest.raises(MemoryUnavailableError):
        _delete(client, cfg)
    assert not fake.delete_called


def test_delete_api_error_maps_to_unavailable():
    fake = _FakeAgentCore(
        record={"namespaces": ["/facts/alice/"]},
        delete_raises=RuntimeError("AccessDeniedException: missing DeleteMemoryRecord"),
    )
    client, cfg = _make_client(fake)
    with pytest.raises(MemoryUnavailableError):
        _delete(client, cfg)


def test_unconfigured_memory_raises_unavailable():
    fake = _FakeAgentCore(record={"namespaces": ["/facts/alice/"]})
    client, cfg = _make_client(fake, cfg=_fake_config(has_memory=False))
    with pytest.raises(MemoryUnavailableError):
        _delete(client, cfg)
    assert fake.get_kwargs is None  # never reached the API


# ---------------------------------------------------------------------------
# Route coroutine — status mapping (auth like the sibling memory routes)
# ---------------------------------------------------------------------------

def _request(user_id="alice"):
    user = types.SimpleNamespace(user_id=user_id) if user_id else None
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


class _StubClient:
    def __init__(self, exc=None):
        self._exc = exc
        self.calls = []

    def delete_record(self, *, record_id, user_id):
        self.calls.append((record_id, user_id))
        if self._exc:
            raise self._exc


def _run_route(monkeypatch, *, exc=None, user_id="alice", record_id="rec-1"):
    stub = _StubClient(exc=exc)
    monkeypatch.setattr(routes, "_client", stub)
    result = asyncio.run(routes.delete_record(_request(user_id), record_id=record_id))
    return result, stub


def test_route_requires_auth(monkeypatch):
    result, stub = _run_route(monkeypatch, user_id=None)
    assert result.status_code == 401
    assert stub.calls == []  # no client call without a user


def test_route_deletes_with_current_users_id(monkeypatch):
    result, stub = _run_route(monkeypatch)
    assert result == {"deleted": True, "id": "rec-1"}
    # The route passes ONLY the authenticated user's id — namespace scoping
    # inside delete_record is therefore always the caller's own.
    assert stub.calls == [("rec-1", "alice")]


def test_route_maps_not_found_to_404(monkeypatch):
    result, _ = _run_route(monkeypatch, exc=MemoryRecordNotFoundError("rec-1"))
    assert result.status_code == 404


def test_route_maps_unavailable_to_503(monkeypatch):
    result, _ = _run_route(monkeypatch, exc=MemoryUnavailableError("boom"))
    assert result.status_code == 503
