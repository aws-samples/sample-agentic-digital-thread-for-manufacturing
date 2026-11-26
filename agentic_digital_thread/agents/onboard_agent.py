# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding agent built with the Strands Agents framework and tool functions."""
import json, logging, os, re
from strands import Agent, tool
from strands.tools.mcp import MCPClient
from .catalog import catalog
from .config import get_config, local_mode_env
from .connection_types import sanitize_system_id, ATHENA, S3TABLES_DUCKDB, SITEWISE_DIRECT, TULIP
from .empty_tool_turn import EmptyToolTurnRetry
from .gateway_auth import bearer_mcp_client
from .surrogate_keys import drop_untrustworthy_tags
from . import reset_lease
from .db import pg, neptune_query, neptune_write, bedrock_call, validate_identifier, validate_system_id
from .public_errors import public_error, public_error_text
from .connection_security import _safe_detail
from .onboarding_session import (
    OnboardingSessionStore,
    SessionNotFoundError,
    SessionExistsError,
    STATUS_DISCOVERING,
    STATUS_DISCOVERED,
    STATUS_MAPPED,
    STATUS_RESOLVED,
    STATUS_PROVISIONING,
    STATUS_ONBOARDED,
    STATUS_FAILED,
)

logger = logging.getLogger(__name__)

# Durable onboarding session state lives in DynamoDB (OnboardingSessionStore),
# NOT a module global. The old ``_session = {}`` dict was process-local and
# advisory; the store is durable and its conditional writes make the
# discover->mapped->resolved->provisioning ordering a load-bearing gate (any
# holder of gateway credentials can call a tool directly — plan section 5).
# DDB-only, no in-memory fallback (user steering 2026-06-30).
_store_singleton: OnboardingSessionStore | None = None


def _store() -> OnboardingSessionStore:
    """Return the process-wide onboarding session store (lazy singleton).

    Lazily constructed so importing this module never touches AWS; tests
    monkeypatch this function (or ``_store_singleton``) with a fake store."""
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = OnboardingSessionStore()
    return _store_singleton


def _initiated_by() -> str:
    """Best-effort actor id for audit (``initiated_by``).

    The in-process agent path has no per-request user sub; the gateway/runtime
    path will pass one through later. Fall back to a stable marker rather than
    inventing a user identity."""
    return os.environ.get("ONBOARDING_INITIATED_BY", "in-process-agent")


# ---------------------------------------------------------------------------
# Secrets Manager helpers
# ---------------------------------------------------------------------------

def _secrets_client():
    import boto3
    return boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _store_secret(system_id: str, credentials: dict) -> str:
    """Write credentials to Secrets Manager. Returns the secret ARN."""
    client = _secrets_client()
    secret_name = f"adt/systems/{system_id}"
    try:
        resp = client.create_secret(
            Name=secret_name,
            SecretString=json.dumps(credentials),
        )
        return resp["ARN"]
    except client.exceptions.ResourceExistsException:
        resp = client.put_secret_value(
            SecretId=secret_name,
            SecretString=json.dumps(credentials),
        )
        return resp["ARN"]


def _fetch_credentials(secret_arn: str) -> dict:
    """Fetch credentials from Secrets Manager by ARN (a full ARN in another
    account than the deployment's is refused,
    ``connection_resolver.refuse_foreign_account_secret``)."""
    from .connection_resolver import refuse_foreign_account_secret
    refuse_foreign_account_secret(secret_arn)
    client = _secrets_client()
    resp = client.get_secret_value(SecretId=secret_arn)
    return json.loads(resp["SecretString"])


def _unless_removal_failed(fn):
    """Refuse ``fn`` (every system-active onboarding tool: store_credentials,
    test_connection, discover, assess_vocabulary_fit, map_fields,
    resolve_identities, provision_system, verify_system; never reset_system,
    which runs the removal that finishes it) while a removal of the system
    runs (:func:`_removal_running_refusal`), for a system whose removal did not finish, or
    when that cannot be checked (:func:`_removal_failed_refusal`); otherwise
    run it unchanged. An identifier the tool would reject is left to the tool.
    A wrapper so the tool bodies stay as they are; ``functools.wraps`` keeps
    the name, docstring and signature the tool spec is built from."""
    import functools

    @functools.wraps(fn)
    def _guarded(system_id: str, *args, **kwargs):
        try:
            validate_system_id(system_id)
        except Exception:  # noqa: BLE001 - the tool's own validation answers it
            return fn(system_id, *args, **kwargs)
        refused = _removal_running_refusal(system_id) or _removal_failed_refusal(system_id)
        return refused if refused else fn(system_id, *args, **kwargs)
    return _guarded


@tool
@_unless_removal_failed
def store_credentials(system_id: str, credentials: str) -> dict:
    """Store credentials securely in AWS Secrets Manager for a system.

    Args:
        system_id: The system to store credentials for.
        credentials: JSON string with credential fields (varies by connection type).
    """
    # Commit-tier guard: the secret name is f"adt/systems/{system_id}",
    # so a system_id containing '/', '\\' or '..' could escape that namespace.
    # system_ids are hyphenated here (e.g. 'mes-mil'), so use the system_id-safe
    # validator (allows '-'/'_', rejects path separators/'..'/whitespace/empty).
    # An invalid identifier RAISES (propagates) — no fallback.
    validate_system_id(system_id)
    try:
        creds = json.loads(credentials) if isinstance(credentials, str) else credentials
    except json.JSONDecodeError:
        return {"error": "credentials must be valid JSON"}
    try:
        arn = _store_secret(system_id, creds)
        entry = catalog.lookup(system_id)
        if entry:
            entry["secret_arn"] = arn
        # The secret ARN is persisted on the catalog entry (above); it does not
        # belong in the onboarding session item. The old ``_session['secret_arn']``
        # write was write-only/unused, so it is dropped (no DDB write here —
        # store_credentials is a commit-tier tool that runs outside the
        # discover->resolved state machine).
        return {"status": "stored", "secret_arn": arn, "system_id": system_id}
    except Exception as e:
        return {"error": "Failed to store credentials: " + public_error_text(
            e, "onboard_agent.store_credentials", default="upstream_error")}


@tool
def get_catalog() -> dict:
    """List all available and connected systems in the catalog."""
    if not catalog.get_all():
        # Stateless-runtime path (adt_onboarding AgentCore runtime): the app's
        # startup restore never ran here, so hydrate from the graph via the
        # catalog's ONE read (the same seam every system-scoped tool uses on
        # a miss — ADR-0069 follow-up; sibling containers each hydrate).
        catalog.hydrate()
    systems = []
    for s in catalog.get_all():
        systems.append({
            'id': s['id'], 'name': s['name'],
            'type': s.get('system_type') or s.get('type', ''),
            'plant': s.get('plant', ''), 'status': s.get('status', ''),
            'connection_type': s.get('connection_type', 'PostgreSQL'),
        })
    return {'systems': systems}


@tool
def get_system_info(system_id: str) -> dict:
    """Get detailed information about a specific system."""
    entry = catalog.lookup(system_id)
    if not entry:
        return {'error': f'System {system_id} not found'}
    from .connection_security import mask_connection, mask_stored_url
    safe = dict(entry)
    if safe.get('connection_string'):
        safe['connection_string'] = mask_stored_url(safe['connection_string'])
    if isinstance(safe.get('connection_config'), dict):
        # Secret keys and every URL field masked, as display and audit mask them.
        safe['connection_config'] = mask_connection(safe['connection_config'])
    return safe


# --- F1 resolution -----------------------------------------------------------
# ``get_field_mappings`` was defined here but never wired into
# ``get_onboard_agent()``'s ``tools=[...]`` (12 defined / 11 wired), and the
# onboarding gateway's two-target split (explore + commit tiers) also omitted it.
# A defined-but-unrouted ``@tool`` would auto-export on the MCP runtime server
# (Task 1.4) as an ungated 12th tool, so per F1/R9 it is DELETED rather than left
# half-wired. (Connected-system field mappings are still queryable via the
# orchestrator/Neptune; this onboarding-agent helper had no callers — confirmed
# repo-wide.)


@tool
@_unless_removal_failed
def test_connection(system_id: str) -> dict:
    """Test connectivity for a system before discovery. Works for all connection types."""
    entry = catalog.lookup(system_id)
    if not entry:
        return {'error': f'System {system_id} not found'}
    # Delegate to the single shared probe (connection_resolver) so the
    # PostgreSQL / REST / MCP / SiteWise probe logic lives in exactly one place.
    from . import connection_resolver
    result = connection_resolver.probe_connection(system_id)
    connection_type = result.get('connection_type') or entry.get('connection_type', 'PostgreSQL')
    # Adapt the shared probe's {status: pass|fail, detail, ...} shape to the
    # tool's existing return shape so the onboarding LLM sees no behavior change.
    if result.get('status') == 'pass':
        out = {'status': 'connected', 'system_id': system_id,
               'name': entry.get('name', system_id), 'connection_type': connection_type}
        refusal, unchecked = _registered_pg_login_check(system_id, entry, connection_type)
        if refusal:
            return {'status': 'failed', 'error': refusal, 'connection_type': connection_type}
        if unchecked:
            out['warning'] = unchecked[0]
        return out
    return {'status': 'failed', 'error': result.get('detail', 'connection failed'),
            'connection_type': connection_type}


def _registered_pg_login_check(system_id: str, entry: dict, connection_type: str) -> tuple:
    """``(refusal, warnings)`` for a USER-registered PostgreSQL login, the
    same check preflight and provisioning run
    (``onboard_preflight.check_registered_pg_login``): a login that can do
    more than read is refused. Demo systems and other types: ``(None, [])``."""
    from . import connection_resolver
    if connection_type != 'PostgreSQL' or not connection_resolver.is_user_registration(entry):
        return None, []
    from .errors import ConnectionSecretUnavailableError
    from .onboard_preflight import LOGIN_UNCHECKED_WARNING, check_registered_pg_login
    try:
        dsn, _arn = connection_resolver.registered_pg_dsn(system_id, entry)
    except ConnectionSecretUnavailableError:
        return None, [LOGIN_UNCHECKED_WARNING]
    return check_registered_pg_login(dsn, str(entry.get('schema') or 'public'))


def _discover_rest_entry(entry: dict, resolved: dict) -> dict:
    """Connection-bearing dict for the OpenAPI backend, built FROM the resolved
    config (spec URL + base_url/token/auth_scheme), not a catalog snapshot."""
    return {
        'connection_string': resolved.get('connection_string', ''),
        'connection_config': {
            'base_url': resolved.get('base_url', ''),
            'token': resolved.get('token', ''),
            'auth_scheme': resolved.get('auth_scheme', 'bearer'),
        },
        'schema': entry.get('schema', ''),
    }


def _discover_i3x_entry(entry: dict, resolved: dict) -> dict:
    """Connection-bearing dict for the i3X data-model backend (resolver-sourced
    per ADR 0031, connection_type riding on the entry). Token: node-resolved
    first, then the registration-time ``adt/conn/<id>`` secret — a freshly
    registered system has no graph node yet (same pre-node fallback the
    cross-account connectors use)."""
    from .connection_resolver import resolve_registered_token
    base_url = resolved.get('base_url', '') or resolved.get('connection_string', '')
    token, auth_scheme = resolve_registered_token(
        entry.get('id', ''), resolved.get('token', ''),
        resolved.get('auth_scheme', ''), entry=entry)
    return {
        'connection_string': base_url,
        'connection_config': {
            'base_url': base_url,
            'token': token,
            'auth_scheme': auth_scheme,
        },
        'connection_type': 'SiteWise (i3X)',
        'schema': entry.get('schema', ''),
    }


def _discover_mcp_entry(system_id: str, resolved: dict) -> dict:
    """Connection-bearing dict for the MCP backend, with the live env/SSM
    bootstrap for the built-in HighByte system before a node exists (ADR 0031
    constraint 2 — a live env read at registration, NOT a persisted catalog
    snapshot). The bootstrap pair is sent as ``Authorization: Bearer <key>``
    (:mod:`builtin_highbyte`)."""
    from . import builtin_highbyte
    mcp_url = resolved.get('url', '') or resolved.get('connection_string', '')
    token = resolved.get('token', '')
    auth_scheme = resolved.get('auth_scheme', 'bearer_literal')
    if not mcp_url and builtin_highbyte.is_builtin(system_id):
        from .catalog import _get_highbyte_url, _get_highbyte_token
        base = _get_highbyte_url()
        if base:
            mcp_url = base + '/mcp'
            auth_scheme = builtin_highbyte.AUTH_SCHEME
            if not token:
                token = _get_highbyte_token()
    return {
        'connection_string': mcp_url,
        'connection_config': {
            'url': mcp_url,
            'token': token,
            'auth_scheme': auth_scheme,
        },
    }


def _discover_backend_result(system_id: str, entry: dict,
                             connection_type: str, resolved: dict) -> dict:
    """Per-connection-type discovery dispatch. i3X short-circuits to the i3X
    data-model discovery — its API is operation-oriented, so the generic
    OpenAPI path-as-table parse would emit junk tables for it.

    Parity contract (ADR-0069 follow-up 3): this set of connection types must
    equal the set ``onboard.py::_onboard_discover_base`` dispatches — the SAME
    tool must behave the same whether the onboarding CHAT runtime or the wizard
    runs it (pinned by ``tests/unit/test_discover_dispatch_parity.py``)."""
    if connection_type == 'PostgreSQL':
        return _discover_postgres(entry, resolved)
    if connection_type == 'SiteWise (i3X)':
        return _discover_i3x_data_model(_discover_i3x_entry(entry, resolved))
    if connection_type in (TULIP, 'REST API'):
        # Dispatch discovery through the vendor registry —
        # no branching on the vendor name here. Each adapter's discover hook
        # adapts the uniform (system_id, entry, resolved) to what it needs
        # (tulip → Tables-API; generic → OpenAPI parse). A
        # new REST vendor is one registry entry, not another branch here.
        from .rest_vendor_registry import resolve_vendor
        return resolve_vendor(connection_type).discover(system_id, entry, resolved)
    if connection_type == 'MCP Server':
        return _discover_mcp(_discover_mcp_entry(system_id, resolved), system_id=system_id)
    if connection_type == ATHENA:
        return _discover_athena_schema(system_id, entry, resolved)
    if connection_type == SITEWISE_DIRECT:
        return _discover_sitewise_direct_model(system_id, entry, resolved)
    if connection_type == S3TABLES_DUCKDB:
        # Parity with the wizard's _discover_s3tables_duckdb: the ONE engine-backed
        # introspection (duckdb_tools.discover_schema), fed the entry/resolved config.
        from .duckdb_tools import discover_schema
        cfg = (entry.get('connection_config') or (resolved or {}).get('connection_config')
               or {k: (resolved or {}).get(k) for k in ('table_bucket_arn', 'namespaces', 'tables', 'region',
                                               'catalog_endpoint', 'role_arn', 'external_id')
                   if (resolved or {}).get(k)}) or None
        return discover_schema(cfg, system_id)
    return {'error': f'Unsupported connection type: {connection_type}'}


#: discover's refusal, by the status of the session it found: what that
#: session holds, the tool that goes on from it (None: no next tool), and
#: whether to add how to start the onboarding over.
_DISCOVER_GATE_STEPS = {
    STATUS_DISCOVERED: ("Its discover is saved: continue with map_fields.", 'map_fields', True),
    STATUS_MAPPED: ("Its field mappings are saved: continue with resolve_identities.",
                    'resolve_identities', True),
    STATUS_RESOLVED: ("Its identity resolution is saved: continue with provision_system.",
                      'provision_system', True),
    STATUS_PROVISIONING: ("It is being provisioned: call provision_system to check on it.",
                          'provision_system', False),
    STATUS_FAILED: ("Its provision failed (provision_system gives the reason).", None, True),
    STATUS_ONBOARDED: ("Its onboarding finished.", None, True),
}


def _start_over_advice(system_id: str, not_onboarded: str) -> str:
    """How the model starts the onboarding of ``system_id`` over, by what
    reset_system would do with it (:func:`_reset_plan` on a checked read of
    its System node, the decision both resets make; nothing is changed
    here). ``not_onboarded`` when reset_system starts it over: reset_system,
    which then clears only its onboarding session. A node a removal marked
    (:func:`_unfinished_removal_advice`): Remove again on its card. An
    onboarded system, which reset_system removes completely: the card's
    Remove system keeping the registration, then onboarding it again. When
    the node cannot be read: not to call reset_system now."""
    try:
        node = _removal_marker_row(system_id)
    except Exception as exc:  # noqa: BLE001 - db.GraphQueryError or a transport error
        logger.warning("start-over advice for %s: node read failed: %s",
                       system_id, type(exc).__name__)
        node = None
    plan = _reset_plan(node) if node is not None else None
    if plan == RESET_STARTS_OVER:
        return not_onboarded
    if plan is not None and _removal_marked(node):
        return _unfinished_removal_advice(system_id, node, plan)
    if plan is not None:
        return (f"Do not call reset_system to start it over: '{system_id}' is onboarded, and "
                "reset_system removes an onboarded system completely (its knowledge-graph "
                "data, agent runtime, gateway target and registration). To start its "
                "onboarding over, the user removes it with Remove system on its card in the "
                "Onboard view, keeping the connection registration (the default), and then "
                "onboards it again.")
    return (f"Whether '{system_id}' is onboarded could not be checked (the knowledge graph "
            "could not be read), so do not call reset_system to start it over now: for an "
            "onboarded system it is a full removal (its knowledge-graph data, agent runtime, "
            "gateway target and registration), not a start-over. Try again in a moment.")


def _unfinished_removal_advice(system_id: str, node: dict, plan: str) -> str:
    """The start-over advice for a system whose System node a removal marked
    (it may still be running, or it stopped): wait for it, or finish it with
    Remove again on its card, which repeats it in the mode the node recorded
    (``removal_full``). Says what reset_system would do (``plan``): finish a
    full removal of an onboarded system, or not start it over."""
    full = (node or {}).get('removal_full') is True
    does = ("reset_system would finish it, removing the system completely (its "
            "knowledge-graph data, agent runtime, gateway target and registration)"
            if plan == RESET_REMOVES else "reset_system does not start it over")
    mode = ("it forgets the registration too" if full
            else "a default removal keeps the registration and its connection")
    after = (", registering it again first if it is no longer in the catalog" if full
             else "")
    return (f"Do not call reset_system to start it over: a removal of '{system_id}' (or a "
            f"reset of all systems) was started and has not finished, and {does}. If that "
            "removal is still running, wait for it; if it stopped, the user finishes it with "
            "Remove again on its card in the Onboard view, which repeats it the way it was "
            f"asked ({mode}), and then onboards it again{after}.")


def _discover_gate_refusal(system_id: str, session: dict, *, raced: bool = False) -> dict:
    """discover's answer for a system that already has an onboarding session
    (nothing was changed): the session's status, the tool that goes on from
    it (:data:`_DISCOVER_GATE_STEPS`), and, where it applies, how to start
    the onboarding over (:func:`_start_over_advice`). ``raced``: this
    discover read the source and lost the session create to another discover
    that saved one meanwhile, so its own read was not saved."""
    status = str((session or {}).get('status') or '')
    step, next_tool, advise = _DISCOVER_GATE_STEPS.get(
        status, (f"Its status is '{status or 'unknown'}'.", None, True))
    if not status:
        step, advise = "Its onboarding session could not be read: try again in a moment.", False
    elif status == STATUS_DISCOVERING:
        age = _session_age_s(session)
        if age is not None and age < DISCOVERING_STALE_AFTER_SECONDS:
            step, advise = "A discover of it is still saving its session: try again shortly.", False
        else:
            step = "A discover of it stopped before its session was saved."
    shown = status or 'unknown'
    if raced:
        text = (f"System {system_id} already has an onboarding session (status '{shown}'): "
                "another discover of it saved its session while this one was reading the "
                f"source, so this discover's result was not saved. {step}")
    else:
        text = (f"System {system_id} already has an onboarding session in progress (status "
                f"'{shown}'), so discover did not run again. {step}")
    if advise:
        text += " " + _start_over_advice(
            system_id,
            "To start its onboarding over instead, call reset_system first: for a system "
            "that is not onboarded it clears only its onboarding session (its registration "
            "and connection stay), and then discover runs again.")
    return {'error': text, 'reason': 'onboarding_in_progress', 'system_id': system_id,
            'session_status': status, **({'next_tool': next_tool} if next_tool else {})}


def _existing_session_refusal(system_id: str) -> dict | None:
    """discover's gate, before anything is read from the source: the refusal
    (:func:`_discover_gate_refusal`) when ``system_id`` already has an
    onboarding session, else None. A session-store error propagates."""
    try:
        session = _store().get_session(system_id)
    except SessionNotFoundError:
        return None  # expected: fresh onboarding
    return _discover_gate_refusal(system_id, session)


def _discovery_seat(connection_type: str, resolved: dict) -> dict | None:
    """The NON-secret identity of the seat a seat-backed Tulip discovery read
    through (``tulip_seat_contract.seat_identity``: URL, workspace, key scope,
    table scope, all from the resolver's one fresh seat read), saved with the
    session so provision can tell a discovery another seat produced. ``None``
    for every other system (an inline Tulip system included)."""
    if (connection_type != TULIP or resolved.get('_secret_unresolved')
            or str(resolved.get('credential_source') or '') != 'secret_arn'):
        return None
    from .tulip_seat_contract import seat_identity
    return seat_identity(resolved.get('base_url'), resolved.get('workspace_id'),
                         resolved.get('key_scope'), resolved.get('table_label_prefix'))


def _persist_discover_session(system_id: str, connection_type: str,
                              schema_val: str, tables: list,
                              discovery_seat: dict | None = None) -> dict | None:
    """Durably persist a successful discover (the state machine's entry point):
    create the session (status='discovering') then atomically advance to
    'discovered' carrying the tables. discover has already refused a system
    with a session before reading its source (:func:`_existing_session_refusal`);
    the conditional create is the durable gate for a session another
    discover saved meanwhile, and its refusal says this read was not saved.
    Only success reaches here, so a *failed* discover never creates a session
    and is freely retryable. Returns an error dict when a session already
    exists, else None."""
    store = _store()
    try:
        # initiated_by is NON-AUTHORITATIVE for now: _initiated_by() returns an
        # env marker / 'in-process-agent', not a verified caller — an audit
        # hint only (no authorization decision keys off it).
        store.create_session(
            system_id, initiated_by=_initiated_by(),
            connection_type=connection_type, schema=schema_val,
        )
    except SessionExistsError:
        # Lost a create race with a concurrent caller (rare on the single-agent
        # path). The conditional write is the durable gate; answer the session
        # the winner wrote (unknown when it cannot be read).
        try:
            session = store.get_session(system_id)
        except Exception:  # noqa: BLE001 - the refusal stands; its status is unknown
            session = {}
        return _discover_gate_refusal(system_id, session, raced=True)
    # A seat-backed Tulip system also saves the seat it read through
    # (:func:`_discovery_seat`); every other session is written as before.
    seat = {'discovery_seat': discovery_seat} if discovery_seat else {}
    store.advance_status(
        system_id, STATUS_DISCOVERING, STATUS_DISCOVERED,
        discovered_tables=_session_tables(connection_type, tables), **seat,
    )
    return None


