# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""_reconcile_query_gateway_lib.py — detect & heal STALE query-gateway target ARNs.

This module is the reusable, importable, offline-unit-testable core behind
``scripts/reconcile_query_gateway.py``. Like ``_reonboard_lib.py`` it knows
nothing about any specific system id, dataset, model, or connection type — it
operates purely on whatever the AgentCore control-plane client (which is
*injected* into every function so unit tests can stub it and no live AWS is
required) reports.

Why it exists
-------------
Each per-system agent runs as its own AgentCore *runtime*; the orchestrator
reaches it through a *target* on the QUERY AgentCore Gateway. A target's MCP
endpoint URL embeds the runtime's ARN (see ``AgentManager._runtime_mcp_url``).

When a runtime is recreated (manual re-provision, ``reonboard.py``, or any path
that deletes+recreates the ``adt_<id>`` runtime) it gets a NEW id — and therefore
a NEW ARN. ``deploy.sh`` re-syncs the ONBOARDING gateway target, but nothing
re-syncs the QUERY gateway targets, so they can keep pointing at a STALE/deleted
runtime ARN. The live symptom is::

    No endpoint or agent found with qualifier 'DEFAULT'

for exactly the systems whose runtime id changed (a system whose target happened
to already be current keeps working, which is why the failure looks selective).

What this does
--------------
For each MCP target on the query gateway, it compares the runtime ARN embedded
in the target's endpoint URL against the CURRENT ARN of the matching
``adt_<system>`` runtime (matched by the same naming convention
``_reonboard_lib``/``AgentManager`` use) and, when drifted, calls
``update_gateway_target`` to repoint the endpoint at the current ARN —
preserving the target's name / description / OAuth credential configuration.

Contract
--------
* **Idempotent** — a target already pointing at the current ARN is left
  untouched; a second run is a no-op.
* **Fail-loud** — a target that embeds a runtime ARN but whose backing
  ``adt_<id>`` runtime does not exist raises :class:`RuntimeMissingForTargetError`
  (the deploy/CI caller should surface this, not silently mask a broken gateway).
* **Injected client** — ``control`` is dependency-injected (never constructed
  here); the CLI wrapper builds the real boto3 client.
