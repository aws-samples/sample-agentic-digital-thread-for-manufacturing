# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Collision-free business numbers and savepoint-isolated inserts (ADR-0090).

Unit tests run on a fake cursor; the ``pg_*`` tests need ERP_SIM_TEST_PG_DSN
(a throwaway PostgreSQL) and prove the SAVEPOINT behaviour on a REAL unique
violation.
"""

import os
import random
import re
import uuid
from datetime import datetime

import pytest

from erp_simulation import utils
from erp_simulation.tests._pg_sql import stmt

_DAY = datetime(2026, 9, 30, 12, 0, 0)


class _UniqueViolation(Exception):
    pgcode = utils.UNIQUE_VIOLATION


class FakeCursor:
    """Records SQL; nextval counts per sequence from ``start``; an INSERT whose
    number is in ``taken`` raises a unique violation."""

    def __init__(self, start=1, taken=(), fail_insert=None):
        self.sql = []
        self.values = {}
        self.start = start
        self.taken = set(taken)
        self.fail_insert = fail_insert
        self._row = None

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("SELECT nextval"):
            seq = params[0]
            self.values[seq] = self.values.get(seq, self.start - 1) + 1
            self._row = (self.values[seq],)
        elif sql.startswith("INSERT"):
            if self.fail_insert is not None:
                raise self.fail_insert
            if params[0] in self.taken:
                raise _UniqueViolation(f"duplicate {params[0]}")
            self.taken.add(params[0])
            self._row = (len(self.taken),)

    def fetchone(self):
        return self._row


@pytest.fixture(autouse=True)
def _fresh_cache():
    utils._reset_sequence_cache()
    yield
    utils._reset_sequence_cache()


# ---------------------------------------------------------------- format ---

@pytest.mark.parametrize("fn, prefix, digits, suffix", [
    (utils.generate_order_number, "ORD", 8, "00000007"),
    (utils.generate_shipment_number, "SHIP", 8, "40000007"),
    (utils.generate_po_number, "PO", 5, "00007"),
    (utils.generate_invoice_number, "INV", 5, "00007"),
    (utils.generate_payment_number, "PAY", 5, "00007"),
    (utils.generate_run_number, "RUN", 5, "00007"),
    (utils.generate_work_order_number, "WO", 5, "40007"),
])
def test_numbers_keep_the_existing_format(fn, prefix, digits, suffix):
    cur = FakeCursor(start=7)
    number = fn(cur, _DAY)
    assert re.fullmatch(rf"{prefix}-20260930-\d{{{digits}}}", number)
    assert number == f"{prefix}-20260930-{suffix}"


def test_consecutive_numbers_are_distinct_and_sequence_backed():
    cur = FakeCursor()
    numbers = [utils.generate_invoice_number(cur, _DAY) for _ in range(5000)]
    assert len(set(numbers)) == 5000
    assert numbers[:2] == ["INV-20260930-00001", "INV-20260930-00002"]
    assert any("nextval" in s for s in cur.sql)


def test_the_sequence_is_created_once_per_prefix_and_keyed_by_prefix():
    cur = FakeCursor()
    utils.generate_invoice_number(cur, _DAY)
    utils.generate_invoice_number(cur, _DAY)
    utils.generate_po_number(cur, _DAY)
    creates = [s for s in cur.sql if s.startswith("CREATE SEQUENCE")]
    assert creates == ["CREATE SEQUENCE IF NOT EXISTS seq_invoice_number",
                       "CREATE SEQUENCE IF NOT EXISTS seq_po_number"]
    assert set(cur.values) == {"seq_invoice_number", "seq_po_number"}


def test_ensure_creates_every_sequence():
    cur = FakeCursor()
    utils.ensure_number_sequences(cur)
    created = {s.rsplit(" ", 1)[1] for s in cur.sql if s.startswith("CREATE SEQUENCE")}
    assert created == {seq for seq, _d, _c in utils.BUSINESS_NUMBER_SPACE.values()}


def test_a_failed_create_is_rolled_back_to_its_savepoint():
    class RacingCursor(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.startswith("CREATE SEQUENCE"):
                raise _UniqueViolation("pg_type_typname_nsp_index")

    cur = RacingCursor()
    assert utils.generate_invoice_number(cur, _DAY) == "INV-20260930-00001"
    assert "ROLLBACK TO SAVEPOINT adt_number_sequence" in cur.sql


# ------------------------------------------------------------ range / cap ---

@pytest.mark.parametrize("fn, low, high", [(utils.generate_run_number, 0, 40_000),
                                           (utils.generate_work_order_number, 40_000, 80_000)])
def test_run_and_work_order_numbers_never_enter_the_story_range(fn, low, high):
    """story_history owns 9xxxx and story_live 8xxxx; runs take 0-39999 and
    work orders 40000-79999, each wrapping after 40,000."""
    for start in (39_998, 40_000, 79_999, 80_000, 99_999, 159_999, 10**12 + 79_999):
        cur = FakeCursor(start=start)
        for _ in range(3):
            suffix = int(fn(cur, _DAY).rsplit("-", 1)[1])
            assert low <= suffix < high


def test_order_and_shipment_numbers_stay_below_the_story_range():
    """story_history orders/shipments use 9xxxxxxx suffixes; orders take
    0..39,999,999 and shipments 40,000,000..79,999,999."""
    cur = FakeCursor(start=80_000_000 + 5)
    assert utils.generate_order_number(cur, _DAY) == "ORD-20260930-00000005"
    assert utils.generate_shipment_number(cur, _DAY) == "SHIP-20260930-40000005"
    cur = FakeCursor(start=40_000_000 - 1)
    assert utils.generate_order_number(cur, _DAY) == "ORD-20260930-39999999"
    assert utils.generate_shipment_number(cur, _DAY) == "SHIP-20260930-79999999"
    assert utils.generate_order_number(cur, _DAY) == "ORD-20260930-00000000"
    assert utils.generate_shipment_number(cur, _DAY) == "SHIP-20260930-40000000"


class _ValueCursor:
    """``nextval`` of every sequence returns ``value``, whatever the test sets."""

    value = 0

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (self.value,)


def _suffixes(prefix, values):
    cur = _ValueCursor()
    out = set()
    for value in values:
        cur.value = value
        out.add(int(utils.next_business_number(cur, prefix, _DAY).rsplit("-", 1)[1]))
    return out


# 100,000 consecutive sequence values cover every residue of any modulus up to
# 100,000, so for a 5-digit prefix this window reaches every suffix the
# sequence can ever produce, here at a large sequence value.
_EVERY_5_DIGIT_RESIDUE = range(10**12 - 50_000, 10**12 + 50_000)
# An 8-digit space is too large to walk: every multiple of 10,000,000 up to
# 200,000,000 (each wrap point of a 40M or 80M modulus) with 50 values either
# side, plus a seeded sample of arbitrary values.
_RNG = random.Random(1106)
_EIGHT_DIGIT_VALUES = (
    [b + d for b in range(0, 200_000_001, 10_000_000) for d in range(-50, 50) if b + d >= 0]
    + [b + d for b in (10**12, 10**15) for d in range(-50, 50)]
    + [_RNG.randrange(1, 10**15) for _ in range(20_000)])


@pytest.mark.parametrize("first, second, values, story_floor", [
    ("RUN", "WO", _EVERY_5_DIGIT_RESIDUE, 80_000),
    ("ORD", "SHIP", _EIGHT_DIGIT_VALUES, 80_000_000),
])
def test_paired_prefixes_never_share_digits_and_stay_out_of_the_story_range(
        first, second, values, story_floor):
    """A run and a work order (an order and a shipment) share digits only when
    they are the story's deliberate pair (story_history / story_live, at or
    above ``story_floor``). The simulator's two sequences run independently,
    so whatever values they hold, the suffix sets must not intersect: on a
    live seat RUN-20261106-00256 matched an unrelated Detroit
    WO-20261106-00256 by digits and took Detroit's site."""
    a, b = _suffixes(first, values), _suffixes(second, values)
    assert not a & b, f"{first} and {second} share {len(a & b)} suffixes, e.g. {sorted(a & b)[:3]}"
    assert max(a | b) < story_floor