#: Connection types whose saved discovery keeps no sample values. No REST
#: vendor's discovery hands up values today, and nothing that reads the
#: session would read them (the chat's mapper uses column names), so a value
#: a REST backend ever returns never sits in the onboarding session for its
#: lifetime. Athena and SiteWise keep theirs: the guided interview can derive
#: questions from them.
_SESSION_WITHOUT_SAMPLES = frozenset({TULIP, 'REST API'})


def _session_tables(connection_type: str, tables: list) -> list:
    """``tables`` as the onboarding session saves them (the caller's copy is
    untouched)."""
    if connection_type not in _SESSION_WITHOUT_SAMPLES:
        return tables
    return [{k: v for k, v in t.items() if k != 'sample_values'} if isinstance(t, dict) else t
            for t in tables]


@tool
@_unless_removal_failed
def discover(system_id: str) -> dict:
    """Connect to a system and discover its schema/tables/endpoints. Auto-detects connection type.

    While the system has an onboarding session it does not run again or read the
    source: the answer names the session's status and the tool to call next."""
    # Commit-tier guard: system_id flows into Secrets Manager / DDB keys downstream;
    # validate up front so a '/', '\\' or '..' identifier cannot escape a namespace.
    # Invalid identifier RAISES (propagates) — no fallback.
    validate_system_id(system_id)
    entry = catalog.lookup(system_id)
    if not entry:
        return {'error': f'System {system_id} not found'}
    # The durable gate, before the source is read (no second source discovery).
    if (refused := _existing_session_refusal(system_id)):
        return refused
    # connection_type / schema are MENU IDENTITY (allowed to come from the catalog).
    connection_type = entry.get('connection_type', 'PostgreSQL')
    # Acquire the live CONNECTION via the single resolver. The resolver is
    # node-only (no catalog/env fallback): when the System node isn't yet
    # registered it returns an unconfigured result, and the per-type entry
    # builders bootstrap the connection from the live env / discover-request
    # input (the first-discover path before a node exists). Never read a
    # persisted catalog connection snapshot directly. ADR 0031.
    from . import connection_resolver
    resolved = connection_resolver.resolve_connection(system_id)
    if resolved.get('_unconfigured') and connection_resolver.is_user_registration(entry):
        # A user registration has no node until its first provision: discover
        # the connection it registered (its own secret), never the demo bootstrap.
        resolved = connection_resolver.registration_connection(system_id, entry)

    result = _discover_backend_result(system_id, entry, connection_type, resolved)
    if 'error' in result:
        return result

    schema_val = entry.get('schema', '')
    session_error = _persist_discover_session(
        system_id, connection_type, schema_val, result['tables'],
        discovery_seat=_discovery_seat(connection_type, resolved))
    if session_error:
        return session_error

    return {
        'system_id': system_id,
        'schema': schema_val,
        'tables': [{'name': t['name'], 'row_count': t.get('row_count', 0),
                     'column_count': len(t.get('columns', []))} for t in result['tables']],
        'total_tables': len(result['tables']),
        'total_rows': sum(t.get('row_count', 0) for t in result['tables']),
        'connection_type': connection_type,
    }


def _structured_secret_dsn(creds: dict, schema: str) -> str:
    """DSN from a structured credentials secret via the ONE resolver
    (``connection_resolver.pg_conn_params``), which accepts the RDS/CDK key
    spellings ``dbname``/``database`` and ``username``/``user``. Measured live:
    the hand-built predecessor read only ``database`` → empty → libpq defaulted
    the database to the USER name → 'database "<user>" does not exist' on the
    onboarding runtime while the wizard (already on the resolver) worked."""
    from urllib.parse import quote
    from .connection_resolver import pg_conn_params
    params = pg_conn_params({**creds, "schema": schema})
    user = quote(str(params.get("user") or ""), safe="")
    password = quote(str(params.get("password") or ""), safe="")
    host, port = params.get("host") or "", params.get("port") or 5432
    dbname = params.get("dbname") or ""
    dsn = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    if schema:
        dsn += f"?options=-csearch_path%3D{schema}"
    return dsn


def _discover_postgres_dsn(entry: dict, resolved: dict) -> tuple[str, str]:
    """``(conn_str, schema)`` for :func:`_discover_postgres`.

    Uses ``secret_arn`` from the resolved connection if present (the legacy
    structured-credentials payload is re-read), else the resolved
    ``connection_string``; when neither yields a DSN and a schema is known,
    the LIVE-ENV bootstrap (``demo.db_routing.demo_pg_dsn``) applies — ADR 0031
    constraint 2 permits that read at registration, before a node exists.
    """
    schema = entry.get('schema', '')
    secret_arn = resolved.get('secret_arn', '')
    if secret_arn:
        try:
            creds = _fetch_credentials(secret_arn)
            conn_str = creds.get('connection_string', '')
            if not conn_str:
                schema = creds.get('schema', schema)
                conn_str = _structured_secret_dsn(creds, schema)
        except Exception as e:
            logger.warning("%s: failed to fetch credentials from Secrets Manager", e)  # logs the error text only, never a credential value
            conn_str = resolved.get('connection_string', '')
    else:
        conn_str = resolved.get('connection_string', '')
    from .connection_resolver import is_user_registration
    if not conn_str and schema and not is_user_registration(entry):
        # Never for a user registration: its database is its own, not the seed DB.
        from .demo.db_routing import demo_pg_dsn
        conn_str = demo_pg_dsn(schema)
    return conn_str, schema


def _discover_postgres(entry: dict, resolved: dict) -> dict:
    """Discover schema from a PostgreSQL database.

    The CONNECTION is taken from the resolved config (``resolve_connection`` — the
    System node when present, else the catalog/env snapshot it fails-soft to),
    never from a persisted catalog connection snapshot (see
    :func:`_discover_postgres_dsn`). ``schema`` is menu identity.
    """
    from . import pgdriver
    conn_str, schema = _discover_postgres_dsn(entry, resolved)
    if not conn_str or not schema:
        return {'error': 'PostgreSQL requires connection credentials and schema'}
    validate_identifier(schema)
    conn = None
    try:
        conn = pgdriver.connect(conn_str)
        cur = conn.cursor(cursor_factory=pgdriver.RealDictCursor)
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY table_name",
            (schema,)
        )
        table_names = [r['table_name'] for r in cur.fetchall()]
        from .db import pg_inspect_tables  # shared with onboard.onboard_discover
        tables = pg_inspect_tables(cur, schema, table_names)
        cur.close()
    except Exception as e:
        return {'error': f"Discover failed: cannot connect to '{entry.get('id', schema)}': "
                         + public_error_text(e, "discover.postgres", default="not_reachable")}
    finally:
        if conn is not None:
            conn.close()
    return {'tables': tables}


def _token_target_matches(base_url: str, spec_url: str) -> bool:
    """True when ``spec_url`` is on the stored ``base_url``'s own server: the
    same scheme, user info, host and effective port
    (``connection_resolver.endpoint_identity``: an IPv6 host is never merged
    with a port, an explicit ``:0`` is not the default port), and a path
    inside the base URL's path at a segment boundary. A string prefix is not
    enough: ``https://api.example.com`` prefixes ``https://api.example.com.attacker.example.org``."""
    from .connection_resolver import endpoint_identity
    b, s = endpoint_identity(base_url), endpoint_identity(spec_url)
    if b is None or s is None or b[:4] != s[:4]:
        return False
    base_path, spec_path = b[4].rstrip('/'), s[4]
    return not base_path or spec_path == base_path or spec_path.startswith(base_path + '/')


