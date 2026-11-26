# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import hashlib
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Unconfigured-store visibility
#
# Every method below no-ops when no table name is configured. That degradation
# is LEGITIMATE for local development and the offline unit lane (which run with
# no DynamoDB at all), but it must be VISIBLE: a DEPLOYED environment whose
# task definition never received FEEDBACK_TABLE_NAME would otherwise discard
# every thumbs-up/down while ``POST /api/feedback`` answers
# ``{"status": "success"}`` and nothing in the logs says otherwise.
#
# The no-op is announced at WARNING, naming the storage kind and the
# operation, so the gap is one CloudWatch Logs Insights filter away
# ("storage_unconfigured"). Deduped to ONCE per (kind, operation) per process:
# UsageStorage writes on EVERY chat turn, and a per-call warning on that path
# would flood the log group hard enough to get the signal muted — which is how
# the breadcrumb gets lost again. Subsequent occurrences stay at DEBUG.
_UNCONFIGURED_WARNED: set[tuple[str, str]] = set()


class StorageReadError(Exception):
    """A strict read (``DynamoDBStorage.query_bounded``) failed, or its table
    is not configured. The cause is chained; its text is never shown to a
    caller (``public_errors`` classifies and logs it)."""


def reset_unconfigured_warnings() -> None:
    """Clear the once-per-process warning dedupe (test seam only)."""
    _UNCONFIGURED_WARNED.clear()


def _warn_unconfigured(kind: str, op: str) -> None:
    """Announce a no-op caused by an unconfigured table. Stable log key:
    ``storage_unconfigured``."""
    key = (kind, op)
    if key in _UNCONFIGURED_WARNED:
        logger.debug("storage_unconfigured: %s.%s skipped (no table configured)",
                     kind, op)
        return
    _UNCONFIGURED_WARNED.add(key)
    logger.warning(
        "storage_unconfigured: %s.%s is a NO-OP — no DynamoDB table name is "
        "configured for this storage, so writes are DISCARDED and reads are "
        "always empty. Expected in local development; in a deployed "
        "environment it means the task definition is missing the table env var.",
        kind, op,
    )


