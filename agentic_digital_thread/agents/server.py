# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""FastAPI backend with SSE streaming for the orchestrator."""

import collections
import logging
import math
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


class _SafeServerLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.exc_info = None
        record.exc_text = None
        return True


logger.addFilter(_SafeServerLogFilter())

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .orchestrator import get_orchestrator, add_system_agent
from .offload import offload
from .public_errors import public_error
from .steward_gate import is_steward
from .turn_limits import ASK_RUN_CEILING_SECONDS
from .metric_turn_invalidation import invalidates_metric_turn_results
from .stored_value_mapping_memo import mutates_value_mappings
from starlette.middleware.gzip import GZipMiddleware

#: Responses smaller than this are sent uncompressed.
GZIP_MINIMUM_SIZE = 1024
from .chat_client import get_chat_client
from .agent_manager import AgentManager, gateway_target_snapshot
from .runtime_provision_helpers import GatewayTargetUpdateError
from .db import neptune_query, neptune_query_checked, pg
from .graph_internal_props import without_internal_properties
from . import joinpath_rule_link as _jp_rule
from .catalog import catalog
from .models import AskRequest, UserDefinedMappingRequest, EditMappingRequest
from .branding import COMPANY, as_dict as branding_dict
from . import mqtt_subscriber
from . import rederive
from .config import get_config
from . import startup_restore
from .startup_restore import refuse_while_restoring
from .auth.middleware import AuthMiddleware
from .auth.routes import router as auth_router
from .prompts import (
    SCHEDULER_BUSY_RESULT, add_interval_job, init as prompts_init,
    router as prompts_router, seed_registry, start_scheduler, stop_scheduler,
)
from .video_briefing import generate_briefing
from .memory_routes import router as memory_router
from .feedback import router as feedback_router



# ── Global agent manager ──────────────────────────────────────────────────────

def _create_agent_manager() -> AgentManager:
    # Explicit override takes priority (e.g. AGENT_MODE=local for dev)
    explicit_mode = os.environ.get("AGENT_MODE", "")
    
    gateway_id = os.environ.get("AGENTCORE_GATEWAY_ID", "")
    gateway_url = os.environ.get("AGENTCORE_GATEWAY_URL", "")
    ecr_image = os.environ.get("AGENTCORE_ECR_IMAGE", "")
    runtime_role = os.environ.get("AGENTCORE_RUNTIME_ROLE", "")

    if explicit_mode:
        mode = explicit_mode
    elif gateway_id and ecr_image:
        mode = "cloud"
    else:
        mode = "local"

    logger.info("Agent_Manager mode: %s (explicit=%s, gateway_id=%s, ecr_image=%s)",
                mode, bool(explicit_mode), bool(gateway_id), bool(ecr_image))

    return AgentManager(
        mode=mode,
        gateway_id=gateway_id,
        gateway_url=gateway_url,
        ecr_image_uri=ecr_image,
        runtime_role_arn=runtime_role,
    )

_agent_manager = _create_agent_manager()


# ── Lifespan ──────────────────────────────────────────────────────────────────

def _sql_tables_desc_from_graph(system_id: str, connection_type: str = "") -> str:
    """Rebuild a SQL system's full per-table schema string from the knowledge graph.

    Reads the ``(System)-[:HAS_SCHEMA]->(SchemaEntry)-[:HAS_FIELD]->(Field)`` shape
    captured at discover time — each Field carries ``name`` and (for Athena)
    ``data_type`` / ``is_partition`` — and renders it via
    ``system_agent.format_tables_desc`` as ``tbl(col: type, ...)`` with partition
    keys flagged. Used on restore for SQL-backed direct-source systems (Athena) so
    the runtime is created with the schema already baked in and does not re-discover
    it per sub-question. Generic — no dataset knowledge. Returns "" when the graph
    carries no fields for the system.

    NOTE — this path deliberately emits NO ``(no partition keys)`` marker
    (``schema_format.PARTITION_KEYS_ABSENT_MARKER``). That marker requires the
    affirmative "physical introspection completed under the row cap" signal, which
    is observed at ONBOARDING time and is not persisted on the graph. A restored
    system therefore renders marker-free for unpartitioned tables, which parses as
    "unknown" and falls back to live partition discovery — a bounded latency cost,
    never a wrong answer. Baking the marker here would require inventing a
    completeness claim the graph cannot support.
    """
    from .connection_types import S3TABLES_DUCKDB as _S3TABLES_DUCKDB
    from .system_agent import format_tables_desc
    rows = neptune_query(
        "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field) "
        "RETURN se.name AS tbl, se.relation_kind AS relation_kind, f.name AS col, "
        "f.data_type AS dtype, f.is_partition AS is_part "
        "ORDER BY se.name, f.name",
        {"sid": system_id},
    ) or []
    qualify = _s3tables_catalog_prefix if connection_type == _S3TABLES_DUCKDB else (lambda t: t)
    table_cols: dict = {}
    for r in rows:
        tbl = r.get("tbl")
        col = r.get("col")
        if not tbl or not col:
            continue
        dtype = (r.get("dtype") or "")
        if r.get("is_part"):
            dtype = (dtype + " (partition key)").strip()
        record = table_cols.setdefault(qualify(tbl), {
            "relation_kind": r.get("relation_kind") or "unknown",
            "columns": [],
        })
        record["columns"].append((col, dtype))
    return format_tables_desc(table_cols)


def _s3tables_catalog_prefix(table: str) -> str:
    """The fully qualified SQL name an S3 Tables SchemaEntry maps to:
    ``s3t.<namespace>.<table>``. The SchemaEntry name is the namespace-qualified
    ``<namespace>.<table>``; prepend the DuckDB catalog so the restored prompt
    names the exact relation the agent must write, as the provision bake does.
    Idempotent when already prefixed."""
    from .duckdb_tools import CATALOG
    name = str(table or "")
    return name if name.startswith(f"{CATALOG}.") else f"{CATALOG}.{name}"



def _restore_relation_kinds(
    connection_type: str, schema: str, connection_config: dict,
) -> dict[str, str]:
    """Read at most 500 server-owned physical relation kinds during restore."""
    if connection_type == "PostgreSQL" and schema:
        result = pg(
            schema,
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name LIMIT 500",
            (schema,),
        )
        rows = result.get("rows", [])
    else:
        from .connection_types import ATHENA
        if connection_type != ATHENA:
            return {}
        from .athena_tools import run_athena_query, _resolve_config, _safe_sql_literal
        cfg = _resolve_config(connection_config)
        database = _safe_sql_literal(cfg["database"])
        rows = run_athena_query(
            "SELECT table_name, table_type FROM information_schema.tables "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
            f"WHERE table_schema = '{database}' ORDER BY table_name LIMIT 500",
            cfg, max_rows=500,
        ).get("rows", [])
    aliases = {"base table": "base_table", "table": "base_table"}
    return {
        str(row[0]): aliases.get(str(row[1]).strip().lower(), str(row[1]).strip().lower())
        for row in rows[:500] if len(row) >= 2
    }


def _reconcile_restored_sql_provenance(
    system_id: str, schema: str, connection_type: str,
    connection_config: dict, tables_desc: str,
) -> None:
    """Backfill a bounded existing SQL schema before restoring its runtime."""
    from .response_integrity_sql import parse_physical_schema
    from .onboard import _persist_schema_relation_kinds

    physical = parse_physical_schema(tables_desc)
    kinds = _restore_relation_kinds(connection_type, schema, connection_config)
    mappings: list[dict] = []
    for table, metadata in physical.items():
        for field, data_type in list(metadata.get("columns", {}).items()):
            mappings.append({
                "table": table, "field": f"{table}.{field}",
                "relation_kind": kinds.get(table, "unknown"),
                "data_type": data_type or "unknown",
            })
            if len(mappings) >= 500:
                break
        if len(mappings) >= 500:
            break
    if mappings:
        _persist_schema_relation_kinds(system_id, mappings)

#: Longest exception message carried into a restore failure log line.
_RESTORE_REASON_CHARS = 300


def _restore_failure_reason(exc: BaseException) -> str:
    """A log-safe reason for a system the restore could not start: the
    exception type always, the message only for the package's own
    gateway-target errors (whose text names targets, statuses and endpoints).
    An arbitrary provisioning exception can carry connection details, so its
    text is never logged (same rule as the onboarding provision-failure log
    filter)."""
    if isinstance(exc, GatewayTargetUpdateError):
        return f"{type(exc).__name__}: {str(exc)[:_RESTORE_REASON_CHARS]}"
    return type(exc).__name__


def _restore_one(**start_kwargs) -> str:
    """Start and register ONE restored system, isolated from the others.

    Returns its MCP URL, or ``""`` when it could not be started: the failure
    is logged (log-safe reason) and the restore carries on with the next
    system. The failed system stays unregistered (fail-closed) until the next
    restart or an explicit re-onboard — there is no background retry."""
    system_id = start_kwargs.get("system_id", "")
    # A server restore is a NO-WRITE registration of a READY runtime: it already
    # carries its baked config from the provision that created it, so restore
    # must never rewrite the agent-config secret or roll it. Only an explicit
    # PROVISION (onboard._start_replacement_agent) refreshes the generated config.
    start_kwargs.setdefault("refresh_generated_config", False)
    try:
        mcp_url = _restore_start(start_kwargs)
    except Exception as exc:  # noqa: BLE001 - per-system isolation; logged, left unregistered
        logger.warning("Restore of %s failed (%s) — it stays unregistered until the next "
                       "restart or a re-onboard; continuing with the other systems",
                       system_id, _restore_failure_reason(exc))
        return ""
    add_system_agent(system_id, mcp_url)
    return mcp_url


def _restore_start(start_kwargs: dict) -> str:
    """Register a restored system without writing or rolling a READY runtime,
    but still CREATE or repair a missing one.

    First takes the no-write fast path (``fast_path_only=True``): a READY runtime
    registers with no secret write and no roll. When no READY fast path exists
    (:class:`FastPathUnavailable`), falls back to the normal create/repair path
    so a missing or unhealthy runtime is rebuilt -- the self-healing restore
    kept from before the provision-only split (review live-config-refresh-r2 F3).
    ``refresh_generated_config`` is already False in ``start_kwargs``, so neither
    attempt refreshes generated config on a reused runtime."""
    from .agent_manager import FastPathUnavailable
    try:
        return _agent_manager.start_agent(**{**start_kwargs, "fast_path_only": True})
    except FastPathUnavailable:
        return _agent_manager.start_agent(**{**start_kwargs, "fast_path_only": False})


def _restore():
    """Restore onboarded systems from Neptune on startup, then seed the core
    concepts.

    Connection details are sourced from the System node via
    ``resolve_connection``: the catalog is not read for
    connection data. System identity/metadata comes from the restore
    Cypher result. Module-level (not nested in :func:`lifespan`) so it is unit
    testable. Uses only module-level names; startup waits for it up to
    ``STARTUP_RESTORE_WAIT_S`` and it finishes on its own thread
    (:func:`_restore_graph`). Each sweep restores one system at a time
    through ``_restore_one``, so one system's failure never stops another.
    The systems come first: neither sweep reads a Concept node, and the seed
    (bounded by ``CORE_CONCEPT_SEED_BUDGET_S``) must not hold them up.
    """
    restored_pg_ids = _restore_sql_systems()
    _restore_direct_source_systems(restored_pg_ids)
    _restore_seed_core_concepts()


#: The startup core-concept seed, one statement per core concept. ON CREATE is
#: its only SET and every value is a parameter or a constant, so a second run
#: changes nothing; Neptune Analytics creates at most one node for concurrent
#: MERGEs of one key, so a copy still running alongside a resend leaves the
#: same graph too. It is sent again after a dropped connection as well as an
#: answered failure (``db.resubmit_safe_write(after_drop=True)``).
CORE_CONCEPT_SEED = (
    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $desc, "
    "c.source = 'core'")
#: Seconds the startup seed keeps starting core-concept MERGEs. It checks the
#: budget before each statement and starts none once it is spent, so the seed
#: ends within the budget plus one statement. From the graph client's own
#: bounds one statement takes at most 105 s: no resend starts later than
#: ``graph_resubmit.TOTAL_BOUND_S`` (30 s) after the first send, and one send
#: is at most ``db.GRAPH_CONNECT_TIMEOUT_S`` (10 s) plus
#: ``db.GRAPH_READ_TIMEOUT_S`` (65 s). The seed therefore ends within 165 s.
#: A computed bound, not a measurement. The concepts it leaves (not confirmed,
#: or not sent) go to the background top-up (:class:`_CoreConceptTopUp`): the
#: seed only starts its thread, which sends nothing before its first pause,
#: so it adds nothing to this bound.
CORE_CONCEPT_SEED_BUDGET_S = 60
#: The clock the seed budget, the top-up and the restore log read (a seam for
#: tests).
_restore_clock = time.monotonic
#: One core-concept seed statement in flight per process, so the startup
#: seed, the top-up and the seed of a later restore
#: (``graph_snapshot.repair_systems`` runs the restore again) never send two
#: copies of one concept at once. A write that timed out may still be running
#: in the graph after it returned; ``_CORE_CONCEPT_HELD`` keeps that concept
#: from being sent again meanwhile. A sender waits for the lock no longer than
#: its own budget, so the seed still ends within the 165 s above
#: (:func:`_take_core_concept_send`).
_CORE_CONCEPT_SEND = threading.Lock()
#: Seconds a sender waits for ``_CORE_CONCEPT_SEND`` at a time before it
#: checks its stop and its deadline again, so shutdown does not wait for
#: another sender's statement (up to 105 s) to end.
CORE_CONCEPT_SEND_WAIT_SLICE_S = 0.5
#: Core concept id -> ``_restore_clock`` time before which no sender sends it
#: again, because its last write timed out and its first copy may still be
#: running (:func:`_hold_if_timed_out`). Read and written holding
#: ``_CORE_CONCEPT_SEND``.
_CORE_CONCEPT_HELD: dict = {}
#: Seconds the core-concept top-up waits before each round; every round after
#: the last one listed waits the last value again.
CORE_CONCEPT_TOPUP_PAUSES_S = (15, 30, 60, 120)
#: Seconds after it starts that the core-concept top-up starts no more
#: rounds. On 2026-10-23 a fresh deploy's graph dropped every early call and
#: the startup seed confirmed 9 of 65 core concepts. That task logged its last
#: dropped call about a minute after it started (none in the next 18 minutes
#: of its log), and a restart 40 minutes later confirmed all 65 in under 3 s.
#: How long a cold graph can keep dropping calls is not measured; this bound
#: is well past the one case seen. What is still not confirmed at the bound is
#: sent by the next start. It starts no round that its pause would carry past
#: the bound and no statement past it, so it ends within the bound plus one
#: statement (105 s, above).
CORE_CONCEPT_TOPUP_BOUND_S = 3600


def _take_core_concept_send(deadline: float, stop=None) -> bool:
    """Take ``_CORE_CONCEPT_SEND`` for one statement: True when it is held
    with ``deadline`` not reached and ``stop`` (when given) not set, else
    False with the lock not held. Waits in slices of
    ``CORE_CONCEPT_SEND_WAIT_SLICE_S``, checking both between them, and
    checks both again once it holds the lock: a sender that waited out
    another one's statement starts no write after shutdown has begun."""
    def ended() -> bool:
        return _restore_clock() >= deadline or (stop is not None and stop.is_set())
    wait_s = deadline - _restore_clock()
    while wait_s > 0 and not ended():
        slice_s = min(wait_s, CORE_CONCEPT_SEND_WAIT_SLICE_S)
        if _CORE_CONCEPT_SEND.acquire(timeout=slice_s):
            if not ended():
                return True
            _CORE_CONCEPT_SEND.release()
            return False
        wait_s = min(deadline - _restore_clock(), wait_s - slice_s)
    return False


def _core_concept_held(cid: str) -> bool:
    """Whether ``cid`` is held after a write of it timed out."""
    return _CORE_CONCEPT_HELD.get(cid, float("-inf")) > _restore_clock()


def _hold_if_timed_out(cid: str, outcome) -> None:
    """Hold ``cid`` when its write ended ``graph_resubmit.TIMED_OUT`` (a read
    timeout or a 504): its first copy may still be running, and sending it
    again would double the wait and the load, as ``graph_resubmit`` says when
    it sends nothing again after a timeout. The graph stops a query at the
    server limit it was sent with (``db.graph_query_timeout_ms()``), counted
    from when it entered the graph's queue, which was before the write
    returned. So the hold ends that limit plus ``db.GRAPH_READ_MARGIN_S``
    after the write returned. Called holding ``_CORE_CONCEPT_SEND``."""
    from . import db, graph_resubmit
    if outcome == graph_resubmit.TIMED_OUT:
        _CORE_CONCEPT_HELD[cid] = (_restore_clock() + db.graph_query_timeout_ms() / 1000
                                   + db.GRAPH_READ_MARGIN_S)


def _send_core_concepts(budget_s: float, ids=None, stop=None) -> "tuple[list, list]":
    """Send the seed MERGE for each core concept in ``ids`` (every core
    concept by default), in order, until ``budget_s`` is spent or ``stop`` is
    set; ``(not_confirmed, not_sent)`` concept ids. A write that was not
    confirmed (``db.neptune_write_outcome``) may still have taken effect (a
    first copy that committed before its connection dropped). A concept held
    after a timed-out write (:func:`_hold_if_timed_out`) is not sent and
    counts as not confirmed. Each statement is sent holding
    ``_CORE_CONCEPT_SEND`` (:func:`_take_core_concept_send`)."""
    from .catalog import CORE_CONCEPT_DEFINITIONS
    from .db import neptune_write_outcome, resubmit_safe_write
    deadline = _restore_clock() + budget_s
    not_confirmed, ids = [], list(CORE_CONCEPT_DEFINITIONS if ids is None else ids)
    for index, cid in enumerate(ids):
        if not _take_core_concept_send(deadline, stop):
            return not_confirmed, ids[index:]
        try:
            held, outcome = _core_concept_held(cid), None
            if not held:
                with resubmit_safe_write(after_drop=True):
                    outcome = neptune_write_outcome(
                        CORE_CONCEPT_SEED, {"cid": cid, "desc": CORE_CONCEPT_DEFINITIONS[cid]})
                _hold_if_timed_out(cid, outcome)
        finally:
            _CORE_CONCEPT_SEND.release()
        if held or outcome is not None:
            not_confirmed.append(cid)
    return not_confirmed, []


def _wait_unless_stopped(stop: threading.Event, seconds: float) -> bool:
    """Wait ``seconds``, or until ``stop`` is set; whether it is set."""
    return stop.wait(seconds)


#: The top-up's pause before a round (a seam for tests, which move a fake
#: clock instead of waiting).
_topup_wait = _wait_unless_stopped


def _topup_pause_s(round_no: int) -> float:
    """The pause before round ``round_no`` (from 1) of the top-up."""
    return CORE_CONCEPT_TOPUP_PAUSES_S[min(round_no, len(CORE_CONCEPT_TOPUP_PAUSES_S)) - 1]


def _top_up_core_concepts(left: list, stop: threading.Event) -> None:
    """Send the core concepts ``left`` again in rounds, each after a pause
    (``CORE_CONCEPT_TOPUP_PAUSES_S``), until the graph has confirmed them
    all, ``stop`` is set, or no further round fits before
    ``CORE_CONCEPT_TOPUP_BOUND_S``. A round sends only the concepts not held
    after a timed-out write (:func:`_hold_if_timed_out`); a held one waits for
    a later round. Logs one line for how it ended, and one warning for each
    round that sent concepts and confirmed none when another round follows."""
    started = _restore_clock()
    deadline, total, round_no = started + CORE_CONCEPT_TOPUP_BOUND_S, len(left), 1
    while _restore_clock() + _topup_pause_s(round_no) < deadline:
        if _topup_wait(stop, _topup_pause_s(round_no)):
            break
        due = [cid for cid in left if not _core_concept_held(cid)]
        not_confirmed, not_sent = _send_core_concepts(deadline - _restore_clock(), due, stop)
        still = set(not_confirmed + not_sent) | (set(left) - set(due))
        left = [cid for cid in left if cid in still]
        if not left:
            logger.info("Core-concept top-up done %.0fs after it started: the graph confirmed "
                        "all %d core concepts the startup seed left",
                        _restore_clock() - started, total)
            return
        if stop.is_set():
            break
        round_no += 1
        if due and set(due) <= still \
                and _restore_clock() + _topup_pause_s(round_no) < deadline:
            logger.warning("Core-concept top-up round %d confirmed none of the %d left; "
                           "trying again in %ss", round_no - 1, len(left),
                           _topup_pause_s(round_no))
    if stop.is_set():
        logger.info("Core-concept top-up stopped by shutdown with %d not confirmed: %s; the "
                    "next start sends them", len(left), ", ".join(left))
    else:
        logger.warning("Core-concept top-up gave up %.0fs after it started (its bound is %ds) "
                       "with %d not confirmed: %s; the next start sends them",
                       _restore_clock() - started, CORE_CONCEPT_TOPUP_BOUND_S, len(left),
                       ", ".join(left))


