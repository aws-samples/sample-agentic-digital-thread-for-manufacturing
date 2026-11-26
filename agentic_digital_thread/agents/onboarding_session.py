# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Durable onboarding session state in DynamoDB with conditional-write gates.

The onboarding flow walks a small state machine — ``discovering -> discovered ->
mapped -> resolved -> provisioning -> {onboarded|failed}`` — keyed by
``system_id``. Each transition is a DynamoDB **conditional write**: the new
status is written only when the item's current status matches the expected
predecessor. That makes the ordering gate *durable* and *load-bearing* (any
holder of gateway credentials can call a tool directly, bypassing the chat
agent) and lets the provisioning claim be **deduplicated** — two concurrent
callers race on ``status = 'resolved'`` and exactly one wins.

Design constraints (user steering 2026-06-30 — **no fallback logic**):

* **DDB-only.** There is no in-memory or local fallback store. A single clean
  path.
* **Errors propagate.** A conditional-check failure raises a typed error the
  caller can distinguish (:class:`OutOfOrderError`, :class:`DuplicateProvisionError`,
  :class:`SessionExistsError`, :class:`SessionNotFoundError`). Nothing is
  swallowed or defaulted.
* **No S3 offload.** ``discovered_tables`` / ``mappings`` and the other JSON
  blobs are stored **inline**. If a serialized item would exceed a safe
  DynamoDB size (~380 KB, under the 400 KB hard cap) the write **raises**
  :class:`ItemTooLargeError` — it is never truncated or offloaded.

Item shape (table ``adt-onboarding-sessions``, PK ``system_id``)::

    system_id              (S)  partition key
    status                 (S)  state-machine status (see STATUS_* below)
    current_step           (N)  TOOL_STEPS index of the last completed step
    discovered_tables      (S)  JSON blob (inline)
    discovery_seat         (S)  JSON blob (inline), seat-backed Tulip only: the
                                NON-secret identity of the seat discovery read
                                through (tulip_seat_contract.seat_identity)
    mappings               (S)  JSON blob (inline)
    rules                  (S)  JSON blob (inline)
    value_mappings         (S)  JSON blob (inline)
    connection_type        (S)
    schema                 (S)  JSON blob (inline)
    initiated_by           (S)  user sub that started onboarding (audit)
    provision_execution_arn(S)  read by agents.provisioner.handlers.validate_session.
                                Was written by the Step Functions provisioner's
                                claim; that provisioner was removed 2026-10-29
                                and nothing writes it now. The app's provision
                                records provision_job_id instead.
    created_at             (S)  ISO-8601
    updated_at             (S)  ISO-8601
    ttl                    (N)  epoch seconds — DynamoDB TTL reaps abandoned sessions
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# --------------------------------------------------------------------------- #
# State-machine statuses
# --------------------------------------------------------------------------- #
STATUS_DISCOVERING = "discovering"
STATUS_DISCOVERED = "discovered"
STATUS_MAPPED = "mapped"
STATUS_RESOLVED = "resolved"
STATUS_PROVISIONING = "provisioning"
STATUS_ONBOARDED = "onboarded"
STATUS_FAILED = "failed"

VALID_STATUSES = frozenset({
    STATUS_DISCOVERING, STATUS_DISCOVERED, STATUS_MAPPED, STATUS_RESOLVED,
    STATUS_PROVISIONING, STATUS_ONBOARDED, STATUS_FAILED,
})

# Legal forward transitions. claim_provisioning() owns resolved->provisioning and
# finalize() owns provisioning->{onboarded,failed}; advance_status() handles the
# rest. Every transition is enforced at the database via a conditional write.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_DISCOVERING: frozenset({STATUS_DISCOVERED}),
    STATUS_DISCOVERED: frozenset({STATUS_MAPPED}),
    STATUS_MAPPED: frozenset({STATUS_RESOLVED}),
    STATUS_RESOLVED: frozenset({STATUS_PROVISIONING}),
    STATUS_PROVISIONING: frozenset({STATUS_ONBOARDED, STATUS_FAILED}),
}

