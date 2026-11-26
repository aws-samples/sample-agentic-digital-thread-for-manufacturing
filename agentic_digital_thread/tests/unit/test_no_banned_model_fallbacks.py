# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No default or fallback model id may name an older Claude model.

The supported defaults are Claude Sonnet 5.5, Claude Haiku 5.5 and Amazon
Nova Pro; the workshop pins Claude Sonnet 5 and Claude Haiku 4.5, which stay
supported. Claude Haiku 3.5, Claude Opus 4.5/4.6/4.7/4.8 and Claude Sonnet
4.5/4.6 may not serve as one. The workshop package sets its models
explicitly, but every code path that runs when a model env var is missing
falls back to a literal or to the catalog default, so those fallbacks must
also stay on the supported list.

The catalog may still offer older models; a deployment's ALLOWED_MODELS
decides what a steward can pick. Those entries, and the metrics/pricing
tables that label them, are the only places an older Claude id may appear
(``_DATA_ONLY``).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]   # agentic_digital_thread/
_REPO_ROOT = _ADT_ROOT.parent

# Every Claude 3.x and Claude Sonnet/Opus 4.x id, in dash or dot spelling:
# the models above plus Claude Sonnet 4 / Opus 4.0-4.1, none of which is a
# supported default. Sonnet 5, Opus 5 and Haiku 4.5 do not match.
_OLD_CLAUDE = re.compile(r"claude-(?:(?:sonnet|opus)-4|3[-.]|haiku-3)")

_APPROVED = {
    "anthropic.claude-sonnet-5-5",
    "anthropic.claude-haiku-5-5",
    "anthropic.claude-sonnet-5",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "amazon.nova-pro-v1:0",
}

# Files where an older Claude id is DATA, never a selection: the selectable
# catalog entries and the tables that label or aggregate their metrics.
_DATA_ONLY = {
    "agentic_digital_thread/agents/model_catalog.py",   # selectable MODELS entries
    "agentic_digital_thread/agents/agents_routes.py",   # per-model metrics keys
    "agentic_digital_thread/frontend/src/shared/agents/pricing.ts",  # price/label table
}


def _rel(path: Path) -> str:
    return str(path.relative_to(_REPO_ROOT))


def _family(model_id: str) -> str:
    for prefix in ("global.", "us.", "eu.", "apac.", "ap."):
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


# --------------------------------------------------------------------------- #
# The selection seams, asserted directly
# --------------------------------------------------------------------------- #

def test_catalog_defaults_are_on_the_approved_list():
    from agents.model_catalog import AGENT, ORCHESTRATOR, default_model
    for role, expected in ((ORCHESTRATOR, "anthropic.claude-sonnet-5-5"),
                           (AGENT, "anthropic.claude-haiku-5-5")):
        for region in ("us-east-1", "us-west-2", "eu-west-1"):
            got = default_model(role, region)
            assert _family(got) == expected, (role, region, got)
            assert expected in _APPROVED


def test_no_catalog_default_is_an_old_claude_model():
    from agents.model_catalog import MODELS
    bad = [m.family for m in MODELS if m.default and _OLD_CLAUDE.search(m.family)]
    assert not bad, bad


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_env_orchestrator_model_blank_env_is_the_catalog_default(monkeypatch, value):
    monkeypatch.setenv("BEDROCK_MODEL", value)
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    from agents.model_catalog import env_orchestrator_model
    assert env_orchestrator_model("us-east-1") == "global.anthropic.claude-sonnet-5-5"


def test_env_orchestrator_model_set_env_wins(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL", " global.anthropic.claude-haiku-4-5-20251001-v1:0 ")
    from agents.model_catalog import env_orchestrator_model
    assert env_orchestrator_model() == "global.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.mark.parametrize("region,expected", [
    ("us-east-1", "global.anthropic.claude-sonnet-5-5"),
    ("us-west-2", "global.anthropic.claude-sonnet-5-5"),
    ("eu-west-1", "global.anthropic.claude-sonnet-5-5"),
])
def test_runtime_env_contract_default_is_sonnet_5_5(region, expected):
    from agents.runtime_env_contract import default_bedrock_model, default_system_agent_model
    assert default_bedrock_model(region) == expected
    assert default_system_agent_model(region) == "global.anthropic.claude-haiku-5-5"


