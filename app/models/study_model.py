# app/models/study.py
from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Enum, ForeignKey, Index, UniqueConstraint, Text, Boolean
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func, expression
from sqlalchemy.orm import relationship
from app.db.base import Base
import uuid

study_type_enum = Enum('grid', 'layer', 'text', 'hybrid', name='study_type_enum')
study_status_enum = Enum('draft', 'active', 'paused', 'completed', name='study_status_enum')
element_type_enum = Enum('image', 'text', name='element_type_enum')
layer_type_enum = Enum('image', 'text', name='layer_type_enum')
study_role_enum = Enum('admin', 'editor', 'viewer', name='study_role_enum')

class Study(Base):
    __tablename__ = "studies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    # Basic info
    title = Column(String(255), nullable=False)
    background = Column(Text, nullable=False)
    language = Column(String(10), nullable=False, server_default='en')
    main_question = Column(Text, nullable=False)
    orientation_text = Column(Text, nullable=False)
    study_type = Column(study_type_enum, nullable=False)
    # Optional global background image URL for layer/grid rendering
    background_image_url = Column(Text, nullable=True)

    # JSON configs
    rating_scale = Column(JSONB, nullable=False)         # {min_value, max_value in {5,7,9}, labels...}
    audience_segmentation = Column('iped_parameters', JSONB, nullable=False)  # renamed logically; same column
    tasks = Column(JSONB, nullable=True)                 # generated task matrix
    phase_order = Column(JSONB, nullable=True)           # e.g., ['grid', 'text']

    # Ownership / meta
    creator_id = Column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id', ondelete='SET NULL'), nullable=True, index=True)
    status = Column(study_status_enum, nullable=False, server_default='draft')
    share_token = Column(String(255), nullable=False, unique=True, index=True)
    share_url = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    launched_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Counters
    total_responses = Column(Integer, nullable=False, server_default=expression.text('0'))
    completed_responses = Column(Integer, nullable=False, server_default=expression.text('0'))
    abandoned_responses = Column(Integer, nullable=False, server_default=expression.text('0'))

    # Study creation progress tracking
    last_step = Column(Integer, nullable=False, server_default=expression.text('1'), default=1)

    # Background job tracking
    jobid = Column(String(255), nullable=True, index=True)

    # Shuffling config
    toggle_shuffle = Column(Boolean, nullable=False, server_default=expression.false(), default=False)

    # Optional product keys (name + percentage per key), e.g. [{"name": "Gourmand", "percentage": 50}, ...]
    product_keys = Column(JSONB, nullable=True)
    # Optional product ID (e.g. for station/product linking)
    product_id = Column(String(100), nullable=True)

    # Relations
    # Use lazy="noload" for heavy relationships to prevent accidental loading
    # Load explicitly with selectinload/joinedload when needed
    creator = relationship("User", back_populates="studies", lazy="noload", passive_deletes=True)
    project = relationship("Project", back_populates="studies", lazy="noload", passive_deletes=True)
    # Grid categories for grouping elements - keep selectin for detail views
    categories = relationship("StudyCategory", back_populates="study", cascade="all, delete-orphan", lazy="selectin")
    elements = relationship("StudyElement", back_populates="study", cascade="all, delete-orphan", lazy="selectin")
    layers = relationship("StudyLayer", back_populates="study", cascade="all, delete-orphan", lazy="selectin")
    classification_questions = relationship("StudyClassificationQuestion", back_populates="study", cascade="all, delete-orphan", lazy="selectin")
    # CRITICAL: study_responses can be huge - never auto-load
    study_responses = relationship("StudyResponse", back_populates="study", cascade="all, delete-orphan", lazy="noload")
    members = relationship("StudyMember", back_populates="study", cascade="all, delete-orphan", lazy="selectin")
    filter_history = relationship("StudyFilterHistory", back_populates="study", cascade="all, delete-orphan", lazy="noload")
    saved_designs = relationship("StudySavedDesign", back_populates="study", cascade="all, delete-orphan", lazy="noload")
    task_assignments = relationship("StudyTaskAssignment", back_populates="study", cascade="all, delete-orphan", lazy="noload")

    __table_args__ = (
        UniqueConstraint('share_token', name='uq_studies_share_token'),
        Index('idx_studies_creator_status_created_at', 'creator_id', 'status', 'created_at'),
        Index('idx_studies_status_created_at', 'status', 'created_at'),
        Index('idx_studies_study_type', 'study_type'),
    )