def test_the_same_sequence_value_gives_a_run_and_a_work_order_different_digits():
    """The live-seat case: both sequences at 256 on the same day."""
    cur = FakeCursor(start=256)
    run, wo = utils.generate_run_number(cur, _DAY), utils.generate_work_order_number(cur, _DAY)
    assert run.rsplit("-", 1)[1] != wo.rsplit("-", 1)[1]


def test_invoice_payment_and_po_numbers_wrap_at_the_daily_cap():
    cur = FakeCursor(start=100_000)
    assert utils.generate_invoice_number(cur, _DAY) == "INV-20260930-00000"
    assert utils.generate_payment_number(cur, _DAY) == "PAY-20260930-00000"
    assert utils.generate_po_number(cur, _DAY) == "PO-20260930-00000"


def test_the_documented_ranges_and_daily_caps():
    spans = {p: span for p, (_s, _d, span) in utils.BUSINESS_NUMBER_SPACE.items()}
    assert spans == {"ORD": range(40_000_000), "SHIP": range(40_000_000, 80_000_000),
                     "PO": range(100_000), "INV": range(100_000), "PAY": range(100_000),
                     "RUN": range(40_000), "WO": range(40_000, 80_000)}
    caps = {p: len(span) for p, span in spans.items()}
    assert caps == {"ORD": 40_000_000, "SHIP": 40_000_000, "PO": 100_000, "INV": 100_000,
                    "PAY": 100_000, "RUN": 40_000, "WO": 40_000}


# ------------------------------------------------------- insert_numbered ---

_SQL = "INSERT INTO invoices (invoice_number) VALUES (%s) RETURNING invoice_id"


def test_insert_numbered_inserts_under_a_released_savepoint():
    cur = FakeCursor()
    number, row = utils.insert_numbered(cur, "INV", _SQL, lambda n: (n,))
    assert number.endswith("-00001") and row == (1,)
    assert cur.sql[-3:] == ["SAVEPOINT adt_numbered_insert", _SQL, "RELEASE SAVEPOINT adt_numbered_insert"]


