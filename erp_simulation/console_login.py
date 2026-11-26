# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ERP dashboard SQL console's own database login.

The dashboard connects as the cluster admin. A READ ONLY transaction stops
that login from writing tables, but not from calling functions whose effect
lands outside the transaction: ``pg_terminate_backend``,
``pg_cancel_backend``, ``pg_advisory_lock`` or ``set_config('role', ...)``.
So console text never runs as the admin. Each console query opens its OWN
short-lived connection as ``CONSOLE_ROLE``, runs in
``sql_console.run_readonly_query``, and the connection is closed afterwards in
every case (advisory locks and session settings die with it).

The role, synced lazily by the admin connection on the first console query of
a process (never at dashboard start, so a failure can only disable the
console):

* ``LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 5`` and the password from the console credential (sent as
  a SCRAM verifier, never as plain text);
* ``default_transaction_read_only = on`` and ``statement_timeout`` on the role;
* ``USAGE`` and ``SELECT ON ALL TABLES`` in every non-system schema the admin
  can use. A later sync covers tables created since.

After each sync the role is checked: no elevated attribute and no membership
in any other role (``pg_signal_backend`` would let it terminate sessions). If
the check fails the console refuses to run.

The sync is cached per process and runs again when the console login fails
authentication, or when a cached sync meets a missing privilege (a data reset
recreates tables, which drops their grants). Either way the query is retried
once. A sync that meets a transient catalog conflict (a deploy-time login
setup granting on the same schema at the same moment) is rolled back and run
again (``retry_on_conflict``).

Fail closed: no credential configured, an unreadable secret or a failed sync
raise ``ConsoleUnavailable`` and the console shows ``UNAVAILABLE_MESSAGE``. It
never falls back to the admin login.

Credential: ``CONSOLE_DB_SECRET_NAME`` names a Secrets Manager secret
``{"username": "erp_console", "password": "..."}`` (deployments). For local
development only, with ``USE_SECRETS_MANAGER`` not ``true``,
``CONSOLE_DB_PASSWORD`` supplies the password instead.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from erp_simulation import sql_console
from erp_simulation.utils import compose_sql

logger = logging.getLogger(__name__)

CONSOLE_ROLE = "erp_console"
SECRET_ENV = "CONSOLE_DB_SECRET_NAME"  # nosec B105 - env var name, not a value
LOCAL_PASSWORD_ENV = "CONSOLE_DB_PASSWORD"  # nosec B105 - env var name, not a value
CONNECTION_LIMIT = 5
UNAVAILABLE_MESSAGE = "The SQL console is not available on this deployment."

_INSUFFICIENT_PRIVILEGE = "42501"
# Connection parameters the console copies from the admin connection.
_COPIED_DSN_KEYS = ("host", "hostaddr", "port", "dbname", "sslmode", "sslrootcert")
_SYSTEM_SCHEMAS_SQL = (
    "SELECT nspname FROM pg_namespace"
    " WHERE nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND nspname NOT LIKE 'pg\\_toast%' AND nspname NOT LIKE 'pg\\_temp%'"
    " AND has_schema_privilege(oid, 'USAGE')"
    " ORDER BY nspname"
)
_ROLE_CHECK_SQL = (
    "SELECT r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole,"
    " r.rolreplication, r.rolbypassrls,"
    " (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid)"
    " FROM pg_roles r WHERE r.rolname = %s"
)
_MINIMAL_ROLE = (True, False, False, False, False, False, 0)


class ConsoleUnavailable(Exception):
    """The console cannot run here; show ``UNAVAILABLE_MESSAGE``. The message
    is a fixed reason for the log and never carries a credential."""


class _AuthFailed(Exception):
    """The console login was refused at connect time."""


@dataclass(frozen=True)
class ConsoleCredential:
    username: str
    password: str = field(repr=False)


# ------------------------------------------------------------ credential ---

