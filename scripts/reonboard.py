#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""reonboard.py — durable, generic re-onboard driver that waits for runtime READY.

This is the SUPPORTED re-onboard path. Unlike ``onboarding_smoketest.py`` (which
intentionally polls ``catalog=onboarded`` — a fast "usable enough" signal), this
driver BLOCKS until BOTH the AgentCore **runtime** AND the gateway **target** for
each system are truly ``READY``. That closes the gap where re-onboard tooling
reports "done" while the freshly recreated runtime is still stranded.

What it does, per system, in the order given
--------------------------------------------
  0. Derive the connection RECORDS-FIRST (ADR-0044 Decision 3): read the
     persisted record server-side via the admin ``describe_connection`` route
     (non-secret host/port/dbname/search_path + the secret ARN pointer), fetch
     the credential VALUE operator-side via the caller's own AWS creds, and
     compose the DSN. Only when the record is absent/unconfigured (e.g. a wiped
     node) does it fall back PER-SYSTEM to the env-derived inputs, with a LOUD
     "fallback to env" log line — the local shell env cannot silently
     override a deployed system's persisted connection. ``--from-env`` forces
     the pure-env path for bootstrap scenarios.
  1. Delete the system's gateway target and its ``adt_<id>`` runtime; wait for
     both to fully disappear (a recreate racing a half-deleted runtime is the
     ConflictException strand we are avoiding).
  2. Re-run the ordinary ``discover → map → resolve → provision`` onboarding
     pipeline through the SAME REST API a UI user drives (reusing
     ``onboarding_smoketest`` helpers — no duplication, no special-case path).
  3. BLOCK until the runtime is READY. If the runtime strands in a terminal
     ``CREATE_FAILED`` / ``UPDATE_FAILED`` (e.g. the AZ-strip self-heal in
     ``agent_manager._start_cloud`` raced a ConflictException), delete it and
     re-trigger the create exactly ONCE (bounded), then continue waiting. A
     second terminal failure propagates — no infinite loop, no silent fallback.
     When the READY deadline expires with the runtime still PENDING (observed
     twice live: the SERVER completes provisioning minutes after the client
     deadline), enter a GRACE phase instead of failing: keep polling every 30s
     for up to ``--grace`` (default 600s) more, and fail only if that also
     expires. The exit log distinguishes "completed in grace" from a hard timeout.
  4. BLOCK until the gateway target is READY (same grace phase, symmetric).

After every system is READY (schemas fully rewritten to the graph), run a
best-effort ``reconcile-blocked`` sweep to purge any proposals logged mid-
re-onboard against a transiently-unavailable schema (they would otherwise linger
as "blocked" review-queue items). The sweep is advisory — a failure is logged and
never fails the re-onboard.

Everything is GENERIC: it operates on whatever system ids are passed via
``--systems`` (in that exact order, so the operator can front-load the
AZ-strip-eating system) or discovered from the live catalog via ``--all`` (a
deterministic sorted order). There are no system-id / dataset / model literals.

Usage
-----
  python3 scripts/reonboard.py \
    --base-url https://<dist>.cloudfront.net \
    --systems <a>,<b>,<c>            # OR --all (from the catalog)
    --gateway-id <gw-id>            # or env AGENTCORE_GATEWAY_ID
    --region <r> [--profile <p>]

The demo password is read from ``--password`` / ``$ADT_PASSWORD`` or, failing
that, the AWS Secrets Manager secret ``adt-demo-password``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from urllib.parse import quote

# Allow sibling-module imports whether run as a script or imported by tests.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _reonboard_lib import (  # noqa: E402
    ReonboardError,
    RuntimeCreateFailedError,
    RuntimeReadyTimeoutError,
    TargetReadyTimeoutError,
    delete_runtime,
    delete_target,
    target_name,
    wait_runtimes_ready,
    wait_targets_ready,
)
from onboarding_smoketest import (  # noqa: E402  (reuse — do NOT duplicate)
    App,
    connection_inputs,
    discover_and_map,
    provision,
    resolve,
    supply_secret_out_of_band,
)

