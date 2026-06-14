from sqlalchemy import Column, String, Boolean, DateTime, Index, UniqueConstraint, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    # Primary key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    # User identification fields
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)

    # Authentication
    password_hash = Column(String(255), nullable=False)

    # Contact information
    phone = Column(String(20), nullable=True, index=True)

    # Personal information
    date_of_birth = Column(DateTime, nullable=True)

    # Status fields
    is_active = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    dashboard_onboarding_completed = Column(Boolean, default=False, nullable=False)
    dashboard_onboarding_skipped = Column(Boolean, default=False, nullable=False)
    create_study_onboarding_completed = Column(Boolean, default=False, nullable=False)
    create_study_onboarding_skipped = Column(Boolean, default=False, nullable=False)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_login = Column(DateTime(timezone=True), nullable=True)
    
    # Password reset fields
    password_reset_token = Column(String(255), nullable=True, index=True)
    password_reset_expires = Column(DateTime(timezone=True), nullable=True)

    # Relations
    # Use lazy="noload" to prevent auto-loading all studies/projects on every user query
    # This is critical for performance - users with many studies would load megabytes of data otherwise
    studies = relationship(
        "Study",
        back_populates="creator",
        lazy="noload",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    projects = relationship(
        "Project",
        back_populates="creator",
        lazy="noload",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    filter_history = relationship(
        "StudyFilterHistory",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    saved_designs = relationship(
        "StudySavedDesign",
        back_populates="created_by",
        lazy="noload",
        passive_deletes=True,
    )

    # Indexes for better query performance
    __table_args__ = (
        Index('idx_user_email_active', 'email', 'is_active'),
        Index('idx_user_created_at', 'created_at'),
        Index('idx_user_last_login', 'last_login'),
        UniqueConstraint('email', name='uq_user_email'),
        CheckConstraint('length(email) >= 5', name='ck_user_email_length'),
        CheckConstraint('length(name) >= 2', name='ck_user_name_length'),
    )

    def __repr__(self):
        return f"<User(id={self.id}, email='{self.email}')>"