def test_a_conflict_regenerates_once_and_keeps_the_transaction_usable():
    today = datetime.now().strftime("%Y%m%d")
    cur = FakeCursor(taken={f"INV-{today}-00001"})
    number, _row = utils.insert_numbered(cur, "INV", _SQL, lambda n: (n,))
    assert number == f"INV-{today}-00002"
    assert cur.sql.count("ROLLBACK TO SAVEPOINT adt_numbered_insert") == 1


def test_a_second_conflict_skips_the_row_with_a_warning(caplog):
    today = datetime.now().strftime("%Y%m%d")
    cur = FakeCursor(taken={f"INV-{today}-00001", f"INV-{today}-00002"})
    assert utils.insert_numbered(cur, "INV", _SQL, lambda n: (n,), what="invoice") is None
    assert cur.sql.count("ROLLBACK TO SAVEPOINT adt_numbered_insert") == 2
    assert any("invoice insert skipped" in r.getMessage() for r in caplog.records)


def test_any_other_error_is_rolled_back_and_raised():
    cur = FakeCursor(fail_insert=ValueError("bad param"))
    with pytest.raises(ValueError):
        utils.insert_numbered(cur, "INV", _SQL, lambda n: (n,))
    assert cur.sql[-2:] == ["ROLLBACK TO SAVEPOINT adt_numbered_insert",
                            "RELEASE SAVEPOINT adt_numbered_insert"]


# ------------------------------------------------ real PostgreSQL (23505) ---

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture
def pg_conn():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    from erp_simulation import pgdriver
    from erp_simulation.pgdriver import make_dsn, parse_dsn
    db_name = "bn_" + uuid.uuid4().hex[:8]
    admin = pgdriver.connect(_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(stmt("CREATE DATABASE {db}", db=db_name))
    conn = pgdriver.connect(make_dsn(**{**parse_dsn(_DSN), "dbname": db_name}))
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE invoices (invoice_id SERIAL PRIMARY KEY, "
                        "invoice_number VARCHAR(50) UNIQUE NOT NULL, note TEXT)")
        conn.commit()
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(stmt("DROP DATABASE {db}", db=db_name))
        admin.close()


_PG_SQL = "INSERT INTO invoices (invoice_number, note) VALUES (%s, %s) RETURNING invoice_id"


def _notes(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT note FROM invoices ORDER BY invoice_id")
        return [r[0] for r in cur.fetchall()]


def test_pg_sequence_numbers_are_distinct_and_survive_a_new_process(pg_conn):
    with pg_conn.cursor() as cur:
        first = [utils.generate_invoice_number(cur, _DAY) for _ in range(3)]
    pg_conn.commit()
    utils._reset_sequence_cache()          # a new Lambda container
    with pg_conn.cursor() as cur:
        nxt = utils.generate_invoice_number(cur, _DAY)
    assert first == ["INV-20260930-00001", "INV-20260930-00002", "INV-20260930-00003"]
    assert nxt == "INV-20260930-00004"


def test_pg_a_real_conflict_does_not_abort_sibling_inserts(pg_conn):
    today = datetime.now().strftime("%Y%m%d")
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO invoices (invoice_number, note) VALUES (%s, 'legacy')",
                    (f"INV-{today}-00002",))          # a pre-sequence random number
        pg_conn.commit()
        results = [utils.insert_numbered(cur, "INV", _PG_SQL, lambda n, i=i: (n, f"row{i}"))
                   for i in range(3)]
    pg_conn.commit()
    assert [r[0] for r in results] == [f"INV-{today}-00001", f"INV-{today}-00003",
                                       f"INV-{today}-00004"]
    assert _notes(pg_conn) == ["legacy", "row0", "row1", "row2"]


def test_pg_two_conflicts_skip_one_row_and_keep_the_rest(pg_conn):
    today = datetime.now().strftime("%Y%m%d")
    with pg_conn.cursor() as cur:
        for n in ("00002", "00003"):
            cur.execute("INSERT INTO invoices (invoice_number, note) VALUES (%s, 'legacy')",
                        (f"INV-{today}-{n}",))
        pg_conn.commit()
        results = [utils.insert_numbered(cur, "INV", _PG_SQL, lambda n, i=i: (n, f"row{i}"))
                   for i in range(3)]
    pg_conn.commit()
    assert results[1] is None
    assert _notes(pg_conn) == ["legacy", "legacy", "row0", "row2"]


def test_the_sequence_cache_is_per_database():
    """A process that talks to a second database (a new test DB, a restored
    cluster) creates the sequence there too instead of trusting the cache."""
    from types import SimpleNamespace
    first, second = FakeCursor(), FakeCursor()
    first.connection = SimpleNamespace(dsn="dbname=a")
    second.connection = SimpleNamespace(dsn="dbname=b")
    utils.generate_run_number(first, _DAY)
    utils.generate_run_number(second, _DAY)
    utils.generate_run_number(second, _DAY)
    assert sum(s.startswith("CREATE SEQUENCE") for s in first.sql) == 1
    assert sum(s.startswith("CREATE SEQUENCE") for s in second.sql) == 1
