# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Runtime entry point.

When SYSTEM_ID is set → runs as MCP server (system agent mode).
When SYSTEM_ID is not set → runs as BedrockAgentCoreApp (orchestrator mode).
"""

import asyncio
import contextvars
import os
import logging
import sys

# Explicit logging config — AgentCore runtime defaults to ERROR level,
# which suppresses all INFO logs from CloudWatch. Force INFO to stdout.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


class _SafeRuntimeLogFilter(logging.Filter):
    _PREFIXES = {
        "Self-learning log failed:": "Self-learning log failed",
        "column-not-found guidance skipped:": "column-not-found guidance skipped",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if message.startswith("=== ORCHESTRATOR INVOKED") and " prompt=" in message:
            record.msg, record.args = message.split(" prompt=", 1)[0], ()
            message = record.msg
        for prefix, replacement in self._PREFIXES.items():
            if message.startswith(prefix):
                record.msg, record.args = replacement, ()
                message = replacement
                break
        lowered = message.lower()
        if record.levelno >= logging.DEBUG and any(
            marker in lowered for marker in (" failed:", " failure:", " skipped:")
        ):
            record.msg, record.args = message.split(":", 1)[0] + " category=runtime", ()
        record.exc_info = None
        record.exc_text = None
        return True


logger.addFilter(_SafeRuntimeLogFilter())

# Pure, stdlib-only intelligence primitives (graph-query predicate, failure
# markers, summary payload shape) — safe to import before any AWS wiring.
from . import query_intelligence  # noqa: E402 - deliberate: after logging config

# Version tag — appears in CloudWatch on every container start
_IMAGE_VERSION = "v8-stream-filter-plus-sliding-window"
logger.info("=== RUNTIME IMAGE %s STARTING === pid=%d", _IMAGE_VERSION, os.getpid())


def _assert_runtime_env(role: str) -> None:
    """Fail CLOSED at process start when required env is absent (ADR-0069).

    ``runtime_env_contract.missing_required(role, os.environ)`` is the ONE
    validator — the required set derives from the contract itself
    (``STALE_CHECKED_KEYS``). Called from the ``__main__`` dispatch for the
    orchestrator/system/digital-thread modes (the onboarding mode validates
    inside ``create_onboarding_server`` at construction). Raising with the
    key names puts the real cause in CloudWatch at boot, instead of letting
    ``neptune_query``'s ``[]``-on-failure degrade path present a
    half-provisioned runtime as "the catalog is empty". Skipped in
    local/DEV_MODE (``config.local_mode_env``) where the env is
    intentionally minimal (ADR-0023).

    The orchestrator also validates ``MEMORY_LTM_SCOPE`` here
    (``config.memory_ltm_scope`` raises on an unknown value): at chat time a
    bad scope only degrades memory, and the runtime's OTel handler drops that
    traceback — boot is where the misconfiguration is visible.
    """
    from .config import local_mode_env, memory_ltm_scope
    if local_mode_env():
        return
    if role == "orchestrator":
        memory_ltm_scope()
    from .runtime_env_contract import missing_required
    missing = missing_required(role, os.environ)
    if missing:
        raise RuntimeError(
            f"{role} runtime env incomplete — missing/empty required keys: "
            f"{', '.join(missing)}. Re-run deploy.sh (the contract rebuilds "
            "runtime env), or set AGENT_MODE=local for local development.")


def _resolve_runtime_connection_config(system_id, connection_config, secret_arn,
                                       connection_type=None):
    """Resolve the query runtime's ``connection_config`` before it reaches
    ``create_mcp_server`` — a pure, offline-testable seam extracted from
    ``_run_system_agent`` (which reads env + starts a server).

    Two steps, both generic across connection types:

    1. **Secret resolution (existing behaviour).** When ``CONNECTION_CONFIG`` is
       EMPTY and a ``CONNECTION_SECRET_ARN`` is set, resolve the real config from
       the secret (the secret holds the Athena workgroup/database, a cross-account
       SiteWise ``role_arn``/``external_id``, DB host/port/creds, ...). A populated
       config is used verbatim and the secret is NOT fetched here.

    2. **Cross-account credential merge (the fix — DRY).** A NON-secret
       ``CONNECTION_CONFIG`` for a cross-account system (e.g. SiteWise Direct)
       carries ``region``/``role_arn`` but NOT the SECRET-only ``external_id`` — so
       the runtime's SiteWise/Athena tool would STS ``AssumeRole`` WITHOUT the
       required ``sts:ExternalId`` and be denied. The in-app dashboard path never
       hits this because ``metric_resolver._full_connection_config`` ->
       ``resolve_evidence._with_cross_account_credentials`` ALWAYS merges the
       secret's ``external_id``. Close that divergence by reusing the SAME helper
       here: when the creds are INCOMPLETE (missing ``role_arn`` or ``external_id``)
       merge only the missing ``_CROSS_ACCOUNT_KEYS`` from the graph node / the
       ``adt/agent-config/<system_id>`` secret. Already-complete configs
       short-circuit (no needless lookup); a local system (no ``role_arn``) is left
       unchanged by the helper. Secret values are never logged.

    PostgreSQL skips step 2: nothing to merge, and the lookup would read the
    System node's owner pointer; the runtime uses its agent-config login only.
    """
    # 1) Empty env config -> resolve the real config from the secret.
    if not connection_config and secret_arn:
        from .system_agent import _fetch_credentials
        connection_config = _fetch_credentials(secret_arn) or {}
        logger.info(  # logs the system id and key names only, never a value
            "%s (keys=%s): resolved an empty CONNECTION_CONFIG from the "
            "CONNECTION_SECRET_ARN secret", system_id, sorted(connection_config.keys()),
        )
    # 2) DRY cross-account merge — only when creds are incomplete, reusing the SAME
    #    helper the in-app path uses so both paths assume the role WITH sts:ExternalId.
    if connection_type == "PostgreSQL":
        return connection_config
    if isinstance(connection_config, dict) and not (
        connection_config.get("role_arn") and connection_config.get("external_id")
    ):
        from .resolve_evidence import _with_cross_account_credentials
        connection_config = _with_cross_account_credentials(system_id, connection_config)
    return connection_config


# ---------------------------------------------------------------------------
# User-identity claim: pop-always + reference enforcement hook (ADR-0049, I2.2)
# ---------------------------------------------------------------------------
# Implemented in agents/runtime_asgi.py (the shared MCP ASGI assembly installs
# the middleware on EVERY FastMCP runtime mode) and RE-EXPORTED here: this
# module remains the runtime entry point that pops the reserved identity arg
# before query handling, always — see runtime_asgi's module docstring for the
# full contract (pop-always, SYSTEM_REQUIRED_GROUP enforcement, and the
# deny-text-vs-retry-marker rule pinned by test_runtime_identity_hook.py).
from .runtime_asgi import (  # noqa: E402,F401 - deliberate re-export surface
    IDENTITY_CLAIM_ARG,
    IDENTITY_CLAIM_SECRET_ARN_ENV,
    IDENTITY_DENY_GROUP,
    IDENTITY_DENY_INVALID,
    IDENTITY_DENY_KEY_UNREADABLE,
    IDENTITY_DENY_KEY_UNCONFIGURED,
    IDENTITY_DENY_MISSING,
    REQUIRED_GROUP_ENV,
    IdentityClaimASGIMiddleware,
    enforce_identity_claim,
    identity_deny_texts,
    reset_identity_claim_secret_cache,
    _fetch_identity_claim_secret,
    _pop_identity_from_mcp_body,
)


def _run_system_agent():
    """Run as FastMCP server for a system agent.

    AgentCore runtime infrastructure sends {"init_event_loop": true} to /invocations
    as a warmup before the gateway connects — handled (with the /mcp vs /mcp/
    trailing-slash alias and the ADR-0049 identity middleware) by the shared
    runtime_asgi.serve_mcp_app assembly.
    """
    import json
    from .runtime_asgi import serve_mcp_app
    from .system_agent import create_mcp_server

    system_id = os.environ["SYSTEM_ID"]
    connection_type = os.environ.get("CONNECTION_TYPE", "PostgreSQL")
    connection_config = json.loads(os.environ.get("CONNECTION_CONFIG", "{}"))
    # The query runtime is started with CONNECTION_SECRET_ARN pointing at the
    # Secrets Manager secret that holds the REAL connection_config (Athena
    # workgroup/database, a cross-account SiteWise role_arn/external_id, DB
    # host/port/creds, ...). CONNECTION_CONFIG is only populated for non-secret
    # configs; when it is empty the config MUST be resolved from the secret —
    # otherwise e.g. the Athena branch calls make_athena_tools({}), which raises
    # AthenaConfigError('requires workgroup'), the MCP server never imports/starts,
    # and the gateway target goes SYNCHRONIZE_UNSUCCESSFUL -> 'Unknown tool'. This
    # also closes the latent gap where a SiteWise cross-account role_arn/external_id
    # stored in the secret was never applied on the query runtime. Generic across
    # connection types; a populated CONNECTION_CONFIG is used verbatim and the
    # secret is NOT fetched.
    secret_arn = os.environ.get("CONNECTION_SECRET_ARN", "")
    connection_config = _resolve_runtime_connection_config(
        system_id, connection_config, secret_arn, connection_type,
    )
    system_prompt, tables_desc, conn_params, connection_config = (
        _extract_prompt_and_conn_params(connection_config, connection_type)
    )

    mcp = create_mcp_server(
        system_id=system_id,
        system_name=os.environ.get("SYSTEM_NAME", system_id),
        schema=os.environ.get("SCHEMA", ""),
        system_prompt=system_prompt,
        tables_desc=tables_desc,
        connection_type=connection_type,
        connection_config=connection_config,
        conn_params=conn_params,
        port=8000,
    )
    serve_mcp_app(mcp, system_id, f"Starting system agent MCP server for {system_id} on port 8000")


def _extract_prompt_and_conn_params(connection_config, connection_type):
    """Split secret-carried prompt/table fields + Postgres conn params from the config.

    SYSTEM_PROMPT / TABLES_DESC are delivered via the agent-config secret (kept
    OUT of the 4000-byte runtime env — see agent_manager._start_cloud). Read
    them back from the resolved connection_config and STRIP them so they are
    not mistaken for connection keys by the per-type tool builders; prefer the
    secret-carried values, fall back to the (usually empty) env. Baking the
    full column schema is what stops the agent re-discovering its schema on
    every delegated sub-question. The secret-carried ``business_context`` key
    (ADR-0054 D4) is deliberately NOT popped here: like ``concept_mappings``
    it RIDES THROUGH connection_config to ``create_system_agent``, which pops
    it before tool building (Mode A prompt composition; the secret is its only
    channel — no env override). Returns
    ``(system_prompt, tables_desc, conn_params, connection_config)``.
    """
    system_prompt = os.environ.get("SYSTEM_PROMPT", "")
    tables_desc = os.environ.get("TABLES_DESC", "")
    if isinstance(connection_config, dict):
        _secret_prompt = connection_config.pop("system_prompt", "")
        _secret_tables = connection_config.pop("tables_desc", "")
        system_prompt = system_prompt or _secret_prompt
        tables_desc = tables_desc or _secret_tables
    conn_params = None
    if connection_type == "PostgreSQL" and "host" in connection_config:
        conn_params = {k: connection_config.pop(k) for k in ["host", "port", "dbname", "user", "password"] if k in connection_config}
    return system_prompt, tables_desc, conn_params, connection_config


def _run_digital_thread():
    """Run as the `digital-thread` FastMCP tool server (reserved SYSTEM_ID=digital-thread).

    Serves the stateless graph-tools server from
    digital_thread_server.create_digital_thread_server through the shared
    runtime_asgi.serve_mcp_app assembly (the /ping + /invocations warmup, the
    /mcp vs /mcp/ trailing-slash alias the gateway depends on, and the ADR-0049
    identity middleware). No SYSTEM_ID / connection config is needed — the
    tools talk to Neptune via the runtime IAM role. Must run in VPC mode
    (Neptune is VPC-private, ADR-0024).
    """
    from .digital_thread_server import create_digital_thread_server, TARGET_NAME
    from .runtime_asgi import serve_mcp_app

    mcp = create_digital_thread_server(port=8000)
    serve_mcp_app(mcp, TARGET_NAME, f"Starting {TARGET_NAME} MCP tool server on port 8000")


def _run_onboarding():
    """Run as the `onboarding` FastMCP tool server (reserved SYSTEM_ID=onboarding).

    Serves the stateless onboarding-tools server from
    onboarding_runtime_server.create_onboarding_server through the shared
    runtime_asgi.serve_mcp_app assembly (the /ping + /invocations warmup, the
    /mcp vs /mcp/ trailing-slash alias the gateway depends on, and the ADR-0049
    identity middleware). No connection config is needed in the payload — the
    onboarding tools resolve connections per-system at call time
    (connection_resolver, ADR-0031) and keep durable state in the DynamoDB
    session store. Must run in VPC mode (Postgres probes / Neptune / the
    session store are VPC-private, ADR-0024/0032).
    """
    from .onboarding_runtime_server import create_onboarding_server, TARGET_NAME
    from .runtime_asgi import serve_mcp_app

    mcp = create_onboarding_server(port=8000)
    serve_mcp_app(mcp, TARGET_NAME, f"Starting {TARGET_NAME} MCP tool server on port 8000")


def _run_query_bridge():
    """Run as the `query-bridge` FastMCP tool server (reserved SYSTEM_ID=query-bridge).

    Serves the stateless async-query bridge (submit_query / get_query_result /
    get_prompts) from query_bridge_server.create_query_bridge_server through
    the shared runtime_asgi.serve_mcp_app assembly (the /ping + /invocations
    warmup, the /mcp vs /mcp/ trailing-slash alias the gateway depends on, and
    the ADR-0049 identity middleware). No model, no connection config, no graph
    access: the tools call the ADT app over HTTP (ADT_APP_URL + the
    x-quick-auth token, read from QUICK_GATEWAY_TOKEN_REF or the plaintext
    QUICK_GATEWAY_TOKEN), so like `onboarding` it does not run the "system"
    role's GRAPH_IDENTIFIER boot assert; a missing ADT_APP_URL is reported
    honestly per tool call rather than crashing the runtime.
    """
    from .query_bridge_server import create_query_bridge_server, TARGET_NAME
    from .runtime_asgi import serve_mcp_app

    mcp = create_query_bridge_server(port=8000)
    serve_mcp_app(mcp, TARGET_NAME, f"Starting {TARGET_NAME} MCP tool server on port 8000")


def _is_explore_graph(name: str) -> bool:
    """True for the Neptune knowledge-graph traversal tool, whether it arrives
    as the bare in-process name (``explore_graph``, Mode-B / orchestrator) or the
    gateway-prefixed name the AgentCore Gateway emits in agent mode
    (``digital-thread___explore_graph``). Matching only the bare name made the
    ``neptune_queries`` counter always report 0 under ``--query-mode agent``."""
    return query_intelligence.is_graph_query(name)


def _check_for_discoveries(tool_calls_log: list[dict], tool_inputs: dict, tool_results: dict,
                           query_context: str, session_id: str):
    """Detect the single most significant cross-system value flow per query.
    
    Rules:
    - Only detect DIRECTIONAL flow: value appears in system A's RESULTS first,
      then in system B's WHERE clause (inputs) — not just shared values.
    - Don't create mappings between MES systems (they share the same DB).
    - Use the SCHEMA name from the SQL (FROM clause) not the tool/system name.
    - Limit to 1 proposed mapping per query (the first directional flow found).
    - Deduplicate against existing DynamoDB records.
    """
    import re as _re

    if not tool_calls_log or len(tool_calls_log) < 3:
        return

    neptune_calls = [t for t in tool_calls_log if _is_explore_graph(t["name"])]
    gateway_calls = [
        t for t in tool_calls_log
        if not _is_explore_graph(t["name"]) and not t["name"].endswith("get_system_schema")
    ]

    if not neptune_calls or len(gateway_calls) < 3:
        return

    # MES systems all share the same underlying database — don't create mappings between them
    MES_SYSTEMS = {"mes-mil", "mes-det", "mes-cr"}

    # Process tool calls IN ORDER to detect directional flow
    # Track: which values have we SEEN in results so far, and from which system
    seen_values = {}  # value → first system that returned it

    # Also track inputs: which system used which value in a WHERE clause
    used_values = []  # list of {"sys": sys_id, "field": table.field, "value": value}

    uuid_re = _re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', _re.I)
    prefixed_re = _re.compile(r'[A-Z]{2,}-[A-Z0-9][-A-Z0-9]{3,}')

    for tool_call in tool_calls_log:
        tool_id = tool_call.get("toolUseId", "")
        tool_name = tool_call.get("name", "")
        input_str = tool_inputs.get(tool_id, "")
        result_str = tool_results.get(tool_id, "")

        # Extract system ID from tool name
        sys_id = None
        if "___" in tool_name:
            sys_id = tool_name.split("___")[0]
        if not sys_id:
            continue

        # Extract the SCHEMA from the SQL FROM clause (more accurate than tool name)
        schema_from_sql = ""
        if input_str:
            # Look for schema.table pattern in FROM clause
            schema_match = _re.search(r'FROM\s+(\w+)\.(\w+)', input_str, _re.IGNORECASE)
            if schema_match:
                schema_from_sql = schema_match.group(1)
            else:
                from_match = _re.search(r'FROM\s+(\w+)', input_str, _re.IGNORECASE)
                if from_match:
                    schema_from_sql = from_match.group(1)

        # Determine the "real" system based on schema name
        real_sys = sys_id  # default to tool name
        if schema_from_sql:
            if 'erp_data' in schema_from_sql or schema_from_sql == 'public':
                real_sys = 'erp'
            elif 'mes_milwaukee' in schema_from_sql:
                real_sys = 'mes-mil'
            elif 'mes_detroit' in schema_from_sql:
                real_sys = 'mes-det'
            elif 'mes_cedarrapids' in schema_from_sql:
                real_sys = 'mes-cr'
            elif 'cmms' in schema_from_sql:
                real_sys = 'cmms'

        # Record WHERE clause values (inputs to this system)
        if input_str:
            table_match = _re.search(r'FROM\s+(\w+)', input_str, _re.IGNORECASE)
            table_name = table_match.group(1) if table_match else ""
            matches = _re.findall(r"(\w+)\s*=\s*'([^']{4,})'", input_str)
            for field, value in matches[:5]:
                if field.endswith("_id") or field in ("lot_number", "batch_id", "batch_number", "production_run_id"):
                    full_field = f"{table_name}.{field}" if table_name else field
                    used_values.append({"sys": real_sys, "field": full_field, "value": value})

        # Record result values
        if result_str:
            uuids = uuid_re.findall(result_str)
            tags = prefixed_re.findall(result_str)
            for val in (uuids[:10] + tags[:5]):
                if val not in seen_values:
                    seen_values[val] = real_sys

    # DETECTION: Find the first value that was RETURNED by system A and then USED by system B
    discovery = None
    for used in used_values:
        value = used["value"]
        if value in seen_values:
            source_sys = seen_values[value]
            target_sys = used["sys"]
            # Skip same-system
            if source_sys == target_sys:
                continue
            # Skip MES-to-MES (same underlying DB, same schema structure)
            if source_sys in MES_SYSTEMS and target_sys in MES_SYSTEMS:
                continue
            # Found a real cross-system directional flow
            discovery = {
                "from": source_sys,
                "to": target_sys,
                "field": used["field"],
                "value": value,
            }
            break  # Only log ONE per query

    if not discovery:
        return

    # Write to DynamoDB (deduplicate against existing)
    try:
        from .self_learning import log_proposed_mapping, get_all_mappings
        # redundant since S3.1 lookup-before-write; removable later
        existing = get_all_mappings()
        field_short = discovery["field"].split(".")[-1]
        existing_keys = {f"{m.get('from_system','')}-{m.get('to_system','')}-{m.get('from_field','').split('.')[-1]}" for m in existing}
        check_key = f"{discovery['from']}-{discovery['to']}-{field_short}"
        
        if check_key in existing_keys:
            return  # Already exists

        # E4 measure gate: a measure-like endpoint (count/quantity/rate/size/…)
        # is never a plausible join key — mirror join_discovery's own defensive
        # emit gate on the FINAL emit (the used_values recorder is _id-focused,
        # but result-side matches can leak measures). Best-effort: a raising
        # import/predicate falls through to today's behavior (the emit stays
        # inside this existing thin fail-loud guard).
        try:
            from .join_discovery import is_measure_like
            if is_measure_like(field_short):
                logger.info(
                    f"Self-learning: skipping measure-like join endpoint "
                    f"'{discovery['field']}' ({discovery['from']}→{discovery['to']}, "
                    f"session={session_id})"
                )
                return
        except Exception:  # nosec B110 - best-effort path; failure is deliberately non-fatal
            pass  # best-effort gate — never blocks the emit on its own failure

        log_proposed_mapping(
            from_system=discovery["from"],
            from_field=discovery["field"],
            from_value=discovery["value"][:50],
            to_system=discovery["to"],
            to_field=discovery["field"],
            to_value=discovery["value"][:50],
            concept=field_short.replace("_id", "").replace("_", "-"),
            confidence=0.85,
            query_context=query_context[:500],
            tool_calls_count=len(tool_calls_log),
            proposed_type="join_path",
        )
        logger.info(f"Self-learning: {discovery['from']}→{discovery['to']} via {discovery['field']} (session={session_id})")
    except Exception as e:
        # Thin fail-loud guard (ADR 0003 F): log_proposed_mapping now RAISES on
        # backend failure; surface it loudly but never kill the user query.
        logger.warning(f"Self-learning log failed: {e}", exc_info=True)


# The guessed (nonexistent) column name lives quoted right after the
# COLUMN_NOT_FOUND token in an Athena/Trino error, e.g.
# ``COLUMN_NOT_FOUND: line 1:8: Column 'use_date' cannot be resolved``. GENERIC —
# matches the engine's error grammar, not any dataset column.
import re as _re_cnf  # noqa: E402 - module-local alias kept next to its consumer

_COLUMN_NOT_FOUND_TOKEN = "COLUMN_NOT_FOUND"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
_GUESSED_COLUMN_RE = _re_cnf.compile(r"[Cc]olumn\s+['\"]([A-Za-z_][\w.]*)['\"]")


def _system_id_from_tool_name(tool_name: str) -> str:
    """The owning system id of a gateway tool call. Gateway tools are named
    ``<system_id>___<tool>`` (generic convention, no dataset literals); returns the
    system id, or ``""`` for a non-prefixed tool (e.g. explore_graph)."""
    if tool_name and "___" in tool_name:
        return tool_name.split("___", 1)[0]
    return ""


def _persist_column_not_found_guidance(tool_calls_log: list[dict], tool_inputs: dict,
                                       tool_results: dict) -> int:
    """File bounded pending guidance for deterministic column-miss corrections.

    Deduplicates each ``(system, table, guessed column)`` within the turn and
    returns only the count whose writer result is explicitly ``filed``.
    """
    from .athena_tools import _referenced_tables
    from .self_learning import append_query_guidance_note

    written = 0
    seen: set[tuple[str, str, str]] = set()
    for tc in tool_calls_log or []:
        tid = tc.get("toolUseId", "")
        result = tool_results.get(tid, "") or ""
        if _COLUMN_NOT_FOUND_TOKEN not in result:
            continue
        system_id = _system_id_from_tool_name(tc.get("name", ""))
        if not system_id:
            continue
        # Guessed column: quoted, immediately after the COLUMN_NOT_FOUND token.
        seg = result[result.index(_COLUMN_NOT_FOUND_TOKEN):]
        col_match = _GUESSED_COLUMN_RE.search(seg)
        if not col_match:
            continue
        guessed = col_match.group(1).split(".")[-1]
        # Failing table: from the failing SQL input's FROM/JOIN clause.
        tables = _referenced_tables(tool_inputs.get(tid, "") or "")
        table = tables[0] if tables else ""
        if not table:
            continue
        key = (system_id, table, guessed)
        if key in seen:
            continue
        seen.add(key)
        note = (
            f"{table}: no column '{guessed}' — use the exact concept-mapped columns "
            "from get_system_schema"
        )
        try:
            if append_query_guidance_note(system_id, note) == "filed":
                written += 1
        except Exception as e:  # noqa: BLE001 - best-effort learning
            logger.debug(f"column-not-found guidance skipped: {e}")
    return written


def _recommendation_accepted(rec: dict) -> bool:
    """LEVER 3: accept an agent-analysis recommendation IFF it carries a real
    table + a single real field AND adds signal — INCLUDING a concept->column
    binding (naming which concept/role a specific real column holds), which is the
    useful anti-guessing hint. Still rejects the true noise: empty/``N/A`` field or
    table, infrastructure fixes (endpoint/connection), and ``ensure ... exists``.

    Does NOT weaken the guards that the field is ONE real column and that the
    table/field must appear in the results — those remain the analysis prompt's
    contract and the presence checks below. Pure/deterministic; GENERIC.
    """
    field = (rec.get("field") or "").strip()
    table = (rec.get("table") or "").strip()
    annotation = (rec.get("annotation") or "").lower()
    if not field or field == "N/A" or not table or table == "N/A":
        return False
    if "endpoint" in annotation or "connection" in annotation:
        return False
    if "ensure" in annotation and "exists" in annotation:
        return False
    return True


def _extract_result_rows(result_str: str) -> list[dict]:
    """Best-effort parse of a tool-result string into a list of column→value dicts.

    Generic + purely structural (no dataset literals): accepts a top-level JSON
    array of objects, or an object carrying such an array under a common results key
    (rows/results/data/records/items). Returns ``[]`` for anything else — including
    truncated or non-JSON results — so the caller degrades cleanly.
    """
    import json as _json
    text = (result_str or "").strip()
    if not text:
        return []
    try:
        data = _json.loads(text)
    except Exception:  # noqa: BLE001 - non-JSON / truncated result → nothing to parse
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("rows", "results", "data", "records", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _check_for_reason_code_mappings(tool_calls_log: list[dict], tool_inputs: dict,
                                    tool_results: dict, session_id: str) -> int:
    """Wire the generic reason-code → failure-mode learner into the post-query flow.

    Reuses ``self_learning.propose_reason_code_mappings`` (the offline-tested core) —
    this layer only resolves the owning system id from the gateway tool name and
    best-effort-parses rows from each tool result. Per-system proposal failures are
    isolated (best-effort learning, ADR 0003 F) so one bad result can never abort the
    others or kill the user query. Returns the number of proposals created (tests).
    """
    from .self_learning import propose_reason_code_mappings

    total = 0
    for tc in tool_calls_log or []:
        system_id = _system_id_from_tool_name(tc.get("name", ""))
        if not system_id:
            continue
        rows = _extract_result_rows(tool_results.get(tc.get("toolUseId", ""), ""))
        if not rows:
            continue
        try:
            total += len(propose_reason_code_mappings(rows, system_id))
        except Exception as e:  # noqa: BLE001 - best-effort learning, never kill query
            logger.warning(
                "reason-code mapping proposal failed system=%s category=%s",
                system_id, type(e).__name__,
            )
    if total:
        logger.info(f"Self-learning: {total} reason-code→failure-mode proposal(s) "
                    f"(session={session_id})")
    return total


def _insight_system_mentions(block: str, known_systems: list[dict]) -> list[tuple[int, str]]:
    """Catalog-derived system mentions in one insight block.

    ``known_systems`` entries (``{"id", "name", ...}``) supply the candidate
    tokens, matched case-insensitively and WORD-BOUNDARY-anchored per token
    (``\\b`` + ``re.escape``) — a short id like "erp" must NOT match inside
    "enterprise"/"interpret", while hyphenated ids like "mes-mil" still match
    at word boundaries (``\\b`` anchors on the alphanumeric edges). Mentions
    are ordered by first occurrence and deduped on the canonical id
    (``system_normalize.normalize_system_id``: display names → catalog ids;
    knowledge-graph self-references normalize to ``None`` and are dropped).
    """
    import re as _re

    from .system_normalize import normalize_system_id

    mentions: list[tuple[int, str]] = []
    seen: set[str] = set()
    for entry in known_systems or []:
        hits = []
        for token in (entry.get("id"), entry.get("name")):
            token = str(token or "").strip()
            if not token:
                continue
            m = _re.search(rf"\b{_re.escape(token)}\b", block, _re.IGNORECASE)
            if m:
                hits.append((m.start(), token))
        if not hits:
            continue
        first_idx, token = min(hits)
        canonical = normalize_system_id(token, known_systems)
        if not canonical or canonical in seen:
            continue  # None → knowledge-graph self-reference: drop mention
        seen.add(canonical)
        mentions.append((first_idx, canonical))
    mentions.sort()
    return mentions


# Generic derivation markers for the entailed-pattern rejection (P11) — SHAPE
# tokens only, never dataset terms (ADR-0044 posture).
_INSIGHT_DERIVATION_MARKERS = (
    "ratio of", "percentage of", "percent of", "proportion of", "fraction of",
    "divided by", "derived from", "calculated from", "computed from",
    "defined as", "sum of",
)
# Tokens too generic to count as a shared quantity stem across a derivation.
_INSIGHT_GENERIC_TOKENS = frozenset({
    "data", "value", "values", "total", "totals", "count", "counts",
    "number", "numbers", "system", "systems", "with", "from", "that",
    "this", "when", "then", "between", "across", "over", "their",
})


def _insight_shared_stem(left: str, right: str) -> bool:
    """True when a significant token stem (≥4 chars, non-generic) appears on
    both sides of a derivation marker — the quantity relates to itself."""
    import re as _re

    def _tokens(text: str) -> set:
        return {t for t in _re.findall(r"[a-z][a-z0-9]{3,}", text.lower())
                if t not in _INSIGHT_GENERIC_TOKENS}

    return bool(_tokens(left) & _tokens(right))


# Correlational-shape markers that EXEMPT a narrative from the coverage-meta
# rejection: "when ERP holds a rush order, MES records show
# higher scrap rates" is a genuine correlation even though a coverage verb
# ("holds") and noun ("records") straddle the gap window. Generic shape
# stems only (ADR-0044 posture).
_INSIGHT_CORRELATIONAL_MARKERS_RE = (
    r"\b(?:correlat\w*|preced\w*|higher|increas\w*|rates?)\b"
)


def _reject_insight_pattern(pattern: str) -> "str | None":
    """P11 narrative-shape gate — a stated skip reason, or ``None`` to keep.

    Generic SHAPE heuristics only (pure, offline-testable, no dataset terms):
    (a) mathematically entailed — the narrative relates a quantity to its own
    derivation (a derivation marker with a shared significant token stem on
    both sides); (b) coverage meta-observations ("system X has data for Y"
    shapes) — inventory facts, not correlations. A correlational marker
    (``_INSIGHT_CORRELATIONAL_MARKERS_RE``) exempts the narrative from the
    coverage rejection (never from the entailment check).
    """
    import re as _re

    lowered = " ".join((pattern or "").split()).lower()
    for marker in _INSIGHT_DERIVATION_MARKERS:
        idx = lowered.find(marker)
        if idx >= 0 and _insight_shared_stem(lowered[:idx],
                                             lowered[idx + len(marker):]):
            return "mathematically entailed (quantity related to its own derivation)"
    if _re.search(_INSIGHT_CORRELATIONAL_MARKERS_RE, lowered):
        return None
    if _re.search(
        r"\b(?:has|have|contains?|holds?|stores?|provides?|tracks?|includes?|covers?)\b"
        r"(?:\s+\S+){0,4}?\s(?:data|records?|information|entries|fields?|history|coverage|tables?)\b",
        lowered,
    ):
        return "coverage meta-observation (inventory fact, not a correlation)"
    return None


# Same opener shapes the display strips (public_answer._PROPOSAL_OPEN_SRC),
# including a bare marker line under a ``---`` rule, so a block the reader
# never sees is still offered for review.
_CORRELATION_BLOCK_RE = _re_cnf.compile(
    r'(?:-{2,}[ \t]*CORRELATION[- ]PROPOSAL[ \t]*-{2,}'
    r'|^[ \t]*CORRELATION[- ]PROPOSAL[ \t]*$)'
    r'\s*(.+?)\s*-{2,}[ \t]*END[- ]PROPOSAL[ \t]*-{2,}',
    _re_cnf.DOTALL | _re_cnf.MULTILINE,
)


def _extract_correlation_insights(full_text: str, known_systems: list[dict]) -> list[dict]:
    """Parse ``---CORRELATION-PROPOSAL---`` blocks into ``insight`` proposal dicts.

    Module-level (not inline in the SSE generator) so the extraction is
    offline-unit-testable. Narrative correlation patterns are FINDINGS, not
    join rules: each returned dict carries ``proposed_type="insight"`` with
    EMPTY ``from_field``/``to_field`` (a sentence is never a table.column
    pair) and the full pattern text preserved in ``query_context``
    (``Auto-analysis: <pattern>`` — ingestion strips the prefix to form the
    annotation digest, so repeated identical narratives dedup).

    System detection is catalog-derived (``_insight_system_mentions``). A
    correlation relates TWO DISTINCT systems (P11): blocks with fewer than
    two canonical mentions are skipped (``to_system`` is always non-empty and
    differs from ``from_system``), and mathematically-entailed or coverage
    meta-observation narratives are rejected (``_reject_insight_pattern``).
    """
    import re as _re

    from .proposal_scoring import confidence_word_to_float

    blocks = _CORRELATION_BLOCK_RE.findall(full_text)
    insights: list[dict] = []
    for block in blocks[:3]:  # Max 3 per query
        pattern_match = _re.search(r'Pattern:\s*(.+?)(?:\n|$)', block)
        confidence_match = _re.search(r'Confidence:\s*(\w+)', block)
        pattern = pattern_match.group(1).strip() if pattern_match else block[:100]
        confidence = confidence_word_to_float(
            confidence_match.group(1) if confidence_match else "",
            high_if_contains=True)
        mentions = _insight_system_mentions(block, known_systems)
        if len(mentions) < 2:
            continue  # P11: fewer than two distinct systems is not a correlation
        reason = _reject_insight_pattern(pattern)
        if reason:
            logger.debug("insight block skipped: %s", reason)
            continue
        insights.append({
            "proposed_type": "insight",
            "from_system": mentions[0][1],
            "to_system": mentions[1][1],
            "from_field": "",
            "to_field": "",
            "concept": "correlation",
            "confidence": confidence,
            "query_context": f"Auto-analysis: {pattern}"[:500],
        })
    return insights


def _count_correlation_writebacks(tool_calls: list[dict], tool_results: dict) -> int:
    """Count successful ``save_correlation`` tool write-backs in this turn.

    A correlation save is a NEW FINDING WRITE, not an application of an already
    accepted ValueMapping/IdentityRule. Keep it separate from ``rules_applied``
    and count only the tool's explicit ``status=logged,type=insight`` success
    envelope with a mapping id; skipped, malformed, and unrelated results do not
    count. Pure/offline-testable.
    """
    import json

    count = 0
    for call in tool_calls or []:
        name = str(call.get("name") or "")
        if name != "save_correlation" and not name.endswith("___save_correlation"):
            continue
        raw = (tool_results or {}).get(call.get("toolUseId"), "")
        if not isinstance(raw, str):
            continue
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(result, dict)
            and result.get("status") == "logged"
            and result.get("type") == "insight"
            and result.get("mapping_id")
        ):
            count += 1
    return count


def _validated_wasted_calls(raw_calls: list, tool_calls: list[dict],
                            tool_results: dict, *,
                            flagged_steps: "set | None" = None) -> list[dict]:
    """Keep LLM-authored no-data rows only for actually failed/empty steps.

    The analysis model critiques the current turn's tool-call log; it does not
    read Self-Learning proposal history. Treat its step references as untrusted:
    a row is displayed only when its 1-based step exists and the shared
    deterministic result classifier says that exact call failed or found no
    data. This prevents successful same-system comparisons from being presented
    as mapping proposals or no-data evidence. Pure/offline-testable.

    ``flagged_steps``: when the mechanical determinism detector supplies
    the set of flagged step numbers, it is the AUTHORITY — a row is kept iff
    its step is in the set (covers exact duplicates, which succeeded and so
    would fail the failed-result check). ``None`` keeps the legacy
    failed-result validation.
    """
    valid: list[dict] = []
    calls = tool_calls or []
    results = tool_results or {}
    for row in raw_calls or []:
        if not isinstance(row, dict):
            continue
        step = row.get("step")
        if (
            isinstance(step, bool)
            or not isinstance(step, int)
            or step < 1
            or step > len(calls)
        ):
            continue
        if flagged_steps is not None:
            if step in flagged_steps:
                valid.append(row)
        else:
            tool_id = calls[step - 1].get("toolUseId")
            if query_intelligence.is_failed_result(results.get(tool_id, "")):
                valid.append(row)
        if len(valid) >= 5:
            break
    return valid


def _clean_run_verdict(det: dict, total_tools: int) -> dict:
    """COMPUTED positive ``agent_analysis`` verdict for a zero-waste trace.

    Emitted with NO LLM call (nothing to hallucinate — the mechanical detector
    found no waste); same payload shape the LLM path yields. A perfect trace
    deserves an explicit verdict, not silence.
    """
    grounded = int(det.get("deterministic_calls") or 0)
    pct = int(det.get("deterministic_pct") or 0)
    if grounded == total_tools:
        sentence = (
            f"{grounded} of {total_tools} calls grounded in knowledge-graph "
            f"evidence — no wasted calls detected. Every system query used "
            f"exact identifiers resolved from the graph."
        )
    else:
        sentence = (
            f"{grounded} of {total_tools} calls grounded in knowledge-graph "
            f"evidence; no wasted calls detected."
        )
    return {"summary": sentence, "deterministic_pct": pct,
            "wasted_calls": [], "recommendations": []}


def _record_rule_usage_hook(tool_results: dict, prompt: str, *,
                            precomputed_ids: "set | None" = None,
                            prefetched_rows: "list | None" = None,
                            evidence_class: str = "tool-text",
                            route_ctx: "dict | None" = None,
                            session_id: str = "") -> int:
    """Post-run usage sequence: rule-usage bump, then artifact-usage ledger.

    Scans the turn's tool-result text for applied-node ids
    (``usage_tracking.extract_rule_node_ids``) and bumps the matching ACCEPTED
    proposals (``self_learning.record_rule_usage``); ``precomputed_ids``/
    ``prefetched_rows`` forward the summary block's single extraction and GSI
    fetch so the turn never re-scans or re-queries. The SAME results then feed
    ``_record_artifact_usage_hook`` (ADR-0050 ledger) under its own swallow,
    stamped with the caller's honest ``evidence_class`` (ADR-0052 (b)). Empty
    extraction + a ``route_ctx`` arms the route-intersection fallback
    (report-only turns, ``evidence="route-intersection"``, own swallow).
    SKIPS automated briefings AND synthetic runs (``_is_synthetic_session``
    on ``session_id`` — B2 try-it replays and feedback-exploration re-runs
    never inflate usage evidence; the ONE gate covers rule bumps, ledger,
    and fallback alike).
    Returns the RULES bumped; the call site adds a swallow guard.
    """
    if _is_synthetic_session(session_id) or "briefing" in (prompt or "").lower():
        return 0
    if precomputed_ids is not None:
        ids = precomputed_ids
    else:
        from .usage_tracking import extract_rule_node_ids

        ids = extract_rule_node_ids(list((tool_results or {}).values()))
    bumped = 0
    if ids:
        from . import self_learning

        if prefetched_rows is not None:
            bumped = self_learning.record_rule_usage(
                ids, prefetched_rows=prefetched_rows)
        else:
            bumped = self_learning.record_rule_usage(ids)
    # ADR-0050 ledger: a ledger failure never affects the rule count returned.
    try:
        _record_artifact_usage_hook(tool_results, prompt, precomputed_ids=ids,
                                    accepted_rows=prefetched_rows,
                                    evidence_class=evidence_class)
    except Exception as e:
        logger.debug(f"Artifact usage recording failed (non-critical): {e}")
    if not ids:
        _maybe_bump_route_intersection(prompt, route_ctx)
    return bumped


def _record_empty_join_signal(tool_calls_log: list, tool_results: dict,
                              prompt: str, session_id: str) -> bool:
    """ADR-0059 shape 2 (``empty_join``) — post-turn, best-effort, NEVER raises.

    Builds the per-system envelope map from THIS turn's gateway tool results
    (system id from the ``<system>___<tool>`` name prefix —
    ``_system_id_from_tool_name``, pure; the graph is never read) and records
    ONE observation when the pure classifier
    (``query_intelligence.classify_empty_join``) flags exactly one failed leg
    against at least one healthy one — the shape that suggests an identifier
    translation gap. Multiple calls to the same system keep a HEALTHY result
    over a failed one (an intra-turn retry that recovered is not a failed leg).

    Gating mirrors ``_record_rule_usage_hook``: synthetic sessions and
    automated briefings never write. ``turn_id`` is the turn's first
    toolUseId (unique per turn — per-turn dedup in the accumulator). The one
    network call is ``record_failure_signal``'s single conditional write,
    itself a never-raise boundary. Returns True iff an observation was
    counted (observability/tests).
    """
    try:
        if _is_synthetic_session(session_id) or "briefing" in (prompt or "").lower():
            return False
        envelopes: dict = {}
        turn_id = ""
        for call in tool_calls_log or []:
            tool_id = str(call.get("toolUseId") or "")
            turn_id = turn_id or tool_id
            system_id = _system_id_from_tool_name(str(call.get("name") or ""))
            text = (tool_results or {}).get(tool_id)
            if not system_id or system_id == "digital-thread" \
                    or not isinstance(text, str) or not text:
                continue
            prev = envelopes.get(system_id)
            if prev is None or query_intelligence.is_failed_result(prev):
                envelopes[system_id] = text
        failed_leg = query_intelligence.classify_empty_join(envelopes)
        if not failed_leg or not turn_id:
            return False
        from .failure_signals import record_failure_signal
        return record_failure_signal(
            "empty_join", failed_leg,
            {"failed_system": failed_leg,
             "healthy_systems": sorted(s for s in envelopes if s != failed_leg)},
            turn_id)
    except Exception as e:  # noqa: BLE001 - a broken accumulator never breaks a turn
        logger.debug(f"Empty-join signal recording failed (non-critical): {e}")
        return False


#: Persistence bound for a miss TERM: the term is model/tool-derived text that
#: rides ``record_failure_signal`` into a durable row (digest + subject_json),
#: so it is bounded HERE, before persistence.
_MISS_TERM_MAX_LEN = 120


def _bounded_miss_term(raw) -> str:
    """Single-line, printable, length-capped miss term (``""`` when unusable).

    Control or other non-printable characters (including any embedded
    newline) REJECT the term outright — never normalized into a different
    term — and an overlong term is capped at :data:`_MISS_TERM_MAX_LEN`
    characters. An empty return means "write nothing".
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip().lower()
    if not text or any(not ch.isprintable() for ch in text):
        return ""
    return text[:_MISS_TERM_MAX_LEN]


