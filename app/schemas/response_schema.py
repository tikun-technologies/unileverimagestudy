# app/schemas/response.py
from __future__ import annotations

from typing import List, Optional, Dict, Any, Literal
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict, model_validator

# ---------- Element Interaction Schemas ----------

class ElementInteractionBase(BaseModel):
    element_id: str = Field(..., max_length=10)
    view_time_seconds: float = Field(..., ge=0.0)
    hover_count: int = Field(default=0, ge=0)
    click_count: int = Field(default=0, ge=0)
    first_view_time: Optional[datetime] = None
    last_view_time: Optional[datetime] = None

class ElementInteractionCreate(ElementInteractionBase):
    pass

class ElementInteractionUpdate(BaseModel):
    element_id: Optional[str] = Field(None, max_length=10)
    view_time_seconds: Optional[float] = Field(None, ge=0.0)
    hover_count: Optional[int] = Field(None, ge=0)
    click_count: Optional[int] = Field(None, ge=0)
    first_view_time: Optional[datetime] = None
    last_view_time: Optional[datetime] = None

class ElementInteractionOut(ElementInteractionBase):
    id: UUID
    study_response_id: UUID
    task_session_id: Optional[UUID] = None
    
    model_config = ConfigDict(from_attributes=True)

# ---------- Completed Task Schemas ----------

class CompletedTaskBase(BaseModel):
    task_id: str = Field(..., max_length=20)
    respondent_id: int = Field(..., ge=0)
    task_index: int = Field(..., ge=0)
    elements_shown_in_task: Dict[str, Any]
    elements_shown_content: Optional[Dict[str, Any]] = None
    task_type: Optional[str] = Field(None, max_length=50)
    task_context: Optional[Dict[str, Any]] = None
    task_start_time: datetime
    task_completion_time: datetime
    task_duration_seconds: float = Field(..., ge=0.0)
    rating_given: int = Field(..., ge=1, le=9)
    rating_timestamp: datetime
    elements_shown: Optional[Dict[str, Any]] = None
    layers_shown_in_task: Optional[Dict[str, Any]] = None

class CompletedTaskCreate(CompletedTaskBase):
    pass

class CompletedTaskUpdate(BaseModel):
    task_id: Optional[str] = Field(None, max_length=20)
    respondent_id: Optional[int] = Field(None, ge=0)
    task_index: Optional[int] = Field(None, ge=0)
    elements_shown_in_task: Optional[Dict[str, Any]] = None
    elements_shown_content: Optional[Dict[str, Any]] = None
    task_type: Optional[str] = Field(None, max_length=50)
    task_context: Optional[Dict[str, Any]] = None
    task_start_time: Optional[datetime] = None
    task_completion_time: Optional[datetime] = None
    task_duration_seconds: Optional[float] = Field(None, ge=0.0)
    rating_given: Optional[int] = Field(None, ge=1, le=9)
    rating_timestamp: Optional[datetime] = None
    elements_shown: Optional[Dict[str, Any]] = None
    layers_shown_in_task: Optional[Dict[str, Any]] = None

class CompletedTaskOut(CompletedTaskBase):
    id: UUID
    study_response_id: UUID
    
    model_config = ConfigDict(from_attributes=True)

# ---------- Classification Answer Schemas ----------

class ClassificationAnswerBase(BaseModel):
    question_id: str = Field(..., max_length=10)
    question_text: str = Field(..., max_length=500)
    answer: str = Field(..., max_length=1000)
    answer_timestamp: datetime
    time_spent_seconds: float = Field(default=0.0, ge=0.0)

class ClassificationAnswerCreate(ClassificationAnswerBase):
    pass

class ClassificationAnswerUpdate(BaseModel):
    question_id: Optional[str] = Field(None, max_length=10)
    question_text: Optional[str] = Field(None, max_length=500)
    answer: Optional[str] = Field(None, max_length=1000)
    answer_timestamp: Optional[datetime] = None
    time_spent_seconds: Optional[float] = Field(None, ge=0.0)

class ClassificationAnswerOut(ClassificationAnswerBase):
    id: UUID
    study_response_id: UUID
    
    model_config = ConfigDict(from_attributes=True)

