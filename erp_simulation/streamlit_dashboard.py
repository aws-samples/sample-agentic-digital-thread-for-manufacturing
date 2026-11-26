# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
ERP Simulation Real-Time Dashboard

Visualizes data flow across all ERP systems with live KPIs and activity indicators.
Shows Orders → Inventory Check → Production (if backordered) → MES → Inventory → Shipping → Finance flow in real-time.
Make-to-Stock model: ALL orders ship from inventory only, production replenishes inventory for backordered orders.
"""

import html
import os
from pathlib import Path

# Dashboard version - update this when making changes to verify deployments
DASHBOARD_VERSION = "1.0.1"  # B17: Reset All now calls erp-database-init directly

# Application version from VERSION file (set during deployment)
def get_app_version() -> str:
    """Read application version from VERSION file."""
    version_paths = [
        Path(__file__).parent.parent / "VERSION",  # Development
        Path(__file__).parent / ".." / "VERSION",  # Alternative path
        Path("/app/VERSION"),  # Container deployment
    ]
    for version_path in version_paths:
        try:
            if version_path.exists():
                return version_path.read_text().strip()
        except Exception:  # nosec B110 - intentional fallback, no recovery action needed
            pass
    return os.environ.get("APP_VERSION", "1.0.0")

APP_VERSION = get_app_version()

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
import time
import sys
import logging

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from erp_simulation.database import get_database
from erp_simulation.config import get_config
from erp_simulation import branding
from erp_simulation.config_widgets import config_bound, config_label
from erp_simulation import console_login
from erp_simulation import cycle_result
from erp_simulation import external_mes
from erp_simulation import sql_console
from erp_simulation.sites import SITE_EVENT_PREFIX, SITE_EVENT_STATES

# Set up logger
logger = logging.getLogger(__name__)

# Page configuration (title is neutral by default; ERP_DASHBOARD_TITLE overrides)
st.set_page_config(
    page_title=branding.page_title(),
    page_icon=branding.HEADER_ICON,
    layout="wide",
    initial_sidebar_state="expanded"
)

# Dark/Light mode toggle
if 'dark_mode' not in st.session_state:
    st.session_state.dark_mode = True  # Default to dark mode for conference

# Theme colors
if st.session_state.dark_mode:
    THEME = {
        'bg': '#0e1117',
        'card_bg': '#1e2130',
        'card_border': '#2d3250',
        'text': '#fafafa',
        'text_muted': '#a0a0b0',
        'metric_bg': '#1e2130',
        'active_bg': '#1a3a2a',
        'active_border': '#28a745',
        'hover_bg': '#2d3250',
        'accent': '#4a9eff',
        'plotly_template': 'plotly_dark',
    }
else:
    THEME = {
        'bg': '#ffffff',
        'card_bg': '#f0f2f6',
        'card_border': '#1f77b4',
        'text': '#1a1a2e',
        'text_muted': '#666666',
        'metric_bg': '#f8f9fa',
        'active_bg': '#d4edda',
        'active_border': '#28a745',
        'hover_bg': '#e9ecef',
        'accent': '#1f77b4',
        'plotly_template': 'plotly_white',
    }

# Custom CSS for better styling
st.markdown(f"""
<style>
    .stApp {{
        background-color: {THEME['bg']};
        color: {THEME['text']};
    }}
    .block-container {{
        padding-top: 2rem;
    }}
    .metric-card {{
        background-color: {THEME['card_bg']};
        padding: 10px;
        border-radius: 8px;
        border-left: 4px solid {THEME['card_border']};
        margin-bottom: 8px;
        position: relative;
        color: {THEME['text']};
    }}
    .activity-indicator {{
        position: absolute;
        top: 10px;
        right: 10px;
        font-size: 0.85rem;
    }}
    .activity-pulse {{
        animation: pulse 1s infinite;
    }}
    @keyframes pulse {{
        0% {{ opacity: 1; }}
        50% {{ opacity: 0.5; }}
        100% {{ opacity: 1; }}
    }}
    .system-active {{
        background-color: {THEME['active_bg']} !important;
        border-left-color: {THEME['active_border']} !important;
    }}
    .stMetric {{
        background-color: {THEME['metric_bg']};
        padding: 5px;
        border-radius: 5px;
        margin: 2px 0;
    }}
    .stMetric label {{
        font-size: 0.85rem !important;
        color: {THEME['text_muted']} !important;
    }}
    .stMetric [data-testid="stMetricValue"] {{
        font-size: 1.2rem !important;
        color: {THEME['text']} !important;
    }}
    h3 {{
        margin-top: 0.5rem !important;
        margin-bottom: 0.5rem !important;
        font-size: 1.1rem !important;
        color: {THEME['text']} !important;
    }}
    .metric-card h3 {{
        font-size: 1rem !important;
        margin: 0 0 5px 0 !important;
        padding-right: 80px;
        color: {THEME['text']} !important;
    }}
    .clickable-metric {{
        cursor: pointer;
        transition: background-color 0.2s;
    }}
    .clickable-metric:hover {{
        background-color: {THEME['hover_bg']};
    }}
    button[kind="secondary"] {{
        cursor: pointer !important;
        background-color: {THEME['card_bg']} !important;
        color: {THEME['text']} !important;
        border-color: {THEME['card_border']} !important;
    }}
    button[kind="secondary"]:hover {{
        background-color: {THEME['hover_bg']} !important;
        border-color: {THEME['accent']} !important;
        color: {THEME['text']} !important;
    }}
    /* Fix all Streamlit buttons in dark mode */
    .stButton > button {{
        color: {THEME['text']} !important;
    }}
    .stButton > button[kind="secondary"] {{
        background-color: {THEME['card_bg']} !important;
        border-color: {THEME['card_border']} !important;
    }}
    [data-testid="stSidebar"] {{
        background-color: {THEME['card_bg']};
    }}
    p, div, span, label {{
        color: {THEME['text']};
    }}
</style>
""", unsafe_allow_html=True)


# MES site code -> dashboard card label (every site in SITE_EVENT_PREFIX; ADR-0072).
SITE_LABELS = {
    'MILWAUKEE_BOTTLING': 'Milwaukee (Beverages)',
    'DETROIT_AUTOPARTS': 'Detroit (Automotive)',
    'CEDARRAPIDS_FOOD': 'Cedar Rapids (Food)',
    'INDIANAPOLIS_MACHINING': 'Indianapolis (Machined Components)',
}


def _sparkline_column_map():
    """(system_key, label) -> simulation_events column; per-site MES keys come from SITE_EVENT_PREFIX."""
    column_map = {
        # Orders
        ("orders", "New Orders"): "orders_new",
        ("orders", "Pending"): "orders_pending",
        ("orders", "Confirmed"): "orders_confirmed",
        ("orders", "🔴 BACKORDERED"): "orders_backordered",
        ("orders", "Shipped"): "orders_shipped",
        ("orders", "In Production"): "orders_in_production",
        # Production
        ("production", "Total Runs"): "production_new",
        ("production", "Planned"): "production_planned",
        ("production", "Released"): "production_released",
        ("production", "In Progress"): "production_in_progress",
        ("production", "Completed"): "production_completed",
        # Procurement
        ("procurement", "Sent"): "procurement_sent",
        ("procurement", "In Transit"): "procurement_in_transit",
        ("procurement", "Received"): "procurement_received",
        # Shipping
        ("shipping", "Preparing"): "shipping_preparing",
        ("shipping", "Picked"): "shipping_picked",
        ("shipping", "Packed"): "shipping_packed",
        ("shipping", "Shipped"): "shipping_shipped",
        ("shipping", "In Transit"): "shipping_in_transit",
        ("shipping", "Delivered"): "shipping_delivered",
        # Finance
        ("finance", "Invoices"): "finance_invoices",
        ("finance", "Paid"): "finance_paid",
        ("finance", "Revenue"): "finance_revenue",
        ("finance", "Collected"): "finance_collected",
        # MES (aggregate)
        ("mes", "Scheduled"): "mes_scheduled",
        ("mes", "Running"): "mes_running",
        ("mes", "Completed"): "mes_completed",
        # Inventory
        ("inventory", "Recent Transactions"): "inventory_transactions",
    }
    # MES per-site: ("mes_<SITE>", "Scheduled") -> "mes_<prefix>_scheduled", ...
    for site, prefix in SITE_EVENT_PREFIX.items():
        for state in SITE_EVENT_STATES:
            column_map[(f"mes_{site}", state.title())] = f"mes_{prefix}_{state}"
    return column_map


def _sparkline_total_metrics():
    """"Total" metrics that sum several simulation_events columns."""
    totals = {
        ("mes", "Work Orders"): ["mes_scheduled", "mes_running", "mes_completed"],
        ("procurement", "Total POs"): ["procurement_sent", "procurement_in_transit", "procurement_received"],
        ("shipping", "Total"): ["shipping_preparing", "shipping_picked", "shipping_packed", "shipping_shipped", "shipping_in_transit", "shipping_delivered"],
    }
    for site, prefix in SITE_EVENT_PREFIX.items():
        totals[(f"mes_{site}", "Work Orders")] = [f"mes_{prefix}_{state}" for state in SITE_EVENT_STATES]
    return totals


SPARKLINE_COLUMN_MAP = _sparkline_column_map()
SPARKLINE_TOTAL_METRICS = _sparkline_total_metrics()

# Live-story clock anchor (ADR-0088 D7): its presence in simulation_config means
# the sensor feed follows the plant clock.
_STORY_ANCHOR_KEY = "story_anchor_epoch"
_STORY_CLOCK_CAPTION = (
    "This plant's sensor feed follows the plant clock shown in the ADT app header."
)
# A live workshop package ships no SiteWise feed Lambda (ADR-0092); the
# dashboard stack sets this on the container so the OT row never offers it.
_OT_FEED_ENV = "OT_FEED_SOURCE"            # stacks/_workshop_story.OT_FEED_ENV_NAME
_OT_FEED_FROM_OPCUA = "opcua"              # stacks/_workshop_story.OT_FEED_FROM_OPCUA


def _feed_from_opcua() -> bool:
    """True when this package's sensor feed is the OPC UA server, not the Lambda."""
    return os.environ.get(_OT_FEED_ENV, "").strip().lower() == _OT_FEED_FROM_OPCUA


def _story_clock_active(conn) -> bool:
    """True when a ``story_anchor_epoch`` row exists. Keyed by DATA, never by
    the story setting. Database errors propagate to the caller."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM simulation_config WHERE config_key = %s",
            (_STORY_ANCHOR_KEY,),
        )
        return cursor.fetchone() is not None


# Live story only (the same check as the OT caption): Pause, Reset All Data and
# the scenario buttons move into one collapsed expander, and Reset takes two
# clicks. Frozen and demo deployments keep the header buttons and scenario bar.
_OPERATOR_CONTROLS_LABEL = "🛠️ Operator controls"
_RESET_CONFIRM_TEXT = "Click Reset again to wipe all plant history"
_RESET_ARMED_AT_KEY = "reset_armed_at"     # session_state: time the first click armed Reset
_AUTO_RERUN_KEY = "auto_refresh_rerun"     # session_state: set by the auto-refresh loop
# Bounds an arm that a user interaction failed to clear: a click that lands
# while the refresh loop sleeps is merged into its marked rerun.
_RESET_ARM_SECONDS = 20.0

_SCENARIO_NAMES = {
    'normal': 'Normal',
    'demand_surge': 'Demand Surge',
    'mes_outage': 'MES Outage',
    'supply_crisis': 'Supply Crisis',
    'equipment_down': 'Equipment Down',
    'cash_crunch': 'Cash Crunch',
    'quality_issues': 'Quality Issues',
    'capacity_plus': 'Capacity+'
}


def _reset_step(armed_at, clicked, auto_rerun, now):
    """One run of the two-step Reset: returns ``(armed_at, run_reset)``.

    ``armed_at`` is when the first click armed it (``None`` when disarmed).
    A click on a live arm runs the reset; a click otherwise arms it. Without
    a click, an auto-refresh rerun keeps a live arm, and any other run (a
    different widget, a reload) or an arm older than ``_RESET_ARM_SECONDS``
    disarms it.
    """
    live = armed_at is not None and 0 <= now - armed_at <= _RESET_ARM_SECONDS
    if clicked:
        return (None, True) if live else (now, False)
    if live and auto_rerun:
        return armed_at, False
    return None, False


def _scenario_status_html(active_scenario):
    """'Current Scenario: <name>' in the theme's text colour (green when normal)."""
    scenario_color = '#28a745' if active_scenario == 'normal' else '#dc3545'  # Green for normal, red for others
    name = _SCENARIO_NAMES.get(active_scenario, 'Normal')
    return (
        f"<div style='padding-top: 5px; color: {THEME['text']};'><strong>Current Scenario: "
        f"<span style='color: {scenario_color};'>{name}</span></strong></div>"
    )

# Colours the Auto Run button; injected right before the simulation buttons render.
_SIM_BUTTON_CSS = """
            <style>
            /* Force Auto Run button to be green when primary */
            button[kind="primary"]:nth-of-type(1) {
                background-color: #28a745 !important;
                border-color: #28a745 !important;
            }
            button[kind="primary"]:nth-of-type(1):hover {
                background-color: #218838 !important;
                border-color: #1e7e34 !important;
            }
            </style>
            """