def _fetch_secret(secret_id: str, environ: Mapping[str, str]) -> Dict[str, Any]:
    import boto3  # deployed dashboards only; local development never gets here

    region = environ.get("AWS_REGION") or environ.get("REGION") or None
    client = boto3.client("secretsmanager", region_name=region)
    return json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])


def load_credential(environ: Optional[Mapping[str, str]] = None,
                    fetch_secret: Optional[Callable[..., Dict[str, Any]]] = None) -> ConsoleCredential:
    """The console credential, or ``ConsoleUnavailable`` when none is usable."""
    env = os.environ if environ is None else environ
    secret_id = (env.get(SECRET_ENV) or "").strip()
    if secret_id:
        try:
            secret = (fetch_secret or _fetch_secret)(secret_id, env)
            username, password = secret["username"], secret["password"]
        except Exception as exc:
            logger.warning("SQL console: %s reading its login secret", type(exc).__name__)
            raise ConsoleUnavailable("console login secret unreadable") from None
        if username != CONSOLE_ROLE or not isinstance(password, str) or not password:
            raise ConsoleUnavailable("console login secret has an unexpected shape")
        return ConsoleCredential(username, password)
    local_password = env.get(LOCAL_PASSWORD_ENV) or ""
    if local_password and (env.get("USE_SECRETS_MANAGER") or "").lower() != "true":
        return ConsoleCredential(CONSOLE_ROLE, local_password)
    raise ConsoleUnavailable("no console login configured")


# ------------------------------------------------------------- role sync ---

def _password_verifier(admin_conn: Any, credential: ConsoleCredential) -> str:
    """A SCRAM-SHA-256 verifier, so the statement never carries the password."""
    from erp_simulation import pgdriver

    return pgdriver.encrypt_password(credential.password, credential.username,
                                     admin_conn, "scram-sha-256")


def _upsert_role(cursor: Any, role: str, verifier: str) -> None:
    from erp_simulation.pgdriver import sql

    # compose_sql quotes the role as an identifier; the limit, the SCRAM
    # verifier and the timeout are sql.Literal.
    limit = sql.Literal(CONNECTION_LIMIT)
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if cursor.fetchone() is None:
        cursor.execute(compose_sql(
            "CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
            " NOBYPASSRLS CONNECTION LIMIT {limit}", role=role, limit=limit))
    # SUPERUSER, REPLICATION and BYPASSRLS can only be changed by a real
    # superuser, which a managed cluster's admin is not; _check_minimal_role
    # refuses a role that carries any of them instead.
    cursor.execute(compose_sql(
        "ALTER ROLE {role} WITH LOGIN NOCREATEDB NOCREATEROLE CONNECTION LIMIT {limit} PASSWORD {verifier}",
        role=role, limit=limit, verifier=sql.Literal(verifier)))
    cursor.execute(compose_sql("ALTER ROLE {role} SET default_transaction_read_only = on", role=role))
    cursor.execute(compose_sql("ALTER ROLE {role} SET statement_timeout = {timeout}", role=role,
                               timeout=sql.Literal(f"{sql_console.STATEMENT_TIMEOUT_SECONDS}s")))


def _grant_select(cursor: Any, role: str) -> List[str]:
    cursor.execute(_SYSTEM_SCHEMAS_SQL)
    schemas = [row[0] for row in cursor.fetchall()]
    for schema in schemas:  # compose_sql quotes schema and role as identifiers
        cursor.execute(compose_sql("GRANT USAGE ON SCHEMA {schema} TO {role}", schema=schema, role=role))
        cursor.execute(compose_sql("GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}",
                                   schema=schema, role=role))
    return schemas


def _check_minimal_role(cursor: Any, role: str) -> None:
    cursor.execute(_ROLE_CHECK_SQL, (role,))
    if tuple(cursor.fetchone() or ()) != _MINIMAL_ROLE:
        raise ConsoleUnavailable("console role has extra attributes or memberships")