# ---------- Task Session Schemas ----------

class TaskSessionBase(BaseModel):
    session_id: str = Field(..., max_length=50)
    task_id: str = Field(..., max_length=20)
    classification_page_time: float = Field(default=0.0, ge=0.0)
    orientation_page_time: float = Field(default=0.0, ge=0.0)
    individual_task_page_times: Optional[List[float]] = None
    page_transitions: Optional[List[Dict[str, Any]]] = None
    is_completed: bool = Field(default=False)
    abandonment_timestamp: Optional[datetime] = None
    abandonment_reason: Optional[str] = Field(None, max_length=200)
    recovery_attempts: int = Field(default=0, ge=0)
    browser_performance: Optional[Dict[str, Any]] = None
    page_load_times: Optional[List[float]] = None
    device_info: Optional[Dict[str, Any]] = None
    screen_resolution: Optional[str] = Field(None, max_length=20)

class TaskSessionCreate(TaskSessionBase):
    pass

class TaskSessionUpdate(BaseModel):
    session_id: Optional[str] = Field(None, max_length=50)
    task_id: Optional[str] = Field(None, max_length=20)
    classification_page_time: Optional[float] = Field(None, ge=0.0)
    orientation_page_time: Optional[float] = Field(None, ge=0.0)
    individual_task_page_times: Optional[List[float]] = None
    page_transitions: Optional[List[Dict[str, Any]]] = None
    is_completed: Optional[bool] = None
    abandonment_timestamp: Optional[datetime] = None
    abandonment_reason: Optional[str] = Field(None, max_length=200)
    recovery_attempts: Optional[int] = Field(None, ge=0)
    browser_performance: Optional[Dict[str, Any]] = None
    page_load_times: Optional[List[float]] = None
    device_info: Optional[Dict[str, Any]] = None
    screen_resolution: Optional[str] = Field(None, max_length=20)

class TaskSessionOut(TaskSessionBase):
    id: UUID
    study_response_id: UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

# ---------- Study Response Schemas ----------

class StudyResponseBase(BaseModel):
    session_id: str = Field(..., max_length=50)
    respondent_id: int = Field(..., ge=0)
    current_task_index: int = Field(default=0, ge=0)
    completed_tasks_count: int = Field(default=0, ge=0)
    total_tasks_assigned: int = Field(..., ge=1)
    session_start_time: datetime
    session_end_time: Optional[datetime] = None
    is_completed: bool = Field(default=False)
    personal_info: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = Field(None, max_length=45)
    user_agent: Optional[str] = Field(None, max_length=500)
    browser_info: Optional[Dict[str, Any]] = None
    completion_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    total_study_duration: float = Field(default=0.0, ge=0.0)
    last_activity: Optional[datetime] = None
    is_abandoned: bool = Field(default=True)
    abandonment_timestamp: Optional[datetime] = None
    abandonment_reason: Optional[str] = Field(None, max_length=200)

    product_id: Optional[str] = Field(None, max_length=100, pattern="^[a-zA-Z0-9]*$")
    panelist_id: Optional[str] = Field(None, max_length=50)
    done_by_id: Optional[str] = Field(None, max_length=50)

class StudyResponseCreate(BaseModel):
    # Optional during creation; generated by server
    session_id: Optional[str] = None
    respondent_id: Optional[int] = None
    study_id: UUID
    current_task_index: int = Field(default=0, ge=0)
    completed_tasks_count: int = Field(default=0, ge=0)
    total_tasks_assigned: int = Field(..., ge=1)
    session_start_time: datetime
    session_end_time: Optional[datetime] = None
    is_completed: bool = Field(default=False)
    personal_info: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = Field(None, max_length=45)
    user_agent: Optional[str] = Field(None, max_length=500)
    browser_info: Optional[Dict[str, Any]] = None
    completion_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    total_study_duration: float = Field(default=0.0, ge=0.0)
    last_activity: Optional[datetime] = None
    is_abandoned: bool = Field(default=True)
    abandonment_timestamp: Optional[datetime] = None
    abandonment_reason: Optional[str] = Field(None, max_length=200)

    product_id: Optional[str] = Field(None, max_length=100, pattern="^[a-zA-Z0-9]*$")
    panelist_id: Optional[str] = Field(None, max_length=50)
    done_by_id: Optional[str] = Field(None, max_length=50)