class _CoreConceptTopUp:
    """The background top-up of the core concepts the startup seed left (not
    confirmed, or not sent once its budget was spent): rounds of the seed's
    own statement (:func:`_top_up_core_concepts`) on a daemon thread,
    ``core-concept-topup``. The restore only starts it, so the restore, and
    the refusal while it runs (``startup_restore``), end as before. Its one
    thread sends one statement at a time, after the seed's last one returned,
    and at most one top-up runs per process (:meth:`start`). Shutdown
    (:meth:`shut_down`, from :func:`lifespan`) sets :attr:`stop`, which ends a
    pause at once and is checked before each statement and once the seed
    lock is held; nothing waits for the thread. Starting and shutting down
    take one lock, so no top-up starts once shutdown has begun."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.thread: "threading.Thread | None" = None
        self._lock = threading.Lock()

    def start(self, left: list, announce) -> bool:
        """Call ``announce`` (it logs the seed line) and start the top-up of
        ``left`` on its thread, as one step with respect to :meth:`shut_down`;
        False, with neither done, when this process already started one or
        shutdown has begun."""
        with self._lock:
            if self.thread is not None or self.stop.is_set():
                return False
            self.thread = threading.Thread(target=self._run, args=(list(left),),
                                           name="core-concept-topup", daemon=True)
            announce()
            self.thread.start()
            return True

    def shut_down(self) -> None:
        """Stop the top-up, and refuse to start one from now on."""
        with self._lock:
            self.stop.set()

    def _run(self, left: list) -> None:
        try:
            _top_up_core_concepts(left, self.stop)
        except Exception as exc:  # noqa: BLE001 - logged; the app keeps serving
            logger.warning("Core-concept top-up ended early (%s); the next start sends the "
                           "core concepts it had left", type(exc).__name__)


#: The one core-concept top-up of this process.
_core_concept_topup = _CoreConceptTopUp()


def _seed_outcome(total: int, not_confirmed: list, not_sent: list) -> str:
    """How many core concepts the seed confirmed, and the ones it did not
    confirm or did not send."""
    parts = [f"{total - len(not_confirmed) - len(not_sent)} of {total} core concepts confirmed"]
    if not_confirmed:
        parts.append(f"{len(not_confirmed)} not confirmed: {', '.join(not_confirmed)}")
    if not_sent:
        parts.append(f"{len(not_sent)} not sent (the {CORE_CONCEPT_SEED_BUDGET_S}s seed "
                     f"budget was spent): {', '.join(not_sent)}")
    return "; ".join(parts)


def _restore_seed_core_concepts() -> None:
    """Seed core concepts (idempotent) so the graph always carries the
    universal manufacturing vocabulary after a deploy. This runs in-VPC
    because the Neptune graph is private and is not reachable from the
    deploy host; ON CREATE keeps it safe to run on every startup. Bounded by
    ``CORE_CONCEPT_SEED_BUDGET_S``. What it leaves goes to the background
    top-up (:class:`_CoreConceptTopUp`), which this only starts. Logs one
    line: how many concepts the graph confirmed, the ones it did not confirm
    and the ones not sent, and what sends those again."""
    try:
        from .catalog import CORE_CONCEPT_DEFINITIONS
        not_confirmed, not_sent = _send_core_concepts(CORE_CONCEPT_SEED_BUDGET_S)
        left = not_confirmed + not_sent
        line = _seed_outcome(len(CORE_CONCEPT_DEFINITIONS), not_confirmed, not_sent)
        if not left:
            logger.info("%s", line)
        elif not _core_concept_topup.start(left, lambda: logger.warning(
                "%s; a background top-up sends the %d left again, for up to %d min",
                line, len(left), CORE_CONCEPT_TOPUP_BOUND_S // 60)):
            logger.warning("%s; the next start sends them again", line)
    except Exception:
        logger.warning("Core-concept seeding skipped", exc_info=True)


def _restore_sql_systems() -> set[str]:
    """Restore SQL-based systems (PostgreSQL) — require SchemaEntry nodes in Neptune.

    The node's REAL connection_type is read so a non-PostgreSQL system that ALSO
    carries a SchemaEntry (SiteWise (Direct) / Athena / i3X / REST / MCP all get
    one at provision time) is NOT relabeled 'PostgreSQL' and NOT given a
    PostgreSQL SQL-agent here — it is left to the direct-source sweep, which
    restores it with its actual connection_type. This is the durability
    root-fix for the startup-hydration mislabel: get_catalog() shows each
    system's real type after a restart. Returns the system ids this sweep
    restored, so the direct-source sweep can guard against double-processing a
    system returned by BOTH queries (a PostgreSQL node carries a SchemaEntry, so
    it matches the SQL query, and — defensively — must never also be processed
    by the non-PostgreSQL direct sweep). Each row is judged by
    :func:`_restore_skip` first (removal, then the onboarding marker), as in
    the direct-source sweep.
    """
    neptune_systems = neptune_query(
        "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry) "
        "RETURN DISTINCT s.id AS id, s.name AS name, s.schema AS schema, "
        "s.plant AS plant, s.connection_type AS connection_type, "
        "s.system_type AS system_type, s.connection_origin AS connection_origin, "
        "s.removal_incomplete AS removal_incomplete, s.removal_full AS removal_full, "
        "s.status AS status, s.connection_config AS connection_config, "
        "s.secret_arn AS secret_arn"
    )
    if not neptune_systems:
        logger.info("No fully-onboarded SQL systems found in Neptune — ready for onboarding")
    restored_pg_ids: set[str] = set()
    for ns in (neptune_systems or []):
        _restore_sql_system(ns, restored_pg_ids)
    return restored_pg_ids


def _restore_sql_system(ns: dict, restored_pg_ids: set) -> None:
    """Restore ONE system of the SQL sweep (``return`` skips it)."""
    sid = ns.get('id', '')
    schema = ns.get('schema', '')
    if not sid or not schema:
        return
    if _restore_skip(ns):
        return
    # A blank/absent connection_type is a legacy PG system (provisioned before
    # the node carried the type) — default to PostgreSQL for backward compat.
    real_ctype = (ns.get('connection_type') or '').strip() or 'PostgreSQL'
    if real_ctype != 'PostgreSQL':
        # Non-PostgreSQL system that happens to carry a SchemaEntry — do NOT
        # mislabel it or start a PostgreSQL agent. The direct-source sweep
        # hydrates it with its real connection_type.
        logger.info("Skipping SQL-restore for %s — real connection_type=%s (handled by direct-source sweep)", sid, real_ctype)
        return
    # A PostgreSQL system restored by this SQL sweep — record it so the
    # direct-source sweep never processes it a second time.
    restored_pg_ids.add(sid)
    _restore_sql_catalog_entry(ns, sid, schema)
    # The origin marker comes from the node's own bound secret, BEFORE (and
    # independent of) table introspection: a user system whose database is
    # unreachable at startup must not come back looking like a demo system.
    from .connection_resolver import resolve_connection
    resolved = resolve_connection(sid)
    is_user = _restore_mark_user_origin(sid, resolved, ns.get('connection_origin'))
    # A USER login is checked BEFORE its tables are read or its agent starts.
    agent_conn_params = _restore_user_conn_params(sid, schema, resolved) if is_user else None
    if is_user and agent_conn_params is None:
        return  # logged, left unregistered: no agent
    table_details = _restore_sql_table_details(schema)
    if table_details:
        # Full per-table schema (column name + data type) so the restored agent
        # composes queries DIRECTLY from its prompt instead of re-running
        # information_schema per sub-question (the dominant Mode A latency source).
        from .system_agent import format_tables_desc
        tables_desc = format_tables_desc(table_details)
        if agent_conn_params is None:
            agent_conn_params = _restore_sql_conn_params(sid, resolved)
        if agent_conn_params is None:
            return  # isolated: logged, left unregistered
        mcp_url = _restore_one(
            system_id=sid, system_name=ns.get('name', schema),
            schema=schema, system_prompt=_restore_query_notes(sid, schema, resolved, is_user),
            tables_desc=tables_desc, conn_params=agent_conn_params,
        )
        if not mcp_url:
            return  # isolated: logged, left unregistered
        logger.info("Restored agent: %s at %s (host=%s)", sid, mcp_url, agent_conn_params.get('host', '?'))


def _restore_query_notes(sid: str, schema: str, resolved: dict, is_user: bool) -> str:
    """The QUERY HINTS block a restored PostgreSQL agent gets from its manifest
    registration's ``agent_query_notes`` (``onboard._agent_query_notes_block``,
    the block provision appends), ``""`` when it has none.

    A restore that cannot take the cloud fast path, or runs locally, rewrites
    ``adt/agent-config/<id>`` as a whole value, so an empty prompt here drops
    the notes provision wrote. Only a registration whose origin is decided as
    not a user's gets them: a user registration (node origin or bound own
    secret) never does, and neither does a node pointing at its own secret
    that could not be read. The notes follow the restored schema. Reads no
    catalog entry: the origin is the one the restore already decided."""
    from .connection_resolver import is_own_connection_secret
    if is_user:
        return ""
    if resolved.get('_secret_unresolved') and is_own_connection_secret(
            resolved.get('secret_arn'), sid):
        return ""
    from .onboard import _agent_query_notes_block
    return _agent_query_notes_block({'id': sid, 'schema': schema}, sid)


def _restore_user_conn_params(sid: str, schema: str, resolved: dict) -> dict | None:
    """A USER PostgreSQL system's restore connect params, or ``None`` (logged)
    when they cannot be built or its login is refused
    (:func:`_restore_login_refused`). A user system onboarded before the login
    check existed, or one a late provision refusal left onboarded, must not
    come back with a privileged login on a restart."""
    params = _restore_sql_conn_params(sid, resolved)
    if params is None or _restore_login_refused(sid, schema, params):
        return None
    return params


def _restore_login_refused(sid: str, schema: str, conn_params: dict) -> bool:
    """Whether a USER PostgreSQL system's effective login must not be
    restored: the catalog shows it is a superuser, or can create objects or
    change data in ``schema`` (``onboard_preflight.check_pg_login_params``,
    the check onboarding runs). ``True`` is logged with the system id and the
    fix; no password or host is logged. A check that cannot run keeps the
    restore (``False``) and logs its warning."""
    from . import onboard_preflight
    refusal, unchecked = onboard_preflight.check_pg_login_params(conn_params, schema)
    if refusal:
        logger.warning("Restore of %s skipped: %s No agent was started; it stays "
                       "unregistered until its connection is edited to a dedicated "
                       "read-only login. Continuing with the other systems.", sid, refusal)
        return True
    for note in unchecked:
        logger.warning("Restore of %s: %s", sid, note)
    return False


def _restore_sql_catalog_entry(ns: dict, sid: str, schema: str) -> None:
    """Mark the system onboarded in the catalog (adding it when absent), and
    restore the durable, free-form system_type label (set at runtime via
    PUT /api/admin/system-type) so it survives the ECS restart / catalog
    re-hydrate. Descriptive metadata only — concept precedence is
    AuthorityRule-driven, not system_type (see metric_resolver)."""
    found = False
    for s in catalog.get_all():
        if s['id'] == sid:
            catalog.update_status(sid, 'onboarded')
            found = True
            break
    if not found:
        catalog.add({
            'id': sid, 'name': ns.get('name', sid), 'plant': ns.get('plant', ''),
            'system_type': ns.get('system_type') or '', 'connection_type': 'PostgreSQL',
            'connection_string': '', 'schema': schema, 'status': 'onboarded',
        })
    _stype = ns.get('system_type')
    if _stype:
        catalog.set_system_type(sid, _stype)


def _restore_sql_table_details(schema: str) -> dict:
    """``{table: [(column, data_type), ...]}`` for the schema (``{}`` on failure)."""
    table_details = {}
    try:
        tables_result = pg(schema, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))
        for row in tables_result.get('rows', []):
            tbl = row[0]
            cols_result = pg(schema, "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (schema, tbl))
            table_details[tbl] = [(r[0], r[1]) for r in cols_result.get('rows', [])]
    except Exception:
        logger.warning("Table introspection failed for %s", schema, exc_info=True)
    return table_details


def _restore_sql_conn_params(sid: str, resolved: dict | None = None) -> dict | None:
    """Connection params from the System node via resolve_connection ->
    pg_conn_params (TTL-cached), not the catalog (Step-3 Wave-2).
    pg_conn_params fail-soft env-bootstraps the credential when the node's
    secret_arn is empty (e.g. local Memgraph).

    A USER registration (its node points at its own ``adt/conn/<id>`` secret,
    bound to it) gets its catalog origin marker back — the durable marker that
    survives the restart. When that own secret cannot be read or has no user
    and password, or the node names a host/database other than the env's own
    (the env credentials fill only the env target), pg_conn_params refuses
    (never the deployment's credentials): ``None`` skips exactly this one
    system, logged. (No catalog entry is passed: a restart rebuilds the entry
    from the graph, so a user system is recognised by its own pointer — the
    restore reads no catalog for connection data.) ``resolved``: the node's
    connection the caller already resolved (one read per restored system)."""
    from .connection_resolver import pg_conn_params, resolve_connection
    from .errors import ConnectionSecretUnavailableError
    if resolved is None:
        resolved = resolve_connection(sid)
        _restore_mark_user_origin(sid, resolved)   # the sweep already marked otherwise
    try:
        return pg_conn_params(resolved)
    except ConnectionSecretUnavailableError as exc:
        logger.warning("Restore of %s skipped: %s — it stays unregistered until its "
                       "connection is fixed; continuing with the other systems", sid, exc)
        return None


def _restore_mark_user_origin(sid: str, resolved: dict, node_origin=None) -> bool:
    """Give a USER registration its catalog origin marker back after a restart:
    the durable marker is the binding in its own ``adt/conn/<id>`` secret
    (``connection_origin`` + ``system_id``), which ``resolve_connection`` merged
    into ``resolved`` through the node's exact own pointer, or the origin
    provision recorded on the System node (``node_origin``, the restore row's
    ``s.connection_origin``: the only marker an unauthenticated MCP has). Only
    ``user`` ever marks; every other system (demo/workshop, HighByte,
    admin-edited, legacy) is left as the restore built it. Returns whether it
    marked (the system is a user registration); no catalog read."""
    from .connection_resolver import (ORIGIN_USER, is_bound_user_secret,
                                      is_own_connection_secret)
    if node_origin == ORIGIN_USER or (is_own_connection_secret(resolved.get('secret_arn'), sid)
                                      and is_bound_user_secret(resolved, sid)):
        catalog.set_connection_origin(sid, ORIGIN_USER)
        return True
    return False


def _restore_direct_source_systems(restored_pg_ids: set) -> None:
    """Restore direct-source (non-SQL) systems by their REAL connection_type on
    the node — INDEPENDENT of whether the system also carries a SchemaEntry.
    This is a durability root-fix: SiteWise (Direct) and Athena systems DO
    carry a SchemaEntry (SiteWise writes schema='sitewise-direct'; Athena
    writes one per table), so the SQL sweep MATCHes them but SKIPs them (real
    connection_type != 'PostgreSQL'). If this sweep also filtered them out —
    e.g. by excluding systems that have a SchemaEntry — such a system would be
    restored by NEITHER sweep and VANISH from the catalog on restart (observed
    live: get_catalog() empty after a redeploy; every such system had to be
    re-onboarded). Selecting on connection_type alone — via the canonical
    DIRECT_SOURCE_CONNECTION_TYPES set, which is exactly the non-PostgreSQL,
    non-KnowledgeGraph types that need a direct agent — guarantees every such
    system is re-hydrated with its true type + agent. The query does not filter
    on the node's status; each returned node is judged by
    :func:`_restore_skip`: a node without the onboarding marker
    (``catalog.node_is_onboarded``, written by every provision path; a node an
    Edit before the first provision wrote has none) is skipped and logged, and
    the skip-if-unconfigured guard still drops any node with no usable
    connection target. Type list is centralized (DRY) in connection_types. A
    node whose removal did not finish (``removal_incomplete``, of ANY type: a
    PostgreSQL node that lost its SchemaEntry is invisible to the SQL sweep) is
    also returned here and is never served (:func:`_restore_removal_failed`)."""
    from .connection_types import DIRECT_SOURCE_CONNECTION_TYPES
    _direct_types_literal = ", ".join(f"'{t}'" for t in DIRECT_SOURCE_CONNECTION_TYPES)
    mcp_systems = neptune_query(
        f"MATCH (s:System) WHERE s.connection_type IN [{_direct_types_literal}] "
        "OR s.removal_incomplete = true "
        "RETURN s.id AS id, s.name AS name, s.plant AS plant, s.connection_type AS connection_type, "
        "s.system_type AS system_type, s.connection_origin AS connection_origin, "
        "s.schema AS schema, s.removal_incomplete AS removal_incomplete, "
        "s.removal_full AS removal_full, s.status AS status, "
        "s.connection_config AS connection_config, s.secret_arn AS secret_arn"
    )
    from . import field_descriptions
    # One bounded read of every listed system's described fields, so each
    # restore below rebuilds its record without a graph read of its own.
    with field_descriptions.prefetched(ns.get('id') for ns in (mcp_systems or [])):
        for ns in (mcp_systems or []):
            _restore_direct_source_system(ns, restored_pg_ids)


def _restore_removal_failed(ns: dict) -> None:
    """A system whose removal started and did not finish (its node carries
    ``removal_incomplete``): no agent and never 'onboarded'. Its catalog entry
    is rebuilt in the ``removal_failed`` state (``catalog.ensure_removal_failed``)
    so the card offers only Remove again, and its user origin is marked back so
    a full retry still finds its own secret."""
    sid = ns.get('id', '')
    if not sid:
        return
    catalog.ensure_removal_failed(ns)
    try:
        from .connection_resolver import resolve_connection
        resolved = resolve_connection(sid)
    except Exception:  # noqa: BLE001 - the origin marker is best-effort here
        resolved = {}
    _restore_mark_user_origin(sid, resolved or {}, ns.get('connection_origin'))
    logger.warning("Restore: %s has an unfinished removal; not served, shown for a retry", sid)


def _restore_skip(ns: dict) -> bool:
    """Whether a restore sweep serves no agent for this node (``True`` when it
    is handled here); both sweeps apply it before anything else. A node whose
    removal did not finish is rebuilt ``removal_failed``
    (:func:`_restore_removal_failed`); a node without the onboarding marker
    (``catalog.node_is_onboarded``) is left out of the catalog: an admin Edit
    before the first provision writes the system's connection onto its node,
    and that never onboarded it. A SchemaEntry is not onboarding evidence
    either (the orphan-schema repair re-attaches entries to whatever node holds
    the id)."""
    from .catalog import node_is_onboarded
    if ns.get('removal_incomplete'):
        _restore_removal_failed(ns)    # never served; shown only for a retry
        return True
    if not node_is_onboarded(ns):
        logger.info("Restore: %s skipped: its System node was never onboarded (it holds "
                    "connection settings only, status=%r)", ns.get('id', ''), ns.get('status'))
        return True
    return False


def _restore_direct_source_system(ns: dict, restored_pg_ids: set) -> None:
    """Restore ONE system of the direct-source sweep (``return`` skips it)."""
    sid = ns.get('id', '')
    if not sid:
        return
    if _restore_skip(ns):
        return
    # Double-processing guard: if the SQL sweep already restored this system as
    # PostgreSQL, do not process it here too. A PostgreSQL node should never
    # carry a direct-source connection_type, so this keeps the two sweeps
    # strictly disjoint without changing PostgreSQL restore behavior.
    if sid in restored_pg_ids:
        return
    # Step-3 Wave-2: resolve the connection from the System node (TTL-cached);
    # no catalog connection read. Identity (name) comes from the restore Cypher.
    from .connection_resolver import resolve_connection
    resolved = resolve_connection(sid)
    # Skip-if-unconfigured guard: a node with no usable connection target is
    # not registered — log and skip. Beyond the URL/secret markers this also
    # accepts a typed Athena target (``workgroup``/``database``) or a
    # cross-account ``role_arn``, whose connection is a boto3 workgroup/role
    # rather than a URL — otherwise such a system would be silently skipped.
    if resolved.get('_unconfigured') or not (
            resolved.get('url') or resolved.get('base_url')
            or resolved.get('connection_string') or resolved.get('secret_arn')
            or resolved.get('workgroup') or resolved.get('database')
            or resolved.get('role_arn')):
        logger.warning("Direct-source system %s has no resolved connection — skipping restore", sid)
        return
    catalog.update_status(sid, 'onboarded')
    # Dispatch the system's REAL connection_type from the node (Athena /
    # SiteWise (Direct) / MCP Server / ...) — NOT a hardcoded 'MCP Server' — so
    # each system restores with the correct connector and re-assumes any
    # cross-account role from the persisted config (auto-refresh continues).
    _ctype = ns.get('connection_type') or resolved.get('connection_type') or 'MCP Server'
    _restore_direct_catalog_entry(ns, sid, _ctype)
    _restore_mark_user_origin(sid, resolved, ns.get('connection_origin'))
    mcp_url = _restore_one(
        system_id=sid,
        system_name=ns.get('name', sid),
        schema='',
        system_prompt="",
        tables_desc=_restore_direct_tables_desc(sid, _ctype),
        connection_type=_ctype,
        connection_config=_restored_connection_config(sid, resolved),
    )
    if not mcp_url:
        return  # isolated: logged, left unregistered
    logger.info("Restored direct-source agent: %s (type=%s)", sid, _ctype)


def _restored_connection_config(system_id: str, resolved: dict) -> dict:
    """A direct-source system's restore connection config: ``resolved`` plus
    the described-field record rebuilt from the graph's ``Field`` types
    (``field_descriptions.rebuilt``), so a runtime the restore recreates
    still lists what each field holds. When the graph cannot be read, the
    record of the system's current agent-config is kept (read with the
    bounded ``field_descriptions.fallback_client_config``)."""
    from . import field_descriptions
    def current():
        return _agent_manager.current_agent_config(
            system_id, client_config=field_descriptions.fallback_client_config())
    return field_descriptions.rebuilt(system_id, resolved, current=current)


def _restore_direct_catalog_entry(ns: dict, sid: str, _ctype: str) -> None:
    """Ensure the catalog carries the REAL connection_type after a restart even
    when this system was NOT already present (e.g. it was skipped by the SQL
    sweep because its real type is non-PostgreSQL). Existence is checked via
    get_all() — never catalog.get() — so _restore keeps sourcing all connection
    data from the node, not the catalog. Then restore the durable, free-form
    system_type label (set at runtime via PUT /api/admin/system-type) so it
    survives the restart / re-hydrate. Descriptive metadata only — precedence
    is AuthorityRule-driven, not system_type."""
    if not any(s['id'] == sid for s in catalog.get_all()):
        catalog.add({
            'id': sid, 'name': ns.get('name', sid), 'plant': ns.get('plant', ''),
            'system_type': ns.get('system_type') or '', 'connection_type': _ctype,
            'connection_string': '', 'schema': '', 'status': 'onboarded',
        })
    _stype = ns.get('system_type')
    if _stype:
        catalog.set_system_type(sid, _stype)


def _restore_direct_tables_desc(sid: str, _ctype: str) -> str:
    """For the direct-source physical-schema engines (Athena, S3 Tables),
    rebuild the FULL per-table schema (column name + data type, partition keys
    flagged) from the graph SchemaEntry->Field nodes captured at discover time,
    so the restored agent composes queries DIRECTLY from its prompt instead of
    re-running list_tables/describe per sub-question. Asset-based types
    (SiteWise) have no SQL schema and stay tables_desc='' (unchanged)."""
    from .connection_types import ATHENA as _ATHENA, S3TABLES_DUCKDB as _S3TABLES
    restore_tables_desc = ''
    if _ctype in (_ATHENA, _S3TABLES):
        # S3 Tables uses the SAME graph shape: its SchemaEntry/Field nodes are
        # persisted at provision with the qualified SchemaEntry names and the
        # partition flags (the physical-fields write), so one rebuilder serves
        # both. Without this an S3 Tables system restored with tables_desc="",
        # so the agent lost its baked column list and the window rules never
        # fired after any app restart.
        try:
            restore_tables_desc = _sql_tables_desc_from_graph(sid, _ctype)
        except Exception:
            logger.warning("schema rebuild from graph failed for %s", sid, exc_info=True)
            restore_tables_desc = ''
    return restore_tables_desc


_restore_base = _restore
_restore_legacy = _restore_base


def _with_restored_distinct_identifiers(system_id: str, kwargs: dict) -> dict:
    """A PostgreSQL system's restore ``start_agent`` kwargs plus what its
    query tool's shape guard is built from (ADR-0098).

    The restore rebuilds the system's schema from the graph, and when its
    runtime is gone or not READY (or in local mode) the agent config is
    written from these kwargs, so without this the guard was lost. The
    records come from the graph, read the way a provision reads them back
    (:func:`_restored_distinct_identifier_records`); with none there, from
    the system's current agent config, for a system provisioned before the
    graph held the pairs (:func:`_with_carried_forward_identifiers`). An
    unreadable graph, or a stored row the guard cannot use, raises (logged):
    the restore's per-system isolation then leaves this one system
    unregistered rather than rewrite its config without a guard."""
    records = _restored_distinct_identifier_records(system_id)
    if records:
        return _with_identifier_records(kwargs, records)
    return _with_carried_forward_identifiers(system_id, kwargs)


def _restored_distinct_identifier_records(system_id: str) -> list[dict]:
    """The graph's records of the system (``onboard.read_distinct_identifier_records``);
    raises ``RuntimeError`` (logged with the cause's category) when the graph
    cannot be read or holds a row that is not a usable record."""
    from .db import GraphQueryError
    from .distinct_identifiers import UnusableRecordError
    from .onboard import read_distinct_identifier_records
    try:
        return read_distinct_identifier_records(system_id)
    except (GraphQueryError, UnusableRecordError) as exc:
        cause = getattr(exc, "category", None) or type(exc).__name__
        logger.warning("Restore of %s: its measured distinct-identifier records could not be "
                       "read from the graph (%s); not restoring it without them",
                       system_id, cause)
        raise RuntimeError("distinct-identifier records unusable") from exc


def _with_identifier_records(kwargs: dict, records: list) -> dict:
    """``kwargs`` with the note rendered from ``records`` onto ``tables_desc``
    (the provision's own text) and the records in ``connection_config`` as
    ``distinct_identifiers``, the provision's channel."""
    from .onboard import _with_distinct_identifier_records
    from .schema_format import with_distinct_identifiers
    return {**kwargs,
            "tables_desc": with_distinct_identifiers(kwargs.get("tables_desc") or "", records),
            "connection_config": _with_distinct_identifier_records(
                kwargs.get("connection_config"), records)}


def _with_carried_forward_identifiers(system_id: str, kwargs: dict) -> dict:
    """For a system the graph holds no record for: what its CURRENT
    ``adt/agent-config/<id>`` gives the guard, carried onto ``kwargs``, so a
    system provisioned before the graph held the pairs keeps its guard when
    the restore rewrites that config. Only in cloud mode (local mode keeps no
    config across a restart), and only for the same host and database.

    Its ``distinct_identifiers`` when every one is a usable record
    (``distinct_identifiers.signed_records``); else its baked note, when the
    fallback parser reads at least one pair with shapes from it and each such
    pair's two columns are in the restored schema; else nothing. Nothing is
    written to the graph here: the next provision stores the pairs. A secret
    that cannot be read raises ``AgentConfigUnreadable``; the restore wrapper
    (:func:`_restore_integrity`) then decides whether the system can still be
    registered without rewriting it."""
    if getattr(_agent_manager, "mode", "") != "cloud":
        return kwargs
    from .agent_manager import _same_pg_database
    current = _agent_manager.current_agent_config(system_id)
    if not isinstance(current, dict) or not _same_pg_database(
            current, kwargs.get("conn_params") or {}):
        return kwargs
    from .distinct_identifiers import signed_records
    records = signed_records(current.get("distinct_identifiers"))
    if records:
        logger.info("Restore of %s: carried forward its %d measured distinct-identifier "
                    "records from its agent config", system_id, len(records))
        return _with_identifier_records(kwargs, records)
    note = _carried_note(current.get("tables_desc"), kwargs.get("tables_desc") or "")
    if not note:
        return kwargs
    from .schema_format import with_distinct_identifier_note
    logger.info("Restore of %s: carried forward its distinct-identifier note from its agent "
                "config (no graph record yet)", system_id)
    return {**kwargs, "tables_desc": with_distinct_identifier_note(
        kwargs.get("tables_desc") or "", note)}


def _carried_note(old_desc, new_desc: str) -> str:
    """The note baked in ``old_desc`` when the guard's fallback parser reads
    at least one pair with shapes from it and every such pair names two
    columns ``new_desc`` (the restored schema) holds; else ``""``."""
    from .response_integrity_sql import parse_physical_schema
    from .schema_format import distinct_identifier_note, distinct_identifier_pairs
    note = distinct_identifier_note(old_desc)
    pairs = distinct_identifier_pairs(note)
    if not pairs:
        return ""
    tables = {t.lower(): {c.lower() for c in (entry.get("columns") or ())}
              for t, entry in parse_physical_schema(new_desc).items()}
    for pair in pairs:
        for table in (pair["table_a"], pair["table_b"]):
            if pair["column"].lower() not in tables.get(table.lower(), set()):
                return ""
    return note


def _restore_integrity():
    """Restore systems while replacing PostgreSQL legacy schema with graph provenance."""
    original = _agent_manager.start_agent

    def start_with_provenance(*args, **kwargs):
        from .connection_types import ATHENA
        connection_type = kwargs.get("connection_type", "PostgreSQL")
        system_id = kwargs.get("system_id", "")
        if connection_type in {"PostgreSQL", ATHENA} and system_id:
            graph_schema = _sql_tables_desc_from_graph(system_id)
            source_schema = kwargs.get("tables_desc", "") or graph_schema
            missing = (
                not graph_schema or "[unknown]" in graph_schema
                or ": unknown" in graph_schema or ": )" in graph_schema
            )
            if source_schema and missing:
                try:
                    _reconcile_restored_sql_provenance(
                        system_id, kwargs.get("schema", ""), connection_type,
                        kwargs.get("connection_config") or {}, source_schema,
                    )
                except Exception as exc:
                    logger.warning(
                        "SQL provenance reconciliation failed system=%s category=%s",
                        system_id, type(exc).__name__,
                    )
                    raise RuntimeError("SQL provenance reconciliation failed") from exc
                graph_schema = _sql_tables_desc_from_graph(system_id)
            if graph_schema:
                kwargs["tables_desc"] = graph_schema
        if connection_type == "PostgreSQL" and system_id:
            from .agent_manager import AgentConfigUnreadable, FastPathUnavailable
            try:
                kwargs = _with_restored_distinct_identifiers(system_id, kwargs)
            except AgentConfigUnreadable:
                # Its current config could not be read, so whatever guard it
                # holds can't be carried. A READY runtime registers by the
                # fast path, which leaves that config as it is; any other path
                # would rewrite it without the guard, so refuse this one system.
                # start_agent checks the fast path once and acts on that answer.
                try:
                    return original(*args, **{**kwargs, "fast_path_only": True})
                except FastPathUnavailable:
                    logger.warning("Restore of %s: its agent config could not be read and its "
                                   "runtime must be recreated; not rewriting the config "
                                   "without what it holds for the identifier guard", system_id)
                    raise RuntimeError("agent config unreadable") from None
        return original(*args, **kwargs)

    _agent_manager.start_agent = start_with_provenance
    try:
        # One complete gateway-target listing per gateway for the whole pass,
        # not one per restored system (startup latency vs ALB health checks).
        with gateway_target_snapshot():
            return _restore_legacy()
    finally:
        _agent_manager.start_agent = original


_restore = _restore_integrity


# ── Self-learning auto-accept triggers (flag-gated, best-effort, non-blocking) ─
# Two triggers reuse the ONE shared sweep (self_learning.sweep_auto_accept — the
# same function the /api/proposed-mappings/accept-high-confidence route calls):
#   1. a periodic scheduler safety-net (register_auto_accept_job), and
#   2. a post-/api/ask background hook (_auto_accept_background_task).
# Both no-op unless SELF_LEARNING_AUTO_ACCEPT is truthy, and neither may raise
# into the request path or crash the scheduler.
_AUTO_ACCEPT_SWEEP_INTERVAL_SECONDS = 300  # 5 min safety-net


def _run_auto_accept_sweep(source: str = "sweep") -> dict:
    """Run the shared high-confidence auto-accept sweep, best-effort.

    Never raises: a failure is logged (debug) and swallowed so it can neither crash
    the scheduler nor surface into the request path. Logs a concise INFO line only
    when at least one mapping is accepted. Returns the sweep result (or a zero
    result on failure) to aid testing. This is the single wrapper both the periodic
    job and the post-query hook call — they share the ONE sweep with no duplicated
    accept logic."""
    try:
        from .self_learning import sweep_auto_accept
        result = sweep_auto_accept()
    except Exception:  # nosec B110 - best-effort: must never crash scheduler/request
        logger.debug("%s auto-accept sweep failed (non-fatal)", source)
        return {"accepted": 0, "failed": 0}
    if result.get("accepted", 0) > 0:
        logger.info("%s auto-accept: accepted %d mapping(s) (failed=%d)",
                    source, result.get("accepted", 0), result.get("failed", 0))
    return result


def register_auto_accept_job():
    """Register the periodic background jobs on the SHARED scheduler.

    THREE registrations ride this lifespan seam (keeping ``lifespan`` itself
    untouched per the maintainability gate): the E5 schema-drift daily check
    (ADR-0053 — files schema_drift PROPOSALS via log_proposed_mapping, never
    mutates the graph; DRIFT_CHECK_INTERVAL_SECONDS tunable, 0 disables), the
    bounded triage annotator (ADR-0062 D11 — default-OFF, see
    ``register_nightly_audit_job``; registered BEFORE the auto-accept flag gate
    so its own flag is the only thing that decides it), and the auto-accept
    safety-net, gated on SELF_LEARNING_AUTO_ACCEPT — a no-op when the flag is
    falsy/unset. Reuses the existing single AsyncIOScheduler via
    ``prompts.add_interval_job`` (no second scheduler); the seam runs AFTER
    ``start_scheduler()`` in the lifespan, which is what makes registration
    land on a live scheduler."""
    from .schema_drift import register_drift_check_job
    register_drift_check_job()
    register_nightly_audit_job()
    if not os.environ.get("SELF_LEARNING_AUTO_ACCEPT"):
        logger.info("Self-learning auto-accept disabled (flag unset) — no scheduled job")
        return
    add_interval_job(
        "self-learning-auto-accept",
        lambda: _run_auto_accept_sweep("scheduled"),
        _AUTO_ACCEPT_SWEEP_INTERVAL_SECONDS,
    )
    logger.info("Self-learning auto-accept job registered (every %ss)",
                _AUTO_ACCEPT_SWEEP_INTERVAL_SECONDS)


# ── Bounded triage annotation (ADR-0062 D11) — DEFAULT-OFF SCHEDULING ─────────
# The annotator has ONE entry (``audit_scheduler.request_audit``) and the admin
# one-shot route drives it regardless of this flag — that route is the live test
# path. NIGHTLY_AUDIT_ENABLED gates REGISTRATION only: unset/falsy and no job is
# ever registered, so a deployed environment schedules nothing until a steward
# opts in. The tick calls the SAME entry with the SAME arguments as the button;
# ``trigger`` is provenance only.
def register_nightly_audit_job():
    """Register the nightly bounded-triage-annotation job when opted in.

    Rides the same lifespan seam as the learning jobs (after
    ``start_scheduler()``) and the same shared AsyncIOScheduler via
    ``prompts.add_interval_job`` — no second scheduler. A no-op when
    NIGHTLY_AUDIT_ENABLED is falsy/unset (the default)."""
    from . import audit_scheduler
    if not audit_scheduler.scheduled_enabled():
        logger.info("Nightly bounded triage annotation disabled (%s unset) — "
                    "no scheduled job; the one-shot admin route still works",
                    audit_scheduler.SCHEDULED_FLAG)
        return
    add_interval_job(audit_scheduler.SCHEDULED_JOB_ID, _run_scheduled_audit,
                     audit_scheduler.SCHEDULED_INTERVAL_SECONDS)
    logger.info("Nightly bounded triage annotation registered (every %ss)",
                audit_scheduler.SCHEDULED_INTERVAL_SECONDS)


def _run_scheduled_audit() -> None:
    """ONE scheduler tick: request a run with the ``scheduled`` trigger.

    Never retries and never raises: a refusal (a run already active, or the
    global daily budget exhausted/unavailable) is already recorded as a
    ``skipped_budget`` run by the entry, and a raise here would kill the
    scheduler job."""
    from . import audit_scheduler
    try:
        run_id, reason = audit_scheduler.request_audit(
            audit_scheduler.TRIGGER_SCHEDULED)
    except Exception:  # nosec B110 - best-effort: must never crash the scheduler
        logger.debug("scheduled bounded triage annotation failed (non-fatal)")
        return
    if run_id is None:
        logger.info("scheduled bounded triage annotation skipped: %s", reason)


def _auto_accept_background_task():
    """Return a ``BackgroundTask`` that runs the shared sweep after the /api/ask
    stream finishes, or ``None`` when SELF_LEARNING_AUTO_ACCEPT is falsy/unset.

    Starlette runs the response ``background`` only AFTER the body is fully sent, so
    a mapping discovered during the query is swept right after WITHOUT delaying the
    user response. Sync callables run in a threadpool, so the (blocking) sweep does
    not block the event loop. Best-effort: ``_run_auto_accept_sweep`` never raises."""
    if not os.environ.get("SELF_LEARNING_AUTO_ACCEPT"):
        return None
    return BackgroundTask(_run_auto_accept_sweep, "post-query")


def _backfill_demo_metadata(entry: dict, sysdef: dict) -> list[str]:
    """Heal a demo system's DEGENERATE display metadata in place from the manifest.

    An already-present demo system (restored from Neptune, or a repeat register)
    may carry degenerate DISPLAY fields written by an earlier provision whose
    request body had no plant/name — ``name == id`` and/or an empty ``plant``. This
    replaces ONLY those degenerate values with the manifest's canonical ``name`` /
    ``plant`` (never overwrites a real, non-degenerate value; never touches
    connection/schema/status). Mutates ``entry`` (the live catalog dict, as
    ``catalog.set_system_type`` does) and returns the list of fields healed.
    """
    sid = entry.get("id", "")
    healed: list[str] = []
    want_name = (sysdef.get("name") or "").strip()
    # ``name`` is degenerate when it is blank or equals the id (the slug fallback).
    if want_name and want_name != sid and (entry.get("name") or "").strip() in ("", sid):
        entry["name"] = want_name
        healed.append("name")
    want_plant = (sysdef.get("plant") or "").strip()
    # ``plant`` is degenerate only when blank — a real non-empty plant is kept.
    if want_plant and not (entry.get("plant") or "").strip():
        entry["plant"] = want_plant
        healed.append("plant")
    return healed


def _backfill_registration_prose(entry: dict, sysdef: dict) -> list[str]:
    """Put the manifest's routing prose back on an entry that has none.

    ``_restore`` rebuilds an onboarded system's catalog entry from its Neptune
    System node without ``capabilities`` / ``query_guidance``. Provision falls
    back to the node's own copy, so that only matters once the node is gone: a
    reset deletes it, and a re-onboard after any app restart then got the generic
    per-connection-type text instead of the system's registration prose. Restores
    each field ONLY when the entry's value is blank and the manifest carries one,
    exactly what the first boot's registration put there; a non-empty value is
    never overwritten. The caller applies it only when the entry's schema is the
    manifest's and the entry is not a user registration (an entry built from
    another story's schema, or registered through Connect New System under the
    same id, is not this manifest system). Mutates ``entry``; returns the fields
    restored.
    """
    healed: list[str] = []
    for field in ("capabilities", "query_guidance"):
        want = sysdef.get(field) or ""
        if want and not (entry.get(field) or "").strip():
            entry[field] = want
            healed.append(field)
    return healed


def _reconcile_demo_schema(entry: dict, sysdef: dict, demo_pg_dsn) -> bool:
    """Correct an existing entry registered with a DIFFERENT schema than the manifest.

    One id can map to two schemas across modes (a workshop storyline schema vs
    the demo/live schema), so a catalog entry left by an earlier boot in the
    other mode would otherwise keep the wrong connection forever. A NOT-onboarded
    entry is rewired to the manifest schema (WARNING); an ONBOARDED entry is left
    as-is (its runtime and graph were built from the old schema) and an ERROR
    names the explicit fix — re-onboarding. Returns ``True`` when rewired.
    """
    sid, want = entry.get("id", ""), sysdef.get("schema", "")
    have = entry.get("schema") or ""
    if not want or not have or have == want:
        return False
    if entry.get("status") == "onboarded":
        logger.error("Demo system %s is onboarded from schema %s but the manifest "
                     "says %s; left unchanged: re-onboarding is required "
                     "(scripts/reonboard.py %s)", sid, have, want, sid)
        return False
    entry["schema"] = want
    entry["connection_string"] = demo_pg_dsn(want)
    logger.warning("Demo system %s: schema %s -> %s (corrected from manifest)",
                   sid, have, want)
    return True


def _heal_existing_demo_system(entry: dict, sysdef: dict, demo_pg_dsn) -> str:
    """Heal an already-present demo system; ``'backfilled'`` or ``'skipped'``."""
    from .connection_resolver import ORIGIN_USER
    healed = _backfill_demo_metadata(entry, sysdef)
    if _reconcile_demo_schema(entry, sysdef, demo_pg_dsn):
        healed.append("schema")
    # The manifest's prose belongs to the manifest's registration only: an entry on
    # another story's schema is not this system, and neither is a USER registration
    # under the same id and schema (Connect New System after a full removal; the
    # restore re-marks it ``connection_origin=user``). That one keeps its own prose:
    # provision falls back to its :System node's copy, never to the manifest.
    if ((entry.get("schema") or "") == (sysdef.get("schema") or "")
            and entry.get("connection_origin") != ORIGIN_USER):
        healed.extend(_backfill_registration_prose(entry, sysdef))
    if healed:
        logger.info("Demo system %s: backfilled %s from manifest",
                    entry.get("id", ""), ",".join(healed))
        return "backfilled"
    return "skipped"


def _register_one_demo_system(sysdef: dict, connect_new_system, demo_pg_dsn) -> str:
    """Register ONE manifest system; return ``'registered'`` / ``'backfilled'`` /
    ``'skipped'`` / ``'error'`` for the caller's tally.

    Idempotent for connection config, but HEALS degenerate DISPLAY metadata:
    a demo system restored from a Neptune System node that an earlier provision
    wrote with no plant / name (name==id, plant empty — the observed MES
    symptom) would otherwise keep those degenerate values forever — `_restore`
    re-adds them to the catalog on every boot, the idempotent skip never
    re-applies the manifest, and a re-onboard reads that same empty catalog
    back into provision (self-perpetuating). Backfill the manifest's canonical
    plant + display name (sourced from the single manifest, not hardcoded) so
    per-plant KG nodes + Line State grouping resolve after the next re-onboard.
    A schema that differs from the manifest is reconciled too
    (``_reconcile_demo_schema``).
    """
    sid = sysdef["id"]
    existing = catalog.get(sid)
    if existing:
        return _heal_existing_demo_system(existing, sysdef, demo_pg_dsn)
    schema = sysdef.get("schema", "")
    res = connect_new_system(
        name=sysdef["name"],
        system_type=sysdef.get("system_type", ""),
        plant=sysdef.get("plant", ""),
        connection_type=sysdef.get("connection_type", "PostgreSQL"),
        connection_string=demo_pg_dsn(schema) if schema else "",
        schema=schema,
        # Optional manifest guidance-through-registration: a manifest entry may
        # pin the routing prose the System node will carry at provision (the
        # workshop storyline systems do). Absent => "" => the per-connection-type
        # default, exactly as before.
        query_guidance=sysdef.get("query_guidance", ""),
        capabilities=sysdef.get("capabilities", ""),
        system_id=sid,
    )
    if isinstance(res, dict) and res.get("error"):
        logger.warning("Demo system registration for %s returned error: %s",
                       sid, res["error"])
        return "error"
    return "registered"


def _register_demo_systems():
    """When the demo addon is enabled, REGISTER the demo SQL systems through the
    SAME ``connect_new_system`` path any system uses — so they appear in
    ``GET /api/onboard/catalog`` (status ``registered``) and can be onboarded.

    Data-mode gate: registers ONLY when ``demo.is_enabled()`` (the ``ADT_DEMO`` env
    flag — set on the ECS task when a seed DB is present — or the runtime override).
    In BASE mode the demo is disabled, so this is a NO-OP and the catalog stays
    EMPTY (ADR-0030). NO demo defaults are baked into the base catalog: each system
    is registered dynamically through the ordinary register endpoint, VERBATIM
    canonical ids (``connect_new_system(system_id=...)``) so the onboarding harness
    can onboard BY id and assert ``adt_<id>`` runtimes.

    Idempotent: a system already present (e.g. restored as ``onboarded`` from
    Neptune, or a repeat call) is skipped via ``catalog.get(id)`` (with display-
    metadata healing — see ``_register_one_demo_system``). Fail-soft per system
    with LOUD logging — one bad system must never crash startup.

    The system set comes from ``demo.systems.manifest_systems()`` — the demo
    manifest, plus the plant systems: in workshop mode (``ADT_WORKSHOP``) the
    story's storyline + register-only systems, and in demo mode the plant MES
    and maintenance (``workshop.DEMO_PLANT_SYSTEMS``). Registration NEVER
    onboards; those systems stay ``registered`` until someone onboards them
    (docs/workshop-mode.md).
    """
    from . import demo
    if not demo.is_enabled():
        logger.info("Demo addon disabled — not registering demo systems (base mode, catalog stays empty)")
        return
    from .demo.systems import manifest_systems
    from .demo.db_routing import demo_pg_dsn
    # The demo hook registers with an explicit demo origin (never a user's own
    # connection) — the same registration implementation as connect_new_system.
    from .onboard_agent import register_demo_system
    systems = manifest_systems()
    tally = {"registered": 0, "backfilled": 0, "skipped": 0, "error": 0}
    for sysdef in systems:
        try:
            outcome = _register_one_demo_system(sysdef, register_demo_system, demo_pg_dsn)
            tally[outcome] += 1
        except Exception:  # noqa: BLE001 - fail-soft: one bad system must not crash startup
            logger.warning("Demo system registration FAILED for %s (continuing)",
                           sysdef.get("id"), exc_info=True)
    logger.info("Demo system registration: %d registered, %d backfilled, %d already present (of %d)",
                tally["registered"], tally["backfilled"], tally["skipped"], len(systems))


#: Seconds startup waits for the graph restore before the app starts serving.
#: A restore still running then finishes on its own thread (no system is cut
#: off), and the startup steps that read or write what it rebuilds wait for
#: it (:class:`_AfterRestore`). Computed from the deploy's health checks in
#: stacks/adt_compute_stack.py, not measured: ECS ignores the ALB and the
#: container /health checks for the service's 300 s grace after the task
#: starts (ECS service API reference), and the ALB target group passes a
#: target after 2 checks 15 s apart, so /health has to answer by
#: 300 - 2 x 15 = 270 s after the task starts. This wait takes 60 s of that
#: and leaves 210 s for the container to start and for the startup steps that
#: do not wait for the restore. The restore itself has no bound under the
#: grace: the core-concept seed alone is bounded at 165 s
#: (``CORE_CONCEPT_SEED_BUDGET_S``; the top-up of what it leaves runs on its
#: own thread, which the restore starts and does not wait for, so it is not in
#: the restore's time), and one system whose runtime has to be
#: created can spend 2165 s in the waits of ``AgentManager._start_cloud``
#: alone (3 creates x ``RUNTIME_READY_TIMEOUT_S`` 600 s, 20 + 60 s between
#: them, the 120 s warmup request timeout and 30 + 9 x 15 s of gateway sync
#: pauses), before any control-plane call time. Both are computed from the
#: code. Measured on one account on 2026-10-23: a 9-system restore took
#: 7.3 s. A restore that ends within this wait starts the app exactly as
#: before.
STARTUP_RESTORE_WAIT_S = 60


class _StartupRestore:
    """The startup restore (``_restore``) on its own daemon thread.

    The thread logs how the restore ended and how long it took, then runs
    what was waiting for it (:meth:`then`). Daemon, so a restore still running
    does not hold the process open at shutdown; the next start runs the
    restore again."""

    def __init__(self) -> None:
        self._started = _restore_clock()
        self._ended = threading.Event()
        self._lock = threading.Lock()
        self._waiting = None
        self.ended_in_time = False

    def start(self) -> None:
        threading.Thread(target=self._run, name="startup-restore", daemon=True).start()

    def _run(self) -> None:
        error = None
        try:
            _restore()
        except BaseException as exc:  # noqa: BLE001 - logged below; the restore must not end silently
            error = exc
        elapsed = self.elapsed()
        if error is None:
            logger.info("Neptune restore finished after %.1fs", elapsed)
        else:
            logger.warning("Neptune restore failed after %.1fs (%s: %s)",
                           elapsed, type(error).__name__, error)
        with self._lock:
            self._ended.set()
            waiting, self._waiting = self._waiting, None
        if waiting is not None:
            waiting()

    def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds; whether the restore has ended."""
        return self._ended.wait(timeout)

    def elapsed(self) -> float:
        return _restore_clock() - self._started

    def then(self, fn) -> None:
        """Run ``fn`` once the restore has ended, always on a thread that is
        not the caller's: the restore's own thread when it ends later, or a
        new one (``startup-held-steps``) when it already has. So a restore
        that ends between the wait and this call still runs nothing on the
        caller, which is the event loop before the app serves."""
        with self._lock:
            if not self._ended.is_set():
                self._waiting = fn
                return
        threading.Thread(target=fn, name="startup-held-steps", daemon=True).start()


def _restore_graph() -> _StartupRestore:
    """Start the startup restore (``_restore``) on its own thread and wait for
    it up to ``STARTUP_RESTORE_WAIT_S``. A restore that ends within the wait
    logs as before: that it finished, or how it failed, and how long it took.
    One still running is logged once and finishes in the background, still
    logging how it ended."""
    restore = _StartupRestore()
    restore.start()
    restore.ended_in_time = restore.wait(STARTUP_RESTORE_WAIT_S)
    if not restore.ended_in_time:
        logger.warning("Neptune restore still running after %.0fs; the app starts serving and "
                       "the restore finishes in the background (changes to onboarded systems "
                       "answer 503 until it ends)", restore.elapsed())
    return restore


class _AfterRestore:
    """The startup steps that read or write what the startup restore rebuilds.

    When the restore ended within ``STARTUP_RESTORE_WAIT_S``, :meth:`run`
    runs each step at once, so every startup step runs in the lifespan's
    order, as before; an off-loop step (blocking work) runs on a worker
    thread while the lifespan awaits it (:meth:`run_off_loop`). Otherwise each
    is held. :meth:`release` (after the last step) puts up the refusal
    (``startup_restore``), and once the restore ends a thread that is not the
    event loop's (``_StartupRestore.then``) runs the held off-loop steps,
    lifts the refusal and hands the other held steps to the event loop, where
    they run in lifespan order after the app starts serving (they start the
    AsyncIOScheduler or create asyncio tasks, which need the running loop).
    Each held step runs at most once, and none starts after :meth:`close`
    (shutdown). A held step that raises is logged and the next one runs: the
    app is already serving."""

    def __init__(self, restore: _StartupRestore, loop) -> None:
        self._restore = restore
        self._loop = loop
        self._inline = restore.ended_in_time
        self._held: list = []
        self._results: dict = {}
        self._closing = threading.Event()

    def run(self, step) -> None:
        if self._inline:
            self._results[step] = step()
        else:
            self._held.append((step, False))

    async def run_off_loop(self, step) -> None:
        if self._inline:
            self._results[step] = await self._loop.run_in_executor(None, step)
        else:
            self._held.append((step, True))

    def release(self) -> None:
        if self._inline:
            return
        from . import startup_restore
        startup_restore.begin()
        self._restore.then(self._run_held)

    def close(self) -> None:
        from . import startup_restore
        self._closing.set()
        startup_restore.end()

    def completed(self, step) -> bool:
        """Whether ``step`` ran to its end."""
        return step in self._results

    def result(self, step, default):
        return self._results.get(step, default)

    def _run_held(self) -> None:
        from . import startup_restore
        if not self._closing.is_set():
            logger.info("Starting the %d startup steps that waited for the restore",
                        len(self._held))
        for step, off_loop in self._held:
            if off_loop:
                self._run_one(step)
        startup_restore.end()
        if self._closing.is_set():
            return
        try:
            self._loop.call_soon_threadsafe(self._run_on_loop)
        except RuntimeError:  # the event loop is closed: the app is shutting down
            logger.info("Startup steps that waited for the restore not started: the app "
                        "is shutting down")

    def _run_on_loop(self) -> None:
        for step, off_loop in self._held:
            if not off_loop:
                self._run_one(step)

    def _run_one(self, step) -> None:
        if self._closing.is_set():
            return
        try:
            self._results[step] = step()
        except Exception as exc:  # noqa: BLE001 - logged; the app keeps serving
            logger.error("Startup step %s failed after the restore (%s); the app keeps "
                         "serving without it", getattr(step, "__name__", step),
                         type(exc).__name__)


#: Pauses between background provisioning attempts for the two built-in
#: gateway targets (digital-thread, query-bridge). Event 34 (2026-10-18):
#: on one of three fresh accounts both runtime creates failed at the service's
#: network-config step and the single-shot provisioner logged once and gave
#: up, leaving the Quick gateway with no target for the life of the task. The
#: sibling accounts' identical creates went READY, so a later attempt wins.
BUILTIN_TARGET_RETRY_DELAYS_S = (60, 120, 300, 600, 900)


def _provision_builtin_target_with_retry(name: str, ensure, *,
                                         delays=BUILTIN_TARGET_RETRY_DELAYS_S,
                                         sleep=time.sleep) -> bool:
    """Run ``ensure`` until it returns without raising, pausing ``delays[i]``
    seconds after failure ``i`` and giving up after the last pause is spent.
    Returns True on success. Meant for a daemon thread: every failure is logged
    at WARNING with the attempt count, the final one at ERROR, nothing raises."""
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
        try:
            if ensure():
                logger.info("%s gateway target ensured (attempt %d)", name, attempt)
            return True
        except Exception:
            if attempt >= attempts:
                logger.error("%s target provisioning failed on the last attempt (%d of %d); "
                             "giving up until the next task start", name, attempt, attempts,
                             exc_info=True)
                return False
            delay = delays[attempt - 1]
            logger.warning("%s target provisioning failed (attempt %d of %d); retrying in %ds",
                           name, attempt, attempts, delay, exc_info=True)
            sleep(delay)
    return False


def _start_digital_thread_provisioner() -> None:
    threading.Thread(
        target=_provision_builtin_target_with_retry,
        args=("digital-thread", _agent_manager.ensure_digital_thread_target),
        name="digital-thread-provision", daemon=True).start()


def _quick_api_enabled() -> bool:
    """The Quick integration opt-in, from the QUICK_API_ENABLED task env
    (set by adt_compute_stack when the `quick_api_enabled` context flag is on;
    absent on base deploys, so the query-bridge target is never provisioned)."""
    return os.environ.get("QUICK_API_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def _start_query_bridge_provisioner() -> None:
    """Ensure the `query-bridge` gateway target — Quick deploys ONLY.

    Same background, retrying posture as the digital-thread provisioner above;
    gated on _quick_api_enabled so base deploys are byte-identical (no thread,
    no control-plane calls)."""
    if not _quick_api_enabled():
        return
    threading.Thread(
        target=_provision_builtin_target_with_retry,
        args=("query-bridge", _agent_manager.ensure_query_bridge_target),
        name="query-bridge-provision", daemon=True).start()


def _execute_prompt_admitted(prompt_text: str) -> str:
    import uuid
    cfg = get_config()
    if not cfg.has_runtime:
        from .orchestrator import get_orchestrator
        from .metric_turn_notice import local_turn_prompt
        # ADR-0078: a local scheduled prompt carries the SAME notice as the
        # cloud one (AgentCoreChatClient's hook) — scheduler thread, not loop.
        # The notice reads the raw prompt text, not the time line in front.
        intent = getattr(prompt_text, "raw_question", prompt_text)
        return str(get_orchestrator()(
            local_turn_prompt(prompt_text, intent, "scheduler")))
    from .answer_runs import answer_text
    from .chat_client import AgentCoreChatClient
    client = AgentCoreChatClient()
    session_id = "scheduler-" + uuid.uuid4().hex[:16]
    return answer_text(client.stream(
        prompt=prompt_text, session_id=session_id, user_id="scheduler",
        write_memory=False))


def _execute_prompt_sync(prompt_text: str) -> str:
    token = get_admission().acquire_if_idle("scheduler")
    if token is None:
        logger.info("Scheduler skipping prompt — query already in progress")
        return SCHEDULER_BUSY_RESULT
    try:
        return _execute_prompt_admitted(prompt_text)
    finally:
        get_admission().release(token)


def _init_prompt_library() -> None:
    """Open the prompt and card stores and build the (not yet started)
    scheduler with the saved prompt schedules, then load the runtime demo and
    live-update overrides. Reads nothing the startup restore rebuilds."""
    cfg = get_config()
    prompts_init(
        prompt_table=cfg.prompt_templates_table_name,
        card_table=cfg.dashboard_cards_table_name,
        execute_fn=_execute_prompt_sync,
        video_fn=lambda pid, text: generate_briefing(
            pid, text, _execute_prompt_sync),
    )
    from . import demo, live_settings
    if os.environ.get("ADT_SETTINGS_STORE", "").strip().lower() == "none":
        logger.info("ADT_SETTINGS_STORE=none — skipping SSM runtime-override "
                    "load (demo/live-updates); env defaults apply (local mode)")
    else:
        from .storage import SsmSettingsStore
        demo.configure(SsmSettingsStore("/adt/demo-enabled"))
        demo.load_persisted()
        live_settings.configure(SsmSettingsStore("/adt/live-updates-enabled"))
        live_settings.load_persisted()


def _start_prompt_scheduler() -> None:
    """Start the shared scheduler (the event loop's: call it on the loop)."""
    start_scheduler()
    logger.info("Prompt library initialized, scheduler started")


def _register_runtime_keepwarm() -> None:
    flag = os.environ.get("RUNTIME_KEEPWARM_ENABLED", "1").strip().lower()
    enabled = flag not in ("0", "false", "no", "off", "")
    if not enabled or _agent_manager.mode != "cloud":
        logger.info("Runtime keep-warm disabled (flag=%s, mode=%s)",
                    flag, _agent_manager.mode)
        return
    try:
        interval = int(os.environ.get("RUNTIME_KEEPWARM_INTERVAL_SECONDS", "150"))
    except ValueError:
        interval = 150
    interval = max(30, interval)

    def _keep_warm() -> None:
        try:
            _agent_manager.keep_warm_onboarded()
        except Exception:
            logger.debug("runtime keep-warm tick failed (non-fatal)", exc_info=True)

    add_interval_job("runtime-keep-warm", _keep_warm, interval)
    logger.info("Runtime keep-warm job registered (every %ss)", interval)


def _ping_orchestrator(cfg) -> None:
    token = get_admission().acquire_if_idle("warmup")
    if token is None:
        logger.debug("Skipping orchestrator warmup ping — query in progress")
        return
    try:
        _invoke_orchestrator_warmup(cfg)
    except Exception as exc:
        logger.debug("Orchestrator runtime warmup failed (non-fatal): %s", exc)
    finally:
        get_admission().release(token)


def _invoke_orchestrator_warmup(cfg) -> None:
    import json
    import uuid
    from io import BytesIO
    import boto3
    from botocore.config import Config
    runtime_arn = cfg.agentcore_runtime_arn
    if not runtime_arn or runtime_arn == "placeholder":
        return
    client = boto3.client(
        "bedrock-agentcore", region_name=cfg.aws_region,
        config=Config(read_timeout=5, connect_timeout=10))
    from .warmup import WARMUP_PROMPT, WARMUP_USER_ID
    payload = json.dumps({"prompt": WARMUP_PROMPT, "userId": WARMUP_USER_ID,
                          "sessionId": str(uuid.uuid4())}).encode()
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn, runtimeSessionId=str(uuid.uuid4()),
            payload=BytesIO(payload))
        stream = response.get("response") or response.get("body")
        if stream:
            try:
                next(stream.iter_lines())
            except (StopIteration, Exception):
                pass
        logger.debug("Orchestrator runtime warmup ping sent")
    except Exception:
        pass


async def _warmup_loop() -> None:
    import asyncio
    cfg = get_config()
    if not cfg.has_gateway:
        return
    while True:
        await asyncio.sleep(180)
        # Only the orchestrator is pinged. The gateway is CUSTOM_JWT, so a
        # SigV4-signed ping to it can only 401 — pure log noise; the
        # orchestrator ping is the one that actually keeps the runtime warm.
        _ping_orchestrator(cfg)


def _start_live_workers() -> tuple[bool, bool]:
    import asyncio
    from . import card_live
    rederive_enabled = bool(os.environ.get("RESOLVER_REDERIVE", "").strip())
    card_live_enabled = card_live._enabled()
    if rederive_enabled:
        rederive.start_worker()
        logger.info("Re-derivation worker started (RESOLVER_REDERIVE on)")
    if card_live_enabled:
        rederive.start_worker()
        card_live.start_stream_driver()
        card_live.register_poll_job()
        logger.info("Live-card data drivers started (CARD_LIVE on)")
    asyncio.create_task(_warmup_loop())
    logger.info("Gateway warmup loop started")
    return rederive_enabled, card_live_enabled


async def _warm_cache(refresh, label: str) -> None:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, refresh)
        logger.info("%s warmed (startup)", label)
    except Exception as exc:
        logger.debug("%s startup warm failed (non-fatal): %s", label, exc)


def _start_cache_workers() -> None:
    import asyncio
    from . import agents_routes, dashboard, fleet_metric_filters
    asyncio.create_task(_warm_cache(
        dashboard.refresh_cards_cache, "Dashboard card cache"))
    asyncio.create_task(_warm_cache(
        agents_routes.refresh_fleet_cache, "Agents fleet snapshot cache"))
    if os.environ.get("AGENTS_FLEET_POLL", "").strip():
        try:
            ttl = int(float(os.environ.get("AGENTS_FLEET_TTL", "150")))
        except (TypeError, ValueError):
            ttl = 150
        add_interval_job("agents-fleet-refresh",
                         agents_routes.refresh_fleet_cache, max(30, ttl))
        logger.info("Agents fleet background poll registered (every %ss)",
                    max(30, ttl))
    if fleet_metric_filters._enabled():
        asyncio.create_task(_warm_cache(
            fleet_metric_filters.bootstrap_metric_filters,
            "Agent metric filters"))


def _validate_metric_turn_config() -> None:
    """ADR-0078: a malformed ``METRIC_TURN_*`` bound refuses to boot (fail-loud)
    rather than silently degrading every defined-metric turn at run time."""
    from .metric_turn_notice import validate_config
    validate_config()


def _validate_model_allowlist() -> None:
    """A malformed ``ALLOWED_MODELS`` (an id outside the model catalog, or a
    value naming no model) refuses to boot, like the synth-time check in the
    compute stack, rather than failing every Admin model read and save."""
    from .model_catalog import allowed_families
    allowed = allowed_families()
    if allowed is not None:
        logger.info("Model allowlist active: %s", ", ".join(sorted(allowed)))


# A restore still running after STARTUP_RESTORE_WAIT_S finishes in the
# background. Each lifespan step run through `after.run` then waits for it,
# because it reads or writes what the restore rebuilds:
#  * the two built-in target provisioners call _agent_manager.start_agent,
#    which the restore replaces for its whole pass (_restore_integrity), and
#    write the agent registry and gateway it is writing;
#  * _register_demo_systems registers or heals by catalog.get(id), which the
#    restore fills (off the event loop in every case: it does blocking I/O);
#  * the scheduler runs the saved prompt schedules, which answer over the
#    registered systems; the steps after it register jobs on it and keep
#    their place after it (keep-warm and the schema drift check read the
#    onboarded systems);
#  * the live workers re-resolve cards over the registered systems, and the
#    cache workers warm the card and agent-fleet caches from them.
# The rest read none of it: the MQTT subscriber is gated on configuration
# only, the prompt library opens its stores and reads the settings overrides
# (the routes and every add_interval_job need it), and seed_registry rewrites
# the registry prompts from the demo manifest.
@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    _validate_metric_turn_config()
    _validate_model_allowlist()
    after = _AfterRestore(_restore_graph(), asyncio.get_running_loop())
    after.run(_start_digital_thread_provisioner)
    after.run(_start_query_bridge_provisioner)
    mqtt_subscriber.start_subscriber()
    _init_prompt_library()
    await after.run_off_loop(_register_demo_systems)
    seed_registry()
    after.run(_start_prompt_scheduler)
    after.run(_register_runtime_keepwarm)
    after.run(register_auto_accept_job)
    after.run(_start_live_workers)
    after.run(_start_cache_workers)
    after.release()   # not reached when a step above raised: startup stops, as before
    try:
        yield
    finally:
        _core_concept_topup.shut_down()   # ends its pause at once; nothing waits for it
        after.close()
        if after.completed(_start_prompt_scheduler):
            stop_scheduler()
        _agent_manager.stop_all()
        mqtt_subscriber.stop_subscriber()
        rederive_enabled, card_live_enabled = after.result(_start_live_workers, (False, False))
        if card_live_enabled:
            from . import card_live
            card_live.stop_stream_driver()
        if rederive_enabled or card_live_enabled:
            rederive.stop_worker()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title=COMPANY, lifespan=lifespan)
