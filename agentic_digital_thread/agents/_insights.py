# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared CloudWatch Logs Insights runner for the admin agents routes.

Lifted from the ``agents_routes._run_insights`` pattern so route modules can
share ONE start-query/poll implementation instead of duplicating it.
``agents_trace_routes`` imports it, and so does ``agents_routes._get_metrics``,
which must tell a failed session count from a successful 0. The eval-score
reads in ``agents_routes`` still use its local ``_run_insights``, which
returns ``[]`` on failure.

Contract difference from the legacy copy: failures RAISE :class:`InsightsError`
instead of returning ``[]``. Callers that need "empty on failure" catch it;
callers that need HONEST errors (the trace drill-down routes — fabricating an
empty timeline would be indistinguishable from a quiet session) surface the
message. Never returns fabricated data. :func:`existing_log_groups` (the
DescribeLogGroups check of which candidate groups exist) follows the same
contract: a failed or incomplete check raises, it never reads as "no groups".

Fully offline-unit-testable: the boto3 logs client, poll count, and sleep are
all injectable.
"""
from __future__ import annotations

import time
from collections.abc import Sequence

#: Poll cadence mirrors agents_routes._run_insights (50 × 0.5s ≈ 25s cap).
DEFAULT_MAX_POLLS = 50
DEFAULT_POLL_INTERVAL_S = 0.5

#: StartQuery accepts at most 50 log groups in ``logGroupNames`` (API
#: reference); :func:`run_insights` refuses a longer list before any call.
MAX_LOG_GROUPS_PER_QUERY = 50

#: DescribeLogGroups accepts at most 50 names in ``logGroupIdentifiers`` (API
#: reference); :func:`existing_log_groups` refuses a longer list before any call.
MAX_LOG_GROUP_IDENTIFIERS = 50

_TERMINAL_FAILURE_STATUSES = ("Failed", "Cancelled", "Timeout", "Unknown")


class InsightsError(RuntimeError):
    """A Logs Insights query could not produce results (start/poll/terminal failure)."""


def existing_log_groups(logs_client, names: Sequence[str]) -> list[str]:
    """Which of ``names`` exist, in ``names`` order, from ONE DescribeLogGroups.

    Asks by name (``logGroupIdentifiers``, 1 to
    :data:`MAX_LOG_GROUP_IDENTIFIERS` names): a name that does not exist is
    left out of the answer, not an error (measured live, 2026-10-22). At most
    that many groups come back, so the answer is one page; a ``nextToken``
    anyway is an error, never a partial answer. Raises :class:`InsightsError`
    when the call fails — never an empty list, which a caller would read as
    "no groups".
    """
    wanted = list(names)
    if not 1 <= len(wanted) <= MAX_LOG_GROUP_IDENTIFIERS:
        raise InsightsError(
            f"DescribeLogGroups checks 1 to {MAX_LOG_GROUP_IDENTIFIERS} "
            f"log groups by name, got {len(wanted)}")
    try:
        resp = logs_client.describe_log_groups(logGroupIdentifiers=wanted)
    except Exception as e:
        raise InsightsError(
            f"Could not check which of {len(wanted)} log groups exist: {e}"
        ) from e
    if resp.get("nextToken"):
        raise InsightsError(
            f"DescribeLogGroups returned a nextToken for {len(wanted)} names; "
            "the existence check is not complete")
    found = {group.get("logGroupName") for group in resp.get("logGroups", [])}
    return [name for name in wanted if name in found]


def _group_kwargs(log_group: str | Sequence[str]) -> dict:
    """StartQuery's log-group argument: one name, or a 1..50 name list."""
    if isinstance(log_group, str):
        return {"logGroupName": log_group}
    names = list(log_group)
    if not 1 <= len(names) <= MAX_LOG_GROUPS_PER_QUERY:
        raise InsightsError(
            f"A Logs Insights query takes 1 to {MAX_LOG_GROUPS_PER_QUERY} "
            f"log groups, got {len(names)}"
        )
    return {"logGroupNames": names}


def _start_query(logs_client, log_group: str | Sequence[str], query: str,
                 hours: float, limit: int) -> str:
    """Start the Insights query; return the query id (raises InsightsError)."""
    groups = _group_kwargs(log_group)
    end = int(time.time())
    start = end - int(hours * 3600)
    try:
        r = logs_client.start_query(
            **groups,
            startTime=start,
            endTime=end,
            queryString=query,
            limit=limit,
        )
        return r["queryId"]
    except Exception as e:
        raise InsightsError(
            f"Logs Insights query failed to start for {_label(log_group)}: {e}"
        ) from e


def _label(log_group: str | Sequence[str]) -> str:
    """Short log-group description for error messages."""
    if isinstance(log_group, str):
        return log_group
    names = list(log_group)
    return names[0] if len(names) == 1 else f"{len(names)} log groups"


def _poll_query(logs_client, log_group: str, qid: str, max_polls: int,
                poll_interval_s: float, sleep) -> list[list[dict]]:
    """Poll until Complete; raise InsightsError on failure or exhaustion."""
    for _ in range(max_polls):
        sleep(poll_interval_s)
        try:
            s = logs_client.get_query_results(queryId=qid)
        except Exception as e:
            raise InsightsError(
                f"Logs Insights poll failed for {log_group}: {e}"
            ) from e
        status = s.get("status")
        if status == "Complete":
            return s.get("results", [])
        if status in _TERMINAL_FAILURE_STATUSES:
            raise InsightsError(
                f"Logs Insights query ended {status} for {log_group}"
            )
    raise InsightsError(
        f"Logs Insights query did not complete within "
        f"{max_polls * poll_interval_s:.0f}s for {log_group}"
    )


def run_insights(
    logs_client,
    log_group: str | Sequence[str],
    query: str,
    hours: float,
    *,
    limit: int = 1000,
    max_polls: int | None = None,
    poll_interval_s: float | None = None,
    sleep=time.sleep,
) -> list[list[dict]]:
    """Run a Logs Insights query and return its result rows.

    ``log_group`` is one log group name (sent as ``logGroupName``) or a list
    of 1..:data:`MAX_LOG_GROUPS_PER_QUERY` names (sent as ``logGroupNames``,
    queried together). A list outside that range raises before any call.

    Raises :class:`InsightsError` on start failure, poll failure, a terminal
    non-Complete status, or poll exhaustion — never fabricates an empty
    result set for a failed query.

    ``max_polls`` / ``poll_interval_s`` default to the module constants at
    CALL time (monkeypatch-friendly for offline tests).
    """
    if max_polls is None:
        max_polls = DEFAULT_MAX_POLLS
    if poll_interval_s is None:
        poll_interval_s = DEFAULT_POLL_INTERVAL_S
    qid = _start_query(logs_client, log_group, query, hours, limit)
    return _poll_query(logs_client, _label(log_group), qid, max_polls,
                       poll_interval_s, sleep)


def parse_row(row: list[dict]) -> dict:
    """Flatten one Insights result row ([{field, value}, ...]) into a dict."""
    return {f.get("field"): f.get("value") for f in row}
