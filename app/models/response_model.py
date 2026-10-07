# app/models/response.py
from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Boolean, ForeignKey, Index, Text, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.db.base import Base
import uuid

# ---------- Element Interaction Table ----------
class ElementInteraction(Base):
    """Table for tracking element interactions and timing."""
    __tablename__ = "element_interactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    study_response_id = Column(UUID(as_uuid=True), ForeignKey('study_responses.id', ondelete='CASCADE'), nullable=False, index=True)
    task_session_id = Column(UUID(as_uuid=True), ForeignKey('task_sessions.id', ondelete='CASCADE'), nullable=True, index=True)
    
    element_id = Column(String(10), nullable=False)
    view_time_seconds = Column(Float, nullable=False, default=0.0)
    hover_count = Column(Integer, default=0)
    click_count = Column(Integer, default=0)
    first_view_time = Column(DateTime(timezone=True), nullable=True)
    last_view_time = Column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    study_response = relationship("StudyResponse", back_populates="element_interactions")
    task_session = relationship("TaskSession", back_populates="element_interactions")

    __table_args__ = (
        Index("idx_element_interaction_response", "study_response_id"),
        Index("idx_element_interaction_task", "task_session_id"),
        Index("idx_element_interaction_element", "element_id"),
        # Composite index for analytics performance
        Index("idx_element_interaction_analytics", "study_response_id", "element_id"),
    )

# ---------- Completed Task Table ----------
class CompletedTask(Base):
    """Table for individual completed task data."""
    __tablename__ = "completed_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    study_response_id = Column(UUID(as_uuid=True), ForeignKey('study_responses.id', ondelete='CASCADE'), nullable=False, index=True)
    
    task_id = Column(String(20), nullable=False)
    respondent_id = Column(Integer, nullable=False)
    task_index = Column(Integer, nullable=False)
    
    # Grid study data
    elements_shown_in_task = Column(JSONB, nullable=False)  # Copy from study.tasks
    
    # Layer study data
    elements_shown_content = Column(JSONB, nullable=True)  # Layer images with z-index and metadata
    
    # Task metadata
    task_type = Column(String(50), nullable=True)  # 'grid' or 'layer'
    task_context = Column(JSONB, nullable=True)  # Additional task context and metadata
    
    # Timing data
    task_start_time = Column(DateTime(timezone=True), nullable=False)
    task_completion_time = Column(DateTime(timezone=True), nullable=False)
    task_duration_seconds = Column(Float, nullable=False)
    
    # Rating data
    rating_given = Column(Integer, nullable=False)
    rating_timestamp = Column(DateTime(timezone=True), nullable=False)
    
    # Backward compatibility fields
    elements_shown = Column(JSONB, nullable=True)  # Legacy field for grid studies
    layers_shown_in_task = Column(JSONB, nullable=True)  # Legacy field for layer studies
    
    # Relationships
    study_response = relationship("StudyResponse", back_populates="completed_tasks")

    __table_args__ = (
        Index("idx_completed_task_response", "study_response_id"),
        Index("idx_completed_task_id", "task_id"),
        Index("idx_completed_task_respondent", "respondent_id"),
        # Composite index for analytics performance
        Index("idx_completed_task_analytics", "study_response_id", "task_duration_seconds"),
    )

# ---------- Classification Answer Table ----------
class ClassificationAnswer(Base):
    """Table for classification question answers."""
    __tablename__ = "classification_answers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    study_response_id = Column(UUID(as_uuid=True), ForeignKey('study_responses.id', ondelete='CASCADE'), nullable=False, index=True)
    
    question_id = Column(String(10), nullable=False)
    question_text = Column(String(500), nullable=False)
    answer = Column(String(1000), nullable=False)
    answer_timestamp = Column(DateTime(timezone=True), nullable=False)
    time_spent_seconds = Column(Float, default=0.0)
    
    # Relationships
    study_response = relationship("StudyResponse", back_populates="classification_answers")

    __table_args__ = (
        Index("idx_classification_response", "study_response_id"),
        Index("idx_classification_question", "question_id"),
    )

