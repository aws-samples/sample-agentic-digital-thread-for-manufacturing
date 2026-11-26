# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A schema change reserves the new schema; the old one stays live until it lands.

A user PostgreSQL credentials Edit that names another schema moved the
catalog entry to the new schema BEFORE the validate-first probe, so
records-first routing (by schema) changed while the probe ran, and a failed
probe "rolled back" by claiming the old schema again: when another
registration had taken the old schema meanwhile, the rollback failed and the
system was left on a schema whose connection was never written. Now the Edit
RESERVES the new schema (no other registration or Edit can take it) while
the entry keeps routing on the old one, commits the move only after the
probe and the write succeed, and an abort releases only the reservation.
Offline.
"""
from __future__ import annotations

import json
import os
import threading
import types
from unittest.mock import patch

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.admin_routes as admin_routes  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401
    NEW_PW, deployed)
from tests.unit.test_connect_pg_edit_schema_change import NEW_SCHEMA_DSN, _body  # noqa: E402

OTHER_DSN = "postgresql://lab_reader:pw-2@db2.example.internal:5432/lims?options=-csearch_path%3D"


def _edit(probe):
    request = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))
    with patch.object(cr, "probe_connection", side_effect=probe), \
            patch.object(admin_routes, "_reconnect_system", return_value=True):
        return admin_routes._do_update_connection(SID, request,
                                                  {"connection_string": NEW_SCHEMA_DSN})


def _register_other(schema: str) -> dict:
    return _register({"name": "Quality Lab 2", "system_type": "Custom",
                      "connection_type": "PostgreSQL",
                      "connection_config": {"dsn": OTHER_DSN + schema}})


def test_routing_stays_on_the_old_schema_while_the_probe_runs(cat, secrets, graph, deployed):
    _register_pg()
    seen = {}

    def _probe(*_a, **_k):
        seen["entry_schema"] = cat.get(SID)["schema"]
        seen["old_routes"] = db._records_conn_params_for_schema("lims") is not None
        seen["new_routes"] = db._records_conn_params_for_schema("lims_v2") is not None
        return {"status": "pass"}
    assert _body(_edit(_probe)).get("status") == "ok"
    assert seen == {"entry_schema": "lims", "old_routes": True, "new_routes": False}, seen
    assert cat.get(SID)["schema"] == "lims_v2"
    assert db._records_conn_params_for_schema("lims_v2") is not None


def test_a_blocked_probe_holds_the_new_schema_against_a_competing_registration(
        cat, secrets, graph, deployed):
    """While the probe is in flight (blocked), a registration of the NEW schema
    is refused; after the Edit lands the old schema is free to register."""
    _register_pg()
    entered, release = threading.Event(), threading.Event()

    def _probe(*_a, **_k):
        entered.set()
        release.wait(10)
        return {"status": "pass"}
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("res", _edit(_probe)))
    t.start()
    assert entered.wait(10)
    competing = _register_other("lims_v2")
    release.set()
    t.join(10)
    assert "error" in competing and "lims_v2" in competing["error"], competing
    assert _body(out["res"]).get("status") == "ok", _body(out["res"])
    assert cat.get(SID)["schema"] == "lims_v2"
    assert _register_other("lims").get("status") == "created"


def test_a_failed_probe_with_a_competing_claim_keeps_the_old_schema(
        cat, secrets, graph, deployed):
    """A competing claim during a failed probe: while the probe runs,
    another registration tries the OLD schema (refused: it stays this
    system's), then the probe fails. The system is still on its old schema
    and its connection is untouched."""
    _register_pg()
    before = dict(secrets[CONN_ARN])
    competing = {}

    def _probe(*_a, **_k):
        competing["res"] = _register_other("lims")
        return {"status": "fail", "reason": "auth_failed"}
    res = _edit(_probe)
    assert getattr(res, "status_code", None) == 400, _body(res)
    assert "error" in competing["res"], competing
    assert cat.get(SID)["schema"] == "lims"
    assert "pending_schema" not in cat.get(SID)
    assert secrets[CONN_ARN] == before and SID not in graph
    assert db._records_conn_params_for_schema("lims") is not None
    # The reservation is gone: the new schema is free again.
    assert _register_other("lims_v2").get("status") == "created"


def test_two_edits_cannot_reserve_one_schema(cat, secrets, graph, deployed):
    _register_pg()
    other = _register_other("lims_x")
    assert other.get("status") == "created", other
    assert cat.reserve_schema(SID, "lims_v9")
    assert not cat.reserve_schema("quality-lab-2", "lims_v9")
    assert cat.release_schema_reservation(SID, "lims_v9")
    assert cat.reserve_schema("quality-lab-2", "lims_v9")


def test_guard_a_failed_write_releases_only_the_reservation(cat, secrets, graph, deployed):
    _register_pg()
    with patch.object(cr, "write_connection", side_effect=RuntimeError("boom")):
        res = _edit(lambda *_a, **_k: {"status": "pass"})
    assert getattr(res, "status_code", None) == 500
    entry = cat.get(SID)
    assert entry["schema"] == "lims" and "pending_schema" not in entry
    assert json.dumps(entry).count("lims_v2") == 0


def test_the_move_commits_as_soon_as_the_write_lands_not_after_the_reconnect(
        cat, secrets, graph, deployed):
    """The write lands the new DSN; while the live reconnect runs (blocked
    here), the old schema must no longer route to this system and the new one
    must: the catalog moves right after write_connection, before the
    reconnect and the audit."""
    _register_pg()
    entered, release = threading.Event(), threading.Event()
    seen = {}

    def _reconnect(*_a, **_k):
        seen["entry_schema"] = cat.get(SID)["schema"]
        seen["old_routes"] = db._records_conn_params_for_schema("lims") is not None
        seen["new_routes"] = db._records_conn_params_for_schema("lims_v2") is not None
        seen["pending"] = cat.get(SID).get("pending_schema")
        entered.set()
        release.wait(10)
        return True
    request = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))
    out = {}

    def _edit_now():
        with patch.object(cr, "probe_connection", return_value={"status": "pass"}), \
                patch.object(admin_routes, "_reconnect_system", side_effect=_reconnect):
            out["res"] = admin_routes._do_update_connection(
                SID, request, {"connection_string": NEW_SCHEMA_DSN})
    t = threading.Thread(target=_edit_now)
    t.start()
    assert entered.wait(10), "the reconnect never ran"
    competing = _register_other("lims")            # the old schema is free now
    release.set()
    t.join(10)
    assert seen == {"entry_schema": "lims_v2", "old_routes": False, "new_routes": True,
                    "pending": None}, seen
    assert competing.get("status") == "created", competing
    assert _body(out["res"]).get("status") == "ok", _body(out["res"])
