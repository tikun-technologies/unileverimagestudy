from fastapi import APIRouter
from app.api.v1.user import router as user_router
from app.api.v1.billing import router as billing_router

api_router = APIRouter()

# Include user routes
api_router.include_router(user_router, prefix="/auth", tags=["authentication"])
api_router.include_router(billing_router, prefix="/billing", tags=["billing"])