startup_restore.install(app)   # the 503 refusal answers with its reason
app.add_middleware(AuthMiddleware)


# ── Core endpoints ────────────────────────────────────────────────────────────

# App version — git-derived, baked into the image env at deploy time (deploy.sh
# computes it and adt_compute_stack passes it as the APP_VERSION build-arg; the
# Dockerfile defaults it to "dev" for local/unbaked builds).
_APP_VERSION = os.environ.get("APP_VERSION", "dev")

@app.get("/health")
async def health():
    return {"status": "ok", "version": _APP_VERSION}


@app.get("/api/version")
async def get_version():
    return {"version": _APP_VERSION}


@app.get("/api/debug/stream-capture/{capture_id}")
async def debug_stream_capture(capture_id: str, session_id: str, request: Request):
    """Download one metadata-only diagnostic owned by this admin/session."""
    from .stream_capture import CaptureAccessDenied, read_capture
    user_id = getattr(getattr(request.state, "user", None), "user_id", "")
    try:
        content = read_capture(
            capture_id, requester_id=user_id, session_id=session_id,
            is_admin=bool(getattr(request.state, "is_admin", False)),
        )
    except CaptureAccessDenied as exc:
        raise HTTPException(status_code=403, detail="Forbidden") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Capture not found") from exc
    return Response(
        content=content, media_type="application/jsonl",
        headers={"Content-Disposition": f'attachment; filename="capture-{capture_id}.jsonl"'},
    )


@app.delete("/api/debug/stream-capture/{capture_id}")
async def debug_delete_stream_capture(capture_id: str, session_id: str, request: Request):
    from .stream_capture import CaptureAccessDenied, delete_capture
    user_id = getattr(getattr(request.state, "user", None), "user_id", "")
    try:
        delete_capture(
            capture_id, requester_id=user_id, session_id=session_id,
            is_admin=bool(getattr(request.state, "is_admin", False)),
        )
    except CaptureAccessDenied as exc:
        raise HTTPException(status_code=403, detail="Forbidden") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Capture not found") from exc
    return {"status": "deleted"}


@app.get("/api/debug/stream-captures")
async def debug_list_stream_captures(session_id: str, request: Request):
    """List this admin/session's unexpired metadata-only diagnostics."""
    from .stream_capture import CaptureAccessDenied, list_captures
    user_id = getattr(getattr(request.state, "user", None), "user_id", "")
    try:
        captures = list_captures(
            requester_id=user_id, session_id=session_id,
            is_admin=bool(getattr(request.state, "is_admin", False)),
        )
    except CaptureAccessDenied as exc:
        raise HTTPException(status_code=403, detail="Forbidden") from exc
    return {"captures": captures}


@app.get("/api/branding")
async def branding():
    return branding_dict()


def _graph_view_fetch_nodes():
    """Fetch KG-view nodes as four budget-separated buckets.

    Graph reads route through db.neptune_query so they honor GRAPH_BACKEND
    (local Memgraph vs cloud Neptune) — single switch point. See ADR 0003.
    Each bucket has its OWN budget: a single high-cardinality label can never
    silently squeeze another label out of the view (the old shared LIMIT 200
    caps dropped System/Plant nodes once Concepts grew, and the old catch-all
    "everything else" bucket let :Asset/:Property spines squeeze out
    ValueMappings). :Field and :Property never appear — they are leaf
    schema/telemetry metadata, far too numerous to render.
    """
    # System and Plant are the topology backbone (a handful of nodes) — fetch
    # them FIRST with NO limit so they are ALWAYS present.
    core_raw = neptune_query(
        "MATCH (n) WHERE (n:System OR n:Plant) RETURN n"
    )
    # Only CONNECTED concepts: unmapped seeded-vocabulary concepts have no
    # edges and would render as orphan clutter; the generous cap keeps mapped
    # concepts from being silently truncated.
    # A JoinPath carries its effective status (joinpath_rule_link): one whose
    # IdentityRule is switched off reads as deactivated, kept for audit.
    priority_raw = neptune_query(
        "MATCH (n) WHERE (n:Concept AND (n)-[]-()) OR n:IdentityRule OR n:JoinPath "
        + _jp_rule.linked_rule_match("n", "jp_rule").replace("WHERE", "WHERE n:JoinPath AND", 1)
        + "RETURN n, CASE WHEN coalesce(n.status, '') = 'deactivated' OR "
        + _jp_rule.rule_inactive("jp_rule") + " THEN 'deactivated' END AS effective_status "
        "LIMIT 1000"
    )
    # Intelligence-layer nodes get a guaranteed budget of their own — includes
    # the MetaNode/MetaEdge self-describing vocabulary.
    intel_raw = neptune_query(
        "MATCH (n) WHERE n:ValueMapping OR n:AuthorityRule OR n:SchemaEntry "
        "OR n:Correlation OR n:MetaNode OR n:MetaEdge "
        "RETURN n LIMIT 600"
    )
    # Asset hierarchy (structural + equipment + models) on a separate budget.
    # :Property nodes are excluded like :Field — dozens per equipment asset.
    hierarchy_raw = neptune_query(
        "MATCH (n) WHERE n:Asset OR n:AssetModel RETURN n LIMIT 300"
    )
    return core_raw + priority_raw + intel_raw + hierarchy_raw


def _graph_view_fetch_edges():
    """Fetch KG-view edges; Field AND Property endpoints excluded.

    Without the :Property exclusion, thousands of HAS_PROPERTY edges pointing
    at nodes not in the view would exhaust the edge cap and hide the edges
    that matter (e.g. MAPS_TO_ASSET, FROM/TO_SYSTEM).
    """
    edges_raw = neptune_query(
        "MATCH (a)-[r]->(b) WHERE NOT a:Field AND NOT b:Field "
        "AND NOT a:Property AND NOT b:Property "
        "RETURN a AS src_node, b AS tgt_node, type(r) AS rel LIMIT 2000"
    )
    # Also get System→Concept connections (via Field, collapsed for visualization)
    concept_edges_raw = neptune_query(
        "MATCH (s:System)-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(f:Field)-[:MAPS_TO]->(c:Concept) "
        "RETURN s AS src_node, c AS tgt_node, 'MAPS_TO_CONCEPT' AS rel"
    )
    return edges_raw + concept_edges_raw


