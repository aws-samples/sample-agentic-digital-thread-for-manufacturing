# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda function to cleanup old sample data and reset for beverage products."""
import json
import os
import time
import boto3
from erp_simulation import pgdriver
from datetime import datetime
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def get_db_credentials():
    """Get database credentials from Secrets Manager."""
    try:
        region = os.environ.get('AWS_REGION', 'us-east-1')
        sm = boto3.client('secretsmanager', region_name=region)
        secret_name = os.environ.get('DB_SECRET_NAME', 'erp-simulation/database-lambda-vpc')
        logger.info(f"Fetching secret: {secret_name} from region: {region}")
        
        secret_response = sm.get_secret_value(SecretId=secret_name)
        secret = json.loads(secret_response['SecretString'])
        
        # Log available keys (without values) for debugging
        logger.info(f"Secret keys available: {list(secret.keys())}")
        
        # Aurora secrets use 'dbname', not 'database'
        db_name = secret.get('dbname', secret.get('database', 'erp_simulation'))
        logger.info(f"Using database: {db_name}")
        
        return {
            'host': secret['host'],
            'port': secret.get('port', 5432),
            'database': db_name,
            'user': secret['username'],
            'password': secret['password']
        }
    except KeyError as e:
        logger.error(f"Missing required key in secret: {e}")
        raise ValueError(f"Database secret missing required key: {e}")
    except Exception as e:
        logger.error(f"Failed to get database credentials: {e}")
        raise


def _pause_simulator():
    """Disable the ERP simulation EventBridge schedule so the simulator stops
    writing before we TRUNCATE. Without this, the simulator holds row locks and
    the TRUNCATE either deadlocks or fails on lock_timeout.

    Returns the list of rule names we disabled (so they can be re-enabled).
    Safe/non-fatal: logs a warning and returns [] if it can't pause.
    """
    import time as _time
    rule_name = os.environ.get('SIMULATION_SCHEDULE_RULE', 'erp-simulation-schedule')
    disabled = []
    try:
        region = os.environ.get('AWS_REGION', 'us-east-1')
        events = boto3.client('events', region_name=region)
        # Only pause (and later resume) if the simulator is actually running, so we
        # never turn on a schedule that an operator deliberately left disabled.
        state = events.describe_rule(Name=rule_name).get('State')
        if state == 'ENABLED':
            events.disable_rule(Name=rule_name)
            disabled.append(rule_name)
            logger.info(f"Paused simulator schedule '{rule_name}'; waiting for in-flight cycle to drain...")
            # The schedule fires every minute and a cycle is short; give any in-flight
            # orchestrator invocation time to commit and release its row locks.
            _time.sleep(15)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        else:
            logger.info(f"Simulator schedule '{rule_name}' is {state}; leaving it as-is.")
    except Exception as e:
        logger.warning(f"Could not pause simulator '{rule_name}' (proceeding; lock_timeout guards): {e}")
    return disabled


def _resume_simulator(rule_names):
    """Re-enable the simulation schedule rule(s) paused by _pause_simulator."""
    if not rule_names:
        return
    try:
        region = os.environ.get('AWS_REGION', 'us-east-1')
        events = boto3.client('events', region_name=region)
        for name in rule_names:
            events.enable_rule(Name=name)
            logger.info(f"Resumed simulator schedule '{name}'")
    except Exception as e:
        logger.error(f"FAILED to resume simulator schedule {rule_names}: {e} "
                     f"- the simulation may stay paused; re-enable the EventBridge rule manually")


# Tables whose row counts are reported before/after cleanup.
_TABLES_TO_CHECK = [
    'mes_downtime_events', 'mes_material_consumption', 'mes_quality_checks',
    'mes_production_events', 'mes_work_orders', 'payments', 'invoice_line_items',
    'invoices', 'shipment_items', 'shipments', 'production_materials',
    'production_runs', 'po_receipts', 'po_line_items', 'purchase_orders',
    'order_line_items', 'customer_orders', 'inventory_transactions',
    'products', 'inventory_items', 'inventory_balances', 'bill_of_materials'
]


def _count_sql(table):
    """``SELECT COUNT(*)`` over *table* (a name from a fixed list in this
    module), quoted by the driver because a name cannot be a bound parameter."""
    sql = pgdriver.sql

    return sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))


