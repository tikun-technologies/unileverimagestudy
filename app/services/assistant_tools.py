"""
Deterministic analytics tools for the verified assistant.

Never send full analysis JSON to the LLM. These tools extract compact,
verified evidence packets from cached/generated analysis.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, defer, selectinload

from app.core.cache import RedisCache
from app.core.config import settings
from app.core.domain import is_unilever_domain
from app.models.response_model import ClassificationAnswer, StudyResponse
from app.models.study_model import Study, StudyClassificationQuestion, StudyMember, StudySavedDesign
from app.models.user_model import User
from app.schemas.assistant_schema import (
    AppliedContext,
    AssistantAction,
    AssistantBlock,
    AssistantFollowUpContext,
    AssistantMetric,
    AssistantQueryPlan,
    AssistantToolName,
    ClassificationOptionCount,
    CompareMode,
    DesignElementSnapshot,
    DesignRankItem,
    ElementRankItem,
    EvidenceFact,
    RankDirection,
)
from app.services.analysis import StudyAnalysisService
from app.services.analysis_settings import get_study_analysis_settings
from app.services.design_optimizer import (
    ALGORITHM_VERSION,
    OptimizerElement,
    build_categories_from_analysis,
    build_conflict_pair_set,
    canonicalize_design_constraints,
    metric_prefix,
    rank_designs,
    rank_elements,
    section_key_for,
    verify_design,
)
from app.services.response import StudyResponseService


METRIC_LABELS = {
    "T": "Top Down",
    "B": "Bottom Up",
    "R": "Response Time",
}


class AssistantToolError(Exception):
    def __init__(self, message: str, status: str = "error"):
        super().__init__(message)
        self.status = status
        self.message = message


def authorize_study_for_assistant(db: Session, study_id: UUID, current_user: User) -> Study:
    study_obj = (
        db.query(Study)
        .options(defer(Study.tasks), selectinload(Study.classification_questions))
        .filter(Study.id == study_id)
        .first()
    )
    if not study_obj:
        raise AssistantToolError("Study not found", status="error")
    if study_obj.creator_id != current_user.id:
        member = db.scalar(
            select(StudyMember).where(
                StudyMember.study_id == study_id,
                StudyMember.user_id == current_user.id,
            )
        )
        if not member:
            raise AssistantToolError("Access denied", status="error")
    return study_obj


def _normalize_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _build_study_data_dict(study_obj: Study) -> Dict[str, Any]:
    from app.services.study import build_study_data_for_analysis

    return build_study_data_for_analysis(study_obj)


# Small in-process memo so repeated identical-filter loads inside one request
# (e.g. compare / cohort tools) never rebuild — even if Redis is unavailable.
# Bounded + short TTL to avoid stale data and unbounded memory growth.
_ANALYSIS_MEMO: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
_ANALYSIS_MEMO_MAX = 32
_ANALYSIS_MEMO_TTL = 20.0


def _analysis_cache_key(
    study_id: Any,
    unilever_format: bool,
    filters: Optional[Dict[str, Any]],
    analysis_options: Optional[Dict[str, Any]] = None,
) -> str:
    # analysis_options must be part of the key: the report's numbers are computed
    # from them (with/without intercept, thresholds, ...). Without this, toggling
    # a setting silently keeps serving numbers computed under the old one until
    # the TTL happens to expire.
    payload = json.dumps(
        {"filters": filters or {}, "analysis_options": analysis_options or {}},
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    fmt = "uni" if unilever_format else "std"
    return f"assistant:analysis:{study_id}:{fmt}:{digest}"


def _analysis_memo_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _ANALYSIS_MEMO.get(key)
    if not entry:
        return None
    ts, value = entry
    if (time.monotonic() - ts) > _ANALYSIS_MEMO_TTL:
        _ANALYSIS_MEMO.pop(key, None)
        return None
    _ANALYSIS_MEMO.move_to_end(key)
    return value


def _analysis_memo_set(key: str, value: Dict[str, Any]) -> None:
    _ANALYSIS_MEMO[key] = (time.monotonic(), value)
    _ANALYSIS_MEMO.move_to_end(key)
    while len(_ANALYSIS_MEMO) > _ANALYSIS_MEMO_MAX:
        _ANALYSIS_MEMO.popitem(last=False)


def load_analysis_for_assistant(
    db: Session,
    study_obj: Study,
    current_user: User,
    filters: Optional[Dict[str, Any]] = None,
    *,
    use_cache: bool = True,
) -> Dict[str, Any]:
    unilever_format = is_unilever_domain(current_user.email or "")
    # Fetched before the cache lookup — it's one indexed row, and the key must
    # include it (see _analysis_cache_key).
    analysis_options = get_study_analysis_settings(db, study_obj.id, study=study_obj)
    cache_key = _analysis_cache_key(study_obj.id, unilever_format, filters, analysis_options)

    if use_cache:
        memo = _analysis_memo_get(cache_key)
        if memo is not None:
            return memo
        cached = RedisCache.get(cache_key)
        if isinstance(cached, dict) and cached:
            _analysis_memo_set(cache_key, cached)
            return cached

    response_service = StudyResponseService(db)
    df = response_service.get_study_dataframe(
        study_obj.id,
        unilever_format=unilever_format,
        completed_only=True,
    )
    study_data = _build_study_data_dict(study_obj)
    analysis_service = StudyAnalysisService()
    report = analysis_service.generate_json_report(
        df,
        study_data,
        include_raw_data=False,
        analysis_options=analysis_options,
        filters=filters or None,
    )
    report = report or {}

    if use_cache and report:
        _analysis_memo_set(cache_key, report)
        try:
            RedisCache.set(
                cache_key,
                report,
                ttl_seconds=settings.ASSISTANT_ANALYSIS_CACHE_TTL_SECONDS,
            )
        except Exception:
            pass

    return report


def _segment_label(
    segment_section: Optional[str],
    segment_key: Optional[str],
    gender_key: Optional[str] = None,
    age_key: Optional[str] = None,
) -> str:
    parts: List[str] = []
    if gender_key:
        parts.append(str(gender_key))
    if age_key:
        parts.append(str(age_key))
    if parts:
        return " · ".join(parts)
    if segment_key:
        return str(segment_key)
    if segment_section:
        return str(segment_section)
    return "Overall"


def _normalize_gender_label(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    key = _normalize_key(raw)
    if key in {"male", "males", "man", "men", "m"} or key.startswith("male"):
        return "Male"
    if key in {"female", "females", "woman", "women", "f"} or key.startswith("female"):
        return "Female"
    return None


def _age_from_personal_info(personal_info: Any) -> Optional[str]:
    """Return canonical age bucket from respondent personal_info."""
    if not isinstance(personal_info, dict):
        return None
    age_val = personal_info.get("age")
    if age_val is None:
        age_val = personal_info.get("Age")
    if age_val is not None:
        try:
            return age_number_to_bucket(float(age_val))
        except (TypeError, ValueError):
            resolved = resolve_age_segment_key(str(age_val))
            if resolved:
                return resolved
    dob = (
        personal_info.get("dob")
        or personal_info.get("date_of_birth")
        or personal_info.get("DateOfBirth")
    )
    if isinstance(dob, str) and dob.strip():
        try:
            from datetime import datetime

            dob_ts = datetime.fromisoformat(dob.strip().replace("Z", "+00:00"))
            years = (datetime.now(dob_ts.tzinfo) - dob_ts).days / 365.25
            return age_number_to_bucket(years)
        except Exception:
            return None
    return None


def _gender_from_personal_info(personal_info: Any) -> Optional[str]:
    if not isinstance(personal_info, dict):
        return None
    return _normalize_gender_label(personal_info.get("gender") or personal_info.get("Gender"))


# Standard analysis age buckets (matches StudyAnalysisService._age_range_label).
AGE_BUCKETS = ("13-18", "18-24", "25-34", "35-44", "45-54", "55-64", "65+")


def age_number_to_bucket(age: float) -> Optional[str]:
    """Map a numeric age (e.g. 47) onto the study's age-band label."""
    if age != age:  # NaN
        return None
    if age >= 65:
        return "65+"
    if age >= 55:
        return "55-64"
    if age >= 45:
        return "45-54"
    if age >= 35:
        return "35-44"
    if age >= 25:
        return "25-34"
    if age >= 18:
        return "18-24"
    if age >= 13:
        return "13-18"
    return None


def resolve_age_segment_key(requested: Optional[str]) -> Optional[str]:
    """
    Normalize age language onto a canonical Age segment key.

    Examples:
      "45" / "47" -> "45-54"
      "45-50" -> "45-54" (midpoint snap)
      "45-54" / "13-17" -> "45-54" / "13-18"
    """
    if requested is None:
        return None
    raw = str(requested).strip()
    if not raw:
        return None

    compact = re.sub(r"\s+", "", raw)
    # Exact / near-exact standard buckets
    if compact in {"13-17", "13–17"}:
        return "13-18"
    if compact in AGE_BUCKETS or compact.replace("–", "-") in AGE_BUCKETS:
        return compact.replace("–", "-")
    if compact in {"65+", "65plus", "65AndOver", "65andover"}:
        return "65+"

    # Single age number: "45", "47", "65"
    if re.fullmatch(r"\d{1,3}", compact):
        return age_number_to_bucket(float(compact))

    # Custom / near-miss ranges: "45-50", "40-49"
    range_match = re.fullmatch(r"(\d{1,3})\s*[-–]\s*(\d{1,3})", compact.replace("–", "-"))
    if range_match:
        lo = int(range_match.group(1))
        hi = int(range_match.group(2))
        if lo > hi:
            lo, hi = hi, lo
        exact = f"{lo}-{hi}"
        if exact == "13-17":
            return "13-18"
        if exact in AGE_BUCKETS:
            return exact
        # Prefer the bucket covering the midpoint of the requested span.
        return age_number_to_bucket((lo + hi) / 2.0)

    return None


def extract_age_segment_from_text(text: str) -> Optional[str]:
    """Pull an age bucket from free-text questions like 'age 47' or '45-50'."""
    if not text:
        return None
    # Prefer an explicit standard bucket when present.
    exact = re.search(r"\b(13-17|13-18|18-24|25-34|35-44|45-54|55-64|65\+)\b", text)
    if exact:
        return resolve_age_segment_key(exact.group(1))

    # Custom range near age wording, or any NN-NN when "age" is present.
    custom = re.search(
        r"(?:age(?:\s*group)?(?:\s*of)?|aged)\s*[:=]?\s*(\d{1,3}\s*[-–]\s*\d{1,3})",
        text,
        flags=re.IGNORECASE,
    )
    if not custom and re.search(r"\bage\b|\baged\b", text):
        custom = re.search(r"\b(\d{1,3}\s*[-–]\s*\d{1,3})\b", text)
    if custom:
        return resolve_age_segment_key(custom.group(1))

    # Single age: "age 47", "age of 45", "aged 50", "47 years old", "22 year old"
    single = re.search(
        r"(?:age(?:\s*group)?(?:\s*of)?|aged)\s*[:=]?\s*(\d{1,3})\b",
        text,
        flags=re.IGNORECASE,
    )
    if not single:
        single = re.search(r"\b(\d{1,3})\s*(?:years?\s*old|yo)\b", text, flags=re.IGNORECASE)
    if single:
        return resolve_age_segment_key(single.group(1))

    # Bare 65+
    if re.search(r"\b65\+\b", text):
        return "65+"
    return None


def extract_gender_from_text(text: str) -> Optional[str]:
    """Pull Male/Female from free-text when present."""
    if not text:
        return None
    lowered = text.casefold()
    if re.search(r"\b(?:female|females|women|woman)\b", lowered):
        return "Female"
    if re.search(r"\b(?:male|males|men|man)\b", lowered):
        return "Male"
    return None


def _canonical_segment_key(
    analysis: Dict[str, Any],
    metric: str,
    segment_section: Optional[str],
    requested: Optional[str],
) -> Optional[str]:
    if not requested:
        return None

    # Snap age language onto analysis buckets before lookup ("47" -> "45-54").
    age_resolved = resolve_age_segment_key(requested)
    lookup_key = age_resolved or requested

    section = analysis.get(section_key_for(metric, segment_section)) or {}
    candidates: List[str] = []
    segments = section.get("segments") or {}
    if isinstance(segments, dict):
        candidates.extend(str(key) for key in segments.keys())
    groups = section.get("groups") or {}
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, dict):
                candidates.extend(str(key) for key in group.keys() if str(key) != "Total")
    for category in section.get("categories") or []:
        for element in category.get("elements") or []:
            values = element.get("values") or {}
            if isinstance(values, dict):
                candidates.extend(str(key) for key in values.keys())

    candidates = list(dict.fromkeys(candidates))
    requested_norm = re.sub(r"[^a-z0-9]+", "", str(lookup_key).casefold())
    for candidate in candidates:
        candidate_norm = re.sub(r"[^a-z0-9]+", "", candidate.casefold())
        if candidate_norm == requested_norm:
            return candidate

    aliases = {
        "men": "male",
        "man": "male",
        "males": "male",
        "women": "female",
        "woman": "female",
        "females": "female",
    }
    alias_norm = aliases.get(requested_norm, requested_norm)
    for candidate in candidates:
        candidate_norm = re.sub(r"[^a-z0-9]+", "", candidate.casefold())
        if candidate_norm == alias_norm:
            return candidate

    # "Mindset 1" should resolve to a real available key rather than becoming
    # an empty segment. Prefer the 2-cluster label when both variants exist.
    mindset_match = re.fullmatch(r"mindset([123])", requested_norm)
    if mindset_match:
        number = mindset_match.group(1)
        matches = [
            candidate
            for candidate in candidates
            if re.sub(r"[^a-z0-9]+", "", candidate.casefold()).startswith(f"mindset{number}of")
        ]
        matches.sort(key=lambda value: ("of2" not in value.casefold().replace("_", ""), value))
        if matches:
            return matches[0]

    # Age snap even when the Age section isn't loaded yet / key not in candidates:
    # return the canonical bucket so callers show the right label in errors.
    if age_resolved:
        return age_resolved
    return requested


def _available_segment_keys(
    analysis: Dict[str, Any],
    metric: str,
    segment_section: Optional[str],
) -> List[str]:
    section = analysis.get(section_key_for(metric, segment_section)) or {}
    candidates: List[str] = []
    segments = section.get("segments") or {}
    if isinstance(segments, dict):
        candidates.extend(str(key) for key in segments.keys())
    groups = section.get("groups") or {}
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, dict):
                candidates.extend(str(key) for key in group.keys() if str(key) != "Total")
    for category in section.get("categories") or []:
        for element in category.get("elements") or []:
            values = element.get("values") or {}
            if isinstance(values, dict):
                candidates.extend(str(key) for key in values.keys())
    return list(dict.fromkeys(candidates))


