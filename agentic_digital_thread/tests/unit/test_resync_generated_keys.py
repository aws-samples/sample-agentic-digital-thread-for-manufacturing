# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A connection Edit of an Athena system keeps its baked schema.

The admin connection Edit (``PUT /api/admin/connection/<id>``) re-syncs the
runtime: ``resync_cloud_connection`` rewrites the whole ``adt/agent-config``
secret from the resolved connection, which carries no generated keys. Those
keys (the prompt, the baked table list, the concept mappings and the
different-identifier records) are copied back from the current secret when
the edit leaves the data target the same, and for Athena so are the admin
reliability settings (scan budget, poll bounds, exact-split caps) that a
secret can hold alone. For an Athena system the target is its region,
catalog, database and role ARN: a workgroup or output location change keeps
them; another database, catalog, region or role (even in the same account,
which can be granted other tables) does not. Without the baked table list the
state-time and storage-bound checks have nothing to read, so a system agent
built without one logs a warning, and the runtime audit reports it.

Fully offline: a fake Secrets Manager, a synthetic schema.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import agents.system_agent as sa
from agents import state_times, storage_bound_rule
from agents.agent_manager import AgentManager

_TS = "timestamp(6) with time zone"
DESC = (f"orders(id: varchar; phase: varchar e.g. SHIPPED, REJECTED, PACKED; "
        f"ship_date: {_TS}; reject_date: {_TS}; pack_date: {_TS}; qty: bigint)")
GENERATED = {
    "system_prompt": "You are the agent for the order lake.",
    "tables_desc": DESC,
    "concept_mappings": [{"concept": "order", "field": "orders.id"}],
    "distinct_identifiers": [{"table_a": "orders", "table_b": "orders", "column": "id"}],
}
ROLE = "arn:aws:iam::111122223333:role/lake-reader"
CURRENT = {"connection_type": "Athena", "workgroup": "wg-a", "database": "lake_db",
           "catalog": "AwsDataCatalog", "region": "us-east-1", "role_arn": ROLE,
           "external_id": "ext-VALUE", "output_location": "s3://amzn-s3-demo-results-a/out/",
           **GENERATED}
RESOLVED = {"system_id": "lake", "connection_type": "Athena", "connection_string": "",
            "workgroup": "wg-b", "database": "lake_db", "catalog": "AwsDataCatalog",
            "region": "us-east-1", "role_arn": ROLE, "external_id": "ext-VALUE",
            "output_location": "s3://amzn-s3-demo-results-a/out/", "secret_arn": ""}


class _Secrets:
    class ResourceExistsException(Exception):
        pass

    def __init__(self, current=None, readable=True):
        self.current, self.readable, self.writes = current, readable, []
        self.exceptions = types.SimpleNamespace(ResourceExistsException=self.ResourceExistsException)

    def get_secret_value(self, SecretId):
        if not self.readable or self.current is None:
            raise RuntimeError("not readable")
        return {"SecretString": json.dumps(self.current)}

    def create_secret(self, Name, SecretString):
        raise self.ResourceExistsException()

    def put_secret_value(self, SecretId, SecretString):
        self.writes.append(json.loads(SecretString))

    def describe_secret(self, SecretId):
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{SecretId}-x1y2z3"}


def _resync(secrets, resolved, connection_type="Athena"):
    mgr = AgentManager(mode="cloud")
    boto = types.SimpleNamespace(client=lambda name, **kw: secrets)
    with patch("agents.agent_manager.boto3", boto), \
            patch.object(mgr, "_apply_business_context", lambda sid, cfg, region: cfg), \
            patch.object(mgr, "_resync_force_runtime_pickup", lambda sid, arn, region: True):
        assert mgr.resync_cloud_connection("lake", connection_type,
                                           connection_config=dict(resolved)) is True
    (payload,) = secrets.writes
    return payload


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    state_times.reset()
    yield
    state_times.reset()


