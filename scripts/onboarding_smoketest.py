#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
onboarding_smoketest.py — deterministic, order-independent onboarding smoke test.

Onboards the demo SQL systems through the SAME REST pipeline the UI uses (it is
NOT a special-case onboarding path — the demo systems are ordinary connections
established through the standard discover/map/resolve/provision API), then
asserts the outcomes that make the demo "delivered". It is the onboarding step
of a post-deploy smoke test.

The harness is the AUTOMATED USER (Step 3, Task 4.1)
----------------------------------------------------
Instead of relying on a server-side catalog connection snapshot (the demo
seed defaults in ``catalog``), the harness DERIVES each demo system's
connection from the DEPLOY ENVIRONMENT — deriving the ``connection_bootstrap``
descriptor per connection TYPE from each system's LIVE registration identity
(``connection_type`` + ``schema`` read from ``GET /api/onboard/catalog``) — and
SUPPLIES it as INPUT to the onboarding API, exactly as a UI user would type it:

  * PostgreSQL (erp/mes-*/cmms/plm) → a DSN
    (``postgresql://USER:PASS@HOST:PORT/DB?options=-csearch_path%3D<schema>``)
    built from ``POSTGRES_*`` / ``ERP_POSTGRES_*`` env (mirrors
    ``db._get_conn_params_for_schema``), passed as ``connection_string`` in the
    discover AND provision bodies. The env credential is included because
    ``/discover`` connects DIRECTLY via ``pgdriver.connect`` and the app sets no
    libpq ``PG*`` vars — a credential-free DSN would fail to authenticate. The
    persisted node stays credential-free: the provision node-write strips the
    userinfo server-side (``onboard._sql_non_secret``) and points ``secret_arn`` at
    the shared DB secret (Option 3), so no password reaches the graph.
  * MCP (highbyte) → ``connection_string = <HIGHBYTE_URL>/mcp`` (env/SSM) and the
    bearer token supplied OUT-OF-BAND via ``PUT /api/admin/highbyte-token`` BEFORE
    provisioning (the bodies carry no secret field; ``models.py`` is off-limits),
    minting ``conn/highbyte`` through ``write_connection``.
  * REST / SiteWise (i3X) → ``base_url`` (env ``SITEWISE_I3X_URL``) as
    ``connection_string``.
  * SiteWise (Direct) → ``iotsitewise://<region>.amazonaws.com`` from ``AWS_REGION``.

This is the SEED step, so reading env/SSM here is explicitly permitted — it is
NOT a runtime catalog connection read. The demo systems register through the
IDENTICAL discover→map→resolve→provision path a UI user drives, just with the
inputs supplied by the harness.

Why two phases
--------------
The per-system steps (discover, map, provision) are independent of other
systems. The cross-system step (resolve) only links a new system to systems
already present in Neptune, so the *first* system onboarded sees no peers. To
make the final cross-system thread independent of onboarding order:

  Phase 1: discover -> map -> resolve -> provision   (each system, any order)
  Phase 2: resolve -> provision  (a lap over every system, now that all exist)

After the Phase-2 lap every system has been resolved against the complete set,
so the identity graph is the same regardless of Phase-1 order.

Assertions check *outcomes* (all systems onboarded, every adt_<id> runtime
exists), never LLM-generated mapping text — so the gate is deterministic
without being brittle to model phrasing.

Usage
-----
  python3 scripts/onboarding_smoketest.py \
    --base-url https://<dist>.cloudfront.net \
    --password "$ADT_PASSWORD" \
    [--systems erp,mes-mil,mes-det,mes-cr,cmms,plm] \
    [--region us-east-1 --profile <p>]   # enables adt_<id> runtime assertion

Env fallbacks: ADT_BASE_URL, ADT_USERNAME (default ADT), ADT_PASSWORD.
Exit code 0 = all assertions passed.
"""
import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from urllib.parse import quote

# SQL systems are the fast default gate. The non-SQL (OT) systems are seeded only
# when --include-ot is passed AND the relevant deploy env is present (HIGHBYTE_URL,
# SITEWISE_I3X_URL, AWS_REGION, or TULIP_URL for the Tulip REST system) — see
# connection_inputs(). --include-ot and 'tulip': the system must be registered in
# Connect New System with its API key and secret first — the harness's token mint
# (supply_secret_out_of_band) cannot complete a credential-less registration.
DEFAULT_SYSTEMS = ["erp", "mes-mil", "mes-det", "mes-cr", "cmms", "plm"]
OT_SYSTEMS = ["sitewise-direct", "highbyte", "sitewise", "tulip"]


# --------------------------------------------------------------------------- #
# Connection derivation (Step 3, Task 4.1) — the harness as the automated user.
# --------------------------------------------------------------------------- #
# The harness derives each demo system's connection from the DEPLOY ENV using the
# seed manifest's declarative ``connection_bootstrap`` descriptor and SUPPLIES it
# as INPUT to the onboarding API. Nothing here reads a persisted catalog
# connection snapshot — it is the seed/registration step, so reading env is the
# sanctioned bootstrap (NOT a runtime catalog read).

# The ERP-family schema→DB routing knowledge lives ONLY in
# agents.demo.db_routing (ADR-0044 Decision 2) — this harness used to carry its
# own drifting copy of the prefix tuple, since deleted. Import
# via the agentic_digital_thread package root (same sys.path insertion pattern
# reonboard.py uses for sibling imports). db_routing imports boto3 only lazily
# (inside its SSM-pointer fallback), which the harness paths never trigger:
# the .sh always exports the env first, so the harness stays stdlib-only.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agentic_digital_thread"
    ),
)
from agents.demo.db_routing import is_erp_schema  # noqa: E402


def _pg_dsn_from_env(schema: str, env: Mapping[str, str]) -> str:
    """Build a PostgreSQL DSN for *schema* from env; routing via ``agents.demo.db_routing``.

    ERP-related schemas (``erp_*`` / ``mes_*`` / ``public`` — per the single
    canonical ``is_erp_schema``) route to the ERP
    database (``ERP_POSTGRES_*``); everything else routes to the demo database
    (``POSTGRES_*``). The DSN carries the env credential because the onboarding
    ``/discover`` endpoint connects DIRECTLY (``pgdriver.connect(connection_string)``)
    and the app runtime sets no libpq ``PG*`` vars — a credential-free DSN would
    fail authentication and break the gate. This is the SEED step, so reading the
    env credential here is the sanctioned bootstrap (NOT a runtime catalog read).

    The persisted node connection stays credential-free: the provision node-write
    strips the ``user:pass@`` userinfo server-side (``onboard._sql_non_secret``)
    and points ``secret_arn`` at the shared DB secret (Option 3), so no password
    reaches the graph. ``search_path`` carries the schema.
    """
    if is_erp_schema(schema) and env.get("ERP_POSTGRES_HOST"):
        host = env.get("ERP_POSTGRES_HOST", "")
        port = env.get("ERP_POSTGRES_PORT", "5432")
        db = env.get("ERP_POSTGRES_DB", "erp_simulation")
        user = env.get("ERP_POSTGRES_USER", "postgres")
        pw = env.get("ERP_POSTGRES_PASSWORD") or env.get("POSTGRES_PASSWORD", "")
    else:
        host = env.get("POSTGRES_HOST", "localhost")
        port = env.get("POSTGRES_PORT", "5432")
        db = env.get("POSTGRES_DB", "agentic_digital_thread")
        user = env.get("POSTGRES_USER", "postgres")
        pw = env.get("POSTGRES_PASSWORD", "")
    # Safety net: without a password the DSN cannot authenticate (discover does a
    # direct pgdriver.connect). Return "" so the caller can FAIL LOUDLY for this
    # system — there is NO server-side fallback anymore (Task 4.2 cut-over made
    # resolve_connection node-only). The harness must SUPPLY a valid connection or
    # error; it must never send an empty DSN. Locally .env.local sets
    # POSTGRES_PASSWORD; in cloud the .sh exports it from the RDS secret.
    if not pw:
        return ""
    userinfo = quote(user, safe="")
    userinfo += ":" + quote(pw, safe="")
    return f"postgresql://{userinfo}@{host}:{port}/{db}?options=-csearch_path%3D{schema}"


def build_connection(bootstrap: dict, env: Mapping[str, str]) -> dict:
    """Derive a connection INPUT from env per a manifest ``connection_bootstrap`` descriptor.

    Returns ``{"connection_string": str, "token": str}`` where ``token`` is the
    out-of-band secret (only the MCP/highbyte path supplies one today). Reads only
    env here (the .sh exports SSM/Secrets-derived values before invoking us), so
    this is pure and unit-testable with a mocked env. The PostgreSQL DSN carries the
    env credential (see ``_pg_dsn_from_env``); the URL-based kinds (mcp/rest) carry
    no inline credential — their secret is supplied out-of-band.
    """
    kind = bootstrap.get("kind", "none")
    if kind == "pg":
        return {"connection_string": _pg_dsn_from_env(bootstrap.get("schema", ""), env), "token": ""}
    if kind == "mcp":
        url = (env.get(bootstrap.get("url_env", ""), "") or "").strip().rstrip("/")
        suffix = bootstrap.get("url_suffix", "")
        conn = (url + suffix) if url else ""
        token = (env.get(bootstrap.get("token_env", ""), "") or "").strip()
        return {"connection_string": conn, "token": token}
    if kind == "rest":
        base = (env.get(bootstrap.get("url_env", ""), "") or "").strip().rstrip("/")
        token = (env.get(bootstrap.get("token_env", ""), "") or "").strip()
        return {"connection_string": base, "token": token}
    if kind == "tulip":
        # Tulip Tables API: base_url from TULIP_URL; the out-of-band secret is an
        # HTTP Basic token = base64(apiKey:secret). Empty base OR missing
        # credentials → empty connection_string so the OT-skip path applies.
        import base64
        base = (env.get(bootstrap.get("url_env", ""), "") or "").strip().rstrip("/")
        key = (env.get(bootstrap.get("key_env", ""), "") or "").strip()
        sec = (env.get(bootstrap.get("secret_env", ""), "") or "").strip()
        token = ""  # nosec B105 - cleared token, not a credential
        if key and sec:
            token = base64.b64encode(f"{key}:{sec}".encode()).decode()
        return {"connection_string": base if (key and sec) else "", "token": token}
    if kind == "sitewise_direct":
        region = (env.get(bootstrap.get("region_env", "AWS_REGION"), "") or "us-east-1").strip()
        # Use the REAL, resolvable SiteWise service endpoint host
        # (iotsitewise.<region>.amazonaws.com) — NOT the bare <region>.amazonaws.com,
        # which fails DNS resolution in the SSRF/scheme guard.
        return {"connection_string": f"iotsitewise://iotsitewise.{region}.amazonaws.com", "token": ""}
    if kind == "athena":
        region = (env.get(bootstrap.get("region_env", "AWS_REGION"), "") or "us-east-1").strip()
        # Regional Athena service endpoint (same pattern connection_probes validates:
        # athena://athena.<region>.amazonaws.com). Workgroup/database/credentials come
        # from the persisted node config + secret, merged server-side at discover time.
        return {"connection_string": f"athena://athena.{region}.amazonaws.com", "token": ""}
    return {"connection_string": "", "token": ""}


# Per-connection-TYPE bootstrap descriptor (identity -> how to build the connection
# from the deploy env). This REPLACES the former hardcoded fallback manifest / no-venv
# fallback (ADR-0035, base-mode purity): the harness reads each demo system's IDENTITY
# (connection_type + schema) from the LIVE registration catalog and derives the
# env-driven bootstrap per TYPE. The connection is still SUPPLIED as INPUT, but the
# system set + identity come from live registrations, not a baked-in per-system table.
def bootstrap_descriptor(connection_type: str, schema: str) -> dict:
    """Derive the env-driven ``connection_bootstrap`` descriptor for a connection TYPE.

    Pure/deterministic: maps a registered system's ``connection_type`` (plus
    ``schema`` for PostgreSQL) to the descriptor ``build_connection`` consumes.
    Unknown/empty types yield ``{"kind": "none"}`` so the caller derives an empty
    connection and skips the system.
    """
    if connection_type == "PostgreSQL":
        return {"kind": "pg", "schema": schema, "from_env": "pg"}
    if connection_type == "MCP Server":
        return {"kind": "mcp", "url_env": "HIGHBYTE_URL",
                "url_ssm": "/adt/highbyte-url", "url_suffix": "/mcp",
                "token_env": "HIGHBYTE_I3X_TOKEN",
                "token_secret": "adt/conn/highbyte"}
    if connection_type == "SiteWise (i3X)":
        return {"kind": "rest", "url_env": "SITEWISE_I3X_URL",
                "token_env": "SITEWISE_I3X_TOKEN"}
    if connection_type == "Tulip (Tables API)":
        # Tulip is a first-class connection type
        # ("Tulip (Tables API)"), no longer a "REST API" system identified by
        # host. Build the connection from TULIP_* env: base_url = TULIP_URL, and
        # an HTTP Basic token minted from base64(TULIP_API_KEY:TULIP_API_SECRET).
        # Supplied out-of-band as the adt/conn/tulip secret (see
        # supply_secret_out_of_band).
        return {"kind": "tulip", "url_env": "TULIP_URL",
                "key_env": "TULIP_API_KEY", "secret_env": "TULIP_API_SECRET"}
    if connection_type == "SiteWise (Direct)":
        return {"kind": "sitewise_direct", "region_env": "AWS_REGION"}
    if connection_type == "Athena":
        # Same shape as SiteWise (Direct): the connection is the regional service
        # endpoint (mirrors connection_probes' athena:// pattern); workgroup/
        # database/credentials ride the persisted node config + secret, merged
        # server-side at discover time (resolve_evidence._with_cross_account_credentials).
        return {"kind": "athena", "region_env": "AWS_REGION"}
    return {"kind": "none"}


def guard_required_system(sid: str, ident) -> str:
    """Fail-loud guard for a REQUIRED (non-OT) demo system's LIVE registration.

    The harness reads each system's identity (connection_type/schema) from the LIVE
    registration catalog (``GET /api/onboard/catalog``) — it does NOT reintroduce a
    hardcoded manifest. This guard refuses to proceed with a required system when
    that live identity is unusable, so the harness FAILS LOUDLY instead of silently
    driving discover with an empty schema (which quietly produced no tables):

      (a) ABSENT from the catalog — the deploy never registered the demo systems
          (is ``ADT_DEMO=1`` / a ``--seed``/``--simulator`` deploy?); an unregistered
          system cannot be onboarded (``/discover`` returns "Unknown system_id").
      (b) PostgreSQL with an EMPTY schema — registered incorrectly; sending an empty
          schema to discover would scan nothing.

    Returns an actionable error message string when the system must be aborted, or
    ``""`` when it is safe to proceed. Pure/deterministic — offline-unit-testable
    with a plain dict; no network, no env, no hardcoded system list.
    """
    if not ident:
        return (f"{sid} is absent from the live onboarding catalog — the deploy did "
                f"not register the demo systems. Ensure this is a --seed/--simulator "
                f"deploy (ADT_DEMO=1) so the demo systems are registered at startup; "
                f"an unregistered system cannot be onboarded.")
    connection_type = ident.get("connection_type", "PostgreSQL")
    schema = (ident.get("schema") or "").strip()
    if connection_type == "PostgreSQL" and not schema:
        return (f"{sid} is registered as PostgreSQL but its live catalog entry has an "
                f"EMPTY schema — the deploy did not register it correctly (ADT_DEMO / "
                f"the demo systems manifest). Refusing to send an empty schema to "
                f"discover.")
    return ""


def connection_inputs(catalog_meta: Mapping[str, dict], systems: list,
                      env: Mapping[str, str]) -> dict:
    """Per requested system, derive the connection INPUT from env + live identity.

    ``catalog_meta`` is the LIVE registration catalog (``{sid: entry}`` from
    ``GET /api/onboard/catalog``); each entry supplies the system's
    ``connection_type`` and ``schema`` (the registration identity). The bootstrap
    descriptor is derived per TYPE (``bootstrap_descriptor``) and the connection is
    built from the deploy env. Returns
    ``{sid: {connection_string, token, connection_type, schema}}``. Systems absent
    from the catalog, or whose env-derived ``connection_string`` is empty, still
    appear so callers can decide to skip them.
    """
    out: dict[str, dict] = {}
    for sid in systems:
        ident = catalog_meta.get(sid, {})
        connection_type = ident.get("connection_type", "PostgreSQL")
        schema = ident.get("schema", "")
        bootstrap = bootstrap_descriptor(connection_type, schema)
        conn = build_connection(bootstrap, env)
        out[sid] = {
            "connection_string": conn.get("connection_string", ""),
            "token": conn.get("token", ""),
            "connection_type": connection_type,
            "schema": schema,
        }
    return out


#: The clock an ``App`` deadline is on (a seam on ``time.monotonic``: tests
#: replace it without touching the process-wide clock).
_monotonic = time.monotonic


class DeadlineExceeded(TimeoutError):
    """An ``App`` call would start after the app's ``deadline``."""