# Secret NAME (not a value) documented in README — the demo password store.
DEMO_PASSWORD_SECRET_ID = "adt-demo-password"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture  # nosemgrep: hashicorp-tf-password

# The admin "current connection" read (admin_routes.py: APIRouter(prefix="/api/admin")
# + GET /connection/{system_id} → connection_resolver.describe_connection). This is
# the GUARANTEED server-side record read (ADR-0044 Decision 3): it serves ONLY
# non-secret fields (host/port/dbname/search_path) plus the secret ARN *pointer* —
# never a secret value.
DESCRIBE_CONNECTION_ROUTE = "/api/admin/connection/{system_id}"

logger = logging.getLogger("reonboard")


# --------------------------------------------------------------------------- #
# Records-first connection derivation (ADR-0044 Decision 3, after the cmms
# localhost incident: a laptop shell env must never silently override a
# deployed system's persisted connection).
# --------------------------------------------------------------------------- #
def _default_sm_client():
    """Lazy default Secrets Manager client (only reached when a usable record
    actually needs its credential fetched and no client was injected)."""
    import boto3
    return boto3.client("secretsmanager")


def _dsn_from_record(record: dict, secret: dict, schema: str) -> str:
    """Compose a PostgreSQL DSN from a persisted record + its fetched credential.

    Mirrors ``onboarding_smoketest._pg_dsn_from_env`` exactly in shape: quoted
    userinfo, ``options=-csearch_path%%3D<schema>``. Fail-closed on a missing
    password (returns ``""`` so the caller falls back LOUDLY rather than sending
    an unauthenticatable DSN to discover).
    """
    host = str(record.get("host") or "")
    port = str(record.get("port") or "5432")
    dbname = str(record.get("dbname") or "")
    search_path = str(record.get("search_path") or schema or "")
    user = str(secret.get("username") or secret.get("user") or "postgres")
    pw = str(secret.get("password") or "")
    if not (host and dbname and pw):
        return ""
    userinfo = quote(user, safe="") + ":" + quote(pw, safe="")
    dsn = f"postgresql://{userinfo}@{host}:{port}/{dbname}"
    if search_path:
        dsn += f"?options=-csearch_path%3D{search_path}"
    return dsn