# JSON-encoded (inline) blob attributes. Stored as DynamoDB strings so floats and
# empty containers round-trip cleanly and the size guard is exact.
JSON_FIELDS = frozenset({
    "discovered_tables", "mappings", "rules", "value_mappings", "schema",
    "discovery_seat",
})

# DynamoDB hard item cap is 400 KB. Stay safely under it; blobs are stored inline.
MAX_ITEM_BYTES = 380 * 1024

TABLE_NAME = os.environ.get("ONBOARDING_SESSIONS_TABLE", "adt-onboarding-sessions")

# 7-day TTL on abandoned sessions (matches the conversation-memory event TTL).
SESSION_TTL_SECONDS = int(os.environ.get("ONBOARDING_SESSION_TTL_SECONDS", str(7 * 24 * 3600)))


# --------------------------------------------------------------------------- #
# Typed errors (callers branch on these — they are NEVER swallowed internally)
# --------------------------------------------------------------------------- #
class OnboardingSessionError(Exception):
    """Base class for onboarding-session store errors."""


class SessionNotFoundError(OnboardingSessionError):
    """No session item exists for the given ``system_id``."""


class SessionExistsError(OnboardingSessionError):
    """A session item already exists for the given ``system_id``."""


class OutOfOrderError(OnboardingSessionError):
    """A status transition was attempted from the wrong predecessor state.

    Raised when the DynamoDB conditional write fails because the item's current
    ``status`` did not match the required ``from_status`` (the durable ordering
    gate rejected an out-of-sequence step).
    """


class DuplicateProvisionError(OnboardingSessionError):
    """A provisioning claim lost the race (or the session was not ``resolved``).

    Raised when the conditional claim of ``resolved -> provisioning`` fails:
    either a concurrent caller already claimed it (dedup) or the session is not
    in ``resolved`` state.
    """