def _missing_segment_result(
    requested: str,
    available: List[str],
) -> Dict[str, Any]:
    available_text = ", ".join(available) if available else "none"
    return {
        "status": "answered",
        "answer_text": (
            f"There is no analyzed respondent segment named “{requested}” in this study. "
            f"Available segments: {available_text}."
        ),
        "blocks": [],
        "evidence": [],
        "follow_ups": [f"Show the best design for {value}" for value in available[:3]]
        or ["Show the best design overall"],
        "actions": [],
    }


def _base_size(analysis: Dict[str, Any], metric: str, segment_section: Optional[str], segment_key: Optional[str]) -> Optional[int]:
    section = analysis.get(section_key_for(metric, segment_section)) or {}
    if segment_key:
        segments = section.get("segments") or {}
        if isinstance(segments, dict) and segment_key in segments:
            return int((segments[segment_key] or {}).get("base_size") or 0)
        groups = section.get("groups") or {}
        if isinstance(groups, dict):
            for group in groups.values():
                if isinstance(group, dict) and segment_key in group:
                    return int((group[segment_key] or {}).get("base_size") or group[segment_key] or 0)
    if section.get("base_size") is not None:
        return int(section.get("base_size") or 0)
    summary = analysis.get("dashboard_summary") or {}
    return int(summary.get("uniquePanelists") or summary.get("totalRespondents") or 0) or None


def build_applied_context(
    study_obj: Study,
    analysis: Dict[str, Any],
    plan: AssistantQueryPlan,
    filters: Optional[Dict[str, Any]],
) -> AppliedContext:
    metric = metric_prefix((plan.metric or AssistantMetric.T).value)
    summary = analysis.get("dashboard_summary") or {}
    return AppliedContext(
        study_id=study_obj.id,
        study_type=str(study_obj.study_type or "grid"),
        study_title=study_obj.title,
        metric=METRIC_LABELS.get(metric, metric),
        segment_label=_segment_label(
            plan.segment_section,
            plan.segment_key,
            gender_key=getattr(plan, "gender_key", None),
            age_key=getattr(plan, "age_key", None),
        ),
        filters=filters or None,
        base_size=_base_size(analysis, metric, plan.segment_section, plan.segment_key),
        panelists=int(summary.get("uniquePanelists") or 0) or None,
        analysis_settings_echo=analysis.get("analysis_settings"),
        verified=True,
        algorithm_version=ALGORITHM_VERSION,
    )


def _fact(fact_id: str, label: str, value: Any = None, **meta: Any) -> EvidenceFact:
    return EvidenceFact(fact_id=fact_id, label=label, value=value, meta=meta)


def tool_study_overview(analysis: Dict[str, Any], context: AppliedContext) -> Dict[str, Any]:
    summary = analysis.get("dashboard_summary") or {}
    info = analysis.get("Information Block") or {}
    evidence = [
        _fact("F1", "Unique panelists", summary.get("uniquePanelists")),
        _fact("F2", "Total responses", summary.get("totalResponses")),
        _fact("F3", "Average rating", round(float(summary.get("avgRating") or 0), 2)),
        _fact("F4", "Average response time (s)", round(float(summary.get("avgResponseTime") or 0), 2)),
        _fact("F5", "Categories / layers", summary.get("categoryCount")),
    ]
    answer = (
        f"{context.study_title or 'This study'} has {summary.get('uniquePanelists') or 0} panelists "
        f"and {summary.get('totalResponses') or 0} scored responses. "
        f"Average rating is {round(float(summary.get('avgRating') or 0), 2)} "
        f"with average response time {round(float(summary.get('avgResponseTime') or 0), 2)}s."
    )
    blocks = [
        AssistantBlock(
            type="kpi",
            title="Study overview",
            data={
                "items": [
                    {"label": "Panelists", "value": summary.get("uniquePanelists") or 0, "fact_id": "F1"},
                    {"label": "Responses", "value": summary.get("totalResponses") or 0, "fact_id": "F2"},
                    {"label": "Avg rating", "value": round(float(summary.get("avgRating") or 0), 2), "fact_id": "F3"},
                    {"label": "Avg time (s)", "value": round(float(summary.get("avgResponseTime") or 0), 2), "fact_id": "F4"},
                ],
                "study_type": context.study_type,
                "background": info.get("Study Background"),
            },
        ),
        AssistantBlock(
            type="chart",
            title="Rating distribution",
            data={
                "chart_type": "donut",
                "items": summary.get("ratingDistribution") or [],
            },
        ),
    ]
    follow_ups = [
        "Show top 10 elements",
        "Show best 10 designs",
        "How many answered each classification question?",
    ]
    return {
        "answer_text": answer,
        "blocks": [b.model_dump() for b in blocks],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": follow_ups,
        "actions": [
            AssistantAction(type="open_view", label="Open overview", payload={"view": "overview"}).model_dump()
        ],
    }


def _classification_questions(study_obj: Study) -> List[StudyClassificationQuestion]:
    questions = getattr(study_obj, "classification_questions", None) or []
    return sorted(
        list(questions),
        key=lambda q: (q.order if q.order is not None else 0, q.question_text or ""),
    )


def _resolve_classification_question(
    study_obj: Study,
    question_hint: Optional[str],
) -> Tuple[Optional[StudyClassificationQuestion], List[str]]:
    questions = _classification_questions(study_obj)
    if not questions:
        return None, []
    if not question_hint:
        if len(questions) == 1:
            return questions[0], []
        return None, [q.question_text for q in questions]
    hint = _normalize_key(question_hint)
    exact = [q for q in questions if _normalize_key(q.question_text) == hint]
    if exact:
        return exact[0], []
    partial = [q for q in questions if hint in _normalize_key(q.question_text) or _normalize_key(q.question_text) in hint]
    if len(partial) == 1:
        return partial[0], []
    return None, [q.question_text for q in (partial or questions)]


def _iter_option_records(answer_options: Any) -> List[Dict[str, str]]:
    """Normalize configured options to {id, text} records."""
    records: List[Dict[str, str]] = []
    if not isinstance(answer_options, list):
        return records
    for item in answer_options:
        if isinstance(item, dict):
            text = item.get("text") or item.get("label") or item.get("value") or item.get("option_text")
            oid = item.get("id") or item.get("option_id") or item.get("value_id")
            if text is None and oid is None:
                continue
            records.append(
                {
                    "id": str(oid).strip() if oid is not None else "",
                    "text": str(text).strip() if text is not None else str(oid).strip(),
                }
            )
        elif item is not None:
            records.append({"id": "", "text": str(item).strip()})
    return [row for row in records if row["text"]]


def _extract_option_texts(answer_options: Any) -> List[str]:
    return [row["text"] for row in _iter_option_records(answer_options)]


def _option_answer_maps(answer_options: Any) -> Tuple[List[str], Dict[str, str]]:
    """
    Build display labels + lookup from stored answer values (id or text) → label.
    Respondent answers often store option ids (e.g. UUID fragments), not labels.
    """
    records = _iter_option_records(answer_options)
    labels: List[str] = []
    lookup: Dict[str, str] = {}
    for row in records:
        text = row["text"]
        labels.append(text)
        lookup[_normalize_key(text)] = text
        oid = row["id"]
        if not oid:
            continue
        lookup[_normalize_key(oid)] = text
        # Truncated / prefixed ids used in some response payloads.
        compact = re.sub(r"[^a-z0-9]+", "", oid.casefold())
        if compact:
            lookup[compact] = text
            if len(compact) >= 8:
                lookup[compact[:8]] = text
    return labels, lookup


def _map_classification_answer(raw: str, lookup: Dict[str, str]) -> str:
    value = str(raw or "").strip()
    if not value:
        return value
    key = _normalize_key(value)
    if key in lookup:
        return lookup[key]
    compact = re.sub(r"[^a-z0-9]+", "", value.casefold())
    if compact in lookup:
        return lookup[compact]
    # Prefix / containment match for truncated UUIDs like "2282f2ad-4".
    if compact:
        for candidate_key, label in lookup.items():
            cand = re.sub(r"[^a-z0-9]+", "", candidate_key)
            if not cand:
                continue
            if cand.startswith(compact) or compact.startswith(cand):
                return label
    return value


def match_classification_options_in_text(study_obj: Study, message: str) -> List[str]:
    """Find configured option labels mentioned in the user message (longest first)."""
    text_norm = _normalize_key(message)
    if not text_norm:
        return []
    catalog: List[Tuple[str, str]] = []
    for question in _classification_questions(study_obj):
        for label in _extract_option_texts(question.answer_options):
            catalog.append((_normalize_key(label), label))
    catalog.sort(key=lambda row: (-len(row[0]), row[0]))
    found: List[str] = []
    consumed = text_norm
    for norm, label in catalog:
        if len(norm) < 3:
            continue
        if norm in consumed and label not in found:
            found.append(label)
            consumed = consumed.replace(norm, " ", 1)
        if len(found) >= 8:
            break
    return found


def resolve_classification_option_hint(study_obj: Study, hint: str) -> Optional[str]:
    """Map a short token like 'rarely' to a configured option label."""
    hint_norm = _normalize_key(hint)
    if len(hint_norm) < 3:
        return None
    best: Optional[str] = None
    for question in _classification_questions(study_obj):
        for label in _extract_option_texts(question.answer_options):
            label_norm = _normalize_key(label)
            if hint_norm == label_norm:
                return label
            if hint_norm in label_norm or label_norm.startswith(hint_norm):
                if best is None or len(label) < len(best):
                    best = label
    return best


def resolve_classification_question_from_options(
    study_obj: Study,
    option_labels: List[str],
) -> Tuple[Optional[StudyClassificationQuestion], List[str]]:
    """If options uniquely identify one question, return it; else candidate question texts."""
    if not option_labels:
        return None, []
    wanted = {_normalize_key(opt) for opt in option_labels}
    matches: List[StudyClassificationQuestion] = []
    for question in _classification_questions(study_obj):
        option_norms = {_normalize_key(opt) for opt in _extract_option_texts(question.answer_options)}
        if wanted & option_norms:
            matches.append(question)
    if len(matches) == 1:
        return matches[0], []
    return None, [q.question_text for q in (matches or _classification_questions(study_obj))]


def find_classification_question_by_text(
    study_obj: Study,
    message: str,
) -> Optional[StudyClassificationQuestion]:
    hint = _normalize_key(message)
    if not hint:
        return None
    for question in _classification_questions(study_obj):
        if _normalize_key(question.question_text) == hint:
            return question
    return None


def _filter_completed_responses_by_demographics(
    db: Session,
    study_id: UUID,
    gender_key: Optional[str],
    age_key: Optional[str],
) -> List[Any]:
    """Completed response ids, optionally restricted to Gender / Age buckets."""
    rows = db.execute(
        select(StudyResponse.id, StudyResponse.personal_info).where(
            StudyResponse.study_id == study_id,
            StudyResponse.is_completed.is_(True),
        )
    ).all()
    want_gender = _normalize_gender_label(gender_key) if gender_key else None
    want_age = resolve_age_segment_key(age_key) if age_key else None
    if want_age == "13-17":
        want_age = "13-18"

    if not want_gender and not want_age:
        return [row[0] for row in rows]

    kept: List[Any] = []
    for response_id, personal_info in rows:
        if want_gender:
            got_gender = _gender_from_personal_info(personal_info)
            if _normalize_key(got_gender) != _normalize_key(want_gender):
                continue
        if want_age:
            got_age = _age_from_personal_info(personal_info)
            # Tolerate 13-17 vs 13-18 labeling differences in stored data.
            got_norm = "13-18" if got_age == "13-17" else got_age
            if _normalize_key(got_norm) != _normalize_key(want_age):
                continue
        kept.append(response_id)
    return kept


