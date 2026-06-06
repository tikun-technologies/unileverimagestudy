from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional, List
from datetime import datetime, timedelta
import uuid
import logging

from app.models.user_model import User
from app.schemas.user_schema import UserRegister, UserUpdate, PasswordChange, UserResponse, OAuthData
from app.services.user_billing import build_user_response, create_tokens_for_user, refresh_access_token_for_user
from app.core.security import get_password_hash, verify_password, generate_password_reset_token
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

class UserService:
    def __init__(self, db: Session):
        self.db = db

    def authenticate_user(self, email: str, password: str) -> Optional[User]:
        """Authenticate user with email and password"""
        logger.debug("Authenticating user: %s", email)
        user = self.db.query(User).filter(
            User.email == email.lower(),
            User.is_active == True
        ).first()
        
        if not user:
            logger.info("Auth failed: user not found or inactive for %s", email)
            return None
        
        if not verify_password(password, user.password_hash):
            logger.info("Auth failed: bad password for user_id=%s", user.id)
            return None
        
        # Update last login
        user.last_login = datetime.utcnow()
        self.db.commit()
        
        return user

    def authenticate_user_with_tokens(self, email: str, password: str) -> Optional[dict]:
        """Authenticate user and return user data with JWT tokens"""
        try:
            user = self.authenticate_user(email, password)
            if not user:
                return None
            tokens = create_tokens_for_user(self.db, user)
            user_response = build_user_response(self.db, user)
            return {"user": user_response, "tokens": tokens}
        except Exception:
            logger.exception("Token generation failed during authentication for %s", email)
            return None

    def create_user(self, user_data: UserRegister) -> User:
        """Create a new user"""
        logger.debug("Creating user: email=%s", user_data.email)
        # Check if email already exists
        existing_user = self.db.query(User).filter(
            User.email == user_data.email.lower()
        ).first()
        
        if existing_user:
            logger.info("Create user failed: email exists: %s", user_data.email)
            raise ValueError("Email already exists")
        
        # Create new user
        hashed_password = get_password_hash(user_data.password)
        
        db_user = User(
            email=user_data.email.lower(),
            name=user_data.name,
            password_hash=hashed_password,
            phone=user_data.phone,
            date_of_birth=user_data.date_of_birth,
            is_active=True,
            is_verified=False
        )
        
        try:
            self.db.add(db_user)
            self.db.commit()
            self.db.refresh(db_user)
            
            # Link any pending project or study invitations
            self._link_pending_invitations(db_user)

            from app.services.billing import BillingService
            BillingService(self.db).get_or_create_profile(db_user)

            logger.info("User created successfully: user_id=%s email=%s", db_user.id, db_user.email)
        except Exception:
            logger.exception("Create user DB error for email=%s", user_data.email)
            self.db.rollback()
            raise
        
        return db_user

    def create_user_with_tokens(self, user_data: UserRegister) -> dict:
        """Create a new user and return user data with JWT tokens"""
        user = self.create_user(user_data)
        
        # Create JWT tokens
        tokens = create_tokens_for_user(self.db, user)
        
        # Convert user to response schema
        user_response = build_user_response(self.db, user)
        
        return {
            "user": user_response,
            "tokens": tokens
        }

    def get_user_by_id(self, user_id: uuid.UUID) -> Optional[User]:
        """Get user by ID"""
        return self.db.query(User).filter(User.id == user_id, User.is_active == True).first()


    def get_user_by_email(self, email: str) -> Optional[User]:
        """Get user by email"""
        return self.db.query(User).filter(
            User.email == email.lower(), 
            User.is_active == True
        ).first()

    def get_users(self, skip: int = 0, limit: int = 100) -> List[User]:
        """Get list of users with pagination"""
        return self.db.query(User).filter(User.is_active == True).offset(skip).limit(limit).all()

    def update_user(self, user_id: uuid.UUID, user_data: UserUpdate) -> Optional[User]:
        """Update user information"""
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        
        # Update only provided fields
        update_data = user_data.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(user, field, value)
        
        user.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)
        
        return user

    def change_password(self, user_id: uuid.UUID, password_data: PasswordChange) -> bool:
        """Change user password"""
        user = self.get_user_by_id(user_id)
        if not user:
            return False
        
        # Verify current password
        if not verify_password(password_data.current_password, user.password_hash):
            return False
        
        # Update password
        user.password_hash = get_password_hash(password_data.new_password)
        user.updated_at = datetime.utcnow()
        self.db.commit()
        
        return True

    def deactivate_user(self, user_id: uuid.UUID) -> bool:
        """Deactivate user (soft delete)"""
        user = self.get_user_by_id(user_id)
        if not user:
            return False
        
        user.is_active = False
        user.updated_at = datetime.utcnow()
        self.db.commit()
        
        return True

    def verify_user(self, user_id: uuid.UUID) -> bool:
        """Mark user as verified"""
        user = self.get_user_by_id(user_id)
        if not user:
            return False
        
        user.is_verified = True
        user.updated_at = datetime.utcnow()
        self.db.commit()
        
        return True


    def check_email_available(self, email: str) -> bool:
        """Check if email is available"""
        user = self.db.query(User).filter(User.email == email.lower()).first()
        return user is None

    def search_users(self, query: str, skip: int = 0, limit: int = 50) -> List[User]:
        """Search users by name or email"""
        search_filter = f"%{query.lower()}%"
        return self.db.query(User).filter(
            User.is_active == True,
            or_(
                User.name.ilike(search_filter),
                User.email.ilike(search_filter)
            )
        ).offset(skip).limit(limit).all()

    def refresh_user_tokens(self, refresh_token: str) -> Optional[dict]:
        """Refresh access token using refresh token (re-loads plan from DB)."""
        return refresh_access_token_for_user(self.db, refresh_token)
    
    def request_password_reset(self, email: str) -> bool:
        """
        Request password reset for user
        
        Args:
            email: Email of the user
            
        Returns:
            bool: True if reset request processed (always returns True for security)
        """
        try:
            # Find user by email
            user = self.get_user_by_email(email)
            
            if not user:
                # Always return True for security (don't reveal if user exists)
                logger.info(f"Password reset requested for non-existent user: {email}")
                return True
            
            # Generate reset token and expiration (1 hour from now)
            reset_token = generate_password_reset_token()
            reset_expires = datetime.utcnow() + timedelta(hours=1)
            
            # Update user with reset token
            user.password_reset_token = reset_token
            user.password_reset_expires = reset_expires
            user.updated_at = datetime.utcnow()
            self.db.commit()
            
            # Send password reset email
            email_sent = email_service.send_password_reset_email(
                user_email=user.email,
                user_name=user.name,
                reset_token=reset_token
            )
            
            if email_sent:
                logger.info(f"Password reset email sent to {user.email}")
            else:
                logger.error(f"Failed to send password reset email to {user.email}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing password reset request for {email}: {str(e)}")
            # Always return True for security
            return True
    
    def reset_password(self, token: str, new_password: str) -> bool:
        """
        Reset user password using token
        
        Args:
            token: Password reset token
            new_password: New password
            
        Returns:
            bool: True if password reset successfully, False otherwise
        """
        try:
            # Find user by reset token
            user = self.db.query(User).filter(
                User.password_reset_token == token,
                User.password_reset_expires > datetime.utcnow(),
                User.is_active == True
            ).first()
            
            if not user:
                logger.warning(f"Invalid or expired password reset token: {token}")
                return False
            
            # Update password and clear reset token
            user.password_hash = get_password_hash(new_password)
            user.password_reset_token = None
            user.password_reset_expires = None
            user.updated_at = datetime.utcnow()
            self.db.commit()
            
            logger.info(f"Password reset successfully for user: {user.email}")
            return True
            
        except Exception as e:
            logger.error(f"Error resetting password with token {token}: {str(e)}")
            self.db.rollback()
            return False
    
    def get_user_by_reset_token(self, token: str) -> Optional[User]:
        """
        Get user by password reset token (for validation)
        
        Args:
            token: Password reset token
            
        Returns:
            User if token is valid and not expired, None otherwise
        """
        try:
            return self.db.query(User).filter(
                User.password_reset_token == token,
                User.password_reset_expires > datetime.utcnow(),
                User.is_active == True
            ).first()
        except Exception as e:
            logger.error(f"Error getting user by reset token: {str(e)}")
            return None

    def _link_pending_invitations(self, db_user: User):
        """Link any pending project or study invitations to the new user ID"""
        from sqlalchemy import update
        from app.models.project_model import ProjectMember
        from app.models.study_model import StudyMember
        
        try:
            # 1. Link project invitations
            self.db.execute(
                update(ProjectMember)
                .where(ProjectMember.invited_email == db_user.email, ProjectMember.user_id == None)
                .values(user_id=db_user.id)
            )
            
            # 2. Link study invitations
            self.db.execute(
                update(StudyMember)
                .where(StudyMember.invited_email == db_user.email, StudyMember.user_id == None)
                .values(user_id=db_user.id)
            )
            
            self.db.commit()
            logger.info("Linked pending invitations for user: %s", db_user.email)
        except Exception:
            logger.exception("Failed to link pending invitations for user: %s", db_user.email)
            self.db.rollback()

    def create_user_from_oauth(self, oauth_data: OAuthData) -> User:
        """
        Create a new user from OAuth data
        
        Args:
            oauth_data: OAuth user data from provider
            
        Returns:
            Created user
        """
        logger.debug("Creating OAuth user: email=%s provider=%s", oauth_data.email, oauth_data.provider)
        
        # Check if user already exists (shouldn't happen in normal flow, but safety check)
        existing_user = self.get_user_by_email(oauth_data.email)
        if existing_user:
            logger.info("OAuth user already exists: %s", oauth_data.email)
            return existing_user
        
        # Create new user with OAuth data
        # Generate a random password since OAuth users don't have passwords
        random_password = f"oauth_{oauth_data.provider}_{oauth_data.provider_id}_{uuid.uuid4().hex[:16]}"
        hashed_password = get_password_hash(random_password)
        
        db_user = User(
            email=oauth_data.email.lower(),
            name=oauth_data.name,
            password_hash=hashed_password,
            is_active=True,
            is_verified=True  # OAuth users are pre-verified
        )
        
        try:
            self.db.add(db_user)
            self.db.commit()
            self.db.refresh(db_user)
            
            # Link any pending project or study invitations
            self._link_pending_invitations(db_user)

            from app.services.billing import BillingService
            BillingService(self.db).get_or_create_profile(db_user)
            
            logger.info("OAuth user created successfully: user_id=%s email=%s", db_user.id, db_user.email)
            return db_user
        except Exception as e:
            self.db.rollback()
            logger.exception("Create OAuth user DB error for email=%s", oauth_data.email)
            raise e

    def oauth_login(self, oauth_data: OAuthData) -> dict:
        """
        Handle OAuth login - either login existing user or create new user
        
        Args:
            oauth_data: OAuth user data from provider
            
        Returns:
            Dict with user, tokens, and is_new_user flag
        """
        logger.debug("OAuth login attempt: email=%s provider=%s", oauth_data.email, oauth_data.provider)
        
        # Check if user exists by email
        existing_user = self.get_user_by_email(oauth_data.email)
        
        if existing_user:
            # User exists - login them
            logger.info("OAuth login for existing user: %s", oauth_data.email)
            tokens = create_tokens_for_user(self.db, existing_user)
            user_response = build_user_response(self.db, existing_user)
            return {
                "user": user_response,
                "tokens": tokens,
                "is_new_user": False
            }
        else:
            # User doesn't exist - create new user
            logger.info("OAuth login creating new user: %s", oauth_data.email)
            new_user = self.create_user_from_oauth(oauth_data)
            tokens = create_tokens_for_user(self.db, new_user)
            user_response = build_user_response(self.db, new_user)
            return {
                "user": user_response,
                "tokens": tokens,
                "is_new_user": True
            }


