# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Turn a configured model id into what ``strands.Agent(model=...)`` needs.

ONE seam for both roles (the orchestrator and every system agent) so a model
family is enabled in exactly one place.

Two serving surfaces exist (ADR-0055):

* **Converse on ``bedrock-runtime``** — Anthropic, Amazon Nova, and the
  open-weight ``openai.gpt-oss*`` models. strands builds a ``BedrockModel``
  implicitly from the id STRING, so this path returns the id UNCHANGED and
  today's behavior is byte-identical.
* **Bedrock Mantle** — the OpenAI-compatible surface that is the ONLY way to
  reach the GPT-5.x family. Those models are served at ``/openai/v1`` (the
  ``/openai/`` prefix is load-bearing: the generic ``/v1`` path answers
  ``"The model 'openai.gpt-5.6-luna' does not support the '/v1/responses'
  API"``, which reads like an entitlement error but is purely a wrong path).
  strands' own ``bedrock_mantle_config`` helper builds the GENERIC ``/v1``
  base url, so it cannot reach GPT-5.x — we pass an explicit ``base_url``
  plus a freshly minted bearer token instead.

Token lifetime is a NON-issue because ADT constructs its agents PER TURN, so
each turn mints a fresh token. A cached model object would outlive its token —
do not add one without a refresh.

Failure posture: a Mantle model that cannot be constructed RAISES. Silently
falling back to a different model would corrupt every A/B measurement and
misreport which model produced an answer.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# GPT-5.x is Mantle-only AND needs the /openai/v1 path. gpt-oss is deliberately
# NOT here: it answers on bedrock-runtime Converse, so it rides the plain path
# and inherits retries/streaming/metrics unchanged.
_MANTLE_ONLY_PREFIXES = ("openai.gpt-5",)
_MANTLE_BASE_URL = "https://bedrock-mantle.{region}.api.aws/openai/v1"


def strip_region_prefix(model_id: str) -> str:
    """Drop an inference-profile prefix (``global.``, ``us.``, ``eu.`` ...).

    Mantle model ids are NOT region-qualified; a stray prefix (``us.openai.
    gpt-5.6-luna``) is rejected by the service, and callers may hand us an id
    that went through ``model_catalog.resolve_id``.
    """
    from .model_catalog import PROFILE_PREFIXES
    for prefix in PROFILE_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def is_mantle_only(model_id: str) -> bool:
    """True when the id can ONLY be served by Bedrock Mantle."""
    bare = strip_region_prefix(str(model_id or ""))
    return bare.startswith(_MANTLE_ONLY_PREFIXES)


def _mantle_model(bare_id: str, region: str, max_tokens: int | None = None):
    """Build a strands ``OpenAIResponsesModel`` bound to Mantle. RAISES on any
    missing dependency or credential problem — never returns a substitute.

    ``max_tokens`` maps to the provider's ``max_output_tokens`` param. Without it
    a Mantle model runs UNCAPPED and is exposed to the same failure that took the
    orchestrator down: exceeding the ceiling mid tool-use makes strands raise an
    unrecoverable state that surfaces only as a generic "Query failed."
    """
    try:
        from strands.models.openai_responses import OpenAIResponsesModel
        from aws_bedrock_token_generator import provide_token
    except ImportError as exc:  # missing `openai` extra → loud, actionable
        raise RuntimeError(
            f"model '{bare_id}' needs the Bedrock Mantle dependencies "
            f"(openai>=2.45, aws-bedrock-token-generator): {exc}"
        ) from exc
    kwargs = {
        "model_id": bare_id,
        "client_args": {"base_url": _MANTLE_BASE_URL.format(region=region),
                        "api_key": provide_token(region=region)},
    }
    if max_tokens:
        kwargs["params"] = {"max_output_tokens": max_tokens}
    return OpenAIResponsesModel(**kwargs)


def _effective_region(region: str | None = None) -> str:
    """The region to bind a model to, never None — a None region sends botocore
    to the IMDS provider (a DNS call), which must never happen at construction.
    """
    return region or os.environ.get("AWS_REGION") or "us-east-1"


def resolve_model(model_id: str, region: str | None = None,
                  max_tokens: int | None = None):
    """The value to hand ``strands.Agent(model=...)``.

    Returns the id STRING for Converse-served models (unchanged behavior) or an
    ``OpenAIResponsesModel`` for Mantle-only ids.
    """
    if not is_mantle_only(model_id):
        return model_id
    region = _effective_region(region)
    bare = strip_region_prefix(model_id)
    logger.info("model_provider: %s served via Bedrock Mantle (region=%s)",
                bare, region)
    return _mantle_model(bare, region, max_tokens)

# Explicit output cap for the ORCHESTRATOR. It had none: a bare model-id string
# inherits the Bedrock default, and a model verbose enough to exceed it mid
# event-loop makes strands raise MaxTokensReachedException — an UNRECOVERABLE
# state that surfaced only as a generic "Query failed." (root-caused 2026-07-30:
# Sonnet 5 failed 6 of 8 interleaved runs this way while Sonnet 4.6 passed 8/8).
# The orchestrator writes the long final answers, so it needs at least the cap
# the system agents already carried. Raised 8192 -> 16384 on 2026-10-01: on the
# live rehearsal Sonnet 5 hit 8192 on g-critical-work-orders (a long multi-plant
# table) and the turn died the same way. Both orchestrator models allow far
# more output; the cap only costs tokens when a model actually uses them.
#
# Kept at 16384 on 2026-10-13 (one cut in ~56 Sonnet 5 turns on a live
# account, while writing tool calls after three long system results). Facts
# from the Amazon Bedrock docs: Sonnet 5's maximum output is 128K tokens, and
# it runs ADAPTIVE THINKING BY DEFAULT even when the request omits
# ``thinking`` (this app sets none), at the default ``high`` effort, so this
# cap covers thinking AND reply text together. A cut is now recovered once in
# the same conversation (agents/output_cap_recovery.py) and a second cut is an
# honest "too long" error. A bigger cap would not have been free: it only
# delays the cut on a runaway reply (that turn already took 394 s before it
# failed), and no measured output-token distribution says what a better
# number is; ``intelligence_summary.cost.output_tokens`` records it per turn.
ORCHESTRATOR_MAX_TOKENS = 16384


def build_model(model_id: str, *, max_tokens: int, region: str | None = None):
    """Resolve *model_id* and return a READY model object.

    The one seam both roles use, so the token cap can never be forgotten on one
    of them. Pass the ID, never a constructed model: ``resolve_model``
    classifies id STRINGS, so handing it an object is a silent no-op (that bug
    sent a Mantle id to ConverseStream and voided two experiments).
    """
    resolved = resolve_model(model_id, region, max_tokens)
    if not isinstance(resolved, str):
        return resolved              # Mantle provider, capped at construction
    from strands.models.bedrock import BedrockModel
    # Region passed EXPLICITLY. With region_name=None botocore falls through to
    # the IMDS region provider, which is a DNS call: constructing the model then
    # needs the network, and the unit lane failed in CI (no ambient AWS config)
    # while passing on a developer machine that has a region set. Model
    # construction must stay offline — same default as resolve_model.
    # Converse streams can go quiet after their headers until the 120 s read
    # timeout; the guard retries that once while nothing has been streamed
    # (see model_stream_retry). Mantle models use a different transport and
    # are returned above, unguarded.
    from agents.model_stream_retry import guard_stream_stalls
    return guard_stream_stalls(BedrockModel(model_id=resolved, max_tokens=max_tokens,
                                            region_name=_effective_region(region)))