class ItemTooLargeError(OnboardingSessionError):
    """The serialized item would exceed the safe DynamoDB item-size threshold."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_epoch() -> int:
    return int(time.time()) + SESSION_TTL_SECONDS


def _encode_fields(fields: dict) -> dict:
    """JSON-encode the inline blob attributes; pass scalars through unchanged."""
    out: dict = {}
    for key, value in fields.items():
        if value is None:
            continue
        out[key] = json.dumps(value, default=str) if key in JSON_FIELDS else value
    return out


def _decode_item(item: dict) -> dict:
    """Decode JSON blob attributes back into native Python objects."""
    decoded = dict(item)
    for key in JSON_FIELDS:
        if key in decoded and isinstance(decoded[key], str):
            decoded[key] = json.loads(decoded[key])
    return decoded


def _check_size(attributes: dict) -> None:
    """Raise :class:`ItemTooLargeError` if the (already-encoded) attributes are
    too large to store inline.

    The blob attributes are encoded strings at this point, so a UTF-8 byte count
    of the full attribute map is a faithful proxy for the DynamoDB item size.
    """
    # TODO(deferred): S3 offload for large HighByte UNS discovery — see plan R3.
    # For now oversize payloads RAISE (no truncation, no offload).
    size = len(json.dumps(attributes, default=str).encode("utf-8"))
    if size > MAX_ITEM_BYTES:
        raise ItemTooLargeError(
            f"Onboarding session item is {size} bytes, over the "
            f"{MAX_ITEM_BYTES}-byte inline limit. Store discovered_tables/mappings "
            f"smaller, or implement S3 offload (deferred — plan R3)."
        )


class OnboardingSessionStore:
    """DynamoDB-backed store for onboarding session state (single clean path)."""

    def __init__(self, table_name: str | None = None, *, dynamodb_resource=None) -> None:
        self._table_name = table_name or TABLE_NAME
        if dynamodb_resource is None:
            region = os.environ.get("AWS_REGION", "us-east-1")
            dynamodb_resource = boto3.resource("dynamodb", region_name=region)
        self._table = dynamodb_resource.Table(self._table_name)

    # -- reads -------------------------------------------------------------- #
    def get_session(self, system_id: str, *, consistent: bool = False) -> dict:
        """Return the decoded session item. Raises :class:`SessionNotFoundError`
        if no item exists (no silent ``None`` fallback). ``consistent`` reads
        with ``ConsistentRead`` (every write acknowledged before is seen)."""
        read = {"ConsistentRead": True} if consistent else {}
        response = self._table.get_item(Key={"system_id": system_id}, **read)
        item = response.get("Item")
        if item is None:
            raise SessionNotFoundError(f"No onboarding session for system_id={system_id!r}")
        return _decode_item(item)

    # -- create ------------------------------------------------------------- #
    def create_session(self, system_id: str, initiated_by: str, **fields) -> dict:
        """Create a new session at ``status='discovering'``.

        Conditional on the item not already existing — re-creating an existing
        session raises :class:`SessionExistsError` (no clobber)."""
        now = _now_iso()
        item = {
            "system_id": system_id,
            "status": STATUS_DISCOVERING,
            "initiated_by": initiated_by,
            "created_at": now,
            "updated_at": now,
            "ttl": _ttl_epoch(),
        }
        item.update(_encode_fields(fields))
        _check_size(item)
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(system_id)",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise SessionExistsError(
                    f"Onboarding session already exists for system_id={system_id!r}"
                ) from exc
            raise
        return _decode_item(item)

    # -- transitions -------------------------------------------------------- #
    def advance_status(self, system_id: str, from_status: str, to_status: str, **fields) -> None:
        """Advance ``from_status -> to_status`` as a conditional write.

        The write succeeds only if the item's current ``status`` equals
        ``from_status``; otherwise :class:`OutOfOrderError` is raised. Extra
        ``fields`` are written atomically with the status change."""
        if to_status not in LEGAL_TRANSITIONS.get(from_status, frozenset()):
            raise ValueError(
                f"Illegal transition {from_status!r} -> {to_status!r}; "
                f"legal targets are {sorted(LEGAL_TRANSITIONS.get(from_status, []))}"
            )
        self._conditional_update(
            system_id, from_status, to_status, fields,
            on_condition_fail=OutOfOrderError(
                f"Cannot move system_id={system_id!r} {from_status!r} -> {to_status!r}: "
                f"current status is not {from_status!r}"
            ),
        )

    def claim_provisioning(self, system_id: str, **fields) -> None:
        """Atomically claim ``resolved -> provisioning`` (dedup point).

        Exactly one concurrent caller wins; the loser (or any caller when the
        session is not ``resolved``) gets :class:`DuplicateProvisionError`."""
        self._conditional_update(
            system_id, STATUS_RESOLVED, STATUS_PROVISIONING, fields,
            on_condition_fail=DuplicateProvisionError(
                f"Provisioning already claimed for system_id={system_id!r} "
                f"(or status is not {STATUS_RESOLVED!r})"
            ),
        )

    def finalize(self, system_id: str, status: str, **fields) -> None:
        """Write a terminal status (``onboarded`` or ``failed``) from
        ``provisioning``. Raises :class:`OutOfOrderError` if not provisioning."""
        if status not in (STATUS_ONBOARDED, STATUS_FAILED):
            raise ValueError(
                f"finalize() status must be {STATUS_ONBOARDED!r} or {STATUS_FAILED!r}, "
                f"got {status!r}"
            )
        self._conditional_update(
            system_id, STATUS_PROVISIONING, status, fields,
            on_condition_fail=OutOfOrderError(
                f"Cannot finalize system_id={system_id!r} to {status!r}: "
                f"current status is not {STATUS_PROVISIONING!r}"
            ),
        )

    # -- delete ------------------------------------------------------------- #
    def delete_session_if_status(self, system_id: str, status: str) -> None:
        """Delete the session only while its status is still ``status`` (a
        conditional ``DeleteItem``). Used by the chat's ``reset_system`` to
        start over a half-done onboarding: a provisioning claim that moved the
        session on in the meantime makes the delete fail instead of removing
        a session a provisioner uses. Raises :class:`OutOfOrderError` when the
        status differs or the session is gone."""
        try:
            self._table.delete_item(
                Key={"system_id": system_id},
                ConditionExpression="#status = :expected",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":expected": status},
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise OutOfOrderError(
                    f"Cannot delete the session of system_id={system_id!r}: its status "
                    f"is no longer {status!r}"
                ) from exc
            raise

    def delete_session(self, system_id: str) -> bool:
        """Delete the session item so a fresh onboarding can start.

        Used by ``reset_system`` and a system's removal to clear durable state
        (the DDB analogue of the old in-memory ``_session.clear()``).
        Idempotent: deleting a non-existent item is a no-op (DynamoDB
        ``DeleteItem`` does not require existence). Returns whether a session
        was there (``ReturnValues=ALL_OLD``)."""
        response = self._table.delete_item(Key={"system_id": system_id},
                                           ReturnValues="ALL_OLD")
        return bool((response or {}).get("Attributes"))

    # -- internals ---------------------------------------------------------- #
    def _conditional_update(self, system_id, from_status, to_status, fields, *, on_condition_fail):
        """Issue a status-gated UpdateItem; map a conditional failure to a typed
        error. Every attribute name is aliased to dodge DynamoDB reserved words."""
        encoded = _encode_fields(fields)
        encoded.update({"status": to_status, "updated_at": _now_iso(), "ttl": _ttl_epoch()})
        _check_size(encoded)

        names = {"#status": "status"}
        values = {":to": to_status, ":from": from_status}
        set_clauses = ["#status = :to"]
        for i, (key, value) in enumerate(encoded.items()):
            if key == "status":
                continue
            placeholder_name = f"#f{i}"
            placeholder_value = f":v{i}"
            names[placeholder_name] = key
            values[placeholder_value] = value
            set_clauses.append(f"{placeholder_name} = {placeholder_value}")

        try:
            self._table.update_item(
                Key={"system_id": system_id},
                UpdateExpression="SET " + ", ".join(set_clauses),
                ConditionExpression="#status = :from",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                raise on_condition_fail from exc
            # The _check_size() guard above is best-effort: it only sees THIS
            # update's delta (the encoded fields being written), not the cumulative
            # stored item. A cumulative item that crosses the DynamoDB 400 KB hard
            # cap therefore slips past it and DDB rejects the write with a raw
            # size-limit ValidationException — that DDB-side error is the backstop.
            # Re-raise it as the typed ItemTooLargeError (loud; no truncation, no
            # fallback) so callers see the same error class as the proactive guard.
            if _is_item_size_exceeded(exc):
                raise ItemTooLargeError(
                    f"Onboarding session item for system_id={system_id!r} exceeds the "
                    f"DynamoDB 400 KB item-size limit (cumulative). Store "
                    f"discovered_tables/mappings smaller, or implement S3 offload "
                    f"(deferred — plan R3)."
                ) from exc
            raise


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _is_item_size_exceeded(exc: ClientError) -> bool:
    """True if ``exc`` is the DynamoDB cumulative item-size ValidationException.

    DynamoDB raises ``ValidationException`` with the message "Item size has
    exceeded the maximum allowed size" when a write would push the stored item
    over the 400 KB hard cap. This is the backstop for the best-effort per-update
    :func:`_check_size` guard (which only sees the per-update delta)."""
    err = exc.response.get("Error", {})
    if err.get("Code") != "ValidationException":
        return False
    return "item size has exceeded" in err.get("Message", "").lower()