# --------------------------------------------------------------------------- #
# A. The same data target keeps the generated keys
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("change", [
    {"workgroup": "wg-b"},
    {"workgroup": "wg-b", "role_arn": f"  {ROLE} "},
    {"output_location": "s3://amzn-s3-demo-results-b/out/"},
    {"catalog": ""},          # an empty catalog is the default catalog
    {"catalog": "awsdatacatalog", "database": "LAKE_DB"},
])
def test_an_edit_that_keeps_the_data_target_keeps_the_baked_schema(change):
    payload = _resync(_Secrets(current=CURRENT), {**RESOLVED, **change})
    for key, value in GENERATED.items():
        assert payload[key] == value, key
    assert payload["workgroup"] == {**RESOLVED, **change}["workgroup"]


def test_the_carry_copies_only_the_generated_keys():
    current = {**CURRENT, "external_id": "old-ext", "secret_arn": "arn:old"}
    payload = _resync(_Secrets(current=current), RESOLVED)
    assert payload["external_id"] == "ext-VALUE" and payload["secret_arn"] == ""


def test_a_key_the_new_config_already_holds_is_not_overwritten():
    payload = _resync(_Secrets(current=CURRENT), {**RESOLVED, "system_prompt": "new prompt"})
    assert payload["system_prompt"] == "new prompt"
    assert payload["tables_desc"] == DESC


@pytest.mark.parametrize("change", [
    {"database": "other_db"},
    {"catalog": "OtherCatalog"},
    {"region": "eu-west-1"},
    {"role_arn": "arn:aws:iam::444455556666:role/lake-reader"},
    # another role of the same account can be granted other tables
    {"role_arn": "arn:aws:iam::111122223333:role/restricted-reader"},
    {"role_arn": "arn:aws:iam::111122223333:role/audit/lake-reader"},
    {"role_arn": "arn:aws:iam::111122223333:user/lake-reader"},
    {"role_arn": ""},                    # ambient credentials: maybe another account
    {"role_arn": "not-an-arn"},
    {"database": ""},
])
def test_another_data_target_carries_nothing_forward(change):
    payload = _resync(_Secrets(current=CURRENT), {**RESOLVED, **change})
    assert not any(key in payload for key in GENERATED), change


# --------------------------------------------------------------------------- #
# A. The admin reliability settings an Athena agent-config carries
# --------------------------------------------------------------------------- #
ADMIN = {"max_scanned_bytes": 123456, "poll_timeout_seconds": 150,
         "introspection_timeout_seconds": 45, "exact_split_max_pieces": 9,
         "exact_split_max_total_bytes": 999999, "exact_split_max_seconds": 33,
         "exact_split_max_groups": 777}


#: The settings only an agent-config can hold for a system: no route writes or
#: clears them on the System node, so a resync keeps them.
SECRET_HELD = {k: v for k, v in ADMIN.items() if k != "max_scanned_bytes"}


def test_a_workgroup_edit_keeps_the_admin_reliability_settings():
    payload = _resync(_Secrets(current={**CURRENT, **ADMIN}), RESOLVED)
    for key, value in SECRET_HELD.items():
        assert payload[key] == value, key
    assert payload["workgroup"] == "wg-b" and payload["tables_desc"] == DESC


def test_a_setting_the_fresh_config_holds_wins():
    fresh = {**RESOLVED, "poll_timeout_seconds": 90, "max_scanned_bytes": 5}
    payload = _resync(_Secrets(current={**CURRENT, **ADMIN}), fresh)
    assert payload["poll_timeout_seconds"] == 90 and payload["max_scanned_bytes"] == 5
    assert payload["exact_split_max_groups"] == 777


def test_a_scan_budget_cleared_on_the_node_stays_cleared_through_an_edit():
    # The scan-budget route sets and clears the budget on the System node, and
    # the resolved connection carries the node's value; an old agent-config
    # value must not bring a cleared budget back.
    payload = _resync(_Secrets(current={**CURRENT, **ADMIN}), RESOLVED)
    assert "max_scanned_bytes" not in payload
    assert payload["poll_timeout_seconds"] == 150


def test_the_scan_budget_route_and_the_resync_agree(monkeypatch):
    from agents import connection_resolver as cr

    node = {"connection_config": json.dumps({"workgroup": "wg-a", "max_scanned_bytes": 123456})}
    writes = []
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: node)
    monkeypatch.setattr(cr, "_neptune_write", lambda q, p: writes.append(p) or True)
    cr.write_scan_budget("lake", None)
    cleared = json.loads(writes[-1]["cfg_json"])
    assert "max_scanned_bytes" not in cleared
    payload = _resync(_Secrets(current={**CURRENT, **ADMIN}), {**RESOLVED, **cleared})
    assert "max_scanned_bytes" not in payload


