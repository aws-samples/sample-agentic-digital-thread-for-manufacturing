# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Slim, dependency-free connection-type constants.

Extracted from :mod:`agents.digital_thread_server` so the small pieces of code
that only need the KnowledgeGraph connection-type *string* (the provisioner
handlers + the shared runtime-provision helpers) can import it
WITHOUT dragging in ``digital_thread_server``'s heavy transitive imports
(``mcp.server.fastmcp`` / Strands / the graph tools).

This module intentionally imports nothing beyond the standard library, so the
provisioner handlers stay importable without the MCP/agent stack.
``digital_thread_server`` re-exports :data:`KNOWLEDGE_GRAPH_CONNECTION_TYPE` as
``CONNECTION_TYPE`` so the value stays single-sourced here.
"""
from __future__ import annotations

import re

#: The one rule an Athena caller is told about partition (storage) columns.
#: Lives in this stdlib-only leaf so every prompt seam can use it, including
#: the import-pure ones; ``storage_bound_rule.PARTITION_RULE`` is this string.
#: Plain, no dashes.
ATHENA_PARTITION_RULE = (
    "Bound the time column the question asks about. Bound a partition column only when "
    "the question asks for that column's range: it drops every row outside it and changes "
    "the result. A COUNT, SUM, MIN or MAX over the scan limit is computed exactly by the "
    "tool, so a total never needs a partition bound. The tool may decline when too little "
    "time is left for the pieces; then run a narrower query.")

# Connection type for the digital-thread system. Unlike PostgreSQL / SiteWise /
# MCP Server systems, a KnowledgeGraph system's connection is the Neptune graph
# itself, reached via the runtime IAM role + GRAPH_IDENTIFIER — there is no
# credentialed DB connection, so no connection secret is stored in Secrets
# Manager. agent_manager keys its graph-connection provisioning branch on this
# value.
KNOWLEDGE_GRAPH_CONNECTION_TYPE = "KnowledgeGraph"

# Connection type for the built-in `query-bridge` Quick-integration target
# (agents/query_bridge_server.py). Like KnowledgeGraph it has no credentialed
# DB connection — its "connection" is the ADT app itself, reached over the VPC
# via ADT_APP_URL with the QUICK_GATEWAY_TOKEN shared secret — so no connection
# secret is stored in Secrets Manager. runtime_connection_env keys its env
# branch on this value; provisioning is gated by quick_api_enabled.
QUERY_BRIDGE_CONNECTION_TYPE = "QueryBridge"

# ---------------------------------------------------------------------------
# Canonical connection-type names + classification tuples.
#
# The TUPLES below (``NON_SQL_CONNECTION_TYPES`` / ``DIRECT_SOURCE_CONNECTION_TYPES``)
# are the single source of truth for "which connection types take the non-default
# provision path" and "which need a direct-source agent runtime". onboard.py and
# server.py import from here, so adding a connection type updates one place.
#
# The scalar name constants are the canonical spelling of each type. New code
# (the Athena branches) uses them; the older equality checks in system_agent /
# onboard still use string literals and can be migrated to these incrementally
# (kept out of this change to limit blast radius on the stable dispatch paths).
# ---------------------------------------------------------------------------
POSTGRESQL = "PostgreSQL"
SITEWISE_DIRECT = "SiteWise (Direct)"
SITEWISE_I3X = "SiteWise (i3X)"
REST_API = "REST API"
MCP_SERVER = "MCP Server"
ATHENA = "Athena"
# S3 Tables read through embedded DuckDB (s3tables_core), the in-process sibling
# of ATHENA over the SAME Iceberg data (ADR-0099). Like Athena it is SQL-dialect
# and SECRETLESS: its connection is a boto3 target (an S3 Tables bucket ARN read
# with the task role via DuckDB's credential_chain), no DSN and no pasted secret,
# so it follows the Athena node-config-carrying provision path. Additive: it
# replaces no existing type.
S3TABLES_DUCKDB = "S3 Tables"
# Tulip Manufacturing (MES) via the Tulip Tables REST API. A FIRST-CLASS
# connection type (mirrors SITEWISE_I3X), NOT a `*.tulip.co` host pattern on a
# generic REST_API system: Tulip publishes no consolidated OpenAPI spec and can
# be self-hosted / proxied on any internal DNS name, so identity is DECLARED by
# the user (this type string), never sniffed from the URL. The rest_vendor_registry
# tulip adapter detects on this string; the URL is only where to reach it.
# Promote Tulip to a first-class connection type.
TULIP = "Tulip (Tables API)"

# Connection types that are NOT the default relational-PostgreSQL provision path.
# Each of these routes its OWN ``write_connection`` in the provision-confirm step
# (rather than falling through to the canonical PG "System node" block), and its
# discover step uses a non-PostgreSQL backend. Athena is SQL-dialect but is grouped
# here because its connection is a boto3 workgroup/catalog target (no DSN / no
# ``search_path`` credentialed DB connection), so it follows the same
# node-config-carrying provision path as the other non-PG types.
NON_SQL_CONNECTION_TYPES = (
    MCP_SERVER,
    REST_API,
    SITEWISE_I3X,
    SITEWISE_DIRECT,
    ATHENA,
    S3TABLES_DUCKDB,  # boto3 bucket target, no DSN — same node-config path as Athena
    TULIP,  # Tulip is a first-class non-SQL connection type
)

# Connection types whose onboarded System node needs a per-system agent runtime
# that reaches the source directly (boto3 / httpx / MCP tools) rather than the
# shared SQL query path. Used by the server's agent-restore sweep and the query
# router to know a dedicated agent must be (re)started for the system.
DIRECT_SOURCE_CONNECTION_TYPES = (
    MCP_SERVER,
    SITEWISE_DIRECT,
    SITEWISE_I3X,
    REST_API,
    ATHENA,
    S3TABLES_DUCKDB,  # per-system agent runs DuckDB in-process against the bucket
    TULIP,  # Tulip needs a direct-source per-system agent runtime
)

# Connection types whose Connect New System form offers a SECOND credential
# source besides a pasted key: naming a Secrets Manager secret by ARN. The
# shared ``connection_pinning`` layer enforces the allowlist and the
# secret-wins pinning of the fields that decide where the key is sent
# (per type: ``connection_pinning.pinned_fields``); a type opts in here. Tulip
# (its key reaches a whole Tulip account, so an attendee must not be able to
# send it to a URL or workspace of their own). The ARN option is still gated at runtime by the
# per-deploy allowlist (empty ⇒ hidden and refused).
SECRET_ARN_CREDENTIAL_TYPES = (
    TULIP,
)


def secret_arn_source_supported(connection_type: str) -> bool:
    """True when ``connection_type`` offers the Secrets Manager ARN credential
    source (see :data:`SECRET_ARN_CREDENTIAL_TYPES`). Whether it is OFFERED to a
    given deploy also depends on the allowlist (``connection_pinning``)."""
    return (connection_type or "") in SECRET_ARN_CREDENTIAL_TYPES

# Whether the deterministic readers (the dashboard's section renderer and the
# metric resolver's fan-out) can read a system. Both compose SQL, with native
# branches only for Athena and SiteWise; a REST source is answered only by its
# per-system agent at chat time. Before this rule each such system showed on the
# dashboard as a failed site on every metric card and as one "Section
# unavailable" row per table. The REST vendor registry owns the answer
# (``RestVendorAdapter.structured_reader``), so a new vendor needs no entry
# here. Non-REST types (PostgreSQL, Athena, SiteWise Direct, MCP) are unchanged;
# MCP keeps its own query path (``query_router.query_system``).
#: Connection types the dashboard SECTION renderer (``entity_render``) has no
#: reader for. The metric layer reads S3 Tables through its DuckDB dialect (the
#: metric resolver, dimension, heatmap and card paths), but the section renderer
#: composes only PostgreSQL / Athena SELECTs, so an S3 Tables system is left off
#: the dashboard sections rather than rendered as a failed row per table.
_NO_SECTION_READER_TYPES = frozenset({S3TABLES_DUCKDB})

#: Connection types the metric layer reads with composed SQL: one aggregate
#: SELECT per metric, in the dialect of the backend. The single "SQL backend"
#: answer for predicates, windows, duration percentiles, grouped reads,
#: heatmaps and live cards.
METRIC_SQL_CONNECTION_TYPES = (POSTGRESQL, ATHENA, S3TABLES_DUCKDB)


def structured_read_supported(system: dict) -> bool:
    """True when the metric fan-outs can read ``system``.

    A missing ``connection_type`` is PostgreSQL (the catalog default)."""
    conn = (system or {}).get("connection_type") or POSTGRESQL
    from .rest_vendor_registry import resolve_vendor  # lazy: the registry imports this module
    adapter = resolve_vendor(conn)
    return adapter is None or adapter.structured_reader


def section_read_supported(system: dict) -> bool:
    """True when the dashboard section renderer can read ``system``: every
    system the metric fan-outs read, except the types in
    :data:`_NO_SECTION_READER_TYPES`."""
    conn = (system or {}).get("connection_type") or POSTGRESQL
    return conn not in _NO_SECTION_READER_TYPES and structured_read_supported(system)


# ---------------------------------------------------------------------------
# system_id -> Bedrock/Nova tool-name safety.
#
# Every onboarded system's tools are exposed to the orchestrator model through
# the AgentCore gateway with a per-system prefix: ``<system_id>___<tool>`` (see
# ``runtime_app.py`` which splits the incoming tool name on ``'___'``). Amazon
# Bedrock Converse requires every ``toolSpec.name`` to match
# ``^[a-zA-Z0-9_-]{1,64}$``, and Strands does NOT sanitize tool names — so a
# system_id that contains a space, dot, slash, or other character would mint an
# invalid tool name and Bedrock would raise a ValidationException at QUERY time
# (not at onboard time), which is hard to diagnose. Today's ids happen to be
# slug-safe, so this is latent; ``sanitize_system_id`` closes the gap at the
# registration boundary.
#
# LENGTH BUDGET (why the id is capped, and why the cap is 24, not 40):
# the longest per-system tool STEM embeds the (underscored) id a SECOND time —
# the SQL query tool is exposed as ``execute_sql_<id>`` and the gateway prefixes
# it, giving the worst-case Bedrock tool name ``<id>___execute_sql_<id>`` whose
# length is ``2*len(id) + len('___execute_sql_')``. Requiring that to stay <= 64
# gives ``2L + 15 <= 64`` -> ``L <= 24``. A larger cap (e.g. 40) would let a
# long custom id overflow the 64-char limit through the doubled-id stem.
_MAX_TOOL_NAME_LEN = 64          # Bedrock Converse toolSpec.name: ^[a-zA-Z0-9_-]{1,64}$
_GATEWAY_SEP = "___"             # per-system tool prefix separator (runtime_app split)
_LONGEST_TOOL_STEM = "execute_sql_"  # longest fixed stem that ALSO embeds the id
MAX_SYSTEM_ID_LEN = (_MAX_TOOL_NAME_LEN - len(_GATEWAY_SEP) - len(_LONGEST_TOOL_STEM)) // 2  # == 24

_SAFE_ID_CHARS_RE = re.compile(r"[^a-z0-9_-]+")   # runs of unsafe chars
_SEP_RUN_RE = re.compile(r"[-_]{2,}")             # collapse ANY run of separators
_SAFE_ID_RE = re.compile(r"[a-z0-9_-]+")          # final validation (fullmatch)


def sanitize_system_id(raw: str) -> str:
    """Slugify a raw system_id into a value safe as a Bedrock tool-name prefix.

    Applied at the system-REGISTRATION boundary (``connect_new_system`` / the
    ``/api/onboard/connect`` route) so only tool-name-safe ids are ever
    persisted. This is *slugify-with-validation*, NOT reject-only: the input is
    normalized (lowercased, unsafe chars -> ``-``, separator runs collapsed,
    ends trimmed, length capped to :data:`MAX_SYSTEM_ID_LEN`), then the RESULT
    is asserted to match ``[a-z0-9_-]+`` — a raw that reduces to nothing
    (e.g. ``""``, ``"///"``, all-unicode) still raises ``ValueError`` because it
    cannot yield a valid tool name.

    Already-valid ids pass through UNCHANGED (e.g. ``'erp'``, ``'mes-mil'``,
    ``'ags-segments'``, ``'mes_mil'``), so this never rewrites existing systems.

    Raises:
        ValueError: if ``raw`` is falsy/blank or cannot be reduced to a
            non-empty ``[a-z0-9_-]`` slug.
    """
    if not raw or not raw.strip():
        raise ValueError(f"Cannot sanitize empty system_id: {raw!r}")
    s = _SAFE_ID_CHARS_RE.sub("-", raw.strip().lower())  # unsafe runs -> single '-'
    s = _SEP_RUN_RE.sub("-", s)                          # collapse ANY separator run
    s = s.strip("-_")                                    # trim leading/trailing seps
    if len(s) > MAX_SYSTEM_ID_LEN:
        s = s[:MAX_SYSTEM_ID_LEN].rstrip("-_")
    if not _SAFE_ID_RE.fullmatch(s):
        raise ValueError(
            f"system_id {raw!r} could not be sanitized to a valid tool-name "
            f"prefix (reduced to {s!r}); it must contain at least one "
            f"[a-z0-9_-] character"
        )
    return s


# ---------------------------------------------------------------------------
# Per-connection-TYPE default query guidance + capabilities (base-mode generic).
#
# In BASE mode the live catalog is EMPTY (ADR-0030), so a REAL system onboarded
# through ``/api/onboard/connect`` carries no hardcoded demo ``query_guidance``.
# The orchestrator reads ``query_guidance``/``capabilities`` off the Neptune
# System node (orchestrator.py Step 1), so provision must persist SOMETHING
# useful. These GENERIC, per-TYPE defaults are the fallback the provision
# node-write applies when a registration did not supply explicit guidance —
# giving e.g. a SiteWise-Direct or Athena source correct usage guidance
# (sequential list-then-describe; never fabricate asset ids) even though it was
# registered generically via /connect, not from the demo catalog.
#
# GENERIC ONLY — no dataset/demo literals (no site codes, no demo hierarchy
# names, no hardcoded system ids). A real system onboarded via /connect can
# also supply its own richer, hand-tuned ``query_guidance`` as registration
# input, which takes precedence over these per-type defaults.
# ---------------------------------------------------------------------------
# Shared SiteWise workflow policy. This module is copied standalone into the
# minimal onboarding provisioner image, so the policy intentionally lives here
# rather than in another local module.
# The compact form keeps a many-asset read short: the tool expands the asset x
# property cross product, so the agent never has to type one id pair per entry
# (typing them ran a system agent past its own output limit).
_SITEWISE_COMPACT_READ = (
    " When the assets share a model, make that read with asset_ids_json (the "
    "asset ids) + property_ids_json (the model's property ids) instead of one "
    "entries_json pair per asset."
)
_SITEWISE_SEEDED_READ = (
    "Call sitewise_list_assets(asset_model_id=<supplied model_id>) at most once "
    "per model needed, then perform one logical batch read: "
    "sitewise_batch_get_property_values for current values or "
    "sitewise_get_asset_property_history for history. Skip root-asset traversal "
    "and do not describe each asset; an asset uses its model's property ids."
    + _SITEWISE_COMPACT_READ
)
_SITEWISE_SEEDED_POLICY = (
    "Use the supplied model_id and property_id values exactly. Do NOT call "
    "sitewise_list_asset_models or sitewise_describe_asset_model to re-discover "
    "the supplied schema. " + _SITEWISE_SEEDED_READ
)
_SITEWISE_UNSEEDED_POLICY = (
    "No onboarded schema is available. Perform bounded discovery once: call "
    "sitewise_list_asset_models once, describe only the model(s) needed to get "
    "property ids, call sitewise_list_assets once per needed model, then perform "
    "one logical batch read with sitewise_batch_get_property_values for current "
    "values or sitewise_get_asset_property_history for history. Do not crawl "
    "root assets or describe every asset." + _SITEWISE_COMPACT_READ
)
_SITEWISE_RECOVERY_POLICY = (
    "After a concrete failed exact read, re-introspect the affected model once "
    "and retry one corrected exact read. TRAVERSAL BUDGET: attempt any failing "
    "recovery drill-down step at most twice total. If a targeted "
    "sitewise_describe_asset lookup fails, list assets per asset model and filter "
    "by name/path once; do not keep retrying rephrased variants. Stop after the "
    "corrected retry and report any remaining gap. Never invent or fabricate an "
    "asset, model, or property id. "
    "To answer which assets are UNDER a site/area/line, or which site/area/line "
    "a machine belongs to (model lists cannot — the same machine name repeats "
    "across sites), call sitewise_list_associated_assets(asset_id, "
    "direction=CHILD for descendants or PARENT for the parent) on a real asset "
    "id from a list/describe; its path field names the site/area/line of each row, "
    "and its default depth reaches equipment under a site."
)


_SITEWISE_PARTIAL_SEED_POLICY = (
    "Use the supplied model_id and property_id values exactly, and do not "
    "re-describe a model listed with its property ids. The supplied list is "
    "condensed: for a model listed without property ids, call "
    "sitewise_describe_asset_model once for its model_id to get them. "
    + _SITEWISE_SEEDED_READ + " A model, property or asset missing from the "
    "list is NOT evidence that it does not exist: call "
    "sitewise_list_asset_models once before reporting one absent."
)


def sitewise_prompt_policy(has_schema: bool, partial: bool = False) -> str:
    """Final-prompt policy for a known schema-seed state. ``partial`` (a
    condensed or truncated seed) REPLACES the seeded policy, whose no-describe
    rule would contradict the targeted describe and the absence check; a
    complete seed's policy is unchanged."""
    selected = _SITEWISE_SEEDED_POLICY if has_schema else _SITEWISE_UNSEEDED_POLICY
    if has_schema and partial:
        selected = _SITEWISE_PARTIAL_SEED_POLICY
    return f"{selected} {_SITEWISE_RECOVERY_POLICY}"


