"""Redis client for pub/sub across multiple Gunicorn workers."""

import json
import logging
from typing import AsyncGenerator, Any

import redis.asyncio as aioredis
import redis as sync_redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_async_redis_pool: aioredis.ConnectionPool | None = None
_sync_redis_client: sync_redis.Redis | None = None


def _get_redis_url() -> str | None:
    """Get Redis URL from settings."""
    return settings.REDIS_URL


async def get_async_redis() -> aioredis.Redis | None:
    """
    Get async Redis client with connection pooling.
    Returns None if REDIS_URL is not configured.
    """
    global _async_redis_pool
    
    redis_url = _get_redis_url()
    if not redis_url:
        logger.debug("REDIS_URL not configured, Redis pub/sub disabled")
        return None
    
    if _async_redis_pool is None:
        try:
            max_connections = max(1, settings.REDIS_MAX_CONNECTIONS)
            _async_redis_pool = aioredis.ConnectionPool.from_url(
                redis_url,
                decode_responses=True,
                max_connections=max_connections,
                retry_on_timeout=True,
            )
            logger.info(
                "Async Redis connection pool initialized with max_connections=%s",
                max_connections,
            )
        except Exception as e:
            logger.error(f"Failed to create async Redis pool: {e}")
            return None
    
    return aioredis.Redis(connection_pool=_async_redis_pool)


def get_sync_redis() -> sync_redis.Redis | None:
    """
    Get sync Redis client for use in background jobs (run_in_executor context).
    Returns None if REDIS_URL is not configured.
    """
    global _sync_redis_client
    
    redis_url = _get_redis_url()
    if not redis_url:
        logger.debug("REDIS_URL not configured, Redis cache disabled")
        return None

    if _sync_redis_client is None:
        try:
            _sync_redis_client = sync_redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
                retry_on_timeout=True,
                # Proactively PING connections that have been idle longer than this
                # (seconds) and reconnect if dead. Without it, a stale pooled
                # connection (managed Redis / firewalls drop idle sockets) blocks
                # for the full socket_timeout on the next command, causing the
                # intermittent multi-second stalls seen on cache reads.
                health_check_interval=30,
                socket_keepalive=True,
            )
            _sync_redis_client.ping()
            logger.info("Sync Redis client initialized")
        except Exception as e:
            logger.error(f"Failed to create sync Redis client: {e}")
            _sync_redis_client = None
            return None
    
    return _sync_redis_client


def publish_user_job_update(user_id: str, data: dict[str, Any]) -> bool:
    """Publish a job update to a user's global notification channel."""
    client = get_sync_redis()
    if client is None:
        logger.debug(f"Redis not available, skipping user publish for {user_id}")
        return False

    channel = f"user:{user_id}:jobs"
    try:
        payload = json.dumps(data)
        client.publish(channel, payload)
        logger.debug(f"Published to {channel}: {data.get('event') or data.get('type')}")
        return True
    except Exception as e:
        logger.warning(f"Failed to publish user job update for {user_id}: {e}")
        return False


async def subscribe_to_user_jobs(user_id: str) -> AsyncGenerator[dict[str, Any], None]:
    """Subscribe to all job updates for a user via Redis."""
    client = await get_async_redis()
    if client is None:
        logger.warning(f"Redis not available, cannot subscribe to user jobs {user_id}")
        return

    channel = f"user:{user_id}:jobs"
    pubsub = client.pubsub()

    try:
        await pubsub.subscribe(channel)
        logger.debug(f"Subscribed to Redis channel: {channel}")

        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    yield data
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON in user Redis message: {e}")
                    continue
    except Exception as e:
        logger.error(f"Error in Redis user subscription for {user_id}: {e}")
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            logger.debug(f"Unsubscribed from Redis channel: {channel}")
        except Exception as e:
            logger.warning(f"Error cleaning up user Redis subscription: {e}")


def publish_job_update(job_id: str, data: dict[str, Any]) -> bool:
    """
    Publish a job progress update to Redis.
    
    This is called from background jobs (often in executor threads).
    Uses sync Redis client.
    
    Args:
        job_id: The job identifier
        data: Dict with type, progress, message, error fields
        
    Returns:
        True if published successfully, False otherwise
    """
    client = get_sync_redis()
    if client is None:
        logger.debug(f"Redis not available, skipping publish for job {job_id}")
        return False
    
    channel = f"job:{job_id}"
    try:
        payload = json.dumps(data)
        client.publish(channel, payload)
        logger.debug(f"Published to {channel}: {data.get('type')}")
        return True
    except Exception as e:
        logger.warning(f"Failed to publish job update for {job_id}: {e}")
        return False


async def subscribe_to_job(job_id: str) -> AsyncGenerator[dict[str, Any], None]:
    """
    Subscribe to job progress updates from Redis.
    
    Async generator that yields progress updates as they arrive.
    Handles cleanup on exit (unsubscribe).
    
    Args:
        job_id: The job identifier to subscribe to
        
    Yields:
        Dict with type, progress, message, error fields
    """
    client = await get_async_redis()
    if client is None:
        logger.warning(f"Redis not available, cannot subscribe to job {job_id}")
        return
    
    channel = f"job:{job_id}"
    pubsub = client.pubsub()
    
    try:
        await pubsub.subscribe(channel)
        logger.debug(f"Subscribed to Redis channel: {channel}")
        
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    yield data
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON in Redis message: {e}")
                    continue
    except Exception as e:
        logger.error(f"Error in Redis subscription for job {job_id}: {e}")
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            logger.debug(f"Unsubscribed from Redis channel: {channel}")
        except Exception as e:
            logger.warning(f"Error cleaning up Redis subscription: {e}")


async def close_redis_pools() -> None:
    """Close Redis connection pools on shutdown."""
    global _async_redis_pool, _sync_redis_client
    
    if _async_redis_pool is not None:
        try:
            await _async_redis_pool.disconnect()
            _async_redis_pool = None
            logger.info("Async Redis pool closed")
        except Exception as e:
            logger.warning(f"Error closing async Redis pool: {e}")
    
    if _sync_redis_client is not None:
        try:
            _sync_redis_client.close()
            _sync_redis_client = None
            logger.info("Sync Redis client closed")
        except Exception as e:
            logger.warning(f"Error closing sync Redis client: {e}")


def is_redis_configured() -> bool:
    """Check if Redis is configured."""
    return _get_redis_url() is not None
