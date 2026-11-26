#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""set_query_mode.py — deterministic SYSTEM_QUERY_MODE (Mode A/B) toggle.

``SYSTEM_QUERY_MODE`` (ADR-0035 A/B toggle) selects Mode A (``agent`` — one
natural-language ``query_<id>`` tool per system) vs Mode B (``orchestrator`` —
generic ``describe_schema`` / ``run_query`` / ``connect`` tools). It is read at
runtime by ``agents/system_agent.py::create_mcp_server`` and must be set
CONSISTENTLY on the orchestrator runtime, every per-system runtime, the query
gateway targets, and the ECS app task — otherwise the stack is in a split state.

Why RECREATE (not in-place update) — empirical, from live debugging
-------------------------------------------------------------------
An in-place ``update_agent_runtime`` env flip followed by a gateway target sync
FAILS deterministically for VPC runtimes: the gateway's MCP initialize against
the updated (idle) runtime cold-starts ~130s, which exceeds the gateway's fixed
40s sync timeout, so the target lands in SYNCHRONIZE_UNSUCCESSFUL/FAILED
forever (warm instances are session-bound; pre-warming does not help; the
gateway's background retries against a failing target churn the runtime).
FRESHLY CREATED runtimes + immediately created targets sync RELIABLY (every
onboarding provision does create-runtime → create-target and goes READY fast).
Each per-system flip therefore captures the runtime's full config + its gateway
target's config, deletes both, and re-creates both with only
``SYSTEM_QUERY_MODE`` changed — no stack redeploy needed.

This tool flips the whole stack to ONE target mode, in order:

  1. Validate the mode arg (exactly ``agent`` or ``orchestrator``, else exit 2).
  2. Orchestrator runtime (``adt_orchestrator``): env-preserving IN-PLACE update
     (idempotent) — the orchestrator has NO per-system gateway target of its
     own, so no target sync is involved and the update path is safe.
     (Skipped with ``--recreate-only``.)
  3. System runtimes (``--systems a,b,c`` or ``--all``): per system,
     ``_query_mode_lib.recreate_runtime_with_mode`` — delete target → delete
     runtime (wait absent) → create runtime with the SAME config but
     ``SYSTEM_QUERY_MODE=<mode>`` (wait READY, bounded single re-trigger on the
     stranded-create strand) → create the target with the same
     endpoint/credential config repointed at the NEW runtime ARN (wait READY).
     A system already at the mode with a READY target is SKIPPED (logged).
  4. Query gateway sweep: ``_reconcile_query_gateway_lib`` verifies every
     remaining target points at a live runtime ARN (the recreated targets are
     fresh and in-sync by construction; this catches pre-existing drift).
     (Skipped with ``--recreate-only``.)
  5. ECS app task (unless ``--skip-ecs`` / ``--recreate-only``): register a new
     task-def revision with the container's ``SYSTEM_QUERY_MODE`` replaced +
     force a new deployment.
  6. Print the QuickSight/Quick MCP action-connector cache REMINDER.

Everything logs; errors propagate (no silent fallback). Only explicit
``--skip-ecs`` / ``--recreate-only`` skip steps. The pure logic lives in
``_query_mode_lib.py``; boto3 client construction is kept here (out of the
testable core), mirroring how ``reonboard.py`` splits from ``_reonboard_lib.py``.

Usage
-----
  python3 scripts/set_query_mode.py <agent|orchestrator> --region <r> \
    [--profile <p>] [--gateway-id <gw> | env AGENTCORE_GATEWAY_ID] \
    [--systems a,b,c | --all] [--cluster <ecs-cluster>] [--service <ecs-service>] \
    [--skip-ecs] [--recreate-only] [--poll N] [--runtime-deadline N] \
    [--target-deadline N] [--delete-deadline N]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

# Allow sibling-module imports whether run as a script or imported by tests.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _query_mode_lib import (  # noqa: E402
    ORCHESTRATOR_RUNTIME_NAME,
    QUERY_MODE_ENV_KEY,
    InvalidModeError,
    list_runtime_summaries,
    recreate_runtime_with_mode,
    select_system_runtime_names,
    update_runtime_query_mode,
    validate_mode,
)
from _reconcile_query_gateway_lib import reconcile_query_gateway  # noqa: E402

logger = logging.getLogger("set_query_mode")