def _graph_view_shape_nodes(nodes_raw):
    """Shape raw node rows for the view; dedupe by `~id`. Returns (nodes, ids)."""
    nodes = []
    node_ids = set()
    for row in nodes_raw:
        n = row.get("n", {})
        nid = n.get("~id", "")
        if not nid or nid in node_ids:
            continue
        node_ids.add(nid)
        labels = n.get("~labels", [])
        props = without_internal_properties(n.get("~properties", {}) or {})
        label_type = labels[0] if labels else "Unknown"
        name = (props.get("name") or props.get("id") or props.get("concept") or
                props.get("pattern") or props.get("from_system") or label_type)
        shown = {k: str(v)[:500] for k, v in props.items()}
        if label_type == "JoinPath" and row.get("effective_status") == "deactivated":
            shown["effective_status"] = "deactivated"
        nodes.append({
            "id": nid,
            "label": str(name)[:40],
            "type": label_type,
            "props": shown,
        })
    return nodes, node_ids


def _graph_view_shape_edges(edges_raw, node_ids):
    """Shape raw edge rows: drop edges to undisplayed nodes, dedupe triples."""
    edges = []
    seen = set()
    for e in edges_raw:
        src_node = e.get("src_node", {})
        tgt_node = e.get("tgt_node", {})
        src = src_node.get("~id", "") if isinstance(src_node, dict) else ""
        tgt = tgt_node.get("~id", "") if isinstance(tgt_node, dict) else ""
        rel = e.get("rel", "")
        if not src or not tgt or src not in node_ids or tgt not in node_ids:
            continue
        key = f"{src}-{rel}-{tgt}"
        if key in seen:
            continue
        seen.add(key)
        edges.append({"source": src, "target": tgt, "label": rel})
    return edges


def neptune_full_graph():
    """Return the intelligence layer of the knowledge graph.

    Budget-separated node buckets: System/Plant backbone (uncapped), connected
    Concept/IdentityRule/JoinPath, intelligence nodes (ValueMapping,
    AuthorityRule, SchemaEntry, Correlation, MetaNode, MetaEdge), and the
    Asset/AssetModel hierarchy. Field and Property nodes are leaf metadata —
    excluded from both nodes and edges (an onboarded telemetry system carries
    hundreds-to-thousands of :Property leaves that would flood the view).
    ``not_drawn`` gives the real count of each label the view leaves out
    (``_graph_view_not_drawn``), so its legend never reads them as absent.
    Sync body (the graph reads); the route offloads it.
    """
    try:
        nodes, node_ids = _graph_view_shape_nodes(_graph_view_fetch_nodes())
        edges = _graph_view_shape_edges(_graph_view_fetch_edges(), node_ids)
        return {"nodes": nodes, "edges": edges, "not_drawn": _graph_view_not_drawn()}
    except Exception as ex:
        return {"nodes": [], "edges": [],
                **public_error(ex, "graph.view", default="graph_query_failed")}


#: How many nodes of each label the KG view never draws: leaf schema and
#: telemetry metadata (:Field, :Property), far too many to render, and the
#: per-provision :OnboardingLog records, which carry no edge. One static
#: aggregate read: the first clause aggregates with no grouping key, so it
#: always yields one row, and each OPTIONAL MATCH keeps that row when a label
#: has no nodes. It returns three numbers, never a node.
_GRAPH_VIEW_NOT_DRAWN_CYPHER = (
    "MATCH (f:Field) WITH count(f) AS field_count "
    "OPTIONAL MATCH (p:Property) WITH field_count, count(p) AS property_count "
    "OPTIONAL MATCH (o:OnboardingLog) "
    "RETURN field_count, property_count, count(o) AS onboarding_log_count"
)
#: Each left-out label and the column its count comes back in.
_GRAPH_VIEW_NOT_DRAWN_COLUMNS = (
    ("Field", "field_count"), ("Property", "property_count"),
    ("OnboardingLog", "onboarding_log_count"))


def _graph_view_not_drawn() -> dict:
    """``label -> count`` for every label the view leaves out, ``None`` where
    the count could not be read: the checked read failed or returned no row,
    or the row's value is not a whole number. A failed count never costs the
    graph that was read, and is never reported as 0."""
    try:
        rows = neptune_query_checked(_GRAPH_VIEW_NOT_DRAWN_CYPHER)
    except Exception:  # noqa: BLE001 - the counts are additive to the view
        rows = []
    row = rows[0] if rows and isinstance(rows[0], dict) else {}

    def _count(value):
        whole = isinstance(value, int) and not isinstance(value, bool)
        return value if whole and value >= 0 else None

    return {label: _count(row.get(column))
            for label, column in _GRAPH_VIEW_NOT_DRAWN_COLUMNS}


@app.get("/api/neptune/graph")
async def neptune_full_graph_route():
    """The intelligence layer of the knowledge graph. Sync body: ``neptune_full_graph``."""
    return await offload(neptune_full_graph)


def neptune_node_neighbors(node_id: str):
    """Return all edges and connected nodes for a given node ID.
    
    Used by the interactive graph traversal UI — click a node to see
    its properties, outgoing edges, and incoming edges with target nodes.
    """
    try:
        # Graph reads route through db.neptune_query (honors GRAPH_BACKEND). See ADR 0003.
        # NOTE: node-detail click-through uses id(n)=$nid; id() semantics differ between
        # Neptune and Memgraph, so this endpoint is a known local-fidelity gap.
        node_results = neptune_query(
            "MATCH (n) WHERE id(n) = $nid RETURN n", {"nid": node_id}
        )
        node_data = {}
        if node_results:
            n = node_results[0].get("n", {})
            node_data = {
                "id": n.get("~id", node_id),
                "labels": n.get("~labels", []),
                "properties": n.get("~properties", {}),
            }

        # Get all outgoing edges
        outgoing = []
        for row in neptune_query(
            "MATCH (n)-[r]->(m) WHERE id(n) = $nid RETURN type(r) AS rel, m", {"nid": node_id}
        ):
            m = row.get("m", {})
            target_labels = m.get("~labels", [])
            target_props = m.get("~properties", {})
            target_name = target_props.get("name") or target_props.get("id") or m.get("~id", "")
            outgoing.append({
                "relationship": row.get("rel", ""),
                "target_id": m.get("~id", ""),
                "target_label": target_labels[0] if target_labels else "Unknown",
                "target_name": str(target_name)[:60],
            })

        # Get all incoming edges
        incoming = []
        for row in neptune_query(
            "MATCH (m)-[r]->(n) WHERE id(n) = $nid RETURN type(r) AS rel, m", {"nid": node_id}
        ):
            m = row.get("m", {})
            source_labels = m.get("~labels", [])
            source_props = m.get("~properties", {})
            source_name = source_props.get("name") or source_props.get("id") or m.get("~id", "")
            incoming.append({
                "relationship": row.get("rel", ""),
                "source_id": m.get("~id", ""),
                "source_label": source_labels[0] if source_labels else "Unknown",
                "source_name": str(source_name)[:60],
            })

        return {
            "node": node_data,
            "outgoing": outgoing,
            "incoming": incoming,
        }
    except Exception as ex:
        return {"node": {}, "outgoing": [], "incoming": [],
                **public_error(ex, "graph.neighbors", default="graph_query_failed")}


@app.get("/api/neptune/node/{node_id}/neighbors")
async def neptune_node_neighbors_route(node_id: str):
    """Return all edges and connected nodes for a given node ID. Sync body:
    ``neptune_node_neighbors``. Operational node properties
    (``graph_internal_props``) are left out of the answer."""
    return without_internal_properties(await offload(neptune_node_neighbors, node_id=node_id))


def kg_analyze_node(body: dict) -> dict:
    """AI analysis of a selected knowledge graph node — explains what it is and its role."""
    try:
        node_type = body.get("node_type", "")
        node_name = body.get("node_name", "")
        properties = body.get("properties", "")

        from .db import bedrock_call
        prompt = (
            f"You are a manufacturing data integration expert explaining a knowledge graph node to a user.\n\n"
            f"Node Type: {node_type}\n"
            f"Node Name: {node_name}\n"
            f"Properties: {properties}\n\n"
            f"In 2-3 concise sentences, explain:\n"
            f"1. What this node represents in a manufacturing digital thread context\n"
            f"2. How it connects to other systems or data\n"
            f"Be specific to THIS node's properties. No generic explanations."
        )
        analysis = bedrock_call(prompt)
        return {"analysis": analysis}
    except Exception as e:
        return {"analysis": "", **public_error(e, "graph.analyze", default="upstream_error")}


@app.post("/api/kg/analyze")
async def kg_analyze_node_route(request: Request):
    """AI analysis of a knowledge graph node. Sync body: ``kg_analyze_node``."""
    try:
        body = await request.json()
    except Exception:
        return {"analysis": "", "error": "request body must be JSON"}
    return await offload(kg_analyze_node, body if isinstance(body, dict) else {})


def _check_steward(request: Request) -> None:
    """Steward gate (ADR-0050): proposal ACCEPTANCE/mutation is admin-only.

    Wired as a ROUTE-REGISTRATION dependency (``dependencies=[Depends(
    _check_steward)]`` on the gated ``@app.post`` decorators), so the gate is
    HTTP-layer only: the scheduler's unattended sweeps call the
    ``self_learning`` functions directly (never through HTTP) and stay
    ungated, and bare handler mounts in offline unit harnesses carry no gate.
    On the real app ``AuthMiddleware`` stamps ``request.state.is_admin`` on
    EVERY request that reaches these routes (real Cognito-group verdict, or
    ``True`` in the DEV_NO_AUTH local mode), so the gate FAILS CLOSED: an
    ABSENT attribute (no auth verdict at all) is denied exactly like an
    explicit non-admin verdict. Proposing stays open to any authenticated
    user (``user-define`` and the GET list are NOT gated); ``user-define``
    reads the same verdict through ``steward_gate.is_steward`` so only a
    steward's assertion is accepted on entry, and anyone else's waits pending.

    Defined here (before its first decorator use) because FastAPI resolves
    ``Depends(_check_steward)`` at route-registration time.
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403,
                            detail="Steward (admin) role required")


# ── Graph maintenance (steward-only, POST, offloaded) ────────────────────────
# These operator tools MUTATE the knowledge graph (MERGE edges, DETACH DELETE
# orphans, create Concept nodes, materialise accepted additive schema drift).
# repair/backfill were GET routes with no steward gate; a GET must be
# read-only (a browser prefetch or a crawler with a session cookie could
# otherwise delete graph nodes). All three are POST, steward-gated at
# registration, and run their sync Neptune work in a worker thread
# (EVENT-LOOP OFFLOAD PATTERN — see ``metric_resolver``). Pinned by
# ``tests/unit/test_graph_maintenance_routes.py``.

def _repair_graph_sync(payload: dict | None = None) -> dict:
    """Repair: retire bad ValueMapping rows (``graph_hygiene``), recreate
    missing edges AND delete orphaned nodes with no valid parent.

    ``{"dry_run": true}`` plans only: every step runs against a no-op writer,
    so the report says what WOULD change and nothing reaches the graph."""
    dry_run = (payload if isinstance(payload, dict) else {}).get("dry_run", False)
    if not isinstance(dry_run, bool):
        return {"error": "graph repair rejected: 'dry_run' must be a JSON boolean",
                "http_status": 422}
    try:
        from .db import neptune_write, neptune_query as _nq
        from .graph_hygiene import run_value_mapping_hygiene
        _nw = _dry_run_write if dry_run else neptune_write
        value_mappings = run_value_mapping_hygiene(_nq, _nw, dry_run=dry_run)
        repairs = _repair_system_edges(_nq, _nw)
        deletions = _delete_orphan_nodes(_nq, _nw)
        repairs += _link_identity_rule_concepts(_nq, _nw)
        report = {
            "repairs": len(repairs),
            "deletions": len(deletions),
            "repair_detail": repairs[:20],
            "deletion_detail": deletions[:20],
            "value_mappings": value_mappings,
            "dry_run": dry_run,
        }
        if dry_run:
            report["dry_run_note"] = REPAIR_DRY_RUN_NOTE
        return report
    except Exception as ex:
        return public_error(ex, "graph.repair", default="graph_query_failed")


# What a dry-run repair report can and cannot promise (operations-playbooks.md).
REPAIR_DRY_RUN_NOTE = (
    "Dry run: only value_mappings is exact. 'repairs' is the total of MERGEs the edge "
    "steps would issue (existing edges included, not a count of missing edges) plus the "
    "ValueMappings naming a removed system they would retire; "
    "'deletions' (orphans) may over-report because the step-1 HAS_SCHEMA edges are "
    "not simulated, so a SchemaEntry they would link still reads as an orphan.")


def _dry_run_write(_cypher, _parameters=None) -> bool:
    """The dry-run graph writer: accepts every write and performs none."""
    return True


def _repair_system_edges(_nq, _nw) -> list:
    """Steps 1-3: ensure a System node + owning edge for every SchemaEntry and
    AuthorityRule that names a system_id, then the ValueMapping edges
    (``_repair_value_mapping_edges``)."""
    repairs = []
    specs = [
        ("SchemaEntry", "se", "HAS_SCHEMA", True),
        ("AuthorityRule", "ar", "APPLIES_TO", False),
    ]
    for label, alias, edge, system_owns in specs:
        rows = _nq(f"MATCH ({alias}:{label}) RETURN {alias}.id AS nid, {alias}.system_id AS sid")
        for row in (rows or []):
            sid, nid = row.get("sid"), row.get("nid")
            if not sid or not nid:
                continue
            _nw("MERGE (s:System {id: $sid}) ON CREATE SET s.name = $sid, s.status = 'onboarded'",
                {"sid": sid})
            if system_owns:
                _nw(f"MATCH (s:System {{id: $sid}}), (n:{label} {{id: $nid}}) MERGE (s)-[:{edge}]->(n)",
                    {"sid": sid, "nid": nid})
                repairs.append(f"System({sid}) --{edge}--> {label}({nid})")
            else:
                _nw(f"MATCH (n:{label} {{id: $nid}}), (s:System {{id: $sid}}) MERGE (n)-[:{edge}]->(s)",
                    {"nid": nid, "sid": sid})
                repairs.append(f"{label}({nid}) --{edge}--> System({sid})")
    return repairs + _repair_value_mapping_edges(_nq, _nw)


def _system_inventory_verified(_nq) -> "set | None":
    """Every :System id, or ``None`` when the read cannot be trusted. ``_nq``
    is the fail-soft ``neptune_query`` (a failure reads as ``[]``), so the
    scan is controlled by a one-row count, as ``graph_hygiene`` controls its
    ValueMapping scan: the scan must answer exactly that many non-empty ids."""
    control = _nq("MATCH (s:System) RETURN count(s) AS n") or []
    n = control[0].get("n") if len(control) == 1 and isinstance(control[0], dict) else None
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        return None
    rows = _nq("MATCH (s:System) RETURN s.id AS id") or []
    ids = {r.get("id") for r in rows if isinstance(r, dict) and r.get("id")}
    return ids if len(rows) == n and len(ids) == n else None


@mutates_value_mappings
def _repair_value_mapping_edges(_nq, _nw) -> list:
    """Step 3: every ValueMapping against a VERIFIED System inventory
    (``_system_inventory_verified``). A mapping whose ``from_system`` or
    ``to_system`` (the properties every writer sets; none sets
    ``system_id``) names no existing System is RETIRED (DETACH DELETE), never
    half-repaired: one left by a removal made before the removal deleted
    mappings still reached the surviving system's route seed through its live
    edge. Steward rows go too, as the removal deletes them; their accepted
    items replay once both systems are onboarded. Every other mapping gets
    its FROM_SYSTEM / TO_SYSTEM edges. Only a write the graph accepted is
    reported, and no System node is ever created from a mapping. An
    unverified inventory changes nothing (it would read every system as
    gone)."""
    from .graph_hygiene import RETIRE_VALUE_MAPPING_CYPHER
    systems = _system_inventory_verified(_nq)
    if systems is None:
        logger.warning("graph repair: System inventory unverified; ValueMappings left unchanged")
        return []
    repairs = []
    rows = _nq("MATCH (vm:ValueMapping) RETURN vm.id AS nid, "
               "vm.from_system AS fsys, vm.to_system AS tsys")
    for row in (rows or []):
        nid = row.get("nid")
        if not nid:
            continue
        missing = [str(row.get(key) or "") for key in ("fsys", "tsys")
                   if row.get(key) not in systems]
        if missing:
            if _nw(RETIRE_VALUE_MAPPING_CYPHER, {"vmid": nid}) is False:
                logger.warning("graph repair: retiring ValueMapping(%s) failed; left unchanged", nid)
                continue
            repairs.append(f"ValueMapping({nid}) retired: names no existing system "
                           f"({', '.join(m or '<none>' for m in missing)})")
            continue
        for key, edge in (("fsys", "FROM_SYSTEM"), ("tsys", "TO_SYSTEM")):
            sid = row.get(key)
            if _nw(f"MATCH (n:ValueMapping {{id: $nid}}), (s:System {{id: $sid}}) MERGE (n)-[:{edge}]->(s)",
                   {"nid": nid, "sid": sid}) is False:
                continue
            repairs.append(f"ValueMapping({nid}) --{edge}--> System({sid})")
    return repairs


def _delete_orphan_nodes(_nq, _nw) -> list:
    """Steps 4-5: DETACH DELETE Field nodes with no HAS_FIELD parent and
    SchemaEntry nodes with no HAS_SCHEMA parent — true orphans."""
    deletions = []
    for label, parent_edge in (("Field", "HAS_FIELD"), ("SchemaEntry", "HAS_SCHEMA")):
        rows = _nq(f"MATCH (n:{label}) WHERE NOT ()-[:{parent_edge}]->(n) RETURN n.id AS nid")
        for row in (rows or []):
            nid = row.get("nid")
            if nid:
                _nw(f"MATCH (n:{label} {{id: $nid}}) DETACH DELETE n", {"nid": nid})
                deletions.append(f"Deleted orphan {label}({nid})")
    return deletions


def _link_identity_rule_concepts(_nq, _nw) -> list:
    """Step 7: create Concept nodes from IdentityRule.concept and link them."""
    repairs = []
    irs_with_concepts = _nq(
        "MATCH (ir:IdentityRule) WHERE ir.concept IS NOT NULL AND ir.concept <> '' AND ir.concept <> 'needs_review' "
        "RETURN ir.id AS irid, ir.concept AS concept LIMIT 500"
    )
    for ir in (irs_with_concepts or []):
        cid = ir.get("concept")
        irid = ir.get("irid")
        if cid and irid:
            _nw("MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $cid", {"cid": cid})
            _nw("MATCH (ir:IdentityRule {id: $irid}), (c:Concept {id: $cid}) MERGE (ir)-[:LINKS_CONCEPT]->(c)", {"irid": irid, "cid": cid})
            repairs.append(f"Concept({cid}) linked from IdentityRule({irid})")
    return repairs


def _graph_maintenance_response(result: dict):
    """A swallowed graph error is a FAILURE, not a 200 with an error key.
    An optional ``http_status`` (e.g. 422 for a rejected input) overrides 500."""
    from fastapi.responses import JSONResponse
    if "error" in result:
        code = int(result.pop("http_status", 500))
        return JSONResponse(status_code=code, content=result)
    return result


@app.post("/api/neptune/graph/repair", dependencies=[Depends(_check_steward)])
async def neptune_repair_graph(body: dict | None = None):
    """Steward-only graph repair (bad ValueMappings retired, edges recreated,
    orphans deleted); optional ``{"dry_run": true}`` body plans only.

    While the startup restore still runs in the background, a repair that
    writes is refused (503, ``startup_restore``): it MERGEs System nodes and
    deletes orphans the restore may be reading. A dry run writes nothing
    (``_repair_graph_sync`` swaps in ``_dry_run_write``) and still answers.
    The steward check and the body parse come first."""
    if not (isinstance(body, dict) and body.get("dry_run") is True):
        refuse_while_restoring()
    from .metric_resolver import offload_resolver
    return _graph_maintenance_response(await offload_resolver(_repair_graph_sync, body))


def _backfill_concepts_sync() -> dict:
    """Backfill Concept nodes from stored onboarding mapping data.
    
    Creates Concept nodes for all unique concept IDs referenced in Field MAPS_TO edges,
    and for all concepts referenced in AuthorityRule/ValueMapping nodes.
    Run this after onboarding if Concepts are missing from the graph.
    """
    try:
        from .db import neptune_write as _nw, neptune_query as _nq
        from .catalog import core_concept_merges
        created = []

        # Step 1: Seed all core concepts from the single source (ADR 0020) so this backfill
        # writer agrees with deploy.sh / onboard_reset / graph_reset — every core Concept gets
        # name + isa95_level + source='core' + description.
        for stmt, params in core_concept_merges():
            _nw(stmt, params)
            created.append(f"core:{params['id']}")

        # Step 2: Create Concepts from AuthorityRule.concept properties
        ars = _nq("MATCH (ar:AuthorityRule) WHERE ar.concept IS NOT NULL AND ar.concept <> '' RETURN DISTINCT ar.concept AS cid")
        for r in (ars or []):
            cid = r.get("cid", "")
            if cid and cid not in ("needs_review", "unknown"):
                _nw(
                    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $cid, c.source = 'authority'",
                    {"cid": cid}
                )
                # [F3] No ABOUT_CONCEPT edge: authority is canonically queried by the
                # ar.concept *property* (resolver AuthorityResolver + the audit's
                # queryable_by_property), never by traversing an edge. The edge was a
                # redundant lazy backfill — removed per the S0.1 audit (ADR-0011). The
                # Concept node above is all this step needs to create.
                created.append(f"authority:{cid}")

        # Step 3: Create Concepts from ValueMapping.concept properties
        vms = _nq("MATCH (vm:ValueMapping) WHERE vm.concept IS NOT NULL AND vm.concept <> '' RETURN DISTINCT vm.concept AS cid")
        for r in (vms or []):
            cid = r.get("cid", "")
            if cid and cid not in ("needs_review", "unknown"):
                _nw(
                    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $cid, c.source = 'mapping'",
                    {"cid": cid}
                )
                created.append(f"mapping:{cid}")

        return {"concepts_created": len(created), "detail": created[:30]}
    except Exception as ex:
        return public_error(ex, "graph.backfill_concepts", default="graph_query_failed")


@app.post("/api/neptune/graph/backfill-concepts",
          dependencies=[Depends(_check_steward)])
async def neptune_backfill_concepts():
    """Steward-only Concept backfill from stored mapping data."""
    from .metric_resolver import offload_resolver
    return _graph_maintenance_response(
        await offload_resolver(_backfill_concepts_sync))


def _repair_schema_sync(payload: dict) -> dict:
    """ADR-0077 D2: re-apply already-accepted additive drift for ONE system
    (counts only). Validation → 422; any other failure → 500 (class only)."""
    from .schema_repair import repair_schema
    detect = payload.get("detect", True)
    if not isinstance(detect, bool):
        return {"error": "schema repair rejected: 'detect' must be a JSON boolean",
                "http_status": 422}
    try:
        return repair_schema(str(payload.get("system_id") or ""),
                             actor=str(payload.get("_actor") or ""), detect=detect)
    except ValueError as exc:
        return {"error": f"schema repair rejected: {exc}", "http_status": 422}
    except Exception as exc:  # noqa: BLE001 - class only; the route maps it to 500
        return {"error": f"schema repair failed: {type(exc).__name__}"}


@app.post("/api/neptune/graph/repair-schema", dependencies=[Depends(_check_steward)])
async def neptune_repair_schema(request: Request, body: dict | None = None):
    """Steward-only additive schema repair (ADR-0077): materialise accepted-but-absent columns."""
    payload = {**(body if isinstance(body, dict) else {}), "_actor": _request_actor(request)}
    return _graph_maintenance_response(await offload(_repair_schema_sync, payload))


def thread_graph_summary():
    """Return the current knowledge graph state — real data from Neptune."""
    try:
        rules = neptune_query("MATCH (ir:IdentityRule) RETURN ir.id AS id, ir.concept AS concept, ir.source_system AS source_system, ir.source_field AS source_field, ir.source_pattern AS source_pattern, ir.target_system AS target_system, ir.target_field AS target_field, ir.target_pattern AS target_pattern, ir.transform AS transform, ir.method AS method, ir.status AS status, ir.confidence AS confidence, ir.provenance AS provenance, ir.origin_context AS origin_context, ir.approved_by AS approved_by, ir.created_at AS created_at, ir.deactivated_by AS deactivated_by, ir.deactivated_at AS deactivated_at") or []
        jp = neptune_query("MATCH (jp:JoinPath) " + _jp_rule.linked_rule_match() + "RETURN jp.id AS id, jp.from_system AS from_system, jp.from_table AS from_table, jp.from_field AS from_field, jp.to_system AS to_system, jp.to_table AS to_table, jp.to_field AS to_field, jp.transform AS transform, jp.description AS description, " + _jp_rule.effective_status() + " AS status") or []
        vm = neptune_query("MATCH (vm:ValueMapping) RETURN vm.id AS id, vm.concept AS concept, vm.from_system AS from_system, vm.from_value AS from_value, vm.to_system AS to_system, vm.to_value AS to_value, vm.confidence AS confidence") or []
        # ar projects vouching provenance too (2.2): method/approved_by/evidence/
        # provenance let the audit gate + review UI show WHY an authority does
        # (or does not) vouch under the B4 P8 rule. ADDITIVE — no field removed.
        ar = neptune_query("MATCH (ar:AuthorityRule) RETURN ar.id AS id, ar.concept AS concept, ar.system_id AS system_id, ar.rule AS rule, ar.method AS method, ar.approved_by AS approved_by, ar.evidence AS evidence, ar.provenance AS provenance, ar.status AS status, ar.deactivated_by AS deactivated_by, ar.deactivated_at AS deactivated_at") or []
        co = neptune_query("MATCH (c:Correlation) RETURN c.id AS id, c.pattern AS pattern, c.confidence AS confidence") or []
        db = neptune_query("MATCH (d:DecisionBoundary) RETURN d.id AS id, d.concept AS concept, d.threshold AS threshold, d.action AS action") or []
        # identity_rules / join_paths count the rules in use; the switched-off
        # ones stay in the lists for audit and are counted apart.
        ir_off = sum(1 for r in rules if isinstance(r, dict) and _jp_rule.is_inactive_status(r.get("status")))
        jp_off = sum(1 for j in jp if isinstance(j, dict) and _jp_rule.is_inactive_status(j.get("status")))
        return {"identity_rules": len(rules) - ir_off, "identity_rules_inactive": ir_off, "rules": rules, "join_paths": len(jp) - jp_off, "join_paths_inactive": jp_off, "join_paths_detail": jp, "value_mappings": len(vm), "value_mappings_detail": vm, "authority_rules": len(ar), "authority_rules_detail": ar, "correlations": len(co), "correlations_detail": co, "decision_boundaries": len(db), "decision_boundaries_detail": db}
    except Exception:
        return {"identity_rules": 0, "identity_rules_inactive": 0, "rules": [], "join_paths": 0, "join_paths_inactive": 0, "join_paths_detail": [], "value_mappings": 0, "value_mappings_detail": [], "authority_rules": 0, "authority_rules_detail": [], "correlations": 0, "correlations_detail": [], "decision_boundaries": 0, "decision_boundaries_detail": []}


@app.get("/api/thread/graph-summary")
async def thread_graph_summary_route():
    """Return the current knowledge graph state — real data from Neptune. Sync body: ``thread_graph_summary``."""
    return await offload(thread_graph_summary)


# ── Rule lifecycle (maintain-side: soft deactivate/reactivate) ───────────────
# Deactivation is a SOFT disable: the node keeps its provenance and stays
# visible in the topology drawers (bucketed as deactivated), but every rule
# READER (orchestrator route seed, resolve, graph_analytics paths for
# IdentityRule; metric_resolver authority resolution and the self_learning
# vouching read for AuthorityRule) filters status='deactivated' so the rule
# stops participating in decisions. Reactivation restores it. Both stamp
# who/when for the audit trail. Covers IdentityRule AND AuthorityRule (T1.4,
# ADR-0050 addendum) — soft-disable only, never delete.


_LIFECYCLE_RULE_LABELS = (("IdentityRule", "ir"), ("AuthorityRule", "ar"), ("JoinPath", "jp"))


def _identity_rule_lifecycle_cypher(verb: str) -> str:
    """The IdentityRule lifecycle SET. Repeating the decision the rule already
    carries (deactivating a deactivated rule, reactivating an active one)
    keeps its recorded who/when, so the action can be run again (its cascade
    then marks any JoinPath an earlier run missed) and changes nothing else on
    the rule. Answers the stored who/when."""
    return ("MATCH (ir:IdentityRule {id: $rid}) "
            "WITH ir, coalesce(ir.status, '') = $status AS same_decision "
            "SET ir.status = $status, "
            f"ir.{verb}_by = CASE WHEN same_decision THEN ir.{verb}_by ELSE $actor END, "
            f"ir.{verb}_at = CASE WHEN same_decision THEN ir.{verb}_at ELSE $now END "
            f"RETURN ir.id AS id, ir.{verb}_by AS {verb}_by, ir.{verb}_at AS {verb}_at")


def _apply_rule_lifecycle(label: str, alias: str, rule_id: str, actor: str,
                          *, deactivate: bool) -> dict | None:
    """Run the soft-lifecycle SET for ONE label; None when the id is absent.

    An IdentityRule's JoinPaths follow it (``joinpath_rule_link``): its
    deactivation marks them switched off, its reactivation restores only the
    ones it marked. A direct JoinPath decision drops any such mark, so the
    steward's own decision on the JoinPath is the one that stands."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    verb = "deactivated" if deactivate else "reactivated"
    if label == "IdentityRule":
        cypher = _identity_rule_lifecycle_cypher(verb)
    else:
        cypher = (
            f"MATCH ({alias}:{label} {{id: $rid}}) "
            f"SET {alias}.status = $status, {alias}.{verb}_by = $actor, {alias}.{verb}_at = $now "
            + (_jp_rule.DIRECT_DECISION_CLAUSE + _jp_rule.DIRECT_DECISION_RETURN
               if label == "JoinPath" else f"RETURN {alias}.id AS id"))
    rows = neptune_query(
        cypher,
        {"rid": rule_id, "status": "deactivated" if deactivate else "active",
         "actor": actor, "now": now},
    )
    if not rows:
        return None
    result = {"status": "deactivated" if deactivate else "active", "id": rule_id,
              f"{verb}_by": actor, f"{verb}_at": now}
    if label == "IdentityRule":
        stored = rows[0] if isinstance(rows[0], dict) else {}
        for key in (f"{verb}_by", f"{verb}_at"):
            if key in stored:
                result[key] = stored[key]
        return _cascade_rule_joinpaths(result, rule_id, actor, now, deactivate=deactivate)
    if label == "JoinPath":
        return _with_effective_joinpath_status(result, rows[0])
    return result


