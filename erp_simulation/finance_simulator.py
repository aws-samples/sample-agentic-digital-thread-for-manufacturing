# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Finance Simulator for ERP Simulation System.

Manages invoice generation and payment processing.
"""

import random
from datetime import datetime, timedelta
from typing import Dict, Any

from .config import get_config
from .utils import (MAX_ROWS_PER_CYCLE, calculate_tax, generate_reference_number, insert_numbered,
                    savepoint, sql_text)
from .logging_config import get_logger
from .base_simulator import BaseSimulator

logger = get_logger(__name__)

_PAYMENT_TERM_DAYS = {'NET_3': 3, 'NET_5': 5, 'NET_7': 7, 'NET_30': 30, 'NET_60': 60}  # else 5 days

# Oldest first, bounded, and SKIP LOCKED so an overlapping invocation takes a
# disjoint batch instead of waiting on (or double-invoicing) the same rows.
_SHIPMENTS_TO_INVOICE_SQL = """
    SELECT s.shipment_id, s.shipment_number, s.order_id, s.customer_id
    FROM shipments s
    WHERE s.status IN ('SHIPPED', 'IN_TRANSIT', 'DELIVERED')
      AND NOT EXISTS (
          SELECT 1 FROM invoices i
          WHERE i.shipment_id = s.shipment_id
      )
    ORDER BY s.shipment_id
    LIMIT %s
    FOR UPDATE OF s SKIP LOCKED
"""

_INVOICES_TO_PAY_SQL = """
    SELECT i.invoice_id, i.invoice_number, i.customer_id, i.total_amount, i.due_date
    FROM invoices i
    WHERE i.status = 'SENT'
      AND i.balance_due > 0
      AND i.due_date <= NOW() + INTERVAL '5 days'
    ORDER BY i.due_date, i.invoice_id
    LIMIT %s
    FOR UPDATE OF i SKIP LOCKED
