# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Action-aware explicit-list proposal batching (ADR-0063 D4)."""
from __future__ import annotations

import json
import logging
from typing import Any, cast

logger = logging.getLogger(__name__)

BATCH_MAX = 50
BODY_KEYS = frozenset({"action", "mapping_ids"})
ACTIONS = ("accept", "reject")
_PATTERN_CHARS = frozenset("*%?")
RESULT_ERROR_CODES = frozenset({
    "not_found", "not_pending", "specialized_accept_required",
    "non_actionable", "eligibility_unavailable", "backend_unavailable",
    "action_rejected", "action_failed",
})
_SPECIALIZED_ACCEPT_TYPES = frozenset({"guidance_note"})


class BatchRequestError(ValueError):
    """A request violation represented only by a fixed public code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_request(body: object) -> tuple[str, list[str]]:
    """Validate an explicit, bounded id list; predicates never exist."""
    if not isinstance(body, dict):
        raise BatchRequestError("invalid_body")
    if set(body) - BODY_KEYS:
        raise BatchRequestError("unsupported_selector")
    action = str(body.get("action") or "").strip().lower()
    if action not in ACTIONS:
        raise BatchRequestError("invalid_action")
    ids = _literal_ids(body.get("mapping_ids"))
    if len(ids) > BATCH_MAX:
        raise BatchRequestError("batch_too_large")
    return action, ids


def _literal_ids(raw: object) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise BatchRequestError("invalid_mapping_ids")
    ids: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise BatchRequestError("invalid_mapping_ids")
        mapping_id = value.strip()
        if any(char in mapping_id for char in _PATTERN_CHARS):
            raise BatchRequestError("pattern_not_allowed")
        if mapping_id not in ids:
            ids.append(mapping_id)
    return ids


def action_eligibility(row: dict) -> dict:
    """Additive read-time accept/reject eligibility from current row state."""
    if str(row.get("status") or "") != "pending":
        denied = {"eligible": False, "code": "not_pending"}
        return {"accept": dict(denied), "reject": dict(denied)}
    accept = _accept_eligibility(row)
    return {"accept": accept, "reject": {"eligible": True}}


def _accept_eligibility(row: dict) -> dict:
    if str(row.get("proposed_type") or "") in _SPECIALIZED_ACCEPT_TYPES:
        return {"eligible": False, "code": "specialized_accept_required"}
    try:
        from .self_learning import _non_executable_mapping_item
        if _non_executable_mapping_item(row):
            return {"eligible": False, "code": "non_actionable"}
    except Exception:
        return {"eligible": False, "code": "eligibility_unavailable"}
    return {"eligible": True}


def adjudicate(action: str, mapping_ids: list[str], actor: str = "") -> dict:
    """Recheck every explicit id, then invoke the existing single-row core."""
    results = [_one(action, mapping_id, actor) for mapping_id in mapping_ids]
    failed = [item["mapping_id"] for item in results if not item["ok"]]
    return {"action": action, "requested": len(mapping_ids),
            "succeeded": len(mapping_ids) - len(failed), "failed": len(failed),
            "failed_ids": failed, "results": results}


def _one(action: str, mapping_id: str, actor: str) -> dict:
    from . import self_learning
    backend = cast(Any, self_learning)
    try:
        row = backend.get_mapping(mapping_id)
    except Exception as exc:  # backend details are never public
        logger.warning("batch lookup failed id=%s category=%s",
                       mapping_id, type(exc).__name__)
        return _failure(mapping_id, "backend_unavailable")
    if not row:
        return _failure(mapping_id, "not_found")
    eligibility = action_eligibility(row).get(action) or {}
    if not eligibility.get("eligible"):
        return _failure(mapping_id, str(eligibility.get("code") or
                                        "ineligible"))
    return _invoke(action, mapping_id, actor)


def _invoke(action: str, mapping_id: str, actor: str) -> dict:
    from . import self_learning
    backend = cast(Any, self_learning)
    handler = (backend.accept_mapping if action == "accept"
               else backend.reject_mapping)
    try:
        result = (handler(mapping_id, actor=actor) if actor
                  else handler(mapping_id))
        return _outcome(mapping_id, result)
    except ValueError:
        return _failure(mapping_id, "action_rejected")
    except Exception as exc:  # fixed code only; no exception text leaves backend
        logger.warning("batch action failed id=%s action=%s category=%s",
                       mapping_id, action, type(exc).__name__)
        return _failure(mapping_id, "action_failed")


def _outcome(mapping_id: str, result: object) -> dict:
    code = int(getattr(result, "status_code", 200) or 200)
    payload = _payload(result)
    if code < 400 and not payload.get("error"):
        return {"mapping_id": mapping_id, "ok": True,
                "status": str(payload.get("status") or "")}
    public_code = "not_found" if code == 404 or _is_not_found(payload) else (
        "action_rejected" if code == 400 else "action_failed")
    return _failure(mapping_id, public_code)


def _payload(result: object) -> dict:
    if isinstance(result, dict):
        return result
    try:
        payload = json.loads(getattr(result, "body", b"") or b"{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _is_not_found(payload: dict) -> bool:
    return "not found" in str(payload.get("error") or "").lower()


def _failure(mapping_id: str, code: str) -> dict:
    public_code = code if code in RESULT_ERROR_CODES else "action_failed"
    return {"mapping_id": mapping_id, "ok": False, "error_code": public_code}