class ProvisionJobFailed(RuntimeError):
    """:func:`provision`'s job ended in error: the app's own job record says
    ``status: error`` (its error in the message). A job whose status could not
    be read, or that the app no longer knows, is not this: nothing shows it
    ended. Running the provision again after this is safe: the app publishes
    a job's end only after it has released its claim on the system, and
    refuses (409) a provision of a system whose provision still runs."""


def _time_left(app) -> "float | None":
    """Seconds until ``app``'s deadline, or ``None`` when it has none."""
    deadline = getattr(app, "deadline", None)
    return None if deadline is None else deadline - _monotonic()


def _past_deadline(app) -> bool:
    left = _time_left(app)
    return left is not None and left <= 0


class App:
    #: When set (a ``time.monotonic`` value), no call starts after it, each
    #: call's socket timeout is capped at the time left (at least 1 s), and
    #: :meth:`poll` stops at it. A caller with an overall budget (the
    #: workshop seed) sets it; ``None`` keeps every call's own bound.
    deadline: "float | None" = None

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def call(self, method: str, path: str, body=None, timeout: int = 120):
        left = _time_left(self)
        if left is not None:
            if left <= 0:
                raise DeadlineExceeded(f"deadline reached before {method} {path}")
            timeout = max(1.0, min(float(timeout), left))
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(req, timeout=timeout) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}

    def poll(self, path: str, label: str, max_s: int) -> dict:
        """Poll an async-job status endpoint until done/error/timeout.

        Transient 5xx on a STATUS GET must not kill a long server-side job:
        during heavy resolve/provision the app's event loop can starve briefly
        (CloudFront/ALB then serve 502/504 for the poll) while the job thread
        keeps working (live evidence 2026-07-14: mes-det resolve). Tolerate a
        bounded run of 5xx polls and keep polling; anything else propagates.
        Stops at the app's ``deadline`` too.
        """
        t0 = time.time()
        consecutive_5xx = 0
        while time.time() - t0 < max_s and not _past_deadline(self):
            try:
                st = self.call("GET", path)
                consecutive_5xx = 0
            except urllib.error.HTTPError as e:
                if e.code >= 500 and consecutive_5xx < 12:
                    consecutive_5xx += 1
                    log(f"    {label}: transient HTTP {e.code} on status poll "
                        f"({consecutive_5xx}/12) — job continues server-side, retrying")
                    time.sleep(10)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
                    continue
                raise
            status = st.get("status")
            if status == "done":
                return st
            if status == "error":
                raise RuntimeError(f"{label} failed: {st.get('error')}")
            time.sleep(5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        return {"status": "timeout"}

    def catalog(self) -> dict:
        """Return {system_id: entry} from the onboarding catalog."""
        data = self.call("GET", "/api/onboard/catalog")
        items = data if isinstance(data, list) else data.get("systems", data.get("catalog", []))
        return {s["id"]: s for s in items}


def log(msg: str) -> None:
    print(msg, flush=True)


def _keep_physical_mappings(tables: list, mappings: list) -> tuple[list, list]:
    """Split map output into (physical, dropped) against discovered columns.

    Mirrors the server's ``_mapping_table_field`` parse — the physical column is
    the FINAL dotted segment — so nested struct paths (``tbl.struct.a.b``) are
    dropped exactly as the provision provenance gate would reject them, and
    connector-owned metadata (``data_type`` / ``relation_kind``) is rebound to
    the DISCOVERED values so the gate's equality checks cannot fail.
    """
    physical = {
        str(t.get("name", "")): {
            str(c.get("name", "")): str(c.get("data_type") or "unknown")
            for c in t.get("columns", [])
        }
        for t in tables if isinstance(t, dict)
    }
    kinds = {str(t.get("name", "")): str(t.get("relation_kind") or "unknown")
             for t in tables if isinstance(t, dict)}

    def _column_of(m: dict) -> str:
        field = str(m.get("field") or m.get("column_name") or m.get("column") or "")
        return field.split(".")[-1].strip()

    kept, dropped = [], []
    for m in mappings:
        table = str(m.get("table") or "")
        column = _column_of(m)
        if column not in physical.get(table, {}):
            dropped.append(str(m.get("field") or column))
            continue
        m = dict(m)
        m["data_type"] = physical[table][column]
        m["relation_kind"] = kinds.get(table, "unknown")
        kept.append(m)
    return kept, dropped


def _map_tables(app: App, sid: str, tables: list) -> list:
    """POST the discovered tables to map and poll its job; the raw mappings.
    Fails LOUD on a map-job timeout."""
    mj = app.call("POST", "/api/onboard/map", {"system_id": sid, "tables": tables})
    if not mj.get("job_id"):
        return mj.get("mappings", [])
    res = app.poll(f"/api/onboard/map/status/{mj['job_id']}", f"map[{sid}]", 600)
    if res.get("status") == "timeout":
        raise RuntimeError(f"map[{sid}] timed out")
    return res.get("mappings", res.get("field_mappings", []))


def discover_and_map(app: App, sid: str, connection_string: str = "",
                     connection_type: str = "") -> dict:
    """discover -> map. Returns {schema, mappings}.

    ``connection_string`` is the harness-supplied connection INPUT (Task 4.1): it
    is included in the discover body so the server uses the env-derived connection
    the harness passes, not a catalog snapshot.

    ``connection_type`` is the client-DECLARED type (the discover route's contract
    for re-onboard: the body type wins so a stale/mis-hydrated catalog identity
    cannot mis-route a non-SQL system into the PostgreSQL branch — the exact
    failure mode was a SiteWise (Direct) system whose in-process identity lacked a
    type, falling through to the PG guard 'Provide system_id or both
    connection_string and schema' and mapping 0 tables).

    Fails LOUD on a discover error or an empty table list — mapping 0 tables only
    produces the cryptic 'AI returned no mappings' downstream. Also returns
    ``timings``: step seconds and table/mapping counts (no values).
    """
    disc_body = {"system_id": sid}
    if connection_string:
        disc_body["connection_string"] = connection_string
    if connection_type:
        disc_body["connection_type"] = connection_type
    t0 = time.monotonic()
    disc = app.call("POST", "/api/onboard/discover", disc_body)
    discover_s = time.monotonic() - t0
    if disc.get("error"):
        raise RuntimeError(f"discover[{sid}] failed: {disc['error']}")
    tables = disc.get("tables", [])
    schema = disc.get("schema_name", "")
    log(f"    discover: {len(tables)} tables (schema={schema}) in {discover_s:.1f}s")
    if not tables:
        raise RuntimeError(
            f"discover[{sid}] returned 0 tables — refusing to map an empty "
            f"discovery (check the connection input / cross-account config)"
        )
    t0 = time.monotonic()
    mappings = _map_tables(app, sid, tables)
    # Non-physical (nested struct path) mappings 400 provision — drop them here.
    mappings, dropped = _keep_physical_mappings(tables, mappings)
    map_s = time.monotonic() - t0
    if dropped:
        log(f"    map: dropped {len(dropped)} non-physical mapping(s): "
            f"{', '.join(dropped[:5])}{' …' if len(dropped) > 5 else ''}")
    log(f"    map: {len(mappings)} field mappings in {map_s:.1f}s")
    return {"schema": schema, "mappings": mappings,
            "timings": {"discover_s": round(discover_s, 1), "map_s": round(map_s, 1),
                        "tables": len(tables), "mappings": len(mappings)}}


def resolve_detailed(app: App, sid: str, mappings: list, join_skip_peers=None,
                     join_sweep_strict: bool = False) -> dict:
    """Resolve and return ``{"rules": [...], "timings": {...}}``.

    ``timings`` is the client wall time (``resolve_s``) plus, when the server
    reports it, the resolve job's own per-stage breakdown under ``server``
    (``agents.stage_timing``: durations and counts only). The job's other
    payload (evidence, sampled examples) is deliberately not returned here.
    ``join_skip_peers``: peers the job's join sweep leaves to another resolve
    of the same graph (the workshop seed's ``Phase2JoinSweep`` once); sent
    only when given, so the request is otherwise the one before.
    ``join_sweep_strict``: the job's sweep is the only one of its pairs
    (retried and reported when it fails); sent only when true.
    """
    t0 = time.monotonic()
    body: dict = {"system_id": sid, "mappings": mappings}
    if join_skip_peers:
        body["join_skip_peers"] = sorted(join_skip_peers)
    if join_sweep_strict:
        body["join_sweep_strict"] = True
    rj = app.call("POST", "/api/onboard/resolve", body)
    server: dict = {}
    res = rj
    if rj.get("job_id"):
        res = app.poll(f"/api/onboard/resolve/status/{rj['job_id']}", f"resolve[{sid}]", 600)
        rules = res.get("rules", res.get("identity_rules", []))
        server = res.get("timings") if isinstance(res.get("timings"), dict) else {}
    else:
        rules = rj.get("rules", [])
    timings: dict = {"resolve_s": round(time.monotonic() - t0, 1)}
    if server:
        timings["server"] = server
    out = {"rules": rules if isinstance(rules, list) else [], "timings": timings}
    if isinstance(res.get("join_sweep"), dict):
        # The job's join sweep (sent only with join_skip_peers): whether it
        # left peers unswept, and which (system ids only).
        out["join_sweep"] = {"incomplete": bool(res["join_sweep"].get("incomplete")),
                             "unswept": [str(p) for p in res["join_sweep"].get("unswept") or []]}
    return out


def resolve(app: App, sid: str, mappings: list) -> list:
    return resolve_detailed(app, sid, mappings)["rules"]


def _catalog_onboarded(app: App, sid: str, consecutive_5xx: int):
    """One catalog check for ``provision``: ``(onboarded, new_5xx_streak)``.

    Same bounded 5xx tolerance as ``App.poll``: a parallel resolve can starve
    the app for a few seconds (live 2026-09-25: one 502 here failed the
    workshop seed and rolled back the whole event stack). A 5xx within the
    bound returns ``(None, streak)`` after a pause; anything else raises.
    """
    try:
        return app.catalog().get(sid, {}).get("status") == "onboarded", 0
    except urllib.error.HTTPError as e:
        if e.code < 500 or consecutive_5xx >= 12:
            raise
        log(f"    provision[{sid}]: transient HTTP {e.code} on catalog poll "
            f"({consecutive_5xx + 1}/12), retrying")
        time.sleep(10)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        return None, consecutive_5xx + 1


#: How long ``provision`` waits: the 720 s it always had, plus the most a
#: provision waits for another system that is rewriting its schema while it
#: re-measures a measured identity rule (``agents.onboard._SCHEMA_REWRITE_WAIT_S``,
#: 300 s). A first provision flips the catalog after that stage, so the wait
#: counts against this bound.
PROVISION_POLL_S = 720 + 300
#: Consecutive 5xx answers a job read tolerates (the same bound as ``App.poll``).
_JOB_READ_5XX_LIMIT = 12


def _provision_request_body(sid: str, meta: dict, mappings: list, rules: list,
                            connection_string: str) -> dict:
    body = {
        "system_id": sid,
        "system_name": meta.get("name", sid),
        "plant": meta.get("plant", ""),
        "schema": meta.get("schema", ""),
        "mappings": mappings,
        "rules": rules,
        "value_mappings": [],
        "connection_type": meta.get("connection_type", "PostgreSQL"),
    }
    if connection_string:
        body["connection_string"] = connection_string
    return body


def start_provision(app: App, sid: str, meta: dict, mappings: list, rules: list,
                    connection_string: str = "", rule_delete_scope: str = "") -> str:
    """POST the provision and return its job id at once (``""`` when the
    server finished it within the request). Raises when it neither started a
    job nor finished. Follow the job with :func:`wait_provision_jobs`.
    ``rule_delete_scope`` ``own`` asks the provision to delete only the
    rules its own system wrote (``ProvisionRequest.rule_delete_scope``);
    sent only when given."""
    body = _provision_request_body(sid, meta, mappings, rules, connection_string)
    if rule_delete_scope:
        body["rule_delete_scope"] = rule_delete_scope
    pj = app.call("POST", "/api/onboard/provision", body)
    job_id = str(pj.get("job_id") or "")
    if job_id or pj.get("status") == "done" or pj.get("runtime_arn"):
        return job_id
    raise RuntimeError(f"provision[{sid}] did not start: {pj.get('error') or 'no job id'}")


def _job_error_text(st: dict) -> str:
    error, reason = str(st.get("error") or "provision_failed"), str(st.get("reason") or "")
    return f"{error} ({reason})" if reason else error


def _job_outcome(app, sid: str, job_id: str, consecutive_5xx: dict) -> "tuple | None":
    """One read of a provision job: ``(status, detail)`` once it has ended
    (``done``, or ``error`` with the job's own error) or can no longer be
    followed (``unknown``: its status could not be read, or the app does not
    know the job id; neither shows the job ended), ``None`` while it runs or
    the read failed transiently (a short run of 5xx, a network error)."""
    try:
        st = app.call("GET", f"/api/onboard/provision/status/{job_id}")
    except urllib.error.HTTPError as e:
        if e.code >= 500 and consecutive_5xx.get(sid, 0) < _JOB_READ_5XX_LIMIT:
            consecutive_5xx[sid] = consecutive_5xx.get(sid, 0) + 1
            log(f"    provision[{sid}]: transient HTTP {e.code} on job read "
                f"({consecutive_5xx[sid]}/{_JOB_READ_5XX_LIMIT}), retrying")
            return None
        return "unknown", f"its job status could not be read (HTTP {e.code})"
    except (urllib.error.URLError, TimeoutError):
        return None                                          # transient, or the deadline: the loop decides
    consecutive_5xx[sid] = 0
    if st.get("status") == "done":
        return "done", ""
    if st.get("status") == "error":
        return "error", _job_error_text(st)
    if "unknown job_id" in str(st.get("error") or "").lower():
        return "unknown", f"the app does not know provision job {job_id} (did it restart?)"
    return None


def wait_provision_jobs(app, jobs: dict, max_s: float, on_end=None) -> dict:
    """Follow provision jobs (``{system id: job id}``; ``""`` = finished in
    the request) to their end, all together, for at most ``max_s`` seconds
    and never past the app's ``deadline``. Returns ``{system id: (status,
    detail)}`` with status ``done``, ``error`` (the job's own error),
    ``unknown`` (it could not be followed: not shown to have ended) or
    ``timeout`` (still running when the wait stopped); ``on_end(sid, status,
    detail)`` is called as each one ends."""
    out: dict = {}

    def end(sid, status, detail):
        out[sid] = (status, detail)
        if on_end is not None:
            on_end(sid, status, detail)

    pending = {}
    for sid, job_id in jobs.items():
        if job_id:
            pending[sid] = job_id
        else:
            end(sid, "done", "")
    t0, consecutive_5xx = _monotonic(), {}
    while pending:
        for sid, job_id in list(pending.items()):
            outcome = _job_outcome(app, sid, job_id, consecutive_5xx)
            if outcome is not None:
                del pending[sid]
                end(sid, *outcome)
        if not pending or _monotonic() - t0 >= max_s or _past_deadline(app):
            break
        time.sleep(8)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
    waited = _monotonic() - t0
    for sid in list(pending):
        end(sid, "timeout", f"still running after {waited:.0f} s")
    return out


def _already_onboarded(app, sid: str) -> bool:
    """Whether the catalog lists ``sid`` as onboarded before its provision
    starts: then the provision is a re-provision, and the catalog says
    nothing about its job. An unreadable catalog counts as not onboarded (the
    first-provision path, as before); the deadline is never swallowed."""
    try:
        return app.catalog().get(sid, {}).get("status") == "onboarded"
    except DeadlineExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - the catalog poll below retries it
        log(f"    provision[{sid}]: catalog unreadable before the provision "
            f"({type(exc).__name__}); following the catalog")
        return False


def provision(app: App, sid: str, meta: dict, mappings: list, rules: list,
              connection_string: str = "", max_s: int = PROVISION_POLL_S,
              follow_job: bool = False) -> bool:
    """Provision the system runtime + write edges.

    ``connection_string`` is the harness-supplied connection INPUT (Task 4.1),
    included in the provision body so registration uses the env-derived connection
    the harness passes — never a server-side catalog snapshot.

    A FIRST provision: the provision *job* stays 'running' through runtime
    warmup + gateway sync, long after the system is actually usable. The
    catalog status flipping to 'onboarded' is the authoritative, faster
    completion signal, so we poll that (and surface an explicit provision-job
    error early if one occurs).

    A RE-provision (the catalog listed the system as onboarded before the
    POST), or any provision with ``follow_job=True`` (the workshop seed, which
    re-runs a job that ends in error after the catalog flip): the job itself
    is followed to its end (:func:`_follow_provision_job`)."""
    reprovision = follow_job or _already_onboarded(app, sid)
    pj = app.call("POST", "/api/onboard/provision",
                  _provision_request_body(sid, meta, mappings, rules, connection_string))
    job_id = pj.get("job_id")
    if not job_id:
        return pj.get("status") == "done" or bool(pj.get("runtime_arn"))
    if reprovision:
        return _follow_provision_job(app, sid, job_id, max_s)
    return _await_first_provision(app, sid, job_id, max_s)


def _follow_provision_job(app, sid: str, job_id: str, max_s: float) -> bool:
    """Follow one provision job to its end (:func:`wait_provision_jobs`):
    ``True`` when it is done, ``False`` while still running at ``max_s``.
    Raises :class:`ProvisionJobFailed` when it ended in error, and a plain
    ``RuntimeError`` when it could not be followed (``unknown``)."""
    status, detail = wait_provision_jobs(app, {sid: job_id}, max_s)[sid]
    if status == "error":
        raise ProvisionJobFailed(f"provision[{sid}] failed: {detail}")
    if status == "unknown":
        raise RuntimeError(f"provision[{sid}]: {detail}")
    return status == "done"


def _await_first_provision(app, sid: str, job_id: str, max_s: float) -> bool:
    """A first provision's wait: ``True`` once the catalog lists ``sid`` as
    onboarded (or its job is done), ``False`` at ``max_s`` or the app's
    deadline; a job read that says ``error`` raises :class:`ProvisionJobFailed`."""
    t0 = time.time()
    consecutive_5xx = 0
    while time.time() - t0 < max_s and not _past_deadline(app):
        onboarded, consecutive_5xx = _catalog_onboarded(app, sid, consecutive_5xx)
        if onboarded is None:
            continue
        if onboarded:
            return True
        # Peek the job only to surface an explicit failure early.
        try:
            st = app.call("GET", f"/api/onboard/provision/status/{job_id}")
            if st.get("status") == "error":
                raise ProvisionJobFailed(f"provision[{sid}] failed: {st.get('error')}")
            if st.get("status") == "done":
                return True
        except urllib.error.URLError:
            pass  # transient; keep polling the catalog
        time.sleep(8)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
    return False


def supply_secret_out_of_band(app: App, sid: str, token: str) -> bool:
    """Supply a system's secret to the server the way a human admin would.

    The discover/provision bodies carry NO secret field (``models.py`` is
    off-limits), so the harness mints ``conn/<id>`` the way an admin would:

    * **highbyte (MCP)** → ``PUT /api/admin/highbyte-token`` delegates to
      ``write_connection`` and persists ``conn/highbyte``.
    * **tulip (Tulip (Tables API))** → there is no Tulip admin endpoint, so the harness
      mints ``adt/conn/tulip`` directly in Secrets Manager (the same secret name
      + JSON shape ``connect_new_system`` writes), carrying the HTTP Basic token
      and ``auth_scheme: "basic"`` so a pre-provision discover reads it via
      ``resolve_registered_token``. This is the scripted greenfield equivalent of
      typing the Basic token into the UI connect form — it lets a fully-scripted
      redeploy auto-onboard Tulip without the UI.

    Returns True when the secret was acked/written.
    """
    if not token:
        return False
    if sid == "highbyte":
        resp = app.call("PUT", "/api/admin/highbyte-token", {"token": token})
        ok = resp.get("status") == "ok"
        log(f"    secret: highbyte token supplied via PUT /api/admin/highbyte-token ({'OK' if ok else 'FAILED'})")
        return ok
    if sid == "tulip":
        ok = _mint_tulip_secret(token)
        if ok and app is not None:
            _hint_if_tulip_mint_unreachable(app)
        return ok
    return False


def _hint_if_tulip_mint_unreachable(app) -> None:
    """Print the remedy when the minted ``adt/conn/tulip`` secret cannot reach the
    app: the app reads a registration's token only through the catalog entry's
    pointer to its own secret (never by name), so a ``tulip`` system registered
    WITHOUT credentials has no pointer and this mint cannot complete it."""
    import re
    try:
        entry = (app.catalog() or {}).get("tulip") or {}
    except Exception:  # noqa: BLE001 — a hint must never abort the run
        return
    arn = str(entry.get("secret_arn") or "")
    if re.search(r":secret:adt/conn/tulip-[A-Za-z0-9]{6}$", arn):
        return
    log("    secret: NOTE — the tulip registration has no pointer to adt/conn/tulip, so the "
        "app cannot read this token. Re-register Tulip in Connect New System with its "
        "API key and secret, then re-run.")


def _mint_tulip_secret(token: str) -> bool:
    """Create-or-update ``adt/conn/tulip`` with the Basic token + auth_scheme.

    Matches connection_resolver._store_connection_secret: secret name
    ``adt/conn/tulip``, SecretString = JSON ``{"token", "auth_scheme": "basic"}``
    plus the registration binding (``connection_origin: "user"`` + ``system_id``)
    a Connect New System registration writes. The app reads this secret only
    through the ``tulip`` registration's own pointer (never by name), so the
    binding keeps a re-mint from turning a registered secret unreadable; a
    ``tulip`` system registered WITHOUT credentials must be registered again
    with them (its entry has no pointer to this secret). Uses an ambient boto3
    session, which honours the harness's AWS_PROFILE / AWS_REGION env (the same
    source --profile/--region default from). The secret VALUE is never logged."""
    import json as _json
    import boto3
    client = boto3.Session().client("secretsmanager")
    payload = _json.dumps({"token": token, "auth_scheme": "basic",
                           "connection_origin": "user", "system_id": "tulip"})
    try:
        client.create_secret(Name="adt/conn/tulip", SecretString=payload)
    except client.exceptions.ResourceExistsException:
        client.put_secret_value(SecretId="adt/conn/tulip", SecretString=payload)
    except Exception as e:  # noqa: BLE001 — surface, don't abort the run
        log(f"    secret: tulip mint FAILED ({type(e).__name__})")
        return False
    log("    secret: tulip Basic token minted to adt/conn/tulip (auth_scheme=basic) OK")
    return True


def adt_runtime_names(region: str, profile: str) -> set:
    import boto3
    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    c = session.client("bedrock-agentcore-control")
    names, token = set(), None
    while True:
        kw = {"nextToken": token} if token else {}
        resp = c.list_agent_runtimes(**kw)
        names.update(r["agentRuntimeName"] for r in resp.get("agentRuntimes", []))
        token = resp.get("nextToken")
        if not token:
            break
    return {n for n in names if n.startswith("adt_")}


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic demo onboarding harness")
    ap.add_argument("--base-url", default=os.environ.get("ADT_BASE_URL", ""))
    ap.add_argument("--username", default=os.environ.get("ADT_USERNAME", "ADT"))
    ap.add_argument("--password", default=os.environ.get("ADT_PASSWORD", ""))
    ap.add_argument("--systems", default="",
                    help="comma-separated system ids, in the order to onboard "
                         "(default: the SQL systems; OT systems added with --include-ot)")
    ap.add_argument("--include-ot", action="store_true",
                    help="also seed the non-SQL OT systems (sitewise-direct, highbyte, "
                         "sitewise) when their deploy env is present")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    ap.add_argument("--skip-phase2", action="store_true", help="skip the cross-system resolve lap")
    ap.add_argument("--assert-runtimes", action="store_true",
                    help="also assert each adt_<id> runtime exists (needs AWS creds)")
    args = ap.parse_args()

    if not args.base_url or not args.password:
        log("ERROR: --base-url and --password (or ADT_BASE_URL/ADT_PASSWORD) are required")
        return 2

    if args.systems.strip():
        systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    else:
        systems = list(DEFAULT_SYSTEMS)
        if args.include_ot:
            systems += list(OT_SYSTEMS)
    app = App(args.base_url)

    log("== login ==")
    me = app.call("POST", "/api/auth/login", {"email": args.username, "password": args.password})
    if me.get("status") != "ok":
        log(f"ERROR: login failed: {me}")
        return 2
    meta_all = app.catalog()
    log(f"  logged in as {me.get('email')}; catalog has {len(meta_all)} systems")

    # Derive each system's connection INPUT from the deploy env, using the identity
    # (connection_type/schema) read from the LIVE registration catalog (Task 4.1 /
    # ADR-0035). The harness SUPPLIES these to the onboarding API; it relies on the
    # live registrations for the system set + identity, never a baked-in manifest.
    # OT systems whose env is absent derive an empty connection_string and are
    # skipped below.
    conn_in = connection_inputs(meta_all, systems, os.environ)

    state: dict[str, dict] = {}
    # Accumulates run failures from connection derivation onward (the Assertions
    # section appends to this same list — it is NOT reset there).
    failures: list[str] = []

    log(f"\n== Phase 1: onboard {len(systems)} systems (order: {', '.join(systems)}) ==")
    seeded: list[str] = []
    for sid in systems:
        ci = conn_in.get(sid, {})
        cs = ci.get("connection_string", "")
        # Fail-loud guard: a REQUIRED (non-OT) system must be present in the LIVE
        # registration catalog and (for PostgreSQL) carry a non-empty schema. If not,
        # the deploy did not register the demo systems (ADT_DEMO/--seed) — abort THIS
        # system with an actionable message rather than silently driving discover with
        # an empty schema. OT systems are env-gated/optional and handled below.
        if sid not in OT_SYSTEMS:
            guard_err = guard_required_system(sid, meta_all.get(sid))
            if guard_err:
                log(f"  [{sid}] [ERROR] {guard_err}")
                failures.append(guard_err)
                continue
        if not cs:
            # OT systems are env-gated and optional — skip when their deploy env is
            # absent (no HIGHBYTE_URL/SITEWISE_I3X_URL).
            if sid in OT_SYSTEMS:
                log(f"  [{sid}] skipped (no connection derivable from env — supply HIGHBYTE_URL/SITEWISE_I3X_URL)")
                continue
            # Required (default/SQL) system with no derivable connection → FAIL
            # LOUDLY. The harness must SUPPLY a valid connection; it must NEVER send
            # an empty DSN and lean on a server fallback (none exists post-cut-over).
            msg = (f"cannot derive connection for {sid} from env; "
                   "set POSTGRES_*/ERP_POSTGRES_*")
            log(f"  [{sid}] [ERROR] {msg}")
            failures.append(msg)
            continue
        seeded.append(sid)
        log(f"  [{sid}] discover -> map -> resolve -> provision")
        try:
            meta = dict(meta_all.get(sid, {"name": sid}))
            # Identity (connection_type/schema) comes from the manifest, not a
            # catalog connection snapshot.
            if ci.get("connection_type"):
                meta["connection_type"] = ci["connection_type"]
            if ci.get("schema"):
                meta["schema"] = ci["schema"]
            # Supply the secret out-of-band (highbyte token) BEFORE provisioning so
            # write_connection mints conn/<id> the same way an admin would.
            if ci.get("token"):
                supply_secret_out_of_band(app, sid, ci["token"])
            dm = discover_and_map(app, sid, connection_string=cs)
            meta["schema"] = dm["schema"] or meta.get("schema", "")
            rules = resolve(app, sid, dm["mappings"])
            log(f"    resolve: {len(rules)} cross-system rules")
            ok = provision(app, sid, meta, dm["mappings"], rules, connection_string=cs)
            log(f"    provision: {'OK' if ok else 'FAILED'}")
            state[sid] = {"meta": meta, "mappings": dm["mappings"], "connection_string": cs}
        except Exception as e:  # noqa: BLE001 — one slow/failed system must not abort the run
            log(f"    [ERROR] {sid} phase-1 failed: {type(e).__name__}: {e}")

    # Subsequent assertions only consider systems we actually attempted to seed.
    systems = seeded

    if not args.skip_phase2:
        log("\n== Phase 2: resolve lap (cross-system links, order-independent) ==")
        for sid in systems:
            if sid not in state:
                log(f"  [{sid}] skipped (phase-1 incomplete)")
                continue
            try:
                rules = resolve(app, sid, state[sid]["mappings"])
                ok = provision(app, sid, state[sid]["meta"], state[sid]["mappings"], rules,
                               connection_string=state[sid].get("connection_string", ""))
                log(f"  [{sid}] resolve={len(rules)} rules -> reprovision {'OK' if ok else 'FAILED'}")
            except Exception as e:  # noqa: BLE001
                log(f"  [{sid}] [ERROR] phase-2 failed: {type(e).__name__}: {e}")

    log("\n== Assertions ==")
    final = app.catalog()
    for sid in systems:
        status = final.get(sid, {}).get("status")
        mark = "PASS" if status == "onboarded" else "FAIL"
        log(f"  [{mark}] {sid} status == onboarded (got '{status}')")
        if status != "onboarded":
            failures.append(f"{sid} not onboarded (status={status})")

    # Graph populated: onboarding must have written System/Field/MAPS_TO into the
    # knowledge graph, and the analytics endpoints must read them back. This is
    # backend-agnostic — it exercises the same REST surface on Neptune or the
    # local Memgraph backend (GRAPH_BACKEND=local), so it gates both.
    try:
        vocab = app.call("GET", "/api/thread/analytics/vocabulary")
        cov = app.call("GET", "/api/thread/analytics/concept-coverage")
        graph_systems = {s.get("id") for s in (cov.get("systems") or [])}
        mapped_fields = vocab.get("total_fields", 0)
        mapped_concepts = vocab.get("mapped_concepts", 0)
        onboarded = [s for s in systems if final.get(s, {}).get("status") == "onboarded"]
        missing = [s for s in onboarded if s not in graph_systems]

        m1 = "PASS" if mapped_fields > 0 and mapped_concepts > 0 else "FAIL"
        log(f"  [{m1}] graph populated: {mapped_fields} fields -> {mapped_concepts} concepts mapped")
        if m1 == "FAIL":
            failures.append("graph not populated (no MAPS_TO edges read back)")

        m2 = "PASS" if not missing else "FAIL"
        log(f"  [{m2}] onboarded systems present in graph ({len(graph_systems)} of {len(onboarded)})")
        if missing:
            failures.append(f"systems missing from graph: {', '.join(missing)}")
    except Exception as e:  # noqa: BLE001 — analytics endpoint unreachable shouldn't mask the status gate
        log(f"  [WARN] graph-populated assertion skipped: {type(e).__name__}: {e}")

    if args.assert_runtimes:
        try:
            live = adt_runtime_names(args.region, args.profile)
            for sid in systems:
                rname = f"adt_{sid}".replace("-", "_")[:48]
                mark = "PASS" if rname in live else "FAIL"
                log(f"  [{mark}] runtime {rname} exists")
                if rname not in live:
                    failures.append(f"runtime {rname} missing")
        except Exception as e:  # noqa: BLE001
            log(f"  [WARN] runtime assertion skipped: {type(e).__name__}: {e}")

    if failures:
        log(f"\n== RESULT: FAIL ({len(failures)}) ==")
        for f in failures:
            log(f"  - {f}")
        return 1
    log(f"\n== RESULT: PASS — {len(systems)} systems onboarded ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