class DynamoDBStorage:
    def __init__(self, table_name: str | None, region: str = "us-east-1",
                 kind: str | None = None):
        self.table_name = table_name
        self.region = region
        # Human-readable label for the unconfigured-store warning: the owning
        # wrapper's class name (FeedbackStorage, UsageStorage, …) so the log
        # line says WHICH data is being dropped, not just "a dynamodb table".
        self.kind = kind or type(self).__name__
        self._table = None

    @property
    def is_configured(self) -> bool:
        """True when a table name is set, i.e. when writes actually persist.

        The honest seam for a caller that needs to know. ``put_item`` /
        ``delete_item`` deliberately keep returning True in the unconfigured
        case (see ``put_item``), so the boolean they return can never answer
        this question.
        """
        return bool(self.table_name)

    def _get_table(self):
        if self._table is None:
            self._table = boto3.resource("dynamodb", region_name=self.region).Table(self.table_name)
        return self._table

    def put_item(self, item: dict) -> bool:
        # CONTRACT DECISION (deliberate — do not "fix" this to False without
        # reading every caller): an unconfigured table still returns True.
        #
        # Why True stays: no production caller inspects this boolean
        # (FeedbackStorage/UsageStorage/GuardrailStorage ``.store()``,
        # PromptTemplateStorage/DashboardCardStorage ``.save()`` — every call
        # site in agents/ discards it), so flipping the value would change no
        # behaviour today while arming a trap for the FIRST caller that starts
        # honouring it: local development and the offline unit lane run
        # table-less ON PURPOSE, and would begin reporting hard failures for a
        # configuration that is correct there.
        #
        # What was actually deceptive was not the return value but the SILENCE
        # around it — a DEBUG line nobody greps. That is what changed: the
        # no-op now warns (once per kind+operation per process), and
        # ``is_configured`` gives any caller that genuinely cares an honest
        # answer without overloading this boolean with two meanings
        # ("persisted" vs "did not need to persist").
        if not self.table_name:
            _warn_unconfigured(self.kind, "put_item")
            return True
        try:
            self._get_table().put_item(Item=item)
            return True
        except Exception as e:
            logger.error("put_item failed: %s", e)
            return False

    def query(self, key_condition: dict, **kwargs) -> list[dict]:
        if not self.table_name:
            _warn_unconfigured(self.kind, "query")
            return []
        try:
            pk, pk_val = next(iter(key_condition.items()))
            condition = Key(pk).eq(pk_val)
            if "sk_between" in kwargs:
                sk, (lo, hi) = kwargs.pop("sk_between")
                condition &= Key(sk).between(lo, hi)
            table = self._get_table()
            # Paginate for the same reason scan() does (~1MB per page): the
            # feedback rows carry a full assistant response each, so a busy
            # day's partition exceeds one page and an unpaginated query would
            # silently truncate the admin feedback listing. Respect an explicit
            # Limit (caller wants one bounded page).
            if "Limit" in kwargs:
                return table.query(KeyConditionExpression=condition,
                                   **kwargs).get("Items", [])
            items: list[dict] = []
            resp = table.query(KeyConditionExpression=condition, **kwargs)
            items.extend(resp.get("Items", []))
            while "LastEvaluatedKey" in resp:
                resp = table.query(KeyConditionExpression=condition,
                                   ExclusiveStartKey=resp["LastEvaluatedKey"],
                                   **kwargs)
                items.extend(resp.get("Items", []))
            return items
        except Exception as e:
            logger.error("query failed: %s", e)
            return []

    def query_bounded(self, key_condition: dict, *, max_pages: int, page_size: int,
                      **kwargs) -> tuple[list[dict], bool]:
        """A STRICT, bounded Query: at most ``max_pages`` pages of at most
        ``page_size`` items. Returns ``(items, cut)``; ``cut`` means the
        partition held more pages than were read.

        Unlike ``query`` (the lenient path other callers keep: it logs and
        returns ``[]``), a failure raises ``StorageReadError`` with the cause
        chained, so a caller can tell a failed read from an empty one. An
        unconfigured table raises too (after the usual one-time warning)."""
        if not self.table_name:
            _warn_unconfigured(self.kind, "query_bounded")
            raise StorageReadError(f"{self.kind} has no table configured")
        pk, pk_val = next(iter(key_condition.items()))
        condition = Key(pk).eq(pk_val)
        if "sk_between" in kwargs:
            sk, (lo, hi) = kwargs.pop("sk_between")
            condition &= Key(sk).between(lo, hi)
        items: list[dict] = []
        start_key = None
        try:
            table = self._get_table()
            for _ in range(max_pages):
                call = {**kwargs, "KeyConditionExpression": condition, "Limit": page_size}
                if start_key:
                    call["ExclusiveStartKey"] = start_key
                resp = table.query(**call)
                items.extend(resp.get("Items", []))
                start_key = resp.get("LastEvaluatedKey")
                if not start_key:
                    return items, False
        except Exception as exc:
            raise StorageReadError(f"{self.kind} query failed") from exc
        return items, True

    def scan(self, filter_expression: str | None = None, **kwargs) -> list[dict]:
        if not self.table_name:
            _warn_unconfigured(self.kind, "scan")
            return []
        try:
            if filter_expression:
                kwargs["FilterExpression"] = filter_expression
            table = self._get_table()
            # Paginate: a single DynamoDB scan returns at most ~1MB, so large
            # tables (e.g. the prompt registry with long prompt bodies) come back
            # partial otherwise — which silently truncated both the prompt listing
            # and the registry re-seed's delete pass (leaving stale/duplicate rows
            # the next seed could not purge). Follow LastEvaluatedKey to the end.
            # Respect an explicit Limit (caller wants a single bounded page).
            if "Limit" in kwargs:
                return table.scan(**kwargs).get("Items", [])
            items: list[dict] = []
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            while "LastEvaluatedKey" in resp:
                resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
                items.extend(resp.get("Items", []))
            return items
        except Exception as e:
            logger.error("scan failed: %s", e)
            return []

    def get_item(self, key: dict) -> dict | None:
        if not self.table_name:
            _warn_unconfigured(self.kind, "get_item")
            return None
        try:
            resp = self._get_table().get_item(Key=key)
            return resp.get("Item")
        except Exception as e:
            logger.error("get_item failed: %s", e)
            return None

    def delete_item(self, key: dict) -> bool:
        if not self.table_name:
            # Same contract decision as put_item: True (nothing to delete is
            # not a failure) plus a visible warning.
            _warn_unconfigured(self.kind, "delete_item")
            return True
        try:
            self._get_table().delete_item(Key=key)
            return True
        except Exception as e:
            logger.error("delete_item failed: %s", e)
            return False


