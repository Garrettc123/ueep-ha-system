#!/usr/bin/env python3
"""
UEEP Enterprise Core - Production High Availability Server

A production-ready Flask application with:
- Circuit breaker pattern for resilience
- Structured JSON logging
- Prometheus metrics
- Health checks and readiness probes
- Connection pooling
- Graceful shutdown
"""

import os
import sys
import time
import signal
import socket
import logging
import json
from datetime import datetime
from functools import wraps
from contextlib import contextmanager

from flask import Flask, jsonify, request
import psycopg2
from psycopg2 import pool
import redis
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# Application Configuration
APP_NAME = "UEEP Enterprise Core"
VERSION = "1.0.0"
HOSTNAME = socket.gethostname()

# Database Configuration
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'postgres'),
    'database': os.getenv('DB_NAME', 'ueep_core'),
    'user': os.getenv('DB_USER', 'ueep_admin'),
    'password': os.getenv('DB_PASSWORD', 'SecurePassword123!'),
    'port': int(os.getenv('DB_PORT', '5432'))
}

# Redis Configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'redis')
REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))

# Application Settings
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
ENVIRONMENT = os.getenv('ENVIRONMENT', 'production')

# Circuit Breaker Configuration
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_RECOVERY_TIMEOUT = 30
CIRCUIT_BREAKER_EXPECTED_EXCEPTION = Exception

# Initialize Flask
app = Flask(__name__)

# Configure JSON Logging
class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': record.levelname,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'hostname': HOSTNAME,
            'environment': ENVIRONMENT
        }
        
        if hasattr(record, 'correlation_id'):
            log_data['correlation_id'] = record.correlation_id
        
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        
        return json.dumps(log_data)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger()
logger.addHandler(handler)
logger.setLevel(getattr(logging, LOG_LEVEL))

# Prometheus Metrics
request_count = Counter(
    'ueep_http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status']
)

request_duration = Histogram(
    'ueep_http_request_duration_seconds',
    'HTTP request duration in seconds',
    ['method', 'endpoint']
)

db_operations = Counter(
    'ueep_database_operations_total',
    'Total database operations',
    ['operation', 'status']
)

cache_operations = Counter(
    'ueep_cache_operations_total',
    'Total cache operations',
    ['operation', 'status']
)

health_status = Gauge(
    'ueep_health_status',
    'Health status (1 = healthy, 0 = unhealthy)',
    ['component']
)

active_connections = Gauge(
    'ueep_active_connections',
    'Number of active connections',
    ['type']
)

circuit_breaker_status = Gauge(
    'ueep_circuit_breaker_status',
    'Circuit breaker status (0 = closed, 1 = open, 2 = half-open)',
    ['circuit']
)

# Circuit Breaker Implementation
class CircuitBreaker:
    def __init__(self, failure_threshold, recovery_timeout, expected_exception):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.failure_count = 0
        self.last_failure_time = None
        self.state = 'closed'  # closed, open, half_open
    
    def call(self, func, *args, **kwargs):
        if self.state == 'open':
            if time.time() - self.last_failure_time >= self.recovery_timeout:
                self.state = 'half_open'
                logger.info(f'Circuit breaker entering half-open state')
            else:
                raise Exception('Circuit breaker is OPEN')
        
        try:
            result = func(*args, **kwargs)
            if self.state == 'half_open':
                self.state = 'closed'
                self.failure_count = 0
                logger.info(f'Circuit breaker closed')
            return result
        except self.expected_exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.failure_count >= self.failure_threshold:
                self.state = 'open'
                logger.error(f'Circuit breaker opened after {self.failure_count} failures')
            
            raise e

# Initialize Circuit Breakers
db_circuit_breaker = CircuitBreaker(
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
    Exception
)

cache_circuit_breaker = CircuitBreaker(
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
    Exception
)

# Database Connection Pool
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(
        minconn=2,
        maxconn=10,
        **DB_CONFIG
    )
    logger.info('Database connection pool initialized')
except Exception as e:
    logger.error(f'Failed to initialize database pool: {e}')
    db_pool = None

# Redis Connection
try:
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5
    )
    redis_client.ping()
    logger.info('Redis connection established')
except Exception as e:
    logger.error(f'Failed to connect to Redis: {e}')
    redis_client = None

# Request Counter
request_counter = {'count': 0}

@contextmanager
def get_db_connection():
    """Context manager for database connections"""
    conn = None
    try:
        if db_pool:
            conn = db_pool.getconn()
            active_connections.labels(type='database').inc()
            yield conn
        else:
            yield None
    finally:
        if conn and db_pool:
            db_pool.putconn(conn)
            active_connections.labels(type='database').dec()