def _discover_openapi(entry: dict) -> dict:
    """Fetch, validate, and parse an OpenAPI spec.

    Reads the connection from the passed dict's ``connection_string`` (spec URL)
    and ``connection_config`` (base_url/token/auth_scheme). Callers supply a dict
    whose connection was acquired via ``resolve_connection`` (the agent
    ``discover`` tool and ``onboard.onboard_discover``), NOT a persisted catalog
    connection snapshot.
    """
    from . import pinned_http
    connection_config = entry.get('connection_config', {})
    token = connection_config.get('token', '')
    auth_scheme = connection_config.get('auth_scheme', 'bearer')

    # Use connection_string as the spec URL (for i3X this is the public api.i3x.dev spec)
    spec_url = entry.get('connection_string', '')
    if not spec_url:
        return {'error': 'REST API requires an OpenAPI spec URL'}

    # Build auth headers only if fetching from the live server (not the public spec)
    headers = {}
    base_url = connection_config.get('base_url', '').rstrip('/')
    if token and base_url and _token_target_matches(base_url, spec_url):
        if auth_scheme == 'bearer_literal':
            headers['Authorization'] = token
        else:
            headers['Authorization'] = f'Bearer {token}'

    try:
        resp = pinned_http.get(spec_url, connection_type='REST API', headers=headers,
                               timeout=30.0)
        resp.raise_for_status()
        raw = resp.text
    except Exception as e:
        return {'error': f'Failed to fetch spec from {spec_url}: ' + public_error_text(
            e, "discover.openapi.fetch", default="not_reachable",
            scrub=lambda t: _safe_detail(t, connection_config))}

    # Parse JSON, fallback YAML
    spec = None
    try:
        spec = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            import yaml
            spec = yaml.safe_load(raw)
        except Exception as ye:
            return {'error': 'Failed to parse spec as JSON or YAML: ' + public_error_text(
                ye, "discover.openapi.parse", category="invalid_response")}
    if not spec or not isinstance(spec, dict):
        return {'error': 'Spec is empty or not a valid object'}

    # Validate
    try:
        from openapi_spec_validator import validate
        validate(spec)
    except ImportError:
        logger.warning("openapi-spec-validator not installed, skipping validation")
    except Exception as ve:
        return {'error': 'Invalid OpenAPI spec: ' + public_error_text(
            ve, "discover.openapi.validate", category="invalid_response")}

    # Extract endpoints as tables
    tables = []
    for path, path_item in spec.get('paths', {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in ('get', 'post', 'put', 'patch', 'delete'):
            op = path_item.get(method)
            if not op or not isinstance(op, dict):
                continue
            columns = []
            for p in op.get('parameters', []):
                if isinstance(p, dict):
                    columns.append({
                        'name': p.get('name', ''),
                        'data_type': f"{p.get('in', 'query')}_{p.get('schema', {}).get('type', 'string')}",
                    })
            # Extract response schema fields
            for status, resp_obj in op.get('responses', {}).items():
                if not isinstance(resp_obj, dict):
                    continue
                for ct, media in resp_obj.get('content', {}).items():
                    if isinstance(media, dict) and 'schema' in media:
                        schema_obj = media['schema']
                        for prop_name, prop_def in schema_obj.get('properties', {}).items():
                            columns.append({
                                'name': prop_name,
                                'data_type': f"response_{prop_def.get('type', 'string')}",
                            })
                        break
                break
            tables.append({
                'name': f"{method.upper()} {path}",
                'columns': columns,
                'row_count': 0,
            })
    return {'tables': tables}


# _is_tulip (host/vendor-hint sniff) REMOVED. Tulip is now the
# first-class connection type TULIP ("Tulip (Tables API)"); identity is the
# declared type string (rest_vendor_registry._tulip_detect), never the URL —
# so a self-hosted/proxied Tulip on any internal host is recognized.


def _tulip_auth_header(token: str, auth_scheme: str) -> str:
    """Authorization header value for Tulip. Tulip uses HTTP Basic with
    ``base64(apiKey:secret)`` as the token; bearer variants kept for parity with
    the generic REST auth schemes."""
    if auth_scheme == 'bearer_literal':
        return token
    if auth_scheme == 'basic':
        return f'Basic {token}'
    return f'Bearer {token}'


def _tulip_basic_token(api_key: str, api_secret: str) -> str:
    """Build the HTTP Basic token from the RAW Tulip API key
    and secret — ``base64(api_key:api_secret)`` — so the user supplies the two
    fields and the encoding happens HERE, not by hand.

    SECURITY: base64 is an ENCODING, not encryption — it is trivially
    reversible. The raw ``api_key``/``api_secret`` (and the token this returns)
    are credentials: they must flow only through the Secrets Manager path
    (``adt/conn/<id>``), never be logged, echoed, or persisted on the node.
    Returns "" when either field is missing (caller treats as no-credential)."""
    import base64
    key = (api_key or "").strip()
    secret = (api_secret or "").strip()
    if not key or not secret:
        return ""
    return base64.b64encode(f"{key}:{secret}".encode()).decode()


# A Tulip workspace id is a short opaque instance string. With an instance-wide
# (global) Service Account the Tables API path is scoped
# ``/api/v3/w/<workspaceID>/...``; with a workspace-scoped account no ``/w/``
# segment is needed. This is the ONE home for both the validation and the path
# shape, so discover, column reads, record reads and the sample-values hook all
# scope their URLs identically (an empty id reproduces today's unscoped path
# byte-for-byte).
_TULIP_WORKSPACE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _valid_tulip_workspace_id(workspace_id: str) -> bool:
    """True when ``workspace_id`` is a safe single path segment: 1-64 chars of
    ``[A-Za-z0-9_-]`` only (no ``/``, ``..``, whitespace, query/scheme
    characters), so it cannot escape the ``/api/v3/w/<id>/`` namespace or steer
    the request elsewhere. Empty/blank is NOT valid here — the caller treats an
    empty id as "no workspace" and uses the unscoped path."""
    return bool(workspace_id) and bool(_TULIP_WORKSPACE_RE.fullmatch(workspace_id))


#: A Tulip table id is a single opaque token (real ids are short alphanumerics).
#: Ids come from the vendor (discovery, ``tulip_list_tables``) or from the model
#: (the runtime tools), so they are validated before they become a path segment.
_TULIP_TABLE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: The ONLY request paths the connector sends (three read-only Tables API reads),
#: optionally under one ``/w/<workspace>`` segment. Anything else is refused
#: before a request is made, so no id can steer the stored credential elsewhere.
_TULIP_REQUEST_PATH_RE = re.compile(
    r"/api/v3(?:/w/[A-Za-z0-9_-]{1,64})?/tables(?:/[A-Za-z0-9_-]{1,64}(?:/records)?)?")
#: Tables one discovery reads (its listing asks for ``limit=100``; the client
#: enforces the same bound, so a server that ignores it cannot fan discovery out).
_TULIP_DISCOVER_MAX_TABLES = 100


def _valid_tulip_table_id(table_id) -> bool:
    """True when ``table_id`` is a plain Tulip id token safe to use as one path
    segment (no ``/``, ``..``, ``?``, ``#``, ``%`` or whitespace; 1-64 chars)."""
    return isinstance(table_id, str) and bool(_TULIP_TABLE_ID_RE.fullmatch(table_id))


def _check_tulip_request_path(path: str) -> None:
    """Raise ``ValueError`` unless ``path`` is one of the allowed Tables API reads."""
    if not _TULIP_REQUEST_PATH_RE.fullmatch(path or ""):
        raise ValueError("Tulip request path is outside the Tables API reads")


def _tulip_discover_get(url: str, headers: dict, params: "dict | None" = None):
    """One discovery GET with the same size cap as the runtime client: streams
    the body through ``read_http_body_capped`` (no unbounded buffering) and
    returns the parsed JSON. Raises on an HTTP error, an oversized body or a
    non-JSON payload; callers own the fixed-safe error text. Callers build
    ``url`` from the base URL, the validated workspace prefix and (for a table
    definition) a table id that passed :func:`_valid_tulip_table_id`."""
    from . import pinned_http
    from .response_integrity_query import MAX_QUERY_RESULT_BYTES, read_http_body_capped
    with pinned_http.client(connection_type=TULIP, timeout=30.0) as client:
        with client.stream("GET", url, headers=headers, params=params or {}) as response:
            response.raise_for_status()
            body, truncated = read_http_body_capped(response, limit=MAX_QUERY_RESULT_BYTES)
    if truncated:
        raise ValueError("response body exceeds connector limit")
    return json.loads(body)


def _tulip_api_prefix(workspace_id: str = "", key_scope: str = "") -> str:
    """The Tulip Tables API path prefix: ``/api/v3/w/<id>`` ONLY for an
    instance-scoped key (``key_scope == "instance"``) with a VALID workspace id,
    else ``/api/v3``. A workspace-scoped key uses the unscoped path the
    connector's documented behaviour expects. ``key_scope`` empty keeps the
    prior behaviour (scope whenever a valid id is set) so an inline-key system
    is byte-identical. An invalid id is dropped (falls back to unscoped)."""
    wid = (workspace_id or "").strip()
    scope = (key_scope or "").strip()
    if not _valid_tulip_workspace_id(wid):
        return "/api/v3"
    if scope == "workspace":
        return "/api/v3"
    if scope in ("", "instance"):
        return f"/api/v3/w/{wid}"
    return "/api/v3"


def _tulip_workspace_or_error(workspace_id: str) -> "str | None":
    """An error string when a NON-empty ``workspace_id`` is not a safe path
    segment, else ``None`` (empty id = no workspace = valid). Used by discover
    to refuse a malformed id up front rather than silently dropping it."""
    wid = (workspace_id or "").strip()
    if wid and not _valid_tulip_workspace_id(wid):
        return ("Tulip workspace_id must be a single path segment of letters, "
                "digits, '-' or '_' (1-64 chars)")
    return None


def _tulip_table_columns(base_url: str, tid: str, headers: dict,
                         workspace_id: str = "", key_scope: str = "") -> list:
    """Fetch one Tulip table's typed columns
    (``GET {prefix}/tables/{id}``, prefix per :func:`_tulip_api_prefix`).
    Returns ``[]`` when the definition can't be read. Tulip type descriptors may
    be nested (``{'type': 'string'}``) or flat (``dataType``)."""
    if not _valid_tulip_table_id(tid):
        return []
    prefix = _tulip_api_prefix(workspace_id, key_scope)
    try:
        tdef = _tulip_discover_get(f'{base_url}{prefix}/tables/{tid}', headers)
    except Exception:
        return []
    if not isinstance(tdef, dict):
        return []
    columns = []
    for c in (tdef.get('columns') or tdef.get('fields') or []):
        if not isinstance(c, dict):
            continue
        dtype = c.get('dataType') or c.get('type')
        if isinstance(dtype, dict):
            dtype = dtype.get('type', 'string')
        columns.append({'name': c.get('name') or c.get('label') or c.get('id', ''),
                        'data_type': str(dtype or 'string')})
    return columns


def _discover_tulip(entry: dict, resolved: dict) -> dict:
    """Tulip-specific discovery: enumerate Tulip Tables + typed columns via the
    Tables REST API (``GET {prefix}/tables`` -> ``GET {prefix}/tables/{id}``).
    Short-circuits the generic OpenAPI backend for the same reason i3X does —
    Tulip has no consolidated spec and path-as-table would emit API-operation
    pseudo-tables. The path prefix is ``/api/v3/w/<workspace_id>`` when an
    OPTIONAL non-secret ``workspace_id`` is set (an instance-wide Service
    Account), else ``/api/v3`` — byte-identical to the workspace-scoped-token
    path that needs no ``/w/`` segment. Never raises.
    """
    from .connection_resolver import resolve_registered_token
    base_url = (resolved.get('base_url', '')
                or resolved.get('connection_string', '')).rstrip('/')
    if not base_url:
        return {'error': 'Tulip discovery requires a base_url'}
    workspace_id = str(resolved.get('workspace_id', '') or '').strip()
    key_scope = str(resolved.get('key_scope', '') or '').strip()
    ws_error = _tulip_workspace_or_error(workspace_id)
    if ws_error:
        return {'error': ws_error}
    # OPTIONAL table scope (the seat's, or the connection's): only the tables
    # whose label starts with it are this system's. Matched exactly as given.
    from .tulip_seat_contract import table_label_prefix_problem
    label_prefix = resolved.get('table_label_prefix') or ''
    scope_error = table_label_prefix_problem(label_prefix)
    if scope_error:
        return {'error': f'Tulip discovery refused: {scope_error}'}
    prefix = _tulip_api_prefix(workspace_id, key_scope)
    # Node-resolved token first, then the registration's own bound secret through
    # its catalog entry's pointer (a freshly registered system has no graph node
    # yet — mirrors i3X); never adt/conn/<id> by name.
    sid = entry.get('id', '')
    token, auth_scheme = resolve_registered_token(
        sid, resolved.get('token', ''),
        resolved.get('auth_scheme', '') or 'basic',
        entry=catalog.get(sid) if sid else None)
    if not token:
        return {'error': 'Tulip discovery requires credentials (adt/conn/<id> secret)'}
    headers = {'Authorization': _tulip_auth_header(token, auth_scheme)}
    try:
        table_list = _tulip_discover_get(f'{base_url}{prefix}/tables', headers,
                                         {'limit': _TULIP_DISCOVER_MAX_TABLES})
    except Exception as e:
        return {'error': f'Failed to list Tulip tables from {base_url}: ' + public_error_text(
            e, "discover.tulip", default="not_reachable",
            scrub=lambda t: _safe_detail(t, {"token": token}))}
    if not isinstance(table_list, list):
        return {'error': 'Unexpected Tulip /api/v3/tables response (expected a list)'}
    return {'tables': _tulip_tables_from_list(table_list, base_url, headers, workspace_id,
                                              key_scope, label_prefix)}


def _tulip_tables_from_list(table_list: list, base_url: str, headers: dict,
                            workspace_id: str, key_scope: str = "",
                            table_label_prefix: str = "") -> list:
    """Shape a Tulip ``/tables`` listing into the discover result tables, reading
    each table's typed columns (scoped to the workspace). Each table keeps its
    Tulip ``table_id`` so provision can seed the agent with it. A table whose id
    is not a plain id token is skipped (it could not be queried safely), and at
    most :data:`_TULIP_DISCOVER_MAX_TABLES` tables (so as many definition reads)
    are taken, whatever the server returned. With a ``table_label_prefix``, a
    table whose label does not start with it is not this system's: it is
    skipped before its definition is read."""
    from .tulip_seat_contract import label_in_scope, table_label
    tables = []
    for t in table_list:
        if len(tables) >= _TULIP_DISCOVER_MAX_TABLES:
            break
        if not isinstance(t, dict):
            continue
        tid = t.get('id')
        if not _valid_tulip_table_id(tid):
            continue
        if not label_in_scope(table_label(t), table_label_prefix):
            continue
        tables.append({
            'name': str(t.get('label') or t.get('name') or tid or ''),
            'columns': _tulip_table_columns(base_url, tid, headers, workspace_id, key_scope) if tid else [],
            'row_count': 0, 'table_id': tid,
        })
    return tables


def _is_i3x_scalar_type(type_id: str) -> bool:
    """True when this ObjectType is a synthetic scalar property container
    (bare 'DOUBLE' or 'urn:aws:iotsitewise:property:DOUBLE'), not a real
    asset model. Canonical implementation: i3x_envelope (shared with the
    provision walker and the system agent's i3X tools)."""
    from .i3x_envelope import is_i3x_scalar_type
    return is_i3x_scalar_type(type_id)


def _discover_i3x_data_model(entry: dict) -> dict:
    """i3X 1.0 data-model discovery — one table per real ObjectType plus
    Namespace/Hierarchy pseudo-tables, unitOfMeasure-aware columns, and the
    minimal Asset/AssetModel/Property fallback when the server is
    unreachable. Implementation lives in ``onboard_i3x`` (module seam kept
    here so ``discover`` routes and tests target one stable name)."""
    from .onboard_i3x import discover_i3x_data_model
    return discover_i3x_data_model(entry)


def _tag_columns(tags: list) -> list:
    """The columns a no-argument MCP tool's sampled answer describes: one per
    tag path, in first-seen order. An answer that is a list of records (a
    prompt catalog, a SmartQuery row list) flattens to the same paths once per
    record; those are one column each, not one per record, so the columns are
    the record's shape and pass the provenance join of the discover that
    reports them. A column's type is its values' (:func:`_values_type`)."""
    seen: dict = {}
    for tag in tags or []:
        seen.setdefault(tag['path'], []).append(tag['type'])
    return [{'name': path, 'data_type': _values_type(types)} for path, types in seen.items()]


def _values_type(types: list) -> str:
    """One type name for the values sampled at a tag path: nulls count only
    when there is nothing else; one type is that type; ints and floats
    together are ``float``; any other mix is ``str`` (each value reads as
    text)."""
    present = list(dict.fromkeys(t for t in types if t != 'NoneType'))
    if not present:
        return 'NoneType'
    if len(present) == 1:
        return present[0]
    if set(present) <= {'int', 'float'}:
        return 'float'
    return 'str'


def _mcp_auth_headers(token: str, auth_scheme: str) -> dict:
    """The auth header an MCP server's stored token goes in, by scheme."""
    if not token:
        return {}
    if auth_scheme == 'bearer_literal':
        return {'Authorization': token}
    if auth_scheme == 'bearer':
        return {'Authorization': f'Bearer {token}'}
    if auth_scheme == 'api_key':
        return {'X-API-Key': token}
    return {}


def _flatten_mcp_tags(obj, prefix='', max_depth=8, depth=0) -> list[dict]:
    """Flatten a nested dict/list into tag paths with sample values."""
    tags = []
    if depth >= max_depth:
        return tags
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                tags.extend(_flatten_mcp_tags(v, path, max_depth, depth + 1))
            else:
                tags.append({'path': path, 'value': v, 'type': type(v).__name__})
    elif isinstance(obj, list):
        for item in obj:  # process all list elements — each may be a different site/namespace
            tags.extend(_flatten_mcp_tags(item, prefix, max_depth, depth))
    return tags


def _mcp_initialize(client, mcp_url: str, headers: dict, client_name: str) -> dict:
    """``initialize`` an MCP session on ``client``; the headers with its
    session id, for the calls that follow."""
    r = client.post(mcp_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": client_name, "version": "1.0"}}},
                    headers=headers)
    r.raise_for_status()
    session_id = r.headers.get('mcp-session-id', '')
    sh = {**headers}
    if session_id:
        sh['mcp-session-id'] = session_id
    return sh


def _mcp_list_tools(mcp_url: str, headers: dict) -> list:
    """The server's ``tools/list``, in a session of its own."""
    from . import pinned_http
    with pinned_http.client(connection_type="MCP Server", timeout=30.0) as client:
        sh = _mcp_initialize(client, mcp_url, headers, "adt-discovery")
        r2 = client.post(mcp_url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=sh)
        r2.raise_for_status()
        return r2.json().get('result', {}).get('tools', [])


def _sampled_tags(tool_name: str, item) -> list:
    """The tags of one content item of a tool's answer (JSON text), or none
    when it is not JSON."""
    text = item.get('text', '') if isinstance(item, dict) else str(item)
    try:
        data = json.loads(text)
        root = data if isinstance(data, dict) else data if isinstance(data, list) else {}
        tags = _flatten_mcp_tags(root, max_depth=8)
        logger.info(f"MCP tool {tool_name}: sampled {len(tags)} tags from namespace")
        # No cap — map_fields handles batching by equipment group
        return tags
    except Exception as ex:
        logger.warning(f"MCP tag flatten failed for {tool_name}: {ex}")
        return []


def _sample_mcp_tool(mcp_url: str, headers: dict, tool_name: str) -> list:
    """Call a no-argument tool once, in a fresh session, and return the
    columns its answer describes (:func:`_tag_columns`); none when the call
    fails."""
    from . import pinned_http
    sampled: list = []
    try:
        with pinned_http.client(connection_type="MCP Server",
                                timeout=15.0) as client:  # 15s timeout per tool
            sh2 = _mcp_initialize(client, mcp_url, headers, "adt-sample")
            rc = client.post(mcp_url,
                             json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": tool_name, "arguments": {}}},
                             headers=sh2)
            rc.raise_for_status()
            for item in rc.json().get('result', {}).get('content', []):
                sampled.extend(_sampled_tags(tool_name, item))
    except Exception as e:
        logger.warning(f"MCP tool sample call failed for {tool_name}: {e}")
    return _tag_columns(sampled)


def _mcp_tool_table(mcp_url: str, headers: dict, tool: dict) -> dict:
    """One discovered table per tool: a tool with parameters has one column
    per parameter; a tool without is called to sample its answer."""
    tool_name = tool.get('name', '')
    input_schema = tool.get('inputSchema', {}) if isinstance(tool, dict) else {}
    props = input_schema.get('properties', {}) if isinstance(input_schema, dict) else {}
    if not props:
        columns = _sample_mcp_tool(mcp_url, headers, tool_name)
    else:
        columns = [{'name': prop_name,
                    'data_type': prop_def.get('type', 'string') if isinstance(prop_def, dict) else 'string'}
                   for prop_name, prop_def in props.items()]
    return {'name': tool_name, 'columns': columns, 'row_count': len(columns)}


def _discover_mcp(entry: dict, system_id: str = "") -> dict:
    """Connect to an MCP server and list its tools, calling each to sample data structure.

    Reads the server URL from the passed dict's ``connection_string``/
    ``connection_config['url']`` and auth from ``connection_config``. Callers
    supply a dict whose connection was acquired via ``resolve_connection`` (the
    agent ``discover`` tool and ``onboard.onboard_discover``), NOT a persisted
    catalog connection snapshot. At most 20 tools are read. ``system_id``
    applies the built-in HighByte read-only tool rule
    (``builtin_highbyte.read_only_tools``): a tool it drops is neither sampled
    nor listed.
    """
    from .builtin_highbyte import read_only_tools

    mcp_url = entry.get('connection_string', '')
    if not mcp_url:
        return {'error': 'MCP Server requires a server URL'}

    cfg = entry.get('connection_config', {}) or {}
    token = cfg.get('token', '')
    auth_scheme = cfg.get('auth_scheme', 'bearer_literal')
    try:
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   **_mcp_auth_headers(token, auth_scheme)}
        logger.info(f"[_discover_mcp] Connecting to {mcp_url} (auth_scheme={auth_scheme}, has_token={bool(token)})")
        tools = read_only_tools(system_id, _mcp_list_tools(mcp_url, headers),
                                lambda t: t.get('name') if isinstance(t, dict) else None)
        return {'tables': [_mcp_tool_table(mcp_url, headers, t) for t in tools[:20]]}
    except Exception as e:
        return {'error': f'Failed to connect to MCP server at {mcp_url}: ' + public_error_text(
            e, "discover.mcp", default="not_reachable",
            scrub=lambda t: _safe_detail(t, {"token": token}))}


# ---------------------------------------------------------------------------
# Athena discovery core — the ONE implementation (ADR-0069 follow-up 3).
# Consumed by BOTH dispatchers: the agent ``discover`` tool's
# ``_discover_backend_result`` (the adt_onboarding runtime / onboarding CHAT)
# and ``onboard.py::_discover_athena`` (the wizard's thin wrapper, which only
# adds body-derived config + app-only catalog persistence).
# ---------------------------------------------------------------------------

def _athena_discover_cfg(system_id: str, entry: dict | None, resolved: dict | None,
                         connection_config: dict | None) -> dict:
    """Config precedence: explicit ``connection_config`` > the entry's persisted
    ``connection_config`` snapshot > the resolved node connection's Athena
    subset. Then merge cross-account STS credentials via the ONE shared helper
    (discover must assume the SAME role query-time uses — see resolve_evidence)."""
    cfg = dict(connection_config or {})
    if not cfg and system_id:
        cfg = dict((entry or {}).get('connection_config') or {})
    if not cfg:
        cfg = {k: resolved[k] for k in ('workgroup', 'database', 'catalog',
                                        'output_location', 'region')
               if (resolved or {}).get(k)}
    from .resolve_evidence import _with_cross_account_credentials
    return _with_cross_account_credentials(system_id, cfg)


def _athena_sample_values(tname: str, acfg: dict) -> dict:
    """Best-effort LIMIT-5 sample values (max 3 distinct per column) — the
    resolve step uses overlapping sample values across systems to propose
    cross-system join keys (IdentityRules). A bare LIMIT relies on the engine's
    LIMIT pushdown to stay cheap; the generic connector can't know partition
    columns, so this is intentionally dataset-agnostic. Failure to sample never
    fails discovery."""
    from .athena_tools import run_athena_query, _safe_sql_literal
    sample_values: dict = {}
    try:
        sample = run_athena_query(f'SELECT * FROM "{_safe_sql_literal(tname)}" LIMIT 5', acfg, max_rows=5)  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
        for i, cn in enumerate(sample.get('columns', [])):
            vals = []
            for r in sample.get('rows', []):
                v = r[i] if i < len(r) else None
                if v not in (None, "") and v not in vals:
                    vals.append(v)
                if len(vals) >= 3:
                    break
            if vals:
                sample_values[cn] = vals
    except Exception as _e:  # noqa: BLE001 - sampling is best-effort
        logger.info(f"Athena sample-value fetch skipped for {tname}: {_e}")
    return sample_values


def _athena_table_entry(tname: str, db: str, acfg: dict) -> dict:
    """One discovered-table dict: information_schema columns + sample values."""
    from .athena_tools import run_athena_query, _safe_sql_literal
    cols = run_athena_query(
        "SELECT column_name, data_type FROM information_schema.columns "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
        f"WHERE table_schema = '{_safe_sql_literal(db)}' AND table_name = '{_safe_sql_literal(tname)}' "
        "ORDER BY ordinal_position", acfg)
    return {
        "name": tname,
        "columns": [{"name": c[0], "data_type": c[1]} for c in cols.get('rows', [])],
        "row_count": 0,
        "sample_values": _athena_sample_values(tname, acfg),
    }


def _discover_athena_schema(system_id: str, entry: dict | None, resolved: dict | None,
                            connection_config: dict | None = None) -> dict:
    """Introspect the Glue database via Athena information_schema (metadata only —
    NO data scan beyond the bounded LIMIT-5 samples, NO LLM) so discover->map
    sees the real tables/columns. Error shapes are part of the contract:
    ``{"error": str(AthenaConfigError)}`` on a bad config, and
    ``{"error": "Athena discovery failed: ..."}`` on any query failure."""
    from .athena_tools import run_athena_query, _resolve_config, _safe_sql_literal, AthenaConfigError
    from .storage_bound_rule import PARTITION_RULE
    cfg = _athena_discover_cfg(system_id, entry, resolved, connection_config)
    try:
        acfg = _resolve_config(cfg)
    except AthenaConfigError as e:
        return {"error": str(e)}
    try:
        db = acfg['database']
        listing = run_athena_query(
            "SELECT table_name FROM information_schema.tables "  # nosec B608 - identifiers/literals validated fail-closed (validate_identifier/_safe_sql_literal/_quoted_identifier); no untrusted values
            f"WHERE table_schema = '{_safe_sql_literal(db)}' ORDER BY table_name", acfg)
        table_names = [r[0] for r in listing.get('rows', [])]
        # Cap discovery breadth; the map step confirms.
        tables = [_athena_table_entry(t, db, acfg) for t in table_names[:25]]
        ai_analysis = (
            f"Amazon Athena connector (workgroup {acfg['workgroup']}, database {db}). "
            f"Discovered {len(tables)} table(s) via information_schema. Query with athena_run_query "
            f"(read-only Presto/Trino SQL). {PARTITION_RULE}"
        )
    except Exception as e:  # noqa: BLE001 - surface discovery failure to the caller
        return {"error": "Athena discovery failed: " + public_error_text(
            e, "discover.athena", default="upstream_error")}
    return {"system_id": system_id, "schema_name": db, "tables": tables,
            "ai_analysis": ai_analysis, "connection_type": ATHENA}


# ---------------------------------------------------------------------------
# SiteWise (Direct) discovery core — the ONE implementation (ADR-0069
# follow-up 3; property-level discovery per ADR-0037). Consumed by BOTH
# dispatchers, exactly like the Athena core above.
# ---------------------------------------------------------------------------

def _sitewise_discover_cfg(system_id: str, entry: dict | None,
                           connection_config: dict | None) -> dict:
    """Config precedence: explicit ``connection_config`` > the entry's persisted
    snapshot. Then ensure the iotsitewise client assumes the SAME cross-account
    STS role that query-time and provision-time use: the snapshot carries the
    NON-SECRET keys (role_arn/region) but omits the SECRET-only external_id
    (persisted in Secrets Manager, not on the node) — without this merge the
    boto3 client falls back to AMBIENT (deploy-account) credentials and is
    DENIED in the cross-account SiteWise source. Best-effort/generic: a blank
    system_id (pre-registration) or a resolve failure leaves cfg unchanged; a
    config already carrying role_arn+external_id is a no-op."""
    cfg = dict(connection_config or {})
    if not cfg and system_id:
        cfg = dict((entry or {}).get('connection_config') or {})
    from .resolve_evidence import _with_cross_account_credentials
    return _with_cross_account_credentials(system_id, cfg)


def _sitewise_prop_type(p: dict) -> str:
    """Column data_type string for a SiteWise property: dataType + unit,
    tagged as a sitewise property."""
    dt = (p.get("dataType") or "").strip() or "unknown"
    unit = (p.get("unit") or "").strip()
    base = f"{dt} ({unit})" if unit else dt
    return f"{base} — sitewise property"


def _sitewise_model_samples(cfg: dict, region: str, model_id: str) -> dict:
    """Best-effort live sample values per property (map-prompt evidence ONLY —
    NEVER written to the graph; rules-engine constraint). Bounded: at most one
    asset per model and a handful of properties. Any failure -> omit."""
    from . import sitewise_read
    samples: dict = {}
    try:
        assets = sitewise_read.list_assets(cfg, region, asset_model_id=model_id)
        if not assets:
            return samples
        detail = sitewise_read.describe_asset(cfg, region, assets[0]["id"])
        for prop in detail.get("properties", [])[:6]:
            pid = prop.get("id")
            pname = prop.get("name")
            if not pid or not pname:
                continue
            val = sitewise_read.get_property_value(cfg, region, assets[0]["id"], pid)
            if isinstance(val, dict) and "error" not in val and val.get("value") is not None:
                samples[pname] = [val.get("value")]
    except Exception as _se:  # noqa: BLE001 - sampling is best-effort only
        logger.debug(f"SiteWise property sampling skipped for {model_id}: {_se}")
    return samples


def _sitewise_model_tables(models: list, cfg: dict, region: str) -> list:
    """One pseudo-table PER asset model whose columns are that model's
    properties (ADR-0037). NO model-name literals — everything is driven by
    runtime discovery."""
    tables = []
    for m in models:
        props = m.get("properties", [])
        columns = [
            {"name": p.get("name", ""), "data_type": _sitewise_prop_type(p)}
            for p in props if p.get("name")
        ]
        tables.append({
            "name": m.get("name", ""),
            "columns": columns,
            "row_count": 0,
            "sample_values": _sitewise_model_samples(cfg, region, m["id"]) if columns else {},
        })
    return tables


#: The SiteWise (Direct) ``asset_hierarchy`` pseudo-table contract: the ordered
#: column names discovery emits (built into the table below). The sampler
#: imports this so its per-column graph projections cannot drift from the
#: columns discovery actually emits.
ASSET_HIERARCHY_COLUMNS = (
    "site_name", "area_name", "line_name", "process_name",
    "asset_id", "asset_model",
)
#: The pseudo-table's name; the sampler imports it too.
ASSET_HIERARCHY_TABLE = "asset_hierarchy"


def _sitewise_hierarchy_table(models: list, cfg: dict, region: str) -> dict:
    """Backward-compatible ``asset_hierarchy`` pseudo-table (retained so existing
    hierarchy resolution / value-mappings keep working). Populated from a
    bounded per-model asset enumeration; sampling is best-effort."""
    from . import sitewise_read
    hierarchy_samples: dict = {"asset_model": [m.get("name", "") for m in models][:8]}
    try:
        asset_names: list = []
        for m in models[:10]:  # cap breadth to avoid timeout
            for a in sitewise_read.list_assets(cfg, region, asset_model_id=m["id"]):
                asset_names.append(a.get("name", ""))
        hierarchy_samples["asset_name"] = asset_names[:8]
    except Exception as _he:  # noqa: BLE001 - hierarchy sampling is best-effort
        logger.debug(f"SiteWise hierarchy sampling skipped: {_he}")
    return {
        "name": ASSET_HIERARCHY_TABLE,
        "columns": [
            {"name": "site_name", "data_type": "string — maps to concept: site"},
            {"name": "area_name", "data_type": "string — maps to concept: site"},
            {"name": "line_name", "data_type": "string — maps to concept: production-line"},
            {"name": "process_name", "data_type": "string — maps to concept: equipment-id"},
            {"name": "asset_id", "data_type": "string (UUID) — maps to concept: equipment-id"},
            {"name": "asset_model", "data_type": "string — maps to concept: asset-class"},
        ],
        "row_count": 0,
        "sample_values": hierarchy_samples,
    }


def _sitewise_fallback_tables() -> list:
    """Static tool-table list when the SiteWise API is unreachable (fail-soft).

    Mirrors the SiteWise (Direct) system-agent tool surface, arguments included
    (pinned against the real tool schemas in test_output_limit_review)."""
    entries = {"name": "entries_json", "data_type": "string (JSON array of {assetId, propertyId})"}
    asset_ids = {"name": "asset_ids_json", "data_type": "string (JSON array of asset ids; with property_ids_json)"}
    property_ids = {"name": "property_ids_json", "data_type": "string (JSON array of property ids read for every asset)"}
    return [
        {"name": "sitewise_list_asset_models", "columns": [], "row_count": 0},
        {"name": "sitewise_list_assets", "columns": [{"name": "asset_model_id", "data_type": "string (optional)"}], "row_count": 0},
        {"name": "sitewise_describe_asset_model", "columns": [{"name": "asset_model_id", "data_type": "string"}], "row_count": 0},
        {"name": "sitewise_describe_asset", "columns": [{"name": "asset_id", "data_type": "string"}], "row_count": 0},
        {"name": "sitewise_list_associated_assets", "columns": [
            {"name": "asset_id", "data_type": "string"},
            {"name": "direction", "data_type": "string (CHILD default, or PARENT)"},
            {"name": "hierarchy_id", "data_type": "string (optional)"},
            {"name": "max_depth", "data_type": "integer (default 3, max 5)"}], "row_count": 0},
        {"name": "sitewise_get_asset_property_value", "columns": [{"name": "asset_id", "data_type": "string"}, {"name": "property_id", "data_type": "string"}], "row_count": 0},
        {"name": "sitewise_batch_get_property_values", "columns": [entries, asset_ids, property_ids], "row_count": 0},
        {"name": "sitewise_get_asset_property_history", "columns": [
            entries, {"name": "hours_back", "data_type": "integer (default 8)"},
            {"name": "max_results", "data_type": "integer (default 100)"},
            {"name": "start_iso", "data_type": "string (ISO-8601 UTC window start, optional)"},
            {"name": "end_iso", "data_type": "string (ISO-8601 UTC window end, optional)"},
            asset_ids, property_ids], "row_count": 0},
    ]


def _discover_sitewise_direct_model(system_id: str, entry: dict | None, resolved: dict | None,
                                    connection_config: dict | None = None) -> dict:
    """Enumerate ALL asset models and their PROPERTIES generically via the shared
    reader seam (``sitewise_read``), one pseudo-table per model plus the
    backward-compatible hierarchy table. ``resolved`` is accepted for dispatcher
    signature parity but unused: SiteWise (Direct) has never consulted the
    resolved node connection here (the cross-account merge inside
    :func:`_sitewise_discover_cfg` covers credentials). NO LLM."""
    from . import sitewise_read
    cfg = _sitewise_discover_cfg(system_id, entry, connection_config)
    # Region precedence: an EXPLICIT connection_config.region (from the connect
    # registration / the Connect New System modal) wins over the ambient
    # AWS_REGION so a source registered for a specific Region is discovered
    # there, not in the app's default Region.
    region = str(cfg.get('region') or '').strip() or os.environ.get("AWS_REGION", "eu-central-1")
    try:
        models = sitewise_read.list_asset_models_with_properties(cfg, region)
        tables = _sitewise_model_tables(models, cfg, region)
        tables.append(_sitewise_hierarchy_table(models, cfg, region))
        n_with_props = sum(1 for m in models if m.get("properties"))
        ai_analysis = (
            f"AWS IoT SiteWise direct connector. Discovered {len(models)} asset "
            f"model(s); {n_with_props} expose time-series properties. Each model is "
            f"a pseudo-table whose columns are its properties — map produced-count/"
            f"rate to throughput, machine state/transition to machine-state, running "
            f"speed to machine-speed, stopped-state duration to downtime, a reason/"
            f"fault code to failure-mode, an equipment identifier to equipment-id, and "
            f"an event timestamp to event-time. The asset_hierarchy table carries "
            f"site/line/asset identifiers for cross-system identity mapping."
        )
    except Exception as e:  # noqa: BLE001 - fail-soft to the static tool-table list
        logger.warning(f"SiteWise discovery API call failed: {e} — falling back to static schema")
        tables = _sitewise_fallback_tables()
        ai_analysis = "AWS IoT SiteWise direct boto3 connector. Provides tools to list asset models, list assets, describe assets, get current property values, and retrieve historical time-series data (batch) using IAM role credentials."
    return {"system_id": system_id, "schema_name": "sitewise-direct", "tables": tables,
            "ai_analysis": ai_analysis, "connection_type": SITEWISE_DIRECT}


# A station's running piece counters (one per station; they only ever grow).
# The tag-path prompt tells the model to leave them unmapped, and the wizard's
# telemetry default (onboard._default_unmatched_to_sensor_reading) keeps them
# unmapped too, so the two can't drift apart.
STATION_PIECE_COUNTER_SUFFIXES = ("_Good", "_Bad", "_RejectCount", "_Total", "_PartsCount")


def is_station_piece_counter(column) -> bool:
    """True when a tag's last path segment is a station piece counter: it ends
    in one of :data:`STATION_PIECE_COUNTER_SUFFIXES`, or is that bare word."""
    signal = str(column or "").rsplit("/", 1)[-1].rsplit(".", 1)[-1].strip().lower()
    words = tuple(s.lower() for s in STATION_PIECE_COUNTER_SUFFIXES)
    return signal.lstrip("_") in tuple(w.lstrip("_") for w in words) or signal.endswith(words)


# The MCP/OPC UA CRITICAL tag-path block (pure code motion out of
# _build_mapfields_prompt — byte-identical prompt text; R2 maintainability).
_MCP_TAG_PATH_BLOCK = (
    "CRITICAL OPC UA TAG PATH MAPPING: The 'table' name is an equipment station (e.g. Line1Washer, Line1Filler, Line2CNC1, Line3Mixer). "
    "The 'column' is a full OPC UA path shaped '<Enterprise>/<Site>/<Area>/<Line>/<Device>/<Signal>' (e.g. a '<Device>_Down' signal under '<Line>/<Device>').\n"
    "Map OPC UA tags using these rules:\n"
    "- Tags ending in _Down, _Blocked, _Starved → concept 'downtime', role 'identifier'\n"
    f"- Tags ending in {', '.join(STATION_PIECE_COUNTER_SUFFIXES)} are ONE station's running piece counters: leave them UNMAPPED. "
    "They only ever grow and every unit passes several stations, so adding them up overcounts: a station tally is not a line's output, good count or scrap.\n"
    "- Tags ending in _LineOutput → concept 'production-run', role 'measure'\n"
    "- Tags ending in _MaterialID, _OrderID → concept 'production-order', role 'identifier'\n"
    "- Tags ending in _Shift → concept 'shift', role 'identifier'\n"
    "- Tags ending in _BatchID, _RecipeVersion, _IngredientLot → concept 'batch', role 'identifier'\n"
    "- Tags with SpindleSpeed, FeedRate, CycleTime, Vibration, HydraulicPressure, CoolantTemp → concept 'sensor-reading', role 'measure'; also 'cycle-time' for CycleTime\n"
    "- Tags with ToolLifeRemaining, ToolChangeEvent → concept 'tool-life', role 'measure'\n"
    "- Tags with FurnaceTemp, SoakTime, QuenchTemp → concept 'sensor-reading', role 'measure'\n"
    "- Tags with TempZone, DieTemp, ScrewSpeed, Pressure, BatchWeight → concept 'sensor-reading', role 'measure'\n"
    "- Tags with HACCP, AllergenVerified, TempCompliant, PackageIntegrity → concept 'food-safety', role 'measure'\n"
    "- Tags: OEE_Availability -> concept 'availability'; OEE_Performance -> concept 'performance'; OEE_Quality -> concept 'quality-yield-rate'; OEE_Overall -> concept 'oee-score' (all role 'measure'). The composite OEE is DERIVED from these, not stored as its own concept.\n"
    "- The equipment station name (table name like Line1Washer, Line2CNC1) → concept 'equipment-id', role 'identifier'\n"
    "- The line segment in the path (Line1, Line2, Line3) → concept 'production-line', role 'identifier'\n"
    "DO NOT create new concepts for OPC UA equipment names. Map everything to the existing concepts above.\n\n"
)

# The production-line / two-layer / field-role CRITICAL rules shared by both
# map_fields branches (pure code motion out of _build_mapfields_prompt —
# byte-identical prompt text; R2 maintainability).
_COMMON_MAPPING_RULES = (
    "CRITICAL MANUFACTURING LINE MAPPING: Fields named 'line', 'line_id', 'line_name', 'area', 'line_number' that contain short line-identifier values "
    "(shapes like LINE1, Line1, <Area>-L1, <SITE>-L1 — a line word plus a number, or an area/site prefix with a line suffix) MUST map to concept 'production-line' with role 'identifier'. "
    "This is the most important cross-system link — MES work orders, CMMS assets, and OPC UA sensors all reference the same physical production line "
    "using different naming conventions. Do NOT map these to 'work-order', 'location', or 'equipment-id'.\n"
    "TWO-LAYER MAPPING — map genuine matches, leave the rest UNMAPPED:\n"
    "- Map a column to an existing concept ONLY when it genuinely matches that concept's meaning. "
    "Map every genuine match — never hold back a real match (e.g. wo_number -> work-order, "
    "part_number -> spare-part, asset_tag -> equipment-id, quantity_on_hand -> mro-inventory MUST be mapped).\n"
    "- If a column does NOT match any available concept, leave it UNMAPPED (return mappings: []). "
    "Do NOT force a weak or wrong concept just to fill the slot — the system handles unmapped columns afterward.\n"
    "- DO NOT MAP generic/audit or surrogate columns: created_at, updated_at, notes, is_active, "
    "entity_type, source_system, unit_of_measure, and surrogate id keys (bare 'id' and *_id columns). "
    "Return mappings: [] for these.\n"
    "FIELD ROLE RULES — CRITICAL for correct orchestrator queries:\n"
    "- For entity tables (customers, suppliers, assets, products): the name/company_name/product_name field → role 'identifier'. "
    "Address/city/state/zip/country/phone/email fields → role 'correlate'. "
    "The orchestrator uses 'identifier' role fields for display — NEVER map location/contact fields as identifiers.\n"
    "- customer_name, company_name, account_name → concept 'customer', role 'identifier'\n"
    "- city, state, zip, address, country, region → concept 'location' or parent concept with role 'correlate'\n"
    "- supplier_name, vendor_name → concept 'supplier', role 'identifier'\n"
    "Return 1-3 mappings per field ordered by confidence. No explanation outside the JSON."
)


def _build_mapfields_prompt(system_id: str, concepts: str, concept_hints: str,
                            fields_summary: str, is_mcp: bool) -> str:
    """Pure builder for the agent map_fields column→concept prompt (offline-assertable).

    Injects the shared MAPPING_DISAMBIGUATION guidance right after the CONCEPT DEFINITIONS
    block; the MCP/OPC UA and production-line CRITICAL blocks ride in as the module
    constants above (pure code motion). No LLM call here.
    """
    from .catalog import MAPPING_DISAMBIGUATION
    return (
        f"You are a manufacturing data expert. Map each column in system '{system_id}' to one or more of these concepts.\n\n"
        f"CONCEPT DEFINITIONS (use these to guide your mapping):\n{concept_hints}\n\n"
        f"{MAPPING_DISAMBIGUATION}\n"
        f"Available concept IDs: {concepts}\n\n"
        f"Tables and columns:\n{fields_summary}\n\n"
        'Return ONLY a JSON array: [{"field": "table.column", "table": "table", "mappings": [{"concept": "...", "role": "...", "confidence": 0.0-1.0}], "reasoning": "..."}]\n'
        "Roles: identifier (field IS this concept, e.g. sensor_tag→sensor-reading, serial_number→equipment-id), "
        "measure (field quantifies this concept, e.g. quantity→work-order; a date or timestamp column follows the "
        "timestamp rule above and never takes the concept of the record it belongs to), "
        "correlate (field describes/classifies this concept, e.g. work_orders.description→work-order, assets.model→equipment-id, unit→parent concept, remedy_code→work-order), "
        "derive (field is computed from this concept).\n"
        + (_MCP_TAG_PATH_BLOCK if is_mcp else "")
        + _COMMON_MAPPING_RULES
    )



def _mcp_equipment_tables(tables: list) -> list:
    """Group MCP/OPC UA tag columns into virtual per-equipment tables.

    Columns are tag paths shaped ``<Enterprise>/<Site>/<Area>/<Line>/<Device>/<Signal>``;
    grouping by the second-to-last path segment (the equipment station) reduces Bedrock
    calls to one per equipment type. Deterministic: sorted by equipment name.
    """
    from collections import defaultdict
    equipment_groups = defaultdict(list)
    for t in tables:
        tool_name = t['name']
        for col in t.get('columns', []):
            col_name = col['name']
            parts = col_name.replace('\\', '/').split('/')
            equipment = parts[-2] if len(parts) >= 2 else tool_name
            equipment_groups[equipment].append({'tool': tool_name, 'col': col_name})
    virtual_tables = [
        {'name': equip, 'columns': [{'name': c['col']} for c in cols]}
        for equip, cols in equipment_groups.items()
    ]
    virtual_tables.sort(key=lambda t: t['name'])
    return virtual_tables


def _restore_mcp_tool_tables(mappings: list, tables: list) -> list:
    """Put each MCP mapping row back on the discovered tool its tag came from.

    ``map_fields`` lists MCP tags to the model by equipment station
    (:func:`_mcp_equipment_tables`) and the model echoes the station as the
    row's ``table`` and ``field`` prefix. A station is not a discovered table,
    so such a row could not be joined to the discovered schema that provision
    writes (the wizard's map job restores its own split tables the same way,
    ``onboard._remap_virtual_tables``). A row whose tag belongs to exactly one
    discovered tool gets that tool; every other row is left as it is. Mutates
    in place and returns ``mappings``. Pure."""
    owners: dict = {}
    for t in tables or []:
        for c in t.get('columns', []):
            owners.setdefault(c.get('name', ''), set()).add(t.get('name', ''))
    real = {t.get('name', '') for t in tables or []}
    for m in mappings or []:
        if not isinstance(m, dict):
            continue
        table, field = str(m.get('table') or ''), str(m.get('field') or '')
        if not table or table in real or not field.startswith(table + '.'):
            continue
        column = field[len(table) + 1:]
        tools = owners.get(column, set())
        if len(tools) == 1:
            tool = next(iter(tools))
            m['table'], m['field'] = tool, f"{tool}.{column}"
    return mappings


def _map_batches(system_id: str, tables_to_process: list, batch_size: int,
                 concepts: str, concept_hints: str, is_mcp: bool) -> list:
    """Run the per-batch map prompt over the tables and collect parsed mappings."""
    all_mappings = []
    for batch_start in range(0, len(tables_to_process), batch_size):
        batch = tables_to_process[batch_start:batch_start + batch_size]
        fields_summary = '\n'.join(
            f"  {t['name']}: {', '.join(c['name'] for c in t.get('columns', []))}"
            for t in batch
        )
        prompt = _build_mapfields_prompt(system_id, concepts, concept_hints, fields_summary, is_mcp)
        text = bedrock_call(prompt)
        start, end = text.find('['), text.rfind(']') + 1
        if start >= 0 and end > start:
            try:
                all_mappings.extend(json.loads(text[start:end]))
            except json.JSONDecodeError:
                logger.warning("Failed to parse mappings for batch starting at table %d", batch_start)
    return all_mappings


def _annotate_best_concepts(mappings: list) -> list:
    """Annotate each mapping row with its best concept (in place); return low-confidence rows.

    Tie-breaking is STABLE: equal confidences resolve alphabetically by
    concept id, never by LLM output order (which is sampling jitter — two
    same-day onboards of one system produced different 'best' concepts for
    tied rows, measured 2026-08-06)."""

    def _rank(r: dict) -> tuple:
        # max() keeps the LAST maximal element; invert the concept id sort so
        # the alphabetically-FIRST concept wins ties deterministically.
        return (r.get('confidence', 0), [-ord(c) for c in str(r.get('concept', ''))])

    low_conf_fields = []
    for m in mappings:
        role_mappings = m.get('mappings', [])
        m['concepts'] = [{'concept': r['concept'], 'confidence': r.get('confidence', 0)} for r in role_mappings]
        # Best identifier-role mapping for backward compat
        identifiers = [r for r in role_mappings if r.get('role') == 'identifier']
        best = max(identifiers, key=_rank) if identifiers else (
            max(role_mappings, key=_rank) if role_mappings else None
        )
        m['concept'] = best['concept'] if best else 'needs_review'
        m['confidence'] = best.get('confidence', 0) if best else 0
        if all(r.get('confidence', 0) < 0.4 for r in role_mappings) if role_mappings else True:
            low_conf_fields.append(m)
    return low_conf_fields


def _propose_new_concepts(low_conf_fields: list, tables: list) -> list:
    """One proposal pass for all-low-confidence fields; annotates rows in place."""
    proposed_concepts = []
    if not low_conf_fields:
        return proposed_concepts
    low_summary = '\n'.join(
        f"  {f['field']}: {', '.join(c['name'] for t in tables if t['name'] == f.get('table') for c in t.get('columns', []) if c['name'] == f['field'].split('.')[-1])}"
        for f in low_conf_fields
    )
    propose_prompt = (
        "You are a manufacturing data expert. The following fields did not match any known concept well.\n"
        f"Fields:\n{low_summary}\n\n"
        'For each field, propose a new concept definition. Return ONLY a JSON array: '
        '[{"field": "table.column", "proposed_concept": {"id": "kebab-case-id", "name": "...", "description": "...", "isa95_level": "..."}}]\n'
        "isa95_level must be one of: Level0, Level1, Level2, Level3, Level4. No explanation outside the JSON."
    )
    try:
        prop_text = bedrock_call(propose_prompt)
        ps, pe = prop_text.find('['), prop_text.rfind(']') + 1
        proposals = json.loads(prop_text[ps:pe])
        field_to_proposal = {p['field']: p['proposed_concept'] for p in proposals if 'proposed_concept' in p}
        for m in low_conf_fields:
            if m['field'] in field_to_proposal:
                m['proposed_concept'] = field_to_proposal[m['field']]
                proposed_concepts.append(field_to_proposal[m['field']])
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    return proposed_concepts


def _apply_mapping_overrides(system_id: str, mappings: list) -> list:
    """Generic, inert, env-gated mapping-override seam.

    When ``ADT_MAPPING_OVERRIDES_FILE`` names a JSON file
    (``{system_id: [mapping-row, ...]}`` in the exact ``_map_batches`` row
    shape), the rows for *system_id* deterministically REPLACE the
    model-proposed row for the same ``field`` (or are appended when the model
    produced none) BEFORE best-concept annotation — so downstream treats them
    exactly like model output. Unset (the default) ⇒ the proposals pass
    through untouched: zero behavior change.

    This module stays content-free: override DATA lives with whoever sets the
    env var (e.g. the workshop fixture under ``agents/demo/fixtures/``). File
    and JSON errors PROPAGATE — a configured override that cannot be read must
    fail loudly, never silently fall back to the model's proposals.
    """
    path = os.environ.get('ADT_MAPPING_OVERRIDES_FILE', '').strip()
    if not path:
        return mappings
    with open(path, encoding='utf-8') as fh:
        overrides = json.load(fh)
    rows = overrides.get(system_id) or []
    if not rows:
        return mappings
    index_by_field = {m.get('field'): i for i, m in enumerate(mappings)}
    for row in rows:
        idx = index_by_field.get(row.get('field'))
        if idx is None:
            mappings.append(dict(row))
        else:
            mappings[idx] = dict(row)
    logger.info("Applied %d mapping override(s) for system %s from %s",
                len(rows), system_id, path)
    return mappings


@tool
@_unless_removal_failed
def map_fields(system_id: str) -> dict:
    """Analyze discovered fields and map them to manufacturing concepts. Must call discover first."""
    # Durable precondition (load-bearing gate): the session must exist, be in
    # the 'discovered' state, and carry discovered tables. Re-checked here
    # because any gateway-cred holder can call this tool directly (plan section 5).
    store = _store()
    try:
        session = store.get_session(system_id)
    except SessionNotFoundError:
        return {'error': 'No tables discovered yet. Call discover first.'}
    tables = session.get('discovered_tables')
    if not tables or session.get('status') != STATUS_DISCOVERED:
        return {'error': 'No tables discovered yet. Call discover first.'}
    from .catalog import concepts_prompt_block, concept_hints_block
    # Non-retired vocabulary as '- <id>: <description>' lines (contract C2) —
    # replaces the bare comma-joined id list so the model sees what each
    # concept MEANS, not just its id. _build_mapfields_prompt is unchanged.
    concepts = concepts_prompt_block()
    # Concept descriptions for the prompt — helps LLM map fields correctly.
    # Routed through catalog's SHARED overlay-aware composition point (T1.3)
    # so this untruncated surface carries the same fenced overlay treatment
    # as concepts_prompt_block; with no overlays the string is byte-identical
    # to the legacy '  <id>: <desc>' join over CORE_CONCEPT_DEFINITIONS.
    concept_hints = concept_hints_block()
    # MCP systems (e.g. HighByte OPC UA) group tag paths by equipment with a
    # larger batch size (groups are small); SQL systems use table batching.
    is_mcp = session.get('connection_type', 'PostgreSQL') == 'MCP Server'
    if is_mcp:
        tables_to_process, batch_size = _mcp_equipment_tables(tables), 20
    else:
        tables_to_process, batch_size = tables, 5

    mappings = _map_batches(system_id, tables_to_process, batch_size,
                            concepts, concept_hints, is_mcp)
    if is_mcp:
        _restore_mcp_tool_tables(mappings, tables)
    drop_untrustworthy_tags(mappings, tables)
    # Env-gated override seam (inert when ADT_MAPPING_OVERRIDES_FILE is unset).
    mappings = _apply_mapping_overrides(system_id, mappings)
    # Collect fields with all-low-confidence for proposed concept discovery
    low_conf_fields = _annotate_best_concepts(mappings)
    proposed_concepts = _propose_new_concepts(low_conf_fields, tables)

    # Advance discovered->mapped, persisting the mappings durably (conditional
    # write: succeeds only if the session is still 'discovered').
    store.advance_status(system_id, STATUS_DISCOVERED, STATUS_MAPPED, mappings=mappings)
    result = {'mappings': mappings, 'total_mappings': len(mappings), 'needs_review': sum(1 for m in mappings if m['concept'] == 'needs_review')}
    if proposed_concepts:
        result['proposed_concepts'] = proposed_concepts
    return result


def _sample_new_system_values(mappings: list, new_schema: str) -> str:
    """Sample distinct values per mapped field from the NEW system (pure code
    motion out of resolve_identities, R2 maintainability), only those fit
    for a prompt shown (``resolve_evidence.prompt_values``)."""
    from .resolve_evidence import prompt_values
    sample_data = {}
    for m in mappings:
        tbl, fld = m.get('table', ''), m.get('field', '')
        if tbl and fld and new_schema:
            try:
                validate_identifier(fld)
                validate_identifier(tbl)
                r = pg(new_schema, f"SELECT DISTINCT CAST({fld} AS TEXT) FROM {tbl} WHERE {fld} IS NOT NULL LIMIT 5")  # nosec B608 - fld and tbl validated by validate_identifier()
                if r['rows']:
                    sample_data[f"{tbl}.{fld}"] = prompt_values([row[0] for row in r['rows']], 5)
            except Exception:  # nosec B110 - intentional fallback, no recovery action needed
                pass
    return '\n'.join(f"  {k}: {v}" for k, v in sample_data.items())


def _sample_target_system_values(existing_fields: list) -> str:
    """Sample values from existing (target) systems for cross-system comparison
    (pure code motion out of resolve_identities, R2 maintainability), only
    those fit for a prompt shown (``resolve_evidence.prompt_values``)."""
    from .resolve_evidence import prompt_values
    target_sample_data = {}
    for field_info in existing_fields:
        sys_id = field_info.get('sys_id', '')
        schema = field_info.get('sys_schema', '')
        table = field_info.get('table_name', '')
        field = field_info.get('field_name', '')
        if schema and table and field:
            try:
                validate_identifier(field)
                validate_identifier(table)
                r = pg(schema, f"SELECT DISTINCT CAST({field} AS TEXT) FROM {table} WHERE {field} IS NOT NULL LIMIT 5")  # nosec B608 - field and table validated by validate_identifier()
                if r['rows']:
                    key = f"{sys_id}.{table}.{field}"
                    target_sample_data[key] = prompt_values([row[0] for row in r['rows']], 5)
            except Exception:  # nosec B110 - intentional fallback, no recovery action needed
                pass
    return '\n'.join(f"  {k}: {v}" for k, v in target_sample_data.items())


def _build_resolve_prompt(system_id: str, mappings_text: str, existing_text: str,
                          sample_text: str, target_sample_text: str) -> str:
    """Pure builder for the resolve_identities cross-system rules prompt (pure
    code motion out of resolve_identities — R2 maintainability). No LLM call."""
    prompt = (
        f"You are a manufacturing integration expert. A new system '{system_id}' has these concept mappings:\n{mappings_text}\n\n"
        f"Already-onboarded systems have these fields mapped to concepts:\n{existing_text}\n\n"
        f"Sample data values from NEW system:\n{sample_text}\n\n"
        f"Sample values from EXISTING systems:\n{target_sample_text}\n\n"
        "CRITICAL: Look especially for 'production-line' concept matches — this is the key cross-system link. "
        "MES systems use field names like 'line', 'line_id' with values LINE1/LINE2/LINE3. "
        "CMMS systems use field names like 'area' with line-identifier values shaped <Area>-L1/<Area>-L2 (an area or site prefix plus a line suffix). "
        "OPC UA/HighByte uses path segments like Line1/Line2/Line3. "
        "These all refer to the same physical production line: create identity rules linking them even if the values differ. "
        "Write each pattern as literal text around a named placeholder that both sides share, for example "
        "'LN{num}' to 'NORTH-L{num}'; a translation without a {placeholder} cannot be stored.\n"
    )
    prompt += (
        "\n\nCRITICAL RULES:\n"
        "- If the values match unchanged, leave source_pattern and target_pattern empty. A "
        "translation writes each pattern as literal text around a {placeholder} both patterns "
        "name (e.g., 'WO-{n}' to 'MFG-WO-{n}'). Never write a regular expression or a wildcard "
        "glob: neither can be stored.\n"
        "- ALWAYS include 'source_table' and 'target_table' in every rule.\n"
        f"- source_system must be '{system_id}': every rule's source_field and source_table are the "
        "NEW system's, never a field of an already-onboarded system.\n"
        "- Compare sample values from BOTH systems before proposing a rule. "
        "If value formats are incompatible (e.g., UUID vs short tag), do NOT propose a JoinPath — "
        "propose a ValueMapping instead.\n"
        "- For equipment/asset identity where names differ across systems, propose a ValueMapping "
        "with from_value/to_value pairs and method='semantic_similarity'.\n"
    )
    prompt += (
        '\n\nIf you detect equipment/asset identity that cannot be joined by column value '
        '(different naming conventions), also return a "value_mappings" array:\n'
        '[{"from_system": "...", "from_field": "...", "from_value": "...", '
        '"to_system": "...", "to_field": "...", "to_value": "...", '
        '"concept": "...", "confidence": 0.0-1.0, "method": "semantic_similarity|hierarchy_position"}]\n'
    )
    prompt += (
        'Return ONLY a JSON object with "rules" and "value_mappings" keys:\n'
        '{"rules": [{"concept": "...", "source_system": ' + json.dumps(system_id) + ', '
        '"source_field": "...", "source_table": "...", "source_pattern": "...", '
        '"target_system": "...", "target_field": "...", "target_table": "...", "target_pattern": "...", '
        '"confidence": 0.0-1.0, "reasoning": "..."}], "value_mappings": [...]}\n'
        'If no connections found, return {"rules": [], "value_mappings": []}.'
    )
    return prompt


def _parse_resolve_response(text: str) -> tuple:
    """Parse the resolve LLM response — JSON object first, array fallback
    (pure code motion out of resolve_identities — R2 maintainability)."""
    parsed = None
    # Look for JSON object
    obj_start = text.find('{')
    obj_end = text.rfind('}') + 1
    arr_start = text.find('[')
    arr_end = text.rfind(']') + 1
    if obj_start >= 0 and (arr_start < 0 or obj_start < arr_start):
        try:
            parsed = json.loads(text[obj_start:obj_end])
        except (json.JSONDecodeError, ValueError):
            pass
    if parsed is None and arr_start >= 0:
        try:
            parsed = json.loads(text[arr_start:arr_end])
        except (json.JSONDecodeError, ValueError):
            parsed = []

    value_mappings = []
    if isinstance(parsed, dict):
        rules = parsed.get("rules", parsed.get("identity_rules", []))
        value_mappings = parsed.get("value_mappings", [])
    elif isinstance(parsed, list):
        rules = parsed
    else:
        rules = []
    # The resolve prompt asks the model for none of the server-owned evidence
    # fields (value_check, method, transform, shared_count); strip any it wrote
    # so a model-written measurement cannot reach the checkpoint, the stored
    # session or provision, and a model method cannot pose as a deterministic
    # pass's.
    from .resolve_gates import sanitize_model_rules
    rules = sanitize_model_rules(rules)
    return rules, value_mappings


def _resolve_peer_fields(system_id: str) -> list:
    """The concept-mapped fields of every OTHER onboarded system, the rule
    targets :func:`resolve_identities` offers the model. A system being
    removed is left out (``catalog.removed_ids``: the graph marker, which
    this runtime's catalog may not reflect, or the catalog state): it keeps
    its fields until the removal's last step."""
    from .catalog import REMOVAL_STARTED_COLUMN, removed_ids
    onboarded_ids = catalog.get_onboarded_ids()
    rows = neptune_query(
        "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field)-[r:MAPS_TO]->(c:Concept) "
        "WHERE s.id <> $sid "
        "RETURN s.id AS sys_id, s.name AS sys_name, s.schema AS sys_schema, "
        "f.name AS field_name, c.id AS concept, se.name AS table_name, " + REMOVAL_STARTED_COLUMN,
        {'sid': system_id}
    )
    served = onboarded_ids - removed_ids(rows, 'sys_id', catalog)
    return [f for f in rows or [] if f.get('sys_id') in served]


def _without_peers_being_removed(system_id: str, rules, value_mappings) -> tuple:
    """``(rules, value_mappings)`` without those naming a system whose removal
    started after :func:`_resolve_peer_fields` read the peers (while the
    model answered): the marker is read again here, just before the session
    stores them (the same filter as the wizard's resolve job,
    ``onboard._drop_removed_candidates``)."""
    from .onboard import _drop_removed_candidates
    kept = _drop_removed_candidates(
        {'rules': rules, 'value_mappings': value_mappings}, system_id, _peers_being_removed(system_id))
    return kept['rules'], kept['value_mappings']


def _peers_being_removed(system_id: str) -> set:
    """The ids and display names of the systems whose removal started, read now."""
    from .catalog import REMOVING_SYSTEMS_READ, removing_system_aliases
    # Model-written fields: matched by id or display name, never a healthy id.
    return removing_system_aliases(neptune_query(REMOVING_SYSTEMS_READ), catalog) - {system_id}


def _gated_without_peers_being_removed(system_id: str, rules, value_mappings, withheld) -> tuple:
    """:func:`_without_peers_being_removed` after the gate pass, its withheld
    rows too: the pass adds the measured pairs the model left out
    (``resolve_gates.measured_pair_rules``), measured on the peers
    :func:`_resolve_peer_fields` read before the model answered (the wizard's
    resolve job reads the marker once, after every pass)."""
    from .onboard import _drop_removed_candidates
    kept = _drop_removed_candidates({'rules': rules, 'value_mappings': value_mappings,
                                     'rejected_rules': withheld}, system_id,
                                    _peers_being_removed(system_id))
    return kept['rules'], kept['value_mappings'], kept.get('rejected_rules') or []


def _gate_chat_rules(system_id: str, rules, mappings, existing_fields, pairs=None) -> tuple:
    """``(rules, withheld, partial)``: the chat's resolve rules judged as the wizard's
    resolve job judges its own. The model's rules are measured with the
    wizard's sampler and evidence passes (``onboard.measure_chat_rules``: the
    candidate field pairs and the pairs the rules name, measured-empty pairs,
    the target search, a translation's check), then go through the wizard's
    gate pass (``onboard.gate_resolve_candidates``: own source, the measured
    pairs the model left out, category, evidence, the identifier-core
    affinity pass on the same mappings, site, storability).
    ``existing_fields``: the peer fields the model was shown. ``partial``:
    the fields the measurement's vendor read budget left unsampled
    (``measurement_partial``). ``pairs``: the candidate pairs
    :func:`resolve_identities` measured before its model answered
    (``onboard.measure_chat_rules``)."""
    from .models import OnboardResolveRequest
    from .onboard import gate_resolve_candidates, measure_chat_rules
    rules = [r for r in rules or []]
    affinity_body = OnboardResolveRequest(
        system_id=system_id, mappings=[m for m in mappings or [] if isinstance(m, dict)])
    result = {'rules': rules, **measure_chat_rules(system_id, mappings, existing_fields,
                                                   rules, pairs)}
    gated = gate_resolve_candidates(system_id, result, mappings, cat=catalog,
                                    affinity_body=affinity_body, measured_pairs=True)
    return gated['rules'], gated.get('rejected_rules') or [], gated.get('measurement_partial') or []


@tool
@_unless_removal_failed
def resolve_identities(system_id: str) -> dict:
    """Generate cross-system identity resolution rules. Must call map_fields first."""
    # Durable precondition (load-bearing gate): the session must exist, be in
    # the 'mapped' state, and carry mappings. Re-checked here because any
    # gateway-cred holder can call this tool directly (plan section 5).
    store = _store()
    try:
        session = store.get_session(system_id)
    except SessionNotFoundError:
        return {'error': 'No mappings available. Call map_fields first.'}
    mappings = session.get('mappings')
    if not mappings or session.get('status') != STATUS_MAPPED:
        return {'error': 'No mappings available. Call map_fields first.'}
    return _resolve_session(system_id, store, session, mappings)


def _resolve_session(system_id: str, store, session: dict, mappings: list) -> dict:
    """:func:`resolve_identities` past its preconditions: the peers, the
    candidate pairs measured, the prompt, the model, the gate pass, then the
    session. The candidate pairs are measured before the model answers, as
    the wizard's concept pass measures them before its model call
    (``onboard._do_resolve``), so both paths judge the same pairs."""
    existing_fields = _resolve_peer_fields(system_id)
    if not existing_fields:
        # No cross-system rules needed, but onboarding still advances to
        # 'resolved' so provisioning can proceed.
        store.advance_status(system_id, STATUS_MAPPED, STATUS_RESOLVED,
                             rules=[], value_mappings=[])
        return {'rules': [], 'message': 'No other systems onboarded yet — no cross-system rules needed.'}
    from .onboard import _measure_resolve_pairs
    pairs = _measure_resolve_pairs(system_id, mappings, existing_fields)

    from .onboard import _qualified_field  # one spelling: table.column, never the table twice
    mappings_text = '\n'.join(f"  {_qualified_field(m.get('table'), m.get('field'))} -> "
                              f"{m.get('concept')}" for m in mappings)
    existing_text = '\n'.join(
        f"  {r.get('sys_id')}: {r.get('table_name')}.{r.get('field_name')} -> {r.get('concept')}"
        for r in existing_fields
    )
    sample_text = _sample_new_system_values(mappings, session.get('schema', ''))
    target_sample_text = _sample_target_system_values(existing_fields)
    prompt = _build_resolve_prompt(system_id, mappings_text, existing_text,
                                   sample_text, target_sample_text)
    text = bedrock_call(prompt)
    rules, value_mappings = _parse_resolve_response(text)
    rules, value_mappings = _without_peers_being_removed(system_id, rules, value_mappings)
    rules, withheld, partial = _gate_chat_rules(system_id, rules, mappings, existing_fields, pairs)
    rules, value_mappings, withheld = _gated_without_peers_being_removed(
        system_id, rules, value_mappings, withheld)

    # Advance mapped->resolved, persisting rules + value mappings durably
    # (conditional write: succeeds only if the session is still 'mapped').
    store.advance_status(system_id, STATUS_MAPPED, STATUS_RESOLVED,
                         rules=rules, value_mappings=value_mappings)
    answer = {'rules': rules, 'value_mappings': value_mappings, 'total_rules': len(rules),
              'total_mappings': len(value_mappings)}
    if withheld:
        answer['rejected_rules'] = withheld
    if partial:
        answer['measurement_partial'] = partial
    return answer


@tool
@_unless_removal_failed
def provision_system(system_id: str) -> dict:
    """Provision the system: write it to the knowledge graph and start its
    agent. Must call resolve_identities first.

    A named slot in ``ONBOARD_TOOLS`` only. The app runs its own
    ``provision_system`` (``onboard_chat_lifecycle.provision_system``) in its
    place in local mode (``in_app_tools``) and in cloud mode
    (``CLOUD_APP_TOOLS``), and the onboarding runtime does not export it
    (``onboard_chat_lifecycle.NOT_ON_GATEWAY``). Called directly it changes
    nothing and says so.
    """
    return {'error': f"'{system_id}' was not provisioned: provisioning runs in the app. "
                     "Use Provision in the Onboard view or the onboarding chat.",
            'reason': 'runs_in_app', 'system_id': system_id}


@tool
@_unless_removal_failed
def verify_system(system_id: str) -> dict:
    """Run verification queries against the newly provisioned system."""
    from .onboard import onboard_verify
    from .models import VerifyRequest
    import asyncio
    # Schema is menu identity; read it from the durable session when present,
    # else from the catalog entry (a not-yet/already-reaped session).
    schema = ''
    try:
        schema = _store().get_session(system_id).get('schema', '')
    except SessionNotFoundError:
        entry = catalog.lookup(system_id)
        schema = entry.get('schema', '') if entry else ''
    body = VerifyRequest(system_id=system_id, schema=schema)
    # Run in a new event loop to avoid conflicts with existing running loops
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(onboard_verify(body))
        loop.close()
    except Exception as e:
        result = {"tables_accessible": 0, "total_rows": 0,
                  **public_error(e, "onboard_agent.verify_system")}
    return result


#: ``SYSTEM_ID`` of the onboarding AgentCore runtime container
#: (``onboarding_runtime_server.TARGET_NAME``, dispatched by ``runtime_app``).
_ONBOARDING_RUNTIME_SYSTEM_ID = "onboarding"


@tool
def reset_system(system_id: str) -> dict:
    """Start a system over or remove it, and relay the answer.

    A system that is not onboarded is never removed here. A chat onboarding of
    it that stopped part-way is started over: only its onboarding session is
    cleared, so discover can run again; its registration and connection stay.
    A discover still in progress is left alone (try again shortly), and a
    provision that stopped before it finished is marked failed and cleared.
    With nothing to start over, the answer points to the system's card in the
    Onboard view.

    An onboarded system is removed completely: its knowledge-graph data,
    its agent runtime and gateway target, and its registration (a system the
    user connected has its stored credentials scheduled for deletion after 7
    days), the same removal as Remove system in the Onboard view with "Also
    forget the connection registration" checked. The removal runs in the
    app and takes a few minutes: the call answers at once with status
    'removing'; tell the user it takes a few minutes and call reset_system
    again with the same system_id to check on it ('removing' while it runs,
    'reset' once it finished, or why it did not finish). A check starts
    nothing. It is refused, with a reason to relay, while the system is
    being provisioned or its connection is being changed, while another
    removal or a reset of all systems runs, and when an earlier removal that
    kept the registration did not finish. A removal that did not finish
    answers that it should be removed again.

    It is not the way to run discover again on an onboarded system: the app
    removes an onboarded system completely, as above, so the system the user
    wanted re-onboarded would be gone. A re-onboarding goes on with the next
    step discover's answer names; to start an onboarded system's onboarding
    over, the user removes it with Remove system on its card in the Onboard
    view, keeping the connection registration (the default), and onboards it
    again.

    Runs the app's own removal (``onboard.start_removal_for_chat``,
    :func:`_reset_in_app`). In the onboarding runtime container that removal
    cannot run (the container may not delete agent runtimes or targets and
    does not hold the app's catalog or locks), so there it starts over a
    half-done onboarding of a system that is not onboarded (its session
    discovered, mapped, resolved, failed or onboarded, or a discover that
    stopped before it saved its session; a recent one is left alone, and so
    is a provision) and otherwise removes nothing and names the action on
    the system's card."""
    if os.environ.get("SYSTEM_ID", "") == _ONBOARDING_RUNTIME_SYSTEM_ID:
        return _reset_in_onboarding_runtime(system_id)
    return _reset_in_app(system_id)


def _reset_in_app(system_id: str) -> dict:
    """``reset_system`` in the app process (also the app's own chat tool,
    ``onboard_chat_lifecycle.reset_system``).

    While a removal of the system runs, the call reports it and starts
    nothing (``onboard.chat_removal_check``: it waits a bounded time for the
    removal to end, then answers its progress or how it ended). Refused at
    once, with the removal's own reason, while a provision of the system
    runs in this process (the wizard's or the chat's). The card's full
    removal, which forgets a user registration and schedules its own secret
    for deletion, is started (``onboard.start_removal_for_chat``: the card's
    background removal; the call answers at once, and the removal deletes
    the onboarding session once the system's graph data is gone) only when a checked read
    finds the system's System node marked onboarded and either carrying no
    removal marker or one a full removal left (:func:`_chat_may_remove`); a
    chat removal that did not finish is first reported once
    (``onboard.chat_removal_failure_answer``). A node with a removal marker
    that fails that test is handed to the card's Remove again, which repeats
    the removal the way it was asked (:func:`_unfinished_removal_handoff`).
    A system with no System node, or one that holds only its connection (an
    Edit or a scan budget before the first provision writes one), is never
    removed from the chat; its chat onboarding is started over instead, or
    the chat's finished removal of it is reported
    (:func:`_start_over_not_onboarded`). A graph that cannot be read
    refuses, with nothing changed."""
    from .onboard import (_provision_running, chat_removal_check, chat_removal_failure_answer,
                          start_removal_for_chat)
    running = chat_removal_check(system_id)
    if running is not None:
        return running
    if _provision_running(system_id):
        return _being_provisioned(system_id)
    try:
        node = _removal_marker_row(system_id)
    except Exception as exc:  # noqa: BLE001 - db.GraphQueryError or a transport error
        return _graph_unavailable_refusal(system_id, exc)
    plan = _reset_plan(node)
    if plan == RESET_REMOVE_AGAIN:
        return _unfinished_removal_handoff(system_id, node)
    if plan == RESET_STARTS_OVER:
        return _start_over_not_onboarded(system_id)
    return (chat_removal_failure_answer(system_id)
            or start_removal_for_chat(system_id))


#: What reset_system does with a system, by its System node
#: (:func:`_reset_plan`).
RESET_STARTS_OVER = 'start_over'      # not onboarded, no removal marker
RESET_REMOVES = 'remove'              # the card's full removal (or finishing one)
RESET_REMOVE_AGAIN = 'remove_again'   # a marked removal only its card's Remove again repeats


def _reset_plan(node: dict) -> str:
    """What reset_system does with the system whose System node row this is
    (:data:`_REMOVAL_MARKER_QUERY`; ``{}``: no node), decided with no side
    effect, in the app (:func:`_reset_in_app`), in the onboarding runtime
    (:func:`_reset_in_onboarding_runtime`) and in the advice the other tools
    give (:func:`_start_over_advice`): :data:`RESET_REMOVES` when the chat
    may run the card's full removal (:func:`_chat_may_remove`),
    :data:`RESET_REMOVE_AGAIN` for any other node a removal marked
    (:func:`_removal_marked`), else :data:`RESET_STARTS_OVER`."""
    if _chat_may_remove(node):
        return RESET_REMOVES
    if _removal_marked(node):
        return RESET_REMOVE_AGAIN
    return RESET_STARTS_OVER


def _removal_marked(node: dict) -> bool:
    """Whether a System node row (:data:`_REMOVAL_MARKER_QUERY`) carries the
    marker a removal writes before it removes anything (``removal_incomplete``).
    A card removal marks its own node; a reset of all systems marks EVERY
    node, a node that holds only a connection included, so the marker alone
    never shows the system was onboarded."""
    return (node or {}).get('removal_incomplete') is True


def _chat_may_remove(node: dict) -> bool:
    """Whether the chat may run the card's full removal of the system whose
    System node row this is: the node carries the provision's marker
    (``catalog.node_is_onboarded``; a node that holds only a connection,
    written by ``connection_resolver.write_connection`` or
    ``write_scan_budget`` before the first provision, does not), and it
    carries no removal marker, or one a full removal left
    (``removal_full``), so finishing it as a full removal repeats what was
    asked. A default removal that did not finish kept the registration; only
    its card's Remove again repeats it that way."""
    from .catalog import node_is_onboarded
    if not node_is_onboarded(node):
        return False
    return not _removal_marked(node) or node.get('removal_full') is True


def _unfinished_removal_handoff(system_id: str, node: dict) -> dict:
    """The chat's answer for a removal that did not finish and that the chat
    does not finish (:func:`_chat_may_remove`): nothing is removed. The
    system's catalog entry is shown as a removal that did not finish, with the
    mode the node recorded (``catalog.ensure_removal_failed``, as a restart
    and the other onboarding tools show it), so its card offers Remove again,
    which repeats the removal that way."""
    catalog.ensure_removal_failed({**node, 'id': system_id})
    logger.info("reset_system for %s: an unfinished removal handed to its card (full=%s)",
                system_id, node.get('removal_full') is True)
    return {'error': f"An earlier removal of '{system_id}' (or a reset of all systems) did not "
                     "finish, so it was not removed from the chat. Finish it with Remove again "
                     "on its card in the Onboard view: it is repeated the way it was asked (a "
                     "default removal keeps the registration and its connection). Nothing was "
                     "removed.",
            'reason': 'remove_in_app', 'system_id': system_id}


def _being_provisioned(system_id: str) -> dict:
    from .connection_lock import PROVISION
    from .onboard import _reset_busy_detail
    return {'error': f"'{system_id}' was not removed: {_reset_busy_detail(system_id, PROVISION)}",
            'reason': 'removal_refused', 'system_id': system_id}


#: How long a ``discovering`` session counts as a discover still writing it.
#: Discover writes ``discovering`` only after its source read is done, then
#: advances to ``discovered`` at once, so an older one was left by a discover
#: that stopped between those two writes (a session too large to store, a
#: crash) and no discover will finish it.
DISCOVERING_STALE_AFTER_SECONDS = 120


def _start_over_not_onboarded(system_id: str) -> dict:
    """``reset_system`` in the app for a system with no System node marked
    onboarded and no removal marker (:func:`_chat_may_remove`). Holds
    the system's provision claim, so no provision starts meanwhile and one
    running here refuses. Touches only the onboarding session, by its status:
    ``discovering`` is left alone while recent (:func:`_session_age_s`) and
    cleared once stale; ``provisioning`` (no provision holds it here: the app
    restarted mid-provision) is marked ``failed`` first; every other status is
    cleared. Each change is conditional on the status read. With no session,
    nothing is changed: the answer is how the chat's removal of the system
    ended when its job record says it finished
    (``onboard.chat_removal_done_answer``), else it names the card action."""
    from .onboard import ProvisionBusyError, _claim_provision, chat_removal_done_answer
    try:
        claim = _claim_provision(system_id)
    except ProvisionBusyError:
        return _being_provisioned(system_id)
    with claim:
        try:
            session = _store().get_session(system_id)
        except SessionNotFoundError:
            return chat_removal_done_answer(system_id) or _nothing_to_start_over(system_id)
        except Exception as exc:  # noqa: BLE001 - unknown state: change nothing
            logger.warning("reset_system for %s: onboarding session unreadable: %s",
                           system_id, type(exc).__name__)
            return _session_unavailable(system_id)
        status = str(session.get('status') or '')
        if status == STATUS_DISCOVERING:
            age = _session_age_s(session)
            if age is not None and age < DISCOVERING_STALE_AFTER_SECONDS:
                return _still_discovering(system_id, age)
        if status == STATUS_PROVISIONING:
            refused = _mark_lost_provision_failed(system_id)
            if refused:
                return refused
            answer = _clear_session_if_status(system_id, STATUS_FAILED)
            if answer.get('status') != 'session_cleared':
                return answer
            return {**answer, 'previous_status': STATUS_PROVISIONING,
                    'message': f"The provision of '{system_id}' had stopped before it finished "
                               "(the app restarted), so its onboarding was cleared. Run discover "
                               "again to start over. Its registration and connection were not "
                               "changed."}
        return _clear_session_if_status(system_id, status)


def _session_age_s(session: dict) -> "float | None":
    """Seconds since the session was last written, or None when unknown."""
    from datetime import datetime, timezone
    stamp = session.get('updated_at') or session.get('created_at')
    try:
        written = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - written).total_seconds()