# ---------- Study Response Table ----------
class StudyResponse(Base):
    """Study response model for anonymous respondent submissions."""
    __tablename__ = "study_responses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    
    # Study and Respondent Identification
    study_id = Column(UUID(as_uuid=True), ForeignKey('studies.id', ondelete='CASCADE'), nullable=False, index=True)
    session_id = Column(String(50), nullable=False, unique=True, index=True)
    respondent_id = Column(Integer, nullable=False)
    
    # Progress Tracking
    current_task_index = Column(Integer, default=0)
    completed_tasks_count = Column(Integer, default=0)
    total_tasks_assigned = Column(Integer, nullable=False)
    
    # Session Management
    session_start_time = Column(DateTime(timezone=True), nullable=False)
    session_end_time = Column(DateTime(timezone=True), nullable=True)
    is_completed = Column(Boolean, default=False)
    # True once this respondent has been accepted against screening-option quotas.
    quota_reserved = Column(Boolean, nullable=False, default=False, server_default="false")
    status = Column(String(20), nullable=True, index=True)
    
    # Classification and Demographics
    personal_info = Column(JSONB, nullable=True)  # Age, gender, education, etc.
    
    # Analytics Data
    ip_address = Column(String(45), nullable=True)  # IPv6 support
    user_agent = Column(String(500), nullable=True)
    browser_info = Column(JSONB, nullable=True)
    
    # Progress and Timing
    completion_percentage = Column(Float, default=0.0)
    total_study_duration = Column(Float, default=0.0)
    last_activity = Column(DateTime(timezone=True), default=func.now())
    
    # Abandonment Detection
    is_abandoned = Column(Boolean, default=True)  # Default to abandoned, set to False when completed
    abandonment_timestamp = Column(DateTime(timezone=True), nullable=True)
    abandonment_reason = Column(String(200), nullable=True)
    
    # Client-specific features
    product_id = Column(String(100), nullable=True) # Optional hexadecimal product id
    done_by_id = Column(String(50), nullable=True, index=True)
    
    # Panelist Integration
    panelist_id = Column(String(50), nullable=True, index=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Relationships
    study = relationship("Study", back_populates="study_responses")
    completed_tasks = relationship("CompletedTask", back_populates="study_response", cascade="all, delete-orphan", order_by="CompletedTask.task_index")
    classification_answers = relationship("ClassificationAnswer", back_populates="study_response", cascade="all, delete-orphan")
    element_interactions = relationship("ElementInteraction", back_populates="study_response", cascade="all, delete-orphan")
    task_sessions = relationship("TaskSession", back_populates="study_response", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("study_id", "respondent_id", name="uq_study_responses_study_respondent"),
        Index("idx_study_response_study", "study_id"),
        Index("idx_study_response_session", "session_id"),
        Index("idx_study_response_respondent", "respondent_id"),
        Index("idx_study_response_start_time", "session_start_time"),
        Index("idx_study_response_completed", "is_completed"),
        Index("idx_study_response_abandoned", "is_abandoned"),
        Index("idx_study_response_activity", "last_activity"),
        # Composite indexes for analytics performance
        Index("idx_study_response_analytics", "study_id", "is_completed", "is_abandoned"),
        Index("idx_study_response_duration", "study_id", "total_study_duration"),
    )


class ScreeningOptionQuota(Base):
    """Atomic seat counter for one screening-question option."""
    __tablename__ = "screening_option_quotas"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_id = Column(UUID(as_uuid=True), ForeignKey("studies.id", ondelete="CASCADE"), nullable=False)
    question_id = Column(String(10), nullable=False)
    option_id = Column(String(10), nullable=False)
    max_respondents = Column(Integer, nullable=False)
    accepted_count = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("study_id", "question_id", "option_id", name="uq_screening_option_quota"),
        Index("idx_screening_option_quota_lookup", "study_id", "question_id", "option_id"),
    )


class FreedRespondentId(Base):
    """Respondent numbers returned after a response row is deleted.

    The primary key is (study_id, respondent_id), so claiming the smallest
    free number is a single index lookup rather than a scan of every response.
    """
    __tablename__ = "freed_respondent_ids"

    study_id = Column(UUID(as_uuid=True), ForeignKey("studies.id", ondelete="CASCADE"), primary_key=True)
    respondent_id = Column(Integer, primary_key=True)

# ---------- Task Session Table ----------
class TaskSession(Base):
    """Individual task timing tracking for detailed analytics."""
    __tablename__ = "task_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    
    # Session Identification
    session_id = Column(String(50), nullable=False, index=True)
    task_id = Column(String(20), nullable=False, index=True)
    study_response_id = Column(UUID(as_uuid=True), ForeignKey('study_responses.id', ondelete='CASCADE'), nullable=False, index=True)
    
    # Page-Level Timing
    classification_page_time = Column(Float, default=0.0)
    orientation_page_time = Column(Float, default=0.0)
    individual_task_page_times = Column(JSONB, nullable=True)  # List of FloatField()
    
    # Page Transitions
    page_transitions = Column(JSONB, nullable=True)  # List of DictField()
    
    # Task Abandonment Detection
    is_completed = Column(Boolean, default=False)
    abandonment_timestamp = Column(DateTime(timezone=True), nullable=True)
    abandonment_reason = Column(String(200), nullable=True)
    recovery_attempts = Column(Integer, default=0)
    
    
    # Performance Analytics
    browser_performance = Column(JSONB, nullable=True)
    page_load_times = Column(JSONB, nullable=True)  # List of FloatField()
    device_info = Column(JSONB, nullable=True)
    screen_resolution = Column(String(20), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Relationships
    study_response = relationship("StudyResponse", back_populates="task_sessions")
    element_interactions = relationship("ElementInteraction", back_populates="task_session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_task_session_session", "session_id"),
        Index("idx_task_session_task", "task_id"),
        Index("idx_task_session_response", "study_response_id"),
        Index("idx_task_session_created", "created_at"),
        Index("idx_task_session_completed", "is_completed"),
    )