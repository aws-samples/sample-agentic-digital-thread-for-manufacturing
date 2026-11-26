# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Agent manager — lifecycle management for system agent MCP servers.

Local mode:  Serves system agents from this process on unique loopback ports
             (8100-8199), one thread each (``local_agent_server``).
Cloud mode:  Creates AgentCore Runtimes + registers as Gateway targets.
"""

import json
import logging
import os
import re
import socket
import time
import uuid
import contextlib
import copy
import contextvars
import functools
from dataclasses import dataclass, field
from typing import Optional

import boto3

from .digital_thread_server import (
    TARGET_NAME as DIGITAL_THREAD_SYSTEM_ID,
    CONNECTION_TYPE as KNOWLEDGE_GRAPH_CONNECTION_TYPE,
)
from .connection_types import ATHENA, QUERY_BRIDGE_CONNECTION_TYPE
from .query_bridge_server import TARGET_NAME as QUERY_BRIDGE_SYSTEM_ID

# Built-in targets whose "connection" is IAM/VPC-reachable infrastructure, not
# a credentialed data source: no agent-config secret is written for them and
# the business-context delivery wrappers pass them through untouched.
_SECRETLESS_CONNECTION_TYPES = (
    KNOWLEDGE_GRAPH_CONNECTION_TYPE,
    QUERY_BRIDGE_CONNECTION_TYPE,
)
from .runtime_provision_helpers import (
    RUNTIME_CONFIG_LOCK,
    GatewayTargetLookupError,
    GatewayTargetRecreateError,
    GatewayTargetUpdateError,
    bring_gateway_target_in_line,
    container_uri as _shared_container_uri,
    gateway_target_drift,
    gateway_target_endpoint,
    network_config as _shared_network_config,
    runtime_connection_env as _shared_runtime_connection_env,
    runtime_update_kwargs as _shared_runtime_update_kwargs,
    upsert_connection_secret as _shared_upsert_connection_secret,
    agent_config_secret_value as _agent_config_secret_value,
    list_all_agent_runtimes,
    list_all_gateway_targets,
    list_all_oauth2_credential_providers,
)
from .errors import SystemReaderLoginError
from .lease_guard import check_write_guard, release_on_loss
from .local_agent_server import LocalAgentServer


def system_connection_config(schema: str, connection_type: str,
                             connection_config: dict | None,
                             conn_params: dict | None) -> dict:
    """The connection config a system runtime's agent-config secret carries.

    PostgreSQL merges the connect params in, then hands the result to the
    demo seam: a connection to a database this deployment provisioned, made
    with that database's owner login, gets the schema's read-only login
    instead, so the SQL the agent's model writes never runs as the owner
    (``demo.db_routing.system_agent_pg_config``; a missing reader raises
    :class:`~agents.errors.SystemReaderLoginError`, never falls back). A
    customer's own login, and every other connection type, is unchanged."""
    effective = dict(connection_config or {})
    if connection_type != "PostgreSQL":
        return effective
    if conn_params:
        effective.update(conn_params)
    from .demo.db_routing import system_agent_pg_config  # lazy: one-way demo seam
    return system_agent_pg_config(schema, effective)


def _same_pg_database(old: dict, new: dict) -> bool:
    """True when two PostgreSQL connection configs name the same host and
    database (case-insensitive host; ``dbname`` or ``database``)."""
    def target(cfg: dict) -> tuple:
        host = str(cfg.get("host") or "").strip().lower()
        return host, str(cfg.get("dbname") or cfg.get("database") or "").strip()
    old_target = target(old)
    return bool(old_target[0] and old_target[1]) and old_target == target(new)


#: An IAM role ARN as an Athena connection config names it (any partition, a
#: path allowed). A role ARN is compared whole: two roles of one account can
#: be granted different tables.
_IAM_ROLE_ARN_RE = re.compile(r"arn:[a-z0-9-]+:iam::\d{12}:role/[\w+=,.@/-]+")


def _athena_data_target(cfg: dict) -> tuple | None:
    """The tables an Athena connection config reads: ``(region, catalog,
    database, role ARN)``, with the connector's own defaults (the default
    catalog, the deployment's region) and the whole ``role_arn`` (``""`` for
    ambient credentials). ``None`` when it names no database or a
    ``role_arn`` that is not an IAM role ARN. The workgroup, output location
    and external id do not change which tables these are."""
    database = str(cfg.get("database") or cfg.get("schema") or "").strip().lower()
    if not database:
        return None
    region = str(cfg.get("region") or os.environ.get("AWS_REGION", "us-east-1")).strip().lower()
    catalog = str(cfg.get("catalog") or "AwsDataCatalog").strip().lower()
    role = str(cfg.get("role_arn") or "").strip()
    if role and not _IAM_ROLE_ARN_RE.fullmatch(role):
        return None
    return region, catalog, database, role


def _same_data_target(connection_type: str, old: dict, new: dict) -> bool:
    """True when two connection configs of ``connection_type`` read the same
    tables, so what onboarding generated for those tables still describes
    them. PostgreSQL: the same host and database (:func:`_same_pg_database`).
    Athena: the same region, catalog, database and role ARN
    (:func:`_athena_data_target`), and neither config naming another
    connection type. Every other type is never the same target."""
    if connection_type == "PostgreSQL":
        return _same_pg_database(old, new)
    if connection_type != ATHENA:
        return False
    if any(str(cfg.get("connection_type") or ATHENA).strip() != ATHENA for cfg in (old, new)):
        return False
    old_target = _athena_data_target(old)
    return old_target is not None and old_target == _athena_data_target(new)


def _split_top_level_tables(tables_desc: str) -> list[str]:
    """Split a tables_desc render into its top-level ``name(...)`` entries,
    honouring nesting so a comma inside a column list or a nested ``(partition
    key)`` does not split an entry. Entries are separated by commas at paren
    depth 0."""
    entries, depth, start = [], 0, 0
    for i, ch in enumerate(tables_desc):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            entries.append(tables_desc[start:i])
            start = i + 1
    entries.append(tables_desc[start:])
    return [e.strip() for e in entries if e.strip()]


def _tables_desc_is_complete(tables_desc: str) -> bool:
    """True only when EVERY top-level table entry in ``tables_desc`` carries a
    complete column block -- not an empty string, not a bare name, and not a
    mixed render where one table has columns and another is names-only.

    Per-table (e.g. Athena) physical discovery is best-effort and can emit a
    non-empty SUBSET such as ``ns.good(id: varchar), ns.missing`` after one table
    fails. That is NOT complete: writing it would overwrite a complete stored
    schema and roll onto missing table/rule facts (review live-config-refresh-r2
    F2). A complete entry is ``name(col: type, ...)`` -- a parenthesised column
    block containing a ``:``. The caller still gates on its engine's own
    expected/complete-table signal; this is the structural all-or-nothing check
    on the value about to be written."""
    if not tables_desc or not tables_desc.strip():
        return False
    entries = _split_top_level_tables(tables_desc)
    if not entries:
        return False
    for entry in entries:
        open_paren = entry.find("(")
        if open_paren == -1 or ":" not in entry[open_paren:]:
            return False  # a names-only or column-less entry: not complete
    return True


#: The transient version stage an agent-config compare-and-set write lands on
#: before it is promoted to AWSCURRENT. A failed promotion leaves no version
#: under this stage.
_ADT_PENDING_STAGE = "ADT_PENDING"

#: A marker key written into the agent-config secret when a config delivery was
#: promoted but its runtime roll failed AND the baseline could not be restored.
#: While present, the next provision rolls even if the generated bytes match; it
#: is cleared from the merged payload once a merge completes and READY confirms.
_DELIVERY_PENDING_KEY = "adt_delivery_pending"


def _cas_put_payload(sm, secret_name, read_version_id, payload, system_id) -> str:
    """Write ``payload`` to a unique ``ADT_PENDING`` version, then promote it to
    ``AWSCURRENT`` with ``UpdateSecretVersionStage(... RemoveFromVersionId=
    read_version_id)``. Secrets Manager rejects the move if AWSCURRENT no longer
    belongs to ``read_version_id``, so a concurrent writer is never clobbered; on
    rejection the pending stage is cleared and this raises. Returns the promoted
    version id. The single CAS protocol shared by the primary merge and the
    delivery-pending marker."""
    pending = _ADT_PENDING_STAGE
    put = sm.put_secret_value(
        SecretId=secret_name,
        ClientRequestToken=str(uuid.uuid4()),
        SecretString=_agent_config_secret_value(payload, label=system_id),
        VersionStages=[pending])
    attempt_version = (put or {}).get("VersionId")
    if not read_version_id or not attempt_version:
        _remove_version_stage(sm, secret_name, pending, attempt_version)
        raise RuntimeError(
            f"agent-config secret for {system_id}: no version baseline for a "
            "compare-and-set write; refusing to clobber it")
    try:
        sm.update_secret_version_stage(
            SecretId=secret_name, VersionStage="AWSCURRENT",
            MoveToVersionId=attempt_version, RemoveFromVersionId=read_version_id)
    except Exception as exc:  # noqa: BLE001 - AWSCURRENT moved under us (or the API refused)
        _remove_version_stage(sm, secret_name, pending, attempt_version)
        raise RuntimeError(
            f"agent-config secret for {system_id} changed under a compare-and-set "
            "write (AWSCURRENT moved); retry with fresh state") from exc
    return attempt_version


def _mark_delivery_pending(sm, secret_name, system_id) -> None:
    """Write the delivery-pending marker through the SAME pending-version CAS as
    the primary write (review live-config-refresh-r4 F1), using the version read
    here as ``RemoveFromVersionId``. If a newer writer has already won, the CAS
    loses and this leaves that version untouched -- it never forces an older
    payload plus marker onto state this attempt no longer owns. Never raises."""
    try:
        read = sm.get_secret_value(SecretId=secret_name)
        cur = json.loads(read.get("SecretString") or "{}")
        if not isinstance(cur, dict):
            return
        cur[_DELIVERY_PENDING_KEY] = True
        _cas_put_payload(sm, secret_name, read.get("VersionId"), cur, system_id)
    except Exception as exc:  # noqa: BLE001 - marker is best-effort; a lost CAS is expected
        logger.warning("agent-config delivery-pending marker for %s not written (%s); "
                       "a newer writer owns the secret", system_id, type(exc).__name__)


def _restore_baseline(sm, secret_name, system_id, promoted, baseline_version,
                      baseline_payload) -> str:
    """Make AWSCURRENT serve the baseline again, only while it still belongs to
    ``promoted`` (this attempt's version). First move the stage back to the
    baseline version; if that move is refused, re-read AWSCURRENT: when it is no
    longer ``promoted`` a newer writer won (``"superseded"``), otherwise re-write
    the baseline payload through the pending-version CAS. ``"restored"`` or
    ``"superseded"`` or ``"failed"``. Never raises."""
    try:
        if baseline_version:
            sm.update_secret_version_stage(
                SecretId=secret_name, VersionStage="AWSCURRENT",
                MoveToVersionId=baseline_version, RemoveFromVersionId=promoted)
            return "restored"
    except Exception as exc:  # noqa: BLE001 - fall through to the payload re-write
        logger.debug("%s: AWSCURRENT stage restore failed (%s); re-writing the baseline",
                     system_id, type(exc).__name__)
    try:
        read = sm.get_secret_value(SecretId=secret_name)
        current_version = read.get("VersionId")
        if baseline_version and current_version == baseline_version:
            return "restored"   # the stage move landed but its response was lost
        if current_version != promoted:
            return "superseded"
        if not isinstance(baseline_payload, dict):
            return "failed"
        _cas_put_payload(sm, secret_name, promoted, baseline_payload, system_id)
        return "restored"
    except Exception:  # noqa: BLE001 - the baseline could not be put back
        return "failed"


def _remove_version_stage(sm, secret_name, stage, version_id) -> None:
    """Best-effort removal of a transient version stage from a secret version,
    so a failed compare-and-set leaves no dangling pending version. Never
    raises -- the original error is what the caller surfaces."""
    if not version_id:
        return
    try:
        sm.update_secret_version_stage(
            SecretId=secret_name, VersionStage=stage, RemoveFromVersionId=version_id)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.warning("agent-config CAS: could not clear pending stage on %s: %s",
                       secret_name, type(exc).__name__)

logger = logging.getLogger(__name__)


# One COMPLETE gateway-target listing per gateway, shared by every fast-path
# check inside a restore pass (see gateway_target_snapshot). ``None`` outside a
# pass: each check then lists fresh, so onboarding never reads a stale view.
_TARGET_SNAPSHOT: contextvars.ContextVar = contextvars.ContextVar(
    "adt_gateway_target_snapshot", default=None)


@contextlib.contextmanager
def gateway_target_snapshot():
    """Scope ONE complete ``list_all_gateway_targets`` per gateway across a
    startup/repair restore pass.

    ``_cloud_fast_path_ready`` runs once per restored system; listing every
    page for each of N systems is N x pages control-plane calls on the
    startup path whose latency already risks ALB health failures. Inside this
    scope the first check on a gateway takes a full listing and the rest
    reuse it (a listing error is not cached; that check falls through to
    ``_start_cloud`` as before). Only the fast-path READY check reads the
    snapshot — create/reuse/delete paths always list fresh. Context-local, so
    a concurrent request on another thread never sees it."""
    token = _TARGET_SNAPSHOT.set({})
    try:
        yield
    finally:
        _TARGET_SNAPSHOT.reset(token)


def _fast_path_targets(control, gateway_id: str) -> list:
    """Every target on ``gateway_id`` for a fast-path check: the restore
    pass's snapshot when one is active, else a fresh full listing."""
    snapshot = _TARGET_SNAPSHOT.get()
    if snapshot is None:
        return list_all_gateway_targets(control, gateway_id)
    if gateway_id not in snapshot:
        snapshot[gateway_id] = list_all_gateway_targets(control, gateway_id)
    return snapshot[gateway_id]


class QuickGatewayNotConfiguredError(RuntimeError):
    """QUICK_API_ENABLED is on but the task env carries no QUICK_GATEWAY_ID.

    The `query-bridge` target is ONLY ever provisioned on the dedicated
    bridge-only gateway (`adt-quick-gw`, ADR-0071). Falling back to the shared
    gateway would silently recreate the tool fan-out that gateway exists to
    prevent, so a missing id is a deployment mismatch (stack and image from
    different builds) and is refused, not papered over."""


class AgentConfigUnreadable(RuntimeError):
    """``adt/agent-config/<id>`` exists or may exist but could not be read
    (any error but "not found"). Raised by
    :meth:`AgentManager.current_agent_config` so a restore can tell "no
    config" from "could not read it" before it rewrites the config."""


class FastPathUnavailable(RuntimeError):
    """``start_agent(..., fast_path_only=True)`` found no READY target backed
    by a READY runtime, so registering would mean creating or rewriting the
    agent config, which the caller ruled out."""


def _business_context_secret_value(state: dict | None) -> str | None:
    """The ``adt/agent-config/<id>`` secret's ``business_context`` value for a
    graph state (``business_context.read_business_context`` shape): the stored
    text when ENABLED and non-empty, else ``None`` (key absent). The ONE
    include rule (ADR-0054 D4) shared by provision (:meth:`AgentManager._start_cloud`)
    and refresh (:meth:`AgentManager.refresh_business_context`)."""
    text = (state or {}).get("business_context") or ""
    if (state or {}).get("enabled") and text.strip():
        return text
    return None


@dataclass
class AgentInstance:
    system_id: str
    system_name: str
    schema: str
    system_prompt: str
    tables_desc: str
    mcp_url: str
    port: int = 0
    server: Optional[LocalAgentServer] = field(default=None, repr=False)  # local mode
    runtime_arn: str = ""
    gateway_target_id: str = ""
    # Gateway the target was provisioned on ("" = the manager's default,
    # AGENTCORE_GATEWAY_ID). The `query-bridge` target lives on the dedicated
    # Quick gateway (QUICK_GATEWAY_ID) — delete_cloud_agent must address it there.
    gateway_id: str = ""


def _find_runtime_id(control, runtime_name: str) -> str | None:
    """AgentCore runtime id for *runtime_name*, or None (extracted: 50-line gate)."""
    for rt in list_all_agent_runtimes(control):
        if rt.get("agentRuntimeName") == runtime_name:
            return rt.get("agentRuntimeId")
    return None


# ---------------------------------------------------------------------------
# T1.5 delivery rule — which artifact classes require a runtime roll (PINNED).
#
# Only SECRET-DELIVERED payloads roll a runtime: they are read ONCE at
# container start (runtime_app reads the adt/agent-config/<id> secret), so
# delivering a change requires deploying a new runtime version. Artifact
# classes with PER-TURN or PER-SESSION read paths — query guidance (ADR-0035
# versioned store), graph artifacts, domain notes — must NEVER trigger a roll:
# their consumers re-read them without a restart, and rolling for them would
# take the data plane down for nothing (the G2 outage class). This table is
# the ONE home for that judgement — future callers (domain-pack installs,
# T2.2) inherit it instead of re-deciding per call site. A class NOT listed
# in ROLL_REQUIRED_ARTIFACT_CLASSES never rolls (fail-safe direction: a new
# secret-delivered class must be registered HERE deliberately; the visible
# symptom of forgetting is a truthful "skipped" refresh, never an outage).
# ---------------------------------------------------------------------------
ROLL_REQUIRED_ARTIFACT_CLASSES = frozenset({
    "business_context",  # adt/agent-config secret key (ADR-0054 D4)
    "system_prompt",     # adt/agent-config secret key
    "tables_desc",       # adt/agent-config secret key
})

#: Documented for the T2.2 preview diff — per-turn/per-session read paths.
NO_ROLL_ARTIFACT_CLASSES = frozenset({
    "query_guidance",   # ADR-0035 versioned store, read per session
    "graph_artifact",   # Neptune rules/mappings, read per query
    "domain_note",      # graph-read at route-seed time (plan T2.1)
})