def test_contract_and_catalog_defaults_agree():
    """The contract spells the defaults the runtimes fall back to; the catalog
    marks the defaults the Admin page shows. One answer."""
    from agents.model_catalog import AGENT, ORCHESTRATOR, default_model
    from agents.runtime_env_contract import default_bedrock_model, default_system_agent_model
    for region in ("us-east-1", "eu-west-1"):
        assert default_bedrock_model(region) == default_model(ORCHESTRATOR, region)
        assert default_system_agent_model(region) == default_model(AGENT, region)


def test_provisioner_input_default_follows_the_deployment(monkeypatch):
    """A Step Functions provision input that omits bedrock_model takes the
    provisioner's BEDROCK_MODEL (the deployment's model), else the contract
    default; never a literal that drifts from the rest of the deployment."""
    from agents.provisioner.handlers import _runtime_env_vars
    from agents.provisioner.models import CreateRuntimeInput
    # system_agent_model is passed so __post_init__ makes no SSM read.
    inp = CreateRuntimeInput(
        system_id="s", region="us-east-1", connection_type="PostgreSQL",
        system_agent_model="global.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert inp.bedrock_model == ""
    monkeypatch.delenv("BEDROCK_MODEL", raising=False)
    env = _runtime_env_vars(inp, "arn:secret", {"host": "db"})
    assert env["BEDROCK_MODEL"] == "global.anthropic.claude-sonnet-5-5"
    monkeypatch.setenv("BEDROCK_MODEL", "global.anthropic.claude-sonnet-5")
    env = _runtime_env_vars(inp, "arn:secret", {"host": "db"})
    assert env["BEDROCK_MODEL"] == "global.anthropic.claude-sonnet-5"


def test_cdk_context_default_is_the_contract_default():
    """cdk.json's bedrock_model (the app's BEDROCK_MODEL on a deploy.sh run
    without an override) and the contract default (the runtimes' on the same
    run) must agree, or the app and its orchestrator runtime split."""
    from agents.runtime_env_contract import default_bedrock_model
    ctx = json.loads((_REPO_ROOT / "cdk.json").read_text())["context"]
    assert ctx["bedrock_model"] == default_bedrock_model("us-east-1")
    assert _family(ctx["bedrock_model"]) in _APPROVED


# --------------------------------------------------------------------------- #
# Python: every string constant (not just env-call literals)
# --------------------------------------------------------------------------- #

_SKIP_DIRS = {"tests", "test", "telemetry", "node_modules", "cdk.out",
              "site-packages", "__pycache__", "build", "dist"}


def _python_sources() -> list[Path]:
    """Every Python file in the repo except tests, telemetry, generated output
    and hidden directories (.git, .venv, .kiro and the like)."""
    out = []
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).parts
        if (_SKIP_DIRS & set(rel[:-1]) or any(p.startswith(".") for p in rel[:-1])
                or rel[-1].startswith("test_") or rel[-1] == "conftest.py"):
            continue
        out.append(path)
    return out


def test_python_scan_reaches_the_model_selecting_scripts():
    """The scan must cover the harness scripts that pick a default model."""
    scanned = {_rel(p) for p in _python_sources()}
    for must in ("scripts/audit_answer.py",
                 "agentic_digital_thread/scripts/generalization_review.py",
                 "agentic_digital_thread/agents/orchestrator.py",
                 "stacks/adt_compute_stack.py"):
        assert must in scanned, must


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                ids.add(id(first.value))
    return ids


