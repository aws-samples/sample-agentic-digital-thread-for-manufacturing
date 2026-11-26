# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Investigation-trace redemption route (Wave 6, Task B3 — W6-F5 pins).

Pins the D5-family sibling
``GET /api/admin/agents/traces/investigations/{query_ref}?session_id=``:

* admin-gated FIRST (403 before validation or store read);
* structured 400 on regex-invalid ids — never an echo, store untouched;
* structured 404 ``{"detail": "Unknown or expired investigation trace"}``;
* 200 is the stored W6-F2 entry, unreshaped (incl. truncated flags and
  ``steps_dropped``); an answer with no graph-known id comes back
  byte-identical. The one addition, ``final_answer_names`` for the card, is
  pinned in ``test_investigation_trace_card_names.py``;
* FAMILY-INDEPENDENCE PIN: a ``(sid, ref)`` that HAS a Wave-5
  provenance-query entry but no trace ⇒ THIS route 404s while
  ``/queries/{ref}`` still 200s;
* NOT gated by ``AGENTS_OBSERVABILITY_DRILLDOWN``;
* caplog: digests + counts only.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agents.agents_trace_routes as atr
import agents.investigation_trace as it
import agents.investigation_trace_store as its
import agents.provenance_store as provenance_store
from agents.job_store import JobStore
from agents.provenance import mint_query_ref

_SID = "sess-w6.route"
_QUESTION = "How many open orders reference part B-2847-X?"
_ANSWER = "Seven open orders reference that part."
_REASONING = "Checking the orders table first."
_SQL_INPUT = '{"sql":"SELECT part_no FROM orders"}'


def _admin_request(is_admin: bool = True):
    return SimpleNamespace(state=SimpleNamespace(is_admin=is_admin))


def _ref(tool_id: str = "toolu_w6") -> str:
    return mint_query_ref(_SID, tool_id)


def _entry(ref: str) -> dict:
    step = {"seq": 1, "reasoning": _REASONING, "tool_name": "execute_sql",
            "tool_input": _SQL_INPUT, "input_bytes": len(_SQL_INPUT),
            "tool_output": '[{"part_no":"B-2847-X"}]', "output_bytes": 24,
            "status": "success", "duration_ms": 42,
            "started_at": "2026-08-04T18:00:00Z"}
    return it.build_trace_entry(
        session_id=_SID, query_ref=ref, system_id="erp", question=_QUESTION,
        kg_context_attached=True, steps=[step], steps_dropped=2,
        final_answer=_ANSWER)


def _seed(tool_id: str = "toolu_w6") -> str:
    ref = mint_query_ref(_SID, tool_id)
    assert its.put_trace(_SID, ref, _entry(ref)) is True
    return ref


@pytest.fixture(autouse=True)
def _memory_stores(monkeypatch):
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=None))
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=None))
    monkeypatch.delenv("AGENTS_OBSERVABILITY_DRILLDOWN", raising=False)


# --------------------------------------------------------------------------- #
# Auth posture
# --------------------------------------------------------------------------- #

def test_non_admin_forbidden_before_anything_else(monkeypatch):
    calls: list = []
    monkeypatch.setattr(its, "get_trace", lambda *a: calls.append(a))
    with pytest.raises(HTTPException) as exc:
        atr.get_investigation_trace("not-a-ref", _admin_request(is_admin=False),
                                    session_id=_SID)
    assert exc.value.status_code == 403
    assert calls == []


# --------------------------------------------------------------------------- #
# Structured 400 — store never touched, no value echo
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_ref", ["q-XYZXYZXYZXYZ", "q-" + "a" * 11,
                                     "Q-3f9c2b81ab04", "3f9c2b81ab04",
                                     "q-3f9c2b81ab04'--", ""])
def test_invalid_query_ref_is_structured_400(monkeypatch, bad_ref):
    calls: list = []
    monkeypatch.setattr(its, "get_trace", lambda *a: calls.append(a))
    with pytest.raises(HTTPException) as exc:
        atr.get_investigation_trace(bad_ref, _admin_request(), session_id=_SID)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid query_ref"
    assert calls == []


