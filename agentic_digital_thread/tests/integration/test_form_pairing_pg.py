# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The form-pairing read (``form_pairing``) on a real PostgreSQL.

The unit suite fakes the database. Here the read runs as the system runtime
runs it: the composed SELECT, a read-only session with the connect and
statement timeouts, ``search_path`` set to the system's schema, both value
lists bound. The graph is a fixture (the stored pairing JoinPath, the two
measured exact joins and the columns' stored concept mappings), so only the
database is real. Pinned:

* the read returns the stored pairs of the requested items from either form,
  and one hop more: the other item a requested item's counterpart also
  pairs with, so the caller can see the ambiguity;
* the session is read only: a write on it fails;
* a same-named table in another schema is not read.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server it may
``CREATE``/``DROP DATABASE`` on (the convention of ``test_erp_join_keys_pg.py``).
Neutral schema; no dataset literals.
"""
from __future__ import annotations

import json
import os
import secrets
from urllib.parse import urlparse

import pytest

from agents import form_pairing as fp

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
SID, SCHEMA = "cell-x", "s_cell"
PHYSICAL = {"jobs": {"columns": {"job_id": "uuid", "job_code": "character varying",
                                 "build_id": "uuid"}},
            "builds": {"columns": {"build_id": "uuid", "build_code": "character varying"}}}
PAIRING = {"id": "jp-int-1", "from_table": f"{SCHEMA}.jobs", "from_field": "build_id",
           "to_table": f"{SCHEMA}.builds", "to_field": "build_id",
           "method": "foreign_key_match", "basis": "declared_foreign_key"}


def _exact(table, column, shape, form_concept, outbound=True):
    """A measured exact join, agreed with the peer's batch column through a
    catalog-related pair, so its label records the form's own concept."""
    own = (SID, f"{SCHEMA}.{table}", column)
    other = ("ledger-x", "s_led.log", "unit_ref")
    a, b = (own, other) if outbound else (other, own)
    return {"from_system": a[0], "from_table": a[1], "from_field": a[2], "to_system": b[0],
            "to_table": b[1], "to_field": b[2], "transform": "identity",
            "provenance": f"exact values (membership-probed); both columns shape {shape}; "
                          f"concept batch (catalog-related {form_concept}); sampled"}


ENDPOINTS = [_exact("jobs", "job_code", "AA-9999-999", "work-order"),
             _exact("builds", "build_code", "AAA99999", "production-run", outbound=False)]
#: The columns' stored concept mappings (concepts on the crossing allowlist;
#: each endpoint exactly its form's concept).
CONCEPTS = [{"field": f"{SID}.{t}.{c}", "concept": concept, "role": role, "retired": False}
            for t, c, concept, role in (
                ("jobs", "job_code", "work-order", "identifier"),
                ("builds", "build_code", "production-run", "identifier"),
                ("jobs", "build_id", "production-run", "correlate"),
                ("builds", "build_id", "production-run", "identifier"))]


def _graph(cypher, params):
    if cypher == fp.PAIRING_CYPHER:
        return [PAIRING] if params["id"] == PAIRING["id"] else []
    if cypher == fp.CONCEPTS_CYPHER:
        return [row for row in CONCEPTS if row["field"] in params["fields"]]
    return list(ENDPOINTS)


def _params(dsn: str) -> dict:
    url = urlparse(dsn)
    return {"host": url.hostname, "port": url.port or 5432, "user": url.username,
            "password": url.password, "dbname": url.path.lstrip("/") or "postgres"}


@pytest.fixture(scope="module")
def database():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    from agents import pgdriver
    name = f"form_pairing_{secrets.token_hex(4)}"
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {name}")
    params = {**_params(_DSN), "dbname": name}
    try:
        _seed(pgdriver.connect(**params))
        yield params
    finally:
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


def _seed(conn) -> None:
    builds = {"BLD00412": "b0000000-0000-0000-0000-000000000412",
              "BLD00413": "b0000000-0000-0000-0000-000000000413",
              "BLD00419": "b0000000-0000-0000-0000-000000000419",
              "BLD00500": "b0000000-0000-0000-0000-000000000500"}
    jobs = [("JB-4471-001", "BLD00412"), ("JB-4471-002", "BLD00413"),
            ("JB-4472-005", "BLD00419"), ("JB-4480-001", "BLD00500"),
            ("JB-4480-002", "BLD00500")]                  # one build, two jobs
    with conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"CREATE TABLE {SCHEMA}.builds (build_id uuid PRIMARY KEY, "
                    "build_code varchar(16) NOT NULL)")
        cur.execute(f"CREATE TABLE {SCHEMA}.jobs (job_id uuid PRIMARY KEY DEFAULT "
                    "gen_random_uuid(), job_code varchar(16) NOT NULL, "
                    f"build_id uuid REFERENCES {SCHEMA}.builds(build_id))")
        for code, bid in builds.items():
            cur.execute(f"INSERT INTO {SCHEMA}.builds VALUES (%s, %s)", (bid, code))
        for job, build in jobs:
            cur.execute(f"INSERT INTO {SCHEMA}.jobs (job_code, build_id) VALUES (%s, %s)",
                        (job, builds[build]))
        # A same-named table elsewhere that must never be read.
        cur.execute("CREATE TABLE public.jobs (job_code varchar(16), build_id uuid)")
        cur.execute("INSERT INTO public.jobs VALUES ('JB-4471-001', %s)", (builds["BLD00413"],))
    conn.close()


def _answer(params, values):
    reply = fp.answer(fp.request_arg(PAIRING["id"], values), system_id=SID,
                      connection_type="PostgreSQL", schema=SCHEMA, physical=PHYSICAL,
                      conn_params=lambda: params, read_graph=_graph)
    return json.loads(reply)


def test_the_read_returns_the_stored_pairs_from_either_form(database):
    reply = _answer(database, ["JB-4471-001", "JB-4471-002", "BLD00419"])
    assert reply["status"] == "ok" and reply["fields"] == ["jobs.job_code", "builds.build_code"]
    assert sorted(map(tuple, reply["pairs"])) == [
        ("JB-4471-001", "BLD00412"), ("JB-4471-002", "BLD00413"), ("JB-4472-005", "BLD00419")]
    assert reply["cut"] is False and reply["dropped"] == 0


def test_the_read_returns_the_other_item_a_counterpart_also_pairs_with(database):
    reply = _answer(database, ["JB-4480-001"])
    assert sorted(map(tuple, reply["pairs"])) == [
        ("JB-4480-001", "BLD00500"), ("JB-4480-002", "BLD00500")]


def test_a_value_with_no_row_is_simply_absent(database):
    reply = _answer(database, ["JB-4471-001", "JB-9999-999"])
    assert reply["pairs"] == [["JB-4471-001", "BLD00412"]]


def test_the_session_is_read_only(database):
    from agents import pgdriver
    conn = fp.connect(database)
    try:
        with pytest.raises(pgdriver.InternalError) as refused:
            conn.cursor().execute(f"INSERT INTO {SCHEMA}.builds VALUES (gen_random_uuid(), 'X')")
        assert refused.value.pgcode == "25006"  # read_only_sql_transaction
        cur = conn.cursor()
        cur.execute("SHOW transaction_read_only")
        assert cur.fetchone()[0] == "on"
    finally:
        conn.close()
