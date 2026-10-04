"""
Celery application configuration for background task processing.

- Broker: RabbitMQ (AMQP / AMQPS) via CELERY_BROKER_URL_* from settings.
- Results: rpc:// by default (uses broker); override with CELERY_RESULT_BACKEND (e.g. Redis).

Windows: Use --pool=solo (prefork is unsupported on Windows).
"""
import sys
from celery import Celery
from app.core.config import settings

BROKER_URL = settings.get_celery_broker_url()
RESULT_BACKEND = settings.get_celery_result_backend()

# Redis result backend: optional ssl_cert_reqs for rediss://
if RESULT_BACKEND.startswith("rediss://") and "ssl_cert_reqs" not in RESULT_BACKEND:
    sep = "&" if "?" in RESULT_BACKEND else "?"
    RESULT_BACKEND = f"{RESULT_BACKEND}{sep}ssl_cert_reqs=CERT_NONE"

celery_app = Celery(
    "mindsurve_celery",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["app.tasks.celery_jobs", "app.tasks.video_encode"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=86400,
    task_time_limit=28800,       # 8h hard limit
    task_soft_time_limit=28200,  # 7h50m soft limit (10m before hard)
    broker_heartbeat=1800,       # 30 min heartbeat (match RabbitMQ)
    broker_connection_timeout=30,
    task_routes={
        "celery_job.encode_video": {"queue": "video"},
    },
)

# Redis-only transport options (skip for rpc:// or amqp)
if RESULT_BACKEND.startswith("redis"):
    celery_app.conf.result_backend_transport_options = {"global_keyprefix": "{celery}:"}

# Windows: prefork is unsupported; use solo pool
if sys.platform == "win32":
    celery_app.conf.worker_pool = "solo"
