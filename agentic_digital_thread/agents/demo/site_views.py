# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo view-schema DDL for the admin ``/migrate/erp-mes-views`` route.

The demo Aurora database keeps the corporate ERP and every site's MES in the
``public`` tables; onboarding sees them through view schemas: ``erp_data.<t>``
(one pass-through per ERP table) and one ``mes_<site>`` schema per site
(filtered by ``mes_work_orders.site`` / ``production_lines.site_id``).

``lambda-db-init/handler.py`` (``get_views_sql`` + ``get_machining_views_sql``)
creates the SAME statements at deploy; the Lambda never imports the app, so the
table lists live in both places and ``tests/unit/test_admin_site_views.py``
pins them equal. Demo content — lives in ``agents.demo`` (ADR-0044).

Requires a db-init-built ERP database: several viewed tables (the lot
tables, ``inventory_transfers``, ``item_suppliers``, ``production_lines``)
exist only in ``lambda-db-init``'s DDL, not in ``erp_simulation/schema``, so
the migration fails on a database built from the latter alone.

Every interpolated identifier must match ``^[a-z_][a-z0-9_]*$`` and every
quoted literal ``^[A-Z0-9_]+$`` (checked when the SQL is built — the values
are module constants, validated anyway so no edit can smuggle SQL in).
"""
from __future__ import annotations

import re

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_LITERAL = re.compile(r"^[A-Z0-9_]+$")


def _ident(name: str) -> str:
    if not _IDENT.match(name or ""):
        raise ValueError(f"unsafe SQL identifier {name!r}")
    return name


def _literal(value: str) -> str:
    if not _LITERAL.match(value or ""):
        raise ValueError(f"unsafe SQL literal {value!r}")
    return value

#: public tables exposed 1:1 as ``erp_data.<table>`` views (the corporate ERP).
ERP_DATA_VIEWS: tuple[str, ...] = (
    "customers", "suppliers", "products", "customer_orders", "order_line_items",
    "purchase_orders", "po_line_items", "po_receipts", "invoices", "invoice_line_items",
    "payments", "inventory_items", "inventory_balances", "inventory_locations",
    "inventory_transactions", "inventory_transfers", "shipments", "shipment_items",
    "material_lots", "lot_warehouse_movements", "lot_mes_tracking", "lot_production_usage",
    "bill_of_materials", "item_suppliers", "product_site_routing",
)

#: (view schema, ``mes_work_orders.site``, ``production_lines.site_id``) per site.
SITE_MES_VIEWS: tuple[tuple[str, str, str], ...] = (
    ("mes_milwaukee", "MILWAUKEE_BOTTLING", "MKE"),
    ("mes_detroit", "DETROIT_AUTOPARTS", "DET"),
    ("mes_cedarrapids", "CEDARRAPIDS_FOOD", "CR"),
    ("mes_indianapolis", "INDIANAPOLIS_MACHINING", "IND"),
)

# (view, public table, alias) for the MES tables filtered through a work-order join.
_JOINED_MES_VIEWS: tuple[tuple[str, str, str], ...] = (
    ("production_events", "mes_production_events", "e"),
    ("quality_checks", "mes_quality_checks", "q"),
    ("downtime_events", "mes_downtime_events", "d"),
    ("material_consumption", "mes_material_consumption", "m"),
)

#: Every view each ``mes_<site>`` schema carries.
SITE_MES_VIEW_NAMES: tuple[str, ...] = (
    "work_orders", *(v for v, _t, _a in _JOINED_MES_VIEWS), "production_lines", "line_status",
    "production_runs", "equipment",
)


def _erp_data_views_sql() -> str:
    lines = ["CREATE SCHEMA IF NOT EXISTS erp_data;\n"]
    lines += [f"CREATE OR REPLACE VIEW erp_data.{t} AS SELECT * FROM public.{t};\n"  # nosec B608 - validated module constants, never input
              for t in map(_ident, ERP_DATA_VIEWS)]
    return "".join(lines)


def _site_mes_views_sql(schema: str, site_code: str, line_prefix: str) -> str:
    schema, site_code, line_prefix = _ident(schema), _literal(site_code), _literal(line_prefix)
    wo_join = "JOIN public.mes_work_orders w ON {a}.work_order_id = w.work_order_id"
    lines = [f"CREATE SCHEMA IF NOT EXISTS {schema};\n",
             f"CREATE OR REPLACE VIEW {schema}.work_orders AS SELECT * FROM public.mes_work_orders "
             f"WHERE site = '{site_code}';\n"]  # nosec B608 - validated identifiers and literals, never input
    lines += [f"CREATE OR REPLACE VIEW {schema}.{view} AS SELECT {a}.* FROM public.{table} {a} "
              f"{wo_join.format(a=a)} WHERE w.site = '{site_code}';\n"  # nosec B608 - validated identifiers and literals, never input
              for view, table, a in _JOINED_MES_VIEWS]
    lines += [f"CREATE OR REPLACE VIEW {schema}.production_lines AS SELECT * FROM public.production_lines "
              f"WHERE site_id = '{line_prefix}';\n",  # nosec B608 - validated identifiers and literals, never input
              f"CREATE OR REPLACE VIEW {schema}.line_status AS SELECT * FROM public.mes_line_status "
              f"WHERE site = '{site_code}';\n",  # nosec B608 - validated identifiers and literals, never input
              _site_runs_view_sql(schema, site_code),
              _site_equipment_view_sql(schema, site_code)]
    return "".join(lines)


def _site_equipment_view_sql(schema: str, site_code: str) -> str:
    """The site's MES equipment registry (``public.mes_equipment``, seeded by
    db-init's ``seed_mes_equipment``): the line, then the machine's id, name,
    type and status. No site column: the schema is the site. Arguments are
    pre-validated by :func:`_site_mes_views_sql`."""
    return (f"CREATE OR REPLACE VIEW {schema}.equipment AS SELECT "
            f"line, equipment_id, equipment_name, equipment_type, status "
            f"FROM public.mes_equipment WHERE site = '{site_code}';\n")  # nosec B608 - validated module constants


def _site_runs_view_sql(schema: str, site_code: str) -> str:
    """The ERP production order the MES pulls, for the runs THIS site's work
    orders execute: the run id (named as the work order's
    ``production_run_id``, so provision's b3 ``*_id`` heuristic joins the
    two), the run number the ERP records as ``shipment_items.batch_id``, then
    the part (``products.sku``; no product UUID), planned quantity, status,
    actual times and line. New columns are APPENDED only, so ``CREATE OR
    REPLACE VIEW`` succeeds over the original 2-column view (D18). Arguments
    are pre-validated by :func:`_site_mes_views_sql`."""
    return (f"CREATE OR REPLACE VIEW {schema}.production_runs AS SELECT "
            f"pr.run_id AS production_run_id, pr.run_number, p.sku, pr.planned_quantity, pr.status, "
            f"pr.actual_start_time, pr.actual_end_time, pr.line FROM public.production_runs pr "
            f"JOIN public.products p ON p.product_id = pr.product_id "
            f"WHERE pr.run_id IN (SELECT w.production_run_id FROM public.mes_work_orders w "
            f"WHERE w.site = '{site_code}');\n")  # nosec B608 - validated module constants


def erp_mes_views_sql() -> str:
    """The full, idempotent (``CREATE ... IF NOT EXISTS`` / ``OR REPLACE``) DDL batch.

    Raises ``ValueError`` on an identifier/literal that fails validation.
    Needs a db-init-built database (see the module docstring).
    """
    blocks = [_erp_data_views_sql()]
    blocks += [_site_mes_views_sql(*site) for site in SITE_MES_VIEWS]
    return "\n".join(blocks)