# ---------------------------------------------------------------------------
# adt-feedback table contract (stacks/adt_database_stack.py owns the resource;
# the lockstep test tests/unit/test_feedback_durability.py pins these literals
# against the stack source).
#
#   table          adt-feedback
#   key            feedback_id (HASH) + timestamp (RANGE)
#   index          FEEDBACK_DATE_INDEX: feedback_date (HASH) + timestamp (RANGE)
#   TTL attribute  ttl (epoch seconds) — the adt-onboarding-sessions convention
#
# ``feedback_id`` is derived from (session_id, message_id), so it is STABLE for
# a given answer: repeated thumbs on the same message land in the same
# partition and are kept as separate rows (distinct ``timestamp`` sort keys)
# rather than overwriting each other, and one lookup by id returns the whole
# history for that turn. It is a digest, not a concatenation, because both
# inputs are client-supplied strings (unbounded length, arbitrary bytes) —
# the ``failure_signals._digest`` precedent. The original ids stay on the item
# as plain attributes, so traceability is not lost.
FEEDBACK_TABLE_DEFAULT = "adt-feedback"
FEEDBACK_DATE_INDEX = "feedback-date-index"
FEEDBACK_TTL_ATTRIBUTE = "ttl"

#: Projection placeholder -> attribute for a bounded rating read: who, which
#: turn, the thumb and its comment, never the stored assistant response.
#: Placeholders, because ``timestamp`` is a DynamoDB reserved word; the
#: ``#fb_`` prefix cannot collide with boto3's generated ``#n<k>`` names.
_FEEDBACK_RATING_FIELDS = {
    "#fb_user": "user_id", "#fb_session": "session_id", "#fb_message": "message_id",
    "#fb_question": "user_message", "#fb_sentiment": "sentiment",
    "#fb_comment": "user_comment", "#fb_time": "timestamp",
}

#: Retention for a feedback row. Feedback is CUSTOMER-DERIVED free text: the
#: ``user_comment`` field is untrusted steward/user prose (ADR-0054) and can
#: carry incidental PII that the guardrail — which filters model RESPONSES, not
#: what is written to a store — never sees. Bounded retention is therefore part
#: of the record's design, not an afterthought. 90 days is the repo's existing
#: decay horizon for accumulated, customer-derived operational state
#: (``TOMBSTONE_TTL_DAYS`` / ``failure_signals.SIGNAL_TTL_DAYS``), and it
#: comfortably covers the admin surface's 7-day default window plus a quarterly
#: review, so the retention choice costs no functionality.
FEEDBACK_TTL_DAYS = 90

#: Upper bound on the per-day fan-out of a time-range query. Deliberately far
#: beyond FEEDBACK_TTL_DAYS, so clamping can never hide a row that still
#: exists — it only stops an absurd client range from issuing thousands of
#: queries.
_MAX_RANGE_DAYS = 400