def _record_metric_term_miss_signal(tool_calls_log: list, tool_results: dict,
                                    response_text: list, prompt: str,
                                    session_id: str) -> bool:
    """Terminology-miss (``terminology_miss``) — post-turn, NEVER raises.

    The orchestrator-side hook (``orchestrator._observe_identifier_miss``)
    counts a miss when the fenced marker appears in a TOOL result; this branch
    covers the FINAL-ANSWER evidence: a marker surviving un-stripped into the
    answer means the recovery loop did NOT translate the term — the question's
    target term has no confirmed vocabulary mapping. ONE (system, term)
    observation rides the SAME writer (``record_failure_signal`` → threshold →
    pending ``steward_question`` proposal; never-auto-accept lifecycle, gates
    apply). Deterministic attribution or nothing: the system id and turn_id
    come from the tool call whose RESULT carried the same token, which also
    makes this write and the orchestrator hook's collapse into ONE counted
    observation per turn (turn_ids dedup) — never a double count. Gating
    mirrors ``_record_empty_join_signal``; the answer is never mutated.
    """
    try:
        if _is_synthetic_session(session_id) or "briefing" in (prompt or "").lower():
            return False
        answer = "".join(response_text or [])
        if "IDENTIFIER-MISS" not in answer:
            return False
        from .system_agent import _MISS_FENCE_RE, _MISS_TOKEN_RE
        fence = _MISS_FENCE_RE.search(answer)
        token_match = _MISS_TOKEN_RE.search(fence.group(0)) if fence else None
        if not token_match:
            return False
        term = _bounded_miss_term(token_match.group(1))
        if not term:
            return False
        for call in tool_calls_log or []:
            tool_id = str(call.get("toolUseId") or "")
            system_id = _system_id_from_tool_name(str(call.get("name") or ""))
            text = (tool_results or {}).get(tool_id)
            if not system_id or system_id == "digital-thread" or not tool_id \
                    or not isinstance(text, str) or "IDENTIFIER-MISS" not in text:
                continue
            carried = _MISS_TOKEN_RE.search(text)
            if not carried or _bounded_miss_term(carried.group(1)) != term:
                continue
            from .failure_signals import record_failure_signal
            return record_failure_signal(
                "terminology_miss", system_id, {"term": term}, tool_id)
        return False
    except Exception as e:  # noqa: BLE001 - a broken accumulator never breaks a turn
        logger.debug(f"Metric-term miss recording failed (non-critical): {e}")
        return False


# ADR-0063 D5: cap the per-turn business-context reads this hook performs
# (one bounded graph read per queried system; a wide fan-out turn must
# not turn a post-turn accumulator into a read storm).
_CONTEXT_SIGNAL_MAX_SYSTEMS = 4


