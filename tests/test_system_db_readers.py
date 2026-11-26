# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The system agents' read-only database logins (setup custom resource).

A system agent runs SQL its model wrote, so it connects with a login that can
read its own schema and nothing else, never as the database owner. A
deploy-time custom resource (lambda-db-reader/handler.py) keeps one such login
per schema with the owner's credential.

Offline: the custom resource events (which secrets it reads, what it refuses
before connecting, dropping a reader an Update no longer lists, a best-effort
Delete). PostgreSQL (``ERP_SIM_TEST_PG_DSN``, a throwaway server; skipped
otherwise): as a NON-superuser owner with CREATEROLE that owns the database
(as on the managed databases), the login is created with exactly CONNECT,
USAGE and SELECT on its schema plus default privileges, a second run changes
nothing, tables created later stay readable, another schema, public tables,
writes and DDL are refused, it cannot end another session, a re-run revokes
anything granted in between, and Delete drops it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import secrets as _stdlib_secrets
import sys
import types
import uuid
from pathlib import Path

import pytest

from _pg_sql import pgdriver, stmt

_REPO = Path(__file__).resolve().parents[1]
_HANDLER = _REPO / "lambda-db-reader" / "handler.py"
_PASSWORD = "s3cr3t-VALUE-reader-password"  # nosec B105 - fake fixture
_OWNER = {"host": "seed-db.example.internal", "port": 5432, "dbname": "adt_demo",
           "username": "postgres", "password": "s3cr3t-VALUE-owner-password"}  # nosec B105 - fake


