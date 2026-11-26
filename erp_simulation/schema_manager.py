# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
ERP Simulation System - Schema Manager
Handles database schema creation and migration.
"""

import logging
from pathlib import Path
from typing import List

from .database import get_connection
from .utils import compose_sql
from .logging_config import setup_logging

logger = logging.getLogger(__name__)

# The tables drop_all_tables drops, dependents before what they reference.
_DROP_ORDER = (
    'payments', 'invoice_line_items', 'invoices', 'shipment_items', 'shipments',
    'mes_downtime_events', 'mes_material_consumption', 'mes_quality_checks', 'mes_production_events',
    'mes_work_orders', 'production_materials', 'production_runs', 'po_receipts', 'po_line_items',
    'purchase_orders', 'order_line_items', 'customer_orders', 'bill_of_materials',
    'inventory_transactions', 'inventory_balances', 'inventory_locations', 'inventory_items',
    'products', 'suppliers', 'customers',
)

class SchemaManager:
    """Manages database schema creation and updates."""
    
    def __init__(self, connection=None):
        """
        Initialize schema manager.
        
        Args:
            connection: Optional database connection. If not provided, will create one.
        """
        self.connection = connection
        self.schema_dir = Path(__file__).parent / 'schema'
        
    def get_schema_files(self) -> List[Path]:
        """
        Get list of schema SQL files in order.
        
        Returns:
            List of Path objects for schema files
        """
        schema_files = [
            '01_customers_suppliers.sql',
            '02_products_inventory.sql',
            '03_order_management.sql',
            '04_procurement.sql',
            '05_production.sql',
            '06_shipping_financial.sql',
            '07_mes_work_orders.sql',
            '08_mes_tracking.sql'
        ]
        
        return [self.schema_dir / filename for filename in schema_files]
    
    def execute_sql_file(self, filepath: Path, connection=None) -> bool:
        """
        Execute SQL commands from a file.
        
        Args:
            filepath: Path to SQL file
            connection: Database connection to use
            
        Returns:
            True if successful, False otherwise
        """
        conn = connection or self.connection
        if not conn:
            logger.error("No database connection available")
            return False
            
        try:
            with open(filepath, 'r', encoding="utf-8") as f:
                sql_content = f.read()
            
            with conn.cursor() as cursor:
                cursor.execute(sql_content)
                conn.commit()
                
            logger.info(f"Successfully executed {filepath.name}")
            return True
            
        except Exception as e:
            logger.error(f"Error executing {filepath.name}: {e}")
            conn.rollback()
            return False
    
    def create_schema(self, connection=None) -> bool:
        """
        Create all database tables and indexes.
        
        Args:
            connection: Optional database connection
            
        Returns:
            True if all schema files executed successfully
        """
        conn = connection or self.connection
        if not conn:
            logger.error("No database connection available")
            return False
        
        logger.info("Starting schema creation...")
        
        schema_files = self.get_schema_files()
        success = True
        
        for filepath in schema_files:
            if not filepath.exists():
                logger.error(f"Schema file not found: {filepath}")
                success = False
                continue
                
            if not self.execute_sql_file(filepath, conn):
                success = False
                logger.error(f"Failed to execute {filepath.name}")
                # Continue with remaining files even if one fails
        
        if success:
            logger.info("Schema creation completed successfully")
        else:
            logger.warning("Schema creation completed with errors")
            
        return success
    
    def verify_schema(self, connection=None) -> dict:
        """
        Verify that all expected tables exist.
        
        Args:
            connection: Optional database connection
            
        Returns:
            Dictionary with verification results
        """
        conn = connection or self.connection
        if not conn:
            logger.error("No database connection available")
            return {'success': False, 'error': 'No connection'}
        
        expected_tables = [
            'customers',
            'suppliers',
            'products',
            'inventory_items',
            'inventory_locations',
            'inventory_balances',
            'inventory_transactions',
            'bill_of_materials',
            'customer_orders',
            'order_line_items',
            'purchase_orders',
            'po_line_items',
            'po_receipts',
            'production_runs',
            'production_materials',
            'shipments',
            'shipment_items',
            'invoices',
            'invoice_line_items',
            'payments',
            'mes_work_orders',
            'mes_production_events',
            'mes_quality_checks',
            'mes_material_consumption',
            'mes_downtime_events'
        ]
        
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT tablename 
                    FROM pg_tables 
                    WHERE schemaname = 'public'
                    AND tablename = ANY(%s)
                """, (expected_tables,))
                
                existing_tables = [row[0] for row in cursor.fetchall()]
                
            missing_tables = set(expected_tables) - set(existing_tables)
            
            result = {
                'success': len(missing_tables) == 0,
                'expected_count': len(expected_tables),
                'existing_count': len(existing_tables),
                'existing_tables': existing_tables,
                'missing_tables': list(missing_tables)
            }
            
            if result['success']:
                logger.info(f"Schema verification passed: {len(existing_tables)}/{len(expected_tables)} tables exist")
            else:
                logger.warning(f"Schema verification failed: Missing tables: {missing_tables}")
                
            return result
            
        except Exception as e:
            logger.error(f"Error verifying schema: {e}")
            return {'success': False, 'error': str(e)}
    
    def drop_all_tables(self, connection=None, confirm: bool = False) -> bool:
        """
        Drop all ERP simulation tables. USE WITH CAUTION!
        
        Args:
            connection: Optional database connection
            confirm: Must be True to actually drop tables
            
        Returns:
            True if successful
        """
        if not confirm:
            logger.warning("drop_all_tables called without confirmation - no action taken")
            return False
            
        conn = connection or self.connection
        if not conn:
            logger.error("No database connection available")
            return False
        
        try:
            with conn.cursor() as cursor:
                for table in _DROP_ORDER:
                    # compose_sql quotes the table name as an identifier
                    cursor.execute(compose_sql("DROP TABLE IF EXISTS {table} CASCADE", table=table))
                    logger.info(f"Dropped table: {table}")
                    
                conn.commit()
                logger.info("All tables dropped successfully")
                return True
                
        except Exception as e:
            logger.error(f"Error dropping tables: {e}")
            conn.rollback()
            return False


