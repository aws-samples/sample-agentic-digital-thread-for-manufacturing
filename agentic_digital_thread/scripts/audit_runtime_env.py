#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""audit_runtime_env.py — computed runtime-env drift lane (against a DEPLOYED env).

COMPLEMENTS smoke_prompts.py / audit_learning.py / eval_answers.py — never
replaces them. Enumerates every AgentCore runtime through the ONE sanctioned
pagination-safe enumerator (``runtime_provision_helpers.list_all_agent_runtimes``),
classifies each by name (``adt_orchestrator`` → orchestrator, ``adt_onboarding``
→ onboarding, other ``adt_*`` → system), and reports three COMPUTED classes
(verdicts are never LLM-judged):

* **missing** — a ``runtime_env_contract.REQUIRED_KEYS`` key absent or empty.
* **stale**   — a deterministic contract value (``STALE_CHECKED_KEYS`` — CFN-
  derived only) differing from the audit-time expected Sources. Live-swap keys
  (``LIVE_SWAP_KEYS``: BEDROCK_MODEL / SYSTEM_AGENT_MODEL / SYSTEM_QUERY_MODE /
  … — ADR-0055 model swaps, sticky-SSM query-mode flips, ADR-0049 secret
  rotation) are NEVER stale-checked: model-swapped env is NOT stale. A stale
  check only fires when the expected value is non-empty (unset expectations
  never alarm).
* **foreign** — keys the contract does not know. INFORMATIONAL ONLY, never
  gating: operator-owned keys (``OPERATOR_OWNED_KEYS``, e.g.
  SYSTEM_REQUIRED_GROUP) are allowlisted in the contract and never flagged.

Plus **posture** facts (informational, never gating): closed-vocabulary labels
for posture keys — ``MEMORY_LTM_SCOPE`` on the orchestrator (``session`` under
the workshop profile; absent ⇒ ``actor (default)``; anything else is labelled
``unrecognised``, never echoed).

