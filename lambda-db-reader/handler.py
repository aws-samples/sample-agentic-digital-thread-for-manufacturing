# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Custom resource: the read-only database logins of the system agents.

A system agent runs SQL its model wrote. It must not run it as the database
owner: a read-only transaction stops table writes, but not reads of other
schemas or functions whose effect lands outside the transaction. So each
schema of a database ADT provisions gets its own login, and the system agent
for that schema connects with it (agents/demo/db_routing.py).

This non-public function does the one privileged step, at deploy time and
after a direct ERP reset, with the database owner's credential:

* Create / Update: for every configured schema, create (or reset) one LOGIN
  role named for it, set its password from its own secret, revoke every
  other privilege the role holds in this database, then grant CONNECT on the
  database, USAGE on the schema, SELECT on every table and view in it, and
  SELECT on tables the owner creates there later (default privileges). The
  result is verified: exactly those grants, no ownership, no privilege
  anywhere else, no privileged attribute, no role membership. A schema that
  does not exist yet gets the login and CONNECT only. A role an Update no
  longer lists is dropped. A transient catalog conflict with another setup
  granting on the same schema at the same moment rolls that reader's
  transaction back and runs it again (``retry_on_conflict``).
* Delete: disable each role's login, then drop it, best effort.

The stack re-runs this resource after every run of the database's own seed
or initialisation (a reset recreates schemas and tables, which drops their
grants), so the grants always match the data. A reset outside CloudFormation
(the ERP dashboard's Reset All, which invokes the ERP db-init function
directly) is followed by the same Update event from that function
(lambda-db-init/handler.py run_direct_reset_steps).

Configured names are validated as plain lower-case identifiers before any
SQL is built; each password is hashed client-side (SCRAM-SHA-256), so only
its verifier ever reaches the server, as a quoted literal (a role statement
cannot take a bound parameter).
"""
from __future__ import annotations

import functools
import json
import logging
import random
import re
import time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONNECT_TIMEOUT_SECONDS = 10
STATEMENT_TIMEOUT_MS = 30000
LOCK_TIMEOUT_MS = 10000
#: The login's own per-statement default; the system agent sets the same value
#: on every query (agents/system_agent.py ``_PG_STATEMENT_TIMEOUT_MS``).
READER_STATEMENT_TIMEOUT_MS = 60000
#: Each query opens one short-lived connection. This bounds a runaway loop of
#: parallel tool calls without limiting normal use.
READER_CONNECTION_LIMIT = 20
PHYSICAL_RESOURCE_ID = "adt-system-db-readers"

# An owner login that is not a superuser can CREATE a role with these attributes
# false, but cannot name SUPERUSER / REPLICATION / BYPASSRLS in ALTER ROLE;
# an existing role is reset with the rest and those three are verified false.
_CREATE_ATTRIBUTES = ("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION "
                      f"NOBYPASSRLS CONNECTION LIMIT {READER_CONNECTION_LIMIT}")
_RESET_ATTRIBUTES = f"LOGIN NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT {READER_CONNECTION_LIMIT}"
# Statement templates for ddl(): each {} is a role name, filled in as a quoted
# identifier; the attributes and the timeout are module constants. The role
# statements' last {} is the password verifier, filled in by role_sql() as a
# quoted literal: a utility statement cannot bind a parameter.
_CREATE_ROLE_SQL = f"CREATE ROLE {{}} WITH {_CREATE_ATTRIBUTES} PASSWORD {{}}"
_RESET_ROLE_SQL = f"ALTER ROLE {{}} WITH {_RESET_ATTRIBUTES} PASSWORD {{}}"
_SET_TIMEOUT_SQL = f"ALTER ROLE {{}} SET statement_timeout = {READER_STATEMENT_TIMEOUT_MS}"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

ROLE_STATE_SQL = "SELECT rolsuper, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = %s"
#: (can log in, superuser, createdb, createrole, replication, bypassrls, inherits).
ROLE_ATTRIBUTES_SQL = ("SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,"
                       " rolbypassrls, rolinherit FROM pg_roles WHERE rolname = %s")
MINIMAL_ATTRIBUTES = (True, False, False, False, False, False, False)
CURRENT_DATABASE_SQL = "SELECT current_database(), current_user"
SCHEMA_EXISTS_SQL = "SELECT 1 FROM pg_namespace WHERE nspname = %s"
#: Every relation GRANT SELECT ON ALL TABLES covers: tables, views,
#: materialized views, foreign tables and partitioned tables.
RELATIONS_SQL = """
SELECT c.relname::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
 ORDER BY 1