def tool_classification_distribution(
    db: Session,
    study_obj: Study,
    plan: AssistantQueryPlan,
    filters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    focus_options = [
        str(item).strip()
        for item in (plan.classification_options or [])
        if str(item or "").strip()
    ][:8]
    gender_key = _normalize_gender_label(plan.gender_key) or (
        _normalize_gender_label(plan.segment_key)
        if str(plan.segment_section or "").casefold() == "gender"
        else None
    )
    age_key = resolve_age_segment_key(plan.age_key) or (
        resolve_age_segment_key(plan.segment_key)
        if str(plan.segment_section or "").casefold() == "age"
        else None
    )
    if age_key == "13-17":
        age_key = "13-18"
    segment_label = _segment_label(plan.segment_section, plan.segment_key, gender_key, age_key)

    question, candidates = _resolve_classification_question(study_obj, plan.classification_question)
    if question is None and focus_options:
        question, candidates = resolve_classification_question_from_options(study_obj, focus_options)

    pending_follow = AssistantFollowUpContext(
        last_tool=AssistantToolName.classification_distribution,
        classification_options=focus_options,
        gender_key=gender_key,
        age_key=age_key,
        segment_section=plan.segment_section,
        segment_key=plan.segment_key,
    )

    if question is None:
        prompt = (
            "Which classification question should I use for "
            + (", ".join(f"“{opt}”" for opt in focus_options) if focus_options else "that")
            + "?"
            if focus_options
            else "Which classification question do you mean?"
        )
        if focus_options and len(candidates) > 1:
            prompt = (
                f"“{focus_options[0]}” appears on more than one classification question. "
                "Which question do you mean?"
            )
        return {
            "status": "needs_clarification",
            "answer_text": prompt,
            "clarification_options": candidates[:8],
            "blocks": [],
            "evidence": [],
            "follow_ups": [],
            "actions": [],
            "follow_up_context": pending_follow.model_dump(),
        }

    response_ids = _filter_completed_responses_by_demographics(
        db, study_obj.id, gender_key, age_key
    )
    total_respondents = len(response_ids)
    options, answer_lookup = _option_answer_maps(question.answer_options)
    counts: Dict[str, int] = {opt: 0 for opt in options}
    answered_ids = set()

    if response_ids:
        rows = db.execute(
            select(ClassificationAnswer.study_response_id, ClassificationAnswer.answer).where(
                ClassificationAnswer.study_response_id.in_(response_ids),
                ClassificationAnswer.question_text == question.question_text,
            )
        ).all()
        for response_id, answer in rows:
            answered_ids.add(response_id)
            mapped = _map_classification_answer(str(answer or ""), answer_lookup)
            counts[mapped] = counts.get(mapped, 0) + 1

    answered = len(answered_ids)
    unanswered = max(0, total_respondents - answered)
    denom = answered or 1

    # Keep configured option order; append any unexpected mapped labels at the end.
    ordered_labels = list(options)
    for label in counts:
        if label not in ordered_labels:
            ordered_labels.append(label)

    focus_norm = {_normalize_key(opt) for opt in focus_options}
    option_rows: List[ClassificationOptionCount] = []
    evidence: List[EvidenceFact] = [
        _fact("F0", "Question", question.question_text),
        _fact("F_segment", "Segment", segment_label),
        _fact("F_answered", "Total answered", answered),
        _fact("F_total", "Segment respondents", total_respondents),
    ]
    for idx, option in enumerate(ordered_labels, start=1):
        count = int(counts.get(option, 0))
        fact_id = f"F{idx}"
        pct = round((count / denom) * 100, 1) if answered else 0.0
        option_rows.append(
            ClassificationOptionCount(
                option=option,
                count=count,
                percentage=pct,
                answered=count > 0,
                fact_id=fact_id,
            )
        )
        evidence.append(_fact(fact_id, option, count, percentage=pct))

    segment_clause = f" among {segment_label}" if segment_label != "Overall" else ""

    # Natural-language answer: focused options when asked, else short summary.
    if focus_options:
        bits: List[str] = []
        for opt in focus_options:
            match = next(
                (row for row in option_rows if _normalize_key(row.option) == _normalize_key(opt)),
                None,
            )
            if match is None:
                bits.append(f"“{opt}” is not an option on “{question.question_text}”.")
            else:
                bits.append(
                    f"“{match.option}” was selected by {match.count} of {answered} "
                    f"answered respondents{segment_clause} ({match.percentage}%) [{match.fact_id}]."
                )
        answer = " ".join(bits)
        answer += f" Total answered{segment_clause}: {answered} of {total_respondents}."
    else:
        answer = (
            f"Total answered{segment_clause}: {answered} of {total_respondents} respondents "
            f"for “{question.question_text}”."
        )
        if option_rows:
            top = max(option_rows, key=lambda row: (row.count, row.option.casefold()))
            answer += f" Most selected: “{top.option}” ({top.count})."

    blocks = [
        AssistantBlock(
            type="classification_distribution",
            title=question.question_text,
            data={
                "question": question.question_text,
                "answered": answered,
                "unanswered": unanswered,
                "total_respondents": total_respondents,
                "segment_label": segment_label,
                "gender_key": gender_key,
                "age_key": age_key,
                "options": [row.model_dump() for row in option_rows],
                "configured_options": options,
                "focus_options": [row.option for row in option_rows if _normalize_key(row.option) in focus_norm]
                if focus_norm
                else [],
            },
        ),
    ]
    highlighted = focus_options[0] if focus_options else (option_rows[0].option if option_rows else None)
    follow_ups = [
        f"How many selected {highlighted}" if highlighted else "Show study overview",
        "Show all classification answer counts",
        "Show study overview",
    ]
    actions: List[AssistantAction] = []
    if highlighted:
        actions.append(
            AssistantAction(
                type="apply_filter",
                label=f"Filter to {highlighted}",
                payload={
                    "filters": {
                        "classification_filters": {
                            question.question_text: [highlighted],
                        }
                    }
                },
            )
        )
    return {
        "status": "answered",
        "answer_text": answer,
        "blocks": [b.model_dump() for b in blocks],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": follow_ups,
        "actions": [a.model_dump() for a in actions],
        "follow_up_context": AssistantFollowUpContext(
            classification_question=question.question_text,
            classification_options=focus_options,
            gender_key=gender_key,
            age_key=age_key,
            segment_section=plan.segment_section,
            segment_key=plan.segment_key,
            last_tool=AssistantToolName.classification_distribution,
        ).model_dump(),
    }


def enrich_categories_from_study_layers(
    categories: List[Any],
    study_obj: Study,
) -> List[Any]:
    """Attach layer_id, image_id, z_index, transform, and urls from study.layers."""
    layers = list(getattr(study_obj, "layers", None) or [])
    if not layers:
        return categories

    by_name: Dict[str, Any] = {}
    by_layer_id: Dict[str, Any] = {}
    for layer in layers:
        name_key = _normalize_key(getattr(layer, "name", None) or getattr(layer, "title", None))
        if name_key and name_key not in by_name:
            by_name[name_key] = layer
        for alias in (
            getattr(layer, "layer_id", None),
            getattr(layer, "id", None),
        ):
            alias_key = _normalize_key(alias)
            if alias_key and alias_key not in by_layer_id:
                by_layer_id[alias_key] = layer

    for category in categories:
        layer = by_name.get(_normalize_key(category.name))
        if not layer:
            # Fall back to any already-known element layer_id on this category.
            for element in category.elements:
                layer = by_layer_id.get(_normalize_key(element.layer_id))
                if layer:
                    break
        if not layer:
            continue
        category.z_index = int(getattr(layer, "z_index", None) or getattr(layer, "order", 0) or 0)
        images = list(getattr(layer, "images", None) or [])
        image_by_name = {
            _normalize_key(getattr(img, "name", None) or getattr(img, "alt_text", None)): img
            for img in images
            if _normalize_key(getattr(img, "name", None) or getattr(img, "alt_text", None))
        }
        image_by_id = {}
        for img in images:
            for alias in (getattr(img, "image_id", None), getattr(img, "id", None)):
                alias_key = _normalize_key(alias)
                if alias_key and alias_key not in image_by_id:
                    image_by_id[alias_key] = img

        layer_id = str(getattr(layer, "layer_id", None) or getattr(layer, "id", None) or "") or None
        raw_transform = getattr(layer, "transform", None)
        transform = (
            raw_transform
            if isinstance(raw_transform, dict)
            else {"x": 0, "y": 0, "width": 100, "height": 100}
        )
        enriched = []
        for element in category.elements:
            img = image_by_name.get(_normalize_key(element.name))
            if not img and element.image_id:
                img = image_by_id.get(_normalize_key(element.image_id))
            if not img and element.code:
                img = image_by_name.get(_normalize_key(element.code)) or image_by_id.get(
                    _normalize_key(element.code)
                )
            if not img:
                # Still stamp the layer_id so constraint matching has a chance.
                if layer_id and not element.layer_id:
                    enriched.append(
                        OptimizerElement(
                            element_id=element.element_id,
                            category_key=element.category_key,
                            category_name=element.category_name,
                            name=element.name,
                            value=element.value,
                            code=element.code,
                            image_url=element.image_url,
                            element_type=element.element_type,
                            z_index=int(getattr(layer, "z_index", None) or category.z_index or 0),
                            category_order=element.category_order,
                            layer_id=layer_id,
                            image_id=element.image_id,
                            transform=transform or element.transform,
                            above_threshold=element.above_threshold,
                        )
                    )
                else:
                    enriched.append(element)
                continue

            enriched.append(
                OptimizerElement(
                    element_id=element.element_id,
                    category_key=element.category_key,
                    category_name=element.category_name,
                    name=element.name,
                    value=element.value,
                    code=element.code,
                    image_url=getattr(img, "url", None) or element.image_url,
                    element_type=element.element_type or "image",
                    z_index=int(getattr(layer, "z_index", None) or category.z_index or 0),
                    category_order=element.category_order,
                    layer_id=layer_id or element.layer_id,
                    image_id=str(getattr(img, "image_id", None) or getattr(img, "id", None) or "")
                    or element.image_id,
                    transform=transform or element.transform,
                    above_threshold=element.above_threshold,
                )
            )
        category.elements = enriched
    return categories


def tool_rank_elements(
    analysis: Dict[str, Any],
    study_obj: Study,
    plan: AssistantQueryPlan,
) -> Dict[str, Any]:
    metric = metric_prefix((plan.metric or AssistantMetric.T).value)
    direction = (plan.direction or RankDirection.highest).value
    limit = min(plan.limit or 10, settings.ASSISTANT_MAX_RESULT_LIMIT)
    plan.segment_key = _canonical_segment_key(
        analysis, metric, plan.segment_section, plan.segment_key
    )
    if plan.segment_key:
        available = _available_segment_keys(analysis, metric, plan.segment_section)
        if plan.segment_key not in available:
            return _missing_segment_result(plan.segment_key, available)
    categories = build_categories_from_analysis(
        analysis,
        metric=metric,
        segment_section=plan.segment_section,
        segment_key=plan.segment_key,
        study_type=str(study_obj.study_type or "grid"),
    )
    categories = enrich_categories_from_study_layers(categories, study_obj)
    # The conversational contract is strict: singular means exactly one and
    # plural defaults to at most four, even when scores tie at the boundary.
    ranked = rank_elements(categories, direction=direction, limit=limit)[:limit]
    if not ranked:
        return {
            "status": "answered",
            "answer_text": "No element coefficients are available yet. Complete responses are required before ranking.",
            "blocks": [],
            "evidence": [],
            "follow_ups": ["Show study overview"],
            "actions": [],
        }

    items: List[ElementRankItem] = []
    evidence: List[EvidenceFact] = []
    for idx, el in enumerate(ranked, start=1):
        fact_id = f"E{idx}"
        items.append(
            ElementRankItem(
                rank=idx,
                element_id=el.element_id,
                code=el.code,
                name=el.name,
                category=el.category_name,
                value=el.value,
                above_threshold=el.above_threshold,
                image_url=el.image_url,
                element_type=el.element_type,
                z_index=el.z_index,
                layer_id=el.layer_id,
                image_id=el.image_id,
                transform=el.transform,
                fact_id=fact_id,
            )
        )
        evidence.append(_fact(fact_id, f"{el.category_name}: {el.name}", el.value, code=el.code))

    label = "highest" if direction == "highest" else "lowest"
    study_type = str(study_obj.study_type or "grid").lower()
    noun = "statement" if study_type == "text" else "element"
    segment_label = _segment_label(plan.segment_section, plan.segment_key)
    if len(items) == 1:
        qualifier = "best" if direction == "highest" else "lowest-performing"
        answer = (
            f"The {qualifier} {noun} for {METRIC_LABELS.get(metric, metric)} "
            f"({segment_label}) is “{items[0].name}” in {items[0].category}, "
            f"with score {items[0].value} [{items[0].fact_id}]."
        )
    else:
        answer = (
            f"These are the {len(items)} {label}-scoring {noun}s for "
            f"{METRIC_LABELS.get(metric, metric)} ({segment_label}). "
            f"“{items[0].name}” ranks first with score {items[0].value} [{items[0].fact_id}]."
        )
    blocks = [
        AssistantBlock(
            type="top_bottom_elements",
            title=f"{label.title()} {noun}{'' if len(items) == 1 else 's'}",
            data={"direction": direction, "metric": METRIC_LABELS.get(metric, metric), "items": [i.model_dump() for i in items]},
        ),
    ]
    return {
        "answer_text": answer,
        "blocks": [b.model_dump() for b in blocks],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": [
            "Show the opposite ranking",
            "Show best 10 designs",
            "Explain Mindset 1",
        ],
        "actions": [
            AssistantAction(type="open_view", label="Open detail analysis", payload={"view": "detail"}).model_dump(),
            AssistantAction(
                type="set_metric",
                label=f"Use {METRIC_LABELS.get(metric, metric)}",
                payload={"metric": METRIC_LABELS.get(metric, metric)},
            ).model_dump(),
        ],
        "follow_up_context": AssistantFollowUpContext(
            metric=AssistantMetric(metric),
            segment_section=plan.segment_section,
            segment_key=plan.segment_key,
            last_tool=AssistantToolName.rank_elements,
            last_direction=RankDirection(direction),
            last_limit=limit,
        ).model_dump(),
    }


def _compact_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _hint_looks_specific(hint: str, hint_norm: str) -> bool:
    """Hyphenated / coded names should force one element, not a color pool."""
    raw = str(hint or "").strip()
    if not raw:
        return False
    if "-" in raw or "_" in raw:
        return True
    if re.search(r"[a-z]+\d+|\d+[a-z]+", hint_norm):
        return True
    # Multi-word phrases like "holographic tick"
    if " " in raw and len(hint_norm) >= 8:
        return True
    return False


def _resolve_must_include(
    categories: List[Any],
    must_include: List[str],
) -> Tuple[Dict[str, str], List[str], List[str], List[str], List[str]]:
    """
    Map user ingredient hints to forced category selections / require-any pools.

    Returns:
      forced_by_category, require_any_ids, matched_labels, unresolved_hints,
      forced_element_ids
    """
    forced_by_category: Dict[str, str] = {}
    require_any_ids: List[str] = []
    matched_labels: List[str] = []
    unresolved: List[str] = []
    forced_element_ids: List[str] = []

    all_elements: List[Any] = []
    for category in categories:
        all_elements.extend(category.elements)

    attribute_hints = {
        "white",
        "black",
        "red",
        "blue",
        "green",
        "yellow",
        "orange",
        "pink",
        "purple",
        "grey",
        "gray",
        "transparent",
        "transp",
        "opaque",
        "silver",
        "gold",
    }

    for hint in must_include or []:
        hint_norm = _compact_token(hint)
        if not hint_norm:
            continue
        scored: List[Tuple[int, Any]] = []
        for element in all_elements:
            name_norm = _compact_token(element.name)
            code_norm = _compact_token(element.code or "")
            id_norm = _compact_token(getattr(element, "element_id", "") or "")
            score = 0
            if hint_norm in {name_norm, code_norm, id_norm} and hint_norm:
                score = 100
            elif hint_norm and (
                hint_norm in name_norm
                or (code_norm and hint_norm in code_norm)
                or (id_norm and hint_norm in id_norm)
            ):
                # Longer / more specific containment ranks higher.
                score = 80 + min(19, len(hint_norm))
            elif name_norm and name_norm in hint_norm and len(name_norm) >= 4:
                score = 70
            elif (
                len(hint_norm) >= 4
                and not _hint_looks_specific(hint, hint_norm)
                and any(
                    token and token in name_norm
                    for token in re.findall(r"[a-z0-9]{3,}", hint.casefold())
                )
            ):
                # Color / partial token match, e.g. "white" — not for coded names.
                score = 55
            if score:
                scored.append((score, element))
        scored.sort(
            key=lambda item: (
                -item[0],
                0 if hint_norm == _compact_token(item[1].name) else 1,
                -len(_compact_token(item[1].name)),
                -item[1].value,
                item[1].category_name.lower(),
                item[1].name.lower(),
            )
        )
        if not scored:
            unresolved.append(hint)
            continue

        top_score = scored[0][0]
        chosen = scored[0][1]
        looks_like_attribute = hint_norm in attribute_hints or (
            len(hint_norm) <= 5
            and top_score < 100
            and "-" not in hint
            and "_" not in hint
            and " " not in hint
        )
        specific = _hint_looks_specific(hint, hint_norm)

        def _force_chosen() -> None:
            forced_by_category[chosen.category_key] = chosen.element_id
            matched_labels.append(chosen.name)
            if chosen.element_id not in forced_element_ids:
                forced_element_ids.append(chosen.element_id)

        # Specific element request ("A6-silver-clinical", "holographic tick")
        # must force that exact element — never expand to a loose "clinical" pool.
        if specific:
            if top_score >= 80:
                _force_chosen()
            else:
                unresolved.append(hint)
            continue

        # Exact/near-exact unique element name -> force that category.
        top_matches = [el for score, el in scored if score >= top_score and score >= 80]
        if (
            not looks_like_attribute
            and top_score >= 80
            and (len({el.element_id for el in top_matches[:3]}) == 1 or top_score >= 95)
        ):
            _force_chosen()
            continue

        # Broader attribute match (e.g. "white") -> require any matching element.
        pool = [el for score, el in scored if score >= max(55, top_score - 10)][:40]
        if not pool:
            unresolved.append(hint)
            continue
        require_any_ids.extend(el.element_id for el in pool)
        matched_labels.append(hint)
    require_any_ids = list(dict.fromkeys(require_any_ids))
    return forced_by_category, require_any_ids, matched_labels, unresolved, forced_element_ids