def _table_counts(cursor):
    """COUNT(*) for every table in _TABLES_TO_CHECK."""
    counts = {}
    for table in _TABLES_TO_CHECK:
        cursor.execute(_count_sql(table))
        counts[table] = cursor.fetchone()[0]
    return counts


#: The ERP dashboard's Reset All lock session: its application_name
#: (erp_simulation/external_mes.py RESET_ALL_LOCK_APPLICATION) and the Tulip
#: sender lock it holds (external_mes.SENDER_LOCK_KEY), both pinned equal by
#: test. Never terminated, so the sender's lock stays held for the whole
#: reset. Only a session with the name that also holds the (exclusive) lock
#: matches: the name alone is client-chosen.
RESET_ALL_LOCK_APPLICATION = 'erp-dashboard-reset-all'
RESET_ALL_LOCK_KEY = 6076847149970312772

#: pg_locks shows a bigint advisory key as its high half (classid), its low
#: half (objid) and objsubid 1.
_RESET_ALL_LOCK_PARAMS = {'app': RESET_ALL_LOCK_APPLICATION,
                          'classid': (RESET_ALL_LOCK_KEY >> 32) & 0xFFFFFFFF,
                          'objid': RESET_ALL_LOCK_KEY & 0xFFFFFFFF}


def _terminate_blocking_writers(conn, cursor):
    """Terminate other active backends before the TRUNCATE.

    The EventBridge rule is already paused, but the simulation Lambda's
    timeout is 300s — an IN-FLIGHT cycle can hold row locks longer than any
    retry budget this 300s Lambda can afford (observed live). This is a
    RESET: the transactional data is being destroyed anyway, so killing
    in-flight writer transactions is semantically safe. Targets only
    non-idle backends on THIS database, never our own connection and never
    Reset All's lock session.
    """
    cursor.execute("""
        SELECT pg_terminate_backend(a.pid) FROM pg_stat_activity a
        WHERE a.datname = current_database()
          AND a.pid <> pg_backend_pid()
          AND a.state <> 'idle'
          AND NOT (COALESCE(a.application_name, '') = %(app)s AND EXISTS (
              SELECT 1 FROM pg_locks l
               WHERE l.pid = a.pid AND l.locktype = 'advisory' AND l.granted
                 AND l.classid = %(classid)s::oid AND l.objid = %(objid)s::oid AND l.objsubid = 1))
    """, _RESET_ALL_LOCK_PARAMS)
    terminated = cursor.rowcount
    conn.commit()
    logger.info("Terminated %s active backend(s) before truncate", terminated)


def _truncate_transactional_tables(conn, cursor, results, attempts=3, settle_seconds=25):
    """1-9. TRUNCATE all transactional tables in one fast operation.

    Row-by-row DELETE across ~24 tables is O(n^2) on bloated data (FK-cascade
    checks) and blows past the Lambda's 300s timeout. TRUNCATE ... CASCADE
    clears every dependent table in milliseconds and reclaims space.

    The caller pauses the simulator EventBridge rule first, but an IN-FLIGHT
    simulation invocation can hold row locks for tens of seconds after the
    pause (observed live: single-attempt 60s lock_timeout still failed with
    "canceling statement due to lock timeout"). So: bounded retry — each
    attempt fails fast at 30s, rolls back, waits ``settle_seconds`` for the
    in-flight cycle to finish, and retries. Worst case ~3*30 + 2*25 = 140s,
    inside the 300s Lambda budget with room for the remaining deletes.
    """
    logger.info("Truncating all transactional tables...")
    truncate_sql = """
        TRUNCATE TABLE
            lot_mes_tracking, lot_production_usage, lot_warehouse_movements, material_lots,
            mes_downtime_events, mes_material_consumption, mes_quality_checks,
            mes_production_events, mes_work_orders,
            payments, invoice_line_items, invoices,
            shipment_items, shipments,
            production_materials, production_runs,
            po_receipts, po_line_items, purchase_orders,
            order_line_items, customer_orders,
            simulation_events, inventory_transactions, inventory_balances
        CASCADE
    """
    for attempt in range(1, attempts + 1):
        try:
            _terminate_blocking_writers(conn, cursor)
            cursor.execute("SET lock_timeout = '30s'")
            cursor.execute(truncate_sql)
            break
        except pgdriver.OperationalError as e:
            conn.rollback()
            if attempt == attempts:
                raise
            logger.warning(
                "TRUNCATE attempt %d/%d hit lock timeout (in-flight simulator "
                "cycle?) — settling %ds then retrying: %s",
                attempt, attempts, settle_seconds, e)
            time.sleep(settle_seconds)
    results['deleted']['transactional_tables'] = 'truncated'
    conn.commit()
    logger.info("Transactional tables truncated")


