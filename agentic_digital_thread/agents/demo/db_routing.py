# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo schema→database routing — the ONE sanctioned home for demo DB knowledge.

Owner directive (2026-07-14): demo data must not appear in core engine files.
The schema→DB routing table (which schemas live in the ERP Aurora database vs
the demo seed RDS database) is *demo knowledge* — it existed in FOUR drifting
copies (the legacy catalog DSN builder — deleted by Task 2.2 —
``db._get_conn_params_for_schema``,
``connection_resolver.db_secret_arn_for_schema``,
``onboarding_smoketest._ERP_SCHEMA_PREFIXES``). This module is the single
canonical copy; the core-engine copies delegate here (plan
``sa.plan-demo-agnostic-engine-plan-v2``, ADR-0044 draft).

Resolution order (each function): **env wins**, then a lazy SSM *pointer* read
(``/adt/demo/seed-db`` | ``/adt/demo/erp-db``, JSON
``{host, port, dbname, secret_arn}`` — published by ``deploy.sh`` when the demo
data flags are active), then the legacy defaults. The SSM read mirrors
``catalog._get_highbyte_url`` verbatim in shape: pure lazy read, ``boto3``
imported only inside the fallback (this module is import-pure — stdlib only at
module import), fail-soft to ``{}``, and **no** ``os.environ`` write-back (a
stale live cache is exactly the bug class this plan removes). Pointers hold
only non-secret values — a secret **ARN** is a pointer, never a secret VALUE.