@pytest.mark.parametrize("empty", [None, ""])
def test_an_explicit_empty_value_in_the_fresh_config_wins(empty):
    fresh = {**RESOLVED, "system_prompt": empty, "poll_timeout_seconds": empty}
    payload = _resync(_Secrets(current={**CURRENT, **ADMIN}), fresh)
    assert payload["system_prompt"] == empty and payload["poll_timeout_seconds"] == empty
    assert payload["tables_desc"] == DESC and payload["exact_split_max_groups"] == 777


@pytest.mark.parametrize("change", [
    {"database": "other_db"},
    {"role_arn": "arn:aws:iam::111122223333:role/restricted-reader"},
])
def test_another_data_target_drops_the_admin_settings(change):
    payload = _resync(_Secrets(current={**CURRENT, **ADMIN}), {**RESOLVED, **change})
    assert not any(key in payload for key in ADMIN), change


def test_the_kept_poll_timeout_reaches_the_athena_tools(monkeypatch):
    from agents.athena_tools import _resolve_config, resolve_poll_timeout
    from agents.runtime_app import _extract_prompt_and_conn_params

    monkeypatch.delenv("SYSTEM_PROMPT", raising=False)
    monkeypatch.delenv("TABLES_DESC", raising=False)
    payload = _resync(_Secrets(current={**CURRENT, **ADMIN}), RESOLVED)
    _prompt, _desc, _conn, cfg = _extract_prompt_and_conn_params(dict(payload), "Athena")
    assert resolve_poll_timeout(_resolve_config(cfg)) == 150.0


def test_the_kept_settings_are_the_node_config_settings():
    from agents.connection_resolver import (
        ATHENA_ADMIN_CONFIG_KEYS, NODE_MANAGED_ADMIN_KEYS, _NODE_CONFIG_KEYS)

    assert set(ATHENA_ADMIN_CONFIG_KEYS) == set(ADMIN)
    assert set(ATHENA_ADMIN_CONFIG_KEYS) <= set(_NODE_CONFIG_KEYS)
    assert NODE_MANAGED_ADMIN_KEYS == ("max_scanned_bytes",)
    assert AgentManager._carried_keys("Athena") == (
        *AgentManager.GENERATED_AGENT_CONFIG_KEYS, *SECRET_HELD)
    assert AgentManager._carried_keys("PostgreSQL") == AgentManager.GENERATED_AGENT_CONFIG_KEYS


def test_a_current_config_without_a_role_matches_only_another_without_one():
    no_role = {k: v for k, v in CURRENT.items() if k != "role_arn"}
    kept = _resync(_Secrets(current=no_role), {**RESOLVED, "role_arn": ""})
    assert kept["tables_desc"] == DESC
    moved = _resync(_Secrets(current=no_role), RESOLVED)
    assert "tables_desc" not in moved


def test_an_unreadable_current_secret_still_rewrites_the_connection():
    payload = _resync(_Secrets(current=CURRENT, readable=False), RESOLVED)
    assert payload["workgroup"] == "wg-b"
    assert not any(key in payload for key in GENERATED)


def test_a_current_secret_of_another_type_carries_nothing_forward():
    current = {**CURRENT, "connection_type": "PostgreSQL"}
    payload = _resync(_Secrets(current=current), RESOLVED)
    assert not any(key in payload for key in GENERATED)


def test_postgres_keeps_its_own_host_and_database_rule():
    from agents.agent_manager import _same_data_target
    old = {"host": "DB.example.com", "dbname": "plant"}
    assert _same_data_target("PostgreSQL", old, {"host": "db.example.com", "dbname": "plant"})
    assert not _same_data_target("PostgreSQL", old, {"host": "db.example.com", "dbname": "x"})
    assert not _same_data_target("MCP Server", old, dict(old))


