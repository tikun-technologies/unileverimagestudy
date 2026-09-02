"""
Orchestrates AI respondent simulation: build study_data, generate panelists,
run AI rating per panelist, and persist via submit_synthetic_respondent.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, Callable, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.synthetic_study_adapter import build_study_data_for_synthetic
from app.synthetic import process_panelist_response, generate_all_panelist_combinations, expand_panelists_to_count
from app.services.response import StudyResponseService
from app.schemas.response_schema import (
    SyntheticRespondentPayload,
    SyntheticClassificationAnswerItem,
    SyntheticTaskRatingItem,
    SyntheticElementShownItem,
)

logger = logging.getLogger(__name__)

# Cap total concurrent OpenAI calls to match standalone single-panelist behavior (avoid 429).
MAX_CONCURRENT_AI = 10

# Retry settings for transient DB errors
MAX_DB_RETRIES = 3
DB_RETRY_DELAY_SECONDS = 1.0


def get_max_panelist_combinations(db: Session, study_id: UUID) -> Optional[int]:
    """
    Return the number of unique classification personas (option combinations)
    that have a matching task slot. This is not a run cap — extra respondents
    cycle those personas. Returns None if study has no tasks or panelists.
    """
    from app.models.study_model import Study

    study = db.get(Study, study_id)
    if not study:
        return None
    study_data = build_study_data_for_synthetic(study, db=db)
    tasks = study_data.get("tasks") or {}
    if not isinstance(tasks, dict) or len(tasks) == 0:
        return None
    classification_questions = study_data.get("classification_questions") or []
    if not classification_questions:
        return None

    total_combinations = 1
    for question in classification_questions:
        answer_options = question.get("answer_options") or []
        option_count = len(answer_options)
        if option_count <= 0:
            return None
        total_combinations *= option_count

    available_panelist_numbers = set()
    for key in tasks:
        try:
            if str(key).isdigit():
                available_panelist_numbers.add(int(key))
        except (ValueError, TypeError):
            continue
    if available_panelist_numbers:
        return sum(1 for number in available_panelist_numbers if 1 <= number <= total_combinations)
    return total_combinations


def _submit_respondent_with_fresh_session(
    study_id: UUID,
    payload: SyntheticRespondentPayload,
    idx: int,
    total: int,
) -> Dict[str, Any]:
    """
    Submit a synthetic respondent using a fresh DB session.
    This avoids stale connection issues during long-running simulations.
    Includes retry logic for transient DB errors.
    """
    from app.db.session import SessionLocal
    
    last_error = None
    
    for attempt in range(MAX_DB_RETRIES):
        db_fresh = SessionLocal()
        try:
            response_service = StudyResponseService(db_fresh)
            result = response_service.submit_synthetic_respondent(study_id, payload)
            return result
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            
            # Check if this is a retryable error (connection issues)
            is_retryable = any(phrase in error_str for phrase in [
                "ssl connection has been closed",
                "connection refused",
                "connection reset",
                "server closed the connection",
                "connection timed out",
                "too many connections",
                "connection pool",
            ])
            
            if is_retryable and attempt < MAX_DB_RETRIES - 1:
                logger.warning(
                    f"Respondent {idx + 1}/{total} DB error (attempt {attempt + 1}/{MAX_DB_RETRIES}): {e}. Retrying..."
                )
                try:
                    db_fresh.rollback()
                except Exception:
                    pass
                time.sleep(DB_RETRY_DELAY_SECONDS * (attempt + 1))  # Exponential backoff
                continue
            else:
                # Non-retryable error or max retries exceeded
                raise
        finally:
            try:
                db_fresh.close()
            except Exception:
                pass
    
    # Should not reach here, but just in case
    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to submit respondent {idx + 1} after {MAX_DB_RETRIES} attempts")


def run_simulation(
    db: Session,
    study_id: UUID,
    study_data: Optional[Dict[str, Any]] = None,
    max_respondents: Optional[int] = None,
    openai_api_key: Optional[str] = None,
    model: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    max_panelist_workers: Optional[int] = None,
    is_special_creator: bool = False,
    randomize: bool = False,
) -> Dict[str, Any]:
    """
    Run AI respondent simulation for a study: generate panelists, rate vignettes
    for each, and store results via submit_synthetic_respondent.
    
    Args:
        db: Database session.
        study_id: Study UUID.
        study_data: Pre-built study_data dict (if None, study is loaded and adapter is used).
        max_respondents: Cap on number of respondents to simulate (default from audience_segmentation or task keys).
        openai_api_key: Optional; uses OPENAI_API_KEY env if not set.
        model: Optional; uses OPENAI_MODEL env or gpt-4o-mini.
        progress_callback: Optional callback(done: int, total: int, message: str) for progress (e.g. background job).
        max_panelist_workers: If > 1, run that many panelists in parallel (AI only); DB writes stay sequential. Default 1.
        randomize: If True, use fallback (random) ratings instead of ChatGPT API.
    
    Returns:
        Summary dict: success, respondents_simulated, message, error (if failed).
    """
    from app.models.study_model import Study
    
    if study_data is None:
        study = db.get(Study, study_id)
        if not study:
            return {"success": False, "respondents_simulated": 0, "message": "Study not found", "error": "Study not found"}
        study_data = build_study_data_for_synthetic(study, db=db)
    study_data["is_special_creator"] = is_special_creator
    study_data["randomize"] = randomize
    if not study_data.get("randomize"):
        from app.synthetic.layer_stimulus import StimulusComposer, is_layer_study
        if is_layer_study(study_data) and not isinstance(study_data.get("_layer_composer"), StimulusComposer):
            study_data["_layer_composer"] = StimulusComposer()
    
    tasks = study_data.get("tasks") or {}
    if not isinstance(tasks, dict) or len(tasks) == 0:
        return {"success": False, "respondents_simulated": 0, "message": "Study has no tasks", "error": "No tasks"}
    
    # Determine N
    if max_respondents is not None and max_respondents >= 1:
        N = max_respondents
    else:
        seg = study_data.get("audience_segmentation") or {}
        N = int(seg.get("number_of_respondents") or 0)
        if N <= 0:
            try:
                task_keys = [k for k in tasks if str(k).isdigit()]
                N = max(int(k) for k in task_keys) if task_keys else 0
            except (ValueError, TypeError):
                N = 0
        if N <= 0:
            return {"success": False, "respondents_simulated": 0, "message": "Could not determine number of respondents", "error": "No number_of_respondents or task keys"}
    
    api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    model_name = model or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    
    # Task keys are available respondent slots. Personas come from classification combos.
    available_panelist_numbers = set()
    for key in tasks:
        try:
            if str(key).isdigit():
                available_panelist_numbers.add(int(key))
        except (ValueError, TypeError):
            continue

    unique_panelists = generate_all_panelist_combinations(study_data)
    if not unique_panelists:
        return {"success": False, "respondents_simulated": 0, "message": "No panelists generated (check classification_questions and answer_options)", "error": "No panelists"}

    if not available_panelist_numbers:
        return {"success": False, "respondents_simulated": 0, "message": "No panelists with tasks", "error": "No panelists with tasks"}

    # Unique personas first; if N is larger, loop from the first persona again.
    # Same expansion is used for AI and randomize — rating method is chosen later per vignette.
    task_numbers = sorted(available_panelist_numbers)
    panelists_to_run = expand_panelists_to_count(unique_panelists, N, task_numbers)
    total = len(panelists_to_run)
    response_service = StudyResponseService(db)
    simulated = 0
    last_error = None
    # Cap panelist workers at MAX_CONCURRENT_AI to match standalone (max_workers=10)
    workers = min(max(1, int(max_panelist_workers or 1)), MAX_CONCURRENT_AI)
    max_vignette_workers = max(1, MAX_CONCURRENT_AI // workers)

    if progress_callback:
        progress_callback(0, total, f"Starting simulation for {total} respondent(s)...")

    def _build_payload(response: Dict[str, Any], panelist: Dict[str, Any], idx: int) -> SyntheticRespondentPayload:
        classification_answers = {}
        for q_id, data in (response.get("classification_answers") or {}).items():
            classification_answers[q_id] = SyntheticClassificationAnswerItem(
                question_id=data.get("question_id", q_id),
                question_text=data.get("question_text", ""),
                answer=data.get("answer", ""),
                answer_index=data.get("answer_index"),
            )
        task_ratings = []
        for tr in response.get("task_ratings") or []:
            elements_shown = []
            for e in tr.get("elements_shown") or []:
                if isinstance(e, dict):
                    elements_shown.append(SyntheticElementShownItem(
                        key=e.get("key"),
                        element_id=str(e.get("element_id")) if e.get("element_id") is not None else None,
                        name=e.get("name"),
                        content=e.get("content"),
                        category_name=e.get("category_name"),
                        element_type=e.get("element_type"),
                    ))
                else:
                    elements_shown.append(SyntheticElementShownItem(
                        key=getattr(e, "key", None),
                        element_id=str(getattr(e, "element_id", None)) if getattr(e, "element_id", None) is not None else None,
                        name=getattr(e, "name", None),
                        content=getattr(e, "content", None),
                        category_name=getattr(e, "category_name", None),
                        element_type=getattr(e, "element_type", None),
                    ))
            task_ratings.append(SyntheticTaskRatingItem(
                task_id=tr.get("task_id", ""),
                task_index=tr.get("task_index"),
                main_question=tr.get("main_question"),
                vignette_content=tr.get("vignette_content"),
                rating=tr.get("rating"),
                reasoning=tr.get("reasoning"),
                elements_shown=elements_shown if elements_shown else None,
                method=tr.get("method"),
            ))
        personal_info = {}
        if panelist.get("gender") is not None:
            personal_info["gender"] = str(panelist["gender"]).strip().lower()
        if panelist.get("age") is not None:
            personal_info["age"] = int(panelist["age"])
        if panelist.get("age_range"):
            personal_info["age_range"] = str(panelist["age_range"]).strip()
        return SyntheticRespondentPayload(
            panelist_id=response.get("panelist_id") or f"panelist_{response.get('panelist_number', idx + 1):06d}",
            panelist_number=response.get("panelist_number", idx + 1),
            classification_answers=classification_answers,
            task_ratings=task_ratings,
            personal_info=personal_info if personal_info else None,
        )

    def _run_one_panelist(args: Tuple[int, Dict[str, Any]]) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
        idx, panelist = args
        try:
            lookup = panelist.get("task_lookup_number", panelist.get("panelist_number"))
            panelist_number = panelist.get("panelist_number")
            tasks_for_panelist = {str(panelist_number): tasks.get(str(lookup), [])}
            resp = process_panelist_response(
                panelist_json=panelist,
                tasks_json=tasks_for_panelist,
                study_data=study_data,
                openai_api_key=api_key,
                model=model_name,
                max_vignette_workers=max_vignette_workers,
            )
            return (idx, resp, None)
        except Exception as e:
            return (idx, None, str(e))

    if workers <= 1:
        # Sequential: original loop with fresh DB session per respondent
        for idx, panelist in enumerate(panelists_to_run):
            pnum = panelist.get("panelist_number", idx + 1)
            if progress_callback:
                progress_callback(idx, total, f"Respondent {idx + 1}/{total} starting (panelist {pnum})...")
            idx_result, response, err = _run_one_panelist((idx, panelist))
            if err:
                last_error = err
                continue
            payload = _build_payload(response, panelist, idx)
            try:
                # Use fresh session to avoid stale connection after long AI processing
                submit_result = _submit_respondent_with_fresh_session(study_id, payload, idx, total)
                simulated += 1
                session_id = submit_result.get("session_id", "")
                if progress_callback:
                    progress_callback(simulated, total, f"Respondent {simulated}/{total} done — session_id: {session_id}")
            except Exception as e:
                last_error = str(e)
                logger.error(f"Respondent {idx + 1}/{total} submission failed: {e}")
                if progress_callback:
                    progress_callback(simulated, total, f"Respondent {idx + 1}/{total} failed: {e}")
    else:
        # Parallel AI phase: run process_panelist_response in threads.
        # Write to DB in index order as soon as each result is available (buffer approach),
        # so progress updates start as soon as the first panelist completes, not after all N complete.
        # Each DB write uses a fresh session to avoid stale connections.
        results_by_idx: Dict[int, Tuple[Optional[Dict[str, Any]], Optional[str]]] = {}
        next_idx_to_write = 0

        def drain_writes():
            """Write any consecutive completed results that are next in order using fresh sessions."""
            nonlocal simulated, next_idx_to_write, last_error
            while next_idx_to_write < total and next_idx_to_write in results_by_idx:
                idx = next_idx_to_write
                panelist = panelists_to_run[idx]
                response, err = results_by_idx[idx]
                next_idx_to_write += 1
                if err:
                    last_error = err
                    if progress_callback:
                        progress_callback(simulated, total, f"Respondent {idx + 1}/{total} AI failed: {err}")
                    continue
                if response is None:
                    continue
                payload = _build_payload(response, panelist, idx)
                try:
                    # Use fresh session to avoid stale connection after long AI processing
                    submit_result = _submit_respondent_with_fresh_session(study_id, payload, idx, total)
                    simulated += 1
                    session_id = submit_result.get("session_id", "")
                    if progress_callback:
                        progress_callback(simulated, total, f"Respondent {simulated}/{total} done — session_id: {session_id}")
                except Exception as e:
                    last_error = str(e)
                    logger.error(f"Respondent {idx + 1}/{total} submission failed: {e}")
                    if progress_callback:
                        progress_callback(simulated, total, f"Respondent {idx + 1}/{total} failed: {e}")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_run_one_panelist, (idx, panelist)): idx for idx, panelist in enumerate(panelists_to_run)}
            for future in as_completed(futures):
                try:
                    idx, response, err = future.result()
                    results_by_idx[idx] = (response, err)
                except Exception as e:
                    idx = futures[future]
                    results_by_idx[idx] = (None, str(e))
                drain_writes()
        drain_writes()

    return {
        "success": True,
        "respondents_simulated": simulated,
        "message": f"Simulated {simulated} of {total} respondents." + (f" Last error: {last_error}" if last_error and simulated < total else ""),
    }