def _update_simulation_config_defaults(conn, cursor):
    """Update simulation_config defaults that may have been seeded with old values."""
    logger.info("Updating simulation_config defaults...")
    cursor.execute("""
        UPDATE simulation_config
        SET config_value = '0.20', default_value = '0.20'
        WHERE config_key = 'mes_downtime_probability'
          AND config_value::float < 0.20
    """)
    cursor.execute("""
        UPDATE simulation_config
        SET config_value = '4', default_value = '4'
        WHERE config_key = 'orders_per_cycle_min'
          AND config_value::int < 4
    """)
    cursor.execute("""
        UPDATE simulation_config
        SET config_value = '8', default_value = '8'
        WHERE config_key = 'orders_per_cycle_max'
          AND config_value::int < 8
    """)
    conn.commit()


def _delete_old_inventory_items(conn, cursor, results):
    """Steps 10-11: clear old product refs, then delete old inventory items."""
    # 10. Clear product_id references from inventory_items that point to old products
    logger.info("Clearing product references from old inventory items...")
    cursor.execute("""
        UPDATE inventory_items SET product_id = NULL 
        WHERE item_code LIKE 'ITEM%' AND product_id IS NOT NULL
    """)
    results['deleted']['cleared_product_refs'] = cursor.rowcount
    conn.commit()

    # 11. Delete old inventory items (ITEM% pattern and old naming conventions)
    logger.info("Deleting old inventory items...")
    cursor.execute("DELETE FROM inventory_items WHERE item_code LIKE 'ITEM%'")
    results['deleted']['old_inventory_items'] = cursor.rowcount

    # Also delete old finished goods with deprecated naming conventions
    # FG-BEV-*, FG-JUC-*, FG-WAT-PURE-* (old naming from before Jan 18, 2026)
    # FG-APPLE-500 (bug artifact from Jan 18, 2026 - should be FG-APPLE-1000)
    logger.info("Deleting deprecated finished goods items...")
    cursor.execute("""
        DELETE FROM inventory_items 
        WHERE item_code IN (
            'FG-BEV-COLA-500', 'FG-BEV-COLA-1000', 'FG-BEV-LEMON-500', 'FG-BEV-ORANGE-500', 
            'FG-JUC-APPLE-1000', 'FG-JUC-ORANGE-1000',
            'FG-WAT-PURE-500', 'FG-WAT-PURE-1000', 'FG-APPLE-500'
        )
    """)
    results['deleted']['deprecated_finished_goods'] = cursor.rowcount
    conn.commit()


def _delete_old_products_and_orphans(conn, cursor, results):
    """Steps 12-14: drop old-product BOMs/products and orphaned balances."""
    # 12. Delete BOM records for old products
    logger.info("Deleting BOM records for old products...")
    cursor.execute("""
        DELETE FROM bill_of_materials 
        WHERE product_id IN (SELECT product_id FROM products WHERE sku LIKE 'PROD%')
    """)
    results['deleted']['old_product_bom'] = cursor.rowcount
    conn.commit()

    # 13. Delete old products without BOM (PROD% pattern)
    logger.info("Deleting old products without BOM...")
    cursor.execute("DELETE FROM products WHERE sku LIKE 'PROD%'")
    results['deleted']['old_products'] = cursor.rowcount
    conn.commit()

    # 14. Clean up any remaining orphaned inventory balances
    logger.info("Cleaning up orphaned inventory balances...")
    cursor.execute("""
        DELETE FROM inventory_balances 
        WHERE item_id NOT IN (SELECT item_id FROM inventory_items)
    """)
    results['deleted']['orphaned_balances'] = cursor.rowcount
    conn.commit()


