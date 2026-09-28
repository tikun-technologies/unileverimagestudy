"""Default Design Element Appeal report, ready to call from a FastAPI route.

The function accepts the analysis export as a dict, JSON text, bytes, or a
file path, and returns the PowerPoint bytes plus a download filename. It does
not import FastAPI.

    from reportgen.default_report import DefaultReportError, create_default_report

    report = create_default_report(analysis_json, study=study_json)
    # report.content, report.filename, report.media_type
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from app.services.reportgen.build import build_report

AnalysisInput = Union[dict, str, bytes, bytearray, Path]

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class DefaultReportError(Exception):
    """The appeal report could not be built from the supplied analysis."""


@dataclass(frozen=True)
class DefaultReport:
    content: bytes
    filename: str
    title: str
    media_type: str = MEDIA_TYPE


def create_default_report(
    analysis: AnalysisInput,
    *,
    study: AnalysisInput | None = None,
    logo: bytes | bytearray | str | Path | None = None,
    download_images: bool = True,
    output_path: str | Path | None = None,
    filename: str | None = None,
    study_type: str | None = None,
    cache_dir: str | Path | None = None,
    prepared_by: str | None = None,
) -> DefaultReport:
    """Build the default appeal deck and return it as bytes.

    ``study`` is optional. When the analysis is a file path and
    ``study_data.json`` sits beside it, that file is used for design
    constraints. Pass ``study`` to supply those constraints in memory.
    ``logo`` replaces the default cover mark when a brand image is available.
    """
    try:
        payload = _as_payload(analysis)
        title = _title(payload)
        with tempfile.TemporaryDirectory(prefix="appeal-report-") as tmp:
            folder = Path(tmp)
            analysis_path = folder / "analysis_data.json"
            _write_json(payload, analysis_path)
            _place_study(analysis, study, folder)
            logo_path = _place_logo(logo, folder)
            destination = Path(output_path) if output_path else folder / "report.pptx"
            built = build_report(
                analysis_path,
                destination,
                download_images=download_images,
                logo_path=logo_path,
                study_type=study_type or _study_type(payload),
                cache_dir=cache_dir,
                prepared_by=prepared_by,
            )
            content = built.read_bytes()
    except DefaultReportError:
        raise
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise DefaultReportError(str(exc)) from exc
    return DefaultReport(content=content, filename=filename or _filename(title), title=title)


def _as_payload(value: AnalysisInput) -> dict:
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, Path) or (isinstance(value, str) and Path(value).is_file()):
        payload = json.loads(Path(value).read_text(encoding="utf-8"))
    elif isinstance(value, (bytes, bytearray)):
        if not value:
            raise DefaultReportError("Analysis JSON is empty.")
        payload = json.loads(bytes(value).decode("utf-8"))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise DefaultReportError("Analysis JSON is empty.")
        payload = json.loads(text)
    else:
        raise DefaultReportError("Analysis must be a JSON object, JSON text, or a file path.")
    if not isinstance(payload, dict):
        raise DefaultReportError("Analysis JSON must be an object.")
    if "analysis" not in payload and "(T) Overall" in payload:
        payload = {"analysis": payload}
    return payload


def _write_json(payload: dict, path: Path) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _place_study(analysis: AnalysisInput, study: AnalysisInput | None, folder: Path) -> None:
    """Keep study_data.json beside the analysis so pack constraints still load."""
    if study not in (None, b"", ""):
        _write_json(_as_object(study), folder / "study_data.json")
        return
    if isinstance(analysis, Path) or (isinstance(analysis, str) and Path(analysis).is_file()):
        sibling = Path(analysis).with_name("study_data.json")
        if sibling.is_file():
            (folder / "study_data.json").write_bytes(sibling.read_bytes())


def _as_object(value: AnalysisInput) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, Path) or (isinstance(value, str) and Path(value).is_file()):
        payload = json.loads(Path(value).read_text(encoding="utf-8"))
    elif isinstance(value, (bytes, bytearray)):
        payload = json.loads(bytes(value).decode("utf-8"))
    elif isinstance(value, str):
        payload = json.loads(value)
    else:
        raise DefaultReportError("Study data must be a JSON object, JSON text, or a file path.")
    if not isinstance(payload, dict):
        raise DefaultReportError("Study data JSON must be an object.")
    return payload


def _place_logo(logo: bytes | bytearray | str | Path | None, folder: Path) -> Path | None:
    if logo is None or logo == b"" or logo == "":
        return None
    if isinstance(logo, (bytes, bytearray)):
        path = folder / "logo.png"
        path.write_bytes(bytes(logo))
        return path
    path = Path(logo)
    if not path.is_file():
        raise DefaultReportError(f"Logo file not found: {path}")
    return path


def _study_type(payload: dict) -> str | None:
    value = payload.get("study_type")
    if value is None and isinstance(payload.get("analysis"), dict):
        info = payload["analysis"].get("Information Block") or {}
        value = info.get("Study Type")
    if not value:
        return None
    return str(value)


def _title(payload: dict) -> str:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    front = analysis.get("Front Page") or {}
    info = analysis.get("Information Block") or {}
    title = front.get("Title") or info.get("Study Title") or payload.get("title")
    return str(title).strip() if title else "Design Element Appeal"


def _filename(title: str) -> str:
    cleaned = re.sub(r"[^\w\s.\-]+", "", title, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:80] or "Appeal Report"
    return f"{cleaned} Appeal Report.pptx"
