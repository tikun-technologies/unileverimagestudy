"""
Verified analytics assistant orchestrator.

GPT-4o-mini only produces a tiny JSON query plan.
All numbers, rankings, and designs come from deterministic tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.cache import RedisCache
from app.core.config import settings
from app.models.user_model import User
from app.schemas.assistant_schema import (
    AssistantFollowUpContext,
    AssistantMetric,
    AssistantQueryPlan,
    AssistantQueryRequest,
    AssistantQueryResponse,
    AssistantToolName,
    RankDirection,
)
from app.services.analysis_filter import get_active_filter
from app.services.assistant_tools import (
    AssistantToolError,
    authorize_study_for_assistant,
    build_applied_context,
    execute_tool,
    extract_age_segment_from_text,
    extract_gender_from_text,
    find_classification_question_by_text,
    load_analysis_for_assistant,
    match_classification_options_in_text,
    resolve_age_segment_key,
    resolve_classification_question_from_options,
)

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI  # type: ignore

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


PLANNER_SYSTEM = """You convert study analytics questions into a tiny JSON query plan.
Return ONLY valid JSON with keys:
tool, metric, direction, limit, segment_section, segment_key, gender_key, age_key,
classification_question, classification_options, must_include, clarification_prompt,
clarification_options, unsupported_reason, confidence.

Allowed tools:
greeting, study_overview, classification_distribution, rank_elements, rank_designs,
compare_segments, use_avoid_elements, response_time_summary, fatigue_summary,
explain_mindset, explain_design, list_saved_designs, clarify, unsupported.

Rules:
- metric must be T, B, or R (default T).
- direction must be highest or lowest.
- A singular request ("best design", "best statement") has limit=1.
- A plural request without a number has limit=4.
- An explicit count ("top 10" or "top 2-3") must be respected (use the upper bound for ranges).
- must_include is an array of required design ingredients from the user question.
  Examples:
  - "best design with A4-largecap-white-transp" -> must_include=["A4-largecap-white-transp"]
  - "top 2 combinations which has A6-silver-clinical" -> must_include=["A6-silver-clinical"]
  - "top combinations with holographic tick and silver-clinical"
    -> must_include=["holographic tick","silver-clinical"]
  - "top combinations with a white colour" -> must_include=["white"]
  - "best mix including antibacterial and DMC-green" -> must_include=["antibacterial","DMC-green"]
- Copy must_include strings from the user question exactly; do not invent or shorten
  element names (keep "A6-silver-clinical", never reduce it to "silver").
- Never invent numbers or element names.
- If ambiguous, use tool=clarify with short options.
- If outside verified study analytics/design, use tool=unsupported.
- rank_designs for best/worst mixes/designs/combinations, including "with <element>" requests.
- rank_elements for top/bottom concepts/elements/claims.
- explain_design for why a design is best/better/not better.
- greeting for hi/hello/hey/thanks and other conversational greetings.
- classification_distribution for classification/prelim questions and option counts.
  Examples:
  - "how many answered How often do you cook meals at home?"
  - "how many selected A few times per month" -> classification_options=["A few times per month"]
  - "how many chose Daily or almost daily and Rarely or never"
  - "how many selected Daily or almost daily in male segment 22 years old"
    -> classification_options=["Daily or almost daily"], gender_key="Male", age_key="18-24"
  - Copy option labels from the user question into classification_options.
  - Options can repeat across questions; questions are unique. If option is ambiguous, still
    set classification_options and leave classification_question null so the tool can clarify.
  - gender_key is Male/Female; age_key is a study bucket (18-24, 25-34, ...). Single ages snap
    to buckets (22 -> 18-24). Both gender_key and age_key may be set together.