def _record_context_not_reflected_signal(tool_calls_log: list, response_text: list,
                                         prompt: str, session_id: str) -> list:
    """ADR-0063 D5 (``context_not_reflected``) — post-turn, best-effort, NEVER raises.

    A steward's business-context correction can be saved, enabled and reaching
    the agent while answers still contradict it (measured twice: two goldens
    resisted context edits for days, then passed immediately once the same
    knowledge was a guidance-note REQUIREMENT). This hook accumulates that
    evidence so the system asks instead of waiting for someone to notice.

    Scoped to the systems THIS turn actually queried (the
    ``<system>___<tool>`` prefix — pure, no graph read for routing), and the
    detector itself refuses to count without every piece of evidence. Gating
    mirrors ``_record_empty_join_signal``: synthetic sessions and automated
    briefings never write. Returns the subjects counted (observability/tests).
    """
    try:
        if _is_synthetic_session(session_id) or "briefing" in (prompt or "").lower():
            return []
        answer = "".join(response_text or "")
        if not answer:
            return []
        turn_id, systems = "", []
        for call in tool_calls_log or []:
            turn_id = turn_id or str(call.get("toolUseId") or "")
            system_id = _system_id_from_tool_name(str(call.get("name") or ""))
            if system_id and system_id != "digital-thread" and system_id not in systems:
                systems.append(system_id)
        if not turn_id or not systems:
            return []
        from .business_context import read_business_context
        from .failure_signals import record_context_not_reflected
        counted: list = []
        for system_id in systems[:_CONTEXT_SIGNAL_MAX_SYSTEMS]:
            context = read_business_context(system_id)
            if not context:  # None == graph read FAILED — never read as absence
                continue
            counted.extend(record_context_not_reflected(
                system_id, context, answer,
                answer_ref=f"{session_id}:{turn_id}", turn_id=turn_id))
        return counted
    except Exception as e:  # noqa: BLE001 - a broken accumulator never breaks a turn
        logger.debug(f"Context-not-reflected recording failed (non-critical): {e}")
        return []


def _record_artifact_usage_hook(tool_results: dict, prompt: str, *,
                                evidence: "object | None" = None,
                                accepted_rows: "list | None" = None,
                                precomputed_ids: "set | None" = None,
                                evidence_class: str = "tool-text") -> int:
    """Artifact-usage ledger bump (ADR-0050) — sibling of the rule-usage hook.

    Extracts the deterministic artifacts this turn actually USED
    (``artifact_usage.extract_artifact_refs``) and bumps their day-bucketed
    counters (``artifact_usage.bump_artifact_usage``) so the vocabulary admin
    surface can show "used in K answers (30d)". Concept refs derive from the
    ACCEPTED ROWS whose rule fired — the production-live path; ``evidence``
    is the optional richer typed-capture source when a caller has one (the
    default report-only entrypoint passes ``None``). ``evidence_class`` is
    the ADR-0052 (b) evidence CLASS stamped on the bumped ledger rows —
    distinct from the typed-capture ``evidence`` object.

    ``precomputed_ids``/``accepted_rows`` forward the rule hook's already-
    computed node ids and prefetched rows, so the turn is never re-scanned
    and the single GSI query is reused (the ``_record_rule_usage_hook``
    idiom).

    SKIPS automated briefings (same gate as the sibling hooks) and SWALLOWS
    every failure — extraction returning empty means zero writes and zero
    behavior change. Module-level so it is offline-unit-testable. Returns the
    number of refs bumped (0 when skipped, empty, or on any failure).
    """
    if "briefing" in (prompt or "").lower():
        return 0
    try:
        from . import artifact_usage

        refs = artifact_usage.extract_artifact_refs(
            tool_results or {}, evidence, accepted_rows=accepted_rows,
            precomputed_ids=precomputed_ids)
        if not refs:
            return 0
        return artifact_usage.bump_artifact_usage(refs, evidence=evidence_class)
    except Exception as e:
        logger.debug(f"Artifact usage extraction/bump failed (non-critical): {e}")
        return 0


def _reasoning_delta_text(event: dict) -> "str | None":
    """Extract forwardable reasoning text from a Strands stream event (Task 1I).

    Strands 1.42 ``ReasoningTextStreamEvent`` payloads look like
    ``{"reasoningText": str, "delta": ..., "reasoning": True}`` — key-disjoint
    from every other branch of the stream filter, so without this branch they
    hit the skip path and produce 30–60s of wire silence between tool batches.
    Returns the text capped at 2KB (same cap as tool inputs), or ``None`` for
    signature/redacted-content reasoning events (no forwardable text).
    """
    text = event.get("reasoningText")
    if isinstance(text, str) and text:
        return text[:2000]
    return None


# The routing-seed step's ``input.question`` (``route_seed_trace.seed_question``):
# the reopened chat rebuilds the same label from the stored turn.
from .route_seed_trace import seed_question as _seed_question  # noqa: E402


# Both Mode-A route-seed headers (full and PARTIAL) end with this literal, so
# one split yields the JSON payload for either variant. The Mode-B graph-first
# seed never contains it — the derivation below is Mode-A-only by construction.
_ROUTE_SEED_RESULT_MARKER = "ROUTE SEED RESULT:\n"


def _seed_holds_tryit_candidate(systems: list) -> bool:
    """Whether a Try-it candidate (``method="tryit-candidate"``) survived the
    route seed's fit, in any system's JoinPath/ValueMapping/IdentityRule list:
    the routing summary's ``tryit_candidate`` flag."""
    for system in systems:
        rules = system.get("rules") or {}
        for kind in ("join_paths", "value_mappings", "identity_rules"):
            items = rules.get(kind)
            if any(isinstance(item, dict) and item.get("method") == "tryit-candidate"
                   for item in (items if isinstance(items, list) else [])):
                return True
    return False


def _planned_with_from_seed(seed) -> "dict | None":
    """Derive the additive ``planned_with`` routing summary from a route seed.

    Parses the JSON payload after ``_ROUTE_SEED_RESULT_MARKER`` and aggregates
    across the seed's systems: deduped shared-concept names (capped per the
    builder's ``MAX_PLANNED_CONCEPTS``/``MAX_PLANNED_CONCEPT_LEN``) plus
    JoinPath/ValueMapping/IdentityRule counts. ``value_mappings`` also counts
    each field pair the IDENTIFIER VALUE MAPPINGS block carries that no
    summary already counts (:func:`_block_value_mappings_beyond_summaries`).
    Fail-soft: no marker (Mode B), no systems, or ANY parse failure gives
    ``None``, so the key stays absent.

    ``authority_rules`` (T1.6, additive) is HONEST-NULL: counted only when at
    least one seed system carries the ``rules.authority_rules`` list (the
    seed-builder marker that authority WAS consulted — an empty list is an
    honest 0); legacy/compact seeds without the key omit it entirely (unknown
    is absent, never a fabricated zero — ADR-0053/0045).
    """
    try:
        if not isinstance(seed, str) or _ROUTE_SEED_RESULT_MARKER not in seed:
            return None
        import json
        fragment = seed.split(_ROUTE_SEED_RESULT_MARKER, 1)[1].lstrip()
        payload = json.JSONDecoder().raw_decode(fragment)[0]  # text may follow
        systems = [s for s in payload.get("systems") or [] if isinstance(s, dict)]
        if not systems:
            return None
        concepts, counts, authority = _seed_concepts_and_counts(systems)
        counts["value_mappings"] += _block_value_mappings_beyond_summaries(seed, systems)
        planned = {"systems": len(systems),
                   "shared_concepts": concepts[:query_intelligence.MAX_PLANNED_CONCEPTS],
                   **counts}
        if authority is not None:
            planned["authority_rules"] = authority
        if _seed_holds_tryit_candidate(systems):
            planned["tryit_candidate"] = True
        return planned
    except Exception:  # noqa: BLE001 - fail-soft: never break the turn on a seed parse
        return None


def _seed_concepts_and_counts(systems: list) -> tuple:
    """``(shared-concept names, rule counts, authority count or None)`` over
    the seed's systems, for :func:`_planned_with_from_seed`."""
    concepts: list[str] = []
    counts = {"join_paths": 0, "value_mappings": 0, "identity_rules": 0}
    authority = None  # honest null until a seed system carries the key
    cap = query_intelligence.MAX_PLANNED_CONCEPT_LEN
    for system in systems:
        for item in system.get("shared_concepts") or []:
            name = item.get("concept") if isinstance(item, dict) else item
            if isinstance(name, str) and name and name[:cap] not in concepts:
                concepts.append(name[:cap])
        rules = system.get("rules") or {}
        for kind in counts:
            items = rules.get(kind)
            counts[kind] += len(items) if isinstance(items, list) else 0
        loaded = rules.get("authority_rules")
        if isinstance(loaded, list):
            authority = (authority or 0) + len(loaded)
    return concepts, counts, authority


#: The field-pair endpoints a ValueMapping summary and an IDENTIFIER VALUE
#: MAPPINGS line share, in the stored orientation.
_VALUE_MAPPING_PAIR = ("from_system", "from_field", "to_system", "to_field")


def _block_value_mappings_beyond_summaries(seed: str, systems: list) -> int:
    """How many field pairs the seed's IDENTIFIER VALUE MAPPINGS block hands
    the model that no per-system ValueMapping summary already counts.

    The summaries are one entry per field pair; budget pressure drops the ones
    the question names no value of, so a seed can carry its stored mappings in
    the block alone. Each block line names its field pair; a compact line
    (no fields) is covered by any summary between the same two systems."""
    from .identifier_translation import parse_value_mappings
    pairs = set()
    for system in systems:
        for item in (system.get("rules") or {}).get("value_mappings") or []:
            if isinstance(item, dict):
                pairs.add(tuple(str(item.get(k) or "") for k in _VALUE_MAPPING_PAIR))
    extra = 0
    for line in parse_value_mappings(seed):
        key = tuple(line[k] for k in _VALUE_MAPPING_PAIR)
        covered = key in pairs or (not (key[1] or key[3]) and any(
            p[0] == key[0] and p[2] == key[2] for p in pairs))
        if not covered:
            pairs.add(key)
            extra += 1
    return extra


# Bound on the per-turn route-seed artifact index (ids + endpoint systems
# only — never full rules). The serialized seed is already budget-bounded
# (~8 KB), so this is a defensive cap, not a working limit.
_SEED_RULE_INDEX_CAP = 100


def _index_seed_rules(index: list, items, kind: str,
                      system_fields: "tuple[str, str]") -> None:
    """Fold one seed rule collection into the bounded artifact index.

    Only entries carrying an ``id`` (jp/ir — seed vm entries have none)
    contribute; each index row is ``{"id", "kind", "systems"}`` — endpoint
    system ids only, never full rules. Deduped; capped.
    """
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        systems = [str(item.get(f)) for f in system_fields if item.get(f)]
        entry = {"id": str(item["id"]), "kind": kind, "systems": systems}
        if entry not in index and len(index) < _SEED_RULE_INDEX_CAP:
            index.append(entry)


def _composed_context_from_text(text) -> "dict | None":
    """Composed-artifact usage block parsed from a Mode-A route seed.

    Sibling of ``_seed_rule_index_from_text`` (same parse, same fail-soft
    posture, zero extra graph queries — the seed was already built this turn).
    Collects each seed system's ``query_guidance`` and hands it to
    ``query_intelligence.build_composed_context``, which reports WHICH learned
    notes reached this turn's prompt.

    Closes the attribution half of the consumption gap: ``rules_applied``
    counts graph RULES, so a learned guidance note — prompt text with no rule
    node — was invisible to it, and a run could not distinguish "no context"
    from "context present and ignored". Fail-soft: no marker (Mode B), no
    systems, or ANY parse failure ⇒ ``None`` (the key stays absent).
    """
    try:
        if not isinstance(text, str) or _ROUTE_SEED_RESULT_MARKER not in text:
            return None
        import json
        fragment = text.split(_ROUTE_SEED_RESULT_MARKER, 1)[1].lstrip()
        payload, _end = json.JSONDecoder().raw_decode(fragment)
        systems = payload.get("systems") or []
        scoped = query_intelligence.build_composed_context_from_metadata(
            systems, payload.get("guidance_delivery_truncation"),
            payload.get("shared_guidance"))
        if scoped:
            return scoped
        guidance_by_system = {}
        for system in systems:
            if not isinstance(system, dict):
                continue
            system_id = system.get("id") or system.get("system_id")
            guidance = system.get("query_guidance")
            if system_id and isinstance(guidance, str):
                guidance_by_system[str(system_id)] = guidance
        return query_intelligence.build_composed_context(guidance_by_system)
    except Exception:  # noqa: BLE001 - fail-soft: never break the turn on a seed parse
        return None


def _seed_rule_index_from_text(text) -> list:
    """Bounded jp/ir artifact index parsed from a Mode-A route seed.

    Sibling of ``_planned_with_from_seed``: accepts the raw seed OR the
    seed-augmented turn text (``raw_decode`` ignores everything after the
    seed's JSON payload). Feeds the ADR-0052 route-intersection ledger
    fallback with zero extra graph queries — the seed was already built this
    turn. Fail-soft: no marker (Mode B), no systems, or ANY parse failure ⇒
    ``[]`` — the fallback simply stays inert.
    """
    try:
        if not isinstance(text, str) or _ROUTE_SEED_RESULT_MARKER not in text:
            return []
        import json
        fragment = text.split(_ROUTE_SEED_RESULT_MARKER, 1)[1].lstrip()
        payload, _end = json.JSONDecoder().raw_decode(fragment)
        index: list = []
        for system in payload.get("systems") or []:
            if not isinstance(system, dict):
                continue
            rules = system.get("rules") or {}
            _index_seed_rules(index, rules.get("join_paths"), "join_path",
                              ("from_system", "to_system"))
            _index_seed_rules(index, rules.get("identity_rules"),
                              "identity_rule",
                              ("source_system", "target_system"))
        return index
    except Exception:  # noqa: BLE001 - fail-soft: the fallback stays inert
        return []


def _route_intersection_refs(seed_rules, queried_system_ids) -> "set[tuple[str, str]]":
    """PURE: seed artifacts whose endpoint systems this turn actually queried.

    ``seed_rules`` is the ``_seed_rule_index_from_text`` shape; the returned
    ``{(kind, id), ...}`` refs feed ``bump_artifact_usage`` with
    ``evidence="route-intersection"`` — the honest-but-weak ADR-0052 class
    ("in routing context AND its systems were queried", NOT "translation
    applied"). Artifacts touching none of the queried systems never match.
    """
    queried = {str(s) for s in (queried_system_ids or ()) if s}
    refs: "set[tuple[str, str]]" = set()
    if not queried:
        return refs
    for entry in seed_rules or ():
        if not isinstance(entry, dict):
            continue
        rule_id, kind = entry.get("id"), entry.get("kind")
        systems = {str(s) for s in (entry.get("systems") or ()) if s}
        if rule_id and kind and systems & queried:
            refs.add((str(kind), str(rule_id)))
    return refs


def _route_ctx(seed_rule_index, tool_names, tool_system_ids=None) -> dict:
    """Bounded routing context for the route-intersection ledger fallback.

    ``systems`` is the tool-name → system-id mapping when the caller already
    holds one (the integrity invocation); ``None`` lets the fallback derive
    it from the called tool names (``_registered_tool_system_ids`` — a
    TTL-cached read, no new per-turn query).
    """
    return {"seed": list(seed_rule_index or ()),
            "tools": [str(n) for n in (tool_names or ()) if n],
            "systems": tool_system_ids}


def _maybe_bump_route_intersection(prompt: str, route_ctx: "dict | None") -> int:
    """Route-intersection ledger fallback (ADR-0052 decision (b)).

    Runs ONLY on report-only turns, only when the stronger extractions
    yielded nothing (the caller gates on empty rule ids), and only with a
    ``route_ctx``: bumps the route-seeded jp/ir artifacts whose endpoint
    systems intersect the systems this turn actually queried, stamped
    ``evidence="route-intersection"``. Strictly additive — never touches
    ``_integrity_rule_usage`` semantics; zero extra graph queries (the seed
    index is parsed from this turn's already-built seed). SKIPS briefings and
    SWALLOWS every failure — a ledger write never fails a turn.
    """
    try:
        if not route_ctx or "briefing" in (prompt or "").lower():
            return 0
        from .config import response_integrity_enforced
        # ADR-0052 (b) writes this fallback "only when the stronger extractions
        # yielded nothing" — and the stronger `relationship-proof` class is an
        # ENFORCE-path extraction: `_integrity_rule_usage` reads
        # `trusted_relationships` and the typed `system_answers` claims, neither
        # of which exists under annotate (ADR-0047 keeps annotate
        # model-cooperation-free — no typed drafts, no relationship
        # verification). Gating this on `== "report-only"` therefore left
        # annotate with NEITHER class, re-creating the "starving usage ledger"
        # that ADR-0052 exists to fix — in the very posture ADR-0047 recommends.
        # Only enforce skips it, because only enforce has the stronger source.
        if response_integrity_enforced():
            return 0
        names = set(route_ctx.get("tools") or ())
        mapping = route_ctx.get("systems")
        if mapping is None:
            mapping = _registered_tool_system_ids(names)
        queried = {mapping[n] for n in names if n in mapping}
        refs = _route_intersection_refs(route_ctx.get("seed"), queried)
        if not refs:
            return 0
        from . import artifact_usage
        bumped = artifact_usage.bump_artifact_usage(
            refs, evidence="route-intersection")
        if bumped:
            logger.info("Route-intersection usage recorded for %d artifact(s)",
                        bumped)
        return bumped
    except Exception as e:  # noqa: BLE001 - a ledger write never fails a turn
        logger.debug(f"Route-intersection usage bump failed (non-critical): {e}")
        return 0


#: Operator-facing text of the paired graph-first (Mode B) ``toolResult`` and
#: the Mode-A fallback. Mode A renders the seed's own findings instead
#: (``route_seed_trace.seed_step_text``); nothing classifies on this literal.
from .route_seed_trace import FALLBACK_TEXT as _ROUTE_SEED_STEP_TEXT  # noqa: E402


def _metric_term_gap_notice(prompt: str, seed: str) -> str:
    """PROACTIVE metric-term resolution notice for this turn, or ``""``.

    Thin fail-open wrapper over ``metric_term_resolution.metric_term_gap_notice``
    (that module owns the shape-based extraction, the stoplist, the vocabulary
    and the notice text — never inline any of it here). Complements the REACTIVE
    ``orchestrator._METRIC_TERM_MISS_RULE``: the rule needs a lookup to have
    missed, and the measured failure was that the model never looked. This
    computes the miss deterministically at prompt-composition time.

    Returns ``""`` — a byte-identical turn — whenever every candidate resolves,
    nothing metric-shaped was asked, or ANY lookup fails. No gate, no
    error_type, no response-integrity semantics touched.
    """
    try:
        from .metric_term_resolution import (
            metric_term_gap_notice, resolution_seed_view)
        # RESOLUTION sees the seed minus guidance prose (a note that mentions a
        # term is an instruction about it, not a definition of it — see
        # metric_term_resolution.resolution_seed_view). DELIVERY is unchanged:
        # the model still receives the full seed, so the notice and the note
        # compose on exactly the turns that need both.
        return metric_term_gap_notice(
            prompt, resolution_seed_view(seed, _ROUTE_SEED_RESULT_MARKER))
    except Exception as e:  # noqa: BLE001 - fail-OPEN: never break a turn
        logger.debug(f"Metric-term pre-check skipped (non-critical): {e}")
        return ""


#: ADR-0066 D6 / ADR-0052 evidence class: a graph-resident metric definition's
#: vocabulary is what let the deterministic pre-check RESOLVE a candidate term
#: (suppressing the gap notice). Records DELIVERY/COMPOSITION only — never a
#: claim the model "followed" the definition (ADR-0063 D7).
_TERM_PRECHECK_RESOLVED_EVIDENCE = "term-precheck-resolved"


def _graph_definition_precheck_ids(candidates: "list[str]", seed: str) -> set:
    """Graph-definition ids whose vocabulary is what resolved ``candidates``.

    Attribution is computed with the pre-check module's OWN resolution
    machinery (never re-implemented): a candidate is credited to a definition
    only when it resolves against that definition's id/label/synonym forms AND
    does NOT resolve against the baseline — ``known_metric_vocabulary()`` minus
    the definitions' contributed forms — or the routing seed. May raise; the
    caller owns the swallow.
    """
    from . import metric_term_resolution as mtr
    from .metric_definition_store import load_definitions
    docs = load_definitions()
    doc_forms: dict = {}
    for doc_id, doc in (docs or {}).items():
        forms: set = set()
        for value in (doc.get("id", ""), doc.get("label", ""),
                      *(doc.get("synonyms") or ())):
            mtr._add_vocabulary_forms(forms, value)
        if forms:
            doc_forms[doc_id] = frozenset(forms)
    if not doc_forms:
        return set()
    graph_forms = frozenset().union(*doc_forms.values())
    baseline = mtr.known_metric_vocabulary() - graph_forms
    seed_normalized = mtr.normalize_term(seed) if seed else ""
    resolved_ids: set = set()
    for term in candidates:
        if mtr._resolves(term, baseline, seed_normalized):
            continue  # the deployment resolved it WITHOUT graph definitions
        for doc_id, forms in doc_forms.items():
            if mtr._resolves(term, forms, ""):
                resolved_ids.add(doc_id)
    return resolved_ids


def _bump_metric_definition_precheck_usage(prompt: str, seed: str) -> int:
    """Usage-ledger bumps for graph definitions that resolved a pre-check term.

    The pre-check read-path arm of the ADR-0052 gate: each definition that
    :func:`_graph_definition_precheck_ids` credits gets one day-bucket bump
    with the delivery-only evidence class ``term-precheck-resolved``.

    Fail-open and bounded: any failure returns ``0`` and never degrades the
    turn; a question with no metric-shaped candidates does no store work, so
    ordinary turns stay byte-identical.
    """
    try:
        from .metric_term_resolution import (
            extract_metric_term_candidates, resolution_seed_view)
        candidates = extract_metric_term_candidates(prompt)
        if not candidates:
            return 0
        resolved_ids = _graph_definition_precheck_ids(
            candidates, resolution_seed_view(seed, _ROUTE_SEED_RESULT_MARKER))
        if not resolved_ids:
            return 0
        from . import artifact_usage
        bumped = artifact_usage.bump_artifact_usage(
            {("metric_definition", doc_id) for doc_id in resolved_ids},
            evidence=_TERM_PRECHECK_RESOLVED_EVIDENCE)
        if bumped:
            logger.info("Pre-check definition usage recorded for %d "
                        "artifact(s)", bumped)
        return bumped
    except Exception as e:  # noqa: BLE001 - a ledger write never fails a turn
        logger.debug(
            f"Metric-definition pre-check usage bump skipped (non-critical): {e}")
        return 0


def _compose_seeded_turn(seed: str, prompt: str,
                         metric_results: "list | None" = None,
                         time_windows: str = "") -> str:
    """Compose the seeded user turn, with the metric-term notice when present.

    The notice rides WITH the seed — before the documented ``\\n\\n---\\n\\n``
    separator — so ``memory_labels.strip_turn_preambles`` still recovers the
    user's question byte-for-byte. No notice ⇒ the pre-check-era composition,
    byte-identical. The ADR-0066 D6 delivery bump rides here too — a ledger
    side-effect only, never a composition change.

    ADR-0078: ``metric_results`` (already sanitized structured data from the
    RESERVED payload key) renders the metric-layer notice AFTER the gap notice
    and before the separator. ``None``/empty ⇒ byte-identical.

    ``time_windows`` (``time_windows.turn_block``) rides directly after the
    seed, before both notices; ``""`` ⇒ byte-identical.

    A concept a metric COMPUTED this turn is dropped from the
    seed's ``unrecorded_terms`` line, so the engine fact ("no system records
    X") does not contradict a computed result for X.
    """
    seed = _drop_computed_from_unrecorded(seed, metric_results)
    notice = _metric_term_gap_notice(prompt, seed)
    _bump_metric_definition_precheck_usage(prompt, seed)
    layer = _metric_layer_notice(metric_results) if metric_results else ""
    preamble = seed + (f"\n\n{time_windows}" if time_windows else "") + (
        f"\n\n{notice}" if notice else "") + (f"\n\n{layer}" if layer else "")
    return f"{preamble}\n\n---\n\n{prompt}"