"""

_INSERT_INVOICE_SQL = """
    INSERT INTO invoices (
        invoice_number, customer_id, order_id, shipment_id,
        invoice_date, due_date, status
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    RETURNING invoice_id
"""

_INSERT_PAYMENT_SQL = """
    INSERT INTO payments (
        payment_number, customer_id, invoice_id,
        payment_date, payment_method, amount, reference_number
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    RETURNING payment_id
"""

# (column, table, date column) are code constants, rendered by sql_text.
_SINCE_LAST_CYCLE_SQL = """
    SELECT COALESCE(SUM({column}), 0)
    FROM {table}
    WHERE {date_column} >= (
        SELECT COALESCE(MAX(event_timestamp), NOW() - INTERVAL '1 hour')
        FROM simulation_events
        WHERE cycle_number < (SELECT COALESCE(MAX(cycle_number), 0) FROM simulation_events)
    )
"""


def _since_last_cycle(cursor, column: str, table: str, date_column: str) -> float:
    """SUM(column) of the rows dated since the previous recorded cycle."""
    cursor.execute(sql_text(_SINCE_LAST_CYCLE_SQL, column=column, table=table, date_column=date_column))
    return float(cursor.fetchone()[0])


class FinanceSimulator(BaseSimulator):
    """
    Simulates financial transactions.
    
    Generates invoices for shipments, creates invoice line items,
    simulates payment receipt with realistic delays, and updates invoice status.
    """
    
    def __init__(self):
        """Initialize the finance simulator."""
        self.config = get_config()
        self.payment_methods = ['WIRE_TRANSFER', 'CHECK', 'ACH', 'CREDIT_CARD']
        logger.info("Finance simulator initialized")
    
    def generate_invoices(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Generate invoices for shipped shipments, at most MAX_ROWS_PER_CYCLE per
        cycle (oldest first; rows an overlapping invocation holds are skipped).
        Each invoice runs under its own savepoint (ADR-0090).

        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.

        Returns:
            Dictionary with invoice generation statistics.
        """
        cursor = conn.cursor()
        try:
            cursor.execute(_SHIPMENTS_TO_INVOICE_SQL, (MAX_ROWS_PER_CYCLE,))
            shipments_to_invoice = cursor.fetchall()
            if not shipments_to_invoice:
                logger.debug("No shipped shipments need invoices")
                return {'invoices_generated': 0}
            logger.info(f"Found {len(shipments_to_invoice)} shipments needing invoices "
                        f"(batch limit {MAX_ROWS_PER_CYCLE})")
            invoices_generated = sum(1 for shipment in shipments_to_invoice
                                     if self._invoice_shipment_isolated(cursor, *shipment))
            total_revenue = _since_last_cycle(cursor, 'total_amount', 'invoices', 'invoice_date')
            logger.info(f"Generated {invoices_generated} invoices totaling ${total_revenue:.2f} (cycle: {cycle_id})")
            return {
                'invoices_generated': invoices_generated,
                'total_revenue': total_revenue,
                'cycle_id': cycle_id
            }
        except Exception as e:
            logger.error(f"Invoice generation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _invoice_shipment_isolated(self, cursor, shipment_id, shipment_number, order_id, customer_id) -> bool:
        """Invoice one shipment under a savepoint; False (rolled back, logged) on failure."""
        try:
            with savepoint(cursor, 'invoice_row'):
                return self._invoice_shipment(cursor, shipment_id, shipment_number, order_id, customer_id)
        except Exception as e:
            logger.error(f"Failed to generate invoice for shipment {shipment_number}: {e}", exc_info=True)
            return False

    def _invoice_shipment(self, cursor, shipment_id, shipment_number, order_id, customer_id) -> bool:
        """Insert the invoice, its line items and totals; False when the number was skipped."""
        cursor.execute("""
            SELECT payment_terms
            FROM customers
            WHERE customer_id = %s
        """, (customer_id,))
        payment_terms_row = cursor.fetchone()
        payment_terms = payment_terms_row[0] if payment_terms_row else 'NET_30'
        invoice_date = datetime.now()
        due_date = invoice_date + timedelta(days=_PAYMENT_TERM_DAYS.get(payment_terms, 5))
        inserted = insert_numbered(
            cursor, 'INV', _INSERT_INVOICE_SQL,
            lambda number: (number, customer_id, order_id, shipment_id, invoice_date, due_date, 'SENT'),
            what=f"invoice for shipment {shipment_number}")
        if inserted is None:
            return False
        invoice_number, (invoice_id,) = inserted
        subtotal = self._insert_invoice_lines(cursor, invoice_id, order_id)
        tax_amount = calculate_tax(subtotal)
        total_amount = subtotal + tax_amount
        cursor.execute("""
            UPDATE invoices
            SET subtotal = %s,
                tax_amount = %s,
                total_amount = %s
            WHERE invoice_id = %s
        """, (subtotal, tax_amount, total_amount, invoice_id))
        logger.debug(
            f"Generated invoice {invoice_number} for shipment {shipment_number}: "
            f"total ${total_amount:.2f}, due {due_date.strftime('%Y-%m-%d')}"
        )
        return True

    @staticmethod
    def _insert_invoice_lines(cursor, invoice_id, order_id) -> float:
        """One invoice line per order line; returns the subtotal."""
        cursor.execute("""
            SELECT oli.product_id, p.product_name, oli.quantity, oli.unit_price
            FROM order_line_items oli
            JOIN products p ON oli.product_id = p.product_id
            WHERE oli.order_id = %s
        """, (order_id,))
        subtotal = 0.0
        for line_number, (product_id, product_name, quantity, unit_price) in enumerate(cursor.fetchall(), 1):
            subtotal += quantity * float(unit_price)
            cursor.execute("""
                INSERT INTO invoice_line_items (
                    invoice_id, line_number, description,
                    product_id, quantity, unit_price
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (invoice_id, line_number, product_name, product_id, quantity, unit_price))
        return subtotal

    def process_payments(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Process payments for due invoices, at most MAX_ROWS_PER_CYCLE per cycle
        (earliest due first). Some customers pay early, some on time, some late.
        Each payment runs under its own savepoint (ADR-0090).

        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.

        Returns:
            Dictionary with payment processing statistics.
        """
        cursor = conn.cursor()
        try:
            cursor.execute(_INVOICES_TO_PAY_SQL, (MAX_ROWS_PER_CYCLE,))
            invoices_to_pay = cursor.fetchall()
            if not invoices_to_pay:
                logger.debug("No invoices ready for payment")
                return {'payments_recorded': 0}
            logger.info(f"Found {len(invoices_to_pay)} invoices ready for payment "
                        f"(batch limit {MAX_ROWS_PER_CYCLE})")
            payments_recorded = sum(1 for invoice in invoices_to_pay
                                    if self._pay_invoice_isolated(cursor, *invoice))
            total_collected = _since_last_cycle(cursor, 'amount', 'payments', 'payment_date')
            logger.info(f"Recorded {payments_recorded} payments totaling ${total_collected:.2f} (cycle: {cycle_id})")
            return {
                'payments_recorded': payments_recorded,
                'total_collected': total_collected,
                'cycle_id': cycle_id
            }
        except Exception as e:
            logger.error(f"Payment processing failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _pay_invoice_isolated(self, cursor, invoice_id, invoice_number, customer_id, total_amount, due_date) -> bool:
        """Maybe pay one invoice under a savepoint; False when skipped or failed (logged)."""
        try:
            with savepoint(cursor, 'payment_row'):
                return self._pay_invoice(cursor, invoice_id, invoice_number, customer_id, total_amount)
        except Exception as e:
            logger.error(f"Failed to process payment for invoice {invoice_number}: {e}", exc_info=True)
            return False

    def _pay_invoice(self, cursor, invoice_id, invoice_number, customer_id, total_amount) -> bool:
        """Randomly (configurable probability) record a full payment; False when not paid."""
        payment_probability = self._load_config_value(cursor, 'finance_payment_probability', 0.30, 'float')
        if random.random() > payment_probability:  # nosec B311 - synthetic data generation, not security context
            return False
        payment_date = datetime.now()
        payment_method = random.choice(self.payment_methods)  # nosec B311 - synthetic data generation, not security context
        reference_number = generate_reference_number()
        inserted = insert_numbered(
            cursor, 'PAY', _INSERT_PAYMENT_SQL,
            lambda number: (number, customer_id, invoice_id, payment_date, payment_method,
                            total_amount, reference_number),
            what=f"payment for invoice {invoice_number}")
        if inserted is None:
            return False
        cursor.execute("""
            UPDATE invoices
            SET amount_paid = %s,
                status = 'PAID'
            WHERE invoice_id = %s
        """, (total_amount, invoice_id))
        logger.debug(
            f"Recorded payment {inserted[0]} for invoice {invoice_number}: "
            f"${total_amount:.2f} via {payment_method}"
        )
        return True
