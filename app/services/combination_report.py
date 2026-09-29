"""Design Combination Readout for one report-builder category.

The category row the analytics page already stores is the saved-combinations
group this deck reads. Analysis is optional and should be the copy already
loaded for the page so the regression is not run again.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from app.models.study_model import Study
from app.services.appeal_report import _image_cache_dir, _prepared_by_credit
from app.services.reportgen.combinations import build_combination_readout

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class CombinationReportError(Exception):
    """The combination readout could not be built from this category."""


def build_category_pptx(
    *,
    study_obj: Study,
    category: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
    prepared_by_name: Any = None,
    prepared_by_email: Any = None,
    download_images: bool = True,
) -> tuple[bytes, str]:
    items = category.get("items") or []
    if not items:
        raise CombinationReportError("Add at least one combination to this category before downloading.")
    if not any(_item_has_elements(item) for item in items):
        raise CombinationReportError("This category has no combinations with elements to include in the report.")

    study_type = str(getattr(study_obj, "study_type", "") or "").lower() or None
    name = str(category.get("name") or "Category").strip() or "Category"
    with tempfile.TemporaryDirectory(prefix="combination-report-") as tmp:
        folder = Path(tmp)
        combinations_path = folder / "saved_combinations.json"
        study_path = folder / "study_data.json"
        output_path = folder / "readout.pptx"
        combinations_path.write_text(
            json.dumps([category], default=str),
            encoding="utf-8",
        )
        study_path.write_text(
            json.dumps(_study_payload(study_obj), default=str),
            encoding="utf-8",
        )
        analysis_path = None
        if isinstance(analysis, dict) and analysis:
            analysis_path = folder / "analysis_data.json"
            analysis_path.write_text(
                json.dumps(_analysis_payload(analysis, study_obj.id), default=str),
                encoding="utf-8",
            )
        try:
            built = build_combination_readout(
                combinations_path,
                study_path,
                output_path,
                analysis_path=analysis_path,
                download_images=download_images,
                study_type=study_type,
                cache_dir=_image_cache_dir(study_obj.id),
                prepared_by=_prepared_by_credit(prepared_by_name, prepared_by_email),
            )
        except ValueError as exc:
            raise CombinationReportError(str(exc)) from exc
        return built.read_bytes(), _filename(name)


def _study_payload(study_obj: Study) -> Dict[str, Any]:
    config = study_obj.audience_segmentation if isinstance(study_obj.audience_segmentation, dict) else {}
    return {
        "id": str(study_obj.id),
        "title": study_obj.title or "",
        "main_question": " ".join(str(getattr(study_obj, "main_question", "") or "").split()),
        "study_type": str(study_obj.study_type or ""),
        "created_at": study_obj.created_at.isoformat() if study_obj.created_at else "",
        "study_config": {
            "aspect_ratio": config.get("aspect_ratio") or "9:16",
            "country": config.get("country") or "",
            "number_of_respondents": config.get("number_of_respondents") or 0,
        },
    }


def _analysis_payload(analysis: Dict[str, Any], study_id: Any) -> Dict[str, Any]:
    if isinstance(analysis.get("analysis"), dict):
        payload = dict(analysis)
        payload.setdefault("study_id", str(study_id))
        return payload
    return {"study_id": str(study_id), "analysis": analysis}


def _item_has_elements(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    config = item.get("configuration") if isinstance(item.get("configuration"), dict) else {}
    elements = config.get("selected_elements") or []
    if not isinstance(elements, list):
        return False
    return any(
        isinstance(element, dict) and (element.get("name") or element.get("content") or element.get("image_url"))
        for element in elements
    )


def _filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\s.\-]+", "", name, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:80] or "Category"
    return f"{cleaned} Combination Readout.pptx"