def artifact_class_requires_roll(artifact_class: str) -> bool:
    """True iff a change to *artifact_class* must roll system runtimes.

    Membership in :data:`ROLL_REQUIRED_ARTIFACT_CLASSES` is the ONLY thing
    consulted — never call-site judgement. Unknown classes return False
    (no roll: the fail-safe direction, see the table comment above)."""
    return artifact_class in ROLL_REQUIRED_ARTIFACT_CLASSES


#: Subnets AgentCore has refused in THIS process ("unsupported availability
#: zone" CREATE_FAILED). Learned once, applied to every later runtime create:
#: on a fresh workshop account all nine runtimes (orchestrator, bridge, seven
#: systems) used to fail on the same subnet one after another, about a minute
#: of deploy time each (event 24, 2026-10-03). Per process by design: the VPC
#: is fixed for the life of the app task, and a restart re-learns in one try.
_UNSUPPORTED_SUBNETS: set[str] = set()


def unsupported_subnets() -> set[str]:
    """The subnets this process has learned AgentCore cannot launch in."""
    return set(_UNSUPPORTED_SUBNETS)


def _forget_unsupported_subnets() -> None:
    """Test seam: clear the learned set."""
    _UNSUPPORTED_SUBNETS.clear()


def _subnets_minus_unsupported(subnets: list[str]) -> list[str]:
    """Drop remembered-bad subnets, but never below one (let the service
    name the offender again rather than offering nothing)."""
    kept = [s for s in subnets if s not in _UNSUPPORTED_SUBNETS]
    return kept if kept else list(subnets)


#: How long one runtime create may sit in CREATING before the poll gives up.
#: A cold account's first runtimes take about five minutes to reach READY
#: (event 34, 2026-10-18: both built-in runtimes READY at the five-minute
#: mark on two of three accounts); the old 300 s deadline raced that.
RUNTIME_READY_TIMEOUT_S = 600

#: Attempts at creating one runtime. Each CREATE_FAILED that is not the
#: unsupported-AZ class (the service's own "Network config provisioning/
#: deletion failed ... InternalError", seen on one of three fresh accounts on
#: event 34) deletes the failed runtime and tries again after a pause.
RUNTIME_CREATE_ATTEMPTS = 3

#: Pause before each transient-failure retry, by attempt index.
RUNTIME_CREATE_RETRY_DELAYS_S = (20, 60)

#: Runtime states that are never reused: the create or last update failed,
#: so the runtime has no working version behind its name. Reusing one
#: (event 34, Team 1) would sync a gateway target at a dead endpoint.
RUNTIME_FAILED_STATES = ("CREATE_FAILED", "UPDATE_FAILED")