def _cascade_rule_joinpaths(result: dict, rule_id: str, actor: str, now: str,
                            *, deactivate: bool) -> dict:
    """Switch an IdentityRule's JoinPaths off or back on with it, reporting
    how many changed (``join_paths_changed``). The write answers one row when
    it ran; no row is the fail-soft seam hiding a graph failure. On
    reactivation that leaves the rule active and its JoinPaths off, so the
    result reports the failure (HTTP 502 at the route). On deactivation every
    reader already leaves those JoinPaths out through the rule, so a missed
    mark is only logged."""
    rows = _jp_rule.cascade_rule_lifecycle(neptune_query, rule_id, actor, now,
                                           deactivate=deactivate)
    if rows:
        changed = rows[0].get("changed") if isinstance(rows[0], dict) else None
        return {**result, "join_paths_changed": changed} if isinstance(changed, int) else result
    if deactivate:
        logger.warning("rule %s deactivated; marking its join paths did not run "
                       "(readers leave them out through the rule)", rule_id[:96])
        return result
    logger.warning("rule %s reactivated; restoring its join paths did not run", rule_id[:96])
    return {**result, "http_status": 502,
            "error": (f"rule {rule_id} was reactivated, but the join paths switched off "
                      "with it could not be restored; reactivate it again")}


def _with_effective_joinpath_status(result: dict, row: object) -> dict:
    """A direct JoinPath decision reports the status every reader sees: a
    JoinPath whose IdentityRule is switched off stays off whatever its own
    status (``own_status``), until that rule is reactivated."""
    if not (isinstance(row, dict) and row.get("rule_inactive") is True):
        return result
    return {**result, "status": "deactivated", "own_status": result["status"],
            "inactive_rule_id": row.get("rule_id")}


@invalidates_metric_turn_results
def _set_rule_lifecycle(rule_id: str, actor: str, *, deactivate: bool) -> dict:
    """Soft deactivate/reactivate a rule node by id — IdentityRule first,
    then AuthorityRule, then JoinPath.

    ADR-0078: an AuthorityRule status change re-points resolver authority, so
    the per-turn metric-layer result cache drops on EVERY exit (found, not
    found, or a raise)."""
    for label, alias in _LIFECYCLE_RULE_LABELS:
        result = _apply_rule_lifecycle(
            label, alias, rule_id, actor, deactivate=deactivate)
        if result is not None:
            return result
    return {"error": f"rule {rule_id} not found (IdentityRule, AuthorityRule or JoinPath)"}


def deactivate_identity_rule(rule_id: str, request: Request):
    """Soft-disable a rule (removed from decisions; kept for audit).

    Admin-gated explicitly: /api/thread/rules/* sits OUTSIDE the /api/admin
    middleware prefix, so the steward gate is applied here and FAILS CLOSED
    (absent verdict == non-admin). Sync body (the graph write); the route
    offloads it."""
    from fastapi.responses import JSONResponse
    _check_steward(request)
    result = _set_rule_lifecycle(rule_id, _request_actor(request), deactivate=True)
    if "error" in result:
        return JSONResponse(status_code=result.pop("http_status", 404), content=result)
    return result


@app.post("/api/thread/rules/{rule_id}/deactivate")
async def deactivate_identity_rule_route(rule_id: str, request: Request):
    """Soft-disable a rule (steward-gated). Sync body: ``deactivate_identity_rule``."""
    return await offload(deactivate_identity_rule, rule_id, request)


def reactivate_identity_rule(rule_id: str, request: Request):
    """Restore a previously deactivated rule to decisions (admin-gated —
    see deactivate_identity_rule). Sync body (the graph write); the route
    offloads it."""
    from fastapi.responses import JSONResponse
    _check_steward(request)
    result = _set_rule_lifecycle(rule_id, _request_actor(request), deactivate=False)
    if "error" in result:
        return JSONResponse(status_code=result.pop("http_status", 404), content=result)
    return result


@app.post("/api/thread/rules/{rule_id}/reactivate")
async def reactivate_identity_rule_route(rule_id: str, request: Request):
    """Restore a deactivated rule (steward-gated). Sync body: ``reactivate_identity_rule``."""
    return await offload(reactivate_identity_rule, rule_id, request)


# Orchestrator-runtime admission (H2): ownership tokens, not a shared boolean.
# The coordinator lives in agents/query_admission.py — see its module docstring
# for the three defects the process-global ``_query_in_progress`` flag caused.
from .query_admission import get_admission  # noqa: E402




def _prepare_question(body: AskRequest) -> str:
    """Validate and enrich the question with per-request context brackets:
    temporal context (fresh 'now' each request — agents.time_context) and,
    when a plant is selected, plant scope."""
    question = body.question
    if not question:
        return ''
    if body.plant and body.plant.lower() != 'all':
        question = (
            f'[Plant context: {body.plant} — scope Neptune queries to systems at this plant. '
            f'If data isn\'t found here, mention that and ask if the user wants to search other plants.] {question}'
        )
    # Rendered at request time (NOT import time) so the timestamp is fresh per
    # turn — the orchestrator system prompt is built once at import and (in
    # cloud mode) lives on the remote AgentCore runtime, so it cannot carry
    # 'now'. Callers that omit timezone/locale (synthetic flows) get UTC-only.
    # Saved-prompt runs (agents/prompts.py) enrich through the same helper.
    from . import time_context
    return time_context.with_time_context(
        question, body.question,
        getattr(body, 'timezone', None), getattr(body, 'locale', None))


def _local_sync_integrity_answer(question: str, session_id: str, user_id: str) -> str:
    """Consume the same integrity-enforced local stream used by `/api/ask`."""
    from .answer_runs import answer_text
    from .chat_client import LocalChatClient
    return answer_text(LocalChatClient().stream(
        prompt=question, session_id=session_id, user_id=user_id, write_memory=False,
    ))


def _ask_sync_cloud(question: str, session_id: str, user_id: str) -> dict:
    """Sync body of ``/api/ask/sync``'s cloud branch (runs in an offload
    worker): drain the AgentCore stream into the final answer JSON, rebuilt
    from its text runs the way the chat does (``agents/answer_runs.py``)."""
    from .answer_runs import answer_text
    from .chat_client import AgentCoreChatClient
    client = AgentCoreChatClient()
    return {'answer': answer_text(client.stream(
        prompt=question, session_id=session_id, user_id=user_id, write_memory=False,
    ))}


@app.post('/api/ask/sync')
async def ask_sync(body: AskRequest):
    """Synchronous query — returns the final answer as JSON (no SSE).

    Used by the Quick Lambda proxy which needs a single JSON response
    rather than an SSE stream. Uses AgentCoreChatClient in cloud mode
    so it goes through the proper runtime path.
    """
    question = _prepare_question(body)
    if not question:
        return {'error': 'No question provided'}

    import asyncio
    import uuid as _uuid
    cfg = get_config()
    session_id = getattr(body, 'session_id', None) or str(_uuid.uuid4())
    user_id = getattr(body, 'user_id', None) or 'sync-user'

    if cfg.has_runtime:
        # The cloud stream is sync I/O (boto3 invoke + the ADR-0078 metric-layer
        # resolve) — never inline on the event loop (agents/offload.py).
        return await offload(_ask_sync_cloud, question, session_id, user_id)

    from .chat_client import _local_integrity_mode
    if _local_integrity_mode():
        answer = await asyncio.to_thread(
            _local_sync_integrity_answer, question, session_id, user_id,
        )
        return {'answer': answer}
    return await offload(_ask_sync_local, question, session_id)


def _ask_sync_local(question: str, session_id: str) -> dict:
    """Sync body of ``/api/ask/sync``'s local legacy branch (offload worker):
    the ADR-0078 metric-layer notice is composed here (sync I/O), then the
    singleton orchestrator answers."""
    from .chat_client import _local_metric_layer_prompt
    orchestrator = get_orchestrator()
    answer = str(orchestrator(_local_metric_layer_prompt(question, question, session_id)))
    # Drain the singleton's chart queue — append the tags to the
    # answer (the frontend renders chart-blocks in markdown); without the
    # drain, redacted charts were lost AND the process-wide queue grew.
    from .orchestrator import drain_pending_charts
    tags = drain_pending_charts(orchestrator)
    if tags:
        answer = answer + "\n\n" + "\n\n".join(tags)
    return {'answer': answer}


# ── Async query (submit + poll) — the AWS Quick path ─────────────────────
# AWS Quick's MCP connector enforces a fixed 60s timeout per tool call
# (HTTP 424 on exceed), but manufacturing queries routinely run 90-120s. A
# single synchronous call (/api/ask/sync) therefore cannot be used from the
# Quick MCP path. These two endpoints implement the standard MCP long-running
# pattern: submit returns a job_id immediately (<1s); result polls for the
# answer. Each call stays well under 60s while the query itself runs as long
# as needed in a background thread.
#
# ADDITIVE — /api/ask (the web UI's SSE path) and /api/ask/sync are untouched,
# and it reuses the same AgentCoreChatClient. The orchestrator and its tools
# are not modified. Reached without a browser session ONLY through the
# x-quick-auth gateway-token gate (agents/auth/middleware.py).
#
# Job state rides JobStore("ask") (DynamoDB write-through when configured, so
# polls are replica-safe; in-memory otherwise). ONE concurrent async ask
# app-wide via the atomic claim_slot/release_slot slot idiom (the try-it
# posture) — a kiosk/connector burst cannot pile parallel orchestrator runs.

_ASK_ACTIVE_KEY = "active"       # ONE concurrent async ask app-wide
_ASK_STALE_SECONDS = 600         # 10 min: stuck-job takeover threshold
#: A job still 'running' this long after it started is reported as an error
#: by the result route (and releases the slot if it still holds it), so a
#: poller never waits on a wedged job forever. A real query can take 11+
#: minutes; the 600 s takeover above only lets a NEW submit claim the slot.
#: ONE home: agents/turn_limits.py (the gateway bearer refresh is sized from it).
_ASK_RUN_CEILING_SECONDS = ASK_RUN_CEILING_SECONDS
#: Past the ceiling the job is ABANDONED (its result, if it ever finishes, is
#: discarded) and its run is stopped: the job's own AgentCore runtime session
#: is stopped and the worker's stream closed (_stop_abandoned_ask). The 600 s
#: takeover never stops a run.
_ASK_RUN_CEILING_DETAIL = (f"the query ran longer than {_ASK_RUN_CEILING_SECONDS // 60} "
                           "minutes and was abandoned; ask again")
#: A submit is refused while MORE than this many earlier workers that no
#: longer hold the slot (taken over after _ASK_STALE_SECONDS, or abandoned at
#: the ceiling) are still alive anywhere in the deployment, so wedged runs
#: cannot pile up. They are recorded in the shared slot record ("detached":
#: job id -> lease time), which every writer changes through JobStore.mutate.
#: An abandoned worker whose runtime stop failed keeps its lease, so it still
#: counts here until the run ends.
_ASK_ABANDONED_LIMIT = 1
_ASK_ABANDONED_BUSY = ("two earlier queries were abandoned and are still finishing; "
                       "retry in a few minutes")
#: A detached entry is a LEASE: the worker's own process renews it this often
#: while the worker thread lives (_ask_lease_renewer), and the worker removes
#: it when it ends. A worker can live far past any fixed age (the runtime's
#: idle heartbeats keep a wedged stream open), so age alone never ends a lease.
_ASK_LEASE_RENEW_SECONDS = 60.0
#: A lease not renewed for this long belongs to a process that died, and no
#: longer counts.
_ASK_LEASE_GRACE_SECONDS = 3 * _ASK_LEASE_RENEW_SECONDS
#: A renewal whose write did not land is retried this soon, well inside the
#: grace, instead of waiting a whole interval.
_ASK_LEASE_RETRY_SECONDS = 10.0
#: This process's live ask worker threads, by job id (each removes itself
#: when it ends), and its one lease renewer thread.
_ASK_LOCAL_WORKERS: "dict[str, threading.Thread]" = {}
_ASK_LOCAL_LOCK = threading.Lock()
#: Each live local worker's run, by job id (registered and removed with the
#: worker): the handle that closes its runtime stream, the runtime session it
#: invoked and when it started. Kept here, not only on the job record, so the
#: worker's own process can still stop the run after the record expires.
_LocalAskRun = collections.namedtuple("_LocalAskRun",
                                      ("cancel", "runtime_session_id", "started_at"))
_ASK_LOCAL_RUNS: "dict[str, _LocalAskRun]" = {}
#: Job ids whose runtime stop is in flight in this process (one at a time).
_ASK_STOPPING: "set[str]" = set()
#: The one ceiling-check thread (:func:`_reap_local_asks`) the renewer starts;
#: it runs apart from the renewer so a slow store read never delays a lease.
_ASK_REAPER: dict = {"thread": None}
_ASK_RENEWER: dict = {"thread": None}
#: Wakes the renewer before its interval ends (a renewal pass now).
_ASK_RENEWER_WAKE = threading.Event()
# Server-side wait for a running job before answering a poll. Capped BELOW
# the API Gateway REST integration timeout (29s) so a poll through the Quick
# API Gateway always returns a real response rather than a 504 — a 90-120s
# query then resolves in ~6-7 short polls, each well under both the API
# Gateway 29s and AWS Quick's 60s per-call limits.
_ASK_RESULT_WAIT_SECONDS = 20.0
#: One step of that wait: a running job's record is re-read this often.
_ASK_RESULT_POLL_STEP_SECONDS = 1.5


def _ask_result_clock() -> float:
    """Monotonic seconds for the result hold; a seam so tests can drive it."""
    import time as _time
    return _time.monotonic()


def _ask_wall_clock() -> float:
    """Epoch seconds (a job's ``started_at`` clock); a seam so tests can drive it."""
    import time as _time
    return _time.time()


def _ask_job_overdue(job: dict) -> bool:
    """True when ``job`` is still running past :data:`_ASK_RUN_CEILING_SECONDS`
    since its ``started_at``. A record with no usable start time is never
    overdue (it keeps the old behaviour)."""
    if job.get("status") != "running":
        return False
    try:
        started = float(job.get("started_at"))
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(started) and _ask_wall_clock() - started >= _ASK_RUN_CEILING_SECONDS


def _expire_overdue_ask(jobs, job_id: str, job: dict) -> dict:
    """Abandon an overdue running job: replace its record with a terminal
    ``error`` tombstone (conditional ``running -> terminal``, so a worker that
    just finished keeps its answer: the current record is returned instead),
    and release the app-wide slot if this job still holds it (exact owner; a
    newer job that took the slot over keeps it). The run is then stopped in the
    background (:func:`_stop_abandoned_ask`); the worker's own completion write
    is conditional too, so it can never recreate or overwrite the tombstone.
    The tombstone keeps the owner stamps (an owned job stays invisible to
    others) and the runtime session id (so the worker's own process can stop
    the run too), and lives until the store's TTL. Neither is ever part of a
    poll's reply."""
    import time as _time
    tombstone = {"status": "error", "error": "ask_failed",
                 "error_detail": _ASK_RUN_CEILING_DETAIL, "abandoned": True,
                 **{k: job[k] for k in ("started_at", "submitter", "actor", "actor_kind",
                                        "runtime_session_id")
                    if k in job}}
    # Record the worker as detached BEFORE the tombstone: if it finishes in
    # between, its own release removes the entry (or the undo below does).
    if not _ask_slot_detach(jobs, job_id, _ask_wall_clock()):
        # Logged; the abandon still stands, and the ceiling check in the
        # worker's own process puts the entry back while the worker lives.
        logger.warning("Async ask job %s abandoned but recording it as detached "
                       "did not land", job_id)
    if not jobs.finish_if_running(job_id, tombstone):
        current = jobs.get(job_id)
        if isinstance(current, dict) and _ask_memory_evictable(current):
            # The worker's own terminal record (``done``, or a non-abandoned
            # ``error``): it ended, undo the entry.
            _ask_slot_forget(jobs, job_id)
        # Otherwise keep it. Another expiry may have won (the poll racing the
        # ceiling check) while the worker is still alive, and a stale, failed
        # or malformed read proves nothing. A worker that did finish has a
        # lease nobody renews, so the entry runs out after the grace.
        return current if isinstance(current, dict) else {"status": "unknown"}
    logger.warning("Async ask job %s still running after %ss: abandoned",
                   job_id, _ASK_RUN_CEILING_SECONDS)
    _stop_abandoned_ask(job_id, job)
    return {**tombstone, "error": "ask_failed"}