def _still_discovering(system_id: str, age: float) -> dict:
    return {'error': f"'{system_id}' is still being discovered (its onboarding session was "
                     f"written {int(age)} s ago), so nothing was changed. Try again shortly.",
            'reason': 'discovering', 'system_id': system_id}


def _mark_lost_provision_failed(system_id: str) -> "dict | None":
    """Move a ``provisioning`` session no provision here holds to ``failed``
    (conditional on ``provisioning``). None when done; else the refusal."""
    from .onboarding_session import OutOfOrderError
    try:
        _store().finalize(system_id, STATUS_FAILED, failure_code='provision_interrupted',
                          failure_reason="The provision stopped before it finished (the app "
                                         "restarted).")
    except OutOfOrderError:
        return _session_changed(system_id, STATUS_PROVISIONING)
    except Exception as exc:  # noqa: BLE001 - the session is unchanged: say so
        logger.warning("reset_system for %s: lost provision not marked failed: %s",
                       system_id, type(exc).__name__)
        return _session_unavailable(system_id)
    logger.info("reset_system for %s: a provision lost in a restart was marked failed", system_id)
    return None


def _nothing_to_start_over(system_id: str) -> dict:
    """The answer for a system with no System node marking it onboarded and
    no onboarding session: nothing was changed; the card action that applies
    is named."""
    from .catalog import REMOVAL_FAILED
    from .connection_resolver import is_user_registration
    entry = catalog.lookup(system_id)
    if entry is None:
        return {'error': f"Unknown system_id '{system_id}'"}
    unchanged = " Nothing was changed."
    if entry.get('status') == REMOVAL_FAILED:
        text = (f"The earlier removal of '{system_id}' did not finish. Finish it with Remove "
                "again on its card in the Onboard view.")
    elif entry.get('status') == 'onboarded':
        text = (f"The knowledge graph does not mark '{system_id}' onboarded, so it was not "
                "removed from the chat. Use Remove system on its card in the Onboard view.")
    elif is_user_registration(entry):
        text = (f"'{system_id}' is registered and not onboarded, and has no half-done "
                "onboarding to start over. To forget its registration, use Remove "
                "registration on its card in the Onboard view.")
    else:
        return {'error': f"System '{system_id}' is not onboarded"}
    return {'error': text + unchanged, 'reason': 'remove_in_app', 'system_id': system_id}