def sitewise_dual_path_guidance() -> str:
    """Persisted guidance for runtimes whose seed availability is not yet known."""
    return (
        f"When an onboarded schema is present: {_SITEWISE_SEEDED_POLICY} "
        f"When no onboarded schema is present: {_SITEWISE_UNSEEDED_POLICY} "
        f"{_SITEWISE_RECOVERY_POLICY}"
    )


# SiteWise (Direct) — prefer the onboarded model/property seed. Runtime
# introspection is a bounded recovery path after a concrete failed exact read.
_SITEWISE_DIRECT_GUIDANCE = (
    "AWS IoT SiteWise (Direct) — " + sitewise_dual_path_guidance()
)
_SITEWISE_DIRECT_CAPABILITIES = (
    "Historical time-series OT/telemetry via the direct AWS IoT SiteWise API. "
    "Assets are organized in a model/hierarchy; use for sensor readings, "
    "equipment KPIs, and any time-range or trend query. Also the authority for "
    "discrete equipment EVENTS when such models are onboarded: machine state "
    "changes, alarm events, and downtime/stoppage reason codes — so route "
    "cause-of-loss and downtime questions here, not only production counts."
)

# SiteWise (i3X) — the data model is OBJECTS and the knowledge graph already
# holds the Asset/Property spine from onboarding. This guidance is read by the
# ORCHESTRATOR, which reaches this system ONLY through its query tool (Mode A
# query_<id> / Mode B run_query) — the one-call sitewise_* telemetry tools
# live on the system's OWN agent, so the guidance steers WHAT to
# delegate, never which sitewise_* tool to call. Wording stays GENERIC (no
# dataset/demo literals).
_SITEWISE_I3X_GUIDANCE = (
    "AWS IoT SiteWise via the i3X REST API — the data model is OBJECTS "
    "(GET /v1/objects), not assets. Do NOT traverse the object hierarchy "
    "manually: the knowledge graph already holds every Asset and Property "
    "node from onboarding — resolve names, hierarchy paths and property_count "
    "via the graph first. This system's one-call telemetry tools (chart / "
    "named-asset telemetry / multi-id batch history) live on ITS OWN agent, "
    "not in your toolset: delegate ONE self-contained sub-question through "
    "its query tool instead of calling sitewise_* tools yourself. Make the "
    "sub-question complete: name the asset (a descriptive phrase is fine — "
    "the system fuzzy-resolves it and returns a candidates list when "
    "ambiguous; re-delegate ONCE with the exact name), ALWAYS state the time "
    "window explicitly (start_time/end_time are always required — history has "
    "no default window), and say when a chart is wanted (it renders "
    "ready-to-display charts for ALL of an asset's channels in one call). Ask "
    "for everything you need from this system in ONE sub-question — its "
    "batched tools do in one round trip what N narrow sub-questions cost "
    "serially. Never request history for an Asset whose property_count is 0 "
    "(structural-only). Never call this system in parallel with itself — "
    "issue its calls serially. NEVER fabricate an elementId — resolve it from "
    "the graph, or ask the system to list its objects."
)
_SITEWISE_I3X_CAPABILITIES = (
    "Historical time-series OT/telemetry via the i3X REST API. Traverse the "
    "object hierarchy top-down; use for sensor readings and time-range queries."
)