# --------------------------------------------------------------------------- #
# Runtime steps (control-plane client injected — unit-testable via the lib)
# --------------------------------------------------------------------------- #
def update_orchestrator(control, mode: str, *, log=logger.info) -> None:
    """Set SYSTEM_QUERY_MODE on the ``adt_orchestrator`` runtime (env-preserving).

    IN-PLACE update is safe (and preferred) here: the orchestrator runtime has
    no per-system gateway target, so the update never triggers a target
    re-synchronize — the failure mode that forces the recreate path for the
    per-system runtimes does not apply.
    """
    from _query_mode_lib import runtime_id_by_name

    rid = runtime_id_by_name(control, ORCHESTRATOR_RUNTIME_NAME)
    if not rid:
        # Fail loud: the orchestrator runtime must exist for the mode to matter.
        raise RuntimeError(
            f"orchestrator runtime {ORCHESTRATOR_RUNTIME_NAME!r} not found "
            f"(is the stack deployed in this region?)"
        )
    log(f"orchestrator: reconcile {QUERY_MODE_ENV_KEY}={mode} on {ORCHESTRATOR_RUNTIME_NAME} ({rid})")
    result = update_runtime_query_mode(control, rid, mode)
    if result.get("skipped"):
        log(f"orchestrator: already {mode} — no update (no restart)")


def recreate_system_runtimes(
    control,
    mode: str,
    system_ids: list[str],
    *,
    gateway_id: str,
    region: str,
    runtime_deadline_s: float = 600.0,
    target_deadline_s: float = 600.0,
    delete_deadline_s: float = 600.0,
    poll_s: float = 10.0,
    log=logger.info,
    **wait_kwargs,
) -> dict[str, dict]:
    """RECREATE each system's runtime + gateway target at *mode*; block on READY.

    Per system: ``_query_mode_lib.recreate_runtime_with_mode`` (delete target →
    delete runtime → create runtime with SYSTEM_QUERY_MODE=<mode> → create
    target repointed at the new ARN, waiting at every step). Systems already at
    the mode with a READY target are skipped. Returns ``{system_id: result}``;
    the first error propagates (systems are processed in the given order, so
    front-load a fragile one).
    """
    results: dict[str, dict] = {}
    for sid in system_ids:
        res = recreate_runtime_with_mode(
            control, gateway_id, sid, mode,
            region=region,
            runtime_deadline_s=runtime_deadline_s,
            target_deadline_s=target_deadline_s,
            delete_deadline_s=delete_deadline_s,
            poll_s=poll_s,
            log=log,
            **wait_kwargs,
        )
        if res.get("skipped"):
            log(f"system: {sid} already {mode} + target READY — skipped (no recreate)")
        else:
            log(
                f"system: {sid} RECREATED at {mode} "
                f"(runtime {res.get('runtime_arn', '?')}, "
                f"target {'recreated' if res.get('target_recreated') else 'absent — none recreated'})"
            )
        results[sid] = res
    return results


# --------------------------------------------------------------------------- #
# ECS step (task-def revision + force new deployment)
# --------------------------------------------------------------------------- #
def _discover_adt_service(ecs, *, log=logger.info):
    """Return (cluster_arn, service_arn) for the ADT app service, or raise if ambiguous.

    The ADT app service is the one whose task definition has a container carrying
    a ``SYSTEM_QUERY_MODE`` env var. Ambiguity (0 or >1 matches) is a hard error —
    the operator must pass --cluster/--service or run `cdk deploy` instead. No
    destructive guessing.
    """
    matches: list[tuple[str, str]] = []
    for cluster_arn in ecs.list_clusters().get("clusterArns", []):
        svc_arns = ecs.list_services(cluster=cluster_arn).get("serviceArns", [])
        if not svc_arns:
            continue
        for svc in ecs.describe_services(cluster=cluster_arn, services=svc_arns).get(
            "services", []
        ):
            td_arn = svc.get("taskDefinition", "")
            if not td_arn:
                continue
            td = ecs.describe_task_definition(taskDefinition=td_arn).get(
                "taskDefinition", {}
            )
            for c in td.get("containerDefinitions", []):
                if any(
                    e.get("name") == QUERY_MODE_ENV_KEY for e in c.get("environment", [])
                ):
                    matches.append((cluster_arn, svc.get("serviceArn", "")))
                    break
    if len(matches) != 1:
        raise RuntimeError(
            f"ECS app service discovery is ambiguous ({len(matches)} candidate(s) "
            f"with a {QUERY_MODE_ENV_KEY} container env). Pass --cluster/--service "
            f"explicitly, or run `cdk deploy -c system_query_mode=<mode>` instead."
        )
    return matches[0]