"""


def _openai_client():
    if not OPENAI_AVAILABLE:
        return None
    api_key = settings.OPENAI_API_KEY or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


_RATE_BUCKET: Dict[str, list] = {}


def _rate_limit_ok(user_id: str) -> bool:
    now = time.time()
    window = 60.0
    limit = settings.ASSISTANT_RATE_LIMIT_PER_MINUTE
    bucket = _RATE_BUCKET.setdefault(user_id, [])
    _RATE_BUCKET[user_id] = [t for t in bucket if now - t < window]
    if len(_RATE_BUCKET[user_id]) >= limit:
        return False
    _RATE_BUCKET[user_id].append(now)
    return True


def _cache_key(study_id: str, user_id: str, payload: Dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
    return f"assistant:query:{study_id}:{user_id}:{digest}"


def _deterministic_plan(message: str, request: AssistantQueryRequest) -> AssistantQueryPlan:
    text = message.casefold()
    follow = request.follow_up

    if re.fullmatch(
        r"\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|thanks|thank\s+you)[!?.\s]*",
        text,
    ):
        return AssistantQueryPlan(tool=AssistantToolName.greeting, confidence=1.0)

    # Follow-up shortcuts
    if follow and any(token in text for token in ("opposite", "worst", "least", "now lowest", "now highest")):
        direction = RankDirection.lowest
        if follow.last_direction == RankDirection.lowest or "highest" in text or "best" in text:
            if "worst" in text or "least" in text or "lowest" in text:
                direction = RankDirection.lowest
            else:
                direction = RankDirection.highest
        if follow.last_direction == RankDirection.highest and ("opposite" in text or "worst" in text or "least" in text):
            direction = RankDirection.lowest
        if follow.last_direction == RankDirection.lowest and ("opposite" in text or "best" in text or "highest" in text):
            direction = RankDirection.highest
        tool = follow.last_tool or AssistantToolName.rank_elements
        return AssistantQueryPlan(
            tool=tool if tool not in {AssistantToolName.clarify, AssistantToolName.unsupported} else AssistantToolName.rank_elements,
            metric=follow.metric or request.metric or AssistantMetric.T,
            direction=direction,
            limit=follow.last_limit or 10,
            segment_section=follow.segment_section or request.segment_section,
            segment_key=follow.segment_key or request.segment_key,
            classification_question=follow.classification_question,
            confidence=0.85,
        )

    limit = 4
    explicit_limit = _extract_result_count(text)
    if explicit_limit:
        limit = explicit_limit

    direction = RankDirection.highest
    direction_text = text.replace("bottom up", "")
    if any(token in direction_text for token in ("worst", "least", "bottom", "lowest", "poor", "underperform")):
        direction = RankDirection.lowest

    metric = request.metric or AssistantMetric.T
    if "bottom up" in text or re.search(r"\b\(b\)\b", text) or re.search(r"\bmetric b\b", text):
        metric = AssistantMetric.B
    if "response time" in text or re.search(r"\b\(r\)\b", text):
        if "fatigue" not in text and "summary" not in text and "distribution" not in text:
            # keep T unless explicitly asking for R metric rankings
            if any(token in text for token in ("metric r", "using r", "response metric", "(r)")):
                metric = AssistantMetric.R

    segment_section = request.segment_section
    segment_key = request.segment_key
    gender_key = extract_gender_from_text(text)
    age_key = None
    if re.search(r"\d{2}-\d{2}|\b65\+|\bage\b|\baged\b|\byears?\s*old\b|\byo\b", text):
        age_key = extract_age_segment_from_text(text)

    if "mindset" in text:
        segment_section = "Mindsets"
        mk = re.search(r"mindset\s*([123])", text)
        if mk:
            n = mk.group(1)
            segment_key = f"Mindset_{n}_of_3" if "of 3" in text or "3 mindset" in text else f"Mindset_{n}_of_2"
    elif gender_key and not age_key:
        segment_section = segment_section or "Gender"
        segment_key = segment_key or gender_key
    elif age_key and not gender_key:
        segment_section = segment_section or "Age"
        segment_key = segment_key or age_key
    elif gender_key and age_key:
        # Dual demographic filter (common for classification). Keep a primary label too.
        segment_section = segment_section or "Gender"
        segment_key = segment_key or gender_key

    # Classification / prelim option counts.
    # Also continue a clarification turn when the previous tool was classification.
    classification_follow = bool(
        follow
        and follow.last_tool == AssistantToolName.classification_distribution
    )
    if classification_follow or any(
        token in text
        for token in (
            "how many answered",
            "how many selected",
            "how many chose",
            "how many picked",
            "how many users answered",
            "how many user answered",
            "classification",
            "prelim",
            "what are the other options",
            "answer options",
            "option counts",
            "selected this option",
            "answered this option",
            "classification option",
        )
    ):
        pending_options = list(follow.classification_options or []) if follow else []
        question_hint = follow.classification_question if follow else None
        # Clicking a clarification chip sends the question title as the message.
        if classification_follow and not any(
            token in text
            for token in (
                "how many",
                "classification",
                "prelim",
                "option",
                "selected",
                "answered",
                "chose",
            )
        ):
            question_hint = message.strip()
        follow_gender = follow.gender_key if follow else None
        follow_age = follow.age_key if follow else None
        return AssistantQueryPlan(
            tool=AssistantToolName.classification_distribution,
            metric=metric,
            segment_section=segment_section,
            segment_key=segment_key,
            gender_key=gender_key or follow_gender,
            age_key=age_key or follow_age,
            classification_question=question_hint,
            classification_options=pending_options,
            confidence=0.9,
        )
    if (
        any(token in text for token in ("why", "explain", "reason"))
        and any(token in text for token in ("design", "mix", "combination", "this one", "this"))
        and "mindset" not in text
    ):
        return AssistantQueryPlan(
            tool=AssistantToolName.explain_design,
            metric=metric,
            direction=direction,
            limit=2,
            segment_section=segment_section,
            segment_key=segment_key,
            confidence=0.9,
        )
    must_include = _extract_must_include(message)
    if any(
        token in text
        for token in (
            "best mix",
            "best design",
            "top design",
            "worst design",
            "least performing design",
            "combinations",
            "configurator",
            "performing combination",
            "performing combinations",
            "performing design",
            "performing mix",
        )
    ) or (
        must_include
        and any(
            token in text
            for token in (
                "with",
                "including",
                "using",
                "contain",
                "containing",
                "combination",
                "design",
                "mix",
                "performing",
            )
        )
    ):
        return AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            metric=metric,
            direction=direction,
            limit=limit,
            segment_section=segment_section,
            segment_key=segment_key,
            must_include=must_include,
            confidence=0.92,
        )
    if any(token in text for token in ("use or avoid", "use/avoid", "should we use", "recommend elements")):
        return AssistantQueryPlan(
            tool=AssistantToolName.use_avoid_elements,
            metric=metric,
            segment_section=segment_section,
            segment_key=segment_key,
            confidence=0.88,
        )
    if "saved design" in text:
        return AssistantQueryPlan(tool=AssistantToolName.list_saved_designs, confidence=0.9)
    if "fatigue" in text:
        return AssistantQueryPlan(tool=AssistantToolName.fatigue_summary, confidence=0.9)
    if "response time" in text:
        return AssistantQueryPlan(tool=AssistantToolName.response_time_summary, confidence=0.9)
    if "explain mindset" in text or ("mindset" in text and "explain" in text):
        return AssistantQueryPlan(
            tool=AssistantToolName.explain_mindset,
            metric=metric,
            segment_section="Mindsets",
            segment_key=segment_key,
            confidence=0.9,
        )
    if any(token in text for token in ("compare", "vs", "versus")) and any(
        token in text for token in ("gender", "age", "mindset", "segment")
    ):
        return AssistantQueryPlan(
            tool=AssistantToolName.compare_segments,
            metric=metric,
            segment_section=segment_section or ("Mindsets" if "mindset" in text else "Gender" if "gender" in text else "Age"),
            confidence=0.86,
        )
    if any(token in text for token in ("top element", "bottom element", "best element", "worst element", "top concept", "performers", "claims")):
        return AssistantQueryPlan(
            tool=AssistantToolName.rank_elements,
            metric=metric,
            direction=direction,
            limit=limit,
            segment_section=segment_section,
            segment_key=segment_key,
            confidence=0.9,
        )
    if any(token in text for token in ("overview", "summary", "how many respondents", "kpi", "dashboard")):
        return AssistantQueryPlan(tool=AssistantToolName.study_overview, confidence=0.9)

    # Generic ranking language
    if any(token in text for token in ("top ", "best ", "worst ", "bottom ")):
        if "design" in text or "mix" in text or "combination" in text or must_include:
            tool = AssistantToolName.rank_designs
        else:
            tool = AssistantToolName.rank_elements
        return AssistantQueryPlan(
            tool=tool,
            metric=metric,
            direction=direction,
            limit=limit,
            segment_section=segment_section,
            segment_key=segment_key,
            must_include=must_include if tool == AssistantToolName.rank_designs else [],
            confidence=0.75,
        )

    return AssistantQueryPlan(
        tool=AssistantToolName.clarify,
        clarification_prompt="What would you like me to analyze?",
        clarification_options=[
            "Study overview",
            "Top 10 elements",
            "Best 10 designs",
            "Classification answer counts",
            "Use / avoid recommendations",
        ],
        confidence=0.4,
    )


def _parse_plan(raw: Dict[str, Any], request: AssistantQueryRequest) -> AssistantQueryPlan:
    tool_raw = str(raw.get("tool") or "clarify").strip()
    try:
        tool = AssistantToolName(tool_raw)
    except ValueError:
        tool = AssistantToolName.clarify

    metric_raw = str(raw.get("metric") or (request.metric.value if request.metric else "T")).upper()
    if metric_raw not in {"T", "B", "R"}:
        metric_raw = "T"

    direction_raw = str(raw.get("direction") or "highest").lower()
    if direction_raw not in {"highest", "lowest"}:
        direction_raw = "highest"

    try:
        limit = int(raw.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, settings.ASSISTANT_MAX_RESULT_LIMIT))

    options = raw.get("clarification_options") or []
    if not isinstance(options, list):
        options = []
    options = [str(o)[:120] for o in options[:8]]

    confidence_raw = raw.get("confidence")
    try:
        confidence = float(confidence_raw or 0.7)
    except (TypeError, ValueError):
        confidence = {
            "high": 0.9,
            "medium": 0.7,
            "low": 0.4,
        }.get(str(confidence_raw or "").casefold(), 0.7)

    must_include_raw = raw.get("must_include") or []
    if isinstance(must_include_raw, str):
        must_include_raw = [must_include_raw]
    if not isinstance(must_include_raw, list):
        must_include_raw = []
    must_include = [str(item)[:120] for item in must_include_raw if str(item or "").strip()][:8]

    classification_options_raw = raw.get("classification_options") or []
    if isinstance(classification_options_raw, str):
        classification_options_raw = [classification_options_raw]
    if not isinstance(classification_options_raw, list):
        classification_options_raw = []
    classification_options = [
        str(item)[:120] for item in classification_options_raw if str(item or "").strip()
    ][:8]
    if not classification_options and request.follow_up:
        classification_options = list(request.follow_up.classification_options or [])[:8]

    gender_key = None
    if raw.get("gender_key"):
        gender_key = extract_gender_from_text(str(raw.get("gender_key"))) or str(raw.get("gender_key"))[:40]
    elif request.follow_up and request.follow_up.gender_key:
        gender_key = request.follow_up.gender_key

    age_key = None
    if raw.get("age_key"):
        age_key = resolve_age_segment_key(str(raw.get("age_key"))) or str(raw.get("age_key"))[:40]
    elif request.follow_up and request.follow_up.age_key:
        age_key = request.follow_up.age_key

    return AssistantQueryPlan(
        tool=tool,
        metric=AssistantMetric(metric_raw),
        direction=RankDirection(direction_raw),
        limit=limit,
        segment_section=(str(raw["segment_section"])[:80] if raw.get("segment_section") else request.segment_section),
        segment_key=(str(raw["segment_key"])[:80] if raw.get("segment_key") else request.segment_key),
        gender_key=gender_key,
        age_key=age_key,
        classification_question=(
            str(raw["classification_question"])[:200]
            if raw.get("classification_question")
            else (request.follow_up.classification_question if request.follow_up else None)
        ),
        classification_options=classification_options,
        must_include=must_include,
        clarification_prompt=(str(raw.get("clarification_prompt") or "")[:400] or None),
        clarification_options=options,
        unsupported_reason=(str(raw.get("unsupported_reason") or "")[:400] or None),
        confidence=max(0.0, min(1.0, confidence)),
    )


def _extract_result_count(text: str) -> Optional[int]:
    """Parse explicit result counts from natural language ranking requests."""
    if not text:
        return None
    normalized = text.casefold()

    range_match = re.search(
        r"\b(?:top|best|worst|bottom|lowest|highest|least|show|give(?:\s+me)?)\s+"
        r"(\d{1,2})\s*(?:[-–]|to)\s*(\d{1,2})\b",
        normalized,
    )
    if range_match:
        upper = max(int(range_match.group(1)), int(range_match.group(2)))
        return max(1, min(upper, settings.ASSISTANT_MAX_RESULT_LIMIT))

    patterns = (
        # "worst performing 5", "best performing 10"
        r"\b(?:top|best|worst|bottom|lowest|highest|least)\s+performing\s+(\d{1,2})\b",
        # "top 5", "worst 5", "best 10"
        r"\b(?:top|best|worst|bottom|lowest|highest|show|give(?:\s+me)?)\s+(\d{1,2})\b",
        # "least performing 5", "least-performing 5"
        r"\bleast(?:-|\s)?performing\s+(\d{1,2})\b",
        # "5 worst designs", "5 best performing combinations"
        r"\b(\d{1,2})\s+(?:top|best|worst|bottom|lowest|highest|least)\s+(?:performing\s+)?"
        r"(?:design|designs|mix|mixes|combination|combinations|element|elements|statement|statements)?",
        # "give me the 5 best overall"
        r"\b(?:give(?:\s+me)?|show(?:\s+me)?|get)\s+(?:the\s+)?(\d{1,2})\s+"
        r"(?:top|best|worst|bottom|lowest|highest|least)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return max(1, min(int(match.group(1)), settings.ASSISTANT_MAX_RESULT_LIMIT))
    return None


def _explicit_result_count(text: str) -> Optional[int]:
    return _extract_result_count(text)


_STOP_INCLUDE_WORDS = {
    "a",
    "an",
    "the",
    "with",
    "using",
    "including",
    "include",
    "that",
    "includes",
    "containing",
    "contains",
    "having",
    "have",
    "colour",
    "color",
    "colored",
    "coloured",
    "design",
    "designs",
    "mix",
    "combination",
    "combinations",
    "element",
    "elements",
    "layer",
    "and",
    "or",
    "for",
    "overall",
    "best",
    "top",
    "performing",
}


def _looks_like_element_code(value: str) -> bool:
    """True for codes like A6-silver-clinical / silver-clinical (not age ranges)."""
    text = str(value or "").strip()
    if not text or " " in text:
        return False
    if re.fullmatch(r"\d{1,3}\s*[-–]\s*\d{1,3}", text):
        return False
    if re.fullmatch(r"\d{1,3}\+?", text):
        return False
    # Letter/digit token with at least one hyphen, or code+digits like A6silver…
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){1,6}", text):
        return True
    if re.fullmatch(r"[A-Za-z]{1,4}\d+[A-Za-z0-9_-]*", text):
        return True
    return False


def _extract_must_include(message: str) -> List[str]:
    """Pull required element/color ingredients from natural-language questions."""
    text = " ".join((message or "").strip().split())
    if not text:
        return []
    found: List[str] = []

    def add(raw: str) -> None:
        cleaned = " ".join(str(raw or "").strip(" .,!?;:\"'()[]").split())
        if not cleaned:
            return
        # Drop leading filler words but keep meaningful tokens like "white".
        parts = [p for p in re.split(r"\s+", cleaned) if p.casefold() not in _STOP_INCLUDE_WORDS]
        if not parts and cleaned.casefold() not in _STOP_INCLUDE_WORDS:
            parts = [cleaned]
        value = " ".join(parts).strip(" -_")
        if len(value) < 2:
            return
        # Avoid capturing whole question tails.
        if len(value) > 80:
            return
        if value.casefold() in {item.casefold() for item in found}:
            return
        found.append(value)

    for quoted in re.findall(r"[\"']([^\"']{2,80})[\"']", text):
        add(quoted)

    # Specific element codes mentioned anywhere (A6-silver-clinical, silver-clinical).
    for code in re.findall(
        r"\b[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){1,6}\b|\b[A-Za-z]{1,4}\d+[A-Za-z0-9_-]*\b",
        text,
    ):
        if _looks_like_element_code(code):
            add(code)

    patterns = [
        r"\bwith\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\bincluding\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\bthat\s+includes?\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\b(?:which\s+)?(?:has|have)\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\bthat\s+has\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\busing\s+(?:the\s+)?(?:element\s+)?(.+?)(?:\s+(?:for|among|in|on|under)\b|[?.!]|$)",
        r"\bcontain(?:s|ing)?\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\bwhich\s+contain(?:s|ing)?\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            chunk = match.group(1)
            # Support "A and B" / "A, B".
            for piece in re.split(r"\s*(?:,|/|\band\b|\bplus\b)\s*", chunk, flags=re.IGNORECASE):
                add(piece)

    # Color shorthand: "white colour/color" or color used as an ingredient filter.
    # Skip when a specific hyphenated element code was already captured (avoid
    # collapsing "A6-silver-clinical" down to a loose "silver" match).
    has_specific_code = any(_looks_like_element_code(item) for item in found)
    color_match = re.search(
        r"\b(white|black|red|blue|green|yellow|orange|pink|purple|grey|gray|transparent|transp|opaque|silver|gold)\b",
        text,
        flags=re.IGNORECASE,
    )
    if (
        color_match
        and not has_specific_code
        and re.search(
            r"\b(?:colour|color|coloured|colored|with|including|contain(?:s|ing)?|has|have)\b",
            text,
            flags=re.IGNORECASE,
        )
    ):
        add(color_match.group(1))

    # Prefer longer/more-specific hints when one contains another
    # (e.g. keep A6-silver-clinical, drop silver).
    pruned: List[str] = []
    for item in sorted(found, key=lambda value: (-len(value), value.casefold())):
        item_norm = re.sub(r"[^a-z0-9]+", "", item.casefold())
        if any(item_norm and item_norm != re.sub(r"[^a-z0-9]+", "", other.casefold()) and item_norm in re.sub(r"[^a-z0-9]+", "", other.casefold()) for other in pruned):
            continue
        pruned.append(item)
    return pruned[:8]


def _normalize_plan_for_question(
    plan: AssistantQueryPlan,
    message: str,
    study_type: str,
    request: AssistantQueryRequest,
) -> AssistantQueryPlan:
    """Enforce product semantics after either OpenAI or fallback planning."""
    text = message.casefold()
    follow = request.follow_up

    if re.fullmatch(
        r"\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|thanks|thank\s+you)[!?.\s]*",
        text,
    ):
        plan.tool = AssistantToolName.greeting
        plan.limit = 1
        return plan

    # No metric in the user's words always means T. UI tab state must not
    # silently change the meaning of an otherwise "overall" question.
    if "bottom up" in text or re.search(r"\bmetric\s*b\b|\busing\s+b\b|\(b\)", text):
        plan.metric = AssistantMetric.B
    elif re.search(r"\bmetric\s*r\b|\busing\s+r\b|\(r\)", text):
        plan.metric = AssistantMetric.R
    elif follow and any(token in text for token in ("same", "opposite", "why", "this", "now")):
        plan.metric = follow.metric or AssistantMetric.T
    else:
        plan.metric = AssistantMetric.T

    if re.search(r"\boverall\b", text):
        plan.segment_section = None
        plan.segment_key = None

    # Demographic filters: Gender and Age can both be present (classification).
    gender_from_text = extract_gender_from_text(text)
    age_from_text = extract_age_segment_from_text(text)
    if gender_from_text:
        plan.gender_key = gender_from_text
    elif plan.gender_key:
        plan.gender_key = extract_gender_from_text(str(plan.gender_key)) or plan.gender_key
    if age_from_text:
        plan.age_key = age_from_text
    elif plan.age_key:
        plan.age_key = resolve_age_segment_key(plan.age_key) or plan.age_key

    # Models often return colloquial labels ("men", "female") while analysis
    # sections use canonical keys. Normalize before deterministic lookup.
    requested_segment = str(plan.segment_key or "").strip()
    requested_lower = requested_segment.casefold()
    if re.search(r"\b(?:men|man|male)\b", text) or requested_lower in {"men", "man", "males", "male"}:
        plan.gender_key = plan.gender_key or "Male"
        if not plan.age_key:
            plan.segment_section = "Gender"
            plan.segment_key = "Male"
    elif re.search(r"\b(?:women|woman|female)\b", text) or requested_lower in {"women", "woman", "females", "female"}:
        plan.gender_key = plan.gender_key or "Female"
        if not plan.age_key:
            plan.segment_section = "Gender"
            plan.segment_key = "Female"
    elif str(plan.segment_section or "").casefold() == "gender":
        plan.segment_section = "Gender"

    # Age: map "45" / "47" / "22 years old" onto analysis buckets.
    age_from_key = resolve_age_segment_key(plan.segment_key) if str(plan.segment_section or "").casefold() == "age" else None
    snapped_age = plan.age_key or age_from_text or age_from_key
    if snapped_age:
        plan.age_key = snapped_age
        if not plan.gender_key:
            plan.segment_section = "Age"
            plan.segment_key = snapped_age
        else:
            # Dual filter: keep Gender as primary segment label for chips.
            plan.segment_section = plan.segment_section or "Gender"
            plan.segment_key = plan.gender_key

    direction_text = text.replace("bottom up", "")
    if any(token in direction_text for token in ("worst", "least-performing", "least performing", "lowest", "underperform")):
        plan.direction = RankDirection.lowest
    elif any(token in direction_text for token in ("best", "top", "highest")):
        plan.direction = RankDirection.highest

    extracted_includes = _extract_must_include(message)
    if extracted_includes:
        # Prefer question text over model output for ingredient requirements.
        merged = list(extracted_includes)
        for item in plan.must_include or []:
            if item.casefold() not in {value.casefold() for value in merged}:
                merged.append(item)
        plan.must_include = merged[:8]
    if plan.must_include and plan.tool in {
        AssistantToolName.clarify,
        AssistantToolName.rank_elements,
        AssistantToolName.study_overview,
    }:
        # "top performing with <element>" is a design request.
        if any(
            token in text
            for token in (
                "with",
                "including",
                "using",
                "contain",
                "containing",
                "combination",
                "design",
                "mix",
                "performing",
            )
        ):
            plan.tool = AssistantToolName.rank_designs

    explicit_count = _explicit_result_count(text)
    singular_design = bool(
        re.search(r"\b(best|worst|top|lowest|highest)\s+(?:overall\s+)?(?:design|mix|combination)\b", text)
        or re.search(r"\bwhich\s+(?:design|mix)\s+is\s+(?:the\s+)?best\b", text)
        or (
            plan.must_include
            and re.search(r"\b(?:which|what)\s+is\s+(?:the\s+)?(?:top|best)\b", text)
            and not re.search(r"\b(?:statement|element|concept|claim)s?\b", text)
        )
    )
    singular_element = bool(
        re.search(r"\b(best|worst|top|lowest|highest)\s+(?:overall\s+)?(?:statement|element|concept|claim)\b", text)
        or re.search(r"\bwhich\s+(?:statement|element|concept|claim)\s+is\s+(?:the\s+)?best\b", text)
    )
    plural_element = bool(re.search(r"\b(statements|elements|concepts|claims)\b", text))
    plural_design = bool(re.search(r"\b(designs|mixes|combinations)\b", text))

    if plan.tool == AssistantToolName.explain_design:
        plan.limit = 2
        return plan

    if singular_design:
        plan.tool = AssistantToolName.rank_designs
        plan.limit = explicit_count or 1
    elif singular_element:
        plan.tool = AssistantToolName.rank_elements
        plan.limit = explicit_count or 1
    elif explicit_count and plan.tool in {AssistantToolName.rank_elements, AssistantToolName.rank_designs}:
        plan.limit = explicit_count
    elif plural_element and plan.tool == AssistantToolName.rank_elements:
        plan.limit = 4
    elif plural_design and plan.tool == AssistantToolName.rank_designs:
        plan.limit = 4

    # "Which is the best overall?" is naturally study-type dependent.
    # A layer study means the composite; a text study means one statement.
    vague_best_overall = bool(
        re.search(r"\b(?:which|what)\s+is\s+(?:the\s+)?best(?:\s+overall)?\b", text)
        or re.fullmatch(r"(?:show\s+me\s+)?(?:the\s+)?best\s+overall[?.!]?", text.strip())
    )
    if vague_best_overall and not singular_design and not singular_element:
        if study_type == "layer":
            plan.tool = AssistantToolName.rank_designs
        elif study_type == "text":
            plan.tool = AssistantToolName.rank_elements
        plan.limit = 1

    return plan


def plan_query(message: str, request: AssistantQueryRequest) -> Tuple[AssistantQueryPlan, Dict[str, Any]]:
    usage: Dict[str, Any] = {"planner": "deterministic", "prompt_tokens": 0, "completion_tokens": 0}
    client = _openai_client()
    if client is None:
        return _deterministic_plan(message, request), usage

    compact_context = {
        "metric_default": (request.metric.value if request.metric else "T"),
        "segment_section": request.segment_section,
        "segment_key": request.segment_key,
        "follow_up": request.follow_up.model_dump() if request.follow_up else None,
    }
    user_payload = {
        "question": message[:500],
        "context": compact_context,
    }
    try:
        response = client.chat.completions.create(
            model=settings.ASSISTANT_MODEL or settings.OPENAI_MODEL or "gpt-4o-mini",
            temperature=0,
            max_tokens=settings.ASSISTANT_MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": json.dumps(user_payload, separators=(",", ":"))},
            ],
            timeout=settings.ASSISTANT_TIMEOUT_SECONDS,
        )
        content = response.choices[0].message.content or "{}"
        raw = json.loads(content)
        plan = _parse_plan(raw, request)
        usage = {
            "planner": "openai",
            "model": settings.ASSISTANT_MODEL or settings.OPENAI_MODEL or "gpt-4o-mini",
            "prompt_tokens": getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(getattr(response, "usage", None), "completion_tokens", 0) or 0,
        }
        # If model is low-confidence, blend with deterministic fallback
        if plan.confidence < 0.45:
            fallback = _deterministic_plan(message, request)
            if fallback.tool != AssistantToolName.clarify:
                plan = fallback
                usage["planner"] = "openai+deterministic_fallback"
        return plan, usage
    except Exception as exc:
        logger.warning("Assistant planner failed, using deterministic fallback: %s", exc)
        usage["planner_error"] = str(exc)[:200]
        return _deterministic_plan(message, request), usage


def _enrich_classification_plan_with_study(
    plan: AssistantQueryPlan,
    message: str,
    study_obj: Any,
    request: AssistantQueryRequest,
) -> AssistantQueryPlan:
    """Resolve classification question/options using the study's configured catalog."""
    follow = request.follow_up
    matched_question = find_classification_question_by_text(study_obj, message)
    matched_options = match_classification_options_in_text(study_obj, message)

    pending: List[str] = []
    for opt in list(plan.classification_options or []) + list(
        (follow.classification_options if follow else None) or []
    ) + matched_options:
        cleaned = " ".join(str(opt or "").strip().split())
        if cleaned and cleaned not in pending:
            pending.append(cleaned)
    plan.classification_options = pending[:8]

    # Preserve demographic filters across clarification turns.
    if follow:
        plan.gender_key = plan.gender_key or follow.gender_key
        plan.age_key = plan.age_key or follow.age_key
    if not plan.gender_key:
        plan.gender_key = extract_gender_from_text(message)
    if not plan.age_key:
        plan.age_key = extract_age_segment_from_text(message)

    if matched_question:
        plan.tool = AssistantToolName.classification_distribution
        plan.classification_question = matched_question.question_text
        return plan

    if follow and follow.last_tool == AssistantToolName.classification_distribution:
        plan.tool = AssistantToolName.classification_distribution
        # Clarification chip sends the full question title as the next message.
        if not plan.classification_question and message.strip():
            plan.classification_question = message.strip()[:200]
        return plan

    if matched_options:
        plan.tool = AssistantToolName.classification_distribution
        # Options can repeat across questions — only auto-bind when unique.
        if not plan.classification_question:
            resolved, candidates = resolve_classification_question_from_options(
                study_obj, matched_options
            )
            if resolved:
                plan.classification_question = resolved.question_text
            elif candidates:
                # Leave question unset so the tool asks which question.
                plan.classification_question = None
        return plan

    if plan.tool == AssistantToolName.classification_distribution and not plan.classification_question:
        # Keep tool; tool layer will clarify which question when needed.
        return plan

    return plan


