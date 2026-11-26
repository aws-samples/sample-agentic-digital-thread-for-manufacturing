# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An S3 Tables runtime must receive its baked table list.

Live bug: an S3 Tables runtime logged ``MODE_A_SCHEMA_ABSENT ... has no
baked table list`` on every cold start, so the agent did not know its columns
and the storage-window / state-time rules never fired. Two gaps:

1. A re-provision of an EXISTING cloud runtime takes the fast path, which wrote
   no agent-config secret, so the freshly baked ``tables_desc`` never reached
   the runtime.
2. ``server._restore_direct_tables_desc`` rebuilt ``tables_desc`` from the graph
   only for Athena, so after any app restart an S3 Tables system was restored
   with ``tables_desc=""``.

Both are fixed engine-neutrally (Athena unchanged). These tests fail on the
pre-fix code.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agent_manager import AgentManager  # noqa: E402
from agents.connection_types import ATHENA, S3TABLES_DUCKDB  # noqa: E402

_SYS = "s3t-baked"
_RUNTIME = "adt_s3t_baked"
_TARGET = "s3t-baked"
_GATEWAY_URL = "https://gw.example.com/mcp"
_TABLES_DESC = ("process_data_global.segment_record_details_raw(id: varchar, "
                "source_region: varchar (partition key))")


class _FakeControl:
    """bedrock-agentcore-control stand-in presenting a READY target + READY
    runtime, so start_agent takes the fast path."""

    def list_gateway_targets(self, gatewayIdentifier=None):  # noqa: N803
        return {"items": [{"name": _TARGET, "targetId": "tgt-1", "status": "READY"}]}

    def list_agent_runtimes(self):
        return {"agentRuntimes": [{
            "agentRuntimeName": _RUNTIME, "agentRuntimeId": "rt-1",
            "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:444455556666:runtime/rt-1",
            "status": "READY"}]}


class _FakeSM:
    """A version-stage-aware Secrets Manager stand-in holding an EXISTING
    agent-config secret, so the fast-path compare-and-set is exercised: ``get``
    then a pending ``put`` then an ``AWSCURRENT`` stage move. A put to a
    non-AWSCURRENT stage writes a new version WITHOUT changing what AWSCURRENT
    serves; promotion only happens through ``update_secret_version_stage``,
    which rejects the move when ``RemoveFromVersionId`` is no longer AWSCURRENT
    (a concurrent writer moved it). ``written`` captures the payload once it
    reaches AWSCURRENT."""

    class exceptions:
        class ResourceExistsException(Exception):
            pass

        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, existing=None):
        self.current = dict(existing or {"table_bucket_arn": "arn:bucket"})
        self.written: dict | None = None
        self._current_version = "v-old"
        self._versions = {"v-old": dict(self.current)}
        self._seq = 0

    def get_secret_value(self, SecretId=None):  # noqa: N803
        return {"SecretString": json.dumps(self.current), "VersionId": self._current_version}

    def put_secret_value(self, SecretId=None, SecretString=None,  # noqa: N803
                         ClientRequestToken=None, VersionStages=None):  # noqa: N803
        payload = json.loads(SecretString)
        self._seq += 1
        version_id = f"v-new-{self._seq}"
        self._versions[version_id] = payload
        stages = VersionStages or ["AWSCURRENT"]
        if "AWSCURRENT" in stages:              # direct current write (no CAS)
            self._current_version = version_id
            self.current = dict(payload)
            self.written = payload
        return {"ARN": f"arn:aws:secretsmanager:us-east-1:111:secret:{SecretId}",
                "VersionId": version_id}

    def update_secret_version_stage(self, SecretId=None, VersionStage=None,  # noqa: N803
                                    MoveToVersionId=None, RemoveFromVersionId=None):  # noqa: N803
        if VersionStage != "AWSCURRENT":
            return {}                           # clearing a transient stage: no-op
        if RemoveFromVersionId != self._current_version:
            raise self.exceptions.ResourceExistsException(
                "AWSCURRENT is not RemoveFromVersionId")
        self._current_version = MoveToVersionId
        self.current = dict(self._versions[MoveToVersionId])
        self.written = self._versions[MoveToVersionId]
        return {}

    def concurrent_write(self, payload):
        """Simulate another task moving AWSCURRENT to its own version."""
        self._seq += 1
        version_id = f"v-other-{self._seq}"
        self._versions[version_id] = dict(payload)
        self._current_version = version_id
        self.current = dict(payload)


