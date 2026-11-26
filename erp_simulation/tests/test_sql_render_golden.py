# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""Golden pins of the SQL the ERP simulator builds from code constants.

Each case drives one statement builder (or reads one module SQL constant) on a
recording cursor and pins exactly what reaches ``cursor.execute``: the
statement text (or, for a pgdriver ``sql.Composed``, its structure) and its
parameters. The pins were captured before the statements moved off f-strings
and ``str.format``, so a refactor that changes a single character of what
PostgreSQL receives fails here. Regenerate (only for an intended SQL change)
with ``SQL_GOLDEN_REGEN=1``.
"""
from __future__ import annotations

import importlib.util
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from erp_simulation import (console_login, finance_simulator, mes_simulator, production_simulator,
                            schema_manager, story_history, story_replan, utils)

_FIXTURE = Path(__file__).parent / "fixtures" / "sql_render_golden.json"
_REPO = Path(__file__).resolve().parents[2]

_ANCHOR = 1_790_000_000.0  # a fixed story anchor (2026-09-21T13:33:20Z)
_NOW = _ANCHOR + 5 * 86_400.0


def _render(query) -> str:
    if isinstance(query, str):
        return "TEXT:" + str(query)
    return "COMPOSED:" + repr(query)


class _Recorder:
    """A cursor that records every statement; ``rows`` feeds fetchall/fetchone."""

    def __init__(self, rows=None, one=None):
        self.calls = []
        self.rowcount = 0
        self._rows = rows if rows is not None else []
        self._one = one
        self.connection = None

    def execute(self, query, params=None):
        self.calls.append([_render(query), repr(params)])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._one

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *args, **kwargs):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ----------------------------------------------------------------- cases ---

def _utils_savepoint_ok():
    cur = _Recorder()
    with utils.savepoint(cur):
        pass
    return cur.calls


def _utils_savepoint_error():
    cur = _Recorder()
    with pytest.raises(RuntimeError):
        with utils.savepoint(cur, "adt_number_sequence"):
            raise RuntimeError("boom")
    return cur.calls


def _utils_ensure_sequences():
    utils._reset_sequence_cache()
    cur = _Recorder()
    utils.ensure_number_sequences(cur)
    utils._reset_sequence_cache()
    return cur.calls


def _finance_since_last_cycle():
    cur = _Recorder(one=(0,))
    finance_simulator._since_last_cycle(cur, "total_amount", "invoices", "invoice_date")
    finance_simulator._since_last_cycle(cur, "amount", "payments", "payment_date")
    return cur.calls


def _console_upsert_role_new():
    cur = _Recorder(one=None)
    console_login._upsert_role(cur, console_login.CONSOLE_ROLE, "SCRAM-SHA-256$4096:verifier")
    return cur.calls


def _console_upsert_role_existing():
    cur = _Recorder(one=(1,))
    console_login._upsert_role(cur, console_login.CONSOLE_ROLE, "SCRAM-SHA-256$4096:verifier")
    return cur.calls


def _console_grant_select():
    cur = _Recorder(rows=[("public",), ("cmms_indianapolis",)])
    console_login._grant_select(cur, console_login.CONSOLE_ROLE)
    return cur.calls


def _schema_manager_drop_all():
    cur = _Recorder()
    manager = object.__new__(schema_manager.SchemaManager)
    manager.connection = None
    assert manager.drop_all_tables(connection=_Conn(cur), confirm=True)
    return cur.calls


def _simulate_local_fast_forward(monkeypatch):
    spec = importlib.util.spec_from_file_location("_simulate_local_golden", _REPO / "scripts" / "simulate_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cur = _Recorder(rows=[("customer_orders", "order_date"), ("shipments", "ship_date")])
    monkeypatch.setattr(module, "_connect", lambda: _Conn(cur))
    module._fast_forward("6 hours")
    return cur.calls


def _production_replenishment():
    cur = _Recorder()
    sim = object.__new__(production_simulator.ProductionSimulator)
    sim._replenishment_candidates(cur, 1000)
    return cur.calls


def _production_receipt_backlog():
    cur = _Recorder()
    sim = object.__new__(production_simulator.ProductionSimulator)
    sim.complete_production(_Conn(cur), "cycle-golden")
    return cur.calls[:1]


def _replan_purge():
    cur = _Recorder()
    story_replan._purge_condition_orders_before(cur, _ANCHOR, _NOW)
    return cur.calls


def _history_require_reference():
    sp = story_history.plan(_ANCHOR)

    class _AllPresent(_Recorder):
        def execute(self, query, params=None):
            super().execute(query, params)
            self._rows = [(key,) for key in params[0]]

    cur = _AllPresent()
    story_history._require_reference(cur, sp)
    return cur.calls


_HISTORY_CONSTANTS = (
    "_UPSERT_CMMS_WORK_ORDER_SQL", "_DELETE_STALE_LOG_SQL", "_INSERT_LOG_SQL",
    "_PURGE_CONDITION_SCOPE", "_PURGE_CONDITION_LOG_SQL", "_PURGE_CONDITION_WO_SQL",
    "_PAST_STOP_PROOF", "_OWNED_PAST_STOP_SCOPE", "_DELETE_PAST_STOP_LOG_SQL", "_DELETE_PAST_STOP_WO_SQL",
    "_FOREIGN_PAST_STOP_ORDERS_SQL", "_ALIGN_PM_SCHEDULE_SQL", "_FROZEN_ERA_SCOPE",
    "_SUPERSEDE_FROZEN_ORDERS_SQL", "_MARK_SUPERSEDED_SQL", "_MARK_PRIOR_ERA_SQL",
    "_PLANT_PM_OPEN_SCOPE", "_SUPERSEDE_PLANT_PM_ORDERS_SQL", "_MARK_PLANT_PM_SUPERSEDED_SQL",
    "_RESTORE_SUPERSEDED_SQL", "_RESTORE_PRIOR_ERA_SQL", "_HISTORY_SCOPE",
    "_DELETE_HISTORY_LOG_SQL", "_DELETE_HISTORY_WO_SQL", "_DELETE_PLANT_PM_LOG_SQL", "_DELETE_PLANT_PM_WO_SQL",
)
_REPLAN_CONSTANTS = ("_STALE_CONDITION_SCOPE", "_PURGE_STALE_CONDITION_LOG_SQL", "_PURGE_STALE_CONDITION_WO_SQL")
_MES_CONSTANTS = ("_CORP_BRIDGE_OPEN", "_CORP_BRIDGE_CANDIDATES_FROM_SQL", "_CORP_BRIDGE_CANDIDATES_BEFORE_SQL")


def _constants(module, names):
    return [[name, _render(getattr(module, name))] for name in names]


_SITE = mes_simulator.MESSimulator.CONDITION_MONITOR_SITE


def _mes_bridge_asset():
    cur = _Recorder(one=("1001-CNC-200",))
    mes_simulator.MESSimulator.indianapolis_bridge_asset(cur, _SITE, "Vibration alarm")
    return cur.calls


def _mes_insert_site_work_order():
    cur = _Recorder(one=("1001-CNC-200",))
    sim = object.__new__(mes_simulator.MESSimulator)
    sim._insert_indianapolis_cmms_work_order(
        cur, _SITE, "IND-LINE-1", "MECHANICAL", "Vibration alarm",
        "0f0e0d0c-0b0a-4908-8706-050403020100", None)
    return cur.calls


def _mes_insert_condition_order():
    cur = _Recorder()
    mes_simulator.MESSimulator.insert_condition_work_order(cur, 7, _ANCHOR, _ANCHOR + 3600.0)
    return cur.calls


def _mes_complete_condition_orders():
    cur = _Recorder()
    mes_simulator.MESSimulator.complete_condition_work_orders(cur, _ANCHOR, _NOW)
    return cur.calls


def _mes_bridge_candidates():
    cur = _Recorder()
    schema = mes_simulator.MESSimulator.CMMS_SITE_TARGET[_SITE][0]
    scope = {"prefix": "IND-%", "open_statuses": ["WAPPR"], "loop_start": "2026-09-21 13:33:20"}
    mes_simulator.MESSimulator._bridge_candidates(cur, schema, scope, "IND-00000000")
    return cur.calls


def _mes_complete_bridge_orders(monkeypatch):
    cls = mes_simulator.MESSimulator
    monkeypatch.setattr(cls, "_bridge_candidates", staticmethod(lambda *a, **k: [("row",)]))
    monkeypatch.setattr(cls, "_bridge_completions", classmethod(lambda c, rows, read, now: (
        [("IND-0F0E0D0C", "2026-09-21 10:00:00", "2026-09-21 11:00:00", "tech", "desc")], [])))
    cur = _Recorder()
    cls.complete_bridge_work_orders(cur, _ANCHOR, _NOW, lambda ids: {})
    return cur.calls


def _mes_corp_bridge_candidates():
    cur = _Recorder()
    mes_simulator.MESSimulator._corp_bridge_candidates(cur, "WO-MES-IND-00000000")
    return cur.calls


CASES = {
    "utils.savepoint ok": _utils_savepoint_ok,
    "utils.savepoint error": _utils_savepoint_error,
    "utils.ensure_number_sequences": _utils_ensure_sequences,
    "finance._since_last_cycle": _finance_since_last_cycle,
    "console_login._upsert_role new": _console_upsert_role_new,
    "console_login._upsert_role existing": _console_upsert_role_existing,
    "console_login._grant_select": _console_grant_select,
    "schema_manager.drop_all_tables": _schema_manager_drop_all,
    "simulate_local._fast_forward": _simulate_local_fast_forward,
    "production._replenishment_candidates": _production_replenishment,
    "production.complete_production backlog": _production_receipt_backlog,
    "story_replan._purge_condition_orders_before": _replan_purge,
    "story_history._require_reference": _history_require_reference,
    "story_history constants": lambda: _constants(story_history, _HISTORY_CONSTANTS),
    "story_replan constants": lambda: _constants(story_replan, _REPLAN_CONSTANTS),
    "mes constants": lambda: _constants(mes_simulator, _MES_CONSTANTS),
    "mes.indianapolis_bridge_asset": _mes_bridge_asset,
    "mes._insert_indianapolis_cmms_work_order": _mes_insert_site_work_order,
    "mes.insert_condition_work_order": _mes_insert_condition_order,
    "mes.complete_condition_work_orders": _mes_complete_condition_orders,
    "mes._bridge_candidates": _mes_bridge_candidates,
    "mes.complete_bridge_work_orders": _mes_complete_bridge_orders,
    "mes._corp_bridge_candidates": _mes_corp_bridge_candidates,
}


def _run(case, monkeypatch):
    fn = CASES[case]
    if fn.__code__.co_argcount:
        return fn(monkeypatch)
    return fn()


@contextmanager
def _golden():
    data = json.loads(_FIXTURE.read_text()) if _FIXTURE.exists() else {}
    yield data
    if os.environ.get("SQL_GOLDEN_REGEN") == "1":
        _FIXTURE.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


@pytest.mark.parametrize("case", sorted(CASES))
def test_sql_matches_golden(case, monkeypatch):
    got = _run(case, monkeypatch)
    assert got, f"{case}: no statement recorded"
    with _golden() as golden:
        if os.environ.get("SQL_GOLDEN_REGEN") == "1":
            golden[case] = got
            return
        assert case in golden, f"{case}: no golden pin (regenerate with SQL_GOLDEN_REGEN=1)"
        assert got == golden[case]


def test_every_golden_pin_has_a_case():
    assert set(json.loads(_FIXTURE.read_text())) == set(CASES)


# ------------------------------------------------- the builders themselves ---

@pytest.mark.parametrize("name", ["adt_row", "cmms_indianapolis", "s.site_code", "_x9"])
def test_sql_text_takes_a_plain_identifier_unquoted(name):
    assert utils.sql_text("SELECT {n} FROM t", n=name) == f"SELECT {name} FROM t"


@pytest.mark.parametrize("name", [
    "Products", "a.b.c", "x; DROP TABLE t", "x--", "x y", "9x", "", "a.", "x\n", "%s", "x'", 'x"',
    utils.SqlText("a = 1") + "",  # str operations drop the marker: the result is plain text again
    7, None, b"x",
])
def test_sql_text_refuses_anything_but_a_plain_identifier(name):
    with pytest.raises(ValueError):
        utils.sql_text("SELECT {n} FROM t", n=name)


def test_sql_text_splices_a_fragment_verbatim_and_returns_one():
    fragment = utils.SqlText("a = %(a)s AND b LIKE 'X-%%'")
    out = utils.sql_text("SELECT 1 FROM {t} WHERE {f}", t="s.t", f=fragment)
    assert out == "SELECT 1 FROM s.t WHERE a = %(a)s AND b LIKE 'X-%%'"
    assert isinstance(out, utils.SqlText)
    assert utils.sql_text("x IN ({q})", q=out) == "x IN (SELECT 1 FROM s.t WHERE a = %(a)s AND b LIKE 'X-%%')"


def test_compose_sql_quotes_names_and_passes_composables():
    from erp_simulation.pgdriver import sql

    composed = utils.compose_sql("GRANT {p} ON {t} TO {r}", p=sql.SQL("SELECT"), t="Odd Name", r=sql.Literal(1))
    assert composed == sql.Composed([sql.SQL("GRANT "), sql.SQL("SELECT"), sql.SQL(" ON "),
                                     sql.Identifier("Odd Name"), sql.SQL(" TO "), sql.Literal(1)])


def test_ensure_sequence_refuses_a_bad_name_before_running_anything():
    utils._reset_sequence_cache()
    cur = _Recorder()
    with pytest.raises(ValueError):
        utils._ensure_sequence(cur, "seq; DROP TABLE t")
    assert cur.calls == []


def test_fg_case_refuses_a_code_that_could_end_its_literal(monkeypatch):
    monkeypatch.setitem(production_simulator.SITE_FG_LOCATION, "EVIL", "X' OR '1'='1")
    with pytest.raises(ValueError):
        production_simulator.fg_location_case_sql("s.site_code")