def _design_satisfies_must_include(
    design: Any,
    forced_by_category: Dict[str, str],
    require_any_ids: List[str],
) -> bool:
    """Ensure ranked designs actually contain the forced / required elements."""
    selected_ids = {el.element_id for el in design.elements}
    if forced_by_category:
        return all(element_id in selected_ids for element_id in forced_by_category.values())
    if require_any_ids:
        required = set(require_any_ids)
        return any(element_id in required for element_id in selected_ids)
    return True


def tool_rank_designs(
    analysis: Dict[str, Any],
    study_obj: Study,
    plan: AssistantQueryPlan,
) -> Dict[str, Any]:
    metric = metric_prefix((plan.metric or AssistantMetric.T).value)
    direction = (plan.direction or RankDirection.highest).value
    limit = min(plan.limit or 10, settings.ASSISTANT_MAX_RESULT_LIMIT)
    study_type = str(study_obj.study_type or "grid").lower()
    plan.segment_key = _canonical_segment_key(
        analysis, metric, plan.segment_section, plan.segment_key
    )
    if plan.segment_key:
        available = _available_segment_keys(analysis, metric, plan.segment_section)
        if plan.segment_key not in available:
            return _missing_segment_result(plan.segment_key, available)
    categories = build_categories_from_analysis(
        analysis,
        metric=metric,
        segment_section=plan.segment_section,
        segment_key=plan.segment_key,
        study_type=study_type,
    )
    categories = enrich_categories_from_study_layers(categories, study_obj)
    forced_by_category, require_any_ids, matched_labels, unresolved, forced_element_ids = _resolve_must_include(
        categories, list(plan.must_include or [])
    )
    if unresolved and not forced_by_category and not require_any_ids:
        available_names = sorted(
            {
                el.name
                for category in categories
                for el in category.elements
            }
        )[:12]
        return {
            "status": "needs_clarification",
            "answer_text": (
                "I could not match "
                + ", ".join(f"“{item}”" for item in unresolved)
                + " to an element in this study. Pick a closer element name."
            ),
            "clarification_options": available_names,
            "blocks": [],
            "evidence": [],
            "follow_ups": ["Show the best design overall", "Show the strongest elements"],
            "actions": [],
        }

    raw_constraints = (
        study_obj.design_constraints if isinstance(study_obj.design_constraints, list) else []
    )
    constraints = canonicalize_design_constraints(
        raw_constraints,
        layers=list(getattr(study_obj, "layers", None) or []),
    )
    designs, meta = rank_designs(
        categories,
        study_type=study_type,
        direction=direction,
        limit=limit,
        design_constraints=constraints,
        require_all_layers=True,
        timeout_ms=settings.ASSISTANT_OPTIMIZER_TIMEOUT_MS,
        forced_by_category=forced_by_category,
        require_any_element_ids=require_any_ids,
    )
    conflict_pairs = build_conflict_pair_set(constraints)
    meta["constraint_count"] = len(constraints)
    meta["conflict_pair_count"] = len(conflict_pairs) // 2
    meta["must_include"] = matched_labels
    meta["must_include_unresolved"] = unresolved
    verified_designs: List[DesignRankItem] = []
    evidence: List[EvidenceFact] = []
    for design in designs:
        ok, errors = verify_design(
            design,
            conflict_pairs,
            require_all_layers=(study_type == "layer"),
            layer_count=len(categories),
        )
        if not ok:
            continue
        if not _design_satisfies_must_include(design, forced_by_category, require_any_ids):
            continue
        fact_id = f"D{design.rank}"
        snapshots = [
            DesignElementSnapshot(
                element_id=el.element_id,
                category_key=el.category_key,
                category_name=el.category_name,
                name=el.name,
                value=el.value,
                code=el.code,
                image_url=el.image_url,
                element_type=el.element_type,
                z_index=el.z_index,
                layer_id=el.layer_id,
                image_id=el.image_id,
                transform=el.transform,
            )
            for el in design.elements
        ]
        # Ensure layer stack order for UI
        snapshots.sort(key=lambda s: (s.z_index, s.category_name.lower(), s.name.lower()))
        verified_designs.append(
            DesignRankItem(
                rank=design.rank,
                score=design.score,
                selection_count=len(snapshots),
                selected_by_category=design.selected_by_category,
                elements=snapshots,
                fact_id=fact_id,
                constraints_applied=design.constraints_applied,
                complete_layers=design.complete_layers,
            )
        )
        evidence.append(
            _fact(
                fact_id,
                f"Design #{design.rank}",
                design.score,
                elements=[s.name for s in snapshots],
                verified=True,
                verify_errors=errors,
            )
        )

    info = analysis.get("Information Block") or {}
    background_url = info.get("Study Background") or info.get("background_image_url")
    aspect_ratio = info.get("Aspect Ratio") or "9 / 16"

    if not verified_designs:
        reason = "No valid designs were found"
        if matched_labels:
            reason += " that include " + ", ".join(f"“{item}”" for item in matched_labels)
        if study_type == "layer" and constraints:
            reason += " under the current design constraints with one element from every layer"
        if meta.get("timed_out"):
            reason += " before the optimizer time limit"
        return {
            "status": "answered",
            "answer_text": f"{reason}. Try relaxing the required element/filter or checking that analysis coefficients exist.",
            "blocks": [],
            "evidence": evidence,
            "follow_ups": ["Show the best design overall", "Show top elements"],
            "actions": [],
            "usage": {"optimizer": meta},
        }

    label = "best" if direction == "highest" else "least-performing"
    top = verified_designs[0]
    segment_label = _segment_label(plan.segment_section, plan.segment_key)
    include_phrase = ""
    if matched_labels:
        include_phrase = " that include " + ", ".join(f"“{item}”" for item in matched_labels)
    if len(verified_designs) == 1:
        answer = (
            f"This is the {label} overall design{include_phrase} for {METRIC_LABELS.get(metric, metric)} "
            f"({segment_label}). "
        )
    else:
        answer = (
            f"These are the {len(verified_designs)} {label} designs{include_phrase} for "
            f"{METRIC_LABELS.get(metric, metric)} ({segment_label}). "
        )
    if study_type == "layer":
        if conflict_pairs:
            answer += (
                f"It uses one element from every layer, respects {len(constraints)} design "
                "constraint rule(s), and is stacked by z-index. "
            )
        else:
            answer += "It uses one element from every layer and is stacked by z-index. "
    elif len(top.elements) > 1:
        answer += f"It combines the best valid set of {len(top.elements)} elements used by the configurator. "
    answer += f"Its total coefficient is {top.score} [{top.fact_id}]."

    blocks = [
        AssistantBlock(
            type="top_k_designs",
            title=f"{label.title()} design{'' if len(verified_designs) == 1 else 's'}",
            data={
                "direction": direction,
                "metric": METRIC_LABELS.get(metric, metric),
                "study_type": study_type,
                "background_url": background_url,
                "aspect_ratio": aspect_ratio,
                "constraints_applied": bool(conflict_pairs) if study_type == "layer" else False,
                "constraint_count": len(constraints),
                "must_include": matched_labels,
                "complete_layers": study_type == "layer",
                "designs": [d.model_dump() for d in verified_designs],
                "optimizer": meta,
            },
        )
    ]
    actions = [
        AssistantAction(
            type="open_configurator",
            label="Open top design in configurator",
            payload={
                "view": "configurator",
                "design": verified_designs[0].model_dump(),
                "metric": METRIC_LABELS.get(metric, metric),
                "segment_label": _segment_label(plan.segment_section, plan.segment_key),
            },
        ).model_dump(),
        AssistantAction(
            type="save_design",
            label="Save top design",
            payload={
                "design": verified_designs[0].model_dump(),
                "metric": METRIC_LABELS.get(metric, metric),
                "segment_label": _segment_label(plan.segment_section, plan.segment_key),
            },
        ).model_dump(),
    ]
    if len(verified_designs) >= 2:
        actions.append(
            AssistantAction(
                type="compare_designs",
                label="Compare top designs",
                payload={"designs": [d.model_dump() for d in verified_designs[:4]]},
            ).model_dump()
        )

    return {
        "answer_text": answer,
        "blocks": [b.model_dump() for b in blocks],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": [
            "Show the opposite ranking",
            "Show top elements instead",
            "Save the best design",
        ],
        "actions": actions,
        "follow_up_context": AssistantFollowUpContext(
            metric=AssistantMetric(metric),
            segment_section=plan.segment_section,
            segment_key=plan.segment_key,
            last_tool=AssistantToolName.rank_designs,
            last_direction=RankDirection(direction),
            last_limit=limit,
        ).model_dump(),
        "usage": {"optimizer": meta},
    }


def tool_explain_design(
    analysis: Dict[str, Any],
    study_obj: Study,
    plan: AssistantQueryPlan,
) -> Dict[str, Any]:
    """Explain the best design using verified contribution deltas."""
    rank_plan = plan.model_copy(
        update={
            "tool": AssistantToolName.rank_designs,
            "direction": RankDirection.highest,
            "limit": 2,
        }
    )
    ranked_result = tool_rank_designs(analysis, study_obj, rank_plan)
    design_block = next(
        (block for block in ranked_result.get("blocks", []) if block.get("type") == "top_k_designs"),
        None,
    )
    designs = ((design_block or {}).get("data") or {}).get("designs") or []
    if not designs:
        return ranked_result

    best = designs[0]
    runner_up = designs[1] if len(designs) > 1 else None
    contributions = sorted(
        [
            {
                "name": element.get("name"),
                "category": element.get("category_name"),
                "value": float(element.get("value") or 0),
            }
            for element in best.get("elements") or []
        ],
        key=lambda item: (-item["value"], str(item["category"]), str(item["name"])),
    )
    delta = (
        round(float(best.get("score") or 0) - float(runner_up.get("score") or 0), 4)
        if runner_up
        else None
    )
    strongest = contributions[0] if contributions else None
    answer = f"This design is best because its verified element coefficients sum to {best.get('score')} [D1]."
    if strongest:
        answer += (
            f" Its strongest contribution is “{strongest['name']}” from "
            f"{strongest['category']} ({strongest['value']})."
        )
    if runner_up and delta is not None:
        answer += f" It leads the next valid design by {delta} coefficient points."
    if str(study_obj.study_type or "").lower() == "layer":
        answer += " Every layer is present and no selected pair violates a design constraint."

    evidence = ranked_result.get("evidence", [])
    evidence.append(
        _fact(
            "DX1",
            "Best versus runner-up coefficient gap",
            delta if delta is not None else 0,
            best_score=best.get("score"),
            runner_up_score=runner_up.get("score") if runner_up else None,
        ).model_dump()
    )
    return {
        "answer_text": answer,
        "blocks": [
            AssistantBlock(
                type="design_explanation",
                title="Why this design wins",
                data={
                    "best": best,
                    "runner_up": runner_up,
                    "delta": delta,
                    "contributions": contributions,
                    "study_type": str(study_obj.study_type or "grid").lower(),
                    "background_url": ((design_block or {}).get("data") or {}).get("background_url"),
                    "aspect_ratio": ((design_block or {}).get("data") or {}).get("aspect_ratio"),
                },
            ).model_dump()
        ],
        "evidence": evidence,
        "follow_ups": ["Show the best design", "Show the runner-up design"],
        "actions": ranked_result.get("actions", []),
        "follow_up_context": AssistantFollowUpContext(
            metric=plan.metric or AssistantMetric.T,
            segment_section=plan.segment_section,
            segment_key=plan.segment_key,
            last_tool=AssistantToolName.explain_design,
            last_direction=RankDirection.highest,
            last_limit=2,
        ).model_dump(),
        "usage": ranked_result.get("usage", {}),
    }


