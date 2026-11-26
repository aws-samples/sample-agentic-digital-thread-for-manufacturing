# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Source tripwires — the post-query Agent Analysis LLM's model selection.

Live evidence (2026-07): ``runtime_app`` hardcoded a Nova Lite fallback
for the analysis LLM while ``SYSTEM_AGENT_MODEL`` was unset on the deployed
runtime, so the analysis ran on a model other than the catalog's AGENT
default (Claude Haiku 4.5). The fallback must be
``model_catalog.default_model(AGENT)``; the catalog stays the single source.

Pure source/catalog assertions — no AWS, no network.
"""
from __future__ import annotations

from pathlib import Path

from agents import model_catalog

_RUNTIME_APP = Path(__file__).resolve().parents[2] / "agents" / "runtime_app.py"


def _source() -> str:
    return _RUNTIME_APP.read_text(encoding="utf-8")


def test_no_hardcoded_nova_lite_in_runtime_app():
    """TRIPWIRE: no nova-lite model literal anywhere in runtime_app source."""
    assert "nova-lite" not in _source().lower(), (
        "runtime_app.py hardcodes a nova-lite model id — the analysis-LLM "
        "fallback must come from model_catalog.default_model(AGENT)"
    )


def test_analysis_fallback_references_the_model_catalog():
    """The env override falls back to the catalog's AGENT default."""
    src = _source()
    assert "default_model" in src
    assert 'os.environ.get("SYSTEM_AGENT_MODEL") or _default_model(_AGENT_ROLE)' in src


def test_agent_role_default_is_not_nova_lite():
    """The catalog's AGENT default is the mandated model (Haiku-class),
    never Nova Lite — pins the role the runtime fallback resolves through."""
    resolved = model_catalog.default_model(model_catalog.AGENT)
    assert resolved, "catalog must resolve an AGENT default"
    assert "nova-lite" not in resolved.lower()
    assert "anthropic.claude-haiku" in resolved


# --------------------------------------------------------------------------- #
# Fleet-wide tripwire (2026-07 follow-up): the same hardcoded-fallback bug was
# later found in system_agent.py, agent_manager.py, and provisioner/models.py.
# Sweep ALL of agents/ so a nova-lite DEFAULT can't sneak into any module.
# --------------------------------------------------------------------------- #
_AGENTS_DIR = _RUNTIME_APP.parent

# Sanctioned nova-lite literals — places where the id is legitimately data,
# never a fallback default:
#   - model_catalog.py: Nova Lite is a selectable (non-default) catalog option.
#   - agents_routes.py: historical metrics-merge keys aggregate usage that was
#     recorded under the old model ids.
_SANCTIONED = {"model_catalog.py", "agents_routes.py"}


def test_no_hardcoded_nova_lite_default_anywhere_in_agents():
    """TRIPWIRE: no nova-lite literal in any agents/ module outside the
    sanctioned data-only homes. Model fallbacks must resolve through
    ``model_catalog.default_model(AGENT)`` — the catalog is the single source."""
    offenders = sorted(
        str(p.relative_to(_AGENTS_DIR))
        for p in _AGENTS_DIR.rglob("*.py")
        if p.name not in _SANCTIONED
        and "nova-lite" in p.read_text(encoding="utf-8").lower()
    )
    assert not offenders, (
        f"nova-lite literal found in {offenders} — hardcoded model fallbacks "
        "are forbidden. "
        "Fall back to model_catalog.default_model(AGENT) instead, or "
        "add the file to _SANCTIONED with a data-only justification."
    )
