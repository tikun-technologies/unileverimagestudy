import asyncio
import contextlib
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import logging

from app.api.v1 import api_router
from app.api.v1.study import router as study_router
from app.api.v1.response import router as response_router
from app.api.v1.uploads import router as uploads_router
from app.api.v1.panelist import router as panelist_router
from app.api.v1.project import router as project_router
from app.api.v1.websocket import router as websocket_router
from app.api.v1.template import router as template_router
from app.core.config import settings
from app.core.cloudinary_config import init_cloudinary

_appinsights_cs = (settings.APPLICATIONINSIGHTS_CONNECTION_STRING or "").strip()
if _appinsights_cs:
    print("Application Insights: connection string present, calling configure_azure_monitor")
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(connection_string=_appinsights_cs)
    print("Application Insights: configure_azure_monitor finished")
else:
    print("Application Insights: skipped (APPLICATIONINSIGHTS_CONNECTION_STRING empty or missing)")

app = FastAPI(
    title="mindsurve API",
    description="API for user authentication and management",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this properly for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(api_router, prefix="/api/v1")
app.include_router(study_router, prefix="/api/v1/studies", tags=["studies"])
app.include_router(response_router, prefix="/api/v1/responses", tags=["responses"])
app.include_router(uploads_router, prefix="/api/v1/uploads", tags=["uploads"])
app.include_router(panelist_router, prefix="/api/v1/panelist", tags=["panelist"])
app.include_router(project_router, prefix="/api/v1/projects", tags=["projects"])
app.include_router(template_router, prefix="/api/v1/templates", tags=["templates"])
app.include_router(websocket_router, prefix="/api/v1/ws", tags=["websocket"])


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger = logging.getLogger("uvicorn.error")
    errors = exc.errors()
    logger.error(f"422 Validation Error: {errors}")
    return JSONResponse(
        status_code=422,
        content={"detail": errors},
    )


@app.get("/")
async def root():
    return {"message": "mindsurve API", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.on_event("startup")
async def on_startup():
    # Ensure progress and info logs (e.g. synthetic respondent job) appear in terminal
    # logging.basicConfig(level=logging.INFO)
    # app_logger = logging.getLogger("app")
    # app_logger.setLevel(logging.INFO)
    # if not app_logger.handlers:
    #     h = logging.StreamHandler()
    #     h.setLevel(logging.INFO)
    #     app_logger.addHandler(h)
    print("Starting application startup...")
    # Initialize Cloudinary once
    init_cloudinary()
    print("Cloudinary initialized")

    if settings.RUN_API_BACKGROUND_SERVICES:
        # These in-process loops should run only in a single-purpose process, not once per Gunicorn worker.
        from app.services.background_tasks import background_task_service
        background_task_service.task = asyncio.create_task(
            background_task_service.start_abandonment_checker(interval_minutes=15)
        )
        print("done background task")

        # Start task generation cleanup service
        from app.services.background_task_service import background_task_service as task_service
        task_service._cleanup_task = asyncio.create_task(task_service._cleanup_old_jobs())
        print("done background cleanup")
    else:
        print("Skipping API background services (RUN_API_BACKGROUND_SERVICES is disabled)")

    print("Startup function completed successfully!")

@app.on_event("shutdown")
async def on_shutdown():
    # Stop background tasks
    from app.services.background_tasks import background_task_service
    background_task_service.stop_abandonment_checker()

    from app.services.background_task_service import background_task_service as task_service
    if task_service._cleanup_task and not task_service._cleanup_task.done():
        task_service._cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task_service._cleanup_task
    
    # Close Redis connections
    from app.core.redis import close_redis_pools
    await close_redis_pools()