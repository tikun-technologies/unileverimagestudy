"""WebSocket endpoints for real-time streaming (analytics, task generation, and simulation)."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.security import verify_token
from app.db.session import get_db, SessionLocal
from app.models.user_model import User
from app.models.study_model import Study, StudyMember
from app.models.job_model import Job, JobStatus
from app.services.response import StudyResponseService
from app.websocket.manager import analytics_manager
from app.websocket.job_notifier import job_progress_notifier

logger = logging.getLogger(__name__)
router = APIRouter()


def _websocket_job_wall_clock_seconds() -> int:
    """Match Celery hard time limit so job WebSockets are not closed before tasks can finish."""
    from app.celery_app import celery_app

    limit = celery_app.conf.task_time_limit
    if limit is None:
        return 28800
    return int(limit)


async def get_user_from_token(token: str, db: Session) -> User | None:
    """Validate JWT token and return the user if valid."""
    if not token:
        return None
    
    payload = verify_token(token, "access")
    if not payload:
        return None
    
    user_id = payload.get("sub")
    if not user_id:
        return None
    
    try:
        user = db.query(User).filter(User.id == UUID(user_id)).first()
        if user and user.is_active:
            return user
    except (ValueError, Exception) as e:
        logger.debug(f"Token validation error: {e}")
    
    return None


def check_study_access(db: Session, study_id: UUID, user_id: UUID) -> bool:
    """Check if user has access to the study (owner or member)."""
    ownership_check = select(Study.creator_id).where(Study.id == study_id)
    result = db.execute(ownership_check).first()
    
    if not result:
        return False
    
    if result.creator_id == user_id:
        return True
    
    member = db.scalar(
        select(StudyMember).where(
            StudyMember.study_id == study_id,
            StudyMember.user_id == user_id
        )
    )
    return member is not None


@router.websocket("/analytics/{study_id}")
async def websocket_analytics(
    websocket: WebSocket,
    study_id: UUID,
    token: str = Query(..., description="JWT access token"),
    interval_seconds: int = Query(5, ge=5, le=60, description="Update interval in seconds"),
):
    """
    WebSocket endpoint for real-time analytics streaming.
    
    Connect with: ws://host/api/v1/ws/analytics/{study_id}?token={jwt_token}
    
    Sends analytics data every `interval_seconds` and a ping every 30 seconds
    to keep the connection alive (Azure has 240s timeout).
    Uses a short-lived DB session per loop iteration to avoid stale connections.
    """
    # Auth phase: one session only for token + access check, then close
    db_auth = next(get_db())
    try:
        user = await get_user_from_token(token, db_auth)
        if not user:
            logger.warning("WebSocket analytics rejected: invalid or expired token")
            await websocket.close(code=4001, reason="Invalid or expired token")
            return
        if not check_study_access(db_auth, study_id, user.id):
            logger.warning("WebSocket analytics rejected: access denied to study %s for user %s", study_id, user.id)
            await websocket.close(code=4003, reason="Access denied to study")
            return
    finally:
        db_auth.close()

    try:
        await analytics_manager.connect(str(study_id), websocket)
        last_payload: str | None = None
        ping_counter = 0

        try:
            while True:
                try:
                    # Per-iteration session: create, use, close (no long-lived session)
                    db = SessionLocal()
                    try:
                        service = StudyResponseService(db)
                        analytics = service.get_study_analytics(study_id)
                        payload = analytics.model_dump()
                        payload_str = str(payload)

                        if payload_str != last_payload:
                            await analytics_manager.send_to_connection(
                                websocket,
                                {"type": "data", "payload": payload}
                            )
                            last_payload = payload_str
                    finally:
                        db.close()

                    ping_counter += interval_seconds
                    if ping_counter >= 30:
                        await analytics_manager.send_to_connection(
                            websocket,
                            {"type": "ping"}
                        )
                        ping_counter = 0

                    await asyncio.sleep(interval_seconds)

                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger.error(f"Error in WebSocket analytics loop: {e}")
                    await asyncio.sleep(interval_seconds)

        finally:
            analytics_manager.disconnect(str(study_id), websocket)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for study {study_id}")
    except Exception as e:
        logger.error(f"WebSocket error for study {study_id}: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass


@router.websocket("/task-generation/{job_id}")
async def websocket_task_generation(
    websocket: WebSocket,
    job_id: str,
    token: str = Query(..., description="JWT access token"),
):
    """
    WebSocket endpoint for real-time task generation progress.
    
    Connect with: ws://host/api/v1/ws/task-generation/{job_id}?token={jwt_token}
    
    Receives progress updates as they happen via Redis pub/sub (works across Gunicorn workers).
    Messages: {"type": "progress|completed|failed", "progress": float, "message": str, "error"?: str}
    
    Connection closes automatically when job completes or fails.
    Timeout after 15 minutes to prevent orphaned connections.
    """
    # Auth phase: verify token and job ownership
    db_auth = next(get_db())
    try:
        user = await get_user_from_token(token, db_auth)
        if not user:
            await websocket.close(code=4001, reason="Invalid or expired token")
            return
        
        # Check if job exists and user owns it
        job = db_auth.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            await websocket.close(code=4004, reason="Job not found")
            return
        
        # Allow any study creator or member to see job progress (not only job starter)
        if not check_study_access(db_auth, UUID(job.study_id), user.id):
            await websocket.close(code=4003, reason="Access denied to study")
            return
        
        # Check if job is already completed or failed
        if job.status == JobStatus.COMPLETED:
            await websocket.accept()
            await websocket.send_json({
                "type": "completed",
                "progress": 100.0,
                "message": job.message or "Task generation completed"
            })
            await websocket.close()
            return
        
        if job.status == JobStatus.FAILED:
            await websocket.accept()
            await websocket.send_json({
                "type": "failed",
                "error": job.error or "Unknown error",
                "message": job.message or "Task generation failed"
            })
            await websocket.close()
            return
        
        if job.status == JobStatus.CANCELLED:
            await websocket.accept()
            await websocket.send_json({
                "type": "failed",
                "error": "Job was cancelled",
                "message": "Task generation was cancelled"
            })
            await websocket.close()
            return
        
        # Get current progress to send initial state
        initial_progress = job.progress or 0.0
        initial_message = job.message or "Task generation in progress..."
        
    finally:
        db_auth.close()

    # Re-check job status before subscribing: job may have completed (race fix)
    db_recheck = next(get_db())
    try:
        job_recheck = db_recheck.query(Job).filter(Job.job_id == job_id).first()
        if job_recheck:
            if job_recheck.status == JobStatus.COMPLETED:
                await websocket.accept()
                await websocket.send_json({
                    "type": "completed",
                    "progress": 100.0,
                    "message": job_recheck.message or "Task generation completed"
                })
                await websocket.close()
                return
            if job_recheck.status == JobStatus.FAILED:
                await websocket.accept()
                await websocket.send_json({
                    "type": "failed",
                    "error": job_recheck.error or "Unknown error",
                    "message": job_recheck.message or "Task generation failed"
                })
                await websocket.close()
                return
            if job_recheck.status == JobStatus.CANCELLED:
                await websocket.accept()
                await websocket.send_json({
                    "type": "failed",
                    "error": "Job was cancelled",
                    "message": "Task generation was cancelled"
                })
                await websocket.close()
                return
    finally:
        db_recheck.close()

    try:
        await websocket.accept()
        logger.info(f"WebSocket connected for task generation job {job_id}")
        
        # Send initial progress state
        await websocket.send_json({
            "type": "progress",
            "progress": initial_progress,
            "message": initial_message
        })
        
        # Subscribe to Redis pub/sub for progress updates (works across workers)
        # Uses async generator that yields updates from any worker
        ping_interval = 30
        last_ping = asyncio.get_event_loop().time()
        start_time = asyncio.get_event_loop().time()
        timeout_seconds = _websocket_job_wall_clock_seconds()

        async def ping_task():
            """Send pings to keep connection alive."""
            nonlocal last_ping
            while True:
                await asyncio.sleep(ping_interval)
                try:
                    await websocket.send_json({"type": "ping"})
                    last_ping = asyncio.get_event_loop().time()
                except Exception:
                    break

        # Start ping task in background
        ping_coro = asyncio.create_task(ping_task())
        
        try:
            # Listen for updates via Redis pub/sub (or in-memory fallback)
            async for update in job_progress_notifier.subscribe_redis(job_id):
                # Check timeout
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout_seconds:
                    logger.warning(f"WebSocket timeout for job {job_id}")
                    await websocket.send_json({
                        "type": "failed",
                        "error": "Connection timeout",
                        "message": "Task generation is taking too long"
                    })
                    break
                
                # Send update to client
                await websocket.send_json(update)
                
                # Close connection if job completed or failed
                if update.get("type") in ("completed", "failed"):
                    logger.info(f"Job {job_id} finished with status: {update.get('type')}")
                    break
                    
        except WebSocketDisconnect:
            logger.info(f"Client disconnected from job {job_id}")
        finally:
            ping_coro.cancel()
            try:
                await ping_coro
            except asyncio.CancelledError:
                pass
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for job {job_id}")
    except Exception as e:
        logger.error(f"WebSocket error for job {job_id}: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass


@router.websocket("/simulate-ai/{job_id}")
async def websocket_simulate_ai(
    websocket: WebSocket,
    job_id: str,
    token: str = Query(..., description="JWT access token"),
):
    """
    WebSocket endpoint for real-time AI simulation progress.
    
    Connect with: ws://host/api/v1/ws/simulate-ai/{job_id}?token={jwt_token}
    
    Receives progress updates as they happen via Redis pub/sub (works across Gunicorn workers).
    Messages: {"type": "progress|completed|failed", "progress": float, "message": str, "error"?: str}
    
    Also includes respondents_completed and respondents_requested for progress calculation.
    Connection closes automatically when job completes or fails.
    Timeout after 30 minutes to prevent orphaned connections (simulation can take longer).
    """
    # Auth phase: verify token and job ownership
    db_auth = next(get_db())
    initial_progress = 0.0
    initial_message = "Simulating AI respondents..."
    respondents_requested = 0
    study_id_str: Optional[str] = None
    
    try:
        user = await get_user_from_token(token, db_auth)
        if not user:
            logger.warning("WebSocket simulate-ai rejected: invalid or expired token")
            await websocket.close(code=4001, reason="Invalid or expired token")
            return
        
        # Check if job exists
        job = db_auth.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            logger.warning(f"WebSocket simulate-ai rejected: job {job_id} not found")
            await websocket.close(code=4004, reason="Job not found")
            return
        
        study_id_str = job.study_id
        
        # Allow any study creator or member to see job progress
        if not check_study_access(db_auth, UUID(job.study_id), user.id):
            logger.warning(f"WebSocket simulate-ai rejected: access denied for user {user.id} to study {job.study_id}")
            await websocket.close(code=4003, reason="Access denied to study")
            return
        
        # Extract respondents_requested from job result/payload if available
        try:
            job_result = job.result if isinstance(job.result, dict) else {}
            payload = job_result.get("payload", {})
            respondents_requested = payload.get("max_respondents", 0) or payload.get("respondents_requested", 0)
        except Exception:
            pass
        
        # Check if job is already completed or failed
        if job.status == JobStatus.COMPLETED:
            await websocket.accept()
            await websocket.send_json({
                "type": "completed",
                "progress": 100.0,
                "message": job.message or "AI simulation completed",
                "respondents_completed": respondents_requested,
                "respondents_requested": respondents_requested
            })
            await websocket.close()
            return
        
        if job.status == JobStatus.FAILED:
            await websocket.accept()
            await websocket.send_json({
                "type": "failed",
                "error": job.error or "Unknown error",
                "message": job.message or "AI simulation failed",
                "respondents_completed": 0,
                "respondents_requested": respondents_requested
            })
            await websocket.close()
            return
        
        if job.status == JobStatus.CANCELLED:
            await websocket.accept()
            await websocket.send_json({
                "type": "failed",
                "error": "Job was cancelled",
                "message": "AI simulation was cancelled",
                "respondents_completed": 0,
                "respondents_requested": respondents_requested
            })
            await websocket.close()
            return
        
        # Get current progress to send initial state
        initial_progress = job.progress or 0.0
        initial_message = job.message or "Simulating AI respondents..."
        
    finally:
        db_auth.close()

    # Re-check job status before subscribing (race condition fix)
    db_recheck = next(get_db())
    try:
        job_recheck = db_recheck.query(Job).filter(Job.job_id == job_id).first()
        if job_recheck:
            # Re-extract respondents_requested in case it was updated
            try:
                job_result = job_recheck.result if isinstance(job_recheck.result, dict) else {}
                payload = job_result.get("payload", {})
                respondents_requested = payload.get("max_respondents", 0) or payload.get("respondents_requested", 0) or respondents_requested
            except Exception:
                pass
            
            if job_recheck.status == JobStatus.COMPLETED:
                await websocket.accept()
                await websocket.send_json({
                    "type": "completed",
                    "progress": 100.0,
                    "message": job_recheck.message or "AI simulation completed",
                    "respondents_completed": respondents_requested,
                    "respondents_requested": respondents_requested
                })
                await websocket.close()
                return
            if job_recheck.status == JobStatus.FAILED:
                await websocket.accept()
                await websocket.send_json({
                    "type": "failed",
                    "error": job_recheck.error or "Unknown error",
                    "message": job_recheck.message or "AI simulation failed",
                    "respondents_completed": 0,
                    "respondents_requested": respondents_requested
                })
                await websocket.close()
                return
            if job_recheck.status == JobStatus.CANCELLED:
                await websocket.accept()
                await websocket.send_json({
                    "type": "failed",
                    "error": "Job was cancelled",
                    "message": "AI simulation was cancelled",
                    "respondents_completed": 0,
                    "respondents_requested": respondents_requested
                })
                await websocket.close()
                return
            
            # Update initial progress from recheck
            initial_progress = job_recheck.progress or initial_progress
            initial_message = job_recheck.message or initial_message
    finally:
        db_recheck.close()

    try:
        await websocket.accept()
        logger.info(f"WebSocket connected for AI simulation job {job_id}")
        
        # Calculate initial completed count
        initial_completed = int((initial_progress / 100.0) * respondents_requested) if respondents_requested > 0 else 0
        
        # Send initial progress state
        await websocket.send_json({
            "type": "progress",
            "progress": initial_progress,
            "message": initial_message,
            "respondents_completed": initial_completed,
            "respondents_requested": respondents_requested
        })
        
        # Subscribe to Redis pub/sub for progress updates (works across workers)
        ping_interval = 30
        start_time = asyncio.get_event_loop().time()
        timeout_seconds = _websocket_job_wall_clock_seconds()
        
        async def ping_task():
            """Send pings to keep connection alive."""
            while True:
                await asyncio.sleep(ping_interval)
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
        
        # Start ping task in background
        ping_coro = asyncio.create_task(ping_task())
        
        try:
            # Listen for updates via Redis pub/sub (or in-memory fallback)
            async for update in job_progress_notifier.subscribe_redis(job_id):
                # Check timeout
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout_seconds:
                    logger.warning(f"WebSocket timeout for simulation job {job_id}")
                    await websocket.send_json({
                        "type": "failed",
                        "error": "Connection timeout",
                        "message": "AI simulation is taking too long",
                        "respondents_completed": 0,
                        "respondents_requested": respondents_requested
                    })
                    break
                
                # Calculate respondents_completed from progress if not in update
                progress_val = update.get("progress", 0)
                if "respondents_completed" not in update and respondents_requested > 0:
                    update["respondents_completed"] = int((progress_val / 100.0) * respondents_requested)
                if "respondents_requested" not in update:
                    update["respondents_requested"] = respondents_requested
                
                # Send update to client
                await websocket.send_json(update)
                
                # Close connection if job completed or failed
                if update.get("type") in ("completed", "failed"):
                    logger.info(f"Simulation job {job_id} finished with status: {update.get('type')}")
                    break
                    
        except WebSocketDisconnect:
            logger.info(f"Client disconnected from simulation job {job_id}")
        finally:
            ping_coro.cancel()
            try:
                await ping_coro
            except asyncio.CancelledError:
                pass
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for simulation job {job_id}")
    except Exception as e:
        logger.error(f"WebSocket error for simulation job {job_id}: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass


@router.websocket("/job-progress/{job_id}")
async def websocket_job_progress(
    websocket: WebSocket,
    job_id: str,
    token: str = Query(..., description="JWT access token"),
):
    """
    Generic WebSocket endpoint for real-time job progress updates.
    Works for any job type (export, task generation, etc.).
    
    Connect with: ws://host/api/v1/ws/job-progress/{job_id}?token={jwt_token}
    
    Messages: {"type": "progress|completed|failed", "progress": float, "message": str, ...}
    """
    db_auth = next(get_db())
    try:
        user = await get_user_from_token(token, db_auth)
        if not user:
            await websocket.close(code=4001, reason="Invalid or expired token")
            return
        
        job = db_auth.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            await websocket.close(code=4004, reason="Job not found")
            return
        
        if job.user_id != str(user.id):
            await websocket.close(code=4003, reason="Access denied")
            return
        
        if job.status == JobStatus.COMPLETED:
            await websocket.accept()
            result = job.result or {}
            await websocket.send_json({
                "type": "completed",
                "progress": 100.0,
                "message": job.message or "Job completed",
                "download_url": result.get("download_url"),
                "filename": result.get("filename"),
            })
            await websocket.close()
            return
        
        if job.status == JobStatus.FAILED:
            await websocket.accept()
            await websocket.send_json({
                "type": "failed",
                "error": job.error or "Unknown error",
                "message": job.message or "Job failed"
            })
            await websocket.close()
            return
        
        if job.status == JobStatus.CANCELLED:
            await websocket.accept()
            await websocket.send_json({
                "type": "failed",
                "error": "Job was cancelled",
                "message": "Job was cancelled"
            })
            await websocket.close()
            return
        
        initial_progress = job.progress or 0.0
        initial_message = job.message or "Processing..."
        
    finally:
        db_auth.close()

    try:
        await websocket.accept()
        await websocket.send_json({
            "type": "progress",
            "progress": initial_progress,
            "message": initial_message
        })
        
        timeout_seconds = _websocket_job_wall_clock_seconds()
        start_time = asyncio.get_event_loop().time()
        
        async def ping_loop():
            while True:
                try:
                    await asyncio.sleep(25)
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
        
        ping_coro = asyncio.create_task(ping_loop())
        
        try:
            async for update in job_progress_notifier.subscribe_redis(job_id):
                # Check timeout
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout_seconds:
                    logger.warning(f"WebSocket timeout for job {job_id}")
                    await websocket.send_json({
                        "type": "failed",
                        "error": "Connection timeout",
                        "message": "Job timed out"
                    })
                    break
                
                await websocket.send_json(update)
                
                if update.get("type") in ("completed", "failed"):
                    logger.info(f"Job {job_id} finished with status: {update.get('type')}")
                    break
                    
        except WebSocketDisconnect:
            logger.info(f"Client disconnected from job {job_id}")
        finally:
            ping_coro.cancel()
            try:
                await ping_coro
            except asyncio.CancelledError:
                pass
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for job {job_id}")
    except Exception as e:
        logger.error(f"WebSocket error for job {job_id}: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass


@router.websocket("/user/jobs")
async def websocket_user_jobs(
    websocket: WebSocket,
    token: str = Query(..., description="JWT access token"),
):
    """
    Global WebSocket for all background job notifications (task generation, synthetic AI).

    Connect with: ws://host/api/v1/ws/user/jobs?token={jwt_token}

    On connect sends: {"event": "snapshot", "jobs": [...]}
    Then streams: {"event": "job_update", "job_id": "...", "progress": ..., ...}
    """
    db_auth = next(get_db())
    user_id_str: str | None = None
    try:
        user = await get_user_from_token(token, db_auth)
        if not user:
            await websocket.close(code=4001, reason="Invalid or expired token")
            return
        user_id_str = str(user.id)
    finally:
        db_auth.close()

    from app.services.job_notification_service import get_user_notifications

    db_snapshot = next(get_db())
    try:
        payload = get_user_notifications(
            db_snapshot,
            UUID(user_id_str),
            active_only=False,
        )
        snapshot = payload.get("jobs") or payload.get("notifications") or []
        active = [j for j in snapshot if j.get("status") in ("pending", "started", "processing")]
        recent_terminal = [
            j for j in snapshot
            if j.get("status") in ("completed", "failed", "cancelled")
        ][:10]
        snapshot = active + recent_terminal
    finally:
        db_snapshot.close()

    try:
        await websocket.accept()
        logger.info(f"Global job WebSocket connected for user {user_id_str}")

        await websocket.send_json({"event": "snapshot", "jobs": snapshot})

        ping_interval = 30
        start_time = asyncio.get_event_loop().time()
        timeout_seconds = _websocket_job_wall_clock_seconds()

        async def ping_task():
            while True:
                await asyncio.sleep(ping_interval)
                try:
                    await websocket.send_json({"event": "ping"})
                except Exception:
                    break

        ping_coro = asyncio.create_task(ping_task())

        try:
            async for update in job_progress_notifier.subscribe_user_redis(user_id_str):
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout_seconds:
                    logger.warning(f"Global job WebSocket timeout for user {user_id_str}")
                    await websocket.send_json({
                        "event": "error",
                        "message": "Connection timeout — reconnect to continue receiving updates",
                    })
                    break

                event_type = update.get("event") or update.get("type")
                if event_type == "ping":
                    continue

                await websocket.send_json(update)
        except WebSocketDisconnect:
            logger.info(f"Global job WebSocket disconnected for user {user_id_str}")
        finally:
            ping_coro.cancel()
            try:
                await ping_coro
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        logger.info(f"Global job WebSocket disconnected for user {user_id_str}")
    except Exception as e:
        logger.error(f"Global job WebSocket error for user {user_id_str}: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass
