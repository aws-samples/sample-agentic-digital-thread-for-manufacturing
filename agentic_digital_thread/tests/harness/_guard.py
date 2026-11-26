# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Importable safety guards for the S0.2 cloud integration harness.

Every destructive harness operation (graph reset, reseed, teardown, redeploy)
MUST call these guards *first* so it can only ever act against the known dev
account and a real target graph, and only when the operator has explicitly
opted in. The guards are pure and read-only: they perform an STS identity read
and inspect environment variables, but never mutate any cloud or local state.

Constants are overridable via the environment so the same code can guard a
different sandbox account without edits:

    ADT_HARNESS_DEV_ACCOUNT   overrides the required account id (default 111122223333)
    ADT_HARNESS_DEV_REGION    overrides the required region    (default us-east-1)

Opt-in for destructive ops is granted by either an explicit ``--yes`` flag
(passed through as ``require_opt_in(yes=True)``) or ``ADT_HARNESS_ALLOW_DESTRUCTIVE=1``.
"""

from __future__ import annotations

import os

import boto3

# Default dev sandbox identity. Overridable via env so the guards can protect a
# different account/region without code changes (the defaults are the real dev
# account this harness was authored against).
DEV_ACCOUNT = os.environ.get("ADT_HARNESS_DEV_ACCOUNT", "111122223333")
DEV_REGION = os.environ.get("ADT_HARNESS_DEV_REGION", "us-east-1")

# The literal placeholder that ``agents.db`` falls back to when GRAPH_IDENTIFIER
# is unset — guarding against it stops a destructive op from running against a
# non-existent / wrong graph.
GRAPH_PLACEHOLDER = "adt"


class GuardError(Exception):
    """Raised when a safety precondition for a destructive operation is not met."""


def assert_dev_account(profile: str | None = None) -> str:
    """Confirm ambient AWS creds resolve to the dev account *and* region.

    Uses STS ``get_caller_identity`` via a session built from ``profile`` (or
    the ambient SSO/env/profile creds when ``profile`` is None). Raises
    :class:`GuardError` unless the resolved account equals ``DEV_ACCOUNT`` and
    the resolved region equals ``DEV_REGION``. Returns the account id on success.
    Read-only: STS identity reads never mutate state.
    """
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    region = session.region_name or os.environ.get("AWS_REGION") or DEV_REGION
    if region != DEV_REGION:
        raise GuardError(
            f"Refusing to proceed: resolved region {region!r} != dev region "
            f"{DEV_REGION!r}. Set --region {DEV_REGION} or ADT_HARNESS_DEV_REGION."
        )
    account = session.client("sts", region_name=region).get_caller_identity()["Account"]
    if account != DEV_ACCOUNT:
        raise GuardError(
            f"Refusing to proceed: caller account {account!r} != dev account "
            f"{DEV_ACCOUNT!r}. Check your profile/SSO or ADT_HARNESS_DEV_ACCOUNT."
        )
    return account


def assert_target_graph(expected_graph_id: str | None = None) -> str:
    """Confirm GRAPH_IDENTIFIER names a real target graph before a destructive op.

    Raises :class:`GuardError` if ``GRAPH_IDENTIFIER`` is unset/empty, if it is
    still the literal placeholder ``"adt"`` (i.e. nothing real was
    captured), or if ``expected_graph_id`` is given and does not match. Returns
    the resolved graph id on success. Read-only.
    """
    graph_id = os.environ.get("GRAPH_IDENTIFIER", "").strip()
    if not graph_id:
        raise GuardError(
            "Refusing to proceed: GRAPH_IDENTIFIER is unset. "
            "Run `python -m tests.harness.capture_env` and source .harness-env first."
        )
    if graph_id == GRAPH_PLACEHOLDER:
        raise GuardError(
            f"Refusing to proceed: GRAPH_IDENTIFIER is the placeholder "
            f"{GRAPH_PLACEHOLDER!r}, not a real graph id. Capture env from a deployment first."
        )
    if expected_graph_id is not None and graph_id != expected_graph_id:
        raise GuardError(
            f"Refusing to proceed: GRAPH_IDENTIFIER {graph_id!r} != expected "
            f"{expected_graph_id!r}."
        )
    return graph_id


def require_opt_in(yes: bool = False) -> None:
    """Gate a destructive op behind an explicit opt-in.

    Proceeds only when ``yes`` is True (e.g. an explicit ``--yes`` flag) or the
    environment sets ``ADT_HARNESS_ALLOW_DESTRUCTIVE=1``. Otherwise raises
    :class:`GuardError` with a clear message. Read-only.
    """
    if yes or os.environ.get("ADT_HARNESS_ALLOW_DESTRUCTIVE") == "1":
        return
    raise GuardError(
        "Refusing destructive operation without opt-in. "
        "Pass --yes or set ADT_HARNESS_ALLOW_DESTRUCTIVE=1 to proceed."
    )