#: Statuses of an onboarding session of a system that is not onboarded that
#: the onboarding runtime's start-over clears, as the app's does
#: (:func:`_start_over_not_onboarded`): ``provisioning`` is claimed only from
#: ``resolved`` (``OnboardingSessionStore.claim_provisioning``), ``failed`` is
#: a provision's terminal write, and an ``onboarded`` session whose System
#: node is not onboarded was left by a removal that kept the registration.
#: ``discovering`` is cleared only once stale
#: (:data:`DISCOVERING_STALE_AFTER_SECONDS`: a discover may still be writing
#: a recent one). ``provisioning`` is left alone: this container cannot tell
#: whether the app's provision of the system runs.
_RESTARTABLE_SESSION_STATUSES = frozenset(
    {STATUS_DISCOVERED, STATUS_MAPPED, STATUS_RESOLVED, STATUS_FAILED, STATUS_ONBOARDED})

def _start_over_session(system_id: str, restartable: frozenset = _RESTARTABLE_SESSION_STATUSES
                        ) -> tuple[dict | None, str]:
    """Clear a half-done chat onboarding of ``system_id``: delete its durable
    session when its status is one of ``restartable``
    (:data:`_RESTARTABLE_SESSION_STATUSES` unless the caller says otherwise)
    or it is a stale ``discovering`` (a recent one is answered as still
    being discovered, unchanged), with a delete conditional on that status,
    so a provisioning claim that wins in between makes the delete fail and
    nothing changes. Touches only the session. The caller has checked the
    system is not onboarded.

    Returns ``(answer, status)``: the tool answer (cleared, or an error that
    changed nothing), or ``None`` when there is no session to clear; and the
    status read (``""`` without a session)."""
    store = _store()
    try:
        session = store.get_session(system_id)
    except SessionNotFoundError:
        return None, ''
    except Exception as exc:  # noqa: BLE001 - unknown state: change nothing
        logger.warning("reset_system for %s: onboarding session unreadable: %s",
                       system_id, type(exc).__name__)
        return _session_unavailable(system_id), ''
    status = str(session.get('status') or '')
    if status == STATUS_DISCOVERING:
        age = _session_age_s(session)
        if age is not None and age < DISCOVERING_STALE_AFTER_SECONDS:
            return _still_discovering(system_id, age), status
        return _clear_session_if_status(system_id, status), status
    if status not in restartable:
        return None, status
    return _clear_session_if_status(system_id, status), status