def _stop_abandoned_ask(job_id: str, job: dict) -> None:
    """End an abandoned job's run in the background, so the poll answers at once.

    Stops the job's AgentCore runtime session (the id the submit minted and
    kept on the record), then, only when the runtime confirms it is stopped or
    already gone, closes this process's stream for the job, which ends the
    worker. On a failed stop the worker keeps reading: its detached lease then
    still counts toward :data:`_ASK_ABANDONED_LIMIT` while the run goes on. A
    record from before the id was kept, or a deployment with no runtime ARN,
    is left as before. Any replica may run this; a second stop of the same
    session answers ``not_found``."""
    from .chat_client import is_runtime_session_id
    rsid = job.get("runtime_session_id") if isinstance(job, dict) else None
    cfg = get_config()
    runtime_arn = getattr(cfg, "agentcore_runtime_arn", None)
    if not (is_runtime_session_id(rsid) and runtime_arn):
        return
    region = getattr(cfg, "aws_region", None)
    with _ASK_LOCAL_LOCK:
        if job_id in _ASK_STOPPING:          # a poll and a renewer pass overlapped
            return
        _ASK_STOPPING.add(job_id)
    try:
        threading.Thread(target=_stop_ask_runtime, args=(job_id, rsid, runtime_arn, region),
                         name="ask-stop", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - the abandon stands; the next renewer pass retries
        with _ASK_LOCAL_LOCK:
            _ASK_STOPPING.discard(job_id)
        logger.warning("Async ask job %s abandoned but its runtime stop could not start: %s",
                       job_id, type(exc).__name__)


def _stop_ask_runtime(job_id: str, rsid: str, runtime_arn: str, region) -> None:
    """(Background.) Stop ``rsid``; on success close the local stream.

    What a ``not_found`` answer means depends on the run's state when the stop
    was SENT, so that state is taken first. Not invoked yet: the invoke is
    refused instead and nothing is sent (no session can exist). The runtime
    had already answered (the body armed): the session existed before the
    request, so ``not_found`` means it is gone and our side is closed. The
    invoke was in flight: the ``not_found`` may predate the new session, so
    the worker keeps reading and the next ceiling check stops it again."""
    from . import chat_client as _chat_client
    try:
        with _ASK_LOCAL_LOCK:
            run = _ASK_LOCAL_RUNS.get(job_id)
        cancel = run.cancel if run is not None else None
        if cancel is not None and cancel.prevent_invoke():
            logger.info("Async ask job %s abandoned before its runtime was invoked; "
                        "the invoke is refused", job_id)
            return
        armed_before = cancel is not None and cancel.armed
        outcome = _chat_client.stop_runtime_session(rsid, runtime_arn=runtime_arn,
                                                    region=region)
        if outcome not in ("stopped", "not_found"):
            logger.warning("Async ask job %s abandoned but its runtime stop failed: %s",
                           job_id, outcome)
            return
        if cancel is None:
            logger.info("Async ask job %s abandoned: runtime session %s", job_id, outcome)
            return
        if outcome == "stopped" or armed_before:
            # The session existed and is now stopped or gone: free the worker.
            cancel.cancel()
        else:
            logger.info("Async ask job %s abandoned: runtime session not found yet; "
                        "retrying", job_id)
            return
        logger.info("Async ask job %s abandoned: runtime session %s", job_id, outcome)
    finally:
        with _ASK_LOCAL_LOCK:
            _ASK_STOPPING.discard(job_id)


def _reap_local_asks(jobs, live: "set[str]") -> None:
    """(Ceiling-check pass.) Give up this process's live workers whose run
    nobody ended. One whose record is still running past the ceiling is
    abandoned here as a poll would. One whose record says abandoned (another
    replica's poll, or a stop that failed or whose stream close failed) is
    stopped again with the session id this process kept. One whose record is
    gone (its TTL ran out) is stopped once its own start is past the ceiling.
    Each stop is retried every pass for as long as the worker lives."""
    now = _ask_wall_clock()
    for job_id in live:
        with _ASK_LOCAL_LOCK:
            run = _ASK_LOCAL_RUNS.get(job_id)
        job = jobs.get(job_id)
        if isinstance(job, dict) and job.get("status") == "running":
            if _ask_job_overdue(job):
                _expire_overdue_ask(jobs, job_id, job)
            continue
        if run is None:
            continue
        given_up = (bool(job.get("abandoned")) if isinstance(job, dict)
                    else now - run.started_at >= _ASK_RUN_CEILING_SECONDS)
        if given_up:
            # Count it while it lives: a detach that never landed, or a lease
            # that lapsed, is put back before the stop is tried again.
            _ask_slot_ensure_detached(jobs, job_id, now)
            _stop_abandoned_ask(job_id, {"runtime_session_id": run.runtime_session_id})


def _start_ask_reaper(live: "set[str]") -> None:
    """(Renewer pass.) Run :func:`_reap_local_asks` on its own thread, at most
    one at a time: its store reads and stop calls can be slow, and the lease
    renewal that keeps an abandoned worker counted must never wait on them."""
    with _ASK_LOCAL_LOCK:
        current = _ASK_REAPER["thread"]
        if current is not None and current.is_alive():
            return
        reaper = threading.Thread(target=_run_ask_reaper, args=(frozenset(live),),
                                  name="ask-reaper", daemon=True)
        _ASK_REAPER["thread"] = reaper
    try:
        reaper.start()
    except Exception as exc:  # noqa: BLE001 - the next renewer pass starts another
        logger.warning("ask ceiling check could not start: %s", type(exc).__name__)


def _run_ask_reaper(live: "frozenset[str]") -> None:
    try:
        _reap_local_asks(_ask_jobs(), set(live))
    except Exception as exc:  # noqa: BLE001 - the next renewer pass retries
        logger.warning("ask ceiling check failed: %s", type(exc).__name__)


def _ask_detached(slot: dict, now: float) -> dict:
    """The slot's detached workers (job id -> lease time) whose lease is
    live: renewed within :data:`_ASK_LEASE_GRACE_SECONDS`."""
    out = {}
    for jid, leased in dict((slot or {}).get("detached") or {}).items():
        try:
            if now - float(leased) < _ASK_LEASE_GRACE_SECONDS:
                out[jid] = float(leased)
        except (TypeError, ValueError):
            continue
    return out


class _LeaseRenewalLost(RuntimeError):
    """A lease renewal had leases to renew but its write never landed."""


def _renew_detached_leases(jobs, live: "set[str]", now: float) -> int:
    """Renew the shared leases of this process's ``live`` workers that are
    detached; how many were renewed. Only an entry that is present AND whose
    lease is still live is renewed: one that was removed (its worker ended)
    or has expired is never brought back. One ``JobStore.mutate``, so a
    renewal that read the slot before another replica changed it re-reads.
    Raises :class:`_LeaseRenewalLost` when there were leases to renew and the
    write never landed (the renewer then retries soon)."""
    renewed: dict = {}

    def _renew(cur):
        if not isinstance(cur, dict):
            renewed["n"] = 0
            return None
        detached = _ask_detached(cur, now)
        mine = [jid for jid in detached if jid in live]
        renewed["n"] = len(mine)
        if not mine:
            return None
        return {**cur, "detached": {**detached, **{jid: now for jid in mine}}}
    landed = jobs.mutate(_ASK_ACTIVE_KEY, _renew)
    if not landed and renewed.get("n", 0):
        raise _LeaseRenewalLost(f"{renewed['n']} lease(s) not renewed")
    return renewed.get("n", 0) if landed else 0


def _ask_lease_renewer() -> None:
    """This process's lease renewer: every :data:`_ASK_LEASE_RENEW_SECONDS`
    (or when woken), renew the detached leases of its live workers. Exits
    when the process has no live worker (the next ask elects a new one).
    However it ends, its ``finally`` clears its ownership and, when live
    workers remain (it died unexpectedly, or a worker arrived as it was
    leaving), elects a replacement that renews at once."""
    me = threading.current_thread()
    wait = _ASK_LEASE_RENEW_SECONDS
    try:
        while True:
            _ASK_RENEWER_WAKE.wait(wait)
            _ASK_RENEWER_WAKE.clear()
            with _ASK_LOCAL_LOCK:
                live = _ask_live_local_workers_locked()
                if not live:
                    if _ASK_RENEWER["thread"] is me:
                        _ASK_RENEWER["thread"] = None
                    return
            wait = _ASK_LEASE_RENEW_SECONDS
            try:
                _renew_detached_leases(_ask_jobs(), live, _ask_wall_clock())
            except Exception as exc:  # noqa: BLE001 - retried soon; the grace covers 3 misses
                logger.warning("ask lease renewal failed: %s; retrying in %ss",
                               type(exc).__name__, _ASK_LEASE_RETRY_SECONDS)
                wait = _ASK_LEASE_RETRY_SECONDS
            _start_ask_reaper(live)
    finally:
        with _ASK_LOCAL_LOCK:
            if _ASK_RENEWER["thread"] is me:
                _ASK_RENEWER["thread"] = None
            if _ask_live_local_workers_locked():
                logger.warning("ask lease renewer ended with live workers; starting a replacement")
                _elect_ask_renewer_locked(renew_now=True)


def _ask_live_local_workers_locked() -> "set[str]":
    """(Caller holds ``_ASK_LOCAL_LOCK``.) Job ids of this process's live workers."""
    return {jid for jid, t in _ASK_LOCAL_WORKERS.items() if t.is_alive()}


def _elect_ask_renewer_locked(*, renew_now: bool = False) -> None:
    """(Caller holds ``_ASK_LOCAL_LOCK``.) Start the lease renewer unless a
    live one owns the role; ``renew_now`` makes the new one renew at once."""
    renewer = _ASK_RENEWER["thread"]
    if renewer is not None and renewer.is_alive():
        return
    renewer = threading.Thread(target=_ask_lease_renewer, name="ask-lease-renewer",
                               daemon=True)
    _ASK_RENEWER["thread"] = renewer
    if renew_now:
        _ASK_RENEWER_WAKE.set()
    renewer.start()


def _register_ask_worker(job_id: str, worker: "threading.Thread",
                         run: "_LocalAskRun | None" = None) -> bool:
    """Start ``worker``, record it as this process's live worker for
    ``job_id`` (with its ``run``: stream handle, runtime session, start, when
    given) and make sure the lease renewer runs, as ONE operation under
    ``_ASK_LOCAL_LOCK``: the renewer scans the registry under the same lock,
    so it can never see the new worker before it is alive (and leave it
    unrenewed). ``False`` when the thread could not start (nothing recorded)."""
    with _ASK_LOCAL_LOCK:
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 - reported to the caller, which rolls back
            logger.error("ask worker for %s could not start: %s", job_id, type(exc).__name__)
            return False
        _ASK_LOCAL_WORKERS[job_id] = worker
        if run is not None:
            _ASK_LOCAL_RUNS[job_id] = run
        _elect_ask_renewer_locked()
        return True


def _claim_ask_slot(jobs, job_id: str, now: float) -> str:
    """Take the app-wide ask slot for ``job_id``: ``""``, else ``"busy"`` (a
    fresh holder) or ``"abandoned"`` (more than :data:`_ASK_ABANDONED_LIMIT`
    detached workers would be alive). A stale holder is taken over and joins
    the detached set. One ``JobStore.mutate`` on the shared slot record, so
    every replica decides on the same state."""
    why: dict = {}

    def _take(cur):
        cur = cur if isinstance(cur, dict) else {}
        detached = _ask_detached(cur, now)
        holder = cur.get("job_id")
        if cur.get("status") == "running" and holder:
            try:
                started = float(cur.get("started_at"))
            except (TypeError, ValueError, OverflowError):
                started = float("nan")
            if math.isfinite(started) and now - started < _ASK_STALE_SECONDS:
                why["reason"] = "busy"
                return None
            detached[holder] = now
        if len(detached) > _ASK_ABANDONED_LIMIT:
            why["reason"] = "abandoned"
            return None
        why.pop("reason", None)
        return {"status": "running", "job_id": job_id, "started_at": now,
                "detached": detached}
    return "" if jobs.mutate(_ASK_ACTIVE_KEY, _take) else why.get("reason", "busy")


def _ask_slot_detach(jobs, job_id: str, now: float) -> bool:
    """``job_id``'s worker is abandoned but alive: add it to the detached set
    and, if it still holds the slot, release the slot (exact owner). ``False``
    when the write never landed."""
    def _detach(cur):
        cur = dict(cur) if isinstance(cur, dict) else {}
        detached = _ask_detached(cur, now)
        detached[job_id] = now
        if cur.get("job_id") == job_id and cur.get("status") == "running":
            cur.update(status="idle", finished_at=now)
        return {**cur, "detached": detached}
    return bool(jobs.mutate(_ASK_ACTIVE_KEY, _detach))


def _ask_slot_ensure_detached(jobs, job_id: str, now: float) -> None:
    """(Ceiling check.) Make sure a given-up worker of THIS process that is
    still alive has a live detached entry; leave a live one as it is. The
    caller only passes a worker it just saw alive, so at worst an entry for a
    worker that ended in between stays until its unrenewed lease runs out."""
    def _ensure(cur):
        cur = dict(cur) if isinstance(cur, dict) else {}
        detached = _ask_detached(cur, now)
        if job_id in detached:
            return None
        detached[job_id] = now
        return {**cur, "detached": detached}
    if not jobs.mutate(_ASK_ACTIVE_KEY, _ensure) and job_id not in _ask_detached(
            jobs.get(_ASK_ACTIVE_KEY) or {}, now):
        logger.warning("Async ask job %s: re-recording it as detached did not land", job_id)


def _ask_slot_release(jobs, job_id: str, now: float) -> None:
    """A worker ended: release the slot if it still holds it (exact owner, so
    a newer job's claim is untouched) and drop it from the detached set."""
    def _release(cur):
        if not isinstance(cur, dict):
            return None
        detached = dict(cur.get("detached") or {})
        dropped = detached.pop(job_id, None) is not None
        if cur.get("job_id") == job_id and cur.get("status") == "running":
            return {**cur, "status": "idle", "finished_at": now, "detached": detached}
        return {**cur, "detached": detached} if dropped else None
    jobs.mutate(_ASK_ACTIVE_KEY, _release)


def _ask_slot_forget(jobs, job_id: str) -> None:
    """Undo :func:`_ask_slot_detach`'s entry (the job finished first)."""
    def _forget(cur):
        if not isinstance(cur, dict) or job_id not in (cur.get("detached") or {}):
            return None
        detached = dict(cur["detached"])
        detached.pop(job_id)
        return {**cur, "detached": detached}
    jobs.mutate(_ASK_ACTIVE_KEY, _forget)


async def _ask_result_hold(jobs, job_id: str, job: dict, wait: "float | None",
                           started: float) -> dict:
    """Hold a poll while ``job`` is running; return the last record read.

    Without ``wait`` this is the original hold: fourteen 1.5 s steps (~21 s).
    ``wait`` (the hold the CALLER can afford; capped at
    _ASK_RESULT_WAIT_SECONDS, <= 0 or NaN means none) bounds the hold's WALL
    time from the request's start: a step is taken only when its sleep plus
    a read as slow as the slowest one so far still ends inside ``wait``. The
    Quick bridge sends it so the app's reply reaches the GET that asked for
    it, and the tool call does not end on a stale ``running``. Delivery is
    repeatable (:func:`_ask_result_reply`), so a single read slower than the
    caller's margin, after the last step, only costs the caller one more
    poll: the finished answer is still there (ADR-0071 correction
    2026-10-16)."""
    import asyncio

    step = _ASK_RESULT_POLL_STEP_SECONDS
    hold = None if wait is None else max(0.0, min(float(wait), _ASK_RESULT_WAIT_SECONDS))
    waited, slowest_read = 0.0, _ask_result_clock() - started
    while job.get("status") == "running" and (
            waited < _ASK_RESULT_WAIT_SECONDS if hold is None
            else (waited + step <= hold
                  and _ask_result_clock() - started + step + slowest_read <= hold)):
        await asyncio.sleep(step)
        waited += step
        read_at = _ask_result_clock()
        job = (await asyncio.to_thread(jobs.get, job_id)) or job
        slowest_read = max(slowest_read, _ask_result_clock() - read_at)
    return job
_ASK_JOBS = None  # lazy JobStore("ask") — keeps module import pure
#: Delivery no longer frees a finished record, so it answers until its record
#: expires, an hour after its last write (the store's TTL). The store enforces
#: that on every read: a table item past its ``ttl`` reads as absent (DynamoDB
#: deletes it lazily), and this process's memory copy of a finished record
#: stops answering at the same time. A memory-only ask store (local/offline,
#: or a shared table that failed) also keeps at most this many finished
#: copies, the oldest written dropped first. Only the copies delivery used to
#: free count (``_ask_memory_evictable``); with one ask admitted at a time a
#: finished answer outlives hundreds of later asks. A store backed by the
#: shared table drops only this process's copy; the table's item still
#: answers until its TTL.
_ASK_MEMORY_FINISHED_MAX = 256


def _ask_memory_evictable(job: dict) -> bool:
    """Whether the memory bounds (count and age) may drop ``job``'s copy: a
    ``done`` record or a non-abandoned ``error``. A running record, the slot
    record and an abandoned job's tombstone are never dropped."""
    status = job.get("status")
    return status == "done" or (status == "error" and not job.get("abandoned"))


def _ask_jobs():
    """The shared async-ask job registry (lazy: the try-it posture). Its TTL
    stamps and expiry checks run on :func:`_ask_wall_clock`, looked up at
    each call, so a test that moves that seam moves the store's clock too."""
    global _ASK_JOBS
    if _ASK_JOBS is None:
        from .job_store import JobStore
        _ASK_JOBS = JobStore("ask", memory_cap=_ASK_MEMORY_FINISHED_MAX,
                             memory_evictable=_ask_memory_evictable,
                             clock=lambda: _ask_wall_clock())
    return _ASK_JOBS


def _delegated_subject_payload(delegated_subject: "str | None") -> "dict | None":
    """ADR-0079: the RESERVED ``delegatedSubject`` runtime-payload key, or None.

    None (every anonymous ask) keeps the runtime payload byte-identical."""
    if not delegated_subject:
        return None
    from .auth.forwarding import DELEGATED_SUBJECT_FIELD
    return {DELEGATED_SUBJECT_FIELD: delegated_subject}


def _blocking_guardrail_action(event) -> "str | None":
    """A guardrail action that blocked the turn, as a log-safe token, or None.

    ``NONE``/empty did not block. Anything else is upper-cased and kept only
    when it is a bare ``[A-Z_]{1,40}`` token, else ``OTHER`` — it is written
    to the server log, never to the job record."""
    import re as _re
    action = str(event.action or "").strip().upper()
    if action in ("", "NONE"):
        return None
    return action if _re.fullmatch(r"[A-Z_]{1,40}", action) else "OTHER"


def _collect_ask_stream(events) -> "tuple[str, str | None, str | None]":
    """Fold one async-ask stream into ``(text, error_category, guardrail_action)``.

    Only ``MessageEvent`` text is the answer, rebuilt from its runs (a
    ``ToolUseEvent`` ends a run) the way the chat does, so the model's
    narration before a tool call is not relayed (``agents/answer_runs.py``).
    The FIRST ``ErrorEvent``'s
    category is kept, clamped to the closed ``ERROR_CATEGORIES`` vocabulary —
    never its message, which may carry internals. The first blocking
    ``GuardrailEvent`` action is kept as a log-safe token
    (``_blocking_guardrail_action``). ``DoneEvent`` text is not read:
    ``AgentCoreChatClient`` only ever yields an empty one."""
    from .answer_runs import AnswerRuns
    from .events import (ERROR_CATEGORIES, ErrorEvent, GuardrailEvent,
                         MessageEvent, ToolUseEvent)
    runs, category, action = AnswerRuns(), None, None
    for event in events:
        if isinstance(event, MessageEvent):
            runs.push_text(event.content)
        elif isinstance(event, ToolUseEvent):
            runs.tool_boundary(event.tool_name)
        elif isinstance(event, ErrorEvent) and category is None:
            category = (event.category if event.category in ERROR_CATEGORIES
                        else "unknown")
        elif isinstance(event, GuardrailEvent) and action is None:
            action = _blocking_guardrail_action(event)
    return runs.finalize(), category, action


def _ask_job_record(text: str, error_category: "str | None",
                    guardrail_action: "str | None") -> "tuple[dict, list[str]]":
    """``(record, reasons)`` for one async ask — never an empty ``done``.

    The record is public: the async path's only consumer is an external MCP
    client (the Quick query bridge relays it verbatim) with no control-block
    parser, so the answer is the external form. Text that survives it is
    ``done``; an error or a blocking guardrail alongside it adds a one-line
    ``warning`` (a partial answer is still an answer, but labelled). No
    surviving text is ``error``. Both name ONLY a closed ``ERROR_CATEGORIES``
    value: the error's category, else ``unknown``. ``reasons`` carry the
    precise cause (``empty_answer``, the guardrail action) for the server
    log only — never the record."""
    from .events import ERROR_CATEGORIES
    from .public_answer import external_answer_text
    answer = external_answer_text(text)
    reasons = [f"error_category={error_category}"] if error_category else []
    if guardrail_action:
        reasons.append(f"guardrail_action={guardrail_action}")
    public = error_category if error_category in ERROR_CATEGORIES else "unknown"
    if answer.strip():
        record = {"status": "done", "answer": answer}
        if reasons:
            record["warning"] = (
                f"answer may be incomplete: query reported {public}")
        return record, reasons
    return ({"status": "error", "error": "ask_failed",
             "error_detail": f"query failed: {public}"},
            reasons + ["empty_answer"])


def _run_ask_job(job_id: str, question: str, session_id: str, user_id: str,
                 delegated_subject: "str | None" = None,
                 runtime_session_id: "str | None" = None, cancel=None) -> None:
    """Background worker: run the full query and store the result under job_id.

    Errors are stored honestly (status ``error``) — a failed run must never
    wedge at "running", and a run that produced no answer is never stored as
    an empty ``done`` (``_ask_job_record``). The single app-wide slot is
    always released (exact-owner, so a stale worker cannot release a newer
    claim). A verified delegated subject (ADR-0079) rides to the orchestrator
    as a reserved payload key; the job record keeps its ``actor`` fields. The
    slot is released BEFORE the terminal record is written, so a poller that
    sees the result can submit again at once (the release waits for the
    worker's registration, which holds the process lock while it starts).
    ``runtime_session_id`` and ``cancel`` are the runtime session the submit
    kept on the record and the handle that closes this worker's stream, so an
    abandoned run can be stopped (:func:`_stop_abandoned_ask`)."""
    jobs = _ask_jobs()
    owner = {**_ask_submitter_field(jobs, job_id), **_ask_job_owner_fields(delegated_subject)}
    final = record_usage = None
    try:
        record, reasons, record_usage = _async_ask_answer(
            question, session_id, user_id, delegated_subject,
            runtime_session_id=runtime_session_id, cancel=cancel)
        if reasons:
            # The precise cause, server-side only: the job id and closed
            # tokens — never the question or upstream text.
            logger.warning("Async ask job %s status=%s reasons=%s",
                           job_id, record["status"], ",".join(reasons))
        final = {**record, **owner}
    except Exception as e:  # noqa: BLE001 — stored honestly, surfaced by the poll
        logger.error("Async ask job %s FAILED: %s", job_id, e, exc_info=True)
        # JobStore sanitizes the "error" field to the "<kind>_failed" sentinel
        # (raw exception text could carry internals); the try-it convention:
        # ride a one-line, type-only detail alongside it.
        final = {"status": "error", "error": "ask_failed",
                 "error_detail": f"query failed: {type(e).__name__}", **owner}
    finally:
        # No longer this process's live worker (its lease is not renewed
        # again), then out of the shared detached set and, if it still holds
        # it, out of the slot; then the result.
        with _ASK_LOCAL_LOCK:
            _ASK_LOCAL_WORKERS.pop(job_id, None)
            _ASK_LOCAL_RUNS.pop(job_id, None)
        if cancel is not None:
            cancel.release()
        _ask_slot_release(jobs, job_id, _ask_wall_clock())
        if final is not None:
            _complete_ask_job(jobs, job_id, final, record_usage)


def _complete_ask_job(jobs, job_id: str, final: dict, record_usage) -> None:
    """Write the worker's terminal record; then, only when that record is
    ``done`` AND its write landed (``_finish_ask_job``: the job was still
    running, so still this worker's), store the job's usage record. A failed
    job, or one abandoned at the ceiling, records no usage."""
    if not _finish_ask_job(jobs, job_id, final):
        # Abandoned in the gap: the expiry re-added this job to the
        # detached set after the release above. Release again (exact
        # owner: it only drops this id, never a newer holder's claim).
        _ask_slot_release(jobs, job_id, _ask_wall_clock())
        return
    if final.get("status") == "done" and record_usage is not None:
        record_usage()


def _async_ask_answer(question: str, session_id: str, user_id: str,
                      delegated_subject: "str | None", *,
                      runtime_session_id: "str | None" = None,
                      cancel=None) -> "tuple[dict, list[str], object]":
    """Run one async ask's stream to ``(record, reasons, record_usage)``:
    ``_ask_job_record``'s pair plus a callable that stores the run's usage
    record through the web chat's own writer (``_store_ask_usage``: same
    ``UsageRecord`` shape, same counting, latency measured at the end of the
    stream). It writes nothing itself: ``_complete_ask_job`` calls it only
    for a ``done`` job whose terminal write landed. A stream that raises
    propagates; the caller stores the error. The runtime is invoked with the
    job's own ``runtime_session_id`` and its stream is closable via ``cancel``."""
    import time as _time
    from .chat_client import AgentCoreChatClient
    start = _time.monotonic()
    stats = {"token_count": 0, "tools_used": []}
    collected = _collect_ask_stream(_usage_counted(AgentCoreChatClient().stream(
        prompt=question, session_id=session_id, user_id=user_id,
        write_memory=False,  # machine flow — never a Memory writer
        extra_payload=_delegated_subject_payload(delegated_subject),
        runtime_session_id=runtime_session_id, cancel=cancel,
    ), stats))
    elapsed = _time.monotonic() - start

    def record_usage() -> None:
        _store_ask_usage(None, user_id, session_id, stats, _time.monotonic() - elapsed)

    return (*_ask_job_record(*collected), record_usage)


def _finish_ask_job(jobs, job_id: str, record: dict) -> bool:
    """The worker's completion: a conditional ``running -> terminal`` write, so
    a job abandoned at the ceiling keeps its tombstone (the result is
    discarded) and its id can never answer ``done`` after ``error``. ``False``
    when the write was refused (the job was abandoned)."""
    if jobs.finish_if_running(job_id, record):
        return True
    logger.info("Async ask job %s finished after it was abandoned; result discarded",
                job_id)
    return False


def _ask_submitter_field(jobs, job_id: str) -> dict:
    """The running record's ``submitter``, for the worker to carry into the
    terminal record so a finished job stays visible only to its submitter
    (:func:`_job_visible_to`). Empty when the record has none."""
    job = jobs.get(job_id)
    if isinstance(job, dict) and "submitter" in job:
        return {"submitter": job["submitter"]}
    return {}


def _ask_job_owner_fields(delegated_subject: "str | None") -> dict:
    """ADR-0079: the job record's owner stamp — empty for anonymous asks."""
    if not delegated_subject:
        return {}
    return {"actor": delegated_subject, "actor_kind": "delegated"}


def _delegated_actor(request: Request) -> "str | None":
    """The VERIFIED delegated subject the auth middleware set, or None.

    Only ``AuthMiddleware._admit_quick_gateway`` sets ``state.delegated`` —
    after verifying the interceptor-minted claim (ADR-0079)."""
    state = getattr(request, "state", None)
    if not getattr(state, "delegated", False):
        return None
    user = getattr(state, "user", None)
    sub = getattr(user, "user_id", None)
    return sub if isinstance(sub, str) and sub else None


def _job_visible_to(job: dict, actor: "str | None",
                    principal: "str | None" = None) -> bool:
    """Whether a caller may see ``job``. An OWNED job (``actor`` stamped,
    ADR-0079) is visible ONLY to that verified actor. Any other job is
    visible only to a caller with no delegated actor whose admitted
    principal is exactly the recorded ``submitter`` (the shared Quick
    service principal is matched like any other). Every request that
    reaches a deployed app's API is admitted as a principal, so a record
    without a submitter answers only a caller that has none (local mode)."""
    owner = job.get("actor")
    if owner:
        return owner == actor
    if actor:
        return False
    return job.get("submitter") == principal


def _admitted_principal(request) -> "str | None":
    """The user id the auth middleware admitted this request as, or None."""
    return getattr(getattr(getattr(request, "state", None), "user", None), "user_id", None)


def _busy_running_job_id(jobs, actor: "str | None", now: float,
                         principal: "str | None" = None) -> "str | None":
    """The running job a 429 may name to THIS caller, or None.

    Named only when the active slot is fresh, its job record exists and is
    running, and the job is the caller's. An OWNED job (ADR-0079) is named
    only to its verified actor. An unowned job is named only to a caller
    with no delegated actor whose admitted principal submitted it: every
    service-auth Quick call is the one ``quick-gateway`` principal (the
    service-auth Quick path polls it, workshop module 1b), while a signed-in
    web user is a principal of their own. A plain read after the failed
    claim; the slot itself is never touched."""
    slot = jobs.get(_ASK_ACTIVE_KEY)
    if not isinstance(slot, dict) or slot.get("status") != "running":
        return None
    running = slot.get("job_id")
    if not isinstance(running, str) or not running or running == _ASK_ACTIVE_KEY:
        return None
    try:
        started = float(slot.get("started_at"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(started) or now - started >= _ASK_STALE_SECONDS:
        return None
    job = jobs.get(running)
    if not isinstance(job, dict) or job.get("status") != "running":
        return None
    if job.get("actor"):
        return running if job.get("actor") == actor else None
    return running if not actor and job.get("submitter") == principal else None


def _ask_busy_response(jobs, actor: "str | None", now: float,
                       principal: "str | None" = None, *,
                       error: str = "an async query is already running — retry shortly"):
    """The 429 for a submit that lost the claim (or was refused because
    abandoned workers are still finishing): ``status`` and ``error`` as
    always, plus ``running_job_id``: the caller's own running job, or None
    (always present, so the bridge never guesses from its own memory)."""
    from fastapi.responses import JSONResponse
    busy = {"status": "busy", "error": error,
            "running_job_id": _busy_running_job_id(jobs, actor, now, principal)}
    return JSONResponse(status_code=429, content=busy)


def ask_async(body: AskRequest, actor: "str | None" = None,
              principal: "str | None" = None):
    """Start a query in the background. Returns a job_id immediately (<1s).

    Poll /api/ask/result/{job_id} until status is 'done' or 'error'. Designed
    for the AWS Quick MCP path where each call must return within 60s but
    the query itself may take 90-120s+. 429 when an async ask is already
    running (ONE concurrent job app-wide — retry shortly). ``actor`` is the
    VERIFIED delegated subject (ADR-0079): it OVERRIDES the untrusted body
    ``user_id`` and owns the job; None keeps today's behaviour. ``principal``
    is the admitted caller (``request.state.user``), recorded as ``submitter``;
    the 429 names ``running_job_id`` only when that job is the caller's own
    (``_busy_running_job_id``)."""
    import uuid as _uuid

    question = _prepare_question(body)
    if not question:
        return {'error': 'No question provided'}
    cfg = get_config()
    if not cfg.has_runtime:
        # Async job path requires the AgentCore runtime (cloud mode). The
        # local-orchestrator fallback used by /api/ask/sync isn't wired for
        # detached jobs, so be explicit rather than silently degrade.
        return {'error': 'Async query requires the AgentCore runtime (cloud mode).'}

    jobs = _ask_jobs()
    job_id = _uuid.uuid4().hex[:12]
    now = _ask_wall_clock()
    # ONE atomic decision on the shared slot record (JobStore.mutate): a fresh
    # holder, or too many detached workers alive in the deployment, refuses;
    # a stale holder is taken over. The worker's release is exact-owner.
    refused = _claim_ask_slot(jobs, job_id, now)
    if refused:
        return _ask_busy_response(jobs, actor, now, principal,
                                  **({"error": _ASK_ABANDONED_BUSY}
                                     if refused == "abandoned" else {}))
    # The runtime session is minted here and kept on the record, so the run
    # can be stopped if the job is abandoned (_stop_abandoned_ask). It is
    # never part of a poll's reply (_ask_result_reply).
    from .chat_client import new_runtime_session_id
    runtime_session_id = new_runtime_session_id()
    jobs[job_id] = {"status": "running", "started_at": now,
                    "submitter": principal, "runtime_session_id": runtime_session_id,
                    **_ask_job_owner_fields(actor)}
    session_id = getattr(body, 'session_id', None) or f'async-{job_id}'
    user_id = actor or getattr(body, 'user_id', None) or 'async-user'
    logger.info("quick_delegated_identity outcome=%s",
                "verified" if actor else "absent")
    _start_ask_worker(job_id, question, session_id, user_id, actor, runtime_session_id, now)
    return {"job_id": job_id, "status": "running"}


def _start_ask_worker(job_id: str, question: str, session_id: str, user_id: str,
                      actor: "str | None", runtime_session_id: "str | None" = None,
                      started_at: "float | None" = None) -> None:
    """Start ``job_id``'s worker thread, registered as this process's live
    worker (its detached lease is renewed while it lives) with its local run:
    the handle that closes its runtime stream, its runtime session and its
    start. If the thread
    cannot start, nothing is recorded, the job is stored as an error and the
    slot it claimed is released, then the failure is raised."""
    from .chat_client import StreamCancellation
    cancel = StreamCancellation()
    run = _LocalAskRun(cancel, runtime_session_id,
                       _ask_wall_clock() if started_at is None else started_at)
    worker = threading.Thread(target=_run_ask_job,
                              args=(job_id, question, session_id, user_id, actor,
                                    runtime_session_id, cancel),
                              daemon=True)
    if _register_ask_worker(job_id, worker, run):
        return
    jobs = _ask_jobs()
    _finish_ask_job(jobs, job_id, {"status": "error", "error": "ask_failed",
                                   "error_detail": "the query could not be started"})
    _ask_slot_release(jobs, job_id, _ask_wall_clock())
    raise RuntimeError(f"ask worker for {job_id} could not start")


@app.post('/api/ask/async')
async def ask_async_route(body: AskRequest, request: Request):
    """EVENT-LOOP OFFLOAD (agents/offload.py) — sync body: ask_async."""
    return await offload(ask_async, body, _delegated_actor(request),
                         _admitted_principal(request))


@app.get('/api/ask/result/{job_id}')
async def ask_result(job_id: str, request: Request = None, wait: "float | None" = None):
    """Poll for an async query result.

    Returns {status: 'running'} or {status: 'done', answer: ...[, warning]}
    or {status: 'error', error: ...} or {status: 'unknown'}. To reduce the number
    of polls the caller must make, this waits up to ~20s server-side
    (_ASK_RESULT_WAIT_SECONDS — kept BELOW the API Gateway 29s integration
    timeout; see the constant's comment) for a running job to finish before
    returning. JobStore reads are offloaded (asyncio.to_thread) so the wait
    never blocks the single uvicorn event loop.

    ``wait`` (seconds, optional) is the hold the CALLER can afford; see
    :func:`_ask_result_hold`. Without ``wait`` the hold is unchanged.

    A job still running :data:`_ASK_RUN_CEILING_SECONDS` after it started is
    answered ``error`` at once (:func:`_expire_overdue_ask`), never held.
    A finished job answers the same on every later poll until its record
    expires (:func:`_ask_result_reply`).
    """
    import asyncio

    started = _ask_result_clock()
    jobs = _ask_jobs()
    if job_id == _ASK_ACTIVE_KEY:
        return {"status": "unknown", "error": f"No such job: {job_id}"}
    job = await asyncio.to_thread(jobs.get, job_id)
    if not isinstance(job, dict) or not _job_visible_to(
            job, _delegated_actor(request), _admitted_principal(request)):
        # ADR-0079: a job polled by anyone but its owner or submitter looks
        # exactly like a missing one: never a hint that it exists.
        return {"status": "unknown", "error": f"No such job: {job_id}"}
    if not _ask_job_overdue(job):
        job = await _ask_result_hold(jobs, job_id, job, wait, started)
    if _ask_job_overdue(job):
        job = await asyncio.to_thread(_expire_overdue_ask, jobs, job_id, job)
    return _ask_result_reply(job_id, job)


def _ask_result_reply(job_id: str, job: dict) -> dict:
    """The poll's answer for ``job``'s record. Delivery is repeatable: a
    ``done`` or ``error`` record is left in place, so every poll of a finished
    job answers the same terminal reply until the record expires, an hour
    after its last write. The store enforces that horizon on read: a table
    item past its ``ttl`` reads as absent although DynamoDB deletes it only
    later, and a memory copy of a finished record stops answering at the same
    time (a memory-only store also keeps at most the newest
    :data:`_ASK_MEMORY_FINISHED_MAX` finished records). An expired record
    answers ``unknown``, as a missing one does. A
    reply that reached a caller who had already stopped listening is not lost:
    the caller's next poll gets the same answer (2026-10-16). An abandoned
    job's tombstone answers the same way, and :func:`ask_result` checks the
    owner (ADR-0079) on every poll, so a re-delivery reaches only the owner."""
    status = job.get("status")
    if status == "done":
        # A partial answer's one-line ``warning`` rides along (the bridge
        # relays the dict verbatim).
        return {"status": "done", "answer": job.get("answer", ""),
                **({"warning": job["warning"]} if job.get("warning") else {})}
    if status == "error":
        return {"status": "error", "error": job.get("error", "unknown error"),
                **({"error_detail": job["error_detail"]}
                   if job.get("error_detail") else {})}
    if status == "unknown":          # the record vanished during the expiry
        return {"status": "unknown", "error": f"No such job: {job_id}"}
    return {"status": "running", "job_id": job_id}


def _ask_stream_identity(body: 'AskRequest', request: Request, is_briefing: bool):
    """Resolve (user, user_id, write_memory, user_access_token) for one
    /api/ask turn. Pure code motion out of ``ask`` (50-line gate).

    D1: persist to AgentCore Memory ONLY for genuine interactive chat — the
    synthetic ops-briefing requests (the 'briefing' session_id marker) must
    NOT write to memory. Task 1.2 / ADR-0033: forward the interactive user's
    adt-app access token to the orchestrator runtime; briefings are synthetic
    (no per-user token); no session token ⇒ None (field omitted, no
    fallback). Never logged (R4)."""
    user = getattr(request.state, "user", None)
    user_id = (user.user_id if user and hasattr(user, "user_id") else None) \
        or getattr(body, 'user_id', None) or 'dev-user'
    # Disable-only (T1.7): a body may SUPPRESS memory but never force it on —
    # `write_memory: true` on a briefing session stays suppressed.
    write_memory = (not is_briefing) and (
        getattr(body, 'write_memory', None) is not False)
    from .auth.forwarding import user_access_token_from_session
    user_access_token = None if is_briefing else user_access_token_from_session(
        getattr(request.state, "session", None)
    )
    return user, user_id, write_memory, user_access_token


def _ask_queue_maxsize() -> int:
    """Bound of one /api/ask event queue (``ASK_STREAM_QUEUE_MAX``, default 512).

    An UNBOUNDED queue would let an abandoned turn (client disconnect,
    aborted briefing) accumulate every remaining event in the app task's memory
    until the whole queue was garbage. The bound turns that into back-pressure the
    producer can notice. Malformed values fall back to 512."""
    try:
        return max(1, int(os.environ.get("ASK_STREAM_QUEUE_MAX", "512")))
    except (TypeError, ValueError):
        return 512


class _StreamWorker:
    """One /api/ask producer's handle: a BOUNDED event queue, the stop flag, and
    the per-invocation CANCEL handle.

    The consumer (:func:`_drain_event_queue`) sets ``stop`` and fires ``cancel``
    on EVERY exit path, including the ``GeneratorExit`` a client disconnect
    raises — which the producer thread otherwise cannot see at all. Before this,
    an abandoned turn kept driving the orchestrator to completion on a
    single-core task (2026-08-24: ~99.9-100% CPU for ~45 min at cpu=512) and
    kept queueing events nobody would ever read.

    ``stop`` alone is NOT enough: a producer parked inside
    ``response["response"].iter_lines()`` never reaches the between-events check,
    so ``cancel`` closes that per-invocation response BODY from outside the
    blocked read. No chat-client object is closed."""

    __slots__ = ("queue", "stop", "cancel")

    def __init__(self, maxsize: int | None = None) -> None:
        import queue as _queue
        import threading as _threading
        from .chat_client import StreamCancellation
        self.queue = _queue.Queue(
            maxsize=_ask_queue_maxsize() if maxsize is None else maxsize)
        self.stop = _threading.Event()
        self.cancel = StreamCancellation()


def _put_event(worker: '_StreamWorker', item) -> bool:
    """Queue ``item`` unless/until the consumer signalled stop → did it land?

    The queue is BOUNDED, so a put on a full queue must never block forever when
    nobody will read it again: poll with a timeout and re-check ``stop`` between
    attempts."""
    import queue as _queue
    while not worker.stop.is_set():
        try:
            worker.queue.put(item, timeout=0.5)
            return True
        except _queue.Full:
            continue
    return False


def _pump_stream(worker: '_StreamWorker', client, stream_kwargs: dict) -> None:
    """Pull ``client.stream`` into the worker's queue until it ends or stops.

    The worker's cancel handle rides IN ``stream_kwargs``: the client arms it
    with the per-invocation response body, so a disconnect can close that body
    from outside a blocked read. This ``finally`` also closes the ITERATOR (and
    releases the handle) so a cancelled turn leaves nothing behind; the chat
    client itself is process-global and is NEVER closed."""
    events = client.stream(**stream_kwargs)
    try:
        for event in events:
            if worker.stop.is_set() or not _put_event(worker, event):
                return
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()
        worker.cancel.release()


def _start_stream_worker(client, question, session_id, user_id, write_memory,
                         user_access_token, extra_payload=None):
    """Producer side of the /api/ask SSE stream: a daemon thread pumps
    ``client.stream`` events into a BOUNDED queue, ``None``-sentinel terminated
    (an exception becomes an ErrorEvent frame then the sentinel). Returns the
    :class:`_StreamWorker` handle (queue + stop flag). ``extra_payload``
    (reserved runtime keys, e.g. the "Ask again" retry flag) is passed to the
    client only when set, so an ordinary turn's call is unchanged.

    The ``except`` below is the LAST holder of the real failure: the downstream
    frame filter replaces the public frame with a generic "Query failed.", so an
    exception here was invisible everywhere — chat_client had already logged
    QUERY_SUCCESS for its own stream, and a reproducible 7-of-10 failure could
    not be named at all (2026-07-29). ``exc_info`` gives the code location; the
    message is digit-masked because it can quote row values (claim-check policy
    covers logs too)."""
    import threading as _threading
    worker = _StreamWorker()
    stream_kwargs = {"prompt": question, "session_id": session_id,
                     "user_id": user_id, "write_memory": write_memory,
                     "user_access_token": user_access_token,
                     "cancel": worker.cancel}
    if extra_payload:
        stream_kwargs["extra_payload"] = extra_payload

    def _stream_worker():
        try:
            _pump_stream(worker, client, stream_kwargs)
        except Exception as e:
            from .chat_client import _redact_digits, classify_error_text
            from .events import ErrorEvent, public_error_message
            from .failure_fingerprint import exception_fingerprint
            category = classify_error_text(f"{type(e).__name__}: {e}")
            # Digit-masked head only (claim-check policy covers logs too) and
            # NO exc_info — the traceback would re-render the unmasked message.
            # ``where`` is file:line:function frames only (no locals).
            logger.error("ASK_STREAM_WORKER_FAILED type=%s category=%s where=%s head=%s",
                         type(e).__name__, category,
                         exception_fingerprint(e)["where"], _redact_digits(e))
            _put_event(worker, ErrorEvent(
                message=public_error_message(category), category=category,
                correlation_id=session_id))
        finally:
            _put_event(worker, None)  # sentinel (a no-op once stop is set)

    _threading.Thread(target=_stream_worker, daemon=True).start()
    return worker


def _cancel_worker(worker: '_StreamWorker | None') -> None:
    """THE idempotent producer-cancel: stop the pump and close its live body.

    ONE helper, called from every ownership path — the drain generator's
    ``finally``, the ASGI response's ``finally``, and the disconnect watch — so
    "the consumer is gone" has exactly one meaning. Both effects are idempotent
    and thread-safe (``Event.set`` and :meth:`StreamCancellation.cancel`), so
    calling it twice, from two threads, in either order, is fine.

    ``stop`` covers a producer BETWEEN events; ``cancel`` closes the
    per-invocation response body, the only thing that can unblock a producer
    already parked inside its read. No chat-client object is touched.
    """
    if worker is None:
        return
    worker.stop.set()
    worker.cancel.cancel()


def _end_turn(worker: '_StreamWorker | None', owner: '_StreamOwner | None') -> None:
    """Full teardown of one /api/ask turn — idempotent, any thread, any path."""
    _cancel_worker(worker)
    if owner is not None:
        get_admission().release(owner.token)


class _StreamOwner:
    """Thread-safe owner of one turn's token and eventually-published worker."""

    __slots__ = ("worker", "token", "_lock", "_cancelled")

    def __init__(self, token) -> None:
        import threading as _threading
        self.worker: '_StreamWorker | None' = None
        self.token = token
        self._lock = _threading.Lock()
        self._cancelled = False

    def attach(self, worker: _StreamWorker) -> None:
        """Publish *worker*, cancelling it immediately if teardown won the race."""
        with self._lock:
            if not self._cancelled:
                self.worker = worker
                return
        _cancel_worker(worker)

    def cancel(self) -> None:
        """Consumer is gone: atomically mark cancelled and release this owner."""
        with self._lock:
            self._cancelled = True
            worker = self.worker
        _end_turn(worker, self)


async def _cancel_off_loop(owner: '_StreamOwner') -> None:
    """Run ``owner.cancel()`` on its own thread and wait for it to finish.

    The cancel closes the turn's runtime response body. Waking the read parked
    in that body (``chat_client._shutdown_reads``) keeps the close quick, but
    if that ever fails the close waits for the runtime's next bytes again; run
    on the event loop, that wait stops every other request. A plain thread is
    used, not an executor, so a cancelled await can never leave the cancel
    queued and unrun: once this coroutine starts, the turn is torn down.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    finished = loop.create_future()

    def _settle(error: BaseException | None) -> None:
        if finished.done():
            return
        if error is None:
            finished.set_result(None)
        else:
            finished.set_exception(error)

    def _run() -> None:
        error: BaseException | None = None
        try:
            owner.cancel()
        except BaseException as exc:  # handed back to the awaiting teardown
            error = exc
        try:
            loop.call_soon_threadsafe(_settle, error)
        except RuntimeError:  # the loop has closed; nothing is waiting any more
            pass

    threading.Thread(target=_run, name="ask-turn-cancel", daemon=True).start()
    await asyncio.shield(finished)


#: Sentinels returned by :func:`_next_stream_event` — distinct from ``None``,
#: which stays the producer's end-of-stream sentinel.
_KEEPALIVE = object()
_STOPPED = object()

#: Public-output silence interval. The wire record is a payload-free SSE
#: comment emitted only by :func:`_drain_event_queue`.
_ASK_STREAM_KEEPALIVE_SECONDS = 15.0

#: Poll slice for the stop-aware queue wait. Small enough that an ASGI
#: disconnect is noticed promptly (the drain must return before the response can
#: finish tearing down), large enough to stay negligible next to the abandoned
#: full-cost turns this whole mechanism exists to end.
_STREAM_STOP_TICK = 0.25


def _next_stream_event(worker: '_StreamWorker', timeout: float | None = None):
    """Next queued event, idle ``_KEEPALIVE``, or cancelled ``_STOPPED``.

    The 250 ms stop poll is independent of the 15 s public-output cadence. A
    final non-blocking queue check at the deadline makes an already-queued real
    event (including the producer's EOF sentinel) win over a due comment.
    """
    import queue as _queue
    import time as _time
    idle_seconds = (_ASK_STREAM_KEEPALIVE_SECONDS
                    if timeout is None else timeout)
    deadline = _time.monotonic() + idle_seconds
    while True:
        if worker.stop.is_set():
            return _STOPPED
        try:
            return worker.queue.get_nowait()
        except _queue.Empty:
            pass
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            try:
                return worker.queue.get_nowait()
            except _queue.Empty:
                return _KEEPALIVE
        try:
            return worker.queue.get(timeout=min(_STREAM_STOP_TICK, remaining))
        except _queue.Empty:
            continue


def _flush_public_answer_events(filter_) -> list:
    from .events import MessageEvent
    pending = filter_.flush()
    return [MessageEvent(pending)] if pending else []


def _public_answer_events(event, filter_) -> list:
    """Return public events while retaining internal counters as metadata."""
    from .events import DoneEvent, MessageEvent
    from .public_answer import redact_internal_telemetry
    if isinstance(event, MessageEvent):
        public = filter_.feed(event.content)
        return [MessageEvent(public)] if public else []
    output = _flush_public_answer_events(filter_)
    if isinstance(event, DoneEvent):
        public, counts = redact_internal_telemetry(event.text)
        filter_.candidate_pair_values.extend(counts)
        output.append(DoneEvent(public))
    else:
        output.append(event)
    return output


def _record_candidate_pair_telemetry(stats: dict, filter_) -> None:
    if filter_.candidate_pair_values:
        stats["additional_candidate_pairs_observed"] = sum(
            filter_.candidate_pair_values)


def _drain_event_queue(worker, stats: dict, start: float, owner=None):
    """Consumer side of the /api/ask SSE stream: yield queued events as SSE
    frames (15 s comments inside the 120 s CloudFront/ALB idle budget),
    accumulating token/tool counts into *stats*.

    The ``finally`` is the local half of turn ownership (L4-1 / P0-A): it ends
    the turn on EVERY exit path — normal completion, an error frame, and the
    ``GeneratorExit`` an explicit close raises. It is NOT sufficient on its own:
    a real ``http.disconnect`` never closes this generator at all, which is why
    :class:`_OwnedStreamingResponse` runs the same teardown from the ASGI layer
    and why the wait above is stop-aware.
    """
    import time as _time
    import logging as _log_mod
    from .public_answer import InternalTelemetryFilter
    _sse_log = _log_mod.getLogger("sse_stream")
    _last_yield_time = _time.monotonic()
    _yield_count = 0
    filter_ = InternalTelemetryFilter()
    try:
        while True:
            event = _next_stream_event(worker)
            if event is _STOPPED or event is None:
                for public_event in _flush_public_answer_events(filter_):
                    yield from _accumulate_and_frame(public_event, stats)
                _record_candidate_pair_telemetry(stats, filter_)
                break
            if event is _KEEPALIVE:
                now = _time.monotonic()
                _sse_log.info(
                    f"SSE_KEEPALIVE gap={now - _last_yield_time:.1f}s "
                    f"elapsed={now - start:.1f}s yields={_yield_count}")
                _last_yield_time = now
                # Exact payload-free comment; no semantic event/stat mutation.
                yield ': keepalive\n\n'
                continue
            for public_event in _public_answer_events(event, filter_):
                yield from _accumulate_and_frame(public_event, stats)
            _record_candidate_pair_telemetry(stats, filter_)
            _yield_count += 1
            now = _time.monotonic()
            gap = now - _last_yield_time
            if gap > 10 or _yield_count % 100 == 0:
                _sse_log.info(f"SSE_YIELD #{_yield_count} gap={gap:.1f}s elapsed={now-start:.1f}s")
            _last_yield_time = now
    finally:
        _end_turn(worker, owner)


def _accumulate_and_frame(event, stats: dict):
    """Fold one event into *stats* and yield its SSE frame (extracted so
    ``_drain_event_queue`` stays inside the 50-line gate)."""
    _fold_usage(event, stats)
    _fold_evidence(event, stats)
    yield event.to_sse_format()


class _TurnEvidence:
    """What the browser was sent that evidences and identifies one turn: its
    provenance frame, folded the way the chat's reducer folds it
    (``turn_evidence_store.fold``), the id of each call the stream
    announced, which with the question identifies the turn
    (``turn_evidence_store.turn_identity``), and the chart tags its answer
    text carried, harvested as the chat harvests them
    (``turn_evidence_store.DeliveredCharts``: each tool call ends a run). The
    gate's structured-answer call is left out of the ids: the reload never
    shows it."""

    __slots__ = ("frame", "calls", "charts")

    def __init__(self) -> None:
        from .turn_evidence_store import DeliveredCharts
        self.frame = None
        self.calls: list[str] = []
        self.charts = DeliveredCharts()

    def observe(self, event) -> None:
        from .events import MessageEvent, MetadataEvent, ToolUseEvent
        if isinstance(event, MessageEvent):
            self.charts.feed(event.content)
        elif isinstance(event, ToolUseEvent):
            self.charts.boundary()
            from .memory_turns import STRUCTURED_ANSWER_TOOL
            if event.tool_use_id and event.tool_name != STRUCTURED_ANSWER_TOOL:
                self.calls.append(event.tool_use_id)
        elif isinstance(event, MetadataEvent) and isinstance(event.data, dict) \
                and "provenance" in event.data:
            from .turn_evidence_store import fold
            self.frame = fold(self.frame, event.data["provenance"])


class _AskStats(dict):
    """The web stream's usage counts, a plain ``dict`` to every reader, with
    the turn's :class:`_TurnEvidence` as an attribute: the usage record keeps
    the counts only."""

    __slots__ = ("evidence",)

    def __init__(self, **counts) -> None:
        super().__init__(**counts)
        self.evidence = _TurnEvidence()


def _fold_evidence(event, stats: dict) -> None:
    """Note an event the browser is sent in the turn's evidence, when *stats*
    carries one (the web stream's :class:`_AskStats`)."""
    evidence = getattr(stats, "evidence", None)
    if evidence is not None:
        evidence.observe(event)


def _keep_turn_evidence(user_id, session_id, question: str, write_memory: bool,
                        evidence: _TurnEvidence) -> None:
    """Keep the turn's folded provenance frame and the chart tags its stream
    sent for the Recent sessions reload (``turn_evidence_store``) under the
    turn's identity: the question read
    from the prompt sent to the runtime the way the reload reads it from the
    stored, redacted copy (``turn_evidence_store.turn_question``) and the
    call ids the stream showed. Records whether this stream ran the
    response-integrity gate. Only for a turn memory stores, so one that can
    be reopened, and one with a frame or a call: a turn whose calls sent no
    chart keeps an empty chart record, so its reload knows the live page
    showed none. Best-effort: never raises."""
    if not (write_memory and (evidence.frame or evidence.calls)):
        return
    try:
        from .config import mode_a_integrity_enforced
        from .turn_evidence_store import keep, turn_identity
        turn = turn_identity(question, evidence.calls)
        keep(user_id, session_id, evidence.frame, turn=turn,
             gated=mode_a_integrity_enforced(), charts=evidence.charts)
    except Exception:  # nosec B110 - display metadata; the answer was already sent
        logger.debug("turn evidence not kept", exc_info=True)


def _fold_usage(event, stats: dict) -> None:
    """Count one stream event into the usage *stats* ``_store_ask_usage``
    records: streamed answer words and the tools called. Shared by the web
    stream and the async (Quick) job, so both count the same way."""
    from .events import MessageEvent, ToolUseEvent
    if isinstance(event, MessageEvent):
        stats["token_count"] += len(event.content.split())
    elif isinstance(event, ToolUseEvent):
        stats["tools_used"].append(event.tool_name)


def _usage_counted(events, stats: dict):
    """Pass *events* through unchanged, folding each into *stats*."""
    for event in events:
        _fold_usage(event, stats)
        yield event


def _ask_tool_usage(stats: dict) -> dict:
    usage = {"tools": stats["tools_used"]}
    if "additional_candidate_pairs_observed" in stats:
        usage["additional_candidate_pairs_observed"] = stats[
            "additional_candidate_pairs_observed"]
    return usage


def _store_ask_usage(user, user_id, session_id, stats: dict, start: float):
    """Best-effort usage record after the stream completes. Pure code motion
    out of ``event_stream`` (50-line gate); failures stay non-fatal."""
    import time as _time
    elapsed_ms = int((_time.monotonic() - start) * 1000)
    try:
        from .usage import _service as _usage_service, UsageRecord
        from datetime import datetime, timezone
        _usage_service.store(UsageRecord(
            user_id=user_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            session_id=session_id,
            model_id=os.environ.get("BEDROCK_MODEL", "unknown"),
            input_tokens=0, output_tokens=stats["token_count"],
            total_tokens=stats["token_count"],
            latency_ms=elapsed_ms,
            tool_usage=_ask_tool_usage(stats),
            user_email=getattr(user, "email", None) if user else None,
        ))
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass  # non-fatal


async def _aclose_body_iterator(iterator) -> None:
    """Close an async or sync iterator after its turn has been cancelled."""
    import inspect

    closer = getattr(iterator, "aclose", None) or getattr(iterator, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        logger.warning("ASK_STREAM_BODY_CLOSE_FAILED type=%s", type(exc).__name__,
                       exc_info=True)


class _OwnedStreamingResponse(StreamingResponse):
    """A ``StreamingResponse`` that OWNS its turn's teardown.

    Starlette 0.51.0 handles ``http.disconnect`` by cancelling
    ``stream_response`` inside its own task group and returning; it never
    ``aclose()``es ``body_iterator``, and ``iterate_in_threadpool`` has no
    ``finally`` closing the sync iterator it wraps. So on a REAL disconnect the
    sync body generator's cleanup — the only thing that stops the producer thread
    and closes the armed per-invocation response body — never ran; it was left to
    the garbage collector. Every ``gen.close()`` proof of that contract was a
    test-only entry point.

    Two hooks make the teardown the response's own responsibility:

    * ``receive`` is watched, so ``owner.cancel()`` fires the instant the
      disconnect message arrives — before Starlette starts unwinding, which is
      what keeps the unwind fast (the drain's wait is stop-aware).
    * ``__call__`` has a ``finally`` that ends the turn and closes the iterator,
      covering the paths with no disconnect message at all: ASGI spec >= 2.4
      (where a disconnect surfaces as ``OSError`` from ``send``), an exception
      from the app, and ordinary completion.
    """

    def __init__(self, content, owner: _StreamOwner, correlation_id: str = "",
                 **kwargs) -> None:
        self._source_iterator = content
        super().__init__(content, **kwargs)
        self._owner = owner
        self._correlation_id = correlation_id

    async def __call__(self, scope, receive, send) -> None:
        from starlette.requests import ClientDisconnect
        disconnect = _DisconnectLog(self._correlation_id)

        async def _watched_receive():
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnect.record()
                await _cancel_off_loop(self._owner)
            return message

        try:
            await super().__call__(scope, _watched_receive, send)
        except ClientDisconnect:
            disconnect.record()   # ASGI >= 2.4: the disconnect surfaces on send
            raise
        finally:
            await _cancel_off_loop(self._owner)
            await _aclose_body_iterator(self.body_iterator)


def _client_disconnect_line(elapsed_s: float, correlation_id: str) -> str:
    """The ``ASK_CLIENT_DISCONNECT`` line: elapsed time and the turn's support
    reference (the chat session id the browser shows), reduced to safe
    characters so a crafted id cannot forge a second log line."""
    import re
    reference = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(correlation_id or "-"))[:96]
    return f"ASK_CLIENT_DISCONNECT elapsed_s={elapsed_s:.1f} reference={reference}"


class _DisconnectLog:
    """Logs ONE ``ASK_CLIENT_DISCONNECT`` line per /api/ask response, the first
    time the browser's disconnect is seen, so the app log shows when a reader
    went away (compare with ``QUERY_SUCCESS``/``QUERY_FAILURE`` of the turn)."""
    __slots__ = ("_started", "_reference", "_done")

    def __init__(self, correlation_id: str) -> None:
        import time as _time
        self._started = _time.monotonic()
        self._reference = correlation_id
        self._done = False

    def record(self) -> None:
        import time as _time
        if self._done:
            return
        self._done = True
        logger.info(_client_disconnect_line(_time.monotonic() - self._started,
                                            self._reference))


def _briefing_skip_response() -> StreamingResponse:
    """The pinned '[Briefing skipped' body (no worker, no admission held)."""
    from .events import DoneEvent

    def _skip_stream():
        yield DoneEvent(text="[Briefing skipped — query in progress]").to_sse_format()

    return StreamingResponse(_skip_stream(), media_type='text/event-stream')


def _admitted_ask_response(body: AskRequest, request: Request, question: str,
                            is_briefing: bool, owner: _StreamOwner):
    """Build one already-admitted response; *owner* spans its full lifetime."""
    client = get_chat_client()
    session_id = getattr(body, 'session_id', None) or str(uuid.uuid4())
    user, user_id, write_memory, user_access_token = _ask_stream_identity(
        body, request, is_briefing)

    def event_stream():
        import time as _time
        start = _time.monotonic()
        stats = _AskStats(token_count=0, tools_used=[])
        worker = _start_stream_worker(client, question, session_id, user_id,
                                      write_memory, user_access_token,
                                      extra_payload=_ask_retry_payload(body))
        owner.attach(worker)
        yield from _drain_event_queue(worker, stats, start, owner)
        _store_ask_usage(user, user_id, session_id, stats, start)
        _keep_turn_evidence(user_id, session_id, question, write_memory, stats.evidence)

    return _OwnedStreamingResponse(
        event_stream(), owner, correlation_id=session_id,
        media_type='text/event-stream', background=_auto_accept_background_task(),
    )


def _ask_retry_payload(body) -> "dict | None":
    """The RESERVED runtime payload key for "Ask again" on the last turn
    (``history_turns.RETRY_PAYLOAD_KEY``), only when the body's ``retry`` is
    exactly true; ``None`` for every ordinary send, so its payload is
    unchanged. The runtime pops the key before anything reaches the model."""
    if getattr(body, "retry", None) is not True:
        return None
    from .history_turns import RETRY_PAYLOAD_KEY
    return {RETRY_PAYLOAD_KEY: True}


async def ask(body: AskRequest, request: Request):
    question = _prepare_question(body)
    if not question:
        return {'error': 'No question provided'}

    is_briefing = 'briefing' in (getattr(body, 'session_id', '') or '').lower()
    admission = get_admission()
    token = (admission.acquire_if_idle("briefing") if is_briefing
             else admission.acquire("query"))
    if token is None:
        logger.info("Skipping briefing — query already in progress")
        return _briefing_skip_response()

    owner = _StreamOwner(token)
    try:
        return _admitted_ask_response(body, request, question, is_briefing, owner)
    except BaseException:
        owner.cancel()
        raise


async def _fixed_public_sse(chunks, owner=None, underlying=None):
    """Fix public error framing and own teardown of every wrapped iterator.

    Every ``event: error`` frame is REBUILT: the public TEXT is always the
    fixed per-category message from ``events.PUBLIC_ERROR_MESSAGES`` (raw
    upstream error text can quote row values — claim-check policy), while the
    closed-taxonomy ``category`` and the app-minted ``correlation_id`` from
    the original frame are PRESERVED so the client can render a descriptive
    message plus a quotable reference. Frames without a classification
    degrade to the generic 'unknown' text — never to upstream text.
    """
    from .events import DoneEvent, ErrorEvent, parse_public_error_frame, public_error_message
    error_seen = done_seen = False
    try:
        async for chunk in chunks:
            is_bytes = isinstance(chunk, bytes)
            text = chunk.decode("utf-8", "replace") if is_bytes else str(chunk)
            if text.startswith("event: error"):
                error_seen = True
                category, correlation_id = parse_public_error_frame(text)
                text = ErrorEvent(
                    message=public_error_message(category), details="",
                    category=category, correlation_id=correlation_id,
                ).to_sse_format()
            elif text.startswith("event: done"):
                done_seen = True
            yield text.encode() if is_bytes else text
        if error_seen and not done_seen:
            yield DoneEvent().to_sse_format()
    finally:
        if owner is not None:
            await _cancel_off_loop(owner)
        await _aclose_body_iterator(chunks)
        if underlying is not None and underlying is not chunks:
            await _aclose_body_iterator(underlying)


_ask_base = ask


@app.post('/api/ask')
async def _ask_integrity(body: AskRequest, request: Request):
    """Run the legacy stream behind the fixed public runtime-error contract."""
    response = await _ask_base(body, request)
    if isinstance(response, StreamingResponse):
        chunks = response.body_iterator
        response.body_iterator = _fixed_public_sse(
            chunks,
            owner=getattr(response, "_owner", None),
            underlying=getattr(response, "_source_iterator", None),
        )
    return response


ask = _ask_integrity


# ── Upload endpoints ──────────────────────────────────────────────────────────

_uploaded_specs: dict[str, dict] = {}  # in-memory store: ref_id -> spec content

@app.post("/api/upload/spec")
async def upload_spec(body: dict):
    """Store an uploaded OpenAPI spec. Returns a reference ID for discovery."""
    import hashlib, json as _json
    filename = body.get("filename", "spec.json")
    content = body.get("content")
    raw_yaml = body.get("raw_yaml")
    if raw_yaml and not content:
        try:
            import yaml
            content = yaml.safe_load(raw_yaml)
        except Exception:
            content = {"raw": raw_yaml}
    if not content:
        return {"error": "No spec content provided"}
    ref_id = hashlib.sha256(_json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()[:12]
    _uploaded_specs[ref_id] = {"filename": filename, "content": content}
    return {"status": "stored", "ref_id": ref_id, "filename": filename}

# ── Router includes ───────────────────────────────────────────────────────────

from .resolve import router as resolve_router
from .dashboard import router as dashboard_router
from .dashboard_drill import router as dashboard_drill_router
from .graph_analytics import router as analytics_router
from .coverage import router as coverage_router

app.include_router(resolve_router)
app.include_router(dashboard_router)
app.include_router(dashboard_drill_router)
app.include_router(analytics_router)
app.include_router(coverage_router)
app.include_router(auth_router)
app.include_router(memory_router)
app.include_router(feedback_router)

from .metric_resolver import router as metrics_router
app.include_router(metrics_router)

from .admin_routes import router as admin_router
app.include_router(admin_router)
from .agents_routes import router as agents_router
app.include_router(agents_router)
from .agents_trace_routes import router as agents_trace_router
app.include_router(agents_trace_router)
app.include_router(prompts_router)
from .demo_routes import router as demo_router
app.include_router(demo_router)
from .workshop_story_routes import router as workshop_story_router
app.include_router(workshop_story_router)
from .guidance_retire_routes import router as guidance_retire_router
app.include_router(guidance_retire_router)
from .vocabulary_routes import router as vocabulary_router
app.include_router(vocabulary_router)
from .calendar_policy_routes import router as calendar_policy_router
app.include_router(calendar_policy_router)
from .learning_routes import router as learning_router
app.include_router(learning_router)
from .graph_snapshot_routes import router as graph_snapshot_router
app.include_router(graph_snapshot_router)
from .notification_routes import router as notification_router
app.include_router(notification_router)
from .eval_routes import router as eval_router
app.include_router(eval_router)

# ── Self-Learning Proposed Mappings ───────────────────────────────────────────

def _schema_existence_verdict(m: dict, schemas: dict, known: list):
    """Real schema-existence verdict for ONE schema-checked proposal row.

    Returns ``(recommendation, confidence, schema_verdict, schema_checked)``
    when DEFINITIVE, or ``None`` when indeterminate (schema unavailable /
    ambiguous multi-dot) so the caller keeps the pure ``score_proposal``
    text. Moved verbatim from the route body (2.2 decompose — behavior
    unchanged). The structured verdict + PROOF (which system.field was
    checked) keep the displayed reasoning trustworthy — it can never
    contradict the UI's block/allow gate."""
    from .schema_check import column_in_schema, system_in_catalog
    sys_id = m.get('from_system', '')
    field = m.get('from_field', '')
    if not sys_id or not field:
        return None
    exists = column_in_schema(schemas.get(sys_id), field)
    if exists is True:
        return (f"✅ Column verified in {sys_id}.{field}", 'high',
                'verified', f"{sys_id}.{field}")
    if exists is False:
        return (f"❌ Column not found in {sys_id} schema — do not accept",
                'low', 'not_found', f"{sys_id}.{field}")
    if system_in_catalog(sys_id, known) is False:
        # DEFINITIVE unknown system (live incident: from_system was a
        # plant/site name, not an onboarded system id, so the schema was
        # unavailable → the verdict stayed None and the UI's ⛔ gate never
        # fired; accepting then produced a zero-row graph write). A system id
        # absent from a NON-EMPTY catalog can never apply — block it with the
        # same 'not_found' verdict the UI already gates on.
        return (f"⛔ System '{sys_id}' is not an onboarded system — "
                f"edit the proposal to a valid system id before accepting",
                'low', 'not_found', f"{sys_id}.{field}")
    return None


def _apply_scoring_enrichment(m: dict, schemas: dict, known: list) -> None:
    """Attach ``ai_recommendation``/``ai_confidence`` (+ schema-verdict proof)
    to ONE row, in place.

    Scores via the SHARED scorer (single source of truth —
    ``proposal_scoring.score_proposal`` — reused by auto-accept + bulk), then
    replaces the static "verify column exists" recommendation with a real
    schema-existence verdict when one is definitive (see
    ``_schema_existence_verdict``); indeterminate leaves the pure
    text/confidence intact."""
    from .proposal_scoring import score_proposal
    from .schema_check import SCHEMA_CHECKED_TYPES
    scored = score_proposal(m)
    recommendation = scored['ai_recommendation']
    confidence = scored['ai_confidence']
    if m.get('proposed_type', '') in SCHEMA_CHECKED_TYPES:
        verdict = _schema_existence_verdict(m, schemas, known)
        if verdict is not None:
            recommendation, confidence, m['schema_verdict'], m['schema_checked'] = verdict
    m['ai_recommendation'] = recommendation
    m['ai_confidence'] = confidence


# Concept fan-out for the consequence blast radius. The
# cypher + fold live in the SHARED home ``learning_health.concept_system_counts``
# so the review list and the steward digest rank with the SAME inputs.
def _concept_system_counts(concept_ids) -> dict:
    """Count catalog systems sharing each concept — ONE graph read for ALL ids.

    Thin delegate to ``learning_health.concept_system_counts`` (the shared
    single source), passing this module's ``neptune_query`` so the
    existing test seam (monkeypatching ``server.neptune_query``) still
    drives the read. Batched, best-effort ``{}`` — never a 500."""
    from .learning_health import concept_system_counts
    return concept_system_counts(concept_ids, query=neptune_query)


def _ledger_evidence_by_node() -> dict:
    """Evidence-class flags per ledger artifact id — ONE bounded read (X-2).

    ``artifact_usage.read_artifact_usage`` is the bounded scan (page cap 20)
    behind its 60 s in-process cache; ledger artifact ids for rules ARE the
    ``neptune_node_id`` stamped on accepted proposals, so the route can map
    ledger evidence onto proposal rows with a dict lookup — no N+1, one read
    per cache window. Best-effort ``{}``: the SlCards evidence badge
    degrades to absent, never a 500."""
    try:
        from . import artifact_usage
        usage = artifact_usage.read_artifact_usage(30) or {}
        return {aid: list(entry.get("evidence_classes") or [])
                for aid, entry in usage.items()
                if entry.get("evidence_classes")}
    except Exception as e:  # noqa: BLE001 - enrichment is best-effort
        logger.warning(f"ledger evidence read failed (non-critical): {e}")
        return {}


def _attach_family_enrichment(m: dict, pending_families: dict) -> None:
    """Attach the ADDITIVE review-FAMILY fields to ONE row, in place (D4).

    ``family_key`` (computed by ``proposal_scoring.family_key`` — the ONE
    definition of the grain) and, when the row's family has pending members,
    ``family_count``. Absent-otherwise, like the sibling advisory fields: a
    row whose family has no pending peers carries no count. The key is a
    selection AID only — the batch route never selects rows by it (the request
    carries the explicit id list), so a stale key can never widen a mutation.
    """
    from .proposal_scoring import family_key
    key = family_key(m)
    if not key:  # grain does not discriminate for this row — no family offer
        return
    m['family_key'] = key
    count = pending_families.get(key, 0)
    if count:
        m['family_count'] = count


def _attach_review_enrichment(m: dict, concept_counts: dict,
                              supersede_by_subject: dict) -> None:
    """Attach the ADDITIVE steward-review fields to ONE row, in place (2.2).

    ``provenance_tier`` (R3 semantics — delegates to the orchestrator's
    single source), ``consequence`` (evidence strength × blast radius — the
    pending list's sort key), and ``touches`` (systems / concept / conflict /
    supersede_count). Additive ONLY — no existing response field is renamed,
    removed, or altered (the sl-cards contract)."""
    from .proposal_scoring import build_touches, consequence, provenance_tier
    concept = m.get('concept', '') or ''
    m['provenance_tier'] = provenance_tier(m)
    m['consequence'] = consequence(m, concept_counts.get(concept, 0))
    m['touches'] = build_touches(
        m, supersede_by_subject.get(m.get('subject_key', '') or '', 0))


def _gather_enrichment_inputs(mappings: list):
    """Batch EVERY blocking read the review route needs, in ONE ``gather``.

    Returns ``(concept_descriptions, schemas_by_system, known_systems,
    concept_system_counts, ledger_evidence_by_node)``:

    - Concept descriptions first-class (ADR 0021): every distinct concept id
      in AT MOST ONE graph round-trip (no N+1 over a full scan); core ids
      need no graph. Offloaded so the async route never blocks the loop.
    - Per-system schemas loaded ONCE per DISTINCT system for the live
      schema-existence verdict (unavailable → None → static text kept).
      ``SCHEMA_CHECKED_TYPES`` is the SINGLE SOURCE shared with the upstream
      ingestion gate (``self_learning`` step 3c) so "reject at creation" and
      "block at display" can never drift.
    - Catalog systems for the DEFINITIVE unknown-system verdict
      (``known_systems`` never raises; [] when there is no basis to judge).
    - Concept fan-out for the consequence blast radius (2.2) — ONE
      additional batched read, folded into this SAME gather (no N+1).
    - Ledger evidence flags (X-2) — ONE bounded artifact-usage read (60 s
      cached) so accepted rows can carry their ``evidence_classes`` badge.
    """
    from .self_learning import known_systems
    from .concept_layer import concept_descriptions
    from .schema_check import load_system_schema, SCHEMA_CHECKED_TYPES
    from concurrent.futures import ThreadPoolExecutor

    concept_ids = {m.get('concept', '') for m in mappings if m.get('concept')}
    schema_sys_ids = {
        m.get('from_system', '') for m in mappings
        if m.get('proposed_type', '') in SCHEMA_CHECKED_TYPES and m.get('from_field')
    }
    schema_sys_ids.discard('')

    # Runs inside the route's offload worker; the five independent reads
    # still overlap (same parallelism as the former asyncio.gather).
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = (
            pool.submit(concept_descriptions, concept_ids),
            pool.submit(lambda: {sid: load_system_schema(sid) for sid in schema_sys_ids}),
            pool.submit(known_systems),
            pool.submit(_concept_system_counts, concept_ids),
            pool.submit(_ledger_evidence_by_node),
        )
        return tuple(f.result() for f in futures)


def _enrich_review_rows(mappings: list, gathered: tuple) -> None:
    """Attach every ADDITIVE review field to each row of an already-fetched
    scan, IN PLACE (extracted verbatim from the route body — same order, same
    absent-otherwise semantics, same best-effort degeneracy guard).

    ``gathered`` is the ``_gather_enrichment_inputs`` tuple, so this stays
    pure-CPU: no row triggers a read of its own. The two whole-list
    derivations (supersede counts, ADR-0063 D4 review families) are computed
    ONCE over the same scan.
    """
    from .concept_layer import concept_layer_of
    from .proposal_scoring import degeneracy_flags, family_counts, supersede_counts
    batch, schemas, known, concept_counts, ledger_evidence = gathered
    supersede_by_subject = supersede_counts(mappings)
    pending_families = family_counts(mappings)
    for m in mappings:
        # A minted proposal carries its authored ``to_description``; otherwise fall back
        # to the batched concept definition (core text or the graph's minted description).
        m['concept_description'] = m.get('to_description') or batch.get(m.get('concept', ''), '')
        m['concept_layer'] = concept_layer_of(m.get('concept', ''))
        m["action_eligibility"] = proposal_batch.action_eligibility(m)
        _apply_scoring_enrichment(m, schemas, known)
        _attach_review_enrichment(m, concept_counts, supersede_by_subject)
        _attach_family_enrichment(m, pending_families)
        # X-2 (additive): accepted rows carry a neptune_node_id — when the
        # usage ledger has evidence for it, attach the class flags the
        # SlCards badge reads; absent otherwise (badge degrades silently).
        evidence_classes = ledger_evidence.get(m.get('neptune_node_id') or '')
        if evidence_classes:
            m['evidence_classes'] = evidence_classes
        # ADVISORY degeneracy verdict (additive, same absent-otherwise
        # idiom as evidence_classes): server-computed so the badge is
        # never re-derived client-side. Applies to EVERY status — legacy
        # pending echo rows get flagged; accepted rows keep their history
        # honest. Pure computation, zero extra I/O, best-effort per row.
        # NO route acts on these keys and auto-accept never reads them.
        try:
            flags = degeneracy_flags(m)
        except Exception as e:  # raising classifier never breaks the route
            logger.warning("degeneracy_flags failed: %s", type(e).__name__)
            flags = []
        if flags:
            m['degeneracy'] = flags
            m['likely_low_value'] = True


_PROPOSAL_STATUS_VALUES = ("pending", "accepted", "rejected", "superseded", "expired")
_PROPOSAL_STATUS_PATTERN = "^(" + "|".join(_PROPOSAL_STATUS_VALUES) + ")$"


_PROPOSED_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


def _query_flag(value) -> bool:
    """Strict boolean query parse — ONLY ``true``/``1``/``yes`` (case-
    insensitive, the ``config._env_bool`` vocabulary) are true. Anything else,
    including a direct caller's ``Query`` default object, is false."""
    return isinstance(value, str) and value.strip().lower() in ("true", "1", "yes")


def get_proposed_mappings(status: "str | None" = None,
                          proposed_type: "str | None" = None,
                          include_expired: "str | None" = None) -> dict:
    """Sync body of the review route: full scan, optional filters, enrichment.

    ``proposed_type`` matches the frontend's own classification — a row with
    no ``proposed_type`` counts as ``value_mapping`` (``isValueMappingProposal``).
    Without ``status``, ``expired`` rows are EXCLUDED unless
    ``include_expired`` is strictly true: they are not actionable and were the
    largest class (673 of 1,850 rows live on 2026-09-28). ``?status=expired``
    still returns them. ``status_counts`` (additive) tallies every lifecycle
    status over the type-filtered scan so a status-scoped client can label
    its other tabs without fetching them.
    Hoisted out of the async handler (offload invariant): the scan
    is a paginated DynamoDB read (7.3 MB / 1,232 pending rows live on
    2026-09-26) and ran on the event loop, stalling every other request.
    """
    from .self_learning import get_all_mappings
    wanted = status if isinstance(status, str) and status else None
    wanted_type = proposed_type if isinstance(proposed_type, str) and proposed_type else None
    try:
        typed = _filter_proposals(get_all_mappings(), None, wanted_type)
        counts = _proposal_status_counts(typed)
        mappings = _filter_proposals(typed, wanted, None)
        if not wanted and not _query_flag(include_expired):
            mappings = [m for m in mappings if str(m.get("status") or "") != "expired"]
        _enrich_review_rows(mappings, _gather_enrichment_inputs(mappings))
    except Exception as e:
        return {**public_error(e, "proposals.list"), "mappings": [], "count": 0}
    payload = {"mappings": mappings, "count": len(mappings), "status_counts": counts}
    if wanted:
        payload["status"] = wanted
    if wanted_type:
        payload["proposed_type"] = wanted_type
    return payload


def _filter_proposals(mappings: list, status: "str | None",
                      proposed_type: "str | None") -> list:
    """Optional lifecycle-status and proposal-type filters (pure)."""
    mappings = [m for m in mappings if isinstance(m, dict)]
    if status:
        mappings = [m for m in mappings if str(m.get("status") or "") == status]
    if proposed_type:
        mappings = [m for m in mappings
                    if str(m.get("proposed_type") or "value_mapping") == proposed_type]
    return mappings


def _proposal_status_counts(mappings: list) -> dict:
    """Rows per exact lifecycle status (pure; the SAME exact match as
    ``?status=``). Rows the review panel never renders — ``detected_gap``
    placeholders (the frontend's ``validMappings``) and status-free staging
    rows — are not counted, so a tab label matches the cards it opens."""
    counts = {s: 0 for s in _PROPOSAL_STATUS_VALUES}
    for m in mappings:
        if "detected_gap" in (m.get("from_field"), m.get("to_field")):
            continue
        s = str(m.get("status") or "")
        if s in counts:
            counts[s] += 1
    return counts


@app.get("/api/proposed-mappings")
async def get_proposed_mappings_route(
    status: str | None = Query(None, pattern=_PROPOSAL_STATUS_PATTERN),
    proposed_type: str | None = Query(None, pattern=_PROPOSED_TYPE_PATTERN),
    include_expired: str | None = Query(None, max_length=8),
):
    """List proposed mappings with AI recommendations, optionally filtered by
    lifecycle ``status`` (ADR-0065: ``?status=pending`` returns ONLY the pending
    queue, never accepted/rejected rows) and/or ``proposed_type``. Without ``status``
    every row EXCEPT ``expired`` is served (an unfiltered multi-MB
    payload stalls the review panel); ``?include_expired=true`` (strict:
    ``true``/``1``/``yes``) restores every row, ``?status=expired`` returns
    only those. ``status_counts`` labels the tabs a scoped client never fetched.

    Serves every proposal type, including ``insight`` — a narrative FINDING
    (no ``table.column`` fields). Insights are scored by the shared scorer as
    permanently-low-confidence informational items; they are NOT in
    ``SCHEMA_CHECKED_TYPES`` so they never get a schema verdict, and their
    accept path never writes a graph rule (see ``self_learning``).
    """
    # Direct (non-HTTP) callers receive the Query default object, not a value;
    # the sync body normalizes anything that is not a non-empty str to None.
    return await offload(get_proposed_mappings, status, proposed_type, include_expired)


def _request_actor(request: Request) -> str:
    """The acting user's identity for provenance stamps: session email, else
    username (the demo 'ADT' user has no email — username is the identity).
    Empty string in auth-disabled/local mode (provenance stays unattributed)."""
    try:
        from .auth import get_current_user
        user = get_current_user(request)
        return str(getattr(user, "email", "") or getattr(user, "username", "") or "")
    except Exception:
        return ""


@app.get("/api/proposed-mappings/{mapping_id}/guidance-preview",
         dependencies=[Depends(_check_steward)])
async def preview_proposed_guidance(mapping_id: str, request: Request):
    """Side-effect-free opaque preview for one pending guidance proposal."""
    from .self_learning import preview_guidance_mapping
    from fastapi.responses import JSONResponse
    result = preview_guidance_mapping(mapping_id)
    code = int(result.get("http_status", 200))
    payload = {key: value for key, value in result.items()
               if key != "http_status"}
    return JSONResponse(status_code=code, content=payload) if code != 200 else payload


@app.post("/api/proposed-mappings/{mapping_id}/accept",
          dependencies=[Depends(_check_steward)])
async def accept_proposed_mapping(mapping_id: str, request: Request,
                                  body: dict | None = None):
    """Accept a proposal; guidance requires strict graph+delivery proof.
    The body (DynamoDB + graph + ADR-0077 source re-read) runs offloaded."""
    return await offload(_accept_proposed_mapping_sync, mapping_id,
                         _request_actor(request), body)


def _accept_proposed_mapping_sync(mapping_id: str, actor: str, body: dict | None):
    """Sync accept body: ValueError → 400; error dicts honour ``http_status``.
    This is the route a person accepts ONE proposal through, so it passes the
    one-row ``confirmation_mode`` (``self_learning.accept_mapping``)."""
    from .identifier_translation import SINGLE_ROW_CONFIRMATION
    from .self_learning import accept_mapping
    from fastapi.responses import JSONResponse
    try:
        kwargs = {"actor": actor, "confirmation_mode": SINGLE_ROW_CONFIRMATION}
        if body:
            kwargs["guidance_confirmation"] = body
        result = accept_mapping(mapping_id, **kwargs)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if "error" in result:
        code = int(result.get("http_status") or
                   (404 if "not found" in result["error"] else 500))
        payload = {key: value for key, value in result.items()
                   if key != "http_status"}
        return JSONResponse(status_code=code, content=payload)
    return result


@app.post("/api/proposed-mappings/{mapping_id}/reject",
          dependencies=[Depends(_check_steward)])
async def reject_proposed_mapping(mapping_id: str, request: Request):
    """Reject a proposed mapping."""
    from .self_learning import reject_mapping
    result = reject_mapping(mapping_id, actor=_request_actor(request))
    if "error" in result:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404 if "not found" in result["error"] else 500, content=result)
    return result


@app.post("/api/proposed-mappings/{mapping_id}/requeue",
          dependencies=[Depends(_check_steward)])
async def requeue_proposed_mapping(mapping_id: str, request: Request):
    """Move an ACCEPTED proposal back to pending for steward re-review.

    Honestly undoes the routing effect the accept created: the graph artifact
    stamped on the row (``neptune_node_id``) is removed or soft-deactivated by
    ``self_learning._undo_graph_artifact`` before the row flips to pending.
    404 unknown id; 400 non-accepted rows and non-invertible proposal types.
    """
    from .self_learning import requeue_mapping
    from fastapi.responses import JSONResponse
    try:
        result = requeue_mapping(mapping_id, actor=_request_actor(request))
    except ValueError as e:
        # Unsupported proposed_type — client-actionable, not a server fault.
        return JSONResponse(status_code=400, content={"error": str(e)})
    if "error" in result:
        code = 404 if "not found" in result["error"] else 400
        return JSONResponse(status_code=code, content=result)
    return result


@app.post("/api/proposed-mappings/{mapping_id}/reopen",
          dependencies=[Depends(_check_steward)])
async def reopen_proposed_mapping(mapping_id: str, request: Request):
    """Re-open a REJECTED proposal — flips it back to pending (ADR-0053 E6).

    The steward's tombstone escape hatch: a pending row no longer matches the
    tombstone predicate (rejected-status rows only), so the shape becomes
    reviewable and proposable again. Returns the updated row.
    404 unknown id; 400 non-rejected rows.
    """
    from .self_learning import reopen_mapping
    from fastapi.responses import JSONResponse
    result = reopen_mapping(mapping_id, actor=_request_actor(request))
    if "error" in result:
        code = 404 if "not found" in result["error"] else 400
        return JSONResponse(status_code=code, content=result)
    return result


@app.post("/api/proposed-mappings/{mapping_id}/edit",
          dependencies=[Depends(_check_steward)])
async def edit_proposed_mapping(mapping_id: str, body: EditMappingRequest, request: Request):
    """Edit an existing pending proposal before accepting.
    
    Validates edited fields against the onboarded schema. Returns the updated
    proposal with re-computed schema_verdict so the UI can update in place.
    The body is :class:`EditMappingRequest`: a key outside its fields is
    refused (422), so an edit never changes a row's provenance or status.
    """
    from .self_learning import edit_mapping
    from .schema_check import column_in_schema, load_system_schema, SCHEMA_CHECKED_TYPES
    
    # Filter out None values - only apply provided fields
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"error": "no fields to update"}
    
    result = edit_mapping(mapping_id, updates)
    if "error" in result:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400 if "not found" in result.get("error", "").lower() or "not onboarded" in result.get("error", "").lower() or "does not exist" in result.get("error", "").lower() or "cannot be edited" in result.get("error", "").lower() else 500, content=result)
    
    # Re-run schema verification on the updated mapping
    m = result
    if m.get('proposed_type', '') in SCHEMA_CHECKED_TYPES:
        sys_id = m.get('from_system', '')
        field = m.get('from_field', '')
        if sys_id and field:
            try:
                schema = load_system_schema(sys_id)
            except Exception:
                schema = None
            exists = column_in_schema(schema, field)
            if exists is True:
                m['schema_verdict'] = 'verified'
                m['schema_checked'] = f"{sys_id}.{field}"
            elif exists is False:
                m['schema_verdict'] = 'not_found'
                m['schema_checked'] = f"{sys_id}.{field}"
    
    return {"mapping": m, "status": "updated"}


@app.post("/api/proposed-mappings/reconcile-blocked",
          dependencies=[Depends(_check_steward)])
async def reconcile_blocked_proposals_route(request: Request):
    """Purge reviewable proposals that reference a column absent from the onboarded
    schema (the idempotent cleanup counterpart to the upstream ingestion gate).

    Deletes only ``pending``/``superseded`` items whose referenced ``table.column``
    DEFINITIVELY does not exist; never touches ``accepted`` items (live graph rules).
    Idempotent and safe to re-run. Offloaded off the event loop because it makes
    blocking DynamoDB + graph-schema calls."""
    from .self_learning import reconcile_blocked_proposals
    import asyncio
    result = await asyncio.to_thread(reconcile_blocked_proposals)
    if isinstance(result, dict) and "error" in result:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content=result)
    return result


# ``POST /api/proposed-mappings/user-define`` lets an END USER assert a
# relationship the AI never proposed. A STEWARD's assertion (``steward`` True:
# the ``request.state.is_admin`` verdict ``_check_steward`` reads) goes through
# ``self_learning.record_user_correction`` exactly as before: the SAME accepted
# lifecycle a metric correction uses (source='user' → the mapping enters
# ``accepted``, supersedes any active agent proposal on the same subject, and is
# applied to Neptune immediately). ANYONE ELSE's assertion goes through
# ``self_learning.record_user_proposal``: the same keys filed ``pending`` for
# steward review, with nothing superseded, nothing applied and no graph read
# (ADR-0050: proposing is open, accepting is a steward's call). NO
# collision/supersede logic is duplicated here.
#
# Required: from_system, from_field, from_value, to_system, to_field, to_value,
# concept. Optional: description, proposed_type (default 'value_mapping'). A
# missing/blank required field ⇒ 400 (no write attempted). Beyond presence, the
# handler also validates SEMANTICS before any write (``_user_define_error``): an
# unknown (non-onboarded) from_system/to_system ⇒ 400 (the in-memory catalog); a
# proposed_type outside {value_mapping, identity_rule, join_path} ⇒ 400. For a
# steward, a from_field/to_field the system's schema KNOWS is absent ⇒ 400 too
# (an unavailable schema skips that specific check rather than hard-blocking).
# That check reads the schema from the graph, so for anyone else it runs at the
# steward's accept instead (``self_learning._user_proposal_refusal``). Either
# door is called only after validation passes. A repoint ``ValueError``
# (corrected column not in the discovered schema) returns 400 with the
# actionable message; any OTHER write failure is fail-loud: the door raises and
# the exception propagates (no silent success), mirroring the accept route.
_USER_DEFINE_PROPOSED_TYPES = {"value_mapping", "identity_rule", "join_path"}


def _user_define_error(body: UserDefinedMappingRequest, steward: bool,
                       proposed_type: str) -> str:
    """Why ``user_define_mapping`` answers 400 before any write, or ``""``:
    a missing required field, else every semantic error, joined."""
    from . import schema_check
    required = {
        "from_system": body.from_system, "from_field": body.from_field,
        "from_value": body.from_value, "to_system": body.to_system,
        "to_field": body.to_field, "to_value": body.to_value,
        "concept": body.concept,
    }
    missing = [name for name, value in required.items() if not (value or "").strip()]
    if missing:
        return f"missing required field(s): {', '.join(missing)}"

    # Semantic validation (beyond presence): a steward's correction supersedes any
    # agent proposal and writes to Neptune immediately, so a bad relationship is
    # costly, and anyone else's lands in the steward queue.
    # Hard-block an unknown system (in-memory catalog, no graph read). For a
    # steward, hard-block a field the schema KNOWS is absent (column_exists →
    # False); SKIP the field check when the schema is unavailable (None) so an
    # un-onboarded-schema edge case doesn't hard-block. column_exists reads the
    # graph, so a non-steward's field check is the steward's accept. Reuses the
    # SAME helpers as the get_proposed_mappings enrichment (DRY).
    errors: list[str] = []
    from_onboarded = schema_check.is_onboarded_system(body.from_system)
    to_onboarded = schema_check.is_onboarded_system(body.to_system)
    if not from_onboarded:
        errors.append(f"from_system '{body.from_system}' is not an onboarded system")
    if not to_onboarded:
        errors.append(f"to_system '{body.to_system}' is not an onboarded system")
    if proposed_type not in _USER_DEFINE_PROPOSED_TYPES:
        errors.append(
            f"proposed_type '{proposed_type}' is not one of "
            f"{sorted(_USER_DEFINE_PROPOSED_TYPES)}"
        )
    # Field-existence (steward only): only meaningful for a known system; None
    # (schema unavailable) skips that specific check, False (known-missing)
    # hard-blocks.
    for side, onboarded in (("from", from_onboarded), ("to", to_onboarded)):
        system, field = getattr(body, f"{side}_system"), getattr(body, f"{side}_field")
        if steward and onboarded and schema_check.column_exists(system, field) is False:
            errors.append(f"{side}_field '{field}' does not exist in {system} schema")
    return "; ".join(errors)


def user_define_mapping(body: UserDefinedMappingRequest, steward: bool = False,
                        actor: str = ""):
    """Sync body of ``POST /api/proposed-mappings/user-define`` (offloaded;
    see the comment above). A steward's correction is one row a person
    wrote, so it carries the one-row stamp an accept of a single proposal
    does (``actor`` and ``confirmation_mode``, ``self_learning.accept_mapping``)."""
    from .identifier_translation import SINGLE_ROW_CONFIRMATION
    from .self_learning import record_user_correction, record_user_proposal
    from fastapi.responses import JSONResponse

    proposed_type = body.proposed_type or "value_mapping"
    error = _user_define_error(body, steward, proposed_type)
    if error:
        return JSONResponse(status_code=400, content={"error": error})

    door = record_user_correction if steward else record_user_proposal
    stamp = ({"actor": actor, "confirmation_mode": SINGLE_ROW_CONFIRMATION}
             if steward else {})
    try:
        result = door(
            from_system=body.from_system, from_field=body.from_field,
            from_value=body.from_value, to_system=body.to_system,
            to_field=body.to_field, to_value=body.to_value, concept=body.concept,
            proposed_type=proposed_type,
            description=body.description, **stamp,
        )
    except ValueError as e:
        # The concept_mapping repoint's fail-loud pre-check ("corrected column …
        # not in the discovered schema … correction not applied") is a
        # CLIENT-actionable rejection — 400 with the message, mirroring the
        # accept route. Every other exception still propagates (fail-loud).
        return JSONResponse(status_code=400, content={"error": str(e)})
    return result


@app.post("/api/proposed-mappings/user-define")
async def user_define_mapping_route(body: UserDefinedMappingRequest, request: Request):
    """Open to any signed-in user; only a steward's assertion is accepted on
    entry, anyone else's files a pending proposal (see ``user_define_mapping``).
    The sync body (schema reads, DynamoDB, graph) runs offloaded."""
    return await offload(user_define_mapping, body, is_steward(request),
                         _request_actor(request))


@app.post("/api/proposed-mappings/reset",
          dependencies=[Depends(_check_steward)])
async def reset_proposed_mappings(request: Request):
    """Reset all accepted mappings — removes from Neptune, resets to pending."""
    from .self_learning import reset_all_accepted
    result = reset_all_accepted()
    if "error" in result:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content=result)
    return result


@app.post("/api/proposed-mappings/clear-rejected",
          dependencies=[Depends(_check_steward)])
async def clear_rejected_mappings(request: Request):
    from .self_learning import clear_rejected
    result = clear_rejected()
    return result


@app.post("/api/proposed-mappings/clear-all",
          dependencies=[Depends(_check_steward)])
async def clear_all_learnings_endpoint(request: Request):
    from .self_learning import clear_all_learnings
    result = clear_all_learnings()
    return result


@app.post("/api/proposed-mappings/accept-all",
          dependencies=[Depends(_check_steward)])
async def accept_all_pending_endpoint(request: Request):
    from .self_learning import accept_all_pending
    result = accept_all_pending()
    return result


@app.post("/api/proposed-mappings/accept-high-confidence",
          dependencies=[Depends(_check_steward)])
async def accept_high_confidence_mappings():
    """Bulk-accept every PENDING high-confidence value_mapping/identity_rule.

    Codifies the admin's "accept the safe ones" action. Thin wrapper over the
    SHARED sweep (``self_learning.sweep_auto_accept``) — the single source of
    truth reused by this route, the periodic scheduler safety-net, and the
    post-``/api/ask`` background hook (no duplicated accept logic)."""
    from .self_learning import sweep_auto_accept
    return sweep_auto_accept()


from . import proposal_batch

_BATCH_ADJUDICATION_MAX = proposal_batch.BATCH_MAX
_BATCH_BODY_KEYS = proposal_batch.BODY_KEYS
_BATCH_ACTIONS = proposal_batch.ACTIONS


def _batch_request(body: dict) -> tuple:
    """Translate fixed batch request errors to bounded HTTP details."""
    try:
        return proposal_batch.parse_request(body)
    except proposal_batch.BatchRequestError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code}) from None