# ---------------------------------------------------------------------------
# adt-usage table contract (stacks/adt_database_stack.py owns the resource;
# tests/unit/test_usage_records_land.py pins the literals in lockstep and
# tests/test_usage_table_wiring.py reads the synthesized template).
#
#   table          adt-usage
#   key            user_id (HASH) + timestamp (RANGE) — what UsageRecord writes
#   TTL attribute  ttl (epoch seconds), the adt-feedback convention
#
# ``get_by_time_range`` scans with a timestamp filter (the admin dashboard's
# 7-day totals), which is why the task role holds PutItem + Scan on this
# table only. A row carries the user's email, so retention is bounded like a
# feedback row's.
USAGE_TABLE_DEFAULT = "adt-usage"
USAGE_TTL_ATTRIBUTE = "ttl"
USAGE_TTL_DAYS = 90


def _usage_ttl_epoch(now: float | None = None) -> int:
    return int((now or time.time()) + USAGE_TTL_DAYS * 86400)


def feedback_id_for(session_id: "str | None", message_id: "str | None") -> str:
    """Stable ``feedback_id`` for one answered turn (see the table contract)."""
    raw = f"{session_id or ''}|{message_id or ''}"
    return f"fb-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _feedback_ttl_epoch(now: float | None = None) -> int:
    return int((now or time.time()) + FEEDBACK_TTL_DAYS * 86400)


def _utc_days_in_range(start: str, end: str) -> "list[str] | None":
    """UTC ``YYYY-MM-DD`` day buckets covering [start, end] inclusive.

    ``None`` when either bound is not date-parseable (the caller warns and
    returns no rows — the same empty result an unparseable bound produced
    before, but now visible). Empty list when the range is inverted.
    """
    def _day(value: str) -> "date | None":
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    lo, hi = _day(start), _day(end)
    if lo is None or hi is None:
        return None
    if hi < lo:
        return []
    if (hi - lo).days + 1 > _MAX_RANGE_DAYS:
        lo = hi - timedelta(days=_MAX_RANGE_DAYS - 1)
    return [(lo + timedelta(days=offset)).isoformat()
            for offset in range((hi - lo).days + 1)]