"""
from __future__ import annotations

import logging
from urllib.parse import unquote

# Reuse the single-source-of-truth naming so the runtime we look up is exactly
# the one AgentManager/_reonboard_lib create (DRY — can never drift).
from _reonboard_lib import runtime_name

logger = logging.getLogger(__name__)

# Markers delimiting the (URL-encoded) runtime ARN inside an MCP invocation URL,
# per AgentManager._runtime_mcp_url:
#   https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<ENC_ARN>/invocations?qualifier=DEFAULT
_RUNTIMES_MARKER = "/runtimes/"
_INVOCATIONS_MARKER = "/invocations"


# --------------------------------------------------------------------------- #
# Typed errors — everything propagates (no silent fallback / no fake data).
# --------------------------------------------------------------------------- #
class QueryGatewayReconcileError(RuntimeError):
    """Base class for all query-gateway reconcile failures."""


class RuntimeMissingForTargetError(QueryGatewayReconcileError):
    """A target embeds a runtime ARN but no matching ``adt_<id>`` runtime exists.

    This is an unexpected, unhealable state (there is no live runtime to repoint
    at), so it is raised rather than masked."""

    def __init__(self, target_nm: str, rt_name: str, stale_arn: str):
        self.target_name = target_nm
        self.runtime_name = rt_name
        self.stale_arn = stale_arn
        super().__init__(
            f"query-gateway target '{target_nm}' points at runtime ARN '{stale_arn}' "
            f"but no live runtime named '{rt_name}' exists — cannot reconcile "
            f"(re-onboard the system to recreate its runtime)"
        )


# --------------------------------------------------------------------------- #
# URL <-> ARN helpers (mirror AgentManager._runtime_mcp_url — single format).
# --------------------------------------------------------------------------- #
def mcp_url_for_arn(runtime_arn: str, region: str) -> str:
    """Derive the MCP invocation URL for *runtime_arn* — mirrors ``_runtime_mcp_url``.

    Kept byte-for-byte identical to ``AgentManager._runtime_mcp_url`` (URL-encoded
    ARN + ``/invocations?qualifier=DEFAULT``) so a healed target matches exactly
    what the in-app create path would have written.
    """
    encoded_arn = runtime_arn.replace(":", "%3A").replace("/", "%2F")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{encoded_arn}/invocations?qualifier=DEFAULT"
    )


def extract_runtime_arn(endpoint: str | None) -> str | None:
    """Return the runtime ARN embedded in an MCP endpoint URL, or ``None``.

    ``None`` means *endpoint* is not an AgentCore runtime invocation URL (e.g. a
    Lambda/OpenAPI builtin target, or an empty/edge value) — such targets are not
    per-system runtime targets and are skipped by the reconciler.
    """
    if not endpoint or _RUNTIMES_MARKER not in endpoint or _INVOCATIONS_MARKER not in endpoint:
        return None
    after = endpoint.split(_RUNTIMES_MARKER, 1)[1]
    encoded = after.split(_INVOCATIONS_MARKER, 1)[0].strip("/")
    arn = unquote(encoded)
    if not arn.startswith("arn:") or ":runtime/" not in arn:
        return None
    return arn


def _target_endpoint(detail: dict) -> str | None:
    """Best-effort pull of ``targetConfiguration.mcp.mcpServer.endpoint`` (tolerant)."""
    cfg = detail.get("targetConfiguration") or {}
    mcp = cfg.get("mcp") or {}
    server = mcp.get("mcpServer") or {}
    ep = server.get("endpoint")
    return ep if isinstance(ep, str) else None


def _runtime_arn_map(control) -> dict[str, str]:
    """Return ``{agentRuntimeName: agentRuntimeArn}`` across ALL pages.

    Tolerant of the two response-shape variants used elsewhere in the codebase
    (``agentRuntimeSummaries`` | ``agentRuntimes``). Paginates: once legacy and
    renamed runtimes coexist the fleet exceeds one page, and a single call
    silently drops runtimes (observed post vf->adt rename).
    """
    out: dict[str, str] = {}
    token = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        resp = control.list_agent_runtimes(**kwargs)
        items = resp.get("agentRuntimeSummaries", resp.get("agentRuntimes", []))
        for rt in items:
            name = rt.get("agentRuntimeName", "")
            arn = rt.get("agentRuntimeArn", "")
            if name and arn:
                out[name] = arn
        token = resp.get("nextToken")
        if not token:
            return out


def _list_targets(control, gateway_id: str) -> list[dict]:
    """Return all gateway target summaries (paginated), tolerant of shape variants."""
    out: list[dict] = []
    next_token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = control.list_gateway_targets(**kwargs)
        out.extend(resp.get("items", resp.get("targets", [])))
        next_token = resp.get("nextToken")
        if not next_token:
            return out


# --------------------------------------------------------------------------- #
# Reconcile
# --------------------------------------------------------------------------- #
def reconcile_query_gateway(
    control,
    gateway_id: str,
    region: str,
    *,
    apply: bool = True,
) -> dict:
    """Detect &, when *apply*, heal query-gateway targets with STALE runtime ARNs.

    For every target on *gateway_id* whose endpoint embeds a runtime ARN, compare
    that ARN to the CURRENT ARN of the matching ``adt_<system>`` runtime. If they
    differ, the target is drifted; when *apply* is True, repoint it with
    ``update_gateway_target`` (preserving name / description / credential config).

    Args:
        control: injected ``bedrock-agentcore-control`` client (or a stub).
        gateway_id: the QUERY gateway identifier.
        region: AWS region — used to rebuild the endpoint URL.
        apply: when False, detect and report only (dry-run); no writes.

    Returns:
        A report dict::

            {"checked": int, "in_sync": int, "drifted": int,
             "updated": int, "skipped": int,
             "changes": [{"target","system","stale_arn","current_arn"}, ...]}

    Raises:
        RuntimeMissingForTargetError: a target embeds a runtime ARN but no live
            ``adt_<id>`` runtime exists to repoint it at (fail-loud).
    """
    if not gateway_id:
        raise QueryGatewayReconcileError("gateway_id is required")

    rt_arns = _runtime_arn_map(control)
    summaries = _list_targets(control, gateway_id)

    report = {
        "checked": 0,
        "in_sync": 0,
        "drifted": 0,
        "updated": 0,
        "skipped": 0,
        "changes": [],
    }

    for summary in summaries:
        target_id = summary.get("targetId") or summary.get("id") or ""
        target_nm = summary.get("name", "")
        if not target_id:
            report["skipped"] += 1
            continue

        # Full detail carries the targetConfiguration + credential config we must
        # preserve on update; the list summary may omit them.
        detail = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        target_nm = detail.get("name", target_nm)
        endpoint = _target_endpoint(detail)
        current_in_target = extract_runtime_arn(endpoint)
        if current_in_target is None:
            # Not a per-system runtime target (builtin lambda/openapi, or no MCP
            # runtime endpoint) — nothing for the reconciler to do.
            report["skipped"] += 1
            logger.debug("skip target '%s' — not an AgentCore runtime endpoint", target_nm)
            continue

        report["checked"] += 1

        # The runtime a target should point at is deterministic from its name
        # (target name == system_id with '_'->'-'; runtime_name folds both to '_').
        rt_name = runtime_name(target_nm)
        live_arn = rt_arns.get(rt_name)
        if not live_arn:
            raise RuntimeMissingForTargetError(target_nm, rt_name, current_in_target)

        if current_in_target == live_arn:
            report["in_sync"] += 1
            logger.info("target '%s' already current (%s)", target_nm, rt_name)
            continue

        # Drifted — the embedded ARN is stale relative to the live runtime.
        report["drifted"] += 1
        report["changes"].append(
            {
                "target": target_nm,
                "system": rt_name,
                "stale_arn": current_in_target,
                "current_arn": live_arn,
            }
        )
        logger.warning(
            "DRIFT target '%s' (%s): %s -> %s", target_nm, rt_name, current_in_target, live_arn
        )
        if not apply:
            continue

        # Rebuild the endpoint at the current ARN, preserving everything else.
        new_cfg = {"mcp": {"mcpServer": {"endpoint": mcp_url_for_arn(live_arn, region)}}}
        update_kwargs = {
            "gatewayIdentifier": gateway_id,
            "targetId": target_id,
            "name": target_nm,
            "targetConfiguration": new_cfg,
        }
        if detail.get("description"):
            update_kwargs["description"] = detail["description"]
        if detail.get("credentialProviderConfigurations"):
            update_kwargs["credentialProviderConfigurations"] = detail[
                "credentialProviderConfigurations"
            ]
        if detail.get("metadataConfiguration"):
            update_kwargs["metadataConfiguration"] = detail["metadataConfiguration"]

        control.update_gateway_target(**update_kwargs)
        report["updated"] += 1
        logger.warning("HEALED target '%s' -> %s", target_nm, live_arn)

    return report
