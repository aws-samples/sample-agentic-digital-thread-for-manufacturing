# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tier-3 query_ref redemption route (provenance plan, Task 3.4 — D5 pins).

Pins the frozen D5 redemption contract on
``GET /api/admin/agents/traces/queries/{query_ref}?session_id=``:

- admin-gated FIRST (403 before any validation or store read);
- structured 400 on regex-invalid ``query_ref``/``session_id`` — the store
  is never touched, and the detail never echoes the invalid value;
- structured 404 ``{"detail": "Unknown or expired query_ref"}`` on a store
  miss — never a payload echo, never fabricated text;
- 200 returns the EXACT D5 body persisted by ``provenance_store`` (Task 3.1);
- NOT gated by ``AGENTS_OBSERVABILITY_DRILLDOWN`` — the flag gates the
  CloudWatch spans reader (a different data source), not this store read;
- logging is digit-masked: no query text, no raw identifiers.

Fully offline: the store runs on its in-memory fail-soft branch; no AWS,
no network (socket-blocked lane enforces zero leakage).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agents.agents_trace_routes as atr
import agents.provenance_store as provenance_store
from agents.job_store import JobStore
from agents.provenance import mint_query_ref

_SID = "sess-abc.123"

_D5_KEYS = {"query_ref", "session_id", "system_id", "tool_name",
            "executed_query", "kg_artifacts", "captured_at"}

_QUERY_TEXT = "SELECT part_no FROM orders WHERE qty > 5"


def _admin_request(is_admin: bool = True):
    return SimpleNamespace(state=SimpleNamespace(is_admin=is_admin))


def _ref(tool_id: str = "toolu_001") -> str:
    return mint_query_ref(_SID, tool_id)


def _seed(session_id: str = _SID, tool_id: str = "toolu_001") -> str:
    ref = mint_query_ref(session_id, tool_id)
    stored = provenance_store.put_turn_queries(session_id, {ref: {
        "system_id": "erp",
        "tool_name": "erp___query_erp",
        "query_text": _QUERY_TEXT,
        "kg_artifact_ids": ["subj-0a1b", "vm-learned-0c2d"],
        "captured_at": "2026-08-03T19:00:00Z",
    }})
    assert stored == 1
    return ref


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch):
    """Fresh in-memory JobStore per test (the offline fail-soft branch) and
    the observability drill-down flag UNSET — proving the route serves
    regardless of that gate."""
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=None))
    monkeypatch.delenv("AGENTS_OBSERVABILITY_DRILLDOWN", raising=False)


# --------------------------------------------------------------------------- #
# Auth posture
# --------------------------------------------------------------------------- #

def test_non_admin_forbidden_before_anything_else(monkeypatch):
    calls = []
    monkeypatch.setattr(provenance_store, "get_query",
                        lambda *a: calls.append(a))
    with pytest.raises(HTTPException) as exc:
        # Even an INVALID ref must 403 first — admin gate precedes validation.
        atr.get_provenance_query("not-a-ref", _admin_request(is_admin=False),
                                 session_id=_SID)
    assert exc.value.status_code == 403
    assert calls == []


# --------------------------------------------------------------------------- #
# Structured 400 — regex-invalid ids, store never touched, no value echo
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_ref", [
    "q-XYZXYZXYZXYZ",        # non-hex
    "q-" + "a" * 11,         # too short
    "q-" + "a" * 13,         # too long
    "Q-3f9c2b81ab04",        # wrong case prefix
    "3f9c2b81ab04",          # missing prefix
    "q-3f9c2b81ab04'--",     # injection shape
    "",
])
def test_invalid_query_ref_is_structured_400(monkeypatch, bad_ref):
    calls = []
    monkeypatch.setattr(provenance_store, "get_query",
                        lambda *a: calls.append(a))
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_query(bad_ref, _admin_request(), session_id=_SID)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid query_ref"
    if bad_ref:                                  # never an echo of the value
        assert bad_ref not in str(exc.value.detail)
    assert calls == []                           # store never touched


@pytest.mark.parametrize("bad_sid", ['s"1', "a b", "x|y", "s\\1", "", "🙂",
                                     "s" * 257])
def test_invalid_session_id_is_structured_400(monkeypatch, bad_sid):
    calls = []
    monkeypatch.setattr(provenance_store, "get_query",
                        lambda *a: calls.append(a))
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_query(_ref(), _admin_request(), session_id=bad_sid)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid session id"
    assert calls == []


def test_session_id_defaults_to_missing_and_400s():
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_query(_ref(), _admin_request())
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------- #
# Structured 404 — unknown/expired, fixed detail, no ref echo
# --------------------------------------------------------------------------- #

def test_unknown_ref_is_structured_404():
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_query(_ref("never-stored"), _admin_request(),
                                 session_id=_SID)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Unknown or expired query_ref"


