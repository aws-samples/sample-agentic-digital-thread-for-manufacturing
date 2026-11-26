# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the ERP cleanup Lambda (lambda_/lambda_cleanup_old_data.py).

Wave 1 Task C2: the transactional wipe is a single ``TRUNCATE ... CASCADE``
(guarded by ``SET lock_timeout``) instead of ~24 ordered ``DELETE FROM``
statements, and the Lambda pauses the ERP simulation EventBridge schedule
around the wipe (pause only if the rule is ENABLED, resume in ``finally``
even when the TRUNCATE raises; a pause failure is non-fatal).

Everything here is offline: ``pgdriver.connect`` and ``boto3.client`` are
monkeypatched (no sockets, no AWS). ``time.sleep`` is stubbed so the drain
wait is instant.

The IAM change in ``stacks/erp_lambda_stack.py`` is pinned by a text-level
tripwire (same convention as ``test_deploy_sh_demo_db_pointers.py``): the
events actions must stay scoped to the ONE named rule ARN — no wildcards.
"""

import re
import sys
from pathlib import Path

import pytest

# Repo root (for the lambda_ package and stacks/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lambda_ import lambda_cleanup_old_data as mod  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeCursor:
    """Records every executed statement; answers COUNT(*) with 0."""

    def __init__(self, executed, fail_on_truncate=False):
        self._executed = executed
        self._fail_on_truncate = fail_on_truncate
        self.rowcount = 0
        self._last = ""

    def execute(self, query, params=None):
        text = str(query)
        self._executed.append(text)
        self._last = text
        if self._fail_on_truncate and "TRUNCATE" in text:
            raise RuntimeError("simulated lock_timeout")

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConn:
    def __init__(self, executed, fail_on_truncate=False):
        self._executed = executed
        self._fail_on_truncate = fail_on_truncate
        self.autocommit = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self._executed, self._fail_on_truncate)

    def commit(self):
        pass

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeEventsClient:
    """Stub EventBridge client recording rule state transitions."""

    def __init__(self, state="ENABLED", describe_raises=False):
        self.state = state
        self.describe_raises = describe_raises
        self.disable_calls = []
        self.enable_calls = []

    def describe_rule(self, Name):
        if self.describe_raises:
            raise RuntimeError("simulated AccessDenied")
        return {"Name": Name, "State": self.state}

    def disable_rule(self, Name):
        self.disable_calls.append(Name)
        self.state = "DISABLED"

    def enable_rule(self, Name):
        self.enable_calls.append(Name)
        self.state = "ENABLED"


@pytest.fixture()
def harness(monkeypatch):
    """Wire the module's pgdriver/boto3/credential seams to offline fakes."""
    executed = []
    events = FakeEventsClient()
    state = {"fail_on_truncate": False}

    monkeypatch.setattr(
        mod, "get_db_credentials",
        lambda: {"host": "db.example.internal", "port": 5432,
                 "database": "erp_simulation", "user": "u", "password": "p"},
    )
    monkeypatch.setattr(
        mod.pgdriver, "connect",
        lambda **kw: FakeConn(executed, state["fail_on_truncate"]),
    )
    monkeypatch.setattr(
        mod.boto3, "client",
        lambda service, region_name=None: events,
    )
    # _pause_simulator does `import time as _time` then sleeps 15s — stub it.
    import time as _time_mod
    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)

    return executed, events, state


# --------------------------------------------------------------------------- #
# (1) single TRUNCATE ... CASCADE replaces the ordered DELETE list
# --------------------------------------------------------------------------- #

def test_truncate_cascade_replaces_per_table_deletes(harness):
    executed, events, _ = harness
    mod.cleanup_old_data()

    truncates = [q for q in executed if "TRUNCATE TABLE" in q]
    assert len(truncates) == 1, f"expected exactly one TRUNCATE, got {truncates}"
    truncate = truncates[0]
    assert "CASCADE" in truncate

    # Every formerly-DELETEd transactional table is covered by the TRUNCATE...
    for table in (
        "lot_mes_tracking", "lot_production_usage", "lot_warehouse_movements",
        "material_lots", "mes_downtime_events", "mes_material_consumption",
        "mes_quality_checks", "mes_production_events", "mes_work_orders",
        "payments", "invoice_line_items", "invoices", "shipment_items",
        "shipments", "production_materials", "production_runs", "po_receipts",
        "po_line_items", "purchase_orders", "order_line_items",
        "customer_orders", "simulation_events", "inventory_transactions",
        "inventory_balances",
    ):
        assert table in truncate, f"{table} missing from TRUNCATE list"
        # ...and no per-table DELETE remains for it (unqualified wipe).
        assert not any(
            re.match(rf"\s*DELETE FROM {table}\s*$", q) for q in executed  # nosec B608 - test fixture composing expected SQL from module constants
        ), f"ordered DELETE FROM {table} still present"  # nosec B608 - test fixture composing expected SQL from module constants


# --------------------------------------------------------------------------- #
# (2) lock_timeout set before the wipe
# --------------------------------------------------------------------------- #

def test_lock_timeout_set_before_truncate(harness):
    executed, _, _ = harness
    mod.cleanup_old_data()

    lock_idx = next(i for i, q in enumerate(executed) if "SET lock_timeout" in q)
    trunc_idx = next(i for i, q in enumerate(executed) if "TRUNCATE TABLE" in q)
    assert lock_idx < trunc_idx, "lock_timeout must be set before the TRUNCATE"


