# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cross-worker per-system guidance delivery with durable snapshot CAS.

The process lock protects only the local read cache. Every mutation is an
atomic backend compare-and-set against a complete snapshot; DynamoDB owns the
cross-worker race through ``UpdateItem.ConditionExpression``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
import threading
from typing import Protocol

logger = logging.getLogger(__name__)


def guidance_digest(text: str) -> str:
    """Full, stable digest of guidance text; never truncate authority tokens."""
    return "sha256:" + hashlib.sha256((text or "").encode(
        "utf-8", "replace")).hexdigest()


@dataclass(frozen=True)
class GuidanceSnapshot:
    """Immutable authority token for one exact durable guidance state."""

    text: str
    version: int
    full_digest: str
    exists: bool
    legacy: bool

    @property
    def digest(self) -> str:
        """Compatibility alias; the value is always the full SHA-256 digest."""
        return self.full_digest

    @property
    def existence(self) -> bool:
        """Explicit existence spelling for reconciliation callers."""
        return self.exists

    @classmethod
    def absent(cls, version: int = 0) -> "GuidanceSnapshot":
        return cls("", int(version), guidance_digest(""), False, False)

    @classmethod
    def present(cls, text: str, version: int,
                *, legacy: bool = False) -> "GuidanceSnapshot":
        clean = text or ""
        return cls(clean, int(version), guidance_digest(clean), True, legacy)


class GuidanceConflict(RuntimeError):
    """The durable state no longer equals the caller's expected snapshot."""


class GuidanceUnavailable(RuntimeError):
    """The durable guidance backend cannot prove state."""


class GuidanceBackend(Protocol):
    def version(self, system_id: str) -> int: ...
    def load(self, system_id: str) -> tuple[str, int]: ...
    def snapshot(self, system_id: str) -> GuidanceSnapshot: ...
    def compare_and_set(self, system_id: str, expected: GuidanceSnapshot,
                        text: str) -> GuidanceSnapshot: ...
    def restore(self, system_id: str, expected: GuidanceSnapshot,
                target: GuidanceSnapshot) -> GuidanceSnapshot: ...
    def list_snapshots(self, *, max_items: int, max_pages: int
                       ) -> dict[str, GuidanceSnapshot]: ...


class NoOpBackend:
    """Reader fallback only; strict writes/listing must report unavailable."""

    def version(self, _system_id: str) -> int:
        return 0

    def load(self, _system_id: str) -> tuple[str, int]:
        return "", 0

    def snapshot(self, _system_id: str) -> GuidanceSnapshot:
        raise GuidanceUnavailable("system-guidance store unavailable")

    def compare_and_set(self, _system_id: str, _expected: GuidanceSnapshot,
                        _text: str) -> GuidanceSnapshot:
        raise GuidanceUnavailable("system-guidance store unavailable")

    def restore(self, _system_id: str, _expected: GuidanceSnapshot,
                _target: GuidanceSnapshot) -> GuidanceSnapshot:
        raise GuidanceUnavailable("system-guidance store unavailable")

    def list_snapshots(self, *, max_items: int, max_pages: int):
        raise GuidanceUnavailable("system-guidance store unavailable")