def cleanup_old_data():
    """Clean up all old transactional data and old reference data without BOM."""
    logger.info("Starting cleanup of old data...")

    # Pause the simulator first so its inserts/updates don't hold row locks that
    # would block or deadlock the TRUNCATE. Always re-enabled in the finally block.
    paused_rules = _pause_simulator()

    conn = None
    cursor = None
    try:
        creds = get_db_credentials()
        conn = pgdriver.connect(
            host=creds['host'], port=creds['port'], database=creds['database'],
            user=creds['user'], password=creds['password'], connect_timeout=30
        )
        conn.autocommit = False
        cursor = conn.cursor()

        results = {'deleted': {}, 'before': {}, 'after': {}}

        results['before'] = _table_counts(cursor)
        logger.info(f"Before cleanup: {results['before']}")

        _truncate_transactional_tables(conn, cursor, results)
        _update_simulation_config_defaults(conn, cursor)
        _delete_old_inventory_items(conn, cursor, results)
        _delete_old_products_and_orphans(conn, cursor, results)

        results['after'] = _table_counts(cursor)
        logger.info(f"After cleanup: {results['after']}")
        logger.info(f"Cleanup complete: {results['deleted']}")

        return results

    except Exception as e:
        if conn is not None:
            conn.rollback()
        logger.error(f"Error during cleanup: {e}")
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
        # Always resume the simulator, even if cleanup failed.
        _resume_simulator(paused_rules)


# Raw materials are reset to lower values so POs trigger after ~10 minutes.
# With 1-min cycles and ~3 orders/cycle consuming ~50 units each of materials,
# we need ~500-1000 units to last 10 cycles before hitting reorder point.
_RAW_MATERIAL_BALANCES = [
    ("BTL-PET-500", "WH-RAW", 800), ("BTL-PET-1000", "WH-RAW", 600),
    ("CAP-28MM", "WH-RAW", 800), ("CAP-38MM", "WH-RAW", 600),
    ("LBL-COLA", "WH-RAW", 400), ("LBL-LEMON", "WH-RAW", 300),
    ("LBL-ORANGE", "WH-RAW", 300), ("LBL-APPLE", "WH-RAW", 250),
    ("LBL-OJ", "WH-RAW", 250), ("LBL-WATER", "WH-RAW", 400),
    ("CONC-COLA", "WH-RAW", 150), ("CONC-LEMON", "WH-RAW", 100),
    ("CONC-ORANGE", "WH-RAW", 100), ("CONC-APPLE", "WH-RAW", 80),
    ("CONC-OJ", "WH-RAW", 80), ("WATER-PURIFIED", "WH-RAW", 2000),
    ("CO2-FOOD", "WH-RAW", 150),
]

# Reorder points lowered for the demo (POs trigger after ~10 min).
_REORDER_UPDATES = [
    ("BTL-PET-500", 400, 2000), ("BTL-PET-1000", 300, 1500),
    ("CAP-28MM", 400, 2000), ("CAP-38MM", 300, 1500),
    ("LBL-COLA", 200, 1000), ("LBL-LEMON", 150, 800),
    ("LBL-ORANGE", 150, 800), ("LBL-APPLE", 100, 500),
    ("LBL-OJ", 100, 500), ("LBL-WATER", 200, 1000),
    ("CONC-COLA", 50, 200), ("CONC-LEMON", 40, 150),
    ("CONC-ORANGE", 40, 150), ("CONC-APPLE", 30, 100),
    ("CONC-OJ", 30, 100), ("WATER-PURIFIED", 1000, 5000),
    ("CO2-FOOD", 50, 200),
]

_FINISHED_GOOD_BALANCES_SQL = """
            SELECT ii.item_code, ib.quantity_on_hand
            FROM inventory_balances ib
            JOIN inventory_items ii ON ib.item_id = ii.item_id
            WHERE ii.item_type = 'FINISHED_GOOD'
        """


def _finished_good_balances(cursor) -> dict:
    cursor.execute(_FINISHED_GOOD_BALANCES_SQL)
    return {row[0]: row[1] for row in cursor.fetchall()}