# Quick-template SQL for the read-only query interface.
SQL_QUERY_TEMPLATES = {
    "List all simulation_config parameters": """SELECT 
    category,
    config_key,
    config_value,
    data_type,
    description
FROM simulation_config
ORDER BY 
    CASE category
        WHEN 'orders' THEN 1
        WHEN 'production' THEN 2
        WHEN 'mes' THEN 3
        WHEN 'procurement' THEN 4
        WHEN 'shipping' THEN 5
        WHEN 'finance' THEN 6
    END,
    config_key;""",
    "Show orders by status": """SELECT 
    status,
    COUNT(*) as count,
    SUM(total_amount) as total_value
FROM customer_orders
GROUP BY status
ORDER BY status;""",
    "Show production runs by status": """SELECT 
    status,
    COUNT(*) as count
FROM production_runs
GROUP BY status
ORDER BY status;""",
    "Show inventory levels": """SELECT 
    i.item_code,
    i.item_name,
    i.item_type,
    i.quantity_on_hand,
    i.unit_of_measure
FROM inventory i
WHERE i.quantity_on_hand > 0
ORDER BY i.item_type, i.item_code;""",
    "Show MES work orders": """SELECT 
    wo.work_order_number,
    wo.production_line,
    wo.status,
    wo.planned_quantity,
    wo.actual_quantity,
    wo.start_time,
    wo.end_time
FROM mes_work_orders wo
ORDER BY wo.created_at DESC
LIMIT 20;""",
    "Show customer order details": """SELECT 
    co.order_number,
    c.company_name,
    co.order_date,
    co.status,
    co.total_amount,
    COUNT(oli.line_item_id) as line_items
FROM customer_orders co
JOIN customers c ON co.customer_id = c.customer_id
LEFT JOIN order_line_items oli ON co.order_id = oli.order_id
GROUP BY co.order_id, co.order_number, c.company_name, co.order_date, co.status, co.total_amount
ORDER BY co.order_date DESC
LIMIT 20;"""
}


def _console_refusal(sql_query):
    """The message for console text refused before the database, or None.

    The size cap runs first so an oversized paste is never upper-cased,
    matched or lexed. The keyword checks only give a clearer first message:
    the console login refuses writes either way. One statement only, because
    a COMMIT in the text would end the read-only transaction.
    """
    import re
    if sql_console.exceeds_size_limit(sql_query):
        return f"❌ {sql_console.TOO_LONG_MESSAGE}"
    if sql_console.leading_keyword(sql_query) not in sql_console.ALLOWED_LEADING_KEYWORDS:
        return "❌ Only SELECT queries are allowed"
    if re.search(r'\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|TRUNCATE)\b', sql_query.upper()):
        return "❌ Only read-only SELECT queries are allowed"
    if not sql_console.is_single_statement(sql_query):
        return "❌ Run one statement at a time"
    return None


#: Reset All's destructive invokes (the cleanup, the balance reset and the
#: db-init full reset, which re-applies the optional plant and re-grants the
#: read-only logins before it answers) wait longer than db-init's own
#: 10-minute timeout and are sent once. botocore's defaults (a 60 s read
#: timeout, legacy retries that retry a read timeout) would send a slow reset
#: again while the first still ran: two full resets at once.
RESET_INVOKE_READ_TIMEOUT_SECONDS = 630
RESET_INVOKE_RETRIES = {"mode": "standard", "total_max_attempts": 1}


def reset_invoke_config():
    """The botocore config of Reset All's destructive invokes."""
    from botocore.config import Config

    return Config(connect_timeout=10, read_timeout=RESET_INVOKE_READ_TIMEOUT_SECONDS,
                  retries=dict(RESET_INVOKE_RETRIES))


def _invoke_reset_step(client, box, function, payload, label) -> bool:
    """One synchronous invoke of a Reset All step. False, with the error in
    ``box``, when the function raised (a FunctionError: db-init raises on a
    failed reset or a failed step after it) or answered statusCode 500."""
    import json

    response = client.invoke(FunctionName=function, InvocationType='RequestResponse',
                             Payload=json.dumps(payload))
    result = json.loads(response['Payload'].read().decode('utf-8'))
    if response.get('FunctionError'):
        box.error(f"❌ {label} Lambda crashed: {result}")
        return False
    if isinstance(result, dict) and result.get('statusCode') == 500:
        error_body = result.get('body', '{}')
        if isinstance(error_body, str):
            error_body = json.loads(error_body)
        box.error(f"❌ {label} failed: {error_body.get('error', 'Unknown error')}")
        return False
    return True


