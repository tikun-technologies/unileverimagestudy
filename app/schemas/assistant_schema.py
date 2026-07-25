"""Strict schemas for the verified analytics assistant."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.response_schema import StudyFilterCriteria


class AssistantToolName(str, Enum):
    greeting = "greeting"
    study_overview = "study_overview"
    classification_distribution = "classification_distribution"
    rank_elements = "rank_elements"
    rank_designs = "rank_designs"
    explain_design = "explain_design"
    compare_segments = "compare_segments"
    use_avoid_elements = "use_avoid_elements"
    response_time_summary = "response_time_summary"
    fatigue_summary = "fatigue_summary"
    explain_mindset = "explain_mindset"
    list_saved_designs = "list_saved_designs"
    clarify = "clarify"
    unsupported = "unsupported"


class AssistantMetric(str, Enum):
    T = "T"
    B = "B"
    R = "R"


class RankDirection(str, Enum):
    highest = "highest"
    lowest = "lowest"


class AssistantFollowUpContext(BaseModel):
    """Bounded structured conversation state (never free-form history dump)."""

    metric: Optional[AssistantMetric] = None
    segment_section: Optional[str] = None
    segment_key: Optional[str] = None
    # Classification can combine Gender + Age at once.
    gender_key: Optional[str] = None
    age_key: Optional[str] = None
    classification_question: Optional[str] = None
    # Pending option filters while clarifying which classification question.
    classification_options: List[str] = Field(default_factory=list, max_length=8)
    last_tool: Optional[AssistantToolName] = None
    last_direction: Optional[RankDirection] = None
    last_limit: Optional[int] = Field(None, ge=1, le=20)


class AssistantQueryRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)
    filters: Optional[StudyFilterCriteria] = None
    use_active_filters: bool = True
    metric: Optional[AssistantMetric] = None
    segment_section: Optional[str] = None
    segment_key: Optional[str] = None
    follow_up: Optional[AssistantFollowUpContext] = None
    conversation_id: Optional[str] = Field(None, max_length=64)

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        cleaned = " ".join((value or "").strip().split())
        if not cleaned:
            raise ValueError("Message cannot be empty")
        return cleaned


class AssistantQueryPlan(BaseModel):
    tool: AssistantToolName
    metric: Optional[AssistantMetric] = AssistantMetric.T
    direction: Optional[RankDirection] = RankDirection.highest
    limit: int = Field(default=10, ge=1, le=20)
    segment_section: Optional[str] = None
    segment_key: Optional[str] = None
    # Optional dual demographic filters (used heavily by classification counts).
    gender_key: Optional[str] = None
    age_key: Optional[str] = None
    classification_question: Optional[str] = None
    # Classification option labels mentioned by the user (may appear on multiple questions).
    classification_options: List[str] = Field(default_factory=list, max_length=8)
    # User-requested required ingredients, e.g. element names or "white colour".
    must_include: List[str] = Field(default_factory=list, max_length=8)
    clarification_prompt: Optional[str] = Field(None, max_length=400)
    clarification_options: List[str] = Field(default_factory=list, max_length=8)
    unsupported_reason: Optional[str] = Field(None, max_length=400)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("must_include")
    @classmethod
    def clean_must_include(cls, value: List[str]) -> List[str]:
        cleaned: List[str] = []
        for item in value or []:
            text = " ".join(str(item or "").strip().split())
            if text and text not in cleaned:
                cleaned.append(text[:120])
            if len(cleaned) >= 8:
                break
        return cleaned


class EvidenceFact(BaseModel):
    fact_id: str
    label: str
    value: Optional[Union[float, int, str]] = None
    unit: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class AppliedContext(BaseModel):
    study_id: UUID
    study_type: str
    study_title: Optional[str] = None
    metric: Optional[str] = None
    segment_label: Optional[str] = None
    filters: Optional[Dict[str, Any]] = None
    base_size: Optional[int] = None
    panelists: Optional[int] = None
    analysis_settings_echo: Optional[Dict[str, Any]] = None
    verified: bool = True
    algorithm_version: str = "1.0.0"


class ChartSeriesItem(BaseModel):
    name: str
    value: float
    fill: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class ChartBlock(BaseModel):
    type: Literal["chart"] = "chart"
    chart_type: Literal["bar", "horizontal_bar", "pie", "donut"] = "horizontal_bar"
    title: str
    items: List[ChartSeriesItem] = Field(default_factory=list)


class ElementRankItem(BaseModel):
    rank: int
    element_id: str
    code: Optional[str] = None
    name: str
    category: str
    value: float
    above_threshold: Optional[bool] = None
    image_url: Optional[str] = None
    element_type: Optional[str] = None
    z_index: Optional[int] = None
    layer_id: Optional[str] = None
    image_id: Optional[str] = None
    transform: Optional[Dict[str, Any]] = None
    fact_id: str


class DesignElementSnapshot(BaseModel):
    element_id: str
    category_key: str
    category_name: str
    name: str
    value: float
    code: Optional[str] = None
    image_url: Optional[str] = None
    element_type: Optional[str] = None
    z_index: int = 0
    layer_id: Optional[str] = None
    image_id: Optional[str] = None
    transform: Optional[Dict[str, Any]] = None


class DesignRankItem(BaseModel):
    rank: int
    score: float
    selection_count: int
    selected_by_category: Dict[str, str]
    elements: List[DesignElementSnapshot]
    fact_id: str
    constraints_applied: bool = True
    complete_layers: bool = True


class ClassificationOptionCount(BaseModel):
    option: str
    count: int
    percentage: float
    answered: bool = True
    fact_id: str


class AssistantBlock(BaseModel):
    type: str
    title: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class AssistantAction(BaseModel):
    type: Literal[
        "apply_filter",
        "open_view",
        "open_configurator",
        "save_design",
        "compare_designs",
        "export_csv",
        "set_metric",
        "set_segment",
    ]
    label: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class AssistantQueryResponse(BaseModel):
    request_id: str
    status: Literal["answered", "needs_clarification", "unsupported", "error"]
    answer_text: str
    tool: Optional[AssistantToolName] = None
    applied_context: AppliedContext
    blocks: List[AssistantBlock] = Field(default_factory=list)
    evidence: List[EvidenceFact] = Field(default_factory=list)
    follow_ups: List[str] = Field(default_factory=list)
    actions: List[AssistantAction] = Field(default_factory=list)
    clarification_options: List[str] = Field(default_factory=list)
    follow_up_context: Optional[AssistantFollowUpContext] = None
    usage: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
