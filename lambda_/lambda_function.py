# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import os
import sys
import logging
from datetime import datetime

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    Lambda handler for ERP Simulation Orchestrator.
    
    This handler runs the complete ERP simulation cycle including:
    - Order generation
    - Procurement simulation  
    - Production planning
    - MES integration
    - Shipping and finance
    """
    
    try:
        # Import the simulation orchestrator
        from erp_simulation.simulation_orchestrator import SimulationOrchestrator
        from erp_simulation.config import get_config
        
        action = event.get('action', 'single_cycle')
        logger.info(f"ERP Simulation Lambda invoked with action: {action}")
        
        # Log environment info
        logger.info(f"Python path: {sys.path}")
        logger.info(f"Working directory: {os.getcwd()}")
        logger.info(f"Lambda task root: {os.environ.get('LAMBDA_TASK_ROOT', 'Not set')}")
        
        # Initialize configuration
        config = get_config(use_secrets_manager=True)
        logger.info(f"Configuration loaded - DB host: {config.database.host}")
        
        # Initialize orchestrator
        orchestrator = SimulationOrchestrator()
        logger.info("Simulation orchestrator initialized")
        
        if action == 'single_cycle':
            # Run a single simulation cycle
            result = orchestrator.run_single_cycle()
            
            if result['success']:
                simulator = result.get('simulator', 'ran')
                logger.info(f"✅ Simulation cycle completed successfully (simulator: {simulator})")
                logger.info(f"   Cycle count: {result['cycle_count']}")
                message = ('ERP simulation cycle completed successfully' if simulator != 'busy' else
                           'Simulator busy: another cycle is running its steps, so no simulator '
                           'step ran in this invocation (the clock steps did)')
                
                return {
                    'statusCode': 200,
                    'body': json.dumps({
                        'success': True,
                        'action': action,
                        'timestamp': datetime.now().isoformat(),
                        'cycle_count': result['cycle_count'],
                        'simulator': simulator,
                        'message': message
                    })
                }
            else:
                logger.error(f"❌ Simulation cycle failed: {result.get('error', 'Unknown error')}")
                
                return {
                    'statusCode': 500,
                    'body': json.dumps({
                        'success': False,
                        'action': action,
                        'timestamp': datetime.now().isoformat(),
                        'error': result.get('error', 'Simulation cycle failed')
                    })
                }
        
        elif action == 'test_connection':
            # Test database connection
            from erp_simulation.database import get_database
            
            db = get_database()
            success = db.test_connection()
            
            if success:
                logger.info("✅ Database connection test successful")
                return {
                    'statusCode': 200,
                    'body': json.dumps({
                        'success': True,
                        'action': action,
                        'timestamp': datetime.now().isoformat(),
                        'message': 'Database connection successful'
                    })
                }
            else:
                logger.error("❌ Database connection test failed")
                return {
                    'statusCode': 500,
                    'body': json.dumps({
                        'success': False,
                        'action': action,
                        'timestamp': datetime.now().isoformat(),
                        'error': 'Database connection failed'
                    })
                }
        
        elif action == 'health_check':
            # Health check
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'success': True,
                    'action': action,
                    'timestamp': datetime.now().isoformat(),
                    'message': 'ERP Simulation Lambda is healthy',
                    'version': '1.0.0'
                })
            }
        
        else:
            logger.warning(f"Unknown action: {action}")
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'success': False,
                    'action': action,
                    'timestamp': datetime.now().isoformat(),
                    'error': f'Unknown action: {action}. Supported actions: single_cycle, test_connection, health_check'
                })
            }
        
    except ImportError as e:
        logger.error(f"Import error: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'success': False,
                'timestamp': datetime.now().isoformat(),
                'error': f'Import error: {str(e)}. Check if erp_simulation module is properly packaged.'
            })
        }
    
    except Exception as e:
        logger.error(f"Lambda execution error: {str(e)}", exc_info=True)
        
        return {
            'statusCode': 500,
            'body': json.dumps({
                'success': False,
                'timestamp': datetime.now().isoformat(),
                'error': str(e)
            })
        }
