
from sqlalchemy import Column, String, Float, DateTime, Enum, Text, UniqueConstraint, Boolean, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from app.db.base import Base
import uuid
import enum

class JobStatus(str, enum.Enum):
    PENDING = "pending"
    STARTED = "started"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class Job(Base):
    __tablename__ = "jobs"

    job_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    study_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    
    status = Column(Enum(JobStatus, name='job_status_enum', native_enum=False, create_constraint=False), default=JobStatus.PENDING, nullable=False)
    progress = Column(Float, default=0.0)
    message = Column(Text, default="")
    error = Column(Text, nullable=True)
    result = Column(JSONB, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class DismissedJobNotification(Base):
    """Tracks job notifications dismissed by a user (hidden from notification UI)."""

    __tablename__ = "dismissed_job_notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_dismissed_job_notifications_user_job"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(36), nullable=False, index=True)
    job_id = Column(String(36), nullable=False, index=True)
    dismissed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UserJobNotification(Base):
    """Persistent per-user job notification (synced across devices)."""

    __tablename__ = "user_job_notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_user_job_notifications_user_job"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(36), nullable=False, index=True)
    job_id = Column(String(36), nullable=False, index=True)
    study_id = Column(String(36), nullable=False, index=True)
    study_title = Column(String(500), nullable=True)
    job_kind = Column(String(32), nullable=False, default="task_generation")
    status = Column(String(32), nullable=False, default="pending")
    progress = Column(Float, default=0.0, nullable=False)
    message = Column(Text, default="")
    error = Column(Text, nullable=True)
    respondents_requested = Column(Integer, nullable=True)
    respondents_completed = Column(Integer, nullable=True)
    is_read = Column(Boolean, default=False, nullable=False)
    dismissed_at = Column(DateTime(timezone=True), nullable=True)
    job_created_at = Column(DateTime(timezone=True), nullable=True)
    job_completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class UserInvitationNotification(Base):
    """Persistent study/project invitation notification for in-app bell."""

    __tablename__ = "user_invitation_notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(36), nullable=False, index=True)
    notification_kind = Column(String(32), nullable=False)  # study_invite | project_invite
    study_id = Column(String(36), nullable=True, index=True)
    project_id = Column(String(36), nullable=True, index=True)
    resource_title = Column(String(500), nullable=True)
    inviter_name = Column(String(255), nullable=True)
    role = Column(String(32), nullable=False)
    member_id = Column(String(36), nullable=True, index=True)
    is_read = Column(Boolean, default=False, nullable=False)
    dismissed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