def _drop_computed_from_unrecorded(seed: str, metric_results) -> str:
    """Rewrite the seed's ``unrecorded_terms`` value, dropping any concept a
    metric COMPUTED this turn is bound to. Fail-soft: no
    marker, no computed concept, no ``unrecorded_terms`` key, or ANY parse
    failure ⇒ ``seed`` unchanged (an engine fact is never silently widened)."""
    try:
        if not isinstance(seed, str) or _ROUTE_SEED_RESULT_MARKER not in seed:
            return seed
        from .metric_turn_notice import computed_concepts
        concepts = computed_concepts(metric_results)
        if not concepts:
            return seed
        import json
        prefix, _, rest = seed.partition(_ROUTE_SEED_RESULT_MARKER)
        fragment = rest.lstrip()
        lead = rest[:len(rest) - len(fragment)]
        payload, end = json.JSONDecoder().raw_decode(fragment)
        line = payload.get("unrecorded_terms") if isinstance(payload, dict) else None
        if not isinstance(line, str) or not line:
            return seed
        from . import concept_notes
        dropped = concept_notes.drop_terms_from_unrecorded_line(line, concepts)
        if dropped == line:
            return seed
        if dropped:
            payload["unrecorded_terms"] = dropped
        else:
            payload.pop("unrecorded_terms", None)
        return (prefix + _ROUTE_SEED_RESULT_MARKER + lead
                + json.dumps(payload, ensure_ascii=False) + fragment[end:])
    except Exception:  # noqa: BLE001 - never break a turn; leave the seed as is
        logger.warning("route seed: unrecorded drop skipped category=%s",
                       "compose")
        return seed


def _compose_unseeded_turn(prompt: str, metric_results: "list | None" = None,
                           time_windows: str = "") -> str:
    """The seed-less turn (route seed failed or empty). ADR-0078: an
    already-computed metric-layer result must SURVIVE graph degradation — the
    notice becomes a seed-independent preamble before the documented
    separator (``memory_labels.strip_turn_preambles`` recovers the question).
    Resolved time windows survive the same way, ahead of the notice. Neither
    ⇒ ``prompt`` unchanged (the pre-ADR degrade, byte-identical)."""
    layer = _metric_layer_notice(metric_results) if metric_results else ""
    preamble = "\n\n".join(part for part in (time_windows, layer) if part)
    return f"{preamble}\n\n---\n\n{prompt}" if preamble else prompt


def _resolved_time_windows(prompt: str) -> str:
    """This turn's RESOLVED TIME WINDOWS block (``time_windows.turn_block``,
    which also hands it to the turn's delegations), ``""`` when the question
    has no date expression or the deployment has no calendar policy. Also
    remembers this turn's chart intent for the delegations
    (:func:`_remember_chart_intent`). Both are per-turn prelude facts: the
    time windows ride each delegation's ``kg_context``; the chart bit rides a
    separate reserved arg. Never raises: additive."""
    _remember_chart_intent(prompt)
    try:
        from .time_windows import turn_block
        return turn_block(prompt)
    except Exception as e:  # noqa: BLE001 - additive; never break a turn
        logger.warning(f"Resolved time windows skipped: {type(e).__name__}")
        return ""


def _remember_chart_intent(prompt: str) -> None:
    """Compute this turn's chart-intent bit from the USER's question and hand
    it to the turn's delegations (``chart_intent.turn_block``): the SiteWise
    history path draws a chart only when the user asked.
    Nothing rides the composed prompt. The bit travels to each delegation in a
    reserved, gateway-owned tool-call arg, never ``kg_context``. Never raises."""
    try:
        from .chart_intent import turn_block
        turn_block(prompt)
    except Exception as e:  # noqa: BLE001 - additive; never break a turn
        logger.warning(f"Chart intent skipped: {type(e).__name__}")


#: ADR-0078 D4 / ADR-0052 evidence class: a graph definition's metric-layer
#: RESULT block was composed into a chat turn. DELIVERY/COMPOSITION only —
#: never a claim the model reported it (ADR-0063 D7).
_METRIC_TURN_COMPOSED_EVIDENCE = "metric-turn-composed"


def _metric_layer_notice(metric_results) -> str:
    """Render the ADR-0078 notice (the ONE renderer). Called only when results
    are present, so a render fault fails CLOSED to the static forbidding
    notice — never to ``""`` (silence would reopen the proxy path)."""
    try:
        from .metric_turn_render import render_or_fallback
        return render_or_fallback(metric_results)
    except Exception as e:  # noqa: BLE001 - composition never breaks a turn
        logger.warning(f"Metric-layer notice render failed; static fallback: "
                       f"{type(e).__name__}")
        return _METRIC_LAYER_FALLBACK_NOTICE


def _pop_metric_layer_results(payload) -> "list[dict]":
    """Pop the RESERVED ``metricLayerResults`` key and re-sanitize it.

    Unconditional (the ``_adt_identity`` / ``tryItCandidate`` discipline): the
    key is removed even when malformed, so it never reaches the model as raw
    payload. The runtime NEVER resolves a metric (it has no source
    connections, ADR-0078 D8) — it only re-bounds what the app computed.

    ABSENT key ⇒ ``[]`` (the ordinary, byte-identical turn). PRESENT key ⇒
    app-side matching named a defined metric, so an invalid value (non-list,
    oversize, malformed items) or a sanitizer fault fails CLOSED to the
    generic forbidding ``invalid`` result — never to silence.
    """
    missing = object()
    try:
        raw = payload.pop(METRIC_LAYER_RESULTS_KEY, missing)
    except Exception:  # noqa: BLE001 - not a mapping: the key cannot be present
        return []
    if raw is missing:
        return []
    try:
        from .metric_turn_render import sanitize_results
        return sanitize_results(raw) or [_invalid_metric_layer_result()]
    except Exception as e:  # noqa: BLE001 - fails CLOSED, never silent
        logger.warning(f"Metric-layer results invalid; forbidding fallback: "
                       f"{type(e).__name__}")
        return [_invalid_metric_layer_result()]


def _bump_metric_layer_composition(metric_results, composed: str,
                                   session_id: str = "") -> int:
    """Usage-ledger bump for GRAPH-defined metrics whose block was COMPOSED.

    Evidence is composition, never receipt: the header must sit in the seeded
    preamble (before the separator) of ``composed``. Synthetic runs and
    briefings write nothing. SWALLOWS every failure; returns refs bumped.
    """
    try:
        session = str(session_id or "")
        if _is_synthetic_session(session) or "briefing" in session.lower():
            return 0
        from .metric_turn_render import METRIC_LAYER_HEADER
        preamble = str(composed or "").partition("\n\n---\n\n")[0]
        if METRIC_LAYER_HEADER not in preamble:
            return 0
        ids = {r.get("metric_id") for r in metric_results or ()
               if r.get("source") == "graph" and r.get("metric_id")}
        if not ids:
            return 0
        from . import artifact_usage
        return artifact_usage.bump_artifact_usage(
            {("metric_definition", mid) for mid in ids},
            evidence=_METRIC_TURN_COMPOSED_EVIDENCE)
    except Exception as e:  # noqa: BLE001 - a ledger write never fails a turn
        logger.debug(f"Metric-layer composition bump skipped (non-critical): {e}")
        return 0


def _seed_trace_events(tool_id: str, mode: str, prompt: str,
                       seed: str = "") -> "list[dict]":
    """The visible graph-routing trace step for a seeded turn.

    The seed is computed BEFORE these are emitted, so the step is already
    complete — the paired ``toolResult`` goes out immediately (the stream
    consumers turn it into the ``tool_result`` SSE). Without it the UI trace
    shows the Knowledge Graph step as forever in-flight. Mode A shows what the
    seed found for this question (systems, shared concepts, identifier
    translations — display names, no rule ids or raw payload); Mode B keeps
    the metadata-only sentence. Trace stream only, never the model's tokens.
    """
    text = (_ROUTE_SEED_STEP_TEXT if mode == "orchestrator"
            else _route_seed_step_text(seed))
    return [
        {"current_tool_use": {
            "toolUseId": tool_id,
            "name": "explore_graph",
            "input": {"question": _seed_question(mode, prompt)},
        }},
        {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": tool_id,
            "status": "success",
            "content": [{"text": text}],
        }}]}},
    ]


def _route_seed_step_text(seed: str) -> str:
    from .route_seed_trace import seed_step_text
    return seed_step_text(seed)


def _routing_question_kwargs(payload: dict, prompt: str) -> dict:
    """``{"question": <rawPrompt>}`` when the payload carries the user's own
    question and it differs from ``prompt``, else ``{}``.

    The app sends the enriched turn (``[Temporal context: ...]``, and with a
    plant ``[Plant context: ...]``, then the question) as ``prompt`` and the
    question the user typed as ``rawPrompt``. The route seed ranks rules by
    the question's words and labels the trace step with it, so it reads
    ``rawPrompt``: with the enriched text the time line's words (date, time,
    start, shift, ...) ranked rules on every turn and the trace label showed
    the time line (a rehearsal run, 2026-10-16). The model still gets ``prompt``.
    ``{}`` keeps a payload with no separate raw question byte-identical, and
    keeps the prelude's documented single-argument stubs working."""
    raw = payload.get("rawPrompt") if isinstance(payload, dict) else None
    if isinstance(raw, str) and raw.strip() and raw != prompt:
        return {"question": raw}
    return {}


def _graph_first_prelude(prompt: str, candidate: "dict | None" = None,
                         metric_results: "list | None" = None,
                         question: "str | None" = None,
                         ) -> "tuple[str, list[dict], dict | None]":
    """Build the deterministic mode-appropriate graph-routing prelude.

    Mode B injects the graph-first seed containing routed physical schemas. Mode A
    injects the concept-level route seed containing systems, shared concepts, and
    bounded translation-rule summaries but NO physical schemas. Both seeds merge into
    the initial user turn and emit one visible ``explore_graph`` event so routing is a
    deterministic precondition rather than an LLM-elective inventory call.

    ``question`` is the user's own question (the payload's ``rawPrompt``,
    :func:`_routing_question_kwargs`): the seed builder and the trace label
    read it, and ``prompt`` is still what the model gets. ``None`` means
    ``prompt`` is the question (direct and synthetic callers).

    The third element is the additive ``planned_with`` routing summary derived
    from the Mode-A route seed (``_planned_with_from_seed``); Mode B and any
    derivation failure yield ``None``. ``candidate`` (B2 try-it) forwards to
    ``build_route_seed(candidate=…)`` — Mode A only, Mode B ignores it;
    ``None`` = byte-identical behavior. Fail-safe: any import/read/build
    failure returns ``(prompt, [], None)`` unchanged. ``metric_results``
    (ADR-0078) are the sanitized metric-layer results composed after the
    gap notice; ``None`` = byte-identical. The question's resolved time
    windows (:func:`_resolved_time_windows`) ride the preamble; its chart
    intent (:func:`_remember_chart_intent`) rides each delegation's context.
    """
    mode = os.environ.get("SYSTEM_QUERY_MODE", "agent").strip().lower()
    question = prompt if question is None else question
    windows = _resolved_time_windows(prompt)
    extra = {"time_windows": windows} if windows else {}
    try:
        seed, tool_id = _build_prelude_seed(mode, candidate, question)
    except Exception:  # noqa: BLE001 - fail-safe: never break the query on a seed failure
        logger.warning("graph routing seed failed category=SETUP_UNAVAILABLE")
        return _compose_unseeded_turn(prompt, metric_results, **extra), [], None
    if not seed:
        return _compose_unseeded_turn(prompt, metric_results, **extra), [], None
    planned_with = None if mode == "orchestrator" else _planned_with_from_seed(seed)
    # PROACTIVE metric-term resolution rides inside _compose_seeded_turn (empty
    # on every ordinary turn ⇒ byte-identical composition).
    augmented = (_compose_seeded_turn(seed, prompt, metric_results, **extra)
                 if metric_results or windows else _compose_seeded_turn(seed, prompt))
    return augmented, _seed_trace_events(tool_id, mode, question, seed), planned_with


def _build_prelude_seed(mode: str, candidate: "dict | None", question: str) -> tuple:
    """``(seed, tool_id)`` for the prelude: the Mode B graph-first seed, or the
    Mode A concept-level route seed (``candidate`` forwards to the try-it
    builder; Mode B ignores it). Raises on any import/build failure, so the
    caller fails the turn open (seedless)."""
    if mode == "orchestrator":
        from .orchestrator import build_graph_first_seed, GRAPH_FIRST_SEED_TOOL_ID
        return build_graph_first_seed(), GRAPH_FIRST_SEED_TOOL_ID
    from .orchestrator import build_route_seed, ROUTE_SEED_TOOL_ID
    seed = (build_route_seed(candidate=candidate, question=question) if candidate is not None
            else build_route_seed(question=question))
    return seed, ROUTE_SEED_TOOL_ID


def _prelude_with_planned(prompt: str,
                          candidate: "dict | None" = None,
                          metric_results: "list | None" = None,
                          session_id: str = "",
                          question: "str | None" = None,
                          ) -> "tuple[str, list[dict], dict | None]":
    """Call ``_graph_first_prelude`` tolerating the historical 2-tuple shape.

    The prelude is a documented monkeypatch seam (several test modules stub it
    with ``lambda p: (p, [])``); a 2-tuple simply carries no ``planned_with``.
    ``candidate``, ``metric_results`` (ADR-0078) and ``question`` (the user's
    own question, :func:`_routing_question_kwargs`) are forwarded ONLY when
    present so single-arg stubs keep working on every ordinary turn; a
    composed metric-layer block bumps its delivery evidence here.
    """
    extra = {"metric_results": metric_results} if metric_results else {}
    if question is not None:
        extra["question"] = question
    if candidate is not None:
        result = _graph_first_prelude(prompt, candidate, **extra)
    else:
        result = _graph_first_prelude(prompt, **extra)
    if metric_results:
        _bump_metric_layer_composition(metric_results, result[0], session_id)
    planned_with = result[2] if len(result) > 2 else None
    return result[0], result[1], planned_with


#: RESERVED payload key for the B2 try-it candidate artifact (ADR-0052 (d)).
#: Same posture as ``_adt_identity`` (ADR-0049): popped UNCONDITIONALLY by
#: every runtime entrypoint before the prompt is composed, so it can never
#: leak into the model's token path — regardless of mode or validity.
TRYIT_CANDIDATE_KEY = "tryItCandidate"

#: RESERVED payload key for ADR-0078 metric-layer results (the ONE spelling
#: lives in ``metric_turn_render``). Same pop-unconditionally posture.
from .metric_turn_render import (  # noqa: E402
    FALLBACK_NOTICE as _METRIC_LAYER_FALLBACK_NOTICE, METRIC_LAYER_RESULTS_KEY,
    invalid_result as _invalid_metric_layer_result)


def _usage_cost_payload(metrics) -> "dict | None":
    """Pinned measured-cost payload from a strands ``EventLoopMetrics`` (E7).

    Reads ``metrics.accumulated_usage`` (strands 1.42.0 ``Usage`` dict:
    ``inputTokens``/``outputTokens``/``totalTokens`` ints — verified surface,
    ADR-0053) into the pinned ``intelligence_summary.cost`` shape, plus
    ``latency_ms`` from ``accumulated_metrics.latencyMs`` when present.
    Scope is stamped ``orchestrator-model-only``: system agents run as
    separate AgentCore runtimes behind the gateway — their model usage never
    reaches this AgentResult, and no estimate is derived for it. Returns
    ``None`` on ANY missing/malformed input AND on an all-zero usage
    (strands zero-initializes ``accumulated_usage``, so zeros mean "no model
    call measured"; the honest-data rule forbids zeros-pretending-to-be-
    measurements — absence = absent). Never estimates, never dollars.
    """
    try:
        usage = getattr(metrics, "accumulated_usage", None)
        if not isinstance(usage, dict):
            return None
        payload: dict = {}
        for src, dest in (("inputTokens", "input_tokens"),
                          ("outputTokens", "output_tokens"),
                          ("totalTokens", "total_tokens")):
            value = usage.get(src)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            payload[dest] = value
        if payload["total_tokens"] <= 0:
            return None  # zero-initialized Usage = unmeasured, never zeros
        perf = getattr(metrics, "accumulated_metrics", None)
        latency = perf.get("latencyMs") if isinstance(perf, dict) else None
        if isinstance(latency, int) and not isinstance(latency, bool) and latency > 0:
            payload["latency_ms"] = latency
        payload["scope"] = "orchestrator-model-only"
        return payload
    except Exception:  # noqa: BLE001 - absence = absent, never a broken turn
        return None


def _bump_turn_cost_hook(metrics, prompt: str, session_id: str = "") -> int:
    """Post-turn cost-ledger bump (E7, ADR-0053) — sibling of the usage hooks.

    Derives the pinned measured payload (``_usage_cost_payload``) from the
    turn's terminal AgentResult metrics and folds it into the day-bucketed
    cost ledger (``cost_ledger.bump_turn_cost``). SAME gates as the sibling
    ledger call sites: SKIPS automated briefings AND synthetic runs
    (``_is_synthetic_session`` — try-it replays and feedback-exploration
    re-runs never inflate the interactive cost trend).
    An unmeasured turn (payload ``None``) writes NOTHING — absence = absent,
    never zeros. SWALLOWS every failure (a ledger write never fails a turn).
    Returns 1 when a bump was written, else 0.
    """
    try:
        if _is_synthetic_session(session_id) or "briefing" in (prompt or "").lower():
            return 0
        payload = _usage_cost_payload(metrics)
        if not payload:
            return 0
        from . import cost_ledger
        return cost_ledger.bump_turn_cost(payload)
    except Exception as e:  # noqa: BLE001 - a ledger write never fails a turn
        logger.debug(f"Turn cost recording failed (non-critical): {e}")
        return 0


def _pop_tryit_candidate(payload) -> "dict | None":
    """Pop the RESERVED ``tryItCandidate`` key from the invocation payload.

    Unconditional (the ``_adt_identity`` pop discipline): the key is removed
    even when malformed, Mode B, or integrity-off — an arriving candidate is
    either consumed by the Mode-A route seed or discarded, never forwarded.
    Returns the candidate dict, or ``None`` for absent/non-dict values.
    """
    try:
        candidate = payload.pop(TRYIT_CANDIDATE_KEY, None)
    except Exception:  # noqa: BLE001 - never break the turn on a weird payload
        return None
    return candidate if isinstance(candidate, dict) else None


def _is_tryit_session(session_id) -> bool:
    """True for a B2 try-it run's synthetic session (``tryit-<jobid>-{a,b}``,
    minted by ``learning_routes``). Try-it turns are steward experiments, not
    production usage — they must NEVER write usage-ledger rows (rule-usage
    bumps, artifact ledger, route-intersection fallback), or the evidence
    surface would count synthetic replays as real answers."""
    return str(session_id or "").startswith("tryit-")


# Synthetic-run session prefixes whose turns must write NO learning artifacts:
# B2 try-it replays (``tryit-<jobid>-{a,b}``), feedback-exploration re-runs
# (``feedback-explore-<id>-r{1,2}``, minted by ``feedback_exploration``, whose
# ``SESSION_PREFIX`` is cross-pinned against this tuple), and server-side
# golden-eval runs (``eval-run-<runid>-q<i>``, minted by the Evals admin
# runner). The session id is the
# no-learning transport: it rides the SAME chat-client payload field discipline
# ``writeMemory`` already does, in BOTH client branches (AgentCore payload
# ``sessionId`` / local in-process ``session_id``), so no new payload key is
# needed. Measured defect (Demo 2026-08-05): exploration runs made the
# orchestrator file 13 side-effect concept=unknown value_mapping proposals —
# an exploration must file exactly its ONE corroborated insight
# (``feedback_exploration._file_proposal``) and nothing else.
_SYNTHETIC_SESSION_PREFIXES = ("tryit-", "feedback-explore-", "eval-run-")
# Every prefix above must ALSO be excluded from the user-facing session
# picker (memory_client.AUTOMATED_SESSION_PREFIXES — a separate policy:
# listing hygiene, not write suppression). Cross-pinned behaviorally in
# tests/unit/test_memory_session_list.py.


def _is_synthetic_session(session_id) -> bool:
    """True for ANY synthetic run's session (try-it replays, feedback
    explorations, server-side golden-eval runs) — every post-turn learning
    capture writer is suppressed for these (the project's class: synthetic
    flows never write)."""
    return str(session_id or "").startswith(_SYNTHETIC_SESSION_PREFIXES)


def _skip_post_turn_capture(s) -> bool:
    """True when this turn must write NO learning artifacts (X-3 containment).

    Two skip conditions, one predicate for EVERY post-turn capture writer
    (``_post_turn_learning_checks`` / ``_capture_correlation_insights`` /
    ``_capture_value_mapping_proposals``, plus the analysis persist in
    ``_waste_analysis_events``): synthetic sessions
    (``_is_synthetic_session`` — B2 try-it replays AND feedback-exploration
    re-runs) and automated briefings (the legacy gate). A try-it turn is a
    steward experiment and must be a PURE READ; a feedback exploration is a
    synthetic ANALYSIS run that files exactly its ONE corroborated insight
    itself (``feedback_exploration._file_proposal``) — no proposal minting,
    no ADR-0042 subject/staging counter churn, no guidance writes here; the
    usage ledger and memory are gated separately
    (``_record_rule_usage_hook``'s own gate, ``write_memory=False``)."""
    return (_is_synthetic_session(getattr(s, "session_id", ""))
            or "briefing" in (s.prompt or "").lower())


def _parse_vm_block(vm_text: str) -> "dict | None":
    """Parse one VALUE-MAPPING-PROPOSAL block into ``log_proposed_mapping``
    kwargs, or ``None`` when any endpoint slot is missing (malformed).

    ``concept`` is parsed from the block's own ``concept:`` line when present
    (same regex family as ``method:``/``reason:``), else stored as the honest
    ``"unknown"`` — never a hardcoded default (R2). ``confidence`` uses the
    table the ``save_value_mapping`` tool uses
    (``proposal_scoring.CONFIDENCE_WORD_FLOAT``), so low stays below medium.
    """
    import re

    from .proposal_scoring import confidence_word_to_float

    parsed: dict = {}
    for field in (
        "from_system", "from_field", "from_value",
        "to_system", "to_field", "to_value",
    ):
        match = re.search(rf"{field}:\s*(.+?)(?:\n|$)", vm_text)
        parsed[field] = match.group(1).strip() if match else ""
    if not all(parsed.values()):
        return None
    method = re.search(r"method:\s*(.+?)(?:\n|$)", vm_text)
    confidence = re.search(r"confidence:\s*(\w+)", vm_text)
    reason = re.search(r"reason:\s*(.+?)(?:\n|$)", vm_text)
    concept = re.search(r"concept:\s*(.+?)(?:\n|$)", vm_text)
    parsed.update(
        concept=concept.group(1).strip() if concept else "unknown",
        confidence=confidence_word_to_float(
            confidence.group(1) if confidence else "", high_if_contains=True),
        method=method.group(1).strip() if method else "",
        reason=reason.group(1).strip() if reason else "",
    )
    return parsed


