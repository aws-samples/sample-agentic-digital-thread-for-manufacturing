# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Secure, metadata-only stream diagnostics bound to owner and session."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from typing import Any

from .runtime_integrity import public_scalar_metadata

_CAPTURE_ENV = "ADT_DEBUG_STREAM_CAPTURE"
_CAPTURE_TTL_SECONDS = 900
_CAPTURE_MAX_BYTES = 5_000_000
_CAPTURE_DIR = Path(tempfile.gettempdir()) / "adt-stream-diagnostics"
_LOCK = threading.Lock()
_REGISTRY: dict[str, "_CaptureRecord"] = {}


class CaptureAccessDenied(PermissionError):
    pass


@dataclass
class _CaptureRecord:
    path: Path
    owner_id: str
    session_id: str
    created_at: float


def _enabled() -> bool:
    return os.environ.get(_CAPTURE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8", "replace")).hexdigest()[:24]


def _expired_orphan_paths(cutoff: float) -> list[Path]:
    if not _CAPTURE_DIR.is_dir():
        return []
    expired: list[Path] = []
    for path in _CAPTURE_DIR.glob("capture-*.jsonl"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                expired.append(path)
        except OSError:
            continue
    return expired


def _cleanup_expired() -> None:
    cutoff = time.time() - _CAPTURE_TTL_SECONDS
    orphans = _expired_orphan_paths(cutoff)
    with _LOCK:
        expired = [key for key, record in _REGISTRY.items() if record.created_at < cutoff]
        registered_paths = {record.path for record in _REGISTRY.values()}
        for capture_id in expired:
            try:
                _REGISTRY.pop(capture_id).path.unlink(missing_ok=True)
            except OSError:
                pass
    for path in orphans:
        if path in registered_paths and path.exists():
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _event_category(payload: dict) -> str:
    if payload.get("error") not in (None, "", False, [], {}):
        return "error"
    if isinstance(payload.get("current_tool_use"), dict):
        return "tool_progress"
    if isinstance(payload.get("tool_result_metadata"), dict):
        return "tool_result"
    if isinstance(payload.get("response_integrity"), dict):
        return "integrity"
    if any(key in payload for key in ("data", "message", "reasoningText")):
        return "model_content_redacted"
    return "metadata"


def _event_metadata(line: str) -> str:
    payload_text = line[6:] if line.startswith("data: ") else line
    byte_count = len(payload_text.encode("utf-8", "replace"))
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return json.dumps({"event_category": "non_json", "bytes": byte_count})
    if not isinstance(payload, dict):
        return json.dumps({"event_category": "non_object", "bytes": byte_count})
    safe: dict[str, Any] = {
        "event_category": _event_category(payload),
        "bytes": byte_count,
        "field_count": min(len(payload), 10_000),
    }
    current = payload.get("current_tool_use")
    result = payload.get("tool_result_metadata")
    tool = current if isinstance(current, dict) else result if isinstance(result, dict) else {}
    if tool:
        safe["tool_hash"] = _hash(tool.get("name"))
        safe["call_hash"] = _hash(tool.get("toolUseId"))
    if isinstance(result, dict):
        safe.update(public_scalar_metadata(result.get("metadata")))
        safe.pop("tool", None)
    integrity = public_scalar_metadata(payload.get("response_integrity"), integrity=True)
    for key in ("verdict", "completeness", "claim_count", "learning_allowed"):
        if key in integrity:
            safe[f"integrity_{key}"] = integrity[key]
    return json.dumps(safe, separators=(",", ":"), default=str)


class StreamCapture:
    def __init__(self, capture_id: str, record: _CaptureRecord, file_obj) -> None:
        self.capture_id = capture_id
        self._record = record
        self._file = file_obj
        self._bytes = 0
        self._closed = False

    def write(self, line: str) -> None:
        if self._closed or not isinstance(line, str):
            return
        safe = _event_metadata(line) + "\n"
        encoded = safe.encode("utf-8", "replace")
        if self._bytes + len(encoded) > _CAPTURE_MAX_BYTES:
            return
        self._file.write(safe)
        self._bytes += len(encoded)

    def close(self) -> None:
        if not self._closed:
            self._file.close()
            self._closed = True


def open_stream_capture(*, owner_id: str, session_id: str) -> StreamCapture | None:
    _cleanup_expired()
    if not _enabled() or not owner_id or not session_id:
        return None
    _CAPTURE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(_CAPTURE_DIR, 0o700)  # 0o700 owner-only is intentionally MORE restrictive than the suggested 0o644  # nosemgrep: insecure-file-permissions
    capture_id = secrets.token_urlsafe(18)
    descriptor, path = tempfile.mkstemp(prefix="capture-", suffix=".jsonl", dir=_CAPTURE_DIR)
    os.chmod(path, 0o600)
    file_obj = os.fdopen(descriptor, "w", encoding="utf-8")
    record = _CaptureRecord(Path(path), owner_id[:200], session_id[:200], time.time())
    with _LOCK:
        _REGISTRY[capture_id] = record
    return StreamCapture(capture_id, record, file_obj)


def _authorized(record: _CaptureRecord, requester_id: str, session_id: str, is_admin: bool) -> bool:
    return bool(is_admin and requester_id and session_id
                and requester_id == record.owner_id and session_id == record.session_id)


def read_capture(capture_id: str, *, requester_id: str, session_id: str, is_admin: bool) -> bytes:
    _cleanup_expired()
    with _LOCK:
        record = _REGISTRY.get(capture_id)
    if record is None:
        raise FileNotFoundError("capture not found")
    if not _authorized(record, requester_id, session_id, is_admin):
        raise CaptureAccessDenied("capture session/owner/admin authorization required")
    return record.path.read_bytes()


def list_captures(*, requester_id: str, session_id: str, is_admin: bool) -> list[dict[str, Any]]:
    _cleanup_expired()
    if not is_admin or not requester_id or not session_id:
        raise CaptureAccessDenied("capture session/owner/admin authorization required")
    with _LOCK:
        records = list(_REGISTRY.items())
    return [{
        "capture_id": capture_id,
        "session_hash": _hash(record.session_id),
        "created_at": record.created_at,
        "size_bytes": record.path.stat().st_size if record.path.exists() else 0,
    } for capture_id, record in records if _authorized(record, requester_id, session_id, is_admin)]


def delete_capture(capture_id: str, *, requester_id: str, session_id: str, is_admin: bool) -> None:
    read_capture(
        capture_id, requester_id=requester_id, session_id=session_id, is_admin=is_admin,
    )
    with _LOCK:
        record = _REGISTRY.pop(capture_id)
    record.path.unlink(missing_ok=True)