class AgentManager:
    """Manages system agent instances — local MCP servers or AgentCore Runtimes."""

    PORT_MIN = 8100
    PORT_MAX = 8199

    def __init__(
        self,
        mode: str = "local",
        gateway_id: str = "",
        gateway_url: str = "",
        ecr_image_uri: str = "",
        runtime_role_arn: str = "",
    ):
        self.mode = mode  # "local" or "cloud"
        self.gateway_id = gateway_id or os.environ.get("AGENTCORE_GATEWAY_ID", "")
        self.gateway_url = gateway_url or os.environ.get("AGENTCORE_GATEWAY_URL", "")
        self.ecr_image_uri = ecr_image_uri or os.environ.get("AGENTCORE_ECR_IMAGE", "")
        self.runtime_role_arn = runtime_role_arn or os.environ.get("AGENTCORE_RUNTIME_ROLE", "")
        self._agents: dict[str, AgentInstance] = {}
        self._used_ports: set[int] = set()
        # M2M OAuth config for gateway → runtime auth
        self._m2m_pool_id = os.environ.get("AGENTCORE_M2M_POOL_ID", "")
        self._m2m_client_id = os.environ.get("AGENTCORE_M2M_CLIENT_ID", "")
        self._m2m_discovery_url = os.environ.get("AGENTCORE_M2M_DISCOVERY_URL", "")
        self._credential_provider_arn: str | None = None

    # ------------------------------------------------------------------
    # Port allocation
    # ------------------------------------------------------------------

    def _allocate_port(self) -> int:
        for port in range(self.PORT_MIN, self.PORT_MAX + 1):
            if port in self._used_ports:
                continue
            # Verify the port is actually free at the OS level before handing it
            # out. Tracking only our own _used_ports set is not enough: a port in
            # the range can be held by a listener this manager did not start, e.g.
            # another app run on this host. Handing out an occupied port fails
            # the agent's start on its bind. Probe with a real bind and skip if
            # busy.
            if not self._port_is_free(port):
                continue
            self._used_ports.add(port)
            return port
        raise RuntimeError(f"No free ports in range {self.PORT_MIN}-{self.PORT_MAX}")

    @staticmethod
    def _port_is_free(port: int) -> bool:
        """Return True if *port* can be bound on loopback right now (OS-level check)."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def _release_port(self, port: int):
        self._used_ports.discard(port)

    # ------------------------------------------------------------------
    # Runtime identity / health (shared by the fast path and _start_cloud)
    # ------------------------------------------------------------------

    @staticmethod
    def _runtime_name(system_id: str) -> str:
        """AgentCore runtime name for a system — single source of truth.

        Mirrors the convention _start_cloud uses to create/find runtimes:
        ``adt_<system_id>`` with hyphens folded to underscores, capped at 48 chars.
        Keeping this in one place ensures the fast-path health check and the
        create/update path can never look up different runtime names (DRY).
        """
        return f"adt_{system_id}".replace("-", "_")[:48]

    @staticmethod
    def _gateway_target_name(system_id: str) -> str:
        """Gateway target name for a system: the name the target is created
        under (``_upsert_and_sync_gateway_target``)."""
        return system_id.replace("_", "-")[:100]

    def _runtime_is_ready(self, control, system_id: str) -> bool:
        """Return True iff an AgentCore runtime for *system_id* exists AND is READY.

        A READY gateway target is not proof the data plane works: the runtime it
        points at may have been deleted (or never created), leaving the target
        stale-READY and DANGLING. The fast path calls this to confirm the backing
        runtime before short-circuiting. Reuses the same ``list_agent_runtimes``
        + ``adt_<id>`` naming that ``_start_cloud`` uses to create/find runtimes.

        Best-effort and fail-safe: any lookup error returns False so the caller
        falls through to ``_start_cloud`` and (re)creates the runtime rather than
        registering a dangling target.
        """
        runtime_name = self._runtime_name(system_id)
        try:
            for rt in list_all_agent_runtimes(control):
                if rt.get("agentRuntimeName") == runtime_name:
                    return rt.get("status", "") == "READY"
        except Exception:
            return False  # can't confirm — force _start_cloud (safe)
        return False

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start_agent(
        self,
        system_id: str,
        system_name: str,
        schema: str,
        system_prompt: str,
        tables_desc: str = "",
        conn_params: dict | None = None,
        connection_type: str = "PostgreSQL",
        connection_config: dict | None = None,
        gateway_id: str | None = None,
        gateway_url: str | None = None,
        fast_path_only: bool = False,
        refresh_generated_config: bool = True,
    ) -> str:
        """Start a system agent. Returns the MCP URL.

        ``gateway_id`` / ``gateway_url`` override the manager's default gateway
        (AGENTCORE_GATEWAY_ID/URL) for THIS system's target — used by the
        `query-bridge` target, which lives on the dedicated Quick gateway.
        ``None`` = default (every onboarded system).

        ``fast_path_only``: register only by the cloud fast path; anything else
        raises :class:`FastPathUnavailable` instead of creating.

        ``refresh_generated_config`` (default ``True``): a PROVISION rewrites the
        generated config and rolls the reused runtime on the fast path. A server
        RESTORE passes ``False`` -- never write/roll a READY runtime, only
        register it -- yet still falls through to :meth:`_start_cloud` to CREATE
        a missing runtime (separate from ``fast_path_only``; r2 F3)."""
        if system_id in self._agents:
            return self._agents[system_id].mcp_url
        gateway_id = self.gateway_id if gateway_id is None else gateway_id
        gateway_url = self.gateway_url if gateway_url is None else gateway_url

        if self.mode == "cloud":
            if self.takes_fast_path(system_id, gateway_id, gateway_url):
                return self._register_fast_path(
                    system_id, system_name, schema, system_prompt, tables_desc,
                    connection_type, connection_config, gateway_id, gateway_url,
                    fast_path_only, refresh_generated_config)
            if fast_path_only:
                raise FastPathUnavailable(system_id)
            return self._start_cloud(system_id, system_name, schema, system_prompt, tables_desc,
                                     connection_type, connection_config, conn_params,
                                     gateway_id=gateway_id, gateway_url=gateway_url)
        if fast_path_only:
            raise FastPathUnavailable(system_id)
        return self._start_local(system_id, system_name, schema, system_prompt, tables_desc,
                                 conn_params, connection_type, connection_config)

    def _register_fast_path(self, system_id, system_name, schema, system_prompt,
                            tables_desc, connection_type, connection_config,
                            gateway_id, gateway_url, fast_path_only,
                            refresh_generated_config=True) -> str:
        """Register a system by the cloud fast path: the runtime + READY target
        exist from a previous deploy, so skip the control plane's create/update.

        A PROVISION still baked a fresh schema (``tables_desc``) and prompt; the
        runtime reads those from the agent-config secret, not the control plane,
        so write them here even though the runtime is reused. Without this an
        existing S3 Tables / Athena runtime kept an empty ``tables_desc`` after a
        re-provision and the agent lost its baked column list and window rules.

        A warm READY container reads the agent-config secret only once at start,
        so merely rewriting the secret is not enough: when the provision CHANGES
        the generated keys, deploy a new runtime version so the replacement
        container re-reads it, and WAIT for READY before registering (review
        live-tables-desc F1). The roll AND the readiness must BOTH succeed before
        this registers: a failed roll or a timed-out READY wait RAISES, so the
        provision fails retryable and its caller keeps the old agent serving
        (``onboard._commit_replacement``) rather than registering a warm runtime
        still reading the stale secret. A ``fast_path_only`` cold-restart
        registration, and any registration with ``refresh_generated_config``
        False (a server restore), writes nothing and rolls nothing."""
        if refresh_generated_config and not fast_path_only and (tables_desc or system_prompt):
            self._refresh_generated_config_on_fast_path(
                system_id, schema, system_prompt, tables_desc,
                connection_type, connection_config)
        self._agents[system_id] = AgentInstance(
            system_id=system_id, system_name=system_name, schema=schema,
            system_prompt=system_prompt, tables_desc=tables_desc,
            mcp_url=gateway_url, gateway_id=gateway_id,
        )
        logger.info(f"Cloud agent {system_id} registered (fast path, gateway={gateway_url[:40]}...)")
        return gateway_url

    def _refresh_generated_config_on_fast_path(self, system_id, schema, system_prompt,
                                               tables_desc, connection_type,
                                               connection_config) -> None:
        """Merge the freshly baked generated config into the agent-config secret
        and, when it CHANGED, roll the reused runtime so a warm container
        re-reads it. The write, the roll and the READY wait must all succeed;
        this RAISES otherwise so the provision fails retryable and keeps the old
        agent, never registering a warm runtime still on the stale secret.

        A no-op write (nothing changed) needs no roll, so it returns quietly. A
        write FAILURE is distinct from a no-op: the write helper raises it, it is
        not reported as ``False``, so a merge that could not be persisted is
        never mistaken for "already current".

        TRANSACTIONAL DELIVERY (r3 F1): the secret is promoted BEFORE the roll,
        so if the roll or READY wait fails the promoted version is CAS-restored
        to the baseline (while AWSCURRENT is still ours). If that restore itself
        fails, a delivery-pending marker is written so the NEXT provision rolls
        even when the generated bytes already match; it clears only once READY is
        confirmed. Either way this RAISES, so an identical retry never registers
        the stale warm runtime."""
        delivery: dict = {}
        changed = self._write_agent_config_on_fast_path(
            system_id, schema, system_prompt, tables_desc,
            connection_type, connection_config, delivery=delivery)
        if changed is False:
            return  # nothing changed (or secretless): no roll needed
        rolled = self._bump_runtime_version(system_id, os.environ.get("AWS_REGION", "us-east-1"))
        if rolled != "ok":
            # The roll never started, so no container read the promoted config:
            # a baseline restore is enough, no compensating roll needed.
            self._undo_config_delivery(delivery, system_id, f"runtime roll {rolled!r}")
            raise RuntimeError(
                f"fast-path provision for {system_id}: runtime roll {rolled!r}; "
                "refusing to register a warm runtime still reading the old config")
        if not self._await_runtime_ready(
                system_id, time.monotonic() + self.BATCH_REFRESH_TIMEOUT):
            # The roll was accepted and may have started a container on the
            # promoted config, so a secret restore alone is not safe: restore the
            # baseline AND roll again so the final container re-reads it, then
            # verify READY (review live-config-refresh-r4 F2).
            self._compensate_after_ready_timeout(delivery, system_id)

    @staticmethod
    def _undo_config_delivery(delivery: dict, system_id: str, reason: str) -> str:
        """Put the baseline agent-config back after a failed roll/READY, so an
        identical retry sees a stale secret and rolls again. Returns:

        * ``"restored"`` -- AWSCURRENT serves the baseline again: the stage was
          moved back, or (when that move is refused) the baseline payload was
          re-written through the same pending-version CAS;
        * ``"superseded"`` -- a newer writer owns AWSCURRENT; left untouched;
        * ``"failed"`` -- the baseline could not be put back; the secret is
          marked delivery-pending (through CAS) so the next provision rolls;
        * ``"noop"`` -- nothing was promoted (e.g. the write helper was stubbed).

        Only ``"restored"`` means the current payload is known to be the
        baseline, so only then may a compensating roll run."""
        secret_name = delivery.get("secret_name")
        promoted = delivery.get("promoted_version")
        if not secret_name or not promoted:
            return "noop"
        region = delivery.get("region") or os.environ.get("AWS_REGION", "us-east-1")
        sm = boto3.client("secretsmanager", region_name=region)
        status = _restore_baseline(sm, secret_name, system_id, promoted,
                                   delivery.get("baseline_version"),
                                   delivery.get("baseline_payload"))
        if status == "restored":
            logger.warning("fast-path provision for %s: %s; restored the previous "
                           "agent-config", system_id, reason)
        elif status == "superseded":
            logger.warning("fast-path provision for %s: %s; a newer agent-config owns the "
                           "secret, left untouched", system_id, reason)
        else:
            logger.warning("fast-path provision for %s: %s; could not restore the baseline "
                           "secret; marking delivery pending so the next provision rolls",
                           system_id, reason)
            _mark_delivery_pending(sm, secret_name, system_id)
        return status

    def _compensate_after_ready_timeout(self, delivery: dict, system_id: str) -> None:
        """A READY timeout after an ACCEPTED roll: the in-progress container may
        already have read the promoted config, so restore the baseline secret AND
        roll the runtime again so the final container re-reads the baseline, then
        verify READY (review live-config-refresh-r4 F2). Always RAISES -- the
        provision fails retryable and never registers. If the compensating roll
        or its READY check cannot be confirmed, the delivery-pending marker is
        left in place (through CAS) so the next provision rolls. The compensating
        roll runs ONLY when the baseline is confirmed restored: it never rolls an
        unverified current payload (the failed attempt's, or a newer writer's)."""
        status = self._undo_config_delivery(delivery, system_id, "rolled runtime not READY")
        if status not in ("restored", "noop"):
            raise RuntimeError(
                f"fast-path provision for {system_id}: rolled runtime not READY and the "
                f"baseline agent-config was not restored ({status}); no compensating roll")
        region = os.environ.get("AWS_REGION", "us-east-1")
        rolled = self._bump_runtime_version(system_id, region)
        ready = rolled == "ok" and self._await_runtime_ready(
            system_id, time.monotonic() + self.BATCH_REFRESH_TIMEOUT)
        if not ready:
            secret_name = delivery.get("secret_name")
            if secret_name:
                _mark_delivery_pending(
                    boto3.client("secretsmanager", region_name=delivery.get("region") or region),
                    secret_name, system_id)
            raise RuntimeError(
                f"fast-path provision for {system_id}: rolled runtime not READY and the "
                "compensating roll could not be confirmed; left delivery pending")
        raise RuntimeError(
            f"fast-path provision for {system_id}: rolled runtime not READY in time; "
            "restored the baseline and re-rolled, refusing to register this attempt")

    def _cloud_fast_path_ready(self, system_id: str, gateway_id: str) -> bool:
        """Fast path: if a gateway target already exists for this system, register
        immediately without recreating cloud resources. The runtime and gateway
        target already exist from the previous deploy — no need to call the
        AgentCore control plane's create/update path on every ECS task restart.
        This avoids 9s × N systems of startup latency that causes ALB health
        check failures.

        IMPORTANT: a READY gateway target ALONE is NOT sufficient. A target can
        linger as stale-READY after its backing AgentCore runtime was deleted (or
        was never created) — a DANGLING target that routes queries to a
        non-existent runtime ("endpoint not responding"), which then lets the
        orchestrator fabricate answers and silently breaks the data plane. So the
        fast path requires BOTH (a) a READY gateway target on ``gateway_id`` AND
        (b) a backing runtime for this system that exists and is READY. If either
        is missing the caller falls through to _start_cloud to (re)create the
        runtime. A single list_agent_runtimes call is acceptable for the healthy
        case."""
        region = os.environ.get("AWS_REGION", "us-east-1")
        control = boto3.client("bedrock-agentcore-control", region_name=region)

        # (a) READY gateway target?
        target_exists = False
        if gateway_id:
            try:
                target_name = system_id.replace("_", "-")
                for t in _fast_path_targets(control, gateway_id):
                    if t.get("name") == target_name and t.get("status") == "READY":
                        target_exists = True
                        break
            except Exception:
                target_exists = False  # don't assume — if we can't check, go through _start_cloud
        else:
            target_exists = True  # no gateway configured, target check is moot

        # (b) READY backing runtime?
        if target_exists and self._runtime_is_ready(control, system_id):
            return True
        if target_exists:
            logger.info(
                f"Cloud agent {system_id}: gateway target is READY but the backing "
                f"runtime {self._runtime_name(system_id)} is absent or not READY "
                f"(stale/dangling target) — (re)creating via _start_cloud"
            )
        else:
            logger.info(f"Cloud agent {system_id}: no READY gateway target found, creating via _start_cloud")
        return False

    def _start_local(
        self, system_id: str, system_name: str, schema: str,
        system_prompt: str, tables_desc: str, conn_params: dict | None,
        connection_type: str = "PostgreSQL", connection_config: dict | None = None,
    ) -> str:
        """Start *system_id*'s agent (host-local, unauthenticated, so on
        loopback only) in this process; its MCP URL. Only while the running
        operation's lease is held, and stopped again if it is lost later
        (:meth:`_stop_local_server`). The configuration, connection
        parameters included, is handed to the server as Python objects
        (deep copies, as the agent process used to get its own), never
        through a command line."""
        from .system_agent import create_mcp_server   # lazy: cloud mode never needs it
        check_write_guard()
        host, port = "127.0.0.1", self._allocate_port()
        build = functools.partial(
            create_mcp_server, system_id=system_id, system_name=system_name,
            schema=schema, system_prompt=system_prompt, tables_desc=tables_desc,
            conn_params=copy.deepcopy(conn_params) or None,
            connection_type=connection_type,
            connection_config=copy.deepcopy(connection_config) or None,
            # Local mode has no AgentCore Gateway in front, so emulate the gateway's
            # "<target>___" tool-name prefix here. This makes local tool names match
            # what the orchestrator prompt + per-system query_guidance expect
            # (e.g. mes-cr___execute_sql_mes_cr), so guidance-following tool calls
            # resolve locally exactly as they do through the gateway in the cloud.
            tool_name_prefix=f"{system_id.replace('_', '-')}___",
            host=host, port=port)
        server = LocalAgentServer(system_id, host, port, build, self.STOP_WAIT_SECONDS)
        try:
            server.start(self.LOCAL_START_TIMEOUT_SECONDS)
        except RuntimeError:
            self._release_port(port)
            raise
        mcp_url = f"http://{host}:{port}/mcp"
        self._agents[system_id] = AgentInstance(
            system_id=system_id, system_name=system_name, schema=schema,
            system_prompt=system_prompt, tables_desc=tables_desc,
            mcp_url=mcp_url, port=port, server=server,
        )
        release_on_loss(lambda: self._stop_local_server(system_id, server))
        print(f"Started local agent {system_id} on port {port}")
        return mcp_url

    #: How long a local agent's server gets to build its agent and listen.
    LOCAL_START_TIMEOUT_SECONDS = 60

    #: How long a stopping local agent's open requests get before they are
    #: cancelled, and how much longer its thread gets to end after that.
    STOP_WAIT_SECONDS = 5

    def _stop_server(self, inst: AgentInstance) -> None:
        """Stop *inst*'s local server (:meth:`LocalAgentServer.stop`). Its
        port is released whatever happens, a server thread that has not
        ended included (its ``TimeoutError`` then reaches the caller)."""
        try:
            inst.server.stop()
        finally:
            self._release_port(inst.port)
        print(f"Stopped local agent {inst.system_id} (port {inst.port})")

    def _stop_local_server(self, system_id: str, server) -> None:
        """Stop the local agent *server* started for *system_id*, free its
        port and drop its registration, if it is still the registered one:
        the provision that started it lost its lease. A server that has
        replaced it since is never stopped, nor its registration dropped."""
        inst = self._agents.get(system_id)
        if inst is None or inst.server is not server:
            return
        logger.warning("Stopping local agent %s: the provision that started it "
                       "lost its lease", system_id)
        if self._agents.get(system_id) is inst:     # not one registered since the check
            del self._agents[system_id]
        self._stop_server(inst)

    def _ensure_credential_provider(self, control, region: str) -> str:
        """Ensure the OAuth2 credential provider exists for gateway → runtime M2M auth.
        
        Creates it on first call, caches the ARN for subsequent calls.
        Uses the Cognito M2M user pool configured in the AgentCore stack.
        Both lookups read EVERY provider page (a provider on page 2 exists).
        """
        if self._credential_provider_arn:
            return self._credential_provider_arn

        if not self._m2m_client_id or not self._m2m_discovery_url:
            logger.warning("M2M OAuth config not set — gateway targets will use no auth")
            return ""

        provider_name = "adt-m2m"

        # Check if it already exists
        try:
            existing = self._find_credential_provider_arn(control, provider_name)
        except Exception:  # intentional fallback: an unreadable list falls through to create
            existing = ""
        if existing:
            self._credential_provider_arn = existing
            logger.info(f"Reusing credential provider: {self._credential_provider_arn}")
            return self._credential_provider_arn

        client_secret = self._m2m_client_secret(region)
        if client_secret is None:
            return ""

        # Create the credential provider
        try:
            resp = control.create_oauth2_credential_provider(
                name=provider_name,
                credentialProviderVendor="CustomOauth2",
                oauth2ProviderConfigInput=self._m2m_provider_config(client_secret),
            )
            self._credential_provider_arn = resp["credentialProviderArn"]
            logger.info(f"Created credential provider: {self._credential_provider_arn}")
            return self._credential_provider_arn
        except Exception as e:
            if "ConflictException" in str(type(e).__name__) or "already exists" in str(e).lower():
                # Race condition — try listing again (every page)
                existing = self._find_credential_provider_arn(control, provider_name)
                if existing:
                    self._credential_provider_arn = existing
                    return self._credential_provider_arn
            logger.error(f"Failed to create credential provider: {e}")
            return ""

    @staticmethod
    def _find_credential_provider_arn(control, provider_name: str) -> str:
        """ARN of the named OAuth2 credential provider, searched over EVERY
        ``list_oauth2_credential_providers`` page; ``""`` when absent. Listing
        errors propagate (callers decide whether to fall through)."""
        for p in list_all_oauth2_credential_providers(control):
            if p.get("name") == provider_name:
                return p["credentialProviderArn"]
        return ""

    def _m2m_client_secret(self, region: str) -> str | None:
        """The Cognito M2M app client's secret; ``None`` (logged) when unreadable."""
        cognito_client = boto3.client("cognito-idp", region_name=region)
        try:
            resp = cognito_client.describe_user_pool_client(
                UserPoolId=self._m2m_pool_id,
                ClientId=self._m2m_client_id,
            )
            return resp["UserPoolClient"].get("ClientSecret", "")
        except Exception as e:
            logger.error(f"Failed to get Cognito client secret: {e}")
            return None

    def _m2m_provider_config(self, client_secret: str) -> dict:
        """``oauth2ProviderConfigInput`` for the Cognito M2M app client."""
        return {
            "customOauth2ProviderConfig": {
                "clientId": self._m2m_client_id,
                "clientSecret": client_secret,
                "oauthDiscovery": {
                    "discoveryUrl": self._m2m_discovery_url,
                },
            },
        }

    @staticmethod
    def _runtime_connection_env(
        connection_type: str, connection_secret_arn: str, effective_config: dict
    ) -> dict:
        """Connection-specific runtime env vars.

        Thin wrapper over the shared
        :func:`agents.runtime_provision_helpers.runtime_connection_env` (lifted to
        a shared module so the in-app create path and the provisioner handler
        cannot drift — Wave 2 2.3R). Retained as a staticmethod for the existing
        callers/tests; behavior is unchanged.
        """
        return _shared_runtime_connection_env(
            connection_type, connection_secret_arn, effective_config
        )

    @staticmethod
    def _system_agent_model(cfg: dict, region: str) -> str:
        """Per-system model override (catalog entry ``system_model``), else the
        configured agent model: the Admin > Settings save (SSM
        ``/adt/llm-agent-model``), else the deployment-wide
        ``SYSTEM_AGENT_MODEL`` env, else the model catalog's AGENT default.
        Athena SQL is harder than Postgres, so a system can pin a stronger
        model. Reading the saved model here is what makes a system onboarded
        or re-onboarded after a save run the model Settings reports."""
        override = (cfg.get("system_model") or "").strip()
        if override:
            return override
        from agents.model_config import configured_agent_model
        return configured_agent_model(region)

    @staticmethod
    def _base_env_vars(
        system_id: str,
        system_name: str,
        schema: str,
        connection_type: str,
        region: str,
        connection_config: dict | None = None,
    ) -> dict:
        """Base (connection-agnostic) runtime env vars for a system agent —
        delegated to the ONE contract builder
        (``agents/runtime_env_contract.py::build_env("system", …)``), which
        owns the key set, the ambient-env fallbacks, and the region-keyed
        BEDROCK_MODEL default. Only ``SYSTEM_AGENT_MODEL`` is computed here
        (per-system model override — see ``_system_agent_model``; it needs
        the model catalog, which the import-light contract cannot touch).

        SYSTEM_PROMPT / TABLES_DESC are intentionally omitted — regenerated
        at runtime to stay under AgentCore's 4000-byte env limit.
        SYSTEM_REQUIRED_GROUP is a per-system operator choice — deliberately
        set NOWHERE here (ADR-0049)."""
        from agents.runtime_env_contract import RuntimeEnvSources, build_env
        return build_env(
            "system",
            RuntimeEnvSources.from_ambient(region=region),
            system_id=system_id,
            system_name=system_name,
            schema=schema,
            connection_type=connection_type,
            system_agent_model=AgentManager._system_agent_model(
                connection_config or {}, region),
        )

    def ensure_digital_thread_target(self) -> str:
        """Provision the built-in `digital-thread` knowledge-graph system as a gateway target.

        The Neptune connection is via the runtime IAM role (GRAPH_IDENTIFIER) — no DB
        credential secret. Reuses the same SYSTEM_ID-driven cloud provisioning path as
        onboarded systems (fast-path reuse + _start_cloud create/sync), so it is
        idempotent across deploys.

        Cloud-only: in local mode the orchestrator already has the graph tools
        in-process and Quick connects to a deployed gateway, so there is nothing to
        provision locally. Returns the gateway MCP URL (or "" when skipped).
        """
        if self.mode != "cloud":
            logger.info("digital-thread target is cloud-only; skipping in %s mode", self.mode)
            return ""
        logger.info("Ensuring digital-thread knowledge-graph gateway target")
        return self.start_agent(
            system_id=DIGITAL_THREAD_SYSTEM_ID,
            system_name="Digital Thread (knowledge graph)",
            schema="",
            system_prompt="",
            tables_desc="",
            connection_type=KNOWLEDGE_GRAPH_CONNECTION_TYPE,
        )

    @staticmethod
    def _quick_gateway_override() -> tuple[str, str]:
        """The dedicated Quick gateway (`adt-quick-gw`) for the `query-bridge`
        target, from the QUICK_GATEWAY_ID / QUICK_GATEWAY_URL task env
        (adt_compute_stack._env_quick_integration, quick_api_enabled deploys).

        Raises :class:`QuickGatewayNotConfiguredError` when unset. The compute
        stack sets QUICK_GATEWAY_ID whenever it sets QUICK_API_ENABLED, so on
        any deploy from one build the env is always present; a missing id means
        a stack/image mismatch, and provisioning the bridge on the SHARED
        gateway instead would quietly recreate the fan-out the dedicated
        gateway exists to prevent (ADR-0071). Refuse, loudly."""
        gateway_id = os.environ.get("QUICK_GATEWAY_ID", "").strip()
        gateway_url = os.environ.get("QUICK_GATEWAY_URL", "").strip()
        if gateway_id:
            return gateway_id, gateway_url
        raise QuickGatewayNotConfiguredError(
            "QUICK_GATEWAY_ID is not set but the Quick integration is enabled. The "
            "query-bridge target is only provisioned on the dedicated adt-quick-gw; "
            "refusing to put it on the SHARED gateway (AGENTCORE_GATEWAY_ID), where Quick "
            "would see every system's tools. Redeploy with quick_api_enabled from one "
            "build so ADTAgentCoreStack builds adt-quick-gw and the task env carries "
            "QUICK_GATEWAY_ID/URL.")

    def ensure_query_bridge_target(self) -> str:
        """Provision the built-in `query-bridge` Quick-integration gateway target.

        A stateless FastMCP tool server (agents/query_bridge_server.py) that
        submits/polls the app's async ask endpoints over the VPC, so Amazon
        Quick web/Suite gets the orchestrator's answer through an AgentCore
        Gateway (CUSTOM_JWT, adt-app issuer) — no API Gateway needed on that
        path. Its runtime env is composed by runtime_connection_env's
        QueryBridge branch (ADT_APP_URL + QUICK_GATEWAY_TOKEN_REF, or the
        plaintext QUICK_GATEWAY_TOKEN on a stack that sets no reference; no DB
        credential secret). Reuses the same SYSTEM_ID-driven cloud
        provisioning path as onboarded systems (fast-path reuse +
        _start_cloud create/sync), so it is idempotent across deploys.

        The target is provisioned on the DEDICATED Quick gateway
        (`adt-quick-gw`, QUICK_GATEWAY_ID/URL — see _quick_gateway_override)
        so it is the ONLY tool set Quick's connector discovers; when that env
        is unset provisioning is refused (QuickGatewayNotConfiguredError).
        After a successful provision, a `query-bridge` target left on the
        SHARED gateway by a deployment that predates the dedicated one is
        removed (_remove_stale_bridge_target_from_shared_gateway).

        Called ONLY when the Quick integration is enabled (server.py gates on
        QUICK_API_ENABLED, which adt_compute_stack sets from the
        quick_api_enabled context flag) — base deploys never provision it.
        Cloud-only, like the digital-thread target. Returns the gateway MCP
        URL Quick should connect to (or "" when skipped).
        """
        if self.mode != "cloud":
            logger.info("query-bridge target is cloud-only; skipping in %s mode", self.mode)
            return ""
        gateway_id, gateway_url = self._quick_gateway_override()
        logger.info("Ensuring query-bridge Quick-integration gateway target on gateway %s",
                    gateway_id)
        mcp_url = self._provision_query_bridge(gateway_id, gateway_url)
        self._remove_stale_bridge_target_from_shared_gateway(gateway_id)
        return mcp_url

    def _provision_query_bridge(self, gateway_id: str, gateway_url: str) -> str:
        """``start_agent`` for the bridge, unless the app holds the token by
        reference and the bridge runtime's env is not known to hold it: a
        bridge provisioned before the token moved to the generated secret
        carries the old plaintext token, and the fast path would keep it.
        Then provision through ``_start_cloud``, which rewrites the runtime
        env, and raise unless a fresh read shows the reference and no
        plaintext token, so the provisioner retries instead of serving 401s.
        An unreadable env counts as not current, both before and after."""
        kwargs = dict(system_id=QUERY_BRIDGE_SYSTEM_ID,
                      system_name="Quick query bridge (async ask)",
                      schema="", system_prompt="", tables_desc="",
                      connection_type=QUERY_BRIDGE_CONNECTION_TYPE,
                      gateway_id=gateway_id, gateway_url=gateway_url)
        if self._bridge_token_env() in ("no-reference", "absent", "current"):
            return self.start_agent(**kwargs)
        logger.warning("query-bridge runtime env does not show the x-quick-auth token "
                       "reference; reprovisioning it")
        mcp_url = self._start_cloud(**kwargs)
        if self._bridge_token_env() != "current":
            raise RuntimeError("query-bridge runtime may still carry the old x-quick-auth "
                               "token after reprovisioning")
        return mcp_url

    def _bridge_token_env(self) -> str:
        """Where the bridge runtime stands on the x-quick-auth token:
        ``no-reference`` (the app holds no reference, nothing to move),
        ``absent`` (no bridge runtime yet; creating one writes the reference),
        ``current`` (its env holds the reference and no plaintext token),
        ``old`` (it does not) or ``unknown`` (the lookup failed)."""
        if not os.environ.get("QUICK_GATEWAY_TOKEN_REF", "").strip():
            return "no-reference"
        try:
            control = boto3.client("bedrock-agentcore-control",
                                   region_name=os.environ.get("AWS_REGION", "us-east-1"))
            runtime_id = _find_runtime_id(control, self._runtime_name(QUERY_BRIDGE_SYSTEM_ID))
            if not runtime_id:
                return "absent"
            env = control.get_agent_runtime(agentRuntimeId=runtime_id).get(
                "environmentVariables") or {}
        except Exception as e:  # noqa: BLE001 - probe result is "unknown", logged
            logger.warning("query-bridge: could not read the runtime env: %s", type(e).__name__)
            return "unknown"
        current = "QUICK_GATEWAY_TOKEN_REF" in env and "QUICK_GATEWAY_TOKEN" not in env
        return "current" if current else "old"

    def _remove_stale_bridge_target_from_shared_gateway(self, quick_gateway_id: str) -> None:
        """Delete a `query-bridge` target left on the SHARED gateway
        (AGENTCORE_GATEWAY_ID) by a deployment that predates the dedicated
        Quick gateway. Left in place it re-exposes submit/poll tools to the
        analyst-path connector alongside the raw system tools — a milder form
        of the drift ADR-0071 fixes. Runs after the dedicated target is up, so
        a failed provision leaves the environment untouched; its own failures
        are logged, never raised (the bridge is already serving)."""
        shared = self.gateway_id
        if not shared or shared == quick_gateway_id:
            return
        region = os.environ.get("AWS_REGION", "us-east-1")
        try:
            control = boto3.client("bedrock-agentcore-control", region_name=region)
            stale = [t for t in list_all_gateway_targets(control, shared)
                     if t.get("name") == QUERY_BRIDGE_SYSTEM_ID and t.get("targetId")]
        except Exception as e:
            logger.warning("Could not list shared gateway %s for a stale query-bridge "
                           "target: %s", shared, e)
            return
        if not stale:
            return
        for t in stale:
            logger.warning("Removing stale query-bridge target %s from the SHARED gateway "
                           "%s (it now lives on %s)", t["targetId"], shared, quick_gateway_id)
            self._delete_gateway_target_and_wait(
                control, shared, t["targetId"], QUERY_BRIDGE_SYSTEM_ID)

    def _start_cloud(
        self, system_id: str, system_name: str, schema: str,
        system_prompt: str, tables_desc: str,
        connection_type: str = "PostgreSQL", connection_config: dict | None = None,
        conn_params: dict | None = None, *,
        gateway_id: str | None = None, gateway_url: str | None = None,
    ) -> str:
        """Provision (or reuse) the AgentCore runtime + gateway target for one
        system. Orchestration only — each step lives in a ≤50-line helper:
        env/secret prep, runtime reuse/image-refresh or create-with-AZ-retry,
        warmup, and gateway-target upsert/sync. ``gateway_id``/``gateway_url``
        default to the manager's shared gateway (see start_agent)."""
        gateway_id = self.gateway_id if gateway_id is None else gateway_id
        gateway_url = self.gateway_url if gateway_url is None else gateway_url
        region = os.environ.get("AWS_REGION", "us-east-1")
        control = boto3.client("bedrock-agentcore-control", region_name=region)
        runtime_name = self._runtime_name(system_id)
        env_vars = self._prepare_runtime_env(
            system_id, system_name, schema, system_prompt, tables_desc,
            connection_type, connection_config, conn_params, region)
        authorizer_config = self._m2m_authorizer_config()

        runtime_arn, runtime_id = self._find_reusable_runtime(
            control, runtime_name, authorizer_config)
        if runtime_arn and runtime_id:
            self._refresh_existing_runtime_image(
                control, runtime_id, runtime_name, env_vars, authorizer_config)
        if not runtime_arn:
            runtime_arn, runtime_id = self._create_runtime_with_az_retry(
                control, runtime_name, env_vars, authorizer_config)

        # Derive MCP endpoint URL from ARN (shared helper — single source of truth),
        # then warm up before the gateway sync (cold start is a few seconds).
        runtime_mcp_url = self._runtime_mcp_url(runtime_arn, region)
        self._warmup_runtime(runtime_mcp_url, region)
        gateway_target_id = self._upsert_and_sync_gateway_target(
            control, region, system_id, system_name, runtime_mcp_url,
            gateway_id=gateway_id)

        # Orchestrator connects to the gateway, not individual runtimes.
        mcp_url = gateway_url or runtime_mcp_url
        self._agents[system_id] = AgentInstance(
            system_id=system_id, system_name=system_name, schema=schema,
            system_prompt=system_prompt, tables_desc=tables_desc,
            mcp_url=mcp_url, runtime_arn=runtime_arn,
            gateway_target_id=gateway_target_id, gateway_id=gateway_id,
        )
        logger.info(f"Created cloud agent {system_id}: runtime={runtime_id}, target={gateway_target_id}")
        return mcp_url

    def _prepare_runtime_env(self, system_id: str, system_name: str,
                             schema: str, system_prompt: str, tables_desc: str,
                             connection_type: str,
                             connection_config: dict | None,
                             conn_params: dict | None, region: str) -> dict:
        """Build the runtime env vars: effective connection config (conn_params
        merged for PostgreSQL), the agent-config secret upsert, and the base +
        connection env. KnowledgeGraph systems (digital-thread) reach Neptune
        via the runtime IAM role + GRAPH_IDENTIFIER — no credentialed DB
        connection, so no Secrets Manager config and no conn_params. The same
        applies to QueryBridge (ADT_APP_URL + shared token, composed by
        runtime_connection_env)."""
        is_secretless = connection_type in _SECRETLESS_CONNECTION_TYPES
        effective_config = system_connection_config(
            schema, connection_type, connection_config, conn_params)
        connection_secret_arn = ""  # nosec B105 - not a credential value (placeholder/key/secret arn)
        if not is_secretless:
            connection_secret_arn = self._upsert_agent_config_secret(
                system_id, effective_config, system_prompt, tables_desc, region)
        env_vars = self._base_env_vars(
            system_id, system_name, schema, connection_type, region, effective_config
        )
        env_vars.update(
            self._runtime_connection_env(connection_type, connection_secret_arn, effective_config)
        )
        # DATASET_REF_BUCKET (Wave 4 claim-check redemption) and
        # ONBOARDING_SESSIONS_TABLE (Wave 5 provenance sidecar) are part of
        # the contract's system-role payload — built inside _base_env_vars'
        # build_env("system", …) delegation, not composed here.
        return env_vars

    def _write_agent_config_on_fast_path(self, system_id: str, schema: str,
                                         system_prompt: str, tables_desc: str,
                                         connection_type: str,
                                         connection_config: dict | None,
                                         delivery: dict | None = None) -> None:
        """READ-MODIFY-WRITE the baked ``tables_desc`` / ``system_prompt`` (and
        ``concept_mappings`` / ``field_descriptions`` when the provision built
        them) into the ``adt/agent-config/<id>`` secret when a PROVISION reuses
        an existing runtime (the cloud fast path). The runtime reads its schema
        from the secret, so a reused runtime must still get the fresh bake.

        Returns ``True`` only when the merged secret was written (the caller
        then deploys a new runtime version so a warm container re-reads it);
        ``False`` on a NO-OP (secretless system, no secret yet, an incomplete
        bake, or the secret already current). A genuine write failure RAISES --
        it is NOT reported as ``False`` -- so the caller fails the provision
        rather than mistaking an unpersisted merge for "already current" (review
        live-config-refresh F1). The merge preserves EVERY existing sibling key
        (credentials, ``external_id``, ``business_context``, PostgreSQL
        ``conn_params``, measured identifiers, and the generated keys this
        provision did not rebuild) -- never a whole-value overwrite (the
        ADR-0054 D4 bug class). Runs under ``RUNTIME_CONFIG_LOCK`` as the other
        secret writers do. Secretless systems have no agent-config secret."""
        if connection_type in _SECRETLESS_CONNECTION_TYPES:
            return False
        region = os.environ.get("AWS_REGION", "us-east-1")
        with RUNTIME_CONFIG_LOCK:
            return self._merge_generated_keys_secret(
                system_id, system_prompt, tables_desc, connection_config, region,
                delivery=delivery)

    def _merge_generated_keys_secret(self, system_id: str, system_prompt: str,
                                     tables_desc: str, connection_config: dict | None,
                                     region: str, delivery: dict | None = None) -> bool:
        """The read-modify-write body of :meth:`_write_agent_config_on_fast_path`
        (under the lock). Returns ``False`` on a NO-OP (no secret yet, incomplete
        bake, already current); a write that cannot be verified RAISES (F1).
        ``tables_desc`` is written only when it is a complete baked schema (F4),
        and the promotion is the version-stage CAS in :meth:`_cas_put_agent_config`
        (F3). When ``delivery`` is given the baseline and promoted version ids are
        recorded in it for the caller's roll-back. A ``_DELIVERY_PENDING_KEY``
        marker from an earlier attempt whose rollback failed forces a write even
        when the bytes match (r3 F1); the merge clears it so a later READY does."""
        if not _tables_desc_is_complete(tables_desc):
            # An empty, partial, or names-only description must never replace a
            # complete stored schema.
            return False
        secret_name = f"adt/agent-config/{system_id}"
        sm = boto3.client("secretsmanager", region_name=region)
        not_found = getattr(sm.exceptions, "ResourceNotFoundException", ())
        try:
            read = sm.get_secret_value(SecretId=secret_name)
        except not_found:
            logger.info("fast-path agent-config: %s has no secret yet -- skipped", system_id)
            return False
        current = json.loads(read.get("SecretString") or "{}")
        if not isinstance(current, dict):
            raise ValueError("agent-config secret is not a JSON object")
        updates = self._generated_key_updates(system_prompt, tables_desc, connection_config)
        pending = bool(current.get(_DELIVERY_PENDING_KEY))
        if not pending and all(current.get(k) == v for k, v in updates.items()):
            return False                      # already current: no write, no roll
        # Build the merged payload and clear any stale delivery-pending marker;
        # the caller re-sets it only if a roll/READY failure cannot be rolled back.
        merged = {k: v for k, v in {**current, **updates}.items() if k != _DELIVERY_PENDING_KEY}
        promoted = self._cas_put_agent_config(
            sm, secret_name, read.get("VersionId"), merged, system_id)
        if delivery is not None:
            delivery.update(secret_name=secret_name, region=region,
                            baseline_version=read.get("VersionId"), promoted_version=promoted,
                            baseline_payload=dict(current))
        logger.info("fast-path agent-config: %s merged keys %s (tables_desc/system_prompt) "  # keys only, never values
                    "into its existing agent-config entry", system_id, sorted(updates))
        return True

    @staticmethod
    def _generated_key_updates(system_prompt, tables_desc, connection_config) -> dict:
        """The generated keys THIS provision rebuilt. A key it did not build is
        left out so the merge preserves it as-is (never nulled)."""
        updates = {"tables_desc": tables_desc}
        if system_prompt:
            updates["system_prompt"] = system_prompt
        for key in ("concept_mappings", "field_descriptions"):
            value = (connection_config or {}).get(key)
            if value not in (None, "", [], {}):
                updates[key] = value
        return updates

    @staticmethod
    def _cas_put_agent_config(sm, secret_name, read_version_id, merged, system_id) -> str:
        """Compare-and-set put of the merged agent-config via the shared
        pending-version protocol (:func:`_cas_put_payload`): write a unique
        pending version, promote it only while AWSCURRENT still belongs to the
        version read, else raise. Returns the promoted version id."""
        return _cas_put_payload(sm, secret_name, read_version_id, merged, system_id)

    def _upsert_agent_config_secret(self, system_id: str, effective_config: dict,
                                    system_prompt: str, tables_desc: str,
                                    region: str) -> str:
        """Store the connection config in the ``adt/agent-config/<id>`` secret.

        SYSTEM_PROMPT / TABLES_DESC are deliberately kept OUT of the runtime env
        (capped at 4000 bytes — see ``_base_env_vars``): a full per-table column
        schema easily exceeds that, so both ride in the agent-config secret
        alongside the connection config (the same size-limit bypass) and are
        read back by ``runtime_app._run_system_agent``. Baking the schema here
        is what lets the system agent compose queries WITHOUT re-running
        list_tables/describe/information_schema per sub-question (the dominant
        Mode A latency source). Written to a COPY so the env builders still see
        only the connection config. Returns "" on failure (the caller falls
        back to the truncated env var). The value is fitted to the secret size
        bound first (:func:`agent_config_secret_value`)."""
        try:
            import boto3 as _boto3
            sm = _boto3.client("secretsmanager", region_name=region)
            secret_payload = dict(effective_config)
            if tables_desc:
                secret_payload["tables_desc"] = tables_desc
            if system_prompt:
                secret_payload["system_prompt"] = system_prompt
            arn = _shared_upsert_connection_secret(
                sm, f"adt/agent-config/{system_id}",
                _agent_config_secret_value(secret_payload, label=system_id))
            logger.info(f"Stored connection config in Secrets Manager: {arn}")
            return arn
        except Exception as e:
            logger.warning(f"Failed to store connection config in Secrets Manager: {e} — falling back to truncated env var")
            return ""

    def _m2m_authorizer_config(self) -> dict | None:
        """JWT authorizer config so the runtime accepts M2M tokens from the
        gateway; ``None`` when the M2M pool is not configured."""
        if self._m2m_discovery_url and self._m2m_client_id:
            return {
                "customJWTAuthorizer": {
                    "discoveryUrl": self._m2m_discovery_url,
                    "allowedClients": [self._m2m_client_id],
                }
            }
        return None

    @staticmethod
    def _wait_runtime_deleted(control, runtime_id: str, timeout: int = 120,
                              poll_log: str = "") -> bool:
        """Poll until ``get_agent_runtime`` raises (= deleted) or *timeout*.

        ``poll_log`` (optionally carrying a ``{status}`` placeholder) logs each
        still-alive poll. Returns True once the runtime is gone."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            try:
                check = control.get_agent_runtime(agentRuntimeId=runtime_id)
                if poll_log:
                    logger.info(poll_log.format(status=check.get("status")))
            except Exception:
                return True
        return False

    def _find_reusable_runtime(self, control, runtime_name: str,
                               authorizer_config: dict | None) -> tuple:
        """Find an existing ``runtime_name`` runtime to reuse.

        A DELETING/DELETE_FAILED runtime is never reused — wait for its
        deletion, then fall through to create. A CREATE_FAILED/UPDATE_FAILED
        runtime is never reused either: it has no working version behind its
        name, so reusing it would sync a gateway target at a dead endpoint
        (event 34, Team 1: both built-in runtimes failed at the service's
        network-config step and a task restart would have "found" them). It
        is deleted, then the caller creates afresh. A READY runtime whose
        Cognito authorizer is stale is deleted for recreation
        (update_agent_runtime always fails on it). Returns ``(runtime_arn,
        runtime_id)`` — ``(None, None)`` when absent (or the best-effort
        listing failed)."""
        runtime_arn = runtime_id = None
        try:
            for rt in list_all_agent_runtimes(control):
                if rt.get("agentRuntimeName") != runtime_name:
                    continue
                rt_status = rt.get("status", "")
                if rt_status in ("DELETING", "DELETE_FAILED"):
                    logger.info(f"Runtime {runtime_name} is in state {rt_status} — waiting for deletion before creating new one")
                    rt_id = rt["agentRuntimeId"]
                    if self._wait_runtime_deleted(
                            control, rt_id,
                            poll_log=f"Runtime {rt_id} still exists, status={{status}}"):
                        logger.info(f"Runtime {rt_id} deleted — proceeding to create new one")
                    break
                if rt_status in RUNTIME_FAILED_STATES:
                    rt_id = rt["agentRuntimeId"]
                    logger.warning(
                        f"Runtime {runtime_name} ({rt_id}) is {rt_status} — not reusable; "
                        f"deleting it and creating a new one")
                    self._delete_failed_runtime(control, rt_id)
                    break
                runtime_arn = rt["agentRuntimeArn"]
                runtime_id = rt["agentRuntimeId"]
                logger.info(f"Runtime {runtime_name} already exists ({runtime_id}) status={rt_status}, reusing")
                if authorizer_config and rt_status == "READY" and \
                        self._delete_stale_authorizer_runtime(
                            control, runtime_name, runtime_id, authorizer_config):
                    runtime_arn = runtime_id = None
                break
        except Exception:  # nosec B110 - intentional fallback, no recovery action needed
            pass
        return runtime_arn, runtime_id

    def _delete_stale_authorizer_runtime(self, control, runtime_name: str,
                                         runtime_id: str,
                                         authorizer_config: dict) -> bool:
        """Delete a READY runtime whose JWT authorizer points at a DIFFERENT
        Cognito pool than the current one (update_agent_runtime always fails on
        it), so the caller recreates it with the correct authorizer. Returns
        True when deleted; best-effort False otherwise."""
        try:
            rt_detail = control.get_agent_runtime(agentRuntimeId=runtime_id)
            existing_auth = rt_detail.get("authorizerConfiguration", {})
            existing_discovery = (existing_auth.get("customJWTAuthorizer") or {}).get("discoveryUrl", "")
            expected_discovery = (authorizer_config.get("customJWTAuthorizer") or {}).get("discoveryUrl", "")
            if not (existing_discovery and expected_discovery
                    and existing_discovery != expected_discovery):
                return False
            logger.warning(
                f"Runtime {runtime_name} has stale authorizer "
                f"(has={existing_discovery}, want={expected_discovery}) — deleting to recreate"
            )
            control.delete_agent_runtime(agentRuntimeId=runtime_id)
            self._wait_runtime_deleted(control, runtime_id)
            logger.info(f"Runtime {runtime_name} deleted — will recreate with correct authorizer")
            return True
        except Exception as auth_err:
            logger.warning(f"Failed to check authorizer for {runtime_name}: {auth_err}")
            return False

    def _refresh_existing_runtime_image(self, control, runtime_id: str,
                                        runtime_name: str, env_vars: dict,
                                        authorizer_config: dict | None) -> None:
        """Re-onboard/redeploy path: point the EXISTING runtime at the CURRENT
        agent image and wait for READY (a reused runtime otherwise stays on its
        old image — observed crash-looping until manually deleted). The shared
        kwargs builder preserves the network config (minus the immutable
        ``requireServiceS3Endpoint``) and ROUND-TRIPS ``protocolConfiguration``
        — update_agent_runtime is full-replacement, so omitting it silently
        downgraded MCP runtimes to HTTP (ADR-0056). The fresh artifact/role/
        authorizer built by the caller override the details' copies;
        ``authorizer_config or {}`` keeps the omit-when-unset semantics.
        Best-effort: a failed image refresh must not abort re-onboarding — the
        runtime still exists and the gateway target can be (re)synced."""
        container_uri = _shared_container_uri(self.ecr_image_uri)
        try:
            self._update_runtime_image(control, runtime_id, env_vars,
                                       authorizer_config, container_uri)
            logger.info(
                f"Runtime {runtime_name} already exists — updated to current image "
                f"{container_uri} (redeploy propagates without a manual delete)"
            )
            deadline = time.time() + 300
            while True:  # wait for READY, exactly as the create path does
                status_resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
                status = status_resp.get("status", "")
                if status == "READY":
                    break
                if status in ("CREATE_FAILED", "UPDATE_FAILED"):
                    raise RuntimeError(
                        f"Runtime {runtime_id} failed after image update: "
                        f"{status_resp.get('failureReason', '')}"
                    )
                if time.time() > deadline:
                    raise TimeoutError(
                        f"Runtime {runtime_id} not READY after image update (status={status})"
                    )
                logger.info(f"Waiting for runtime {runtime_id} after image update (status={status})...")
                time.sleep(5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        except Exception as upd_err:
            logger.warning(
                f"Failed to refresh image for existing runtime {runtime_name}: {upd_err}"
            )

    def _update_runtime_image(self, control, runtime_id: str, env_vars: dict,
                              authorizer_config: dict | None,
                              container_uri: str) -> None:
        """The read and full-replacement write of the image refresh, under
        ``RUNTIME_CONFIG_LOCK`` (the READY wait that follows is not)."""
        with RUNTIME_CONFIG_LOCK:
            details = control.get_agent_runtime(agentRuntimeId=runtime_id)
            update_kwargs = _shared_runtime_update_kwargs(
                runtime_id, details, env_vars,
                # The whole point: point the runtime at the CURRENT image.
                artifact={"containerConfiguration": {"containerUri": container_uri}},
                role_arn=self.runtime_role_arn or details.get("roleArn", ""),
                authorizer_config=authorizer_config or {},
            )
            control.update_agent_runtime(**update_kwargs)

    def _create_runtime_with_az_retry(self, control, runtime_name: str,
                                      env_vars: dict,
                                      authorizer_config: dict | None) -> tuple:
        """Create a new runtime (JWT auth, VPC mode for private RDS access),
        retrying a failed create up to ``RUNTIME_CREATE_ATTEMPTS`` times.

        Two failure classes come back as CREATE_FAILED. AgentCore VPC runtimes
        can only launch in the AZs the service supports: CDK's max_azs=2 VPC
        may include an unsupported AZ (e.g. us-east-1 use1-az6), and the
        failureReason names the offending subnet(s). We strip those and
        recreate rather than hardcoding a per-region allowlist that would rot
        as AgentCore expands coverage (ADR 0010). Any OTHER CREATE_FAILED is
        treated as transient (the service's own network-config InternalError
        on a fresh account, event 34): the failed runtime is deleted and the
        same create is tried again after a pause. Either way the failed
        runtime is gone before the retry, so the name is free."""
        vpc_subnets = _subnets_minus_unsupported(
            [s.strip() for s in os.environ.get("VPC_PRIVATE_SUBNETS", "").split(",") if s.strip()])
        vpc_sg = os.environ.get("AGENTCORE_RUNTIME_SG", "")
        container_uri = _shared_container_uri(self.ecr_image_uri)
        runtime_arn, runtime_id, last_reason = None, "", None
        for attempt in range(RUNTIME_CREATE_ATTEMPTS):
            create_kwargs = {
                "agentRuntimeName": runtime_name,
                "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": container_uri}},
                "networkConfiguration": _shared_network_config(vpc_subnets, vpc_sg),
                "protocolConfiguration": {"serverProtocol": "MCP"},
                "roleArn": self.runtime_role_arn,
                "environmentVariables": env_vars,
            }
            if authorizer_config:
                create_kwargs["authorizerConfiguration"] = authorizer_config
            runtime_arn, runtime_id = self._create_runtime_conflict_retry(
                control, runtime_name, create_kwargs)
            retry, vpc_subnets, reason = self._poll_new_runtime(
                control, runtime_id, vpc_subnets, attempt)
            if not retry:
                break  # READY
            runtime_arn = None
            last_reason = reason
            if "availability zone" not in reason.lower():
                delay = RUNTIME_CREATE_RETRY_DELAYS_S[
                    min(attempt, len(RUNTIME_CREATE_RETRY_DELAYS_S) - 1)]
                logger.warning(
                    f"Runtime '{runtime_name}' create attempt {attempt + 1} of "
                    f"{RUNTIME_CREATE_ATTEMPTS} failed ({reason}); retrying in {delay}s")
                time.sleep(delay)  # deliberate pacing/backoff delay in a bounded retry loop  # nosemgrep: arbitrary-sleep
        if not runtime_arn:
            raise RuntimeError(
                f"Runtime '{runtime_name}' could not be created after "
                f"{RUNTIME_CREATE_ATTEMPTS} attempts. Last failure: {last_reason}"
            )
        return runtime_arn, runtime_id

    def _create_runtime_conflict_retry(self, control, runtime_name: str,
                                       create_kwargs: dict) -> tuple:
        """``create_agent_runtime`` with ConflictException retries.

        A conflict means a runtime with this name still exists (e.g. stuck in
        DELETING from a previous offboard) — wait for it to disappear, then
        retry (3 attempts). Returns ``(runtime_arn, runtime_id)``."""
        for create_attempt in range(3):
            try:
                resp = control.create_agent_runtime(**create_kwargs)
                return resp["agentRuntimeArn"], resp["agentRuntimeId"]
            except Exception as ce:
                if "ConflictException" not in type(ce).__name__ and "already exists" not in str(ce).lower():
                    raise
                if create_attempt >= 2:
                    raise RuntimeError(
                        f"Runtime '{runtime_name}' still exists after waiting — cannot create. "
                        f"Try offboarding the system first."
                    ) from ce
                logger.warning(f"ConflictException creating runtime '{runtime_name}' (attempt {create_attempt+1}) — waiting for old runtime to be deleted...")
                self._wait_for_stale_runtime_deletion(control, runtime_name)

    def _wait_for_stale_runtime_deletion(self, control, runtime_name: str) -> None:
        """Find the conflicting same-name runtime and wait for it to finish
        deleting (best-effort — a plain wait when it cannot be listed)."""
        try:
            for rt in list_all_agent_runtimes(control):
                if rt.get("agentRuntimeName") == runtime_name:
                    stale_id = rt["agentRuntimeId"]
                    logger.info(f"Found stale runtime {stale_id} in state {rt.get('status', '')} — waiting up to 120s for deletion")
                    if self._wait_runtime_deleted(
                            control, stale_id,
                            poll_log=f"Stale runtime {stale_id} still exists, waiting..."):
                        logger.info(f"Stale runtime {stale_id} confirmed deleted — retrying create")
                    break
            else:
                # Not found in list — may be in a transient state; just wait.
                logger.info("Stale runtime not found in list — waiting 15s before retry")
                time.sleep(15)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        except Exception as list_err:
            logger.warning(f"Failed to list runtimes while waiting: {list_err}")
            time.sleep(15)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

    def _poll_new_runtime(self, control, runtime_id: str, vpc_subnets: list,
                          attempt: int) -> tuple:
        """Poll a just-created runtime until READY (max
        ``RUNTIME_READY_TIMEOUT_S``; a cold account takes about five minutes).

        On CREATE_FAILED the failed runtime is deleted so its name is free,
        then: a reason naming unsupported-AZ subnet(s) signals a retry with
        those subnets stripped; any other reason signals a plain retry of the
        same create. On the last attempt the failure is raised instead.
        Returns ``(retry, remaining_subnets, failure_reason)``."""
        deadline = time.time() + RUNTIME_READY_TIMEOUT_S
        last_attempt = attempt >= RUNTIME_CREATE_ATTEMPTS - 1
        while True:
            status_resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
            status = status_resp.get("status", "")
            if status == "READY":
                return False, vpc_subnets, ""
            if status in RUNTIME_FAILED_STATES:
                reason = status_resp.get("failureReason", "")
                bad_subnets = (
                    set(re.findall(r"subnet-[0-9a-fA-F]+", reason))
                    if "availability zone" in reason.lower()
                    else set()
                )
                remaining = [s for s in vpc_subnets if s not in bad_subnets]
                if bad_subnets and remaining:
                    _UNSUPPORTED_SUBNETS.update(bad_subnets)  # remembered for every later create
                if bad_subnets and not remaining:
                    raise RuntimeError(
                        f"Runtime {runtime_id} failed: every offered subnet is in an "
                        f"unsupported AZ. Reason: {reason}")
                if last_attempt:
                    raise RuntimeError(f"Runtime {runtime_id} failed: {reason}")
                if bad_subnets:
                    logger.warning(
                        f"Runtime {runtime_id} CREATE_FAILED in unsupported AZ(s) — stripping "
                        f"{sorted(bad_subnets)}, retrying with {remaining}. Reason: {reason}"
                    )
                else:
                    logger.warning(
                        f"Runtime {runtime_id} {status} (transient, will recreate). Reason: {reason}"
                    )
                self._delete_failed_runtime(control, runtime_id)
                return True, remaining, reason
            if time.time() > deadline:
                raise TimeoutError(
                    f"Runtime {runtime_id} not READY after {RUNTIME_READY_TIMEOUT_S}s (status={status})")
            logger.info(f"Waiting for runtime {runtime_id} (status={status})...")
            time.sleep(5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

    def _delete_failed_runtime(self, control, runtime_id: str) -> None:
        """Delete a CREATE_FAILED/UPDATE_FAILED runtime and wait for it to go,
        so a recreate under the same name does not hit ConflictException.
        Best-effort: the create path's own conflict handling covers a miss."""
        try:
            control.delete_agent_runtime(agentRuntimeId=runtime_id)
            self._wait_runtime_deleted(control, runtime_id)
        except Exception as del_err:
            logger.warning(f"Failed to delete failed runtime {runtime_id}: {del_err}")

    def _upsert_and_sync_gateway_target(self, control, region: str,
                                        system_id: str, system_name: str,
                                        runtime_mcp_url: str, *,
                                        gateway_id: str | None = None) -> str:
        """Ensure the OAuth2 credential provider (gateway outbound auth), then
        upsert + sync the gateway target for this runtime on ``gateway_id``
        (default: the manager's shared gateway). Returns the target id ("" when
        no gateway is configured)."""
        gateway_id = self.gateway_id if gateway_id is None else gateway_id
        credential_provider_arn = self._ensure_credential_provider(control, region)
        # M2M scopes for the gateway to request.
        m2m_scopes = ["mcp/tools.read", "mcp/tools.execute"] if credential_provider_arn else []
        if not gateway_id:
            return ""
        target_name = self._gateway_target_name(system_id)
        target_config = {"mcp": {"mcpServer": {"endpoint": runtime_mcp_url}}}
        cred_config = None
        if credential_provider_arn and m2m_scopes:
            cred_config = [{
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": credential_provider_arn,
                        "scopes": m2m_scopes,
                        "grantType": "CLIENT_CREDENTIALS",
                    }
                },
            }]
        create_target_kwargs = {
            "gatewayIdentifier": gateway_id,
            "name": target_name,
            "description": f"System agent for {system_name}",
            "targetConfiguration": target_config,
        }
        if cred_config:
            create_target_kwargs["credentialProviderConfigurations"] = cred_config
        gateway_target_id, needs_sync = self._create_or_reuse_gateway_target(
            control, target_name, create_target_kwargs)
        if gateway_target_id and needs_sync:
            self._sync_gateway_target(control, gateway_target_id, target_name,
                                      gateway_id=gateway_id)
        return gateway_target_id

    def _create_or_reuse_gateway_target(self, control, target_name: str,
                                        create_target_kwargs: dict) -> tuple:
        """Create the gateway target; on ConflictException reuse a READY one
        that already matches this provision, update a READY one that does not
        (see :meth:`_reuse_or_align_gateway_target`), or delete-and-recreate a
        non-READY one. Returns ``(target_id, needs_sync)`` — only newly
        (re)created or updated targets need a sync. The gateway addressed is
        ``create_target_kwargs["gatewayIdentifier"]`` throughout."""
        gateway_id = create_target_kwargs["gatewayIdentifier"]
        try:
            tgt_resp = control.create_gateway_target(**create_target_kwargs)
            return tgt_resp.get("targetId", ""), True
        except control.exceptions.ConflictException:
            logger.info(f"Gateway target '{target_name}' already exists — checking state")
        for t in list_all_gateway_targets(control, gateway_id):
            if t.get("name") != target_name:
                continue
            gateway_target_id = t.get("targetId", "")
            target_status = t.get("status", "")
            if target_status != "READY":
                return self._recreate_gateway_target(
                    control, gateway_target_id, target_name, target_status,
                    create_target_kwargs)
            return self._reuse_or_align_gateway_target(
                control, gateway_target_id, target_name, create_target_kwargs)
        return "", False

    def _recreate_gateway_target(self, control, gateway_target_id: str, target_name: str,
                                 target_status: str, create_target_kwargs: dict) -> tuple:
        """Delete a non-READY target, wait for it to go, and create it again.
        Returns ``(target_id, True)``.

        Never hands back a target that is not there: when the delete went
        through and the create fails, it raises
        :class:`GatewayTargetRecreateError` (nothing is registered; a retry
        finds the name free, or the target still non-READY, and runs again).
        When the delete itself fails, the old target is synced as before only
        if it still exists, is not DELETING and carries a readable MCP
        endpoint (:meth:`_keep_undeletable_target`); otherwise the same error
        is raised."""
        gateway_id = create_target_kwargs["gatewayIdentifier"]
        logger.info(f"Gateway target '{target_name}' in state {target_status} — deleting and recreating")
        try:
            control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=gateway_target_id)
        except Exception as delete_err:
            self._keep_undeletable_target(control, gateway_target_id, target_name,
                                          delete_err, create_target_kwargs)
            return gateway_target_id, True  # verified in line; sync it
        for _wait in range(20):  # wait for delete to complete
            time.sleep(3)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            try:
                control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=gateway_target_id)
            except Exception:
                break  # deleted
        try:
            tgt_resp = control.create_gateway_target(**create_target_kwargs)
        except Exception as create_err:
            raise GatewayTargetRecreateError(
                f"gateway target '{target_name}' was deleted (it was {target_status}) but "
                f"could not be created again: {create_err}") from create_err
        gateway_target_id = tgt_resp.get("targetId", "")
        logger.info(f"Gateway target '{target_name}' recreated: {gateway_target_id}")
        return gateway_target_id, True

    def _keep_undeletable_target(self, control, gateway_target_id: str, target_name: str,
                                 delete_err: Exception, create_target_kwargs: dict) -> None:
        """The delete of a non-READY target failed. Return (keep the old
        target; the caller syncs it) only when it still exists, is not
        DELETING, has a readable MCP endpoint AND matches this provision —
        either already (``gateway_target_drift`` empty) or after the verified
        update path brings it in line. Otherwise raise
        :class:`GatewayTargetRecreateError`: a drifted target is never synced
        or registered unchanged."""
        gateway_id = create_target_kwargs["gatewayIdentifier"]
        try:
            detail = control.get_gateway_target(gatewayIdentifier=gateway_id,
                                                targetId=gateway_target_id)
        except Exception as get_err:
            raise GatewayTargetRecreateError(
                f"could not delete gateway target '{target_name}' ({delete_err}) and "
                f"could not read it back ({get_err})") from delete_err
        status = detail.get("status", "")
        if status == "DELETING" or gateway_target_endpoint(detail) is None:
            raise GatewayTargetRecreateError(
                f"could not delete gateway target '{target_name}' ({delete_err}) and it is "
                f"{'DELETING' if status == 'DELETING' else 'without a readable endpoint'}"
            ) from delete_err
        drift = gateway_target_drift(create_target_kwargs, detail)
        if not drift:
            logger.warning(f"Failed to delete gateway target '{target_name}': {delete_err} — it "
                           f"still exists ({status}) and matches this runtime; syncing it")
            return
        logger.warning(f"Failed to delete gateway target '{target_name}': {delete_err} — it "
                       f"does not match this runtime (differs: {', '.join(drift)}); "
                       f"updating it in place")
        try:
            bring_gateway_target_in_line(
                control, gateway_target_id, create_target_kwargs, detail,
                sleep=time.sleep, poll_seconds=self._TARGET_UPDATE_POLL_SECONDS,
                max_polls=self._TARGET_UPDATE_POLLS)
        except GatewayTargetUpdateError as update_err:
            raise GatewayTargetRecreateError(
                f"could not delete gateway target '{target_name}' ({delete_err}) and could "
                f"not bring it in line with this runtime: {update_err}") from update_err

    #: Settle budget for an updated target before its sync (polls x seconds).
    _TARGET_UPDATE_POLLS = 24
    _TARGET_UPDATE_POLL_SECONDS = 5.0

    def _reuse_or_align_gateway_target(self, control, gateway_target_id: str,
                                       target_name: str, create_target_kwargs: dict) -> tuple:
        """A READY target is reused unchanged ONLY when it matches what this
        provision would create (``gateway_target_drift``: its MCP endpoint is
        this runtime's, its outbound credential config is this one). A target
        whose runtime was deleted and recreated also reads READY while still
        pointing at the old runtime, so a mismatch is updated in place and
        synced. Returns ``(target_id, needs_sync)``. Raises
        :class:`GatewayTargetUpdateError` when the target cannot be read or
        the update does not take; the target is never deleted here, and the
        caller registers nothing, so a retry runs the same check again."""
        gateway_id = create_target_kwargs["gatewayIdentifier"]
        try:
            detail = control.get_gateway_target(gatewayIdentifier=gateway_id,
                                                targetId=gateway_target_id)
        except Exception as e:
            raise GatewayTargetUpdateError(
                f"could not read gateway target '{target_name}' to confirm it points "
                f"at this runtime: {e}") from e
        drift = gateway_target_drift(create_target_kwargs, detail)
        if not drift:
            logger.info(f"Gateway target '{target_name}' is READY and matches this runtime — reusing")
            return gateway_target_id, False
        old = ((detail.get("targetConfiguration") or {}).get("mcp") or {}).get("mcpServer") or {}
        logger.warning(
            f"Gateway target '{target_name}' is READY but does not match this runtime "
            f"(differs: {', '.join(drift)}; endpoint was {old.get('endpoint')!r}) — "
            f"updating it in place, then syncing")
        bring_gateway_target_in_line(
            control, gateway_target_id, create_target_kwargs, detail,
            sleep=time.sleep, poll_seconds=self._TARGET_UPDATE_POLL_SECONDS,
            max_polls=self._TARGET_UPDATE_POLLS)
        logger.info(f"Gateway target '{target_name}' updated to this runtime")
        return gateway_target_id, True

    def _sync_gateway_target(self, control, gateway_target_id: str,
                             target_name: str, *,
                             gateway_id: str | None = None) -> None:
        """Sync a newly (re)created gateway target, with retries.

        Waits 30s first: the target must leave Creating state and the gateway
        needs the runtime to respond within 20s during sync."""
        gateway_id = self.gateway_id if gateway_id is None else gateway_id
        logger.info("Waiting 30s for runtime to warm up before gateway sync...")
        time.sleep(30)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        for attempt in range(10):
            try:
                control.synchronize_gateway_targets(
                    gatewayIdentifier=gateway_id,
                    targetIdList=[gateway_target_id],
                )
                logger.info(f"Gateway target {target_name} synced successfully")
                break
            except Exception as sync_err:
                if "Creating state" in str(sync_err) and attempt < 9:
                    logger.info(f"Gateway target still creating, retrying sync in 5s (attempt {attempt+1})")
                    time.sleep(5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
                elif "SYNCHRONIZE_UNSUCCESSFUL" in str(sync_err) or "unsuccessful" in str(sync_err).lower():
                    # Runtime not responding — wait longer and retry.
                    if attempt < 9:
                        logger.info(f"Sync unsuccessful, waiting 15s before retry (attempt {attempt+1})")
                        time.sleep(15)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
                    else:
                        logger.warning(f"Gateway target sync failed after retries: {sync_err}")
                        break
                else:
                    logger.warning(f"Gateway target sync failed: {sync_err}")
                    break

    def resync_cloud_connection(
        self,
        system_id: str,
        connection_type: str,
        connection_config: dict | None = None,
        conn_params: dict | None = None,
    ) -> bool:
        """Re-persist a changed downstream connection and force the runtime to pick it up.

        A ``PUT /api/admin/connection/{id}`` reconnect in gateway mode cannot rely on
        :meth:`start_agent` to refresh a system agent's downstream config: when a READY
        gateway target already exists (the steady state after deploy) ``start_agent``
        takes the fast path and returns the gateway URL WITHOUT calling
        :meth:`_start_cloud`, so the ``adt/agent-config/<id>`` secret is
        never rewritten. And even when rewritten, a running AgentCore runtime resolves
        its connection exactly ONCE at container start
        (``runtime_app._run_system_agent`` -> ``system_agent.create_mcp_server``) and
        never re-reads it. This method closes BOTH gaps:

          1. Rewrite ``adt/agent-config/<id>`` with the freshly resolved
             config (the runtime reads it via ``CONNECTION_SECRET_ARN``).
          2. Call ``update_agent_runtime`` on the existing ``adt_<id>`` runtime to deploy
             a new version — the replacement container re-reads the rewritten secret.

        Best-effort: returns ``True`` only when BOTH the secret rewrite AND the runtime
        update succeed; returns ``False`` (never raises) otherwise, so the caller can
        surface a truthful ``reconnected=False`` instead of a 500. Secret VALUES are
        never logged — only the non-sensitive secret ARN and exception type.
        """
        region = os.environ.get("AWS_REGION", "us-east-1")
        effective_config = self._resync_connection_config(
            system_id, connection_type, connection_config, conn_params)
        if effective_config is None:
            return False
        with RUNTIME_CONFIG_LOCK:  # graph read, secret rewrite, runtime bump
            # The whole-value rewrite would DROP delivered business context
            # (ADR-0054 D4) and the generated prompt keys; re-include both
            # (``_carry_forward_generated_keys``) before serializing.
            effective_config = self._carry_forward_generated_keys(
                system_id, connection_type,
                self._apply_business_context(system_id, effective_config, region), region)
            connection_secret_arn = self._resync_rewrite_secret(
                system_id, effective_config, region)
            if connection_secret_arn is None:
                return False
            # Deploy a new runtime version: the replacement container
            # re-reads CONNECTION_SECRET_ARN at start.
            return self._resync_force_runtime_pickup(
                system_id, connection_secret_arn, region)

    def _resync_connection_config(self, system_id: str, connection_type: str,
                                  connection_config: dict | None,
                                  conn_params: dict | None) -> dict | None:
        """The re-synced runtime's connection config
        (:func:`system_connection_config`, schema from the config or the
        registered agent); ``None`` when a demo schema has no read-only login,
        so the owner's is never written."""
        schema = str((connection_config or {}).get("schema") or "")
        if not schema and system_id in self._agents:
            schema = self._agents[system_id].schema
        try:
            return system_connection_config(schema, connection_type, connection_config, conn_params)
        except SystemReaderLoginError as exc:
            logger.warning("resync_cloud_connection: %s not refreshed: %s", system_id, exc)
            return None

    #: agent-config keys onboarding generated for the system's own tables
    #: (prompt, column list, concept mappings, the measured different-identifier
    #: records the graph holds for them, the fields whose recorded type says
    #: what they hold), not part of its connection.
    GENERATED_AGENT_CONFIG_KEYS = ("system_prompt", "tables_desc", "concept_mappings",
                                   "distinct_identifiers", "field_descriptions")

    @classmethod
    def _carried_keys(cls, connection_type: str) -> tuple:
        """The current agent-config keys a same-target resync keeps: the
        generated keys and, for Athena, the admin reliability settings
        (``connection_resolver.ATHENA_ADMIN_CONFIG_KEYS``: poll bounds,
        exact-split caps), which an agent-config written before its System
        node held them carries alone. A setting an admin route manages on the
        node (``NODE_MANAGED_ADMIN_KEYS``: the scan budget) is not carried:
        the resolved connection holds the node's value, and a cleared one
        must stay cleared."""
        if connection_type != ATHENA:
            return cls.GENERATED_AGENT_CONFIG_KEYS
        from .connection_resolver import ATHENA_ADMIN_CONFIG_KEYS, NODE_MANAGED_ADMIN_KEYS
        return (*cls.GENERATED_AGENT_CONFIG_KEYS,
                *(k for k in ATHENA_ADMIN_CONFIG_KEYS if k not in NODE_MANAGED_ADMIN_KEYS))

    def _carry_forward_generated_keys(self, system_id: str, connection_type: str,
                                      cfg: dict, region: str) -> dict:
        """``cfg`` plus what the whole-value resync rewrite would otherwise
        drop: the carried keys of a same-target edit
        (:meth:`_carried_same_target_keys`), and for EVERY connection type the
        described-field record rebuilt from the graph's ``Field`` types
        (``field_descriptions.rebuilt``; when the graph cannot be read, the
        record already carried or the current agent-config's)."""
        from . import field_descriptions
        carried = self._carried_same_target_keys(system_id, connection_type, cfg, region)
        def current():
            return self.current_agent_config(
                system_id, client_config=field_descriptions.fallback_client_config())
        return field_descriptions.rebuilt(system_id, carried, current=current)

    def _carried_same_target_keys(self, system_id: str, connection_type: str,
                                  cfg: dict, region: str) -> dict:
        """``cfg`` plus the carried keys (:meth:`_carried_keys`) of the
        CURRENT agent-config secret, for a system whose re-synced connection
        still reads the same tables (:func:`_same_data_target`: for
        PostgreSQL the same host and database; for Athena the same region,
        catalog, database and role ARN, so a workgroup edit keeps them).

        ``resolve_connection`` carries none of the generated keys, so the
        whole-value rewrite used to drop them: the agent then lost its baked
        table list, prompt and concept mappings until a re-onboard, and every
        check read from the baked table list (state times, storage bounds)
        went inactive; an admin setting held only by the secret fell back to
        its default. Only the carried keys are copied, never a login or
        pointer, and never over a value ``cfg`` already holds. Another data
        target, another connection type, or an unreadable secret keeps
        ``cfg`` as it is. Values are never logged."""
        if connection_type not in ("PostgreSQL", ATHENA):
            return cfg
        try:
            sm = boto3.client("secretsmanager", region_name=region)
            current = json.loads(sm.get_secret_value(
                SecretId=f"adt/agent-config/{system_id}").get("SecretString") or "{}")
        except Exception as e:  # noqa: BLE001 - nothing to carry forward
            logger.info("resync_cloud_connection: no current agent-config for %s (%s)",
                        system_id, type(e).__name__)
            return cfg
        if not isinstance(current, dict) or not _same_data_target(connection_type, current, cfg):
            return cfg
        out = dict(cfg)
        for key in self._carried_keys(connection_type):
            if key not in out and current.get(key) not in (None, ""):
                out[key] = current[key]
        return out

    def _resync_rewrite_secret(self, system_id: str, effective_config: dict,
                               region: str) -> str | None:
        """Resync step 1: rewrite ``adt/agent-config/<id>`` with the fresh
        config, fitted to the secret size bound. Returns the secret ARN, or
        ``None`` on failure. Secret VALUES are never logged, only the
        non-sensitive ARN and exception type."""
        secret_name = f"adt/agent-config/{system_id}"
        try:
            sm = boto3.client("secretsmanager", region_name=region)
            secret_value = _agent_config_secret_value(effective_config, label=system_id)
            try:
                resp = sm.create_secret(Name=secret_name, SecretString=secret_value)
                arn = resp["ARN"]
            except sm.exceptions.ResourceExistsException:
                sm.put_secret_value(SecretId=secret_name, SecretString=secret_value)
                arn = sm.describe_secret(SecretId=secret_name)["ARN"]
            # Never log secret_value — only the (non-sensitive) ARN.
            logger.info("resync_cloud_connection: %s (arn=%s): agent-config secret rewritten",  # logs the secret's ARN only, never its value
                        system_id, arn)
            return arn
        except Exception as e:  # noqa: BLE001 - best-effort; surface as None
            logger.warning("resync_cloud_connection: %s: %s while rewriting the "
                           "agent-config secret", system_id, type(e).__name__)
            return None

    def _resync_force_runtime_pickup(self, system_id: str,
                                     connection_secret_arn: str,
                                     region: str) -> bool:
        """Resync step 2: deploy a new version of the existing ``adt_<id>``
        runtime so the replacement container re-reads the rewritten secret
        (its ARN injected as ``CONNECTION_SECRET_ARN``). ``False`` when the
        runtime is missing or the update fails — never raises."""
        runtime_name = self._runtime_name(system_id)
        try:
            control = boto3.client("bedrock-agentcore-control", region_name=region)
            runtime_id = None
            for rt in list_all_agent_runtimes(control):
                if rt.get("agentRuntimeName") == runtime_name:
                    runtime_id = rt.get("agentRuntimeId")
                    break
            if not runtime_id:
                logger.warning("resync_cloud_connection: no runtime %s found — secret "
                               "rewritten but cannot force pickup", runtime_name)
                return False
            details = control.get_agent_runtime(agentRuntimeId=runtime_id)
            env_vars = dict(details.get("environmentVariables", {}))
            if connection_secret_arn:
                env_vars["CONNECTION_SECRET_ARN"] = connection_secret_arn
            control.update_agent_runtime(
                **self._runtime_update_kwargs(runtime_id, details, env_vars))
            logger.info("resync_cloud_connection: deployed new version of runtime %s — "
                        "container will re-read the updated config on restart",
                        runtime_name)
            return True
        except Exception as e:  # noqa: BLE001 - best-effort; surface as False
            logger.warning("resync_cloud_connection: %s: runtime update failed (%s) after the "  # logs the exception type only, never a value
                           "secret was rewritten", system_id, type(e).__name__)
            return False

    @staticmethod
    def _runtime_update_kwargs(runtime_id: str, details: dict,
                               env_vars: dict) -> dict:
        """Build ``update_agent_runtime`` kwargs from ``get_agent_runtime``
        details. Thin delegate to the ONE shared builder
        (:func:`agents.runtime_provision_helpers.runtime_update_kwargs`), which
        round-trips ``protocolConfiguration`` (full-replacement API — omitting
        it downgrades MCP runtimes to HTTP, ADR-0056), drops the immutable
        ``requireServiceS3Endpoint``, and passes ``authorizerConfiguration``
        only when a ``customJWTAuthorizer`` is actually configured."""
        return _shared_runtime_update_kwargs(runtime_id, details, env_vars)

    def refresh_business_context(self, system_id: str) -> str:
        """Deliver the System node's business context to the RUNNING runtime.

        READ-MODIFY-WRITE merge into the ``adt/agent-config/<id>`` secret
        (sibling keys — system_prompt / tables_desc / concept_mappings /
        connection creds — preserved), then ``update_agent_runtime`` deploys a
        new version whose replacement container re-reads the secret at start
        (~1-2 min) — no reonboard needed (ADR-0054 D4).

        Returns ``"ok"`` only when secret merge AND runtime update succeed;
        ``"skipped"`` = local mode, no agent-config secret, or no cloud
        runtime; ``"failed"`` otherwise (never raises). Local mode is NOT a
        delivery gap: ``_start_local`` includes graph truth in the agent's
        config, so a restart/re-onboard picks up saved context. Secret values
        and the context text are never logged (length + hash only).
        """
        if self.mode != "cloud":
            logger.info("refresh_business_context: %s skipped (mode=%s)",
                        system_id, self.mode)
            return "skipped"
        region = os.environ.get("AWS_REGION", "us-east-1")
        from . import business_context as _bc
        with RUNTIME_CONFIG_LOCK:  # graph read, secret merge, runtime bump
            try:
                state = _bc.read_business_context(system_id)
            except Exception as e:  # noqa: BLE001 - best-effort; surface as failed
                logger.warning("refresh_business_context: graph read failed for %s: %s",
                               system_id, type(e).__name__)
                return "failed"
            if state is None:
                # Unreadable ≠ unset (ADR-0054 D4): a Neptune blip must NOT be
                # mistaken for "no context set" — NO secret mutation, no
                # runtime bump; report the refresh truthfully as failed.
                logger.warning("refresh_business_context: graph UNREADABLE for %s "
                               "— no secret mutation", system_id)
                return "failed"
            early = self._merge_business_context_secret(system_id, state, region)
            if early:
                return early
            return self._bump_runtime_version(system_id, region)

    @staticmethod
    def _merged_agent_config_payload(secret_string: str, state: dict | None) -> str:
        """Pure RMW merge (ADR-0054 D4): parse the CURRENT agent-config secret
        JSON and set or remove ONLY the ``business_context`` key (present iff
        enabled and non-empty), preserving every sibling key byte-for-byte.
        NEVER copy :meth:`resync_cloud_connection`'s whole-value overwrite
        semantics here — that would wipe the prompt keys (the exact bug class
        D4 warns about). The one exception is size: a merged value over the
        secret bound has its concept mappings trimmed to fit
        (:func:`agent_config_secret_value`), as every writer does."""
        payload = json.loads(secret_string or "{}")
        if not isinstance(payload, dict):
            raise ValueError("agent-config secret is not a JSON object")
        value = _business_context_secret_value(state)
        if value is None:
            payload.pop("business_context", None)
        else:
            payload["business_context"] = value
        return _agent_config_secret_value(payload)

    def _merge_business_context_secret(self, system_id: str, state: dict,
                                       region: str) -> str | None:
        """READ-MODIFY-WRITE the agent-config secret's ``business_context`` key.

        Returns ``"skipped"`` (system has no agent-config secret — e.g. never
        provisioned), ``"failed"`` (any other error), or ``None`` on success.
        Values are never logged — only length + hash.
        """
        secret_name = f"adt/agent-config/{system_id}"
        try:
            sm = boto3.client("secretsmanager", region_name=region)
            not_found = getattr(sm.exceptions, "ResourceNotFoundException", ())
            try:
                current = sm.get_secret_value(SecretId=secret_name)
            except not_found:
                logger.info("refresh_business_context: %s has no agent-config "
                            "secret — skipped", system_id)
                return "skipped"
            merged = self._merged_agent_config_payload(
                current.get("SecretString", ""), state)
            sm.put_secret_value(SecretId=secret_name, SecretString=merged)
            from .business_context import context_hash as _hash
            logger.info("refresh_business_context: %s (enabled=%s len=%d hash=%s): "  # logs flags, a length and a hash only, never the value
                        "business-context secret merged", system_id,
                        bool(state.get("enabled")),
                        len(state.get("business_context") or ""),
                        _hash(state.get("business_context") or ""))
            return None
        except Exception as e:  # noqa: BLE001 - best-effort; surface as failed
            logger.warning("refresh_business_context: %s: %s while merging the "
                           "secret", system_id, type(e).__name__)
            return "failed"

    def current_agent_config(self, system_id: str, client_config=None) -> dict | None:
        """The CURRENT ``adt/agent-config/<id>`` secret, as its JSON object:
        the carry-forward source of a startup restore for a system provisioned
        before the graph held its measured different-identifier pairs
        (``server._with_carried_forward_identifiers``). ``None`` in local mode,
        when there is no secret, or when it is not a JSON object. Any other
        read error raises :class:`AgentConfigUnreadable` (logged by type;
        values are never logged): "no secret" and "could not read it" must
        stay apart, because a restore that rewrites the secret after a failed
        read would drop whatever guard it held. ``client_config`` (a botocore
        ``Config``) bounds the read's client; the SDK defaults without it."""
        if self.mode != "cloud":
            return None
        region = os.environ.get("AWS_REGION", "us-east-1")
        try:
            sm = boto3.client("secretsmanager", region_name=region,
                              **({"config": client_config} if client_config else {}))
            raw = sm.get_secret_value(
                SecretId=f"adt/agent-config/{system_id}").get("SecretString") or "{}"
        except Exception as e:  # noqa: BLE001 - classified below
            code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                return None
            logger.warning("current agent-config of %s could not be read: %s",
                           system_id, type(e).__name__)
            raise AgentConfigUnreadable(system_id) from e
        try:
            current = json.loads(raw)
        except ValueError:
            return None
        return current if isinstance(current, dict) else None

    def takes_fast_path(self, system_id: str, gateway_id: str | None = None,
                        gateway_url: str | None = None) -> bool:
        """Whether :meth:`start_agent` would register ``system_id`` by the
        cloud fast path (a READY target backed by a READY runtime), which
        writes no agent-config secret. Same condition and defaults as
        ``start_agent``."""
        if self.mode != "cloud":
            return False
        gateway_id = self.gateway_id if gateway_id is None else gateway_id
        gateway_url = self.gateway_url if gateway_url is None else gateway_url
        return bool(gateway_url) and self._cloud_fast_path_ready(system_id, gateway_id)

    def _existing_secret_business_context(self, system_id: str,
                                          region: str) -> str | None:
        """Best-effort read of the CURRENT agent-config secret's
        ``business_context`` value — the carry-forward source when the graph
        is unreadable (ADR-0054 D4: absent-because-unreadable must never drop
        delivered steward content). ``None`` when there is no secret, no key,
        or any error. Values are never logged."""
        try:
            sm = boto3.client("secretsmanager", region_name=region)
            current = sm.get_secret_value(SecretId=f"adt/agent-config/{system_id}")
            payload = json.loads(current.get("SecretString") or "{}")
            value = payload.get("business_context") if isinstance(payload, dict) else None
            if isinstance(value, str) and value.strip():
                return value
        except Exception as e:  # noqa: BLE001 - best-effort carry-forward
            logger.warning("business_context: carry-forward read failed for %s: %s",
                           system_id, type(e).__name__)
        return None

    def _apply_business_context(self, system_id: str, cfg: dict,
                                region: str) -> dict:
        """Set/remove ``cfg['business_context']`` from GRAPH TRUTH — the one
        include rule (ADR-0054 D4) — for the whole-payload secret writers
        (provision ``_start_cloud`` and ``resync_cloud_connection``), so no
        code path can silently lose steward content. An UNREADABLE graph
        (``read_business_context`` → ``None``, or a raise) never drops the
        key: the existing secret's delivered value is carried forward."""
        from . import business_context as _bc
        try:
            state = _bc.read_business_context(system_id)
        except Exception as e:  # noqa: BLE001 - unreadable, NOT unset
            logger.warning("business_context: graph read raised for %s: %s",
                           system_id, type(e).__name__)
            state = None
        cfg = dict(cfg)
        cfg.pop("business_context", None)  # graph truth only — never a stale copy
        if state is not None:
            value = _business_context_secret_value(state)
            if value is not None:
                cfg["business_context"] = value
            return cfg
        carried = self._existing_secret_business_context(system_id, region)
        if carried is not None:
            cfg["business_context"] = carried
        return cfg

    def refresh_agent_model(self, system_id: str, model_id: str) -> str:
        """Point a RUNNING system-agent runtime at a different model.

        ``SYSTEM_AGENT_MODEL`` is normally baked into the runtime env at
        provision, so changing the agent model used to require a full
        re-onboard. This updates the env in place and deploys a new runtime
        version instead — the same seam the business-context refresh uses.
        Returns ``"ok"`` | ``"failed"`` | ``"skipped"`` (local mode / no cloud
        runtime). Never raises: the caller reports the outcome honestly.
        """
        if getattr(self, "mode", "") != "cloud":
            return "skipped"
        region = os.environ.get("AWS_REGION", "us-east-1")
        return self._bump_runtime_version(
            system_id, region, env_overrides={"SYSTEM_AGENT_MODEL": model_id})

    def _bump_runtime_version(self, system_id: str, region: str,
                              env_overrides: dict | None = None) -> str:
        """Deploy a new version of the existing ``adt_<id>`` runtime.

        Used for two live-update paths that both avoid a re-onboard: the
        replacement container re-reads the agent-config secret at start (the
        business-context path, no overrides), and ``env_overrides`` can change
        runtime env in place (the agent-MODEL path — ``SYSTEM_AGENT_MODEL`` is
        otherwise only set at provision time). Existing env is PRESERVED and
        overrides merged on top. ``"skipped"`` when no runtime exists;
        ``"failed"`` on any error; else ``"ok"``. The read and the write run
        under ``RUNTIME_CONFIG_LOCK``."""
        try:
            control = boto3.client("bedrock-agentcore-control", region_name=region)
            runtime_name = self._runtime_name(system_id)
            with RUNTIME_CONFIG_LOCK:
                runtime_id = _find_runtime_id(control, runtime_name)
                if not runtime_id:
                    logger.info("runtime bump: no runtime %s — skipped", runtime_name)
                    return "skipped"
                details = control.get_agent_runtime(agentRuntimeId=runtime_id)
                existing_env = dict(details.get("environmentVariables", {}))
                if env_overrides and all(existing_env.get(k) == v
                                         for k, v in env_overrides.items()):
                    return "unchanged"  # already set: no bump; drift self-corrects
                # Shared builder: round-trips protocolConfiguration (full-
                # replacement API — omitting it bricked MCP runtimes, ADR-0056),
                # drops the immutable requireServiceS3Endpoint, and re-sends the
                # authorizer only when actually configured.
                update_kwargs = self._runtime_update_kwargs(
                    runtime_id, details, {**existing_env, **(env_overrides or {})})
                control.update_agent_runtime(**update_kwargs)
            logger.info("runtime bump: deployed new version of runtime %s", runtime_name)
            return "ok"
        except Exception as e:  # noqa: BLE001 - best-effort; surface as failed
            logger.warning("runtime bump: update failed for %s: %s", system_id, type(e).__name__)
            return "failed"

    # ------------------------------------------------------------------
    # T1.5 — batched serial runtime-roll coordinator (G2)
    # ------------------------------------------------------------------

    #: Bound on ONE whole batched operation (seconds). Callers behind the
    #: CloudFront 120 s origin cap (the BC PUT) pass a smaller bound.
    BATCH_REFRESH_TIMEOUT = 900
    #: Readiness-gate poll cadence (seconds) — same block-until-READY
    #: discipline as scripts/reonboard.py, reusing _runtime_is_ready.
    READY_POLL_SECONDS = 10

    def refresh_systems_batched(self, system_ids, reason: str = "", *,
                                artifact_classes=("business_context",),
                                timeout_seconds: int | None = None,
                                refresh_fn=None) -> dict:
        """ONE batched, deduped, SERIAL runtime-roll coordinator (T1.5 / G2).

        Both business-context writes and (later, T2.2) domain-pack installs
        route runtime rolls through here so a steward text edit can never
        take the whole data plane down: (a) DEDUPE — at most one roll per
        system per operation, first-seen order kept; (b) the pinned NO-ROLL
        rule — when no entry in *artifact_classes* requires a roll
        (:func:`artifact_class_requires_roll`), every system reports
        ``skipped`` and ZERO runtime updates happen; (c) SERIAL readiness
        gate + one warm-up MCP probe per rolled system (see
        :meth:`_roll_one_system`) — never two runtimes down at once;
        (d) BOUNDED by *timeout_seconds* with a clear partial report.

        Returns ``{system_id: {"status": "ok"|"failed"|"skipped",
        "readiness": "ready_verified"|"ready_unverified"|"not_ready"|None,
        "detail": str|None}}`` — the ADR-0054 D4 tri-state unchanged; a
        refresh failure never rolls back the caller's durable graph write.
        *refresh_fn* defaults to :meth:`refresh_business_context`.
        """
        deduped = list(dict.fromkeys(system_ids))
        refresh = refresh_fn or self.refresh_business_context
        if not any(artifact_class_requires_roll(c) for c in artifact_classes):
            logger.info("batched refresh (%s): no-roll artifact classes %s — "
                        "zero rolls", reason, sorted(artifact_classes))
            return {sid: {"status": "skipped", "readiness": None,
                          "detail": "no-roll artifact class"} for sid in deduped}
        bound = self.BATCH_REFRESH_TIMEOUT if timeout_seconds is None else timeout_seconds
        report = self._run_serial_rolls(deduped, refresh,
                                        time.monotonic() + bound)
        logger.info("batched refresh (%s): %s", reason,
                    {k: v["status"] for k, v in report.items()})
        return report

    def _run_serial_rolls(self, system_ids: list, refresh,
                          deadline: float) -> dict:
        """The serial loop: one roll at a time, each gated on READY before the
        next is issued. A failed roll — or one whose runtime never came READY
        — aborts the tail (rolling on would put two runtimes down at once);
        aborted/timed-out systems report ``skipped`` with a named detail."""
        report: dict[str, dict] = {}
        abort_detail = None
        for sid in system_ids:
            if abort_detail is not None:
                report[sid] = {"status": "skipped", "readiness": None,
                               "detail": abort_detail}
                continue
            if time.monotonic() >= deadline:
                report[sid] = {"status": "skipped", "readiness": None,
                               "detail": "batch timeout — not attempted"}
                continue
            entry = self._roll_one_system(sid, refresh, deadline)
            report[sid] = entry
            if entry["status"] == "failed":
                abort_detail = f"aborted — {sid} failed"
            elif entry.get("readiness") == "not_ready":
                abort_detail = f"aborted — {sid} not READY"
        return report

    def _roll_one_system(self, system_id: str, refresh, deadline: float) -> dict:
        """Roll ONE system, then hold the serial gate: block until its runtime
        is READY again and run the warm-up probe. Never raises."""
        try:
            status = refresh(system_id)
        except Exception as e:  # noqa: BLE001 - tri-state, never raises upward
            logger.warning("batched refresh: %s raised %s", system_id,
                           type(e).__name__)
            return {"status": "failed", "readiness": None,
                    "detail": type(e).__name__}
        if status != "ok":
            # skipped (local mode / no secret / no runtime) or failed — no
            # roll was completed, so there is nothing to gate on.
            out = status if status in ("failed", "skipped") else "skipped"
            detail = None if status in ("failed", "skipped") else f"refresh returned {status!r}"
            return {"status": out, "readiness": None, "detail": detail}
        if not self._await_runtime_ready(system_id, deadline):
            return {"status": "ok", "readiness": "not_ready",
                    "detail": "READY deadline expired — roll issued, runtime "
                              "still coming up"}
        verified = self._verify_runtime_warm(system_id)
        return {"status": "ok",
                "readiness": "ready_verified" if verified else "ready_unverified",
                "detail": None}

    def _await_runtime_ready(self, system_id: str, deadline: float) -> bool:
        """Block until the ``adt_<id>`` runtime reports READY (the serial gate).

        Reuses :meth:`_runtime_is_ready` — the SAME list-runtimes readiness
        source the fast path and reonboard discipline use; deliberately not a
        second polling implementation. Bounded by *deadline*: returns False
        (never raises) when the runtime is still not READY in time."""
        region = os.environ.get("AWS_REGION", "us-east-1")
        control = boto3.client("bedrock-agentcore-control", region_name=region)
        while True:
            if self._runtime_is_ready(control, system_id):
                return True
            if time.monotonic() + self.READY_POLL_SECONDS > deadline:
                return False
            time.sleep(self.READY_POLL_SECONDS)

    def _verify_runtime_warm(self, system_id: str) -> bool:
        """ONE MCP session-init probe against the rolled runtime (T1.5c).

        READY is control-plane truth only — ADR-0056's bricked runtimes all
        reported READY while serving nothing. Reuses the existing keep-warm
        plumbing (:meth:`_get_m2m_token` + :meth:`_send_mcp_initialize`);
        ``clientInfo`` names the probe so it never looks like user traffic.
        True = probe answered (``ready_verified``); False = no token, no ARN,
        or the probe failed (``ready_unverified`` — honest, never a guess)."""
        region = os.environ.get("AWS_REGION", "us-east-1")
        try:
            token = self._get_m2m_token(region)
            if not token:
                return False
            control = boto3.client("bedrock-agentcore-control", region_name=region)
            name = self._runtime_name(system_id)
            arn = self._list_runtimes_by_name(control).get(name, {}).get("arn", "")
            if not arn:
                return False
            self._send_mcp_initialize(self._runtime_mcp_url(arn, region), token,
                                      timeout=30, client_name="roll-verify")
            return True
        except Exception as e:  # noqa: BLE001 - verification is best-effort
            logger.warning("warm-up probe failed for %s: %s", system_id,
                           type(e).__name__)
            return False

    def stop_agent(self, system_id: str):
        inst = self._agents.pop(system_id, None)
        if not inst:
            return
        if inst.server:
            self._stop_server(inst)
        elif inst.runtime_arn:
            # Cloud agents: do NOT delete the runtime or gateway target on shutdown.
            # These are persistent cloud resources that survive task restarts.
            # Deleting them on every ECS task replacement breaks onboarding state.
            logger.info(f"Cloud agent {system_id} removed from memory (runtime/gateway preserved)")

    def _warmup_runtime(self, runtime_mcp_url: str, region: str):
        """Warm up the runtime by sending a quick MCP initialize request.

        AgentCore runtimes are serverless. Per-session invocation cold start is
        single-digit seconds (avg ~2.9s, p99 < ~5s on demand; ~200-250ms when a
        pre-warmed/reused session serves the request), and steady state is kept
        warm by session reuse plus the server.py _warmup_loop and the periodic
        per-runtime keep-warm job (:meth:`keep_warm_onboarded`). (This is distinct
        from the one-time runtime CREATE / first image pull, which is far slower
        but happens only once at provisioning.) This initialize triggers the
        first session so the gateway sync doesn't hit an unwarmed runtime when it
        tries to connect.
        """
        token = self._get_m2m_token(region)
        if not token:
            logger.warning("M2M token unavailable — skipping runtime warmup")
            return
        try:
            logger.info("Warming up runtime (per-session cold start is typically a few seconds)...")
            self._send_mcp_initialize(runtime_mcp_url, token, timeout=120)
            logger.info("Runtime warmed up successfully")
        except Exception as e:
            logger.warning(f"Runtime warmup failed (will proceed anyway): {e}")

    # ------------------------------------------------------------------
    # M2M auth + cheap per-runtime MCP liveness ping (shared by provisioning
    # warmup and the periodic keep-warm job)
    # ------------------------------------------------------------------

    def _m2m_token_url(self, region: str) -> str:
        """The M2M Cognito token endpoint:
        ``https://adt-m2m-{acct[-8:]}.auth.{region}.amazoncognito.com/oauth2/token``,
        falling back to the gateway id's tail when the account can't be read."""
        try:
            acct = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
            domain_prefix = f"adt-m2m-{acct[-8:]}"
        except Exception:  # noqa: BLE001 - fall back to the gateway id's tail
            domain_prefix = f"adt-m2m-{self.gateway_id[-8:]}" if self.gateway_id else ""
        return f"https://{domain_prefix}.auth.{region}.amazoncognito.com/oauth2/token"

    def _get_m2m_token(self, region: str) -> str:
        """Fetch a machine-to-machine OAuth access token for runtime invocation.

        Reuses the Cognito M2M client-credentials flow that gateway → runtime auth
        uses. Returns "" (never raises) when M2M is not configured or the token
        exchange fails, so callers degrade to a no-op rather than crashing.
        """
        if not self._m2m_client_id or not self._m2m_discovery_url or not self._m2m_pool_id:
            logger.debug("M2M config not set — no token available")
            return ""
        try:
            import urllib.request
            import urllib.parse
            import json
            import ssl

            cognito = boto3.client("cognito-idp", region_name=region)
            resp = cognito.describe_user_pool_client(
                UserPoolId=self._m2m_pool_id,
                ClientId=self._m2m_client_id,
            )
            client_secret = resp["UserPoolClient"].get("ClientSecret", "")
            token_url = self._m2m_token_url(region)

            data = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": self._m2m_client_id,
                "client_secret": client_secret,
                "scope": "mcp/tools.read mcp/tools.execute",
            }).encode()

            # Default TLS verification (mirrors gateway_auth.py). The Cognito token
            # endpoint presents a valid AWS-issued certificate — no bypass is needed.
            ctx = ssl.create_default_context()

            token_req = urllib.request.Request(
                token_url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:  # nosec B310 - URL constructed from internal trusted config  # nosemgrep: dynamic-urllib-use-detected - URL constructed from internal trusted config
                return json.loads(r.read())["access_token"]
        except Exception as e:
            logger.debug("M2M exchange failed: %s (token request)", e)
            return ""

    @staticmethod
    def _runtime_mcp_url(runtime_arn: str, region: str) -> str:
        """Derive the MCP invocation URL for a runtime ARN (single source of truth)."""
        encoded_arn = runtime_arn.replace(":", "%3A").replace("/", "%2F")
        return (
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
            f"{encoded_arn}/invocations?qualifier=DEFAULT"
        )

    def _send_mcp_initialize(self, runtime_mcp_url: str, token: str, timeout: int = 30,
                             client_name: str = "warmup") -> None:
        """POST a cheap MCP ``initialize`` to a runtime URL to wake/keep its container warm.

        This is the lightest call that touches the runtime data plane — it opens a
        session but runs NO tool and NO model (Bedrock) call. ``client_name`` names
        the MCP clientInfo so keep-warm traffic is distinguishable from user traffic
        and never pollutes conversation memory/metrics.
        """
        import urllib.request
        import json
        import ssl

        # Default TLS verification (mirrors gateway_auth.py). The AgentCore runtime
        # endpoint presents a valid AWS-issued certificate — no bypass is needed.
        ctx = ssl.create_default_context()

        mcp_body = json.dumps({
            "jsonrpc": "2.0", "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": client_name, "version": "1.0"}},
            "id": 1,
        }).encode()
        req = urllib.request.Request(
            runtime_mcp_url, data=mcp_body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
        )
        # It invokes the runtime (as InvokeAgentRuntime would) outside botocore:
        # only while the running operation's lease, if it holds one, is held.
        check_write_guard()
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:  # nosec B310 - URL constructed from internal trusted config  # nosemgrep: dynamic-urllib-use-detected - URL constructed from internal trusted config
            r.read(100)

    @staticmethod
    def _list_runtimes_by_name(control) -> dict:
        """Return ``{runtimeName: {"arn": ..., "status": ...}}`` from one list call.

        Best-effort: returns ``{}`` (never raises) so keep-warm degrades to a no-op
        if the control-plane list fails.
        """
        try:
            existing = list_all_agent_runtimes(control)
        except Exception as e:
            logger.debug("keep-warm: list_agent_runtimes failed: %s", e)
            return {}
        out: dict[str, dict] = {}
        for rt in existing:
            name = rt.get("agentRuntimeName")
            if name:
                out[name] = {"arn": rt.get("agentRuntimeArn", ""), "status": rt.get("status", "")}
        return out

    def _ping_runtime(self, runtime_arn: str, token: str, region: str) -> None:
        """Send one cheap MCP ``initialize`` keep-warm ping to a runtime by ARN."""
        url = self._runtime_mcp_url(runtime_arn, region)
        self._send_mcp_initialize(url, token, timeout=30, client_name="keepwarm")

    def keep_warm_onboarded(self) -> int:
        """Ping each currently-onboarded, READY system runtime to prevent cold-scaling.

        AgentCore per-system runtimes scale to zero when idle; the next user query
        then hits a cold container (multi-second cold start, or a transient
        "no endpoint or agent found" while it spins up). This job sends the CHEAPEST
        thing that keeps a container alive — an MCP ``initialize`` (session open, no
        tool call, no Bedrock call) — to every onboarded system whose backing runtime
        is READY. It complements the gateway/orchestrator ``server.py`` ``_warmup_loop``,
        which pings the gateway (``tools/list``) and the orchestrator runtime but does
        NOT wake the individual system-agent runtimes behind the gateway.

        Contract:
          * Cloud-only — a no-op (returns 0) in local/dev mode.
          * Guarded — a no-op when no systems are onboarded yet.
          * Scoped — iterates the durable catalog's onboarded set unioned with the
            in-memory ``self._agents`` (memory alone is empty right after a task
            redeploy) and skips any system whose ``adt_<id>`` runtime is absent or
            not READY.
          * Best-effort — a per-system ping failure is logged and swallowed; it never
            aborts the loop or propagates to the scheduler.
          * Namespaced — pings use an MCP clientInfo of ``keepwarm`` so they never
            pollute conversation memory or user metrics.

        Returns the number of runtimes successfully pinged (useful for tests/metrics).
        """
        if self.mode != "cloud":
            return 0
        # Enumerate from the DURABLE catalog (rebuilt from Neptune at startup)
        # unioned with the in-memory set. ``self._agents`` alone is empty/partial
        # right after an ECS task redeploy, which silently reduced this job to
        # pinging a subset (observed live: 1 of 3 READY runtimes pinged, so the
        # others answered queries cold). Lazy import — catalog is import-pure.
        try:
            from .catalog import catalog as _catalog
            onboarded_ids = set(_catalog.get_onboarded_ids()) | set(self._agents)
        except Exception:  # noqa: BLE001 - catalog unavailable → memory-only fallback
            onboarded_ids = set(self._agents)
        if not onboarded_ids:
            logger.debug("keep-warm: no onboarded systems yet — skipping")
            return 0

        region = os.environ.get("AWS_REGION", "us-east-1")
        try:
            control = boto3.client("bedrock-agentcore-control", region_name=region)
        except Exception as e:
            logger.debug("keep-warm: could not create control client: %s", e)
            return 0

        runtimes = self._list_runtimes_by_name(control)
        if not runtimes:
            return 0

        token = None  # fetched lazily once we know there is at least one READY runtime
        pinged = 0
        for system_id in sorted(onboarded_ids):
            try:
                rt = runtimes.get(self._runtime_name(system_id))
                if not rt or rt.get("status") != "READY":
                    logger.debug("keep-warm: skipping %s (runtime absent or not READY)", system_id)
                    continue
                if token is None:
                    token = self._get_m2m_token(region)
                    if not token:
                        logger.debug("keep-warm: no M2M token — skipping all pings")
                        return pinged
                self._ping_runtime(rt["arn"], token, region)
                pinged += 1
            except Exception as e:  # noqa: BLE001 - best-effort per-system; never crash the job
                logger.warning("keep-warm: ping failed for %s (non-fatal): %s", system_id, e)
        if pinged:
            logger.info("keep-warm: pinged %d onboarded runtime(s)", pinged)
        return pinged

    def stop_all(self):
        """Remove all agents from memory (cloud agents: runtime/gateway preserved)."""
        for sid in list(self._agents):
            self.stop_agent(sid)

    def delete_cloud_agent(self, system_id: str):
        """Explicitly delete cloud runtime and gateway target (use for reset, not shutdown).
        Falls back to AWS lookup if agent is not in memory (e.g. after task def redeploy).
        The gateway target is addressed on the gateway it actually lives on (see
        _deletion_gateway_id) — the `query-bridge` target is on the dedicated Quick
        gateway, everything else on the shared one. If that gateway's targets
        cannot be listed, :class:`GatewayTargetLookupError` propagates and the
        runtime is NOT deleted (the target may still exist)."""
        inst = self._agents.pop(system_id, None)
        region = os.environ.get("AWS_REGION", "us-east-1")
        control = boto3.client("bedrock-agentcore-control", region_name=region)
        gateway_id = self._deletion_gateway_id(inst, system_id)

        if inst and inst.runtime_arn:
            runtime_id = inst.runtime_arn.split("/")[-1]
            gateway_target_id = inst.gateway_target_id
        else:
            runtime_id, gateway_target_id = self._lookup_cloud_agent(
                control, system_id, gateway_id)

        if gateway_target_id and gateway_id:
            outcome = self._delete_gateway_target_and_wait(
                control, gateway_id, gateway_target_id, system_id)
            if outcome == "failed":
                # The gateway target survived (unrelated error / transitional
                # budget exhausted). Deleting the runtime now would strand the
                # live target against a missing runtime — skip it and return.
                logger.warning(
                    f"Skipping runtime delete for {system_id}: its gateway target "
                    f"{gateway_target_id} delete did not take (target survives)")
                return

        if runtime_id:
            self._delete_runtime_and_wait(control, runtime_id, system_id)

    @staticmethod
    def _delete_runtime_and_wait(control, runtime_id: str, system_id: str) -> None:
        """Delete one cloud agent runtime and wait (bounded 120 s) for it to be
        gone. A ConflictException/Deleting-state error means it is already going
        away (wait); any other error is logged."""
        try:
            control.delete_agent_runtime(agentRuntimeId=runtime_id)
            logger.info(f"Deleted cloud agent runtime {runtime_id} for {system_id}")
        except Exception as e:
            if "ConflictException" in type(e).__name__ or "Deleting" in str(e):
                logger.info(f"Runtime {runtime_id} already deleting — will wait")
            else:
                logger.warning(f"Failed to delete cloud agent runtime {runtime_id}: {e}")
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(4)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            try:
                control.get_agent_runtime(agentRuntimeId=runtime_id)
                logger.debug(f"Runtime {runtime_id} still deleting...")
            except Exception:
                logger.info(f"Runtime {runtime_id} confirmed deleted")
                break

    def _deletion_gateway_id(self, inst: "AgentInstance | None", system_id: str) -> str:
        """The gateway ``system_id``'s target lives on: recorded on the in-memory
        instance; else, for the `query-bridge` (not in memory — e.g. after a task
        redeploy), the dedicated Quick gateway (QUICK_GATEWAY_ID) when the env
        has one, else the shared gateway (where a deployment that predates the
        dedicated gateway put it — deletion must still be able to reach that);
        else the shared gateway. Deletion never refuses on a missing env: it is
        cleanup, not provisioning."""
        if inst and inst.gateway_id:
            return inst.gateway_id
        if not inst and system_id == QUERY_BRIDGE_SYSTEM_ID:
            return os.environ.get("QUICK_GATEWAY_ID", "").strip() or self.gateway_id
        return self.gateway_id

    @staticmethod
    def _lookup_cloud_agent(control, system_id: str, gateway_id: str) -> tuple:
        """AWS-side lookup of ``(runtime_id, gateway_target_id)`` by name for an
        agent that is not in memory. Either may be ``None`` (absent).

        A failed RUNTIME listing yields ``runtime_id=None`` (nothing is then
        deleted). A failed TARGET listing raises
        :class:`GatewayTargetLookupError` instead: an unlisted target is
        unknown, not absent, and reading it as absent would let the caller
        delete the runtime while the target survives."""
        runtime_id, gateway_target_id = None, None
        runtime_name = f"adt_{system_id.replace('-', '_')}"
        try:
            for r in list_all_agent_runtimes(control):
                if r.get("agentRuntimeName") == runtime_name:
                    runtime_id = r["agentRuntimeArn"].split("/")[-1]
                    logger.info(f"Found runtime {runtime_name} via AWS lookup: {runtime_id}")
                    break
        except Exception as e:
            logger.warning(f"Failed to lookup runtime for {system_id}: {e}")

        if gateway_id:
            try:
                targets = list_all_gateway_targets(control, gateway_id)
            except Exception as e:
                logger.warning(f"Failed to lookup gateway target for {system_id}: {e}")
                raise GatewayTargetLookupError(gateway_id, system_id) from e
            for t in targets:
                if t.get("name") == system_id:
                    gateway_target_id = t["targetId"]
                    logger.info(f"Found gateway target {system_id} via AWS lookup: {gateway_target_id}")
                    break
        return runtime_id, gateway_target_id

    @staticmethod
    def _delete_gateway_target_and_wait(control, gateway_id: str,
                                        gateway_target_id: str, system_id: str) -> str:
        """Delete one gateway target on ``gateway_id`` and wait (bounded 60s) for
        it to be verifiably ABSENT — targets block gateway deletion, and the
        caller deletes the runtime only on a going-away outcome.

        Returns ``"gone"`` ONLY when absence is positively confirmed (a NotFound
        read). ``"failed"`` otherwise: a refused delete (unrelated error /
        transitional budget), a non-NotFound read error during the wait
        (AccessDenied/Throttling/InternalServer — the target may survive), or the
        target still readable at the 60 s deadline. Both ``"accepted"`` AND a
        service-reported ``"deleting"`` go through the absence poll: ``DELETING``
        is the *current* status of an async delete, not proof the target is gone,
        so the caller must still see confirmed absence before deleting the
        runtime. A transitional NON-deleting refusal
        (``SYNCHRONIZING``/``CREATING``/``UPDATING`` or "Retry the request") is
        RETRIED through the budget, not mistaken for "already deleting" (observed
        live 2026-10-09)."""
        from .runtime_provision_helpers import read_gateway_target_status
        deadline = time.time() + 60
        outcome = AgentManager._issue_target_delete(
            control, gateway_id, gateway_target_id, system_id, deadline)
        if outcome in ("gone", "failed"):
            return outcome  # gone: already confirmed absent. failed: refused.
        # outcome is "accepted" or "deleting": confirm absence before telling the
        # caller the runtime is safe to delete. A read error (non-NotFound) or a
        # target still present at the deadline leaves absence UNKNOWN -> failed,
        # never "confirmed deleted".
        while time.time() < deadline:
            time.sleep(3)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            status, is_gone = read_gateway_target_status(control, gateway_id, gateway_target_id)
            if is_gone:
                logger.info(f"Gateway target {gateway_target_id} confirmed deleted")
                return "gone"
            if not status:
                # A non-NotFound read error — absence cannot be verified.
                logger.warning(f"Gateway target {gateway_target_id} absence unverifiable "
                               f"(status read failed); leaving the runtime in place")
                return "failed"
            logger.debug(f"Gateway target {gateway_target_id} still present (status={status})...")
        logger.warning(f"Gateway target {gateway_target_id} still present after 60s; "
                       f"leaving the runtime in place")
        return "failed"

    @staticmethod
    def _issue_target_delete(control, gateway_id: str, gateway_target_id: str,
                             system_id: str, deadline: float) -> str:
        """Issue ``delete_gateway_target``, retrying through transitional
        non-deleting states until ``deadline``. Returns the terminal outcome
        ``"accepted" | "gone" | "deleting" | "failed"``. Per-retry detail is
        DEBUG; the terminal line is INFO/WARNING."""
        from .runtime_provision_helpers import (
            classify_gateway_target_delete, gateway_delete_error_code,
            read_gateway_target_status)
        attempt = 0
        while True:
            attempt += 1
            try:
                control.delete_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=gateway_target_id)
                logger.info(f"Deleted gateway target {gateway_target_id} for {system_id}")
                return "accepted"
            except Exception as e:
                status, gone = read_gateway_target_status(
                    control, gateway_id, gateway_target_id)
                if gone:
                    logger.info(f"Gateway target {gateway_target_id} already gone")
                    return "gone"
                err_code = gateway_delete_error_code(e)
                kind = classify_gateway_target_delete(str(e), status, err_code)
                logger.debug("Gateway target %s delete attempt %d -> %s (code=%s, status=%s): %s",
                             gateway_target_id, attempt, kind, err_code or "?", status or "unknown", e)
                if kind == "transitional" and time.time() < deadline:
                    time.sleep(3)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
                    continue
                if kind == "deleting":
                    logger.info(f"Gateway target {gateway_target_id} already deleting — will wait")
                    return "deleting"
                logger.warning(
                    f"Failed to delete gateway target {gateway_target_id} "
                    f"(attempts={attempt}, status={status or 'unknown'}): {e}")
                return "failed"

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_agents(self) -> list[dict]:
        return [
            {"system_id": a.system_id, "system_name": a.system_name,
             "schema": a.schema, "mcp_url": a.mcp_url, "port": a.port}
            for a in self._agents.values()
        ]

    def get_mcp_url(self, system_id: str) -> str | None:
        inst = self._agents.get(system_id)
        return inst.mcp_url if inst else None

    def get_mcp_urls(self) -> dict[str, str]:
        return {sid: a.mcp_url for sid, a in self._agents.items()}

    def agent_count(self) -> int:
        return len(self._agents)