def test_python_string_constants_name_no_old_claude_model():
    """Covers literal env fallbacks, module constants, f-string parts,
    ``getenv(KEY) or "<id>"``, env dicts and CDK props alike: any old Claude
    id in a string constant outside ``_DATA_ONLY`` fails. Docstrings are
    prose and are checked by review, not here."""
    bad = []
    for path in _python_sources():
        if _rel(path) in _DATA_ONLY:
            continue
        tree = ast.parse(path.read_text())
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs and _OLD_CLAUDE.search(node.value)):
                bad.append(f"{_rel(path)}:{node.lineno}: {node.value[:80]}")
    assert not bad, bad


def test_the_constant_scan_catches_the_usual_fallback_shapes(tmp_path):
    """The scanner above must see each shape a regression would take."""
    shapes = [
        'import os\nos.environ.get("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")\n',
        'import os\nDEFAULT = "global.anthropic.claude-sonnet-4-6"\nos.getenv("BEDROCK_MODEL", DEFAULT)\n',
        'import os\nKEY = "BEDROCK_MODEL"\nos.getenv(KEY, "global.anthropic.claude-sonnet-4-6")\n',
        'import os\np = "us"\nos.getenv("BEDROCK_MODEL", f"{p}.anthropic.claude-sonnet-4-6")\n',
        'import os\nm = os.getenv("BEDROCK_MODEL") or "us.anthropic.claude-sonnet-4.6"\n',
        'env = {}\nenv.get("BEDROCK_MODEL", "us.anthropic.claude-3-5-haiku-20241022-v1:0")\n',
        'env = {"BEDROCK_MODEL": "eu.anthropic.claude-opus-4-8"}\n',
    ]
    for src in shapes:
        tree = ast.parse(src)
        docs = _docstring_ids(tree)
        hits = [n for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docs and _OLD_CLAUDE.search(n.value)]
        assert hits, src
    for ok in ("global.anthropic.claude-sonnet-5", "global.anthropic.claude-sonnet-5",
               "global.anthropic.claude-haiku-4-5-20251001-v1:0", "us.anthropic.claude-opus-5",
               "us.amazon.nova-pro-v1:0"):
        assert not _OLD_CLAUDE.search(ok), ok


def test_data_only_files_still_exist():
    """A rename must not silently widen the exemption."""
    missing = [p for p in _DATA_ONLY if not (_REPO_ROOT / p).is_file()]
    assert not missing, missing


# --------------------------------------------------------------------------- #
# Non-Python deploy inputs and the frontend: whole-text scans
# --------------------------------------------------------------------------- #

def _text_sources() -> list[Path]:
    files = [
        _REPO_ROOT / "cdk.json",
        _REPO_ROOT / ".env.local.example",
        _REPO_ROOT / "config" / "highbyte" / "intelligencehub-configuration.json",
    ]
    for path in sorted(_REPO_ROOT.rglob("*.sh")):
        rel = path.relative_to(_REPO_ROOT).parts
        if not (_SKIP_DIRS & set(rel[:-1]) or any(p.startswith(".") for p in rel[:-1])):
            files.append(path)
    fe = _ADT_ROOT / "frontend" / "src"
    for path in sorted(fe.rglob("*")):
        if path.suffix in {".ts", ".tsx", ".js", ".jsx"} and "test" not in path.parts \
                and ".test." not in path.name:
            files.append(path)
    return [p for p in files if p.is_file() and _rel(p) not in _DATA_ONLY]


@pytest.mark.parametrize("path", _text_sources(), ids=_rel)
def test_text_sources_name_no_old_claude_model(path):
    hits = [f"{n}: {line.strip()[:120]}" for n, line in
            enumerate(path.read_text(errors="replace").splitlines(), 1)
            if _OLD_CLAUDE.search(line)]
    assert not hits, hits


def test_highbyte_project_bedrock_inputs_are_approved():
    cfg = json.loads((_REPO_ROOT / "config" / "highbyte"
                      / "intelligencehub-configuration.json").read_text())
    models = [(i.get("name"), (i.get("qualifier") or {}).get("modelType", ""))
              for i in cfg["project"]["inputs"] if i.get("type") == "aws.bedrock"]
    bad = [(name, m) for name, m in models if _family(m) not in _APPROVED]
    assert models and not bad, bad