@app.post("/api/proposed-mappings/adjudicate-batch",
          dependencies=[Depends(_check_steward)])
async def adjudicate_mappings_batch(body: dict, request: Request):
    """Adjudicate an EXPLICIT LIST of proposals in one steward action (D4).

    The queue's false rows arrive in families (same method, same field pair,
    same claim shape), and reviewing them one at a time does not scale. This
    route is the TRANSPORT for a family action: the client sends the ids it is
    showing the steward, each id runs the single-row accept/reject path, and
    the response echoes PER-ID outcomes — never an all-or-nothing verdict, so
    one bad id neither blocks nor hides the rest. Grouping itself is computed
    on read (``family_key`` on the GET list); this route never selects by it.
    """
    action, ids = _batch_request(body)
    return proposal_batch.adjudicate(action, ids, actor=_request_actor(request))


def reject_degenerate_mappings():
    """Bulk-reject malformed pending rows and degenerate field annotations.

    Every non-insight proposal requires non-empty field endpoints; insights are
    narrative findings and are explicitly exempt. Field annotations with both
    endpoints still pass through the shared semantic degeneracy predicate.
    """
    from .self_learning import get_pending_mappings, reject_mapping
    from .proposal_scoring import is_degenerate_field_annotation
    rejected = 0
    for m in get_pending_mappings():
        proposed_type = m.get("proposed_type", "")
        if proposed_type == "insight":
            continue
        empty_endpoint = (
            not str(m.get("from_field") or "").strip()
            or not str(m.get("to_field") or "").strip()
        )
        degenerate_annotation = False
        if proposed_type == "field_annotation" and not empty_endpoint:
            annotation = m.get("query_context", "").replace("Auto-analysis: ", "")
            degenerate_annotation = is_degenerate_field_annotation(
                m.get("from_system", ""), m.get("from_field", ""),
                m.get("to_field", ""), m.get("from_value", ""),
                m.get("to_value", ""), annotation,
            )
        if not empty_endpoint and not degenerate_annotation:
            continue
        result = reject_mapping(m["mapping_id"])
        if not (isinstance(result, dict) and "error" in result):
            rejected += 1
    return {"rejected": rejected}