class StudyResponseUpdate(BaseModel):
    session_id: Optional[str] = Field(None, max_length=50)
    respondent_id: Optional[int] = Field(None, ge=0)
    current_task_index: Optional[int] = Field(None, ge=0)
    completed_tasks_count: Optional[int] = Field(None, ge=0)
    total_tasks_assigned: Optional[int] = Field(None, ge=1)
    session_start_time: Optional[datetime] = None
    session_end_time: Optional[datetime] = None
    is_completed: Optional[bool] = None
    personal_info: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = Field(None, max_length=45)
    user_agent: Optional[str] = Field(None, max_length=500)
    browser_info: Optional[Dict[str, Any]] = None
    completion_percentage: Optional[float] = Field(None, ge=0.0, le=100.0)
    total_study_duration: Optional[float] = Field(None, ge=0.0)
    last_activity: Optional[datetime] = None
    is_abandoned: Optional[bool] = None
    abandonment_timestamp: Optional[datetime] = None
    abandonment_reason: Optional[str] = Field(None, max_length=200)

    product_id: Optional[str] = Field(None, max_length=100, pattern="^[a-zA-Z0-9]*$")
    panelist_id: Optional[str] = Field(None, max_length=50)
    done_by_id: Optional[str] = Field(None, max_length=50)

class StudyResponseOut(StudyResponseBase):
    id: UUID
    study_id: UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

# ---------- Detailed Response Schemas (with relationships) ----------

class StudyResponseDetail(StudyResponseOut):
    """Study response with all related data"""
    completed_tasks: List[CompletedTaskOut] = []
    classification_answers: List[ClassificationAnswerOut] = []
    element_interactions: List[ElementInteractionOut] = []
    task_sessions: List[TaskSessionOut] = []
    study_layers: Optional[List[Dict[str, Any]]] = None
    background_image_url: Optional[str] = None

class StudyResponseListItem(BaseModel):
    """Lightweight response for listing"""
    id: UUID
    session_id: str
    respondent_id: int
    personal_info: Optional[Dict[str, Any]] = None
    is_completed: bool
    is_abandoned: bool
    completion_percentage: Optional[float] = None
    total_study_duration: Optional[float] = None
    session_start_time: datetime
    session_end_time: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)

# ---------- User Details Schemas ----------

class UserDetails(BaseModel):
    """Structured user details for study participation"""
    date_of_birth: Optional[str] = Field(None, description="Date of birth in YYYY-MM-DD format")
    gender: Optional[str] = Field(None, description="Gender (male, female, other, prefer_not_to_say)")
    age_range: Optional[str] = Field(None, description="Age range (18-24, 25-34, 35-44, 45-54, 55-64, 65+)")
    education_level: Optional[str] = Field(None, description="Education level")
    occupation: Optional[str] = Field(None, description="Occupation or job title")
    location: Optional[str] = Field(None, description="Country or region")
    additional_info: Optional[Dict[str, Any]] = Field(None, description="Any additional custom fields")

# ---------- Request/Response Schemas for API ----------

class StartStudyRequest(BaseModel):
    """Request to start a new study session"""
    study_id: UUID
    personal_info: Optional[Dict[str, Any]] = None
    user_details: Optional[UserDetails] = None

    product_id: Optional[str] = Field(None, max_length=100, pattern="^[a-zA-Z0-9]*$")
    panelist_id: Optional[str] = Field(None, max_length=50)
    done_by_id: Optional[str] = Field(None, max_length=50)

class StartStudyResponse(BaseModel):
    """Response when starting a study"""
    session_id: str
    respondent_id: int
    total_tasks_assigned: int
    study_info: Dict[str, Any]  # Basic study details
    done_by_id: Optional[str] = None

class SubmitTaskRequest(BaseModel):
    """Request to submit a completed task"""
    task_id: str
    rating_given: int = Field(..., ge=1, le=9)
    task_duration_seconds: float = Field(..., ge=0.0)
    element_interactions: Optional[List[ElementInteractionCreate]] = None