@pytest.fixture
def h():
    spec = importlib.util.spec_from_file_location("system_db_reader_handler", _HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the custom resource (offline) --------------------------------------------


@pytest.fixture
def aws(monkeypatch, h):
    """Fake Secrets Manager and pgdriver; ensure/drop recorded, not run."""
    state = {"secrets": {
        "arn:owner": json.dumps(_OWNER),
        "arn:a": json.dumps({"username": "adt_reader_sys_a", "password": "pw-a"}),
        "arn:b": json.dumps({"username": "adt_reader_sys_b", "password": "pw-b"}),
    }, "reads": [], "connects": [], "ensured": [], "dropped": [], "closed": 0}

    def client(name, **kwargs):
        def get_secret_value(SecretId):
            state["reads"].append(SecretId)
            return {"SecretString": state["secrets"][SecretId]}
        return types.SimpleNamespace(get_secret_value=get_secret_value)

    class Conn:
        def close(self):
            state["closed"] += 1

        def rollback(self):
            pass

    def connect(**kwargs):
        state["connects"].append(kwargs)
        if state.get("refuse"):
            raise OSError("could not connect")
        return Conn()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "pgdriver", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(h, "ensure_schema_reader",
                        lambda conn, role, pw, schema: state["ensured"].append((role, pw, schema)) or [])
    monkeypatch.setattr(h, "drop_reader",
                        lambda conn, role: state["dropped"].append(role) or True)
    return state


def _props(*readers):
    return {"ServiceToken": "x", "MasterSecretArn": "arn:owner", "Version": "v1", "DataVersion": "d1",
            "Readers": [{"Schema": s, "Role": f"adt_reader_{s}", "SecretArn": a} for s, a in readers]}


def test_create_sets_up_every_reader_with_its_own_password(h, aws):
    out = h.handler({"RequestType": "Create", "ResourceProperties": _props(("sys_a", "arn:a"),
                                                                            ("sys_b", "arn:b"))}, None)
    assert out == {"PhysicalResourceId": h.PHYSICAL_RESOURCE_ID}
    assert aws["ensured"] == [("adt_reader_sys_a", "pw-a", "sys_a"), ("adt_reader_sys_b", "pw-b", "sys_b")]
    (kwargs,) = aws["connects"]
    assert (kwargs["user"], kwargs["password"]) == ("postgres", _OWNER["password"])
    assert "statement_timeout" in kwargs["options"] and "lock_timeout" in kwargs["options"]
    assert aws["closed"] == 1


def test_a_reader_secret_for_another_login_is_refused_before_connecting(h, aws):
    aws["secrets"]["arn:a"] = json.dumps({"username": "postgres", "password": "x"})
    with pytest.raises(ValueError, match="another username"):
        h.handler({"RequestType": "Create", "ResourceProperties": _props(("sys_a", "arn:a"))}, None)
    assert aws["connects"] == [] and aws["ensured"] == []


def test_a_reader_named_like_the_owner_login_is_refused(h, aws):
    aws["secrets"]["arn:owner"] = json.dumps({**_OWNER, "username": "adt_reader_sys_a"})
    with pytest.raises(ValueError, match="owner"):
        h.handler({"RequestType": "Create", "ResourceProperties": _props(("sys_a", "arn:a"))}, None)
    assert aws["connects"] == []


@pytest.mark.parametrize("bad", [
    [{"Schema": "Sys", "Role": "adt_reader_sys", "SecretArn": "arn:a"}],
    [{"Schema": "sys_a", "Role": 'r"x', "SecretArn": "arn:a"}],
    [{"Schema": "sys_a", "Role": "adt_reader_sys_a", "SecretArn": ""}],
    [{"Schema": "sys_a", "Role": "adt_reader_sys_a", "SecretArn": "arn:a"},
     {"Schema": "sys_a", "Role": "adt_reader_sys_b", "SecretArn": "arn:b"}],
    "not-a-list",
])
def test_configured_names_are_validated_before_any_read(h, aws, bad):
    props = {**_props(), "Readers": bad}
    with pytest.raises(ValueError):
        h.handler({"RequestType": "Create", "ResourceProperties": props}, None)
    assert aws["reads"] == [] and aws["connects"] == []


def test_update_drops_a_reader_no_longer_listed(h, aws):
    event = {"RequestType": "Update", "PhysicalResourceId": h.PHYSICAL_RESOURCE_ID,
             "ResourceProperties": _props(("sys_a", "arn:a")),
             "OldResourceProperties": _props(("sys_a", "arn:a"), ("sys_b", "arn:b"))}
    h.handler(event, None)
    assert [r for r, _, _ in aws["ensured"]] == ["adt_reader_sys_a"]
    assert aws["dropped"] == ["adt_reader_sys_b"]


def test_delete_drops_every_reader_and_tolerates_an_unreachable_database(h, aws):
    event = {"RequestType": "Delete", "PhysicalResourceId": h.PHYSICAL_RESOURCE_ID,
             "ResourceProperties": _props(("sys_a", "arn:a"), ("sys_b", "arn:b"))}
    assert h.handler(event, None)["PhysicalResourceId"] == h.PHYSICAL_RESOURCE_ID
    assert aws["dropped"] == ["adt_reader_sys_a", "adt_reader_sys_b"]
    aws["refuse"] = True
    assert h.handler(event, None)["PhysicalResourceId"] == h.PHYSICAL_RESOURCE_ID


def test_anything_but_a_scram_verifier_is_refused_before_any_ddl(h, monkeypatch):
    statements = []

    class Cursor:
        def execute(self, sql, params=None):
            statements.append(sql)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    conn = types.SimpleNamespace(cursor=Cursor, commit=lambda: None)
    for bad in (_PASSWORD, "md5abc", ""):
        monkeypatch.setattr(h, "_password_verifier", lambda c, r, p, bad=bad: bad)
        with pytest.raises(RuntimeError, match="SCRAM"):
            h.ensure_schema_reader(conn, "adt_reader_sys_a", _PASSWORD, "sys_a")
    assert statements == []


def test_the_role_statements_carry_the_verifier_as_a_literal_with_no_parameter(h):
    """A utility statement cannot bind a parameter, so CREATE/ALTER ROLE get
    the SCRAM verifier as a quoted literal, never the password."""
    pytest.importorskip("pg8000")
    pgdriver()                                            # the layer's module, importable as pgdriver
    verifier = "SCRAM-SHA-256$4096:c2FsdA==$c3RvcmVk:c2VydmVy"
    statements = []

    class Cursor:
        def __init__(self, row):
            self.row = row

        def execute(self, sql, params=None):
            statements.append((sql.as_string(None) if hasattr(sql, "as_string") else sql, params))

        def fetchone(self):
            return self.row

    h._create_or_reset(Cursor(None), "adt_reader_sys_a", verifier)
    h._create_or_reset(Cursor((False, False, False)), "adt_reader_sys_a", verifier)
    create, reset = [(s, p) for s, p in statements if " PASSWORD " in s]
    assert create == (f'CREATE ROLE "adt_reader_sys_a" WITH {h._CREATE_ATTRIBUTES} PASSWORD \'{verifier}\'', None)
    assert reset == (f'ALTER ROLE "adt_reader_sys_a" WITH {h._RESET_ATTRIBUTES} PASSWORD \'{verifier}\'', None)


@pytest.mark.parametrize("bad", ["Reader", 'r"x', "r;drop", "1r", "", "a" * 64])
def test_identifiers_are_validated_before_any_sql(h, bad):
    conn = types.SimpleNamespace(cursor=lambda: pytest.fail("no SQL may run"))
    for role, schema in ((bad, "sys_a"), ("adt_reader_sys_a", bad)):
        with pytest.raises(ValueError):
            h.ensure_schema_reader(conn, role, _PASSWORD, schema)


def test_the_inventory_covers_every_kind_it_revokes(h):
    for kind in ("table", "column", "schema", "database", "function", "default", "member"):
        assert f"'{kind}'" in h.GRANTS_SQL
    assert "%(role)s" in h.GRANTS_SQL and "aclexplode" in h.GRANTS_SQL
    assert "pg_database" in h.INVENTORIED_CATALOGS


# --- PostgreSQL ---------------------------------------------------------------

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture
def pg():
    """A throwaway database owned by a NON-superuser owner login with CREATEROLE
    (as on the managed databases), with two system schemas and a public
    table. Yields ``(connect, owner)``: ``connect(user, password)`` logs in
    to it, ``connect()`` as the test superuser."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    driver = pgdriver()
    make_dsn, parse_dsn = driver.make_dsn, driver.parse_dsn

    tag = uuid.uuid4().hex[:8]
    db_name, owner = f"sysr_{tag}", f"sysr_owner_{tag}"
    owner_pw = _stdlib_secrets.token_hex(16)
    admin = driver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE ROLE {owner} LOGIN CREATEROLE CREATEDB PASSWORD {pw}", owner=owner,
                         pw=driver.sql.Literal(owner_pw)))
        cur.execute(stmt("CREATE DATABASE {db} OWNER {owner}", db=db_name, owner=owner))

    def connect(user=None, password=None, **kwargs):
        params = {**parse_dsn(_DSN), "dbname": db_name}
        if user:
            params.update(user=user, password=password)
        return driver.connect(make_dsn(**params), **kwargs)

    seed = connect(owner, owner_pw)
    with seed.cursor() as cur:
        cur.execute("CREATE SCHEMA sys_a; CREATE SCHEMA sys_b;"
                    " CREATE TABLE sys_a.orders (id int, qty int); INSERT INTO sys_a.orders VALUES (1, 5);"
                    " CREATE VIEW sys_a.order_view AS SELECT id FROM sys_a.orders;"
                    " CREATE TABLE sys_b.secrets (id int); INSERT INTO sys_b.secrets VALUES (7);"
                    " CREATE TABLE public.shared (id int); INSERT INTO public.shared VALUES (9);")
    seed.commit()
    seed.close()
    roles = []
    try:
        yield connect, (owner, owner_pw), roles
    finally:
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE IF EXISTS {db} WITH (FORCE)", db=db_name))
            for role in roles:
                cur.execute(stmt("DROP ROLE IF EXISTS {role}", role=role))
            cur.execute(stmt("DROP ROLE IF EXISTS {role}", role=owner))
        admin.close()


def _role(roles):
    role = f"adt_reader_t{uuid.uuid4().hex[:8]}"
    roles.append(role)
    return role


def _denied(conn, sql):
    """True when ``sql`` fails for lack of privilege (not for read-only mode)."""
    with conn.cursor() as cur:
        try:
            cur.execute(sql)
        except pgdriver().Error as exc:
            conn.rollback()
            return exc.pgcode == "42501"                      # insufficient_privilege
    conn.rollback()
    return False


def _grants(connect, h, role):
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(h.GRANTS_SQL, {"role": role})
        rows = {tuple(r) for r in cur.fetchall()}
        cur.execute("SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,"
                    " rolbypassrls, rolinherit, rolconnlimit, rolconfig FROM pg_roles WHERE rolname = %s",
                    (role,))
        attrs = cur.fetchone()
        cur.execute("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member"
                    " WHERE r.rolname = %s", (role,))
        memberships = cur.fetchone()[0]
        cur.execute("SELECT current_database()")
        database = cur.fetchone()[0]
    conn.close()
    return rows, attrs, memberships, database


def test_postgres_reader_has_exactly_the_read_grant_and_a_rerun_changes_nothing(pg, h):
    connect, (owner, owner_pw), roles = pg
    role = _role(roles)
    reader_pw = _stdlib_secrets.token_hex(16)
    setup = connect(owner, owner_pw)
    assert h.ensure_schema_reader(setup, role, reader_pw, "sys_a") == ["order_view", "orders"]
    first, attrs, memberships, database = _grants(connect, h, role)
    assert first == {
        ("database", None, database, None, "CONNECT"),
        ("schema", None, "sys_a", None, "USAGE"),
        ("table", "sys_a", "orders", None, "SELECT"),
        ("table", "sys_a", "order_view", None, "SELECT"),
        ("default", "sys_a", "r", owner, "SELECT"),
    }
    assert attrs[:7] == (True, False, False, False, False, False, False)
    assert attrs[7] == h.READER_CONNECTION_LIMIT
    assert sorted(attrs[8]) == ["default_transaction_read_only=on",
                                f"statement_timeout={h.READER_STATEMENT_TIMEOUT_MS}"]
    assert memberships == 0
    h.ensure_schema_reader(setup, role, reader_pw, "sys_a")
    again, attrs_again, _, _ = _grants(connect, h, role)
    assert (again, attrs_again) == (first, attrs)
    setup.close()


def test_postgres_reader_reads_its_schema_including_tables_created_later(pg, h):
    connect, (owner, owner_pw), roles = pg
    role = _role(roles)
    reader_pw = _stdlib_secrets.token_hex(16)
    setup = connect(owner, owner_pw)
    h.ensure_schema_reader(setup, role, reader_pw, "sys_a")
    with setup.cursor() as cur:   # the owner adds a table after the setup (a later seed run)
        cur.execute("CREATE TABLE sys_a.later (id int); INSERT INTO sys_a.later VALUES (3)")
    setup.commit()
    conn = connect(role, reader_pw)
    with conn.cursor() as cur:
        for sql, expected in (("SELECT qty FROM sys_a.orders", 5), ("SELECT id FROM sys_a.order_view", 1),
                              ("SELECT id FROM sys_a.later", 3)):
            cur.execute(sql)
            assert cur.fetchone()[0] == expected, sql
    conn.close()
    setup.close()


def test_postgres_reader_cannot_read_another_systems_schema_or_write(pg, h):
    connect, (owner, owner_pw), roles = pg
    role = _role(roles)
    reader_pw = _stdlib_secrets.token_hex(16)
    setup = connect(owner, owner_pw)
    h.ensure_schema_reader(setup, role, reader_pw, "sys_a")
    conn = connect(role, reader_pw)
    with conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = off")    # even out of read-only mode
    conn.commit()
    for sql in ("SELECT * FROM sys_b.secrets",
                "SELECT * FROM public.shared",
                "INSERT INTO sys_a.orders VALUES (2, 1)",
                "UPDATE sys_a.orders SET qty = 0",
                "DELETE FROM sys_a.orders",
                "TRUNCATE sys_a.orders",
                "CREATE TABLE sys_a.x (id int)",
                "CREATE TABLE public.x (id int)",
                "CREATE SCHEMA mine"):
        assert _denied(conn, sql), sql
    conn.close()
    setup.close()


def test_postgres_reader_cannot_end_another_session(pg, h):
    connect, (owner, owner_pw), roles = pg
    role = _role(roles)
    reader_pw = _stdlib_secrets.token_hex(16)
    setup = connect(owner, owner_pw)
    h.ensure_schema_reader(setup, role, reader_pw, "sys_a")
    with setup.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        owner_pid = cur.fetchone()[0]
    setup.commit()
    conn = connect(role, reader_pw)
    assert _denied(conn, f"SELECT pg_terminate_backend({owner_pid})")
    assert _denied(conn, f"SELECT pg_cancel_backend({owner_pid})")
    conn.close()
    with setup.cursor() as cur:   # the owner's session is still alive
        cur.execute("SELECT 1")
    setup.close()


def test_postgres_rerun_revokes_what_was_granted_in_between(pg, h):
    connect, (owner, owner_pw), roles = pg
    role = _role(roles)
    first_pw, second_pw = _stdlib_secrets.token_hex(16), _stdlib_secrets.token_hex(16)
    setup = connect(owner, owner_pw)
    h.ensure_schema_reader(setup, role, first_pw, "sys_a")
    before, _, _, _ = _grants(connect, h, role)
    with setup.cursor() as cur:
        cur.execute(stmt("GRANT INSERT, UPDATE ON sys_a.orders TO {role}", role=role))
        cur.execute(stmt("GRANT USAGE ON SCHEMA sys_b TO {role}", role=role))
        cur.execute(stmt("GRANT SELECT ON sys_b.secrets TO {role}", role=role))
        cur.execute(stmt("GRANT SELECT (id) ON public.shared TO {role}", role=role))
        cur.execute(stmt("GRANT CREATE ON SCHEMA sys_a TO {role}", role=role))
        cur.execute(stmt("ALTER DEFAULT PRIVILEGES IN SCHEMA sys_b GRANT SELECT ON TABLES TO {role}", role=role))
    setup.commit()
    h.ensure_schema_reader(setup, role, second_pw, "sys_a")
    after, _, _, _ = _grants(connect, h, role)
    assert after == before
    conn = connect(role, second_pw)
    assert _denied(conn, "SELECT * FROM sys_b.secrets")
    assert _denied(conn, "SELECT id FROM public.shared")
    conn.close()
    with pytest.raises(pgdriver().OperationalError):
        connect(role, first_pw)                                   # the old password is gone
    setup.close()


def test_postgres_a_reset_that_recreates_the_schema_is_restored_by_the_re_run(pg, h):
    """The ERP initialisation drops each view schema CASCADE and recreates
    it over the public tables. That drop succeeds with a reader present, the
    reader then reads nothing until the setup re-runs (the stack re-runs it
    after every initialisation), and afterwards it reads the views (the view
    owner's rights reach the public tables) but still not the tables."""
    connect, (owner, owner_pw), roles = pg
    role = _role(roles)
    reader_pw = _stdlib_secrets.token_hex(16)
    setup = connect(owner, owner_pw)
    with setup.cursor() as cur:
        cur.execute("CREATE SCHEMA site_v; CREATE VIEW site_v.shared_view AS SELECT id FROM public.shared")
    setup.commit()
    h.ensure_schema_reader(setup, role, reader_pw, "site_v")
    with setup.cursor() as cur:
        cur.execute("DROP SCHEMA site_v CASCADE; CREATE SCHEMA site_v;"
                    " CREATE VIEW site_v.shared_view AS SELECT id FROM public.shared")
    setup.commit()
    conn = connect(role, reader_pw)
    assert _denied(conn, "SELECT id FROM site_v.shared_view")
    conn.close()
    assert h.ensure_schema_reader(setup, role, reader_pw, "site_v") == ["shared_view"]
    conn = connect(role, reader_pw)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM site_v.shared_view")
        assert cur.fetchone()[0] == 9
    conn.rollback()
    assert _denied(conn, "SELECT id FROM public.shared")
    conn.close()
    setup.close()


def test_postgres_a_missing_schema_gets_connect_only(pg, h):
    connect, (owner, owner_pw), roles = pg
    role = _role(roles)
    setup = connect(owner, owner_pw)
    assert h.ensure_schema_reader(setup, role, _stdlib_secrets.token_hex(16), "not_there") == []
    rows, _, _, database = _grants(connect, h, role)
    assert rows == {("database", None, database, None, "CONNECT")}
    setup.close()


def test_postgres_delete_drops_the_reader_and_a_second_delete_is_harmless(pg, h):
    connect, (owner, owner_pw), roles = pg
    role = _role(roles)
    setup = connect(owner, owner_pw)
    h.ensure_schema_reader(setup, role, _stdlib_secrets.token_hex(16), "sys_a")
    assert h.drop_reader(setup, role) is True
    assert h.drop_reader(setup, role) is False
    with setup.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (role,))
        assert cur.fetchone()[0] == 0
    setup.close()


def test_postgres_the_server_only_ever_sees_a_scram_verifier(pg, h, monkeypatch):
    connect, (owner, owner_pw), roles = pg
    role = _role(roles)
    reader_pw = _stdlib_secrets.token_hex(16)
    log: list = []
    driver = pgdriver()
    real_execute = driver.Cursor.execute

    def recording(self, query, vars=None):
        text = query if isinstance(query, str) else query.as_string(self.connection)
        log.append((text, vars))
        return real_execute(self, query, vars)

    monkeypatch.setattr(driver.Cursor, "execute", recording)
    setup = connect(owner, owner_pw)
    for _ in range(2):                                    # create, then reset
        h.ensure_schema_reader(setup, role, reader_pw, "sys_a")
    setup.close()
    # A role statement cannot bind: the verifier is the statement's quoted literal.
    ddl = [query for query, vars in log if " PASSWORD " in query]
    assert len(ddl) == 2 and all(" PASSWORD 'SCRAM-SHA-256$" in q for q in ddl)
    assert all(vars is None for query, vars in log if " PASSWORD " in query)
    assert not any(reader_pw in f"{q} {v}" for q, v in log)


def test_postgres_the_real_seed_gets_one_reader_per_schema_each_confined_to_it(pg, h):
    """The bundled seed SQL, applied by the owner as the seed Lambda does, then
    the setup for every seed schema the stack lists: each reader reads every
    relation of its own schema and is refused the next schema's."""
    sys.path.insert(0, str(_REPO))
    from stacks.adt_seed_data_stack import SEED_READER_SCHEMAS

    connect, (owner, owner_pw), roles = pg
    setup = connect(owner, owner_pw)
    setup.autocommit = True
    with setup.cursor() as cur:
        for path in sorted((_REPO / "lambda-adt-seed").glob("*.sql")):
            cur.execute(path.read_text(encoding="utf-8"))
    setup.autocommit = False
    logins = {}
    for schema in SEED_READER_SCHEMAS:
        role = _role(roles)
        logins[schema] = (role, _stdlib_secrets.token_hex(16))
        relations = h.ensure_schema_reader(setup, role, logins[schema][1], schema)
        assert relations, schema
        logins[schema] += (relations,)
    for index, schema in enumerate(SEED_READER_SCHEMAS):
        role, password, relations = logins[schema]
        other = SEED_READER_SCHEMAS[(index + 1) % len(SEED_READER_SCHEMAS)]
        conn = connect(role, password)
        with conn.cursor() as cur:
            for relation in relations:
                cur.execute(stmt("SELECT 1 FROM {rel} LIMIT 1", rel=(schema, relation)))
        conn.rollback()
        assert _denied(conn, stmt("SELECT 1 FROM {rel} LIMIT 1", rel=(other, logins[other][2][0]))), (schema, other)
        conn.close()
    setup.close()


def test_postgres_the_real_erp_database_gets_one_reader_per_view_schema(pg, h):
    """The ERP database as lambda-db-init builds it (tables in public, one
    view schema per system), by the owner: each view-schema reader reads its
    own views (the views reach the public tables with the owner's rights),
    and is refused the public tables and the next system's views."""
    pytest.importorskip("boto3")                  # lambda-db-init imports it at module load
    sys.path.insert(0, str(_REPO))
    from stacks.erp_database_stack import ERP_READER_SCHEMAS

    spec = importlib.util.spec_from_file_location("db_init_for_readers",
                                                  _REPO / "lambda-db-init" / "handler.py")
    db_init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(db_init)
    connect, (owner, owner_pw), roles = pg
    setup = connect(owner, owner_pw)
    db_init.create_schema(setup)
    db_init.seed_data(setup)
    logins = {}
    for schema in ERP_READER_SCHEMAS:
        role = _role(roles)
        password = _stdlib_secrets.token_hex(16)
        logins[schema] = (role, password, h.ensure_schema_reader(setup, role, password, schema))
        assert logins[schema][2], schema
    for index, schema in enumerate(ERP_READER_SCHEMAS):
        role, password, relations = logins[schema]
        other = ERP_READER_SCHEMAS[(index + 1) % len(ERP_READER_SCHEMAS)]
        conn = connect(role, password)
        with conn.cursor() as cur:
            for relation in relations:
                cur.execute(stmt("SELECT 1 FROM {rel} LIMIT 1", rel=(schema, relation)))
        conn.rollback()
        assert _denied(conn, stmt("SELECT 1 FROM {rel} LIMIT 1", rel=(other, logins[other][2][0]))), (schema, other)
        assert _denied(conn, "SELECT 1 FROM public.customers LIMIT 1"), schema
        conn.close()
    setup.close()


def test_postgres_a_seed_only_update_adds_a_view_the_existing_reader_can_read(pg, h):
    """An ERP database whose readers were set up before the MES equipment
    registry existed: the seed-only Update (``seed_catalogue_upsert``), run by
    the owner, adds every site schema's ``equipment`` view, and each schema's
    existing reader can read it through the default privileges its setup
    granted, with no new setup run; the base table stays refused."""
    pytest.importorskip("boto3")                  # lambda-db-init imports it at module load
    sys.path.insert(0, str(_REPO))
    from stacks.erp_database_stack import ERP_READER_SCHEMAS

    spec = importlib.util.spec_from_file_location("db_init_for_reader_upsert",
                                                  _REPO / "lambda-db-init" / "handler.py")
    db_init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(db_init)
    connect, (owner, owner_pw), roles = pg
    setup = connect(owner, owner_pw)
    db_init.create_schema(setup)
    db_init.seed_data(setup)
    site_schemas = [s for s in ERP_READER_SCHEMAS if s.startswith("mes_")]
    with setup.cursor() as cur:                   # the shape before the equipment registry
        for schema in site_schemas:
            cur.execute(stmt("DROP VIEW IF EXISTS {rel}", rel=(schema, "equipment")))
        cur.execute("DROP TABLE IF EXISTS public.mes_equipment")
    setup.commit()
    logins = {}
    for schema in site_schemas:
        role, password = _role(roles), _stdlib_secrets.token_hex(16)
        h.ensure_schema_reader(setup, role, password, schema)
        logins[schema] = (role, password)
    db_init.seed_catalogue_upsert(setup)
    for schema, (role, password) in logins.items():
        conn = connect(role, password)
        with conn.cursor() as cur:
            cur.execute(stmt("SELECT count(*) FROM {rel}", rel=(schema, "equipment")))
            assert cur.fetchone()[0] == 3, schema
        conn.rollback()
        assert _denied(conn, "SELECT 1 FROM public.mes_equipment LIMIT 1"), schema
        conn.close()
    setup.close()
