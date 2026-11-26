# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The agent-config secret stays under the Secrets Manager value limit.

Each system's ``adt/agent-config/<id>`` secret carries its connection config
(login included), the baked table description, the system prompt and the
KNOWN CONCEPT MAPPINGS items in ONE value, and Secrets Manager refuses a value
over 65,536 bytes. A refused write leaves the runtime without its login (the
plaintext fallback strips credentials), so the agent cannot read its system.
Provision caps the items at ``_CONCEPT_MAPPING_MAX_ITEMS`` before they join
the rest, so a wide schema plus a full item list could pass the limit.

Every writer of the secret now sizes the serialized value first and, over a
safe bound, trims the concept mappings in provision's order (one item per
concept per round, so every concept keeps an item first), or drops the list
when even one item per concept does not fit. A value over the bound without
any concept mappings is written as before. All offline: boto3 is stubbed.
"""
import json
import logging
import types
from unittest.mock import patch

import pytest

from agents import agent_manager as am
from agents.agent_manager import AgentManager
from agents.onboard import _concept_mapping_items
from agents.provisioner import create_runtime
from agents.provisioner.models import CreateRuntimeInput

SECRET_LIMIT = 65_536


class _Secrets:
    """Secrets Manager stand-in that records every written value."""

    class ResourceExistsException(Exception):
        pass

    class ResourceNotFoundException(Exception):
        pass

    def __init__(self, current: dict | None = None):
        self.current = current
        self.written: list[str] = []
        self.exceptions = types.SimpleNamespace(
            ResourceExistsException=self.ResourceExistsException,
            ResourceNotFoundException=self.ResourceNotFoundException)

    def create_secret(self, Name=None, SecretString=None):  # noqa: N803 - boto3 kwarg names
        self.written.append(SecretString)
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{Name}"}

    def put_secret_value(self, SecretId=None, SecretString=None):  # noqa: N803
        self.written.append(SecretString)
        return {}

    def describe_secret(self, SecretId=None):  # noqa: N803
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{SecretId}"}

    def get_secret_value(self, SecretId=None):  # noqa: N803
        if self.current is None:
            raise self.ResourceNotFoundException(SecretId)
        return {"SecretString": json.dumps(self.current)}


def _boto3(secrets):
    def client(service, region_name=None):
        assert service == "secretsmanager", service
        return secrets
    return types.SimpleNamespace(client=client)


_CONNECTION = {"host": "db.example.internal", "port": 5432, "dbname": "plant",
               "user": "reader", "password": "pw-EXAMPLE", "schema": "plant_data"}
_PROMPT = "You are the agent for the example system. " * 40


def _rows(n_concepts: int, per_concept: int) -> list:
    """Provision-body rows: ``per_concept`` long qualified fields for each of
    ``n_concepts`` committed concepts (a wide relational schema's shape)."""
    return [{"table": f"process_measurement_readings_{k:03d}",
             "field": (f"process_measurement_readings_{k:03d}."
                       f"sensor_channel_{j:03d}_engineering_units_hourly_average_value"
                       "_rolling_window_minutes_smoothed"),
             "mappings": [{"concept": f"process-measurement-{k:03d}", "role": "measure"}]}
            for k in range(n_concepts) for j in range(per_concept)]


def _items(n_concepts: int, per_concept: int, max_items: int = 1_000) -> list:
    concepts = {f"process-measurement-{k:03d}" for k in range(n_concepts)}
    return _concept_mapping_items(_rows(n_concepts, per_concept), committable=concepts,
                                  max_items=max_items)


def _config(items: list) -> dict:
    return {**_CONNECTION, "concept_mappings": items}


def _write_at_provision(cfg: dict, tables_desc: str) -> str:
    """The provision writer's value for ``cfg`` (stubbed Secrets Manager)."""
    secrets = _Secrets()
    with patch("boto3.client", _boto3(secrets).client):
        arn = AgentManager(mode="cloud")._upsert_agent_config_secret(
            "plant-db", dict(cfg), _PROMPT, tables_desc, "us-east-1")
    assert arn.endswith("adt/agent-config/plant-db")
    assert len(secrets.written) == 1
    return secrets.written[0]


def _today(cfg: dict, tables_desc: str) -> str:
    """The value today's writer serializes, untrimmed."""
    payload = dict(cfg)
    if tables_desc:
        payload["tables_desc"] = tables_desc
    payload["system_prompt"] = _PROMPT
    return json.dumps(payload)


def _concepts(items: list) -> set:
    return {it["concept"] for it in items}


_WIDE_SCHEMA = "readings(" + ", ".join(f"col_{i:04d}: numeric" for i in range(525)) + ")"


def test_the_shapes_match_the_reported_boundary():
    # 200 items and a wide baked schema fit the limit; 300 do not.
    assert 9_000 < len(_WIDE_SCHEMA) < 11_000
    assert len(_today(_config(_items(30, 10, max_items=200)), _WIDE_SCHEMA)) < SECRET_LIMIT
    assert len(_today(_config(_items(30, 10)), _WIDE_SCHEMA)) > SECRET_LIMIT


def test_200_items_and_a_wide_schema_are_written_as_today():
    cfg = _config(_items(30, 10, max_items=200))
    assert _write_at_provision(cfg, _WIDE_SCHEMA) == _today(cfg, _WIDE_SCHEMA)


def test_300_items_and_a_wide_schema_are_trimmed_to_fit_keeping_every_concept():
    items = _items(30, 10)
    assert len(items) == 300
    written = _write_at_provision(_config(items), _WIDE_SCHEMA)
    assert len(written.encode()) <= SECRET_LIMIT
    payload = json.loads(written)
    kept = payload["concept_mappings"]
    assert 30 <= len(kept) < 300
    assert _concepts(kept) == _concepts(items)
    # The same order the provision item cap uses, and as many as fit.
    assert kept == _items(30, 10, max_items=len(kept))
    from agents.runtime_provision_helpers import AGENT_CONFIG_SECRET_MAX_BYTES
    assert AGENT_CONFIG_SECRET_MAX_BYTES < SECRET_LIMIT
    assert len(written.encode()) <= AGENT_CONFIG_SECRET_MAX_BYTES
    one_more = _today(_config(_items(30, 10, max_items=len(kept) + 1)), _WIDE_SCHEMA)
    assert len(one_more.encode()) > AGENT_CONFIG_SECRET_MAX_BYTES
    # Nothing else in the value changes, the login included.
    assert {k: v for k, v in payload.items() if k != "concept_mappings"} == {
        **_CONNECTION, "tables_desc": _WIDE_SCHEMA, "system_prompt": _PROMPT}
    assert list(payload) == list(json.loads(_today(_config(items), _WIDE_SCHEMA)))


def test_when_one_item_per_concept_does_not_fit_the_list_is_dropped(caplog):
    items = _items(300, 1)
    assert len(_concepts(items)) == 300
    with caplog.at_level(logging.WARNING):
        written = _write_at_provision(_config(items), _WIDE_SCHEMA)
    assert len(written.encode()) <= SECRET_LIMIT
    payload = json.loads(written)
    assert "concept_mappings" not in payload
    assert payload == {**_CONNECTION, "tables_desc": _WIDE_SCHEMA, "system_prompt": _PROMPT}
    assert any("concept mappings" in r.getMessage() and "plant-db" in r.getMessage()
               for r in caplog.records)
    assert not any("pw-EXAMPLE" in r.getMessage() for r in caplog.records)


def test_over_the_bound_without_concept_mappings_the_value_is_written_as_before():
    # A table description alone over the bound is not this change's to fix:
    # the value goes out as it did before.
    huge = "t(" + "x: int, " * 9_000 + ")"
    cfg = _config(_items(5, 2))
    assert _write_at_provision(cfg, huge) == _today(cfg, huge)


def test_the_resync_rewrite_fits_the_same_way():
    items = _items(30, 10)
    cfg = {**_config(items), "tables_desc": _WIDE_SCHEMA, "system_prompt": _PROMPT}
    secrets = _Secrets()
    with patch.object(am, "boto3", _boto3(secrets)):
        arn = AgentManager(mode="cloud")._resync_rewrite_secret("plant-db", cfg, "us-east-1")
    assert arn
    written = secrets.written[-1]
    assert len(written.encode()) <= SECRET_LIMIT
    kept = json.loads(written)["concept_mappings"]
    assert _concepts(kept) == _concepts(items) and len(kept) < 300


def test_the_business_context_merge_fits_the_same_way():
    items = _items(30, 10)
    current = {**_config(items), "tables_desc": _WIDE_SCHEMA, "system_prompt": _PROMPT}
    state = {"business_context": "Lines run two shifts.", "enabled": True,
             "updated_at": None, "context_hash": None}
    merged = AgentManager._merged_agent_config_payload(json.dumps(current), state)
    assert len(merged.encode()) <= SECRET_LIMIT
    payload = json.loads(merged)
    assert payload["business_context"] == "Lines run two shifts."
    assert _concepts(payload["concept_mappings"]) == _concepts(items)


def test_the_provisioner_create_runtime_fits_the_same_way():
    items = _items(30, 10)

    class _Control:
        class exceptions:  # noqa: N801 - boto3 shape
            ConflictException = type("ConflictException", (Exception,), {})

        def create_agent_runtime(self, **kwargs):
            return {"agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:"
                                       "111122223333:runtime/adt_plant_db-1",
                    "agentRuntimeId": "adt_plant_db-1"}

    secrets = _Secrets()
    inp = CreateRuntimeInput(
        system_id="plant-db", system_name="Plant DB", schema="plant_data",
        connection_type="PostgreSQL",
        connection_config={**_config(items), "tables_desc": _WIDE_SCHEMA,
                           "system_prompt": _PROMPT},
        region="us-east-1",
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-system-agent",
        runtime_role_arn="arn:aws:iam::111122223333:role/runtime",
        vpc_subnets=["subnet-a"], vpc_security_group="sg-1")
    create_runtime(inp, control=_Control(), secrets=secrets)
    written = secrets.written[-1]
    assert len(written.encode()) <= SECRET_LIMIT
    assert _concepts(json.loads(written)["concept_mappings"]) == _concepts(items)


# --------------------------------------------------------------------------- #
# Between the safe bound and the service limit
# --------------------------------------------------------------------------- #
# Without its concept mappings the value is over the 61,440-byte bound but
# under Secrets Manager's 65,536-byte limit. Dropping the mappings makes it
# fit, so every writer drops them rather than send a value the service refuses.

def _desc_for_rest(rest_bytes: int) -> str:
    base = len(json.dumps({**_CONNECTION, "tables_desc": "",
                           "system_prompt": _PROMPT}).encode())
    return "t" * (rest_bytes - base)


def _written_by(writer: str, items: list, desc: str) -> str:
    if writer == "provision":
        return _write_at_provision(_config(items), desc)
    cfg = {**_config(items), "tables_desc": desc, "system_prompt": _PROMPT}
    if writer == "merge":
        state = {"business_context": "Lines run two shifts.", "enabled": True,
                 "updated_at": None, "context_hash": None}
        return AgentManager._merged_agent_config_payload(json.dumps(cfg), state)
    secrets = _Secrets()
    if writer == "resync":
        with patch.object(am, "boto3", _boto3(secrets)):
            assert AgentManager(mode="cloud")._resync_rewrite_secret(
                "plant-db", cfg, "us-east-1")
        return secrets.written[-1]

    class _Control:
        class exceptions:  # noqa: N801 - boto3 shape
            ConflictException = type("ConflictException", (Exception,), {})

        def create_agent_runtime(self, **kwargs):
            return {"agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:"
                                       "111122223333:runtime/adt_plant_db-1",
                    "agentRuntimeId": "adt_plant_db-1"}

    create_runtime(CreateRuntimeInput(
        system_id="plant-db", system_name="Plant DB", schema="plant_data",
        connection_type="PostgreSQL", connection_config=cfg, region="us-east-1",
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-system-agent",
        runtime_role_arn="arn:aws:iam::111122223333:role/runtime",
        vpc_subnets=["subnet-a"], vpc_security_group="sg-1"),
        control=_Control(), secrets=secrets)
    return secrets.written[-1]


@pytest.mark.parametrize("writer", ["provision", "resync", "merge", "create_runtime"])
def test_over_the_bound_but_under_the_limit_without_mappings_the_list_is_dropped(writer):
    from agents.runtime_provision_helpers import AGENT_CONFIG_SECRET_MAX_BYTES
    desc = _desc_for_rest(61_469)
    items = _items(10, 10)
    assert len(json.dumps({**_CONNECTION, "tables_desc": desc,
                           "system_prompt": _PROMPT}).encode()) == 61_469
    assert 61_469 > AGENT_CONFIG_SECRET_MAX_BYTES
    assert len(_today(_config(items), desc).encode()) > SECRET_LIMIT
    written = _written_by(writer, items, desc)
    assert len(written.encode()) <= SECRET_LIMIT
    payload = json.loads(written)
    assert "concept_mappings" not in payload
    assert payload["password"] == "pw-EXAMPLE" and payload["tables_desc"] == desc
    assert payload["system_prompt"] == _PROMPT


def test_over_the_limit_without_mappings_the_value_is_still_written_as_before():
    # Past the service limit even without the list, nothing this change does
    # can make it fit: the value goes out unchanged, as on main.
    desc = _desc_for_rest(SECRET_LIMIT + 1)
    cfg = _config(_items(10, 10))
    assert _write_at_provision(cfg, desc) == _today(cfg, desc)


# --------------------------------------------------------------------------- #
# The measured workshop systems' values are byte-identical
# --------------------------------------------------------------------------- #
# Their items as provision ships them, with a table description larger than
# the ERP's measured one (27,443 characters on 2026-10-14) and a long prompt:
# every one is far under the bound and written exactly as before.

def _measured(system_id: str) -> list:
    from pathlib import Path
    fixtures = Path(__file__).parent / "fixtures"
    if system_id == "erp":
        live = json.loads((fixtures / "erp_concept_mappings_live.json").read_text())["items"]
    elif system_id == "sitewise-direct":
        live = json.loads((fixtures / "sitewise_concept_mappings_live.json").read_text())["items"]
    else:
        live = json.loads((fixtures / "concept_mappings_workshop_systems.json").read_text()
                          )["systems"][system_id]
    rows = [{"table": i["field"].split(".", 1)[0], "field": i["field"],
             "mappings": [{"concept": i["concept"], "role": i["role"]}]} for i in live]
    return _concept_mapping_items(rows, committable={i["concept"] for i in live})


@pytest.mark.parametrize("system_id", ["erp", "mes-ind", "mes-mil", "mes-det", "mes-cr",
                                       "plm", "sitewise-direct"])
def test_the_measured_systems_values_are_byte_identical(system_id):
    desc = "t(" + "column_name: character varying, " * 940 + ")"
    assert len(desc) > 30_000
    cfg = _config(_measured(system_id))
    assert _write_at_provision(cfg, desc) == _today(cfg, desc)