class SubmitTaskResponse(BaseModel):
    """Response after submitting a task"""
    success: bool
    next_task_index: Optional[int] = None
    is_study_complete: bool = False
    completion_percentage: float

class BulkSubmitTaskItem(BaseModel):
    """Single task item for bulk submission"""
    task_id: str
    rating_given: int = Field(..., ge=1, le=9)
    task_duration_seconds: float = Field(..., ge=0.0)
    elements_shown_in_task: Optional[Dict[str, Any]] = None
    elements_shown_content: Optional[Dict[str, Any]] = None
    element_interactions: Optional[List[ElementInteractionCreate]] = None

class BulkSubmitTasksRequest(BaseModel):
    """Bulk submit multiple completed tasks in order"""
    tasks: List[BulkSubmitTaskItem]

class BulkSubmitTasksResponse(BaseModel):
    """Response after bulk task submission"""
    success: bool
    submitted_count: int
    next_task_index: Optional[int] = None
    is_study_complete: bool = False
    completion_percentage: float

class SubmitClassificationRequest(BaseModel):
    """Request to submit classification answers"""
    answers: List[ClassificationAnswerCreate]
    finalize_response: bool = False

class SubmitClassificationResponse(BaseModel):
    """Response after submitting classification"""
    success: bool
    message: str

class AbandonStudyRequest(BaseModel):
    """Request to mark study as abandoned"""
    reason: str = Field(..., max_length=200)

class AbandonStudyResponse(BaseModel):
    """Response after abandoning study"""
    success: bool
    message: str

class UpdateUserDetailsRequest(BaseModel):
    """Request to update user details for a study session"""
    user_details: UserDetails

class SubmitProductIdRequest(BaseModel):
    """Request to submit a product ID during participation"""
    product_id: str = Field(..., max_length=100, pattern="^[a-zA-Z0-9]*$")

class SubmitProductIdResponse(BaseModel):
    """Response after submitting product ID"""
    success: bool
    message: str

class SubmitPanelistRequest(BaseModel):
    """Request to submit a panelist ID"""
    panelist_id: str = Field(..., max_length=50)

class SubmitPanelistResponse(BaseModel):
    """Response after submitting panelist ID"""
    success: bool
    panelist_age: Optional[int]
    panelist_gender: Optional[str]
    message: str


class CheckPanelistParticipationResponse(BaseModel):
    """Response for panelist participation check (study + panelist)."""
    ok: bool = True
    participated: bool = False
    message: Optional[str] = None


# ---------- Synthetic Respondent (accepts client JSON; stored like tasks + classification) ----------

class SyntheticClassificationAnswerItem(BaseModel):
    """Single classification answer in synthetic respondent payload."""
    question_id: str
    question_text: str
    answer: str
    answer_index: Optional[int] = None

class SyntheticElementShownItem(BaseModel):
    """Element shown in a task (synthetic payload)."""
    key: Optional[str] = None
    element_id: Optional[str] = None
    name: Optional[str] = None
    content: Optional[str] = None
    category_name: Optional[str] = None
    element_type: Optional[str] = None

class SyntheticTaskRatingItem(BaseModel):
    """Single task rating in synthetic respondent payload."""
    task_id: str
    task_index: Optional[int] = None
    main_question: Optional[str] = None
    vignette_content: Optional[str] = None
    rating: Optional[int] = Field(None, ge=1, le=9)
    reasoning: Optional[str] = None
    elements_shown: Optional[List[SyntheticElementShownItem]] = None
    method: Optional[str] = None

class SyntheticRespondentPayload(BaseModel):
    """Synthetic respondent: classification_answers + task_ratings (same shape as real). Optional personal_info for Gender/Age in analysis."""
    panelist_id: str = Field(..., max_length=50)
    panelist_number: int = Field(..., ge=0)
    classification_answers: Dict[str, SyntheticClassificationAnswerItem] = Field(default_factory=dict)
    task_ratings: List[SyntheticTaskRatingItem] = Field(default_factory=list)
    personal_info: Optional[Dict[str, Any]] = Field(None, description="Demographics (gender, age, age_range) stored like human flow for analysis")

