import json
import logging
from typing import Any, Optional, Callable
from uuid import UUID

from app.core.redis import get_sync_redis

logger = logging.getLogger(__name__)

class RedisCache:
    """
    Safe Redis cache wrapper that falls back gracefully if Redis is unavailable.
    """
    
    @staticmethod
    def get(key: str) -> Optional[Any]:
        """
        Get a value from cache. Returns None if not found or if Redis fails.
        """
        try:
            client = get_sync_redis()
            if not client:
                return None
            
            data = client.get(key)
            if data:
                return json.loads(data)
            return None
        except Exception as e:
            logger.debug(f"Redis cache get failed for key {key}: {e}")
            return None

    @staticmethod
    def set(key: str, value: Any, ttl_seconds: int = 300) -> bool:
        """
        Set a value in cache with TTL. Returns True if successful.
        """
        try:
            client = get_sync_redis()
            if not client:
                return False
            
            # Use default function for UUID serialization
            def default_serializer(obj):
                if isinstance(obj, UUID):
                    return str(obj)
                raise TypeError(f"Type {type(obj)} not serializable")
                
            payload = json.dumps(value, default=default_serializer)
            client.setex(key, ttl_seconds, payload)
            return True
        except Exception as e:
            logger.debug(f"Redis cache set failed for key {key}: {e}")
            return False

    @staticmethod
    def delete(key: str) -> bool:
        """
        Delete a value from cache.
        """
        try:
            client = get_sync_redis()
            if not client:
                return False
            
            client.delete(key)
            return True
        except Exception as e:
            logger.debug(f"Redis cache delete failed for key {key}: {e}")
            return False

    @staticmethod
    def delete_pattern(pattern: str) -> bool:
        """
        Delete all keys matching a pattern.
        """
        try:
            client = get_sync_redis()
            if not client:
                return False
            
            keys = client.keys(pattern)
            if keys:
                client.delete(*keys)
            return True
        except Exception as e:
            logger.debug(f"Redis cache delete_pattern failed for pattern {pattern}: {e}")
            return False

def invalidate_study_cache(study_id: UUID | str) -> None:
    """
    Invalidate all cached data for a specific study.
    Call this when a study is updated, tasks are regenerated, or status changes.
    """
    study_id_str = str(study_id)
    RedisCache.delete(f"study_config:public:{study_id_str}")
    RedisCache.delete(f"study_config:public_details:{study_id_str}")
    RedisCache.delete(f"study_config:basic:{study_id_str}")
    RedisCache.delete(f"study_config:basic_v2:{study_id_str}")
    
    # Also clear respondent info caches if any exist
    RedisCache.delete_pattern(f"respondent_study_info:{study_id_str}:*")
    
    # Clear analytics caches
    RedisCache.delete(f"study_analytics:{study_id_str}")
    # Drop any legacy optimized-analysis cache keys from older deployments
    RedisCache.delete_pattern(f"optimized_analysis:{study_id_str}:*")
    # Verified assistant query cache + heavy analysis report cache
    RedisCache.delete_pattern(f"assistant:query:{study_id_str}:*")
    RedisCache.delete_pattern(f"assistant:analysis:{study_id_str}:*")


def invalidate_assistant_analysis_cache(study_id: UUID | str) -> None:
    """
    Invalidate only the assistant's cached analysis report + query answers for a study.

    Call this whenever the underlying response data changes (new completion,
    reset, synthetic respondent) so the assistant never serves stale numbers.
    """
    study_id_str = str(study_id)
    RedisCache.delete_pattern(f"assistant:analysis:{study_id_str}:*")
    RedisCache.delete_pattern(f"assistant:query:{study_id_str}:*")


def invalidate_template_cache(template_id: UUID | str | None = None) -> None:
    """
    Invalidate published template caches.
    Call on create / update / delete / publish / move-to-draft.
    """
    RedisCache.delete("templates:published:list")
    RedisCache.delete_pattern("templates:published:list:*")
    if template_id is not None:
        RedisCache.delete(f"templates:published:item:{template_id}")
    else:
        RedisCache.delete_pattern("templates:published:item:*")