"""

SCRAM_PREFIX = "SCRAM-SHA-256$"

#: The role's ownership ('o') and privilege ('a') dependencies, cluster-wide:
#: (deptype, is in this database, catalog of the object). A privilege on this
#: database itself is recorded as a shared object (dbid 0) and counts as here.
SHDEPEND_SQL = """
SELECT d.deptype::text,
       d.dbid = cur.oid OR (d.dbid = 0 AND d.classid = 'pg_database'::regclass AND d.objid = cur.oid),
       d.classid::regclass::text
  FROM pg_shdepend d,
       (SELECT oid FROM pg_database WHERE datname = current_database()) cur
 WHERE d.refclassid = 'pg_authid'::regclass
   AND d.refobjid = (SELECT oid FROM pg_roles WHERE rolname = %(role)s)
   AND d.deptype IN ('o', 'a')
 ORDER BY 1, 2, 3
"""
#: Catalogs whose privileges the inventory below lists and verifies exactly.
INVENTORIED_CATALOGS = ("pg_class", "pg_namespace", "pg_proc", "pg_default_acl", "pg_database")

#: Every privilege granted directly to the role in this database, and every
#: role it is a member of: (kind, schema, object, column, privilege). A
#: default-privilege row names its schema, its object type and, in the column
#: slot, the role whose future objects it covers.
GRANTS_SQL = """
WITH r AS (SELECT oid FROM pg_roles WHERE rolname = %(role)s)
SELECT 'table', n.nspname::text, c.relname::text, NULL::text, a.privilege_type::text
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN LATERAL aclexplode(c.relacl) a JOIN r ON a.grantee = r.oid
UNION ALL
SELECT 'column', n.nspname::text, c.relname::text, att.attname::text, a.privilege_type::text
  FROM pg_attribute att JOIN pg_class c ON c.oid = att.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN LATERAL aclexplode(att.attacl) a JOIN r ON a.grantee = r.oid
UNION ALL
SELECT 'schema', NULL, n.nspname::text, NULL, a.privilege_type::text
  FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a JOIN r ON a.grantee = r.oid
UNION ALL
SELECT 'database', NULL, d.datname::text, NULL, a.privilege_type::text
  FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) a JOIN r ON a.grantee = r.oid
 WHERE d.datname = current_database()
UNION ALL
SELECT 'function', NULL, p.oid::regprocedure::text, NULL, a.privilege_type::text
  FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a JOIN r ON a.grantee = r.oid
UNION ALL
SELECT 'default', n.nspname::text, d.defaclobjtype::text, g.rolname::text, a.privilege_type::text
  FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
  JOIN pg_roles g ON g.oid = d.defaclrole
  CROSS JOIN LATERAL aclexplode(d.defaclacl) a JOIN r ON a.grantee = r.oid
UNION ALL
SELECT 'member', NULL, g.rolname::text, NULL, 'MEMBER'
  FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid JOIN r ON m.member = r.oid
