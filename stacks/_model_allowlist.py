# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synth-time wiring of the deployment model allowlist (``ALLOWED_MODELS``).

The optional ``allowed_models`` CDK context (comma-separated, region-agnostic
model family ids from ``agentic_digital_thread/agents/model_catalog.py``)
becomes the app container's ``ALLOWED_MODELS`` env, which limits the models
the Admin LLM settings page offers and accepts. Unset or blank adds nothing,
so a deployment that does not set it synthesizes the same template as before.

Parsing is the app's own rule (``model_catalog.parse_allowed_models``, loaded
by path because the stacks package cannot import the app package; the module
is stdlib-only), so a value the app would refuse at boot fails the synth
instead. The synth also fails when a model the deployment itself configures
is outside its own allowlist: the Admin page would then show the running
model as not offered, and could not save it back.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import ModuleType

#: CDK context key (``-c allowed_models=<family>,<family>``).
CONTEXT_KEY = "allowed_models"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_CATALOG = os.path.join(
    _REPO_ROOT, "agentic_digital_thread", "agents", "model_catalog.py")
RUNTIME_ENV_CONTRACT = os.path.join(
    _REPO_ROOT, "agentic_digital_thread", "agents", "runtime_env_contract.py")


def _load_by_path(name: str, path: str) -> ModuleType:
    """A stdlib-only app module, loaded by path once per process."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses resolve their module by name
        spec.loader.exec_module(module)
    return sys.modules[name]


def load_model_catalog() -> ModuleType:
    """The app's model catalog module, loaded by path once per process."""
    return _load_by_path("adt_stacks_model_catalog", MODEL_CATALOG)


def default_bedrock_model(region: str) -> str:
    """The orchestrator model default, as the runtime-env contract spells it
    (the one spelling the runtimes also fall back to)."""
    return _load_by_path("adt_stacks_runtime_env_contract",
                         RUNTIME_ENV_CONTRACT).default_bedrock_model(region)


def default_system_agent_model(region: str) -> str:
    """The system-agent model default, as the runtime-env contract spells it."""
    return _load_by_path("adt_stacks_runtime_env_contract",
                         RUNTIME_ENV_CONTRACT).default_system_agent_model(region)


def _context_text(raw) -> str:
    """Context arrives as a string from ``-c``, or as a list from cdk.json."""
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        return ",".join(str(item) for item in raw)
    return str(raw)


#: The app env variable that carries each role's model, for the synth check.
_ENV_ROLE = {"BEDROCK_MODEL": "orchestrator", "SYSTEM_AGENT_MODEL": "agent"}


def _allowed_for_env(catalog, name: str, model_id: str, allowed) -> bool:
    role = _ENV_ROLE.get(name)
    if role is None:
        return catalog.is_allowed(model_id, allowed)
    return catalog.is_allowed_for(model_id, role, allowed)


def allowed_models_env(raw, configured: dict[str, str]) -> dict[str, str]:
    """The app-container env for the ``allowed_models`` context value *raw*.

    ``{}`` when unset or blank. Otherwise ``{"ALLOWED_MODELS": <families>}``
    (sorted, comma-joined). Raises ``ValueError`` for an id outside the
    catalog, a value naming no model, or when any model in *configured*
    (env name -> model id, e.g. ``BEDROCK_MODEL``) may not run in its role
    under the allowlist (``model_catalog.role_families``).
    """
    catalog = load_model_catalog()
    allowed = catalog.parse_allowed_models(_context_text(raw))
    if allowed is None:
        return {}
    outside = sorted(f"{name}={model_id}" for name, model_id in configured.items()
                     if model_id and not _allowed_for_env(catalog, name, model_id, allowed))
    if outside:
        raise ValueError(
            f"-c {CONTEXT_KEY} excludes the deployment's own configured "
            f"model(s): {', '.join(outside)}; add them to {CONTEXT_KEY} or "
            "change the configured model")
    return {catalog.ALLOWED_MODELS_ENV: ",".join(sorted(allowed))}


def allowed_models_value(raw) -> str:
    """The canonical allowlist value for context *raw* (what the app container
    gets as ALLOWED_MODELS); ``""`` when unset or blank. Raises like
    :func:`allowed_models_env`."""
    env = allowed_models_env(raw, {})
    return next(iter(env.values()), "")


def allows(raw, model_id: str, role: str = "") -> bool:
    """True when the allowlist *raw* (unset or blank: no restriction) allows
    *model_id*, for *role* when given (``model_catalog.role_families``).
    Role ``memory`` asks about the memory extraction model instead: one of
    ``model_catalog.MEMORY_EXTRACTION_FAMILIES``, intersected with the
    allowlist when one is set (``model_catalog.effective_memory_model``, the
    rule the app's save and its drift line use too).
    deploy.sh asks before reusing a saved Admin model."""
    catalog = load_model_catalog()
    allowed = catalog.parse_allowed_models(_context_text(raw))
    if role == catalog.MEMORY:
        return bool(catalog.effective_memory_model(model_id, None, allowed))
    if role:
        return catalog.is_allowed_for(model_id, role, allowed)
    return catalog.is_allowed(model_id, allowed)


if __name__ == "__main__":  # pragma: no cover - exercised by the deploy.sh test
    # python3 stacks/_model_allowlist.py allows <ALLOWED_MODELS or ""> <model-id> [role]
    # Exit 0 when allowed, 1 when not, 2 on bad usage or an invalid allowlist.
    if len(sys.argv) not in (4, 5) or sys.argv[1] != "allows":
        sys.exit(2)
    try:
        sys.exit(0 if allows(*sys.argv[2:]) else 1)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