# Athena — SQL over a boto3 workgroup/catalog. Athena exposes its OWN tools
# (athena_list_tables / athena_describe_table / athena_run_query) — NOT the
# generic ``execute_sql_<id>`` tool. The orchestrator's global GATEWAY TOOL NAMING
# rule points SQL systems at ``<prefix>___execute_sql_<prefix>``, which does NOT
# exist for an Athena system (the gateway rejects it as an unknown tool). This
# per-system guidance therefore names the ACTUAL gateway-prefixed Athena tools so
# the orchestrator calls the right ones. list-then-describe; exact identifiers
# only; read-only SELECT.
_ATHENA_GUIDANCE = (
    "Amazon Athena (SQL federation) — this system exposes ATHENA-SPECIFIC tools, "
    "NOT the generic execute_sql tool. Do NOT call "
    "<tool_prefix>___execute_sql_<tool_prefix> for this system — it does not exist "
    "here and the gateway will reject it as an unknown tool. Call "
    "<tool_prefix>___athena_list_tables then <tool_prefix>___athena_describe_table "
    "first for the exact table and column names; NEVER guess an identifier. If a "
    "table or column you expect is not listed, call "
    "<tool_prefix>___athena_describe_table for that table before querying — do NOT "
    "guess a column name (guessing causes COLUMN_NOT_FOUND). If a query DOES fail "
    "with COLUMN_NOT_FOUND, do NOT guess another name — re-run "
    "<tool_prefix>___athena_describe_table for that table, take the REAL column "
    "names from the result, and correct the query. Then use "
    "the gateway-prefixed Athena tools for THIS system: "
    "<tool_prefix>___athena_list_tables (list tables), "
    "<tool_prefix>___athena_describe_table (columns for one table), and "
    "<tool_prefix>___athena_run_query (run a read-only SELECT). Read-only SELECT "
    "only; qualify with the catalog/database. Use the EXACT column "
    "names returned by the schema — different sources model the same concept under "
    "different names. ALWAYS bound scan cost — Athena is COLUMNAR and bills by "
    "bytes scanned, so a query reads every column it SELECTs across every row it "
    "does not prune: (a) SELECT ONLY the specific columns you need and NEVER "
    "SELECT * — on a WIDE table (many columns) SELECT * reads every column and "
    "alone can exceed the workgroup's bytes-scanned limit, so name each column "
    "explicitly; (b) bound the rows with a WHERE filter: " + ATHENA_PARTITION_RULE + " "
    "(c) put a LIMIT on reads that return rows. An unbounded or SELECT * scan can "
    "exceed the workgroup's bytes-scanned limit and be rejected. When a table exposes "
    "partition columns (<tool_prefix>___athena_describe_table flags them), they are "
    "storage columns: a bound on one the question does not ask about leaves rows out "
    "of the result. When comparing "
    "or joining columns of different types, CAST both operands to a common type (a "
    "type mismatch otherwise errors with TYPE_MISMATCH). <tool_prefix>___athena_describe_table reports each "
    "column's DATA TYPE — use it: when you compare a timestamp/date column to a string "
    "literal (e.g. a 'YYYY-MM-DD' value), CAST so the operand types match — CAST the "
    "column with CAST(col AS date), or parse the literal with date_parse / "
    "CAST(literal AS timestamp) — never compare a temporal column to a raw string "
    "literal (that errors with TYPE_MISMATCH). Prefer SIMPLE single-level "
    "queries; if you must use a "
    "subquery, PROJECT (SELECT) in the subquery every column you reference in the "
    "outer scope — never reference in an outer scope a column the subquery did not "
    "select (that is a common COLUMN_NOT_FOUND cause)."
)
_ATHENA_CAPABILITIES = (
    "SQL query access over an Amazon Athena workgroup/catalog (federated data "
    "lake / external tables). Use for tabular, historical, and analytical queries."
)