class RatingScaleItem(BaseModel):
    """Optional rating scale (e.g. from ready_for_selenium). Not stored in DB."""
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    min_label: Optional[str] = None
    max_label: Optional[str] = None
    middle_label: Optional[str] = None

class ReadyForSeleniumPayload(BaseModel):
    """Payload under 'ready_for_selenium': study_id, classification_answers, task_ratings; optional rating_scale and panelist ids."""
    study_id: UUID
    classification_answers: Dict[str, SyntheticClassificationAnswerItem] = Field(default_factory=dict)
    task_ratings: List[SyntheticTaskRatingItem] = Field(default_factory=list)
    rating_scale: Optional[RatingScaleItem] = None
    panelist_id: Optional[str] = Field(None, max_length=50)
    panelist_number: Optional[int] = Field(None, ge=0)

class SubmitSyntheticRespondentRequest(BaseModel):
    """Request to store one synthetic respondent. Use either (study_id + payload) or ready_for_selenium."""
    study_id: Optional[UUID] = None
    payload: Optional[SyntheticRespondentPayload] = None
    ready_for_selenium: Optional[ReadyForSeleniumPayload] = None

    @model_validator(mode="after")
    def require_one_format(self):
        if self.ready_for_selenium is not None:
            if self.study_id is not None or self.payload is not None:
                raise ValueError("Use either ready_for_selenium or (study_id + payload), not both.")
            return self
        if self.study_id is None or self.payload is None:
            raise ValueError("Provide either ready_for_selenium or both study_id and payload.")
        return self

class SubmitSyntheticRespondentResponse(BaseModel):
    """Response after storing synthetic respondent."""
    success: bool
    session_id: str
    message: str

# Rebuild models to resolve forward references ----------

class StudyAnalytics(BaseModel):
    """Analytics data for a study"""
    total_responses: int
    completed_responses: int
    abandoned_responses: int
    in_progress_responses: int
    completion_rate: float
    average_duration: float
    abandonment_rate: float
    element_heatmap: Dict[str, Any]
    timing_distributions: Dict[str, Any]

class ResponseAnalytics(BaseModel):
    """Analytics for a specific response"""
    response_id: UUID
    session_id: str
    completion_percentage: float
    total_duration: float
    task_analytics: List[Dict[str, Any]]
    element_interactions: List[ElementInteractionOut]
    abandonment_analysis: Optional[Dict[str, Any]] = None


# ---------- Study Filter (Analysis) Schemas ----------

class StudyFilterCriteria(BaseModel):
    """Filter criteria for study regression report. All criteria are ANDed."""
    age_groups: Optional[List[str]] = Field(
        None,
        description="Age bins e.g. ['18-24', '25-34']. Values: 13-17, 18-24, 25-34, 35-44, 45-54, 55-64, 65+",
    )
    genders: Optional[List[str]] = Field(
        None,
        description="Normalized genders e.g. ['Male', 'Female']",
    )
    classification_filters: Optional[Dict[str, List[str]]] = Field(
        None,
        description="Map question_text to list of allowed answers e.g. {'What is your region?': ['North', 'South']}",
    )


class ActiveFilterPayload(BaseModel):
    """Request body for POST /study/{study_id}/active-filter."""
    filters: Optional[StudyFilterCriteria] = Field(
        None,
        description="Filter criteria to persist as the user's active analytics filter.",
    )


class ActiveFilterResponse(BaseModel):
    """Saved active filter for a user on a study."""
    study_id: UUID
    filters: Optional[StudyFilterCriteria] = None
    has_active_filter: bool = False
    updated_at: Optional[str] = None


class AnalyticsSessionResponse(BaseModel):
    """Bootstrap payload for the analytics page (active filter + cached analysis)."""
    study_id: UUID
    active_filters: Optional[StudyFilterCriteria] = None
    has_active_filter: bool = False
    analysis: Dict[str, Any] = Field(default_factory=dict)


class OptimizedAnalysisPayload(BaseModel):
    """Request body for POST /study/{study_id}/optimized-analysis-json."""
    filters: Optional[StudyFilterCriteria] = Field(
        None,
        description="Optional filter criteria. When set, the full optimized analysis JSON is computed on the filtered cohort only.",
    )
    save_to_history: bool = Field(
        default=True,
        description="If true and filters are applied, save this filter selection to the user's filter history.",
    )
    name: Optional[str] = Field(
        None,
        max_length=255,
        description="Optional label when saving filter history.",
    )