#: Bounded refusal reasons a parsed block can carry (log keys, never data).
_VM_REFUSAL_REASONS = ("multi_field", "placeholder_value", "self_echo")


def _vm_block_refusal_reason(parsed: dict) -> "str | None":
    """Why a parsed block cannot be a mapping, or ``None`` to file it.

    Reuses the ingestion gates' own predicates (3f sentinel, 3b self-map, 3g
    echo) so a refusal here never disagrees with ``log_proposed_mapping``;
    the field-slot list check is new (``value_mapping`` is not shape-gated).
    """
    from . import proposal_scoring
    from .self_learning import _is_failed_lookup_sentinel

    if any(proposal_scoring.names_multiple_fields(parsed[f])
           for f in ("from_field", "to_field")):
        return "multi_field"
    if _is_failed_lookup_sentinel(parsed["to_value"]):
        return "placeholder_value"
    endpoints = tuple(parsed[f] for f in (
        "from_system", "from_field", "to_system", "to_field",
        "from_value", "to_value"))
    if (proposal_scoring.is_degenerate_join_proposal(*endpoints, "value_mapping")
            or proposal_scoring.is_equal_literal_echo(*endpoints)):
        return "self_echo"
    return None


def _vm_block_refusal(parsed: dict) -> "str | None":
    """Best-effort pre-check, the same boundary as the canonical gates: a
    raising predicate refuses nothing, so the block files exactly as before."""
    try:
        return _vm_block_refusal_reason(parsed)
    except Exception as e:  # noqa: BLE001 - best-effort gate, never fail propose
        logger.warning("value mapping block pre-check failed, filing as "
                       "before: %s", type(e).__name__)
        return None


def _log_vm_refusals(refused: dict) -> None:
    """One bounded info line of per-reason counts when a block was refused."""
    if any(refused.get(r) for r in _VM_REFUSAL_REASONS):
        logger.info(
            "Self-learning value mapping blocks refused: %s",
            " ".join(f"{r}={refused.get(r, 0)}" for r in _VM_REFUSAL_REASONS),
        )


def _query_context_for_storage(prompt: str, cap: int = 500) -> str:
    """The bounded, fence-free ``query_context`` a stored proposal may carry.

    A fenced untrusted block (``untrusted_text.render_fenced`` — customer
    comments, business context) must NEVER be storable in ``query_context``:
    on Demo, 13 exploration-run proposals stored the fence header + origin
    label, and a marginally shorter question would have stored real user
    comment text. Policy (conservative by construction): everything at or
    beyond the FIRST fence token is dropped — content past an untrusted
    boundary is either the untrusted text itself or the renderer's trailer,
    never storable — and the single line directly above the token is dropped
    too (the renderer places ``\\n\\n{heading}\\n`` right before the opening
    fence, and the heading carries the origin label). Fence-free prompts pass
    through cap-bounded and unchanged."""
    from .untrusted_text import FENCE_TOKEN_PATTERN
    text = str(prompt or "")
    match = FENCE_TOKEN_PATTERN.search(text)
    if match is None:
        return text[:cap]
    head = text[:match.start()].rstrip()
    newline_at = head.rfind("\n")
    head = head[:newline_at].rstrip() if newline_at != -1 else ""
    return head[:cap]


from .stored_value_mapping_memo import operation_scope as _stored_vm_scope  # noqa: E402


# One operation for the stored-ValueMapping reads the filings make
# (``self_learning._stored_value_mapping_rows``): each system pair once.
@_stored_vm_scope()
def _ingest_value_mapping_proposals(
    proposals: list[str], *, prompt: str, tool_calls_count: int,
    log_mapping=None, accepted_cap: int = 5, work_cap: int = 50,
) -> int:
    """Scan bounded response blocks until ``accepted_cap`` writes are stored.

    ``prompt`` must be the ORIGINAL user prompt (never the seed-augmented
    turn text) — it is stored bounded as ``query_context`` after
    ``_query_context_for_storage`` strips anything at/inside/after a fenced
    untrusted block (R2). Safe for the value_mapping dedup grain: its
    canonical keys digest the six endpoint slots only
    (``proposal_key._DISPATCH['value_mapping']``), never ``query_context``.
    """
    if log_mapping is None:
        from .self_learning import log_proposed_mapping as log_mapping

    accepted = 0
    scanned = 0
    refused: dict = {}
    not_stored = 0
    for vm_text in proposals[:work_cap]:
        scanned += 1
        parsed = _parse_vm_block(vm_text)
        reason = "malformed" if parsed is None else _vm_block_refusal(parsed)
        if reason:
            refused[reason] = refused.get(reason, 0) + 1
            continue
        mapping_id = log_mapping(
            **parsed,
            query_context=_query_context_for_storage(prompt),
            tool_calls_count=tool_calls_count,
            proposed_type="value_mapping",
            provenance="orchestrator",
        )
        if mapping_id is None:
            not_stored += 1
            continue
        accepted += 1
        if accepted >= accepted_cap:
            break

    unscanned = max(0, len(proposals) - scanned)
    if refused.get("malformed") or not_stored or unscanned:
        logger.debug(
            "Self-learning value mapping blocks: scanned=%d accepted=%d "
            "malformed=%d skipped=%d unscanned=%d work_cap=%d", scanned,
            accepted, refused.get("malformed", 0), not_stored, unscanned, work_cap)
    _log_vm_refusals(refused)
    return accepted


def _integrity_prompt(payload: dict) -> str:
    raw = payload.get("rawPrompt")
    return raw if isinstance(raw, str) else str(payload.get("prompt") or "")


def _registered_agent_tools(agent) -> set[str]:
    registry = getattr(getattr(agent, "tool_registry", None), "registry", None)
    return {str(name) for name in registry} if isinstance(registry, dict) else set()


def _registered_tool_system_ids(tool_names: set[str]) -> dict[str, str]:
    """Bind actual inventory names to unique canonical registered system IDs.

    Known systems are sourced PROCESS-AGNOSTICALLY via
    ``self_learning.known_systems()`` — the hydrated catalog in the ECS app,
    a TTL-cached Neptune ``System`` read in the AgentCore runtime process
    (where the catalog singleton is never hydrated). Reading the catalog
    singleton directly here left the mapping EMPTY in the runtime process, so
    every gateway-prefixed system tool result was rejected
    ``SYSTEM_TARGET_MISMATCH`` before parsing and every live Mode A prompt
    failed closed (smoke run e00048e3, 2026-07-17). An empty/failed graph read
    still yields an empty mapping — unmapped qualified tools stay rejected
    (fail-closed), never trusted.
    """
    from .self_learning import known_systems

    by_target: dict[str, list[str]] = {}
    for entry in known_systems():
        system_id = str(entry.get("id") or "")
        if system_id:
            by_target.setdefault(system_id.replace("_", "-")[:100], []).append(system_id)
    mapping: dict[str, str] = {}
    for tool_name in tool_names:
        target = _system_id_from_tool_name(tool_name)
        candidates = by_target.get(target, [])
        if len(candidates) == 1:
            mapping[tool_name] = candidates[0]
        elif target == "digital-thread":
            mapping[tool_name] = target
    return mapping


def _integrity_rule_usage(context) -> "tuple[set, list]":
    """Single rules-applied extraction + GSI fetch for the integrity path.

    The IntegrityContext never retains raw tool-result payloads, but it DOES
    retain bounded scannable strings: trusted-relationship proof tuples (whose
    first element is ``RelationshipProof.graph_record_id`` — the very node id
    a learned ValueMapping carries), bounded string ``ClaimInput.value``
    scalars, and claim/evidence ``semantic_field``/``locator`` fields. Scan
    those with ``usage_tracking.extract_rule_node_ids`` and resolve accepted
    rows with ONE GSI query. Returns ``(ids, accepted_rows)``. Best-effort:
    ANY failure ⇒ ``(set(), [])`` + debug log — this must never break
    ``final_events``.
    """
    try:
        texts: list[str] = []
        for rel in getattr(context, "trusted_relationships", ()) or ():
            texts.extend(part for part in rel if isinstance(part, str))
        for answer in getattr(context, "system_answers", ()) or ():
            for claim in answer.claims:
                for inp in claim.inputs:
                    if isinstance(inp.value, str):
                        texts.append(inp.value)
                    texts.extend((inp.semantic_field, inp.locator))
            for evidence in answer.evidence:
                texts.extend((evidence.semantic_field, evidence.locator))
        from .usage_tracking import extract_rule_node_ids
        ids = extract_rule_node_ids(texts)
        if not ids:
            return set(), []
        from .self_learning import get_accepted_rules_by_node_ids
        return ids, get_accepted_rules_by_node_ids(ids)
    except Exception as exc:  # noqa: BLE001 - best-effort summary enrichment only
        logger.debug("integrity rules_applied extraction skipped: %s",
                     type(exc).__name__)
        return set(), []


def _format_rules_applied(ids: set, rows: list) -> list[dict]:
    """Format extraction output exactly like ``_build_summary_payload``:
    matched rows render human labels; unmatched ids fall back label == id,
    sorted."""
    matched_ids = {r.get("neptune_node_id") for r in rows}
    return [
        {"id": r.get("neptune_node_id", ""),
         "label": query_intelligence.format_rule_label(r)}
        for r in rows
    ] + [{"id": rid, "label": rid} for rid in sorted(ids - matched_ids)]


def _integrity_rules_applied(context) -> list[dict]:
    """Mirror the legacy ``rules_applied`` extraction onto the integrity path.

    Composition of ``_integrity_rule_usage`` (extract + ONE GSI fetch) and
    ``_format_rules_applied``. ``final_events`` calls the two pieces
    separately so the post-turn usage-ledger write
    (``_integrity_post_turn_learning``, R1) reuses the SAME ids/rows as
    ``precomputed_ids``/``prefetched_rows`` — the turn is never re-scanned
    or re-queried.
    """
    ids, rows = _integrity_rule_usage(context)
    return _format_rules_applied(ids, rows)


def _integrity_evidence_by_tool(state) -> dict:
    """toolUseId → bounded evidence dict from the IntegrityContext's typed
    observations (``SourceEvidence`` rows are public structural metadata only
    — system/tool/semantic fields/window/queried_at, never values).
    Evidence binds by tool NAME (``SourceEvidence`` carries no toolUseId), so
    every call of that tool shares the binding — sufficient for D4's
    ``verified`` gate, which additionally requires an accepted verdict."""
    by_name: dict[str, dict] = {}
    for answer in getattr(state.context, "system_answers", None) or []:
        for ev in getattr(answer, "evidence", None) or []:
            name = getattr(ev, "tool_name", "") or ""
            if not name:
                continue
            entry = by_name.setdefault(name, {
                "system_id": getattr(ev, "system_id", "") or "",
                "columns": [],
                "queried_at": getattr(ev, "queried_at", None),
                "source_observed_start": getattr(ev, "source_observed_start", None),
                "source_observed_end": getattr(ev, "source_observed_end", None),
            })
            field = getattr(ev, "semantic_field", "") or ""
            if field and field not in entry["columns"]:
                entry["columns"].append(field)
    return {tool_id: by_name[name]
            for tool_id, name in state.tool_names.items() if name in by_name}


def _invocation_tool_calls(state) -> list[dict]:
    """Builder-shaped tool-call trace from invocation state —
    ``tool_names`` is insertion-ordered, so citation ordinals match the
    stream order (shared by both integrity emitters)."""
    return [{"name": name, "toolUseId": tool_id}
            for tool_id, name in state.tool_names.items()]


def _enriched_provenance_payload(payload: dict, session_id: str,
                                 verdict: "str | None" = None) -> dict:
    """Assembly-time sidecar merge (the designed Wave-5 follow-up), shared
    by BOTH runtime emitters: consult the shared provenance store for each
    record's ``query_ref`` and fill ``entity``/``result_shape.rows`` plus
    the D4 ``unverified`` → ``partial`` upgrade — through the ONE merge
    helper (``agents.provenance.enrich_records_from_sidecar``; reads ≤32,
    SQL never merged, D1 cap re-enforced). The reader also counts the
    turn's capability misses and logs them once, digests only
    (``provenance_store.FrameReader``). ``verdict`` is the turn's integrity
    verdict: a rejected turn's records stay exactly as built (D4). FAIL-SOFT
    end to end: any
    failure (import, store, merge) returns the payload exactly as built —
    enrichment may never break, empty, or delay the frame."""
    try:
        from . import provenance_store
        from .provenance import enrich_records_from_sidecar
        reader = provenance_store.FrameReader()
        enriched = enrich_records_from_sidecar(
            payload, session_id=session_id, get_entry=reader, verdict=verdict)
        reader.log_misses(session_id)
        return enriched
    except Exception as exc:  # noqa: BLE001 - additive enrichment; base frame survives
        logger.debug("Provenance sidecar enrichment failed: %s", type(exc).__name__)
        return payload


def _integrity_provenance_events(state, integrity) -> list[dict]:
    """The enforce-path ``{"provenance": ...}`` frame (T1.2 — ADDITIVE D1
    metadata key; absent key ⇒ legacy client unaffected).

    Built by the ONE builder (``agents/provenance.py``) from invocation
    state: tool order from ``tool_names`` (insertion-ordered), the per-call
    bounded ``safe_tool_result_metadata`` retained by ``_one_tool_result``,
    the rules_applied rows ``final_events`` already extracted (never
    re-queried), typed evidence bindings from the context's observations,
    and the integrity verdict (D4: ``verified`` requires an accepted verdict
    AND bound evidence). Swallow-guarded: any failure ⇒ frame ABSENT (never
    an empty-empty frame), stream unaffected."""
    try:
        from datetime import datetime, timezone
        from .provenance import build_provenance_records, format_captured_at
        from .provenance import uncited_query_refs
        verdict = integrity.quality_metadata().get("verdict")
        payload = build_provenance_records(
            session_id=state.session_id,
            tool_calls=_invocation_tool_calls(state),
            tool_inputs={},  # claim-check: the integrity path retains no raw inputs
            tool_results_metadata=state.tool_metadata,
            evidence_by_tool=_integrity_evidence_by_tool(state),
            rules_applied=_format_rules_applied(state.rule_ids, state.matched_rows),
            verdict=verdict,
            captured_at=format_captured_at(datetime.now(timezone.utc)),
            graph_query_refs=uncited_query_refs(
                session_id=state.session_id,
                tool_calls=_invocation_tool_calls(state),
                tool_inputs=state.tool_query_inputs),
        )
        payload = _enriched_provenance_payload(payload, state.session_id, verdict)
        if (not payload["records"] and not payload["truncated"]
                and not payload.get("graph_queries")):
            return []
        return [{"provenance": payload}]
    except Exception as exc:  # noqa: BLE001 - additive frame must never break the stream
        logger.debug("Provenance frame generation failed: %s", type(exc).__name__)
        return []


def _integrity_query_sidecar(state) -> list[dict]:
    """The enforce-path ``{"provenance_queries": ...}`` sidecar frame (T3.2).

    SERVER-ONLY (D6): emitted after the ``provenance`` frame, persisted by
    the app server (T3.3) and NEVER broadcast on the SSE stream — chat_client
    strips the key before any event reaches a client. Entries are built by
    the ONE sidecar builder (``agents.provenance.build_sidecar_entries`` —
    strict JSON parse, no literal-eval tolerance: the runtime captures JSON)
    from the query-bearing inputs ``_tool_use`` retained
    (``tool_query_inputs`` — a capture surface separate from the provenance
    builder, which still receives ``tool_inputs={}``: the D1 frame stays
    input-free). Same swallow guard as the provenance frame: any failure ⇒
    frame ABSENT."""
    try:
        from datetime import datetime, timezone
        from .provenance import build_sidecar_entries, format_captured_at
        entries = build_sidecar_entries(
            session_id=state.session_id, tool_calls=_invocation_tool_calls(state),
            tool_inputs=state.tool_query_inputs, rule_ids=state.rule_ids,
            captured_at=format_captured_at(datetime.now(timezone.utc)))
        if not entries:
            return []
        return [{"provenance_queries": entries}]
    except Exception as exc:  # noqa: BLE001 - sidecar must never break the stream
        logger.debug("Provenance query sidecar generation failed: %s", type(exc).__name__)
        return []


class _RuntimeIntegrityInvocation:
    def __init__(
        self, prompt: str, registered_tools: set[str] | None = None,
        tool_system_ids: dict[str, str] | None = None,
    ):
        from .response_integrity import IntegrityContext
        self.context = IntegrityContext(prompt, tool_system_ids=tool_system_ids)
        # The ORIGINAL user question (the payload's ``rawPrompt`` — the ctor
        # arg is ``_integrity_prompt(payload)``): what stored proposal
        # query_context derives from in ``_integrity_post_turn_learning`` (R2).
        self.prompt = prompt
        self.registered_tools = registered_tools
        self.agent_result = None
        self.tool_names: dict[str, str] = {}
        self.tool_calls = 0
        self.wasted_calls = 0  # tool results whose SAFE metadata reported an error
        self.error_seen = False
        # Additive Mode-A route-seed routing summary (set by
        # _invoke_integrity_runtime from the prelude); None ⇒ key absent.
        self.planned_with: dict | None = None
        # Bounded Mode-A route-seed artifact index (jp/ir ids + endpoint
        # systems only, never full rules — set by _invoke_integrity_runtime
        # at prelude time) feeding the ADR-0052 route-intersection fallback.
        self.seed_rule_index: list = []
        # Post-turn learning inputs (R1): streamed assistant text (proposal-
        # block parsing) + final_events' single extraction, reused by
        # _integrity_post_turn_learning so the turn is never re-scanned.
        self.response_text: list[str] = []
        self.rule_ids: set = set()
        self.matched_rows: list = []
        # Provenance (T1.2): session id threaded by _invoke_integrity_runtime
        # (deterministic query_ref minting, D3) + the bounded per-call
        # safe_tool_result_metadata retained by _one_tool_result (the D1
        # result_shape source — metadata only, never payload content).
        self.session_id: str = ""
        self.tool_metadata: dict[str, dict] = {}
        # T3.2 sidecar capture (D6): query-bearing tool inputs, retained
        # ONLY for the server-only provenance_queries frame (stripped
        # before broadcast, T3.3). The provenance builder itself still
        # receives tool_inputs={} — the D1 frame stays input-free.
        self.tool_query_inputs: dict[str, str] = {}

    def _progress(self, output: dict | None) -> list[dict]:
        if output is None:
            return []
        from .response_integrity import MAX_PROGRESS_EVENTS
        self.context.progress_events += 1
        if self.context.progress_events > MAX_PROGRESS_EVENTS:
            self.context.mark_budget_exceeded()
            return []
        return [output]

    def _tool_use(self, event: dict) -> list[dict]:
        current = event.get("current_tool_use")
        if not isinstance(current, dict) or not current.get("name"):
            return []
        name = str(current.get("name") or "")
        if self.registered_tools is not None and name not in self.registered_tools:
            return []
        tool_id = str(current.get("toolUseId") or "")
        if tool_id and current.get("input"):
            # T3.2 (D6): retain the input string for the sidecar — deltas
            # accumulate, so overwriting means the LAST (complete) string
            # wins even though the duplicate branch below returns early.
            # 2 KB cap mirrors the legacy s.tool_inputs capture.
            self.tool_query_inputs[tool_id] = str(current["input"])[:2000]
        if tool_id:
            if tool_id in self.tool_names:
                # Streamed input-delta duplicate of an already-announced call.
                # The public payload is identical (input is redacted by
                # safe_tool_progress), so re-emitting only burned the bounded
                # progress budget — live, ~250 fragments over 7 calls tripped
                # MAX_PROGRESS_EVENTS → TRAVERSAL_BUDGET_EXCEEDED, which
                # clears ALL claims and failed the turn closed. Drop it; the
                # progress-event cap stays enforced as the backstop.
                return []
            self.tool_names[tool_id] = name
            self.tool_calls += 1
        from .runtime_integrity import safe_tool_progress
        return self._progress(safe_tool_progress(event, self.registered_tools))

    def forget_tool_calls(self, tool_ids) -> None:
        """Drop tool calls a cut reply started but never sent
        (``output_cap_recovery`` ``on_cut``): they leave the summary counts,
        the provenance/sidecar inputs and post-turn learning. Their progress
        events were already public and stay counted toward the progress cap."""
        for tool_id in tool_ids:
            if self.tool_names.pop(tool_id, None) is not None:
                self.tool_calls -= 1
            self.tool_query_inputs.pop(tool_id, None)
            self.tool_metadata.pop(tool_id, None)

    def _tool_results(self, event: dict) -> list[dict]:
        message = event.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            return []
        output: list[dict] = []
        for block in content:
            result = block.get("toolResult") if isinstance(block, dict) else None
            if not isinstance(result, dict):
                continue
            output.extend(self._one_tool_result(result))
        return output

    def _one_tool_result(self, result: dict) -> list[dict]:
        tool_id = str(result.get("toolUseId") or "")
        name = self.tool_names.get(tool_id, "")
        if not name or (self.registered_tools is not None and name not in self.registered_tools):
            return []
        from .response_integrity import capture_orchestrator_tool_result
        capture_orchestrator_tool_result(name, result, self.context)
        from .tool_result_capture import safe_tool_result_metadata
        from .runtime_integrity import public_tool_result_event
        metadata = safe_tool_result_metadata(result, tool_name=name, max_chars=1024)
        self.tool_metadata[tool_id] = metadata  # provenance result_shape source (T1.2)
        if metadata.get("app_status") == "error":
            self.wasted_calls += 1
        return self._progress(public_tool_result_event(
            tool_use_id=tool_id, tool_name=name, metadata=metadata,
        ))

    def consume(self, event) -> list[dict]:
        if not isinstance(event, dict):
            return []
        if event.get("error") not in (None, "", False, [], {}):
            # Log a payload-SAFE fingerprint before scrubbing. The client only
            # ever sees "Query failed.", and this branch used to discard the
            # original entirely — a 2026-07-28 investigation into two failing
            # golden questions had no way to tell WHICH error fired. Type plus a
            # short redacted head only: an error string can quote row data, and
            # the claim-check policy keeps telemetry out of logs as well as out
            # of the token path.
            self.error_seen = True
            logger.error("STREAM_ERROR_EVENT type=%s head=%s",
                         type(event.get("error")).__name__,
                         _redacted_error_head(event.get("error")))
            return [{"error": "Query failed."}]
        if "result" in event:
            self.agent_result = event["result"]
            return []
        if "message" in event:
            return self._tool_results(event)
        if "current_tool_use" in event or "data" in event:
            if isinstance(event.get("data"), str):
                # Mirror the legacy path: accumulate assistant text locally
                # for post-turn proposal parsing (R1) — never yielded here.
                self.response_text.append(event["data"])
            return self._tool_use(event)
        return []

    def final_events(self) -> list[dict]:
        from .response_integrity import gate_final_result
        markdown, integrity = gate_final_result(self.agent_result, self.context)
        from .runtime_integrity import integrity_metadata, persist_typed_proposals
        persisted = 0
        try:
            persisted = persist_typed_proposals(integrity, tool_calls_count=self.tool_calls)
        except Exception as exc:
            logger.warning("Typed proposal persistence failed type=%s", type(exc).__name__)
        # Public intelligence summary (counts of already-public tool names only;
        # no payload content). The integrity path MUST emit one, or the smoke
        # grounding gate reads system_queries == 0 even for fully grounded
        # answers. rules_applied is extracted from the IntegrityContext's
        # RETAINED bounded strings (relationship-
        # proof graph_record_ids plus bounded claim/evidence scalars — still
        # never raw tool payloads); the ids/rows are stored on self so the
        # post-turn learning seam (R1) reuses them without re-scanning;
        # planned_with carries the Mode-A route-seed routing summary when the
        # prelude captured one.
        from . import query_intelligence
        self.rule_ids, self.matched_rows = _integrity_rule_usage(self.context)
        summary = query_intelligence.build_intelligence_summary(
            tool_names=list(self.tool_names.values()),
            wasted_calls=self.wasted_calls,
            rules_applied=_format_rules_applied(self.rule_ids, self.matched_rows),
            new_proposed_mappings=persisted,
            planned_with=self.planned_with,
            # E7: measured orchestrator-model usage from the terminal
            # AgentResult (absent when unmeasured — never zeros).
            cost=_usage_cost_payload(getattr(self.agent_result, "metrics", None)),
            # Which learned artifacts actually reached this turn's prompt —
            # the evidence rules_applied cannot give for prompt-text context.
            composed_context=getattr(self, "composed_context", None),
        )
        _attach_earlier_sources(summary, self)
        return [
            {"data": markdown},
            {"intelligence_summary": summary},
            # Additive per-claim provenance (T1.2) — after intelligence_summary,
            # before the integrity marker; [] on failure (frame absent).
            *_integrity_provenance_events(self, integrity),
            # Server-only Tier-3 sidecar (T3.2, D6) — after the provenance
            # frame; stripped by chat_client before broadcast (T3.3).
            *_integrity_query_sidecar(self),
            integrity_metadata(integrity),
        ]