def reset_inventory_balances():
    """Reset inventory balances to initial seed values and update reorder points."""
    logger.info("Resetting inventory balances to initial values...")
    
    creds = get_db_credentials()
    conn = pgdriver.connect(
        host=creds['host'], port=creds['port'], database=creds['database'],
        user=creds['user'], password=creds['password'], connect_timeout=30
    )
    conn.autocommit = False
    cursor = conn.cursor()
    
    results = {'reset': {}, 'before': {}, 'after': {}, 'reorder_points': {}}
    
    try:
        # Get current finished goods balances before reset
        results['before'].update(_finished_good_balances(cursor))
        _reset_balances_and_reorder_points(cursor, results)
        conn.commit()
        
        # Also reset simulation_config to correct defaults
        config_resets = [
            ('orders_per_cycle_min', '4'),
            ('orders_per_cycle_max', '8'),
            ('mes_downtime_probability', '0.20'),
        ]
        for key, val in config_resets:
            cursor.execute("""
                UPDATE simulation_config SET config_value = %s, default_value = %s
                WHERE config_key = %s
            """, (val, val, key))
        conn.commit()
        
        # Get finished goods balances after reset
        results['after'].update(_finished_good_balances(cursor))
        
        logger.info(f"Reset complete: {results}")
        return results
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error resetting balances: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


def _reset_balances_and_reorder_points(cursor, results: dict) -> None:
    """The raw-material and finished-good balances and the reorder points,
    recorded in *results* (one transaction with the caller's commit)."""
    for item_code, loc_code, qty in _RAW_MATERIAL_BALANCES:
        cursor.execute("""
            UPDATE inventory_balances ib
            SET quantity_on_hand = %s, quantity_allocated = 0, last_transaction_date = CURRENT_TIMESTAMP
            FROM inventory_items ii, inventory_locations il
            WHERE ib.item_id = ii.item_id AND ib.location_id = il.location_id
            AND ii.item_code = %s AND il.location_code = %s
        """, (qty, item_code, loc_code))
        if cursor.rowcount > 0:
            results['reset'][item_code] = qty

    # Reset finished goods to 0 (or small positive amount for demo)
    cursor.execute("""
        UPDATE inventory_balances ib
        SET quantity_on_hand = 100, quantity_allocated = 0, last_transaction_date = CURRENT_TIMESTAMP
        FROM inventory_items ii
        WHERE ib.item_id = ii.item_id AND ii.item_type = 'FINISHED_GOOD'
    """)
    results['reset']['finished_goods_reset_to'] = 100
    results['reset']['finished_goods_count'] = cursor.rowcount

    for item_code, reorder_point, reorder_qty in _REORDER_UPDATES:
        cursor.execute("""
            UPDATE inventory_items SET reorder_point = %s, reorder_quantity = %s
            WHERE item_code = %s
        """, (reorder_point, reorder_qty, item_code))
        if cursor.rowcount > 0:
            results['reorder_points'][item_code] = {'reorder_point': reorder_point, 'reorder_qty': reorder_qty}


def run_warmup_cycles(num_cycles=10):
    """Run N simulation cycles to warm up the ERP data after a reset.
    Gives the demo a realistic starting state with orders, production, and shipments.

    A reply of ``simulator: busy`` (another cycle still running its steps) is
    waited out and retried, never counted (erp_simulation/cycle_result.py).
    """
    from erp_simulation import cycle_result

    region = os.environ.get('AWS_REGION', 'us-east-1')
    sim_function = os.environ.get('SIM_FUNCTION_NAME', 'erp-simulation-orchestrator')
    client = boto3.client('lambda', region_name=region)
    payload = json.dumps({"action": "single_cycle"}).encode()

    def invoke_once():
        try:
            resp = client.invoke(FunctionName=sim_function, InvocationType='RequestResponse',
                                 Payload=payload)
            return resp.get('FunctionError'), json.loads(resp['Payload'].read() or b'null')
        except Exception as e:  # a failed invoke is a failed cycle; the warmup goes on
            logger.warning(f"Warmup cycle invoke failed: {type(e).__name__}: {e}")
            return 'InvokeError', None

    def on_cycle(n, outcome):
        logger.info(f"Warmup cycle {n}/{num_cycles}: {outcome}")

    summary = cycle_result.run_cycles(invoke_once, num_cycles, on_cycle=on_cycle)
    logger.info(f"Warmup complete: {summary['ran']}/{num_cycles} cycles ran "
                f"({summary['failed']} failed, waited {summary['waited_seconds']} s for a busy simulator)")
    return {'warmup_cycles': summary['ran'], 'warmup_failed': summary['failed'],
            'warmup_gave_up': summary['gave_up']}