def test_wrong_session_for_stored_ref_is_404():
    """Session isolation surfaces as the SAME 404 — no oracle for whether a
    ref exists under another session."""
    ref = _seed()
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_query(ref, _admin_request(),
                                 session_id="other-session")
    assert exc.value.status_code == 404
    assert exc.value.detail == "Unknown or expired query_ref"
    assert ref not in str(exc.value.detail)


# --------------------------------------------------------------------------- #
# 200 — exact D5 body from the real store
# --------------------------------------------------------------------------- #

def test_hit_returns_exact_d5_shape():
    ref = _seed()
    out = atr.get_provenance_query(ref, _admin_request(), session_id=_SID)
    assert out == {
        "query_ref": ref,
        "session_id": _SID,
        "system_id": "erp",
        "tool_name": "erp___query_erp",
        "executed_query": _QUERY_TEXT,
        "kg_artifacts": ["subj-0a1b", "vm-learned-0c2d"],
        "captured_at": "2026-08-03T19:00:00Z",
    }
    assert set(out) == _D5_KEYS


def test_over_bound_entry_served_honestly():
    """An over-cap write (Task 3.1) redeems with the honest empty query +
    note — never fabricated or truncated text."""
    ref = _ref()
    big = "SELECT 1 -- " + "x" * provenance_store.MAX_QUERY_TEXT_BYTES
    provenance_store.put_turn_queries(_SID, {ref: {"query_text": big}})
    out = atr.get_provenance_query(ref, _admin_request(), session_id=_SID)
    assert out["executed_query"] == ""
    assert out["note"] == provenance_store.OVER_BOUND_NOTE


# --------------------------------------------------------------------------- #
# NOT gated by the observability drill-down flag
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", [None, "false", "true"])
def test_route_serves_regardless_of_drilldown_flag(monkeypatch, flag):
    """The flag gates the CloudWatch spans READER, not this store read —
    the route must behave identically in every flag state (and never
    return the reader's ``{"enabled": false}`` shape)."""
    if flag is None:
        monkeypatch.delenv("AGENTS_OBSERVABILITY_DRILLDOWN", raising=False)
    else:
        monkeypatch.setenv("AGENTS_OBSERVABILITY_DRILLDOWN", flag)
    monkeypatch.setattr(atr, "_boto_session",
                        lambda: pytest.fail("boto3 touched by queries route"))
    ref = _seed()
    out = atr.get_provenance_query(ref, _admin_request(), session_id=_SID)
    assert "enabled" not in out
    assert out["executed_query"] == _QUERY_TEXT
    with pytest.raises(HTTPException) as exc:
        atr.get_provenance_query(_ref("missing"), _admin_request(),
                                 session_id=_SID)
    assert exc.value.status_code == 404


def test_route_registered_on_admin_trace_prefix():
    paths = [r.path for r in atr.router.routes]
    assert "/api/admin/agents/traces/queries/{query_ref}" in paths


# --------------------------------------------------------------------------- #
# Digit-masked logging — no query text, no raw identifiers
# --------------------------------------------------------------------------- #

def test_logs_carry_no_query_text_or_raw_ids(caplog):
    ref = _seed()
    with caplog.at_level(logging.DEBUG):
        atr.get_provenance_query(ref, _admin_request(), session_id=_SID)
        with pytest.raises(HTTPException):
            atr.get_provenance_query(_ref("missing"), _admin_request(),
                                     session_id=_SID)
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.name.startswith("agents.agents_trace_routes"))
    assert "SELECT" not in text
    assert "part_no" not in text
    assert _QUERY_TEXT not in text
    assert ref not in text          # only sha256[:12] digests, never the ref
    assert _SID not in text


def test_hit_returns_additive_query_shape_observability_fields(caplog):
    ref = _ref("toolu_shape")
    fingerprint = "c" * 64
    provenance_store.put_turn_queries(_SID, {ref: {
        "system_id": "erp",
        "tool_name": "erp___query_erp",
        "query_text": _QUERY_TEXT,
        "kg_artifact_ids": [],
        "captured_at": "2026-08-03T19:00:00Z",
        "query_fingerprint": fingerprint,
        "query_shape_version": "v1",
    }})
    with caplog.at_level(logging.INFO):
        out = atr.get_provenance_query(ref, _admin_request(), session_id=_SID)
    assert out["query_fingerprint"] == fingerprint
    assert out["query_shape_version"] == "v1"
    # Additive only: every frozen D5 field remains present and unchanged.
    assert _D5_KEYS < set(out)
    assert out["executed_query"] == _QUERY_TEXT
    route_logs = "\n".join(
        record.getMessage() for record in caplog.records
        if record.name == "agents.agents_trace_routes"
    )
    assert fingerprint in route_logs
    assert "version=v1" in route_logs
    assert f"query_len={len(_QUERY_TEXT)}" in route_logs
    assert _QUERY_TEXT not in route_logs
    assert "part_no" not in route_logs