# S3 Tables — the in-process DuckDB reader over an S3 Tables (Iceberg)
# bucket (ADR-0099). SQL like Athena, but its own tools and DuckDB dialect, and
# NO per-query scan cap. Names the gateway-prefixed s3tables_* tools.
_S3TABLES_DUCKDB_GUIDANCE = (
    "S3 Tables: this system exposes S3TABLES-SPECIFIC tools, NOT the "
    "generic execute_sql tool. Do NOT call "
    "<tool_prefix>___execute_sql_<tool_prefix> for this system; it does not exist "
    "here and the gateway will reject it as an unknown tool. Call "
    "<tool_prefix>___s3tables_list_tables then <tool_prefix>___s3tables_describe_table "
    "first for the exact namespace.table and column names; NEVER guess an identifier. "
    "If a table or column you expect is not listed, call "
    "<tool_prefix>___s3tables_describe_table for that table before querying; do NOT "
    "guess a column name. If a query DOES fail with a binder/column error, do NOT guess "
    "another name: re-run <tool_prefix>___s3tables_describe_table for that table, take "
    "the REAL column names from the result, and correct the query. Then use the "
    "gateway-prefixed S3 Tables tools for THIS system: "
    "<tool_prefix>___s3tables_list_tables (list tables), "
    "<tool_prefix>___s3tables_describe_table (columns for one table), and "
    "<tool_prefix>___s3tables_run_query (run a read-only DuckDB SELECT). Read-only "
    "SELECT/WITH only; reference tables as s3t.<namespace>.<table> (three parts). Use "
    "the EXACT column names the describe returns; different sources model the same "
    "concept under different names. This is DuckDB, so write DuckDB dialect: a column "
    "whose name CONTAINS DOTS is ONE physical column, so reference it as a single "
    'double-quoted identifier (SELECT "a.b.c"), never bare (bare a.b.c fails with '
    'Binder Error: Referenced table "a.b" not found) and never with the dots rewritten '
    "as underscores. When comparing a TIMESTAMP/DATE column to a literal, type the "
    "literal (TIMESTAMP '2026-01-31 00:00:00', DATE '2026-01-31') or parse it "
    "(strptime(s, '%Y-%m-%d'), TRY_CAST(s AS TIMESTAMP)); never compare a temporal "
    "column to a bare string, and use TRY_CAST (not Trino's try/try_cast spelling "
    "differences) when a cast may fail. For a day difference use epoch differences "
    "(epoch(a) - epoch(b), in seconds) rather than assuming date_diff's unit. For "
    "percentiles use quantile_disc / quantile_cont, not approx_percentile. Prefer SIMPLE "
    "single-level queries; if you must use a subquery, PROJECT (SELECT) in the subquery "
    "every column you reference in the outer scope. When a table exposes partition "
    "columns (<tool_prefix>___s3tables_describe_table flags them), they are storage "
    "columns: a bound on one the question does not ask about leaves rows out of the "
    "result, so bound the time column the question asks about, not a partition column "
    "it does not. Results are row-capped, but a COUNT/SUM/MIN/MAX aggregate is computed "
    "exactly by the tool. There is no per-query bytes-scanned cap, so you need not "
    "minimise selected columns for cost; still SELECT only what you need for clarity. "
    "Only this system's own tables are reachable: file, URL, and other-catalog reads are "
    "refused."
)
_S3TABLES_DUCKDB_CAPABILITIES = (
    "SQL query access over an Amazon S3 Tables (Apache Iceberg) bucket through "
    "embedded DuckDB: tabular, historical, and analytical queries with no "
    "per-query scan cap."
)