``--reader-logins`` (opt-in, annotate-only, never gating) also reads each
PostgreSQL system runtime's agent-config secret and reports the login it
connects with, by name: its schema's read-only login (``reader``), the
deployment's own database login (``deployment``: the system still needs the
one-time upgrade step in docs/deployment.md), the system's own login
(``own``, a registered customer database), or none. It needs
``secretsmanager:GetSecretValue`` on ``adt/agent-config/*``; no secret value,
password or pointer is ever printed.

Every run (annotate-only, never gating) also reports, per SQL or Athena
system runtime, whether it starts with a baked table list (its
``TABLES_DESC`` or its agent-config's ``tables_desc``). A runtime with none
has its state-time and storage-bound checks inactive, so a drift-free report
is not a clean bill for it. Reading the secret needs the same
``secretsmanager:GetSecretValue``; a denied read is reported as
``unreadable``, by exception type only.

``--strict`` exits non-zero on missing/stale only. Expected values come from
the process env (export the CFN outputs, or run with the app task env) via
``RuntimeEnvSources.from_ambient()``.

Digest-only logging: env VALUES never reach stdout/logs — stale findings show
an 8-hex SHA-256 digest of expected vs actual.

Usage:
    python3 scripts/audit_runtime_env.py --region us-east-1 \
        [--base-url https://app.example --username ADT --password "$ADT_PASSWORD"] \
        [--timeout 30] [--strict] [--reader-logins]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agents.config import MEMORY_LTM_SCOPES
from agents.runtime_env_contract import (
    FILL_IF_ABSENT,
    REQUIRED_KEYS,
    STALE_CHECKED_KEYS,
    missing_required,
    RuntimeEnvSources,
    build_env,
    known_keys,
)
from agents.runtime_provision_helpers import list_all_agent_runtimes


def classify_role(runtime_name: str) -> str | None:
    """Contract role for a runtime name; None for non-ADT runtimes."""
    if runtime_name == "adt_orchestrator":
        return "orchestrator"
    if runtime_name == "adt_onboarding":
        return "onboarding"
    if runtime_name.startswith("adt_"):
        return "system"
    return None


def _digest(value: str) -> str:
    """8-hex digest so findings are comparable without leaking values."""
    return hashlib.sha256((value or "").encode()).hexdigest()[:8]


@dataclass
class RuntimeFinding:
    """Computed drift classes for one runtime."""

    runtime_name: str
    role: str
    missing: list = field(default_factory=list)   # [key, ...]
    stale: list = field(default_factory=list)     # [(key, expected8, actual8)]
    foreign: list = field(default_factory=list)   # [key, ...] informational
    #: Closed-vocabulary posture facts ({key: label}) — informational, never
    #: gating; a value outside the vocabulary is labelled, never echoed.
    posture: dict = field(default_factory=dict)
    #: Exception TYPE NAME when this runtime's live env could not be read at
    #: all (transient AWS error, permissions). Drift is then UNKNOWN, not
    #: clean — reported loudly but deliberately non-gating so a transient
    #: API failure cannot flap a pre-push gate. Never carries a message,
    #: which could contain ARNs or endpoints.
    unauditable: str = ""

    @property
    def gating(self) -> bool:
        """Only missing/stale gate --strict; foreign and unauditable never do."""
        return bool(self.missing or self.stale)


def _expected_env(role: str, sources: RuntimeEnvSources) -> dict:
    """The contract payload used as the stale reference for *role*."""
    if role == "system":
        # Per-system fields are not stale-checked; dummies satisfy build_env.
        return build_env(role, sources, system_id="x", system_name="x",
                         schema="x", connection_type="x",
                         system_agent_model="x")
    return build_env(role, sources)


def _posture(role: str, env: dict) -> dict:
    """Informational posture facts for *role* — closed-vocabulary labels only
    (never a raw value). ``MEMORY_LTM_SCOPE`` (orchestrator): which LTM
    namespaces chat retrieves; the workshop profile writes ``session``,
    absent means the ``actor`` default."""
    if role != "orchestrator":
        return {}
    raw = (env.get("MEMORY_LTM_SCOPE") or "").strip().lower()
    if not raw:
        label = f"{MEMORY_LTM_SCOPES[0]} (default)"
    else:
        label = raw if raw in MEMORY_LTM_SCOPES else "unrecognised"
    return {"MEMORY_LTM_SCOPE": label}


def audit_runtime_env(runtime_name: str, env: dict,
                      sources: RuntimeEnvSources) -> RuntimeFinding | None:
    """Classify one runtime's live env. Pure — unit-testable offline."""
    role = classify_role(runtime_name)
    if role is None:
        return None
    finding = RuntimeFinding(runtime_name=runtime_name, role=role)
    for key in sorted(REQUIRED_KEYS[role]):
        # Only fill-policy keys must always be present; overwrite-policy keys
        # (SYSTEM_IDENTITY_CLAIM_SECRET_ARN) are legitimately empty —
        # empty ⇒ allow-all (ADR-0049) — and never alarm.
        if REQUIRED_KEYS[role][key] != FILL_IF_ABSENT:
            continue
        if not (env.get(key) or "").strip():
            finding.missing.append(key)
    # ADR-0069: the boot gate's required set is the contract's ONE definition
    # (`missing_required`); an ABSENT key used to audit `ok` because the stale
    # check only compares keys that are present (measured live on a pre-0069
    # onboarding runtime). Report exactly what the boot gate would fail on.
    for key in missing_required(role, env):
        if key not in finding.missing:
            finding.missing.append(key)
    finding.missing.sort()
    expected = _expected_env(role, sources)
    for key in sorted(STALE_CHECKED_KEYS[role]):
        want = expected.get(key, "")
        if not want:
            continue  # unset expectation never alarms
        have = env.get(key, "")
        if have and have != want:
            finding.stale.append((key, _digest(want), _digest(have)))
    allowed = known_keys(role)
    finding.foreign = sorted(k for k in env if k not in allowed)
    finding.posture = _posture(role, env)
    return finding


def audit_all(runtimes: list, get_env, sources: RuntimeEnvSources) -> list:
    """Audit every ADT runtime. ``get_env(runtime)`` returns its live env
    dict (injected so unit tests need no control client).

    ``get_env`` reaches a live AWS API, so it can fail PER RUNTIME for
    reasons that have nothing to do with drift — a transient
    AgentCredentialProvider discovery-document fetch failure was observed
    killing the whole lane. One unreadable runtime must never destroy the
    report for the other ten, so the failure is isolated and surfaced as
    ``unauditable`` rather than raised. It is NOT counted as clean: the
    reason is printed, and ``--strict`` intentionally does not gate on it
    so a transient AWS error cannot flap a pre-push gate (missing/stale
    drift remains the only gating class).
    """
    findings = []
    for runtime in runtimes:
        name = runtime.get("agentRuntimeName", "")
        if classify_role(name) is None:
            continue  # not an ADT runtime — skip before any API call
        try:
            env = get_env(runtime) or {}
        except Exception as exc:  # noqa: BLE001 - isolate one runtime's failure
            findings.append(RuntimeFinding(
                runtime_name=name, role=classify_role(name) or "",
                unauditable=type(exc).__name__))
            continue
        finding = audit_runtime_env(name, env, sources)
        if finding is not None:
            findings.append(finding)
    return findings


def render_report(findings: list) -> str:
    """Human-readable report — key names and digests only, never values."""
    lines = []
    for f in findings:
        if f.unauditable:
            lines.append(f"{f.runtime_name} [{f.role}] UNAUDITABLE "
                         f"({f.unauditable}) — env could not be read; "
                         f"drift unknown, not gated")
            continue
        status = "DRIFT" if f.gating else "ok"
        lines.append(f"{f.runtime_name} [{f.role}] {status}")
        for key in f.missing:
            lines.append(f"  missing: {key}")
        for key, want8, have8 in f.stale:
            lines.append(f"  stale:   {key} expected#{want8} != live#{have8}")
        for key in f.foreign:
            lines.append(f"  foreign: {key} (informational — operator/unknown key)")
        for key, label in sorted(f.posture.items()):
            lines.append(f"  posture: {key}={label} (informational)")
    gating = sum(1 for f in findings if f.gating)
    unreadable = sum(1 for f in findings if f.unauditable)
    tail = f"{len(findings)} runtime(s) audited; {gating} with missing/stale drift."
    if unreadable:
        tail += f" {unreadable} unauditable (drift unknown)."
    lines.append(tail)
    return "\n".join(lines)


#: Per-system secrets a login of the system's own lives in; any other pointer
#: is a database secret the deployment shares (admin_routes uses the same rule).
_OWN_SECRET_PREFIXES = ("adt/conn/", "adt/systems/")


def reader_login_annotation(runtime_name: str, env: dict, read_secret) -> dict | None:
    """The login one PostgreSQL system runtime connects with, by name only.

    ``read_secret(arn)`` returns the agent-config secret as a dict (injected:
    offline-testable). Statuses: ``reader`` (its schema's read-only login),
    ``deployment`` (the deployment's own database login: not yet moved),
    ``own`` (a login of the system's own), ``no-login``, ``no-agent-config``,
    ``unreadable`` (``detail`` is the exception type only). ``None`` for any
    runtime that is not a PostgreSQL system."""
    if classify_role(runtime_name) != "system" or env.get("CONNECTION_TYPE") != "PostgreSQL":
        return None
    from agents.demo.db_routing import READER_LOGIN_PREFIX
    note = {"system": env.get("SYSTEM_ID") or runtime_name, "login": "", "status": "", "detail": ""}
    arn = (env.get("CONNECTION_SECRET_ARN") or "").strip()
    if not arn:
        return {**note, "status": "no-agent-config"}
    try:
        config = read_secret(arn) or {}
    except Exception as exc:  # noqa: BLE001 - annotation only; the type, never the message
        return {**note, "status": "unreadable", "detail": type(exc).__name__}
    login = str(config.get("user") or config.get("username") or "")
    pointer = str(config.get("secret_arn") or "").split(":secret:", 1)[-1]
    if not login:
        status = "no-login"
    elif login == f"{READER_LOGIN_PREFIX}{env.get('SCHEMA', '')}":
        status = "reader"
    elif pointer.startswith(_OWN_SECRET_PREFIXES):
        status = "own"
    else:
        status = "deployment"
    return {**note, "login": login, "status": status}


_READER_STATUS_TEXT = {
    "reader": "its schema's read-only login",
    "deployment": "the deployment's database login; run the one-time upgrade step "
                  "(docs/deployment.md, 'Upgrading an existing deployment')",
    "own": "its own login (kept as registered)",
    "no-login": "no login in its agent-config",
    "no-agent-config": "no agent-config secret",
    "unreadable": "agent-config unreadable",
}


def render_reader_logins(notes: list) -> str:
    """The ``--reader-logins`` section: system id, login name and status only."""
    lines = ["system database logins (annotation only)"]
    for n in notes:
        detail = f" ({n['detail']})" if n.get("detail") else ""
        login = f" login={n['login']}" if n.get("login") else ""
        lines.append(f"  {n['system']}{login}: {_READER_STATUS_TEXT[n['status']]}{detail}")
    pending = sum(1 for n in notes if n["status"] == "deployment")
    lines.append(f"{len(notes)} PostgreSQL system(s); {pending} still on the deployment's login.")
    return "\n".join(lines)


#: System runtimes whose per-question checks read the baked table list.
_BAKED_SCHEMA_TYPES = ("PostgreSQL", "Athena")


def baked_schema_annotation(runtime_name: str, env: dict, read_secret) -> dict | None:
    """Whether one SQL or Athena system runtime starts with a baked table list.

    The runtime reads it from ``TABLES_DESC`` or, when that is empty, from its
    agent-config secret's ``tables_desc`` (``runtime_app``), so the same order
    is read here; ``read_secret(arn)`` returns that secret as a dict
    (injected: offline-testable). Statuses: ``baked``, ``no-tables-desc``
    (the state-time and storage-bound checks are inactive on it),
    ``no-agent-config``, ``unreadable`` (``detail`` is the exception type
    only). ``None`` for any runtime that is not such a system. No value is
    ever returned."""
    if (classify_role(runtime_name) != "system"
            or env.get("CONNECTION_TYPE") not in _BAKED_SCHEMA_TYPES):
        return None
    note = {"system": env.get("SYSTEM_ID") or runtime_name, "status": "", "detail": ""}
    if (env.get("TABLES_DESC") or "").strip():
        return {**note, "status": "baked"}
    arn = (env.get("CONNECTION_SECRET_ARN") or "").strip()
    if not arn:
        return {**note, "status": "no-agent-config"}
    try:
        config = read_secret(arn) or {}
    except Exception as exc:  # noqa: BLE001 - annotation only; the type, never the message
        return {**note, "status": "unreadable", "detail": type(exc).__name__}
    baked = isinstance(config, dict) and str(config.get("tables_desc") or "").strip()
    return {**note, "status": "baked" if baked else "no-tables-desc"}


_BAKED_STATUS_TEXT = {
    "baked": "baked table list present",
    "no-tables-desc": "no baked table list; its state-time and storage-bound checks are "
                      "inactive until the system is re-onboarded",
    "no-agent-config": "no agent-config secret",
    "unreadable": "agent-config unreadable",
}


def render_baked_schemas(notes: list) -> str:
    """The baked table list section: system id and status only."""
    lines = ["system baked table lists (annotation only)"]
    for n in notes:
        detail = f" ({n['detail']})" if n.get("detail") else ""
        lines.append(f"  {n['system']}: {_BAKED_STATUS_TEXT[n['status']]}{detail}")
    absent = sum(1 for n in notes if n["status"] == "no-tables-desc")
    lines.append(f"{len(notes)} SQL or Athena system(s); {absent} with no baked table list.")
    return "\n".join(lines)


def _secret_reader(boto3, region: str):
    """``read_secret(arn)`` over one lazily created Secrets Manager client."""
    import json
    clients: list = []

    def read_secret(arn: str) -> dict:
        if not clients:
            clients.append(boto3.client("secretsmanager", region_name=region))
        return json.loads(clients[0].get_secret_value(SecretId=arn).get("SecretString") or "{}")

    return read_secret


def _baked_schemas_section(boto3, region: str, envs: dict) -> str:
    """The baked table list section of every run, ``""`` when no runtime is
    an SQL or Athena system."""
    read_secret = _secret_reader(boto3, region)
    notes = [baked_schema_annotation(name, env, read_secret) for name, env in sorted(envs.items())]
    notes = [n for n in notes if n is not None]
    return render_baked_schemas(notes) if notes else ""


def fetch_gate_health(base_url: str, username: str, password: str,
                      timeout: int = 30) -> dict:
    """Login and read the admin diagnostics GET with the shared lane client."""
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from scripts.smoke_prompts import App  # noqa: E402 - shared lane helper
    app = App(base_url)
    login = app.call("POST", "/api/auth/login",
                     {"email": username, "password": password}, timeout=timeout)
    if login.get("status") != "ok":
        raise RuntimeError("login failed")
    return app.call("GET", "/api/admin/agents/gate-health", timeout=timeout)


def render_gate_health(payload: dict) -> str:
    """Human annotation; optional measurements remain absent, never zero-filled."""
    admission = payload.get("admission") or {}
    owner = admission.get("current_owner_kind")
    first = (f"admission={admission.get('state', 'unknown')}"
             + (f" owner={owner}" if owner else "")
             + f" outstanding={admission.get('tokens_outstanding', 'absent')}")
    lines = ["current-replica gate health (annotation-only)", f"  {first}"]
    for key in ("briefings_admitted", "briefings_skipped_busy", "last_release_at"):
        if key in admission:
            lines.append(f"  {key}={admission[key]}")
    leak = str(bool(admission.get("leak_signal"))).lower()
    oldest = admission.get("oldest_token_age_seconds", "absent")
    threshold = admission.get("leak_threshold_seconds", "absent")
    lines.append(f"  LEAK signal={leak} oldest_seconds={oldest} threshold_seconds={threshold}")
    lines.append(f"  card_live_mode={payload.get('card_live_mode', 'absent')}")
    lines.append(f"  ask_stream_queue_bound={payload.get('ask_stream_queue_bound', 'absent')}")
    return "\n".join(lines)


def render_gate_health_error(exc: Exception) -> str:
    """Credential-safe annotation for the optional gate-health source."""
    code = getattr(exc, "code", None)
    kind = f"HTTP status={code}" if isinstance(code, int) else type(exc).__name__
    return f"gate-health unavailable ({kind}) — annotation only"


def _reader_logins_section(boto3, region: str, envs: dict) -> str:
    """``--reader-logins``: one annotation per PostgreSQL system runtime."""
    import json
    secrets = boto3.client("secretsmanager", region_name=region)

    def read_secret(arn: str) -> dict:
        return json.loads(secrets.get_secret_value(SecretId=arn).get("SecretString") or "{}")

    notes = [reader_login_annotation(name, env, read_secret) for name, env in sorted(envs.items())]
    return render_reader_logins([n for n in notes if n is not None])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", default=None, help="AWS Region (default: ambient)")
    parser.add_argument("--base-url", default=os.environ.get("ADT_BASE_URL", ""),
                        help="deployed app URL for current-replica gate facts")
    parser.add_argument("--username", default=os.environ.get("ADT_USERNAME", "ADT"))
    parser.add_argument("--password", default=os.environ.get("ADT_PASSWORD", ""))
    parser.add_argument("--timeout", type=int, default=30,
                        help="gate-health HTTP timeout in seconds")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 on any missing/stale finding (foreign never gates)")
    parser.add_argument("--reader-logins", action="store_true",
                        help="annotate each PostgreSQL system's database login by name "
                             "(reads adt/agent-config/*; never gates)")
    args = parser.parse_args(argv)
    if args.base_url and not args.password:
        print("ERROR: --password (or ADT_PASSWORD) required with --base-url")
        return 2

    import boto3  # deferred: the diff logic above stays offline-testable
    sources = RuntimeEnvSources.from_ambient(
        **({"region": args.region} if args.region else {}))
    control = boto3.client("bedrock-agentcore-control", region_name=sources.region)
    runtimes = list_all_agent_runtimes(control)
    envs: dict = {}

    def get_env(runtime) -> dict:
        runtime_id = (runtime.get("agentRuntimeId")
                      or runtime.get("agentRuntimeArn", "").rsplit("/", 1)[-1])
        details = control.get_agent_runtime(agentRuntimeId=runtime_id)
        envs[runtime.get("agentRuntimeName", "")] = details.get("environmentVariables") or {}
        return envs[runtime.get("agentRuntimeName", "")]

    findings = audit_all(runtimes, get_env, sources)
    print(render_report(findings))
    baked = _baked_schemas_section(boto3, sources.region, envs)
    if baked:
        print("\n" + baked)
    if args.reader_logins:
        print("\n" + _reader_logins_section(boto3, sources.region, envs))
    if args.base_url:
        try:
            payload = fetch_gate_health(
                args.base_url, args.username, args.password, args.timeout)
            print("\n" + render_gate_health(payload))
        except Exception as exc:  # noqa: BLE001 - annotation source never gates
            print("\n" + render_gate_health_error(exc))
    if args.strict and any(f.gating for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