def _clear_session_if_status(system_id: str, status: str) -> dict:
    """Delete the session of ``system_id`` while its status is still
    ``status`` (a conditional delete: a provisioning claim that wins in
    between makes it fail and nothing changes). Returns the tool answer."""
    from .onboarding_session import OutOfOrderError
    try:
        _store().delete_session_if_status(system_id, status)
    except OutOfOrderError:
        return _session_changed(system_id, status)
    except Exception as exc:  # noqa: BLE001 - the session is still there: say so
        logger.warning("reset_system for %s: onboarding session not cleared: %s",
                       system_id, type(exc).__name__)
        return _session_unavailable(system_id)
    logger.info("reset_system for %s: onboarding session (%s) cleared; nothing else changed",
                system_id, status)
    return {'status': 'session_cleared', 'system_id': system_id, 'previous_status': status,
            'message': f"The half-done onboarding of '{system_id}' was cleared (its session was "
                       f"'{status}'). Run discover again to start over. Its registration and "
                       "connection were not changed."}


def _session_changed(system_id: str, status: str) -> dict:
    logger.warning("reset_system for %s: onboarding session moved on from %s while it "
                   "was being cleared; not cleared", system_id, status)
    return {'error': f"'{system_id}' was not started over: its onboarding session changed "
                     f"(it was '{status}') while it was being cleared, for example because "
                     "provisioning started. Nothing was changed; check its state and try "
                     "again.",
            'reason': 'session_changed', 'system_id': system_id}


def _session_unavailable(system_id: str) -> dict:
    return {'error': f"The onboarding session of '{system_id}' could not be read or cleared, so "
                     "nothing was changed. Try again in a moment.",
            'reason': 'session_unavailable', 'system_id': system_id}


def _reset_in_onboarding_runtime(system_id: str) -> dict:
    """``reset_system`` in the onboarding runtime container. The System node
    (checked read: a failure changes nothing) decides, with the app's own
    decision (:func:`_reset_plan`), whether the system is started over: not
    onboarded (``catalog.node_is_onboarded``: a node that holds only a
    connection is not) and no removal marker (:func:`_removal_marked`). Then
    a half-done chat onboarding is started over
    (:func:`_start_over_session`). Otherwise nothing is changed and the answer
    names the action that exists on the system's card
    (:func:`_card_handoff`)."""
    try:
        node = _removal_marker_row(system_id)
    except Exception as exc:  # noqa: BLE001 - db.GraphQueryError or a transport error
        return _graph_unavailable_refusal(system_id, exc)
    status = ''
    if _reset_plan(node) == RESET_STARTS_OVER:
        answer, status = _start_over_session(system_id)
        if answer is not None:
            return answer
    return _card_handoff(system_id, node, catalog.lookup(system_id), status)


def _card_handoff(system_id: str, node: dict, entry: dict | None, session_status: str) -> dict:
    """The runtime's answer when there is nothing it may change: the card
    action in the Onboard view that exists for this system, when this
    container can tell (its catalog holds only systems with a System node, so
    a node-less registration the app owns is not in it: then both actions are
    named). Nothing was changed."""
    from .catalog import node_is_onboarded
    from .connection_resolver import is_user_registration
    unchanged = " Nothing was changed."
    if node.get('removal_incomplete') is True:
        reason = 'remove_in_app'
        text = (f"The earlier removal of '{system_id}' did not finish. Finish it with Remove "
                "again on its card in the Onboard view.")
    elif node_is_onboarded(node):
        reason = 'remove_in_app'
        text = (f"'{system_id}' is onboarded. Remove it with Remove system on its card in the "
                "Onboard view (check \"Also forget the connection registration\" to forget it "
                "too).")
    elif session_status == STATUS_PROVISIONING:
        reason = 'provisioning'
        text = (f"'{system_id}' is being provisioned. Once it is onboarded, remove it with "
                "Remove system on its card in the Onboard view.")
    elif is_user_registration(entry):
        reason = 'remove_in_app'
        text = (f"'{system_id}' is registered and not onboarded. To forget it, use Remove "
                "registration on its card in the Onboard view.")
    elif entry is None:
        # A registration the app owns has no System node until it is
        # provisioned, so this container cannot see it: do not say which card
        # action applies, name both.
        reason = 'remove_in_app'
        text = (f"Found nothing to reset from the chat for '{system_id}'. If it is in the "
                "Onboard view, use its card there: Remove registration for a registered "
                "system, Remove system for an onboarded one.")
        if session_status:
            text += f" Its onboarding session is '{session_status}'."
    else:
        reason = 'nothing_to_remove'
        text = f"'{system_id}' is not onboarded and has no half-done onboarding to start over."
        if session_status:
            text += f" Its onboarding session is '{session_status}'."
    logger.info("reset_system for %s: nothing changed in the onboarding runtime (%s)",
                system_id, reason)
    return {'error': text + unchanged, 'reason': reason, 'system_id': system_id}


#: The guard's node read (``neptune_query_checked``: a graph failure raises).
#: tests/unit/conftest.py answers it offline from the faked node.
_REMOVAL_MARKER_QUERY = (
    "MATCH (s:System {id: $sid}) RETURN s.removal_incomplete AS removal_incomplete, "
    "s.name AS name, s.plant AS plant, s.schema AS schema, "
    "s.connection_type AS connection_type, s.system_type AS system_type, "
    "s.status AS status, s.removal_full AS removal_full, "
    "s.connection_origin AS connection_origin, "
    "s.connection_config AS connection_config, s.secret_arn AS secret_arn")


def _removal_running_refusal(system_id: str) -> dict | None:
    """The refusal (``reason: reset_in_progress``, the shape the HTTP writers
    answer, ``onboard._reset_conflict_or_none``) while a removal of
    ``system_id`` or a reset of all systems runs, else ``None``. A removal
    deletes the system's graph data and then its onboarding session before
    its job says done, and Reset All does that for every system before it
    puts the catalog back, so a tool run in between would start an
    onboarding the reset is still tearing down. Both hold the shared reset
    lease while they run. In the app the HTTP writers' checks decide
    (``onboard._reset_lease_state``, ``onboard._reset_in_progress``); the
    onboarding runtime container reads the lease from the table they share
    (:func:`_runtime_removal_refusal`). A lease that could not be read
    refuses (``reason: reset_state_unavailable``, retryable)."""
    if os.environ.get("SYSTEM_ID", "") == _ONBOARDING_RUNTIME_SYSTEM_ID:
        return _runtime_removal_refusal(system_id)
    from .onboard import (_LEASE_SINGLE, _LEASE_UNAVAILABLE, _reset_in_progress,
                          _reset_lease_state)
    state = _reset_lease_state(system_id)
    if state == reset_lease.ALL:
        return _reset_running_answer(system_id, every_system=True)
    if state == _LEASE_SINGLE or _reset_in_progress(system_id):
        return _reset_running_answer(system_id)
    return _reset_state_unavailable(system_id) if state == _LEASE_UNAVAILABLE else None


def _runtime_removal_refusal(system_id: str) -> dict | None:
    """:func:`_removal_running_refusal` in the onboarding runtime container,
    which holds none of the app's reset state: the shared reset lease, read
    from the table with a consistent read and never from memory (empty in
    this process). A read that fails refuses rather than letting the tool run
    without knowing; the next call reads the table again."""
    from .job_store import JobStoreUnavailableError
    try:
        scope = reset_lease.reset_holding(_shared_reset_jobs(), system_id, shared_only=True)
    except JobStoreUnavailableError as exc:
        logger.warning("removal guard: reset state for %s not read: %s", system_id, exc)
        return _reset_state_unavailable(system_id)
    if scope == reset_lease.ALL:
        return _reset_running_answer(system_id, every_system=True)
    return _reset_running_answer(system_id) if scope else None


def _reset_state_unavailable(system_id: str) -> dict:
    return {'error': f"Whether '{system_id}' is being removed could not be checked, so "
                     "nothing was done. Try again in a moment.",
            'reason': 'reset_state_unavailable', 'system_id': system_id}


def _reset_running_answer(system_id: str, *, every_system: bool = False) -> dict:
    what = (f"All systems are being reset, so nothing was done for '{system_id}'. A reset"
            if every_system else
            f"'{system_id}' is being removed, so nothing was done. A removal")
    return {'error': f"{what} takes a few minutes: tell the user, and try again once it "
                     "has finished.",
            'reason': 'reset_in_progress', 'system_id': system_id}


_shared_reset_jobs_singleton = None


def _shared_reset_jobs():
    """The app's reset leases as the onboarding runtime container reads them:
    ``JobStore("reset")``, the store ``onboard._RESET_LEASES`` writes through
    to the shared onboarding-sessions table (lazy, so importing this module
    never touches AWS). Read only with consistent strict reads."""
    global _shared_reset_jobs_singleton
    if _shared_reset_jobs_singleton is None:
        from .job_store import JobStore
        _shared_reset_jobs_singleton = JobStore("reset")
    return _shared_reset_jobs_singleton


def _removal_failed_refusal(system_id: str) -> dict | None:
    """The app's refusal (``onboard._reset_conflict_or_none``) for a system whose
    removal did not finish, else ``None``: no system-active tool may rebuild a
    half-removed system; only ``reset_system`` finishes it. The catalog decides
    first; a runtime container's entry can predate the marker (it hydrated the
    system before the app's removal failed), so otherwise the node's
    ``removal_incomplete`` is read with ``neptune_query_checked`` and, when set,
    the entry is built ``removal_failed`` through ``catalog.ensure_removal_failed``.
    A graph read failure is never "not marked": the call is refused
    (``reason: graph_unavailable``, retryable) before the tool runs."""
    from .catalog import REMOVAL_FAILED
    if (catalog.lookup(system_id) or {}).get('status') != REMOVAL_FAILED:
        try:
            row = _removal_marker_row(system_id)
        except Exception as exc:  # noqa: BLE001 - db.GraphQueryError or a transport error
            return _graph_unavailable_refusal(system_id, exc)
        if row.get('removal_incomplete') is not True:
            return None
        catalog.ensure_removal_failed({**row, 'id': system_id})
    return _removal_failed_answer(system_id)


def _removal_marker_row(system_id: str) -> dict:
    """The System node's marker row (``{}``: no node), read with
    ``neptune_query_checked``: a graph failure raises."""
    from .db import neptune_query_checked
    rows = neptune_query_checked(_REMOVAL_MARKER_QUERY, {'sid': system_id}) or []
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def _graph_unavailable_refusal(system_id: str, exc: Exception) -> dict:
    logger.warning("removal guard: node read for %s failed: %s", system_id, type(exc).__name__)
    return {'error': f"The knowledge graph could not be read, so '{system_id}' could not be "
                     "checked for an unfinished removal and nothing was done. "
                     "Try again in a moment.",
            'reason': 'graph_unavailable', 'system_id': system_id}


def _removal_failed_answer(system_id: str) -> dict:
    return {'error': f"The removal of '{system_id}' did not finish. Remove it again, "
                     "then onboard it.",
            'reason': 'removal_failed', 'system_id': system_id}


def _registration_id_refusal(system_id: str) -> dict | None:
    """Whether a USER registration may claim ``system_id``: ``None`` only on a
    checked no-row (no catalog entry, no System node). The catalog can be cold
    (a runtime container whose fail-soft hydration found nothing during a graph
    outage), so the node is read with ``neptune_query_checked``: a marked node
    refuses (``removal_failed``: claiming it would restore and overwrite the
    half-removed system's pending-deletion secret), a read failure refuses
    (``graph_unavailable``), and an existing entry or node is the existing
    "already exists" error (:func:`_already_registered_answer`). Nothing is
    written here."""
    from .catalog import REMOVAL_FAILED
    entry = catalog.lookup(system_id)
    if (entry or {}).get('status') == REMOVAL_FAILED:
        return _removal_failed_answer(system_id)
    try:
        row = _removal_marker_row(system_id)
    except Exception as exc:  # noqa: BLE001 - db.GraphQueryError or a transport error
        return _graph_unavailable_refusal(system_id, exc)
    if row.get('removal_incomplete') is True:
        return _removal_failed_answer(system_id)
    if entry or row:
        return _already_registered_answer(system_id, entry, row)
    return None


def _already_registered_answer(system_id: str, entry: dict | None, row: dict) -> dict:
    """The refusal for an id that is taken. A System node with no catalog entry
    and no onboarding marker (``catalog.node_is_onboarded``) is the connection
    settings an earlier registration of this id left on the graph: an Edit
    before onboarding writes them, and a restart drops the registration itself.
    The catalog shows no such system, so this refusal says what holds the id
    instead of "already exists"."""
    from .catalog import node_is_onboarded
    if not entry and row and not node_is_onboarded(row):
        return {'error': f"A previous registration of '{system_id}' is still recorded, so "
                         "this id can't be registered again. Choose a different system id."}
    return {'error': f'System {system_id} already exists'}


def _registration_entry(system_id: str, name: str, system_type: str, plant: str,
                        connection_type: str, schema: str, connection_string: str,
                        connection_config: dict | None) -> dict:
    """The catalog entry for a newly registered system. Stores ONLY the
    non-secret allowlist subset of ``connection_config`` (the UI system-card
    summary reads it); secrets (external_id / token / ...) are dropped here —
    they flow to Secrets Manager, never the catalog. Empty-after-sanitize ⇒
    key omitted (matches blank input). The plant is stored normalized
    (:func:`catalog.normalize_plant`)."""
    from .catalog import normalize_plant
    entry = {
        'id': system_id, 'name': name, 'system_type': system_type,
        'plant': normalize_plant(plant),
        'connection_type': connection_type, 'schema': schema, 'status': 'registered',
    }
    if connection_string:
        entry['connection_string'] = connection_string
    if connection_config:
        from .connection_resolver import sanitize_connection_config
        safe_cfg = sanitize_connection_config(connection_config)
        if safe_cfg:
            entry['connection_config'] = safe_cfg
    return entry


def _build_registration_secret(cfg: dict, entry: dict) -> dict:
    """Assemble the Secrets Manager payload for a registration (no IO).

    Starts from the generic ``{external_id, token}`` allowlist, then:
      * A Tulip system may supply the RAW api_key +
        api_secret — encode the HTTP Basic token HERE so the user never computes
        base64(key:secret) by hand (an explicit ``token`` wins). The raw fields
        go ONLY into the secret (never node/catalog/logs); base64 is an encoding.
      * A REST vendor whose credential
        keys are NOT external_id/token (for example client_id/client_secret)
        shapes its own payload via ``RestVendorAdapter.encode_registration_secret``;
        merge its output so those keys land in adt/conn/<id> instead of being
        dropped. None hook (i3x/tulip/generic) → no change. The inline Tulip
        branch could move into a ``_tulip_encode_registration_secret`` hook; it
        stays inline for now to stay byte-identical.
    """
    reg_secret = {k: v for k, v in cfg.items()
                  if k in ('external_id', 'token') and v}
    if not reg_secret.get('token') and cfg.get('api_key') and cfg.get('api_secret'):
        basic = _tulip_basic_token(cfg.get('api_key'), cfg.get('api_secret'))
        if basic:
            reg_secret['token'] = basic
            reg_secret['auth_scheme'] = 'basic'
    ct = (entry or {}).get('connection_type', '')
    if ct:
        try:
            from .rest_vendor_registry import resolve_vendor
            adapter = resolve_vendor(ct)
            if adapter is not None and adapter.encode_registration_secret is not None:
                for k, v in (adapter.encode_registration_secret(cfg) or {}).items():
                    if v and not reg_secret.get(k):
                        reg_secret[k] = v
        except Exception as exc:  # noqa: BLE001 - best-effort; never block registration on the hook
            # Registration continues, but the vendor's credential keys did not
            # reach adt/conn/<id>: say so (exception TYPE only, never values).
            logger.warning("registration: %s hook failed (%s); vendor credentials not "
                           "stored in the registration secret", ct, type(exc).__name__)
    return reg_secret


def _store_registration_secret(system_id: str, connection_config: dict | None,
                               entry: dict) -> str:
    """M4: registration-time secrets (external_id / token / vendor creds) are
    DROPPED by the catalog sanitize, and on a FIRST cross-account provision no
    conn/<id> secret exists yet — so they were silently lost. Upsert them NOW
    into the secret the resolution chain reads: ``adt/conn/<system_id>`` via
    ``connection_resolver._store_connection_secret`` (which, unlike
    ``write_connection``, does NOT MERGE a System node early). The ARN POINTER
    (never a value) goes on the catalog entry. Returns the steward-facing note
    ("" when nothing to store); registration never 500s on a secret failure.
    The payload is assembled by :func:`_build_registration_secret`.
    """
    reg_secret = _build_registration_secret(connection_config or {}, entry)
    if not reg_secret:
        return ""
    # Bind the payload to this registration (origin + system id): the token is
    # then read only through this entry's own pointer
    # (connection_resolver.resolve_registered_token), and a restart re-marks a
    # user system's origin from it (server._restore).
    from .connection_resolver import CONNECTION_ORIGIN_KEY
    if entry.get(CONNECTION_ORIGIN_KEY):
        reg_secret.update({CONNECTION_ORIGIN_KEY: entry[CONNECTION_ORIGIN_KEY],
                           'system_id': system_id})
    try:
        from .connection_resolver import _store_connection_secret, invalidate_secret
        arn = _store_connection_secret(system_id, reg_secret)
        invalidate_secret(arn)
        entry['secret_arn'] = arn
        return ("connection credentials stored securely in Secrets "
                "Manager (adt/conn/" + system_id + ")")
    except Exception as sec_err:  # noqa: BLE001 - registration must not 500; surfaced in note
        logger.warning("connect_new_system: %s: %s writing the credential secret "
                       "(user registration: not registered; demo: continues without "
                       "stored credentials)", system_id, type(sec_err).__name__)
        return (_CRED_WRITE_FAILED_NOTE + " — "
                "re-submit them or store via store_credentials")


#: Prefix of :func:`_store_registration_secret`'s note when the write failed
#: (a demo registration keeps it as its fail-soft note).
_CRED_WRITE_FAILED_NOTE = "WARNING: connection credentials could NOT be stored"

#: A USER registration whose credentials could not be stored is not
#: registered (``_claim_and_store`` removes exactly the entry it claimed).
_CRED_WRITE_FAILED_ERROR = ("The connection credentials could not be stored in Secrets "
                            "Manager, so the system was not registered. Try again.")


