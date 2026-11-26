# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 1.1 (demo-agnostic engine plan v2): ``agents.demo.db_routing`` is the ONE
demo schema→DB routing module.

Pins the plan's acceptance criteria:
(a) with today's env set, ``demo_pg_dsn``/``demo_conn_params_for_schema``/
    ``demo_db_secret_arn`` are behavior-identical to the legacy copies
    (the legacy catalog DSN builder — deleted by Task 2.2, now a hardcoded
    pin — ``db._get_conn_params_for_schema``,
    ``connection_resolver.db_secret_arn_for_schema``);
(b) env absent + SSM pointer present → pointer values used, routed correctly;
(c) both absent → ``None``/``""``/fail-closed-password behavior, and a boto3
    tripwire proves SSM is NOT called when env is set;
(d) module import is pure (no boto3 at import time) and the one-way demo seam
    holds (no ``agents.db``/``agents.catalog``/``agents.main`` imports).
"""

import os
import re
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.demo import db_routing
from agents.errors import MissingDbPasswordError

ERP_SCHEMAS = ["erp_data", "mes_milwaukee", "public"]
DEMO_SCHEMAS = ["cmms_maintenance", "plm_engineering", "adt_demo"]

_ENV_KEYS = (
    "ERP_POSTGRES_HOST", "ERP_POSTGRES_PORT", "ERP_POSTGRES_DB",
    "ERP_POSTGRES_USER", "ERP_POSTGRES_PASSWORD",
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
    "POSTGRES_USER", "POSTGRES_PASSWORD",
    "ERP_DB_SECRET_ARN", "DB_SECRET_ARN",
)

SEED_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt-seed-db-AbCdEf"
ERP_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt-erp-db-GhIjKl"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test from a known-empty routing env and a COLD pointer cache."""
    db_routing._POINTER_CACHE.clear()
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    db_routing._POINTER_CACHE.clear()


@pytest.fixture
def _pointer_spy(monkeypatch):
    """Replace the SSM pointer read with a recorder returning {} — any call is
    visible; tests asserting the env-wins tripwire require calls == []."""
    calls: list[str] = []
    monkeypatch.setattr(
        db_routing, "_demo_db_pointer", lambda kind: (calls.append(kind), {})[1]
    )
    return calls