def main():
    """Main function for command-line schema management."""
    import argparse
    
    setup_logging()
    
    parser = argparse.ArgumentParser(description='ERP Simulation Schema Manager')
    parser.add_argument('action', choices=['create', 'verify', 'drop'],
                       help='Action to perform')
    parser.add_argument('--confirm-drop', action='store_true',
                       help='Confirm dropping all tables (required for drop action)')
    
    args = parser.parse_args()
    
    try:
        with get_connection() as conn:
            manager = SchemaManager(conn)
            
            if args.action == 'create':
                success = manager.create_schema()
                if success:
                    print("✓ Schema created successfully")
                    # Verify after creation
                    result = manager.verify_schema()
                    if result['success']:
                        print(f"✓ Verification passed: {result['existing_count']} tables created")
                    else:
                        print(f"✗ Verification failed: {result.get('missing_tables', [])}")
                else:
                    print("✗ Schema creation failed")
                    
            elif args.action == 'verify':
                result = manager.verify_schema()
                if result['success']:
                    print(f"✓ Schema verification passed: {result['existing_count']}/{result['expected_count']} tables exist")
                else:
                    print(f"✗ Schema verification failed")
                    if result.get('missing_tables'):
                        print(f"  Missing tables: {', '.join(result['missing_tables'])}")
                        
            elif args.action == 'drop':
                if not args.confirm_drop:
                    print("✗ Must use --confirm-drop flag to drop tables")
                    return
                    
                print("WARNING: This will drop all ERP simulation tables!")
                response = input("Type 'yes' to confirm: ")
                if response.lower() == 'yes':
                    success = manager.drop_all_tables(confirm=True)
                    if success:
                        print("✓ All tables dropped")
                    else:
                        print("✗ Failed to drop tables")
                else:
                    print("Cancelled")
                    
    except Exception as e:
        logger.error(f"Error: {e}")
        print(f"✗ Error: {e}")


if __name__ == '__main__':
    main()
