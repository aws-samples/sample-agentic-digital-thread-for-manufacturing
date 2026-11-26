# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Inventory draws that keep an item's stock positive in every location.

An item's stock can sit in more than one ERP location: its site warehouse and
the legacy central one (``WH-RAW`` for raw materials, ``FG-WAREHOUSE`` for
finished goods). MES material consumption always drew from ``WH-RAW`` while the
site warehouse held the seeded stock, so a fresh account showed negative raw
balances within hours (live 2026-10-02: the 7075 bar stock read IND-WH-RAW
2,400 kg and WH-RAW -530 kg, and the ERP agent flagged it as an allocation
problem).

:func:`plan_draw` spends the preferred location first, then the item's other
locations, fullest first. Only a quantity the item's TOTAL cannot cover is left
as a negative, on the preferred location: a real stock-out, which the
procurement trigger (it sums every location) already reads.
:func:`plan_netting` moves stock between an item's locations to clear the
negatives older draws left behind, so a long-lived account heals without a
reseed. Neither ever changes an item's total.
"""
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple

#: (location_id, quantity_on_hand)
Balance = Tuple[str, int]

#: inventory_transactions.notes on a netting TRANSFER pair.
NETTING_NOTE = "netting: cleared a negative balance from the item's other location"

_BALANCES_SQL = """
    SELECT ib.location_id, il.location_code, ib.quantity_on_hand
    FROM inventory_balances ib
    JOIN inventory_locations il ON il.location_id = ib.location_id
    WHERE ib.item_id = %s
      AND il.location_code = ANY(%s)
    ORDER BY il.location_code
"""

_SPLIT_ITEMS_SQL = """
    SELECT ib.item_id
    FROM inventory_balances ib
    JOIN inventory_locations il ON il.location_id = ib.location_id
    WHERE il.location_code = ANY(%s)
    GROUP BY ib.item_id
    HAVING MIN(ib.quantity_on_hand) < 0 AND MAX(ib.quantity_on_hand) > 0
    ORDER BY ib.item_id
    LIMIT %s
"""


def plan_draw(balances: Sequence[Balance], quantity: int,
              preferred: Optional[str] = None) -> List[Tuple[str, int]]:
    """``[(location_id, take)]`` that removes ``quantity`` from ``balances``. PURE.

    Preferred location first, then the rest fullest first; each gives at most
    what it holds. A remainder no location covers goes to the preferred location
    (else the first in that order): the only way a balance turns negative.
    """
    if quantity <= 0 or not balances:
        return []
    order = sorted(balances, key=lambda b: (b[0] != preferred, -int(b[1])))
    plan, left = {}, int(quantity)
    for location_id, on_hand in order:
        take = min(left, max(int(on_hand), 0))
        if take > 0:
            plan[location_id] = take
            left -= take
        if left == 0:
            break
    if left > 0:
        target = order[0][0]
        plan[target] = plan.get(target, 0) + left
    return list(plan.items())


def plan_netting(balances: Sequence[Balance]) -> List[Tuple[str, str, int]]:
    """``[(from_location_id, to_location_id, quantity)]`` clearing negatives. PURE.

    Each negative location is topped up from the positive ones, fullest first,
    never taking a donor below zero. The item's total is unchanged.
    """
    donors = sorted(([loc, int(q)] for loc, q in balances if int(q) > 0), key=lambda d: -d[1])
    moves = []
    for location_id, on_hand in sorted(balances, key=lambda b: int(b[1])):
        need = -int(on_hand)
        for donor in donors:
            if need <= 0:
                break
            give = min(need, donor[1])
            if give > 0:
                moves.append((donor[0], location_id, give))
                donor[1] -= give
                need -= give
    return moves


def _item_balances(cursor, item_id, location_codes: Iterable[str]):
    cursor.execute(_BALANCES_SQL, (item_id, list(location_codes)))
    return cursor.fetchall()


def _post(cursor, item_id, location_id, transaction_type, delta, reference_type,
          reference_id, when, notes=None) -> None:
    """One inventory transaction and the matching balance change."""
    cursor.execute("""
        INSERT INTO inventory_transactions (
            item_id, location_id, transaction_type,
            quantity, reference_type, reference_id,
            transaction_date, notes
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (item_id, location_id, transaction_type, delta, reference_type, reference_id, when, notes))
    cursor.execute("""
        UPDATE inventory_balances
        SET quantity_on_hand = quantity_on_hand + %s,
            last_transaction_date = %s
        WHERE item_id = %s AND location_id = %s
    """, (delta, when, item_id, location_id))


def draw(cursor, item_id, quantity: int, location_codes: Iterable[str], *,
         reference_type: str, reference_id, preferred_code: Optional[str] = None,
         when: Optional[datetime] = None) -> List[Tuple[str, int]]:
    """Post CONSUMPTION of ``quantity`` across ``location_codes`` (:func:`plan_draw`).

    Returns ``[(location_code, taken)]``; empty, with nothing posted, when the
    item has no balance in any of those locations.
    """
    rows = _item_balances(cursor, item_id, location_codes)
    codes = {location_id: code for location_id, code, _on_hand in rows}
    preferred = next((lid for lid, code in codes.items() if code == preferred_code), None)
    plan = plan_draw([(lid, on_hand) for lid, _code, on_hand in rows], quantity, preferred)
    when = when or datetime.now()
    for location_id, take in plan:
        _post(cursor, item_id, location_id, 'CONSUMPTION', -take, reference_type, reference_id, when)
    return [(codes[lid], take) for lid, take in plan]


def net_split_balances(cursor, location_codes: Iterable[str], *, limit: int = 50,
                       when: Optional[datetime] = None) -> int:
    """Clear negative balances an item holds beside positive ones; transfers posted.

    Bounded to ``limit`` items per call (oldest first by id), so a long-lived
    account heals over a few cycles. Each move is a TRANSFER pair.
    """
    codes = list(location_codes)
    cursor.execute(_SPLIT_ITEMS_SQL, (codes, limit))
    moves = 0
    when = when or datetime.now()
    for (item_id,) in cursor.fetchall():
        rows = _item_balances(cursor, item_id, codes)
        for source, target, quantity in plan_netting([(lid, q) for lid, _c, q in rows]):
            _post(cursor, item_id, source, 'TRANSFER', -quantity, 'NETTING', None, when, NETTING_NOTE)
            _post(cursor, item_id, target, 'TRANSFER', quantity, 'NETTING', None, when, NETTING_NOTE)
            moves += 1
    return moves