# PostgreSQL — the generic relational default (demo PG systems carry richer,
# per-schema guidance in the dormant seed data).
_POSTGRESQL_GUIDANCE = (
    "PostgreSQL (relational) — discover the schema FIRST by querying "
    "information_schema.tables / information_schema.columns, and use the EXACT "
    "table and column names it returns; NEVER assume a column exists. Read-only "
    "SELECT only. Different systems model the same concept under different names, "
    "so rely only on this system's schema and query_guidance."
)
_POSTGRESQL_CAPABILITIES = (
    "Relational SQL data via a read-only PostgreSQL connection."
)

# MCP Server — call the exposed MCP tool as its guidance/toolset describes.
_MCP_SERVER_GUIDANCE = (
    "MCP Server — call the tool exactly as it appears in your available toolset; "
    "do NOT invent a tool name. Follow the tool's own argument contract."
)
_MCP_SERVER_CAPABILITIES = (
    "Live data via a Model Context Protocol (MCP) server's exposed tools."
)

# REST API — generic OpenAPI-described HTTP source queried GET-only (read-only).
# GENERIC (no vendor/demo literals): a specific source can supply richer,
# hand-tuned query_guidance at registration, which takes precedence.
_REST_API_GUIDANCE = (
    "REST API (OpenAPI) — this system is queried GET-only and read-only: pass a "
    "relative GET path (with query parameters) as the request; never assume a "
    "write/POST is available. Use the EXACT resource paths and field names from "
    "the discovered schema; NEVER invent a path or field. Prefer server-side "
    "filtering and always bound result size with the API's paging/limit "
    "parameters (results are byte-capped). If the API is workspace- or "
    "tenant-scoped, include the required scope/prefix in the path or headers as "
    "the source's guidance specifies."
)
_REST_API_CAPABILITIES = (
    "Read-only data via a generic REST API (OpenAPI-described endpoints, GET)."
)