def _drain_pending_chart_text(agent) -> str:
    """Drain ChartRedactionHook's queued chart tags into an injectable string.

    The hook (agents/orchestrator.py) redacts chart payloads from the model's
    context and queues the original tags on ``agent._pending_chart_blocks``;
    the stream wrapper below yields the returned text as a plain
    ``{"data": …}`` event so the client renders charts at network speed,
    exactly once per rendered chart (title + content-hash dedup, scoped to
    this request's Agent — see ``orchestrator.drain_pending_charts``).

    ``RESPONSE_INTEGRITY_MODE=enforce`` never injects: the queue is still
    drained (the hook keeps the model context clean in every mode) but the
    tags are dropped — the enforce path's public answer is the
    deterministically gated final text only. Fail-soft: any error returns ''
    (worst case: no chart, the stream completes).
    """
    try:
        from .orchestrator import drain_pending_charts
        fresh = drain_pending_charts(agent)
        if not fresh:
            return ""
        from .config import response_integrity_enforced
        if response_integrity_enforced():
            logger.info(
                "CHART_INJECTION: %d tag(s) suppressed (RESPONSE_INTEGRITY_MODE=enforce)",
                len(fresh),
            )
            return ""
        logger.info("CHART_INJECTION: yielding %d chart tag(s) to stream", len(fresh))
        return "\n\n" + "\n\n".join(fresh) + "\n\n"
    except Exception:
        logger.exception("CHART_INJECTION failed — no chart injected")
        return ""


def _wrap_stream_with_chart_injection(agent, stream):
    """Wrap an Agent's ``stream_async`` so ChartRedactionHook's queued tags
    are yielded as plain ``{"data": …}`` events right after the event that
    queued them.

    Installed OUTSIDE the (gate-frozen) legacy entrypoint: the injected data
    events ride the loop's ordinary data branch, so forwarding, byte
    accounting, and stream capture all apply unchanged. Never yields past a
    terminal ``result`` event — the synchronous ``Agent.__call__`` path reads
    the LAST event's ``result`` key. Tags queued AT the terminal event (the
    final tool result's MessageAddedEvent firing right before the stream
    ends) are drained and yielded BEFORE the result event — the
    stub already told the model those charts rendered, so dropping them was
    a silent lie.
    """
    async def wrapped(prompt, **kwargs):
        async for event in stream(prompt, **kwargs):
            if isinstance(event, dict) and "result" in event:
                chart_text = _drain_pending_chart_text(agent)
                if chart_text:
                    yield {"data": chart_text}
                yield event
                continue
            yield event
            chart_text = _drain_pending_chart_text(agent)
            if chart_text:
                yield {"data": chart_text}
    return wrapped


def _chart_injecting_build(build_orchestrator):
    """Wrap ``build_orchestrator`` so every Agent it returns streams with
    deterministic chart injection (runtime process only — installed by
    ``_run_orchestrator_integrity`` alongside the entrypoint interception,
    the same outside-the-legacy-loop pattern). ADR-0079: the invocation's
    popped delegated subject (``_delegated_subject_var``) is passed as
    ``delegated_subject=`` ONLY when set — every other build call is
    unchanged, so the gate-frozen legacy invoke body stays byte-identical."""
    def build(**kwargs):
        delegated_subject = _delegated_subject_var.get()
        if delegated_subject and "delegated_subject" not in kwargs:
            kwargs["delegated_subject"] = delegated_subject
        agent = build_orchestrator(**kwargs)
        # Each tool result carries the route context its delegation was sent
        # (route_facts), for the chat trace; the agent's own messages keep none.
        from .route_facts import wrap_stream as forward_route_contexts
        agent.stream_async = forward_route_contexts(agent.stream_async)
        agent.stream_async = _wrap_stream_with_chart_injection(agent, agent.stream_async)
        # The engine's provenance sentence (an exact-split figure, a storage
        # bound that ran) rides the same seam, outermost, so injected chart
        # text is ordinary answer text.
        from .answer_provenance_stream import wrap_stream
        agent.stream_async = wrap_stream(agent.stream_async)
        return agent
    return build


async def _integrity_model_events(agent, prompt, state):
    """The enforce turn's model events, consumed by ``state``, through the ONE
    output-cap recovery (the same seam as the legacy ``_filtered_stream``). A
    second cut marks the turn errored and streams the fixed output-limit frame
    instead of the generic "Query failed." (final events are then skipped)."""
    from .output_cap_recovery import (OUTPUT_LIMIT_ERROR_CODE, OutputLimitExceeded,
                                      stream_with_output_cap_recovery)
    from .response_integrity import FinalAnswerDraft
    state.earlier_sources = _earlier_sources(agent)
    cut_again = False
    try:
        async for event in stream_with_output_cap_recovery(
                agent, prompt, session_id=state.session_id,
                on_cut=state.forget_tool_calls,
                structured_output_model=FinalAnswerDraft):
            for output in state.consume(event):
                yield output
    except OutputLimitExceeded:
        cut_again = True
    if cut_again:
        state.error_seen = True
        yield {"error": OUTPUT_LIMIT_ERROR_CODE}


async def _invoke_integrity_runtime(payload, context, build_orchestrator):
    try:
        # RESERVED keys — popped unconditionally (ADR-0049; B2 try-it; ADR-0078).
        tryit_candidate = _pop_tryit_candidate(payload)
        metric_results = _pop_metric_layer_results(payload)
        session_id = payload.get("sessionId") or getattr(context, "session_id", None) or "default"
        user_id = payload.get("userId", "anonymous")
        raw_prompt = payload.get("prompt", "Hello")
        routing = _routing_question_kwargs(payload, raw_prompt)
        prompt, graph_events, planned_with = (_prelude_with_planned(
            raw_prompt, tryit_candidate, metric_results=metric_results, session_id=session_id,
            **routing)
            if metric_results else _prelude_with_planned(raw_prompt, tryit_candidate, **routing))
        agent = build_orchestrator(
            session_id=session_id, user_id=user_id,
            write_memory=bool(payload.get("writeMemory", False)),
            user_access_token=payload.get("userAccessToken"),
        )
        registered_tools = _registered_agent_tools(agent)
        state = _RuntimeIntegrityInvocation(
            _integrity_prompt(payload), registered_tools,
            _registered_tool_system_ids(registered_tools),
        )
        state.planned_with = planned_with
        state.session_id = session_id  # provenance query_ref minting (D3)
        state.seed_rule_index = _seed_rule_index_from_text(prompt)
        state.composed_context = _composed_context_from_text(prompt)
        for event in graph_events:
            # The graph-routing seed counts in the SAME tool accounting as model
            # calls (intelligence_summary: graph consulted first; cf. _tool_calls_log).
            ctu = event.get("current_tool_use") or {}
            if ctu.get("toolUseId"):
                state.tool_names[ctu["toolUseId"]] = ctu.get("name", "")
                state.tool_calls += 1
            yield event
        async for output in _integrity_model_events(agent, prompt, state):
            yield output
        if not state.error_seen:
            for output in state.final_events():
                yield output
            # R1: post-turn learning hangs off the invocation's completion —
            # after final_events yields, never inside it (50-line gate) and
            # never able to degrade the already-emitted stream.
            try:
                _integrity_post_turn_learning(state, raw_prompt, session_id)
            except Exception as e:  # noqa: BLE001 - learning never breaks the stream
                logger.debug(f"Integrity post-turn learning failed (non-critical): {e}")
    except Exception as exc:
        _log_invocation_failure(exc, str((payload if isinstance(payload, dict) else {}).get("sessionId") or "default"))
        yield {"error": "Query failed."}


def _log_invocation_failure(exc: BaseException, session_id: str) -> None:
    """Log a streaming failure with a traceback that SURVIVES the OTel handler.

    ``logger.exception``'s ``exc_info`` is dropped by this runtime's log handler
    — the record arrives with the message only, so no exception in this runtime
    was traceable (found 2026-07-30 chasing a reproducible Sonnet 5 failure: the
    code location was visible, the cause never was). Formatting the traceback
    INTO the message is what makes it survive; digits are masked because frames
    can carry data values.
    """
    import traceback as _tb
    logger.error("Error during agent invocation session=%s type=%s trace=%s",
                 session_id, type(exc).__name__,
                 _redacted_error_head(_tb.format_exc(), limit=1800))


def _redacted_error_head(err: object, limit: int = 120) -> str:
    """First *limit* chars of an error, credentials redacted, digit runs masked.

    Digits are what carry telemetry and business values, so masking them keeps
    the fingerprint useful for diagnosis (which error, which code path) without
    logging measurements. Credentials (a DSN password, a bearer token) are
    redacted over the whole text first, by the same helper as every other log
    head (``failure_fingerprint.redact_secrets``).
    """
    import re

    from .failure_fingerprint import redact_secrets
    return re.sub(r"\d", "#", redact_secrets(str(err), limit))[:limit]


def _seed_derived_turn_fields(prompt: str, planned_with) -> dict:
    """The additive turn-state fields derived from the Mode-A route seed.

    ``planned_with`` (routing summary, captured by the prelude) and
    ``composed_context`` (WHICH learned artifacts reached this turn's prompt).
    Either being ``None`` leaves its summary key absent.

    Set for the LEGACY state too, not only the integrity path: response
    integrity is OPT-IN (``RESPONSE_INTEGRITY_MODE`` defaults to report-only),
    so this is what a default deployment actually runs — wiring only the
    integrity invocation left the artifact telemetry blind everywhere.
    """
    return {
        "planned_with": planned_with,
        "composed_context": _composed_context_from_text(prompt),
    }


def _reserved_key_prelude(payload, raw_prompt: str, session_id: str):
    """Pop the RESERVED ``tryItCandidate`` / ``metricLayerResults`` keys
    (ADR-0049 posture; ADR-0078) and run the graph-first prelude with them.
    Metric results are forwarded ONLY when present, so 2-arg prelude stubs
    keep working on every ordinary turn."""
    candidate = _pop_tryit_candidate(payload)
    metric_results = _pop_metric_layer_results(payload)
    routing = _routing_question_kwargs(payload, raw_prompt)
    if metric_results:
        return _prelude_with_planned(raw_prompt, candidate,
                                     metric_results=metric_results,
                                     session_id=session_id, **routing)
    return _prelude_with_planned(raw_prompt, candidate, **routing)


def _new_turn_state(payload, context):
    """Per-invocation state for the legacy orchestrator entrypoint.

    Session identity (ADR-0005): AgentCore Memory binds to (actor_id,
    session_id); the session id is the CALLER-PROVIDED Conversation Session —
    deliberately NOT context.session_id, the EPHEMERAL Runtime Session minted
    fresh per invocation (AgentCoreChatClient mints a new runtimeSessionId
    per call to dodge stale/busy-session IncompleteRead failures); falls back
    to it, then "default", only when the caller omits it. Memory write gate
    (ADR-0007 / D1): only interactive chat sets writeMemory=True —
    build_orchestrator attaches the persisting session manager ONLY then, so
    synthetic flows never write. Forwarded user identity (ADR-0033): the
    payload's userAccessToken is handed to build_orchestrator; never logged
    (R4). Also logs the INVOKED line and runs the fail-safe graph-first prelude
    (``_reserved_key_prelude``).
    """
    from types import SimpleNamespace
    session_id = payload.get("sessionId") or getattr(context, "session_id", None) or "default"
    raw_prompt = payload.get("prompt", "Hello")
    logger.info("=== ORCHESTRATOR INVOKED [%s] === session=%s prompt=%s", _IMAGE_VERSION, session_id, raw_prompt[:80])
    # tryItCandidate / metricLayerResults are RESERVED — popped unconditionally.
    prompt, graph_first_events, planned_with = _reserved_key_prelude(
        payload, raw_prompt, session_id)
    return SimpleNamespace(
        session_id=session_id,
        user_id=payload.get("userId", "anonymous"),
        write_memory=bool(payload.get("writeMemory", False)),
        user_access_token=payload.get("userAccessToken"),
        prompt=prompt,
        # ORIGINAL user question (R2): the caller's rawPrompt, else the
        # pre-prelude prompt — never enriched/seed-augmented boilerplate.
        raw_prompt=_integrity_prompt(payload),
        graph_first_events=graph_first_events,
        # Additive seed-derived fields (routing summary + composed artifacts).
        **_seed_derived_turn_fields(prompt, planned_with),
        event_count=0, skipped_count=0, total_yielded_bytes=0, seen_tool_use_ids=set(),
        # Track bytes by event category to identify what's eating the 100MB
        bytes_by_category={"message": 0, "event": 0, "current_tool_use": 0,
                           "callback": 0, "other": 0},
        # Self-learning: tool-call trace for discovery detection
        tool_calls_log=[],   # list of {"name": str, "toolUseId": str}
        tool_inputs={},      # toolUseId → accumulated input (discovery parsing)
        tool_results={},     # toolUseId → result snippet (discovery parsing)
        response_text=[],    # assistant text for CORRELATION-PROPOSAL parsing
        # Single extraction (usage loop): computed ONCE in the summary block;
        # defensive inits so the finally-block hooks never NameError when the
        # stream/summary raises early. agent_metrics = terminal AgentResult
        # metrics (E7 cost — set by _filter_stream_event's result branch).
        rule_ids=set(), matched_rows=[], agent_metrics=None,
    )


def _register_graph_first_call(event, s):
    """Register a deterministic graph-first prelude event in the tool-call
    trace BEFORE the caller yields it: it lands in tool_calls_log so the
    intelligence_summary counts it as a neptune_query (graph consulted
    first), and chat_client turns the current_tool_use event into a
    tool_start SSE so the UI trace shows graph routing before any system
    delegation or SQL."""
    ctu = event.get("current_tool_use", {})
    tool_id = ctu.get("toolUseId", "")
    if tool_id:
        s.seen_tool_use_ids.add(tool_id)
        s.tool_calls_log.append({"name": ctu.get("name", ""), "toolUseId": tool_id})


def _filter_data_event(event, s):
    """Text delta — the actual streamed content (accumulated locally for
    CORRELATION-PROPOSAL parsing); a rider current_tool_use is forwarded
    name+id only."""
    filtered = {"data": event["data"]}
    if isinstance(event["data"], str):
        s.response_text.append(event["data"])
    ctu = event.get("current_tool_use")
    if ctu and ctu.get("name"):
        filtered["current_tool_use"] = {
            "toolUseId": ctu.get("toolUseId", ""),
            "name": ctu.get("name", ""),
        }
    return filtered


def _capture_tool_results_from_message(msg, s):
    """Capture toolResult text from a role="user" message for discovery
    parsing (local only — the caller forwards the full message unchanged).
    Tool results come as role="user" with toolResult content blocks.
    Envelope forensics (runtime side): one compact line per captured
    toolResult — toolUseId, status, content shape, first 200 chars.
    Guarded: never logs more, never raises (see events helper)."""
    if not (isinstance(msg, dict) and msg.get("role") == "user"
            and isinstance(msg.get("content"), list)):
        return
    for block in msg["content"]:
        if isinstance(block, dict) and "toolResult" in block:
            tr = block["toolResult"]
            tool_id = tr.get("toolUseId", "")
            # Extract text from content blocks
            tr_content = ""
            for c in tr.get("content", []):
                if isinstance(c, dict) and "text" in c:
                    tr_content += c["text"]
            if tool_id and tr_content:
                s.tool_results[tool_id] = tr_content[:2000]
                from .events import log_tool_result_envelope
                log_tool_result_envelope(
                    logger, tool_id, tr.get("status", ""), tr.get("content"),
                )


def _filter_tool_use_event(ctu, s):
    """Tool-use progress — forward name, ID, and the tool INPUT so the UI
    can surface the call arguments in the reasoning trace. Strands streams
    this input as a PARTIAL JSON string that accumulates over deltas; the
    client (chat_client) defers these partial strings and emits the
    tool_start from the COMPLETE parsed input in the assistant `message`
    toolUse block. Forwarding the (capped) string here lets the client
    distinguish a streaming tool call (str input) from a synthetic
    graph-first event (no input) — see chat_client."""
    tool_name = ctu.get("name", "")
    tool_id = ctu.get("toolUseId", "")
    # Track for self-learning discovery detection
    if tool_name and tool_id and tool_id not in s.seen_tool_use_ids:
        s.seen_tool_use_ids.add(tool_id)
        s.tool_calls_log.append({"name": tool_name, "toolUseId": tool_id})
    # Capture tool input locally (for discovery parsing) AND forward it.
    if tool_id and ctu.get("input"):
        s.tool_inputs[tool_id] = str(ctu["input"])[:2000]  # Cap at 2KB per tool
    return {"current_tool_use": {
        "toolUseId": tool_id,
        "name": tool_name,
        "input": str(ctu.get("input", ""))[:2000],
    }}


def _filter_stream_event(event, s):
    """Stream filter: only yield lightweight fields. Strands stream_async()
    emits raw events containing the full messages array, agent object, model
    object, and trace metadata on every token — forward only what the
    consumer needs (None ⇒ skip). Signature/redacted-content reasoning
    events carry no reasoningText and stay skipped; reasoning deltas are
    forwarded capped like tool inputs (Task 1I — skipping them causes
    30–60s of wire silence between tool batches)."""
    if not isinstance(event, dict):
        s.skipped_count += 1
        return None
    if "data" in event:
        return _filter_data_event(event, s)
    if "message" in event:
        _capture_tool_results_from_message(event["message"], s)
        return {"message": event["message"]}
    if "result" in event:
        # Terminal event: capture the AgentResult's metrics for the E7
        # measured-cost payload (_build_summary_payload + the cost hook).
        s.agent_metrics = getattr(event["result"], "metrics", None)
        return {"result": event["result"]}
    if "init_event_loop" in event:
        return {"init_event_loop": True}
    if "start_event_loop" in event:
        return {"start_event_loop": True}
    if "force_stop" in event:
        return {"force_stop": event["force_stop"]}
    if "current_tool_use" in event:
        return _filter_tool_use_event(event["current_tool_use"], s)
    if "reasoningText" in event:
        _rt = _reasoning_delta_text(event)
        if _rt is None:
            s.skipped_count += 1
            return None
        return {"reasoningText": _rt}
    s.skipped_count += 1
    return None


def _account_stream_bytes(out, s):
    """Measure a filtered event about to be yielded, categorize its bytes,
    and emit the periodic STREAM_BUDGET log every 500 events."""
    import json as _json
    event_size = len(_json.dumps(out, default=str))
    s.total_yielded_bytes += event_size
    if "message" in out:
        s.bytes_by_category["message"] += event_size
    elif "current_tool_use" in out:
        s.bytes_by_category["current_tool_use"] += event_size
    elif "data" in out:
        s.bytes_by_category["event"] += event_size
    else:
        s.bytes_by_category["other"] += event_size
    s.event_count += 1
    if s.event_count % 500 == 0:
        logger.info("STREAM_BUDGET session=%s events=%d total=%.1fMB msg=%.1fMB data=%.1fMB tool=%.1fMB",
                    s.session_id, s.event_count, s.total_yielded_bytes/(1024*1024),
                    s.bytes_by_category["message"]/(1024*1024),
                    s.bytes_by_category["event"]/(1024*1024),
                    s.bytes_by_category["current_tool_use"]/(1024*1024))


def _turn_determinism_score(s):
    """Deterministic score: computed ONCE, never LLM-judged. See
    agents/determinism.py for semantics — grounded = KG read OR input tokens
    ⊆ earlier results' value tokens; wasted = failed-and-retried or exact
    duplicate (hard evidence only). All calls grounded + zero waste ⇒ 100%."""
    from .determinism import score_tool_calls
    det = score_tool_calls(s.tool_calls_log, s.tool_inputs, s.tool_results)
    logger.info(
        "DETERMINISM: %d/%d grounded, %d wasted -> %d%% | %s",
        det["deterministic_calls"], len(s.tool_calls_log),
        len(det["wasted_steps"]), det["deterministic_pct"],
        [(c["step"], c["name"], "G" if c["grounded"] else "x")
         for c in det["per_call"]])
    return det


def _extract_rule_usage(s, is_briefing):
    """Single extraction (usage loop): scan the turn's tool results for
    applied-rule node ids ONCE; fetch their accepted rows with ONE GSI
    query. The finally-block usage hook reuses BOTH
    (precomputed_ids/prefetched_rows) instead of re-scanning and
    re-querying."""
    from .usage_tracking import extract_rule_node_ids
    s.rule_ids = extract_rule_node_ids(list(s.tool_results.values()))
    if s.rule_ids and not is_briefing:
        from .self_learning import get_accepted_rules_by_node_ids
        s.matched_rows = get_accepted_rules_by_node_ids(s.rule_ids)


def _build_summary_payload(s, _det_score):
    """Assemble the intelligence_summary payload: the shared wasted-call
    count, human rule labels (matched rows render labels; ids with no
    matched row fall back to label == id, Decision 3, so the UI still shows
    them), the recent proposed-mapping count, the ADDITIVE mechanical
    grounded_steps/deterministic_pct keys + wasted_calls override,
    and the ADDITIVE planned_with route-seed summary when the prelude
    captured one (absent otherwise), plus the ADDITIVE measured cost key
    (E7) when the terminal AgentResult carried token usage (absent when
    unmeasured — never zeros)."""
    wasted = query_intelligence.count_wasted_calls(s.tool_results.values())
    matched_ids = {r.get("neptune_node_id") for r in s.matched_rows}
    rules_applied = [
        {"id": r.get("neptune_node_id", ""),
         "label": query_intelligence.format_rule_label(r)}
        for r in s.matched_rows
    ] + [
        {"id": rid, "label": rid}
        for rid in sorted(s.rule_ids - matched_ids)
    ]
    # Check if new proposed mappings were created
    proposed_count = 0
    try:
        from .self_learning import get_pending_mappings
        pending = get_pending_mappings()
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        proposed_count = sum(1 for m in pending if m.get("discovered_at", "") > cutoff)
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    payload = query_intelligence.build_intelligence_summary(
        tool_names=[t["name"] for t in s.tool_calls_log],
        wasted_calls=wasted,
        rules_applied=rules_applied,
        new_proposed_mappings=proposed_count,
        determinism=_det_score,
        planned_with=getattr(s, "planned_with", None),
        cost=_usage_cost_payload(getattr(s, "agent_metrics", None)),
        composed_context=getattr(s, "composed_context", None),
    )
    payload["correlation_writebacks"] = (
        _count_correlation_writebacks(s.tool_calls_log, s.tool_results)
    )
    return _attach_earlier_sources(payload, s)


