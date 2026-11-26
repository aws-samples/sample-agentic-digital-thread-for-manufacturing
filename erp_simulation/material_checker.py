# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Material Availability Checker for ERP Simulation System.

Checks if sufficient raw materials are available before releasing production runs.
This prevents negative inventory by ensuring materials exist before production starts.
"""

from typing import Dict, Any, List, Tuple, Optional

from .logging_config import get_logger

logger = get_logger(__name__)


class MaterialChecker:
    """
    Checks material availability for production runs.
    
    Validates that all required raw materials are available in sufficient
    quantities before production can be released to MES.
    """
    
    def __init__(self):
        """Initialize the material checker."""
        logger.info("Material checker initialized")
    
    def check_material_availability(
        self, 
        conn, 
        run_id: str
    ) -> Tuple[bool, Optional[List[Dict[str, Any]]]]:
        """
        Check if all materials are available for a production run.
        
        Args:
            conn: Database connection.
            run_id: Production run ID to check.
            
        Returns:
            Tuple of (materials_available: bool, shortages: List[Dict] or None)
            - materials_available: True if all materials available, False otherwise
            - shortages: List of shortage details if materials_available is False, None otherwise
        """
        cursor = conn.cursor()
        
        try:
            # Get production run details
            cursor.execute("""
                SELECT pr.run_id, pr.run_number, pr.product_id, pr.planned_quantity,
                       p.sku, p.product_name
                FROM production_runs pr
                JOIN products p ON pr.product_id = p.product_id
                WHERE pr.run_id = %s
            """, (run_id,))
            
            run_row = cursor.fetchone()
            if not run_row:
                logger.warning(f"Production run {run_id} not found")
                return False, None
            
            run_id, run_number, product_id, planned_quantity, sku, product_name = run_row
            
            # Get BOM for the product
            cursor.execute("""
                SELECT bom.component_item_id, bom.quantity_required,
                       ii.item_code, ii.item_name
                FROM bill_of_materials bom
                JOIN inventory_items ii ON bom.component_item_id = ii.item_id
                WHERE bom.product_id = %s
                  AND bom.is_active = TRUE
            """, (product_id,))
            
            bom_items = cursor.fetchall()
            
            if not bom_items:
                logger.warning(f"No BOM found for product {sku} in run {run_number}")
                return False, None
            
            # Check availability for each material
            shortages = []
            all_available = True
            
            for component_item_id, quantity_required, item_code, item_name in bom_items:
                # Calculate required quantity
                required_quantity = float(quantity_required) * planned_quantity
                
                # Get available inventory (sum across all locations)
                cursor.execute("""
                    SELECT COALESCE(SUM(quantity_on_hand), 0) as available
                    FROM inventory_balances
                    WHERE item_id = %s
                """, (component_item_id,))
                
                available_quantity = float(cursor.fetchone()[0])
                
                # Check if sufficient
                if available_quantity < required_quantity:
                    shortage = required_quantity - available_quantity
                    shortages.append({
                        'item_id': component_item_id,
                        'item_code': item_code,
                        'item_name': item_name,
                        'required': required_quantity,
                        'available': available_quantity,
                        'shortage': shortage
                    })
                    all_available = False
                    
                    logger.debug(
                        f"Material shortage for run {run_number}: "
                        f"{item_code} needs {required_quantity:.2f}, "
                        f"only {available_quantity:.2f} available (short {shortage:.2f})"
                    )
            
            if all_available:
                logger.debug(f"All materials available for run {run_number}")
                return True, None
            else:
                logger.info(
                    f"Material shortages prevent release of run {run_number}: "
                    f"{len(shortages)} items short"
                )
                return False, shortages
        
        except Exception as e:
            logger.error(f"Material availability check failed for run {run_id}: {e}", exc_info=True)
            return False, None
        finally:
            cursor.close()