@pytest.mark.parametrize("bad_sid", ['s"1', "a b", "", "🙂", "s" * 257])
def test_invalid_session_id_is_structured_400(monkeypatch, bad_sid):
    calls: list = []
    monkeypatch.setattr(its, "get_trace", lambda *a: calls.append(a))
    with pytest.raises(HTTPException) as exc:
        atr.get_investigation_trace(_ref(), _admin_request(), session_id=bad_sid)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid session id"
    assert calls == []


def test_session_id_defaults_to_missing_and_400s():
    with pytest.raises(HTTPException) as exc:
        atr.get_investigation_trace(_ref(), _admin_request())
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------- #
# Structured 404 — the exact detail string
# --------------------------------------------------------------------------- #

def test_unknown_ref_is_structured_404():
    with pytest.raises(HTTPException) as exc:
        atr.get_investigation_trace(_ref("never-stored"), _admin_request(),
                                    session_id=_SID)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Unknown or expired investigation trace"


def test_wrong_session_for_stored_ref_is_404():
    ref = _seed()
    with pytest.raises(HTTPException) as exc:
        atr.get_investigation_trace(ref, _admin_request(),
                                    session_id="other-session")
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------- #
# 200 — byte-passthrough of the stored W6-F2 entry
# --------------------------------------------------------------------------- #

def test_hit_returns_stored_entry_unreshaped():
    ref = _seed()
    out = atr.get_investigation_trace(ref, _admin_request(), session_id=_SID)
    assert out == its.get_trace(_SID, ref)          # byte-passthrough
    assert out["question"] == _QUESTION
    assert out["final_answer"] == _ANSWER
    assert out["steps_dropped"] == 2                # honest flags survive
    assert out["kg_context_attached"] is True
    assert out["steps"][0]["reasoning"] == _REASONING
    assert out["origin"] == "system-agent"


# --------------------------------------------------------------------------- #
# Family independence: Wave-5 entry present, trace absent ⇒ 404 here
# --------------------------------------------------------------------------- #

def test_wave5_query_entry_does_not_serve_this_route():
    ref = _ref("toolu_family")
    stored = provenance_store.put_turn_queries(_SID, {ref: {
        "system_id": "erp", "tool_name": "query_erp",
        "query_text": "SELECT 1", "kg_artifact_ids": [],
        "captured_at": "2026-08-04T18:00:00Z"}})
    assert stored == 1
    # /queries/ 200s …
    body = atr.get_provenance_query(ref, _admin_request(), session_id=_SID)
    assert body["executed_query"] == "SELECT 1"
    # … while /investigations/ 404s for the SAME (sid, ref)
    with pytest.raises(HTTPException) as exc:
        atr.get_investigation_trace(ref, _admin_request(), session_id=_SID)
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------- #
# Not drilldown-gated + registration
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", [None, "false", "true"])
def test_route_serves_regardless_of_drilldown_flag(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("AGENTS_OBSERVABILITY_DRILLDOWN", raising=False)
    else:
        monkeypatch.setenv("AGENTS_OBSERVABILITY_DRILLDOWN", flag)
    monkeypatch.setattr(atr, "_boto_session",
                        lambda: pytest.fail("boto3 touched by investigations route"))
    ref = _seed()
    out = atr.get_investigation_trace(ref, _admin_request(), session_id=_SID)
    assert "enabled" not in out
    assert out["final_answer"] == _ANSWER


def test_route_registered_on_admin_trace_prefix():
    paths = [r.path for r in atr.router.routes]
    assert "/api/admin/agents/traces/investigations/{query_ref}" in paths


# --------------------------------------------------------------------------- #
# Digest-only logging
# --------------------------------------------------------------------------- #

def test_logs_carry_no_trace_content_or_raw_ids(caplog):
    ref = _seed()
    with caplog.at_level(logging.DEBUG):
        atr.get_investigation_trace(ref, _admin_request(), session_id=_SID)
        with pytest.raises(HTTPException):
            atr.get_investigation_trace(_ref("missing"), _admin_request(),
                                        session_id=_SID)
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.name.startswith("agents.agents_trace_routes"))
    for fragment in (_QUESTION, _ANSWER, _REASONING, "SELECT", ref, _SID):
        assert fragment not in text