def _earlier_sources(agent) -> list[dict]:
    """The earlier cited results ``CitationOrdinalHook`` labels ``[E<k>]``
    this turn (``citation_ordinals.earlier_sources``), read from the agent's
    history BEFORE the turn's own messages are added: the same messages the
    hook reads at the start of the invocation. Names only; ``[]`` on a fault."""
    from .citation_ordinals import earlier_sources
    return earlier_sources(getattr(agent, "messages", None))


def _attach_earlier_sources(summary, holder):
    """Add ``earlier_sources`` (``[{"label", "tool"}]``, names only) to an
    intelligence summary when the turn's history had earlier cited results,
    so the client can show a Sources chip for each ``[E<k>]`` the answer
    cites (test event 32: a reused result had no chip). Absent otherwise,
    so a turn with no earlier results keeps the pinned key set."""
    entries = getattr(holder, "earlier_sources", None)
    if isinstance(summary, dict) and isinstance(entries, list) and entries:
        summary["earlier_sources"] = [dict(entry) for entry in entries]
    return summary


def _intelligence_summary_events(s):
    """Yield the intelligence_summary event, then the agent_analysis verdict.

    Agent Analysis is an LLM-annotated review of MECHANICALLY detected waste
    only: the LLM never decides what was wasted and never produces
    the score — it only explains the steps the deterministic detector
    already flagged and proposes schema annotations. Zero detected waste ⇒
    NO LLM call: a COMPUTED positive verdict is emitted instead (nothing to
    hallucinate — a perfect trace deserves an explicit verdict, not
    silence). SKIPPED for automated briefings — only user-initiated queries
    feed the learning loops."""
    total_tools = len(s.tool_calls_log)
    neptune_calls = sum(1 for t in s.tool_calls_log if _is_explore_graph(t["name"]))
    det = _turn_determinism_score(s)
    wasted_steps = det["wasted_steps"]
    # Hoisted briefing check — gates the rule lookup AND the analysis below.
    is_briefing = "briefing" in s.prompt.lower() or "operations briefing" in s.prompt.lower()
    _extract_rule_usage(s, is_briefing)
    yield {"intelligence_summary": _build_summary_payload(s, det)}
    if not wasted_steps and total_tools >= 3 and not is_briefing:
        yield {"agent_analysis": _clean_run_verdict(det, total_tools)}
        logger.info(
            "Agent analysis: clean run verdict, %d%% deterministic (computed, no LLM)",
            det["deterministic_pct"])
    if wasted_steps and total_tools >= 3 and s.tool_inputs and s.tool_results and not is_briefing:
        for event in _waste_analysis_events(s, det, total_tools, neptune_calls):
            yield event


def _provenance_events(s) -> list[dict]:
    """The legacy-path ``{"provenance": ...}`` frame (T1.2) — an ADDITIVE
    metadata key emitted right after the ``_intelligence_summary_events``
    loop; absent key ⇒ legacy client unaffected by construction.

    Built by the ONE builder (``agents/provenance.py``) from already-bounded
    turn state: the tool-call trace, per-snippet
    ``safe_tool_result_metadata`` (the snippets are the existing 2 KB-capped
    local capture — only their bounded metadata summary enters the frame,
    never text), and the rules_applied rows ``_extract_rule_usage`` already
    fetched (reused, never a second GSI query). The report-only path carries
    ``verdict=None`` and no typed evidence, so records cap out at
    ``partial``/``unverified`` (D4 honest absence;
    ``RESPONSE_INTEGRITY_MODE`` semantics unchanged). Swallow-guarded: any
    failure ⇒ frame ABSENT (never an empty-empty frame)."""
    try:
        from datetime import datetime, timezone
        from .provenance import build_provenance_records, format_captured_at
        from .provenance import uncited_query_refs
        from .tool_result_capture import safe_tool_result_metadata
        metadata: dict[str, dict] = {}
        for call in s.tool_calls_log:
            tool_id = call.get("toolUseId", "")
            snippet = s.tool_results.get(tool_id, "")
            if not tool_id or not snippet:
                continue
            metadata[tool_id] = safe_tool_result_metadata(
                {"content": [{"text": snippet}]},
                tool_name=call.get("name", ""), max_chars=2048)
        payload = build_provenance_records(
            session_id=s.session_id,
            tool_calls=s.tool_calls_log,
            tool_inputs=s.tool_inputs,
            tool_results_metadata=metadata,
            evidence_by_tool={},  # report-only: no typed evidence bindings
            rules_applied=_format_rules_applied(s.rule_ids, s.matched_rows),
            verdict=None,
            captured_at=format_captured_at(datetime.now(timezone.utc)),
            graph_query_refs=uncited_query_refs(
                session_id=s.session_id, tool_calls=s.tool_calls_log,
                tool_inputs=s.tool_inputs),
        )
        payload = _enriched_provenance_payload(payload, s.session_id)
        if (not payload["records"] and not payload["truncated"]
                and not payload.get("graph_queries")):
            return []
        return [{"provenance": payload}]
    except Exception as exc:  # noqa: BLE001 - additive frame must never break the stream
        logger.debug("Provenance frame generation failed: %s", type(exc).__name__)
        return []


def _provenance_query_sidecar(s) -> list[dict]:
    """The legacy-path ``{"provenance_queries": ...}`` sidecar frame (T3.2).

    SERVER-ONLY (D6): yielded right after the ``provenance`` frame,
    intercepted and persisted by the app server (T3.3) and NEVER broadcast
    on the SSE stream. Entries exist ONLY where the runtime actually saw
    query text — Mode-B SQL / ``explore_graph`` inputs in ``s.tool_inputs``
    (2 KB-capped); Mode-A NL sub-questions are omitted, and Tier 3 renders
    honest absence for them. Entries are built by the ONE sidecar builder
    (``agents.provenance.build_sidecar_entries`` — strict JSON parse: the
    runtime captures JSON); ``kg_artifact_ids`` reuses the rule ids
    ``_extract_rule_usage`` already fetched (never a second query). Same
    swallow guard as the provenance frame: any failure ⇒ frame ABSENT."""
    try:
        from datetime import datetime, timezone
        from .provenance import build_sidecar_entries, format_captured_at
        entries = build_sidecar_entries(
            session_id=s.session_id, tool_calls=s.tool_calls_log,
            tool_inputs=s.tool_inputs, rule_ids=s.rule_ids,
            captured_at=format_captured_at(datetime.now(timezone.utc)))
        if not entries:
            return []
        return [{"provenance_queries": entries}]
    except Exception as exc:  # noqa: BLE001 - sidecar must never break the stream
        logger.debug("Provenance query sidecar generation failed: %s", type(exc).__name__)
        return []


def _analysis_call_log(s):
    """Concise tool-call log for the analysis LLM. Failure detection uses
    the SHARED envelope markers in query_intelligence.is_failed_result
    (deliberately NOT the bare word "error" — see the marker comment there)
    so the summary's wasted_calls and this analysis can never drift."""
    call_log_lines = []
    wasted_call_count = 0
    for i, tc in enumerate(s.tool_calls_log):
        tid = tc["toolUseId"]
        name = tc["name"]
        inp = s.tool_inputs.get(tid, "")[:400]
        res = s.tool_results.get(tid, "")[:400]
        failed = query_intelligence.is_failed_result(res)
        if failed:
            wasted_call_count += 1
        call_log_lines.append(f"Step {i+1}: {name}\n  Input: {inp}\n  Result: {'FAILED - ' if failed else ''}{res}")
    return call_log_lines, wasted_call_count


def _analysis_prompt_text(det, total_tools, neptune_calls, call_log_lines):
    """The analysis-LLM prompt: the FULL call sequence (capped for token
    safety — when the cap truncates, say so explicitly so the model never
    reports counts over calls it cannot see) plus the mechanically flagged
    wasted steps as the ONLY review focus."""
    import json as _json2
    _ANALYSIS_CALL_CAP = 30
    shown_lines = call_log_lines[:_ANALYSIS_CALL_CAP]
    shown_count = len(shown_lines)
    omitted = total_tools - shown_count
    scope_note = (
        f"You can see all {shown_count} tool calls below."
        if omitted <= 0 else
        f"You can see the first {shown_count} of {total_tools} tool calls below; "
        f"{omitted} later calls are omitted — base every count ONLY on the calls "
        f"shown and never cite a total you cannot see."
    )
    return f"""You are analyzing an AI orchestrator's query execution across manufacturing systems (ERP, MES, CMMS, PLM).
The orchestrator made {total_tools} tool calls; {neptune_calls} used the knowledge graph. {scope_note}

TOOL CALL LOG:
{chr(10).join(shown_lines)}

MECHANICALLY DETECTED WASTED STEPS (failed-and-retried or exact duplicate — detected by code, not judgment):
{_json2.dumps([{"step": c["step"], "tool": c["name"]} for c in det["wasted_steps"]])}

TASK: Explain ONLY the wasted steps listed above and produce a JSON response. RULES:
- "wasted_calls" MUST contain exactly the steps listed above — no more, no fewer. You explain WHY each failed or was redundant and what schema annotation would have prevented it. NEVER flag any other step: calls that succeeded on first attempt are correct by definition, even if you would have written them differently.
- Only reference tables/fields that ACTUALLY APPEAR in the tool results above
- Do NOT propose annotations for tables that don't exist (no "sales_orders", no "customer_orders" unless they appear in the results)
- Do NOT propose infrastructure fixes (endpoint URLs, connection issues) as schema annotations
- Focus on: wrong column used for a concept, missing type information, missing enum values, wrong join assumptions

Return JSON with:
1. "summary": One sentence describing why the wasted steps failed. Base every count ONLY on the calls in the log above — never cite a total you cannot see.
2. "wasted_calls": Array of {{"step": N, "tool": "name", "reason": "why this exact call failed or was redundant", "fix": "specific schema annotation that would prevent this"}} — exactly the steps listed above. Describe the failed lookup; do NOT invent or critique a mapping proposal, and do NOT call a successful same-system comparison redundant. Max 5.
3. "recommendations": Array of {{"system": "system_id", "table": "real_table_name", "field": "real_field_name", "annotation": "the domain context the LLM was missing", "impact": "saves N exploratory calls"}}. Only include if the table and field appear in the results above. The "field" must be ONE real column name (never a comma-separated list, never "available columns" or other describe-output fragments); the "annotation" must add domain context — for example WHICH CONCEPT or ROLE this exact column holds (a concept→column binding, the useful anti-guessing hint so the model stops guessing column names), enum values, a type gotcha, or a cross-system hint. Do NOT return an empty annotation, an infrastructure fix, or a bare restatement of the column name with no concept/role/context. Max 3.

Return ONLY valid JSON, no markdown, no explanation."""


def _invoke_analysis_model(analysis_prompt):
    """One converse call to the analysis model. Model: SYSTEM_AGENT_MODEL
    env override, else the model catalog's AGENT default (the single source;
    never hardcode a model id here).
    Converse is model-agnostic, so the fallback family can change in the
    catalog without touching this call."""
    from .aws_session import bedrock_runtime_client as _bedrock_runtime_client
    _bedrock = _bedrock_runtime_client()
    from .model_catalog import AGENT as _AGENT_ROLE
    from .model_catalog import accepts_temperature as _accepts_temperature
    from .model_catalog import default_model as _default_model
    model_id = os.environ.get("SYSTEM_AGENT_MODEL") or _default_model(_AGENT_ROLE)
    inference = {"maxTokens": 1500, "temperature": 0.1}
    if not _accepts_temperature(model_id):
        inference.pop("temperature")   # Sonnet 5 rejects any temperature
    response = _bedrock.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": analysis_prompt}]}],
        inferenceConfig=inference,
    )
    return response.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "")


def _parsed_analysis(analysis_text, _det_score, s):
    """Parse the analysis LLM's JSON → (analysis_data, valid_recs); (None,
    []) when no JSON. The score is COMPUTED — always override whatever the
    LLM emitted (it explains flagged steps, never re-decides), and keep only
    wasted-call rows for steps the mechanical detector flagged. Validate
    recommendations — reject garbage, but KEEP a concept->column binding
    (LEVER 3): the useful anti-guessing hint. _recommendation_accepted
    still drops empty/N/A, infrastructure, and 'ensure exists' noise, and
    does NOT weaken the one-real-column / must-appear-in-results guards
    (prompt + presence check)."""
    import json as _json2
    import re as _re3
    _json_match = _re3.search(r'\{[\s\S]*\}', analysis_text)
    if not _json_match:
        return None, []
    analysis_data = _json2.loads(_json_match.group())
    analysis_data["deterministic_pct"] = _det_score["deterministic_pct"]
    analysis_data["wasted_calls"] = _validated_wasted_calls(
        analysis_data.get("wasted_calls", []),
        s.tool_calls_log,
        s.tool_results,
        flagged_steps={c["step"] for c in _det_score["wasted_steps"]},
    )
    valid_recs = [
        rec for rec in analysis_data.get("recommendations", [])
        if _recommendation_accepted(rec)
    ]
    analysis_data["recommendations"] = valid_recs[:3]
    return analysis_data, valid_recs


def _persist_analysis_recommendations(valid_recs, turn_had_wasted, total_tools):
    """Write valid recommendations as proposed field_annotations (with
    dedup). A RECURRING discovery (LEARN loop) — the same field annotation
    surfaced before — is promoted into the system's PERSISTED query_guidance
    (durable, system-level guidance the orchestrator reads on future
    queries) and skipped as a duplicate proposal. Source-level noise
    prevention (3b): a turn with zero wasted/FAILED calls emits NO new
    field_annotation proposals."""
    from .self_learning import log_proposed_mapping, get_all_mappings, append_query_guidance_note
    # redundant since S3.1 lookup-before-write; removable later
    existing = get_all_mappings()
    existing_keys = {f"{m.get('from_system','')}-{m.get('from_field','')}" for m in existing}
    for rec in valid_recs:
        field_key = f"{rec.get('system', 'unknown')}-{rec.get('table', '')}.{rec.get('field', '')}"
        if field_key in existing_keys:
            try:
                _ann = rec.get("annotation", "")
                if _ann:
                    append_query_guidance_note(
                        rec.get("system", ""),
                        f"{rec.get('table', '')}.{rec.get('field', '')}: {_ann}",
                    )
            except Exception as _ge:  # noqa: BLE001 - best-effort learning
                logger.debug(f"guidance promotion skipped: {_ge}")
            continue  # Skip duplicate proposal
        if not turn_had_wasted:
            continue
        log_proposed_mapping(
            from_system=rec.get("system", "unknown"),
            from_field=f"{rec.get('table', '')}.{rec.get('field', '')}",
            from_value="",
            to_system=rec.get("system", "unknown"),
            to_field=f"{rec.get('table', '')}.{rec.get('field', '')}",
            to_value="",
            concept="schema-annotation",
            confidence=0.9,
            query_context=f"Auto-analysis: {rec.get('annotation', '')}",
            tool_calls_count=total_tools,
            proposed_type="field_annotation",
            provenance="analysis-llm",
        )


def _waste_analysis_events(s, det, total_tools, neptune_calls):
    """Run the waste-focused analysis LLM and yield its agent_analysis
    event ("wasted" for noise prevention = a deterministic FAILED marker
    this turn OR an LLM-reported wasted_call). Thin fail-loud guard (ADR
    0003 F): log_proposed_mapping RAISES on backend failure; surface it
    loudly but never kill the user query."""
    try:
        call_log_lines, _wasted_call_count = _analysis_call_log(s)
        analysis_prompt = _analysis_prompt_text(det, total_tools, neptune_calls, call_log_lines)
        _analysis_text = _invoke_analysis_model(analysis_prompt)
        analysis_data, valid_recs = _parsed_analysis(_analysis_text, det, s)
        if analysis_data is None:
            return
        # Only yield if there's something useful
        if valid_recs or analysis_data.get("wasted_calls"):
            yield {"agent_analysis": analysis_data}
            logger.info(f"Agent analysis: {len(valid_recs)} recommendations, {analysis_data.get('deterministic_pct', '?')}% deterministic")
            _turn_had_wasted = (
                _wasted_call_count > 0
                or bool(analysis_data.get("wasted_calls"))
            )
            # X-3: synthetic runs (try-it replays, feedback explorations)
            # read the analysis but never persist its field_annotation
            # proposals / query-guidance promotions.
            if not _is_synthetic_session(getattr(s, "session_id", "")):
                _persist_analysis_recommendations(valid_recs, _turn_had_wasted, total_tools)
    except Exception as e:
        logger.warning(f"Agent analysis failed (non-critical): {e}", exc_info=True)


def _log_turn_complete(s):
    """The ORCHESTRATOR COMPLETE line with the per-category byte budget.

    ``tool_calls`` is every tool the turn called. This line is written in the
    turn's ``finally``, so the Agents tab reads the count here even for a
    turn that never logged its DETERMINISM line.
    """
    logger.info("=== ORCHESTRATOR COMPLETE === session=%s total_events=%d tool_calls=%d skipped=%d yielded=%.1fMB msg=%.1fMB evt=%.1fMB tool=%.1fMB",
                s.session_id, s.event_count, len(getattr(s, "tool_calls_log", None) or ()),
                s.skipped_count,
                s.total_yielded_bytes/(1024*1024),
                s.bytes_by_category["message"]/(1024*1024),
                s.bytes_by_category["event"]/(1024*1024),
                s.bytes_by_category["current_tool_use"]/(1024*1024))


def _post_turn_learning_checks(s):
    """Self-learning finally-block checks (all best-effort, all SKIPPED for
    automated briefings AND try-it replays — ``_skip_post_turn_capture``,
    X-3): Neptune gaps followed by successful cross-system
    SQL; reason-code → failure-mode value_mappings from any tool result
    exposing an opaque machine-stop reason/fault CODE column (generic;
    reuses the shared proposal pipeline + scorer); (LEVER 2) immediate
    durable query_guidance for a COLUMN_NOT_FOUND self-correction — not
    gated behind cross-turn recurrence, so a fresh conversation stops
    re-guessing; and (ADR-0059) the empty-join failure-signal count."""
    if _skip_post_turn_capture(s):
        return
    try:
        _check_for_discoveries(s.tool_calls_log, s.tool_inputs, s.tool_results, s.prompt, s.session_id)
    except Exception as e:
        logger.debug(f"Self-learning discovery check failed (non-critical): {e}")
    try:
        _check_for_reason_code_mappings(s.tool_calls_log, s.tool_inputs, s.tool_results, s.session_id)
    except Exception as e:
        logger.debug(f"Self-learning reason-code check failed (non-critical): {e}")
    try:
        _persist_column_not_found_guidance(s.tool_calls_log, s.tool_inputs, s.tool_results)
    except Exception as e:
        logger.debug(f"Column-not-found guidance persist failed (non-critical): {e}")
    # ADR-0059: empty-join failure signal — best-effort single write, own
    # never-raise boundary (its synthetic/briefing gate mirrors the one above).
    _record_empty_join_signal(s.tool_calls_log, s.tool_results, s.prompt, s.session_id)
    # Metric-term miss that survived into the FINAL answer
    # — same posture (own never-raise boundary, same gates, telemetry-only).
    _record_metric_term_miss_signal(
        s.tool_calls_log, s.tool_results, s.response_text, s.prompt, s.session_id)
    # ADR-0063 D5: business-context-not-reflected signal — same posture (own
    # never-raise boundary, same synthetic/briefing gate, bounded reads).
    _record_context_not_reflected_signal(
        s.tool_calls_log, s.response_text, s.prompt, s.session_id)


def _capture_correlation_insights(s):
    """Parse LLM CORRELATION-PROPOSAL blocks from response text into
    `insight` proposals (findings — never identity_rule graph rules);
    skipped for automated briefings. known_systems() (F6) is
    PROCESS-AGNOSTIC — the hydrated catalog in the ECS app, a TTL-cached
    Neptune read in the AgentCore runtime. It can still return [] (catalog
    empty AND graph read failing): then the extractor has no system aliases
    and insight capture no-ops for this turn — logged so the condition is
    visible instead of silent. Two known_systems() calls are deliberate:
    the second is the wiring-tripwire-pinned extractor feed (cheap; never
    raises). Narrative findings carry EMPTY from_field/to_field (a sentence
    is never a table.column pair) — the pattern text lives in query_context
    ("Auto-analysis: ..." → ingestion derives the annotation digest from
    it). Thin fail-loud guard (ADR 0003 F): log_proposed_mapping RAISES on
    backend failure; log loudly, never kill the live query."""
    insights = []
    try:
        if not _skip_post_turn_capture(s):  # briefings + try-it replays (X-3)
            full_text = "".join(s.response_text)
            from .self_learning import known_systems
            if not known_systems():
                logger.info(
                    "insight capture skipped: no known systems "
                    "(catalog empty + graph unavailable)"
                )
            insights = _extract_correlation_insights(full_text, known_systems())
        if insights:
            from .self_learning import log_proposed_mapping
            for ins in insights:
                log_proposed_mapping(
                    from_system=ins["from_system"],
                    from_field="",
                    from_value="",
                    to_system=ins["to_system"],
                    to_field="",
                    to_value="",
                    concept=ins["concept"],
                    confidence=ins["confidence"],
                    query_context=ins["query_context"],
                    tool_calls_count=len(s.tool_calls_log),
                    proposed_type="insight",
                    provenance="orchestrator",
                )
                logger.info(
                    f"Self-learning: LLM correlation insight logged "
                    f"({ins['from_system']}→{ins['to_system'] or '-'})"
                )
    except Exception as e:
        logger.warning(f"Correlation proposal parsing failed (non-critical): {e}", exc_info=True)


def _capture_value_mapping_proposals(s):
    """Parse LLM VALUE-MAPPING-PROPOSAL blocks from response text; skipped
    for synthetic sessions and automated briefings
    (``_skip_post_turn_capture``). ``query_context`` stores the ORIGINAL user
    question (``s.raw_prompt`` — the payload's ``rawPrompt``, R2), never the
    seed-augmented or app-enriched turn text, and
    ``_query_context_for_storage`` guarantees no fenced untrusted block can
    reach storage. No structural change needed here: the
    free-text from_system/to_system parsed below are canonicalized
    centrally by the system-id normalization gate in
    self_learning.log_proposed_mapping (ingestion normalizes against
    catalog ids and drops self-referential systems). ADR 0003 F thin
    guard: log_proposed_mapping raises — log loudly, but never let a
    discovery failure kill the live query."""
    vm_proposals = []
    try:
        if not _skip_post_turn_capture(s):  # briefings + try-it replays (X-3)
            import re as _re3
            full_text = "".join(s.response_text)
            vm_proposals = _re3.findall(
                r'---VALUE-MAPPING-PROPOSAL---\s*(.+?)\s*---END-PROPOSAL---',
                full_text, _re3.DOTALL
            )
        if vm_proposals:
            logged_vm_count = _ingest_value_mapping_proposals(
                vm_proposals,
                prompt=getattr(s, "raw_prompt", s.prompt),
                tool_calls_count=len(s.tool_calls_log),
            )
            logger.info(
                "Self-learning: %d value mapping proposal(s) logged",
                logged_vm_count,
            )
    except Exception as e:
        logger.warning(f"Value mapping proposal logging failed (non-critical): {e}", exc_info=True)