class StudyCategory(Base):
    """
    Grid categories for grouping elements, e.g., 'a', 'b', 'c' with order.
    """
    __tablename__ = "study_categories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_id = Column(UUID(as_uuid=True), ForeignKey('studies.id', ondelete='CASCADE'), nullable=False, index=True)

    category_id = Column(UUID(as_uuid=True), nullable=False)  # UUID instead of string
    name = Column(String(100), nullable=False)
    order = Column(Integer, nullable=False, default=0)
    phase_type = Column(study_type_enum, nullable=True)  # Associated phase ('grid' or 'text')

    study = relationship("Study", back_populates="categories", lazy="selectin")
    elements = relationship("StudyElement", back_populates="category", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('study_id', 'category_id', name='uq_study_categories_category_id'),
        Index('idx_study_categories_study_id_category_id', 'study_id', 'category_id'),
        Index('idx_study_categories_study_id_order', 'study_id', 'order'),
    )

class StudyElement(Base):
    """
    Grid elements now belong to a category.
    """
    __tablename__ = "study_elements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_id = Column(UUID(as_uuid=True), ForeignKey('studies.id', ondelete='CASCADE'), nullable=False, index=True)

    # Category foreign key
    category_id = Column(UUID(as_uuid=True), ForeignKey('study_categories.id', ondelete='CASCADE'), nullable=False, index=True)

    element_id = Column(UUID(as_uuid=True), nullable=False)  # UUID for element
    name = Column(String(1000), nullable=False)
    description = Column(Text, nullable=True)
    element_type = Column(element_type_enum, nullable=False)  # image|text
    content = Column(Text, nullable=False)                    # URL or text content
    cloudinary_public_id = Column(Text, nullable=True)
    alt_text = Column(String(200), nullable=True)

    study = relationship("Study", back_populates="elements", lazy="selectin")
    category = relationship("StudyCategory", back_populates="elements", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('study_id', 'element_id', name='uq_study_elements_element_id'),
        Index('idx_study_elements_study_id_element_id', 'study_id', 'element_id'),
        Index('idx_study_elements_category_id', 'category_id'),
    )