# --------------------------------------------------------------------------- #
# A, end to end: the rewritten secret arms the state-time check again
# --------------------------------------------------------------------------- #
def test_the_kept_schema_reaches_the_state_time_check(monkeypatch):
    from agents.runtime_app import _extract_prompt_and_conn_params

    monkeypatch.delenv("SYSTEM_PROMPT", raising=False)
    monkeypatch.delenv("TABLES_DESC", raising=False)
    payload = _resync(_Secrets(current=CURRENT), RESOLVED)
    prompt, tables_desc, _conn, _cfg = _extract_prompt_and_conn_params(dict(payload), "Athena")
    assert prompt == GENERATED["system_prompt"] and tables_desc == DESC
    sa._remember_turn_time_facts("lake", None, tables_desc, connection_type="Athena")
    assert state_times.has_mapping("lake")
    sql = ("SELECT phase, COUNT(*) AS n FROM orders WHERE ship_date >= "
           "TIMESTAMP '2026-03-02 00:00:00 UTC' AND ship_date < "
           "TIMESTAMP '2026-03-03 00:00:00 UTC' GROUP BY phase")
    bounded = storage_bound_rule.bounded_columns(sql, include_same_column_or=True)[1]
    types_ = {"id": "varchar", "phase": "varchar", "ship_date": _TS, "reject_date": _TS,
              "pack_date": _TS, "qty": "bigint"}
    window = state_times.decide(sql, "orders", bounded, types_,
                                state_times.mappings_for("lake", "orders"),
                                question="How many orders by phase on 3/2?")
    assert window is not None and window.refuse is True
    assert dict((s, o) for s, o, _w, _r in window.rows)["REJECTED"] == "reject_date"


def test_the_edit_that_dropped_the_schema_left_no_check():
    """What an Edit did before: a rewritten secret with no table list gives
    the system no state-time mapping, so the check cannot run."""
    sa._remember_turn_time_facts("lake", None, "", connection_type="Athena")
    assert not state_times.has_mapping("lake")


# --------------------------------------------------------------------------- #
# B. A system agent built with no baked table list says so
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", ["Athena", "PostgreSQL"])
def test_an_sql_system_with_no_baked_schema_logs_one_warning(caplog, connection_type):
    with caplog.at_level(logging.WARNING, logger="agents.system_agent"):
        sa._remember_turn_time_facts("lake", None, "  ", connection_type=connection_type)
    hits = [r for r in caplog.records if "MODE_A_SCHEMA_ABSENT" in r.getMessage()]
    assert len(hits) == 1
    text = hits[0].getMessage()
    assert "lake" in text and "state-time" in text and "storage-bound" in text


@pytest.mark.parametrize(("connection_type", "desc"), [
    ("Athena", DESC), ("PostgreSQL", DESC), ("MCP Server", ""), ("REST API", ""), ("", ""),
])
def test_no_warning_with_a_schema_or_for_a_non_sql_system(caplog, connection_type, desc):
    with caplog.at_level(logging.WARNING, logger="agents.system_agent"):
        sa._remember_turn_time_facts("lake", None, desc, connection_type=connection_type)
    assert not [r for r in caplog.records if "MODE_A_SCHEMA_ABSENT" in r.getMessage()]