def _store_pg_registration_secret(system_id: str, pg_secret: dict, entry: dict) -> str:
    """Write a USER PostgreSQL registration's DSN + credentials to
    ``adt/conn/<system_id>`` (the secret the resolution chain reads), bound to
    this system by the durable origin marker + system id in its payload, and put
    the ARN POINTER on the catalog entry. Returns the steward-facing note, or ""
    when the write failed — the caller then rolls the registration back,
    because a PostgreSQL system with no stored credentials cannot be onboarded.
    Logs the exception TYPE only (its message could echo the payload)."""
    from .connection_resolver import CONNECTION_ORIGIN_KEY, ORIGIN_USER
    payload = {**pg_secret, CONNECTION_ORIGIN_KEY: ORIGIN_USER, 'system_id': system_id}
    try:
        from .connection_resolver import _store_connection_secret, invalidate_secret
        arn = _store_connection_secret(system_id, payload)
        invalidate_secret(arn)
    except Exception as sec_err:  # noqa: BLE001 - the caller reports a clean error
        logger.warning("connect_new_system: %s: %s writing the PostgreSQL credential "
                       "secret", system_id, type(sec_err).__name__)
        return ""
    entry['secret_arn'] = arn
    return ("connection credentials stored securely in Secrets "
            "Manager (adt/conn/" + system_id + ")")


_REST_UNSUPPORTED = ("REST API (OpenAPI) systems are not supported yet: this build cannot "
                     "provision a generic REST connector. Tulip and AWS IoT SiteWise (i3X) "
                     "have their own connection types.")


def _target_error(target: str, connection_type: str) -> str:
    """'' when a USER registration's target passes the SSRF/scheme guard
    (``connection_security.validate_connection_target``), else the reason."""
    from . import connection_resolver as _cr
    try:
        _cr.validate_connection_target(target, connection_type=connection_type)
    except _cr.ConnectionTargetError as exc:
        return f"{connection_type} connection target rejected: {exc}"
    return ""


def _schema_owner(schema: str) -> str:
    """The id of a registered system already using (or, during its credentials
    Edit, reserving) ``schema``, else ''."""
    return next((s.get('id', '') for s in catalog.get_all()
                 if schema in (s.get('schema'), s.get('pending_schema'))), '')


def _prepare_user_pg(connection_string: str, schema: str, connection_config) -> dict:
    """A USER PostgreSQL registration: the DSN (``connection_config["dsn"]``, or a
    ``connection_string``) goes to Secrets Manager only; the schema is derived
    from its search_path (a different supplied schema is refused); a schema
    already registered by another system is refused (records-first sampling is
    keyed by schema); the target must pass the SSRF guard."""
    from .connection_resolver import PG_DSN_CONFIG_KEY, parse_pg_registration_dsn
    cfg = dict(connection_config or {})
    dsn = cfg.pop(PG_DSN_CONFIG_KEY, '') or connection_string
    pg_secret = None
    if dsn:
        try:
            non_secret, pg_secret = parse_pg_registration_dsn(dsn)
        except ValueError as exc:
            return {'error': str(exc)}
        derived = non_secret.pop('schema')
        if schema and schema != derived:
            return {'error': f"schema '{schema}' does not match the connection "
                             f"string's search_path '{derived}'"}
        schema, cfg = derived, {**cfg, **non_secret}
        err = _target_error(dsn, 'PostgreSQL')
        if err:
            return {'error': err}
    owner = _schema_owner(schema) if schema else ''
    if owner:
        return {'error': f"schema '{schema}' is already registered by system {owner}; "
                         "each PostgreSQL system needs its own schema"}
    return {'connection_string': '', 'schema': schema,
            'connection_config': cfg or None, 'pg_secret': pg_secret}


def _prepare_user_registration(connection_type: str, connection_string: str, schema: str,
                               connection_config) -> dict:
    """Validate a USER registration (HTTP route or chat tool) before anything is
    claimed or stored → the (possibly rewritten) registration fields, or
    ``{'error': ...}``. Demo/workshop registrations never come through here."""
    from .connection_resolver import PG_DSN_CONFIG_KEY
    from .connection_security import credential_url_error
    cfg = connection_config if isinstance(connection_config, dict) else {}
    for url in (connection_string, cfg.get('url'), cfg.get('base_url'),
                cfg.get(PG_DSN_CONFIG_KEY)):
        err = credential_url_error(url) if isinstance(url, str) else ''
        if err:
            return {'error': err}
    if connection_type == 'REST API':
        return {'error': _REST_UNSUPPORTED}
    if connection_type == 'PostgreSQL':
        return _prepare_user_pg(connection_string, schema, connection_config)
    if connection_type == 'MCP Server':
        err = _mcp_registration_url_error(connection_string, connection_config or {})
        if err:
            return {'error': err}
    err = _typed_target_error(connection_type, connection_string, connection_config or {})
    if err:
        return {'error': err}
    arn_prep = _prepare_secret_arn_source(connection_type, cfg)
    if 'error' in arn_prep:
        return arn_prep
    return {'connection_string': connection_string, 'schema': schema,
            'connection_config': arn_prep['connection_config'], 'pg_secret': None,
            'secret_arn': arn_prep['secret_arn']}


def _prepare_secret_arn_source(connection_type: str, cfg: dict) -> dict:
    """Validate a request-named ``secret_arn`` credential source.

    The SECOND credential source besides a pasted key: a request may carry a
    ``secret_arn`` in ``connection_config`` to point the system at an EXISTING
    Secrets Manager secret instead of supplying the key inline. It is accepted
    ONLY as :func:`_secret_arn_source_refusal` allows (an opted-in type, an
    allowlisted ARN, a type the deployment enables). The historical refusal of
    a client-supplied ARN stays for every other type.

    On success returns ``{'connection_config': cfg', 'secret_arn': <ARN>}`` where
    ``cfg'`` carries ``credential_source="secret_arn"`` so the node is marked
    ARN-sourced (the resolver then pins url/workspace from the secret at use
    time) and the raw ``secret_arn`` is removed from the config blob (it travels
    as the entry's pointer, not in ``connection_config``). No ARN in the request
    ⇒ the config is returned unchanged with an empty pointer. ``{'error': ...}``
    on any refusal; the message names the reason, never a value."""
    arn = str((cfg or {}).get('secret_arn') or '').strip()
    rest = {k: v for k, v in (cfg or {}).items() if k != 'secret_arn'}
    if not arn:
        return {'connection_config': rest or None, 'secret_arn': ''}
    refusal = _secret_arn_source_refusal(connection_type, arn)
    if refusal:
        return {'error': refusal}
    # A URL / workspace / key_scope filled in the request must MATCH the secret's
    # value (the secret wins — the hard rule). When the named secret is readable
    # now, refuse a conflict here; when it is not (an external secret the
    # dispenser has not written yet), the use-time resolver still forces the
    # secret's value, so the key never travels to a caller-chosen URL.
    conflict = _secret_arn_pin_conflict(arn, rest, connection_type)
    if conflict:
        return {'error': f"{conflict} does not match the credentials secret this system "
                         "reads; leave it blank to use the secret's value"}
    # Mark the node ARN-sourced; the ARN itself becomes the entry's pointer.
    rest['credential_source'] = 'secret_arn'
    return {'connection_config': rest, 'secret_arn': arn}


def _secret_arn_source_refusal(connection_type: str, arn: str) -> str:
    """The fixed refusal for naming ``arn`` as ``connection_type``'s credential
    source, or ``""`` when it may: the type must have opted in, the ARN must
    clear :func:`connection_pinning.check_arn_allowed`, and the deployment must
    enable the source for this type (:func:`connection_pinning.source_type_enabled`).
    Never echoes a value."""
    from .connection_types import secret_arn_source_supported
    if not secret_arn_source_supported(connection_type):
        # The historical client-supplied-ARN refusal (ADR-0031): a request may
        # not name a secret for a type that has not opted in.
        return "naming a Secrets Manager secret is not supported for this connection type"
    from .connection_pinning import (SecretArnNotAllowedError, check_arn_allowed,
                                     source_type_enabled)
    try:
        check_arn_allowed(arn)
    except SecretArnNotAllowedError:
        # The reason is author-written and value-free, but the error surface
        # here is a fixed message (public_errors tripwire): the ARN is not a
        # Secrets Manager ARN in this Region that this deployment allows and
        # does not belong to the application itself.
        return "that Secrets Manager secret cannot be used as this connection's credential source"
    if not source_type_enabled(connection_type):
        # The deployment offers the named-secret source for other connection
        # types only (ADT_SECRET_ARN_SOURCE_TYPES): its read grants do not
        # reach this type's secret, so a registration could only fail later.
        return ("naming a Secrets Manager secret is not enabled for this connection type "
                "in this deployment")
    return ""


def _secret_arn_pin_conflict(arn: str, cfg: dict, connection_type: str = "") -> str:
    """The NAME of the first pinned field (``connection_pinning.pinned_fields``
    of ``connection_type``: Tulip's ``base_url`` / ``workspace_id`` /
    ``key_scope`` / ``table_label_prefix``) the request's ``connection_config`` disagrees with the named
    secret on, or ``""`` — no conflict, the secret is unreadable now, or the
    field is blank. Fail-open on an unreadable secret (the use-time resolver
    still enforces the pin); never raises, never echoes a value."""
    from . import connection_resolver as _cr
    from . import connection_pinning as _cp
    try:
        secret = _cr._fetch_secret(arn)
    except Exception as exc:  # noqa: BLE001 - external secret may not exist yet
        logger.info("registration: pin check skipped (%s): the named secret is not "
                    "readable; the use-time resolver still pins", type(exc).__name__)
        return ""
    fields = _cp.pinned_fields(connection_type)
    submitted = {k: cfg.get(k) for k in fields if cfg.get(k)}
    return _cp.first_pin_conflict(secret or {}, submitted, fields=fields)


#: The fields an MCP registration can carry its URL in. Discover and provision
#: read ``url`` first, then ``connection_string``; ``base_url`` rides along on
#: the catalog entry.
_MCP_URL_FIELDS = ('connection_string', 'url', 'base_url')


def _mcp_registration_url_error(connection_string: str, cfg: dict) -> str:
    """'' when an MCP registration carries ONE URL, else the reason. Every
    non-empty URL field passes the target guard, and they must all be the same
    URL (``connection_resolver.same_endpoint``: scheme, host, effective port,
    exact path and query): the stored token goes to whichever one the reader
    picks, so two different URLs beside one token are refused before anything
    is claimed or stored."""
    from .connection_resolver import same_endpoint
    values = {'connection_string': connection_string, 'url': cfg.get('url'),
              'base_url': cfg.get('base_url')}
    present = [(k, str(values[k]).strip()) for k in _MCP_URL_FIELDS
               if str(values[k] or '').strip()]
    for _key, url in present:
        err = _target_error(url, 'MCP Server')
        if err:
            return err
    if any(not same_endpoint(present[0][1], url) for _key, url in present[1:]):
        names = ' and '.join(k if k == 'connection_string' else f'connection_config.{k}'
                             for k, _url in present)
        return (f"An MCP Server registration takes one URL: {names} differ. Send the "
                "server's URL once (connection_string) and register again.")
    return ''


#: REST-family registrations whose URL(s) pass the target guard at registration.
_URL_GUARDED_TYPES = (TULIP, 'SiteWise (i3X)')
_AWS_REGION_SHAPE = re.compile(r"[a-z]{2}(-[a-z0-9]+)+-\d{1,2}")
_IAM_ROLE_ARN_SHAPE = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,512}")


def _typed_target_error(connection_type: str, connection_string: str, cfg: dict) -> str:
    """'' when a Tulip / i3X / Athena registration's targets pass the
    checks MCP and PostgreSQL registrations get, else the reason. REST family:
    every URL it carries (``connection_string`` and ``connection_config``'s
    ``base_url`` and ``url``) passes the same SSRF/scheme guard. Athena has no URL — boto3
    builds its endpoint from the region — so the region must look like an AWS
    Region (the endpoint host cannot then be steered anywhere), a ``role_arn``
    is required outside local development (:data:`ATHENA_ROLE_REQUIRED`) and
    must be an IAM role ARN, and an ``output_location`` must be an ``s3://``
    URI; the preflight probe still runs the endpoint through the guard. A
    Tulip registration's optional ``table_label_prefix`` must be a valid
    prefix (``tulip_seat_contract.table_label_prefix_problem``)."""
    if connection_type == TULIP:
        from .tulip_seat_contract import table_label_prefix_problem
        problem = table_label_prefix_problem(cfg.get('table_label_prefix'))
        if problem:
            return f"Tulip connection rejected: {problem}"
    if connection_type in _URL_GUARDED_TYPES:
        for url in dict.fromkeys(u for u in (connection_string, cfg.get('base_url'),
                                             cfg.get('url')) if u):
            err = _target_error(str(url), connection_type)
            if err:
                return err
        return ''
    if connection_type == ATHENA:
        return _athena_target_error(cfg)
    return ''


def _athena_target_error(cfg: dict) -> str:
    region = str(cfg.get('region') or '').strip()
    if region and not _AWS_REGION_SHAPE.fullmatch(region):
        return "Athena connection target rejected: region is not an AWS Region name"
    role_arn = str(cfg.get('role_arn') or '').strip()
    if not role_arn and not local_mode_env():
        return ATHENA_ROLE_REQUIRED
    if role_arn and not _IAM_ROLE_ARN_SHAPE.fullmatch(role_arn):
        return "Athena connection target rejected: role_arn is not an IAM role ARN"
    output = str(cfg.get('output_location') or '').strip()
    if output and not output.startswith('s3://'):
        return "Athena connection target rejected: output_location must be an s3:// URI"
    return ''


#: The refusal for a user Athena registration with no role ARN. No app or
#: runtime role of a deployed ADT grants Athena actions, so a blank role could
#: only fail later with an AccessDenied. Local development
#: (``config.local_mode_env``) runs on the developer's own credentials and is
#: not refused.
ATHENA_ROLE_REQUIRED = (
    "Athena needs a read-only IAM role: this app's own roles cannot query Athena. "
    "Deploy stacks/cross-account/adt-data-readonly-role.yaml in the account that holds "
    "the data, with TrustedAccountId set to this app's account (DataAccountId "
    "defaults to the account you deploy the role in; set S3TablesAccountId only "
    "when your tables are in Amazon S3 Tables, to the account that owns the S3 "
    "Tables catalog; set DataBucketName for a plain Amazon S3 source; left empty, "
    "the role gets no plain S3 access), make sure the app is deployed with "
    "--cross-account-role convention (or that role's ARN), then register again with "
    "the role ARN and the External ID you deployed it with.")


def _claim_and_store(system_id: str, entry: dict, connection_config,
                     pg_secret, *, arn_pointer: str = "") -> tuple[str, str]:
    """Claim the id (``catalog.add`` checks and appends under one lock — for a
    USER PostgreSQL registration the schema too), THEN write the registration
    secret; for a USER registration a failed secret write rolls the claim back
    (``remove_entry`` removes only this entry object) and is an error, so the
    form keeps what was typed and a retry is not blocked. A demo registration
    keeps the fail-soft note. → ``(note, error)``.

    ``arn_pointer`` (an ARN-sourced registration, :func:`_prepare_secret_arn_source`):
    the system names an EXISTING Secrets Manager secret, already validated. The
    ARN becomes the entry's ``secret_arn`` pointer and NO ``adt/conn/<id>``
    secret is minted — the key/secret live only in the named secret, read at use
    time. The node is marked ``credential_source="secret_arn"`` (carried in
    ``connection_config``) so the resolver pins its url/workspace from the secret."""
    from .connection_resolver import is_user_registration
    if entry.get('connection_type') == 'PostgreSQL' and is_user_registration(entry):
        if catalog.add(entry, unique_schema=True) is False:
            return '', _claim_conflict(system_id, entry.get('schema') or '')
    elif catalog.add(entry) is False:
        return '', f'System {system_id} already exists'
    if arn_pointer:
        # ARN-sourced: store the named pointer, mint nothing. The entry object
        # catalog.add appended is mutated in place (the same pattern the secret
        # writers use), so the pointer is persisted on the claimed entry.
        entry['secret_arn'] = arn_pointer
        return ("this system reads its credentials from the Secrets Manager secret "
                "it names"), ''
    if pg_secret is None:
        note = _store_registration_secret(system_id, connection_config, entry)
        if note.startswith(_CRED_WRITE_FAILED_NOTE) and is_user_registration(entry):
            # A user registration is transactional: without its credentials
            # it could only fail later, and the claimed id would block a retry.
            catalog.remove_entry(entry)
            return '', _CRED_WRITE_FAILED_ERROR
        return note, ''
    note = _store_pg_registration_secret(system_id, pg_secret, entry)
    if not note:
        catalog.remove_entry(entry)
        return '', _CRED_WRITE_FAILED_ERROR
    from .db import invalidate_schema_conn_cache
    invalidate_schema_conn_cache(entry.get('schema') or '')
    return note, ''


def _claim_conflict(system_id: str, schema: str) -> str:
    """The refusal text for a user PostgreSQL claim ``catalog.add`` refused:
    the id is taken, else the schema is (the same message the sequential check
    in :func:`_prepare_user_pg` gives)."""
    if any(s.get('id') == system_id for s in catalog.get_all()):
        return f'System {system_id} already exists'
    owner = _schema_owner(schema) or 'another system'
    return (f"schema '{schema}' is already registered by system {owner}; "
            "each PostgreSQL system needs its own schema")


def _apply_registration_guidance(entry: dict, system_id: str, query_guidance: str,
                                 capabilities: str, tool_prefix: str) -> None:
    """Guidance-through-registration (durable on the System node at provision).
    An EXPLICIT tool_prefix is also a Bedrock/Nova tool-name prefix and the
    gateway builds ``<tool_prefix>___<tool>``, so it is slugified-with-validation
    like system_id (^[a-zA-Z0-9_-]{1,64}$); omitted ⇒ the sanitized system_id."""
    if query_guidance:
        entry['query_guidance'] = query_guidance
    if capabilities:
        entry['capabilities'] = capabilities
    entry['tool_prefix'] = sanitize_system_id(tool_prefix) if tool_prefix else system_id


def register_system(name: str, system_type: str, plant: str, connection_type: str,
                    connection_string: str = "", schema: str = "",
                    connection_config: dict | None = None, query_guidance: str = "",
                    capabilities: str = "", tool_prefix: str = "", system_id: str = "",
                    *, origin: str) -> dict:
    """The ONE registration implementation, behind ``connect_new_system`` (the
    HTTP route and the chat tool — ``ORIGIN_USER``) and ``register_demo_system``
    (the demo/workshop hook — ``ORIGIN_DEMO``). The origin is recorded on the
    catalog entry; only a USER registration is validated
    (``_prepare_user_registration``) and may store a PostgreSQL DSN.

    Explicit id (from the demo registration hook / an onboarding driver) is used
    VERBATIM; otherwise it is derived from the display name. Either way it is
    guarded by sanitize_system_id so the gateway-prefixed tool names
    (`<system_id>___<tool>`) stay valid for Bedrock Converse
    (^[a-zA-Z0-9_-]{1,64}$); an id that cannot reduce to a safe slug raises.
    """
    from .connection_resolver import CONNECTION_ORIGIN_KEY, ORIGIN_USER
    _raw_id = system_id if system_id else name.lower().replace(' ', '-').replace('_', '-')
    system_id = sanitize_system_id(_raw_id)
    if origin == ORIGIN_USER:
        # Checked, fail closed: a cold catalog never makes a half-removed id "new".
        refused = _registration_id_refusal(system_id)
        if refused:
            return refused
    elif catalog.lookup(system_id):
        return {'error': f'System {system_id} already exists'}
    pg_secret = None
    arn_pointer = ""  # nosec B105 - a secret ARN pointer (identifier), not a credential
    if origin == ORIGIN_USER:
        prepared = _prepare_user_registration(connection_type, connection_string, schema,
                                              connection_config)
        if 'error' in prepared:
            return {'error': prepared['error']}
        connection_string, schema = prepared['connection_string'], prepared['schema']
        connection_config, pg_secret = prepared['connection_config'], prepared['pg_secret']
        arn_pointer = prepared.get('secret_arn') or ""
    if connection_type == 'PostgreSQL' and not schema:
        return {'error': 'schema is required for PostgreSQL connections'}
    entry = _registration_entry(system_id, name, system_type, plant, connection_type,
                                schema, connection_string, connection_config)
    entry[CONNECTION_ORIGIN_KEY] = origin
    _apply_registration_guidance(entry, system_id, query_guidance, capabilities, tool_prefix)
    _cred_note, _err = _claim_and_store(system_id, entry, connection_config, pg_secret,
                                        arn_pointer=arn_pointer)
    if _err:
        return {'error': _err}
    return {'status': 'created', 'system_id': system_id, 'name': name,
            'message': f'System registered. You can now run discover to scan its schema.',
            **({'note': _cred_note} if _cred_note else {})}


def register_demo_system(**kwargs) -> dict:
    """The demo/workshop registration hook's entry point: :func:`register_system`
    with an explicit ``ORIGIN_DEMO`` (never validated as, or treated as, a
    user's own connection)."""
    from .connection_resolver import ORIGIN_DEMO
    return register_system(**kwargs, origin=ORIGIN_DEMO)


@tool
def connect_new_system(name: str, system_type: str, plant: str, connection_type: str, connection_string: str = "", schema: str = "", connection_config: dict | None = None, query_guidance: str = "", capabilities: str = "", tool_prefix: str = "", system_id: str = "") -> dict:
    """Register a new system in the catalog so it can be onboarded.

    connection_type may be PostgreSQL, MCP Server, SiteWise (Direct),
    SiteWise (i3X), Athena, or Tulip (Tables API); a generic REST API is not
    supported yet. For PostgreSQL, schema is required. Typed connectors
    (e.g. Athena) carry their non-secret settings in ``connection_config`` —
    Athena: ``{workgroup, database, catalog?, output_location?, region?}``.

    Guidance-through-registration: an optional ``query_guidance``/``capabilities``/
    ``tool_prefix`` are persisted on the catalog entry (and thence the Neptune
    System node at provision), so a connect-registered system carries its own
    orchestrator guidance without relying on the (base-empty) demo catalog.
    ``tool_prefix`` defaults to the sanitized ``system_id``; blank guidance/
    capabilities are left unset so the generic per-connection-type default applies.

    Explicit id passthrough: when ``system_id`` is supplied it is used VERBATIM
    (still sanitized to a tool-safe slug) instead of slugifying ``name``. Omitted
    => the id is derived from ``name``.

    The plant may not be the retired form placeholder 'Custom'
    (:func:`catalog.reserved_plant`): it now reads as no plant everywhere, so
    it is refused here instead of being stored as no plant without a word.
    """
    from .catalog import RESERVED_PLANT_ERROR, reserved_plant
    from .connection_resolver import ORIGIN_USER
    from . import startup_restore
    # The app's startup restore still running in the background: its catalog
    # holds only the systems restored so far, so the id and schema claims
    # below could take those of a system not restored yet (POST /connect
    # answers this as a 503).
    restoring = startup_restore.refusal()
    if restoring:
        return restoring
    if reserved_plant(plant):
        return {'error': RESERVED_PLANT_ERROR}
    return register_system(name, system_type, plant, connection_type, connection_string,
                           schema, connection_config, query_guidance, capabilities,
                           tool_prefix, system_id, origin=ORIGIN_USER)


