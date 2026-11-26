# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Plan v2 Task 1.2 / ADR-0044 Decision 2: deploy.sh publishes the
``/adt/demo/{seed-db,erp-db}`` SSM pointers.

``deploy.sh`` must publish the demo DB connection POINTERS — JSON
``{host,port,dbname,secret_arn}`` sourced from the demo stacks'
CloudFormation outputs — when the corresponding data flag is active
(``--seed`` → seed-db, ``--simulator`` → erp-db), and DELETE them on a
flagless deploy (a non-sticky mirror of the ADR-0030 data flags, so a base
deploy leaves no stale demo pointer behind). Only the Secrets Manager secret
**ARN** (a pointer) may ever be written — never a secret value.

Tripwire tests read deploy.sh as text (same convention as
``test_save_correlation_insight_and_deploy_refresh.py``) so a regression —
dropping the delete branch, gating on the wrong flag, or leaking a secret
value into the parameter — cannot land silently.
"""

import json
import re
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]  # agentic_digital_thread/

_SECTION_HEADER = "Demo DB SSM pointers"
_NEXT_HEADER = "2. Seed Neptune Analytics graph"


def _deploy_sh() -> str:
    return (_ADT_ROOT / "deploy.sh").read_text()


def _section() -> str:
    """The demo-DB-pointer section of deploy.sh, bounded by its own header
    comment and the next numbered section header."""
    src = _deploy_sh()
    start = src.find(_SECTION_HEADER)
    assert start != -1, "deploy.sh demo DB SSM pointer section not found"
    end = src.find(_NEXT_HEADER, start)
    assert end != -1, "section terminator (Neptune seed header) not found"
    return src[start:end]


def _extract_json_value(section: str, param: str) -> dict:
    """Extract the ``--value "..."`` JSON for the given put-parameter, unescape
    the shell quoting, substitute shell variables with dummies, and parse it."""
    m = re.search(
        r"put-parameter --name \"" + re.escape(param) + r"\"\s*\\\n\s*--value \"(.*?)\"\s*\\\n",
        section,
    )
    assert m, f"put-parameter for {param} with a --value JSON not found"
    raw = m.group(1).replace('\\"', '"')
    raw = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", "dummy", raw)
    return json.loads(raw)


# --- publication: puts gated on the data flags -------------------------------

def test_seed_db_put_is_gated_on_with_seed_and_sourced_from_seed_stack():
    section = _section()
    gate = section.find('if [[ "$WITH_SEED" == "true" ]]')
    put = section.find('put-parameter --name "/adt/demo/seed-db"')
    assert gate != -1, "WITH_SEED gate not found"
    assert put != -1, "seed-db put-parameter not found"
    assert gate < put, "seed-db put must be inside the WITH_SEED branch"
    # Sourced from ADTSeedDataStack outputs (the smoketest CFN-lookup idiom).
    assert '"ADTSeedDataStack" "ADTRdsEndpoint"' in section
    assert '"ADTSeedDataStack" "ADTRdsSecretArn"' in section


def test_erp_db_put_is_gated_on_with_simulator_and_sourced_from_erp_stack():
    section = _section()
    gate = section.find('if [[ "$WITH_SIMULATOR" == "true" ]]')
    put = section.find('put-parameter --name "/adt/demo/erp-db"')
    assert gate != -1, "WITH_SIMULATOR gate not found"
    assert put != -1, "erp-db put-parameter not found"
    assert gate < put, "erp-db put must be inside the WITH_SIMULATOR branch"
    assert '"ADTERPDatabaseStack" "ClusterEndpoint"' in section
    assert '"ADTERPDatabaseStack" "ClusterPort"' in section
    assert '"ADTERPDatabaseStack" "SecretArn"' in section


@pytest.mark.parametrize("param", [
    "/adt/demo/seed-db",
    "/adt/demo/erp-db",
])
def test_puts_use_overwrite(param):
    section = _section()
    m = re.search(
        r"put-parameter --name \"" + re.escape(param) + r"\"(.*?)> /dev/null",
        section, re.DOTALL,
    )
    assert m, f"put-parameter for {param} not found"
    assert "--overwrite" in m.group(0), f"{param} put must use --overwrite"


def test_puts_guard_on_resolved_cfn_values():
    """The existing [ -n "$X" ] && [ "$X" != "None" ] idiom must guard both
    puts — an unresolved stack output must never publish a broken pointer."""
    section = _section()
    assert '[ "$DEMO_SEED_DB_HOST" != "None" ]' in section
    assert '[ "$DEMO_SEED_DB_SECRET_ARN" != "None" ]' in section
    assert '[ "$DEMO_ERP_DB_HOST" != "None" ]' in section
    assert '[ "$DEMO_ERP_DB_SECRET_ARN" != "None" ]' in section


# --- JSON shape: exactly the keys agents/demo/db_routing.py reads ------------

def test_seed_db_value_is_json_with_pointer_keys():
    value = _extract_json_value(_section(), "/adt/demo/seed-db")
    assert set(value) == {"host", "port", "dbname", "secret_arn"}
    assert value["port"] == "5432"
    assert value["dbname"] == "adt_demo"


def test_erp_db_value_is_json_with_pointer_keys():
    value = _extract_json_value(_section(), "/adt/demo/erp-db")
    assert set(value) == {"host", "port", "dbname", "secret_arn"}
    assert value["dbname"] == "erp_simulation"


# --- non-sticky mirror: flagless deploys DELETE the pointers ------------------

@pytest.mark.parametrize("param", [
    "/adt/demo/seed-db",
    "/adt/demo/erp-db",
])
def test_flag_absent_deletes_the_pointer(param):
    """Each put branch must have an else branch that delete-parameter's the
    pointer (ParameterNotFound tolerated via || true) — the belt-and-suspenders
    against ADR-0030's non-sticky-flags footgun."""
    section = _section()
    m = re.search(
        r"delete-parameter --name \"" + re.escape(param) + r"\"(.*?)\|\| true",
        section, re.DOTALL,
    )
    assert m, f"delete-parameter for {param} with '|| true' not found"