class DynamoGuidanceBackend:
    """DynamoDB snapshot store keyed by ``system_id``."""

    def __init__(self, table_name: str | None = None):
        self._table_name = (table_name if table_name is not None else
                            os.environ.get("ADT_SYSTEM_GUIDANCE_TABLE", ""))
        self._dynamodb = None

    def _table(self):
        if not self._table_name:
            return None
        if self._dynamodb is None:
            import boto3
            self._dynamodb = boto3.resource(
                "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        return self._dynamodb.Table(self._table_name)

    @staticmethod
    def _version(raw) -> int:
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _snapshot_item(cls, item: dict | None) -> GuidanceSnapshot:
        row = item or {}
        if "guidance" not in row:
            return GuidanceSnapshot.absent(cls._version(row.get("version")))
        text = str(row.get("guidance") or "")
        stored_digest = str(row.get("digest") or "")
        computed_digest = guidance_digest(text)
        if stored_digest and stored_digest != computed_digest:
            raise GuidanceUnavailable("system-guidance digest mismatch")
        return GuidanceSnapshot(
            text=text, version=cls._version(row.get("version")),
            full_digest=computed_digest, exists=True,
            legacy=not stored_digest)

    def snapshot(self, system_id: str) -> GuidanceSnapshot:
        table = self._table()
        if table is None:
            raise GuidanceUnavailable("system-guidance store unavailable")
        response = table.get_item(Key={"system_id": system_id}, ConsistentRead=True)
        return self._snapshot_item(response.get("Item"))

    def version(self, system_id: str) -> int:
        table = self._table()
        if table is None:
            return 0
        try:
            response = table.get_item(
                Key={"system_id": system_id}, ProjectionExpression="#v",
                ExpressionAttributeNames={"#v": "version"})
            return self._version((response.get("Item") or {}).get("version"))
        except Exception as exc:  # noqa: BLE001 - advisory reader is fail-soft
            logger.warning("system-guidance version GET failed for %s: %s",
                           system_id, exc)
            return 0

    def load(self, system_id: str) -> tuple[str, int]:
        table = self._table()
        if table is None:
            return "", 0
        try:
            snapshot = self.snapshot(system_id)
            return snapshot.text, snapshot.version
        except Exception as exc:  # noqa: BLE001 - advisory reader is fail-soft
            logger.warning("system-guidance load failed for %s: %s", system_id, exc)
            return "", 0

    @staticmethod
    def _condition(expected: GuidanceSnapshot) -> tuple[str, dict, dict]:
        names = {"#g": "guidance", "#v": "version", "#d": "digest"}
        values: dict = {}
        if not expected.exists:
            if expected.version == 0:
                condition = ("attribute_not_exists(#g) AND "
                             "attribute_not_exists(#v) AND "
                             "attribute_not_exists(#d)")
            else:
                values[":expected_version"] = str(expected.version)
                condition = ("attribute_not_exists(#g) AND "
                             "#v = :expected_version AND "
                             "attribute_not_exists(#d)")
            return condition, names, values
        values[":expected_version"] = str(expected.version)
        if expected.legacy:
            values[":expected_text"] = expected.text
            condition = ("attribute_exists(#g) AND #v = :expected_version AND "
                         "#g = :expected_text AND attribute_not_exists(#d)")
        else:
            values[":expected_digest"] = expected.full_digest
            condition = ("attribute_exists(#g) AND #v = :expected_version AND "
                         "#d = :expected_digest")
        return condition, names, values

    @staticmethod
    def _is_conflict(exc: Exception) -> bool:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        return (code == "ConditionalCheckFailedException" or
                "Conditional" in type(exc).__name__)

    def _update(self, system_id: str, expected: GuidanceSnapshot,
                target: GuidanceSnapshot) -> GuidanceSnapshot:
        table = self._table()
        if table is None:
            raise GuidanceUnavailable("system-guidance store unavailable")
        condition, names, values = self._condition(expected)
        next_version = expected.version + 1
        if target.exists:
            text = target.text or ""
            values.update({":text": text, ":next_version": str(next_version),
                           ":next_digest": guidance_digest(text)})
            update = "SET #g = :text, #v = :next_version, #d = :next_digest"
        else:
            values[":next_version"] = str(next_version)
            update = "SET #v = :next_version REMOVE #g, #d"
        try:
            response = table.update_item(
                Key={"system_id": system_id}, UpdateExpression=update,
                ConditionExpression=condition, ExpressionAttributeNames=names,
                ExpressionAttributeValues=values, ReturnValues="ALL_NEW")
        except Exception as exc:
            if self._is_conflict(exc):
                raise GuidanceConflict() from exc
            raise
        snapshot = self._snapshot_item(response.get("Attributes"))
        expected_target = (GuidanceSnapshot.present(target.text, next_version)
                           if target.exists else GuidanceSnapshot.absent(next_version))
        if snapshot != expected_target:
            raise GuidanceUnavailable("system-guidance update readback failed")
        return snapshot

    def compare_and_set(self, system_id: str, expected: GuidanceSnapshot,
                        text: str) -> GuidanceSnapshot:
        return self._update(system_id, expected,
                            GuidanceSnapshot.present(text or "", 0))

    def restore(self, system_id: str, expected: GuidanceSnapshot,
                target: GuidanceSnapshot) -> GuidanceSnapshot:
        return self._update(system_id, expected, target)

    def list_snapshots(self, *, max_items: int = 1000,
                       max_pages: int = 10) -> dict[str, GuidanceSnapshot]:
        if not 1 <= max_items <= 1000 or not 1 <= max_pages <= 10:
            raise ValueError("system-guidance scan bounds out of range")
        table = self._table()
        if table is None:
            raise GuidanceUnavailable("system-guidance store unavailable")
        rows: dict[str, GuidanceSnapshot] = {}
        start = None
        scanned = 0
        for _page in range(max_pages):
            kwargs = {"Limit": max_items - scanned, "ConsistentRead": True}
            if start:
                kwargs["ExclusiveStartKey"] = start
            response = table.scan(**kwargs)
            items = response.get("Items", [])
            scanned += len(items)
            for item in items:
                system_id = str(item.get("system_id") or "")
                if system_id:
                    rows[system_id] = self._snapshot_item(item)
            start = response.get("LastEvaluatedKey")
            if not start:
                return rows
            if scanned >= max_items:
                raise GuidanceUnavailable("system-guidance scan bounded")
        raise GuidanceUnavailable("system-guidance scan bounded")


def _default_backend() -> GuidanceBackend:
    return (DynamoGuidanceBackend() if os.environ.get("ADT_SYSTEM_GUIDANCE_TABLE")
            else NoOpBackend())


class SystemGuidanceStore:
    """Durable CAS plus an in-process, version-validated read cache."""

    def __init__(self, backend: GuidanceBackend | None = None):
        self._backend = backend if backend is not None else _default_backend()
        self._cache: dict[str, tuple[int, str]] = {}
        self._lock = threading.Lock()

    def set_backend(self, backend: GuidanceBackend) -> None:
        with self._lock:
            self._backend = backend
            self._cache.clear()

    def snapshot(self, system_id: str) -> GuidanceSnapshot:
        if not system_id:
            return GuidanceSnapshot.absent()
        loader = getattr(self._backend, "snapshot", None)
        if loader:
            return loader(system_id)
        text, version = self._backend.load(system_id)
        return GuidanceSnapshot.present(text, version, legacy=True)

    def current_version(self, system_id: str) -> int:
        return self._backend.version(system_id) if system_id else 0

    def compare_and_set(self, system_id: str, expected: GuidanceSnapshot,
                        text: str) -> GuidanceSnapshot:
        if not system_id:
            raise GuidanceUnavailable("system_id required")
        writer = getattr(self._backend, "compare_and_set", None)
        if writer is None:
            raise GuidanceUnavailable("backend does not support guidance CAS")
        result = writer(system_id, expected, text or "")
        with self._lock:
            self._cache.pop(system_id, None)
        return result

    def restore(self, system_id: str, expected: GuidanceSnapshot,
                target: GuidanceSnapshot) -> GuidanceSnapshot:
        if not system_id:
            raise GuidanceUnavailable("system_id required")
        writer = getattr(self._backend, "restore", None)
        if writer is None:
            raise GuidanceUnavailable("backend does not support guidance CAS")
        result = writer(system_id, expected, target)
        with self._lock:
            self._cache.pop(system_id, None)
        return result

    def write(self, system_id: str, text: str) -> int:
        if not system_id:
            return 0
        if getattr(self._backend, "compare_and_set", None) is None:
            # Backward compatibility for old injected readers/mirror test doubles.
            # Production backends expose CAS and never enter this path.
            version = self._backend.version(system_id) + 1
            self._backend.save(system_id, text or "", version)
            with self._lock:
                self._cache.pop(system_id, None)
            return version
        expected = self.snapshot(system_id)
        return self.compare_and_set(system_id, expected, text).version

    def publish_exact(self, system_id: str, text: str,
                      expected: GuidanceSnapshot | None = None) -> int:
        before = expected if expected is not None else self.snapshot(system_id)
        written = self.compare_and_set(system_id, before, text)
        observed = self.snapshot(system_id)
        if observed != written or observed.text != (text or ""):
            raise GuidanceUnavailable("system-guidance exact readback failed")
        return written.version

    def read_strict(self, system_id: str) -> str:
        if not system_id:
            raise GuidanceUnavailable("system_id required")
        return self.snapshot(system_id).text

    def list_snapshots(self, *, max_items: int = 1000,
                       max_pages: int = 10) -> dict[str, GuidanceSnapshot]:
        lister = getattr(self._backend, "list_snapshots", None)
        if lister is None:
            raise GuidanceUnavailable("backend does not support guidance listing")
        return lister(max_items=max_items, max_pages=max_pages)

    def read(self, system_id: str) -> str:
        if not system_id:
            return ""
        version = self._backend.version(system_id)
        with self._lock:
            cached = self._cache.get(system_id)
            if cached is not None and cached[0] == version:
                return cached[1]
        text, loaded_version = self._backend.load(system_id)
        with self._lock:
            self._cache[system_id] = (loaded_version, text)
        return text


_store: SystemGuidanceStore | None = None


def _get_store() -> SystemGuidanceStore:
    global _store
    if _store is None:
        _store = SystemGuidanceStore()
    return _store


def set_store(store: SystemGuidanceStore) -> None:
    global _store
    _store = store


def read(system_id: str) -> str:
    return _get_store().read(system_id)


def write(system_id: str, text: str) -> int:
    return _get_store().write(system_id, text)


def current_version(system_id: str) -> int:
    return _get_store().current_version(system_id)


def snapshot(system_id: str) -> GuidanceSnapshot:
    return _get_store().snapshot(system_id)


def compare_and_set(system_id: str, expected: GuidanceSnapshot,
                    text: str) -> GuidanceSnapshot:
    return _get_store().compare_and_set(system_id, expected, text)


def restore(system_id: str, expected: GuidanceSnapshot,
            target: GuidanceSnapshot) -> GuidanceSnapshot:
    return _get_store().restore(system_id, expected, target)


def publish_exact(system_id: str, text: str,
                  expected: GuidanceSnapshot | None = None) -> int:
    return _get_store().publish_exact(system_id, text, expected)


def read_strict(system_id: str) -> str:
    return _get_store().read_strict(system_id)


def list_snapshots(*, max_items: int = 1000,
                   max_pages: int = 10) -> dict[str, GuidanceSnapshot]:
    return _get_store().list_snapshots(max_items=max_items, max_pages=max_pages)