# Convenience functions for direct use
def authenticate_user(db: Session, username_or_email: str, password: str) -> Optional[User]:
    """Authenticate user - convenience function"""
    service = UserService(db)
    return service.authenticate_user(username_or_email, password)


def create_user(db: Session, user_data: UserRegister) -> User:
    """Create user - convenience function"""
    service = UserService(db)
    return service.create_user(user_data)


def get_user_by_id(db: Session, user_id: uuid.UUID) -> Optional[User]:
    """Get user by ID - convenience function"""
    service = UserService(db)
    return service.get_user_by_id(user_id)


def authenticate_user_with_tokens(db: Session, email: str, password: str) -> Optional[dict]:
    """Authenticate user with tokens - convenience function"""
    service = UserService(db)
    return service.authenticate_user_with_tokens(email, password)


def create_user_with_tokens(db: Session, user_data: UserRegister) -> dict:
    """Create user with tokens - convenience function"""
    service = UserService(db)
    return service.create_user_with_tokens(user_data)


def refresh_user_tokens(db: Session, refresh_token: str) -> Optional[dict]:
    """Refresh user tokens - convenience function"""
    service = UserService(db)
    return service.refresh_user_tokens(refresh_token)


def request_password_reset(db: Session, email: str) -> bool:
    """Request password reset - convenience function"""
    service = UserService(db)
    return service.request_password_reset(email)


def reset_password(db: Session, token: str, new_password: str) -> bool:
    """Reset password - convenience function"""
    service = UserService(db)
    return service.reset_password(token, new_password)


def get_user_by_reset_token(db: Session, token: str) -> Optional[User]:
    """Get user by reset token - convenience function"""
    service = UserService(db)
    return service.get_user_by_reset_token(token)


def oauth_login(db: Session, oauth_data: OAuthData) -> dict:
    """OAuth login - convenience function"""
    service = UserService(db)
    return service.oauth_login(oauth_data)