class ClassificationCohortPayload(BaseModel):
    """Request body for classification cohort drill-down."""
    filters: Optional[StudyFilterCriteria] = Field(
        None,
        description="Current active analytics filters (age, gender, classification).",
    )
    question_text: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Classification question text clicked in Prelim.",
    )
    answer: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Selected answer option clicked in Prelim.",
    )
    limit: int = Field(
        default=120,
        ge=1,
        le=500,
        description="Max respondents to return for the current page.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Pagination offset for respondents list.",
    )


class ClassificationCohortRespondent(BaseModel):
    id: str
    label: str
    session_id: str
    panelist_id: Optional[str] = None
    gender: Optional[str] = None
    age_group: Optional[str] = None
    answers: Dict[str, Optional[str]] = Field(default_factory=dict)


class ClassificationCohortMeta(BaseModel):
    cohort_size: int
    question_text: str
    answer: str
    limit: int
    offset: int
    has_more: bool
    filters_applied: Optional[StudyFilterCriteria] = None


class CohortDemographicBreakdown(BaseModel):
    """Gender / age counts for the full cohort. Omitted when filter already pins one value."""
    gender: Optional[Dict[str, int]] = None
    age_group: Optional[Dict[str, int]] = None


class ClassificationCohortResponse(BaseModel):
    meta: ClassificationCohortMeta
    questions: List[Dict[str, Any]] = Field(default_factory=list)
    respondents: List[ClassificationCohortRespondent] = Field(default_factory=list)
    cross_tabs: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    demographic_breakdown: Optional[CohortDemographicBreakdown] = None


class SavedFilterReportCreate(BaseModel):
    """Create a named saved filter report."""
    name: str = Field(..., min_length=1, max_length=255)
    filters: StudyFilterCriteria = Field(default_factory=StudyFilterCriteria)


class SavedFilterReportUpdate(BaseModel):
    """Rename a saved filter report."""
    name: str = Field(..., min_length=1, max_length=255)


class SavedFilterReportOut(BaseModel):
    id: UUID
    study_id: UUID
    name: str
    filters: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class StudyFilterPayload(BaseModel):
    """Request body for POST /study/{study_id}/filter (filtered regression report)."""
    filters: Optional[StudyFilterCriteria] = Field(
        None,
        description="Filter criteria. Omitted or empty = no filtering (full study).",
    )
    include_per_panelist: bool = Field(
        default=False,
        description="If true, include per-panelist coefficient tables in the response.",
    )
    save_to_history: bool = Field(
        default=True,
        description="If true, save this filter selection to the user's filter history for the study.",
    )
    name: Optional[str] = Field(
        None,
        max_length=255,
        description="Optional label for this filter when saving to history (e.g. 'Male 18-24 No').",
    )


class RatingScoringGroup(BaseModel):
    """Ratings mapped to 100 or 0 for top-down / bottom-up regressions."""
    hundred: List[int] = Field(default_factory=list, description="Ratings coded as 100")
    zero: List[int] = Field(default_factory=list, description="Ratings coded as 0")


class AnalysisRegressionSettings(BaseModel):
    include_intercept: bool = Field(default=True, description="If false, regress without intercept")


class StudyAnalysisSettingsPayload(BaseModel):
    """Rating mappings and intercept mode for study analysis."""
    top: RatingScoringGroup = Field(default_factory=lambda: RatingScoringGroup(hundred=[4, 5], zero=[1, 2, 3]))
    bottom: RatingScoringGroup = Field(default_factory=lambda: RatingScoringGroup(hundred=[1, 2], zero=[3, 4, 5]))
    regression: AnalysisRegressionSettings = Field(default_factory=AnalysisRegressionSettings)


class StudyAnalysisSettingsResponse(BaseModel):
    study_id: UUID
    settings: StudyAnalysisSettingsPayload
    max_rating: int = 5
    is_default: bool = False
    updated_at: Optional[str] = None


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