def records_connection_inputs(app, systems, sm_client, env, *,
                              catalog_meta=None, fallback=connection_inputs,
                              log=logger.info) -> dict:
    """Derive per-system connection INPUTS from the PERSISTED records (records-first).

    For each system, read the persisted connection record server-side via the
    admin ``describe_connection`` route (non-secret ``host``/``port``/``dbname``/
    ``search_path`` + the ``secret_arn`` POINTER). For a usable PostgreSQL record
    (host + dbname + secret_arn), fetch the credential VALUE operator-side with
    the caller's OWN AWS credentials (``sm_client.get_secret_value``) and compose
    the DSN exactly as ``onboarding_smoketest._pg_dsn_from_env`` does. Non-SQL
    types keep their existing env derivation (their secret is supplied
    out-of-band, unchanged).

    Fallback is PER-SYSTEM and LOUD: when the record is absent/unconfigured
    (e.g. a node wiped by a graph reset — the cmms incident) or its fetched
    secret carries no password, a ``logger.warning`` naming the system and the
    reason is emitted (greppable: "fallback to env") and the env-derived
    ``fallback`` inputs are used for THAT system only. But when the record IS
    usable and only the operator-side ``get_secret_value`` fails (e.g.
    AccessDenied), there is NO env fallback — that is the F2 incident class
    (stale laptop env overriding a known-good record) — a ``ReonboardError``
    naming the secret ARN and the required IAM action
    (``secretsmanager:GetSecretValue``) is raised instead; ``--from-env`` is
    the explicit escape hatch. An underivable connection still surfaces as the
    caller's fail-loud ``ReonboardError`` — never a silent "done". Secret
    VALUES never appear in any log line.

    Same return shape as ``connection_inputs``:
    ``{sid: {connection_string, token, connection_type, schema}}`` (plus a
    non-functional ``source`` marker: ``record`` | ``env``).
    """
    catalog_meta = app.catalog() if catalog_meta is None else catalog_meta
    env_inputs = None  # env-derived fallback, computed lazily at most once

    def _env_inputs():
        nonlocal env_inputs
        if env_inputs is None:
            env_inputs = fallback(catalog_meta, list(systems), env)
        return env_inputs

    out: dict[str, dict] = {}
    for sid in systems:
        ident = catalog_meta.get(sid, {})
        connection_type = ident.get("connection_type", "PostgreSQL")
        schema = ident.get("schema", "")

        record, reason = {}, ""
        try:
            record = app.call("GET", DESCRIBE_CONNECTION_ROUTE.format(system_id=sid)) or {}
        except Exception as exc:  # noqa: BLE001 — classified as a per-system fallback
            reason = f"describe_connection failed ({type(exc).__name__}: {exc})"

        rec_type = record.get("connection_type") or connection_type
        if rec_type != "PostgreSQL":
            # Non-SQL types keep their existing derivation (out-of-band secret).
            entry = dict(_env_inputs().get(sid, {}))
            entry.setdefault("connection_type", connection_type)
            entry.setdefault("schema", schema)
            entry["source"] = "env"
            out[sid] = entry
            log(f"  [{sid}] non-SQL type '{rec_type}' — env-derived input (existing path)")
            continue

        connection_string = ""
        if not reason:
            host = record.get("host") or ""
            dbname = record.get("dbname") or ""
            secret_arn = record.get("secret_arn") or ""
            if host and dbname and secret_arn:
                try:
                    sm = sm_client if sm_client is not None else _default_sm_client()
                    raw = sm.get_secret_value(SecretId=secret_arn).get("SecretString", "")
                except Exception as exc:  # noqa: BLE001 — fail LOUD, never a value leak
                    # The record EXISTS and is usable — only the operator-side
                    # credential fetch failed (typically AccessDenied). Falling
                    # back to env here is the F2 incident class: a stale laptop
                    # env silently overriding a known-good persisted record. So
                    # fail loud instead (--from-env is the explicit escape hatch).
                    raise ReonboardError(
                        f"[{sid}] persisted connection record is usable but the "
                        f"operator-side credential fetch from {secret_arn} failed "
                        f"({type(exc).__name__}). The operator identity needs IAM "
                        f"secretsmanager:GetSecretValue on that secret. NOT falling "
                        f"back to env — a stale shell env must not override a "
                        f"known-good record; fix IAM, or pass --from-env to force "
                        f"the pure-env derivation."
                    ) from exc
                try:
                    secret = json.loads(raw) if raw else {}
                except ValueError:
                    secret = {}  # malformed SecretString → no password → loud fallback
                if not isinstance(secret, dict):
                    secret = {}
                connection_string = _dsn_from_record(record, secret, schema)
                if not connection_string:
                    reason = f"secret {secret_arn} carries no password"
            else:
                missing = [k for k, v in (("host", host), ("dbname", dbname),
                                          ("secret_arn", secret_arn)) if not v]
                reason = f"record absent/unconfigured (missing: {', '.join(missing)})"

        if connection_string:
            out[sid] = {"connection_string": connection_string,
                        "token": "",
                        "connection_type": rec_type,
                        "schema": record.get("search_path") or schema,
                        "source": "record"}
            log(f"  [{sid}] connection derived from persisted record "
                f"(host={record.get('host')})")
        else:
            # LOUD per-system fallback — the deployed gate greps for this line.
            logger.warning("[%s] fallback to env: persisted connection record "
                           "unusable (%s)", sid, reason)
            entry = dict(_env_inputs().get(sid, {}))
            entry.setdefault("connection_type", connection_type)
            entry.setdefault("schema", schema)
            entry["source"] = "env"
            out[sid] = entry
    return out



