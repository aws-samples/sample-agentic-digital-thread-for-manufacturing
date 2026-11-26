# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers for SQL-trace characterization tests after ADR-0090.

The business SQL the characterization tests pin is unchanged; ADR-0090 wraps
it in savepoints and draws business numbers from a PostgreSQL sequence.
:func:`fake_business_numbers` replaces the sequence read with a format-valid
counter, and :func:`without_isolation` drops the ADR-0090 savepoint wrappers
from a recorded trace, so a golden keeps pinning exactly the business SQL.
"""

import random
import re
import string
from datetime import datetime
from itertools import count

from erp_simulation import utils

ISOLATION_SAVEPOINTS = ('adt_numbered_insert', 'adt_number_sequence', 'plan_run', 'backorder_run',
                        'invoice_row', 'payment_row', 'supplier_po')
_ISOLATION_RE = re.compile(r"^\s*(SAVEPOINT|RELEASE SAVEPOINT|ROLLBACK TO SAVEPOINT) (%s)\s*$"
                           % '|'.join(ISOLATION_SAVEPOINTS))


def fake_business_numbers(monkeypatch):
    """Sequence-free business numbers in the production format."""
    counter = count(1)

    def next_number(cursor, prefix, now=None):
        _seq, digits, span = utils.BUSINESS_NUMBER_SPACE[prefix]
        # The pre-ADR-0090 generator drew ``digits`` random digits; consuming
        # the same draws keeps every seeded golden's random stream identical.
        random.choices(string.digits, k=digits)  # nosec B311 - test fixture
        # The prefix's own suffix range, as next_business_number places it.
        value = span.start + next(counter) % len(span)
        return f"{prefix}-{(now or datetime.now()):%Y%m%d}-{value:0{digits}d}"

    monkeypatch.setattr(utils, 'next_business_number', next_number)


def without_isolation(calls):
    """The recorded (sql, params) calls minus the ADR-0090 savepoint wrappers."""
    return [call for call in calls if not _ISOLATION_RE.match(call[0])]