# ---------------------------------------------------------------------------
# Pre-map vocabulary fit + concept-proposal preview (ADR-0068) — READ-ONLY
# ---------------------------------------------------------------------------

# Bound on one propose_concepts batch: a chat-scale preview, not a bulk import
# (the admin import route keeps its own larger batch cap).
_PROPOSE_MAX_ROWS = 20


def _vocabulary_concepts() -> list[dict]:
    """Live concept vocabulary as ``[{id, name, description}]`` — the SAME
    core-definitions + non-retired-graph merge the map prompt reads
    (``catalog.concepts_prompt_block``), shaped for ``vocab_fit.compute_fit``.
    Graph unavailable ⇒ the curated core definitions only (same fallback
    discipline as ``get_all_concepts``). Deterministic order (sorted by id)."""
    from .catalog import CONCEPT_NAME, CORE_CONCEPT_DEFINITIONS
    concepts = {cid: {'id': cid, 'name': CONCEPT_NAME.get(cid, cid),
                      'description': desc}
                for cid, desc in CORE_CONCEPT_DEFINITIONS.items()}
    try:
        rows = neptune_query(
            "MATCH (c:Concept) WHERE coalesce(c.retired, false) = false "
            "RETURN c.id AS id, c.name AS name, c.description AS description", {})
        for r in rows or []:
            cid = r.get('id')
            if cid and cid not in concepts:
                concepts[cid] = {'id': cid, 'name': r.get('name') or '',
                                 'description': r.get('description') or ''}
    except Exception:  # nosec B110 - graph unavailable => core-only fallback
        pass
    return [concepts[cid] for cid in sorted(concepts)]


def _pending_binding_count(system_id: str) -> int:
    """Pending vocabulary-drift ``concept_mapping`` proposals for this system
    (ADR-0081 D6). READ-ONLY by construction: ``_query_by_status``, never
    ``get_pending_mappings`` (which expires stale insights — a write).
    Best-effort: a store the onboarding runtime cannot reach ⇒ 0 (the count
    is a hint for the agent's narration, never evidence)."""
    try:
        from .self_learning import _query_by_status
        return sum(
            1 for r in _query_by_status("pending")
            if r.get("proposed_type") == "concept_mapping"
            and str(r.get("source") or "") == "vocabulary_drift"
            and str(r.get("to_system") or "") == system_id)
    except Exception:  # nosec B110 - best-effort hint, store may be absent
        return 0


@tool
@_unless_removal_failed
def assess_vocabulary_fit(system_id: str) -> dict:
    """Deterministic pre-map vocabulary-fit report. Call after discover, before map_fields.

    Reports how well the current concept vocabulary covers the discovered
    columns (fit_pct, unmatched column names, a computed suggestion) plus how
    many steward context questions the schema makes worth asking
    (``context_questions`` — a COUNT only; the question text stays server-owned
    and is fetched by the UI card, never relayed through the model), plus how
    many pending vocabulary-drift binding proposals await steward review in
    Self-Learning Discoveries (``pending_bindings``). Read-only;
    the fit signal is COMPUTED (``agents/vocab_fit.py``), never model-judged.
    Safe to re-consult at any later step.
    """
    validate_system_id(system_id)
    try:
        session = _store().get_session(system_id)
    except SessionNotFoundError:
        return {'error': 'No tables discovered yet. Call discover first.'}
    tables = session.get('discovered_tables')
    if not tables:
        return {'error': 'No tables discovered yet. Call discover first.'}
    from .context_interview.questions import (
        schema_facts_from_tables, select_questions)
    from .vocab_fit import compute_fit
    report = compute_fit(tables, _vocabulary_concepts())
    questions = select_questions(schema_facts_from_tables(tables), set())
    return {'system_id': system_id, 'context_questions': len(questions),
            'pending_bindings': _pending_binding_count(system_id), **report}


def _existing_concept_ids(ids: list[str]) -> set[str]:
    """One bounded pre-read of graph ids matching the candidate ids exactly or
    on the normalized collision key — the ``vocabulary_routes._read_existing_ids``
    contract, kept local because that module is a FastAPI router and this tool
    runs in the onboarding runtime. A read failure degrades to none-existing
    (matching the admin preview's posture — the steward accept re-classifies
    on the write path, so a stale preview can never cause a bad write)."""
    try:
        rows = neptune_query(
            "MATCH (c:Concept) WHERE c.id IN $ids "
            "OR replace(toLower(c.id), '-', '_') IN $ids "
            "RETURN c.id AS id",
            {'ids': ids},
        )
    except Exception as e:
        logger.warning("propose_concepts pre-read failed (treating as no matches): %s", e)
        rows = []
    return {r.get('id') for r in (rows or []) if r.get('id')}


def _classify_concept_proposals(rows: list[dict]) -> tuple[list[dict], dict]:
    """Classify validated proposal rows via the ONE shared import classifier
    (``vocab_import_core.classify_import_rows`` — the same helper the admin
    import/preview routes use, so this preview cannot drift from what an
    accept would do). Returns ``(proposal rows, counters)``. ZERO writes."""
    from .vocab_import_core import classify_import_rows
    classified = classify_import_rows(
        rows, _existing_concept_ids([r['id'] for r in rows]))
    proposals = [
        {'id': row['id'], 'name': row['name'], 'description': row['description'],
         'action': action, 'collides_with': collides_with}
        for row, action, collides_with in classified
    ]
    actions = [p['action'] for p in proposals]
    return proposals, {
        'insert_count': actions.count('insert'),
        'skip_count': actions.count('skip_exact'),
        'collision_count': actions.count('skip_collision'),
    }


def _rejected_proposal_row(item, err: str) -> dict:
    """Per-row rejection entry for a proposal that failed boundary validation
    (a bad row never aborts the batch — the import-route discipline)."""
    raw_id = str(item.get('id') or '') if isinstance(item, dict) else ''
    raw_name = str(item.get('name') or '') if isinstance(item, dict) else ''
    return {'id': raw_id, 'name': raw_name, 'description': '',
            'action': 'rejected', 'collides_with': None, 'reason': err}


@tool
def propose_concepts(system_id: str, concepts: str) -> dict:
    """Preview NEW concept proposals against the existing vocabulary. ZERO writes.

    ``concepts`` is a JSON string: ``[{"id", "name", "description"}]`` (at most
    20 rows). Each row is boundary-validated and classified as ``insert`` /
    ``skip_exact`` / ``skip_collision`` (an existing or colliding id is already
    in the vocabulary — refine its meaning via a proposal instead). Nothing is
    written by this tool — the steward accepts proposals in the UI card, which
    calls the existing vocabulary import route.
    """
    validate_system_id(system_id)
    try:
        items = json.loads(concepts) if isinstance(concepts, str) else concepts
    except json.JSONDecodeError:
        return {'error': 'concepts must be valid JSON'}
    if not isinstance(items, list) or not items:
        return {'error': 'concepts must be a non-empty JSON array of '
                         '{"id", "name", "description"} objects'}
    if len(items) > _PROPOSE_MAX_ROWS:
        return {'error': f'at most {_PROPOSE_MAX_ROWS} concepts per call — '
                         'propose the most valuable ones first'}
    from .vocab_import_core import validate_import_row
    rows: list[dict] = []
    rejected: list[dict] = []
    for item in items:
        row, err = validate_import_row(item)
        if err:
            rejected.append(_rejected_proposal_row(item, err))
        else:
            rows.append(row)
    proposals, counters = (_classify_concept_proposals(rows) if rows
                           else ([], {'insert_count': 0, 'skip_count': 0,
                                      'collision_count': 0}))
    proposals.extend(rejected)  # classified rows first, per-row rejections after
    return {'system_id': system_id, 'proposals': proposals,
            'rejected_count': len(rejected), **counters}


ONBOARD_AGENT_PROMPT = """You are the Onboarding Agent for the Agentic Digital Thread sample, a manufacturing intelligence platform.

You guide users through connecting their factory systems so an AI agent can query them.

GROUNDING — NEVER FABRICATE SYSTEMS:
Report ONLY systems returned by get_catalog(). If it returns an empty list, say "no systems connected yet" and guide the user to connect_new_system — NEVER invent, suggest, or example-list vendor or product names not present in the tool output.

You have tools to execute each step of the onboarding workflow:
1. get_catalog — see what systems are available
2. get_system_info — details about a specific system
3. test_connection — verify connectivity (works for all connection types)
4. store_credentials — securely store credentials in AWS Secrets Manager
5. connect_new_system — register a new system (PostgreSQL, REST API, MCP Server, SiteWise (Direct), SiteWise (i3X), Athena, or Tulip (Tables API))
6. discover — connect and find tables/schemas (auto-detects connection type)
7. assess_vocabulary_fit — deterministic report of how well the concept vocabulary covers the discovered columns (read-only)
8. propose_concepts — zero-write preview classifying proposed new concepts against the existing vocabulary
9. map_fields — assign manufacturing concepts to fields
10. resolve_identities — create cross-system identity rules
11. provision_system — write to knowledge graph, create agent
12. verify_system — check the source reads and the knowledge-graph registration; the panel runs the agent test separately
13. reset_system — start over a half-done onboarding of a system that is not onboarded, or remove an onboarded system completely (graph, agent, registration); never call it to run discover again on an onboarded system; relay its answer when it is refused

Workflow:
- When the user wants to connect an existing system, call get_catalog to find it, then discover.
- When the user wants to add a new system not in the catalog, call connect_new_system first, then discover.
- After each tool call, present the results clearly and ask if the user wants to proceed or make changes.
- If the user asks to modify results (exclude a table, change a mapping), acknowledge and note the change.
- Keep responses concise — 2-3 sentences plus the data.
- Be transparent about what each step does and why.

Vocabulary fit (after discover, BEFORE map_fields):
- Call assess_vocabulary_fit after a successful discover. The fit numbers are computed by the tool — relay them, never re-judge them.
- When suggestion is not "ok", tell the user a fit report card is shown in the chat with the details.
- You MAY then call propose_concepts with concepts you infer from the unmatched columns: a short id, a short name, and a one-sentence generic description each.
- NEVER claim a concept was added — propose_concepts writes nothing; the steward accepts proposals in the card. A low fit may also be solved by a domain pack (Admin → Vocabulary).
- Proceed to map_fields when the user confirms. Mapping reads the vocabulary live, so concepts accepted after discover are picked up without re-running discover.

Connection types and what's needed:
- PostgreSQL: connection credentials (host, port, database, username, password) and schema name
- REST API (OpenAPI): spec URL or uploaded spec file, plus auth credentials if the API requires them
- MCP Server: server URL, plus optional auth headers
- SiteWise (Direct): AWS IoT SiteWise in this or a cross-account (assumed read-only role) — region, plus role ARN/external ID for cross-account
- SiteWise (i3X): the i3X REST endpoint URL fronting AWS IoT SiteWise
- Athena: workgroup and database (optional catalog, output location, region); cross-account via an assumed read-only role
- Tulip (Tables API): the Tulip base URL (cloud or self-hosted/proxied on any host) plus an API key and secret (HTTP Basic); no OpenAPI spec

SECURITY — CREDENTIAL HANDLING:
If a user message contains what appears to be credentials (passwords, tokens, API keys,
connection strings with passwords, bearer tokens), DO NOT use them. Respond:
"I noticed what may be credentials in your message. For security, please use the secure
input form instead — I'll open it for you. If those were real credentials, you should
consider rotating them since they're now in chat history."
Never echo credentials back. Never store credentials in conversation history.
Credentials must only flow through the store_credentials tool or the secure UI form.
"""


#: What a deployment's prompt says in place of the chat flow for a new system
#: (``(local text, deployment text)``; each local text must be in the prompt).
_CLOUD_PROMPT_EDITS = (
    ('say "no systems connected yet" and guide the user to connect_new_system',
     'say "no systems connected yet" and guide the user to Connect New System in the Onboard '
     'view'),
    ("5. connect_new_system — register a new system (PostgreSQL, REST API, MCP Server, "
     "SiteWise (Direct), SiteWise (i3X), Athena, or Tulip (Tables API))",
     "5. connect_new_system — registers nothing in this deployment: it says how a new system is "
     "added (Connect New System in the Onboard view, then the wizard's Discover, Map, Resolve "
     "and Provision); relay it and offer to explain each step"),
    ("- When the user wants to add a new system not in the catalog, call connect_new_system "
     "first, then discover.",
     "- When the user wants to add a new system not in the catalog, do not register it from "
     "the chat: tell them to add it with Connect New System in the Onboard view, then run the "
     "wizard's steps on its card: Discover, Map, Resolve and Provision, and offer to explain any "
     "of those steps. discover, map_fields, resolve_identities and provision_system work only "
     "for systems get_catalog lists."),
    ("Connection types and what's needed:",
     "Connection types and what Connect New System asks for:"),
)


def _cloud_onboard_prompt(prompt: str) -> str:
    """:data:`ONBOARD_AGENT_PROMPT` with :data:`_CLOUD_PROMPT_EDITS` applied.
    Raises at import when a local text is no longer in the prompt, so the
    deployment's prompt cannot silently keep the chat flow for a new system."""
    for local, cloud in _CLOUD_PROMPT_EDITS:
        if local not in prompt:
            raise RuntimeError(f"onboarding prompt text not found for the deployment's prompt: "
                               f"{local[:60]!r}")
        prompt = prompt.replace(local, cloud)
    return prompt


ONBOARD_AGENT_PROMPT_CLOUD = _cloud_onboard_prompt(ONBOARD_AGENT_PROMPT)


# The wired onboarding tool set — the SINGLE SOURCE OF TRUTH for which onboarding
# tools exist. The in-process agent built with Strands Agents (``get_onboard_agent``)
# wires all of it in local mode, and the AgentCore MCP runtime server
# (``onboarding_runtime_server.create_onboarding_server``) exports it without the
# tools the app runs itself in a deployment (``onboard_chat_lifecycle.NOT_ON_GATEWAY``:
# get_catalog, provision_system, reset_system, connect_new_system, store_credentials).
# Keeping ONE list means the agent's wired set and the gateway-exported set cannot
# drift: a tool is exposed only if it is in this list, so a defined-but-unwired
# ``@tool`` can never auto-export as an orphaned extra tool (F1/F9:
# ``get_field_mappings`` was deleted upstream). The explore/commit grouping mirrors
# ADR-0032's two-target split (9 explore + 4 commit here; the runtime exports 8
# explore and no commit-tier tool); the split itself is a gateway concern (Task 2.1),
# not the server's.
ONBOARD_TOOLS = [
    # explore tier (read / probe / analyze — gateway scope ``onboard:explore``)
    get_catalog, get_system_info, test_connection, discover,
    assess_vocabulary_fit, propose_concepts,
    map_fields, resolve_identities, verify_system,
    # commit tier (mutate catalog / secrets / graph — gateway scope ``onboard:write``)
    connect_new_system, store_credentials, provision_system, reset_system,
]


def _create_onboarding_gateway_client(mcp_url: str, bearer_token: str) -> MCPClient:
    """Build an MCPClient bound to the onboarding AgentCore gateway (CUSTOM_JWT).

    Mirrors ``orchestrator._create_mcp_client`` but authenticates with the
    forwarded interactive user's ``adt-app`` access token as
    ``Authorization: Bearer <token>`` (not SigV4). Routing tool calls through the
    gateway means they execute in the onboarding tool-server runtime behind it,
    under the user's JWT (no Cedar policy engine is attached yet).

    The gateway's ``get_catalog``, ``discover``, ``provision_system``,
    ``reset_system``, ``connect_new_system`` and ``store_credentials`` are left
    out (``onboard_chat_lifecycle.GATEWAY_TOOL_FILTERS``): the app answers
    those itself (``_get_onboard_tools``). get_catalog and discover are the
    app's so they read the app's catalog, which holds registrations the
    runtime's catalog does not; store_credentials is the app's so a secret is
    written only from the app.

    The token is passed only in the request headers — never logged, never echoed
    (R4 / credential guardrails).
    """
    from .onboard_chat_lifecycle import GATEWAY_TOOL_FILTERS
    return bearer_mcp_client(mcp_url, bearer_token, tool_filters=GATEWAY_TOOL_FILTERS)


def _assert_gateway_toolbox_nonempty(mcp_url: str, user_access_token: str) -> None:
    """Fail-closed EMPTY-TOOLBOX gate (fabrication guard).

    A reachable gateway whose target is broken (e.g. SYNCHRONIZE_UNSUCCESSFUL —
    observed live when the runtime image could not be pulled / lacked
    serverProtocol MCP) advertises ZERO tools. A model run tool-less does not
    error — it ROLE-PLAYS its tools and fabricates catalog answers. Probe
    tools/list with a throwaway client and raise loudly instead.
    """
    probe = _create_onboarding_gateway_client(mcp_url, user_access_token)
    try:
        with probe:
            tool_count = len(probe.list_tools_sync())
    except Exception as exc:
        raise RuntimeError(
            "The onboarding agent's tools are unavailable (the onboarding "
            "gateway did not answer tools/list). Refusing to run the agent "
            "without tools — a tool-less model fabricates answers. Check the "
            f"onboarding gateway target status. Cause: {exc}"
        ) from exc
    if tool_count == 0:
        raise RuntimeError(
            "The onboarding gateway advertises ZERO tools (its runtime target "
            "is likely not READY — check for SYNCHRONIZE_UNSUCCESSFUL). "
            "Refusing to run the onboarding agent without tools — a tool-less "
            "model fabricates answers instead of erroring."
        )



def _get_onboard_tools(user_access_token: "str | None") -> list:
    """Select the onboarding agent's tool source with an EXPLICIT mode check.

    No silent fallback (project steering):

      * local mode (``AGENT_MODE=local`` / ``DEV_MODE`` — ADR-0023, no cloud
        gateway) -> the in-process ``ONBOARD_TOOLS``.
      * cloud/deployed mode -> a single MCP client bound to the onboarding gateway
        (CUSTOM_JWT) with the forwarded user bearer token. The gateway federates
        the onboarding tools from the tool-server runtime.

    In both modes ``provision_system`` and ``reset_system`` are the app's own
    (``onboard_chat_lifecycle``): the same names and parameters, running the
    wizard's provision and the card's removal in this process. In cloud mode
    ``get_catalog``, ``discover`` and ``connect_new_system`` are too: get_catalog
    and discover read the app's in-process catalog (which holds registrations
    the onboarding runtime's catalog does not), and connect_new_system registers
    nothing (a new system is added in the Onboard view). So is
    ``store_credentials``, which writes the secret from the app
    (``onboard_chat_lifecycle.store_credentials``). The gateway client leaves
    those out.

    Misconfiguration raises loudly rather than degrading:
      * cloud mode but ``ONBOARDING_GATEWAY_URL`` unset -> ``RuntimeError``.
      * cloud mode but no forwarded user token -> ``RuntimeError`` (a CUSTOM_JWT
        gateway rejects an anonymous call; onboarding is always interactive so a
        missing token is a wiring bug, not a service caller).
    """
    from . import onboard_chat_lifecycle as lifecycle
    config = get_config()
    if config.is_local_mode:  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        return lifecycle.in_app_tools(ONBOARD_TOOLS)

    # Cloud/deployed: tools MUST come from the onboarding gateway. Never fall back
    # to in-process tools just because a URL/token is missing — that would hide a
    # misconfiguration and skip the gateway's per-user JWT check.
    if not config.has_onboarding_gateway:
        raise RuntimeError(
            "Onboarding is in cloud mode but ONBOARDING_GATEWAY_URL is not set. "
            "The in-app onboarding agent federates its tools from the onboarding "
            "gateway; refusing to fall back to in-process tools (no silent "
            "fallback). Set ONBOARDING_GATEWAY_URL (ADTOnboardingStack output) or run "
            "with AGENT_MODE=local for local development."
        )
    if not user_access_token:
        raise RuntimeError(
            "Onboarding is in cloud mode but no forwarded user access token was "
            "provided. The onboarding gateway (CUSTOM_JWT) requires the "
            "interactive user's bearer token for per-user identity; refusing to "
            "call it anonymously."
        )
    # Fail-closed EMPTY-TOOLBOX gate — see _assert_gateway_toolbox_nonempty.
    _assert_gateway_toolbox_nonempty(config.onboarding_gateway_url, user_access_token)
    gateway_client = _create_onboarding_gateway_client(
        config.onboarding_gateway_url, user_access_token)
    # The app's discover forwards a runtime-owned case (an onboarded system
    # with no onboarding session, or any non-'registered' status) to the
    # runtime's discover through this same client (its session is live for the
    # agent's turn), so that discover writes the runtime's session exactly as
    # on main. Bound per request (ContextVar) so concurrent chats never share.
    lifecycle.bind_runtime_discover(gateway_client)
    return [gateway_client, *lifecycle.CLOUD_APP_TOOLS]


def get_onboard_agent(user_access_token: "str | None" = None) -> Agent:
    """Create the onboarding agent.

    The agent loop and SSE streaming stay in-app (ADR C1 / P4-A); only the tool
    SOURCE changes with the deployment mode (see ``_get_onboard_tools``):

      * cloud -> tools federated from the onboarding gateway (MCP client,
        CUSTOM_JWT) with ``user_access_token`` forwarded as ``Authorization:
        Bearer``, so tool calls run in the onboarding tool-server runtime under
        the user's identity (no Cedar policy engine is attached yet).
      * local (ADR-0023) -> in-process ``ONBOARD_TOOLS``.

    Args:
        user_access_token: The interactive user's ``adt-app`` access token
            (extracted from the ``/api/onboard/chat`` session). Required in cloud
            mode; ignored in local mode. Never logged (R4).
    """
    from .model_catalog import env_orchestrator_model
    return Agent(
        model=env_orchestrator_model(),
        system_prompt=_onboard_agent_prompt(),
        tools=_get_onboard_tools(user_access_token),
        hooks=[EmptyToolTurnRetry()],
    )


def _onboard_agent_prompt() -> str:
    """The onboarding agent's prompt for the deployment mode: local mode's
    in-process tools share the app's catalog, so :data:`ONBOARD_AGENT_PROMPT`
    describes the whole chat flow; a deployment's agent gets
    :data:`ONBOARD_AGENT_PROMPT_CLOUD`, which sends a new system to the
    Onboard view (see ``_get_onboard_tools``)."""
    config = get_config()
    if config.is_local_mode:  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        return ONBOARD_AGENT_PROMPT
    return ONBOARD_AGENT_PROMPT_CLOUD