# Tulip (Tables API) — NOT the generic OpenAPI REST guidance. Tulip exposes its
# business data as Tulip Tables reached through a fixed three-endpoint tool set
# (tulip_list_tables / tulip_get_table_columns / tulip_query_records), NOT
# arbitrary OpenAPI GET paths — so it gets its OWN guidance rather than
# inheriting _REST_API_GUIDANCE (whose "relative GET path" advice does not apply
# to the Tables API and is contradictory here). GENERIC: no tenant/table
# literals; the provisioned agent additionally receives the discovered "known
# business tables" (per-provision) in its prompt.
# Tulip gets its own query guidance/capabilities, not inherited REST guidance.
_TULIP_GUIDANCE = (
    "Tulip Manufacturing (MES) via the Tulip Tables REST API. This system "
    "exposes TULIP-SPECIFIC tools, NOT the generic execute_sql or OpenAPI GET "
    "path tools, and it is NOT i3X — there are no /namespaces, /objecttypes, or "
    "/objects endpoints. When the agent already knows a table's id and columns "
    "(its provisioned description lists the known business tables with their ids "
    "and columns), query records DIRECTLY with tulip_query_records(table_id, "
    "limit, filters) — do NOT re-list tables or re-read columns first. Call "
    "tulip_list_tables only to find a table whose id you do not already know, "
    "and tulip_get_table_columns(table_id) only for a table whose columns you do "
    "not know or when a query fails on an unexpected column — NEVER guess a "
    "table id or column name. Read "
    "rows with tulip_query_records(table_id, limit, filters): always bound "
    "result size with limit (1-100) and prefer server-side filtering via the "
    "Tulip filter grammar (a JSON array of {field, functionType, arg}) rather "
    "than pulling whole tables. Read-only. "
    # Static: a scoped connection's agent reports a table outside its scope as
    # out of scope; the orchestrator keeps that and never fills the gap.
    "When the Tulip system agent reports that a table is out of this "
    "connection's scope, keep that wording: do not restate it as missing or "
    "nonexistent, and do not answer with another table's rows in its place."
)
_TULIP_CAPABILITIES = (
    "Business/production data (work orders, defects, machine logs, and other "
    "operator-app records) via the Tulip Tables REST API. Use for tabular "
    "queries against a Tulip workspace's Tables."
)


