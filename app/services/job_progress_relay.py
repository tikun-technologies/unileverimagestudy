"""Live task-generation progress without a database write per respondent.

The caller records the latest percent. A side thread publishes that percent to
Redis immediately, so an open page keeps moving. A second side thread writes
the job row and the notification row on an interval, and only while the job is
still processing. Completed, failed, and cancelled are written by the caller
after this relay is closed.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECONDS = 30.0
_CANCEL_KEY_TTL_SECONDS = 6 * 60 * 60

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, "JobProgressRelay"] = {}


def _cancel_key(job_id: str) -> str:
    return f"job-cancelled:{job_id}"


def report_generation_progress(job_id: str, progress: Optional[float], message: str) -> bool:
    """Hand progress to the open relay. Return False when this job has none."""
    with _REGISTRY_LOCK:
        relay = _REGISTRY.get(job_id)
    if relay is None:
        return False
    relay.update(progress, message)
    return True


def stop_generation_progress(job_id: str) -> None:
    """Stop the relay in this process. Safe when generation runs somewhere else."""
    with _REGISTRY_LOCK:
        relay = _REGISTRY.get(job_id)
    if relay is not None:
        relay.close()


def mark_generation_cancelled(job_id: str) -> None:
    """Tell a worker in another process to stop publishing this job."""
    stop_generation_progress(job_id)
    try:
        from app.core.redis import get_sync_redis

        client = get_sync_redis()
        if client is not None:
            client.set(_cancel_key(job_id), "1", ex=_CANCEL_KEY_TTL_SECONDS)
    except Exception as exc:
        logger.warning("Could not record cancellation for job %s: %s", job_id, exc)


class JobProgressRelay:
    """One relay per running task-generation job."""

    def __init__(self, job_id: str, interval_seconds: float = SNAPSHOT_INTERVAL_SECONDS):
        self.job_id = job_id
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._latest: Optional[tuple[float, str]] = None
        self._closed = False
        self._suppress_live = False
        self._opened = False
        self._user_id: Optional[str] = None
        self._study_id: Optional[str] = None
        self._study_title: Optional[str] = None
        self._kind = "task_generation"
        self._created_at: Optional[str] = None
        self._created_at_dt: Optional[datetime] = None
        self._started_at: Optional[str] = None
        self._dismissed = False
        self._publish_thread: Optional[threading.Thread] = None
        self._db_thread: Optional[threading.Thread] = None

    def __enter__(self) -> "JobProgressRelay":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def open(self) -> None:
        if self._opened:
            return
        self._load_meta()
        with _REGISTRY_LOCK:
            _REGISTRY[self.job_id] = self
        self._publish_thread = threading.Thread(
            target=self._publish_loop,
            name=f"job-progress-live-{self.job_id[:8]}",
            daemon=True,
        )
        self._db_thread = threading.Thread(
            target=self._db_loop,
            name=f"job-progress-db-{self.job_id[:8]}",
            daemon=True,
        )
        self._opened = True
        self._publish_thread.start()
        self._db_thread.start()

    def update(self, progress: Optional[float], message: str) -> None:
        """Record the latest percent. Does not talk to the database or Redis."""
        with self._lock:
            if self._closed or self._suppress_live:
                return
            if progress is None:
                progress = self._latest[0] if self._latest is not None else 0.0
            self._latest = (float(progress), str(message))
        self._wake.set()

    def close(self) -> None:
        """Stop both threads. Does not write a final snapshot."""
        with _REGISTRY_LOCK:
            if _REGISTRY.get(self.job_id) is self:
                _REGISTRY.pop(self.job_id, None)
        with self._lock:
            if self._closed and not self._opened:
                return
            self._closed = True
        self._stop.set()
        self._wake.set()
        if self._publish_thread is not None and self._publish_thread.is_alive():
            self._publish_thread.join(timeout=5)
        if self._db_thread is not None and self._db_thread.is_alive():
            self._db_thread.join(timeout=15)
        self._opened = False

    def _load_meta(self) -> None:
        from app.db.session import SessionLocal
        from app.models.job_model import Job
        from app.models.study_model import Study
        from app.services.job_notification_service import (
            infer_job_kind,
            is_job_notification_dismissed,
        )

        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.job_id == self.job_id).first()
            if job is None:
                return
            self._user_id = job.user_id
            self._study_id = job.study_id
            self._kind = infer_job_kind(job)
            self._created_at_dt = job.created_at
            self._created_at = job.created_at.isoformat() if job.created_at else None
            self._started_at = job.started_at.isoformat() if job.started_at else None
            if job.study_id:
                study = db.query(Study).filter(Study.id == job.study_id).first()
                title = study.title if study is not None else None
                self._study_title = title or "Untitled Study"
            if job.user_id:
                self._dismissed = is_job_notification_dismissed(db, job.user_id, self.job_id)
        except Exception as exc:
            logger.warning("Progress relay could not load job %s: %s", self.job_id, exc)
        finally:
            db.close()

    def _publish_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                return
            with self._lock:
                self._wake.clear()
                snapshot = self._latest
                suppressed = self._suppress_live
            if snapshot is None or suppressed:
                continue
            if self._cancel_requested():
                with self._lock:
                    self._suppress_live = True
                continue
            self._publish_live(*snapshot)

    def _db_loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._flush_db()

    def _cancel_requested(self) -> bool:
        try:
            from app.core.redis import get_sync_redis

            client = get_sync_redis()
            if client is None:
                return False
            return bool(client.exists(_cancel_key(self.job_id)))
        except Exception:
            return False

    def _publish_live(self, progress: float, message: str) -> None:
        from app.core.redis import publish_job_update, publish_user_job_update

        with self._publish_lock:
            with self._lock:
                if self._closed or self._suppress_live:
                    return
                dismissed = self._dismissed
                user_id = self._user_id
                kind = self._kind
            try:
                publish_job_update(
                    self.job_id,
                    {"type": "progress", "progress": progress, "message": message},
                )
            except Exception as exc:
                logger.warning("Live progress publish failed for job %s: %s", self.job_id, exc)
            if dismissed or not user_id or kind not in ("task_generation", "simulate_ai"):
                return
            try:
                publish_user_job_update(user_id, self._user_envelope(progress, message))
            except Exception as exc:
                logger.warning("Live notification publish failed for job %s: %s", self.job_id, exc)

    def _user_envelope(self, progress: float, message: str) -> dict:
        return {
            "event": "job_update",
            "type": "progress",
            "job_id": self.job_id,
            "study_id": self._study_id,
            "study_title": self._study_title,
            "job_kind": self._kind,
            "status": "processing",
            "progress": progress,
            "message": message,
            "error": None,
            "respondents_requested": None,
            "respondents_completed": None,
            "created_at": self._created_at,
            "started_at": self._started_at,
            "completed_at": None,
        }

    def _flush_db(self) -> None:
        with self._lock:
            snapshot = self._latest
        if snapshot is None:
            return
        progress, message = snapshot
        try:
            still_processing = self._write_job_row(progress, message)
        except Exception as exc:
            logger.warning("Progress snapshot failed for job %s: %s", self.job_id, exc)
            return
        if not still_processing:
            with self._lock:
                self._suppress_live = True
            return
        try:
            self._write_notification(progress, message)
        except Exception as exc:
            logger.warning("Notification snapshot failed for job %s: %s", self.job_id, exc)

    def _write_job_row(self, progress: float, message: str) -> bool:
        from app.db.session import SessionLocal
        from app.models.job_model import Job, JobStatus

        db = SessionLocal()
        try:
            job = (
                db.query(Job)
                .filter(Job.job_id == self.job_id)
                .with_for_update()
                .first()
            )
            if job is None or job.status != JobStatus.PROCESSING:
                db.rollback()
                return False
            job.progress = progress
            job.message = message
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _write_notification(self, progress: float, message: str) -> None:
        from sqlalchemy.exc import IntegrityError

        from app.db.session import SessionLocal
        from app.models.job_model import UserJobNotification
        from app.services.job_notification_service import (
            invalidate_user_notification_cache,
            is_job_notification_dismissed,
        )

        if self._dismissed or not self._user_id or not self._study_id:
            return
        if self._kind not in ("task_generation", "simulate_ai"):
            return

        db = SessionLocal()
        try:
            if is_job_notification_dismissed(db, self._user_id, self.job_id):
                self._dismissed = True
                db.rollback()
                return

            row = (
                db.query(UserJobNotification)
                .filter(
                    UserJobNotification.user_id == self._user_id,
                    UserJobNotification.job_id == self.job_id,
                )
                .with_for_update()
                .first()
            )
            if row is None:
                db.add(
                    UserJobNotification(
                        user_id=self._user_id,
                        job_id=self.job_id,
                        study_id=self._study_id,
                        study_title=self._study_title,
                        job_kind=self._kind,
                        status="processing",
                        progress=progress,
                        message=message,
                        is_read=False,
                        job_created_at=self._created_at_dt,
                    )
                )
            elif row.dismissed_at is not None or row.status not in ("pending", "started", "processing"):
                db.rollback()
                return
            else:
                row.progress = progress
                row.message = message
                row.status = "processing"
            db.commit()
            invalidate_user_notification_cache(UUID(self._user_id))
        except IntegrityError:
            db.rollback()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
