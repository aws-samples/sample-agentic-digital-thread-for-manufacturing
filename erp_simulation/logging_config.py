# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Logging infrastructure with CloudWatch integration.

Provides structured logging with support for local file logging and CloudWatch Logs.
"""

import logging
import sys
from datetime import datetime, timezone
from typing import Optional
import json

from .config import CloudWatchConfig, get_config


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        """
        Format log record as JSON.
        
        Args:
            record: Log record to format.
            
        Returns:
            JSON-formatted log string.
        """
        log_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        
        # Add extra fields if present
        if hasattr(record, 'extra_fields'):
            log_data.update(record.extra_fields)
        
        return json.dumps(log_data)


class CloudWatchHandler(logging.Handler):
    """
    Custom handler for sending logs to CloudWatch Logs.
    
    Note: This is a placeholder implementation. In production, use
    watchtower or boto3 CloudWatch Logs client for actual integration.
    """
    
    def __init__(self, config: CloudWatchConfig):
        """
        Initialize CloudWatch handler.
        
        Args:
            config: CloudWatch configuration.
        """
        super().__init__()
        self.config = config
        self.log_stream_name = f"{config.log_stream_prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        
        # Initialize CloudWatch client if enabled
        if config.enabled:
            try:
                import boto3
                self.client = boto3.client('logs', region_name=config.region)
                self._ensure_log_group_exists()
                self._ensure_log_stream_exists()
            except ImportError:
                logging.warning("boto3 not available, CloudWatch logging disabled")
                self.client = None
            except Exception as e:
                logging.warning(f"Failed to initialize CloudWatch client: {e}")
                self.client = None
        else:
            self.client = None
    
    def _ensure_log_group_exists(self) -> None:
        """Create log group if it doesn't exist."""
        if not self.client:
            return
        
        try:
            self.client.create_log_group(logGroupName=self.config.log_group_name)
            logging.info(f"Created CloudWatch log group: {self.config.log_group_name}")
        except self.client.exceptions.ResourceAlreadyExistsException:
            pass  # Log group already exists
        except Exception as e:
            logging.error(f"Failed to create log group: {e}")
    
    def _ensure_log_stream_exists(self) -> None:
        """Create log stream if it doesn't exist."""
        if not self.client:
            return
        
        try:
            self.client.create_log_stream(
                logGroupName=self.config.log_group_name,
                logStreamName=self.log_stream_name
            )
            logging.info(f"Created CloudWatch log stream: {self.log_stream_name}")
        except self.client.exceptions.ResourceAlreadyExistsException:
            pass  # Log stream already exists
        except Exception as e:
            logging.error(f"Failed to create log stream: {e}")
    
    def emit(self, record: logging.LogRecord) -> None:
        """
        Emit a log record to CloudWatch.
        
        Args:
            record: Log record to emit.
        """
        if not self.client:
            return
        
        try:
            log_event = {
                'logGroupName': self.config.log_group_name,
                'logStreamName': self.log_stream_name,
                'logEvents': [
                    {
                        'timestamp': int(record.created * 1000),
                        'message': self.format(record)
                    }
                ]
            }
            self.client.put_log_events(**log_event)
        except Exception:  # nosec B110 - intentional fallback, no recovery action needed
            # Don't let logging errors crash the application
            # Don't log the error to avoid recursion
            pass


def setup_logging(
    level: str = 'INFO',
    log_file: Optional[str] = None,
    enable_cloudwatch: Optional[bool] = None,
    json_format: bool = False
) -> None:
    """
    Set up logging configuration.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file: Optional file path for file logging.
        enable_cloudwatch: Whether to enable CloudWatch logging. If None, uses config.
        json_format: Whether to use JSON formatting for logs.
    """
    # Get CloudWatch config
    cloudwatch_config = get_config().cloudwatch
    if enable_cloudwatch is None:
        enable_cloudwatch = cloudwatch_config.enabled
    
    # Create root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))
    
    # Remove existing handlers
    root_logger.handlers.clear()
    
    # Choose formatter
    if json_format:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, level.upper()))
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(getattr(logging, level.upper()))
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        logging.info(f"File logging enabled: {log_file}")
    
    # CloudWatch handler (optional)
    if enable_cloudwatch:
        try:
            cloudwatch_handler = CloudWatchHandler(cloudwatch_config)
            cloudwatch_handler.setLevel(getattr(logging, level.upper()))
            cloudwatch_handler.setFormatter(JSONFormatter())  # Always use JSON for CloudWatch
            root_logger.addHandler(cloudwatch_handler)
            logging.info(f"CloudWatch logging enabled: {cloudwatch_config.log_group_name}")
        except Exception as e:
            logging.warning(f"Failed to enable CloudWatch logging: {e}")
    
    logging.info(f"Logging configured: level={level}, json_format={json_format}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance.
    
    Args:
        name: Logger name (typically __name__).
        
    Returns:
        Logger instance.
    """
    return logging.getLogger(name)


class MetricsPublisher:
    """
    Publisher for CloudWatch metrics.
    
    Provides methods to publish custom metrics to CloudWatch.
    """
    
    def __init__(self, config: Optional[CloudWatchConfig] = None):
        """
        Initialize metrics publisher.
        
        Args:
            config: CloudWatch configuration. If None, uses global config.
        """
        self.config = config or get_config().cloudwatch
        self.client = None
        
        if self.config.enabled:
            try:
                import boto3
                self.client = boto3.client('cloudwatch', region_name=self.config.region)
                logging.info(f"CloudWatch metrics publisher initialized: namespace={self.config.metrics_namespace}")
            except ImportError:
                logging.warning("boto3 not available, CloudWatch metrics disabled")
            except Exception as e:
                logging.warning(f"Failed to initialize CloudWatch metrics client: {e}")


# Global metrics publisher instance
_metrics_publisher: Optional[MetricsPublisher] = None