# --------------------------------------------------------------------------- #
# Onboarding pipeline seam (default = the real REST pipeline; tests inject a stub)
# --------------------------------------------------------------------------- #
def default_provision_fn(app, system_id, *, meta, connection_string, token):
    """Run the ordinary discover→map→resolve→provision pipeline for one system.

    Reuses the ``onboarding_smoketest`` helpers verbatim (DRY) so this driver
    exercises the IDENTICAL REST surface the UI/smoke test use. The secret (only
    the MCP token path supplies one today) is supplied out-of-band BEFORE
    provisioning, exactly as the smoke test does.
    """
    if token:
        supply_secret_out_of_band(app, system_id, token)
    dm = discover_and_map(app, system_id, connection_string=connection_string,
                          connection_type=str(meta.get("connection_type") or ""))
    m = dict(meta)
    m["schema"] = dm.get("schema") or meta.get("schema", "")
    rules = resolve(app, system_id, dm["mappings"])
    provision(app, system_id, m, dm["mappings"], rules, connection_string=connection_string)


# Poll interval during the server-side completion GRACE phase (seconds).
GRACE_POLL_S = 30.0


def _wait_ready_with_grace(wait_once, label, *, deadline_s, grace_s, log,
                           sleep=time.sleep, monotonic=time.monotonic):
    """Run ``wait_once(deadline)``; on a READY-timeout, enter a GRACE phase.

    Observed twice live (cmms, then lake-athena): the client gives up at the
    READY deadline while the SERVER completes provisioning minutes later and
    the resource goes READY on its own. So instead of raising at the deadline,
    keep polling the list every ``GRACE_POLL_S`` for up to *grace_s* more
    seconds. Only a grace expiry fails — the original typed timeout error is
    re-raised with the TOTAL wait (deadline + grace) after a "hard timeout"
    log line, so the exit message is distinguishable from "completed in grace".
    A terminal ``CREATE_FAILED`` inside grace propagates unchanged (grace never
    masks a genuine failure). ``grace_s <= 0`` disables the phase.
    """
    try:
        return wait_once(deadline_s)
    except (RuntimeReadyTimeoutError, TargetReadyTimeoutError) as exc:
        if grace_s <= 0:
            raise
        last = exc
    t0 = monotonic()
    while True:
        left = grace_s - (monotonic() - t0)
        if left <= 0:
            log(f"  {label} hard timeout — still not READY after {deadline_s:g}s "
                f"deadline + {grace_s:g}s grace")
            raise type(last)(last.pending, deadline_s + grace_s) from last
        log(f"  {label} deadline exceeded — server-side completion grace ({left:.0f}s left)")
        try:
            result = wait_once(0)  # deadline 0 = exactly ONE list poll
            log(f"  {label} READY — completed in grace (server-side completion)")
            return result
        except (RuntimeReadyTimeoutError, TargetReadyTimeoutError) as exc:
            last = exc
        sleep(GRACE_POLL_S)


def _graceful_runtime_wait(control, system_id, deadline_s, poll_s, grace_s, log,
                           **wait_kwargs):
    """Runtime READY wait with the server-side completion grace phase."""
    return _wait_ready_with_grace(
        lambda d: wait_runtimes_ready(control, [system_id], d, poll_s, **wait_kwargs),
        f"[{system_id}] runtime", deadline_s=deadline_s, grace_s=grace_s, log=log,
        sleep=wait_kwargs.get("sleep", time.sleep),
        monotonic=wait_kwargs.get("monotonic", time.monotonic),
    )


def _graceful_target_wait(control, gateway_id, system_id, deadline_s, poll_s,
                          grace_s, log, **wait_kwargs):
    """Gateway-target READY wait — symmetric grace (same window as runtimes)."""
    return _wait_ready_with_grace(
        lambda d: wait_targets_ready(control, gateway_id, [target_name(system_id)],
                                     d, poll_s, **wait_kwargs),
        f"[{system_id}] gateway target", deadline_s=deadline_s, grace_s=grace_s,
        log=log, sleep=wait_kwargs.get("sleep", time.sleep),
        monotonic=wait_kwargs.get("monotonic", time.monotonic),
    )