def update_ecs_service(
    ecs, mode: str, *, cluster: str = "", service: str = "", log=logger.info
) -> None:
    """Register a new task-def revision with SYSTEM_QUERY_MODE=<mode>, force deploy.

    Discovers the ADT service when --cluster/--service are not both given. Only the
    ``SYSTEM_QUERY_MODE`` env of the matching container(s) is changed; everything
    else in the task definition is preserved.
    """
    if not (cluster and service):
        cluster, service = _discover_adt_service(ecs, log=log)
    log(f"ecs: reconciling {QUERY_MODE_ENV_KEY}={mode} on cluster={cluster} service={service}")

    svc = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
    td_arn = svc["taskDefinition"]
    td = ecs.describe_task_definition(taskDefinition=td_arn)["taskDefinition"]

    # Rebuild the register_task_definition payload from the current revision,
    # replacing only the SYSTEM_QUERY_MODE env on the container(s) that carry it.
    containers = []
    for c in td.get("containerDefinitions", []):
        env = [e for e in c.get("environment", []) if e.get("name") != QUERY_MODE_ENV_KEY]
        if any(e.get("name") == QUERY_MODE_ENV_KEY for e in c.get("environment", [])):
            env.append({"name": QUERY_MODE_ENV_KEY, "value": mode})
        containers.append({**c, "environment": env})

    register_kwargs = {
        "family": td["family"],
        "containerDefinitions": containers,
    }
    # Carry forward the fields register_task_definition accepts (when present).
    for key in (
        "taskRoleArn",
        "executionRoleArn",
        "networkMode",
        "volumes",
        "placementConstraints",
        "requiresCompatibilities",
        "cpu",
        "memory",
        "runtimePlatform",
        "ephemeralStorage",
        "proxyConfiguration",
        "ipcMode",
        "pidMode",
    ):
        if td.get(key):
            register_kwargs[key] = td[key]

    new_td = ecs.register_task_definition(**register_kwargs)["taskDefinition"]
    new_td_arn = new_td["taskDefinitionArn"]
    log(f"ecs: registered {new_td_arn}")
    ecs.update_service(
        cluster=cluster,
        service=service,
        taskDefinition=new_td_arn,
        forceNewDeployment=True,
    )
    log("ecs: forced new deployment")


# --------------------------------------------------------------------------- #
# Orchestration (clients injected — CLI builds the real ones)
# --------------------------------------------------------------------------- #
def system_ids_to_recreate(control, system_ids: list[str] | None,
                           all_systems: bool) -> list[str]:
    """The system ids to recreate: with ``all_systems``, every ``adt_<id>``
    system runtime (infra runtimes excluded) across ALL ``list_agent_runtimes``
    pages — one call returns a single page, so ``--all`` would otherwise skip
    the systems on page 2; else the ids given, in order."""
    if not all_systems:
        return list(system_ids or [])
    names = select_system_runtime_names(
        [rt.get("agentRuntimeName", "") for rt in list_runtime_summaries(control)])
    # runtime_name folds '-'→'_' so the stripped id round-trips through both
    # runtime_name() and target_name() (single-source naming convention).
    return [n[len("adt_"):] for n in names]


_QUICK_REMINDER = (
    "REMINDER: QuickSight / Quick MCP action-connectors CACHE their tool list. "
    "After a mode flip the tool names change (Mode A: query_<id>; Mode B: "
    "run_query/describe_schema/connect), so any existing action-connector MUST be "
    "DELETED and RECREATED to pick up the new tool names — a refresh is not enough."
)


def run(
    control,
    ecs,
    mode: str,
    *,
    gateway_id: str,
    region: str,
    system_ids: list[str] | None,
    all_systems: bool,
    skip_ecs: bool,
    recreate_only: bool = False,
    cluster: str = "",
    service: str = "",
    runtime_deadline_s: float = 600.0,
    target_deadline_s: float = 600.0,
    delete_deadline_s: float = 180.0,
    poll_s: float = 10.0,
    log=logger.info,
    **wait_kwargs,
) -> dict:
    """Reconcile SYSTEM_QUERY_MODE across the stack; returns a small report dict.

    ``control`` (bedrock-agentcore-control) and ``ecs`` are dependency-injected.
    Errors propagate. Steps run in the documented order. ``recreate_only``
    restricts the run to the per-system recreate step (no orchestrator update,
    no gateway sweep, no ECS).
    """
    validate_mode(mode)
    if not gateway_id:
        raise RuntimeError("gateway_id is required (--gateway-id or AGENTCORE_GATEWAY_ID)")

    report: dict = {"mode": mode}

    # 2. Orchestrator runtime — in-place update (no gateway target involved).
    if recreate_only:
        log("orchestrator: skipped (--recreate-only)")
        report["orchestrator"] = "skipped"
    else:
        update_orchestrator(control, mode, log=log)
        report["orchestrator"] = "reconciled"

    # 3. System runtimes — determine target system ids, then RECREATE each
    #    (runtime + gateway target), blocking on READY per system.
    ids = system_ids_to_recreate(control, system_ids, all_systems)
    log(f"system runtimes to recreate at mode={mode} ({len(ids)}): {', '.join(ids) or 'none'}")
    results = recreate_system_runtimes(
        control, mode, ids,
        gateway_id=gateway_id,
        region=region,
        runtime_deadline_s=runtime_deadline_s,
        target_deadline_s=target_deadline_s,
        delete_deadline_s=delete_deadline_s,
        poll_s=poll_s,
        log=log,
        **wait_kwargs,
    )
    report["systems"] = {
        sid: ("skipped" if res.get("skipped") else "recreated")
        for sid, res in results.items()
    }

    # 4. Query gateway sweep — the recreated targets are fresh/in-sync by
    #    construction; this verifies-and-heals any PRE-EXISTING drift on other
    #    targets (idempotent; in-sync targets are untouched).
    if recreate_only:
        log("gateway sweep: skipped (--recreate-only)")
        report["gateway"] = "skipped"
    else:
        log(f"reconciling query gateway targets on {gateway_id}")
        gw_report = reconcile_query_gateway(control, gateway_id, region)
        report["gateway"] = gw_report

    # 5. ECS app task.
    if skip_ecs or recreate_only:
        log(f"ecs: skipped ({'--recreate-only' if recreate_only else '--skip-ecs'})")
        report["ecs"] = "skipped"
    else:
        update_ecs_service(ecs, mode, cluster=cluster, service=service, log=log)
        report["ecs"] = "updated"

    # 6. Reminder.
    log(_QUICK_REMINDER)
    return report


