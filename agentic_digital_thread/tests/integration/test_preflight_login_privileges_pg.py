# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The preflight login-privilege check on real PostgreSQL.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server. One schema,
three logins: the test superuser (refused as a superuser), a login with CREATE
on the schema and INSERT on a table (refused: it can change data), and a login
with USAGE + SELECT only (not refused). The check reads the catalog only.

Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_preflight_login_privileges_pg.py -q
"""
from __future__ import annotations

import os
import secrets
import sys
import uuid
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.onboard_preflight as op  # noqa: E402
from tests.integration._pg_sql import stmt  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture
def logins():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    from agents import pgdriver
    from agents.pgdriver import make_dsn, parse_dsn, sql

    tag = uuid.uuid4().hex[:8]
    db, writer, reader = f"pfl_{tag}", f"pfl_writer_{tag}", f"pfl_reader_{tag}"
    writer_pw, reader_pw = secrets.token_hex(16), secrets.token_hex(16)
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=db))
        # CREATE ROLE cannot take a bound parameter: the password is a literal.
        cur.execute(stmt("CREATE ROLE {role} LOGIN PASSWORD {pw}", role=writer, pw=sql.Literal(writer_pw)))
        cur.execute(stmt("CREATE ROLE {role} LOGIN PASSWORD {pw}", role=reader, pw=sql.Literal(reader_pw)))

    def connect(user=None, password=None):
        params = {**parse_dsn(_DSN), "dbname": db}
        if user:
            params.update(user=user, password=password)
        conn = pgdriver.connect(make_dsn(**params))
        conn.autocommit = True
        return conn

    setup = connect()
    with setup.cursor() as cur:
        cur.execute("CREATE SCHEMA plant; CREATE TABLE plant.orders (id int)")
        cur.execute(stmt("GRANT USAGE, CREATE ON SCHEMA plant TO {role}", role=writer))
        cur.execute(stmt("GRANT SELECT, INSERT ON plant.orders TO {role}", role=writer))
        cur.execute(stmt("GRANT USAGE ON SCHEMA plant TO {role}", role=reader))
        cur.execute(stmt("GRANT SELECT ON plant.orders TO {role}", role=reader))
    setup.close()
    try:
        yield {"superuser": connect(), "writer": connect(writer, writer_pw),
               "reader": connect(reader, reader_pw)}
    finally:
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE IF EXISTS {db} WITH (FORCE)", db=db))
            cur.execute(stmt("DROP ROLE IF EXISTS {role}", role=writer))
            cur.execute(stmt("DROP ROLE IF EXISTS {role}", role=reader))
        admin.close()


def test_the_superuser_login_is_refused(logins):
    refusal, warnings = op._pg_login_check(logins["superuser"], "plant")
    assert "superuser" in refusal and warnings == []


def test_a_login_that_can_write_is_refused(logins):
    refusal, warnings = op._pg_login_check(logins["writer"], "plant")
    assert "create objects or change data" in refusal and warnings == []


def test_a_select_only_login_is_not_refused(logins):
    assert op._pg_login_check(logins["reader"], "plant") == (None, [])


def test_a_missing_schema_is_not_refused_for_a_non_superuser(logins):
    assert op._pg_login_check(logins["reader"], "not_there") == (None, [])