ORDER BY 1, 2, 3, 4, 5
"""

#: pg_default_acl.defaclobjtype -> the ALTER DEFAULT PRIVILEGES object class.
_DEFAULT_OBJECT_CLASS = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES",
                         "n": "SCHEMAS"}


def check_ident(name: str) -> str:
    """A configured identifier, refused unless it is a plain lower-case name."""
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise ValueError(f"not a plain lower-case identifier: {name!r}")
    return name


def ident(name: str) -> str:
    """A configured identifier, validated and double-quoted."""
    return f'"{check_ident(name)}"'


def ddl(template: str, *names: str):
    """*template* with each ``{}`` filled by the next name, quoted by the
    driver as an identifier. DDL cannot bind identifiers, so this is how a
    name gets into a statement. Callers pass names they have checked."""
    from pgdriver import sql  # from the function's layer

    return sql.SQL(template).format(*(sql.Identifier(name) for name in names))


def role_sql(template: str, role: str, verifier: str):
    """A role statement (*template* of ``{}`` role, ``{}`` password) with the
    checked role as an identifier and the SCRAM verifier as a quoted literal."""
    from pgdriver import sql  # from the function's layer

    return sql.SQL(template).format(sql.Identifier(check_ident(role)), sql.Literal(verifier))


def quote_catalog(name: str) -> str:
    """A name read from the catalog, quoted as a SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def ensure_schema_reader(conn, role: str, password: str, schema: str) -> list[str]:
    """Create or reset ``role`` so it can read ``schema`` and nothing else, in
    one transaction; returns the relations it can read. Raises (nothing
    committed) when the result differs from that grant. The password is hashed
    here (SCRAM-SHA-256), so no statement the server sees or logs carries it."""
    check_ident(role)
    check_ident(schema)
    verifier = _password_verifier(conn, role, password)
    if not (isinstance(verifier, str) and verifier.startswith(SCRAM_PREFIX) and verifier != password):
        raise RuntimeError("could not compute a SCRAM-SHA-256 verifier for the reader password")
    with conn.cursor() as cur:
        cur.execute(CURRENT_DATABASE_SQL)
        database, owner = cur.fetchone()
        if owner == role:
            raise RuntimeError(f"role {role} is the login running the setup; refusing to use it")
        _create_or_reset(cur, role, verifier)
        _revoke_everything(cur, role)
        cur.execute(ddl("GRANT CONNECT ON DATABASE {} TO {}", str(database), role))
        cur.execute(SCHEMA_EXISTS_SQL, (schema,))
        present = cur.fetchone() is not None
        if present:
            cur.execute(ddl("GRANT USAGE ON SCHEMA {} TO {}", schema, role))
            cur.execute(ddl("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}", schema, role))
            cur.execute(ddl("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}",
                            schema, role))
        else:
            logger.warning("schema %s does not exist yet; %s gets CONNECT only", schema, role)
        cur.execute(ddl("ALTER ROLE {} SET default_transaction_read_only = on", role))
        cur.execute(ddl(_SET_TIMEOUT_SQL, role))
        relations = _verify(cur, role, schema, database, owner, present)
    conn.commit()
    return relations


def _password_verifier(conn, role: str, password: str) -> str:
    """A SCRAM-SHA-256 verifier for ``password``, computed client-side."""
    from pgdriver import encrypt_password  # from the function's layer

    return encrypt_password(password, role, conn, "scram-sha-256")


def _create_or_reset(cur, role: str, verifier: str) -> None:
    cur.execute(ROLE_STATE_SQL, (role,))
    state = cur.fetchone()
    if state is None:
        cur.execute(role_sql(_CREATE_ROLE_SQL, role, verifier))
        return
    if any(state):
        raise RuntimeError(f"role {role} has superuser, replication or bypassrls; refusing to use it")
    cur.execute(role_sql(_RESET_ROLE_SQL, role, verifier))


def _revoke_sql(kind: str, schema, name: str, column, role: str):
    """The REVOKE for one inventory object (None for kinds left to verification)."""
    target = ident(role)
    if kind == "table":
        return f"REVOKE ALL ON TABLE {quote_catalog(schema)}.{quote_catalog(name)} FROM {target}"
    if kind == "column":
        return (f"REVOKE ALL ({quote_catalog(column)}) ON TABLE "
                f"{quote_catalog(schema)}.{quote_catalog(name)} FROM {target}")
    if kind == "schema":
        return f"REVOKE ALL ON SCHEMA {quote_catalog(name)} FROM {target}"
    if kind == "database":
        return f"REVOKE ALL ON DATABASE {quote_catalog(name)} FROM {target}"
    if kind == "function":  # functions and procedures; the catalog's own regprocedure text
        return f"REVOKE ALL ON ROUTINE {name} FROM {target}"
    if kind == "member":
        return f"REVOKE {quote_catalog(name)} FROM {target}"
    if kind == "default" and name in _DEFAULT_OBJECT_CLASS:
        scope = f" IN SCHEMA {quote_catalog(schema)}" if schema else ""
        return (f"ALTER DEFAULT PRIVILEGES FOR ROLE {quote_catalog(column)}{scope} "
                f"REVOKE ALL ON {_DEFAULT_OBJECT_CLASS[name]} FROM {target}")
    return None


