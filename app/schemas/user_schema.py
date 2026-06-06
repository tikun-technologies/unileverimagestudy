from pydantic import BaseModel, EmailStr, Field, validator
from typing import Optional
from datetime import datetime
import uuid

from app.schemas.billing_schema import PlanLimitsOut, PlanType, SubscriptionStatus


class UserLogin(BaseModel):
    """Schema for user login - accepts email only"""
    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=6, description="User password")


class UserRegister(BaseModel):
    """Schema for user registration"""
    email: EmailStr = Field(..., description="User email address")
    name: str = Field(..., min_length=2, max_length=100, description="Full name")
    password: str = Field(..., min_length=6, description="User password")
    phone: Optional[str] = Field(None, max_length=20, description="Phone number")
    date_of_birth: Optional[datetime] = Field(None, description="Date of birth")

    @validator('name')
    def validate_name(cls, v):
        return v.strip().title()


class UserResponse(BaseModel):
    """Schema for user response (without sensitive data)"""
    id: uuid.UUID
    email: str
    name: str
    phone: Optional[str]
    date_of_birth: Optional[datetime]
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime
    last_login: Optional[datetime]
    plan: PlanType = "free"
    subscription_status: SubscriptionStatus = "none"
    billing_limits: Optional[PlanLimitsOut] = None
    has_active_subscription: bool = False

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    """Schema for updating user information"""
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    phone: Optional[str] = Field(None, max_length=20)
    date_of_birth: Optional[datetime] = None

    @validator('name')
    def validate_name(cls, v):
        if v is not None:
            return v.strip().title()
        return v


class PasswordChange(BaseModel):
    """Schema for password change"""
    current_password: str = Field(..., description="Current password")
    new_password: str = Field(..., min_length=6, description="New password")

    @validator('new_password')
    def validate_new_password(cls, v):
        if len(v) < 6:
            raise ValueError('Password must be at least 6 characters long')
        return v


class Token(BaseModel):
    """Schema for JWT token response"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    """Schema for token refresh request"""
    refresh_token: str


class TokenRefreshResponse(BaseModel):
    """Schema for token refresh response"""
    access_token: str
    token_type: str = "bearer"


class ValidateTokenRequest(BaseModel):
    """Schema for token validation request"""
    access_token: str = Field(..., description="JWT access token")
    refresh_token: Optional[str] = Field(None, description="Optional refresh token to get new access token if expired")


class ValidateTokenResponse(BaseModel):
    """Schema for token validation response - fast, no DB for sub-10ms"""
    valid: bool
    access_token: Optional[str] = None
    user_id: Optional[str] = None
    email: Optional[str] = None
    plan: Optional[PlanType] = None
    subscription_status: Optional[SubscriptionStatus] = None
    error: Optional[str] = None


class UserLoginResponse(BaseModel):
    """Schema for successful login response"""
    user: UserResponse
    tokens: Token


class ForgotPasswordRequest(BaseModel):
    """Schema for forgot password request"""
    email: EmailStr = Field(..., description="User email address")


class ResetPasswordRequest(BaseModel):
    """Schema for password reset request"""
    token: str = Field(..., min_length=32, description="Password reset token")
    new_password: str = Field(..., min_length=6, description="New password")

    @validator('new_password')
    def validate_new_password(cls, v):
        if len(v) < 6:
            raise ValueError('Password must be at least 6 characters long')
        return v


class PasswordResetResponse(BaseModel):
    """Schema for password reset response"""
    message: str


class OAuthData(BaseModel):
    """Schema for OAuth login data from frontend"""
    email: EmailStr = Field(..., description="User email from OAuth provider")
    name: str = Field(..., min_length=2, max_length=100, description="Full name from OAuth provider")
    provider: str = Field(..., description="OAuth provider (google, apple, etc.)")
    provider_id: str = Field(..., description="User ID from OAuth provider")
    profile_picture: Optional[str] = Field(None, description="Profile picture URL from OAuth provider")
    
    @validator('name')
    def validate_name(cls, v):
        return v.strip().title()


class OAuthLoginResponse(BaseModel):
    """Schema for OAuth login response"""
    user: UserResponse
    tokens: Token
    is_new_user: bool


# Rebuild models to resolve forward references
def rebuild_models():
    """Rebuild all models to resolve forward references"""
    # Get all model classes defined in this module
    import sys
    current_module = sys.modules[__name__]
    
    for name in dir(current_module):
        obj = getattr(current_module, name)
        if (isinstance(obj, type) and 
            issubclass(obj, BaseModel) and 
            obj != BaseModel and
            obj.__module__ == __name__):
            try:
                obj.model_rebuild()
            except Exception as e:
                print(f"Warning: Could not rebuild {name}: {e}")

# Call rebuild to resolve forward references
rebuild_models()
