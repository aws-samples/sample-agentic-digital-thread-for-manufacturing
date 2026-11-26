# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The workshop profile's models: the one home for its pins.

The workshop runs Claude Sonnet 5 (orchestrator and onboarding) and Claude
Haiku 4.5 (system agents), and its Admin LLM settings page offers only those
two. Its AgentCore Memory extraction model is Amazon Nova Pro
(``WORKSHOP_MEMORY_EXTRACTION_FAMILY``, read by stacks/agentcore_stack.py on
any ``with_workshop`` synth; ``python3 stacks/_workshop_models.py resolve
<name> [<operator value>]`` prints them). The base and
demo deployments use the catalog defaults instead
(``agents/model_catalog.py``), so moving those defaults never moves the
workshop.

Both models are Bedrock GLOBAL cross-Region inference profiles, like every
catalog model that has one: the same id in every Region (so no pin depends on
the deploy Region), and the global profile's lower rate.

The pins are not operator-overridable: ``resolve`` accepts an operator value
only when it names the same model (any inference-profile spelling of the
pinned family, or the same allowlist set) and refuses anything else, so a
``BEDROCK_MODEL`` left exported in a shell cannot put a workshop on another
model.

Stdlib only: loaded by path from packaging and from deploy.sh, outside the
CDK app.
"""
from __future__ import annotations

import sys

#: The workshop's orchestrator model (the app's BEDROCK_MODEL and the
#: orchestrator and onboarding runtimes'). The global inference profile.
WORKSHOP_BEDROCK_MODEL = "global.anthropic.claude-sonnet-5"

#: The workshop's system-agent model (the app's SYSTEM_AGENT_MODEL, and the
#: onboarding mapping step's). The global inference profile.
WORKSHOP_SYSTEM_AGENT_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

#: The workshop's AgentCore Memory extraction model family (the model_id of
#: the memory's custom strategies, stacks/agentcore_stack.py). Base and demo
#: deploys use Claude Haiku 5.5; the workshop keeps Amazon Nova Pro, so its
#: templates do not move with the base. Nova Pro has no global inference
#: profile, so the stack resolves the deploy Region's geo id from the model
#: catalog. Not part of WORKSHOP_ALLOWED_MODELS: the Admin LLM settings page
#: shows the workshop's memory extraction model read-only, and the stack
#: refuses the ``memory_extraction_model`` context on a workshop synth.
WORKSHOP_MEMORY_EXTRACTION_FAMILY = "amazon.nova-pro-v1:0"

#: Every inference-profile prefix Bedrock uses (model_catalog.PROFILE_PREFIXES).
_PREFIXES = ("global.", "us.", "eu.", "apac.", "au.", "jp.", "in.", "ap.")


def _family(model_id: str) -> str:
    text = (model_id or "").strip()
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


#: Region-agnostic families of the workshop's two models.
WORKSHOP_BEDROCK_FAMILY = _family(WORKSHOP_BEDROCK_MODEL)
WORKSHOP_SYSTEM_AGENT_FAMILY = _family(WORKSHOP_SYSTEM_AGENT_MODEL)

#: The workshop's model allowlist (the ``allowed_models`` context, the app's
#: ALLOWED_MODELS): exactly its two models, orchestrator first.
WORKSHOP_ALLOWED_MODELS = ",".join((WORKSHOP_BEDROCK_FAMILY, WORKSHOP_SYSTEM_AGENT_FAMILY))

_PINS = {
    "bedrock_model": WORKSHOP_BEDROCK_MODEL,
    "system_agent_model": WORKSHOP_SYSTEM_AGENT_MODEL,
    "allowed_models": WORKSHOP_ALLOWED_MODELS,
}


def value(name: str) -> str:
    """One pin by name."""
    return _PINS[name]


def resolve(name: str, operator: str = "") -> str:
    """The value a workshop deploy uses for *name*.

    The pin when *operator* is empty. An operator value naming the SAME model
    is kept as spelled (a model id of the pinned family, or an allowlist with
    exactly the pinned families). Anything else raises ``ValueError``: the
    workshop's models are not changeable.
    """
    pinned = value(name)
    given = (operator or "").strip()
    if not given:
        return pinned
    if name == "allowed_models":
        families = {_family(part) for part in given.split(",") if part.strip()}
        same = families == {WORKSHOP_BEDROCK_FAMILY, WORKSHOP_SYSTEM_AGENT_FAMILY}
    else:
        same = _family(given) == _family(pinned)
    if not same:
        env = name.upper() if name != "bedrock_model" else "BEDROCK_MODEL"
        raise ValueError(
            f"--workshop runs fixed models; {env}={given} conflicts with the workshop's "
            f"{pinned} (stacks/_workshop_models.py). Unset {env} for a workshop deploy.")
    return given


if __name__ == "__main__":  # pragma: no cover - exercised by the deploy.sh test
    args = sys.argv[1:]
    if len(args) not in (2, 3) or args[0] != "resolve":
        sys.exit("usage: _workshop_models.py resolve "
                 "bedrock_model|system_agent_model|allowed_models [operator-value]")
    try:
        print(resolve(*args[1:]))
    except (KeyError, ValueError) as exc:
        sys.exit(f"ERROR: {exc}")
