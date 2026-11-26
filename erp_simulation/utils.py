# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Utility functions for ERP Simulation System.

Provides helper functions for ID generation, number formatting, and common operations.

Business numbers (ADR-0090). Every number with a UNIQUE column behind it
(order, shipment, PO, invoice, payment, production run, MES work order) is
``PREFIX-YYYYMMDD-N...N`` with the digits drawn from ONE PostgreSQL sequence
per prefix (``seq_<kind>_number``), never from randomness alone. Each prefix
owns a range of suffixes and the sequence value is placed in it modulo the
range's length: within a day the numbers are distinct as long as fewer than
that length are issued that day (the DAILY CAP: 100,000 invoices / payments
/ POs, 40,000 runs, 40,000 work orders, 40,000,000 orders, 40,000,000
shipments). Every range ends below the numbers the workshop story owns
(``story_history`` 9xxxx / 9xxxxxxx, ``story_live`` 8xxxx), and the PAIRED
prefixes get disjoint ranges (RUN 0-39,999 and WO 40,000-79,999; ORD
0-39,999,999 and SHIP 40,000,000-79,999,999). The story gives a run and its
own work order, and an order and its own shipment, the SAME digits; an
unrelated simulator run and work order (or order and shipment) never share
them, so a reader matching numbers by digits cannot pair two unrelated
rows. The sequence is created on first use (``CREATE SEQUENCE IF NOT EXISTS``), and
:func:`insert_numbered` wraps the INSERT in a SAVEPOINT so a conflict (a
row numbered before this scheme, or a daily wrap) regenerates once and then
skips just that row with a warning: one conflict never aborts the cycle.
"""

from contextlib import contextmanager
from datetime import datetime
import random
import re
import string
from typing import Callable, Optional, Tuple

from .logging_config import get_logger

logger = get_logger(__name__)


class SqlText(str):
    """SQL text built only from code: a :func:`sql_text` result, or a fixed
    fragment written in the source (``SqlText("...")``, the plain-text
    counterpart of pgdriver's ``sql.SQL``). Never wrap a value from input."""


# A name PostgreSQL reads the same quoted or not: lower case, optionally
# qualified once (``schema.table``, ``alias.column``).
_PLAIN_IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?')


def sql_text(template: str, /, **pieces) -> SqlText:
    """``template`` (a code constant with ``{name}`` placeholders; driver
    parameters ``%s`` / ``%(name)s`` stay as they are) with each piece in
    place, as plain text any DB-API cursor takes.

    A :class:`SqlText` piece goes in verbatim. Any other piece must be a plain
    identifier (``_PLAIN_IDENTIFIER``), which goes in unquoted, so the text is
    what an f-string gave; anything else raises ``ValueError``. Values are
    never pieces: bind them. For a name that needs quoting or a literal value,
    use :func:`compose_sql`."""
    def piece(name, value):
        if isinstance(value, SqlText):
            return value
        if isinstance(value, str) and _PLAIN_IDENTIFIER.fullmatch(value):
            return value
        raise ValueError(f"sql_text: {name}={value!r} is neither SqlText nor a plain identifier")

    return SqlText(template.format(**{name: piece(name, value) for name, value in pieces.items()}))


def compose_sql(template: str, /, **pieces):
    """``template`` (a code constant with ``{name}`` placeholders) as a pgdriver
    ``sql.Composed``: a ``str`` piece is quoted as an identifier, a
    ``sql.Composable`` piece (``sql.Literal(...)``) goes in as is. Needs a real
    pgdriver cursor to execute."""
    from erp_simulation.pgdriver import sql  # lazy, as console_login: importing utils needs no driver

    return sql.SQL(template).format(**{
        name: value if isinstance(value, sql.Composable) else sql.Identifier(value)
        for name, value in pieces.items()})

# prefix -> (sequence name, digits, suffix range). The names are code
# constants: they are the only identifiers interpolated into SQL here. The
# range's length is the daily cap. RUN/WO and ORD/SHIP split the space below
# the story's numbers (80,000 / 80,000,000 and up) into disjoint halves, so
# two unrelated numbers of a pair never carry the same digits.
BUSINESS_NUMBER_SPACE = {
    'ORD': ('seq_order_number', 8, range(40_000_000)),
    'SHIP': ('seq_shipment_number', 8, range(40_000_000, 80_000_000)),
    'PO': ('seq_po_number', 5, range(100_000)),
    'INV': ('seq_invoice_number', 5, range(100_000)),
    'PAY': ('seq_payment_number', 5, range(100_000)),
    'RUN': ('seq_run_number', 5, range(40_000)),
    'WO': ('seq_work_order_number', 5, range(40_000, 80_000)),
}

# Rows one bulk simulator step (shipping, invoicing, payments) handles per
# cycle, oldest first: a backlog drains over cycles instead of blowing the
# Lambda timeout.
MAX_ROWS_PER_CYCLE = 200

# The ``production_runs.notes`` of every run the live-story history writer
# plants (``story_history.write``). Those runs are a planted past whose
# shipments the history plan records in full, so the live shipper never
# allocates a new shipment to one (``ShippingSimulator._traceable_batch_id``).
HISTORY_RUN_NOTE = 'Live-story history run'

# The number spaces the workshop story owns (ADR-0090): story_history numbers
# its orders ORD-YYYYMMDD-9NNNNNNN and the matching shipment SHIP-YYYYMMDD with
# the SAME 9NNNNNNN suffix (its failed-batch orders 9000000N, its run orders
# <run tail>001: both 8 digits starting 9). The simulator's own ORD numbers
# are 0..39,999,999 and its SHIP numbers 40,000,000..79,999,999
# (BUSINESS_NUMBER_SPACE), so their 8-digit field starts 0..7 and never
# collides with the story's leading 9.
# ONE shared definition so the shipment ladder and the order catch-up
# (state_advancer) exclude story-owned rows. ``story_replan`` keeps its own
# identical order-pattern literal (its module-size budget); a test pins the two
# equal so they cannot drift.
STORY_ORDER_NUMBER_RE = r'^ORD-[0-9]{8}-9[0-9]{7}$'
STORY_SHIPMENT_NUMBER_RE = r'^SHIP-[0-9]{8}-9[0-9]{7}$'

UNIQUE_VIOLATION = '23505'  # PostgreSQL SQLSTATE unique_violation

# (database, sequence) pairs this process has created or found, keyed by the
# connection DSN so another database never inherits the entry. The
# orchestrator re-ensures every sequence at the start of every cycle (its own
# commit), so an entry never outlives its sequence for more than one cycle.
_ensured_sequences = set()


def _sequence_key(cursor, sequence: str) -> tuple:
    connection = getattr(cursor, 'connection', None)
    dsn_parameters = getattr(connection, 'get_dsn_parameters', None)
    # pgdriver has no ``dsn`` string; its connect parameters (password left
    # out) name the database the same way.
    parameters = dsn_parameters() if callable(dsn_parameters) else None
    dsn = tuple(sorted(parameters.items())) if isinstance(parameters, dict) else getattr(connection, 'dsn', None)
    return (dsn, sequence)


def _reset_sequence_cache() -> None:
    """Forget which sequences exist (tests)."""
    _ensured_sequences.clear()


@contextmanager
def savepoint(cursor, name: str = 'adt_row'):
    """Run the block under ``SAVEPOINT name``: on any exception roll back to it
    (the transaction stays usable) and re-raise; otherwise release it."""
    cursor.execute(sql_text("SAVEPOINT {name}", name=name))
    try:
        yield
    except BaseException:
        cursor.execute(sql_text("ROLLBACK TO SAVEPOINT {name}", name=name))
        cursor.execute(sql_text("RELEASE SAVEPOINT {name}", name=name))
        raise
    cursor.execute(sql_text("RELEASE SAVEPOINT {name}", name=name))


def _ensure_sequence(cursor, sequence: str) -> None:
    """CREATE SEQUENCE IF NOT EXISTS under a savepoint; a concurrent creator
    (an overlapping invocation) losing the race is not an error; a name that
    is not a plain identifier raises before anything runs."""
    create = sql_text("CREATE SEQUENCE IF NOT EXISTS {sequence}", sequence=sequence)
    try:
        with savepoint(cursor, 'adt_number_sequence'):
            cursor.execute(create)
    except Exception as e:
        logger.debug(f"CREATE SEQUENCE {sequence} raced or failed ({type(e).__name__}); using it as is")
    _ensured_sequences.add(_sequence_key(cursor, sequence))


def ensure_number_sequences(cursor) -> None:
    """Create every business-number sequence that is missing (idempotent)."""
    for sequence, _digits, _span in BUSINESS_NUMBER_SPACE.values():
        _ensure_sequence(cursor, sequence)


def next_business_number(cursor, prefix: str, now: Optional[datetime] = None) -> str:
    """``PREFIX-YYYYMMDD-<digits>``: the prefix's sequence value modulo the
    length of its suffix range, offset to the range's start."""
    sequence, digits, span = BUSINESS_NUMBER_SPACE[prefix]
    if _sequence_key(cursor, sequence) not in _ensured_sequences:
        _ensure_sequence(cursor, sequence)
    cursor.execute("SELECT nextval(%s)", (sequence,))
    value = span.start + int(cursor.fetchone()[0]) % len(span)
    return f"{prefix}-{(now or datetime.now()):%Y%m%d}-{value:0{digits}d}"


def is_unique_violation(error: BaseException) -> bool:
    """True for a PostgreSQL unique_violation (SQLSTATE 23505)."""
    return getattr(error, 'pgcode', None) == UNIQUE_VIOLATION


def insert_numbered(cursor, prefix: str, sql: str, params_for: Callable[[str], tuple], *,
                    returning: bool = True, what: str = '') -> Optional[Tuple[str, Optional[tuple]]]:
    """INSERT one row carrying a fresh business number, under a savepoint.

    ``params_for(number)`` builds the parameters. On a unique violation the
    row is rolled back to the savepoint and retried ONCE with a new number;
    a second conflict skips the row (warning, returns None) and the
    transaction stays usable. Returns ``(number, RETURNING row or None)``.
    Any other error is rolled back to the savepoint and re-raised.
    """
    numbers = []
    for _attempt in range(2):
        number = next_business_number(cursor, prefix)
        numbers.append(number)
        try:
            with savepoint(cursor, 'adt_numbered_insert'):
                cursor.execute(sql, params_for(number))
                row = cursor.fetchone() if returning else None
        except Exception as e:
            if not is_unique_violation(e):
                raise
            continue
        return number, row
    logger.warning(f"{what or prefix} insert skipped: {' and '.join(numbers)} both hit a unique "
                   f"constraint; the row is left for the next cycle")
    return None


def generate_order_number(cursor, now: Optional[datetime] = None) -> str:
    """Order number ``ORD-YYYYMMDD-NNNNNNNN`` (sequence-backed, 0..39,999,999:
    cap 40,000,000/day, never a shipment's 40,000,000.. or the story's range)."""
    return next_business_number(cursor, 'ORD', now)


def generate_po_number(cursor, now: Optional[datetime] = None) -> str:
    """Purchase order number ``PO-YYYYMMDD-NNNNN`` (sequence-backed, cap 100,000/day)."""
    return next_business_number(cursor, 'PO', now)


def generate_shipment_number(cursor, now: Optional[datetime] = None) -> str:
    """Shipment number ``SHIP-YYYYMMDD-NNNNNNNN`` (sequence-backed,
    40,000,000..79,999,999: cap 40,000,000/day, never an order's 0..39,999,999
    or the story's range)."""
    return next_business_number(cursor, 'SHIP', now)


def generate_invoice_number(cursor, now: Optional[datetime] = None) -> str:
    """Invoice number ``INV-YYYYMMDD-NNNNN`` (sequence-backed, cap 100,000/day)."""
    return next_business_number(cursor, 'INV', now)


def generate_payment_number(cursor, now: Optional[datetime] = None) -> str:
    """Payment number ``PAY-YYYYMMDD-NNNNN`` (sequence-backed, cap 100,000/day)."""
    return next_business_number(cursor, 'PAY', now)


def generate_tracking_number() -> str:
    """
    Generate a tracking number for shipments.
    
    Returns:
        Tracking number in format: 1Z999AA10123456784
    """
    return '1Z' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))  # nosec B311 - synthetic data generation, not security context


def generate_reference_number() -> str:
    """
    Generate a reference number for payments.
    
    Returns:
        Reference number in format: REF-XXXXXXXXXXXX
    """
    random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))  # nosec B311 - synthetic data generation, not security context
    return f"REF-{random_part}"


def generate_run_number(cursor, now: Optional[datetime] = None) -> str:
    """Production run number ``RUN-YYYYMMDD-NNNNN`` (sequence-backed, 0-39999:
    cap 40,000/day, never a work order's 40000-79999 or the story's 80000-99999)."""
    return next_business_number(cursor, 'RUN', now)


def generate_work_order_number(cursor, now: Optional[datetime] = None) -> str:
    """MES work order number ``WO-YYYYMMDD-NNNNN`` (sequence-backed, 40000-79999:
    cap 40,000/day, never a run's 0-39999 or the story's 80000-99999)."""
    return next_business_number(cursor, 'WO', now)


def calculate_tax(subtotal: float, tax_rate: float = 0.08) -> float:
    """
    Calculate tax amount.
    
    Args:
        subtotal: Subtotal amount.
        tax_rate: Tax rate (default 8%).
        
    Returns:
        Tax amount.
    """
    return round(subtotal * tax_rate, 2)

