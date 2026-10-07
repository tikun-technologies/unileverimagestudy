"""Atomic limits for individual screening-question options.

Seats are taken only when a respondent continues past screening, and only if
every selected option still has room. A full option rejects the whole set:
no other option is incremented, and the respondent row is removed by the caller.

Synthetic and random respondents share this counter. Existing saved answers are
counted, and a new one is stored only when every selected limited option still
has a seat.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import select, tuple_, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.response_model import ClassificationAnswer, ScreeningOptionQuota, StudyResponse
from app.models.study_model import StudyClassificationQuestion

OptionKey = Tuple[str, str]
QUOTA_FULL_MESSAGE = "This response has been removed. You can no longer continue this study."


def _positive_limit(value) -> Optional[int]:
    if value is None or value is False:
        return None
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None
    if limit < 1:
        return None
    return limit


def _option_id(option: dict) -> Optional[str]:
    raw = option.get("id")
    if raw is None or raw is False:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text[:10]


def _option_text(option: dict) -> str:
    return str(option.get("text") or option.get("label") or option.get("name") or "").strip()


def _is_optional_question(question) -> bool:
    if isinstance(question, dict):
        if question.get("optional_classification_question"):
            return True
        config = question.get("config") or {}
    else:
        config = getattr(question, "config", None) or {}
    return bool(isinstance(config, dict) and config.get("optional_classification_question"))


def _question_id(question) -> str:
    raw = question.get("question_id") if isinstance(question, dict) else question.question_id
    return str(raw or "")[:10]


def _answer_options(question) -> list:
    raw = question.get("answer_options") if isinstance(question, dict) else question.answer_options
    return list(raw or [])


def resolve_screening_option_id(question, answer: str, answer_index: Optional[int] = None) -> Optional[str]:
    """Option id for a screening answer. Post-task questions are left unchanged."""
    if _is_optional_question(question):
        return None
    options = _answer_options(question)
    if isinstance(answer_index, int) and 0 <= answer_index < len(options):
        chosen = options[answer_index]
        if isinstance(chosen, dict):
            oid = _option_id(chosen)
            if oid:
                return oid
    text = (answer or "").strip()
    if not text:
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        oid = _option_id(option)
        if not oid:
            continue
        if text == oid or text == _option_text(option):
            return oid
    return None


def _answer_values_for_option(option_id: str, option: dict) -> List[str]:
    values = {option_id}
    text = _option_text(option)
    if text:
        values.add(text[:1000])
    return [value for value in values if value]


def _holder_response_ids(db: Session, study_id: UUID, question_id: str, answer_values: Sequence[str]) -> List[UUID]:
    """Real and synthetic respondents who already chose this option and were not abandoned."""
    values = [value for value in answer_values if value]
    if not values:
        return []
    return list(
        db.execute(
            select(StudyResponse.id)
            .join(ClassificationAnswer, ClassificationAnswer.study_response_id == StudyResponse.id)
            .where(
                StudyResponse.study_id == study_id,
                StudyResponse.is_abandoned.is_(False),
                ClassificationAnswer.question_id == question_id,
                ClassificationAnswer.answer.in_(values),
            )
            .distinct()
        ).scalars().all()
    )


def sync_screening_quotas(db: Session, study_id: UUID) -> None:
    """Match quota rows to the limits saved on screening options.

    Existing accepted counts are left in place. A newly added limit starts at
    the number of non-abandoned respondents who already chose that option, and
    those respondents are marked reserved so a later Continue does not count
    them a second time.
    """
    questions = db.execute(
        select(StudyClassificationQuestion).where(StudyClassificationQuestion.study_id == study_id)
    ).scalars().all()

    desired: Dict[OptionKey, Tuple[int, dict]] = {}
    for question in questions:
        config = question.config or {}
        if isinstance(config, dict) and config.get("optional_classification_question"):
            continue
        for option in question.answer_options or []:
            if not isinstance(option, dict):
                continue
            option_id = option.get("id")
            limit = _positive_limit(option.get("max_respondents"))
            if not option_id or limit is None:
                continue
            desired[(str(question.question_id), str(option_id)[:10])] = (limit, option)

    existing = db.execute(
        select(ScreeningOptionQuota).where(ScreeningOptionQuota.study_id == study_id)
    ).scalars().all()
    existing_map = {(row.question_id, row.option_id): row for row in existing}

    for key, row in existing_map.items():
        if key not in desired:
            db.delete(row)
        elif row.max_respondents != desired[key][0]:
            # Update the cap only. Never write accepted_count back from memory.
            db.execute(
                update(ScreeningOptionQuota)
                .where(ScreeningOptionQuota.id == row.id)
                .values(max_respondents=desired[key][0])
            )

    for (question_id, option_id), (limit, option) in desired.items():
        if (question_id, option_id) in existing_map:
            continue
        holder_ids = _holder_response_ids(
            db,
            study_id,
            question_id,
            _answer_values_for_option(option_id, option if isinstance(option, dict) else {}),
        )
        if holder_ids:
            db.execute(
                update(StudyResponse)
                .where(StudyResponse.id.in_(holder_ids))
                .values(quota_reserved=True)
            )
        db.add(
            ScreeningOptionQuota(
                study_id=study_id,
                question_id=question_id,
                option_id=option_id,
                max_respondents=limit,
                accepted_count=len(holder_ids),
            )
        )


def _selected_options(answers: Iterable) -> Dict[str, str]:
    """Last answer wins when the same question appears more than once."""
    selected: Dict[str, str] = {}
    for answer in answers:
        question_id = getattr(answer, "question_id", None)
        raw_answer = getattr(answer, "answer", None)
        if question_id is None or raw_answer is None:
            continue
        selected[str(question_id)] = str(raw_answer)
    return selected


def study_has_screening_quotas(db: Session, study_id: UUID) -> bool:
    """True only when this study saved at least one option limit."""
    return (
        db.execute(
            select(ScreeningOptionQuota.id)
            .where(ScreeningOptionQuota.study_id == study_id)
            .limit(1)
        ).first()
        is not None
    )


def reserve_screening_quotas(db: Session, response: StudyResponse, answers: Sequence) -> str:
    """Lock the selected option rows and take one seat on each, or refuse all of them.

    The caller must already hold a row lock on ``response``.
    Returns ``already``, ``reserved``, ``skipped``, or ``rejected``.
    ``skipped`` means this study has no option limits, so the normal flow is unchanged.
    Rejection does not delete the response; the caller does that in this transaction.
    """
    if response.quota_reserved:
        return "already"

    if not study_has_screening_quotas(db, response.study_id):
        return "skipped"

    selected = _selected_options(answers)
    if not selected:
        response.quota_reserved = True
        return "reserved"

    pairs = list(selected.items())
    # Lock only the chosen options, in a stable order, so two respondents cannot
    # both take the last seat and cannot deadlock on different questions.
    rows = db.execute(
        select(ScreeningOptionQuota)
        .where(
            ScreeningOptionQuota.study_id == response.study_id,
            tuple_(ScreeningOptionQuota.question_id, ScreeningOptionQuota.option_id).in_(pairs),
        )
        .order_by(ScreeningOptionQuota.question_id, ScreeningOptionQuota.option_id)
        .with_for_update()
    ).scalars().all()

    if any(row.accepted_count >= row.max_respondents for row in rows):
        return "rejected"

    for row in rows:
        row.accepted_count = int(row.accepted_count or 0) + 1
    response.quota_reserved = True
    return "reserved"


def quota_question_ids(db: Session, study_id: UUID) -> set:
    return set(
        db.execute(
            select(ScreeningOptionQuota.question_id).where(ScreeningOptionQuota.study_id == study_id)
        ).scalars().all()
    )


def quota_full_redirect_url() -> Optional[str]:
    url = (settings.CINT_QUOTA_FULL_URL or "").strip()
    return url or None


def absorb_unreserved_synthetic_holders(db: Session, study_id: UUID) -> int:
    """Count synthetic answers saved before they shared this limit.

    The response row is locked first, then the option rows, same as Continue.
    Respondents already over the limit stay saved and do not take another seat.
    """
    if not study_has_screening_quotas(db, study_id):
        return 0

    responses = db.execute(
        select(StudyResponse)
        .where(
            StudyResponse.study_id == study_id,
            StudyResponse.session_id.like("synthetic_%"),
            StudyResponse.quota_reserved.is_(False),
            StudyResponse.is_abandoned.is_(False),
        )
        .order_by(StudyResponse.id)
        .with_for_update()
    ).scalars().all()
    if not responses:
        return 0

    questions = db.execute(
        select(StudyClassificationQuestion).where(StudyClassificationQuestion.study_id == study_id)
    ).scalars().all()
    by_id = {str(question.question_id): question for question in questions}
    claimed = 0
    for response in responses:
        answer_rows = db.execute(
            select(ClassificationAnswer).where(ClassificationAnswer.study_response_id == response.id)
        ).scalars().all()
        mapped = []
        for row in answer_rows:
            question = by_id.get(str(row.question_id))
            if question is None:
                continue
            option_id = resolve_screening_option_id(question, row.answer or "", None)
            if not option_id:
                continue
            mapped.append(SimpleNamespace(question_id=str(row.question_id), answer=option_id))
        if not mapped:
            continue
        if reserve_screening_quotas(db, response, mapped) == "reserved":
            claimed += 1
    return claimed


def remaining_screening_seats(db: Session, study_id: UUID) -> Optional[Dict[OptionKey, int]]:
    """Seats still open on each limited option. None when the study has no limits.

    Commits so the row locks taken while counting existing synthetic answers
    are released before any rating work starts.
    """
    if not study_has_screening_quotas(db, study_id):
        return None
    absorb_unreserved_synthetic_holders(db, study_id)
    db.commit()
    rows = db.execute(
        select(ScreeningOptionQuota).where(ScreeningOptionQuota.study_id == study_id)
    ).scalars().all()
    if not rows:
        return None
    return {
        (row.question_id, row.option_id): max(0, int(row.max_respondents) - int(row.accepted_count or 0))
        for row in rows
    }


def _option_choices(question, seat_keys: set) -> List[Tuple[int, dict, Optional[OptionKey]]]:
    """Options on one question. The seat key is set only when that option has a limit."""
    qid = _question_id(question)
    choices: List[Tuple[int, dict, Optional[OptionKey]]] = []
    for index, option in enumerate(_answer_options(question)):
        if not isinstance(option, dict):
            continue
        oid = _option_id(option)
        if not oid:
            continue
        key = (qid, oid)
        choices.append((index, option, key if key in seat_keys else None))
    return choices


def _seat_is_open(seat_key: Optional[OptionKey], remaining: Dict[OptionKey, int]) -> bool:
    if seat_key is None:
        return True
    return remaining.get(seat_key, 0) > 0


def _planned_choice(question, answer: dict, choices: Sequence[Tuple[int, dict, Optional[OptionKey]]]):
    planned_index = answer.get("answer_index")
    if isinstance(planned_index, int):
        for choice in choices:
            if choice[0] == planned_index:
                return choice
    planned_id = resolve_screening_option_id(
        question,
        str(answer.get("answer") or ""),
        planned_index if isinstance(planned_index, int) else None,
    )
    if not planned_id:
        return None
    for choice in choices:
        if _option_id(choice[1]) == planned_id:
            return choice
    return None


def _write_panelist_answer(panelist: dict, question, raw_id, index: int, option: dict) -> None:
    answers = panelist.setdefault("answers", {})
    qid = _question_id(question)
    existing = answers.get(raw_id)
    if not isinstance(existing, dict):
        existing = answers.get(qid)
    if not isinstance(existing, dict):
        existing = {}
    if existing.get("answer_index") == index and existing.get("answer") == (_option_text(option) or existing.get("answer")):
        return
    updated = dict(existing)
    updated["answer"] = _option_text(option) or _option_id(option) or ""
    updated["answer_index"] = index
    answers[raw_id] = updated
    if qid != str(raw_id) and qid in answers:
        answers[qid] = updated


def assign_panelists_within_option_limits(
    panelists: Sequence[dict],
    questions: Sequence,
    seats: Optional[Dict[OptionKey, int]],
) -> Tuple[List[dict], int]:
    """Place each panelist on options that still have room.

    A full option does not drop the respondent. Their answer moves to another
    option on the same question that still has a seat. They are skipped only
    when every option on that question is full.
    """
    if not seats:
        return list(panelists), 0
    remaining = dict(seats)
    seat_keys = set(remaining)
    kept: List[dict] = []
    skipped = 0
    for panelist in panelists:
        answers = panelist.get("answers") or {}
        placements = []
        blocked = False
        for question in questions:
            if _is_optional_question(question):
                continue
            choices = _option_choices(question, seat_keys)
            if not any(choice[2] is not None for choice in choices):
                continue
            raw_id = question.get("question_id") if isinstance(question, dict) else question.question_id
            qid = _question_id(question)
            answer = answers.get(raw_id)
            if not isinstance(answer, dict):
                answer = answers.get(qid) if isinstance(answers.get(qid), dict) else {}
            planned = _planned_choice(question, answer, choices)
            chosen = planned if planned is not None and _seat_is_open(planned[2], remaining) else None
            if chosen is None:
                for choice in choices:
                    if _seat_is_open(choice[2], remaining):
                        chosen = choice
                        break
            if chosen is None:
                blocked = True
                break
            placements.append((question, raw_id, chosen))
        if blocked:
            skipped += 1
            continue
        for question, raw_id, (index, option, seat_key) in placements:
            if seat_key is not None:
                remaining[seat_key] -= 1
            _write_panelist_answer(panelist, question, raw_id, index, option)
        kept.append(panelist)
    return kept, skipped