def test_create_system_agent_passes_its_connection_type(monkeypatch):
    seen = []
    monkeypatch.setattr(sa, "_remember_turn_time_facts",
                        lambda sid, items, desc, connection_type="": seen.append(
                            (sid, desc, connection_type)))
    monkeypatch.setattr(sa, "_system_tools", lambda *a, **k: ([], ""))
    monkeypatch.setattr(sa, "_compose_system_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(sa, "_system_agent_model", lambda: None)
    monkeypatch.setattr(sa, "_system_agent_hooks", lambda *a, **k: [])
    monkeypatch.setattr(sa, "_attach_schema_names", lambda *a, **k: None)
    monkeypatch.setattr(sa, "Agent", lambda **kwargs: object())
    sa.create_system_agent("lake", "Lake", "lake_db", "", tables_desc="",
                           connection_type="Athena", connection_config={"workgroup": "wg"})
    assert seen == [("lake", "", "Athena")]


# --------------------------------------------------------------------------- #
# B. The runtime audit reports it
# --------------------------------------------------------------------------- #
_ADT_ROOT = Path(__file__).resolve().parents[2]


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "audit_runtime_env_baked_schema", _ADT_ROOT / "scripts" / "audit_runtime_env.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("audit_runtime_env_baked_schema", module)
    spec.loader.exec_module(module)
    return module


_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/agent-config/{}-x1y2z3"


def _env(system_id, connection_type, **extra):
    return {"SYSTEM_ID": system_id, "CONNECTION_TYPE": connection_type,
            "CONNECTION_SECRET_ARN": _ARN.format(system_id), **extra}


@pytest.mark.parametrize(("connection_type", "payload", "status"), [
    ("Athena", {"workgroup": "wg", "database": "lake_db"}, "no-tables-desc"),
    ("Athena", {"workgroup": "wg", "tables_desc": "  "}, "no-tables-desc"),
    ("PostgreSQL", {"user": "reader", "password": "pw-VALUE"}, "no-tables-desc"),
    ("Athena", {"workgroup": "wg", "tables_desc": DESC}, "baked"),
    ("PostgreSQL", {"user": "reader", "tables_desc": DESC}, "baked"),
])
def test_the_audit_names_a_runtime_with_no_baked_schema(connection_type, payload, status):
    audit = _load_audit()
    note = audit.baked_schema_annotation("adt_lake", _env("lake", connection_type),
                                         lambda arn: dict(payload))
    assert note["system"] == "lake" and note["status"] == status
    text = audit.render_baked_schemas([note])
    assert "pw-VALUE" not in text and DESC not in text and "secret:" not in text
    if status == "no-tables-desc":
        assert "no baked table list" in text and "1 with no baked table list" in text


def test_a_table_list_in_the_runtime_env_counts_as_baked():
    audit = _load_audit()
    note = audit.baked_schema_annotation(
        "adt_lake", _env("lake", "Athena", TABLES_DESC="orders(id: varchar)"),
        lambda arn: pytest.fail("read the secret when the env carries the table list"))
    assert note["status"] == "baked"


def test_the_audit_skips_other_runtimes_and_survives_an_unreadable_secret():
    audit = _load_audit()
    assert audit.baked_schema_annotation("adt_tags", _env("tags", "MCP Server"),
                                         lambda arn: pytest.fail("read")) is None
    assert audit.baked_schema_annotation("adt_orchestrator", {"CONNECTION_TYPE": "Athena"},
                                         lambda arn: pytest.fail("read")) is None
    assert audit.baked_schema_annotation(
        "adt_lake", {"SYSTEM_ID": "lake", "CONNECTION_TYPE": "Athena"},
        lambda arn: pytest.fail("read"))["status"] == "no-agent-config"

    def boom(arn):
        raise RuntimeError(f"AccessDenied on {arn} VALUE")

    note = audit.baked_schema_annotation("adt_lake", _env("lake", "Athena"), boom)
    assert note["status"] == "unreadable" and note["detail"] == "RuntimeError"
    assert "VALUE" not in audit.render_baked_schemas([note])


def _run_default_audit(monkeypatch, capsys, secret: dict) -> tuple:
    audit = _load_audit()
    env = _env("lake", "Athena")

    class _Control:
        def list_agent_runtimes(self, **kwargs):
            return {"agentRuntimes": [{"agentRuntimeName": "adt_lake", "agentRuntimeId": "r1"}]}

        def get_agent_runtime(self, agentRuntimeId):
            return {"environmentVariables": env}

    class _Sm:
        def get_secret_value(self, SecretId):
            return {"SecretString": json.dumps(secret)}

    class _Boto3:
        @staticmethod
        def client(name, *a, **k):
            return _Sm() if name == "secretsmanager" else _Control()

    monkeypatch.setitem(sys.modules, "boto3", _Boto3)
    rc = audit.main(["--strict", "--region", "us-east-1"])
    return rc, capsys.readouterr().out


def test_the_default_audit_run_prints_the_baked_schema_section(monkeypatch, capsys):
    """The post-deploy audit runs with no extra flag, so the section is part
    of every run: a runtime with no baked table list is never reported clean
    by omission. It annotates and never changes the exit code."""
    rc_absent, out = _run_default_audit(monkeypatch, capsys,
                                        {"workgroup": "wg", "database": "lake_db"})
    assert "lake: no baked table list" in out
    assert "1 with no baked table list" in out
    rc_baked, out = _run_default_audit(monkeypatch, capsys,
                                       {"workgroup": "wg", "tables_desc": DESC})
    assert "lake: baked table list present" in out
    assert "0 with no baked table list" in out
    assert rc_absent == rc_baked