def _filters_dict(request: AssistantQueryRequest, db: Session, study_id, user_id) -> Optional[Dict[str, Any]]:
    if re.search(r"\boverall\b", request.message, flags=re.IGNORECASE):
        return None
    if request.filters is not None:
        data = request.filters.model_dump(exclude_none=True)
        return data or None
    if request.use_active_filters:
        active = get_active_filter(db, study_id, user_id)
        if active:
            return active
    return None


def run_assistant_query(
    db: Session,
    study_id,
    current_user: User,
    request: AssistantQueryRequest,
) -> AssistantQueryResponse:
    request_id = str(uuid.uuid4())
    if not _rate_limit_ok(str(current_user.id)):
        study_obj = authorize_study_for_assistant(db, study_id, current_user)
        return AssistantQueryResponse(
            request_id=request_id,
            status="error",
            answer_text="Rate limit exceeded. Please wait a minute and try again.",
            applied_context=build_applied_context(
                study_obj,
                {},
                AssistantQueryPlan(tool=AssistantToolName.unsupported),
                None,
            ),
            error="rate_limit",
            usage={"rate_limited": True},
        )

    try:
        study_obj = authorize_study_for_assistant(db, study_id, current_user)
    except AssistantToolError as exc:
        # Minimal error response without study context
        from app.schemas.assistant_schema import AppliedContext

        return AssistantQueryResponse(
            request_id=request_id,
            status="error",
            answer_text=exc.message,
            applied_context=AppliedContext(
                study_id=study_id,
                study_type="unknown",
                verified=False,
            ),
            error=exc.message,
        )

    filters = _filters_dict(request, db, study_id, current_user.id)
    cache_payload = {
        "assistant_semantics_version": 11,
        "message": request.message,
        "filters": filters,
        "metric": request.metric.value if request.metric else None,
        "segment_section": request.segment_section,
        "segment_key": request.segment_key,
        "follow_up": request.follow_up.model_dump() if request.follow_up else None,
    }
    cache_key = _cache_key(str(study_id), str(current_user.id), cache_payload)
    cached = RedisCache.get(cache_key)
    if isinstance(cached, dict) and cached.get("answer_text"):
        cached["request_id"] = request_id
        cached.setdefault("usage", {})["cache_hit"] = True
        try:
            return AssistantQueryResponse(**cached)
        except Exception:
            pass

    plan, planner_usage = plan_query(request.message, request)
    plan = _normalize_plan_for_question(
        plan,
        request.message,
        str(study_obj.study_type or "grid").lower(),
        request,
    )
    plan = _enrich_classification_plan_with_study(plan, request.message, study_obj, request)

    # Load analysis only when needed
    needs_analysis = plan.tool not in {
        AssistantToolName.greeting,
        AssistantToolName.clarify,
        AssistantToolName.unsupported,
        AssistantToolName.classification_distribution,
        AssistantToolName.list_saved_designs,
    }
    analysis: Dict[str, Any] = {}
    if needs_analysis or plan.tool == AssistantToolName.study_overview:
        try:
            analysis = load_analysis_for_assistant(db, study_obj, current_user, filters=filters)
        except Exception as exc:
            logger.exception("Failed to load analysis for assistant")
            context = build_applied_context(study_obj, {}, plan, filters)
            return AssistantQueryResponse(
                request_id=request_id,
                status="error",
                answer_text="I could not load verified analysis for this study right now.",
                tool=plan.tool,
                applied_context=context,
                error=str(exc)[:200],
                usage=planner_usage,
            )

    # Classification can still benefit from empty analysis context
    if plan.tool == AssistantToolName.classification_distribution and not analysis:
        try:
            analysis = load_analysis_for_assistant(db, study_obj, current_user, filters=filters)
        except Exception:
            analysis = {}

    context = build_applied_context(study_obj, analysis, plan, filters)
    result = execute_tool(
        db=db,
        study_obj=study_obj,
        current_user=current_user,
        plan=plan,
        analysis=analysis,
        filters=filters,
        context=context,
    )

    status = result.get("status") or "answered"
    follow_up_ctx = result.get("follow_up_context")
    if isinstance(follow_up_ctx, dict):
        try:
            follow_up_model = AssistantFollowUpContext(**follow_up_ctx)
        except Exception:
            follow_up_model = None
    else:
        follow_up_model = None

    usage = {**planner_usage, **(result.get("usage") or {})}
    response = AssistantQueryResponse(
        request_id=request_id,
        status=status,
        answer_text=result.get("answer_text") or "",
        tool=plan.tool,
        applied_context=context,
        blocks=result.get("blocks") or [],
        evidence=result.get("evidence") or [],
        follow_ups=result.get("follow_ups") or [],
        actions=result.get("actions") or [],
        clarification_options=result.get("clarification_options") or plan.clarification_options or [],
        follow_up_context=follow_up_model,
        usage=usage,
        error=result.get("error"),
    )

    if status == "answered":
        RedisCache.set(cache_key, json.loads(response.model_dump_json()), ttl_seconds=settings.ASSISTANT_CACHE_TTL_SECONDS)
    return response
