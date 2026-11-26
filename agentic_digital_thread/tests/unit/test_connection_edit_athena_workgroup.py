# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An admin Edit can move an Athena system to another workgroup.

The workgroup decides the per-query scan limit and the results location, so an
admin needs to change it without removing the system (removal clears the
system's graph). The Edit validates first: the probe describes the NEW
workgroup through the system's role before anything is written, and the rest of
the stored connection (database, catalog, region, role) is kept. Other
connection types do not take a workgroup. All IO is mocked.
"""
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.admin_routes as admin_routes  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
from agents.admin_routes import update_connection as update_connection_route  # noqa: E402
from agents.connection_types import ATHENA  # noqa: E402

SID = "lake-x"
OWN_ARN = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{SID}-AbCdEf"
ROLE = "arn:aws:iam::444455556666:role/ReadOnlyData"
PASS = {"status": "pass", "latency_ms": 3, "connection_type": ATHENA,
        "reason": "ok", "detail": "workgroup wg-new ENABLED"}
FAIL = {"status": "fail", "latency_ms": 3, "connection_type": ATHENA,
        "reason": "access_denied", "detail": "not authorized for this workgroup"}

_STORED = {"system_id": SID, "connection_type": ATHENA, "workgroup": "wg-old",
           "database": "lake_db", "catalog": "AwsDataCatalog", "region": "us-east-1",
           "output_location": "s3://amzn-s3-demo-results-bucket/results/", "role_arn": ROLE,
           "secret_arn": OWN_ARN, "_secret_unresolved": False}
_CATALOG = {"name": "Lake", "connection_type": ATHENA, "secret_arn": OWN_ARN,
            "connection_config": {"workgroup": "wg-old", "database": "lake_db",
                                  "region": "us-east-1", "role_arn": ROLE}}


def _request():
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        is_admin=True, user=types.SimpleNamespace(email="admin@example.com"))
    return req


class _Catalog:
    def __init__(self, entry):
        self.entry = entry
        self.synced = []

    def get(self, system_id):
        return self.entry if system_id == SID else None

    def set_connection_config(self, system_id, cfg, secret_arn=None):
        self.synced.append(dict(cfg))


def _run(body, *, resolved=_STORED, catalog_entry=_CATALOG, probe=PASS):
    """Run one Edit; return (response, write_calls, probe_candidates, catalog)."""
    writes, probes = [], []
    catalog = _Catalog(catalog_entry)

    def _write(system_id, *, non_secret, secret=None, secret_arn=None, entry=None):
        writes.append({"non_secret": dict(non_secret), "secret_arn": secret_arn})
        return OWN_ARN

    def _probe(system_id, resolved=None):
        probes.append(dict(resolved or {}))
        return probe

    with patch("agents.catalog.catalog", catalog), \
            patch.object(cr, "resolve_connection", lambda sid: dict(resolved)), \
            patch.object(cr, "probe_connection", _probe), \
            patch.object(cr, "write_connection", _write), \
            patch.object(cr, "read_secret_uncached", lambda arn: {"external_id": "x"}), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None), \
            patch.object(admin_routes, "_reconnect_system", lambda *a, **k: True):
        res = asyncio.run(update_connection_route(SID, _request(), body))
    return res, writes, probes, catalog


def test_edit_moves_an_athena_system_to_the_new_workgroup():
    res, writes, probes, catalog = _run({"connection_type": ATHENA, "workgroup": "wg-new"})
    assert isinstance(res, dict) and res["status"] == "ok", res
    # The probe described the NEW workgroup, with the stored role and region.
    assert probes[0]["workgroup"] == "wg-new", probes[0]
    assert probes[0]["role_arn"] == ROLE and probes[0]["region"] == "us-east-1"
    written = writes[0]["non_secret"]
    assert written["workgroup"] == "wg-new", written
    for key in ("database", "catalog", "region", "role_arn"):
        assert written[key] == _STORED[key], (key, written)
    assert writes[0]["secret_arn"] == OWN_ARN      # the credential pointer is kept
    assert catalog.synced and catalog.synced[0]["workgroup"] == "wg-new", catalog.synced


def test_a_workgroup_the_probe_rejects_changes_nothing():
    res, writes, probes, catalog = _run({"connection_type": ATHENA, "workgroup": "wg-new"},
                                        probe=FAIL)
    assert getattr(res, "status_code", None) == 400, res
    assert probes and probes[0]["workgroup"] == "wg-new"
    assert writes == [] and catalog.synced == []


def test_an_invalid_workgroup_name_is_refused_before_the_probe():
    for bad in ("wg new", "wg/../x", "w" * 129, 7, ["wg-new"]):
        res, writes, probes, _ = _run({"connection_type": ATHENA, "workgroup": bad})
        assert getattr(res, "status_code", None) == 400, (bad, res)
        assert b"workgroup" in res.body, (bad, res.body)
        assert probes == [] and writes == [], bad


def test_a_blank_workgroup_keeps_the_current_one():
    """The Edit form sends a blank field as "keep current"."""
    res, writes, probes, _ = _run({"connection_type": ATHENA, "workgroup": ""})
    assert isinstance(res, dict) and res["status"] == "ok", res
    assert probes[0]["workgroup"] == "wg-old" and writes[0]["non_secret"]["workgroup"] == "wg-old"


def test_moving_workgroup_drops_the_old_results_location():
    """The stored output_location belonged to the old workgroup; Athena would
    keep sending it, so a move lets the new workgroup's own location apply."""
    res, writes, _, catalog = _run({"connection_type": ATHENA, "workgroup": "wg-new"})
    assert isinstance(res, dict) and res["status"] == "ok", res
    assert "output_location" not in writes[0]["non_secret"], writes[0]
    assert "output_location" not in catalog.synced[0], catalog.synced


def test_an_edit_that_keeps_the_workgroup_keeps_the_results_location():
    for body in ({"connection_type": ATHENA, "workgroup": ""},
                 {"connection_type": ATHENA, "workgroup": "wg-old"}):
        res, writes, _, _ = _run(body)
        assert isinstance(res, dict) and res["status"] == "ok", (body, res)
        assert writes[0]["non_secret"]["output_location"] == _STORED["output_location"], body
