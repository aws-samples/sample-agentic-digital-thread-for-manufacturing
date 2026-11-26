# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the non-secret ``connection_config`` on catalog entries: the
allowlist sanitizer, /connect storage, the GET /api/onboard/catalog lazy
backfill (once per entry per process, fail-soft), and the defense-in-depth
response sanitize. Generic (synthetic identifiers only)."""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from agents.catalog import SystemCatalog
from agents.connection_resolver import sanitize_connection_config


@pytest.fixture(autouse=True)
def _athena_role_rule_off(monkeypatch):
    """These tests register Athena systems to pin config sanitization and the
    secret routing, not the rule that a deployed app's Athena registration
    needs a role ARN (test_connect_athena_role_required.py): run them as local
    development, where a blank role is accepted."""
    import agents.onboard_agent as onboard_agent
    monkeypatch.setattr(onboard_agent, "local_mode_env", lambda *a, **k: True)


# ── sanitizer (pure) ─────────────────────────────────────────────────────────

def test_sanitizer_keeps_only_allowlisted_fields():
    cfg = {
        "workgroup": "wg", "database": "db", "catalog": "AwsDataCatalog",
        "output_location": "s3://amzn-s3-demo-results-bucket/prefix/", "region": "us-west-2",
        "role_arn": "arn:aws:iam::111122223333:role/ReadRole",
        "base_url": "https://api.example.com", "url": "https://api.example.com/mcp",
        "host": "db.example.com", "port": "5432", "dbname": "analytics",
    }
    assert sanitize_connection_config(cfg) == cfg


def test_sanitizer_drops_secret_fields():
    """external_id / token / password / secret* / credential* / api_key are DROPPED."""
    out = sanitize_connection_config({
        "workgroup": "wg",
        "external_id": "sekrit-external-id",
        "token": "sekrit-token",
        "password": "sekrit-pw",
        "secret_key": "sekrit-sk",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:x",
        "credentials": {"aws_access_key_id": "AKIA..."},
        "credential_blob": "x",
        "api_key": "sekrit-api-key",
    })
    assert out == {"workgroup": "wg"}


def test_sanitizer_is_allowlist_not_denylist():
    """A never-before-seen key is dropped by default — safe against new secrets."""
    out = sanitize_connection_config({"database": "db", "shiny_new_secret": "x"})
    assert out == {"database": "db"}


def test_sanitizer_tolerates_json_string_and_non_dict_and_blank_values():
    assert sanitize_connection_config(
        json.dumps({"region": "us-east-1", "token": "t"})) == {"region": "us-east-1"}
    assert sanitize_connection_config("{not json") == {}
    assert sanitize_connection_config(None) == {}
    assert sanitize_connection_config(["region"]) == {}
    assert sanitize_connection_config({"region": "", "workgroup": None}) == {}


def test_sanitizer_strips_inline_url_credentials():
    out = sanitize_connection_config({"url": "https://user:sekrit-pw@host.example.com/mcp"})
    assert "sekrit-pw" not in out["url"]
    assert "@" not in out["url"]


# ── /connect stores the sanitized subset ─────────────────────────────────────

_CONN_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/lake-analytics"


def test_connect_stores_sanitized_config_strips_external_id():
    """external_id/token are stripped from the STORED catalog entry even when the
    caller sends them; allowlisted fields survive."""
    from agents.onboard_agent import connect_new_system
    added: dict = {}
    cat = MagicMock()
    cat.get.return_value = None
    cat.lookup.return_value = None
    cat.add.side_effect = lambda e: added.update(e)
    with patch("agents.onboard_agent.catalog", cat), \
         patch("agents.connection_resolver._store_connection_secret",
               return_value=_CONN_ARN):
        res = connect_new_system(
            name="Lake Analytics", system_type="Historian", plant="Site One",
            connection_type="Athena",
            connection_config={"workgroup": "wg", "database": "db",
                               "region": "us-west-2",
                               "external_id": "sekrit-external-id",
                               "token": "sekrit-token"},
        )
    assert res["status"] == "created"
    assert added["connection_config"] == {
        "workgroup": "wg", "database": "db", "region": "us-west-2"}


def test_connect_omits_config_key_when_all_fields_are_secret():
    """A config that sanitizes to {} stores NO connection_config key."""
    from agents.onboard_agent import connect_new_system
    added: dict = {}
    cat = MagicMock()
    cat.get.return_value = None
    cat.lookup.return_value = None
    cat.add.side_effect = lambda e: added.update(e)
    with patch("agents.onboard_agent.catalog", cat), \
         patch("agents.connection_resolver._store_connection_secret",
               return_value=_CONN_ARN):
        connect_new_system(
            name="Lake Analytics", system_type="Historian", plant="",
            connection_type="Athena",
            connection_config={"external_id": "sekrit", "token": "t"},
        )
    assert "connection_config" not in added


# ── M4: /connect upserts the conn/<id> secret for registration-time secrets ──

def test_connect_upserts_conn_secret_and_persists_arn_pointer():
    """A submitted config carrying external_id (a SECRET) mints/updates the
    ``adt/conn/<id>`` secret THE RESOLVER READS (via the existing
    ``connection_resolver._store_connection_secret`` helper — no early System
    node), persists the ARN POINTER on the catalog entry, and returns a note —
    with NO secret value anywhere in the result or entry."""
    from agents.onboard_agent import connect_new_system
    added: dict = {}
    cat = MagicMock()
    cat.get.return_value = None
    cat.lookup.return_value = None
    # The registration claims the id (catalog.add) BEFORE writing its secret, then
    # sets the pointer on that same entry object — keep the reference, not a copy.
    cat.add.side_effect = lambda e: added.setdefault("entry", e) and None
    with patch("agents.onboard_agent.catalog", cat), \
         patch("agents.connection_resolver._store_connection_secret",
               return_value=_CONN_ARN) as store:
        res = connect_new_system(
            name="Lake Analytics", system_type="Historian", plant="Site One",
            connection_type="Athena",
            connection_config={"workgroup": "wg", "database": "db",
                               "region": "us-west-2",
                               "role_arn": "arn:aws:iam::444455556666:role/ReadRole",
                               "external_id": "sekrit-external-id"},
        )
    added = added["entry"]
    # The secret was upserted with ONLY the secret keys plus the registration
    # binding (origin + system id — the token is read only through this
    # entry's own pointer, never by name), under the system id.
    store.assert_called_once_with("lake-analytics",
                                  {"external_id": "sekrit-external-id",
                                   "connection_origin": "user",
                                   "system_id": "lake-analytics"})
    # ARN POINTER persisted on the entry (metric_resolver contract); catalog clean.
    assert added["secret_arn"] == _CONN_ARN
    assert "external_id" not in json.dumps(added["connection_config"])
    # The result carries a note that credentials were stored — but NO values.
    assert "note" in res
    assert "stored" in res["note"]
    assert "sekrit" not in json.dumps(res)


def test_connect_upserts_conn_secret_for_token_too():
    from agents.onboard_agent import connect_new_system
    added: dict = {}
    cat = MagicMock()
    cat.get.return_value = None
    cat.lookup.return_value = None
    cat.add.side_effect = lambda e: added.setdefault("entry", e) and None  # reference (see above)
    with patch("agents.onboard_agent.catalog", cat), \
         patch("agents.connection_resolver._store_connection_secret",
               return_value=_CONN_ARN) as store, \
         patch("agents.connection_resolver.validate_connection_target",
               return_value="203.0.113.10"):  # a user MCP URL is SSRF-validated (offline)
        connect_new_system(
            name="Lake Analytics", system_type="Historian", plant="",
            connection_type="MCP Server",
            connection_config={"url": "https://mcp.example.com", "token": "sekrit-token"},
        )
    store.assert_called_once_with("lake-analytics", {"token": "sekrit-token",
                                                     "connection_origin": "user",
                                                     "system_id": "lake-analytics"})
    assert added["entry"]["secret_arn"] == _CONN_ARN


def test_connect_without_secrets_writes_no_secret_and_no_note():
    from agents.onboard_agent import connect_new_system
    added: dict = {}
    cat = MagicMock()
    cat.get.return_value = None
    cat.lookup.return_value = None
    cat.add.side_effect = lambda e: added.update(e)
    with patch("agents.onboard_agent.catalog", cat), \
         patch("agents.connection_resolver._store_connection_secret") as store:
        res = connect_new_system(
            name="Lake Analytics", system_type="Historian", plant="",
            connection_type="Athena",
            connection_config={"workgroup": "wg", "database": "db"},
        )
    store.assert_not_called()
    assert "secret_arn" not in added
    assert "note" not in res


def test_connect_secret_write_failure_registers_nothing():
    """A Secrets Manager failure must not 500 the registration, and a user
    registration must not keep a claim without its credentials (they would be
    lost and the id would block a retry): exactly the claimed entry is
    removed and the answer is an error. No secret value in the result.
    (test_connect_registration_secret_transactional.py has the end-to-end
    cases; a demo registration keeps the fail-soft note.)"""
    from agents.onboard_agent import connect_new_system
    added: dict = {}
    cat = MagicMock()
    cat.get.return_value = None
    cat.lookup.return_value = None
    cat.add.side_effect = lambda e: added.update(e)
    with patch("agents.onboard_agent.catalog", cat), \
         patch("agents.connection_resolver._store_connection_secret",
               side_effect=RuntimeError("secretsmanager unavailable")):
        res = connect_new_system(
            name="Lake Analytics", system_type="Historian", plant="",
            connection_type="Athena",
            connection_config={"workgroup": "wg", "external_id": "sekrit"},
        )
    assert "status" not in res and "could not be stored" in res["error"], res
    assert "secret_arn" not in added
    cat.remove_entry.assert_called_once()
    assert cat.remove_entry.call_args[0][0]["id"] == "lake-analytics"
    assert "sekrit" not in json.dumps(res)


# ── GET /api/onboard/catalog: lazy backfill + defense-in-depth ───────────────

@pytest.fixture()
def onboard_mod():
    import agents.onboard as onboard
    onboard._cfg_backfill_attempted.clear()
    yield onboard
    onboard._cfg_backfill_attempted.clear()


def _athena_entry(**over) -> dict:
    e = {"id": "lake-analytics", "name": "Lake Analytics", "plant": "Site One",
         "system_type": "Historian", "connection_type": "Athena",
         "connection_string": "", "schema": "", "status": "onboarded"}
    e.update(over)
    return e


_RESOLVED = {
    "system_id": "lake-analytics", "connection_type": "Athena",
    "connection_string": "", "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:conn",
    "workgroup": "wg", "database": "db", "region": "us-west-2",
    "role_arn": "arn:aws:iam::111122223333:role/ReadRole",
    "external_id": "sekrit-external-id",  # merged from the conn secret — must never be served
}


def test_catalog_lazy_backfill_happy_path(onboard_mod):
    """A typed-connector entry lacking connection_config gains the SANITIZED
    resolved subset AND the node's secret_arn POINTER; both are persisted, and a
    second GET serves the persisted value with the resolver called EXACTLY once."""
    cat = SystemCatalog([_athena_entry()])
    with patch.object(onboard_mod, "catalog", cat), \
         patch("agents.connection_resolver.resolve_connection",
               return_value=dict(_RESOLVED)) as rc:
        first = asyncio.run(onboard_mod.onboard_catalog())
        second = asyncio.run(onboard_mod.onboard_catalog())
    assert rc.call_count == 1
    expected = {"workgroup": "wg", "database": "db", "region": "us-west-2",
                "role_arn": "arn:aws:iam::111122223333:role/ReadRole"}
    assert first[0]["connection_config"] == expected
    assert second[0]["connection_config"] == expected
    # persisted onto the in-memory catalog entry (not just the response copy):
    # the sanitized config AND the ARN POINTER — exactly the two keys
    # metric_resolver._full_connection_config's documented contract reads.
    assert cat.get("lake-analytics")["connection_config"] == expected
    assert cat.get("lake-analytics")["secret_arn"] == _RESOLVED["secret_arn"]
    # RESPONSE POLICY: the ARN pointer is served (existing serializer behavior —
    # /credentials-stored entries already expose it and the onboarding UI reads
    # sys.secret_arn); it is a pointer, never a secret VALUE.
    assert first[0]["secret_arn"] == _RESOLVED["secret_arn"]


def test_catalog_backfill_without_secret_persists_config_only(onboard_mod):
    """A resolved node with NO secret (secret_arn == '' — e.g. IAM-role auth)
    persists the sanitized config, sets NO secret_arn key, and never raises."""
    cat = SystemCatalog([_athena_entry()])
    resolved = {k: v for k, v in _RESOLVED.items() if k != "external_id"}
    resolved["secret_arn"] = ""  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    with patch.object(onboard_mod, "catalog", cat), \
         patch("agents.connection_resolver.resolve_connection",
               return_value=resolved):
        first = asyncio.run(onboard_mod.onboard_catalog())
    entry = cat.get("lake-analytics")
    assert entry["connection_config"] == {
        "workgroup": "wg", "database": "db", "region": "us-west-2",
        "role_arn": "arn:aws:iam::111122223333:role/ReadRole"}
    assert "secret_arn" not in entry
    assert "secret_arn" not in first[0]


def test_catalog_backfill_persists_arn_pointer_even_when_cfg_empty(onboard_mod):
    """LOW (a): a secret-only connection (every resolved field is secret-shaped,
    sanitized cfg == {}) must STILL persist the secret_arn POINTER onto the entry
    (matches _persist_node_connection's `if safe_cfg or resolved_arn`), so
    metric_resolver._full_connection_config can merge the referenced secret."""
    cat = SystemCatalog([_athena_entry()])
    resolved = {"system_id": "lake-analytics", "connection_type": "Athena",
                "connection_string": "", "secret_arn": _RESOLVED["secret_arn"],
                "external_id": "sekrit-external-id"}  # sanitizes to {}
    with patch.object(onboard_mod, "catalog", cat), \
         patch("agents.connection_resolver.resolve_connection",
               return_value=resolved):
        asyncio.run(onboard_mod.onboard_catalog())
    entry = cat.get("lake-analytics")
    assert entry["secret_arn"] == _RESOLVED["secret_arn"]
    assert entry.get("connection_config") in (None, {})


def test_catalog_backfill_failure_is_fail_soft_and_cached(onboard_mod):
    """A resolution error serves the entry WITHOUT a config (no raise), and the
    miss is cached — polling never re-hammers resolution in this process."""
    cat = SystemCatalog([_athena_entry()])
    with patch.object(onboard_mod, "catalog", cat), \
         patch("agents.connection_resolver.resolve_connection",
               side_effect=RuntimeError("secrets manager unavailable")) as rc:
        first = asyncio.run(onboard_mod.onboard_catalog())
        second = asyncio.run(onboard_mod.onboard_catalog())
    assert rc.call_count == 1
    assert "connection_config" not in first[0]
    assert "connection_config" not in second[0]


def test_catalog_backfill_skips_unconfigured_node(onboard_mod):
    """An _unconfigured resolve result (unregistered node) yields no config and
    caches the miss."""
    cat = SystemCatalog([_athena_entry()])
    unconfigured = {"system_id": "lake-analytics", "connection_type": None,
                    "connection_string": "", "secret_arn": "", "_unconfigured": True}
    with patch.object(onboard_mod, "catalog", cat), \
         patch("agents.connection_resolver.resolve_connection",
               return_value=unconfigured) as rc:
        first = asyncio.run(onboard_mod.onboard_catalog())
        asyncio.run(onboard_mod.onboard_catalog())
    assert rc.call_count == 1
    assert "connection_config" not in first[0]


def test_catalog_backfill_only_for_typed_connectors(onboard_mod):
    """PostgreSQL/MCP/REST entries never trigger resolution from the catalog GET."""
    cat = SystemCatalog([
        _athena_entry(id="pg-sys", connection_type="PostgreSQL",
                      connection_string="postgresql://u:p@h:5432/db"),
        _athena_entry(id="mcp-sys", connection_type="MCP Server"),
    ])
    with patch.object(onboard_mod, "catalog", cat), \
         patch("agents.connection_resolver.resolve_connection") as rc:
        res = asyncio.run(onboard_mod.onboard_catalog())
    assert rc.call_count == 0
    # existing masking behavior unchanged
    assert res[0]["connection_string"] == "postgresql://***@h:5432/db"


def test_catalog_response_never_contains_external_id(onboard_mod):
    """Regression: the serialized response for ALL entries — a legacy/hand-edited
    entry carrying secrets, a backfilled Athena entry, and a backfilled SiteWise
    (Direct) entry — never contains the string 'external_id'."""
    cat = SystemCatalog([
        _athena_entry(id="legacy-a",
                      connection_config={"workgroup": "wg", "database": "db",
                                         "external_id": "sekrit-1", "token": "t"}),
        _athena_entry(id="fresh-b"),
        _athena_entry(id="sw-c", connection_type="SiteWise (Direct)"),
    ])
    with patch.object(onboard_mod, "catalog", cat), \
         patch("agents.connection_resolver.resolve_connection",
               return_value=dict(_RESOLVED)):
        res = asyncio.run(onboard_mod.onboard_catalog())
    blob = json.dumps(res)
    assert "external_id" not in blob
    assert "sekrit" not in blob
    # the legacy entry still serves its non-secret fields (defense-in-depth, not erasure)
    legacy = next(e for e in res if e["id"] == "legacy-a")
    assert legacy["connection_config"] == {"workgroup": "wg", "database": "db"}


# ── discover-body persistence helper ─────────────────────────────────────────

def test_persist_catalog_config_from_body_sanitizes_and_writes_once(onboard_mod):
    cat = SystemCatalog([_athena_entry()])
    with patch.object(onboard_mod, "catalog", cat):
        onboard_mod._persist_catalog_config_from_body(
            "lake-analytics", {"workgroup": "wg", "database": "db",
                               "external_id": "sekrit"})
        assert cat.get("lake-analytics")["connection_config"] == {
            "workgroup": "wg", "database": "db"}
        # an entry that already carries a config is left untouched
        onboard_mod._persist_catalog_config_from_body(
            "lake-analytics", {"workgroup": "other-wg"})
    assert cat.get("lake-analytics")["connection_config"] == {
        "workgroup": "wg", "database": "db"}


def test_persist_catalog_config_from_body_noop_cases(onboard_mod):
    cat = SystemCatalog([_athena_entry()])
    with patch.object(onboard_mod, "catalog", cat):
        onboard_mod._persist_catalog_config_from_body("", {"workgroup": "wg"})
        onboard_mod._persist_catalog_config_from_body("lake-analytics", None)
        onboard_mod._persist_catalog_config_from_body("absent-system", {"workgroup": "wg"})
        onboard_mod._persist_catalog_config_from_body("lake-analytics", {"external_id": "s"})
    assert "connection_config" not in cat.get("lake-analytics")


# ── SystemCatalog.set_connection_config: secret_arn POINTER semantics ────────

_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/lake-analytics"


def test_set_connection_config_persists_secret_arn_pointer():
    """The optional secret_arn kwarg persists a plain ARN POINTER onto the entry
    (the key metric_resolver._full_connection_config reads); no config VALUES
    beyond what the caller passed ever land."""
    cat = SystemCatalog([_athena_entry()])
    assert cat.set_connection_config("lake-analytics", {"workgroup": "wg"},
                                     secret_arn=_ARN)
    entry = cat.get("lake-analytics")
    assert entry["connection_config"] == {"workgroup": "wg"}
    assert entry["secret_arn"] == _ARN


def test_set_connection_config_none_arn_leaves_existing_pointer_untouched():
    """secret_arn=None (and the plain 2-arg call) never clears an existing pointer."""
    cat = SystemCatalog([_athena_entry(secret_arn=_ARN)])
    assert cat.set_connection_config("lake-analytics", {"database": "db"})
    entry = cat.get("lake-analytics")
    assert entry["secret_arn"] == _ARN
    assert entry["connection_config"] == {"database": "db"}
    # unknown system → False, no raise
    assert not cat.set_connection_config("absent", {}, secret_arn=_ARN)


def test_catalog_response_secret_arn_is_pointer_only(onboard_mod):
    """RESPONSE POLICY regression: a backfilled entry's served secret_arn is the
    plain ARN pointer; the full response blob still carries NO secret VALUE
    (external_id/token/password) from the merged conn secret."""
    cat = SystemCatalog([_athena_entry()])
    with patch.object(onboard_mod, "catalog", cat), \
         patch("agents.connection_resolver.resolve_connection",
               return_value=dict(_RESOLVED)):
        res = asyncio.run(onboard_mod.onboard_catalog())
    blob = json.dumps(res)
    assert res[0]["secret_arn"] == _RESOLVED["secret_arn"]
    for leak in ("external_id", "sekrit", "token", "password"):
        assert leak not in blob, f"secret VALUE key {leak!r} leaked: {blob}"