def _mgr():
    return AgentManager(mode="cloud", gateway_id="gw-123", gateway_url=_GATEWAY_URL,
                        ecr_image_uri="444455556666.dkr.ecr.us-east-1.amazonaws.com/adt:h",
                        runtime_role_arn="arn:role")


def _fast_path_start(connection_type, existing=None):
    control, sm = _FakeControl(), _FakeSM(existing=existing)
    events = []

    def _client(service, region_name=None):
        if service == "bedrock-agentcore-control":
            return control
        if service == "secretsmanager":
            return sm
        raise AssertionError(f"unexpected client {service}")

    mgr = _mgr()
    with patch("boto3.client", _client), \
            patch.object(AgentManager, "_start_cloud", autospec=True,
                         return_value="cloud://created") as start_cloud, \
            patch.object(AgentManager, "_bump_runtime_version", autospec=True,
                         side_effect=lambda self, *a, **k: events.append("roll") or "ok"), \
            patch.object(AgentManager, "_await_runtime_ready", autospec=True,
                         side_effect=lambda self, *a, **k: events.append("ready") or True):
        url = mgr.start_agent(
            system_id=_SYS, system_name="S3T", schema="s3tables",
            system_prompt="p", tables_desc=_TABLES_DESC,
            connection_type=connection_type,
            connection_config={"table_bucket_arn": "arn:aws:s3tables:us-east-1:1:bucket/b",
                               "region": "us-east-1"})
    return url, sm, start_cloud, events


def test_fast_path_provision_writes_tables_desc_to_the_agent_config_secret():
    # The existing S3 Tables runtime is reused (fast path, _start_cloud NOT
    # called), yet the baked tables_desc is merged into the agent-config secret
    # and the runtime is rolled and awaited READY before registering.
    url, sm, start_cloud, events = _fast_path_start(S3TABLES_DUCKDB)
    assert url == _GATEWAY_URL
    assert not start_cloud.called                # runtime reused, not recreated
    assert sm.written is not None                # the secret WAS written on the fast path
    assert sm.written.get("tables_desc") == _TABLES_DESC
    assert sm.written.get("system_prompt") == "p"
    assert events == ["roll", "ready"]           # version bump, then READY wait


def test_fast_path_provision_writes_tables_desc_for_athena_too():
    # Athena uses the same fast path; its baked schema must land the same way.
    _url, sm, _sc, events = _fast_path_start(ATHENA)
    assert sm.written is not None and sm.written.get("tables_desc") == _TABLES_DESC
    assert events == ["roll", "ready"]


# --------------------------------------------------------------------------- #
# Restore rebuilds a non-empty S3 Tables tables_desc from the graph            #
# --------------------------------------------------------------------------- #
_GRAPH_ROWS = [
    {"tbl": "process_data_global.segment_record_details_raw", "relation_kind": "base_table",
     "col": "id", "dtype": "varchar", "is_part": False},
    {"tbl": "process_data_global.segment_record_details_raw", "relation_kind": "base_table",
     "col": "source_region", "dtype": "varchar", "is_part": True},
]


