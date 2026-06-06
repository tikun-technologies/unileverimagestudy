from fastapi import APIRouter, Depends, HTTPException, status
import logging, traceback
from sqlalchemy.orm import Session
from typing import List
import uuid

from app.db.session import get_db
from app.schemas.user_schema import (
    UserLogin, UserRegister, UserResponse, UserUpdate, 
    PasswordChange, Token, TokenRefresh, TokenRefreshResponse,
    ValidateTokenRequest, ValidateTokenResponse,
    UserLoginResponse, ForgotPasswordRequest, ResetPasswordRequest, PasswordResetResponse,
    OAuthData, OAuthLoginResponse
)
from app.services.user import (
    authenticate_user_with_tokens, create_user_with_tokens,
    refresh_user_tokens, UserService, request_password_reset,
    reset_password, get_user_by_reset_token, oauth_login
)
from app.services.user_billing import build_user_response, refresh_access_token_for_user
from app.core.dependencies import get_current_user, get_current_active_user
from app.core.security import verify_token
from app.models.user_model import User

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/register", response_model=UserLoginResponse, status_code=status.HTTP_201_CREATED)
async def register_user(user_data: UserRegister, db: Session = Depends(get_db)):
    """
    Register a new user and return user data with JWT tokens
    """
    try:
        logger.debug("Register request: email=%s", user_data.email)
        result = create_user_with_tokens(db, user_data)
        return UserLoginResponse(**result)
    except ValueError as e:
        logger.info("Register validation error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("Register failed with unexpected error")
        # Surface actual error for debugging
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.post("/login", response_model=UserLoginResponse)
async def login_user(login_data: UserLogin, db: Session = Depends(get_db)):
    """
    Login user with email and password, return JWT tokens
    """
    result = authenticate_user_with_tokens(
        db, 
        login_data.email, 
        login_data.password
    )
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )
    
    return UserLoginResponse(**result)


@router.post("/oauth-login", response_model=OAuthLoginResponse)
async def oauth_login_endpoint(oauth_data: OAuthData, db: Session = Depends(get_db)):
    """
    OAuth login endpoint - handles both new and existing users
    """
    try:
        logger.debug("OAuth login request: email=%s provider=%s", oauth_data.email, oauth_data.provider)
        result = oauth_login(db, oauth_data)
        return OAuthLoginResponse(**result)
    except ValueError as e:
        logger.info("OAuth login validation error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("OAuth login failed with unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OAuth login failed"
        )


@router.post("/refresh", response_model=TokenRefreshResponse)
async def refresh_tokens(token_data: TokenRefresh, db: Session = Depends(get_db)):
    """
    Refresh access token using refresh token
    """
    result = refresh_user_tokens(db, token_data.refresh_token)
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token"
        )
    
    return TokenRefreshResponse(**result)


@router.post("/validate-token", response_model=ValidateTokenResponse)
async def validate_token(request: ValidateTokenRequest, db: Session = Depends(get_db)):
    """
    Fast token validation - validates access token, optionally refreshes if expired.
    Plan is read from the JWT payload (refreshed from DB when a new access token is issued).
    """
    payload = verify_token(request.access_token, "access")
    if payload:
        return ValidateTokenResponse(
            valid=True,
            user_id=payload.get("sub"),
            email=payload.get("email"),
            plan=payload.get("plan"),
            subscription_status=payload.get("subscription_status"),
        )
    if request.refresh_token:
        refreshed = refresh_access_token_for_user(db, request.refresh_token)
        if refreshed:
            sub = verify_token(refreshed["access_token"], "access")
            return ValidateTokenResponse(
                valid=True,
                access_token=refreshed["access_token"],
                user_id=sub.get("sub") if sub else None,
                email=sub.get("email") if sub else None,
                plan=sub.get("plan") if sub else None,
                subscription_status=sub.get("subscription_status") if sub else None,
            )
    return ValidateTokenResponse(
        valid=False,
        error="Invalid or expired token",
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Get current user profile information
    """
    return build_user_response(db, current_user)


@router.put("/me", response_model=UserResponse)
async def update_user_profile(
    user_data: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Update current user profile information
    """
    service = UserService(db)
    updated_user = service.update_user(current_user.id, user_data)
    
    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return UserResponse.from_orm(updated_user)


@router.post("/change-password")
async def change_password(
    password_data: PasswordChange,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Change user password
    """
    service = UserService(db)
    success = service.change_password(current_user.id, password_data)
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect"
        )
    
    return {"message": "Password changed successfully"}


@router.get("/users", response_model=List[UserResponse])
async def get_users(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Get list of users (admin function)
    """
    service = UserService(db)
    users = service.get_users(skip=skip, limit=limit)
    return [UserResponse.from_orm(user) for user in users]


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user_by_id(
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Get user by ID
    """
    service = UserService(db)
    user = service.get_user_by_id(user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return UserResponse.from_orm(user)


@router.get("/search", response_model=List[UserResponse])
async def search_users(
    q: str,
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Search users by name or email
    """
    service = UserService(db)
    users = service.search_users(q, skip=skip, limit=limit)
    return [UserResponse.from_orm(user) for user in users]


@router.post("/verify/{user_id}")
async def verify_user(
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Verify a user (admin function)
    """
    service = UserService(db)
    success = service.verify_user(user_id)
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return {"message": "User verified successfully"}


@router.delete("/me")
async def deactivate_user(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Deactivate current user account (soft delete)
    """
    service = UserService(db)
    success = service.deactivate_user(current_user.id)
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return {"message": "Account deactivated successfully"}




@router.get("/check-email/{email}")
async def check_email_available(
    email: str,
    db: Session = Depends(get_db)
):
    """
    Check if email is available
    """
    service = UserService(db)
    available = service.check_email_available(email)
    
    return {
        "email": email,
        "available": available
    }


@router.post("/forgot-password", response_model=PasswordResetResponse)
async def forgot_password(
    request: ForgotPasswordRequest,
    db: Session = Depends(get_db)
):
    """
    Request password reset for user
    """
    try:
        # Process password reset request
        success = request_password_reset(db, request.email)
        
        if success:
            return PasswordResetResponse(
                message="If an account with that email exists, a password reset link has been sent."
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to process password reset request"
            )
            
    except Exception as e:
        logger.exception("Error in forgot password endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process password reset request"
        )


@router.post("/reset-password", response_model=PasswordResetResponse)
async def reset_password_endpoint(
    request: ResetPasswordRequest,
    db: Session = Depends(get_db)
):
    """
    Reset user password using token
    """
    try:
        # Validate token first
        user = get_user_by_reset_token(db, request.token)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token"
            )
        
        # Reset password
        success = reset_password(db, request.token, request.new_password)
        
        if success:
            return PasswordResetResponse(
                message="Password has been reset successfully. You can now login with your new password."
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to reset password. Please try again."
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error in reset password endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset password"
        )


@router.get("/validate-reset-token/{token}")
async def validate_reset_token(
    token: str,
    db: Session = Depends(get_db)
):
    """
    Validate password reset token
    """
    try:
        user = get_user_by_reset_token(db, token)
        
        if user:
            return {
                "valid": True,
                "message": "Token is valid"
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error validating reset token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to validate reset token"
        )
