# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Curated catalog of selectable LLM models — the single source of truth.

The model list, labels and pricing live ONLY here — the admin dropdown, the
fleet pricing (agents_routes / fleet), the frontend pricing reference, and the
configured-model defaults all read this module: **add a new model = one entry
in ``MODELS``.**

Models are stored region-agnostically (the family id *without* the inference-
profile prefix). ``resolve_id`` returns the ``global.`` profile for every model
that has one, and the Region's geo prefix (``us.`` / ``eu.`` / ``apac.``) for
the rest, so the IDs callers see are still full inference-profile IDs.

Pricing note: the ``input_per_1k`` / ``output_per_1k`` figures below are the
standard Amazon Bedrock on-demand list prices (USD, never introductory rates)
from the AWS Price List API, us-east-1, read **2026-10-22**: the global
cross-Region rate for a model with a global profile (a geo profile of it bills
``REGIONAL_PREMIUM`` more, applied by :func:`pricing`), the Region's rate for
the rest. They drive cost *display* only: they are estimates, they are
subject to change, and they are not a quote. Consult the Amazon Bedrock pricing
page (<https://aws.amazon.com/bedrock/pricing/>) for current rates.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Roles
ORCHESTRATOR = "orchestrator"
AGENT = "agent"
#: The AgentCore Memory extraction model (the third Admin LLM settings select).
#: Not a ``Model.role``: the memory models are listed by family below.
MEMORY = "memory"


@dataclass(frozen=True)
class Model:
    family: str           # region-agnostic, e.g. "anthropic.claude-sonnet-5"
    label: str
    role: str             # ORCHESTRATOR | AGENT
    input_per_1k: float   # USD per 1K input tokens
    output_per_1k: float  # USD per 1K output tokens
    default: bool = False
    # Bedrock MANTLE-only (ADR-0055): served on the OpenAI-compatible endpoint,
    # NOT bedrock-runtime Converse. The UI marks these so a steward knows the
    # serving surface differs (and, for GPT-5.x, that zero-retention mode is
    # not offered — allowed_modes are default/provider_data_share only).
    mantle: bool = False
    # False for models that REJECT a sampling ``temperature`` (Bedrock answers
    # ValidationException "`temperature` is deprecated for this model";
    # observed for Claude Sonnet 5 on 2026-10-01). Callers that pin
    # temperature for determinism must omit it for these models.
    temperature: bool = True
    # True when Bedrock publishes a GLOBAL cross-Region inference profile for
    # the model (``global.<family>``, listed in the model's card). Those
    # resolve to the global profile in every Region, which is what lets the
    # stack deploy outside the US and bills the global profile's lower rate;
    # the rest (Nova Lite v1 and Nova Pro, which have none) keep their geo
    # profile. Every Claude entry, Nova Lite v2 and the Opus 4.x entries
    # listed an ACTIVE global profile in us-east-1, us-west-2, eu-west-1 and
    # ap-southeast-2 on 2026-10-22, and Opus 4.7/4.8, Haiku 4.5 and Nova
    # Lite v2 answered Converse on it in us-east-1 and us-west-2.
    global_profile: bool = False


# The curated list. To offer a new model, add a line here (and nothing else).
MODELS: list[Model] = [
    # Orchestrator (reasoning + tool use) — Anthropic Claude. Family ids resolve
    # (resolve_id adds the us./eu./ap. prefix) to account-verified ACTIVE inference
    # profiles. NOTE: there is no Claude *Sonnet* 4.7/4.8 — Anthropic shipped 4.7/4.8
    # only in the *Opus* tier; Sonnet tops out at 4.6 below Sonnet 5. Verified via
    # list-inference-profiles.
    # Every entry carries the standard list price, never an introductory rate,
    # so the cost display never understates spend.
    # Claude Sonnet 5.5 is the default orchestrator and onboarding model and
    # Claude Haiku 5.5 the default system-agent model. The older models stay
    # selectable; a deployment's ALLOWED_MODELS decides what a steward can
    # pick. The workshop pins Sonnet 5 and Haiku 4.5 and allows only those two
    # (stacks/_workshop_models.py), so these defaults never reach it.
    # The Claude 5.5 entries (2026-10-21, us-east-1): global and us. profiles
    # ACTIVE, Converse answers without temperature and rejects one
    # (ValidationException), prices are the Price List API's global standard
    # on-demand rates ($ per MTok: Opus 5.5 4/20, Sonnet 5.5 2/10, Haiku 5.5
    # 0.10/0.50). Haiku 5.5 bills long-context prompts at 0.50/2.50, which
    # this per-model rate does not show.
    # Every non-Mantle price is checked against the Price List
    # (tests/unit/test_model_catalog_list_prices.py; read 2026-10-22). Sonnet
    # 5 launched at $2/$10 with $3/$15 announced from 2026-09-01, but the
    # Price List still bills $2/$10, so that is the list price now.
    Model("anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5", ORCHESTRATOR, 0.002, 0.010,
          default=True, temperature=False, global_profile=True),
    Model("anthropic.claude-opus-5-5", "Claude Opus 5.5", ORCHESTRATOR, 0.004, 0.020,
          temperature=False, global_profile=True),
    Model("anthropic.claude-sonnet-5", "Claude Sonnet 5", ORCHESTRATOR, 0.002, 0.010,
          temperature=False, global_profile=True),
    Model("anthropic.claude-sonnet-4-6", "Claude Sonnet 4.6", ORCHESTRATOR, 0.003, 0.015,
          global_profile=True),
    Model("anthropic.claude-sonnet-4-5-20250929-v1:0", "Claude Sonnet 4.5", ORCHESTRATOR, 0.003, 0.015,
          global_profile=True),
    Model("anthropic.claude-opus-4-7", "Claude Opus 4.7", ORCHESTRATOR, 0.005, 0.025,
          global_profile=True),
    Model("anthropic.claude-opus-4-8", "Claude Opus 4.8", ORCHESTRATOR, 0.005, 0.025,
          global_profile=True),
    # Bedrock Mantle (ADR-0055) — GPT-5.6 family, served ONLY at /openai/v1.
    Model("openai.gpt-5.6-sol", "GPT-5.6 Sol (Mantle)", ORCHESTRATOR, 0.005, 0.030, mantle=True),
    Model("openai.gpt-5.6-terra", "GPT-5.6 Terra (Mantle)", ORCHESTRATOR, 0.0025, 0.015, mantle=True),
    # System agents (schema-scoped SQL/NL). Claude Haiku 5.5 is the default.
    Model("anthropic.claude-haiku-5-5", "Claude Haiku 5.5", AGENT, 0.0001, 0.0005, default=True,
          temperature=False, global_profile=True),
    Model("anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku 4.5", AGENT, 0.001, 0.005,
          global_profile=True),
    Model("amazon.nova-2-lite-v1:0", "Nova Lite v2", AGENT, 0.0003, 0.0025,
          global_profile=True),
    Model("amazon.nova-lite-v1:0", "Nova Lite v1", AGENT, 0.00006, 0.00024),
    Model("amazon.nova-pro-v1:0", "Nova Pro", AGENT, 0.0008, 0.0032),
    # Mantle agent option (GPT-5.6 Luna), offered so it can be measured on this
    # workload (docs/experiments.md).
    Model("openai.gpt-5.6-luna", "GPT-5.6 Luna (Mantle)", AGENT, 0.001, 0.006, mantle=True),
]

# AgentCore Memory extraction models: the model_id of the memory's three
# custom strategies (stacks/agentcore_stack.py), which turns conversations
# into the long-term Facts, Summaries and Preferences records. The Admin LLM
# settings page offers these on a base or demo deploy and switches the
# strategies in place (agents/memory_model.py); deploy.sh passes the saved one
# back as the ``memory_extraction_model`` context.
#
# These are the five models AgentCore Memory accepted as a custom-strategy
# model in a live comparison on 2026-10-26 in us-east-1. Acceptance outside
# us-east-1 is unprobed. In that comparison Nova Pro, Sonnet 5 and Haiku 4.5
# sometimes wrote non-English records despite the strategies' English
# instruction. No Mantle model: AgentCore Memory does not serve them.
#
# Claude Haiku 5.5 is the default (base and demo deploys): in a 2026-10-25/26
# side-by-side run on one us-east-1 account (same prompts, same
# conversations, 3 runs each) it wrote no records with details the
# conversation never stated, kept every record in English and saved nothing
# from small talk; Nova Pro did each of those, though it kept more of the
# values the assistant reported. The workshop keeps Nova Pro, pinned in
# stacks/_workshop_models.py, and its page shows the model read-only.
MEMORY_EXTRACTION_FAMILIES: tuple[str, ...] = (
    "anthropic.claude-haiku-5-5",
    "anthropic.claude-sonnet-5-5",
    "amazon.nova-pro-v1:0",
    "anthropic.claude-sonnet-5",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
)

#: The memory extraction model of a base or demo deploy with nothing saved.
MEMORY_DEFAULT_FAMILY = MEMORY_EXTRACTION_FAMILIES[0]

#: Every inference-profile prefix Bedrock uses: the global profile and the geo
#: profiles. ``ap.`` is not a Bedrock prefix; it stays here only so an id
#: written by an older build still reduces to its family.
PROFILE_PREFIXES = ("global.", "us.", "eu.", "apac.", "au.", "jp.", "in.", "ap.")
_PREFIXES = PROFILE_PREFIXES

GLOBAL_PREFIX = "global"


def region_prefix(region: str | None = None) -> str:
    """Geo inference-profile prefix for a Region (models without a global
    profile, such as Nova)."""
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    return "eu" if region.startswith("eu") else "apac" if region.startswith("ap") else "us"


def _by_family(family: str) -> "Model | None":
    return next((m for m in MODELS if m.family == family), None)


def resolve_id(family: str, region: str | None = None) -> str:
    """Inference-profile id for a catalog family, e.g. ``global.anthropic.claude-sonnet-5``.

    A model with a global profile resolves to it in every Region; the rest get
    the Region's geo prefix. Bedrock MANTLE ids (``openai.*``) are returned
    UNCHANGED — they are not inference profiles and the service rejects a
    prefixed form (``us.openai.gpt-5.6-luna``).
    """
    if family.startswith("openai."):
        return family
    m = _by_family(family)
    if m is not None and m.global_profile:
        return f"{GLOBAL_PREFIX}.{family}"
    return f"{region_prefix(region)}.{family}"


def _strip_prefix(model_id: str) -> str:
    for p in _PREFIXES:
        if model_id.startswith(p):
            return model_id[len(p):]
    return model_id


#: Deployment setting that limits which catalog models the Admin LLM settings
#: may select: comma-separated, region-agnostic family ids (the ``family`` of a
#: ``MODELS`` entry). Unset or blank means no restriction, which is the
#: behavior of every deployment that does not set it.
ALLOWED_MODELS_ENV = "ALLOWED_MODELS"


def family_of(model_id: str) -> str:
    """Region-agnostic family id of *model_id* (inference-profile prefix stripped)."""
    return _strip_prefix((model_id or "").strip())


def parse_allowed_models(raw: str | None) -> frozenset[str] | None:
    """Parse an ``ALLOWED_MODELS`` value.

    Returns ``None`` when the value is unset or blank (no restriction).
    Otherwise returns the set of allowed catalog family ids. Entries are
    comma-separated; surrounding whitespace is ignored and a region-prefixed
    id (``us.``/``eu.``/``ap.``) is accepted and reduced to its family.

    Fails loudly with ``ValueError``: on any entry that is not a catalog
    family (a typo must never quietly shrink or empty the selectable set), and
    on a value that is set but names no model at all (``","``).
    """
    text = (raw or "").strip()
    if not text:
        return None
    entries = [family_of(part) for part in text.split(",") if part.strip()]
    if not entries:
        raise ValueError(f"{ALLOWED_MODELS_ENV} is set but names no models: {raw!r}")
    known = {m.family for m in MODELS}
    unknown = sorted({e for e in entries if e not in known})
    if unknown:
        raise ValueError(
            f"{ALLOWED_MODELS_ENV} names models that are not in the catalog "
            f"(agents/model_catalog.py): {', '.join(unknown)}")
    return frozenset(entries)


def allowed_families(environ=None) -> frozenset[str] | None:
    """The deployment's allowlist from *environ* (default ``os.environ``);
    ``None`` when no restriction is configured. Raises like
    :func:`parse_allowed_models` on an invalid value."""
    env = os.environ if environ is None else environ
    return parse_allowed_models(env.get(ALLOWED_MODELS_ENV))


def is_allowed(model_id: str, allowed: frozenset[str] | None) -> bool:
    """True when *model_id* (prefixed or not) is in *allowed* (any role)."""
    return allowed is None or family_of(model_id) in allowed


def role_families(role: str, allowed: frozenset[str] | None) -> frozenset[str] | None:
    """The families *role* may run under the allowlist *allowed*.

    The allowed models of that catalog role, or every allowed model when the
    allowlist names none of that role (the same fallback :func:`default_model`
    takes). ``None`` (no allowlist) means no restriction. This is what keeps
    the workshop's pair from being swapped between the roles: with Sonnet 5
    and Haiku 4.5 allowed, the orchestrator may run only Sonnet 5 and the
    system agents only Haiku 4.5.
    """
    if allowed is None:
        return None
    of_role = frozenset(m.family for m in MODELS if m.family in allowed and m.role == role)
    return of_role or allowed


def is_allowed_for(model_id: str, role: str, allowed: frozenset[str] | None) -> bool:
    """True when *model_id* (prefixed or not) may run as *role* under *allowed*."""
    return is_allowed(model_id, role_families(role, allowed))


def memory_families(allowed: frozenset[str] | None) -> frozenset[str]:
    """The memory extraction families selectable under the allowlist
    *allowed*: :data:`MEMORY_EXTRACTION_FAMILIES`, intersected with the
    allowlist when one is set (``None``: every memory family)."""
    families = frozenset(MEMORY_EXTRACTION_FAMILIES)
    return families if allowed is None else families & allowed


def is_memory_model_allowed(model_id: str, allowed: frozenset[str] | None) -> bool:
    """True when *model_id* (prefixed or not) is a memory extraction model
    :func:`memory_families` offers under *allowed*. Unlike the two roles, no
    allowlist does not mean "anything": only the memory families qualify."""
    return family_of(model_id) in memory_families(allowed)


def effective_memory_model(value: str, region: str | None,
                           allowed: frozenset[str] | None) -> str:
    """The memory extraction model a deploy in *region* puts on the memory
    when *value* is saved (SSM ``/adt/llm-memory-model``), or ``""`` when
    the deploy ignores it. The ONE rule for a saved value: deploy.sh passes
    it as the ``memory_extraction_model`` context only when
    ``stacks/_model_allowlist.py allows ... memory`` says
    :func:`is_memory_model_allowed`, and the stack resolves its family for
    its Region; the Admin save and the GET's drift line use this too."""
    if not is_memory_model_allowed(value, allowed):
        return ""
    return resolve_id(family_of(value), region)


def default_model(role: str, region: str | None = None) -> str:
    """Resolved id of the default model for a role (first default, else first of role).

    Under an allowlist (:data:`ALLOWED_MODELS_ENV`) the result is always an
    allowed model: the role's default when it is allowed, else the first
    allowed model of that role, else the first allowed model of any role.
    """
    allowed = allowed_families()
    pool = [m for m in MODELS if allowed is None or m.family in allowed]
    of_role = [m for m in pool if m.role == role]
    pick = (next((m for m in of_role if m.default), None)
            or (of_role[0] if of_role else None)
            or (pool[0] if allowed is not None and pool else None))
    return resolve_id(pick.family, region) if pick else ""


def env_orchestrator_model(region: str | None = None) -> str:
    """``BEDROCK_MODEL`` when set (non-empty), else the catalog's orchestrator
    default. The one fallback for app code that reads the orchestrator model
    straight from the env, so a missing env var can never land on a hardcoded
    legacy model id."""
    return (os.environ.get("BEDROCK_MODEL") or "").strip() or default_model(ORCHESTRATOR, region)


#: The Price List bills a model that has a global profile 10% more under a geo
#: (``us.``/``eu.``/``apac.``) profile: the catalog stores the global rate, and
#: :func:`pricing` applies this to such an id (a saved older ``us.`` id the
#: deployment still invokes). The frontend's ``priceFor`` mirrors it
#: (parity-tested).
REGIONAL_PREMIUM = 1.1


def pricing(model_id: str) -> dict:
    """USD per 1K tokens for a model id (prefixed or not): the rate of the
    profile the id names, so a geo id of a model with a global profile gets
    :data:`REGIONAL_PREMIUM`.

    Falls back by family keyword so an unknown/new id still gets a sane estimate
    rather than zero cost.
    """
    fam = _strip_prefix(model_id)
    for m in MODELS:
        if m.family == fam:
            factor = REGIONAL_PREMIUM if (m.global_profile and model_id != fam
                                          and not model_id.startswith(f"{GLOBAL_PREFIX}.")) else 1.0
            return {"input": round(m.input_per_1k * factor, 9),
                    "output": round(m.output_per_1k * factor, 9)}
    low = fam.lower()
    if "nova-pro" in low:
        return {"input": 0.0008, "output": 0.0032}
    if "nova" in low:
        return {"input": 0.00006, "output": 0.00024}
    return {"input": 0.003, "output": 0.015}  # sonnet-class default


def accepts_temperature(model_id: str) -> bool:
    """False when the catalog says *model_id* (prefixed or not) rejects a
    sampling ``temperature``. Ids outside the catalog keep the old behavior
    (True), so every model that accepted it before still gets it."""
    fam = _strip_prefix(model_id or "")
    for m in MODELS:
        if m.family == fam:
            return m.temperature
    return True


def all_resolved_ids(region: str | None = None) -> list[str]:
    """Every catalog model resolved for the region (for metric enumeration)."""
    return [resolve_id(m.family, region) for m in MODELS]


def _catalog_entry(m: Model, role: str, region: str | None,
                   available_ids: set | None,
                   allowed: frozenset[str] | None = None,
                   default_id: str = "") -> dict:
    """One region-resolved catalog entry for *role*'s list.

    Without an allowlist (``allowed`` is ``None``) the entry is exactly the
    pre-allowlist shape: ``default`` is true only when the model is the
    default FOR THAT LIST's role, and there is no ``allowed`` key.

    Under an allowlist the entry also carries ``allowed`` (false for a model
    *role* may not run, :func:`role_families`: outside the allowlist, or an
    allowed model of the other role; independent of ``available``, region
    enablement; Mantle entries follow the same rule), and ``default`` marks the list's
    EFFECTIVE default, *default_id* (:func:`default_model` for that role), so
    the marker never points at a model the server would refuse.
    """
    rid = resolve_id(m.family, region)
    # Mantle families are exempt from the inference-profile availability
    # judgment: they are NOT inference profiles (resolve_id returns them
    # unchanged, and list_inference_profiles never lists them), so checking
    # ``rid in available_ids`` would ALWAYS mark them unavailable and the UI
    # would show a false "(not enabled in region)".
    available = True if (available_ids is None or m.mantle) else (rid in available_ids)
    entry = {
        "id": rid,
        "family": m.family,
        "label": m.label,
        "role": m.role,
        "input_per_1k": m.input_per_1k,
        "output_per_1k": m.output_per_1k,
        "default": m.default and m.role == role,
        "available": available,
    }
    if allowed is not None:
        entry["default"] = rid == default_id
        entry["allowed"] = family_of(rid) in (role_families(role, allowed) or ())
    return entry


def catalog(region: str | None = None, available_ids: set | None = None) -> dict:
    """Role-grouped, region-resolved entries.

    EVERY catalog model appears in BOTH role lists (the steward makes an
    informed choice for either role; entries carry list pricing so heavier
    picks are visible). ``default`` marks exactly one entry per list: the
    model :func:`default_model` returns for that list's role. ``role`` keeps
    the model's native role.

    If ``available_ids`` is given (the inference profiles enabled in the
    region), each entry is annotated ``available``; if it's ``None``
    (availability unknown, e.g. local dev or missing permission) everything is
    marked available. Mantle models are always marked available — see
    :func:`_catalog_entry`.

    Without an allowlist (:data:`ALLOWED_MODELS_ENV` unset or blank) the
    response has exactly its pre-allowlist shape: no ``allowlist_active``
    key and no per-entry ``allowed`` key. Under one, every model still
    appears, the response carries ``allowlist_active: true``, each entry
    carries ``allowed`` (false for a model that list's role may not run, so
    the UI shows it disabled rather than hiding it), and each list's ``default`` follows the
    allowlist-aware :func:`default_model`, including its cross-role fallback.

    The ``memory`` list holds the memory extraction models
    (:data:`MEMORY_EXTRACTION_FAMILIES`, in that order) with the same entry
    fields; ``default`` marks :data:`MEMORY_DEFAULT_FAMILY` (what a deploy
    with nothing saved runs), and under an allowlist ``allowed`` follows
    :func:`memory_families`.
    """
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    allowed = allowed_families()
    out: dict = {ORCHESTRATOR: [], AGENT: [], "region": region}
    if allowed is not None:
        out["allowlist_active"] = True
    for role in (ORCHESTRATOR, AGENT):
        default_id = default_model(role, region) if allowed is not None else ""
        for m in MODELS:
            out[role].append(
                _catalog_entry(m, role, region, available_ids, allowed, default_id))
    out[MEMORY] = _memory_entries(region, available_ids, allowed)
    return out


def _memory_entries(region: str, available_ids: set | None,
                    allowed: frozenset[str] | None) -> list[dict]:
    """The catalog's ``memory`` list (see :func:`catalog`)."""
    offered = memory_families(allowed)
    entries = []
    for family in MEMORY_EXTRACTION_FAMILIES:
        entry = _catalog_entry(_by_family(family), MEMORY, region, available_ids)
        entry["default"] = family == MEMORY_DEFAULT_FAMILY
        if allowed is not None:
            entry["allowed"] = family in offered
        entries.append(entry)
    return entries
