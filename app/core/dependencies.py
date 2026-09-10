from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import Optional
import uuid

from app.db.session import get_db
from app.core.security import verify_token
from app.services.user import get_user_by_id

# Security scheme
security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    """Get current authenticated user from JWT token"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        # Extract token from Bearer header
        token = credentials.credentials
        
        # Verify token
        payload = verify_token(token, "access")
        if payload is None:
            raise credentials_exception
        
        # Get user ID from token
        user_id_str = payload.get("sub")
        if user_id_str is None:
            raise credentials_exception
        
        # Convert to UUID
        try:
            user_id = uuid.UUID(user_id_str)
        except ValueError:
            raise credentials_exception
        
        # Get user from database
        user = get_user_by_id(db, user_id)
        if user is None:
            raise credentials_exception
        
        return user
        
    except Exception:
        raise credentials_exception


def get_current_active_user(current_user = Depends(get_current_user)):
    """Get current active user"""
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )
    return current_user


def get_current_verified_user(current_user = Depends(get_current_active_user)):
    """Get current verified user"""
    if not current_user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User not verified"
        )
    return current_user


def get_optional_current_active_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
    db: Session = Depends(get_db),
):
    """Return the active user when a Bearer token is present; otherwise None."""
    if not credentials:
        return None

    try:
        token = credentials.credentials
        payload = verify_token(token, "access")
        if payload is None:
            return None

        user_id_str = payload.get("sub")
        if user_id_str is None:
            return None

        try:
            user_id = uuid.UUID(user_id_str)
        except ValueError:
            return None

        user = get_user_by_id(db, user_id)
        return user if user and user.is_active else None
    except Exception:
        return None


def get_optional_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
    db: Session = Depends(get_db)
):
    """Get current user if token is provided, otherwise return None"""
    if not credentials:
        return None
    
    try:
        token = credentials.credentials
        payload = verify_token(token, "access")
        if payload is None:
            return None
        
        user_id_str = payload.get("sub")
        if user_id_str is None:
            return None
        
        try:
            user_id = uuid.UUID(user_id_str)
        except ValueError:
            return None
        
        user = get_user_by_id(db, user_id)
        return user if user and user.is_active else None
        
    except Exception:
        return None
