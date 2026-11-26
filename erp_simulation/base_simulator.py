# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Base Simulator Class with Configuration Loading.

Provides common config loading functionality for all simulators.
"""

from typing import Any, Union
from .logging_config import get_logger

logger = get_logger(__name__)


class BaseSimulator:
    """
    Base class for all simulators with config loading capability.
    
    Provides _load_config_value() method that reads from simulation_config table
    with fallback to default values.
    """
    
    def _load_config_value(
        self, 
        cursor, 
        key: str, 
        default_value: Union[int, float, str, bool], 
        data_type: str
    ) -> Any:
        """
        Load configuration value from database or use default.
        
        Args:
            cursor: Database cursor.
            key: Configuration key.
            default_value: Default value if not found in database.
            data_type: Data type ('int', 'float', 'string', 'boolean').
            
        Returns:
            Configuration value with correct type.
        """
        try:
            cursor.execute("""
                SELECT config_value FROM simulation_config WHERE config_key = %s
            """, (key,))
            row = cursor.fetchone()
            
            if row:
                value_str = row[0]
                
                if data_type == 'int':
                    return int(value_str)
                elif data_type == 'float':
                    return float(value_str)
                elif data_type == 'boolean':
                    return value_str.lower() in ('true', '1', 'yes')
                else:  # string
                    return value_str
            
            # Not found in database, use default
            return default_value
            
        except Exception as e:
            # If config table doesn't exist or any error, use default
            logger.debug(f"Config lookup failed for {key}, using default: {e}")
            return default_value