def _wait_runtime_with_bounded_retrigger(
    control,
    app,
    system_id,
    *,
    meta,
    connection_string,
    token,
    provision_fn,
    runtime_deadline_s,
    delete_deadline_s,
    poll_s,
    grace_s,
    log,
    **wait_kwargs,
):
    """Block until the runtime is READY, re-triggering the create at most ONCE.

    On a terminal ``CREATE_FAILED`` / ``UPDATE_FAILED`` (the stranded-runtime
    strand), delete the failed runtime and re-run provisioning a single time,
    then wait again. A second terminal/timeout failure propagates unchanged.
    Both waits carry the server-side completion grace phase (*grace_s*).
    """
    try:
        _graceful_runtime_wait(control, system_id, runtime_deadline_s, poll_s,
                               grace_s, log, **wait_kwargs)
        return
    except RuntimeCreateFailedError as exc:
        log(
            f"  [{system_id}] runtime {exc.runtime_name} stranded {exc.status} "
            f"({exc.failure_reason or 'no failureReason'}) — deleting and "
            f"re-triggering create ONCE"
        )
        delete_runtime(control, system_id, delete_deadline_s, poll_s, **wait_kwargs)
        provision_fn(app, system_id, meta=meta, connection_string=connection_string, token=token)
        # Second attempt: any terminal failure / timeout now propagates (bounded).
        _graceful_runtime_wait(control, system_id, runtime_deadline_s, poll_s,
                               grace_s, log, **wait_kwargs)


# --------------------------------------------------------------------------- #
# E1 preflight gate (ADR-0053 D-E1) — optional, runs BEFORE the delete phase.
# --------------------------------------------------------------------------- #
PREFLIGHT_ROUTE = "/api/onboard/preflight"
PREFLIGHT_POLL_S = 3.0
PREFLIGHT_DEADLINE_S = 360.0


def _poll_preflight(app, job_id, *, deadline_s, poll_s, sleep, monotonic) -> dict:
    """Poll ``GET /api/onboard/preflight/{job_id}`` until done/error or the
    client-side deadline (an expired poll folds to an honest error job)."""
    t0 = monotonic()
    while True:
        job = app.call("GET", f"{PREFLIGHT_ROUTE}/{job_id}") or {}
        if job.get("status") in ("done", "error"):
            return job
        if monotonic() - t0 >= deadline_s:
            return {"status": "error",
                    "error": f"preflight poll deadline ({deadline_s:g}s) exceeded"}
        sleep(poll_s)


def preflight_gate(app, system_id, *, log=logger.info,
                   deadline_s=PREFLIGHT_DEADLINE_S, poll_s=PREFLIGHT_POLL_S,
                   sleep=time.sleep, monotonic=time.monotonic) -> bool:
    """Run the E1 preflight job for ONE system BEFORE its delete phase.

    Verdict semantics (ADR-0053 D-E1 — a diagnostic gate, not authz):
    ``blocked`` ⇒ ``False`` — the caller ABORTS this system with the honest
    verdict + connection reason logged, BEFORE anything is torn down;
    ``degraded`` ⇒ warnings logged, proceed; ``ready`` ⇒ proceed. An
    errored / timed-out / unavailable preflight (job error, or an older env
    without the route) logs an honest "inconclusive" line and PROCEEDS —
    only an explicit blocked verdict stops a re-onboard.
    """
    try:
        started = app.call("POST", PREFLIGHT_ROUTE, {"system_id": system_id}) or {}
        job = _poll_preflight(app, str(started.get("job_id") or ""),
                              deadline_s=deadline_s, poll_s=poll_s,
                              sleep=sleep, monotonic=monotonic)
    except Exception as exc:  # noqa: BLE001 — diagnostic-only, never a new failure mode
        log(f"  [{system_id}] preflight inconclusive ({type(exc).__name__}) — proceeding")
        return True
    result = job.get("result") or {}
    verdict = str(result.get("verdict") or "")
    if job.get("status") != "done" or not verdict:
        log(f"  [{system_id}] preflight inconclusive "
            f"({job.get('error') or 'no verdict'}) — proceeding")
        return True
    for warning in result.get("warnings") or []:
        log(f"  [{system_id}] preflight warning: {warning}")
    if verdict == "blocked":
        conn = result.get("connection") or {}
        logger.error("[%s] preflight verdict BLOCKED (%s: %s) — aborting this "
                     "system; nothing was deleted", system_id,
                     conn.get("reason") or "connection failed",
                     conn.get("detail") or "")
        return False
    log(f"  [{system_id}] preflight verdict {verdict.upper()} — proceeding")
    return True