def _integrity_post_turn_learning(state, prompt: str, session_id: str) -> None:
    """Post-turn learning on the integrity path (R1) — mirrors the legacy
    ``finally``-block seam: the usage-ledger write plus the three capture
    hooks. Same functions, same gating: each hook skips automated briefings
    and swallows its own failures; the call site in
    ``_invoke_integrity_runtime`` adds one more swallow so learning can never
    degrade the already-emitted stream.

    ``prompt`` is the pre-prelude payload prompt, so briefing gating and the
    usage hook see honest text; ``state.prompt`` is the ORIGINAL user
    question (the payload's ``rawPrompt`` — R2) and is what stored proposal
    ``query_context`` derives from.
    ``tool_inputs``/``tool_results`` stay EMPTY by design — the
    IntegrityContext never retains raw tool payloads (claim-check policy) —
    so the result-scanning checks no-op while the usage-ledger write reuses
    ``final_events``' single extraction (``precomputed_ids`` /
    ``prefetched_rows``: the turn is never re-scanned or re-queried), stamped
    ``evidence_class="relationship-proof"`` — the integrity extraction reads
    verified relationship proofs (ADR-0052 (b)). The threaded ``route_ctx``
    arms the route-intersection fallback, inert here unless the env runs
    report-only AND the extraction yielded nothing.
    """
    from types import SimpleNamespace

    question = getattr(state, "prompt", None)  # the payload's rawPrompt (R2)
    s = SimpleNamespace(
        prompt=prompt, raw_prompt=question if question is not None else prompt,
        session_id=session_id,
        tool_calls_log=[{"name": name, "toolUseId": tool_id}
                        for tool_id, name in state.tool_names.items()],
        tool_inputs={}, tool_results={},
        response_text=list(state.response_text),
    )
    _post_turn_learning_checks(s)
    route_ctx = _route_ctx(state.seed_rule_index, state.tool_names.values(),
                           getattr(state.context, "tool_system_ids", None))
    try:
        _record_rule_usage_hook({}, prompt, precomputed_ids=state.rule_ids,
                                prefetched_rows=state.matched_rows,
                                evidence_class="relationship-proof",
                                route_ctx=route_ctx, session_id=session_id)
    except Exception as e:
        logger.debug(f"Rule usage recording failed (non-critical): {e}")
    # E7 cost ledger — the integrity-path mirror of the legacy finally-block
    # bump (own gate + swallow inside the hook).
    _bump_turn_cost_hook(
        getattr(getattr(state, "agent_result", None), "metrics", None),
        prompt, session_id)
    _capture_correlation_insights(s)
    _capture_value_mapping_proposals(s)


async def _filtered_stream(agent, prompt, s):
    """The filtered + byte-accounted model event stream (extracted from the
    invoke loop so ``_run_orchestrator`` stays under the 50-line gate).

    The model stream runs through the ONE output-cap recovery
    (``output_cap_recovery``): a reply cut off at the orchestrator's output cap
    is continued once in the same conversation; a second cut streams the fixed
    output-limit error frame instead of failing the turn generically. Tool
    calls a cut reply started never ran, so they leave the turn's trace
    (``_forget_cut_tool_calls``) before any summary or learning reads it."""
    from .output_cap_recovery import (OUTPUT_LIMIT_ERROR_CODE, OutputLimitExceeded,
                                      stream_with_output_cap_recovery)
    s.earlier_sources = _earlier_sources(agent)
    cut_again = False
    try:
        async for event in stream_with_output_cap_recovery(
                agent, prompt, session_id=s.session_id,
                on_cut=lambda ids: _forget_cut_tool_calls(s, ids)):
            out = _filter_stream_event(event, s)
            if out is None:
                continue
            _account_stream_bytes(out, s)
            yield out
    except OutputLimitExceeded:
        cut_again = True
    if cut_again:
        yield {"error": OUTPUT_LIMIT_ERROR_CODE}


def _forget_cut_tool_calls(s, tool_ids) -> None:
    """Drop tool calls a cut reply started but never sent from the legacy turn
    trace: the summary counts (``total_tool_calls``/``system_queries``, which
    golden ``systems_min`` gates read), determinism, provenance and post-turn
    learning all read these four fields."""
    ids = set(tool_ids)
    if not ids:
        return
    s.tool_calls_log[:] = [c for c in s.tool_calls_log if c.get("toolUseId") not in ids]
    s.seen_tool_use_ids.difference_update(ids)
    for tool_id in ids:
        s.tool_inputs.pop(tool_id, None)
        s.tool_results.pop(tool_id, None)


def _run_orchestrator():
    """BedrockAgentCoreApp orchestrator — thin loop over the turn helpers (≤50-line gate)."""
    from bedrock_agentcore import BedrockAgentCoreApp
    from .orchestrator import build_orchestrator

    app = BedrockAgentCoreApp()

    @app.entrypoint
    async def invoke(payload, context):
        s = _new_turn_state(payload, context)
        agent = build_orchestrator(
            session_id=s.session_id, user_id=s.user_id,
            write_memory=s.write_memory, user_access_token=s.user_access_token)
        prompt, _tool_results = s.prompt, s.tool_results
        try:
            for _gf_event in s.graph_first_events:
                _register_graph_first_call(_gf_event, s)
                yield _gf_event
            async for out in _filtered_stream(agent, prompt, s):
                yield out
            try:  # summary yields INSIDE try so AgentCore forwards it
                for _summary_event in _intelligence_summary_events(s):
                    yield _summary_event
            except Exception as e:
                logger.debug(f"Intelligence summary generation failed: {e}")
            for _prov_event in _provenance_events(s):  # additive; swallows, [] on failure
                yield _prov_event
            # Server-only Tier-3 sidecar (T3.2, D6) — chat_client strips it (T3.3).
            for _pq_event in _provenance_query_sidecar(s):
                yield _pq_event
        except Exception as exc:
            _log_invocation_failure(exc, s.session_id)
            yield {"error": "Agent invocation failed"}
        finally:
            _log_turn_complete(s)
            _post_turn_learning_checks(s)
            # Usage loop (item C) + ADR-0052 fallback + E7 cost; never raise.
            _route = _route_ctx(_seed_rule_index_from_text(prompt),
                                (c.get("name") for c in s.tool_calls_log))
            try:
                _record_rule_usage_hook(_tool_results, prompt, precomputed_ids=s.rule_ids,
                                        prefetched_rows=s.matched_rows, route_ctx=_route, session_id=s.session_id)
            except Exception as e:
                logger.debug(f"Rule usage recording failed (non-critical): {e}")
            _bump_turn_cost_hook(getattr(s, "agent_metrics", None), prompt, s.session_id)
            _capture_correlation_insights(s)
            _capture_value_mapping_proposals(s)

    app.run()


_run_orchestrator_base = _run_orchestrator


def _pop_delegated_subject(payload) -> "str | None":
    """Pop the RESERVED ``delegatedSubject`` key (ADR-0079), unconditionally.

    The ``_adt_identity`` pop discipline: the key never reaches the prompt or
    the model. Returns the subject string, or ``None`` for absent/non-string.
    """
    from .auth.forwarding import DELEGATED_SUBJECT_FIELD
    try:
        subject = payload.pop(DELEGATED_SUBJECT_FIELD, None)
    except Exception:  # noqa: BLE001 - never break the turn on a weird payload
        return None
    return subject if isinstance(subject, str) and subject else None


#: ADR-0079: the delegated subject of the CURRENT invocation — set on EVERY
#: invocation by ``_runtime_entrypoint`` (valid-or-``None``) and read by
#: ``_chart_injecting_build``. Never logged.
_delegated_subject_var: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "adt_delegated_subject", default=None)

#: The question an "Ask again" invocation re-asks (``history_turns``): set on
#: EVERY invocation by ``_runtime_entrypoint`` and read by the chat-history
#: read wrapper while the same invocation builds its agent.
from .history_turns import RETRY_QUESTION_VAR as _retry_question_var  # noqa: E402


def _pop_retry_question(payload) -> "str | None":
    """Pop the RESERVED ``retryUndeliveredTurn`` key, unconditionally (the
    ``_pop_delegated_subject`` discipline: it never reaches the prompt or the
    model). Only an exact ``True`` counts; it returns the re-asked question
    (the payload's ``rawPrompt``, else its ``prompt``), anything else ``None``."""
    from .history_turns import RETRY_PAYLOAD_KEY
    try:
        flag = payload.pop(RETRY_PAYLOAD_KEY, None)
    except Exception:  # noqa: BLE001 - never break the turn on a weird payload
        return None
    if flag is not True:
        return None
    for key in ("rawPrompt", "prompt"):
        question = payload.get(key)
        if isinstance(question, str) and question.strip():
            return question
    return None


#: The RESERVED single key of a keepalive heartbeat frame. No text, no data,
#: no timestamps — a reader keys ONLY on this one key to drop the frame.
HEARTBEAT_KEY = "adt_heartbeat"
#: The exact frame emitted while the orchestrator stream is idle.
HEARTBEAT_FRAME = {HEARTBEAT_KEY: 1}

_HEARTBEAT_DEFAULT_SECONDS = 30.0
_HEARTBEAT_FLOOR_SECONDS = 1.0
#: The ceiling MUST stay safely BELOW the app's socket read timeout
#: (chat_client.RUNTIME_STREAM_READ_TIMEOUT_S = 300 s). If an override were
#: allowed up to or past that, an idle turn could time out before the next
#: heartbeat and lose the answer — the exact bug this keepalive fixes. 120 s
#: leaves a 2.5x margin and still fires at least twice inside the window.
#: A test pins this < RUNTIME_STREAM_READ_TIMEOUT_S.
_HEARTBEAT_CEILING_SECONDS = 120.0


def _heartbeat_interval_seconds() -> float:
    """The idle-heartbeat interval, coerced FAIL-CLOSED from the env.

    ``ADT_RUNTIME_STREAM_HEARTBEAT_SECONDS`` overrides the 30 s default. A
    missing/blank/non-numeric/non-finite/non-positive value falls back to the
    default; the result is clamped to ``[floor, ceiling]`` so a typo can never
    disable the keepalive (0/∞) or hammer the stream (sub-second)."""
    raw = os.environ.get("ADT_RUNTIME_STREAM_HEARTBEAT_SECONDS")
    if raw is None or not raw.strip():
        return _HEARTBEAT_DEFAULT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _HEARTBEAT_DEFAULT_SECONDS
    import math
    if not math.isfinite(value) or value <= 0:
        return _HEARTBEAT_DEFAULT_SECONDS
    return max(_HEARTBEAT_FLOOR_SECONDS, min(value, _HEARTBEAT_CEILING_SECONDS))


class _StreamDone:
    """Sentinel: the inner generator ended normally."""
    __slots__ = ()


class _StreamError:
    """Sentinel carrier: the inner generator raised ``exc`` (re-raised in the
    consumer with its type unchanged)."""
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException):
        self.exc = exc


_STREAM_DONE = _StreamDone()


class _HeartbeatChannel:
    """One-event rendezvous between the producer task and the consumer.

    ``queue`` (maxsize 1) carries the event or a terminal sentinel. ``resumed``
    is the acknowledgement the consumer sets AFTER it resumes past an event's
    ``yield``; the producer waits on it before pulling the next inner event, so
    the inner generator advances only when the consumer asks — direct-iteration
    pacing, never more than one event pulled ahead of the downstream yield."""
    __slots__ = ("queue", "resumed")

    def __init__(self):
        self.queue: "asyncio.Queue" = asyncio.Queue(maxsize=1)
        self.resumed = asyncio.Event()


async def _heartbeat_producer(inner, channel: "_HeartbeatChannel") -> None:
    """Drive the ENTIRE inner generator in ONE task, one event at a time.

    Running the whole turn in a single task is the fix for the per-``__anext__``
    task bug: the inner generator — the orchestrator turn, with OpenTelemetry
    spans, ``_delegated_subject_var`` and other request-scoped contextvars —
    keeps ONE consistent context across all its steps (copied from the caller's
    context when this task is created, exactly what direct iteration saw).

    Pacing: ``resumed`` is cleared BEFORE each ``put`` (so a fast consumer
    cannot set-then-have-it-cleared and deadlock) and awaited AFTER, so the
    inner generator is never advanced to event N+1 until the consumer has
    resumed past event N's yield. The inner generator's own ``finally``
    (turn-complete logging, learning capture) runs here on normal end and on
    cancellation when the consumer closes early."""
    try:
        async for event in inner:
            channel.resumed.clear()
            await channel.queue.put(event)
            await channel.resumed.wait()   # consumer resumed past this yield
    except asyncio.CancelledError:
        # Consumer closed early: let the cancellation unwind the producer (and,
        # via the finally below, the inner generator's own teardown). Do NOT
        # try to enqueue a sentinel — the maxsize-1 queue may be full and no
        # one is reading, which would deadlock the awaited cancel.
        raise
    except BaseException as exc:  # noqa: BLE001 - forwarded verbatim to consumer
        await channel.queue.put(_StreamError(exc))
    else:
        await channel.queue.put(_STREAM_DONE)
    finally:
        # ``async for`` closes a real async GENERATOR on cancel, but a plain
        # async ITERATOR (custom ``__anext__``) is not auto-closed — drive its
        # ``aclose`` explicitly so its teardown always runs. Idempotent for a
        # real async generator (already closed).
        aclose = getattr(inner, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


class _InFlightTools:
    """Tool calls the stream has started and not yet returned a result for,
    read from the events passing through (both runtime paths' shapes): a
    ``current_tool_use`` starts one; a ``toolResult`` in a ``message`` or a
    ``tool_result_metadata`` event ends it. Names only, never inputs."""
    __slots__ = ("open",)

    def __init__(self):
        self.open: dict = {}

    def see(self, event) -> None:
        if not isinstance(event, dict):
            return
        current = event.get("current_tool_use")
        if isinstance(current, dict) and current.get("toolUseId") and current.get("name"):
            self.open[current["toolUseId"]] = str(current["name"])
        done = event.get("tool_result_metadata")
        if isinstance(done, dict):
            self.open.pop(done.get("toolUseId"), None)
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            result = block.get("toolResult") if isinstance(block, dict) else None
            if isinstance(result, dict):
                self.open.pop(result.get("toolUseId"), None)

    def describe(self) -> str:
        import re
        names = sorted({re.sub(r"[^A-Za-z0-9_.:-]", "_", n)[:96] for n in self.open.values()})
        return ",".join(names[:5]) or "none"


def _log_client_gone(started: float, in_flight: "_InFlightTools") -> None:
    """ONE line when the consumer of the runtime stream went away early (the
    app closed or cancelled it): how long the turn had run and which tool
    call was still running. Never raises."""
    import time
    try:
        logger.info("ORCHESTRATOR_CLIENT_GONE elapsed_s=%.1f in_flight_tool=%s",
                    time.monotonic() - started, in_flight.describe())
    except Exception:  # noqa: BLE001 - diagnostics never break teardown
        pass


async def _with_stream_heartbeat(inner, interval: "float | None" = None):
    """Yield ``inner``'s events unchanged + in order, interleaving a heartbeat
    frame whenever ``inner`` stays idle longer than ``interval`` seconds.

    The whole inner generator runs in ONE dedicated producer task (so the turn
    keeps a single consistent context) that is paced to a single event lead by
    the channel's rendezvous. The consumer awaits the next event with an
    ``interval`` timeout and emits a heartbeat on timeout — including while the
    producer is blocked inside a slow inner step. Inner exceptions re-raise in
    the consumer with their type unchanged. On consumer close/cancellation the
    producer is cancelled and awaited so the inner generator's ``finally`` still
    runs, after one ``ORCHESTRATOR_CLIENT_GONE`` line."""
    import time
    if interval is None:
        interval = _heartbeat_interval_seconds()
    started, in_flight = time.monotonic(), _InFlightTools()
    channel = _HeartbeatChannel()
    producer = asyncio.ensure_future(_heartbeat_producer(inner, channel))
    try:
        while True:
            try:
                item = await asyncio.wait_for(channel.queue.get(), interval)
            except asyncio.TimeoutError:
                yield dict(HEARTBEAT_FRAME)   # idle: keep the socket alive
                continue
            if item is _STREAM_DONE:
                return
            if isinstance(item, _StreamError):
                raise item.exc
            in_flight.see(item)
            yield item
            channel.resumed.set()   # ack: let the producer pull the next event
    except (GeneratorExit, asyncio.CancelledError):
        _log_client_gone(started, in_flight)
        raise
    finally:
        if not producer.done():
            producer.cancel()
        try:
            await producer           # lets the inner generator's finally run
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def _reset_graph_turn_budget() -> None:
    """Open this turn's explore_graph budget (``graph_turn_budget``): one count
    for every agent invocation of the turn, the output-cap continuation's
    included."""
    from .graph_turn_budget import reset_turn_budget
    reset_turn_budget()


def _answer_warmup_or(turn):
    """Answer the app's 180 s warmup ping here; pass every other payload to *turn*.

    The ping only needs the runtime to answer, so it is answered at the
    sanctioned seam without building the orchestrator or calling the model
    (agents/warmup.py).
    """
    async def selected(payload, context):
        from .warmup import is_warmup_payload
        if is_warmup_payload(payload):
            logger.info("=== ORCHESTRATOR WARMUP === (answered without a model call)")
            yield {"warmup": "ok"}
            return
        events = turn(payload, context)
        try:
            async for event in events:
                yield event
        finally:
            # Close the turn now (its own finally blocks run in order), never
            # leaving it to the garbage collector when the stream is cut.
            await events.aclose()
    return selected


def _runtime_entrypoint(legacy, build_orchestrator):
    async def selected(payload, context):
        from .config import mode_a_integrity_enforced, response_integrity_mode
        from .concept_notes import reset_turn_notes
        from .route_facts import reset_turn as reset_turn_route_facts
        from .time_windows import reset_turn_windows
        from .chart_intent import reset_turn_intent
        from .identifier_provenance import begin_unanswered_record, reset_turn_facts
        _delegated_subject_var.set(_pop_delegated_subject(payload))
        _retry_question_var.set(_pop_retry_question(payload))
        reset_turn_notes()  # this turn's route seed records its own
        reset_turn_route_facts()  # and its own route facts
        reset_turn_windows()  # and this turn's prelude its own windows
        reset_turn_intent()  # and its own chart intent
        reset_turn_facts()  # and its own identifier-provenance graph facts
        unanswered = begin_unanswered_record()  # logged once when the turn ends
        _reset_graph_turn_budget()  # one explore_graph budget for the whole turn
        # ADR-0047: the ADR-0046 integrity runtime is opt-in — Mode A alone is
        # not enough; RESPONSE_INTEGRITY_MODE=enforce must also be set (the
        # default report-only keeps the legacy answer flow). One decision,
        # shared with the chat streams and the session reload.
        integrity = mode_a_integrity_enforced()
        implementation = _invoke_integrity_runtime if integrity else legacy
        args = (payload, context, build_orchestrator) if integrity else (payload, context)
        # Keepalive: wrap the inner stream (both paths) so an idle orchestrator
        # — e.g. parked on a slow MCP tool result — emits a heartbeat frame and
        # the app's 300 s socket read never times out mid-answer. The wrapper
        # is the sanctioned outside-the-frozen-loop seam; it passes every inner
        # event through unchanged and in order.
        try:
            async for event in _with_stream_heartbeat(implementation(*args)):
                yield event
        finally:
            unanswered.log()
        if not integrity:
            # A-lite integrity marker (additive, trailing): lets stream
            # consumers (the smoke_prompts scorecard) distinguish "integrity
            # enforcement not enabled on this env" from "integrity failed".
            # Appended HERE — the sanctioned outside-the-frozen-loop seam —
            # after the legacy generator completes, so the gate-frozen legacy
            # invoke body stays byte-identical. The enforce path instead
            # carries ``mode`` inside final_events' integrity_metadata.
            yield {"response_integrity": {"mode": response_integrity_mode()}}
    return _answer_warmup_or(selected)


def _run_orchestrator_integrity():
    from bedrock_agentcore import BedrockAgentCoreApp
    from . import orchestrator as _orchestrator_module
    original = BedrockAgentCoreApp.entrypoint
    original_build = _orchestrator_module.build_orchestrator
    # Deterministic chart injection rides a build_orchestrator wrapper (every
    # Agent's stream drains ChartRedactionHook's queue) — installed here, the
    # same outside-the-legacy-loop seam as the entrypoint interception below,
    # so the gate-frozen legacy invoke body stays byte-identical.
    build_orchestrator = _chart_injecting_build(original_build)

    def register(instance, function):
        return original(instance, _runtime_entrypoint(function, build_orchestrator))

    BedrockAgentCoreApp.entrypoint = register
    _orchestrator_module.build_orchestrator = build_orchestrator
    try:
        return _run_orchestrator_base()
    finally:
        BedrockAgentCoreApp.entrypoint = original
        _orchestrator_module.build_orchestrator = original_build


_run_orchestrator = _run_orchestrator_integrity


if __name__ == "__main__":
    # Dispatch on SYSTEM_ID (the existing axis): unset → orchestrator; the reserved
    # id `digital-thread` → the knowledge-graph tool server; the reserved id
    # `onboarding` → the onboarding-tools server (both fixed/built-in systems); any
    # other id → a per-system agent. Using the reserved SYSTEM_ID (vs a separate
    # AGENT_ROLE) means the gateway target name and the `<id>___<tool>` prefixes
    # derive automatically from SYSTEM_ID, and the existing agent_manager
    # provisioning/sync path is reused unchanged.
    _system_id = os.environ.get("SYSTEM_ID")
    if not _system_id:
        _assert_runtime_env("orchestrator")  # fail CLOSED at boot (ADR-0069)
        _run_orchestrator()
    else:
        from .digital_thread_server import TARGET_NAME as _DIGITAL_THREAD_ID
        from .onboarding_runtime_server import TARGET_NAME as _ONBOARDING_ID
        from .query_bridge_server import TARGET_NAME as _QUERY_BRIDGE_ID
        if _system_id == _DIGITAL_THREAD_ID:
            _assert_runtime_env("system")  # contract role for adt_* runtimes
            _run_digital_thread()
        elif _system_id == _ONBOARDING_ID:
            # Validated inside create_onboarding_server at construction.
            _run_onboarding()
        elif _system_id == _QUERY_BRIDGE_ID:
            # No graph tools; needs only ADT_APP_URL and the x-quick-auth
            # token (QUICK_GATEWAY_TOKEN_REF, or the plaintext env);
            # tool calls report a missing app URL honestly (see _run_query_bridge).
            _run_query_bridge()
        else:
            _assert_runtime_env("system")  # fail CLOSED at boot (ADR-0069)
            _run_system_agent()
