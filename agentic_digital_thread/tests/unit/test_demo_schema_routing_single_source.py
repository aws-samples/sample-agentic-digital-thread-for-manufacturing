# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Single-source gate for the demo schema→database routing (ADR-0044 Decision 2).

Incident narrative (2026-07-14, mgmt env — WHY this gate exists): a cmms
re-onboard 500'd with localhost:5432. Investigation initially blamed the legacy
catalog DSN-builder prefix tuple (its docstring wrongly claimed cmms_*/plm_*
route to the ERP database) — but the tuple was CORRECT: cmms_maintenance /
plm_engineering are created by lambda-adt-seed in the demo database. The real
failure was client-side (``scripts/reonboard.py`` derived connection strings
from the local shell env; POSTGRES_* was unset). The routing table then turned
out to exist in FOUR drifting copies (catalog.py, db.py,
connection_resolver.py, the onboarding smoketest) — a day of confusion.

Plan ``sa.plan-demo-agnostic-engine-plan-v2`` (Tasks 2.1–2.4, 3.2) collapsed
every copy into ``agents/demo/db_routing.py`` — the ONE sanctioned home for
demo DB knowledge (owner directive 2026-07-14). This file supersedes
``test_pg_conn_str_schema_routing.py`` (same incident, stronger invariant):

  * **grep gate** — NO routing-tuple assignment may reappear in core
    ``agents/*.py`` (outside ``agents/demo/``) or in the smoketest; the deleted
    catalog DSN builder must stay deleted from ``catalog.py``/``onboard.py``;
    the smoketest must import the single source.
  * **behavior pins** — the env-fallback routing itself (ERP-family schemas →
    ERP database; lambda-adt-seed schemas → demo database) via the demo module AND
    via ``db._get_conn_params_for_schema``. Records-first behavior is covered
    separately in ``test_db_conn_params_records_first.py``.
"""
import re
from pathlib import Path

import pytest

import agents.db as db
from agents.catalog import SystemCatalog
from agents.demo.db_routing import ERP_SCHEMA_PREFIXES, demo_pg_dsn, is_erp_schema

_ADT_ROOT = Path(__file__).resolve().parents[2]  # agentic_digital_thread/
_REPO_ROOT = _ADT_ROOT.parent
_AGENTS_DIR = _ADT_ROOT / "agents"
_SMOKETEST = _REPO_ROOT / "scripts" / "onboarding_smoketest.py"

# Token-assembled forbidden names so this gate file itself never matches a
# repo-wide grep for them.
_TUPLE_NAMES = (
    "erp_" + "schemas",
    "_?ERP_SCHEMA_" + "PREFIXES",
    "erp_" + "prefixes",
)
_ROUTING_TUPLE_RE = re.compile(
    r"(?:%s)(?:\s*:\s*tuple)?\s*=\s*\(" % "|".join(_TUPLE_NAMES)
)
_DSN_BUILDER_TOKEN = "_pg_conn" + "_str"

ERP_FAMILY = ["erp_data", "mes_milwaukee", "mes_detroit", "mes_cedarrapids", "public"]
DEMO_SEED = ["cmms_maintenance", "plm_engineering", "adt_demo"]


# --------------------------------------------------------------------------- #
# Grep gate — the single-source invariant (fails if anyone re-adds a copy).
# --------------------------------------------------------------------------- #

def _core_agents_files():
    return [p for p in _AGENTS_DIR.rglob("*.py") if "demo" not in p.parts]


def test_no_routing_tuple_in_core_agents_files():
    offenders = [
        str(p.relative_to(_ADT_ROOT))
        for p in _core_agents_files()
        if _ROUTING_TUPLE_RE.search(p.read_text())
    ]
    assert not offenders, (
        f"routing tuple re-grew in core agents files: {offenders} — the ONE "
        "sanctioned home is agents/demo/db_routing.py (ADR-0044 Decision 2)"
    )


def test_dsn_builder_stays_deleted_from_catalog_and_onboard():
    for name in ("catalog.py", "onboard.py"):
        text = (_AGENTS_DIR / name).read_text()
        assert _DSN_BUILDER_TOKEN not in text, (
            f"agents/{name} re-grew the deleted catalog DSN builder — use "
            "agents.demo.db_routing.demo_pg_dsn"
        )


def test_smoketest_imports_the_single_source():
    text = _SMOKETEST.read_text()
    assert not _ROUTING_TUPLE_RE.search(text), (
        "onboarding_smoketest.py re-grew a local routing tuple"
    )
    assert "from agents.demo.db_routing import" in text
    assert "is_erp_schema" in text


def test_canonical_tuple_is_the_only_definition():
    assert ERP_SCHEMA_PREFIXES == ("erp_", "mes_", "public")
    # The class of drift that bit us: seed-DB families must never appear.
    assert not {"cmms_", "plm_"} & set(ERP_SCHEMA_PREFIXES), (
        "canonical tuple routes seed-DB schemas to ERP"
    )
    defs = [
        p
        for base in (_AGENTS_DIR, _REPO_ROOT / "scripts")
        for p in sorted(base.rglob("*.py"))
        if _ROUTING_TUPLE_RE.search(p.read_text())
    ]
    assert defs == [_AGENTS_DIR / "demo" / "db_routing.py"], (
        f"routing tuple defined outside the single source: {[str(p) for p in defs]}"
    )


# --------------------------------------------------------------------------- #
# Behavior pins — env-fallback routing (records-first is covered elsewhere).
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _routing_env(monkeypatch):
    """Deterministic env, empty catalog, cold cache, NO SSM fallback."""
    db._SCHEMA_CONN_CACHE.clear()
    db._RECORDS_FIRST_GUARD.active = False
    import agents.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "catalog", SystemCatalog([]))
    import agents.demo.db_routing as db_routing
    monkeypatch.setattr(db_routing, "_demo_db_pointer", lambda kind: {})
    for var in ("ERP_POSTGRES_PORT", "ERP_POSTGRES_DB", "ERP_POSTGRES_USER",
                "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    monkeypatch.setenv("ERP_POSTGRES_PASSWORD", "test-pw")  # noqa: S105 - test-only placeholder
    monkeypatch.setenv("POSTGRES_HOST", "adt.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-pw")  # noqa: S105 - test-only placeholder
    yield
    db._SCHEMA_CONN_CACHE.clear()


@pytest.mark.parametrize("schema", ERP_FAMILY)
def test_erp_family_schemas_route_to_erp_database(schema):
    assert is_erp_schema(schema)
    assert "erp.example.com" in demo_pg_dsn(schema)


@pytest.mark.parametrize("schema", DEMO_SEED)
def test_demo_seed_schemas_route_to_vf_database(schema):
    # cmms_maintenance / plm_engineering are lambda-adt-seed schemas — demo
    # database, NOT ERP (the docstring lie behind the original incident).
    assert not is_erp_schema(schema)
    assert "adt.example.com" in demo_pg_dsn(schema)


@pytest.mark.parametrize("schema", ERP_FAMILY)
def test_db_env_fallback_routes_erp_family_to_erp(schema):
    assert db._get_conn_params_for_schema(schema)["host"] == "erp.example.com"


@pytest.mark.parametrize("schema", DEMO_SEED)
def test_db_env_fallback_routes_demo_seed_to_vf(schema):
    assert db._get_conn_params_for_schema(schema)["host"] == "adt.example.com"