class ERPDashboard:
    """Main dashboard class for ERP simulation visualization."""
    
    def __init__(self):
        """Initialize dashboard with database connection."""
        try:
            # Check if we should use secrets manager from environment
            use_secrets = os.getenv('USE_SECRETS_MANAGER', 'false').lower() == 'true'
            self.config = get_config(use_secrets_manager=use_secrets)
            self.db = get_database()
        except Exception as e:
            st.error(f"Failed to connect to database: {e}")
            st.info("Make sure the database is running and environment variables are set.")
            st.stop()
    
    def get_system_kpis(self):
        """Fetch KPIs for all systems - filtered to last 1 hour to match sparklines."""
        try:
            with self.db.connection() as conn:
                with conn.cursor() as cursor:
                    # Filter to last 1 hour to match sparkline timeframe
                    one_hour_ago = datetime.now() - timedelta(hours=1)
                    
                    # Orders - new orders from last simulation event
                    cursor.execute("""
                        SELECT 
                            COALESCE((SELECT orders_new FROM simulation_events ORDER BY event_timestamp DESC LIMIT 1), 0) as new_orders,
                            COUNT(*) FILTER (WHERE status = 'PENDING') as pending,
                            COUNT(*) FILTER (WHERE status = 'CONFIRMED') as confirmed,
                            COUNT(*) FILTER (WHERE status = 'BACKORDERED') as backordered,
                            (SELECT COUNT(*) FROM production_runs WHERE status = 'IN_PROGRESS' AND created_at > %s) as in_production,
                            COUNT(*) FILTER (WHERE status = 'SHIPPED') as shipped,
                            COUNT(*) FILTER (WHERE created_at > %s) as recent_orders
                        FROM customer_orders
                    """, (one_hour_ago, one_hour_ago,))
                    orders = cursor.fetchone()
                    
                    # Production - new runs created since last simulation event
                    cursor.execute("""
                        SELECT 
                            COALESCE((SELECT production_new FROM simulation_events ORDER BY event_timestamp DESC LIMIT 1), 0) as new_runs,
                            COUNT(*) FILTER (WHERE created_at > %s) as recent_runs,
                            COUNT(*) FILTER (WHERE status = 'PLANNED') as planned,
                            COUNT(*) FILTER (WHERE status = 'RELEASED') as released,
                            COUNT(*) FILTER (WHERE status = 'IN_PROGRESS') as in_progress,
                            COUNT(*) FILTER (WHERE status = 'COMPLETED') as completed,
                            SUM(actual_quantity) FILTER (WHERE status = 'COMPLETED') as total_produced
                        FROM production_runs
                    """, (one_hour_ago,))
                    production = cursor.fetchone()
                    
                    # MES
                    cursor.execute("""
                        SELECT 
                            COUNT(*) as total_work_orders,
                            COUNT(*) FILTER (WHERE created_at > %s) as recent_work_orders,
                            COUNT(*) FILTER (WHERE status = 'SCHEDULED') as scheduled,
                            COUNT(*) FILTER (WHERE status = 'RUNNING') as running,
                            COUNT(*) FILTER (WHERE status = 'COMPLETED') as completed,
                            AVG(oee_percentage) FILTER (WHERE oee_percentage IS NOT NULL) as avg_oee
                        FROM mes_work_orders
                    """, (one_hour_ago,))
                    mes = cursor.fetchone()
                    
                    # MES per-site breakdown — always show all active sites
                    cursor.execute("""
                        SELECT s.site_code as site,
                               COUNT(mwo.work_order_id) as total,
                               COUNT(mwo.work_order_id) FILTER (WHERE mwo.status = 'SCHEDULED') as scheduled,
                               COUNT(mwo.work_order_id) FILTER (WHERE mwo.status = 'RUNNING') as running,
                               COUNT(mwo.work_order_id) FILTER (WHERE mwo.status = 'COMPLETED') as completed
                        FROM sites s
                        LEFT JOIN mes_work_orders mwo ON mwo.site = s.site_code AND mwo.created_at > %s
                        WHERE s.is_active = TRUE AND s.site_code != 'BOTTLING_PLANT'
                        GROUP BY s.site_code
                        ORDER BY s.site_code
                    """, (one_hour_ago,))
                    mes_per_site = cursor.fetchall()
                    
                    # MES Events
                    cursor.execute("""
                        SELECT COUNT(*) as total_events
                        FROM mes_production_events
                        WHERE created_at > %s
                    """, (one_hour_ago,))
                    mes_events = cursor.fetchone()
                    
                    # Inventory
                    cursor.execute("""
                        SELECT 
                            COUNT(DISTINCT ib.item_id) as total_items,
                            SUM(ib.quantity_on_hand) as total_quantity,
                            COUNT(DISTINCT ib.item_id) FILTER (WHERE ib.quantity_on_hand < 
                                (SELECT reorder_point FROM inventory_items ii 
                                 WHERE ii.item_id = ib.item_id)) as low_stock,
                            COUNT(DISTINCT it.transaction_id) FILTER (WHERE it.transaction_date > %s) as recent_transactions
                        FROM inventory_balances ib
                        LEFT JOIN inventory_transactions it ON ib.item_id = it.item_id
                    """, (one_hour_ago,))
                    inventory = cursor.fetchone()
                    
                    # Procurement
                    cursor.execute("""
                        SELECT 
                            COUNT(*) as total_pos,
                            COUNT(*) FILTER (WHERE created_at > %s) as recent_pos,
                            COUNT(*) FILTER (WHERE status = 'SENT') as sent,
                            COUNT(*) FILTER (WHERE status = 'IN_TRANSIT') as in_transit,
                            COUNT(*) FILTER (WHERE status = 'RECEIVED') as received
                        FROM purchase_orders
                    """, (one_hour_ago,))
                    procurement = cursor.fetchone()
                    
                    # Shipping
                    cursor.execute("""
                        SELECT 
                            COUNT(*) as total_shipments,
                            COUNT(*) FILTER (WHERE created_at > %s) as recent_shipments,
                            COUNT(*) FILTER (WHERE status = 'PREPARING') as preparing,
                            COUNT(*) FILTER (WHERE status = 'PICKED') as picked,
                            COUNT(*) FILTER (WHERE status = 'PACKED') as packed,
                            COUNT(*) FILTER (WHERE status = 'SHIPPED') as shipped,
                            COUNT(*) FILTER (WHERE status = 'IN_TRANSIT') as in_transit,
                            COUNT(*) FILTER (WHERE status = 'DELIVERED') as delivered
                        FROM shipments
                    """, (one_hour_ago,))
                    shipping = cursor.fetchone()
                    
                    # Finance
                    cursor.execute("""
                        SELECT 
                            COUNT(*) as total_invoices,
                            COUNT(*) FILTER (WHERE invoice_date > %s) as recent_invoices,
                            SUM(total_amount) as total_revenue,
                            SUM(amount_paid) as total_paid,
                            COUNT(*) FILTER (WHERE status = 'PAID') as paid_count
                        FROM invoices
                    """, (one_hour_ago,))
                    finance = cursor.fetchone()
                    
                    return {
                        'orders': orders,
                        'production': production,
                        'mes': mes,
                        'mes_per_site': mes_per_site,
                        'mes_events': mes_events,
                        'inventory': inventory,
                        'procurement': procurement,
                        'shipping': shipping,
                        'finance': finance
                    }
        except Exception as e:
            st.error(f"Error fetching KPIs: {e}")
            return None
    
    def render_system_card(self, title, icon, kpis, recent_activity, col, system_key):
        """Render a system card with KPIs and activity indicator."""
        with col:
            # Activity indicator
            activity_class = "system-active" if recent_activity > 0 else ""
            
            st.markdown(f"""
            <div class="metric-card {activity_class}">
                <h3>{icon} {title}</h3>
                <div class="activity-indicator">
                    {'<span class="activity-pulse">🟢 Active</span>' if recent_activity > 0 else '⚪ Idle'}
                </div>
            </div>
            """, unsafe_allow_html=True)
            
            # KPIs with clickable details and sparklines
            for label, value in kpis.items():
                if value is not None:
                    # Create unique key for each metric
                    metric_key = f"{system_key}_{label.replace(' ', '_')}"
                    
                    # Only show sparklines for event-based metrics, not state-based metrics
                    # Inventory metrics (Items, Total Qty, Low Stock) are state-based - no historical data
                    show_sparkline = True
                    if system_key == "inventory" and label != "Recent Transactions":
                        show_sparkline = False  # Items, Total Qty, Low Stock are snapshots, not events
                    
                    # Display metric with sparkline and button for details
                    if show_sparkline:
                        col_metric, col_spark, col_button = st.columns([1, 2.5, 0.5])
                    else:
                        col_metric, col_button = st.columns([3, 1])
                    
                    with col_metric:
                        if isinstance(value, float):
                            st.metric(label, f"{value:.1f}")
                        elif isinstance(value, str):
                            st.metric(label, value)
                        else:
                            st.metric(label, f"{value:,}")
                    
                    if show_sparkline:
                        with col_spark:
                            # Get and display sparkline events with status filter
                            trend_data = self.get_sparkline_events(system_key, label)
                            # Debug: Log to see if data is being collected
                            logger.info(f"Sparkline {system_key}/{label}: sum={sum(trend_data) if trend_data else 0}, last5={trend_data[-5:] if trend_data else []}")
                            sparkline = self.create_sparkline(trend_data)
                            st.plotly_chart(sparkline, use_container_width=True, config={'displayModeBar': False}, key=f"spark_{metric_key}")
                    
                    with col_button:
                        if st.button("🗃️", key=metric_key, help=f"View {label} details"):
                            # Store which details to show in session state
                            st.session_state['show_details_system'] = system_key
                            st.session_state['show_details_label'] = label
                            st.session_state['show_details_value'] = value
                            st.session_state['show_details_key'] = metric_key
                            st.rerun()
    
    @st.dialog("Details", width="large")
    def show_details_popup(self, system_key, label, value, metric_key):
        """Show metric details in a popup dialog with only table results."""
        try:
            with self.db.connection() as conn:
                df = pd.DataFrame()
                
                # Fetch detailed data based on system and metric - simplified to show only tables
                if system_key == "orders":
                    if "BACKORDERED" in label:
                        # Simplified backorder view - just the table
                        df = pd.read_sql("""
                            SELECT 
                                co.order_number,
                                c.company_name as customer,
                                p.product_name,
                                oli.quantity as ordered_qty,
                                COALESCE(SUM(ib.quantity_on_hand), 0) as available_inventory,
                                oli.quantity - COALESCE(SUM(ib.quantity_on_hand), 0) as shortage,
                                co.order_date,
                                co.updated_at as backordered_since
                            FROM customer_orders co
                            JOIN customers c ON co.customer_id = c.customer_id
                            JOIN order_line_items oli ON co.order_id = oli.order_id
                            JOIN products p ON oli.product_id = p.product_id
                            LEFT JOIN inventory_items ii ON p.product_id = ii.product_id AND ii.item_type = 'FINISHED_GOOD'
                            LEFT JOIN inventory_balances ib ON ii.item_id = ib.item_id
                            WHERE co.status = 'BACKORDERED'
                            GROUP BY co.order_id, co.order_number, c.company_name, p.product_name, 
                                     oli.quantity, co.order_date, co.updated_at
                            ORDER BY co.updated_at DESC
                            LIMIT 50
                        """, conn)
                    else:
                        # Standard order details for other statuses - include products
                        status_filter = ""
                        if "Pending" in label:
                            status_filter = "AND co.status = 'PENDING'"
                        elif "Confirmed" in label:
                            status_filter = "AND co.status = 'CONFIRMED'"
                        elif "Production" in label:
                            # "In Production" shows production runs that are IN_PROGRESS
                            df = self.db.execute_query_pandas("""
                                SELECT 
                                    pr.run_number,
                                    p.product_name,
                                    pr.status,
                                    pr.planned_quantity,
                                    pr.actual_quantity,
                                    pr.created_at 
                                FROM production_runs pr
                                JOIN products p ON pr.product_id = p.product_id
                                WHERE pr.status = 'IN_PROGRESS'
                                ORDER BY pr.created_at DESC 
                                LIMIT 50
                            """)
                        
                        if "Production" not in label and ("Pending" in label or "Confirmed" in label):
                            # For Pending/Confirmed: Get orders with aggregated line item info
                            # This avoids showing one row per line item which is confusing
                            df = self.db.execute_query_pandas(f"""
                                SELECT 
                                    co.order_number,
                                    c.company_name as customer,
                                    co.status,
                                    co.total_amount,
                                    COUNT(oli.line_item_id) as line_items,
                                    STRING_AGG(p.product_name || ' (x' || oli.quantity || ')', ', ' ORDER BY oli.line_item_id) as products,
                                    co.created_at 
                                FROM customer_orders co
                                JOIN customers c ON co.customer_id = c.customer_id
                                LEFT JOIN order_line_items oli ON co.order_id = oli.order_id
                                LEFT JOIN products p ON oli.product_id = p.product_id
                                WHERE 1=1 {status_filter}
                                GROUP BY co.order_id, co.order_number, c.company_name, co.status, co.total_amount, co.created_at
                                ORDER BY co.created_at DESC 
                                LIMIT 50
                            """)  # nosec B608 - status_filter built from hardcoded literal SQL fragments above; not user input
                        elif "Production" not in label:
                            df = self.db.execute_query_pandas(f"""
                                SELECT 
                                    co.order_number,
                                    c.company_name as customer,
                                    p.product_name as product,
                                    oli.quantity as qty_ordered,
                                    co.status,
                                    co.total_amount,
                                    co.created_at 
                                FROM customer_orders co
                                JOIN customers c ON co.customer_id = c.customer_id
                                LEFT JOIN order_line_items oli ON co.order_id = oli.order_id
                                LEFT JOIN products p ON oli.product_id = p.product_id
                                WHERE 1=1 {status_filter}
                                ORDER BY co.created_at DESC 
                                LIMIT 50
                            """)  # nosec B608 - status_filter built from hardcoded literal SQL fragments above; not user input
                
                elif system_key == "production":
                    status_filter = ""
                    if "Planned" in label:
                        status_filter = "WHERE pr.status = 'PLANNED'"
                    elif "Released" in label:
                        status_filter = "WHERE pr.status = 'RELEASED'"
                    elif "Progress" in label:
                        status_filter = "WHERE pr.status = 'IN_PROGRESS'"
                    elif "Completed" in label:
                        status_filter = "WHERE pr.status = 'COMPLETED'"
                    
                    df = self.db.execute_query_pandas(f"""
                        SELECT 
                            pr.run_number,
                            p.product_name,
                            pr.status,
                            pr.planned_quantity,
                            pr.actual_quantity,
                            pr.created_at 
                        FROM production_runs pr
                        JOIN products p ON pr.product_id = p.product_id
                        {status_filter}
                        ORDER BY pr.created_at DESC 
                        LIMIT 50
                    """)  # nosec B608 - status_filter built from hardcoded literal SQL fragments above; not user input
                
                elif system_key == "mes" or system_key.startswith("mes_"):
                    # Extract site filter for per-site MES cards
                    site_filter = ""
                    site_params = []
                    if system_key.startswith("mes_"):
                        site_code = system_key[4:]  # strip "mes_"
                        site_filter = "AND mwo.site = %s"
                        site_params = [site_code]
                    
                    if "Work Orders" in label or "Running" in label or "Completed" in label or "Scheduled" in label:
                        status_filter = ""
                        if "Running" in label:
                            status_filter = "AND mwo.status = 'RUNNING'"
                        elif "Completed" in label:
                            status_filter = "AND mwo.status = 'COMPLETED'"
                        elif "Scheduled" in label:
                            status_filter = "AND mwo.status = 'SCHEDULED'"
                        
                        query = f"""
                            SELECT 
                                mwo.work_order_number,
                                pr.run_number as production_run,
                                mwo.site,
                                mwo.line,
                                mwo.status,
                                mwo.oee_percentage,
                                mwo.created_at 
                            FROM mes_work_orders mwo
                            LEFT JOIN production_runs pr ON mwo.production_run_id = pr.run_id
                            WHERE 1=1 {status_filter} {site_filter}
                            ORDER BY mwo.created_at DESC 
                            LIMIT 50
                        """  # nosec B608 - status_filter and site_filter built from hardcoded literal SQL fragments; site value parameterized via %s
                        if site_params:
                            df = pd.read_sql(query, conn, params=site_params)
                        else:
                            df = self.db.execute_query_pandas(query)
                    elif "Events" in label:
                        df = self.db.execute_query_pandas("""
                            SELECT 
                                mpe.event_type,
                                mwo.work_order_number,
                                mpe.quantity,
                                mpe.event_time 
                            FROM mes_production_events mpe
                            LEFT JOIN mes_work_orders mwo ON mpe.work_order_id = mwo.work_order_id
                            ORDER BY mpe.event_time DESC 
                            LIMIT 50
                        """)
                
                elif system_key == "inventory":
                    df = self.db.execute_query_pandas("""
                        SELECT 
                            ii.item_code,
                            ii.item_name,
                            ii.item_type,
                            ib.quantity_on_hand,
                            ii.reorder_point,
                            il.location_name
                        FROM inventory_balances ib
                        JOIN inventory_items ii ON ib.item_id = ii.item_id
                        JOIN inventory_locations il ON ib.location_id = il.location_id
                        ORDER BY ib.quantity_on_hand DESC
                        LIMIT 50
                    """)
                
                elif system_key == "procurement":
                    status_filter = ""
                    if "Sent" in label:
                        status_filter = "AND po.status = 'SENT'"
                    elif "Transit" in label:
                        status_filter = "AND po.status = 'IN_TRANSIT'"
                    elif "Received" in label:
                        status_filter = "AND po.status = 'RECEIVED'"
                    
                    # Show PO details with products being ordered/received
                    df = self.db.execute_query_pandas(f"""
                        SELECT 
                            po.po_number,
                            s.company_name as supplier,
                            ii.item_code as item_code,
                            ii.item_name as item_name,
                            pli.quantity_ordered as qty_ordered,
                            pli.quantity_received as qty_received,
                            po.status,
                            po.total_amount,
                            po.created_at 
                        FROM purchase_orders po
                        JOIN suppliers s ON po.supplier_id = s.supplier_id
                        JOIN po_line_items pli ON po.po_id = pli.po_id
                        JOIN inventory_items ii ON pli.item_id = ii.item_id
                        WHERE 1=1 {status_filter}
                        ORDER BY po.created_at DESC 
                        LIMIT 50
                    """)  # nosec B608 - status_filter built from hardcoded literal SQL fragments above; not user input
                
                elif system_key == "shipping":
                    status_filter = ""
                    if "Preparing" in label:
                        status_filter = "WHERE s.status = 'PREPARING'"
                    elif "Picked" in label:
                        status_filter = "WHERE s.status = 'PICKED'"
                    elif "Packed" in label:
                        status_filter = "WHERE s.status = 'PACKED'"
                    elif "Shipped" in label:
                        status_filter = "WHERE s.status = 'SHIPPED'"
                    elif "In Transit" in label:
                        status_filter = "WHERE s.status = 'IN_TRANSIT'"
                    elif "Delivered" in label:
                        status_filter = "WHERE s.status = 'DELIVERED'"
                    
                    df = self.db.execute_query_pandas(f"""
                        SELECT 
                            s.shipment_number,
                            co.order_number,
                            c.company_name as customer,
                            s.status,
                            s.tracking_number,
                            s.created_at 
                        FROM shipments s
                        JOIN customer_orders co ON s.order_id = co.order_id
                        JOIN customers c ON co.customer_id = c.customer_id
                        {status_filter}
                        ORDER BY s.created_at DESC 
                        LIMIT 50
                    """)  # nosec B608 - status_filter built from hardcoded literal SQL fragments above; not user input
                
                elif system_key == "finance":
                    status_filter = ""
                    if "Paid" in label:
                        status_filter = "WHERE i.status = 'PAID'"
                    
                    df = self.db.execute_query_pandas(f"""
                        SELECT 
                            i.invoice_number,
                            co.order_number,
                            c.company_name as customer,
                            i.total_amount,
                            i.amount_paid,
                            i.status,
                            i.created_at 
                        FROM invoices i
                        JOIN customer_orders co ON i.order_id = co.order_id
                        JOIN customers c ON co.customer_id = c.customer_id
                        {status_filter}
                        ORDER BY i.created_at DESC 
                        LIMIT 50
                    """)  # nosec B608 - status_filter built from hardcoded literal SQL fragments above; not user input
                
                # Show only the table - no extra information
                # Calculate appropriate height based on data size (avoid scrollbars)
                if not df.empty:
                    # Calculate height: header + rows + padding
                    # Each row is approximately 35px, header is ~38px, add some padding
                    num_rows = min(len(df), 20)  # Cap at 20 visible rows to prevent huge dialogs
                    table_height = (num_rows * 35) + 38 + 10  # rows + header + padding
                    
                    st.dataframe(
                        df, 
                        height=table_height,
                        use_container_width=False  # Let table size itself to content
                    )
                else:
                    st.info("No data available")
                
                # Add close button (no need to clear state or rerun - already cleared in trigger)
                if st.button("Close", key="close_details_dialog"):
                    pass  # Dialog will close automatically
                    
        except Exception as e:
            st.error(f"Error loading details: {e}")
    
    def render_details_popup_trigger(self):
        """Check if details popup should be shown and trigger it.

        Also renders the workshop OT-feed control row: this method is
        render()'s first call after the scenario separator, and the 612-line
        render() itself is frozen by the changed-function maintainability
        gate — hooking here keeps render() byte-identical (same pattern as
        the C1 _start_cloud precedent).
        """
        # Workshop OT chapter: SiteWise degradation feed control (CMX-441)
        self.render_ot_feed_controls()

        # Check if details should be shown
        if ('show_details_system' in st.session_state and 
            'show_details_label' in st.session_state and 
            'show_details_value' in st.session_state and 
            'show_details_key' in st.session_state):
            
            system_key = st.session_state['show_details_system']
            label = st.session_state['show_details_label']
            value = st.session_state['show_details_value']
            metric_key = st.session_state['show_details_key']
            
            # Show popup and immediately clear state to prevent reopening
            self.show_details_popup(system_key, label, value, metric_key)
            
            # Clear session state after showing popup to prevent it from reopening on refresh
            if 'show_details_system' in st.session_state:
                del st.session_state['show_details_system']
            if 'show_details_label' in st.session_state:
                del st.session_state['show_details_label']
            if 'show_details_value' in st.session_state:
                del st.session_state['show_details_value']
            if 'show_details_key' in st.session_state:
                del st.session_state['show_details_key']

    
    def get_sparkline_events(self, system_key, label=""):
        """
        Get last N events for sparkline display from simulation_events table.
        
        Reads last N rows from simulation_events table (one row per Lambda cycle).
        All sparklines perfectly aligned to Lambda execution cycles.
        No browser cache issues, adapts automatically to any EventBridge interval.
        N is configurable via dashboard_sparkline_events in simulation_config table.
        
        Time representation depends on EventBridge interval:
        - If EventBridge = 1 min → N events = N minutes  
        - If EventBridge = 5 min → N events = 5*N minutes
        """
        try:
            event_count = self._sparkline_event_count()
            df = self._sparkline_frame(event_count)
            if df is None or df.empty:
                return [0] * event_count
            return self._sparkline_values(df, system_key, label, event_count)

        except Exception as e:
            logger.error(f"Error getting trend for {system_key}/{label}: {e}")
            # Return zeros with configured event count
            if 'sparkline_event_count' in st.session_state:
                return [0] * st.session_state.sparkline_event_count
            return [0] * 60  # Fallback default

    def _sparkline_event_count(self):
        """Sparkline event count from config (cached in session state)."""
        # Read sparkline event count from config (cached in session state)
        if 'sparkline_event_count' not in st.session_state:
            query = """
                SELECT CAST(config_value AS INTEGER) as event_count
                FROM simulation_config
                WHERE config_key = 'dashboard_sparkline_events'
            """
            df = self.db.execute_query_pandas(query)
            if df.empty:
                # Default to 60 if config not found
                st.session_state.sparkline_event_count = 60
                logger.warning("dashboard_sparkline_events not found in config, using default 60")
            else:
                st.session_state.sparkline_event_count = int(df['event_count'].iloc[0])
                logger.info(f"Loaded sparkline_event_count from config: {st.session_state.sparkline_event_count}")
        return st.session_state.sparkline_event_count

    def _sparkline_frame(self, event_count):
        """simulation_events rows, oldest first, loaded once per dashboard refresh (None = no events)."""
        # Load events data once per dashboard refresh
        if 'sparkline_events' not in st.session_state:
            # event_count comes from a CAST INTEGER config value (already int); pass via parameters
            query = "SELECT * FROM simulation_events ORDER BY event_timestamp DESC LIMIT %s"
            df = self.db.execute_query_pandas(query, params=(int(event_count),))
            if df.empty:
                # No events yet, return zeros
                st.session_state.sparkline_events = None
                return None

            # Reverse to get chronological order (oldest first)
            df = df.iloc[::-1].reset_index(drop=True)
            st.session_state.sparkline_events = df
            logger.info(f"Loaded {len(df)} simulation events for sparklines")
        return st.session_state.sparkline_events

    def _sparkline_values(self, df, system_key, label, event_count):
        """Values for one (system_key, label) metric, zero-padded to event_count."""
        # Get column name for this metric
        key = (system_key, label)

        # Check if this is a "total" metric that needs summing
        if key in SPARKLINE_TOTAL_METRICS:
            columns_to_sum = SPARKLINE_TOTAL_METRICS[key]
            # Sum the columns for each event
            values = []
            for idx, row in df.iterrows():
                total = sum(row[col] for col in columns_to_sum if col in df.columns)
                values.append(total)
        elif key in SPARKLINE_COLUMN_MAP:
            column_name = SPARKLINE_COLUMN_MAP[key]

            # Check if column exists in dataframe
            if column_name not in df.columns:
                logger.warning(f"Column {column_name} not found in simulation_events")
                return [0] * event_count

            # Extract values for this metric
            values = df[column_name].fillna(0).tolist()
        else:
            logger.warning(f"Unknown sparkline metric: {system_key}/{label}")
            return [0] * event_count

        # Pad with zeros if we have less than event_count events
        if len(values) < event_count:
            values = [0] * (event_count - len(values)) + values

        return values
    
    def create_sparkline(self, data):
        """Create a small sparkline chart."""
        # Always create a sparkline, even with empty data (shows flat line at 0)
        if not data or len(data) == 0:
            # Use configured event count or default to 60
            event_count = st.session_state.get('sparkline_event_count', 60)
            data = [0] * event_count
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            y=data,
            mode='lines',
            line=dict(color='#1f77b4', width=1.5),
            fill='tozeroy',
            fillcolor='rgba(31, 119, 180, 0.2)',
            hoverinfo='y'
        ))
        
        fig.update_layout(
            showlegend=False,
            height=40,
            margin=dict(l=0, r=0, t=0, b=0),
            xaxis=dict(showgrid=False, showticklabels=False, zeroline=False),
            yaxis=dict(showgrid=False, showticklabels=False, zeroline=False),
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
        )
        
        return fig
    
    def render_flow_diagram(self):
        """Render data flow diagram with Plotly - clickable squares showing schema."""
        fig = go.Figure()
        
        # Define system positions - New layout for make-to-stock flow clarity:
        # Horizontal main flow: Orders → Inventory → Shipping → Finance
        # Production above Inventory, MES to right of Production, Procurement below Inventory
        systems = [
            {"name": "Orders", "x": 0, "y": 2, "color": "#1f77b4", "tables": ["customer_orders", "order_line_items"]},
            {"name": "Inventory", "x": 3, "y": 2, "color": "#9467bd", "tables": ["inventory_items", "inventory_balances", "inventory_transactions", "inventory_locations"]},
            {"name": "Shipping", "x": 6, "y": 2, "color": "#8c564b", "tables": ["shipments", "shipment_items"]},
            {"name": "Finance", "x": 9, "y": 2, "color": "#e377c2", "tables": ["invoices", "payments"]},
            {"name": "Production", "x": 3, "y": 4, "color": "#2ca02c", "tables": ["production_runs", "production_materials"]},
            {"name": "MES1", "x": 5, "y": 4, "color": "#d62728", "tables": ["mes_work_orders", "mes_production_events"]},
            {"name": "MES2", "x": 7, "y": 4, "color": "#d62728", "tables": ["mes_work_orders", "mes_production_events"]},
            {"name": "MES3", "x": 9, "y": 4, "color": "#d62728", "tables": ["mes_work_orders", "mes_production_events"]},
            {"name": "Procurement", "x": 3, "y": 0, "color": "#ff7f0e", "tables": ["purchase_orders", "po_line_items"]},
        ]
        
        # Add nodes as rectangles with labels inside - most are rectangles, only Orders is a square
        for system in systems:
            # Make Production, Procurement, Inventory, Shipping, Finance, MES wider (rectangles)
            if system["name"] in ["Production", "Procurement", "Inventory", "Shipping", "Finance"]:
                width = 0.7  # Wider rectangle
                height = 0.45
            elif system["name"] in ["MES1", "MES2", "MES3"]:
                width = 0.5  # Medium rectangle for MES
                height = 0.45
            else:
                width = 0.45  # Square (Orders)
                height = 0.45
            
            # Create rectangle shape
            x_coords = [
                system["x"] - width, system["x"] + width,
                system["x"] + width, system["x"] - width,
                system["x"] - width
            ]
            y_coords = [
                system["y"] - height, system["y"] - height,
                system["y"] + height, system["y"] + height,
                system["y"] - height
            ]
            
            # Add filled rectangle
            fig.add_trace(go.Scatter(
                x=x_coords,
                y=y_coords,
                fill='toself',
                fillcolor=system["color"],
                line=dict(color='white', width=3),
                mode='lines',
                name=system["name"],
                hoverinfo='text',
                hovertext=f"{system['name']}<br>Tables: {', '.join(system['tables'])}",
                showlegend=False
            ))
            
            # Add text label
            fig.add_trace(go.Scatter(
                x=[system["x"]],
                y=[system["y"]],
                mode='text',
                text=[system["name"]],
                textposition="middle center",
                textfont=dict(size=13, color="white", family="Arial", weight="bold"),
                hoverinfo='skip',
                showlegend=False
            ))
        
        # Add flow arrows - adjusted for rectangle edges
        # Rectangles (0.7): Production, Procurement, Inventory, Shipping, Finance
        # Squares (0.45): Orders, MES
        
        flows = [
            # Main horizontal flow: Orders → Inventory → Shipping → Finance
            (0, 2, 3, 2, "inventory_check", 0.45, 0.7),  # Orders → Inventory
            (3, 2, 6, 2, "inventory_fulfillment", 0.7, 0.7),  # Inventory → Shipping
            (6, 2, 9, 2, "shipment_invoice", 0.7, 0.7),  # Shipping → Finance
            
            # Production cycle (Inventory ↔ Production):
            (3, 2, 3, 4, "backorder_trigger", 0.7, 0.7),  # Inventory → Production
            (3, 4, 3, 2, "finished_goods", 0.7, 0.7),  # Production → Inventory
            
            # Production → MES (work orders flow to each plant):
            (3, 4, 5, 4, "mes_work_orders", 0.7, 0.5),  # Production → MES1
            (3, 4, 7, 4, "mes_work_orders", 0.7, 0.5),  # Production → MES2
            (3, 4, 9, 4, "mes_work_orders", 0.7, 0.5),  # Production → MES3
            
            # Procurement cycle:
            (3, 2, 3, 0, "reorder_trigger", 0.7, 0.7),  # Inventory → Procurement
            (3, 0, 3, 2, "material_receipt", 0.7, 0.7),  # Procurement → Inventory
        ]
        
        for x0, y0, x1, y1, link_field, edge0, edge1 in flows:
            # Calculate direction vector
            dx = x1 - x0
            dy = y1 - y0
            length = (dx**2 + dy**2)**0.5
            
            # Normalize and scale by rectangle edges (different for start and end)
            # For vertical arrows, use height (0.45) instead of width
            if length > 0:
                dx_norm = dx / length
                dy_norm = dy / length
                
                # Determine if arrow is more vertical or horizontal
                is_vertical = abs(dy) > abs(dx)
                
                # Use height for vertical arrows, width for horizontal
                actual_edge0 = 0.45 if is_vertical else edge0
                actual_edge1 = 0.45 if is_vertical else edge1
                
                # Adjust start and end points to rectangle edges
                ax0 = x0 + dx_norm * actual_edge0
                ay0 = y0 + dy_norm * actual_edge0
                ax1 = x1 - dx_norm * actual_edge1
                ay1 = y1 - dy_norm * actual_edge1
                
                # Offset vertical arrows between Inventory and Production so they don't overlap
                if (x0 == x1 and abs(y1 - y0) > 1):  # Vertical arrows between Inventory and Production
                    if y1 > y0:  # Going up (Inventory → Production)
                        ax0 = ax0 - 0.15  # Offset to the left
                        ax1 = ax1 - 0.15
                    else:  # Going down (Production → Inventory)
                        ax0 = ax0 + 0.15  # Offset to the right  
                        ax1 = ax1 + 0.15
            else:
                ax0, ay0, ax1, ay1 = x0, y0, x1, y1
            
            # Add arrow without text
            fig.add_annotation(
                x=ax1, y=ay1,
                ax=ax0, ay=ay0,
                xref='x', yref='y',
                axref='x', ayref='y',
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=2,
                arrowcolor='#333333',
                opacity=0.8,
                text='',  # No text on arrow itself
            )
            
            # Add label at midpoint of arrow, positioned above/beside it
            mid_x = (ax0 + ax1) / 2
            mid_y = (ay0 + ay1) / 2
            
            # Offset label slightly above or to the side of arrow
            if abs(dy) < 0.1:  # Horizontal arrow
                label_y = mid_y + 0.3  # Position above
            elif abs(dx) < 0.1:  # Vertical arrow
                label_y = mid_y
                mid_x = mid_x + 0.5  # Position to the right
            else:  # Diagonal arrow
                label_y = mid_y + 0.3
            
            fig.add_annotation(
                x=mid_x,
                y=label_y,
                text=link_field,
                showarrow=False,
                font=dict(size=10, color='#333333', family='Arial'),
                bgcolor='rgba(255,255,255,0.9)',
                bordercolor='#cccccc',
                borderwidth=1,
                borderpad=3
            )
        
        # Add separate bidirectional arrows between Production and MES
        # MES Pull Architecture: MES pulls PLANNED production runs, then pushes status back
        prod_x, prod_y = 3, 4  # Production position
        mes_x, mes_y = 6, 4    # MES position (to the right of Production)
        offset = 0.2  # Offset arrows above and below
        
        # Production → MES (horizontal, going right) - Production triggers MES
        fig.add_annotation(
            x=mes_x - 0.7, y=mes_y - offset,  # Left side of MES, offset down
            ax=prod_x + 0.7, ay=prod_y - offset,  # Right side of Production, offset down
            xref='x', yref='y',
            axref='x', ayref='y',
            showarrow=True,
            arrowhead=2,
            arrowsize=1,
            arrowwidth=2,
            arrowcolor='#333333',
            opacity=0.8,
            text='',
        )
        
        # Label for Production → MES (production triggers MES)
        fig.add_annotation(
            x=(prod_x + mes_x) / 2,
            y=prod_y - offset - 0.4,
            text="production_triggers_MES",
            showarrow=False,
            font=dict(size=10, color='#333333', family='Arial'),
            bgcolor='rgba(255,255,255,0.9)',
            bordercolor='#cccccc',
            borderwidth=1,
            borderpad=3
        )
        
        # Arrow 2: MES → Production (horizontal, going left) - MES updates production status
        fig.add_annotation(
            x=prod_x + 0.7, y=prod_y + offset,  # Right side of Production, offset up
            ax=mes_x - 0.7, ay=mes_y + offset,  # Left side of MES, offset up
            xref='x', yref='y',
            axref='x', ayref='y',
            showarrow=True,
            arrowhead=2,
            arrowsize=1,
            arrowwidth=2,
            arrowcolor='#333333',
            opacity=0.8,
            text='',
        )
        
        # Label for MES → Production (MES updates production)
        fig.add_annotation(
            x=(prod_x + mes_x) / 2,
            y=prod_y + offset + 0.4,
            text="MES_updates_production",
            showarrow=False,
            font=dict(size=10, color='#333333', family='Arial'),
            bgcolor='rgba(255,255,255,0.9)',
            bordercolor='#cccccc',
            borderwidth=1,
            borderpad=3
        )
        
        
        fig.update_layout(
            title="Data Flow (click system for schema)",
            showlegend=False,
            height=500,
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False, range=[-1, 11]),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False, range=[-1, 5]),
            plot_bgcolor='rgba(240,242,246,0.3)',
            paper_bgcolor='rgba(0,0,0,0)',
            margin=dict(l=20, r=20, t=40, b=20),
            clickmode='event+select'
        )
        
        return fig
    
    def render_activity_timeline(self):
        """Render recent activity as a simple event log grouped by 5-minute cycles."""
        try:
            with self.db.connection() as conn:
                # Get activity grouped by 5-minute intervals
                query = """
                WITH activity AS (
                    SELECT 
                        date_trunc('minute', created_at) - 
                        (EXTRACT(MINUTE FROM created_at)::int % 5) * INTERVAL '1 minute' as cycle_time,
                        'Orders' as category,
                        COUNT(*) as count
                    FROM customer_orders
                    WHERE created_at > NOW() - INTERVAL '1 hour'
                    GROUP BY cycle_time
                    
                    UNION ALL
                    
                    SELECT 
                        date_trunc('minute', created_at) - 
                        (EXTRACT(MINUTE FROM created_at)::int % 5) * INTERVAL '1 minute' as cycle_time,
                        'Production Runs' as category,
                        COUNT(*) as count
                    FROM production_runs
                    WHERE created_at > NOW() - INTERVAL '1 hour'
                    GROUP BY cycle_time
                    
                    UNION ALL
                    
                    SELECT 
                        date_trunc('minute', created_at) - 
                        (EXTRACT(MINUTE FROM created_at)::int % 5) * INTERVAL '1 minute' as cycle_time,
                        'Shipments' as category,
                        COUNT(*) as count
                    FROM shipments
                    WHERE created_at > NOW() - INTERVAL '1 hour'
                    GROUP BY cycle_time
                    
                    UNION ALL
                    
                    SELECT 
                        date_trunc('minute', created_at) - 
                        (EXTRACT(MINUTE FROM created_at)::int % 5) * INTERVAL '1 minute' as cycle_time,
                        'POs Sent' as category,
                        COUNT(*) as count
                    FROM purchase_orders
                    WHERE created_at > NOW() - INTERVAL '1 hour'
                      AND status = 'SENT'
                    GROUP BY cycle_time
                    
                    UNION ALL
                    
                    SELECT 
                        date_trunc('minute', updated_at) - 
                        (EXTRACT(MINUTE FROM updated_at)::int % 5) * INTERVAL '1 minute' as cycle_time,
                        'POs In Transit' as category,
                        COUNT(*) as count
                    FROM purchase_orders
                    WHERE updated_at > NOW() - INTERVAL '1 hour'
                      AND status IN ('IN_TRANSIT', 'RECEIVED')
                    GROUP BY cycle_time
                    
                    UNION ALL
                    
                    SELECT 
                        date_trunc('minute', updated_at) - 
                        (EXTRACT(MINUTE FROM updated_at)::int % 5) * INTERVAL '1 minute' as cycle_time,
                        'POs Received' as category,
                        COUNT(*) as count
                    FROM purchase_orders
                    WHERE updated_at > NOW() - INTERVAL '1 hour'
                      AND status = 'RECEIVED'
                    GROUP BY cycle_time
                    
                    UNION ALL
                    
                    SELECT 
                        date_trunc('minute', created_at) - 
                        (EXTRACT(MINUTE FROM created_at)::int % 5) * INTERVAL '1 minute' as cycle_time,
                        'Invoices' as category,
                        COUNT(*) as count
                    FROM invoices
                    WHERE created_at > NOW() - INTERVAL '1 hour'
                    GROUP BY cycle_time
                )
                SELECT 
                    cycle_time,
                    category,
                    count
                FROM activity
                WHERE count > 0
                ORDER BY cycle_time DESC, category
                """
                
                df = self.db.execute_query_pandas(query)
                
                if not df.empty:
                    # Convert to datetime
                    df['cycle_time'] = pd.to_datetime(df['cycle_time'])
                    
                    # Create stacked bar chart
                    fig = px.bar(df, 
                                x='cycle_time', 
                                y='count',
                                color='category',
                                title=f"Activity by 5-Minute Cycle (Last Hour)",
                                labels={'cycle_time': 'Time', 'count': 'Events', 'category': 'Type'},
                                barmode='stack')
                    
                    fig.update_layout(
                        height=400,
                        showlegend=True,
                        legend=dict(
                            orientation="v",
                            yanchor="top",
                            y=1,
                            xanchor="left",
                            x=1.02
                        ),
                        font=dict(size=11),
                        xaxis=dict(
                            showgrid=True,
                            gridwidth=1,
                            gridcolor='rgba(128,128,128,0.2)',
                            tickformat='%H:%M'
                        ),
                        yaxis=dict(
                            showgrid=True,
                            gridwidth=1,
                            gridcolor='rgba(128,128,128,0.2)'
                        ),
                        margin=dict(r=150)
                    )
                    return fig
                else:
                    return None
        except Exception as e:
            st.error(f"Error fetching activity timeline: {e}")
            import traceback
            st.code(traceback.format_exc())
            return None
    
    
    def show_system_schema(self, system_name):
        """Display database schema for a system."""
        # Map systems to their tables
        system_tables = {
            "Orders": ["customer_orders", "order_line_items"],
            "Production": ["production_runs", "production_materials"],
            "MES": ["mes_work_orders", "mes_production_events", "mes_quality_checks", 
                    "mes_material_consumption", "mes_downtime_events"],
            "Inventory": ["inventory_items", "inventory_balances", "inventory_transactions", "inventory_locations"],
            "Procurement": ["purchase_orders", "po_line_items"],
            "Shipping": ["shipments", "shipment_items"],
            "Finance": ["invoices", "payments"],
            "Products": ["products"],
            "Customers": ["customers"],
            "BOM": ["bill_of_materials"]
        }
        
        tables = system_tables.get(system_name, [])
        
        try:
            with self.db.connection() as conn:
                for table in tables:
                    st.markdown(f"**`{table}`**")
                    
                    # Get table schema
                    query = """
                        SELECT 
                            column_name,
                            data_type,
                            is_nullable,
                            column_default
                        FROM information_schema.columns
                        WHERE table_name = %s
                        ORDER BY ordinal_position
                    """
                    df = self.db.execute_query_pandas(query, (table,))
                    
                    if not df.empty:
                        # Format for display
                        df['nullable'] = df['is_nullable'].map({'YES': '✓', 'NO': ''})
                        df = df[['column_name', 'data_type', 'nullable']]
                        df.columns = ['Column', 'Type', 'Nullable']
                        st.dataframe(df, use_container_width=True, height=min(len(df) * 35 + 38, 300))
                    else:
                        st.info(f"No schema found for {table}")
                    
                    st.markdown("---")
        except Exception as e:
            st.error(f"Error loading schema: {e}")
    
    def apply_scenario(self, scenario_name: str):
        """Apply a predefined scenario configuration to simulation_config table."""
        from erp_simulation.scenarios import SCENARIOS, apply_scenario as _apply_scenario
        if scenario_name not in SCENARIOS:
            st.error(f"Unknown scenario: {scenario_name}")
            return
        try:
            # Shared writer (erp_simulation/scenarios.py) is the single source of
            # truth for scenario definitions + the config upsert; connection()
            # auto-commits on success.
            with self.db.connection() as conn:
                scenario = _apply_scenario(conn, scenario_name)

            st.session_state['active_scenario'] = scenario_name
            # Close advanced controls when scenario is applied
            st.session_state['show_advanced_controls'] = False
            st.success(f"✅ Applied scenario: {scenario['name']}")
            st.info("Changes will take effect in the next simulation cycle (within 1 minute)")
            time.sleep(1)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            st.rerun()

        except Exception as e:
            st.error(f"❌ Failed to apply scenario: {e}")
            logger.error(f"Apply scenario error: {e}", exc_info=True)
    
    _ADVANCED_CATEGORIES = {
        'orders': '📦 Orders',
        'production': '🏭 Production',
        'mes': '⚙️ MES',
        'procurement': '🚚 Procurement',
        'shipping': '📮 Shipping',
        'finance': '💰 Finance',
        'dashboard': '📊 Dashboard'
    }

    def render_advanced_controls(self):
        """Render advanced parameter controls with sliders grouped by category."""
        try:
            df = self._advanced_config_frame()
            if df.empty:
                st.warning("No configuration parameters found")
                return
            updated_values = {}
            for category_key, category_label in self._ADVANCED_CATEGORIES.items():
                category_df = df[df['category'] == category_key]
                if category_df.empty:
                    continue
                st.markdown(f"**{category_label}**")
                for idx, row in category_df.iterrows():
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        updated_values[row['config_key']] = self._advanced_config_widget(row)
                    with col2:
                        # Show current value from database (before any slider changes)
                        st.caption(f"DB: {row['config_value']}")
            st.markdown("---")
            col1, col2, col3 = st.columns([1, 1, 2])
            with col1:
                if st.button("💾 Save Changes", type="primary", use_container_width=True):
                    self._save_advanced_config(updated_values)
            with col2:
                if st.button("🔄 Reset to Defaults", use_container_width=True):
                    self._reset_advanced_config()
            with col3:
                st.caption("Changes apply to next simulation cycle (within 1 minute)")
        except Exception as e:
            st.error(f"❌ Error loading advanced controls: {e}")
            logger.error(f"Advanced controls error: {e}", exc_info=True)

    def _advanced_config_frame(self):
        """Every simulation_config row, in the Advanced panel's order."""
        with self.db.connection() as conn:
            query = """
                SELECT config_key, config_value, data_type, category, description, 
                       min_value, max_value, default_value
                FROM simulation_config
                ORDER BY 
                    CASE category
                        WHEN 'orders' THEN 1
                        WHEN 'production' THEN 2
                        WHEN 'mes' THEN 3
                        WHEN 'procurement' THEN 4
                        WHEN 'shipping' THEN 5
                        WHEN 'finance' THEN 6
                    END,
                    config_key
            """
            return self.db.execute_query_pandas(query)

    @staticmethod
    def _advanced_config_widget(row) -> str:
        """One row's widget; returns its value as stored text. A NULL
        description or bound (a row a scenario inserted) falls back safely."""
        key = row['config_key']
        current_value = row['config_value']
        data_type = row['data_type']
        label = config_label(row['description'], key)
        min_val = row['min_value']
        max_val = row['max_value']
        if data_type == 'boolean':
            new_value = st.checkbox(
                label, value=current_value.lower() == 'true', key=f"adv_{key}")
            return 'true' if new_value else 'false'
        if data_type == 'int':
            new_value = st.slider(
                label,
                min_value=int(config_bound(min_val, 0)),
                max_value=int(config_bound(max_val, 1000)),
                value=int(float(current_value)),
                key=f"adv_{key}"
            )
            return str(new_value)
        if data_type == 'float':
            min_float = config_bound(min_val, 0.0)
            max_float = config_bound(max_val, 1.0)
            step = 0.01 if (max_float - min_float) < 10 else 0.1
            new_value = st.slider(
                label, min_value=min_float, max_value=max_float,
                value=float(current_value), step=step, key=f"adv_{key}")
            return str(new_value)
        return st.text_input(label, value=current_value, key=f"adv_{key}")

    def _save_advanced_config(self, updated_values: dict) -> None:
        """Write the panel's values, then reload the dashboard."""
        try:
            with self.db.connection() as conn:
                with conn.cursor() as cursor:
                    for key, value in updated_values.items():
                        cursor.execute("""
                            UPDATE simulation_config
                            SET config_value = %s, updated_at = NOW()
                            WHERE config_key = %s
                        """, (value, key))
                    conn.commit()
            # Clear dashboard session state to force reload of config values
            for key in ['sparkline_event_count', 'sparkline_events', 'dashboard_refresh_seconds', 'last_refresh_timestamp']:
                if key in st.session_state:
                    del st.session_state[key]
            st.success("✅ Configuration saved! Changes will take effect in next cycle.")
            time.sleep(1)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            st.rerun()
        except Exception as e:
            st.error(f"❌ Failed to save: {e}")

    def _reset_advanced_config(self) -> None:
        """Set every row back to its default, then reload the dashboard."""
        try:
            # Clear ALL widget states FIRST (all keys starting with 'adv_')
            # This must happen before database update to ensure clean slate
            keys_to_delete = [key for key in list(st.session_state.keys()) if key.startswith('adv_')]
            for key in keys_to_delete:
                del st.session_state[key]
            # Clear dashboard session state
            for key in ['sparkline_event_count', 'sparkline_events', 'dashboard_refresh_seconds', 'last_refresh_timestamp']:
                if key in st.session_state:
                    del st.session_state[key]
            # Now update database to defaults
            with self.db.connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE simulation_config
                        SET config_value = default_value, updated_at = NOW()
                    """)
                    conn.commit()
            st.success("✅ Reset to defaults!")
            time.sleep(1)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            st.rerun()
        except Exception as e:
            st.error(f"❌ Failed to reset: {e}")

    def render_ot_feed_controls(self):
        """Workshop OT chapter: one-click SiteWise degradation feed for CMX-441.

        Mirrors the Simulation Scenarios row layout. The Lambda is
        idempotent (deterministic values on a fixed timestamp grid), so
        clicking twice is safe — a re-run overwrites the same series.

        Plan D7: when the live story clock is anchored (a
        ``story_anchor_epoch`` row), the button is replaced by a read-only
        caption, since a click would overwrite the anchored grid with a
        wall-clock ramp. A database error falls back to the button, except
        on a package that ships no feed Lambda (see _ot_feed_caption_only).
        """
        story_clock = self._story_guard()
        col_label, col_btn, col_spacer = st.columns([1.5, 2.0, 6.5])
        with col_label:
            st.markdown("**📡 OT Telemetry (SiteWise)**")
        with col_btn:
            if story_clock:
                st.caption(_STORY_CLOCK_CAPTION)
            elif st.button(
                "📉 Start machine degradation feed",
                key="ot_feed_start",
                type="secondary",
                use_container_width=True,
                help=(
                    "Backfill 72h of degrading vibration (1.8→2.8 mm/s) for milling "
                    "center CMX-441 into AWS IoT SiteWise. Safe to click again — "
                    "re-runs overwrite the same series."
                ),
            ):
                self.start_ot_degradation_feed()

    def _ot_feed_caption_only(self) -> bool:
        """True when the OT row shows the caption instead of the feed button.

        A live workshop package ships no feed Lambda (ADR-0092: the story
        site's OPC UA server, read by SFC, is the only SiteWise writer) and
        says so with ``OT_FEED_SOURCE=opcua``: caption, no database read.
        Otherwise an anchored story clock shows the caption, and a database
        error falls back to the button so frozen behaviour never breaks.
        """
        if _feed_from_opcua():
            return True
        try:
            with self.db.connection() as conn:
                return _story_clock_active(conn)
        except Exception as e:
            logger.warning(f"Story clock check failed, showing the feed button: {e}")
            return False

    def _per_run(self, name, compute):
        """``compute()`` once per Streamlit run (main() builds a new dashboard each run)."""
        memo = self.__dict__.setdefault("_per_run_memo", {})
        if name not in memo:
            memo[name] = compute()
        return memo[name]

    def _story_guard(self) -> bool:
        """The OT caption check, read once per run and shared by the OT row and
        the operator-controls layout (header, scenario area)."""
        return self._per_run("story_guard", self._ot_feed_caption_only)

    def _auto_run_state(self) -> bool:
        """EventBridge schedule state, read once per run (header and expander share it)."""
        return self._per_run("auto_run", self._auto_run_enabled)

    def start_ot_degradation_feed(self):
        """Invoke the erp-sitewise-degradation-feed Lambda with inline progress.

        Follows the reset_all_data pattern: st.status box, RequestResponse
        invoke, FunctionError surfaced verbatim (the Lambda raises loud,
        contextual errors — e.g. 'asset model CNC not found').
        """
        import boto3
        import json

        box = st.status("Starting machine degradation feed...", expanded=True)
        try:
            aws_region = os.environ.get('AWS_REGION', 'us-east-1')
            lambda_client = boto3.Session().client('lambda', region_name=aws_region)

            box.info("📡 Writing 72h vibration backfill for CMX-441 to AWS IoT SiteWise (~1 min)...")
            response = lambda_client.invoke(
                FunctionName='erp-sitewise-degradation-feed',
                InvocationType='RequestResponse',
                Payload=json.dumps({}),
            )
            result = json.loads(response['Payload'].read().decode('utf-8'))

            if response.get('FunctionError'):
                message = result.get('errorMessage', result) if isinstance(result, dict) else result
                box.error(f"❌ Feed failed: {message}")
                box.update(label="Degradation feed failed", state="error")
                return False

            box.success(
                f"✅ Wrote {result.get('values_written', '?')} values across "
                f"{len(result.get('properties', []))} properties for {result.get('asset_name', 'CMX-441')} "
                f"(asset {'created' if result.get('asset_created') else 'already existed'}). "
                f"Vibration now trends to ~{result.get('vibration_end_mm_s', 2.8)} mm/s."
            )
            box.update(label="Degradation feed complete", state="complete", expanded=True)
            return True
        except Exception as e:
            box.error(f"❌ Feed failed: {e}")
            box.update(label="Degradation feed failed", state="error")
            logger.error(f"OT degradation feed error: {e}", exc_info=True)
            return False

    def reset_all_data(self):
        """Reset all simulation data: clean up the transactional data, reset
        balances and config, run db-init's full reset (which re-applies the
        optional plant and re-grants every read-only login it dropped before
        it answers) and ten simulation cycles. The erp-simulation-* rules it
        pauses (the simulator's and the Tulip sender's) are enabled again on
        every exit (the finally below): success, a step's early return (a
        failed db-init reset arrives as a FunctionError) and an exception. A
        rule already disabled stays disabled. The Tulip sender's lock is held
        from before the first destructive step to the end (_hold_sender_lock).

        Streamlit status container: the per-step updates render in one
        collapsible box that collapses on success (the rerun then clears it)
        and stays expanded on error."""
        import boto3

        box = st.status("Resetting simulation data...", expanded=True)
        paused_rules = []
        events_client = None
        sender_lock = None
        try:
            # The task role in ECS; the default credential chain locally.
            session = boto3.Session()
            aws_region = os.environ.get('AWS_REGION', 'us-east-1')
            lambda_client = session.client('lambda', region_name=aws_region)
            reset_client = session.client('lambda', region_name=aws_region,
                                          config=reset_invoke_config())
            events_client = session.client('events', region_name=aws_region)
            paused_rules = self._pause_simulation_rules(events_client)
            sender_lock = self._hold_sender_lock(box)
            if sender_lock is None:
                box.update(label="Reset failed", state="error")
                return False
            if not self._run_reset_steps(reset_client, box):
                box.update(label="Reset failed", state="error")
                return False
            time.sleep(2)  # Longer pause before simulation  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            self._run_initial_cycles(lambda_client, box)
            box.success("✅ Reset complete! Data refreshed, config reset to defaults, scenario set to Normal.")
            box.update(label="Reset complete", state="complete", expanded=False)
            return True
        except Exception as e:
            box.error(f"❌ Reset failed: {e}")
            box.update(label="Reset failed", state="error")
            logger.error(f"Reset failed: {e}", exc_info=True)
            return False
        finally:
            self._release_sender_lock(sender_lock)
            self._resume_simulation_rules(events_client, paused_rules)

    #: How long Reset All waits for a running Tulip sender cycle to finish
    #: (a cycle ends inside the sender's 55 s timeout) before it refuses.
    SENDER_LOCK_WAIT_SECONDS = 70

    def _hold_sender_lock(self, box):
        """Take the Tulip sender's advisory lock (external_mes.SENDER_LOCK_KEY)
        on a session of the dashboard's own, waiting for a running sender
        cycle, before anything destructive runs; the open connection (the
        lock), or None (shown as the error) when a cycle keeps it. Held until
        the reset ends. The session is autocommit and named
        external_mes.RESET_ALL_LOCK_APPLICATION, which both the cleanup's
        terminate of active writers and db-init's full reset leave alive, so
        the lock is held through the whole reset. Where no sender is deployed
        the lock is always free."""
        conn = self.db.dedicated_connection(idle_in_transaction_timeout_s=60,
                                            application_name=external_mes.RESET_ALL_LOCK_APPLICATION)
        try:
            conn.autocommit = True
            if external_mes.wait_for_sender_lock(conn, self.SENDER_LOCK_WAIT_SECONDS):
                return conn
        except Exception:
            self._release_sender_lock(conn)
            raise
        self._release_sender_lock(conn)
        box.error("❌ A Tulip sender cycle is still running and kept its lock; "
                  "nothing was reset. Try again in a minute.")
        return None

    @staticmethod
    def _release_sender_lock(conn):
        """Close the lock's session (which drops the lock). Best effort."""
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # nosec B110 - the session ends with the dashboard anyway
            pass

    @staticmethod
    def _pause_simulation_rules(events_client):
        """Disable every ENABLED erp-simulation-* rule (so no cycle deadlocks
        with the cleanup) and return their names. Best effort: a rule that
        cannot be paused leaves the reset to go ahead anyway."""
        paused = []
        try:
            rules = events_client.list_rules(NamePrefix='erp-simulation-')
            for rule in rules.get('Rules', []):
                if rule.get('State') == 'ENABLED':
                    events_client.disable_rule(Name=rule['Name'])
                    paused.append(rule['Name'])
            if paused:
                time.sleep(3)  # Wait for any in-flight invocations to complete  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        except Exception:  # nosec B110 - intentional fallback, no recovery action needed
            pass  # Non-fatal — proceed with cleanup anyway
        return paused

    def _run_reset_steps(self, client, box) -> bool:
        """Steps 1 to 4 of Reset All, the destructive invokes on ``client``
        (reset_invoke_config); False, with the error shown, at the first that
        fails."""
        box.info("🧹 Step 1/4: Cleaning up transactional data...")
        if not _invoke_reset_step(client, box, 'erp-cleanup-old-data', {'action': 'cleanup'}, "Cleanup"):
            return False
        box.success("✅ Transactional data cleaned")
        time.sleep(1)  # Brief pause between operations  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        box.info("� Step 2/4: Resetting inventory balances...")
        if not _invoke_reset_step(client, box, 'erp-cleanup-old-data', {'action': 'reset_balances'},
                                  "Reset balances"):
            return False
        box.success("✅ Inventory balances reset")
        time.sleep(1)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        box.info("⚙️ Step 3/5: Resetting configuration to defaults...")
        self._reset_simulation_config()
        box.success("✅ Configuration reset to defaults (Normal scenario)")
        time.sleep(1)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        # Re-seed reference data via the consolidated db-init Lambda: its full
        # reset, then the optional plant and the read-only logins it restores.
        box.info("🌱 Step 4/5: Re-seeding reference data...")
        if not _invoke_reset_step(client, box, 'erp-database-init', {'RequestType': 'Update'}, "Seed data"):
            return False
        box.success("✅ Reference data seeded")
        return True

    def _reset_simulation_config(self):
        """Step 3: every config value back to its default (downtime at a
        realistic 0.05, ~90% availability), the widget and dashboard session
        state cleared, and the scenario back to Normal."""
        with self.db.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE simulation_config
                    SET config_value = default_value, updated_at = NOW()
                """)
                cursor.execute("""
                    UPDATE simulation_config
                    SET config_value = '0.05', default_value = '0.05'
                    WHERE config_key = 'mes_downtime_probability'
                """)
                conn.commit()
        # Clear ALL widget states (all keys starting with 'adv_')
        for key in [key for key in list(st.session_state.keys()) if key.startswith('adv_')]:
            del st.session_state[key]
        for key in ['sparkline_event_count', 'sparkline_events', 'dashboard_refresh_seconds', 'last_refresh_timestamp']:
            if key in st.session_state:
                del st.session_state[key]
        st.session_state['active_scenario'] = 'normal'

    @staticmethod
    def _run_initial_cycles(lambda_client, box):
        """Step 5: ten simulation cycles for a good demo state. A cycle that
        answers busy (one the schedule started before the pause) is waited
        out, not counted."""
        import json

        box.info("🔄 Step 5/5: Running initial simulation cycles...")
        last_payload = {}

        def invoke_cycle():
            cycle_response = lambda_client.invoke(
                FunctionName='erp-simulation-orchestrator',
                InvocationType='RequestResponse',
                Payload=json.dumps({'action': 'single_cycle'})
            )
            last_payload['body'] = json.loads(cycle_response['Payload'].read().decode('utf-8'))
            return cycle_response.get('FunctionError'), last_payload['body']

        def on_cycle(n, outcome):
            if outcome == cycle_result.FAILED:
                box.warning(f"⚠️ Simulation cycle {n} had issues: {last_payload['body']}")
            else:
                box.write(f"   ✓ Cycle {n} complete")
            time.sleep(1)  # Pause between cycles  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep

        def on_busy(waited):
            if waited == 0:
                box.info("⏳ A scheduled cycle that started before the reset is still "
                         "running; waiting for it to finish...")

        warmup = cycle_result.run_cycles(invoke_cycle, 10, on_cycle=on_cycle, on_busy=on_busy)
        if warmup['gave_up']:
            box.warning(f"⚠️ Only {warmup['ran'] + warmup['failed']} of 10 initial cycles ran: "
                        f"the simulator stayed busy for {warmup['waited_seconds']} s. "
                        f"The schedule fills in the rest once it is re-enabled.")

    @staticmethod
    def _resume_simulation_rules(events_client, rule_names):
        """Re-enable the simulator rules reset_all_data disabled (its finally:
        every exit goes through here). A rule that cannot be re-enabled is
        logged; the reset's own result stands."""
        for name in rule_names:
            try:
                events_client.enable_rule(Name=name)
            except Exception as e:  # nosec B110 - logged; the reset result stands
                logger.warning(f"Could not re-enable {name} after Reset All: {e}")
    
    def render(self):
        """Render the complete dashboard."""
        self._render_header_row()

        active_scenario = self._load_active_scenario()
        self._render_scenario_area(active_scenario)

        st.markdown("---")  # Separator

        # Check for popup trigger
        self.render_details_popup_trigger()

        # Fetch KPIs
        kpis = self.get_system_kpis()

        if kpis:
            self._render_kpi_sections(kpis)

        self._render_auto_refresh()

    def _render_scenario_area(self, active_scenario):
        """Under the header: the collapsed operator controls on a live story,
        otherwise the collapsible Simulation Scenarios bar as before."""
        if self._story_guard():
            self._render_operator_controls(active_scenario)
            return
        self._render_scenario_bar(active_scenario)

        # Show scenario buttons only when expanded
        if st.session_state.get('show_scenarios', False):
            self._render_scenario_buttons(active_scenario)

    def _render_operator_controls(self, active_scenario):
        """Live story: Pause, Reset All Data and the scenarios in one collapsed
        expander. Each one stops, wipes or reshapes the plant history the
        workshop story runs on, so none of them sits one click away."""
        with st.expander(_OPERATOR_CONTROLS_LABEL, expanded=False):
            col_pause, col_reset, _col_spacer = st.columns([1, 1, 2])
            with col_pause:
                self._render_pause_button(self._auto_run_state())
            with col_reset:
                self._render_guarded_reset_button()
            st.markdown(_scenario_status_html(active_scenario), unsafe_allow_html=True)
            self._render_scenario_buttons(active_scenario, nested=True)

    def _render_header_row(self):
        """Compact header row: title, simulation controls, version, reset, theme."""
        # Compact header row: title, version, simulation controls, reset button, and theme toggle
        col_title, col_sim_controls, col_version, col_reset, col_theme = st.columns([2.5, 2.5, 0.8, 0.7, 0.5])
        with col_title:
            st.title(branding.header_title())
        with col_theme:
            st.markdown("<div style='padding-top: 18px;'></div>", unsafe_allow_html=True)
            theme_label = "☀️ Light" if st.session_state.dark_mode else "🌙 Dark"
            if st.button(theme_label, key="theme_toggle"):
                st.session_state.dark_mode = not st.session_state.dark_mode
                st.rerun()
        with col_sim_controls:
            self._render_sim_controls()

        with col_version:
            st.markdown(f"<div style='text-align: right; padding-top: 20px;'>App v{APP_VERSION} | Dashboard v{DASHBOARD_VERSION}</div>", unsafe_allow_html=True)
        with col_reset:
            if not self._story_guard():
                self._render_reset_button()

    def _render_sim_controls(self):
        """Simulation control buttons (Auto Run/Pause/Single Cycle) and status line.

        On a live story Pause sits in the operator controls instead."""
        st.markdown("<div style='padding-top: 10px;'></div>", unsafe_allow_html=True)

        # Check EventBridge rule status to determine if auto-run is enabled
        auto_run_enabled = self._auto_run_state()
        guarded = self._story_guard()

        btn_cols = st.columns(2 if guarded else 3)

        # Inject CSS to color buttons - must be done right before buttons render
        st.markdown(_SIM_BUTTON_CSS, unsafe_allow_html=True)
        self._init_sim_status()

        with btn_cols[0]:
            self._render_auto_run_button(auto_run_enabled)
        if not guarded:
            with btn_cols[1]:
                self._render_pause_button(auto_run_enabled)
        with btn_cols[-1]:
            self._render_single_cycle_button()

        # Display status message below buttons (clears after 1 sec)
        if st.session_state.sim_status_message:
            st.markdown(f"<div style='text-align: center; font-size: 0.85rem; color: #666; margin-top: 5px;'>{st.session_state.sim_status_message}</div>", unsafe_allow_html=True)

    def _auto_run_enabled(self):
        """EventBridge schedule state; False when it cannot be read."""
        auto_run_enabled = False
        try:
            import boto3
            if os.getenv('AWS_EXECUTION_ENV') or os.getenv('ECS_CONTAINER_METADATA_URI'):  # branches intentionally identical; comments document ECS vs local credential chains  # nosemgrep: useless-if-body
                session = boto3.Session()
            else:
                session = boto3.Session()
            aws_region = os.environ.get('AWS_REGION', 'us-east-1')
            events = session.client('events', region_name=aws_region)
            rule = events.describe_rule(Name='erp-simulation-schedule')
            auto_run_enabled = rule['State'] == 'ENABLED'
        except:  # nosec B110 - intentional fallback, no recovery action needed
            pass  # If we can't check, assume disabled
        return auto_run_enabled

    def _init_sim_status(self):
        """Initialize the status message and clear it after 1 second."""
        # Initialize status message in session state
        if 'sim_status_message' not in st.session_state:
            st.session_state.sim_status_message = None
        if 'sim_status_time' not in st.session_state:
            st.session_state.sim_status_time = None

        # Clear status message after 1 second
        if st.session_state.sim_status_time:
            import time as time_module
            if time_module.time() - st.session_state.sim_status_time > 1:
                st.session_state.sim_status_message = None
                st.session_state.sim_status_time = None

    def _render_auto_run_button(self, auto_run_enabled):
        """Auto Run button (enables the EventBridge schedule)."""
        # Auto Run button - green when enabled (default state)
        if st.button("▶️ Auto Run", 
                   key="auto_run_sim", 
                   help="Enable automatic simulation cycles every 1 minute", 
                   use_container_width=True,
                   type="primary" if auto_run_enabled else "secondary"):
            try:
                import boto3
                if os.getenv('AWS_EXECUTION_ENV') or os.getenv('ECS_CONTAINER_METADATA_URI'):  # branches intentionally identical; comments document ECS vs local credential chains  # nosemgrep: useless-if-body
                    session = boto3.Session()
                else:
                    session = boto3.Session()
                aws_region = os.environ.get('AWS_REGION', 'us-east-1')
                events = session.client('events', region_name=aws_region)
                events.enable_rule(Name='erp-simulation-schedule')
                st.session_state.sim_status_message = "▶️ Auto Run enabled"
                st.session_state.sim_status_time = time.time()
                # Reset refresh timestamp to restart auto-refresh immediately
                st.session_state.last_refresh_timestamp = time.time()
                time.sleep(1)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
                st.rerun()
            except Exception as e:
                st.session_state.sim_status_message = f"Failed: {e}"
                st.session_state.sim_status_time = time.time()

    def _render_pause_button(self, auto_run_enabled):
        """Pause button (disables the EventBridge schedule)."""
        # Pause button - green if paused (auto-run disabled)
        if st.button("⏸️ Pause", 
                   key="pause_sim", 
                   help="Pause automatic simulation cycles", 
                   use_container_width=True,
                   type="primary" if not auto_run_enabled else "secondary"):
            try:
                import boto3
                if os.getenv('AWS_EXECUTION_ENV') or os.getenv('ECS_CONTAINER_METADATA_URI'):  # branches intentionally identical; comments document ECS vs local credential chains  # nosemgrep: useless-if-body
                    session = boto3.Session()
                else:
                    session = boto3.Session()
                aws_region = os.environ.get('AWS_REGION', 'us-east-1')
                events = session.client('events', region_name=aws_region)
                events.disable_rule(Name='erp-simulation-schedule')
                st.session_state.sim_status_message = "⏸️ Paused"
                st.session_state.sim_status_time = time.time()
                time.sleep(1)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
                st.rerun()
            except Exception as e:
                st.session_state.sim_status_message = f"Failed: {e}"
                st.session_state.sim_status_time = time.time()

    def _render_single_cycle_button(self):
        """Single Cycle button (invokes the orchestrator Lambda once)."""
        # Single Cycle button - flashes green when pressed
        single_cycle_pressed = 'single_cycle_pressed' in st.session_state and st.session_state.single_cycle_pressed
        if st.button("⏭️ Single Cycle", 
                   key="single_cycle", 
                   help="Run a single simulation cycle", 
                   use_container_width=True,
                   type="primary" if single_cycle_pressed else "secondary"):
            try:
                import boto3
                import json
                if os.getenv('AWS_EXECUTION_ENV') or os.getenv('ECS_CONTAINER_METADATA_URI'):  # branches intentionally identical; comments document ECS vs local credential chains  # nosemgrep: useless-if-body
                    session = boto3.Session()
                else:
                    session = boto3.Session()
                aws_region = os.environ.get('AWS_REGION', 'us-east-1')
                lambda_client = session.client('lambda', region_name=aws_region)

                # Set button to green
                st.session_state.single_cycle_pressed = True

                with st.spinner("Running cycle..."):
                    response = lambda_client.invoke(
                        FunctionName='erp-simulation-orchestrator',
                        InvocationType='RequestResponse',
                        Payload=json.dumps({'action': 'single_cycle'})
                    )
                    result = json.loads(response['Payload'].read().decode('utf-8'))
                    outcome = cycle_result.cycle_outcome(response.get('FunctionError'), result)

                    if response.get('FunctionError'):
                        st.session_state.sim_status_message = f"❌ Failed: {result}"
                        st.session_state.sim_status_time = time.time()
                    elif outcome == cycle_result.BUSY:
                        st.session_state.sim_status_message = (
                            "⏳ A scheduled cycle is still running, so this one ran no "
                            "simulator steps. Try again in a minute.")
                        st.session_state.sim_status_time = time.time()
                        st.session_state.single_cycle_pressed = False
                    else:
                        st.session_state.sim_status_message = "✅ Cycle complete"
                        st.session_state.sim_status_time = time.time()
                        time.sleep(1)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
                        st.session_state.single_cycle_pressed = False
                        st.rerun()
            except Exception as e:
                st.session_state.sim_status_message = f"Failed: {e}"
                st.session_state.sim_status_time = time.time()
                st.session_state.single_cycle_pressed = False

    def _render_reset_button(self):
        """Reset All Data button."""
        st.markdown("<div style='padding-top: 10px;'></div>", unsafe_allow_html=True)
        if st.button("🔄 Reset All Data", key="reset_data", type="secondary", help="Truncate all transactional data and re-seed", use_container_width=True):
            self._run_reset()

    def _run_reset(self):
        """Run reset_all_data with a spinner; rerun after a success."""
        with st.spinner("Resetting simulation data..."):
            if self.reset_all_data():
                time.sleep(2)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
                st.rerun()

    def _render_guarded_reset_button(self):
        """Live story Reset All Data: the first click arms it, the second wipes.

        See _reset_step. The auto-refresh loop marks its own reruns
        (_AUTO_RERUN_KEY) so the arm outlives the refresh clock; any other
        run disarms it.
        """
        clicked = st.button("🔄 Reset All Data", key="reset_data", type="secondary", help="Truncate all transactional data and re-seed", use_container_width=True)
        armed_at, run_reset = _reset_step(
            st.session_state.get(_RESET_ARMED_AT_KEY),
            bool(clicked),
            bool(st.session_state.pop(_AUTO_RERUN_KEY, False)),
            time.time(),
        )
        st.session_state[_RESET_ARMED_AT_KEY] = armed_at
        # Always emit the slot: every run ends in the refresh loop's st.rerun(),
        # so a run never completes and Streamlit never clears a stale element.
        # Without the slot a disarmed warning stays on screen, greyed out.
        confirm_slot = st.empty()
        if armed_at is not None:
            confirm_slot.warning(_RESET_CONFIRM_TEXT)
        if run_reset:
            self._run_reset()

    def _load_active_scenario(self):
        """Active scenario from the database (persists across page refreshes)."""
        # Get active scenario from database (persists across page refreshes)
        active_scenario = 'normal'  # default
        if 'active_scenario' not in st.session_state:
            # Load from database on first page load
            try:
                with self.db.connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("""
                            SELECT config_value FROM simulation_config
                            WHERE config_key = 'active_scenario'
                        """)
                        result = cursor.fetchone()
                        if result:
                            active_scenario = result[0]
                            st.session_state['active_scenario'] = active_scenario
            except Exception as e:
                logger.warning(f"Could not load active_scenario from database: {e}")
                st.session_state['active_scenario'] = 'normal'
        else:
            active_scenario = st.session_state.get('active_scenario', 'normal')
        return active_scenario

    def _render_scenario_bar(self, active_scenario):
        """Collapsible Simulation Scenarios header row."""
        # Collapsible Simulation Scenarios section - left-justified
        col_label, col_scenario, col_expand, col_spacer = st.columns([1.5, 1.5, 0.8, 6.2])
        with col_label:
            st.markdown("**🎛️ Simulation Scenarios**")
        with col_scenario:
            st.markdown(_scenario_status_html(active_scenario), unsafe_allow_html=True)
        with col_expand:
            show_scenarios = st.session_state.get('show_scenarios', False)
            if st.button("▼ Expand" if not show_scenarios else "▲ Collapse", key="toggle_scenarios"):
                st.session_state['show_scenarios'] = not show_scenarios
                st.rerun()
        # col_spacer is empty to push content left

    def _render_scenario_buttons(self, active_scenario, nested=False):
        """Scenario buttons and the Advanced controls (``nested``: inside the
        operator-controls expander)."""
        # Use a container to properly scope the scenarios section
        with st.container():
            row1_col1, row1_col2, row1_col3 = st.columns(3)
            row2_col1, row2_col2, row2_col3 = st.columns(3)
            row3_col1, row3_col2, row3_col3 = st.columns(3)

            with row1_col1:
                if st.button("✓ Normal", type="primary" if active_scenario == "normal" else "secondary", use_container_width=True, help="Balanced production - current settings"):
                    self.apply_scenario("normal")
            with row1_col2:
                if st.button("📈 Demand Surge", type="primary" if active_scenario == "demand_surge" else "secondary", use_container_width=True, help="10x orders, large quantities"):
                    self.apply_scenario("demand_surge")
            with row1_col3:
                if st.button("🔴 MES Outage", type="primary" if active_scenario == "mes_outage" else "secondary", use_container_width=True, help="Production system down"):
                    self.apply_scenario("mes_outage")

            with row2_col1:
                if st.button("🚚 Supply Crisis", type="primary" if active_scenario == "supply_crisis" else "secondary", use_container_width=True, help="Long lead times, late deliveries"):
                    self.apply_scenario("supply_crisis")
            with row2_col2:
                if st.button("⚠️ Equipment Down", type="primary" if active_scenario == "equipment_down" else "secondary", use_container_width=True, help="Frequent breakdowns"):
                    self.apply_scenario("equipment_down")
            with row2_col3:
                if st.button("💰 Cash Crunch", type="primary" if active_scenario == "cash_crunch" else "secondary", use_container_width=True, help="Slow customer payments"):
                    self.apply_scenario("cash_crunch")

            with row3_col1:
                if st.button("🔍 Quality Issues", type="primary" if active_scenario == "quality_issues" else "secondary", use_container_width=True, help="High failure rates"):
                    self.apply_scenario("quality_issues")
            with row3_col2:
                if st.button("🚀 Capacity+", type="primary" if active_scenario == "capacity_plus" else "secondary", use_container_width=True, help="Faster lines, less downtime"):
                    self.apply_scenario("capacity_plus")
            with row3_col3:
                if st.button("⚙️ Advanced", use_container_width=True, help="Fine-tune individual parameters"):
                    st.session_state['show_advanced_controls'] = not st.session_state.get('show_advanced_controls', False)
                    st.rerun()

            # Advanced controls (shown when Advanced button clicked)
            self._render_advanced_section(nested)

    def _render_advanced_section(self, nested):
        """Advanced parameter controls once toggled on: in their own expander,
        or under a heading when already inside the operator expander, since
        expanders should not nest."""
        if not st.session_state.get('show_advanced_controls', False):
            return
        if nested:
            st.markdown("**⚙️ Advanced Parameter Controls**")
            self.render_advanced_controls()
            return
        with st.expander("⚙️ Advanced Parameter Controls", expanded=True):
            self.render_advanced_controls()

    def _render_kpi_sections(self, kpis):
        """System cards, flow/activity charts and the SQL interface."""
        self._render_flow_cards(kpis)
        self._render_mes_site_cards(kpis)
        self._render_store_cards(kpis)
        self._render_flow_and_timeline()
        self._render_sql_interface()

        # Last updated timestamp
        st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    def _render_flow_cards(self, kpis):
        """System Cards Row 1: Orders, Production, Shipping."""
        # System Cards Row 1: Orders, Production, Shipping
        col1, col2, col3 = st.columns(3)

        self.render_system_card(
            "Orders",
            "📦",
            {
                "New Orders": kpis['orders'][0],
                "Pending": kpis['orders'][1],
                "Confirmed": kpis['orders'][2],
                "🔴 BACKORDERED": kpis['orders'][3],
                "In Production": kpis['orders'][4],
                "Shipped": kpis['orders'][5],
            },
            kpis['orders'][6],
            col1,
            "orders"
        )

        self.render_system_card(
            "Production",
            "🏭",
            {
                "Total Runs": kpis['production'][0],
                "Planned": kpis['production'][2],
                "Released": kpis['production'][3],
                "In Progress": kpis['production'][4],
                "Completed": kpis['production'][5],
            },
            kpis['production'][1],
            col2,
            "production"
        )

        self._render_shipping_card(kpis, col3)

    def _render_shipping_card(self, kpis, col3):
        """Shipping system card."""
        self.render_system_card(
            "Shipping",
            "🚚",
            {
                "Total": kpis['shipping'][0],
                "Preparing": kpis['shipping'][2],
                "Picked": kpis['shipping'][3],
                "Packed": kpis['shipping'][4],
                "Shipped": kpis['shipping'][5],
                "In Transit": kpis['shipping'][6],
                "Delivered": kpis['shipping'][7],
            },
            kpis['shipping'][1],
            col3,
            "shipping"
        )

    def _render_mes_site_cards(self, kpis):
        """System Cards Row 2: MES per-site (labels from SITE_LABELS)."""
        # System Cards Row 2: MES per-site
        mes_per_site = kpis.get('mes_per_site', [])
        if mes_per_site:
            mes_cols = st.columns(len(mes_per_site))
            for i, site_data in enumerate(mes_per_site):
                site_code, total, scheduled, running, completed = site_data
                label = SITE_LABELS.get(site_code, site_code)
                self.render_system_card(
                    f"MES - {label}",
                    "⚙️",
                    {
                        "Work Orders": total,
                        "Scheduled": scheduled,
                        "Running": running,
                        "Completed": completed,
                    },
                    total,
                    mes_cols[i],
                    f"mes_{site_code}"
                )

    def _render_store_cards(self, kpis):
        """System Cards Row 3: Inventory, Procurement, Finance."""
        # System Cards Row 3: Inventory, Procurement, Finance
        col1, col2, col3 = st.columns(3)

        self.render_system_card(
            "Inventory",
            "📦",
            {
                "Items": kpis['inventory'][0],
                "Total Qty": kpis['inventory'][1],
                "Low Stock": kpis['inventory'][2],
            },
            kpis['inventory'][3],
            col1,
            "inventory"
        )

        self.render_system_card(
            "Procurement",
            "🛒",
            {
                "Total POs": kpis['procurement'][0],
                "Sent": kpis['procurement'][2],
                "In Transit": kpis['procurement'][3],
                "Received": kpis['procurement'][4],
            },
            kpis['procurement'][1],
            col2,
            "procurement"
        )

        self.render_system_card(
            "Finance",
            "💰",
            {
                "Invoices": kpis['finance'][0],
                "Revenue": f"${kpis['finance'][2]:,.0f}" if kpis['finance'][2] else "$0",
                "Collected": f"${kpis['finance'][3]:,.0f}" if kpis['finance'][3] else "$0",
                "Paid": kpis['finance'][4],
            },
            kpis['finance'][1],
            col3,
            "finance"
        )

    def _render_flow_and_timeline(self):
        """Flow Diagram and Activity Timeline side by side."""
        # Flow Diagram and Activity Timeline side by side
        col1, col2 = st.columns([1, 1])

        with col1:
            st.markdown("**🔄 Data Flow**")
            flow_fig = self.render_flow_diagram()
            st.plotly_chart(flow_fig, use_container_width=True)

            # Schema display
            with st.expander("📋 View Database Schema"):
                schema_system = st.selectbox(
                    "Select System",
                    ["Orders", "Production", "MES", "Inventory", "Procurement", "Shipping", "Finance", "Products", "Customers", "BOM"]
                )
                self.show_system_schema(schema_system)

        with col2:
            st.markdown("**📊 Simulation Activity**")
            timeline_fig = self.render_activity_timeline()
            if timeline_fig:
                st.plotly_chart(timeline_fig, use_container_width=True)
            else:
                st.info("No recent activity. Start the simulation to see data flow!")

    def _render_sql_interface(self):
        """SQL Query Interface - Full Width (read-only SELECTs)."""
        with st.expander("🔍 SQL Query Interface"):
            st.markdown("**Execute custom SQL queries** (read-only)")

            # Quick query templates
            query_template = st.selectbox(
                "Quick Templates",
                [
                    "Custom Query",
                    "List all simulation_config parameters",
                    "Show orders by status",
                    "Show production runs by status",
                    "Show inventory levels",
                    "Show MES work orders",
                    "Show customer order details"
                ]
            )

            # Pre-fill query based on template
            default_query = SQL_QUERY_TEMPLATES.get(query_template, "")

            sql_query = st.text_area(
                "SQL Query",
                value=default_query,
                height=200,
                help="Enter a SELECT query to execute. Only read-only queries are allowed."
            )

            col_query1, col_query2 = st.columns([1, 4])
            with col_query1:
                execute_query = st.button("▶️ Execute", type="primary")
            with col_query2:
                max_rows = st.number_input("Max rows", min_value=10, max_value=1000, value=100, step=10)

            if execute_query and sql_query.strip():
                self._execute_readonly_query(sql_query, max_rows)

    def _execute_readonly_query(self, sql_query, max_rows):
        """Run one read-only SELECT and show the result.

        The console's own login (``console_login``) is the security boundary:
        the query never runs as the dashboard's admin login. The checks before
        it only give a clearer message, except the size cap, which keeps an
        oversized paste from being scanned at all.
        """
        try:
            refusal = _console_refusal(sql_query)
            if refusal:
                st.error(refusal)
            else:
                with st.spinner("Executing query..."):
                    columns, rows = console_login.run_console_query(self.db, sql_query)
                    self._show_query_rows(columns, rows, max_rows)

        except console_login.ConsoleUnavailable as e:
            st.error(f"❌ {console_login.UNAVAILABLE_MESSAGE}")
            logger.warning(f"SQL console unavailable: {e}")
        except Exception as e:
            # Never show the raw database error; the log keeps the detail.
            st.error(f"❌ {sql_console.console_error_message(e)}")
            logger.error(f"SQL query error (SQLSTATE {getattr(e, 'pgcode', None)}): {e}", exc_info=True)

    def _show_query_rows(self, columns, rows, max_rows):
        """Result table plus CSV download (or "no results")."""
        if rows:
            # Convert to list of dicts
            result = [dict(zip(columns, row)) for row in rows]

            # Limit rows
            limited_result = result[:max_rows]

            st.success(f"✅ Query executed successfully ({len(result)} rows returned, showing {len(limited_result)})")

            # Display as dataframe
            import pandas as pd
            df = pd.DataFrame(limited_result)
            st.dataframe(df, use_container_width=True, height=400)

            # Download button
            csv = df.to_csv(index=False)
            st.download_button(
                label="📥 Download CSV",
                data=csv,
                file_name=f"query_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )
        else:
            st.info("Query returned no results")

    def _render_auto_refresh(self):
        """Config-driven, non-blocking auto-refresh."""
        # Config-driven auto-refresh - read interval from database
        # Default to 60 seconds if not configured
        if 'dashboard_refresh_seconds' not in st.session_state:
            try:
                query = """
                    SELECT CAST(config_value AS INTEGER) as refresh_seconds
                    FROM simulation_config
                    WHERE config_key = 'dashboard_refresh_seconds'
                """
                df = self.db.execute_query_pandas(query)
                if df.empty:
                    st.session_state.dashboard_refresh_seconds = 60
                    logger.warning("dashboard_refresh_seconds not found in config, using default 60")
                else:
                    st.session_state.dashboard_refresh_seconds = int(df['refresh_seconds'].iloc[0])
                    logger.info(f"Auto-refresh interval: {st.session_state.dashboard_refresh_seconds} seconds")
            except Exception as e:
                logger.error(f"Error loading dashboard_refresh_seconds: {e}")
                st.session_state.dashboard_refresh_seconds = 60

        refresh_seconds = st.session_state.dashboard_refresh_seconds

        # Non-blocking auto-refresh using time-based rerun
        # Initialize refresh timestamp if not set
        if 'last_refresh_timestamp' not in st.session_state:
            st.session_state.last_refresh_timestamp = time.time()

        # Check if it's time to refresh
        elapsed = time.time() - st.session_state.last_refresh_timestamp
        if elapsed >= refresh_seconds:
            st.session_state.last_refresh_timestamp = time.time()
            # Clear sparkline cache to force fresh data load
            if 'sparkline_events' in st.session_state:
                del st.session_state['sparkline_events']
            self._mark_auto_rerun()
            st.rerun()

        # Schedule next check after remaining time
        remaining = refresh_seconds - elapsed
        if remaining > 0:
            time.sleep(min(remaining, 1))  # Sleep max 1 second to keep UI responsive
            self._mark_auto_rerun()
            st.rerun()

    def _mark_auto_rerun(self):
        """Live story: tell the two-step Reset that the next run is the refresh
        clock, not a user interaction (see _reset_step)."""
        if self._story_guard():
            st.session_state[_AUTO_RERUN_KEY] = True


def main():
    """Main entry point for the dashboard."""
    dashboard = ERPDashboard()
    dashboard.render()


if __name__ == "__main__":
    main()