class StudyLayer(Base):
    """
    For layer studies: each layer (category) containing images.
    Use layer_id for parity with legacy (string id).
    """
    __tablename__ = "study_layers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_id = Column(UUID(as_uuid=True), ForeignKey('studies.id', ondelete='CASCADE'), nullable=False, index=True)

    layer_id = Column(String(100), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    layer_type = Column(layer_type_enum, nullable=False, server_default='image')
    z_index = Column(Integer, nullable=False)
    order = Column(Integer, nullable=False)
    # Layer-level transform percentages {x,y,width,height}
    transform = Column(JSONB, nullable=True)

    study = relationship("Study", back_populates="layers", lazy="selectin")
    images = relationship("LayerImage", back_populates="layer", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('study_id', 'layer_id', name='uq_study_layers_layer_id'),
        Index('idx_study_layers_study_id_layer_id', 'study_id', 'layer_id'),
    )

class LayerImage(Base):
    __tablename__ = "layer_images"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    layer_id = Column(UUID(as_uuid=True), ForeignKey('study_layers.id', ondelete='CASCADE'), nullable=False, index=True)

    image_id = Column(String(100), nullable=False)  # string id; keep parity with legacy
    name = Column(String(100), nullable=False)
    url = Column(Text, nullable=False)
    cloudinary_public_id = Column(Text, nullable=True)
    alt_text = Column(String(200), nullable=True)
    order = Column(Integer, nullable=False)
    # Config for text layers (font, color, etc.) or other layer types
    config = Column(JSONB, nullable=True)

    layer = relationship("StudyLayer", back_populates="images", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('layer_id', 'image_id', name='uq_layer_images_image_id'),
        Index('idx_layer_images_layer_id_image_id', 'layer_id', 'image_id'),
    )

class StudyClassificationQuestion(Base):
    """Classification questions that belong to a study."""
    __tablename__ = "study_classification_questions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_id = Column(UUID(as_uuid=True), ForeignKey('studies.id', ondelete='CASCADE'), nullable=False, index=True)

    question_id = Column(String(10), nullable=False)  # Q1, Q2, ...
    question_text = Column(Text, nullable=False)
    question_type = Column(String(20), nullable=False, default='multiple_choice')  # multiple_choice, text, rating, etc.
    is_required = Column(String(1), nullable=False, default='Y')  # Y/N
    order = Column(Integer, nullable=False, default=1)
    
    # Answer options (for multiple choice questions)
    answer_options = Column(JSONB, nullable=True)  # [{"id": "A", "text": "Option 1"}, ...]
    
    # Additional configuration
    config = Column(JSONB, nullable=True)  # Additional question-specific config

    study = relationship("Study", back_populates="classification_questions", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('study_id', 'question_id', name='uq_study_classification_questions_question_id'),
        Index('idx_study_classification_questions_study_id_order', 'study_id', 'order'),
    )

class StudyMember(Base):
    """
    Join table for study sharing with roles (Google Docs style).
    """
    __tablename__ = "study_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_id = Column(UUID(as_uuid=True), ForeignKey('studies.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=True, index=True)
    
    # role inherited from admin/editor/viewer
    role = Column(study_role_enum, nullable=False, server_default='viewer')
    
    # invited_email handles the case where the user doesn't have an account yet
    invited_email = Column(String(255), nullable=False, index=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relations
    study = relationship("Study", back_populates="members", lazy="selectin")
    user = relationship("User", backref="shared_studies", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('study_id', 'invited_email', name='uq_study_members_study_email'),
        Index('idx_study_members_study_id_role', 'study_id', 'role'),
        Index('idx_study_members_user_id', 'user_id'),
    )


class StudyTaskAssignment(Base):
    """
    Normalized storage for task assignments per respondent.
    Replaces the study.tasks JSONB blob to avoid PostgreSQL size limits.
    Each row represents one task for one respondent.
    """
    __tablename__ = "study_task_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_id = Column(UUID(as_uuid=True), ForeignKey('studies.id', ondelete='CASCADE'), nullable=False, index=True)
    
    respondent_id = Column(Integer, nullable=False)
    task_index = Column(Integer, nullable=False)
    task_id = Column(String(50), nullable=False)
    
    elements_shown = Column(JSONB, nullable=False)
    elements_shown_content = Column(JSONB, nullable=True)
    phase_type = Column(String(20), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    study = relationship("Study", back_populates="task_assignments", lazy="noload")

    __table_args__ = (
        UniqueConstraint('study_id', 'respondent_id', 'task_index', name='uq_study_task_assignment'),
        Index('idx_sta_study_respondent', 'study_id', 'respondent_id'),
        Index('idx_sta_study', 'study_id'),
    )


class StudyFilterHistory(Base):
    """
    Stores filter selections (history) for study analysis.
    Used so users can save and reload filter combinations.
    """
    __tablename__ = "study_filter_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    study_id = Column(UUID(as_uuid=True), ForeignKey('studies.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    # Stored filter payload: { "age_groups": [], "genders": [], "classification_filters": {} }
    filters = Column(JSONB, nullable=False, server_default='{}')
    name = Column(String(255), nullable=True, index=True)  # Optional label e.g. "Male 18-24 No"

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    study = relationship("Study", back_populates="filter_history", lazy="noload")
    user = relationship("User", back_populates="filter_history", lazy="noload")

    __table_args__ = (
        Index('idx_study_filter_history_study_user', 'study_id', 'user_id'),
        Index('idx_study_filter_history_created', 'study_id', 'created_at'),
    )


class StudySavedDesign(Base):
    """
    Shared saved design configurations for the analytics design configurator.
    Stores a denormalized JSONB snapshot so compare reads stay fast.
    """
    __tablename__ = "study_saved_designs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    study_id = Column(UUID(as_uuid=True), ForeignKey('studies.id', ondelete='CASCADE'), nullable=False, index=True)
    created_by_id = Column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)

    name = Column(String(255), nullable=False)
    normalized_name = Column(String(255), nullable=False)
    design_type = Column(String(30), nullable=False, server_default='configurator')
    study_type = Column(study_type_enum, nullable=False)
    metric = Column(String(50), nullable=False)
    segment_label = Column(String(255), nullable=True)
    selection_count = Column(Integer, nullable=False, server_default=expression.text('0'))
    total_coefficient = Column(Float, nullable=True)
    configuration = Column(JSONB, nullable=False, server_default='{}')

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    study = relationship("Study", back_populates="saved_designs", lazy="noload")
    created_by = relationship("User", back_populates="saved_designs", lazy="noload")

    __table_args__ = (
        UniqueConstraint('study_id', 'design_type', 'normalized_name', name='uq_study_saved_designs_study_type_name'),
        Index('idx_study_saved_designs_study_created', 'study_id', 'created_at'),
        Index('idx_study_saved_designs_study_type_created', 'study_id', 'design_type', 'created_at'),
        Index('idx_study_saved_designs_study_updated', 'study_id', 'updated_at'),
    )