def verify_data():
    """Verify current data state."""
    logger.info("Verifying data state...")
    
    creds = get_db_credentials()
    conn = pgdriver.connect(
        host=creds['host'], port=creds['port'], database=creds['database'],
        user=creds['user'], password=creds['password'], connect_timeout=30
    )
    cursor = conn.cursor()
    
    results = {}
    
    # Count records in key tables
    tables = [
        'customers', 'suppliers', 'products', 'inventory_items', 'inventory_locations', 
        'bill_of_materials', 'inventory_balances',
        'customer_orders', 'production_runs', 'shipments', 'invoices'
    ]
    
    for table in tables:
        cursor.execute(_count_sql(table))
        results[table] = cursor.fetchone()[0]
    
    # Check for old PROD% products
    cursor.execute("SELECT COUNT(*) FROM products WHERE sku LIKE 'PROD%'")
    results['old_products_remaining'] = cursor.fetchone()[0]
    
    # Check for old ITEM% inventory items
    cursor.execute("SELECT COUNT(*) FROM inventory_items WHERE item_code LIKE 'ITEM%'")
    results['old_items_remaining'] = cursor.fetchone()[0]
    
    # Check for BEV% products (new beverage products)
    cursor.execute("SELECT COUNT(*) FROM products WHERE sku LIKE 'BEV%' OR sku LIKE 'JUC%' OR sku LIKE 'WAT%'")
    results['beverage_products'] = cursor.fetchone()[0]
    
    # Check BOM records for beverage products
    cursor.execute("""
        SELECT COUNT(*) FROM bill_of_materials bom
        JOIN products p ON bom.product_id = p.product_id
        WHERE p.sku LIKE 'BEV%' OR p.sku LIKE 'JUC%' OR p.sku LIKE 'WAT%'
    """)
    results['beverage_bom_records'] = cursor.fetchone()[0]
    
    cursor.close()
    conn.close()
    
    logger.info(f"Verification results: {results}")
    return results


def clear_events_only():
    """Clear ONLY the simulation_events table - nothing else."""
    logger.info("Clearing simulation_events table only...")
    
    creds = get_db_credentials()
    conn = pgdriver.connect(
        host=creds['host'], port=creds['port'], database=creds['database'],
        user=creds['user'], password=creds['password'], connect_timeout=30
    )
    cursor = conn.cursor()
    
    try:
        cursor.execute("DELETE FROM simulation_events")
        deleted_count = cursor.rowcount
        conn.commit()
        logger.info(f"Deleted {deleted_count} rows from simulation_events")
        return {"simulation_events_deleted": deleted_count}
    except Exception as e:
        conn.rollback()
        logger.error(f"Error clearing simulation_events: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


def lambda_handler(event, context):
    """Lambda handler."""
    try:
        action = event.get('action', 'cleanup')
        logger.info(f"Lambda invoked with action: {action}")
        
        if action == 'cleanup':
            result = cleanup_old_data()
        elif action == 'verify':
            result = verify_data()
        elif action == 'reset_balances':
            result = reset_inventory_balances()
        elif action == 'clear_events_only':
            result = clear_events_only()
        elif action == 'warmup':
            num_cycles = event.get('num_cycles', 10)
            result = run_warmup_cycles(num_cycles)
        else:
            result = {"error": f"Unknown action: {action}. Use 'cleanup', 'verify', 'reset_balances', 'warmup', or 'clear_events_only'"}
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'action': action,
                'timestamp': datetime.now().isoformat(),
                'result': result
            })
        }
    except Exception as e:
        logger.error(f"Lambda error: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
                'timestamp': datetime.now().isoformat()
            })
        }