DEFAULT_QUERY_GUIDANCE: dict[str, str] = {
    POSTGRESQL: _POSTGRESQL_GUIDANCE,
    SITEWISE_DIRECT: _SITEWISE_DIRECT_GUIDANCE,
    SITEWISE_I3X: _SITEWISE_I3X_GUIDANCE,
    MCP_SERVER: _MCP_SERVER_GUIDANCE,
    ATHENA: _ATHENA_GUIDANCE,
    S3TABLES_DUCKDB: _S3TABLES_DUCKDB_GUIDANCE,
    REST_API: _REST_API_GUIDANCE,
    TULIP: _TULIP_GUIDANCE,
}

DEFAULT_CAPABILITIES: dict[str, str] = {
    POSTGRESQL: _POSTGRESQL_CAPABILITIES,
    SITEWISE_DIRECT: _SITEWISE_DIRECT_CAPABILITIES,
    SITEWISE_I3X: _SITEWISE_I3X_CAPABILITIES,
    MCP_SERVER: _MCP_SERVER_CAPABILITIES,
    ATHENA: _ATHENA_CAPABILITIES,
    S3TABLES_DUCKDB: _S3TABLES_DUCKDB_CAPABILITIES,
    REST_API: _REST_API_CAPABILITIES,
    TULIP: _TULIP_CAPABILITIES,
}


def default_query_guidance(connection_type: str) -> str:
    """Generic default ``query_guidance`` for a connection TYPE (``""`` if unknown).

    Applied as the provision-time fallback when a registration supplied no
    explicit ``query_guidance`` — so a generically-onboarded system still carries
    correct, type-appropriate usage guidance on its System node.
    """
    return DEFAULT_QUERY_GUIDANCE.get(connection_type, "")


def default_capabilities(connection_type: str) -> str:
    """Generic default ``capabilities`` for a connection TYPE (``""`` if unknown)."""
    return DEFAULT_CAPABILITIES.get(connection_type, "")