def _revoke_everything(cur, role: str) -> None:
    """Revoke every privilege and membership the role holds, once per object."""
    cur.execute(GRANTS_SQL, {"role": role})
    seen: list[tuple] = []
    for kind, schema, name, column, _privilege in cur.fetchall():
        key = (kind, schema, name, column)
        if key not in seen:
            seen.append(key)
    for key in seen:
        sql = _revoke_sql(*key, role)
        if sql:
            cur.execute(sql)


def expected_grants(schema: str, database: str, owner: str, relations: list[str],
                    present: bool) -> set[tuple]:
    """The exact inventory a reader holds after the setup."""
    want = {("database", None, database, None, "CONNECT")}
    if present:
        want |= {("schema", None, schema, None, "USAGE"),
                 ("default", schema, "r", owner, "SELECT")}
        want |= {("table", schema, rel, None, "SELECT") for rel in relations}
    return want


def _verify(cur, role: str, schema: str, database: str, owner: str, present: bool) -> list[str]:
    """The role holds exactly the read grant, owns nothing, holds no privilege
    the inventory cannot see, has no privileged attribute and no membership."""
    relations: list[str] = []
    if present:
        cur.execute(RELATIONS_SQL, (schema,))
        relations = [row[0] for row in cur.fetchall()]
    want = expected_grants(schema, database, owner, relations, present)
    cur.execute(GRANTS_SQL, {"role": role})
    found = {tuple(row) for row in cur.fetchall()}
    if found != want:
        raise RuntimeError(f"reader role {role} privileges differ from the grant: extra "
                           f"{sorted(found - want, key=str)}, missing {sorted(want - found, key=str)}")
    _verify_dependencies(cur, role)
    cur.execute(ROLE_ATTRIBUTES_SQL, (role,))
    state = cur.fetchone()
    if state is None or tuple(state) != MINIMAL_ATTRIBUTES:
        raise RuntimeError(f"role {role} is missing or has privileged attributes after the setup")
    return relations


def _verify_dependencies(cur, role: str) -> None:
    """Refuse a role that owns any object (in any database), holds a privilege
    outside this database, or holds one here on a catalog the inventory does
    not list (types, languages, large objects, foreign servers, ...)."""
    cur.execute(SHDEPEND_SQL, {"role": role})
    for deptype, here, catalog in cur.fetchall():
        if deptype == "o":
            raise RuntimeError(f"role {role} owns an object ({catalog}); refusing to use it")
        if not here:
            raise RuntimeError(f"role {role} holds a privilege outside this database ({catalog})")
        if catalog not in INVENTORIED_CATALOGS:
            raise RuntimeError(f"role {role} holds a privilege the setup does not cover ({catalog})")


def drop_reader(conn, role: str) -> bool:
    """Disable the role's login, then revoke its privileges (default
    privileges included, or DROP ROLE refuses) and drop it; False when it does
    not exist. The NOLOGIN commits on its own, so a drop that fails still
    leaves a role nobody can log in as."""
    check_ident(role)
    with conn.cursor() as cur:
        cur.execute(ROLE_STATE_SQL, (role,))
        if cur.fetchone() is None:
            return False
        cur.execute(ddl("ALTER ROLE {} NOLOGIN", role))
    conn.commit()
    with conn.cursor() as cur:
        _revoke_everything(cur, role)
        cur.execute(ddl("DROP ROLE {}", role))
    conn.commit()
    return True


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


