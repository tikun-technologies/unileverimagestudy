"""Job progress notifier for event-driven WebSocket updates.

Uses Redis pub/sub for cross-process communication (multiple Gunicorn workers).
Falls back to in-memory queues if Redis is not configured.
"""

import asyncio
import logging
from typing import Dict, Set, Any, AsyncGenerator, Optional

from app.core.redis import (
    publish_job_update,
    publish_user_job_update,
    subscribe_to_job,
    subscribe_to_user_jobs,
    is_redis_configured,
)

logger = logging.getLogger(__name__)

# Cache job metadata for user-channel publishing (avoid DB hit on every throttled progress tick)
_job_meta_cache: Dict[str, Dict[str, Any]] = {}


def _load_job_meta(job_id: str) -> Optional[Dict[str, Any]]:
    if job_id in _job_meta_cache:
        return _job_meta_cache[job_id]

    from app.db.session import SessionLocal
    from app.models.job_model import Job, JobStatus
    from app.models.study_model import Study
    from app.services.job_notification_service import infer_job_kind

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            return None

        study_title = None
        try:
            study = db.query(Study).filter(Study.id == job.study_id).first()
            if study:
                study_title = study.title or "Untitled Study"
        except Exception:
            pass

        meta = {
            "user_id": job.user_id,
            "study_id": job.study_id,
            "study_title": study_title,
            "job_kind": infer_job_kind(job),
        }
        _job_meta_cache[job_id] = meta

        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            _job_meta_cache.pop(job_id, None)

        return meta
    except Exception as e:
        logger.warning(f"Failed to load job meta for {job_id}: {e}")
        return None
    finally:
        db.close()


def _publish_user_update(job_id: str, data: Dict[str, Any]) -> None:
    """Publish enriched update to user global channel."""
    from app.db.session import SessionLocal
    from app.models.job_model import Job, JobStatus
    from app.services.job_notification_service import (
        build_user_job_envelope,
        is_job_notification_dismissed,
        upsert_user_job_notification,
    )

    meta = _load_job_meta(job_id)
    if not meta:
        return

    kind = meta.get("job_kind", "task_generation")
    if kind not in ("task_generation", "simulate_ai"):
        return

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            return
        if is_job_notification_dismissed(db, meta["user_id"], job_id):
            return

        envelope = build_user_job_envelope(job, data, meta.get("study_title"))
        publish_user_job_update(meta["user_id"], envelope)

        try:
            upsert_user_job_notification(db, job, study_title=meta.get("study_title"))
        except Exception as e:
            logger.warning(f"Failed to persist notification for job {job_id}: {e}")

        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            _job_meta_cache.pop(job_id, None)
    except Exception as e:
        logger.warning(f"User channel publish failed for job {job_id}: {e}")
    finally:
        db.close()


class JobProgressNotifier:
    """
    Event-driven job progress notification system.

    Uses Redis pub/sub when configured (for multi-worker deployments).
    Falls back to in-memory asyncio.Queue when Redis is not available.
    """

    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._user_subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, job_id: str) -> asyncio.Queue:
        """Subscribe to progress updates for a job (in-memory fallback)."""
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            if job_id not in self._subscribers:
                self._subscribers[job_id] = set()
            self._subscribers[job_id].add(queue)
        logger.debug(
            f"Subscribed to job {job_id} (in-memory), total subscribers: {len(self._subscribers[job_id])}"
        )
        return queue

    async def subscribe_user(self, user_id: str) -> asyncio.Queue:
        """Subscribe to all job updates for a user (in-memory fallback)."""
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            if user_id not in self._user_subscribers:
                self._user_subscribers[user_id] = set()
            self._user_subscribers[user_id].add(queue)
        return queue

    async def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber's queue from a job (in-memory fallback)."""
        async with self._lock:
            if job_id in self._subscribers:
                self._subscribers[job_id].discard(queue)
                if not self._subscribers[job_id]:
                    del self._subscribers[job_id]

    async def unsubscribe_user(self, user_id: str, queue: asyncio.Queue) -> None:
        """Remove a user-level subscriber."""
        async with self._lock:
            if user_id in self._user_subscribers:
                self._user_subscribers[user_id].discard(queue)
                if not self._user_subscribers[user_id]:
                    del self._user_subscribers[user_id]

    def notify(self, job_id: str, data: Dict[str, Any]) -> None:
        """
        Push a progress update to all subscribers of a job and the job owner's user channel.
        """
        if is_redis_configured():
            publish_job_update(job_id, data)
            _publish_user_update(job_id, data)

        if job_id in self._subscribers:
            dead_queues: Set[asyncio.Queue] = set()
            for queue in self._subscribers[job_id]:
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    logger.warning(f"Queue full for job {job_id}, dropping message")
                except Exception as e:
                    logger.debug(f"Failed to notify subscriber for job {job_id}: {e}")
                    dead_queues.add(queue)

            for queue in dead_queues:
                self._subscribers[job_id].discard(queue)

            if job_id in self._subscribers and not self._subscribers[job_id]:
                del self._subscribers[job_id]

        meta = _load_job_meta(job_id)
        if meta and meta.get("user_id") in self._user_subscribers:
            from app.db.session import SessionLocal
            from app.models.job_model import Job
            from app.services.job_notification_service import build_user_job_envelope

            db = SessionLocal()
            try:
                job = db.query(Job).filter(Job.job_id == job_id).first()
                if job:
                    envelope = build_user_job_envelope(job, data, meta.get("study_title"))
                    user_id = meta["user_id"]
                    dead: Set[asyncio.Queue] = set()
                    for queue in self._user_subscribers.get(user_id, set()):
                        try:
                            queue.put_nowait(envelope)
                        except Exception:
                            dead.add(queue)
                    for queue in dead:
                        self._user_subscribers[user_id].discard(queue)
            finally:
                db.close()

    async def subscribe_redis(self, job_id: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Subscribe to job progress updates via Redis pub/sub."""
        if is_redis_configured():
            logger.debug(f"Using Redis subscription for job {job_id}")
            async for update in subscribe_to_job(job_id):
                yield update
        else:
            logger.debug(f"Redis not configured, using in-memory subscription for job {job_id}")
            queue = await self.subscribe(job_id)
            try:
                while True:
                    update = await queue.get()
                    yield update
            finally:
                await self.unsubscribe(job_id, queue)

    async def subscribe_user_redis(self, user_id: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Subscribe to all job updates for a user via Redis or in-memory fallback."""
        if is_redis_configured():
            async for update in subscribe_to_user_jobs(user_id):
                yield update
        else:
            queue = await self.subscribe_user(user_id)
            try:
                while True:
                    update = await queue.get()
                    yield update
            finally:
                await self.unsubscribe_user(user_id, queue)

    def has_subscribers(self, job_id: str) -> bool:
        """Check if a job has any in-memory subscribers."""
        return job_id in self._subscribers and len(self._subscribers[job_id]) > 0

    def get_subscriber_count(self, job_id: str) -> int:
        """Get the number of in-memory subscribers for a job."""
        return len(self._subscribers.get(job_id, set()))

    async def cleanup_job(self, job_id: str) -> None:
        """Remove all in-memory subscribers for a completed/failed job."""
        async with self._lock:
            if job_id in self._subscribers:
                del self._subscribers[job_id]
        _job_meta_cache.pop(job_id, None)
        logger.debug(f"Cleaned up in-memory subscribers for job {job_id}")


job_progress_notifier = JobProgressNotifier()