@app.post("/api/proposed-mappings/reject-degenerate",
          dependencies=[Depends(_check_steward)])
async def reject_degenerate_mappings_route():
    """Route for :func:`reject_degenerate_mappings` — the paginated pending
    scan and per-row DynamoDB writes run off the event loop."""
    return await offload(reject_degenerate_mappings)


@app.post("/api/proposed-mappings/promote-values",
          dependencies=[Depends(_check_steward)])
async def promote_value_mappings():
    """Promote accumulated per-value observations into single rule proposals
    (ADR-0043). Thin wrapper over ``value_promotion.sweep_value_promotion`` —
    groups PENDING ``value_mapping`` rows by field pair, and where ≥3 distinct
    from_values confirm one rule emits ONE ``join_path``/``crosswalk`` proposal
    (entering ``pending`` — the sweep never accepts; auto-accept stays OFF by
    construction) and marks the subsumed per-value rows ``superseded`` with
    ``subsumed_by``. Operator-triggered (no scheduler hook). Offloaded off the
    event loop because the sweep makes blocking DynamoDB calls."""
    from .value_promotion import sweep_value_promotion
    import asyncio
    return await asyncio.to_thread(sweep_value_promotion)


# ── Learned query-guidance transparency + pruning ────────────────────────────
# Management surface for the ACCUMULATED query_guidance (mirrors the proposed-
# mapping admin routes above): list every system's learned notes, prune one note,
# or clear a system's learnings (keeping the onboarded base guidance). The GET is
# read-only; prune/clear are scoped, fail-loud single-node writes.

@app.get("/api/query-guidance/reconciliation",
         dependencies=[Depends(_check_steward)])
async def guidance_reconciliation_endpoint(request: Request):
    """Read-only accepted→graph→delivery counts and opaque mismatches."""
    from .guidance_reconciliation import collect_reconciliation
    import asyncio
    return await asyncio.to_thread(collect_reconciliation)


@app.get("/api/query-guidance")
async def list_query_guidance():
    """List every system's accumulated query_guidance, parsed into base + LEARNED notes."""
    from .self_learning import get_all_query_guidance
    systems = get_all_query_guidance()
    return {"systems": systems, "count": len(systems)}


@app.post("/api/query-guidance/{system_id}/prune",
          dependencies=[Depends(_check_steward)])
async def prune_query_guidance(system_id: str, body: dict, request: Request):
    """Prune ONE learned note (exact ``note`` text in the body) from a system."""
    from .self_learning import prune_query_guidance_note
    result = prune_query_guidance_note(system_id, (body or {}).get("note", ""))
    if "error" in result:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404 if "not found" in result["error"] else 400,
                            content=result)
    return result


@app.post("/api/query-guidance/{system_id}/clear",
          dependencies=[Depends(_check_steward)])
async def clear_query_guidance_endpoint(system_id: str, request: Request):
    """Clear ALL learned notes for a system (keeps the onboarded base guidance)."""
    from .self_learning import clear_query_guidance
    result = clear_query_guidance(system_id)
    if "error" in result:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404 if "not found" in result["error"] else 500,
                            content=result)
    return result


def migrate_system_properties():
    """One-time migration: write query_guidance to Neptune System nodes from catalog."""
    from .catalog import catalog
    from .onboard import neptune_write
    updated = 0
    errors = []
    for sys_entry in catalog.get_all():
        system_id = sys_entry["id"]
        qg = sys_entry.get("query_guidance", "")
        caps = sys_entry.get("capabilities", "")
        tp = sys_entry.get("tool_prefix", system_id)
        if not qg:
            continue
        try:
            neptune_write(
                "MATCH (s:System {id: $sid}) SET s.query_guidance = $qg, s.capabilities = $caps, s.tool_prefix = $tp",
                {"sid": system_id, "qg": qg, "caps": caps, "tp": tp}
            )
            updated += 1
        except Exception as e:
            errors.append(f"{system_id}: "
                          f"{public_error(e, 'graph.migrate_system', default='graph_query_failed')['error']}")
    return {"updated": updated, "errors": errors}


@app.post("/api/admin/migrate-system-properties")
async def migrate_system_properties_route():
    """One-time migration: write query_guidance to Neptune System nodes from catalog. Sync body: ``migrate_system_properties``."""
    return await offload(migrate_system_properties)


# Inject agent_manager into dashboard module
from . import dashboard as _dashboard_mod
_dashboard_mod.set_agent_manager(_agent_manager)

# Inject agent_manager into agents_routes so the Agents page picks the local vs
# cloud fleet provider from the manager's mode.
from . import agents_routes as _agents_mod
_agents_mod.set_agent_manager(_agent_manager)

# Onboard router — imported once onboard.py exists
try:
    from .onboard import router as onboard_router
    from . import onboard as _onboard_mod
    app.include_router(onboard_router)
    _onboard_mod.set_agent_manager(_agent_manager)
except ImportError:
    pass


# ── Static files ──────────────────────────────────────────────────────────────

_base_dir = Path(__file__).resolve().parent.parent
_i3x_dir = _base_dir / "static" / "i3x-explorer"
_dist_dir = _base_dir / "frontend" / "dist"
# The UI is served ONLY from the built Vite SPA (frontend/dist); static/ hosts
# only the deploy.sh-built i3x-explorer bundle. The production container
# always builds dist
# (Dockerfile stage 1), so a missing dist only happens in local dev.

# Wrap StaticFiles with no-cache headers so browsers always revalidate after deploys
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Apply no-store to all static assets (JS, CSS, HTML)
        # no-store prevents the browser from using any cached version,
        # ensuring updated JS/CSS is always fetched after a deployment.
        path = request.url.path
        if path == "/" or path.endswith((".js", ".css", ".html")):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            # Remove ETag and Last-Modified so browser can't send conditional requests
            if "etag" in response.headers:
                del response.headers["etag"]
            if "last-modified" in response.headers:
                del response.headers["last-modified"]
        return response

app.add_middleware(NoCacheStaticMiddleware)
# Outermost: compress large JSON (the review queue was 7.3 MB uncompressed).
# Starlette's GZipMiddleware NEVER compresses text/event-stream (its
# DEFAULT_EXCLUDED_CONTENT_TYPES), so the /api/ask SSE stream — incl. the
# payload-free ': keepalive' comments — stays byte-for-byte unbuffered.
# Pinned by tests/unit/test_server_gzip.py.
app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE)
# deploy.sh builds the i3x-explorer bundle into static/i3x-explorer/ —
# mounted before '/' so it always wins.
if (_i3x_dir / "index.html").is_file():
    app.mount("/i3x-explorer", StaticFiles(directory=str(_i3x_dir), html=True), name="i3x")
if _dist_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist_dir), html=True), name="static")
else:
    logger.warning(
        "frontend/dist not found — UI not served; build with: cd frontend && npm run build"
    )