def test_deletes_are_in_the_else_branches():
    section = _section()
    seed_else = section.find("else", section.find('if [[ "$WITH_SEED" == "true" ]]'))
    seed_del = section.find('delete-parameter --name "/adt/demo/seed-db"')
    assert -1 < seed_else < seed_del, "seed-db delete must be in the WITH_SEED else branch"
    sim_gate = section.find('if [[ "$WITH_SIMULATOR" == "true" ]]')
    sim_else = section.find("else", sim_gate)
    sim_del = section.find('delete-parameter --name "/adt/demo/erp-db"')
    assert -1 < sim_else < sim_del, "erp-db delete must be in the WITH_SIMULATOR else branch"


# --- never a secret VALUE -----------------------------------------------------

def test_section_never_reads_or_writes_secret_values():
    """The section may reference the secret ARN output (a pointer) only — it
    must never call get-secret-value or embed password-shaped keys."""
    section = _section()
    assert "get-secret-value" not in section
    for forbidden in ("password", "\"username\"", "SecretString"):
        assert forbidden not in section, f"secret-value material ({forbidden}) in the pointer section"


# --- pointer names match the demo bootstrap reader ----------------------------

def test_param_names_match_db_routing_reader():
    """agents/demo/db_routing.py reads /adt/demo/{kind} for
    kind in {seed-db, erp-db}; deploy.sh must publish exactly those names."""
    routing_src = (_ADT_ROOT / "agents" / "demo" / "db_routing.py").read_text()
    assert "/adt/demo/" in routing_src
    section = _section()
    assert 'put-parameter --name "/adt/demo/seed-db"' in section
    assert 'put-parameter --name "/adt/demo/erp-db"' in section
