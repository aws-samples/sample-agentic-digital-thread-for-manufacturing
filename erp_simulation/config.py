# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Configuration management for ERP Simulation System.

Handles database credentials, simulation parameters, and environment-specific settings.
"""

import os
from dataclasses import dataclass
from typing import Optional
import json


@dataclass
class DatabaseConfig:
    """Database connection configuration."""
    host: str
    port: int
    database: str
    user: str
    password: str
    min_pool_size: int = 2
    max_pool_size: int = 10
    connection_timeout: int = 30
    
    @classmethod
    def from_env(cls) -> 'DatabaseConfig':
        """Load database configuration from environment variables."""
        return cls(
            host=os.getenv('DB_HOST', 'localhost'),
            port=int(os.getenv('DB_PORT', '5432')),
            database=os.getenv('DB_NAME', 'erp_simulation'),
            user=os.getenv('DB_USER', 'postgres'),
            password=os.getenv('DB_PASSWORD', ''),
            min_pool_size=int(os.getenv('DB_MIN_POOL_SIZE', '2')),
            max_pool_size=int(os.getenv('DB_MAX_POOL_SIZE', '10')),
            connection_timeout=int(os.getenv('DB_CONNECTION_TIMEOUT', '30'))
        )
    
    @classmethod
    def from_secrets_manager(cls, secret_name: str, region: str = 'us-east-1') -> 'DatabaseConfig':
        """Load database configuration from AWS Secrets Manager."""
        try:
            import boto3
            from botocore.exceptions import ClientError
            
            client = boto3.client('secretsmanager', region_name=region)
            response = client.get_secret_value(SecretId=secret_name)
            secret = json.loads(response['SecretString'])
            
            return cls(
                host=secret['host'],
                port=int(secret.get('port', 5432)),
                # Aurora secrets use 'dbname', not 'database'
                database=secret.get('dbname', secret.get('database', 'erp_simulation')),
                user=secret['username'],
                password=secret['password'],
                min_pool_size=int(secret.get('min_pool_size', 2)),
                max_pool_size=int(secret.get('max_pool_size', 10)),
                connection_timeout=int(secret.get('connection_timeout', 30))
            )
        except (ImportError, ClientError) as e:
            raise RuntimeError(f"Failed to load secrets from AWS Secrets Manager: {e}")


@dataclass
class SimulationConfig:
    """Simulation engine configuration."""
    cycle_interval_seconds: int = 60  # 1 minute for active demo
    orders_per_cycle_min: int = 4  # Min 4 orders per cycle to keep lines busy
    orders_per_cycle_max: int = 8  # Max 8 orders per cycle
    production_time_hours_min: int = 1
    production_time_hours_max: int = 3
    shipping_time_days_min: int = 1
    shipping_time_days_max: int = 3
    payment_delay_days_min: int = 15
    payment_delay_days_max: int = 45
    reorder_point_multiplier: float = 1.5
    
    # MES Configuration
    target_rate_per_minute: int = 120  # bottles per minute
    quality_check_interval_minutes: int = 30
    downtime_probability: float = 0.1  # 10% chance per work order
    
    @classmethod
    def from_env(cls) -> 'SimulationConfig':
        """Load simulation configuration from environment variables."""
        return cls(
            cycle_interval_seconds=int(os.getenv('SIM_CYCLE_INTERVAL', '60')),
            orders_per_cycle_min=int(os.getenv('SIM_ORDERS_MIN', '4')),
            orders_per_cycle_max=int(os.getenv('SIM_ORDERS_MAX', '8')),
            production_time_hours_min=int(os.getenv('SIM_PROD_TIME_MIN', '1')),
            production_time_hours_max=int(os.getenv('SIM_PROD_TIME_MAX', '3')),
            shipping_time_days_min=int(os.getenv('SIM_SHIP_TIME_MIN', '1')),
            shipping_time_days_max=int(os.getenv('SIM_SHIP_TIME_MAX', '3')),
            payment_delay_days_min=int(os.getenv('SIM_PAYMENT_DELAY_MIN', '15')),
            payment_delay_days_max=int(os.getenv('SIM_PAYMENT_DELAY_MAX', '45')),
            reorder_point_multiplier=float(os.getenv('SIM_REORDER_MULTIPLIER', '1.5')),
            target_rate_per_minute=int(os.getenv('MES_TARGET_RATE', '120')),
            quality_check_interval_minutes=int(os.getenv('MES_QC_INTERVAL', '30')),
            downtime_probability=float(os.getenv('MES_DOWNTIME_PROB', '0.1'))
        )


@dataclass
class CloudWatchConfig:
    """CloudWatch logging and metrics configuration."""
    enabled: bool = True
    log_group_name: str = '/erp-simulation/engine'
    log_stream_prefix: str = 'simulation'
    region: str = 'us-east-1'
    metrics_namespace: str = 'ERPSimulation'
    
    @classmethod
    def from_env(cls) -> 'CloudWatchConfig':
        """Load CloudWatch configuration from environment variables."""
        return cls(
            enabled=os.getenv('CLOUDWATCH_ENABLED', 'true').lower() == 'true',
            log_group_name=os.getenv('CLOUDWATCH_LOG_GROUP', '/erp-simulation/engine'),
            log_stream_prefix=os.getenv('CLOUDWATCH_LOG_STREAM_PREFIX', 'simulation'),
            # AWS_REGION is automatically set by Lambda runtime
            region=os.getenv('AWS_REGION', 'us-east-1'),
            metrics_namespace=os.getenv('CLOUDWATCH_METRICS_NAMESPACE', 'ERPSimulation')
        )


class Config:
    """Main configuration class combining all configuration sections."""
    
    def __init__(
        self,
        database: Optional[DatabaseConfig] = None,
        simulation: Optional[SimulationConfig] = None,
        cloudwatch: Optional[CloudWatchConfig] = None
    ):
        self.database = database or DatabaseConfig.from_env()
        self.simulation = simulation or SimulationConfig.from_env()
        self.cloudwatch = cloudwatch or CloudWatchConfig.from_env()
    
    @classmethod
    def from_env(cls) -> 'Config':
        """
        Load complete configuration from environment variables.
        
        WARNING: This method loads database password from environment variables.
        For production use, prefer from_secrets_manager() for better security.
        """
        return cls(
            database=DatabaseConfig.from_env(),
            simulation=SimulationConfig.from_env(),
            cloudwatch=CloudWatchConfig.from_env()
        )
    
    @classmethod
    def from_secrets_manager(cls, secret_name: str = None, region: str = None) -> 'Config':
        """
        Load configuration with database credentials from AWS Secrets Manager (RECOMMENDED).
        
        This is the recommended method for production deployments as it:
        - Keeps passwords encrypted at rest
        - Supports automatic rotation
        - Provides audit logging
        - Uses IAM-based access control
        
        Args:
            secret_name: Name of the secret in Secrets Manager. 
                        If not provided, reads from DB_SECRET_NAME env var.
            region: AWS region. If not provided, reads from AWS_REGION env var.
                   In Lambda, AWS_REGION is automatically set by the runtime.
        
        Returns:
            Config instance with credentials from Secrets Manager
        """
        secret_name = secret_name or os.getenv('DB_SECRET_NAME', 'erp-simulation/database')
        # AWS_REGION is automatically set by Lambda runtime
        region = region or os.getenv('AWS_REGION', 'us-east-1')
        
        return cls(
            database=DatabaseConfig.from_secrets_manager(secret_name, region),
            simulation=SimulationConfig.from_env(),
            cloudwatch=CloudWatchConfig.from_env()
        )
    
    def validate(self) -> None:
        """Validate configuration values."""
        # Validate database config
        if not self.database.host:
            raise ValueError("Database host is required")
        if not self.database.database:
            raise ValueError("Database name is required")
        if not self.database.user:
            raise ValueError("Database user is required")
        if self.database.min_pool_size < 1:
            raise ValueError("Minimum pool size must be at least 1")
        if self.database.max_pool_size < self.database.min_pool_size:
            raise ValueError("Maximum pool size must be >= minimum pool size")
        
        # Validate simulation config
        if self.simulation.cycle_interval_seconds < 1:
            raise ValueError("Cycle interval must be at least 1 second")
        if self.simulation.orders_per_cycle_min < 0:
            raise ValueError("Minimum orders per cycle cannot be negative")
        if self.simulation.orders_per_cycle_max < self.simulation.orders_per_cycle_min:
            raise ValueError("Maximum orders must be >= minimum orders")
        if self.simulation.reorder_point_multiplier <= 0:
            raise ValueError("Reorder point multiplier must be positive")
        
        # Validate CloudWatch config
        if self.cloudwatch.enabled and not self.cloudwatch.log_group_name:
            raise ValueError("CloudWatch log group name is required when enabled")


# Global configuration instance
_config: Optional[Config] = None


def get_config(use_secrets_manager: bool = True) -> Config:
    """
    Get the global configuration instance.
    
    Args:
        use_secrets_manager: If True (default), use AWS Secrets Manager for database credentials.
                           If False, fall back to environment variables (not recommended for production).
    
    Returns:
        Config instance
    """
    global _config
    if _config is None:
        if use_secrets_manager:
            try:
                _config = Config.from_secrets_manager()
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to load from Secrets Manager: {e}. Falling back to environment variables.")
                _config = Config.from_env()
        else:
            _config = Config.from_env()
        _config.validate()
    return _config

