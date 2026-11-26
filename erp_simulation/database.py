# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Database connection and management for ERP Simulation System.
Uses the in-repo pgdriver client with connection pooling for the ERP Streamlit dashboard.
"""

import os
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from contextlib import contextmanager
from erp_simulation import pgdriver

logger = logging.getLogger(__name__)

def _safe_error(exc: Exception) -> str:
    """Exception class and SQLSTATE only: driver messages can repeat bound values."""
    return f"{type(exc).__name__} (SQLSTATE {getattr(exc, 'pgcode', None)})"


class DatabaseManager:
    """Database connection manager using pgdriver with connection pooling."""
    
    def __init__(self, config=None):
        """Initialize database manager with configuration."""
        self.config = config
        self._pool = None
    
    @property
    def pool(self):
        """Get or create connection pool."""
        if self._pool is None:
            params = self.get_connection_params()
            self._pool = pgdriver.SimpleConnectionPool(
                minconn=1,
                maxconn=5,
                host=params['host'],
                port=params['port'],
                database=params['database'],
                user=params['user'],
                password=params['password']
            )
        return self._pool
    
    def get_connection_params(self) -> Dict[str, Any]:
        """Get database connection parameters."""
        if os.getenv('USE_SECRETS_MANAGER', 'false').lower() == 'true':
            return self._get_connection_params_from_secrets()
        else:
            return self._get_connection_params_from_env()
    
    def _get_connection_params_from_secrets(self) -> Dict[str, Any]:
        """Get connection parameters from AWS Secrets Manager."""
        import boto3
        from botocore.exceptions import ClientError
        
        secret_name = os.getenv('DB_SECRET_NAME')
        if not secret_name:
            raise RuntimeError("DB_SECRET_NAME environment variable is required but not set")
        
        # AWS_REGION is automatically set by Lambda runtime
        region = os.getenv('AWS_REGION', 'us-east-1')
        
        try:
            client = boto3.client('secretsmanager', region_name=region)
            response = client.get_secret_value(SecretId=secret_name)
            secret = json.loads(response['SecretString'])
            
            return {
                'host': secret['host'],
                'port': int(secret.get('port', 5432)),
                'database': secret.get('database') or secret.get('dbname', 'erp_simulation'),
                'user': secret['username'],
                'password': secret['password']
            }
        except (ClientError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load database credentials from Secrets Manager: {e}")  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
            raise RuntimeError(f"Database configuration error: {e}")
    
    def _get_connection_params_from_env(self) -> Dict[str, Any]:
        """Get connection parameters from environment variables."""
        return {
            'host': os.getenv('DB_HOST', 'localhost'),
            'port': int(os.getenv('DB_PORT', '5432')),
            'database': os.getenv('DB_NAME', 'erp_simulation'),
            'user': os.getenv('DB_USER', 'postgres'),
            'password': os.getenv('DB_PASSWORD', '')
        }
    
    def dedicated_connection(self, *, idle_in_transaction_timeout_s: int, application_name: str):
        """A fresh connection outside the pool; the caller closes it.

        For a lock that must die with its holder: never handed to another
        caller, so a lock on it cannot be re-entered by a reused session, and
        ``idle_in_transaction_session_timeout`` makes the SERVER end the
        session (and every lock it holds) once it has sat idle inside a
        transaction that long, even when the client is frozen with its socket
        open. Connect is bounded (10 s) and TCP keepalives are on.
        """
        params = self.get_connection_params()
        return pgdriver.connect(
            host=params['host'], port=params['port'], dbname=params['database'],
            user=params['user'], password=params['password'],
            connect_timeout=10, application_name=application_name,
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
            options=f"-c idle_in_transaction_session_timeout={int(idle_in_transaction_timeout_s) * 1000}",
        )

    @contextmanager
    def connection(self):
        """Get database connection from pool with auto-commit on success."""
        conn = None
        try:
            conn = self.pool.getconn()
            yield conn
            # Commit on successful completion
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Database operation failed: {_safe_error(e)}")
            raise
        finally:
            if conn:
                self.pool.putconn(conn)
    
    def execute_query_pandas(self, query: str, params: Optional[Tuple] = None):
        """Execute a SELECT query and return pandas DataFrame."""
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("pandas is required for execute_query_pandas. Install with: pip install pandas")
        
        with self.connection() as conn:
            return pd.read_sql(query, conn, params=params)
    
    def execute_query(self, query: str, params: Optional[Tuple] = None) -> List[Dict[str, Any]]:
        """Execute a SELECT query and return results."""
        with self.connection() as conn:
            with conn.cursor() as cursor:
                try:
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)
                    
                    # Get column names
                    columns = [desc[0] for desc in cursor.description]
                    
                    # Convert to list of dictionaries
                    return [dict(zip(columns, row)) for row in cursor.fetchall()]
                    
                except Exception as e:
                    logger.error(f"Query execution failed: {_safe_error(e)}")  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                    logger.error(f"Query: {query}")  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                    raise
    
    def execute_update(self, query: str, params: Optional[Tuple] = None) -> int:
        """Execute an INSERT/UPDATE/DELETE query and return affected rows."""
        with self.connection() as conn:
            with conn.cursor() as cursor:
                try:
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)
                    
                    conn.commit()
                    return cursor.rowcount
                    
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Update execution failed: {_safe_error(e)}")  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                    logger.error(f"Query: {query}")  # deliberate: logged for ops visibility then handled or re-raised; message carries context (OTel handler discards tracebacks)  # nosemgrep: logging-error-without-handling
                    raise
    
    def test_connection(self) -> bool:
        """Test database connection."""
        try:
            with self.connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    result = cursor.fetchone()
                    return result[0] == 1
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False

# Global database manager instance
_db_manager: Optional[DatabaseManager] = None

def get_db_manager(config=None) -> DatabaseManager:
    """Get the global database manager instance."""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager(config)
    return _db_manager

# Backward compatibility functions
def get_database(config=None) -> DatabaseManager:
    """Backward compatibility function - returns database manager."""
    return get_db_manager(config)

def get_connection():
    """Backward compatibility function - returns connection context manager."""
    return get_db_manager().connection()
