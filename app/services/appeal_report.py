"""Design Element Appeal PowerPoint export for study analytics.

Thin wrapper around the vendored ``reportgen`` package. It turns the analysis
JSON the analytics page already computes into the appeal deck and returns the
PowerPoint bytes plus a download filename.

Scalability notes:
- Element artwork is cached on disk per study (keyed by URL inside the folder),
  so a second export — or a filtered re-export — does not re-download images.
- The heavy work (image IO + slide layout) is synchronous; call this from a
  regular ``def`` route so FastAPI runs it in its threadpool instead of the
  event loop.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from app.models.study_model import Study
from app.services.reportgen.default_report import (
    DefaultReport,
    DefaultReportError,
    create_default_report,
)

__all__ = ["DefaultReportError", "build_appeal_pptx"]

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _image_cache_dir(study_id: Any) -> Path:
    """Persistent per-study folder for element artwork.

    Lives under the OS temp dir so it survives across requests on the same
    instance but never leaks between studies.
    """
    root = Path(tempfile.gettempdir()) / "mindsurve-appeal-cache" / str(study_id) / "elements"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _constraint_study_data(study_obj: Study) -> Optional[Dict[str, Any]]:
    """Minimal ``study_data`` payload so the deck honours design constraints.

    Only layer studies carry element-exclusion rules. The generator maps
    ``image_id -> name`` from ``layers`` to resolve each constraint, so we ship
    just those two keys. Returns ``None`` when there is nothing to enforce.
    """
    if str(getattr(study_obj, "study_type", "") or "").lower() != "layer":
        return None

    constraints = getattr(study_obj, "design_constraints", None) or []
    if not constraints:
        return None

    layers = []
    for layer in sorted(getattr(study_obj, "layers", []) or [], key=lambda x: getattr(x, "order", 0)):
        images = []
        for img in getattr(layer, "images", []) or []:
            image_id = getattr(img, "image_id", None)
            name = getattr(img, "name", None)
            if image_id and name:
                images.append({"image_id": str(image_id), "name": str(name)})
        layers.append(
            {
                "layer_id": str(getattr(layer, "layer_id", "") or ""),
                "name": getattr(layer, "name", "") or "",
                "elements": images,
                "images": images,
            }
        )

    return {"design_constraints": constraints, "layers": layers}


def _prepared_by_credit(name: Any, email: Any) -> str:
    person = " ".join(str(name or "").split())
    mail = " ".join(str(email or "").split())
    if person and mail:
        return f"{person} | {mail}"
    return person or mail


def build_appeal_pptx(
    *,
    study_obj: Study,
    analysis: Dict[str, Any],
    download_images: bool = True,
    prepared_by_name: Any = None,
    prepared_by_email: Any = None,
) -> DefaultReport:
    """Build the Design Element Appeal deck.

    ``analysis`` is the analysis object the analytics page already produces
    (the value under ``analysis`` in the analytics session response, or the
    optimized-analysis-json body). Raises :class:`DefaultReportError` when the
    analysis has no scored elements yet (e.g. study still collecting, or a
    filter cohort of zero) so the caller can return a clean message.
    """
    if not isinstance(analysis, dict) or not analysis:
        raise DefaultReportError("Analytics are not ready to export yet.")

    study_type = str(getattr(study_obj, "study_type", "") or "").lower() or None
    payload = _with_main_question(analysis, getattr(study_obj, "main_question", None))

    return create_default_report(
        payload,
        study=_constraint_study_data(study_obj),
        download_images=download_images,
        study_type=study_type,
        cache_dir=_image_cache_dir(study_obj.id),
        prepared_by=_prepared_by_credit(prepared_by_name, prepared_by_email),
    )


def _with_main_question(analysis: Dict[str, Any], main_question: Any) -> Dict[str, Any]:
    """Put the study's main question on the Front Page so the cover can use it."""
    question = " ".join(str(main_question or "").split())
    if not question:
        return analysis
    payload = dict(analysis)
    if isinstance(payload.get("analysis"), dict):
        inner = dict(payload["analysis"])
        front = dict(inner.get("Front Page") or {})
        front["Main Question"] = question
        inner["Front Page"] = front
        payload["analysis"] = inner
        return payload
    front = dict(payload.get("Front Page") or {})
    front["Main Question"] = question
    payload["Front Page"] = front
    return payload