def sync_role(admin_conn: Any, credential: ConsoleCredential) -> List[str]:
    """Create or update the console role and its grants, then commit.

    Returns the schemas granted. Raises ``ConsoleUnavailable`` when the role
    would be the admin itself or is not minimal after the sync."""
    verifier = _password_verifier(admin_conn, credential)
    with admin_conn.cursor() as cursor:
        cursor.execute("SELECT current_user")
        if cursor.fetchone()[0] == credential.username:
            raise ConsoleUnavailable("console login is the admin login")
        _upsert_role(cursor, credential.username, verifier)
        schemas = _grant_select(cursor, credential.username)
        _check_minimal_role(cursor, credential.username)
    admin_conn.commit()
    return schemas


# ----------------------------------------------------------- per process ---

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {}


def forget_sync() -> None:
    """Drop the cached credential and sync (tests; a later query re-syncs)."""
    with _LOCK:
        _STATE.clear()


# >>> retry on a transient catalog conflict (one identical copy in each database
# setup, since each is packaged alone; tests/test_db_setup_conflict_retry.py)
#: A GRANT on a schema or table rewrites that object's one catalog row for every
#: grantee, so two setups granting on the same object at the same moment
#: collide: the second fails with "tuple concurrently updated" (XX000) once the
#: first commits. A serialization failure (40001) or a deadlock (40P01) is the
#: same kind of transient conflict. A setup is one idempotent transaction, so it
#: is rolled back and run again from the start.
RETRY_SQLSTATES = ("40001", "40P01")
CONCURRENT_UPDATE_SQLSTATE = "XX000"
CONCURRENT_UPDATE_MESSAGE = "tuple concurrently updated"
RETRY_ATTEMPTS = 6
RETRY_FIRST_DELAY_SECONDS = 0.5
RETRY_MAX_DELAY_SECONDS = 8.0
#: No run starts later than this after the first one started.
RETRY_BUDGET_SECONDS = 30.0
#: Invoke time a retry must leave after its wait: one more run and the answer.
RETRY_RESERVE_SECONDS = 20.0


def is_transient_conflict(exc: BaseException) -> bool:
    """True for a catalog conflict another transaction caused (worth a retry)."""
    code = getattr(exc, "pgcode", None)
    if code in RETRY_SQLSTATES:
        return True
    text = getattr(exc, "pgerror", None) or str(exc)
    return code == CONCURRENT_UPDATE_SQLSTATE and CONCURRENT_UPDATE_MESSAGE in text


def retry_on_conflict(conn, setup, context=None, *, sleep=None, clock=None, jitter=None):
    """``setup()``, one whole setup transaction that commits itself, run again
    from the start after a transient catalog conflict: rolled back, then retried
    with exponential backoff and jitter, at most RETRY_ATTEMPTS runs, none
    starting more than RETRY_BUDGET_SECONDS after the first, and none leaving
    less than RETRY_RESERVE_SECONDS of the Lambda invoke (``context``). Any
    other error, or a conflict with no retry left, is raised as it came. Logs
    the attempt and the error class only."""
    sleep, clock, jitter = sleep or time.sleep, clock or time.monotonic, jitter or random.random
    started = clock()
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return setup()
        except Exception as exc:
            if not is_transient_conflict(exc):
                raise
            try:
                conn.rollback()
            except Exception:
                raise exc from None
            delay = min(RETRY_MAX_DELAY_SECONDS, RETRY_FIRST_DELAY_SECONDS * 2 ** (attempt - 1))
            delay *= 0.5 + jitter() / 2
            remaining = getattr(context, "get_remaining_time_in_millis", None)
            if (attempt == RETRY_ATTEMPTS or clock() - started + delay > RETRY_BUDGET_SECONDS
                    or (remaining is not None and remaining() / 1000 - delay < RETRY_RESERVE_SECONDS)):
                logger.warning("database setup: transient catalog conflict (%s) on attempt %d of %d; "
                               "not retried", type(exc).__name__, attempt, RETRY_ATTEMPTS)
                raise
            logger.warning("database setup: transient catalog conflict (%s) on attempt %d of %d; "
                           "retrying in %.1f s", type(exc).__name__, attempt, RETRY_ATTEMPTS, delay)
        sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