def _gate_and_reonboard(app, control, gateway_id, system_id, ci, catalog_meta,
                        provision_fn, dl, log, wait_kwargs, preflight) -> str:
    """One system: optional preflight gate (BEFORE delete), then
    :func:`_reonboard_one`. Returns ``"ready"`` | ``"preflight-blocked"``.
    Time seams ride the shared ``wait_kwargs`` (offline-testable)."""
    if preflight and not preflight_gate(
            app, system_id, log=log,
            sleep=wait_kwargs.get("sleep", time.sleep),
            monotonic=wait_kwargs.get("monotonic", time.monotonic)):
        return "preflight-blocked"
    _reonboard_one(app, control, gateway_id, system_id, ci, catalog_meta,
                   provision_fn, dl, log, wait_kwargs)
    return "ready"


def _reonboard_one(app, control, gateway_id, system_id, ci, catalog_meta,
                   provision_fn, dl: dict, log, wait_kwargs) -> None:
    """Delete → provision → graceful runtime+target waits for ONE system.
    Extracted verbatim from the reonboard_systems loop (maintainability
    gate); raises on any failure — the caller records success only."""
    connection_string = ci.get("connection_string", "")
    if not connection_string:
        # No server-side fallback: a system we cannot connect to must fail
        # loudly rather than be reported "done".
        raise ReonboardError(
            f"cannot derive a connection for '{system_id}' — no usable "
            f"persisted record and no env-derived input (set "
            f"POSTGRES_*/ERP_POSTGRES_* or the OT url env, or fix the record)"
        )
    meta = dict(catalog_meta.get(system_id, {"name": system_id}))
    if ci.get("connection_type"):
        meta["connection_type"] = ci["connection_type"]
    if ci.get("schema"):
        meta["schema"] = ci["schema"]

    log(f"== reonboard [{system_id}] ==")
    log(f"  [{system_id}] deleting existing gateway target + runtime")
    delete_target(control, gateway_id, system_id, dl["delete"], dl["poll"], **wait_kwargs)
    delete_runtime(control, system_id, dl["delete"], dl["poll"], **wait_kwargs)

    log(f"  [{system_id}] discover → map → resolve → provision")
    provision_fn(app, system_id, meta=meta,
                 connection_string=connection_string, token=ci.get("token", ""))

    log(f"  [{system_id}] waiting for runtime READY")
    _wait_runtime_with_bounded_retrigger(
        control, app, system_id,
        meta=meta, connection_string=connection_string, token=ci.get("token", ""),
        provision_fn=provision_fn, runtime_deadline_s=dl["runtime"],
        delete_deadline_s=dl["delete"], poll_s=dl["poll"], grace_s=dl["grace"],
        log=log, **wait_kwargs)

    log(f"  [{system_id}] waiting for gateway target READY")
    _graceful_target_wait(control, gateway_id, system_id,
                          dl["target"], dl["poll"], dl["grace"], log, **wait_kwargs)
    log(f"  [{system_id}] READY (runtime + gateway target)")


def reonboard_systems(
    app,
    control,
    gateway_id,
    systems,
    *,
    env=None,
    provision_fn=default_provision_fn,
    connection_resolver=None,
    sm_client=None,
    runtime_deadline_s=600,
    target_deadline_s=600,
    delete_deadline_s=600,
    grace_s=600,
    poll_s=10,
    log=logger.info,
    sweep_blocked=True,
    preflight=False,
    **wait_kwargs,
):
    """Re-onboard each system in order; block on runtime + target READY.
    Dependency-injected (offline-testable); returns {system_id: "ready" |
    "preflight-blocked"} or raises the first error (READY timeout after the
    grace_s server-side window, terminal runtime failure, un-derivable
    connection). connection_resolver defaults to RECORDS-FIRST (ADR-0044 D3;
    inject connection_inputs for --from-env); wait_kwargs → all wait helpers;
    preflight=True runs the E1 gate per system BEFORE its delete phase."""
    env = os.environ if env is None else env
    if not gateway_id:
        raise ReonboardError("gateway_id is required (pass --gateway-id or set AGENTCORE_GATEWAY_ID)")

    catalog_meta = app.catalog()
    if connection_resolver is None:
        def connection_resolver(cm, sys_list, e):
            return records_connection_inputs(app, sys_list, sm_client, e,
                                             catalog_meta=cm, log=log)
    conn_in = connection_resolver(catalog_meta, list(systems), env)

    deadlines = {"runtime": runtime_deadline_s, "target": target_deadline_s,
                 "delete": delete_deadline_s, "grace": grace_s, "poll": poll_s}
    results: dict[str, str] = {}
    for system_id in systems:
        results[system_id] = _gate_and_reonboard(
            app, control, gateway_id, system_id, conn_in.get(system_id, {}),
            catalog_meta, provision_fn, deadlines, log, wait_kwargs, preflight)

    if sweep_blocked:
        _sweep_blocked_proposals(app, log)
    return results