# --------------------------------------------------------------------------- #
# CLI wiring (boto3 / argparse) — kept out of the testable core.
# --------------------------------------------------------------------------- #
def _session(profile: str, region: str):
    import boto3

    return (
        boto3.Session(profile_name=profile, region_name=region)
        if profile
        else boto3.Session(region_name=region)
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Reconcile SYSTEM_QUERY_MODE across the ADT stack (ADR-0035 A/B toggle)"
    )
    ap.add_argument("mode", help="target query mode: 'agent' (Mode A) or 'orchestrator' (Mode B)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    ap.add_argument("--gateway-id", default=os.environ.get("AGENTCORE_GATEWAY_ID", ""))
    ap.add_argument(
        "--systems",
        default="",
        help="comma-separated system ids to update (mutually exclusive with --all)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="update every adt_* system runtime (excludes orchestrator/onboarding/digital-thread)",
    )
    ap.add_argument("--cluster", default="", help="ECS cluster (else auto-discover the ADT service)")
    ap.add_argument("--service", default="", help="ECS service (else auto-discover the ADT service)")
    ap.add_argument("--skip-ecs", action="store_true", help="do not touch the ECS app task")
    ap.add_argument(
        "--recreate-only",
        action="store_true",
        help="only recreate the per-system runtimes+targets (skip orchestrator, "
             "gateway sweep, and ECS)",
    )
    ap.add_argument("--poll", type=float, default=10.0)
    ap.add_argument("--runtime-deadline", type=float, default=600.0)
    ap.add_argument("--target-deadline", type=float, default=600.0)
    ap.add_argument(
        "--delete-deadline", type=float, default=600.0,
        help="seconds to wait for a runtime/target to fully delete before "
             "recreate (VPC-mode teardown can run for minutes; matches "
             "reonboard.py's 600s delete deadline — the same delete path)",
    )
    return ap


def _system_ids_or_error(args):
    """Resolve ``(system_ids, gateway_id)`` from parsed args, or ``None`` after
    logging the usage error (``main`` returns 2)."""
    if bool(args.systems.strip()) == bool(args.all):
        logger.error("pass exactly one of --systems <a,b,c> or --all")
        return None
    if not args.gateway_id:
        logger.error("--gateway-id (or AGENTCORE_GATEWAY_ID) is required")
        return None
    system_ids = (
        None
        if args.all
        else [s.strip() for s in args.systems.split(",") if s.strip()]
    )
    if system_ids is not None and not system_ids:
        logger.error("no systems given")
        return None
    return system_ids, args.gateway_id


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        validate_mode(args.mode)
    except InvalidModeError as exc:
        logger.error(str(exc))
        return 2

    resolved = _system_ids_or_error(args)
    if resolved is None:
        return 2
    system_ids, gateway_id = resolved

    session = _session(args.profile, args.region)
    control = session.client("bedrock-agentcore-control")
    ecs = session.client("ecs")

    try:
        run(
            control,
            ecs,
            args.mode,
            gateway_id=gateway_id,
            region=args.region,
            system_ids=system_ids,
            all_systems=args.all,
            skip_ecs=args.skip_ecs,
            recreate_only=args.recreate_only,
            cluster=args.cluster,
            service=args.service,
            runtime_deadline_s=args.runtime_deadline,
            target_deadline_s=args.target_deadline,
            delete_deadline_s=args.delete_deadline,
            poll_s=args.poll,
        )
    except Exception as exc:  # noqa: BLE001 — report and fail non-zero; do not mask
        logger.error(f"set-query-mode FAILED: {type(exc).__name__}: {exc}")
        return 1

    logger.info(f"set-query-mode complete — stack reconciled to '{args.mode}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
