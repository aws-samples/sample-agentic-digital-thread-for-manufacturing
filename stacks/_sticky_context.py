# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sticky CDK context resolution backed by SSM Parameter Store.

Some deploy-time toggles (``system_query_mode``, ``crossAccountDataRoleArn``)
are plain CDK context values: leaving the flag off a later ``cdk deploy`` /
``deploy.sh`` run silently reverts the environment (query mode falls back to
the default, the cross-account ``sts:AssumeRole`` grant is dropped). The repo's
existing convention for this problem is the HighByte sticky flag: deploy.sh
persists the value in SSM and re-applies it on later deploys (see
``/adt/highbyte-enabled`` in ``agentic_digital_thread/deploy.sh`` and
``_highbyte_enabled_in_ssm`` in ``app.py``).

This module generalizes that pattern for string-valued context keys.
:func:`resolve_sticky` returns the first non-empty of:

1. the explicit context value (an explicit ``-c key=value`` always wins),
2. the SSM parameter value (the sticky value persisted by deploy.sh),
3. the caller-supplied default.

The SSM read is **fail-soft** by default: any failure whatsoever (no
credentials, no region, parameter absent, offline synth, missing boto3) yields
the default — CI, unit tests, and offline ``cdk synth`` must never break on it.
During a real deploy, deploy.sh sets ``ADT_STICKY_STRICT=1``: then only a
parameter that does not exist yields the default, and any other failure raises
:class:`StickyReadError`, so a deploy that cannot see its settings stops
instead of silently dropping them. The module is import-pure: boto3 is
imported lazily, only when an SSM read is attempted.

Persistence is deliberately NOT done here — ``agentic_digital_thread/deploy.sh``
owns writing the SSM parameters (mirroring the HighByte flag), so stacks and
app.py only ever read.
"""
from __future__ import annotations

import os
from typing import Optional


def resolve_sticky(
    context_value: Optional[str],
    ssm_name: str,
    *,
    region: Optional[str],
    profile_env: bool = False,
    default: str = "",
    ssm_client=None,
) -> str:
    """Resolve a sticky context value: explicit > SSM > default.

    :param context_value: the explicit CDK context value (``try_get_context``
        result). Any non-empty, non-whitespace value wins outright — no SSM
        call is made.
    :param ssm_name: the SSM parameter name holding the sticky value.
    :param region: region for the SSM read. When falsy and no ``ssm_client``
        is injected, the SSM read is skipped entirely (offline synth).
    :param profile_env: honor the ``AWS_PROFILE`` env var explicitly when
        building the boto3 client (boto3's default session already does; this
        forces a fresh session for callers that mutate the env mid-process).
    :param default: returned when both the context value and SSM are empty.
    :param ssm_client: optional pre-built SSM client (dependency injection
        for unit tests; also bypasses the region gate).
    """
    if context_value is not None and str(context_value).strip():
        return str(context_value).strip()

    sticky = _read_ssm_fail_soft(
        ssm_name, region=region, profile_env=profile_env, ssm_client=ssm_client
    )
    if sticky:
        return sticky

    return default


def resolve_sticky_bool(
    context_value: Optional[str],
    ssm_name: str,
    *,
    region: Optional[str],
    profile_env: bool = False,
    default: bool = False,
    ssm_client=None,
) -> bool:
    """Resolve a sticky BOOLEAN flag: explicit > SSM > default.

    Thin wrapper over :func:`resolve_sticky` for opt-in enable flags persisted
    as the string ``"true"`` (the HighByte/observability convention). Only a
    case-insensitive ``"true"`` enables; any other resolved value — including
    ``"false"``, junk, or an empty/failed read — yields ``default``. The same
    fail-soft contract applies: offline synth, missing credentials, or an
    absent parameter must never raise.
    """
    resolved = resolve_sticky(
        context_value,
        ssm_name,
        region=region,
        profile_env=profile_env,
        default="",
        ssm_client=ssm_client,
    )
    if not resolved:
        return default
    return resolved.strip().lower() == "true"


class StickyReadError(RuntimeError):
    """A sticky SSM read failed during a strict (deploy) run."""


#: Set by deploy.sh for its CDK runs. A deploy that cannot read its sticky
#: settings must stop, not synthesize a stack with every setting blanked.
STRICT_ENV = "ADT_STICKY_STRICT"


def _strict() -> bool:
    return os.environ.get(STRICT_ENV, "").strip().lower() not in ("", "0", "false")


def _is_parameter_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return response.get("Error", {}).get("Code") == "ParameterNotFound"


def _read_ssm_fail_soft(
    ssm_name: str,
    *,
    region: Optional[str],
    profile_env: bool,
    ssm_client,
) -> str:
    """SSM parameter read; returns "" when the parameter does not exist.

    Lenient (default): returns "" on ANY failure, so CI, unit tests and
    offline synth never break. Strict (``ADT_STICKY_STRICT`` set, as
    deploy.sh does): any failure other than a missing parameter raises
    :class:`StickyReadError` naming the parameter.
    """
    try:
        client = ssm_client
        if client is None:
            if not region:
                if _strict():
                    raise StickyReadError(
                        f"cannot read sticky setting {ssm_name}: no AWS region "
                        f"({STRICT_ENV} is set, so the deploy stops here)")
                return ""
            import boto3  # lazy: keep this module import-pure for unit/synth

            profile = os.environ.get("AWS_PROFILE") if profile_env else None
            if profile:
                session = boto3.Session(profile_name=profile)
                client = session.client("ssm", region_name=region)
            else:
                client = boto3.client("ssm", region_name=region)
        value = client.get_parameter(Name=ssm_name)["Parameter"]["Value"]
        return str(value).strip()
    except StickyReadError:
        raise
    except Exception as exc:
        if _is_parameter_not_found(exc) or not _strict():
            return ""
        raise StickyReadError(
            f"cannot read sticky setting {ssm_name}: {type(exc).__name__}: {exc}. "
            f"{STRICT_ENV} is set, so the deploy stops instead of treating the "
            f"setting as unset (which would drop it from the stacks).") from exc