One-way demo seam (see ``agents/demo/__init__.py``): this module MUST NOT
import ``agents.db``, ``agents.catalog``, or ``agents.main`` — callers route
through the seam, never the reverse.
"""

import json
import os
import threading
import time
from typing import Mapping, Optional

from ..errors import MissingDbPasswordError, SystemReaderLoginError  # stdlib-only module, seam-safe

# The single canonical ERP-family prefix tuple. Schemas matching these prefixes
# live in the ERP Aurora database (erp_data, mes_milwaukee/mes_detroit/
# mes_cedarrapids — covered by ``erp_``/``mes_`` — and ``public``); everything
# else (cmms_*, plm_*, adt_demo — created by lambda-adt-seed) lives in
# the ADT seed database. Incident pin: a cmms re-onboard once 500'd because a
# *docstring* lied about this routing — see
# ``tests/unit/test_demo_db_routing.py`` for the behavior pins.
ERP_SCHEMA_PREFIXES: tuple = ("erp_", "mes_", "public")

# Seed-resident exceptions to the prefix rule: lambda-adt-seed creates these
# ``mes_`` schemas in the ADT SEED database (the frozen Indianapolis B-2847
# defect chain — docs/architecture.md "The B-2847 defect chain"), NOT in the ERP
# Aurora. They are registered only in the frozen workshop story
# (agents/demo/workshop.py); without this exception their connection strings
# pointed at the ERP database, where the schemas do not exist (Workshop Studio
# test event 9, 2026-09-13). One ERP (ADR-0084): the seed carries no plant ERP.
# Routing is "where a schema lives" and never reads the workshop story.
SEED_RESIDENT_ERP_FAMILY_SCHEMAS: frozenset = frozenset({"mes_line3"})

_POINTER_KINDS = ("seed-db", "erp-db")

# Short TTL cache for the SSM pointer reads (mirrors db._SCHEMA_CONN_CACHE's
# idiom: module dict + time.monotonic + lock). A pointer is deploy-time-stable,
# so ~60s is safe; NEGATIVE results ({} — parameter absent / SSM error) are
# cached briefly too so an unconfigured env doesn't re-hit SSM per call.
# {kind: (pointer_dict, expires_monotonic)}
_POINTER_CACHE: dict = {}
_POINTER_CACHE_TTL = 60.0
_POINTER_CACHE_NEG_TTL = 10.0
_POINTER_CACHE_LOCK = threading.Lock()


def _read_demo_db_pointer(kind: str) -> dict:
    """UNCACHED SSM read of the ``/adt/demo/{kind}`` pointer.

    The parameter value is JSON ``{host, port, dbname, secret_arn}`` (non-secret
    — the ARN is a pointer, never a secret value). Mirrors
    ``catalog._get_highbyte_url``: pure lazy read, boto3 imported HERE (not at
    module import), fail-soft to ``{}``, no ``os.environ`` write-back.
    """
    try:
        import boto3
        region = os.environ.get("AWS_REGION", "us-east-1")
        ssm = boto3.client("ssm", region_name=region)
        resp = ssm.get_parameter(Name=f"/adt/demo/{kind}")
        raw = (resp.get("Parameter", {}).get("Value") or "").strip()
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {k: str(v).strip() for k, v in data.items() if v is not None}
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    return {}


def _demo_db_pointer(kind: str) -> dict:
    """Short-TTL-cached read of the ``/adt/demo/{kind}`` demo-DB
    pointer (``kind`` is ``"seed-db"`` — demo seed RDS — or ``"erp-db"`` — ERP
    Aurora).

    Callers consult env FIRST and reach this only when the relevant env vars
    are absent (env wins — the offline/tripwire tests pin that SSM is never
    touched while the env wiring is present). The cache keeps repeated
    fallback reads from hammering SSM: positive results live ~60s, negative
    results (``{}``) briefly (:data:`_POINTER_CACHE_NEG_TTL`).
    """
    if kind not in _POINTER_KINDS:
        return {}
    now = time.monotonic()
    with _POINTER_CACHE_LOCK:
        cached = _POINTER_CACHE.get(kind)
    if cached is not None and cached[1] > now:
        return dict(cached[0])
    pointer = _read_demo_db_pointer(kind)
    ttl = _POINTER_CACHE_TTL if pointer else _POINTER_CACHE_NEG_TTL
    with _POINTER_CACHE_LOCK:
        _POINTER_CACHE[kind] = (dict(pointer), now + ttl)
    return pointer


def is_erp_schema(schema: str) -> bool:
    """True iff *schema* belongs to the ERP-family (routes to the ERP database).

    Seed-resident exceptions (:data:`SEED_RESIDENT_ERP_FAMILY_SCHEMAS`) route
    to the seed database even though their names carry an ERP-family prefix.
    """
    name = schema or ""
    if name in SEED_RESIDENT_ERP_FAMILY_SCHEMAS:
        return False
    return any(name.startswith(p) for p in ERP_SCHEMA_PREFIXES)


def demo_conn_params_for_schema(
    schema: str, env: Optional[Mapping[str, str]] = None
) -> Optional[dict]:
    """Connect params for an ERP-family schema; ``None`` for everything else.

    The ERP branch of the legacy ``db._get_conn_params_for_schema`` moved here:
    when ``is_erp_schema(schema)`` and an ERP host is known (env
    ``ERP_POSTGRES_HOST``, else the erp-db SSM pointer), return the
    connection params — fail-closed on a missing password (preserving the
    legacy ``RuntimeError``; the pointer never carries credentials). Otherwise
    return ``None`` and the caller falls through — the demo/generic branch stays
    engine-side in ``db.get_conn_params`` (generic single-DB config, not demo
    knowledge).
    """
    if env is None:
        env = os.environ
    if not is_erp_schema(schema):
        return None
    host = (env.get("ERP_POSTGRES_HOST") or "").strip()
    pointer: dict = {}
    if not host:
        pointer = _demo_db_pointer("erp-db")
        host = (pointer.get("host") or "").strip()
    if not host:
        return None
    # Fail-closed: require an explicit ERP (or shared) password rather than
    # silently using the well-known 'postgres' default (legacy behavior kept).
    erp_password = env.get("ERP_POSTGRES_PASSWORD") or env.get("POSTGRES_PASSWORD")
    if not erp_password:
        raise MissingDbPasswordError(
            "ERP_POSTGRES_PASSWORD (or POSTGRES_PASSWORD) is not set. Set it explicitly — "
            "no insecure default is used."
        )
    return {
        "host": host,
        "port": env.get("ERP_POSTGRES_PORT") or pointer.get("port") or "5432",
        "dbname": env.get("ERP_POSTGRES_DB") or pointer.get("dbname") or "erp_simulation",
        "user": env.get("ERP_POSTGRES_USER") or "postgres",
        "password": erp_password,
    }


def demo_pg_dsn(schema: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Postgres DSN for *schema* — the legacy catalog DSN builder moved here
    verbatim in behavior (ERP branch + demo branch, incl. the ``search_path``
    option), now reading env → SSM pointer for host/port/dbname.

    Seed/registration-time bootstrap only (ADR-0031 constraint 2), never a
    steady-state runtime connection read.
    """
    if env is None:
        env = os.environ
    erp_pointer: dict = {}
    erp_host = ""
    if is_erp_schema(schema):
        erp_host = (env.get("ERP_POSTGRES_HOST") or "").strip()
        if not erp_host:
            erp_pointer = _demo_db_pointer("erp-db")
            erp_host = (erp_pointer.get("host") or "").strip()
    if erp_host:
        host = erp_host
        port = env.get("ERP_POSTGRES_PORT") or erp_pointer.get("port") or "5432"
        db = env.get("ERP_POSTGRES_DB") or erp_pointer.get("dbname") or "erp_simulation"
        user = env.get("ERP_POSTGRES_USER") or "postgres"
        # No default password; must be set via environment variable.
        pw = env.get("ERP_POSTGRES_PASSWORD") or env.get("POSTGRES_PASSWORD", "")
    else:
        seed_pointer: dict = {}
        host = (env.get("POSTGRES_HOST") or "").strip()
        if not host:
            seed_pointer = _demo_db_pointer("seed-db")
            host = (seed_pointer.get("host") or "").strip() or "postgres"
        port = env.get("POSTGRES_PORT") or seed_pointer.get("port") or "5432"
        db = env.get("POSTGRES_DB") or seed_pointer.get("dbname") or "adt_demo"
        user = env.get("POSTGRES_USER") or "postgres"
        # No default password; must be set via environment variable.
        pw = env.get("POSTGRES_PASSWORD", "")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}?options=-csearch_path%3D{schema}"