# <<< retry on a transient catalog conflict


def _sync_with_admin(db: Any, credential: ConsoleCredential) -> Dict[str, Any]:
    """Run ``sync_role`` on a pooled admin connection (again after a transient
    catalog conflict); return the console's connection parameters. The sync's
    own error never reaches the pool wrapper's log, so no statement text is
    logged."""
    failure: Optional[BaseException] = None
    with db.connection() as admin:
        try:
            params = {k: v for k, v in admin.get_dsn_parameters().items()
                      if k in _COPIED_DSN_KEYS and v}
            retry_on_conflict(admin, functools.partial(sync_role, admin, credential))
        except Exception as exc:
            failure = exc
            try:
                admin.rollback()
            except Exception:  # nosec B110 - the pool wrapper discards a broken connection
                pass
    if isinstance(failure, ConsoleUnavailable):
        raise failure
    if failure is not None:
        logger.warning("SQL console: role sync failed (%s, SQLSTATE %s)",
                       type(failure).__name__, getattr(failure, "pgcode", None))
        raise ConsoleUnavailable("console role sync failed")
    return params


def _ensure_synced(db: Any, force: bool, environ: Optional[Mapping[str, str]]
                   ) -> Tuple[ConsoleCredential, Dict[str, Any], bool]:
    """``(credential, params, fresh)``; ``fresh`` when this call synced."""
    with _LOCK:
        if not force and "params" in _STATE:
            return _STATE["credential"], _STATE["params"], False
        _STATE.clear()
        credential = load_credential(environ)
        params = _sync_with_admin(db, credential)
        _STATE.update(credential=credential, params=params)
        return credential, params, True


# ----------------------------------------------------------------- query ---

def _connect(params: Mapping[str, Any], credential: ConsoleCredential) -> Any:
    from erp_simulation import pgdriver

    try:
        return pgdriver.connect(user=credential.username, password=credential.password,
                                connect_timeout=10, application_name="erp-sql-console",
                                **params)
    except pgdriver.OperationalError as exc:
        if "authentication failed" in str(exc):
            raise _AuthFailed() from None
        raise


def _query_as_console(params: Mapping[str, Any], credential: ConsoleCredential,
                      sql: str, connect: Callable[..., Any]) -> Tuple[List[str], Sequence[tuple]]:
    conn = connect(params, credential)
    try:
        return sql_console.run_readonly_query(conn, sql)
    finally:
        sql_console.close_quietly(conn)


def run_console_query(db: Any, sql: str, *, connect: Optional[Callable[..., Any]] = None,
                      environ: Optional[Mapping[str, str]] = None
                      ) -> Tuple[List[str], Sequence[tuple]]:
    """Run one console statement as the console login; never as the admin.

    ``db`` is the dashboard's ``DatabaseManager``; its pool is used only to
    sync the role. Raises ``ConsoleUnavailable`` (show ``UNAVAILABLE_MESSAGE``)
    or the query's own database error (show
    ``sql_console.console_error_message``)."""
    connect = connect or _connect
    for attempt in (0, 1):
        credential, params, fresh = _ensure_synced(db, attempt > 0, environ)
        try:
            return _query_as_console(params, credential, sql, connect)
        except _AuthFailed:
            if attempt:
                raise ConsoleUnavailable("console login refused after a re-sync") from None
        except Exception as exc:
            if attempt or fresh or getattr(exc, "pgcode", None) != _INSUFFICIENT_PRIVILEGE:
                raise
    raise AssertionError("unreachable")  # pragma: no cover