def track_metrics(f):
    """Decorator to track request metrics"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        
        try:
            response = f(*args, **kwargs)
            status = response[1] if isinstance(response, tuple) else 200
            
            request_count.labels(
                method=request.method,
                endpoint=request.endpoint,
                status=status
            ).inc()
            
            return response
        finally:
            request_duration.labels(
                method=request.method,
                endpoint=request.endpoint
            ).observe(time.time() - start_time)
    
    return decorated_function

# Application Routes
@app.before_request
def before_request():
    """Add correlation ID to each request"""
    correlation_id = request.headers.get('X-Correlation-ID', str(time.time()))
    request.correlation_id = correlation_id

@app.route('/')
@track_metrics
def index():
    """Root endpoint with system information"""
    request_counter['count'] += 1
    
    return jsonify({
        'service': APP_NAME,
        'version': VERSION,
        'environment': ENVIRONMENT,
        'node': HOSTNAME,
        'timestamp': datetime.utcnow().isoformat(),
        'request_count': request_counter['count'],
        'status': 'operational'
    }), 200

@app.route('/health')
@track_metrics
def health():
    """Health check endpoint"""
    health_checks = {
        'service': 'healthy',
        'database': 'unknown',
        'cache': 'unknown'
    }
    
    overall_healthy = True
    
    # Check Database
    try:
        def check_db():
            with get_db_connection() as conn:
                if conn:
                    with conn.cursor() as cur:
                        cur.execute('SELECT 1')
                        cur.fetchone()
                    return True
            return False
        
        if db_circuit_breaker.call(check_db):
            health_checks['database'] = 'healthy'
            health_status.labels(component='database').set(1)
            circuit_breaker_status.labels(circuit='database').set(0)
        else:
            health_checks['database'] = 'unhealthy'
            health_status.labels(component='database').set(0)
            overall_healthy = False
    except Exception as e:
        health_checks['database'] = 'unhealthy'
        health_status.labels(component='database').set(0)
        circuit_breaker_status.labels(circuit='database').set(1 if db_circuit_breaker.state == 'open' else 2)
        overall_healthy = False
        logger.error(f'Database health check failed: {e}')
    
    # Check Redis
    try:
        def check_redis():
            if redis_client:
                redis_client.ping()
                return True
            return False
        
        if cache_circuit_breaker.call(check_redis):
            health_checks['cache'] = 'healthy'
            health_status.labels(component='cache').set(1)
            circuit_breaker_status.labels(circuit='cache').set(0)
        else:
            health_checks['cache'] = 'unhealthy'
            health_status.labels(component='cache').set(0)
            overall_healthy = False
    except Exception as e:
        health_checks['cache'] = 'unhealthy'
        health_status.labels(component='cache').set(0)
        circuit_breaker_status.labels(circuit='cache').set(1 if cache_circuit_breaker.state == 'open' else 2)
        overall_healthy = False
        logger.error(f'Cache health check failed: {e}')
    
    status_code = 200 if overall_healthy else 503
    
    return jsonify({
        'status': 'healthy' if overall_healthy else 'unhealthy',
        'timestamp': datetime.utcnow().isoformat(),
        'node': HOSTNAME,
        'checks': health_checks
    }), status_code

@app.route('/ready')
@track_metrics
def ready():
    """Readiness probe for Kubernetes"""
    return jsonify({
        'status': 'ready',
        'timestamp': datetime.utcnow().isoformat()
    }), 200

@app.route('/metrics')
def metrics():
    """Prometheus metrics endpoint"""
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}

@app.route('/api/data')
@track_metrics
def get_data():
    """Sample API endpoint with database and cache"""
    cache_key = 'sample_data'
    
    # Try cache first
    try:
        def get_from_cache():
            if redis_client:
                cached = redis_client.get(cache_key)
                if cached:
                    cache_operations.labels(operation='get', status='hit').inc()
                    return json.loads(cached)
            return None
        
        cached_data = cache_circuit_breaker.call(get_from_cache)
        if cached_data:
            return jsonify({
                'data': cached_data,
                'source': 'cache',
                'node': HOSTNAME,
                'timestamp': datetime.utcnow().isoformat()
            }), 200
        
        cache_operations.labels(operation='get', status='miss').inc()
    except Exception as e:
        logger.error(f'Cache read failed: {e}')
        cache_operations.labels(operation='get', status='error').inc()
    
    # Get from database
    try:
        def get_from_db():
            with get_db_connection() as conn:
                if conn:
                    with conn.cursor() as cur:
                        cur.execute('SELECT NOW() as current_time')
                        result = cur.fetchone()
                        return {'current_time': str(result[0])}
            return None
        
        data = db_circuit_breaker.call(get_from_db)
        
        if data:
            db_operations.labels(operation='select', status='success').inc()
            
            # Update cache
            try:
                def set_cache():
                    if redis_client:
                        redis_client.setex(
                            cache_key,
                            60,  # 60 second TTL
                            json.dumps(data)
                        )
                
                cache_circuit_breaker.call(set_cache)
                cache_operations.labels(operation='set', status='success').inc()
            except Exception as e:
                logger.error(f'Cache write failed: {e}')
                cache_operations.labels(operation='set', status='error').inc()
            
            return jsonify({
                'data': data,
                'source': 'database',
                'node': HOSTNAME,
                'timestamp': datetime.utcnow().isoformat()
            }), 200
        else:
            db_operations.labels(operation='select', status='error').inc()
            return jsonify({
                'error': 'Database unavailable',
                'node': HOSTNAME,
                'timestamp': datetime.utcnow().isoformat()
            }), 503
    
    except Exception as e:
        logger.error(f'Database operation failed: {e}')
        db_operations.labels(operation='select', status='error').inc()
        return jsonify({
            'error': 'Service temporarily unavailable',
            'node': HOSTNAME,
            'timestamp': datetime.utcnow().isoformat()
        }), 503

# Graceful Shutdown
def graceful_shutdown(signum, frame):
    """Handle graceful shutdown"""
    logger.info(f'Received signal {signum}, shutting down gracefully')
    
    # Close database pool
    if db_pool:
        db_pool.closeall()
        logger.info('Database connections closed')
    
    # Close Redis connection
    if redis_client:
        redis_client.close()
        logger.info('Redis connection closed')
    
    sys.exit(0)

signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)

if __name__ == '__main__':
    logger.info(f'Starting {APP_NAME} v{VERSION} on {HOSTNAME}')
    logger.info(f'Environment: {ENVIRONMENT}')
    logger.info(f'Log level: {LOG_LEVEL}')
    
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=False
    )
