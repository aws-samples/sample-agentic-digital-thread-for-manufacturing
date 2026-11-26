# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the onboarding-agent DDB ordering gates (Task 1.2).

Proves the ``discover -> map_fields -> resolve_identities`` tools enforce their
preconditions against the durable :class:`OnboardingSessionStore` status (not the
old module-global ``_session``):

* an out-of-order call (``map_fields``/``resolve_identities`` before its
  predecessor) returns the preserved user-facing error AND writes nothing AND
  makes no Bedrock/Neptune call;
* the happy path advances the session status
  ``discovering -> discovered -> mapped -> resolved``.

Fully offline: ``boto3``/``bedrock_call``/``neptune_query``/``pg`` and the store
are all replaced with in-memory fakes. Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_onboard_agent_gates.py
  * standalone:  python agentic_digital_thread/tests/unit/test_onboard_agent_gates.py
"""
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from agents.onboarding_session import (  # noqa: E402
    DuplicateProvisionError,
    OutOfOrderError,
    SessionExistsError,
    SessionNotFoundError,
)


class _FakeStore:
    """In-memory faithful stand-in for OnboardingSessionStore (no AWS).

    Mirrors the create->advance->claim->finalize conditional state machine and
    records every mutating call in ``writes`` so a test can assert that an
    out-of-order tool wrote nothing."""

    _LEGAL = {
        "discovering": {"discovered"},
        "discovered": {"mapped"},
        "mapped": {"resolved"},
        "resolved": {"provisioning"},
        "provisioning": {"onboarded", "failed"},
    }

    def __init__(self):
        self.items = {}
        self.writes = []

    def get_session(self, system_id):
        if system_id not in self.items:
            raise SessionNotFoundError(system_id)
        return dict(self.items[system_id])

    def create_session(self, system_id, initiated_by, **fields):
        self.writes.append(("create", system_id))
        if system_id in self.items:
            raise SessionExistsError(system_id)
        item = {"system_id": system_id, "status": "discovering", "initiated_by": initiated_by}
        item.update(fields)
        self.items[system_id] = item
        return dict(item)

    def advance_status(self, system_id, from_status, to_status, **fields):
        self.writes.append(("advance", system_id, from_status, to_status))
        if to_status not in self._LEGAL.get(from_status, set()):
            raise ValueError(f"illegal {from_status}->{to_status}")
        item = self.items.get(system_id)
        if item is None or item.get("status") != from_status:
            raise OutOfOrderError(f"{system_id}: not {from_status}")
        item["status"] = to_status
        item.update(fields)

    def claim_provisioning(self, system_id, **fields):
        self.writes.append(("claim", system_id))
        item = self.items.get(system_id)
        if item is None or item.get("status") != "resolved":
            raise DuplicateProvisionError(system_id)
        item["status"] = "provisioning"
        item.update(fields)

    def delete_session(self, system_id):
        self.writes.append(("delete", system_id))
        self.items.pop(system_id, None)


def _install_fake_store():
    orig = oa._store
    fake = _FakeStore()
    oa._store = lambda: fake
    return fake, (lambda: setattr(oa, "_store", orig))


# --------------------------------------------------------------------------- #
# Out-of-order: error + write nothing + no model/graph calls
# --------------------------------------------------------------------------- #
def test_map_fields_before_discover_errors_and_writes_nothing():
    fake, restore = _install_fake_store()
    called = {"bedrock": 0}
    orig_bedrock = oa.bedrock_call
    oa.bedrock_call = lambda *a, **k: called.__setitem__("bedrock", called["bedrock"] + 1) or "[]"
    try:
        res = oa.map_fields("erp")
        assert res == {"error": "No tables discovered yet. Call discover first."}, res
        assert fake.writes == [], fake.writes          # nothing persisted
        assert called["bedrock"] == 0, "no Bedrock call before the gate"
    finally:
        oa.bedrock_call = orig_bedrock
        restore()


def test_resolve_before_map_fields_errors_and_writes_nothing():
    fake, restore = _install_fake_store()
    called = {"bedrock": 0, "neptune": 0}
    orig_bedrock, orig_neptune = oa.bedrock_call, oa.neptune_query
    oa.bedrock_call = lambda *a, **k: called.__setitem__("bedrock", called["bedrock"] + 1) or "[]"
    oa.neptune_query = lambda *a, **k: called.__setitem__("neptune", called["neptune"] + 1) or []
    try:
        res = oa.resolve_identities("erp")
        assert res == {"error": "No mappings available. Call map_fields first."}, res
        assert fake.writes == [], fake.writes
        assert called["bedrock"] == 0 and called["neptune"] == 0, called
    finally:
        oa.bedrock_call, oa.neptune_query = orig_bedrock, orig_neptune
        restore()


def test_map_fields_on_mapped_session_is_gated():
    """Even with a session present, map_fields refuses unless status=='discovered'."""
    fake, restore = _install_fake_store()
    fake.items["erp"] = {"system_id": "erp", "status": "mapped",
                         "discovered_tables": [{"name": "orders", "columns": []}],
                         "connection_type": "PostgreSQL"}
    called = {"bedrock": 0}
    orig_bedrock = oa.bedrock_call
    oa.bedrock_call = lambda *a, **k: called.__setitem__("bedrock", called["bedrock"] + 1) or "[]"
    try:
        res = oa.map_fields("erp")
        assert res == {"error": "No tables discovered yet. Call discover first."}, res
        assert called["bedrock"] == 0, called
    finally:
        oa.bedrock_call = orig_bedrock
        restore()


# --------------------------------------------------------------------------- #
# Happy path: discovering -> discovered -> mapped -> resolved
# --------------------------------------------------------------------------- #
def test_happy_path_advances_status():
    fake, restore = _install_fake_store()
    orig = {
        "get": oa.catalog.get,
        "onboarded": oa.catalog.get_onboarded_ids,
        "resolve": cr.resolve_connection,
        "discover_pg": oa._discover_postgres,
        "bedrock": oa.bedrock_call,
        "neptune": oa.neptune_query,
    }
    oa.catalog.get = lambda sid: {"id": sid, "name": "ERP",
                                  "connection_type": "PostgreSQL", "schema": "erp_data"}
    oa.catalog.get_onboarded_ids = lambda: set()
    cr.resolve_connection = lambda sid: {"system_id": sid, "connection_type": "PostgreSQL",
                                         "connection_string": "postgresql://x/db", "secret_arn": ""}
    oa._discover_postgres = lambda entry, resolved: {
        "tables": [{"name": "orders", "columns": [{"name": "id", "data_type": "int"}], "row_count": 3}]
    }
    # map_fields: a single clean identifier mapping (high confidence → no proposal pass)
    oa.bedrock_call = lambda *a, **k: (
        '[{"field": "orders.id", "table": "orders", '
        '"mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.9}]}]'
    )
    oa.neptune_query = lambda *a, **k: []  # no other onboarded systems
    try:
        d = oa.discover("erp")
        assert "error" not in d, d
        assert fake.items["erp"]["status"] == "discovered", fake.items

        m = oa.map_fields("erp")
        assert "error" not in m, m
        assert fake.items["erp"]["status"] == "mapped", fake.items
        assert fake.items["erp"]["mappings"], "mappings persisted"

        r = oa.resolve_identities("erp")
        assert "error" not in r, r
        assert fake.items["erp"]["status"] == "resolved", fake.items
    finally:
        oa.catalog.get = orig["get"]
        oa.catalog.get_onboarded_ids = orig["onboarded"]
        cr.resolve_connection = orig["resolve"]
        oa._discover_postgres = orig["discover_pg"]
        oa.bedrock_call = orig["bedrock"]
        oa.neptune_query = orig["neptune"]
        restore()


_TESTS = [
    test_map_fields_before_discover_errors_and_writes_nothing,
    test_resolve_before_map_fields_errors_and_writes_nothing,
    test_map_fields_on_mapped_session_is_gated,
    test_happy_path_advances_status,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} onboard_agent gate tests passed")