class FeedbackStorage:
    def __init__(self, table_name: str | None, region: str = "us-east-1"):
        self._db = DynamoDBStorage(table_name, region, kind="FeedbackStorage")

    @staticmethod
    def _with_key_contract(record: dict) -> dict:
        """Stamp the table's key/TTL attributes onto a caller's record.

        Lives HERE rather than in ``FeedbackRecord.to_dict`` so the durability
        contract belongs to the store that owns the table shape — any writer
        gets a well-keyed, expiring row without having to know the schema.
        Caller-supplied values always win (idempotent re-store keeps its id).
        ``timestamp`` is defaulted because it is the SORT key: DynamoDB rejects
        a missing/empty key attribute, and a raw dict from a future caller must
        not be able to turn a write into a ValidationException.
        """
        item = dict(record)
        stamped = str(item.get("timestamp") or "").strip()
        if not stamped:
            stamped = datetime.now(timezone.utc).isoformat()
            item["timestamp"] = stamped
        if not item.get("feedback_id"):
            item["feedback_id"] = feedback_id_for(
                item.get("session_id"), item.get("message_id"))
        if not item.get("feedback_date"):
            item["feedback_date"] = stamped[:10]
        item.setdefault(FEEDBACK_TTL_ATTRIBUTE, _feedback_ttl_epoch())
        return item

    def store(self, record: dict) -> bool:
        return self._db.put_item(self._with_key_contract(record))

    def get_by_time_range(self, start: str, end: str, sentiment: str | None = None) -> list[dict]:
        """Feedback rows in [start, end], newest last.

        Index-backed (FEEDBACK_DATE_INDEX) rather than a full-table scan: one
        Query per UTC day bucket with the ISO ``timestamp`` range as the sort-key
        condition, which also keeps the IAM grant down to PutItem + Query.
        Sentiment stays a filter (it is not part of any key).
        """
        days = _utc_days_in_range(start, end)
        if days is None:
            logger.warning(
                "feedback_range_unparseable: start/end are not ISO dates — "
                "returning no rows")
            return []
        kwargs: dict = {"IndexName": FEEDBACK_DATE_INDEX,
                        "sk_between": ("timestamp", (start, end))}
        if sentiment:
            kwargs["FilterExpression"] = Attr("sentiment").eq(sentiment)
        items: list[dict] = []
        for day in days:
            items.extend(self._db.query({"feedback_date": day}, **dict(kwargs)))
        items.sort(key=lambda i: str(i.get("timestamp", "")))
        return items

    def get_by_time_range_bounded(self, start: str, end: str, *, max_pages_per_day: int,
                                  page_size: int) -> tuple[list[dict], list[str]]:
        """Feedback rows in [start, end] read STRICTLY and within a bound:
        per UTC day, at most ``max_pages_per_day`` Query pages of at most
        ``page_size`` rows through the date index, NEWEST FIRST (so the rows
        a bound leaves unread are each day's oldest), projected to the fields
        a rating needs (never the stored ``assistant_response``). Returns
        ``(rows, cut_days)``, rows newest first; ``cut_days`` lists the UTC
        days (``YYYY-MM-DD``) that held more than was read.
        Raises ``StorageReadError`` on any failure (``get_by_time_range``
        above stays the lenient path)."""
        days = _utc_days_in_range(start, end)
        if days is None:
            raise StorageReadError("feedback range is not ISO dates")
        rows: list[dict] = []
        cut_days: list[str] = []
        for day in days:
            page, day_cut = self._db.query_bounded(
                {"feedback_date": day}, max_pages=max_pages_per_day, page_size=page_size,
                IndexName=FEEDBACK_DATE_INDEX, sk_between=("timestamp", (start, end)),
                ScanIndexForward=False,
                ProjectionExpression=", ".join(_FEEDBACK_RATING_FIELDS),
                ExpressionAttributeNames=dict(_FEEDBACK_RATING_FIELDS.items()))
            rows.extend(page)
            if day_cut:
                cut_days.append(day)
        rows.sort(key=lambda i: str(i.get("timestamp", "")), reverse=True)
        return rows, cut_days


class UsageStorage:
    """Usage rows for the admin dashboard — see the adt-usage contract above."""

    def __init__(self, table_name: str | None, region: str = "us-east-1"):
        self._db = DynamoDBStorage(table_name, region, kind="UsageStorage")

    def store(self, record: dict) -> bool:
        item = dict(record)
        item.setdefault(USAGE_TTL_ATTRIBUTE, _usage_ttl_epoch())
        return self._db.put_item(item)

    def get_by_time_range(self, start: str, end: str) -> list[dict]:
        return self._db.scan(FilterExpression=Attr("timestamp").between(start, end))


class GuardrailStorage:
    def __init__(self, table_name: str | None, region: str = "us-east-1"):
        self._db = DynamoDBStorage(table_name, region, kind="GuardrailStorage")

    def store(self, record: dict) -> bool:
        return self._db.put_item(record)

    def get_by_time_range(self, start: str, end: str) -> list[dict]:
        return self._db.scan(FilterExpression=Attr("timestamp").between(start, end))


class PromptTemplateStorage:
    def __init__(self, table_name: str | None, region: str = "us-east-1"):
        self._db = DynamoDBStorage(table_name, region, kind="PromptTemplateStorage")

    def save(self, template: dict) -> bool:
        return self._db.put_item(template)

    def get(self, template_id: str) -> dict | None:
        return self._db.get_item({"template_id": template_id})

    def delete(self, template_id: str) -> bool:
        return self._db.delete_item({"template_id": template_id})

    def list_all(self, source: str | None = None) -> list[dict]:
        if source:
            return self._db.scan(FilterExpression=Attr("source").eq(source))
        return self._db.scan()

    def update(self, template_id: str, updates: dict) -> bool:
        item = self._db.get_item({"template_id": template_id})
        if item is None:
            return False
        item.update(updates)
        return self._db.put_item(item)