def _sweep_blocked_proposals(app, log) -> None:
    """Best-effort post-READY cleanup: proposals logged mid-re-onboard against
    a transiently-unavailable schema carry an INDETERMINATE column verdict;
    with the schema back they resolve to not_found and would linger blocked in
    the review queue. reconcile-blocked purges exactly those (idempotent) and
    NEVER fails the re-onboard — errors are logged and swallowed."""
    try:
        rc = app.call("POST", "/api/proposed-mappings/reconcile-blocked", {})
        log(f"  reconcile-blocked sweep: {rc}")
    except Exception as exc:  # noqa: BLE001 — advisory cleanup; systems are READY
        log(f"  reconcile-blocked sweep skipped ({type(exc).__name__}: {exc})")


# --------------------------------------------------------------------------- #
# CLI wiring (boto3 / Secrets Manager / argparse) — kept out of the testable core
# --------------------------------------------------------------------------- #
def _session(profile: str, region: str):
    import boto3
    return (
        boto3.Session(profile_name=profile, region_name=region)
        if profile else boto3.Session(region_name=region)
    )


def _control_client(profile: str, region: str):
    return _session(profile, region).client("bedrock-agentcore-control")


def _resolve_password(explicit: str, profile: str, region: str) -> str:
    """Return the demo password from --password, $ADT_PASSWORD, or Secrets Manager."""
    if explicit:
        return explicit
    env_pw = os.environ.get("ADT_PASSWORD", "")
    if env_pw:
        return env_pw
    sm = _session(profile, region).client("secretsmanager")
    return sm.get_secret_value(SecretId=DEMO_PASSWORD_SECRET_ID)["SecretString"]