def test_restore_rebuilds_s3tables_tables_desc_from_the_graph(monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", lambda *a, **k: list(_GRAPH_ROWS))
    desc = server._restore_direct_tables_desc(_SYS, S3TABLES_DUCKDB)
    assert desc                                               # non-empty (was "" before)
    # Live fix: the restored desc carries the FULLY QUALIFIED s3t.<ns>.<table>
    # SQL name, so the restored agent writes the exact relation instead of
    # guessing the namespace.
    from agents.system_agent import desc_qualified_table_names
    assert "s3t.process_data_global.segment_record_details_raw" in desc_qualified_table_names(desc)
    assert "source_region" in desc and "partition key" in desc        # partition flag


def test_restore_athena_tables_desc_unchanged(monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query",
                        lambda *a, **k: [{"tbl": "segments", "relation_kind": "base_table",
                                          "col": "id", "dtype": "varchar", "is_part": False}])
    desc = server._restore_direct_tables_desc(_SYS, ATHENA)
    assert "segments" in desc and "id" in desc
    assert "s3t." not in desc                   # Athena render is NOT catalog-prefixed


def test_restore_non_sql_type_still_empty(monkeypatch):
    import agents.server as server
    monkeypatch.setattr(server, "neptune_query", lambda *a, **k: list(_GRAPH_ROWS))
    assert server._restore_direct_tables_desc(_SYS, "SiteWise (Direct)") == ""


# --------------------------------------------------------------------------- #
# Review live-tables-desc: version deploy + read-modify-write merge            #
# --------------------------------------------------------------------------- #
def test_fast_path_deploys_a_runtime_version_that_rereads_the_secret(monkeypatch):
    # F1: a provision fast path must write the secret AND deploy a new runtime
    # version (so a warm container re-reads it), in that order.
    mgr = _mgr()
    events = []
    monkeypatch.setattr(mgr, "_write_agent_config_on_fast_path",
                        lambda *a, **k: events.append("secret"))
    monkeypatch.setattr(mgr, "_bump_runtime_version",
                        lambda *a, **k: events.append("runtime") or "ok")
    monkeypatch.setattr(mgr, "_await_runtime_ready",
                        lambda *a, **k: events.append("ready") or True)
    mgr._register_fast_path(
        _SYS, "S3T", "s3tables", "prompt", "ns.table(id: varchar)", S3TABLES_DUCKDB,
        {"table_bucket_arn": "arn:aws:s3tables:us-east-1:1:bucket/demo"},
        "gw-123", _GATEWAY_URL, False)
    assert events == ["secret", "runtime", "ready"]


def test_fast_path_cold_restart_writes_and_rolls_nothing(monkeypatch):
    # fast_path_only (a cold-restart register-only call) must not write the
    # secret or roll the runtime.
    mgr = _mgr()
    events = []
    monkeypatch.setattr(mgr, "_write_agent_config_on_fast_path",
                        lambda *a, **k: events.append("secret"))
    monkeypatch.setattr(mgr, "_bump_runtime_version",
                        lambda *a, **k: events.append("runtime") or "ok")
    monkeypatch.setattr(mgr, "_await_runtime_ready",
                        lambda *a, **k: events.append("ready") or True)
    mgr._register_fast_path(
        _SYS, "S3T", "s3tables", "prompt", "ns.table(id: varchar)", S3TABLES_DUCKDB,
        {"table_bucket_arn": "arn:aws:s3tables:us-east-1:1:bucket/demo"},
        "gw-123", _GATEWAY_URL, True)
    assert events == []


def test_fast_path_secret_update_preserves_existing_sibling_keys(monkeypatch):
    # F2: the merge is read-modify-write; every sibling key survives, and only
    # the generated keys the provision built are updated.
    existing = {
        "table_bucket_arn": "arn:aws:s3tables:us-east-1:1:bucket/demo",
        "external_id": "existing-external-id",
        "business_context": "existing context",
        "concept_mappings": [{"concept": "c", "field": "ns.table.id"}],
        "field_descriptions": {"ns.table.id": "identifier"},
        "conn_params": {"host": "db", "dbname": "d"},
        "tables_desc": "old schema",
        "system_prompt": "old prompt",
    }
    sm = _FakeSM(existing=existing)
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    new_schema = "ns.table(id: varchar, name: varchar)"
    changed = mgr._write_agent_config_on_fast_path(
        _SYS, "s3tables", "new prompt", new_schema, S3TABLES_DUCKDB,
        {"table_bucket_arn": existing["table_bucket_arn"]})
    assert changed is True
    for key in ("external_id", "business_context", "concept_mappings",
                "field_descriptions", "conn_params"):
        assert sm.written[key] == existing[key]       # every sibling preserved
    assert sm.written["tables_desc"] == new_schema
    assert sm.written["system_prompt"] == "new prompt"


def test_fast_path_names_only_tables_desc_never_replaces_a_complete_schema(monkeypatch):
    # F4: a discovery that did not complete yields a bare list of mapped names
    # (no column detail). It must never overwrite a complete stored schema.
    existing = {"table_bucket_arn": "arn:bucket",
                "tables_desc": "ns.table(id: varchar, name: varchar)"}
    sm = _FakeSM(existing=existing)
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    changed = mgr._write_agent_config_on_fast_path(
        _SYS, "s3tables", "p", "ns.table, ns.other", S3TABLES_DUCKDB,
        {"table_bucket_arn": "arn:bucket"})
    assert changed is False
    assert sm.written is None                         # no write, no roll
    assert sm.current["tables_desc"] == existing["tables_desc"]


def test_fast_path_empty_tables_desc_writes_nothing(monkeypatch):
    # An empty/partial tables_desc must never delete a valid stored schema.
    existing = {"table_bucket_arn": "arn:bucket", "tables_desc": "good schema"}
    sm = _FakeSM(existing=existing)
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    changed = mgr._write_agent_config_on_fast_path(
        _SYS, "s3tables", "p", "", S3TABLES_DUCKDB, {"table_bucket_arn": "arn:bucket"})
    assert changed is False
    assert sm.written is None                         # no write
    assert sm.current["tables_desc"] == "good schema"


def test_fast_path_missing_secret_writes_nothing(monkeypatch):
    # No current secret to merge into -> write nothing (the create path handles a
    # first provision; the fast path never overwrites from scratch).
    class _NoSecret(_FakeSM):
        def get_secret_value(self, SecretId=None):  # noqa: N803
            raise self.exceptions.ResourceNotFoundException()
    sm = _NoSecret()
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    changed = mgr._write_agent_config_on_fast_path(
        _SYS, "s3tables", "p", _TABLES_DESC, S3TABLES_DUCKDB, {"table_bucket_arn": "arn:bucket"})
    assert changed is False and sm.written is None


def test_fast_path_write_failure_raises_not_reported_as_no_op(monkeypatch):
    # F1: a genuine write failure must RAISE, never return False. A False would
    # let the caller treat an unpersisted merge as "already current" and register
    # a warm runtime still reading the stale secret.
    class _PutFails(_FakeSM):
        def put_secret_value(self, **kwargs):
            raise RuntimeError("secretsmanager unavailable")
    sm = _PutFails(existing={"table_bucket_arn": "arn:bucket", "tables_desc": "old(x: varchar)"})
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    with pytest.raises(RuntimeError):
        mgr._write_agent_config_on_fast_path(
            _SYS, "s3tables", "p", _TABLES_DESC, S3TABLES_DUCKDB,
            {"table_bucket_arn": "arn:bucket"})


def test_fast_path_merge_refuses_when_awscurrent_moved_under_it(monkeypatch):
    # F1 (r2): the merge is a real compare-and-set via version stages. The write
    # lands on a pending stage, and another task moves AWSCURRENT before the
    # promotion. UpdateSecretVersionStage then rejects the move, the merge raises,
    # and the concurrent writer's value is never clobbered.
    sm = _FakeSM(existing={"table_bucket_arn": "arn:bucket", "tables_desc": "old(x: varchar)"})
    original_put = sm.put_secret_value

    def put_then_concurrent_move(**kwargs):
        result = original_put(**kwargs)             # our pending version
        sm.concurrent_write({"table_bucket_arn": "arn:bucket",
                             "tables_desc": "other(id: varchar)", "credential": "newer"})
        return result

    sm.put_secret_value = put_then_concurrent_move
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    with pytest.raises(RuntimeError):
        mgr._write_agent_config_on_fast_path(
            _SYS, "s3tables", "p", _TABLES_DESC, S3TABLES_DUCKDB,
            {"table_bucket_arn": "arn:bucket"})
    assert sm.current["tables_desc"] == "other(id: varchar)"  # concurrent write survives
    assert sm.current.get("credential") == "newer"


def test_fast_path_merge_writes_when_awscurrent_is_unchanged(monkeypatch):
    # The complement: when AWSCURRENT still points at the read version, the
    # pending version is promoted to AWSCURRENT and the merge is written.
    sm = _FakeSM(existing={"table_bucket_arn": "arn:bucket", "tables_desc": "old(x: varchar)"})
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    changed = mgr._write_agent_config_on_fast_path(
        _SYS, "s3tables", "p", _TABLES_DESC, S3TABLES_DUCKDB, {"table_bucket_arn": "arn:bucket"})
    assert changed is True and sm.written is not None
    assert sm.written["tables_desc"] == _TABLES_DESC
    assert sm.current["tables_desc"] == _TABLES_DESC  # promoted to AWSCURRENT


# --------------------------------------------------------------------------- #
# Review live-config-refresh F1/F2: a failed roll or READY wait must not       #
# register a stale warm runtime; server restore is a no-write registration.    #
# --------------------------------------------------------------------------- #
def _register_fast(mgr):
    return mgr._register_fast_path(
        _SYS, "S3T", "s3tables", "prompt", _TABLES_DESC, S3TABLES_DUCKDB,
        {"table_bucket_arn": "arn:aws:s3tables:us-east-1:1:bucket/demo"},
        "gw-123", _GATEWAY_URL, False)


def test_runtime_bump_failure_does_not_register_stale_runtime(monkeypatch):
    mgr = _mgr()
    monkeypatch.setattr(mgr, "_write_agent_config_on_fast_path", lambda *a, **k: True)
    monkeypatch.setattr(mgr, "_bump_runtime_version", lambda *a, **k: "failed")
    monkeypatch.setattr(mgr, "_await_runtime_ready", lambda *a, **k: True)
    with pytest.raises(RuntimeError):
        _register_fast(mgr)
    assert _SYS not in mgr._agents


def test_runtime_ready_timeout_does_not_register_stale_runtime(monkeypatch):
    mgr = _mgr()
    monkeypatch.setattr(mgr, "_write_agent_config_on_fast_path", lambda *a, **k: True)
    monkeypatch.setattr(mgr, "_bump_runtime_version", lambda *a, **k: "ok")
    monkeypatch.setattr(mgr, "_await_runtime_ready", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        _register_fast(mgr)
    assert _SYS not in mgr._agents


def test_no_op_write_registers_without_a_roll(monkeypatch):
    # When the merge changed nothing, the runtime is already on that config: no
    # roll, no READY wait, and the system still registers.
    mgr = _mgr()
    events = []
    monkeypatch.setattr(mgr, "_write_agent_config_on_fast_path", lambda *a, **k: False)
    monkeypatch.setattr(mgr, "_bump_runtime_version",
                        lambda *a, **k: events.append("roll") or "ok")
    monkeypatch.setattr(mgr, "_await_runtime_ready",
                        lambda *a, **k: events.append("ready") or True)
    assert _register_fast(mgr) == _GATEWAY_URL
    assert events == [] and _SYS in mgr._agents


def test_server_restore_registers_with_no_write(monkeypatch):
    # A server restore takes the no-write fast path first and never refreshes
    # generated config. Only an explicit provision refreshes.
    import agents.server as server
    captured = {}

    class _Manager:
        def start_agent(self, **kwargs):
            captured.update(kwargs)
            return _GATEWAY_URL

    monkeypatch.setattr(server, "_agent_manager", _Manager())
    monkeypatch.setattr(server, "add_system_agent", lambda *a, **k: None)
    assert server._restore_one(
        system_id=_SYS, system_name="S3T", schema="",
        system_prompt="", tables_desc=_TABLES_DESC,
        connection_type=S3TABLES_DUCKDB,
        connection_config={"table_bucket_arn": "arn:bucket"}) == _GATEWAY_URL
    assert captured.get("fast_path_only") is True
    assert captured.get("refresh_generated_config") is False


def test_server_restore_recreates_a_missing_runtime(monkeypatch):
    # F3 (r2): defaulting restore to the no-write fast path must not block the
    # self-healing create path. When no READY fast path exists
    # (FastPathUnavailable), restore falls back to the create path and rebuilds
    # the runtime -- it never refreshes generated config on a reused runtime.
    import agents.server as server
    from agents.agent_manager import FastPathUnavailable
    calls = []

    class _Manager:
        def start_agent(self, **kwargs):
            calls.append(kwargs.get("fast_path_only"))
            if kwargs.get("fast_path_only"):
                raise FastPathUnavailable(_SYS)
            return "cloud://created"

    monkeypatch.setattr(server, "_agent_manager", _Manager())
    monkeypatch.setattr(server, "add_system_agent", lambda *a, **k: None)
    assert server._restore_one(
        system_id=_SYS, system_name="S3T", schema="",
        system_prompt="", tables_desc=_TABLES_DESC,
        connection_type=S3TABLES_DUCKDB,
        connection_config={"table_bucket_arn": "arn:bucket"}) == "cloud://created"
    assert calls == [True, False]           # no-write fast path first, then create


def test_mixed_complete_and_names_only_tables_desc_is_not_complete():
    # F2 (r2): a non-empty SUBSET (one table with columns, another names-only)
    # is NOT complete. Writing it would overwrite a complete stored schema.
    from agents.agent_manager import _tables_desc_is_complete
    assert _tables_desc_is_complete("ns.good(id: varchar), ns.missing") is False
    assert _tables_desc_is_complete("ns.good(id: varchar), ns.other(x: int)") is True
    assert _tables_desc_is_complete(
        "process_data_global.seg(id: varchar, region: varchar (partition key))") is True


# --------------------------------------------------------------------------- #
# Review live-config-refresh r3: transactional delivery + expected/complete    #
# --------------------------------------------------------------------------- #
def test_failed_runtime_roll_restores_previous_agent_config(monkeypatch):
    # F1 (r3): the secret is promoted before the roll. When the roll fails, the
    # promoted version is CAS-restored to the baseline, so an identical retry
    # sees a stale secret and rolls again instead of registering a warm runtime
    # on config the failed provision delivered.
    old = {"tables_desc": "old.table(id: varchar)", "credential": "keep"}
    sm = _FakeSM(existing=old)
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    monkeypatch.setattr(mgr, "_bump_runtime_version", lambda *a, **k: "failed")
    monkeypatch.setattr(mgr, "_await_runtime_ready", lambda *a, **k: True)
    with pytest.raises(RuntimeError):
        _register_fast(mgr)
    assert sm.current == old                 # baseline restored
    assert _SYS not in mgr._agents


def test_failed_ready_wait_restores_baseline_and_compensates(monkeypatch):
    # F2 (r4): a READY timeout after an accepted roll restores the baseline
    # schema AND rolls again so the final container re-reads it. Here the
    # compensating READY also fails, so the delivery-pending marker is left on
    # the restored baseline and the provision fails retryable (no register).
    from agents.agent_manager import _DELIVERY_PENDING_KEY
    old = {"tables_desc": "old.table(id: varchar)", "credential": "keep"}
    sm = _FakeSM(existing=old)
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    rolls = []
    monkeypatch.setattr(mgr, "_bump_runtime_version", lambda *a, **k: rolls.append(1) or "ok")
    monkeypatch.setattr(mgr, "_await_runtime_ready", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        _register_fast(mgr)
    assert sm.current["tables_desc"] == old["tables_desc"]   # baseline schema restored
    assert sm.current["credential"] == "keep"                # sibling preserved
    assert sm.current.get(_DELIVERY_PENDING_KEY) is True      # next provision will roll
    assert len(rolls) == 2                                    # original + compensating roll
    assert _SYS not in mgr._agents


def test_delivery_pending_marker_forces_a_roll_when_bytes_already_match(monkeypatch):
    # F1 (r3): if a prior attempt's baseline restore itself failed, the secret
    # carries a delivery-pending marker. The next provision must roll even though
    # the generated bytes already match, so the runtime finally re-reads the
    # delivered config.
    from agents.agent_manager import _DELIVERY_PENDING_KEY
    existing = {"table_bucket_arn": "arn:bucket", "tables_desc": _TABLES_DESC,
                "system_prompt": "p", _DELIVERY_PENDING_KEY: True}
    sm = _FakeSM(existing=existing)
    monkeypatch.setattr("boto3.client", lambda *a, **k: sm)
    mgr = _mgr()
    changed = mgr._write_agent_config_on_fast_path(
        _SYS, "s3tables", "p", _TABLES_DESC, S3TABLES_DUCKDB, {"table_bucket_arn": "arn:bucket"})
    assert changed is True                    # not a no-op despite matching bytes
    assert _DELIVERY_PENDING_KEY not in sm.current   # marker cleared by the merge


def test_athena_omitted_table_renders_incomplete(monkeypatch):
    # F2 (r3): completeness is expected-vs-completed. When per-table Athena
    # discovery omits an expected table, the rendered desc names it (names-only)
    # so the fast-path gate refuses it; it never overwrites a complete schema.
    from types import SimpleNamespace

    import agents.onboard as onboard
    from agents.agent_manager import _tables_desc_is_complete

    body = SimpleNamespace(mappings=[
        {"table": "ns.good", "field": "id", "name": "ns.good"},
        {"table": "ns.missing", "field": "id", "name": "ns.missing"},
    ])
    ctx = SimpleNamespace(body=body, system_id="ath-partial", schema="db")

    def partial(_cfg, _tables, complete_tables=None):
        complete_tables.add("ns.good")
        return {"ns.good": [{"name": "id", "data_type": "varchar", "is_partition": False}]}

    monkeypatch.setattr(onboard, "_discover_athena_physical_columns", partial)
    monkeypatch.setattr(onboard, "_athena_hidden_partitioning", lambda *a, **k: {})
    monkeypatch.setattr(onboard, "_collect_provision_samples", lambda *a, **k: {})

    desc = onboard._dp_athena_tables_desc(ctx, {"database": "db", "workgroup": "wg"})
    assert "ns.missing" in desc
    assert _tables_desc_is_complete(desc) is False


# --------------------------------------------------------------------------- #
# Review live-config-refresh r4: marker CAS + compensating roll on READY timeout #
# --------------------------------------------------------------------------- #
def test_delivery_pending_marker_does_not_clobber_a_newer_secret(monkeypatch):
    # F1 (r4): the marker is written through the SAME pending-version CAS. A
    # newer writer moves AWSCURRENT after the marker's read (v-old) but before
    # its promotion, so the CAS (RemoveFromVersionId=v-old) is rejected and the
    # newer writer is left untouched. The pre-fix unconditional AWSCURRENT put
    # clobbered it with the older payload plus marker.
    from agents.agent_manager import _mark_delivery_pending, _DELIVERY_PENDING_KEY
    newer = {"table_bucket_arn": "arn:bucket", "tables_desc": "newer(id: varchar)",
             "credential": "newer"}
    sm = _FakeSM(existing={"table_bucket_arn": "arn:bucket", "tables_desc": "mine(id: varchar)"})
    original_get = sm.get_secret_value

    def get_then_concurrent_move(**kwargs):
        read = original_get(**kwargs)             # returns v-old
        sm.concurrent_write(newer)                # another task wins AWSCURRENT
        return read

    sm.get_secret_value = get_then_concurrent_move
    _mark_delivery_pending(sm, "adt/agent-config/x", _SYS)
    assert sm.current == newer                    # newer writer untouched
    assert _DELIVERY_PENDING_KEY not in sm.current


def test_delivery_pending_marker_cas_wins_when_awscurrent_unchanged(monkeypatch):
    # The complement: when AWSCURRENT still belongs to the read version, the
    # marker promotes normally so the next provision rolls.
    from agents.agent_manager import _mark_delivery_pending, _DELIVERY_PENDING_KEY
    sm = _FakeSM(existing={"table_bucket_arn": "arn:bucket", "tables_desc": "mine(id: varchar)"})
    _mark_delivery_pending(sm, "adt/agent-config/x", _SYS)
    assert sm.current.get(_DELIVERY_PENDING_KEY) is True


def test_ready_timeout_compensates_runtime_after_secret_restore(monkeypatch):
    # F2 (r4): a READY timeout after an accepted roll restores the baseline
    # secret AND rolls again so the final container re-reads the baseline, then
    # verifies READY. The provision still fails retryable (raises, no register).
    mgr = _mgr()
    events = []

    def write(*args, delivery=None, **kwargs):
        delivery.update(secret_name="secret", baseline_version="v-old",
                        promoted_version="v-new", region="us-east-1")
        return True

    monkeypatch.setattr(mgr, "_write_agent_config_on_fast_path", write)
    monkeypatch.setattr(mgr, "_bump_runtime_version",
                        lambda *a, **k: events.append("roll") or "ok")
    monkeypatch.setattr(mgr, "_await_runtime_ready",
                        lambda *a, **k: events.append("ready") or False)
    monkeypatch.setattr(mgr, "_undo_config_delivery",
                        lambda *a, **k: events.append("secret-restore") or "restored")
    # The compensating marker write would reach boto3; stub it out of this unit.
    monkeypatch.setattr("agents.agent_manager._mark_delivery_pending", lambda *a, **k: None)

    with pytest.raises(RuntimeError):
        mgr._refresh_generated_config_on_fast_path(
            _SYS, "s3tables", "prompt", "ns.table(id: varchar)", S3TABLES_DUCKDB, {})
    assert events == ["roll", "ready", "secret-restore", "roll", "ready"]
    assert _SYS not in mgr._agents


def _timeout_then(monkeypatch, mgr, ready_after):
    """Roll accepted; the first READY wait times out, later ones return ``ready_after``.
    Returns the list the roll calls are counted into."""
    rolls = []
    monkeypatch.setattr(mgr, "_bump_runtime_version", lambda *a, **k: rolls.append(1) or "ok")
    answers = iter([False] + [ready_after] * 5)
    monkeypatch.setattr(mgr, "_await_runtime_ready", lambda *a, **k: next(answers))
    return rolls


def test_ready_timeout_restores_baseline_by_rewrite_when_stage_move_is_refused(monkeypatch):
    # The stage move back to the baseline version is refused, but AWSCURRENT is
    # still this attempt's: the baseline payload is re-written through CAS, so the
    # compensating roll reads the baseline, and nothing registers.
    old = {"tables_desc": "old.table(id: varchar)", "credential": "keep"}

    class MoveBackRefused(_FakeSM):
        def update_secret_version_stage(self, **kwargs):
            if kwargs.get("VersionStage") == "AWSCURRENT" and kwargs.get("MoveToVersionId") == "v-old":
                raise RuntimeError("baseline stage move refused")
            return super().update_secret_version_stage(**kwargs)

    secrets = MoveBackRefused(existing=old)
    monkeypatch.setattr("boto3.client", lambda *a, **k: secrets)
    mgr = _mgr()
    rolls = _timeout_then(monkeypatch, mgr, True)
    with pytest.raises(RuntimeError):
        mgr._register_fast_path(_SYS, "S3T", "s3tables", "prompt", "ns.table(id: varchar)",
                                S3TABLES_DUCKDB, {"table_bucket_arn": "arn:bucket"}, "gw-1",
                                "https://gw/mcp", False, refresh_generated_config=True)
    assert secrets.current == old
    assert len(rolls) == 2                       # the accepted roll plus the compensating one
    assert _SYS not in mgr._agents


def test_ready_timeout_never_rolls_when_the_baseline_cannot_be_restored(monkeypatch):
    # Neither the stage move nor the CAS re-write can put the baseline back: the
    # current payload is still the failed attempt's, so no compensating roll may
    # run on it. The provision fails and the delivery-pending marker is left.
    from agents.agent_manager import _DELIVERY_PENDING_KEY
    old = {"tables_desc": "old.table(id: varchar)", "credential": "keep"}

    class RestoreImpossible(_FakeSM):
        def update_secret_version_stage(self, **kwargs):
            if kwargs.get("VersionStage") == "AWSCURRENT" and self._restoring:
                raise RuntimeError("restore refused")
            result = super().update_secret_version_stage(**kwargs)
            if kwargs.get("VersionStage") == "AWSCURRENT":
                self._restoring = True           # the first promotion succeeded
            return result

        def __init__(self, existing=None):
            super().__init__(existing)
            self._restoring = False

    secrets = RestoreImpossible(existing=old)
    monkeypatch.setattr("boto3.client", lambda *a, **k: secrets)
    mgr = _mgr()
    rolls = _timeout_then(monkeypatch, mgr, True)
    with pytest.raises(RuntimeError):
        mgr._register_fast_path(_SYS, "S3T", "s3tables", "prompt", "ns.table(id: varchar)",
                                S3TABLES_DUCKDB, {"table_bucket_arn": "arn:bucket"}, "gw-1",
                                "https://gw/mcp", False, refresh_generated_config=True)
    assert len(rolls) == 1                       # no roll on an unverified payload
    assert secrets.current.get("tables_desc") == "ns.table(id: varchar)"
    assert _SYS not in mgr._agents
    assert _DELIVERY_PENDING_KEY not in old       # the baseline itself was never touched


def test_ready_timeout_compensates_after_a_restore_whose_response_was_lost(monkeypatch):
    # The stage move back to the baseline lands but its response is lost. The
    # re-read shows AWSCURRENT is the baseline again, which is a restore, not a
    # newer writer, so the compensating roll must still run.
    old = {"tables_desc": "old.table(id: varchar)", "credential": "keep"}

    class ResponseLost(_FakeSM):
        def update_secret_version_stage(self, **kwargs):
            result = super().update_secret_version_stage(**kwargs)
            if kwargs.get("VersionStage") == "AWSCURRENT" and kwargs.get("MoveToVersionId") == "v-old":
                raise TimeoutError("stage move landed, response lost")
            return result

    secrets = ResponseLost(existing=old)
    monkeypatch.setattr("boto3.client", lambda *a, **k: secrets)
    mgr = _mgr()
    rolls = _timeout_then(monkeypatch, mgr, True)
    with pytest.raises(RuntimeError):
        mgr._register_fast_path(_SYS, "S3T", "s3tables", "prompt", "ns.table(id: varchar)",
                                S3TABLES_DUCKDB, {"table_bucket_arn": "arn:bucket"}, "gw-1",
                                "https://gw/mcp", False, refresh_generated_config=True)
    assert secrets.current == old
    assert len(rolls) == 2
    assert _SYS not in mgr._agents