class DashboardCardStorage:
    def __init__(self, table_name: str | None, region: str = "us-east-1"):
        self._db = DynamoDBStorage(table_name, region, kind="DashboardCardStorage")

    def save(self, card: dict) -> bool:
        return self._db.put_item(card)

    def get(self, card_id: str) -> dict | None:
        return self._db.get_item({"card_id": card_id})

    def delete(self, card_id: str) -> bool:
        return self._db.delete_item({"card_id": card_id})

    def list_all(self) -> list[dict]:
        return self._db.scan()

    def get_by_prompt(self, prompt_id: str) -> list[dict]:
        return self._db.scan(FilterExpression=Attr("prompt_id").eq(prompt_id))


# Truthy string values accepted when reading a persisted boolean flag from SSM.
_TRUTHY = {"1", "true", "yes", "on"}


class SsmSettingsStore:
    """Persist a boolean flag in SSM Parameter Store.

    Duck-typed to the demo seam persister (``get_flag()`` / ``set_flag(value)``).
    The seam stays pure: it depends only on this small wrapper's interface, not on
    boto3. Reads are boot-resilient (never crash app boot over a settings read);
    writes fail loud (the admin PUT must surface a failed persist).

    Optionally carries a companion INTEGER setting in a second SSM parameter
    (``interval_param_name`` — ``get_interval()`` / ``set_interval(value)``),
    used by the live-updates seam for the snapshot interval. Same contract:
    reads fail-soft to ``None``, writes fail loud.
    """

    def __init__(self, param_name: str, region: str | None = None,
                 interval_param_name: str | None = None):
        self.param_name = param_name
        self.interval_param_name = interval_param_name
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client("ssm", region_name=self.region)
        return self._client

    def _read_parameter(self, name: str, op: str) -> Optional[str]:
        """Raw parameter value, or None if not set / unreadable (boot-resilient)."""
        try:
            resp = self._get_client().get_parameter(Name=name)
            return resp["Parameter"]["Value"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ParameterNotFound":
                return None
            logger.warning("SsmSettingsStore.%s(%s) failed: %s", op, name, e)
            return None
        except Exception as e:  # boot resilience — do not crash on a settings read
            logger.warning("SsmSettingsStore.%s(%s) failed: %s", op, name, e)
            return None

    def get_flag(self) -> Optional[bool]:
        """Return the persisted flag, or None if not set / unreadable.

        None is NORMAL (parameter not set). On any other read error we log and
        return None so a settings read can never crash app boot.
        """
        value = self._read_parameter(self.param_name, "get_flag")
        if value is None:
            return None
        return value.strip().lower() in _TRUTHY

    def set_flag(self, value: bool) -> None:
        """Persist the flag. Exceptions PROPAGATE — the admin write path is fail-loud."""
        self._get_client().put_parameter(
            Name=self.param_name,
            Value="true" if value else "false",
            Type="String",
            Overwrite=True,
        )

    def get_interval(self) -> Optional[int]:
        """Persisted interval (seconds), or None when unset/unreadable/non-numeric.

        None is fail-soft — the caller's env/default applies. Bounds are the
        SEAM's job (``live_settings.coerce_interval``); this store only parses.
        """
        if not self.interval_param_name:
            return None
        value = self._read_parameter(self.interval_param_name, "get_interval")
        if value is None:
            return None
        try:
            return int(value.strip())
        except ValueError:
            logger.warning("SsmSettingsStore.get_interval(%s): non-numeric value ignored",
                           self.interval_param_name)
            return None

    def set_interval(self, value: int) -> None:
        """Persist the interval. Exceptions PROPAGATE (fail-loud admin write path)."""
        if not self.interval_param_name:
            raise ValueError("no interval_param_name configured on this store")
        self._get_client().put_parameter(
            Name=self.interval_param_name,
            Value=str(int(value)),
            Type="String",
            Overwrite=True,
        )