def _discover_all_systems(app) -> list[str]:
    """Deterministic --all order: onboarded systems (sorted), else all (sorted).

    The fallback (nothing onboarded yet) drops the register-only exercise
    systems (``agents.demo.workshop.ONBOARD_EXCLUDE``) — they are never a bulk
    default, in demo or workshop mode. An already-onboarded one is kept.
    """
    from agents.demo.workshop import ONBOARD_EXCLUDE
    catalog_meta = app.catalog()
    onboarded = sorted(sid for sid, e in catalog_meta.items() if e.get("status") == "onboarded")
    return onboarded or sorted(set(catalog_meta) - ONBOARD_EXCLUDE)


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI surface (extracted from main for the maintainability gate)."""
    ap = argparse.ArgumentParser(description="Durable re-onboard driver (waits for runtime READY)")
    ap.add_argument("--base-url", default=os.environ.get("ADT_BASE_URL", ""))
    ap.add_argument("--username", default=os.environ.get("ADT_USERNAME", "ADT"))
    ap.add_argument("--password", default="")
    ap.add_argument("--systems", default="",
                    help="comma-separated system ids in the order to re-onboard "
                         "(put the AZ-strip-eating system first)")
    ap.add_argument("--all", action="store_true",
                    help="re-onboard every system in the catalog (deterministic sorted order)")
    ap.add_argument("--from-env", action="store_true",
                    help="derive connections from the local shell env ONLY "
                         "(POSTGRES_*/ERP_POSTGRES_*/OT url env) instead of the "
                         "records-first default — the bootstrap escape hatch")
    ap.add_argument("--gateway-id", default=os.environ.get("AGENTCORE_GATEWAY_ID", ""))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    ap.add_argument("--runtime-deadline", type=float, default=600.0)
    ap.add_argument("--target-deadline", type=float, default=600.0)
    ap.add_argument("--delete-deadline", type=float, default=600.0,
                    help="seconds to wait for a runtime/target to fully delete "
                         "before recreate (default 600s: a VPC-mode runtime "
                         "teardown was observed to outlive 180s)")
    ap.add_argument("--grace", type=float, default=600.0,
                    help="extra seconds to keep polling after a READY deadline expires "
                         "(server-side completion grace — the create often finishes "
                         "minutes after the client gives up); 0 disables")
    ap.add_argument("--preflight", action="store_true",
                    help="run the E1 onboarding preflight per system BEFORE its "
                         "delete phase: a 'blocked' verdict aborts that system "
                         "(nothing torn down); 'degraded' logs and proceeds")
    ap.add_argument("--poll", type=float, default=10.0)
    return ap


def _prepare_session(args):
    """Validate CLI args, log in, resolve the system list. None ⇒ exit 2
    (each failure already logged). Extracted from main (gate)."""
    if not args.base_url:
        logger.error("--base-url (or ADT_BASE_URL) is required")
        return None
    if bool(args.systems.strip()) == bool(args.all):
        logger.error("pass exactly one of --systems <a,b,c> or --all")
        return None
    if not args.gateway_id:
        logger.error("--gateway-id (or AGENTCORE_GATEWAY_ID) is required")
        return None
    try:
        password = _resolve_password(args.password, args.profile, args.region)
    except Exception as exc:  # noqa: BLE001 — surface a clear credential error
        logger.error(f"could not resolve demo password: {type(exc).__name__}: {exc}")
        return None
    app = App(args.base_url)
    login = app.call("POST", "/api/auth/login", {"email": args.username, "password": password})
    if login.get("status") != "ok":
        # Only the status: the reply body can echo session or credential details.
        logger.error(f"login failed (status {login.get('status')!r})")
        return None
    systems = (_discover_all_systems(app) if args.all
               else [s.strip() for s in args.systems.split(",") if s.strip()])
    if not systems:
        logger.error("no systems to re-onboard")
        return None
    return app, systems


def _report_outcome(results: dict) -> int:
    """Final log + exit status. 0 only when EVERY system reached READY; a
    preflight-blocked system exits 1 with the blocked ids named (honest —
    a partial run is never reported as complete)."""
    blocked = sorted(s for s, v in results.items() if v != "ready")
    if blocked:
        ready = sum(1 for v in results.values() if v == "ready")
        logger.error(f"re-onboard INCOMPLETE — {ready} ready, "
                     f"preflight-blocked: {', '.join(blocked)}")
        return 1
    logger.info(f"re-onboard complete — {len(results)} system(s) runtime+target READY")
    return 0


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    prepared = _prepare_session(args)
    if prepared is None:
        return 2
    app, systems = prepared

    logger.info(f"re-onboarding {len(systems)} system(s) in order: {', '.join(systems)}")
    control = _control_client(args.profile, args.region)

    if args.from_env:
        logger.info("connection derivation: --from-env (pure env, records-first bypassed)")
        connection_resolver = connection_inputs
        sm_client = None
    else:
        logger.info("connection derivation: records-first (persisted records via "
                    "describe_connection; env is a LOUD per-system fallback)")
        connection_resolver = None  # reonboard_systems defaults to records-first
        sm_client = _session(args.profile, args.region).client("secretsmanager")

    try:
        results = reonboard_systems(
            app, control, args.gateway_id, systems,
            connection_resolver=connection_resolver,
            sm_client=sm_client,
            runtime_deadline_s=args.runtime_deadline,
            target_deadline_s=args.target_deadline,
            delete_deadline_s=args.delete_deadline,
            grace_s=args.grace,
            poll_s=args.poll,
            preflight=args.preflight,
        )
    except Exception as exc:  # noqa: BLE001 — report and fail non-zero; do not mask
        logger.error(f"re-onboard FAILED: {type(exc).__name__}: {exc}")
        return 1

    return _report_outcome(results)


if __name__ == "__main__":
    sys.exit(main())