def demo_db_secret_arn(schema: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Shared demo DB-secret ARN backing a SQL *schema* (``""`` fail-soft).

    The body of the legacy ``connection_resolver.db_secret_arn_for_schema``
    moved here: ERP-family schemas (when an ERP host is known) use
    ``ERP_DB_SECRET_ARN`` (legacy fallback: ``DB_SECRET_ARN``), else the erp-db
    pointer's ``secret_arn``; everything else uses ``DB_SECRET_ARN``, else the
    seed-db pointer's ``secret_arn``. Only ever an ARN — never a secret VALUE.
    """
    if env is None:
        env = os.environ
    if is_erp_schema(schema):
        erp_host = (env.get("ERP_POSTGRES_HOST") or "").strip()
        pointer: dict = {}
        if not erp_host:
            pointer = _demo_db_pointer("erp-db")
            erp_host = (pointer.get("host") or "").strip()
        if erp_host:
            arn = (env.get("ERP_DB_SECRET_ARN") or env.get("DB_SECRET_ARN", "")).strip()
            if arn:
                return arn
            return (pointer.get("secret_arn") or "").strip()
    arn = (env.get("DB_SECRET_ARN") or "").strip()
    if arn:
        return arn
    return (_demo_db_pointer("seed-db").get("secret_arn") or "").strip()


# ---------------------------------------------------------------------------
# System-agent read-only logins (stacks/_db_readers.py)
# ---------------------------------------------------------------------------
#: ``{schema: reader secret ARN}`` as JSON, one map per demo database, set on
#: the app task by the CDK when that database is deployed. Each secret holds
#: ``{"username", "password"}`` of a login that can read that one schema.
SEED_READERS_ENV = "DB_READER_SECRET_ARNS"
ERP_READERS_ENV = "ERP_DB_READER_SECRET_ARNS"
#: A schema's reader login is ``READER_LOGIN_PREFIX + schema`` (the stacks
#: create it under the same name, stacks/_db_readers.py, pinned by test).
READER_LOGIN_PREFIX = "adt_reader_"

#: Connection keys an owner login brings with it; none of them may reach a
#: system agent whose login is replaced by the schema's reader.
_OWNER_LOGIN_KEYS = frozenset({
    "user", "username", "password", "secret_arn", "connection_string", "dsn",
    "host", "port", "dbname", "database",
})


def _reader_secret_map(env: Mapping[str, str], key: str) -> Optional[dict]:
    """The deployment's ``{schema: secret ARN}`` map, ``None`` when unset.
    A value that is not such a map is a deploy defect: fail closed."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in data.items()):
        raise SystemReaderLoginError(f"{key} is not a JSON object of schema to secret ARN")
    return data


def _read_reader_secret(arn: str) -> dict:
    """Default reader-secret read (boto3 imported here, like the SSM pointer)."""
    import boto3
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return json.loads(client.get_secret_value(SecretId=arn)["SecretString"])


def system_agent_reader_login(schema: str, target: Mapping, env: Optional[Mapping[str, str]] = None,
                              fetch_secret=None) -> Optional[dict]:
    """The read-only login a system agent for ``schema`` connects with, or
    ``None`` when its connection keeps the login it has.

    Applies to a connection to the demo database that holds ``schema``
    (``target`` names the env's own host for it) made with that database's
    owner login (``POSTGRES_USER`` / ``ERP_POSTGRES_USER``), no login, or the
    schema's reader already: the result is ``{host, port, dbname, user,
    password}`` on the same database with the reader's login. Another host,
    another login (a customer's own), or no reader map for that database
    keeps the login (``None``).

    Fail closed, never the owner's login: a map without the schema, or a
    reader secret that cannot be read or names the owner, raises
    :class:`~agents.errors.SystemReaderLoginError` (schema only, no value)."""
    if env is None:
        env = os.environ
    target = target or {}
    family = _reader_family(schema, env)
    if family is None:
        return None
    readers, db_host, owner = family
    target_host = str(target.get("host") or "").strip()
    if not db_host or target_host != db_host:
        return None
    login = str(target.get("user") or target.get("username") or "").strip()
    owner_login = login in ("", owner)
    arn = readers.get(schema or "")
    if not arn:
        if owner_login:
            raise SystemReaderLoginError(
                f"no read-only login is configured for schema {schema!r}; the system agent "
                "is not started with the database owner's login")
        return None
    secret = _reader_secret(arn, schema, owner_login, fetch_secret)
    if secret is None or (not owner_login and login != secret[0]):
        return None
    username, password = secret
    if not username or not password or username == owner:
        raise SystemReaderLoginError(
            f"the read-only login secret for schema {schema!r} is incomplete or names the owner")
    return {
        "host": target_host,
        "port": target.get("port") or 5432,
        "dbname": target.get("dbname") or target.get("database") or "",
        "user": username,
        "password": password,
    }


def _reader_family(schema: str, env: Mapping[str, str]) -> Optional[tuple]:
    """``(reader map, database host, owner login)`` for the database that
    holds ``schema``, or ``None`` when the deployment has no reader map for it."""
    erp = is_erp_schema(schema)
    readers = _reader_secret_map(env, ERP_READERS_ENV if erp else SEED_READERS_ENV)
    if readers is None:
        return None
    host_key, user_key, kind = (("ERP_POSTGRES_HOST", "ERP_POSTGRES_USER", "erp-db") if erp
                                else ("POSTGRES_HOST", "POSTGRES_USER", "seed-db"))
    db_host = (env.get(host_key) or "").strip() or (_demo_db_pointer(kind).get("host") or "").strip()
    return readers, db_host, (env.get(user_key) or "postgres").strip()


def _reader_secret(arn: str, schema: str, owner_login: bool, fetch_secret) -> Optional[tuple]:
    """``(username, password)`` from a reader secret. Unreadable: fail closed
    for an owner login; ``None`` (keep the login) for any other login."""
    try:
        secret = (fetch_secret or _read_reader_secret)(arn)
        return str(secret.get("username") or ""), str(secret.get("password") or "")
    except Exception as exc:  # noqa: BLE001 - classified below, never the value
        if not owner_login:
            return None
        raise SystemReaderLoginError(
            f"the read-only login for schema {schema!r} could not be read "
            f"({type(exc).__name__})") from None


def system_agent_pg_config(
    schema: str, config: Mapping, env: Optional[Mapping[str, str]] = None,
    fetch_secret=None,
) -> dict:
    """``config`` (a PostgreSQL system agent's connection config, its connect
    params merged in) with an owner login on a demo database replaced by the
    schema's read-only login (:func:`system_agent_reader_login`). Every key
    the owner login brought (user, password, the owner secret pointer, a DSN)
    is dropped; everything else (prompt, tables, concept mappings) is kept.
    A config that keeps its login is returned as a copy, unchanged."""
    reader = system_agent_reader_login(schema, config, env, fetch_secret)
    out = dict(config or {})
    if reader is None:
        return out
    out = {k: v for k, v in out.items() if k not in _OWNER_LOGIN_KEYS}
    out.update(reader)
    return out