@pytest.fixture
def _todays_env(monkeypatch):
    """The full env the deployed demo app/agents task exports today."""
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    monkeypatch.setenv("ERP_POSTGRES_PORT", "5432")
    monkeypatch.setenv("ERP_POSTGRES_DB", "erp_simulation")
    monkeypatch.setenv("ERP_POSTGRES_USER", "erp_user")
    monkeypatch.setenv("ERP_POSTGRES_PASSWORD", "erp-pw")  # noqa: S105 - test-only placeholder
    monkeypatch.setenv("POSTGRES_HOST", "adt.example.com")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "adt_demo")
    monkeypatch.setenv("POSTGRES_USER", "demo_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "demo-pw")  # noqa: S105 - test-only placeholder
    monkeypatch.setenv("ERP_DB_SECRET_ARN", ERP_ARN)
    monkeypatch.setenv("DB_SECRET_ARN", SEED_ARN)


# ---------------------------------------------------------------------------
# Canonical constants
# ---------------------------------------------------------------------------

def test_canonical_prefix_tuple():
    """The single canonical copy — the minimal set (erp_data/mes_milwaukee/…
    are covered by erp_/mes_; the widened catalog.py tuple was redundant)."""
    assert db_routing.ERP_SCHEMA_PREFIXES == ("erp_", "mes_", "public")


@pytest.mark.parametrize("schema", ERP_SCHEMAS + ["mes_detroit", "mes_cedarrapids", "erp_"])
def test_is_erp_schema_true(schema):
    assert db_routing.is_erp_schema(schema)


@pytest.mark.parametrize("schema", DEMO_SCHEMAS + ["", None])
def test_is_erp_schema_false(schema):
    assert not db_routing.is_erp_schema(schema)


def test_only_the_frozen_storyline_mes_is_seed_resident():
    """One ERP (ADR-0084): the seed carries no plant ERP any more, so the only
    ERP-family schema that lives in the seed DB is the frozen storyline MES.
    A plant-ERP schema name (derived from the plant's maintenance schema) is
    no longer special and follows the prefix rule to the ERP database."""
    from agents.demo import workshop
    assert db_routing.SEED_RESIDENT_ERP_FAMILY_SCHEMAS == frozenset({"mes_line3"})
    assert db_routing.is_erp_schema("mes_line3") is False
    plant_token = workshop.WORKSHOP_REGISTER_ONLY_SYSTEMS[0]["schema"].split("_", 1)[1]
    assert db_routing.is_erp_schema(f"erp_{plant_token}") is True
    # The live storyline MES is a simulator view in the ERP database.
    assert db_routing.is_erp_schema(workshop.LIVE_MES_SYSTEM["schema"]) is True


# ---------------------------------------------------------------------------
# (a) Parity with the legacy copies under today's env — plus the SSM tripwire
#     (env set → the pointer read must never fire).
#     The direct legacy-import comparisons are the strongest pin while the
#     legacy copies exist; Wave 2 (Tasks 2.2/2.3/3.2) deletes/delegates them
#     and owns updating these to the hardcoded pins below.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("schema", ERP_SCHEMAS)
def test_demo_pg_dsn_erp_hardcoded_pin(_todays_env, _pointer_spy, schema):
    """Hardcoded pin of the legacy catalog DSN-builder behavior (the legacy copy
    was deleted by Task 2.2; this pin replaces the direct-import parity test)."""
    assert db_routing.demo_pg_dsn(schema) == (
        "postgresql://erp_user:erp-pw@erp.example.com:5432/erp_simulation"
        f"?options=-csearch_path%3D{schema}"
    )
    assert _pointer_spy == [], "SSM pointer consulted although env is set"


@pytest.mark.parametrize("schema", DEMO_SCHEMAS)
def test_demo_pg_dsn_vf_hardcoded_pin(_todays_env, _pointer_spy, schema):
    """Hardcoded pin of the legacy demo-branch DSN (see the ERP pin above)."""
    assert db_routing.demo_pg_dsn(schema) == (
        "postgresql://demo_user:demo-pw@adt.example.com:5432/adt_demo"
        f"?options=-csearch_path%3D{schema}"
    )
    assert _pointer_spy == [], "SSM pointer consulted although env is set"


@pytest.mark.parametrize("schema", ERP_SCHEMAS)
def test_demo_conn_params_parity_erp(_todays_env, _pointer_spy, schema):
    from agents.db import _get_conn_params_for_schema
    assert db_routing.demo_conn_params_for_schema(schema) == _get_conn_params_for_schema(schema)
    assert _pointer_spy == []


@pytest.mark.parametrize("schema", DEMO_SCHEMAS)
def test_demo_conn_params_none_for_demo_schemas(_todays_env, _pointer_spy, schema):
    """Non-ERP schemas return None — the demo/generic branch stays engine-side
    in db.get_conn_params (generic single-DB config, not demo knowledge)."""
    assert db_routing.demo_conn_params_for_schema(schema) is None
    assert _pointer_spy == []


@pytest.mark.parametrize("schema", ERP_SCHEMAS + DEMO_SCHEMAS)
def test_demo_db_secret_arn_parity(_todays_env, _pointer_spy, schema):
    from agents.connection_resolver import db_secret_arn_for_schema
    assert db_routing.demo_db_secret_arn(schema) == db_secret_arn_for_schema(schema)
    assert _pointer_spy == []


@pytest.mark.parametrize("schema", ERP_SCHEMAS)
def test_erp_routing_hardcoded_pin(_todays_env, schema):
    """Durable routing pin (survives the Wave-2 legacy deletions)."""
    assert "erp.example.com" in db_routing.demo_pg_dsn(schema)
    assert db_routing.demo_conn_params_for_schema(schema) == {
        "host": "erp.example.com",
        "port": "5432",
        "dbname": "erp_simulation",
        "user": "erp_user",
        "password": "erp-pw",
    }
    assert db_routing.demo_db_secret_arn(schema) == ERP_ARN


@pytest.mark.parametrize("schema", DEMO_SCHEMAS)
def test_demo_routing_hardcoded_pin(_todays_env, schema):
    """cmms_*/plm_* are lambda-adt-seed schemas — demo database, NOT ERP (the
    docstring-lie incident, 2026-07-14 mgmt env)."""
    dsn = db_routing.demo_pg_dsn(schema)
    assert "adt.example.com" in dsn
    assert f"search_path%3D{schema}" in dsn
    assert db_routing.demo_db_secret_arn(schema) == SEED_ARN


def test_erp_password_falls_back_to_shared_and_fails_closed(monkeypatch):
    """Legacy parity: ERP password falls back to POSTGRES_PASSWORD; with
    neither set, params fail CLOSED (RuntimeError), never a default."""
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    monkeypatch.setenv("POSTGRES_PASSWORD", "shared-pw")  # noqa: S105 - test-only placeholder
    params = db_routing.demo_conn_params_for_schema("erp_data")
    assert params["password"] == "shared-pw"  # noqa: S105
    monkeypatch.delenv("POSTGRES_PASSWORD")
    with pytest.raises(RuntimeError, match="ERP_POSTGRES_PASSWORD"):
        db_routing.demo_conn_params_for_schema("erp_data")


def test_injected_env_wins_over_os_environ(monkeypatch, _pointer_spy):
    """The env parameter is injectable (harness pattern) — a poisoned
    os.environ must not leak into an explicit-env call."""
    monkeypatch.setenv("ERP_POSTGRES_HOST", "poison.example.com")
    env = {"ERP_POSTGRES_HOST": "explicit.example.com", "ERP_POSTGRES_PASSWORD": "pw"}
    dsn = db_routing.demo_pg_dsn("erp_data", env=env)
    assert "explicit.example.com" in dsn
    assert "poison.example.com" not in dsn


# ---------------------------------------------------------------------------
# (b) Env absent + SSM pointer present → pointer values used, routed correctly
# ---------------------------------------------------------------------------

@pytest.fixture
def _pointers(monkeypatch):
    """Monkeypatched pointer store: env absent, both SSM pointers published."""
    store = {
        "seed-db": {
            "host": "seed-ptr.example.com",
            "port": "5432",
            "dbname": "adt_demo",
            "secret_arn": SEED_ARN,
        },
        "erp-db": {
            "host": "erp-ptr.example.com",
            "port": "5433",
            "dbname": "erp_simulation",
            "secret_arn": ERP_ARN,
        },
    }
    monkeypatch.setattr(db_routing, "_demo_db_pointer", lambda kind: dict(store.get(kind, {})))
    return store


@pytest.mark.parametrize("schema", ERP_SCHEMAS)
def test_pointer_fallback_routes_erp(_pointers, schema):
    dsn = db_routing.demo_pg_dsn(schema)
    assert "erp-ptr.example.com:5433" in dsn
    assert "/erp_simulation" in dsn
    assert db_routing.demo_db_secret_arn(schema) == ERP_ARN


@pytest.mark.parametrize("schema", DEMO_SCHEMAS)
def test_pointer_fallback_routes_demo(_pointers, schema):
    dsn = db_routing.demo_pg_dsn(schema)
    assert "seed-ptr.example.com:5432" in dsn
    assert "/adt_demo" in dsn
    assert db_routing.demo_db_secret_arn(schema) == SEED_ARN


def test_pointer_conn_params_still_fail_closed_on_password(_pointers, monkeypatch):
    """The pointer carries no credentials — host from the pointer, password
    still env-only and fail-closed (legacy RuntimeError preserved)."""
    with pytest.raises(RuntimeError, match="ERP_POSTGRES_PASSWORD"):
        db_routing.demo_conn_params_for_schema("erp_data")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")  # noqa: S105 - test-only placeholder
    params = db_routing.demo_conn_params_for_schema("erp_data")
    assert params["host"] == "erp-ptr.example.com"
    assert params["port"] == "5433"


def test_pointer_read_does_not_write_back_to_environ(_pointers):
    db_routing.demo_pg_dsn("erp_data")
    db_routing.demo_db_secret_arn("cmms_maintenance")
    for key in _ENV_KEYS:
        assert key not in os.environ, f"{key} written back to os.environ"


def test_env_partially_set_env_wins_per_field(_pointers, monkeypatch):
    """Env wins field-by-field: an env ERP host suppresses the pointer for the
    ERP branch entirely (host AND port/db come from env-or-defaults)."""
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    dsn = db_routing.demo_pg_dsn("erp_data")
    assert "erp.example.com:5432" in dsn  # env host + legacy default port
    assert "erp-ptr" not in dsn


# ---------------------------------------------------------------------------
# _demo_db_pointer itself — real (faked) SSM path, fail-soft, kind allowlist
# ---------------------------------------------------------------------------

def _fake_boto3(params: dict, calls: list):
    class _FakeSSM:
        def get_parameter(self, Name):  # noqa: N803 - boto3 API shape
            calls.append(Name)
            if Name not in params:
                raise KeyError(Name)
            return {"Parameter": {"Value": params[Name]}}

    def _client(name, **kwargs):
        assert name == "ssm"
        return _FakeSSM()

    return types.SimpleNamespace(client=_client)


def test_demo_db_pointer_reads_ssm_json():
    calls: list = []
    fake = _fake_boto3(
        {"/adt/demo/erp-db": (
            '{"host": "erp-ptr.example.com", "port": 5433, '
            f'"dbname": "erp_simulation", "secret_arn": "{ERP_ARN}"}}'
        )},
        calls,
    )
    with patch.dict(sys.modules, {"boto3": fake}):
        ptr = db_routing._demo_db_pointer("erp-db")
    assert calls == ["/adt/demo/erp-db"]
    assert ptr == {
        "host": "erp-ptr.example.com",
        "port": "5433",  # coerced to str
        "dbname": "erp_simulation",
        "secret_arn": ERP_ARN,
    }


def test_demo_db_pointer_fail_soft():
    """SSM unavailable / parameter missing / non-JSON → {} — never raises.
    (Cache cleared between sub-cases: negative results are briefly cached.)"""
    calls: list = []
    with patch.dict(sys.modules, {"boto3": _fake_boto3({}, calls)}):
        assert db_routing._demo_db_pointer("seed-db") == {}
    db_routing._POINTER_CACHE.clear()
    with patch.dict(sys.modules, {"boto3": _fake_boto3(
            {"/adt/demo/seed-db": "not json"}, calls)}):
        assert db_routing._demo_db_pointer("seed-db") == {}
    db_routing._POINTER_CACHE.clear()
    with patch.dict(sys.modules, {"boto3": _fake_boto3(
            {"/adt/demo/seed-db": '["not", "a", "dict"]'}, calls)}):
        assert db_routing._demo_db_pointer("seed-db") == {}


def test_demo_db_pointer_rejects_unknown_kind():
    calls: list = []
    with patch.dict(sys.modules, {"boto3": _fake_boto3({}, calls)}):
        assert db_routing._demo_db_pointer("highbyte") == {}
    assert calls == [], "unknown kind must not reach SSM"


# ---------------------------------------------------------------------------
# Short TTL cache on the pointer SSM reads (X5 — mirrors db.py's cache idiom)
# ---------------------------------------------------------------------------

class _FakeTime:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


def test_pointer_cache_second_call_within_ttl_one_ssm_read(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(db_routing, "time", fake)
    reads: list = []
    pointer = {"host": "erp-ptr.example.com", "port": "5433",
               "dbname": "erp_simulation", "secret_arn": ERP_ARN}
    monkeypatch.setattr(db_routing, "_read_demo_db_pointer",
                        lambda kind: (reads.append(kind), dict(pointer))[1])

    p1 = db_routing._demo_db_pointer("erp-db")
    fake.now += 30  # within the 60s TTL
    p2 = db_routing._demo_db_pointer("erp-db")
    assert reads == ["erp-db"], "second call within TTL must not re-read SSM"
    assert p1 == p2 == pointer
    # Cached copies are independent dicts — a caller mutating one must not
    # poison the cache.
    p1["host"] = "mutated"
    assert db_routing._demo_db_pointer("erp-db")["host"] == "erp-ptr.example.com"


def test_pointer_cache_expiry_rereads(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(db_routing, "time", fake)
    reads: list = []
    monkeypatch.setattr(db_routing, "_read_demo_db_pointer",
                        lambda kind: (reads.append(kind), {"host": "h"})[1])
    db_routing._demo_db_pointer("seed-db")
    fake.now += db_routing._POINTER_CACHE_TTL + 1
    db_routing._demo_db_pointer("seed-db")
    assert reads == ["seed-db", "seed-db"]


def test_pointer_negative_result_cached_briefly(monkeypatch):
    """A negative ({}) read is cached for the SHORT negative TTL — a second
    call within it costs no SSM read; past it, SSM is consulted again."""
    fake = _FakeTime()
    monkeypatch.setattr(db_routing, "time", fake)
    reads: list = []
    monkeypatch.setattr(db_routing, "_read_demo_db_pointer",
                        lambda kind: (reads.append(kind), {})[1])

    assert db_routing._demo_db_pointer("seed-db") == {}
    fake.now += db_routing._POINTER_CACHE_NEG_TTL - 1
    assert db_routing._demo_db_pointer("seed-db") == {}
    assert reads == ["seed-db"], "negative result within its TTL must be served cached"
    fake.now += 2  # past the negative TTL
    db_routing._demo_db_pointer("seed-db")
    assert reads == ["seed-db", "seed-db"]


def test_pointer_kinds_cached_independently(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(db_routing, "time", fake)
    reads: list = []
    monkeypatch.setattr(db_routing, "_read_demo_db_pointer",
                        lambda kind: (reads.append(kind), {"host": kind})[1])
    assert db_routing._demo_db_pointer("seed-db")["host"] == "seed-db"
    assert db_routing._demo_db_pointer("erp-db")["host"] == "erp-db"
    assert reads == ["seed-db", "erp-db"]


# ---------------------------------------------------------------------------
# Fail-closed refusal type (X3 — dedicated exception, RuntimeError-compatible)
# ---------------------------------------------------------------------------

def test_no_password_refusal_is_dedicated_error(monkeypatch):
    """The fail-closed refusal raises MissingDbPasswordError (a RuntimeError
    subclass — backward compatible) so discriminating callers can re-raise
    ONLY the deliberate refusal."""
    assert issubclass(MissingDbPasswordError, RuntimeError)
    monkeypatch.setenv("ERP_POSTGRES_HOST", "erp.example.com")
    with pytest.raises(MissingDbPasswordError, match="ERP_POSTGRES_PASSWORD"):
        db_routing.demo_conn_params_for_schema("erp_data")


def test_boto3_tripwire_env_set(monkeypatch, _todays_env):
    """(c) tripwire at the boto3 level: with env set, no boto3 client is ever
    created across all three routing reads."""
    calls: list = []

    def _client(name, **kwargs):
        calls.append(name)
        raise AssertionError("boto3 client must not be created when env is set")

    with patch.dict(sys.modules, {"boto3": types.SimpleNamespace(client=_client)}):
        for schema in ERP_SCHEMAS + DEMO_SCHEMAS:
            db_routing.demo_pg_dsn(schema)
            db_routing.demo_conn_params_for_schema(schema)
            db_routing.demo_db_secret_arn(schema)
    assert calls == []


# ---------------------------------------------------------------------------
# (c) Both env AND pointer absent → legacy-parity defaults / fail-soft
# ---------------------------------------------------------------------------

def test_both_absent_conn_params_none(_pointer_spy):
    assert db_routing.demo_conn_params_for_schema("erp_data") is None
    assert _pointer_spy == ["erp-db"]  # pointer consulted once, then fall-through


def test_both_absent_secret_arn_empty(_pointer_spy):
    assert db_routing.demo_db_secret_arn("erp_data") == ""
    assert db_routing.demo_db_secret_arn("cmms_maintenance") == ""


def test_both_absent_dsn_matches_legacy_defaults(_pointer_spy):
    """No ERP host anywhere → even ERP schemas fall to the demo branch with the
    legacy defaults (host 'postgres', empty password) — parity with the legacy
    catalog DSN builder under an empty env."""
    for schema in ("erp_data", "cmms_maintenance"):
        assert db_routing.demo_pg_dsn(schema) == (
            f"postgresql://postgres:@postgres:5432/adt_demo"
            f"?options=-csearch_path%3D{schema}"
        )


# ---------------------------------------------------------------------------
# (d) Import purity + one-way demo seam
# ---------------------------------------------------------------------------

def test_module_import_is_pure_no_boto3():
    """Fresh interpreter with boto3 BOMBED must still import the module —
    boto3 is only reachable lazily inside the SSM fallback. Runs in a
    SUBPROCESS (the fresh-interpreter convention — see
    test_system_normalize.py) so sys.modules and the ``agents.demo`` parent
    attribute are never left pointing at a stale in-process re-import
    (order-dependent failures + a real SSM attempt in the offline lane)."""
    code = (
        "import sys\n"
        "sys.modules['boto3'] = None\n"  # import boto3 → ImportError
        "from agents.demo import db_routing\n"
        "assert db_routing.ERP_SCHEMA_PREFIXES == ('erp_', 'mes_', 'public')\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv list, no shell, internal inputs only
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout


def test_one_way_demo_seam_no_core_agent_imports():
    """agents/demo must not import agents.db / agents.catalog / agents.main
    (one-way dependency, agents/demo/__init__.py)."""
    src = Path(db_routing.__file__).read_text()
    forbidden = re.compile(
        r"^\s*(from|import)\s+(agents\.(db|catalog|main)\b|\.\.(db|catalog|main)\b)"
        r"|^\s*from\s+\.\.\s*import\s+(db|catalog|main)\b",
        re.MULTILINE,
    )
    assert not forbidden.search(src), "db_routing imports a core agents module"


def test_only_erp_prefix_definition_lives_here():
    """agents/demo/db_routing.py is the only ERP_SCHEMA_PREFIXES definition
    under agents/ (the Wave-3 grep gate extends this repo-wide)."""
    agents_dir = Path(db_routing.__file__).resolve().parents[1]
    offenders = []
    for py in agents_dir.rglob("*.py"):
        if py == Path(db_routing.__file__).resolve():
            continue
        if re.search(r"^ERP_SCHEMA_PREFIXES\s*=", py.read_text(), re.MULTILINE):
            offenders.append(str(py))
    assert offenders == []