def merge_assistant_filters(
    base: Optional[Dict[str, Any]],
    extra: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge analytics filter dicts (age, gender, classification)."""
    merged: Dict[str, Any] = dict(base or {})
    if not extra:
        return merged
    for key in ("age_groups", "genders"):
        vals = extra.get(key)
        if vals:
            merged[key] = list(vals)
    extra_class = extra.get("classification_filters") or {}
    if extra_class:
        current = dict(merged.get("classification_filters") or {})
        for q_text, answers in extra_class.items():
            if not answers:
                continue
            current[str(q_text)] = [str(a) for a in answers]
        merged["classification_filters"] = current
    return merged


def build_cohort_filter_payload(
    study_obj: Study,
    plan: AssistantQueryPlan,
    *,
    option_labels: Optional[List[str]] = None,
    gender_key: Optional[str] = None,
    age_key: Optional[str] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str], List[str]]:
    """
    Resolve classification cohort filters for ranking/compare.

    Returns (question_text, filters_dict, clarify_prompt, clarify_options).
    """
    options = [
        str(item).strip()
        for item in (option_labels or plan.classification_options or [])
        if str(item or "").strip()
    ][:8]
    gender = _normalize_gender_label(gender_key or plan.gender_key)
    age = resolve_age_segment_key(age_key or plan.age_key)
    if age == "13-17":
        age = "13-18"

    question, candidates = _resolve_classification_question(study_obj, plan.classification_question)
    if question is None and options:
        question, candidates = resolve_classification_question_from_options(study_obj, options)

    if question is None:
        if options and len(candidates) > 1:
            return (
                None,
                None,
                f"“{options[0]}” appears on more than one classification question. Which question do you mean?",
                candidates[:8],
            )
        if options:
            return (
                None,
                None,
                "Which classification question should I use for "
                + ", ".join(f"“{opt}”" for opt in options)
                + "?",
                candidates[:8],
            )
        return None, None, "Which classification answer should I filter to?", candidates[:8]

    if not options and plan.classification_question:
        return None, None, "Which classification answer option should I use?", _extract_option_texts(question.answer_options)[:8]

    if not options:
        return None, None, "Which classification answer should I filter to?", _extract_option_texts(question.answer_options)[:8]

    payload: Dict[str, Any] = {
        "classification_filters": {question.question_text: options[:8]},
    }
    if gender:
        payload["genders"] = [gender]
    if age:
        payload["age_groups"] = [age]
    return question.question_text, payload, None, []


def _strip_compare_verb(side: str) -> str:
    return re.sub(
        r"^(?:please\s+)?(?:compare|show|give(?:\s+me)?|what(?:'s|\s+is)\s+the\s+)?"
        r"(?:the\s+)?(?:difference|differences|gap|contrast)\s+(?:between\s+)?"
        r"|(?:please\s+)?(?:compare|show|give(?:\s+me)?)\s+",
        "",
        (side or "").strip(" ,;:-"),
        flags=re.IGNORECASE,
    ).strip()


def _message_has_both_genders(text: str) -> bool:
    lowered = (text or "").casefold()
    has_male = bool(re.search(r"\b(?:male|males|men|man)\b", lowered))
    has_female = bool(re.search(r"\b(?:female|females|women|woman)\b", lowered))
    return has_male and has_female


def _looks_like_compare_side_pair(left: str, right: str) -> bool:
    """True when an 'and'-split looks like two real compare targets (not prose)."""
    g1, g2 = extract_gender_from_text(left), extract_gender_from_text(right)
    if g1 and g2 and g1 != g2:
        return True
    a1 = extract_age_segment_from_text(left) or resolve_age_segment_key(left)
    a2 = extract_age_segment_from_text(right) or resolve_age_segment_key(right)
    if a1 and a2 and a1 != a2:
        return True
    r1, r2 = parse_design_rank_hint(left), parse_design_rank_hint(right)
    if r1 and r2 and r1 != r2:
        return True
    p1 = left if left in {"best", "worst"} else parse_design_polarity_hint(left)
    p2 = right if right in {"best", "worst"} else parse_design_polarity_hint(right)
    if p1 in {"best", "worst"} and p2 in {"best", "worst"} and p1 != p2:
        return True
    # Soft polarity slang on either side ("bestie", "fave") still counts.
    soft_best = re.compile(
        r"\b(?:best|top|bestie|fave|favorite|favourite|winner|winning)\b",
        flags=re.IGNORECASE,
    )
    soft_worst = re.compile(
        r"\b(?:worst|least|lowest|bottom|loser|losing)\b",
        flags=re.IGNORECASE,
    )
    if soft_best.search(left) and soft_best.search(right) and (g1 or a1) and (g2 or a2):
        return True
    if soft_best.search(left) and soft_worst.search(right):
        return True
    if soft_worst.search(left) and soft_best.search(right):
        return True
    return False


def extract_compare_pair(message: str) -> Optional[Tuple[str, str]]:
    """
    Split free-text into left/right compare targets.

    Supports vs/versus, difference between X and Y, X compared to Y, contrast,
    and careful 'and' splits when both sides look like real targets.
    """
    if not message:
        return None
    text = message.strip()

    # "difference between X and Y" / "differences between X & Y"
    between = re.search(
        r"\b(?:difference|differences|gap|contrast)\s+between\s+(.+?)\s+and\s+(.+?)(?:[?.!]\s*)?$",
        text,
        flags=re.IGNORECASE,
    )
    if between:
        left = between.group(1).strip(" ,;:-")
        right = between.group(2).strip(" ,;:-")
        if left and right:
            return left, right

    # "how does X differ from Y" / "how do X and Y differ"
    differ_from = re.search(
        r"\bhow\s+do(?:es)?\s+(.+?)\s+differ(?:s)?\s+from\s+(.+?)(?:[?.!]\s*)?$",
        text,
        flags=re.IGNORECASE,
    )
    if differ_from:
        left = differ_from.group(1).strip(" ,;:-")
        right = differ_from.group(2).strip(" ,;:-")
        if left and right:
            return left, right

    # "contrast X with Y" / "contrast X and Y"
    contrast = re.search(
        r"\bcontrast\s+(.+?)\s+(?:with|and|vs\.?|versus)\s+(.+?)(?:[?.!]\s*)?$",
        text,
        flags=re.IGNORECASE,
    )
    if contrast:
        left = contrast.group(1).strip(" ,;:-")
        right = contrast.group(2).strip(" ,;:-")
        if left and right:
            return left, right

    # "X compared to Y" / "X compared with Y" / "X relative to Y"
    compared = re.search(
        r"(.+?)\s+(?:compared\s+(?:to|with)|relative\s+to)\s+(.+?)(?:[?.!]\s*)?$",
        text,
        flags=re.IGNORECASE,
    )
    if compared:
        left = _strip_compare_verb(compared.group(1))
        right = compared.group(2).strip(" ,;:-")
        if left and right and _looks_like_compare_side_pair(left, right):
            return left, right

    match = re.search(
        r"(.+?)\s+(?:vs\.?|versus|against)\s+(.+?)(?:[?.!]\s*)?$",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        left = _strip_compare_verb(match.group(1))
        right = match.group(2).strip(" ,;:-")
        if left and right:
            return left, right

    # "compare male best design and female best design"
    # Only accept 'and' when both sides look like real compare targets.
    and_match = re.search(
        r"(.+?)\s+\band\b\s+(.+?)(?:[?.!]\s*)?$",
        text,
        flags=re.IGNORECASE,
    )
    if and_match:
        left = _strip_compare_verb(and_match.group(1))
        right = and_match.group(2).strip(" ,;:-")
        if left and right and _looks_like_compare_side_pair(left, right):
            return left, right
    return None


def parse_design_rank_hint(text: str) -> Optional[int]:
    match = re.search(r"\bdesign\s*#?\s*(\d+)\b", text or "", flags=re.IGNORECASE)
    if not match:
        return None
    try:
        rank = int(match.group(1))
    except ValueError:
        return None
    return rank if 1 <= rank <= 20 else None


_WORD_COUNTS_COMPARE = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_BEST_POLARITY_RE = re.compile(
    r"\b(?:best|bestie|top|highest|leading|strongest|winning|fave|favorite|favourite)\b",
    flags=re.IGNORECASE,
)
_WORST_POLARITY_RE = re.compile(
    r"\b(?:worst|least(?:[\s-]+performing)?|lowest|bottom|underperform(?:ing)?|losing)\b",
    flags=re.IGNORECASE,
)


def parse_design_polarity_hint(text: str) -> Optional[str]:
    """Return 'best' or 'worst' when a side clearly asks for polarity ranking."""
    if not text:
        return None
    has_worst = bool(_WORST_POLARITY_RE.search(text))
    has_best = bool(_BEST_POLARITY_RE.search(text))
    if has_worst and not has_best:
        return "worst"
    if has_best and not has_worst:
        return "best"
    return None


def is_best_vs_worst_design_query(message: str) -> bool:
    """Detect 'compare top/best and worst design' / 'best vs worst' style asks."""
    if not message:
        return False
    text = message.casefold()
    # "top" / "highest" are common synonyms for best in user phrasing.
    has_best = bool(_BEST_POLARITY_RE.search(text))
    has_worst = bool(_WORST_POLARITY_RE.search(text))
    if not (has_best and has_worst):
        return False
    # "top two designs" is a ranked pair, not best-vs-worst polarity.
    if re.search(
        r"\b(?:top|best|highest|leading)\s+(?:\d{1,2}|"
        + "|".join(_WORD_COUNTS_COMPARE.keys())
        + r")\b",
        text,
    ):
        return False
    has_design = bool(re.search(r"\b(?:design|mix|combination|package|stack|creative)s?\b", text))
    has_compare = bool(
        re.search(
            r"\b(?:compare|vs|versus|against|and|difference|differences|contrast|differ)\b",
            text,
        )
    )
    # "compare best and worst" / "difference between best and worst" is still valid.
    return has_design or has_compare


def is_compare_top_designs_query(message: str) -> bool:
    """
    True only for an explicit ranked-pair ask: compare the top/best N designs
    or Design #1 vs #2.

    Intentionally narrow — “top 10 designs” alone is rank_designs, not compare.
    Free paraphrases like “difference between male best and female best” must
    NOT collapse to Design #1 vs #2.
    """
    if not message:
        return False
    text = message.casefold()
    if is_best_vs_worst_design_query(text):
        return False
    # Male best vs Female best (any phrasing) is a segment compare.
    if _message_has_both_genders(text):
        return False
    ages = re.findall(r"\b(\d{1,2}\s*[-–to]{1,3}\s*\d{1,2})\b", text)
    if len({resolve_age_segment_key(a) or a for a in ages}) >= 2:
        return False
    if not re.search(r"\b(?:design|mix|combination|package|stack|creative)s?\b", text):
        return False

    # Design #1 vs #2 is always a design-pair compare.
    if re.search(r"\bdesign\s*#?\s*1\b.+\bdesign\s*#?\s*2\b", text):
        return True

    # Must ask to compare/contrast — not merely “show top 10 designs”.
    has_compare_verb = bool(
        re.search(
            r"\b(?:compare|comparison|difference\s+between|differences\s+between|"
            r"contrast|side[\s-]?by[\s-]?side)\b",
            text,
        )
    )
    if not has_compare_verb:
        return False

    # Explicit count: "compare top two designs", "compare the best 2 mixes".
    if re.search(
        r"\b(?:top|best|highest|leading)\s+(?:\d{1,2}|"
        + "|".join(_WORD_COUNTS_COMPARE.keys())
        + r")\s+(?:design|mix|combination|package|stack|creative)s?\b",
        text,
    ):
        return True
    # "compare the top designs" / "compare the best designs" with no other sides.
    if re.search(
        r"\b(?:compare|difference\s+between|differences\s+between)\s+(?:the\s+)?"
        r"(?:top|best|leading)\s+(?:design|mix|combination)s?\b",
        text,
    ) and not extract_compare_pair(message):
        return True
    return False


def is_compare_intent(message: str) -> bool:
    """
    True when the user is asking for a side-by-side / difference — not only the
    word “compare”. Covers client paraphrases like “difference between…”.
    """
    if not message:
        return False
    text = message.casefold()
    if re.search(
        r"\b(?:compare|comparison|versus|vs\.?|against|contrast|"
        r"differ(?:ence|ences)?|difference\s+between|differences\s+between|"
        r"compared\s+to|relative\s+to|side[\s-]?by[\s-]?side)\b",
        text,
    ):
        return True
    if re.search(r"\bhow\s+do(?:es)?\b.+\bdiffer\b", text):
        return True
    if extract_compare_pair(message):
        return True
    if is_best_vs_worst_design_query(text) or is_compare_top_designs_query(text):
        return True
    return False


def infer_compare_mode(
    message: str,
    study_obj: Study,
    plan: AssistantQueryPlan,
) -> Tuple[Optional[CompareMode], Optional[str], Optional[str]]:
    """Infer pairwise compare mode and targets from message + plan."""
    left = plan.compare_left
    right = plan.compare_right
    if not left or not right:
        pair = extract_compare_pair(message)
        if pair:
            left, right = pair

    # "compare top two designs" → side-by-side of ranked Design #1 vs #2.
    if is_compare_top_designs_query(message):
        return CompareMode.design, "design 1", "design 2"

    # Whole-message best vs worst (works with "and" as well as "vs").
    if is_best_vs_worst_design_query(message):
        # Prefer polarity over segment/option when both best and worst are present.
        # Exception: "best design for males vs females" is a segment ask, not best/worst.
        segment_pair = bool(
            left
            and right
            and (
                (extract_gender_from_text(left) and extract_gender_from_text(right))
                or (
                    (extract_age_segment_from_text(left) or resolve_age_segment_key(left))
                    and (extract_age_segment_from_text(right) or resolve_age_segment_key(right))
                )
            )
        )
        if not segment_pair:
            return CompareMode.design, "best", "worst"

    if not left or not right:
        return None, None, None

    left_rank = parse_design_rank_hint(left)
    right_rank = parse_design_rank_hint(right)
    if left_rank and right_rank:
        return CompareMode.design, str(left_rank), str(right_rank)

    if (
        extract_gender_from_text(left)
        or extract_gender_from_text(right)
        or extract_age_segment_from_text(left)
        or extract_age_segment_from_text(right)
        or resolve_age_segment_key(left)
        or resolve_age_segment_key(right)
        or re.search(r"mindset\s*\d", left, flags=re.IGNORECASE)
        or re.search(r"mindset\s*\d", right, flags=re.IGNORECASE)
    ):
        return CompareMode.segment, left, right

    left_opts = match_classification_options_in_text(study_obj, left)
    right_opts = match_classification_options_in_text(study_obj, right)
    if not left_opts:
        hinted = resolve_classification_option_hint(study_obj, left)
        if hinted:
            left_opts = [hinted]
    if not right_opts:
        hinted = resolve_classification_option_hint(study_obj, right)
        if hinted:
            right_opts = [hinted]
    if left_opts and right_opts:
        return CompareMode.classification, left_opts[0], right_opts[0]
    if left_opts or right_opts:
        return CompareMode.classification, left_opts[0] if left_opts else left, right_opts[0] if right_opts else right

    left_polarity = left if left in {"best", "worst"} else parse_design_polarity_hint(left)
    right_polarity = right if right in {"best", "worst"} else parse_design_polarity_hint(right)
    if (
        left_polarity in {"best", "worst"}
        and right_polarity in {"best", "worst"}
        and left_polarity != right_polarity
    ):
        return CompareMode.design, "best", "worst"

    if left_rank or right_rank:
        return CompareMode.design, str(left_rank or 1), str(right_rank or 2)

    return CompareMode.segment, left, right


def _top_design_from_rank_result(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    block = next((b for b in result.get("blocks", []) if b.get("type") == "top_k_designs"), None)
    designs = ((block or {}).get("data") or {}).get("designs") or []
    return designs[0] if designs else None


def _design_at_rank_from_result(result: Dict[str, Any], rank_num: int) -> Optional[Dict[str, Any]]:
    block = next((b for b in result.get("blocks", []) if b.get("type") == "top_k_designs"), None)
    designs = ((block or {}).get("data") or {}).get("designs") or []
    for design in designs:
        if int(design.get("rank") or 0) == rank_num:
            return design
    if 1 <= rank_num <= len(designs):
        return designs[rank_num - 1]
    return None


def _top_element_from_rank_result(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    block = next((b for b in result.get("blocks", []) if b.get("type") == "top_bottom_elements"), None)
    items = ((block or {}).get("data") or {}).get("items") or []
    return items[0] if items else None


def _resolve_segment_side(
    analysis: Dict[str, Any],
    metric: str,
    side_text: str,
) -> Tuple[Optional[str], Optional[str], str]:
    """Map free text to (segment_section, segment_key, label)."""
    label = side_text.strip()
    gender = extract_gender_from_text(side_text)
    age = extract_age_segment_from_text(side_text) or resolve_age_segment_key(side_text)
    if gender and age:
        canon = _canonical_segment_key(analysis, metric, "Gender", gender) or gender
        return "Gender", canon, f"{gender} · {age}"
    if gender:
        canon = _canonical_segment_key(analysis, metric, "Gender", gender) or gender
        return "Gender", canon, gender
    if age:
        canon = _canonical_segment_key(analysis, metric, "Age", age) or age
        return "Age", canon, age
    mindset = re.search(r"mindset\s*(\d+)", side_text, flags=re.IGNORECASE)
    if mindset:
        key = f"Mindset {mindset.group(1)}"
        canon = _canonical_segment_key(analysis, metric, "Mindsets", key) or key
        return "Mindsets", canon, canon
    canon = _canonical_segment_key(analysis, metric, "Age", side_text)
    if canon:
        return "Age", canon, canon
    canon = _canonical_segment_key(analysis, metric, "Gender", side_text)
    if canon:
        return "Gender", canon, canon
    return None, None, label


def _compare_side_snapshot(
    analysis: Dict[str, Any],
    study_obj: Study,
    plan: AssistantQueryPlan,
    *,
    label: str,
    segment_section: Optional[str] = None,
    segment_key: Optional[str] = None,
    gender_key: Optional[str] = None,
    age_key: Optional[str] = None,
) -> Dict[str, Any]:
    metric = metric_prefix((plan.metric or AssistantMetric.T).value)
    side_plan = plan.model_copy(
        update={
            "segment_section": segment_section,
            "segment_key": segment_key,
            "gender_key": gender_key,
            "age_key": age_key,
            "limit": 1,
            "direction": RankDirection.highest,
        }
    )
    design_result = tool_rank_designs(analysis, study_obj, side_plan)
    element_result = tool_rank_elements(analysis, study_obj, side_plan)
    top_design = _top_design_from_rank_result(design_result)
    top_element = _top_element_from_rank_result(element_result)
    return {
        "label": label,
        "segment_section": segment_section,
        "segment_key": segment_key,
        "gender_key": gender_key,
        "age_key": age_key,
        "top_design": top_design,
        "top_element": top_element,
        "design_answer": design_result.get("answer_text"),
        "element_answer": element_result.get("answer_text"),
        "available": bool(top_design or top_element),
    }


def _biggest_gender_element_gap(analysis: Dict[str, Any], metric: str) -> Optional[Dict[str, Any]]:
    section = analysis.get(section_key_for(metric, "Gender")) or {}
    categories = section.get("categories") or []
    if not categories:
        return None
    best_gap = 0.0
    best_row: Optional[Dict[str, Any]] = None
    for category in categories:
        for element in category.get("elements") or []:
            values = element.get("values") or {}
            if not isinstance(values, dict):
                continue
            male_raw = values.get("Male")
            female_raw = values.get("Female")
            if male_raw is None or female_raw is None:
                continue
            male_val = float(male_raw.get("value") if isinstance(male_raw, dict) else male_raw or 0)
            female_val = float(female_raw.get("value") if isinstance(female_raw, dict) else female_raw or 0)
            gap = round(abs(male_val - female_val), 4)
            if gap >= best_gap:
                best_gap = gap
                leader = "Male" if male_val >= female_val else "Female"
                best_row = {
                    "element": element.get("name"),
                    "category": category.get("name"),
                    "male": male_val,
                    "female": female_val,
                    "gap": gap,
                    "leader": leader,
                }
    return best_row


def _biggest_age_element_gap(analysis: Dict[str, Any], metric: str) -> Optional[Dict[str, Any]]:
    section = analysis.get(section_key_for(metric, "Age")) or {}
    categories = section.get("categories") or []
    segments = list((section.get("segments") or {}).keys())
    if len(segments) < 2 or not categories:
        return None
    seg_a, seg_b = segments[0], segments[1]
    best_gap = 0.0
    best_row: Optional[Dict[str, Any]] = None
    for category in categories:
        for element in category.get("elements") or []:
            values = element.get("values") or {}
            if not isinstance(values, dict):
                continue
            raw_a = values.get(seg_a)
            raw_b = values.get(seg_b)
            if raw_a is None or raw_b is None:
                continue
            val_a = float(raw_a.get("value") if isinstance(raw_a, dict) else raw_a or 0)
            val_b = float(raw_b.get("value") if isinstance(raw_b, dict) else raw_b or 0)
            gap = round(abs(val_a - val_b), 4)
            if gap >= best_gap:
                best_gap = gap
                leader = seg_a if val_a >= val_b else seg_b
                best_row = {
                    "element": element.get("name"),
                    "category": category.get("name"),
                    "left_segment": seg_a,
                    "right_segment": seg_b,
                    "left_value": val_a,
                    "right_value": val_b,
                    "gap": gap,
                    "leader": leader,
                }
    return best_row


def _top_classification_split(db: Session, study_obj: Study) -> Optional[Dict[str, Any]]:
    questions = _classification_questions(study_obj)
    if not questions:
        return None
    question = questions[0]
    labels, lookup = _option_answer_maps(question.answer_options)
    if not labels:
        return None
    response_ids = _filter_completed_responses_by_demographics(db, study_obj.id, None, None)
    if not response_ids:
        return None
    rows = db.execute(
        select(ClassificationAnswer.study_response_id, ClassificationAnswer.answer).where(
            ClassificationAnswer.study_response_id.in_(response_ids),
            ClassificationAnswer.question_text == question.question_text,
        )
    ).all()
    counts: Dict[str, int] = {label: 0 for label in labels}
    answered = 0
    for _rid, raw in rows:
        mapped = _map_classification_answer(str(raw or ""), lookup)
        if mapped in counts:
            counts[mapped] += 1
            answered += 1
    if answered <= 0:
        return None
    top_option = max(counts.items(), key=lambda item: (item[1], item[0].casefold()))
    pct = round(100.0 * top_option[1] / answered, 1)
    return {
        "question": question.question_text,
        "option": top_option[0],
        "count": top_option[1],
        "answered": answered,
        "percentage": pct,
    }


def tool_compare(
    db: Session,
    analysis: Dict[str, Any],
    study_obj: Study,
    current_user: User,
    plan: AssistantQueryPlan,
    filters: Optional[Dict[str, Any]],
    message: str,
) -> Dict[str, Any]:
    """Side-by-side compare for two segments, designs, or classification cohorts."""
    metric = metric_prefix((plan.metric or AssistantMetric.T).value)
    mode, left, right = infer_compare_mode(message, study_obj, plan)
    if not mode or not left or not right:
        return {
            "status": "needs_clarification",
            "answer_text": "What should I compare side by side? Try “Male vs Female”, “18-24 vs 45-54”, or “Design #1 vs Design #2”.",
            "clarification_options": [
                "Best design for Male vs Female",
                "Compare best vs worst design",
                "Design #1 vs Design #2",
            ],
            "blocks": [],
            "evidence": [],
            "follow_ups": [],
            "actions": [],
        }

    evidence: List[EvidenceFact] = []
    sides: List[Dict[str, Any]] = []

    if mode == CompareMode.design:
        left_polarity = left if left in {"best", "worst"} else parse_design_polarity_hint(left)
        right_polarity = right if right in {"best", "worst"} else parse_design_polarity_hint(right)
        use_best_worst = (
            left_polarity in {"best", "worst"}
            and right_polarity in {"best", "worst"}
            and left_polarity != right_polarity
        ) or (left == "best" and right == "worst")

        if use_best_worst:
            best_plan = plan.model_copy(
                update={"limit": 1, "direction": RankDirection.highest, "must_include": []}
            )
            worst_plan = plan.model_copy(
                update={"limit": 1, "direction": RankDirection.lowest, "must_include": []}
            )
            best_ranked = tool_rank_designs(analysis, study_obj, best_plan)
            worst_ranked = tool_rank_designs(analysis, study_obj, worst_plan)
            design_left = _top_design_from_rank_result(best_ranked)
            design_right = _top_design_from_rank_result(worst_ranked)
            if not design_left or not design_right:
                return {
                    "status": "answered",
                    "answer_text": (
                        "I could not find both a best and worst verified design to compare. "
                        "Make sure analysis has enough valid designs."
                    ),
                    "blocks": [],
                    "evidence": (best_ranked.get("evidence") or []) + (worst_ranked.get("evidence") or []),
                    "follow_ups": ["Show best 10 designs", "Show the least-performing designs"],
                    "actions": [],
                }
            # Relabel for UI clarity (rank #1 appears on both highest/lowest lists).
            design_left = {**design_left, "rank": 1}
            design_right = {**design_right, "rank": 1}
            delta = round(float(design_left.get("score") or 0) - float(design_right.get("score") or 0), 4)
            evidence.extend(
                [
                    _fact("C1", "Best design", design_left.get("score")),
                    _fact("C2", "Worst design", design_right.get("score")),
                    _fact("C3", "Best–worst score gap", abs(delta)),
                ]
            )
            sides = [
                {"label": "Best design", "top_design": design_left, "polarity": "best"},
                {"label": "Worst design", "top_design": design_right, "polarity": "worst"},
            ]
            answer = (
                f"The best design scores {design_left.get('score')} vs "
                f"{design_right.get('score')} for the worst design "
                f"(gap {abs(delta)}) [C3]."
            )
            strong_names = {el.get("name") for el in design_left.get("elements") or []}
            weak_names = {el.get("name") for el in design_right.get("elements") or []}
            unique_best = sorted(n for n in (strong_names - weak_names) if n)
            unique_worst = sorted(n for n in (weak_names - strong_names) if n)
            if unique_best:
                answer += f" The best side uniquely includes “{unique_best[0]}”."
            if unique_worst:
                answer += f" The worst side uniquely includes “{unique_worst[0]}”."
        else:
            left_rank = parse_design_rank_hint(left) or 1
            right_rank = parse_design_rank_hint(right) or 2
            rank_plan = plan.model_copy(
                update={"limit": max(left_rank, right_rank), "direction": RankDirection.highest}
            )
            ranked = tool_rank_designs(analysis, study_obj, rank_plan)
            design_left = _design_at_rank_from_result(ranked, left_rank)
            design_right = _design_at_rank_from_result(ranked, right_rank)
            if not design_left or not design_right:
                return {
                    "status": "answered",
                    "answer_text": "I could not find both designs to compare. Make sure analysis has enough valid ranked designs.",
                    "blocks": [],
                    "evidence": ranked.get("evidence") or [],
                    "follow_ups": ["Show best 10 designs", "Explain why the top design wins"],
                    "actions": [],
                }
            delta = round(float(design_left.get("score") or 0) - float(design_right.get("score") or 0), 4)
            winner_rank = left_rank if delta >= 0 else right_rank
            evidence.extend(
                [
                    _fact("C1", f"Design #{left_rank}", design_left.get("score")),
                    _fact("C2", f"Design #{right_rank}", design_right.get("score")),
                    _fact("C3", "Score gap", abs(delta)),
                ]
            )
            sides = [
                {"label": f"Design #{left_rank}", "top_design": design_left},
                {"label": f"Design #{right_rank}", "top_design": design_right},
            ]
            answer = (
                f"Design #{winner_rank} leads by {abs(delta)} coefficient points "
                f"({design_left.get('score')} vs {design_right.get('score')}) [C3]."
            )
            if delta != 0:
                stronger = design_left if delta > 0 else design_right
                weaker = design_right if delta > 0 else design_left
                strong_names = {el.get("name") for el in stronger.get("elements") or []}
                weak_names = {el.get("name") for el in weaker.get("elements") or []}
                diff = sorted(strong_names - weak_names)
                if diff:
                    answer += f" Unique lift drivers in the leader include “{diff[0]}”."

    elif mode == CompareMode.classification:
        left_opts = match_classification_options_in_text(study_obj, left) or [
            resolve_classification_option_hint(study_obj, left) or left
        ]
        right_opts = match_classification_options_in_text(study_obj, right) or [
            resolve_classification_option_hint(study_obj, right) or right
        ]
        shared_gender = plan.gender_key or extract_gender_from_text(message)
        shared_age = plan.age_key or extract_age_segment_from_text(message)

        for idx, (side_label, opts) in enumerate(
            ((left, left_opts[:1]), (right, right_opts[:1])),
            start=1,
        ):
            side_plan = plan.model_copy(
                update={
                    "classification_options": opts,
                    "gender_key": shared_gender,
                    "age_key": shared_age,
                }
            )
            question_text, cohort_filters, clarify, options = build_cohort_filter_payload(
                study_obj, side_plan, option_labels=opts, gender_key=shared_gender, age_key=shared_age
            )
            if clarify:
                return {
                    "status": "needs_clarification",
                    "answer_text": clarify,
                    "clarification_options": options[:8],
                    "blocks": [],
                    "evidence": [],
                    "follow_ups": [],
                    "actions": [],
                }
            merged = merge_assistant_filters(filters, cohort_filters)
            # Reuse the already-built base analysis when the cohort filter is a
            # no-op or identical to the request filter — avoids a redundant
            # full analysis rebuild for one side of the comparison.
            if not merged or merged == (filters or {}):
                cohort_analysis = analysis
            else:
                cohort_analysis = load_analysis_for_assistant(
                    db, study_obj, current_user, filters=merged
                )
            meta = cohort_analysis.get("filter_meta") or {}
            panelists = meta.get("panelists_after_filter")
            if panelists == 0:
                sides.append(
                    {
                        "label": opts[0],
                        "available": False,
                        "top_design": None,
                        "filters": cohort_filters,
                    }
                )
                continue
            snap = _compare_side_snapshot(
                cohort_analysis,
                study_obj,
                plan,
                label=opts[0],
            )
            snap["filters"] = cohort_filters
            snap["question"] = question_text
            sides.append(snap)
            if snap.get("top_design"):
                evidence.append(
                    _fact(f"C{idx}", opts[0], snap["top_design"].get("score"), panelists=panelists)
                )

        available_sides = [s for s in sides if s.get("top_design")]
        if len(available_sides) < 2:
            empty = [s.get("label") for s in sides if not s.get("top_design")]
            return {
                "status": "answered",
                "answer_text": (
                    "One or both cohorts have no completed responses after filtering"
                    + (f" ({', '.join(empty)})" if empty else "")
                    + ". Try broader segments or check classification answers."
                ),
                "blocks": [],
                "evidence": [e.model_dump() for e in evidence],
                "follow_ups": ["Show classification answer counts", "Show best design overall"],
                "actions": [],
            }
        score_a = float(available_sides[0]["top_design"].get("score") or 0)
        score_b = float(available_sides[1]["top_design"].get("score") or 0)
        delta = round(score_a - score_b, 4)
        leader = available_sides[0]["label"] if delta >= 0 else available_sides[1]["label"]
        answer = (
            f"Best design for “{available_sides[0]['label']}” scores {score_a} vs "
            f"{score_b} for “{available_sides[1]['label']}”. "
            f"“{leader}” leads by {abs(delta)} points [C3]."
        )
        evidence.append(_fact("C3", "Cohort design gap", abs(delta)))

    else:
        left_section, left_key, left_label = _resolve_segment_side(analysis, metric, left)
        right_section, right_key, right_label = _resolve_segment_side(analysis, metric, right)
        if not left_key or not right_key:
            return {
                "status": "answered",
                "answer_text": "I could not map both sides to available study segments.",
                "blocks": [],
                "evidence": [],
                "follow_ups": ["Compare Male vs Female", "Compare 18-24 vs 45-54"],
                "actions": [],
            }
        left_snap = _compare_side_snapshot(
            analysis, study_obj, plan, label=left_label, segment_section=left_section, segment_key=left_key
        )
        right_snap = _compare_side_snapshot(
            analysis, study_obj, plan, label=right_label, segment_section=right_section, segment_key=right_key
        )
        sides = [left_snap, right_snap]
        if not left_snap.get("available") or not right_snap.get("available"):
            missing = [s["label"] for s in sides if not s.get("available")]
            return {
                "status": "answered",
                "answer_text": f"No verified coefficients are available for: {', '.join(missing)}.",
                "blocks": [],
                "evidence": [],
                "follow_ups": ["Show study overview", "Compare another segment"],
                "actions": [],
            }
        left_design = left_snap["top_design"] or {}
        right_design = right_snap["top_design"] or {}
        left_score = float(left_design.get("score") or 0)
        right_score = float(right_design.get("score") or 0)
        delta = round(left_score - right_score, 4)
        leader = left_label if delta >= 0 else right_label
        left_el = (left_snap.get("top_element") or {}).get("name")
        right_el = (right_snap.get("top_element") or {}).get("name")
        answer = (
            f"For {left_label}, the best design scores {left_score}; for {right_label}, {right_score}. "
            f"{leader} leads by {abs(delta)} points [C3]."
        )
        if left_el and right_el:
            answer += f" Top elements: “{left_el}” vs “{right_el}”."
        evidence.extend(
            [
                _fact("C1", left_label, left_score, top_element=left_el),
                _fact("C2", right_label, right_score, top_element=right_el),
                _fact("C3", "Best-design gap", abs(delta)),
            ]
        )

    actions: List[AssistantAction] = []
    if mode == CompareMode.classification:
        for side in sides:
            cohort = side.get("filters") or {}
            if cohort:
                actions.append(
                    AssistantAction(
                        type="apply_filter",
                        label=f"Filter to {side.get('label')}",
                        payload={"filters": cohort},
                    )
                )

    info = analysis.get("Information Block") or {}
    left_score = float(((sides[0] or {}).get("top_design") or {}).get("score") or 0) if sides else 0.0
    right_score = float(((sides[1] or {}).get("top_design") or {}).get("score") or 0) if len(sides) > 1 else 0.0
    score_gap = round(abs(left_score - right_score), 4)
    leader_label = None
    if sides and len(sides) >= 2 and (sides[0].get("top_design") or sides[1].get("top_design")):
        if left_score >= right_score and sides[0].get("top_design"):
            leader_label = sides[0].get("label")
        elif sides[1].get("top_design"):
            leader_label = sides[1].get("label")

    block = AssistantBlock(
        type="side_by_side_compare",
        title="Side-by-side comparison",
        data={
            "mode": mode.value if mode else "segment",
            "metric": METRIC_LABELS.get(metric, metric),
            "study_type": str(study_obj.study_type or "grid").lower(),
            "background_url": info.get("Study Background") or info.get("background_image_url"),
            "aspect_ratio": info.get("Aspect Ratio") or "9 / 16",
            "left": sides[0] if sides else None,
            "right": sides[1] if len(sides) > 1 else None,
            "sides": sides,
            "score_gap": score_gap,
            "leader_label": leader_label,
            "left_score": left_score,
            "right_score": right_score,
        },
    )
    return {
        "status": "answered",
        "answer_text": answer,
        "blocks": [block.model_dump()],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": [
            "Explain why the leading side wins",
            "Show use / avoid recommendations",
            "Give me an executive summary",
        ],
        "actions": [a.model_dump() for a in actions],
        "follow_up_context": AssistantFollowUpContext(
            metric=plan.metric or AssistantMetric.T,
            last_tool=AssistantToolName.compare,
        ).model_dump(),
    }


def _element_image_url_by_name(analysis: Dict[str, Any], element_name: Optional[str]) -> Optional[str]:
    """Best-effort image URL lookup from Information Block / category elements."""
    if not element_name:
        return None
    target = str(element_name).strip().casefold()
    if not target:
        return None
    info = analysis.get("Information Block") or {}
    categories = info.get("Categories") or []
    if not isinstance(categories, list):
        return None
    for category in categories:
        for element in (category or {}).get("elements") or []:
            name = str(element.get("name") or element.get("code") or "").strip()
            if name.casefold() != target:
                continue
            url = element.get("url") or element.get("image_url")
            if url:
                return str(url)
    return None


def tool_executive_summary(
    db: Session,
    analysis: Dict[str, Any],
    study_obj: Study,
    plan: AssistantQueryPlan,
    context: AppliedContext,
) -> Dict[str, Any]:
    """Deterministic stakeholder summary from existing verified tools."""
    metric = metric_prefix((plan.metric or AssistantMetric.T).value)
    summary = analysis.get("dashboard_summary") or {}
    panelists = int(summary.get("uniquePanelists") or 0)
    bullets: List[Dict[str, Any]] = []
    evidence: List[EvidenceFact] = []
    info = analysis.get("Information Block") or {}
    background_url = info.get("Study Background") or info.get("background_image_url")
    aspect_ratio = info.get("Aspect Ratio")

    if panelists <= 0:
        return {
            "status": "answered",
            "answer_text": "This study does not have completed responses yet, so an executive summary is not available.",
            "blocks": [],
            "evidence": [],
            "follow_ups": ["Show study overview"],
            "actions": [],
        }

    design_plan = plan.model_copy(
        update={"tool": AssistantToolName.rank_designs, "limit": 1, "direction": RankDirection.highest}
    )
    top_design_result = tool_rank_designs(analysis, study_obj, design_plan)
    top_design = _top_design_from_rank_result(top_design_result)
    if top_design:
        design_elements = top_design.get("elements") or []
        first_el_url = None
        for el in design_elements:
            if el.get("image_url"):
                first_el_url = el.get("image_url")
                break
        bullets.append(
            {
                "title": "Top design",
                "text": f"Best overall design scores {top_design.get('score')} coefficient points.",
                "fact_id": "X1",
                "image_url": first_el_url,
                "images": [
                    {"name": el.get("name"), "image_url": el.get("image_url")}
                    for el in design_elements
                    if el.get("image_url")
                ][:8],
                "design": top_design,
            }
        )
        evidence.append(_fact("X1", "Top design score", top_design.get("score")))

    element_plan = plan.model_copy(
        update={"tool": AssistantToolName.rank_elements, "limit": 3, "direction": RankDirection.highest}
    )
    element_result = tool_rank_elements(analysis, study_obj, element_plan)
    top_elements = ((element_result.get("blocks") or [{}])[0].get("data") or {}).get("items") or []
    if top_elements:
        names = ", ".join(f"“{item.get('name')}”" for item in top_elements[:3])
        bullets.append(
            {
                "title": "Top elements",
                "text": f"Strongest lift comes from {names}.",
                "fact_id": "X2",
                "image_url": top_elements[0].get("image_url"),
                "images": [
                    {"name": item.get("name"), "image_url": item.get("image_url"), "value": item.get("value")}
                    for item in top_elements[:3]
                    if item.get("image_url")
                ],
            }
        )
        evidence.append(_fact("X2", "Top element", top_elements[0].get("value"), names=names))

    gender_gap = _biggest_gender_element_gap(analysis, metric)
    if gender_gap and gender_gap.get("gap", 0) > 0:
        gap_url = _element_image_url_by_name(analysis, gender_gap.get("element"))
        bullets.append(
            {
                "title": "Biggest gender gap",
                "text": (
                    f"“{gender_gap['element']}” differs most by gender "
                    f"(Male {gender_gap['male']} vs Female {gender_gap['female']}, gap {gender_gap['gap']})."
                ),
                "fact_id": "X3",
                "image_url": gap_url,
                "images": (
                    [{"name": gender_gap["element"], "image_url": gap_url}] if gap_url else []
                ),
            }
        )
        evidence.append(_fact("X3", "Gender gap", gender_gap["gap"], element=gender_gap["element"]))
    else:
        age_gap = _biggest_age_element_gap(analysis, metric)
        if age_gap and age_gap.get("gap", 0) > 0:
            gap_url = _element_image_url_by_name(analysis, age_gap.get("element"))
            bullets.append(
                {
                    "title": "Biggest age gap",
                    "text": (
                        f"“{age_gap['element']}” differs most between "
                        f"{age_gap['left_segment']} ({age_gap['left_value']}) and "
                        f"{age_gap['right_segment']} ({age_gap['right_value']})."
                    ),
                    "fact_id": "X3",
                    "image_url": gap_url,
                    "images": (
                        [{"name": age_gap["element"], "image_url": gap_url}] if gap_url else []
                    ),
                }
            )
            evidence.append(_fact("X3", "Age gap", age_gap["gap"], element=age_gap["element"]))

    class_split = _top_classification_split(db, study_obj)
    if class_split:
        bullets.append(
            {
                "title": "Classification split",
                "text": (
                    f"On “{class_split['question']}”, "
                    f"“{class_split['option']}” leads at {class_split['percentage']}% "
                    f"({class_split['count']} of {class_split['answered']})."
                ),
                "fact_id": "X4",
            }
        )
        evidence.append(
            _fact("X4", class_split["option"], class_split["percentage"], count=class_split["count"])
        )

    use_plan = plan.model_copy(update={"tool": AssistantToolName.use_avoid_elements})
    use_result = tool_use_avoid(analysis, study_obj, use_plan)
    use_block = next((b for b in use_result.get("blocks", []) if b.get("type") == "use_avoid"), None)
    use_items = ((use_block or {}).get("data") or {}).get("use") or []
    avoid_items = ((use_block or {}).get("data") or {}).get("avoid") or []
    if use_items or avoid_items:
        use_name = use_items[0]["name"] if use_items else None
        avoid_name = avoid_items[0]["name"] if avoid_items else None
        if use_name and avoid_name:
            text = f"Prefer “{use_name}”; avoid “{avoid_name}”."
        elif use_name:
            text = f"Prefer “{use_name}”."
        else:
            text = f"Avoid “{avoid_name}”."
        images = []
        for item in (use_items[:1] + avoid_items[:1]):
            if item.get("image_url"):
                images.append({"name": item.get("name"), "image_url": item.get("image_url")})
        bullets.append(
            {
                "title": "Use / avoid",
                "text": text,
                "fact_id": "X5",
                "image_url": (use_items[0].get("image_url") if use_items else None)
                or (avoid_items[0].get("image_url") if avoid_items else None),
                "images": images,
            }
        )
        evidence.append(_fact("X5", "Use / avoid", use_name or avoid_name))

    bullets = bullets[:5]
    for idx, bullet in enumerate(bullets, start=1):
        bullet["rank"] = idx

    if not bullets:
        return {
            "status": "answered",
            "answer_text": "Not enough verified analytics are available yet to build an executive summary.",
            "blocks": [],
            "evidence": [],
            "follow_ups": ["Show study overview"],
            "actions": [],
        }

    answer_bits = [f"{b['rank']}. {b['text']} [{b['fact_id']}]" for b in bullets]
    answer = "Here are the most important verified findings:\n" + "\n".join(answer_bits)

    return {
        "status": "answered",
        "answer_text": answer,
        "blocks": [
            AssistantBlock(
                type="executive_summary",
                title="Executive summary",
                data={
                    "bullets": bullets,
                    "study_title": context.study_title,
                    "study_type": str(study_obj.study_type or "grid"),
                    "background_url": background_url,
                    "aspect_ratio": aspect_ratio,
                },
            ).model_dump()
        ],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": [
            "Compare Male vs Female",
            "Show best design for a classification segment",
            "Show top 10 elements",
        ],
        "actions": [],
        "follow_up_context": AssistantFollowUpContext(
            metric=plan.metric or AssistantMetric.T,
            last_tool=AssistantToolName.executive_summary,
        ).model_dump(),
    }


def tool_compare_segments(analysis: Dict[str, Any], plan: AssistantQueryPlan) -> Dict[str, Any]:
    metric = metric_prefix((plan.metric or AssistantMetric.T).value)
    section_name = plan.segment_section or "Gender"
    section = analysis.get(section_key_for(metric, section_name)) or {}
    categories = section.get("categories") or []
    segments = section.get("segments") or {}
    if not categories:
        return {
            "answer_text": f"No {section_name} segment coefficients are available yet.",
            "blocks": [],
            "evidence": [],
            "follow_ups": ["Show study overview"],
            "actions": [],
        }

    # For each segment, compute mean of top element values as a compact comparison signal
    segment_keys = list(segments.keys()) if segments else []
    if not segment_keys:
        # Mindsets groups
        groups = section.get("groups") or {}
        for group_name, group in groups.items():
            if isinstance(group, dict):
                segment_keys.extend([k for k in group.keys() if k != "Total"])
        segment_keys = list(dict.fromkeys(segment_keys))

    rows = []
    evidence = []
    for idx, seg in enumerate(segment_keys[:12], start=1):
        values = []
        for category in categories:
            for element in category.get("elements") or []:
                val_map = element.get("values") or {}
                if isinstance(val_map, dict) and seg in val_map:
                    raw = val_map[seg]
                    values.append(float(raw.get("value") if isinstance(raw, dict) else raw or 0))
                elif not val_map and element.get("value") is not None:
                    values.append(float(element.get("value") or 0))
        avg = round(sum(values) / len(values), 2) if values else 0.0
        top = max(values) if values else 0.0
        fact_id = f"S{idx}"
        rows.append({"segment": seg, "avg": avg, "top": top, "count": len(values), "fact_id": fact_id})
        evidence.append(_fact(fact_id, seg, top, avg=avg, elements=len(values)))

    rows.sort(key=lambda r: (-r["top"], r["segment"]))
    answer = (
        f"Compared {len(rows)} {section_name} segments on {METRIC_LABELS.get(metric, metric)}. "
        f"Strongest top-element lift is in “{rows[0]['segment']}” at {rows[0]['top']} [{rows[0]['fact_id']}]."
        if rows
        else f"No comparable {section_name} segments found."
    )
    blocks = [
        AssistantBlock(
            type="segment_comparison",
            title=f"{section_name} comparison",
            data={"rows": rows, "metric": METRIC_LABELS.get(metric, metric)},
        ),
        AssistantBlock(
            type="chart",
            title="Top element by segment",
            data={
                "chart_type": "horizontal_bar",
                "items": [{"name": r["segment"], "value": r["top"], "fact_id": r["fact_id"]} for r in rows],
            },
        ),
    ]
    return {
        "answer_text": answer,
        "blocks": [b.model_dump() for b in blocks],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": ["Show best designs for the top segment", "Show top elements overall"],
        "actions": [],
    }


def tool_use_avoid(analysis: Dict[str, Any], study_obj: Study, plan: AssistantQueryPlan) -> Dict[str, Any]:
    metric = metric_prefix((plan.metric or AssistantMetric.T).value)
    categories = build_categories_from_analysis(
        analysis,
        metric=metric,
        segment_section=plan.segment_section,
        segment_key=plan.segment_key,
        study_type=str(study_obj.study_type or "grid"),
    )
    use = rank_elements(categories, direction="highest", limit=5)
    avoid = rank_elements(categories, direction="lowest", limit=5)
    evidence = []
    use_items = []
    avoid_items = []
    for idx, el in enumerate(use, start=1):
        fact_id = f"U{idx}"
        use_items.append({"name": el.name, "category": el.category_name, "value": el.value, "fact_id": fact_id, "image_url": el.image_url})
        evidence.append(_fact(fact_id, f"Use {el.name}", el.value))
    for idx, el in enumerate(avoid, start=1):
        fact_id = f"A{idx}"
        avoid_items.append({"name": el.name, "category": el.category_name, "value": el.value, "fact_id": fact_id, "image_url": el.image_url})
        evidence.append(_fact(fact_id, f"Avoid {el.name}", el.value))
    answer = "Use the highest-lift elements and avoid the lowest-lift ones for this segment."
    if use_items:
        answer = f"Prefer “{use_items[0]['name']}” [{use_items[0]['fact_id']}]"
    if avoid_items:
        answer += f" and avoid “{avoid_items[0]['name']}” [{avoid_items[0]['fact_id']}]."
    blocks = [
        AssistantBlock(type="use_avoid", title="Use / avoid", data={"use": use_items, "avoid": avoid_items})
    ]
    return {
        "answer_text": answer,
        "blocks": [b.model_dump() for b in blocks],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": ["Build the best design from use elements", "Show mindset explanation"],
        "actions": [AssistantAction(type="open_configurator", label="Open configurator", payload={"view": "configurator"}).model_dump()],
    }


def tool_response_time_summary(analysis: Dict[str, Any]) -> Dict[str, Any]:
    summary = analysis.get("dashboard_summary") or {}
    dist = summary.get("responseTimeDistribution") or []
    by_task = summary.get("responseTimeByTask") or []
    evidence = [
        _fact("RT1", "Average response time (s)", round(float(summary.get("avgResponseTime") or 0), 2)),
    ]
    for idx, item in enumerate(dist, start=2):
        evidence.append(_fact(f"RT{idx}", item.get("name"), item.get("value")))
    answer = (
        f"Average response time is {round(float(summary.get('avgResponseTime') or 0), 2)}s "
        f"across {summary.get('totalResponses') or 0} responses [RT1]."
    )
    blocks = [
        AssistantBlock(type="chart", title="Response time distribution", data={"chart_type": "donut", "items": dist}),
        AssistantBlock(type="chart", title="Response time by task", data={"chart_type": "bar", "items": [
            {"name": f"Task {i.get('task')}", "value": round(float(i.get('avg') or 0), 2)} for i in by_task[:20]
        ]}),
    ]
    return {
        "answer_text": answer,
        "blocks": [b.model_dump() for b in blocks],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": ["Show fatigue summary", "Show study overview"],
        "actions": [],
    }


def tool_fatigue_summary(analysis: Dict[str, Any]) -> Dict[str, Any]:
    summary = analysis.get("dashboard_summary") or {}
    by_task = summary.get("responseTimeByTask") or []
    if len(by_task) < 2:
        return {
            "answer_text": "Not enough task-level timing data to estimate fatigue.",
            "blocks": [],
            "evidence": [],
            "follow_ups": ["Show response time summary"],
            "actions": [],
        }
    first = by_task[: max(1, len(by_task) // 3)]
    last = by_task[-max(1, len(by_task) // 3) :]
    first_avg = sum(float(i.get("avg") or 0) for i in first) / len(first)
    last_avg = sum(float(i.get("avg") or 0) for i in last) / len(last)
    delta = round(last_avg - first_avg, 2)
    evidence = [
        _fact("FT1", "Early-task avg time (s)", round(first_avg, 2)),
        _fact("FT2", "Late-task avg time (s)", round(last_avg, 2)),
        _fact("FT3", "Late minus early (s)", delta),
    ]
    if delta > 0.25:
        risk = "elevated"
    elif delta > 0.1:
        risk = "moderate"
    else:
        risk = "low"
    answer = (
        f"Fatigue risk looks {risk}: late tasks average {round(last_avg, 2)}s vs early tasks "
        f"{round(first_avg, 2)}s (delta {delta}s) [FT3]."
    )
    return {
        "answer_text": answer,
        "blocks": [AssistantBlock(type="fatigue", title="Fatigue summary", data={"risk": risk, "delta": delta, "early": first_avg, "late": last_avg}).model_dump()],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": ["Show response time summary", "Show top elements"],
        "actions": [],
    }


def tool_explain_mindset(analysis: Dict[str, Any], study_obj: Study, plan: AssistantQueryPlan) -> Dict[str, Any]:
    metric = metric_prefix((plan.metric or AssistantMetric.T).value)
    section = analysis.get(section_key_for(metric, "Mindsets")) or {}
    segment_key = plan.segment_key or "Mindset_1_of_2"
    categories = build_categories_from_analysis(
        analysis,
        metric=metric,
        segment_section="Mindsets",
        segment_key=segment_key,
        study_type=str(study_obj.study_type or "grid"),
    )
    if not categories:
        # Try alternate keys
        for alt in ("Mindset_1_of_3", "Mindset_2_of_2", "Mindset_2_of_3", "Mindset_3_of_3"):
            categories = build_categories_from_analysis(
                analysis, metric=metric, segment_section="Mindsets", segment_key=alt, study_type=str(study_obj.study_type or "grid")
            )
            if categories:
                segment_key = alt
                break
    top = rank_elements(categories, direction="highest", limit=5)
    bottom = rank_elements(categories, direction="lowest", limit=5)
    if not top:
        return {
            "status": "needs_clarification",
            "answer_text": "Which mindset should I explain?",
            "clarification_options": ["Mindset_1_of_2", "Mindset_2_of_2", "Mindset_1_of_3", "Mindset_2_of_3", "Mindset_3_of_3"],
            "blocks": [],
            "evidence": [],
            "follow_ups": [],
            "actions": [],
        }
    evidence = []
    for idx, el in enumerate(top, start=1):
        evidence.append(_fact(f"M{idx}", f"Attracted to {el.name}", el.value))
    for idx, el in enumerate(bottom, start=1):
        evidence.append(_fact(f"N{idx}", f"Less attracted to {el.name}", el.value))
    answer = (
        f"{segment_key.replace('_', ' ')} is most attracted to “{top[0].name}” [{evidence[0].fact_id}] "
        f"and least attracted to “{bottom[0].name}” [{evidence[len(top)].fact_id}]."
    )
    blocks = [
        AssistantBlock(
            type="mindset",
            title=segment_key,
            data={
                "segment_key": segment_key,
                "attracted": [{"name": e.name, "value": e.value, "category": e.category_name} for e in top],
                "avoid": [{"name": e.name, "value": e.value, "category": e.category_name} for e in bottom],
            },
        )
    ]
    return {
        "answer_text": answer,
        "blocks": [b.model_dump() for b in blocks],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": ["Show best designs for this mindset", "Compare mindsets"],
        "actions": [],
        "follow_up_context": AssistantFollowUpContext(
            metric=AssistantMetric(metric),
            segment_section="Mindsets",
            segment_key=segment_key,
            last_tool=AssistantToolName.explain_mindset,
        ).model_dump(),
    }


def tool_list_saved_designs(db: Session, study_obj: Study) -> Dict[str, Any]:
    designs = (
        db.query(StudySavedDesign)
        .filter(StudySavedDesign.study_id == study_obj.id)
        .order_by(StudySavedDesign.created_at.desc())
        .limit(20)
        .all()
    )
    items = []
    evidence = []
    for idx, design in enumerate(designs, start=1):
        fact_id = f"SD{idx}"
        items.append(
            {
                "id": str(design.id),
                "name": design.name,
                "metric": design.metric,
                "segment_label": design.segment_label,
                "total_coefficient": design.total_coefficient,
                "selection_count": design.selection_count,
                "fact_id": fact_id,
            }
        )
        evidence.append(_fact(fact_id, design.name, design.total_coefficient))
    answer = f"Found {len(items)} saved designs." if items else "No saved designs yet."
    return {
        "answer_text": answer,
        "blocks": [AssistantBlock(type="saved_designs", title="Saved designs", data={"items": items}).model_dump()],
        "evidence": [e.model_dump() for e in evidence],
        "follow_ups": ["Show best 10 designs", "Open design configurator"],
        "actions": [AssistantAction(type="open_configurator", label="Open configurator", payload={"view": "configurator"}).model_dump()],
    }


def tool_generate_ppt(
    *,
    db: Session,
    study_obj: Study,
    analysis: Dict[str, Any],
    plan: AssistantQueryPlan,
    context: AppliedContext,
    filters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Prepare a branded analytics PowerPoint download for the study."""
    summary = analysis.get("dashboard_summary") or {}
    panelists = int(summary.get("uniquePanelists") or 0)
    responses = int(summary.get("totalResponses") or 0)
    if responses <= 0:
        return {
            "status": "answered",
            "answer_text": (
                "This study does not have completed responses yet, so I cannot generate "
                "an analytics PowerPoint."
            ),
            "blocks": [],
            "evidence": [],
            "follow_ups": ["Show study overview"],
            "actions": [],
        }

    from app.services.ppt_export import _pptx_filename

    study_title = context.study_title or study_obj.title or "Study"
    filename = _pptx_filename(study_title)
    answer = (
        f"I’ve prepared a MindSurve analytics PowerPoint for “{study_title}” "
        f"({panelists} panelists, {responses} responses). "
        "It includes the study overview, executive findings, top/worst elements, "
        "top/worst designs, classification insights, segment gaps, use/avoid guidance, "
        "and next steps. Click Download PowerPoint to save the deck."
    )
    return {
        "status": "answered",
        "answer_text": answer,
        "blocks": [
            AssistantBlock(
                type="ppt_export",
                title="Analytics PowerPoint",
                data={
                    "filename": filename,
                    "study_title": study_title,
                    "slide_count": 15,
                    "panelists": panelists,
                    "responses": responses,
                },
            ).model_dump()
        ],
        "evidence": [
            _fact("P1", "Panelists", panelists),
            _fact("P2", "Responses", responses),
            _fact("P3", "Deck slides", 15),
        ],
        "follow_ups": [
            "Show the most important findings",
            "Show top 5 designs",
            "Show top 5 elements",
        ],
        "actions": [
            AssistantAction(
                type="download_ppt",
                label="Download PowerPoint",
                payload={
                    "filename": filename,
                    "filters": filters or {},
                    "metric": (plan.metric or AssistantMetric.T).value,
                    "segment_section": plan.segment_section,
                    "segment_key": plan.segment_key,
                },
            ).model_dump()
        ],
        "follow_up_context": AssistantFollowUpContext(
            metric=plan.metric or AssistantMetric.T,
            segment_section=plan.segment_section,
            segment_key=plan.segment_key,
            last_tool=AssistantToolName.generate_ppt,
        ).model_dump(),
    }


def execute_tool(
    *,
    db: Session,
    study_obj: Study,
    current_user: User,
    plan: AssistantQueryPlan,
    analysis: Dict[str, Any],
    filters: Optional[Dict[str, Any]],
    context: AppliedContext,
    message: str = "",
) -> Dict[str, Any]:
    tool = plan.tool
    if tool == AssistantToolName.greeting:
        return {
            "status": "answered",
            "answer_text": (
                f"Hello! Welcome to {study_obj.title or 'this study'} analytics. "
                "Ask me about the best designs, strongest elements, audience segments, "
                "classification answers, mindsets, or response behavior."
            ),
            "blocks": [],
            "evidence": [],
            "follow_ups": [
                "Show the best design overall",
                "Show the strongest elements",
                "Summarize this study",
            ],
            "actions": [],
        }
    if tool == AssistantToolName.clarify:
        return {
            "status": "needs_clarification",
            "answer_text": plan.clarification_prompt or "Please clarify your question.",
            "clarification_options": plan.clarification_options or [],
            "blocks": [],
            "evidence": [],
            "follow_ups": [],
            "actions": [],
        }
    if tool == AssistantToolName.unsupported:
        return {
            "status": "unsupported",
            "answer_text": plan.unsupported_reason
            or "I can only answer verified study analytics and design questions for this study.",
            "blocks": [],
            "evidence": [],
            "follow_ups": [
                "Show study overview",
                "Show top 10 elements",
                "Show best 10 designs",
                "Classification answer counts",
            ],
            "actions": [],
        }

    # Empty cohort guard
    summary = analysis.get("dashboard_summary") or {}
    if int(summary.get("totalResponses") or 0) <= 0 and tool not in {
        AssistantToolName.study_overview,
        AssistantToolName.list_saved_designs,
        AssistantToolName.generate_ppt,
    }:
        return {
            "status": "answered",
            "answer_text": "This study does not have completed responses yet, so I cannot compute verified analytics.",
            "blocks": [],
            "evidence": [],
            "follow_ups": ["Show study overview"],
            "actions": [],
        }

    if tool == AssistantToolName.study_overview:
        return tool_study_overview(analysis, context)
    if tool == AssistantToolName.classification_distribution:
        return tool_classification_distribution(db, study_obj, plan, filters)
    if tool == AssistantToolName.rank_elements:
        return tool_rank_elements(analysis, study_obj, plan)
    if tool == AssistantToolName.rank_designs:
        cohort_filters: Optional[Dict[str, Any]] = None
        if plan.classification_options or plan.classification_question:
            _q, cohort_filters, clarify, options = build_cohort_filter_payload(study_obj, plan)
            if clarify:
                return {
                    "status": "needs_clarification",
                    "answer_text": clarify,
                    "clarification_options": options[:8],
                    "blocks": [],
                    "evidence": [],
                    "follow_ups": [],
                    "actions": [],
                }
            merged = merge_assistant_filters(filters, cohort_filters)
            if merged and merged != (filters or {}):
                try:
                    analysis = load_analysis_for_assistant(
                        db, study_obj, current_user, filters=merged or None
                    )
                except Exception:
                    pass
        result = tool_rank_designs(analysis, study_obj, plan)
        if cohort_filters and result.get("status", "answered") == "answered":
            actions = list(result.get("actions") or [])
            label = ", ".join(
                vals[0]
                for vals in (cohort_filters.get("classification_filters") or {}).values()
                if vals
            )
            actions.insert(
                0,
                AssistantAction(
                    type="apply_filter",
                    label=f"Apply filter ({label})" if label else "Apply cohort filter",
                    payload={"filters": cohort_filters},
                ).model_dump(),
            )
            result["actions"] = actions
        return result
    if tool == AssistantToolName.explain_design:
        return tool_explain_design(analysis, study_obj, plan)
    if tool == AssistantToolName.compare:
        return tool_compare(db, analysis, study_obj, current_user, plan, filters, message)
    if tool == AssistantToolName.compare_segments:
        return tool_compare_segments(analysis, plan)
    if tool == AssistantToolName.executive_summary:
        return tool_executive_summary(db, analysis, study_obj, plan, context)
    if tool == AssistantToolName.use_avoid_elements:
        return tool_use_avoid(analysis, study_obj, plan)
    if tool == AssistantToolName.response_time_summary:
        return tool_response_time_summary(analysis)
    if tool == AssistantToolName.fatigue_summary:
        return tool_fatigue_summary(analysis)
    if tool == AssistantToolName.explain_mindset:
        return tool_explain_mindset(analysis, study_obj, plan)
    if tool == AssistantToolName.list_saved_designs:
        return tool_list_saved_designs(db, study_obj)
    if tool == AssistantToolName.generate_ppt:
        return tool_generate_ppt(
            db=db,
            study_obj=study_obj,
            analysis=analysis,
            plan=plan,
            context=context,
            filters=filters,
        )

    return {
        "status": "unsupported",
        "answer_text": "That analytics question is not supported yet.",
        "blocks": [],
        "evidence": [],
        "follow_ups": ["Show study overview"],
        "actions": [],
    }
