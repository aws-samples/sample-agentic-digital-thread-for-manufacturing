# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Single source for the *configured* LLM model IDs.

Both the admin **LLM Configuration** page and the **Agents** fleet page report
which models are in use. This module is the one place that resolves them (two
separate readers with different fallback defaults could disagree — and disagree
with what the runtime actually runs), in the same order the runtime itself
resolves:

  SSM override (set from the admin page in cloud) → the env the agents actually
  read (``BEDROCK_MODEL`` for the orchestrator, ``SYSTEM_AGENT_MODEL`` for system
  agents) → a sane default.
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_SSM_ORCHESTRATOR = "/adt/llm-orchestrator-model"
_SSM_AGENT = "/adt/llm-agent-model"

#: One SSM client per Region for the process. ``db.bedrock_call`` reads the
#: saved agent model on every call (so a save applies to the next one), and a
#: client built per read would cost more than the read. Built under the lock:
#: boto3's default session is not safe to build clients from in two threads
#: at once.
_SSM_CLIENTS: dict = {}
_SSM_CLIENTS_LOCK = threading.Lock()


def _ssm_client(region: str):
    """The process's SSM client for *region* (2 s connect/read, one attempt)."""
    with _SSM_CLIENTS_LOCK:
        client = _SSM_CLIENTS.get(region)
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client(
                "ssm", region_name=region,
                config=Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 1}))
            _SSM_CLIENTS[region] = client
        return client


def _reset_ssm_clients() -> None:
    """Forget the process's SSM clients (tests stub ``boto3.client``)."""
    with _SSM_CLIENTS_LOCK:
        _SSM_CLIENTS.clear()


def _ssm_value(name: str, region: str) -> str:
    """Read an SSM string parameter, or "" if unset/unavailable (e.g. local dev).

    Bounded (2 s connect/read, one attempt): this read sits on the provisioning
    path as well as the Settings page, and a slow SSM must cost seconds there,
    never the SDK's default timeouts and retries."""
    try:
        return _ssm_client(region).get_parameter(Name=name)["Parameter"]["Value"]
    except Exception:  # nosec B110 - SSM is an optional override; fall back to env
        return ""


def get_configured_models(region: str | None = None) -> dict:
    """Return ``{"orchestrator", "agent", "region"}`` reflecting actual usage.

    The orchestrator model mirrors ``BEDROCK_MODEL`` (what ``orchestrator.py``
    reads); the system-agent model mirrors ``SYSTEM_AGENT_MODEL`` (what
    ``system_agent.create_system_agent`` reads). An SSM parameter, when set,
    overrides the env (that is how the admin page persists a change in cloud).
    Defaults come from the curated catalog (agents/model_catalog.py).
    """
    from .model_catalog import default_model

    region = region or os.environ.get("AWS_REGION", "us-east-1")

    orchestrator = (
        _allowed_saved_model(_SSM_ORCHESTRATOR, region, "orchestrator")
        or os.environ.get("BEDROCK_MODEL")
        or default_model("orchestrator", region)
    )
    return {"orchestrator": orchestrator, "agent": configured_agent_model(region),
            "region": region}


def _allowed_saved_model(name: str, region: str, role: str) -> str:
    """The model saved in SSM *name*, or "" when none is saved or the
    deployment's ``ALLOWED_MODELS`` does not offer it for *role*
    (``model_catalog.role_families``).

    A value saved before a later deploy tightened the allowlist (a demo
    account redeployed as a workshop, say), or an allowed model saved in the
    other role's slot, is ignored with a warning: the save path refuses such
    a model, deploy.sh does not deploy it, and the Settings page must not
    report a model the deployment does not run."""
    from .model_catalog import allowed_families, is_allowed_for

    saved = _ssm_value(name, region)
    if saved and not is_allowed_for(saved, role, allowed_families()):
        logger.warning("ignoring saved %s model %s: not offered for that role by "
                       "this deployment's model allowlist", role, saved)
        return ""
    return saved


def configured_agent_model(region: str | None = None) -> str:
    """The system-agent model: SSM override, else env, else catalog default.

    One resolution for what Settings reports AND what a newly provisioned
    system runtime gets (``AgentManager._system_agent_model``), so a system
    onboarded after an Admin > Settings save runs the saved model, not the
    deploy-time ``SYSTEM_AGENT_MODEL`` of the app task.

    A saved value the deployment's ``ALLOWED_MODELS`` no longer offers (saved
    before a later deploy tightened the allowlist) is ignored with a warning:
    the save path refuses such a model, so provisioning must not run one
    either. The env value is checked against the allowlist at synth.
    """
    from .model_catalog import default_model

    region = region or os.environ.get("AWS_REGION", "us-east-1")
    return (
        _allowed_saved_model(_SSM_AGENT, region, "agent")
        or os.environ.get("SYSTEM_AGENT_MODEL")
        or os.environ.get("AGENT_MODEL")
        or default_model("agent", region)
    )