# --------------------------------------------------------------------------- #
# Re-provision preservation (ADR-0054 D4) — business-context re-include
# --------------------------------------------------------------------------- #
# _start_cloud composes a FRESH adt/agent-config/<id> secret payload from
# connection_config, so without this wrapper a re-onboard would silently drop
# steward business context. The base method is 468 lines and FROZEN by the
# changed-function maintainability gates (test_response_integrity_iteration5/6) — same rebinding
# pattern as server._restore_integrity / orchestrator: the base body stays
# byte-identical and the wrapper injects the graph-read key into the config
# COPY the base turns into secret_payload.
_base_start_cloud = AgentManager._start_cloud


def _start_cloud_with_business_context(
    self, system_id: str, system_name: str, schema: str,
    system_prompt: str, tables_desc: str,
    connection_type: str = "PostgreSQL", connection_config: dict | None = None,
    conn_params: dict | None = None, *,
    gateway_id: str | None = None, gateway_url: str | None = None,
) -> str:
    """Provision the cloud agent, re-including the steward business context.

    The System node (graph = source of truth) is read here and the
    ``business_context`` key is injected into the ``connection_config`` copy
    that the base ``_start_cloud`` turns into the agent-config secret payload
    (any stale caller-supplied key is dropped first), so a re-onboard never
    silently drops steward content. An UNREADABLE graph (read → ``None`` or a
    raise) is NOT treated as unset: the existing agent-config secret's
    delivered value is carried forward instead (ADR-0054 D4) — only a
    readable, genuinely-unset/disabled state omits the key. The value is
    never logged. The 400-byte plaintext CONNECTION_CONFIG env fallback
    (secret-write-failure path only) may carry a truncated copy — an accepted
    residual: the write-time secret-shape rejection (D5 layer 1) is
    deterministic and BEST-EFFORT ONLY (four structured-shape regexes; it
    does not cover unstructured sensitive prose), so the fragment is
    reduced-risk, not guaranteed non-sensitive. KnowledgeGraph and QueryBridge
    systems have no agent-config secret and pass through untouched.
    """
    if connection_type in _SECRETLESS_CONNECTION_TYPES:
        return _base_start_cloud(self, system_id, system_name, schema,
                                 system_prompt, tables_desc, connection_type,
                                 connection_config, conn_params,
                                 gateway_id=gateway_id, gateway_url=gateway_url)
    region = os.environ.get("AWS_REGION", "us-east-1")
    cfg = self._apply_business_context(
        system_id, dict(connection_config or {}), region)
    return _base_start_cloud(self, system_id, system_name, schema, system_prompt,
                             tables_desc, connection_type, cfg, conn_params,
                             gateway_id=gateway_id, gateway_url=gateway_url)