# --------------------------------------------------------------------------- #
# (3) pause only when ENABLED; resume in finally even on failure
# --------------------------------------------------------------------------- #

def test_rule_disabled_only_when_enabled_and_resumed(harness):
    _, events, _ = harness
    mod.cleanup_old_data()
    assert events.disable_calls == ["erp-simulation-schedule"]
    assert events.enable_calls == ["erp-simulation-schedule"]


def test_rule_left_alone_when_not_enabled(harness):
    _, events, _ = harness
    events.state = "DISABLED"
    mod.cleanup_old_data()
    assert events.disable_calls == []
    # Never re-enable a schedule an operator deliberately left disabled.
    assert events.enable_calls == []


def test_rule_resumed_in_finally_when_truncate_raises(harness):
    _, events, state = harness
    state["fail_on_truncate"] = True
    with pytest.raises(RuntimeError, match="simulated lock_timeout"):
        mod.cleanup_old_data()
    assert events.disable_calls == ["erp-simulation-schedule"]
    assert events.enable_calls == ["erp-simulation-schedule"], (
        "the simulator schedule must be resumed even when the wipe fails"
    )


# --------------------------------------------------------------------------- #
# (4) pause failure is non-fatal — cleanup proceeds
# --------------------------------------------------------------------------- #

def test_pause_failure_proceeds_with_cleanup(harness):
    executed, events, _ = harness
    events.describe_raises = True
    result = mod.cleanup_old_data()
    assert result["deleted"]["transactional_tables"] == "truncated"
    assert any("TRUNCATE TABLE" in q for q in executed)
    assert events.disable_calls == []
    assert events.enable_calls == []


# --------------------------------------------------------------------------- #
# IAM tripwire: events actions stay scoped to the ONE named rule ARN
# --------------------------------------------------------------------------- #

def test_erp_lambda_stack_events_iam_scoped_to_single_rule():
    src = (_REPO_ROOT / "stacks" / "erp_lambda_stack.py").read_text()

    # Locate the cleanup Lambda's events policy statement.
    start = src.find("events:DisableRule")
    assert start != -1, "events:DisableRule grant not found in erp_lambda_stack.py"
    block_start = src.rfind("self.cleanup_lambda.add_to_role_policy", 0, start)
    assert block_start != -1, (
        "the events grant must be attached to the CLEANUP Lambda's role"
    )
    block_end = src.find(")", src.find("resources", start))
    block = src[block_start:block_end + 1]

    for action in ("events:DisableRule", "events:EnableRule", "events:DescribeRule"):
        assert action in block, f"{action} missing from the events policy"

    # Scoped to exactly the one named rule; partition/region/account from the
    # stack context — no literals, no wildcards.
    assert (
        "arn:{self.partition}:events:{self.region}:{self.account}"
        ":rule/erp-simulation-schedule" in block.replace("f\"", "\"")
    ), "events grant must be scoped to the single erp-simulation-schedule rule ARN"
    assert "*" not in block.split("resources")[1], (
        "no wildcard allowed in the events grant resources"
    )


def test_truncate_retries_on_lock_timeout_then_succeeds(monkeypatch):
    """An in-flight simulator cycle can hold locks past the rule pause: the
    TRUNCATE must fail fast, settle, and retry — not give up on attempt 1."""
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "lambda_cleanup_old_data",
        pathlib.Path(__file__).resolve().parents[3] / "lambda_" / "lambda_cleanup_old_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))

    class Cur:
        rowcount = 0
        def __init__(self):
            self.calls = 0
            self.terminates = 0
        def execute(self, sql, *a):
            if "pg_terminate_backend" in sql:
                self.terminates += 1
            if "TRUNCATE" in sql:
                self.calls += 1
                if self.calls == 1:
                    raise mod.pgdriver.OperationalError("canceling statement due to lock timeout")
    class Conn:
        rolled = 0
        committed = 0
        def rollback(self): Conn.rolled += 1
        def commit(self): Conn.committed += 1

    cur, conn, results = Cur(), Conn(), {"deleted": {}}
    mod._truncate_transactional_tables(conn, cur, results, attempts=3, settle_seconds=5)
    assert cur.calls == 2                      # failed once, succeeded on retry
    assert cur.terminates == 2                 # writer sweep before each attempt
    assert Conn.rolled == 1 and Conn.committed == 3  # 2 terminate commits + final
    assert sleeps == [5]                       # settled between attempts
    assert results["deleted"]["transactional_tables"] == "truncated"


def test_truncate_raises_after_exhausted_attempts(monkeypatch):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "lambda_cleanup_old_data",
        pathlib.Path(__file__).resolve().parents[3] / "lambda_" / "lambda_cleanup_old_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    class Cur:
        rowcount = 0
        def execute(self, sql, *a):
            if "TRUNCATE" in sql:
                raise mod.pgdriver.OperationalError("canceling statement due to lock timeout")
    class Conn:
        def rollback(self): pass
        def commit(self): pass

    import pytest
    with pytest.raises(mod.pgdriver.OperationalError):
        mod._truncate_transactional_tables(Conn(), Cur(), {"deleted": {}}, attempts=2, settle_seconds=1)