def readers_from(props: dict) -> list[dict]:
    """The configured readers, each ``{Schema, Role, SecretArn}``, validated
    before anything connects. Roles and schemas must be unique."""
    readers = props.get("Readers") or []
    if not isinstance(readers, list):
        raise ValueError("Readers must be a list")
    roles, schemas = set(), set()
    out = []
    for item in readers:
        schema, role, arn = item.get("Schema"), item.get("Role"), item.get("SecretArn")
        ident(schema)
        ident(role)
        if not isinstance(arn, str) or not arn:
            raise ValueError(f"reader {role!r} has no secret")
        if role in roles or schema in schemas:
            raise ValueError(f"reader {role!r} / schema {schema!r} is listed twice")
        roles.add(role)
        schemas.add(schema)
        out.append({"Schema": schema, "Role": role, "SecretArn": arn})
    return out


def _secrets_client():
    import boto3  # provided by the Lambda runtime
    from botocore.config import Config

    return boto3.client("secretsmanager", config=Config(
        connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 3, "mode": "standard"}))


def _secret_json(client, arn: str) -> dict:
    return json.loads(client.get_secret_value(SecretId=arn)["SecretString"])


def _connect(owner: dict):
    import pgdriver  # from the function's layer

    return pgdriver.connect(
        host=owner["host"], port=int(owner.get("port", 5432)),
        dbname=owner.get("dbname", owner.get("database")),
        user=owner["username"], password=owner["password"],
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}",
        application_name="adt-system-reader-setup",
    )


def _create_or_update(props: dict, removed: list[dict], context=None) -> None:
    readers = readers_from(props)
    client = _secrets_client()
    passwords = {}
    for reader in readers:
        value = _secret_json(client, reader["SecretArn"])
        if value.get("username") != reader["Role"]:
            raise ValueError(f"the secret for {reader['Role']} names another username")
        if not isinstance(value.get("password"), str) or not value["password"]:
            raise ValueError(f"the secret for {reader['Role']} has no password")
        passwords[reader["Role"]] = value["password"]
    owner = _secret_json(client, props["MasterSecretArn"])
    if owner.get("username") in passwords:
        raise ValueError("a reader role is the database owner login")
    conn = _connect(owner)
    try:
        for reader in readers:
            setup = functools.partial(ensure_schema_reader, conn, reader["Role"],
                                      passwords[reader["Role"]], reader["Schema"])
            relations = retry_on_conflict(conn, setup, context)
            logger.info("reader %s: %d relations in %s", reader["Role"], len(relations),
                        reader["Schema"])
        for reader in removed:
            dropped = retry_on_conflict(conn, functools.partial(drop_reader, conn, reader["Role"]), context)
            logger.info("reader %s %s", reader["Role"], "dropped" if dropped else "was already absent")
    finally:
        conn.close()


def _delete(props: dict) -> None:
    """Best effort: a delete must not block the stack (a role is left behind,
    with a password nobody else holds, only if this fails)."""
    try:
        readers = readers_from(props)
        owner = _secret_json(_secrets_client(), props["MasterSecretArn"])
        conn = _connect(owner)
    except Exception as exc:  # noqa: BLE001 - database or secret already gone
        logger.warning("reader roles not dropped: database unreachable (%s)", type(exc).__name__)
        return
    try:
        for reader in readers:
            try:
                dropped = drop_reader(conn, reader["Role"])
                logger.info("reader %s %s", reader["Role"], "dropped" if dropped else "was already absent")
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                logger.warning("reader %s not dropped (%s)", reader["Role"], type(exc).__name__)
    finally:
        conn.close()


def handler(event, context):
    """CloudFormation custom-resource entry point (CDK provider framework)."""
    props = event["ResourceProperties"]
    if event["RequestType"] == "Delete":
        _delete(props)
        return {"PhysicalResourceId": PHYSICAL_RESOURCE_ID}
    removed: list[dict] = []
    if event["RequestType"] == "Update":
        keep = {r["Role"] for r in readers_from(props)}
        try:
            old = readers_from(event.get("OldResourceProperties") or {})
        except ValueError:
            old = []
        removed = [r for r in old if r["Role"] not in keep]
    _create_or_update(props, removed, context)
    return {"PhysicalResourceId": PHYSICAL_RESOURCE_ID}