AgentManager._start_cloud = _start_cloud_with_business_context


# --------------------------------------------------------------------------- #
# Local-mode delivery (ADR-0054 D4) — business-context include at agent start
# --------------------------------------------------------------------------- #
# Local mode has no agent-config secret: the connection_config handed to the
# local agent's server is the delivery channel (create_system_agent pops connection_config's
# business_context exactly as the cloud runtime does). Without this wrapper
# the local channel is write-only — a steward saves context and no local
# agent ever sees it. refresh_business_context stays "skipped" in local mode:
# delivery happens at agent START (restart or re-onboard picks up changes).
_base_start_local = AgentManager._start_local


def _start_local_with_business_context(
    self, system_id: str, system_name: str, schema: str,
    system_prompt: str, tables_desc: str, conn_params: dict | None,
    connection_type: str = "PostgreSQL", connection_config: dict | None = None,
) -> str:
    """Start the local agent, including the steward business context.

    Mirrors the ``_start_cloud`` rebind: the System node (graph = source of
    truth) is read and the ``business_context`` key is injected into the
    ``connection_config`` copy handed to the agent (any stale
    caller-supplied key is dropped first). An unreadable graph omits the key
    — local mode has no delivered secret copy to carry forward, and the next
    agent start re-reads graph truth. The value is never logged.
    KnowledgeGraph and QueryBridge systems pass through untouched (parity
    with cloud).
    """
    if connection_type in _SECRETLESS_CONNECTION_TYPES:
        return _base_start_local(self, system_id, system_name, schema,
                                 system_prompt, tables_desc, conn_params,
                                 connection_type, connection_config)
    from . import business_context as _bc
    cfg = dict(connection_config or {})
    cfg.pop("business_context", None)  # graph truth only — never a stale copy
    try:
        state = _bc.read_business_context(system_id)
    except Exception as e:  # noqa: BLE001 - best-effort; never fail the start
        logger.warning("business_context: local graph read raised for %s: %s",
                       system_id, type(e).__name__)
        state = None
    value = _business_context_secret_value(state) if state is not None else None
    if value is not None:
        cfg["business_context"] = value
    return _base_start_local(self, system_id, system_name, schema, system_prompt,
                             tables_desc, conn_params, connection_type,
                             cfg or None)


AgentManager._start_local = _start_local_with_business_context
